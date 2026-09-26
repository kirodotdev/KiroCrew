"""Container entrypoint: order the task, supervise it, drain it on shutdown.

Run as ``python -m container.supervisor``. This is the task's init process. It
does not serve anything itself; it enforces the startup order the contract makes
a correctness requirement and then supervises the children.

The order (``docs/system-specs/modules/aws-control.md``, "Four processes, one task"):

1. Gate the environment (layout, model credential, sandbox) and install the crew
   bundle. Nothing has started.
2. The authority files are restored to completion. This is before the backend on
   purpose: the backend flushes the slot table from its own memory, so a backend
   that starts first persists an empty one over the restored files.
3. The backend starts and ``wait_until_ready`` returns (port answers AND the
   boot secret exists).
4. The front process starts, and then the sidecar, whose first cycle copies what
   the backend has written.

A task with no bucket configured has no durability: steps 2 and the sidecar are
both no-ops, the front says so once at startup, and the task serves turns.

Shutdown drains process groups, not pids (see ``process.py``): a ``kiro-cli``
worker is a two-process tree and signalling only the launcher orphans a child
that finishes its turn. Teardown order is front, then backend, then sidecar: stop
new turns arriving first, then let the backend drain in-flight work and flush to
disk, and the sidecar LAST because its final cycle has to see the bytes that
flush produced. Anything still alive after the backend is gone is an escaped
worker it could not reap, so the teardown sweeps orphaned process groups directly.

Track boundaries: the front ``__main__`` seam is imported by its documented path,
lazily, so this module stays importable and testable and never reimplements the
other track's work.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from .. import common
from ..common import Settings
from ..common.config import BACKEND_DRAIN_SECS, FRONT_DRAIN_SECS, SIDECAR_DRAIN_SECS
from ..sidecar import restore as restore_mod
from ..sidecar.store import S3ObjectStore
from . import backend as backend_mod
from . import bundle as bundle_mod
from .process import ProcessGroup, spawn_process_group

log = logging.getLogger("container.supervisor")

# Drain windows. The backend gets the longest so an in-flight turn can finish.
# Drain windows. The backend gets the longest, and the length is load-bearing:
# a kiro-cli worker spawns with start_new_session (acp/runtime.py:1321), so it
# setsid's into its OWN process group and is NOT in the backend's group. Our
# group SIGKILL therefore cannot reach a worker; only the backend's own SIGTERM
# shutdown reaps it. Too short a drain here would SIGKILL the backend before it
# finishes reaping, orphaning workers that go on to finish their turn. Verified
# confirmed by reading the real source and booting the real backend.
#
# The three windows and their sum live in ``common/config.py``, because the sum is a
# contract this process shares with the task definition the control plane registers:
# the task's stop timeout has to cover it or the platform SIGKILLs this process
# mid-drain, and a number duplicated in two subsystems is one that drifts.
# How many discover-kill rounds the orphan sweep makes at teardown. Each round
# reaps a layer, and a killed process's own children reparent to PID 1 and surface in the
# NEXT round, so more than one is required to reach a worker's grandchildren. Bounded so a
# process respawning children cannot spin the teardown forever; a torn-down container has no
# legitimate reason to rebuild its tree faster than this drains it.
_TEARDOWN_SWEEP_ROUNDS: int = 8


def _start_front(settings: Settings) -> ProcessGroup:
    """Launch Track S1's front process (its documented ``__main__``)."""
    return spawn_process_group("front", [sys.executable, "-m", "container.front"])


#: The shutdown reason a spent lifetime produces.
_LIFETIME_REASON: str = "lifetime"


def _start_sidecar(settings: Settings) -> ProcessGroup | None:
    """Launch the backup process, or ``None`` when there is nowhere to write.

    A crew with no bucket has no durability, which the front already says once at
    startup. Starting a writer with no destination would be worse than not starting one:
    a process that runs and writes nothing looks exactly like a working backup.
    """
    if not settings.backup_bucket:
        log.warning(
            "sidecar: no bucket configured, so this task's state is not backed up and "
            "does not survive replacement. Set SMC_BACKUP_BUCKET to make it durable."
        )
        return None
    child = spawn_process_group("sidecar", [sys.executable, "-m", "container.sidecar"])
    log.info("sidecar: started")
    return child


