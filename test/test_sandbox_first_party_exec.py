"""First-party fixed-argv carve-out in the no-backend fail-close branch.

``agent.sandbox_allow_unsandboxed_exec`` is one boolean that conflated two
decisions on a host with no sandbox backend: allowing Kiro Crew's OWN managed
MCP servers to spawn (argv fully derived inside this package) and unconfining
the ``mode="strict"`` hostile-input paths. The ``first_party_fixed_argv``
carve-out lets the first class proceed — unconfined but env-scrubbed, loudly
warned, and SEL-audited with the distinct ``unconfined`` outcome — while every
other combination keeps the fail-closed behavior byte-for-byte.

Matrix pinned here:

* no backend + flag + no floor  -> passthrough (env-scrubbed argv, SEL
  ``unconfined``, one-shot SECURITY warning);
* no backend + NO flag          -> still raises ``SandboxUnavailableError``;
* transient probe failure + flag -> raises (self-heals; must not buy a bypass);
* foreign macOS sandbox + flag   -> raises (host sandbox is fine; remedy is
  config, not bypass);
* governance ``sandbox.min_level`` floor + flag -> raises (the carve-out must
  not duck the floor);
* governance floor + flag + NON-first-party spawn -> raises, and the refusal
  names the policy rather than the flag the operator already set: the floor
  outranks ``sandbox_allow_unsandboxed_exec`` because ``config.json`` is not
  policy (issue #3162);
* no floor + flag                               -> unchanged passthrough (the
  population that relies on the opt-in keeps working byte-for-byte);
* ``sandbox_allow_unsandboxed_exec=true``       -> identical with or without
  the flag (the opt-in remains a strict superset);
* backend available + flag       -> normal sandbox wrap (flag is inert).
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sandbox as sandbox_mod
from kiro_crew.sandbox import SandboxUnavailableError, reset_backend, wrap_argv

_ARGV = ["/opt/kirocrew/bin/kirocrew", "mcp-core"]


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Reset cached backend, one-shot latches, and host-specific interference.

    Mirrors ``test_sandbox_argv.clean_backend``: neutralize the host's real
    kiro internal-sandbox setting (so the darwin delegation branch never
    preempts the code under test), clear the in-sandbox marker, and reset every
    per-process warning sentinel these tests assert on.
    """
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.delenv("KIROCREW_ALLOW_UNSANDBOXED", raising=False)
    monkeypatch.setattr(
        "kiro_crew.sandbox._KIRO_INTERNAL_SETTINGS_PATH",
        "/nonexistent/kirocrew-test/amazon-internal.json",
    )
    for func, attr in (
        (sandbox_mod._warn_first_party_unconfined_once, "_warned"),
        (sandbox_mod.wrap_argv, "_warned"),
        (sandbox_mod.wrap_argv, "_nested_passthrough_logged"),
    ):
        if hasattr(func, attr):
            delattr(func, attr)
    reset_backend()
    yield
    if hasattr(sandbox_mod._warn_first_party_unconfined_once, "_warned"):
        delattr(sandbox_mod._warn_first_party_unconfined_once, "_warned")
    reset_backend()


@pytest.fixture
def no_backend(monkeypatch):
    """Force the ``backend == "none"`` branch with the default fail-close config."""
    monkeypatch.setattr(sandbox_mod, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: False)


