"""Tests for the configurable ``pod up`` health-wait budget.

A slow-but-healthy first boot (config migration, CLI staging, a fresh
``.local_secret``) on a loaded host must be reportable as such, not with the
same "never became healthy" verdict as a dead gateway. These tests pin the
three pieces of that contract: the flag > env > default resolver (with its
malformed-value fallback and floor/ceiling clamp), the resolved budget
actually reaching ``_wait_healthy`` as a wall-clock deadline, and the
exhausted-budget message telling a live, still-starting gateway apart from
one that died or serves errors.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig


@pytest.fixture(autouse=True)
def _isolate_pod_host_state(tmp_path_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep pod HOST state (unit file, pods_dir mutex) out of the real home.

    Same isolation as test_pod.py's fixture of the same name: ``unit.unit_path``
    and ``PodConfig.pods_dir`` resolve through ``Path.home()``, not
    ``KIROCREW_HOME``, so a test reaching ``_up`` would otherwise write the
    developer's real per-user service state.
    """
    home = tmp_path_factory.mktemp("pod-host-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture(autouse=True)
def _systemd_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the systemd backend so ``rt.systemctl`` patches hold on every host."""
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", False)


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _ns(**over: object) -> argparse.Namespace:
    base: dict[str, object] = dict(
        name="demo", json=False, seed="", ttl="2h", provision=False, wait_secs=None
    )
    base.update(over)
    return argparse.Namespace(**base)


class TestHealthWaitSecsResolver:
    def test_flag_wins_over_env_and_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(pod_cli.POD_HEALTH_WAIT_SECS_ENV, "120")
        assert pod_cli._health_wait_secs(_ns(wait_secs=33)) == 33

    def test_env_wins_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(pod_cli.POD_HEALTH_WAIT_SECS_ENV, "120")
        assert pod_cli._health_wait_secs(_ns()) == 120

    def test_default_when_nothing_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(pod_cli.POD_HEALTH_WAIT_SECS_ENV, raising=False)
        assert pod_cli._health_wait_secs(_ns()) == pod_cli.POD_HEALTH_WAIT_SECS_DEFAULT

    def test_namespace_without_the_attr_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hand-built Namespaces (tests, older callers) may not carry wait_secs."""
        monkeypatch.delenv(pod_cli.POD_HEALTH_WAIT_SECS_ENV, raising=False)
        ns = argparse.Namespace(name="demo")
        assert pod_cli._health_wait_secs(ns) == pod_cli.POD_HEALTH_WAIT_SECS_DEFAULT

    @pytest.mark.parametrize("bad", ["soon", "12.5", "-30", "0", "90s"])
    def test_malformed_env_warns_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], bad: str
    ) -> None:
        """A typo in the env var must degrade, never make `pod up` unbootable."""
        monkeypatch.setenv(pod_cli.POD_HEALTH_WAIT_SECS_ENV, bad)
        assert pod_cli._health_wait_secs(_ns()) == pod_cli.POD_HEALTH_WAIT_SECS_DEFAULT
        err = capsys.readouterr().err
        assert "ignoring" in err and pod_cli.POD_HEALTH_WAIT_SECS_ENV in err

    @pytest.mark.parametrize("empty", ["", "   "])
    def test_blank_env_is_treated_as_unset_without_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], empty: str
    ) -> None:
        monkeypatch.setenv(pod_cli.POD_HEALTH_WAIT_SECS_ENV, empty)
        assert pod_cli._health_wait_secs(_ns()) == pod_cli.POD_HEALTH_WAIT_SECS_DEFAULT
        assert capsys.readouterr().err == ""

    def test_floor_clamps_a_tiny_flag(self) -> None:
        assert pod_cli._health_wait_secs(_ns(wait_secs=1)) == pod_cli._POD_HEALTH_WAIT_FLOOR_SECS

    @pytest.mark.parametrize("bad", [0, -30])
    def test_non_positive_flag_warns_and_falls_back_like_a_bad_env_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        bad: int,
    ) -> None:
        """Flag and env degrade the same way: warn, then use the default."""
        monkeypatch.delenv(pod_cli.POD_HEALTH_WAIT_SECS_ENV, raising=False)
        assert pod_cli._health_wait_secs(_ns(wait_secs=bad)) == (
            pod_cli.POD_HEALTH_WAIT_SECS_DEFAULT
        )
        assert "ignoring --wait-secs" in capsys.readouterr().err

    def test_ceiling_clamps_an_absurd_flag(self) -> None:
        assert pod_cli._health_wait_secs(_ns(wait_secs=10**400)) == (
            pod_cli._POD_HEALTH_WAIT_CEIL_SECS
        )

    def test_ceiling_clamps_an_absurd_env_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(pod_cli.POD_HEALTH_WAIT_SECS_ENV, str(10**400))
        assert pod_cli._health_wait_secs(_ns()) == pod_cli._POD_HEALTH_WAIT_CEIL_SECS

    def test_invalid_flag_takes_the_default_not_the_env_var(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The warning promises the default, so the env var must not apply."""
        monkeypatch.setenv(pod_cli.POD_HEALTH_WAIT_SECS_ENV, "120")
        assert pod_cli._health_wait_secs(_ns(wait_secs=0)) == (pod_cli.POD_HEALTH_WAIT_SECS_DEFAULT)
        assert "ignoring --wait-secs" in capsys.readouterr().err

    def test_floor_clamps_a_tiny_env_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(pod_cli.POD_HEALTH_WAIT_SECS_ENV, "2")
        assert pod_cli._health_wait_secs(_ns()) == pod_cli._POD_HEALTH_WAIT_FLOOR_SECS


class _Clock:
    """Deterministic stand-in for the module's monotonic clock and sleep."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs


class TestWaitHealthyBudget:
    def _pin_clock(self, monkeypatch: pytest.MonkeyPatch) -> _Clock:
        clock = _Clock()
        monkeypatch.setattr(pod_cli.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(pod_cli.time, "sleep", clock.sleep)
        return clock

    def test_budget_is_wall_clock_with_fast_probes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``tries`` is the budget in seconds against a monotonic deadline.

        With instant probes and 1s sleeps a never-healthy gateway is polled
        once per second: ``tries`` in-budget polls plus the one at the deadline.
        """
        clock = self._pin_clock(monkeypatch)
        calls: list[int] = []
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: (calls.append(1), 0)[1])
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("activating", 0))
        cfg = PodConfig.load()
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=7) == 0
        assert len(calls) == 8
        assert clock.now == 7.0

    def test_slow_probes_shrink_the_sleeps_instead_of_stretching_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bound-but-silent port eats the probe timeout; the budget must not
        multiply by it. Overrun is capped at one probe past the deadline."""
        clock = self._pin_clock(monkeypatch)
        calls: list[int] = []

        def _slow_health(cfg: object, n: object, p: object) -> int:
            calls.append(1)
            clock.now += 3.0  # each probe burns its own 3s timeout
            return 0

        monkeypatch.setattr(rt, "health", _slow_health)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("activating", 0))
        cfg = PodConfig.load()
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=7) == 0
        # Poll-count semantics would spend tries * 4s of wall clock; the
        # deadline caps the overrun at one probe past the budget.
        assert clock.now <= 7.0 + 3.0
        assert len(calls) == 2

    def test_a_gateway_that_served_errors_then_went_silent_is_not_a_slow_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An HTTP answer seen mid-wait is remembered at exhaustion: 500 then
        silence must not read as "never answered" (code 0)."""
        self._pin_clock(monkeypatch)
        codes = iter([500])
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: next(codes, 0))
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        cfg = PodConfig.load()
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == 500

    def test_exhausted_budget_returns_the_last_http_error_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A gateway serving errors (404/5xx) must surface its real status, not
        0 -- `_up` keys the slow-boot attribution on code == 0."""
        self._pin_clock(monkeypatch)
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 404)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        cfg = PodConfig.load()
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == 404

    def test_default_budget_is_the_module_constant(self) -> None:
        import inspect

        sig = inspect.signature(pod_cli._wait_healthy)
        assert sig.parameters["tries"].default == pod_cli.POD_HEALTH_WAIT_SECS_DEFAULT
        assert pod_cli.POD_HEALTH_WAIT_SECS_DEFAULT > 45


