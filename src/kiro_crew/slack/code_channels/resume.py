"""Resume code channels that a gateway restart interrupted.

A code channel's binding survives a restart (``slack-code-channels.json``), so the
next message already reaches the same session. What did not survive is a turn
that was running: nothing picks it up, and the channel still shows Slack's
"Working..." loader. :func:`resume_after_restart` runs once per gateway start,
after the memory barrier and once Slack is connected.

**Whose work.** Only a channel whose binding, live Slack-link index and slot all
agree it is the channel's session (the dispatch exemption's strict identity), in
a channel that is admitted right now: not archived (asked of Slack itself once the
channel has interrupted work, since an archive made while the gateway was offline
sent no event; an unreadable answer holds the work for Resume), fenced, stopped or
wrapped up, in an admitted workspace, admitted by the per-channel policy ceiling
(``allowed_channel_ids``, as Archive and every inbound path apply it), let in by
its channel and origin activation and by channels governance. The Resume and Stop
buttons apply the same ceiling. Then that session, and each of ITS OWN workers -- recorded
by the store at birth or listed by the crew log's session tree, whose live
``created_by`` still names it and which is linked to no channel. Nothing else.

**Automatic, once.** Each turn the restart interrupted is continued through the
Resume button's own path (``continue_slot_turn(require_interrupted=True)``: the
fixed "resumed" body, no prompt text). Which turn that is comes only from what the
dispatcher produced, never from how a row looks (see "which turn" below): the
turn marker the dispatcher wrote at admission, kept by the restore, names the row
the turn was started from. Before the continue, that turn is recorded in the
write-protected code-channel store. A recorded turn is never resumed
automatically again, so a turn that takes the gateway down cannot loop; a turn
whose identity cannot be named is never resumed automatically; a store that
cannot be written means no automatic resume. Identity and admission are
re-checked under the slot lock immediately before the continuation is queued. The
channel's stale "Working..." status is settled before the conductor's turn is
continued, so the status that turn sets as it starts is the one left showing.

**One notice**, top level: "I resumed where I
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
posted again unless that restart resumed something, whose Stop the new notice
carries; when a notice cannot be posted or recorded, the owner is told in
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
import uuid
import weakref
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

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

AUTO_TEXT = "Kiro Crew restarted while I was working; I resumed where I left off."
HELD_TEXT = (
    "Kiro Crew restarted while I was working. "
    "I've paused this channel's work until you press Resume, because {reason}."
)
HELD_SOME_TEXT = (
    "Kiro Crew restarted while I was working. I resumed part of this channel's "
    "work and paused the rest until you press Resume, because {reason}."
)
RESUMED_LINE = "*Resumed:* {names}"
HELD_LINE = "*Paused:* {names}"
#: Why a session was held for the button rather than resumed automatically.
HELD_ALREADY = "this work already restarted itself once and was cut off again"
HELD_UNKNOWN = "I couldn't tell which turn was interrupted"
HELD_UNRECORDED = "I couldn't save the record that this turn was resumed"
HELD_REFUSED = "the work couldn't be restarted right now"
HELD_UNCONFIRMED = "Slack couldn't confirm this channel is still open"
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
    "not started": "it is queued but could not start yet (a Pause all, or the gateway is busy)",
    "slot_stopping": "a stop is in progress",
    "slot_queue_pending": "it is still finishing an earlier reply",
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
NOTHING_TEXT = "Nothing could be resumed right now ({reason}). Try Resume again in a minute."
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
    "Nothing could be resumed yet: an action is waiting for your approval in {links} "
    "(open it to approve or decline), then press Resume."
)
PARTIAL_APPROVAL_TEXT = (
    "Resumed {done}. An action is waiting for your approval in {links} "
    "(open it to approve or decline), then press Resume."
)
#: Under a notice's live buttons: who may press them, so a member who sees work
#: running knows who can stop it.
OWNER_ONLY_TEXT = {
    (True, True): "Only {owner} can resume or stop this.",
    (True, False): "Only {owner} can resume this.",
    (False, True): "Only {owner} can stop this.",
}
PARTIAL_TEXT = (
    "Resumed {done}; {rest} could not be resumed right now ({reason}). "
    "The Resume button now resumes only {rest}: press it again in a minute."
)
NOT_OWNER_TEXT = ":no_entry: Only {owner} can resume this channel's work."
#: The notice's line once its buttons could not be registered (and were removed).
UNREGISTERED_TEXT = (
    "I couldn't save this notice, so its buttons were removed. Resume or stop this "
    "work from its session in the dashboard."
)
STALE_TEXT = "That Resume button is out of date."
NOT_ALLOWED_TEXT = ":no_entry: You can't resume work from this channel."
UNCONFIRMED_TEXT = (
    ":hourglass: Slack couldn't confirm this channel is still open, so nothing was "
    "resumed. Press Resume again in a moment."
)
#: The refusal :func:`_archive_refusal` gives when Slack's answer can't be had.
ARCHIVE_UNREADABLE = "whether the channel is archived could not be read from Slack"

#: How long the start-up pass waits for Slack to connect before giving up.
CONNECT_WAIT_S = 120.0
#: What :func:`_interrupted_turn` answers for an interrupted turn whose identity
#: the dispatcher's evidence cannot name. Never resumed automatically: missing
#: identity is not a new turn.
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
    #: :func:`_dispatch_token` of each resumed key's resumed run.
    dispatches: dict[str, str] = field(default_factory=dict)

    def hold(self, key: str, reason: str) -> None:
        self.held.append(key)
        self.why[key] = reason


def held_text(reasons: list[str], *, some_resumed: bool = False) -> str:
    """:data:`HELD_TEXT` naming each distinct reason, in the order first seen."""
    distinct = list(dict.fromkeys(r for r in reasons if r)) or [HELD_REFUSED]
    template = HELD_SOME_TEXT if some_resumed else HELD_TEXT
    return template.format(reason=" and ".join(distinct))


# ── which turn: the dispatcher's evidence, never a row's look ──────────────
#
# A turn's identity is read only from what the dispatcher produced, never from
# how a transcript row looks (a steer, a message queued behind a running turn and
# a replayed row all LOOK like openers and open nothing):
#
# * in this process, the slot's ``_turn_generation``, which moves exactly when a
#   turn is dispatched (``slot.task`` is set), read when a turn is resumed or held;
# * across a restart, the turn marker the dispatcher wrote at admission, naming the
#   row the turn was started from (``_restored_turn_marker``, kept by the restore).
#
# Every transcript row is writable by the session's own agent, so a row's fields
# never decide that a turn may run again on its own. A resume this module makes
# records, in the code-channel store, the ``ts`` and ``mid`` of the recovery row
# it dispatched (read from that row in this process's memory). Across a restart,
# while such a record exists, the cut opener counts as a new turn only when the
# recorded row is still in the window as written and the opener comes after it;
# a recorded row that is missing or altered, an opener at or before it, and any
# continuing row the store does not name read as unknown or as the recorded
# turn, both of which are held for the button, never resumed automatically.

#: Row meta on a recovery row of a resume made here: the turn it continues, for a
#: reader of the transcript only (never read back as an identity).
RESUMED_TURN_META = "code_channel_resumed_turn"
#: Row meta on that recovery row: the attempt it belongs to, so this process can
#: find the row it dispatched in its own memory and record where it is.
RESUME_ATTEMPT_META = "code_channel_resume_attempt"
#: This process: a generation counts dispatches in one process only.
_BOOT = uuid.uuid4().hex[:12]
#: One id per slot OBJECT: a slot reopened under the same key starts its
#: generation at 0 again, so the generation alone would match an old token.
_INCARNATIONS: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()


def _dispatch_token(slot: Any, *, lineage: bool = False) -> str:
    """The latest dispatch on this *slot* object in this process.

    *lineage* names the TURN that dispatch belongs to instead: a retry the turn
    queued for itself (a transient error, a reconnect) is the same turn, so the
    notice's Stop still reaches it.
    """
    try:
        incarnation = _INCARNATIONS.setdefault(slot, uuid.uuid4().hex[:12])
    except TypeError:  # not weak-referenceable (a stub): its identity is enough
        incarnation = f"id{id(slot)}"
    generation = int(getattr(slot, "_turn_generation", 0) or 0)
    if lineage and callable(getattr(slot, "_turn_lineage_generation", None)):
        generation = int(slot._turn_lineage_generation())
    return f"{_BOOT}:{incarnation}:{generation}"


def _restart_turn(
    slot: Any, record: dict[str, str] | None = None, cut: dict[str, str] | None = None
) -> str:
    """The identity of the turn the restart cut on *slot*: ``""`` when it cut none.

    ``""`` too once a turn has been dispatched in this process: whatever the
    restart cut has been superseded. Read from the code-channel store: *cut* is
    the opener the last process recorded at the cut turn's admission
    (:mod:`dispatch_record`), *record* the slot's resume record. The restored
    marker only says a turn was cut and which opener; it must name that same
    recorded opener (a later turn no process recorded leaves an older record that
    does not match), so a marker can only make the restart hold, and no
    transcript row names the turn.

    :data:`UNKNOWN_TURN` (held for the button) when the store recorded no opener,
    or the cut turn is a continuation it does not name as a resume made here; the
    recorded resume's turn when the cut turn is that resume's own recovery row or
    the turn it resumed; else the opener's ``ts``, a turn not resumed before.
    """
    from kiro_crew.dashboard.state import _TURN_CONTINUING_INJECT_KINDS

    if getattr(slot, "_turn_generation", 0):
        return ""
    marker = getattr(slot, "_restored_turn_marker", None)
    if not isinstance(marker, dict) or not marker.get("generation"):
        return ""
    ts = str((cut or {}).get("ts") or "")
    mid = str((cut or {}).get("mid") or "")
    if not ts or (str(marker.get("ts") or ""), str(marker.get("mid") or "")) != (ts, mid):
        # No recorded opener, or it is not the turn the marker says was cut (a
        # later turn this process did not record): nothing names the cut turn.
        return UNKNOWN_TURN
    if record is not None:
        resumed_row = (str(record.get("row_ts") or ""), str(record.get("row_mid") or ""))
        if (ts, mid) == resumed_row or ts == record.get("turn"):
            # That resume's own run, or the turn it resumed: tried once already.
            return str(record.get("turn") or UNKNOWN_TURN)
    if (cut or {}).get("role") == "inject" and (cut or {}).get("kind") in (
        _TURN_CONTINUING_INJECT_KINDS
    ):
        # A continuation the store does not name (the dashboard's Resume, a replay).
        return UNKNOWN_TURN
    return ts


async def _interrupted_turn(slot: Any, *, pressed: bool = False) -> str:
    """The identity of *slot*'s interrupted turn; ``""`` when there is none to resume.

    Nothing while a turn is running now. The automatic pass resumes only a turn
    the restart cut (:func:`_restart_turn`). The owner's press also continues any
    other interrupted turn, as the dashboard's Resume does, under
    :data:`UNKNOWN_TURN`: its record names the recovery row it dispatched, so a
    later restart that cuts that row holds it rather than resuming it.
    """
    from kiro_crew.dashboard.state import is_turn_interrupted, load_window_off_loop
    from kiro_crew.slack.code_channels import dispatch_record

    if _busy(slot):
        return ""
    await load_window_off_loop(slot)
    if _busy(slot):
        return ""
    messages = list(getattr(slot, "messages", None) or [])
    if not is_turn_interrupted(messages):
        return ""
    key = str(slot.key)
    try:
        await dispatch_record.taken()
        record = await asyncio.to_thread(store().resume_record, key)
    except Exception:
        # Unreadable: nothing can name the cut turn.
        logger.warning("code channel resume: records for %s unreadable", key, exc_info=True)
        return UNKNOWN_TURN
    turn = _restart_turn(slot, record, dispatch_record.opener(key))
    return turn or (UNKNOWN_TURN if pressed else "")


async def _record_row(slot: Any, key: str, attempt: dict[str, str]) -> None:
    """Add to *key*'s record *attempt* where its recovery row is, read from memory.

    The row the continue dispatched carries :data:`RESUME_ATTEMPT_META`; its
    ``ts`` and ``mid`` are read off this process's own copy, never the file. Left
    as it was when the row cannot be found or the store refuses: a record with no
    row holds every later cut on this slot for the button, which is safe.
    """
    for row in reversed(list(getattr(slot, "messages", None) or [])):
        meta = row.get("meta")
        if isinstance(meta, dict) and meta.get(RESUME_ATTEMPT_META) == attempt["id"]:
            ts, mid = str(row.get("ts") or ""), str(meta.get("mid") or "")
            if ts and mid:
                where = {**attempt, "row_ts": ts, "row_mid": mid}
                try:
                    await asyncio.to_thread(store().replace_resume_if, key, attempt, where)
                except Exception:
                    logger.warning("code channel resume: row for %s not recorded", key)
            return


def _busy(slot: Any) -> bool:
    """A turn holds *slot* now: its own (``running``) or its linked session's."""
    return bool(getattr(slot, "running", False) or getattr(slot, "linked_turn_running", False))


