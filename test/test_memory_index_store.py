"""A store's memory index: the change log, the local backend on disk, and V2 recall
reading through the index."""

from __future__ import annotations

import json
import sqlite3

import pytest
from test_memory_edit import store  # noqa: F401  (the v1 / v2 store fixture)

from conftest import requires_symlinks
from kiro_crew.config import KiroCrewConfig, loader
from kiro_crew.memory_index import changelog, store_index
from kiro_crew.memory_index.conformance import run_conformance
from kiro_crew.memory_index.contract import EmbeddingSpace, IndexDoc, IndexQuery, StoreRef
from kiro_crew.memory_index.exact import ExactIndex
from kiro_crew.memory_index.local import LocalIndex
from kiro_crew.memory_index.store_index import IndexSettings, index_for, settings_from
from kiro_crew.vector_memory_runtime.episodic_search import search_episodic_v2

pytest.importorskip("usearch")


def _embed(text: str) -> list[float]:
    """Three topics on three axes, so every cosine is known."""
    lowered = text.lower()
    return [
        1.0 if "worktree" in lowered or "checkout" in lowered else 0.0,
        1.0 if "chinese" in lowered or "language" in lowered else 0.0,
        1.0 if "shard" in lowered else 0.05,
    ]


def _with_index(store, mode: str):  # noqa: F811
    store._embedding_dim = 3
    store.embed_fn = _embed
    store._index_settings = IndexSettings(mode=mode)
    return index_for(store)


def _built(store, mode: str = "on", in_child: bool = False):  # noqa: F811
    """The store's index, built in this thread, so a test waits on nothing."""
    index = _with_index(store, mode)
    assert index._ensure_log()
    index.in_child = in_child
    index.child_timeout = 60.0
    index._building = True
    index._build()
    assert index._ready, index._failed
    return index


def _seed(store):  # noqa: F811
    _with_index(store, "off")  # the embedder is set before any write, so rows carry vectors
    assert store.set_semantic("user.language", "Chinese for chat", 1.0, "user_explicit") is None
    assert store.set_semantic("project.flaky", "shard 8 is flaky", 0.8, "taskrunner") is None
    assert (
        store.set_semantic("project.checkout", "keep the live checkout clean", 0.8, "taskrunner")
        is None
    )
    assert store.set_semantic("pref.reply", "short replies", 1.0, "user_explicit") is None
    assert store.write_episodic(
        "Moved the worktree before the rebase.", conversation_id="c1", tags=["ops"]
    )
    assert store.write_episodic("Shard 8 failed again on main.", conversation_id="c1", tags=["ci"])


def _keys(rows) -> list[str]:
    return [row["key"] for row in rows]


def test_the_change_log_names_each_written_and_deleted_memory_but_not_reads(store):  # noqa: F811
    index = _with_index(store, "on")
    assert index._ensure_log()
    start = changelog.current_seq(store.db)
    assert store.set_semantic("user.city", "Berlin", 1.0, "user_explicit") is None
    assert store.write_episodic("Moved the worktree before the rebase.", conversation_id="c1")
    episode = store.db.execute(
        "SELECT id FROM episodic_memories ORDER BY created_at DESC LIMIT 1"
    ).fetchone()[0]
    store.delete_semantic("user.city", "user_explicit")
    logged = {(rel, ident) for _seq, rel, ident in changelog.changes_after(store.db, start, 100)}
    assert ("semantic", "user.city") in logged
    assert ("episodic", episode) in logged
    before = changelog.current_seq(store.db)
    store._touch_last_accessed([episode])
    assert changelog.current_seq(store.db) == before
    # The log names memories; it never copies what they say.
    assert not any("Berlin" in ident for _s, _r, ident in changelog.changes_after(store.db, 0, 100))


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_fact_recall_on_the_index_matches_the_scan_and_follows_a_delete(store):  # noqa: F811
    _seed(store)
    scanned = _keys(store._semantic_candidates_v2("keep the live checkout clean"))
    _built(store)
    indexed = _keys(store._semantic_candidates_v2("keep the live checkout clean"))
    assert indexed == scanned
    assert "pref.reply" in indexed  # a preference is always admitted
    store.delete_semantic("project.checkout", "user_explicit")
    after = _keys(store._semantic_candidates_v2("keep the live checkout clean"))
    assert "project.checkout" not in after
    assert "project.checkout" in scanned


