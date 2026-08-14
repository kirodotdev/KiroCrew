"""Tests for nested-sandbox passthrough tier detection (KIROCREW_SANDBOX_LEVEL).

The nested passthrough in ``wrap_argv`` cannot re-wrap (Linux seccomp denies
``unshare``; macOS Seatbelt refuses ``sandbox_apply`` with EPERM), so an
in-sandbox request for a stricter tier than the outer sandbox necessarily runs
at the outer tier. These tests pin the fix for that downgrade being invisible:
both launchers export the active tier, and the passthrough compares it against
the requested tier — auditing the pair in SEL, warning loudly on a proven
downgrade, and applying the stricter tier's env-scrub delta (the one enforceable
slice of the stricter tier).
"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

import kiro_crew.sandbox as sandbox_mod
from kiro_crew.sandbox import (
    _build_launcher_script,
    reset_backend,
    sandbox_exec_argv,
    wrap_argv,
)

# Shares the subprocess_spawn group with test_sandbox_argv.py: same module under
# test, same cached-backend globals. Requires --dist loadgroup.
pytestmark = pytest.mark.xdist_group(name="subprocess_spawn")


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Isolate marker/level env vars, cached backend, and one-shot log flags."""
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.delenv("KIROCREW_SANDBOX_LEVEL", raising=False)
    monkeypatch.setattr(
        "kiro_crew.sandbox._KIRO_INTERNAL_SETTINGS_PATH",
        "/nonexistent/kirocrew-test/amazon-internal.json",
    )
    # The passthrough info log is once-only per process; clear it so caplog
    # assertions in this module are order-independent, and restore the prior
    # state afterwards — the attribute is a module-level global shared with
    # test_sandbox_argv.py in the same xdist group.
    had_flag = getattr(sandbox_mod.wrap_argv, "_nested_passthrough_logged", None)
    if had_flag is not None:
        delattr(sandbox_mod.wrap_argv, "_nested_passthrough_logged")
    reset_backend()
    yield
    if had_flag is not None:
        sandbox_mod.wrap_argv._nested_passthrough_logged = had_flag
    elif hasattr(sandbox_mod.wrap_argv, "_nested_passthrough_logged"):
        delattr(sandbox_mod.wrap_argv, "_nested_passthrough_logged")
    reset_backend()


class TestLauncherExportsLevel:
    """Both launcher sites must record the tier beside the ACTIVE marker."""

    @pytest.mark.parametrize("level", ["standard", "cc", "strict"])
    def test_linux_launcher_templates_level_constant(self, level):
        script = _build_launcher_script(level)
        assert f'SANDBOX_LEVEL = "{level}"' in script
        # The export must live in the script body so the sandboxed tree
        # carries the tier at runtime, not just the template.
        assert 'os.environ["KIROCREW_SANDBOX_LEVEL"] = SANDBOX_LEVEL' in script

    def test_linux_launcher_level_export_after_scrub(self):
        # The level export must sit after the env-scrub loop (same guarantee
        # as the ACTIVE marker: a scrubbed prefix cannot delete it).
        script = _build_launcher_script("strict")
        scrub_pos = script.index("for key in list(os.environ):")
        level_pos = script.index('os.environ["KIROCREW_SANDBOX_LEVEL"]')
        assert level_pos > scrub_pos

    @pytest.mark.parametrize("level", ["standard", "cc", "strict"])
    def test_macos_env_prefix_carries_level(self, level, tmp_path, monkeypatch):
        monkeypatch.setattr(sandbox_mod, "_ensure_run_dir", lambda: str(tmp_path))
        wrapped, profile = sandbox_exec_argv(["echo", "hi"], level)
        try:
            assert wrapped[0] == "env"
            assert f"KIROCREW_SANDBOX_LEVEL={level}" in wrapped
            # Positioned after every -u flag so the scrub cannot drop it —
            # same non-droppable placement as the ACTIVE marker.
            last_u = max((i for i, a in enumerate(wrapped) if a == "-u"), default=-1)
            assert wrapped.index(f"KIROCREW_SANDBOX_LEVEL={level}") > last_u
            assert wrapped.index("KIROCREW_SANDBOX_ACTIVE=1") > last_u
        finally:
            if profile:
                os.unlink(profile)


