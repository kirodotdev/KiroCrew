"""opencode with no provider of the operator's own must not answer from its hosted model.

opencode 1.19.1 reports no error when no provider is set up: ``session/new`` puts the
session on ``opencode/big-pickle``, advertises only ``opencode/`` models, and the first
prompt goes to that hosted model. A bad key is the other half: the provider's 401
reaches Crew as ``-32603 Internal error: Incorrect API key provided.``. Both frames
below are copied from live runs (``run-opencode-signedout``, ``run-opencode-unauth``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import model_registry as mr
from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.harness import opencode as opencode_mod
from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.acp.harness.base import HarnessReadBack
from kiro_crew.acp.no_model_provider import AcpNoModelProvider
from kiro_crew.acp.transport_errors import _format_acp_error
from kiro_crew.acp.types import ACP_BACKEND_KIRO, ACP_BACKEND_OPENCODE
from kiro_crew.agent_sdk import host_auth

#: The ``model`` select of ``session/new`` on a host with no provider set up.
_SIGNED_OUT_MODELS = (
    "opencode/big-pickle",
    "opencode/cat-jam-free",
    "opencode/deepseek-v4.1-flash-free",
    "opencode/longcat-2.5-preview-free",
    "opencode/mimo-v2.6-flash-free",
    "opencode/muse-spark-1.3-contributor-free",
    "opencode/nemotron-3-ultra-free",
    "opencode/nemotron-3.5-lightning-free",
    "opencode/nemotron-3.5-super-vl-free",
    "opencode/space-bunny-free",
    "opencode/step-5-preview-free",
)

#: The ``session/prompt`` error when the configured provider refuses the key.
_BAD_KEY_ERROR = {
    "code": -32603,
    "data": {"errorName": "APIError", "service": "session"},
    "message": "Internal error: Incorrect API key provided.",
}


@pytest.fixture(autouse=True)
def _isolate_advertised_cache(_floor_monkeypatch):
    _floor_monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})


def _client(tmp_path, *, advertised, current, model="", config_model=""):
    client = AcpClient(work_dir=tmp_path, model=model, acp_backend=ACP_BACKEND_OPENCODE)
    client._session_id = "ses-1"
    client._available_models = [{"modelId": m, "name": m} for m in advertised]
    client._resolved_model_id = current
    client._process_adapter = SimpleNamespace(read_back=HarnessReadBack(config_model=config_model))
    client.set_config_option = AsyncMock()  # type: ignore[method-assign]
    return client


class TestTheHostedFallbackIsRefused:
    @pytest.mark.asyncio
    async def test_no_provider_refuses_before_any_prompt(self, tmp_path):
        client = _client(tmp_path, advertised=_SIGNED_OUT_MODELS, current="opencode/big-pickle")

        with pytest.raises(AcpNoModelProvider) as info:
            await client._apply_startup_model()

        text = str(info.value)
        assert "opencode/big-pickle" in text
        assert host_auth.signed_out_message(ACP_BACKEND_OPENCODE) in text
        assert info.value.transient is False
        assert info.value.user_worded is True
        # The model was never switched on the wire either.
        client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_refused_pin_that_leaves_the_hosted_default_is_refused(self, tmp_path):
        client = _client(
            tmp_path,
            advertised=_SIGNED_OUT_MODELS,
            current="opencode/big-pickle",
            model="opencode/space-bunny-free",
        )
        client._push_model_config_option = AsyncMock(return_value="")  # type: ignore[method-assign]

        with pytest.raises(AcpNoModelProvider):
            await client._apply_startup_model()


class TestAChosenModelIsLeftAlone:
    @pytest.mark.asyncio
    async def test_a_provider_of_the_operators_own_is_reachable(self, tmp_path):
        """opencode still lists its hosted models beside the operator's provider."""
        client = _client(
            tmp_path,
            advertised=(*_SIGNED_OUT_MODELS, "stub/stub-model"),
            current="opencode/big-pickle",
        )
        await client._apply_startup_model()
        # The session keeps the model ``session/new`` put it on.
        assert client._resolved_model_id == "opencode/big-pickle"

    @pytest.mark.asyncio
    async def test_the_harness_config_names_the_model(self, tmp_path):
        client = _client(
            tmp_path,
            advertised=_SIGNED_OUT_MODELS,
            current="opencode/big-pickle",
            config_model="opencode/big-pickle",
        )
        await client._apply_startup_model()
        assert client._resolved_model_id == "opencode/big-pickle"

    @pytest.mark.asyncio
    async def test_a_crew_pin_on_a_hosted_model_is_sent(self, tmp_path):
        client = _client(
            tmp_path,
            advertised=_SIGNED_OUT_MODELS,
            current="opencode/big-pickle",
            model="opencode/space-bunny-free",
        )
        client._push_model_config_option = AsyncMock(  # type: ignore[method-assign]
            return_value="opencode/space-bunny-free"
        )
        await client._apply_startup_model()
        assert client._model == "opencode/space-bunny-free"

    @pytest.mark.asyncio
    async def test_no_advertised_list_is_no_evidence(self, tmp_path):
        client = _client(tmp_path, advertised=(), current="opencode/big-pickle")
        await client._apply_startup_model()
        assert client._resolved_model_id == "opencode/big-pickle"


def test_the_refusal_is_not_retried() -> None:
    spawns: list[int] = []
    client = AcpClient(acp_backend=ACP_BACKEND_OPENCODE)
    client._process = None
    client._session_id = None
    client._cleanup_failed_live_spawn = AsyncMock()
    client._snapshot_process_tree = AsyncMock()

    def _reset():
        client._process = None
        client._session_id = None

    client._reset_state = _reset

    async def _spawn():
        spawns.append(1)
        client._process = MagicMock()
        client._process.returncode = None

    async def _init():
        raise AcpNoModelProvider("refused")

    client._spawn = _spawn
    client._initialize_session = _init
    with pytest.raises(AcpNoModelProvider):
        asyncio.run(client.ensure_ready())
    # One spawn: a fresh process reads the same missing provider.
    assert spawns == [1]


def test_the_harness_config_model_is_read_back(tmp_path, monkeypatch) -> None:
    class _Completed:
        returncode = 0
        stdout = 'banner\n{"permission": "ask", "model": "opencode/big-pickle"}'
        stderr = ""

    monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
    client._process_adapter = process_adapter_for(ACP_BACKEND_OPENCODE)
    issue, _remedy = opencode_mod._verify_opencode_routing(
        client, ACP_BACKEND_OPENCODE, ["opencode", "debug", "config"], "{}"
    )
    assert issue == ""
    assert client._harness_read_back().config_model == "opencode/big-pickle"


class TestABadKeyNamesTheFix:
    def test_opencode_shows_its_signed_out_message(self):
        text = _format_acp_error(_BAD_KEY_ERROR, backend=ACP_BACKEND_OPENCODE)
        assert text.startswith(host_auth.signed_out_message(ACP_BACKEND_OPENCODE))

    def test_the_phrase_does_not_classify_another_harness(self):
        text = _format_acp_error(_BAD_KEY_ERROR, backend=ACP_BACKEND_KIRO)
        assert host_auth.signed_out_message(ACP_BACKEND_OPENCODE) not in text
        assert "Incorrect API key provided" in text

    def test_the_raised_error_is_terminal(self):
        from kiro_crew.acp.transport_errors import _raise_acp_error

        with pytest.raises(AcpError) as info:
            _raise_acp_error(_BAD_KEY_ERROR, backend=ACP_BACKEND_OPENCODE)
        assert info.value.transient is False