class TestCarveOutAllowedPath:
    def test_passthrough_ends_with_original_argv_and_no_cleanup(self, no_backend):
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            wrapped, cleanup = wrap_argv(_ARGV, mode="standard", first_party_fixed_argv=True)
        assert cleanup is None
        assert wrapped[-len(_ARGV):] == _ARGV

    def test_env_scrub_prefix_uses_trusted_env_binary(self, no_backend, monkeypatch):
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sentinel")
        # Pin the trusted-candidate list to a binary that exists on EVERY CI
        # platform (Windows has no /usr/bin/env), so the test exercises the
        # prefix mechanics — resolution from the trusted absolute list, never a
        # PATH lookup — rather than the host's filesystem layout. The
        # no-candidate Windows shape is covered by the test below.
        monkeypatch.setattr(sandbox_mod, "_ENV_BINARY_CANDIDATES", (sys.executable,))
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            wrapped, _ = wrap_argv(_ARGV, mode="standard", first_party_fixed_argv=True)
        assert wrapped[0] == sys.executable
        assert "AWS_SECRET_ACCESS_KEY" in wrapped[: -len(_ARGV)]
        assert wrapped[-len(_ARGV):] == _ARGV

    def test_no_trusted_env_binary_returns_plain_argv(self, no_backend, monkeypatch):
        """Windows shape: no ``env`` binary — the argv-level scrub is skipped.

        Safe only because every allowlisted caller routes through
        ``sandboxed_spawn_argv``, whose ``scrub_env`` drops a superset of these
        keys from the child environment; pinned so a refactor cannot turn the
        missing binary into a hard failure that re-bricks Windows.
        """
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sentinel")
        monkeypatch.setattr(sandbox_mod, "_ENV_BINARY_CANDIDATES", ())
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            wrapped, cleanup = wrap_argv(_ARGV, mode="standard", first_party_fixed_argv=True)
        assert wrapped == _ARGV
        assert cleanup is None

    def test_sel_unconfined_event_is_emitted(self, no_backend):
        sel_instance = MagicMock()
        with patch("kiro_crew.sel.sel", return_value=sel_instance):
            wrap_argv(_ARGV, mode="standard", first_party_fixed_argv=True)
        assert sel_instance.log_tool_invocation.call_count == 1
        kwargs = sel_instance.log_tool_invocation.call_args.kwargs
        # Deliberately a THIRD outcome: neither "denied" (nothing was refused)
        # nor the nested-passthrough "allowed" (nothing confines this spawn).
        assert kwargs["outcome"] == "unconfined"
        # Deliberately NOT critical: this event fires per managed probe per
        # discovery cycle on a backend-less host, and the critical path flushes
        # synchronously on the gateway event loop.
        assert not kwargs.get("critical")
        assert "first-party fixed argv" in kwargs["resources"]
        assert kwargs["tool_name"] == _ARGV[0]

    def test_sel_failure_is_log_and_proceed(self, no_backend):
        """Matches the mode="off" delegation precedent: audit hiccups must not
        brick built-in tooling on a host that has no safer alternative."""
        sel_instance = MagicMock()
        sel_instance.log_tool_invocation.side_effect = OSError("disk full")
        with patch("kiro_crew.sel.sel", return_value=sel_instance):
            wrapped, cleanup = wrap_argv(_ARGV, mode="standard", first_party_fixed_argv=True)
        assert wrapped[-len(_ARGV):] == _ARGV
        assert cleanup is None

    def test_security_warning_fires_exactly_once_per_process(self, no_backend, caplog):
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            with caplog.at_level(logging.WARNING, logger=sandbox_mod.logger.name):
                wrap_argv(_ARGV, mode="standard", first_party_fixed_argv=True)
                wrap_argv(_ARGV, mode="standard", first_party_fixed_argv=True)
        hits = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "first-party fixed-argv" in r.getMessage()
        ]
        assert len(hits) == 1, [r.getMessage() for r in hits]
        assert hits[0].getMessage().startswith("SECURITY:")


