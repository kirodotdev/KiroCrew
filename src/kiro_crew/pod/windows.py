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
would have to elevate to test a worktree, and the pod would stop running as the
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
its exit-0 translation, and it means the crash
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

**5. No cgroups, so the ceiling is a Job object instead — and it IS enforced.**
The systemd unit's ``MemoryMax=4G`` and ``CPUQuota=200%`` are kernel-enforced
cgroup limits with no scheduled-task equivalent, so :func:`supervise_gateway`
attaches a Windows Job object to the gateway instead, through
:func:`kiro_crew.sandbox.apply_windows_resource_ceiling` — the same seam and the
same ``resource_limits`` config the agent-subprocess path uses, so one operator
setting governs both platforms. The child is created ``CREATE_SUSPENDED`` and
resumed only after the job is attached, which is what makes it airtight rather
than merely small: job membership covers a member's future descendants but not
ones it already spawned. Two honest gaps remain. The process row is a LOOSER
bound than the cgroup row (``ActiveProcessLimit`` counts processes where
``TasksMax`` counts threads), and there is no CPU row at all, because a Job
object's CPU rate control is a different mechanism from ``CPUQuota`` and is not
wired here. So do not read this as parity with the systemd unit; read it as a
real fork-bomb and memory ceiling where there was none. macOS still has neither.

Every other isolation property is unchanged: own ``KIROCREW_HOME``, own derived
port, no tunnel, ``--no-crons``, and the refusal to bind the live port.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from kiro_crew.instances import run_marker
from kiro_crew.platform_compat import (
    CREATE_NEW_PROCESS_GROUP,
    CREATE_SUSPENDED,
    IS_WINDOWS,
    attributed_descendants,
)
from kiro_crew.platform_compat import created_after as _created_after_impl
from kiro_crew.platform_compat import (
    pid_exists,
    process_start_time,
    resume_process_main_thread,
    trusted_system_bin,
)
from kiro_crew.pod import _windows_job as jobs
from kiro_crew.pod import _windows_run as runs
from kiro_crew.pod.config import EXIT_REFUSED_UNRECOVERABLE, PodConfig, environment_vars
from kiro_crew.pod.unit import _kirocrew_argv as _shared_kirocrew_argv
from kiro_crew.sandbox import apply_windows_resource_ceiling
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# Task Scheduler folder every pod task lives in. One folder per pod plane, so a
# hermetic test plane (KIROCREW_POD_UNIT_PREFIX) cannot collide with a
# developer's real pods — the same property cfg.unit_prefix buys on the other two
# backends.
TASK_FOLDER_ROOT = r"\KiroCrew\pods"

# Shared bound for publisher retirement and contained-Job draining.
STOP_TIMEOUT_SECS = 15.0

#: Margin used by the producer's handoff-marker refresh.
_HANDOFF_FRESHNESS_MARGIN_SECS = 5.0

#: How long :func:`supervise_gateway` waits for a restart successor to claim the
#: pod's gateway sidecar after the process it supervised exits. Bounds how long a
#: pod can report itself alive after its LAST gateway is gone, so it is a
#: correctness ceiling rather than a comfort setting: too short and an in-app
#: restart is misread as a stop (the fail-OPEN direction — `pod down` would then
#: reclaim a live pod), too long and a genuinely stopped pod lingers as running.
#: The wait is only ever entered while the exited gateway still has a live
#: attributed child, so an ordinary shutdown never pays it.
SUCCESSOR_ADOPT_TIMEOUT_SECS = 30.0


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
    """A per-pod sidecar under the plane's ``pods_dir``.

    Two different trust levels meet in this one f-string, and only one of them is
    validated. *name* has been through ``runtime.validate_name`` because it can
    reach here from a CLI argument. ``cfg.unit_prefix`` has NOT, and deliberately:
    it is operator configuration on exactly the same footing as ``pod_root`` and
    ``pods_dir``, every backend splices it unvalidated (systemd builds
    ``~/.config/systemd/user/<prefix>@.service`` from it, launchd builds its
    label), and the whole pod config surface already points pod state anywhere the
    operator's own identity can write. So a rooted prefix escapes this directory --
    self-directed, no privilege boundary crossed. Validating it HERE alone would be
    theatre; if that surface is to be constrained it belongs at config load, for
    every platform at once, as its own change.
    """
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


