"""The code-channel dispatch exemption: a Slack code channel's own session may conduct.

A Slack code channel runs in ONE dashboard slot -- the one the code-channel runtime
created for it and recorded in ``slack-code-channels.json`` -- whose turns are
mirrored into the channel. Every channel containment gate refuses such a slot:
``session_create`` (``mirrored_caller``), the session-control caller half
(``mirrored_caller``) and the work ledger (``channel_session``). The containment
protects against two things: words from a room other people are in driving
dispatch, and private dispatch data (briefs, worker reports, other sessions'
transcripts) being published into that room. The default code-channel agent is a
conductor, which writes nothing itself, so under that containment it can do
nothing at all.

This module is the ONE predicate (:func:`judge_code_channel`) those three gates
consult, beside the owner-DM exemption, to let that slot -- and only that slot --
conduct, bounded so neither protection is given up:

* **Identity, server-side.** The slot is the channel's session only when the
  code-channel store (in memory once loaded; write-protected on disk) holds a
  binding naming THIS slot key, the binding's anchor resolves to THIS slot object
  through the live Slack-link index, and the slot's own Slack link names the
  binding's channel. A title, key prefix or agent name is never read.
* **Who drives the turn.** Slack is owner-only on this gateway: the inbound paths
  drop every message whose author fails ``access.is_allowed_user`` before it is
  routed. A channel-origin turn is admitted only when every Slack author whose
  message was ROUTED into this slot -- as a turn or as a steer -- IS the owner
  (``is_owner``, by identity, so the clause does not widen with the allow-list) NOW (:func:`note_channel_sender` records them at routing); a
  dashboard turn and the slot's own patrol wake carry no routed channel text and
  pass. This clause is about who DRIVES the conversation, not about what it reads:
  third-party text the session reads as content -- the origin thread the first
  turn carries as fenced untrusted context, a pasted log, an issue body, a file in
  the worktree -- is content-borne injection, which no author check can close
  (the same text is readable from the worktree on any later turn). That class is
  bounded by reach instead, below.
* **Audience.** ONE read of the slot's outbound binding must name exactly the
  channel's own room and nothing else, and the channel must be confirmed as not
  shared beyond this workspace (:mod:`kiro_crew.slack.code_channels.audience`);
  the admission recorded for the publisher is built from that read.
* **Reach.** The slot is creator-fenced (:func:`is_code_channel_slot` feeds
  ``_caller_is_ownership_fenced``), only the verbs in
  :data:`EXEMPT_TARGET_OPERATIONS` are admitted -- worker transcripts are never
  read into the channel -- and nothing it creates may create in turn
  (:func:`code_channel_root`).

Fails CLOSED throughout: an unloaded store, an unreadable mirror, an unknown share
state or an unrecorded sender all refuse. One switch turns it all on:
``slack.code_channel_dispatch``, OFF by default and opt-in on the Slack Code card,
with ``slack.code_channels`` on and capable beneath it. Turning code channels on
never turns dispatch on. A missing, malformed or unreadable value -- or any config
file or ``slack`` section this process failed to read whole -- reads as off.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.session_control_parts.caller_access import (
    _SLACK,
    _bare_channel,
    _sel_off_loop,
    code_channel_binding_of,
)
from kiro_crew.dashboard.session_control_parts.delivery_containment import (
    _meta_from_probe,
    _mirror_identity_of,
    _read_mirror_binding,
)
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

# The session-control family's logger name, which log filters key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")

#: Session-control verbs the exemption admits, by the ``operation`` name the gate
#: audits under. Each acts on a worker the conductor created (the creator fence
#: bounds them) and returns nothing of the worker's transcript: its state, a
#: delivery receipt, a close or a stop. ``read`` and ``summary`` are absent on
#: purpose -- they put a worker's transcript, with its tool output, into a reply
#: the channel's members read -- as are ``broadcast`` (fan-out of channel words),
#: ``fork`` (a copy of the channel's own transcript) and every verb that spends
#: another session's model unasked (retry, continue, reload, set_model).
EXEMPT_TARGET_OPERATIONS: frozenset[str] = frozenset(
    {"status", "send", "close", "stop", "end_wait"}
)

#: The operation name for a creation and for every work-ledger route.
CREATE_OPERATION = "create"
LEDGER_OPERATION = "work_ledger"


@dataclass(frozen=True)
class CodeChannelVerdict:
    """One judgement of a slot against the exemption, on ONE read of its binding.

    ``applies`` is whether the slot is a code channel's session at all (identity
    alone) -- a gate consults the rest only when it does, so every other slot keeps
    exactly the containment it had. ``refusal`` is ``""`` when the exemption
    admits, else the first clause that failed (generic: never an id). ``admission``
    is the audience to record, built from the row the clauses judged.
    """

    applies: bool
    refusal: str
    channel_id: str = ""
    admission: dict[str, Any] | None = None


_NOT_A_CODE_CHANNEL = CodeChannelVerdict(
    applies=False, refusal="the session is not a code channel's session"
)


# ── Who wrote what this slot received ──────────────────────────────────────

#: Most authors remembered per slot; a slot past it fails closed (it is not one
#: the owner alone drives).
_MAX_SENDERS = 8
#: Most slots tracked; the oldest drop first and then read as "no author on
#: record", which refuses a channel-origin turn rather than admitting one.
_MAX_SLOTS = 512

_senders_lock = threading.Lock()
_senders: OrderedDict[str, tuple[Any, set[str], bool]] = OrderedDict()


def note_channel_sender(slot: Any, user_id: str) -> None:
    """Record that *slot* received Slack text authored by *user_id*.

    Called at the one place a Slack message is routed into a dashboard slot
    (``maybe_route_linked_thread``), after its own authorization check. Kept per
    slot OBJECT (a re-minted key is a new record), never reset by a turn: the
    exemption asks whether everyone who has driven this conversation from Slack
    is someone allowed to drive it, and a turn boundary does not take an earlier
    instruction out of the model's context.
    """
    key = str(getattr(slot, "key", "") or "")
    if not key:
        return
    with _senders_lock:
        entry = _senders.get(key)
        if entry is None or entry[0] is not slot:
            entry = (slot, set(), False)
        _slot, authors, overflowed = entry
        if user_id:
            authors.add(user_id)
        else:
            overflowed = True  # an author we cannot name cannot be vouched for
        if len(authors) > _MAX_SENDERS:
            overflowed = True
            authors = set(list(authors)[:_MAX_SENDERS])
        _senders[key] = (slot, authors, overflowed)
        _senders.move_to_end(key)
        while len(_senders) > _MAX_SLOTS:
            _senders.popitem(last=False)


def _recorded_senders(slot: Any) -> tuple[frozenset[str], bool]:
    """``(authors, overflowed)`` on record for *slot* (empty when none)."""
    with _senders_lock:
        entry = _senders.get(str(getattr(slot, "key", "") or ""))
    if entry is None or entry[0] is not slot:
        return frozenset(), False
    return frozenset(entry[1]), entry[2]


def forget_senders() -> None:
    """Drop every record (tests)."""
    with _senders_lock:
        _senders.clear()


def _turn_carries_channel_text(slot: Any) -> bool:
    """The running turn was opened by, or steered with, a channel human's text."""
    return (
        getattr(slot, "_turn_channel_origin", False) is True
        or getattr(slot, "_turn_channel_narrowed", False) is True
    )


