"""Tests for kiro_crew.safety_override — time-limited safety override (YOLO replacement)."""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.safety_override import (
    ActivationResult,
    OverrideStatus,
    RenewResult,
    SafetyOverride,
    reset_singleton,
    safety_override,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the singleton between tests."""
    reset_singleton()
    yield
    reset_singleton()


@pytest.fixture
def override() -> SafetyOverride:
    """Create a fresh SafetyOverride instance bypassing the singleton."""
    inst = object.__new__(SafetyOverride)
    inst._active = False
    inst._source = ""
    inst._activated_at = 0.0
    inst._expires_at = 0.0
    inst._activation_count = 0
    inst._last_renewed_at = 0.0
    inst._last_renewed_by = ""
    inst._on_expired = None
    inst._on_activated = None
    return inst


# ─── Activation ─────────────────────────────────────────────────────────────


class TestActivation:
    def test_activate_from_slack(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("slack")
        assert isinstance(result, ActivationResult)
        assert result.ttl == SafetyOverride._SLACK_TTL
        assert result.ttl == 1800
        assert result.active is True

    def test_activate_from_dashboard(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("dashboard")
        assert result.ttl == SafetyOverride._DASHBOARD_TTL
        assert result.ttl == 21600
        assert result.active is True

    def test_activate_from_config(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("config")
        assert result.ttl == SafetyOverride._CONFIG_TTL
        assert result.ttl == 86400
        assert result.active is True

    def test_activate_caps_at_max_ttl(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("slack", ttl=200000)
        assert result.ttl == SafetyOverride._MAX_TTL
        assert result.ttl == 86400

    def test_activate_fires_callback(self, override: SafetyOverride) -> None:
        callback = MagicMock()
        override._on_activated = callback
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("slack")
        callback.assert_called_once_with("slack", result.ttl)

    def test_activation_count_increments(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            assert override._activation_count == 0
            override.activate("slack")
            assert override._activation_count == 1
            override.activate("dashboard")
            assert override._activation_count == 2

    def test_activate_custom_ttl_within_max(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("slack", ttl=3600)
        assert result.ttl == 3600

    def test_activate_sets_active_true(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            assert not override.is_active()
            override.activate("slack")
        assert override.is_active()


# ─── Expiry ─────────────────────────────────────────────────────────────────


class TestExpiry:
    def test_is_active_returns_false_after_expiry(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack", ttl=1)
        # Manually expire it
        override._expires_at = time.monotonic() - 1
        assert not override.is_active()

    def test_expiry_fires_callback(self, override: SafetyOverride) -> None:
        callback = MagicMock()
        override._on_expired = callback
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack", ttl=1)
        # Manually expire
        override._expires_at = time.monotonic() - 1
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.is_active()
        assert not result
        callback.assert_called_once_with("slack")

    def test_expiry_logs_sel_event(self, override: SafetyOverride) -> None:
        mock_sel_instance = MagicMock()
        with patch("kiro_crew.safety_override.sel", return_value=mock_sel_instance):
            override.activate("slack", ttl=1)
        # Force expiry
        override._expires_at = time.monotonic() - 1
        mock_sel_instance2 = MagicMock()
        with patch("kiro_crew.safety_override.sel", return_value=mock_sel_instance2):
            override.is_active()
        mock_sel_instance2.log_api_access.assert_called_once()
        call_kwargs = mock_sel_instance2.log_api_access.call_args.kwargs
        assert call_kwargs["operation"] == "safety_override:expired"
        assert call_kwargs["outcome"] == "expired"


# ─── Deactivation ───────────────────────────────────────────────────────────


class TestDeactivation:
    def test_deactivate(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack")
            assert override.is_active()
            override.deactivate("slack")
        assert not override.is_active()

    def test_deactivate_when_inactive_is_noop(self, override: SafetyOverride) -> None:
        # Should not raise, not log a SEL event
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel_instance = MagicMock()
            mock_sel.return_value = mock_sel_instance
            assert not override.is_active()
            override.deactivate("slack")
        mock_sel_instance.log_api_access.assert_not_called()

    def test_renew_after_explicit_deactivate_fails(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack")
            override.deactivate("slack")
            result = override.renew("slack")
        assert result.renewed is False
        assert result.reason == "not_active"


# ─── Renewal ────────────────────────────────────────────────────────────────


class TestRenewal:
    def test_renew_active_override(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack")
            result = override.renew("slack")
        assert isinstance(result, RenewResult)
        assert result.renewed is True
        assert result.ttl > 0

    def test_renew_within_grace_period(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack", ttl=1)
        # Expire it but stay within grace window (_RENEW_GRACE_SECS = 300)
        override._expires_at = time.monotonic() - 60  # 60s past expiry, < 300s grace
        override._active = False  # mark expired
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.renew("slack")
        assert result.renewed is True

    def test_renew_outside_grace_period_fails(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack", ttl=1)
        # Expire it way beyond grace window
        override._expires_at = time.monotonic() - 400  # 400s past expiry > 300s grace
        override._active = False
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.renew("slack")
        assert result.renewed is False

    def test_renew_logs_sel(self, override: SafetyOverride) -> None:
        mock_sel_instance = MagicMock()
        with patch("kiro_crew.safety_override.sel", return_value=mock_sel_instance):
            override.activate("slack")
            override.renew("slack")
        # Expect at least two log_api_access calls: activation + renewal
        calls = mock_sel_instance.log_api_access.call_args_list
        operations = [c.kwargs["operation"] for c in calls]
        assert "safety_override:renew" in operations

    def test_renew_denied_logs_sel(self, override: SafetyOverride) -> None:
        # Renew on an override that was never activated (neither active nor in grace)
        mock_sel_instance = MagicMock()
        with patch("kiro_crew.safety_override.sel", return_value=mock_sel_instance):
            result = override.renew("slack")
        assert result.renewed is False
        calls = mock_sel_instance.log_api_access.call_args_list
        operations = [c.kwargs["operation"] for c in calls]
        assert "safety_override:renew" in operations
        outcomes = [c.kwargs["outcome"] for c in calls]
        assert "denied" in outcomes


# ─── Status ─────────────────────────────────────────────────────────────────


class TestStatus:
    def test_status_when_active(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack")
        status = override.status()
        assert isinstance(status, OverrideStatus)
        assert status.active is True
        assert status.source == "slack"
        assert status.remaining_secs > 0
        assert status.activated_at_iso is not None
        assert status.expires_at_iso is not None
        # Verify ISO 8601 format
        assert "T" in status.activated_at_iso
        assert "T" in status.expires_at_iso

    def test_status_when_inactive(self, override: SafetyOverride) -> None:
        status = override.status()
        assert isinstance(status, OverrideStatus)
        assert status.active is False
        assert status.remaining_secs == 0
        assert status.activated_at_iso is None
        assert status.expires_at_iso is None


# ─── remaining_secs ─────────────────────────────────────────────────────────


class TestRemainingSecs:
    def test_remaining_secs_when_inactive(self, override: SafetyOverride) -> None:
        assert override.remaining_secs() == 0

    def test_remaining_secs_when_active(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack", ttl=3600)
        secs = override.remaining_secs()
        assert secs > 3500  # just activated, should be close to 3600
        assert secs <= 3600

    def test_remaining_secs_zero_after_expiry(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack", ttl=1)
        override._expires_at = time.monotonic() - 5
        assert override.remaining_secs() == 0


# ─── Singleton ──────────────────────────────────────────────────────────────


class TestSingleton:
    def test_safety_override_returns_same_instance(self) -> None:
        a = safety_override()
        b = safety_override()
        assert a is b

    def test_reset_singleton_creates_fresh_instance(self) -> None:
        a = safety_override()
        reset_singleton()
        b = safety_override()
        assert a is not b

    def test_singleton_is_safetyoverride_instance(self) -> None:
        inst = safety_override()
        assert isinstance(inst, SafetyOverride)


# ─── SEL fault tolerance ────────────────────────────────────────────────────


class TestSelFaultTolerance:
    def test_sel_crash_rolls_back_activate(self, override: SafetyOverride) -> None:
        """SEL audit failure during activate() must roll back — fail closed."""
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock(log_api_access=MagicMock(side_effect=RuntimeError("boom")))
            result = override.activate("slack")
        assert result.active is False
        assert not override.is_active()
        assert override._expires_at == 0.0
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            renew_result = override.renew("slack")
        assert renew_result.renewed is False

    def test_sel_crash_does_not_crash_deactivate(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack")
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock(log_api_access=MagicMock(side_effect=RuntimeError("boom")))
            # Should not raise
            override.deactivate("slack")
        assert not override.is_active()

    def test_sel_import_error_rolls_back_activate(self, override: SafetyOverride) -> None:
        """SEL import error during activate() must roll back — fail closed."""
        with patch("kiro_crew.safety_override.sel", side_effect=ImportError("no sel")):
            result = override.activate("slack")
        assert result.active is False
        assert not override.is_active()

    def test_activate_denied_with_real_async_sel_unwritable(
        self, override: SafetyOverride, tmp_path, monkeypatch
    ) -> None:
        """End-to-end reproduction of the pentest finding.

        Using the REAL async SEL (not a mock), make the log file unwritable and
        confirm activation is DENIED with no state change. Before the fix,
        ``log_api_access`` enqueued to the background writer and returned, so
        the async writer swallowed the PermissionError and YOLO activated
        unaudited (activation_count incremented, _active flipped True). With the
        critical synchronous write, the error propagates and activate() rolls
        back.
        """
        from kiro_crew.sel import SecurityEventLog

        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        real_sel = SecurityEventLog(base_dir=tmp_path)
        monkeypatch.setattr("kiro_crew.safety_override.sel", lambda: real_sel)

        real_os_open = os.open

        def _boom(path, *a, **k):
            if str(path).endswith("security_events.jsonl"):
                raise PermissionError("SEL file unwritable (chmod 000)")
            return real_os_open(path, *a, **k)

        monkeypatch.setattr(os, "open", _boom)
        try:
            result = override.activate("dashboard")
        finally:
            monkeypatch.undo()
            SecurityEventLog._instance = None
            SecurityEventLog._initialized = False

        # Fail-closed: activation refused, no state committed.
        assert result.active is False
        assert override._active is False
        assert override._activation_count == 0
        assert override._expires_at == 0.0
        # And no activate audit record was persisted.
        sel_file = tmp_path / "security_events.jsonl"
        if sel_file.exists():
            assert "safety_override:activate" not in sel_file.read_text(encoding="utf-8")


# ─── Callbacks ──────────────────────────────────────────────────────────────


class TestCallbacks:
    def test_on_activated_callback_receives_correct_args(self, override: SafetyOverride) -> None:
        received: list[tuple] = []
        override._on_activated = lambda source, ttl: received.append((source, ttl))
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("dashboard")
        assert len(received) == 1
        assert received[0] == ("dashboard", result.ttl)

    def test_on_expired_callback_receives_source(self, override: SafetyOverride) -> None:
        received: list[str] = []
        override._on_expired = lambda source: received.append(source)
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("config")
        override._expires_at = time.monotonic() - 1
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.is_active()
        assert received == ["config"]

    def test_no_callback_set_does_not_error(self, override: SafetyOverride) -> None:
        """Neither callback set — activation and expiry must not raise."""
        assert override._on_activated is None
        assert override._on_expired is None
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack")
        override._expires_at = time.monotonic() - 1
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            assert not override.is_active()


# ─── SOURCE_TTLS constant ───────────────────────────────────────────────────


class TestSourceTtls:
    def test_source_ttls_maps_slack(self) -> None:
        assert SafetyOverride._SOURCE_TTLS["slack"] == SafetyOverride._SLACK_TTL

    def test_source_ttls_maps_dashboard(self) -> None:
        assert SafetyOverride._SOURCE_TTLS["dashboard"] == SafetyOverride._DASHBOARD_TTL

    def test_source_ttls_maps_config(self) -> None:
        assert SafetyOverride._SOURCE_TTLS["config"] == SafetyOverride._CONFIG_TTL

    def test_activate_unknown_source_uses_slack_ttl(self, override: SafetyOverride) -> None:
        """Unknown sources should fall back to a sensible default."""
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("unknown_source")
        # Should not crash; use slack TTL as fallback
        assert result.active is True
        assert result.ttl == SafetyOverride._SLACK_TTL
