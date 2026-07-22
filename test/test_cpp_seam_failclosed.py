"""Fail-closed + behavioral-identity tests for the wave-3 CPP seams.

Covers the security-critical invariants the wave-3 wiring introduced (the
JailProvider gate, the lazy-fallback ``build_provider_factory``, and the
``safe_context_call`` / ``async_safe_context_call`` fail-closed contract).  Each
test proves the invariant the project marks HIGH-severity: a composition failure
propagates (never silently degrades to open-source defaults), while a transient
adapter error degrades — and the public Default seams stay behaviorally
identical to the pre-wiring direct calls.

The core never imports a companion: the overlay adapters below are inline test
doubles, exactly as ``test_cpp_wiring_amazon`` does.
"""

from __future__ import annotations

import dataclasses
from typing import Any, List, Optional

import pytest

from kiro_crew import cli
from kiro_crew.config.loader import KiroCrewConfig, build_provider_factory
from kiro_crew.platform import (
    PROFILE_ENTERPRISE,
    PlatformCompositionError,
    async_safe_context_call,
    build_default_context,
    safe_context_call,
    set_context,
)


@pytest.fixture
def cfg() -> KiroCrewConfig:
    return KiroCrewConfig()


# Context isolation (reset before+after every test, KIROCREW_PROFILE=standalone)
# is provided by conftest.py's autouse ``_reset_platform_context`` fixture.


@pytest.fixture(autouse=True)
def _jail_env_isolation(monkeypatch):
    """Clear the jail env markers so a prior test (or the ambient shell) cannot
    flip the re-entry guard / off-switch for this one."""
    monkeypatch.delenv("KIROCREW_JAILED", raising=False)
    monkeypatch.delenv("KIROCREW_NO_JAIL", raising=False)


@pytest.fixture
def stub_child_argv(monkeypatch):
    """Pin ``_child_argv`` to a fixed list so a gate test exercises the on-mode
    floor / re-exec wiring, not live ``sys.argv`` / binary resolution.  Opt-in
    (the ``_child_argv`` tests need the REAL implementation)."""
    monkeypatch.setattr(cli, "_child_argv", lambda: ["kirocrew", "chat"])
    return ["kirocrew", "chat"]


# ── inline jail backends (test doubles, not in the core) ──


class _NoJail:
    """Mirror of DefaultJailProvider: no backend."""

    def available(self) -> bool:
        return False

    def status_detail(self) -> str:
        return "no jail provider (test)"

    def maybe_reexec_into_jail(self, argv: List[str], mode: str) -> Optional[int]:
        return None


class _RealJailReturns:
    """A backend that re-execs and returns the child's exit code."""

    def __init__(self, rc: int) -> None:
        self._rc = rc
        self.calls: List[tuple] = []

    def available(self) -> bool:
        return True

    def status_detail(self) -> str:
        return "real jail (test)"

    def maybe_reexec_into_jail(self, argv: List[str], mode: str) -> Optional[int]:
        self.calls.append((tuple(argv), mode))
        return self._rc


class _RealJailReturnsNone:
    """A backend present but that returns None (could not establish isolation)."""

    def available(self) -> bool:
        return True

    def status_detail(self) -> str:
        return "real jail, no-op (test)"

    def maybe_reexec_into_jail(self, argv: List[str], mode: str) -> Optional[int]:
        return None


class _RealJailRaises:
    """A backend present but whose re-exec attempt errors."""

    def available(self) -> bool:
        return True

    def status_detail(self) -> str:
        return "real jail, broken (test)"

    def maybe_reexec_into_jail(self, argv: List[str], mode: str) -> Optional[int]:
        raise RuntimeError("jail backend exploded")


class _FlakyAvailable:
    """A backend present but whose availability PROBE raises a transient error.

    The gate must distinguish 'no backend' (available() returns False cleanly)
    from 'availability unknown' (the probe raised) — under mode='on' the latter
    must fail closed, not degrade to a no-op.
    """

    def available(self) -> bool:
        raise RuntimeError("availability probe exploded")

    def status_detail(self) -> str:
        return "real jail, flaky probe (test)"

    def maybe_reexec_into_jail(self, argv: List[str], mode: str) -> Optional[int]:
        return 0


class _CompositionErrorJail:
    """A backend whose re-exec raises PlatformCompositionError — MUST propagate."""

    def available(self) -> bool:
        return True

    def status_detail(self) -> str:
        return "real jail (test)"

    def maybe_reexec_into_jail(self, argv: List[str], mode: str) -> Optional[int]:
        raise PlatformCompositionError("companion jail backend could not compose")


