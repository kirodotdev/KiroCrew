"""``_build_store_vectors`` admits a named store before it opens it.

``VectorMemoryStore.init()`` creates whatever is missing: the owner-only parent
directories and then a fresh, empty SQLite file. ``require_memory_store`` exists
to refuse a declared store whose directory is gone, so a lost store reads as a
loss that needs a restore rather than as a store that holds nothing. Running the
open first defeats the refusal: the gateway's own context path recreates the
empty database, and the admission that follows then finds a directory to admit.

These tests delete a declared store's directory and pin that the build raises
``UnknownMemoryStore``, that nothing is recreated on disk, and that an intact
store still builds so the reorder does not refuse healthy stores. A present
directory with no ``memory.db`` yet is a freshly provisioned store, not a loss,
and stays admitted.
"""

from __future__ import annotations

import shutil

import pytest
from member_memory_helpers import forget_declared_stores, write_member_home

from kiro_crew import context
from kiro_crew.memory_stores import UnknownMemoryStore


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    write_member_home(tmp_path, "alice")
    forget_declared_stores(monkeypatch)
    monkeypatch.setattr(context, "_vector_stores", {})
    yield tmp_path
    forget_declared_stores(monkeypatch)


@pytest.mark.asyncio
async def test_missing_store_directory_is_refused_and_not_recreated(home):
    store_dir = home / "memory_stores" / "member-alice"
    assert store_dir.is_dir()
    shutil.rmtree(store_dir)

    with pytest.raises(UnknownMemoryStore):
        await context._build_store_vectors("member-alice")

    assert not store_dir.exists(), "the build recreated the deleted store directory"
    assert not (
        store_dir / "memory.db"
    ).exists(), "the build wrote an empty database into a lost store"
    assert "member-alice" not in context._vector_stores


@pytest.mark.asyncio
async def test_intact_declared_store_still_builds(home):
    """The reorder must not refuse a healthy store."""
    from kiro_crew.vector_memory import VectorMemoryStore

    db = home / "memory_stores" / "member-alice" / "memory.db"
    seed = VectorMemoryStore(db_path=db)
    seed.init()
    seed.close()

    store = await context._build_store_vectors("member-alice")
    try:
        assert store is not None
        assert context._vector_stores.get("member-alice") is store
    finally:
        for cached in context._vector_stores.values():
            cached.close()


@pytest.mark.asyncio
async def test_store_removed_during_init_is_refused_at_publication(home, monkeypatch):
    """The publication-edge admission: a store that vanishes after ``init()``
    but before the cache hands it out is refused and not cached."""
    from kiro_crew import embeddings

    store_dir = home / "memory_stores" / "member-alice"

    def vanish(store):
        shutil.rmtree(store_dir)

    monkeypatch.setattr(embeddings, "reconcile_store_embedding_space", vanish)

    with pytest.raises(UnknownMemoryStore):
        await context._build_store_vectors("member-alice")

    assert "member-alice" not in context._vector_stores
