"""Tests for the Windows Task Scheduler pod backend.

Two halves, deliberately:

**Platform-independent.** Rendering, the pid record's start-time binding, the
``unit_state`` result mapping, the runtime dispatch and the teardown contract all
run on EVERY platform — like ``test_pod.py`` fakes ``systemctl`` and
``test_pod_launchd.py`` fakes ``launchctl``, these fake ``schtasks`` at the module
boundary and pin the argv. That is what gives the backend coverage on the Linux
matrix, where the bulk of CI lives.

**Real win32.** The handful of tests under ``skipif(not IS_WINDOWS)`` shell out to
the actual ``schtasks.exe`` and create a real throwaway task. Those are the only
ones that can answer "can this user supervise a pod at all", which is precisely
the question the fakes cannot.
"""

from __future__ import annotations

import subprocess

import pytest

from kiro_crew.platform_compat import IS_WINDOWS
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import windows as win
from kiro_crew.pod.config import EXIT_REFUSED_UNRECOVERABLE, PodConfig
from kiro_crew.subprocess_utf8 import UTF8_TEXT

requires_windows = pytest.mark.skipif(
    not IS_WINDOWS, reason="drives the real schtasks.exe, which only exists on win32"
)


def _no_successor(tmp_path):
    """A gateway pid sidecar path with nothing at it.

    ``supervise_gateway`` reads that sidecar after the gateway it spawned exits,
    to see whether an in-app restart (``os.execv``, which on Windows spawns a
    successor and exits the caller) left a replacement serving. An absent file is
    the "no successor" answer, which is what every test here means: they assert
    the ordinary spawn-and-exit contract, not the restart handover.
    """
    return tmp_path / "no-such-gateway.pid"


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> PodConfig:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows Path.home() reads USERPROFILE, not HOME. Same reason the launchd
    # suite sets both: without it the plane resolves into the runner's real
    # profile and one test's leftovers change another's verdict.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods-env"))
    monkeypatch.setenv("KIROCREW_POD_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    c = PodConfig.load()
    c.pods_dir.mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture(autouse=True)
def _no_real_schtasks(monkeypatch):
    """Never shell out to a real schtasks, and let the gate pass.

    Scoped to the module's own symbol so a test that WANTS the real binary (the
    ``requires_windows`` group) can undo it by calling through
    ``win._schtasks_raw`` with an explicitly resolved path.
    """
    monkeypatch.setattr(win, "require_backend", lambda: None)


# --------------------------------------------------------------------------
# The generated .cmd wrapper
# --------------------------------------------------------------------------
def test_the_wrapper_pins_the_pod_plane_because_a_task_carries_no_env(cfg):
    """The whole reason the wrapper exists.

    A scheduled task has no ``Environment=`` / ``EnvironmentVariables`` analogue,
    so a plane the CLI resolved would be lost and the booted gateway would read a
    DIFFERENT PodConfig. Every non-default key must therefore appear as a `set`.
    """
    body = win.render_task_script(cfg, "smoke")
    from kiro_crew.pod.config import environment_vars

    selected = environment_vars(cfg)
    assert selected, "the hermetic fixture plane must differ from the defaults"
    for key, value in selected.items():
        assert f'set "{key}={value}"' in body, f"{key} is not pinned into the wrapper"


def test_the_wrapper_boots_the_named_pod_through_python(cfg):
    body = win.render_task_script(cfg, "smoke")
    # Boot logic stays in kiro_crew.pod.runtime.boot; the wrapper only re-enters
    # the entry point, exactly as the systemd ExecStart and the launchd
    # ProgramArguments do. Nothing shell-shaped is shipped.
    assert 'pod _run "smoke"' in body


def test_the_wrapper_records_the_exit_code_because_there_is_no_restart_policy(cfg):
    """``unit_state``'s crash signal is derived from this file, so it is required.

    Task Scheduler retries a failed START, not a non-zero exit, and exposes no
    per-exit-code state in any locale-independent form. The recorded code is the
    only crash signal ``_wait_healthy`` can stop on.
    """
    body = win.render_task_script(cfg, "smoke")
    result = str(win.result_path(cfg, "smoke"))
    # Cleared BEFORE the boot so the previous run's failure cannot be read as
    # this one's, and captured into RC before anything else can clobber
    # ERRORLEVEL.
    assert f'del /q "{result}" 2>nul' in body
    assert 'set "RC=%ERRORLEVEL%"' in body
    assert f'> "{result}" echo %RC%' in body
    assert body.rstrip().endswith("exit /b %RC%")


def test_the_wrapper_routes_logs_to_files_since_there_is_no_journal(cfg):
    body = win.render_task_script(cfg, "smoke")
    out, err = win.log_paths(cfg, "smoke")
    assert f'>> "{out}"' in body
    assert f'2>> "{err}"' in body
    # The gateway child inherits these handles (supervise_gateway does not
    # redirect), which is what puts its output in `pod logs`.
    assert f'if not exist "{out.parent}" mkdir "{out.parent}"' in body


def test_the_wrapper_disables_delayed_expansion(monkeypatch, tmp_path):
    """A `!` in a pod-plane path stays literal."""
    weird = tmp_path / "pod!root"
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(weird))
    cfg = PodConfig.load()
    cfg.pods_dir.mkdir(parents=True, exist_ok=True)
    body = win.render_task_script(cfg, "smoke")
    assert body.splitlines()[2] == "setlocal DisableDelayedExpansion"
    assert f'set "KIROCREW_POD_ROOT={weird}"' in body


