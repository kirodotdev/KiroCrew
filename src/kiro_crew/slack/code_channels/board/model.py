"""The work board's data: what one worker row and the whole board PUBLISH.

Everything here is pure. :mod:`.source` fills it from the channel conductor's own
ledger and its own workers' live sessions; :mod:`.render` turns it into Slack
blocks; the workspace service uses the same snapshot for the context bar and the
combined diff.

THE PUBLISHED SET IS CLOSED. A code channel is a room other people may be in, and
the work ledger is kept out of channel audiences everywhere else
(``handlers/work_ledger.contained_channel_caller``). The conductor's dispatch
exemption admits exactly these fields into the channel, and nothing else may be
added without amending that rule:

* the conductor's ``goal``, first line, at most :data:`GOAL_CHARS` characters,
  through the same passes as a summary (the board header);
* the item's ``title``;
* the DERIVED state (:data:`STATES`), never the raw ``status`` / ``reason`` words;
* the first line of the worker's last report ``summary``, at most
  :data:`SUMMARY_CHARS` characters, through the outbound redaction and with
  channel-wide and user-group pings stripped;
* ``artifacts["branch"]``;
* the pull request number, URL and CI state;
* for a worker with a tool approval pending, WHAT it asks for as one phrase of
  the closed :data:`APPROVAL_WORDS` vocabulary, chosen by the approval's ACP
  tool-call kind -- never the approval's title, its command or its arguments.

Never the acceptance bar, the conductor's decision, the event log, or any
transcript text. :class:`WorkerRow` has no field that could carry one.
"""

from __future__ import annotations

import functools
import json
import os
import re
import socket
from dataclasses import dataclass, field, replace

#: Longest summary line the board shows.
SUMMARY_CHARS = 140
#: Longest title shown on a row.
TITLE_CHARS = 120
#: Longest goal line in the board header.
GOAL_CHARS = 200

#: What a pending approval asks for, by its ACP tool-call kind (``spawn`` is a
#: coordinator spawn approval). A CLOSED vocabulary: the board shows one of these
#: phrases or "a tool", never the approval's title, which is the transcript's
#: permission row and, for a shell call, its command line.
APPROVAL_WORDS: dict[str, str] = {
    "execute": "a command",
    "edit": "a file edit",
    "delete": "a file delete",
    "move": "a file move",
    "read": "a file read",
    "search": "a search",
    "fetch": "a web fetch",
    "spawn": "a sub-agent",
}


def approval_words(kind: str) -> str:
    """The fixed phrase for an approval of ACP *kind*; ``""`` (shown as "a tool") otherwise."""
    return APPROVAL_WORDS.get(str(kind or "").strip().lower(), "")


WORKING = "working"
IDLE = "idle"
WAITING_ON_YOU = "waiting_on_you"
BLOCKED = "blocked"
DONE = "done"
LOST = "lost"
CLOSED = "closed"

#: Every row state, most urgent first. Rows themselves stay in dispatch order, so
#: a pinned board never reshuffles under the reader's eye.
STATES: tuple[str, ...] = (WAITING_ON_YOU, BLOCKED, LOST, WORKING, IDLE, DONE, CLOSED)
#: Rows that are working or waiting: while one shows, the board is kept current
#: (the watcher's backstop and the publisher's stale-stamp edit).
ACTIVE_STATES = frozenset({WORKING, WAITING_ON_YOU})

STATE_LABELS = {
    WORKING: "working",
    IDLE: "idle",
    WAITING_ON_YOU: "waiting on you",
    BLOCKED: "blocked",
    DONE: "done",
    LOST: "lost",
    CLOSED: "closed",
}


def row_label(row: "WorkerRow") -> str:
    """The words for a row's state: a pending approval names its tool."""
    if row.waiting_on == "approval":
        return f"waiting on you: approve {row.approval_tool or 'a tool'}"
    return STATE_LABELS.get(row.state, row.state)


#: The board's overall state words.
OVERALL_DONE = "done"
OVERALL_NEEDS_YOU = "needs you"
OVERALL_WORKING = "working"
OVERALL_STUCK = "stuck"
OVERALL_IDLE = "idle"
OVERALL_EMPTY = "no workers yet"

