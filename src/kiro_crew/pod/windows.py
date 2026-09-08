"""Windows Task Scheduler backend for pods — the win32 sibling of
:mod:`kiro_crew.pod.unit` (systemd) and :mod:`kiro_crew.pod.launchd`.

The pod runtime's platform-neutral core (name validation, port derivation,
checkout resolution and pinning, env scrubbing, token minting, ``boot``, and the
``cleanup_home`` teardown safety check) is reused unchanged. Only the
service-manager mechanics differ.

**Why Task Scheduler and not a Windows service.** A pod is a per-user,
no-elevation, disposable gateway supervised by the OS. ``sc.exe create`` needs
``SeCreateServiceNamePrivilege`` — an administrator right — and installs a
machine-wide LocalSystem service, so it is the wrong fit twice over: a developer
would have to elevate to test a worktree, and the pod would no longer run as the
user whose ``~/.kiro`` it is isolating from. ``schtasks.exe`` creates a task in
the calling user's own namespace with no elevation, which is exactly the systemd
``--user`` / launchd ``gui/<uid>`` shape.

Five things differ from the other two backends, and each one is load-bearing:

**1. No task-level environment variables.** A systemd unit carries
``Environment=`` lines and a launchd plist carries ``EnvironmentVariables``. A
scheduled task carries neither: its action is one command line and it runs with
the user's *profile* environment, so any ``KIROCREW_POD_*`` override the CLI
resolved would be lost. The pod's action is therefore a generated ``.cmd``
wrapper (:func:`render_task_script`) that sets the plane from
:func:`kiro_crew.pod.config.environment_vars` — the same selection both other
backends serialise — and then re-enters ``kirocrew pod _run <name>``. The wrapper
is data, not logic: boot stays in :func:`kiro_crew.pod.runtime.boot`, so nothing
shell-shaped ships in the package.

**2. No ``KeepAlive`` / ``Restart=on-failure``.** Task Scheduler can retry a
*failed start*, not a process that exited non-zero, so a crashed pod stays down.
That removes the restart-loop hazard the launchd backend has to work around with
its exit-0 translation (see :func:`windows_exit_code`), and it means the crash
signal ``pod up`` waits on has to be derived rather than read: the wrapper
records the boot's exit code beside the task and :func:`unit_state` reports
``failed`` when that code is non-zero and the supervised process is gone.

**3. No PID from the service manager.** ``systemctl show -p MainPID`` and
``launchctl print`` both name the running process; ``schtasks /Query`` names
none, at any verbosity. Worse, Windows has no ``exec``: CPython's ``os.execve``
there *spawns and exits*, so the systemd invariant "the gateway REPLACES the
unit's main process, therefore ``MainPID`` is the process that bound the port"
cannot hold. So :func:`supervise_gateway` spawns the gateway as a child of the
wrapper, records its pid plus its process-creation identity beside the task, and
waits on it — which restores the invariant with the wrapper as the supervisor.
:func:`main_pid` reads that record. It stays an *independent* fact from the
gateway PID sidecar ``port_owner`` compares it against: different file,
different directory, different writer.

**4. ``schtasks`` output is LOCALIZED, so this backend never parses it.** Both
the CSV column headers and the ``Status`` values of ``schtasks /Query /FO CSV
/V`` are translated on a non-English Windows, so a reader keyed on ``"Status" ==
"Running"`` silently reports every pod down on a German host — the fail-OPEN
direction, which would let teardown delete a live pod's HOME. Liveness and the
last result are therefore read from the two files the supervised process itself
writes, which are locale-independent, cheaper (no subprocess), and more precise
(they name the gateway, which is what ``main_pid`` owes its caller). ``schtasks``
is used only for verbs whose *exit code* is the answer: ``/Create``, ``/Run``,
``/End``, ``/Delete``, ``/Query`` as an existence probe.

**5. No cgroups — the resource ceiling is NOT enforced.** Same gap as macOS, for
the same reason and with the same decision: the systemd unit's ``MemoryMax=4G``
and ``CPUQuota=200%`` are kernel-enforced, a scheduled task has no equivalent,
and emitting a weaker knob that reads as the guarantee is worse than stating its
absence. (A Job object *could* bound the tree — ``sandbox.apply_windows_resource_ceiling``
does exactly that for agent subprocesses — but it has to be applied by the
process that spawns the tree, and a pod's tree is spawned by the worktree's own
gateway, not by this backend. Wiring it is a follow-up, not a rename of this
gap.)

Every other isolation property is unchanged: own ``KIROCREW_HOME``, own derived
port, no tunnel, ``--no-crons``, and the refusal to bind the live port.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

from kiro_crew.platform_compat import (
    CREATE_NEW_PROCESS_GROUP,
    IS_WINDOWS,
    SIGTERM,
    kill_process_tree_pinned,
    process_start_time,
    trusted_system_bin,
)
from kiro_crew.pod.config import PodConfig, environment_vars
from kiro_crew.pod.unit import _kirocrew_argv as _shared_kirocrew_argv
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# Task Scheduler folder every pod task lives in. One folder per pod plane, so a
# hermetic test plane (KIROCREW_POD_UNIT_PREFIX) cannot collide with a
# developer's real pods — the same property cfg.unit_prefix buys on the other two
# backends.
TASK_FOLDER_ROOT = r"\KiroCrew\pods"

# How long stop() waits for the supervised gateway to go away before escalating
# to a pinned tree kill. Named so the wait and the message reporting it expiring
# cannot drift apart.
STOP_TIMEOUT_SECS = 15.0


class WindowsTaskError(RuntimeError):
    """Task Scheduler is not usable on this host."""


# ------------------------------------------------------------------------- #
# Gate
# ------------------------------------------------------------------------- #
# require_backend() sits on the chokepoint every schtasks call funnels through,
# and its create-and-delete probe costs two subprocess spawns. Cache the
# SUCCESS only: a host that can create a task will not stop being able to
# mid-process, while a refusal must stay a refusal every time it is asked.
_PROBE_OK = False


def schtasks_bin() -> str | None:
    """Absolute path of ``schtasks.exe``, or ``None`` when unavailable.

    Resolved through :func:`kiro_crew.platform_compat.trusted_system_bin` rather
    than a bare argv name: ``PATH`` on Windows can lead with a same-user-writable
    directory, and this binary is handed a command line that boots a gateway.
    """
    return trusted_system_bin("schtasks")


def require_backend() -> None:
    """Fail loudly and early when Task Scheduler cannot be driven.

    Three stages, mirroring the systemd gate's shape (platform, binary,
    can-we-actually-use-it):

    1. This is win32 at all.
    2. ``schtasks.exe`` resolves to a trusted system path.
    3. This user can really create a task. Stage 3 is a probe rather than an
       inspection because there is nothing to inspect: task creation is
       refused by Group Policy, by a locked-down ``Schedule`` service, and by a
       principal with no ``TASK_CREATE`` right, and none of those is visible
       from the client side. Without the probe every one of them surfaces as a
       failed ``pod up`` blaming the worktree build. The throwaway task is
       created in the pod plane's own folder and deleted immediately.
    """
    global _PROBE_OK
    if not IS_WINDOWS:
        raise WindowsTaskError(
            f"the Task Scheduler pod backend is win32-only; this host is {sys.platform}."
        )
    exe = schtasks_bin()
    if exe is None:
        raise WindowsTaskError(
            "pods need `schtasks.exe`, which was not found in a trusted system "
            "directory. Run `kirocrew pod` from a normal user session on Windows."
        )
    if _PROBE_OK:
        return
    probe = rf"{TASK_FOLDER_ROOT}\_probe_{uuid.uuid4().hex}"
    created = _schtasks_raw(
        exe,
        "/Create",
        "/F",
        "/SC",
        "ONCE",
        "/ST",
        "00:00",
        "/TN",
        probe,
        "/TR",
        '"cmd.exe /c exit 0"',
    )
    if created.returncode != 0:
        raise WindowsTaskError(
            "this user cannot create a scheduled task, so pods cannot be "
            f"supervised on this host (schtasks /Create rc={created.returncode}): "
            f"{(created.stderr or created.stdout or '').strip()}\n"
            "Pods are per-user scheduled tasks and never elevate; a policy that "
            "forbids user task creation has no non-admin workaround."
        )
    _schtasks_raw(exe, "/Delete", "/TN", probe, "/F")
    _PROBE_OK = True


# ------------------------------------------------------------------------- #
# Naming and paths
# ------------------------------------------------------------------------- #
def task_name(cfg: PodConfig, name: str) -> str:
    """Full Task Scheduler path for pod *name*.

    Replaces systemd's ``<prefix>@<name>.service`` and launchd's
    ``dev.kirocrew.pod.<prefix>.<name>``. The name has already been through
    ``runtime.validate_name`` (one safe segment, no ``\\``, no ``..``), which is
    what makes it legal to splice into a task path.
    """
    return rf"{TASK_FOLDER_ROOT}\{cfg.unit_prefix}\{name}"


def task_folder(cfg: PodConfig) -> str:
    """The plane's own task folder — every pod task is a direct child."""
    return rf"{TASK_FOLDER_ROOT}\{cfg.unit_prefix}"