def _same_dispatch(slot: Any, token: str) -> str:
    """``""`` while no turn has been dispatched on *slot* since *token* was read."""
    return "" if _dispatch_token(slot) == token else "a newer turn started"


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
    from kiro_crew.session_map_store import _MAP_LOCK

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


def _admitted_now(state: Any, channel_id: str, conductor: Any, *, caller: str = "system") -> str:
    """Why *channel_id* may not be resumed now; ``""`` when it may. No disk access.

    The live binding (archived, wrapped up), this process's archive fence and Stop,
    the workspace, the per-channel policy ceiling (``allowed_channel_ids``), the
    channel and origin activation, and the conductor's strict identity. Run before
    work starts, again right before anything is recorded, and under the slot lock
    right before a continuation is queued. Every caller audits the reason it
    returns, so the policy seam writes no second row (*caller* names who asked).
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
    if surface.channel_policy_refusal(channel_id, operation=OPERATION, caller=caller, audit=False):
        return CHANNEL_POLICY_REFUSAL
    origin = _origin_of(channel_id, binding, None)
    if not channel_and_origin_permit(live_config(), channel_id, origin):
        return "channel or origin does not admit the bot"
    if not _is_the_channels_session(state, binding, conductor):
        return "not the channel's session"
    return ""


#: Why :func:`_admitted_now` refuses a channel the per-channel policy refuses.
CHANNEL_POLICY_REFUSAL = "the channel policy refuses this channel"


async def _channel_admitted(
    state: Any, channel_id: str, conductor: Any, *, caller: str = "system"
) -> str:
    """:func:`_admitted_now` plus channels governance (an await)."""
    why = _admitted_now(state, channel_id, conductor, caller=caller)
    if why:
        return why
    if not await surface.outbound_permitted():
        return "channels governance policy"
    return ""


async def _archive_refusal(binding: Binding) -> str:
    """Why the channel counts as archived now; ``""`` only when Slack says it is not.

    The stored stamp and this process's fence first; then Slack itself
    (:func:`lifecycle.reconcile_archived`), since an archive made while the
    gateway was offline left neither. A confirmed archive is recorded there for
    every later check; an answer that cannot be had refuses.
    """
    from kiro_crew.slack.code_channels import lifecycle

    if await surface.is_archived(binding):
        return "archived"
    seen = await lifecycle.reconcile_archived(binding)
    if seen is True:
        return "archived"
    if seen is None:
        return ARCHIVE_UNREADABLE
    return ""


class _LiveCheck:
    """:func:`_archive_refusal` for one channel, asked at most once per pass."""

    def __init__(self, binding: Binding) -> None:
        self._binding = binding
        self.why: str | None = None

    async def __call__(self) -> str:
        if self.why is None:
            self.why = await _archive_refusal(self._binding)
        return self.why


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
    from kiro_crew.session_map_store import _MAP_LOCK

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
    allow_restored_queue: bool = False,
    resumed_turn: str = UNKNOWN_TURN,
    attempt_id: str = "",
) -> str:
    """Continue *slot*'s interrupted turn; ``""`` when queued, else the refusal code.

    *still_ok* runs under the slot lock immediately before the queue insert
    (``before_dispatch``) and refuses on any answer but ``""``.
    *allow_restored_queue* is the owner's press only, as the dashboard's Resume:
    a queue restored from before the restart then does not refuse the resume (its
    cards stay queued behind it). The automatic pass leaves it off, so a restored
    card waits for a person. *resumed_turn* rides the recovery row for a reader
    (:data:`RESUMED_TURN_META`), and *attempt_id* (:data:`RESUME_ATTEMPT_META`)
    lets :func:`_record_row` find that row in memory once it is dispatched.
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
            allow_restored_queue=allow_restored_queue,
            prepare_dispatch=_governance,
            dispatch_guard=_session_map_untaken,
            before_dispatch=_recheck,
            extra_meta={
                RESUMED_TURN_META: resumed_turn,
                **({RESUME_ATTEMPT_META: attempt_id} if attempt_id else {}),
            },
        )
    except SlotContinueRefusal as exc:
        return exc.code or "refused"
    except Exception:  # pragma: no cover - a failed continue is held back, not raised
        logger.warning("code channel resume: continue failed", exc_info=True)
        return "error"
    return "" if body == "resumed" else f"unexpected_{body}"


