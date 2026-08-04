"""Tests for the macOS launchd pod backend.

These run on EVERY platform: like ``test_pod.py`` fakes ``systemctl``, they fake
``launchctl`` at the module boundary, so a Linux CI runner exercises the darwin
code path without launchd being present. That matters because the repo's macOS CI
job is deliberately narrow — without these, the backend would ship with no
coverage at all.
"""

from __future__ import annotations

import plistlib
import subprocess

import pytest

from kiro_crew.pod import launchd
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> PodConfig:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows Path.home() reads USERPROFILE, not HOME — without this the plist
    # dir resolved to the runner's REAL profile, and a plist left by one test
    # made another test's teardown sweep think the name had been reclaimed.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods-env"))
    monkeypatch.setenv("KIROCREW_POD_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    return PodConfig.load()


@pytest.fixture(autouse=True)
def _no_real_launchctl(monkeypatch):
    """Never shell out to a real launchctl, and let require_backend pass."""
    monkeypatch.setattr(launchd, "require_backend", lambda: None)


# --------------------------------------------------------------------------
# plist rendering
# --------------------------------------------------------------------------
def test_plist_declares_no_resource_ceiling(cfg):
    """macOS cannot enforce the cgroup ceiling, so the plist must not imply it.

    This is the documented decision, not an oversight: RLIMIT_AS bounds one
    process's address space, not resident memory and not the subprocess tree the
    gateway spawns, so any limit key here would read as the 4G guarantee while
    delivering something materially weaker. Asserted so nobody "improves" it.
    """
    body = launchd.render_plist(cfg, "smoke")
    for key in (
        "SoftResourceLimits",
        "HardResourceLimits",
        "MemoryMax",
        "CPUQuota",
        "ProcessType",
    ):
        assert key not in body, f"{key} must not be emitted — see launchd module docstring"


def test_plist_boots_the_named_pod_through_python(cfg):
    body = launchd.render_plist(cfg, "smoke")
    argv = body["ProgramArguments"]
    assert argv[-3:] == ["pod", "_run", "smoke"]
    assert body["Label"] == "dev.kirocrew.pod.kirocrew-pod.smoke"
    assert body["RunAtLoad"] is True
    # Restart on crash, but never fight a deliberate bootout.
    assert body["KeepAlive"] == {"SuccessfulExit": False}


def test_plist_routes_logs_to_files_since_launchd_has_no_journal(cfg):
    body = launchd.render_plist(cfg, "smoke")
    out, err = launchd.log_paths(cfg, "smoke")
    assert body["StandardOutPath"] == str(out)
    assert body["StandardErrorPath"] == str(err)


def test_plist_is_valid_and_written_per_pod(cfg):
    dst = launchd.write_plist(cfg, "smoke")
    assert dst.name == "dev.kirocrew.pod.kirocrew-pod.smoke.plist"
    # Deliberately NOT ~/Library/LaunchAgents: launchd loads that directory at
    # login, which would resurrect pods after a reboot — the systemd path is
    # transient (start, never enable) and macOS must match.
    assert dst.parent == cfg.pods_dir
    assert "LaunchAgents" not in str(dst)
    with dst.open("rb") as fh:
        parsed = plistlib.load(fh)
    assert parsed["Label"] == "dev.kirocrew.pod.kirocrew-pod.smoke"


def test_label_honours_a_hermetic_unit_prefix(cfg, monkeypatch):
    """A test plane must not be able to collide with a developer's real pods."""
    monkeypatch.setenv("KIROCREW_POD_UNIT_PREFIX", "kirocrew-pod-test")
    hermetic = PodConfig.load()
    assert launchd.pod_label(hermetic, "smoke") == "dev.kirocrew.pod.kirocrew-pod-test.smoke"
    # The default plane carries its prefix segment too: without it, the default
    # prefix was a strict prefix of every custom plane's labels and a hermetic
    # plane's pods surfaced in the default plane's listing (review finding).
    assert launchd.pod_label(cfg, "smoke") == "dev.kirocrew.pod.kirocrew-pod.smoke"
    assert not launchd.pod_label(cfg, "").startswith(launchd.pod_label(hermetic, ""))
    assert not launchd.pod_label(hermetic, "").startswith(launchd.pod_label(cfg, ""))


def test_env_selection_is_shared_with_the_systemd_backend(cfg):
    """Both backends must pin the SAME plane, or a pod boots differently per OS."""
    from kiro_crew.pod.config import environment_vars

    body = launchd.render_plist(cfg, "smoke")
    assert body.get("EnvironmentVariables") == environment_vars(cfg)


# --------------------------------------------------------------------------
# launchctl print parsing
# --------------------------------------------------------------------------
_RUNNING = "state = running\n\tpid = 4242\n\tlast exit code = 0\n"
_DEAD = "state = waiting\n\tlast exit code = 1\n"
_ABSENT = _cp(returncode=113, stderr="Could not find service \"x\" in domain")


def test_is_active_needs_a_live_pid(cfg, monkeypatch):
    monkeypatch.setattr(launchd, "launchctl", lambda *a, **k: _cp(_RUNNING))
    assert launchd.is_active(cfg, "smoke") is True


def test_is_active_false_when_loaded_but_dead(cfg, monkeypatch):
    """Loaded is not running — the distinction systemd's is-active makes for us."""
    monkeypatch.setattr(launchd, "launchctl", lambda *a, **k: _cp(_DEAD))
    assert launchd.is_active(cfg, "smoke") is False


def test_is_active_false_when_label_absent(cfg, monkeypatch):
    monkeypatch.setattr(launchd, "launchctl", lambda *a, **k: _ABSENT)
    assert launchd.is_active(cfg, "smoke") is False


def test_is_active_refuses_to_guess_on_an_operational_error(cfg, monkeypatch):
    """Review blocker round 4: a non-absent launchctl failure must not read as
    "not running" — down/removal guards would fail open and delete live state."""
    monkeypatch.setattr(
        launchd, "launchctl", lambda *a, **k: _cp(returncode=5, stderr="Input/output error")
    )
    with pytest.raises(launchd.LaunchdError):
        launchd.is_active(cfg, "smoke")


def test_unit_state_reports_active_with_a_pid(cfg, monkeypatch):
    monkeypatch.setattr(launchd, "launchctl", lambda *a, **k: _cp(_RUNNING))
    assert launchd.unit_state(cfg, "smoke") == ("active", 0)


def test_unit_state_preserves_the_crash_signal_without_nrestarts(cfg, monkeypatch):
    """launchd has no NRestarts; reporting 0 would lose the crash-loop signal
    that stops the up-path waiting on a pod that cannot boot."""
    monkeypatch.setattr(launchd, "launchctl", lambda *a, **k: _cp(_DEAD))
    state, restarts = launchd.unit_state(cfg, "smoke")
    assert state == "failed"
    assert restarts > 0


def test_unit_state_inactive_when_not_loaded(cfg, monkeypatch):
    monkeypatch.setattr(launchd, "launchctl", lambda *a, **k: _cp(returncode=1))
    assert launchd.unit_state(cfg, "smoke") == ("inactive", 0)


def test_active_names_filters_to_our_prefix_and_liveness(cfg, monkeypatch):
    domain_dump = (
        "dev.kirocrew.pod.kirocrew-pod.alpha\n"
        "dev.kirocrew.pod.kirocrew-pod.beta\n"
        "com.apple.something\n"
        "dev.other.pod.gamma\n"
    )

    def fake(*args, **kwargs):
        # `print <domain>` lists labels; `print <domain>/<label>` probes one.
        target = args[1] if len(args) > 1 else ""
        if target.count("/") <= 1:
            return _cp(domain_dump)
        return _cp(_RUNNING if target.endswith("alpha") else _DEAD)

    monkeypatch.setattr(launchd, "launchctl", fake)
    assert launchd.active_names(cfg) == {"alpha"}


# --------------------------------------------------------------------------
# journal stand-in + the teardown gap
# --------------------------------------------------------------------------
def test_recent_journal_tails_the_pod_log_files(cfg):
    out, err = launchd.log_paths(cfg, "smoke")
    out.parent.mkdir(parents=True, exist_ok=True)
    err.write_text("boom-1\nboom-2\n")
    out.write_text("hello\n")
    text = launchd.recent_journal(cfg, "smoke", lines=1)
    assert "boom-2" in text
    assert "boom-1" not in text  # honours the line cap
    assert "hello" in text


def test_recent_journal_says_why_it_is_empty(cfg):
    text = launchd.recent_journal(cfg, "smoke")
    assert "no pod log yet" in text


def test_orphan_homes_reports_only_reapable_leftovers(cfg, monkeypatch):
    """With no ExecStopPost, a crashed pod leaves its HOME; ls/down GC it."""
    cfg.pod_root.mkdir(parents=True, exist_ok=True)
    (cfg.pod_root / "orphan").mkdir()
    (cfg.pod_root / "running").mkdir()
    (cfg.pod_root / "installed").mkdir()
    (cfg.pod_root / ".e2e-artifacts").mkdir()  # dot dirs are not pods
    launchd.write_plist(cfg, "installed")
    monkeypatch.setattr(launchd, "active_names", lambda c: {"running"})
    assert launchd.orphan_homes(cfg) == ["orphan"]


# --------------------------------------------------------------------------
# dispatch: the runtime must route to launchd on darwin and nowhere else
# --------------------------------------------------------------------------
def test_runtime_dispatches_to_launchd_on_macos(cfg, monkeypatch):
    monkeypatch.setattr(rt, "IS_MACOS", True)
    monkeypatch.setattr(rt.launchd, "is_active", lambda c, n: "sentinel")
    assert rt.is_active(cfg, "smoke") == "sentinel"


def test_runtime_does_not_touch_launchd_off_macos(cfg, monkeypatch):
    """The Linux/Windows contract: dispatch must not reach the launchd module."""
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "require_systemd", lambda: None)
    monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))

    def explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("launchd was consulted on a non-darwin host")

    monkeypatch.setattr(rt.launchd, "is_active", explode)
    assert rt.is_active(cfg, "smoke") is True


