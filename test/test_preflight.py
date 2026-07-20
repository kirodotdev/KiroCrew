"""Tests for the seam-supplied preflight checks runner."""

from __future__ import annotations

import dataclasses
from typing import Callable, List

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import PlatformCompositionError, set_context
from kiro_crew.preflight import run_preflight_checks


class _StubIdentity:
    """IdentityProvider stub returning a caller-supplied check list."""

    def __init__(self, checks: List[Callable[[], None]]):
        self._checks = checks

    def status(self) -> dict:
        return {}

    async def status_line(self, prefix: str = "*Midway:*") -> str:
        return ""

    def whoami(self) -> None:
        return None

    def issuer(self) -> None:
        return None

    def preflight_checks(self) -> List[Callable[[], None]]:
        return self._checks


class _RaisingIdentity(_StubIdentity):
    """IdentityProvider stub whose preflight_checks lookup itself fails."""

    def __init__(self, exc: BaseException):
        super().__init__([])
        self._exc = exc

    def preflight_checks(self) -> List[Callable[[], None]]:
        raise self._exc


def _install_identity(identity: object) -> None:
    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, identity=identity))


class TestRunPreflightChecks:
    def test_empty_list_is_noop(self) -> None:
        run_preflight_checks([])  # should not raise

    def test_default_context_is_noop(self) -> None:
        # DefaultIdentityProvider.preflight_checks() returns [] — the lazily
        # built standalone context makes run_preflight_checks() a pure no-op.
        run_preflight_checks()  # should not raise

    def test_calls_each_check_in_order(self) -> None:
        calls: List[str] = []
        run_preflight_checks([lambda: calls.append("a"), lambda: calls.append("b")])
        assert calls == ["a", "b"]

    def test_swallows_exception_and_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        calls: List[str] = []

        def _boom() -> None:
            raise RuntimeError("boom")

        with caplog.at_level("WARNING", logger="kiro_crew.preflight"):
            run_preflight_checks([_boom, lambda: calls.append("after")])
        # The failing check is logged and the NEXT check still runs.
        assert calls == ["after"]
        assert any("preflight check" in rec.getMessage() for rec in caplog.records)

    def test_propagates_system_exit(self) -> None:
        def _abort() -> None:
            raise SystemExit(1)

        with pytest.raises(SystemExit):
            run_preflight_checks([_abort])

    def test_checks_sourced_from_context_identity(self) -> None:
        calls: List[str] = []
        _install_identity(_StubIdentity([lambda: calls.append("seam")]))
        run_preflight_checks()
        assert calls == ["seam"]

    def test_context_failure_degrades_to_empty(self) -> None:
        # A transient adapter failure must never block gateway/token startup:
        # safe_context_call degrades to [] and the runner is a no-op.
        _install_identity(_RaisingIdentity(RuntimeError("adapter broke")))
        run_preflight_checks()  # should not raise

    def test_composition_error_propagates_fail_closed(self) -> None:
        # PlatformCompositionError (a non-standalone host that could not
        # compose its companion) must NOT silently degrade to no checks.
        _install_identity(_RaisingIdentity(PlatformCompositionError("no companion")))
        with pytest.raises(PlatformCompositionError):
            run_preflight_checks()

    def test_explicit_checks_bypass_context(self) -> None:
        # An explicit list is used as-is — the context is never consulted.
        calls: List[str] = []
        _install_identity(_RaisingIdentity(PlatformCompositionError("no companion")))
        run_preflight_checks([lambda: calls.append("explicit")])
        assert calls == ["explicit"]
