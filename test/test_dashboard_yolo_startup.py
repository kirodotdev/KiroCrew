"""Tests that a declared auto-approve grant is standing (non-expiring)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kiro_crew.dashboard.server import _apply_startup_yolo
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.safety_override import SafetyOverride, reset_singleton, safety_override


def _make_state() -> DashboardState:
    return DashboardState(
        sessions=MagicMock(),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _cfg(yolo: bool, duration: str = "6h") -> SimpleNamespace:
    return SimpleNamespace(
        agent=SimpleNamespace(dangerously_skip_permissions=yolo, yolo_duration=duration)
    )


def setup_function() -> None:
    reset_singleton()


def teardown_function() -> None:
    reset_singleton()


def test_declared_yolo_does_not_expire() -> None:
    """The defect this replaces: it used to lapse after 24h and revert to Normal."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel"):
        _apply_startup_yolo(state, _cfg(yolo=True))

    so = safety_override()
    assert so.is_active() is True
    assert so._source == "config"
    assert so.is_permanent is True
    assert so.remaining_secs() == -1

    # Drive time past every deadline that used to end it.
    base = time.monotonic()
    with patch(
        "kiro_crew.safety_override.time.monotonic",
        return_value=base + SafetyOverride._MAX_TTL + 3600,
    ):
        assert so.is_active() is True, "declared YOLO must not expire"
        assert so.status().active is True


def test_declared_yolo_is_cleared_by_choosing_another_mode() -> None:
    """Permanence must never mean unrevokable."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel"):
        _apply_startup_yolo(state, _cfg(yolo=True))
        safety_override().deactivate("dashboard")

    assert safety_override().is_active() is False


def test_startup_seeds_the_adhoc_ttl_even_when_yolo_is_off() -> None:
    """A later Slack/dashboard grant must use the configured duration."""
    state = _make_state()
    cfg = MagicMock()
    cfg.agent.dangerously_skip_permissions = False
    cfg.agent.yolo_duration = "1h"
    with patch("kiro_crew.safety_override.sel"), patch(
        "kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg
    ):
        _apply_startup_yolo(state, _cfg(yolo=False, duration="1h"))

    assert safety_override().adhoc_ttl == 3600
    assert safety_override().is_active() is False


def test_slack_only_path_also_gets_a_standing_grant() -> None:
    """A headless --slack-only gateway never runs _apply_startup_yolo.

    It activates the declared grant via slack.handler.set_yolo_mode instead, so
    that path must grant permanence too — otherwise YOLO still dies for exactly
    the users driving the agent from another channel.
    """
    from kiro_crew.slack.handler import set_yolo_mode

    with patch("kiro_crew.safety_override.sel"):
        set_yolo_mode(True)

    so = safety_override()
    assert so._source == "config"
    assert so.is_permanent is True


def test_apply_startup_yolo_noop_when_config_false() -> None:
    """No declaration means no override at startup."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel"):
        _apply_startup_yolo(state, _cfg(yolo=False))

    assert safety_override().is_active() is False


def test_apply_startup_yolo_logs_sel() -> None:
    """Activation emits SEL audit event via safety_override module."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel") as mock_sel:
        _apply_startup_yolo(state, _cfg(yolo=True))

    mock_sel.return_value.log_api_access.assert_called()
    kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
    assert kwargs["operation"] == "safety_override:activate"
    assert kwargs["outcome"] == "enabled"
    assert "ttl:permanent" in kwargs["resources"]


def test_apply_startup_yolo_handles_exception_gracefully() -> None:
    """If the grant raises, startup continues without YOLO."""
    state = _make_state()
    with patch(
        "kiro_crew.dashboard.server.grant_declared_yolo",
        side_effect=RuntimeError("boom"),
    ) as mock_grant:
        _apply_startup_yolo(state, _cfg(yolo=True))

    mock_grant.assert_called_once()
    assert safety_override().is_active() is False


def test_apply_startup_yolo_refuses_when_sel_fails() -> None:
    """SEL audit failure must prevent activation (fail-closed)."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel") as mock_sel:
        mock_sel.return_value.log_api_access.side_effect = RuntimeError("sel down")
        _apply_startup_yolo(state, _cfg(yolo=True))
    assert safety_override().is_active() is False
