"""The diagnostic recorder — persists what today exists only in memory.

The gateway already measures itself. The adaptive controller samples host memory,
RSS, fds and loop lag every five seconds into
``AdaptiveController._samples``, a 60-entry ring; ``resource_status.probe`` reads
the memory posture on demand; ``stall_enrichment`` captures sockets once the loop
has already wedged. None of it survives the moment it was taken. So when a file
went to zero bytes at 21:50:44, nobody could say what the host or the gateway
were doing at that second — not because the data was hard to collect, but because
nothing wrote it down.

This module writes it down. One JSON line every
``KIROCREW_DIAG_SAMPLE_SECS`` (default 30) to
``<config_dir>/diag/snapshots-YYYYMMDD.jsonl``, kept for
``KIROCREW_DIAG_RETAIN_DAYS`` (default 7). About 2880 rows and ~1.7 MB a day.
On by default, because a recorder that has to be switched on before the incident
is a recorder that is off during every incident; ``KIROCREW_DIAG_RECORDER=0``
turns it off.

Three rules shape the implementation, each paid for by something that has
already gone wrong in this codebase:

* **The periodic path spawns nothing and reads no bytes it does not need.** No
  ``psutil`` (not in the venv), no subprocess (the rule
  :mod:`kiro_crew.dashboard.stall_enrichment` follows — forking from a sick
  process is riskier than reading procfs), and watched config files are read as
  ``stat`` metadata only, never opened. ``.env`` and the vault are never read.
* **Sampling happens off the event loop.** Every sample runs through
  ``asyncio.to_thread``. A diagnostic that adds loop lag would corrupt the
  loop-lag figure it reports, and on a busy host it would trip the very watchdog
  it exists to explain.
* **It is bounded, and it says when it backed off.** A sample must cost under
  20 ms; three consecutive samples over 200 ms drop the cadence to 60 s with one
  WARNING. The per-row ``cost_ms`` makes that visible rather than something a
  reader has to infer from gaps in the series.

What the recorder measures itself is the host and its own process. Everything
that needs gateway state — open sessions, live loops, subagent counts, the
adaptive cap, SEL rows in the window — arrives through
:meth:`Recorder.register_source`, registered by the boot wiring. That keeps this
module free of dashboard imports (it starts on the boot path, where an import
cycle is fatal) and lets a test drive a full sample with no gateway at all. The
process-family roster plugs into the same hook.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import stat
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

ENV_ENABLED = "KIROCREW_DIAG_RECORDER"
ENV_SAMPLE_SECS = "KIROCREW_DIAG_SAMPLE_SECS"
ENV_RETAIN_DAYS = "KIROCREW_DIAG_RETAIN_DAYS"

DEFAULT_SAMPLE_SECS = 30.0
DEFAULT_RETAIN_DAYS = 7

#: Target cost of one sample. Not enforced — exceeding it is recorded, not
#: refused, because a single slow sample during a storm is exactly the row worth
#: having.
SAMPLE_BUDGET_MS = 20.0
#: Cost above which a sample counts toward the back-off streak.
SAMPLE_SLOW_MS = 200.0
#: Consecutive slow samples before the cadence drops.
SAMPLE_SLOW_STREAK = 3
#: Cadence after back-off.
BACKOFF_SAMPLE_SECS = 60.0

#: Heartbeat cadence. Fine enough to see a one-second stall (the threshold
#: :mod:`kiro_crew.diag.threads` captures at) and four wakeups a second is far
#: below the gateway's existing 5 s watchdog heartbeat in cost.
HEARTBEAT_SECS = 0.25

#: Output cap for one :meth:`Recorder.query` answer, per spec. Cut on a row
#: boundary with a cursor, never mid-row: half a JSON object is not a smaller
#: answer, it is an unparseable one.
QUERY_BYTE_CAP = 64 * 1024

#: How many distinct numeric field names one answer will tally.
#:
#: The page is byte-capped, but the stats block is not billed against that cap
#: and its size tracks distinct FIELD NAMES seen across the window rather than
#: rows returned. Rows come from a file under the data home, so the set of names
#: in them is not this reader's to bound: a row carrying a thousand one-off keys
#: would otherwise grow the answer a thousand entries, per row. Past the cap the
#: answer says so rather than growing.
STATS_FIELD_CAP = 256

#: Default query window when the caller names neither end.
DEFAULT_QUERY_RADIUS_SECS = 300.0

FILE_PREFIX = "snapshots-"
FILE_SUFFIX = ".jsonl"
DIR_NAME = "diag"

#: Filesystems whose fullness has broken this host before. ``/tmp`` is tmpfs
#: here (RAM-backed), so filling it is a memory event, not a disk event.
WATCHED_MOUNTS = ("/tmp", "/dev/shm")
#: tmpfs fullness that becomes an event.
TMPFS_EVENT_PCT = 80.0
#: Load per CPU that becomes an event.
LOAD_EVENT_FACTOR = 2.0

#: Config files watched for a rewrite. METADATA ONLY — ``stat`` never opens the
#: file, which is what keeps ``.env`` (a credential store) inside the rule that
#: fenced files are metadata, never bytes.
# Watched for metadata only, never opened. ``.env`` is deliberately absent: the
# sandbox hides it in every mode, and its size and modification time sitting in an
# agent-readable diagnostic leaf would describe a file the mask exists to keep out
# of reach, including when its secrets were last rotated.
WATCHED_CONFIG_FILES = ("config.json", "config.local.json")

_TRUTHY_OFF = frozenset({"0", "false", "no", "off"})

# Event kinds. A closed set, so a reader can filter on them.
EVENT_GATEWAY_START = "gateway_start"
EVENT_GATEWAY_STOP = "gateway_stop"
EVENT_LOOP_STALL = "loop_stall"
EVENT_ADAPTIVE_CAP_LOWERED = "adaptive_cap_lowered"
EVENT_MEMORY_POSTURE = "memory_posture_change"
EVENT_CONFIG_REWRITTEN = "config_rewritten"
EVENT_THRESHOLD = "threshold_crossed"
EVENT_PROC_BURST = "proc_burst"
EVENT_ORPHAN_APPEARED = "orphan_appeared"


def _pinned_dir_fd_supported() -> bool:
    """Whether snapshot writes and removals can share one pinned directory."""
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in getattr(os, "supports_dir_fd", set())
        and os.unlink in getattr(os, "supports_dir_fd", set())
        and os.listdir in getattr(os, "supports_fd", set())
    )


def _is_link_or_reparse_point(path: Path) -> bool:
    """Whether *path* is a link or a reparse point, judged without following it.

    ``os.lstat`` describes the entry itself, so a planted symlink, directory
    junction or other reparse point is reported as one rather than as whatever it
    points at. An absent path is not a link — the append is free to create it.
    Metadata that cannot be read at all is treated as a link, because a write
    whose target is unknown is the case this guard exists to refuse.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _finite(number: float, field: str) -> float:
    """*number* unless it is nan or an infinity.

    These survive every ordinary guard: ``nan <= 0`` is false, so a comparison
    check waves it through, and it then poisons whatever arithmetic it reaches --
    a window that matches nothing, or a sleep that raises after a thread has
    already started. Refusing it where it enters keeps that out of the interior.
    """
    if not math.isfinite(number):
        raise ValueError(f"{field}: expected a finite number, got {number!r}")
    return number