def _install_jail(cfg: KiroCrewConfig, jail: Any, *, jail_mode: str = "auto") -> None:
    """Compose + install a context whose JailProvider is *jail* and agent.jail=*jail_mode*.

    Deep-copies the nested ``agent`` dataclass (``dataclasses.replace`` is a
    SHALLOW copy) so mutating ``jail`` here never writes through to the shared
    fixture object — keeps the helper safe even if ``cfg`` is reused or rescoped.
    """
    cfg = dataclasses.replace(cfg, agent=dataclasses.replace(cfg.agent, jail=jail_mode))
    base = build_default_context(cfg, profile=PROFILE_ENTERPRISE)
    set_context(dataclasses.replace(base, jail=jail))


# ── R1: public Default + jail='on' must be a NO-OP, not a brick ──


def test_public_default_jail_on_runs_in_process(cfg: KiroCrewConfig) -> None:
    """available()==False + mode='on' → gate returns (no sys.exit), runs in-process.

    This is the rank-1 blocking regression: the DefaultJailProvider returns None,
    and the on-mode fail-closed floor must NOT fire when there is no backend.
    """
    _install_jail(cfg, _NoJail(), jail_mode="on")
    # No SystemExit: the gate must fall through.
    cli._jail_reexec_gate("chat", no_jail_flag=False)


def test_public_default_jail_auto_runs_in_process(cfg: KiroCrewConfig) -> None:
    _install_jail(cfg, _NoJail(), jail_mode="auto")
    cli._jail_reexec_gate("run", no_jail_flag=False)


# ── R1/R8: a present backend under mode='on' must fail closed ──


def test_backend_returns_none_on_mode_fails_closed(cfg: KiroCrewConfig) -> None:
    """available()==True + None return + mode='on' → SystemExit(2)."""
    _install_jail(cfg, _RealJailReturnsNone(), jail_mode="on")
    with pytest.raises(SystemExit) as ei:
        cli._jail_reexec_gate("chat", no_jail_flag=False)
    assert ei.value.code == 2


def test_backend_raises_on_mode_fails_closed(cfg: KiroCrewConfig) -> None:
    """available()==True + backend raises + mode='on' → SystemExit(2) (same as None)."""
    _install_jail(cfg, _RealJailRaises(), jail_mode="on")
    with pytest.raises(SystemExit) as ei:
        cli._jail_reexec_gate("eval", no_jail_flag=False)
    assert ei.value.code == 2


def test_backend_raises_auto_mode_degrades(cfg: KiroCrewConfig) -> None:
    """available()==True + backend raises + mode='auto' → run in-process (no exit)."""
    _install_jail(cfg, _RealJailRaises(), jail_mode="auto")
    cli._jail_reexec_gate("chat", no_jail_flag=False)  # no SystemExit


def test_backend_returns_none_auto_mode_degrades(cfg: KiroCrewConfig) -> None:
    _install_jail(cfg, _RealJailReturnsNone(), jail_mode="auto")
    cli._jail_reexec_gate("chat", no_jail_flag=False)  # no SystemExit


# ── availability-probe failure: fail closed under on, degrade under auto ──


def test_flaky_available_on_mode_fails_closed(cfg: KiroCrewConfig) -> None:
    """available() raises a transient error + mode='on' → SystemExit(2).

    'availability unknown' must NOT be conflated with 'no backend' under on-mode:
    a flaky presence probe cannot silently downgrade an on-mode host to un-jailed.
    """
    _install_jail(cfg, _FlakyAvailable(), jail_mode="on")
    with pytest.raises(SystemExit) as ei:
        cli._jail_reexec_gate("chat", no_jail_flag=False)
    assert ei.value.code == 2


def test_flaky_available_auto_mode_degrades(cfg: KiroCrewConfig) -> None:
    """available() raises + mode='auto' → run in-process (no exit)."""
    _install_jail(cfg, _FlakyAvailable(), jail_mode="auto")
    cli._jail_reexec_gate("chat", no_jail_flag=False)  # no SystemExit


# ── PlatformCompositionError MUST propagate through the gate (fail-closed) ──


def test_composition_error_propagates_through_gate(cfg: KiroCrewConfig, monkeypatch) -> None:
    """A PlatformCompositionError from maybe_reexec_into_jail is never swallowed."""
    _install_jail(cfg, _CompositionErrorJail(), jail_mode="on")
    monkeypatch.setattr(cli, "_child_argv", lambda: ["kirocrew", "chat"])
    with pytest.raises(PlatformCompositionError):
        cli._jail_reexec_gate("chat", no_jail_flag=False)


# ── successful re-exec propagates the child's exit code ──