def handoff_marker_path(cfg: PodConfig, name: str) -> Path:
    """Compatibility sidecar for supervisor handoff visibility, not drain proof.

    PID identity names a gateway; this marker names a handoff publisher. Neither
    replaces the durable run descriptor and retained Job used by teardown.
    """
    return _plane_file(cfg, name, ".handoff")


def _begin_handoff(cfg: PodConfig, name: str) -> bool:
    """Publish the supervisor identity for restart visibility.

    Publication failure ends adoption; the supervisor drains its lifetime Job
    instead. Marker absence never authorizes cleanup, which requires the
    independent durable run descriptor and kernel proof.
    """
    token = process_start_time(os.getpid()) or ""
    try:
        handoff_marker_path(cfg, name).write_text(f"{os.getpid()}\n{token}\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _end_handoff(cfg: PodConfig, name: str) -> None:
    """Retract the marker once the outcome is recorded, either way."""
    with contextlib.suppress(OSError):
        handoff_marker_path(cfg, name).unlink(missing_ok=True)


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

    * ``setlocal DisableDelayedExpansion`` keeps a ``!`` in a path literal.
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
        # Delayed expansion corrupts literal "!" characters in embedded pod paths.
        "setlocal DisableDelayedExpansion",
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


def _script_encoding() -> str:
    """The codec ``cmd.exe`` reads a batch file with.

    ``cmd.exe`` decodes a ``.cmd`` in the console's OEM code page (``chcp``),
    never UTF-8, so the wrapper is written in that code page: Python's ``oem``
    codec is exactly that page on Windows. Elsewhere (the render tests run on
    Linux) there is no OEM page and UTF-8 stands in.
    """
    return "oem" if IS_WINDOWS else "utf-8"


def write_task_script(cfg: PodConfig, name: str) -> Path:
    """Render and install this pod's wrapper. Returns its path.

    Re-rendered on every ``up``, like the launchd plist and unlike the systemd
    template, so it cannot go stale against a moved worktree or a changed plane.

    Written in the console's OEM code page (:func:`_script_encoding`) and encoded
    STRICTLY: the wrapper carries the plane's paths, and a path with a character
    that page cannot represent (a profile name outside the page's repertoire)
    would be read back by ``cmd.exe`` as different bytes, so the pod would start
    against a path that does not exist. Refusing up front with the offending
    text is the only honest outcome; the operator moves the plane to a path the
    page can spell.
    """
    dst = task_script_path(cfg, name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out_log, _ = log_paths(cfg, name)
    out_log.parent.mkdir(parents=True, exist_ok=True)
    body = render_task_script(cfg, name)
    encoding = _script_encoding()
    try:
        data = body.encode(encoding)
    except UnicodeEncodeError as exc:
        raise WindowsTaskError(
            f"pod {name}: the Task Scheduler wrapper cannot be written in the "
            f"console code page ({encoding}): {exc.object[exc.start:exc.end]!r} in "
            "a pod plane path has no representation there, and cmd.exe would read "
            "the script back as a different path. Point KIROCREW_HOME and the pod "
            "plane (KIROCREW_POD_*) at paths the console code page can spell."
        ) from exc
    # Bytes, not text mode: the body already carries CRLF, and the strict encode
    # above is the one place the code page is applied.
    dst.write_bytes(data)
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
    """Record the serving gateway's PID and creation identity for liveness readers.

    Missing identities and write failures raise: a gateway must not serve without
    a usable visibility record. The supervisor then drains its contained Job.
    This record is independent of the gateway's own sidecar for port attestation;
    neither its disappearance nor PID death is a runtime reclamation certificate.
    """
    token = process_start_time(pid)
    if not token:
        # A record with no identity is the same fabrication as no record: every
        # reader compares the stored token against the live process, and a blank
        # never matches, so the pod would read as stopped while its gateway
        # serves. OSError is what the caller already treats as "unrecordable".
        raise OSError(f"could not read the creation-time identity of pid {pid}")
    record = pid_record_path(cfg, name)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(f"{pid}\n{token}\n", encoding="utf-8")


def clear_supervised_pid(cfg: PodConfig, name: str) -> None:
    """Drop pod *name*'s pid record (the gateway has exited)."""
    try:
        pid_record_path(cfg, name).unlink(missing_ok=True)
    except OSError:
        pass


def _read_pid_record(cfg: PodConfig, name: str) -> tuple[int, str] | None:
    try:
        raw = pid_record_path(cfg, name).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if not raw or not raw[0].strip().isdigit():
        return None
    return int(raw[0].strip()), (raw[1].strip() if len(raw) > 1 else "")


def supervised_pid(cfg: PodConfig, name: str) -> int | None:
    """Pod *name*'s live gateway pid, PROVEN to still be that process, or ``None``.

    Fails CLOSED on every way of not knowing — no record, no recorded identity,
    a host that will not report a creation time, or a token that does not
    matches. Each of those must read as "this pod has no process", never as a
    pid a caller may go on to signal.

    **The creation token answers IDENTITY, never LIVENESS, and asking it for
    liveness inverts this function's fail direction.** A Windows process object
    outlives the process itself for as long as any handle to it is open, and its
    creation ``FILETIME`` stays readable that whole time — so ``==`` against the
    recorded token keeps matching a gateway that has already exited. This path
    has a guaranteed handle holder: :func:`supervise_gateway` spawns the gateway
    through ``subprocess.Popen`` and sits in ``proc.wait()``, which holds the
    process handle open until it reaps. The result was a pod that read as
    running after its gateway was gone, which made ``stop`` refuse a teardown
    with nothing left to tear down and report the pod NOT zero-residue.
    :func:`kiro_crew.platform_compat.pid_exists` is the liveness answer (it
    reads ``GetExitCodeProcess`` rather than the creation time), so it is asked
    FIRST and the token then narrows a live pid to the right process. Both are
    needed: existence alone would signal a recycled pid, identity alone reports
    a corpse as a pod.
    """
    record = _read_pid_record(cfg, name)
    if record is None:
        return None
    pid, recorded = record
    if pid <= 0 or not recorded:
        return None
    if not pid_exists(pid):
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
    try:
        prior = runs.read(cfg, name)
        if prior is not None and prior["state"] == "cancelled":
            _rollback_start(cfg, name, prior)
        if _stop_state_path(cfg, name) is not None or task_exists(cfg, name):
            raise OSError("prior Windows pod state must be retired before starting again")
        reservation = runs.reserve(cfg, name)
    except (OSError, ValueError, subprocess.SubprocessError, WindowsTaskError) as exc:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=str(exc))
    try:
        script = write_task_script(cfg, name)
        # Refuse before scheduling if readers could see an old failure result.
        result_path(cfg, name).unlink(missing_ok=True)
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
            raise WindowsTaskError(
                f"schtasks /Create rc={created.returncode}: "
                f"{(created.stderr or created.stdout or '').strip()}"
            )
    except (OSError, ValueError, subprocess.SubprocessError, WindowsTaskError) as exc:
        detail = str(exc)
        try:
            _rollback_start(cfg, name, reservation)
        except (OSError, ValueError, subprocess.SubprocessError, WindowsTaskError) as cleanup:
            detail += (
                f"; startup rollback incomplete: {cleanup}. Run evidence retained; "
                f"retry kirocrew pod up {name} after correcting the cleanup error."
            )
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=detail)
    # /Run can fail or time out AFTER spawning. Never revoke its reservation:
    # absence of a claim at this instant does not prove no publisher will claim.
    try:
        return schtasks("/Run", "/TN", task_name(cfg, name))
    except (OSError, ValueError, subprocess.SubprocessError, WindowsTaskError) as exc:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=f"{exc}; run evidence preserved"
        )