def test_stop_pod_reaps_the_home_on_macos(cfg, monkeypatch):
    """launchd has no ExecStopPost, so stop_pod owns the teardown obligation.

    The reap sweeps the WHOLE grace window (a dying child can flush state after
    a clean early sample — observed live) plus one final authoritative pass, so
    cleanup_home runs multiple times by design.
    """
    monkeypatch.setattr(rt, "IS_MACOS", True)
    monkeypatch.setattr(rt.launchd, "stop", lambda c, n: _cp(returncode=0))
    monkeypatch.setattr(rt.time, "sleep", lambda _s: None)
    reaped: list[str] = []
    monkeypatch.setattr(rt, "cleanup_home", lambda c, n: reaped.append(n))
    cp = rt.stop_pod(cfg, "smoke")
    assert cp.returncode == 0
    assert set(reaped) == {"smoke"}
    assert len(reaped) == 7  # 6 sweeps + 1 final authoritative pass


def test_stop_waits_for_the_asynchronous_unload(cfg, monkeypatch):
    """`bootout` returns before launchd has unloaded.

    Found by a real bring-up: returning immediately let the caller reap the pod's
    HOME while the gateway was still alive, the removal failed silently, and the
    CLI reported zero residue over a HOME still on disk.
    """
    seen: list[str] = []
    # loaded, loaded, then gone
    prints = [_cp("state = running\n\tpid = 1\n"), _cp("state = waiting\n"), _ABSENT]

    def fake(*args, **kwargs):
        verb = args[0] if args else ""
        seen.append(verb)
        if verb == "bootout":
            return _cp(returncode=0)
        return prints.pop(0) if prints else _cp(returncode=1)

    monkeypatch.setattr(launchd, "launchctl", fake)
    monkeypatch.setattr(launchd.time, "sleep", lambda _s: None)
    launchd.write_plist(cfg, "smoke")
    launchd.stop(cfg, "smoke")
    assert seen[0] == "bootout"
    # It kept polling `print` until the label disappeared.
    assert seen.count("print") >= 3