def test_successful_reexec_propagates_rc(cfg: KiroCrewConfig, monkeypatch) -> None:
    jail = _RealJailReturns(7)
    _install_jail(cfg, jail, jail_mode="on")
    monkeypatch.setattr(cli, "_child_argv", lambda: ["kirocrew", "chat"])
    with pytest.raises(SystemExit) as ei:
        cli._jail_reexec_gate("chat", no_jail_flag=False)
    assert ei.value.code == 7
    assert jail.calls and jail.calls[0][1] == "on"
    # The gate must pass the _child_argv() result through to the backend verbatim.
    assert jail.calls[0][0] == ("kirocrew", "chat")


# ── R2: --no-jail and KIROCREW_NO_JAIL both force off (skip a present backend) ──


def test_no_jail_flag_skips_present_backend(cfg: KiroCrewConfig) -> None:
    jail = _RealJailReturns(0)
    _install_jail(cfg, jail, jail_mode="on")
    cli._jail_reexec_gate("chat", no_jail_flag=True)  # no SystemExit
    assert jail.calls == []  # backend never invoked


def test_env_no_jail_skips_present_backend(cfg: KiroCrewConfig, monkeypatch) -> None:
    """KIROCREW_NO_JAIL=1 is the documented env-only bypass and IS read at the gate."""
    jail = _RealJailReturns(0)
    _install_jail(cfg, jail, jail_mode="on")
    monkeypatch.setenv("KIROCREW_NO_JAIL", "1")
    cli._jail_reexec_gate("chat", no_jail_flag=False)  # no SystemExit
    assert jail.calls == []


@pytest.mark.parametrize("falsey", ["0", "false", "no", "", " "])
def test_env_no_jail_falsey_does_not_disable(cfg: KiroCrewConfig, monkeypatch, falsey) -> None:
    """KIROCREW_NO_JAIL=0/false/no must NOT disable the jail (truthy-set, not bool()).

    A bare ``bool(os.environ.get(...))`` would treat '0' as truthy and silently
    bypass isolation — the regression this guards against.  With jail='on' and a
    present backend returning None, a non-disabling value must hit the fail-closed
    floor (exit 2), proving the env value did NOT force mode=off.
    """
    _install_jail(cfg, _RealJailReturnsNone(), jail_mode="on")
    monkeypatch.setenv("KIROCREW_NO_JAIL", falsey)
    with pytest.raises(SystemExit) as ei:
        cli._jail_reexec_gate("chat", no_jail_flag=False)
    assert ei.value.code == 2


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "On", "YES", " on ", "yes"])
def test_env_no_jail_truthy_variants_disable(cfg: KiroCrewConfig, monkeypatch, truthy) -> None:
    """Case/space-insensitive truthy values DO disable the jail (the .strip().lower() path)."""
    jail = _RealJailReturns(0)
    _install_jail(cfg, jail, jail_mode="on")
    monkeypatch.setenv("KIROCREW_NO_JAIL", truthy)
    cli._jail_reexec_gate("chat", no_jail_flag=False)  # no SystemExit
    assert jail.calls == []  # backend never invoked


# ── off-mode + re-entry guard + normalize ──


def test_off_mode_skips_present_backend(cfg: KiroCrewConfig) -> None:
    """A persisted agent.jail='off' returns before the probe even with a backend."""
    jail = _RealJailReturns(0)
    _install_jail(cfg, jail, jail_mode="off")
    cli._jail_reexec_gate("chat", no_jail_flag=False)  # no SystemExit
    assert jail.calls == []  # never probed/jailed


def test_garbage_mode_normalizes_to_auto(cfg: KiroCrewConfig) -> None:
    """An off-spec persisted mode is re-normalized to 'auto' at the gate (deny-by-default).

    Non-vacuous: the backend records the mode it was handed.  If the gate passed
    'garbage' through raw, the on-mode floor logic would not apply — but we assert
    the backend SAW exactly 'auto', proving the gate normalized it (and a rc=None
    degrades rather than fail-closing, unlike 'on')."""
    jail = _RealJailReturns(0)
    _install_jail(cfg, jail, jail_mode="garbage")
    with pytest.raises(SystemExit) as ei:  # rc=0 re-exec → propagate child rc
        cli._jail_reexec_gate("chat", no_jail_flag=False)
    assert ei.value.code == 0
    assert jail.calls and jail.calls[0][1] == "auto"  # normalized, not raw 'garbage'


