"""Portable zip export/import for KiroCrew state (dashboard endpoint).

Creates a zip archive of all KiroCrew settings and memory for download
via the dashboard, and restores from uploaded zip archives. Designed to
work over HTTP for remote users (e.g. Linux Cloud Desktop → macOS browser).

Credentials (.env, session secrets) are always excluded from exports.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import socket
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_cron import _log_cron_denial, _vet_shell_command
from kiro_crew.security import is_sensitive_path
from kiro_crew.snapshot import (
    _copy_tree_no_overwrite,
    _do_replace,
    _merge_crons,
    _merge_memory,
    _merge_notifications,
)

logger = logging.getLogger(__name__)

EXPORT_EXCLUDE = frozenset({
    ".env",
    ".local_secret",
    "sel_hmac.key",
    "telemetry_salt",
    # NOTE: the beacon's per-install identity files (beacon_install_id /
    # beacon_last_sent) are deliberately NOT listed here. This set is matched by
    # BASENAME and `_is_excluded` runs over the workspace/, plan_memory/ and
    # skills/ trees, so an entry here would silently drop any USER file that
    # happens to share the name. They need no entry: root-level export is a
    # hard-coded allowlist (config.json, hooks.json, crons.json,
    # notifications.jsonl, project_dir, workspace_dir), so a root beacon file is
    # never selected in the first place.
    "session_map.json",
    "kiro_session_pids.txt",
    "kiro_pids.txt",
})

EXCLUDE_DIRS = frozenset({
    "snapshots",
    "outbox",
    "uploads",
    "__pycache__",
})


def _mc_dir() -> Path:
    return Path(os.environ.get("KIROCREW_HOME", config_dir()))


def _is_excluded(rel_path: PurePosixPath) -> bool:
    if rel_path.name in EXPORT_EXCLUDE:
        return True
    if rel_path.name.endswith(".pid"):
        return True
    for part in rel_path.parts:
        if part in EXCLUDE_DIRS:
            return True
    return False


def _wal_checkpoint(db_path: Path) -> None:
    if db_path.is_file():
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.close()
        except Exception:
            logger.debug("WAL checkpoint failed for %s", db_path)


def _backup_sqlite(src: Path, dst_buffer: io.BytesIO) -> None:
    """Use SQLite backup API for a consistent copy."""
    src_conn = sqlite3.connect(str(src))
    mem_conn = sqlite3.connect(":memory:")
    try:
        src_conn.backup(mem_conn)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        try:
            disk_conn = sqlite3.connect(tmp.name)
            try:
                mem_conn.backup(disk_conn)
            finally:
                disk_conn.close()
            dst_buffer.write(Path(tmp.name).read_bytes())
        finally:
            os.unlink(tmp.name)
    finally:
        src_conn.close()
        mem_conn.close()


def create_export_zip() -> tuple[bytes, dict]:
    """Create a zip archive of KiroCrew state. Returns (zip_bytes, manifest_dict)."""
    mc = _mc_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"kirocrew-export-{ts}"

    _wal_checkpoint(mc / "memory.db")
    _wal_checkpoint(mc / "memory_index.db")

    buf = io.BytesIO()
    contents_summary: dict = {}

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # Core JSON/text files
        for fname in ("config.json", "hooks.json", "crons.json", "notifications.jsonl",
                      "project_dir", "workspace_dir"):
            src = mc / fname
            if src.is_file() and not src.is_symlink():
                zf.write(str(src), f"{prefix}/{fname}")
                contents_summary[fname] = src.stat().st_size

        # SQLite databases via backup API
        for db_name in ("memory.db", "memory_index.db"):
            src = mc / db_name
            if src.is_file() and not src.is_symlink():
                db_buf = io.BytesIO()
                _backup_sqlite(src, db_buf)
                zf.writestr(f"{prefix}/{db_name}", db_buf.getvalue())
                contents_summary[db_name] = db_buf.tell()

        # Directory trees: workspace, plan_memory, skills
        dir_counts: dict[str, int] = {}
        for dirname in ("workspace", "plan_memory", "skills"):
            src_dir = mc / dirname
            count = 0
            if src_dir.is_dir():
                for fpath in src_dir.rglob("*"):
                    if fpath.is_symlink():
                        continue
                    rel = fpath.relative_to(mc)
                    if _is_excluded(PurePosixPath(str(rel))):
                        continue
                    if is_sensitive_path(str(fpath)):
                        continue
                    if dirname == "skills" and "auto" in rel.parts:
                        continue
                    if fpath.is_file():
                        zf.write(str(fpath), f"{prefix}/{rel}")
                        count += 1
            dir_counts[dirname] = count
        contents_summary["workspace_files"] = dir_counts.get("workspace", 0)
        contents_summary["plan_memory_files"] = dir_counts.get("plan_memory", 0)
        contents_summary["skill_count"] = dir_counts.get("skills", 0)

        # Manifest
        manifest = {
            "version": 2,
            "format": "zip",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hostname": socket.gethostname(),
            "user": os.environ.get("USER", "unknown"),
            "contents": contents_summary,
        }
        zf.writestr(f"{prefix}/MANIFEST.json", json.dumps(manifest, indent=2))

    return buf.getvalue(), manifest


# Zip-bomb guards for import archives (CWE-409). A real personal snapshot is far
# below these ceilings; a decompression bomb (huge declared uncompressed size, or
# millions of entries) is rejected before extraction rather than filling the
# host disk.
_MAX_IMPORT_MEMBERS = 50_000
_MAX_IMPORT_UNCOMPRESSED = 2 * 1024 ** 3  # 2 GiB


def _is_link_entry(info: zipfile.ZipInfo) -> bool:
    """True when a zip member declares itself a symlink or hardlink.

    CPython's ``ZipFile.extract`` does NOT honor S_IFLNK -- it writes the link
    target as ordinary file content -- so a link member cannot currently redirect
    a later write outside the extraction root. This guard exists because that is a
    property of the extraction backend rather than of the archive: swapping in
    ``shutil.unpack_archive``, an external ``unzip``, or a future stdlib that
    honors the mode bit would silently turn a link member into a real symlink and
    make the escape reachable (CWE-22 via CWE-59). Rejecting these members keeps
    the guarantee at the archive boundary, where it does not depend on which
    extractor runs. Legitimate archives carry none: the export side skips symlinks.
    """
    mode = info.external_attr >> 16
    return bool(mode) and stat.S_ISLNK(mode)


def validate_import_zip(zip_path: Path) -> tuple[bool, str, dict]:
    """Validate a zip file for import.

    Returns (ok, error_message, manifest_dict).
    """
    try:
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            names = zf.namelist()

            # Check for path traversal
            for name in names:
                parts = PurePosixPath(name).parts
                if ".." in parts or name.startswith("/"):
                    return False, f"Rejected path traversal: {name}", {}

            # Zip-bomb guard: bound entry count and total uncompressed size.
            infos = zf.infolist()
            if len(infos) > _MAX_IMPORT_MEMBERS:
                return False, f"Rejected: archive has too many entries ({len(infos)} > {_MAX_IMPORT_MEMBERS})", {}
            total_uncompressed = sum(i.file_size for i in infos)
            if total_uncompressed > _MAX_IMPORT_UNCOMPRESSED:
                return False, (
                    f"Rejected: uncompressed size {total_uncompressed} exceeds cap "
                    f"{_MAX_IMPORT_UNCOMPRESSED} (possible zip bomb)"
                ), {}

            # Link members can redirect a later write outside the extraction root
            # even when every name passes the traversal check above.
            for info in infos:
                if _is_link_entry(info):
                    return False, f"Rejected link entry: {info.filename}", {}

            # Find manifest
            manifest_entries = [n for n in names if n.endswith("MANIFEST.json")]
            if not manifest_entries:
                return False, "No MANIFEST.json found in archive", {}

            manifest_data = json.loads(zf.read(manifest_entries[0]))
            version = manifest_data.get("version")
            if version not in (1, 2):
                return False, f"Unsupported manifest version: {version}", {}

            return True, "", manifest_data
    except zipfile.BadZipFile:
        return False, "Invalid zip file", {}
    except (json.JSONDecodeError, KeyError) as e:
        return False, f"Invalid manifest: {e}", {}


#: Stands in for a job name when the whole store had to be replaced, so the
#: import summary can say something happened without inventing a name.
_UNREADABLE_STORE = "<the whole cron store was unreadable>"


def _sanitize_imported_crons(crons_path: Path) -> tuple[list[str], list[str]]:
    """Make an imported cron store safe to load and safe to run.

    Returns ``(dropped, paused)`` — two lists, because they are two different
    outcomes and a caller that conflates them tells the user the wrong thing. A
    dropped job is gone; a paused one is fully restored and simply waiting to be
    switched on. Rewrites *crons_path* in place. A missing file is left alone.

    Three rules, each closing a different way an archive can act on the host:

    1. A job that is not an object, or whose ``schedule`` is not one, is DROPPED.
       ``CronService._load`` skips such a record with a warning and it is then
       silently dropped from the store on the next write — dead weight with an
       invisible deadline. Dropping it at import, with the drop REPORTED to the
       user, is the honest version of the same outcome.

    2. A ``command`` is vetted with ``mcp_cron._vet_shell_command``, so it is
       judged exactly as the same command would be at ``cron_add`` (deny-list,
       sensitive-path, credential-path and exfiltration checks). A failure DROPS
       the job, and so does the vet itself raising — an unverifiable command must
       not be scheduled.

    3. A job that survives with a ``command``, and any job naming a ``script``,
       is imported DISABLED (``user_paused``) rather than live, and reported as
       PAUSED, not rejected. The vet bounds what a command may do, not whether the
       user asked for THIS command on THIS machine, and a ``script`` cannot be
       vetted at all: the export never carries the ``crons/`` directory, so the
       name resolves against whatever the target already has there. Both become an
       ambush if they start running on their own. Disabling keeps the restore — the
       jobs, their schedules and their history are all still there — while making
       the first run an explicit human action. Message-only jobs are untouched:
       they prompt an agent, they do not execute anything on the host.
    """
    if not crons_path.is_file():
        return [], []
    try:
        data = json.loads(crons_path.read_text())
    except (ValueError, OSError):
        # Unparseable bytes are not installable as a cron store either, but they
        # are also not something this function can reason about — an empty store
        # is the only safe thing to hand the loader.
        crons_path.write_text(json.dumps({"jobs": []}, indent=2))
        return [_UNREADABLE_STORE], []
    # A store whose top level is not an object, or whose `jobs` is not a list, is
    # REPLACED rather than left alone. `CronService._load` treats such a document
    # as unsalvageable (empty registry, warning) — replacing it here means the
    # user is TOLD the store was unreadable at import time, instead of the
    # gateway silently starting with an empty schedule later.
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        crons_path.write_text(json.dumps({"jobs": []}, indent=2))
        return [_UNREADABLE_STORE], []
    jobs = data["jobs"]

    kept: list = []
    dropped: list[str] = []
    paused: list[str] = []
    changed = False

    def _name_of(job: object) -> str:
        name = job.get("name") if isinstance(job, dict) else None
        return str(name) if name else "<unnamed>"

    for job in jobs:
        # Rule 1: a shape unfit for the store must not survive the import. This
        # predicate is deliberately STRICTER than the loader's (it also demands
        # str-typed id/name/message, which `_job_from_record` would accept
        # untyped): the loader skips a bad record with a warning and the next
        # write drops it for good, so importing anything questionable only
        # manufactures dead weight with an invisible deadline. Dropping it here,
        # reported in the import summary, tells the user it happened.
        if (
            not isinstance(job, dict)
            or not all(isinstance(job.get(f), str) for f in ("id", "name", "message"))
            or not isinstance(job.get("schedule"), dict)
            or not isinstance(job["schedule"].get("kind"), str)
        ):
            dropped.append(_name_of(job))
            changed = True
            continue

        command = job.get("command", "")
        script = job.get("script", "")

        # Rule 2: the command is judged exactly as `cron_add` would judge it.
        if command:
            try:
                reason = _vet_shell_command(command)
            except Exception:  # noqa: BLE001 — unverifiable command must fail closed
                reason = "command could not be verified"
            if reason is not None:
                dropped.append(_name_of(job))
                changed = True
                # Same audit obligation as a `cron_add` denial: the dropped
                # command never reaches the ACP permission/hook flow, so this is
                # the only place the denial can be recorded. Named for where it
                # happened, so an import-time drop is not read as an attempted
                # `cron_add`.
                _log_cron_denial("settings_import", reason)
                continue

        # Rule 3: anything that EXECUTES arrives paused, awaiting a human.
        if (command or script) and not job.get("user_paused", False):
            job["user_paused"] = True
            job["enabled"] = False
            changed = True
            paused.append(_name_of(job))
            _log_cron_denial(
                "settings_import",
                "Error: an imported job that runs a command or script is "
                "restored paused until it is enabled by hand",
            )
        kept.append(job)

    if changed:
        data["jobs"] = kept
        crons_path.write_text(json.dumps(data, indent=2))
    return dropped, paused


def apply_import_zip(zip_path: Path, mode: str = "merge") -> dict:
    """Extract and apply an import zip.

    Args:
        zip_path: Path to validated zip file.
        mode: "merge" (default, non-destructive) or "replace" (overwrites).

    Returns summary dict of what was imported.
    """
    mc = _mc_dir()
    summary: dict = {"mode": mode, "items": []}

    with tempfile.TemporaryDirectory() as work_str:
        work = Path(work_str)

        with zipfile.ZipFile(str(zip_path), "r") as zf:
            infos = zf.infolist()
            # Zip-bomb guard (defense-in-depth; validate_import_zip also checks).
            if len(infos) > _MAX_IMPORT_MEMBERS:
                raise ValueError(
                    f"Import archive has too many entries ({len(infos)} > {_MAX_IMPORT_MEMBERS})"
                )
            total_uncompressed = sum(i.file_size for i in infos)
            if total_uncompressed > _MAX_IMPORT_UNCOMPRESSED:
                raise ValueError(
                    f"Import archive uncompressed size {total_uncompressed} exceeds cap "
                    f"{_MAX_IMPORT_UNCOMPRESSED} (possible zip bomb)"
                )
            for info in infos:
                parts = PurePosixPath(info.filename).parts
                if ".." in parts or info.filename.startswith("/"):
                    continue
                if _is_link_entry(info):
                    continue
                zf.extract(info, work)

        snap_dirs = [d for d in work.iterdir() if d.is_dir()]
        if len(snap_dirs) != 1:
            raise ValueError(
                f"Expected 1 top-level directory in zip, found {len(snap_dirs)}"
            )
        snap = snap_dirs[0]

        # Re-vet imported cron commands before ANY path below consumes
        # crons.json (merge, copy, or replace). An import archive is
        # attacker-influenced — the threat is a "settings backup" a user is
        # talked into importing — and a cron ``command`` is a free-form shell
        # string the scheduler later runs via ``sh -c``, entirely outside the
        # ACP permission/hook flow. ``cron_add`` guards exactly that out-of-band
        # execution with ``_vet_shell_command`` at storage time; the import path
        # wrote crons.json directly and so skipped it, turning a crafted archive
        # into arbitrary command execution with no CSRF, prompt injection, or
        # auth bypass required (CWE-502, CWE-862). Apply the identical guard here
        # and drop any job that fails, so a mostly-benign backup still restores
        # its safe jobs instead of the whole import aborting.
        dropped_crons, paused_crons = _sanitize_imported_crons(snap / "crons.json")
        if dropped_crons:
            summary["rejected_crons"] = dropped_crons
        # Reported separately: these are restored in full and only need switching
        # on, so calling them "rejected" would tell the user their jobs are gone.
        if paused_crons:
            summary["paused_crons"] = paused_crons

        if mode == "replace":
            # Strip sensitive files and skills/auto/ from snapshot before replace
            for excluded_name in EXPORT_EXCLUDE:
                excluded_file = snap / excluded_name
                if excluded_file.exists():
                    excluded_file.unlink()
            for fpath in snap.rglob("*"):
                if fpath.is_file() and is_sensitive_path(str(fpath)):
                    fpath.unlink()
            auto_dir = snap / "skills" / "auto"
            if auto_dir.is_dir():
                shutil.rmtree(str(auto_dir))
            _do_replace(snap, mc, None)
            summary["items"].append("full replace")
        else:
            # Merge mode
            if (snap / "memory.db").is_file():
                if not (mc / "memory.db").is_file():
                    shutil.copy2(str(snap / "memory.db"), str(mc / "memory.db"))
                    if (snap / "memory_index.db").is_file():
                        shutil.copy2(str(snap / "memory_index.db"), str(mc / "memory_index.db"))
                    summary["items"].append("memory (copied)")
                else:
                    _merge_memory(snap / "memory.db", mc / "memory.db")
                    summary["items"].append("memory (merged)")

            if (snap / "crons.json").is_file():
                if (mc / "crons.json").is_file():
                    _merge_crons(snap / "crons.json", mc / "crons.json")
                    summary["items"].append("crons (merged)")
                else:
                    shutil.copy2(str(snap / "crons.json"), str(mc / "crons.json"))
                    summary["items"].append("crons (copied)")

            if (snap / "hooks.json").is_file():
                if not (mc / "hooks.json").is_file():
                    shutil.copy2(str(snap / "hooks.json"), str(mc / "hooks.json"))
                    summary["items"].append("hooks (copied)")
                else:
                    summary["items"].append("hooks (skipped, already exists)")

            if (snap / "config.json").is_file() and not (mc / "config.json").is_file():
                shutil.copy2(str(snap / "config.json"), str(mc / "config.json"))
                summary["items"].append("config (restored)")

            if (snap / "notifications.jsonl").is_file():
                if (mc / "notifications.jsonl").is_file():
                    _merge_notifications(
                        snap / "notifications.jsonl", mc / "notifications.jsonl"
                    )
                    summary["items"].append("notifications (merged)")
                else:
                    shutil.copy2(
                        str(snap / "notifications.jsonl"), str(mc / "notifications.jsonl")
                    )
                    summary["items"].append("notifications (copied)")

            for dirname in ("workspace", "plan_memory"):
                sd = snap / dirname
                if sd.is_dir():
                    dd = mc / dirname
                    dd.mkdir(parents=True, exist_ok=True)
                    _copy_tree_no_overwrite(sd, dd)
                    summary["items"].append(f"{dirname} (merged)")

            if (snap / "skills").is_dir():
                (mc / "skills").mkdir(parents=True, exist_ok=True)
                # Skip skills/auto/ — those must go through SkillsLoader APIs
                for item in (snap / "skills").iterdir():
                    if item.name == "auto":
                        continue
                    target = mc / "skills" / item.name
                    if item.is_dir() and not target.exists():
                        shutil.copytree(str(item), str(target))
                    elif item.is_file() and not target.exists():
                        shutil.copy2(str(item), str(target))
                summary["items"].append("skills (merged, auto/ skipped)")

    return summary