#: Reasons that mean only a PERSON can move the item (``work_vocab``'s words).
_PERSON_REASONS = frozenset({"needs_human", "approval"})
_ASKING_STATUSES = frozenset({"blocked", "question"})

#: Liveness of a worker's session, joined server-side by :mod:`.source`.
LIVE_RUNNING = "running"
LIVE_QUEUED = "queued"
LIVE_IDLE = "idle"
LIVE_APPROVAL = "approval"
LIVE_GONE = "gone"
LIVE_UNBOUND = "unbound"

_USER_PING = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_WS = re.compile(r"\s+")


def _redact(text: str) -> str:
    """The outbound redaction, scanned against what Slack will DISPLAY.

    ``redact_for_display`` also scans the text with emphasis, code and link
    markup rendered away, so a credential split by ``*...*`` cannot reassemble
    on screen; ``redact_via_context`` is the platform's egress redactor.
    """
    from kiro_crew.messaging.display_safety import redact_for_display
    from kiro_crew.platform.context import redact_via_context

    return redact_for_display(text, redact_via_context)[0]


def clean_text(text: str, limit: int) -> str:
    """Agent-authored text made safe for the channel: one line, no pings, redacted.

    The first line only; every Slack mention form defused (the wrap-up's own
    patterns for channel-wide and user-group pings, plus user mentions), so a
    worker's words never page anyone; whitespace folded; the outbound redaction;
    the cut to *limit* characters; and the redaction AGAIN, last, so a cut can
    never leave a credential the first pass would have caught whole.
    """
    from kiro_crew.slack.code_channels.wrapup import _BROADCAST, _GROUP_PING

    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    out = _BROADCAST.sub(r"@\1", first)
    out = _GROUP_PING.sub(lambda m: m.group(1) or "@group", out)
    out = _USER_PING.sub(r"@\1", out)
    out = _WS.sub(" ", out).strip()
    out = _redact(out)
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return _redact(out)


@dataclass(frozen=True)
class Activity:
    """Raw facts about a worker's running turn (:func:`~.source.worker_activity`).

    Never published as is: :func:`step_text` reduces them to one safe line.
    """

    turn_started_at: str = ""
    project: str = ""
    tool_title: str = ""
    tool_kind: str = ""
    tool_input: str = ""
    tool_name: str = ""


#: Longest step line, cut with an ellipsis after the redaction.
STEP_CHARS = 70
#: The step when a turn runs with no tool call in flight and no fresh report.
STEP_THINKING = "thinking"
_INPUT_PATH_KEYS = ("path", "file_path", "filePath", "target_file", "file")
#: An absolute path: a ``/`` not after a word character, ``~`` or ``.``, and not the
#: second slash of a URL's ``scheme://`` (so ``https://host/x`` stays whole while
#: ``-v /srv:/opt/x``, ``host:/var/db`` and ``file:///etc/x`` are all caught).
_ABS_PATH = re.compile(r"(?<![\w~.])(?<!:/)/(?:[\w.@+-]+/)*([\w.@+-]+)")
#: Host names and addresses that say where the work runs: cloud-internal names
#: (``ip-10-0-0-1.ec2.internal``, ``db.internal.corp``), developer desktops
#: (``dev-dsk-…``) and private IPv4 addresses. Not every host name is
#: recognisable as one; public names (``github.com``) are left alone.
_INTERNAL_HOST = re.compile(
    # Anchored on a name START (not a word boundary, which sits at every ``a|-``
    # boundary and made a long ``a-a-a-…`` quadratic).
    r"(?<![\w.-])(?:ip-\d{1,3}(?:-\d{1,3}){3}(?:\.[\w-]+)*"
    r"|[\w-]+(?:\.[\w-]+)*\.(?:internal|local)(?:\.[\w-]+)*"
    r"|dev-dsk-[\w-]+(?:\.[\w-]+)*"
    r"|(?:10|127)(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2})(?![\w-])"
)