def _sender_refusal(slot: Any) -> str:
    """Why the running turn is not one the owner alone drove; ``""`` when it is."""
    if not _turn_carries_channel_text(slot):
        # A dashboard turn (the owner typed it), the slot's own patrol or monitor
        # wake, or a worker's report. None carries channel text. Another session
        # cannot inject into this slot at all: a mirrored target is refused.
        return ""
    authors, overflowed = _recorded_senders(slot)
    if overflowed:
        return "the channel's session has been driven by more Slack authors than can be vouched for"
    if not authors:
        return (
            "the author of this turn's channel message is not on record -- it is not "
            "kept across a gateway restart, so send a message in the channel and retry"
        )
    try:
        from kiro_crew.slack.handler import is_owner
    except Exception:  # pragma: no cover - import failure fails closed
        return "the Slack owner check is unavailable"
    try:
        # The OWNER, by identity: the clause is "only the owner's Slack text drove
        # this conversation", which must not widen if the Slack allow-list ever
        # admits other people again.
        if all(is_owner(author) for author in authors):
            return ""
    except Exception:
        logger.debug("code-channel sender check failed", exc_info=True)
        return "the Slack owner check could not be completed"
    return "the channel's session has been driven from Slack by someone other than the owner"


# ── The switch ─────────────────────────────────────────────────────────────


