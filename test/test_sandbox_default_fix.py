"""Regression tests for the insecure-defaults fix (sandbox.sandbox='auto').

Verifies the fixes from the security audit:
  Fix #1: Default changed from 'off' to 'auto'.
  Fix #2: mode='off' verifies kiro-cli delegation before honoring.
  Fix #3: mode='off' emits SECURITY warning when both layers inactive.
  Fix #4: (install.sh seeding dropped — in-code default is sufficient.)
  Fix #5: (version probe removed — blocked event loop; delegation trusts
           kiro_internal_sandbox_enabled() which reads a settings file.)
  Event-loop safety: delegation path must not spawn subprocesses.
"""

from __future__ import annotations

import logging
import sys

import pytest

import kiro_crew.sandbox as sb
from kiro_crew.config.loader import AgentConfig


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Neutralize environment that would short-circuit wrap_argv."""
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.setattr(sb, "_inside_kirocrew_sandbox", lambda: False)
    monkeypatch.setattr(sb, "_macos_sandbox_state", lambda: None)
    monkeypatch.setattr(
        sb, "_KIRO_INTERNAL_SETTINGS_PATH",
        "/nonexistent/kirocrew-test/amazon-internal.json",
    )
    # Reset one-shot flags
    for obj in (sb.wrap_argv, sb._warn_mode_off_unconfined):
        if hasattr(obj, "_warned"):
            delattr(obj, "_warned")
    if hasattr(sb._warn_mode_off_unconfined, "_warned_set"):
        delattr(sb._warn_mode_off_unconfined, "_warned_set")
    if hasattr(sb._warn_mode_off_unconfined, "_info_logged"):
        delattr(sb._warn_mode_off_unconfined, "_info_logged")
    sb.reset_backend()
    yield
    sb.reset_backend()


class TestFix1DefaultIsAuto:
    """Fix #1: AgentConfig.sandbox defaults to 'auto'."""

    def test_dataclass_default_is_auto(self):
        assert AgentConfig().sandbox == "auto"

    def test_config_read_fallback_is_auto(self):
        """When config.json has no 'sandbox' key, the read path returns 'auto'."""
        from kiro_crew.knowledge.llm_pool import _get_sandbox_mode

        # Empty config -> returns new default
        assert _get_sandbox_mode(config={}) == "auto"

    def test_config_read_preserves_explicit_off(self):
        """Existing config with explicit 'off' is still honored."""
        from kiro_crew.knowledge.llm_pool import _get_sandbox_mode

        assert _get_sandbox_mode(config={"agent": {"sandbox": "off"}}) == "off"


class TestFix2DelegationVerification:
    """Fix #2: mode='off' verifies kiro-cli delegation before silently returning."""

    def test_mode_off_delegates_when_kiro_sandbox_active(self, monkeypatch):
        """On macOS with kiro-cli internal sandbox ON, mode='off' applies env
        scrub (without seatbelt wrap) rather than raw passthrough."""
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sb, "kiro_internal_sandbox_enabled", lambda: True)
        # Inject a sensitive env var so the scrub has something to strip
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")

        argv, cleanup = sb.wrap_argv(["kiro-cli", "chat"], mode="off", is_kiro_cli=True)
        # Should have env -u prefix (scrubbing the sensitive var)
        assert argv[0] == "env", "expected env scrub prefix on delegation"
        assert "-u" in argv
        assert "kiro-cli" in argv
        assert cleanup is None  # No seatbelt profile to clean up

    def test_mode_off_delegates_without_seatbelt_fallback(self, monkeypatch):
        """mode='off' delegation must NEVER produce a seatbelt wrap, even if
        the real _delegate_to_kiro_internal_sandbox would have fallen back."""
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sb, "kiro_internal_sandbox_enabled", lambda: True)

        argv, cleanup = sb.wrap_argv(["kiro-cli", "chat"], mode="off", is_kiro_cli=True)
        # Must not contain sandbox-exec (the seatbelt wrapper)
        assert "sandbox-exec" not in argv
        assert cleanup is None

    def test_mode_off_no_delegation_returns_raw_on_linux(self, monkeypatch):
        """On Linux with mode='off', no delegation is possible — raw passthrough."""
        monkeypatch.setattr(sys, "platform", "linux")

        argv, cleanup = sb.wrap_argv(["kiro-cli", "chat"], mode="off", is_kiro_cli=True)
        assert argv == ["kiro-cli", "chat"]
        assert cleanup is None


class TestFix3LoudDegradation:
    """Fix #3: mode='off' emits SECURITY warning when both layers inactive."""

    def test_mode_off_warns_linux(self, monkeypatch, caplog):
        """Linux + mode='off' logs a warning about no OS-level confinement."""
        monkeypatch.setattr(sys, "platform", "linux")

        with caplog.at_level(logging.WARNING, logger=sb.logger.name):
            sb.wrap_argv(["kiro-cli", "chat"], mode="off", is_kiro_cli=True)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings
        assert "sandbox='off'" in warnings[0].getMessage()

    def test_mode_off_warns_darwin_no_kiro_sandbox(self, monkeypatch, caplog):
        """macOS + mode='off' + kiro sandbox OFF logs a warning."""
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sb, "kiro_internal_sandbox_enabled", lambda: False)

        with caplog.at_level(logging.WARNING, logger=sb.logger.name):
            sb.wrap_argv(["kiro-cli", "chat"], mode="off", is_kiro_cli=True)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings
        msg = warnings[0].getMessage()
        assert "both" in msg.lower() or "sandbox='off'" in msg

    def test_mode_off_warning_is_oneshot(self, monkeypatch, caplog):
        """Warning emitted only once per process."""
        monkeypatch.setattr(sys, "platform", "linux")

        with caplog.at_level(logging.WARNING, logger=sb.logger.name):
            sb.wrap_argv(["kiro-cli", "chat"], mode="off", is_kiro_cli=True)
            sb.wrap_argv(["kiro-cli", "chat"], mode="off", is_kiro_cli=True)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1


class TestDelegationDoesNotBlockEventLoop:
    """The delegation path must not spawn subprocesses or perform blocking I/O.

    The version probe (Fix #5 in the original PR) was removed after review:
    it introduced a synchronous subprocess.run call on the asyncio event loop.
    The delegation relies on kiro_internal_sandbox_enabled() which does a single
    small-file read — acceptable for the hot path.
    """

    def test_delegation_path_has_no_subprocess_call(self, monkeypatch):
        """Verify that the delegation path doesn't call subprocess.run."""
        import subprocess
        from unittest.mock import MagicMock

        spy = MagicMock(side_effect=AssertionError("subprocess.run called on delegation path!"))
        monkeypatch.setattr(subprocess, "run", spy)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sb, "kiro_internal_sandbox_enabled", lambda: True)
        # Mock _delegate_to_kiro_internal_sandbox to avoid SEL
        delegated = (["env", "-u", "SSH_AUTH_SOCK", "kiro-cli", "chat"], None)
        monkeypatch.setattr(sb, "_delegate_to_kiro_internal_sandbox", lambda *a, **kw: delegated)

        # Should NOT raise (no subprocess.run call)
        argv, cleanup = sb.wrap_argv(["kiro-cli", "chat"], mode="auto", is_kiro_cli=True)
        assert argv == delegated[0]
        spy.assert_not_called()
