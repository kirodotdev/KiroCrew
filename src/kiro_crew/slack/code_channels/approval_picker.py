"""A code channel's approval mode, set from Slack.

A code channel's session starts on ``slack.code_channel_defaults.approval_mode``
(:mod:`.binding`). From the channel, the owner can put it on Trust with
**Trust this channel** on a tool-approval card the channel's session posted,
which approves that call too (the Trust change answers every call the session
is waiting on).

**One path.** Every change runs the dashboard picker's own mutation,
:func:`kiro_crew.dashboard.chat_handlers.apply_approval_mode`, on the channel's
slot -- the call ``POST /api/chat/mode`` makes after its caller checks, and the
one a crewmate profile write already makes. No approval store of its own: the
slot flags, the session policy a worker inherits, the pending-card sweep and the
``mode_change:<mode>`` SEL row are that function's. The row's caller names the
Slack user (``slack-code-channels:<user>``), and this module writes one more row
per decision (operation :data:`OPERATION`), refusals included.

**Who may change it.** Only Kiro Crew's owner, by identity (``is_owner``, never a
wider allow-list), in a channel and origin that still admit the bot, in the bound workspace, in a channel not archived --
the gates every code-channel command applies. Anyone else is told so
ephemerally, and nothing changes.

**Trust is said out loud.** A change to Trust posts one line in the channel:
tools run without prompts, for the session's workers too, running ones included
(every mode change reaches the workers it already runs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import string
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.dashboard.approval_inheritance import _RANK
from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import channel_session, code_channels_on, live_config
from kiro_crew.slack.code_channels.store import Binding, store

logger = logging.getLogger(__name__)

#: The approval card's "Trust this channel" button (routed by
#: ``slack/interactions.py``).
TRUST_CALL_ACTION_ID = "kc_code_channel_trust_call"

#: The characters of a slash-command name as Slack accepts one (what the
#: replies may echo).
_COMMAND_CHARS = frozenset(string.ascii_lowercase + string.digits + "_-")


def _is_command_name(name: str) -> bool:
    return 1 <= len(name) <= 32 and all(ch in _COMMAND_CHARS for ch in name)


AUDIT_CALLER = "slack-code-channels"
OPERATION = "slack.code_channel.approval_mode"

#: The kickoff select's action id (routed by ``slack/interactions.py``).
APPROVAL_SELECT_ACTION_ID = "kc_code_channel_approval"
#: Prefix of the select block's id; a fresh suffix per render makes Slack re-draw
#: the select from ``initial_option`` instead of keeping a refused pick.
APPROVAL_BLOCK_PREFIX = "kc_code_channel_approval."

#: The modes in picker order: (mode, label, ``/kirocrew approval`` word).
MODES: tuple[tuple[str, str, str], ...] = (
    ("normal", "Normal", "normal"),
    ("trust_reads", "Reads", "reads"),
    ("trust", "Trust", "trust"),
)
_LABELS = {mode: text for mode, text, _ in MODES}
#: The context line under the kickoff's select: what each mode does (a select's
#: options cannot carry a description in Block Kit).
MODES_NOTE = (
    "*Normal* looks things up on its own and asks for the rest · *Reads* also "
    "searches memory, past chats, skills and knowledge on its own · *Trust* runs "
    "everything without asking"
)
_BY_WORD = {word: mode for mode, _, word in MODES}


def _shown_mode(mode: str) -> str:
    """*mode* as an audit row may show it: a known mode, or a placeholder (a
    select value is free text and must not forge SEL ``key=`` tokens)."""
    from kiro_crew.slack.code_channels.agent_picker import INVALID_NAME

    return mode if mode in _LABELS else INVALID_NAME


PICKER_LABEL = "*Approval mode*"
PICKER_PLACEHOLDER = "Choose a mode"
SET_NOTE = "Approval mode set to *{label}* by <@{user}>."
UNKNOWN_TEXT = (
    "❌ `{word}` isn't a mode. Use `/{command} approval normal`, "
    "`/{command} approval reads` or `/{command} approval trust`."
)
YOLO_OFF_TEXT = (
    " The override that let every tool run without asking in every session "
    "(Kiro Crew calls it YOLO) is off too."
)
YOLO_ON_NOTE = (
    "⚡ An override is on: every tool runs without asking in every session, "
    "whatever this select shows (Kiro Crew calls this YOLO). Kiro Crew's owner turns "
    "it off with the `yolo off` Slack command or the approval mode picker on the dashboard."
)
CURRENT_TEXT = (
    "This channel is on *{label}*. Change it with `/{command} approval normal`, "
    "`/{command} approval reads` or `/{command} approval trust`."
)

TRUST_CALL_LABEL = "Trust this channel"
#: How every Trust text names the channel's workers, in words a Slack user knows:
#: the sessions the channel's agent starts, which a mode change reaches.
HELPERS = "any helper sessions it starts, including ones already running"
#: How every raise's confirmation says who can undo it and how: the change is
#: owner-only, and a switch back to Normal reaches the helper sessions too.
UNDO = (
    "Only Kiro Crew's owner can undo it: pick Normal in the first message, and helper "
    "sessions follow."
)
#: The confirmation "Trust this channel" asks first: one click puts the whole
#: channel, its workers included, on Trust until the owner picks Normal again;
#: what ran under Trust before that is not undone.
TRUST_CALL_CONFIRM = {
    "title": {"type": "plain_text", "text": "Trust this channel?"},
    "text": {
        "type": "plain_text",
        "text": (
            "This approves this call and puts the whole channel on Trust: tools run "
            f"without asking for this channel's agent and for {HELPERS}. {UNDO}"
        ),
    },
    "confirm": {"type": "plain_text", "text": "Trust this channel"},
    "deny": {"type": "plain_text", "text": "Cancel"},
    # The riskiest action here: red, as the select's "Switch to Trust" is.
    "style": "danger",
}
#: The same confirmation on a worker's card, saying whose call it approves and
#: that the grant is the whole channel's, not only that worker's.
WORKER_TRUST_CALL_CONFIRM = {
    **TRUST_CALL_CONFIRM,
    "text": {
        "type": "plain_text",
        "text": (
            "This approves this helper session's call and puts the whole channel on "
            "Trust: tools run without asking for this channel's agent and for "
            f"{HELPERS}. {UNDO}"
        ),
    },
}
#: The kickoff select applies Normal at once (it only takes grants away). For
#: Reads or Trust it first replies privately with a button that names the mode:
#: Block Kit confirms a select as a whole, so its dialog could not say which.
APPROVAL_CONFIRM_ACTION_ID = "kc_code_channel_approval_confirm"
#: How long that button stays good for, in seconds.
CONFIRM_TTL_SECS = 600
#: What each raised mode does, as the private confirmation says it.
MODE_EFFECT = {
    "trust_reads": (
        "Searches of memory, past chats, skills and the knowledge library also run "
        "without asking, beside Normal's look-ups; everything else still asks. "
        f"This applies to this channel's agent and to {HELPERS}. {UNDO}"
    ),
    "trust": (
        f"Every tool runs without asking, for this channel's agent and for {HELPERS}. {UNDO}"
    ),
}
CONFIRM_TEXT = "Switch this channel to *{label}*? {effect}"
CONFIRM_BUTTON = "Switch to {label}"
CONFIRM_KEEP_NOTE = "Ignore this to keep the current mode. The button expires in 10 minutes."
CONFIRM_STALE_TEXT = (
    "⛔ That confirmation has expired or was already used. Pick the mode again in the "
    "first message."
)
#: Confirmation buttons already pressed (each is good for one click). In memory:
#: a restart drops it, and the session pin and the age bound still hold then.
_used_confirms: set[str] = set()
TRUST_WARNING = (
    "⚠️ <@{user}> put this channel on *Trust*: tools run without asking for the agent "
    f"and for {HELPERS}. Set *Approval mode* in the "
    "first message back to Normal, or run `/{command} approval normal`, to turn it off."
)
FAILED_TEXT = "⚠️ Could not change the approval mode: {reason}"
NO_SESSION_TEXT = "This channel's session isn't open right now. Try again in a moment."
NOT_ALLOWED_TEXT = "⛔ You can't change this channel's approval mode."
OWNER_ONLY_TEXT = "⛔ Only Kiro Crew's owner can change this channel's approval mode."
NOT_THIS_CARD_TEXT = "⛔ That approval isn't this channel's to trust."
NOT_LIVE_TEXT = "⛔ That approval isn't waiting any more, so nothing changed."
REBOUND_TEXT = (
    "This channel moved to a new session, so nothing changed. Use the channel's latest "
    "kickoff message or `/{command} approval <normal|reads|trust>`."
)
#: The same words ``apply_approval_mode`` answers a spent prompt with (its 409).
STALE_REASON = "the approval is no longer waiting"
TRUSTED_CARD_TEXT = "🤝 Approved, and this channel is now on Trust (<@{user}>)."
RAN_UNDER_TRUST = "🤝 Ran under Trust: <@{user}> put this channel on Trust."

#: Code-channel approval cards the channel's Trust answered and re-wrote in
#: place, so ``_run_chat``'s cleanup keeps them instead of deleting them.
_kept_cards: set[str] = set()


def keep_card(channel: str, ts: str) -> bool:
    """Whether ``_run_chat`` should keep (not delete) this card; consumed once."""
    key = f"{channel}:{ts}"
    if key in _kept_cards:
        _kept_cards.discard(key)
        return True
    return False


def _audit(outcome: str, resources: str, error: str = "") -> None:
    surface.audit(AUDIT_CALLER, OPERATION, outcome, resources, error)


# ── The mode a slot holds ─────────────────────────────────────────────────


def current_mode(slot: Any) -> str:
    """``normal``, ``trust_reads`` or ``trust`` -- what the dashboard header shows.

    A live trust scope reads as Trust, as it does in the header.
    """
    if getattr(slot, "_trust", False) or getattr(slot, "_trust_scope", ""):
        return "trust"
    if getattr(slot, "_trust_reads", False):
        return "trust_reads"
    return "normal"


def label(mode: str) -> str:
    return _LABELS.get(mode, _LABELS["normal"])


def _yolo_active() -> bool:
    """Whether the process-wide override (YOLO) is on; True when it cannot be read.

    While it is on, a pick that matches the slot's own flags is still not
    "unchanged": ``apply_approval_mode`` is what ends the override (Normal always
    does; Reads or Trust do for a declared grant), so the change must reach it.
    """
    try:
        from kiro_crew.dashboard.chat_handlers import safety_override

        return bool(safety_override().is_active())
    except Exception:
        logger.debug("safety override state unreadable", exc_info=True)
        return True


def _yolo_on() -> bool:
    """Whether YOLO is on, for display: False when it cannot be read."""
    try:
        from kiro_crew.dashboard.chat_handlers import safety_override

        return bool(safety_override().is_active())
    except Exception:
        return False


def yolo_note() -> str:
    """The kickoff's YOLO line ("" while YOLO is off)."""
    return YOLO_ON_NOTE if _yolo_on() else ""


