"""Claude Code's raw SDK messages about background tasks and API retries, on ``AcpClient``.

A session of a backend in ``ACP_BACKENDS_RAW_SDK_TASKS`` asks claude-agent-acp for
them: ``session/new`` and ``session/load`` carry
``_meta.claudeCode.emitRawSDKMessages`` = :data:`RAW_SDK_MESSAGE_FILTER`, a list of
``{type, subtype}`` filters beside ``options`` (0.107.0 ``v1/acp-agent.js`` 7857 reads
it into the session and ``shouldEmitRawMessage``, 8109-8117, matches each Claude Code
message against it). Each message that matches is sent, before the adapter handles it,
as the extension notification ``_claude/sdkMessage`` ``{sessionId, message}``
(2899-2905). The filter names five ``system`` subtypes, each read here:

* ``task_started`` -- a task registered: its ``task_id``, ``task_type``
  (``local_bash`` for a shell command and for a Monitor, ``local_workflow`` for a
  Workflow run), ``description``, ``is_backgrounded``, the ``ambient`` and
  ``skip_transcript`` flags of a housekeeping task, and ``owned_by_subagent`` /
  ``parent_task_id`` on a shell an agent launched (a subagent, or an agent inside a
  Workflow run), whose end Claude Code reports to that agent. Only Workflow runs and
  the shells the main thread launched are kept (:data:`MAX_TRACKED_TASKS` of them, the
  one with nothing left to report dropped first, :func:`_evictable`). Every task frame
  may name its ``run_id``: a resumed task keeps its ``task_id`` and opens a new run, and
  one task's run ids sort, as plain strings, in the order the runs opened. A start
  naming another run than an ended task's replaces its record (:func:`_resumes`), and a
  frame naming a run that opened before the recorded one is late and read as nothing
  (:func:`_late`);
* ``task_progress`` -- a Workflow run's ``workflow_progress`` rows
  (``acp.raw_sdk_workflow_cards``), when the beat carries them;
* ``task_updated`` -- a ``patch``: an ``is_backgrounded`` that moved a foreground
  command to the background, or a final ``status`` (``completed``, ``failed``,
  ``killed``), which ends a Workflow run;
* ``task_notification`` -- a task's end: ``status`` ``completed`` / ``failed`` /
  ``stopped`` and a one-line ``summary``;
* ``api_retry`` -- a request that failed and is retried: ``attempt``, ``max_retries``,
  the ``error`` category (``rate_limit``, ``overloaded``, ...) and ``error_status``.

What they become:

* a Workflow run's agents are native Subagents cards: an ``EVENT_SUBAGENT_LIST``
  roster when they change, and at the run's end every card's final status
  (``raw_sdk_workflow_cards``);
* the end of a background shell or Monitor task is one ``EVENT_HARNESS_NOTICE`` naming
  the task and how it ended (:class:`TaskEndNotice`), with what Claude Code's summary
  adds to the task's name: its exit code. Its start makes none: its tool row shows the
  launch. A foreground command, a housekeeping task, a shell an agent launched, a
  subagent and a Workflow run make none either;
* the first API retry a dashboard turn draws is one warning ``EVENT_HARNESS_NOTICE``
  (:class:`RetryNotice`), carrying the error category as Claude Code names it; the rest
  of that turn's retries make none, and so does a sign-in failure, which Claude Code
  does not retry past its first attempt: it reports the sign-out instead, and
  claude-agent-acp shows no retry for it either (0.107.0 ``v1/acp-agent.js`` 3673-3682,
  ``session-failure-extension.js`` 396-398).

Where they are read. ``client_transport._read_message``, the one reader of the
process's stdout, hands each frame to :func:`take` before any caller sees it. A
``_claude/sdkMessage`` notification of a member backend is read into the client's
state there and is returned to no caller: a turn, a request waiting for its answer, a
compaction wait, the idle reader between prompts and a reader cancelled mid-wait all
read past it, so none of them can lose, reorder or hold it. It moves no activity clock
and no deadline either: a background run sends these whatever the session is doing, so
a stalled foreground tool still trips the tool-stall watchdog while a Workflow reports.
A message of another session, or of a subtype or shape not read here, is taken and read
as nothing. On a backend outside the set :func:`take` takes nothing.

What the state makes waits in a bounded FIFO (:attr:`RawSdkState.pending`): a task's end
notice, a retry notice (one at a time) and a Workflow run whose cards moved. Only a
dashboard chat turn draws it. The dashboard announces its stream with
:func:`expect_dashboard_turn`, and the dispatch that comes next
(``client_turn._dispatch_events``) takes that mark in :func:`begin_draw` and takes what
waits with :func:`pending_events` before each frame it handles: before its first, so a task's end
read between prompts lands above the reply of the cycle Claude Code runs for it, and
after each one, so the cards follow the run. Every other dispatch (a compaction's
``/compact`` stream, a channel's, a cron job's or a Crew subagent's turn) draws nothing
and moves nothing, and neither does a text-only reader (``send_message``,
``send_message_stream``): the state and the FIFO wait for the next dashboard turn, and no
card is marked shown.

The notice text is redacted and bounded here; the card strings are bounded here and
redacted by the cards. State lives on the client as ``_raw_sdk`` (:class:`RawSdkState`),
forgotten with the session or the process (:func:`forget_tasks`), which runs the callback
:func:`on_cards_lost` registered for the cards a dashboard kept open. The dashboard's
mark and the card ids of every Workflow agent seen (:func:`workflow_card_ids`, bounded
by ``NATIVE_CHILD_ROSTER_CAP``) outlive that, as the client's native child ids do. Each
function here whose first parameter is ``client`` reads ``backend``, ``_session_id`` and
reads and writes ``_raw_sdk``; :func:`pending_events` also calls ``_native_child_roster``.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator

from kiro_crew.acp import raw_sdk_workflow_cards as cards
from kiro_crew.acp._dispatch import redact_text
from kiro_crew.acp.session_handle_native_children import NATIVE_CHILD_ROSTER_CAP
from kiro_crew.acp.types import (
    EVENT_HARNESS_NOTICE,
    EVENT_SUBAGENT_LIST,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_RAW_SDK_TASKS

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

logger = logging.getLogger("kiro_crew.acp.client")

#: The extension notification claude-agent-acp sends each matching message as.
METHOD_CLAUDE_SDK_MESSAGE = "_claude/sdkMessage"

SDK_TYPE_SYSTEM = "system"
SUBTYPE_TASK_STARTED = "task_started"
SUBTYPE_TASK_PROGRESS = "task_progress"
SUBTYPE_TASK_UPDATED = "task_updated"
SUBTYPE_TASK_NOTIFICATION = "task_notification"
SUBTYPE_API_RETRY = "api_retry"
_SUBTYPES = (
    SUBTYPE_TASK_STARTED,
    SUBTYPE_TASK_PROGRESS,
    SUBTYPE_TASK_UPDATED,
    SUBTYPE_TASK_NOTIFICATION,
    SUBTYPE_API_RETRY,
)

#: ``_meta.claudeCode.emitRawSDKMessages``: the messages asked for, and no others.
RAW_SDK_MESSAGE_FILTER: tuple[dict[str, str], ...] = tuple(
    {"type": SDK_TYPE_SYSTEM, "subtype": subtype} for subtype in _SUBTYPES
)

#: ``task_type`` of a shell command or a Monitor, and of a Workflow run.
TASK_TYPE_SHELL = "local_bash"
TASK_TYPE_WORKFLOW = "local_workflow"
_TRACKED_TASK_TYPES = frozenset({TASK_TYPE_SHELL, TASK_TYPE_WORKFLOW})

#: How a task ended, by ``task_notification.status`` and by a final
#: ``task_updated.patch.status``.
END_COMPLETED = cards.END_COMPLETED
END_FAILED = cards.END_FAILED
END_STOPPED = cards.END_STOPPED
_NOTIFIED_ENDS = {"completed": END_COMPLETED, "failed": END_FAILED, "stopped": END_STOPPED}
_PATCH_ENDS = {"completed": END_COMPLETED, "failed": END_FAILED, "killed": END_STOPPED}

#: :attr:`TaskEndNotice.kind` and :attr:`RetryNotice.kind`.
NOTICE_KIND_TASK_ENDED = "task_ended"
NOTICE_KIND_API_RETRY = "api_retry"

#: Tasks kept per session, what waits for the next dashboard turn (the oldest dropped
#: past it), the bound on a notice's task name, and the longest summary shown beside it.
MAX_TRACKED_TASKS = 64
MAX_PENDING = 64
TASK_NAME_MAX = 300
SUMMARY_MAX = 200
#: The exit code a shell or Monitor summary reports after the task's name:
#: ``... completed (exit code 0)``, ``... failed with exit code 1``, ``... (exit 1)``.
_EXIT_PART = re.compile(r"\bexit(?: code)? -?\d{1,6}\b")
#: The largest retry count read, and the bound on an error category.
_MAX_COUNT = 10_000
_ERROR_CATEGORY = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
#: The error categories of a sign-in failure, which makes no retry notice.
_SIGN_IN_ERRORS = frozenset({"authentication_failed", "oauth_org_not_allowed"})
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass(frozen=True)
class TaskEndNotice:
    """A background shell or Monitor task ended: *task* (its description), *status*
    (:data:`END_COMPLETED`, :data:`END_FAILED` or :data:`END_STOPPED`) and *detail*,
    what Claude Code's summary adds to the name (:func:`_detail`). Redacted and bounded."""

    task: str
    status: str
    detail: str = ""
    kind: str = NOTICE_KIND_TASK_ENDED


