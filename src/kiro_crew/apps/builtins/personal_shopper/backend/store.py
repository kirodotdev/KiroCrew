"""Personal Shopper preference store — sqlite with vector search.

Preferences are ranked by embedding similarity when the shared embedding
backend is serving, and by keyword match when it is not. The two paths are
deliberately NOT interchangeable: a cosine score means "these mean the same
thing", while a keyword score only means "these share words", so only the
vector path is trusted for the identity judgement behind deduplication.

The user's tags are metadata for their own organisation of the list. Retrieval
never consults them, so a tag the user never assigned cannot degrade recall.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import struct
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from kiro_crew.apps.manager import app_data_dir

APP_NAME = "personal-shopper"

# Cosine similarity above which a new entry is treated as a restatement of an
# existing one rather than a new preference. Only ever applied to embedding
# scores — see ``_find_similar``.
_DEDUP_COSINE = 0.9

# Optional: absent on a first boot before the model lands, so every embed call
# returns None and the store degrades to keyword search.
try:
    from kiro_crew.embeddings import get_shared_embedder
except ImportError:  # pragma: no cover - embeddings are an optional extra
    get_shared_embedder = None  # type: ignore[assignment]


class SearchResult(NamedTuple):
    id: str
    text: str
    tags: list[str]
    score: float
    #: True when ``score`` is a cosine similarity, False when it came from the
    #: keyword fallback. Callers that need a semantic guarantee (deduplication)
    #: must check this rather than thresholding ``score`` blindly.
    semantic: bool


def _default_db_path() -> Path:
    """Resolve the store path under the ACTIVE data home.

    Deferred to call time on purpose: ``KIROCREW_HOME`` decides the data home,
    and a module-level constant would bind whichever home happened to be set
    at import — sending a pod's or a test's writes into the real user's data.
    """
    return app_data_dir(APP_NAME) / "preferences.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _embed(text: str) -> bytes | None:
    """Embed *text* as packed float32, or None when no model is serving."""
    if get_shared_embedder is None:
        return None
    embedder = get_shared_embedder()
    if not embedder.is_ready():
        return None
    vec = embedder.embed(text)
    if vec is None:
        return None
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes | None) -> list[float] | None:
    if not blob:
        return None
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, or 0.0 when the two vectors are incomparable.

    A dimension mismatch means the vectors came from different models, so the
    stored one predates a model swap. Scoring it against a truncated prefix
    would invent a similarity, so it scores 0 and drops out of the ranking
    until ``reembed_all`` rebuilds it.
    """
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


_FTS_TOKEN = re.compile(r"\w+", re.UNICODE)


def _fts_query(raw: str) -> str:
    """Turn free text into an FTS5 OR-query of quoted terms.

    Quoting each token is what makes arbitrary user text safe: an unbalanced
    quote or a bare ``*``/``OR`` in the raw string is a query-syntax error to
    FTS5, and passing it through would turn a legitimate search into a silent
    empty result.
    """
    tokens = _FTS_TOKEN.findall(raw)
    return " OR ".join(f'"{t}"' for t in tokens)


