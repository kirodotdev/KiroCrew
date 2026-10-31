"""What a consolidation pass knows about the rows it cannot show whole.

A pass hands the model a bounded ``## Current Semantic Memory`` table. Over its cap the
table is filled by relevance to the span being consolidated and by a slice of the rest
that rotates through the key order from pass to pass. ``score_consolidation_rows``
gives each active row its relevance to the span: the V1 chat path's per-row score
(``text_scoring._semantic_row_score``) of a stemmed keyword overlap, over the span's words
that few rows share (``_COMMON_WORD_SHARE``), and the cosine of ONE embedding of the
span's user text against the row's stored write-time vector.
``consolidation_rotation_cursor`` and ``advance_consolidation_rotation`` read and
compare-and-set the last key the rotating slice showed, which lives in this store
file's ``memory_meta`` so it survives a restart.

The functions are bound on ``VectorMemoryStore`` as the class attributes of the same
name and take the store as ``self``. Store fields read: ``db``, ``_db_lock`` and
``space_generation``; every other ``self.`` name they use is a store method.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from typing import TYPE_CHECKING

from kiro_crew.embeddings_settings import (
    _MAX_EMBED_CHARS,
    PRIORITY_BULK,
    EmbeddingWork,
    embedding_work,
)
from kiro_crew.vector_memory_runtime import text_scoring as _text_scoring

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")

#: ``memory_meta`` key of the last key the consolidation table's rotating slice showed.
#: Its value is a key that already exists in this file (tombstones keep their keys).
_ROTATION_CURSOR_META_KEY = "consolidation.semantic_rotation_after"

#: A query word held by more than ``max(1, int(n * _COMMON_WORD_SHARE))`` of the *n*
#: scored rows is left out of the keyword overlap, so a word only one row holds always
#: counts, on a table of any size: it is the most telling word there is. A span is
#: many messages, so its words cover most of the language, and the overlap term caps
#: at ten shared words: counted, the words most rows share (``the``, ``is``,
#: ``project``) give every long row the whole keyword term and tell none of them apart.
_COMMON_WORD_SHARE = 0.1

#: Held while a consolidation pass's call of :func:`score_consolidation_rows` runs, taken
#: and given back on the pool thread that runs it. The pass stops waiting for its scores
#: at a bound but cannot stop that thread, which keeps one of the background memory
#: pool's two admissions until the call returns; a pass that finds this held takes no
#: scores, so scorers that passes gave up on hold at most one admission between them.
_SCORER_IN_FLIGHT = threading.Lock()


def score_consolidation_rows(
    self: VectorMemoryStore,
    rows: list[dict],
    texts: list[str],
    query_text: str,
    *,
    deadline: float | None = None,
) -> list[float] | None:
    """Score each of *rows* for relevance to one consolidation span, or ``None``.

    *rows* are active semantic rows as ``get_all_semantic`` returns them, *texts* the
    value text the consolidation table renders for each (a lesson's rule prose, not
    its JSON envelope), one to one, and *query_text* the span's user text. A row scores
    as ``_semantic_candidates_v1`` scores it, through ``_semantic_row_score``: the
    stemmed overlap of the query with the key and its recorded identity, weighted 3,
    and with the rendered value, blended with the cosine of the query's vector against
    the row's stored one. No row is re-embedded, and a row with no stored vector scores
    on the keyword term of the same scale. Unlike a chat turn's one message, the query is a
    whole span, so a word held by more than ``max(1, int(n * _COMMON_WORD_SHARE))``
    of the *n* rows does not count towards the overlap; a word only one row holds
    always counts.

    The query is embedded once, at ``PRIORITY_BULK``, from its LAST ``_MAX_EMBED_CHARS``
    characters: the embedder keeps the head of what it is given, and the newest
    statements of a span are the likeliest to change a fact. It is ``_try_embed``'s
    embed whatever ``embed_fn`` holds, so a store whose model was missing when it
    opened binds one from ``embed_fn_factory`` here, as on any other embed. The keyword
    overlap reads the whole query. A vector is used only in the space it came from: the
    space generation is read before the embed, and a model swap that lands before the
    scan ends drops the vector, so every row scores on keywords alone -- as they do
    when no embedder is bound and none can be, or after an embed that fails. Rows read
    before the embed are the caller's to keep in one space (the pass reads the
    generation before its fetch).

    *deadline*, a ``time.monotonic()`` instant, bounds the embed: it is this thread's
    ``embedding_work`` for the embed alone, so the shared embedder's queue gives up on
    the query there and the rows score on keywords. A re-bind's probe runs under it
    too, and a probe it cuts short binds nothing: a later embed binds the embedder once
    the re-bind cooldown has run. Without one a bulk embed waits for as long as the
    queue takes. It is a parameter rather than a deadline the caller sets, so it bounds
    this embed and nothing else: the consolidation pass runs the scorer on the
    background memory pool (``run_in_memory_background_pool``), which runs a call in a
    copy of the caller's context, where an ``embedding_work`` the pass set would bound
    its later store calls as well until it was reset. The pass runs one call at a time
    in the process (``_SCORER_IN_FLIGHT``), since a call it stopped waiting for keeps
    its admission to that pool until it returns.

    Never raises: a consolidation pass that raises before it is billed re-arms on the
    next idle tick with no backoff, and a relevance signal is not worth that. Any
    error, or *texts* that are not one to one with *rows*, returns ``None``, which the
    caller reads as no signal.
    """
    try:
        if len(texts) != len(rows):
            return None
        row_tokens = _text_scoring._row_stem_tokens_for_scan(2 * len(rows))
        identities = self._fact_identities()
        key_words = []
        for row in rows:
            key = str(row["key"])
            identity = identities.get(key)
            label = (
                key if identity is None else self._fact_label({"key": key, "identity": identity})
            )
            key_words.append(_text_scoring._semantic_key_words(label, row_tokens))
        val_words = [row_tokens(str(text).lower()) for text in texts]
        query_words = _text_scoring._stem_words(set(re.findall(r"\w+", query_text.lower())))
        held = Counter(
            word
            for keys, values in zip(key_words, val_words)
            for word in query_words & (keys | values)
        )
        common = max(1, int(len(rows) * _COMMON_WORD_SHARE))
        query_words = {word for word in query_words if held[word] <= common}
        generation = self.space_generation
        query_embedding = None
        # Not gated on ``embed_fn``: ``_try_embed`` binds a missing one from the factory.
        if query_text.strip():
            work = embedding_work.get()
            if deadline is not None:
                work = EmbeddingWork(
                    deadline=deadline if work is None else min(deadline, work.deadline),
                    cancelled=threading.Event() if work is None else work.cancelled,
                    priority=PRIORITY_BULK,
                )
            token = embedding_work.set(work)
            try:
                query_embedding = self._try_embed(query_text[-_MAX_EMBED_CHARS:], PRIORITY_BULK)
            finally:
                embedding_work.reset(token)
        if not isinstance(query_embedding, list) or not query_embedding:
            query_embedding = None
        cosines: list[float] | None = None
        if query_embedding is not None:
            similarity = self._stored_similarity_scorer(query_embedding)
            # Clamped as the chat path does: the query has a vector, so a negative
            # cosine would otherwise pull a merely dissimilar row below its keyword floor.
            cosines = [max(0.0, float(similarity(row))) for row in rows]
            if self.space_generation != generation:
                query_embedding, cosines = None, None
        return [
            _text_scoring._semantic_row_score(
                query_words,
                keys,
                values,
                cosines[index] if cosines is not None else 0.0,
                query_has_vector=query_embedding is not None,
            )
            for index, (keys, values) in enumerate(zip(key_words, val_words))
        ]
    except Exception:
        logger.debug("Consolidation relevance scoring failed", exc_info=True)
        return None


def consolidation_rotation_cursor(self: VectorMemoryStore) -> str | None:
    """The last key the consolidation table's rotating slice showed, or ``None``.

    ``None`` when no slice has been recorded yet, when the stored value is not text,
    and when the read fails: the rotation then starts from the first key, which shows
    a slice again rather than skipping one. The column is ``TEXT``, but SQLite keeps a
    BLOB written into it as a BLOB, which ``_read_meta`` would render as ``"b'...'"``,
    so the type is checked in the query; the next advance from ``None`` replaces such
    a value. Never raises, for the same reason :func:`score_consolidation_rows` does
    not.
    """
    try:
        row = self._fetch_one_locked(
            "SELECT value FROM memory_meta WHERE key = ? AND typeof(value) = 'text'",
            (_ROTATION_CURSOR_META_KEY,),
        )
    except Exception:
        logger.debug("Consolidation rotation cursor could not be read", exc_info=True)
        return None
    return None if row is None else row["value"]


def advance_consolidation_rotation(self: VectorMemoryStore, expected: str | None, new: str) -> bool:
    """Move the rotation cursor to *new* only while it still reads *expected*.

    *expected* is the cursor the pass read before it built its table (``None`` when
    none was recorded, or what was stored is not text) and *new* the last key its
    rotating slice showed. An advance from ``None`` writes *new* when the key is
    absent or holds a non-text value, so such a value cannot pin the rotation to its
    first slice. The compare and the write are one statement, so the check holds
    across processes sharing the file as well as threads: two passes that read the
    same cursor showed the same slice, and the second to advance finds the cursor
    moved and changes nothing, unless the cursor has since come round the whole
    table to that key again, when it advances over the slice it showed like any
    pass. A slice can be shown twice that way, never skipped: an advance moves over
    only rows its slice showed or passed as too wide, and keys added after its pass
    read the table, which wait for the next sweep.

    Returns whether the cursor moved. Never raises: a cursor that did not move shows
    the same slice on the next pass, which loses nothing.
    """
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    if not isinstance(new, str) or not (expected is None or isinstance(expected, str)):
        return False
    try:
        with self._db_lock:
            try:
                if expected is None:
                    cursor = self.db.execute(
                        "INSERT INTO memory_meta (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                        "updated_at = excluded.updated_at "
                        "WHERE typeof(memory_meta.value) <> 'text'",
                        (_ROTATION_CURSOR_META_KEY, new, vm._now_iso()),
                    )
                else:
                    cursor = self.db.execute(
                        "UPDATE memory_meta SET value = ?, updated_at = ? "
                        "WHERE key = ? AND value = ?",
                        (new, vm._now_iso(), _ROTATION_CURSOR_META_KEY, expected),
                    )
                moved = cursor.rowcount == 1
                self.db.commit()
            except BaseException:
                # The connection opens an implicit transaction on DML, so a failed
                # statement would leave it open for the next writer's BEGIN.
                self.db.rollback()
                raise
    except Exception as exc:
        logger.warning(
            "Consolidation rotation cursor not advanced (%s); the next pass shows the "
            "same slice again",
            type(exc).__name__,
        )
        return False
    return moved
