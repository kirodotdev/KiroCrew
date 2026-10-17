"""Derived failures preserve primary text; provenance must describe the producer."""

import asyncio
from types import SimpleNamespace

import pytest
from test_embedding_rebuild_generation import rebuild_home as _rebuild_home

from kiro_crew import embeddings as emb
from kiro_crew.dashboard.handlers import memory
from kiro_crew.vector_memory import VectorMemoryStore

rebuild_home = _rebuild_home


class FaultConnection:
    def __init__(self, connection, failure):
        self.connection, self.failure = connection, failure
        self.armed = False
        self.rollbacks = 0
        self.commits = 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        if self.armed and self.failure == "begin" and sql == "BEGIN IMMEDIATE":
            raise OSError("admission disk failure")
        if self.armed and self.failure == "write" and "SET embedding = ?" in sql:
            raise OSError("vector disk failure")
        return self.connection.execute(sql, *args)

    def commit(self):
        self.commits += 1
        if self.armed and self.failure in ("commit", "rollback"):
            raise OSError("commit disk failure")
        return self.connection.commit()

    def rollback(self):
        self.rollbacks += 1
        if self.failure == "rollback":
            raise OSError("rollback disk failure")
        return self.connection.rollback()


@pytest.mark.parametrize("failure", ["begin", "write", "commit", "rollback"])
def test_fact_derived_failure_preserves_committed_body(tmp_path, monkeypatch, failure):
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    store.init()
    connection = store.db
    faults = FaultConnection(connection, failure)
    monkeypatch.setattr(store, "_db", faults)

    def embed(text):
        faults.armed = True
        return [1.0, 0.0]

    store.embed_fn = embed
    try:
        assert store.set_semantic("project.saved", "saved body", 1.0, "user_explicit") is None
        assert faults.rollbacks == (0 if failure == "begin" else 1)
    finally:
        store.close()
    reopened = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    reopened.init()
    try:
        row = reopened.get_semantic("project.saved")
        assert row["value_json"] == '"saved body"'
        assert row["embedding"] is None
    finally:
        reopened.close()


def test_old_producer_cannot_adopt_new_database_token(rebuild_home, monkeypatch):
    home = rebuild_home
    store = home.global_store
    backend = emb.get_shared_embedder()
    backend._llm = SimpleNamespace(
        create_embedding=lambda texts: {"data": [{"embedding": [1.0, 0.0]} for _ in texts]},
        close=lambda: None,
    )
    old_token = store._embedding_token
    first = True

    def interleave():
        nonlocal first
        if first:
            first = False
            import os
            import subprocess
            import sys
            from pathlib import Path

            script = (
                "import sys; from kiro_crew.vector_memory import VectorMemoryStore; "
                "s=VectorMemoryStore(db_path=sys.argv[1], embedding_dim=2); s.init(); "
                "s.reconcile_embedding_space(sys.argv[2], force=True, rebuild_generation='new-request'); "
                "s.close()"
            )
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(store._db_path),
                    emb.embedding_space_signature("other-model", 2),
                ],
                cwd=home.root,
                env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")),
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
        return old_token()

    monkeypatch.setattr(store, "_embedding_token", interleave)
    assert store._try_embed("must not bless old producer") is None


def test_config_request_blocks_publish_before_store_invalidation(rebuild_home):
    home = rebuild_home
    backend = emb.get_shared_embedder()
    backend._llm = SimpleNamespace(
        create_embedding=lambda texts: {"data": [{"embedding": [1.0, 0.0]} for _ in texts]},
        close=lambda: None,
    )
    vector = home.global_store._try_embed("computed before new request")
    assert vector is not None
    asyncio.run(memory._write_embed_model_config(str(home.model), 2))
    assert not home.global_store._embedding_current(vector)