def _recheck_for(
    state: Any, channel_id: str, conductor: Any, slot: Any, seen: str
) -> Callable[[], str]:
    """The under-lock re-check for one continuation of *slot* (conductor or worker).

    *seen* is :func:`_dispatch_token` when the interrupted turn was read: a turn
    dispatched since then refuses (``a newer turn started``)."""
    key = str(getattr(slot, "key", "") or "")
    conductor_key = str(getattr(conductor, "key", "") or "")

    def _check() -> str:
        # Under the slot lock: the caller audits a refusal ("continue refused").
        why = _admitted_now(state, channel_id, conductor)
        if why:
            return why
        if state.get_slot(key) is not slot:
            return "the session changed"
        from kiro_crew import crew_pause

        if crew_pause.holds_slot(slot):
            # "Pause all" holds this session: the drain would queue the recovery
            # and start nothing, so the work stays held for the button.
            return "its crew is paused"
        if slot is not conductor:
            if not _is_own_worker(state, conductor_key, slot):
                return "the worker changed"
            mirror = _worker_mirror(state, slot)
            if mirror:
                return f"worker mirror {mirror}"
        return _same_dispatch(slot, seen)

    return _check


async def _settle_status(channel_id: str) -> None:
    """Move the channel's native status off a ``processing`` the restart left behind."""
    from kiro_crew.slack.code_channels import status

    try:
        await status.settle_after_stop(channel_id)
    except Exception:  # pragma: no cover - decoration only
        logger.debug("code channel resume: status not settled", exc_info=True)


