"""Pod service lifecycle: start, stop with HOME reclamation, and backend install.

:func:`start_pod` and :func:`stop_pod` are backend-agnostic, so no verb has to
know which service manager it is talking to, and the teardown obligations -- drain
the unit's process tree, reclaim the isolated HOME, then verify -- cannot be
honoured at one call site and forgotten at another. Both run under the per-name
lifecycle mutex. The systemd half keeps the invariant that a unit file present on
disk has been loaded by systemd, because the cached definition is what executes.

The systemd adapter, the mutex and the platform flags are read from
:mod:`kiro_crew.pod.runtime` at call time, the namespace the pod suite patches.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from kiro_crew.pod import launchd, runtime, runtime_home
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod import windows as win_backend
from kiro_crew.pod.config import PodConfig

# --------------------------------------------------------------------------- #
# Sentinel in stop_pod's stdout meaning the pod NAME was reclaimed by a new pod
# mid-teardown (down/up race). The old pod is gone, but per-name state (the env
# file pinning CHECKOUT=) now belongs to the NEW pod and must not be deleted.
RECLAIMED_MARKER = "pod-name-reclaimed-by-new-pod"


def _write_and_load_unit(cfg: PodConfig) -> subprocess.CompletedProcess | None:
    """Render the template unit AND load it, or leave nothing behind.

    The single writer of the unit file, because the invariant it maintains has to
    hold for EVERY writer: *a unit file present on disk has been loaded by
    systemd.* :func:`kiro_crew.pod.unit.unit_is_current` reads the file, but what
    systemd executes is the definition it loaded — so a writer that renders the
    current hookless template and then fails to reload leaves a file that reads
    "current" in front of a cached definition still carrying the destructive
    ``ExecStopPost``. :func:`start_pod` then skips its refresh and boots the pod
    under that cached definition, whose hook deletes the pod's HOME on any
    systemd-initiated stop — including the stop half of a ``Restart=``, which no
    ``down`` gate is in the path of. Unlinking on failure keeps the on-disk state
    honest, so the next call re-renders and retries.

    Returns ``None`` on success, or the failing ``daemon-reload`` result.
    """
    unit_mod.install_unit(cfg)
    cp = runtime.systemctl("daemon-reload")
    if cp.returncode == 0:
        return None
    unit_mod.unit_path(cfg).unlink(missing_ok=True)
    return cp


def _refresh_stale_unit(cfg: PodConfig) -> subprocess.CompletedProcess | None:
    """Re-render and load the template unit; report why the caller must not proceed.

    Returns ``None`` once systemd is running the current definition, or a failure
    carrying the remedy when it is not.
    """
    cp = _write_and_load_unit(cfg)
    if cp is None:
        return None
    detail = f" {cp.stderr.strip()}" if (cp.stderr or "").strip() else ""
    return subprocess.CompletedProcess(
        args=[],
        returncode=cp.returncode or 1,
        stdout=cp.stdout or "",
        stderr=(
            f"refreshed the pod template unit but `systemctl --user daemon-reload` "
            f"failed (rc={cp.returncode}), so systemd would still run the previous "
            "definition — which deletes a pod's HOME from a stop hook. Refusing to "
            "start or stop a pod until the unit is loaded: run `kirocrew pod install` "
            f"and retry.{detail}"
        ),
    )


def loaded_teardown_hook(cfg: PodConfig, name: str) -> bool | None:
    """Whether systemd will run a teardown hook when THIS pod's unit stops.

    Asks systemd what it has LOADED instead of reading the unit file. Disk
    freshness is not proof of a load — a hand-edited unit, or any writer whose
    reload failed, leaves the two disagreeing — and the question that decides
    whether a stop is safe is only ever "what will systemd execute now".

    ``None`` means the question could not be answered; callers must treat that as
    "assume the hook is there" rather than as absence.
    """
    cp = runtime.systemctl("show", runtime.pod_unit(cfg, name), "-p", "ExecStopPost", "--value")
    if cp.returncode != 0:
        return None
    return bool((cp.stdout or "").strip())


def _install_pod_dropin(cfg: PodConfig, name: str) -> subprocess.CompletedProcess | None:
    """Pin pod *name* to its checkout binary, or return a start-blocking failure."""
    checkout = runtime.read_env_file(cfg, name).get("CHECKOUT", "")
    if not checkout:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                f"pod {name!r} has no pinned checkout, so its boot cannot use the "
                f"worktree's own kirocrew. Run `kirocrew pod up {name}` from inside "
                "the checkout."
            ),
        )
    try:
        unit_mod.install_dropin(cfg, name, Path(checkout).expanduser())
    except (OSError, ValueError) as exc:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"could not write the boot override for pod {name!r}: {exc}",
        )
    cp = runtime.systemctl("daemon-reload")
    if cp.returncode == 0:
        return None
    unit_mod.remove_dropin(cfg, name)
    detail = f" {cp.stderr.strip()}" if (cp.stderr or "").strip() else ""
    return subprocess.CompletedProcess(
        args=[],
        returncode=cp.returncode or 1,
        stdout=cp.stdout or "",
        stderr=(
            f"wrote the boot override for pod {name!r} but `systemctl --user "
            f"daemon-reload` failed (rc={cp.returncode}), so systemd would still "
            "boot the globally installed kirocrew. Refusing to start it; retry or "
            f"run `kirocrew pod install`.{detail}"
        ),
    )


def start_pod(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    """Bring pod *name* up through whichever service manager this host uses."""
    with runtime.pod_name_mutex(cfg, name):
        if runtime.IS_MACOS:
            # Re-rendered every start, which is why launchd needs no equivalent
            # of the systemd path's stale-ExecStart self-heal. The mutex
            # serializes against a concurrent stop of the same name, whose
            # definition unlink and HOME sweep would otherwise race this write.
            launchd.write_plist(cfg, name)
            return launchd.start(cfg, name)

        if runtime.IS_WINDOWS:
            # Same reasoning as macOS: the wrapper script and the task are both
            # re-created on every start, so neither can go stale against a moved
            # worktree, and the mutex serializes against a concurrent stop of the
            # same name whose script unlink and HOME sweep would race this write.
            return win_backend.start(cfg, name)

        # Self-heal a stale installed unit before booting it: the template bakes
        # an absolute kirocrew path at install time (a pruned worktree leaves it
        # failing EXEC 203), and a unit installed by an older build can still
        # carry the teardown hook this one removed.
        if not unit_mod.unit_is_current(cfg):
            refused = _refresh_stale_unit(cfg)
            if refused is not None:
                return refused
        refused = _install_pod_dropin(cfg, name)
        if refused is not None:
            return refused
        return runtime.systemctl("start", runtime.pod_unit(cfg, name))


# Where a systemd cgroup's process list lives on a cgroup-v2 host.
_CGROUP_ROOT = Path("/sys/fs/cgroup")


# How long teardown waits for a stopped unit's process tree to go away. Named so
# the wait and the message that reports it expiring cannot drift apart.
DRAIN_TIMEOUT_SECS = 15.0


def cgroup_procs_file(cfg: PodConfig, name: str) -> Path | None:
    """``cgroup.procs`` for pod *name*'s unit, or ``None`` when unresolvable.

    Must be read while the unit is still up: systemd reports an empty
    ``ControlGroup`` once it goes inactive, so asking after the stop is too late.
    Returns ``None`` off cgroup-v2 layouts (and on any host where the path does
    not exist), which makes the drain wait an optimisation rather than a
    dependency — the post-delete verification in :func:`stop_pod` is what actually
    decides whether teardown succeeded.
    """
    cp = runtime.systemctl("show", runtime.pod_unit(cfg, name), "-p", "ControlGroup", "--value")
    rel = (cp.stdout or "").strip()
    if cp.returncode != 0 or not rel.startswith("/"):
        return None
    procs = _CGROUP_ROOT / rel.lstrip("/") / "cgroup.procs"
    return procs if procs.parent.is_dir() else None


def drain_cgroup(procs: Path, timeout: float = DRAIN_TIMEOUT_SECS) -> list[str]:
    """Wait for a stopped unit's cgroup to empty; return the PIDs still in it.

    An empty list means every pod-scoped process is gone, so the HOME can be
    deleted without racing a writer that would recreate it. A vanished cgroup
    directory counts as drained — systemd removes it once the last process exits.
    An unreadable one is reported as drained too: nothing better can be observed
    from here, and the caller verifies the deleted HOME afterwards regardless.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            pids = [ln.strip() for ln in procs.read_text().splitlines() if ln.strip()]
        except OSError:
            return []
        if not pids:
            return []
        if time.monotonic() >= deadline:
            return pids
        time.sleep(0.2)


