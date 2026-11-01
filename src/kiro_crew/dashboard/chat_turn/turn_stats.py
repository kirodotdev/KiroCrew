"""The per-turn stats footer, its first-token clock, the first-token latency metric
(``_emit_ttft_metric``) and the context meter payload, the turn outcome, the turn metric and
the recovery-outcome counter, the stream's terminal (``_on_complete``) and the turn's context
usage and landed verdict after it (``_turn_context_usage``), and the usage row of a turn that
ends without ``EVENT_COMPLETE`` (``_persist_abnormal_turn_usage``)."""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING, Any, Callable, Literal

from kiro_crew.dashboard.state import row_mid
from kiro_crew.dashboard.system_notices import is_system_notice
from kiro_crew.llm_helpers import (
    _NO_PRIOR_STATS,
    provider_active_model,
    record_interaction_event,
    usage_has_billing,
)
from kiro_crew.metrics.events import TURN_TIMEOUT_CAUSE, emit_counter
from kiro_crew.metrics.harness import HARNESS_UNKNOWN, harness_of
from kiro_crew.metrics.turns import emit_turn_duration, emit_turn_usage, turn_outcome
from kiro_crew.metrics.usage_attrs import TRIGGER_OTHER, trigger_for_dashboard_turn
from kiro_crew.monitoring.completion import (
    disposition_for_stop_reason,
    is_monitor_completion_evidence,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        STOP_REASON_CANCELLED,
        STOP_REASON_END_TURN,
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TOOL_STALL,
        STOP_RECOVERY_MAX_RETRIES,
        DashboardState,
        RefusalInfo,
        TurnUsage,
        _ChatSlot,
        capabilities_of,
        classify_stop_reason,
        get_recorder,
        logger,
        note_coding_activity,
        persist_token_record_async,
        provider_last_turn_usage,
        read_context_tokens,
        read_effective_agent,
        read_turn_model,
        telemetry_channel_of,
    )
    from kiro_crew.dashboard.chat_turn.file_changes import _turn_rows
    from kiro_crew.dashboard.chat_turn.mcp_session import _redact_acp_string
    from kiro_crew.dashboard.chat_turn.model_fallback import _backfill_canonical_model
    from kiro_crew.dashboard.chat_turn.native_cards import (
        NATIVE_SUBAGENT_INTERRUPTED_ERROR,
        _native_subagent_close_all,
    )
    from kiro_crew.dashboard.chat_turn.tool_rows import _MAX_TCID_SOURCES
    from kiro_crew.dashboard.chat_turn.turn_ledgers import _report_consumed, _wt_collect
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn, _Flow


def _last_reply_row(
    slot: "_ChatSlot", turn_boundary: int, turn_start_mid: str | None
) -> dict[str, Any] | None:
    """The last reply row the runner appended this turn, or None when it appended none.

    The one identity walk, which ``_attach_file_changes`` also calls to place the
    turn's file chips: only rows this turn appended (``_turn_rows``), and among
    them only a reply the runner recorded (``slot._turn_reply_mids``), because
    an assistant row another writer injects mid-turn, such as a workflow or
    sub-agent completion envelope, is not this turn's reply. A system notice
    (``is_system_notice``: a compaction or session-reload status row) is written
    under the assistant role but is not a reply either: a failed compaction
    appends its notice after the text the user read. A slot that tracks no
    identities (a test double without the list) keeps the position rule.
    """
    reply_mids = getattr(slot, "_turn_reply_mids", None)
    for m in reversed(_turn_rows(slot, turn_boundary, turn_start_mid)):
        if m.get("role") != "assistant" or is_system_notice("assistant", m.get("meta")):
            continue
        if isinstance(reply_mids, list) and row_mid(m) not in reply_mids:
            continue
        return m
    return None


def _completed_reply_row(
    slot: "_ChatSlot", turn_boundary: int, turn_start_mid: str | None
) -> dict[str, Any] | None:
    """The row a completed turn's footer lands on.

    ``_last_reply_row`` once the runner recorded a reply this turn, so a workflow
    or sub-agent envelope another writer appended after the reply does not take
    the turn's figures. A turn that recorded none keeps the position rule, the
    newest assistant row of the turn that is not a system notice: a slash
    command's confirmation is appended as a plain assistant row, and a slot
    double built without the list records nothing.
    """
    reply_mids = getattr(slot, "_turn_reply_mids", None)
    if isinstance(reply_mids, list) and reply_mids:
        return _last_reply_row(slot, turn_boundary, turn_start_mid)
    for m in reversed(_turn_rows(slot, turn_boundary, turn_start_mid)):
        if m.get("role") == "assistant" and not is_system_notice("assistant", m.get("meta")):
            return m
    return None


