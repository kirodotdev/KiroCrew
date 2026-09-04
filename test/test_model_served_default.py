"""A session must not run on a backend default its partition does not serve.

``session/new`` chooses the model itself and reports it as ``currentModelId``.
Both the direct client and the pooled handle recorded that id as the session's
resolved model without ever checking it against the ``availableModels`` list the
same response carried. On a partition whose kiro-cli defaults to ``"auto"`` but
whose served list omits it, the session is born on an unusable model and the
first ``session/prompt`` dies with "your account does not have access to model
'auto'" — with nothing on the wire from Crew to blame.

These pin the shared predicate and both call sites: the answer is one served
model to switch to, or ``""`` meaning keep inheriting.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import DEFAULT_MODEL, AcpClient, pick_served_default
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_SET_MODEL,
)


class TestPickServedDefault:
    """The predicate, alone. Every "" answer is a distinct reason to do nothing."""

    def test_unknown_advertised_set_does_nothing(self):
        """Entitlement unknowable: an absent list is not "nothing is served"."""
        assert pick_served_default("auto", []) == ""
        assert pick_served_default("auto", None) == ""

    def test_no_echoed_default_does_nothing(self):
        """The backend named no model, so there is no evidence to act on."""
        assert pick_served_default("", ["glm-5"]) == ""
        assert pick_served_default("   ", ["glm-5"]) == ""

    def test_served_default_does_nothing(self):
        assert pick_served_default("glm-5", ["gpt-5.6-sol", "glm-5"]) == ""

    def test_served_only_under_the_peeled_spelling_does_nothing(self):
        """A ``<namespace>::`` qualifier on a model the backend fully serves.

        The shared fold answers the membership question, so a switch here would
        move a session off a model it can actually run.
        """
        assert pick_served_default("openrouter::glm-5", ["glm-5"]) == ""

    def test_unserved_default_prefers_auto_when_advertised(self):
        assert pick_served_default("glm-9", ["auto", "glm-5"]) == "auto"

    def test_unserved_default_takes_the_first_advertised_id(self):
        """No ``auto`` to fall back on: a served model beats an unusable session."""
        assert pick_served_default("auto", ["gpt-5.6-sol", "glm-5"]) == "gpt-5.6-sol"


def _kiro_client(advertised, resolved, model=""):
    client = AcpClient()
    client._session_id = "sess-1"
    client._model = model
    # ACP_BACKEND_KIRO is the AcpClient default; set it explicitly so the
    # backend under test is stated rather than inherited.
    client._acp_backend = ACP_BACKEND_KIRO
    client._resolved_model_id = resolved
    client._available_models = [{"modelId": m, "name": m} for m in advertised]
    return client


def _sent(sink):
    async def _send_request(method, params=None):
        sink.append((method, params or {}))
        return 1

    return _send_request


class TestClientInheritExits:
    """Both inherit exits of ``_apply_startup_model`` run the check."""

    @pytest.mark.asyncio
    async def test_unpinned_slot_switches_off_an_unserved_default(self):
        client = _kiro_client(["gpt-5.6-sol", "glm-5"], "auto")
        sent: list = []
        client._send_request = _sent(sent)

        await client._apply_startup_model()

        assert sent == [
            (METHOD_SET_MODEL, {"sessionId": "sess-1", "modelId": "gpt-5.6-sol"}),
        ]
        assert client._resolved_model_id == "gpt-5.6-sol"
        # The slot is still UNPINNED: "" means inherit to the settings seed and
        # the warm-pool re-apply, and correcting the wire must not rewrite that.
        assert client._model == ""

    @pytest.mark.asyncio
    async def test_advertised_auto_needs_no_switch(self):
        client = _kiro_client(["auto", "glm-5"], "auto")
        sent: list = []
        client._send_request = _sent(sent)

        await client._apply_startup_model()

        assert sent == []
        assert client._resolved_model_id == "auto"

    @pytest.mark.asyncio
    async def test_withheld_pin_still_lands_on_a_served_model(self):
        """The pin is withheld AND the default it falls back to is unserved."""
        client = _kiro_client(["gpt-5.6-sol"], "auto", model="claude-opus-4.8")
        sent: list = []
        client._send_request = _sent(sent)

        await client._apply_startup_model()

        assert sent == [
            (METHOD_SET_MODEL, {"sessionId": "sess-1", "modelId": "gpt-5.6-sol"}),
        ]
        assert client._resolved_model_id == "gpt-5.6-sol"
        # Withholding still records the pin as inherited, unchanged by this.
        assert client._model == DEFAULT_MODEL


def _handle(backend: str, advertised, resolved):
    runtime = MagicMock()
    runtime.acp_backend = backend
    runtime.send_request = AsyncMock(return_value=1)
    handle = AcpSessionHandle("sess-1", asyncio.Queue(), runtime)
    handle._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]
    handle._available_models = [{"modelId": m, "name": m} for m in advertised]
    handle._resolved_model_id = resolved
    return handle, runtime


class TestPooledHandle:
    @pytest.mark.asyncio
    async def test_switches_off_an_unserved_default(self):
        handle, runtime = _handle(ACP_BACKEND_KIRO, ["gpt-5.6-sol", "glm-5"], "auto")

        await handle.ensure_served_default()

        assert [c.args[0] for c in runtime.send_request.await_args_list] == [METHOD_SET_MODEL]
        assert runtime.send_request.await_args_list[0].args[1]["modelId"] == "gpt-5.6-sol"
        assert handle._resolved_model_id == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_switch_keeps_the_inherit_intent(self):
        """The wire moves; the session's intent does not. ``_model`` is what the
        warm-pool re-apply and the slot backfill read, so the served id must not
        become a pin there."""
        handle, _runtime = _handle(ACP_BACKEND_KIRO, ["gpt-5.6-sol", "glm-5"], "auto")
        before = handle._model

        await handle.ensure_served_default()

        assert handle._model == before
        assert handle.model == before
        assert handle._resolved_model_id == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_served_default_sends_nothing(self):
        handle, runtime = _handle(ACP_BACKEND_KIRO, ["auto", "glm-5"], "auto")

        await handle.ensure_served_default()

        assert runtime.send_request.await_args_list == []
        assert handle._resolved_model_id == "auto"

    @pytest.mark.asyncio
    async def test_other_backends_are_left_alone(self):
        """Only kiro's advertised ids are the ids its set-model verb accepts."""
        handle, runtime = _handle(ACP_BACKEND_KAS, ["gpt-5.6-sol"], "auto")

        await handle.ensure_served_default()

        assert runtime.send_request.await_args_list == []