def _rollback_start(cfg: PodConfig, name: str, reservation: dict) -> None:
    """Clean only an explicitly cancelled pre-/Run generation, never a runtime.

    The caller holds the CLI name mutex. The admission lock additionally keeps
    a supervisor from claiming between the identity check and sidecar removal.
    Cancellation survives cleanup failure; a fresh start may retry that receipt,
    but must not infer cancellation from an ordinary reserved record.
    """
    with runs.cancel_reserved(cfg, name, reservation):
        for evidence in (
            pid_record_path(cfg, name),
            handoff_marker_path(cfg, name),
            cfg.home_dir(name),
        ):
            try:
                evidence.lstat()
            except FileNotFoundError:
                continue
            raise OSError(f"runtime evidence at {evidence}; startup rollback refused")
        if task_exists(cfg, name):
            deleted = schtasks("/Delete", "/TN", task_name(cfg, name), "/F")
            if deleted.returncode != 0:
                raise OSError(
                    f"schtasks /Delete rc={deleted.returncode}: "
                    f"{(deleted.stderr or deleted.stdout or '').strip()}"
                )
        task_script_path(cfg, name).unlink(missing_ok=True)
        result_path(cfg, name).unlink(missing_ok=True)


def created_after(child_token: str, parent_token: str) -> bool:
    """Whether a process the parent map lists under the gateway is really its child.

    Thin local name for :func:`kiro_crew.platform_compat.created_after`, which is
    where the rule lives -- beside :func:`process_descendants`, the primitive whose
    stale parent pids it compensates for. Kept as a name here because ``stop`` and
    ``pod.runtime.port_owner`` both read it through this module, and because the
    reasoning belongs with the primitive rather than with one of its callers.
    """
    return _created_after_impl(child_token, parent_token)


