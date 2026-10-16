"""Updates an administrator's policy decides.

Covers a policy-selected update provider (its failure never falls back to the
legacy updater, and its state is published before the operator is notified) and a
policy min-version that makes an update mandatory on each install kind: applied
through the shadow engine on a managed venv, reset on a git checkout, notified or
badged where the gateway cannot apply it.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _install_effect,
    _make_orchestrator,
    _mock_dashboard_state,
)


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

    @pytest.mark.asyncio
    async def test_mandatory_update_on_wheel_auto_applies(self, monkeypatch):
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        # Wheel install below a policy floor with a NEWER build available: the
        # mandatory update applies through the shadow engine. (The no-newer-build
        # case is test_mandatory_wheel_no_newer_build_notifies below — that path
        # must NOT apply, to avoid an infinite update→restart loop.)
        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": True,
                "can_apply": False,
                "managed_by": "kirocrew",
                # A feed-checkable wheel carries an installer command; that is
                # what distinguishes it from an externally-managed install.
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "curl -fsSL … | sh",
                },
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")
        # Only the managed venv may self-update automatically. Runtime
        # ownership covers both stamped and older unstamped managed installs.
        monkeypatch.setattr(
            "kiro_crew.platform.wheel_engine.running_from_managed_venv", lambda: True
        )

        apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", apply_called)
        wheel_apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_wheel_update", wheel_apply_called)

        with _install_effect("mandatory", "wheel"):
            await orch._check_for_updates()

        # Must NOT attempt the git apply on a non-git tree.
        apply_called.assert_not_awaited()
        # Must call the wheel auto-apply for mandatory updates.
        wheel_apply_called.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mandatory_wheel_no_newer_build_notifies(self, monkeypatch):
        """A wheel install below the floor but with NO newer build available
        must NOT apply — applying would reinstall the same below-floor version
        and execv-restart into the same state forever. It notifies instead."""
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": False,  # floor pinned above the latest build
                "can_apply": False,
                "managed_by": "kirocrew",
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "curl -fsSL … | sh",
                },
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")
        monkeypatch.setattr(
            "kiro_crew.platform.wheel_engine.running_from_managed_venv", lambda: True
        )

        wheel_apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", AsyncMock())
        monkeypatch.setattr(orch, "_auto_apply_wheel_update", wheel_apply_called)

        await orch._check_for_updates()

        # Must NOT apply (would loop); must notify via the dashboard badge.
        wheel_apply_called.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_mandatory_update_on_non_managed_installer_badges(self, monkeypatch, caplog):
        """An install with an installer command outside the managed venv must
        notify rather than run it, even when a floor mandates the update.

        A plain pip install (not pipx, not the managed venv) must be pointed at
        the in-place `pip install -U kirocrew` upgrade, NOT at `kirocrew update`
        — that command re-runs the installer and leaves a second copy."""
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": False,
                "can_apply": False,
                "managed_by": "kirocrew",
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "curl -fsSL … | sh",
                },
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")
        monkeypatch.setattr(
            "kiro_crew.platform.wheel_engine.running_from_managed_venv", lambda: False
        )
        # Not pipx either: the plain-pip shape, where the installer re-run is the bug.
        monkeypatch.setattr("kiro_crew.platform.wheel_engine.running_from_pipx", lambda: False)
        # The hint prefers the signed pinned wheel; this test exercises the
        # verification-FAILURE report, so make the manifest fetch raise cleanly.
        from kiro_crew.platform.wheel_engine import WheelUpdateError

        def _no_manifest(*a, **k):
            raise WheelUpdateError("no CDN in test")

        monkeypatch.setattr("kiro_crew.platform.wheel_engine.fetch_verified_manifest", _no_manifest)

        # The install-shape probe stats the filesystem, so it must run off the
        # event loop. Spy on asyncio.to_thread and record the functions it ran.
        offloaded: list[str] = []
        real_to_thread = asyncio.to_thread

        async def _spy_to_thread(func, /, *args, **kwargs):
            offloaded.append(getattr(func, "__name__", repr(func)))
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", _spy_to_thread)

        apply_called = AsyncMock()
        wheel_apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", apply_called)
        monkeypatch.setattr(orch, "_auto_apply_wheel_update", wheel_apply_called)

        with caplog.at_level(logging.WARNING):
            await orch._check_for_updates()

        apply_called.assert_not_awaited()
        wheel_apply_called.assert_not_awaited()
        ds.push_refresh.assert_called_once_with("update_available")
        # The dashboard badge reads _update_info["update_available"]; a mandatory
        # update must light it even though the check left it False.
        assert handlers._update_info.get("update_available") is True
        # The warning must give the verification-failure report (the signed
        # wheel could not be verified with no CDN in the test), which points at
        # the channel's artifact directory and emits NO name-resolving command.
        warning = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert "download.crew.kiro.dev/cli/" in warning
        assert "--extra-index-url" not in warning
        assert "/simple/ -U kirocrew" not in warning
        assert "-U kirocrew" not in warning
        assert "kirocrew restart" in warning
        assert "run `kirocrew update`" not in warning
        # The shape probes AND the channel-keyed hint build are dispatched to a
        # worker thread in one closure, not run on the event loop.
        assert "_resolve_hint" in offloaded

    @pytest.mark.asyncio
    async def test_floor_warning_redacts_cdn_userinfo_from_the_logged_hint(
        self, monkeypatch, caplog
    ):
        """A userinfo-bearing KIROCREW_CDN_BASE (user:pass@host) passes
        _SAFE_CDN_BASE_RE, so the plain-pip verification-failure report embeds it
        in its manual-install URL. This log line is a durable, dashboard-served
        surface, so the credential must be redacted from the LOGGED string —
        while the note the user is shown (via non_managed_pip_update_hint) still
        carries the real base."""
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov
        from kiro_crew.platform.update_layout import non_managed_pip_update_hint

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": False,
                "can_apply": False,
                "managed_by": "kirocrew",
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "curl -fsSL … | sh",
                },
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")
        monkeypatch.setattr(
            "kiro_crew.platform.wheel_engine.running_from_managed_venv", lambda: False
        )
        monkeypatch.setattr("kiro_crew.platform.wheel_engine.running_from_pipx", lambda: False)
        # The hint prefers the signed pinned wheel; with no CDN in the test it
        # takes the verification-FAILURE report, whose manual-install URL embeds
        # the CDN base — so make the manifest fetch raise cleanly.
        from kiro_crew.platform.wheel_engine import WheelUpdateError

        def _no_manifest(*a, **k):
            raise WheelUpdateError("no CDN in test")

        monkeypatch.setattr("kiro_crew.platform.wheel_engine.fetch_verified_manifest", _no_manifest)
        # A credential-bearing CDN base. _SAFE_CDN_BASE_RE admits userinfo, so
        # without redaction it would reach the dashboard-served log.
        monkeypatch.setenv("KIROCREW_CDN_BASE", "https://user:s3cr3t@cdn.example.invalid")

        monkeypatch.setattr(orch, "_auto_apply_update", AsyncMock())
        monkeypatch.setattr(orch, "_auto_apply_wheel_update", AsyncMock())

        with caplog.at_level(logging.WARNING):
            await orch._check_for_updates()

        warning = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        # The secret must not appear in the logged line.
        assert "s3cr3t" not in warning
        # The artifact URL (minus the credential) is still shown, so the operator
        # sees which install this is about.
        assert "cdn.example.invalid/cli/" in warning
        # The note the USER is shown is NOT redacted at the source — it is the
        # real, actionable guidance with the credential-bearing base intact; only
        # the LOGGED copy is redacted.
        upgrade, _restart = non_managed_pip_update_hint()
        assert upgrade.command is None
        assert "user:s3cr3t@cdn.example.invalid" in upgrade.note

    @pytest.mark.asyncio
    async def test_mandatory_update_on_pipx_keeps_the_update_pointer(self, monkeypatch, caplog):
        """A pipx install below the floor keeps the `kirocrew update` pointer —
        its installer re-run upgrades the pipx venv in place, so the pip hint
        does not apply."""
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": False,
                "can_apply": False,
                "managed_by": "kirocrew",
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "curl -fsSL … | sh",
                },
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")
        monkeypatch.setattr(
            "kiro_crew.platform.wheel_engine.running_from_managed_venv", lambda: False
        )
        monkeypatch.setattr("kiro_crew.platform.wheel_engine.running_from_pipx", lambda: True)

        monkeypatch.setattr(orch, "_auto_apply_update", AsyncMock())
        monkeypatch.setattr(orch, "_auto_apply_wheel_update", AsyncMock())

        with caplog.at_level(logging.WARNING):
            await orch._check_for_updates()

        ds.push_refresh.assert_called_once_with("update_available")
        assert handlers._update_info.get("update_available") is True
        warning = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert "run `kirocrew update`" in warning
        assert "--extra-index-url" not in warning
        assert "/simple/ -U kirocrew" not in warning

    @pytest.mark.asyncio
    async def test_mandatory_update_on_externally_managed_does_not_badge(self, monkeypatch):
        """A dmg/appimage/docker install below the floor has no `can_apply`
        AND no remediation command — it updates via its own surface, so
        the CLI 'run kirocrew update' badge must NOT light."""
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": False,
                "can_apply": False,
                "managed_by": "container",
                "remediation": None,  # externally managed: no CLI update path
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")
        apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", apply_called)

        await orch._check_for_updates()

        apply_called.assert_not_awaited()
        ds.push_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_mandatory_update_on_git_still_auto_applies(self, monkeypatch):
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        async def _noop_check():
            return None

        # Git checkout (`can_apply`) below the floor WITH a newer build available
        # (`version_newer`): the mandatory git apply runs. The no-newer-build git
        # case is test_mandatory_git_no_newer_build_notifies below.
        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": True,
                "can_apply": True,
                "managed_by": "git",
                "version_newer": True,
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")

        apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", apply_called)

        with _install_effect("mandatory"):
            await orch._check_for_updates()
        apply_called.assert_awaited_once()
        # Marked mandatory so a no-op apply is visible rather than silent.
        assert apply_called.await_args.kwargs.get("mandatory") is True

    @pytest.mark.asyncio
    async def test_mandatory_git_no_newer_build_notifies(self, monkeypatch):
        """A git checkout below the floor but with NO newer build must NOT reset.

        `_auto_apply_update` resets hard onto the upstream tip, and
        `update_available` is true for a checkout on commit distance alone. A
        floor-mandated apply that reset on that would drag the checkout to every
        intermediate commit on every cycle and at every boot without the running
        `__version__` ever advancing. The git branch reads `version_newer`, the
        same signal the voluntary branch does, so no newer build means notify.
        """
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": True,  # commit distance only — released or not
                "can_apply": True,
                "managed_by": "git",
                "version_newer": False,
                "check_status": "succeeded",
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")

        apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", apply_called)

        with _install_effect("mandatory"):
            await orch._check_for_updates()

        # Must NOT reset (version not newer → would churn); must notify via the
        # badge. The shared check cache is NOT clobbered: the verdict the check
        # wrote survives (mirroring the wheel branch, which only refreshes).
        apply_called.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")
        assert handlers._update_info.get("check_status") == "succeeded"

    @pytest.mark.asyncio
    async def test_mandatory_git_diverged_checkout_notifies_not_resets(self, monkeypatch):
        """A diverged below-floor checkout must notify, not hard-reset.

        A checkout that pulled a version bump and then committed on top reads
        `version_newer` true (upstream `__version__` outranks the imported one)
        but `update_available` false (`can_fast_forward or restart_pending` is
        false when the checkout is both ahead and behind). Resetting it would
        discard the local commits. Requiring `update_available` as well as
        `version_newer` keeps a diverged host on the notify path rather than
        entering `_auto_apply_update` and relying on its late ahead-count refusal.
        """
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": False,  # diverged: neither ff nor restart-pending
                "can_apply": True,
                "managed_by": "git",
                "version_newer": True,  # upstream version IS newer
                "commits_ahead": 2,
                "commits_behind": 3,
                "check_status": "succeeded",
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")

        apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", apply_called)

        with _install_effect("mandatory"):
            await orch._check_for_updates()

        # version_newer alone is not enough: a diverged checkout notifies.
        apply_called.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_mandatory_git_failed_check_distinct_from_no_newer_build(
        self, monkeypatch, caplog
    ):
        """A failed check and a no-newer-build verdict stay distinguishable.

        Both decline to reset, but a non-answer (the check could not run) must
        not read as a compliance decision, and neither path may clobber the
        shared cache with a fabricated `update_available=True`. The two produce
        distinct log lines and both leave the cache verdict the check wrote
        intact.
        """
        import logging

        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        async def _noop_check():
            return None

        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")

        # Case A: the check FAILED (non-answer). Log must say the check did not
        # succeed; cache verdict (failed, not available) must survive.
        orch_a = _make_orchestrator()
        orch_a.dashboard_state = _mock_dashboard_state()
        apply_a = AsyncMock()
        monkeypatch.setattr(orch_a, "_auto_apply_update", apply_a)
        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": False,
                "can_apply": True,
                "managed_by": "git",
                "version_newer": False,
                "check_status": "failed",
            }
        )
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            with _install_effect("mandatory"):
                await orch_a._check_for_updates()
            failed_logged = "did not succeed" in caplog.text
        failed_cache = dict(handlers._update_info)

        # Case B: the check SUCCEEDED but nothing newer. A different log line;
        # cache verdict (succeeded, not available) survives.
        orch_b = _make_orchestrator()
        orch_b.dashboard_state = _mock_dashboard_state()
        apply_b = AsyncMock()
        monkeypatch.setattr(orch_b, "_auto_apply_update", apply_b)
        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": True,  # commit distance only
                "can_apply": True,
                "managed_by": "git",
                "version_newer": False,
                "check_status": "succeeded",
            }
        )
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            with _install_effect("mandatory"):
                await orch_b._check_for_updates()
            no_newer_logged = "no newer build is cleanly applicable" in caplog.text
        succeeded_cache = dict(handlers._update_info)

        # Neither path resets.
        apply_a.assert_not_awaited()
        apply_b.assert_not_awaited()
        # The two log lines are distinct and each matches its case.
        assert failed_logged
        assert no_newer_logged
        # Neither path clobbers the shared cache's own verdict.
        assert failed_cache.get("check_status") == "failed"
        assert failed_cache.get("update_available") is False
        assert succeeded_cache.get("check_status") == "succeeded"
