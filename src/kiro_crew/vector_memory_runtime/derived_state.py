"""When state derived from the rows stops being current.

A vector is current only while the embedding space it was inferred in is the one the store
records (``_embedding_token``, ``_embedding_current``), and a managed vector is published
under the config write lock an explicit embedding-model apply holds
(``_embedding_config_guard``). The resident episodic and semantic scoring sets are dropped
by a generation bump on every scoring-relevant write (``_invalidate_episodic_scoring``,
``_invalidate_semantic_scoring``, ``invalidate_semantic_content``) and checked against other
connections' commits through ``PRAGMA data_version`` (``_sqlite_data_version``). The
transaction a vector is published in, ``_vector_commit``, stays on the class and calls
these.

The functions are bound on ``VectorMemoryStore`` as the class attributes of the same name
and take the store as ``self``. Store fields written: ``_episodic_scoring``,
``_episodic_scoring_generation``, ``_semantic_scoring`` and
``_semantic_scoring_generation``. Read: ``_db_lock``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from kiro_crew.vector_memory_runtime.embedding import _EmbeddingVector

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore


def invalidate_semantic_content(self: VectorMemoryStore) -> None:
    """Drop the resident semantic scoring set after a content edit.

    Call after an edit transaction commits a change to `fact`/`directive`
    (`semantic_memory`) rows on this store's own connection. An own-connection
    commit does not move ``PRAGMA data_version`` and an external edit path does
    not bump the write-time generation, so without this the resident snapshot
    keeps ranking a tombstoned or stale row into context until an unrelated
    local write or a restart. The twin of :meth:`invalidate_episode_content`.
    """
    self._invalidate_semantic_scoring()


def _invalidate_episodic_scoring(self: VectorMemoryStore) -> None:
    """Drop the resident episodic scoring set.

    Called by every writer that changes which episodic rows are scored, or
    what any of them scores as. Bumping the generation as well as clearing
    the reference is what makes it safe to call WITHOUT ``_db_lock``: a set
    built from a read that started before the bump carries the old
    generation, so it is rejected on the next lookup rather than installed
    over this invalidation.

    NOT called by :meth:`_touch_last_accessed` — ``last_accessed_at`` is
    never scored and is re-read per search from the winners' row bodies, so
    dropping the set on the search path's own write would make it useless.
    """
    self._episodic_scoring_generation += 1
    self._episodic_scoring = None


def _invalidate_semantic_scoring(self: VectorMemoryStore, key: str | None = None) -> None:
    """Retire cached query-aware semantic rows after a scoring-relevant write.

    The generation always moves. With *key*, the write changed that one fact and
    the resident set is patched to match instead of dropped (see
    :mod:`semantic_scoring_patch`, which drops it when it cannot be sure). Without
    one, the set is dropped. Call with *key* only holding ``_db_lock`` and after
    the write is applied on this connection.
    """
    if key is not None:
        from kiro_crew.vector_memory_runtime import (  # imported late: it imports semantic
            semantic_scoring_patch,
        )

        semantic_scoring_patch.patch_semantic_scoring(self, key)
        return
    self._semantic_scoring_generation += 1
    self._semantic_scoring = None


def _sqlite_data_version(self: VectorMemoryStore) -> int | None:
    """``PRAGMA data_version``, or None when the library predates it.

    Moves when another CONNECTION commits to this database, and deliberately
    not for this connection's own commits, which is exactly the half of the
    validity token the in-process generation cannot cover. Costs a few
    microseconds. An sqlite older than 3.9.0 returns no row rather than
    raising, so a missing value is treated as "cannot detect", not as zero.
    """

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    sqlite3 = vm.sqlite3

    try:
        with self._db_lock:
            row = self.db.execute("PRAGMA data_version").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError, IndexError):
        return None


def _embedding_token(self: VectorMemoryStore) -> tuple[str | None, str]:
    with self._db_lock:
        return self.recorded_embedding_space(), self.recorded_rebuild_generation()


def _embedding_current(self: VectorMemoryStore, vector: list[float] | None) -> bool:
    if not isinstance(vector, _EmbeddingVector):
        return True
    if vector.space_token != self._embedding_token():
        return False
    if vector.managed:
        from kiro_crew.embeddings import embedding_rebuild_generation

        requested = embedding_rebuild_generation()
        if requested and requested != vector.space_token[1]:
            return False
    return True


@contextmanager
def _embedding_config_guard(self: VectorMemoryStore, vector: list[float] | None):
    """Serialize managed vector publication against explicit config apply."""
    from kiro_crew.config.loader import _config_write_lock, _lock_target, config_path

    if isinstance(vector, _EmbeddingVector) and vector.managed:
        with _config_write_lock(_lock_target(config_path())):
            yield
    else:
        yield