#: :func:`_moved_on`'s answers: a person's newer message took the session (take
#: the attempt back), or the interrupted turn itself is running again by another
#: path (keep the attempt: it is that turn's loop guard). Neither is held.
MOVED_ON_NEWER = "newer"
MOVED_ON_RUNNING = "running"


def _moved_on(slot: Any, seen: str, refused: str) -> str:
    """Why a refused continue is not held work (``""`` when it is held).

    Only a turn DISPATCHED since the read (*seen*) moves the work on, judged by
    the row the dispatcher started it from (``_turn_in_flight_prompt``): a
    person's message takes the session (:data:`MOVED_ON_NEWER`), a continuation
    is the interrupted turn running again by another path
    (:data:`MOVED_ON_RUNNING`). Anything else (a monitor wake, a cron run, an
    opener not yet recorded) leaves the interrupted work held.
    """
    from kiro_crew.dashboard.state import _TURN_CONTINUING_INJECT_KINDS

    if refused not in ("slot_running", "code_channel_resume_changed"):
        return ""
    if not _same_dispatch(slot, seen):
        return ""
    opener = getattr(slot, "_turn_in_flight_prompt", None)
    if not isinstance(opener, dict):
        return ""
    if opener.get("role") == "user":
        return MOVED_ON_NEWER
    raw = opener.get("meta")
    meta: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if opener.get("role") == "inject" and meta.get("injectKind") in _TURN_CONTINUING_INJECT_KINDS:
        return MOVED_ON_RUNNING
    return ""


async def _take_back(key: str, attempt: dict[str, str], previous: dict[str, str] | None) -> None:
    """Restore *key*'s resume record to *previous* when it still holds *attempt*."""
    try:
        await asyncio.to_thread(store().replace_resume_if, key, attempt, previous)
    except Exception:
        # Kept: a later restart then holds this turn for the button, which is safe.
        logger.warning("code channel resume: attempt for %s not taken back", key, exc_info=True)