def _input_field(raw: str, *keys: str) -> str:
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _host_paths_out(text: str, project: str) -> str:
    """Paths relative to the worker's project, ``~`` for home, nothing else absolute.

    The project prefix is cut first (``src/x.py``), then the home directory
    becomes ``~``; any absolute path still left is shortened to ``…/<name>`` and
    the machine's own host name is replaced, so no host path or host name reaches
    the channel.
    """
    home = os.path.expanduser("~")
    for root in sorted(
        {p for p in (project, os.path.realpath(project) if project else "") if p},
        key=len,
        reverse=True,
    ):
        root = root.rstrip("/")
        if root and root != home:
            text = text.replace(root + "/", "").replace(root, ".")
    for prefix in sorted({home, os.path.realpath(home)}, key=len, reverse=True):
        if prefix and prefix != "/":
            text = text.replace(prefix, "~")
    text = _ABS_PATH.sub(lambda m: "…/" + m.group(1), text)
    for name in _host_names():
        text = text.replace(name, "host")
    return _INTERNAL_HOST.sub("host", text)


@functools.lru_cache(maxsize=1)
def _host_names() -> tuple[str, ...]:
    """This machine's own names, longest first (read once: ``getfqdn`` may hit DNS)."""
    names = set()
    for read in (socket.gethostname, socket.getfqdn):
        try:
            names.add(read())
        except OSError:
            continue
    return tuple(
        sorted((n for n in names if len(n) > 2 and n != "localhost"), key=len, reverse=True)
    )


def tool_step(activity: Activity) -> str:
    """The call in flight, worded the way the dashboard's live line words it (raw)."""
    title = re.sub(r"^[^\w@]+", "", activity.tool_title or "").strip()
    kind = (activity.tool_kind or "").lower()
    if kind == "execute":
        command = _input_field(activity.tool_input, "command", "cmd") or re.sub(
            r"^Running:?\s*", "", title
        )
        return f"Running {command}" if command else ""
    if kind == "edit":
        path = _input_field(activity.tool_input, *_INPUT_PATH_KEYS)
        return f"Editing {path}" if path else title
    if kind == "read":
        path = _input_field(activity.tool_input, *_INPUT_PATH_KEYS)
        return f"Reading {path}" if path else title
    if activity.tool_name:
        return activity.tool_name
    if title.startswith("@") and "/" in title:
        return title.rsplit("/", 1)[-1]
    return title


def safe_line(
    text: str, project: str = "", *, limit: int = STEP_CHARS, first_line: bool = False
) -> str:
    """Worker text made publishable as ONE line of at most *limit* characters.

    In this order: the outbound redaction over the WHOLE raw text (before anything
    rewrites it, so a pattern such as a URL's ``user:pw@`` still sees its ``://``),
    then the host paths and host names out (relative to the worker's worktree, ``~``,
    ``…/name``, ``host``), then ONE line -- the first only when *first_line* (a
    report's summary, a title, a goal), every line folded into it otherwise (a
    command) -- then :func:`clean_text`: pings defused, redacted again, cut,
    redacted once more, so a credential straddling the cut cannot leave half of
    itself behind. Mrkdwn is escaped where it is rendered.
    """
    if not text or not text.strip():
        return ""
    # The redaction sees the WHOLE text; only then is it bounded (a few lines'
    # worth beyond the cut), so a huge heredoc costs the rewrite nothing.
    scrubbed = _host_paths_out(_redact(text)[: limit * 8], project)
    if first_line:
        scrubbed = scrubbed.strip().splitlines()[0] if scrubbed.strip() else ""
    return clean_text(" ".join(scrubbed.split()), limit)


def step_text(activity: Activity, *, report: str = "") -> str:
    """The working row's step: the call in flight, else a fresh report, else thinking."""
    raw = tool_step(activity)
    if raw:
        return safe_line(raw, activity.project) or STEP_THINKING
    if report:
        return safe_line(report, activity.project, first_line=True)
    return STEP_THINKING


