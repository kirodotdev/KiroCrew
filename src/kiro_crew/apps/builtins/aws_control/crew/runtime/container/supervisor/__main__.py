"""Container entrypoint: order the task, supervise it, drain it on shutdown.

Run as ``python -m container.supervisor``. This is the task's init process. It
does not serve anything itself; it enforces the startup order the contract makes
a correctness requirement and then supervises the three children.

The order (``container/CONTRACT.md``, "Startup order"):

1. ``run_restore(settings)`` runs to COMPLETION. Nothing else has started.
   Restore must finish before the backend starts: the backend's periodic flush
   writes its in-memory slot table, and a flush landing before restore finishes
   persists an empty ``open_slots.json`` and destroys the record of which
   conversations existed. If restore fails we abort *before* the backend starts,
   for the same reason -- a backend on an incompletely restored home will flush
   over the gap.
2. The backend starts and ``wait_until_ready`` returns (port answers AND the
   boot secret exists).
3. The front process and the sidecar start.

Shutdown drains process groups, not pids (see ``process.py``): a ``kiro-cli``
worker is a two-process tree and signalling only the launcher orphans a child
that finishes its turn. Teardown order is front, then backend, then sidecar:
stop new turns arriving first, let the backend drain in-flight work and flush to
disk, and terminate the sidecar last so it ran through the whole drain window
and had the latest on-disk state to copy.

Track boundaries: the seams for the other two tracks (``run_restore``,
``run_sidecar``, the front ``__main__``) are imported by their documented paths,
lazily, so this module stays importable and testable while S1/S2 are still being
written, and so it never reimplements their work.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .. import common
from ..common import Settings
from . import backend as backend_mod
from . import bundle as bundle_mod
from .process import ProcessGroup, spawn_process_group

if TYPE_CHECKING:
    from ..backup.restore import RestoreResult

log = logging.getLogger("container.supervisor")

# Drain windows. The backend gets the longest so an in-flight turn can finish.
# Drain windows. The backend gets the longest, and the length is load-bearing:
# a kiro-cli worker spawns with start_new_session (acp/runtime.py:1321), so it
# setsid's into its OWN process group and is NOT in the backend's group. Our
# group SIGKILL therefore cannot reach a worker; only the backend's own SIGTERM
# shutdown reaps it. Too short a drain here would SIGKILL the backend before it
# finishes reaping, orphaning workers that go on to finish their turn. Verified
# 2026-09-02 by reading the real source and booting the real backend.
FRONT_DRAIN_SECS: float = 5.0
BACKEND_DRAIN_SECS: float = 25.0
SIDECAR_DRAIN_SECS: float = 10.0
# Fargate's stop grace is finite and shared by every drain above. Not enforced
# as a timeout (killing a partial upload helps nobody); it is the line past
# which the log says the final cycle is too slow to be relied on.
FINAL_BACKUP_BUDGET_SECS: float = 15.0


def _run_restore(settings: Settings) -> "RestoreResult":
    """Call Track S2's restore seam to completion. Imported lazily by contract.

    Returns the ``RestoreResult`` so the supervisor can act on it. ``run_restore``
    reports a partial restore rather than raising (a missing authority file is a
    degraded boot, not an exception), so discarding this value would silence the
    one signal the startup order exists to protect: the caller decides whether a
    degraded restore is safe to boot on.
    """
    from ..backup.restore import run_restore

    return run_restore(settings)


def _start_front(settings: Settings) -> ProcessGroup:
    """Launch Track S1's front process (its documented ``__main__``)."""
    return spawn_process_group("front", [sys.executable, "-m", "container.front"])


def _start_sidecar(settings: Settings) -> ProcessGroup:
    """Launch Track S2's sidecar via its documented ``run_sidecar`` seam.

    The backup package has no committed ``__main__``; we bootstrap the seam in a
    child rather than create a file in another track's tree, and rather than run
    a blocking loop in-process where it could not be drained as its own group.
    Each child re-reads the environment through ``common.load()`` by design, so
    it gets the same frozen settings.
    """
    bootstrap = (
        "from container.backup.sidecar import run_sidecar; "
        "from container import common; "
        "run_sidecar(common.load())"
    )
    return spawn_process_group("sidecar", [sys.executable, "-c", bootstrap])


