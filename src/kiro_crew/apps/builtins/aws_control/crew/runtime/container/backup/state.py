"""The sidecar's on-disk memory of what it has already uploaded.

Two reasons this is persisted rather than kept in RAM:

* Cost. Re-hashing every artifact on every cycle would make single-cycle work
  grow with total history. The state lets the sidecar skip write-once artifacts
  it has already seen and re-hash only the small, mutable transcripts.
* Visibility. The design accepts that backup lags live conversation, but
  requires the lag to be *readable* by the owner. The last-cycle timestamp is
  recorded here so ``sidecar.backup_status`` can report the exposure window.

The state file lives beside the data home but OUTSIDE the backup unit, so it is
never itself uploaded or restored.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..common import Settings

STATE_FILENAME = ".smc_backup_state.json"


def state_path(settings: Settings) -> Path:
    return settings.data_home / STATE_FILENAME


@dataclass
class ObjMeta:
    size: int
    hash: str  # sha256 hex, or "" when only the size is known (seeded from S3)


@dataclass
class BackupState:
    objects: dict[str, ObjMeta] = field(default_factory=dict)
    cycles: int = 0
    last_cycle_ts: float | None = None
    last_success_ts: float | None = None

    # --- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "BackupState":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return cls()
        objects = {
            k: ObjMeta(int(v["size"]), str(v.get("hash", "")))
            for k, v in raw.get("objects", {}).items()
            if isinstance(v, dict) and "size" in v
        }
        return cls(
            objects=objects,
            cycles=int(raw.get("cycles", 0)),
            last_cycle_ts=raw.get("last_cycle_ts"),
            last_success_ts=raw.get("last_success_ts"),
        )

    def save(self, path: Path) -> None:
        payload = {
            "objects": {k: {"size": m.size, "hash": m.hash} for k, m in self.objects.items()},
            "cycles": self.cycles,
            "last_cycle_ts": self.last_cycle_ts,
            "last_success_ts": self.last_success_ts,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # --- seeding -----------------------------------------------------------

    def seed_sizes(self, sizes: dict[str, ObjMeta | int]) -> None:
        """Prime the index from an S3 listing after a fresh task start.

        Only sizes are known from a listing, so hashes stay ``""``. That is
        enough for the write-once artifact skip (which checks presence), while
        mutable transcripts are safely re-hashed once because ``""`` never
        equals a real hash.
        """
        for key, meta in sizes.items():
            if key in self.objects:
                continue
            size = meta.size if isinstance(meta, ObjMeta) else int(meta)
            self.objects[key] = ObjMeta(size, "")


def backup_status(settings: Settings, *, now: float | None = None) -> dict:
    """The owner-readable backup metric: how stale is the backup, right now.

    ``lag_secs`` is the age of the last completed cycle. ``None`` means no cycle
    has completed yet (the exposure is the whole conversation, not a bounded
    window) — reported as such rather than as zero.
    """
    st = BackupState.load(state_path(settings))
    now = time.time() if now is None else now
    lag = None if st.last_success_ts is None else max(0.0, now - st.last_success_ts)
    return {
        "lag_secs": lag,
        "last_success_ts": st.last_success_ts,
        "last_cycle_ts": st.last_cycle_ts,
        "cycles": st.cycles,
        "objects": len(st.objects),
    }