def _live_config() -> Any | None:
    from kiro_crew.slack.code_channels.binding import live_config

    return live_config()


#: How long an off-loop reading of the config paths (:func:`refresh_config_path_check`)
#: answers the synchronous gate. Every session-control verb and every work-ledger
#: route refreshes it immediately before judging, so this only bounds a re-judgement
#: later in the same request (the ledger routes re-check after their reads).
_PATH_CHECK_TTL_S = 10.0

#: ``(time.monotonic() stamp, answer)`` of the last off-loop reading, or None.
_path_check: tuple[float, str] | None = None
_path_check_lock = threading.Lock()
_path_refreshing = False

#: The answer while no fresh reading exists: unknown, so the switch errs off.
_PATH_CHECK_PENDING = "the config files have not been checked yet"


def _check_config_paths_blocking() -> str:
    """The first config file present but not readable as one, ``""`` when none.

    BLOCKING (``lstat``/``stat``): only :func:`refresh_config_path_check` calls it,
    off the event loop. The loader reads a config path that is a directory or a
    dangling symlink as absent, which is right for every other setting: such a
    path holds no document. This opt-in reads it as unknown instead, since it can
    stand where an opt-out was, and the switch errs off.
    """
    import os
    import stat as _stat

    from kiro_crew.config.loader import config_local_path, config_path

    for path in (config_path(), config_local_path()):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            return path.name
        try:
            if _stat.S_ISLNK(st.st_mode):
                st = os.stat(path)  # FileNotFoundError: a dangling link
            if not _stat.S_ISREG(st.st_mode):
                return path.name
        except OSError:
            return path.name
    return ""


async def refresh_config_path_check() -> None:
    """Read the config paths OFF the event loop for :func:`_unreadable_config_paths`.

    Awaited right before the synchronous gates by every session-control verb (via
    ``load_code_channel_lineage``) and by every work-ledger route, so the gate
    never touches the filesystem on the loop. Never raises: a failed reading is
    recorded as unreadable.
    """
    import asyncio
    import time

    global _path_check
    try:
        answer = await asyncio.to_thread(_check_config_paths_blocking)
    except Exception:  # pragma: no cover - fail closed on an unexpected error
        logger.debug("code-channel dispatch: config path check failed", exc_info=True)
        answer = "the config files could not be checked"
    with _path_check_lock:
        _path_check = (time.monotonic(), answer)


def _refresh_config_path_check_soon() -> None:
    """Start one background reading from a synchronous gate that found none fresh."""
    import asyncio

    global _path_refreshing
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with _path_check_lock:
        if _path_refreshing:
            return
        _path_refreshing = True

    async def _run() -> None:
        global _path_refreshing
        try:
            await refresh_config_path_check()
        finally:
            with _path_check_lock:
                _path_refreshing = False

    try:
        loop.create_task(_run())
    except Exception:  # pragma: no cover - no task, try again next call
        with _path_check_lock:
            _path_refreshing = False


def _forget_config_path_check() -> None:
    """Drop the cached reading (tests)."""
    global _path_check
    with _path_check_lock:
        _path_check = None


def _unreadable_config_paths() -> str:
    """The first config file not readable as one, ``""`` when none. No disk access.

    Read ONLY by this switch, so nothing else in the gateway changes. Two sources,
    each erring off:

    - a file the live watcher has found torn since the gateway started
      (:func:`kiro_crew.config.live.torn_config_files`, sticky until restart: a
      tear can hide an opt-out the snapshot the watcher kept never adopted);
    - the last off-loop reading of the paths (:func:`refresh_config_path_check`):
      a directory or a dangling symlink at either path. With no reading younger
      than :data:`_PATH_CHECK_TTL_S` the answer is unknown, and one is started.
    """
    import time

    from kiro_crew.config.live import torn_config_files
    from kiro_crew.config.loader import config_local_path, config_path

    torn = torn_config_files()
    for path in (config_path(), config_local_path()):
        if str(path) in torn:
            return path.name
    with _path_check_lock:
        reading = _path_check
    if reading is None or time.monotonic() - reading[0] > _PATH_CHECK_TTL_S:
        _refresh_config_path_check_soon()
        return _PATH_CHECK_PENDING
    return reading[1]


