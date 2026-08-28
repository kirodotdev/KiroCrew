"""goose session mode always pins to approve."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import goose, tool_gate
from kiro_crew.acp.client import (
    AcpClient,
    AcpError,
    AcpToolGateUnroutable,
    SpecAdapterAcpClient,
)
from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.types import ACP_BACKEND_GOOSE, ACP_BACKEND_PI, METHOD_SET_MODE


class TestGooseModeHelpers:
    def test_auto_and_smart_approve_bypass_the_gate(self) -> None:
        assert goose.mode_bypasses_gate("auto")
        assert goose.mode_bypasses_gate("smart_approve")
        assert not goose.mode_bypasses_gate(goose.MODE_APPROVE)
        assert not goose.mode_bypasses_gate("chat")

    def test_missing_approve_on_an_advertised_list_is_an_issue(self) -> None:
        issue = goose.permission_mode_issue(["auto", "chat"], advertised=True)
        assert issue
        assert goose.MODE_APPROVE in issue

    def test_omitted_modes_are_not_an_issue(self) -> None:
        assert goose.permission_mode_issue([], advertised=False) == ""

    def test_approve_on_the_list_is_not_an_issue(self) -> None:
        assert goose.permission_mode_issue(["auto", goose.MODE_APPROVE], advertised=True) == ""


class TestGooseRoutingProbe:
    def test_doctor_reason_names_the_approve_pin(self, tmp_path: Path) -> None:
        verdict, reason = tool_gate.routing_verdict(ACP_BACKEND_GOOSE, tmp_path)
        assert verdict is Verdict.ROUTED
        assert "approve" in reason
        assert "auto" in reason

    def test_pi_reason_stays_structural(self, tmp_path: Path) -> None:
        verdict, reason = tool_gate.routing_verdict(ACP_BACKEND_PI, tmp_path)
        assert verdict is Verdict.ROUTED
        assert "session/request_permission" in reason or "asks per privileged tool" in reason


class TestGoosePermissionModePin:
    def _client(self, tmp_path: Path) -> AcpClient:
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_GOOSE)
        client._session_id = "sess-goose"
        client._session_key = "agent:main:main"
        client._send_request = AsyncMock(return_value=7)
        client._wait_for_response = AsyncMock(return_value={})
        return client

    @pytest.mark.asyncio
    async def test_pins_approve_when_session_starts_in_auto(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        client._available_mode_ids = ["auto", "approve", "smart_approve", "chat"]
        client._modes_advertised = True
        client._current_mode_id = "auto"

        await client._apply_goose_permission_mode()

        client._send_request.assert_awaited_once_with(
            METHOD_SET_MODE,
            {"sessionId": "sess-goose", "modeId": goose.MODE_APPROVE},
        )
        assert client._current_mode_id == goose.MODE_APPROVE

    @pytest.mark.asyncio
    async def test_skips_set_mode_when_already_approve(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        client._available_mode_ids = ["auto", "approve"]
        client._modes_advertised = True
        client._current_mode_id = goose.MODE_APPROVE

        await client._apply_goose_permission_mode()

        client._send_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refuses_when_approve_is_not_advertised(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        client._available_mode_ids = ["auto", "chat"]
        client._modes_advertised = True
        client._current_mode_id = "auto"

        with pytest.raises(AcpToolGateUnroutable, match="auto-approves"):
            await client._apply_goose_permission_mode()
        client._send_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refuses_when_set_mode_is_rejected(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        client._available_mode_ids = ["auto", "approve"]
        client._modes_advertised = True
        client._current_mode_id = "auto"
        client._wait_for_response = AsyncMock(side_effect=AcpError("mode rejected"))

        with pytest.raises(AcpToolGateUnroutable, match="rejected session/set_mode"):
            await client._apply_goose_permission_mode()

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_set_mode_auto_refuses_even_with_ungated_opt_out(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        client._allow_ungated_tools = True
        client._available_mode_ids = ["auto", "approve"]
        client._modes_advertised = True
        client._current_mode_id = goose.MODE_APPROVE

        with pytest.raises(AcpError, match="expiring safety override"):
            await client.set_goose_permission_mode(goose.MODE_AUTO)

        client._send_request.assert_not_awaited()
        assert client._current_mode_id == goose.MODE_APPROVE

    @pytest.mark.asyncio
    async def test_set_mode_auto_requires_the_named_ungated_opt_out(self, tmp_path: Path) -> None:
        """Auto cannot become a second way around Crew's security gate."""
        client = self._client(tmp_path)
        client._available_mode_ids = ["auto", "approve"]
        client._modes_advertised = True
        client._current_mode_id = goose.MODE_APPROVE

        with pytest.raises(AcpError, match="expiring safety override"):
            await client.set_goose_permission_mode(goose.MODE_AUTO)

        client._send_request.assert_not_awaited()
        assert client._current_mode_id == goose.MODE_APPROVE

    @pytest.mark.asyncio
    async def test_set_mode_auto_refuses_on_non_goose(self, tmp_path: Path) -> None:
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        client._session_id = "sess-pi"
        client._send_request = AsyncMock()

        with pytest.raises(AcpError, match="only valid on the goose harness"):
            await client.set_goose_permission_mode(goose.MODE_AUTO)
        client._send_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_initialize_sends_set_mode_approve_for_goose(self, tmp_path: Path) -> None:
        client = SpecAdapterAcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_GOOSE)
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        client._process = proc
        client._next_req_id = MagicMock(side_effect=range(1, 100))
        sent: list[str] = []

        async def capture(method: str, params: dict | None = None) -> int:
            sent.append(method)
            return len(sent)

        async def fake_wait(
            req_id: int,
            timeout: float = 50.0,
            *,
            method: str = "",
            expected_mcp: object = None,
        ) -> dict:
            if req_id == 1:
                return {"protocolVersion": 1, "agentCapabilities": {}}
            if req_id == 2:
                return {
                    "sessionId": "sess-g",
                    "modes": {
                        "currentModeId": "auto",
                        "availableModes": [
                            {"id": "auto"},
                            {"id": "approve"},
                        ],
                    },
                }
            return {}

        client._send_request = AsyncMock(side_effect=capture)
        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()
        client._apply_startup_model = AsyncMock()

        await client._initialize_session()

        assert METHOD_SET_MODE in sent
        assert client._current_mode_id == goose.MODE_APPROVE
        assert client._session_id == "sess-g"