def test_a_percent_in_the_plane_is_escaped_not_expanded(cfg, monkeypatch, tmp_path):
    """`%` is the one cmd.exe metacharacter a path can legally contain."""
    weird = tmp_path / "pod%root"
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(weird))
    c = PodConfig.load()
    c.pods_dir.mkdir(parents=True, exist_ok=True)
    body = win.render_task_script(c, "smoke")
    assert "pod%%root" in body
    assert "pod%root" not in body.replace("pod%%root", "")


def test_a_quote_in_the_plane_is_refused_at_up_not_at_boot(cfg, monkeypatch, tmp_path):
    """cmd.exe has no escape for a quote inside a quoted token.

    Emitting it anyway would silently change the value the gateway is booted
    with, so this must fail loudly while the operator is still at the `pod up`
    prompt.
    """
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / 'pod"root'))
    c = PodConfig.load()
    with pytest.raises(win.WindowsTaskError, match="cmd.exe cannot quote"):
        win.render_task_script(c, "smoke")


def test_the_wrapper_is_written_per_pod_beside_the_env_file(cfg):
    dst = win.write_task_script(cfg, "smoke")
    assert dst.parent == cfg.pods_dir
    assert dst.name == "kirocrew-pod.smoke.cmd"
    # CRLF: a batch file with bare LF line endings is not reliably parsed by
    # cmd.exe, and newline="" keeps Python from translating what is already CRLF.
    # Asserted on BYTES: read_text() universal-newline-translates CRLF back to LF
    # on POSIX, so a text read here would pass even if the writer had emitted LF.
    assert b"\r\n" in dst.read_bytes()
    assert b"\n\n" not in dst.read_bytes()


def test_the_task_name_honours_a_hermetic_unit_prefix(cfg, monkeypatch):
    """A test plane must not be able to collide with a developer's real pods.

    Same property cfg.unit_prefix buys on the other two backends: the prefix is
    its own folder segment, so no plane's task path is a prefix of another's.
    """
    monkeypatch.setenv("KIROCREW_POD_UNIT_PREFIX", "kctest-pod")
    c = PodConfig.load()
    assert win.task_name(c, "smoke") == r"\KiroCrew\pods\kctest-pod\smoke"
    assert win.task_script_path(c, "smoke").name == "kctest-pod.smoke.cmd"


# --------------------------------------------------------------------------
# The supervised pid record — this platform's MainPID
# --------------------------------------------------------------------------
def test_the_pid_record_proves_its_own_freshness(cfg):
    """A bare pid is not an identity; a recycled one must not attest."""
    import os

    win.record_supervised_pid(cfg, "smoke", os.getpid())
    assert win.supervised_pid(cfg, "smoke") == os.getpid()
    assert win.main_pid(cfg, "smoke") == os.getpid()
    assert win.is_active(cfg, "smoke") is True


def test_a_record_whose_start_identity_does_not_match_is_refused(cfg):
    """The PID-reuse guard: fail CLOSED, never report the wrong process."""
    import os

    win.pid_record_path(cfg, "smoke").write_text(f"{os.getpid()}\nnot-the-real-token\n")
    assert win.supervised_pid(cfg, "smoke") is None
    assert win.is_active(cfg, "smoke") is False


def test_a_record_with_no_start_identity_is_refused(cfg):
    """ "This host would not say" must read as unproven, not as a match."""
    import os

    win.pid_record_path(cfg, "smoke").write_text(f"{os.getpid()}\n")
    assert win.supervised_pid(cfg, "smoke") is None