def _broadcast_turn_stats(
    state: "DashboardState", slot: "_ChatSlot", row: dict[str, Any], stats: dict[str, Any]
) -> None:
    """Send the footer just put on ``row`` to open clients.

    A turn that ends delivers its footer through the refresh its ``chat_done``
    triggers. An abnormal end can hand the slot straight to a queued recovery,
    which sends no ``chat_done`` until it finishes, so the stats go out on the
    ``chat_message_update`` patch, which the client merges into the row's meta.
    Keyed on the row's ``mid`` where it has one, since two rows can share a
    ``ts``. Best-effort: a client that misses it reads the stats from the next
    refresh.
    """
    meta = row.get("meta")
    mid = meta.get("mid") if isinstance(meta, dict) else None
    ts = str(row.get("ts") or "")
    if not ts and not (isinstance(mid, str) and mid):
        return
    payload: dict[str, Any] = {"slot": slot.key, "ts": ts, "meta": {"turn_stats": stats}}
    if isinstance(mid, str) and mid:
        payload["mid"] = mid
    try:
        state.broadcast_ws("chat_message_update", payload)
    except Exception:
        logger.debug("Broadcasting the reply footer failed for slot %s", slot.key, exc_info=True)


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
    retries: int = 0,
) -> bool:
    """Attach per-turn stats to the turn's last reply row.

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

    ``turn_boundary`` is ``len(slot.messages)`` captured at turn start and
    ``turn_start_mid`` the identity of the row at the window's tail then: only
    messages appended DURING this turn are candidates (``_turn_rows``). Without
    the boundary, an error/refusal-only turn (which appends no assistant
    message) would walk back into the PREVIOUS turn's assistant message and
    overwrite its stats with the failed turn's numbers. Among the turn's rows
    the footer lands on the reply the runner recorded (``_completed_reply_row``),
    not on a workflow or sub-agent envelope another writer appended after it,
    and never on a system notice. No-op when the turn produced no assistant
    message or when there is nothing to show.

    ``retries`` is how many times the user turn was re-sent after a pre-output
    transient error (``slot._turn_retries``), recorded only when positive.
    """
    stats = turn_stats_meta(elapsed_ms, credits, cost_usd, model)
    if stats is None:
        return False
    if ttft_ms > 0:
        stats["ttft_ms"] = int(ttft_ms)
    if retries > 0:
        stats["retries"] = int(retries)
    row = _completed_reply_row(slot, turn_boundary, turn_start_mid)
    if row is None:
        return False
    row.setdefault("meta", {})["turn_stats"] = stats
    return True


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
    harness: str = HARNESS_UNKNOWN,
    trigger: str = TRIGGER_OTHER,
) -> None:
    """Emit this turn's OTEL samples (best-effort).

    ``trigger`` is who started the turn (:func:`_dashboard_turn_trigger`).

    ``harness`` labels the duration sample only (see
    :func:`kiro_crew.metrics.turns.emit_turn_duration`); the caller resolves it
    from the turn's live client.

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
        harness=harness,
        trigger=trigger,
    )
    emit_turn_usage(
        input_tokens=getattr(usage, "input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
        credits=getattr(usage, "credits", 0.0),
        cost_usd=getattr(usage, "cost_usd", 0.0),
        model=model,
        provider=provider,
    )


def _dashboard_turn_trigger(turn: object) -> str:
    """``trigger_source`` for a dashboard turn; never raises.

    Read from the actor the turn entry bound (``turn._crew_log_actor``). A
    ``user`` actor is the entry's fallback for a turn no dispatch claimed, so it
    counts as ``user`` only when the turn also carries a person's send -- the
    authenticated-human or channel-origin directive -- and is not a queue row
    restored after a restart, whose provenance is gone. Otherwise ``other``.
    """
    try:
        return trigger_for_dashboard_turn(
            getattr(turn, "_crew_log_actor", None),
            user_origin=bool(getattr(turn, "_directive_user_origin", False)),
            channel_origin=bool(getattr(turn, "_directive_channel_origin", False)),
            provenance_restored=bool(getattr(turn, "_turn_provenance_restored", False)),
        )
    except Exception:
        return TRIGGER_OTHER


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


async def _on_complete(turn: _ChatTurn, event) -> Literal[_Flow.BREAK] | None:
    """The stream's terminal: the turn's stats, usage row and metric, and its stop reason."""
    _crew_log_terminal: "dict | None"
    _turn_refusal: RefusalInfo | None
    _stop_reason: str
    _native_card_output = turn._native_card_output
    _native_tracker = turn._native_tracker
    _prompt_depth = turn._prompt_depth
    _stall_command = turn._stall_command
    _stall_evidence = turn._stall_evidence
    _stall_status = turn._stall_status
    _stall_tool_title = turn._stall_tool_title
    _turn_cost_usd = turn._turn_cost_usd
    _turn_credits = turn._turn_credits
    _turn_project = turn._turn_project
    _turn_provenance_restored = turn._turn_provenance_restored
    _turn_t0 = turn._turn_t0
    _wt_pending_coding = turn._wt_pending_coding
    _wt_pending_dropped = turn._wt_pending_dropped
    agent_model = turn.agent_model
    client = turn.client
    monitor_completion = turn.monitor_completion
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    # Latch the terminal reason the INSTANT the completion arrives,
    # before any cancellable await in this arm runs. The full
    # assignment (with the slot mirror and the tool-stall/timeout
    # refinements) still happens below at its original site; this is
    # only the earliest-possible capture of the reason itself.
    #
    # Why it must be here and not there: the arm then runs several
    # awaits that can be cancelled mid-flight -- `_report_consumed()`,
    # the wakatime `asyncio.to_thread` collect, the shielded partial
    # persist -- and the wall-clock turn ceiling (or a hard Stop that
    # surfaces as a bare `CancelledError`) can fire during any of
    # them. If the cancel lands before the lower assignment, `_stop_reason`
    # would still be `""` in _run_chat's finally even though this was a
    # genuinely CANCELLED completion. The first-turn history-debt
    # settle keys `_turn_emitted and _stop_reason != STOP_REASON_CANCELLED`
    # off that value: an un-latched `""` would read as "not cancelled"
    # and settle the debt on a turn kiro-cli DISCARDS, so the next warm
    # turn goes out without the history the cancelled prompt carried
    # (a data loss). Latching here closes that window:
    # a cancelled completion sets `_stop_reason = STOP_REASON_CANCELLED`
    # before the first await, so the settle correctly stays armed.
    if event.stop_reason:
        _stop_reason = event.stop_reason
        turn._stop_reason = _stop_reason
    # A turn that ran to a real END OF TURN processed this prompt, so it
    # was consumed even if it produced nothing at all -- an empty
    # response re-queues a CONTINUATION, not a replay, so whoever armed
    # this turn must not keep waiting for a delivery that happened.
    #
    # Only that stop reason. The same event also carries the reasons
    # that CUT a turn short -- stale-recover, tool-stall, cancelled, an
    # unrecognised provider error -- and those re-queue the prompt
    # itself, so reporting consumption for them would start the
    # retention clock on a result the retry still has to deliver. An
    # absent stop reason is deliberately not treated as end-of-turn
    # either: a provider that streamed anything has already reported
    # through the token/tool triggers, and the cost of being wrong here
    # is asymmetric (a duplicate re-announce versus a pruned result).
    if event.stop_reason == STOP_REASON_END_TURN:
        await _report_consumed(turn)
        # A coding key still held here reached no permission
        # decision, so it executed without a gating denial and
        # counts as activity. One bounded summary for any keys the
        # cap shed this turn, instead of a line per drop.
        if _wt_pending_coding:
            turn._wt_coded_this_turn = True
        if _wt_pending_dropped:
            logger.warning(
                "wakatime pending-coding cap (%d) shed %d tool key(s) this turn",
                _MAX_TCID_SOURCES,
                _wt_pending_dropped,
            )
        # A restricted (incognito/temporary) session persists no
        # durable state, and a heartbeat is an external, irreversible
        # write of session metadata (project label, token and
        # line-change deltas). Gate on the same slot.is_restricted
        # signal the artifact and history writers key off, so the
        # privacy-mode guarantee holds for this path too. Also skip
        # app-owned slots (slot._app): the config help scopes this
        # feature to the gateway chat loop and states the app /
        # task-runner exclusion as deliberate, and a persistent
        # app slot would otherwise emit despite that. Finally, only
        # interactive user turns emit: cron, autonudge, sub-agent,
        # and other injected turns are outside the declared scope.
        # Restored queue rows have lost their actor stamp, so their
        # explicit provenance guard keeps the user fallback from
        # misclassifying a gateway-restart replay as interactive.
        if (
            turn._wt_coded_this_turn
            and turn._crew_log_actor == "user"
            and not _turn_provenance_restored
            and not slot.is_restricted
            and not slot._app
        ):
            _wt_out = await asyncio.to_thread(_wt_collect, turn)
            if _wt_out is not None:
                _wt_cfg, _wt_line_changes = _wt_out
                _wt_usage = event.usage
                # The turn's own project snapshot: a set_project
                # directive or a chip pick can move slot.project
                # while this turn streams, and the heartbeat
                # describes coding done in this turn.
                note_coding_activity(
                    _turn_project or None,
                    ai_input_tokens=getattr(_wt_usage, "input_tokens", 0) or 0,
                    ai_output_tokens=getattr(_wt_usage, "output_tokens", 0) or 0,
                    ai_line_changes=_wt_line_changes,
                    config=_wt_cfg,
                )
    # Turn-end diagnostics. Read only from `event`, which nothing in
    # this arm mutates, so the position is free — kept below the
    # consumption gate because that gate's adjacency to the arm's start
    # is pinned by test_subagent_delivery_ttl_anchor.
    # `synthetic_completion` is readable ONLY here, and the
    # empty-response verdict that needs it runs in _turn_settle_answer.
    _saw_terminal_event = True
    turn._saw_terminal_event = _saw_terminal_event
    _terminal_synthetic = bool(event.synthetic_completion)
    turn._terminal_synthetic = _terminal_synthetic
    _turn_billed = usage_has_billing(event.usage)
    turn._turn_billed = _turn_billed
    # Hang-attribution snapshot BEFORE the close-all safety net
    # below force-marks every card done: only children still
    # unfinished at the cut may count toward timeout attribution
    # (terminal entries linger in the tracker for reconnect
    # replay and would corrupt the series).
    _children_unfinished = any(not _i.get("done") for _i in _native_tracker.values())
    # Safety net: complete any native subagent cards still marked
    # running at turn end (in case a terminal status was missed),
    # so cards don't stay stuck "running". Only a raw end_turn counts
    # as a normal landing; a cancel, refusal or synthesized completion
    # cut its children off, so their cards close as interrupted.
    _landed_normally = event.stop_reason == STOP_REASON_END_TURN and not event.synthetic_completion
    _native_subagent_close_all(
        state,
        slot,
        _native_tracker,
        _native_card_output,
        error=None if _landed_normally else NATIVE_SUBAGENT_INTERRUPTED_ERROR,
    )
    _u = event.usage
    if monitor_completion is not None and is_monitor_completion_evidence(
        event.stop_reason,
        synthetic=event.synthetic_completion,
    ):
        try:
            await monitor_completion.complete(
                disposition_for_stop_reason(event.stop_reason),
                event.usage,
            )
        except Exception:
            logger.warning(
                "dashboard monitor turn completion callback failed",
                exc_info=True,
            )
    # Capture per-turn stats for the assistant-message footer.
    # Prefer the provider-reported duration (claude_code) over the
    # local wall clock (kiro/acp reports duration_ms=0).
    try:
        _turn_elapsed_ms = int(_u.duration_ms or (time.monotonic() - _turn_t0) * 1000)
        turn._turn_elapsed_ms = _turn_elapsed_ms
        _turn_credits = float(_u.credits or 0.0)
        turn._turn_credits = _turn_credits
        _turn_cost_usd = float(_u.cost_usd or 0.0)
        turn._turn_cost_usd = _turn_cost_usd
    except (TypeError, ValueError):
        _turn_elapsed_ms = int((time.monotonic() - _turn_t0) * 1000)
        turn._turn_elapsed_ms = _turn_elapsed_ms
    # Model attribution for the footer. read_turn_model reports the
    # id the backend actually served, or the bare "auto" when the
    # turn was handed to Auto and no concrete id came back — the
    # two are different facts and a blank footer conflates them
    # with a missing measurement. Still never guesses: an
    # unattributable turn stays "" and the footer omits the field.
    _turn_model = read_turn_model(client)
    turn._turn_model = _turn_model
    _turn_stats_captured = True
    turn._turn_stats_captured = _turn_stats_captured
    # ── Turn outcome for this turn's histogram sample ──
    # ``exhausted`` mirrors the stop-reason branches of _turn_stop_recovery: the
    # recovery-outcome exclusion from fault_rate is earned only by a
    # turn that is actually re-driven in place, so a stall takes the
    # terminal stall_exhausted label when its 3-attempt budget is
    # already spent ("Session stuck") OR when it is a NESTED turn
    # (depth > 0), which those branches never re-queue — it dies
    # with "please retry", a user-visible fault that must reach
    # fault_rate. Only this surface maintains such a budget; every
    # other surface's outcome comes from its stop reason alone.
    #
    # Computed ABOVE the billing gate deliberately: both the persist
    # call inside it and the unconditional emit below read it, and a
    # zero-billing timeout takes the second path only.
    if event.stop_reason == STOP_REASON_STALE_RECOVER:
        _turn_exhausted = (
            _prompt_depth > 0 or slot._stale_recovery_retries >= STOP_RECOVERY_MAX_RETRIES
        )
    elif event.stop_reason == STOP_REASON_TOOL_STALL:
        _turn_exhausted = _prompt_depth > 0 or slot._tool_stall_retries >= STOP_RECOVERY_MAX_RETRIES
    else:
        _turn_exhausted = False
    # Model + provider for the row AND the metric attributes, resolved
    # ABOVE the billing gate for exactly the reason _turn_exhausted
    # is: the unconditional emit below reads them, and a zero-billing
    # turn (a timeout, a cancel) reaches only that path — reading names
    # bound inside the gate would raise NameError on the one
    # population whose latency matters most. Both reads are cheap and
    # side-effect-free; the CC late-backfill that WRITES slot.model
    # stays behind the gate below.
    #
    # The provider is the SERVED backend, read off the live client,
    # NOT `cfg.agent.provider`: that field is declared `enum=["acp"]`
    # and `validate_config_data` deletes an out-of-enum value, so it is
    # a constant naming no backend at all. Reading it labelled every
    # dashboard turn "acp" — a claude_code turn included — which makes
    # a provider split answer nothing.
    #
    # Read as `SessionCapabilities.provider_seam`, the label the
    # agent-SDK boundary now exposes for this question. It replaced an
    # `is_claude_backend(client)` ternary that had to be spelled that
    # way because `providers.acp.provider_label` is behind a forbidden
    # root: `scripts/check_agent_sdk_boundary.py` rejects an ACP-layer
    # import on any line a change touches, baselined file or not, so
    # reaching the real label helper was a hard gate failure. Asking
    # the SDK is the sanctioned route the old comment here pointed at.
    #
    # Known residue, deliberately UNCHANGED by that move: this still
    # cannot name the KAS backend, so a KAS turn labels "acp". Only the
    # ACP layer's own `PROVIDER_LABEL_*` constants distinguish it, and
    # promoting that distinction would change what every KAS turn
    # records — a telemetry change, not a refactor, so it belongs in
    # its own commit with its own reason.
    #
    # A fallback model serving the turn records the model that RAN,
    # not the one the user pinned: billing a model that never
    # executed is wrong.
    #
    # Read from the LIVE client rather than from
    # `slot._active_fallback_model` directly, because that sticky
    # field can outlive the swap it records. It deliberately survives
    # a landed turn (the session stays on the fallback until the
    # start-of-turn restore probe succeeds), and that probe is skipped
    # for a synthetic recovery message — so a turn that reset the
    # session and resumed it on `slot.model` runs on the PRIMARY with
    # the sticky candidate still set. Recording the candidate there
    # would bill a model that, again, never executed.
    #
    # `provider_active_model` reads the provider's own
    # `served_model` / `_model` accessor directly: the live
    # provider is the freshest witness to what served THIS turn,
    # and passing it as the caller-side model makes the row correct
    # by construction — `_resolve_model` short-circuits on a
    # non-blank caller-side model. `persist_token_record_async`'s
    # `model_source` walk is the independent second witness, not
    # the row's only source: blanking here and leaving recovery to
    # another module's traversal of the wrapper graph makes this
    # turn's attribution hostage to that graph's shape, and a row
    # that lands `"model": ""` is rendered as `unknown` at read
    # time, merging this turn's credits with genuinely
    # attribute-less rows.
    #
    # An unreadable provider still yields `""` and falls through to
    # the `model_source` walk — no worse than the blank it would
    # write anyway.
    _provider_name = capabilities_of(client).provider_seam
    _record_model = slot.model
    # A model pick can land on the slot mid-turn: api_chat_slot_model's
    # deferred branch writes slot.model and marks _deferred_model_switch_key
    # for the turn boundary to apply. The live session is NOT switched then, so
    # THIS turn still ran on what the client serves while slot.model already
    # names the NEXT turn's model. Attribute the row to the served id, not the
    # pending pin, and the backfill below additionally refuses to WRITE
    # slot.model — which would overwrite the pending pick, replacing an Auto
    # pick's "" with the served id and silently undoing it.
    _deferred_model_pick = bool(slot._deferred_model_switch_key)
    if _deferred_model_pick:
        _record_model = _turn_model or provider_active_model(client)
    # Occupancy is read ABOVE the billing gate, beside the other locals
    # the turn closer needs. Two reasons, and either alone decides it:
    # the closer runs for EVERY completed turn, so a name bound only
    # inside the gate is unbound on a turn that billed nothing (a fake
    # backend, an unmetered provider) and the completion path raises;
    # and occupancy is not a billing quantity -- a turn that costs
    # nothing still filled the window, so gating the read would drop a
    # real measurement for the turns least likely to have one recorded
    # elsewhere. `read_context_tokens` never raises and answers (0, 0)
    # for a provider without the accessors.
    _ctx_used, _ctx_window = read_context_tokens(client)
    if slot._active_fallback_model or slot._refusal_fallback_primary:
        # Either fallback mechanism active ⇒ the model that SERVED
        # this turn is the provider's, not the pin; attribute usage
        # to it without ever writing it into slot.model.
        _record_model = provider_active_model(client)
    # One shared predicate across every persist gate: a
    # claude-seam turn ending via a synthetic EVENT_COMPLETE
    # (timeout, tool-stall, cancel-unacked) can carry cost or cache
    # tokens with zero fresh tokens and zero credits, and the
    # footer above already reads _u.cost_usd for the same event.
    #
    # ── Turn-completion histogram (OTel M2) ──
    # kirocrew.turn.duration → turn latency p50/p90 + fault rate.
    # Emitted HERE, BEFORE the cancellable persist await below, and
    # UNCONDITIONALLY (outside the usage_has_billing gate), for three
    # reasons the gated persist cannot serve:
    #
    #   1. The persist await is offloaded to a thread and can be
    #      cancelled mid-turn. Emitting after it means a cancellation
    #      landing on that await unwinds past this call into the
    #      abnormal-end seam, which — seeing the row already claimed —
    #      records nothing, so the turn's ONLY kirocrew.turn.* sample
    #      is lost. Emitting first makes the sample survive a
    #      cancelled persist.
    #   2. The persist sits behind usage_has_billing. A turn that
    #      timed out having billed nothing writes no row, and letting
    #      the row's absence swallow the sample would drop exactly the
    #      faults fault_rate exists to count, so this emit is
    #      unconditional.
    #   3. session_key is the EFFECTIVE session, which for a linked
    #      channel conversation is its channel key, while the row
    #      stays keyed by slot.key for title and navigation joins.
    #      Attributing the sample to the slot would file every linked
    #      Slack or Telegram turn under `dashboard`.
    # `_record_model` here is pre-backfill, but the emit prefers
    # `_turn_model` (the served id) and the backfill only fills a
    # blank model for a CC row, so the sample's attribution is
    # unaffected.
    _emit_turn_metric(
        event.usage.duration_ms,
        event.stop_reason,
        session_key,
        elapsed_ms=_turn_elapsed_ms,
        exhausted=_turn_exhausted,
        # The same usage object the persist call below is given, so
        # the row store and the instruments describe one turn's
        # NUMBERS identically.
        usage=event.usage,
        model=_turn_model or _record_model,
        provider=_provider_name,
        # The EFFECTIVE harness, read off the client that ran the
        # turn (a per-session acp_backend override included).
        harness=harness_of(client),
        trigger=_dashboard_turn_trigger(turn),
    )
    if usage_has_billing(_u):
        # Claim the turn's single usage row BEFORE the persist await
        # below, then SHIELD the write so a turn cancellation cannot
        # drop it. persist_token_record_async offloads the append via
        # asyncio.to_thread, and a cancel delivered BEFORE the executor
        # thread picks up the write cancels that future so the append
        # never lands -- while the claim above already told the
        # abnormal-end seam the row was written, so the seam writes
        # nothing and the row is lost. asyncio.shield runs
        # the write as its own task the turn's cancel cannot reach, so
        # the append completes; the CancelledError still propagates
        # afterwards. The claim-before-await also keeps the double-bill
        # guard: were the claim set only after the await, that
        # cancellation would unwind into the abnormal-end seam with the
        # guard still False and the seam would re-read live stats and
        # bill the turn a second time. Claimed here, the seam sees it
        # recorded and writes nothing.
        turn._turn_usage_persisted = True
        # Late backfill: CC reports model only via the `init`
        # system event which arrives after the run starts, so
        # slot.model may still be empty here even though the
        # provider learned the model mid-turn. Read it back
        # before persisting so tokens.jsonl is never tagged
        # with a blank model for CC sessions.
        #
        # The `_active_fallback_model` clause guards the slot.model
        # WRITE, not the row's value: the provider reports the
        # FALLBACK, and writing it into slot.model would make the
        # temporary swap a permanent pin (same guard as the pre-turn
        # site). Kept as its own condition rather than folded into the
        # `_record_model` check above, so the write's safety does not
        # depend on whether the live provider happened to report a
        # served model.
        #
        # _record_model / _provider_name are already resolved above
        # the gate; this only refines the model, and only here
        # because the slot.model WRITE must not run for a turn that
        # billed nothing.
        if (
            not _record_model
            and not slot._active_fallback_model
            and not slot._refusal_fallback_primary
            and not _deferred_model_pick
        ):
            _canonical = _backfill_canonical_model(client, _provider_name)
            if _canonical:
                # Same rule as the pre-turn backfill: a slot that
                # inherits (no crew pin either) is recorded but
                # never pinned.
                if agent_model:
                    slot.model = _canonical
                _record_model = _canonical
        await asyncio.shield(
            persist_token_record_async(
                slot.key,
                _record_model,
                event,
                provider=_provider_name,
                # The row stays keyed by its dashboard slot for title
                # and navigation joins; source follows the session the
                # turn actually ran on, including linked channel
                # sessions.
                surface=telemetry_channel_of(session_key),
                # Resolved agent, not the slot alias:
                # resolve_agent_bindings maps e.g. "default" to
                # "kirocrew" before dispatch, so the alias would credit
                # an agent that never ran.
                agent=read_effective_agent(client) or slot.agent or "",
                context_used=_ctx_used,
                context_window=_ctx_window,
                # Ownership is recorded at write time (see
                # _build_token_record): the row must outlive the slot
                # without becoming readable by whoever recreates its
                # name.
                app=getattr(slot, "_app", "") or "",
                # Same wall clock the turn-duration histogram below is
                # given, so the row store and the histogram can never
                # disagree about one turn. acp reports 0 here.
                elapsed_ms=_turn_elapsed_ms,
                model_source=client,
                # The turn's sample is emitted ABOVE, before this
                # cancellable persist and outside the usage_has_billing
                # gate, where its exhausted-aware outcome and the
                # effective session key reach the histogram; this call
                # must not also sample, so it opts out.
                emit_metric=False,
            )
        )
    # The turn's crew log closers are STASHED, not emitted here. This
    # arm runs inside _run_chat's stream loop, and the turn's LAST assistant
    # message is flushed after that loop breaks -- so emitting now puts
    # `turn/completed` in the file ahead of a `message/sent` belonging
    # to the turn it closes. A reader folding entries in order would
    # see a completed turn still producing text, and the closer is the
    # one entry whose position carries meaning. _turn_close_crew_log
    # (chat_turn/crew_log_attribution.py) runs them from _run_chat's finally once
    # every path has flushed.
    _crew_log_terminal = {
        "input_tokens": event.usage.input_tokens,
        "output_tokens": event.usage.output_tokens,
        "cache_read_tokens": event.usage.cache_read_tokens,
        "cache_write_tokens": event.usage.cache_creation_tokens,
        "credits": _turn_credits,
        "duration_ms": _turn_elapsed_ms,
        "stop_reason": event.stop_reason,
        "model": _turn_model or _record_model,
        "provider": _provider_name,
        "depth": _prompt_depth,
        # The same occupancy reading the row store is given just above,
        # so the crew log and the row describe one turn's window
        # identically. It is the provider's own figure; the token counts
        # beside it are billing, which is a different quantity.
        "context_used": _ctx_used,
        "context_window": _ctx_window,
    }
    turn._crew_log_terminal = _crew_log_terminal
    if "timeout" in (event.stop_reason or ""):
        # Hang-resilience series: attribute the CAUSE of a turn
        # timeout (the 2h-ceiling hang class). Both attrs are
        # booleans read defensively from live state — the inner
        # client may be an AcpClient (flag on itself) or an
        # AcpSessionProvider (flag on its handle).
        _ac = slot._acp_client
        _awaiting = bool(
            getattr(_ac, "_awaiting_permission", False)
            or getattr(getattr(_ac, "_handle", None), "_awaiting_permission", False)
        )
        emit_counter(
            TURN_TIMEOUT_CAUSE,
            {
                "path": "provider_timeout",
                "awaiting_permission": _awaiting,
                "children_announced": _children_unfinished,
            },
        )
    _stop_reason = event.stop_reason
    turn._stop_reason = _stop_reason
    _turn_refusal = event.refusal
    turn._turn_refusal = _turn_refusal
    # Recorded on the slot so post-turn consumers reached later
    # (which do not receive the event) can tell a turn that really
    # finished from one cut short by a timeout, cancel or stall.
    slot._last_stop_reason = _stop_reason or ""
    if _stop_reason == STOP_REASON_TOOL_STALL:
        _stall_tool_title = event.title
        turn._stall_tool_title = _stall_tool_title
        _stall_command = event.tool_input
        turn._stall_command = _stall_command
        _stall_evidence = event.text
        turn._stall_evidence = _stall_evidence
        # The watchdog's typed verdict (``kirocrew/status``), when the
        # provider carries one: the wait reason is read from it, not
        # parsed back out of the evidence text.
        _stall_status = getattr(event, "status", None)
        turn._stall_status = _stall_status
    # One mapping for every entry (acp.types.classify_stop_reason):
    # a reason the table does not know is logged here and handled
    # in _turn_stop_recovery as the generic `failed` class. An abandoned
    # post-compaction-failure turn is an EXPECTED terminal state
    # (the compaction notice already told the user), so it is a
    # known class and must not log as unexpected.
    _stop_class = classify_stop_reason(
        _stop_reason,
        compaction_transient=(getattr(client, "last_compaction_transient", False) is True),
    )
    turn._stop_class = _stop_class
    if _stop_reason and not _stop_class.known:
        logger.warning(
            "Unexpected stop_reason %r for slot %s",
            _stop_reason,
            slot.key,
        )
    return _Flow.BREAK


async def _turn_context_usage(turn: _ChatTurn) -> None:
    """The turn's context usage and prompt stats, and its landed verdict (``record_success``, ``_turn_landed``)."""
    from kiro_crew.dashboard import responsive_chat

    _escalation = turn._escalation
    _noticed_leak = turn._noticed_leak
    _recovering_compaction = turn._recovering_compaction
    _recovering_infra = turn._recovering_infra
    _recovering_promise = turn._recovering_promise
    _responsive_owner = turn._responsive_owner
    _responsive_turn = turn._responsive_turn
    _retrying_empty = turn._retrying_empty
    _stop_reason = turn._stop_reason
    client = turn.client
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    # An interrupted owner can otherwise queue /compact ahead of the human
    # answer on the same permit. The protected answering turn checks again.
    _defer_compaction = (
        _responsive_turn.owner is _responsive_owner
        and _responsive_turn.interrupted
        and responsive_chat.first_priority_id(slot) is not None
    )
    if _defer_compaction:
        state.sessions.check_context_usage(session_key, client, defer_compaction=True)
    else:
        state.sessions.check_context_usage(session_key, client)
    pct = client.context_usage_pct()
    state.broadcast_context_usage(slot.key, _context_usage_payload(slot.key, client))
    if (
        _stop_reason != STOP_REASON_CANCELLED
        and not _retrying_empty
        and not _recovering_promise
        and not _recovering_compaction
        and not _noticed_leak
        and not _recovering_infra
    ):
        # An unacted turn (promise-only, or a tool call leaked as text) is
        # deliberately NOT recorded as a landed success: it announced or
        # serialized work it never did, so counting it would tell the
        # reliability metrics (and the poisoned-conversation one-shot) the turn
        # succeeded. The promise-only continuation gets its own turn; if THAT
        # lands, it records success normally.
        state.sessions.record_success(session_key)
        # A LANDED turn breaks the pre-stream-exhaustion streak and
        # re-arms the poisoned-conversation one-shot: only a prompt that
        # actually reached the model and completed proves the (possibly
        # fresh) conversation works. Deliberately NOT in the cancel-
        # inclusive budget block of _turn_persist_and_settle — a Stop press during the recovery
        # turn must not re-arm a second discard without that evidence.
        _escalation.record_landed_turn()
        slot._session_not_found_retry_used = False
        # This turn landed: the prompt (including any re-injected skills
        # index) reached the model, so _turn_settle_debts (chat_turn/prompt_assembly.py)
        # must NOT restore the one-shot flag.
        _turn_landed = True
        turn._turn_landed = _turn_landed
        # Per-interaction telemetry (PlatformContext seam) — shared helper so
        # the payload shape and model reflection cannot drift across surfaces.
        record_interaction_event(client, session_key, "dashboard")
    # Broadcast prompt stats for activity viewer
    _prompt_stats = getattr(  # type: ignore[assignment]
        getattr(client, "_client", client), "last_prompt_stats", None
    )
    if _prompt_stats:
        state.broadcast_ws(
            "activity_event",
            {
                "slot": slot.key,
                "kind": "stats",
                "text": f"Turn complete: {_prompt_stats.event_count} events, {len(_prompt_stats.tool_calls)} tool calls, context {round(pct)}%",  # type: ignore[attr-defined]
            },
        )


