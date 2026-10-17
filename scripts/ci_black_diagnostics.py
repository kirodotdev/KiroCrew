#!/usr/bin/env python3
"""Observe Black's memory without changing its gate verdict."""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path


def read(path, limit=4096):
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read(limit)
    except OSError:
        return ""


def snapshot(phase):
    print(f"black resources {phase}", flush=True)
    # Host meminfo is context, not the container's memory quota.
    for line in read("/proc/meminfo").splitlines():
        if line.startswith(("MemTotal:", "MemAvailable:", "SwapFree:")):
            print(f"host {line}", flush=True)
    groups = [line.split(":", 2) for line in read("/proc/self/cgroup", 16384).splitlines()[:64]]
    seen: set[Path] = set()
    for line in read("/proc/self/mountinfo", 65536).splitlines()[:512]:
        fields = line.split()
        if "-" not in fields:
            continue
        sep = fields.index("-")
        kind = fields[sep + 1]
        if kind not in ("cgroup", "cgroup2"):
            continue
        if kind == "cgroup" and "memory" not in fields[sep + 3].split(","):
            continue
        root, mount = Path(fields[3]), Path(fields[4])
        candidates = [mount]  # Also covers a namespaced container root.
        for group in groups:
            if len(group) != 3:
                continue
            if not (group[1] == "" if kind == "cgroup2" else "memory" in group[1].split(",")):
                continue
            try:
                relative = Path(group[2]).relative_to(root)
            except ValueError:
                continue
            if ".." in relative.parts:
                continue  # A namespace can hide ancestors outside this mount.
            current = mount / relative
            for _ in range(16):
                candidates.append(current)
                if current == mount:
                    break
                current = current.parent
        names = (
            (
                "memory.current",
                "memory.max",
                "memory.peak",
                "memory.events",
                "memory.events.local",
                "cpu.max",
                "pids.current",
                "pids.max",
            )
            if kind == "cgroup2"
            else (
                "memory.usage_in_bytes",
                "memory.limit_in_bytes",
                "memory.max_usage_in_bytes",
                "memory.failcnt",
                "memory.oom_control",
            )
        )
        for directory in candidates:
            if directory in seen or len(seen) >= 32:
                continue
            seen.add(directory)
            for name in names:
                value = read(directory / name).strip()
                if value:
                    print(f"cgroup {directory / name}: {value}", flush=True)
    if sys.platform == "linux":
        import resource

        print(
            f"child_maxrss_kib={resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}", flush=True
        )
    print("cgroup probes are best-effort; absent/hidden limits remain unknown", flush=True)


def diagnose(phase):
    # A diagnostic failure must never prevent or replace the real gate.
    try:
        snapshot(phase)
    except Exception as exc:
        print(f"black resources unavailable: {type(exc).__name__}", flush=True)


def main(gate: str) -> int:
    print(
        f"Python {sys.version.split()[0]}; BLACK_NUM_WORKERS={os.getenv('BLACK_NUM_WORKERS')}",
        flush=True,
    )
    try:
        print(f"Black {importlib.metadata.version('black')}", flush=True)
    except importlib.metadata.PackageNotFoundError:
        print("Black distribution unavailable", flush=True)
    diagnose("before")
    result = subprocess.run([sys.executable, gate])
    diagnose("after")
    print(f"black gate returncode={result.returncode}", flush=True)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