async def _resume_slot(
    state: Any,
    conductor: Any,
    slot: Any,
    channel_id: str,
    report: ResumeReport,
    live: _LiveCheck,
) -> None:
    """Resume one interrupted slot automatically, at most once per interrupted turn."""
    key = str(slot.key)
    turn = await _interrupted_turn(slot)
    if not turn:
        return
    seen = _dispatch_token(slot)
    # Asked of Slack only once some work is interrupted, and once per channel.
    unconfirmed = await live()
    if unconfirmed == "archived":
        return
    if unconfirmed:
        # Can't tell whether the channel is still open: nothing runs by itself,
        # and a Resume press asks Slack again.
        surface.audit(
            "system", OPERATION, "denied", f"channel={channel_id} session={key}", unconfirmed
        )
        if slot is conductor and not getattr(slot, "running", False):
            await _settle_status(channel_id)  # the notice says paused, not "Working..."
        report.turns[key] = turn
        report.hold(key, HELD_UNCONFIRMED)
        return
    # Admission again after Slack was asked (an await): a channel the policy or a
    # gate dropped meanwhile gets no status change and no attempt record.
    why = _admitted_now(state, channel_id, conductor)
    if why:
        surface.audit("system", OPERATION, "denied", f"channel={channel_id} session={key}", why)
        return
    report.turns[key] = turn
    if slot is conductor and not getattr(slot, "running", False):
        # The interrupted turn left the channel showing "Working...". Clear it
        # BEFORE any continuation: a resumed turn sets its own status as it
        # starts, and a settle after that would overwrite it with ``active``. A
        # turn already running (a message arrived first) owns the status.
        await _settle_status(channel_id)
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
    # And once more after the status and record awaits, right before the record:
    # a channel dropped meanwhile leaves no attempt that would hold its turn later.
    why = _admitted_now(state, channel_id, conductor)
    if why:
        surface.audit("system", OPERATION, "denied", resources, why)
        return
    if _busy(slot) or _same_dispatch(slot, seen):
        # A turn started during the awaits above: the one read is not the turn in
        # flight now, so it gets no attempt record.
        surface.audit("system", OPERATION, "denied", resources, "already running")
        return
    # ``attempt`` names this pass's write (``via``/``id``), so taking it back below
    # can never match a Resume press's record written in the same second.
    attempt = {
        "turn": turn,
        "at": str(int(time.time())),
        "via": "auto",
        "id": uuid.uuid4().hex[:16],
    }
    try:
        await asyncio.to_thread(store().record_resume, key, attempt)
    except Exception:
        # No durable record, no automatic resume: the loop guard must exist first.
        surface.audit("system", OPERATION, "error", resources, "attempt not recorded")
        report.hold(key, HELD_UNRECORDED)
        return
    refused = await _continue(
        state,
        slot,
        _recheck_for(state, channel_id, conductor, slot, seen),
        channel_origin=slot is conductor,
        resumed_turn=turn,
        attempt_id=attempt["id"],
    )
    moved = _moved_on(slot, seen, refused) if refused else ""
    if moved:
        # Nothing is held, so no button. A person's newer message took the
        # session: this pass ran nothing, so its attempt is taken back and a later
        # restart is not told this turn "already restarted once". The interrupted
        # turn running again by another path keeps the attempt as its loop guard.
        surface.audit("system", OPERATION, "denied", resources, f"moved on ({moved}): {refused}")
        if moved == MOVED_ON_NEWER:
            await _take_back(key, attempt, record)
        return
    if refused:
        surface.audit("system", OPERATION, "denied", resources, f"continue refused: {refused}")
        report.hold(key, HELD_REFUSED)
        return
    if not _same_dispatch(slot, seen):
        # Queued but nothing dispatched (a hold the drain honours): not running,
        # so the notice must not say it was picked up, and Stop has nothing to name.
        surface.audit("system", OPERATION, "denied", resources, "queued, not started")
        report.hold(key, HELD_REFUSED)
        return
    surface.audit("system", OPERATION, "allowed", f"{resources} result=resumed")
    report.resumed.append(key)
    # The dispatch this pass started (the drain sets ``slot.task`` before the
    # continue returns): what the notice's Stop may stop, and nothing later.
    report.dispatches[key] = _dispatch_token(slot, lineage=True)
    await _record_row(slot, key, attempt)


async def resume_channel(state: Any, binding: Binding) -> ResumeReport | None:
    """Resume one channel's interrupted work and post its notice; ``None`` if skipped."""
    from kiro_crew.slack.code_channels.binding import bound_slot

    channel = binding.channel_id
    conductor = bound_slot(state, binding)
    why = await _channel_admitted(state, channel, conductor)
    if why:
        surface.audit("system", OPERATION, "denied", f"channel={channel}", why)
        return None
    report = ResumeReport(channel_id=channel)
    live = _LiveCheck(binding)
    await _resume_slot(state, conductor, conductor, channel, report, live)
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
        await _resume_slot(state, conductor, worker, channel, report, live)
    if live.why == "archived":
        # Slack says the channel was archived (while the gateway was down, or
        # since): nothing was resumed or held, and no notice goes to it.
        surface.audit("system", OPERATION, "denied", f"channel={channel}", "archived")
        return None
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
    """Which sessions, at which interrupted turns, a notice is about.

    A JSON list of ``[session, turn]`` pairs. The turn is the ts the turn marker
    named, which a session can edit, so it is never joined into a delimited
    string: a ts carrying the delimiter would add a pair of its own.
    """
    return json.dumps([[k, str(turns.get(k, ""))] for k in sorted(keys)], separators=(",", ":"))


def _same_held(previous: str, keys: list[str], turns: dict[str, str]) -> bool:
    """Whether the held work is the set a live button already names.

    The same sessions, each at the same turn or at one this start cannot name: a
    start takes the last process's recorded openers out of the store, so held work
    a restart cut nothing new on reads as unknown on the next one. Only whether to
    post again rides on this; a Resume press checks every turn again.
    """
    try:
        pairs = json.loads(previous)
        named = {str(k): str(t) for k, t in pairs}
    except (TypeError, ValueError):
        return False
    if set(named) != set(keys):
        return False
    return all(turns.get(k, "") in (named[k], UNKNOWN_TURN) for k in keys)