class TestPassthroughTierComparison:
    """The passthrough compares requested vs active tier and audits the pair."""

    @patch("kiro_crew.sandbox.detect_backend")
    def test_downgrade_detected_audited_and_warned(self, mock_detect, monkeypatch, caplog):
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setenv("KIROCREW_SANDBOX_LEVEL", "standard")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: None)
        # Deterministic scrub delta: exactly one agent-denied key present.
        # Deletion is prefix-based to mirror _sandbox_env_unset_args's own
        # startswith matching — an ambient SLACK_BOT_TOKEN_OLD would otherwise
        # survive and add an extra -u pair.
        scrub_prefixes = list(sandbox_mod._AGENT_DENIED_ENV_KEYS) + list(
            sandbox_mod._SENSITIVE_ENV_PREFIXES
        )
        for prefix in scrub_prefixes:
            for key in list(os.environ):
                if key.startswith(prefix):
                    monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        argv = ["kiro-cli", "acp"]
        with patch("kiro_crew.sel.sel") as mock_sel:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
                result, cleanup = wrap_argv(argv, mode="strict")
        # The passthrough never fail-closes (refusing the downgrade breaks
        # in-sandbox callers that legitimately request a stricter tier), but
        # the stricter tier's env scrub is prefixed — through an ABSOLUTE
        # trusted env path, never a PATH lookup this environment controls.
        assert result[0] in sandbox_mod._ENV_BINARY_CANDIDATES
        assert result[1:] == ["-u", "SLACK_BOT_TOKEN", *argv]
        assert cleanup is None
        mock_detect.assert_not_called()
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["metadata"]["requested_tier"] == "strict"
        assert kwargs["metadata"]["active_tier"] == "standard"
        assert kwargs["metadata"]["tier_known"] is True
        assert kwargs["metadata"]["tier_downgrade"] is True
        assert any("SECURITY" in r.message and "downgrade" in r.message for r in caplog.records)

    @patch("kiro_crew.sandbox.detect_backend")
    def test_downgrade_scrub_skipped_without_trusted_env_binary(
        self, mock_detect, monkeypatch, caplog
    ):
        # No env binary at a trusted absolute path (Windows, exotic layouts):
        # the scrub cannot be applied safely, so the plain passthrough returns
        # with a loud warning — never a PATH-resolved "env", never fail-closed.
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setenv("KIROCREW_SANDBOX_LEVEL", "standard")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: None)
        monkeypatch.setattr(sandbox_mod, "_sandbox_env_unset_args", lambda *a, **k: ["-u", "X"])
        monkeypatch.setattr(sandbox_mod, "_unset_env_argv", lambda keys: None)
        argv = ["kiro-cli", "acp"]
        with patch("kiro_crew.sel.sel"):
            with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
                result, cleanup = wrap_argv(argv, mode="strict")
        assert result == argv
        assert cleanup is None
        assert any("no trusted env binary" in r.message for r in caplog.records)

    @patch("kiro_crew.sandbox.detect_backend")
    def test_downgrade_without_scrub_delta_returns_argv(self, mock_detect, monkeypatch):
        # No sensitive keys in the environment → the delta is empty and the
        # passthrough returns argv verbatim even on a downgrade.
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setenv("KIROCREW_SANDBOX_LEVEL", "standard")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: None)
        monkeypatch.setattr(sandbox_mod, "_sandbox_env_unset_args", lambda *a, **k: [])
        argv = ["kiro-cli", "acp"]
        with patch("kiro_crew.sel.sel"):
            result, cleanup = wrap_argv(argv, mode="strict")
        assert result == argv
        assert cleanup is None

    @patch("kiro_crew.sandbox.detect_backend")
    def test_no_downgrade_when_requesting_weaker_tier(self, mock_detect, monkeypatch, caplog):
        # standard requested under a strict outer sandbox is an UPGRADE of the
        # actual confinement — not a downgrade. No warning, verbatim argv.
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setenv("KIROCREW_SANDBOX_LEVEL", "strict")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: None)
        argv = ["git", "status"]
        with patch("kiro_crew.sel.sel") as mock_sel:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
                result, cleanup = wrap_argv(argv, mode="standard")
        assert result == argv
        assert cleanup is None
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["metadata"]["requested_tier"] == "standard"
        assert kwargs["metadata"]["active_tier"] == "strict"
        assert kwargs["metadata"]["tier_downgrade"] is False
        assert not any("SECURITY" in r.message for r in caplog.records)

    @patch("kiro_crew.sandbox.detect_backend")
    def test_same_tier_is_not_a_downgrade(self, mock_detect, monkeypatch):
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setenv("KIROCREW_SANDBOX_LEVEL", "strict")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: None)
        argv = ["kiro-cli", "acp"]
        with patch("kiro_crew.sel.sel") as mock_sel:
            result, _ = wrap_argv(argv, mode="strict")
        assert result == argv
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["metadata"]["tier_downgrade"] is False

    @patch("kiro_crew.sandbox.detect_backend")
    def test_unknown_active_tier_no_crash_no_downgrade_claim(
        self, mock_detect, monkeypatch, caplog
    ):
        # Marker set but level var absent: an outer sandbox built by an older
        # launcher. ``unknown`` has no ordinal, so no downgrade can be proven —
        # the passthrough is unaffected and must never crash.
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: None)
        argv = ["kiro-cli", "acp"]
        with patch("kiro_crew.sel.sel") as mock_sel:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
                result, cleanup = wrap_argv(argv, mode="strict")
        assert result == argv
        assert cleanup is None
        mock_detect.assert_not_called()
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["metadata"]["requested_tier"] == "strict"
        assert kwargs["metadata"]["active_tier"] == "unknown"
        assert kwargs["metadata"]["tier_known"] is False
        assert kwargs["metadata"]["tier_downgrade"] is False
        assert not any("SECURITY" in r.message for r in caplog.records)

    @patch("kiro_crew.sandbox.detect_backend")
    def test_unrecognized_level_value_treated_as_unknown(self, mock_detect, monkeypatch):
        # A garbage/forged value carries no ordinal claim either.
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setenv("KIROCREW_SANDBOX_LEVEL", "bogus-tier")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: None)
        with patch("kiro_crew.sel.sel") as mock_sel:
            result, _ = wrap_argv(["kiro-cli"], mode="strict")
        assert result == ["kiro-cli"]
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["metadata"]["active_tier"] == "unknown"
        assert kwargs["metadata"]["tier_known"] is False
        assert kwargs["metadata"]["tier_downgrade"] is False

    @patch("kiro_crew.sandbox.detect_backend")
    def test_downgrade_survives_sel_failure(self, mock_detect, monkeypatch, caplog):
        # SEL down must not brick the passthrough (existing guarantee), and the
        # downgrade warning + scrub delta must still apply — they do not depend
        # on the audit landing.
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setenv("KIROCREW_SANDBOX_LEVEL", "standard")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: None)
        monkeypatch.setattr(sandbox_mod, "_sandbox_env_unset_args", lambda *a, **k: ["-u", "X"])
        argv = ["kiro-cli", "acp"]
        with patch("kiro_crew.sel.sel", side_effect=OSError("SEL down")):
            with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
                result, cleanup = wrap_argv(argv, mode="strict")
        assert result[0] in sandbox_mod._ENV_BINARY_CANDIDATES
        assert result[1:] == ["-u", "X", *argv]
        assert cleanup is None
        assert any("downgrade" in r.message for r in caplog.records)


class TestModeToLevel:
    @pytest.mark.parametrize(
        "mode,expected",
        [
            ("strict", "strict"),
            ("cc", "cc"),
            ("standard", "standard"),
            ("auto", "standard"),
        ],
    )
    def test_mapping(self, mode, expected):
        assert sandbox_mod._mode_to_level(mode) == expected