class TestCarveOutStillRaises:
    def test_no_flag_keeps_fail_close(self, no_backend):
        sel_instance = MagicMock()
        with patch("kiro_crew.sel.sel", return_value=sel_instance):
            with pytest.raises(SandboxUnavailableError) as exc_info:
                wrap_argv(_ARGV, mode="standard")
        assert exc_info.value.kind == "no_backend"
        # The denial audit is unchanged — and no "unconfined" event fires.
        outcomes = [
            c.kwargs["outcome"] for c in sel_instance.log_tool_invocation.call_args_list
        ]
        assert outcomes == ["denied"]

    def test_transient_probe_failure_raises_despite_flag(self, no_backend, monkeypatch):
        """Transient failures self-heal on the next spawn; they must not buy a
        bypass through the carve-out."""
        monkeypatch.setattr(
            sandbox_mod, "_last_unshare_failure", (True, "fork EAGAIN", "retry")
        )
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            with pytest.raises(SandboxUnavailableError) as exc_info:
                wrap_argv(_ARGV, mode="standard", first_party_fixed_argv=True)
        assert exc_info.value.kind == "transient"

    def test_foreign_sandbox_raises_despite_flag(self, no_backend, monkeypatch):
        """A foreign outer sandbox means the host's sandbox is FINE; the remedy
        is config-level, never an unconfined bypass."""
        monkeypatch.setattr(sandbox_mod, "_inside_macos_sandbox", lambda: True)
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            with pytest.raises(SandboxUnavailableError) as exc_info:
                wrap_argv(_ARGV, mode="standard", first_party_fixed_argv=True)
        assert exc_info.value.kind == "foreign_sandbox"

    def test_governance_floor_raises_despite_flag(self, no_backend, monkeypatch):
        """A governed host keeps fail-closing for first-party spawns too — the
        carve-out must not duck the ``sandbox.min_level`` floor.

        Patched at the SOURCE (``governance_profiles.governance_floor_ordinal``)
        so both consumers of the shared read — ``_clamp_sandbox_mode`` and
        ``_governance_sandbox_floor_active`` — see the same governed host.
        """
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_floor_ordinal",
            lambda scope, **kwargs: "strict",
        )
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            with pytest.raises(SandboxUnavailableError):
                wrap_argv(_ARGV, mode="standard", first_party_fixed_argv=True)


class TestGovernanceFloorOverridesTheOptIn:
    """A ``sandbox.min_level`` floor outranks ``sandbox_allow_unsandboxed_exec``.

    The floor already refused the *audited* first-party carve-out
    (``TestCarveOutStillRaises.test_governance_floor_raises_despite_flag``) while
    having no effect at all on the broad config opt-in, so pinning a floor
    weakened the constrained path and left the unconstrained one alone. These
    pin the corrected direction: on a governed host the config flag grants
    nothing, because ``config.json`` is not policy (issue #3162).
    """

    @pytest.fixture
    def governed(self, monkeypatch):
        """A host whose policy pins the floor, with the opt-in ALSO set.

        Patched at the SOURCE so both consumers of the shared read — the mode
        clamp and the floor gate — agree about this host, matching
        ``test_governance_floor_raises_despite_flag``.
        """
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda config_mode="auto": "none")
        monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: True)
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_floor_ordinal",
            lambda scope, **kwargs: "strict",
        )

    def test_governed_host_raises_even_though_the_opt_in_is_set(self, governed):
        """THE FIX. Before this, these exact conditions returned bare argv."""
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            with pytest.raises(SandboxUnavailableError) as exc_info:
                wrap_argv(_ARGV, mode="strict")
        assert exc_info.value.kind == "no_backend"

    def test_refusal_names_the_policy_not_the_missing_flag(self, governed):
        """The operator HAS set the flag, so the old wording would misdirect.

        ``allow_unsandboxed_exec is not set`` would send them to edit a config
        key that is deliberately powerless here.
        """
        sel_instance = MagicMock()
        with patch("kiro_crew.sel.sel", return_value=sel_instance):
            with pytest.raises(SandboxUnavailableError) as exc_info:
                wrap_argv(_ARGV, mode="strict")
        message = str(exc_info.value)
        assert "sandbox.min_level" in message
        assert "governance policy forbids" in message
        assert "allow_unsandboxed_exec is not set" not in message
        # The audit trail must be able to distinguish a policy refusal from a
        # plain missing-opt-in refusal.
        error = sel_instance.log_tool_invocation.call_args.kwargs["error"]
        assert "sandbox.min_level" in error
        assert "overridden" in error

    def test_ungoverned_host_with_the_opt_in_is_unchanged(self, monkeypatch):
        """REGRESSION GUARD. Only governed hosts change; this is the population
        that relies on the opt-in and must keep working byte-for-byte."""
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda config_mode="auto": "none")
        monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: True)
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            assert wrap_argv(_ARGV, mode="strict") == (_ARGV, None)

    def test_governed_host_without_the_opt_in_keeps_its_original_message(
        self, monkeypatch
    ):
        """A governed host that never set the flag is refused for the ordinary
        reason, so the policy wording must not leak into that case."""
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda config_mode="auto": "none")
        monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: False)
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_floor_ordinal",
            lambda scope, **kwargs: "strict",
        )
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            with pytest.raises(SandboxUnavailableError) as exc_info:
                wrap_argv(_ARGV, mode="strict")
        assert "allow_unsandboxed_exec is not set" in str(exc_info.value)

    def test_a_floor_of_the_loosest_tier_does_not_deny(self, monkeypatch):
        """REGRESSION GUARD. ``sandbox.min_level: off`` is a governed floor that
        requires NOTHING — the loosest tier of ("off", "standard", "cc", "strict").

        Testing the floor string for raw truthiness would read "no isolation
        required" as "isolation mandatory": it would deny a spawn the operator
        legitimately opted into, and tell them a floor of ``off`` forbids
        unsandboxed execution. This population must be byte-for-byte unchanged.
        """
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda config_mode="auto": "none")
        monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: True)
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_floor_ordinal",
            lambda scope, **kwargs: "off",
        )
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            assert wrap_argv(_ARGV, mode="standard") == (_ARGV, None)

    def test_every_tier_above_the_loosest_denies(self, monkeypatch):
        """The tiers that DO mandate isolation all refuse, so the fix cannot be
        satisfied by only the tier this change was written against."""
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda config_mode="auto": "none")
        monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: True)
        for tier in ("standard", "cc", "strict"):
            monkeypatch.setattr(
                "kiro_crew.platform.governance_profiles.governance_floor_ordinal",
                lambda scope, _tier=tier, **kwargs: _tier,
            )
            with patch("kiro_crew.sel.sel", return_value=MagicMock()):
                with pytest.raises(SandboxUnavailableError):
                    wrap_argv(_ARGV, mode="standard")

    def test_message_does_not_promise_that_built_in_spawns_keep_running(self, governed):
        """A governed host withholds the first-party carve-out as well, so the
        guidance must not tell an operator built-in tooling is unaffected."""
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            with pytest.raises(SandboxUnavailableError) as exc_info:
                wrap_argv(_ARGV, mode="strict")
        message = str(exc_info.value)
        assert "withholds the first-party carve-out" in message
        assert "runs no agent subprocess" in message


