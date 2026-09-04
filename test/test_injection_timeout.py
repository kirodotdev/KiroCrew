"""Tests for the configurable INJECTION_TIMEOUT.

The gateway wraps each injected continuation turn (fired when the last
spawn_run subagent completes) in ``asyncio.wait_for(..., timeout=INJECTION_TIMEOUT)``.
spawn_run-heavy crons doing their final synthesis / multi-file apply on that turn
were cancelled at the old hard 300s cap. The fix raises the default to 900s and
makes it tunable via the ``KIROCREW_INJECTION_TIMEOUT`` env var, clamped to the
outer ``_ON_DONE_TIMEOUT`` cap, with invalid values falling back to the default.
"""

from __future__ import annotations

import pytest

import kiro_crew.subagent as subagent
from kiro_crew.subagent import (
    _DEFAULT_INJECTION_TIMEOUT,
    _ON_DONE_TIMEOUT,
    _env_float,
    _resolve_injection_timeout,
)


class TestInjectionTimeoutDefault:
    def test_default_is_900(self) -> None:
        """The shipped default is 900s (raised from the old hard 300s)."""
        assert _DEFAULT_INJECTION_TIMEOUT == 900.0

    def test_resolve_with_no_env_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_INJECTION_TIMEOUT", raising=False)
        assert _resolve_injection_timeout() == 900.0

    def test_module_constant_matches_resolver(self) -> None:
        """The module-level INJECTION_TIMEOUT is produced by the resolver."""
        # On an unset env, the import-time value equals the default.
        assert subagent.INJECTION_TIMEOUT == pytest.approx(_resolve_injection_timeout())


class TestInjectionTimeoutEnvOverride:
    def test_env_override_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_INJECTION_TIMEOUT", "600")
        assert _resolve_injection_timeout() == 600.0

    def test_env_override_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_INJECTION_TIMEOUT", "450.5")
        assert _resolve_injection_timeout() == 450.5

    def test_env_override_clamped_to_on_done_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A value above the outer cap is clamped to _ON_DONE_TIMEOUT."""
        monkeypatch.setenv("KIROCREW_INJECTION_TIMEOUT", "99999")
        assert _resolve_injection_timeout() == _ON_DONE_TIMEOUT

    def test_env_override_at_cap_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_INJECTION_TIMEOUT", str(_ON_DONE_TIMEOUT))
        assert _resolve_injection_timeout() == _ON_DONE_TIMEOUT


class TestInjectionTimeoutInvalidFallback:
    @pytest.mark.parametrize(
        "bad",
        ["notanumber", "", "  ", "abc123", "1.2.3", "nan_but_text"],
    )
    def test_invalid_env_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        monkeypatch.setenv("KIROCREW_INJECTION_TIMEOUT", bad)
        assert _resolve_injection_timeout() == 900.0

    @pytest.mark.parametrize("bad", ["0", "-5", "-0.1", "0.0"])
    def test_non_positive_env_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        """Zero / negative would disable or invert the cap — reject them."""
        monkeypatch.setenv("KIROCREW_INJECTION_TIMEOUT", bad)
        assert _resolve_injection_timeout() == 900.0


class TestEnvFloatHelper:
    def test_parses_valid_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_TEST_FLOAT", "12.5")
        assert _env_float("KIROCREW_TEST_FLOAT", 1.0) == 12.5

    def test_missing_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_TEST_FLOAT", raising=False)
        assert _env_float("KIROCREW_TEST_FLOAT", 3.0) == 3.0

    def test_invalid_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_TEST_FLOAT", "xyz")
        assert _env_float("KIROCREW_TEST_FLOAT", 7.0) == 7.0

    def test_non_positive_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_TEST_FLOAT", "-2")
        assert _env_float("KIROCREW_TEST_FLOAT", 7.0) == 7.0