def test_stop_removes_the_per_pod_plist_once_unloaded(cfg, monkeypatch):
    """A launchd plist is per-pod, unlike systemd's machine-wide template."""
    # explicit absent-service result -> unload confirmed immediately
    monkeypatch.setattr(launchd, "launchctl", lambda *a, **k: _ABSENT)
    dst = launchd.write_plist(cfg, "smoke")
    assert dst.exists()
    cp = launchd.stop(cfg, "smoke")
    assert cp.returncode == 0
    assert not dst.exists()


def test_stop_does_not_treat_a_generic_print_failure_as_unloaded(cfg, monkeypatch):
    """Review blocker round 2: an OPERATIONAL print failure (rc!=0 without the
    absent-service message) proves nothing about the label. Confirming the
    unload on it would let teardown proceed against a possibly-live pod."""
    monkeypatch.setattr(launchd, "launchctl", lambda *a, **k: _cp(returncode=5, stderr="Input/output error"))
    monkeypatch.setattr(launchd.time, "sleep", lambda _s: None)
    dst = launchd.write_plist(cfg, "smoke")
    cp = launchd.stop(cfg, "smoke", timeout=0.5)
    assert cp.returncode != 0
    assert dst.exists()


def test_stop_preserves_everything_when_the_unload_cannot_be_confirmed(cfg, monkeypatch):
    """Blocking review finding: a failed/hung unload must NOT tear anything down.

    If the label is still loaded when the wait expires, the gateway may be
    alive — deleting its plist (or letting the caller delete its HOME) while
    returning success would destroy live state.
    """
    monkeypatch.setattr(launchd, "launchctl", lambda *a, **k: _cp("state = running\n\tpid = 4\n"))
    monkeypatch.setattr(launchd.time, "sleep", lambda _s: None)
    dst = launchd.write_plist(cfg, "smoke")
    cp = launchd.stop(cfg, "smoke", timeout=0.5)
    assert cp.returncode != 0
    assert "preserved" in cp.stderr
    assert dst.exists()


