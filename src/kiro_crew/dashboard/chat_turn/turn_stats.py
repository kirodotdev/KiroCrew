"""The per-turn stats footer, its first-token clock and the context meter payload, the
turn outcome, the turn metric and the recovery-outcome counter."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _ChatSlot,
        emit_turn_duration,
        emit_turn_usage,
        get_recorder,
        logger,
        row_mid,
        turn_outcome,
    )
    from kiro_crew.dashboard.chat_turn.mcp_session import _redact_acp_string


def _context_usage_payload(slot_key: str, client: Any) -> dict[str, Any]:
    """Build the ``context_usage`` WS payload: pct plus real token counts.

    The token counts let the frontend ring tooltip show "used / window" in
    absolute tokens (sourced from the adapter's usage_update), so a 44%-of-200k
    reading is not misread as 44%-of-1M.

    When real per-turn token counts are unavailable the payload carries
    ``reset: True`` instead of the ``used_tokens``/``window_tokens`` pair. This
    is load-bearing, not cosmetic: the frontend keeps the percentage and the
    token counts in two independent slices (``slotContextPct`` vs
    ``slotContextTokens``), so a bare ``{slot, pct}`` frame updates the
    percentage while leaving whatever token counts the ring last stored in
    place — a headline that disagrees with the count beside it. Emitting
    ``reset`` whenever ``used`` is unknown — a fresh session before the first
    ``usage_update``, or the post-compaction / post-model-switch state where the
    provider zeroes ``used`` but keeps the window — moves the two fields
    together: the ring drops its stored counts and the meter self-corrects on
    the next turn's telemetry. Harmless when nothing is stored.
    """
    pct = client.context_usage_pct()
    payload: dict[str, Any] = {"slot": slot_key, "pct": round(pct, 1)}
    # Use the provider's public accessors — last_prompt_stats lives on the
    # inner AcpClient, not on the provider, so reaching for it on `client`
    # (the AcpProvider returned by get_or_create) would always miss.
    window = client.context_window_tokens() if hasattr(client, "context_window_tokens") else 0
    # used == 0 means "not measured yet", not "empty context" — it is the
    # post-compaction / post-model-switch state (AcpPromptStats zeroes the
    # counts but keeps the window until the next turn's telemetry). Shipping
    # {used: 0, window: W} would assert a false "0 / W tokens", so we omit the
    # pair and signal a reset instead.
    used = 0
    if window and hasattr(client, "context_used_tokens"):
        used = client.context_used_tokens()
    if window and used:
        payload["used_tokens"] = used
        payload["window_tokens"] = window
    else:
        payload["reset"] = True
    return payload


def _attach_turn_stats(
    slot: "_ChatSlot",
    elapsed_ms: int,
    credits: float,
    cost_usd: float,
    turn_boundary: int = 0,
    model: str = "",
    ttft_ms: int = 0,
) -> bool:
    """Attach per-turn stats to the last assistant message's meta.

    Returns whether a row received them, so a caller can tell a saved row from
    a turn that had none (a denied tool with no text), whose recovery still has
    to save it.

    Mirrors ``_flush_file_changes``: the meta lands on the in-memory message
    BEFORE ``_save_slot_to_history`` persists it, and reaches the live UI via
    the ``chat_done`` → ``refreshSlot`` re-fetch (no dedicated WS event).

    ``elapsed_ms`` is the turn wall clock (or the provider-reported duration
    when available); ``credits`` is kiro-cli's per-turn ``meteringUsage`` sum;
    ``cost_usd`` is claude_code's API-reported cost. ``model`` is what served
    this turn (``read_turn_model``): a concrete id on a pinned session, or the
    bare ``"auto"`` when the turn was handed to Auto and the backend disclosed
    no id for it — Auto's per-turn choice is not on the ACP wire, so ``"auto"``
    is the whole of what can be said truthfully. ``ttft_ms`` is the turn's
    dispatch to first broadcast output latency (``_FirstVisibleClock``; queue
    wait excluded), stored so it is readable without telemetry on.
    Zero/empty fields are omitted so the frontend renders only what the
    provider actually reported.

    ``turn_boundary`` is ``len(slot.messages)`` captured at turn start: only
    messages appended DURING this turn are candidates. Without it, an
    error/refusal-only turn (which appends no assistant message) would walk
    back into the PREVIOUS turn's assistant message and overwrite its stats
    with the failed turn's numbers. No-op when the turn produced no assistant
    message or when there is nothing to show.
    """
    return _attach_owned_turn_stats(
        slot,
        elapsed_ms,
        credits,
        cost_usd,
        turn_boundary=turn_boundary,
        model=model,
        ttft_ms=ttft_ms,
        reply_mids=None,
    )


def _attach_owned_turn_stats(
    slot: "_ChatSlot",
    elapsed_ms: int,
    credits: float,
    cost_usd: float,
    *,
    turn_boundary: int,
    model: str,
    ttft_ms: int,
    reply_mids: list[str] | None,
) -> bool:
    """``_attach_turn_stats``, narrowed to rows the runner in flight appended.

    ``reply_mids`` is ``slot._turn_reply_mids``: a workflow or sub-agent result
    another writer appends into the window mid-turn is skipped, so it never
    takes this turn's figures. ``None`` keeps the plain boundary scan.
    """
    stats = turn_stats_meta(elapsed_ms, credits, cost_usd, model)
    if stats is None:
        return False
    if ttft_ms > 0:
        stats["ttft_ms"] = int(ttft_ms)
    boundary = max(0, turn_boundary)
    for m in reversed(slot.messages[boundary:]):
        if m.get("role") != "assistant":
            continue
        if reply_mids is not None and row_mid(m) not in reply_mids:
            continue
        m.setdefault("meta", {})["turn_stats"] = stats
        return True
    return False


class _FirstVisibleClock:
    """Turn dispatch -> first output that actually reaches the wire, in ms.

    Starts where ``kirocrew.chat.first_token.duration`` starts, when
    ``_run_chat`` begins the turn. A message sent while the slot is busy is
    dispatched when the queue drains, so its wait in the queue is NOT counted:
    that wait is set by the length of the turn ahead of it, not by this turn's
    own path, and the value exists to measure the path.

    Stops on the first NON-EMPTY broadcast, not on chunk receipt: the stream
    redactors can withhold a whole first chunk until a later chunk or a flush,
    so the receipt time would understate what the user waited. ``t0`` None
    (a synthetic or nested prompt) never measures, leaving ``ms`` at 0.
    ``clock`` must be the same clock ``t0`` was read from.
    """

    def __init__(self, t0: float | None, clock: Callable[[], float] = time.monotonic) -> None:
        self._t0 = t0
        self._clock = clock
        self.ms = 0

    def mark(self, wire: str) -> None:
        if wire and self._t0 is not None:
            self.ms = int((self._clock() - self._t0) * 1000.0)
            self._t0 = None


def _turn_clock(
    slot: "_ChatSlot", t0: float | None, *, top_level: bool, recovery_turn: bool
) -> _FirstVisibleClock:
    """The first-token clock a top-level turn measures with.

    Every top-level turn that does not reuse a clock stores the clock it
    starts on the slot. A recovery turn of any rung (a replay, a
    continuation, a requeue of either) skips its stats until one of them
    saves the row, and it starts no clock of its own. So it reuses the slot's
    clock object. If that clock already stopped, it keeps its reading. If it
    never saw output (a tool-only turn), it is still running from the user
    turn's start and stops at the recovery's first broadcast.

    The stored clock is replaced when the next non-recovery turn starts, and
    cleared when a top-level turn's stats actually land on a row (a turn with
    no assistant row keeps it for its recovery). Every recovery turn
    descends from the last non-recovery turn, so a reused clock always
    belongs to the episode it is attached to. Nested prompts neither read nor
    store it.
    """
    if not top_level:
        return _FirstVisibleClock(t0)
    if recovery_turn and slot._carried_ttft_clock is not None:
        return slot._carried_ttft_clock
    clock = _FirstVisibleClock(t0)
    slot._carried_ttft_clock = clock
    return clock


def _turn_outcome(stop_reason: str | None, *, exhausted: bool = False) -> str:
    """Map an EVENT_COMPLETE stop_reason to a low-cardinality turn outcome.

    Thin delegate to :func:`kiro_crew.metrics.turns.turn_outcome`, which is the
    single source of truth shared by every dispatch surface. Kept as a name here
    because this module's own tests and the stop-reason branches below read it,
    and because the mapping is part of what ``_run_chat`` decides (it is the only
    surface that can say ``exhausted``).
    """
    return turn_outcome(stop_reason, exhausted=exhausted)


def _emit_turn_metric(
    duration_ms: int | float | None,
    stop_reason: str | None,
    slot_key: str,
    *,
    elapsed_ms: int | float | None = None,
    exhausted: bool = False,
    usage: object = None,
    model: str = "",
    provider: str = "",
) -> None:
    """Emit this turn's OTEL samples (best-effort).

    Thin delegate to :func:`kiro_crew.metrics.turns.emit_turn_duration` and
    :func:`~kiro_crew.metrics.turns.emit_turn_usage`. The family is emitted for
    EVERY dispatch surface, and by two owners that between them sample each turn
    exactly once — see :mod:`kiro_crew.metrics.turns`.

    ``_run_chat`` DOES call this, and is the only production caller. Its persist
    call passes ``emit_metric=False`` so the shared boundary does not also sample
    the turn, because two things about this surface the boundary cannot serve:
    that persist sits behind ``usage_has_billing`` (a turn that timed out having
    billed nothing writes no row, and the sample must survive that), and only
    here are the EFFECTIVE session key and the spent-recovery-budget
    ``exhausted`` flag available.

    ``usage`` is therefore read HERE rather than left to the boundary: with
    ``emit_metric=False`` the boundary emits nothing at all, so a usage emit that
    lived only there would leave the dashboard — the surface carrying most of the
    traffic — contributing no token or spend samples whatsoever. Fields are read
    defensively because a turn can complete without one (an errored turn's
    ``usage`` may be absent), and ``emit_turn_usage`` drops non-positive values.

    Every other surface is sampled by ``persist_token_record_async`` itself,
    which is what ended this metric being a dashboard-only reading.
    """
    emit_turn_duration(
        duration_ms,
        session_key=slot_key,
        outcome=turn_outcome(stop_reason, exhausted=exhausted),
        elapsed_ms=elapsed_ms,
        model=model,
        provider=provider,
    )
    emit_turn_usage(
        input_tokens=getattr(usage, "input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
        credits=getattr(usage, "credits", 0.0),
        cost_usd=getattr(usage, "cost_usd", 0.0),
        model=model,
        provider=provider,
    )


def _emit_recovery_outcome(mechanism: str, outcome: str, attempts: int) -> None:
    """Emit kirocrew.watchdog.recovery.outcome (best-effort).

    One counter point per RESOLVED recovery cycle, derived from the per-slot
    retry budgets the stop-reason branches already maintain
    (``slot._stale_recovery_retries`` / ``slot._tool_stall_retries``):

    - ``outcome=recovered`` — a synthetic recovery turn completed ``ok`` while
      a budget was armed (emitted at the budget-reset block, which is the one
      place a completed cycle and its attempt count coexist).
    - ``outcome=exhausted`` — the budget hit its cap and the slot surfaced
      "start a new chat" (emitted in the stall branches themselves).

    ``attempt_bucket`` is the attempt count clamped to the budget cap (1-3) —
    a closed enum per the metrics/schema.py cardinality rule, mirroring the
    CLI's ``attempt_number_bucket`` precedent. Single source of truth shared
    with its unit test so the mapping cannot silently drift.
    """
    try:
        get_recorder().counter(
            "kirocrew.watchdog.recovery.outcome",
            attrs={
                "mechanism": mechanism,
                "outcome": outcome,
                "attempt_bucket": max(1, min(int(attempts), 3)),
            },
        )
    except Exception:
        logger.debug("recovery outcome metric emit failed", exc_info=True)


def turn_stats_meta(
    elapsed_ms: int, credits: float, cost_usd: float, model: str = ""
) -> dict[str, Any] | None:
    """The ``meta.turn_stats`` value the footer renders, or None for nothing to show.

    One shape for every writer: a chat turn (``_attach_turn_stats``) and a cron
    run's result row (the gateway passes it to ``inject_cron_result_to_dashboard``).
    Zero/empty fields are omitted so the footer shows only what was reported.
    A non-finite number is omitted too: ``json.dumps`` writes it as a bare
    ``Infinity``/``NaN`` that no browser parses. The provider-reported model id is
    scrubbed like every ACP-controlled string (``_redact_acp_string``) and dropped past ``_MAX_MODEL_ID_LEN``, because the row is retained.
    """
    if elapsed_ms <= 0:
        return None
    stats: dict[str, Any] = {"elapsed_ms": int(elapsed_ms)}
    if math.isfinite(credits) and credits > 0:
        stats["credits"] = round(credits, 4)
    if math.isfinite(cost_usd) and cost_usd > 0:
        stats["cost_usd"] = round(cost_usd, 6)
    if model:
        safe_model = _redact_acp_string(model)
        if len(safe_model) <= _MAX_MODEL_ID_LEN:
            stats["model"] = safe_model
    return stats


# Longest provider-SUPPLIED model id the reactive access-denial fallback will
# adopt. Both ids it handles come off the provider's error -- the rejected one
# from ``exc.rejected_model`` and the replacement from ``exc.advertised`` via
# ``first_advertised_fallback``, which checks only type and emptiness -- and both
# are RETAINED: in the persisted transcript notice, in
# ``slot._active_fallback_model``, and in the session log. So the bound is
# applied at that entry, before anything retains either value, rather than at the
# notice that displays them: a bound at the display site would leave the store
# unbounded.
#
# REFUSED rather than truncated, because the same value is passed to set_model to
# move the live session: a truncated id is a different model, or none. An id this
# long is not a real one, so the guard simply goes false and the terminal
# entitlement error surfaces instead -- the already-bounded path this branch
# exists to improve on.
_MAX_MODEL_ID_LEN = 256