def audit_refused_pick(payload: dict[str, Any], reason: str) -> None:
    """This module's row for a select pick (or its confirmation) refused before it
    reached it."""
    channel, user, msg_ts, _team, action = _ids(payload)
    picked = (action.get("selected_option") or {}).get("value") or str(
        action.get("value") or ""
    ).split("|", 1)[0]
    mode = _shown_mode(str(picked or "").strip())
    _audit(
        "denied", f"channel={channel} user={user} mode={mode} via=select message={msg_ts}", reason
    )


def _command(orch: Any = None) -> str:
    """The app's slash command, as the person types it: the one the router
    matched (``orch.slack_command``) when an orchestrator is in hand, else
    ``slack.command``."""
    routed = str(getattr(orch, "slack_command", "") or "").strip().lstrip("/")
    if not routed:
        try:
            routed = str(getattr(getattr(live_config(), "slack", None), "command", "") or "")
        except Exception:
            routed = ""
        routed = routed.strip().lstrip("/")
    # A hand-edited value is never echoed into a channel: only a name Slack
    # itself accepts for a command is shown, else the default.
    return routed if _is_command_name(routed) else "kirocrew"


# ── The kickoff select ────────────────────────────────────────────────────


def _option(mode: str) -> dict[str, Any]:
    return {"text": {"type": "plain_text", "text": label(mode)}, "value": mode}


def approval_block(selected: str) -> dict[str, Any]:
    """The kickoff's Approval mode select, on *selected* (drawn by
    :func:`.agent_picker.kickoff_blocks`, below the agent picker)."""
    return {
        "type": "section",
        "block_id": f"{APPROVAL_BLOCK_PREFIX}{time.time_ns()}",
        "text": {"type": "mrkdwn", "text": PICKER_LABEL},
        "accessory": {
            "type": "static_select",
            "action_id": APPROVAL_SELECT_ACTION_ID,
            "placeholder": {"type": "plain_text", "text": PICKER_PLACEHOLDER},
            "options": [_option(mode) for mode, _, _ in MODES],
            "initial_option": _option(selected if selected in _LABELS else "normal"),
        },
    }


async def _refresh(binding: Binding, note: str = "") -> None:
    """Re-draw the kickoff from the session's real agent and mode (never raises)."""
    from kiro_crew.slack.code_channels.agent_picker import refresh_kickoff

    await refresh_kickoff(binding, note=note)


# ── The change ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModeResult:
    """What :func:`set_channel_mode` did, and the reply for the person.

    ``result`` is ``set``, ``unchanged``, ``unavailable`` or ``refused``.
    """

    result: str
    text: str
    #: Running workers the change could not reach (named in ``text``).
    partial: bool = False
    #: ``channel:ts`` of the channel's own cards this change's Trust sweep approved.
    trusted_cards: frozenset[str] = frozenset()


_locks: dict[str, asyncio.Lock] = {}


def _lock(channel_id: str) -> asyncio.Lock:
    lock = _locks.get(channel_id)
    if lock is None:
        lock = _locks[channel_id] = asyncio.Lock()
    return lock