@dataclass(frozen=True)
class RetryNotice:
    """Claude Code is retrying a failed request: retry *attempt* of *max_retries*;
    *reason*, the error category as Claude Code names it (``rate_limit``), or ``""``;
    and *status*, the HTTP status, or None for a request that got no response."""

    attempt: int
    max_retries: int
    reason: str = ""
    status: int | None = None
    kind: str = NOTICE_KIND_API_RETRY


@dataclass
class TaskRecord:
    """A shell or Workflow task of this session, from its ``task_started``.

    ``ended`` is how it ended, once a final ``task_updated`` or its
    ``task_notification`` said; ``notified`` that its ``task_notification`` was read;
    ``run_id`` the run it records, when its frames named one.
    """

    task_type: str
    description: str = ""
    run_id: str = ""
    backgrounded: bool = False
    housekeeping: bool = False
    ended: str = ""
    notified: bool = False
    workflow: cards.WorkflowRun | None = None


@dataclass
class RawSdkState:
    """``AcpClient._raw_sdk``: the session's tasks; what the next dashboard turn draws,
    in the order it was read (a notice, or a Workflow run whose cards moved); whether
    this dashboard turn drew a retry notice; the callback run when the tasks are lost;
    whether the next dispatch is a dashboard turn's (:func:`expect_dashboard_turn`); and
    the card ids of the Workflow agents seen, oldest first (:func:`workflow_card_ids`).
    The last two outlive the session's tasks (:func:`forget_tasks`)."""

    tasks: OrderedDict[str, TaskRecord] = field(default_factory=OrderedDict)
    pending: deque[TaskEndNotice | RetryNotice | cards.WorkflowRun] = field(
        default_factory=lambda: deque(maxlen=MAX_PENDING)
    )
    retry_drawn: bool = False
    on_lost: Callable[[], None] | None = None
    dashboard_turn: bool = False
    seen_cards: OrderedDict[str, None] = field(default_factory=OrderedDict)
    #: Items :attr:`pending` dropped at its bound, and tasks refused at
    #: :data:`MAX_TRACKED_TASKS`, since the last drain said them.
    dropped: int = 0
    untracked: int = 0