def test_a_missing_or_junk_record_reads_as_no_process(cfg):
    assert win.supervised_pid(cfg, "smoke") is None
    win.pid_record_path(cfg, "smoke").write_text("not-a-pid\n")
    assert win.supervised_pid(cfg, "smoke") is None
    win.pid_record_path(cfg, "smoke").write_text("")
    assert win.supervised_pid(cfg, "smoke") is None


def test_clearing_the_record_reports_the_pod_down(cfg):
    import os

    win.record_supervised_pid(cfg, "smoke", os.getpid())
    win.clear_supervised_pid(cfg, "smoke")
    assert win.main_pid(cfg, "smoke") is None


# --------------------------------------------------------------------------
# unit_state — the crash signal without a restart counter
# --------------------------------------------------------------------------
def test_unit_state_reports_active_while_the_gateway_lives(cfg):
    import os

    win.record_supervised_pid(cfg, "smoke", os.getpid())
    assert win.unit_state(cfg, "smoke") == ("active", 0)


def test_unit_state_derives_failed_from_a_nonzero_result_and_a_dead_pid(cfg):
    """The signal `_wait_healthy` stops on, since nothing restarts a crashed pod."""
    win.result_path(cfg, "smoke").write_text("1\n")
    assert win.unit_state(cfg, "smoke") == ("failed", 1)


def test_unit_state_is_inactive_on_a_clean_exit(cfg):
    win.result_path(cfg, "smoke").write_text("0\n")
    assert win.unit_state(cfg, "smoke") == ("inactive", 0)


def test_unit_state_is_inactive_when_nothing_was_ever_recorded(cfg):
    assert win.unit_state(cfg, "smoke") == ("inactive", 0)


def test_a_live_pid_outranks_a_stale_failure_record(cfg):
    """A restarted name must not read as failed because an older boot did."""
    import os

    win.result_path(cfg, "smoke").write_text("70\n")
    win.record_supervised_pid(cfg, "smoke", os.getpid())
    assert win.unit_state(cfg, "smoke") == ("active", 0)


def test_last_result_ignores_a_non_numeric_body(cfg):
    win.result_path(cfg, "smoke").write_text("ECHO is off.\n")
    assert win.last_result(cfg, "smoke") is None


# --------------------------------------------------------------------------
# Enumeration and logs
# --------------------------------------------------------------------------
def test_active_names_lists_only_pods_with_a_live_process(cfg):
    """Not a schtasks listing: its status column is localized (fail-OPEN)."""
    import os

    win.record_supervised_pid(cfg, "alive", os.getpid())
    win.pid_record_path(cfg, "stale").write_text("999999999\nbogus\n")
    assert win.active_names(cfg) == {"alive"}


def test_active_names_ignores_another_planes_records(cfg, monkeypatch):
    import os

    win.record_supervised_pid(cfg, "mine", os.getpid())
    (cfg.pods_dir / "other-plane.theirs.winpid").write_text(f"{os.getpid()}\nx\n")
    assert win.active_names(cfg) == {"mine"}


def test_active_names_handles_a_dotted_pod_name(cfg):
    """validate_name allows '.', and the plane prefix is fixed, so this is exact."""
    import os

    win.record_supervised_pid(cfg, "feat.win", os.getpid())
    assert win.active_names(cfg) == {"feat.win"}


def test_recent_journal_tails_the_pod_log_files(cfg):
    out, err = win.log_paths(cfg, "smoke")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("boot line\n")
    err.write_text("FATAL: nope\n")
    text = win.recent_journal(cfg, "smoke")
    assert "FATAL: nope" in text and "boot line" in text


def test_recent_journal_says_why_it_is_empty(cfg):
    assert "no pod log yet" in win.recent_journal(cfg, "smoke")


# --------------------------------------------------------------------------
# The exit-code contract
# --------------------------------------------------------------------------
def test_the_runtime_wrapper_does_not_translate_on_windows(cfg, monkeypatch):
    """The launchd twin translates 78 to 0; this platform must NOT.

    Pinned on ``terminal_exit_code`` -- the function callers actually reach --
    because that is where the decision lives, and a change that gives the task a
    restart policy would have to change it HERE. Asserting the same property on a
    per-platform identity helper instead would be decorative: nothing calls such a
    helper, so a translation added to this branch would leave it green.

    The reason the answer is "no translation" is a property of the task this
    backend renders: Task Scheduler has no restart policy, so a non-zero exit is
    recorded and stays down, and the honest code is already the terminal one.
    """
    from kiro_crew.pod.config import TERMINAL_BOOT_EXIT_CODES

    monkeypatch.setattr(rt, "IS_MACOS", False)
    for code in (*TERMINAL_BOOT_EXIT_CODES, 0, 1, 42):
        assert rt.terminal_exit_code(cfg, "smoke", code) == code


