"""MeshClaw → KiroCrew data migration engine.

Pure logic (no aiohttp / dashboard deps) so it can be unit-tested and reused by
the CLI.  Brings a user's data from the legacy ``~/.meshclaw`` install into the
current KiroCrew home (``~/.kirocrew``, honoring ``KIROCREW_HOME``).

Design notes (see ``docs/system-specs/post-launch-removals.md``):

* The DB schemas are identical between the two installs, so memory migration is
  a copy (target empty) or a row-level ``INSERT OR IGNORE`` merge.
* The source is treated as **read-only** — we never write to ``~/.meshclaw``.
* ``~/.kirocrew`` is backed up before any write so the operation is reversible.
* Per-install identity/crypto files and runtime state are **never** copied.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew.config.loader import config_dir
from kiro_crew.config.paths import MIGRATION_MARKER_NAME

logger = logging.getLogger(__name__)

# Internal sentinels the data-home layer writes into the home (not user data):
# the ``config_dir()`` fresh-install completion marker. A home containing only
# these is treated as empty by the backup check.
_INTERNAL_SENTINELS: frozenset[str] = frozenset({MIGRATION_MARKER_NAME})

# ── What to migrate ──────────────────────────────────────────────────────────

# User-data directories copied wholesale (union by filename; existing target
# files are preserved unless the target dir is empty).
COPY_DIRS: tuple[str, ...] = (
    "sessions",
    "workspace",
    "uploads",
    "skills",
    "artifacts",
    "tasks",
    "plan_memory",
    "agent-metadata",
    "apps",
    "cron-history",
)

# User-data JSON/state files copied if absent in target.  Path-rewritten on copy.
COPY_FILES: tuple[str, ...] = (
    "crons.json",
    "session_map.json",
    "tags.json",
    "autonudge.json",
    "hooks.json",
    "folders.json",
    "notifications.jsonl",
    "playwright-config.json",
)

# Per-install identity / crypto — copying these breaks auth or leaks secrets.
SKIP_FILES: frozenset[str] = frozenset(
    {
        ".env",
        ".local_secret",
        "sel_hmac.key",
        "token_signing.key",
        "telemetry_salt",
    }
)

# Runtime / process state — never meaningful in a different install.
SKIP_GLOBS: tuple[str, ...] = (
    "*.lock",
    "*_pids.txt",
    "*_pids.lock",
    "session_pid_*.txt",
    "*.log",
    "*.log.*",
)

# config.json keys that hold user data and may be filled in from MeshClaw.
CONFIG_USER_KEYS: tuple[str, ...] = (
    "agents",
    "workspaces",
    "default_workspace",
    "default_agent",
    "memory_stores",
    "default_memory_store",
    "skills",
    "hooks",
    "taskrunner",
    "orchestrator",
    "slack",
    "stt",
    "timezone",
    "secretary",
    "taskkeeper",
)

# Mesh-only legacy config keys that no longer exist in the KiroCrew schema.
CONFIG_DROP_KEYS: tuple[str, ...] = ("mcp_gateway", "tunnel")

# Memory tables merged row-level when the target DB already has data.
_MEMORY_TABLES: tuple[str, ...] = (
    "semantic_memory",
    "episodic_memories",
    "memory_events",
)


def meshclaw_dir() -> Path:
    """Legacy MeshClaw home.  Read-only source for migration."""
    return Path.home() / ".meshclaw"


def kirocrew_dir() -> Path:
    """Current KiroCrew home (honors ``KIROCREW_HOME``)."""
    return config_dir()


# ── Detection ────────────────────────────────────────────────────────────────


def _dir_stats(path: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) for *path*, 0/0 if missing."""
    if not path.is_dir():
        return (0, 0)
    files = 0
    size = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                files += 1
                size += p.stat().st_size
        except OSError:
            continue
    return (files, size)


