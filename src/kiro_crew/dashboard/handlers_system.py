"""System metrics and status handlers — CPU, memory, network, disk monitoring."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import hashlib
import hmac
import logging
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

import kiro_crew
from kiro_crew import platform_compat
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.embeddings import get_shared_embedder, model_file_present
from kiro_crew.executors import subprocess_executor
from kiro_crew.platform import current_context
from kiro_crew.safety_override import safety_override, until_shutdown_permitted
from kiro_crew.stats import Stats

logger = logging.getLogger(__name__)

# Absolute paths for macOS system commands — shutil.which may fail when PATH
# is minimal (e.g. launched as a background service), so fall back to known
# locations.
_SYSCTL = shutil.which("sysctl") or "/usr/sbin/sysctl"
_VM_STAT = shutil.which("vm_stat") or "/usr/bin/vm_stat"
_NETSTAT = shutil.which("netstat") or "/usr/sbin/netstat"

# Server-side network speed tracking (survives page refresh)
_prev_net: dict[str, float] = {"rx": 0.0, "tx": 0.0, "ts": 0.0}
_net_speed: dict[str, float] = {"rx_kbs": 0.0, "tx_kbs": 0.0}

# Server-side process CPU % tracking (delta of cpu_time / wall_time)
_prev_cpu: dict[str, float] = {"total": 0.0, "ts": 0.0}
_proc_cpu_pct: float = 0.0

# Server-side system CPU % tracking (/proc/stat busy/total jiffy delta)
_prev_sys_cpu: dict[str, float] = {"busy": 0.0, "total": 0.0}
_sys_cpu_pct: float = 0.0

# Last-known Windows system CPU % (GetSystemTimes delta returns None on the
# first sample; reuse the previous value so the header doesn't flash to 0).
_last_win_cpu_pct: float = 0.0

# Cached static system info (computed once)
_STATIC_SYSTEM_INFO: dict[str, object] | None = None


def _system_cpu_pct_from_proc_stat() -> float | None:
    """System CPU % from the /proc/stat busy/total jiffy delta since the last
    call; iowait and steal count as busy. Returns None on non-Linux or the
    first (pre-delta) sample so the caller can fall back to ps."""
    try:
        with open("/proc/stat") as f:
            fields = f.readline().split()
        if len(fields) < 5 or fields[0] != "cpu":
            return None
        # First 8 cols only (user..steal); guest/guest_nice are already
        # folded into user/nice, so summing them would double-count.
        vals = [float(x) for x in fields[1:9]]
    except (OSError, ValueError):
        return None
    total = sum(vals)
    busy = total - vals[3]  # vals[3] is idle; iowait/steal stay in busy
    global _sys_cpu_pct
    prev_total, prev_busy = _prev_sys_cpu["total"], _prev_sys_cpu["busy"]
    _prev_sys_cpu["total"], _prev_sys_cpu["busy"] = total, busy
    if prev_total <= 0:
        return None  # first sample, no delta yet
    dtotal = total - prev_total
    if dtotal <= 0:
        return _sys_cpu_pct  # counters didn't advance; reuse last value
    _sys_cpu_pct = min(100.0, max(0.0, round((busy - prev_busy) / dtotal * 100, 1)))
    return _sys_cpu_pct


# Eager fallback salt — trivial cost (32 bytes), eliminates race under run_in_executor
_IN_MEMORY_SALT: bytes = secrets.token_bytes(32)


def _get_telemetry_salt() -> bytes:
    """Return a per-install random salt, generating one on first run."""
    try:
        salt_file = config_dir() / "telemetry_salt"
        if salt_file.exists():
            data = salt_file.read_bytes()
            if len(data) == 32:
                return data
            # corrupted/truncated — remove before regenerating
            salt_file.unlink(missing_ok=True)
        salt_file.parent.mkdir(parents=True, exist_ok=True)
        salt = secrets.token_bytes(32)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(salt_file.parent))
        try:
            os.write(tmp_fd, salt)
            os.close(tmp_fd)
            tmp_fd = -1
            platform_compat.chmod_safe(tmp_path, 0o600)
            os.link(tmp_path, str(salt_file))
            return salt
        except FileExistsError:
            data = salt_file.read_bytes()
            if len(data) == 32:
                return data
            raise OSError("incomplete salt file")
        finally:
            if tmp_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(tmp_fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
    except (RuntimeError, KeyError, OSError):
        return _IN_MEMORY_SALT


def _yolo_duration_fields() -> tuple[str, bool]:
    """``(configured_duration, until_shutdown_permitted)`` for the Settings card.

    BOTH values touch the filesystem — the config read and the governance profile
    resolution (``iterdir``/``stat`` over the profiles dir) — so this runs in a
    worker thread, never on the event loop. ``/api/status`` is polled
    continuously; doing this inline stalls the whole gateway on a slow home.
    """
    try:
        label = str(KiroCrewConfig.load().agent.yolo_duration)
    except Exception:
        logger.debug("could not read agent.yolo_duration for status", exc_info=True)
        label = "6h"
    try:
        permitted = bool(until_shutdown_permitted())
    except Exception:
        logger.debug("could not resolve until_shutdown permission", exc_info=True)
        permitted = True
    return label, permitted


async def api_status(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    uptime = time.time() - state.start_time
    from kiro_crew.dashboard.handlers import (
        _UPDATE_CHECK_INTERVAL,
        _do_update_check,
    )
    from kiro_crew.dashboard.handlers import updates as _updates_mod

    # Auto-recheck every 12h in background. Tracked in ``_background_tasks`` (this
    # module's own documented pattern) rather than left as a bare create_task: the
    # check now performs network I/O with a multi-second timeout, so an untracked
    # task can be garbage-collected mid-flight or still be pending when the loop
    # closes. ``_do_update_check`` is additionally single-flight, because the
    # interval clock is only stamped once a check finishes.
    if time.time() - _updates_mod._last_update_check > _UPDATE_CHECK_INTERVAL:
        _bg = asyncio.create_task(_do_update_check())
        state._background_tasks.add(_bg)
        _bg.add_done_callback(state._background_tasks.discard)

    data = state.status_snapshot(**_updates_mod.status_update_fields())  # type: ignore[arg-type]
    static_info = _get_static_system_info()
    if state._owner_hash is not None:
        owner_hash = state._owner_hash
    else:
        loop = asyncio.get_running_loop()
        try:
            owner_hash = await loop.run_in_executor(None, _get_owner_hash, state)
        except Exception:
            owner_hash = "unknown"
    so_status = safety_override().status()
    # Off-loop: both values hit the filesystem (see _yolo_duration_fields).
    yolo_duration, until_shutdown_ok = await asyncio.to_thread(_yolo_duration_fields)
    data.update(
        {
            "uptime_secs": int(uptime),
            "messages_received": state.messages_received,
            "cron": state.crons.status(),
            "stats": Stats().snapshot(),
            "stats_summary": Stats().summary(),
            "update_progress": state._update_progress,
            "version": kiro_crew.__version__,
            "platform": sys.platform,
            "yolo": so_status.active,
            "yolo_active": so_status.active,
            "yolo_expires_at": so_status.expires_at_iso or "",
            "yolo_remaining_secs": so_status.remaining_secs,
            # True when the live grant has no timed expiry at all (declared in
            # config, or ad-hoc under yolo_duration: until_shutdown).
            # ``yolo_remaining_secs`` is -1 and ``yolo_expires_at`` empty then.
            "yolo_until_shutdown": so_status.permanent,
            # The configured ad-hoc duration, and whether policy allows the
            # no-timed-expiry option — the Settings card lock-badges it when not.
            "yolo_duration": yolo_duration,
            "yolo_until_shutdown_permitted": until_shutdown_ok,
            "owner_id_hash": owner_hash,
            "os_type": static_info.get("os", ""),
            "arch": static_info.get("arch", ""),
            "cpu_count": static_info.get("cpu_count", 0),
            "mem_total_gb": static_info.get("mem_total_gb", 0),
        }
    )
    # Frontend RUM config blob (PlatformContext telemetry).  The Default
    # TelemetryProvider returns None (RUM off), so the standalone status payload
    # is byte-for-byte unchanged and the SPA's RUM shim stays a no-op.  The
    # Amazon companion returns the Cognito/RUM config its frontend host consumes;
    # only then is a ``rum`` key added.  Best-effort — a telemetry-lookup failure
    # never breaks the status endpoint.
    try:
        rum_config = current_context().telemetry.frontend_rum_config()
        if rum_config is not None:
            data["rum"] = rum_config
    except Exception:
        logger.debug("frontend_rum_config lookup failed; RUM omitted", exc_info=True)
    return web.json_response(data)


def _get_static_system_info() -> dict[str, object]:
    global _STATIC_SYSTEM_INFO
    if _STATIC_SYSTEM_INFO is not None:
        return _STATIC_SYSTEM_INFO

    arch = platform.machine()
    if sys.platform == "darwin":
        try:
            real_arch = (
                subprocess.check_output([_SYSCTL, "-n", "hw.optional.arm64"], timeout=2)
                .decode()
                .strip()
            )
            if real_arch == "1":
                arch = "arm64 (Apple Silicon)"
        except Exception:
            pass

    info: dict[str, object] = {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
        "arch": arch,
        "pid": os.getpid(),
        "cpu_count": os.cpu_count() or 0,
        "cwd": os.getcwd(),
    }

    # Total memory (static) — cross-platform
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output([_SYSCTL, "-n", "hw.memsize"], timeout=2).decode().strip()
            info["mem_total_gb"] = round(int(out) / (1024**3), 1)
        except Exception:
            pass
    elif sys.platform == "linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        info["mem_total_gb"] = round(kb / (1024**2), 1)
                        break
        except Exception:
            pass
    elif sys.platform == "win32":
        mem = platform_compat.system_memory()
        if mem:
            info["mem_total_gb"] = round(mem[0] / (1024**3), 1)

    _STATIC_SYSTEM_INFO = info
    return info


def _get_owner_hash(state: DashboardState) -> str:
    """Return a cached HMAC-SHA256 hash of the owner identity. Stored on state to avoid stale globals."""
    cached = getattr(state, "_owner_hash", None)
    if cached is not None:
        return cached
    try:
        raw_owner = state.owner_id or f"{platform.node()}:{getpass.getuser()}"
    except (OSError, KeyError):
        raw_owner = f"{platform.node()}:unknown"
    h = hmac.new(_get_telemetry_salt(), raw_owner.encode(), hashlib.sha256).hexdigest()
    state._owner_hash = h
    return h


def _parse_vm_stat(vm_stat_output: str) -> tuple[int, dict[str, int]]:
    """Parse `vm_stat` output into ``(page_size, {stat_name: pages})``.

    Page size defaults to 16 KiB (Apple Silicon) if the header is absent.
    """
    page_size = 16384
    counts: dict[str, int] = {}
    for line in vm_stat_output.splitlines():
        if "page size of" in line:
            with contextlib.suppress(ValueError, IndexError):
                page_size = int(line.split()[-2])
            continue
        key, sep, val = line.partition(":")
        if not sep:
            continue
        val = val.strip().rstrip(".")
        if val.isdigit():
            counts[key.strip()] = int(val)
    return page_size, counts


def _macos_memory_gb(total_bytes: int, vm_stat_output: str) -> tuple[float, float]:
    """Return ``(used_gb, free_gb)`` matching macOS Activity Monitor's numbers.

    Activity Monitor's "Memory Used" is ``App Memory + Wired + Compressed``:

        App Memory = anonymous pages - purgeable pages   (dirty app allocations)
        Wired      = wired-down pages                     (kernel/non-pageable)
        Compressed = pages occupied by the compressor

    Everything else — free pages plus the reclaimable file-backed cache
    ("Cached Files") — is reported as free/available, so ``used + free`` equals
    total.

    The previous implementation counted *every* inactive page as free and
    ignored compressed memory entirely, which under-reported "used" by several
    GB versus Activity Monitor and `memory_pressure` (e.g. it showed 28 GB used
    where Activity Monitor reported ~33 GB on a 48 GB machine). "Pages inactive"
    includes dirty anonymous pages that are genuinely in use, not just
    reclaimable cache, so it must not be treated as free.
    """
    page_size, counts = _parse_vm_stat(vm_stat_output)

    anonymous = counts.get("Anonymous pages", 0)
    purgeable = counts.get("Pages purgeable", 0)
    wired = counts.get("Pages wired down", 0)
    compressed = counts.get("Pages occupied by compressor", 0)

    if anonymous:
        app_pages = max(0, anonymous - purgeable)
    else:
        # Legacy vm_stat without the "Anonymous pages" line: fall back to the
        # active-page count as an approximation of app memory.
        app_pages = counts.get("Pages active", 0)

    used_bytes = (app_pages + wired + compressed) * page_size
    # Never exceed physical memory (guards against a parse/field mismatch).
    used_bytes = max(0, min(used_bytes, total_bytes))

    used_gb = round(used_bytes / (1024**3), 1)
    free_gb = round((total_bytes - used_bytes) / (1024**3), 1)
    return used_gb, free_gb


#: Process-scan results, cached independently of the rest of the payload.
_proc_scan_cache: dict[str, object] = {}
_proc_scan_cache_ts: float = 0.0

#: How long a process COUNT may be reused.
#:
#: Deliberately much longer than :data:`_METRICS_CACHE_TTL`, because the two
#: answer different questions. CPU / memory / network are a live graph and are
#: meaningless stale; "how many MCP processes exist" is a slow-moving fact that
#: nobody reads at 2s resolution — and on any host without ``/proc`` it costs a
#: whole-machine ``ps`` walk to obtain, which grows with the process count the
#: shared MCP gateway multiplies. Tying it to the graph's refresh rate is what
#: made every poll pay for it.
_PROC_SCAN_CACHE_TTL = 15.0


def _scan_mcp_processes() -> dict[str, object]:
    """Count MCP-ecosystem processes by command-line signature.

    A single process may match multiple signatures (e.g. a sandboxed kiro-cli
    matches both "kirocrew_sandbox" and "kiro-cli"); per-category counts can
    overlap, while ``mcp_total`` dedups by PID. kiro-cli is an optional backend;
    the signature is harmless when it is not installed.

    Sandbox counting platform differences:
      Linux:  The namespace launcher (python3 ~/.kiro/crew/run/kirocrew_sandbox_*.py ...)
              forks — the parent stays alive with "kirocrew_sandbox" in its
              /proc/cmdline, so sandbox count is accurate.
      macOS:  sandbox-exec execs the target command, replacing the process
              image. The final cmdline becomes "kiro-cli ..." and the
              "kirocrew_sandbox" string (only in the -f path arg) is lost.
              Sandbox count will be 0 even when sandboxes are running.
    """
    try:
        _my = os.getpid()
        _counts: dict[str, int] = {"sandbox": 0, "kiro_cli": 0}
        _seen: set[str] = set()
        if sys.platform == "linux":
            for d in os.listdir("/proc"):
                if not d.isdigit() or int(d) == _my:
                    continue
                try:
                    cmd = Path(f"/proc/{d}/cmdline").read_bytes()
                    matched = False
                    if b"kirocrew_sandbox" in cmd:
                        _counts["sandbox"] += 1
                        matched = True
                    if b"kiro-cli" in cmd:
                        _counts["kiro_cli"] += 1
                        matched = True
                    if matched:
                        _seen.add(d)
                except OSError:
                    pass
        else:
            _sigs = {
                "kirocrew_sandbox": "sandbox",
                "kiro-cli": "kiro_cli",
            }
            try:
                out = subprocess.check_output(
                    ["ps", "-eo", "pid,command"],
                    timeout=5,
                    text=True,
                )
                for line in out.splitlines():
                    parts = line.split(None, 1)
                    if len(parts) < 2:
                        continue
                    pid_s, cmd = parts
                    if pid_s.strip() == str(_my):
                        continue
                    matched = False
                    for sig, key in _sigs.items():
                        if sig in cmd:
                            _counts[key] += 1
                            matched = True
                    if matched:
                        _seen.add(pid_s.strip())
            except Exception:
                pass
        return {"mcp_processes": _counts, "mcp_total": len(_seen)}
    except Exception:
        return {"mcp_processes": {"sandbox": 0, "kiro_cli": 0}, "mcp_total": 0}


def _apply_mcp_process_counts(data: dict[str, object]) -> None:
    """Merge the (separately cached) process counts into a metrics payload.

    Cached rather than recomputed so the expensive scan runs on its own slow
    cadence while the live numbers around it stay at the graph's refresh rate.
    Called from the sampling thread, and the write is a single rebind of two
    module globals, so no lock is needed: a racing pair of samplers publishes
    one consistent snapshot or the other, never a mixture.
    """
    global _proc_scan_cache, _proc_scan_cache_ts
    now = time.monotonic()
    if not _proc_scan_cache or now - _proc_scan_cache_ts >= _PROC_SCAN_CACHE_TTL:
        _proc_scan_cache = _scan_mcp_processes()
        _proc_scan_cache_ts = now
    data.update(_proc_scan_cache)


def _collect_system_metrics() -> dict[str, object]:
    """Collect system metrics synchronously (runs in thread pool).

    All subprocess calls and blocking I/O are isolated here so the
    asyncio event loop stays responsive.
    """
    data: dict[str, object] = dict(_get_static_system_info())

    # Process memory. `proc_mem_mb` is the LIVE resident set (falls when memory
    # is released); `proc_mem_peak_mb` is the high-water mark since start, kept
    # as a separate reading so a transient spike stays diagnosable without the
    # live figure inheriting it.
    try:
        data["proc_mem_mb"] = round(platform_compat.proc_rss_bytes() / (1024 * 1024), 1)
    except Exception:
        data["proc_mem_mb"] = 0
    try:
        data["proc_mem_peak_mb"] = round(platform_compat.proc_peak_rss_bytes() / (1024 * 1024), 1)
    except Exception:
        data["proc_mem_peak_mb"] = 0

    # System-wide memory — cross-platform
    try:
        if sys.platform == "darwin":
            out = subprocess.check_output([_SYSCTL, "-n", "hw.memsize"], timeout=2).decode().strip()
            total_bytes = int(out)
            data["mem_total_gb"] = round(total_bytes / (1024**3), 1)
            vm = subprocess.check_output([_VM_STAT], timeout=2).decode()
            mem_used, mem_free = _macos_memory_gb(total_bytes, vm)
            data["mem_used_gb"] = mem_used
            data["mem_free_gb"] = mem_free
        elif sys.platform == "win32":
            mem = platform_compat.system_memory()
            if mem:
                total_bytes, avail_bytes = mem
                mem_total = round(total_bytes / (1024**3), 1)
                mem_free = round(avail_bytes / (1024**3), 1)
                data["mem_total_gb"] = mem_total
                data["mem_free_gb"] = mem_free
                data["mem_used_gb"] = round(mem_total - mem_free, 1)
        else:
            with open("/proc/meminfo") as f:
                meminfo: dict[str, int] = {}
                for line in f:
                    parts = line.split()
                    meminfo[parts[0].rstrip(":")] = int(parts[1])
                mem_total = round(meminfo.get("MemTotal", 0) / (1024**2), 1)
                # Prefer the kernel's own MemAvailable (Linux 3.14+): it already
                # accounts for reclaimable page cache AND reclaimable slab
                # (SReclaimable), so "used" matches `free`'s accounting. The old
                # MemFree+Buffers+Cached estimate omitted SReclaimable and could
                # over-report "used" by the whole slab cache (tens of GB on hosts
                # with large dentry/inode caches). Fall back to the estimate
                # (now including SReclaimable) on kernels without MemAvailable.
                if "MemAvailable" in meminfo:
                    mem_free = round(meminfo["MemAvailable"] / (1024**2), 1)
                else:
                    mem_free = round(
                        (
                            meminfo.get("MemFree", 0)
                            + meminfo.get("Buffers", 0)
                            + meminfo.get("Cached", 0)
                            + meminfo.get("SReclaimable", 0)
                        )
                        / (1024**2),
                        1,
                    )
                data["mem_total_gb"] = mem_total
                data["mem_free_gb"] = mem_free
                data["mem_used_gb"] = round(mem_total - mem_free, 1)
    except Exception:
        pass

    # CPU usage
    cores = os.cpu_count() or 1
    try:
        load1, load5, load15 = os.getloadavg()
        data["load_1m"] = round(load1, 2)
        data["load_5m"] = round(load5, 2)
        data["load_15m"] = round(load15, 2)
    except Exception:
        pass
    # Prefer the /proc/stat interval delta (no subprocess, counts iowait+steal);
    # fall back to the ps lifetime-average on non-Linux or the first sample.
    # Windows has no /proc or ps — use the GetSystemTimes delta via ctypes.
    if sys.platform == "win32":
        global _last_win_cpu_pct
        win_cpu = platform_compat.system_cpu_percent()
        if win_cpu is not None:
            _last_win_cpu_pct = win_cpu
        cpu_pct = _last_win_cpu_pct
    else:
        cpu_pct = _system_cpu_pct_from_proc_stat()
        if cpu_pct is None:
            try:
                ps_cpu = subprocess.check_output(
                    ["ps", "-A", "-o", "%cpu"], timeout=2, stderr=subprocess.DEVNULL
                ).decode()
                total_cpu = sum(float(x) for x in ps_cpu.strip().splitlines()[1:] if x.strip())
                cpu_pct = min(100.0, round(total_cpu / cores, 1))
            except Exception:
                cpu_pct = 0
    data["cpu_pct"] = cpu_pct

    # Local IP address
    try:
        # Context manager guarantees the socket fd is closed on every path,
        # including when connect()/getsockname() raise (CWE-772 fd leak).
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            data["ip"] = s.getsockname()[0]
    except Exception:
        data["ip"] = "127.0.0.1"

    # Network bytes + speed — cross-platform
    try:
        rx_total = 0
        tx_total = 0
        if sys.platform == "darwin":
            out = subprocess.check_output([_NETSTAT, "-ib"], timeout=2).decode()
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 10 and parts[2] != "<Link#0>":
                    try:
                        rx_total += int(parts[6])
                        tx_total += int(parts[9])
                    except (ValueError, IndexError):
                        pass
        else:
            with open("/proc/net/dev") as f:
                for line in f:
                    if ":" in line:
                        parts = line.split(":")[1].split()
                        rx_total += int(parts[0])
                        tx_total += int(parts[8])
        rx_mb = round(rx_total / (1024**2), 1)
        tx_mb = round(tx_total / (1024**2), 1)
        data["net_rx_mb"] = rx_mb
        data["net_tx_mb"] = tx_mb

        now = time.monotonic()
        if _prev_net["ts"] > 0:
            dt = now - _prev_net["ts"]
            if dt > 0.1:
                _net_speed["rx_kbs"] = round(((rx_mb - _prev_net["rx"]) * 1024) / dt, 1)
                _net_speed["tx_kbs"] = round(((tx_mb - _prev_net["tx"]) * 1024) / dt, 1)
        _prev_net["rx"] = rx_mb
        _prev_net["tx"] = tx_mb
        _prev_net["ts"] = now
        data["net_rx_kbs"] = max(0, _net_speed["rx_kbs"])
        data["net_tx_kbs"] = max(0, _net_speed["tx_kbs"])
    except Exception:
        pass

    # Disk — cross-platform (shutil.disk_usage works on all platforms)
    try:
        disk_total_v, _used, disk_free_v = shutil.disk_usage("/")
        data["disk_total_gb"] = round(disk_total_v / (1024**3), 1)
        data["disk_free_gb"] = round(disk_free_v / (1024**3), 1)
    except Exception:
        pass

    # Process monitoring
    try:
        data["thread_count"] = threading.active_count()
    except Exception:
        data["thread_count"] = 0
    try:
        cpu_total = platform_compat.proc_cpu_seconds()
        now_mono = time.monotonic()
        global _proc_cpu_pct
        if _prev_cpu["ts"] > 0:
            dt = now_mono - _prev_cpu["ts"]
            if dt > 0.1:
                cpu_delta = cpu_total - _prev_cpu["total"]
                _proc_cpu_pct = min(100.0, round(cpu_delta / dt * 100, 1))
        _prev_cpu["total"] = cpu_total
        _prev_cpu["ts"] = now_mono
        data["proc_cpu_pct"] = _proc_cpu_pct
    except Exception:
        data["proc_cpu_pct"] = 0
    try:
        my_pid = os.getpid()
        if sys.platform == "darwin":
            ps_out = subprocess.check_output(
                ["pgrep", "-P", str(my_pid)], timeout=2, stderr=subprocess.DEVNULL
            ).decode()
            child_pids = [p.strip() for p in ps_out.splitlines() if p.strip()]
        else:
            task_dir = Path(f"/proc/{my_pid}/task")
            child_pids = [d.name for d in task_dir.iterdir()] if task_dir.exists() else []
        data["child_processes"] = len(child_pids)
    except Exception:
        data["child_processes"] = 0

    _apply_mcp_process_counts(data)

    # In-process embedder monitoring — the model runs inside the gateway process
    # now (no external server), so report functional availability instead of a
    # separate PID. Field names ollama_running/ollama_remote are kept for
    # frontend compatibility. "Running" = embeddings are usable: the model file
    # is present (it lazy-loads into memory on first embed) or already loaded.
    try:
        embedder = get_shared_embedder()
        data["ollama_running"] = model_file_present() or embedder.is_ready()
        data["ollama_remote"] = False
        data["embedding_model_loaded"] = embedder.is_ready()
    except Exception:
        data["ollama_running"] = False

    # Resource posture — advisory probe from the same cgroup-aware memory reader
    # that drives the dynamic sub-agent cap and the injected [RESOURCES] line.
    try:
        from kiro_crew.resource_status import probe as _resource_probe
        from kiro_crew.subagent import compute_max_subagents

        status = _resource_probe()
        data["resource_posture"] = status.posture
        data["resource_available_gb"] = round(status.available_gb, 2)
        data["resource_pressure_gb"] = status.pressure_gb
        data["resource_critical_gb"] = status.critical_gb
        try:
            cfg = KiroCrewConfig.load()
            data["subagent_cap"] = compute_max_subagents(cfg)
        except Exception:
            # Fallback: derive from available memory directly
            if status.available_gb > 0:
                data["subagent_cap"] = min(11, max(1, int(status.available_gb / 0.5)))
            else:
                data["subagent_cap"] = 3
    except Exception:
        data["resource_posture"] = "unknown"
        data["resource_available_gb"] = -1.0
        data["resource_pressure_gb"] = 4.0
        data["resource_critical_gb"] = 2.0
        data["subagent_cap"] = 3

    return data


# Cached system metrics (avoid subprocess spawning on every poll)
_metrics_cache: dict[str, object] = {}
_metrics_cache_ts: float = 0.0
_METRICS_CACHE_TTL = 2.0  # seconds

#: Guards a single in-flight collection. The TTL alone does NOT bound the work:
#: it is only stamped AFTER a collection returns, so while one is still running
#: every further poll saw a stale cache and launched its own. On a host where a
#: collection is cheap (Linux: pure ``/proc`` reads) that never showed; on a host
#: where it spawns several subprocesses the duplicate collections stack up,
#: because the dashboard polls this endpoint at exactly the TTL. Coalescing makes
#: concurrent pollers await the SAME collection, so N tabs and a slow host cost
#: one collection, not N.
_metrics_lock: asyncio.Lock | None = None


async def api_system(request: web.Request) -> web.Response:
    """System information endpoint with live CPU, memory, network metrics.

    Caches results briefly and coalesces concurrent collections, so several
    dashboard tabs polling at once cost one collection rather than one each.
    """
    global _metrics_cache, _metrics_cache_ts, _metrics_lock
    now = time.monotonic()
    if now - _metrics_cache_ts < _METRICS_CACHE_TTL and _metrics_cache:
        return web.json_response(_metrics_cache)
    if _metrics_lock is None:
        _metrics_lock = asyncio.Lock()
    async with _metrics_lock:
        # Re-check under the lock: whoever held it may have just refreshed, and
        # this waiter wants that result rather than a second collection of its own.
        now = time.monotonic()
        if now - _metrics_cache_ts < _METRICS_CACHE_TTL and _metrics_cache:
            return web.json_response(_metrics_cache)
        loop = asyncio.get_running_loop()
        # subprocess_executor (mc-subproc), NOT the default pool: this is
        # browser-triggered on a 2s poll and spawns up to six subprocesses on a
        # host without /proc, so leaving it in the default pool let it contend
        # with the getaddrinfo calls the event loop files there.
        data = await loop.run_in_executor(subprocess_executor(), _collect_system_metrics)
        _metrics_cache = data
        _metrics_cache_ts = time.monotonic()
    return web.json_response(data)


async def api_sso_ttl(request: web.Request) -> web.Response:
    """SSO credential TTL — SSH cert primary, cookie fallback."""
    # Identity status via the active PlatformContext.  The Default adapter
    # delegates to ``sso_status.sso_status()`` — the real SSO SSH-cert probe,
    # which spawns up to 4 subprocesses (5s timeout each) — while the Amazon
    # companion returns the real SSO TTL.  ``IdentityProvider.status()`` is
    # SYNCHRONOUS and BLOCKING in both editions, so it must be offloaded to a
    # thread-pool executor to avoid stalling the aiohttp event loop (matches the
    # legacy ``sso_status_async()`` which wrapped sso_status() this way).
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, current_context().identity.status)
    return web.json_response(data)


async def api_compliance_yolo_status(request: web.Request) -> web.Response:
    """GET /api/admin/compliance/yolo-status — safety override governance status."""
    status = safety_override().status()
    return web.json_response(asdict(status))


def _channel_members() -> tuple[str, ...]:
    """Canonical ``channel_type`` ids for the messaging channels, derived from
    the builtin channel registry — the single source of truth — so this list
    can never drift from the channels themselves. Imported here (off the event
    loop, inside the executor worker) so the channel modules' own imports don't
    run on the aiohttp loop; the roster is cached after the first call.
    """
    from kiro_crew.channels import builtin_channel_descriptors
    from kiro_crew.messaging.registry import governed_members

    return governed_members(builtin_channel_descriptors())


def _collect_channel_governance() -> dict[str, object]:
    """Resolve the effective ``channels`` policy decision for every transport.

    Returns ``{channel_type: true|false|null}`` — ``true`` permitted, ``false``
    denied by policy, ``null`` when governance evaluation transiently FAILED (the
    UI renders ``null`` as "policy status unavailable", never "Off by admin").

    Runs in a thread-pool executor (``governance_permits`` may read profile
    files from disk via the ProfileStore). ``session_key=HOST_SESSION_KEY``
    binds the host surface, matching the messaging chokepoint
    (``mcp_core._vet_channel_governance``) and the app-activation gate.

    Byte-identical default: with NO policy governing ``channels`` (the standard
    OSS build), ``governance_permits`` returns ``permitted=True`` for every
    member, so this returns all-true and the Settings UI is unchanged.
    """
    from kiro_crew.platform.governance_profiles import (
        GOVERNANCE_ERROR_REASON,
        HOST_SESSION_KEY,
        governance_permits,
    )

    result: dict[str, object] = {}
    for member in _channel_members():
        # fail_closed=True so a genuine governance-evaluation ERROR denies (agrees
        # with the connect-time startup gate). But for the read-only DISPLAY we
        # must distinguish a real POLICY deny (``false`` → "Off by admin") from a
        # transient EVALUATION error (which fail-closed also renders as a denying
        # Decision): mislabeling a transient failure as an explicit admin denial is
        # misleading. ``governance_permits`` marks an eval-error degrade with
        # ``rule == "default"`` AND a ``GOVERNANCE_ERROR_REASON`` reason (a real deny
        # carries a governing rule/layer), so we surface those as ``null`` → the UI
        # shows "policy status unavailable", not "Off by admin". Matching the shared
        # constant (not a hand-copied string) keeps this in lockstep with the reason
        # the evaluator emits. Byte-identical on the no-policy default build: every
        # member is a permitting Decision → ``true``.
        decision = governance_permits(
            "channels", member, session_key=HOST_SESSION_KEY, fail_closed=True
        )
        permitted = bool(getattr(decision, "permitted", False))
        reason = str(getattr(decision, "reason", "") or "")
        if not permitted and GOVERNANCE_ERROR_REASON in reason:
            result[member] = None  # transient eval failure → "unavailable", not a deny
        else:
            result[member] = permitted
    return result


async def api_governance_channels(request: web.Request) -> web.Response:
    """GET /api/governance/channels — effective per-channel policy decision.

    Returns a ``{channel_type: true|false|null}`` map (``true`` permitted,
    ``false`` denied by the ``channels`` governance policy, ``null`` governance
    evaluation transiently failed → "unavailable"). The Settings UI greys out and
    disables a policy-denied channel tab ("Off by admin") rather than hiding it.
    Read-only; behind the same dashboard token auth as the sibling GETs.

    Offloaded to the dedicated ``governance_executor`` (``mc-gov``), NOT the shared
    default executor: this walks the ProfileStore (filesystem) and is browser-
    triggerable, so several concurrent requests on a slow profile FS would
    otherwise pin the default-pool workers the event loop shares for DNS.
    """
    from kiro_crew.executors import governance_executor

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(governance_executor(), _collect_channel_governance)
    return web.json_response(data)
