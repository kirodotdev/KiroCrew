"""Codex footer attribution never treats a selection's effort suffix as a turn fact."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_runner_coverage_helpers import (
    _complete,
    _drive,
    _errors,
    _runner_state,
    _set_stream,
)
from chat_runner_coverage_helpers import _slot as _runner_slot

from kiro_crew.acp.types import EVENT_TEXT_CHUNK, TurnUsage
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.handlers.usage import read_turn_model
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMEvent


def _slot(provider):
    slot = _ChatSlot("codex-footer-effort")
    # As turn admission publishes it: the inner AcpClient, not the provider.
    slot._acp_client = provider._client
    row = slot.append("assistant", "Done.", "reply", broadcast=False)
    slot._turn_reply_mids.append(row["meta"]["mid"])
    return slot


@pytest.mark.asyncio
@pytest.mark.parametrize("abnormal", [False, True])
async def test_confirmed_effort_change_does_not_leave_low_on_the_reply(
    tmp_path, monkeypatch, abnormal
):
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    client = provider._client
    client._session_id = "session"
    client._resolved_model_id = "test-model[low]"
    client._send_request = AsyncMock(return_value=1)
    client._wait_for_response = AsyncMock(
        return_value={
            "configOptions": [
                {
                    "id": "reasoning_effort",
                    "type": "select",
                    "currentValue": "xhigh",
                    "options": [{"value": level} for level in ("low", "xhigh")],
                }
            ]
        }
    )
    await client.set_config_option("reasoning_effort", "xhigh")
    slot = _slot(provider)
    selected = read_turn_model(provider)

    if abnormal:
        monkeypatch.setattr(chat_runner, "_broadcast_turn_stats", lambda *args: None)
        turn = SimpleNamespace(
            slot=slot,
            state=None,
            client=provider,
            _turn_msg_boundary=0,
            _turn_start_mid=None,
            _turn_footer_attached=False,
        )
        attached = chat_runner._attach_abnormal_turn_stats(turn, TurnUsage(), 1000)
    else:
        attached = chat_runner._attach_turn_stats(
            slot, 1000, 0.0, 0.0, model=selected, model_source=provider
        )

    assert attached
    stats = slot.messages[-1]["meta"]["turn_stats"]
    assert stats["model"] == "test-model[xhigh]"
    assert "reasoning_effort" not in stats
    assert client.acp_config_options[0]["currentValue"] == "xhigh"
    assert read_turn_model(provider) == selected == "test-model[low]"


@pytest.mark.parametrize(
    ("backend", "model", "expected"),
    [
        (ACP_BACKEND_CODEX, "test-model[low]", "test-model"),
        (ACP_BACKEND_CODEX, "test-model[xhigh]", "test-model"),
        (ACP_BACKEND_CODEX, "test-model", "test-model"),
        (ACP_BACKEND_CODEX, "test-model[1m]", "test-model[1m]"),
        (ACP_BACKEND_CLAUDE, "test-model[low]", "test-model[low]"),
        (ACP_BACKEND_CLAUDE, "test-model[1m]", "test-model[1m]"),
    ],
)
def test_shared_footer_builder_keeps_model_selection_unchanged(tmp_path, backend, model, expected):
    provider = AcpProvider(work_dir=tmp_path, acp_backend=backend)
    provider._client._resolved_model_id = model

    stats = chat_runner.turn_stats_meta(1000, 1.0, 0.0, model, model_source=provider)

    assert stats == {"elapsed_ms": 1000, "credits": 1.0, "model": expected}
    assert read_turn_model(provider) == model


def test_footer_without_a_known_harness_keeps_the_identifier():
    stats = chat_runner.turn_stats_meta(1000, 0.0, 0.0, "test-model[low]")

    assert stats["model"] == "test-model[low]"


@pytest.mark.parametrize("level", ["", "auto", "default", None, {}, "x" * 22, "xhigh\n"])
def test_an_unknown_or_invalid_report_never_falls_back_to_the_selection(tmp_path, level):
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    client = provider._client
    client._resolved_model_id = "test-model[low]"
    client._acp_config_options = [{"id": "reasoning_effort", "currentValue": level}]

    stats = chat_runner.turn_stats_meta(
        1000, 0.0, 0.0, read_turn_model(provider), model_source=provider
    )

    assert provider.reported_effort is None
    assert stats == {"elapsed_ms": 1000, "model": "test-model"}


@pytest.mark.parametrize("snapshot", ["xhigh", ""])
def test_a_later_effort_change_does_not_rewrite_the_captured_level(tmp_path, snapshot):
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    provider._client._acp_config_options = [{"id": "reasoning_effort", "currentValue": "low"}]

    stats = chat_runner.turn_stats_meta(
        1000,
        0.0,
        0.0,
        "test-model[low]",
        model_source=provider,
        reasoning_effort=snapshot,
    )

    expected = {"elapsed_ms": 1000, "model": "test-model"}
    if snapshot:
        expected.update(model="test-model[xhigh]")
    assert stats == expected
    assert provider.reported_effort == "low"


def test_auto_is_not_turned_into_a_model_id(tmp_path):
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    stats = chat_runner.turn_stats_meta(
        1000, 0.0, 0.0, "auto", model_source=provider, reasoning_effort="xhigh"
    )
    assert stats == {"elapsed_ms": 1000, "model": "auto"}


def test_a_confirmed_suffix_cannot_put_the_model_over_its_bound(tmp_path):
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    stats = chat_runner.turn_stats_meta(
        1000,
        0.0,
        0.0,
        "m" * 250 + "[low]",
        model_source=provider,
        reasoning_effort="xhigh",
    )
    assert stats == {"elapsed_ms": 1000}


def test_a_failed_harness_report_does_not_drop_the_reply_footer(tmp_path):
    class ClosedSession(AcpProvider):
        @property
        def reported_effort(self):
            raise RuntimeError("closed session")

    provider = ClosedSession(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)

    stats = chat_runner.turn_stats_meta(1000, 0.0, 0.0, "test-model[low]", model_source=provider)

    assert stats == {"elapsed_ms": 1000, "model": "test-model"}


@pytest.mark.asyncio
async def test_a_stream_without_completion_keeps_its_reply(tmp_path):
    state, client = _runner_state(tmp_path)
    slot = _runner_slot("no-effort-completion")
    client.client.pop_pending_oauth_requests = MagicMock(return_value=[])
    _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="The requested answer.")])

    await _drive(state, slot)

    assert _errors(slot) == []
    assert any(
        row.get("role") == "assistant" and row.get("content") == "The requested answer."
        for row in slot.messages
    )


@pytest.mark.asyncio
async def test_an_effort_change_during_completion_awaits_keeps_the_turn_level(
    tmp_path, monkeypatch
):
    # The turn is done once its completion arrives, so a picker change can land
    # while the completion arm awaits; the footer must keep the level that ran.
    state, client = _runner_state(tmp_path)
    slot = _runner_slot("effort-during-completion")
    client.client.pop_pending_oauth_requests = MagicMock(return_value=[])
    client.reported_effort = "low"
    real_report = chat_runner._report_consumed

    async def _change_effort_mid_await(turn, *args, **kwargs):
        # Only the completion arm's call (no flags); the first-output call
        # passes irreversible=True and runs before the turn is done.
        if not args and not kwargs:
            client.reported_effort = "xhigh"
        return await real_report(turn, *args, **kwargs)

    monkeypatch.setattr(chat_runner, "_report_consumed", _change_effort_mid_await)
    # The level the footer's model suffix is built from.
    footer_levels = []
    real_meta = chat_runner.turn_stats_meta

    footer_sources = []

    def _record_level(*args, **kwargs):
        footer_levels.append(kwargs.get("reasoning_effort"))
        footer_sources.append(kwargs.get("model_source"))
        return real_meta(*args, **kwargs)

    monkeypatch.setattr(chat_runner, "turn_stats_meta", _record_level)
    _set_stream(
        client,
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done at low."),
            _complete(usage=TurnUsage(duration_ms=1000)),
        ],
    )

    await _drive(state, slot)

    assert any((row.get("meta") or {}).get("turn_stats") for row in slot.messages), slot.messages
    assert footer_levels and footer_levels[-1] == "low"
    # The harness is read off the provider the turn ran on, not the slot's
    # published inner client, whose harness reads as unknown.
    assert footer_sources[-1] is client
    assert slot._acp_client is not client