def restore_authority(settings: Settings) -> None:
    """Bring the authority files back before the backend can flush over them.

    Called from :func:`run` at the point where "before the backend starts" is enforced.
    A failure RAISES, so the task does not start: booting without the slot table lets
    the backend persist an empty one, and then the transcripts are still in the bucket
    while the conversation list is gone.

    With no bucket there is nothing to restore and this is a no-op, which is the same
    call :func:`_start_sidecar` makes about the writer.
    """
    if not settings.backup_bucket:
        return
    result = restore_mod.restore_authority(settings, S3ObjectStore(settings.backup_bucket))
    log.info("restore: %s", result.summary())


#: Shutdown reasons that mean the task did what was asked of it, so the process
#: exits zero. Both members are produced by ``_wait_for_shutdown`` a few lines
#: below, and a reason added there without being decided here reports a clean stop
#: as a failure -- which is why the two live next to each other. Everything else,
#: including a reason this code cannot account for, is a failure: see ``run``.
_ORDERLY_REASONS: frozenset[str] = frozenset({"signal", _LIFETIME_REASON})


def _wait_for_shutdown(children: Sequence[ProcessGroup], *, ttl_seconds: int = 0) -> str:
    """Block until a stop signal arrives, a child exits, or the lifetime is spent.

    Returns ``"signal"`` on SIGTERM/SIGINT, ``"lifetime"`` when *ttl_seconds* has
    passed, or ``"<name> exited"`` if a child dies first (the backend dying is
    fatal; so is either other child, since the task cannot do its job).

    ``ttl_seconds`` of zero is UNBOUNDED, which is what a launch path saying
    nothing about lifetime gets: the wait then ends only on a signal or a child.

    The deadline is measured from here on the monotonic clock, so a wall-clock
    correction inside the task cannot cut the lifetime short or extend it. Here
    rather than at process start because this is the point from which the task is
    doing its job; the launch-time sweep measures the same bound from the task's
    own ``startedAt``, which is EARLIER, so where both enforcement points exist
    the sweep is the one that fires. That ordering is the intended one: this
    deadline is the backstop for a cluster no further launch ever sweeps.

    Elapsed time is compared against *ttl_seconds*, which is never added to the
    clock: an integer bound larger than any representable float would raise on
    that addition, and a bound nobody can reach must read as a long lifetime
    rather than as a crash. The sweep compares the same way.
    """
    stop = threading.Event()
    reason = {"why": ""}

    def _on_signal(signum, _frame):
        reason["why"] = "signal"
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    started = time.monotonic()
    bounded = ttl_seconds > 0
    while not stop.wait(0.5):
        for child in children:
            if child.poll() is not None:
                reason["why"] = f"{child.name} exited (code {child.returncode()})"
                return reason["why"]
        if bounded and time.monotonic() - started >= ttl_seconds:
            return _LIFETIME_REASON
    return reason["why"]


def _our_live_children(exclude: set[int]) -> list[int]:
    """Pids whose parent is this process, minus *exclude*.

    The supervisor is PID 1 in this image (``CMD ["python", "-m", "container.supervisor"]``),
    so a process orphaned inside the container is reparented to it. That is what makes an
    ESCAPED worker findable at all: a kiro-cli worker calls ``start_new_session``, so it is in
    its own process group and no ``killpg`` of the backend's group can reach it -- but when the
    backend dies, the worker becomes our child.

    Read from ``/proc`` rather than tracked, because the supervisor never learns the pid: the
    backend spawns its workers and tells nobody. Linux-only, which ``crew/runtime/**`` already
    is; a missing ``/proc`` yields an empty list rather than an error, so a host without it
    degrades to the previous behaviour instead of failing the shutdown.
    """
    mine = os.getpid()
    found: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == mine or pid in exclude:
            continue
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
        except OSError:
            # Unlike the test suite's liveness helper, "could not determine" may drop the
            # candidate here: an unreadable stat gives no ppid to attribute, this scan sees
            # every pid on the host (not just our own children), and the common cause is the
            # pid exiting mid-scan. Failing the whole teardown over one alien pid would be
            # worse than missing it.
            continue
        # After the ')' closing comm: state, ppid. Split this way because comm can contain
        # spaces and parentheses, which is why the naive field index is wrong.
        if len(fields) < 2:
            continue
        if fields[0] == "Z":
            # Already dead and waiting to be reaped; the wait below collects it.
            continue
        try:
            if int(fields[1]) == mine:
                found.append(pid)
        except ValueError:
            continue
    return found