def _wait_for_shutdown(children: Sequence[ProcessGroup]) -> str:
    """Block until a stop signal arrives or any child exits. Return the reason.

    Returns ``"signal"`` on SIGTERM/SIGINT, or ``"<name> exited"`` if a child
    dies first (the backend dying is fatal; so is either other child, since the
    task can no longer do its job).
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


def _final_backup_cycle(settings: Settings) -> None:
    """Upload once more after the writers are gone, before the task disappears.

    Without this the last turn a customer took is lost on every task replacement,
    which is not a rare event: it happens on every deploy, every failed health
    check and every platform update. The periodic sidecar cycle runs on a timer,
    so a turn finishing between the last cycle and SIGTERM was never uploaded, and
    the backend's own drain flushes MORE state after that cycle.

    Ordering is the point. Front and backend are already drained, so nothing is
    writing, and the sidecar is already stopped, so there is exactly one writer.
    Running this while the sidecar still lived would be two processes uploading
    the same keys.

    The state is fresh rather than the sidecar's, because that state lives in the
    process that just exited. The cost is re-uploading unchanged objects; the
    alternative is trusting a hand-off of change-detection state across a process
    boundary at the least reliable moment in the task's life. Whole-object puts
    are idempotent and the store has no delete, so re-uploading is safe.
    """
    started = time.monotonic()
    try:
        from ..backup.sidecar import _build_store, run_backup_cycle
        from ..backup.state import BackupState

        store = _build_store(settings)
        if store is None:
            # Cannot happen here (a sidecar existed, so a bucket was configured),
            # but a shutdown path must not raise on the way out.
            log.warning("final backup: no bucket; skipping")
            return
        result = run_backup_cycle(settings, store, BackupState())
    except Exception:
        # Everything from here is guarded, not just the cycle: building the store
        # constructs an S3 client, which needs boto3 -- present in the container
        # image, absent in a source checkout. A shutdown path is the worst place
        # to raise, because the task is already leaving and a traceback would
        # replace an orderly drain with a crash. log.exception rather than a bare
        # pass: an owner whose newest conversation went missing needs to find the
        # reason, so this is loud without being fatal.
        log.exception("final backup: FAILED; the newest turns may not be uploaded")
        return
    elapsed = time.monotonic() - started
    log.info(
        "final backup: uploaded %d object(s), skipped %d unchanged, %d artifact, in %.1fs",
        result.uploaded,
        result.skipped_unchanged,
        result.skipped_artifact,
        elapsed,
    )
    if elapsed > FINAL_BACKUP_BUDGET_SECS:
        # The platform's stop grace is finite. Saying so is the difference between
        # "we did not try" and "we tried and ran out of time", which are different
        # bugs for whoever reads this log after a conversation goes missing.
        log.warning(
            "final backup: took %.1fs, over the %.0fs budget -- a slower unit may "
            "be killed mid-upload on task replacement",
            elapsed,
            FINAL_BACKUP_BUDGET_SECS,
        )


def _teardown(
    front: ProcessGroup,
    backend: ProcessGroup,
    sidecar: ProcessGroup | None,
    settings: Settings,
) -> None:
    """Drain the children in order: front, backend, sidecar.

    ``sidecar`` is None for a chatbot crew, which never started one.
    """
    log.info("draining front (%.0fs)", FRONT_DRAIN_SECS)
    front.terminate(FRONT_DRAIN_SECS)
    log.info("draining backend (%.0fs)", BACKEND_DRAIN_SECS)
    backend.terminate(BACKEND_DRAIN_SECS)
    if sidecar is None:
        # chatbot mode: there was never a sidecar. Nothing to flush, because
        # nothing was ever going to be uploaded.
        return
    log.info("draining sidecar (%.0fs)", SIDECAR_DRAIN_SECS)
    sidecar.terminate(SIDECAR_DRAIN_SECS)
    _final_backup_cycle(settings)


def verify_layout(settings: Settings) -> None:
    """Refuse to start if the SMC paths disagree with what Kiro Crew resolves.

    Kiro Crew keeps its whole data home under ONE root: ``config_dir()`` equals
    the data home equals ``KIROCREW_HOME``, and it writes ``sessions/``,
    ``open_slots.json``, ``session_map.json`` and ``run/gateway-<port>.secret``
    directly under that root (chat_persistence.py:322, run_marker.py, verified
    2026-09-02 by booting the real gateway). The backend is launched with
    ``KIROCREW_HOME=settings.data_home``, so the backend's own ``config_dir()``
    IS ``settings.data_home``. Two path settings must therefore agree, or the
    deployment comes up looking healthy and loses state silently:

    * ``settings.config_dir`` must equal ``settings.data_home``. S2's backup unit
      reads ``open_slots.json`` and ``session_map.json`` from ``config_dir``; if
      that is a ``/config`` subdir the backend never writes to, restore and
      backup target empty files and the record of which conversations existed is
      lost -- the exact §9.1 failure.
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