#: Per channel, the sequence number of the latest mode request made from Slack.
_intents: dict[str, int] = {}


def _latest_intent(channel_id: str) -> int:
    return _intents.get(channel_id, 0)


def _note_intent(channel_id: str) -> int:
    """Record a new mode request on *channel_id*; its sequence number."""
    seq = _intents[channel_id] = _latest_intent(channel_id) + 1
    return seq


def _is_latest_intent(channel_id: str, seq: int) -> bool:
    return _latest_intent(channel_id) == seq


def reset() -> None:
    """Forget per-channel locks, mode requests and kept cards (tests)."""
    _locks.clear()
    _intents.clear()
    _kept_cards.clear()
    _used_confirms.clear()


def _spend_confirm(value: str) -> None:
    """Mark a confirmation used; drop ones past their age bound (they refuse anyway)."""
    now = time.time()
    for old in [v for v in _used_confirms if not _within_ttl(v, now)]:
        _used_confirms.discard(old)
    _used_confirms.add(value)


def _within_ttl(value: str, now: float) -> bool:
    try:
        return 0 <= now - int(value.rsplit("|", 1)[-1]) <= CONFIRM_TTL_SECS
    except ValueError:
        return False


async def _apply(
    state: Any,
    slot: Any,
    mode: str,
    user: str,
    still_live: Callable[[], bool] | None = None,
    *,
    lower_only: bool = False,
) -> tuple[bool, str]:
    """The dashboard picker's mutation on *slot*; ``(ok, error)``.

    *still_live*, when given, is asked once more after the last await and right
    before the mutation: a change asked for on behalf of one approval card must
    not land once that card's prompt has been answered or has expired.

    *lower_only* keeps the shared mutation's creator-mode cascade from raising
    any of *slot*'s children: set for a worker, whose tie to the channel may be
    an unverified ``_created_by``. Only the channel session's own change raises.
    """
    from kiro_crew.dashboard.chat_handlers import apply_approval_mode
    from kiro_crew.safety_override import approval_mode_permitted

    # The governance check ``POST /api/chat/mode`` makes before it mutates.
    if not await asyncio.to_thread(approval_mode_permitted, mode):
        return False, "the approval-modes policy does not allow it"
    if still_live is not None and not still_live():
        return False, STALE_REASON
    resp = await apply_approval_mode(
        state,
        mode=mode,
        slot=slot,
        slot_key=slot.key,
        request_app="",
        cron_creator="",
        audit_caller=lambda _label: f"{AUDIT_CALLER}:{user}",
        # Asked again inside, after its own await and right before the grant.
        still_live=still_live,
        cascade_may_raise=not lower_only,
    )
    try:
        body = json.loads(getattr(resp, "text", "") or "{}")
    except ValueError:
        body = {}
    if getattr(resp, "status", 200) >= 400 or not body.get("ok", False):
        return False, str(body.get("error") or "the change was refused")
    return True, ""


