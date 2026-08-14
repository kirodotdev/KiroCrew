"""Runtime wiring for KAS's auth callback.

KAS sends ``_kiro/auth/getAccessToken`` as a connection-level request (no
sessionId), so the runtime answers it directly. These tests pin: a resolved
token is sent straight back; every failure becomes a JSON-RPC error (KAS never
hangs); an UNEXPECTED error's text never reaches the wire; and a non-KAS backend
refuses outright.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.kas_assets import build_kas_argv
from kiro_crew.acp.kas_auth import KasAuthCallbackError
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

_FAKE_TOKEN = "aoaAbc123." + "X" * 200
_RESULT = {"accessToken": _FAKE_TOKEN, "expiresAt": "2026-08-14T00:12:59Z"}


def _fake_runtime(backend: str = ACP_BACKEND_KAS) -> MagicMock:
    """A stand-in carrying only what _answer_get_access_token touches."""
    fake = MagicMock()
    fake._acp_backend = backend
    fake.send_response = AsyncMock()
    fake.send_error = AsyncMock()
    return fake


class TestAnswerGetAccessToken:
    @pytest.mark.asyncio
    async def test_answers_with_the_resolved_token(self, monkeypatch):
        monkeypatch.setattr(
            runtime_mod, "resolve_kas_access_token", AsyncMock(return_value=_RESULT)
        )
        fake = _fake_runtime()
        await AcpRuntime._deliver_kas_access_token(fake, 7)
        fake.send_response.assert_awaited_once_with(7, _RESULT)
        fake.send_error.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_callback_error_is_returned_as_reason(self, monkeypatch):
        monkeypatch.setattr(
            runtime_mod,
            "resolve_kas_access_token",
            AsyncMock(side_effect=KasAuthCallbackError("not logged in")),
        )
        fake = _fake_runtime()
        await AcpRuntime._deliver_kas_access_token(fake, 7)
        fake.send_response.assert_not_awaited()
        fake.send_error.assert_awaited_once()
        args = fake.send_error.await_args.args
        assert args[0] == 7
        assert "not logged in" in args[2]

    @pytest.mark.asyncio
    async def test_unexpected_error_message_is_generic(self, monkeypatch):
        # An unexpected exception could carry token bytes in its text; the wire
        # error must be generic, never str(exc).
        monkeypatch.setattr(
            runtime_mod,
            "resolve_kas_access_token",
            AsyncMock(side_effect=ValueError(_FAKE_TOKEN)),
        )
        fake = _fake_runtime()
        await AcpRuntime._deliver_kas_access_token(fake, 7)
        fake.send_response.assert_not_awaited()
        fake.send_error.assert_awaited_once()
        msg = fake.send_error.await_args.args[2]
        assert _FAKE_TOKEN not in msg
        assert msg == "auth callback failed"

    @pytest.mark.asyncio
    async def test_kas_backend_dispatches_to_delivery(self):
        # Positive backend dispatch (harness-parity H5): KAS → deliver.
        fake = _fake_runtime()
        fake._deliver_kas_access_token = AsyncMock()
        await AcpRuntime._answer_get_access_token(fake, 7)
        fake._deliver_kas_access_token.assert_awaited_once_with(7)
        fake.send_error.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_kas_backend_refuses_without_delivering(self):
        fake = _fake_runtime(backend=ACP_BACKEND_KIRO)
        fake._deliver_kas_access_token = AsyncMock()
        await AcpRuntime._answer_get_access_token(fake, 7)
        fake._deliver_kas_access_token.assert_not_awaited()
        fake.send_response.assert_not_awaited()
        fake.send_error.assert_awaited_once()


class TestLaunchFlag:
    def test_kas_argv_requests_acp_callback(self, tmp_path):
        node = tmp_path / "node"
        script = tmp_path / "acp-server.js"
        argv = build_kas_argv(node, script)
        assert "--auth=acp-callback" in argv
