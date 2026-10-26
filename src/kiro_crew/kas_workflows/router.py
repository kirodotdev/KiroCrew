"""The gateway's router for KAS's own workflow engine.

Every run has a VISIBLE parent: the dashboard chat that launched it. KAS requires
a parent session for every run and, when the run ends, wakes that parent with a
model turn ("A workflow you launched … completed"). With a real chat as the
parent that turn is the run's summary, in a conversation the user can continue,
instead of a hidden turn nobody reads.

So a run is started from INSIDE its chat's turn pipeline, on the chat's own KAS
process (:func:`launch_from_turn`): the dashboard opens a chat, registers a
pending launch here (:meth:`KasWorkflowRouter.prepare_launch`) and sends the
chat a launch message; the turn that message starts claims the chat's session,
and this module sends ``_kiro/workflow/new`` + ``invoke`` on that process with
the chat's ACP session as the parent. No model turn runs for the launch itself.

When the run ends, KAS's wake turn on the chat's session is started by KAS, not
by a prompt, so nothing in the chat would read it. On the run's ``run_complete``
the router sends the chat an OBSERVE message (:meth:`KasWorkflowRouter.prepare_observe`,
single-use like a launch's); that turn claims the session and, instead of
prompting, reads the engine's turn through the ordinary chat-turn pipeline
(:func:`observe_from_turn`, :meth:`~kiro_crew.acp.session_handle.AcpSessionHandle.observe_turn`),
so its reply shows and its approvals follow the chat's mode.

Every KAS runtime carries a sink bound to this router
(:func:`kiro_crew.acp.workflow_sink.sink_for`), so whichever process a run lives
on, its notifications reach the one :class:`~kiro_crew.kas_workflows.mirror.RunMirror`
every surface reads, and its steps' permission prompts reach the dashboard.

Step approvals fail closed. A request is held for a person only after its
command (if it has one) passed the operator's deny rules
(:mod:`kiro_crew.kas_workflows.policy`); a deny match, a full queue, a dead
runtime or a step this router cannot place is answered with the request's own
reject option.

A step that passed that screen follows the approval mode of the chat that owns
its run, exactly as one of that chat's own tool calls would: under Trust (or a
process-wide override) it is allowed, under Trust reads only a read-only shell
command is, and under Normal it waits for a person. An auto-approval selects the
request's ALLOW-ONCE option, never an always-allow one, so nothing persists in
KAS past the call; it also passes the same name-grant check every Crew
auto-approve tier runs, and it is written to the security event log. A step
whose chat is unknown (no turn has named it yet) waits for a person.

Reads that need no parent (the recipe catalog, a project's run history) go to
any live KAS process, or to a session-less query process spawned for them. A run
is never ``load``-ed or resumed from a process that does not own it: each KAS
process keeps its own in-memory run registry, and two processes driving one run
is the failure this rule exists to prevent. The one exception is boot recovery's
ownership check (:meth:`KasWorkflowRouter.recover_on_boot`), which asks the query
process to ``load`` a run it has never held: KAS serves that from disk,
read-only, registering and claiming nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from kiro_crew.kas_workflows import asks, policy, wire
from kiro_crew.kas_workflows.mirror import RunMirror

logger = logging.getLogger(__name__)

METHOD_REQUEST_PERMISSION = "session/request_permission"
#: KAS's structured question on a step session (``acp.kas_wire.METHOD_USER_INPUT``),
#: sent instead of the legacy bridge while the process announces ``userInput``.
METHOD_USER_INPUT = "_kiro/userInput"

#: Approvals held for a person at once. Past this a step's request is rejected:
#: an unattended run must not grow gateway memory without bound.
MAX_PENDING_APPROVALS = 64

#: Launches registered but not yet taken by their chat's turn, and how long one
#: waits. A launch that was never picked up (the chat was closed, the turn
#: failed before claiming a session) expires instead of lingering.
MAX_PENDING_LAUNCHES = 32
LAUNCH_TTL_SECS = 600.0

#: Longest title / command text kept for display.
_MAX_DISPLAY_CHARS = 2000
#: A plan lists every step with its agent, model and first task line.
_MAX_PLAN_CHARS = 16000

#: Option kinds a person may pick. Anything else on a request is not offered.
_OFFERED_KINDS = frozenset({"allow_once", "allow_always", "reject_once", "reject_always"})

#: Control actions a person may send, and the method each maps to.
CONTROL_METHODS = {
    "pause": wire.METHOD_PAUSE,
    "resume": wire.METHOD_RESUME,
    "cancel": wire.METHOD_CANCEL,
    "delete": wire.METHOD_DELETE,
}

#: How a refused control names its verb (:meth:`KasWorkflowRouter._require`).
_VERBS = {"pause": "pause", "resume": "resume", "cancel": "stop", "delete": "delete"}

#: The message that starts a launch turn. Readable, because it is the user turn
#: the chat shows; the token is what authorizes it, so typing the shape by hand
#: without a token this router issued for that chat starts nothing.
_LAUNCH_RE = re.compile(r"^Start workflow .* \(launch ([0-9a-f]{32})\)\s*$", re.DOTALL)
_RECONNECT_RE = re.compile(r"^Reconnect workflow runs \(reconnect ([0-9a-f]{32})\)\s*$")
#: The message that starts an OBSERVE turn: the chat turn that shows the model
#: turn KAS starts on its own when a run ends (:meth:`KasWorkflowRouter.prepare_observe`).
#: Same rule as a launch: only a token this router issued for that chat counts.
_OBSERVE_RE = re.compile(r"^\[Workflow `[^`\n]*` [a-z]+\] .* \(observe ([0-9a-f]{32})\)\s*$")
#: The word an observe row carries for a step's waking note, by severity. The
#: dashboard draws each as its own muted line (``KasTurnMarker``).
_NOTE_VERBS = {"info": "update", "warning": "waiting", "error": "error"}
#: Every word an observe row may carry; anything else is shown as "ended".
_OBSERVE_VERBS = wire.TERMINAL_RUN_STATUSES | frozenset(_NOTE_VERBS.values())

#: Observe turns waiting for their chat, and runs whose end already got one.
MAX_PENDING_OBSERVES = 32
MAX_OBSERVED_RUNS = 512

#: ``(slot_key, message) -> started``: starts a chat turn with *message* when the
#: chat is open and idle. The dashboard installs it (:meth:`KasWorkflowRouter.set_turn_starter`).
TurnStarter = Callable[[str, str], bool]

#: The pause reason KAS stamps on a run whose owning process died mid-run
#: (``STALE_RUNNING_PAUSE_REASON`` in kiro-agent ``workflow/run-liveness.ts``).
#: Only a run paused for exactly this reason is resumed automatically: any other
#: pause is someone's decision, and reconnecting leaves it paused.
STALE_RUNNING_PAUSE_REASON = wire.STALE_RUNNING_PAUSE_REASON
RECONNECT_REASON = "Reconnected after Kiro Agent restart"

#: Automatic reconnects: at most one per chat per interval, and at most this many
#: tries for one loss, so a run another process still holds is not chased forever.
RECONNECT_INTERVAL_SECS = 120.0
RECONNECT_MAX_TRIES = 3

#: KAS ``STALE_AFTER_MS`` (kiro-agent ``workflow/run-liveness.ts``: 4.5 x
#: ``BEAT_INTERVAL_MS`` 30s): an owner that has not touched a run's beat file
#: for this long has abandoned it. Boot recovery waits it out once (plus
#: slack), so a run the previous gateway's processes held reads stale to KAS,
#: while one a process outside this gateway still drives keeps reading live.
OWNER_ABANDON_SECS = 135.0
BOOT_RECOVERY_SLACK_SECS = 15.0
#: Boot recovery bounds: chats reattached at once, and chats per boot.
BOOT_RECOVERY_CONCURRENCY = 2
BOOT_RECOVERY_MAX_CHATS = 16
#: How long one chat's reconnect turn may take before the next chat goes.
BOOT_RECOVERY_TURN_SECS = 120.0

#: Runs remembered in the ledger. The oldest finished runs go first.
LEDGER_MAX_RUNS = 500
#: Chats whose KAS session id the router remembers for adopting model-started runs.
MAX_PARENT_SESSIONS = 1024


class KasRuntime(Protocol):
    """The slice of :class:`~kiro_crew.acp.runtime.AcpRuntime` the router drives."""

    def is_alive(self) -> bool: ...

    async def extension_request(
        self, method: str, params: dict[str, Any], *, timeout: float = ...
    ) -> dict[str, Any]: ...

    async def send_response(self, request_id: int | str, result: dict[str, Any]) -> None: ...

    async def send_notification(self, method: str, params: dict[str, Any]) -> None: ...

    async def kill(self, *, expected: bool = False, reason: str = "") -> None: ...


RuntimeFactory = Callable[[], Awaitable[KasRuntime]]


class KasWorkflowError(RuntimeError):
    """A request the router could not complete; the message is safe to show."""


class StepNotStarted(KasWorkflowError):
    """The step has no conversation to show yet (or it is not on disk)."""


@dataclass(frozen=True)
class PendingLaunch:
    """A run the dashboard asked for, waiting for its chat's turn to start it."""

    token: str
    slot_key: str
    cwd: str
    source: str
    inputs: Mapping[str, str]
    run_label: str | None
    created_at: float
    #: Launch options: the run's default model and effort for its steps
    #: (``_kiro/workflow/new`` ``modelId`` / ``effortLevel``).
    model: str | None = None
    effort: str | None = None

    def message(self, display_name: str) -> str:
        """The user turn that starts this launch in its chat."""
        name = display_name.replace("`", "'")[:200] or "workflow"
        return f"Start workflow `{name}` in `{self.cwd}` (launch {self.token})"


@dataclass(frozen=True)
class PendingApproval:
    """One step permission request waiting for a person."""

    key: str
    request_id: int | str
    session_id: str
    workflow_id: str
    node_id: str
    title: str
    command: str | None
    options: tuple[Mapping[str, str], ...]
    plan: Mapping[str, Any] | None
    created_at: float
    runtime: "weakref.ReferenceType[Any]"
    #: The unredacted command and title, for re-deciding under a changed mode
    #: (:meth:`KasWorkflowRouter.recheck_pending`). Never sent to a client.
    raw_command: str | None = None
    raw_title: str | None = None
    #: A plan handoff: never answered by a chat's approval mode, Trust included.
    is_plan: bool = False
    #: KAS's plain-text plan (redacted, capped), shown on the card.
    plan_text: str | None = None
    #: What an "always" answer grants in Crew (:func:`_step_grant_key`): the
    #: exact command, or the tool when the request carries no command.
    grant_key: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "workflowId": self.workflow_id,
            "nodeId": self.node_id,
            "title": self.title,
            "command": self.command,
            "options": [
                {
                    **dict(o),
                    "grant": (
                        None
                        if self.is_plan
                        else _option_grant(o.get("kind"), self.command is not None)
                    ),
                }
                for o in self.options
            ],
            # A plan handoff carries KAS's text and, when KAS sent it, the steps
            # to add as a tree (``_meta.kiro.workflowPlan``), already neutralized.
            "plan": asks.plan_payload(self.plan_text, self.plan) if self.is_plan else None,
            "isPlan": self.is_plan,
            "planText": self.plan_text,
            "createdAt": self.created_at,
        }


def _redact(text: str) -> str:
    from kiro_crew.platform import redact_via_context

    return redact_via_context(text[: _MAX_DISPLAY_CHARS * 2])[:_MAX_DISPLAY_CHARS]


def _redact_plan(text: str) -> str:
    """A plan's text for the card: redacted, and capped higher than a title."""
    from kiro_crew.platform import redact_via_context

    return redact_via_context(text[: _MAX_PLAN_CHARS * 2])[:_MAX_PLAN_CHARS]


def _offered_options(params: Mapping[str, Any]) -> tuple[Mapping[str, str], ...]:
    out: list[Mapping[str, str]] = []
    for raw in wire.as_list(params.get("options")):
        item = wire.as_mapping(raw)
        if item is None:
            continue
        option_id = wire.as_str(item.get("optionId"))
        kind = wire.as_str(item.get("kind"))
        if option_id is None or kind not in _OFFERED_KINDS:
            continue
        name = wire.as_str(item.get("name")) or option_id
        out.append({"optionId": option_id, "kind": kind, "name": name[:200]})
    return tuple(out)


def _plan_meta(params: Mapping[str, Any]) -> Mapping[str, Any] | None:
    meta = wire.as_mapping(params.get("_meta"))
    kiro = wire.as_mapping(meta.get("kiro")) if meta else None
    return wire.as_mapping(kiro.get("workflowPlan")) if kiro else None


def _is_plan_approval(params: Mapping[str, Any]) -> bool:
    """Whether a step's request is a workflow plan handoff (``update_workflow``).

    KAS stamps such a request ``_meta.kiro.toolId: "update_workflow"`` and, when
    the plan fits, carries it as ``_meta.kiro.workflowPlan``. Either marks it.
    """
    meta = wire.as_mapping(params.get("_meta"))
    kiro = wire.as_mapping(meta.get("kiro")) if meta else None
    if kiro is None:
        return False
    return wire.as_str(kiro.get("toolId")) == wire.TOOL_UPDATE_WORKFLOW or (
        wire.as_mapping(kiro.get("workflowPlan")) is not None
    )


def _tool_call_text(tool_call: Mapping[str, Any]) -> str | None:
    """The text blocks of a request's ``toolCall.content``, joined; None when empty.

    For a plan handoff this is KAS's plain-text plan, the one copy that is
    always sent (``workflowPlan`` is dropped when the plan is too large).
    """
    parts: list[str] = []
    for raw in wire.as_list(tool_call.get("content")):
        item = wire.as_mapping(raw) or {}
        inner = wire.as_mapping(item.get("content")) or item
        if wire.as_str(inner.get("type")) == "text":
            text = wire.as_str(inner.get("text"))
            if text:
                parts.append(text)
    return "\n\n".join(parts) or None