class TestFlagIsOtherwiseInert:
    def test_opt_in_behavior_is_identical_with_or_without_flag(self, monkeypatch):
        """``sandbox_allow_unsandboxed_exec=true`` stays a strict superset:
        byte-identical behavior for all callers, flag or no flag."""
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda config_mode="auto": "none")
        monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: True)
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            with_flag = wrap_argv(_ARGV, mode="standard", first_party_fixed_argv=True)
            without_flag = wrap_argv(_ARGV, mode="standard")
        assert with_flag == without_flag == (_ARGV, None)

    def test_backend_available_flag_is_inert(self, monkeypatch):
        stub = ["launcher", "/tmp/launcher.py", *_ARGV]
        monkeypatch.setattr(
            sandbox_mod, "detect_backend", lambda config_mode="auto": "namespace"
        )
        monkeypatch.setattr(
            sandbox_mod, "namespace_argv", lambda argv, level, **kwargs: list(stub)
        )
        with_flag = wrap_argv(_ARGV, mode="standard", first_party_fixed_argv=True)
        without_flag = wrap_argv(_ARGV, mode="standard")
        assert with_flag == without_flag == (stub, stub[1])


class TestChokepointThreading:
    def test_sandboxed_spawn_argv_threads_the_flag(self, monkeypatch):
        """The chokepoint must hand the flag to ``wrap_argv`` — a silently
        dropped kwarg would fail-close the allowlisted first-party sites."""
        seen: dict[str, bool] = {}

        def _record(argv, mode="auto", **kwargs):
            seen["flag"] = kwargs.get("first_party_fixed_argv", False)
            return list(argv), None

        monkeypatch.setattr(sandbox_mod, "wrap_argv", _record)
        monkeypatch.setattr(sandbox_mod, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(sandbox_mod, "cgroup_scope_bus_env", lambda env: (env, ()))
        sandbox_mod.sandboxed_spawn_argv(_ARGV, first_party_fixed_argv=True)
        assert seen["flag"] is True
        sandbox_mod.sandboxed_spawn_argv(_ARGV)
        assert seen["flag"] is False
