"""Resume code channels that a gateway restart interrupted.

A code channel's binding survives a restart (``slack-code-channels.json``), so the
next message already reaches the same session. What did not survive is a turn
that was running: nothing picks it up, and the channel still shows Slack's
"Working..." loader. :func:`resume_after_restart` runs once per gateway start,
after the memory barrier and once Slack is connected.

**Whose work.** Only a channel whose binding, live Slack-link index and slot all
agree it is the channel's session (the dispatch exemption's strict identity), in
a channel that is admitted right now: not archived, fenced, stopped or wrapped
up, in an admitted workspace, let in by its channel and origin activation and by
channels governance. Then that session, and each of ITS OWN workers -- recorded
by the store at birth or listed by the crew log's session tree, whose live
``created_by`` still names it and which is linked to no channel. Nothing else.

**Automatic, once.** Each interrupted turn (``is_turn_interrupted``, the signal
``session_continue`` uses for "resumed") is continued through the Resume button's
own path (``continue_slot_turn(require_interrupted=True)``: the fixed "resumed"
body, no prompt text). Before that, the turn -- the ts of the row that opened it;
a Resume's recovery row continues it, so a second interruption names the same
turn -- is recorded in the write-protected code-channel store. A recorded turn is
never resumed automatically again, so a turn that takes the gateway down cannot
loop; a turn whose opening row is no longer in the window has no provable
identity and is never resumed automatically; a store that cannot be written
means no automatic resume. Identity and admission are re-checked under the slot
lock immediately before the continuation is queued.

**One notice**, top level, after the stale status is settled: "picking up where I
left off" with an owner-only **Stop** button when everything resumed, else "Kiro
Crew restarted while I was working." with the reason it paused (the turn already
restarted once, its identity is unknown, or its record could not be saved) and an
owner-only **Resume** button. Stop is the owner's "stop what the restart resumed",
as the dashboard's Stop control: single use on the recorded notice ts, it stops
(``stop_slot_turn``) only the sessions that notice recorded as resumed and that are
still the channel's own, each SEL-audited. The Resume button follows the
session-resume button pattern of ``slack/interactions.py`` (an action-id prefix
plus a short hash, a JSON value, prefix-dispatched) under the gateway's reserved
``kc_code_channel_`` prefix. It is the owner's explicit "resume what is
interrupted here now", as the dashboard's Resume control is: it counts only on the
notice ts the store recorded, is claimed by compare-and-set, re-runs every
identity and admission check, and is given back (still pressable) when nothing
could be resumed. A notice whose set of held sessions already has a button is not
posted again; when a notice cannot be posted or recorded, the owner is told in
the dashboard, with a link to each held session.

**Channel authority.** A resumed conductor turn is stamped as channel text, as the
turn it continues was: the dispatch exemption's sender check then applies to it, and
after a restart no author is on record, so it hands off nothing until someone sends
a message in the channel. Every decision is SEL-audited with the channel id.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import code_channels_on, live_config
from kiro_crew.slack.code_channels.store import Binding, store

logger = logging.getLogger(__name__)

OPERATION = "slack.code_channel.resume"
STOP_OPERATION = "slack.code_channel.resume.stop"
RESUME_ACTION_PREFIX = "kc_code_channel_resume_"
#: The automatic notice's Stop button. Not a ``RESUME_ACTION_PREFIX`` extension:
#: the router dispatches each prefix on its own.
STOP_ACTION_PREFIX = "kc_code_channel_stop_"

AUTO_TEXT = "Kiro Crew restarted while I was working; picking up where I left off."
HELD_TEXT = (
    "Kiro Crew restarted while I was working. "
    "I've paused this channel's work until you press Resume, because {reason}."
)
HELD_SOME_TEXT = (
    "Kiro Crew restarted while I was working. I picked up part of this channel's "
    "work and paused the rest until you press Resume, because {reason}."
)
RESUMED_LINE = "*Picked up:* {names}"
HELD_LINE = "*Paused:* {names}"
#: Why a session was held for the button rather than resumed automatically.
HELD_ALREADY = "this work was already resumed once after a restart"
HELD_UNKNOWN = "I couldn't tell which turn was interrupted"
HELD_UNRECORDED = "I couldn't save the record that this turn was resumed"
HELD_REFUSED = "the work couldn't be restarted right now"
STOP_TEXT = "Stop resumed work"
STOPPED_TEXT = (
    "Stopped by <@{user}>. The resumed work is stopped; send a message here to start again."
)
STOPPED_IDLE_TEXT = (
    "<@{user}> pressed Stop; none of the work the restart resumed was still running."
)
STOP_LOST_TEXT = (
    "I couldn't stop the resumed work ({reason}), and this button can't be offered "
    "again. Stop it from the session in the dashboard."
)
STOP_FAILED_TEXT = "I couldn't stop the resumed work ({reason}). Press Stop again in a moment."
STOP_NOT_OWNER_TEXT = ":no_entry: Only {owner} can stop this channel's resumed work."
STOP_STALE_TEXT = "That Stop button is out of date."
STOP_NOT_ALLOWED_TEXT = ":no_entry: You can't stop work from this channel."

#: What a Resume press tells the owner for each refusal code (the audit keeps the code).
_REFUSAL_WORDS = {
    "slot_running": "it is already running",
    "slot_stopping": "a stop is in progress",
    "slot_queue_pending": "messages are waiting in that session's queue",
    "slot_approval_pending": "it is waiting on an approval",
    "slot_subagents_running": "its sub-agents are still running",
    "slot_not_found": "there is nothing to continue",
    "slot_empty": "there is nothing to continue",
    "turn_not_failed": "there is nothing to continue",
    "session_start_repeat": "its agent session keeps failing to start",
    "code_channel_resume_changed": "something changed since the restart",
    "code_channel_resume_busy": "the gateway was busy",
    "attempt not recorded": "the record that it was resumed couldn't be saved",
}


def _in_words(code: str) -> str:
    return _REFUSAL_WORDS.get(code, "it couldn't be restarted")


def _sessions(count: int) -> str:
    return f"{count} session" + ("" if count == 1 else "s")


TRUST_NOTE = (
    "This channel is in Trust mode (its tool calls are approved automatically), so once "
    "work resumes it acts without asking you first."
)
BUTTON_TEXT = "Resume"
PRESSED_TEXT = "Resumed by <@{user}>."
ALREADY_RUNNING_TEXT = "<@{user}> pressed Resume; the work was already running again."
NOTHING_TEXT = "Nothing could be resumed right now ({reason}). Try Resume again in a moment."
NOTHING_LOST_TEXT = (
    "Nothing could be resumed ({reason}), and this button can't be offered again. "
    "Resume it from its session in the dashboard."
)
APPROVAL_LINKS_TEXT = "Waiting for your approval: {links}."
RESUME_LOST_TEXT = (
    "Resumed {done}; {rest} could not be resumed ({reason}), and this button "
    "can't be offered again. Resume {rest} from the dashboard."
)
NOTHING_APPROVAL_TEXT = (
    "Nothing could be resumed yet: a tool call is waiting for your approval in {links}. "
    "Approve or decline it there, then press Resume."
)
PARTIAL_APPROVAL_TEXT = (
    "Resumed {done}. A tool call is waiting for your approval in {links}. "
    "Approve or decline it there, then press Resume."
)
PARTIAL_TEXT = (
    "Resumed {done}; {rest} could not be resumed right now ({reason}). "
    "Press Resume again in a moment for {rest}."
)
NOT_OWNER_TEXT = ":no_entry: Only {owner} can resume this channel's work."
STALE_TEXT = "That Resume button is out of date."
NOT_ALLOWED_TEXT = ":no_entry: You can't resume work from this channel."

#: How long the start-up pass waits for Slack to connect before giving up.
CONNECT_WAIT_S = 120.0
#: What :func:`_interrupted_turn` answers for an interrupted turn whose opening row
#: is no longer in the window. Never resumed automatically: missing identity is not
#: a new turn.
UNKNOWN_TURN = "?"

_NOTICE_PREFIX = "notice:"


@dataclass
class ResumeReport:
    """What one channel's restart pass did, for the work board."""

    channel_id: str
    resumed: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    #: Interrupted-turn label per resumed or held key (``?`` when unknown).
    turns: dict[str, str] = field(default_factory=dict)
    #: Why each held key was held (one of the ``HELD_*`` clauses).
    why: dict[str, str] = field(default_factory=dict)

    def hold(self, key: str, reason: str) -> None:
        self.held.append(key)
        self.why[key] = reason