@pytest.mark.parametrize("marker", ["1", "jailed", "ns-42", "true", "0", "false"])
def test_reentry_marker_short_circuits(cfg: KiroCrewConfig, monkeypatch, marker) -> None:
    """KIROCREW_JAILED PRESENT (any non-empty value) → gate returns without re-jailing,
    even under jail='on' with a None-returning backend (otherwise the child deadlocks).

    Parametrized across truthy AND non-truthy values: re-entry is detected by
    PRESENCE, so a companion's descriptive marker ('jailed', a namespace id) — or
    even '0'/'false' — must still short-circuit, matching the JailProvider
    re-entry contract.  (A truthiness check would wrongly re-probe on '0'/'jailed'
    and deadlock the on-mode child.)"""
    jail = _RealJailReturns(0)  # would re-exec if reached; tracks .calls
    _install_jail(cfg, jail, jail_mode="on")
    monkeypatch.setenv(cli._JAILED_ENV_MARKER, marker)
    cli._jail_reexec_gate("chat", no_jail_flag=False)  # no SystemExit, no deadlock
    assert jail.calls == []  # never re-probed/re-jailed the child


def test_empty_marker_does_not_short_circuit(cfg: KiroCrewConfig, monkeypatch) -> None:
    """An EMPTY KIROCREW_JAILED is NOT 'already jailed' (presence = non-empty), so the
    gate still runs — proving the presence check ignores a blank value."""
    jail = _RealJailReturns(5)
    _install_jail(cfg, jail, jail_mode="on")
    monkeypatch.setenv(cli._JAILED_ENV_MARKER, "")
    with pytest.raises(SystemExit) as ei:
        cli._jail_reexec_gate("chat", no_jail_flag=False)
    assert ei.value.code == 5  # gate ran → backend re-exec'd
    assert jail.calls  # backend WAS invoked


def test_marker_restored_after_degrade(cfg: KiroCrewConfig, monkeypatch) -> None:
    """try/finally invariant: the gate must NOT leak KIROCREW_JAILED after the backend
    RETURNS (no re-exec).  A prior value is restored; an unset stays unset."""
    import os

    # (a) prior unset → still unset after a degrade path.
    monkeypatch.delenv(cli._JAILED_ENV_MARKER, raising=False)
    _install_jail(cfg, _RealJailReturnsNone(), jail_mode="auto")
    cli._jail_reexec_gate("chat", no_jail_flag=False)
    assert cli._JAILED_ENV_MARKER not in os.environ

    # (b) prior EMPTY-STRING value preserved across a degrade path.  An empty
    # value bypasses the re-entry guard (_already_jailed() strips → False), so
    # the gate actually reaches the set-marker + try/finally; but os.environ.get
    # returns "" (not None), so this exercises the finally's restore-prior `else`
    # branch (NOT the pop branch).  A non-empty prior would short-circuit at the
    # re-entry guard and never touch the env var — a vacuous assertion.
    monkeypatch.setenv(cli._JAILED_ENV_MARKER, "")
    _install_jail(cfg, _RealJailReturnsNone(), jail_mode="auto")
    cli._jail_reexec_gate("chat", no_jail_flag=False)
    assert os.environ.get(cli._JAILED_ENV_MARKER) == ""


def test_marker_restored_before_on_mode_exit(cfg: KiroCrewConfig, monkeypatch) -> None:
    """finally runs before the on-mode sys.exit(2), so the marker is restored even on
    the fail-closed path (no leak into a parent that catches SystemExit)."""
    import os

    monkeypatch.delenv(cli._JAILED_ENV_MARKER, raising=False)
    _install_jail(cfg, _RealJailReturnsNone(), jail_mode="on")
    with pytest.raises(SystemExit):
        cli._jail_reexec_gate("chat", no_jail_flag=False)
    assert cli._JAILED_ENV_MARKER not in os.environ  # restored (was unset) despite exit


def test_normalize_jail_unit() -> None:
    """_normalize_jail: valid modes pass through; anything else → 'auto' (deny-by-default)."""
    from kiro_crew.config.loader import _normalize_jail

    assert _normalize_jail("on") == "on"
    assert _normalize_jail("off") == "off"
    assert _normalize_jail("auto") == "auto"
    assert _normalize_jail("On") == "auto"  # case-sensitive: not a valid literal
    assert _normalize_jail("garbage") == "auto"
    assert _normalize_jail(123) == "auto"
    assert _normalize_jail(None) == "auto"


# ── R3: the jailed command set covers all in-process agent commands ──


def test_jailed_commands_cover_agent_bearing_set() -> None:
    """consolidate + eval (both build a provider factory) are jailed too."""
    assert {"chat", "tui", "run", "consolidate", "eval"} <= cli._JAILED_COMMANDS
    assert "gateway" not in cli._JAILED_COMMANDS  # excluded (execv self-update)


# ── R5: _child_argv reuses _resolve_kirocrew_bin incl. the sentinel branch ──


