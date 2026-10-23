"""The conductor context cap (``docs/request-for-change/rfc-conductor-context-cap.md``).

A goal conductor's KAS chat over its marks is trimmed at a dashboard turn's start,
before the turn claims its session: the KAS conversation ends through
``discard_conversation(replay=True)``, and the session the turn then starts is seeded
with a trim note, the session-ledger snapshot and a longer text tail of the transcript.
No model call is spent, and the transcript on disk gains only the notice row.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_utils import _broadcast_expired_oauth_banners

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        KiroCrewConfig,
        _ChatSlot,
        append_and_surface,
        logger,
        subagents_attached_async,
    )

#: Where a trim lands, in tokens. A cold request there takes about as long as a warm
#: one at 400K.
CONTEXT_CAP_LOW_TOKENS = 150_000
#: Rule A: a session over this at a turn's start is trimmed, warm cache or not.
CONTEXT_CAP_HIGH_TOKENS = 400_000
#: Rule B: a session over this whose previous turn ended a cache life ago is trimmed.
CONTEXT_CAP_IDLE_TOKENS = CONTEXT_CAP_LOW_TOKENS + 50_000
#: The prompt-cache life Rule B measures the gap against, in seconds.
CONTEXT_CAP_CACHE_SECS = 300.0
#: A session a trim started is not trimmed again until its reading has grown this far
#: past its first one, so a trim that lands high cannot repeat on every cold turn.
CONTEXT_CAP_DAMPING_TOKENS = 50_000
#: The trim's replay budget in chars: the room LOW leaves after a fresh conductor
#: session's ~55K-token start and ~5K for the note, the snapshot and the message, at
#: 3 chars per token. Every other cold start keeps ``_REPLAY_BUDGET_CHARS``.
CONTEXT_CAP_TAIL_CHARS = 270_000
#: Trims remembered at once, one per conductor chat key; past it the oldest goes.
_CONTEXT_CAP_MAX_TRIMS = 256

#: Opens a trimmed session's replay: what the model needs to know about the trim.
CONTEXT_CAP_TRIM_NOTE = (
    "[Context trimmed: this chat moved to a new session at the start of this turn, to "
    "keep its context small. Its newest messages follow as text. Tool calls, their "
    "output and older messages are not in this context. They are in this chat's "
    "transcript: search_chat_history searches it and get_chat_session reads it.]"
)

#: The notice row a trim leaves in the transcript, for the person. It names only the
#: reading that fired: where the new session lands is known after its first request.
#: The restart notices' memory sentence follows it.
CONTEXT_CAP_NOTICE_TEXT = (
    "♻️ Context reached {reached}K tokens, so the session was restarted on a shorter context."
)


@dataclass
class _CapTrim:
    """What the cap remembers about the session one of its trims started."""

    #: The next cold start's replay is the trim's seed. Spent only once a provider is
    #: known to hold it, so a cold start that fails before the provider keeps its
    #: prompt, or whose session dies first, is rebuilt from the same seed.
    seed_pending: bool = True
    #: A replay was built from the seed.
    seeded: bool = False
    #: The session the trim started and its reading, both taken at the first turn
    #: start that reads it. The damping guard counts from that reading.
    sid: str = ""
    first_tokens: int = 0


#: Session key -> the trim that started the session now on that key.
_CONTEXT_CAP_TRIMS: dict[str, _CapTrim] = {}


async def _cap_conductor_context(
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
    cfg: KiroCrewConfig | None,
    *,
    is_slash: bool,
) -> str:
    """Trim *slot*'s conductor KAS chat when it is over a mark, before the turn claims it.

    Runs at a dashboard turn's start, beside the pending-reset consume. Returns
    ``"trimmed"`` or the hold that stopped the trim (RFC section 3.4); every hold
    leaves the session as it is, and the next turn start checks again. Never raises:
    a failure costs the trim, never the turn.
    """
    try:
        return await _context_cap_trim(state, slot, session_key, cfg, is_slash=is_slash)
    except Exception:
        logger.warning(
            "Context cap: check failed for slot %s; the turn keeps its session",
            slot.key,
            exc_info=True,
        )
        return "error"


async def _context_cap_trim(
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
    cfg: KiroCrewConfig | None,
    *,
    is_slash: bool,
) -> str:
    from kiro_crew.autonudge_selfarm import WAKE_RESET_AGENTS
    from kiro_crew.dashboard.session_directive_apply import _slot_has_pending_interaction

    _settle_context_cap_seed(state, session_key)
    # An unreadable config reads as off: a trim is never the fallback.
    if cfg is None or not cfg.session.context_cap:
        return "cap_off"
    if str(slot.agent or "") not in WAKE_RESET_AGENTS:
        return "out_of_scope"
    if is_slash:
        return "slash_command"
    provider = state.sessions.get_provider(session_key)
    if provider is None:
        return "no_session"
    backend = getattr(provider, "harness_managed_compaction_backend", None)
    if not (isinstance(backend, str) and backend):
        return "out_of_scope"
    tokens = _context_cap_reading(provider)
    if tokens is None:
        return "unknown_reading"
    floor = _context_cap_damping_floor(state, session_key, tokens)
    rule = _context_cap_rule(tokens, _context_cap_idle_secs(state, session_key))
    if rule is None:
        return "below_marks"
    if floor is not None and tokens < floor:
        hold = "damped"
    elif slot._pending_discard_conversation_key:
        # The queued reset lands at this turn's end and drops more than a trim.
        hold = "reset_queued"
    elif _slot_has_pending_interaction(state, slot):
        # The answer would reach a session without the question in its context.
        hold = "interaction_pending"
    elif await subagents_attached_async(state, slot, session_key, "context_cap"):
        # The teardown releases the runtime the children run on.
        hold = "subagents_attached"
    elif not await _context_cap_discard(state, slot, session_key):
        # Another turn holds the session; the refusal changed nothing.
        hold = "busy"
    else:
        hold = ""
    if hold:
        logger.info(
            "Context cap: slot %s at %dK of context (rule %s) left on its session: %s",
            slot.key,
            tokens // 1000,
            rule,
            hold,
        )
        return hold
    _remember_context_cap_trim(session_key)
    _append_context_cap_notice(state, slot, tokens)
    logger.info(
        "Context cap: slot %s at %dK of context (rule %s) trimmed; this turn starts a new session",
        slot.key,
        tokens // 1000,
        rule,
    )
    return "trimmed"


def _context_cap_rule(tokens: int, idle_secs: float | None) -> str | None:
    """The rule that trims a session at *tokens* whose previous turn ended *idle_secs* ago.

    Rule A: over HIGH. Rule B: over the Rule B line once the cache life has passed,
    when the next request writes a new cache either way. ``None``: neither holds.
    """
    if tokens > CONTEXT_CAP_HIGH_TOKENS:
        return "A"
    if idle_secs is not None and idle_secs >= CONTEXT_CAP_CACHE_SECS:
        if tokens > CONTEXT_CAP_IDLE_TOKENS:
            return "B"
    return None


def _context_cap_reading(provider: Any) -> int | None:
    """The session's last confirmed context reading in tokens, or ``None`` when unknown.

    The reading is the previous turn's last request: the percentage times the window.
    A reading the provider calls unknown, a zero one and one with no window never trim.
    """
    from kiro_crew.context import window_for_provider_client  # circular: context -> chat
    from kiro_crew.session_parts.provider_probes import _context_pct_is_unknown

    if _context_pct_is_unknown(provider):
        return None
    pct = provider.context_usage_pct()
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct):
        return None
    window = window_for_provider_client(provider)
    if pct <= 0 or not window:
        return None
    return int(pct * window / 100)


def _context_cap_idle_secs(state: DashboardState, session_key: str) -> float | None:
    """Seconds since the session's previous turn ended, or ``None`` when unknown.

    Read off ``_Session.last_used``: ``SessionAllocationService.release`` stamps it at a
    turn's end, and this check runs before the next turn acquires, so it holds that end.
    Every other writer stamps a later moment, which can only read the cache warmer.
    """
    record = getattr(state.sessions, "_sessions", {}).get(session_key)
    last_used = getattr(record, "last_used", None)
    if isinstance(last_used, bool) or not isinstance(last_used, (int, float)):
        return None
    return time.monotonic() - last_used


def _context_cap_damping_floor(state: DashboardState, session_key: str, tokens: int) -> int | None:
    """The reading a session a trim started must reach before another trim, or ``None``.

    The first turn start that reads the session after its trim names it (its sid) and
    takes its reading. A session that replaced it since is not damped.
    """
    trim = _CONTEXT_CAP_TRIMS.get(session_key)
    if trim is None:
        return None
    sid = state.sessions.mapped_sid(session_key)
    if not isinstance(sid, str) or not sid:
        return None
    if not trim.sid:
        trim.sid, trim.first_tokens = sid, tokens
    elif trim.sid != sid:
        _CONTEXT_CAP_TRIMS.pop(session_key, None)
        return None
    return trim.first_tokens + CONTEXT_CAP_DAMPING_TOKENS


async def _context_cap_discard(state: DashboardState, slot: _ChatSlot, session_key: str) -> bool:
    """End the slot's KAS conversation with replay kept on; False when a turn holds it.

    ``skip_if_busy`` refuses under the same session lock that pops the session, so a
    turn that starts between the checks above and the teardown is never cut.
    """
    try:
        discarded = await state.sessions.discard_conversation(
            session_key, replay=True, skip_if_busy=True
        )
    except Exception:
        # A teardown that raised leaves a session this slot cannot vouch for.
        slot.forget_session_model_state()
        raise
    if not discarded:
        return False
    # The discarded conversation advertised the model list and the MCP report.
    slot.forget_session_model_state()
    if slot.clear_mcp_report():
        state.broadcast_ws("mcp_report_update", {"slot": slot.key, "mcp_report": None})
    _broadcast_expired_oauth_banners(state, slot)
    return True


def _remember_context_cap_trim(session_key: str) -> None:
    _CONTEXT_CAP_TRIMS.pop(session_key, None)
    while len(_CONTEXT_CAP_TRIMS) >= _CONTEXT_CAP_MAX_TRIMS:
        _CONTEXT_CAP_TRIMS.pop(next(iter(_CONTEXT_CAP_TRIMS)))
    _CONTEXT_CAP_TRIMS[session_key] = _CapTrim()


def _append_context_cap_notice(state: DashboardState, slot: _ChatSlot, tokens: int) -> None:
    """One notice row per trim, drawn by the compaction card; the replay skips it."""
    from kiro_crew.context_assembly.replay import CONTEXT_CAP_NOTICE
    from kiro_crew.dashboard.state import _RESTART_MEMORY_TAIL

    text = CONTEXT_CAP_NOTICE_TEXT.format(reached=round(tokens / 1000))
    try:
        append_and_surface(
            state,
            slot,
            "assistant",
            f"{text} {_RESTART_MEMORY_TAIL}",
            "msg msg-a",
            meta={"kind": "compaction", "notice": CONTEXT_CAP_NOTICE},
        )
    except Exception:
        logger.debug("Context cap: notice row failed for slot %s", slot.key, exc_info=True)


def _spend_context_cap_seed(session_key: str) -> None:
    """The turn that built a trim's seed delivered its first-turn history: spend the seed.

    Called where ``_turn_settle_debts`` (``chat_turn/prompt_assembly.py``) settles the
    first-turn history debt, the one point that knows the provider kept the prompt,
    before anything between turns can end the session.
    """
    trim = _CONTEXT_CAP_TRIMS.get(session_key)
    if trim is not None and trim.seeded:
        trim.seed_pending = False


def _settle_context_cap_seed(state: DashboardState, session_key: str) -> None:
    """Spend a trim's seed once a live session is seen holding it: built, and not owed.

    A session that is gone proves nothing either way (it may have died before its
    provider kept the prompt), so the seed waits for the next cold start.
    """
    trim = _CONTEXT_CAP_TRIMS.get(session_key)
    if (
        trim is not None
        and trim.seed_pending
        and trim.seeded
        and state.sessions.get_provider(session_key) is not None
        and state.sessions.first_turn_history_owed_pending(session_key) is not True
    ):
        trim.seed_pending = False


def _take_context_cap_seed(session_key: str) -> bool:
    """Whether the replay this cold start builds is a trim's seed."""
    trim = _CONTEXT_CAP_TRIMS.get(session_key)
    if trim is None or not trim.seed_pending:
        return False
    trim.seeded = True
    return True


def _drop_context_cap_seed(session_key: str) -> None:
    """A cold start a reset told to forget spends the trim's seed without building it."""
    trim = _CONTEXT_CAP_TRIMS.get(session_key)
    if trim is not None:
        trim.seed_pending = False


async def _context_cap_seed_head(session_key: str) -> str:
    """The trim note and the session-ledger snapshot that open a trimmed session's replay.

    The snapshot is the block a patrol wake already carries (``compose_nudge_body``),
    read off the loop; a failed read costs the snapshot, never the seed.
    """
    from kiro_crew.session_ledger import ledger_key, render_snapshot

    try:
        snapshot = await asyncio.to_thread(render_snapshot, ledger_key(session_key))
    except Exception:
        logger.debug("Context cap: ledger snapshot failed for %s", session_key, exc_info=True)
        snapshot = ""
    head = CONTEXT_CAP_TRIM_NOTE + "\n\n"
    if snapshot:
        head += snapshot + "\n\n"
    return head
