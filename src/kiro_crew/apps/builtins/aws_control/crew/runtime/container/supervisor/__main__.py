"""Container entrypoint: order the task, supervise it, drain it on shutdown.

Run as ``python -m container.supervisor``. This is the task's init process. It
does not serve anything itself; it enforces the startup order the contract makes
a correctness requirement and then supervises the children.

The order (``docs/system-specs/modules/aws-control.md``, "Three processes, one task"):

1. Gate the environment (layout, model credential, sandbox) and install the crew
   bundle. Nothing has started.
2. The backend starts and ``wait_until_ready`` returns (port answers AND the
   boot secret exists).
3. The front process starts.

There is no restore phase and no sidecar: the backup subsystem was extracted from
this PR (durability is tracked separately). When it returns it must reinstate the
rule that made it correct -- restore to completion before the backend starts, or
the backend's periodic flush persists an empty slot table over the gap.

Shutdown drains process groups, not pids (see ``process.py``): a ``kiro-cli``
worker is a two-process tree and signalling only the launcher orphans a child
that finishes its turn. Teardown order is front, then backend: stop new turns
arriving first, then let the backend drain in-flight work and flush to disk.
Anything still alive after the backend is gone is an escaped worker it could not
reap, so the teardown sweeps orphaned process groups directly.

Track boundaries: the front ``__main__`` seam is imported by its documented path,
lazily, so this module stays importable and testable and never reimplements the
other track's work.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from .. import common
from ..common import Settings
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
FRONT_DRAIN_SECS: float = 5.0
BACKEND_DRAIN_SECS: float = 25.0
# How many discover-kill rounds the orphan sweep makes at teardown. Each round
# reaps a layer, and a killed process's own children reparent to PID 1 and surface in the
# NEXT round, so more than one is required to reach a worker's grandchildren. Bounded so a
# process respawning children cannot spin the teardown forever; a torn-down container has no
# legitimate reason to rebuild its tree faster than this drains it.
_TEARDOWN_SWEEP_ROUNDS: int = 8


def _start_front(settings: Settings) -> ProcessGroup:
    """Launch Track S1's front process (its documented ``__main__``)."""
    return spawn_process_group("front", [sys.executable, "-m", "container.front"])


def _wait_for_shutdown(children: Sequence[ProcessGroup]) -> str:
    """Block until a stop signal arrives or any child exits. Return the reason.

    Returns ``"signal"`` on SIGTERM/SIGINT, or ``"<name> exited"`` if a child
    dies first (the backend dying is fatal; so is either other child, since the
    task cannot do its job).
    """
    stop = threading.Event()
    reason = {"why": ""}

    def _on_signal(signum, _frame):
        reason["why"] = "signal"
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    while not stop.wait(0.5):
        for child in children:
            if child.poll() is not None:
                reason["why"] = f"{child.name} exited (code {child.returncode()})"
                return reason["why"]
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
) -> None:
    """Drain the children in order: front, then backend, then sweep orphans.

    The backend gets the longer drain so an in-flight turn can finish. Once it is gone,
    anything of ours still running is a process it could not reap -- an escaped worker in its
    own process group, which no group signal reached -- so we discover and kill those directly.
    """
    log.info("draining front (%.0fs)", FRONT_DRAIN_SECS)
    front.terminate(FRONT_DRAIN_SECS)
    log.info("draining backend (%.0fs)", BACKEND_DRAIN_SECS)
    backend.terminate(BACKEND_DRAIN_SECS)
    # The backend is gone. Anything of ours still running is a process it cannot reap --
    # an escaped worker in its own process group, which no group signal could reach.
    known = {front.pid, backend.pid}
    _sweep_orphans_the_backend_cannot_reap(known)


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