def held_text(reasons: list[str], *, some_resumed: bool = False) -> str:
    """:data:`HELD_TEXT` naming each distinct reason, in the order first seen."""
    distinct = list(dict.fromkeys(r for r in reasons if r)) or [HELD_REFUSED]
    template = HELD_SOME_TEXT if some_resumed else HELD_TEXT
    return template.format(reason=" and ".join(distinct))


# ── reading the transcript ─────────────────────────────────────────────────


def opening_turn_ts(messages: list[dict[str, Any]]) -> str:
    """The ts of the row that opened the newest turn, ``""`` when none.

    A Resume's recovery row CONTINUES the turn above it (it is not an opener), so
    a resumed turn that is interrupted again names the same opening row.
    """
    from kiro_crew.dashboard.state import TURN_OPENING_INJECT_KINDS

    for row in reversed(messages or []):
        role = row.get("role")
        meta = row.get("meta") or {}
        if role in ("user", "nudge"):
            return str(row.get("ts") or "")
        if role == "inject" and isinstance(meta, dict):
            if meta.get("injectKind") in TURN_OPENING_INJECT_KINDS:
                return str(row.get("ts") or "")
    return ""


async def _interrupted_turn(slot: Any) -> str:
    """The opening ts of *slot*'s interrupted last turn; ``""`` when it finished."""
    from kiro_crew.dashboard.state import is_turn_interrupted, load_window_off_loop

    await load_window_off_loop(slot)
    messages = list(getattr(slot, "messages", None) or [])
    if not is_turn_interrupted(messages):
        return ""
    return opening_turn_ts(messages) or UNKNOWN_TURN


def _same_opening(slot: Any, turn: str) -> str:
    """``""`` while *slot*'s newest opening row is still *turn* (no disk access)."""
    try:
        current = opening_turn_ts(list(getattr(slot, "messages", None) or []))
    except Exception:
        return "transcript unavailable"
    want = "" if turn == UNKNOWN_TURN else turn
    return "" if current == want else "a newer turn started"


# ── whose work, and may the channel be touched now ─────────────────────────


def _is_the_channels_session(state: Any, binding: Binding, slot: Any) -> bool:
    """*slot* IS *binding*'s session by the strict identity the exemption uses."""
    from kiro_crew.dashboard.session_control_parts.caller_access import code_channel_binding_of

    if slot is None or str(getattr(slot, "key", "")) != binding.session_key:
        return False
    if state is None or state.get_slot(binding.session_key) is not slot:
        return False
    found = code_channel_binding_of(state, slot)
    return found is not None and found.channel_id == binding.channel_id


def _is_own_worker(state: Any, conductor_key: str, worker: Any) -> bool:
    """*worker* is an ordinary dashboard session this conductor created."""
    from kiro_crew.dashboard.session_control_parts.caller_access import (
        _channel_link_of,
        code_channel_binding_of,
    )

    if worker is None or not conductor_key:
        return False
    if str(getattr(worker, "_created_by", "") or "") != conductor_key:
        return False
    if _channel_link_of(worker) or getattr(worker, "_slack_thread_ts", ""):
        return False
    return code_channel_binding_of(state, worker) is None


MIRRORED = "mirrored"
UNVERIFIED = "unverified"


