"""Derived-tier escalation through the real chat runner.

``test_decisions_model_route_derived.py`` pins the escalation rules on the point
itself. This file drives ``chat_runner._run_chat`` end to end against a recording
client, because the rules only matter if the runner feeds them: it must mark the
tier a turn ran at once the switch lands, report how the turn ended from its
``finally``, and pass a regenerate through as a retry. Each test asserts the id
``set_model`` received, which is the one observable proof a turn moved.

The model ids are fakes; test files are outside the tree the model-id gate reads.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_decisions_strip_rides_message import _quiet_sel, _runner_state, _settle, _slot

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT, DecisionsConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.decisions import gate as gate_mod
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions import outcomes
from kiro_crew.decisions.points import model_route as mr
from kiro_crew.decisions.types import Answer
from kiro_crew.providers.base import LLMEvent

ADVERTISED = ["model-a", "model-b", "model-c"]
TIER_MAP = {"simple": "model-a", "medium": "model-b", "complex": "model-c"}


@pytest.fixture(autouse=True)
def clean_state():
    outcomes.reset()
    for store in (mr._LAST_TIER, mr._FLOOR, mr._UNSETTLED):
        store.clear()
    yield
    outcomes.reset()
    for store in (mr._LAST_TIER, mr._FLOOR, mr._UNSETTLED):
        store.clear()


@pytest.fixture(autouse=True)
def consented(tmp_path, _floor_monkeypatch):
    path = tmp_path / "decisions_consent.json"
    path.write_text(
        json.dumps({"enabled": True, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT}),
        encoding="utf-8",
    )
    _floor_monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    _floor_monkeypatch.setattr(log_mod, "log_dir", lambda: tmp_path / "decisions")


@pytest.fixture
def switch(monkeypatch):
    """Pin the live snapshot: the owner's tier map, derivation on or off."""

    def _set(on: bool) -> None:
        cfg = DecisionsConfig(model_route=dict(TIER_MAP), model_route_derive=on)
        monkeypatch.setattr(gate_mod, "_snapshot", lambda: SimpleNamespace(decisions=cfg))

    _set(True)
    return _set


@pytest.fixture
def answers(monkeypatch):
    import kiro_crew.decisions.impl_jev as impl_mod

    state = {"tier": "simple"}

    class _Oracle:
        async def ask(self, _state, questions):
            return {q.id: Answer(id=q.id, value=state["tier"], p=0.91) for q in questions}

    monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: _Oracle())

    def _set(tier: str) -> None:
        state["tier"] = tier

    return _set


def _routed_slot(key: str = "chat-escalate-1"):
    slot = _slot(key)
    slot.jev_route = True
    return slot


async def _run(tmp_path, slot, *, stop_reason: str = "end_turn", **kwargs):
    """One scripted turn that ends with *stop_reason*; returns the client."""
    kwargs.setdefault("_directive_user_origin", True)
    state, client = _runner_state(tmp_path)
    client.available_models = MagicMock(return_value=[{"modelId": name} for name in ADVERTISED])
    client.served_model = "model-b"

    async def _switch(model, *_args, **_kwargs):
        # A switch that takes: the session then serves what was asked for.
        client.served_model = model

    client.set_model = AsyncMock(side_effect=_switch)

    async def _stream(*_args, **_kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="an answer")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason)

    client.stream = MagicMock(side_effect=lambda *a, **k: _stream())
    with _quiet_sel():
        await chat_runner._run_chat(state, slot, "please rename this variable", **kwargs)
    await _settle(slot)
    return client


def _switched_to(client) -> list[str]:
    return [call.args[0] for call in client.set_model.await_args_list]


class TestTheRunnerFeedsEscalation:
    @pytest.mark.asyncio
    async def test_a_turn_that_hit_its_limit_moves_the_next_one_up_one_tier(
        self, tmp_path, switch, answers
    ):
        slot = _routed_slot()
        answers("simple")
        first = await _run(tmp_path, slot, stop_reason="max_tokens")
        assert _switched_to(first) == ["model-a"]
        second = await _run(tmp_path, slot)
        assert _switched_to(second) == ["model-b"], "one tier up, from the tier it ran at"

    @pytest.mark.asyncio
    async def test_a_provider_error_ending_moves_nothing(self, tmp_path, switch, answers):
        slot = _routed_slot()
        answers("simple")
        await _run(tmp_path, slot, stop_reason="error: backend unavailable")
        second = await _run(tmp_path, slot)
        assert _switched_to(second) == ["model-a"]

    @pytest.mark.asyncio
    async def test_a_regenerate_runs_one_tier_above_the_turn_it_repeats(
        self, tmp_path, switch, answers
    ):
        slot = _routed_slot()
        answers("medium")
        first = await _run(tmp_path, slot)
        assert _switched_to(first) == ["model-b"]
        again = await _run(tmp_path, slot, regenerate_hint="answer it differently")
        assert _switched_to(again) == ["model-c"]

    @pytest.mark.asyncio
    async def test_with_the_switch_off_the_runner_changes_nothing(self, tmp_path, switch, answers):
        switch(False)
        slot = _routed_slot()
        answers("simple")
        await _run(tmp_path, slot, stop_reason="max_tokens")
        second = await _run(tmp_path, slot, regenerate_hint="answer it differently")
        assert _switched_to(second) == ["model-a"]
        assert mr._FLOOR == {} and mr._LAST_TIER == {} and mr._UNSETTLED == {}