def _still_alive(pid: int, token: str) -> bool:
    """Whether *pid* is STILL RUNNING and still the process *token* identified.

    The same inversion :func:`supervised_pid` documents, at the other place this
    backend decides liveness. A creation ``FILETIME`` stays readable for as long
    as any handle to the process object is open, which outlives the process, so
    ``process_start_time(pid) == token`` alone reports an exited child as a live
    one — and every survivor here is re-probed with exactly that comparison to
    decide whether the pod left residue. Reading a corpse as residue is the
    fail-OPEN direction for the operator (``pod down`` refuses, preserves the
    HOME and reports NOT zero-residue for a pod that is entirely gone), so
    existence is asked first and the token only narrows a live pid to the right
    process. An unreadable token is not attributable and is not treated as a
    match, matching :func:`supervised_pid`.
    """
    return pid_exists(pid) and process_start_time(pid) == token


def _stop_state_path(cfg: PodConfig, name: str) -> Path | None:
    """Existing or unreadable pod state is evidence, never proof of writer death."""
    for path in (
        runs.path(cfg, name),
        pid_record_path(cfg, name),
        task_script_path(cfg, name),
        result_path(cfg, name),
        handoff_marker_path(cfg, name),
        cfg.home_dir(name),
    ):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            pass  # An unreadable path cannot certify a never-started plane.
        return path
    return None