def _worker_mirror(state: Any, worker: Any) -> str:
    """``""`` when *worker* has no outbound channel mirror; never waits.

    A dashboard-born worker given an outbound mirror reaches a channel as surely
    as a channel-born one, so it is never resumed (:data:`MIRRORED`). The link
    lives in the session map, whose lock an off-loop writer can hold through a
    disk write; this runs on the event loop (under the slot lock, right before a
    continue is queued), so it only TRIES the lock and answers
    :data:`UNVERIFIED` when it is taken, as for an unreadable link. Both refuse.
    """
    from kiro_crew.dashboard.session_control import _has_channel_mirror
    from kiro_crew.session_map import _MAP_LOCK

    if not _MAP_LOCK.acquire(blocking=False):
        return UNVERIFIED
    try:
        # Re-entrant: the probe's own guarded reads take the lock held here.
        probed = _has_channel_mirror(state, worker, on_probe_failure=True)
    except Exception:
        return UNVERIFIED
    finally:
        _MAP_LOCK.release()
    return MIRRORED if probed else ""


def _own_workers(conductor_key: str) -> list[str]:
    """The conductor's recorded workers: server-written records plus the crew-log tree."""
    keys = set(store().workers_of(conductor_key))
    try:
        from kiro_crew.dashboard.session_control_parts.session_status import (
            _created_tree_roster,
        )

        children, _quality = _created_tree_roster(conductor_key)
        keys.update(children)
    except Exception:  # pragma: no cover - the store records still stand
        logger.debug("code channel resume: crew-log roster unavailable", exc_info=True)
    keys.discard(conductor_key)
    return sorted(keys)


def _admitted_now(state: Any, channel_id: str, conductor: Any) -> str:
    """Why *channel_id* may not be resumed now; ``""`` when it may. No disk access.

    The live binding (archived, wrapped up), this process's archive fence and Stop,
    the workspace, the channel and origin activation, and the conductor's strict
    identity. Run before work starts and again under the slot lock right before a
    continuation is queued.
    """
    from kiro_crew.slack.code_channels.routing import _origin_of, channel_and_origin_permit

    if not code_channels_on(live_config()):
        return "code channels are off"
    binding = store().get_binding(channel_id)
    if binding is None:
        return "no longer a bound code channel"
    if binding.archived_at or binding.wrapped_up_at:
        return "archived or wrapped up"
    if channel_id in getattr(surface, "_archived_here", ()) or surface.channel_stopped(channel_id):
        return "archived or stopped"
    if not surface.workspace_admitted(binding.team_id):
        return "workspace not admitted"
    origin = _origin_of(channel_id, binding, None)
    if not channel_and_origin_permit(live_config(), channel_id, origin):
        return "channel or origin does not admit the bot"
    if not _is_the_channels_session(state, binding, conductor):
        return "not the channel's session"
    return ""


async def _channel_admitted(state: Any, channel_id: str, conductor: Any) -> str:
    """:func:`_admitted_now` plus channels governance (an await)."""
    why = _admitted_now(state, channel_id, conductor)
    if why:
        return why
    if not await surface.outbound_permitted():
        return "channels governance policy"
    return ""


# ── continuing ─────────────────────────────────────────────────────────────


@contextmanager
def _session_map_untaken() -> Iterator[None]:
    """Hold the session map's lock for the continue's final synchronous stretch.

    The re-check, the containment snapshot and the queue insert all read the
    session map, whose lock an off-loop writer can hold through a disk write; on
    the event loop that read would stall every task. So the lock is only TRIED:
    taken, every reader inside re-enters it without waiting; contended, the
    continue is refused (and held for the Resume button). Never held across an
    await: ``continue_slot_turn`` runs no await inside the guard.
    """
    from kiro_crew.dashboard.chat_handlers import SlotContinueRefusal
    from kiro_crew.session_map import _MAP_LOCK

    if not _MAP_LOCK.acquire(blocking=False):
        raise SlotContinueRefusal("session map busy", "code_channel_resume_busy")
    try:
        yield
    finally:
        _MAP_LOCK.release()


async def _continue(
    state: Any,
    slot: Any,
    still_ok: Callable[[], str] | None = None,
    channel_origin: bool = False,
) -> str:
    """Continue *slot*'s interrupted turn; ``""`` when queued, else the refusal code.

    *still_ok* runs under the slot lock immediately before the queue insert
    (``before_dispatch``) and refuses on any answer but ``""``.
    """
    from kiro_crew.dashboard.chat_handlers import SlotContinueRefusal, continue_slot_turn

    def _recheck() -> None:
        why = still_ok() if still_ok is not None else ""
        if why:
            raise SlotContinueRefusal(why, "code_channel_resume_changed")

    async def _governance() -> None:
        # After the continue's last await: a policy revoked while it waited stops
        # the continuation here (the evaluation itself runs off the loop).
        if not await surface.outbound_permitted():
            raise SlotContinueRefusal("channels governance policy", "code_channel_resume_changed")

    try:
        body = await continue_slot_turn(
            state,
            slot,
            directive_user_origin=False,
            # A conductor's turns are the channel's: the resumed one keeps that
            # narrower authority, so the sender check still governs hand-off.
            directive_channel_origin=channel_origin,
            via="code_channel_resume",
            require_interrupted=True,
            prepare_dispatch=_governance,
            dispatch_guard=_session_map_untaken,
            before_dispatch=_recheck,
        )
    except SlotContinueRefusal as exc:
        return exc.code or "refused"
    except Exception:  # pragma: no cover - a failed continue is held back, not raised
        logger.warning("code channel resume: continue failed", exc_info=True)
        return "error"
    return "" if body == "resumed" else f"unexpected_{body}"


def _recheck_for(
    state: Any, channel_id: str, conductor: Any, slot: Any, turn: str
) -> Callable[[], str]:
    """The under-lock re-check for one continuation of *slot* (conductor or worker)."""
    key = str(getattr(slot, "key", "") or "")
    conductor_key = str(getattr(conductor, "key", "") or "")

    def _check() -> str:
        why = _admitted_now(state, channel_id, conductor)
        if why:
            return why
        if state.get_slot(key) is not slot:
            return "the session changed"
        if slot is not conductor:
            if not _is_own_worker(state, conductor_key, slot):
                return "the worker changed"
            mirror = _worker_mirror(state, slot)
            if mirror:
                return f"worker mirror {mirror}"
        return _same_opening(slot, turn)

    return _check


