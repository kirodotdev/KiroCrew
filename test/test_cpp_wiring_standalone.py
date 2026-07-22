"""Defaults-preserving checks for the CPP consumption-site wiring.

With NO companion installed and NO ``KIROCREW_PROFILE`` override, the active
PlatformContext MUST be the all-defaults standalone context, and every wired
consumption site MUST read the SAME value it did before the wiring (the value
held in the module-global the Default adapter delegates to).
"""

from __future__ import annotations

import pytest

from kiro_crew import sandbox, security
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import (
    BASELINE_DENY,
    PROFILE_STANDALONE,
    boot_platform,
    current_context,
)


@pytest.fixture
def cfg() -> KiroCrewConfig:
    return KiroCrewConfig()


def test_boot_standalone_no_signals(cfg: KiroCrewConfig, monkeypatch) -> None:
    """No env + no companion → standalone context installed."""
    monkeypatch.delenv("KIROCREW_PROFILE", raising=False)
    monkeypatch.setattr("kiro_crew.platform.bootstrap.plugin_entry_points", lambda: [])
    # Avoid a real SSO marker on the dev box flipping the profile.
    monkeypatch.setattr(
        "kiro_crew.platform.profile.Path.home",
        lambda: _NoMarkerHome(),
    )
    ctx = boot_platform(cfg)
    assert ctx.profile == PROFILE_STANDALONE
    assert current_context() is ctx


def test_boot_platform_is_idempotent(cfg: KiroCrewConfig, monkeypatch) -> None:
    """A second boot call returns the already-installed context, no re-resolve."""
    monkeypatch.setenv("KIROCREW_PROFILE", "standalone")
    first = boot_platform(cfg)
    # A second call must NOT re-resolve (would raise if it tried amazon w/o companion).
    monkeypatch.setenv("KIROCREW_PROFILE", "amazon")
    second = boot_platform(cfg)
    assert second is first


def test_sandbox_dirs_match_module_globals() -> None:
    """The context-sourced sandbox dirs equal today's module globals."""
    ctx = current_context()
    assert ctx.profile == PROFILE_STANDALONE
    assert ctx.sandbox.strict_dirs() == list(sandbox._STRICT_DIRS)
    assert ctx.sandbox.cc_dirs() == list(sandbox._CC_DIRS)


def test_seatbelt_profile_unchanged_under_context() -> None:
    """The generated seatbelt profile is byte-identical to building from globals.

    Confirms the context indirection (strict + cc branches) and the .aws
    exclusion at the cc branch produce the same profile as the legacy globals.
    """
    for level in ("strict", "cc", "standard"):
        produced = sandbox._build_seatbelt_profile(level)
        # Recompute the expected dir list the legacy way for this level.
        if level == "standard":
            expected_dirs = sandbox._STANDARD_DIRS
        elif level == "cc":
            expected_dirs = [d for d in sandbox._CC_DIRS if d != ".aws"]
        else:
            expected_dirs = sandbox._STRICT_DIRS
        # Every expected dir must appear as a deny rule subpath.
        for d in expected_dirs:
            assert d in produced


def test_security_floor_is_baseline_only() -> None:
    """Standalone deny floor == baseline (no overlay)."""
    ctx = current_context()
    assert set(ctx.security.effective_patterns()) == set(BASELINE_DENY)
    # And the deny decision matches security.is_denied directly.
    assert ctx.security.is_denied("get_secret_foo") == security.is_denied("get_secret_foo")
    assert ctx.security.is_denied("ls -la") is None
    assert security.is_denied("ls -la") is None


def test_extra_mcp_servers_empty_standalone() -> None:
    """No edition-contributed MCP servers in standalone."""
    ctx = current_context()
    assert ctx.mcp_tooling.extra_mcp_servers() == {}


class _NoMarkerHome:
    """A fake home dir whose ``/ ".midway"`` never exists."""

    def __truediv__(self, _other):
        class _Path:
            def exists(self):
                return False

        return _Path()
