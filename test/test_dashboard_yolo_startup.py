"""Tests that a declared auto-approve grant is standing (non-expiring).

The declaration is the operator-owned keystone ``standing-approval/grant.json``, not a
key in ``config.json``: a standing skip of every approval must not be declarable by the
population it governs, and ``config.json`` stays agent-READABLE by design, which leaves
the inode behind its read-only seal reachable under a second name. So every test here
that means "the operator declared it" writes the keystone with :func:`_declare`, and
``agent.dangerously_skip_permissions`` appears only where the subject is that the
retired key grants nothing.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import standing_approval
from kiro_crew.config.loader import standing_approval_path
from kiro_crew.dashboard.server import _apply_startup_yolo
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.safety_override import SafetyOverride, reset_singleton, safety_override


@pytest.fixture(autouse=True)
def _host_that_masks_the_keystone(monkeypatch):
    """Pin the host as one whose sandbox masks the keystone.

    The subject here is the startup path's reading of the declaration, not the mask that
    makes the declaration trustworthy, and a machine with no sandbox backend refuses the
    grant before the reading is reached. ``test_standing_approval_keystone.py`` owns the
    cases about the mask itself.
    """
    monkeypatch.setattr(standing_approval, "_keystone_is_masked", lambda *_a, **_k: True)


def _make_state() -> DashboardState:
    return DashboardState(
        sessions=MagicMock(),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _cfg(yolo: bool, duration: str = "6h") -> SimpleNamespace:
    """A config whose ``dangerously_skip_permissions`` is the RETIRED key.

    ``yolo=True`` here means "the operator left the retired key set", which is a
    migration case and never a grant. To grant, call :func:`_declare`.
    """
    return SimpleNamespace(
        agent=SimpleNamespace(
            dangerously_skip_permissions=yolo, yolo_duration=duration, sandbox="auto"
        )
    )


def _declare() -> None:
    """Write the operator's standing declaration to the keystone.

    The suite gives each test an isolated ``KIROCREW_HOME``, so this writes into that
    scratch data home rather than the operator's real one.
    """
    path = standing_approval_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dangerously_skip_permissions": True}), encoding="utf-8")


def setup_function() -> None:
    reset_singleton()


def teardown_function() -> None:
    reset_singleton()


def test_declared_yolo_does_not_expire() -> None:
    """Declared YOLO does not lapse after 24h and revert to Normal."""
    state = _make_state()
    _declare()
    with patch("kiro_crew.safety_override.sel"):
        _apply_startup_yolo(state, _cfg(yolo=False))

    so = safety_override()
    assert so.is_active() is True
    assert so._source == "config"
    assert so.is_permanent is True
    assert so.remaining_secs() == -1

    # Drive time past every deadline that would end it.
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
    _declare()
    with patch("kiro_crew.safety_override.sel"):
        _apply_startup_yolo(state, _cfg(yolo=False))
        # Guard against a vacuous pass: the grant must be live before it is cleared,
        # or the assertion below would hold for a startup that granted nothing.
        assert safety_override().is_active() is True
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


def test_apply_startup_yolo_noop_when_nothing_is_declared() -> None:
    """No keystone means no override at startup."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel"):
        _apply_startup_yolo(state, _cfg(yolo=False))

    assert safety_override().is_active() is False


def test_the_retired_config_key_alone_grants_nothing(caplog) -> None:
    """The migration is explicit: the retired key is announced, never honoured.

    Control beside it: the very same startup call grants once the keystone is written,
    so the refusal above comes from the declaration's ABSENCE and not from the harness.
    """
    state = _make_state()
    with patch("kiro_crew.safety_override.sel"), caplog.at_level("WARNING"):
        _apply_startup_yolo(state, _cfg(yolo=True))

    assert safety_override().is_active() is False
    assert "no longer grants" in caplog.text

    reset_singleton()
    _declare()
    with patch("kiro_crew.safety_override.sel"):
        _apply_startup_yolo(state, _cfg(yolo=True))
    assert safety_override().is_active() is True


def test_apply_startup_yolo_logs_sel() -> None:
    """Activation emits SEL audit event via safety_override module."""
    state = _make_state()
    _declare()
    with patch("kiro_crew.safety_override.sel") as mock_sel:
        _apply_startup_yolo(state, _cfg(yolo=False))

    mock_sel.return_value.log_api_access.assert_called()
    kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
    assert kwargs["operation"] == "safety_override:activate"
    assert kwargs["outcome"] == "enabled"
    assert "ttl:permanent" in kwargs["resources"]


def test_apply_startup_yolo_handles_exception_gracefully() -> None:
    """If the grant raises, startup continues without YOLO."""
    state = _make_state()
    _declare()
    with patch(
        "kiro_crew.dashboard.server.grant_declared_yolo",
        side_effect=RuntimeError("boom"),
    ) as mock_grant:
        _apply_startup_yolo(state, _cfg(yolo=False))

    mock_grant.assert_called_once()
    assert safety_override().is_active() is False


def test_apply_startup_yolo_refuses_when_sel_fails() -> None:
    """SEL audit failure must prevent activation (fail-closed).

    The keystone IS declared here, so the inactive result below is the SEL refusal
    rather than a startup that never attempted a grant.
    """
    state = _make_state()
    _declare()
    with patch("kiro_crew.safety_override.sel") as mock_sel:
        mock_sel.return_value.log_api_access.side_effect = RuntimeError("sel down")
        _apply_startup_yolo(state, _cfg(yolo=False))
    assert safety_override().is_active() is False