def stop_pod(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    """Stop pod *name* and reclaim its isolated HOME, or say why it could not.

    Teardown lives HERE on both platforms rather than in a post-stop service hook.
    systemd runs ``ExecStopPost`` before the final kill of the unit's cgroup, so a
    hook-based delete raced the pod's own surviving subprocesses — they reopened
    their audit log in append mode and recreated the directory behind it — and it
    also ran on the stop half of a ``Restart=``, bringing the pod back up on a
    home stripped of its sessions or config. Reclaiming after the service
    is confirmed down fixes both, at the cost of a pod that goes away without a
    ``down`` leaving its HOME behind; :func:`orphan_homes` reports those.

    Sequenced so nothing is deleted while a writer could still be alive: stop the
    service, wait for its process tree to drain, delete, then VERIFY. A HOME that
    survives is reported as a failure — never as zero residue.
    """
    with runtime.pod_name_mutex(cfg, name):
        if runtime.IS_MACOS:
            return _stop_pod_launchd(cfg, name)
        if runtime.IS_WINDOWS:
            return _stop_pod_windows(cfg, name)
        # A unit installed by an OLDER build still carries the destructive
        # ExecStopPost, and `systemctl stop` runs it before our drain — deleting
        # the HOME under the pod's own live processes, which is the exact defect
        # this path exists to remove. Refresh BEFORE stopping: daemon-reload
        # re-parses the fragment for an already-running unit, and the stop job has
        # not started yet, so the refreshed (hookless) definition is what runs.
        #
        # Gated on what systemd has LOADED, never on the unit file: disk freshness
        # is not proof of a load, so a hookless file can sit in front of a cached
        # definition that still deletes the HOME. An unanswerable query counts as
        # "hook present" — the only safe reading.
        #
        # Refuse rather than proceed when the reload fails. Proceeding would mean
        # knowingly triggering the hook-races-live-processes defect this change
        # removes, on the argument that the HOME is being deleted anyway — the
        # same reasoning the fix rejects. A pod left running after a loud,
        # retryable failure is the safer end state.
        if loaded_teardown_hook(cfg, name) is not False:
            refused = _refresh_stale_unit(cfg)
            if refused is not None:
                return refused
        # Read the cgroup path BEFORE stopping: systemd clears ControlGroup on
        # an inactive unit.
        procs_file = cgroup_procs_file(cfg, name)
        cp = runtime.systemctl("stop", runtime.pod_unit(cfg, name))
        if cp.returncode != 0:
            # The unit may still be live; deleting its HOME here is exactly the
            # race this ordering exists to avoid.
            return cp
        survivors = drain_cgroup(procs_file) if procs_file is not None else []
        # Resolved, because cleanup_home reports the resolved path: on a host
        # whose home is a symlink, naming it both ways reads as two directories.
        leftover = runtime_home.resolved_pod_home(cfg, name)
        if survivors:
            # Deleting now would BE the original defect. A process that outlived
            # the drain either holds the tree open or reopens its audit log in
            # append mode right behind the delete, and the verification below
            # cannot catch that because the recreation lands after it. So leave
            # the HOME alone and name what is holding it.
            shown = ", ".join(survivors[:5])
            return subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=cp.stdout or "",
                stderr=(
                    f"pod stopped but {len(survivors)} pod process(es) are still in "
                    f"its cgroup (pid {shown}) after {DRAIN_TIMEOUT_SECS:.0f}s, so "
                    f"its isolated HOME at {leftover} was NOT deleted — this pod is "
                    f"NOT zero-residue. Reclaim it with `kirocrew pod down {name}` "
                    "once nothing is writing there."
                ),
            )
        rc = runtime_home.cleanup_home(cfg, name)
        dropin_path = unit_mod.dropin_path(cfg, name)
        # Linux-only (a systemd drop-in): junctions do not exist on this
        # platform, so ``is_symlink()`` is the complete link test here.
        had_dropin = dropin_path.exists() or dropin_path.is_symlink()
        dropin_gone = unit_mod.remove_dropin(cfg, name)
        reload_cp: subprocess.CompletedProcess | None = None
        if had_dropin and dropin_gone:
            reload_cp = runtime.systemctl("daemon-reload")
        if rc != 0 or leftover.exists():
            return subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=cp.stdout or "",
                stderr=(
                    f"pod stopped but its isolated HOME is still at {leftover} — "
                    f"teardown is incomplete, so this pod is NOT zero-residue. "
                    f"Reclaim it with `kirocrew pod down {name}` once nothing is "
                    "writing there."
                ),
            )
        if not dropin_gone:
            return subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=cp.stdout or "",
                stderr=(
                    f"pod stopped and its HOME was reclaimed, but the boot override at "
                    f"{unit_mod.dropin_path(cfg, name)} could not be removed — this pod "
                    f"is NOT zero-residue. Delete it, then run `systemctl --user "
                    "daemon-reload`."
                ),
            )
        if reload_cp is not None and reload_cp.returncode != 0:
            detail = f" {reload_cp.stderr.strip()}" if (reload_cp.stderr or "").strip() else ""
            return subprocess.CompletedProcess(
                args=[],
                returncode=reload_cp.returncode or 1,
                stdout=cp.stdout or "",
                stderr=(
                    "pod stopped and its on-disk override was removed, but `systemctl "
                    "--user daemon-reload` failed, so systemd may still retain it in "
                    f"memory — this pod is NOT zero-residue.{detail}"
                ),
            )
        return cp