def test_child_argv_sentinel_falls_back_to_module(monkeypatch) -> None:
    """When _resolve_kirocrew_bin returns the bare 'kirocrew' sentinel (no usable
    binary), _child_argv falls back to ``python -m kiro_crew`` with sys.argv[1:]."""
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_resolve_kirocrew_bin", lambda: "kirocrew")
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "chat", "--model", "x"])
    assert cli._child_argv() == [cli.sys.executable, "-m", "kiro_crew", "chat", "--model", "x"]


def test_child_argv_resolved_path_used(monkeypatch) -> None:
    """A resolved absolute path is used verbatim as argv[0], with sys.argv[1:]."""
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_resolve_kirocrew_bin", lambda: "/opt/venv/bin/kirocrew")
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "run", "TASK.md"])
    assert cli._child_argv() == ["/opt/venv/bin/kirocrew", "run", "TASK.md"]


# ── R6/R3: --no-jail argparse plumbing (parent-parser + SUPPRESS, both orderings) ──


@pytest.mark.parametrize(
    "argv",
    [
        ["--no-jail", "chat"],
        ["chat", "--no-jail"],
        ["--no-jail", "run", "T.md"],
        ["run", "T.md", "--no-jail"],
        ["--no-jail", "consolidate"],
        ["consolidate", "--no-jail"],
        ["--no-jail", "eval"],
        ["eval", "--no-jail"],
    ],
)
def test_no_jail_flag_accepted_in_both_orderings(monkeypatch, argv) -> None:
    """`kirocrew --no-jail <cmd>` AND `kirocrew <cmd> --no-jail` both set no_jail=True
    for every jailed command, and the gate is invoked with that flag.

    Drives the REAL ``cli.main`` parser (so it guards the parent-parser +
    default=argparse.SUPPRESS technique end-to-end), intercepting at the jail gate
    so nothing downstream actually runs.
    """
    captured = {}

    def _fake_gate(command, no_jail_flag):
        captured["command"] = command
        captured["no_jail"] = no_jail_flag
        raise SystemExit(0)  # stop main() right after the gate

    monkeypatch.setattr(cli, "boot_platform", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_jail_reexec_gate", _fake_gate)
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", *argv])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 0
    assert captured.get("no_jail") is True


def test_no_jail_default_false_when_absent(monkeypatch) -> None:
    """Without --no-jail, the gate is invoked with no_jail_flag=False (SUPPRESS does
    not leave the attribute unset/true)."""
    captured = {}

    def _fake_gate(command, no_jail_flag):
        captured["no_jail"] = no_jail_flag
        raise SystemExit(0)

    monkeypatch.setattr(cli, "boot_platform", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_jail_reexec_gate", _fake_gate)
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "chat"])
    with pytest.raises(SystemExit):
        cli.main()
    assert captured.get("no_jail") is False


# ── R7: build_provider_factory degrade + lazy fallback ──


class _RaisingProviders:
    """A ProviderRegistry whose create_factory raises a transient (non-composition) error."""

    def create_factory(self, cfg: Any):
        raise RuntimeError("transient registry failure")

    def register_acp_backends(self) -> None:
        return None


class _CompositionErrorProviders:
    def create_factory(self, cfg: Any):
        raise PlatformCompositionError("companion missing")

    def register_acp_backends(self) -> None:
        return None


def test_build_provider_factory_degrades_to_public(cfg: KiroCrewConfig) -> None:
    """A transient providers.create_factory error degrades to cfg.create_provider_factory(),
    and the LAZY fallback_factory is invoked exactly once (no eager double-build)."""
    sentinel = object()
    calls = {"n": 0}

    def _fake_create():
        calls["n"] += 1
        return sentinel

    cfg.create_provider_factory = _fake_create  # type: ignore[assignment,method-assign]
    base = build_default_context(cfg, profile=PROFILE_ENTERPRISE)
    set_context(dataclasses.replace(base, providers=_RaisingProviders()))
    factory = build_provider_factory(cfg)
    # Degrade path returned EXACTLY the lazily-built public fallback, built once.
    assert factory is sentinel
    assert calls["n"] == 1


def test_build_provider_factory_reraises_composition_error(cfg: KiroCrewConfig) -> None:
    """A PlatformCompositionError from the seam MUST propagate (fail-closed)."""
    base = build_default_context(cfg, profile=PROFILE_ENTERPRISE)
    set_context(dataclasses.replace(base, providers=_CompositionErrorProviders()))
    with pytest.raises(PlatformCompositionError):
        build_provider_factory(cfg)