def session_meta_filter(backend: str) -> list[dict[str, str]] | None:
    """The ``emitRawSDKMessages`` filter a *backend* session asks for, or None."""
    if backend not in ACP_BACKENDS_RAW_SDK_TASKS:
        return None
    return [dict(entry) for entry in RAW_SDK_MESSAGE_FILTER]


def sdk_message(params: Any) -> dict[str, Any] | None:
    """The SDK message a ``_claude/sdkMessage`` notification carries, when it is one
    of the subtypes read here, else None."""
    message = params.get("message") if isinstance(params, dict) else None
    if not isinstance(message, dict) or message.get("type") != SDK_TYPE_SYSTEM:
        return None
    return message if message.get("subtype") in _SUBTYPES else None


def take(client: AcpClient, msg: JsonRpcMessage) -> bool:
    """Whether *msg* is a ``_claude/sdkMessage`` notification of a backend in the set,
    read into *client*'s state here; ``client_transport._read_message`` returns no
    frame this takes. One of another session, subtype or shape is read as nothing."""
    if (
        msg.method != METHOD_CLAUDE_SDK_MESSAGE
        or msg.id is not None
        or client.backend not in ACP_BACKENDS_RAW_SDK_TASKS
    ):
        return False
    params = msg.params if isinstance(msg.params, dict) else {}
    own = bool(client._session_id) and params.get("sessionId") == client._session_id
    message = sdk_message(params) if own else None
    if message is not None:
        _read(client._raw_sdk, message)
    return True