#: The refusal while the owner has not turned hand-off on. It names the control a
#: person uses -- the Settings path and the toggle's own label -- and the config key
#: only in parentheses, because an agent relays this text to the person as is.
#: Must stay in step with the card's label (``code_channel_dispatch`` in the
#: locale catalog) and the Slack notice (:mod:`kiro_crew.slack.code_channels.handoff`).
DISPATCH_OFF_REFUSAL = (
    "hand-off is turned off for code channels. The owner can turn it on in "
    'Settings → Channels → Slack → Code channels → "Let the conductor hand off work" '
    '(slack.code_channel_dispatch), or with the "Turn on hand-off for code '
    'channels" button posted in this channel; then ask again'
)


def _switch_refusal() -> str:
    """Why the exemption is switched off; ``""`` when it is on."""
    try:
        cfg = _live_config()
    except Exception:
        cfg = None
    if cfg is None:
        return "the Slack configuration is unreadable"
    # The switch is opt-in (default off), so a LOST value reads off by itself. A
    # config FILE that could not be read whole (``config.local.json`` corrupt or
    # unreadable) or a malformed ``slack`` section can still have held an opt-OUT
    # shadowing an opt-in in the other file. The loader reports exactly that -- on
    # this snapshot's ``degraded_sections`` and in the process-wide record every
    # read writes (the loader's own, sticky until restart; nothing here adds to
    # it). Both are read: the live Slack config is a cached object a reload copies
    # fields onto, and it does not carry the degradation. A path that is a
    # directory, a dangling link or ever torn under the live watcher is this
    # switch's own check (:func:`_unreadable_config_paths`, read off the loop), so
    # no other gate moves.
    from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG, observed_degraded_sections

    degraded = frozenset(getattr(cfg, "degraded_sections", None) or ()) | (
        observed_degraded_sections()
    )
    if DEGRADED_WHOLE_CONFIG in degraded or "slack" in degraded:
        return (
            "a config file or the slack section could not be read whole, so whether "
            "code-channel dispatch is turned off is unknown; fix the file and restart "
            "the gateway"
        )
    from kiro_crew.slack.code_channels.binding import code_channels_on

    if not code_channels_on(cfg):
        return "Slack code channels are off"
    if getattr(getattr(cfg, "slack", None), "code_channel_dispatch", False) is not True:
        return DISPATCH_OFF_REFUSAL
    if unreadable := _unreadable_config_paths():
        if unreadable == _PATH_CHECK_PENDING:
            return (
                "the config files have not been checked yet, so whether code-channel "
                "dispatch is turned off is unknown; try again in a moment"
            )
        return (
            f"{unreadable} could not be read as a whole file, so whether code-channel "
            "dispatch is turned off is unknown; fix the file (a file found torn also "
            "needs a gateway restart)"
        )
    return ""


# ── The judgement ──────────────────────────────────────────────────────────


def _audience_refusal(binding: Any, read: tuple[Any, str, str] | None) -> str:
    """Why the slot's outbound binding is not exactly the channel's own room."""
    if read is None:
        return "the session store is unreadable"
    mirror, slack_thread, slack_channel = read
    want = (binding.channel_id, binding.anchor_ts)
    rooms: list[tuple[str, str, str]] = []
    if mirror is not None:
        rooms.append(
            (
                str(getattr(mirror, "channel_type", "") or ""),
                _bare_channel(getattr(mirror, "channel_id", "")),
                str(getattr(mirror, "thread_id", "") or ""),
            )
        )
    if slack_thread:
        rooms.append((_SLACK, _bare_channel(slack_channel), slack_thread))
    if not rooms:
        return "the session is not linked to its code channel"
    if any(room != (_SLACK, *want) for room in rooms):
        return "the session also publishes somewhere other than its code channel"
    return ""


