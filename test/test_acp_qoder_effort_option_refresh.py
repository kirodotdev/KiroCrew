"""Qoder's effort option follows the current model, so a config write must refresh it.

Qoder serves ``reasoning_effort`` only while a reasoning-capable model is current.
The response to ``session/set_config_option`` carries the session's complete
``configOptions``, and the client stores them: a model switch then changes what
the effort writes see. The reasoning list is the measured v167 ``session/new``
result; the auto list is that same list without the effort selector.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from kiro_crew import model_registry
from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.types import ACP_BACKEND_QODER
from kiro_crew.dashboard import chat_persistence
from kiro_crew.providers.acp import AcpProvider

QODER_CORPUS = Path(__file__).parents[1] / "test/fixtures/acp_frames/qoder"
HANDSHAKE = QODER_CORPUS / "handshake-live.jsonl"
V167 = QODER_CORPUS / "v167-handshake-live.jsonl"
# Qoder advertises xhigh, low, medium, none; the getter publishes least to most effort.
ORDERED = ["none", "low", "medium", "xhigh"]


def _result(fixture: Path, request_id: int) -> dict:
    for line in fixture.read_text(encoding="utf-8").splitlines():
        frame = json.loads(line)
        if frame.get("id") == request_id and "method" not in frame and "result" in frame:
            return frame["result"]
    raise AssertionError(f"no response {request_id} in {fixture.name}")


# session/new on qfmodel: mode, model and the reasoning_effort selector.
REASONING = _result(V167, 3)["configOptions"]
# The response to the measured reasoning_effort write.
EFFORT_ECHO = _result(V167, 4)
# session/new on auto: the same options without the effort selector.
AUTO = [option for option in REASONING if option.get("id") != "reasoning_effort"]


@pytest.fixture(autouse=True)
def _cold_qoder_model_cache(_floor_monkeypatch):
    """Advertised ids are per-account; a cache left by another test is not evidence."""
    _floor_monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    _floor_monkeypatch.setattr(model_registry, "_ADVERTISED_MODEL_ROWS", {})
    _floor_monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS_OBSERVED_AT", {})
    _floor_monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)


@pytest.fixture(autouse=True)
def effort_levels_sink(_floor_monkeypatch):
    """Qoder's advertised levels are registered in a process-wide set; keep each test's answer local."""
    sink = MagicMock()
    _floor_monkeypatch.setattr(chat_persistence, "register_reasoning_effort_values", sink)
    return sink


@pytest.fixture(autouse=True)
def global_fallback_sink(_floor_monkeypatch):
    """The fallback order unrelated slots read; Qoder's levels must never replace it."""
    sink = MagicMock()
    _floor_monkeypatch.setattr(chat_persistence, "update_reasoning_effort_values", sink)
    return sink


def _client(tmp_path, options: list[dict], *, model: str = "") -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_QODER)
    client._session_id = "qoder-session-1"
    client._capture_available_models(_result(HANDSHAKE, 3))
    client._store_session_config({"configOptions": options})
    client._model = model
    client._send_request = AsyncMock(return_value=11)  # type: ignore[method-assign]
    return client


def _provider(client: AcpClient) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=ACP_BACKEND_QODER)
    provider._client = client
    return provider


def _sent_config_ids(client: AcpClient) -> list[str]:
    return [sent.args[1]["configId"] for sent in client._send_request.await_args_list]


def _unknown_option() -> AcpError:
    return AcpError(
        "JSON-RPC error: {'code': -32603, 'message': 'Internal error', "
        "'data': {'details': 'Unknown config option: reasoning_effort'}}"
    )


@pytest.mark.asyncio
async def test_set_config_option_response_replaces_config_options(
    tmp_path, effort_levels_sink, global_fallback_sink
):
    client = _client(tmp_path, AUTO)
    assert client.supports_config_option("reasoning_effort") is False
    client._wait_for_response = AsyncMock(  # type: ignore[method-assign]
        return_value={"configOptions": REASONING}
    )

    await client.set_config_option("model", "qfmodel")

    assert client.supports_config_option("reasoning_effort") is True
    assert client.get_valid_effort_levels() == ORDERED
    effort_levels_sink.assert_called_with(ORDERED)
    global_fallback_sink.assert_not_called()