def _sweep_orphans_the_backend_cannot_reap(exclude: set[int]) -> None:
    """SIGKILL any of our children left after the backend was drained.

    Run at ONE point: after ``backend.terminate``. What makes it safe there is that the
    backend is already gone, so a process still running is one whose reaper is dead --
    nothing is going to finish its turn or flush its state, and it is left writing to the
    container filesystem after the task is meant to be gone.

    Deliberately NOT a general "kill workers on shutdown". A worker that escaped the group is
    reaped by the backend's own SIGTERM handler, and ``BACKEND_DRAIN_SECS`` is sized for that
    (see the constant): killing one during the drain is exactly what the long drain exists to
    prevent. This runs after the drain has already ended, one way or the other.
    """
    orphans = _our_live_children(exclude)
    if not orphans:
        return
    log.warning(
        "teardown: %d process(es) outlived the backend and cannot be reaped by it (%s). "
        "Killing them so nothing keeps writing to the data home after the task is "
        "supposed to be gone.",
        len(orphans),
        ", ".join(str(p) for p in orphans),
    )
    # Repeat discovery-and-kill until no live child remains, bounded. A single pass is not
    # enough: an orphan's OWN children reparent to the supervisor (PID 1) only when the
    # orphan dies, so a grandchild becomes findable in the NEXT scan, not this one. Killing
    # once and walking away leaves that grandchild still writing to the data home after the
    # task is torn down. Each round also signals the process GROUP, because a kiro-cli worker
    # start_new_session()s into its own group (the spec's "Shutdown" section documents that it escapes a killpg
    # of the backend's group), so killpg of the worker's OWN pgid takes its subtree in one
    # signal rather than one pid at a time. The round cap bounds the loop against a process
    # that respawns children faster than we can reap them; it is a container being torn down,
    # so a few rounds is generous.
    for _ in range(_TEARDOWN_SWEEP_ROUNDS):
        live = _our_live_children(exclude)
        if not live:
            break
        for pid in live:
            # Group first: reaches the worker's whole session in one signal. A pid whose
            # group cannot be resolved (already gone) falls back to a direct kill.
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        # Reap what just died so the next _our_live_children scan does not re-list zombies as
        # live and so no zombie is left for the platform to report. Bounded per round.
        for _ in range(len(live) + 1):
            try:
                if os.waitpid(-1, os.WNOHANG) == (0, 0):
                    break
            except ChildProcessError:
                break


def _teardown(
    front: ProcessGroup,
    backend: ProcessGroup,
    sidecar: ProcessGroup | None = None,
) -> int | None:
    """Drain the children in order: front, backend, sidecar, then sweep orphans.

    The backend gets the longer drain so an in-flight turn can finish. The sidecar goes
    LAST and not first, because its final cycle is what makes an orderly replacement
    lossless: the front stops new turns arriving, the backend flushes the turns it holds,
    and only then does the writer get to upload what that flush produced.

    Once the backend is gone, anything of ours still running is a process it could not
    reap -- an escaped worker in its own process group, which no group signal reached --
    so we discover and kill those directly.

    Returns the SIDECAR's exit status, because that status is the only evidence that the
    final cycle actually committed: the writer exits non-zero when its post-shutdown
    cycle is incomplete, and is killed with a negative status when the drain window
    elapses mid-upload. ``None`` means there was no sidecar, or that its status could not
    be read; the caller decides what each of those is worth. The front's and backend's
    statuses are not returned: they are draining on our own signal, so a non-zero status
    there is the signal, not a fault.
    """
    log.info("draining front (%.0fs)", FRONT_DRAIN_SECS)
    front.terminate(FRONT_DRAIN_SECS)
    log.info("draining backend (%.0fs)", BACKEND_DRAIN_SECS)
    backend.terminate(BACKEND_DRAIN_SECS)
    known = {front.pid, backend.pid}
    sidecar_status: int | None = None
    if sidecar is not None:
        log.info("draining sidecar (%.0fs)", SIDECAR_DRAIN_SECS)
        sidecar_status = sidecar.terminate(SIDECAR_DRAIN_SECS)
        known.add(sidecar.pid)
    # The backend is gone. Anything of ours still running is a process it cannot reap --
    # an escaped worker in its own process group, which no group signal could reach.
    _sweep_orphans_the_backend_cannot_reap(known)
    return sidecar_status