@pytest.mark.timeout(120)
@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_a_build_in_a_child_process_is_adopted_and_answers_like_the_scan(store):  # noqa: F811
    _seed(store)
    scanned = _keys(store._semantic_candidates_v2("keep the live checkout clean"))
    index = _built(store, in_child=True)
    status = index._backend.status()
    assert status.state == "ready" and status.rows == 6
    assert _keys(store._semantic_candidates_v2("keep the live checkout clean")) == scanned


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_episode_recall_on_the_index_matches_the_scan(store):  # noqa: F811
    _seed(store)
    query = _embed("shard failures")
    scanned = [
        row["id"]
        for row in search_episodic_v2(store, query, "shard failures", 5, False, None, True)
    ]
    _built(store)
    indexed = [
        row["id"]
        for row in search_episodic_v2(store, query, "shard failures", 5, False, None, True)
    ]
    assert indexed == scanned and scanned


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_shadow_answers_from_the_scan_and_counts_what_the_index_missed(store):  # noqa: F811
    _seed(store)
    scanned = _keys(store._semantic_candidates_v2("shard flaky"))
    index = _built(store, "shadow")
    assert _keys(store._semantic_candidates_v2("shard flaky")) == scanned
    checked, missed = index.shadow["semantic"]
    assert checked >= 1 and missed == 0


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_recall_scans_until_the_index_is_built(store):  # noqa: F811
    _seed(store)
    index = _with_index(store, "on")
    index._building = True  # a build in progress
    assert index.candidates("semantic", "shard", None, min_match=1, floor=0.57) is None
    assert "project.flaky" in _keys(store._semantic_candidates_v2("shard flaky"))


def test_a_store_opened_with_the_index_off_drops_the_change_log(tmp_path, monkeypatch):
    from kiro_crew.vector_memory import VectorMemoryStore

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"memory": {"index": {"mode": "off"}}}), encoding="utf-8"
    )
    loader._invalidate_config_cache()
    first = VectorMemoryStore(db_path=tmp_path / "memory.db")
    first.init()
    first.db.execute("BEGIN IMMEDIATE")
    changelog.install(first.db, first._lineage)
    first.db.execute("COMMIT")
    first.close()
    second = VectorMemoryStore(db_path=tmp_path / "memory.db", config=KiroCrewConfig.load())
    second.init()
    try:
        assert not changelog.is_present(second.db)
    finally:
        second.close()
        loader._invalidate_config_cache()


def test_settings_read_memory_index_and_fall_back_to_off():
    class Section:
        mode, backend, exact_rows = "on", "local", 5

    class Memory:
        index = Section()

    assert settings_from(Memory()) == IndexSettings("on", "local", 5)
    Section.mode = "sideways"
    assert settings_from(Memory()).mode == "off"
    assert settings_from(object()).mode == "off"


def test_the_local_backend_passes_the_conformance_kit_on_disk(tmp_path):
    counter = iter(range(1000))

    class OnDisk(LocalIndex):
        def open(self, ref, space, config):
            super().open(
                StoreRef(ref.store_id, str(tmp_path / f"idx{next(counter)}")), space, config
            )

    results = run_conformance(OnDisk)
    assert {r.name: r.detail for r in results if r.status == "fail"} == {}
    assert {r.name for r in results if r.status == "skip"} == {"substring_matches_exact"}


def test_a_local_index_reopens_its_saved_build_and_marks_a_lagging_one_stale(tmp_path):
    space = EmbeddingSpace("s", 2)
    ref = StoreRef("s", str(tmp_path / "idx"))
    doc = IndexDoc(
        ("semantic", "a"), "fact", b"\x00\x00\x80\x3f" + b"\x00" * 4, "alpha", "alpha", {}
    )
    first = LocalIndex()
    first.open(ref, space, {})
    first.swap(first.rebuild([doc], build_seq=3))
    first.close()
    second = LocalIndex()
    second.open(ref, space, {})
    assert second.status().state == "ready"
    assert [hit.ident for hit in second.search(IndexQuery("terms", 5, text="alpha"))] == [
        ("semantic", "a")
    ]
    second.upsert([IndexDoc(("semantic", "b"), "fact", None, "beta", "beta", {})], seq=4)
    # A crash before close: the words moved on, the saved vectors did not.
    second._gen.db.execute("UPDATE meta SET value = '3' WHERE key = 'vectors_seq'")
    second._gen.db.commit()
    second._gen.close()
    second._gen = None
    third = LocalIndex()
    third.open(ref, space, {})
    try:
        assert third.status().state == "stale"
    finally:
        # A shutdown before the rebuild swaps must not mark the old vectors current.
        third.close()
    fourth = LocalIndex()
    fourth.open(ref, space, {})
    try:
        assert fourth.status().state == "stale"
    finally:
        fourth.close()