async def set_channel_mode(
    binding: Binding,
    mode: str,
    user: str,
    *,
    via: str,
    still_live: Callable[[], bool] | None = None,
    origin_card: str = "",
    orch: Any = None,
    expected_slot: Any = None,
    team: str,
) -> ModeResult:
    """Put *binding*'s session on *mode* for *user*, who is already authorized.

    *still_live* ties the change to one approval card: it is checked under the
    channel lock and again right before the mutation, and a card whose prompt
    has stopped waiting changes no mode. On a change to Trust, posts the
    channel's warning line.

    *origin_card* (``channel:ts``) is the card whose click asked: the caller
    writes its final text, so the Trust sweep does not re-write it first.

    *expected_slot* is the session the caller authorized against: a channel
    whose session was replaced since then changes nothing. Right before each
    mutation the channel must also still be unarchived here and the feature on,
    since an Archive or a toggle-off can land during the awaits in between.

    Any change but to Normal also re-runs the code channel's owner gates
    (*team* is the workspace the request came from) after its awaits and again
    at the mutation, the way :mod:`.agent_picker`'s switch does: an owner
    removed, a channel or origin that stopped admitting the bot, the channel
    policy narrowed, or Slack denied meanwhile grants nothing. That includes
    Trust to Reads, a lowering of the channel itself, because its creator-mode
    cascade can still raise a worker on Normal up to Reads. Only Normal, which
    can raise nobody, is never held back by them (none of them: owner, feature,
    archive, origin, policy) once its caller's entry checks passed: holding it
    back would leave a grant live on a channel just cut off. Like any Normal
    write it also ends a live YOLO override for every session.
    """
    from kiro_crew.slack.code_channels import agent_picker

    channel = binding.channel_id
    resources = f"channel={channel} user={user} mode={_shown_mode(mode)} via={via}"
    if mode not in _LABELS:
        _audit("denied", resources, "unknown mode")
        return ModeResult("refused", FAILED_TEXT.format(reason="unknown mode"))
    state = surface.dashboard_state()
    slot = channel_session(state, binding)
    if slot is None or (expected_slot is not None and slot is not expected_slot):
        _audit("error", resources, "session unavailable")
        return ModeResult("unavailable", NO_SESSION_TEXT)

    def _gates_open() -> bool:
        # Synchronous: safe inside apply_approval_mode's commit section.
        return _channel_open(binding, orch) and agent_picker._sync_gates(orch, binding, user, team)

    # This request is the channel's latest; a later one (a revoke included)
    # supersedes it before it commits.
    intent = _note_intent(channel)
    # A raise also needs the mode it started from: a change from any other
    # surface meanwhile (the dashboard's picker included) voids it.
    seen = current_mode(slot)

    def _raise_still_from_seen() -> bool:
        # Judged at the commit: a request that raises the slot as it is NOW
        # needs the mode it started from to still hold.
        now = current_mode(slot)
        return _RANK.get(mode, 3) <= _RANK.get(now, -1) or now == seen

    def _channel_live() -> bool:
        return (
            # Anything but Normal needs the channel still open and this request
            # still the latest: even Trust to Reads cascades a raise to Reads
            # onto inheriting workers on Normal. Normal only takes grants away,
            # so nothing ever blocks it.
            (not _gated(mode) or (_gates_open() and _is_latest_intent(channel, intent)))
            and _raise_still_from_seen()
            and channel_session(state, binding) is slot
            and (still_live is None or still_live())
        )

    yolo_before = _yolo_on()
    async with _lock(channel):
        # The sweep and the grant read the slot's rows on the loop: load a
        # restored session's window off it first (a no-op once loaded).
        if not await agent_picker._load_window(slot):
            _audit("error", resources, "transcript window could not be loaded")
            return ModeResult("unavailable", NO_SESSION_TEXT)
        refused = (
            await agent_picker._still_admitted(orch, binding, user, team) if _gated(mode) else None
        )
        if refused == agent_picker.REBOUND:
            # The channel was rebound to a new session: point at its kickoff.
            _audit("denied", resources, agent_picker.REBOUND)
            return ModeResult("refused", REBOUND_TEXT.format(command=_command(orch)))
        if refused:
            _audit("denied", resources, agent_picker.NO_LONGER_ADMITTED)
            return ModeResult("refused", NOT_ALLOWED_TEXT)
        if not _channel_live():
            _audit("denied", resources, STALE_REASON)
            return ModeResult("refused", NOT_LIVE_TEXT)
        if current_mode(slot) == mode and not _yolo_active():
            # The channel already holds it. A worker a person raised above it
            # is lowered to it; none is raised from here (raises reach workers
            # only through the channel session's own grant). Nothing has
            # committed yet, so the originating card's fence rides on theirs.
            fenced, failed = await _apply_to_workers(
                state, binding, slot, mode, user, via, gates=_gates_open, origin_live=_channel_live
            )
            if not fenced:
                _audit("denied", resources, STALE_REASON)
                return ModeResult("refused", NOT_LIVE_TEXT)
            _audit("allowed", f"{resources} result=unchanged")
            return ModeResult(
                "unchanged",
                f"ℹ️ This channel is already on *{label(mode)}*.{_failed_note(failed)}",
                partial=bool(failed),
            )
        # Each waiting card's own future, captured before any await, so a card
        # is only ever said to have run under Trust when THIS grant's sweep
        # approved it (see _settle_own_cards).
        own_futures = _own_pending_cards(binding, slot) if mode == "trust" else {}
        pending_at_grant: set[str] = set()
        # The channel's own grant also raises its inheriting workers
        # (``approval_inheritance``) and sweeps their prompts in the same step,
        # so their relayed cards are claimed BEFORE it: otherwise each worker's
        # own cleanup would label its card "approved in the dashboard".
        claimed: list[Any] = []
        claim_workers = mode == "trust" and _raises(mode, slot)
        if _raises(mode, slot):
            from kiro_crew.slack.code_channels import worker_approvals

            # That cascade's sweep reads every descendant's rows on the loop:
            # load their windows off it first. One that cannot be loaded would
            # be read synchronously on the loop by the sweep, after the grant
            # landed: nothing changes instead.
            for worker in worker_approvals.descendants(state, slot):
                if not await agent_picker._load_window(worker):
                    _audit("error", resources, "a worker's transcript window could not be loaded")
                    return ModeResult("unavailable", NO_SESSION_TEXT)
            if mode == "trust":
                for worker in worker_approvals.live_workers(state, slot):
                    claimed += worker_approvals.claim_pending(str(getattr(worker, "key", "")))
        claimed_at_grant: set[str] = set()

        def _grant_fence() -> bool:
            # Asked last right before the mutation, with the sweep following it
            # in the same synchronous step: what is still waiting now is what
            # the sweep answers.
            if not _channel_live():
                return False
            if claim_workers:
                # A card relayed during the awaits above is claimed here too,
                # with the sweep following in this same synchronous step.
                from kiro_crew.slack.code_channels import worker_approvals

                for worker in worker_approvals.live_workers(state, slot):
                    claimed.extend(worker_approvals.claim_pending(str(getattr(worker, "key", ""))))
            pending_at_grant.clear()
            pending_at_grant.update(k for k, f in own_futures.items() if not f.done())
            claimed_at_grant.clear()
            claimed_at_grant.update(f"{c.channel}:{c.ts}" for c in claimed if not c.future.done())
            return True

        # Nothing is kept from the runner's delete before the grant: a card
        # answered while it awaits is deleted as it always was. Only the cards
        # the sweep answers are kept, below, with no yield since the sweep, so
        # no runner cleanup can have run for them yet.
        try:
            ok, error = await _apply(state, slot, mode, user, _grant_fence)
        except asyncio.CancelledError:
            # Release the claims: each card's own cleanup says how it ended.
            for card in claimed:
                card.answered = False
            raise
        except Exception:
            logger.warning("code channel %s: approval mode change failed", channel, exc_info=True)
            ok, error = False, "the change failed"
        trusted = (
            frozenset(k for k in pending_at_grant if _approved(own_futures[k]))
            if ok
            else frozenset()
        )
        _kept_cards.update(trusted)
        # The worker cards the cascade's sweep approved; the rest are released
        # (still waiting) or said how they ended. Settled before the
        # lowering-only per-worker pass.
        cascade_swept = (
            {
                f"{c.channel}:{c.ts}"
                for c in claimed
                if f"{c.channel}:{c.ts}" in claimed_at_grant and _approved(c.future)
            }
            if ok
            else set()
        )
        if claimed:
            from kiro_crew.slack.code_channels import worker_approvals

            await worker_approvals.settle_claimed(
                claimed,
                swept=cascade_swept,
                line=RAN_UNDER_TRUST.format(user=user),
                skip=origin_card,
            )
        # The channel's own cards are re-written below; the worker cards
        # settle_claimed already wrote (header kept) are only reported.
        own_trusted = trusted
        trusted = trusted | frozenset(cascade_swept)
        if not ok and error == STALE_REASON:
            _audit("denied", resources, STALE_REASON)
            return ModeResult("refused", NOT_LIVE_TEXT)
        if not ok:
            _audit("error", f"{resources} result=refused", error)
            return ModeResult("refused", FAILED_TEXT.format(reason=error))
        _audit("allowed", f"{resources} result=set")
        await _settle_own_cards(binding, [k for k in own_trusted if k != origin_card], user)
        _fenced, failed = await _apply_to_workers(
            state, binding, slot, mode, user, via, gates=_gates_open
        )
    if mode == "trust" and _channel_open(binding, orch):
        await _post_channel(orch, channel, TRUST_WARNING.format(user=user, command=_command(orch)))
    await _refresh(binding, note=SET_NOTE.format(label=label(mode), user=user))
    ended = YOLO_OFF_TEXT if yolo_before and not _yolo_on() else ""
    return ModeResult(
        "set",
        f"✅ This channel is now on *{label(mode)}*.{ended}{_failed_note(failed)}",
        partial=bool(failed),
        trusted_cards=trusted if mode == "trust" else frozenset(),
    )


def _channel_open(binding: Binding, orch: Any = None) -> bool:
    """Synchronous: the feature is on and the channel is not archived.

    Both halves of :func:`.surface.is_archived`: the in-process fence (an archive
    in progress) and the stored stamp (one already recorded, which lifts the
    fence). The store is read from its loaded snapshot, so nothing blocks here.
    """
    try:
        on = code_channels_on(live_config(orch))
        if not on or binding.channel_id in surface._archived_here:
            return False
        current = store().get_binding(binding.channel_id)
    except Exception:
        return False
    return not bool((current or binding).archived_at)


def _raises(mode: str, slot: Any) -> bool:
    """Whether putting *slot* on *mode* can widen what runs without asking.

    Judged by what the write does, not by :func:`current_mode`: every Trust
    write re-runs the sweep that approves pending prompts (a lapsed trust scope
    or a live YOLO grant still reads as Trust), and Reads widens a slot holding
    neither flag. Only Normal, or Reads on a slot already above it, takes away.
    """
    if mode == "trust":
        return True
    if mode == "trust_reads":
        return not (getattr(slot, "_trust", False) or getattr(slot, "_trust_reads", False))
    return mode != "normal"


