"""Tests for Slack dispatch approval-mode resolution (Mesh-2257).

The two Slack dispatch sites previously hardcoded APPROVAL_INTERACTIVE,
ignoring the resolved approval mode that dashboard/subagent paths honor.
"""

from __future__ import annotations

from types import SimpleNamespace

from kiro_crew.slack.events import _resolve_approval_mode
from kiro_crew.slack.handler import APPROVAL_AUTO, APPROVAL_INTERACTIVE


def _orch(flag: str | None, config_mode: str) -> SimpleNamespace:
    """Minimal orch stub exposing _approval_mode (CLI flag) and _cfg.agent.approval_mode."""
    return SimpleNamespace(
        _approval_mode=flag,
        _cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode=config_mode)),
    )


class TestResolveApprovalMode:
    def test_config_auto_no_flag_resolves_auto(self) -> None:
        # The core bug: config 'auto' was ignored for Slack; now honored.
        assert _resolve_approval_mode(_orch(None, "auto")) == APPROVAL_AUTO

    def test_config_interactive_no_flag_resolves_interactive(self) -> None:
        assert _resolve_approval_mode(_orch(None, "interactive")) == APPROVAL_INTERACTIVE

    def test_cli_flag_auto_overrides_config_interactive(self) -> None:
        assert _resolve_approval_mode(_orch("auto", "interactive")) == APPROVAL_AUTO

    def test_cli_flag_interactive_overrides_config_auto(self) -> None:
        assert _resolve_approval_mode(_orch("interactive", "auto")) == APPROVAL_INTERACTIVE

    def test_reads_flag_normalizes_to_interactive(self) -> None:
        # 'reads' is gated at the gateway approval-event layer, not handle_message.
        assert _resolve_approval_mode(_orch("reads", "interactive")) == APPROVAL_INTERACTIVE

    def test_yolo_flag_normalizes_to_interactive(self) -> None:
        # 'yolo' is handled by the global YOLO toggle, not handle_message.
        assert _resolve_approval_mode(_orch("yolo", "interactive")) == APPROVAL_INTERACTIVE

    def test_runtime_yolo_active_resolves_auto(self, monkeypatch) -> None:
        # Runtime /meshclaw yolo (safety_override) must auto-approve on BOTH
        # native and transport paths — folded in at this chokepoint.
        monkeypatch.setattr("kiro_crew.slack.events.is_yolo_mode", lambda: True)
        assert _resolve_approval_mode(_orch(None, "interactive")) == APPROVAL_AUTO
        assert _resolve_approval_mode(_orch("interactive", "interactive")) == APPROVAL_AUTO

    def test_runtime_yolo_inactive_keeps_config(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.slack.events.is_yolo_mode", lambda: False)
        assert _resolve_approval_mode(_orch(None, "interactive")) == APPROVAL_INTERACTIVE
        assert _resolve_approval_mode(_orch(None, "auto")) == APPROVAL_AUTO
