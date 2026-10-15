"""Model fallback the ENGINE made, reported to Crew (``acp/engine_fallback.py``).

Pins the two wire decoders (KAS ``session_info_update`` and V2
``_kiro.dev/session/update``) against the rules kiro-cli's own terminal UI applies,
the tracker that keeps a session's resolved model in step with the move, the
walk's end-of-turn wording, and the handle / dispatcher / dashboard seams that
carry the notice.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_frame_replay_harness import read_fixture
from chat_runner_coverage_helpers import _complete, _drive, _runner_state, _set_stream
from chat_runner_coverage_helpers import _slot as _runner_slot
from test_acp_runtime import (
    _await_routed,
    _feed,
    _make_runtime,
    _register,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp._dispatch import classify_notification
from kiro_crew.acp.client import AcpClient
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
    ACP_BACKEND_KIRO,
    EVENT_MODEL_FALLBACK,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
    RefusalInfo,
)
from kiro_crew.dashboard import engine_fallback as dash
from kiro_crew.llm_helpers import (
    TURN_FALLBACK_ATTR,
    annotate_model_fallback,
    engine_attributed_model,
    probe_fallback_restore,
)
from kiro_crew.testing.wait import await_until

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


_WINDOWS = {A: 1_000_000, B: 200_000}


@pytest.fixture
def _known_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import model_registry

    monkeypatch.setattr(model_registry, "has_known_window", lambda m: m in _WINDOWS)
    monkeypatch.setattr(model_registry, "model_window", lambda m, **_: _WINDOWS.get(m))


def _owner_with_context() -> SimpleNamespace:
    stats = AcpPromptStats(
        context_window_tokens=1_000_000, context_used_tokens=100_000, context_pct=10.0
    )
    return SimpleNamespace(_model="", _resolved_model_id=A, last_prompt_stats=stats)


@pytest.mark.usefixtures("_known_windows")
def test_a_hop_rebases_the_context_window_and_the_next_prompt_rebases_it_back() -> None:
    owner, tracker = _owner_with_context(), EngineFallbackTracker()
    tracker.apply(owner, [_hop(cause="unavailable")])
    stats = owner.last_prompt_stats
    assert (stats.context_window_tokens, stats.context_pct) == (200_000, 50.0)
    tracker.begin_prompt(owner)
    assert (stats.context_window_tokens, stats.context_pct) == (1_000_000, 10.0)


@pytest.mark.usefixtures("_known_windows")
def test_a_promotion_rebases_the_context_window() -> None:
    owner, tracker = _owner_with_context(), EngineFallbackTracker()
    tracker.apply(owner, [ModelFallbackNotice(A, B, "refused", promoted=True)])
    assert owner.last_prompt_stats.context_window_tokens == 200_000


@pytest.mark.usefixtures("_known_windows")
def test_a_decline_leaves_the_context_window_alone() -> None:
    owner, tracker = _owner_with_context(), EngineFallbackTracker()
    tracker.apply(owner, [_hop(to=None)])
    assert owner.last_prompt_stats.context_window_tokens == 1_000_000


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


def test_the_last_model_is_the_last_hop_not_an_earlier_promotion() -> None:
    # A promotion to B, then a turn-scoped hop to C: C ran last and is what the
    # usage rows and the chip name, while the conversation's model stays B.
    walk = _walk(
        _hop(engine="kas"),
        ModelFallbackNotice(A, B, "refused", promoted=True, engine="kas"),
        _hop(frm=B, to=C, cause="unavailable", engine="kas"),
    )
    assert walk.last_model() == C
    assert walk.promoted_to == B
    # A promotion with no hop recorded still names the model.
    assert _walk(ModelFallbackNotice(A, B, "refused", promoted=True)).last_model() == B
    # A decline after a hop leaves the hop's target as the last model.
    assert _walk(_hop(cause="unavailable"), _hop(frm=B, to=None)).last_model() == B


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


def _v2_fallback_frame(session_id: str | None) -> dict:
    """A V2 turn-scoped hop A -> B, as the wire spells it."""
    params: dict = {
        "update": {"sessionUpdate": "model_fallback", "from": A, "to": B, "cause": "unavailable"}
    }
    if session_id is not None:
        params["sessionId"] = session_id
    return {"method": "_kiro.dev/session/update", "params": params}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_id, applied",
    [("sOther", False), ("sA", True), (None, True)],
    ids=["other-session", "own-session", "no-session-id"],
)
async def test_the_handle_applies_only_its_own_v2_fallback_notice(
    session_id: str | None, applied: bool
) -> None:
    # A V2 notice that names a co-tenant reaches this queue on the shared
    # runtime; applying it would bill this session's turn to that session's model.
    rt = MagicMock()
    rt.acp_backend = ACP_BACKEND_KIRO
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    queue: asyncio.Queue = asyncio.Queue()
    handle = AcpSessionHandle("sA", queue, rt)
    handle._resolved_model_id = A
    queue.put_nowait(JsonRpcMessage(**_v2_fallback_frame(session_id)))
    queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": STOP_REASON_END_TURN}))
    events = [ev async for ev in handle._dispatch_events(1, 3.0)]
    fallbacks = [ev for ev in events if ev.kind == EVENT_MODEL_FALLBACK]
    assert len(fallbacks) == (1 if applied else 0)
    assert handle._resolved_model_id == (B if applied else A)


@pytest.mark.asyncio
async def test_each_handle_prompt_undoes_the_last_turns_hop_and_starts_a_new_walk() -> None:
    rt, reader, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KIRO
    queues = _register(rt, "sA")
    handle = AcpSessionHandle("sA", queues["sA"], rt)
    handle._resolved_model_id = A
    seen: list[object] = []
    task = await _start_reader(rt)
    try:
        for frames in ([_v2_fallback_frame("sA")], []):

            async def drive() -> None:
                async for _ in handle.prompt("hi", timeout=5.0):
                    pass

            driver = asyncio.ensure_future(drive())
            req_id = (await _await_routed(rt, "sA"))["sA"]
            # The turn has started: the previous turn's hop is undone by now.
            seen.append(handle._resolved_model_id)
            for frame in frames:
                _feed(reader, frame)
            _feed(reader, {"id": req_id, "result": {"stopReason": STOP_REASON_END_TURN}})
            await await_until(driver, what="the driver finishing its turn")
            seen += [handle._resolved_model_id, handle._engine_fallback.walk.moved()]
    finally:
        await _stop_reader(task)
    # Turn 1 starts on A and is billed to B; turn 2 starts back on A with an empty walk.
    assert seen == [A, B, True, A, A, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", [True, False], ids=["switched", "control"])
async def test_client_set_model_onto_the_hop_target_survives_the_next_prompt(
    switch: bool,
) -> None:
    client = AcpClient()
    client._session_id = "sA"
    client._model = A
    client._resolved_model_id = A
    client._acp_backend = ""  # kiro-cli V2
    client._available_models = [{"modelId": m, "name": m, "description": ""} for m in (A, B)]
    client._send_request = AsyncMock(return_value=1)  # type: ignore[method-assign]
    client._engine_fallback.apply(client, [_hop(cause="unavailable")])
    if switch:
        await client.set_model(B)
    client._engine_fallback.begin_prompt(client)
    assert client._resolved_model_id == (B if switch else A)


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


_LIVE_REFUSAL = Path(__file__).parent / "fixtures/acp_frames/kiro/refusal-fallback-live.jsonl"


def _is_hop_frame(frame: dict) -> bool:
    update = (frame.get("params") or {}).get("update") or {}
    return update.get("sessionUpdate") == "model_fallback" and update.get("promoted") is False


async def _terminal_of(frames: list[dict]) -> tuple[str | None, str]:
    """Run one V2 prompt turn of *frames* through a handle; return its terminal and model."""
    rt = MagicMock()
    rt.acp_backend = ACP_BACKEND_KIRO
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    queue: asyncio.Queue = asyncio.Queue()
    handle = AcpSessionHandle("kiro-refusal-session-1", queue, rt)
    handle._resolved_model_id = "claude-opus-4.6"
    for frame in frames:
        queue.put_nowait(JsonRpcMessage.from_dict(frame))
    events = [ev async for ev in handle._dispatch_events(2, 3.0)]
    (terminal,) = [ev for ev in events if ev.kind == "complete"]
    return terminal.stop_reason, handle._resolved_model_id


@pytest.mark.asyncio
async def test_the_live_v2_refusal_capture_reads_as_answered_by_the_target() -> None:
    # kiro-cli's own wire order (the capture's header says how it was taken):
    # the _kiro.dev/metadata refusal, THEN the hop, then the answer and end_turn.
    # The hop unfolds the refusal it moved off, so the turn reads as answered.
    meta, frames = read_fixture(_LIVE_REFUSAL)
    assert meta["recorded"] == "live"
    order = [
        "refusal" if "refusal" in (f.get("params") or {}) else "hop" if _is_hop_frame(f) else ""
        for f in frames
    ]
    assert [o for o in order if o] == ["refusal", "hop"]
    assert await _terminal_of(frames) == (STOP_REASON_END_TURN, "claude-sonnet-4.6")


@pytest.mark.asyncio
async def test_the_unfold_depends_on_the_refusal_arriving_before_the_hop() -> None:
    _, frames = read_fixture(_LIVE_REFUSAL)
    # Control: without the hop, the recorded refusal folds the end_turn terminal.
    no_hop = [f for f in frames if not _is_hop_frame(f)]
    assert (await _terminal_of(no_hop))[0] == STOP_REASON_REFUSAL
    # A refusal arriving AFTER the hop is the target's own, and still folds.
    (hop,) = [f for f in frames if _is_hop_frame(f)]
    hop_first = [hop] + [f for f in frames if f is not hop]
    assert (await _terminal_of(hop_first))[0] == STOP_REASON_REFUSAL


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


def test_a_second_promotion_after_a_followed_one_moves_the_pick_again() -> None:
    owner, tracker = _owner(model=A), EngineFallbackTracker()
    provider = SimpleNamespace(**{TRACKER_ATTR: tracker})
    prefs = {"route": A}
    tracker.apply(owner, [ModelFallbackNotice(A, B, "refused", promoted=True)])
    assert follow_engine_promotion(prefs, "route", provider, before=A) is True
    tracker.begin_prompt(owner)
    tracker.apply(owner, [ModelFallbackNotice(B, C, "refused", promoted=True)])
    assert follow_engine_promotion(prefs, "route", provider, before=B) is True
    assert prefs == {"route": C}


def test_a_promotion_is_followed_after_a_later_prompt_resets_the_walk() -> None:
    # Post-turn compaction prompts again before the channel follows.
    owner, tracker = _owner(model=A), EngineFallbackTracker()
    provider = SimpleNamespace(**{TRACKER_ATTR: tracker})
    tracker.apply(owner, list(_kas_promotion()))
    tracker.begin_prompt(owner)
    prefs = {"route": A}
    assert follow_engine_promotion(prefs, "route", provider, before=A) is True
    assert prefs == {"route": B}


def test_a_turn_scoped_hop_leaves_the_channel_pick_alone() -> None:
    prefs = {"route": A}
    provider = _provider_with(_hop(cause="unavailable"))
    assert follow_engine_promotion(prefs, "route", provider) is False
    assert prefs == {"route": A}


def test_a_pick_stored_for_the_next_session_is_not_overwritten() -> None:
    # `/model C` stored for the next session while the live one still ran A:
    # the engine's A -> B promotion replaced A, not the user's C.
    prefs = {"route": C}
    provider = _provider_with(*_kas_promotion())
    assert follow_engine_promotion(prefs, "route", provider, before=C) is False
    assert prefs == {"route": C}


def _chained_promotions() -> list[ModelFallbackNotice]:
    """KAS session-scoped moves A -> B, then B -> C, in one prompt."""
    return [
        _hop(A, B, engine="kas"),
        ModelFallbackNotice(A, B, "refused", promoted=True, engine="kas"),
        _hop(B, C, engine="kas"),
        ModelFallbackNotice(B, C, "refused", promoted=True, engine="kas"),
    ]


def test_a_chained_promotion_moves_the_channel_pick_to_the_last_target() -> None:
    prefs = {"route": A}
    assert follow_engine_promotion(prefs, "route", _provider_with(*_chained_promotions())) is True
    assert prefs == {"route": C}


@pytest.mark.asyncio
async def test_an_unattended_restore_survives_a_chained_promotion() -> None:
    provider = _UnattendedProvider("primary", A)
    provider._client._engine_fallback.apply(provider._client, _chained_promotions())
    await probe_fallback_restore(provider)
    provider.set_model.assert_awaited_once_with("primary")


@pytest.mark.asyncio
async def test_an_unattended_restore_survives_promotions_across_a_failed_restore() -> None:
    # Crew fell back P -> A; the engine promotes A -> B; restoring P fails; the
    # next prompt promotes B -> C. The next probe must still try P.
    provider = _UnattendedProvider("primary", A)
    tracker = provider._client._engine_fallback
    tracker.apply(provider._client, [ModelFallbackNotice(A, B, "refused", promoted=True)])
    provider.set_model.side_effect = RuntimeError("primary still unavailable")
    await probe_fallback_restore(provider)
    assert getattr(provider, TURN_FALLBACK_ATTR) == ("primary", B)
    provider.set_model.side_effect = provider._switch
    provider.set_model.reset_mock()
    tracker.apply(provider._client, [ModelFallbackNotice(B, C, "refused", promoted=True)])
    await probe_fallback_restore(provider)
    provider.set_model.assert_awaited_once_with("primary")
    assert getattr(provider, TURN_FALLBACK_ATTR) is None


@pytest.mark.asyncio
async def test_an_unattended_promotion_back_onto_the_primary_ends_the_fallback() -> None:
    # Crew fell back P -> A; the engine then promoted A back onto P itself.
    provider = _UnattendedProvider("primary", A)
    tracker = provider._client._engine_fallback
    tracker.apply(provider._client, [ModelFallbackNotice(A, "primary", "refused", promoted=True)])
    await probe_fallback_restore(provider)
    provider.set_model.assert_not_awaited()
    assert getattr(provider, TURN_FALLBACK_ATTR) is None


def test_the_served_model_outlives_the_walk_until_a_model_switch() -> None:
    # A recovery prompt on the same session starts a new walk; the session is
    # still on the promoted model, so usage and the chip must still name it.
    owner, tracker = _owner(model=A), EngineFallbackTracker()
    provider = SimpleNamespace(**{TRACKER_ATTR: tracker})
    tracker.apply(owner, list(_kas_promotion()))
    tracker.begin_prompt(owner)
    assert engine_served_model(provider) == B
    assert engine_attributed_model(A, provider) == B
    tracker.note_model_set()
    assert engine_served_model(provider) == ""


def test_a_promotion_the_session_has_since_left_is_not_read() -> None:
    # An agent switch activates its own model without set_model.
    provider = _provider_with(*_kas_promotion())
    getattr(provider, TRACKER_ATTR).begin_prompt(provider)
    provider._resolved_model_id = C
    assert engine_served_model(provider) == ""
    prefs = {"route": A}
    assert follow_engine_promotion(prefs, "route", provider, before=A) is False
    assert prefs == {"route": A}


def test_a_turn_scoped_hop_after_a_promotion_still_moves_the_channel_pick() -> None:
    # KAS promotes A -> B, then hops B -> C for the rest of the prompt. The next
    # prompt starts on B, so the pick and the next cold start must name B.
    provider = _provider_with(*_kas_promotion(), _hop(B, C, cause="unavailable", engine="kas"))
    assert provider._resolved_model_id == C
    prefs = {"route": A}
    assert follow_engine_promotion(prefs, "route", provider, before=A) is True
    assert prefs == {"route": B}
    getattr(provider, TRACKER_ATTR).begin_prompt(provider)
    assert provider._resolved_model_id == B
    assert engine_served_model(provider) == B


def test_a_session_moved_off_a_hop_after_a_promotion_is_not_read() -> None:
    # Something other than the engine moved the session, so the hop's restore does not apply.
    provider = _provider_with(*_kas_promotion(), _hop(B, C, cause="unavailable", engine="kas"))
    provider._resolved_model_id = "model-d"
    prefs = {"route": A}
    assert follow_engine_promotion(prefs, "route", provider, before=A) is False
    assert prefs == {"route": A}
    from kiro_crew.llm_helpers import _annotate_engine_fallback

    provider = _provider_with(*_kas_promotion())
    setattr(provider, TURN_FALLBACK_ATTR, ("primary", A))
    out = _annotate_engine_fallback("ok", provider, STOP_REASON_END_TURN)
    assert out == "⚠ model-a refused; answered by model-b for this turn.\n\nok"


def test_the_app_agent_launch_check_reads_a_real_provider_stack() -> None:
    from kiro_crew.dashboard.chat_turn.app_agent_binding import _runs_app_agent_launch

    # The session-provider shape: provider._client._handle, beside a runtime.
    runtime = SimpleNamespace(_launch_agent="app-copy")
    handle = SimpleNamespace(_runtime=runtime)
    provider = SimpleNamespace(_client=SimpleNamespace(_handle=handle, _runtime=runtime))
    assert _runs_app_agent_launch(provider, "app-copy") is True
    assert _runs_app_agent_launch(provider, "other-copy") is False


def test_a_follow_before_the_promotion_arrives_leaves_it_for_the_turn() -> None:
    # A BUSY channel call reaches the walk of a prompt still in flight.
    prefs = {"route": A}
    tracker = EngineFallbackTracker()
    provider = SimpleNamespace(**{TRACKER_ATTR: tracker})
    tracker.apply(_owner(model=A), [_hop()])
    assert follow_engine_promotion(prefs, "route", provider) is False
    tracker.apply(_owner(model=A), [ModelFallbackNotice(A, B, "refused", promoted=True)])
    assert follow_engine_promotion(prefs, "route", provider) is True
    assert prefs == {"route": B}


class _UnattendedProvider:
    """A heartbeat/cron provider: Crew's marker on top, the engine's tracker below."""

    def __init__(self, primary: str, fallback: str) -> None:
        self._client = SimpleNamespace(_model=fallback, _resolved_model_id=fallback)
        self._client._engine_fallback = EngineFallbackTracker()
        setattr(self, TURN_FALLBACK_ATTR, (primary, fallback))
        self.set_model = AsyncMock(side_effect=self._switch)

    async def _switch(self, model: str) -> None:
        self._client._model = self._client._resolved_model_id = model
        self._client._engine_fallback.note_model_set()

    @property
    def served_model(self) -> str:
        return self._client._resolved_model_id


@pytest.mark.asyncio
async def test_an_unattended_restore_survives_the_engine_promoting_crews_fallback() -> None:
    # Crew fell back P -> A; the engine then promoted A -> B.
    provider = _UnattendedProvider("primary", A)
    tracker = provider._client._engine_fallback
    tracker.apply(provider._client, [ModelFallbackNotice(A, B, "refused", promoted=True)])
    await probe_fallback_restore(provider)
    provider.set_model.assert_awaited_once_with("primary")
    assert provider.served_model == "primary"
    assert getattr(provider, TURN_FALLBACK_ATTR) is None


@pytest.mark.asyncio
async def test_an_unattended_switch_off_the_fallback_still_drops_the_record() -> None:
    # Control: a model switch, not the engine, moved the session off A.
    provider = _UnattendedProvider("primary", A)
    await provider._switch(B)
    provider.set_model.reset_mock()
    await probe_fallback_restore(provider)
    provider.set_model.assert_not_awaited()
    assert getattr(provider, TURN_FALLBACK_ATTR) is None


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
    slot._active_fallback_model = ""
    slot._refusal_fallback_primary = ""
    return slot


@pytest.mark.asyncio
@pytest.mark.parametrize("promoted", [True, False], ids=["promotion", "turn-hop"])
async def test_the_next_cold_start_requests_the_model_the_engine_promoted(
    tmp_path, promoted: bool
) -> None:
    # The decision this pins: once kiro-cli promotes B in place of the pinned A,
    # the session stays on B. A cold start re-applies the session's model from
    # the slot, so without the follow it would request A, the model that refused.
    state, client = _runner_state(tmp_path)
    slot = _runner_slot()
    slot.model = A
    notices = [ModelFallbackNotice(A, B, "refused", engine="v2")]
    if promoted:
        notices.append(ModelFallbackNotice(A, B, "refused", promoted=True, engine="v2"))
    _set_stream(
        client,
        [AcpEvent(kind=EVENT_MODEL_FALLBACK, model_fallback=n) for n in notices] + [_complete()],
    )
    await _drive(state, slot)
    assert state.sessions.get_or_create.await_args.kwargs["model"] == A
    # The session is gone (respawn, eviction); the next turn starts a new one.
    state.sessions.get_or_create.reset_mock()
    await _drive(state, slot)
    assert state.sessions.get_or_create.await_args.kwargs["model"] == (B if promoted else A)


def test_dashboard_promotion_follows_a_pin_in_another_spelling() -> None:
    # The pin is the engine's model in another spelling; it was not withheld.
    slot, state = _slot(model="global.vendor.model-a[1m]"), MagicMock()
    client = SimpleNamespace(served_model=B, _explicit_pick_epoch=0, model_pin_refused="")
    walk = EngineFallbackWalk()
    for notice in normalize_kas({"from": A, "to": B, "trigger": "refusal", "scope": "session"}):
        dash.record_engine_fallback(state, slot, client, walk, notice)
    assert slot.model == B


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


def test_dashboard_promotion_leaves_a_pick_made_during_the_turn() -> None:
    # The user picked C mid-turn; the boundary applies it, so B answers this turn only.
    slot, state = _slot(model=C), MagicMock()
    slot._deferred_model_switch_key = "s1"
    client = SimpleNamespace(served_model=B, _explicit_pick_epoch=0)
    walk = EngineFallbackWalk()
    for notice in normalize_kas({"from": A, "to": B, "trigger": "refusal", "scope": "session"}):
        dash.record_engine_fallback(state, slot, client, walk, notice)
    assert slot.model == C
    assert slot._model_pick_gen == 0 and client._explicit_pick_epoch == 0
    assert walk.line("answered") == "model-a refused; answered by model-b for this turn."


def test_dashboard_turn_hop_leaves_the_slot_alone() -> None:
    slot, state = _slot(model=A), MagicMock()
    dash.record_engine_fallback(
        state, slot, SimpleNamespace(), EngineFallbackWalk(), _hop(cause="unavailable")
    )
    assert slot.model == A and slot._model_pick_gen == 0
    state.push_slots_update.assert_not_called()


@pytest.mark.parametrize("pick", ["", "auto"])
def test_dashboard_promotion_leaves_an_unpinned_slot_unpinned(pick: str) -> None:
    slot, state = _slot(model=pick), MagicMock()
    client = SimpleNamespace(served_model=B, _explicit_pick_epoch=0)
    walk = EngineFallbackWalk()
    for notice in normalize_kas({"from": A, "to": B, "trigger": "refusal", "scope": "session"}):
        dash.record_engine_fallback(state, slot, client, walk, notice)
    assert slot.model == pick
    # The chip still names the served model.
    slot.record_served_model.assert_called_once_with(B)


# Crew's own fallback serves the session: P is the user's pin, F Crew's candidate,
# and the engine then moves F to G.
P, F, G = "model-p", "model-f", "model-g"


def test_dashboard_promotion_leaves_a_withheld_pin_alone() -> None:
    # Startup withheld pin A and served C; the engine then promoted C -> B.
    slot, state = _slot(model=A), MagicMock()
    client = SimpleNamespace(served_model=B, _explicit_pick_epoch=0, model_pin_refused=A)
    walk = EngineFallbackWalk()
    for notice in normalize_kas({"from": C, "to": B, "trigger": "refusal", "scope": "session"}):
        dash.record_engine_fallback(state, slot, client, walk, notice)
    assert slot.model == A
    assert slot._model_pick_gen == 0 and client._explicit_pick_epoch == 0
    slot.record_served_model.assert_called_with(B)


def _crew_fallback_slot(kind: str) -> SimpleNamespace:
    slot = SimpleNamespace(
        key="s1",
        model=P,
        _model_pick_lock=None,
        _model_pick_gen=0,
        _active_fallback_model="",
        _fallback_primary_model="",
        _fallback_slot_model="",
        _fallback_pick_gen=0,
        _fallback_client_pick_epoch=0,
        _refusal_fallback_primary="",
        _refusal_fallback_candidate="",
        record_served_model=MagicMock(),
    )
    if kind == "throttle":
        slot._active_fallback_model = F
        slot._fallback_primary_model = P
        slot._fallback_slot_model = P
    else:
        slot._refusal_fallback_primary = P
        slot._refusal_fallback_candidate = F
    return slot


def _engine_promotes_f_to_g(slot: SimpleNamespace, client: object) -> EngineFallbackWalk:
    walk = EngineFallbackWalk()
    for notice in normalize_kas({"from": F, "to": G, "trigger": "refusal", "scope": "session"}):
        dash.record_engine_fallback(MagicMock(), slot, client, walk, notice)
    return walk


@pytest.mark.parametrize("kind", ["throttle", "refusal"])
def test_a_promotion_during_crew_fallback_keeps_the_pin_and_moves_crews_record(
    kind: str,
) -> None:
    from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

    slot = _crew_fallback_slot(kind)
    client = SimpleNamespace(served_model=G, _explicit_pick_epoch=0)
    if kind == "throttle":
        setattr(client, TURN_FALLBACK_ATTR, (P, F))
    walk = _engine_promotes_f_to_g(slot, client)
    # The user's pin and the pick generation are untouched.
    assert slot.model == P
    assert slot._model_pick_gen == 0 and client._explicit_pick_epoch == 0
    # Crew's record follows the engine, so its restore still targets P.
    if kind == "throttle":
        assert slot._active_fallback_model == G
        assert getattr(client, TURN_FALLBACK_ATTR) == (P, G)
    else:
        assert slot._refusal_fallback_candidate == G
    # Said as a move for this turn: Crew will take the session back to P.
    assert walk.line("answered") == "model-f refused; answered by model-g for this turn."
    slot.record_served_model.assert_called_once_with(G)


@pytest.mark.parametrize("kind", ["throttle", "refusal"])
def test_a_promotion_back_onto_the_primary_ends_crews_fallback(kind: str) -> None:
    from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

    slot = _crew_fallback_slot(kind)
    client = SimpleNamespace(served_model=P, _explicit_pick_epoch=0)
    if kind == "throttle":
        setattr(client, TURN_FALLBACK_ATTR, (P, F))
    walk = EngineFallbackWalk()
    for notice in normalize_kas({"from": F, "to": P, "trigger": "refusal", "scope": "session"}):
        dash.record_engine_fallback(MagicMock(), slot, client, walk, notice)
    # Nothing is left for Crew's restore to do, and the pin is already P.
    assert slot.model == P
    assert not slot._active_fallback_model and not slot._refusal_fallback_primary
    assert getattr(client, TURN_FALLBACK_ATTR, None) is None
    assert walk.line("answered") == (
        "model-f refused; answered by model-p, now this conversation's model."
    )


@pytest.mark.asyncio
async def test_crews_throttle_restore_still_returns_the_session_to_the_pin() -> None:
    from kiro_crew.dashboard import chat_runner as crew
    from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

    class _Client:
        def __init__(self) -> None:
            self._model = self.served_model = G
            self._explicit_pick_epoch = 0
            setattr(self, TURN_FALLBACK_ATTR, (P, F))

        async def set_model(self, model: str) -> None:
            self._model = self.served_model = model

    slot, client = _crew_fallback_slot("throttle"), _Client()
    _engine_promotes_f_to_g(slot, client)
    await crew._probe_fallback_restore_for_slot(slot, client)
    assert client._model == P
    assert slot.model == P and slot._active_fallback_model == ""


@pytest.mark.asyncio
async def test_without_the_retarget_the_restore_drops_the_record_instead() -> None:
    # Control for the test above: left on F, the probe reads the engine's move
    # as the session leaving the fallback and restores nothing.
    from kiro_crew.dashboard import chat_runner as crew
    from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

    client = SimpleNamespace(_model=G, served_model=G, set_model=AsyncMock())
    setattr(client, TURN_FALLBACK_ATTR, (P, F))
    slot = _crew_fallback_slot("throttle")
    await crew._probe_fallback_restore_for_slot(slot, client)
    client.set_model.assert_not_called()
    assert slot._active_fallback_model == ""


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