class PreferenceStore:
    """SQLite-backed preference store with vector search and keyword fallback."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # One connection shared across the worker threads that `asyncio.to_thread`
        # hands route handlers, so every write goes through `_lock`: sqlite allows
        # a single writer, and the multi-statement FTS sync below must not
        # interleave with another writer's.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # EVERY connection access goes through this lock, reads included: a
        # sqlite3 connection is not safe for concurrent use from several threads
        # even when only one of them writes, and route handlers arrive on
        # whatever threads `asyncio.to_thread` hands out. Reentrant because the
        # write paths call the read paths (`add` consults `search` to dedup).
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        # `preferences_fts` is a standalone FTS index keyed by our own id, NOT an
        # external-content table over `preferences`. External content would make
        # every edit a three-way handshake through the `'delete'` command carrying
        # the OLD text; owning the rows outright means a plain DELETE-then-INSERT
        # by id keeps the index correct.
        with self._lock:
            self._conn.executescript(
                """
            CREATE TABLE IF NOT EXISTS preferences (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                embedding BLOB,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS groups (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                icon TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                problem TEXT NOT NULL,
                advice TEXT DEFAULT '',
                products TEXT DEFAULT '[]',
                feedback TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS preferences_fts
                USING fts5(entry_id UNINDEXED, text);
            """
            )
            self._conn.commit()

    # ── FTS index (callers hold `_lock`) ──

    def _fts_put(self, entry_id: str, text: str) -> None:
        self._conn.execute("DELETE FROM preferences_fts WHERE entry_id = ?", (entry_id,))
        self._conn.execute(
            "INSERT INTO preferences_fts (entry_id, text) VALUES (?, ?)", (entry_id, text)
        )

    def _fts_drop(self, entry_id: str) -> None:
        self._conn.execute("DELETE FROM preferences_fts WHERE entry_id = ?", (entry_id,))

    # ── Preferences ──

    def add(self, text: str, *, tags: list[str] | None = None) -> str:
        """Add a preference, merging into a semantically equivalent existing one.

        Deduplication requires a SEMANTIC match, so it is skipped entirely while
        the store is on the keyword fallback: sharing a word ("shoe size US 10"
        vs "prefers running shoes") is not the same claim, and merging on that
        basis would overwrite an unrelated preference the user still holds.
        """
        # Deduplicate on the way in: duplicates serve no purpose (a tag is
        # set membership) and every consumer that removes one has to remember
        # to remove all of them.
        tags = sorted(set(tags or []))
        # The whole read-then-write is one critical section: between deciding
        # "this is new" and inserting it, a concurrent add of the same
        # preference would otherwise slip in and create a duplicate.
        with self._lock:
            existing = self._find_similar(text)
            if existing is not None:
                # Union, never replace. A group IS a tag holding the group's id,
                # so passing the incoming tags straight through would strip an
                # existing entry out of its group the moment the user re-added an
                # equivalent preference without picking a group -- the entry stays
                # in the list but silently vanishes from the section it was filed
                # under, which reads as data loss.
                merged = sorted(set(existing.tags) | set(tags))
                self.update(existing.id, text=text, tags=merged)
                return existing.id

            entry_id = uuid.uuid4().hex[:8]
            now = _now_iso()
            embedding = _embed(text)
            self._conn.execute(
                "INSERT INTO preferences (id, text, tags, embedding, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (entry_id, text, json.dumps(tags), embedding, now, now),
            )
            self._fts_put(entry_id, text)
            self._conn.commit()
        return entry_id

    def update(
        self,
        entry_id: str,
        *,
        text: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Update an entry's text and/or tags, keeping its vector honest.

        When the text changes but no model is serving, the stored vector is
        CLEARED rather than kept: a vector describing the old text would keep
        matching the entry against queries about wording the user has replaced.
        Dropping it costs the entry its ranking until ``reembed_all`` rebuilds
        it, and never returns it for the wrong query.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT text, tags FROM preferences WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                return
            new_text = row[0] if text is None else text
            new_tags = row[1] if tags is None else json.dumps(tags)

            if text is not None:
                # Re-embed on a text change, or clear a now-stale vector.
                self._conn.execute(
                    "UPDATE preferences SET text=?, tags=?, embedding=?, updated_at=? WHERE id=?",
                    (new_text, new_tags, _embed(new_text), _now_iso(), entry_id),
                )
                self._fts_put(entry_id, new_text)
            else:
                self._conn.execute(
                    "UPDATE preferences SET tags=?, updated_at=? WHERE id=?",
                    (new_tags, _now_iso(), entry_id),
                )
            self._conn.commit()

    def delete(self, entry_id: str) -> None:
        with self._lock:
            self._fts_drop(entry_id)
            self._conn.execute("DELETE FROM preferences WHERE id = ?", (entry_id,))
            self._conn.commit()

    def list_all(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, tags, created_at, updated_at FROM preferences "
                "ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {
                "id": r[0],
                "text": r[1],
                "tags": json.loads(r[2]),
                "created_at": r[3],
                "updated_at": r[4],
            }
            for r in rows
        ]

    # ── Retrieval ──

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        tag_filter: list[str] | None = None,
    ) -> list[SearchResult]:
        """Rank preferences against *query*, by meaning when possible."""
        query_vec = None
        if get_shared_embedder is not None:
            embedder = get_shared_embedder()
            if embedder.is_ready():
                query_vec = embedder.embed(query)

        if query_vec is not None:
            return self._vector_search(query_vec, top_k=top_k, tag_filter=tag_filter)
        return self._fts_search(query, top_k=top_k, tag_filter=tag_filter)

    def _matches_tags(self, tags: list[str], tag_filter: list[str] | None) -> bool:
        return not tag_filter or any(t in tags for t in tag_filter)

    def _vector_search(
        self, query_vec: list[float], *, top_k: int, tag_filter: list[str] | None
    ) -> list[SearchResult]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, tags, embedding FROM preferences "
                "WHERE embedding IS NOT NULL"
            ).fetchall()

        scored: list[SearchResult] = []
        for row_id, text, tags_json, blob in rows:
            tags = json.loads(tags_json)
            if not self._matches_tags(tags, tag_filter):
                continue
            entry_vec = _unpack(blob)
            if entry_vec is None:
                continue
            scored.append(
                SearchResult(
                    id=row_id,
                    text=text,
                    tags=tags,
                    score=_cosine(query_vec, entry_vec),
                    semantic=True,
                )
            )
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    def _fts_search(
        self, query: str, *, top_k: int, tag_filter: list[str] | None
    ) -> list[SearchResult]:
        match = _fts_query(query)
        if not match:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT f.entry_id, p.text, p.tags, f.rank "
                "FROM preferences_fts f JOIN preferences p ON p.id = f.entry_id "
                "WHERE preferences_fts MATCH ? ORDER BY f.rank LIMIT ?",
                (match, top_k * 3),  # over-fetch, tag filtering happens below
            ).fetchall()

        results: list[SearchResult] = []
        for entry_id, text, tags_json, rank in rows:
            tags = json.loads(tags_json)
            if not self._matches_tags(tags, tag_filter):
                continue
            # bm25 rank is negative and unbounded; map it to a monotonic 0-1
            # ordering signal. It is NOT a similarity, hence semantic=False.
            results.append(
                SearchResult(
                    id=entry_id,
                    text=text,
                    tags=tags,
                    score=1.0 / (1.0 + abs(float(rank))),
                    semantic=False,
                )
            )
        return results[:top_k]

    def _find_similar(self, text: str) -> SearchResult | None:
        """The existing entry *text* restates, or None.

        Returns None whenever the ranking was not semantic, which is what keeps
        the keyword fallback from merging merely word-sharing preferences.
        """
        results = self.search(text, top_k=1)
        if not results:
            return None
        top = results[0]
        if not top.semantic:
            return None
        return top if top.score >= _DEDUP_COSINE else None

    # ── Groups ──

    def add_group(self, name: str, *, icon: str = "") -> str:
        """Create a group, or return the existing one with that name.

        The id is opaque rather than a slug of the name. A slug collapses
        distinct names onto one key -- ``Work Shoes`` and ``work-shoes`` both slug
        to ``work-shoes`` -- and the old ``INSERT OR REPLACE`` then destroyed the
        first group's metadata AND silently reassigned every preference tagged
        with that id to the second group.

        Same-name adds still return the original group instead of creating a
        duplicate, which is the idempotency the slug used to provide for free.
        Matching is case-insensitive on the trimmed name.
        """
        clean = name.strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM groups WHERE lower(name) = lower(?)", (clean,)
            ).fetchone()
            if row is not None:
                return str(row[0])

            group_id = uuid.uuid4().hex[:8]
            max_order = self._conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM groups"
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO groups (id, name, icon, sort_order) VALUES (?, ?, ?, ?)",
                (group_id, clean, icon, max_order + 1),
            )
            self._conn.commit()
        return group_id

    def list_groups(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, icon, sort_order FROM groups ORDER BY sort_order"
            ).fetchall()
        return [{"id": r[0], "name": r[1], "icon": r[2], "sort_order": r[3]} for r in rows]

    def delete_group(self, group_id: str) -> None:
        """Remove a group, keeping the preferences that carried its tag.

        A group is the user's filing, not the preference itself — deleting the
        folder must not delete what the advisor knows, so the entries survive
        untagged.
        """
        with self._lock:
            self._conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
            rows = self._conn.execute("SELECT id, tags FROM preferences").fetchall()
            for entry_id, tags_json in rows:
                tags = json.loads(tags_json)
                if group_id in tags:
                    # Strip EVERY occurrence. list.remove drops only the first,
                    # so a preference tagged twice kept a tag pointing at a group
                    # that no longer exists -- it then belongs to no visible group
                    # and is not "ungrouped" either, so it vanishes from the UI
                    # while still sitting in the database.
                    tags = [t for t in tags if t != group_id]
                    self._conn.execute(
                        "UPDATE preferences SET tags = ? WHERE id = ?",
                        (json.dumps(tags), entry_id),
                    )
            self._conn.commit()

    # ── History ──

    def add_history(
        self,
        problem: str,
        *,
        advice: str = "",
        products: list[dict] | None = None,
    ) -> str:
        entry_id = uuid.uuid4().hex[:8]
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO history (id, date, problem, advice, products, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (entry_id, now[:10], problem, advice, json.dumps(products or []), now),
            )
            self._conn.commit()
        return entry_id

    def list_history(self, *, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, date, problem, advice, products, feedback, created_at "
                "FROM history ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "date": r[1],
                "problem": r[2],
                "advice": r[3],
                "products": json.loads(r[4]),
                "feedback": json.loads(r[5]),
                "created_at": r[6],
            }
            for r in rows
        ]

    def update_feedback(self, history_id: str, product_name: str, feedback: str) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT feedback FROM history WHERE id = ?", (history_id,)
            ).fetchone()
            if row is None:
                return
            fb = json.loads(row[0])
            fb[product_name] = feedback
            self._conn.execute(
                "UPDATE history SET feedback = ? WHERE id = ?", (json.dumps(fb), history_id)
            )
            self._conn.commit()

    # ── Maintenance ──

    def reembed_all(self) -> int:
        """Rebuild every vector under the current model. Returns the count written.

        Needed after a model swap (a new model means a new vector space) and to
        fill in entries written while no model was serving.
        """
        with self._lock:
            rows = self._conn.execute("SELECT id, text FROM preferences").fetchall()
            written = 0
            for entry_id, text in rows:
                blob = _embed(text)
                if blob is None:
                    continue
                self._conn.execute(
                    "UPDATE preferences SET embedding = ? WHERE id = ?", (blob, entry_id)
                )
                written += 1
            self._conn.commit()
        return written

    def close(self) -> None:
        with self._lock:
            self._conn.close()