async def _resume_slot(
    state: Any, conductor: Any, slot: Any, channel_id: str, report: ResumeReport
) -> None:
    """Resume one interrupted slot automatically, at most once per interrupted turn."""
    key = str(slot.key)
    turn = await _interrupted_turn(slot)
    if not turn:
        return
    report.turns[key] = turn
    resources = f"channel={channel_id} session={key} turn={turn}"
    if turn == UNKNOWN_TURN:
        surface.audit("system", OPERATION, "denied", resources, "turn identity unknown")
        report.hold(key, HELD_UNKNOWN)
        return
    record = await asyncio.to_thread(store().resume_record, key)
    if record is not None and record.get("turn") == turn:
        # Already tried for this very turn: its resumed run was interrupted again,
        # or the gateway went down during it. Never twice.
        surface.audit("system", OPERATION, "denied", resources, "already resumed once")
        report.hold(key, HELD_ALREADY)
        return
    try:
        await asyncio.to_thread(
            store().record_resume, key, {"turn": turn, "at": str(int(time.time()))}
        )
    except Exception:
        # No durable record, no automatic resume: the loop guard must exist first.
        surface.audit("system", OPERATION, "error", resources, "attempt not recorded")
        report.hold(key, HELD_UNRECORDED)
        return
    refused = await _continue(
        state,
        slot,
        _recheck_for(state, channel_id, conductor, slot, turn),
        channel_origin=slot is conductor,
    )
    if refused:
        surface.audit("system", OPERATION, "denied", resources, f"continue refused: {refused}")
        report.hold(key, HELD_REFUSED)
        return
    surface.audit("system", OPERATION, "allowed", f"{resources} result=resumed")
    report.resumed.append(key)


async def resume_channel(state: Any, binding: Binding) -> ResumeReport | None:
    """Resume one channel's interrupted work and post its notice; ``None`` if skipped."""
    from kiro_crew.slack.code_channels.binding import bound_slot

    channel = binding.channel_id
    conductor = bound_slot(state, binding)
    why = await _channel_admitted(state, channel, conductor)
    if not why and await surface.is_archived(binding):
        why = "archived"
    if why:
        surface.audit("system", OPERATION, "denied", f"channel={channel}", why)
        return None
    report = ResumeReport(channel_id=channel)
    await _resume_slot(state, conductor, conductor, channel, report)
    conductor_key = str(getattr(conductor, "key", "") or "")
    for worker_key in _own_workers(conductor_key):
        worker = state.get_slot(worker_key)
        if worker is None:
            # Gone with the process: nothing to continue, and the board says so.
            report.lost.append(worker_key)
            continue
        if not _is_own_worker(state, conductor_key, worker):
            surface.audit(
                "system",
                OPERATION,
                "denied",
                f"channel={channel} session={worker_key}",
                "not this channel's worker any more",
            )
            continue
        if _worker_mirror(state, worker) == MIRRORED:
            surface.audit(
                "system",
                OPERATION,
                "denied",
                f"channel={channel} session={worker_key}",
                "the worker mirrors to a channel",
            )
            continue
        await _resume_slot(state, conductor, worker, channel, report)
    report.lost.extend(k for k in report.held if k != conductor_key)
    if report.resumed or report.held:
        await _notify(state, binding, conductor, report)
    return report


async def resume_after_restart(state: Any) -> list[ResumeReport]:
    """The start-up pass over every bound code channel (see the module docstring)."""
    if not code_channels_on(live_config()):
        return []
    try:
        await asyncio.to_thread(store().load)
        bindings = await asyncio.to_thread(store().bindings)
    except Exception:
        logger.warning("code channel resume: store unavailable", exc_info=True)
        return []
    reports: list[ResumeReport] = []
    for binding in bindings:
        try:
            report = await resume_channel(state, binding)
        except Exception:
            logger.warning("code channel %s: resume failed", binding.channel_id, exc_info=True)
            continue
        if report is not None:
            reports.append(report)
    return reports