def _read(state: RawSdkState, message: dict[str, Any]) -> None:
    subtype = message.get("subtype")
    if subtype == SUBTYPE_API_RETRY:
        _retried(state, message)
        return
    task_id = _task_id(message)
    if task_id is None or _late(state.tasks.get(task_id), _run_id(message)):
        return
    if subtype == SUBTYPE_TASK_STARTED:
        _note_started(state, task_id, message)
    elif subtype == SUBTYPE_TASK_PROGRESS:
        _progress(state, task_id, message)
    elif subtype == SUBTYPE_TASK_UPDATED:
        _updated(state, task_id, message)
    else:
        _notified(state, task_id, message)


def _task_id(message: dict[str, Any]) -> str | None:
    task_id = message.get("task_id")
    return task_id if isinstance(task_id, str) and _TASK_ID.match(task_id) else None


def _run_id(message: dict[str, Any]) -> str:
    """The ``run_id`` a task frame names, or "" when it names none usable."""
    run_id = message.get("run_id")
    return run_id if isinstance(run_id, str) and _TASK_ID.match(run_id) else ""


def _late(record: TaskRecord | None, run_id: str) -> bool:
    """Whether a frame naming *run_id* belongs to a run of *record*'s task that opened
    before the one recorded: one task's run ids sort in the order the runs opened, so
    such a frame is late."""
    return record is not None and bool(run_id and record.run_id) and run_id < record.run_id


def _resumes(record: TaskRecord, run_id: str) -> bool:
    """Whether a ``task_started`` naming *run_id* opens a new run of *record*'s ended
    task: a resumed task keeps its ``task_id`` and gets a new ``run_id``."""
    return bool(record.ended and run_id) and run_id != record.run_id


def _plain(value: Any, limit: int) -> str:
    """*value* as redacted, stripped text cut to *limit*, or "" for a non-string."""
    return redact_text(value.strip())[:limit] if isinstance(value, str) else ""


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNT:
        return None
    return value


def forget_tasks(client: Any) -> None:
    """Forget the tasks of a session that ``session/new`` or ``session/load`` replaced,
    or whose process ended, with what waits for the next turn, and run the callback
    :func:`on_cards_lost` registered.

    Tasks run inside the process that started them and are named by its session, so
    none of them reports to the next one. The dashboard's mark (a respawn can come
    between it and the turn's dispatch) and the card ids seen are kept.
    """
    lost = getattr(client, "_raw_sdk", None)
    if not isinstance(lost, RawSdkState):
        return
    client._raw_sdk = RawSdkState(dashboard_turn=lost.dashboard_turn, seen_cards=lost.seen_cards)
    if lost.on_lost is None:
        return
    try:
        lost.on_lost()
    except Exception:
        logger.debug("closing the cards of lost raw SDK tasks failed", exc_info=True)


def on_cards_lost(client: Any, callback: Callable[[], None]) -> None:
    """Have *callback* run once if *client* loses the Workflow runs it knows now, its
    session replaced or its process ended (:func:`forget_tasks`); nothing for a client
    of another backend, or another kind."""
    state = getattr(client, "_raw_sdk", None)
    if (
        isinstance(state, RawSdkState)
        and getattr(client, "backend", None) in ACP_BACKENDS_RAW_SDK_TASKS
    ):
        state.on_lost = callback


def expect_dashboard_turn(client: Any, expected: bool = True) -> None:
    """Have the next turn *client* dispatches draw what waits (:func:`begin_draw`), or,
    with *expected* false, take that back. A dashboard chat turn marks its stream so
    just before opening it, and unmarks it at its release, so a stream that never
    reached a dispatch hands the mark to no other reader. Nothing for a client of another
    backend, or another kind."""
    state = getattr(client, "_raw_sdk", None)
    if (
        isinstance(state, RawSdkState)
        and getattr(client, "backend", None) in ACP_BACKENDS_RAW_SDK_TASKS
    ):
        state.dashboard_turn = expected