@dataclass(frozen=True)
class WorkerRow:
    """One work item on the board, already reduced to the published set."""

    #: The ledger's server-minted item id (``it_<hex>``): a stable row key.
    item_id: str
    title: str
    state: str
    summary: str = ""
    branch: str = ""
    pr_number: int = 0
    pr_url: str = ""
    pr_state: str = ""
    pr_ci: str = ""
    #: ``(passed, total, running)`` for the PR's checks (``None``: not known).
    pr_checks: tuple[int, int, int] | None = None
    #: The worker's dashboard slot key, for the deep link (never shown as text).
    session_key: str = ""
    #: ``last_report_at`` of the report that put the row in its state; the
    #: needs-you alert is sent once per value.
    report_stamp: str = ""
    round: int = 0
    #: What a ``waiting_on_you`` row waits on: ``approval`` (a tool approval on
    #: the worker's session) or ``report`` (a report naming a person).
    waiting_on: str = ""
    #: For an ``approval`` wait, what it asks for: one :data:`APPROVAL_WORDS`
    #: phrase (``""`` when the kind is unknown), never the approval's own text.
    approval_tool: str = ""
    #: For a ``closed`` row, how the conductor closed it: ``rejected`` / ``abandoned``.
    closed_as: str = ""
    #: For a ``working`` row: what the turn is doing now (:func:`step_text`, safe).
    step: str = ""
    #: For a ``working`` row: when its turn started (ISO), for an elapsed time.
    turn_started_at: str = ""

    @property
    def needs_owner(self) -> bool:
        return self.state == WAITING_ON_YOU

    @property
    def settled(self) -> bool:
        return self.state in {DONE, CLOSED}

    def with_pr(
        self,
        number: int,
        url: str,
        state: str,
        ci: str,
        checks: tuple[int, int, int] | None = None,
    ) -> WorkerRow:
        return replace(
            self, pr_number=number, pr_url=url, pr_state=state, pr_ci=ci, pr_checks=checks
        )


#: Slack's limit on tasks in one plan block.
MAX_TASKS = 50
#: Rows the board message, the context bar and the Code tab show at most
#: (Slack caps a message at 50 blocks). Totals always count every item.
MAX_SHOWN = 20


@dataclass(frozen=True)
class BoardSnapshot:
    """The whole board for one channel: EVERY item, in dispatch order."""

    goal: str = ""
    round: int = 0
    rows: tuple[WorkerRow, ...] = field(default_factory=tuple)

    @property
    def shown(self) -> tuple[WorkerRow, ...]:
        """The rows on display: every open row first (dispatch order), then the
        most recently dispatched settled ones, at most :data:`MAX_SHOWN`."""
        open_rows = [r for r in self.rows if not r.settled]
        settled = [r for r in self.rows if r.settled]
        room = max(0, MAX_SHOWN - len(open_rows))
        keep = set(id(r) for r in open_rows[:MAX_SHOWN])
        keep.update(id(r) for r in (settled[-room:] if room else []))
        return tuple(r for r in self.rows if id(r) in keep)

    @property
    def omitted(self) -> int:
        return len(self.rows) - len(self.shown)

    @property
    def plan_rows(self) -> tuple[WorkerRow, ...]:
        """The rows the plan block shows: all of them up to :data:`MAX_TASKS`, else the
        :data:`MAX_TASKS` - 1 most urgent (open first, then by state), in dispatch order."""
        if len(self.rows) <= MAX_TASKS:
            return self.rows
        urgent = sorted(self.rows, key=lambda r: (r.settled, STATES.index(r.state)))
        keep = {id(r) for r in urgent[: MAX_TASKS - 1]}
        return tuple(r for r in self.rows if id(r) in keep)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def done(self) -> int:
        return sum(1 for r in self.rows if r.state == DONE)

    @property
    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(STATES, 0)
        for row in self.rows:
            out[row.state] = out.get(row.state, 0) + 1
        return out

    @property
    def overall(self) -> str:
        return overall_state(self.rows)

    @property
    def aggregate(self) -> str:
        return aggregate_line(self.rows)


def row_state(
    *,
    item_state: str,
    status: str | None,
    reason: str | None,
    live: str,
    verdict: str | None = None,
    handed_back: bool = False,
) -> str:
    """The derived row state, from the item and its worker's live session.

    The order is the rule, most decisive first:

    1. a closed item: ``accepted`` is done, ``rejected`` / ``abandoned`` closed;
    2. a tool approval waiting on the worker's session: waiting on you;
    3. a turn in flight or queued: working (a fresh turn outranks an old report);
    4. a ``blocked`` / ``question`` report the conductor already answered (a
       ``decide`` at or after it) or a ``done`` the conductor's evaluator failed:
       the move is the worker's again, so the report's own word does not apply
       (idle, or lost when gone);
    5. a ``blocked`` / ``question`` report a person must move: waiting on you;
    6. any other ``blocked`` / ``question``: blocked (a service, or the conductor);
    7. a session that is gone: done when it last reported done, else lost;
    8. a ``done`` report: done (awaiting the conductor's acceptance);
    9. otherwise idle (open, quiet, or not dispatched yet).
    """
    if item_state == "accepted":
        return DONE
    if item_state in {"rejected", "abandoned"}:
        return CLOSED
    if live == LIVE_APPROVAL:
        return WAITING_ON_YOU
    if live in {LIVE_RUNNING, LIVE_QUEUED}:
        return WORKING
    if handed_back or (status == "done" and verdict == "fail"):
        return LOST if live == LIVE_GONE else IDLE
    if status in _ASKING_STATUSES:
        return WAITING_ON_YOU if reason in _PERSON_REASONS else BLOCKED
    if live == LIVE_GONE:
        return DONE if status == "done" else LOST
    if status == "done":
        return DONE
    return IDLE