def stop(
    cfg: PodConfig, name: str, *, timeout: float = STOP_TIMEOUT_SECS
) -> subprocess.CompletedProcess:
    """Retire the publisher, drain its boot-contained Job, then delete the task.

    A legacy PID/marker/tree snapshot cannot establish boot-time containment.
    Such runs preserve HOME and task rather than granting a token-only fallback.
    A durable receipt permits retry after the Job disappears, but only after its
    publisher is proven retired. The caller keeps the name mutex through HOME
    reclamation and consumes the receipt after all seven cleanup sweeps.
    """
    try:
        record = runs.read(cfg, name)
        if record is None:
            if _stop_state_path(cfg, name) is not None or task_exists(cfg, name):
                raise OSError("legacy or unrecorded runtime has no boot-contained Job proof")
        elif record["state"] not in {"ready", "drained"}:
            raise OSError("boot publication is incomplete; its runtime cannot be reclaimed")
        else:
            publisher_pid, publisher_token = record["publisher"]
            if publisher_pid == os.getpid():
                raise OSError("refusing to retire the calling process as a pod publisher")
            with contextlib.ExitStack() as stack:
                publisher = jobs.open_identity(publisher_pid, publisher_token)
                if publisher is not None:
                    stack.callback(jobs.close_identity, publisher)
                job = None
                if record["state"] == "ready":
                    # Open BEFORE /End can destroy the last publisher-held handle.
                    # An absent Job is never replaced with an empty one.
                    job = stack.enter_context(jobs.PodJob.open_existing(record["job"]))
                    root = jobs.open_identity(*record["root"])
                    if root is not None:
                        stack.callback(jobs.close_identity, root)
                        if not job.contains(root):
                            raise OSError("recorded initial process is outside its lifetime Job")
                schtasks("/End", "/TN", task_name(cfg, name))
                if publisher is not None:
                    jobs.retire_identity(publisher, timeout=timeout)
                current = runs.read(cfg, name)
                if current is None or {**current, "state": "ready"} != {**record, "state": "ready"}:
                    raise OSError("run identity changed while retiring its publisher")
                if job is not None:
                    job.terminate_and_wait(timeout=timeout)
                    runs.drained(cfg, name, record)
                # A stored receipt represents a kernel-zero proof with no further
                # spawn/resume from that publisher. It survives cleanup retries.
        deleted = schtasks("/Delete", "/TN", task_name(cfg, name), "/F")
        if deleted.returncode != 0 and task_exists(cfg, name):
            raise OSError("the drained pod's scheduled task could not be deleted")
        task_script_path(cfg, name).unlink(missing_ok=True)
        # Supervisor cleanup is best-effort; authoritative teardown must surface
        # errors so its durable receipt remains available for a later retry.
        handoff_marker_path(cfg, name).unlink(missing_ok=True)
        pid_record_path(cfg, name).unlink(missing_ok=True)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"pod {name!r}: {exc}. HOME and remaining task/run evidence were "
            "preserved; this pod is NOT proven zero-residue.",
        )


def _live_children_of(pid: int, token: str) -> list[int]:
    """Attributed live children of *pid*, cheapest possible successor pre-check.

    Measured on this platform: CPython's ``os.execv`` is CreateProcess plus an
    exit of the caller, so a restart successor is a genuine CHILD of the process
    it replaces and appears in the parent map BEFORE that process is reaped. This
    is therefore a zero-latency answer to "could a successor exist at all", which
    is what keeps the sidecar poll below off the ordinary shutdown path.

    Attributed PER EDGE by :func:`attributed_descendants`, not by testing a
    flattened list against the ROOT's token. The difference decides whether a
    stranger's process can be killed: a grandchild that postdates the root is
    admitted by a root-token test even when the intermediate pid it hangs off has
    been RECYCLED, so a stale orphan under that recycled number reads as ours.
    The ``Popen`` handle the caller still holds pins the ROOT's number against
    reuse; it says nothing about intermediate descendants. Both of this
    function's callers hand their result to a tree kill, so an unattributable
    child has to be dropped WITH its subtree rather than merely doubted.
    """
    return [child for child in attributed_descendants(pid, token) if pid_exists(child)]


