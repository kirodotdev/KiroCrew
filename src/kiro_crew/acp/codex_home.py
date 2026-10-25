"""Persistent Windows Codex storage owned by Crew, separate from desktop history."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Mapping
from uuid import UUID

from kiro_crew.agent_sdk.host_auth import CREW_CODEX_HOME_LEAF


def _plain(path: Path) -> bool:
    info = path.lstat()
    return not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400)


def _directory(root: Path, target: Path) -> None:
    """Never publish imported configuration or conversations through a reparse point."""
    relative = target.relative_to(root)
    cursor = root
    for part in ("", *relative.parts):
        cursor = cursor / part
        cursor.mkdir(mode=0o700, exist_ok=True)
        if not _plain(cursor) or not cursor.is_dir():
            raise ValueError("Crew Codex storage must contain ordinary directories")


def _copy_new(source: Path, destination: Path, root: Path) -> None:
    _directory(root, destination.parent)
    if destination.exists():
        if not _plain(destination) or not destination.is_file():
            raise ValueError("Crew Codex storage contains an aliased file")
        return
    # Publish a complete file without replacing an existing profile or a copy
    # another runtime won. No auth files or desktop indexes are copied.
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".crew-import-") as stage:
        temporary = Path(stage) / "import"
        with temporary.open("xb") as output, source.open("rb") as incoming:
            while chunk := incoming.read(1024 * 1024):
                output.write(chunk)
        temporary.chmod(0o600)
        try:
            if os.name == "nt":
                os.rename(temporary, destination)
            else:
                os.link(temporary, destination)
        except FileExistsError:
            if not _plain(destination):
                raise ValueError("Crew Codex storage contains an aliased file")


def _crew_thread_ids(crew_home: Path) -> set[str]:
    index = crew_home / "session_map.json"
    if not index.exists():
        return set()
    entries = json.loads(index.read_text("utf-8"))
    if not isinstance(entries, dict):
        raise ValueError("Crew session map must be an object")
    result = set()
    for entry in entries.values():
        if not isinstance(entry, dict) or entry.get("provider") != "codex":
            continue
        sid = entry.get("sid")
        if not isinstance(sid, str):
            continue
        try:
            if str(UUID(sid)) == sid:
                result.add(sid)
        except ValueError:
            continue
    return result


def _rollout(source: Path, sid: str) -> Path | None:
    sessions = source / "sessions"
    database = source / "state_5.sqlite"
    candidates: list[Path] = []
    if database.is_file():
        try:
            with closing(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
            ) as db:
                row = db.execute(
                    "SELECT rollout_path FROM threads WHERE id = ? AND archived = 0", (sid,)
                ).fetchone()
            if row and isinstance(row[0], str):
                candidates.append(Path(row[0]))
        except sqlite3.Error:
            # An incomplete or rebuilding desktop index is only a missed
            # optimization. Crew's own mapped UUID remains the authority.
            pass
    candidates.extend(sessions.glob(f"**/rollout-*-{sid}.jsonl"))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            candidate.relative_to(sessions)
        except ValueError:
            continue
        cursor = candidate
        while cursor != sessions.parent:
            if not _plain(cursor):
                raise ValueError("A desktop Codex rollout is aliased")
            cursor = cursor.parent
        with candidate.open(encoding="utf-8") as stream:
            header = json.loads(stream.readline())
        if header.get("type") == "session_meta" and header.get("payload", {}).get("id") == sid:
            return candidate
    return None


def prepare_codex_home(environ: Mapping[str, str], home: Path) -> Path | None:
    """Prepare the default home off-loop; an explicit CODEX_HOME remains the operator's.

    Import only IDs Crew's own session map already owns. Desktop history stays
    untouched, and the destination index is rebuilt from its private rollouts.
    Authentication is independent: do not duplicate OAuth refresh-token state.
    """
    from kiro_crew.config.paths import data_home

    crew = data_home()
    target = crew / CREW_CODEX_HOME_LEAF
    if environ.get("CODEX_HOME") and Path(environ["CODEX_HOME"]) != target:
        return None
    _directory(crew, target)
    source = home / ".codex"
    config = source / "config.toml"
    if config.is_file():
        _copy_new(config, target / "config.toml", target)
    for sid in _crew_thread_ids(crew):
        if any((target / "sessions").glob(f"**/rollout-*-{sid}.jsonl")):
            continue
        rollout = _rollout(source, sid)
        if rollout is not None:
            _copy_new(rollout, target / rollout.relative_to(source), target)
    return target


def configure_codex_broker() -> None:
    """An explicit setup action: route trusted Crew tools through their host broker.

    Sharing and all other server decisions stay as configured. Core and cron
    cannot audit or authenticate inside a foreign harness's credential mask.
    """
    from kiro_crew.config.loader import config_path, update_config_locked

    def configure(data: dict) -> dict:
        gateway = dict(data.get("mcp_gateway", {}))
        overrides = dict(gateway.get("stub_overrides", {}))
        overrides.update({"kirocrew-core": True, "kirocrew-cron": True})
        gateway["stub_overrides"] = overrides
        data["mcp_gateway"] = gateway
        return data

    update_config_locked(config_path(), mutate=configure, fsync=True)