def _resumed_signature(report: "ResumeReport") -> str:
    """The sessions *report* resumed, as JSON ``[session, turn, dispatch]`` triples."""
    return json.dumps(
        [
            [k, str(report.turns.get(k, "")), str(report.dispatches.get(k, ""))]
            for k in sorted(report.resumed)
        ],
        separators=(",", ":"),
    )


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
    who = OWNER_ONLY_TEXT.get((resume, stop), "")
    owner_block: dict[str, Any] | None = (
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": who.format(owner=surface.owner_name())}],
        }
        if who
        else None
    )
    if resumed_line and held_line:
        for line, live, prefix, label in (
            (resumed_line, stop, STOP_ACTION_PREFIX, stop_label or STOP_TEXT),
            (held_line, resume, RESUME_ACTION_PREFIX, resume_label or BUTTON_TEXT),
        ):
            row: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": line}}
            if live:
                row["accessory"] = _button(prefix, label, channel_id, signature)
            blocks.append(row)
        if owner_block:
            blocks.append(owner_block)  # under both rows, whose buttons it is about
        if context:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})
        return blocks
    if owner_block:
        blocks.append(owner_block)  # right under the line, above its buttons
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


def _trust_on(conductor: Any) -> bool:
    """The channel's session is in Trust, as the dashboard header reads it now."""
    from kiro_crew.slack.code_channels.approval_picker import current_mode

    return conductor is not None and current_mode(conductor) == "trust"


def _without_trust_note(text: str) -> str:
    return text.removesuffix(f" {TRUST_NOTE}")


async def _channel_trusted(channel: str) -> bool:
    """:func:`_trust_on` for *channel*'s bound session; False when it cannot be read."""
    from kiro_crew.slack.code_channels.binding import bound_slot

    try:
        binding = await asyncio.to_thread(store().get_binding, channel)
        if binding is None:
            return False
        return _trust_on(bound_slot(surface.dashboard_state(), binding))
    except Exception:
        logger.debug("code channel %s: approval mode not read", channel, exc_info=True)
        return False


async def _notify(state: Any, binding: Binding, conductor: Any, report: ResumeReport) -> None:
    """Post the one notice, deduplicated by store record."""
    channel = binding.channel_id
    resources = f"channel={channel}"
    held = bool(report.held)
    keys = report.held if held else report.resumed
    signature = _signature(keys, report.turns)
    notice_key = f"{_NOTICE_PREFIX}{channel}"
    previous = await asyncio.to_thread(store().resume_record, notice_key)
    if (
        previous
        and _same_held(str(previous.get("sig") or ""), keys, report.turns)
        and previous.get("state") == "button"
        and not report.resumed
    ):
        # This held set already has its button and nothing new resumed: a further
        # restart posts none. Anything resumed needs a fresh notice whose Stop
        # names it.
        surface.audit("system", f"{OPERATION}.notice", "denied", resources, "already posted")
        return
    slack = surface.slack()
    why = await _channel_admitted(state, channel, conductor)
    if slack is None or why:
        surface.audit("system", f"{OPERATION}.notice", "denied", resources, why or "no Slack")
        _notify_dashboard(state, binding, report.held, report.resumed)
        return
    trust = _trust_on(conductor)
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
        _notify_dashboard(state, binding, report.held, report.resumed)
        return
    record = {"sig": signature, "state": "button" if held else "auto", "notice_ts": ts}
    # Without the Trust note: each redraw adds it from the channel's mode THEN.
    record["text"] = _without_trust_note(text)
    record.update(lines)
    record.update({k: v for k, v in labels.items() if v})
    if report.resumed:
        # What the Stop button may stop: exactly the sessions this notice resumed,
        # each at the dispatch that resumed it (JSON ``[session, turn, dispatch]``
        # triples), so a later turn is not stopped.
        record["keys"] = _resumed_signature(report)
    try:
        await asyncio.to_thread(store().record_resume, notice_key, record)
    except Exception:
        # Posted but not recorded: every button on it would be refused as stale.
        # Take the buttons off and say where the work is controlled instead.
        logger.warning("code channel %s: restart notice not recorded", channel, exc_info=True)
        surface.audit(
            "system", f"{OPERATION}.notice", "error", f"{resources} message={ts}", "not recorded"
        )
        unregistered = f"{_without_trust_note(text)} {UNREGISTERED_TEXT}"
        # The same lines (what resumed, what is paused), with no buttons.
        kept = [
            {"type": "section", "text": {"type": "mrkdwn", "text": line}}
            for line in (lines.get("resumed_line", ""), lines.get("held_line", ""))
            if line
        ]
        try:
            await slack.update_message(
                channel,
                ts,
                unregistered,
                [{"type": "section", "text": {"type": "mrkdwn", "text": unregistered}}, *kept],
            )
        except Exception:
            logger.warning("code channel %s: restart notice not updated", channel, exc_info=True)
        _notify_dashboard(state, binding, report.held, report.resumed)
        return
    surface.audit(
        "system",
        f"{OPERATION}.notice",
        "allowed",
        f"{resources} message={ts} held={len(report.held)} resumed={len(report.resumed)}",
    )


def _session_link(key: str) -> str:
    """A link to *key*'s dashboard tab: the configured dashboard URL's (it also works
    from a phone), else the dashboard's own path, which the note's reader is already on."""
    from kiro_crew.dashboard.session_control_parts.code_channel_exemption import (
        worker_dashboard_link,
    )

    return worker_dashboard_link(key) or (f"/chat?sid={quote(key, safe='')}" if key else "")


