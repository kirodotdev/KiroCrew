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
