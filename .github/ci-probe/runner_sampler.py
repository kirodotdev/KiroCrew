"""Throwaway runner-utilization sampler (ci-probe/runner-utilization).

Writes one JSON line every INTERVAL seconds to OUT until STOP exists.
Runs from its own venv with psutil so the test environment is untouched.
"""

import json
import os
import platform
import sys
import time

import psutil


def _header():
    info = {
        "kind": "header",
        "t": time.time(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_logical": psutil.cpu_count(logical=True),
        "cpu_physical": psutil.cpu_count(logical=False),
        "mem_total_mib": psutil.virtual_memory().total // (1024 * 1024),
        "swap_total_mib": psutil.swap_memory().total // (1024 * 1024),
        "disks": [p._asdict() for p in psutil.disk_partitions(all=False)],
    }
    try:
        freq = psutil.cpu_freq()
        info["cpu_freq_mhz"] = freq.current if freq else None
    except Exception:  # noqa: BLE001 - best effort only
        info["cpu_freq_mhz"] = None
    if os.path.exists("/proc/cpuinfo"):
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            text = handle.read()
        models = [
            line.split(":", 1)[1].strip()
            for line in text.splitlines()
            if line.startswith("model name")
        ]
        info["cpu_model"] = models[0] if models else None
        info["cpuinfo_flags_hypervisor"] = " hypervisor" in text
    return info


def _proc_stat():
    out = {}
    try:
        with open("/proc/stat", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(("procs_running", "procs_blocked")):
                    key, value = line.split()
                    out[key] = int(value)
    except OSError:
        pass
    for res in ("cpu", "io", "memory"):
        try:
            with open(f"/proc/pressure/{res}", encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split()
                    total = [p for p in parts if p.startswith("total=")]
                    if total:
                        out[f"psi_{res}_{parts[0]}_total_us"] = int(total[0].split("=", 1)[1])
        except OSError:
            pass
    return out


def _python_procs():
    count = 0
    rss = 0
    for proc in psutil.process_iter(["name", "memory_info"]):
        name = (proc.info.get("name") or "").lower()
        if name.startswith("python"):
            count += 1
            mem = proc.info.get("memory_info")
            if mem is not None:
                rss += mem.rss
    return count, rss // (1024 * 1024)


def main():
    out_path, stop_path = sys.argv[1], sys.argv[2]
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0
    with open(out_path, "a", encoding="utf-8") as out:
        out.write(json.dumps(_header()) + "\n")
        out.flush()
        psutil.cpu_percent(percpu=True)
        psutil.cpu_times_percent()
        last_disk = psutil.disk_io_counters()
        last_t = time.time()
        while not os.path.exists(stop_path):
            time.sleep(interval)
            now = time.time()
            disk = psutil.disk_io_counters()
            vm = psutil.virtual_memory()
            row = {
                "kind": "sample",
                "t": now,
                "cpu_pct": psutil.cpu_percent(percpu=True),
                "cpu_times_pct": psutil.cpu_times_percent()._asdict(),
                "mem_used_mib": (vm.total - vm.available) // (1024 * 1024),
                "mem_avail_mib": vm.available // (1024 * 1024),
                "swap_used_mib": psutil.swap_memory().used // (1024 * 1024),
            }
            if disk is not None and last_disk is not None:
                dt = max(now - last_t, 1e-6)
                cur = disk._asdict()
                prev = last_disk._asdict()
                row["disk_delta"] = {k: cur[k] - prev[k] for k in cur}
                row["disk_dt"] = dt
            last_disk, last_t = disk, now
            try:
                row["loadavg"] = os.getloadavg()
            except (AttributeError, OSError):
                pass
            row.update(_proc_stat())
            row["py_procs"], row["py_rss_mib"] = _python_procs()
            out.write(json.dumps(row) + "\n")
            out.flush()


if __name__ == "__main__":
    main()