class TestGoosePermissionModeApi:
    def _state(self, client: object | None) -> tuple[object, object]:
        from kiro_crew.dashboard.state import DashboardState

        state = DashboardState.__new__(DashboardState)
        slot = MagicMock(key="s1")
        slot._trust = False
        slot._trust_reads = False
        state._slots = {"s1": slot}
        state._live_slot_acp_client = lambda _slot: client  # type: ignore[method-assign]
        state.sessions = MagicMock()
        return state, slot

    @pytest.mark.asyncio
    async def test_enable_refuses_without_an_expiring_safety_grant(self) -> None:
        from kiro_crew.dashboard.chat_handlers import _enable_goose_permission_auto

        client = MagicMock()
        client.backend = ACP_BACKEND_GOOSE
        client._available_mode_ids = ["auto", "approve"]
        client._allow_ungated_tools = True
        client.set_goose_permission_mode = AsyncMock()
        state, _slot = self._state(client)

        refused = await _enable_goose_permission_auto(state, "s1")

        assert refused is not None
        assert refused.status == 409
        assert "permission_auto_requires_safety_override" in refused.text
        client.set_goose_permission_mode.assert_not_awaited()
        state.sessions.set_approval_policy.assert_not_called()

    @pytest.mark.asyncio
    async def test_enable_refuses_even_with_ungated_opt_out_off(self) -> None:
        from kiro_crew.dashboard.chat_handlers import _enable_goose_permission_auto

        client = MagicMock()
        client.backend = ACP_BACKEND_GOOSE
        client._available_mode_ids = ["auto", "approve"]
        client._allow_ungated_tools = False
        client.set_goose_permission_mode = AsyncMock()
        state, _slot = self._state(client)

        refused = await _enable_goose_permission_auto(state, "s1")

        assert refused is not None
        assert refused.status == 409
        assert "permission_auto_requires_safety_override" in refused.text
        client.set_goose_permission_mode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enable_refuses_on_non_goose(self) -> None:
        from kiro_crew.dashboard.chat_handlers import _enable_goose_permission_auto

        client = MagicMock()
        client.backend = ACP_BACKEND_PI
        client._available_mode_ids = ["auto"]
        client.set_goose_permission_mode = AsyncMock()
        state, _slot = self._state(client)

        refused = await _enable_goose_permission_auto(state, "s1")

        assert refused is not None
        assert refused.status == 409
        assert "permission_auto_requires_safety_override" in refused.text
        client.set_goose_permission_mode.assert_not_awaited()
