"""Ledger → vector index adaptor: git carries text, the local DB carries vectors.

This is the "back to database" half of the git-native sync. `ledger.jsonl` is the
synced source of truth (small, diffable, merge-reconcilable); this module projects it
into Kiro Crew's `VectorMemoryStore` so a *similar* failure — not just a
fingerprint-identical one — can surface a lesson a teammate learned.

**Vectors are never committed.** The embedding model is sha256-pinned, so a vector is
derivable from its text: committing 1024 float32s per entry would add ~390 MB at 100k
entries, conflict unresolvably as binary, and rewrite on every push. Text is ~38 MB at
the same scale and merges. So git moves text; each instance embeds locally. That also
means an instance can rebuild its whole index from a fresh clone with no extra state.

**Import is incremental, which is what makes large ledgers viable.** Re-importing 100k
entries must not re-embed 100k rows. Two guards, in cost order:

1. A local import cursor (``imported.json``) records the entry ids already projected, so
   a repeat import is a set difference — no DB round trip for known entries.
2. ``has_episodic_text`` is the correctness backstop for anything the cursor missed (a
   deleted cursor, a restored backup, a teammate's entry that arrived by merge).

Embedding is **deferred**, not inline. ``write_episodic(defer_embedding=True)`` stores
the row keyword-searchable immediately and leaves the vector NULL; one
``backfill_missing_embeddings()`` sweep at the end embeds the batch and rebuilds FAISS
once. Inline embedding costs ~0.4s per 2000-char chunk on CPU, so a 10k import would
hold the caller for over an hour; deferral turns that into one batched pass. The tradeoff
is real and documented upstream: a deferred row is absent from *vector* search until the
sweep runs, so this module always runs it.

Deferral also skips the store's similarity dedup (it needs a vector), which is exactly
why the two guards above are this module's job rather than the store's.

See ``docs/system-specs/modules/ops-mission-control.md`` § Ledger sync.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

APP_NAME = "ops-mission-control"

#: Local, per-instance import cursor. NOT synced: it records what THIS machine has
#: projected into ITS index, so pushing it would tell a teammate their index holds rows
#: it does not. Lives beside the ledger and is safe to delete — the next import falls
#: back to ``has_episodic_text`` and re-projects.
CURSOR_FILENAME = "ledger_index_cursor.json"

#: The store rejects episodic text outside these bounds, so clamp rather than let rows
#: silently fail to write. Mirrors ``vector_memory._EPISODIC_TEXT_MIN/MAX``.
TEXT_MIN = 10
TEXT_MAX = 2000

#: Tag applied to every projected row, so ledger-derived memories are distinguishable
#: from ordinary episodic ones and can be tag-filtered in search.
SOURCE_TAG = "ops-ledger"

#: Rows per import call. Bounds the work one dispatch cycle can trigger; the remainder
#: is picked up next time, so a 100k first import drains over successive cycles instead
#: of stalling one.
MAX_PER_IMPORT = 500


def _cursor_path() -> Any:
    return app_data_dir(APP_NAME) / CURSOR_FILENAME


def _read_cursor() -> set[str]:
    """Entry ids already projected by this instance. Empty on any fault.

    A corrupt or missing cursor must degrade to "re-check everything" rather than
    "assume nothing needs importing" — the second would silently leave the index
    permanently stale.
    """
    try:
        raw = json.loads(_cursor_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return set()
    if isinstance(raw, dict):
        ids = raw.get("imported")
        if isinstance(ids, list):
            return {str(x) for x in ids}
    return set()


def _write_cursor(ids: set[str]) -> None:
    atomic_write(
        _cursor_path(),
        json.dumps({"imported": sorted(ids)}, indent=2),
    )


def entry_text(entry: LedgerEntry) -> str:
    """The searchable text for one ledger entry.

    Pattern AND fix together: the pattern is what a responder recognizes, the fix is
    what they need, and embedding only the pattern would match the right lesson and hand
    back nothing actionable. Clamped to the store's bounds — a lesson too short to be a
    sentence is not worth indexing, and one over the cap is truncated rather than
    dropped, because a truncated fix still points at the answer.
    """
    text = f"{entry.pattern.strip()} — fix: {entry.fix.strip()}".strip()
    return text[:TEXT_MAX]


def import_pending(store: Any, *, limit: int = MAX_PER_IMPORT) -> dict[str, int]:
    """Project not-yet-indexed ledger entries into the vector store.

    ``store`` is injected rather than imported so this module stays testable without a
    real SQLite/FAISS pair, and so a caller that already holds the shared store does not
    open a second connection to the same files.

    Returns counts: ``{"scanned", "written", "skipped", "embedded"}``. Never raises —
    the index is an enhancement to matching, and failing to build it must not fail the
    cycle that asked for it.
    """
    result = {"scanned": 0, "written": 0, "skipped": 0, "embedded": 0}
    try:
        entries = ledger.read_entries()
    except Exception:  # noqa: BLE001 — a ledger fault is reported, never fatal here
        logger.exception("ops-mission-control: could not read ledger for indexing")
        return result

    result["scanned"] = len(entries)
    cursor = _read_cursor()
    pending = [e for e in entries if e.entry_id not in cursor]
    if not pending:
        return result

    newly: set[str] = set()
    for entry in pending[:limit]:
        text = entry_text(entry)
        if len(text) < TEXT_MIN:
            # Too short for the store to accept. Cursor it anyway so we do not re-scan
            # it forever; it is not a failure, just not indexable.
            newly.add(entry.entry_id)
            result["skipped"] += 1
            continue
        try:
            # deferred: the batch is embedded once by the sweep below.
            # preserve_existing: import is merge-only — it must never tombstone a row
            # another writer owns, which is what this flag exists for upstream.
            wrote = store.write_episodic(
                text,
                tags=[SOURCE_TAG, f"confidence:{entry.confidence}", f"trust:{entry.trust}"],
                importance=_importance(entry),
                source="ops-ledger-import",
                preserve_existing=True,
                defer_embedding=True,
            )
        except Exception:  # noqa: BLE001 — one bad row must not abort the import
            logger.exception("ops-mission-control: failed to index ledger entry %s", entry.entry_id)
            continue
        newly.add(entry.entry_id)
        if wrote:
            result["written"] += 1
        else:
            # Already present (the store's own exact-text check) — cursor it so the
            # next import does not pay for the round trip again.
            result["skipped"] += 1

    if newly:
        _write_cursor(cursor | newly)

    if result["written"]:
        try:
            result["embedded"] = int(store.backfill_missing_embeddings() or 0)
        except Exception:  # noqa: BLE001 — rows stay keyword-searchable if this fails
            logger.exception("ops-mission-control: embedding backfill failed")
        sel().log_api_access(
            caller="core:ops-mission-control",
            operation="ledger_index_import",
            outcome="success",
            resources=f"written={result['written']} embedded={result['embedded']}",
        )
    return result


def _importance(entry: LedgerEntry) -> float:
    """Map ledger confidence/trust onto the store's 0..1 importance.

    A verified, high-confidence, frequently-used lesson should outrank a one-off
    observation in decay scoring — otherwise the index ranks by recency alone and the
    ledger's own quality signals are thrown away at the boundary.
    """
    score = 0.4
    if entry.trust == "verified":
        score += 0.25
    if entry.confidence == "high":
        score += 0.20
    if entry.use_count >= 3:
        score += 0.15
    return min(1.0, score)


def search_similar(store: Any, query: str, *, limit: int = 5) -> list[dict]:
    """Ledger-derived memories similar to ``query``, most relevant first.

    Tag-filtered to ``SOURCE_TAG`` so an ops investigation searching for a failure does
    not get back unrelated conversational memories — the index is shared with the rest
    of Kiro Crew, and an ops query wants ops knowledge.

    Returns ``[]`` on any fault: semantic recall is additive to fingerprint matching,
    never a prerequisite for it.
    """
    if not query.strip():
        return []
    try:
        return list(
            store.search_episodic(
                query_text=query,
                limit=limit,
                tag_filter=[SOURCE_TAG],
            )
            or []
        )
    except Exception:  # noqa: BLE001 — never break an investigation over search
        logger.exception("ops-mission-control: semantic ledger search failed")
        return []


def reset_cursor() -> None:
    """Forget what was imported, forcing a full re-projection.

    For an operator whose index was rebuilt or lost. Deliberately does NOT delete rows:
    ``has_episodic_text`` and ``preserve_existing`` make re-import idempotent, so the
    safe operation is to re-scan rather than to destroy and rebuild.
    """
    try:
        _cursor_path().unlink(missing_ok=True)
    except OSError:
        logger.exception("ops-mission-control: could not reset the index cursor")