def _plane_file(cfg: PodConfig, name: str, suffix: str) -> Path:
    return cfg.pods_dir / f"{cfg.unit_prefix}.{name}{suffix}"


def task_script_path(cfg: PodConfig, name: str) -> Path:
    """The generated ``.cmd`` the task's action points at.

    Beside the per-pod env file, in the pod plane's own directory — the same
    place launchd keeps its per-pod plist, and for the same reason: it is
    per-pod state that must not outlive the pod, so its presence doubles as the
    "this name is installed" marker :func:`kiro_crew.pod.runtime.orphan_homes`
    reads.
    """
    return _plane_file(cfg, name, ".cmd")


def pid_record_path(cfg: PodConfig, name: str) -> Path:
    """Where the wrapper records the supervised gateway's pid + start identity.

    HOST-side, deliberately not inside the pod's isolated home: this record is
    the service-manager half of ``port_owner``'s two-independent-facts proof,
    and putting it in the same tree as the gateway's own PID sidecar would make
    that proof compare a file with itself.
    """
    return _plane_file(cfg, name, ".winpid")


def result_path(cfg: PodConfig, name: str) -> Path:
    """Where the wrapper records the boot's exit code.

    Stands in for systemd's ``ActiveState=failed`` and launchd's ``last exit
    code``, both of which this platform's service manager does not expose in a
    locale-independent form.
    """
    return _plane_file(cfg, name, ".winresult")


