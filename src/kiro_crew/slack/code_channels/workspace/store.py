"""Per-channel workspace records, persisted so a restart picks up where it left off.

One small JSON document under the data home, keyed by ``team_id:channel_id``. It
holds pointers and digests only (worktree path, branch, base sha, the hash of
the last published diff and context bar, the PR last seen), never repository
content. The branch name is derived from the channel, so a lost or evicted
record is recoverable: the next bind adopts the worktree that branch holds.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

#: Records kept; the least recently touched one is dropped past this.
MAX_RECORDS = 1000


@dataclass
class WorkspaceRecord:
    """Everything the workspace layer remembers about one code channel."""

    team_id: str
    channel_id: str
    session_key: str = ""
    #: The project the binding resolved, before any worktree.
    project_dir: str = ""
    #: The git toplevel of ``project_dir`` ("" when it is not a repository).
    repo_root: str = ""
    worktree: str = ""
    branch: str = ""
    base_sha: str = ""
    base_ref: str = ""
    #: ``project_dir`` relative to ``repo_root`` ("" when it IS the root).
    subdir: str = ""
    #: Why no worktree was made ("" when one was, or none was needed).
    worktree_skipped: str = ""
    #: Binds that ended without a worktree on a retryable (git) error.
    bind_attempts: int = 0
    #: The session key last pointed at the worktree (each session once, at bind
    #: or when a replacement session takes over the channel).
    pointed_session: str = ""
    #: The project the bind actually used: the binding's own project, else the
    #: one the bound session was created with (the dashboard default).
    resolved_project: str = ""
    #: The channel's own sidebar folder (its project is the worktree), or "".
    folder_id: str = ""
    diff_hash: str = ""
    view_id: str = ""
    bar_hash: str = ""
    pr_number: int = 0
    pr_url: str = ""
    pr_state: str = ""
    pr_ci: str = ""
    #: A Slack error that turned this channel's views off (feature_disabled ...).
    slack_off: str = ""
    #: ``kept`` once release found uncommitted work and left the worktree.
    status: str = "active"
    updated_at: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return record_key(self.team_id, self.channel_id)


def record_key(team_id: str, channel_id: str) -> str:
    return f"{team_id}:{channel_id}"


_FIELDS = {f.name for f in fields(WorkspaceRecord)}


def _from_dict(raw: object) -> WorkspaceRecord | None:
    if not isinstance(raw, dict):
        return None
    data = {k: v for k, v in raw.items() if k in _FIELDS}
    if not isinstance(data.get("team_id"), str) or not isinstance(data.get("channel_id"), str):
        return None
    try:
        return WorkspaceRecord(**data)
    except TypeError:
        return None


class WorkspaceStore:
    """Thread-safe load/save of :class:`WorkspaceRecord` (blocking; call off-loop)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _file(self) -> Path:
        if self._path is None:
            from kiro_crew.config.paths import data_home

            self._path = data_home() / "slack" / "code_channel_workspaces.json"
        return self._path

    def _read(self) -> dict[str, WorkspaceRecord]:
        path = self._file()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("code channel workspace store unreadable at %s; starting empty", path)
            return {}
        out: dict[str, WorkspaceRecord] = {}
        for item in raw.get("records", []) if isinstance(raw, dict) else []:
            rec = _from_dict(item)
            if rec is not None:
                out[rec.key] = rec
        return out

    def _write(self, records: dict[str, WorkspaceRecord]) -> None:
        ordered = sorted(records.values(), key=lambda r: r.updated_at, reverse=True)[:MAX_RECORDS]
        path = self._file()
        os.makedirs(path.parent, exist_ok=True)
        payload = {"version": 1, "records": [asdict(r) for r in ordered]}
        atomic_write(path, json.dumps(payload, indent=1, sort_keys=True), restrict_to_owner=True)

    def get(self, team_id: str, channel_id: str) -> WorkspaceRecord | None:
        with self._lock:
            return self._read().get(record_key(team_id, channel_id))

    def all(self) -> list[WorkspaceRecord]:
        """Every record (a snapshot)."""
        with self._lock:
            return list(self._read().values())

    def put(self, record: WorkspaceRecord) -> None:
        with self._lock:
            records = self._read()
            record.updated_at = time.time()
            records[record.key] = record
            self._write(records)

    def delete(self, team_id: str, channel_id: str) -> None:
        with self._lock:
            records = self._read()
            if records.pop(record_key(team_id, channel_id), None) is not None:
                self._write(records)