def begin_draw(client: AcpClient) -> bool:
    """Start a dispatch; whether it is the dashboard turn :func:`expect_dashboard_turn`
    marked, which takes the mark and alone draws what waits (:func:`pending_events`).

    That turn has drawn no retry notice yet, and every card listed with its final status
    in the last one is settled, so a later turn's fresh card tracker never draws it
    again. Any other dispatch changes nothing: what waits stays for the next dashboard
    turn.
    """
    if client.backend not in ACP_BACKENDS_RAW_SDK_TASKS:
        return False
    state = client._raw_sdk
    if not state.dashboard_turn:
        return False
    state.dashboard_turn = False
    state.retry_drawn = False
    for record in state.tasks.values():
        if record.workflow is not None:
            cards.begin_listing(record.workflow)
    return True


def pending_events(client: AcpClient) -> list[AcpEvent]:
    """What was read since the last call, as the events of the dashboard turn
    :func:`begin_draw` marked, in the order it was read: each notice (one retry notice
    per turn), and one roster for each run of card changes between them
    (:func:`_roster`). Nothing on a backend outside the set."""
    if client.backend not in ACP_BACKENDS_RAW_SDK_TASKS:
        return []
    state = client._raw_sdk
    _say_overflow(state)
    events: list[AcpEvent] = []
    moved: list[cards.WorkflowRun] = []
    while state.pending:
        item = state.pending.popleft()
        if isinstance(item, cards.WorkflowRun):
            moved.append(item)
            continue
        events += _roster(client, moved)
        moved = []
        if isinstance(item, RetryNotice):
            if state.retry_drawn:
                continue
            state.retry_drawn = True
        events.append(AcpEvent(kind=EVENT_HARNESS_NOTICE, harness_notice=item))
    return events + _roster(client, moved)


def _roster(client: AcpClient, runs: list[cards.WorkflowRun]) -> list[AcpEvent]:
    """The roster after *runs* moved: the turn's Agent-tool children
    (``_native_child_roster``), which every roster names so none reads as gone, then
    those runs' agents, a run whose record a resumed run replaced since included.
    Listing an agent with its final status marks it so."""
    rows: list[dict[str, Any]] = [row for run in runs for row in cards.roster_rows(run)]
    if not rows:
        return []
    children = [row for event in client._native_child_roster() for row in event.subagents or []]
    return [AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=children + rows)]


def _agents(client: Any) -> Iterator[tuple[TaskRecord, cards.WorkflowAgent]]:
    """Every Workflow agent *client*'s session knows; none for a client of another
    backend, or another kind."""
    state = getattr(client, "_raw_sdk", None)
    if (
        not isinstance(state, RawSdkState)
        or getattr(client, "backend", None) not in ACP_BACKENDS_RAW_SDK_TASKS
    ):
        return
    for record in state.tasks.values():
        if record.workflow is not None:
            for agent in record.workflow.agents.values():
                yield record, agent


def live_card_ids(client: Any) -> frozenset[str]:
    """The cards of the Workflow agents *client*'s harness still runs.

    The dashboard keeps these open past a turn Crew did not cut short
    (``native_cards._keep_background_open``); a run this client does not know (its
    session was replaced) has none, so its cards close.
    """
    return frozenset(
        agent.card_id for record, agent in _agents(client) if not record.ended and not agent.final
    )


def workflow_card_ids(client: Any) -> frozenset[str]:
    """The cards of every Workflow agent *client* has seen, running or ended, also once
    its record was dropped for a new task or forgotten with its session or process:
    harness-native children with no conversation of their own
    (``client_native_children.native_child_resume_refusal``). The newest
    ``NATIVE_CHILD_ROSTER_CAP`` of them; none for a client of another backend, or
    another kind."""
    state = getattr(client, "_raw_sdk", None)
    if (
        not isinstance(state, RawSdkState)
        or getattr(client, "backend", None) not in ACP_BACKENDS_RAW_SDK_TASKS
    ):
        return frozenset()
    return frozenset(state.seen_cards)


def _moved(state: RawSdkState, run: cards.WorkflowRun) -> None:
    """Have the next pending_events call list *run*, once however often it moved, and remember its
    cards' ids (:func:`workflow_card_ids`)."""
    for agent in run.agents.values():
        if agent.card_id not in state.seen_cards:
            state.seen_cards[agent.card_id] = None
            if len(state.seen_cards) > NATIVE_CHILD_ROSTER_CAP:
                state.seen_cards.popitem(last=False)
    if not any(item is run for item in state.pending):
        _queue(state, run)


