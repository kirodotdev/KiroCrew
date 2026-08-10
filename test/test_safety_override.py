"""Tests for kiro_crew.safety_override — time-limited safety override (YOLO replacement)."""

from __future__ import annotations

import json
import os
import pathlib
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
        assert result.ttl == SafetyOverride._ADHOC_TTL_DEFAULT
        assert result.ttl == 21600
        assert result.active is True

    def test_activate_from_dashboard(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("dashboard")
        assert result.ttl == SafetyOverride._ADHOC_TTL_DEFAULT
        assert result.ttl == 21600
        assert result.active is True

    def test_activate_from_config(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("config")
        assert result.ttl == SafetyOverride._ADHOC_TTL_DEFAULT
        assert result.ttl == 21600
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

    def test_deactivate_lapsed_grant_emits_sel(self, override: SafetyOverride) -> None:
        """An explicit deactivate against a grant that already lapsed is an
        operator DECISION and must reach the SEL sink — lazy expiry clearing
        ``_active`` first must not swallow it."""
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack")
        override._expires_at = time.monotonic() - 1
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            assert not override.is_active()  # trips lazy expiry
        # Pin the discriminator: lazy expiry clears _active but NOT _expires_at,
        # which is exactly why the deactivate guard must not key off _active.
        assert override._active is False
        assert override._expires_at > 0.0

        mock_sel_instance = MagicMock()
        with patch("kiro_crew.safety_override.sel", return_value=mock_sel_instance):
            override.deactivate("dashboard")
        mock_sel_instance.log_api_access.assert_called_once()
        kwargs = mock_sel_instance.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "safety_override:deactivate"
        assert kwargs["outcome"] == "disabled"
        assert "source:dashboard" in kwargs["resources"]
        assert "was_active:False" in kwargs["resources"]
        assert "was_permanent:False" in kwargs["resources"]
        assert "remaining:0s" in kwargs["resources"]
        assert "prior_source:slack" in kwargs["resources"]

    def test_deactivate_live_grant_emits_sel_with_was_active(
        self, override: SafetyOverride
    ) -> None:
        """A live-grant deactivate keeps its event, with was_active recording
        that a real grant was revoked — the lapsed-grant event does not
        replace or dilute the existing signal."""
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack", ttl=600)
        mock_sel_instance = MagicMock()
        with patch("kiro_crew.safety_override.sel", return_value=mock_sel_instance):
            override.deactivate("slack")
        mock_sel_instance.log_api_access.assert_called_once()
        kwargs = mock_sel_instance.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "safety_override:deactivate"
        assert kwargs["outcome"] == "disabled"
        assert "was_active:True" in kwargs["resources"]

    def test_deactivate_permanent_grant_records_permanence(
        self, override: SafetyOverride
    ) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate_declared()
        mock_sel_instance = MagicMock()
        with patch("kiro_crew.safety_override.sel", return_value=mock_sel_instance):
            override.deactivate("dashboard")
        kwargs = mock_sel_instance.log_api_access.call_args.kwargs
        assert "was_active:True" in kwargs["resources"]
        assert "was_permanent:True" in kwargs["resources"]
        assert "remaining:-1s" in kwargs["resources"]

    def test_second_deactivate_is_silent(self, override: SafetyOverride) -> None:
        """After an explicit deactivate the 0.0 sentinel is restored, so a
        repeat call has no grant to report and emits nothing."""
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack")
            override.deactivate("slack")
        mock_sel_instance = MagicMock()
        with patch("kiro_crew.safety_override.sel", return_value=mock_sel_instance):
            override.deactivate("slack")
        mock_sel_instance.log_api_access.assert_not_called()

    def test_deactivate_lapsed_unpolled_grant_reports_inactive(
        self, override: SafetyOverride
    ) -> None:
        """A TTL that lapsed WITHOUT an intervening is_active() poll leaves
        _active stale at True; the event must still report was_active:False —
        liveness is derived from the deadline, not the unreconciled flag."""
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack")
        override._expires_at = time.monotonic() - 1
        assert override._active is True  # lazy expiry has NOT run
        mock_sel_instance = MagicMock()
        with patch("kiro_crew.safety_override.sel", return_value=mock_sel_instance):
            override.deactivate("dashboard")
        mock_sel_instance.log_api_access.assert_called_once()
        kwargs = mock_sel_instance.log_api_access.call_args.kwargs
        assert "was_active:False" in kwargs["resources"]
        assert "remaining:0s" in kwargs["resources"]

    def test_renew_after_deactivating_lapsed_grant_fails(
        self, override: SafetyOverride
    ) -> None:
        """Deactivating a lapsed grant zeroes _expires_at, so the renew grace
        window cannot resurrect an explicitly revoked grant."""
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack")
            override._expires_at = time.monotonic() - 60  # lapsed, within 300s grace
            assert not override.is_active()
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


# ─── One ad-hoc duration for every surface ──────────────────────────────────


class TestAdhocDuration:
    """Slack, dashboard and API grants all expire on the same clock."""

    def test_every_adhoc_source_gets_the_same_ttl(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            ttls = {src: override.activate(src).ttl for src in ("slack", "dashboard", "api")}
        assert len(set(ttls.values())) == 1, f"per-surface TTLs diverged: {ttls}"
        assert set(ttls.values()) == {SafetyOverride._ADHOC_TTL_DEFAULT}

    def test_default_is_six_hours(self) -> None:
        assert SafetyOverride._ADHOC_TTL_DEFAULT == 21600

    def test_configured_duration_is_honoured(self, override: SafetyOverride) -> None:
        override.adhoc_ttl = 3600
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            assert override.activate("slack").ttl == 3600
            assert override.activate("dashboard").ttl == 3600

    def test_configured_duration_is_capped_at_ceiling(self, override: SafetyOverride) -> None:
        override.adhoc_ttl = 999999
        assert override.adhoc_ttl == SafetyOverride._MAX_TTL

    def test_activate_unknown_source_uses_adhoc_ttl(self, override: SafetyOverride) -> None:
        """Unknown sources should fall back to a sensible default."""
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("unknown_source")
        assert result.active is True
        assert result.ttl == SafetyOverride._ADHOC_TTL_DEFAULT


# ─── Task-scoped grants ───────────────────────────────────────────────────────


class TestScopedGrants:
    def test_activate_scoped_uses_source_ttl(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate_scoped("taskrunner:t1:autoapprove", source="dashboard")
        assert result.active is True
        assert result.ttl == SafetyOverride._ADHOC_TTL_DEFAULT
        assert override.is_scope_active("taskrunner:t1:autoapprove") is True

    def test_ttl_capped_at_ceiling(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate_scoped("s", source="dashboard", ttl=999999)
        assert result.ttl == SafetyOverride._MAX_TTL

    def test_unknown_scope_is_inactive(self, override: SafetyOverride) -> None:
        assert override.is_scope_active("never-granted") is False

    def test_scope_expires_and_is_purged(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate_scoped("s", source="dashboard", ttl=60)
            # Fast-forward past the TTL.
            with patch("kiro_crew.safety_override.time.monotonic", return_value=time.monotonic() + 120):
                assert override.is_scope_active("s") is False
        # Purged from the internal map after expiry.
        assert "s" not in override._scoped

    def test_deactivate_scope_revokes(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate_scoped("s", source="dashboard")
            assert override.is_scope_active("s") is True
            override.deactivate_scope("s")
        assert override.is_scope_active("s") is False

    def test_scoped_grant_does_not_flip_global(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate_scoped("s", source="dashboard")
        # A scoped grant must NOT activate the session-wide override.
        assert override.is_active() is False

    def test_activate_scoped_fails_closed_on_audit_error(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value.log_api_access.side_effect = RuntimeError("sel down")
            result = override.activate_scoped("s", source="dashboard")
        # No grant is committed when the fail-closed audit raises.
        assert result.active is False
        assert override.is_scope_active("s") is False

    def test_renew_scoped_extends_expiry(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            base = time.monotonic()
            with patch("kiro_crew.safety_override.time.monotonic", return_value=base):
                override.activate_scoped("s", source="dashboard", ttl=100)
            # 90s later a tool call renews it — expiry slides forward.
            with patch("kiro_crew.safety_override.time.monotonic", return_value=base + 90):
                r = override.renew_scoped("s", source="dashboard", ttl=100)
                assert r.renewed is True
                # Now ~100s of remaining window, not the ~10s left before renewal.
                assert override.scope_remaining_secs("s") > 50

    def test_renew_capped_at_ceiling(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            base = time.monotonic()
            with patch("kiro_crew.safety_override.time.monotonic", return_value=base):
                override.activate_scoped("s", source="dashboard", ttl=100)
            # Past the 24h ceiling from first activation → cannot renew further.
            with patch(
                "kiro_crew.safety_override.time.monotonic",
                return_value=base + SafetyOverride._MAX_TTL + 10,
            ):
                r = override.renew_scoped("s", source="dashboard", ttl=100)
                assert r.renewed is False
                assert r.reason == "ceiling_reached"

    def test_renew_absent_scope(self, override: SafetyOverride) -> None:
        r = override.renew_scoped("never", source="dashboard")
        assert r.renewed is False
        assert r.reason == "not_active"


# ─── Standing authority (config-declared grants survive their TTL) ──────────


# ─── Declared grants never expire ───────────────────────────────────────────


class TestDeclaredGrant:
    """``dangerouslySkipPermissions`` is standing, not a session decision."""

    def test_declared_grant_never_expires(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate_declared()
            # Push far past any conceivable deadline.
            override._expires_at = time.monotonic() - 1
            assert override.is_active() is True

    def test_declared_grant_survives_the_24h_ceiling(self, override: SafetyOverride) -> None:
        """The finite placeholder deadline must not resurrect expiry."""
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate_declared()
            base = time.monotonic()
            with patch(
                "kiro_crew.safety_override.time.monotonic",
                return_value=base + SafetyOverride._MAX_TTL + 60,
            ):
                assert override.is_active() is True
                assert override.status().active is True

    def test_declared_grant_reports_no_expiry(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate_declared()
        st = override.status()
        assert st.permanent is True
        assert st.remaining_secs == -1
        assert st.expires_at_iso is None
        assert override.remaining_secs() == -1
        assert override.is_permanent is True

    def test_no_expiry_callback_ever_fires(self, override: SafetyOverride) -> None:
        cb = MagicMock()
        override.on_expired = cb
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate_declared()
            override._expires_at = time.monotonic() - 1
            assert override.is_active() is True
        cb.assert_not_called()

    def test_deactivate_clears_a_declared_grant(self, override: SafetyOverride) -> None:
        """Picking another approval mode wins immediately."""
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate_declared()
            override.deactivate("dashboard")
        assert override.is_active() is False
        assert override.is_permanent is False
        assert override.status().permanent is False

    def test_renew_does_not_downgrade_to_a_deadline(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate_declared()
            r = override.renew("dashboard")
        assert r.renewed is True
        assert r.ttl == -1
        assert override.is_permanent is True

    def test_adhoc_activation_downgrades_a_declared_grant(self, override: SafetyOverride) -> None:
        """An explicit ad-hoc activation replaces permanence with a deadline."""
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate_declared()
            assert override.is_permanent is True
            override.activate("dashboard")
        assert override.is_permanent is False
        assert 0 < override.remaining_secs() <= SafetyOverride._ADHOC_TTL_DEFAULT

    def test_declared_activation_is_audited_as_permanent(self, override: SafetyOverride) -> None:
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate_declared()
            calls = mock_sel.return_value.log_api_access.call_args_list
        acts = [c for c in calls if c.kwargs["operation"] == "safety_override:activate"]
        assert len(acts) == 1
        assert "ttl:permanent" in acts[0].kwargs["resources"]
        assert "source:config" in acts[0].kwargs["resources"]

    def test_sel_failure_still_refuses_a_declared_grant(self, override: SafetyOverride) -> None:
        """Fail-closed discipline is not weakened by the permanent path."""
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value.log_api_access.side_effect = OSError("disk full")
            result = override.activate_declared()
        assert result.active is False
        assert override.is_active() is False
        assert override._expires_at == 0.0


class TestDeclaredGrantGovernance:
    """An enterprise policy can forbid a never-expiring grant."""

    def test_permitted_when_ungoverned(self) -> None:
        from kiro_crew.safety_override import declared_grant_permitted

        assert declared_grant_permitted() is True

    def test_denied_when_policy_denies_permanent(self) -> None:
        from kiro_crew import safety_override as so_mod

        denied = MagicMock()
        denied.permitted = False
        with patch(
            "kiro_crew.platform.governance_profiles.governance_permits", return_value=denied
        ):
            assert so_mod.declared_grant_permitted() is False

    def test_asks_the_host_profile_fail_closed(self) -> None:
        """Bypassing the host profile would let a session profile decide this."""
        from kiro_crew import safety_override as so_mod
        from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

        permitted = MagicMock()
        permitted.permitted = True
        with patch(
            "kiro_crew.platform.governance_profiles.governance_permits", return_value=permitted
        ) as gp:
            so_mod.declared_grant_permitted()
        assert gp.call_args.args == ("yolo_duration", "permanent")
        assert gp.call_args.kwargs["session_key"] == HOST_SESSION_KEY
        assert gp.call_args.kwargs["fail_closed"] is True

    def test_denied_policy_falls_back_to_adhoc_ttl(self) -> None:
        """A forbidden permanent grant becomes a bounded one, not nothing."""
        from kiro_crew import safety_override as so_mod

        with patch("kiro_crew.safety_override.sel") as mock_sel, patch.object(
            so_mod, "declared_grant_permitted", return_value=False
        ), patch.object(so_mod, "apply_config_duration", return_value=21600):
            mock_sel.return_value = MagicMock()
            result = so_mod.grant_declared_yolo()
        so = safety_override()
        assert result.active is True
        assert so.is_permanent is False
        assert so.is_active() is True
        assert 0 < so.remaining_secs() <= SafetyOverride._MAX_TTL

    def test_permitted_policy_grants_permanence(self) -> None:
        from kiro_crew import safety_override as so_mod

        with patch("kiro_crew.safety_override.sel") as mock_sel, patch.object(
            so_mod, "declared_grant_permitted", return_value=True
        ), patch.object(so_mod, "apply_config_duration", return_value=21600):
            mock_sel.return_value = MagicMock()
            result = so_mod.grant_declared_yolo()
        assert result.active is True
        assert safety_override().is_permanent is True


# ─── until_shutdown: an ad-hoc grant with no timed expiry ───────────────────


class TestUntilShutdownDuration:
    """``yolo_duration: until_shutdown`` keeps an ad-hoc grant until restart.

    Distinct from a DECLARED grant: this one is NOT re-established at startup,
    so a restart genuinely clears it.
    """

    def test_adhoc_grant_has_no_timed_expiry(self, override: SafetyOverride) -> None:
        override.adhoc_until_shutdown = True
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("dashboard")
        assert result.active is True
        assert override.is_permanent is True
        assert override.remaining_secs() == -1

    def test_survives_past_the_ceiling(self, override: SafetyOverride) -> None:
        override.adhoc_until_shutdown = True
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("slack")
        base = time.monotonic()
        with patch(
            "kiro_crew.safety_override.time.monotonic",
            return_value=base + SafetyOverride._MAX_TTL + 60,
        ):
            assert override.is_active() is True

    def test_every_adhoc_surface_gets_it(self, override: SafetyOverride) -> None:
        override.adhoc_until_shutdown = True
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            for src in ("slack", "dashboard", "api"):
                override.activate(src)
                assert override.is_permanent is True, f"{src} did not get until_shutdown"

    def test_explicit_ttl_still_wins(self, override: SafetyOverride) -> None:
        """A caller asking for a specific TTL must get a timed grant."""
        override.adhoc_until_shutdown = True
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("dashboard", ttl=600)
        assert result.ttl == 600
        assert override.is_permanent is False

    def test_deactivate_clears_it(self, override: SafetyOverride) -> None:
        override.adhoc_until_shutdown = True
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("dashboard")
            override.deactivate("dashboard")
        assert override.is_active() is False

    def test_governance_can_forbid_it(self) -> None:
        """A denied until_shutdown falls back to a timed duration."""
        from kiro_crew import safety_override as so_mod

        cfg = MagicMock()
        cfg.agent.yolo_duration = "until_shutdown"
        denied = MagicMock()
        denied.permitted = False
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg), patch(
            "kiro_crew.platform.governance_profiles.governance_permits", return_value=denied
        ):
            secs = so_mod.apply_config_duration()
        assert secs == SafetyOverride._ADHOC_TTL_DEFAULT
        assert safety_override().adhoc_until_shutdown is False

    def test_permitted_until_shutdown_is_applied(self) -> None:
        from kiro_crew import safety_override as so_mod

        cfg = MagicMock()
        cfg.agent.yolo_duration = "until_shutdown"
        permitted = MagicMock()
        permitted.permitted = True
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg), patch(
            "kiro_crew.platform.governance_profiles.governance_permits", return_value=permitted
        ):
            secs = so_mod.apply_config_duration()
        assert secs == 0
        assert safety_override().adhoc_until_shutdown is True


class TestRenamedConfigKey:
    """``dangerouslySkipPermissions`` replaces ``yolo``, without breaking it."""

    @staticmethod
    def _load(agent: dict) -> bool:
        from kiro_crew.config.loader import _read_skip_permissions

        return _read_skip_permissions(agent)

    def test_new_key_is_honoured(self) -> None:
        assert self._load({"dangerously_skip_permissions": True}) is True
        assert self._load({"dangerously_skip_permissions": False}) is False

    def test_camel_case_spelling_is_accepted(self) -> None:
        """Other agent tools use the camelCase form; a copied config should work."""
        assert self._load({"dangerouslySkipPermissions": True}) is True

    def test_legacy_yolo_key_still_works(self) -> None:
        """An existing config must not silently lose auto-approve on upgrade."""
        assert self._load({"yolo": True}) is True

    def test_canonical_key_wins_over_older_spellings(self) -> None:
        assert self._load({"dangerously_skip_permissions": False, "yolo": True}) is False
        assert (
            self._load({"dangerously_skip_permissions": True, "dangerouslySkipPermissions": False})
            is True
        )
        assert self._load({"dangerouslySkipPermissions": False, "yolo": True}) is False

    def test_round_trips_through_save(self, tmp_path: pathlib.Path) -> None:
        """save() writes the canonical key, so a reload must see the same value.

        Regression guard: reading only the camelCase spelling while save() wrote
        snake_case silently dropped the setting on the next load.
        """
        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        cfg_file.write_text(json.dumps({"agent": {"dangerously_skip_permissions": False}}))
        with patch("kiro_crew.config.loader.config_path", return_value=cfg_file), patch(
            "kiro_crew.config.loader.config_local_path", return_value=local
        ):
            cfg = KiroCrewConfig.load()
            assert cfg.agent.dangerously_skip_permissions is False
            cfg.agent.dangerously_skip_permissions = True
            cfg.save()
            assert KiroCrewConfig.load().agent.dangerously_skip_permissions is True

    def test_absent_defaults_to_off(self) -> None:
        assert self._load({}) is False


# ─── Never-expiring grants must not be described as expiring ─────────────────


class TestGrantLifetimeCopy:
    """Slack surfaces must not claim a no-expiry grant self-disarms.

    Regression guard for the false-safety-signal class: before no-expiry grants
    existed every message could safely assume a finite TTL, so ``remaining //
    60`` and "auto-expires in 6h" were always true. They are not any more.
    """

    @staticmethod
    def _helpers():
        from kiro_crew.slack.handler import describe_grant_lifetime, describe_new_grant

        return describe_grant_lifetime, describe_new_grant

    def test_declared_grant_is_not_described_as_expiring(self) -> None:
        describe_grant_lifetime, _ = self._helpers()
        so = safety_override()
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            so.activate_declared()
        text = describe_grant_lifetime()
        assert "restart" in text
        assert "remaining" not in text
        assert "-1" not in text

    def test_until_shutdown_grant_is_not_described_as_expiring(self) -> None:
        describe_grant_lifetime, _ = self._helpers()
        so = safety_override()
        so.adhoc_until_shutdown = True
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            so.activate("slack")
        text = describe_grant_lifetime()
        assert "restart" in text
        assert "min remaining" not in text

    def test_timed_grant_still_reports_minutes(self) -> None:
        describe_grant_lifetime, _ = self._helpers()
        so = safety_override()
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            so.activate("slack", ttl=3600)
        assert "min remaining" in describe_grant_lifetime()

    def test_inactive_reads_off(self) -> None:
        describe_grant_lifetime, _ = self._helpers()
        assert describe_grant_lifetime() == "off"

    def test_new_grant_description_matches_the_grant(self) -> None:
        _, describe_new_grant = self._helpers()
        assert "restart" in describe_new_grant(0)
        assert "restart" in describe_new_grant(-1)
        assert describe_new_grant(3600) == "auto-expires in 1h"

    def test_no_slack_surface_renders_a_raw_remaining_figure(self) -> None:
        """Structural guard: a new surface must use the helper, not `// 60`."""
        import pathlib

        import kiro_crew.slack.handler as h

        src_dir = pathlib.Path(h.__file__).parent
        offenders = []
        for name in ("handler.py", "events.py"):
            # Explicit utf-8: these files contain emoji, and the Windows default
            # (cp1252) cannot decode them.
            text = (src_dir / name).read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                if "remaining // 60" in line:
                    offenders.append(f"{name}:{i}")
        assert not offenders, f"raw remaining rendering reintroduced at {offenders}"


class TestDurationResolvedLive:
    """A duration saved from Settings must apply without a gateway restart."""

    def test_activate_reads_the_resolver_each_time(self, override: SafetyOverride) -> None:
        seen = {"ttl": 1800, "until": False}
        override.duration_resolver = lambda: (seen["ttl"], seen["until"])
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            assert override.activate("dashboard").ttl == 1800
            seen["ttl"] = 43200
            assert override.activate("dashboard").ttl == 43200, "stale duration reused"

    def test_resolver_can_switch_to_until_shutdown(self, override: SafetyOverride) -> None:
        state = {"until": False}
        override.duration_resolver = lambda: (21600, state["until"])
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            override.activate("dashboard")
            assert override.is_permanent is False
            state["until"] = True
            override.activate("dashboard")
            assert override.is_permanent is True

    def test_resolver_failure_falls_back_without_wedging(self, override: SafetyOverride) -> None:
        def _boom() -> tuple[int, bool]:
            raise RuntimeError("config unreadable")

        override.adhoc_ttl = 3600
        override.duration_resolver = _boom
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            result = override.activate("dashboard")
        assert result.active is True
        assert result.ttl == 3600

    def test_resolver_output_is_capped(self, override: SafetyOverride) -> None:
        override.duration_resolver = lambda: (999999, False)
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            assert override.activate("dashboard").ttl == SafetyOverride._MAX_TTL


class TestDurationIsEditableFromSettings:
    """The Settings card PATCHes the real handler, so the key must be allowlisted."""

    def test_yolo_duration_is_in_the_editable_allowlist(self) -> None:
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        spec = _EDITABLE_CONFIG.get("agent.yolo_duration")
        assert spec is not None, "Settings duration card would 400 on every save"
        assert spec["type"] == "enum"
        assert "until_shutdown" in spec["values"]
        assert "6h" in spec["values"]

    def test_the_declared_grant_is_NOT_editable_from_settings(self) -> None:
        """The never-expiring grant stays config-file-only, by design."""
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        assert "agent.dangerously_skip_permissions" not in _EDITABLE_CONFIG
        assert "agent.yolo" not in _EDITABLE_CONFIG


class TestStatusDoesNotBlockTheEventLoop:
    """The status payload's new fields touch disk, so they must run off-loop.

    ``/api/status`` is polled continuously; resolving the governance profile
    (an ``iterdir``/``stat`` walk) inline would stall the whole gateway on a slow
    home directory.
    """

    def test_duration_fields_are_resolved_in_a_worker_thread(self) -> None:
        import inspect

        from kiro_crew.dashboard import handlers_system

        src = inspect.getsource(handlers_system.api_status)
        assert "to_thread(_yolo_duration_fields)" in src, (
            "the duration/permission fields must be resolved via asyncio.to_thread"
        )

    def test_helper_is_fail_soft(self) -> None:
        """A broken config or governance layer must not break the status call."""
        from kiro_crew.dashboard.handlers_system import _yolo_duration_fields

        with patch(
            "kiro_crew.dashboard.handlers_system.KiroCrewConfig.load",
            side_effect=RuntimeError("unreadable"),
        ), patch(
            "kiro_crew.dashboard.handlers_system.until_shutdown_permitted",
            side_effect=RuntimeError("governance down"),
        ):
            label, permitted = _yolo_duration_fields()
        assert label == "6h"
        assert permitted is True