def _stop_pod_launchd(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    """The macOS half of :func:`stop_pod` — called with the name mutex held.

    launchd has no cgroup to drain, so the surviving-writer problem is handled by
    sweeping the grace window instead: ``bootout`` confirms the SERVICE process is
    unloaded, but a dying child can outlive it by a beat and flush state on exit
    (observed in a real teardown — cleanup ran, verification passed, then a child
    wrote settings back and resurrected the HOME milliseconds later).
    """
    # launchd.stop() is authoritative: rc 0 means the label is confirmed
    # unloaded (a bootout of an unloaded label is a no-op success). A non-zero
    # rc means the unload could NOT be confirmed — in that case do NOT touch
    # the HOME: it may belong to a live gateway.
    cp = launchd.stop(cfg, name)
    if cp.returncode != 0:
        return cp
    leftover = runtime_home.resolved_pod_home(cfg, name)
    # Observe the FULL window — no early exit on a clean sample (the dying
    # child that motivated this flushed state after a beat). But DO exit the
    # moment the name is claimed by a NEW pod: the mutex serializes callers that
    # route through it, and a new `up` re-writes the plist BEFORE bootstrapping,
    # so plist presence is the claim marker for any writer that bypasses it.
    # Deliberately a pure filesystem check: probing launchctl here would shell
    # out on every sweep and break on hosts without launchd (the unit suites run
    # this path on Linux/Windows CI).
    #
    # A reclaimed name is reported via RECLAIMED_MARKER in stdout so the caller
    # knows the teardown handed over: it must NOT delete the per-pod env file,
    # which now pins the NEW pod's checkout.
    for _ in range(6):
        if launchd.plist_path(cfg, name).exists():
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout=RECLAIMED_MARKER, stderr=""
            )
        runtime_home.cleanup_home(cfg, name)
        time.sleep(0.5)
    if launchd.plist_path(cfg, name).exists():
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=RECLAIMED_MARKER, stderr=""
        )
    runtime_home.cleanup_home(cfg, name)
    if leftover.exists():
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=cp.stdout or "",
            stderr=(
                f"pod stopped but its isolated HOME keeps reappearing at "
                f"{leftover} — a process is still writing there, so teardown "
                "is incomplete. Remove it by hand and report this."
            ),
        )
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=cp.stdout or "", stderr="")