def verify_sandbox(settings: Settings, *, probe=_user_namespaces_available) -> None:
    """Refuse to start unless the model subprocess can run sandboxed.

    kiro-cli runs the model subprocess inside a sandbox. On Linux that needs an
    unprivileged user namespace; without one, ``wrap_argv`` fails CLOSED
    (sections.py:636). This container ships SANDBOXED-ONLY: if the host has no
    user namespace we refuse to start rather than run the model subprocess
    without one.

    There is deliberately no opt-in to run unsandboxed. kiro-cli auto-approves
    every tool (``--approval yolo``, nothing in the container clicks Approve),
    and the backend's environment carries the model credential ``KIRO_API_KEY``,
    which kiro-cli re-injects into the worker. An unsandboxed worker would then
    run an auto-approved shell driven by untrusted customer prompt content with
    that credential readable in its environment -- a prompt-injection-to-
    credential-exfiltration path the ECS boundary does not close, because the
    attacker is the prompt content, already inside the boundary. Offering that
    safely needs the credential brokered out of the worker's environment, which
    is not part of this change; until then the only posture this container
    accepts is sandboxed. On a host without user namespaces (Fargate today) it
    refuses to start, loudly, rather than boot into the exposed posture.

    **Only ``SANDBOX_AVAILABLE`` proceeds.** Undetermined refuses, and so does any
    verdict this function does not recognise. Reading a probe that cannot reach an
    answer as permission to continue is the same defect as reading the environment
    through a denylist: it holds for the hosts someone already thought of and fails
    open on the next one, and here failing open means an auto-approving worker
    holding the model credential with no sandbox. The refusal repeats the verdict
    verbatim so an operator learns what could not be determined rather than only
    that something could not be.
    """
    verdict = probe()
    if verdict == SANDBOX_AVAILABLE:
        return
    if verdict == SANDBOX_DENIED:
        raise common.ConfigError(
            "No user-namespace sandbox is available on this host, so kiro-cli cannot "
            "spawn the model subprocess sandboxed. This container runs sandboxed-only "
            "and does not offer an unsandboxed posture, so it refuses to start rather "
            "than run the model subprocess -- which auto-approves every tool and holds "
            "the model credential in its environment -- without a sandbox. Run where "
            "unprivileged user namespaces are permitted."
        )
    raise common.ConfigError(
        f"Whether this host permits an unprivileged user-namespace sandbox could not be "
        f"determined: {verdict}. This container runs sandboxed-only, so an undetermined "
        "answer refuses exactly as a denial does: continuing would run the model "
        "subprocess -- which auto-approves every tool and holds the model credential in "
        "its environment -- with no evidence that a sandbox is in place. Run this image "
        "on Linux where unprivileged user namespaces are permitted, and fix what "
        "stopped the probe rather than reading its silence as consent."
    )


def run(settings: Settings, *, wait_for_shutdown=_wait_for_shutdown) -> int:
    """Order, supervise and drain the task. Return a process exit code.

    ``wait_for_shutdown`` is injected so tests can drive the supervise phase
    without signals or real processes.
    """
    # 0. Fail loudly, before anything starts, if the environment cannot run a
    #    turn: bad path layout, no model credential, an unspawnable sandbox, or a
    #    bundle that is absent or names a different crew.
    verify_layout(settings)
    env = backend_mod.build_backend_env(settings)
    backend_mod.require_api_key(env)
    verify_sandbox(settings)
    # Install the crew into the paths Kiro Crew reads BEFORE the backend starts,
    # so "it started" means "the named crew is installed" rather than a default
    # agent. Refuses closed on any mismatch (see bundle.install_bundle).
    bundle_mod.install_bundle(settings)
    # Then the container's own configuration, which must land after the bundle (a
    # bundle may ship config, and this has to win on the keys it sets) and before the
    # backend, which reads this file at boot: a transport it starts there is already
    # connected by the time anything else could object.
    backend_mod.write_backend_config(settings)

    # 1. Backend, then readiness. Nothing else has started yet.
    #
    # There is no restore phase: the backup subsystem was extracted from this PR (its
    # durability design is tracked separately), so the container boots straight into the
    # backend on a fresh data home. Cross-task-replacement persistence is a capability the
    # container does not yet have, not a regression -- there is no crew container on main.
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

    # 2. Front. There is no sidecar: backup was extracted from this PR.
    front = _start_front(settings)
    log.info("front: started")

    watched = [backend, front]
    try:
        why = wait_for_shutdown(watched)
        log.info("shutdown: %s", why)
    finally:
        _teardown(front, backend)
    # The exit code has to distinguish the two reasons, because it is the only one
    # the platform reads. `_wait_for_shutdown` returns "signal" for an orderly stop
    # (ECS asked the task to go) and "<name> exited (code N)" when a child died
    # first -- and its own docstring calls the backend dying fatal. Returning 0 for
    # both told ECS a crash loop was a clean shutdown, so the console showed a task
    # exiting normally over and over with nothing marked failed.
    #
    # `signal` is the ONLY success case. Anything else, including an empty reason,
    # is reported as a failure: a reason this code cannot account for is not
    # evidence that things went well.
    if why == "signal":
        return 0
    log.error("exiting non-zero: %s", why or "shutdown reason unknown")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    settings = common.load()
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
