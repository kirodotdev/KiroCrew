"""Who holds the memory: a host memory reading plus the biggest processes by RSS.

A loop-stall dump or a ``host memory is critical`` log line says THAT the host ran
out of memory. Neither says WHO used it, so a report of a host that froze and
rebooted cannot tell Kiro Crew's own runtimes and MCP servers from a build or a
browser. This module writes that answer down as plain text, in two places:

* the diagnostics bundle collects one at bundle time (``memory.txt``);
* the diag recorder writes one into ``<config_dir>/diag/memory-critical/`` when
  the memory posture turns critical, because by the time someone collects a
  bundle the host may already have rebooted and the moment is gone.

What it records about a process is its pid, the kernel's short executable name
(``/proc/<pid>/comm``, at most 15 characters) and its resident memory, plus the
Kiro Crew process kind when the process belongs to this install's family. It
never records a command line or an environment: an argument can carry a secret,
and the name and size are what the question needs.

The host-wide list is Linux only, read straight from ``/proc`` with no
subprocess. shortcut: macOS and Windows get the Kiro Crew family totals (macOS)
or a note saying the list is unavailable; add a ``ps``/libproc host list when a
report from those platforms needs it.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: How many of the host's processes the snapshot lists, biggest RSS first.
TOP_N = 15
#: The diag recorder's directory under the data home; ``recorder.DIR_NAME`` is
#: this value, so the bundle reads the folder the recorder writes.
DIAG_DIR_NAME = "diag"
#: Directory under the diag recorder's directory that holds critical snapshots.
CRITICAL_DIR_NAME = "memory-critical"
CRITICAL_PREFIX = "memory-critical-"
CRITICAL_SUFFIX = ".txt"
#: Critical snapshots kept on disk; older ones are deleted when a new one lands.
KEEP_CRITICAL = 5
#: A posture that flaps around the critical line writes at most one snapshot
#: per this many seconds.
CRITICAL_MIN_GAP_SECS = 600.0

PROC_ROOT = Path("/proc")


def _page_size() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return 4096


def host_top_processes(
    proc_root: Path = PROC_ROOT, top: int = TOP_N
) -> list[tuple[int, str, int]] | None:
    """``[(pid, name, rss_kb)]`` for the *top* biggest processes, or None off Linux.

    Reads ``comm`` (the short name, never argv) and ``statm`` (resident pages)
    per pid. A process that exits mid-walk, or one whose files are unreadable, is
    skipped.
    """
    if not sys.platform.startswith("linux") and proc_root == PROC_ROOT:
        return None
    try:
        pids = [int(e.name) for e in proc_root.iterdir() if e.name.isdigit()]
    except OSError:
        return None
    page_kb = _page_size() // 1024
    rows: list[tuple[int, str, int]] = []
    for pid in pids:
        base = proc_root / str(pid)
        try:
            resident_pages = int((base / "statm").read_text(encoding="ascii").split()[1])
            name = (base / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except (OSError, ValueError, IndexError):
            continue
        rows.append((pid, name[:32] or "?", resident_pages * page_kb))
    rows.sort(key=lambda row: (-row[2], row[0]))
    return rows[:top]


def running_gateway_pid() -> int | None:
    """The live gateway's pid from ``gateway.lock``, for a caller outside the gateway.

    None when no gateway holds the lock or the lock cannot be read. Uses
    :func:`kiro_crew.gateway_lock.lock_holder`, the same oracle ``kirocrew perf``
    and ``kirocrew doctor`` use; it names only a live holder.
    """
    try:
        from kiro_crew import gateway_lock
        from kiro_crew.config.loader import config_dir

        return gateway_lock.lock_holder(config_dir()).pid
    except Exception:  # noqa: BLE001 - a snapshot never raises into its caller
        logger.debug("memory snapshot: gateway lock probe failed", exc_info=True)
        return None


def _family_kinds(
    proc_root: Path, gateway_pid: int | None = None
) -> tuple[dict[int, tuple[str, int | None]], list[str]]:
    """``{pid: (kind, rss_kb)}`` for this install's process family, and any gaps.

    The family and its kinds come from :func:`kiro_crew.diag.procs.scan`, the
    reaper's own rules, so "is this ours" has one answer everywhere. With no
    *gateway_pid* the scan roots the family at this process, which is right
    only inside the gateway.
    """
    try:
        from kiro_crew.diag import procs

        roster = procs.scan(proc_root=proc_root, gateway_pid=gateway_pid)
    except Exception as exc:  # noqa: BLE001 - a snapshot never raises into its caller
        logger.debug("memory snapshot: family scan failed", exc_info=True)
        return {}, [f"family scan failed: {type(exc).__name__}"]
    return {pid: (node.kind, node.rss_kb) for pid, node in roster.nodes.items()}, list(
        roster.degraded
    )


def _mb(kb: int | None) -> str:
    return "?" if kb is None else f"{kb / 1024:.0f}"


def _gb(value: float | None) -> str:
    return "?" if value is None else f"{value / 1024:.1f} GB"


def render(
    proc_root: Path = PROC_ROOT,
    *,
    reason: str = "",
    now: float | None = None,
    family_fn: Callable[[Path], tuple[dict[int, tuple[str, int | None]], list[str]]] | None = None,
    posture_fn: Callable[[], dict] | None = None,
    top: int = TOP_N,
    find_gateway: Callable[[], int | None] | None = None,
) -> str:
    """The snapshot as plain text. Never raises; a failed read prints as ``?``.

    A caller outside the gateway (``kirocrew doctor --bundle``) passes
    *find_gateway*, which returns the running gateway's pid or None. The family
    is then rooted at that pid, and with no gateway found the snapshot says so
    instead of listing the caller's own process tree as Kiro Crew's.
    """
    from kiro_crew.diag import recorder

    stamp = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    lines = [f"collected_at: {stamp.isoformat()}"]
    if reason:
        lines.append(f"reason: {reason}")

    mem = recorder._read_meminfo(proc_root)
    lines.append(
        "host memory: total {} available {} swap used {} of {}".format(
            _gb(mem.get("mem_total_mb")),
            _gb(mem.get("mem_available_mb")),
            _gb(mem.get("swap_used_mb")),
            _gb(mem.get("swap_total_mb")),
        )
    )
    try:
        posture = (posture_fn or recorder._read_posture)()
    except Exception:  # noqa: BLE001
        posture = {}
    lines.append(
        "posture: {} (available {} GB, tight below {} GB, critical at or below {} GB)".format(
            posture.get("posture", "?"),
            posture.get("available_gb", "?"),
            posture.get("pressure_gb", "?"),
            posture.get("critical_gb", "?"),
        )
    )

    family: dict[int, tuple[str, int | None]] = {}
    degraded: list[str] = []
    if find_gateway is None:
        family, degraded = (family_fn or _family_kinds)(proc_root)
    else:
        try:
            gateway_pid = find_gateway()
        except Exception:  # noqa: BLE001
            gateway_pid = None
        if gateway_pid is None:
            degraded = ["no running gateway found, so its processes are not listed"]
        else:
            family, degraded = _family_kinds(proc_root, gateway_pid)
    lines.append("")
    host = host_top_processes(proc_root, top)
    if host is None:
        lines.append(f"top processes by RSS: not available on {sys.platform}")
    else:
        lines.append("top processes by RSS (name only, never arguments):")
        lines.append(f"  {'pid':>8}  {'rss_mb':>8}  {'kiro_crew':<12}  name")
        for pid, name, rss_kb in host:
            kind = family.get(pid, ("", None))[0]
            lines.append(f"  {pid:>8}  {_mb(rss_kb):>8}  {kind:<12}  {name}")

    lines.append("")
    lines.append("Kiro Crew processes by kind:")
    totals: dict[str, list[int]] = {}
    for kind, member_kb in family.values():
        count_and_kb = totals.setdefault(kind, [0, 0])
        count_and_kb[0] += 1
        count_and_kb[1] += member_kb or 0
    if not totals:
        lines.append("  none found")
    for kind, (count, kb) in sorted(totals.items(), key=lambda item: -item[1][1]):
        lines.append(f"  {kind:<12}  count {count:>4}  rss_mb {_mb(kb):>8}")
    if totals:
        lines.append(
            "  {:<12}  count {:>4}  rss_mb {:>8}".format(
                "total",
                sum(c for c, _ in totals.values()),
                _mb(sum(kb for _, kb in totals.values())),
            )
        )
    for note in degraded:
        lines.append(f"  note: {note}")
    return "\n".join(lines) + "\n"


_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(_STAMP_FORMAT)


def critical_dir(diag_dir: Path) -> Path:
    return diag_dir / CRITICAL_DIR_NAME


def newest_critical_ts(diag_dir: Path) -> float | None:
    """When the newest critical snapshot on disk was written, from its name.

    Lets the 10-minute gap hold across gateway restarts: a gateway that keeps
    respawning while memory stays critical would otherwise write one per start
    and rotate out the snapshot from the moment memory first ran out.
    """
    newest = latest_critical(diag_dir, 1)
    if not newest:
        return None
    stamp = newest[0].name[len(CRITICAL_PREFIX) : -len(CRITICAL_SUFFIX)]
    try:
        whole_second = datetime.strptime(stamp, _STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    # The name drops the fraction; round up so the gap is never a second short.
    return whole_second.timestamp() + 1.0


def latest_critical(diag_dir: Path, limit: int) -> list[Path]:
    """The newest *limit* critical snapshots, newest first. Never follows a symlink."""
    folder = critical_dir(diag_dir)
    try:
        if folder.is_symlink() or not folder.is_dir():
            return []
        found = [
            p
            for p in folder.iterdir()
            if p.name.startswith(CRITICAL_PREFIX)
            and p.name.endswith(CRITICAL_SUFFIX)
            and not p.is_symlink()
            and p.is_file()
        ]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.name, reverse=True)[:limit]


def write_critical(diag_dir: Path, text: str, now: float) -> Path | None:
    """Write *text* as a new critical snapshot, keep the newest few. None on failure."""
    from kiro_crew.atomic_write import atomic_write, fsync_dir

    folder = critical_dir(diag_dir)
    name = CRITICAL_PREFIX + _stamp(now) + CRITICAL_SUFFIX
    try:
        if folder.is_symlink():
            return None
        created = not folder.is_dir()
        folder.mkdir(parents=True, exist_ok=True)
        if created:
            fsync_dir(folder.parent, best_effort=True)
        path = folder / name
        # Synced, file and directory: the host this is written for is about to
        # freeze, and a hard reset must not take the snapshot with it.
        atomic_write(path, text, fsync=True, restrict_to_owner=True)
        fsync_dir(folder, best_effort=True)
    except OSError:
        logger.debug("memory snapshot: could not write %s", name, exc_info=True)
        return None
    # The one just written always stays, even when a clock that stepped back
    # gave it a name older than the others.
    others = [p for p in latest_critical(diag_dir, 10_000) if p != path]
    for stale in others[KEEP_CRITICAL - 1 :]:
        try:
            stale.unlink()
        except OSError:
            pass
    return path