def _memory_counts(db: Path) -> dict[str, int]:
    """Row counts per memory table, or zeros if the DB is missing/unreadable."""
    counts = {t: 0 for t in _MEMORY_TABLES}
    if not db.is_file():
        return counts
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for t in _MEMORY_TABLES:
                try:
                    counts[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                except sqlite3.Error:
                    counts[t] = 0
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return counts


def detect() -> dict[str, Any]:
    """Report whether a MeshClaw install exists and what it holds."""
    src = meshclaw_dir()
    if not src.is_dir():
        return {"available": False, "source": str(src)}

    sessions = src / "sessions"
    sess_files, sess_bytes = _dir_stats(sessions)
    mem = _memory_counts(src / "memory.db")
    _, total_bytes = _dir_stats(src)

    return {
        "available": True,
        "source": str(src),
        "target": str(kirocrew_dir()),
        "summary": {
            "sessions": sess_files,
            "session_bytes": sess_bytes,
            "memory": mem,
            "total_bytes": total_bytes,
        },
    }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _is_skipped(name: str) -> bool:
    if name in SKIP_FILES:
        return True
    from fnmatch import fnmatch

    return any(fnmatch(name, g) for g in SKIP_GLOBS)


def _rewrite_paths(text: str, src: Path, dst: Path) -> str:
    """Rewrite absolute MeshClaw paths in copied JSON text to the target home.

    First swap the exact source-home prefix for the resolved target home. Then a
    fallback pass rewrites any residual ``/.meshclaw/`` substring (a differently
    rooted legacy path the exact-prefix swap missed) to the target home's home-
    relative form. The data home moved to ``~/.kiro/crew``, so the fallback is
    derived from ``dst`` (``/.kiro/crew/``) rather than a hardcoded ``/.kirocrew/``
    — hardcoding the old top-level name would rewrite paths into a home KiroCrew
    no longer reads.
    """
    text = text.replace(str(src), str(dst))
    try:
        dst_rel = dst.relative_to(Path.home())
        fallback = f"/{dst_rel.as_posix()}/"
    except ValueError:
        fallback = f"/{dst.name}/"
    return text.replace("/.meshclaw/", fallback)


def _backup_target(dst: Path) -> Path | None:
    """Snapshot the current KiroCrew home next to it.  Returns backup path.

    Treats a home containing ONLY internal sentinels (the data-home migration's
    ``.data-home-ready`` marker) as empty — that marker is written by
    ``config_dir()`` on a fresh install and is not user data worth backing up;
    without this, resolving ``kirocrew_dir()`` alone would make an otherwise-empty
    target look non-empty and trigger a spurious backup.
    """
    if not dst.is_dir():
        return None
    real_entries = [c for c in dst.iterdir() if c.name not in _INTERNAL_SENTINELS]
    if not real_entries:
        return None
    # Collision-safe: a same-second second call must not hit "File exists" on the
    # timestamped path (copytree refuses an existing dest). Add a uniquifier.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = dst.parent / f"{dst.name}.backup-{ts}"
    n = 1
    while backup.exists():
        backup = dst.parent / f"{dst.name}.backup-{ts}-{n}"
        n += 1
    shutil.copytree(dst, backup, symlinks=True, ignore_dangling_symlinks=True)
    return backup


def _copy_dir_union(src: Path, dst: Path, *, dry_run: bool) -> int:
    """Copy files from *src* into *dst*, never overwriting existing files.

    Returns the number of files that were (or would be) copied.
    """
    if not src.is_dir():
        return 0
    copied = 0
    for sp in src.rglob("*"):
        if not sp.is_file():
            continue
        if _is_skipped(sp.name):
            continue
        rel = sp.relative_to(src)
        dp = dst / rel
        if dp.exists():
            continue
        copied += 1
        if not dry_run:
            dp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp, dp)
    return copied


def _merge_memory_db(src_db: Path, dst_db: Path, *, dry_run: bool) -> dict[str, int]:
    """Copy (target empty) or row-merge the memory DB.  Returns rows added per table."""
    added = {t: 0 for t in _MEMORY_TABLES}
    if not src_db.is_file():
        return added

    dst_counts = _memory_counts(dst_db)
    target_empty = sum(dst_counts.values()) == 0

    if target_empty:
        src_counts = _memory_counts(src_db)
        if not dry_run:
            dst_db.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_db, dst_db)
        return dict(src_counts)

    # Row-level merge via ATTACH + INSERT OR IGNORE (PK collisions skipped).
    if dry_run:
        src_counts = _memory_counts(src_db)
        for t in _MEMORY_TABLES:
            added[t] = max(src_counts[t] - dst_counts[t], 0)
        return added

    conn = sqlite3.connect(dst_db)
    try:
        conn.execute("ATTACH DATABASE ? AS mc", (str(src_db),))
        for t in _MEMORY_TABLES:
            before = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            try:
                conn.execute(f"INSERT OR IGNORE INTO {t} SELECT * FROM mc.{t}")
            except sqlite3.Error as e:
                logger.warning("memory merge skipped table %s: %s", t, e)
                continue
            after = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            added[t] = after - before
        conn.commit()
        conn.execute("DETACH DATABASE mc")
    finally:
        conn.close()
    return added


