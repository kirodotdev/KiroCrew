"""Durable per-session goal drafts shared by dashboard clients.

The browser keeps a local copy so the goal editor remains useful offline, but
``localStorage`` is per browser profile.  This store is the canonical bridge
between desktop and mobile: every record carries the browser draft's edit time,
so a stale client that opens later cannot overwrite a newer draft while old
local-only records are migrated.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import data_home
from kiro_crew.platform_compat import file_lock, is_link_or_junction, open_lock_file

logger = logging.getLogger(__name__)

_STORE_VERSION = 1
_STORE_FILE = "goal_drafts.json"
_LOCK_FILE = ".goal_drafts.lock"
_MAX_STORE_BYTES = 1024 * 1024
MAX_GOAL_DRAFTS = 50
GOAL_DRAFT_TTL_MS = 30 * 24 * 60 * 60 * 1000
MAX_GOAL_MESSAGE_CHARS = 8_000
MAX_GOAL_SLOT_CHARS = 512
MAX_GOAL_IDLE_SECS = 86_400
MAX_GOAL_CYCLES = 2_147_483_647
_CLOCK_SKEW_MS = 5 * 60 * 1000


@dataclass(frozen=True)
class GoalDraft:
    """One user-edited goal form, including its last edit time."""

    message: str
    idle_secs: int
    max_cycles: int
    updated_at: int

    def to_public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GoalDraftSnapshot:
    """A draft or a tombstone at one monotonic conflict timestamp."""

    draft: GoalDraft | None
    updated_at: int

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "draft": self.draft.to_public_dict() if self.draft is not None else None,
            "updated_at": self.updated_at,
        }


class GoalDraftStore:
    """Cross-process-safe JSON store for per-slot goal drafts.

    Writes are read/modify/write transactions under a sidecar lock.  The data
    file is replaced atomically and owner-restricted because goal text can carry
    paths or other sensitive instructions.  Methods are synchronous by design;
    HTTP handlers must call them through ``asyncio.to_thread``.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir or data_home()
        self._path = self._base_dir / _STORE_FILE
        self._lock_path = self._base_dir / _LOCK_FILE

    @staticmethod
    def _slot(slot_key: str) -> str:
        if not isinstance(slot_key, str) or not slot_key or len(slot_key) > MAX_GOAL_SLOT_CHARS:
            raise ValueError(
                f"slot_key must be a non-empty string of at most {MAX_GOAL_SLOT_CHARS} characters"
            )
        return slot_key

    @staticmethod
    def _updated_at(value: Any, *, now_ms: int) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("updated_at must be a finite timestamp")
        stamp = float(value)
        if not math.isfinite(stamp) or stamp < 0:
            raise ValueError("updated_at must be a finite timestamp")
        # Client edit clocks order migration writes.  Bound future skew so one
        # misconfigured browser cannot pin a record far into the future.
        return int(min(stamp, now_ms + _CLOCK_SKEW_MS))

    @staticmethod
    def _draft(
        message: Any,
        idle_secs: Any,
        max_cycles: Any,
        *,
        updated_at: int,
    ) -> GoalDraft:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if len(message) > MAX_GOAL_MESSAGE_CHARS:
            raise ValueError(f"message too long (max {MAX_GOAL_MESSAGE_CHARS} chars)")
        if (
            isinstance(idle_secs, bool)
            or not isinstance(idle_secs, int)
            or not 1 <= idle_secs <= MAX_GOAL_IDLE_SECS
        ):
            raise ValueError(f"idle_secs must be an integer between 1 and {MAX_GOAL_IDLE_SECS}")
        if (
            isinstance(max_cycles, bool)
            or not isinstance(max_cycles, int)
            or not 0 <= max_cycles <= MAX_GOAL_CYCLES
        ):
            raise ValueError(f"max_cycles must be an integer between 0 and {MAX_GOAL_CYCLES}")
        return GoalDraft(
            message=message,
            idle_secs=idle_secs,
            max_cycles=max_cycles,
            updated_at=updated_at,
        )

    def _read_unlocked(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        if is_link_or_junction(self._path):
            raise OSError(f"refusing linked goal-draft store: {self._path}")
        if self._path.stat().st_size > _MAX_STORE_BYTES:
            raise OSError(f"goal-draft store exceeds {_MAX_STORE_BYTES} bytes")
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Ignoring corrupt goal-draft store %s", self._path, exc_info=True)
            return {}
        if not isinstance(raw, dict) or raw.get("version") != _STORE_VERSION:
            return {}
        drafts = raw.get("drafts")
        return drafts if isinstance(drafts, dict) else {}

    @staticmethod
    def _decode_record(raw: Any) -> GoalDraftSnapshot | None:
        if not isinstance(raw, dict):
            return None
        updated_at = raw.get("updated_at")
        if isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at < 0:
            return None
        if raw.get("deleted") is True:
            return GoalDraftSnapshot(draft=None, updated_at=updated_at)
        try:
            draft = GoalDraftStore._draft(
                raw.get("message"),
                raw.get("idle_secs"),
                raw.get("max_cycles"),
                updated_at=updated_at,
            )
        except ValueError:
            return None
        return GoalDraftSnapshot(draft=draft, updated_at=updated_at)

    @staticmethod
    def _encode_snapshot(snapshot: GoalDraftSnapshot) -> dict[str, Any]:
        if snapshot.draft is None:
            return {"deleted": True, "updated_at": snapshot.updated_at}
        return snapshot.draft.to_public_dict()

    @staticmethod
    def _prune(
        records: dict[str, dict[str, Any]], *, now_ms: int
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        cutoff = now_ms - GOAL_DRAFT_TTL_MS
        decoded: list[tuple[str, GoalDraftSnapshot]] = []
        changed = False
        for slot, raw in records.items():
            snapshot = GoalDraftStore._decode_record(raw)
            if snapshot is None or snapshot.updated_at < cutoff:
                changed = True
                continue
            decoded.append((slot, snapshot))
        decoded.sort(key=lambda item: item[1].updated_at, reverse=True)
        if len(decoded) > MAX_GOAL_DRAFTS:
            changed = True
            decoded = decoded[:MAX_GOAL_DRAFTS]
        clean = {slot: GoalDraftStore._encode_snapshot(snapshot) for slot, snapshot in decoded}
        return clean, changed or clean != records

    def _write_unlocked(self, records: dict[str, dict[str, Any]]) -> None:
        payload = (
            json.dumps(
                {"version": _STORE_VERSION, "drafts": records},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        if len(payload.encode("utf-8")) > _MAX_STORE_BYTES:
            raise OSError(f"goal-draft store exceeds {_MAX_STORE_BYTES} bytes")
        atomic_write(
            self._path,
            payload,
            fsync=True,
            restrict_to_owner=True,
        )

    def get(self, slot_key: str, *, now_ms: int | None = None) -> GoalDraftSnapshot:
        slot = self._slot(slot_key)
        now = int(time.time() * 1000) if now_ms is None else now_ms
        self._base_dir.mkdir(parents=True, exist_ok=True)
        with open_lock_file(self._lock_path) as fd:
            with file_lock(fd, exclusive=True, required=True):
                records, changed = self._prune(self._read_unlocked(), now_ms=now)
                if changed:
                    self._write_unlocked(records)
                return self._decode_record(records.get(slot)) or GoalDraftSnapshot(None, 0)

    def put(
        self,
        slot_key: str,
        *,
        message: str | None,
        idle_secs: int | None,
        max_cycles: int | None,
        updated_at: int | float,
        now_ms: int | None = None,
    ) -> GoalDraftSnapshot:
        """Apply a last-write-wins draft or tombstone and return canonical state."""

        slot = self._slot(slot_key)
        now = int(time.time() * 1000) if now_ms is None else now_ms
        stamp = self._updated_at(updated_at, now_ms=now)
        incoming = (
            GoalDraftSnapshot(None, stamp)
            if message is None
            else GoalDraftSnapshot(
                self._draft(message, idle_secs, max_cycles, updated_at=stamp),
                stamp,
            )
        )
        self._base_dir.mkdir(parents=True, exist_ok=True)
        with open_lock_file(self._lock_path) as fd:
            with file_lock(fd, exclusive=True, required=True):
                records, changed = self._prune(self._read_unlocked(), now_ms=now)
                current = self._decode_record(records.get(slot)) or GoalDraftSnapshot(None, 0)
                # Equal timestamps are idempotent.  Keeping the committed value
                # also prevents two same-millisecond clients from oscillating.
                if incoming.updated_at > current.updated_at:
                    records.pop(slot, None)
                    records[slot] = self._encode_snapshot(incoming)
                    records, _ = self._prune(records, now_ms=now)
                    changed = True
                    current = incoming
                if changed:
                    self._write_unlocked(records)
                return current


_default_store: GoalDraftStore | None = None


def get_goal_draft_store() -> GoalDraftStore:
    """Return the process-wide default store, created lazily after config load."""

    global _default_store
    if _default_store is None:
        _default_store = GoalDraftStore()
    return _default_store