def _channel_state_refusal(binding: Any) -> str:
    """Archived, Stop-fenced, workspace not admitted, or shared beyond the workspace."""
    try:
        from kiro_crew.slack.code_channels import audience, surface
    except Exception:  # pragma: no cover
        return "the code-channel surface is unavailable"
    if binding.archived_at or binding.channel_id in getattr(surface, "_archived_here", ()):
        return "the code channel is archived"
    if surface.channel_stopped(binding.channel_id):
        return "someone pressed Stop in the code channel"
    if not surface.workspace_admitted(binding.team_id):
        return "the code channel's Slack workspace is not admitted"
    shared = audience.channel_shared(binding.channel_id)
    if shared is None:
        audience.refresh_soon(binding.channel_id)
        return (
            "whether the code channel is shared outside this workspace is not "
            "freshly on record -- it is being checked now, so retry in a moment"
        )
    if shared:
        return "the code channel is shared outside this workspace (Slack Connect)"
    return ""


def _publish_clauses(
    state: "DashboardState | Any", slot: Any, binding: Any
) -> tuple[str, dict[str, Any] | None]:
    """``(refusal, admission)``: may the channel receive what this slot reads, now?

    The switch, the channel's own state (archived, stopped, workspace, shared) and
    ONE read of the slot's outbound binding, which must name the channel's room
    and nothing else. The admission is built from that same read.
    """
    why = _switch_refusal() or _channel_state_refusal(binding)
    if why:
        return why, None
    sessions = getattr(state, "sessions", None)
    if getattr(sessions, "get_mirror_link", None) is None:
        return "the session store is unavailable", None
    try:
        key = slot_history_key(slot)
    except Exception:
        key = None
    read = None if key is None else _read_mirror_binding(sessions, key)
    why = _audience_refusal(binding, read)
    if why or read is None:
        return why or "the session store is unreadable", None
    return "", _meta_from_probe(state, slot, _mirror_identity_of(read))


def _note_handoff_state(channel_id: str) -> None:
    """Show the owner the Turn-on-hand-off notice once per switch-off; no disk access.

    Off: one notice in the channel (deduplicated per channel until the switch is
    next seen on). On: forget, so a later switch-off notices afresh. Any other
    switch answer (config unreadable, code channels off, paths unchecked) leaves
    the record as it is.
    """
    try:
        from kiro_crew.slack.code_channels import handoff
    except Exception:  # pragma: no cover
        return
    switch = _switch_refusal()
    if switch == DISPATCH_OFF_REFUSAL:
        handoff.notice_soon(channel_id)
    elif not switch:
        handoff.forget(channel_id)


def judge_code_channel(
    state: "DashboardState | Any", slot: Any, *, operation: str
) -> CodeChannelVerdict:
    """Judge *slot* for *operation* against the exemption (see the module docstring).

    Every clause is a positive fact and the first that cannot be established is the
    refusal. The admission is built from the ONE read the audience clause judged,
    so the audience a gate records and the audience it validated are one row.
    Writes one SEL row per judgement of a code channel's session.
    """
    binding = code_channel_binding_of(state, slot)
    if binding is None:
        return _NOT_A_CODE_CHANNEL
    admission: dict[str, Any] | None = None
    if operation not in EXEMPT_TARGET_OPERATIONS | {CREATE_OPERATION, LEDGER_OPERATION}:
        why = f"a code channel's session may not use {operation or 'this verb'} on other sessions"
    else:
        why, admission = _publish_clauses(state, slot, binding)
        _note_handoff_state(binding.channel_id)
    if not why:
        why = _sender_refusal(slot)
    verdict = CodeChannelVerdict(
        applies=True,
        refusal=why,
        channel_id=binding.channel_id,
        admission=admission if not why else None,
    )
    _audit_judgement(slot, operation, verdict)
    return verdict


# ── What may be published ──────────────────────────────────────────────────

#: Longest report-summary line a code channel may be shown.
PUBLISHED_SUMMARY_MAX = 140
#: Longest conductor-goal line a code channel may be shown.
PUBLISHED_GOAL_MAX = 200

#: Slack mention markup needs an angle bracket (``<@U..>``, ``<!here>``,
#: ``<!subteam^..>``, ``<#C..>``), so removing EVERY bracket defuses all of it --
#: including markup a deletion would reassemble from nested pieces
#: (``<<@U0X>!here>``), which removing whole matches cannot promise.
_ANGLE_BRACKETS = str.maketrans("", "", "<>\uff1c\uff1e\u2039\u203a")
#: The bare broadcast words, removed until none is left.
_BARE_PINGS = re.compile(r"@(?:channel|here|everyone)", re.IGNORECASE)