def _queue(state: RawSdkState, item: TaskEndNotice | RetryNotice | cards.WorkflowRun) -> None:
    """Queue *item* for the next drain, counting the oldest one a full queue drops."""
    if len(state.pending) == MAX_PENDING:
        state.dropped += 1
    state.pending.append(item)


def _say_overflow(state: RawSdkState) -> None:
    """Log what the queue dropped and the tasks refused since the last drain, once."""
    if state.dropped or state.untracked:
        logger.warning(
            "raw SDK tasks: %d queued notice(s) or card update(s) dropped at %d, and %d "
            "task(s) not tracked at %d live tasks, since the last turn",
            state.dropped,
            MAX_PENDING,
            state.untracked,
            MAX_TRACKED_TASKS,
        )
        state.dropped = state.untracked = 0


def _reported(kept: TaskRecord) -> bool:
    """Whether *kept* ended with nothing left to report: its end was read, or its end
    makes no notice."""
    return bool(kept.ended) and (
        kept.notified
        or kept.task_type == TASK_TYPE_WORKFLOW
        or not kept.backgrounded
        or kept.housekeeping
    )


def _evictable(state: RawSdkState) -> str | None:
    """The task to drop for a new one: the oldest with nothing left to report, else the
    oldest foreground command, which makes no notice unless it moves to the background,
    else the oldest ended one."""
    for wanted in (
        _reported,
        lambda kept: kept.task_type == TASK_TYPE_SHELL and not kept.backgrounded,
        lambda kept: bool(kept.ended),
    ):
        found = next((tid for tid, kept in state.tasks.items() if wanted(kept)), None)
        if found is not None:
            return found
    return None


def _admit(state: RawSdkState, task_id: str, record: TaskRecord) -> bool:
    """Keep *record* under :data:`MAX_TRACKED_TASKS`, dropping one (:func:`_evictable`)."""
    if task_id not in state.tasks and len(state.tasks) >= MAX_TRACKED_TASKS:
        dropped = _evictable(state)
        if dropped is None:
            state.untracked += 1
            return False
        del state.tasks[dropped]
    state.tasks[task_id] = record
    return True


def _housekeeping(message: dict[str, Any]) -> bool:
    return message.get("ambient") is True or message.get("skip_transcript") is True


def _agents_shell(message: dict[str, Any]) -> bool:
    """Whether an agent launched the task (a subagent, or an agent inside a Workflow
    run): Claude Code marks such a shell ``owned_by_subagent`` and names a subagent
    launcher in ``parent_task_id``, and reports its end to that agent."""
    parent = message.get("parent_task_id")
    return message.get("owned_by_subagent") is True or (isinstance(parent, str) and bool(parent))


def _workflow(record: TaskRecord, task_id: str) -> cards.WorkflowRun:
    if record.workflow is None:
        record.workflow = cards.WorkflowRun(task_id=task_id, name=record.description)
    return record.workflow


def _note_started(state: RawSdkState, task_id: str, message: dict[str, Any]) -> None:
    """Record a task's start; one naming a new run of an ended task replaces its record,
    and the new run's cards carry the run id, so none takes an earlier run's card."""
    task_type = message.get("task_type")
    run_id = _run_id(message)
    kept = state.tasks.get(task_id)
    resumed = kept is not None and _resumes(kept, run_id)
    if task_type not in _TRACKED_TASK_TYPES or (kept is not None and not resumed):
        return
    if task_type == TASK_TYPE_SHELL and _agents_shell(message):
        return
    record = TaskRecord(
        task_type=str(task_type),
        description=_plain(message.get("description"), TASK_NAME_MAX),
        backgrounded=message.get("is_backgrounded") is True,
        housekeeping=_housekeeping(message),
        run_id=run_id,
    )
    if task_type == TASK_TYPE_WORKFLOW:
        run = _workflow(record, task_id)
        run.name = cards.card_text(message.get("workflow_name")) or cards.card_text(
            message.get("description")
        )
        run.run_id = run_id if resumed else ""
    if resumed:
        del state.tasks[task_id]
    _admit(state, task_id, record)


def _live_run(record: TaskRecord | None) -> bool:
    """Whether *record* is a Workflow run still running, or a task not known yet (a run
    whose start this session did not read: its rows say what it is)."""
    return record is None or (record.task_type == TASK_TYPE_WORKFLOW and not record.ended)