def _stop_pod_windows(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    """The Windows half of :func:`stop_pod` — called with the name mutex held.

    A boot-contained Job proves the gateway descendants empty after the exact
    publisher retires. The seven filesystem sweeps accommodate delayed Windows
    handle release; they are not a substitute for process retirement. A durable
    receipt remains until every sweep and the final absence check succeed.
    """
    # windows.stop() is authoritative: rc 0 means the gateway is confirmed gone
    # AND the task is deleted. A non-zero rc means one of those could not be
    # confirmed — in that case do NOT touch the HOME: it may belong to a live
    # gateway.
    cp = win_backend.stop(cfg, name)
    if cp.returncode != 0:
        return cp
    leftover = runtime_home.resolved_pod_home(cfg, name)
    # Observe the FULL window — no early exit on a clean sample. But DO exit the
    # moment the name is claimed by a NEW pod: a new `up` writes the wrapper
    # script BEFORE creating the task, so script presence is the claim marker for
    # any writer that bypasses the mutex. Deliberately a pure filesystem check,
    # for the same reason the macOS path is: probing the service manager here
    # would shell out on every sweep and break on hosts without schtasks (the
    # unit suites run this path on Linux and macOS CI).
    for _ in range(6):
        if win_backend.task_script_path(cfg, name).exists():
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout=RECLAIMED_MARKER, stderr=""
            )
        runtime_home.cleanup_home(cfg, name)
        time.sleep(0.5)
    if win_backend.task_script_path(cfg, name).exists():
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=RECLAIMED_MARKER, stderr=""
        )
    runtime_home.cleanup_home(cfg, name)
    # The recorded boot result is per-pod state like the wrapper script, so it
    # must not outlive the pod: a stale non-zero code would make the NEXT `up` of
    # this name read as already-failed before its own boot recorded anything.
    try:
        win_backend.result_path(cfg, name).unlink(missing_ok=True)
    except OSError as exc:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=cp.stdout or "",
            stderr=f"pod result sidecar {win_backend.result_path(cfg, name)} could not "
            f"be deleted; its retirement receipt was preserved for retry: {exc}",
        )
    if leftover.exists():
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=cp.stdout or "",
            stderr=(
                f"pod stopped but its isolated HOME keeps reappearing at "
                f"{leftover} — a process is still writing there, so teardown "
                "is incomplete. Remove it by hand and report this."
            ),
        )
    try:
        win_backend.runs.finish(cfg, name)
    except (OSError, ValueError) as exc:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=cp.stdout or "",
            stderr=f"pod HOME is gone but its retirement receipt was preserved: {exc}",
        )
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=cp.stdout or "", stderr="")