def _defuse(text: str) -> str:
    """*text* with no Slack mention markup left and no bare broadcast word, then
    redacted (credentials, exfiltration URLs) and whitespace-collapsed.

    Redaction runs LAST, on the final normalized text, so a credential or a
    credential-bearing URL that the defusing reassembled is still caught.
    """
    from kiro_crew.dashboard.chat_delivery import sanitize_outbound

    text = str(text or "").translate(_ANGLE_BRACKETS)
    while True:
        cleaned = _BARE_PINGS.sub("", text)
        if cleaned == text:
            break
        text = cleaned
    return " ".join(sanitize_outbound(" ".join(text.split())).split())


def _first_line(text: Any, limit: int) -> str:
    """*text*'s FIRST line only, defused and redacted (:func:`_defuse`), at most
    *limit* characters."""
    first = str(text or "").strip().splitlines()[0:1]
    line = _defuse(first[0] if first else "")
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "\u2026"
    return line


def published_summary(summary: Any) -> str:
    """A worker report summary as a code channel may show it: its first line,
    defused and redacted, at most :data:`PUBLISHED_SUMMARY_MAX` characters."""
    return _first_line(summary, PUBLISHED_SUMMARY_MAX)


def published_goal(goal: Any) -> str:
    """The conductor ledger's goal as a code channel may show it: its first line,
    defused and redacted, at most :data:`PUBLISHED_GOAL_MAX` characters. The goal
    restates the owner's own channel request, so it adds no audience."""
    return _first_line(goal, PUBLISHED_GOAL_MAX)


def published_state(item: Any, *, stale: bool = False) -> str:
    """One word for where an item stands, derived from its state and last report."""
    state = str(getattr(item, "state", "") or "open")
    if state != "open":
        return state
    if stale:
        return "stale"
    status = str(getattr(item, "status", "") or "")
    if status:
        return status
    return "dispatched" if getattr(item, "worker_session_key", None) else "unassigned"


def published_item_fields(item: Any, *, stale: bool = False) -> dict[str, Any]:
    """The ONLY item fields that may be published into a code channel.

    With :func:`published_goal` for the conductor's goal, this is the whole of
    the ledger-derived data a code channel may be shown. Per item: its title; a derived state word (:func:`published_state`); the
    worker's report summary, first line only (:func:`published_summary`); the
    ``branch`` artifact; the PR number and the CI state (the item's acceptance
    verdict). Plus the two handles the conductor needs to act on the item at all
    -- its id and its worker's session key, which names a session the conductor
    created. NEVER the acceptance bar, decision text, event tails, any other
    artifact, or transcript text.
    """
    artifacts = getattr(item, "artifacts", None) or {}
    branch = artifacts.get("branch") if isinstance(artifacts, dict) else None
    title = _defuse(str(getattr(item, "title", "") or ""))
    return {
        "item_id": str(getattr(item, "item_id", "") or ""),
        "title": title,
        "state": published_state(item, stale=stale),
        "summary": published_summary(getattr(item, "summary", "")),
        "branch": _defuse(branch) if isinstance(branch, str) and branch else None,
        "pr": getattr(item, "pr", None),
        "ci": getattr(item, "verdict", None),
        "worker_session_key": getattr(item, "worker_session_key", None),
    }


def _audit_judgement(slot: Any, operation: str, verdict: CodeChannelVerdict) -> None:
    """One SEL row per judgement, naming the code channel; never changes the answer."""
    slot_key = str(getattr(slot, "key", "") or "")

    def _write() -> None:
        sel().log_api_access(
            caller=slot_key or "unknown",
            operation="code_channel_dispatch_exemption",
            outcome="allowed" if not verdict.refusal else "denied",
            source="dashboard",
            resources=f"channel={verdict.channel_id} slot={slot_key} verb={operation}",
            error=verdict.refusal,
        )

    _sel_off_loop(_write, "code-channel exemption audit")


def admits(verdict: CodeChannelVerdict) -> bool:
    """The verdict applies to a code channel and admits."""
    return verdict.applies and not verdict.refusal