def test_stop_pod_does_not_reap_the_home_on_a_failed_unload(cfg, monkeypatch):
    """runtime.stop_pod must honour launchd.stop's authoritative failure."""
    monkeypatch.setattr(rt, "IS_MACOS", True)
    monkeypatch.setattr(
        rt.launchd, "stop", lambda c, n: _cp(returncode=1, stderr="preserved")
    )
    reaped: list[str] = []
    monkeypatch.setattr(rt, "cleanup_home", lambda c, n: reaped.append(n))
    cp = rt.stop_pod(cfg, "smoke")
    assert cp.returncode != 0
    assert reaped == []


def test_stop_pod_reports_a_surviving_home_instead_of_claiming_zero_residue(cfg, monkeypatch):
    """cleanup_home deletes with ignore_errors, so its failure is SILENT.

    On systemd, ExecStopPost owns teardown and a failure shows up as a unit
    failure. Here nothing would notice, so stop_pod must verify rather than trust
    — otherwise `pod down` prints "zero residue" over a HOME that is still there.
    """
    monkeypatch.setattr(rt, "IS_MACOS", True)
    monkeypatch.setattr(rt.launchd, "stop", lambda c, n: _cp(returncode=0))
    monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)  # pretend it silently failed
    monkeypatch.setattr(rt.time, "sleep", lambda _s: None)  # collapse the grace window
    home = rt.pod_home(cfg, "smoke")
    home.mkdir(parents=True, exist_ok=True)
    cp = rt.stop_pod(cfg, "smoke")
    assert cp.returncode != 0
    assert "reappearing" in cp.stderr
    assert str(home) in cp.stderr