def test_an_exact_index_opened_empty_is_not_ready_until_a_build():
    index = ExactIndex()
    index.open(StoreRef("s"), EmbeddingSpace("s", 2), {})
    assert index.status().state == "missing"
    index.swap(index.rebuild([], build_seq=0))
    assert index.status().state == "ready"


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_the_index_shares_the_store_lock_so_recall_cannot_deadlock_it(store):  # noqa: F811
    assert _with_index(store, "on")._lock is store._db_lock


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_a_recreated_change_log_makes_the_index_rebuild(store):  # noqa: F811
    _seed(store)
    index = _built(store)
    store.db.execute("BEGIN IMMEDIATE")
    changelog.strip(store.db)  # the index was turned off for a while
    store.db.execute("COMMIT")
    assert (
        store.set_semantic("project.release", "keep the live checkout for release", 0.8, "t")
        is None
    )
    scheduled = []
    index._schedule_build = lambda: scheduled.append(True)  # build in this thread below
    assert index._usable() is None
    assert scheduled and index._log_reinstalled
    index._building = True
    index._build()
    assert "project.release" in _keys(store._semantic_candidates_v2("keep the live checkout clean"))


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_a_full_term_window_hands_the_search_back_to_the_scan(store, monkeypatch):  # noqa: F811
    _seed(store)
    for n in range(3):
        assert store.write_episodic(
            f"Shard {n} failed on main again.", conversation_id="c2", tags=["ci"]
        )
    index = _built(store)
    monkeypatch.setattr(store_index, "TERMS_K", 3)
    # Episodes cannot crowd facts out of the window: each search reads its own kind.
    assert index.candidates("semantic", "shard flaky", None, min_match=1, floor=0.57) == {
        "project.flaky"
    }
    assert index.candidates("episodic", "shard main", None, min_match=1, floor=0.57) is None
    assert index.fallbacks == {"episodic": 1}


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_closing_the_store_closes_its_index_and_frees_the_owner_lock(store):  # noqa: F811
    _seed(store)
    first = _built(store)
    assert first._owner_fd is not None
    path = store._db_path
    store.close()
    assert first._owner_fd is None and store._memory_index is None
    from kiro_crew.vector_memory import open_member_database

    again = open_member_database(path, member_id="alice", store_id=path.parent.name)
    try:
        second = _with_index(again, "on")
        assert second._own_index_dir()
    finally:
        again.close()


def test_a_named_store_index_directory_never_rides_in_a_bundle():
    from kiro_crew import memory_stores

    assert memory_stores.is_host_local_store_state(
        ("memory_stores", "member-alice", "memory.db.index", "gen-1", "words.db")
    )
    assert not memory_stores.is_host_local_store_state(
        ("memory_stores", "member-alice", "memory.db")
    )


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_a_log_another_process_recreated_makes_the_index_rebuild(store):  # noqa: F811
    _seed(store)
    index = _built(store)
    store.db.execute("BEGIN IMMEDIATE")
    changelog.strip(store.db)  # another process ran with the index off ...
    changelog.install(store.db, store._lineage)  # ... and another turned it on again
    store.db.execute("COMMIT")
    for n in range(12):  # the new counter passes the old build's seq
        assert (
            store.set_semantic(f"project.note{n}", f"keep the live checkout {n}", 0.8, "t") is None
        )
    scheduled = []
    index._schedule_build = lambda: scheduled.append(True)
    assert index._usable() is None and scheduled


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_a_store_closed_while_its_build_waits_keeps_no_index(store):  # noqa: F811
    _seed(store)
    index = _with_index(store, "on")
    assert index._ensure_log()
    index._building = True
    index.close()
    index._build()
    assert index._backend is None and index._owner_fd is None