def _emit_ttft_metric(t0: float, session_key: str, *, is_new: bool, resumed: bool) -> None:
    """Emit the user-message → first-visible-token latency histogram.

    Best-effort, one point per top-level user prompt. ``first_turn`` splits the
    cold-path population eager spawn targets (the slot's first message) from
    steady-state turns, and ``resumed`` separates ``session/load`` costs — the
    same attribution axes as the startup metric, so the two histograms can be
    read side by side.
    """
    try:
        # Re-read at call time even though the module also imports it at the
        # top: the rebind is what lets a test patching
        # ``kiro_crew.metrics.provider.get_recorder`` reach this emit.
        from kiro_crew.metrics.provider import get_recorder  # noqa: F811

        get_recorder().histogram(
            "kirocrew.chat.first_token.duration",
            (time.monotonic() - t0) * 1000.0,
            unit="ms",
            attrs={
                "channel": telemetry_channel_of(session_key),
                "first_turn": bool(is_new),
                "resumed": bool(resumed),
            },
        )
    except Exception:
        logger.debug("TTFT metric emission failed", exc_info=True)


class _AbnormalTurnEvent:
    """Minimal ``event`` carrier for the abnormal-end usage row.

    ``_build_token_record`` reads a turn's billing off ``event.usage`` and its
    terminal reason off ``event.stop_reason`` -- the same two attributes an
    ``AcpEvent`` exposes at ``EVENT_COMPLETE``. A turn that ends through
    ``_persist_partial_reply`` never produced that event, so this wraps the
    recovered :class:`TurnUsage` and the turn's stop reason in the shape the
    record builder already reads, and nothing more.
    """

    __slots__ = ("usage", "stop_reason")

    def __init__(self, usage: TurnUsage, stop_reason: str) -> None:
        self.usage = usage
        self.stop_reason = stop_reason