def test_stop_pod_succeeds_when_the_home_is_gone(cfg, monkeypatch):
    """A confirmed unload with the HOME already gone is a clean success."""
    monkeypatch.setattr(rt, "IS_MACOS", True)
    monkeypatch.setattr(rt.launchd, "stop", lambda c, n: _cp(returncode=0))
    monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
    cp = rt.stop_pod(cfg, "smoke")
    assert cp.returncode == 0


def test_reap_sweep_aborts_when_a_new_pod_claims_the_name(cfg, monkeypatch):
    """Blocking review finding round 3: down and up are independent endpoints
    with no per-name lock, so the grace sweep must stand down the moment a NEW
    pod claims the name — otherwise its live HOME lands under our rmtree."""
    monkeypatch.setattr(rt, "IS_MACOS", True)
    monkeypatch.setattr(rt.launchd, "stop", lambda c, n: _cp(returncode=0))
    monkeypatch.setattr(rt.time, "sleep", lambda _s: None)
    # the new pod writes its plist during our sweep
    launchd.write_plist(cfg, "smoke")
    reaped: list[str] = []
    monkeypatch.setattr(rt, "cleanup_home", lambda c, n: reaped.append(n))
    cp = rt.stop_pod(cfg, "smoke")
    assert cp.returncode == 0
    assert rt.RECLAIMED_MARKER in cp.stdout  # callers must know not to delete per-name state
    assert reaped == []  # never touched the reclaimed name


def test_down_preserves_the_new_pods_checkout_pin_when_reclaimed(cfg, monkeypatch, capsys):
    """Review blocker round 3 (part 2): after a reclaimed teardown, `down` must
    NOT delete the per-pod env file — it pins the NEW pod's checkout."""
    import argparse

    from kiro_crew.pod import cli as pod_cli

    monkeypatch.setattr(rt, "validate_name", lambda n: n)
    monkeypatch.setattr(rt, "is_active", lambda c, n: True)
    monkeypatch.setattr(
        rt, "stop_pod", lambda c, n: _cp(returncode=0, stdout=rt.RECLAIMED_MARKER)
    )
    monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
    env = cfg.env_file("smoke")
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("CHECKOUT=/the/new/pods/checkout\n")
    pod_cli._down(cfg, argparse.Namespace(name="smoke"))
    assert env.exists(), "the reclaimed name's env file belongs to the NEW pod"
    assert "reclaimed" in capsys.readouterr().out


def test_install_backend_writes_nothing_on_macos(cfg, monkeypatch):
    """No template units on launchd — saying so beats writing an inert file."""
    monkeypatch.setattr(rt, "IS_MACOS", True)
    monkeypatch.setattr(rt, "require_backend", lambda: None)
    msg, reload_cp = rt.install_backend(cfg)
    assert reload_cp is None
    assert "nothing to install on macOS" in msg


def test_active_names_refuses_to_guess_on_an_operational_error(cfg, monkeypatch):
    """Review blocker round 4 (same class as is_active): an empty set on a failed
    domain print would tell pod ls / Dev Fleet that live pods are absent."""
    monkeypatch.setattr(launchd, "launchctl", lambda *a, **k: _cp(returncode=1, stderr="boom"))
    with pytest.raises(launchd.LaunchdError):
        launchd.active_names(cfg)


