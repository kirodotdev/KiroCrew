"""The model is set by a different VERB on each backend.

kiro-cli takes ``session/set_model``. KAS implements no such method — the model is
one of its session config options — so a session on KAS would error on every
model assignment if the kiro verb were reused. The bookkeeping either verb
triggers (resolved id, context-window rebase) is identical and stays shared.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODEL,
    MODEL_CONFIG_ID,
)

_MODEL = "claude-sonnet-4.5"


def _handle(backend: str) -> tuple[AcpSessionHandle, MagicMock]:
    runtime = MagicMock()
    runtime.acp_backend = backend
    # send_request returns a request id the caller may then await a response for.
    runtime.send_request = AsyncMock(return_value=1)
    handle = AcpSessionHandle("sess-1", asyncio.Queue(), runtime)
    # Response plumbing is not what these tests are about; the wire call is.
    handle._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]
    # The gate in set_model only sends a model the session advertised, so the
    # advertisement has to exist for either verb to be reached at all.
    handle._available_models = [{"modelId": _MODEL}]
    return handle, runtime


def _sent_methods(runtime: MagicMock) -> list[str]:
    return [call.args[0] for call in runtime.send_request.await_args_list]


class TestModelVerbPerBackend:
    @pytest.mark.asyncio
    async def test_kas_uses_a_session_config_option(self):
        handle, runtime = _handle(ACP_BACKEND_KAS)
        await handle.set_model(_MODEL)
        methods = _sent_methods(runtime)
        assert METHOD_SET_CONFIG_OPTION in methods
        # Reusing the kiro verb here is the defect this pins: KAS has no handler
        # for it, so the assignment would fail on every switch.
        assert METHOD_SET_MODEL not in methods

    @pytest.mark.asyncio
    async def test_kas_names_the_model_config_id(self):
        handle, runtime = _handle(ACP_BACKEND_KAS)
        await handle.set_model(_MODEL)
        params = next(
            c.args[1]
            for c in runtime.send_request.await_args_list
            if c.args[0] == METHOD_SET_CONFIG_OPTION
        )
        assert params["configId"] == MODEL_CONFIG_ID
        assert params["value"] == _MODEL
        assert params["sessionId"] == "sess-1"

    @pytest.mark.asyncio
    async def test_kiro_still_uses_set_model(self):
        handle, runtime = _handle(ACP_BACKEND_KIRO)
        await handle.set_model(_MODEL)
        methods = _sent_methods(runtime)
        assert METHOD_SET_MODEL in methods
        assert METHOD_SET_CONFIG_OPTION not in methods


class TestSharedBookkeeping:
    """Whichever verb carried it, the handle's own state must agree.

    The context meter converts percentages against the resolved model's window,
    so a backend that updated the wire but not this state would report usage
    against the previous model.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ACP_BACKEND_KIRO])
    async def test_resolved_model_is_recorded(self, backend):
        handle, _ = _handle(backend)
        await handle.set_model(_MODEL)
        assert handle._model == _MODEL
        assert handle._resolved_model_id == _MODEL

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ACP_BACKEND_KIRO])
    async def test_an_unserved_model_sends_nothing_on_either_backend(self, backend):
        """The advertised-model gate is backend-independent.

        It can only run after ``session/new`` returns the served list, which is
        why the model is not carried in the session/new payload instead.
        """
        handle, runtime = _handle(backend)
        await handle.set_model("some-model-nobody-serves")
        assert runtime.send_request.await_args_list == []
        # The session stays on its backend default and records no refusal.
        assert handle._model == ""
        assert handle._resolved_model_id == ""
        assert handle.model_pin_refused == ""


class TestKasModelSpelling:
    """A pin stored as another backend's model id reaches KAS in its own spelling.

    The claude_code id for Opus 5.5 is ``global.anthropic.claude-opus-5-5[1m]``;
    KAS takes ``claude-opus-5.5``. KAS accepts the claude_code id on its
    ``model`` option and then fails the turn, so the id has to be changed
    before it is sent.
    """

    _BEDROCK = "global.anthropic.claude-opus-5-5[1m]"

    @staticmethod
    def _sent_value(runtime: MagicMock) -> str:
        return next(
            c.args[1]["value"]
            for c in runtime.send_request.await_args_list
            if c.args[0] == METHOD_SET_CONFIG_OPTION
        )

    @pytest.mark.asyncio
    async def test_claude_code_id_is_sent_as_the_kiro_id_with_no_model_list(self):
        handle, runtime = _handle(ACP_BACKEND_KAS)
        handle._available_models = []
        await handle.set_model(self._BEDROCK)
        assert self._sent_value(runtime) == "claude-opus-5.5"
        assert handle._model == "claude-opus-5.5"
        assert handle._resolved_model_id == "claude-opus-5.5"

    @pytest.mark.asyncio
    async def test_a_family_alias_is_sent_unchanged(self):
        handle, runtime = _handle(ACP_BACKEND_KAS)
        handle._available_models = []
        await handle.set_model("opus")
        assert self._sent_value(runtime) == "opus"

    @pytest.mark.asyncio
    async def test_claude_code_id_lands_on_an_advertised_kiro_id(self):
        handle, runtime = _handle(ACP_BACKEND_KAS)
        handle._available_models = [{"modelId": "claude-opus-5.5"}]
        await handle.set_model(self._BEDROCK)
        assert self._sent_value(runtime) == "claude-opus-5.5"

    @pytest.mark.asyncio
    async def test_an_advertised_id_is_sent_unchanged(self):
        handle, runtime = _handle(ACP_BACKEND_KAS)
        handle._available_models = [{"modelId": self._BEDROCK}]
        await handle.set_model(self._BEDROCK)
        assert self._sent_value(runtime) == self._BEDROCK

    @pytest.mark.asyncio
    async def test_an_unknown_id_is_sent_unchanged(self):
        handle, runtime = _handle(ACP_BACKEND_KAS)
        handle._available_models = []
        await handle.set_model("us.anthropic.some-future-model")
        assert self._sent_value(runtime) == "us.anthropic.some-future-model"

    @pytest.mark.asyncio
    async def test_kiro_is_not_changed(self):
        handle, runtime = _handle(ACP_BACKEND_KIRO)
        handle._available_models = []
        await handle.set_model(self._BEDROCK)
        params = next(
            c.args[1] for c in runtime.send_request.await_args_list if c.args[0] == METHOD_SET_MODEL
        )
        assert params["modelId"] == self._BEDROCK