def _progress(state: RawSdkState, task_id: str, message: dict[str, Any]) -> None:
    rows = message.get("workflow_progress")
    record = state.tasks.get(task_id)
    if not isinstance(rows, list) or not _live_run(record):
        return
    if record is None:
        record = TaskRecord(task_type=TASK_TYPE_WORKFLOW, run_id=_run_id(message))
        if not _admit(state, task_id, record):
            return
        _workflow(record, task_id).name = cards.card_text(message.get("summary"))
    run = _workflow(record, task_id)
    if cards.merge_rows(run, rows):
        _moved(state, run)


def _end_workflow(state: RawSdkState, record: TaskRecord, ended: str, rows: Any) -> None:
    record.ended = ended
    if record.workflow is not None and cards.finish(record.workflow, ended, rows):
        _moved(state, record.workflow)


def _end(table: dict[str, str], status: Any) -> str | None:
    return table.get(status) if isinstance(status, str) else None


def _updated(state: RawSdkState, task_id: str, message: dict[str, Any]) -> None:
    record = state.tasks.get(task_id)
    patch = message.get("patch")
    if record is None or record.ended or not isinstance(patch, dict):
        return
    if patch.get("is_backgrounded") is True:
        record.backgrounded = True
    ended = _end(_PATCH_ENDS, patch.get("status"))
    if ended is None:
        return
    if record.task_type != TASK_TYPE_WORKFLOW:
        # The notification that follows says the end; this only frees the record.
        record.ended = ended
        return
    _end_workflow(state, record, ended, None)


def _detail(summary: str, task: str) -> str:
    """What Claude Code's *summary* of a task's end adds to the task's name *task*.

    A shell or Monitor summary names the task (``Background command "<task>" completed
    (exit code 0)``), so only the exit code it reports after the name is kept. A
    summary that does not name the task is kept whole when it is short.
    """
    if not summary or summary == task:
        return ""
    at = summary.find(task) if task else -1
    if at < 0:
        return summary if len(summary) <= SUMMARY_MAX else ""
    exit_part = _EXIT_PART.search(summary, at + len(task))
    return exit_part.group(0) if exit_part else ""


def _end_notice(record: TaskRecord, ended: str, message: dict[str, Any]) -> TaskEndNotice | None:
    """The notice a shell task's end makes: only a task that ran in the background and
    is not housekeeping makes one."""
    if not record.backgrounded or record.housekeeping or _housekeeping(message):
        return None
    summary = _plain(message.get("summary"), SUMMARY_MAX + 1)
    task = record.description or summary[:TASK_NAME_MAX]
    if not task:
        return None
    return TaskEndNotice(task=task, status=ended, detail=_detail(summary, task))


def _notified(state: RawSdkState, task_id: str, message: dict[str, Any]) -> None:
    record = state.tasks.get(task_id)
    ended = _end(_NOTIFIED_ENDS, message.get("status"))
    if record is None or ended is None or record.notified:
        return
    record.notified = True
    if record.task_type == TASK_TYPE_WORKFLOW:
        if not record.ended:
            _end_workflow(state, record, ended, message.get("workflow_progress"))
        return
    record.ended = ended
    notice = _end_notice(record, ended, message)
    if notice is not None:
        _queue(state, notice)


def _retry_counts(message: dict[str, Any]) -> tuple[int, int] | None:
    """The attempt and the retry budget of a retry that makes a notice, else None."""
    if message.get("error") in _SIGN_IN_ERRORS:
        return None
    attempt, max_retries = _count(message.get("attempt")), _count(message.get("max_retries"))
    return None if attempt is None or max_retries is None else (attempt, max_retries)


def _retried(state: RawSdkState, message: dict[str, Any]) -> None:
    """Queue a retry notice, unless one already waits: a drain draws one per turn."""
    counts = _retry_counts(message)
    if counts is None or any(isinstance(item, RetryNotice) for item in state.pending):
        return
    category = message.get("error")
    reason = (
        category
        if isinstance(category, str) and _ERROR_CATEGORY.match(category) and category != "unknown"
        else ""
    )
    status = _count(message.get("error_status"))
    _queue(
        state,
        RetryNotice(
            attempt=counts[0],
            max_retries=counts[1],
            reason=reason,
            status=status if status is not None and 100 <= status <= 599 else None,
        ),
    )