def test_a_local_index_drops_generations_its_current_does_not_name(tmp_path):
    space = EmbeddingSpace("s", 2)
    ref = StoreRef("s", str(tmp_path / "idx"))
    first = LocalIndex()
    first.open(ref, space, {})
    first.swap(first.rebuild([], build_seq=1))
    stray = first.rebuild([], build_seq=2)  # a build that was never swapped in
    first.close()
    stray.gen.close()
    second = LocalIndex()
    second.open(ref, space, {})
    try:
        generations = [p.name for p in (tmp_path / "idx").iterdir() if p.name.startswith("gen-")]
        assert generations == [(tmp_path / "idx" / "current").read_text(encoding="utf-8").strip()]
    finally:
        second.close()


def test_terms_keep_a_tai_lue_word_that_unicode61_splits():
    from kiro_crew import memory_v2

    word = "ᦞᦰᦙ"  # Tai Lue vowel signs became letters after unicode61's Unicode version.
    terms = " ".join(sorted(memory_v2.terms(word)))
    assert terms == word
    index = LocalIndex()
    index.open(StoreRef("s"), EmbeddingSpace("s", 2), {})
    try:
        index.upsert([IndexDoc(("semantic", "word"), "fact", None, word, terms, {})], seq=1)
        assert [hit.ident for hit in index.search(IndexQuery("terms", 5, text=terms))] == [
            ("semantic", "word")
        ]
    finally:
        index.close()


