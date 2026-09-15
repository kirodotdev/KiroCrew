"""In-flight markers for cron runs -- the record a hard exit leaves behind.

A cron run writes its ``last_run_ts``, its history row and its status in the
``finally`` of the run task. A gateway that the loop-stall watchdog hard-exits
(``os._exit`` from faulthandler's thread) runs no ``finally``: the store shows
the job as never having fired, so on the next boot it is due again, fires
again, and stalls again. Nothing on disk said WHICH job was running when the
process died, so ``kirocrew doctor`` could show the stack but not the job, and
nothing could break the loop.

This module is that record. ``write_marker`` drops one small JSON file per
running job under ``<cron dir>/cron-running/`` when the run starts executing,
and ``clear_marker`` removes it when the run ends by any path that runs
``finally``. A marker that is still there for a PID that is no longer alive is
therefore exactly "this job was in flight when that gateway died", with no
inference from timestamps or schedules. The stall attribution
(:mod:`kiro_crew.stall_attribution`) joins it to the crash dump by PID; the
cron service's boot-time breaker pauses the job it names; ``doctor`` prints it.

Every function here is synchronous filesystem I/O and is called off the event
loop (``asyncio.to_thread`` from the run task, a worker from ``start()``, the
doctor CLI). Writes are best-effort: a marker failure must never fail a run.

**These files are evidence the breaker acts on, so this module owns every byte
that reaches them and refuses anything it did not write.** The directory is on
``security._SENSITIVE_HOME_DIRS`` beside ``crons.json`` and ``cron-history``, so
an agent's file tools and shell cannot forge or edit a marker. Under that fence
the I/O here is still written for a hostile filesystem, because a leaf that was
planted BEFORE the fence existed must not be followed either: reads open
``O_NOFOLLOW`` and refuse anything that is not a single-linked regular file
(a symlink read lands wherever it points, and a ``read_text`` on a FIFO blocks
for ever -- on the worker the cron service awaits during ``start()``, which
would hang the scheduler instead of arming it), and writes go through
``atomic_write(restrict_to_owner=True)``, whose ``mkstemp`` temp name cannot be
pre-planted and whose linked-parent refusal stops a redirected write from
landing the payload on a keystone file.
"""

from __future__ import annotations

import json
import logging
import os
import stat as _stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.dashboard.crash_dump_store import current_process_identity, pid_identity_alive
from kiro_crew.platform_compat import is_link_or_junction

logger = logging.getLogger(__name__)

#: Directory (under the cron store's base directory) holding one file per
#: running job.
RUNNING_DIR_NAME = "cron-running"
_MARKER_SUFFIX = ".json"
#: A marker is never read past this size: it is written by this module and
#: holds five short fields, so anything larger is not a marker.
_MARKER_MAX_BYTES = 4096
#: Name of the file recording the newest dump the loop-stall breaker has already
#: reached a verdict on, so one crash pauses its job once: a dump stays on disk
#: for a week and a job the operator resumed must not be re-paused on the next
#: boot.
BREAKER_CLAIM_FILE = ".loop-stall-breaker"
#: The claim file holds one dump filename; anything longer is not a claim.
_CLAIM_MAX_BYTES = 512
#: The verdict the boot-time breaker reached from the abandoned markers, kept
#: beside them after they are swept: the doctor and the restart notification run
#: AFTER that sweep and read the same evidence, so without this record the
#: ambiguous cases (several runs in flight) would be reported to nobody. No
#: ``.json`` suffix, so :func:`read_markers` never mistakes it for a marker.
ATTRIBUTION_RECORD_NAME = ".loop-stall-attribution"
_RECORD_MAX_BYTES = 256 * 1024
#: Names in the record are display text; a bound keeps ~1 000 in-flight runs
#: inside ``_RECORD_MAX_BYTES``.
_RECORD_NAME_MAX_CHARS = 128