@pytest.mark.parametrize("kind", ["lesson", "episode-backfill", "fact-backfill", "lesson-backfill"])
@pytest.mark.parametrize("failure", ["begin", "write", "commit", "rollback"])
def test_other_vector_callers_keep_primary_rows(tmp_path, monkeypatch, kind, failure):
    from contextlib import contextmanager

    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    store.init()
    if kind == "episode-backfill":
        store.write_episodic("durable episode awaiting vector", defer_embedding=True)
    elif kind == "fact-backfill":
        store.set_semantic("project.saved", "saved body", 1.0, "user_explicit")
    elif kind == "lesson-backfill":
        store.write_lesson("Keep database backups available")
    faults = FaultConnection(store.db, failure)
    monkeypatch.setattr(store, "_db", faults)
    store.embed_fn = lambda text: [1.0, 0.0]
    original = store._vector_commit

    @contextmanager
    def arm(vector, **kwargs):
        faults.armed = True
        with original(vector, **kwargs) as current:
            yield current

    monkeypatch.setattr(store, "_vector_commit", arm)
    try:
        if kind == "lesson":
            assert store.write_lesson("Keep database backups available")
        else:
            with pytest.raises(OSError):
                store.backfill_missing_embeddings(pace=False)
    finally:
        store.close()
    reopened = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    reopened.init()
    try:
        relation = "episodic_memories" if kind == "episode-backfill" else "semantic_memory"
        row = reopened.db.execute(f"SELECT embedding FROM {relation}").fetchone()
        assert row is not None and row[0] is None
    finally:
        reopened.close()


def test_failed_vector_admission_does_not_rollback_an_outer_transaction(tmp_path):
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    store.init()
    try:
        store.db.execute("BEGIN IMMEDIATE")
        store.db.execute("INSERT INTO memory_meta VALUES ('outer', 'keep', 'now')")
        with store._vector_commit([1.0, 0.0], best_effort=True) as current:
            assert not current
        assert store.db.in_transaction
        assert store._read_meta("outer") == "keep"
        store.db.rollback()
    finally:
        store.close()


def test_primary_fact_failure_is_not_swallowed(tmp_path, monkeypatch):
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    store.init()
    faults = FaultConnection(store.db, "commit")
    faults.armed = True
    monkeypatch.setattr(store, "_db", faults)
    try:
        with pytest.raises(OSError, match="commit disk failure"):
            store.set_semantic("project.saved", "not committed", 1.0, "user_explicit")
        assert store.get_semantic("project.saved") is None
    finally:
        store.close()


def test_managed_vector_uses_the_config_writers_symlink_target(rebuild_home):
    from kiro_crew.config.loader import _config_write_lock
    from kiro_crew.vector_memory import _EmbeddingVector

    home = rebuild_home
    target = home.config.with_name("actual-config.json")
    home.config.rename(target)
    try:
        home.config.symlink_to(target)
    except OSError:
        target.rename(home.config)
        pytest.skip("file symlinks are unavailable")
    vector = _EmbeddingVector([1.0, 0.0], home.global_store._embedding_token(), managed=True)
    with home.global_store._embedding_config_guard(vector):
        with pytest.raises(OSError):
            with _config_write_lock(target, wait=False):
                pytest.fail("config writer acquired a different sidecar")


@pytest.mark.parametrize("failure", ["begin", "commit"])
@pytest.mark.parametrize("preserve", [False, True])
@pytest.mark.parametrize("private", [False, True])
def test_primary_episode_transaction_failure_is_reported(
    rebuild_home, monkeypatch, failure, preserve, private
):
    store = rebuild_home.late[1] if private else rebuild_home.global_store
    before = store.get_episodic_list()

    class TransactionFault(FaultConnection):
        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

    faults = TransactionFault(store.db, failure)
    faults.armed = True
    monkeypatch.setattr(store, "_db", faults)
    with pytest.raises(OSError, match="disk failure"):
        store.write_episodic(
            "primary episode that must not report success",
            defer_embedding=True,
            preserve_existing=preserve,
        )
    assert store.get_episodic_list() == before
    assert not store.db.in_transaction