# --------------------------------------------------------------------------
# schtasks argv — pinned, because the fakes are the only thing asserting it
# --------------------------------------------------------------------------
def test_start_creates_a_manual_run_only_task_then_runs_it(cfg, monkeypatch):
    """`/SC ONCE /ST 00:00` is a trigger already in the past, so Task Scheduler
    never fires it on its own — that keeps a pod transient, like `systemctl
    start` (never `enable`) and launchd's refusal to install under LaunchAgents.

    Neither `/RU` nor `/RP` is passed: that is the documented "current logged-on
    user" form and the only one that never prompts for a password.
    """
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(win, "schtasks", lambda *a: calls.append(a) or _cp())
    win.start(cfg, "smoke")
    script = win.task_script_path(cfg, "smoke")
    assert calls[0] == (
        "/Create",
        "/F",
        "/SC",
        "ONCE",
        "/ST",
        "00:00",
        "/TN",
        r"\KiroCrew\pods\kirocrew-pod\smoke",
        "/TR",
        f'"{script}"',
    )
    assert calls[1] == ("/Run", "/TN", r"\KiroCrew\pods\kirocrew-pod\smoke")
    assert "/RU" not in calls[0] and "/RP" not in calls[0]
    assert script.exists(), "the wrapper must exist before the task points at it"


def test_start_clears_a_stale_result_before_creating_the_task(cfg, monkeypatch):
    """Otherwise unit_state reads the previous boot's failure as this one's."""
    monkeypatch.setattr(win, "schtasks", lambda *a: _cp())
    win.result_path(cfg, "smoke").write_text("70\n")
    win.start(cfg, "smoke")
    assert win.last_result(cfg, "smoke") is None


