#!/usr/bin/env python3
"""MeshClaw → KiroCrew migration — command-line entry point.

Usage:
    python transfer/meshclaw_to_kirocrew/migrate.py [--dry-run] [--yes]

Requires KiroCrew to be installed (the engine imports ``kiro_crew`` so the
target path resolution matches the app, including ``KIROCREW_HOME``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the sibling engine importable whether run as a file or a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import engine  # noqa: E402


def _fmt_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.1f} {units[i]}" if i else f"{int(v)} {units[i]}"


def _print_summary(res: dict) -> None:
    if not res.get("ok"):
        print(f"\n✗ Migration failed: {res.get('error')}")
        return
    head = "Dry run (nothing written)" if res.get("dry_run") else "Migration complete"
    print(f"\n✓ {head}")
    for name, n in (res.get("copied_dirs") or {}).items():
        if n:
            print(f"  • {name}: {n} files")
    files = res.get("copied_files") or []
    if files:
        print(f"  • config/state files: {', '.join(files)}")
    mem = res.get("memory_added") or {}
    if any(mem.values()):
        print(f"  • memory records added: {sum(mem.values())}")
    cfg = res.get("config") or {}
    if cfg.get("filled"):
        print(f"  • config keys filled: {', '.join(cfg['filled'])}")
    if res.get("backup_path"):
        print(f"  • backup of ~/.kirocrew: {res['backup_path']}")
    for w in res.get("warnings") or []:
        print(f"  ⚠ {w}")
    if not res.get("dry_run"):
        print("\nRestart KiroCrew to load the imported data.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Migrate MeshClaw data into KiroCrew.")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing anything.")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = p.parse_args(argv)

    info = engine.detect()
    if not info.get("available"):
        print(f"No MeshClaw install found at {info.get('source')}. Nothing to migrate.")
        return 0

    s = info.get("summary", {})
    mem_total = sum((s.get("memory") or {}).values())
    print(f"Found MeshClaw data at {info['source']}:")
    print(f"  • {s.get('sessions', 0)} sessions ({_fmt_bytes(s.get('session_bytes', 0))})")
    print(f"  • {mem_total} memory records")
    print(f"  • {_fmt_bytes(s.get('total_bytes', 0))} total")
    print(f"Target: {info.get('target')}")
    print("Your current ~/.kirocrew is backed up first; ~/.meshclaw is never modified.")

    if args.dry_run:
        res = engine.migrate(dry_run=True)
        _print_summary(res)
        return 0 if res.get("ok") else 1

    if not args.yes:
        reply = input("\nProceed with migration? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 1

    res = engine.migrate(dry_run=False)
    _print_summary(res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