def verify_layout(settings: Settings) -> None:
    """Refuse to start if the SMC paths disagree with what Kiro Crew resolves.

    Kiro Crew keeps its whole data home under ONE root: ``config_dir()`` equals
    the data home equals ``KIROCREW_HOME``, and it writes ``sessions/``,
    ``open_slots.json``, ``session_map.json`` and ``run/gateway-<port>.secret``
    directly under that root (chat_persistence.py:322, run_marker.py, verified by
    booting the real gateway). The backend is launched with
    ``KIROCREW_HOME=settings.data_home``, so the backend's own ``config_dir()``
    IS ``settings.data_home``. Two path settings must therefore agree, or the
    deployment comes up looking healthy and loses state silently:

    * ``settings.config_dir`` must equal ``settings.data_home``. Kiro Crew writes
      ``open_slots.json`` and ``session_map.json`` at the data-home root; if
      ``config_dir`` is a ``/config`` subdir the backend never writes to, the
      deployment comes up looking healthy while the authoritative files are
      nowhere the rest of the system reads them -- the exact section9.1 failure.
    * ``settings.backend_run_dir`` must be ``settings.data_home / "run"``, or
      ``wait_until_ready`` polls a secret path the backend did not write.

    This is the "verify rather than trust" the Dockerfile open item calls for.
    It is checked before anything starts so a path mistake fails at deploy
    rather than as missing conversations later.
    """
    problems = []
    if settings.config_dir != settings.data_home:
        problems.append(
            f"SMC_CONFIG_DIR ({settings.config_dir}) must equal SMC_DATA_HOME "
            f"({settings.data_home}): Kiro Crew writes open_slots.json and "
            f"session_map.json at the data-home root, not a /config subdir."
        )
    expected_run = settings.data_home / "run"
    if settings.backend_run_dir != expected_run:
        problems.append(
            f"SMC_BACKEND_RUN_DIR ({settings.backend_run_dir}) must be "
            f"{expected_run}: the backend writes its per-boot secret under "
            f"<data home>/run."
        )
    # --approval yolo is REFUSED unless KIROCREW_HOME is an isolated,
    # non-default home (cli.py:498-533). data_home IS KIROCREW_HOME, so reject a
    # default/legacy home here -- otherwise the backend would exit rc=2 on the
    # yolo rail, which reads as a boot failure. This also enforces R1 (one
    # gateway per data home; never the live home).
    protected = set()
    for p in (Path("~/.kiro/crew").expanduser(), Path("~/.kirocrew").expanduser()):
        try:
            protected.add(p.resolve())
        except OSError:
            protected.add(p)
    try:
        home_resolved = settings.data_home.resolve()
    except OSError:
        home_resolved = settings.data_home
    if home_resolved in protected:
        problems.append(
            f"SMC_DATA_HOME ({settings.data_home}) resolves to a default/live "
            f"Kiro Crew home; --approval yolo is refused there and it would "
            f"collide with the real gateway (R1). Use an isolated data home."
        )
    if problems:
        raise common.ConfigError(
            "Container path layout disagrees with Kiro Crew's resolved paths; "
            "refusing to start rather than silently lose state:\n  - " + "\n  - ".join(problems)
        )