def _merge_config(src: Path, dst: Path, *, dry_run: bool) -> dict[str, Any]:
    """Merge user-data keys from MeshClaw config into the KiroCrew config.

    KiroCrew's install-specific values win; only keys that are missing/empty in
    the target are filled from the source.  Legacy mesh-only keys are dropped.
    """
    result: dict[str, Any] = {"filled": [], "dropped": []}
    src_cfg_path = src / "config.json"
    dst_cfg_path = dst / "config.json"
    if not src_cfg_path.is_file():
        return result
    try:
        src_cfg = json.loads(src_cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result
    dst_cfg: dict[str, Any] = {}
    if dst_cfg_path.is_file():
        try:
            dst_cfg = json.loads(dst_cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            dst_cfg = {}

    for key in CONFIG_USER_KEYS:
        if key not in src_cfg:
            continue
        if key not in dst_cfg or not dst_cfg.get(key):
            result["filled"].append(key)
            if not dry_run:
                dst_cfg[key] = src_cfg[key]

    result["dropped"] = [k for k in CONFIG_DROP_KEYS if k in src_cfg]

    if not dry_run and result["filled"]:
        # Rewrite any embedded mesh paths before persisting.
        text = _rewrite_paths(json.dumps(dst_cfg, indent=2), src, dst)
        dst_cfg_path.write_text(text, encoding="utf-8")
    return result


# ── Top-level migration ────────────────────────────────────────────────────


def migrate(*, dry_run: bool = False) -> dict[str, Any]:
    """Run the MeshClaw → KiroCrew migration.

    Returns a structured summary: backup path, per-category copy counts,
    memory rows added, config keys filled/dropped, and any warnings.
    """
    src = meshclaw_dir()
    dst = kirocrew_dir()
    warnings: list[str] = []

    if not src.is_dir():
        return {"ok": False, "error": f"No MeshClaw install found at {src}"}
    if src.resolve() == dst.resolve():
        return {"ok": False, "error": "Source and target are the same directory"}

    backup_path: str | None = None
    if not dry_run:
        try:
            bp = _backup_target(dst)
            backup_path = str(bp) if bp else None
        except OSError as e:
            return {"ok": False, "error": f"Backup failed, aborting: {e}"}

    copied_dirs: dict[str, int] = {}
    for name in COPY_DIRS:
        try:
            copied_dirs[name] = _copy_dir_union(src / name, dst / name, dry_run=dry_run)
        except OSError as e:
            warnings.append(f"{name}: {e}")
            copied_dirs[name] = 0

    copied_files: list[str] = []
    for name in COPY_FILES:
        sp = src / name
        dp = dst / name
        if not sp.is_file() or dp.exists():
            continue
        copied_files.append(name)
        if not dry_run:
            try:
                text = _rewrite_paths(sp.read_text(encoding="utf-8", errors="replace"), src, dst)
                dp.write_text(text, encoding="utf-8")
            except OSError as e:
                warnings.append(f"{name}: {e}")

    try:
        memory_added = _merge_memory_db(src / "memory.db", dst / "memory.db", dry_run=dry_run)
    except sqlite3.Error as e:
        warnings.append(f"memory.db: {e}")
        memory_added = {t: 0 for t in _MEMORY_TABLES}

    # memory_index.db is an FTS index rebuilt from memory.db on next load;
    # we intentionally do not copy stale WAL state.
    config_result = _merge_config(src, dst, dry_run=dry_run)
    if config_result["dropped"]:
        warnings.append("Dropped legacy config keys: " + ", ".join(config_result["dropped"]))

    return {
        "ok": True,
        "dry_run": dry_run,
        "source": str(src),
        "target": str(dst),
        "backup_path": backup_path,
        "copied_dirs": copied_dirs,
        "copied_files": copied_files,
        "memory_added": memory_added,
        "config": config_result,
        "warnings": warnings,
    }
