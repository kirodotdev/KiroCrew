"""Updates an administrator's policy decides.

Covers a policy-selected update provider (its failure never falls back to the
legacy updater, and its state is published before the operator is notified) and a
policy min-version that makes an update mandatory on each install kind: applied
through the shadow engine on a managed venv, reset on a git checkout, notified or
badged where the gateway cannot apply it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import _make_orchestrator, _mock_dashboard_state


class TestProviderFailureDoesNotFallBackToLegacy:
    """A policy-selected provider OWNS the update. Falling through to the legacy
    updater on its failure would run the built-in git/CDN update on a host whose
    administrator selected a different package manager."""

    @pytest.mark.asyncio
    async def test_provider_raising_does_not_run_legacy(self, monkeypatch):
        from kiro_crew.platform.update_provider import CommandProvider

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        provider = CommandProvider(check_command="c", apply_command="a")
        monkeypatch.setattr("kiro_crew.platform.update_provider.resolve_provider", lambda: provider)
        boom = AsyncMock(side_effect=RuntimeError("provider exploded"))
        monkeypatch.setattr(orch, "_check_for_updates_via_provider", boom)
        legacy = AsyncMock()
        monkeypatch.setattr(orch, "_check_for_updates_legacy", legacy)

        # Contained, not propagated: this runs on the gateway boot path.
        await orch._check_for_updates()

        legacy.assert_not_awaited()
        boom.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resolution_failure_still_uses_legacy(self, monkeypatch):
        """Only RESOLUTION failures may fall back: if the policy cannot be read
        we do not know a provider was selected, so built-in behaviour is right."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        def _boom():
            raise RuntimeError("policy unreadable")

        monkeypatch.setattr("kiro_crew.platform.update_provider.resolve_provider", _boom)
        legacy = AsyncMock()
        monkeypatch.setattr(orch, "_check_for_updates_legacy", legacy)

        await orch._check_for_updates()
        legacy.assert_awaited_once()


class TestProviderStateIsPublishedBeforeNotifying:
    """The SSE snapshot renders the update badge from _update_info["available"],
    which only the legacy check writes. A provider carries its own result, so
    notifying without publishing it left the badge reading a stale False and the
    operator never saw a waiting policy-defined update."""

    @pytest.mark.asyncio
    async def test_auto_update_off_publishes_state_before_notifying(self, monkeypatch):
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov
        from kiro_crew.platform.update_provider import UpdateCheckResult

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        handlers._update_info.clear()
        handlers._update_info.update({"update_available": False})
        monkeypatch.setattr(gov, "update_required", lambda _v: False)

        cfg = MagicMock()
        cfg.auto_update = False
        # A real provider: the method asserts isinstance(provider, UpdateProvider),
        # which a bare MagicMock does not satisfy.
        from kiro_crew.platform.update_provider import CommandProvider

        provider = CommandProvider(check_command="c", apply_command="a")
        provider.check = AsyncMock(  # type: ignore[method-assign]
            return_value=UpdateCheckResult(available=True, remote_version="9.9.9")
        )

        with patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg):
            await orch._check_for_updates_via_provider(provider)

        # The badge must be able to see it, not just the log. `check_status` is
        # asserted too: under the capability contract a verdict without a status is
        # indistinguishable from a check that never ran, so the badge would stay
        # dark on a provider's real answer.
        assert handlers._update_info["update_available"] is True
        assert handlers._update_info["latest_version"] == "9.9.9"
        assert handlers._update_info["check_status"] == "succeeded"
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_provider_check_publishes_command_ownership_when_up_to_date(self, monkeypatch):
        """The status frame must say a command owns updates even when the
        provider finds nothing and no dashboard-side check has run."""
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov
        from kiro_crew.platform.update_provider import CommandProvider, UpdateCheckResult

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        handlers._update_info.clear()
        monkeypatch.setattr(gov, "update_required", lambda _v: False)
        provider = CommandProvider(check_command="c", apply_command="a")
        provider.check = AsyncMock(  # type: ignore[method-assign]
            return_value=UpdateCheckResult(available=False)
        )

        await orch._check_for_updates_via_provider(provider)

        assert handlers._update_info["managed_by"] == "command"

    @pytest.mark.asyncio
    async def test_auto_update_busy_defers_provider_apply(self, monkeypatch):
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov
        from kiro_crew.platform.update_provider import CommandProvider, UpdateCheckResult

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._update_apply_deferred = False
        orch._prepare_auto_update_apply = AsyncMock(return_value=False)
        handlers._update_info.clear()
        handlers._update_info.update({"update_available": False})
        monkeypatch.setattr(gov, "update_required", lambda _v: False)

        cfg = MagicMock()
        cfg.auto_update = True
        provider = CommandProvider(check_command="c", apply_command="a")
        provider.check = AsyncMock(  # type: ignore[method-assign]
            return_value=UpdateCheckResult(available=True, remote_version="9.9.9")
        )
        provider.apply = AsyncMock(return_value=True)  # type: ignore[method-assign]

        with patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg):
            await orch._check_for_updates_via_provider(provider)

        provider.apply.assert_not_awaited()
        assert handlers._update_info["update_available"] is True
        assert handlers._update_info["latest_version"] == "9.9.9"


class TestMandatoryUpdateOnWheelInstall:
    """A policy min-version makes an update mandatory. On a managed-venv install
    the gateway applies it through the shadow engine (signed manifest, wheel
    digest, a sibling tree promoted by symlink): _auto_apply_wheel_update is
    called instead of merely lighting the dashboard badge."""