#: Suffixes accepted on a duration, in seconds.
_DURATION_UNITS: "dict[str, float]" = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _parse_duration(value: Any, field: str) -> float:
    """Seconds from a number, a numeric string, or a suffixed duration.

    The MCP tool schema documents this field as ``"5m"``, and a route hands over
    whatever the query string held, so the plain ``float()`` this replaces turned
    a documented input into an uncaught ValueError and a 500. The field name
    travels with the error so the caller is told WHICH value it was.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field}: expected a duration, got a boolean")
    if isinstance(value, (int, float)):
        return _finite(float(value), field)
    text = str(value).strip().lower()
    if not text:
        raise ValueError(f"{field}: expected a duration, got an empty value")
    scale = _DURATION_UNITS.get(text[-1])
    try:
        return float(text[:-1]) * scale if scale is not None else float(text)
    except ValueError:
        raise ValueError(
            f"{field}: expected seconds or a duration like 30s, 5m, 2h, 1d; got {value!r}"
        ) from None


def _parse_instant(value: Any, field: str) -> float:
    """A unix timestamp from a number, a numeric string, or an ISO 8601 instant.

    ISO 8601 is accepted with or without a zone; a naive value is read as UTC,
    because every timestamp this module writes is UTC and guessing local time
    would silently shift a window by the host's offset.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field}: expected a time, got a boolean")
    if isinstance(value, (int, float)):
        return _finite(float(value), field)
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field}: expected a time, got an empty value")
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"{field}: expected a unix timestamp or an ISO 8601 time; got {value!r}"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _env_flag_on(env: dict[str, str], name: str, default: bool = True) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in _TRUTHY_OFF


