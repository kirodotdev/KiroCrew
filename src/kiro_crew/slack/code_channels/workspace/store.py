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
import re
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
    #: A channel-specific Slack error that turned this channel's views off.
    slack_off: str = ""
    updated_at: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return record_key(self.team_id, self.channel_id)


def record_key(team_id: str, channel_id: str) -> str:
    return f"{team_id}:{channel_id}"


_FIELDS = {f.name for f in fields(WorkspaceRecord)}
#: Declared type of every field (annotations are strings under ``__future__``).
_TYPES = {f.name: f.type for f in fields(WorkspaceRecord)}
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
#: The only base refs a bind records (see ``git_ops.create_or_adopt_worktree``).
_BASE_REFS = frozenset({"", "origin/HEAD"})
#: ``worktree_skipped`` on a loaded record whose diff base was malformed: the next
#: turn start rebinds it, which re-adopts the same worktree and re-derives the base.
REPAIRED = "record_repaired"


def _typed(name: str, value: object) -> bool:
    kind = _TYPES[name]
    if kind == "bool":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False  # bool is an int subclass, never a valid count or stamp
    if kind == "str":
        return isinstance(value, str)
    if kind == "int":
        return isinstance(value, int)
    if kind == "float":
        return isinstance(value, (int, float))
    return False


def _from_dict(raw: object) -> WorkspaceRecord | None:
    """A record from its stored form; ``None`` only when it names no channel.

    The file is data the gateway later hands to git and compares as numbers, so
    a field of the wrong type falls back to its default instead of raising
    mid-turn, and a ``base_sha`` that is not a full object id (or a ``base_ref``
    the bind never records) never reaches git: the base is dropped and the
    record marked :data:`REPAIRED`, so the next turn start rebinds and re-derives
    it. The rest of the record (its folder, the session already pointed) is kept.
    """
    if not isinstance(raw, dict):
        return None
    data = {k: v for k, v in raw.items() if k in _FIELDS}
    if not isinstance(data.get("team_id"), str) or not isinstance(data.get("channel_id"), str):
        return None
    data = {k: v for k, v in data.items() if _typed(k, v)}
    sha = data.get("base_sha", "")
    if data.get("base_ref", "") not in _BASE_REFS:
        data.pop("base_ref")
        sha = "-"  # the base is unknown either way
    if sha and not _SHA.fullmatch(sha):
        data.pop("base_sha", None)
        if data.get("worktree"):
            data["worktree_skipped"] = REPAIRED
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
