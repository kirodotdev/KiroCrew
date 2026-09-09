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
    SIGTERM,
    attributed_descendants,
)
from kiro_crew.platform_compat import created_after as _created_after_impl
from kiro_crew.platform_compat import (
    kill_process_tree_pinned,
    pid_exists,
    process_start_time,
    resume_process_main_thread,
    trusted_system_bin,
)
from kiro_crew.pod.config import EXIT_REFUSED_UNRECOVERABLE, PodConfig, environment_vars
from kiro_crew.pod.unit import _kirocrew_argv as _shared_kirocrew_argv
from kiro_crew.sandbox import apply_windows_resource_ceiling
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

# A predecessor can exit one Python statement before the supervisor publishes
# its handoff marker. Settle a missing pid briefly so teardown sees that marker
# or the successor's pid record instead of spending the transient empty state.
_HANDOFF_SETTLE_TIMEOUT_SECS = 1.0
_HANDOFF_SETTLE_POLL_SECS = 0.05

#: Margin added to :data:`SUCCESSOR_ADOPT_TIMEOUT_SECS` when judging a handoff
#: marker's freshness, and the same bound teardown waits for one to resolve. Named
#: once so the writer's ceiling, the reader's bound and the wait cannot drift.
_HANDOFF_FRESHNESS_MARGIN_SECS = 5.0
_HANDOFF_OUTCOME_POLL_SECS = 0.2

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
    """Where the supervisor records that a restart HANDOFF is in progress.

    Exists because the pid record cannot express "alive, pid not known yet". When
    ``proc.wait()`` returns, the predecessor is reaped and ``supervised_pid`` fails
    closed on a dead pid, so the pod reads STOPPED for as long as it takes the
    successor to claim its sidecar. The stop path settles that transient reading
    before acting, and where it finds a marker it WAITS, bounded, for the supervisor
    to decide rather than refusing on sight -- the marker is published on every reap,
    including the one teardown itself causes.

    A separate marker rather than a stale pid record, because every reader of the
    record wants "which process", and answering that with a corpse is the fail-OPEN
    direction this whole module exists to remove. This file answers a different
    question -- "is a handoff underway" -- and only teardown asks it.
    """
    return _plane_file(cfg, name, ".handoff")


def handoff_in_progress(cfg: PodConfig, name: str) -> bool:
    """Whether a restart handoff is underway: judged by its PUBLISHER first.

    A handoff is something a supervisor is *doing*, so the honest question is
    whether the supervisor that published this marker is still running -- which
    the marker answers directly, because it names its writer by pid and
    creation-time token (:func:`_begin_handoff`), the same identity proof every
    other reading in this module uses instead of a bare pid.

    * **Publisher alive** (pid live and token matches): a handoff, whatever the
      marker's age. Age is wall-clock, and a suspended machine keeps a marker
      ageing while its handoff stands still, so a ``pod down`` run first thing
      after a long sleep must not read a live handoff as expired and delete the
      task and HOME beneath the successor.
    * **Publisher named but gone**: NOT a handoff. ``/End`` reaps the supervisor,
      and it can do so between the reap of its gateway (which publishes this
      marker as its very next statement) and the ``finally`` that retracts it.
      The marker is then ORPHANED, and reading it as a live handoff makes
      ``pod down`` refuse a pod it has itself just stopped -- leaving the
      scheduled task registered, which is the residue teardown exists to remove.
      Time cannot tell that marker from a real one; its writer's liveness can.
    * **No identity** (an unparseable marker, or one written by a build that
      carries none): fail closed for as long as a handoff could legitimately
      still be running -- the adoption timeout the supervisor itself obeys plus a
      small margin -- and not past it, so such a marker cannot wedge teardown
      forever.

    An unreadable marker (a stat error) counts as a live handoff: the
    alternative is deleting a serving pod's HOME on the strength of an I/O error.

    The protection this does NOT give up: an unrecorded successor left behind by
    a killed supervisor is still caught, by the survivor snapshot taken before
    anything is signalled and by the still-running check, both in :func:`stop`.
    """
    marker = handoff_marker_path(cfg, name)
    try:
        raw = marker.read_text(encoding="utf-8")
        age = time.time() - marker.stat().st_mtime
    except FileNotFoundError:
        return False
    except OSError:
        return True
    publisher = _handoff_publisher(raw)
    if publisher is not None:
        pid, token = publisher
        return pid_exists(pid) and process_start_time(pid) == token
    return age <= SUCCESSOR_ADOPT_TIMEOUT_SECS + _HANDOFF_FRESHNESS_MARGIN_SECS