def _not_above_channel(mode: str, channel_slot: Any) -> bool:
    """Whether a worker put on *mode* sits at or below the channel's mode NOW.

    Asked at the worker's commit: the channel can be lowered from the dashboard
    while the worker's change awaits, and a target planned against its earlier
    mode must not leave the worker above it.
    """
    return _RANK.get(mode, 3) <= _RANK.get(current_mode(channel_slot), -1)


def _gated(mode: str) -> bool:
    """Whether a change to *mode* must pass the channel's gates.

    Wider than :func:`_raises`: a slot put on Reads hands Reads to its
    inheriting descendants (``approval_inheritance``), raising one on Normal,
    even when the slot itself is lowered from Trust. Only Normal raises nobody.
    """
    return mode != "normal"


def _failed_title(worker: Any, channel_slot: Any) -> str:
    """How the reply names a session a lowering missed: its title only when the
    gateway vouches it is this channel's (an unverified one could be anyone's)."""
    from kiro_crew.slack.code_channels import worker_approvals

    key = str(getattr(channel_slot, "key", "") or "")
    vouched = worker_approvals.created_by(worker, key) or any(
        worker_approvals.created_by(worker, str(getattr(s, "key", "") or ""))
        for s in worker_approvals.descendants(surface.dashboard_state(), channel_slot)
    )
    return worker_approvals._worker_title(worker) if vouched else "a session"


def _failed_note(failed: list[str]) -> str:
    """The reply's line for workers a change could not reach ("" when none)."""
    if not failed:
        return ""
    shown = ", ".join(failed[:5]) + (f" and {len(failed) - 5} more" if len(failed) > 5 else "")
    return f" ⚠️ Couldn't change {len(failed)} running worker(s): {shown}."


def _own_pending_cards(binding: Binding, slot: Any) -> dict[str, asyncio.Future[str]]:
    """The cards the channel's own session has waiting: ``channel:ts`` to the
    exact approval future each card was posted for, still pending now."""
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.slack.handler import _linked_approvals

    session = effective_session_key(slot)
    prefix = f"{binding.channel_id}:"
    futures = getattr(slot, "_approval_futures", None) or {}
    out: dict[str, asyncio.Future[str]] = {}
    for key, entry in list(_linked_approvals.items()):
        if not key.startswith(prefix) or str(getattr(entry, "session_key", "")) != session:
            continue
        fut = futures.get(str(getattr(entry, "request_id", "")))
        if fut is not None and not fut.done():
            out[key] = fut
    return out


def _approved(fut: asyncio.Future[str]) -> bool:
    return fut.done() and not fut.cancelled() and str(fut.result()).startswith("approved")


def settled_blocks(line: str) -> list[dict[str, Any]]:
    """A decided card's replacement: the line alone, no buttons.

    ``chat.update`` keeps a message's blocks when none are sent, so every
    rewrite of a card sends these, or the old buttons would stay live.
    """
    return [{"type": "section", "text": {"type": "mrkdwn", "text": line}}]


async def write_card(channel: str, ts: str, line: str) -> None:
    """Replace one card with *line* (never raises)."""
    slack = surface.slack()
    if slack is None:
        return
    try:
        await slack.update_message(channel, ts, text=line, blocks=settled_blocks(line))
    except Exception:
        logger.debug("code channel card not updated", exc_info=True)


async def _settle_own_cards(binding: Binding, keys: list[str], user: str) -> None:
    """Re-write the channel's own cards this grant's sweep approved, in place.

    Only those (*keys*, kept from the runner's delete): a card answered any
    other way meanwhile is deleted by the runner as it always was.
    """
    line = RAN_UNDER_TRUST.format(user=user)
    for key in keys:
        await write_card(binding.channel_id, key.split(":", 1)[1], line)


def _worker_lowering_live(
    state: Any,
    binding: Binding,
    channel_slot: Any,
    worker: Any,
    mode: str,
    origin_live: Callable[[], bool] | None,
    gates: Callable[[], bool],
) -> Callable[[], bool]:
    """The check ``apply_approval_mode`` runs right before it lowers *worker*.

    Synchronous and asked after the last await, so what it vouches for holds at
    the mutation: the exact worker object is still the registered slot under its
    key (not a replacement under a reused key), it is still the direct, unlinked
    child of the channel's CURRENT session, *mode* still lowers it and leaves it
    no higher than the channel holds now (:func:`_not_above_channel`).
    *origin_live*, the card that asked, must still be waiting while nothing has
    committed. A lowering to anything but Normal also needs the channel's
    *gates*: the worker's own cascade can raise one of ITS inheriting sessions.
    """

    def live() -> bool:
        key = str(getattr(worker, "key", "") or "")
        return (
            bool(key)
            and channel_session(state, binding) is channel_slot
            and (getattr(state, "_slots", None) or {}).get(key) is worker
            and str(getattr(worker, "_created_by", "") or "") == str(channel_slot.key)
            and not getattr(worker, "_slack_linked", False)
            and _RANK.get(mode, 3) < _RANK.get(current_mode(worker), -1)
            and _not_above_channel(mode, channel_slot)
            and (not _gated(mode) or gates())
            and not _raises_a_sharer(state, worker, mode)
            and (origin_live is None or origin_live())
        )

    return live


def _lowering_live(
    state: Any,
    binding: Binding,
    channel_slot: Any,
    worker: Any,
    mode: str,
    gates: Callable[[], bool],
) -> Callable[[], bool]:
    """The fence for lowering a deeper session (a worker's worker, a linked one).

    Asked right before the mutation: the exact session is still registered,
    still under the channel's CURRENT session in the ``_created_by`` tree, and
    *mode* still lowers it, no higher than the channel holds now. A lowering to
    anything but Normal also needs the
    channel's *gates* (its own cascade can raise a session below it).
    """
    from kiro_crew.slack.code_channels import worker_approvals

    def live() -> bool:
        key = str(getattr(worker, "key", "") or "")
        return (
            bool(key)
            and channel_session(state, binding) is channel_slot
            and (getattr(state, "_slots", None) or {}).get(key) is worker
            and _RANK.get(mode, 3) < _RANK.get(current_mode(worker), -1)
            and _not_above_channel(mode, channel_slot)
            and (not _gated(mode) or gates())
            and not _raises_a_sharer(state, worker, mode)
            and any(s is worker for s in worker_approvals.descendants(state, channel_slot))
        )

    return live


def _raises_a_sharer(state: Any, worker: Any, mode: str) -> bool:
    """Whether writing *mode* to *worker* would raise another slot on its session.

    The shared mutation writes the mode to every slot that shares *worker*'s
    effective session, and that sharing can come from a link read back from an
    editable transcript. A lowering of *worker* must not grant any of them more
    than they hold, so a lowering to Reads that would is refused (and falls back
    to Normal, :func:`_lower_one`).
    """
    from kiro_crew.dashboard.chat_utils import effective_session_key

    key = effective_session_key(worker)
    return any(
        sharing is not worker
        and effective_session_key(sharing) == key
        and _RANK.get(current_mode(sharing), -1) < _RANK.get(mode, 3)
        for sharing in list((getattr(state, "_slots", None) or {}).values())
    )