def test_build_provider_factory_default_delegates_to_cfg(cfg: KiroCrewConfig) -> None:
    """Public Default registry delegates to cfg.create_provider_factory() (identity).

    create_provider_factory returns a FRESH closure each call, so object identity
    is impossible; instead we prove the Default ProviderRegistry actually routes
    through cfg.create_provider_factory() (the transparency the public edition
    rests on) by asserting that method is invoked exactly once via the seam, and
    that the seam returns precisely what it produced.
    """
    sentinel = object()
    base = build_default_context(cfg)  # standalone defaults (DefaultProviderRegistry)
    set_context(base)
    calls = {"n": 0}

    def _fake_create():
        calls["n"] += 1
        return sentinel

    # The Default registry calls cfg.create_provider_factory(); stub it to a
    # sentinel and assert the seam returns exactly that, proving delegation.
    cfg.create_provider_factory = _fake_create  # type: ignore[assignment,method-assign]
    result = build_provider_factory(cfg)
    assert result is sentinel
    assert calls["n"] == 1  # built exactly once (no eager double-build)


# ── safe_context_call / async_safe_context_call lazy-fallback + fail-closed ──


def test_safe_context_call_lazy_fallback_not_built_on_happy_path() -> None:
    built = {"count": 0}

    def _fallback():
        built["count"] += 1
        return "fallback"

    result = safe_context_call(lambda: "ok", fallback_factory=_fallback)
    assert result == "ok"
    assert built["count"] == 0  # fallback NEVER built on the happy path


def test_safe_context_call_lazy_fallback_built_on_degrade() -> None:
    built = {"count": 0}

    def _fallback():
        built["count"] += 1
        return "fallback"

    def _boom():
        raise RuntimeError("transient")

    result = safe_context_call(_boom, fallback_factory=_fallback)
    assert result == "fallback"
    assert built["count"] == 1


def test_safe_context_call_reraises_composition_error() -> None:
    def _boom():
        raise PlatformCompositionError("nope")

    with pytest.raises(PlatformCompositionError):
        safe_context_call(_boom, fallback="x")


# ── fallback_factory that itself raises (the degrade-path guard) ──


def test_fallback_factory_failure_reraises_without_eager_fallback() -> None:
    """A fallback_factory raising on the degrade path is NOT swallowed: with no
    eager fallback there is no usable value, so the error propagates (it does not
    escape as an uncaught raise from inside the except, nor silently return)."""

    def _boom():
        raise RuntimeError("transient")

    def _bad_factory():
        raise ValueError("factory itself failed")

    with pytest.raises(ValueError, match="factory itself failed"):
        safe_context_call(_boom, fallback_factory=_bad_factory)


def test_fallback_factory_failure_degrades_to_eager_fallback() -> None:
    """If BOTH a raising fallback_factory and an eager fallback are given, a
    factory failure degrades to the eager fallback rather than propagating."""

    def _boom():
        raise RuntimeError("transient")

    def _bad_factory():
        raise ValueError("factory itself failed")

    assert safe_context_call(_boom, fallback="safe", fallback_factory=_bad_factory) == "safe"


def test_fallback_factory_composition_error_propagates() -> None:
    """A PlatformCompositionError from the fallback_factory ALWAYS propagates
    (fail-closed), even when an eager fallback is present."""

    def _boom():
        raise RuntimeError("transient")

    def _comp_factory():
        raise PlatformCompositionError("companion missing")

    with pytest.raises(PlatformCompositionError):
        safe_context_call(_boom, fallback="safe", fallback_factory=_comp_factory)


@pytest.mark.asyncio
async def test_async_safe_context_call_degrades() -> None:
    async def _boom():
        raise RuntimeError("transient")

    result = await async_safe_context_call(_boom, fallback="fallback")
    assert result == "fallback"


@pytest.mark.asyncio
async def test_async_safe_context_call_reraises_composition_error() -> None:
    async def _boom():
        raise PlatformCompositionError("nope")

    with pytest.raises(PlatformCompositionError):
        await async_safe_context_call(_boom, fallback="x")


@pytest.mark.asyncio
async def test_async_safe_context_call_happy_path() -> None:
    async def _ok():
        return "value"

    assert await async_safe_context_call(_ok, fallback="fallback") == "value"


@pytest.mark.asyncio
async def test_async_safe_context_call_lazy_fallback_not_built_on_happy_path() -> None:
    """async sibling: fallback_factory is NOT invoked when fn succeeds."""
    built = {"count": 0}

    def _fallback():
        built["count"] += 1
        return "fallback"

    async def _ok():
        return "ok"

    result = await async_safe_context_call(_ok, fallback_factory=_fallback)
    assert result == "ok"
    assert built["count"] == 0


@pytest.mark.asyncio
async def test_async_safe_context_call_lazy_fallback_built_on_degrade() -> None:
    """async sibling: fallback_factory invoked exactly once on the degrade path."""
    built = {"count": 0}

    def _fallback():
        built["count"] += 1
        return "fallback"

    async def _boom():
        raise RuntimeError("transient")

    result = await async_safe_context_call(_boom, fallback_factory=_fallback)
    assert result == "fallback"
    assert built["count"] == 1


