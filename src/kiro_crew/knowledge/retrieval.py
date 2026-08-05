"""HybridRetriever -- FTS5 keyword + graph + optional vector, fused with RRF."""

from __future__ import annotations

import json
import logging
import math
import struct
from collections import defaultdict

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

from .store import KnowledgeStore

logger = logging.getLogger(__name__)

# Recall for natural-language queries.
# Common English stopwords + connective phrasing are dropped before FTS5
# matching so a query like "VoC related to Budget Planning" does not require the
# literal tokens "related"/"to" to appear in a matching document.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "about", "be", "been", "by", "do",
    "does", "did", "for", "from", "how", "in", "into", "is", "it", "its", "of",
    "on", "or", "related", "that", "the", "their", "them", "then", "there",
    "these", "this", "those", "to", "was", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "i", "we", "you",
})

# Weight applied to the vector leg in RRF fusion so semantically-strong matches
# dominate when the keyword leg returns weak/literal junk.
VECTOR_RRF_WEIGHT = 2.0


class HybridRetriever:
    """FTS5 keyword + graph traversal + optional vector search, fused with RRF."""

    def __init__(self, store: KnowledgeStore, embedder=None):
        """store: KnowledgeStore instance. embedder: optional callable(str) -> list[float]."""
        self.store = store
        self.embedder = embedder

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Hybrid search with RRF fusion. Returns [{id, title, summary, content, score, source, match_type}]."""
        kw = self._keyword_search(query, limit=limit * 2)
        gr = self._graph_search(query, limit=limit * 2)
        vec = self._vector_search(query, limit=limit * 2)

        # Vector leg is weighted higher so semantic matches dominate when the
        # keyword leg is weak. Weights align positionally
        # with (kw, gr, vec).
        fused = self._rrf_fuse(kw, gr, vec, weights=(1.0, 1.0, VECTOR_RRF_WEIGHT))

        # Batch-fetch all candidate items once
        all_ids = [item_id for item_id, _ in fused]
        items_cache: dict[str, dict] = {}
        for item_id in all_ids:
            item = self.store.get_item(item_id)
            if item:
                items_cache[item_id] = item

        # Tie-break by recency (newer docs win)
        def _sort_key(item_score: tuple[str, float]) -> tuple[float, str]:
            item_id, score = item_score
            updated = items_cache.get(item_id, {}).get("updated_at", "")
            return (score, updated)

        fused.sort(key=_sort_key, reverse=True)

        # Track which lists each item appeared in
        kw_ids = {i for i, _ in kw}
        gr_ids = {i for i, _ in gr}
        vec_ids = {i for i, _ in (vec or [])}

        results = []
        for item_id, score in fused[:limit]:
            item = items_cache.get(item_id)
            if not item:
                continue
            types = []
            if item_id in kw_ids:
                types.append("keyword")
            if item_id in gr_ids:
                types.append("graph")
            if item_id in vec_ids:
                types.append("vector")
            results.append({
                "id": item_id,
                "title": item["title"],
                "summary": item.get("summary"),
                "content": item["content"],
                "score": score,
                "source": item.get("source_id"),
                "match_type": "+".join(types),
            })

        self._attach_source_locations(results)
        self._attach_citation_sources(results)
        return results

    def _attach_source_locations(self, results: list[dict]) -> None:
        """Enrich results in place with citation metadata (section + line range).

        Batch-fetches ``source_locations`` for every result item_id in one query
        (avoids N+1) and adds ``section_title``, ``chunk_range`` and ``anchor``.
        Item == chunk is the norm, so the first location row per item_id is used.
        Results without a stored location are left unchanged (degrade to
        name-only display downstream).
        """
        if not results:
            return
        item_ids = [r["id"] for r in results]
        placeholders = ",".join("?" for _ in item_ids)
        try:
            rows = self.store.db.execute(
                "SELECT item_id, chunk_range, section_title, anchor "
                f"FROM source_locations WHERE item_id IN ({placeholders})",  # noqa: S608
                item_ids,
            ).fetchall()
        except sqlite3.OperationalError:
            return
        loc_by_item: dict[str, sqlite3.Row] = {}
        for row in rows:
            loc_by_item.setdefault(row["item_id"], row)
        for result in results:
            loc = loc_by_item.get(result["id"])
            if loc:
                result["section_title"] = loc["section_title"]
                result["chunk_range"] = loc["chunk_range"]
                result["anchor"] = loc["anchor"]

    def _attach_citation_sources(self, results: list[dict]) -> None:
        """Enrich results in place with source identity + a per-document locator.

        Adds ``source_type``, ``source_name`` and ``source_uri`` (resolved once
        per distinct source id). Then attaches the most specific locator the
        source type affords, reusing the ingest-time reverse maps:

        - folder/vault sources -> ``file_path`` (the specific file within the
          folder, from ``folder_file_state``), since the source uri is only the
          folder root.
        - the aggregate artifact source -> ``artifact_slug`` + ``artifact_name``
          (from ``artifact_item_state``), so a citation can name the artifact
          and deep-link to ``/artifacts/<slug>``.
        - every other source type -> nothing extra; ``source_uri`` is already the
          document locator (uploads, quip, etc.).

        Results whose source is missing or unmapped degrade cleanly -- the extra
        keys are simply absent.
        """
        if not results:
            return
        source_ids = {r["source"] for r in results if r.get("source")}
        if not source_ids:
            return
        sid_list = list(source_ids)
        placeholders = ",".join("?" for _ in sid_list)
        meta: dict[str, sqlite3.Row] = {}
        try:
            for row in self.store.db.execute(
                "SELECT id, name, source_type, uri "
                f"FROM sources WHERE id IN ({placeholders})",  # noqa: S608
                sid_list,
            ).fetchall():
                meta[row["id"]] = row
        except sqlite3.OperationalError:
            return

        folder_sids = [
            sid for sid in sid_list
            if meta.get(sid) is not None
            and meta[sid]["source_type"] in ("local_folder", "obsidian_vault")
        ]
        artifact_sids = [
            sid for sid in sid_list
            if meta.get(sid) is not None and meta[sid]["source_type"] == "artifact"
        ]

        item_to_file: dict[str, str] = {}
        for sid in folder_sids:
            for row in self.store.db.execute(
                "SELECT file_path, item_ids FROM folder_file_state WHERE source_id = ?",
                (sid,),
            ).fetchall():
                try:
                    ids = json.loads(row["item_ids"]) if row["item_ids"] else []
                except (json.JSONDecodeError, TypeError):
                    continue
                for item_id in ids:
                    item_to_file[item_id] = row["file_path"]

        item_to_artifact: dict[str, tuple] = {}
        for sid in artifact_sids:
            for row in self.store.db.execute(
                "SELECT slug, name, item_ids FROM artifact_item_state WHERE source_id = ?",
                (sid,),
            ).fetchall():
                try:
                    ids = json.loads(row["item_ids"]) if row["item_ids"] else []
                except (json.JSONDecodeError, TypeError):
                    continue
                for item_id in ids:
                    item_to_artifact[item_id] = (row["slug"], row["name"])

        for result in results:
            sid = result.get("source")
            row = meta.get(sid) if isinstance(sid, str) else None
            if row is not None:
                result["source_type"] = row["source_type"]
                result["source_name"] = row["name"]
                result["source_uri"] = row["uri"]
            file_path = item_to_file.get(result["id"])
            if file_path:
                result["file_path"] = file_path
            artifact = item_to_artifact.get(result["id"])
            if artifact:
                result["artifact_slug"], result["artifact_name"] = artifact

    def _keyword_search(self, query: str, limit: int = 20) -> list[tuple[str, int]]:
        """FTS5 search. Returns [(item_id, rank)] where rank is position (1=best)."""
        safe_query = self._sanitize_fts5_query(query)
        if not safe_query:
            return []
        try:
            rows = self.store.db.execute(
                "SELECT i.id FROM items_fts fts "
                "JOIN items i ON i.rowid = fts.rowid "
                "WHERE items_fts MATCH ? ORDER BY fts.rank LIMIT ?",
                (safe_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(row["id"], rank + 1) for rank, row in enumerate(rows)]

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Escape user input for safe FTS5 MATCH usage.

        Each token is individually double-quoted (with internal quotes doubled)
        so it is treated as a literal FTS5 string -- the user's input never
        contributes FTS5 operators (parameterized quoting). Stopwords are dropped
        and the remaining tokens OR-joined
        so natural-language queries no longer require every
        literal token to appear in a matching document.
        """
        raw_tokens = [t for t in query.split() if t]
        if not raw_tokens:
            return ""
        tokens = [t for t in raw_tokens if t.lower() not in _STOPWORDS]
        # If the query is *all* stopwords, fall back to the raw tokens rather
        # than returning an empty match (which would drop the keyword leg).
        if not tokens:
            tokens = raw_tokens
        quoted = ['"' + t.replace('"', '""') + '"' for t in tokens]
        return " OR ".join(quoted)

    def _graph_search(self, query: str, limit: int = 20) -> list[tuple[str, int]]:
        """Find entities matching query terms, traverse graph, rank items by mention count."""
        words = query.split()
        # Try individual words and consecutive pairs
        candidates = list(words)
        for i in range(len(words) - 1):
            candidates.append(f"{words[i]} {words[i + 1]}")

        entity_ids = set()
        for term in candidates:
            ent = self.store.find_entity(term)
            if ent:
                entity_ids.add(ent["id"])

        if not entity_ids:
            return []

        # Expand via graph neighbors (depth=2)
        all_entity_ids = set(entity_ids)
        for eid in entity_ids:
            for neighbor in self.store.get_neighbors(eid, depth=2):
                all_entity_ids.add(neighbor["id"])

        # Count item mentions
        item_counts: dict[str, int] = defaultdict(int)
        placeholders = ",".join("?" * len(all_entity_ids))
        rows = self.store.db.execute(
            f"SELECT item_id, COUNT(*) as cnt FROM mentions "  # noqa: S608
            f"WHERE entity_id IN ({placeholders}) GROUP BY item_id ORDER BY cnt DESC LIMIT ?",
            (*all_entity_ids, limit),
        ).fetchall()
        for row in rows:
            item_counts[row["item_id"]] = row["cnt"]

        sorted_items = sorted(item_counts.items(), key=lambda x: x[1], reverse=True)
        return [(item_id, rank + 1) for rank, (item_id, _) in enumerate(sorted_items)]

    def _vector_search(self, query: str, limit: int = 20) -> list[tuple[str, int]] | None:
        """Brute-force cosine similarity against stored embeddings. Returns None if no embedder."""
        if self.embedder is None:
            return None

        query_vec = self.embedder(query)
        if not query_vec:
            return None
        rows = self.store.db.execute(
            "SELECT id, embedding FROM items WHERE embedding IS NOT NULL AND status = 'active'"
        ).fetchall()

        scored = []
        mismatched = 0
        q_len = len(query_vec)
        for row in rows:
            item_vec = _bytes_to_floats(row["embedding"])
            if not item_vec:
                continue
            if len(item_vec) != q_len:
                # Incomparable dims (embedding model/dimension changed between
                # ingestion and query). _cosine_similarity would return 0.0; skip
                # the item entirely so all-zero "ghost" results can't fill the
                # top-K when vector search is the only signal.
                mismatched += 1
                continue
            sim = self._cosine_similarity(query_vec, item_vec)
            if sim > 0.0:
                scored.append((row["id"], sim))

        if mismatched:
            # One log line per search (not per item) — gives operators a signal
            # that a model swap / re-index left the index degraded, without
            # instrumenting the pure static helper.
            logger.warning(
                "Knowledge vector search: skipped %d/%d items with mismatched "
                "embedding dimension (query=%d) — search may be degraded; re-index needed",
                mismatched,
                len(rows),
                q_len,
            )

        scored.sort(key=lambda x: x[1], reverse=True)
        return [(item_id, rank + 1) for rank, (item_id, _) in enumerate(scored[:limit])]

    @staticmethod
    def _rrf_fuse(*ranked_lists, k: int = 60, weights=None) -> list[tuple[str, float]]:
        """Reciprocal Rank Fusion across all non-None ranked lists.

        Optional per-list ``weights`` (aligned positionally with
        ``ranked_lists``) let a leg contribute more to the fused score. The
        vector leg is weighted higher so semantic matches dominate when keyword
        matches are weak. When ``weights`` is None every
        leg is weighted 1.0.
        """
        scores: dict[str, float] = defaultdict(float)
        for i, rlist in enumerate(ranked_lists):
            if rlist is None:
                continue
            weight = 1.0 if weights is None else weights[i]
            for item_id, rank in rlist:
                scores[item_id] += weight * (1.0 / (k + rank))
        return sorted(scores.items(), key=lambda x: x[1], reverse=True)

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Cosine similarity. Returns 0.0 for zero vectors.

        Vectors of differing dimensionality are incomparable and return 0.0:
        a freshly embedded query vector is compared against item vectors read
        from the DB, so an embedding-dimension change between ingestion and query
        would otherwise let ``zip`` silently truncate the dot product (while the
        norms use the full vectors), yielding a meaningless similarity. This
        mirrors the length guard already enforced in ``vector_memory.py``.
        """
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)


def _bytes_to_floats(blob: bytes) -> list[float]:
    """Decode embedding blob (binary struct or JSON-encoded list of floats)."""
    if not blob:
        return []
    try:
        # Try JSON format first (legacy)
        result = json.loads(blob)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        pass
    # Try binary format (struct packed floats, must be >8 bytes to avoid false positives)
    if isinstance(blob, bytes) and len(blob) >= 16 and len(blob) % 4 == 0:
        try:
            n = len(blob) // 4
            return list(struct.unpack(f"{n}f", blob))
        except struct.error:
            pass
    return []