async def _apply_to_workers(
    state: Any,
    binding: Binding,
    slot: Any,
    mode: str,
    user: str,
    via: str,
    *,
    gates: Callable[[], bool],
    origin_live: Callable[[], bool] | None = None,
) -> tuple[bool, list[str]]:
    """Lower the channel session's workers that sit above *mode* (never raises).

    A raise reaches workers only through the channel session's own grant: its
    creator-mode cascade (``approval_inheritance``, via ``apply_approval_mode``)
    decides which workers follow, under its rules, and sweeps their prompts.
    This pass only takes grants away, where that cascade does not reach: a
    worker whose own mode a person raised on the dashboard, and every deeper
    session in the ``_created_by`` tree (a worker's workers, linked ones), which
    inherited the grant when they started. A worker the change could not lower
    (its window would not load, or the change failed) is named in the reply; one
    a closed gate refused Reads is put on Normal instead (:func:`_lower_one`).

    *origin_live* is the asking card's fence, carried until the first change
    commits. Returns ``(fenced, failed)``: *fenced* False when that fence
    refused, so nothing changed; *failed* the shown titles of the sessions the
    change could not reach.
    """
    from kiro_crew.slack.code_channels import agent_picker, worker_approvals

    failed: list[str] = []
    direct = worker_approvals.live_workers(state, slot)
    for worker in direct:
        if not _RANK.get(mode, 3) < _RANK.get(current_mode(worker), -1):
            continue  # not above the channel: nothing to take away
        resources = (
            f"channel={binding.channel_id} user={user} mode={mode} via={via} "
            f"worker={getattr(worker, 'key', '')}"
        )
        if origin_live is not None and not origin_live():
            return False, failed
        if not await agent_picker._load_window(worker):
            failed.append(_failed_title(worker, slot))
            continue

        def live_for(
            target: str, _worker: Any = worker, _origin: Callable[[], bool] | None = origin_live
        ) -> Callable[[], bool]:
            return _worker_lowering_live(state, binding, slot, _worker, target, _origin, gates)

        ok, error, landed = await _lower_one(state, worker, mode, user, live_for, gates, slot)
        if ok:
            _follows_channel(state, worker)
        _audit(
            "allowed" if ok else ("denied" if error == STALE_REASON else "error"),
            f"{resources} result={'worker_set' if ok else 'worker_refused'}"
            + (f" landed={landed}" if landed != mode else ""),
            error,
        )
        if ok:
            origin_live = None  # a change committed under the fence
        elif origin_live is not None and error == STALE_REASON and not origin_live():
            return False, failed
        elif _still_above(state, worker, mode):
            failed.append(_failed_title(worker, slot))
    failed += await _lower_deeper(state, binding, slot, mode, user, via, skip=direct, gates=gates)
    return True, failed


async def _lower_one(
    state: Any,
    worker: Any,
    mode: str,
    user: str,
    live_for: Callable[[str], Callable[[], bool]],
    gates: Callable[[], bool],
    channel_slot: Any,
) -> tuple[bool, str, str]:
    """Lower *worker* to *mode* under the fence ``live_for(mode)``.

    ``(ok, error, landed)``, *landed* the mode written. A lowering to Reads
    refused because a gate closed, because the channel itself went below
    Reads meanwhile, or because a slot sharing *worker*'s session sits below
    Reads (:func:`_raises_a_sharer`), falls back to Normal, which needs no gate and is never
    above the channel (it raises nobody): the worker is not left on Trust over
    a refused or lowered channel. A worker that cannot be put there either is
    named in the reply. Never while the YOLO override is on: a Normal write
    ends it for every session, which a lowering to Reads never does.
    """
    landed = mode
    try:
        ok, error = await _apply(state, worker, mode, user, live_for(mode), lower_only=True)
        stale = (
            not gates()
            or not _not_above_channel(mode, channel_slot)
            or _raises_a_sharer(state, worker, mode)
        )
        if not ok and _gated(mode) and stale and not _yolo_active():
            landed = "normal"
            ok, error = await _apply(state, worker, landed, user, live_for(landed), lower_only=True)
    except Exception:
        logger.warning("code channel worker mode lowering failed", exc_info=True)
        ok, error = False, "the change failed"
    return ok, error, landed


def _follows_channel(state: Any, worker: Any) -> None:
    """Keep *worker*'s posture marked inherited after the channel's change lands on it.

    The channel's change reaches a worker through ``apply_approval_mode``, which
    counts as a person's own pick and so marks the worker's mode explicit
    (``approval_inheritance``). The worker is following its creator, not
    holding a choice of its own: left explicit, a later lowering of the channel
    session from the dashboard would skip it and leave it above its creator.
    Synchronous, run straight after the grant.
    """
    from kiro_crew.dashboard.chat_utils import effective_session_key

    key = effective_session_key(worker)
    for sharing in list(getattr(state, "_slots", {}).values()):
        if effective_session_key(sharing) == key:
            sharing._posture_inherited = True


async def _lower_deeper(
    state: Any,
    binding: Binding,
    slot: Any,
    mode: str,
    user: str,
    via: str,
    *,
    skip: list[Any],
    gates: Callable[[], bool],
) -> list[str]:
    """Lower every deeper session above *mode*; the titles of those it could not."""
    from kiro_crew.slack.code_channels import agent_picker, worker_approvals

    failed: list[str] = []
    for worker in worker_approvals.descendants(state, slot):
        if any(worker is s for s in skip):
            continue
        if not _RANK.get(mode, 3) < _RANK.get(current_mode(worker), -1):
            continue
        resources = (
            f"channel={binding.channel_id} user={user} mode={mode} via={via} "
            f"worker={getattr(worker, 'key', '')} depth=deeper"
        )
        if not await agent_picker._load_window(worker):
            failed.append(_failed_title(worker, slot))
            continue

        def live_for(target: str, _worker: Any = worker) -> Callable[[], bool]:
            return _lowering_live(state, binding, slot, _worker, target, gates)

        ok, error, landed = await _lower_one(state, worker, mode, user, live_for, gates, slot)
        if ok:
            _follows_channel(state, worker)
        _audit(
            "allowed" if ok else ("denied" if error == STALE_REASON else "error"),
            f"{resources} result={'worker_set' if ok else 'worker_refused'}"
            + (f" landed={landed}" if landed != mode else ""),
            error,
        )
        if not ok and _still_above(state, worker, mode):
            failed.append(_failed_title(worker, slot))
    return failed


def _still_above(state: Any, worker: Any, mode: str) -> bool:
    """A lowering that did not take: *worker* is still registered and above *mode*."""
    key = str(getattr(worker, "key", "") or "")
    registered = (getattr(state, "_slots", None) or {}).get(key) is worker
    return registered and _RANK.get(mode, 3) < _RANK.get(current_mode(worker), -1)


