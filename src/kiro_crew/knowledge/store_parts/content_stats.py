"""Library statistics: admitted content per source, and raw table counts.

``aggregate_stats`` counts active items and documents, totalled and per source, so the
per-source numbers sum to the totals; ``get_stats`` and ``source_count`` report raw
table cardinality. All of them only read.

A function here whose first parameter is ``self`` is a ``KnowledgeStore`` method:
the class binds it under the same name in ``knowledge/store.py``, so it reads the
store's fields and calls its sibling methods through ``self``.

The ``KnowledgeStore`` fields this module uses. Writes: none. Reads: none; every query
goes through ``self.db``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.knowledge.store import KnowledgeStore


@dataclass(frozen=True)
class SourceContentStats:
    """One source's share of the admitted content.

    ``source_id`` is None for the bucket holding items that belong to no source.
    The store deliberately does not spell that bucket with the dashboard's
    ``__none__`` wire sentinel: that string is a contract between the items API
    and the SPA, and a third copy down here in the store would have to change
    with them while nothing in SQLite needs it.
    """

    source_id: str | None
    name: str
    documents: int
    items: int


@dataclass(frozen=True)
class ContentStats:
    """Admitted knowledge content: totals plus the same numbers per source.

    ``sources`` counts registered sources, so it excludes the sourceless bucket
    that ``per_source`` may carry. Both totals reconcile against ``per_source``
    exactly -- summing its ``items`` gives ``items`` and summing its
    ``documents`` gives ``documents`` -- which is the property that makes these
    numbers auditable, and the reason membership here is plain ownership
    (``items.source_id``) rather than the ownership-OR-location rule
    ``knowledge_list_sources`` uses to estimate what a scope would yield. Under
    that rule an item surviving a cross-source dedup collapse counts for two
    sources and the per-source numbers over-sum the totals.
    """

    sources: int
    documents: int
    items: int
    per_source: tuple[SourceContentStats, ...]


def source_count(self: KnowledgeStore) -> int:
    """Total number of registered sources (all types)."""
    row = self.db.execute("SELECT COUNT(*) AS cnt FROM sources").fetchone()
    return int(row["cnt"]) if row else 0


def aggregate_stats(self: KnowledgeStore) -> ContentStats:
    """Admitted content, totalled and broken down by source.

    Distinct from ``get_stats``, which reports raw table cardinality for the
    dashboard overview: this counts ACTIVE items only, because a superseded
    or deduped copy is not content the library will serve, and it resolves
    the two units a reader conflates otherwise. An ``items`` row IS a chunk
    -- the unit ``knowledge_list_sources`` and ``/source-counts`` already
    call an item -- and every chunk of one document carries that document's
    whole-text ``content_hash``, so ``(source_id, content_hash)`` is the
    document identity, the same one ``dedup`` groups on. An item written
    without a content hash is therefore counted in ``items`` and belongs to
    no document.

    Read-only: no write, no repair, no rebuild. A caller that finds the
    numbers wrong has a diagnosis, not a fix.
    """
    totals = self.db.execute(
        "SELECT COUNT(*) AS items, "
        "COUNT(DISTINCT CASE WHEN content_hash IS NOT NULL AND content_hash != '' "
        "  THEN COALESCE(source_id, '') || char(31) || content_hash END) AS documents "
        "FROM items WHERE status = 'active'"
    ).fetchone()
    # char(31) is a unit separator: concatenating the two keys raw would let
    # a source id ending in a hash prefix collide with its neighbour.
    by_source = {
        row["sid"]: row
        for row in self.db.execute(
            "SELECT COALESCE(source_id, '') AS sid, COUNT(*) AS items, "
            "COUNT(DISTINCT CASE WHEN content_hash IS NOT NULL AND content_hash != '' "
            "  THEN content_hash END) AS documents "
            "FROM items WHERE status = 'active' GROUP BY sid"
        ).fetchall()
    }
    per_source: list[SourceContentStats] = []
    source_rows = self.db.execute("SELECT id, name FROM sources ORDER BY name").fetchall()
    for src in source_rows:
        counted = by_source.get(src["id"])
        per_source.append(
            SourceContentStats(
                source_id=src["id"],
                name=src["name"],
                documents=int(counted["documents"]) if counted else 0,
                items=int(counted["items"]) if counted else 0,
            )
        )
    # Every registered source is listed even at zero, so a source that
    # ingested nothing is visible rather than absent. The sourceless bucket
    # is the opposite: it is not a registered row, so it appears only when it
    # holds something. It holds every active item no registered source owns:
    # the NULL-source rows, and any row whose source_id names a source that no
    # longer exists. `items.source_id REFERENCES sources(id)` keeps the second
    # kind out of anything this store writes, but a database written before
    # the foreign key was enforced can still hold one, and a row counted in
    # `items` that appeared on no line would break the reconciliation this
    # breakdown promises. A document is identified by (source_id,
    # content_hash), so summing the per-source_id document counts is exact.
    registered = {src["id"] for src in source_rows}
    unowned = [row for sid, row in by_source.items() if sid not in registered]
    unowned_items = sum(int(row["items"]) for row in unowned)
    if unowned_items > 0:
        per_source.append(
            SourceContentStats(
                source_id=None,
                name="(no source)",
                documents=sum(int(row["documents"]) for row in unowned),
                items=unowned_items,
            )
        )
    return ContentStats(
        sources=len(source_rows),
        documents=int(totals["documents"]) if totals else 0,
        items=int(totals["items"]) if totals else 0,
        per_source=tuple(per_source),
    )


def get_stats(self: KnowledgeStore) -> dict:
    return {
        "items": self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "entities": self.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
        "relations": self.db.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0],
        "sources": self.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
    }