def _reject_result(params: Mapping[str, Any]) -> dict[str, Any]:
    from kiro_crew.agent_sdk.kas_workflows_driver import reject_option_id

    option_id = reject_option_id(dict(params))
    if option_id is None:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def _alive(ref: "weakref.ReferenceType[Any] | None") -> Any:
    runtime = ref() if ref is not None else None
    return runtime if runtime is not None and runtime.is_alive() else None


#: A chat's approval modes as a step sees them. ``trust`` covers the chat's own
#: Trust, a scoped grant and the process-wide override alike.
MODE_NORMAL = "normal"
MODE_TRUST = "trust"
MODE_TRUST_READS = "trust_reads"

#: ``() -> (mode, reason)``: the owning chat's mode NOW, and the SEL provenance
#: an auto-approval under it records (``trust``, ``yolo``, ``scoped``, ...).
ModeReader = Callable[[], tuple[str, str]]


def _allow_once(options: tuple[Mapping[str, str], ...]) -> str | None:
    for option in options:
        if option.get("kind") == "allow_once":
            return option.get("optionId")
    return None


#: Step grants ("Always allow" in one step) kept per step session, and sessions kept.
MAX_GRANTS_PER_STEP = 64
MAX_GRANT_SESSIONS = 512


def _option_grant(kind: str | None, has_command: bool) -> dict[str, Any]:
    """What picking an option of *kind* grants, as the card states it (gap 21).

    Crew's own Trust model, not KAS's consent scopes: an "always" allow is a
    Crew grant for THIS step instance's session (exact command, else the tool),
    never a KAS-persisted rule. A reject-always is KAS's own refusal for the
    rest of that step session.
    """
    if kind == "allow_always":
        return {"scope": "step", "match": "command" if has_command else "tool"}
    if kind == "reject_always":
        return {"scope": "step", "match": None}
    return {"scope": "once", "match": None}


def _step_grant_key(params: Mapping[str, Any], command: str | None, title: str) -> str:
    """The key an "always" grant matches on: the exact command, else the tool.

    The tool is KAS's ``_meta.kiro.toolId`` when it sent one, else the call's
    title, so a grant for one tool never covers another.
    """
    if command:
        return f"cmd:{command}"
    meta = wire.as_mapping(params.get("_meta"))
    kiro = wire.as_mapping(meta.get("kiro")) if meta else None
    tool = wire.as_str(kiro.get("toolId")) if kiro else None
    return f"tool:{tool}" if tool else f"title:{title}"


@dataclass(frozen=True)
class DashboardHooks:
    """What the router may do in the dashboard besides starting turns.

    - ``notify(key, slot_key, title, body, meta)``: publish one bell note for a
      held step ask (gap 6); ``retire(key)`` marks it read once the ask is gone.
    - ``append_marker(slot_key, content, meta) -> bool``: append one notice row
      to a chat's transcript (gap 10), True when written.

    Every hook is best effort: a missing one or one that raises costs the
    signal, never the run.
    """

    notify: Callable[[str, str, str, str, Mapping[str, Any]], None] | None = None
    retire: Callable[[str], None] | None = None
    append_marker: Callable[[str, str, Mapping[str, Any]], bool] | None = None


async def _name_grant_refusal(command: str) -> Any:
    from kiro_crew.name_grant import refusal_for_command_off_loop

    return await refusal_for_command_off_loop(command)


def _read_only(command: str) -> bool:
    from kiro_crew.security.readonly_bash import is_read_only_bash

    return is_read_only_bash(command)


def _audit_auto_approval(**fields: Any) -> None:
    from kiro_crew.sel import sel

    sel().log_tool_invocation(**fields)


class _RuntimeSink:
    """One runtime's view of the router: the reader loop's frames, tagged with it."""

    def __init__(self, router: "KasWorkflowRouter", runtime: Any) -> None:
        self._router = router
        self._runtime = weakref.ref(runtime)

    def claims_notification(self, method: str) -> bool:
        return method in wire.NOTIFICATION_METHODS

    def on_notification(self, method: str, params: dict[str, Any]) -> None:
        self._router._on_notification(self._runtime, method, params)

    def watches_notification(self, method: str) -> bool:
        return method == wire.NOTIFY_SESSION_NOTIFY

    def on_watched_notification(self, method: str, params: dict[str, Any]) -> None:
        self._router._on_watched_notification(method, params)

    def claims_step_request(self, session_id: str, method: str) -> bool:
        return self._router._claims_step(self._runtime, session_id, method)

    def on_step_request(
        self, request_id: int | str, session_id: str, method: str, params: dict[str, Any]
    ) -> None:
        self._router._on_step_request(self._runtime, request_id, session_id, params, method)