def log_paths(cfg: PodConfig, name: str) -> tuple[Path, Path]:
    """stdout/stderr files that stand in for the journal.

    Same layout as the launchd backend, so ``pod logs`` reads one shape on both
    journal-less platforms.
    """
    d = cfg.artifacts_dir / name
    return d / "pod.out.log", d / "pod.err.log"


# ------------------------------------------------------------------------- #
# The generated .cmd wrapper
# ------------------------------------------------------------------------- #
def _cmd_literal(value: str) -> str:
    """Quote *value* for a batch file, refusing what cmd.exe cannot express.

    ``%`` doubles (a batch file expands ``%%`` to one literal ``%``). A double
    quote and a newline are REFUSED rather than escaped: cmd.exe has no escape
    for a quote inside a quoted token, so any attempt would silently change the
    value the gateway is booted with, and a path that cannot be expressed must
    fail at ``pod up`` rather than at boot.
    """
    if '"' in value or "\r" in value or "\n" in value:
        raise WindowsTaskError(
            "cannot boot a pod through a scheduled task: the value "
            f"{value!r} contains a character cmd.exe cannot quote (a double "
            "quote or a newline). Move the pod plane to a path without it "
            "(KIROCREW_POD_ROOT / KIROCREW_POD_ENV_DIR)."
        )
    return value.replace("%", "%%")


def _cmd_quote(arg: str) -> str:
    """One argv element as a cmd.exe token — always quoted, never bare."""
    return f'"{_cmd_literal(arg)}"'