def test_start_and_stop_hold_the_per_name_mutex(cfg, monkeypatch):
    """Review blocker round 4: start (plist write + bootstrap) and the whole
    stop (bootout + sweep) must serialize per name, or a down/up race deletes
    the replacement pod's plist and HOME."""
    import contextlib as _ctx

    held: list[str] = []

    @_ctx.contextmanager
    def _fake_mutex(c, n):
        held.append(f"enter:{n}")
        yield
        held.append(f"exit:{n}")

    monkeypatch.setattr(rt.launchd, "pod_mutex", _fake_mutex)
    monkeypatch.setattr(rt, "IS_MACOS", True)
    monkeypatch.setattr(rt.launchd, "write_plist", lambda c, n: None)
    monkeypatch.setattr(rt.launchd, "start", lambda c, n: _cp(returncode=0))
    rt.start_pod(cfg, "smoke")
    assert held == ["enter:smoke", "exit:smoke"]

    held.clear()
    monkeypatch.setattr(rt.launchd, "stop", lambda c, n: _cp(returncode=0))
    monkeypatch.setattr(rt, "cleanup_home", lambda c, n: None)
    monkeypatch.setattr(rt.time, "sleep", lambda _s: None)
    rt.stop_pod(cfg, "smoke")
    assert held == ["enter:smoke", "exit:smoke"], "the sweep must run INSIDE the mutex"


def test_runtime_probes_surface_launchd_errors_as_pod_errors(cfg, monkeypatch):
    """The CLI's contract is PodError -> `pod: <msg>` + exit 1; a raw
    LaunchdError from a probe would crash with a traceback instead."""
    monkeypatch.setattr(rt, "IS_MACOS", True)
    monkeypatch.setattr(launchd, "launchctl", lambda *a, **k: _cp(returncode=5, stderr="EIO"))
    with pytest.raises(rt.PodError):
        rt.is_active(cfg, "smoke")
    with pytest.raises(rt.PodError):
        rt.active_names(cfg)


def test_pod_mutex_is_reentrant_within_a_thread(cfg):
    """The CLI holds the mutex across a transaction while start_pod/stop_pod
    re-acquire it inside; flock is per open-file-description, so without
    reentrancy that inner acquisition would deadlock against our own lock."""
    with launchd.pod_mutex(cfg, "smoke"):
        with launchd.pod_mutex(cfg, "smoke"):  # must not block
            pass


def test_down_fails_on_macos_when_stop_cannot_confirm_even_if_not_active(cfg, monkeypatch):
    """Review blocker round 4: a loaded-but-dead agent has no pid (was_up False)
    but its unload still needs confirming — a swallowed nonzero stop deleted the
    checkout pin while leaving service, plist and HOME behind."""
    import argparse

    from kiro_crew.pod import cli as pod_cli

    monkeypatch.setattr(rt, "IS_MACOS", True)
    monkeypatch.setattr(rt, "validate_name", lambda n: n)
    monkeypatch.setattr(rt, "is_active", lambda c, n: False)  # loaded-but-dead
    monkeypatch.setattr(
        rt, "stop_pod", lambda c, n: _cp(returncode=1, stderr="unload not confirmed")
    )
    monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
    env = cfg.env_file("smoke")
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("CHECKOUT=/live/checkout\n")
    with pytest.raises(SystemExit):
        pod_cli._down(cfg, argparse.Namespace(name="smoke"))
    assert env.exists(), "metadata must survive an unconfirmed unload"


def test_up_pins_the_checkout_inside_the_name_mutex(cfg, monkeypatch, tmp_path):
    """Review blocker round 4: the pin must move atomically with the start —
    pinned outside the lock, a concurrent down's sweep deleted the fresh pin."""
    import argparse
    import contextlib as _ctx

    from kiro_crew.pod import cli as pod_cli

    events: list[str] = []

    @_ctx.contextmanager
    def _tracked_mutex(c, n):
        events.append("lock")
        yield
        events.append("unlock")

    checkout = tmp_path / "wt"
    (checkout / "website" / "static" / "dist").mkdir(parents=True)
    monkeypatch.setattr(rt, "pod_name_mutex", _tracked_mutex)
    monkeypatch.setattr(rt, "validate_name", lambda n: n)
    monkeypatch.setattr(pod_cli, "_resolve_or_die", lambda c, n: checkout)
    monkeypatch.setattr(pod_cli.prov, "has_venv", lambda co: True)
    monkeypatch.setattr(pod_cli.prov, "has_dist", lambda co: True)
    monkeypatch.setattr(rt, "derive_port", lambda c, n: 7999)
    monkeypatch.setattr(rt, "pin_checkout", lambda c, n, co: events.append("pin"))
    monkeypatch.setattr(rt, "is_active", lambda c, n: False)
    monkeypatch.setattr(rt, "start_pod", lambda c, n: (events.append("start"), _cp())[1])
    monkeypatch.setattr(rt, "mint_token", lambda c, n, ttl: "t")
    monkeypatch.setattr(pod_cli, "_wait_healthy", lambda c, n, p: (events.append("wait"), 200)[1])
    monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
    args = argparse.Namespace(
        name="smoke", seed=None, json=True, provision=False, checkout=None, ttl="2h"
    )
    try:
        pod_cli._up(cfg, args)
    except SystemExit:
        pass
    assert "pin" in events and "start" in events and "wait" in events
    assert (
        events.index("lock")
        < events.index("pin")
        < events.index("start")
        < events.index("wait")
        < events.index("unlock")
    ), f"pin, start and the health wait must all happen inside the mutex: {events}"


