"""Rows a V2 consolidation writes without a vector get one at the next repair visit."""

import threading
from types import SimpleNamespace

from member_memory_helpers import env as _member_env

from kiro_crew import embeddings
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.vector_memory_runtime import embedding_repair

env = _member_env


def _vector(store):
    return [1.0] + [0.0] * (store._embedding_dim - 1)


def _consolidate(store, *, source_id="span-1", key="project.codename"):
    return store.apply_consolidation(
        source_id=source_id,
        session_key="chat:alice",
        source_total=0,
        snapshot={},
        messages=[],
        result={
            "semantic": [{"key": key, "value": "aurora", "confidence": 0.9}],
            "episodic": [{"text": f"The team approved the {key} release."}],
        },
    )


def _episode_vector(store, needle):
    row = store.db.execute(
        "SELECT embedding FROM episodic_memories WHERE text LIKE ?", (f"%{needle}%",)
    ).fetchone()
    return row["embedding"]


def test_consolidated_rows_jump_the_backlog_and_the_cursor_stays(env):
    tier = env.tiers["member-alice"]
    # A backlog that sorts ahead of the new fact and is never embedded.
    for index in range(3):
        tier.set_semantic_if_absent(f"project.abacklog{index}", "old", 1.0, "user_explicit")
    _consolidate(tier, key="project.zfresh")
    assert embedding_repair.has_fresh_vectors(tier)
    tier.embed_fn = lambda text: _vector(tier)

    assert tier._backfill_semantic_kv_embeddings(pace=False, max_rows=1) == 1
    assert tier.get_semantic("project.zfresh")["embedding"] is not None
    assert all(tier.get_semantic(f"project.abacklog{i}")["embedding"] is None for i in range(3))
    tier.backfill_missing_embeddings(pace=False, max_rows_per_kind=1)
    assert _episode_vector(tier, "project.zfresh") is not None
    assert not embedding_repair.has_fresh_vectors(tier)
    # The fresh page did not move the paging cursor: the backlog starts at its head.
    assert tier.get_semantic("project.abacklog0")["embedding"] is not None
    assert tier.get_semantic("project.abacklog1")["embedding"] is None


def _gateway(stop=None):
    gateway = object.__new__(GatewayOrchestrator)
    gateway.vector_memory = None
    gateway._auto_migrate_task = None
    gateway._memory_repair_cursor = ""
    gateway._memory_repair_stop = stop or threading.Event()
    return gateway


def _patch_visit(monkeypatch, entries, ready):
    monkeypatch.setattr("kiro_crew.context.cached_vector_store_entries", lambda: entries)
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.peek_ready_shared_embedder",
        lambda: object() if ready() else None,
    )
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.reembed_progress", lambda: SimpleNamespace(is_active=lambda: False)
    )
    monkeypatch.setattr("kiro_crew.slack.gateway.reconcile_store_embedding_space", lambda s: 0)
    monkeypatch.setattr("kiro_crew.memory_stores.require_memory_store", lambda name: name)


def test_a_written_fact_gets_its_vector_at_the_next_visit(env, monkeypatch):
    alice, bob = env.tiers["member-alice"], env.tiers["member-bob"]
    for index in range(40):
        alice.set_semantic_if_absent(f"project.abacklog{index:02}", "old", 1.0, "user_explicit")
    _patch_visit(monkeypatch, (("member-alice", alice), ("member-bob", bob)), lambda: True)
    for store in (alice, bob):
        store.embed_fn = lambda text, store=store: _vector(store)
    gateway = _gateway()
    gateway._memory_repair_cursor = "member-alice"  # the rotation's next turn is bob
    _consolidate(alice, key="project.zfresh")

    gateway._repair_member_memory_once()

    assert alice.get_semantic("project.zfresh")["embedding"] is not None
    assert _episode_vector(alice, "project.zfresh") is not None
    assert gateway._memory_repair_cursor == "member-alice"


def test_fresh_rows_load_a_cold_model_and_the_backlog_alone_does_not(env, monkeypatch):
    alice = env.tiers["member-alice"]
    alice.set_semantic_if_absent("project.abacklog", "old", 1.0, "user_explicit")
    loaded = threading.Event()
    waits = []

    def wait(timeout, should_stop=None):
        waits.append(timeout)
        loaded.set()
        return True

    monkeypatch.setattr(embeddings, "wait_for_cold_shared_embedder", wait)
    _patch_visit(monkeypatch, (("member-alice", alice),), loaded.is_set)
    alice.embed_fn = lambda text: _vector(alice)
    gateway = _gateway()

    gateway._repair_member_memory_once()
    assert waits == [] and alice.get_semantic("project.abacklog")["embedding"] is None

    _consolidate(alice, key="project.zfresh")
    gateway._repair_member_memory_once()
    assert waits == [120.0]
    assert alice.get_semantic("project.zfresh")["embedding"] is not None