def render_task_script(cfg: PodConfig, name: str) -> str:
    """The ``.cmd`` body for one pod. Returned as text so tests can assert on it
    without creating a task.

    Structure, in the order it matters:

    * ``setlocal`` without ``EnableDelayedExpansion`` — a ``!`` in a path must
      stay literal.
    * The pod plane, from the shared :func:`environment_vars` selection. This is
      the whole reason the wrapper exists (module docstring, point 1).
    * A stale result file is cleared BEFORE the boot, so ``unit_state`` cannot
      read the previous run's failure as this one's.
    * stdout/stderr append to the pod's own log files, and the gateway child
      inherits those handles — that is what gives ``pod logs`` content on a
      platform with no journal.
    * The exit code is captured into ``RC`` before anything else runs, then
      recorded and re-raised as the task's own result.
    """
    out_log, err_log = log_paths(cfg, name)
    lines = [
        "@echo off",
        f"rem Kiro Crew pod {name} -- generated by kiro_crew.pod.windows. Do not edit.",
        "setlocal",
    ]
    for key, value in sorted(environment_vars(cfg).items()):
        lines.append(f'set "{_cmd_literal(key)}={_cmd_literal(value)}"')
    log_dir = _cmd_quote(str(out_log.parent))
    lines += [
        f"if not exist {log_dir} mkdir {log_dir}",
        f"del /q {_cmd_quote(str(result_path(cfg, name)))} 2>nul",
        " ".join(
            [
                *(_cmd_quote(a) for a in _shared_kirocrew_argv()),
                "pod",
                "_run",
                _cmd_quote(name),
                f">> {_cmd_quote(str(out_log))}",
                f"2>> {_cmd_quote(str(err_log))}",
            ]
        ),
        'set "RC=%ERRORLEVEL%"',
        f"> {_cmd_quote(str(result_path(cfg, name)))} echo %RC%",
        "exit /b %RC%",
    ]
    return "\r\n".join(lines) + "\r\n"