def _user_namespaces_available() -> bool | None:
    """Probe whether this host permits an unprivileged user namespace.

    Returns True/False on Linux, or None where the probe cannot run (no
    ``os.unshare`` -- non-Linux), in which case the caller treats availability
    as unknown and does not block. The probe runs in a forked child because
    ``unshare`` mutates the caller's namespaces.
    """
    if not (hasattr(os, "unshare") and hasattr(os, "CLONE_NEWUSER")):
        return None
    pid = os.fork()
    if pid == 0:
        try:
            os.unshare(os.CLONE_NEWUSER)  # type: ignore[attr-defined]
            os._exit(0)
        except OSError:
            os._exit(1)
        except Exception:
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def _unsandboxed_exec_opted_in(settings: Settings) -> bool:
    """Read ``agent.sandbox_allow_unsandboxed_exec`` from the data home.

    Kiro Crew reads it from ``<home>/config.json`` under ``agent`` (loader.py:
    2251), with ``config.local.json`` overlaying it. We read the same files
    minimally rather than importing kiro_crew.
    """
    result = False
    for name in ("config.json", "config.local.json"):
        path = settings.data_home / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        agent = data.get("agent") if isinstance(data, dict) else None
        if isinstance(agent, dict) and "sandbox_allow_unsandboxed_exec" in agent:
            raw = agent["sandbox_allow_unsandboxed_exec"]
            # This value decides whether the model subprocess may run WITHOUT a
            # sandbox, so it gets the strictest treatment of the three truthiness
            # sites. ``bool("false")`` is ``True``: a config that said the STRING
            # "false" -- meaning off -- would be read as CONSENT to run
            # unsandboxed, the single worst direction to be wrong in. So only a
            # real boolean ``True`` counts as opt-in. Anything that is not a bool
            # is neither trusted as consent nor silently read as "no": it is
            # refused loudly, because a string "true" the author expected to
            # enable the posture would otherwise let the container refuse to boot
            # with a message that names the wrong cause. The safe DEFAULT remains
            # "not opted in" (sandbox required) -- refusing is fail-closed, since
            # a refused boot never runs a turn, let alone an unsandboxed one.
            if isinstance(raw, bool):
                result = raw
                continue
            raise common.ConfigError(
                f"{path} has a non-boolean agent.sandbox_allow_unsandboxed_exec: "
                f"{raw!r}. It is not interpreted loosely because the string "
                f'"false" is truthy, and reading it as consent would run the '
                f"model subprocess UNSANDBOXED. Write true or false, not a string."
            )
    return result


def apply_sandbox_posture(settings: Settings) -> None:
    """Record the deployment's sandbox decision where kiro-cli reads it.

    ``agent.sandbox_allow_unsandboxed_exec`` is a CONFIG KEY with no environment
    variable equivalent (sections.py:636), so a deployment that has accepted
    unsandboxed execution cannot express it in the task definition alone -- it has
    to land in ``<home>/config.json``. Nothing wrote that file, and the guard below
    only read it, so both sides correctly deferred the decision to the other and
    neither made it: the container refused to boot on Fargate with a message
    naming a file that never existed.

    Merges into any existing config rather than replacing it, and writes only when
    the value would change, so a restored or hand-edited config keeps every other
    key and an unchanged boot does not rewrite the file.
    """
    if not settings.allow_unsandboxed_exec:
        return
    path = settings.data_home / "config.json"
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError) as exc:
            # Refuse rather than overwrite: a config.json we cannot parse may hold
            # settings that matter, and replacing it would discard them silently.
            raise common.ConfigError(
                f"{path} exists but is not readable JSON ({exc}); refusing to "
                "replace it. Fix or remove the file."
            ) from exc

    agent = data.get("agent")
    if not isinstance(agent, dict):
        agent = {}
    if agent.get("sandbox_allow_unsandboxed_exec") is True:
        return
    agent["sandbox_allow_unsandboxed_exec"] = True
    data["agent"] = agent

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    log.warning(
        "SMC_ALLOW_UNSANDBOXED_EXEC is set, so %s now allows the model subprocess "
        "to run WITHOUT a sandbox. This is the deployment's accepted posture: "
        "Fargate does not permit an unprivileged user namespace.",
        path,
    )


