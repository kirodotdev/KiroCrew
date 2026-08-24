"""Tests for Feishu gateway boot (maybe_start_feishu) and approval resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.feishu.gateway import _resolve_approval_mode, maybe_start_feishu
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE


class _FakeState:
    def __init__(self) -> None:
        self.registered: list = []

    def register_channel_transport(self, transport) -> None:
        self.registered.append(transport)


def _orch(**overrides):
    base = dict(
        _feishu_enabled=True,
        _feishu_app_id="app-id-1",
        _feishu_app_secret="secret-1",
        _feishu_allowed_open_ids=["ou_abc123"],
        _feishu_allow_group=False,
        _feishu_allowed_group_ids=[],
        sessions=object(),
        ctx_builder=object(),
        conv_log=None,
        _approval_mode=None,
        _cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode="interactive")),
        dashboard_state=_FakeState(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestResolveApprovalMode:
    def test_yolo_resolves_auto(self) -> None:
        orch = _orch(_approval_mode="yolo")
        assert _resolve_approval_mode(orch) == APPROVAL_AUTO

    def test_explicit_auto_resolves_auto(self) -> None:
        orch = _orch(_approval_mode=APPROVAL_AUTO)
        assert _resolve_approval_mode(orch) == APPROVAL_AUTO

    def test_interactive_resolves_interactive(self) -> None:
        orch = _orch(_approval_mode="interactive")
        assert _resolve_approval_mode(orch) == APPROVAL_INTERACTIVE

    def test_none_falls_back_to_cfg(self) -> None:
        orch = _orch(
            _approval_mode=None,
            _cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode="interactive")),
        )
        assert _resolve_approval_mode(orch) == APPROVAL_INTERACTIVE

    def test_none_falls_back_to_cfg_auto(self) -> None:
        orch = _orch(
            _approval_mode=None,
            _cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode=APPROVAL_AUTO)),
        )
        assert _resolve_approval_mode(orch) == APPROVAL_AUTO

    def test_unknown_mode_resolves_interactive(self) -> None:
        orch = _orch(_approval_mode="something_else")
        assert _resolve_approval_mode(orch) == APPROVAL_INTERACTIVE


@patch("kiro_crew.feishu.gateway.LarkClient")
@patch("kiro_crew.feishu.gateway.FeishuTransport")
@patch("kiro_crew.feishu.gateway.FeishuDispatcher")
class TestMaybeStartFeishu:
    @pytest.mark.asyncio
    async def test_noop_when_disabled(self, mock_dispatcher, mock_transport, mock_client) -> None:
        orch = _orch(_feishu_enabled=False)
        result = await maybe_start_feishu(orch)
        assert result is None
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path(self, mock_dispatcher, mock_transport_cls, mock_client_cls) -> None:
        fake_client = MagicMock()
        mock_client_cls.return_value = fake_client

        fake_transport = MagicMock()
        fake_transport.connect = AsyncMock()
        mock_transport_cls.return_value = fake_transport

        fake_disp = MagicMock()
        mock_dispatcher.return_value = fake_disp

        orch = _orch()
        result = await maybe_start_feishu(orch)

        assert result is fake_client
        mock_client_cls.assert_called_once_with(app_id="app-id-1", app_secret="secret-1")
        fake_client.set_message_handler.assert_called_once_with(fake_transport.receive)
        assert fake_disp.client is fake_client
        fake_transport.connect.assert_awaited_once()
        assert orch.dashboard_state.registered == [fake_transport]

    @pytest.mark.asyncio
    async def test_dashboard_state_none_no_crash(
        self, mock_dispatcher, mock_transport_cls, mock_client_cls
    ) -> None:
        fake_client = MagicMock()
        mock_client_cls.return_value = fake_client

        fake_transport = MagicMock()
        fake_transport.connect = AsyncMock()
        mock_transport_cls.return_value = fake_transport

        fake_disp = MagicMock()
        mock_dispatcher.return_value = fake_disp

        orch = _orch(dashboard_state=None)
        result = await maybe_start_feishu(orch)

        assert result is fake_client
        # No crash — register_channel_transport simply not called.

    @pytest.mark.asyncio
    async def test_empty_allow_list_warns_but_starts(
        self, mock_dispatcher, mock_transport_cls, mock_client_cls, caplog
    ) -> None:
        fake_client = MagicMock()
        mock_client_cls.return_value = fake_client

        fake_transport = MagicMock()
        fake_transport.connect = AsyncMock()
        mock_transport_cls.return_value = fake_transport

        fake_disp = MagicMock()
        mock_dispatcher.return_value = fake_disp

        orch = _orch(_feishu_allowed_open_ids=[])
        with caplog.at_level("WARNING"):
            result = await maybe_start_feishu(orch)

        assert result is fake_client
        assert any("allowed_open_ids is empty" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_import_error_swallowed(
        self, mock_dispatcher, mock_transport_cls, mock_client_cls, caplog
    ) -> None:
        mock_client_cls.side_effect = ImportError("No module named 'lark_oapi'")

        fake_disp = MagicMock()
        mock_dispatcher.return_value = fake_disp

        orch = _orch()
        with caplog.at_level("ERROR"):
            result = await maybe_start_feishu(orch)

        assert result is None
        assert any("Failed to start Feishu" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_generic_exception_swallowed(
        self, mock_dispatcher, mock_transport_cls, mock_client_cls, caplog
    ) -> None:
        mock_client_cls.side_effect = RuntimeError("boom")

        fake_disp = MagicMock()
        mock_dispatcher.return_value = fake_disp

        orch = _orch()
        with caplog.at_level("ERROR"):
            result = await maybe_start_feishu(orch)

        assert result is None
        assert any("Failed to start Feishu" in r.message for r in caplog.records)