async def resume_when_ready(
    state: Any,
    connected: Callable[[], bool],
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Wait (bounded) for Slack to connect, then run :func:`resume_after_restart`."""
    for _ in range(int(CONNECT_WAIT_S)):
        if connected():
            await resume_after_restart(state)
            return
        await sleep(1.0)
    logger.info("code channel resume: Slack did not connect; skipped")


# ── the notice ─────────────────────────────────────────────────────────────


def _short_id(channel_id: str, signature: str) -> str:
    return hashlib.sha256(f"{channel_id}|{signature}".encode()).hexdigest()[:12]


def _signature(keys: list[str], turns: dict[str, str]) -> str:
    """Which sessions, at which interrupted turns, a notice is about (dedupe key)."""
    return ",".join(f"{k}@{turns.get(k, '')}" for k in sorted(keys))


def _button(prefix: str, label: str, channel_id: str, signature: str) -> dict[str, Any]:
    return {
        "type": "button",
        "action_id": f"{prefix}{_short_id(channel_id, signature)}",
        "value": json.dumps({"channel": channel_id}),
        "text": {"type": "plain_text", "text": label},
    }


def _view(
    channel_id: str,
    text: str,
    signature: str,
    *,
    resume: bool,
    stop: bool,
    context: str = "",
    resumed_line: str = "",
    held_line: str = "",
    resume_label: str = "",
    stop_label: str = "",
) -> list[dict[str, Any]]:
    """One notice: its line, its live buttons, and an optional context line.

    Each button's label names the sessions it acts on (*resume_label* /
    *stop_label*, built by :func:`_label`); without one it falls back to the verb.

    With *resumed_line* and *held_line* (a restart that both resumed and held
    work) each kind of work is named on its own line with its own button beside
    it, so neither button's target is left to guess.
    """
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if resumed_line and held_line:
        for line, live, prefix, label in (
            (resumed_line, stop, STOP_ACTION_PREFIX, stop_label or STOP_TEXT),
            (held_line, resume, RESUME_ACTION_PREFIX, resume_label or BUTTON_TEXT),
        ):
            row: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": line}}
            if live:
                row["accessory"] = _button(prefix, label, channel_id, signature)
            blocks.append(row)
        if context:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})
        return blocks
    buttons = []
    if resume:
        buttons.append(
            _button(RESUME_ACTION_PREFIX, resume_label or BUTTON_TEXT, channel_id, signature)
        )
    if stop:
        buttons.append(_button(STOP_ACTION_PREFIX, stop_label or STOP_TEXT, channel_id, signature))
    if buttons:
        blocks.append({"type": "actions", "elements": buttons})
    if context:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})
    return blocks


def notice_blocks(
    channel_id: str,
    signature: str,
    *,
    held: bool,
    trust: bool,
    reasons: list[str] | None = None,
    resumed: bool | None = None,
    resumed_line: str = "",
    held_line: str = "",
    resume_label: str = "",
    stop_label: str = "",
) -> list[dict]:
    """The restart notice: one line, the Trust note, and its buttons.

    Held work gets the owner's Resume button and says why it was held; work that
    resumed by itself gets the owner's Stop button, as the dashboard's Stop
    control would stop it. A restart that did both names each set of sessions on
    its own line, with its button beside it.
    """
    resumed = (not held) if resumed is None else resumed
    if held:
        text = held_text(list(reasons or []), some_resumed=resumed)
    else:
        text = AUTO_TEXT
    if trust:
        text = f"{text} {TRUST_NOTE}"
    return _view(
        channel_id,
        text,
        signature,
        resume=held,
        stop=resumed,
        resumed_line=resumed_line if held and resumed else "",
        held_line=held_line if held and resumed else "",
        resume_label=resume_label,
        stop_label=stop_label,
    )


def _label(verb: str, state: Any, keys: list[str]) -> str:
    """A button label naming what it acts on: ``Stop Billing retry fix``, ``Resume 2 sessions``."""
    if not keys:
        return verb
    who = _names(state, keys) if len(keys) == 1 else _sessions(len(keys))
    who = who.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")  # plain_text
    label = f"{verb} {who}"
    return label if len(label) <= 75 else label[:74] + "…"


def _session_links(state: Any, keys: list[str]) -> str:
    """Slack links to the sessions' dashboard tabs (titles when no link can be made)."""
    from kiro_crew.dashboard.session_control_parts.code_channel_exemption import (
        worker_dashboard_link,
    )

    parts = []
    for key in keys:
        name = _names(state, [key]).replace("|", "/")
        link = worker_dashboard_link(key)
        parts.append(f"<{link}|{name}>" if link else name)
    return ", ".join(parts)


