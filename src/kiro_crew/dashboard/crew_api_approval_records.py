"""What the Owner approval routes keep on disk about each ``apr_`` id.

One JSON file under ``member-records/crew-api-approvals/``, which no sandbox can read
or write. A row holds the id, its state, its times, why it expired and the session it
was raised in. It holds no tool name, reason or argument:
those stay in the gateway's memory and in the session's own transcript.

Every function here that touches the disk blocks (``record_path``, ``load``,
``set_aside``, ``save``); :mod:`crew_api_approvals` calls them off the event loop.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import (
    atomic_write,
    fsync_dir,
    read_bytes_with_retry,
    rename_with_retry,
)

#: The record kind under ``member-records/``; one of ``member_records.RECORD_KINDS``.
APPROVALS_KIND = "crew-api-approvals"
#: The fields of a row, and nothing else, reach the file.
FIELDS = ("id", "state", "created_at", "decided_at", "expired_reason", "slot")
_STATES = ("pending", "allowed", "denied", "expired")
# Separate byte ceiling: pending rows can exceed MAX_RECORDS in the index.
_MAX_BYTES = 1024 * 1024


def record_path() -> Path:
    """The gateway's record file. Touches disk: the records directory may move once."""
    from kiro_crew import member_records

    return member_records.records_dir(APPROVALS_KIND) / "state.json"


def _row(raw: Any) -> dict[str, Any]:
    """One stored row, or ``ValueError``: a row is never half believed."""
    if not isinstance(raw, dict) or set(raw) != set(FIELDS):
        raise ValueError("approval record row has the wrong fields")
    text = (raw["id"], raw["state"], raw["slot"])
    times = (raw["created_at"], raw["decided_at"])
    if (
        not all(isinstance(v, str) for v in text)
        or not raw["id"].startswith("apr_")
        or raw["state"] not in _STATES
        or not (raw["expired_reason"] is None or isinstance(raw["expired_reason"], str))
        or not all(v is None or (isinstance(v, int) and not isinstance(v, bool)) for v in times)
        or raw["created_at"] is None
    ):
        raise ValueError("approval record row has a wrong value")
    return dict(raw)


def load(path: Path) -> list[dict[str, Any]]:
    """The stored rows, oldest first. A missing file is empty; a file that cannot be
    read raises ``OSError`` and one whose content is not valid raises ``ValueError`` or
    ``RecursionError``, so no id is taken for unknown and given out again."""
    try:
        raw = read_bytes_with_retry(path, max_bytes=_MAX_BYTES)
    except FileNotFoundError:
        return []
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("approvals"), list):
        raise ValueError("approval record is malformed")
    rows = [_row(item) for item in data["approvals"]]
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("approval record repeats an id")
    return rows


def set_aside(path: Path, now: float) -> Path:
    """Move a file whose content is not valid out of the way, and return where it went.

    Never deletes: the name carries the time, and a name already taken gets a
    number, so an earlier set-aside copy is kept too.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    target = path.with_name(f"{path.name}.damaged-{stamp}")
    count = 1
    # lexists, so a link that points nowhere also counts as a taken name.
    while os.path.lexists(target):
        count += 1
        target = path.with_name(f"{path.name}.damaged-{stamp}-{count}")
    rename_with_retry(path, target)
    fsync_dir(path.parent)
    return target


def save(path: Path, rows: list[dict[str, Any]]) -> None:
    """Replace the file with *rows* and make the replacement durable before returning."""
    serialized = json.dumps({"approvals": [{k: row[k] for k in FIELDS} for row in rows]}) + "\n"
    if len(serialized.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("approval record is full")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Defence in depth only: the sandbox mask is the fence.
        platform_compat.restrict_dir_to_owner(path.parent)
    except OSError:
        pass
    atomic_write(path, serialized, fsync=True)
    fsync_dir(path.parent)
    fsync_dir(path.parent.parent)