def write_task_script(cfg: PodConfig, name: str) -> Path:
    """Render and install this pod's wrapper. Returns its path.

    Re-rendered on every ``up``, like the launchd plist and unlike the systemd
    template, so it cannot go stale against a moved worktree or a changed plane.
    """
    dst = task_script_path(cfg, name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out_log, _ = log_paths(cfg, name)
    out_log.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so the CRLF the body already carries is not translated again.
    with dst.open("w", encoding="utf-8", newline="") as fh:
        fh.write(render_task_script(cfg, name))
    return dst


# ------------------------------------------------------------------------- #
# Talking to schtasks
# ------------------------------------------------------------------------- #
def _schtasks_raw(exe: str, *args: str) -> subprocess.CompletedProcess:
    """Run *exe* with *args*, no gate — used by the gate's own probe."""
    return subprocess.run(
        [exe, *args],
        capture_output=True,
        timeout=30,
        check=False,
        **UTF8_TEXT,
    )


def schtasks(*args: str) -> subprocess.CompletedProcess:
    """The single chokepoint for talking to Task Scheduler.

    Mirrors ``runtime.systemctl`` and ``launchd.launchctl``: one seam for tests
    to monkeypatch, and one place the gate cannot be forgotten.
    """
    require_backend()
    exe = schtasks_bin()
    assert exe is not None  # require_backend refuses otherwise
    return _schtasks_raw(exe, *args)


def task_exists(cfg: PodConfig, name: str) -> bool:
    """Whether Task Scheduler still holds a task for pod *name*.

    Keyed on ``/Query``'s EXIT CODE, never on its output: the output is
    localized (module docstring, point 4) and the code is not.
    """
    return schtasks("/Query", "/TN", task_name(cfg, name)).returncode == 0


# ------------------------------------------------------------------------- #
# The supervised pid record
# ------------------------------------------------------------------------- #
def record_supervised_pid(cfg: PodConfig, name: str, pid: int) -> None:
    """Record *pid* as pod *name*'s gateway, bound to its start identity.

    A bare pid is not an identity: a wrapper killed without running its cleanup
    leaves the record behind, and Windows recycles pids. The creation-time token
    is what lets :func:`supervised_pid` refuse a recycled number instead of
    reporting an unrelated process as the pod.

    Best-effort on the write: the gateway is already spawned by the time this
    runs, so a failure here must degrade to "no record" (which reads as
    inactive) rather than kill a booting pod.
    """
    record = pid_record_path(cfg, name)
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(f"{pid}\n{process_start_time(pid) or ''}\n", encoding="utf-8")
    except OSError:
        pass


def clear_supervised_pid(cfg: PodConfig, name: str) -> None:
    """Drop pod *name*'s pid record (the gateway has exited)."""
    try:
        pid_record_path(cfg, name).unlink(missing_ok=True)
    except OSError:
        pass


def _read_pid_record(cfg: PodConfig, name: str) -> tuple[int, str] | None:
    try:
        raw = pid_record_path(cfg, name).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not raw or not raw[0].strip().isdigit():
        return None
    return int(raw[0].strip()), (raw[1].strip() if len(raw) > 1 else "")


def supervised_pid(cfg: PodConfig, name: str) -> int | None:
    """Pod *name*'s live gateway pid, PROVEN to still be that process, or ``None``.

    Fails CLOSED on every way of not knowing — no record, no recorded identity,
    a host that will not report a creation time, or a token that no longer
    matches. Each of those must read as "this pod has no process", never as a
    pid a caller may go on to signal.
    """
    record = _read_pid_record(cfg, name)
    if record is None:
        return None
    pid, recorded = record
    if pid <= 0 or not recorded:
        return None
    return pid if process_start_time(pid) == recorded else None


def last_result(cfg: PodConfig, name: str) -> int | None:
    """The exit code pod *name*'s last boot recorded, or ``None`` if unknown."""
    try:
        raw = result_path(cfg, name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.lstrip("-").isdigit() else None


# ------------------------------------------------------------------------- #
# Lifecycle
# ------------------------------------------------------------------------- #
def start(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    """Create pod *name*'s task and run it now.

    ``/SC ONCE /ST 00:00`` is a schedule Task Scheduler will not fire on its
    own: the trigger time is already in the past when the task is created, and
    Windows does not replay a missed trigger unless the task asks it to. That
    keeps a pod TRANSIENT, matching the systemd path (``start``, never
    ``enable``) and the launchd path's deliberate refusal to install under
    ``~/Library/LaunchAgents``.

    Neither ``/RU`` nor ``/RP`` is passed, which is the documented form for "run
    as the current logged-on user" and the only one that never prompts for a
    password: ``/RU`` without ``/RP`` asks for one on an interactive console and
    fails outright without one. So the task runs as the user, unelevated, which
    is the whole reason this backend is Task Scheduler and not ``sc.exe``.
    """
    script = write_task_script(cfg, name)
    # Clear the previous run's result HOST-side too. The wrapper also does it,
    # but only once the task has started: until then unit_state would read the
    # stale code and report this fresh start as already failed.
    try:
        result_path(cfg, name).unlink(missing_ok=True)
    except OSError:
        pass
    created = schtasks(
        "/Create",
        "/F",
        "/SC",
        "ONCE",
        "/ST",
        "00:00",
        "/TN",
        task_name(cfg, name),
        "/TR",
        f'"{script}"',
    )
    if created.returncode != 0:
        return created
    return schtasks("/Run", "/TN", task_name(cfg, name))


def stop(
    cfg: PodConfig, name: str, *, timeout: float = STOP_TIMEOUT_SECS
) -> subprocess.CompletedProcess:
    """End pod *name*'s task, confirm its gateway is really gone, then delete it.

    Three things here are not obvious, and each mirrors a hazard the launchd
    backend documents:

    **``/End`` is asynchronous and only reaches the task's own process.** It
    returns before the wrapper has exited, and Task Scheduler's termination is
    not a contractual kill of the whole tree, so the gateway can outlive it. The
    caller reaps the pod's isolated HOME immediately afterwards, so returning
    early means deleting state from under a live writer — the removal then fails
    quietly while the CLI reports zero residue. So poll the SUPERVISED PID, not
    the task's status: that is the process whose death makes the HOME safe to
    delete, and it is the reading that is not localized.

    **A survivor is escalated, not waited out forever.** Once the window
    expires the gateway is killed through
    :func:`kiro_crew.platform_compat.kill_process_tree_pinned`, which will not
    fire unless the creation-time token still matches — so a recycled pid cannot
    be signalled.

    **The unload result must be authoritative.** If the gateway is STILL alive,
    or the task could not be deleted, this returns a failure and keeps the
    wrapper script: the caller must not tear down state that may belong to a
    live pod. A ``/End`` or ``/Delete`` against a task that is not there is a
    no-op, not a failure, which is why both are judged by re-probing existence
    rather than by their own exit code.

    The caller still owns the HOME removal (see ``runtime.stop_pod``) because
    that goes through ``cleanup_home``'s name re-validation.
    """
    ended = schtasks("/End", "/TN", task_name(cfg, name))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if supervised_pid(cfg, name) is None:
            break
        time.sleep(0.2)
    pid = supervised_pid(cfg, name)
    if pid is not None:
        token = process_start_time(pid)
        if token:
            kill_process_tree_pinned(pid, token, SIGTERM)
        grace = time.monotonic() + 5.0
        while time.monotonic() < grace and supervised_pid(cfg, name) is not None:
            time.sleep(0.2)
    if supervised_pid(cfg, name) is not None:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=ended.stdout or "",
            stderr=(
                f"the gateway for pod {name!r} is still running after "
                f"{timeout:.0f}s and a pinned tree kill (schtasks /End "
                f"rc={ended.returncode}). Its HOME and task were preserved; this "
                "pod is NOT zero-residue."
            ),
        )
    deleted = schtasks("/Delete", "/TN", task_name(cfg, name), "/F")
    if deleted.returncode != 0 and task_exists(cfg, name):
        return subprocess.CompletedProcess(
            args=[],
            returncode=deleted.returncode or 1,
            stdout=deleted.stdout or "",
            stderr=(
                f"pod {name!r} stopped but its scheduled task at "
                f"{task_name(cfg, name)} could not be deleted "
                f"(rc={deleted.returncode}): "
                f"{(deleted.stderr or deleted.stdout or '').strip()}"
            ),
        )
    # Per-pod state must not outlive the pod: a leftover wrapper makes
    # runtime.orphan_homes classify the HOME as "installed, not orphaned" and
    # never collect it, and leaves a definition that could be re-run later.
    try:
        task_script_path(cfg, name).unlink(missing_ok=True)
    except OSError:
        pass
    clear_supervised_pid(cfg, name)
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=ended.stdout or "", stderr="")


def supervise_gateway(
    cfg: PodConfig, name: str, bin_path: Path, argv: list[str], env: dict[str, str]
) -> int:
    """Spawn the pod's gateway, record it, wait for it, and return its exit code.

    **The win32 substitute for ``os.execve``**, which the POSIX path ends with.
    Windows has no ``exec``: CPython's ``os.execve`` spawns a new process and
    terminates the caller, so using it here would (a) change the pid, breaking
    ``main_pid``'s contract that it names the process which bound the port, and
    (b) let the task's own process exit while the gateway kept running,
    orphaned, with Task Scheduler reporting the task finished. Supervising
    instead keeps the wrapper alive as the parent, which is what makes ``/End``
    a real stop and the pid record a real identity.

    ``CREATE_NEW_PROCESS_GROUP`` is the analogue of the POSIX
    ``start_new_session``: a Ctrl+C in whatever console the task ran under must
    not reach the pod. Standard handles are deliberately INHERITED so the
    gateway's output lands in the log files the wrapper redirected — that is the
    journal on this platform.
    """
    proc = subprocess.Popen(  # noqa: S603 - argv is package-derived, never user text
        [str(bin_path), *argv],
        env=env,
        creationflags=CREATE_NEW_PROCESS_GROUP,
        close_fds=False,
    )
    record_supervised_pid(cfg, name, proc.pid)
    try:
        return proc.wait()
    finally:
        clear_supervised_pid(cfg, name)


# ------------------------------------------------------------------------- #
# Probes
# ------------------------------------------------------------------------- #
def is_active(cfg: PodConfig, name: str) -> bool:
    """Whether this pod has a live gateway process.

    Answered from the supervised pid record, not from ``schtasks /Query``: the
    query's ``Status`` column is localized, so keying on it would report every
    pod down on a non-English Windows — and that is the fail-OPEN direction,
    where teardown deletes a live pod's HOME.
    """
    return supervised_pid(cfg, name) is not None


def main_pid(cfg: PodConfig, name: str) -> int | None:
    """PID of this pod's own gateway, or ``None`` when it is not running.

    The Windows counterpart of systemd's ``MainPID``, and the identity
    ``runtime.port_owner`` compares a port's listener against. The wrapper's
    ``supervise_gateway`` writes it and the creation-time token proves it still
    names the same process (see :func:`supervised_pid`).

    Cannot raise the "could not ask" error its two siblings can, because there
    is nothing to ask: the record either proves a pid or it does not. That makes
    ``None`` unambiguous here in a way it is not on the other backends.
    """
    return supervised_pid(cfg, name)


def unit_state(cfg: PodConfig, name: str) -> tuple[str, int]:
    """``(state, restarts)`` shaped like the systemd backend's return.

    Task Scheduler neither restarts a crashed pod nor exposes a restart counter,
    so the pair is derived from two facts the wrapper records: the supervised
    pid, and the exit code of the last boot.

    * a live pid -> ``("active", 0)``
    * no pid and a NON-ZERO recorded result -> ``("failed", 1)``
    * anything else -> ``("inactive", 0)``

    The synthetic ``1`` is the same device the launchd backend uses: it is the
    CRASH SIGNAL ``_wait_healthy`` stops waiting on, not a tally, so it must
    never be shown to a user as a restart count.
    """
    if supervised_pid(cfg, name) is not None:
        return "active", 0
    rc = last_result(cfg, name)
    if rc is not None and rc != 0:
        return "failed", 1
    return "inactive", 0


def active_names(cfg: PodConfig) -> set[str]:
    """Names of pods with a live gateway process.

    Enumerates this plane's pid records instead of listing tasks. A full
    ``schtasks /Query /FO CSV`` dump would have to be filtered on a localized
    status column, and it would also count a task that exists but whose process
    is gone — which systemd's ``--state=active`` filter excludes for us.
    """
    prefix = f"{cfg.unit_prefix}."
    names: set[str] = set()
    try:
        entries = list(cfg.pods_dir.glob(f"{prefix}*.winpid"))
    except OSError:
        return names
    for path in entries:
        candidate = path.name[len(prefix) : -len(".winpid")]
        if candidate and supervised_pid(cfg, candidate) is not None:
            names.add(candidate)
    return names


def recent_journal(cfg: PodConfig, name: str, lines: int = 50) -> str:
    """The journal stand-in: the tail of this pod's own stderr/stdout files."""
    out_log, err_log = log_paths(cfg, name)
    chunks: list[str] = []
    for path in (err_log, out_log):
        try:
            tail = path.read_text(errors="replace").splitlines()[-lines:]
        except OSError:
            continue
        if tail:
            chunks.append(f"== {path.name} ==\n" + "\n".join(tail))
    if not chunks:
        return (
            f"no pod log yet at {err_log.parent} — Task Scheduler has no journal, "
            "so a pod that never started writes nothing here."
        )
    return "\n\n".join(chunks)


def windows_exit_code(code: int) -> int:
    """Pass *code* through unchanged — the honest code is also the safe one here.

    The launchd twin of this function has to translate a terminal refusal to 0
    (``KeepAlive`` restarts on non-zero, so exiting 78 loops every 5s). Task
    Scheduler has no such policy: a task whose action exits non-zero is simply
    recorded with that result and stays down. So the refusal is already
    terminal, the recorded code stays honest, and ``pod ls`` reads the same
    ``.refused`` note it reads everywhere.

    Kept as a named function rather than left implicit so the platform's answer
    is stated somewhere a reader can find it, and pinned by a test — a future
    change that adds a restart policy to the task MUST revisit this, exactly as
    the launchd side's ``KeepAlive`` is pinned.
    """
    return code