def _notify_dashboard(
    state: Any, binding: Binding, held: list[str], resumed: list[str] | None = None
) -> None:
    """Tell the owner in the dashboard what the channel's buttons would have done.

    Held sessions are listed to resume; sessions that resumed on their own are
    listed to stop from their session, since no working Stop is in the channel.
    Each is a link to its session; one with no link says where to find it.
    """
    notify = getattr(state, "notify", None)
    resumed = list(resumed or [])
    if not callable(notify) or not (held or resumed):
        return

    def _lines(keys: list[str], verb: str) -> tuple[str, bool]:
        lines, linked = [], True
        for key in keys:
            link = _session_link(key)
            slot = state.get_slot(key) if callable(getattr(state, "get_slot", None)) else None
            name = str(getattr(slot, "title", "") or key).replace("[", "(").replace("]", ")")
            linked = linked and bool(link)
            lines.append(
                f"- [{name}]({link})" if link else f"- In the sidebar, find {name} and press {verb}"
            )
        return "\n".join(lines), linked

    parts = []
    if held:
        lines, linked = _lines(held, "Resume")
        parts.append(
            "Kiro Crew restarted while a code channel was working, and its Resume "
            "button could not be offered in the channel. "
            + ("Open each session below and press Resume:" if linked else "Resume each session:")
            + "\n"
            + lines
        )
    if resumed:
        lines, linked = _lines(resumed, "Stop")
        parts.append(
            "These sessions resumed their work on their own, and the channel has no "
            "working Stop button for them. "
            + ("To stop one, open it and press Stop:" if linked else "To stop one:")
            + "\n"
            + lines
        )
    try:
        notify(
            "slack",
            "Code channel work to resume" if held else "Code channel work resumed",
            "\n\n".join(parts),
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
    why = await _channel_admitted(state, channel, conductor, caller=user) or await _archive_refusal(
        binding
    )
    if why:
        surface.audit(user, OPERATION, "denied", resources, why)
        await _ephemeral(
            channel, user, UNCONFIRMED_TEXT if why == ARCHIVE_UNREADABLE else NOT_ALLOWED_TEXT
        )
        return True
    notice_key = f"{_NOTICE_PREFIX}{channel}"
    record = await asyncio.to_thread(store().resume_record, notice_key)
    if record is None or record.get("state") != "button" or record.get("notice_ts") != msg_ts:
        surface.audit(user, OPERATION, "denied", resources, "not the current restart notice")
        await _ephemeral(channel, user, STALE_TEXT)
        return True
    # Admission again, synchronously, after the Slack and store awaits: a channel
    # dropped meanwhile leaves no claim and no attempt record behind.
    why = _admitted_now(state, channel, conductor, caller=user)
    if why:
        surface.audit(user, OPERATION, "denied", resources, why)
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
    try:
        resumed, refusals, approvals, refused_keys = await _resume_on_press(
            state, channel, conductor, user, resources
        )
    except asyncio.CancelledError:
        # Cancelled mid-press: give the button back before going, so a later
        # press is not refused as already pressed. Best effort: with the loop's
        # executor already shut down the store write fails, and the button stays
        # pressed until the next restart posts a fresh notice.
        await _update_notice(
            notice_key,
            msg_ts,
            lambda c: c.get("state") == "pressed",
            lambda c: {**c, "state": "button"},
        )
        raise
    except Exception:
        logger.warning("code channel %s: Resume failed", channel, exc_info=True)
        surface.audit(user, OPERATION, "error", resources, "resume failed")
        resumed, refusals, approvals, refused_keys = [], ["error"], [], []
    # Already running is not a failure: that work needs nothing from the button.
    running = [r for r in refusals if r == "slot_running"]
    refusals = [r for r in refusals if r != "slot_running"]
    if refusals or not (resumed or running):
        # Something could not run: give the button back so the owner can try
        # again (what did resume is running again, so it is not re-run). The
        # button now names only what is still held.
        def _give_back(c: dict[str, str]) -> dict[str, str]:
            given = {**c, "state": "button"}
            if refused_keys:
                given["resume_label"] = _label(BUTTON_TEXT, state, refused_keys)
                if c.get("held_line"):
                    given["held_line"] = HELD_LINE.format(names=_names(state, refused_keys))
            return given

        released = await _update_notice(
            notice_key,
            msg_ts,
            lambda c: c.get("state") == "pressed",
            _give_back,
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
        turn = await _interrupted_turn(slot, pressed=True)
        if not turn:
            continue
        seen = _dispatch_token(slot)
        key = str(slot.key)
        # Admission again after the transcript read (an await), before anything is
        # recorded: a channel dropped meanwhile gets no attempt record.
        why = _admitted_now(state, channel, conductor)
        if why:
            surface.audit(user, OPERATION, "denied", f"{resources} session={key} via=button", why)
            refusals.append(why)
            refused_keys.append(key)
            continue
        if _busy(slot) or _same_dispatch(slot, seen):
            # Running again already (a message arrived first): nothing to record.
            surface.audit(
                user, OPERATION, "denied", f"{resources} session={key} via=button", "slot_running"
            )
            refusals.append("slot_running")
            continue
        # Recorded BEFORE it runs, as the automatic path does: a later restart then
        # holds this turn for the button instead of resuming it again. A turn whose
        # identity is unknown is recorded too, when the store takes it, so the row
        # it dispatches anchors the next restart; when it does not, the press still
        # runs, as the dashboard's Resume does.
        attempt = {
            "turn": turn,
            "at": str(int(time.time())),
            "via": "button",
            "id": uuid.uuid4().hex[:16],
        }
        try:
            await asyncio.to_thread(store().record_resume, key, attempt)
        except Exception:
            if turn == UNKNOWN_TURN:
                attempt = {}
            else:
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
            _recheck_for(state, channel, conductor, slot, seen),
            channel_origin=slot is conductor,
            # The owner's press, authenticated above: as the dashboard's Resume.
            allow_restored_queue=True,
            resumed_turn=turn,
            attempt_id=attempt.get("id", ""),
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
        if not _same_dispatch(slot, seen):
            # Queued but nothing dispatched (a hold the drain honours): held.
            surface.audit(
                user, OPERATION, "denied", f"{resources} session={key} via=button", "not started"
            )
            refusals.append("not started")
            refused_keys.append(key)
            continue
        resumed.append(key)
        if attempt:
            await _record_row(slot, key, attempt)
    return resumed, refusals, approvals, refused_keys


# ── the Stop button ────────────────────────────────────────────────────────


async def handle_stop_click(orch: Any, payload: dict[str, Any]) -> bool:
    """A Stop click on an automatic-resume notice; False when it is not ours to answer.

    The owner's "stop what the restart resumed", as the dashboard's Stop control:
    each session the notice recorded as resumed that is still the channel's own
    (its session, or one of its own workers), still on the dispatch the restart
    resumed (no turn dispatched since), and still running is stopped through
    ``stop_slot_turn``. It counts
    only on the notice ts the store recorded and is claimed by compare-and-set
    (single use); when a stop fails the claim is given back so the owner can press
    again. Every decision is SEL-audited. It deliberately skips the channel
    admission checks Resume runs: stopping only reduces what runs, so an archived
    or wrapped-up channel's notice may still stop its own resumed work. The
    per-channel policy ceiling still applies, as for every Slack action: a press
    in a channel the policy refuses is audited and changes nothing.
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
    if surface.channel_policy_refusal(channel, operation=STOP_OPERATION, caller=user):
        return True  # audited by the policy seam; nothing is claimed, stopped or posted
    notice_key = f"{_NOTICE_PREFIX}{channel}"
    record = await asyncio.to_thread(store().resume_record, notice_key)
    if record is None or not _stop_live(record) or record.get("notice_ts") != msg_ts:
        surface.audit(user, STOP_OPERATION, "denied", resources, "not the current restart notice")
        await _ephemeral(channel, user, STOP_STALE_TEXT)
        return True
    # The policy again after the store read, right before the claim and the stop.
    if surface.channel_policy_refusal(channel, operation=STOP_OPERATION, caller=user):
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
    record does not belong to *notice_ts* (a newer notice replaced it), *check*
    fails on the current record, every retry lost a race, or the store failed.
    """
    for _ in range(3):
        try:
            current = await asyncio.to_thread(store().resume_record, notice_key)
        except Exception:
            logger.warning(
                "code channel resume notice %s: record not read", notice_key, exc_info=True
            )
            return None
        if current is None or current.get("notice_ts") != notice_ts or not check(current):
            return None
        updated = change(current)
        try:
            replaced = await asyncio.to_thread(
                store().replace_resume_if, notice_key, current, updated
            )
        except Exception:
            logger.warning(
                "code channel resume notice %s: record not updated", notice_key, exc_info=True
            )
            return None
        if replaced:
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
        posted = _without_trust_note(str(current.get("text") or "") or AUTO_TEXT)
        if await _channel_trusted(channel):
            posted = f"{posted} {TRUST_NOTE}"
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
        bool(_resumed_turns(record))  # a record that names nothing shows no Stop
        and record.get("stop") != "pressed"
        and record.get("state") in ("auto", "button", "pressed")
    )


def _resumed_turns(record: dict[str, str]) -> list[tuple[str, str]]:
    """``(key, dispatch)`` for each session *record*'s notice resumed.

    Decoded from :func:`_resumed_signature`'s JSON. Anything else -- not JSON, not
    a list of three-string triples, a blank session or dispatch, a repeated session
    -- names nothing, so a Stop press stops nothing rather than a session the
    notice never resumed.
    """
    try:
        decoded = json.loads(str(record.get("keys") or ""))
    except ValueError:
        return []
    if not isinstance(decoded, list):
        return []
    pairs: list[tuple[str, str]] = []
    for item in decoded:
        if not (
            isinstance(item, list)
            and len(item) == 3
            and all(isinstance(part, str) for part in item)
            and item[0]
            and item[2]
        ):
            return []
        pairs.append((item[0], item[2]))
    if len({key for key, _ in pairs}) != len(pairs):
        return []
    return pairs


async def _stop_resumed(
    state: Any, binding: Binding, conductor: Any, record: dict[str, str], user: str
) -> tuple[list[str], list[str]]:
    """Stop each session *record* resumed that is still the channel's own, still on
    the dispatch the restart resumed, and running; ``(stopped, failures)``."""
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    channel = binding.channel_id
    stopped: list[str] = []
    failed: list[str] = []
    for key, dispatch in _resumed_turns(record):
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
        newer = "" if _dispatch_token(slot, lineage=True) == dispatch else "a newer turn started"
        if newer:
            # Stop means "what the restart resumed": a later turn is the owner's own.
            surface.audit(user, STOP_OPERATION, "denied", resources, newer)
            continue
        # The resumed run's own task, not ``running``: a later send's admission
        # reservation reads as running before its dispatch moves the generation.
        if not getattr(slot, "turn_running", False):
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