def _env_float(env: dict[str, str], name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(env[name])
    except (KeyError, TypeError, ValueError):
        return default
    if value != value:  # NaN never orders correctly against a clamp
        return default
    return min(high, max(low, value))


def _env_int(env: dict[str, str], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(env[name]))
    except (KeyError, OverflowError, TypeError, ValueError):
        return default
    return min(high, max(low, value))


# ── Host reads ───────────────────────────────────────────────────────────────


def _read_loadavg(procfs: Path) -> dict[str, Any]:
    """Load averages. ``None`` where the host does not publish them."""
    try:
        parts = (procfs / "loadavg").read_text(encoding="utf-8").split()
        return {
            "load1": float(parts[0]),
            "load5": float(parts[1]),
            "load15": float(parts[2]),
        }
    except (OSError, IndexError, ValueError):
        pass
    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is not None:
        try:
            one, five, fifteen = getloadavg()
            return {"load1": one, "load5": five, "load15": fifteen}
        except OSError:
            pass
    return {"load1": None, "load5": None, "load15": None}


def _read_meminfo(procfs: Path) -> dict[str, Any]:
    """Available memory and swap use in MB, from ``meminfo``'s kB values.

    ``MemAvailable`` rather than ``MemFree``: free memory excludes reclaimable
    page cache and so understates what a new process can actually get, which is
    the figure every consumer here cares about.
    """
    blank: dict[str, Any] = {
        "mem_available_mb": None,
        "mem_total_mb": None,
        "swap_used_mb": None,
        "swap_total_mb": None,
    }
    try:
        text = (procfs / "meminfo").read_text(encoding="utf-8")
    except OSError:
        return blank
    values: dict[str, float] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if not fields:
            continue
        try:
            values[key] = float(fields[0]) / 1024.0  # kB -> MB
        except ValueError:
            continue
    swap_total = values.get("SwapTotal")
    swap_free = values.get("SwapFree")
    return {
        "mem_available_mb": _round(values.get("MemAvailable")),
        "mem_total_mb": _round(values.get("MemTotal")),
        "swap_used_mb": (
            _round(swap_total - swap_free)
            if swap_total is not None and swap_free is not None
            else None
        ),
        "swap_total_mb": _round(swap_total),
    }


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _read_mount_usage(path: str, statvfs: "Callable[[str], Any] | None") -> dict[str, Any]:
    """Usage of one filesystem, or null fields when it cannot be measured.

    *statvfs* is ``None`` on Windows, which has no ``os.statvfs`` at all — so
    this reports "not measured" there rather than guessing, the same contract
    every other platform-specific field in a row follows. The watched mounts are
    POSIX paths anyway.
    """
    if statvfs is None:
        return {"total_mb": None, "used_mb": None, "used_pct": None}
    try:
        st = statvfs(path)
    except (OSError, ValueError):
        return {"total_mb": None, "used_mb": None, "used_pct": None}
    frsize = st.f_frsize or st.f_bsize
    total = st.f_blocks * frsize
    # f_bavail (available to an unprivileged process), not f_bfree: the reserved
    # blocks a root-only process could still use are not headroom for the
    # gateway's children, which is what this figure is read as.
    free = st.f_bavail * frsize
    used = max(0, total - free)
    return {
        "total_mb": _round(total / 1048576.0),
        "used_mb": _round(used / 1048576.0),
        "used_pct": _round(used / total * 100.0, 2) if total else None,
    }


def _read_self_process(procfs: Path) -> dict[str, Any]:
    """This process's RSS, thread count and open fd count.

    RSS comes from :func:`kiro_crew.platform_compat.proc_rss_bytes`, the routine
    the adaptive controller already uses, so the two never disagree about the
    gateway's footprint.
    """
    out: dict[str, Any] = {"rss_mb": None, "threads": None, "fds": None}
    try:
        from kiro_crew import platform_compat

        out["rss_mb"] = _round(platform_compat.proc_rss_bytes() / 1048576.0)
    except Exception:  # noqa: BLE001 - a probe failure is a null field, not an error
        logger.debug("diag: rss probe failed", exc_info=True)
    try:
        for line in (procfs / "self" / "status").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            if key == "Threads":
                out["threads"] = int(rest.strip())
                break
    except (OSError, ValueError):
        out["threads"] = threading.active_count()
    for fd_dir in (procfs / "self" / "fd", Path("/dev/fd")):
        try:
            out["fds"] = len(os.listdir(fd_dir))
            break
        except OSError:
            continue
    return out


def _read_posture() -> dict[str, Any]:
    """Memory posture and the pressure line it is judged against.

    Reuses :func:`kiro_crew.resource_status.probe`, which is cgroup-clamped and
    container-aware, so the recorder's posture is the same one
    ``resource_status`` reports to an agent rather than a second opinion derived
    from raw ``meminfo``.
    """
    try:
        from kiro_crew import resource_status

        status = resource_status.probe()
        return {
            "posture": status.posture,
            "available_gb": _round(status.available_gb, 2),
            "pressure_gb": _round(status.pressure_gb, 2),
            "critical_gb": _round(status.critical_gb, 2),
            "cpu_count": status.cpu_count,
            "load_per_cpu": _round(status.load_per_cpu, 3),
        }
    except Exception:  # noqa: BLE001
        logger.debug("diag: posture probe failed", exc_info=True)
        return {
            "posture": None,
            "available_gb": None,
            "pressure_gb": None,
            "critical_gb": None,
            "cpu_count": os.cpu_count(),
            "load_per_cpu": None,
        }


# ── The recorder ─────────────────────────────────────────────────────────────


class Recorder:
    """Appends one state row per interval, and answers questions about the file.

    Everything with an external dependency is injectable, so a test drives a full
    sample with a fixture procfs tree and a fake clock and never touches the real
    host: *clock* for time, *procfs* for the ``/proc`` root, *statvfs* for mount
    usage.
    """

    def __init__(
        self,
        *,
        config_dir: Path | None = None,
        env: dict[str, str] | None = None,
        clock: Callable[[], float] | None = None,
        procfs: Path | str = "/proc",
        statvfs: "Callable[[str], Any] | None" = None,
        mounts: tuple[str, ...] = WATCHED_MOUNTS,
    ) -> None:
        self._env = dict(os.environ if env is None else env)
        self._clock = clock or time.time
        self._procfs = Path(procfs)
        # getattr, not os.statvfs: the name does not EXIST on Windows, so naming
        # it here raises AttributeError while building the recorder — i.e. on the
        # gateway boot path, before anything can degrade gracefully.
        self._statvfs = statvfs if statvfs is not None else getattr(os, "statvfs", None)
        self._mounts = tuple(mounts)
        self._config_dir_override = config_dir

        self.enabled = _env_flag_on(self._env, ENV_ENABLED, default=True)
        self._base_interval = _env_float(
            self._env, ENV_SAMPLE_SECS, DEFAULT_SAMPLE_SECS, 1.0, 3600.0
        )
        self._interval = self._base_interval
        self.retain_days = _env_int(self._env, ENV_RETAIN_DAYS, DEFAULT_RETAIN_DAYS, 1, 365)

        self._sources: dict[str, Callable[[], dict[str, Any]]] = {}
        self._write_lock = threading.Lock()
        self._running = False
        self._backoff = False
        self._slow_streak = 0
        self._backoff_warned = False
        self._last_sample_ts: float | None = None
        self._last_cost_ms: float | None = None
        self._samples_written = 0
        self._events_written = 0

        # Heartbeat / loop-lag accounting. Written by the loop task, read by the
        # sampler thread; a float store is atomic under the GIL.
        self._beat_at = 0.0
        self._lag_max_ms = 0.0

        # Previous-sample state, for the change-detecting events.
        self._prev_posture: str | None = None
        self._prev_config_stat: dict[str, tuple[float, int, int] | None] = {}
        self._prev_adaptive_cap: int | None = None
        self._prev_day: str | None = None
        self._threshold_active: dict[str, bool] = {}

        self._tasks: list[asyncio.Task[Any]] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- paths -------------------------------------------------------------

    def _config_dir(self) -> Path:
        if self._config_dir_override is not None:
            return self._config_dir_override
        from kiro_crew.config.paths import config_dir

        return config_dir()

    def _dir(self) -> Path:
        return self._config_dir() / DIR_NAME

    def _day_key(self, ts: float) -> str:
        """The ``YYYYMMDD`` a timestamp belongs to, for naming and comparing files.

        Total by construction. ``query`` takes its window from a caller, and
        ``_files_for_window`` widens that window by a day at each end, so a
        request as ordinary as ``since=0`` asks for a negative epoch -- which
        Windows refuses outright with ``EINVAL`` rather than returning a date.
        Raising ``OSError`` out of a read because someone asked for "everything"
        is the wrong answer, so an unrepresentable instant collapses to the
        matching end of the range. Both sentinels sort correctly against a real
        key in the string comparison that selects day files.
        """
        try:
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d")
        except (OSError, OverflowError, ValueError):
            return "00000000" if ts < 0 else "99999999"

    def _file_for(self, ts: float) -> Path:
        return self._dir() / f"{FILE_PREFIX}{self._day_key(ts)}{FILE_SUFFIX}"

    # -- registration ------------------------------------------------------

    def register_source(self, name: str, fn: Callable[[], dict[str, Any]]) -> None:
        """Add a named block to every sample.

        *fn* is called on a worker thread, once per sample, inside its own
        ``try``: a source that raises contributes ``{"error": ...}`` and does not
        cost the row. This is how gateway state and the process roster reach a
        module that must not import either.
        """
        if not callable(fn):
            raise TypeError(f"source {name!r} is not callable")
        self._sources[name] = fn

    # -- lifecycle ---------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Begin recording (idempotent). Registers the threads source and probe."""
        global _recorder  # noqa: PLW0603 - the documented module singleton
        _recorder = self
        if not self.enabled:
            logger.info("diag recorder disabled (%s=0)", ENV_ENABLED)
            return
        if self._running:
            return

        # Resolve the loop BEFORE touching any state, so a caller with neither
        # a running loop nor an explicit one gets a clear error and an
        # unchanged recorder. ``get_running_loop`` rather than
        # ``get_event_loop``: the latter is deprecated for this use and, with no
        # loop set, MAKES one that nothing runs -- the recorder's tasks would
        # then be scheduled onto a loop that never turns, which reads as "the
        # recorder started" and records nothing.
        if loop is not None:
            target = loop
        else:
            try:
                target = asyncio.get_running_loop()
            except RuntimeError:
                raise RuntimeError(
                    "Recorder.start() needs a running event loop or an explicit loop="
                ) from None

        from kiro_crew.diag import threads as diag_threads

        self.register_source("threads", diag_threads.window_stats)

        self._running = True
        self._beat_at = time.monotonic()
        self._loop = target
        if target.is_running():
            startup = target.create_task(self._start_on_loop(target))
            self._tasks = [startup]
            startup.add_done_callback(self._task_finished)
            return
        if not self._prepare_directory():
            self._running = False
            return
        self._activate(target)
        self.emit_event(EVENT_GATEWAY_START, {"pid": os.getpid(), "interval": self._interval})

    async def _start_on_loop(self, target: asyncio.AbstractEventLoop) -> None:
        if not await asyncio.to_thread(self._prepare_directory):
            self._running = False
            return
        if not self._running:
            return
        self._activate(target)
        await asyncio.to_thread(
            self.emit_event,
            EVENT_GATEWAY_START,
            {"pid": os.getpid(), "interval": self._interval},
        )

    def _prepare_directory(self) -> bool:
        try:
            self._dir().mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("diag recorder cannot create its directory; disabling", exc_info=True)
            return False
        return True

    def _activate(self, target: asyncio.AbstractEventLoop) -> None:
        from kiro_crew.diag import threads as diag_threads

        diag_threads.start_probe(emit_event=self.emit_event)
        self._tasks = [
            target.create_task(self._sampler_loop()),
            target.create_task(self._heartbeat_loop()),
        ]
        for task in self._tasks:
            task.add_done_callback(self._task_finished)
        logger.info(
            "diag recorder started (interval=%.0fs, retain=%dd, file=%s)",
            self._interval,
            self.retain_days,
            self._file_for(self._clock()).name,
        )

    def stop(self) -> "asyncio.Task[None] | None":
        """Stop recording and the probe (idempotent).

        Returns the task carrying the off-loop finish when a running loop made one,
        and ``None`` when the finish already ran inline. A caller that cannot await
        may ignore it; one that can should, so the closing event is written before
        the loop goes away.
        """
        if not self._running:
            return None
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        target = self._loop
        if target is not None and target.is_running():
            cleanup = target.create_task(asyncio.to_thread(self._finish_stop))
            cleanup.add_done_callback(self._task_finished)
            # Handed back, not awaited here: this method stays callable from
            # ordinary code, while a shutdown hook that CAN await gets something
            # to wait on. Without that wait the loop closes while the thread is
            # still queued and the final event, the one saying the gateway went
            # down cleanly, is the row that never lands. Awaiting a thread from
            # the hook yields rather than blocking, so the loop is not held.
            return cleanup
        self._finish_stop()
        return None

    def _finish_stop(self) -> None:
        self.emit_event(EVENT_GATEWAY_STOP, {"pid": os.getpid()})
        try:
            from kiro_crew.diag import threads as diag_threads

            diag_threads.stop_probe()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("diag: probe stop failed", exc_info=True)
        logger.info("diag recorder stopped")

    def _task_finished(self, task: "asyncio.Task[Any]") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("diag recorder task exited unexpectedly", exc_info=exc)

    # -- the loops ---------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Measure loop lag, and keep the probe's stall watch fed.

        The lag figure is the largest single overshoot since the last sample —
        a maximum, not a mean, because one 900 ms stall and a steady 9 ms of
        drift average the same and mean opposite things.
        """
        from kiro_crew.diag import threads as diag_threads

        while self._running:
            t0 = time.monotonic()
            await asyncio.sleep(HEARTBEAT_SECS)
            lag_ms = (time.monotonic() - t0 - HEARTBEAT_SECS) * 1000.0
            if lag_ms > self._lag_max_ms:
                self._lag_max_ms = lag_ms
            self._beat_at = time.monotonic()
            diag_threads.beat()

    async def _sampler_loop(self) -> None:
        # Retention reads the directory and can block on a slow filesystem, so
        # startup performs its one sweep on the worker used by every sample.
        try:
            await asyncio.to_thread(self._prune)
        except Exception:  # noqa: BLE001 - retention cannot stop sampling
            logger.debug("diag recorder startup prune failed", exc_info=True)

        # Warm the posture probe before the first counted sample. Its first call
        # loads config and the learned-cost store — measured at ~1.6 s on a cold
        # interpreter, against a 20 ms budget — and every call after that is
        # fingerprint-cached and cheap. Counted, that one-off boot cost would
        # trip the three-sample back-off immediately and pin the cadence at 60 s
        # on a perfectly healthy host, so the back-off would signal "the
        # gateway started" instead of "the gateway is under load". Paid here,
        # off the loop and outside the series, because the cost is real and
        # belongs to boot.
        try:
            await asyncio.to_thread(_read_posture)
        except Exception:  # noqa: BLE001 - a cold probe failure is not fatal
            logger.debug("diag recorder posture warm-up failed", exc_info=True)
        while self._running:
            await asyncio.sleep(self._interval)
            if not self._running:
                return
            try:
                # Off the loop: the sample reads procfs and calls every source,
                # and doing that inline would add to the very lag it reports.
                row = await asyncio.to_thread(self._build_sample)
                # The sample lands BEFORE the events its own numbers reveal, so
                # every event's ts is greater than or equal to the ts of the
                # sample it came from. A reader paginating by ts would otherwise
                # meet the event first and, resuming from the sample's ts on the
                # next page, read that event a second time.
                await asyncio.to_thread(self._append, row)
                await asyncio.to_thread(self._detect_events, row)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad sample must not end the series
                logger.warning("diag recorder sample failed", exc_info=True)

    # -- sampling ----------------------------------------------------------

    def sample_now(self) -> dict[str, Any]:
        """Build one sample row without writing it. Blocking; call off the loop.

        The row is returned rather than persisted, while the events its numbers
        reveal are still emitted: a caller asking for the current state should not
        have to miss a threshold crossing to get it.
        """
        row = self._build_sample()
        self._detect_events(row)
        return row

    def _build_sample(self) -> dict[str, Any]:
        started = time.perf_counter()
        ts = self._clock()
        lag_ms, self._lag_max_ms = self._lag_max_ms, 0.0

        row: dict[str, Any] = {"ts": ts, "pid": os.getpid()}
        row.update(_read_loadavg(self._procfs))
        row.update(_read_meminfo(self._procfs))
        row["posture"] = _read_posture()
        row["process"] = _read_self_process(self._procfs)
        row["loop_lag_ms_max"] = round(max(0.0, lag_ms), 2)
        row["mounts"] = {mount: _read_mount_usage(mount, self._statvfs) for mount in self._mounts}

        sources: dict[str, Any] = {}
        for name, fn in list(self._sources.items()):
            source_started = time.perf_counter()
            try:
                value = fn()
                if not isinstance(value, dict):
                    value = {"error": f"source returned {type(value).__name__}, not dict"}
            except Exception as exc:  # noqa: BLE001 - a source never breaks the row
                logger.debug("diag source %s failed", name, exc_info=True)
                value = {"error": repr(exc)[:300]}
            value["_cost_ms"] = round((time.perf_counter() - source_started) * 1000.0, 2)
            sources[name] = value
        row.update(sources)

        cost_ms = (time.perf_counter() - started) * 1000.0
        row["cost_ms"] = round(cost_ms, 2)
        row["over_budget"] = cost_ms > SAMPLE_BUDGET_MS
        self._note_cost(cost_ms)
        row["interval"] = self._interval
        self._last_sample_ts = ts
        self._last_cost_ms = round(cost_ms, 2)
        return row

    def _note_cost(self, cost_ms: float) -> None:
        """Apply the back-off rule.

        Sticky once tripped, deliberately. A cadence that recovers to 30 s and
        trips again produces a series whose spacing varies with load, which is
        the hardest kind to read: a gap then fails to distinguish "the host was
        busy" from "the cadence changed". One WARNING, one cadence change, and
        ``health()`` reports it.
        """
        if self._backoff:
            return
        if cost_ms > SAMPLE_SLOW_MS:
            self._slow_streak += 1
        else:
            self._slow_streak = 0
        if self._slow_streak < SAMPLE_SLOW_STREAK:
            return
        self._backoff = True
        self._interval = max(self._base_interval, BACKOFF_SAMPLE_SECS)
        if not self._backoff_warned:
            self._backoff_warned = True
            logger.warning(
                "diag recorder: %d consecutive samples over %.0fms (last %.0fms); "
                "cadence reduced to %.0fs",
                SAMPLE_SLOW_STREAK,
                SAMPLE_SLOW_MS,
                cost_ms,
                self._interval,
            )

    # -- events ------------------------------------------------------------

    def emit_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Append an event row.

        Callable from any thread — the probe thread emits ``loop_stall`` — so it
        takes no fresh host reading: it carries the payload plus the last known
        cadence state, and the periodic sample (which is already probing) is what
        attaches full host state to the events it detects itself. A stall is
        exactly the moment when probing more would be the wrong move.
        """
        row = {
            "ts": self._clock(),
            "kind": kind,
            "pid": os.getpid(),
            "payload": payload,
            "last_sample_ts": self._last_sample_ts,
        }
        try:
            self._append(row)
        except Exception:  # noqa: BLE001 - an event must never raise into a caller
            logger.debug("diag event %s could not be written", kind, exc_info=True)

    def _detect_events(self, row: dict[str, Any]) -> None:
        """Emit the events this sample's own numbers reveal."""
        posture = (row.get("posture") or {}).get("posture")
        if posture is not None and self._prev_posture is not None and posture != self._prev_posture:
            self.emit_event(
                EVENT_MEMORY_POSTURE,
                {
                    "from": self._prev_posture,
                    "to": posture,
                    "available_gb": row["posture"].get("available_gb"),
                },
            )
        if posture is not None:
            self._prev_posture = posture

        available_gb = (row.get("posture") or {}).get("available_gb")
        pressure_gb = (row.get("posture") or {}).get("pressure_gb")
        if available_gb is not None and pressure_gb is not None:
            memory_low = available_gb < pressure_gb
            if self._threshold_entered("memory_below_pressure", memory_low):
                self.emit_event(
                    EVENT_THRESHOLD,
                    {
                        "signal": "memory_below_pressure",
                        "available_gb": available_gb,
                        "pressure_gb": pressure_gb,
                    },
                )

        for mount, usage in (row.get("mounts") or {}).items():
            pct = usage.get("used_pct")
            if pct is not None and self._threshold_entered(
                f"mount_full:{mount}", pct > TMPFS_EVENT_PCT
            ):
                self.emit_event(
                    EVENT_THRESHOLD,
                    {
                        "signal": "mount_full",
                        "mount": mount,
                        "used_pct": pct,
                        "limit_pct": TMPFS_EVENT_PCT,
                    },
                )

        load1 = row.get("load1")
        cpus = (row.get("posture") or {}).get("cpu_count") or os.cpu_count() or 1
        if load1 is not None and self._threshold_entered(
            "load_high", load1 > LOAD_EVENT_FACTOR * cpus
        ):
            self.emit_event(
                EVENT_THRESHOLD,
                {
                    "signal": "load_high",
                    "load1": load1,
                    "cpu_count": cpus,
                    "factor": LOAD_EVENT_FACTOR,
                },
            )

        self._detect_config_rewrite()
        self._detect_adaptive_drop(row)

        day = self._day_key(row["ts"])
        if self._prev_day is not None and day != self._prev_day:
            self._prune()
        self._prev_day = day

    def _threshold_entered(self, name: str, active: bool) -> bool:
        """Return true once when a measured threshold enters its active state."""
        was_active = self._threshold_active.get(name, False)
        self._threshold_active[name] = active
        return active and not was_active

    def _detect_config_rewrite(self) -> None:
        """Report a watched config file whose identity or mtime changed.

        ``stat`` only. The inode is checked alongside the mtime because the safe
        way to replace a config file is write-a-temp-then-rename, which can land
        inside one mtime granule: comparing mtime alone would miss exactly the
        rewrite that a careful writer performs. No file is opened, so this stays
        inside the rule that fenced files are metadata and never bytes — which
        matters most for ``.env``.
        """
        try:
            base = self._config_dir()
        except Exception:  # noqa: BLE001
            return
        for name in WATCHED_CONFIG_FILES:
            path = base / name
            try:
                st = path.stat()
                current: tuple[float, int, int] | None = (st.st_mtime, st.st_ino, st.st_size)
            except OSError:
                current = None
            if name in self._prev_config_stat and self._prev_config_stat[name] != current:
                previous = self._prev_config_stat[name]
                self.emit_event(
                    EVENT_CONFIG_REWRITTEN,
                    {
                        "file": name,
                        "existed_before": previous is not None,
                        "exists_now": current is not None,
                        "size_before": previous[2] if previous else None,
                        "size_now": current[2] if current else None,
                        "inode_changed": bool(previous and current and previous[1] != current[1]),
                    },
                )
            self._prev_config_stat[name] = current

    def _detect_adaptive_drop(self, row: dict[str, Any]) -> None:
        """Report the adaptive subagent cap going DOWN, with the policy's reason.

        The cap is read from whatever the boot wiring registered as the
        ``gateway`` source, so this module needs no controller import. Only a
        decrease is an event: a cap climbing back is the system working.
        """
        gateway = row.get("gateway")
        if not isinstance(gateway, dict):
            return
        cap = gateway.get("adaptive_cap")
        if not isinstance(cap, int):
            return
        if self._prev_adaptive_cap is not None and cap < self._prev_adaptive_cap:
            self.emit_event(
                EVENT_ADAPTIVE_CAP_LOWERED,
                {
                    "from": self._prev_adaptive_cap,
                    "to": cap,
                    "reason": gateway.get("adaptive_reason"),
                },
            )
        self._prev_adaptive_cap = cap

    # -- writing and retention --------------------------------------------

    def _append(self, row: dict[str, Any]) -> None:
        """Append one row as a single JSON line.

        Opened per append rather than held: 2880 opens a day is nothing, and an
        open fd would keep a pruned file's inode alive and make the recorder the
        reason its own retention did not free space.
        """
        path: Path | None = None
        try:
            line = json.dumps(row, default=str, separators=(",", ":"), ensure_ascii=False)
            path = self._file_for(row["ts"])
            with self._write_lock:
                if _pinned_dir_fd_supported():
                    # Creating the directory here is safe on this branch because the
                    # open below pins it with O_NOFOLLOW and verifies the descriptor.
                    # The other branch does NOT get this call: append_line resolves
                    # the anchor once and creates the leaf under that pin, so asking
                    # for the directory by path first would be the one step in the
                    # append able to follow a redirected diag directory.
                    path.parent.mkdir(parents=True, exist_ok=True)
                    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    dir_fd = os.open(str(path.parent), dir_flags)
                    try:
                        file_flags = (
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_APPEND
                            | os.O_NOFOLLOW
                            | getattr(os, "O_NONBLOCK", 0)
                            | getattr(os, "O_BINARY", 0)
                        )
                        fd = os.open(path.name, file_flags, 0o600, dir_fd=dir_fd)
                        try:
                            # O_NOFOLLOW refuses a symlink, and S_ISREG alone does
                            # not refuse a HARD link, which is itself a regular
                            # file: a link planted at this name on the same
                            # filesystem would take the append into whatever else
                            # it names. A snapshot the recorder owns has exactly
                            # one name. platform_log_append applies the same pair
                            # for the branch that cannot pin a descriptor.
                            info = os.fstat(fd)
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                                raise OSError(f"not an exclusively named regular file: {path}")
                            handle = os.fdopen(fd, "a", encoding="utf-8")
                            fd = -1
                            with handle:
                                handle.write(line + "\n")
                        finally:
                            if fd >= 0:
                                os.close(fd)
                    finally:
                        os.close(dir_fd)
                else:
                    from kiro_crew.platform_log_append import append_line

                    append_line(path, (line + "\n").encode("utf-8"))
        except Exception:  # noqa: BLE001 - probe-thread writers must never raise
            logger.debug("diag recorder could not append to %s", path, exc_info=True)
            return
        if row.get("kind"):
            self._events_written += 1
        else:
            self._samples_written += 1

    def _prune(self) -> int:
        """Delete day files older than the retention window. Returns the count.

        Keyed on the filename's date, not mtime: an appended-to file's mtime is
        today whatever day its rows describe, so an mtime rule would keep the
        oldest file forever.
        """
        cutoff = (
            datetime.fromtimestamp(self._clock(), timezone.utc) - timedelta(days=self.retain_days)
        ).strftime("%Y%m%d")
        removed = 0
        dir_fd: int | None = None
        # Either pin satisfies this type: the POSIX branch keeps its own dir fd in
        # ``dir_fd`` and leaves this a no-op, while pinned_log_dir yields a fd on
        # POSIX and None on Windows, where the held directory handle is what blocks
        # the swap and no descriptor is usable for dir_fd= syscalls.
        retention_pin: AbstractContextManager[int | None] = nullcontext()
        try:
            if _pinned_dir_fd_supported():
                dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                dir_fd = os.open(str(self._dir()), dir_flags)
            else:
                from kiro_crew.platform_log_append import pinned_log_dir

                retention_pin = pinned_log_dir(self._dir())
            with retention_pin:
                if dir_fd is not None:
                    entries = os.listdir(dir_fd)
                else:
                    entries = [path.name for path in self._dir().iterdir()]
                for name in entries:
                    if not (name.startswith(FILE_PREFIX) and name.endswith(FILE_SUFFIX)):
                        continue
                    day = name[len(FILE_PREFIX) : -len(FILE_SUFFIX)]
                    if len(day) != 8 or not day.isdigit() or day >= cutoff:
                        continue
                    path = self._dir() / name
                    try:
                        if dir_fd is None:
                            path.unlink()
                        else:
                            os.unlink(name, dir_fd=dir_fd)
                        removed += 1
                    except OSError:
                        logger.debug("diag recorder could not prune %s", path, exc_info=True)
        except OSError:
            return 0
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
        if removed:
            logger.info("diag recorder pruned %d snapshot file(s) past retention", removed)
        return removed

    # -- reading -----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Whether the recorder is working, and where its rows are going."""
        now = self._clock()
        path = self._file_for(now)
        rows_today = 0
        try:
            with open(path, encoding="utf-8") as handle:
                rows_today = sum(1 for _ in handle)
        except OSError:
            rows_today = 0
        return {
            "enabled": self.enabled,
            "running": self._running,
            "last_sample_ts": self._last_sample_ts,
            "last_sample_age_secs": (
                round(now - self._last_sample_ts, 1) if self._last_sample_ts else None
            ),
            "last_cost_ms": self._last_cost_ms,
            "interval": self._interval,
            "base_interval": self._base_interval,
            "backoff": self._backoff,
            "budget_ms": SAMPLE_BUDGET_MS,
            "retain_days": self.retain_days,
            "file": str(path),
            "rows_today": rows_today,
            "samples_written": self._samples_written,
            "events_written": self._events_written,
            "sources": sorted(self._sources),
        }

    def query(
        self,
        since: float | None = None,
        until: float | None = None,
        around: float | None = None,
        radius: float | None = None,
        fields: list[str] | None = None,
        events_only: bool = False,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Rows and events in a time window, with per-field min/max/avg.

        The window is ``around`` +/- ``radius`` when given, otherwise
        ``since``..``until``, otherwise the last five minutes. *fields* projects
        the series (``ts`` is always kept — a point with no time is not a point).
        *cursor* resumes a previous answer that hit :data:`QUERY_BYTE_CAP`;
        statistics are computed over every row in the window, not only the
        returned page, so a truncated answer still reports true extremes.
        """
        window_from, window_to = self._resolve_window(since, until, around, radius)
        start_at = window_from
        # Rows already served AT ``start_at``. A sample and the events its own
        # numbers reveal share one sampling instant, so a timestamp alone cannot
        # say which of them a page ended on: resuming at that instant would serve
        # the earlier one twice. The count is what makes the resume exact.
        start_skip = 0
        if cursor:
            try:
                head, _, tail = str(cursor).partition(":")
                start_at = max(window_from, float(head))
                start_skip = int(tail) if tail else 0
            except (TypeError, ValueError):
                pass
        seen_at_start = 0

        series: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        accumulators: dict[str, list[float]] = {}
        dropped_fields: set[str] = set()
        total_rows = 0
        budget = QUERY_BYTE_CAP
        next_cursor: str | None = None

        for row in self._iter_rows(window_from, window_to):
            total_rows += 1
            if not row.get("kind"):
                self._accumulate(row, accumulators, dropped_fields)
            if row["ts"] < start_at or next_cursor is not None:
                continue
            is_event = bool(row.get("kind"))
            if events_only and not is_event:
                continue
            if row["ts"] == start_at and start_skip:
                # Counted after the filter above, because the cursor counted rows
                # this answer RETURNED: a row the filter drops must not consume one
                # of the places being skipped.
                seen_at_start += 1
                if seen_at_start <= start_skip:
                    continue
            projected = _project(row, fields)
            cost = len(json.dumps(projected, default=str, separators=(",", ":")))
            if cost > budget and (series or events):
                # Cut on a row boundary and hand back where to resume: the instant,
                # plus how many rows sharing it this answer already returned.
                # Cumulative, not per-page: the count has to include the rows
                # EARLIER pages already served at this instant, which this page
                # skipped on the way in. Counting only this page's own rows makes
                # the next cursor point back at rows already returned, and with
                # more rows sharing one instant than fit on a page the later ones
                # are never reached at all.
                served_here = sum(1 for r in (*series, *events) if r.get("ts") == row["ts"])
                if row["ts"] == start_at:
                    served_here += start_skip
                next_cursor = f"{row['ts']!r}:{served_here}"
                continue
            budget -= cost
            (events if is_event else series).append(projected)

        return {
            "window": {"since": window_from, "until": window_to},
            "series": series,
            "events": events,
            "stats": {
                name: {
                    "min": round(tally[2], 3),
                    "max": round(tally[3], 3),
                    "avg": round(tally[1] / tally[0], 3),
                    "n": int(tally[0]),
                }
                for name, tally in sorted(accumulators.items())
                if tally[0]
            },
            "rows_in_window": total_rows,
            "returned": len(series) + len(events),
            "truncated": next_cursor is not None,
            "cursor": next_cursor,
            "stats_truncated": bool(dropped_fields),
            "stats_fields_dropped": len(dropped_fields),
        }

    def _resolve_window(
        self,
        since: float | None,
        until: float | None,
        around: float | None,
        radius: float | None,
    ) -> tuple[float, float]:
        if around is not None:
            span = (
                _parse_duration(radius, "radius")
                if radius is not None
                else float(DEFAULT_QUERY_RADIUS_SECS)
            )
            middle = _parse_instant(around, "around")
            return middle - span, middle + span
        now = self._clock()
        end = _parse_instant(until, "until") if until is not None else now
        begin = (
            _parse_instant(since, "since") if since is not None else end - DEFAULT_QUERY_RADIUS_SECS
        )
        if begin > end:
            begin, end = end, begin
        return begin, end

    def _iter_rows(self, window_from: float, window_to: float) -> "Any":
        """Yield parsed rows in the window, oldest first, across day files.

        Records come through :func:`kiro_crew.jsonl_util.bounded_records`, which
        caps each one and skips anything over the cap. The file lives under the
        data home, so its size is not this reader's to trust: one very long line
        would otherwise be read whole into memory to answer a five-minute
        question. The read-only, degradable posture is the right one here --
        a query that drops one unreadable record still answers correctly, where
        aborting would lose the window.
        """
        from kiro_crew import jsonl_util

        for path in self._files_for_window(window_from, window_to):
            try:
                with open(path, "rb") as handle:
                    for line in jsonl_util.bounded_records(handle, path, label="diag-query"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue  # a partial trailing write, not a reason to stop
                        if not isinstance(row, dict):
                            continue  # valid JSON, but not a record: skip, do not abort
                        ts = row.get("ts")
                        if not isinstance(ts, (int, float)):
                            continue
                        if window_from <= ts <= window_to:
                            yield row
            except OSError:
                continue

    def _files_for_window(self, window_from: float, window_to: float) -> list[Path]:
        """The day files that can hold rows in the window, oldest first.

        Selected by filename date rather than by reading every file: a week of
        retention is seven files, and a five-minute question should not parse six
        days of rows to answer.
        """
        try:
            names = sorted(
                p
                for p in self._dir().iterdir()
                if p.name.startswith(FILE_PREFIX) and p.name.endswith(FILE_SUFFIX)
            )
        except OSError:
            return []
        # One day of slack at each end: the files are named in UTC and a window
        # can straddle midnight.
        low = self._day_key(window_from - 86400)
        high = self._day_key(window_to + 86400)
        return [p for p in names if low <= p.name[len(FILE_PREFIX) : -len(FILE_SUFFIX)] <= high]

    @staticmethod
    def _accumulate(row: dict[str, Any], out: dict[str, list[float]], dropped: "set[str]") -> None:
        """Tally numeric leaves as dotted paths, one level into nested blocks.

        Each field carries four running numbers, ``[n, total, low, high]``, rather
        than every value it has taken. The returned page is byte-capped but the
        tally spans the whole window, so holding the values would make memory grow
        with the window instead of with the answer. n, min, max and avg are all a
        caller reads, and each is exact from the four.
        """

        def walk(prefix: str, value: Any, depth: int) -> None:
            if isinstance(value, bool):
                return  # a bool is an int in Python; min/max of a flag is noise
            if isinstance(value, (int, float)):
                number = float(value)
                tally = out.get(prefix)
                if tally is None:
                    if len(out) >= STATS_FIELD_CAP:
                        dropped.add(prefix)
                        return
                    out[prefix] = [1.0, number, number, number]
                else:
                    tally[0] += 1.0
                    tally[1] += number
                    tally[2] = min(tally[2], number)
                    tally[3] = max(tally[3], number)
                return
            if isinstance(value, dict) and depth < 2:
                for key, child in value.items():
                    if key.startswith("_"):
                        continue
                    walk(f"{prefix}.{key}" if prefix else key, child, depth + 1)

        for key, value in row.items():
            if key in ("ts", "pid", "kind", "payload"):
                continue
            walk(key, value, 0)


def _project(row: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
    """*row* reduced to *fields*, keeping the keys that identify it."""
    if not fields:
        return row
    keep = set(fields) | {"ts", "kind", "payload"}
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key in keep:
            out[key] = value
            continue
        if isinstance(value, dict):
            # Allow "process.rss_mb" to select one leaf of a nested block.
            nested = {
                leaf: value[leaf]
                for leaf in (f.split(".", 1)[1] for f in fields if f.startswith(f"{key}."))
                if leaf in value
            }
            if nested:
                out[key] = nested
    return out


#: Module singleton. Set by :meth:`Recorder.start` so an HTTP route can reach the
#: live instance without the gateway threading it through every handler.
_recorder: Recorder | None = None


def get_recorder() -> Recorder | None:
    """The running recorder, or ``None`` when the gateway never started one."""
    return _recorder