#: The three things the sandbox probe can conclude. A verdict is a string rather
#: than a tri-state boolean because the interesting case carries information: an
#: undetermined verdict names WHY it could not be settled, and an operator needs
#: that to act. ``SANDBOX_UNDETERMINED_PREFIX`` is the prefix every such verdict
#: carries.
SANDBOX_AVAILABLE = "available"
SANDBOX_DENIED = "denied"
SANDBOX_UNDETERMINED_PREFIX = "undetermined: "


def _user_namespaces_available() -> str:
    """Probe whether this host permits an unprivileged user namespace.

    Returns one of :data:`SANDBOX_AVAILABLE`, :data:`SANDBOX_DENIED`, or an
    ``undetermined: <why>`` verdict. The probe runs in a forked child because
    ``unshare`` mutates the caller's namespaces.

    Undetermined is a real outcome and is reported as one, not folded into either
    answer. It happens when the platform has no ``os.unshare``, when the fork
    itself fails, or when the child neither succeeds nor reports a clean denial --
    and the caller refuses on it, so the honest thing is to say which of those it
    was rather than to pick a side on the host's behalf.
    """
    if not (hasattr(os, "unshare") and hasattr(os, "CLONE_NEWUSER")):
        return (
            f"{SANDBOX_UNDETERMINED_PREFIX}this platform has no os.unshare/os.CLONE_NEWUSER "
            f"(sys.platform is {sys.platform!r}), so whether a user namespace could be "
            "created cannot be tested here"
        )
    try:
        pid = os.fork()
    except OSError as exc:  # pragma: no cover - fork refused by the host
        return f"{SANDBOX_UNDETERMINED_PREFIX}the probe could not fork a child ({exc})"
    if pid == 0:
        try:
            os.unshare(os.CLONE_NEWUSER)  # type: ignore[attr-defined]
            os._exit(0)
        except OSError:
            os._exit(1)
        except Exception:
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        code = os.WEXITSTATUS(status)
        if code == 0:
            return SANDBOX_AVAILABLE
        if code == 1:
            return SANDBOX_DENIED
        return (
            f"{SANDBOX_UNDETERMINED_PREFIX}the probe child failed for a reason that is "
            f"neither success nor a kernel refusal (exit code {code})"
        )
    if os.WIFSIGNALED(status):  # pragma: no cover - requires killing the probe child
        return (
            f"{SANDBOX_UNDETERMINED_PREFIX}the probe child was killed by signal "
            f"{os.WTERMSIG(status)} before it could answer"
        )
    return (  # pragma: no cover - waitpid reporting neither exit nor signal
        f"{SANDBOX_UNDETERMINED_PREFIX}the probe child reported neither an exit code nor "
        f"a signal (raw wait status {status})"
    )


#: ``prctl`` option number for the dumpable flag (``linux/prctl.h``). Value 0 clears it.
PR_SET_DUMPABLE: int = 4