class KasWorkflowRouter:
    """Routes KAS workflow traffic for the dashboard. See the module docstring."""

    def __init__(
        self,
        query_runtime_factory: RuntimeFactory,
        *,
        mirror: RunMirror | None = None,
        screen: Callable[[str], str | None] = policy.deny_reason,
        clock: Callable[[], float] = time.time,
        max_pending: int = MAX_PENDING_APPROVALS,
        request_timeout: float = 30.0,
        ledger_path: Path | None = None,
        name_grant: Callable[[str], Awaitable[Any]] = _name_grant_refusal,
        read_only: Callable[[str], bool] = _read_only,
        audit: Callable[..., None] = _audit_auto_approval,
    ) -> None:
        self._query_factory = query_runtime_factory
        self.mirror = mirror or RunMirror(clock=clock)
        self._screen = screen
        self._name_grant = name_grant
        self._read_only = read_only
        self._audit = audit
        self._clock = clock
        self._max_pending = max_pending
        self._timeout = request_timeout
        #: Every runtime with a sink, for reads that need no parent.
        self._runtimes: "weakref.WeakSet[Any]" = weakref.WeakSet()
        self._query_runtime: KasRuntime | None = None
        self._query_lock = asyncio.Lock()
        #: Which process drives each run (in memory: a process is not durable).
        self._run_runtime: dict[str, "weakref.ReferenceType[Any]"] = {}
        #: Which chat parents each run the dashboard launched, ON DISK, so a
        #: gateway restart still knows where to reconnect it:
        #: ``{workflowId: {"slot", "cwd", "createdAt", "ended", "workspace"}}``.
        self._ledger_path = ledger_path
        self._ledger: dict[str, dict[str, Any]] = {}
        self._ledger_loaded = ledger_path is None
        self._ledger_lock = asyncio.Lock()
        #: Serializes ledger WRITES: each save snapshots the ledger inside it, so
        #: two saves can never land on disk in reverse order (an older snapshot
        #: overwriting a newer adopt / end).
        self._ledger_save_lock = asyncio.Lock()
        #: Step sessions with a person's reply on its way to KAS right now.
        self._replies_in_flight: set[str] = set()
        self._reconnects: dict[str, tuple[str, float]] = {}
        self._reconnect_tries: dict[str, tuple[int, float]] = {}
        #: Which process each step session lives on (from its node_start).
        self._step_runtime: dict[str, "weakref.ReferenceType[Any]"] = {}
        #: Which chat each parent ACP session belongs to, learned on that chat's
        #: turns, so a run its model starts (KAS's run_workflow) is adopted.
        self._parent_slot: dict[str, str] = {}
        #: Each chat's approval-mode reader, from its turns (note_chat_session).
        self._slot_mode: dict[str, ModeReader] = {}
        #: Each chat's workspace, from its turns, stamped on the runs it starts.
        self._slot_workspace: dict[str, str] = {}
        #: The chat mode each held approval was last decided under (recheck_pending).
        self._decided_under: dict[str, str | None] = {}
        #: Runs whose step tree was read with ``inspect`` (or is being read).
        self._inspecting: set[str] = set()
        self._inspected: set[str] = set()
        self._launches: dict[str, PendingLaunch] = {}
        #: Observe turns issued and not yet taken:
        #: ``{token: (slot_key, createdAt, parentSessionId)}``.
        self._observes: dict[str, tuple[str, float, str]] = {}
        #: Runs whose end already scheduled an observe turn (insertion-ordered).
        self._observed_runs: dict[str, None] = {}
        self._turn_starter: TurnStarter | None = None
        self._pending: dict[str, PendingApproval] = {}
        #: Step questions (``user_input``) held for a person. Never answered by
        #: an approval mode: picking an answer is not an allow.
        self._questions: dict[str, asks.PendingQuestion] = {}
        #: Each chat's current (runtime, ACP session id), from its turns, so a
        #: finished run can be retried or run again on its chat's live process.
        self._slot_binding: dict[str, tuple["weakref.ReferenceType[Any]", str]] = {}
        self._tasks: set[asyncio.Task] = set()
        #: Dashboard side effects: bell notes and transcript marker rows.
        self._hooks = DashboardHooks()
        #: Run ends that already got their outcome marker row (insertion-ordered).
        self._marked_ends: dict[str, None] = {}
        #: Crew "always allow in this step" grants: step session -> grant keys.
        self._step_grants: dict[str, set[str]] = {}
        #: Bumped on every change a surface would render; a cheap poll key.
        self.revision = 0
        #: Bumped on every ``recipes_changed`` push (the recipe list's poll key).
        self.recipes_revision = 0

    # ── Sinks (run ON a runtime's reader loop: never block) ──

    def sink_for(self, runtime: Any) -> _RuntimeSink:
        self._runtimes.add(runtime)
        return _RuntimeSink(self, runtime)

    def _on_notification(
        self, runtime: "weakref.ReferenceType[Any]", method: str, params: dict[str, Any]
    ) -> None:
        if method == wire.NOTIFY_RECIPES_CHANGED:
            self.revision += 1
            self.recipes_revision += 1
            return
        workflow_id = wire.as_str(params.get("workflowId"))
        if workflow_id:
            # The process a run's notifications arrive on is the one driving it;
            # this also adopts a run a chat's model started on its own. A run's
            # parent is fixed once known (gap 29), so a conflicting
            # ``parentSessionId`` never adopts it into another chat.
            self._run_runtime[workflow_id] = runtime
            if workflow_id not in self._ledger:
                parent = self.mirror.parent_of(workflow_id) or wire.as_str(
                    params.get("parentSessionId")
                )
                slot_key = self._parent_slot.get(parent or "")
                if slot_key is not None:
                    self._spawn(self._adopt(workflow_id, slot_key))
        if method == wire.NOTIFY_NODE_START:
            step = wire.as_str(params.get("sessionId"))
            if step:
                self._step_runtime[step] = runtime
        if self.mirror.apply_notification(method, params) is not None:
            self.revision += 1
        if workflow_id and method == wire.NOTIFY_RUN_START:
            # Running again (resume, retry, restart): its next end is a new one.
            self._marked_ends.pop(workflow_id, None)
        if workflow_id and method == wire.NOTIFY_RUN_COMPLETE:
            self._mark_outcome(workflow_id, params)
            self._schedule_observe(workflow_id, params)
        if workflow_id and self.mirror.status(workflow_id) in wire.TERMINAL_RUN_STATUSES:
            self._note_ended(workflow_id)
        if (
            workflow_id
            and workflow_id not in self._inspecting
            and self._serves(runtime(), wire.METHOD_INSPECT)
        ):
            # A run this router did not create (a chat model's run_workflow) is
            # known only from its lifecycle events, and ``run_start`` carries the
            # recipe name but not the run's display label (``runLabel``) -- nor,
            # on older KAS, the step tree. So a newly seen run is read once, and
            # the run's end once more to settle every step. Both reads use
            # ``inspect``, KAS's read-only snapshot, on the process driving the
            # run -- never ``load``, which would take it over.
            terminal = method == wire.NOTIFY_RUN_COMPLETE
            if terminal or workflow_id not in self._inspected:
                self._inspecting.add(workflow_id)
                self._spawn(self._inspect(runtime, workflow_id))

    async def _inspect(self, runtime: "weakref.ReferenceType[Any]", workflow_id: str) -> None:
        """Fold KAS's read-only snapshot of one run in. Best effort, once per need."""
        try:
            live = _alive(runtime)
            if live is None:
                return
            response = await self._call(live, wire.METHOD_INSPECT, {"workflowId": workflow_id})
            if self.mirror.apply_load(response) is not None:
                self.revision += 1
        except Exception:  # noqa: BLE001 -- a missing tree costs a picture, not a run
            logger.debug("workflow inspect failed for %s", workflow_id, exc_info=True)
        finally:
            self._inspecting.discard(workflow_id)
            self._inspected.add(workflow_id)
            while len(self._inspected) > LEDGER_MAX_RUNS:
                self._inspected.pop()

    def _note_ended(self, workflow_id: str) -> None:
        entry = self._ledger.get(workflow_id)
        if entry is None or entry.get("ended"):
            return
        entry["ended"] = True
        self._spawn(self._save_ledger())

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── Dashboard side effects (gaps 6, 10) ──

    def set_dashboard_hooks(self, hooks: DashboardHooks | None) -> None:
        """Install the bell-note and marker-row hooks (:func:`dashboard_hooks`)."""
        self._hooks = hooks or DashboardHooks()

    @staticmethod
    def _serves(runtime: Any, method: str) -> bool:
        """Whether *runtime* advertised *method* (a legacy agent serves everything)."""
        caps = getattr(runtime, "_agent_capabilities", None) if runtime is not None else None
        return wire.method_served(method, wire.extension_methods(caps))

    def _require(self, runtime: Any, method: str, verb: str) -> None:
        """Refuse a verb the driving Kiro Agent does not serve, in words (gap 20)."""
        if not self._serves(runtime, method):
            raise KasWorkflowError(f"This Kiro Agent version does not support {verb}.")

    def _slot_for_run(self, workflow_id: str, parent: str | None = None) -> str | None:
        """The chat a run belongs to: by its parent session, else by the ledger."""
        parent = parent or self.mirror.parent_of(workflow_id)
        return (self._parent_slot.get(parent) if parent else None) or self.slot_of(workflow_id)

    def _write_marker(self, slot_key: str | None, event: Mapping[str, Any]) -> bool:
        writer = self._hooks.append_marker
        if writer is None or not slot_key:
            return False
        from kiro_crew.messaging.workflow_markers import event_line

        try:
            return bool(writer(slot_key, event_line(event), {"kas_event": dict(event)}))
        except Exception:  # noqa: BLE001 -- a lost row costs the line, not the run
            logger.warning("KAS workflow marker row not written for %s", slot_key, exc_info=True)
            return False

    def _mark_outcome(self, workflow_id: str, params: Mapping[str, Any]) -> None:
        """Write the run's outcome row in its chat, once per run end (gap 10).

        Written whether or not an observe turn follows (the chat may be busy,
        or KAS may not wake it), so every end leaves one line.
        """
        if workflow_id in self._marked_ends:
            return
        status = self.mirror.status(workflow_id)
        if status not in wire.TERMINAL_RUN_STATUSES:
            return
        row = self.mirror.row(workflow_id) or {}
        final = wire.as_mapping(params.get("finalState")) or {}
        parent = wire.as_str(params.get("parentSessionId")) or wire.as_str(
            final.get("parentSessionId")
        )
        event = {
            "type": "outcome",
            "workflowId": workflow_id,
            "name": _redact(str(row.get("name") or workflow_id))[:200],
            "status": status,
            "at": row.get("endedAt"),
            "durationSecs": _duration_secs(row.get("startedAt"), row.get("endedAt")),
        }
        self._marked_ends[workflow_id] = None
        while len(self._marked_ends) > MAX_OBSERVED_RUNS:
            self._marked_ends.pop(next(iter(self._marked_ends)))
        self._write_marker(self._slot_for_run(workflow_id, parent), event)

    def _mark_note(self, workflow_id: str, params: Mapping[str, Any], slot_key: str) -> None:
        """Write a step's note to the run's parent as a row in its chat (gap 10)."""
        node_id = wire.as_str(params.get("nodeId"))
        label = None
        row = self.mirror.row(workflow_id) or {}
        if node_id:
            label = _node_label(row.get("nodes") or [], node_id) or node_id
        message = wire.as_str(params.get("message")) or ""
        self._write_marker(
            slot_key,
            {
                "type": "note",
                "workflowId": workflow_id,
                "name": _redact(str(row.get("name") or workflow_id))[:200],
                "severity": wire.as_str(params.get("severity")) or "info",
                "message": _redact(message)[:_MAX_DISPLAY_CHARS],
                "nodeId": node_id,
                "step": _redact(label)[:200] if label else None,
                "agent": wire.as_str(params.get("agentName")),
                "at": _now_iso(self._clock()),
            },
        )

    def _announce(self, key: str, ask: str, workflow_id: str, node_id: str) -> None:
        """Publish one bell note for an ask held for a person (gap 6). Never raises."""
        notify = self._hooks.notify
        slot_key = self._slot_for_run(workflow_id)
        if notify is None or slot_key is None:
            return
        row = self.mirror.row(workflow_id) or {}
        run_name = _redact(str(row.get("name") or workflow_id))[:120]
        step = _redact(_node_label(row.get("nodes") or [], node_id) or node_id)[:120]
        if ask == "question":
            title = f"{run_name} · {step} has a question for you"
        elif ask == "plan":
            title = f"{run_name} · {step}: the plan is waiting for your approval"
        else:
            title = f"{run_name} · {step} is waiting for your approval"
        body = "A workflow step is waiting for you. Open the chat to answer it."
        try:
            notify(
                key,
                slot_key,
                title,
                body,
                {"workflowId": workflow_id, "nodeId": node_id, "ask": ask},
            )
        except Exception:  # noqa: BLE001 -- a lost note costs the signal, not the ask
            logger.warning("KAS workflow ask notification not published", exc_info=True)

    def _retire(self, key: str) -> None:
        """Mark an ask's bell note read: it was answered, withdrawn or lost."""
        retire = self._hooks.retire
        if retire is None:
            return
        try:
            retire(key)
        except Exception:  # noqa: BLE001
            logger.debug("KAS workflow ask notification not retired", exc_info=True)

    def _claims_step(
        self, runtime: "weakref.ReferenceType[Any]", session_id: str, method: str
    ) -> bool:
        if method not in (METHOD_REQUEST_PERMISSION, METHOD_USER_INPUT):
            return False
        owner = self._step_runtime.get(session_id)
        return (
            owner is not None
            and owner() is runtime()
            and (self.mirror.run_for_step_session(session_id) is not None)
        )

    def _on_step_request(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        session_id: str,
        params: dict[str, Any],
        method: str = METHOD_REQUEST_PERMISSION,
    ) -> None:
        task = asyncio.ensure_future(self._admit(runtime, request_id, session_id, params, method))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _reject_unheld(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        params: dict[str, Any],
        owner: tuple[str, str] | None,
        reason: str,
        command: str | None = None,
    ) -> None:
        """Refuse a step's permission request without holding it, and audit why.

        Every fail-closed refusal (no owning run, a deny rule, nothing to pick,
        a full queue) is a permission decision Crew made on the person's behalf,
        so it leaves the same SEL record an auto-approval does. The audit is
        best effort: a refusal that cannot be logged is still sent, because the
        safe answer must never wait on the log.
        """
        tool_call = wire.as_mapping(params.get("toolCall")) or {}
        title = wire.as_str(tool_call.get("title")) or "Tool call"
        workflow_id, node_id = owner if owner is not None else (None, None)
        try:
            await asyncio.to_thread(
                self._audit,
                session_key=(self.slot_of(workflow_id) if workflow_id else None) or "kas-workflow",
                agent="kas-workflow",
                source="dashboard",
                tool_name=_redact(command or title),
                tool_kind="workflow_step",
                outcome="auto_rejected",
                request_id=str(request_id),
                metadata={"reason": _redact(reason), "workflowId": workflow_id, "nodeId": node_id},
            )
        except Exception:  # noqa: BLE001 -- the refusal goes out regardless
            logger.warning("workflow step refusal not logged", exc_info=True)
        await self._send(runtime, request_id, _reject_result(params))

    async def _admit(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        session_id: str,
        params: dict[str, Any],
        method: str = METHOD_REQUEST_PERMISSION,
    ) -> None:
        owner = self.mirror.run_for_step_session(session_id)
        if method == METHOD_USER_INPUT:
            # A structured question: nothing runs, so no deny-rule screen and
            # no approval mode; it waits for the person (or is dismissed).
            if owner is None:
                await self._send(runtime, request_id, asks.dismiss_result_for(method))
                return
            await self._hold_question(runtime, request_id, session_id, owner, params, method)
            return
        if owner is None:
            await self._reject_unheld(runtime, request_id, params, None, "no run owns this step")
            return
        command = policy.step_command(params)
        if command is not None:
            try:
                reason = await asyncio.to_thread(self._screen, command)
            except Exception:  # noqa: BLE001 -- a screen that cannot run refuses
                logger.warning("workflow step screen failed; rejecting", exc_info=True)
                reason = "the deny-rule screen could not run"
            if reason:
                logger.warning(
                    "workflow step command refused by deny rules (run %s, step %s)",
                    owner[0],
                    owner[1],
                )
                await self._reject_unheld(
                    runtime, request_id, params, owner, f"deny rule: {reason}", command
                )
                return
        options = _offered_options(params)
        if not options:
            await self._reject_unheld(runtime, request_id, params, owner, "no option offered")
            return
        tool_call = wire.as_mapping(params.get("toolCall")) or {}
        title = wire.as_str(tool_call.get("title")) or "Tool call"
        # A plan handoff is the person approving what the run will do next, so
        # it always waits for them, whatever the chat's mode.
        is_plan = _is_plan_approval(params)
        if not is_plan and asks.is_question_request(params):
            # A question waits for the person's answer under every mode.
            await self._hold_question(runtime, request_id, session_id, owner, params)
            return
        grant_key = None if is_plan else _step_grant_key(params, command, title)
        if grant_key is not None and await self._apply_step_grant(
            runtime, request_id, session_id, owner, command, options, title, grant_key
        ):
            return
        if not is_plan and await self._auto_approve(
            runtime, request_id, owner, command, options, title
        ):
            return
        if len(self._pending) >= self._max_pending:
            await self._reject_unheld(
                runtime, request_id, params, owner, "too many asks waiting", command
            )
            return
        plan_text = _tool_call_text(tool_call) if is_plan else None
        approval = PendingApproval(
            key=uuid.uuid4().hex,
            request_id=request_id,
            session_id=session_id,
            workflow_id=owner[0],
            node_id=owner[1],
            title=_redact(title),
            command=_redact(command) if command else None,
            options=options,
            plan=_plan_meta(params),
            created_at=self._clock(),
            runtime=runtime,
            raw_command=command,
            raw_title=title,
            is_plan=is_plan,
            plan_text=_redact_plan(plan_text) if plan_text else None,
            grant_key=grant_key,
        )
        self._pending[approval.key] = approval
        self._decided_under[approval.key] = self._current_mode(owner[0])
        self.revision += 1
        self._announce(approval.key, "plan" if is_plan else "approval", owner[0], owner[1])

    async def recheck_pending(self) -> int:
        """Re-decide held approvals under their chats' CURRENT approval mode.

        A step that asked while its chat was in Normal waits for a person. If
        the person then switches the chat to Trust, that held step must follow
        the switch like every later step does, not keep waiting. Called from the
        dashboard's state poll, so a mode change lands within one poll. Returns
        how many were answered here.
        """
        answered = 0
        for stale in [k for k in self._decided_under if k not in self._pending]:
            self._decided_under.pop(stale, None)
        for key in list(self._pending):
            approval = self._pending.get(key)
            if approval is None or approval.is_plan:
                continue
            mode = self._current_mode(approval.workflow_id)
            if mode == self._decided_under.get(key):
                continue  # nothing changed since this one was last decided
            self._decided_under[key] = mode
            self._pending.pop(key, None)
            ok = False
            try:
                ok = await self._auto_approve(
                    approval.runtime,
                    approval.request_id,
                    (approval.workflow_id, approval.node_id),
                    approval.raw_command,
                    approval.options,
                    approval.raw_title or approval.title,
                )
            finally:
                if ok:
                    answered += 1
                    self.revision += 1
                    self._decided_under.pop(key, None)
                    self._retire(key)
                elif key not in self._pending:
                    self._pending[key] = approval
        return answered

    def _current_mode(self, workflow_id: str) -> str | None:
        """The owning chat's approval mode now, or None when it cannot be read."""
        slot_key = self.slot_of(workflow_id)
        reader = self._slot_mode.get(slot_key) if slot_key else None
        if reader is None:
            return None
        try:
            return reader()[0]
        except Exception:  # noqa: BLE001
            return None

    async def _auto_approve(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        owner: tuple[str, str],
        command: str | None,
        options: tuple[Mapping[str, str], ...],
        title: str,
    ) -> bool:
        """Answer a screened step under its chat's approval mode, if that mode allows it.

        True when the request was answered here; False leaves it for a person.
        Every doubt -- an unknown chat, a reader that raises, no allow-once
        option, a command the name-grant check refuses, a log write that fails
        -- lands on False, so the cost of a wrong guess is one more prompt.
        """
        workflow_id, node_id = owner
        slot_key = self.slot_of(workflow_id)
        reader = self._slot_mode.get(slot_key) if slot_key else None
        if reader is None:
            return False
        try:
            mode, reason = reader()
        except Exception:  # noqa: BLE001 -- an unreadable mode asks
            logger.debug("chat approval mode unreadable for %s", slot_key, exc_info=True)
            return False
        if mode == MODE_TRUST:
            pass
        elif mode == MODE_TRUST_READS:
            if not command or not self._read_only(command):
                return False
            reason = MODE_TRUST_READS
        else:
            return False
        option_id = _allow_once(options)
        if option_id is None:
            return False
        if command:
            try:
                refusal = await self._name_grant(command)
            except Exception:  # noqa: BLE001 -- a check that cannot run asks
                logger.debug("name-grant check failed for a workflow step", exc_info=True)
                return False
            if refusal is not None:
                logger.info("workflow step not auto-approved: the name-grant check refused it")
                return False
        assert slot_key is not None  # a reader was found under it
        try:
            await asyncio.to_thread(
                self._audit,
                session_key=slot_key,
                agent="kas-workflow",
                source="dashboard",
                tool_name=_redact(command or title),
                tool_kind="workflow_step",
                outcome="auto_approved",
                request_id=str(request_id),
                metadata={"reason": reason, "workflowId": workflow_id, "nodeId": node_id},
                # Audit-or-ask: only a critical write raises when the log fails.
                critical=True,
            )
        except Exception:  # noqa: BLE001 -- an approval that cannot be logged asks
            logger.warning("workflow step auto-approval not logged; asking", exc_info=True)
            return False
        return await self._send(
            runtime, request_id, {"outcome": {"outcome": "selected", "optionId": option_id}}
        )

    async def _apply_step_grant(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        session_id: str,
        owner: tuple[str, str],
        command: str | None,
        options: tuple[Mapping[str, str], ...],
        title: str,
        grant_key: str,
    ) -> bool:
        """Answer a request the person already "always allowed" in this step (gap 21).

        The grant is Crew's, not KAS's: it covers exactly this step session and
        this command (or tool). The request has passed the deny-rule screen; a
        command also passes the name-grant check, and every use is written to
        the security event log. Any doubt asks the person again.
        """
        if grant_key not in self._step_grants.get(session_id, set()):
            return False
        option_id = _allow_once(options)
        if option_id is None:
            return False
        if command:
            try:
                refusal = await self._name_grant(command)
            except Exception:  # noqa: BLE001 -- a check that cannot run asks
                return False
            if refusal is not None:
                return False
        workflow_id, node_id = owner
        try:
            await asyncio.to_thread(
                self._audit,
                session_key=self.slot_of(workflow_id) or "kas-workflow",
                agent="kas-workflow",
                source="dashboard",
                tool_name=_redact(command or title),
                tool_kind="workflow_step",
                outcome="auto_approved",
                request_id=str(request_id),
                metadata={"reason": "step_always", "workflowId": workflow_id, "nodeId": node_id},
                critical=True,
            )
        except Exception:  # noqa: BLE001 -- an approval that cannot be logged asks
            logger.warning("workflow step grant use not logged; asking", exc_info=True)
            return False
        return await self._send(
            runtime, request_id, {"outcome": {"outcome": "selected", "optionId": option_id}}
        )

    def _record_step_grant(self, session_id: str, grant_key: str) -> None:
        keys = self._step_grants.pop(session_id, set())
        if len(keys) < MAX_GRANTS_PER_STEP:
            keys.add(grant_key)
        self._step_grants[session_id] = keys
        while len(self._step_grants) > MAX_GRANT_SESSIONS:
            self._step_grants.pop(next(iter(self._step_grants)))

    async def _send(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        result: dict[str, Any],
    ) -> bool:
        live = _alive(runtime)
        if live is None:
            return False
        try:
            await live.send_response(request_id, result)
        except Exception:  # noqa: BLE001 -- a dead runtime fails every wait anyway
            logger.debug("workflow step answer not written", exc_info=True)
            return False
        return True

    # ── Approvals ──

    def _prune_dead_approvals(self) -> None:
        dead = [k for k, a in self._pending.items() if _alive(a.runtime) is None]
        for key in dead:
            self._pending.pop(key, None)
            self._decided_under.pop(key, None)
            self._retire(key)
        gone = [k for k, q in self._questions.items() if _alive(q.runtime) is None]
        for key in gone:
            self._questions.pop(key, None)
            self._retire(key)
        if dead or gone:
            self.revision += 1

    def approvals(self, workflow_id: str | None = None) -> list[dict[str, Any]]:
        self._prune_dead_approvals()
        items = [
            a for a in self._pending.values() if workflow_id is None or a.workflow_id == workflow_id
        ]
        items.sort(key=lambda a: a.created_at)
        return [a.to_payload() for a in items]

    async def answer(self, key: str, option_id: str) -> None:
        """Answer a held approval with one of the options it offered."""
        approval = self._pending.get(key)
        if approval is None:
            raise KasWorkflowError("That approval is no longer waiting.")
        if option_id not in {o["optionId"] for o in approval.options}:
            raise KasWorkflowError("That option was not offered for this approval.")
        kind = next((o.get("kind") for o in approval.options if o["optionId"] == option_id), None)
        sent_id = option_id
        if kind == "allow_always" and not approval.is_plan:
            # Crew's Trust model (gap 21): KAS is answered allow-ONCE, so no rule
            # persists in KAS that would skip Crew's deny-rule screen, and Crew
            # remembers the grant for this step session only.
            once = _allow_once(approval.options)
            if once is not None and approval.grant_key:
                sent_id = once
                self._record_step_grant(approval.session_id, approval.grant_key)
        self._pending.pop(key, None)
        self._decided_under.pop(key, None)
        self.revision += 1
        self._retire(key)
        sent = await self._send(
            approval.runtime,
            approval.request_id,
            {"outcome": {"outcome": "selected", "optionId": sent_id}},
        )
        if not sent:
            raise KasWorkflowError("The step's Kiro Agent session is gone; nothing was answered.")

    # ── Questions ──

    async def _hold_question(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        session_id: str,
        owner: tuple[str, str],
        params: Mapping[str, Any],
        method: str = METHOD_REQUEST_PERMISSION,
    ) -> None:
        """Hold a step's question for a person, or decline it when it cannot be held.

        A question that cannot wait (a full queue, no answer to offer) is
        answered "no answer" in its request's own shape (``cancelled`` on the
        legacy bridge, ``dismissed`` on ``_kiro/userInput``), which KAS reads as
        "move the step on": never a pick made on the person's behalf.
        """
        if len(self._pending) + len(self._questions) >= self._max_pending:
            await self._send(runtime, request_id, asks.dismiss_result_for(method))
            return
        build = (
            asks.question_from_user_input
            if method == METHOD_USER_INPUT
            else asks.question_from_request
        )
        question = build(
            params,
            key=uuid.uuid4().hex,
            request_id=request_id,
            session_id=session_id,
            owner=owner,
            created_at=self._clock(),
            runtime=runtime,
            path=self.mirror.path_of_step_session(owner[0], session_id),
            slot=self.slot_of(owner[0]),
        )
        if question is None:
            await self._send(runtime, request_id, asks.dismiss_result_for(method))
            return
        self._questions[question.key] = question
        self.revision += 1
        self._announce(question.key, "question", owner[0], owner[1])

    def questions(self, workflow_id: str | None = None) -> list[dict[str, Any]]:
        """Every held step question, oldest first (``/state`` ``questions``)."""
        self._prune_dead_approvals()
        items = [
            q
            for q in self._questions.values()
            if workflow_id is None or q.workflow_id == workflow_id
        ]
        items.sort(key=lambda q: q.created_at)
        return [q.to_payload() for q in items]

    async def answer_question(
        self, key: str, answers: Mapping[str, Any] | None, *, dismiss: bool = False
    ) -> None:
        """Answer a held question with one offered option, or dismiss it.

        The answer goes back on the question's own request: the picked option
        (KAS hands its label to the step as the answer) or, dismissed,
        ``cancelled`` (KAS moves the step on without an answer).
        """
        question = self._questions.get(key)
        if question is None:
            raise KasWorkflowError("That question is no longer waiting.")
        if dismiss:
            result: dict[str, Any] = question.dismiss_result()
        else:
            try:
                result = question.answer_result(answers or {})
            except ValueError as exc:
                raise KasWorkflowError(str(exc).capitalize() + ".") from exc
        self._questions.pop(key, None)
        self.revision += 1
        self._retire(key)
        if not await self._send(question.runtime, question.request_id, result):
            raise KasWorkflowError("The step's Kiro Agent session is gone; nothing was answered.")

    async def _withdraw_session_asks(self, session_id: str) -> None:
        """Answer ``cancelled`` to every ask held for *session_id* (its turn was stopped).

        ACP asks a client to answer every open permission request of a session
        it cancels; KAS reads ``cancelled`` as a refusal or a skipped question.
        """
        held: list[tuple[Any, int | str, dict[str, Any]]] = []
        cancelled: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}
        for key in [k for k, a in self._pending.items() if a.session_id == session_id]:
            approval = self._pending.pop(key)
            self._decided_under.pop(key, None)
            self._retire(key)
            held.append((approval.runtime, approval.request_id, cancelled))
        for key in [k for k, q in self._questions.items() if q.session_id == session_id]:
            question = self._questions.pop(key)
            self._retire(key)
            held.append((question.runtime, question.request_id, question.dismiss_result()))
        for runtime, request_id, result in held:
            await self._send(runtime, request_id, result)
        if held:
            self.revision += 1

    # ── Ledger ──

    async def ensure_ledger(self) -> None:
        """Read the run ledger once, off the loop."""
        if self._ledger_loaded:
            return
        async with self._ledger_lock:
            if self._ledger_loaded:
                return
            path = self._ledger_path
            loaded: dict[str, dict[str, Any]] = {}
            if path is not None:
                try:
                    raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
                    data = json.loads(raw)
                    runs = data.get("runs") if isinstance(data, dict) else None
                    for wid, entry in (runs or {}).items():
                        if (
                            isinstance(wid, str)
                            and isinstance(entry, dict)
                            and isinstance(entry.get("slot"), str)
                        ):
                            loaded[wid] = {
                                "slot": entry["slot"],
                                "cwd": str(entry.get("cwd") or ""),
                                "createdAt": float(entry.get("createdAt") or 0),
                                "ended": bool(entry.get("ended")),
                                "workspace": str(entry.get("workspace") or ""),
                            }
                except FileNotFoundError:
                    pass
                except Exception:  # noqa: BLE001 -- a bad ledger loses links, not runs
                    logger.warning("KAS workflow run ledger unreadable; starting empty")
            # Entries written since startup win over what disk held.
            self._ledger = {**loaded, **self._ledger}
            self._ledger_loaded = True

    async def _save_ledger(self) -> None:
        path = self._ledger_path
        if path is None:
            return
        await self.ensure_ledger()
        from kiro_crew.atomic_write import atomic_write

        async with self._ledger_save_lock:
            if len(self._ledger) > LEDGER_MAX_RUNS:
                # Oldest finished runs first, then oldest of all.
                order = sorted(
                    self._ledger,
                    key=lambda w: (not self._ledger[w].get("ended"), self._ledger[w]["createdAt"]),
                )
                for wid in order[: len(self._ledger) - LEDGER_MAX_RUNS]:
                    self._ledger.pop(wid, None)
            # Snapshot INSIDE the lock: a save that waited here writes the ledger
            # as it is now, so the last write to finish is the newest state.
            payload = json.dumps({"version": 1, "runs": self._ledger}, indent=1, sort_keys=True)
            try:
                await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(atomic_write, path, payload, mode=0o600)
            except Exception:  # noqa: BLE001 -- the in-memory ledger still serves
                logger.warning("KAS workflow run ledger not saved", exc_info=True)

    def slot_of(self, workflow_id: str) -> str | None:
        entry = self._ledger.get(workflow_id)
        return entry["slot"] if entry else None

    # ── Chat-started runs ──

    def note_chat_session(
        self,
        slot_key: str,
        provider: Any,
        mode: ModeReader | None = None,
        *,
        workspace: str | None = None,
    ) -> None:
        """Remember which chat *provider*'s KAS session belongs to, and its mode.

        Called on every chat turn, before the prompt goes out, so the
        notifications of a run that turn's model starts already find their chat.
        *mode* reads the chat's approval mode at the moment a step asks, so a
        mode change in the picker reaches the next step without another turn.
        *workspace* is stamped on the ledger entry of a run this chat starts, so
        boot recovery never reattaches it to a chat of another workspace.
        Cheap and synchronous: dict writes, or nothing off Kiro Agent.
        """
        if workspace is not None:
            self._slot_workspace.pop(slot_key, None)
            self._slot_workspace[slot_key] = workspace
            while len(self._slot_workspace) > MAX_PARENT_SESSIONS:
                self._slot_workspace.pop(next(iter(self._slot_workspace)))
        if mode is not None:
            self._slot_mode.pop(slot_key, None)
            self._slot_mode[slot_key] = mode
            while len(self._slot_mode) > MAX_PARENT_SESSIONS:
                self._slot_mode.pop(next(iter(self._slot_mode)))
        binding = session_binding(provider)
        if binding is None:
            return
        session_id = binding[1]
        self._slot_binding.pop(slot_key, None)
        self._slot_binding[slot_key] = (weakref.ref(binding[0]), session_id)
        while len(self._slot_binding) > MAX_PARENT_SESSIONS:
            self._slot_binding.pop(next(iter(self._slot_binding)))
        if self._parent_slot.get(session_id) == slot_key:
            return
        self._parent_slot.pop(session_id, None)
        self._parent_slot[session_id] = slot_key
        while len(self._parent_slot) > MAX_PARENT_SESSIONS:
            self._parent_slot.pop(next(iter(self._parent_slot)))

    async def _adopt(self, workflow_id: str, slot_key: str) -> None:
        """Record a run a chat's model started as that chat's, like a launched one."""
        await self.ensure_ledger()
        if workflow_id in self._ledger:
            return
        self._ledger[workflow_id] = {
            "slot": slot_key,
            "cwd": "",
            "createdAt": self._clock(),
            "ended": self.mirror.status(workflow_id) in wire.TERMINAL_RUN_STATUSES,
            "workspace": self._slot_workspace.get(slot_key, ""),
        }
        self.revision += 1
        await self._save_ledger()

    # ── Launching ──

    def _expire_launches(self) -> None:
        cutoff = self._clock() - LAUNCH_TTL_SECS
        for token in [t for t, la in self._launches.items() if la.created_at < cutoff]:
            self._launches.pop(token, None)

    def prepare_launch(
        self,
        slot_key: str,
        cwd: str,
        source: str,
        inputs: Mapping[str, str],
        *,
        run_label: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> PendingLaunch:
        """Register a run for *slot_key*'s next launch turn to start.

        *model* / *effort* become the run's default for its steps (a step's own
        override still wins; KAS ``_kiro/workflow/new`` ``modelId`` /
        ``effortLevel``).
        """
        if not source:
            raise KasWorkflowError("A workflow source is required.")
        self._expire_launches()
        if len(self._launches) >= MAX_PENDING_LAUNCHES:
            raise KasWorkflowError("Too many workflow launches are waiting to start.")
        launch = PendingLaunch(
            token=secrets.token_hex(16),
            slot_key=slot_key,
            cwd=cwd,
            source=source,
            inputs=dict(inputs),
            run_label=run_label,
            created_at=self._clock(),
            model=model or None,
            effort=effort or None,
        )
        self._launches[launch.token] = launch
        return launch

    @staticmethod
    def launch_token(message: str) -> str | None:
        """The token of a launch message, or None for any other message."""
        match = _LAUNCH_RE.match(message or "")
        return match.group(1) if match else None

    @staticmethod
    def reconnect_token(message: str) -> str | None:
        """The token of a reconnect message, or None for any other message."""
        match = _RECONNECT_RE.match(message or "")
        return match.group(1) if match else None

    @classmethod
    def is_turn_action(cls, message: str) -> bool:
        """Whether *message* is a launch or reconnect the chat turn answers itself."""
        return cls.launch_token(message) is not None or cls.reconnect_token(message) is not None

    def take_launch(self, slot_key: str, token: str) -> PendingLaunch | None:
        """The launch *token* names, consumed, only when it was issued for *slot_key*."""
        self._expire_launches()
        launch = self._launches.get(token)
        if launch is None or launch.slot_key != slot_key:
            return None
        self._launches.pop(token, None)
        return launch

    async def start_in_session(
        self, runtime: Any, session_id: str, launch: PendingLaunch
    ) -> dict[str, Any]:
        """Create and start *launch*'s run with *session_id* as its parent."""
        try:
            params = wire.new_run_params(
                source=launch.source,
                parent_session_id=session_id,
                inputs=launch.inputs,
                run_label=launch.run_label,
                model_id=launch.model,
                effort_level=launch.effort,
            )
        except ValueError as exc:
            raise KasWorkflowError(str(exc)) from exc
        created = await self._call(runtime, wire.METHOD_NEW, params)
        workflow_id = wire.as_str(created.get("workflowId"))
        if workflow_id is None:
            raise KasWorkflowError("Kiro Agent created no run.")
        self._run_runtime[workflow_id] = weakref.ref(runtime)
        await self.ensure_ledger()
        self._ledger[workflow_id] = {
            "slot": launch.slot_key,
            "cwd": launch.cwd,
            "createdAt": self._clock(),
            "ended": False,
            "workspace": self._slot_workspace.get(launch.slot_key, ""),
        }
        await self._save_ledger()
        state = wire.as_mapping(created.get("initialState"))
        if state is not None:
            self.mirror.apply_load({"workflowId": workflow_id, "state": state})
        await self._call(runtime, wire.METHOD_INVOKE, {"workflowId": workflow_id})
        self.revision += 1
        return self._row(workflow_id) or {"workflowId": workflow_id}

    # ── Observing a run's end ──
    #
    # When a run ends, KAS starts a model turn on the run's parent session by
    # itself ("A workflow you launched ... completed") -- but only when that
    # session is idle; a busy one gets the nudge in its steering buffer for its
    # current turn instead. No client sent that turn's prompt, so without a
    # reader its text is never shown and its permission requests are never
    # answered. The chat gets a short OBSERVE turn for it: the chat-turn pipeline
    # reads the engine's turn as its own (approvals under the chat's mode, text,
    # stats), sending nothing (:meth:`AcpSessionHandle.observe_turn`).

    def set_turn_starter(self, starter: TurnStarter | None) -> None:
        """Install how a chat is sent an observe turn (the dashboard's chat slots)."""
        self._turn_starter = starter

    def _expire_observes(self) -> None:
        cutoff = self._clock() - LAUNCH_TTL_SECS
        for token in [t for t, held in self._observes.items() if held[1] < cutoff]:
            self._observes.pop(token, None)

    def prepare_observe(self, slot_key: str, name: str, status: str, parent_session_id: str) -> str:
        """A single-use observe message for *slot_key*'s next turn.

        *parent_session_id* is the session KAS wakes; the turn reads it only if
        the chat's claimed session is still that one (:func:`observe_from_turn`).
        """
        self._expire_observes()
        while len(self._observes) >= MAX_PENDING_OBSERVES:
            self._observes.pop(next(iter(self._observes)))
        token = secrets.token_hex(16)
        self._observes[token] = (slot_key, self._clock(), parent_session_id)
        # The row the chat shows for this turn. Characters the chat would expand
        # (``$skill``, ``@prompt``) or that end the code span are dropped.
        label = re.sub(r"[`$@\n\r]", "", _redact(name)).strip()[:200] or "workflow"
        verb = status if status in _OBSERVE_VERBS else "ended"
        return f"[Workflow `{label}` {verb}] Kiro Agent's reply follows (observe {token})"

    @staticmethod
    def observe_token(message: str) -> str | None:
        """The token of an observe message, or None for any other message."""
        match = _OBSERVE_RE.match(message or "")
        return match.group(1) if match else None

    def take_observe(self, slot_key: str, token: str) -> str | None:
        """Consume *token* and return the session it watches, only for *slot_key*."""
        self._expire_observes()
        held = self._observes.get(token)
        if held is None or held[0] != slot_key:
            return None
        self._observes.pop(token, None)
        return held[2]

    def _schedule_observe(self, workflow_id: str, params: Mapping[str, Any]) -> None:
        """On a run's end, send its parent chat one observe turn. Never blocks.

        Only for a run whose parent session is a chat this router has seen a turn
        on (:meth:`note_chat_session`): that is the session KAS wakes. Once per
        run. The turn itself starts on the next loop pass, off the reader's frame.
        """
        if self._turn_starter is None or workflow_id in self._observed_runs:
            return
        status = wire.as_str(params.get("status")) or self.mirror.status(workflow_id)
        if status not in wire.TERMINAL_RUN_STATUSES:
            return
        final = wire.as_mapping(params.get("finalState")) or {}
        parent = (
            wire.as_str(params.get("parentSessionId"))
            or wire.as_str(final.get("parentSessionId"))
            or self.mirror.parent_of(workflow_id)
        )
        if not self._queue_observe(workflow_id, parent, status or "", final):
            return
        self._observed_runs[workflow_id] = None
        while len(self._observed_runs) > MAX_OBSERVED_RUNS:
            self._observed_runs.pop(next(iter(self._observed_runs)))

    def _on_watched_notification(self, method: str, params: Mapping[str, Any]) -> None:
        """A step's note to its run's parent: send the chat an observe turn.

        KAS delivers a step's ``send_message`` (and the runtime's own note on a
        step's behalf, such as a repeat pausing at its iteration limit) to the
        run's parent; a ``warning``, an ``error`` or an ``info`` wakes that
        parent when it is idle, with a model turn no client sent -- the same
        unread turn as a run's end. Only notes addressed to the run's parent
        are read here; a parent's note to one of its steps wakes the step.
        """
        if method != wire.NOTIFY_SESSION_NOTIFY:
            return
        workflow_id = wire.as_str(params.get("workflowId"))
        target = wire.as_str(params.get("sessionId"))
        severity = wire.as_str(params.get("severity")) or "info"
        if not workflow_id or not target:
            return
        parent = self.mirror.parent_of(workflow_id)
        if parent is None:
            # A run not mirrored yet (a restart): the note's target is the
            # parent only if it is a chat this router knows the run under.
            ledger_slot = self.slot_of(workflow_id)
            if ledger_slot is None or self._parent_slot.get(target) not in (None, ledger_slot):
                return
            parent = target
        if parent != target:
            return
        # Every note to the parent leaves a row in its chat, whatever its
        # severity and whether or not the chat is busy (gap 10).
        slot_key = self._slot_for_run(workflow_id, parent)
        if slot_key is not None:
            self._mark_note(workflow_id, params, slot_key)
        if self._turn_starter is None or severity not in wire.WAKING_NOTE_SEVERITIES:
            return
        self._queue_observe(workflow_id, parent, _NOTE_VERBS[severity], {})

    def _queue_observe(
        self, workflow_id: str, parent: str | None, verb: str, final: Mapping[str, Any]
    ) -> bool:
        """Queue one observe turn on *workflow_id*'s chat for *parent*'s woken turn.

        Only for a run whose parent session is a chat this router has seen a
        turn on (:meth:`note_chat_session`), or one the ledger names. Returns
        whether a turn was queued. Never blocks.
        """
        starter = self._turn_starter
        if starter is None or not parent:
            return False
        slot_key = self._parent_slot.get(parent)
        if slot_key is None:
            # After a gateway restart the in-memory map is empty until the chat
            # has a turn, but the ledger (on disk) still names the run's chat.
            # The observe turn itself re-checks that the chat's claimed session
            # IS the one KAS woke, so a wrong guess shows a notice, not a read.
            slot_key = self.slot_of(workflow_id)
        if slot_key is None:
            return False
        row = self.mirror.row(workflow_id) or {}
        name = wire.as_str(final.get("workflowName")) or str(row.get("name") or workflow_id)
        message = self.prepare_observe(slot_key, name, verb, parent)
        token = self.observe_token(message)
        try:
            asyncio.get_running_loop().call_soon(
                self._start_observe, starter, slot_key, message, token
            )
        except RuntimeError:  # no loop: nothing could read the turn anyway
            self._observes.pop(token or "", None)
            return False
        return True

    def _start_observe(
        self, starter: TurnStarter, slot_key: str, message: str, token: str | None
    ) -> None:
        try:
            started = bool(starter(slot_key, message))
        except Exception:  # noqa: BLE001 -- a chat that cannot take it loses only the view
            logger.warning("KAS workflow observe turn not started for %s", slot_key, exc_info=True)
            started = False
        if not started:
            self._observes.pop(token or "", None)

    # ── Reconnecting ──

    def _undriven_live(self, slot_key: str | None = None) -> list[str]:
        """Ledger runs not known finished and with no live driving process."""
        out = []
        for wid, entry in self._ledger.items():
            if entry.get("ended") or (slot_key is not None and entry["slot"] != slot_key):
                continue
            if self.mirror.status(wid) in wire.TERMINAL_RUN_STATUSES:
                continue
            if _alive(self._run_runtime.get(wid)) is None:
                out.append(wid)
        return out

    def prepare_reconnect(self, slot_key: str) -> str:
        """A single-use reconnect message for *slot_key*'s next turn."""
        now = self._clock()
        for token in [t for t, (_s, at) in self._reconnects.items() if at < now - LAUNCH_TTL_SECS]:
            self._reconnects.pop(token, None)
        token = secrets.token_hex(16)
        self._reconnects[token] = (slot_key, now)
        return f"Reconnect workflow runs (reconnect {token})"

    def _take_reconnect(self, slot_key: str, token: str) -> bool:
        held = self._reconnects.get(token)
        if held is None or held[0] != slot_key:
            return False
        self._reconnects.pop(token, None)
        return True

    async def reattach(self, slot_key: str, runtime: Any, session_id: str) -> list[str]:
        """Re-attach *slot_key*'s unfinished runs to the chat's live process.

        Each run is ``load``-ed on this process, which is read-only for a run it
        does not own: KAS reconciles a dead owner's run to paused with
        :data:`STALE_RUNNING_PAUSE_REASON` and never steals a live owner's. A
        run paused for that reason is resumed here, which is where KAS hands
        this process ownership; a run paused for any other reason is attached
        and left paused; a run still running belongs to a live process
        elsewhere and is left for a later try. Returns one line per run.
        """
        await self.ensure_ledger()
        lines: list[str] = []
        ref = weakref.ref(runtime)
        for wid in self._undriven_live(slot_key):
            try:
                loaded = await self._call(runtime, wire.METHOD_LOAD, {"workflowId": wid})
            except KasWorkflowError as exc:
                lines.append(f"`{wid}`: could not be loaded ({_redact(str(exc))[:200]})")
                continue
            if self.mirror.apply_load(loaded) is not None:
                self.revision += 1
            for raw in wire.as_list(loaded.get("stepSessions")):
                item = wire.as_mapping(raw)
                step = wire.as_str(item.get("sessionId")) if item else None
                if step:
                    self._step_runtime[step] = ref
            state = wire.as_mapping(loaded.get("state")) or {}
            status = wire.as_str(state.get("status"))
            name = self.mirror.row(wid) or {}
            label = f"**{_redact(str(name.get('name') or wid))}**"
            if status in wire.TERMINAL_RUN_STATUSES:
                self._note_ended(wid)
                lines.append(f"{label} had already finished ({status}).")
            elif status == wire.STATUS_PAUSED and (plan_steps := _kept_plan_waits(state)):
                # KAS keeps a top-level step's open plan approval across an agent
                # restart and asks it again on that step session's next
                # ``session/load``, but only while the run stays paused on it:
                # resuming would move the step on and cancel the plan. So the
                # step session is loaded here (its request then reaches this
                # router) and the run is left paused; Allow resumes it in KAS.
                self._run_runtime[wid] = ref
                reasked = await self._reask_plans(runtime, wid, plan_steps)
                lines.append(
                    f"{label} is waiting for its plan approval again."
                    if reasked
                    else f"{label} is reconnected and stays paused."
                )
            elif status == wire.STATUS_PAUSED and (
                wire.as_str(state.get("pauseReason")) == STALE_RUNNING_PAUSE_REASON
            ):
                await self._call(
                    runtime, wire.METHOD_RESUME, wire.user_control_params(wid, RECONNECT_REASON)
                )
                self._run_runtime[wid] = ref
                lines.append(f"{label} was interrupted by a Kiro Agent restart and is resuming.")
            elif status == wire.STATUS_PAUSED:
                self._run_runtime[wid] = ref
                lines.append(f"{label} is reconnected and stays paused.")
            else:
                lines.append(
                    f"{label} is still held by another Kiro Agent process; "
                    "it will be retried shortly."
                )
        if any(_alive(self._run_runtime.get(w)) is runtime for w in self._ledger):
            self._reconnect_tries.pop(slot_key, None)
        self.revision += 1
        return lines

    def _step_cwd(self, workflow_id: str, runtime: Any) -> str:
        """The directory a step session is loaded in: its run's project, else the process's."""
        return str((self._ledger.get(workflow_id) or {}).get("cwd") or "") or str(
            getattr(runtime, "_work_dir", "") or ""
        )

    async def _reask_plans(self, runtime: Any, workflow_id: str, sessions: list[str]) -> bool:
        """Load each step session holding a kept plan approval, so KAS asks it again.

        The step's runtime is recorded first: the re-asked request arrives right
        after the load answers and must reach this router, not the runtime's
        auto-reject. True when at least one load was accepted.
        """
        cwd = self._step_cwd(workflow_id, runtime)
        if not cwd:
            return False
        ref = weakref.ref(runtime)
        asked = False
        for session_id in sessions:
            self._step_runtime[session_id] = ref
            try:
                await self._call(
                    runtime,
                    wire.METHOD_SESSION_LOAD,
                    {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
                )
            except KasWorkflowError:
                logger.info("plan approval step session did not load for %s", workflow_id)
                continue
            asked = True
        return asked

    def schedule_reattach(self, slot_key: str, provider: Any) -> None:
        """On an ordinary chat turn, re-attach that chat's undriven runs in the background."""
        if not self._ledger_loaded or not self._undriven_live(slot_key):
            return
        binding = session_binding(provider)
        if binding is None:
            return

        async def _go() -> None:
            try:
                await self.reattach(slot_key, *binding)
            except Exception:  # noqa: BLE001 -- best effort; the dashboard retries
                logger.info("background workflow reattach failed for %s", slot_key)

        self._spawn(_go())

    def due_reconnects(self) -> list[str]:
        """Chats whose undriven runs are due an automatic reconnect turn, marked tried."""
        now = self._clock()
        due = []
        for slot_key in sorted({self._ledger[w]["slot"] for w in self._undriven_live()}):
            tries, last = self._reconnect_tries.get(slot_key, (0, 0.0))
            if tries >= RECONNECT_MAX_TRIES or now - last < RECONNECT_INTERVAL_SECS:
                continue
            self._reconnect_tries[slot_key] = (tries + 1, now)
            due.append(slot_key)
        return due

    def reset_reconnect_tries(self, slot_key: str) -> None:
        self._reconnect_tries.pop(slot_key, None)

    # ── Boot recovery ──

    async def _owner_verdict(self, workflow_id: str) -> str:
        """KAS's own word on whether *workflow_id* has a live owner.

        ``_kiro/workflow/load`` on a process that does not hold the run serves
        it from disk, read-only (kiro-agent ``workflow-handlers.ts``: "read
        paths reconcile stale-running state but never register or claim
        ownership"): a run whose owner stopped beating past KAS's window comes
        back ``paused`` with :data:`STALE_RUNNING_PAUSE_REASON`, one a live
        owner drives stays ``running``. Returns ``recover`` (paused: nobody
        drives it), ``live``, ``finished``, ``other:<status>`` or ``unreadable``.
        """
        try:
            loaded = await self._call(
                await self._reader(), wire.METHOD_LOAD, {"workflowId": workflow_id}
            )
        except KasWorkflowError:
            return "unreadable"
        if self.mirror.apply_load(loaded) is not None:
            self.revision += 1
        state = wire.as_mapping(loaded.get("state")) or {}
        status = wire.as_str(state.get("status")) or ""
        if status in wire.TERMINAL_RUN_STATUSES:
            self._note_ended(workflow_id)
            return "finished"
        if status == wire.STATUS_PAUSED:
            return "recover"
        if status == wire.STATUS_RUNNING:
            return "live"
        return f"other:{status or 'unknown'}"

    async def recover_on_boot(
        self,
        chat_workspace: Callable[[str], str | None],
        reconnect: Callable[[str], Awaitable[bool]],
        *,
        max_chats: int = BOOT_RECOVERY_MAX_CHATS,
        concurrency: int = BOOT_RECOVERY_CONCURRENCY,
    ) -> list[tuple[str, str, str]]:
        """Reattach this gateway's own interrupted runs, once, at startup.

        Only runs in Crew's own ledger are considered (a run another client
        started is never in it), only those not known finished and not driven
        by a live process here, and only those KAS itself reports ownerless
        (:meth:`_owner_verdict`): a run another live owner holds is left alone.
        KAS's ``_kiro/workflow/resumeAll`` is deliberately NOT used: it resumes
        every interrupted run in the process's scope, IDE and other clients'
        runs included, which would take over work that is not Crew's.

        Runs are grouped by chat and each chat gets ONE *reconnect* (the
        reconnect turn a chat's own poll already sends; it dispatches no model
        prompt), at most *concurrency* at a time and *max_chats* per boot, most
        recent first. *chat_workspace* names a chat's workspace, or None when
        the chat no longer exists; a chat that is gone, or whose workspace is
        not the one its run was started in, is skipped, never recreated.

        Returns ``(workflowId, slot, outcome)`` per run considered; each is
        also logged.
        """
        await self.ensure_ledger()
        outcomes: list[tuple[str, str, str]] = []

        def note(wid: str, slot_key: str, outcome: str) -> None:
            outcomes.append((wid, slot_key, outcome))
            logger.info("KAS workflow boot recovery: run %s (chat %s) %s", wid, slot_key, outcome)

        by_chat: dict[str, list[str]] = {}
        for wid in self._undriven_live():
            slot_key = self._ledger[wid]["slot"]
            verdict = await self._owner_verdict(wid)
            if verdict == "recover":
                by_chat.setdefault(slot_key, []).append(wid)
            elif verdict == "live":
                note(wid, slot_key, "skipped: a live owner still holds it")
            elif verdict == "finished":
                note(wid, slot_key, "skipped: already finished")
            elif verdict == "unreadable":
                note(wid, slot_key, "skipped: Kiro Agent could not read it")
            else:
                note(wid, slot_key, f"skipped: not paused ({verdict.split(':', 1)[1]})")

        def newest(slot_key: str) -> float:
            return max(self._ledger[w]["createdAt"] for w in by_chat[slot_key])

        chats = sorted(by_chat, key=newest, reverse=True)
        for slot_key in chats[max_chats:]:
            for wid in by_chat[slot_key]:
                note(wid, slot_key, f"skipped: over the {max_chats}-chat boot cap")
        gate = asyncio.Semaphore(max(1, concurrency))

        async def one(slot_key: str) -> None:
            runs = by_chat[slot_key]
            workspace = chat_workspace(slot_key)
            if workspace is None:
                for wid in runs:
                    note(wid, slot_key, "skipped: its chat no longer exists")
                return
            mine = [w for w in runs if self._ledger[w].get("workspace") in ("", workspace)]
            for wid in runs:
                if wid not in mine:
                    note(wid, slot_key, "skipped: started in another workspace")
            if not mine:
                return
            async with gate:
                # Counts as this chat's first automatic try, so the dashboard
                # poll does not send a second reconnect on top of this one.
                self._reconnect_tries[slot_key] = (1, self._clock())
                try:
                    sent = await reconnect(slot_key)
                except Exception:  # noqa: BLE001 -- one chat must not stop the rest
                    logger.info("KAS workflow boot reconnect failed for %s", slot_key)
                    sent = False
            outcome = "reattached" if sent else "skipped: its chat could not take the turn"
            for wid in mine:
                note(wid, slot_key, outcome)

        await asyncio.gather(*(one(s) for s in chats[:max_chats]))
        return outcomes

    # ── Requests ──

    async def _call(self, runtime: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await runtime.extension_request(method, params, timeout=self._timeout)
        except Exception as exc:
            short = method.rsplit("/", 1)[-1]
            raise KasWorkflowError(f"Kiro Agent refused {short}: {exc}") from exc

    async def _reader(self) -> Any:
        """A live KAS process for parentless reads, spawning one if none is up."""
        for runtime in list(self._runtimes):
            if runtime.is_alive():
                return runtime
        async with self._query_lock:
            runtime = self._query_runtime
            if runtime is not None and runtime.is_alive():
                return runtime
            try:
                runtime = await self._query_factory()
            except Exception as exc:
                raise KasWorkflowError(f"Kiro Agent could not start: {exc}") from exc
            self._query_runtime = runtime
            return runtime

    def _row(self, workflow_id: str) -> dict[str, Any] | None:
        plan_waiting = any(
            a.is_plan and a.workflow_id == workflow_id for a in self._pending.values()
        )
        row = self.mirror.row(workflow_id, plan_waiting=plan_waiting)
        if row is None:
            return None
        driven = _alive(self._run_runtime.get(workflow_id)) is not None
        server = (
            _alive(self._run_runtime.get(workflow_id))
            if driven
            else (
                self._finished_runtime(workflow_id)
                if row.get("status") in wire.TERMINAL_RUN_STATUSES
                else None
            )
        )
        reachable = server is not None
        if not reachable:
            # KAS would accept these, but only on the process that holds the
            # run: until the run's chat reconnects nothing here can send them.
            # A finished run may still be deleted from any process.
            row["actions"] = [a for a in row.get("actions", []) if a == wire.RUN_ACTION_DELETE]
            _strip_node_actions(row.get("nodes") or [])
        else:
            # Only what the serving Kiro Agent advertised (gap 20).
            methods = wire.extension_methods(getattr(server, "_agent_capabilities", None))
            if methods is not None:
                row["actions"] = wire.gate_actions(list(row.get("actions", [])), methods)
                _gate_node_actions(row.get("nodes") or [], methods)
        return {**row, "slotKey": self.slot_of(workflow_id), "driven": driven}

    def run_row(self, workflow_id: str) -> dict[str, Any] | None:
        """One run's row as every surface reads it (actions gated on its driver)."""
        return self._row(workflow_id)

    def rows(self) -> list[dict[str, Any]]:
        out = []
        for row in self.mirror.rows():
            wid = row.get("workflowId")
            if isinstance(wid, str):
                out.append(self._row(wid) or row)
        return out

    async def recipes(self, cwd: str) -> list[dict[str, Any]]:
        runtime = await self._reader()
        project = _project(cwd)
        response = await self._call(
            runtime, wire.METHOD_LIST_RECIPES, {"workspacePaths": [project]}
        )
        return [r.to_payload(project) for r in wire.parse_recipes(response)]

    async def recipe_detail(self, cwd: str, source: str) -> dict[str, Any]:
        """One recipe with its steps' prompt text, for the recipe detail view (gap 27).

        *source* must be a recipe KAS lists for *cwd* right now, so no caller
        can name a file to read. Only a ``.workflow.json`` file is read (the
        definition format Crew can parse without a YAML dependency), capped in
        size; a bundled or YAML recipe answers ``definitionAvailable: false``.
        Prompts are redacted and capped.
        """
        runtime = await self._reader()
        project = _project(cwd)
        response = await self._call(
            runtime, wire.METHOD_LIST_RECIPES, {"workspacePaths": [project]}
        )
        recipe = next(
            (r for r in wire.parse_recipes(response) if r.source and r.source == source), None
        )
        if recipe is None:
            raise KasWorkflowError("That recipe is not listed for this project.")
        steps: list[dict[str, Any]] = []
        available = False
        path = recipe.source or ""
        if not recipe.read_only and path.endswith(".workflow.json") and os.path.isabs(path):
            definition = await asyncio.to_thread(_read_definition, path)
            if definition is not None:
                available = True
                steps = _definition_steps(definition)
        return {
            "recipe": recipe.to_payload(project),
            "steps": steps,
            "definitionAvailable": available,
        }

    async def runs(self, cwd: str) -> list[dict[str, Any]]:
        """Every run in *cwd*'s bucket, from disk. Nothing is loaded or resumed.

        The answer is the project's whole run list, so a run an earlier list of
        this project returned and this one omits was deleted elsewhere (the IDE,
        another process) and is dropped here too (gap 19), unless a process of
        this gateway still drives it.
        """
        runtime = await self._reader()
        project = _project(cwd)
        response = await self._call(runtime, wire.METHOD_LIST, {"workspacePaths": [project]})
        summaries = wire.parse_run_list(response)
        changed, forgotten = self.mirror.apply_scoped_list(
            project,
            summaries,
            keep=lambda wid: _alive(self._run_runtime.get(wid)) is not None,
        )
        if changed or forgotten:
            self.revision += 1
        await self.ensure_ledger()
        for wid in forgotten:
            self._forget_deleted(wid)
        for summary in summaries:
            if summary.status in wire.TERMINAL_RUN_STATUSES:
                self._note_ended(summary.workflow_id)
        ids = {s.workflow_id for s in summaries}
        return [row for row in self.rows() if row.get("workflowId") in ids]

    def _forget_deleted(self, workflow_id: str) -> None:
        """A run deleted outside this gateway: drop what is held for it here."""
        self._run_runtime.pop(workflow_id, None)
        self._inspected.discard(workflow_id)
        for key in [k for k, a in self._pending.items() if a.workflow_id == workflow_id]:
            self._pending.pop(key, None)
            self._decided_under.pop(key, None)
            self._retire(key)
        for key in [k for k, q in self._questions.items() if q.workflow_id == workflow_id]:
            self._questions.pop(key, None)
            self._retire(key)
        # Kept in the ledger (it names the chat), but never chased by a reconnect.
        self._note_ended(workflow_id)

    async def step_transcript(
        self, workflow_id: str, node_id: str, path: str | None = None
    ) -> dict[str, Any]:
        """One step's own conversation (the IDE's step chat), read-only.

        *path* (root-relative runtime path, e.g. ``loop/iter-1/code``) picks one
        instance of a node a loop runs more than once; without it the latest
        instance of *node_id* is read.

        Only a step of a run this router knows (mirrored, or in the ledger) is
        served, and its session id comes from KAS's own run state, never from the
        caller. A run whose step sessions are not mirrored yet (after a gateway
        restart) is read once with ``inspect`` first. Raises
        :class:`KasWorkflowError` when the step has not started.
        """
        from kiro_crew.kas_workflows.transcript import StepTranscriptError, read_step_transcript

        await self.ensure_ledger()
        if self.mirror.status(workflow_id) is None and workflow_id not in self._ledger:
            raise KasWorkflowError("That workflow run is not known here.")

        def lookup() -> str | None:
            # A path names one instance (a loop body's iteration); a bare id
            # names the latest instance, which is all a path-less caller means.
            if path:
                return self.mirror.step_session_at(workflow_id, path)
            return self.mirror.step_session(workflow_id, node_id)

        session_id = lookup()
        reader = await self._reader() if session_id is None else None
        if session_id is None and self._serves(reader, wire.METHOD_INSPECT):
            try:
                response = await self._call(
                    reader, wire.METHOD_INSPECT, {"workflowId": workflow_id}
                )
            except KasWorkflowError:
                response = None
            if response is not None and self.mirror.apply_load(response) is not None:
                self.revision += 1
            session_id = lookup()
        if session_id is None:
            raise StepNotStarted("This step has not started.")
        try:
            return await asyncio.to_thread(read_step_transcript, workflow_id, node_id, session_id)
        except StepTranscriptError as exc:
            raise StepNotStarted(str(exc)) from exc

    async def chat_runs(self, slot_key: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Every run *slot_key*'s chat parents, reloaded read-only when not mirrored.

        The mirror is memory: after a gateway restart it holds only runs that
        have spoken since. The ledger survives the restart, so a chat's own
        runs are read back from it with ``inspect`` (KAS's read-only snapshot,
        which falls back to disk for a run no process holds) -- the newest
        *limit* of them, once each. Never ``load``: that takes a run over.
        """
        await self.ensure_ledger()
        mine = sorted(
            (w for w, e in self._ledger.items() if e["slot"] == slot_key),
            key=lambda w: self._ledger[w]["createdAt"],
            reverse=True,
        )[:limit]
        missing = [w for w in mine if w not in self._inspected and not self.mirror.has_plan(w)]
        reader = await self._reader() if missing else None
        if missing and self._serves(reader, wire.METHOD_INSPECT):
            for workflow_id in missing:
                self._inspected.add(workflow_id)
                try:
                    response = await self._call(
                        reader, wire.METHOD_INSPECT, {"workflowId": workflow_id}
                    )
                except KasWorkflowError:
                    logger.debug("chat run %s could not be inspected", workflow_id)
                    continue
                if self.mirror.apply_load(response) is not None:
                    self.revision += 1
                if self.mirror.status(workflow_id) in wire.TERMINAL_RUN_STATUSES:
                    self._note_ended(workflow_id)
        return [row for row in (self._row(w) for w in mine) if row is not None]

    def run(self, workflow_id: str) -> dict[str, Any]:
        row = self._row(workflow_id)
        if row is None:
            raise KasWorkflowError("No run with that id is known; list its project first.")
        return {
            **row,
            "approvals": self.approvals(workflow_id),
            "questions": self.questions(workflow_id),
        }

    async def control(self, workflow_id: str, action: str, reason: str | None = None) -> None:
        method = CONTROL_METHODS.get(action)
        if method is None:
            raise KasWorkflowError(f"Unknown action {action!r}.")
        try:
            params = wire.user_control_params(workflow_id, reason)
        except ValueError as exc:
            raise KasWorkflowError(str(exc)) from exc
        runtime = _alive(self._run_runtime.get(workflow_id))
        if runtime is None:
            status = self.mirror.status(workflow_id)
            if action != "delete" or status not in wire.TERMINAL_RUN_STATUSES:
                raise KasWorkflowError(
                    "This run's chat is not connected to Kiro Agent right now. "
                    "Open the run's chat, then try again."
                )
            # A finished run has no driver to race; any process may delete it.
            runtime = await self._reader()
        self._require(runtime, method, _VERBS.get(action, action))
        await self._call(runtime, method, params)
        if action == "delete":
            self.mirror.forget(workflow_id)
            self._run_runtime.pop(workflow_id, None)
            if self._ledger.pop(workflow_id, None) is not None:
                await self._save_ledger()
            for key in [k for k, a in self._pending.items() if a.workflow_id == workflow_id]:
                self._pending.pop(key, None)
                self._retire(key)
            for key in [k for k, q in self._questions.items() if q.workflow_id == workflow_id]:
                self._questions.pop(key, None)
                self._retire(key)
        self.revision += 1

    # ── Step and recovery controls ──
    #
    # Each maps one dashboard action onto the KAS verb that does it, on the
    # process that drives the run. KAS is the authority on every state rule: a
    # refusal comes back as KasWorkflowError (409) with KAS's own reason.

    _NOT_CONNECTED = (
        "This run's chat is not connected to Kiro Agent right now. "
        "Open the run's chat, then try again."
    )

    def _driver(self, workflow_id: str) -> Any:
        runtime = _alive(self._run_runtime.get(workflow_id))
        if runtime is None:
            raise KasWorkflowError(self._NOT_CONNECTED)
        return runtime

    def _finished_runtime(self, workflow_id: str) -> Any:
        """The process that may re-drive a finished run, or None.

        Its last driver while that process lives; otherwise the process of the
        run's chat, but only while the chat's session is still the run's
        parent, since a step's permissions come from that session.
        """
        live = _alive(self._run_runtime.get(workflow_id))
        if live is not None:
            return live
        slot_key = self.slot_of(workflow_id)
        held = self._slot_binding.get(slot_key) if slot_key else None
        parent = self.mirror.parent_of(workflow_id)
        if held is None or parent is None or held[1] != parent:
            return None
        return _alive(held[0])

    async def _redriven(self, workflow_id: str, runtime: Any) -> None:
        """A finished run is running again on *runtime*: it is live in every record."""
        self._run_runtime[workflow_id] = weakref.ref(runtime)
        self._observed_runs.pop(workflow_id, None)
        self._inspected.discard(workflow_id)
        entry = self._ledger.get(workflow_id)
        if entry is not None and entry.get("ended"):
            entry["ended"] = False
            await self._save_ledger()
        self.revision += 1

    async def retry(
        self, workflow_id: str, node_id: str | None = None, path: str | None = None
    ) -> list[str]:
        """Retry every failed/aborted step of a finished run, or one (KAS ``retry``).

        KAS resets the step(s) and their containers and drives the run again.
        It names a step by id alone and resolves the FIRST instance, so a step
        inside a loop iteration is retried only with all failed steps.
        """
        status = self.mirror.status(workflow_id)
        if status not in (wire.STATUS_FAILED, wire.STATUS_ABORTED):
            raise KasWorkflowError("Only a failed or stopped run can be retried.")
        if path:
            if any(
                seg.startswith(wire.ITERATION_SEGMENT_PREFIX) for seg in path.strip("/").split("/")
            ):
                raise KasWorkflowError(
                    "A step inside a loop can only be retried together with every failed step."
                )
            known = self.mirror.node_ids_at(workflow_id, path)
            if known is not None and node_id is not None and known != node_id:
                raise KasWorkflowError("That path is not this step.")
            node_id = node_id or known
        if node_id is not None and self.mirror.instance_status(workflow_id, node_id, path) not in (
            wire.STATUS_FAILED,
            wire.STATUS_ABORTED,
        ):
            raise KasWorkflowError("Only a failed or stopped step can be retried.")
        runtime = self._finished_runtime(workflow_id)
        if runtime is None:
            raise KasWorkflowError(self._NOT_CONNECTED)
        try:
            params = wire.retry_params(workflow_id, node_id)
        except ValueError as exc:
            raise KasWorkflowError(str(exc)) from exc
        self._require(runtime, wire.METHOD_RETRY, "retry")
        response = await self._call(runtime, wire.METHOD_RETRY, params)
        await self._redriven(workflow_id, runtime)
        return [n for n in wire.as_list(response.get("retriedNodeIds")) if isinstance(n, str)]

    async def restart(self, workflow_id: str) -> None:
        """Run a finished run again from scratch (KAS ``invoke`` on a terminal run)."""
        if self.mirror.status(workflow_id) not in wire.TERMINAL_RUN_STATUSES:
            raise KasWorkflowError("Only a finished run can be run again.")
        runtime = self._finished_runtime(workflow_id)
        if runtime is None:
            raise KasWorkflowError(self._NOT_CONNECTED)
        self._require(runtime, wire.METHOD_INVOKE, "running a workflow again")
        await self._call(runtime, wire.METHOD_INVOKE, {"workflowId": workflow_id})
        await self._redriven(workflow_id, runtime)

    async def grant(
        self, workflow_id: str, node_id: str, iterations: int, reason: str | None = None
    ) -> None:
        """Grant an exhausted loop more passes and drive it (KAS ``resume`` + ``extendRepeat``).

        Done iterations are kept; KAS re-checks the stop condition first and
        refuses a budget past its cap.
        """
        try:
            params = wire.extend_repeat_params(workflow_id, node_id, iterations, reason)
        except ValueError as exc:
            raise KasWorkflowError(str(exc)) from exc
        driver = self._driver(workflow_id)
        self._require(driver, wire.METHOD_RESUME, "granting more passes")
        await self._call(driver, wire.METHOD_RESUME, params)
        self.revision += 1

    async def end_step(
        self,
        workflow_id: str,
        node_id: str,
        status: str,
        reason: str | None = None,
        path: str | None = None,
    ) -> str:
        """End a paused step with *status*, or end an exhausted loop and continue.

        KAS ``update`` ``update_status`` with ``nodeId``: a paused step takes
        ``completed`` or ``failed``; the repeat the run is parked on at its limit
        takes ``completed`` only. KAS answers ``updated: false`` with its reason
        for anything else, which is raised here.
        """
        if path:
            known = self.mirror.node_ids_at(workflow_id, path)
            if known is not None and known != node_id:
                raise KasWorkflowError("That path is not this step.")
        try:
            params = wire.end_step_params(workflow_id, node_id, status, reason)
        except ValueError as exc:
            raise KasWorkflowError(str(exc)) from exc
        driver = self._driver(workflow_id)
        self._require(driver, wire.METHOD_UPDATE, "ending a step")
        response = await self._call(driver, wire.METHOD_UPDATE, params)
        message = wire.as_str(response.get("message")) or ""
        if response.get("updated") is not True:
            raise KasWorkflowError(_redact(message) or "Kiro Agent did not end that step.")
        self.revision += 1
        return _redact(message)

    async def cancel_step(self, workflow_id: str, node_id: str, path: str | None = None) -> None:
        """Stop one running step (``session/cancel`` on its own session).

        KAS parks that step ``paused`` ("stopped by user"), keeps its session,
        and lets parallel siblings run on; the run settles paused once they
        finish, and Resume or a reply re-drives the step. Any ask still open on
        the step's session is answered ``cancelled``, as ACP requires.
        """
        if path:
            session_id = self.mirror.step_session_at(workflow_id, path)
        else:
            session_id = self.mirror.step_session(workflow_id, node_id)
        if session_id is None:
            raise StepNotStarted("This step has not started.")
        if self.mirror.instance_status(workflow_id, node_id, path) != wire.STATUS_RUNNING:
            raise KasWorkflowError("This step is not running.")
        runtime = self._driver(workflow_id)
        try:
            await runtime.send_notification(wire.METHOD_SESSION_CANCEL, {"sessionId": session_id})
        except Exception as exc:  # noqa: BLE001 -- a dead pipe is the run's state to report
            raise KasWorkflowError(f"Kiro Agent did not take the stop: {exc}") from exc
        await self._withdraw_session_asks(session_id)
        self.revision += 1

    async def reply_to_step(
        self, workflow_id: str, node_id: str, path: str | None, text: str
    ) -> None:
        """Send the person's *text* to a step parked for their next message.

        KAS has no special method for this: a ``session/prompt`` on the step's
        own session is recognised as the paused step's continuation, answered
        ``end_turn`` at once, and the step's turn runs under the workflow. Only
        a step KAS lists as waiting for a reply is sent one (a running step
        would take the prompt as an unrelated chat turn), and only on the
        process driving the run. A step session that process no longer holds
        (it restarted) is loaded first, as KAS's guide prescribes.
        """
        body = (text or "").strip()
        if not body:
            raise KasWorkflowError("Write a reply first.")
        if len(body) > wire.MAX_STEP_REPLY_CHARS:
            raise KasWorkflowError("That reply is too long.")
        if path:
            session_id = self.mirror.step_session_at(workflow_id, path)
        else:
            session_id = self.mirror.step_session(workflow_id, node_id)
        if session_id is None:
            raise StepNotStarted("This step has not started.")
        if not self.mirror.step_awaits_reply(workflow_id, session_id):
            raise KasWorkflowError("This step is not waiting for a reply.")
        runtime = _alive(self._run_runtime.get(workflow_id))
        if runtime is None:
            raise KasWorkflowError(
                "This run's chat is not connected to Kiro Agent right now. "
                "Open the run's chat, then try again."
            )
        # One reply per parked step at a time. The waiting check above reads the
        # mirror, which moves only when KAS reports the step running again, so a
        # second reply (a double send, another tab) would pass it too and reach
        # KAS as an unrelated chat turn on the step's session.
        if session_id in self._replies_in_flight:
            raise KasWorkflowError("A reply to this step is already being sent.")
        self._replies_in_flight.add(session_id)
        try:
            await self._send_step_reply(workflow_id, session_id, runtime, body)
        finally:
            self._replies_in_flight.discard(session_id)
        self.revision += 1

    async def _send_step_reply(
        self, workflow_id: str, session_id: str, runtime: Any, body: str
    ) -> None:
        prompt = {"sessionId": session_id, "prompt": [{"type": "text", "text": body}]}
        try:
            await self._call(runtime, wire.METHOD_SESSION_PROMPT, prompt)
        except KasWorkflowError:
            cwd = self._step_cwd(workflow_id, runtime)
            if not cwd:
                raise
            # A load replays the step's open requests (a plan approval is asked
            # again); they must reach this router, not the runtime's auto-reject.
            self._step_runtime[session_id] = weakref.ref(runtime)
            await self._call(
                runtime,
                wire.METHOD_SESSION_LOAD,
                {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
            )
            await self._call(runtime, wire.METHOD_SESSION_PROMPT, prompt)

    async def close(self) -> None:
        """Stop the query process; chat processes belong to their sessions."""
        for task in list(self._tasks):
            task.cancel()
        async with self._query_lock:
            runtime, self._query_runtime = self._query_runtime, None
        if runtime is not None:
            try:
                await runtime.kill(expected=True, reason="kas workflow query runtime shutdown")
            except Exception:  # noqa: BLE001
                logger.debug("kas workflow query runtime kill failed", exc_info=True)
        self._pending.clear()
        self._questions.clear()
        self._launches.clear()
        self._observes.clear()


def _gate_node_actions(nodes: list[Any], methods: frozenset[str]) -> None:
    """Drop node actions the serving Kiro Agent did not advertise, in place."""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node["actions"] = wire.gate_actions(list(node.get("actions") or []), methods, node=True)
        _gate_node_actions(node.get("children") or [], methods)
        for iteration in node.get("iterations") or []:
            if isinstance(iteration, dict):
                _gate_node_actions(iteration.get("children") or [], methods)


def _node_label(nodes: list[Any], node_id: str) -> str | None:
    """The display label of *node_id* in a row's ``nodes`` tree, if present."""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("id") == node_id:
            return str(node.get("label") or node_id)
        found = _node_label(node.get("children") or [], node_id)
        if found:
            return found
    return None


def _parse_iso(value: Any) -> float | None:
    from datetime import datetime

    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _duration_secs(started: Any, ended: Any) -> int | None:
    """Whole seconds between two ISO stamps, or None when either is unknown."""
    start, end = _parse_iso(started), _parse_iso(ended)
    if start is None or end is None or end < start:
        return None
    return int(end - start)


def _now_iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


#: Largest recipe definition read for the detail view, and the per-prompt cap.
MAX_DEFINITION_BYTES = 512 * 1024
MAX_PROMPT_CHARS = 4000


def _read_definition(path: str) -> Mapping[str, Any] | None:
    """A ``.workflow.json`` file's object, or None when unreadable, too large or not JSON."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_DEFINITION_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_DEFINITION_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _definition_steps(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every step of a recipe definition with its agent and prompt text, in plan order."""
    out: list[dict[str, Any]] = []

    def walk(nodes: Any, depth: int) -> None:
        if depth > wire.MAX_PLAN_DEPTH or len(out) >= MAX_PATHS_PER_DEFINITION:
            return
        for raw in wire.as_list(nodes):
            node = wire.as_mapping(raw)
            if node is None:
                continue
            node_id = wire.as_str(node.get("id")) or wire.as_str(node.get("nodeId"))
            if wire.as_str(node.get("type")) == wire.NODE_TYPE_STEP and node_id:
                prompt = wire.as_str(node.get("prompt"))
                out.append(
                    {
                        "nodeId": node_id,
                        "agent": wire.as_str(node.get("agent")),
                        "prompt": (
                            _redact_plan(prompt[: MAX_PROMPT_CHARS * 2])[:MAX_PROMPT_CHARS]
                            if prompt
                            else None
                        ),
                    }
                )
            for key in ("steps", "children", "branches"):
                walk(node.get(key), depth + 1)

    walk(definition.get("steps"), 0)
    return out


#: Steps listed from one recipe definition at most.
MAX_PATHS_PER_DEFINITION = 500


def _strip_node_actions(nodes: list[Any]) -> None:
    """Empty every node's ``actions`` (and its iterations'), in place."""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node["actions"] = []
        _strip_node_actions(node.get("children") or [])
        for iteration in node.get("iterations") or []:
            if isinstance(iteration, dict):
                _strip_node_actions(iteration.get("children") or [])


def _kept_plan_waits(state: Mapping[str, Any]) -> list[str]:
    """Step sessions of a loaded run that hold an open request across a restart.

    On load KAS drops every request wait a dead process left behind except the
    plan approval it keeps (``pending-handoff.json``) for a top-level step, so a
    ``permission`` wait with a ``toolCallId`` on a just-loaded run is that plan.
    """
    out: list[str] = []
    for wait in wire.parse_attention(state.get("attention")):
        if wait.kind == wire.ATTENTION_PERMISSION and wait.tool_call_id and wait.session_id:
            if wait.session_id not in out:
                out.append(wait.session_id)
    return out


def _project(cwd: str) -> str:
    path = os.path.realpath(cwd)
    if not os.path.isdir(path):
        raise KasWorkflowError("That project directory does not exist.")
    return path


def session_binding(provider: Any) -> tuple[Any, str] | None:
    """The (runtime, ACP session id) a claimed chat provider runs on, if KAS.

    ``provider`` is what the chat turn claimed: an ``AcpProvider`` whose
    ``client`` is the :class:`~kiro_crew.acp.session_provider.AcpSessionProvider`
    for its shared process. Anything else -- another harness, a provider not
    started yet -- answers None.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_NATIVE_WORKFLOWS

    inner = getattr(provider, "client", None)
    runtime = getattr(inner, "runtime", None)
    session_id = getattr(inner, "session_id", None)
    if runtime is None or not isinstance(session_id, str) or not session_id:
        return None
    if getattr(runtime, "acp_backend", "") not in ACP_BACKENDS_NATIVE_WORKFLOWS:
        return None
    return runtime, session_id


async def launch_from_turn(slot_key: str, message: str, provider: Any) -> str | None:
    """Start the launch *message* names on the chat's own session.

    Called by the chat turn once it has claimed the slot's session. Returns the
    reply the chat shows in place of a model turn, or None when *message* is not
    a launch message (the turn then proceeds normally).
    """
    router = get_router()
    reconnect = router.reconnect_token(message)
    if reconnect is not None:
        return await _reconnect_from_turn(router, slot_key, reconnect, provider)
    token = router.launch_token(message)
    if token is None:
        return None
    launch = router.take_launch(slot_key, token)
    if launch is None:
        return "That workflow launch has expired or belongs to another chat; nothing was started."
    binding = session_binding(provider)
    if binding is None:
        return (
            "This chat is not running on Kiro Agent, so it cannot start a Kiro Agent "
            "workflow. Switch the agent to Kiro Agent and launch it again."
        )
    runtime, session_id = binding
    try:
        row = await router.start_in_session(runtime, session_id, launch)
    except KasWorkflowError as exc:
        return f"The workflow could not start: {_redact(str(exc))}"
    name = row.get("name") or launch.source
    return (
        f"Started workflow **{_redact(str(name))}** (run `{row.get('workflowId')}`). "
        "Its steps run in Kiro Agent; step approvals appear in the run view. "
        "Kiro Agent posts a summary here when the run ends."
    )


def is_observe_turn(message: str) -> bool:
    """Whether *message* has the shape of an observe turn (its token unchecked)."""
    return KasWorkflowRouter.observe_token(message) is not None


def observe_from_turn(slot_key: str, message: str, provider: Any, *, is_new: bool) -> str | None:
    """Authorize the observe turn *message* names, on the chat's claimed session.

    Called by the chat turn once it has claimed the slot's session. Returns None
    when the turn should go on to read the engine's turn
    (:meth:`AcpSessionHandle.observe_turn`), or the reply the chat shows in its
    place: the token is spent, foreign or expired; the chat is not on Kiro Agent;
    or the claimed session is not the one KAS woke (it was started afresh for
    this turn, or the chat has moved to another session since the run began),
    so nothing it could read is that turn. The token is consumed either way.
    """
    router = get_router()
    token = router.observe_token(message)
    if token is None:
        return "That is not a workflow summary request; nothing was read."
    watched = router.take_observe(slot_key, token)
    if watched is None:
        return (
            "That workflow summary request has expired or belongs to another chat; "
            "nothing was read."
        )
    binding = session_binding(provider)
    if binding is None:
        return "This chat is not running on Kiro Agent, so there is no Kiro Agent reply to show."
    if is_new or binding[1] != watched:
        return (
            "Kiro Agent replied to the run's end on a session this chat is no longer "
            "connected to, so its reply cannot be shown here."
        )
    return None


async def _reconnect_from_turn(
    router: KasWorkflowRouter, slot_key: str, token: str, provider: Any
) -> str:
    if not router._take_reconnect(slot_key, token):
        return "That reconnect request has expired or belongs to another chat; nothing was done."
    binding = session_binding(provider)
    if binding is None:
        return "This chat is not running on Kiro Agent, so its workflow runs cannot reconnect."
    try:
        lines = await router.reattach(slot_key, *binding)
    except KasWorkflowError as exc:
        return f"Reconnecting failed: {_redact(str(exc))}"
    if not lines:
        return "No unfinished workflow runs needed reconnecting."
    return "\n".join(f"- {line}" for line in lines)


async def _spawn_query_runtime() -> KasRuntime:
    """A session-less KAS process for catalog and history reads."""
    from kiro_crew.agent_sdk.kas_workflows_driver import spawn_query_runtime
    from kiro_crew.config import KiroCrewConfig

    config = await asyncio.to_thread(KiroCrewConfig.load)
    runtime: KasRuntime = await spawn_query_runtime(getattr(config.agent, "sandbox", "auto"))
    return runtime


_ROUTER: KasWorkflowRouter | None = None
#: The dashboard's observe-turn starter, applied to the router whenever it exists.
_TURN_STARTER: TurnStarter | None = None
#: The dashboard's bell-note and marker-row hooks, likewise.
_HOOKS: DashboardHooks | None = None


def install_dashboard_hooks(hooks: DashboardHooks | None) -> None:
    """Install the dashboard hooks without creating the router (see :func:`dashboard_hooks`)."""
    global _HOOKS
    _HOOKS = hooks
    if _ROUTER is not None:
        _ROUTER.set_dashboard_hooks(hooks)


def dashboard_hooks(state: Any) -> DashboardHooks:
    """The hooks a dashboard *state* gives the router: bell notes and marker rows.

    - A held step ask becomes one ``system.approval`` note (critical, never
      muted) that opens its chat; once the ask is gone the note is marked read.
    - A run's outcome and its steps' notes become one ``notice`` row each in
      the run's chat, when that chat is open.
    """
    from urllib.parse import quote

    def notify(key: str, slot_key: str, title: str, body: str, meta: Mapping[str, Any]) -> None:
        state.notify(
            "approval",
            title,
            body,
            meta={**dict(meta), "slot": slot_key, "kas_ask": key},
            url=f"/chat?slot={quote(slot_key, safe='')}",
            channel="system.approval",
            publisher_session=slot_key,
        )

    def retire(key: str) -> None:
        stamps = [
            note.get("ts")
            for note in list(getattr(state, "_notification_log", []) or [])
            if note.get("kas_ask") == key and not note.get("acked")
        ]
        for ts in stamps:
            if isinstance(ts, str) and ts:
                try:
                    asyncio.get_running_loop().create_task(state.ack_notification(ts))
                except RuntimeError:  # no loop: nothing is watching the bell either
                    return

    def append_marker(slot_key: str, content: str, meta: Mapping[str, Any]) -> bool:
        slot = state.get_slot(slot_key)
        if slot is None:
            return False
        from kiro_crew.dashboard.state import append_and_surface

        append_and_surface(state, slot, "notice", content, "msg msg-info", meta=dict(meta))
        return True

    return DashboardHooks(notify=notify, retire=retire, append_marker=append_marker)


def install_turn_starter(starter: TurnStarter | None) -> None:
    """Install how chats are sent observe turns, without creating the router.

    The dashboard calls this at startup; the router picks it up when it is first
    created (a gateway that never uses KAS workflows never creates one).
    """
    global _TURN_STARTER
    _TURN_STARTER = starter
    if _ROUTER is not None:
        _ROUTER.set_turn_starter(starter)


def get_router() -> KasWorkflowRouter:
    """The gateway's one router, created on first use."""
    global _ROUTER
    if _ROUTER is None:
        from kiro_crew.config.paths import config_dir

        _ROUTER = KasWorkflowRouter(
            _spawn_query_runtime, ledger_path=config_dir() / "kas_workflows" / "runs.json"
        )
        _ROUTER.set_turn_starter(_TURN_STARTER)
        _ROUTER.set_dashboard_hooks(_HOOKS)
    return _ROUTER


async def close_router() -> None:
    global _ROUTER
    router, _ROUTER = _ROUTER, None
    if router is not None:
        await router.close()


__all__ = [
    "CONTROL_METHODS",
    "DashboardHooks",
    "KasWorkflowError",
    "KasWorkflowRouter",
    "PendingLaunch",
    "close_router",
    "dashboard_hooks",
    "get_router",
    "install_dashboard_hooks",
    "install_turn_starter",
    "is_observe_turn",
    "launch_from_turn",
    "observe_from_turn",
    "STALE_RUNNING_PAUSE_REASON",
    "session_binding",
]