async def _persist_abnormal_turn_usage(
    slot: "_ChatSlot",
    client: object,
    session_key: str,
    *,
    elapsed_ms: int,
    stop_reason: str,
    since: object = _NO_PRIOR_STATS,
    usage: TurnUsage | None = None,
) -> bool:
    """Write the usage row for a turn ending without ``EVENT_COMPLETE``.

    The dashboard's one ``persist_token_record_async`` call sits in the
    ``EVENT_COMPLETE`` branch, so every turn that ends through
    ``_persist_partial_reply`` instead -- the turn ceiling, the Stop button, a
    signed-out CLI mid-turn, and the other recovery paths that share that seam
    -- records no credits however much it billed. This is the accounting half of
    that gap: it recovers the turn's billing from the still live provider
    (``provider_last_turn_usage`` reads the credits kiro-cli accumulated on
    ``last_prompt_stats``) and writes the same row the complete path writes,
    keyed and attributed the same way. This seam persists the row ONLY -- it
    emits no histogram sample. The row's persist opts out of the metric
    (``emit_metric=False``) because the persist would key the sample on
    ``slot.key``, filing a linked-channel abnormal turn under ``dashboard``
    instead of its channel surface; abnormal-turn sampling is out of this
    change's scope, so the sample is simply not emitted rather than re-keyed.

    ``client`` is the OUTER provider wrapper (``providers/acp`` /
    ``acp/session_provider``): attribution reads -- ``provider_seam``,
    ``read_context_tokens``, the effective agent -- live there, so the inner
    ``AcpClient`` would label every row ``provider="acp"`` with a ``(0, 0)``
    context window. The caller passes the inner handle only to decide liveness,
    and runs this before ``_turn_release_handles`` drops ``slot._acp_client`` -- the one
    point the credits are still reachable.

    ``since`` is the per-turn stats object snapshotted BEFORE the turn's
    cancellable work: a dispatch that dies before the runtime installs fresh
    stats (a busy session, a dead runtime, an expired credential) leaves the
    PREVIOUS turn's object in place, still carrying credits already recorded, and
    ``provider_last_turn_usage``'s identity check against ``since`` reports
    nothing rather than billing that earlier turn a second time.

    Gated by ``usage_has_billing`` for the same reason the ``EVENT_COMPLETE``
    call is: a turn that billed nothing writes no row. Returns ``True`` once the
    row has been written, so the caller marks the turn recorded and this stays
    once-per-turn across the seam's callers. Never raises -- these are
    best-effort analytics, as the complete path's own persist is.

    ``usage`` is the turn's billing when the caller already read it (the Stop
    footer shares the one read, because ``provider_last_turn_usage`` consumes
    the accumulated total); ``None`` reads it here.
    """
    try:
        if usage is None:
            usage = provider_last_turn_usage(client, since=since)
        if not usage_has_billing(usage):
            return False
        _provider_name = capabilities_of(client).provider_seam
        _record_model = read_turn_model(client) or slot.model
        _ctx_used, _ctx_window = read_context_tokens(client)
        await persist_token_record_async(
            slot.key,
            _record_model,
            _AbnormalTurnEvent(usage, stop_reason),
            provider=_provider_name,
            surface=telemetry_channel_of(session_key),
            agent=read_effective_agent(client) or slot.agent or "",
            context_used=_ctx_used,
            context_window=_ctx_window,
            app=getattr(slot, "_app", "") or "",
            elapsed_ms=elapsed_ms,
            model_source=client,
            # Abnormal-turn histogram sampling is out of this change's scope
            # (the usage ROW is the deliverable); opt the row's persist out of
            # the metric so it does not file a sample under slot.key, which for a
            # linked-channel turn would mis-attribute to session_source=dashboard.
            emit_metric=False,
        )
        return True
    except asyncio.CancelledError:
        # This runs inside the turn's CancelledError teardown, so its own
        # analytics I/O can be cancelled in turn. Swallow it: the row and sample
        # are best-effort, and re-raising here would escape _run_chat's recovery
        # handler and strand the durable completion the finally block still owes
        # (test_raw_complete_survives_cancellation_during_token_persistence).
        logger.debug("Abnormal-turn usage write cancelled for slot %s", slot.key)
        return False
    except Exception:
        logger.debug("Failed to persist abnormal-turn usage for slot %s", slot.key, exc_info=True)
        return False