def verify_sandbox(settings: Settings, *, probe=_user_namespaces_available) -> None:
    """Refuse to start if kiro-cli workers could never spawn.

    kiro-cli runs the model subprocess inside a sandbox. On Linux that needs an
    unprivileged user namespace; without one, ``wrap_argv`` fails CLOSED unless
    ``agent.sandbox_allow_unsandboxed_exec=true`` (sections.py:636). If the host
    has no user namespaces AND the opt-in is off, every turn fails to spawn a
    worker and the container looks healthy while doing nothing -- so we fail
    loudly at startup instead. We do NOT silently enable unsandboxed exec: that
    is a security decision for the deployment (a config write into the home).
    """
    available = probe()
    if available is None or available is True:
        return
    if _unsandboxed_exec_opted_in(settings):
        log.warning(
            "user namespaces unavailable; agent.sandbox_allow_unsandboxed_exec "
            "is on, so the model subprocess will run UNSANDBOXED (fail-open, "
            "accepted by config)."
        )
        return
    raise common.ConfigError(
        "No user-namespace sandbox is available on this host and "
        "agent.sandbox_allow_unsandboxed_exec is off: kiro-cli cannot spawn the "
        "model subprocess, so every turn would fail while the container reports "
        "healthy. Either run where unprivileged user namespaces are permitted, "
        "or set agent.sandbox_allow_unsandboxed_exec=true in "
        f"{settings.data_home / 'config.json'} to accept unsandboxed execution."
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
    apply_sandbox_posture(settings)
    verify_sandbox(settings)
    # Install the crew into the paths Kiro Crew reads BEFORE the backend starts,
    # so "it started" means "the named crew is installed" rather than a default
    # agent. Refuses closed on any mismatch (see bundle.install_bundle).
    bundle_mod.install_bundle(settings)

    # 1. Restore to completion, before anything else exists.
    log.info("restore: starting")
    result = _run_restore(settings)
    # Act on what restore reported. run_restore does NOT raise on a partial set
    # (a missing authority file is a degraded boot, not an exception), so the
    # decision is ours and it is fail-closed: if an authoritative file was
    # missing, we abort BEFORE the backend starts. This is the same reason the
    # module docstring gives -- a backend on an incompletely restored home runs
    # its periodic flush and persists an empty open_slots.json over the gap,
    # destroying the record of which conversations existed. Booting a task that
    # would erase that record is worse than not booting at all.
    #
    # "abort" here means raise, which exits the process. On Fargate the service
    # replaces the task, so a persistent cause (a truly missing object in the
    # bucket) becomes a crash loop. A crash loop that refuses to serve is the
    # right failure: it is loud, it is visible in the task's exit status and the
    # logged reason below, and it never erases state -- unlike a silent boot that
    # flushes over the gap and looks healthy while losing every conversation. The
    # same reasoning already governs the sidecar, which is NOT started with no
    # bucket precisely because an unconditional start caused exactly such a loop.
    # The log line names what was missing and what an owner must do, so the loop
    # is explained rather than mysterious.
    #
    # ``empty`` (nothing in the bucket) is NOT partial: it is a clean first boot
    # and MUST proceed. ``disabled`` (no bucket configured -- chatbot mode) also
    # proceeds; there was never anything to restore. Only ``partial`` aborts.
    if result.partial:
        raise common.ConfigError(
            "restore was PARTIAL: an authoritative file was missing from the "
            "backup, so booting the backend now would flush an in-memory slot "
            "table over the gap and destroy the record of which conversations "
            "existed. Refusing to start. Missing: "
            + (", ".join(result.missing) if result.missing else "unknown")
            + ". An owner should confirm the backup bucket "
            "(SMC_BACKUP_BUCKET) holds session_map.json and open_slots.json "
            "under the crew prefix; if this is a genuine first boot the bucket "
            "should be EMPTY, not missing individual files. See the "
            "'restore: SUMMARY' line above for the exact state."
        )
    log.info("restore: complete (state=%s)", result.state)

    # 2. Backend, then readiness. Nothing else has started yet.
    backend = backend_mod.start_backend(settings, env=env)
    try:
        backend_mod.wait_until_ready(
            settings, backend_mod.DEFAULT_READY_TIMEOUT_SECS, process=backend
        )
    except Exception:
        # Readiness failed or the backend exited: tear the backend down and
        # abort. Front and sidecar were never started.
        log.error("backend did not become ready; aborting")
        backend.terminate(BACKEND_DRAIN_SECS)
        raise
    log.info("backend: ready on %s", settings.backend_base_url)

    # 3. Front, and the sidecar only when there is somewhere to sync to.
    #
    # Starting it with no bucket would be a BOOT LOOP, not a no-op: run_sidecar
    # logs and returns when backup_bucket is None, and _wait_for_shutdown treats
    # any child exiting as the end of the task, so the container would come up,
    # lose its sidecar within a second, shut down, and be replaced by ECS forever.
    front = _start_front(settings)
    sidecar = _start_sidecar(settings) if settings.backup_bucket else None
    if sidecar is None:
        log.info("front: started; sidecar: NOT started (no bucket, chatbot mode)")
    else:
        log.info("front and sidecar: started")

    watched = [backend, front] + ([sidecar] if sidecar else [])
    try:
        why = wait_for_shutdown(watched)
        log.info("shutdown: %s", why)
    finally:
        _teardown(front, backend, sidecar, settings)
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
