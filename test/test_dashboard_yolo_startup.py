"""Tests that ``agent.yolo=true`` enables time-limited safety override at startup."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kiro_crew.dashboard.server import _apply_startup_yolo
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.safety_override import reset_singleton, safety_override


def _make_state() -> DashboardState:
    return DashboardState(
        sessions=MagicMock(),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _cfg(yolo: bool) -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(yolo=yolo))


def setup_function() -> None:
    reset_singleton()


def teardown_function() -> None:
    reset_singleton()


def test_apply_startup_yolo_enables_with_24h_ttl() -> None:
    """agent.yolo=true activates safety override with 24h cap."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel"):
        _apply_startup_yolo(state, _cfg(yolo=True))

    so = safety_override()
    assert so.is_active() is True
    assert so._source == "config"
    assert so.remaining_secs() > 86000


def test_apply_startup_yolo_noop_when_config_false() -> None:
    """agent.yolo=false does not activate override."""
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


def test_apply_startup_yolo_handles_exception_gracefully() -> None:
    """If safety_override().activate() raises, startup continues without YOLO."""
    state = _make_state()
    with patch("kiro_crew.dashboard.server.safety_override") as mock_so:
        mock_so.return_value.activate.side_effect = RuntimeError("boom")
        _apply_startup_yolo(state, _cfg(yolo=True))

    # Should not have activated (exception was caught)
    mock_so.return_value.activate.assert_called_once()


def test_apply_startup_yolo_refuses_when_sel_fails() -> None:
    """SEL audit failure must prevent activation (fail-closed)."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel") as mock_sel:
        mock_sel.return_value.log_api_access.side_effect = RuntimeError("sel down")
        _apply_startup_yolo(state, _cfg(yolo=True))
    assert safety_override().is_active() is False