def _handoff_publisher(raw: str) -> tuple[int, str] | None:
    """The ``(pid, creation-token)`` a marker names, or ``None`` if it names none.

    ``None`` is the fail-CLOSED answer for every unreadable shape -- an older
    build's literal payload, a truncated write, a non-numeric first line -- so a
    marker this function cannot judge keeps blocking teardown rather than being
    waved through.
    """
    lines = raw.splitlines()
    if len(lines) < 2 or not lines[0].strip().isdigit() or not lines[1].strip():
        return None
    return int(lines[0].strip()), lines[1].strip()


def _begin_handoff(cfg: PodConfig, name: str) -> bool:
    """Publish the handoff marker, reporting whether the window is protected.

    A failure here must not CRASH the supervisor, but it must not be mistaken for
    a published marker either: the marker is the only thing that stops a
    concurrent teardown from deleting the isolated HOME while a successor boots,
    so an unpublished one means the protection does not exist. The caller answers
    that by ending the successor rather than serving it unprotected — losing an
    in-app restart is recoverable, losing the HOME under a live gateway is not.

    The payload NAMES THIS PROCESS -- pid plus its creation-time token, the same
    two-fact identity the pid record carries -- so a reader can ask whether the
    supervisor that claimed this window still exists rather than inferring it from
    the file's age. ``/End`` can reap this process between the publication and the
    retraction in the ``finally``, and an orphan left that way is indistinguishable
    from a live handoff by mtime alone (see :func:`handoff_in_progress`). Written as
    one payload rather than appended, so a reader never sees a half-built record.
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
    """Record *pid* as pod *name*'s gateway, bound to its start identity.

    A bare pid is not an identity: a wrapper terminated without running its
    cleanup leaves the record behind, and Windows recycles pids. The
    creation-time token is what lets :func:`supervised_pid` refuse a recycled
    number instead of reporting an unrelated process as the pod.

    **Raises on failure, because this record IS the pod's liveness.** Every
    Windows reader of "is this pod running" resolves through it: with no record
    ``supervised_pid`` answers None, ``is_active`` reports the pod down, and
    ``stop`` skips both the tree kill and the still-alive guard, so it deletes
    the task and the isolated HOME while the gateway is serving and reports
    success. A silent degrade here therefore does not lose a diagnostic, it
    fabricates a stopped pod, which is the fail-OPEN direction this backend
    refuses everywhere else. :func:`supervise_gateway` catches the raise,
    terminates the child it just spawned, and exits non-zero. A creation time
    that cannot be read is refused the same way: a record carrying a blank token
    is one no reader can ever match, so it would fabricate the same stopped pod.
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
    except OSError:
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
    script = write_task_script(cfg, name)
    # Clear the previous run's result HOST-side too. The wrapper also does it,
    # but only once the task has started: until then unit_state would read the
    # stale code and report this fresh start as already failed.
    #
    # REFUSED, not best-effort, when the removal fails. A start that proceeds with
    # a stale FAILURE result left in place hands every reader a lie in the one
    # direction that destroys data: `unit_state` reports the pod that is booting
    # right now as already failed, and a caller acting on a failed boot reclaims
    # its state -- deleting the isolated HOME of a pod that is coming up. Refusing
    # before `/Create` costs an operator one diagnosable error; proceeding costs
    # them the home.
    stale = result_path(cfg, name)
    try:
        stale.unlink(missing_ok=True)
    except OSError as exc:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                f"pod {name!r} cannot start: the previous run's result file could not "
                f"be removed ({exc}).\n"
                f"  Left in place:  {stale}\n"
                "  Why this stops the start: until that file is gone every reader of "
                "this pod's state reads the OLD exit code, so a pod that boots "
                "successfully still reports as failed -- and a caller that acts on a "
                "failed boot reclaims the pod's isolated home.\n"
                f"  Remove it, then retry: kirocrew pod up {name}"
            ),
        )
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


