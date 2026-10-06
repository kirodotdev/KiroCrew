"""Say why an auto-nudge loop stopped, in the gateway log.

A loop can stop on many paths: its cycle cap, its wall-clock budget, an
unanswered tool approval, a restart mid-cycle, an agent calling
``autonudge_stop``, a user delete, a closed session. Before this module several
of those were logged at INFO (hidden at the gateway's default level) and a
removed legacy loop left no row behind, so "the loop just stopped" could not be
explained after the fact.

The service calls :func:`stop_records` at its ONE store commit point, with the
set of loops that were active in the previous committed store and the rows of
the new one. Every loop that was active and now is not -- inactive or gone --
yields one record, whichever path stopped it, and each record is logged as one
WARNING line; grep ``gateway.log`` for ``AutoNudge:``.

Each boot moves ``gateway.log`` aside, so that line alone does not outlive a few
restarts. :func:`append_record` therefore also appends the same record, as one
JSON line, to ``<data home>/logs/autonudge_stops.jsonl`` (:data:`STOPS_FILE`).
That file is the record that survives restarts, bounded by
:func:`kiro_crew.jsonl_util.rotate_jsonl_at` to one ``.1`` generation.

Pure helpers only; the service owns locking and when to call them.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from kiro_crew.jsonl_util import rotate_jsonl_at
from kiro_crew.owner_only_files import ensure_directory, owner_only_opener_for
from kiro_crew.platform import redact_log_via_context

logger = logging.getLogger("kiro_crew.autonudge")

# Model-authored or user-authored free text is clipped, never dropped: it is the
# most useful "why" for an agent-initiated stop.
DETAIL_MAX_CHARS = 300

# Reason for a loop that vanished from the store with no note naming the path.
REMOVED_REASON = "removed"
# Reason for a loop that went inactive with no recorded reason.
UNSPECIFIED_REASON = "unspecified"
# Default clip for store-sourced strings; a stop's free-text detail uses DETAIL_MAX_CHARS.
FIELD_MAX_CHARS = 200

# The stop records that outlive a gateway restart, under ``<data home>/logs/``.
STOPS_FILE = "autonudge_stops.jsonl"
# One record is a few hundred bytes, so the live file plus its ``.1`` generation
# hold thousands of stops in about 2 MiB.
STOPS_MAX_BYTES = 1024 * 1024


def safe_text(value: object, limit: int = FIELD_MAX_CHARS) -> str:
    """Scrub credentials, then clip: store and caller text is untrusted.

    Redaction runs on the whole string first. Clipping first could cut a
    credential in half, and the redactor does not match a half token, so its
    raw prefix would reach the log.
    """
    return redact_log_via_context(str(value or ""))[:limit]


def _number(value: object) -> int | None:
    """A store number as a bounded int, or None. The store is agent-writable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if abs(value) > 10**15:
        return None
    return int(value)


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    monitor = row.get("monitor")
    target = monitor.get("target") if isinstance(monitor, Mapping) else None
    return {
        "loop_id": safe_text(row.get("id")),
        "slot_key": safe_text(row.get("slot_key")),
        "kind": "monitor" if isinstance(monitor, Mapping) else "loop",
        "target": safe_text(target),
        "cycle_count": _number(row.get("cycle_count")),
        "max_cycles": _number(row.get("max_cycles")),
        "created_ts": _number(row.get("created_ts")),
        "max_runtime_secs": _number(row.get("max_runtime_secs")),
    }


def _is_active(row: Mapping[str, Any]) -> bool:
    return row.get("active") is True


def active_summaries(rows: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Map loop id -> summary for every ACTIVE row. Non-dict rows are skipped."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, Mapping) and _is_active(row) and row.get("id"):
            out[str(row["id"])] = _summary(row)
    return out


def _stored_reason(row: Mapping[str, Any]) -> tuple[str, str]:
    """(reason, detail) a stopped row carries in its own fields."""
    monitor = row.get("monitor")
    if isinstance(monitor, Mapping):
        reason = str(monitor.get("stopped_reason") or monitor.get("outcome") or "")
        detail = str(monitor.get("user_stop_reason") or "")
        if reason:
            return reason, detail
    return str(row.get("stopped_reason") or ""), ""


def stop_records(
    previous_active: Mapping[str, Mapping[str, Any]],
    rows: Iterable[Any],
    notes: Mapping[str, tuple[str, str]],
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """One record per loop that was active before this commit and is not now.

    ``notes`` maps loop id -> (reason, detail) left by a removal in flight. It
    names a row that is GONE; a row still present carries its own reason.
    """
    now = time.time() if now is None else now
    by_id = {str(r["id"]): r for r in rows if isinstance(r, Mapping) and r.get("id")}
    records: list[dict[str, Any]] = []
    for loop_id, before in previous_active.items():
        row = by_id.get(loop_id)
        if row is not None and _is_active(row):
            continue
        if row is None:
            summary = dict(before)
            note_reason, detail = notes.get(loop_id, ("", ""))
            reason = note_reason or REMOVED_REASON
        else:
            summary = _summary(row)
            stored, detail = _stored_reason(row)
            reason = stored or UNSPECIFIED_REASON
        created = summary.pop("created_ts", None)
        ran = int(now - created) if isinstance(created, int) and created > 0 else None
        records.append(
            {
                "ts": round(now, 3),
                **summary,
                "ran_secs": ran,
                "reason": safe_text(reason),
                "detail": safe_text(detail, DETAIL_MAX_CHARS),
            }
        )
    return records


def log_record(record: Mapping[str, Any]) -> None:
    """One WARNING line per stop, so the reason shows at the default log level.

    Every field goes through ``%r`` so a newline or escape in store text cannot
    forge a second log record.
    """
    logger.warning(
        "AutoNudge: %r %r on %r stopped — reason=%r%s (cycles %r/%r, ran %rs of %r)",
        record.get("kind"),
        record.get("loop_id"),
        record.get("slot_key"),
        record.get("reason"),
        f" detail={record['detail']!r}" if record.get("detail") else "",
        record.get("cycle_count"),
        record.get("max_cycles") or None,
        record.get("ran_secs"),
        record.get("max_runtime_secs") or None,
    )


def stops_path(base_dir: Path) -> Path:
    """Where the stop records of the data home ``base_dir`` are appended."""
    return base_dir / "logs" / STOPS_FILE


def append_record(base_dir: Path, record: Mapping[str, Any]) -> None:
    """Append one stop record to :func:`stops_path` as a JSON line. Never raises.

    The record's strings already went through :func:`safe_text`, so the file holds
    the same scrubbed values as the log line. ``json.dumps`` escapes every control
    character, so store text cannot forge a second line. The caller's store write
    has already committed: a failure here costs this record, never the write.
    """
    try:
        path = stops_path(base_dir)
        ensure_directory(path.parent)  # 0700 inside the data home
        line = json.dumps(dict(record), ensure_ascii=True, separators=(",", ":"), default=str)
        rotate_jsonl_at(path, STOPS_MAX_BYTES)
        with open(path, "a", encoding="utf-8", opener=owner_only_opener_for(path)) as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 - see docstring: the store write already stands
        logger.warning("AutoNudge: could not append a stop record to the stop file", exc_info=True)
