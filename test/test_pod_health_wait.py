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

from kiro_crew.instances import run_marker
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
    """Pin the systemd backend so runtime boundary patches hold on every host."""
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", False)
    monkeypatch.setattr(rt, "require_backend", lambda: None)


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

    def test_the_escalation_hint_names_a_budget_the_resolver_will_honour(self) -> None:
        """Doubling is only advice while doubling is reachable.

        ``_health_wait_secs`` clamps to the ceiling, so at the ceiling "try
        --wait-secs <2x>" names a value silently reduced back to the one that just
        failed, sending the operator to retry an identical wait.
        """
        below = pod_cli._wait_escalation_hint("demo", 90)
        assert "--wait-secs 180" in below
        assert f"{pod_cli.POD_HEALTH_WAIT_SECS_ENV}=180" in below

        at_ceiling = pod_cli._wait_escalation_hint("demo", pod_cli._POD_HEALTH_WAIT_CEIL_SECS)
        assert "--wait-secs" not in at_ceiling
        assert str(pod_cli._POD_HEALTH_WAIT_CEIL_SECS) in at_ceiling
        assert "ceiling" in at_ceiling

        # The boundary itself: half the ceiling doubles exactly onto it, which the
        # resolver still honours unchanged, so it stays actionable advice.
        exact = pod_cli._wait_escalation_hint("demo", pod_cli._POD_HEALTH_WAIT_CEIL_SECS // 2)
        assert f"--wait-secs {pod_cli._POD_HEALTH_WAIT_CEIL_SECS}" in exact

    def test_a_budget_below_the_ceiling_is_never_told_it_is_at_the_ceiling(self) -> None:
        """The question is whether THIS budget is at the ceiling, not whether doubling
        it would pass one.

        `_health_wait_secs` clamps only ABOVE the ceiling, so a 2000s budget is honoured
        verbatim -- and testing the DOUBLED value declared every budget past the halfway
        mark to be at the ceiling, withholding the one remedy that helps. Below the
        ceiling there is always more to ask for, and the advice names a value the
        resolver will honour rather than one it would clamp away.
        """
        ceil = pod_cli._POD_HEALTH_WAIT_CEIL_SECS
        for budget in (ceil // 2 + 1, 2000, ceil - 1):
            assert budget < ceil, "this case must sit below the ceiling to be meaningful"
            hint = pod_cli._wait_escalation_hint("demo", budget)
            assert "ceiling" not in hint, f"{budget}s is below the ceiling: {hint}"
            assert f"--wait-secs {ceil}" in hint, hint
            assert f"{pod_cli.POD_HEALTH_WAIT_SECS_ENV}={ceil}" in hint

        # Whatever the budget, the advised value is one the RESOLVER honours -- asked
        # of `_health_wait_secs` itself rather than compared against the ceiling,
        # because re-deriving the cap here would only restate the arithmetic under
        # test. What the operator needs is that typing the advised number back in
        # produces that number, and only the resolver can answer that.
        for budget in (5, 90, 1000, 2000, ceil - 1, ceil):
            hint = pod_cli._wait_escalation_hint("demo", budget)
            advised = [int(tok) for tok in hint.replace("`", " ").split() if tok.isdigit()]
            for value in advised:
                assert pod_cli._health_wait_secs(_ns(wait_secs=value)) == value, (
                    f"{budget}s was advised {value}s, which the resolver does not honour "
                    f"verbatim -- retrying it would not change the budget"
                )


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
            lambda cfg, n, p, tries=0, **_kw: (seen.append(tries), 403)[1],
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
            lambda cfg, n, p, tries=0, **_kw: (seen.append(tries), 403)[1],
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
        was_active: bool = False,
        home_populated: bool = False,
        stop_rc: int = 0,
        stop_stderr: str = "",
    ) -> tuple[list[tuple], list[str]]:
        """Drive `_up` into the exhausted-budget branch; return (audits, stops)."""
        c = self._prep(tmp_path, monkeypatch)
        audits: list[tuple] = []
        stops: list[str] = []
        halts: list[str] = []
        self._halts = halts
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: audits.append((a, k)))
        # Default 0 = budget exhausted without ever answering (not -1, not
        # FOREIGN); pass a real HTTP error code to model a serving-but-broken
        # gateway.
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: code)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: was_active)
        monkeypatch.setattr(pod_cli, "_home_holds_state", lambda cfg, n: home_populated)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: unit_state)
        monkeypatch.setattr(rt, "recent_journal", lambda cfg, n, ln=30: "journal tail")
        monkeypatch.setattr(
            rt,
            "stop_pod",
            lambda cfg, n: (stops.append(n), _cp(returncode=stop_rc, stderr=stop_stderr))[1],
        )
        monkeypatch.setattr(rt, "halt_pod", lambda cfg, n: (halts.append(n), _cp())[1])
        with pytest.raises(SystemExit):
            pod_cli._up(c, _ns(wait_secs=17))
        return audits, stops

    def test_an_unreadable_credential_does_not_destroy_a_running_pod(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`up` must not tear down a pod it did not start over a credential.

        `stop_pod` reaches `cleanup_home`, which rmtree's the pod's isolated HOME --
        its sessions and its config -- and nothing restores that. The credential
        verdict is the only failure a SERVING pod can reach, so an `up` against an
        already-running pod whose credential file is missing, blank or corrupt would
        otherwise destroy a healthy instance the caller merely asked about.
        """
        _audits, stops = self._fail_up(
            tmp_path,
            monkeypatch,
            unit_state=("active", 0),
            code=pod_cli.HEALTH_NO_CREDENTIAL,
            was_active=True,
        )
        assert stops == [], "a pod this command did not start must not be stopped"
        err = capsys.readouterr().err
        assert "NOT stopped" in err

    @pytest.mark.parametrize(
        "code",
        [0, -1, 404, "foreign", "nocred"],
        ids=["silent", "crash-loop", "broken-health-route", "foreign-port", "no-credential"],
    )
    def test_every_verdict_says_the_pod_was_not_stopped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        code: object,
    ) -> None:
        """The notice has to be as verdict-independent as the guard is.

        The teardown is skipped on every verdict, so announcing it on only one of the
        five exits leaves the operator of a foreign-port, crashed or slow-booting pod
        believing `up` cleaned up after itself -- and what they do next is derived from
        that belief, whether that is rerunning or reallocating the port under a live
        gateway. The notice states only what this command DID, because a pre-existing
        pod that has genuinely failed is not running and claiming otherwise would swap
        one false impression for another.
        """
        verdict = {
            "foreign": rt.HEALTH_FOREIGN,
            "nocred": pod_cli.HEALTH_NO_CREDENTIAL,
        }.get(code, code)
        self._fail_up(
            tmp_path,
            monkeypatch,
            unit_state=("active", 0),
            code=verdict,
            was_active=True,
            home_populated=True,
        )
        err = capsys.readouterr().err
        assert "NOT stopped" in err
        assert "pod down demo" in err, "the notice must name the deliberate way out"

    @pytest.mark.parametrize(
        "code",
        [0, -1, 404, "foreign", "nocred"],
        ids=["silent", "crash-loop", "broken-health-route", "foreign-port", "no-credential"],
    )
    def test_no_verdict_claims_a_stopped_pod_was_spared(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        code: object,
    ) -> None:
        """The inverse: a pod this command really did tear down must not be described
        as spared, or the operator is sent to `pod down` something already gone."""
        verdict = {
            "foreign": rt.HEALTH_FOREIGN,
            "nocred": pod_cli.HEALTH_NO_CREDENTIAL,
        }.get(code, code)
        _audits, stops = self._fail_up(
            tmp_path,
            monkeypatch,
            unit_state=("active", 0),
            code=verdict,
            was_active=False,
            home_populated=False,
        )
        err = capsys.readouterr().err
        assert stops == ["demo"]
        assert "NOT stopped" not in err

    def test_a_failed_halt_is_reported_instead_of_claimed_as_stopped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The notice's claim rests on halt_pod's return code, so it must be read.

        `halt_pod` returns non-zero exactly for the cases that matter -- a Linux reload
        it refused to proceed without, a launchd bootout or a Windows retirement it
        could not confirm -- and every one leaves the gateway RUNNING. An operator told
        "stopped" would leave a crash-looping unit in place believing it was handled.
        """
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 0)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(pod_cli, "_home_holds_state", lambda cfg, n: True)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("activating", 2))
        monkeypatch.setattr(rt, "recent_journal", lambda cfg, n, ln=30: "journal tail")
        monkeypatch.setattr(rt, "stop_pod", lambda cfg, n: _cp())
        monkeypatch.setattr(
            rt,
            "halt_pod",
            lambda cfg, n: _cp(returncode=1, stderr="daemon-reload refused"),
        )
        with pytest.raises(SystemExit):
            pod_cli._up(c, _ns(wait_secs=17))
        err = capsys.readouterr().err
        assert "Stopping it FAILED" in err
        assert "daemon-reload refused" in err
        assert "has been stopped" not in err, "a failed halt must not be reported as stopped"

    def test_the_slow_boot_verdict_does_not_claim_the_port_was_silent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`code == 0` covers two states and only one of them is a silent port.

        `_wait_healthy` returns `last_http` for a pod that was serving on the final
        poll but for less than the credential verdict's span, and `last_http` is 0 when
        no error status was ever seen. Asserting "/api/health not yet answering" there
        was false, and false in the direction that sends the operator hunting a dead
        process instead of raising the budget.
        """
        _audits, _stops = self._fail_up(
            tmp_path,
            monkeypatch,
            unit_state=("activating", 0),
            code=0,
            was_active=False,
            home_populated=False,
        )
        err = capsys.readouterr().err
        assert "still starting" in err
        assert "readiness did not complete" in err
        assert "not yet answering)" not in err, "the port may have been answering"

    def test_a_unit_this_command_started_is_stopped_without_losing_its_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Stopping is owed when we started it; deleting only when we made the home.

        Sparing the home must not mean walking away from the unit. The pod unit is
        `Restart=on-failure` with `RestartSec=5` and no `StartLimit` override, and a
        gateway that raises at import exits 1 -- not a terminal boot code -- so an
        unstopped crash-looping unit respawns every five seconds indefinitely: the 5s
        gap never fills systemd's default ten-second burst window, so the rate limiter
        never retires it either. `halt_pod` ends the respawn without reaching
        `cleanup_home`.
        """
        _audits, stops = self._fail_up(
            tmp_path,
            monkeypatch,
            unit_state=("activating", 2),
            code=0,
            was_active=False,
            home_populated=True,
        )
        err = capsys.readouterr().err
        assert stops == [], "the home predates this command, so it must not be reclaimed"
        assert self._halts == ["demo"], "a unit this command started must still be stopped"
        assert "has been stopped" in err and "KEPT" in err
        assert "pod down demo" in err

    def test_a_pod_this_command_did_not_start_is_neither_stopped_nor_reclaimed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The round-9 case stays untouched: this invocation started nothing, so the
        pod's lifecycle is not its business and `pod down` owns it."""
        _audits, stops = self._fail_up(
            tmp_path,
            monkeypatch,
            unit_state=("active", 0),
            code=pod_cli.HEALTH_NO_CREDENTIAL,
            was_active=True,
            home_populated=True,
        )
        err = capsys.readouterr().err
        assert stops == [] and self._halts == []
        assert "NOT stopped" in err
        assert "did not start it" in err

    def test_a_failed_pre_existing_pod_is_not_described_as_running(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The notice may only claim what this command did, not what the unit is doing.

        Sparing a pod is decided by `started_here`/`home_was_populated`, neither of
        which says the unit is healthy -- a pre-existing pod whose unit has genuinely
        FAILED is spared and is not running. Asserting that it is would swap the false
        impression that `up` cleaned up for the false impression that there is a live
        gateway to talk to.
        """
        _audits, stops = self._fail_up(
            tmp_path,
            monkeypatch,
            unit_state=("failed", 3),
            code=0,
            was_active=True,
            home_populated=True,
        )
        err = capsys.readouterr().err
        assert stops == [] and self._halts == []
        assert "NOT stopped" in err
        assert "pod down demo" in err
        assert "RUNNING" not in err, "the message must not claim a failed unit is running"
        assert "still running" not in err.lower()

    @pytest.mark.parametrize(
        "code",
        [0, -1, 404, "foreign", "nocred"],
        ids=["silent", "crash-loop", "broken-health-route", "foreign-port", "no-credential"],
    )
    def test_no_verdict_destroys_the_home_of_a_pod_in_restart_backoff(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        code: object,
    ) -> None:
        """The destruction guard must not rest on a liveness probe.

        `rt.is_active` shells `systemctl is-active --quiet`, which succeeds only for
        `active`, so a pod in `activating` or in `Restart=on-failure` backoff -- the
        crash-loop this package documents as the ordinary case -- answers False. A
        guard keyed on that reading treats a PRE-EXISTING pod as one this command
        created, and an unsatisfied health wait then rmtree's a home that predates the
        invocation, with no recovery path. `started_here` plus `home_was_populated`
        are facts about what actually happened, so neither can be wrong about the
        past.
        """
        verdict = {
            "foreign": rt.HEALTH_FOREIGN,
            "nocred": pod_cli.HEALTH_NO_CREDENTIAL,
        }.get(code, code)
        _audits, stops = self._fail_up(
            tmp_path,
            monkeypatch,
            # `activating` with restarts on the clock: is_active says False, so this
            # command allocates a port and starts the unit -- but the home is not ours.
            unit_state=("activating", 2),
            code=verdict,
            was_active=False,
            home_populated=True,
        )
        capsys.readouterr()
        assert stops == [], "a home that predates this command must never be deleted"

    @pytest.mark.parametrize(
        "code",
        [0, -1, 404, "foreign", "nocred"],
        ids=["silent", "crash-loop", "broken-health-route", "foreign-port", "no-credential"],
    )
    def test_no_verdict_stops_a_pod_this_command_did_not_start(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        code: object,
    ) -> None:
        """`up` never tears down a pod it did not start, whatever the verdict.

        `stop_pod` reaches `cleanup_home`, which rmtree's the pod's isolated HOME --
        its sessions and its config -- and nothing restores it. The mutex rationale
        beside that call already claims "the pod we stop here can only be the one we
        started"; keying the guard on the VERDICT instead left that claim false for
        every verdict the carve-out did not enumerate, which is how a recovered pod
        carrying a cumulative restart count could still be deleted.
        """
        verdict = {
            "foreign": rt.HEALTH_FOREIGN,
            "nocred": pod_cli.HEALTH_NO_CREDENTIAL,
        }.get(code, code)
        _audits, stops = self._fail_up(
            tmp_path,
            monkeypatch,
            unit_state=("active", 0),
            code=verdict,
            was_active=True,
        )
        capsys.readouterr()
        assert stops == [], "a pod this command did not start must never be stopped"

    def test_a_pod_this_command_started_is_still_stopped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The guard keys on `was_active`, so the contract that `up` does not leak a
        half-booted service it created is unchanged."""
        _audits, stops = self._fail_up(
            tmp_path,
            monkeypatch,
            unit_state=("active", 0),
            code=pod_cli.HEALTH_NO_CREDENTIAL,
            was_active=False,
        )
        err = capsys.readouterr().err
        assert stops == ["demo"]
        assert "still running" not in err

    def test_exhausted_budget_with_a_live_unit_says_still_starting(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        audits, stops = self._fail_up(tmp_path, monkeypatch, unit_state=("activating", 0))
        err = capsys.readouterr().err
        assert "gateway still starting after 17s" in err
        assert "process alive" in err and "readiness did not complete" in err
        # The remedy names both raise paths, doubled from the exhausted budget.
        assert "--wait-secs 34" in err
        assert f"{pod_cli.POD_HEALTH_WAIT_SECS_ENV}=34" in err
        assert "never became healthy" not in err
        # The pod is still stopped: the caller contract stays "up means healthy".
        assert stops == ["demo"]
        assert any(
            k.get("error") == "health wait exhausted while gateway alive" for _a, k in audits
        )

    def test_a_brief_serving_pod_is_a_slow_boot_whatever_its_restart_count(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A gateway seen serving on the final poll is starting, not dead.

        The population this matters for is precisely the one the recovered-pod
        rationale rests on: ``restarts`` is systemd's cumulative ``NRestarts`` and
        nothing in this package resets it, so any pod that ever auto-restarted
        carries it forever. Resolving liveness from the unit therefore condemns it,
        and the operator is told the gateway never became healthy -- with the only
        applicable remedy, a bigger budget, withheld -- about a pod that answered
        200 moments earlier.
        """
        # ("failed", 9) is the strongest possible contradiction: if the verdict
        # consulted the unit at all it would land on "never became healthy". Naming
        # the unit this way proves the port's own answer decides, without needing to
        # instrument a call the helper installs itself.
        audits, stops = self._fail_up(
            tmp_path,
            monkeypatch,
            unit_state=("failed", 9),
            code=pod_cli.HEALTH_SERVING_TOO_BRIEFLY,
        )
        err = capsys.readouterr().err
        assert "gateway still starting after 17s" in err
        assert "never became healthy" not in err
        # The remedy this verdict exists to deliver.
        assert "--wait-secs 34" in err
        assert any(
            k.get("error") == "health wait exhausted while gateway alive" for _a, k in audits
        )
        assert stops == ["demo"]

    def test_a_failed_teardown_is_reported_instead_of_claimed_as_removed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A `stop_pod` that failed leaves a live gateway, so it must be said.

        This is the branch that DID create the pod, so silence is right when the
        teardown succeeds. When it does not, the gateway may still hold the port and
        the isolated home may survive -- and an operator who reads only the readiness
        verdict reruns against a port they believe is free.
        """
        audits, stops = self._fail_up(
            tmp_path,
            monkeypatch,
            unit_state=("activating", 0),
            stop_rc=1,
            stop_stderr="Job for pod.service failed",
        )
        err = capsys.readouterr().err
        assert stops == ["demo"], "the teardown is still attempted"
        assert "Tearing it down FAILED (rc=1)" in err
        assert "Job for pod.service failed" in err
        assert "kirocrew pod down demo" in err
        assert audits

    def test_a_clean_teardown_says_nothing_about_the_pod_surviving(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The inverse pin: a pod that WAS removed must not be described as kept."""
        self._fail_up(tmp_path, monkeypatch, unit_state=("activating", 0))
        err = capsys.readouterr().err
        assert "FAILED" not in err
        assert "NOT stopped" not in err
        assert "may still be running" not in err

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

    def test_serving_without_a_credential_is_reported_as_itself(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The verdict a slow runner produces must not read as a slow boot.

        The gateway was still serving when the budget expired, so "still starting"
        is false and doubling the wait is not the remedy -- the credential write is
        what failed. The pod is still stopped, because the caller contract is that
        `up` means usable, and a pod nothing can authenticate to is not.
        """
        audits, stops = self._fail_up(
            tmp_path,
            monkeypatch,
            unit_state=("active", 0),
            code=pod_cli.HEALTH_NO_CREDENTIAL,
        )
        err = capsys.readouterr().err
        assert "published no" in err and "internal-API credential" in err
        # The wording must describe the LAST observation, not the whole budget:
        # the verdict is keyed on the final poll, so claiming it served throughout
        # would assert something the wait never checked.
        assert "still serving" in err
        assert "for the whole" not in err
        # The remedy must be PRESENT. A late publish and a failed write both land
        # here, and a bigger budget is the fix for the first, so suppressing the
        # escalation would leave the operator with no move on that half.
        assert "--wait-secs 34" in err
        assert f"{pod_cli.POD_HEALTH_WAIT_SECS_ENV}=34" in err
        assert "still starting" not in err
        assert "never became healthy" not in err
        assert stops == ["demo"]
        assert any(
            k.get("error") == "serving but no internal-API credential published" for _a, k in audits
        )


class TestWaitHealthyRequiresTheCredential:
    """A serving port is not a ready pod.

    A pod's gateway publishes its internal-API credential only AFTER its listener
    is bound, so between the bind and the end of the remaining startup work the
    port already answers ``/api/health`` while the credential does not exist.
    ``_up`` mints immediately after this wait returns, so a wait that stopped at
    the HTTP status handed the mint a pod whose credential was still unwritten and
    died with "no internal-API credential ... is it running?" -- intermittently, on
    whichever runner happened to be slow, blaming a pod that was running fine.

    Pinned here with a fake clock and real files rather than a real gateway: the
    window is milliseconds wide on an idle host and seconds wide on a loaded one,
    so only a test that controls the ordering can pin it on every platform.
    """

    def _pin_clock(self, monkeypatch: pytest.MonkeyPatch) -> _Clock:
        clock = _Clock()
        monkeypatch.setattr(pod_cli.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(pod_cli.time, "sleep", clock.sleep)
        return clock

    def _secret_path(self, cfg: PodConfig, name: str, port: int) -> Path:
        return cfg.home_dir(name) / run_marker.RUN_DIR_NAME / run_marker.secret_file_name(port)

    def test_a_serving_gateway_is_not_ready_until_its_credential_lands(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact CI failure, ordered deliberately: health answers on the first
        poll, the credential appears three polls later. The wait must not return
        before it does -- returning early is what fed the mint an absent secret."""
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        secret = self._secret_path(cfg, "x", 7999)
        polls: list[int] = []

        def _health(_cfg: object, _n: object, _p: object) -> int:
            polls.append(1)
            if len(polls) == 4:
                secret.parent.mkdir(parents=True, exist_ok=True)
                secret.write_text("published-now")
            return 200

        monkeypatch.setattr(rt, "health", _health)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=30) == 200
        # Four polls: three that saw a serving-but-uncredentialled pod, and the
        # fourth that published. Returning on the first is the bug.
        assert len(polls) == 4

    def test_serving_the_whole_budget_without_a_credential_is_its_own_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Budget exhaustion must NOT return the 200 it kept seeing.

        Returning 200 would put ``_up`` back on the success path and straight into
        a mint that cannot succeed -- the original failure, just 90s later.
        """
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 200)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == pod_cli.HEALTH_NO_CREDENTIAL
        assert pod_cli.HEALTH_NO_CREDENTIAL not in (200, 401, 403)

    def test_the_credential_verdict_is_not_any_other_verdict(self) -> None:
        """All of these share ONE return channel, so a collision is silent.

        Spelling this one as the literal -2 put it on ``HEALTH_FOREIGN``'s value and
        ``_up`` told the operator to choose a different port for a pod whose port
        was fine -- caught by a test, not by review.
        """
        assert pod_cli.HEALTH_NO_CREDENTIAL not in (0, -1, rt.HEALTH_FOREIGN)
        assert pod_cli.HEALTH_NO_CREDENTIAL < 0

    def test_the_brief_serving_sentinel_collides_with_no_sibling(self) -> None:
        """Same one return channel, same silent-collision risk.

        Landing this on ``HEALTH_NO_CREDENTIAL`` would route a slow boot into the
        credential verdict and blame a write that never ran; landing it on 0 or -1
        would hand it back to the very unit read it exists to bypass.
        """
        siblings = (0, -1, rt.HEALTH_FOREIGN, pod_cli.HEALTH_NO_CREDENTIAL)
        assert pod_cli.HEALTH_SERVING_TOO_BRIEFLY not in siblings
        assert pod_cli.HEALTH_SERVING_TOO_BRIEFLY < 0
        assert pod_cli.HEALTH_SERVING_TOO_BRIEFLY not in (200, 401, 403)

    @pytest.mark.parametrize("gated", [401, 403])
    def test_a_gated_health_route_still_needs_the_credential(
        self, monkeypatch: pytest.MonkeyPatch, gated: int
    ) -> None:
        """401/403 mean "serving but gated", which counts as up -- so they are on
        exactly the same footing as 200 here, not a bypass."""
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: gated)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == pod_cli.HEALTH_NO_CREDENTIAL

    @pytest.mark.parametrize("blank", ["", "   \n"])
    def test_a_created_but_unwritten_credential_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch, blank: str
    ) -> None:
        """The file is created and then filled, so presence is not publication.

        A bare ``exists()`` check would call the pod ready during that gap and the
        mint -- which tests the same emptiness -- would then find nothing.
        """
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        secret = self._secret_path(cfg, "x", 7999)
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text(blank)
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 200)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == pod_cli.HEALTH_NO_CREDENTIAL

    def test_the_shared_local_secret_satisfies_readiness(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pod whose gateway predates the per-listener file publishes only the
        shared ``.local_secret``, and the mint falls back to it -- so readiness has
        to accept it too, or such a pod could never come up."""
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        home = cfg.home_dir("x")
        home.mkdir(parents=True, exist_ok=True)
        (home / ".local_secret").write_text("legacy-shared")
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 200)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == 200

    @pytest.mark.parametrize(
        "corrupt",
        [
            # Undecodable in cp1252 too (0x81 is undefined there), so this half
            # pins the ValueError catch on every host regardless of locale.
            b"\x81\xff\xfe not-utf8",
            # VALID cp1252 ('ÿþ...'), invalid UTF-8. This half pins the explicit
            # encoding: without it a cp1252 runner decodes this into a non-empty
            # string and calls the pod ready with a credential the gateway never
            # wrote -- green on a UTF-8 host, wrong on CI. That asymmetry is how
            # this test first went red on Windows while passing locally.
            b"\xff\xfe\x00not-utf8",
        ],
        ids=["undecodable-anywhere", "cp1252-decodable-utf8-invalid"],
    )
    def test_an_undecodable_credential_does_not_escape_the_boot_poll(
        self, monkeypatch: pytest.MonkeyPatch, corrupt: bytes
    ) -> None:
        """A corrupt credential reads as "not published", on every host locale.

        ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``, so it has to
        be caught explicitly or it escapes ``pod up``'s boot poll and replaces the
        boot's own diagnosis with a traceback.
        """
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        secret = self._secret_path(cfg, "x", 7999)
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_bytes(corrupt)
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 200)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == pod_cli.HEALTH_NO_CREDENTIAL

    def test_the_credential_read_does_not_depend_on_the_host_locale(self) -> None:
        """Pinned directly, because the locale default is silent and platform-split.

        ``read_text()`` with no encoding decodes with whatever the host prefers, so
        the same bytes are a valid credential on a cp1252 runner and an error on a
        UTF-8 one. Asserting the explicit encoding keeps that from drifting back.
        """
        import inspect

        source = inspect.getsource(rt._read_pod_secret_file)
        assert 'encoding="utf-8"' in source
        assert "(OSError, ValueError)" in source

    def test_a_corrupt_per_listener_file_falls_back_to_the_shared_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable candidate must not end the search.

        The per-listener file is read first, so treating "cannot decode it" as fatal
        would strand a pod whose shared ``.local_secret`` is perfectly good -- and
        for the mint it would be a traceback rather than its own named error.
        """
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        secret = self._secret_path(cfg, "x", 7999)
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_bytes(b"\x81\xff\xfe")
        (cfg.home_dir("x") / ".local_secret").write_text("good-shared-secret")
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 200)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == 200
        # And the mint agrees, which is the point of the shared reader.
        assert rt._pod_mint_secret(cfg, "x", 7999) == "good-shared-secret"

    def test_the_mint_reports_its_own_error_for_an_undecodable_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not a traceback: the mint's named PodError, naming both paths it tried."""
        cfg = PodConfig.load()
        secret = self._secret_path(cfg, "x", 7999)
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_bytes(b"\x81\xff\xfe")
        with pytest.raises(rt.PodError, match="no internal-API credential"):
            rt._pod_mint_secret(cfg, "x", 7999)

    def test_a_restart_count_does_not_condemn_a_gateway_that_is_answering(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live port outranks the unit's restart history, not the other way round.

        `restarts` is the unit's CUMULATIVE NRestarts, which nothing in this package
        resets, so a pod that ever auto-restarted carries it for good. Reading it
        while the port is SERVING would condemn a healthy recovered pod as a crash
        and the caller's cleanup would delete its home. The fast-fail is for a
        gateway that is not coming up; one that answers has come up, so the wait
        stays on the credential and ends in the credential verdict instead.
        """
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 200)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("activating", 1))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == pod_cli.HEALTH_NO_CREDENTIAL

    def test_a_foreign_holder_still_outranks_the_missing_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Somebody else on the port explains the absent credential, and names a
        remedy (choose a port) the credential verdict does not."""
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: rt.HEALTH_FOREIGN)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == rt.HEALTH_FOREIGN

    def test_a_broken_health_route_keeps_its_own_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """404/5xx never counted as serving, so they must not be re-labelled as a
        credential problem: the remedy for those is the health route, not the
        credential write."""
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 500)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == 500

    def test_a_gateway_that_binds_in_the_final_second_is_a_slow_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Binding at the end of the budget is not a failed credential write.

        The gap between the bind and the credential write is seconds wide on a
        loaded host, so a gateway that comes up in the final second of its budget is
        serving on the last poll while having had no chance at all to publish.
        Reporting that as the credential verdict blames a write that never ran and,
        because the credential branch is tested before the slow-boot one, would
        withhold the one remedy that helps: a bigger budget.
        """
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        polls: list[int] = []

        def _health(_cfg: object, _n: object, _p: object) -> int:
            polls.append(1)
            # Silent until the very last poll, then serving.
            return 200 if len(polls) >= 4 else 0

        monkeypatch.setattr(rt, "health", _health)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("activating", 0))
        code = pod_cli._wait_healthy(cfg, "x", 7999, tries=3)
        assert code != pod_cli.HEALTH_NO_CREDENTIAL
        # Its own verdict, not 0: 0 also means "nothing ever answered", and `_up`
        # resolves that one from the unit -- a read that condemns any pod whose
        # cumulative restart count is non-zero. Carrying the distinction is what
        # keeps a gateway last seen serving out of the never-healthy verdict.
        assert code == pod_cli.HEALTH_SERVING_TOO_BRIEFLY

    def test_serving_across_a_poll_still_earns_the_credential_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The span requirement must not swallow the verdict it guards.

        A gateway serving from the first poll to the deadline with no credential has
        been watched for the whole budget, which is exactly the evidence the verdict
        claims.
        """
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 200)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == pod_cli.HEALTH_NO_CREDENTIAL

    def test_a_serving_state_that_lapsed_is_timed_from_the_comeback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A gateway that served, dropped, and came back in the final second gets no
        credit for the earlier span: the pod the caller would be handed is the one
        that has just come back."""
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        polls: list[int] = []

        def _health(_cfg: object, _n: object, _p: object) -> int:
            polls.append(1)
            # Serving, then silent, then serving again on the last poll only.
            return 200 if len(polls) == 1 or len(polls) >= 4 else 0

        monkeypatch.setattr(rt, "health", _health)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) != pod_cli.HEALTH_NO_CREDENTIAL

    def test_a_recovered_pod_serving_without_its_new_credential_is_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cumulative restart count must not condemn a gateway that is answering.

        `unit_state` returns the unit's NRestarts, which nothing in this package
        resets, so a pod that ever auto-restarted carries `restarts > 0` forever.
        Consulting the crash fast-fail while the port is SERVING therefore turned a
        healthy recovered pod -- answering, new credential not yet published -- into
        the crash verdict, and the caller's cleanup then deleted the home of the pod
        that had just recovered.
        """
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        secret = self._secret_path(cfg, "x", 7999)
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("old-generation")
        polls: list[int] = []

        def _health(_cfg: object, _n: object, _p: object) -> int:
            polls.append(1)
            if len(polls) == 3:
                secret.write_text("recovered-generation")
            return 200

        monkeypatch.setattr(rt, "health", _health)
        # Cumulative restarts from an earlier auto-restart, plus a live unit.
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 4))
        code = pod_cli._wait_healthy(cfg, "x", 7999, tries=30, superseded="old-generation")
        assert code == 200
        assert len(polls) == 3

    def test_a_crash_is_still_detected_once_the_port_goes_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skipping the fast-fail while serving must not disable it.

        A gateway that is NOT answering and whose unit reports restarts is the
        crash-loop this bails out on early, and that verdict is unchanged.
        """
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 0)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 4))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=90) == -1

    def test_a_failed_unit_is_still_detected_once_the_port_goes_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same for the `failed` half of the fast-fail."""
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 0)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("failed", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=90) == -1

    def test_probe_latency_is_not_counted_as_time_spent_serving(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow 200 must not satisfy the span requirement by itself.

        The span exists to separate "watched it serve, waited, still no credential"
        from "it had no chance to publish yet". `health` does real I/O, so timing the
        observation from BEFORE the probe credits its latency as serving time, and one
        slow 200 then clears the span on its very first poll -- reporting a slow boot
        as a credential failure, the verdict that also tells the caller the gateway is
        healthy.
        """
        cfg = PodConfig.load()
        clock = {"t": 0.0}
        monkeypatch.setattr(pod_cli.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(pod_cli.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

        def _slow_health(_cfg: object, _n: object, _p: object) -> int:
            # A single probe that takes longer than the whole span requirement.
            clock["t"] += pod_cli._MIN_SERVING_SPAN_SECS + 2.0
            return 200

        monkeypatch.setattr(rt, "health", _slow_health)
        monkeypatch.setattr(rt, "published_credential", lambda cfg, n, p: "")
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        # A budget that the first slow probe already exhausts, so the verdict is
        # decided on one observation and cannot have watched the pod serve at all.
        code = pod_cli._wait_healthy(cfg, "x", 7999, tries=1)
        assert code != pod_cli.HEALTH_NO_CREDENTIAL, (
            "one slow probe cleared the serving span, so probe latency was still "
            "being counted as time this pod spent serving"
        )

    def test_the_foreign_latch_clears_when_the_port_changes_hands(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A predecessor seen early must not label a pod that later wins its port.

        Clearing requires `port_owner` to answer OWNER_POD -- positive proof the
        handover happened. A sticky latch reported FOREIGN for a pod demonstrably
        serving on its own port, which is both the wrong diagnosis and -- in `_up` --
        the wrong cleanup branch.
        """
        cfg = PodConfig.load()
        clock = {"t": 0.0}
        monkeypatch.setattr(pod_cli.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(pod_cli.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
        seen: list[int] = []

        def _health(_cfg: object, _n: object, _p: object) -> int:
            seen.append(1)
            # Predecessor holds the port, then this pod's gateway wins it back.
            return rt.HEALTH_FOREIGN if len(seen) == 1 else 200

        monkeypatch.setattr(rt, "health", _health)
        monkeypatch.setattr(rt, "port_owner", lambda cfg, n, p: rt.OWNER_POD)
        monkeypatch.setattr(rt, "published_credential", lambda cfg, n, p: "")
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        code = pod_cli._wait_healthy(cfg, "x", 7999, tries=6)
        assert code == pod_cli.HEALTH_NO_CREDENTIAL
        assert code != rt.HEALTH_FOREIGN

    def test_an_unproven_owner_does_not_clear_the_foreign_latch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Not provably foreign" is not "provably ours".

        `health` downgrades to HEALTH_FOREIGN only when ownership is PROVEN foreign,
        and hands back the status unchanged for OWNER_UNPROVEN -- a pid on the port
        with no fresh record behind it, which is what an unavailable or failed
        listener lookup leaves on a host that cannot see another process's sockets.
        Reading that serving code as proof of a handover would clear the latch for a
        port still held by somebody else, dropping the one attribution that sends the
        operator to the right diagnosis.
        """
        cfg = PodConfig.load()
        self._pin_clock(monkeypatch)
        seen: list[int] = []

        def _health(_cfg: object, _n: object, _p: object) -> int:
            seen.append(1)
            return rt.HEALTH_FOREIGN if len(seen) == 1 else 200

        monkeypatch.setattr(rt, "health", _health)
        monkeypatch.setattr(rt, "port_owner", lambda cfg, n, p: rt.OWNER_UNPROVEN)
        monkeypatch.setattr(rt, "published_credential", lambda cfg, n, p: "")
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=5) == rt.HEALTH_FOREIGN

    def test_ownership_is_not_re_attested_while_no_squatter_was_seen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The extra attestation is confined to the path whose verdict can change.

        `health` already consults `port_owner` internally; asking again on every
        serving poll would double the process lookups for every ordinary boot. The
        latch can only be cleared when it is set, so the second call is guarded on
        that and an untroubled boot pays nothing.
        """
        cfg = PodConfig.load()
        self._pin_clock(monkeypatch)
        calls: list[int] = []
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 200)
        monkeypatch.setattr(rt, "port_owner", lambda cfg, n, p: calls.append(1) or rt.OWNER_POD)
        monkeypatch.setattr(rt, "published_credential", lambda cfg, n, p: "")
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        pod_cli._wait_healthy(cfg, "x", 7999, tries=4)
        assert calls == [], "port_owner was re-attested with no foreign responder on record"

    def test_a_foreign_holder_that_never_yields_is_still_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clearing the latch on proof must not disable the verdict itself."""
        cfg = PodConfig.load()
        self._pin_clock(monkeypatch)
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: rt.HEALTH_FOREIGN)
        monkeypatch.setattr(rt, "published_credential", lambda cfg, n, p: "")
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=4) == rt.HEALTH_FOREIGN

    def test_the_gateway_mints_a_fresh_credential_per_generation(self) -> None:
        """Drift guard for the assumption `live != superseded` rests on.

        The supersede check can only tell a new generation's credential from its
        predecessor's if a starting gateway MINTS a new one rather than reusing what
        it finds on disk. Nothing in this package can enforce that -- the mint lives
        in the dashboard -- and the failure mode if it ever changes is silent and
        expensive: every reboot of a crashed pod would keep reading the stale secret
        as "not yet superseded" and burn the entire readiness budget before failing.
        Asserting the property at its source is what makes that coupling visible to
        whoever edits the mint.
        """
        server = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "server.py"
        )
        mints = [
            line.strip()
            for line in server.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("_internal_secret =")
        ]
        assert mints, "no internal-secret mint found in the dashboard server"
        for mint in mints:
            assert mint == "_internal_secret = os.urandom(16).hex()", (
                "every gateway entrypoint must mint a FRESH internal-API credential; "
                "pod up's supersede check reads a reused or derived one as the "
                f"predecessor's and waits out its whole budget -- found: {mint}"
            )

    def test_readiness_reads_the_same_files_the_mint_does(self) -> None:
        """The predicate and the mint must not drift apart.

        Two independently spelled path pairs is how a pod gets reported ready with
        a credential the mint then cannot find, so both go through one helper.
        """
        import inspect

        assert "_pod_secret_candidates" in inspect.getsource(rt.published_credential)
        assert "_pod_secret_candidates" in inspect.getsource(rt._pod_mint_secret)
        # And through the same reader, so the encoding and the tolerated errors
        # cannot diverge either -- a predicate that accepted a file the mint
        # rejects is the drift this pins.
        assert "_read_pod_secret_file" in inspect.getsource(rt.published_credential)
        assert "_read_pod_secret_file" in inspect.getsource(rt._pod_mint_secret)

    def test_a_stale_predecessor_credential_does_not_satisfy_a_fresh_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crashed pod's home keeps its secret, and that must not read as ready.

        ``clear_marker`` runs only on a graceful shutdown and the stale-marker prune
        deliberately never removes the credential, so rebooting a crashed pod finds
        the PREVIOUS generation's secret already on disk. Without the supersede
        check the wait is satisfied on its very first poll and hands the mint a
        credential the new gateway never minted -- the defect this whole wait exists
        to prevent, just with a stale value instead of a missing one.
        """
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        secret = self._secret_path(cfg, "x", 7999)
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("secret-of-the-crashed-generation")
        polls: list[int] = []

        def _health(_cfg: object, _n: object, _p: object) -> int:
            polls.append(1)
            if len(polls) == 3:
                secret.write_text("secret-of-the-new-generation")
            return 200

        monkeypatch.setattr(rt, "health", _health)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        code = pod_cli._wait_healthy(
            cfg, "x", 7999, tries=30, superseded="secret-of-the-crashed-generation"
        )
        assert code == 200
        # Three polls: two that saw only the dead generation's secret, and the
        # third that saw the new one. Returning on the first is the bug.
        assert len(polls) == 3

    def test_a_credential_that_lands_during_the_final_probes_is_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The verdict rests on a fresh read, not on one taken before the I/O.

        Every reading in a poll predates the work that poll does: the credential is
        read first, then `health` and `unit_state` each make a real call. Those calls
        are exactly as long as the bind-to-credential window this wait exists to
        cover, so a credential published while one of them runs is present but
        unseen -- and the exhausted-budget branch would then report that the gateway
        published none, on which `_up` tears down a pod it started.
        """
        clock = self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        secret = self._secret_path(cfg, "x", 7999)
        secret.parent.mkdir(parents=True, exist_ok=True)

        def _unit_state(_cfg: object, _n: object) -> tuple[str, int]:
            # Publishes while this call runs, and the call itself spends the rest of
            # the budget -- so the loop lands on the deadline with a stale reading.
            secret.write_text("published-during-the-unit-read")
            clock.sleep(30.0)
            return ("active", 0)

        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 200)
        monkeypatch.setattr(rt, "unit_state", _unit_state)
        code = pod_cli._wait_healthy(cfg, "x", 7999, tries=3)
        assert code == 200, (
            "the credential was on disk when the verdict was made, so reporting "
            f"{code} says the gateway published none about a pod that had"
        )

    def test_a_stale_credential_that_is_never_replaced_is_not_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The supersede check must also fail CLOSED, not just delay."""
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        secret = self._secret_path(cfg, "x", 7999)
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("stale")
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 200)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert (
            pod_cli._wait_healthy(cfg, "x", 7999, tries=3, superseded="stale")
            == pod_cli.HEALTH_NO_CREDENTIAL
        )

    def test_an_already_active_pod_keeps_its_current_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`superseded` empty means "no new generation", so today's secret is right.

        `up` against a running pod starts nothing. Waiting its credential out would
        burn the whole budget and then fail a pod that was serving correctly the
        entire time.
        """
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        secret = self._secret_path(cfg, "x", 7999)
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("already-serving")
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 200)
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == 200

    def test_up_captures_the_outgoing_credential_before_starting_the_pod(self) -> None:
        """Order matters: the capture has to precede `start_pod`.

        Read after the start and the new gateway may already have published, so the
        captured value IS the new one and the supersede check compares a credential
        against itself -- silently disabling the guard rather than failing.
        """
        import inspect

        source = inspect.getsource(pod_cli._up)
        capture = source.index("superseded_credential = rt.published_credential")
        start = source.index("cp = rt.start_pod(cfg, name)")
        assert capture < start, "the credential capture must come before start_pod"

    def test_a_gateway_that_served_once_then_died_is_not_a_credential_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attribution comes from the LAST observation, never from a latch.

        A gateway can answer 200 once inside the very bind-to-credential window this
        wait exists for and then die. A sticky "was ever serving" flag would report
        that as `never published an internal-API credential` -- the one verdict that
        tells the operator the gateway is healthy and the write is at fault.
        """
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        codes = iter([200])
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: next(codes, 0))
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == 0

    def test_a_gateway_that_served_once_then_serves_errors_keeps_the_error_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same rule with a broken health route rather than a dead process: the 500
        is what the operator has to fix, so it must not be relabelled."""
        self._pin_clock(monkeypatch)
        cfg = PodConfig.load()
        codes = iter([200])
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: next(codes, 500))
        monkeypatch.setattr(rt, "unit_state", lambda cfg, n: ("active", 0))
        assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == 500
