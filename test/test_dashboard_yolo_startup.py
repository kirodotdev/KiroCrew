"""Tests that a declared auto-approve grant is standing (non-expiring)."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.server import _apply_startup_yolo
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.hooks import TOOL_DENY, HookManager
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
    """Declared YOLO does not lapse after 24h and revert to Normal."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel"):
        _apply_startup_yolo(state, _cfg(yolo=True))

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


def _launch(approval_mode: str | None) -> DashboardState:
    """A dashboard state started the way ``gateway --approval <approval_mode>`` starts one."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel"):
        _apply_startup_yolo(state, _cfg(yolo=False), approval_mode=approval_mode)
    return state


def test_gateway_approval_yolo_arms_a_cli_grant() -> None:
    """``gateway --approval yolo`` auto-approves dashboard chats for the launch, as source ``cli``."""
    state = _launch("yolo")

    assert state.is_yolo_active() is True
    assert safety_override().is_cli_launch is True
    assert safety_override().is_permanent is True


def test_gateway_approval_yolo_leaves_slack_asking() -> None:
    """A Slack tool call under ``--approval yolo`` still asks, as before the flag armed a grant."""
    from kiro_crew.slack.events_queue import _resolve_approval_mode
    from kiro_crew.slack.handler import APPROVAL_INTERACTIVE, is_yolo_mode

    _launch("yolo")
    orch = SimpleNamespace(_approval_mode="yolo", _cfg=_cfg(yolo=False))

    assert is_yolo_mode() is False
    assert _resolve_approval_mode(orch) == APPROVAL_INTERACTIVE


def test_a_slack_yolo_grant_still_reaches_slack() -> None:
    """Only the ``cli`` grant is withheld from Slack: the owner's own ``yolo on`` is not."""
    from kiro_crew.slack.handler import is_yolo_mode

    with patch("kiro_crew.safety_override.sel"):
        safety_override().activate("slack")

    assert is_yolo_mode() is True


@pytest.mark.parametrize(
    ("slot", "channel_origin", "reaches"),
    [
        (SimpleNamespace(), False, True),
        (SimpleNamespace(), True, False),
        (SimpleNamespace(linked_session_key="slack:1793.1"), False, False),
        (SimpleNamespace(_slack_channel="C1"), False, False),
        (SimpleNamespace(_turn_channel_narrowed=True), False, False),
        (SimpleNamespace(_turn_channel_origin=True), False, False),
        (SimpleNamespace(linked_session_key="cron:job-1:run-1"), False, True),
    ],
    ids=[
        "dashboard",
        "channel-origin",
        "channel-born",
        "slack-linked",
        "channel-steered",
        "channel-opened-then-unlinked",
        "cron-tab",
    ],
)
def test_the_cli_grant_reaches_unlinked_dashboard_turns_only(slot, channel_origin, reaches) -> None:
    """A turn a channel started or steered, or a channel-linked slot, keeps asking."""
    state = _launch("yolo")

    assert state.yolo_reaches(slot, channel_origin=channel_origin) is reaches


def test_any_other_grant_reaches_every_dashboard_turn() -> None:
    """The narrowing is the ``cli`` grant's alone: a dashboard pick still covers linked slots."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel"):
        safety_override().activate("dashboard")

    assert state.yolo_reaches(SimpleNamespace(_slack_channel="C1"), channel_origin=True) is True


def test_an_ended_cli_grant_is_not_one() -> None:
    """``is_cli_launch`` covers a live or renewable ``cli`` grant, never an ended one."""
    _launch("yolo")
    with patch("kiro_crew.safety_override.sel"):
        safety_override().deactivate("dashboard")

    assert safety_override().is_cli_launch is False


def test_gateway_approval_yolo_is_refused_when_policy_denies_yolo() -> None:
    """An ``approval_modes`` deny of ``yolo`` still refuses the flag's grant."""
    state = _make_state()
    with (
        patch("kiro_crew.safety_override.sel"),
        patch("kiro_crew.safety_override.yolo_policy_permits", return_value=False),
    ):
        _apply_startup_yolo(state, _cfg(yolo=False), approval_mode="yolo")

    assert state.is_yolo_active() is False


def test_gateway_approval_yolo_is_refused_when_its_audit_fails() -> None:
    """The grant's activation audit is critical: no audit row, no grant."""
    state = _make_state()
    with patch("kiro_crew.safety_override.sel") as mock_sel:
        mock_sel.return_value.log_api_access.side_effect = RuntimeError("sel down")
        _apply_startup_yolo(state, _cfg(yolo=False), approval_mode="yolo")

    assert state.is_yolo_active() is False


def test_gateway_approval_yolo_expires_at_the_time_cap(monkeypatch) -> None:
    """``yolo_max_seconds`` turns the flag's grant into a timed one that lapses."""
    from kiro_crew import safety_override_grant
    from kiro_crew.testing.clock import ManualClock

    clock = ManualClock()
    clock.install(monkeypatch, safety_override_grant)
    with patch("kiro_crew.safety_override.yolo_ttl_cap", return_value=60):
        state = _launch("yolo")
    assert safety_override().is_permanent is False
    assert state.is_yolo_active() is True

    clock.advance(61)
    assert state.is_yolo_active() is False


def test_gateway_approval_yolo_shows_on_the_status_bar() -> None:
    """The status frame reports the flag's grant, so the bar shows auto-approve on."""
    from kiro_crew.dashboard.status_counts import yolo_grant_fields

    _launch("yolo")
    fields = yolo_grant_fields()

    assert fields["yolo_active"] is True
    assert fields["yolo_until_shutdown"] is True


@pytest.mark.parametrize("approval_mode", [None, "", "reads", "auto", "normal", "YOLO"])
def test_only_gateway_approval_yolo_arms_the_grant(approval_mode) -> None:
    """Every other ``--approval`` value leaves dashboard chats prompting."""
    assert _launch(approval_mode).is_yolo_active() is False


@pytest.mark.parametrize(
    "title",
    ["Running: rm -rf /", f"{os.path.expanduser('~')}/.kirocrew/run/gateway-7781.bin"],
)
def test_gateway_approval_yolo_keeps_the_tool_gate_floors(title) -> None:
    """The flag's grant never lifts the deny list or the protected-path gate."""
    assert _launch("yolo").is_yolo_active() is True

    assert HookManager().on_tool_call(title, session_key="cli_chat").action == TOOL_DENY


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
