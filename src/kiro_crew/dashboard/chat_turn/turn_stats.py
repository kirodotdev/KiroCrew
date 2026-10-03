"""The per-turn stats footer, its first-token clock and the context meter payload."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _ChatSlot,
        _turn_rows,
        row_mid,
        turn_stats_meta,
    )


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
    turn_start_mid: str | None = None,
    reply_mids: "set[str] | None" = None,
) -> bool:
    """Attach per-turn stats to this turn's assistant message's meta.

    Returns whether a row received them, so a caller can tell a saved row from
    a turn that had none (a denied tool with no text), whose recovery still has
    to save it.

    Mirrors ``_flush_file_changes``: the meta lands on the in-memory message
    BEFORE ``_save_slot_to_history`` persists it, and reaches the live UI via
    the ``chat_done`` → ``refreshSlot`` re-fetch (no dedicated WS event). It
    also scopes to the turn's own rows the same way, so the two cannot disagree
    about which message belongs to this turn.

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

    The turn's rows are identified exactly as ``_flush_file_changes`` does, via
    ``_turn_rows``: ``turn_start_mid`` relocates the turn's first row by
    identity, falling back to ``turn_boundary`` (``len(slot.messages)`` at turn
    start) when no id is available. A position stops naming this turn's row the
    moment another writer shifts the window under it — most sharply when a
    regenerate whose turn ended empty schedules a restore that SPLICES the
    removed old reply in ahead of a running follow-up's boundary, so the
    boundary slice would then also cover that restored reply and a plain reverse
    scan would land this turn's stats on it. Scoping by ``turn_start_mid``
    excludes a row spliced before the turn began, and ``reply_mids`` (this
    turn's own reply ids, ``_turn_reply_mids_all``) pins the attach to the
    turn's reply BY IDENTITY within that scope — the same identity door
    ``_flush_file_changes`` anchors its chips to, so an assistant row another
    writer injected mid-turn is not mistaken for this turn's reply.

    ``reply_mids`` is three-valued, and the empty case is NOT the None case.
    ``None`` means the caller supplied no identity signal — the backward-compatible
    path — so the positional reverse scan is the only locator and runs. A SET
    (even empty) means the caller IS asserting this turn's reply identity: a
    non-empty set pins by id, and an EMPTY set means the turn produced no reply
    of its own, so the stats attach to NOTHING. Falling back to the positional
    scan on an empty set would attach an error/refusal-only turn's stats onto
    whatever assistant row happens to be newest in scope — a concurrent
    workflow/sub-agent injection, which is not this turn's output — corrupting a
    foreign row. So the positional fallback is gated on ``reply_mids is None``.
    """
    stats = turn_stats_meta(elapsed_ms, credits, cost_usd, model)
    if stats is None:
        return False
    if ttft_ms > 0:
        stats["ttft_ms"] = int(ttft_ms)
    turn_rows = _turn_rows(slot, turn_boundary, turn_start_mid)
    if reply_mids is not None:
        # The caller asserted this turn's reply identity. Attach ONLY by id — an
        # empty set means the turn produced no reply, so nothing is attached
        # rather than falling onto a foreign assistant row.
        for m in reversed(turn_rows):
            if m.get("role") == "assistant" and row_mid(m) in reply_mids:
                m.setdefault("meta", {})["turn_stats"] = stats
                return True
        return False
    # No identity signal (backward-compatible callers): positional reverse scan,
    # scoped to this turn's rows.
    for m in reversed(turn_rows):
        if m.get("role") == "assistant":
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