def overall_state(rows: tuple[WorkerRow, ...] | list[WorkerRow]) -> str:
    """One word for the whole board."""
    if not rows:
        return OVERALL_EMPTY
    states = {r.state for r in rows}
    if states <= {DONE, CLOSED}:
        return OVERALL_DONE
    if WAITING_ON_YOU in states:
        return OVERALL_NEEDS_YOU
    if WORKING in states:
        return OVERALL_WORKING
    if states & {BLOCKED, LOST}:
        return OVERALL_STUCK
    return OVERALL_IDLE


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def aggregate_line(rows: tuple[WorkerRow, ...] | list[WorkerRow]) -> str:
    """``"2 working · 1 waiting on you · 1 of 4 done · 1 PR open"``: the one-line status.

    Only non-zero counts are named, in urgency order after "working"; the done
    count always follows, then the open PRs when there are any. An empty board
    says so.
    """
    if not rows:
        return "No workers yet"
    counts = dict.fromkeys(STATES, 0)
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    parts: list[str] = []
    if counts[WORKING]:
        parts.append(f"{counts[WORKING]} working")
    for state in (WAITING_ON_YOU, BLOCKED, LOST, IDLE):
        if counts[state]:
            parts.append(f"{counts[state]} {STATE_LABELS[state]}")
    if counts[CLOSED]:
        parts.append(f"{counts[CLOSED]} closed")
    parts.append(f"{counts[DONE]} of {len(rows)} done")
    open_prs = sum(1 for r in rows if r.pr_number and r.pr_state in {"open", "draft"})
    if open_prs:
        parts.append(_plural(open_prs, "PR open", "PRs open"))
    return " · ".join(parts)


#: ``work_aggregate`` states, worst first: the first that applies wins.
AGGREGATE_STATES: tuple[str, ...] = ("waiting", "stuck", "working", "done", "idle")


def work_aggregate(snapshot: BoardSnapshot | None) -> dict | None:
    """The conductor slot's ``work_aggregate`` field, or ``None`` with no workers.

    THE FIELD CONTRACT (read by the dashboard sidebar to collapse a conductor's
    workers under its row)::

        {"state": "waiting" | "stuck" | "working" | "done" | "idle",
         "label": "<the board's summary line>",
         "counts": {"working": n, "waiting": n, "stuck": n, "done": n, "total": n}}

    ``state`` is worst first (:data:`AGGREGATE_STATES`): ``waiting`` when any
    worker waits on you, ``stuck`` when any is blocked or lost, ``working`` when any
    runs, ``done`` when every item is settled (done or closed), else ``idle``.
    ``label`` is the board's own summary line, made only of counts and fixed words
    and passed through the board's redaction all the same.
    """
    if snapshot is None or not snapshot.rows:
        return None
    rows = snapshot.rows
    counts = {
        "working": sum(1 for r in rows if r.state == WORKING),
        "waiting": sum(1 for r in rows if r.state == WAITING_ON_YOU),
        "stuck": sum(1 for r in rows if r.state in {BLOCKED, LOST}),
        "done": sum(1 for r in rows if r.state == DONE),
        "total": len(rows),
    }
    if counts["waiting"]:
        state = "waiting"
    elif counts["stuck"]:
        state = "stuck"
    elif counts["working"]:
        state = "working"
    elif all(r.settled for r in rows):
        state = "done"
    else:
        state = "idle"
    return {"state": state, "label": clean_text(snapshot.aggregate, 200), "counts": counts}