def test_start_does_not_run_a_task_it_could_not_create(cfg, monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake(*a):
        calls.append(a)
        return _cp(returncode=1, stderr="ERROR: Access is denied.")

    monkeypatch.setattr(win, "schtasks", fake)
    cp = win.start(cfg, "smoke")
    assert cp.returncode == 1
    assert len(calls) == 1, "a failed /Create must not be followed by /Run"


def test_stop_ends_then_deletes_and_drops_the_wrapper(cfg, monkeypatch):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(win, "schtasks", lambda *a: calls.append(a) or _cp())
    win.write_task_script(cfg, "smoke")
    win.result_path(cfg, "smoke").write_text("0\n")
    cp = win.stop(cfg, "smoke")
    assert cp.returncode == 0
    tn = r"\KiroCrew\pods\kirocrew-pod\smoke"
    assert calls == [("/End", "/TN", tn), ("/Delete", "/TN", tn, "/F")]
    # Per-pod state must not outlive the pod: a leftover wrapper makes
    # orphan_homes read the HOME as "installed, not orphaned" and never collect
    # it, and leaves a definition that could be re-run later.
    assert not win.task_script_path(cfg, "smoke").exists()


def test_stop_waits_for_the_supervised_pid_before_deleting_anything(cfg, monkeypatch):
    """`/End` is asynchronous and reaches only the task's own process.

    Returning while the gateway is alive makes the caller reap the HOME from
    under a live writer: the removal then fails quietly while the CLI reports
    zero residue.
    """
    import os

    monkeypatch.setattr(win, "schtasks", lambda *a: _cp())
    monkeypatch.setattr(win, "time", _FakeClock())
    seen = {"n": 0}

    def dying(_cfg, _name):
        seen["n"] += 1
        return os.getpid() if seen["n"] < 4 else None

    monkeypatch.setattr(win, "supervised_pid", dying)
    win.write_task_script(cfg, "smoke")
    assert win.stop(cfg, "smoke").returncode == 0
    assert seen["n"] >= 4, "stop must poll rather than trust /End's return"


def test_stop_preserves_everything_when_the_gateway_will_not_die(cfg, monkeypatch):
    import os

    monkeypatch.setattr(win, "schtasks", lambda *a: _cp())
    monkeypatch.setattr(win, "time", _FakeClock())
    monkeypatch.setattr(win, "supervised_pid", lambda *_: os.getpid())
    # The escalation must be PINNED, so a recycled pid can never be signalled.
    killed: list[tuple[int, str]] = []
    monkeypatch.setattr(
        win,
        "kill_process_tree_pinned",
        lambda pid, token, sig=None: killed.append((pid, token)) or True,
    )
    win.write_task_script(cfg, "smoke")
    cp = win.stop(cfg, "smoke")
    assert cp.returncode == 1
    assert "NOT zero-residue" in cp.stderr
    assert killed and killed[0][0] == os.getpid()
    assert win.task_script_path(cfg, "smoke").exists(), "a live pod keeps its definition"


def test_stop_reports_a_task_it_could_not_delete(cfg, monkeypatch):
    def fake(*a):
        if a[0] == "/Delete":
            return _cp(returncode=1, stderr="ERROR: Access is denied.")
        return _cp()  # /End and the /Query existence probe both succeed

    monkeypatch.setattr(win, "schtasks", fake)
    win.write_task_script(cfg, "smoke")
    cp = win.stop(cfg, "smoke")
    assert cp.returncode == 1
    assert "could not be deleted" in cp.stderr
    assert win.task_script_path(cfg, "smoke").exists()


def test_deleting_a_task_that_is_already_gone_is_a_no_op_not_a_failure(cfg, monkeypatch):
    """Judged by re-probing existence, never by /Delete's own localized error."""

    def fake(*a):
        if a[0] == "/Delete":
            return _cp(returncode=1, stderr="ERROR: The system cannot find the file specified.")
        if a[0] == "/Query":
            return _cp(returncode=1)  # gone
        return _cp()

    monkeypatch.setattr(win, "schtasks", fake)
    win.write_task_script(cfg, "smoke")
    assert win.stop(cfg, "smoke").returncode == 0


def test_task_exists_reads_the_exit_code_not_the_localized_output(cfg, monkeypatch):
    monkeypatch.setattr(
        win, "schtasks", lambda *a: _cp(stdout="Aufgabe wird ausgeführt", returncode=0)
    )
    assert win.task_exists(cfg, "smoke") is True
    monkeypatch.setattr(win, "schtasks", lambda *a: _cp(stdout="egal", returncode=1))
    assert win.task_exists(cfg, "smoke") is False


class _FakeClock:
    """Monotonic that advances a second per read, so bounded waits do not sleep."""

    def __init__(self) -> None:
        self._t = 0.0

    def monotonic(self) -> float:
        self._t += 1.0
        return self._t

    def time(self) -> float:
        return self._t

    def sleep(self, _s: float) -> None:
        return None


# --------------------------------------------------------------------------
# Runtime dispatch — Linux and macOS must be byte-identical
# --------------------------------------------------------------------------
def test_runtime_dispatches_to_the_windows_backend_on_win32(cfg, monkeypatch):
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(rt, "systemctl", lambda *a, **k: pytest.fail("systemd must not be touched"))
    monkeypatch.setattr(win, "is_active", lambda c, n: True)
    monkeypatch.setattr(win, "main_pid", lambda c, n: 4242)
    monkeypatch.setattr(win, "unit_state", lambda c, n: ("active", 0))
    monkeypatch.setattr(win, "active_names", lambda c: {"smoke"})
    monkeypatch.setattr(win, "recent_journal", lambda c, n, lines=30: "log")
    assert rt.is_active(cfg, "smoke") is True
    assert rt.main_pid(cfg, "smoke") == 4242
    assert rt.unit_state(cfg, "smoke") == ("active", 0)
    assert rt.active_names(cfg) == {"smoke"}
    assert rt.recent_journal(cfg, "smoke") == "log"


def test_runtime_does_not_touch_the_windows_backend_off_win32(cfg, monkeypatch):
    """The Linux and macOS paths must be provably unchanged by this backend."""
    monkeypatch.setattr(rt, "IS_WINDOWS", False)
    monkeypatch.setattr(rt, "IS_MACOS", False)
    for fn in ("is_active", "main_pid", "unit_state", "active_names", "start", "stop"):
        monkeypatch.setattr(
            win, fn, lambda *a, **k: pytest.fail(f"windows.{fn} must not run off win32")
        )
    monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(stdout="MainPID=7\n"))
    assert rt.main_pid(cfg, "smoke") == 7


def test_require_backend_translates_the_windows_error_to_a_pod_error(monkeypatch):
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(
        win, "require_backend", lambda: (_ for _ in ()).throw(win.WindowsTaskError("no schtasks"))
    )
    with pytest.raises(rt.PodError, match="no schtasks"):
        rt.require_backend()


def test_install_backend_writes_nothing_on_windows(cfg, monkeypatch):
    """Task Scheduler has no template concept, so there is nothing to install."""
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(rt, "require_backend", lambda: None)
    monkeypatch.setattr(
        rt, "_write_and_load_unit", lambda c: pytest.fail("no unit is written on Windows")
    )
    msg, reload_cp = rt.install_backend(cfg)
    assert reload_cp is None
    assert "nothing to install on Windows" in msg