def test_busy_fresh_stores_share_visits_with_the_rotation(monkeypatch):
    stores = {
        name: SimpleNamespace(_fresh_vectors={"fact": {"k": None}})
        for name in ("member-a", "member-b")
    }
    stores["member-c"] = SimpleNamespace()
    _patch_visit(monkeypatch, tuple(sorted(stores.items())), lambda: True)
    visits = []
    monkeypatch.setattr(
        GatewayOrchestrator,
        "_repair_store_once",
        lambda self, name, store, *, fresh: visits.append((name, fresh)) or True,
    )
    gateway = _gateway()
    for _ in range(6):
        gateway._repair_member_memory_once()
    # Queues never drain here, yet the rotation still gets every other visit.
    assert visits == [
        ("member-a", True),
        ("member-a", False),
        ("member-b", True),
        ("member-b", False),
        ("member-a", True),
        ("member-c", False),
    ]


def test_a_turned_away_store_stops_jumping_the_rotation(env, monkeypatch):
    alice = env.tiers["member-alice"]
    _patch_visit(monkeypatch, (("member-alice", alice),), lambda: False)
    monkeypatch.setattr(embeddings, "wait_for_cold_shared_embedder", lambda *a, **k: False)
    _consolidate(alice)
    _gateway()._repair_member_memory_once()
    assert not embedding_repair.has_fresh_vectors(alice)
    assert alice.get_semantic("project.codename")["embedding"] is None


class _ColdBackend:
    def __init__(self, *, loads=True):
        self.loaded = False
        self.loads = loads
        self.waits = 0

    def is_ready(self):
        return self.loaded

    def can_load_on_demand(self):
        return self.loads

    def wait_ready(self, timeout=None):
        self.waits += 1
        self.loaded = self.loads
        return self.loaded


def test_cold_wait_loads_only_a_built_unloaded_backend(monkeypatch):
    monkeypatch.setattr(embeddings, "_shared_embedder", None)
    assert embeddings.wait_for_cold_shared_embedder(1.0) is False
    backend = _ColdBackend()
    monkeypatch.setattr(embeddings, "_shared_embedder", backend)
    assert embeddings.wait_for_cold_shared_embedder(1.0) is True
    # Already loaded: an empty embed was some other failure, so no retry.
    assert embeddings.wait_for_cold_shared_embedder(1.0) is False
    broken = _ColdBackend(loads=False)
    monkeypatch.setattr(embeddings, "_shared_embedder", broken)
    assert embeddings.wait_for_cold_shared_embedder(1.0) is False
    assert broken.waits == 0
    stopped = _ColdBackend()
    monkeypatch.setattr(embeddings, "_shared_embedder", stopped)
    assert embeddings.wait_for_cold_shared_embedder(1.0, lambda: True) is False
    assert stopped.waits == 0


def test_a_cold_sweep_waits_for_the_load_and_fills_the_row(env, monkeypatch):
    tier = env.tiers["member-alice"]
    tier.set_semantic_if_absent("project.cold", "stored", 1.0, "user_explicit")
    backend = _ColdBackend()
    managed = lambda text: None  # noqa: E731 - identity stand-in for the shared embed fn
    monkeypatch.setattr(embeddings, "_shared_embedder", backend)
    monkeypatch.setattr(embeddings, "make_sync_embed_fn", lambda: managed)
    tier.embed_fn = managed
    # A bulk embed on an unloaded model returns nothing and only starts the load.
    monkeypatch.setattr(
        tier, "_try_embed", lambda text, priority=0: _vector(tier) if backend.loaded else None
    )

    assert tier._backfill_semantic_kv_embeddings(pace=True, max_rows=16) == 1
    assert backend.waits == 1
    assert tier.get_semantic("project.cold")["embedding"] is not None


class _SlowLoad(_ColdBackend):
    """A load that never finishes; each wait slice may stop the caller."""

    def __init__(self, on_wait):
        super().__init__()
        self.on_wait = on_wait
        self.slices = []

    def wait_ready(self, timeout=None):
        self.slices.append(timeout)
        self.on_wait()
        return False


def test_shutdown_ends_the_cold_wait_at_once(monkeypatch):
    stop = threading.Event()
    backend = _SlowLoad(stop.set)
    monkeypatch.setattr(embeddings, "_shared_embedder", backend)
    assert embeddings.wait_for_cold_shared_embedder(120.0, stop.is_set) is False
    # One short slice, then the stop is seen: no 120 s wait on shutdown.
    assert backend.slices == [0.1]


def test_a_sweep_stopped_during_the_cold_wait_commits_nothing(env, monkeypatch):
    tier = env.tiers["member-alice"]
    tier.set_semantic_if_absent("project.cold", "stored", 1.0, "user_explicit")
    stop = threading.Event()
    backend = _SlowLoad(stop.set)
    managed = lambda text: None  # noqa: E731 - identity stand-in for the shared embed fn
    monkeypatch.setattr(embeddings, "_shared_embedder", backend)
    monkeypatch.setattr(embeddings, "make_sync_embed_fn", lambda: managed)
    tier.embed_fn = managed
    monkeypatch.setattr(tier, "_try_embed", lambda text, priority=0: None)

    assert (
        tier._backfill_semantic_kv_embeddings(pace=True, max_rows=16, should_stop=stop.is_set) == 0
    )
    assert backend.slices == [0.1]
    assert tier.get_semantic("project.cold")["embedding"] is None