def test_native_inference_holds_neither_store_nor_config_lock(rebuild_home):
    from kiro_crew.config.loader import _config_write_lock, _lock_target

    home = rebuild_home
    store = home.global_store
    backend = emb.get_shared_embedder()
    observed = []

    def infer(texts):
        assert store._db_lock.acquire(blocking=False)
        try:
            with _config_write_lock(_lock_target(home.config), wait=False):
                observed.append(True)
        finally:
            store._db_lock.release()
        return {"data": [{"embedding": [1.0, 0.0]} for _ in texts]}

    backend._llm = SimpleNamespace(create_embedding=infer, close=lambda: None)
    assert store._try_embed("native inference lock probe") == [1.0, 0.0]
    assert observed == [True]


@pytest.mark.parametrize("private", [False, True])
def test_managed_rollback_failure_closes_without_reentering_config_lock(
    rebuild_home, monkeypatch, private
):
    from kiro_crew.config.loader import _config_write_lock, _lock_target
    from kiro_crew.vector_memory import _EmbeddingVector

    home = rebuild_home
    store = home.late[1] if private else home.global_store
    store.reconcile_embedding_space(store.recorded_embedding_space(), force=True)
    vector = _EmbeddingVector([1.0, 0.0], store._embedding_token(), managed=True)
    faults = FaultConnection(store.db, "rollback")
    faults.armed = True
    monkeypatch.setattr(store, "_db", faults)
    with store._vector_commit(vector, best_effort=True) as current:
        assert current
        store.db.execute(
            f"UPDATE {store._sem_rel} SET embedding = ? WHERE key = ?{store._sem_guard}",
            (b"bad vector", "project.status"),
        )
    assert store._db is None
    with _config_write_lock(_lock_target(home.config), wait=False):
        pass
    reopened = home.open_store(store._db_path)
    row = reopened.get_semantic("project.status")
    assert row["value_json"] == '"active"'
    assert row["embedding"] is None


@pytest.mark.parametrize("kind", ["derived", "semantic", "lesson", "episode"])
@pytest.mark.parametrize("private", [False, True])
def test_config_wait_does_not_hold_database_lock(rebuild_home, monkeypatch, kind, private):
    """A real config flock contender must not block another database reader."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from kiro_crew.config import loader
    from kiro_crew.vector_memory import _EmbeddingVector

    home = rebuild_home
    store = home.late[1] if private else home.global_store
    vector = _EmbeddingVector([1.0, 0.0], store._embedding_token(), managed=True)
    entered = threading.Event()
    real_lock = loader._config_write_lock

    @contextmanager
    def observed_lock(path, **kwargs):
        entered.set()
        with real_lock(path, **kwargs):
            yield

    monkeypatch.setattr(loader, "_config_write_lock", observed_lock)
    monkeypatch.setattr(store, "_try_embed", lambda *args: vector)
    store.embed_fn = lambda text: vector

    def publish():
        if kind == "derived":
            with store._vector_commit(vector) as current:
                assert current
        elif kind == "semantic":
            assert store.set_semantic("project.lock", "retained body", 1, "user_explicit") is None
        elif kind == "lesson":
            assert store.write_lesson("Prefer cursor pagination for large result sets")
        else:
            assert store.write_episodic(
                "An independent import completed with its original contents", embedding=vector
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with real_lock(loader._lock_target(home.config)):
            future = pool.submit(publish)
            assert entered.wait(2), "writer never attempted config admission"
            assert not future.done(), "real config lock did not serialize the writer"
            acquired = store._db_lock.acquire(blocking=False)
            try:
                assert acquired, "config waiter owns the database lock"
                assert store.db.execute("SELECT 1").fetchone()[0] == 1
            finally:
                if acquired:
                    store._db_lock.release()
        future.result(timeout=5)