def test_orphan_homes_skips_a_pod_with_an_installed_wrapper(cfg, monkeypatch):
    """A wrapper on disk means "installed" (a name mid-`up`), not orphaned."""
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(rt, "active_names", lambda c: set())
    (cfg.pod_root / "installed").mkdir(parents=True)
    (cfg.pod_root / "orphan").mkdir(parents=True)
    win.write_task_script(cfg, "installed")
    assert rt.orphan_homes(cfg) == ["orphan"]


def test_stop_pod_reaps_the_home_on_windows(cfg, monkeypatch):
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(win, "stop", lambda c, n: _cp())
    monkeypatch.setattr(rt.time, "sleep", lambda _s: None)
    home = cfg.home_dir("smoke")
    home.mkdir(parents=True)
    (home / "state.json").write_text("{}")
    assert rt.stop_pod(cfg, "smoke").returncode == 0
    assert not home.exists()


def test_stop_pod_does_not_reap_the_home_when_the_stop_failed(cfg, monkeypatch):
    """A pod that may still be live must keep its state."""
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(win, "stop", lambda c, n: _cp(returncode=1, stderr="still running"))
    home = cfg.home_dir("smoke")
    home.mkdir(parents=True)
    cp = rt.stop_pod(cfg, "smoke")
    assert cp.returncode == 1
    assert home.exists()


def test_stop_pod_hands_over_when_a_new_pod_claims_the_name(cfg, monkeypatch):
    """A new `up` writes the wrapper BEFORE creating the task, so its presence is
    the claim marker for any writer that bypasses the mutex. The old pod is gone
    but the env file now pins the NEW pod's checkout and must be left alone."""
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(win, "stop", lambda c, n: _cp())
    win.write_task_script(cfg, "smoke")
    cp = rt.stop_pod(cfg, "smoke")
    assert cp.returncode == 0
    assert rt.RECLAIMED_MARKER in cp.stdout


def test_stop_pod_clears_the_recorded_result_so_a_reused_name_starts_clean(cfg, monkeypatch):
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(win, "stop", lambda c, n: _cp())
    monkeypatch.setattr(rt.time, "sleep", lambda _s: None)
    win.result_path(cfg, "smoke").write_text("70\n")
    assert rt.stop_pod(cfg, "smoke").returncode == 0
    assert win.last_result(cfg, "smoke") is None


def test_boot_supervises_instead_of_exec_on_windows(cfg, monkeypatch, tmp_path):
    """Windows has no exec: os.execve there spawns and terminates the caller.

    Using it would change the pid (so main_pid would stop naming the process
    that bound the port) and let the task's own process exit while the gateway
    kept running orphaned, with Task Scheduler reporting the task finished.
    """
    import os
    import sys

    seen: dict[str, object] = {}

    class FakeProc:
        pid = os.getpid()

        def wait(self):
            return 3

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["flags"] = kwargs.get("creationflags")
        return FakeProc()

    monkeypatch.setattr(win.subprocess, "Popen", fake_popen)
    # Pinned alongside the fake Popen: on macOS ``process_start_time`` shells out
    # to ``ps`` through ``subprocess``, so an unpinned call would hand the fake
    # Popen a real query and fail on the fake's missing context-manager protocol.
    # On Linux it reads /proc and the pin is a no-op.
    monkeypatch.setattr(win, "process_start_time", lambda pid: "1234567")
    monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda pid: True)
    monkeypatch.setattr(win, "resume_process_main_thread", lambda pid: True)
    # Same reason as the pin above, for the other platform primitive the
    # supervision loop reads. After the gateway is reaped it asks whether a
    # restart successor could exist, and `process_descendants` shells out to `ps`
    # on macOS -- an unpinned call would hand the fake Popen a real query. Empty
    # is the ordinary-shutdown answer: nothing to adopt.
    monkeypatch.setattr(win, "attributed_descendants", lambda pid, token: [])
    rc = win.supervise_gateway(
        cfg,
        "smoke",
        tmp_path / "kirocrew",
        ["gateway", "--no-crons"],
        {"A": "b"},
        gateway_pid_record=_no_successor(tmp_path),
    )
    assert rc == 3
    assert seen["argv"][1:] == ["gateway", "--no-crons"]
    # The flag composition itself is pinned by
    # test_the_ceiling_is_attached_while_the_gateway_is_still_suspended, which
    # substitutes sentinels because both constants are 0 off win32.
    assert seen["flags"] == win.CREATE_NEW_PROCESS_GROUP | win.CREATE_SUSPENDED
    # The record is dropped once the gateway exits, so a dead pod never attests.
    assert win.supervised_pid(cfg, "smoke") is None
    assert sys is not None  # keep the import meaningful for linters