#: Absent on Windows, where ``getattr`` yields 0 and the flag is a no-op. The
#: ``S_ISREG`` + ``st_nlink`` check on the OPENED descriptor is what carries the
#: refusal there, and it is pinned to the inode actually read rather than to a
#: name that could be swapped after the check.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
#: Opening a FIFO for reading blocks until a writer arrives; ``O_NONBLOCK``
#: makes that open return instead, so the type check below gets to refuse it.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _read_own_file(path: Path, max_bytes: int) -> str | None:
    """Read *path* only if it is a plain file this module could have written.

    ``None`` for anything else -- a symlink, a FIFO, a device, a hardlinked
    inode, an over-long file, an unreadable one. The type check runs on the
    descriptor that is then read (``fstat``, not ``lstat`` + open by name), so
    there is no window in which the name is swapped between check and use.
    """
    try:
        # ``O_NOFOLLOW`` is the race-free refusal, but Windows has no such flag
        # (it opens as 0), so the name is also checked first: a link at the
        # leaf is refused everywhere, and on POSIX the flag closes the window
        # between this check and the open.
        if is_link_or_junction(path):
            return None
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            return None
        if st.st_size > max_bytes:
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = -1  # fdopen owns it now
            return fh.read(max_bytes + 1).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


@dataclass(frozen=True)
class RunningMarker:
    """One in-flight (or abandoned) cron run."""

    job_id: str
    name: str
    started_at: float
    pid: int
    path: Path
    #: The PID domain (host + PID namespace) and process start id recorded
    #: beside the PID, the same identity the crash dump header carries. A PID
    #: number alone is ambiguous across a container restart (the replacement is
    #: PID 1 like the one that died) and across PID reuse on one host. Every
    #: marker carries its domain -- one without it was not written by this
    #: module and is not read; ``pid_start`` is ``None`` where no start
    #: identity is readable.
    pid_domain: str
    pid_start: str | None = None

    def owner_alive(self) -> bool | None:
        """``True`` when the writing process is confirmed live here, ``False``
        when it is confirmed gone, ``None`` when its PID belongs to a domain this
        process cannot probe (the crash dump's own identity settles that case)."""
        return pid_identity_alive(self.pid, self.pid_domain, self.pid_start)

    def same_process(self, pid: int, pid_domain: str | None, start_id: str | None) -> bool:
        """Was this marker written by the process ``(pid, pid_domain, start_id)``
        names -- a dump header's owner? PID and PID domain must both match (a
        marker always carries its domain; a dump header without one predates
        it and joins nothing); the start id is compared when both sides
        recorded it."""
        if self.pid != pid or pid_domain is None or self.pid_domain != pid_domain:
            return False
        if self.pid_start is not None and start_id is not None and self.pid_start != start_id:
            return False
        return True


def running_dir(base_dir: Path) -> Path:
    return base_dir / RUNNING_DIR_NAME


def _readable_running_dir(base_dir: Path) -> Path | None:
    """The marker directory, or ``None`` when it is missing or is a LINK.

    Reads open each child ``O_NOFOLLOW``, but that guards the leaf only: a
    symlink or junction AT ``cron-running`` (planted before the directory was
    fenced, say) would make every child open resolve inside whatever it points
    at, and a marker forged there would name a job the breaker then pauses.
    Writers are already refused by ``atomic_write``'s linked-parent check; this
    is the readers' half of the same rule.
    """
    d = running_dir(base_dir)
    try:
        if is_link_or_junction(d) or not d.is_dir():
            return None
    except OSError:
        return None
    return d


def marker_path(base_dir: Path, job_id: str) -> Path:
    # Job ids are hex tokens minted by the service; a stray separator is
    # rejected rather than resolved into a path outside the marker directory.
    if not job_id or "/" in job_id or "\\" in job_id or job_id in (".", ".."):
        raise ValueError(f"not a cron job id: {job_id!r}")
    return running_dir(base_dir) / f"{job_id}{_MARKER_SUFFIX}"