# ── R2-7: missing-fallback footgun guard (neither fallback nor factory) ──


def test_safe_context_call_requires_a_fallback() -> None:
    """Passing NEITHER fallback nor fallback_factory raises TypeError at the call
    site (rather than silently returning the _UNSET sentinel on degrade)."""
    with pytest.raises(TypeError):
        safe_context_call(lambda: "ok")  # type: ignore[call-overload]


@pytest.mark.asyncio
async def test_async_safe_context_call_requires_a_fallback() -> None:
    async def _ok():
        return "ok"

    with pytest.raises(TypeError):
        await async_safe_context_call(_ok)  # type: ignore[call-overload]


# ── DashboardContributor / Jail Default no-op identity ──


def test_default_dashboard_contributor_is_noop(cfg: KiroCrewConfig) -> None:
    from kiro_crew.platform.defaults import DefaultDashboardContributor

    d = DefaultDashboardContributor()
    assert d.contribute_routes(object()) is None
    assert d.sso_login_handler() is None


@pytest.mark.asyncio
async def test_default_dashboard_services_symmetric_app(cfg: KiroCrewConfig) -> None:
    """stop_services takes the same app handle as start_services (symmetric)."""
    from kiro_crew.platform.defaults import DefaultDashboardContributor

    d = DefaultDashboardContributor()
    sentinel = object()
    assert await d.start_services(sentinel) is None
    assert await d.stop_services(sentinel) is None  # accepts app — no TypeError


def test_default_jail_provider_noop() -> None:
    from kiro_crew.platform.defaults import DefaultJailProvider

    j = DefaultJailProvider()
    assert j.available() is False
    assert j.maybe_reexec_into_jail(["kirocrew", "chat"], "on") is None
    assert isinstance(j.status_detail(), str)


# ── R4-6: KnowledgeProvider extra_connectors seam (propagate / degrade / merge) ──
#
# The production merge in dashboard/handlers/knowledge.py is
#   connectors.update(safe_context_call(lambda: ctx.knowledge.extra_connectors(ctx.cfg),
#                                        fallback={}, ...))
# with the built-ins set FIRST.  These tests reproduce that exact contract with an
# overlay KnowledgeProvider (a heavyweight setup_knowledge_routes integration test
# would be fragile; the seam behavior is what regresses).


class _ExtraConnectorsProvider:
    def __init__(self, extra):
        self._extra = extra

    def extra_connectors(self, cfg):
        return self._extra


class _RaisingKnowledge:
    def __init__(self, exc):
        self._exc = exc

    def extra_connectors(self, cfg):
        raise self._exc


def _merge_connectors(cfg: KiroCrewConfig):
    """Mirror of the production connector-merge (built-ins first, then the seam)."""
    from kiro_crew.platform import current_context, safe_context_call

    connectors = {"local_folder": object(), "obsidian_vault": object()}

    def _extra():
        ctx = current_context()
        return ctx.knowledge.extra_connectors(ctx.cfg)

    connectors.update(safe_context_call(_extra, fallback={}, log_message="degrade to built-ins"))
    return connectors


def test_knowledge_extra_connectors_merges_after_builtins(cfg: KiroCrewConfig) -> None:
    base = build_default_context(cfg, profile=PROFILE_ENTERPRISE)
    extra = {"quip": object()}
    set_context(dataclasses.replace(base, knowledge=_ExtraConnectorsProvider(extra)))
    merged = _merge_connectors(cfg)
    assert set(merged) == {"local_folder", "obsidian_vault", "quip"}


def test_knowledge_extra_connectors_degrades_to_builtins(cfg: KiroCrewConfig) -> None:
    base = build_default_context(cfg, profile=PROFILE_ENTERPRISE)
    set_context(dataclasses.replace(base, knowledge=_RaisingKnowledge(RuntimeError("boom"))))
    merged = _merge_connectors(cfg)
    assert set(merged) == {"local_folder", "obsidian_vault"}  # built-ins only


def test_knowledge_extra_connectors_reraises_composition_error(cfg: KiroCrewConfig) -> None:
    base = build_default_context(cfg, profile=PROFILE_ENTERPRISE)
    set_context(
        dataclasses.replace(base, knowledge=_RaisingKnowledge(PlatformCompositionError("x")))
    )
    with pytest.raises(PlatformCompositionError):
        _merge_connectors(cfg)


def test_default_knowledge_provider_empty(cfg: KiroCrewConfig) -> None:
    from kiro_crew.platform.defaults import DefaultKnowledgeProvider

    assert DefaultKnowledgeProvider().extra_connectors(cfg) == {}