def test_the_ceiling_is_attached_while_the_gateway_is_still_suspended(cfg, monkeypatch, tmp_path):
    """Order is the whole guarantee, so it is pinned rather than left to reading.

    Job membership covers a member's FUTURE descendants, not ones it already
    spawned, so attaching the ceiling to a RUNNING child leaves a window where a
    grandchild escapes. A child created suspended has executed no instructions
    and provably has no descendants, which closes that window by construction --
    which only holds if the calls happen in this order.

    The two creation flags are replaced with distinct sentinels for the duration:
    both are literally ``0`` off win32, so asserting on their real values would
    make this test vacuous on the matrix that actually runs it.
    """
    import os

    order: list[str] = []
    monkeypatch.setattr(win, "CREATE_NEW_PROCESS_GROUP", 0x200)
    monkeypatch.setattr(win, "CREATE_SUSPENDED", 0x4)

    class FakeProc:
        pid = os.getpid()

        def wait(self):
            return 0

    def fake_popen(argv, **kwargs):
        assert kwargs["creationflags"] & win.CREATE_SUSPENDED, (
            "the gateway must be created SUSPENDED, or the ceiling races the "
            "descendants it is meant to bound"
        )
        assert (
            kwargs["creationflags"] & win.CREATE_NEW_PROCESS_GROUP
        ), "the pod must still be out of the task console's Ctrl+C group"
        order.append("spawn")
        return FakeProc()

    monkeypatch.setattr(win.subprocess, "Popen", fake_popen)
    # Pinned alongside the fake Popen: on macOS ``process_start_time`` shells out
    # to ``ps`` through ``subprocess``, so an unpinned call would hand the fake
    # Popen a real query and fail on the fake's missing context-manager protocol.
    # On Linux it reads /proc and the pin is a no-op.
    monkeypatch.setattr(win, "process_start_time", lambda pid: "1234567")
    monkeypatch.setattr(
        win, "apply_windows_resource_ceiling", lambda pid: order.append("ceiling") or True
    )
    monkeypatch.setattr(
        win, "resume_process_main_thread", lambda pid: order.append("resume") or True
    )
    # Pinned like `process_start_time`: the post-reap successor check would
    # otherwise shell out to a real `ps` on macOS. Empty keeps it out of `order`.
    monkeypatch.setattr(win, "attributed_descendants", lambda pid, token: [])
    assert (
        win.supervise_gateway(
            cfg,
            "smoke",
            tmp_path / "kirocrew",
            ["gateway"],
            {},
            gateway_pid_record=_no_successor(tmp_path),
        )
        == 0
    )
    assert order == ["spawn", "ceiling", "resume"]


def test_a_gateway_that_cannot_be_resumed_is_killed_not_left_frozen(cfg, monkeypatch, tmp_path):
    """A frozen child must never masquerade as a running gateway.

    Same policy ``acp.client.finish_suspended_spawn`` implements for this
    handshake: a failed resume leaves a process that is alive, answers nothing,
    and would otherwise sit there until the boot timeout blamed the worktree.
    """
    import os

    killed: list[str] = []

    class FakeProc:
        pid = os.getpid()

        def kill(self):
            killed.append("kill")

        def wait(self):
            return 0

    monkeypatch.setattr(win.subprocess, "Popen", lambda argv, **kw: FakeProc())
    # Pinned alongside the fake Popen: on macOS ``process_start_time`` shells out
    # to ``ps`` through ``subprocess``, so an unpinned call would hand the fake
    # Popen a real query and fail on the fake's missing context-manager protocol.
    # On Linux it reads /proc and the pin is a no-op.
    monkeypatch.setattr(win, "process_start_time", lambda pid: "1234567")
    monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda pid: True)
    monkeypatch.setattr(win, "resume_process_main_thread", lambda pid: False)
    # Pinned like `process_start_time`: the post-reap successor check would
    # otherwise shell out to a real `ps` on macOS.
    monkeypatch.setattr(win, "attributed_descendants", lambda pid, token: [])
    rc = win.supervise_gateway(
        cfg,
        "smoke",
        tmp_path / "kirocrew",
        ["gateway"],
        {},
        gateway_pid_record=_no_successor(tmp_path),
    )
    assert killed == ["kill"]
    assert rc == EXIT_REFUSED_UNRECOVERABLE
    # Nothing was recorded, so no reader can attest for a pod that never ran.
    assert win.supervised_pid(cfg, "smoke") is None