async def _post_channel(orch: Any, channel: str, text: str) -> None:
    """A top-level line in the channel, where the session's replies go, through
    the code channels' one fixed-notice helper (:func:`.routing._post`)."""
    from kiro_crew.slack.code_channels import routing

    if getattr(orch, "slack", None) is None or not await surface.outbound_permitted():
        return
    if not await routing._post(orch, channel, text, None):
        logger.info("code channel %s: approval notice not posted", channel)


# ── Authorization ─────────────────────────────────────────────────────────


async def authorize(
    orch: Any, binding: Binding, user: str, team: str, resources: str
) -> str | None:
    """None when *user* may change *binding*'s mode; else the refusal to show.

    The code channel's one owner gate (:func:`.agent_picker.authorize`: bound
    workspace, owner, channel and origin admit the bot, not archived), with this
    module's audit rows. ``""`` means refuse silently (a foreign workspace).
    """
    from kiro_crew.slack.code_channels import agent_picker

    return await agent_picker.authorize(
        orch,
        binding,
        user,
        team,
        resources,
        audit=_audit,
        not_allowed=NOT_ALLOWED_TEXT,
        owner_only=OWNER_ONLY_TEXT,
    )


async def _ephemeral(
    channel: str, user: str, text: str, blocks: list[dict[str, Any]] | None = None
) -> None:
    slack = surface.slack()
    if slack is None or not (channel and user and text):
        return
    try:
        if blocks is None:
            await slack.post_ephemeral(channel, user, text)
        else:
            await slack.post_ephemeral(channel, user, text, blocks=blocks)
    except Exception:
        logger.debug("code channel ephemeral failed", exc_info=True)


def _ids(payload: dict[str, Any]) -> tuple[str, str, str, str, dict[str, Any]]:
    """``(channel, user, message ts, team, action)`` of a ``block_actions``."""
    channel = str((payload.get("channel") or {}).get("id") or "")
    user = str((payload.get("user") or {}).get("id") or "")
    msg_ts = str((payload.get("message") or {}).get("ts") or "")
    team = str(
        (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id") or ""
    )
    action = (payload.get("actions") or [{}])[0] or {}
    return channel, user, msg_ts, team, action


# ── "Trust this channel" on an approval card ──────────────────────────────


def _card_entry(channel: str, msg_ts: str) -> Any | None:
    from kiro_crew.slack.handler import _linked_approvals

    return _linked_approvals.get(f"{channel}:{msg_ts}")


async def handle_trust_call(orch: Any, payload: dict[str, Any]) -> bool:
    """A click on an approval card's "Trust this channel"; False when the gate is shut.

    Only a card the channel's OWN session posted counts: the card's registry
    entry must name the session the channel's binding resolves to. The session
    goes to Trust through :func:`set_channel_mode`, whose sweep approves every
    call the session is waiting on, this one included; a Trust that does not
    take approves nothing, so a failed escalation is never reported as an
    approval.
    """
    if not code_channels_on(live_config(orch)):
        return False
    channel, user, msg_ts, team, _action = _ids(payload)
    resources = f"channel={channel} user={user} via=approval_card message={msg_ts}"
    # The workspace first, before any side effect (as the agent picker does).
    if not surface.workspace_admitted(team):
        _audit("denied", f"{resources} team_id={team}", "enterprise_origin_mismatch")
        return True
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    entry = _card_entry(channel, msg_ts) if binding is not None else None
    state = surface.dashboard_state()
    slot = channel_session(state, binding) if binding is not None else None
    from kiro_crew.dashboard.chat_utils import effective_session_key

    if (
        binding is None
        or entry is None
        or slot is None
        or str(getattr(entry, "session_key", "")) != effective_session_key(slot)
    ):
        _audit("denied", resources, "not this channel's approval card")
        await _ephemeral(channel, user, NOT_THIS_CARD_TEXT)
        return True
    refusal = await authorize(orch, binding, user, team, resources)
    if refusal is not None:
        await _ephemeral(channel, user, refusal)
        return True
    # Pin the ONE call this card asks about: the channel slot's own future for
    # the card's request id, captured before anything changes. Request ids are
    # only unique per session, so nothing here ever looks one up state-wide.
    request_id = str(getattr(entry, "request_id", ""))
    futures = getattr(slot, "_approval_futures", None) or {}
    expected = futures.get(request_id)

    def _live() -> bool:
        # The card's own prompt, still waiting on the channel's slot.
        return (
            expected is not None
            and not expected.done()
            and getattr(slot, "_approval_futures", {}).get(request_id) is expected
        )

    if not _live():
        _audit("denied", resources, STALE_REASON)
        await _ephemeral(channel, user, NOT_LIVE_TEXT)
        return True
    outcome = await set_channel_mode(
        binding,
        "trust",
        user,
        via="approval_card",
        still_live=_live,
        origin_card=f"{channel}:{msg_ts}",
        orch=orch,
        expected_slot=slot,
        team=team,
    )
    if outcome.result not in ("set", "unchanged"):
        await _ephemeral(channel, user, outcome.text)
        return True
    if expected is None:  # unreachable: _live() held above
        return True
    card_key = f"{channel}:{msg_ts}"
    # A change to Trust answered the call through its slot-scoped sweep; only
    # then does the card say this click approved it.
    approved_here = card_key in outcome.trusted_cards
    if not approved_here and expected.done():
        # Answered another way while the change ran: never "Approved" here;
        # the runner deletes the card as it would any decided prompt's.
        _audit("denied", resources, STALE_REASON)
        await _ephemeral(channel, user, NOT_LIVE_TEXT)
        return True
    if not approved_here:
        # An already-trusted session's card (a race) is answered on that slot
        # alone, only while the channel is open and the slot still holds the
        # very future the card was for; nothing is awaited from here to the
        # resolve.
        resolve = getattr(state, "resolve_slot_approval", None)
        from kiro_crew.slack.code_channels import agent_picker

        if (
            _channel_open(binding, orch)
            and agent_picker._sync_gates(orch, binding, user, team)
            # Still on Trust: a revoke (from any surface) during the awaits
            # after the grant committed approves nothing by hand.
            and current_mode(slot) == "trust"
            and callable(resolve)
        ):
            _kept_cards.add(card_key)  # the runner keeps the card this click rewrites
            try:
                approved_here = bool(resolve(slot, request_id, True, expected_future=expected))
            except Exception:
                logger.warning("code channel %s: trusted card not resolved", channel, exc_info=True)
            if not approved_here:
                _kept_cards.discard(card_key)
    if not approved_here:
        _audit("denied", resources, STALE_REASON)
        await _ephemeral(channel, user, NOT_LIVE_TEXT)
        return True
    _audit(
        "allowed",
        f"{resources} session={entry.session_key} request={request_id} result=approved",
    )
    await write_card(channel, msg_ts, TRUSTED_CARD_TEXT.format(user=user))
    return True


# ── The kickoff select and /kirocrew approval ───────────────────────────────


async def handle_select(orch: Any, payload: dict[str, Any]) -> bool:
    """A pick in the kickoff's Approval mode select; False when the gate is shut."""
    if not code_channels_on(live_config(orch)):
        return False
    channel, user, msg_ts, team, action = _ids(payload)
    mode = str(((action.get("selected_option") or {}).get("value")) or "").strip()
    resources = f"channel={channel} user={user} mode={_shown_mode(mode)} via=select"
    # The workspace first, before any side effect (as the agent picker does).
    if not surface.workspace_admitted(team):
        _audit("denied", f"{resources} team_id={team}", "enterprise_origin_mismatch")
        return True
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    if binding is None or not msg_ts or msg_ts != binding.anchor_ts:
        _audit("denied", f"{resources} message={msg_ts}", "not a code channel kickoff")
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    refusal = await authorize(orch, binding, user, team, resources)
    if refusal is not None:
        await _ephemeral(channel, user, refusal)
        if refusal:
            await _refresh(binding)  # put the select back on the real mode
        return True
    slot = channel_session(surface.dashboard_state(), binding)
    if mode in MODE_EFFECT and (slot is None or current_mode(slot) != mode or _yolo_active()):
        # Reads or Trust: ask first, privately, naming the mode. Nothing changes yet,
        # and the select goes back to the mode the session really has.
        _audit("allowed", f"{resources} result=confirm_asked")
        # Asking is a request too: it voids any confirmation asked before it.
        text, blocks = confirm_blocks(mode, binding.session_key, _note_intent(channel))
        await _ephemeral(channel, user, text, blocks)
        await _refresh(binding)
        return True
    held = slot
    still_live = (
        # A pick of the mode the session already holds raises nothing; should the
        # mode drop while the change waits, it must not raise without the confirm.
        (lambda: current_mode(held) == mode and not _yolo_active())
        if mode in MODE_EFFECT
        else None
    )
    await _apply_pick(
        orch,
        binding,
        mode,
        user,
        team,
        channel,
        via="select",
        still_live=still_live,
        expected_slot=held if still_live is not None else None,
    )
    return True


async def _apply_pick(
    orch: Any,
    binding: Binding,
    mode: str,
    user: str,
    team: str,
    channel: str,
    *,
    via: str,
    still_live: Callable[[], bool] | None = None,
    expected_slot: Any = None,
) -> None:
    """Change the mode for an authorized pick; say anything short of a clean set."""
    try:
        outcome = await set_channel_mode(
            binding,
            mode,
            user,
            via=via,
            orch=orch,
            team=team,
            still_live=still_live,
            expected_slot=expected_slot,
        )
    except asyncio.CancelledError:
        # Put the select back on the mode the session really has before unwinding.
        await asyncio.shield(_refresh(binding))
        raise
    if outcome.result != "set" or outcome.partial:  # a partial set names what it missed
        await _ephemeral(channel, user, outcome.text)
    if outcome.result != "set":
        await _refresh(binding)


def confirm_blocks(
    mode: str, session_key: str, intent: int = 0
) -> tuple[str, list[dict[str, Any]]]:
    """The private confirmation for raising the channel to *mode*: its text (also
    the fallback) and the blocks, with one button that names the mode.

    The button's value pins the mode, the channel's session, the channel's
    latest mode request when it was asked (*intent*) and the time, so a click
    after a rebind, after any later mode request, or past
    :data:`CONFIRM_TTL_SECS` changes nothing.
    """
    text = CONFIRM_TEXT.format(label=label(mode), effect=MODE_EFFECT[mode])
    value = f"{mode}|{session_key}|{intent}|{int(time.time())}"
    button: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": CONFIRM_BUTTON.format(label=label(mode))},
        "action_id": APPROVAL_CONFIRM_ACTION_ID,
        "value": value,
    }
    if mode == "trust":
        button["style"] = "danger"
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [button]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": CONFIRM_KEEP_NOTE}]},
    ]
    return text, blocks


