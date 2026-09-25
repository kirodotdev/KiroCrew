"""An inheriting claude session runs the model its adapter reports.

With ``agent.model`` left at ``auto`` Crew sends no model, and claude-agent-acp
resolves one from ``ANTHROPIC_MODEL`` or the user's ``settings.model``. It reports
that id as the ``model`` option's current value, but when the id is the setting
verbatim it does not pass it on to Claude Code. After a resume Claude Code can then
run its own built-in default while the adapter still reports the settings model, and
a custom gateway that does not serve that default refuses every turn until the user
runs ``/model``.

These drive ``_initialize_session`` through a fake adapter on both routes a session
starts by (``session/new`` and ``session/load``), and pin what is left alone: a
session with no model setting, an explicit pin, and the kiro backend.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from kiro_crew import model_registry as mr
from kiro_crew.acp.client import DEFAULT_MODEL, AcpClient, AcpError, AcpProcessDied
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KIRO,
    METHOD_INITIALIZE,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODEL,
)

# The adapter lists its ``default`` pseudo-model first and reports it when no model
# setting applies. The other two stand in for ids a custom gateway serves.
_ADVERTISED = ("default", "gateway-model-a", "gateway-model-b")


@pytest.fixture(autouse=True)
def _isolate_advertised_cache(monkeypatch):
    """Session init feeds the captured list into the process-wide model cache."""
    monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})


class _FakeClaudeAdapter:
    """Answers the claude-agent-acp handshake and records every request.

    ``current`` is the ``model`` option's ``currentValue`` in the session/new and
    session/load responses, i.e. what the adapter resolved from its settings.
    """

    def __init__(self, current: str):
        self.current = current
        self.sent: list[tuple[str, dict]] = []

    async def send_request(self, method: str, params: dict) -> int:
        self.sent.append((method, params))
        return len(self.sent)

    async def wait_for_response(self, req_id, timeout=None, *, method="", expected_mcp=None):
        sent_method = self.sent[req_id - 1][0]
        if sent_method == METHOD_INITIALIZE:
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
        if sent_method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD):
            resp = {
                "modes": {"currentModeId": "default", "availableModes": []},
                "configOptions": [
                    {
                        "id": "model",
                        "name": "Model",
                        "category": "model",
                        "type": "select",
                        "currentValue": self.current,
                        "options": [{"value": v, "name": v} for v in _ADVERTISED],
                    }
                ],
            }
            if sent_method == METHOD_SESSION_NEW:
                resp["sessionId"] = "sess-new"
            return resp
        return {}

    def methods(self) -> list[str]:
        return [m for m, _ in self.sent]

    def model_pushes(self) -> list[str]:
        return [
            p["value"]
            for m, p in self.sent
            if m == METHOD_SET_CONFIG_OPTION and p.get("configId") == "model"
        ]


def _claude_client(tmp_path, adapter: _FakeClaudeAdapter, *, model="", resume=None):
    client = AcpClient(work_dir=tmp_path, model=model, acp_backend=ACP_BACKEND_CLAUDE)
    client._resume_session_id = resume
    client._session_mcp_cache = []
    client._send_request = adapter.send_request  # type: ignore[method-assign]
    client._wait_for_response = adapter.wait_for_response  # type: ignore[method-assign]
    client._drain_notifications = AsyncMock()  # type: ignore[method-assign]
    return client


class TestTheReportedSettingsModelIsReapplied:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", ["", DEFAULT_MODEL])
    async def test_on_session_new(self, tmp_path, caplog, model):
        adapter = _FakeClaudeAdapter(current="gateway-model-a")
        client = _claude_client(tmp_path, adapter, model=model)

        with caplog.at_level(logging.INFO, logger="kiro_crew.acp.client"):
            await client._initialize_session()

        assert METHOD_SESSION_NEW in adapter.methods()
        assert adapter.model_pushes() == ["gateway-model-a"]
        assert client._resolved_model_id == "gateway-model-a"
        # Still inheriting: the settings seed, the warm-pool re-apply and the model
        # chip read this field, and none of them may start treating it as a pin.
        assert client._model == DEFAULT_MODEL
        assert "backend reports gateway-model-a" in caplog.text

    @pytest.mark.asyncio
    async def test_on_session_load(self, tmp_path, caplog):
        """The resume is the route the fallback was reported on."""
        adapter = _FakeClaudeAdapter(current="gateway-model-a")
        client = _claude_client(tmp_path, adapter, resume="sess-old")

        with caplog.at_level(logging.INFO, logger="kiro_crew.acp.client"):
            await client._initialize_session()

        assert client._resumed is True
        assert client._session_id == "sess-old"
        assert METHOD_SESSION_LOAD in adapter.methods()
        assert METHOD_SESSION_NEW not in adapter.methods()
        assert adapter.model_pushes() == ["gateway-model-a"]
        assert client._model == DEFAULT_MODEL
        assert "backend reports gateway-model-a" in caplog.text


class TestWhatIsLeftAlone:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("resume", [None, "sess-old"])
    async def test_no_model_setting_sends_nothing(self, tmp_path, resume):
        """The adapter reports the head of its list: there is no setting to re-apply."""
        adapter = _FakeClaudeAdapter(current="default")
        client = _claude_client(tmp_path, adapter, resume=resume)

        await client._initialize_session()

        assert adapter.model_pushes() == []
        assert client._resolved_model_id == "default"

    @pytest.mark.asyncio
    async def test_an_explicit_pin_is_pushed_as_before(self, tmp_path):
        """The pin goes out once; the reported settings model is not sent as well."""
        adapter = _FakeClaudeAdapter(current="gateway-model-a")
        client = _claude_client(tmp_path, adapter, model="gateway-model-b")

        await client._initialize_session()

        assert adapter.model_pushes() == ["gateway-model-b"]
        assert client._model == "gateway-model-b"
        assert client._resolved_model_id == "gateway-model-b"

    @pytest.mark.asyncio
    async def test_a_report_the_list_does_not_carry_sends_nothing(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._session_id = "sess-1"
        client._available_models = [{"modelId": m, "name": m} for m in _ADVERTISED]
        client._resolved_model_id = "gateway-model-z"
        client.set_config_option = AsyncMock()  # type: ignore[method-assign]

        await client._apply_startup_model()

        client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_kiro_backend_is_unchanged(self, tmp_path):
        """A served, non-head kiro default is not re-sent; kiro keeps its own check."""
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_KIRO)
        client._session_id = "sess-1"
        client._available_models = [{"modelId": m, "name": m} for m in ("model-x", "model-y")]
        client._resolved_model_id = "model-y"
        sent: list[tuple[str, dict]] = []

        async def _send_request(method, params=None):
            sent.append((method, params or {}))
            return len(sent)

        client._send_request = _send_request  # type: ignore[method-assign]
        client.set_config_option = AsyncMock()  # type: ignore[method-assign]

        await client._apply_startup_model()

        assert sent == []
        client.set_config_option.assert_not_awaited()
        assert client._resolved_model_id == "model-y"

    @pytest.mark.asyncio
    async def test_the_kiro_served_default_check_still_runs(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_KIRO)
        client._session_id = "sess-1"
        client._available_models = [{"modelId": m, "name": m} for m in ("model-x", "model-y")]
        client._resolved_model_id = "auto"
        sent: list[tuple[str, dict]] = []

        async def _send_request(method, params=None):
            sent.append((method, params or {}))
            return len(sent)

        client._send_request = _send_request  # type: ignore[method-assign]

        await client._apply_startup_model()

        assert sent == [(METHOD_SET_MODEL, {"sessionId": "sess-1", "modelId": "model-x"})]


class TestBestEffort:
    """The session started without this write, so failing it must not end the session."""

    def _client(self, tmp_path) -> AcpClient:
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._session_id = "sess-1"
        client._available_models = [{"modelId": m, "name": m} for m in _ADVERTISED]
        client._resolved_model_id = "gateway-model-a"
        return client

    @pytest.mark.asyncio
    async def test_a_failed_request_is_logged_and_the_session_continues(self, tmp_path, caplog):
        client = self._client(tmp_path)
        client.set_config_option = AsyncMock(  # type: ignore[method-assign]
            side_effect=AcpError("JSON-RPC error: internal")
        )

        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            await client._apply_startup_model()

        assert "could not be re-applied" in caplog.text
        assert client._resolved_model_id == "gateway-model-a"
        assert client._model == DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_a_refused_value_leaves_the_session_on_the_adapter_model(self, tmp_path):
        client = self._client(tmp_path)
        client.set_config_option = AsyncMock(  # type: ignore[method-assign]
            side_effect=AcpError("Invalid value for config option model: gateway-model-a")
        )

        await client._apply_startup_model()

        assert client._resolved_model_id == "gateway-model-a"
        assert client._model == DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_a_dead_process_still_propagates(self, tmp_path):
        client = self._client(tmp_path)
        client.set_config_option = AsyncMock(  # type: ignore[method-assign]
            side_effect=AcpProcessDied("ACP process pipe broken")
        )

        with pytest.raises(AcpProcessDied):
            await client._apply_startup_model()