class TestUpThreadsTheBudget:
    """`_up` must hand the RESOLVED budget to `_wait_healthy`, and the
    exhausted-budget verdict must tell a live slow boot apart from a dead one."""

    def _prep(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_WORKTREES_ROOT", str(tmp_path / "wts"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {})
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        # A ready worktree: venv binary + built dist, so `_up` skips provisioning.
        from kiro_crew.pod import provision as prov

        co = tmp_path / "wts" / "demo"
        b = prov.venv_bin(co)
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
        (co / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        return PodConfig.load()

    def test_up_passes_the_resolved_budget_to_wait_healthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._prep(tmp_path, monkeypatch)
        seen: list[int] = []
        monkeypatch.setattr(
            pod_cli,
            "_wait_healthy",
            lambda cfg, n, p, tries=0: (seen.append(tries), 403)[1],
        )
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        pod_cli._up(c, _ns(wait_secs=17))
        assert seen == [17]

    def test_up_env_budget_reaches_wait_healthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setenv(pod_cli.POD_HEALTH_WAIT_SECS_ENV, "23")
        seen: list[int] = []
        monkeypatch.setattr(
            pod_cli,
            "_wait_healthy",
            lambda cfg, n, p, tries=0: (seen.append(tries), 403)[1],
        )
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        pod_cli._up(c, _ns())
        assert seen == [23]

    def _fail_up(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        unit_state: tuple[str, int],
        code: int = 0,
    ) -> tuple[list[tuple], list[str]]:
        """Drive `_up` into the exhausted-budget branch; return (audits, stops)."""
        c = self._prep(tmp_path, monkeypatch)
        audits: list[tuple] = []
        stops: list[str] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: audits.append((a, k)))
        # Default 0 = budget exhausted without ever answering (not -1, not
        # FOREIGN); pass a real HTTP error code to model a serving-but-broken
        # gateway.
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0: code)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: unit_state)
        monkeypatch.setattr(rt, "recent_journal", lambda cfg, n, ln=30: "journal tail")
        monkeypatch.setattr(rt, "stop_pod", lambda cfg, n: (stops.append(n), _cp())[1])
        with pytest.raises(SystemExit):
            pod_cli._up(c, _ns(wait_secs=17))
        return audits, stops

    def test_exhausted_budget_with_a_live_unit_says_still_starting(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        audits, stops = self._fail_up(tmp_path, monkeypatch, unit_state=("activating", 0))
        err = capsys.readouterr().err
        assert "gateway still starting after 17s" in err
        assert "process alive, /api/health not yet answering" in err
        # The remedy names both raise paths, doubled from the exhausted budget.
        assert "--wait-secs 34" in err
        assert f"{pod_cli.POD_HEALTH_WAIT_SECS_ENV}=34" in err
        assert "never became healthy" not in err
        # The pod is still stopped: the caller contract stays "up means healthy".
        assert stops == ["demo"]
        assert any(
            k.get("error") == "health wait exhausted while gateway alive" for _a, k in audits
        )

    @pytest.mark.parametrize(
        "dead", [("failed", 0), ("inactive", 0), ("unknown", 0), ("active", 2)]
    )
    def test_exhausted_budget_without_a_live_unit_keeps_the_legacy_verdict(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        dead: tuple[str, int],
    ) -> None:
        """Only positive evidence of a live unit earns the slow-boot attribution."""
        audits, stops = self._fail_up(tmp_path, monkeypatch, unit_state=dead)
        err = capsys.readouterr().err
        assert "never became healthy" in err
        assert "still starting" not in err
        assert stops == ["demo"]
        assert not any(
            k.get("error") == "health wait exhausted while gateway alive" for _a, k in audits
        )

    @pytest.mark.parametrize("code", [404, 500])
    def test_a_gateway_serving_errors_keeps_the_legacy_verdict(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        code: int,
    ) -> None:
        """A live unit whose gateway answers 404/5xx is NOT still starting: it is
        serving and its health route is broken, so more wait can never fix it.
        The slow-boot attribution must not displace the legacy verdict here."""
        audits, stops = self._fail_up(tmp_path, monkeypatch, unit_state=("active", 0), code=code)
        err = capsys.readouterr().err
        assert "never became healthy" in err
        assert "still starting" not in err
        assert stops == ["demo"]
        assert not any(
            k.get("error") == "health wait exhausted while gateway alive" for _a, k in audits
        )