def _describe_processes(pids: list[int]) -> str:
    """One ``tasklist`` line per pid, for the refusal report. Best-effort."""
    tasklist = trusted_system_bin("tasklist")
    if tasklist is None:
        return ""
    lines: list[str] = []
    for pid in pids:
        try:
            r = subprocess.run(
                [tasklist, "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                timeout=10,
                **UTF8_TEXT,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        lines.append(f"  pid {pid}: {(r.stdout or r.stderr or '').strip()}")
    return "\n".join(lines)


def _kill_tree_quietly(pid: int, token: str) -> None:
    """Pinned tree kill whose outcome is judged by re-probing, not by its raise.

    ``taskkill /T`` reports rc=128 ("no running instance") when any member of
    the tree exits between the snapshot and the kill — a child reaped by its
    own exiting parent is the ordinary case on this path — and that surfaces as
    ``ProcessLookupError`` even though the target is gone, which is the outcome
    wanted. An access denial or any other failure is equally not a verdict: the
    caller re-reads every pinned identity afterwards and refuses the stop when
    one is still alive, so nothing is lost by swallowing the raise here, and
    letting it escape would abort ``pod down`` with a traceback instead of the
    preserved-HOME report.
    """
    with contextlib.suppress(OSError):
        kill_process_tree_pinned(pid, token, SIGTERM)


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
    if any child it had when the stop began is still alive (the pid record says
    nothing about children, so they are snapshotted first and ended, pinned,
    after the gateway), or the task could not be deleted, this returns a failure
    and keeps the wrapper script: the caller must not tear down state that may
    belong to a live pod. A ``/End`` or ``/Delete`` against a task that is not there is a
    no-op, not a failure, which is why both are judged by re-probing existence
    rather than by their own exit code.

    The caller still owns the HOME removal (see ``runtime.stop_pod``) because
    that goes through ``cleanup_home``'s name re-validation.
    """
    # Snapshot the gateway's descendants BEFORE anything is signalled, each with
    # its creation-time identity, keeping only those created after the gateway
    # (see created_after for the recycled-pid stray this excludes). The pid record answers only for the gateway
    # itself, so "the record is gone" proves the gateway exited and nothing
    # about its children (kiro-cli sessions, MCP servers): a child that
    # survived would keep writing into the HOME the caller is about to delete.
    # The Job object attached at spawn bounds the tree but does not end it on
    # close (see platform_compat's KILL_ON_JOB_CLOSE note), so the survivors are
    # ended here, pinned by the identity read now so a recycled pid is never
    # signalled.
    survivors: dict[int, str] = {}
    gateway_pid = supervised_pid(cfg, name)
    handoff = handoff_in_progress(cfg, name)
    if gateway_pid is None and not handoff:
        settle_deadline = time.monotonic() + _HANDOFF_SETTLE_TIMEOUT_SECS
        while gateway_pid is None and not handoff and time.monotonic() < settle_deadline:
            time.sleep(_HANDOFF_SETTLE_POLL_SECS)
            gateway_pid = supervised_pid(cfg, name)
            handoff = handoff_in_progress(cfg, name)
    if gateway_pid is None and handoff and _await_handoff_outcome(cfg, name, bound=timeout):
        # The handoff resolved while we waited, so re-read the pid it settled on and
        # carry on into the ordinary path. Refusing here without waiting was the same
        # defect the pre-`/Delete` gate had -- and worse, because this arm returns
        # before `/End` runs at all, so it left the scheduled task registered AND the
        # pod running. This is the gate the canary's own retry hit.
        gateway_pid = supervised_pid(cfg, name)
        handoff = False
    if gateway_pid is None and handoff:
        # The pid record cannot name a process right now because a restart handoff
        # is underway: the predecessor is reaped and the successor has not claimed
        # its sidecar yet. "No record" therefore does NOT mean "nothing is running",
        # and proceeding would delete the task and let the caller rmtree the isolated
        # HOME under a gateway that is booting into it. Refusing is recoverable --
        # the marker goes stale on its own ceiling if the supervisor died -- while the
        # deletion is not.
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                f"pod {name!r} is handing off to a restarted gateway, so it has no "
                "settled pid to stop and its state must not be reclaimed yet.\n"
                f"  Retry in a moment: kirocrew pod down {name}\n"
                f"  What is up:        kirocrew pod ls"
            ),
        )
    gateway_token = process_start_time(gateway_pid) if gateway_pid is not None else None
    if gateway_pid is not None and gateway_token:
        # EVERY edge is attributed, not just "created after the gateway". The root
        # comparison alone admits a stale orphan sitting under a RECYCLED
        # INTERMEDIATE pid: it too was created after the gateway, it is unrelated to
        # this pod, and it is frequently a same-user process this call could
        # successfully terminate. Killing a stranger's tree is not recoverable, so
        # the walk drops an unattributable child together with its subtree.
        for child in attributed_descendants(gateway_pid, gateway_token):
            child_token = process_start_time(child)
            if child_token:
                survivors[child] = child_token
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
            _kill_tree_quietly(pid, token)
        grace = time.monotonic() + 5.0
        while time.monotonic() < grace and supervised_pid(cfg, name) is not None:
            time.sleep(0.2)
    for child, child_token in survivors.items():
        if _still_alive(child, child_token):
            _kill_tree_quietly(child, child_token)
    grace = time.monotonic() + 5.0
    while time.monotonic() < grace and any(_still_alive(c, t) for c, t in survivors.items()):
        time.sleep(0.2)
    orphans = sorted(c for c, t in survivors.items() if _still_alive(c, t))
    if orphans:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=ended.stdout or "",
            stderr=(
                f"the gateway for pod {name!r} exited but {len(orphans)} of its child "
                f"processes are still running after a pinned tree kill (pids "
                f"{orphans}). Its HOME and task were preserved; this pod is NOT "
                f"zero-residue.\n{_describe_processes(orphans)}"
            ),
        )
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
    unattributable = _unattributable_live_pid(cfg, name)
    if unattributable is not None:
        # A record whose pid is ALIVE but whose creation-time identity does not
        # match is the one shape that reads as "stopped" while a process is
        # serving. Deleting the task here hands the caller a rc=0 it would
        # reclaim the HOME on, out from under that process. A record for a pid
        # that is GONE is the ordinary hard-stop leftover and passes through.
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=ended.stdout or "",
            stderr=(
                f"pod {name!r} has a pid record at {pid_record_path(cfg, name)} "
                f"naming pid {unattributable}, which is ALIVE but does not carry "
                "the creation-time identity the record was written with. This pod "
                "cannot be proven stopped, so its task and HOME were preserved. "
                f"Inspect pid {unattributable} (its own start time against that "
                "record's second line): end it by hand if it is this pod's "
                "gateway, or delete the record if the pid has been recycled onto "
                f"something else, then re-run `kirocrew pod down {name}`."
            ),
        )
    late_pid = supervised_pid(cfg, name)
    if late_pid is not None:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=ended.stdout or "",
            stderr=(
                f"the gateway for pod {name!r} appeared during teardown as pid "
                f"{late_pid}. Its HOME and task were preserved; retry "
                f"`kirocrew pod down {name}`."
            ),
        )
    if handoff_in_progress(cfg, name) and not _await_handoff_outcome(cfg, name, bound=timeout):
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=ended.stdout or "",
            stderr=(
                f"pod {name!r} is handing off to a restarted gateway and had not "
                "settled by the time teardown gave up waiting, so its task and HOME "
                "were preserved.\n"
                f"  Retry:             kirocrew pod down {name}\n"
                f"  What is up:        kirocrew pod ls"
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
    wrapper = task_script_path(cfg, name)
    try:
        wrapper.unlink(missing_ok=True)
    except OSError as exc:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=deleted.stdout or "",
            stderr=(
                f"pod {name!r} stopped but its task wrapper at {wrapper} could "
                f"not be deleted: {exc}. Its HOME was preserved."
            ),
        )
    # Same rule for the handoff marker. A settled teardown routinely leaves one
    # behind now -- the reap `/End` caused publishes it -- and while nothing keys on
    # a stale marker, leaving per-pod state on disk after the pod is gone is exactly
    # what the wrapper unlink above exists to prevent.
    _end_handoff(cfg, name)
    clear_supervised_pid(cfg, name)
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=ended.stdout or "", stderr="")


def _await_handoff_outcome(cfg: PodConfig, name: str, *, bound: float | None = None) -> bool:
    """Wait, BOUNDED, for a handoff seen during teardown to RESOLVE. True = settled.

    A marker's presence is not a verdict, it is a question a supervisor has not yet
    answered: :func:`supervise_gateway` publishes one the moment the process it
    supervised is reaped and only THEN looks for a successor. So teardown waits for
    the answer instead of assuming the worst, which is what the refusal's own advice
    ("retry in a moment") was asking every caller to do by hand.

    Three outcomes, one of them safe to delete state on:

    * the marker is retracted -- the supervisor DECIDED, and :func:`_end_handoff`
      runs on every exit of the successor loop, including the one that ends an
      unadoptable successor -- so teardown may finish;
    * a pid is recorded again -- a successor WAS adopted and is running, which is a
      different refusal from this one, so report unsettled and let the caller say so;
    * the bound expires with the marker still live -- undecided, so refuse.

    A marker that goes STALE also settles, and deliberately: staleness means nothing
    is refreshing it, which this module already treats as "its supervisor is presumed
    dead" (:func:`handoff_in_progress`) and trades for bounded teardown rather than
    an unbounded refusal. That is the pre-existing trade, not a new one.

    The default bound is the supervisor's own adoption ceiling plus the same margin
    the freshness test uses, because that is the longest a legitimate handoff can
    stay undecided. It is injectable so ``stop``'s own ``timeout`` can shorten it --
    without that, a caller passing a small ``timeout`` could still block here for the
    full ceiling.
    """
    if bound is None:
        bound = SUCCESSOR_ADOPT_TIMEOUT_SECS + _HANDOFF_FRESHNESS_MARGIN_SECS
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        if not handoff_in_progress(cfg, name):
            return True
        if supervised_pid(cfg, name) is not None:
            return False
        time.sleep(_HANDOFF_OUTCOME_POLL_SECS)
    return not handoff_in_progress(cfg, name)


def _unattributable_live_pid(cfg: PodConfig, name: str) -> int | None:
    """The recorded pid when it is ALIVE but cannot prove it is this pod's gateway.

    ``supervised_pid`` collapses four different answers into ``None``: no record,
    a junk record, a record with no creation-time token, and a token that does
    not match the live process. The first three are the ordinary shapes of a pod
    that is genuinely down, and the LAST one is too whenever the pid is gone --
    a hard ``/End`` reaps the wrapper before its cleanup runs, so a stale record
    naming a dead pid is the routine leftover.

    The one dangerous shape is a record naming a pid that is still ALIVE and
    still unattributable, which is what a fragment written by a failed record
    write, or a pid recycled onto another process, looks like. There ``None``
    from ``supervised_pid`` means "cannot tell", not "not running", and teardown
    has to refuse rather than delete state a live process may own.

    Returns that pid, or ``None`` when nothing is in that shape.
    """
    record = _read_pid_record(cfg, name)
    if record is None:
        return None
    pid, _token = record
    if pid <= 0 or supervised_pid(cfg, name) is not None:
        return None
    return pid if pid_exists(pid) else None


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
    """Spawn the pod's gateway, record it, wait for it, and return its exit code.

    *gateway_pid_record* is the path of the pod's OWN gateway pid sidecar (the
    file a booting gateway rewrites with its pid and start identity). It is
    handed in rather than re-derived here so the derivation stays in the one
    place that already owns it, ``runtime._pod_pid_record_path``; a second
    spelling of that path would be a way for the reader and the writer to drift.
    It is what lets an in-app restart be ADOPTED rather than misread as a stop —
    see the successor handling at the end of this function.

    **RESIDUAL, stated because it is not observable rather than not considered:**
    an adopted successor was not spawned by this process, so there is no handle
    to read its exit code from, and this task therefore reports the code of the
    gateway it originally spawned. A gateway that crashes AFTER an in-app restart
    is thus recorded as a clean task completion, and the crash shows in the pod's
    logs rather than in ``last_result``. That is no worse than before adoption
    existed (the record was cleared and the task exited 0 either way) and it is
    strictly better on the thing that mattered: the pod stays visible to every
    verb instead of becoming one ``pod down`` would reclaim underneath.

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

    **The resource ceiling is applied here, and this is the only place it can
    be.** systemd caps a pod with ``MemoryMax``/``CPUQuota`` on the unit; a
    scheduled task has no such field, so the Windows equivalent is a Job object,
    which cannot be expressed as an argv prefix and has to be attached to a live
    pid. The ordering below is what makes it airtight rather than merely small:
    the child is created ``CREATE_SUSPENDED`` (it has executed no instructions,
    so it provably has no descendants that could already have escaped the job),
    the ceiling is attached, and only then is it resumed. That is the sequence
    ``platform-compat.md`` names for race-free Job object assignment, and
    :func:`kiro_crew.sandbox.apply_windows_resource_ceiling` reads the same
    ``resource_limits`` config the cgroup path reads, so one operator setting
    governs both platforms.

    A ceiling that could not be installed does NOT fail the boot: that matches
    how an unavailable cgroup scope is handled, and ``apply_job_limits`` has
    already logged it as a SECURITY warning. A failed RESUME is the opposite --
    the child is alive but frozen, so it is terminated rather than left to
    masquerade as a running gateway, which is the policy
    ``acp.client.finish_suspended_spawn`` implements for the same handshake.

    A pid record that cannot be WRITTEN is fatal for the same reason, one step
    later. That record is this platform's only liveness answer, so a gateway
    running without one is a pod every reader calls stopped, and the first
    ``pod down`` deletes its task and its isolated HOME while it serves. The
    child is terminated with a pinned tree kill, no record is left behind, and
    the exit names the path that could not be written.
    """
    proc = subprocess.Popen(  # noqa: S603 - argv is package-derived, never user text
        [str(bin_path), *argv],
        env=env,
        creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED,
        close_fds=False,
    )
    # A process created CREATE_SUSPENDED has executed nothing and will execute
    # nothing until it is resumed, so ANY exception between the spawn and the resume
    # leaks a permanently frozen process that nothing can find: no pid record exists
    # yet, and Task Scheduler reports the task finished. `apply_job_limits` cannot
    # raise (it has a blanket except), but `apply_windows_resource_ceiling` reads the
    # operator's `resource_limits` config OUTSIDE that guard, so its docstring's
    # "never raises" is a promise no code enforces. Make the window exception-safe
    # rather than trusting the promise.
    try:
        apply_windows_resource_ceiling(proc.pid)
        resumed = resume_process_main_thread(proc.pid)
    except BaseException:
        _terminate_unrecorded_child(proc)
        print(
            "FATAL: the pod's gateway was created suspended so its resource ceiling "
            "could be attached without a race, and attaching or resuming it raised, "
            "so it was terminated rather than left frozen with no record of it."
        )
        raise
    if not resumed:
        proc.kill()
        proc.wait()
        print(
            "FATAL: the pod's gateway was created suspended so its resource "
            "ceiling could be attached without a race, but it could not be "
            "resumed, so it was terminated rather than left frozen."
        )
        return EXIT_REFUSED_UNRECOVERABLE
    # Read while the process is provably ours: it has just been resumed and this
    # process holds its handle, so the number cannot yet have been recycled. The
    # successor pre-check below attributes children against this token.
    spawn_token = process_start_time(proc.pid) or ""
    try:
        record_supervised_pid(cfg, name, proc.pid)
    except OSError as exc:
        _terminate_unrecorded_child(proc)
        # Best-effort, and only ever a PARTIAL record: the write above is the one
        # that failed, so anything at that name is a fragment no reader may trust.
        clear_supervised_pid(cfg, name)
        print(
            "FATAL: the pod's gateway started but its pid record could not be "
            f"written at {pid_record_path(cfg, name)} ({exc}). That record is the "
            "only thing that reports this pod alive on Windows, so the gateway was "
            "terminated rather than left running as a pod every verb calls stopped "
            "and `pod down` would reclaim underneath."
        )
        return EXIT_REFUSED_UNRECOVERABLE
    try:
        rc = proc.wait()
        # FIRST statement after the reap, deliberately: the window this marker
        # covers opens the moment the predecessor dies, and liveness is derived
        # from `pid_exists` on the recorded pid, so it has already flipped by the
        # time control returns here. Publishing before the wait would keep the
        # marker present for the pod's whole lifetime and defeat the freshness
        # bound that stops a dead supervisor from wedging teardown. `stop()`
        # settles a missing pid across this one-statement gap and then WAITS for
        # whatever this publication decides, because the reap it is publishing for
        # is very often the one `stop()` itself just caused. The marker names THIS
        # process, so a `/End` that reaps this supervisor before the `finally`
        # retracts it leaves an orphan a reader can recognise instead of waiting out.
        # Liveness and handoff remain separate because one atomic record changes the
        # all-backend on-disk contract.
        protected = _begin_handoff(cfg, name)
        # An in-app restart does NOT end this pod, and on Windows it looks
        # exactly like one ending. `POST /api/restart`, the update path and the
        # stale-assets reload all reach `platform_compat.reexec_python_module`,
        # whose `os.execv` on this platform is CreateProcess plus an exit of the
        # caller — measured here: the predecessor reports exit code 0 (so `rc`
        # cannot tell a restart from a stop), the successor is its CHILD, and the
        # successor outlives the reap. So `proc.wait()` returns for a pod that is
        # still serving, under a new pid.
        #
        # Clearing the record here would be the fail-OPEN direction, and the
        # worst one this backend has: the record is the ONLY thing that reports
        # this pod alive on Windows, so `is_active` would call the pod stopped,
        # `stop` would take neither the survivor snapshot nor the still-alive
        # guard, and the next `pod down` would delete the live successor's task
        # and its isolated HOME — an irreversible `rmtree` under a serving
        # gateway. Adopt the successor instead and keep supervising it; the
        # record is cleared only once no successor remains.
        #
        # The Job object ceiling survives the handover for free: job membership
        # covers a member's descendants, and the successor is the member's child.
        # The anchor ADVANCES with each handover. Both of `_await_successor`'s
        # signals are relative to the process that was just reaped: the sidecar
        # read excludes that pid as a leftover, and the tree check asks whether
        # THAT pid still has a live attributed child. Holding the original
        # gateway's pid across a second restart breaks both — the intermediate is
        # dead, so it has no children to find, and the window then closes
        # instantly with a successor that has not yet rewritten the sidecar still
        # booting. The `finally` would clear the only record that reports this pod
        # alive, and `pod down` would rmtree a serving gateway's HOME. It is a
        # RACE in that shape, not a clean failure: a successor quick enough to
        # claim the sidecar first survives, a slow one loses the pod.
        # The LAUNCHER is not the gateway. `bin_path` can spawn the interpreter that
        # becomes the gateway, and an in-app restart execs inside THAT process, so the
        # successor is the launcher's GRANDCHILD. Anchoring on the launcher makes the
        # successor invisible rather than merely hard to find: per-edge attribution
        # drops a child whose intermediate is dead, together with its whole subtree,
        # and the intermediate here is exactly the process that just exec'd away. The
        # window then closes with nothing found, the record is cleared, and `pod down`
        # rmtrees a serving gateway's HOME.
        #
        # So the anchor is the pid the gateway's OWN sidecar named -- but only when
        # that pid is GONE, which is what makes it the process that was reaped. A
        # sidecar still naming a LIVE pid is naming the successor itself, and
        # `_await_successor`'s sidecar signal returns it directly; anchoring on it
        # would instead exclude it as the leftover.
        reaped_pid, reaped_token = proc.pid, spawn_token
        gateway_named = run_marker.read_pid_record_path(gateway_pid_record)
        if gateway_named is not None:
            gw_pid, gw_token = gateway_named
            if gw_pid != proc.pid and not pid_exists(gw_pid):
                reaped_pid, reaped_token = gw_pid, gw_token
        try:
            # An unprotected window must not be entered. With no marker published,
            # `handoff_in_progress` reports nothing in flight while the record names
            # a pid that is already gone, so a concurrent `pod down` would `/Delete`
            # the task and rmtree the isolated HOME under whatever is booting. The
            # `finally` below ends any successor before the state is dropped, which
            # is the recoverable outcome; serving it unprotected is not.
            if not protected:
                print(
                    "kirocrew-pod: the handoff marker could not be published, so a "
                    "restart cannot be protected from a concurrent teardown; any "
                    "successor is being ended instead. Run `pod up` again."
                )
            while protected and (
                (successor := _await_successor(gateway_pid_record, reaped_pid, reaped_token))
                is not None
            ):
                successor_token = process_start_time(successor)
                if not successor_token:
                    # No identity: either it already exited between the sighting and
                    # this read, or it cannot be opened. Confirm which, because the
                    # two need opposite handling and only one of them is benign.
                    if pid_exists(successor):
                        _terminate_unrecorded_pid(successor)
                        print(
                            "kirocrew-pod: the gateway restarted itself but the successor's "
                            "creation time could not be read, so it could not be tracked; "
                            "it has been ended rather than left serving unrecorded."
                        )
                    break
                try:
                    record_supervised_pid(cfg, name, successor)
                except OSError as exc:
                    # Fail CLOSED. Leaving it serving is the unrecoverable direction:
                    # the record is the only thing that reports this pod alive, so a
                    # later `pod down` would take neither the survivor snapshot nor
                    # the still-alive guard and would rmtree the isolated HOME under a
                    # live writer. Ending it costs the restart and nothing else, and
                    # it is what makes the advice below safe to follow -- `pod down`
                    # on a pod that is genuinely gone is the ordinary cleanup path.
                    _terminate_unrecorded_pid(successor, token=successor_token)
                    print(
                        "kirocrew-pod: the gateway restarted itself but the successor's "
                        f"pid record could not be written at {gateway_pid_record} ({exc}); "
                        "the successor has been ended so it cannot serve unrecorded. Run "
                        "`pod down` to clear the task, then boot again."
                    )
                    break
                # The refresh runs INSIDE the wait, so the marker never ages into
                # staleness while this successor lives and the poll's own detection
                # lag is covered. The re-stamp after this returns then only has to
                # cover the gap that opens for the NEXT handover.
                _wait_for_pid(
                    successor,
                    successor_token,
                    on_poll=lambda: _refresh_handoff_if_stale(cfg, name),
                )
                # RE-STAMP: this successor is now gone and the next gap has just
                # opened, so the marker must date from THIS handover rather than
                # from the first. The freshness bound that stops a dead supervisor
                # wedging teardown would otherwise read the original mtime and
                # report no handoff on every handover after the first — the pod's
                # second in-app restart would then be exactly the fail-open the
                # marker exists to prevent, since `supervised_pid` is None while
                # the record still names the successor that just died.
                if not _begin_handoff(cfg, name):
                    # The gap that just opened cannot be protected either, so stop
                    # adopting: the `finally` ends whatever is live before the state
                    # that would have covered it goes away.
                    print(
                        "kirocrew-pod: the handoff marker could not be re-published for "
                        "this restart, so the window is unprotected; any successor is "
                        "being ended instead. Run `pod up` again."
                    )
                    reaped_pid, reaped_token = successor, successor_token
                    break
                reaped_pid, reaped_token = successor, successor_token
        finally:
            # Before the protection goes away, make sure there is nothing left to
            # protect. The loop also exits when `_await_successor` gives up — a
            # successor that took longer than SUCCESSOR_ADOPT_TIMEOUT_SECS to claim
            # the sidecar is alive but was never adopted — and the `finally` below
            # then clears the record while `_end_handoff` retracts the marker,
            # leaving it serving with NEITHER. That is the same unrecoverable shape
            # as an unrecordable successor, so it gets the same answer: end it, and
            # end it before the state that would have covered it is dropped. The
            # ordering is the whole point; retracting first would reopen the window
            # for exactly as long as the kill takes.
            for orphan in _live_children_of(reaped_pid, reaped_token):
                _terminate_unrecorded_pid(orphan)
                print(
                    "kirocrew-pod: the gateway restarted itself but no successor claimed "
                    f"the pod within {SUCCESSOR_ADOPT_TIMEOUT_SECS:.0f}s, so pid {orphan} "
                    "could not be adopted; it has been ended rather than left serving "
                    "with no record. Run `pod down` to clear the task, then boot again."
                )
            # Retracted on EVERY exit: adopted, terminated, or timed out. A marker
            # left behind would block teardown until it went stale, which is the
            # slow version of the bug this whole handoff exists to avoid.
            _end_handoff(cfg, name)
        return rc
    finally:
        clear_supervised_pid(cfg, name)


def _terminate_unrecorded_pid(pid: int, *, token: str = "") -> None:
    """End an untrackable process TREE, pinned to its creation time.

    The by-pid twin of :func:`_terminate_unrecorded_child`, for a successor this
    process adopted rather than spawned: there is no ``Popen`` to fall back on, so
    the pin is the only safety, and a token that cannot be read means the kill is
    skipped rather than aimed at whatever now holds the number.
    """
    identity = token or process_start_time(pid)
    if not identity:
        return
    with contextlib.suppress(OSError):
        kill_process_tree_pinned(pid, identity, SIGTERM)


def _terminate_unrecorded_child(proc: "subprocess.Popen[bytes]") -> None:
    """End a gateway that started but could not be recorded, tree and all.

    The pinned tree kill first, because the gateway spawns its own children and
    ``Popen.kill`` reaches only the process itself, which would leave exactly the
    grandchildren the Job object ceiling exists to bound. The pin is what keeps a
    recycled pid from being signalled. ``proc.kill`` then covers the case where no
    creation-time token can be read, and the wait reaps whichever call landed.

    Every step is independently suppressed, INCLUDING the tree kill: it is one
    access-denied member away from raising, and an escape there would skip the two
    fallbacks below it and replace the caller's boot-failure handling with an
    ``OSError``. The gateway this is called on has no record, so nothing else will
    ever come back for it -- leaving it running is what the caller is about to
    reclaim a HOME out from under. A best-effort step must not be able to cancel
    the step that covers for it.
    """
    token = process_start_time(proc.pid)
    if token:
        with contextlib.suppress(OSError):
            kill_process_tree_pinned(proc.pid, token, SIGTERM)
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        proc.wait(timeout=10)


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