def _names(state: Any, keys: list[str]) -> str:
    """The sessions' titles for a notice line (mrkdwn-escaped, bounded)."""
    titles = []
    for key in keys:
        slot = state.get_slot(key) if callable(getattr(state, "get_slot", None)) else None
        title = str(getattr(slot, "title", "") or key)
        titles.append(title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    text = ", ".join(titles)
    return text if len(text) <= 300 else text[:299] + "…"


async def _notify(state: Any, binding: Binding, conductor: Any, report: ResumeReport) -> None:
    """Settle the stale status and post the one notice, deduplicated by store record."""
    from kiro_crew.slack.code_channels import status

    channel = binding.channel_id
    resources = f"channel={channel}"
    held = bool(report.held)
    keys = report.held if held else report.resumed
    signature = _signature(keys, report.turns)
    notice_key = f"{_NOTICE_PREFIX}{channel}"
    previous = await asyncio.to_thread(store().resume_record, notice_key)
    if previous and previous.get("sig") == signature and previous.get("state") == "button":
        # This held set already has its button: a further restart posts none.
        surface.audit("system", f"{OPERATION}.notice", "denied", resources, "already posted")
        return
    slack = surface.slack()
    why = await _channel_admitted(state, channel, conductor)
    if slack is None or why:
        surface.audit("system", f"{OPERATION}.notice", "denied", resources, why or "no Slack")
        if held:
            _notify_dashboard(state, binding, report.held)
        return
    try:
        await status.settle_after_stop(channel)
    except Exception:  # pragma: no cover - decoration only
        logger.debug("code channel resume: status not settled", exc_info=True)
    trust = bool(getattr(conductor, "_trust", False))
    reasons = [report.why.get(k, "") for k in report.held]
    lines = {}
    if report.held and report.resumed:
        lines = {
            "resumed_line": RESUMED_LINE.format(names=_names(state, report.resumed)),
            "held_line": HELD_LINE.format(names=_names(state, report.held)),
        }
    labels = {
        "resume_label": _label(BUTTON_TEXT, state, report.held) if report.held else "",
        "stop_label": _label("Stop", state, report.resumed) if report.resumed else "",
    }
    blocks = notice_blocks(
        channel,
        signature,
        held=held,
        trust=trust,
        reasons=reasons,
        resumed=bool(report.resumed),
        **lines,
        **labels,
    )
    text = blocks[0]["text"]["text"]
    try:
        ts = str(await slack.post_blocks(channel, blocks, text) or "")
    except Exception:
        logger.warning("code channel %s: restart notice outcome unknown", channel, exc_info=True)
        ts = ""
    if not ts:
        surface.audit("system", f"{OPERATION}.notice", "error", resources, "not posted")
        if held:
            _notify_dashboard(state, binding, report.held)
        return
    record = {"sig": signature, "state": "button" if held else "auto", "notice_ts": ts}
    record["text"] = text
    record.update(lines)
    record.update({k: v for k, v in labels.items() if v})
    if report.resumed:
        # What the Stop button may stop: exactly the sessions this notice resumed,
        # each at the turn it resumed (``key@turn``), so a later turn is not stopped.
        record["keys"] = _signature(report.resumed, report.turns)
    try:
        await asyncio.to_thread(store().record_resume, notice_key, record)
    except Exception:
        # Posted but not recorded: its button will be refused as stale, so say so.
        logger.warning("code channel %s: restart notice not recorded", channel, exc_info=True)
        if held:
            _notify_dashboard(state, binding, report.held)
    surface.audit(
        "system",
        f"{OPERATION}.notice",
        "allowed",
        f"{resources} message={ts} held={len(report.held)} resumed={len(report.resumed)}",
    )


def _notify_dashboard(state: Any, binding: Binding, held: list[str]) -> None:
    """Tell the owner in the dashboard which held sessions to resume, with links."""
    notify = getattr(state, "notify", None)
    if not callable(notify) or not held:
        return
    from kiro_crew.dashboard.session_control_parts.code_channel_exemption import (
        worker_dashboard_link,
    )

    lines = []
    for key in held:
        link = worker_dashboard_link(key)
        slot = state.get_slot(key) if callable(getattr(state, "get_slot", None)) else None
        name = str(getattr(slot, "title", "") or key).replace("[", "(").replace("]", ")")
        lines.append(f"- [{name}]({link})" if link else f"- {name}")
    try:
        notify(
            "slack",
            "Code channel work to resume",
            "Kiro Crew restarted while a code channel was working, and its Resume "
            "button could not be offered in the channel. Open each session below and "
            "press Resume:\n" + "\n".join(lines),
            meta={"channel_id": binding.channel_id, "session_key": binding.session_key},
        )
    except Exception:  # pragma: no cover
        logger.debug("restart-resume dashboard notification failed", exc_info=True)


# ── the Resume button ──────────────────────────────────────────────────────


async def _ephemeral(channel: str, user: str, text: str) -> None:
    slack = surface.slack()
    if slack is None or not (channel and user):
        return
    try:
        await slack.post_ephemeral(channel, user, text)
    except Exception:
        logger.debug("code channel ephemeral failed", exc_info=True)


async def handle_resume_click(orch: Any, payload: dict[str, Any]) -> bool:
    """A Resume click on a restart notice; False when it is not ours to answer."""
    if not code_channels_on(live_config(orch)):
        return False
    from kiro_crew.slack.code_channels.binding import bound_slot
    from kiro_crew.slack.handler import is_owner

    channel = str((payload.get("channel") or {}).get("id") or "")
    user = str((payload.get("user") or {}).get("id") or "")
    msg_ts = str((payload.get("message") or {}).get("ts") or "")
    action = (payload.get("actions") or [{}])[0] or {}
    try:
        value = json.loads(str(action.get("value") or "{}"))
    except ValueError:
        value = {}
    resources = f"channel={channel} message={msg_ts}"
    team = str(
        (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id") or ""
    )
    if not surface.workspace_admitted(team):
        surface.audit(user, OPERATION, "denied", f"{resources} team_id={team}", "workspace")
        return True
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    if binding is None or not isinstance(value, dict) or value.get("channel") != channel:
        surface.audit(user, OPERATION, "denied", resources, "not a bound code channel")
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    if not is_owner(user):
        surface.audit(user, OPERATION, "denied", resources, "not the owner")
        await _ephemeral(channel, user, NOT_OWNER_TEXT.format(owner=surface.owner_name()))
        return True
    state = surface.dashboard_state()
    conductor = bound_slot(state, binding)
    why = await _channel_admitted(state, channel, conductor)
    if not why and await surface.is_archived(binding):
        why = "archived"
    if why:
        surface.audit(user, OPERATION, "denied", resources, why)
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    notice_key = f"{_NOTICE_PREFIX}{channel}"
    record = await asyncio.to_thread(store().resume_record, notice_key)
    if record is None or record.get("state") != "button" or record.get("notice_ts") != msg_ts:
        surface.audit(user, OPERATION, "denied", resources, "not the current restart notice")
        await _ephemeral(channel, user, STALE_TEXT)
        return True
    # Claimed atomically: of two deliveries of the same press, one wins. Only this
    # button's field is compared, so a Stop pressed meanwhile is not a stale press.
    claimed = await _update_notice(
        notice_key,
        msg_ts,
        lambda c: c.get("state") == "button",
        lambda c: {**c, "state": "pressed"},
    )
    if claimed is None:
        surface.audit(user, OPERATION, "denied", resources, "already pressed")
        await _ephemeral(channel, user, STALE_TEXT)
        return True
    resumed, refusals, approvals, refused_keys = await _resume_on_press(
        state, channel, conductor, user, resources
    )
    # Already running is not a failure: that work needs nothing from the button.
    running = [r for r in refusals if r == "slot_running"]
    refusals = [r for r in refusals if r != "slot_running"]
    if refusals or not (resumed or running):
        # Something could not run: give the button back so the owner can try
        # again (what did resume is no longer interrupted, so it is not re-run).
        released = await _update_notice(
            notice_key,
            msg_ts,
            lambda c: c.get("state") == "pressed",
            lambda c: {**c, "state": "button"},
        )
        reason = ", ".join(sorted({_in_words(r) for r in refusals})) or "nothing is interrupted"
        if resumed:
            text = PARTIAL_TEXT.format(
                done=_names(state, resumed),
                rest=_names(state, refused_keys) or "the rest",
                reason=reason,
            )
        else:
            text = NOTHING_TEXT.format(reason=reason)
        if approvals:
            # Waiting will not help: the owner has to answer the approval first.
            text = (PARTIAL_APPROVAL_TEXT if resumed else NOTHING_APPROVAL_TEXT).format(
                done=_names(state, resumed),
                rest=_names(state, refused_keys) or "the rest",
                links=_session_links(state, approvals),
            )
        if released is None:
            lost = RESUME_LOST_TEXT if resumed else NOTHING_LOST_TEXT
            text = lost.format(
                done=_names(state, resumed),
                rest=_names(state, refused_keys) or "the rest",
                reason=reason,
            )
            if approvals:
                text += " " + APPROVAL_LINKS_TEXT.format(links=_session_links(state, approvals))
        else:
            # The other button may have redrawn the notice while this one held its
            # claim: draw it again from the record, so the button offered is shown.
            await _redraw(channel, msg_ts, notice_key)
        await _ephemeral(channel, user, text)
        return True
    note = (PRESSED_TEXT if resumed else ALREADY_RUNNING_TEXT).format(user=user)
    await _update_notice(notice_key, msg_ts, lambda c: True, lambda c: {**c, "resume_note": note})
    await _redraw(channel, msg_ts, notice_key)
    return True


async def _resume_on_press(
    state: Any, channel: str, conductor: Any, user: str, resources: str
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Continue the channel's own interrupted sessions for the owner's press.

    ``(resumed, refusal codes, keys waiting on an approval, keys refused)``.
    """
    conductor_key = str(getattr(conductor, "key", "") or "")
    candidates: list[Any] = [conductor]
    for key in _own_workers(conductor_key):
        worker = state.get_slot(key) if state is not None else None
        if (
            worker is not None
            and _is_own_worker(state, conductor_key, worker)
            and _worker_mirror(state, worker) != MIRRORED
        ):
            candidates.append(worker)
    resumed: list[str] = []
    refusals: list[str] = []
    approvals: list[str] = []
    refused_keys: list[str] = []
    for slot in candidates:
        turn = await _interrupted_turn(slot)
        if not turn:
            continue
        key = str(slot.key)
        if turn != UNKNOWN_TURN:
            # Recorded BEFORE it runs, as the automatic path does: a later restart
            # then holds this turn for the button instead of resuming it again.
            try:
                await asyncio.to_thread(
                    store().record_resume, key, {"turn": turn, "at": str(int(time.time()))}
                )
            except Exception:
                surface.audit(
                    user,
                    OPERATION,
                    "error",
                    f"{resources} session={key} via=button",
                    "attempt not recorded",
                )
                refusals.append("attempt not recorded")
                refused_keys.append(key)
                continue
        refused = await _continue(
            state,
            slot,
            _recheck_for(state, channel, conductor, slot, turn),
            channel_origin=slot is conductor,
        )
        surface.audit(
            user,
            OPERATION,
            "denied" if refused else "allowed",
            f"{resources} session={key} via=button",
            f"continue refused: {refused}" if refused else "",
        )
        if refused:
            refusals.append(refused)
            if refused != "slot_running":
                refused_keys.append(key)
            if refused == "slot_approval_pending":
                approvals.append(key)
            continue
        resumed.append(key)
    return resumed, refusals, approvals, refused_keys


# ── the Stop button ────────────────────────────────────────────────────────


async def handle_stop_click(orch: Any, payload: dict[str, Any]) -> bool:
    """A Stop click on an automatic-resume notice; False when it is not ours to answer.

    The owner's "stop what the restart resumed", as the dashboard's Stop control:
    each session the notice recorded as resumed that is still the channel's own
    (its session, or one of its own workers), still on the turn the restart
    resumed, and still running is stopped through ``stop_slot_turn``. It counts
    only on the notice ts the store recorded and is claimed by compare-and-set
    (single use); when a stop fails the claim is given back so the owner can press
    again. Every decision is SEL-audited. It deliberately skips the channel
    admission checks Resume runs: stopping only reduces what runs, so an archived
    or wrapped-up channel's notice may still stop its own resumed work.
    """
    if not code_channels_on(live_config(orch)):
        return False
    from kiro_crew.slack.code_channels.binding import bound_slot
    from kiro_crew.slack.handler import is_owner

    channel = str((payload.get("channel") or {}).get("id") or "")
    user = str((payload.get("user") or {}).get("id") or "")
    msg_ts = str((payload.get("message") or {}).get("ts") or "")
    action = (payload.get("actions") or [{}])[0] or {}
    try:
        value = json.loads(str(action.get("value") or "{}"))
    except ValueError:
        value = {}
    resources = f"channel={channel} message={msg_ts}"
    team = str(
        (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id") or ""
    )
    if not surface.workspace_admitted(team):
        surface.audit(user, STOP_OPERATION, "denied", f"{resources} team_id={team}", "workspace")
        return True
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    if binding is None or not isinstance(value, dict) or value.get("channel") != channel:
        surface.audit(user, STOP_OPERATION, "denied", resources, "not a bound code channel")
        await _ephemeral(channel, user, STOP_NOT_ALLOWED_TEXT)
        return True
    if not is_owner(user):
        surface.audit(user, STOP_OPERATION, "denied", resources, "not the owner")
        await _ephemeral(channel, user, STOP_NOT_OWNER_TEXT.format(owner=surface.owner_name()))
        return True
    notice_key = f"{_NOTICE_PREFIX}{channel}"
    record = await asyncio.to_thread(store().resume_record, notice_key)
    if record is None or not _stop_live(record) or record.get("notice_ts") != msg_ts:
        surface.audit(user, STOP_OPERATION, "denied", resources, "not the current restart notice")
        await _ephemeral(channel, user, STOP_STALE_TEXT)
        return True
    # Claimed atomically: of two deliveries of the same press, one stops. Its own
    # field, so a notice that also offers Resume keeps that button's state.
    claimed = await _update_notice(
        notice_key, msg_ts, _stop_live, lambda c: {**c, "stop": "pressed"}
    )
    if claimed is None:
        surface.audit(user, STOP_OPERATION, "denied", resources, "already pressed")
        await _ephemeral(channel, user, STOP_STALE_TEXT)
        return True
    try:
        state = surface.dashboard_state()
        stopped, failed = await _stop_resumed(
            state, binding, bound_slot(state, binding), record, user
        )
    except Exception:
        logger.warning("code channel %s: Stop failed", channel, exc_info=True)
        stopped, failed = [], ["unexpected error"]
    if failed:
        # Something may still be running: give the button back so the owner can retry.
        released = await _update_notice(
            notice_key,
            msg_ts,
            lambda c: c.get("stop") == "pressed",
            lambda c: {k: v for k, v in c.items() if k != "stop"},
        )
        reason = ", ".join(sorted(set(failed)))
        surface.audit(user, STOP_OPERATION, "error", f"{resources} stopped={len(stopped)}", reason)
        if released is not None:
            await _redraw(channel, msg_ts, notice_key)
            await _ephemeral(channel, user, STOP_FAILED_TEXT.format(reason=reason))
        else:
            await _ephemeral(channel, user, STOP_LOST_TEXT.format(reason=reason))
        return True
    surface.audit(user, STOP_OPERATION, "allowed", f"{resources} stopped={len(stopped)}")
    note = (STOPPED_TEXT if stopped else STOPPED_IDLE_TEXT).format(user=user)
    await _update_notice(notice_key, msg_ts, lambda c: True, lambda c: {**c, "stop_note": note})
    await _redraw(channel, msg_ts, notice_key)
    return True


async def _update_notice(
    notice_key: str,
    notice_ts: str,
    check: Callable[[dict[str, str]], bool],
    change: Callable[[dict[str, str]], dict[str, str]],
) -> dict[str, str] | None:
    """Compare-and-set the notice record, re-reading it on each try.

    Both buttons of one notice write the same record, each its own field, so a
    write never replaces what the other button set meanwhile. ``None`` when the
    record no longer belongs to *notice_ts* (a newer notice replaced it), *check*
    fails on the current record, or every retry lost a race.
    """
    for _ in range(3):
        current = await asyncio.to_thread(store().resume_record, notice_key)
        if current is None or current.get("notice_ts") != notice_ts or not check(current):
            return None
        updated = change(current)
        if await asyncio.to_thread(store().replace_resume_if, notice_key, current, updated):
            return updated
    return None


#: One redraw at a time per notice, so two buttons' redraws cannot interleave a
#: read of the record with another's message update. Bound to the running loop.
_redraw_locks: dict[str, tuple[Any, asyncio.Lock]] = {}


def _redraw_lock(notice_key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    held = _redraw_locks.get(notice_key)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        _redraw_locks[notice_key] = held
    return held[1]


async def _redraw(channel: str, msg_ts: str, notice_key: str) -> None:
    """Draw the notice from its CURRENT record: live buttons, each press's note."""
    slack = surface.slack()
    if slack is None or not msg_ts:
        return
    async with _redraw_lock(notice_key):
        current = await asyncio.to_thread(store().resume_record, notice_key)
        if current is None or current.get("notice_ts") != msg_ts:
            return
        posted = str(current.get("text") or "") or AUTO_TEXT
        notes = [current.get("resume_note", ""), current.get("stop_note", "")]
        view = _view(
            channel,
            posted,
            str(current.get("sig") or ""),
            resume=current.get("state") == "button",
            stop=_stop_live(current),
            context=" ".join(n for n in notes if n),
            resumed_line=str(current.get("resumed_line") or ""),
            held_line=str(current.get("held_line") or ""),
            resume_label=str(current.get("resume_label") or ""),
            stop_label=str(current.get("stop_label") or ""),
        )
        try:
            await slack.update_message(channel, msg_ts, posted, view)
        except Exception:
            logger.debug("code channel resume notice not updated", exc_info=True)


def _stop_live(record: dict[str, str]) -> bool:
    """*record*'s notice still offers Stop: it resumed something, not yet stopped."""
    return (
        bool(record.get("keys"))
        and record.get("stop") != "pressed"
        and record.get("state") in ("auto", "button", "pressed")
    )


def _resumed_turns(record: dict[str, str]) -> list[tuple[str, str]]:
    """``(key, turn)`` for each session *record*'s notice resumed."""
    pairs = []
    for item in str(record.get("keys") or "").split(","):
        key, _, turn = item.rpartition("@")
        if key:
            pairs.append((key, turn))
    return pairs


async def _stop_resumed(
    state: Any, binding: Binding, conductor: Any, record: dict[str, str], user: str
) -> tuple[list[str], list[str]]:
    """Stop each session *record* resumed that is still the channel's own, still on
    the turn the restart resumed, and running; ``(stopped, failures)``."""
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    channel = binding.channel_id
    stopped: list[str] = []
    failed: list[str] = []
    for key, turn in _resumed_turns(record):
        resources = f"channel={channel} session={key}"
        slot = state.get_slot(key) if state is not None else None
        if slot is None:
            continue
        if key == binding.session_key:
            own = slot is conductor and _is_the_channels_session(state, binding, slot)
        else:
            own = _is_own_worker(state, binding.session_key, slot)
        if not own:
            surface.audit(
                user, STOP_OPERATION, "denied", resources, "not this channel's session any more"
            )
            continue
        newer = _same_opening(slot, turn)
        if newer:
            # Stop means "what the restart resumed": a later turn is the owner's own.
            surface.audit(user, STOP_OPERATION, "denied", resources, newer)
            continue
        if not getattr(slot, "running", False):
            surface.audit(user, STOP_OPERATION, "allowed", f"{resources} result=idle")
            continue
        try:
            result = await stop_slot_turn(state, slot, source="slack", escalate=False)
        except Exception:
            logger.warning("code channel %s: resumed session not stopped", channel, exc_info=True)
            surface.audit(user, STOP_OPERATION, "error", resources, "stop failed")
            failed.append("a session did not stop")
            continue
        if (result or {}).get("compacting"):
            # Declined: the session is compacting its context and is still running.
            surface.audit(user, STOP_OPERATION, "error", resources, "declined while compacting")
            failed.append("it is compacting its context")
            continue
        info = str((result or {}).get("info") or "stopping")
        surface.audit(user, STOP_OPERATION, "allowed", f"{resources} result={info}")
        stopped.append(key)
    return stopped, failed
