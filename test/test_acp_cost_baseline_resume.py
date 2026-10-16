"""The per-turn cost baseline across a process restart and a session restore.

The claude harness restores a resumed session's cost ledger from its own
transcript, so after ``session/load`` its session-cumulative ``cost`` carries
the session's whole lifetime spend. These tests pin that the first reading after
a restore is never billed as new spend: a baseline measured on the same session
is kept across an adapter-process restart, and a client that has no baseline for
the restored session adopts the first reading instead of billing it.
"""

import logging
import math
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import AcpPromptStats

# ── AcpPromptStats: begin / restore / apply ──────────────────────────────


class TestBaselineRules:
    def test_begin_starts_a_known_zero_baseline(self):
        stats = AcpPromptStats(cost_session_usd=4.0, cost_baseline_unknown=True)
        stats.begin_cost_session("s1")
        stats.apply_cost_cumulative(0.02)
        assert math.isclose(stats.cost_usd, 0.02)

    def test_restore_of_the_same_session_keeps_the_baseline(self):
        stats = AcpPromptStats()
        stats.begin_cost_session("s1")
        stats.apply_cost_cumulative(5.00)
        stats = stats.carry_over()
        stats.restore_cost_session("s1")
        stats.apply_cost_cumulative(5.03)
        assert math.isclose(stats.cost_usd, 0.03)

    def test_restore_without_a_baseline_adopts_the_first_reading(self, caplog):
        stats = AcpPromptStats()
        stats.restore_cost_session("s1")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.types"):
            stats.apply_cost_cumulative(5.00)
        assert stats.cost_usd == 0.0
        assert math.isclose(stats.cost_session_usd, 5.00)
        assert "restored session s1" in caplog.text
        stats.apply_cost_cumulative(5.04)
        assert math.isclose(stats.cost_usd, 0.04)

    def test_restore_of_another_session_does_not_reuse_the_baseline(self):
        stats = AcpPromptStats()
        stats.begin_cost_session("s1")
        stats.apply_cost_cumulative(1.00)
        stats = stats.carry_over()
        stats.restore_cost_session("s2")
        stats.apply_cost_cumulative(7.00)
        assert stats.cost_usd == 0.0

    def test_unknown_survives_the_turn_boundary(self):
        stats = AcpPromptStats()
        stats.restore_cost_session("s1")
        stats = stats.carry_over()
        stats.apply_cost_cumulative(5.00)
        assert stats.cost_usd == 0.0

    def test_a_repeated_restore_before_any_reading_stays_unknown(self):
        stats = AcpPromptStats()
        stats.restore_cost_session("s1")
        stats.restore_cost_session("s1")
        stats.apply_cost_cumulative(5.00)
        assert stats.cost_usd == 0.0


# ── AcpClient: the real restart and restore sequence ─────────────────────


class TestClientRestartSequence:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _isolate_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def _client(self, tmp_path) -> AcpClient:
        client = AcpClient(work_dir=tmp_path)
        self._attach_process(client)
        return client

    @staticmethod
    def _attach_process(client: AcpClient) -> None:
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        client._process = proc
        client._next_req_id = MagicMock(side_effect=range(1, 100))
        client._drain_notifications = AsyncMock()

    @staticmethod
    def _answer(client: AcpClient, second: dict) -> None:
        responses = {
            1: {"protocolVersion": "2025-08-22", "agentCapabilities": {"loadSession": True}},
            2: second,
        }

        async def fake_wait(req_id, timeout=50.0, *, method="", expected_mcp=None):
            return responses.get(req_id, {})

        client._wait_for_response = AsyncMock(side_effect=fake_wait)

    @staticmethod
    def _transcript(sid: str) -> None:
        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / f"{sid}.json").write_text("{}")

    @staticmethod
    def _cost(client: AcpClient, amount: float) -> None:
        client._track_usage_update(
            MagicMock(
                params={
                    "update": {
                        "sessionUpdate": "usage_update",
                        "used": 10,
                        "size": 100,
                        "cost": {"amount": amount, "currency": "USD"},
                    }
                }
            )
        )

    async def _restart_process(self, client: AcpClient) -> None:
        with patch("kiro_crew.session._untrack_pid"):
            client._reset_state()
        self._attach_process(client)

    @pytest.mark.asyncio
    async def test_adapter_restart_then_resume_bills_only_the_new_turn(self, tmp_path):
        client = self._client(tmp_path)
        self._answer(client, {"sessionId": "s1"})
        await client._initialize_session()
        self._cost(client, 5.00)
        client.last_prompt_stats = client.last_prompt_stats.carry_over()

        await self._restart_process(client)
        self._transcript("s1")
        client._resume_session_id = "s1"
        self._answer(client, {"modes": ["chat"]})
        await client._initialize_session()
        assert client._resumed is True

        # The restored ledger reports lifetime spend plus this turn's.
        self._cost(client, 5.03)
        assert client.last_prompt_stats.cost_usd == pytest.approx(0.03)

    @pytest.mark.asyncio
    async def test_fresh_client_resume_never_bills_the_lifetime(self, tmp_path):
        client = self._client(tmp_path)
        self._transcript("s1")
        client._resume_session_id = "s1"
        self._answer(client, {"modes": ["chat"]})
        await client._initialize_session()
        assert client._resumed is True

        self._cost(client, 5.00)
        assert client.last_prompt_stats.cost_usd == 0.0
        client.last_prompt_stats = client.last_prompt_stats.carry_over()
        self._cost(client, 5.04)
        assert client.last_prompt_stats.cost_usd == pytest.approx(0.04)

    @pytest.mark.asyncio
    async def test_restart_onto_a_new_session_bills_from_zero(self, tmp_path):
        client = self._client(tmp_path)
        self._answer(client, {"sessionId": "s1"})
        await client._initialize_session()
        self._cost(client, 5.00)
        client.last_prompt_stats = client.last_prompt_stats.carry_over()

        await self._restart_process(client)
        self._answer(client, {"sessionId": "s2"})
        await client._initialize_session()
        assert client._resumed is False

        self._cost(client, 0.02)
        assert client.last_prompt_stats.cost_usd == pytest.approx(0.02)