def test_a_missing_ceiling_does_not_fail_the_boot(cfg, monkeypatch, tmp_path):
    """Matches how an unavailable cgroup scope is handled on the POSIX side.

    ``apply_job_limits`` has already logged the miss as a SECURITY warning; a pod
    that refuses to start because a ceiling could not be attached would be a
    worse outcome than one that runs unbounded and says so in the log.
    """
    import os

    class FakeProc:
        pid = os.getpid()

        def wait(self):
            return 0

    monkeypatch.setattr(win.subprocess, "Popen", lambda argv, **kw: FakeProc())
    # Pinned alongside the fake Popen: on macOS ``process_start_time`` shells out
    # to ``ps`` through ``subprocess``, so an unpinned call would hand the fake
    # Popen a real query and fail on the fake's missing context-manager protocol.
    # On Linux it reads /proc and the pin is a no-op.
    monkeypatch.setattr(win, "process_start_time", lambda pid: "1234567")
    monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda pid: False)
    monkeypatch.setattr(win, "resume_process_main_thread", lambda pid: True)
    # Pinned like `process_start_time`: the post-reap successor check would
    # otherwise shell out to a real `ps` on macOS.
    monkeypatch.setattr(win, "attributed_descendants", lambda pid, token: [])
    assert (
        win.supervise_gateway(
            cfg,
            "smoke",
            tmp_path / "kirocrew",
            ["gateway"],
            {},
            gateway_pid_record=_no_successor(tmp_path),
        )
        == 0
    )


# --------------------------------------------------------------------------
# Real win32 — the only tests that can answer "can this user supervise a pod"
# --------------------------------------------------------------------------
@requires_windows
def test_schtasks_resolves_from_a_trusted_system_directory():
    """A bare argv name would resolve through a PATH that can lead with a
    same-user-writable directory, and this binary is handed a command line that
    boots a gateway."""
    assert win.schtasks_bin() is not None


# The two tests that drive the REAL Task Scheduler (the creation probe and the
# create/run/delete round trip) live in test_pod_windows_boot.py: that module is
# opt-in (KIROCREW_E2E_POD_WINDOWS=1) and on the root conftest's host-service
# allowlist, so an ordinary Windows `pytest` never registers a scheduled task.


@requires_windows
def test_the_wrapper_a_real_pod_would_boot_is_a_parsable_batch_file(cfg):
    """Runs the generated wrapper's own env block through a real cmd.exe.

    The pod itself is not booted (that needs a provisioned worktree); what is
    proven here is that the quoting this backend emits is something cmd.exe
    actually accepts, which no amount of string assertion can establish.
    """
    body = win.render_task_script(cfg, "smoke")
    env_only = [ln for ln in body.splitlines() if ln.startswith(("@echo", "setlocal", 'set "'))]
    probe = cfg.pods_dir / "envprobe.cmd"
    probe.write_text("\r\n".join([*env_only, "echo %KIROCREW_POD_ROOT%"]) + "\r\n", newline="")
    cp = subprocess.run([str(probe)], capture_output=True, timeout=30, **UTF8_TEXT)
    assert cp.returncode == 0, cp.stderr
    assert str(cfg.pod_root) in cp.stdout


def test_the_wrapper_is_written_in_the_console_code_page_and_refuses_what_it_cannot_spell(
    tmp_path, monkeypatch
):
    """cmd.exe reads a batch file in the OEM code page, never UTF-8, so a wrapper
    written as UTF-8 would hand cmd.exe different bytes for any non-ASCII path.
    The write encodes in that page strictly and refuses rather than misspell."""
    monkeypatch.setattr(win, "_script_encoding", lambda: "cp437")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods-env"))
    monkeypatch.setenv("KIROCREW_POD_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    # A Latin-1 letter is inside cp437, so this plane still renders and writes.
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "caf\u00e9" / "pods"))
    cfg = PodConfig.load()
    cfg.pods_dir.mkdir(parents=True, exist_ok=True)
    path = win.write_task_script(cfg, "smoke")
    body = path.read_bytes().decode("cp437")
    assert body.startswith("@echo off") and "caf\u00e9" in body

    # A CJK character is not, so the write refuses with the offending text named.
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "\u4e2d\u6587" / "pods"))
    cfg = PodConfig.load()
    cfg.pods_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(win.WindowsTaskError) as info:
        win.write_task_script(cfg, "smoke")
    assert "console code page" in str(info.value)
    assert "\u4e2d" in str(info.value)


def test_the_wrapper_encoding_is_oem_on_windows_and_utf8_elsewhere(monkeypatch):
    monkeypatch.setattr(win, "IS_WINDOWS", True)
    assert win._script_encoding() == "oem"
    monkeypatch.setattr(win, "IS_WINDOWS", False)
    assert win._script_encoding() == "utf-8"
