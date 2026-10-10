"""The persisted identity of every spawned backend: ``app_backends.pids.json``.

App backends run in their OWN session (``start_new_session=True``) and are NOT in the
gateway's process group, so when the liveness probe SIGKILLs a wedged gateway (no
``on_cleanup`` runs) they orphan, reparent to PID 1, and accumulate across restarts.
Each spawned backend's ``(pid, start_time, port, spawn_instance)`` is therefore
persisted here and the next clean start reaps the survivors of a PRIOR generation
(:mod:`~kiro_crew.apps.backend_runtime.stale_reap`). ``start_time`` is the PID-reuse
guard: a recorded pid whose live start time does not match names another process now.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.backend_runtime import _FACADE
from kiro_crew.apps.backend_runtime.tracking import (
    AppProcess,
    _lock,
    _processes,
    app_backend_lifecycle_flock,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.session_pid import (
    _env_spawn_instance,
    group_vouching_available,
)

logger = logging.getLogger(_FACADE)


# Serializes the pidfile read-modify-write. _record_app_pid runs on the
# to_thread worker that spawns a backend (both the runtime app-enable path and
# the startup reconcile offload start_app_backend via asyncio.to_thread) while
# _forget_app_pid runs on the to_thread worker that stops one — distinct OS
# threads, so without this lock their non-atomic read-modify-writes of the
# whole JSON dict lose each other's entries.
#
# A threading.Lock serializes only THIS process's threads. The offline CLI
# uninstall (`kirocrew app uninstall`) revokes/forgets the spawn row in a
# SEPARATE process, which can race the live gateway recording a different app's
# spawn — two processes each read the JSON, mutate their own snapshot, and the
# later atomic rename clobbers the other's new row. _PidfileGuard adds an
# advisory interprocess flock on a sidecar lockfile so the CLI and the gateway
# serialize their read-modify-writes too, not just threads within one process.
class _PidfileGuard:
    """A composite lock: this process's thread lock PLUS a cross-process flock.

    ``with _pidfile_lock:`` acquires the ``threading.Lock`` first (the in-process
    guarantee, unchanged) and THEN tries an exclusive ``flock`` on a sidecar
    ``app_backends.pids.lock`` so a writer in another process (the offline CLI
    uninstall) cannot interleave its read-modify-write with the gateway's.

    The flock is BEST-EFFORT and never changes the in-process guarantee: if it
    cannot be taken — the sidecar is unopenable, or the loop-thread single-shot
    refusal fires under contention — the block still runs under the thread lock
    alone, exactly as before this guard existed. That keeps a live gateway's
    on-loop writers from being refused or stalled, while giving the standalone CLI
    process (where the flock is a real, uncontended wait) genuine cross-process
    serialization against the gateway. Re-entrancy matches the old bare lock: the
    thread lock is non-reentrant, so nested acquisition still deadlocks and the
    code must not re-enter — the flock is only held while the thread lock is.
    """

    def __init__(self) -> None:
        self._thread_lock = threading.Lock()
        self._flock_fd: int | None = None
        self._flock_cm: Any = None

    def _lock_path(self) -> Path:
        return config_dir() / "app_backends.pids.lock"

    def __enter__(self) -> "_PidfileGuard":
        self._thread_lock.acquire()
        try:
            path = self._lock_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # 0o600: this sidecar lives in the owner-only data home, so a
            # fresh boot must not leave it readable by other users. The pidfile
            # itself is already owner-only (atomic_write defaults a data-home
            # target to 0o600); the lock takes the same restriction.
            self._flock_fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
            cm = platform_compat.file_lock(self._flock_fd, exclusive=True)
            cm.__enter__()
            self._flock_cm = cm
        except Exception as exc:  # noqa: BLE001 — best-effort: fall back to thread lock
            logger.debug("pidfile interprocess flock not taken (%s); thread lock only", exc)
            self._release_flock()
        return self

    def _release_flock(self) -> None:
        if self._flock_cm is not None:
            try:
                self._flock_cm.__exit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("pidfile flock release failed: %s", exc)
            self._flock_cm = None
        if self._flock_fd is not None:
            try:
                os.close(self._flock_fd)
            except OSError:
                pass
            self._flock_fd = None

    def __exit__(self, *exc: Any) -> None:
        self._release_flock()
        self._thread_lock.release()


_pidfile_lock = _PidfileGuard()

# In-memory quarantine backstop, guarded by _pidfile_lock. The on-disk marker
# (app_backends.quarantine.json) is written by the SAME atomic_write that fails
# under the ENOSPC/EDQUOT the quarantine exists to guard against -- so a disk-only
# marker is absent in precisely the case it must be present. This set needs no
# write, so a quarantine landed here holds for the running generation even when the
# disk is full; the on-disk marker adds durability ACROSS a restart (and a restart's
# stale-reap independently signals and drops the orphaned leader row either way).
_quarantined_backends: set[str] = set()


class PidfileDeleteFailed(Exception):
    """A spawn-record deletion that MUST be confirmed could not be persisted.

    Raised only by :func:`forget_backend_provenance`, the uninstall/update path:
    there a retained row re-attributes an old-code survivor, so the caller must
    abort before any restart rather than proceed on an unconfirmed delete. The
    best-effort stop/record paths never raise it — they restore the row on refusal.
    """


class PidfileRevokeFailed(Exception):
    """A DURABLE revocation that must land before destructive lifecycle work failed.

    Raised by :func:`revoke_backend_provenance`. Update/uninstall revoke the spawn
    row BEFORE they replace or delete the app so a detached survivor of the old code
    cannot be re-attributed on a same-name reinstall. The revoke must survive a
    restart — the in-memory quarantine set does not — so it is written AND re-read to
    confirm the ``revoked`` stamp reached disk. When it cannot be confirmed (ENOSPC /
    EDQUOT), the caller must refuse the destructive step rather than commit onto a row
    that would still vouch for the survivor after a crash or restart.
    """


def _pidfile_path() -> Path:
    # The app-backend spawn record: pid, start instant and per-spawn instance
    # token for each app whose backend the gateway launched. The adoption path
    # attributes a captured owner against this record to tell an app's own
    # backend from a survivor of a previous install.
    return config_dir() / "app_backends.pids.json"


def _proc_start_time(pid: int) -> str | None:
    """Stable per-process start time, or None if unavailable.

    PID-reuse guard: a recorded pid whose live start_time does not match has
    been recycled to an unrelated process and MUST NOT be killed. The value must
    be stable across gateway restarts (the reap compares a string recorded by a
    prior generation against one read now), so it cannot use ``hash()`` — that
    is salted per interpreter by ``PYTHONHASHSEED``.

    Per-platform sources live in ``platform_compat.process_start_time``: Linux
    reads ``/proc/<pid>/stat`` field 22, Windows the process creation FILETIME
    through a query-only handle, and other POSIX ``ps -o lstart=``. Resolving it
    there is what keeps the guard alive on Windows — a ``/proc``-or-``ps`` probe
    answers None for every pid there, and a recorded None makes the reap decline
    to confirm ANY backend, so nothing is ever reaped and the entries accumulate.
    """
    return platform_compat.process_start_time(pid)


def _read_pidfile() -> dict[str, dict[str, Any]]:
    try:
        with open(_pidfile_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # A corrupt/half-written pidfile (e.g. a SIGKILL mid-write before atomic
        # writes landed, or a leftover from an older build) silently disabling
        # the reap is exactly the leak this feature exists to prevent — log it.
        logger.warning("App-backend pidfile unreadable (%s); stale-reap skipped this start", exc)
        return {}


def _read_pidfile_strict() -> dict[str, dict[str, Any]]:
    """Read the pidfile, RAISING on an unreadable store instead of masking it as empty.

    :func:`_read_pidfile` swallows ``OSError``/``ValueError`` and returns ``{}`` so a
    corrupt or unreadable file disables only the best-effort stale-reap. A deletion
    that MUST be confirmed cannot use it: a swallowed read makes ``data.pop`` yield
    ``None``, indistinguishable from "no row", so the caller would report a confirmed
    invalidation that never happened and restart onto the survivor the stale row still
    attributes. This variant surfaces the failure as ``OSError`` (``ValueError`` on a
    corrupt file is re-raised as ``OSError``) so :func:`forget_backend_provenance`
    aborts. A missing file is still an empty dict — there is simply no row to delete.
    """
    try:
        with open(_pidfile_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise OSError(f"app-backend pidfile is corrupt: {exc}") from exc


def _write_pidfile(data: dict[str, dict[str, Any]]) -> bool:
    # Atomic temp-file + rename (fsync): the whole point of the pidfile is to
    # survive a gateway SIGKILL, so a non-atomic open("w") that truncates first
    # would leave an empty/partial file if the kill lands mid-write.
    #
    # Returns whether the write LANDED. A caller that merely persists best-effort
    # (record on spawn, lenient forget on a stop that restores on refusal) ignores
    # it; a caller that must not proceed on an unconfirmed delete — uninstall and
    # update, where a retained row re-attributes an old-code survivor — checks it
    # and aborts. ENOSPC/EDQUOT is the real shape: unlink succeeds while
    # atomic_write fails, so the in-memory row is gone but the on-disk one stays.
    try:
        atomic_write(_pidfile_path(), json.dumps(data), fsync=True)
        return True
    except OSError as exc:
        logger.debug("Could not write app-backend pidfile: %s", exc)
        return False


def _record_app_pid(
    app_name: str, pid: int, port: int, spawn_instance: str | None = None
) -> str | None:
    """Persist a spawned backend's identity for the startup stale-reap. Never raises.

    *spawn_instance* is the per-spawn ``KIROCREW_SPAWN_INSTANCE`` stamped on the
    backend's environment and inherited by its whole tree. It is what lets the
    reap vouch the group's MEMBERS once the leader itself is gone; a row written
    by an older build carries none, and the reap then declines to touch that
    group rather than aim a signal at a bare (possibly recycled) group number.
    """
    if pid <= 0:
        return None
    start_time: str | None = None
    _recorded = False
    try:
        # Compute start_time BEFORE taking the lock: the probe is slow on the
        # platforms that cannot answer from memory (a `ps` spawn on macOS, an
        # OpenProcess round trip on Windows), and holding _pidfile_lock across
        # that IO would serialize concurrent enable/stop/uninstall ops behind
        # it. Mirrors the reap path's validate-lock-free / store-under-lock
        # discipline.
        start_time = _proc_start_time(pid)
        with _pidfile_lock:
            data = _read_pidfile()
            entry: dict[str, Any] = {"pid": pid, "start_time": start_time, "port": port}
            if spawn_instance:
                entry["spawn_instance"] = spawn_instance
            data[app_name] = entry
            _recorded = _write_pidfile(data)
    except Exception as exc:  # noqa: BLE001 — persistence must never break a spawn
        logger.debug("Could not record app pid for %s: %s", app_name, exc)
    # A fresh spawn we launched owns the row, so the stale row a quarantine
    # guards against is overwritten and cannot be adopted -- lift any hold.
    # Outside the lock: clear_backend_quarantine re-takes the non-reentrant lock.
    if _recorded:
        clear_backend_quarantine(app_name)
    return start_time


def _forget_app_pid(app_name: str) -> dict[str, Any] | None:
    """Drop an app's pidfile entry and return it (called when no process identity is tracked).

    The removed row is handed BACK so a caller that must undo the removal can. A stop
    drops the row inside its lifecycle transition, before it signals anything, and it
    can then REFUSE and restore tracking; the row is where an adopted backend's
    provenance is read from, so leaving it dropped makes every retry unable to
    attribute the listener it is trying to stop.
    """
    try:
        with _pidfile_lock:
            data = _read_pidfile()
            removed = data.pop(app_name, None)
            if removed is not None:
                _write_pidfile(data)
            return removed if isinstance(removed, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not forget app pid for %s: %s", app_name, exc)
        return None


def _forget_app_pid_if(app_name: str, pid: int, start_time: str | None) -> dict[str, Any] | None:
    """Drop a pidfile row only if it still identifies the expected process.

    Returns the removed row, or ``None`` when the row stayed, for the same
    reversibility reason as :func:`_forget_app_pid`.
    """
    try:
        with _pidfile_lock:
            data = _read_pidfile()
            entry = data.get(app_name)
            if (
                isinstance(entry, dict)
                and entry.get("pid") == pid
                and entry.get("start_time") == start_time
            ):
                data.pop(app_name, None)
                _write_pidfile(data)
                return entry
            return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not conditionally forget app pid for %s: %s", app_name, exc)
        return None


def _restore_app_pid(app_name: str, row: dict[str, Any]) -> bool:
    """Put back a row a refused stop removed, unless the name has been re-recorded.

    Deliberately ``setdefault`` and not an overwrite: between the removal and the
    restore a fresh spawn can have recorded its own identity, and replacing that with
    the older row would aim both the stale-reap and adoption provenance at a process
    that is gone. Never raises -- a restore that cannot happen leaves the retry no
    worse off than before this function existed.

    Returns whether the row is durably present after the call: ``True`` when it was
    written, or when a fresh spawn already holds the name (``setdefault`` left it in
    place); ``False`` when the write was swallowed (ENOSPC/EDQUOT) so the row did NOT
    land on disk. A caller that needs the restore to survive a gateway restart -- a
    refused stop under a withdrawn ceiling -- reads the ``False`` and takes a
    fail-closed backstop, rather than silently reporting a restore that never persisted.
    """
    try:
        with _pidfile_lock:
            data = _read_pidfile()
            if app_name in data:
                return True
            data[app_name] = row
            return _write_pidfile(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not restore app pid for %s: %s", app_name, exc)
        return False


def _unrevoke_app_pid(app_name: str, original: dict[str, Any] | None) -> None:
    """Undo a :func:`revoke_backend_provenance` stamp when its destructive step rolled back.

    :func:`_restore_app_pid` is ``setdefault`` — it refuses to touch a name already in
    the pidfile — so it cannot undo a revoke, because the revoked row is still THERE
    under the name. A failed uninstall rollback needs the opposite: the app is still
    installed, so its backend is legitimately the app's again and the ``revoked`` stamp
    (which makes adoption refuse it) must come off.

    *original* is what :func:`revoke_backend_provenance` returned: the pre-revoke row,
    or ``None`` when the revoke wrote a BARE tombstone over an absent row. With a real
    *original*, overwrite the live row with it ONLY when the live row is the REVOKED
    INCARNATION of that same spawn — same ``pid``/``start_time``/``spawn_instance``,
    differing only by the ``revoked`` flag. A row whose identity differs is a genuine
    SUCCESSOR a fresh spawn recorded between the revoke and this rollback, and clearing
    THAT would aim adoption and the reap at a process that is gone — so it is left
    untouched. One exception: when the live row is a BARE revoked tombstone (``revoked``
    with ``tombstone`` and no spawn identity), the update's own stop forgot *original*'s
    row before the revoke ran, so the revoke stamped a pid-less tombstone instead of
    *original*'s revoked incarnation; the tombstone is not a successor, so the rollback
    restores *original* over it. With *original* ``None`` the revoke created a bare
    tombstone where there was no row, so the rollback DELETES that tombstone (leaving a
    successor's real row if one appeared). Never raises: a rollback that cannot persist
    leaves the retry no worse off.
    """

    def _same_spawn(live: dict[str, Any]) -> bool:
        assert original is not None
        return all(live.get(k) == original.get(k) for k in ("pid", "start_time", "spawn_instance"))

    try:
        with _pidfile_lock:
            data = _read_pidfile()
            live = data.get(app_name)
            if original is None:
                # Bare tombstone case: delete it ONLY if the live row is still that
                # bare tombstone (no real spawn). A successor that recorded its own
                # identity meanwhile is left untouched.
                if isinstance(live, dict) and live.get("revoked") and live.get("tombstone"):
                    del data[app_name]
                else:
                    return
            elif not isinstance(live, dict):
                # The row was fully deleted after the revoke (an unusual interleaving):
                # restore the original so the still-installed app stays attributable.
                data[app_name] = dict(original)
            elif live.get("revoked") and live.get("tombstone"):
                # The revoke wrote a BARE tombstone, not the revoked incarnation of
                # *original*: the update's own stop forgot *original*'s row first, so
                # :func:`revoke_backend_provenance` found no row and stamped a pid-less
                # tombstone (and returned None, which the row-stop path drops — but the
                # rollback still holds the pre-stop row as *original*). _same_spawn can
                # never match that tombstone (it has no pid/start_time/spawn_instance),
                # so without this branch the recovery row would be lost and the still-
                # installed app's surviving backend refused by adoption. A bare
                # tombstone is never a real successor — a fresh spawn records its own
                # pid, not a tombstone — so overwriting it with *original* cannot clobber
                # a successor's record. Done under the lifecycle flock the caller holds.
                data[app_name] = dict(original)
            elif live.get("revoked") and _same_spawn(live):
                data[app_name] = dict(original)
            else:
                # A successor's own un-revoked row, or an unrelated identity — leave it.
                return
            _write_pidfile(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not un-revoke app pid for %s: %s", app_name, exc)


def _forget_exited_leader_row(app_name: str, ap: AppProcess) -> None:
    """Drop an exited leader's pidfile row unless an adopted instance rests on it.

    Called once the whole restart sequence is OVER, never between its attempts. The
    row carries the spawn tree's instance token, and that token is the only thing
    that attributes a pre-fork worker or a detached child still holding the app's
    declared port, so a removal taken while the loop can still retry leaves
    :func:`_adoption_provenance` with no record on every later attempt: adoption
    refuses, the respawn returns nothing, and the loop retries forever against an
    orphan only an operator can clear.

    The ``_processes`` successor check runs under ``_lock``; the conditional pidfile
    delete runs AFTER ``_lock`` is released, under ``_pidfile_lock`` alone.

    ``_lock`` must NOT be held across the delete. This function runs on the backend
    supervisor THREAD, and the delete goes through ``_forget_app_pid_if`` ->
    ``_pidfile_lock``, a CROSS-PROCESS flock that waits up to 300s under contention;
    holding ``_lock`` across that wait stalls every on-loop ``list_app_processes()``
    (``/api/apps``) for the same window, up to a watchdog-driven gateway exit. The
    atomicity the old single-hold sought is already provided WITHOUT it: the delete
    is conditional on the exited leader's own ``pid`` + ``start_time``
    (:func:`_forget_app_pid_if` only pops a row that still matches both), and
    adoption's own write (:func:`_record_app_pid`) serializes under the SAME
    ``_pidfile_lock``. So a concurrent adoption either recorded a DIFFERENT identity
    (the conditional pop then leaves it untouched) or re-recorded the identical
    ``pid`` + ``start_time`` — which is the very same surviving OS process the row
    legitimately attributes, so the pid-matched pop is the correct outcome either
    way. The in-memory ``proc is None`` short-circuit below still skips the delete
    whenever a live-handle successor is already tracked.

    The check and the delete together run under ``app_backend_lifecycle_flock``, the
    lock a start holds across its whole spawn body. Adoption reuses the row it finds
    without writing a new one, so an adoption landing between an unlocked check and
    the delete would have its own provenance removed by the pid-matched pop. Under the
    flock a start either finished before the check (and the check sees what it
    tracked) or waits until the delete is done (and judges the row that is left). The
    flock is taken outside ``_lock`` and outside ``_pidfile_lock``, the order a start
    takes them in; this runs on the supervisor thread, never on the loop.

    An adopted instance is tracked with no handle of ours (``proc is None``) and its
    ownership is re-read from this row on every re-bind, so the row stays while that
    instance does. Any other outcome leaves the row stale and it goes: a fresh spawn
    has recorded its own identity, or nothing is tracked at all. The drop stays
    conditional on the exited leader's own pid and start instant, so a successor's row
    is never the one removed.
    """
    try:
        flock = app_backend_lifecycle_flock(app_name)
        flock.__enter__()
    except OSError as exc:
        # Keeping the row is the safe miss: it names a leader that has exited, so the
        # pid + start_time it carries attribute nothing that is still running.
        logger.warning(
            "App %s: could not take the lifecycle lock to drop the exited leader's "
            "spawn row; keeping it: %s",
            app_name,
            exc,
        )
        return
    try:
        with _lock:
            tracked = _processes.get(app_name)
            if tracked is not None and tracked.proc is None:
                return
        _forget_app_pid_if(app_name, ap.pid, ap.pid_start_time)
    finally:
        flock.__exit__(None, None, None)


def revoke_backend_provenance(app_name: str) -> dict[str, Any] | None:
    """DURABLY mark *app_name*'s spawn row revoked, confirming the stamp hit disk.

    The durable half of invalidation, run BEFORE the destructive step of an
    update/uninstall so the window where the old row is still live is closed by a
    record that survives a crash or restart — not by the in-memory quarantine set,
    which a restart loses. :func:`_adoption_provenance` refuses any ``revoked`` row,
    and the stamp rides on the SAME row the startup stale-reap retains when it cannot
    persist a removal, so the refusal holds across the restart that the in-memory set
    could not.

    Returns a COPY of the pre-revoke row so the UPDATE's rollback can restore the
    un-revoked record if the update then fails (a successful rollback means the old
    backend is legitimately the app's again). Returns ``None`` when there was no row:
    rather than leave the name unprotected, a BARE revoked tombstone is written and
    confirmed, because a stop of this app racing from another process can RESTORE a
    row after this returns — the tombstone makes that restore a no-op and keeps
    adoption refused. ``None`` tells a rollback to DELETE that tombstone (there was no
    row to restore); callers decide "a revoke committed" from the call returning
    without raising, not from a non-None result.

    CONFIRMS the stamp landed: the write goes out with ``fsync`` and is then RE-READ
    through :func:`_read_pidfile_strict`, because ``_write_pidfile`` swallows an
    ``OSError`` and would let a disk-full (ENOSPC/EDQUOT) revoke silently vanish. A
    stamp that cannot be confirmed raises :class:`PidfileRevokeFailed` so the caller
    REFUSES the destructive work rather than commit onto a row that would still
    attribute an old-code survivor after a restart.
    """
    try:
        with _pidfile_lock:
            data = _read_pidfile_strict()
            row = data.get(app_name)
            if isinstance(row, dict) and row.get("revoked"):
                # Already revoked (an earlier attempt landed the stamp but could not
                # confirm, or a retry): confirmed-revoked is the state we want.
                return dict(row)
            if isinstance(row, dict):
                original: dict[str, Any] | None = dict(row)
                stamped = {**row, "revoked": True}
            else:
                # No row to stamp — but "no row" is NOT safe to leave as-is. A tracked
                # stop of this same app, racing this uninstall from the gateway
                # process, drops the row and (when a detached child still serves)
                # RESTORES it afterwards via _restore_app_pid, whose only guard is
                # name-absence. If this uninstall returned here having written nothing,
                # that restore would resurrect an UN-revoked row and a same-name
                # reinstall would adopt the removed app's listener. So persist a BARE
                # revoked tombstone even with no row: its presence makes the later
                # restore a no-op (name present) and _adoption_provenance refuses it
                # (revoked). ``original`` is None so a rollback DELETES the tombstone
                # rather than restoring a row that never existed.
                original = None
                stamped = {"revoked": True, "tombstone": True}
            data[app_name] = stamped
            if not _write_pidfile(data):
                raise PidfileRevokeFailed(
                    f"could not persist revocation of the spawn record for {app_name!r}"
                )
            # Re-read to CONFIRM the stamp reached disk: _write_pidfile reports False
            # on a swallowed OSError, but a short/partial write that still "succeeded"
            # must not be trusted to carry the stamp a restart will read.
            persisted = _read_pidfile_strict().get(app_name)
            if not (isinstance(persisted, dict) and persisted.get("revoked")):
                raise PidfileRevokeFailed(
                    f"revocation of the spawn record for {app_name!r} did not land on disk"
                )
            return original
    except PidfileRevokeFailed:
        raise
    except OSError as exc:
        raise PidfileRevokeFailed(
            f"could not read the spawn record for {app_name!r} to revoke it: {exc}"
        ) from exc


def forget_backend_provenance(app_name: str) -> dict[str, Any] | None:
    """Drop *app_name*'s spawn record, returning it so a caller can undo the drop.

    An app UPDATE replaces the code behind the name while reusing the port, and
    uninstall removes the app entirely. The pre-update/pre-removal row still vouches
    for whatever rebinds that port, so the post-update restart's adopt branch (or a
    later same-name install) would attribute a process running the OLD code. Called
    at the post-update, pre-restart seam, this invalidates the stale row so the new
    spawn records its own. The row is handed back so the UPDATE's rollback can
    restore it: if the update fails, the old backend is still legitimately the app's
    and its provenance must survive.

    CONFIRMS the delete reached disk, both directions. The read goes through
    :func:`_read_pidfile_strict`: ``_read_pidfile`` swallows an ``OSError``/corrupt
    file and returns ``{}``, so a read failure would make ``data.pop`` yield ``None``
    — indistinguishable from "no row" — and the caller would report a confirmed drop
    that never happened. And ``_forget_app_pid`` returns the popped in-memory row even
    when ``_write_pidfile`` swallowed an ``OSError`` — on ENOSPC/EDQUOT the row leaves
    memory while the on-disk copy stays. Either way a caller that reported success and
    restarted would adopt the survivor the stale row still attributes, so an
    unconfirmed read OR write raises :class:`PidfileDeleteFailed` and the caller aborts
    before restart.
    """
    _confirmed = False
    try:
        with _pidfile_lock:
            data = _read_pidfile_strict()
            removed = data.pop(app_name, None)
            if removed is None:
                _confirmed = True
            else:
                if not _write_pidfile(data):
                    # The delete did not land (ENOSPC/EDQUOT), so the stale row stays
                    # on disk and still vouches for whatever rebinds the port. The
                    # in-memory quarantine the caller sets is lost on restart, and a
                    # SIGTERM-survivor forked into the reaped group makes the startup
                    # reap RETAIN the row rather than re-drop it -- so after a restart
                    # nothing would refuse the old-code survivor. Stamp a DURABLE
                    # revoked flag onto the surviving row as a best-effort fallback:
                    # _adoption_provenance refuses any revoked row, and the flag rides
                    # on the row the reap retains, so the refusal survives the restart
                    # even when the full delete could not. Best-effort because the same
                    # disk-full may defeat this smaller write too; the raise below is
                    # the hard backstop that stops THIS restart regardless.
                    data[app_name] = {**removed, "revoked": True}
                    _write_pidfile(data)
                    raise PidfileDeleteFailed(
                        f"could not persist removal of the spawn record for {app_name!r}"
                    )
                _confirmed = True
    except PidfileDeleteFailed:
        raise
    except OSError as exc:
        raise PidfileDeleteFailed(
            f"could not read the spawn record for {app_name!r}: {exc}"
        ) from exc
    # With the stale row the quarantine guards against confirmed gone, a later
    # start cannot adopt an old-code survivor through it -- lift the hold.
    # Done OUTSIDE the lock: clear_backend_quarantine re-takes _pidfile_lock, which is
    # not reentrant. ``removed is None`` (no row) is still a confirmed-absent state.
    if _confirmed:
        clear_backend_quarantine(app_name)
    return removed if isinstance(removed, dict) else None


def capture_backend_provenance(app_name: str) -> dict[str, Any] | None:
    """Return a COPY of *app_name*'s spawn row without mutating the pidfile.

    An app UPDATE stops the backend before replacing files, and a tracked stop
    forgets the row. If the update then fails and rolls back, the restart re-captures
    a surviving detached child but :func:`_adoption_provenance` refuses it — the stop
    erased the record that attributes it — so the enabled app stays unreachable. The
    update captures the row with this BEFORE the stop and restores it (via
    :func:`_restore_app_pid`) before the rollback restart, so the survivor is
    attributable again. A copy, so a later mutation of the live dict cannot change
    what the caller holds.
    """
    try:
        with _pidfile_lock:
            row = _read_pidfile().get(app_name)
            return dict(row) if isinstance(row, dict) else None
    except Exception as exc:  # noqa: BLE001 — a read that fails just yields no row to restore
        logger.debug("Could not capture app provenance for %s: %s", app_name, exc)
        return None


def _quarantine_path() -> Path:
    # Sibling of the pidfile, same directory and lock discipline. Holds the set of
    # app names whose pre-update spawn record could NOT be confirmed deleted
    # (ENOSPC/EDQUOT), so a later start/adopt must refuse until the record is gone
    # -- otherwise the retained row vouches for an OLD-code survivor and the next
    # enable adopts it as the new version. Kept out of the pidfile dict so the
    # app->row contract the reap and adoption iterate stays untouched.
    return config_dir() / "app_backends.quarantine.json"


class QuarantineUnreadable(Exception):
    """The quarantine marker file EXISTS but could not be read or parsed.

    Distinct from the file being absent (FileNotFoundError -> an empty quarantine).
    A present-but-unreadable marker (EACCES / EIO / fd exhaustion / corrupt JSON)
    must fail CLOSED at the adoption gate: the marker may name an app whose stale
    spawn row would otherwise adopt an old-code survivor, and reading it as empty
    would silently defeat the quarantine. Raised by _read_quarantine so callers that
    gate on it can refuse; the read-modify-write callers swallow it best-effort.
    """


def _read_quarantine() -> dict[str, str]:
    try:
        with open(_quarantine_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        # Truly absent -> nothing is quarantined. This is the ENOSPC/EDQUOT case
        # where the marker write never landed; the in-memory set covers that
        # generation, so an empty on-disk read here is correct, not fail-open.
        return {}
    except (OSError, ValueError) as exc:
        # The marker EXISTS but cannot be read/parsed (EACCES / EIO / fd
        # exhaustion / corruption). Returning {} would silently defeat the gate,
        # so signal unreadability and let the adoption gate fail CLOSED.
        logger.warning("App-backend quarantine file present but unreadable (%s)", exc)
        raise QuarantineUnreadable(str(exc)) from exc


def quarantine_backend(app_name: str, reason: str) -> bool:
    """Mark *app_name* so a later start/adopt refuses until its record is confirmed gone.

    Called when an UPDATE's provenance invalidation could not be confirmed on disk
    (ENOSPC/EDQUOT): the stale row survives and still vouches for whatever rebinds the
    port, so a later enable would adopt an OLD-code survivor as the new version. The
    quarantine is the backstop the skipped restart alone does not give -- it blocks
    the LATER start too, not just the immediate one.

    The IN-MEMORY set is recorded FIRST and is authoritative for the running
    generation: it needs no disk write, so it holds under the very ENOSPC/EDQUOT that
    makes the on-disk marker (and the provenance delete) fail -- the case where the
    guard matters most. The on-disk marker is then written best-effort for durability
    across a gateway restart. A lost marker does not reopen the window: when a restart
    cannot persist its stale-reap row removal under that same ENOSPC/EDQUOT, the reap
    re-quarantines the affected names in memory for its own generation (see
    _reap_stale_app_backends), so the survivor the retained row vouches for stays
    unadoptable until a confirmed removal lands.
    Returns True -- the in-memory record always lands.
    """
    with _pidfile_lock:
        _quarantined_backends.add(app_name)
        try:
            data = _read_quarantine()
            data[app_name] = reason
            atomic_write(_quarantine_path(), json.dumps(data), fsync=True)
        except (
            Exception
        ) as exc:  # noqa: BLE001 -- in-memory record already holds for this generation
            logger.warning(
                "Backend quarantine for %s recorded in memory but not persisted (%s); "
                "it still blocks adoption until this gateway restarts, and the restart "
                "reap drops the orphaned row",
                app_name,
                exc,
            )
    return True


def is_backend_quarantined(app_name: str) -> bool:
    """Whether *app_name* is quarantined pending a confirmed spawn-record deletion.

    Reads the IN-MEMORY backstop OR the on-disk marker: either being set refuses
    adoption. The in-memory set is what makes the guard fail CLOSED under the
    ENOSPC/EDQUOT that defeats the file -- the quarantine landed in memory even when
    the marker write failed, so a later enable in the same generation still refuses to
    adopt the old-code survivor the retained row vouches for. A marker that is PRESENT
    but unreadable (EACCES / EIO / fd exhaustion / corruption, raised as
    QuarantineUnreadable) also fails CLOSED -- it may name this app, and reading it as
    empty would let a retained row adopt an old-code survivor past the gate.
    """
    with _pidfile_lock:
        if app_name in _quarantined_backends:
            return True
        try:
            return app_name in _read_quarantine()
        except QuarantineUnreadable:
            # The marker file EXISTS but cannot be read: fail CLOSED. It may name
            # this app, and reading it as empty would let a retained spawn row
            # adopt an old-code survivor past the gate. Refuse adoption until the
            # marker is readable (and then explicitly cleared on a confirmed delete).
            logger.warning(
                "Treating %s as quarantined: the quarantine marker is present but unreadable",
                app_name,
            )
            return True


def clear_backend_quarantine(app_name: str) -> None:
    """Lift *app_name*'s quarantine once its spawn record is confirmed gone.

    Called on a CONFIRMED forget_backend_provenance (the stale row the quarantine
    guarded against is deleted) and on a fresh spawn recording its OWN identity
    (_record_app_pid: a fresh row vouches for the new backend, not the old survivor).
    Clears both the in-memory backstop and the on-disk marker. Idempotent; the disk
    clear is best-effort -- a clear that cannot persist leaves the marker, which only
    costs an extra refusal, never a wrong adoption.
    """
    with _pidfile_lock:
        _quarantined_backends.discard(app_name)
        try:
            data = _read_quarantine()
            if app_name not in data:
                return
            del data[app_name]
            atomic_write(_quarantine_path(), json.dumps(data), fsync=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not clear backend quarantine for %s: %s", app_name, exc)


def _adoption_provenance(app_name: str, owners: list[int]) -> tuple[bool, str]:
    """Whether EVERY pid in *owners* is attributable to this gateway's spawn for *app_name*.

    Adoption otherwise keys on two facts that say nothing about the code the
    listener runs: the port the manifest declares, and a health answer on it. A
    backend that outlives its app's uninstall and rebinds that port satisfies both,
    so the next install that happens to use the same app name adopts it -- the
    gateway then addresses a process the install did not place as that app's
    backend, reports its health as the app's, and aims stop at its PIDs.

    The record that closes it already exists: every backend this gateway spawns is
    written to the app pidfile as ``pid`` + ``start_time`` + per-spawn
    ``spawn_instance``. Attribution asks whether every captured owner matches that
    record; a survivor of an earlier install that rebound the port is not in it, so
    it is refused and the misattribution the issue describes does not happen.

    Two routes attribute ONE owner, and either is enough for that owner:

    ``leader`` -- the owner IS the recorded pid and its live start instant still
    equals the recorded one. A pid plus a start instant names one process for good,
    so this route answers on every platform, and it attributes that pid alone.

    ``tree`` -- the owner's exec-time environment carries the recorded
    ``spawn_instance``. The whole spawn tree inherits that token, so this route
    reaches a pre-fork worker or a detached child still holding the port after its
    leader has exited. It reads ``/proc/<pid>/environ``, which exists on Linux
    alone; :func:`group_vouching_available` reports that, and the reason names it
    so an operator can tell "not ours" from "this host cannot see".

    EVERY owner must clear a route, because every owner the caller captured enters
    the managed set and is signalled at stop. A same-UID ``SO_REUSEPORT`` co-binder
    lands in the same dispatch tier as the real backend, so attributing the set from
    one match would hand stop an unrelated process to terminate -- and the start-time
    token stop re-checks was captured at the same moment, so it confirms that
    bystander rather than excluding it.

    FAILS CLOSED, uniformly: no recorded spawn, an unreadable pidfile, a row
    carrying neither usable identity, an empty owner set, and any owner no route
    attributes all return ``False``. Refusing costs a start on a port the gateway
    does not own, which the caller reports; adopting on an unproven listener is the
    defect itself.

    Returns ``(attributed, reason)``. *reason* is a short phrase for the log and the
    audit trail on both verdicts.
    """
    if not owners:
        return False, "no owning pid to attribute"
    try:
        with _pidfile_lock:
            row = _read_pidfile().get(app_name)
    except Exception as exc:  # noqa: BLE001 — an unreadable record must refuse, not raise
        return False, f"app pidfile unreadable ({exc})"
    if not isinstance(row, dict):
        return False, "no spawn recorded for this app"
    if row.get("revoked"):
        # An update/uninstall that could not confirm the row's deletion stamped it
        # revoked (forget_backend_provenance). The row survives on disk and still
        # names a spawn instance a detached survivor inherited, so the tree route
        # below would otherwise vouch for an OLD-code listener after a restart. A
        # revoked row attributes nothing: the next clean spawn records its own,
        # un-revoked row and wins.
        return False, "the spawn record was revoked by an update or uninstall"
    try:
        recorded_pid = int(row.get("pid", 0))
    except (TypeError, ValueError):
        recorded_pid = 0
    recorded_start = row.get("start_time")
    instance = row.get("spawn_instance")
    if not isinstance(instance, str) or not instance:
        instance = ""
    vouchable = bool(instance) and group_vouching_available()
    unattributed: list[int] = []
    for pid in owners:
        if (
            recorded_pid > 0
            and recorded_start
            and pid == recorded_pid
            and _proc_start_time(pid) == recorded_start
        ):
            continue
        if vouchable and _env_spawn_instance(pid) == instance:
            continue
        unattributed.append(pid)
    if not unattributed:
        return True, f"every owner {owners} belongs to the recorded spawn"
    if not instance:
        why = "the row carries no spawn instance to vouch its tree with"
    elif not vouchable:
        why = "this host cannot read a process's spawn instance"
    else:
        why = "they were not placed by this gateway for this app"
    return False, (
        f"owner pid(s) {unattributed} are not the recorded spawn (pid {recorded_pid}): {why}"
    )


def retire_windows_app_tracking(pid: int, creation: int) -> None:
    """Retire only this incarnation's app rows, while its cleanup pin is held.

    This mandatory writer does not use the best-effort readers/writers: an
    unreadable file or failed atomic write must leave the cleanup receipt owed.
    No app name or caller callback is retained by the cleanup registry.
    """
    with _pidfile_lock:
        try:
            with open(_pidfile_path(), encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return
        if not isinstance(data, dict):
            raise OSError("Windows app tracking file is malformed")
        remove = [
            name
            for name, entry in data.items()
            if isinstance(entry, dict)
            and entry.get("pid") == pid
            and entry.get("start_time") == str(creation)
        ]
        if remove:
            for name in remove:
                del data[name]
            atomic_write(_pidfile_path(), json.dumps(data), fsync=True)