def install_backend(cfg: PodConfig) -> tuple[str, subprocess.CompletedProcess | None]:
    """Install whatever machine-wide definition the backend needs.

    systemd needs one template unit + a daemon-reload. launchd has no template
    concept — each pod's plist is written at ``up`` — and neither does Task
    Scheduler, whose per-pod ``.cmd`` wrapper and task are both created at ``up``.
    So on both of those there is nothing to install, and saying so is better than
    writing a file that does nothing.

    Returns ``(message, reload_result)``. Raises :class:`PodError` only for an
    unusable host, and does so BEFORE writing anything, so an unsupported
    platform never leaves a stray definition behind. A failed reload comes back
    as the second element rather than an exception, because the caller reports
    that as a hard exit while the gate refusal is converted by the CLI's
    dispatch layer — two different documented behaviours.

    Routed through :func:`_write_and_load_unit` so this path upholds the same
    invariant the lifecycle paths do: a unit file left on disk has been loaded.
    A hookless file left behind by a failed reload would make
    ``unit_is_current`` report "current" while systemd still runs the old
    ``ExecStopPost`` — so :func:`start_pod` would skip its refresh and boot the
    pod under a definition that deletes its HOME from a stop hook.
    """
    runtime.require_backend()
    if runtime.IS_MACOS:
        return (
            "nothing to install on macOS: launchd has no template units, so each "
            "pod's agent plist is written at `kirocrew pod up <worktree>`.",
            None,
        )
    if runtime.IS_WINDOWS:
        return (
            "nothing to install on Windows: Task Scheduler has no template tasks, "
            "so each pod's task and its .cmd wrapper are created at "
            "`kirocrew pod up <worktree>`.",
            None,
        )
    dst = unit_mod.unit_path(cfg)
    failed = _write_and_load_unit(cfg)
    if failed is not None:
        return (
            f"rendered the pod template unit but `systemctl --user daemon-reload` "
            f"failed, so it was removed again rather than left unloaded at {dst}",
            failed,
        )
    return (
        f"installed pod template unit → {dst}\nsystemctl --user daemon-reload OK",
        subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