def test_up_failure_cleanup_stops_the_pod_inside_the_mutex(cfg, monkeypatch, tmp_path):
    """Verifier-found High: with the lock released before the health wait, a
    down + replacement up could interleave and the failure cleanup's stop_pod
    unloaded and erased the REPLACEMENT pod."""
    import argparse
    import contextlib as _ctx

    from kiro_crew.pod import cli as pod_cli

    events: list[str] = []

    @_ctx.contextmanager
    def _tracked_mutex(c, n):
        events.append("lock")
        try:
            yield
        finally:
            events.append("unlock")

    checkout = tmp_path / "wt"
    (checkout / "website" / "static" / "dist").mkdir(parents=True)
    monkeypatch.setattr(rt, "pod_name_mutex", _tracked_mutex)
    monkeypatch.setattr(rt, "validate_name", lambda n: n)
    monkeypatch.setattr(pod_cli, "_resolve_or_die", lambda c, n: checkout)
    monkeypatch.setattr(pod_cli.prov, "has_venv", lambda co: True)
    monkeypatch.setattr(pod_cli.prov, "has_dist", lambda co: True)
    monkeypatch.setattr(rt, "derive_port", lambda c, n: 7999)
    monkeypatch.setattr(rt, "pin_checkout", lambda c, n, co: None)
    monkeypatch.setattr(rt, "is_active", lambda c, n: False)
    monkeypatch.setattr(rt, "start_pod", lambda c, n: _cp())
    monkeypatch.setattr(rt, "recent_journal", lambda c, n, lines: "")
    monkeypatch.setattr(rt, "stop_pod", lambda c, n: (events.append("stop"), _cp())[1])
    monkeypatch.setattr(pod_cli, "_wait_healthy", lambda c, n, p: -1)  # boot failed
    monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
    args = argparse.Namespace(
        name="smoke", seed=None, json=True, provision=False, checkout=None, ttl="2h"
    )
    with pytest.raises(SystemExit):
        pod_cli._up(cfg, args)
    assert "stop" in events, "the failed boot must be stopped"
    assert events.index("lock") < events.index("stop") < events.index(
        "unlock"
    ), f"the failure cleanup must stop the pod INSIDE the mutex: {events}"


def test_ls_translates_orphan_probe_failures_to_the_documented_error(cfg, monkeypatch):
    """A LaunchdError escaping pod ls printed a traceback instead of the
    documented one-line `pod: <msg>` refusal (review finding)."""
    import argparse

    from kiro_crew.pod import cli as pod_cli

    monkeypatch.setattr(rt, "IS_MACOS", True)
    monkeypatch.setattr(rt, "active_names", lambda c: set())

    def _boom(c):
        raise launchd.LaunchdError("launchctl print failed")

    monkeypatch.setattr(launchd, "orphan_homes", _boom)
    with pytest.raises(rt.PodError):
        pod_cli._ls(cfg, argparse.Namespace(json=False))