def write_marker(base_dir: Path, job_id: str, name: str, started_at: float | None = None) -> None:
    """Record that *job_id* is executing in this process. Best-effort.

    A refusal (a redirecting parent link, a filesystem that cannot lock the file
    to its owner) leaves no marker, which is the safe direction: the breaker
    then names no job and pauses nothing.
    """
    try:
        path = marker_path(base_dir, job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        pid_domain, pid_start = current_process_identity()
        payload = {
            "job_id": job_id,
            "name": name,
            "started_at": float(started_at if started_at is not None else time.time()),
            "pid": os.getpid(),
            "pid_domain": pid_domain,
            "pid_start": pid_start,
        }
        atomic_write(path, json.dumps(payload), restrict_to_owner=True)
    except Exception:
        logger.debug("cron in-flight marker not written for %s", job_id, exc_info=True)


def clear_marker(base_dir: Path, job_id: str) -> None:
    """Remove *job_id*'s marker. Best-effort; a missing marker is not an error.

    Refused when ``cron-running`` is a link: ``unlink`` follows every component
    but the last, so the file removed would be the one INSIDE the link's target.
    """
    try:
        if _readable_running_dir(base_dir) is None:
            return
        marker_path(base_dir, job_id).unlink(missing_ok=True)
    except Exception:
        logger.debug("cron in-flight marker not cleared for %s", job_id, exc_info=True)


def _read_marker(path: Path) -> RunningMarker | None:
    raw = _read_own_file(path, _MARKER_MAX_BYTES)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return _marker_from(data, path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _marker_from(data: dict[str, Any], path: Path) -> RunningMarker:
    """A marker from its JSON. Raises for a shape this module never writes --
    including one without ``pid_domain``, so a file planted before the
    identity fields existed cannot name a job on the upgraded breaker."""
    start = data.get("pid_start")
    return RunningMarker(
        job_id=str(data["job_id"]),
        name=str(data.get("name") or ""),
        started_at=float(data.get("started_at") or 0.0),
        pid=int(data["pid"]),
        path=path,
        pid_domain=str(data["pid_domain"]),
        pid_start=str(start) if start is not None else None,
    )


def read_markers(base_dir: Path) -> list[RunningMarker]:
    """Every readable marker, oldest start first. Unparseable files are skipped."""
    d = _readable_running_dir(base_dir)
    if d is None:
        return []
    out: list[RunningMarker] = []
    for path in sorted(d.iterdir()):
        if path.suffix != _MARKER_SUFFIX or not path.name.endswith(_MARKER_SUFFIX):
            continue
        marker = _read_marker(path)
        if marker is not None:
            out.append(marker)
    out.sort(key=lambda m: m.started_at)
    return out


def abandoned_markers(base_dir: Path) -> list[RunningMarker]:
    """Markers whose owning process is gone: runs cut short by a hard exit."""
    return [m for m in read_markers(base_dir) if m.owner_alive() is False]


def claim_path(base_dir: Path) -> Path:
    return running_dir(base_dir) / BREAKER_CLAIM_FILE


def read_claim(base_dir: Path) -> str:
    """The dump name the breaker last reached a verdict on, or ``""``.

    An unreadable or forged-shape claim reads as "no claim", which costs at most
    one repeated pause attempt -- and that attempt is itself a no-op on a job
    that is already paused.
    """
    if _readable_running_dir(base_dir) is None:
        return ""
    raw = _read_own_file(claim_path(base_dir), _CLAIM_MAX_BYTES)
    return "" if raw is None else raw.strip()


def write_claim(base_dir: Path, dump_name: str) -> bool:
    """Record *dump_name* as settled. Returns whether the claim landed.

    Best-effort in that it never raises, but the caller must know: markers are
    swept only behind a written claim, so a boot that could not claim leaves
    them for the next one to try again (see :func:`read_claim`).
    """
    try:
        path = claim_path(base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, dump_name + "\n", restrict_to_owner=True)
        return True
    except Exception:
        logger.debug("loop-stall breaker claim not written", exc_info=True)
        return False


def _marker_payload(marker: RunningMarker) -> dict[str, object]:
    return {
        "job_id": marker.job_id,
        # A name is display text only; keep the record within what its reader
        # accepts even with many runs in flight.
        "name": marker.name[:_RECORD_NAME_MAX_CHARS],
        "started_at": marker.started_at,
        "pid": marker.pid,
        "pid_domain": marker.pid_domain,
        "pid_start": marker.pid_start,
    }


def record_attribution(
    base_dir: Path,
    dump_name: str,
    candidates: list[RunningMarker],
    unrelated: list[RunningMarker],
) -> bool:
    """Keep what the abandoned markers said about *dump_name* before they go.

    Written by the breaker just before :func:`sweep_abandoned_markers`, so a
    later reader of the same dump -- ``kirocrew doctor``, the restart
    notification -- sees the candidates the breaker saw instead of an empty
    directory. One record, for the newest dump only: a newer dump replaces it.
    Written like a marker, since the breaker trusts it the same way, and sized
    for its reader: the ``unrelated`` list is trimmed until the serialized
    record fits ``_RECORD_MAX_BYTES`` (candidates are never dropped). Returns
    whether a record the reader will accept is on disk; the caller sweeps only
    when it is.
    """
    try:
        path = running_dir(base_dir) / ATTRIBUTION_RECORD_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "dump": dump_name,
            "candidates": [_marker_payload(m) for m in candidates],
            "unrelated_abandoned": [_marker_payload(m) for m in unrelated],
        }
        text = json.dumps(payload)
        while len(text.encode("utf-8")) > _RECORD_MAX_BYTES and payload["unrelated_abandoned"]:
            payload["unrelated_abandoned"] = payload["unrelated_abandoned"][:-1]
            text = json.dumps(payload)
        if len(text.encode("utf-8")) > _RECORD_MAX_BYTES:
            logger.warning("loop-stall attribution record too large to keep; markers retained")
            return False
        atomic_write(path, text, restrict_to_owner=True)
        return True
    except Exception:
        logger.debug("loop-stall attribution record not written", exc_info=True)
        return False


def read_recorded_attribution(
    base_dir: Path, dump_name: str
) -> tuple[list[RunningMarker], list[RunningMarker]] | None:
    """The recorded (candidates, unrelated) for *dump_name*, or None when the
    record is missing, unreadable, or about a different dump."""
    if _readable_running_dir(base_dir) is None:
        return None
    path = running_dir(base_dir) / ATTRIBUTION_RECORD_NAME
    raw = _read_own_file(path, _RECORD_MAX_BYTES)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        if data.get("dump") != dump_name:
            return None

        def _markers(key: str) -> list[RunningMarker]:
            out: list[RunningMarker] = []
            for item in data.get(key) or []:
                out.append(_marker_from(item, path))
            return out

        return _markers("candidates"), _markers("unrelated_abandoned")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def remove_markers(markers: "list[RunningMarker] | tuple[RunningMarker, ...]") -> int:
    """Delete the given markers. Returns how many went."""
    removed = 0
    for marker in markers:
        try:
            marker.path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            logger.debug("abandoned cron marker not removed: %s", marker.path, exc_info=True)
    return removed


def sweep_abandoned_markers(base_dir: Path) -> int:
    """Delete the abandoned markers once they have been read. Returns the count.

    Called by the cron service after its boot-time attribution has consumed them
    -- and after :func:`record_attribution` has kept what they said -- so a run
    interrupted long ago is not re-attributed on every boot while the verdict
    still reaches the doctor. A marker owned by a live PID (another gateway on
    the same data home, or this one) is never touched.
    """
    return remove_markers(abandoned_markers(base_dir))
