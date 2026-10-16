"""Model fallback the ENGINE made, reported to Crew (``acp/engine_fallback.py``).

Pins the two wire decoders (KAS ``session_info_update`` and V2
``_kiro.dev/session/update``) against the rules kiro-cli's own terminal UI applies,
the tracker that keeps a session's resolved model in step with the move, the
walk's end-of-turn wording, and the handle / dispatcher / dashboard seams that
carry the notice.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp._dispatch import classify_notification
from kiro_crew.acp.engine_fallback import (
    TRACKER_ATTR,
    EngineFallbackTracker,
    EngineFallbackWalk,
    ModelFallbackNotice,
    engine_served_model,
    follow_engine_promotion,
    normalize_kas,
    normalize_kiro_v2,
    parse_model_id,
    tracker_of,
    walk_outcome,
)
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    EVENT_MODEL_FALLBACK,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    AcpPromptStats,
    JsonRpcMessage,
    RefusalInfo,
)
from kiro_crew.dashboard import engine_fallback as dash
from kiro_crew.llm_helpers import annotate_model_fallback, engine_attributed_model

A, B, C = "model-a", "model-b", "model-c"


def _hop(
    frm: str = A, to: str | None = B, cause: str = "refused", engine: str = "v2"
) -> ModelFallbackNotice:
    return ModelFallbackNotice(from_model=frm, to_model=to, cause=cause, engine=engine)  # type: ignore[arg-type]


# ── parse_model_id ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["model-a", "vendor.model-3-5:0", "a_b.c-d:e"])
def test_model_id_grammar_accepts_engine_ids(value: str) -> None:
    assert parse_model_id(value) == value


@pytest.mark.parametrize(
    "value", ["", "two words", "a\nb", "a/b", "x" * 1025, None, 3, "\x1b[31mred"]
)
def test_model_id_grammar_rejects_everything_else(value: object) -> None:
    assert parse_model_id(value) is None


def test_model_id_that_looks_like_a_credential_is_rejected() -> None:
    assert parse_model_id("AKIAIOSFODNN7EXAMPLE") is None


# ── KAS decoder ──────────────────────────────────────────────────────────────


def test_kas_session_scope_is_a_hop_then_its_promotion() -> None:
    notices = normalize_kas(
        {"kind": "model_fallback", "from": A, "to": B, "trigger": "refusal", "scope": "session"}
    )
    assert notices == [
        ModelFallbackNotice(A, B, "refused", engine="kas"),
        ModelFallbackNotice(A, B, "refused", promoted=True, engine="kas"),
    ]


def test_kas_turn_scope_is_a_hop_only() -> None:
    notices = normalize_kas({"from": A, "to": B, "trigger": "availability", "scope": "turn"})
    assert notices == [ModelFallbackNotice(A, B, "unavailable", engine="kas")]


def test_kas_unknown_scope_keeps_the_hop_and_never_promotes() -> None:
    notices = normalize_kas({"from": A, "to": B, "trigger": "refusal", "scope": "forever"})
    assert notices == [ModelFallbackNotice(A, B, "refused", engine="kas")]


def test_kas_unknown_trigger_is_reported_as_unknown_not_guessed() -> None:
    notices = normalize_kas({"from": A, "to": B, "trigger": "budget", "scope": "turn"})
    assert notices[0].cause == "unknown"


def test_kas_decline_has_no_target_and_keeps_its_reason() -> None:
    notices = normalize_kas(
        {"from": A, "trigger": "availability", "scope": "turn", "reason": "chainExhausted"}
    )
    assert notices == [
        ModelFallbackNotice(A, None, "unavailable", reason="chainExhausted", engine="kas")
    ]


def test_kas_sub_agent_move_is_not_the_turns() -> None:
    assert (
        normalize_kas(
            {"from": A, "to": B, "trigger": "refusal", "scope": "turn", "agentSubtaskId": "sub-1"}
        )
        == []
    )


def test_kas_malformed_sub_agent_id_does_not_suppress_the_turns_notice() -> None:
    assert (
        len(
            normalize_kas(
                {"from": A, "to": B, "trigger": "refusal", "scope": "turn", "agentSubtaskId": 7}
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    "meta", [{"to": B}, {"from": "bad id", "to": B}, {"from": A, "to": "bad id"}]
)
def test_kas_notice_with_an_unreadable_id_is_dropped(meta: dict) -> None:
    assert normalize_kas({"trigger": "refusal", "scope": "turn", **meta}) == []


# ── V2 decoder ───────────────────────────────────────────────────────────────


def test_v2_hop_and_promotion_are_separate_notices() -> None:
    hop = normalize_kiro_v2(
        {
            "sessionUpdate": "model_fallback",
            "from": A,
            "to": B,
            "cause": "refused",
            "source": "backend",
            "promoted": False,
        }
    )
    promo = normalize_kiro_v2(
        {
            "sessionUpdate": "model_fallback",
            "from": A,
            "to": B,
            "cause": "refused",
            "source": "backend",
            "promoted": True,
        }
    )
    assert hop == [ModelFallbackNotice(A, B, "refused", source="backend")]
    assert promo == [ModelFallbackNotice(A, B, "refused", promoted=True, source="backend")]


def test_v2_null_target_is_a_decline() -> None:
    assert normalize_kiro_v2(
        {"from": A, "to": None, "cause": "unavailable", "promoted": False}
    ) == [ModelFallbackNotice(A, None, "unavailable")]


def test_v2_absent_promoted_reads_as_a_hop() -> None:
    assert normalize_kiro_v2({"from": A, "to": B, "cause": "unavailable"})[0].promoted is False


@pytest.mark.parametrize(
    "update",
    [
        {"from": A, "to": B, "cause": "budget"},
        {"from": A, "to": B, "cause": "refused", "promoted": "yes"},
        {"from": "bad id", "to": B, "cause": "refused"},
    ],
)
def test_v2_malformed_notice_is_dropped(update: dict) -> None:
    assert normalize_kiro_v2(update) == []


def test_dispatcher_routes_only_the_v2_fallback_notice() -> None:
    fb = JsonRpcMessage(
        method="_kiro.dev/session/update",
        params={"sessionId": "s", "update": {"sessionUpdate": "model_fallback", "from": A}},
    )
    other = JsonRpcMessage(
        method="_kiro.dev/session/update",
        params={"sessionId": "s", "update": {"sessionUpdate": "tool_call_chunk"}},
    )
    assert classify_notification(fb) == "model_fallback"
    assert classify_notification(other) == "subagent_activity"


# ── tracker ──────────────────────────────────────────────────────────────────


def _owner(model: str = "", resolved: str = A) -> SimpleNamespace:
    return SimpleNamespace(_model=model, _resolved_model_id=resolved)


def test_turn_hop_credits_the_target_until_the_next_prompt() -> None:
    owner, tracker = _owner(), EngineFallbackTracker()
    tracker.apply(owner, [_hop(cause="unavailable")])
    assert owner._resolved_model_id == B
    tracker.begin_prompt(owner)
    assert owner._resolved_model_id == A


def test_turn_restore_leaves_a_model_something_else_set_since() -> None:
    owner, tracker = _owner(), EngineFallbackTracker()
    tracker.apply(owner, [_hop(cause="unavailable")])
    owner._resolved_model_id = C
    tracker.begin_prompt(owner)
    assert owner._resolved_model_id == C


def test_a_switch_onto_the_hop_target_is_not_undone() -> None:
    # The user picks the model the engine moved the last turn onto: the
    # override and the pick are the same id, so only the switch itself can
    # tell the next prompt not to put back the model the hop moved off.
    owner, tracker = _owner(), EngineFallbackTracker()
    tracker.apply(owner, [_hop(cause="unavailable")])
    owner._resolved_model_id = B
    tracker.note_model_set()
    tracker.begin_prompt(owner)
    assert owner._resolved_model_id == B


def test_promotion_moves_a_pinned_session_for_good() -> None:
    owner, tracker = _owner(model=A), EngineFallbackTracker()
    tracker.apply(
        owner, normalize_kas({"from": A, "to": B, "trigger": "refusal", "scope": "session"})
    )
    tracker.begin_prompt(owner)
    assert (owner._model, owner._resolved_model_id) == (B, B)


@pytest.mark.parametrize("intent", ["", "auto"])
def test_promotion_leaves_an_unpinned_session_unpinned(intent: str) -> None:
    owner, tracker = _owner(model=intent), EngineFallbackTracker()
    tracker.apply(
        owner, normalize_kas({"from": A, "to": B, "trigger": "refusal", "scope": "session"})
    )
    tracker.begin_prompt(owner)
    assert (owner._model, owner._resolved_model_id) == (intent, B)


def test_decline_and_same_id_retry_move_nothing() -> None:
    owner, tracker = _owner(), EngineFallbackTracker()
    tracker.apply(owner, [_hop(to=None), _hop(to=A)])
    assert owner._resolved_model_id == A
    assert not tracker.walk.moved()


def test_tracker_is_found_under_provider_wrappers() -> None:
    handle = SimpleNamespace(_engine_fallback=EngineFallbackTracker())
    provider = SimpleNamespace(_client=SimpleNamespace(_handle=handle))
    assert tracker_of(provider) is handle._engine_fallback
    assert tracker_of(SimpleNamespace()) is None


# ── walk wording (kiro-cli fallbackWalkLine) ─────────────────────────────────


def _walk(*notices: ModelFallbackNotice) -> EngineFallbackWalk:
    walk = EngineFallbackWalk()
    for notice in notices:
        walk.record(notice)
    return walk


def test_no_hop_says_nothing() -> None:
    assert _walk().line("answered") is None
    assert _walk(_hop(to=A)).line("answered") is None


def test_turn_hop_answered() -> None:
    assert _walk(_hop(cause="unavailable")).line("answered") == (
        "model-a was unavailable; answered by model-b for this turn."
    )


def test_promoted_hop_answered() -> None:
    walk = _walk(_hop(), ModelFallbackNotice(A, B, "refused", promoted=True))
    assert (
        walk.line("answered")
        == "model-a refused; answered by model-b, now this conversation's model."
    )


def test_decline_names_no_answer_and_where_to_go() -> None:
    assert _walk(_hop(to=None, cause="unavailable")).line("faulted") == (
        "model-a was unavailable. No model answered. Try a different model from the model picker."
    )


def test_faulted_after_one_hop_and_after_two() -> None:
    assert _walk(_hop()).line("faulted") == "model-a refused. No model answered."
    two = _walk(_hop(), _hop(frm=B, to=C, cause="unknown"))
    assert two.line("faulted") == "model-a refused; model-b yielded; model-c produced no answer."


def test_cancelled_still_names_a_promotion() -> None:
    walk = _walk(_hop(), ModelFallbackNotice(A, B, "refused", promoted=True))
    assert (
        walk.line("cancelled")
        == "model-a refused; cancelled. model-b is now this conversation's model."
    )


def test_long_id_is_cut_for_display() -> None:
    long_id = "m" * 80
    line = _walk(_hop(frm=long_id)).line("answered") or ""
    assert ("m" * 63 + "…") in line and long_id not in line


# ── session handle (KAS) ─────────────────────────────────────────────────────


def _kas_handle() -> AcpSessionHandle:
    rt = MagicMock()
    rt.acp_backend = ACP_BACKEND_KAS
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle("sA", asyncio.Queue(), rt)


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", [True, False], ids=["switched", "control"])
async def test_handle_set_model_onto_the_hop_target_survives_the_next_prompt(
    switch: bool,
) -> None:
    handle = _kas_handle()
    handle._resolved_model_id = A
    handle._advertised_model_ids = lambda: [A, B]  # type: ignore[method-assign]
    handle.set_config_option = AsyncMock()  # type: ignore[method-assign]
    handle._engine_fallback.apply(handle, [_hop(engine="kas")])
    if switch:
        await handle.set_model(B)
    handle._engine_fallback.begin_prompt(handle)
    # Without the switch the turn-scoped hop is undone; with it, the pick stands.
    assert handle._resolved_model_id == (B if switch else A)


def _kas_info(handle: AcpSessionHandle, kiro: dict) -> list:
    update = {"sessionUpdate": "session_info_update", "_meta": {"kiro": kiro}}
    return handle._handle_update(JsonRpcMessage(method="session/update", params={"update": update}))


def test_kas_notice_reaches_the_turn_and_moves_the_resolved_model() -> None:
    handle = _kas_handle()
    handle._resolved_model_id = A
    events = _kas_info(
        handle,
        {"kind": "model_fallback", "from": A, "to": B, "trigger": "availability", "scope": "turn"},
    )
    assert [e.kind for e in events] == [EVENT_MODEL_FALLBACK]
    assert events[0].model_fallback == ModelFallbackNotice(A, B, "unavailable", engine="kas")
    assert handle._resolved_model_id == B
    assert engine_attributed_model(A, handle) == B
    assert annotate_model_fallback("done", handle) == (
        "⚠ model-a was unavailable; answered by model-b for this turn.\n\ndone"
    )


# ── unattended annotation (cron / heartbeat / sub-agent) ─────────────────────


def _provider_with(*notices: ModelFallbackNotice) -> SimpleNamespace:
    # tracker_of finds the tracker on the session-owning object under TRACKER_ATTR.
    tracker = EngineFallbackTracker()
    owner = SimpleNamespace(_resolved_model_id=A, _model=A)
    tracker.apply(owner, list(notices))
    setattr(owner, TRACKER_ATTR, tracker)
    return owner


def _kas_promotion() -> tuple[ModelFallbackNotice, ModelFallbackNotice]:
    return (
        _hop(engine="kas"),
        ModelFallbackNotice(A, B, "refused", promoted=True, engine="kas"),
    )


@pytest.mark.parametrize(
    ("notices", "billed"),
    [
        # A turn-scoped hop leaves the pin in place but the target answered.
        ((_hop(cause="unavailable"),), B),
        # A promotion bills the promoted model.
        (_kas_promotion(), B),
        # A decline and a same-id retry moved nothing: the pin stands.
        ((_hop(to=None), _hop(to=A)), A),
        ((), A),
    ],
    ids=["turn-hop", "promotion", "no-move", "no-walk"],
)
def test_a_usage_row_bills_the_model_the_engine_ran(notices: tuple, billed: str) -> None:
    assert engine_attributed_model(A, _provider_with(*notices)) == billed
    # Unpinned, and with no tracker at all.
    assert engine_attributed_model("", _provider_with(*notices)) == ("" if billed == A else B)
    assert engine_attributed_model(A, SimpleNamespace()) == A


def test_the_tracker_is_found_through_the_shared_wrapper_walk() -> None:
    # The session-provider shape: provider._client is an AcpSessionProvider whose
    # handle carries the tracker two levels down, beside a runtime.
    handle = SimpleNamespace(_engine_fallback=EngineFallbackTracker())
    provider = SimpleNamespace(
        _client=SimpleNamespace(_handle=handle, _runtime=SimpleNamespace()), client=None
    )
    assert tracker_of(provider) is handle._engine_fallback
    assert tracker_of(MagicMock()) is None


def test_unattended_kas_refusal_terminal_reads_as_no_answer() -> None:
    # A cron run where KAS pinned model-b at move time and model-b also refused:
    # the delivered result must not claim model-b answered.
    out = annotate_model_fallback(
        "canned", _provider_with(*_kas_promotion()), stop_reason=STOP_REASON_REFUSAL
    )
    assert out == (
        "⚠ model-a refused. No model answered. model-b is now this conversation's model."
        "\n\ncanned"
    )


def test_unattended_kas_recovery_reads_as_answered() -> None:
    out = annotate_model_fallback(
        "ok", _provider_with(*_kas_promotion()), stop_reason=STOP_REASON_END_TURN
    )
    assert out == "⚠ model-a refused; answered by model-b, now this conversation's model.\n\nok"


def test_unattended_v2_recovery_reads_as_answered() -> None:
    # A V2 hop unfolds the refusal it moved off, so a recovered run's terminal
    # is end_turn by the time it is delivered.
    provider = _provider_with(_hop(), ModelFallbackNotice(A, B, "refused", promoted=True))
    out = annotate_model_fallback("ok", provider, stop_reason=STOP_REASON_END_TURN)
    assert out == "⚠ model-a refused; answered by model-b, now this conversation's model.\n\nok"


def test_unattended_without_a_terminal_keeps_the_answered_wording() -> None:
    # A caller that saw no completion event passes None; a result is being
    # delivered, so the line reads as answered (the pre-verdict behavior).
    out = annotate_model_fallback("ok", _provider_with(*_kas_promotion()))
    assert out.startswith("⚠ model-a refused; answered by model-b")


# ── surfaces that show the model (sub-agent chip, channel /model pickers) ─────


def test_engine_served_model_names_the_last_target() -> None:
    assert engine_served_model(_provider_with()) == ""
    assert engine_served_model(SimpleNamespace()) == ""
    # A turn-scoped hop: the pin is untouched, but the hop's target ran.
    assert engine_served_model(_provider_with(_hop(cause="unavailable"))) == B
    # Two hops: the last target ran.
    two = _provider_with(_hop(), _hop(frm=B, to=C))
    assert engine_served_model(two) == C
    # A promotion names the model outright.
    assert engine_served_model(_provider_with(*_kas_promotion())) == B
    # A decline found nowhere to go, so nothing moved.
    assert engine_served_model(_provider_with(_hop(to=None))) == ""


def _owner_with_refusal() -> SimpleNamespace:
    """A session owner whose prompt recorded a content-filter refusal on metadata."""
    stats = AcpPromptStats()
    stats.refusal = RefusalInfo(category="CYBER")
    return SimpleNamespace(_resolved_model_id=A, _model=A, last_prompt_stats=stats)


def test_a_v2_refusal_hop_unfolds_the_refusal_it_moved_off() -> None:
    # V2 sends the refused model's refusal on metadata, then hops, then reports
    # its own end_turn when the target answers. The fold must not relabel that.
    owner = _owner_with_refusal()
    EngineFallbackTracker().apply(owner, [_hop(engine="v2")])
    assert owner.last_prompt_stats.terminal_refusal(STOP_REASON_END_TURN) == (
        STOP_REASON_END_TURN,
        None,
    )


def test_a_refusal_the_v2_target_sends_after_the_hop_still_folds() -> None:
    owner = _owner_with_refusal()
    tracker = EngineFallbackTracker()
    tracker.apply(owner, [_hop(engine="v2")])
    owner.last_prompt_stats.refusal = RefusalInfo(category="CYBER")  # the target refused too
    # A promotion notice reaching the client after the target's refusal must not wipe it.
    tracker.apply(owner, [ModelFallbackNotice(A, B, "refused", promoted=True, engine="v2")])
    reason, refusal = owner.last_prompt_stats.terminal_refusal(STOP_REASON_END_TURN)
    assert reason == STOP_REASON_REFUSAL and refusal is not None


@pytest.mark.parametrize(
    "notices",
    [
        # KAS reports its own terminal; its refusal is not on this channel.
        [_hop(engine="kas")],
        # An availability hop says nothing about a refusal.
        [_hop(cause="unavailable", engine="v2")],
        # A decline left the turn on the model that refused.
        [_hop(to=None, engine="v2")],
    ],
    ids=["kas-hop", "v2-availability-hop", "v2-decline"],
)
def test_nothing_else_unfolds_a_recorded_refusal(notices: list) -> None:
    owner = _owner_with_refusal()
    EngineFallbackTracker().apply(owner, notices)
    assert owner.last_prompt_stats.terminal_refusal(STOP_REASON_END_TURN)[0] == (
        STOP_REASON_REFUSAL
    )


def test_a_pinned_channel_pick_follows_a_promotion() -> None:
    prefs = {"route": A}
    assert follow_engine_promotion(prefs, "route", _provider_with(*_kas_promotion())) is True
    assert prefs == {"route": B}


@pytest.mark.parametrize("pick", ["", "auto"])
def test_an_auto_channel_pick_stays_auto(pick: str) -> None:
    prefs = {"route": pick}
    provider = _provider_with(*_kas_promotion())
    assert follow_engine_promotion(prefs, "route", provider) is False
    # Read once even when skipped: a pinned pick made later is not overwritten.
    prefs["route"] = C
    assert follow_engine_promotion(prefs, "route", provider) is False
    assert prefs == {"route": C}


def test_a_turn_scoped_hop_leaves_the_channel_pick_alone() -> None:
    prefs = {"route": A}
    provider = _provider_with(_hop(cause="unavailable"))
    assert follow_engine_promotion(prefs, "route", provider) is False
    assert prefs == {"route": A}


def test_a_missing_channel_pick_or_tracker_is_a_no_op() -> None:
    prefs: dict[str, str] = {}
    assert follow_engine_promotion(prefs, "route", _provider_with(*_kas_promotion())) is False
    assert prefs == {}
    pinned = {"route": A}
    assert follow_engine_promotion(pinned, "route", SimpleNamespace()) is False
    assert pinned == {"route": A}


# ── dashboard ────────────────────────────────────────────────────────────────


def _slot(model: str = "") -> MagicMock:
    slot = MagicMock()
    slot.model = model
    slot._model_pick_gen = 0
    return slot


def test_dashboard_promotion_moves_a_pinned_slot_and_stales_restores() -> None:
    slot, state = _slot(model=A), MagicMock()
    client = SimpleNamespace(served_model=B, _explicit_pick_epoch=0)
    walk = EngineFallbackWalk()
    for notice in normalize_kas({"from": A, "to": B, "trigger": "refusal", "scope": "session"}):
        dash.record_engine_fallback(state, slot, client, walk, notice)
    assert slot.model == B
    assert slot._model_pick_gen == 1 and client._explicit_pick_epoch == 1
    slot.record_served_model.assert_called_once_with(B)
    state.push_slots_update.assert_called_once()


def test_dashboard_turn_hop_leaves_the_slot_alone() -> None:
    slot, state = _slot(model=A), MagicMock()
    dash.record_engine_fallback(
        state, slot, SimpleNamespace(), EngineFallbackWalk(), _hop(cause="unavailable")
    )
    assert slot.model == A and slot._model_pick_gen == 0
    state.push_slots_update.assert_not_called()


@pytest.mark.parametrize(
    ("stop_reason", "cancelled", "outcome"),
    [
        # `end_turn` is the only terminal that answered, on both engines.
        (STOP_REASON_END_TURN, False, "answered"),
        (STOP_REASON_REFUSAL, False, "faulted"),
        ("", False, "faulted"),
        ("error: tool stall", False, "faulted"),
        # Cancellation wins over the stop reason, by either signal.
        (STOP_REASON_CANCELLED, False, "cancelled"),
        (STOP_REASON_END_TURN, True, "cancelled"),
    ],
)
def test_walk_outcome(stop_reason: str, cancelled: bool, outcome: str) -> None:
    assert walk_outcome(stop_reason, cancelled=cancelled) == outcome


def test_dashboard_finish_appends_one_notice_once() -> None:
    slot, walk = _slot(), _walk(_hop(cause="unavailable"))
    kw = dict(stop_reason=STOP_REASON_END_TURN, cancelled=False)
    dash.finish_engine_fallback(slot, walk, **kw)
    dash.finish_engine_fallback(slot, walk, **kw)
    slot.append.assert_called_once_with(
        "notice", "model-a was unavailable; answered by model-b for this turn.", "msg msg-info"
    )


@pytest.mark.parametrize("engine", ["kas", "v2"])
def test_a_promotion_with_a_refusal_terminal_reads_as_no_answer(engine: str) -> None:
    # The promoted target refused too, so the terminal is refusal. A promotion
    # is not an answer: KAS pins at move time, and a V2 target that refuses
    # after the hop leaves a refusal the fold keeps (see the unfold tests).
    slot = _slot()
    walk = _walk(
        _hop(engine=engine),
        ModelFallbackNotice(A, B, "refused", promoted=True, engine=engine),  # type: ignore[arg-type]
    )
    dash.finish_engine_fallback(slot, walk, stop_reason=STOP_REASON_REFUSAL, cancelled=False)
    slot.append.assert_called_once_with(
        "notice",
        "model-a refused. No model answered. model-b is now this conversation's model.",
        "msg msg-info",
    )


@pytest.mark.parametrize("engine", ["kas", "v2"])
def test_a_recovered_turn_reads_as_answered(engine: str) -> None:
    # The target answered, so the terminal is end_turn on both engines.
    slot = _slot()
    walk = _walk(
        _hop(engine=engine),
        ModelFallbackNotice(A, B, "refused", promoted=True, engine=engine),  # type: ignore[arg-type]
    )
    dash.finish_engine_fallback(slot, walk, stop_reason=STOP_REASON_END_TURN, cancelled=False)
    slot.append.assert_called_once_with(
        "notice",
        "model-a refused; answered by model-b, now this conversation's model.",
        "msg msg-info",
    )


def test_provider_passthrough_preserves_the_fallback_payload() -> None:
    # The ACP provider rebuilds every event field by field; a field it forgets
    # is silently zeroed, so the dashboard enters its branch with nothing to
    # record. This pins model_fallback across that boundary.
    from kiro_crew.acp.types import AcpEvent
    from kiro_crew.providers.acp import AcpProvider

    notice = ModelFallbackNotice(A, B, "refused", promoted=True)
    out = AcpProvider._to_llm_event(AcpEvent(kind=EVENT_MODEL_FALLBACK, model_fallback=notice))
    assert out.kind == EVENT_MODEL_FALLBACK
    assert out.model_fallback == notice


def test_a_pick_changed_during_the_turn_is_not_overwritten() -> None:
    # `/model C` sent mid-turn is the user's newer choice; the promotion of the
    # turn that was already running does not replace it, now or on a later
    # turn that never prompts.
    prefs = {"route": C}
    provider = _provider_with(*_kas_promotion())
    assert follow_engine_promotion(prefs, "route", provider, before=A) is False
    assert follow_engine_promotion(prefs, "route", provider, before=C) is False
    assert prefs == {"route": C}


def test_a_promotion_is_followed_once() -> None:
    # A later turn that never prompts leaves the last walk in place; reading it
    # again would undo a pick made since.
    prefs = {"route": A}
    provider = _provider_with(*_kas_promotion())
    assert follow_engine_promotion(prefs, "route", provider, before=A) is True
    prefs["route"] = C
    assert follow_engine_promotion(prefs, "route", provider, before=C) is False
    assert prefs == {"route": C}
