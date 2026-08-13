"""Coverage for AcpProvider's cancel ladder, steer delegation and session cleanup.

Companion to ``test_acp_provider.py``, which owns this module's main behaviour; this file
only closes the coverage gaps left at its edges. New behaviour cases belong
in the sibling, not here.

``cancel`` is the abort path: every outcome it can return ("no_turn", "error",
"acked", "timeout") means something different to the caller, and collapsing two
of them either wedges a turn or reports a cancel that never landed. The session
file cleanup is the other side of the same coin — it must delete the transcript
pair, refuse a traversal, and survive an unlink failure.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KIRO,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
)
from kiro_crew.providers import acp as acp_mod
from kiro_crew.providers.acp import AcpProvider


def _provider(backend: str = ACP_BACKEND_KIRO) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


class TestCancel:
    @pytest.mark.asyncio
    async def test_idle_session_reports_no_turn(self) -> None:
        provider = _provider()
        provider._client.has_active_turn.return_value = False
        provider._client.cancel_session = AsyncMock()
        assert await provider.cancel() == "no_turn"
        provider._client.cancel_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transport_error_reports_error(self) -> None:
        provider = _provider()
        provider._client.has_active_turn.return_value = True
        provider._client.cancel_session = AsyncMock(side_effect=AcpError("channel down"))
        assert await provider.cancel(wait_ack_timeout=5.0) == "error"

    @pytest.mark.asyncio
    async def test_fire_and_forget_returns_acked_without_waiting(self) -> None:
        provider = _provider()
        provider._client.has_active_turn.return_value = True
        provider._client.cancel_session = AsyncMock()
        provider._client.wait_turn_done = AsyncMock()
        assert await provider.cancel() == "acked"
        provider._client.wait_turn_done.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ack_budget_is_passed_to_the_client(self) -> None:
        provider = _provider()
        provider._client.has_active_turn.return_value = True
        provider._client.cancel_session = AsyncMock()
        provider._client.wait_turn_done = AsyncMock(return_value=STOP_REASON_CANCELLED)
        assert await provider.cancel(wait_ack_timeout=12.5) == "acked"
        provider._client.cancel_session.assert_awaited_once_with(grace_secs=12.5)
        provider._client.wait_turn_done.assert_awaited_once_with(timeout=12.5)

    @pytest.mark.asyncio
    async def test_end_turn_also_counts_as_acked(self) -> None:
        provider = _provider()
        provider._client.has_active_turn.return_value = True
        provider._client.cancel_session = AsyncMock()
        provider._client.wait_turn_done = AsyncMock(return_value=STOP_REASON_END_TURN)
        assert await provider.cancel(wait_ack_timeout=1.0) == "acked"

    @pytest.mark.asyncio
    async def test_unexpected_stop_reason_is_a_timeout(self) -> None:
        provider = _provider()
        provider._client.has_active_turn.return_value = True
        provider._client.cancel_session = AsyncMock()
        provider._client.wait_turn_done = AsyncMock(return_value="refusal")
        assert await provider.cancel(wait_ack_timeout=1.0) == "timeout"

    @pytest.mark.asyncio
    async def test_missing_ack_is_a_timeout(self) -> None:
        provider = _provider()
        provider._client.has_active_turn.return_value = True
        provider._client.cancel_session = AsyncMock()
        provider._client.wait_turn_done = AsyncMock(side_effect=asyncio.TimeoutError)
        assert await provider.cancel(wait_ack_timeout=0.5) == "timeout"


class TestSteer:
    @pytest.mark.asyncio
    async def test_steer_delegates_to_the_client(self) -> None:
        provider = _provider()
        provider._client.steer = AsyncMock(return_value=True)
        assert await provider.steer("also check the logs") is True
        provider._client.steer.assert_awaited_once_with("also check the logs")

    @pytest.mark.asyncio
    async def test_unsteerable_client_returns_false(self) -> None:
        provider = _provider()
        provider._client.steer = AsyncMock(return_value=False)
        assert await provider.steer("late addition") is False

    def test_support_flag_follows_the_client(self) -> None:
        provider = _provider()
        provider._client.supports_steer = True
        assert provider.supports_steer is True
        provider._client.supports_steer = False
        assert provider.supports_steer is False

    def test_support_flag_defaults_false_without_the_attribute(self) -> None:
        provider = _provider()
        provider._client = object()
        assert provider.supports_steer is False


class TestSessionId:
    def test_session_id_comes_from_the_client(self) -> None:
        provider = _provider()
        provider._client._session_id = "sess-uuid-1"
        assert provider.session_id == "sess-uuid-1"

    def test_absent_session_id_is_empty(self) -> None:
        provider = _provider()
        provider._client._session_id = ""
        assert provider.session_id == ""

    def test_clientless_provider_has_no_session_id(self) -> None:
        provider = _provider()
        provider._client = None
        assert provider.session_id == ""


class TestCleanupSession:
    @pytest.mark.asyncio
    async def test_deletes_both_transcript_files(self, monkeypatch, tmp_path: Path) -> None:
        sessions = tmp_path / "cli"
        sessions.mkdir()
        (sessions / "sess-1.json").write_text("{}", encoding="utf-8")
        (sessions / "sess-1.jsonl").write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr(acp_mod, "kiro_sessions_dir", lambda: sessions)

        await _provider().cleanup_session("sess-1")

        assert list(sessions.iterdir()) == []

    @pytest.mark.asyncio
    async def test_missing_files_are_not_an_error(self, monkeypatch, tmp_path: Path) -> None:
        sessions = tmp_path / "cli"
        sessions.mkdir()
        monkeypatch.setattr(acp_mod, "kiro_sessions_dir", lambda: sessions)
        await _provider().cleanup_session("never-existed")

    @pytest.mark.asyncio
    async def test_blank_session_id_is_a_noop(self, monkeypatch) -> None:
        monkeypatch.setattr(
            acp_mod, "kiro_sessions_dir", lambda: pytest.fail("resolved a dir for a blank id")
        )
        await _provider().cleanup_session("")

    @pytest.mark.asyncio
    async def test_traversal_attempt_deletes_nothing(self, monkeypatch, tmp_path: Path) -> None:
        sessions = tmp_path / "cli"
        sessions.mkdir()
        outsider = tmp_path / "outside.json"
        outsider.write_text("keep me", encoding="utf-8")
        monkeypatch.setattr(acp_mod, "kiro_sessions_dir", lambda: sessions)

        await _provider().cleanup_session("../outside")

        assert outsider.exists()

    @pytest.mark.asyncio
    async def test_unlink_failure_is_swallowed(self, monkeypatch, tmp_path: Path) -> None:
        sessions = tmp_path / "cli"
        sessions.mkdir()
        (sessions / "sess-2.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(acp_mod, "kiro_sessions_dir", lambda: sessions)

        def _boom(*_a, **_k):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "unlink", _boom)
        await _provider().cleanup_session("sess-2")
        assert (sessions / "sess-2.json").exists()


class TestClearEffort:
    @pytest.mark.asyncio
    async def test_model_without_effort_support_is_a_noop(self, monkeypatch) -> None:
        provider = _provider()
        provider._client._model = "some-model"
        monkeypatch.setattr(acp_mod, "model_supports_effort", lambda _m: False)
        assert await provider.clear_effort() is False

    @pytest.mark.asyncio
    async def test_kiro_pushes_the_workspace_default_live(self, monkeypatch) -> None:
        provider = _provider()
        provider._client._model = "some-model"
        provider._client.send_command = AsyncMock()
        monkeypatch.setattr(acp_mod, "model_supports_effort", lambda _m: True)
        monkeypatch.setattr(provider, "_resolve_effort", lambda: "high")
        monkeypatch.setattr(provider, "_apply_effort_overlay", lambda: None)

        assert await provider.clear_effort() is True
        provider._client.send_command.assert_awaited_once_with("/effort", args={"level": "high"})

    @pytest.mark.asyncio
    async def test_claude_cannot_clear_live_and_asks_for_a_reset(self, monkeypatch) -> None:
        provider = _provider(ACP_BACKEND_CLAUDE)
        provider._client._model = "some-model"
        monkeypatch.setattr(acp_mod, "model_supports_effort", lambda _m: True)
        assert await provider.clear_effort() is False


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_delegates_to_the_client(self) -> None:
        provider = _provider()
        provider._client.shutdown = AsyncMock()
        await provider.shutdown()
        provider._client.shutdown.assert_awaited_once()


class TestBackendIdentityHelpers:
    def test_free_function_needs_an_acp_provider(self) -> None:
        assert acp_mod.is_claude_backend(object()) is False

    def test_free_function_reads_the_backend_string(self) -> None:
        assert acp_mod.is_claude_backend(_provider(ACP_BACKEND_CLAUDE)) is True
        assert acp_mod.is_claude_backend(_provider(ACP_BACKEND_KIRO)) is False