async def handle_confirm(orch: Any, payload: dict[str, Any]) -> bool:
    """The "Switch to <mode>" button of :func:`confirm_blocks`; False when the gate is shut."""
    if not code_channels_on(live_config(orch)):
        return False
    channel, user, _msg_ts, team, action = _ids(payload)
    parts = (str(action.get("value") or "").split("|") + ["", "", "", ""])[:4]
    mode, session_key, intent_seen, asked = parts
    resources = f"channel={channel} user={user} mode={_shown_mode(mode)} via=select_confirm"
    if not surface.workspace_admitted(team):
        _audit("denied", f"{resources} team_id={team}", "enterprise_origin_mismatch")
        return True
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    try:
        age = time.time() - int(asked)
    except ValueError:
        age = CONFIRM_TTL_SECS + 1
    if (
        mode not in MODE_EFFECT
        or binding is None
        or binding.session_key != session_key
        or not 0 <= age <= CONFIRM_TTL_SECS
    ):
        _audit("denied", resources, "stale approval mode confirmation")
        await _ephemeral(channel, user, CONFIRM_STALE_TEXT)
        return True
    value = str(action.get("value"))
    if value in _used_confirms:
        # One click per confirmation: a later click on the same message changes nothing.
        _audit("denied", resources, "approval mode confirmation already used")
        await _ephemeral(channel, user, CONFIRM_STALE_TEXT)
        return True
    refusal = await authorize(orch, binding, user, team, resources)
    if refusal is not None:
        await _ephemeral(channel, user, refusal)
        return True
    # Checked again and spent with no await in between: two clicks that both
    # passed the check above cannot both apply, and a mode request made since
    # the confirmation was asked (a revoke included) voids it.
    if value in _used_confirms or str(_latest_intent(channel)) != intent_seen:
        _audit("denied", resources, "approval mode confirmation already used or superseded")
        await _ephemeral(channel, user, CONFIRM_STALE_TEXT)
        return True
    _spend_confirm(value)
    await _apply_pick(orch, binding, mode, user, team, channel, via="select")
    return True


async def handle_command(
    orch: Any,
    binding: Binding,
    user: str,
    arg: str,
    respond: Callable[[str], Awaitable[None]],
    *,
    team: str,
) -> None:
    """``/kirocrew approval [<normal|reads|trust>]`` in an authorized code channel."""
    command = _command(orch)
    word = arg.strip().lower()
    if not word:
        slot = channel_session(surface.dashboard_state(), binding)
        _audit("allowed", f"channel={binding.channel_id} user={user} via=command result=shown")
        if slot is None:
            await respond(NO_SESSION_TEXT)
            return
        await respond(CURRENT_TEXT.format(label=label(current_mode(slot)), command=command))
        return
    mode = _BY_WORD.get(word)
    if mode is None:
        _audit("denied", f"channel={binding.channel_id} user={user} via=command", "unknown mode")
        from kiro_crew.slack.format import escape_mrkdwn

        await respond(UNKNOWN_TEXT.format(word=escape_mrkdwn(word[:40]), command=command))
        return
    outcome = await set_channel_mode(binding, mode, user, via="command", orch=orch, team=team)
    await respond(outcome.text)