# ── R4-5: DashboardContributor seam (sso_login_handler / contribute_routes / services) ──
#
# Reproduces the production fail-closed contract from dashboard/server.py:
#   sso_login_handler  : safe_context_call(lambda: ctx.dashboard.sso_login_handler(), fallback=None) or stub
#   contribute_routes: safe_context_call(lambda: ctx.dashboard.contribute_routes(app), fallback=None)
#   start/stop      : async_safe_context_call(lambda: ctx.dashboard.start_services(app), fallback=None)


class _RaisingDashboard:
    def __init__(self, exc):
        self._exc = exc

    def sso_login_handler(self):
        raise self._exc

    def contribute_routes(self, app):
        raise self._exc

    async def start_services(self, app):
        raise self._exc

    async def stop_services(self, app):
        raise self._exc


def test_dashboard_sso_login_handler_degrades_to_stub(cfg: KiroCrewConfig) -> None:
    from kiro_crew.platform import current_context, safe_context_call

    base = build_default_context(cfg, profile=PROFILE_ENTERPRISE)
    set_context(dataclasses.replace(base, dashboard=_RaisingDashboard(RuntimeError("boom"))))
    stub = object()
    handler = (
        safe_context_call(
            lambda: current_context().dashboard.sso_login_handler(),
            fallback=None,
            log_message="degrade",
        )
        or stub
    )
    assert handler is stub  # transient error → keep the built-in stub


def test_dashboard_contribute_routes_reraises_composition_error(cfg: KiroCrewConfig) -> None:
    from kiro_crew.platform import current_context, safe_context_call

    base = build_default_context(cfg, profile=PROFILE_ENTERPRISE)
    set_context(
        dataclasses.replace(base, dashboard=_RaisingDashboard(PlatformCompositionError("x")))
    )
    with pytest.raises(PlatformCompositionError):
        safe_context_call(
            lambda: current_context().dashboard.contribute_routes(object()),
            fallback=None,
            log_message="routes",
        )


@pytest.mark.asyncio
async def test_dashboard_start_services_degrades(cfg: KiroCrewConfig) -> None:
    from kiro_crew.platform import async_safe_context_call, current_context

    base = build_default_context(cfg, profile=PROFILE_ENTERPRISE)
    set_context(dataclasses.replace(base, dashboard=_RaisingDashboard(RuntimeError("boom"))))
    # Transient error degrades (returns fallback) rather than bricking gateway start.
    result = await async_safe_context_call(
        lambda: current_context().dashboard.start_services(object()),
        fallback=None,
        log_message="services",
    )
    assert result is None


@pytest.mark.asyncio
async def test_dashboard_start_services_reraises_composition_error(cfg: KiroCrewConfig) -> None:
    from kiro_crew.platform import async_safe_context_call, current_context

    base = build_default_context(cfg, profile=PROFILE_ENTERPRISE)
    set_context(
        dataclasses.replace(base, dashboard=_RaisingDashboard(PlatformCompositionError("x")))
    )
    with pytest.raises(PlatformCompositionError):
        await async_safe_context_call(
            lambda: current_context().dashboard.start_services(object()),
            fallback=None,
            log_message="services",
        )


# ── R5-1: the seam tests above reproduce the wiring; these GUARD the real call
# sites by source inspection, so a future swap of safe_context_call → a bare
# ``except Exception`` (which would SWALLOW PlatformCompositionError and silently
# downgrade a non-standalone host to OSS defaults) fails a test even though the
# behavioral tests use inline doubles. ──


def _read_source(modpath: str) -> str:
    import importlib
    from pathlib import Path

    return Path(importlib.import_module(modpath).__file__).read_text()


def test_knowledge_extra_connectors_site_uses_safe_context_call() -> None:
    """dashboard/handlers/knowledge.py must route extra_connectors through the
    fail-closed shim (not a bare except that would swallow composition errors)."""
    src = _read_source("kiro_crew.dashboard.handlers.knowledge")
    assert "extra_connectors" in src
    assert "safe_context_call" in src
    # The merge must NOT have regressed to swallowing everything.
    assert ".extra_connectors(" in src and "safe_context_call(" in src


def test_dashboard_contributor_sites_use_safe_context_call() -> None:
    """dashboard/server.py must wrap the DashboardContributor seam calls in the
    fail-closed shims: sync ``safe_context_call`` for sso_login_handler /
    contribute_routes, async ``async_safe_context_call`` for start/stop_services."""
    src = _read_source("kiro_crew.dashboard.server")
    for sym in ("sso_login_handler", "contribute_routes", "start_services", "stop_services"):
        assert sym in src, f"production site for {sym} disappeared"
    assert "safe_context_call(" in src
    assert "async_safe_context_call(" in src