def _restart_successor(record: Path, reaped_pid: int) -> int | None:
    """The pid of a live gateway that REPLACED the one at *reaped_pid*, or None.

    Read from the gateway's OWN pid sidecar inside the pod home — the file a
    booting gateway rewrites with its pid and start identity — rather than from
    the process tree. The tree can only say "a live child exists", and a gateway
    legitimately spawns children (MCP servers, agent sessions); adopting one of
    those as the pod's gateway would be worse than adopting nothing. Claiming
    that sidecar is what makes a process the gateway, so it is the only
    authoritative answer available to a different process.

    Fails CLOSED exactly like :func:`kiro_crew.pod.runtime._pod_recorded_pid`,
    whose reader this mirrors: an absent sidecar, a missing start identity, a pid
    that is not live, or a token that does not match all read as "no successor".
    The reaped pid itself is excluded — a sidecar the predecessor wrote and never
    got to clear is a leftover, not a successor.
    """
    parsed = run_marker.read_pid_record_path(record)
    if parsed is None:
        return None
    pid, recorded_start = parsed
    if pid <= 0 or pid == reaped_pid or not recorded_start:
        return None
    if not pid_exists(pid):
        return None
    live_start = run_marker.pid_start_token(pid)
    return pid if live_start and live_start == recorded_start else None


def _await_successor(record: Path, reaped_pid: int, reaped_token: str) -> int | None:
    """Wait, BOUNDED and only when a successor could exist, for one to claim the pod.

    Two signals, each covering the other's blind spot. The process tree answers
    instantly but cannot tell a restart successor from an ordinary child, so it
    is used only to decide whether waiting is warranted at all: with no live
    attributed child there is provably nothing to adopt and an ordinary shutdown
    pays nothing. The sidecar names the gateway authoritatively but only once the
    successor has booted far enough to write it, so it is polled — for
    :data:`SUCCESSOR_ADOPT_TIMEOUT_SECS`, which bounds how long a pod can appear
    alive after its last gateway is gone.

    Returns the successor's pid, or ``None`` when the window closes with no
    claim — at which point the pod really has stopped.
    """
    deadline = time.monotonic() + SUCCESSOR_ADOPT_TIMEOUT_SECS
    while time.monotonic() < deadline:
        successor = _restart_successor(record, reaped_pid)
        if successor is not None:
            return successor
        if not _live_children_of(reaped_pid, reaped_token):
            return None
        time.sleep(0.2)
    return None


def _wait_for_pid(pid: int, token: str, *, on_poll: Callable[[], None] | None = None) -> None:
    """Block until *pid* stops being the process *token* named.

    The adopted successor was not spawned by this process, so there is no
    ``Popen`` to wait on and the only portable answer is to poll its identity.
    :func:`_still_alive` is the predicate for the reason it documents: the
    creation token alone would keep matching a corpse.

    *on_poll* runs once per iteration WHILE the target is alive. The supervisor
    uses it to keep the handoff marker fresh: detection here lags death by up to
    the poll interval, so a marker re-stamped only after this returns leaves that
    lag unprotected, and on a 2nd+ handoff the previous stamp is long stale. A
    marker refreshed from inside the poll cannot open that gap, and it still goes
    stale on its own once the supervisor itself dies -- which is what keeps a dead
    supervisor from wedging `pod down` forever.
    """
    while _still_alive(pid, token):
        if on_poll is not None:
            on_poll()
        time.sleep(0.5)


def _refresh_handoff_if_stale(cfg: PodConfig, name: str) -> None:
    """Re-stamp the handoff marker only as it approaches its freshness bound.

    Called from the successor poll, which runs twice a second for the whole life of
    an adopted gateway -- days, on a long-lived pod. Writing every iteration would
    be two file writes a second forever to keep a value that only has to stay
    younger than SUCCESSOR_ADOPT_TIMEOUT_SECS + 5. Refreshing at half that bound
    keeps the same guarantee (the marker is never within a poll interval of going
    stale while the supervisor lives) at one write per ~17s.

    Best-effort by design: this is the REFRESH, and a failure here only shortens the
    protection the next poll re-establishes. The publication that must be judged is
    `_begin_handoff`'s, whose verdict the caller acts on.
    """
    marker = handoff_marker_path(cfg, name)
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        # Unreadable or absent: re-publishing is the safe direction, and an
        # unreadable marker already counts as a live handoff to `stop`.
        age = None
    if age is None or age >= (SUCCESSOR_ADOPT_TIMEOUT_SECS + _HANDOFF_FRESHNESS_MARGIN_SECS) / 2:
        _begin_handoff(cfg, name)