@pytest.mark.asyncio
async def test_set_config_option_response_without_config_options_keeps_cache(
    tmp_path, effort_levels_sink
):
    client = _client(tmp_path, REASONING)
    effort_levels_sink.reset_mock()
    client._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]

    await client.set_config_option("reasoning_effort", "low")

    assert client.acp_config_options == REASONING
    assert client.get_valid_effort_levels() == ORDERED
    effort_levels_sink.assert_not_called()


@pytest.mark.asyncio
async def test_qoder_switch_auto_to_reasoning_model_reapplies_carried_effort(tmp_path):
    """Effort is keyed by the model a switch targets, so the level is stored under qfmodel."""
    client = _client(tmp_path, AUTO, model="auto")
    client._wait_for_response = AsyncMock(  # type: ignore[method-assign]
        side_effect=[{"configOptions": REASONING}, EFFORT_ECHO]
    )
    provider = _provider(client)
    provider._effort_per_model["qfmodel"] = "low"

    await provider.set_model("qfmodel")

    assert client._send_request.await_args_list == [
        call(
            "session/set_config_option",
            {"sessionId": "qoder-session-1", "configId": "model", "value": "qfmodel"},
        ),
        call(
            "session/set_config_option",
            {"sessionId": "qoder-session-1", "configId": "reasoning_effort", "value": "low"},
        ),
    ]


@pytest.mark.asyncio
async def test_qoder_switch_to_auto_skips_effort_write(tmp_path):
    """The session serves qfmodel with its effort option; auto's answer drops it."""
    client = _client(tmp_path, REASONING, model="qfmodel")
    client._resolved_model_id = "qfmodel"
    client._wait_for_response = AsyncMock(  # type: ignore[method-assign]
        return_value={"configOptions": AUTO}
    )
    provider = _provider(client)
    provider._effort_per_model["qfmodel"] = "low"

    await provider.set_model("auto")

    assert _sent_config_ids(client) == ["model"]
    assert client.supports_config_option("reasoning_effort") is False
    assert await provider.change_effort("low") is False
    assert _sent_config_ids(client) == ["model"]


@pytest.mark.asyncio
async def test_initial_effort_on_auto_does_not_log_applied(tmp_path, caplog):
    """Auto offers no level, so the resolver answers None: nothing is written or logged."""
    client = _client(tmp_path, AUTO, model="auto")
    provider = _provider(client)
    provider._effort_per_model["auto"] = "low"
    caplog.set_level(logging.INFO, logger="kiro_crew.providers.acp")

    await provider._apply_initial_effort()

    client._send_request.assert_not_awaited()
    assert "ACP initial effort applied" not in caplog.text


@pytest.mark.asyncio
async def test_initial_effort_refused_as_unknown_option_is_not_logged_applied(tmp_path, caplog):
    client = _client(tmp_path, REASONING, model="qfmodel")
    client._wait_for_response = AsyncMock(side_effect=_unknown_option())  # type: ignore[method-assign]
    provider = _provider(client)
    provider._effort_per_model["qfmodel"] = "low"
    caplog.set_level(logging.INFO, logger="kiro_crew.providers.acp")

    await provider._apply_initial_effort()

    assert "ACP initial effort applied" not in caplog.text
    assert "effort 'low' not applied to this session (option or level not offered)" in caplog.text


@pytest.mark.asyncio
async def test_change_effort_unknown_option_returns_false_and_rolls_back(tmp_path, caplog):
    client = _client(tmp_path, REASONING, model="qfmodel")
    client._wait_for_response = AsyncMock(side_effect=_unknown_option())  # type: ignore[method-assign]
    provider = _provider(client)
    caplog.set_level(logging.INFO, logger="kiro_crew.providers.acp")

    ok = await provider.change_effort("low")

    assert ok is False
    assert "qfmodel" not in provider._effort_per_model
    assert "live-changed" not in caplog.text
    client._send_request.assert_awaited_once_with(
        "session/set_config_option",
        {"sessionId": "qoder-session-1", "configId": "reasoning_effort", "value": "low"},
    )