def test_an_old_format_is_stale_until_rebuilt(tmp_path):
    from kiro_crew.memory_index.local import FORMAT_VERSION

    ref, space = StoreRef("s", str(tmp_path / "idx")), EmbeddingSpace("s", 2)
    first = LocalIndex()
    first.open(ref, space, {})
    try:
        first.swap(first.rebuild([], build_seq=1))
        first._gen.set_meta(format_version=FORMAT_VERSION - 1)
        first._gen.db.commit()
    finally:
        first.close()
    second = LocalIndex()
    second.open(ref, space, {})
    try:
        assert second.status().state == "stale"
        assert "format" in second.status().detail
        second.swap(second.rebuild([], build_seq=1))
        assert second.status().state == "ready"
        assert second._gen.meta("format_version") == str(FORMAT_VERSION)
        schema = second._gen.db.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'words_terms'"
        ).fetchone()[0]
        assert "tokenize='ascii'" in schema
    finally:
        second.close()


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_backend_open_failure_warns_once_and_recall_keeps_scanning(
    store, monkeypatch, caplog  # noqa: F811
):
    import logging
    import sys

    from kiro_crew.extras import install_hint

    _seed(store)
    scanned = _keys(store._semantic_candidates_v2("shard flaky"))
    index = _with_index(store, "on")
    monkeypatch.setitem(sys.modules, "usearch.index", None)
    with caplog.at_level(logging.WARNING, logger=store_index.__name__):
        for _ in range(3):
            assert _keys(store._semantic_candidates_v2("shard flaky")) == scanned
    assert index._backend is None and index._open_failed
    assert "MemoryIndexError" in index._failed
    assert install_hint("memory-index") in index._failed
    warnings = [r for r in caplog.records if "did not open" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_index_construction_holds_the_store_lock(store, monkeypatch):  # noqa: F811
    original_lock = store._db_lock
    original_index = store_index.StoreIndex

    class ObservedLock:
        depth = 0

        def __enter__(self):
            original_lock.__enter__()
            self.depth += 1
            return self

        def __exit__(self, *args):
            self.depth -= 1
            return original_lock.__exit__(*args)

    lock = ObservedLock()
    monkeypatch.setattr(store, "_db_lock", lock)
    constructed = []

    def construct(owner, settings):
        assert lock.depth > 0, "index construction must hold the store lock"
        index = original_index(owner, settings)
        constructed.append(index)
        return index

    monkeypatch.setattr(store_index, "StoreIndex", construct)
    first = _with_index(store, "on")
    assert index_for(store) is first
    assert constructed == [first]
    store._index_settings = IndexSettings("on", exact_rows=5)
    second = index_for(store)
    assert constructed == [first, second] and first._closed
    store._index_settings = IndexSettings("off")
    assert index_for(store) is None and second._closed


@pytest.mark.parametrize("store", ["v2"], indirect=True)
@pytest.mark.parametrize(
    "settings", [IndexSettings("on", exact_rows=5), IndexSettings("on", "exact")]
)
def test_live_backend_settings_replace_the_index(store, settings):  # noqa: F811
    _seed(store)
    first = _built(store)
    generation = first._backend._gen.path
    store._index_settings = settings
    second = index_for(store)
    assert second is not first and first._closed and first._owner_fd is None
    backend = second._open_backend()
    assert backend is not None and backend.name == settings.backend
    if settings.backend == "local":
        assert backend._exact_rows == 5
        assert backend._gen.path == generation and second._ready
    store._index_settings = IndexSettings("shadow", settings.backend, settings.exact_rows)
    assert index_for(store) is second


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_the_child_build_receives_the_store_timeout(store, monkeypatch):  # noqa: F811
    from kiro_crew.memory_index import build_child

    _seed(store)
    index = _with_index(store, "on")
    assert index.child_timeout == build_child.CHILD_TIMEOUT_SECS
    index.child_timeout = 60.0
    assert index._ensure_log()
    backend = index._open_backend()
    received = []

    def child(*args, timeout):
        received.append(timeout)
        return None  # the fallback builds synchronously, without starting a child

    monkeypatch.setattr(build_child, "run_build_in_child", child)
    handle = index._rebuilt(backend)
    try:
        assert received == [60.0]
    finally:
        backend.discard(handle)


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_a_failed_catch_up_scans_and_keeps_the_changes_pending(store, monkeypatch):  # noqa: F811
    _seed(store)
    index = _built(store)
    backend = index._open_backend()
    applied = backend.status().applied_seq
    store.delete_semantic("project.flaky", "taskrunner")
    assert store.set_semantic("user.city", "Berlin", 1.0, "user_explicit") is None

    def full_disk(*_args, **_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(backend, "upsert", full_disk)
    scheduled = []
    monkeypatch.setattr(index, "_schedule_build", lambda: scheduled.append(True))
    assert index.candidates("semantic", "Berlin", None, min_match=1, floor=0.5) is None
    # The removal landed but did not move the seq, so nothing is skipped later.
    assert backend.status().applied_seq == applied
    assert not index._ready
    # The next search hands the index to a rebuild instead of trusting it.
    assert index.candidates("semantic", "Berlin", None, min_match=1, floor=0.5) is None
    assert scheduled == [True]


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_a_member_store_reopened_with_the_index_off_drops_the_change_log(store):  # noqa: F811
    index = _with_index(store, "on")
    assert index._ensure_log() and changelog.is_present(store.db)
    store.close()
    store._index_settings = IndexSettings("off")
    store.init()
    assert not changelog.is_present(store.db)


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_catch_up_trims_the_rows_it_applied(store, monkeypatch):  # noqa: F811
    _seed(store)
    index = _built(store)
    monkeypatch.setattr(store_index, "TRIM_EVERY", 1)
    assert store.set_semantic("user.city", "Berlin", 1.0, "user_explicit") is None
    assert changelog.current_seq(store.db) > index._open_backend().status().built_seq
    assert index.candidates("semantic", "Berlin", None, min_match=1, floor=0.5) is not None
    rows = store.db.execute(f"SELECT count(*) FROM {changelog.LOG_TABLE}").fetchone()[0]
    assert rows == 0


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_a_failing_index_close_never_keeps_the_store_open(store, monkeypatch):  # noqa: F811
    _seed(store)
    index = _built(store)

    def full_disk():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(index._backend._gen, "save_vectors", full_disk)
    index.close()
    assert index._owner_fd is None

    store._memory_index = index
    monkeypatch.setattr(index, "close", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    store.close()
    assert store._db is None


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_an_index_that_cannot_open_keeps_no_change_log(store, monkeypatch):  # noqa: F811
    import sys

    _seed(store)
    monkeypatch.setitem(sys.modules, "usearch.index", None)
    index = _with_index(store, "on")
    assert index.candidates("semantic", "shard", None, min_match=1, floor=0.5) is None
    assert index._open_failed and not changelog.is_present(store.db)
    assert store.set_semantic("user.city", "Berlin", 1.0, "user_explicit") is None
    assert index.candidates("semantic", "Berlin", None, min_match=1, floor=0.5) is None
    assert not changelog.is_present(store.db)


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_switching_the_index_off_while_running_drops_the_change_log(store):  # noqa: F811
    _seed(store)
    _built(store)
    assert changelog.is_present(store.db)
    store._index_settings = IndexSettings("off")
    assert index_for(store) is None
    assert not changelog.is_present(store.db)


def test_a_build_whose_vector_file_is_gone_reads_stale_until_rebuilt(tmp_path):
    space = EmbeddingSpace("s", 2)
    ref = StoreRef("s", str(tmp_path / "idx"))
    doc = IndexDoc(
        ("semantic", "a"), "fact", b"\x00\x00\x80\x3f" + b"\x00" * 4, "alpha", "alpha", {}
    )
    first = LocalIndex()
    first.open(ref, space, {})
    first.swap(first.rebuild([doc], build_seq=3))
    first.close()
    (vectors,) = (tmp_path / "idx").glob("gen-*/vectors.usearch")
    vectors.unlink()
    for _ in range(2):  # closing must not save the empty graph as current
        reopened = LocalIndex()
        reopened.open(ref, space, {})
        try:
            assert reopened.status().state == "stale"
        finally:
            reopened.close()


@pytest.mark.parametrize("pointer", ["../victim", "VICTIM_ABS", "gen-x/../../victim", "gen-"])
def test_a_pointer_that_names_no_generation_never_reaches_another_directory(tmp_path, pointer):
    space = EmbeddingSpace("s", 2)
    root = tmp_path / "idx"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.json").write_text("{}", encoding="utf-8")
    ref = StoreRef("s", str(root))
    doc = IndexDoc(
        ("semantic", "a"), "fact", b"\x00\x00\x80\x3f" + b"\x00" * 4, "alpha", "alpha", {}
    )
    first = LocalIndex()
    first.open(ref, space, {})
    first.close()
    (root / "current").write_text(
        str(victim) if pointer == "VICTIM_ABS" else pointer, encoding="utf-8"
    )
    index = LocalIndex()
    index.open(ref, space, {})
    try:
        index.swap(index.rebuild([doc], build_seq=1))  # swap deletes the old generation
    finally:
        index.close()
    assert sorted(p.name for p in victim.iterdir()) == ["keep.json"]


@requires_symlinks
def test_only_a_generation_directly_under_the_root_is_ever_deleted(tmp_path):
    from kiro_crew.memory_index.local import _remove_generation

    root = tmp_path / "idx"
    root.mkdir()
    outside = tmp_path / "gen-0123456789abcdef0123456789abcdef"
    outside.mkdir()
    link = root / "gen-fedcba9876543210fedcba9876543210"
    link.symlink_to(outside, target_is_directory=True)
    _remove_generation(root, outside)  # not under the root
    _remove_generation(root, link)  # a link, not a generation
    _remove_generation(root, root / "profiles")  # not a generation name
    assert outside.is_dir() and link.is_symlink()


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_a_process_that_does_not_own_the_index_never_installs_the_log(
    store, monkeypatch  # noqa: F811
):
    _seed(store)
    index = _with_index(store, "on")
    monkeypatch.setattr(index, "_own_index_dir", lambda: False)  # another process owns it
    assert index.candidates("semantic", "shard", None, min_match=1, floor=0.5) is None
    assert not changelog.is_present(store.db)


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_the_change_log_stays_bounded_when_nothing_ever_trims_it(store):  # noqa: F811
    _seed(store)
    index = _built(store)
    applied = index._open_backend().status().applied_seq
    index.close()  # no owner, no catch-up, no trim from here on
    row_id = store.db.execute("SELECT id FROM memory_items LIMIT 1").fetchone()[0]
    with store._db_lock:
        for n in range(changelog.MAX_ROWS):  # two log rows per update: twice the bound
            store.db.execute("UPDATE memory_items SET text = ? WHERE id = ?", (f"t{n}", row_id))
        store.db.commit()
    rows = store.db.execute(f"SELECT count(*) FROM {changelog.LOG_TABLE}").fetchone()[0]
    assert 0 < rows <= changelog.MAX_ROWS
    # The counter only grew, and the rows it dropped leave a gap past what the
    # index applied, so the next catch-up refuses and the index rebuilds.
    assert changelog.current_seq(store.db) >= applied + 2 * changelog.MAX_ROWS
    assert changelog.oldest_seq(store.db) > applied + 1
    store._memory_index = None
    fresh = _with_index(store, "on")
    assert fresh is not index and fresh._ensure_log()
    backend = fresh._open_backend()
    assert backend is not None and fresh._catch_up(backend) is False