def _clear_dumpable() -> str:
    """Clear this process's dumpable flag. Returns "" on success, else a reason.

    A string rather than a bool so the caller can report WHY, and a reason rather than
    an exception so a platform without ``prctl`` is distinguishable from a ``prctl``
    that ran and refused.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError as err:
        # Not Linux, or a libc under another name. The image is Linux; this path exists
        # so importing this module on a developer's or CI runner's other platform does
        # not fail.
        return f"libc.so.6 not loadable ({err})"
    if not hasattr(libc, "prctl"):
        return "libc has no prctl"
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        return f"prctl(PR_SET_DUMPABLE, 0) failed with errno {ctypes.get_errno()}"
    return ""


def make_non_dumpable(*, clear=_clear_dumpable) -> None:
    """Make this process's ``/proc`` entries unreadable by other processes of its uid.

    The credential reaches this process in its environment, and `/proc/<pid>/environ`
    exposes the region the exec set up rather than the live ``environ`` array -- so
    clearing the variable does not unpublish the value. The model worker runs under this
    same uid with no PID namespace between them, so it can read this process's
    environment directly, and it runs an auto-approved shell on untrusted prompt
    content. Clearing the dumpable flag makes the kernel reparent this process's
    ``/proc`` entries to root, and a same-uid reader then gets ``EACCES``.

    Side effects, and why they are acceptable here: a non-dumpable process cannot be
    ``ptrace``d and produces no core dump. The supervisor needs neither -- it spawns and
    drains children and reads no ``/proc`` entry of its own.

    On Linux a ``prctl`` that RAN and refused is fatal: the alternative is to serve
    turns with the credential published to the worker. A platform with no ``prctl`` at
    all is a different case and only logs, because this module is imported by tests on
    runners that are not the image.
    """
    reason = clear()
    if not reason:
        log.info("supervisor is non-dumpable: its /proc entries are root-owned")
        return
    if sys.platform.startswith("linux"):
        raise common.ConfigError(
            f"could not make the supervisor non-dumpable: {reason}. This process holds "
            "the model identity in its environment, and the model worker runs under the "
            "same uid with no PID namespace, so without this its /proc entries are "
            "readable by a worker that auto-approves every tool it calls on untrusted "
            "prompt content. Refusing to start."
        )
    log.warning("not making this process non-dumpable (%s): %s", sys.platform, reason)


def verify_sandbox(
    settings: Settings, *, env: Mapping[str, str], probe=_user_namespaces_available
) -> None:
    """Refuse to start unless the model subprocess can run sandboxed.

    kiro-cli runs the model subprocess inside a sandbox. On Linux that needs an
    unprivileged user namespace; without one, ``wrap_argv`` fails CLOSED. This
    container is sandboxed-only, so a host that cannot provide one is refused here,
    loudly, rather than left to fail every turn.

    **Why taking the credential out of the worker's environment does not earn an
    unsandboxed posture.** The worker auto-approves every tool it calls on untrusted
    prompt content, so what matters is whether it can REACH a credential -- not whether
    one is resident in its own environment. ``build_backend_env`` closes the
    environment route, and this function asserts that below. The route it cannot close
    is the vault: the backend answers the engine's token request from it, so the
    backend's uid must be able to decrypt it, and the worker is a child of the backend
    under that same uid. Measured -- a uid-1000 process reads and decrypts that vault
    directly. An auto-approved unsandboxed worker therefore still has a route, which is
    why ``sandbox_allow_unsandboxed_exec`` stays false and why this refusal has no
    credential-shaped escape hatch.

    Closing the remaining route is not something this file can do: it needs a user
    namespace, or a worker under a different uid from the BACKEND (the gateway's own
    spawn path), or a credential not worth stealing.

    **Only ``SANDBOX_AVAILABLE`` proceeds.** Undetermined refuses, and so does any
    verdict this function does not recognise. Reading a probe that cannot reach an
    answer as permission to continue is the same defect as reading the environment
    through a denylist: it holds for the hosts someone already thought of and fails
    open on the next one. The refusal repeats the verdict verbatim so an operator
    learns what could not be determined rather than only that something could not be.
    """
    # The environment route, asserted rather than decided -- and checked before the
    # probe, because it is broken whatever the host can provide. `build_backend_env`
    # withholds both credential shapes, so a value here means that withholding was
    # removed or defeated, which is a broken invariant and not a host posture. It is
    # deliberately NOT a decision: the code that builds `env` is the code that empties
    # it, so a posture taken from this reading could only ever confirm itself.
    leaked = sorted(
        name
        for name in (backend_mod.ENV_KIRO_IDENTITY, backend_mod.ENV_KIRO_API_KEY)
        if (env.get(name) or "").strip()
    )
    if leaked:
        raise common.ConfigError(
            f"the environment prepared for the backend carries {', '.join(leaked)}. "
            "build_backend_env withholds the model credential in both of its shapes, "
            "so a value here means that withholding was removed or defeated. The "
            "backend spawns the model worker, which auto-approves every tool it calls "
            "on untrusted prompt content. Refusing to start."
        )
    verdict = probe()
    if verdict == SANDBOX_AVAILABLE:
        return
    if verdict == SANDBOX_DENIED:
        raise common.ConfigError(
            "No user-namespace sandbox is available on this host, so kiro-cli cannot "
            "spawn the model subprocess sandboxed. This container runs sandboxed-only. "
            "Taking the model credential out of the worker's environment is not enough "
            "to offer an unsandboxed posture instead: the backend answers the engine's "
            "token request from the crew's vault, so the backend's uid must be able to "
            "decrypt it, and the worker runs as a child of the backend under that same "
            "uid. Run where unprivileged user namespaces are permitted."
        )
    raise common.ConfigError(
        f"Whether this host permits an unprivileged user-namespace sandbox could not be "
        f"determined: {verdict}. This container runs sandboxed-only, so an undetermined "
        "answer refuses exactly as a denial does: continuing would run a model "
        "subprocess that auto-approves every tool, with no evidence that a sandbox is "
        "in place. Run this image on Linux where unprivileged user namespaces are "
        "permitted, and fix what stopped the probe rather than reading its silence as "
        "consent."
    )


def run(settings: Settings, *, wait_for_shutdown=None) -> int:
    """Order, supervise and drain the task. Return a process exit code.

    The code is 0 only when BOTH halves of an orderly stop held: the task was asked to
    go rather than losing a child, and the writer's final backup cycle committed. The
    second half matters because that cycle runs after the backend's flush and is the
    only copy of the turns in it, so a task that exits 0 having failed it reports a
    lossless replacement for a lossy one.

    ``wait_for_shutdown`` is injected so tests can drive the supervise phase
    without signals or real processes. It takes the watched children and returns a
    reason; the task's lifetime is bound onto the default here, where the settings
    are, so an injected stub keeps the one-argument shape and a test that is not
    about the lifetime does not have to say anything about it.
    """
    if wait_for_shutdown is None:
        wait_for_shutdown = functools.partial(
            _wait_for_shutdown, ttl_seconds=settings.task_ttl_seconds
        )
    # 0. Fail loudly, before anything starts, if the environment cannot run a
    #    turn: bad path layout, no model identity, a sandbox absent where one is
    #    required, or a bundle that is absent or names a different crew.
    verify_layout(settings)
    env = backend_mod.build_backend_env(settings)
    # The identity is delivered in the SUPERVISOR's environment and moved into the
    # vault here, which is where the backend's auth callback reads it.
    #
    # A seed that did not store anything is FATAL, not a return value to discard. The
    # delivered identity is what makes this task this account, so "nothing was
    # delivered" must not fall through to the vault check: that check reads
    # `TokenStore.resolve`, which would accept a slot left behind by a prior task on
    # this persistent volume and start the task authenticated as the previous account,
    # silently. A blank or whitespace secret is exactly that case.
    if not backend_mod.seed_model_identity(settings):
        raise common.ConfigError(
            f"no model identity was delivered: {backend_mod.ENV_KIRO_IDENTITY} is unset "
            "or blank. The task injects one from Secrets Manager. Refusing to start "
            "rather than continuing on whatever identity this data home already holds, "
            "which would authenticate the task as another account without saying so."
        )
    backend_mod.require_model_identity(settings)
    # Now drop BOTH credential shapes from this process's own environment. The front is
    # spawned with no env argument and so inherits this one whole, and
    # `build_backend_env` only ever cleaned the COPY handed to the backend -- so without
    # this a credential reaches a second long-lived process for no reason. The front's
    # own exec resets the dumpable flag cleared below, so its `/proc` entry is readable
    # by a same-uid worker whatever this process does about its own.
    #
    # `ENV_KIRO_API_KEY` is popped even though nothing is supposed to deliver it. The
    # secrets path derives each destination variable from its secret's name with no
    # allowlist refusing this one, so an operator CAN provision it, and the container's
    # posture is not to rely on the absence of a delivery path. Both names, because
    # covering one and leaving its sibling is how the same route stays open beside the
    # fix.
    for name in (backend_mod.ENV_KIRO_IDENTITY, backend_mod.ENV_KIRO_API_KEY):
        os.environ.pop(name, None)
    # And make THIS process unreadable through procfs before anything is spawned.
    #
    # Clearing the variable above does not remove it from `/proc/<pid>/environ`, which
    # exposes the exec-time region rather than the live `environ` array -- measured, not
    # assumed. The model worker runs as a child of the backend under this same uid with
    # no PID namespace between them, and it auto-approves every tool it calls on
    # untrusted prompt content, so it could read this process's environment directly.
    # `PR_SET_DUMPABLE=0` makes the kernel reparent this process's `/proc` entries to
    # root, so a same-uid reader gets EACCES. Before the spawn, because after it the
    # window is already open.
    make_non_dumpable()
    verify_sandbox(settings, env=env)
    # Install the crew into the paths Kiro Crew reads BEFORE the backend starts,
    # so "it started" means "the named crew is installed" rather than a default
    # agent. Refuses closed on any mismatch (see bundle.install_bundle).
    bundle_mod.install_bundle(settings)
    # Then the container's own configuration, which must land after the bundle (a
    # bundle may ship config, and this has to win on the keys it sets) and before the
    # backend, which reads this file at boot: a transport it starts there is already
    # connected by the time anything else could object.
    backend_mod.write_backend_config(settings)

    # 1. Restore, then the backend. Nothing has started yet, and the ORDER is the
    #    correctness rule rather than an optimisation: the backend flushes the slot table
    #    from its own memory, so a backend that starts first persists an empty one over
    #    the restored files and the conversation list comes up blank with nothing to say
    #    so. Transcripts are not restored here -- the front fetches the one a turn
    #    continues, on that turn.
    restore_authority(settings)
    backend = backend_mod.start_backend(settings, env=env)
    try:
        backend_mod.wait_until_ready(
            settings, backend_mod.DEFAULT_READY_TIMEOUT_SECS, process=backend
        )
    except Exception:
        # Readiness failed or the backend exited: tear the backend down and
        # abort. The front was never started.
        log.error("backend did not become ready; aborting")
        backend.terminate(BACKEND_DRAIN_SECS)
        raise
    log.info("backend: ready on %s", settings.backend_base_url)

    # 2. Front, then the sidecar. The sidecar is started last because its first cycle
    #    reads what the backend has written, and it is absent entirely when no bucket is
    #    configured: that is a crew running without durability, not a fault.
    front = _start_front(settings)
    log.info("front: started")
    sidecar = _start_sidecar(settings)

    watched = [child for child in (backend, front, sidecar) if child is not None]
    sidecar_status: int | None = None
    try:
        why = wait_for_shutdown(watched)
        log.info("shutdown: %s", why)
    finally:
        sidecar_status = _teardown(front, backend, sidecar)
    # The exit code has to distinguish the two reasons, because it is the only one
    # the platform reads. `_wait_for_shutdown` returns "signal" for an orderly stop
    # (ECS asked the task to go) and "<name> exited (code N)" when a child died
    # first -- and its own docstring calls the backend dying fatal. Returning 0 for
    # both told ECS a crash loop was a clean shutdown, so the console showed a task
    # exiting normally over and over with nothing marked failed.
    #
    # A spent lifetime joins "signal" as a success: the task ran for as long as it
    # was allowed and then stood down, which is the bound working rather than
    # anything going wrong. Reporting it as a failure would leave an operator
    # reading every expiry as an incident.
    #
    # Anything outside `_ORDERLY_REASONS`, including an empty reason, is reported as
    # a failure: a reason this code cannot account for is not evidence that things
    # went well.
    if why not in _ORDERLY_REASONS:
        log.error("exiting non-zero: %s", why or "shutdown reason unknown")
        return 1
    # An orderly stop is only a SUCCESSFUL stop if the writer's final cycle committed.
    # That cycle runs after the backend's flush and carries the turns nothing else has
    # copied, so its failure -- a refused upload, or a kill when the drain window
    # elapses mid-upload -- is state this task produced and lost. Reporting 0 for it
    # would hand the platform a clean shutdown for a lossy one, which is the same
    # mistake as reporting 0 for a crash loop. This applies to a spent lifetime as much
    # as to a signal: both stop a task that was serving turns a moment earlier.
    if sidecar is not None and sidecar_status != 0:
        log.error(
            "exiting non-zero: the sidecar's final backup cycle did not complete "
            "(status %s), so state written after the backend's flush is not in the "
            "bucket",
            "unknown" if sidecar_status is None else sidecar_status,
        )
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    settings = common.load()
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
