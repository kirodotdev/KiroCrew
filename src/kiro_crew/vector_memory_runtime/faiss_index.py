"""The FAISS accelerator: an in-memory index derived from SQLite's vectors.

SQLite stays authoritative. The index is rebuilt from the stored episodic
vectors, dropped whenever content or the vector space changes, persisted with a
content signature and both file digests stamped in ``memory_meta`` inside one
immediate transaction, and loaded back only when that stamp still matches the
database and the files. ``faiss`` is optional: every entry point is a no-op
without it, and the search ladder falls back to the stored vectors.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import faiss

    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")


def invalidate_episode_content(store: VectorMemoryStore) -> None:
    """Drop derived vectors after a content edit; SQLite stays authoritative.

    Call after the edit transaction commits. No fallible filesystem/SQL write
    follows the accepted edit: saved indexes are separately checked against
    current SQLite vectors when loaded, including across process restarts.
    """
    with store._db_lock:
        store._faiss_index = None
        store._faiss_id_map = []
        store._faiss_data_version = None
        store._invalidate_episodic_scoring()


def faiss_content_signature(store: VectorMemoryStore) -> str:
    digest = hashlib.sha256()
    for row in store._fetch_all_locked(
        "SELECT id, embedding FROM episodic_memories "
        "WHERE is_deleted=0 AND embedding IS NOT NULL ORDER BY id"
    ):
        identity = row["id"].encode("utf-8")
        vector = bytes(row["embedding"])
        digest.update(struct.pack("!II", len(identity), len(vector)))
        digest.update(identity)
        digest.update(vector)
    return digest.hexdigest()


def build_faiss_index(store: VectorMemoryStore) -> int:
    """Rebuild FAISS index from all episodic embeddings in SQLite. Returns count."""
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    if not vm._HAS_FAISS or not vm._HAS_NUMPY:
        return 0
    version = store._sqlite_data_version()
    store._faiss_index = vm.faiss.IndexFlatIP(store._embedding_dim)
    store._faiss_id_map = []
    rows = store._fetch_all_locked(
        "SELECT id, embedding FROM episodic_memories "
        "WHERE is_deleted = 0 AND embedding IS NOT NULL"
    )
    skipped = 0
    for row in rows:
        vec = vm.np.frombuffer(row["embedding"], dtype=vm.np.float32).reshape(1, -1)
        if vec.shape[1] != store._embedding_dim:
            skipped += 1
            continue
        store._faiss_index.add(vec)  # type: ignore[union-attr,attr-defined]
        store._faiss_id_map.append(row["id"])
    if skipped:
        logger.warning(
            "Skipped %d episodic entries with mismatched embedding dim (expected %d)",
            skipped,
            store._embedding_dim,
        )
    if version != store._sqlite_data_version():
        store._faiss_index = None
        store._faiss_id_map = []
    store._faiss_data_version = version
    logger.info("Built FAISS index with %d vectors", len(store._faiss_id_map))
    return len(store._faiss_id_map)


def save_faiss_index(store: VectorMemoryStore) -> None:
    """Save a SQLite-derived snapshot and stamp both files in the same epoch."""
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    if not vm._HAS_FAISS or store._faiss_index is None:
        return
    try:
        with store._db_lock:
            store.db.execute("BEGIN IMMEDIATE")
            try:
                # An external editor may have changed vectors since this
                # process built its index. Rebuild while SQLite owns the
                # write reservation; a file stamp can never bless old data.
                store.build_faiss_index()
                vm.faiss.write_index(
                    cast("faiss.Index", store._faiss_index), str(store._faiss_path)
                )
                id_map_path = store._faiss_path.with_suffix(".ids.json")
                id_map_path.write_text(json.dumps(store._faiss_id_map), encoding="utf-8")
                stamp = json.dumps(
                    {
                        "database": store._faiss_content_signature(),
                        "index": hashlib.sha256(store._faiss_path.read_bytes()).hexdigest(),
                        "ids": hashlib.sha256(id_map_path.read_bytes()).hexdigest(),
                    },
                    sort_keys=True,
                )
                store.db.execute(
                    "INSERT INTO memory_meta (key,value,updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    ("faiss_content_signature", stamp, vm._now_iso()),
                )
                store._faiss_data_version = store._sqlite_data_version()
                store.db.commit()
                store._faiss_writes_since_save = 0
            except Exception:
                store.db.rollback()
                raise
    except Exception:
        logger.warning("Failed to save FAISS index", exc_info=True)


def load_faiss_index(store: VectorMemoryStore) -> bool:
    """Load FAISS index from disk. Returns True if loaded, False if rebuilt."""
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    if not vm._HAS_FAISS:
        return False
    id_map_path = store._faiss_path.with_suffix(".ids.json")
    if store._faiss_path.exists() and id_map_path.exists():
        try:
            version = store._sqlite_data_version()
            stamp = json.loads(store._read_meta("faiss_content_signature") or "{}")
            if stamp != {
                "database": store._faiss_content_signature(),
                "index": hashlib.sha256(store._faiss_path.read_bytes()).hexdigest(),
                "ids": hashlib.sha256(id_map_path.read_bytes()).hexdigest(),
            }:
                store.build_faiss_index()
                return False
            loaded_index = vm.faiss.read_index(str(store._faiss_path))
            store._faiss_index = loaded_index
            store._faiss_id_map = json.loads(id_map_path.read_text(encoding="utf-8"))
            # Consistency gate: the persisted index and id-map can drift out of
            # sync if a prior process was interrupted mid-write, or the two files
            # were flushed at different points. Serving a desynced pair silently
            # returns wrong/missing lookups and can IndexError on id resolution,
            # so reconcile by rebuilding from SQLite (the source of truth). Read
            # ntotal off the freshly-loaded local (typed by read_index) rather
            # than the object|None attribute to keep the access type-clean.
            ntotal = loaded_index.ntotal
            if ntotal != len(store._faiss_id_map):
                logger.warning(
                    "FAISS index/id-map desync (index.ntotal=%d, id_map=%d); rebuilding",
                    ntotal,
                    len(store._faiss_id_map),
                )
                store.build_faiss_index()
                return False
            if version != store._sqlite_data_version():
                store.build_faiss_index()
                return False
            store._faiss_data_version = version
            logger.info("Loaded FAISS index: %d vectors", len(store._faiss_id_map))
            return True
        except Exception:
            logger.warning("FAISS index corrupted, rebuilding", exc_info=True)
    store.build_faiss_index()
    return False