def withheld_clause(verdict: CodeChannelVerdict) -> str:
    """The text a refusal appends for a code channel's session (``""`` otherwise)."""
    if not verdict.applies or not verdict.refusal:
        return ""
    return f"; the code-channel exemption is withheld because {verdict.refusal}"


# ── A worker's state and its dashboard link ────────────────────────────────

#: What a code channel's conductor is told about handing off work and reporting on
#: it, seeded into the channel's session (``routing._seed_framing``) and repeated
#: on each ``session_create`` reply it gets. It answers "is it still running?" from
#: the state read, never by hedging about transcripts it cannot read.
CONDUCTOR_GUIDANCE = (
    "Handing off work from this channel: create workers with session_create and "
    "leave the folder out -- they are filed beside you and nest under this session. "
    'To answer "is it still running?" or "how far along is it?", call '
    "session_status and answer from each worker's state and since_activity_s, e.g. "
    '"Fix #21964: working, last activity 40s ago", or "waiting on you: approve '
    'shell" when it shows pending_approval. Use its dashboard_link when you name a '
    "worker. A worker's transcript is not readable from a code channel, and you "
    "need not say so: its state is the answer."
)

#: The verbs that would put another session's transcript into the channel. Never
#: admitted (:data:`EXEMPT_TARGET_OPERATIONS`); their refusal points at the state
#: the conductor MAY read instead, and at the worker's dashboard tab.
TRANSCRIPT_OPERATIONS: frozenset[str] = frozenset({"read", "summary"})

#: Text that must never appear in a link bound for a channel: a sign-in credential.
_CREDENTIAL_MARKERS = ("token=", "access_token", "auth=", "key=", "@")


def worker_dashboard_link(slot_key: str) -> str:
    """A link to *slot_key*'s dashboard tab for a code channel, or ``""``.

    Built server-side from the gateway's own origin (the configured dashboard URL,
    or the live tunnel when ``slack.use_tunnel_url`` opts in), the same shape and
    origin choice the Slack mirror uses for links that can reach a shared channel:
    a PATH link only, with no sign-in token, so opening it still meets the
    dashboard's own sign-in. Passed through the outbound credential/exfiltration
    filter like every other text bound for a channel, and dropped whole if that
    filter changes it or anything credential-shaped remains. No disk access.
    """
    if not slot_key:
        return ""
    try:
        from kiro_crew.dashboard.chat_backfill import session_deep_link
        from kiro_crew.dashboard.chat_delivery import sanitize_outbound
        from kiro_crew.dashboard.urls import tunnel_origin_if_opted_in

        cfg = _live_config()
        dashboard_url = str(getattr(getattr(cfg, "dashboard", None), "url", "") or "")
        use_tunnel = getattr(getattr(cfg, "slack", None), "use_tunnel_url", False) is True
        link = session_deep_link(
            dashboard_url, slot_key, tunnel_url=tunnel_origin_if_opted_in(use_tunnel)
        )
        if not link or sanitize_outbound(link) != link:
            return ""
    except Exception:  # pragma: no cover - no link rather than a broken one
        logger.debug("code-channel worker link unavailable", exc_info=True)
        return ""
    lowered = link.lower()
    if any(marker in lowered for marker in _CREDENTIAL_MARKERS):
        return ""
    return link


def transcript_read_hint(state: "DashboardState | Any", caller_slot: Any, target: str) -> str:
    """What a code channel's conductor may do instead of reading *target*'s transcript.

    Appended to the transcript-verb refusal. Names ``session_status`` (state only)
    and, when *target* is a session this conductor created, a dashboard link to it.
    A target that is not its own gets the same sentence with no link, so the
    refusal tells it nothing about other sessions.
    """
    hint = (
        "; a code channel may not read another session's transcript, so check the "
        "worker with session_status (working, idle, queued, waiting on an approval, "
        "closed or lost, and how long since its last activity)"
    )
    get_slot = getattr(state, "get_slot", None)
    worker = get_slot(target) if target and callable(get_slot) else None
    caller_key = str(getattr(caller_slot, "key", "") or "")
    own = (
        worker is not None
        and bool(caller_key)
        and str(getattr(worker, "_created_by", "") or "") == caller_key
    )
    link = worker_dashboard_link(str(worker.key)) if own and worker is not None else ""
    if link:
        return f"{hint}, or open it in the dashboard: {link}"
    return f"{hint}, or open it in the dashboard"
