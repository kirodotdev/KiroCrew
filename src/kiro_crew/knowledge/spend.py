"""Per-source ingestion progress and outstanding-spend estimate.

Indexing a knowledge source is neither free nor instantaneous: a sweep pays one
model extraction call per chunk plus one summary call per file, on a pool of
billed sessions, and a paced folder source keeps drawing that cost sweep after
sweep for as long as files remain. Without a per-source figure the only place
that ongoing spend shows up is a credit balance, after it has been spent.

Everything here is derived from rows the ingestion path already writes --
``folder_file_state`` for per-file progress, ``items`` for embedded chunks -- so
it adds no schema and no bookkeeping a sweep has to keep in step with.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_crew.knowledge.folder_watcher import estimate_scan_cost, max_files_prop

logger = logging.getLogger(__name__)

#: ``folder_file_state.status`` values a sweep will not revisit. Anything else --
#: 'pending', 'scanning', or a status some future writer adds -- is outstanding
#: work, so an unrecognised value counts as remaining rather than silently as done.
_TERMINAL_STATUSES = frozenset({"done", "deduped", "failed", "skipped"})

#: Terminal statuses reached by ingesting the file, as opposed to failing on it or
#: being skipped by the user.
_COMPLETED_STATUSES = frozenset({"done", "deduped"})


def _properties(raw: Any) -> dict:
    """A source's ``properties`` column as a dict, whatever shape it is stored in."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _empty() -> dict[str, int]:
    return {
        "files_total": 0,
        "files_done": 0,
        "files_failed": 0,
        "files_skipped": 0,
        "files_pending": 0,
        "chunks_embedded": 0,
        "estimated_llm_calls_remaining": 0,
    }


def source_spend(store: Any, sources: list[dict]) -> dict[str, dict[str, int]]:
    """Progress and outstanding-cost counters keyed by source id.

    Read-only: three aggregate reads plus the size ``stat`` per outstanding file
    that the cost estimate needs. Blocking, so a caller on the event loop offloads
    it.

    Every source in *sources* gets an entry. One with no per-file state -- an
    uploaded file, or an aggregate artifact or agent source -- reports zero files
    and zero remaining calls, which is truthful: nothing is queued for it. Its
    ``chunks_embedded`` still counts, since that is what its ingestion produced.
    """
    out: dict[str, dict[str, int]] = {s["id"]: _empty() for s in sources if s.get("id")}
    if not out:
        return out
    ids = list(out)
    ph = ",".join("?" * len(ids))

    for row in store.db.execute(
        f"SELECT source_id, status, COUNT(*) AS cnt FROM folder_file_state "  # noqa: S608
        f"WHERE source_id IN ({ph}) GROUP BY source_id, status", ids
    ).fetchall():
        entry = out.get(row["source_id"])
        if entry is None:
            continue
        status = row["status"] or "pending"
        cnt = row["cnt"]
        entry["files_total"] += cnt
        if status in _COMPLETED_STATUSES:
            entry["files_done"] += cnt
        elif status == "failed":
            entry["files_failed"] += cnt
        elif status == "skipped":
            entry["files_skipped"] += cnt
        else:
            entry["files_pending"] += cnt

    for row in store.db.execute(
        f"SELECT source_id, COUNT(*) AS cnt FROM items "  # noqa: S608
        f"WHERE source_id IN ({ph}) AND embedding IS NOT NULL GROUP BY source_id", ids
    ).fetchall():
        entry = out.get(row["source_id"])
        if entry is not None:
            entry["chunks_embedded"] = row["cnt"]

    terminal = sorted(_TERMINAL_STATUSES)
    terminal_ph = ",".join("?" * len(terminal))
    outstanding: dict[str, list[tuple[str, float]]] = {}
    for row in store.db.execute(
        f"SELECT source_id, file_path, mtime FROM folder_file_state "  # noqa: S608
        f"WHERE source_id IN ({ph}) "
        f"AND COALESCE(status, 'pending') NOT IN ({terminal_ph})", ids + terminal
    ).fetchall():
        if row["source_id"] in out:
            outstanding.setdefault(row["source_id"], []).append(
                (row["file_path"], row["mtime"] or 0.0))

    for source in sources:
        sid = source.get("id") or ""
        paths = outstanding.get(sid)
        if not paths:
            continue
        entry = out[sid]
        # The cap bounds a source's whole ingestion, so what is left of it is the cap
        # less the files already taken -- otherwise a source sitting at its cap would
        # keep reporting work it is never going to do.
        headroom = max_files_prop(_properties(source.get("properties"))) - entry["files_done"]
        if headroom <= 0:
            continue
        # estimate_scan_cost is the same helper the add-source preview uses, applied
        # to the files still outstanding instead of the whole walk: same newest-first
        # order the sweep takes them in, same per-file cap, same one-call-per-chunk
        # plus one-summary-per-file arithmetic. Like that preview it is an order-of-
        # magnitude figure, never a bound the sweep enforces.
        entry["estimated_llm_calls_remaining"] = estimate_scan_cost(
            paths, max_files=headroom)["llm_calls"]
    return out
