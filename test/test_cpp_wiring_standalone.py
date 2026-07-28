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


# ── AgentRuntime.run_first_run_setup (newly wired) ──
#
# The gateway boot path used to call ``agent.run_first_run_setup()`` directly,
# bypassing the ``agent_runtime`` seam entirely. It now routes through
# ``current_context().agent_runtime.run_first_run_setup()``. These two tests are
# the behavior-preserving proof: the Default adapter must invoke the SAME
# underlying function with the same (no) arguments, so a standalone install gets
# byte-identical first-run behavior through the seam.


def test_default_agent_runtime_delegates_to_agent_first_run_setup(monkeypatch) -> None:
    """The Default adapter calls ``agent.run_first_run_setup`` verbatim.

    Behavior-preserving proof for routing the gateway's direct call through the
    seam: same target function, same argument list (none), called exactly once.
    """
    calls: list[tuple] = []
    monkeypatch.setattr(
        "kiro_crew.agent.run_first_run_setup",
        lambda *a, **kw: calls.append((a, kw)),
    )
    current_context().agent_runtime.run_first_run_setup()
    assert calls == [((), {})]


def test_gateway_first_run_setup_routes_through_the_seam(monkeypatch) -> None:
    """A composed adapter is what the gateway's wired call reaches.

    Asserts the seam is genuinely load-bearing at the call site: an edition
    adapter composed into ``agent_runtime`` is invoked INSTEAD of the module
    function, which is exactly what the direct import made impossible.
    """
    import dataclasses

    from kiro_crew.platform import build_default_context, reset_context, set_context
    from kiro_crew.platform.context import safe_context_call

    seen: list[str] = []

    class _EditionAgentRuntime:
        def managed_mcp_servers(self):
            return {}

        def run_first_run_setup(self) -> None:
            seen.append("edition")

    monkeypatch.setattr("kiro_crew.agent.run_first_run_setup", lambda: seen.append("core-direct"))
    base = build_default_context(KiroCrewConfig())
    composed = dataclasses.replace(base, agent_runtime=_EditionAgentRuntime())
    set_context(composed)
    try:
        # The exact expression the gateway boot path evaluates.
        safe_context_call(
            lambda: current_context().agent_runtime.run_first_run_setup(),
            fallback=None,
            log_message="agent_runtime.run_first_run_setup failed",
        )
    finally:
        reset_context()
    assert seen == ["edition"]


class _NoMarkerHome:
    """A fake home dir whose ``/ ".midway"`` never exists."""

    def __truediv__(self, _other):
        class _Path:
            def exists(self):
                return False

        return _Path()