def supervise_gateway(
    cfg: PodConfig,
    name: str,
    bin_path: Path,
    argv: list[str],
    env: dict[str, str],
    *,
    gateway_pid_record: Path,
) -> int:
    """Contain the suspended gateway before publishing and resuming it.

    The CLI reservation is claimed once, independently of the CLI name mutex.
    All restart descendants inherit the lifetime Job, including branches whose
    intermediaries disappear before either supervisor or stop can observe them.
    Gateway sidecars still select the serving gateway; they never certify drain.
    The terminal receipt is published only after the final kernel-zero proof,
    with no further gateway spawn/resume possible from this invocation.
    """
    record = runs.claim(cfg, name)
    with jobs.PodJob.create() as job:
        proc = None
        try:
            proc = subprocess.Popen(
                [str(bin_path), *argv],
                env=env,
                creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED,
                close_fds=False,
            )
            native = int(getattr(proc, "_handle"))
            job.assign_suspended(native)
            identity = jobs.pc._windows_process_handle_identity(native)
            if identity is None or identity[0] != proc.pid or identity[2] is not None:
                raise OSError("the suspended gateway has no provable creation identity")
            # Resource ceilings remain optional and use the existing settings;
            # the separate lifetime Job is mandatory and already contains it.
            apply_windows_resource_ceiling(proc.pid)
            record_supervised_pid(cfg, name, proc.pid)
            record = runs.ready(cfg, name, record, job.name, (proc.pid, str(identity[1])))
            if not resume_process_main_thread(proc.pid):
                raise OSError("the contained gateway could not be resumed")
            rc = proc.wait()
            protected = _begin_handoff(cfg, name)
            reaped_pid, reaped_token = proc.pid, str(identity[1])
            named = run_marker.read_pid_record_path(gateway_pid_record)
            if named is not None and named[0] != proc.pid and not pid_exists(named[0]):
                reaped_pid, reaped_token = named
            while protected:
                successor = _await_successor(gateway_pid_record, reaped_pid, reaped_token)
                if successor is None:
                    break
                token = process_start_time(successor)
                if not token:
                    raise OSError("restart successor identity is unavailable")
                handle = jobs.open_identity(successor, token)
                if handle is None:
                    break
                try:
                    if not job.contains(handle):
                        raise OSError("restart successor is outside this pod's lifetime Job")
                    record_supervised_pid(cfg, name, successor)
                    _wait_for_pid(
                        successor, token, on_poll=lambda: _refresh_handoff_if_stale(cfg, name)
                    )
                finally:
                    jobs.close_identity(handle)
                reaped_pid, reaped_token = successor, token
                protected = _begin_handoff(cfg, name)
            return rc
        except (OSError, ValueError, AttributeError) as exc:
            print(f"FATAL: Windows pod lifetime containment refused: {exc}")
            return EXIT_REFUSED_UNRECOVERABLE
        finally:
            try:
                # This also covers an assignment failure: the original Popen
                # handle owns the still-suspended child, without a PID fallback.
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
                job.terminate_and_wait(timeout=STOP_TIMEOUT_SECS)
                _end_handoff(cfg, name)
                clear_supervised_pid(cfg, name)
                if record["state"] == "ready":
                    runs.drained(cfg, name, record)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                # Keep the run descriptor: a consumer can retry the exact Job,
                # but must never infer completion from this publisher's exit.
                print(f"FATAL: Windows pod drain remains unproven: {exc}")
                raise


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
