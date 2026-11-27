"""A big repair backlog drains at a higher duty only while the host is idle."""

import threading
from types import SimpleNamespace

import pytest
from member_memory_helpers import env as _member_env

from kiro_crew import embeddings
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.vector_memory_runtime import embedding_repair

env = _member_env


@pytest.fixture
def duty_config(monkeypatch):
    config = {}
    monkeypatch.setattr(embeddings, "_read_memory_config", lambda: config)
    return config


def test_boost_raises_only_the_shipped_duty_and_only_while_allowed(duty_config):
    idle = [True]
    assert embeddings.bulk_duty_cycle() == 0.2
    with embeddings.bulk_backlog_boost(lambda: idle[0]):
        assert embeddings.bulk_duty_cycle() == 0.5
        idle[0] = False
        assert embeddings.bulk_duty_cycle() == 0.2
        idle[0] = True
        duty_config["embedding_bulk_duty"] = 0.3
        assert embeddings.bulk_duty_cycle() == 0.3
        duty_config["embedding_bulk_duty"] = 0.2
        assert embeddings.bulk_duty_cycle() == 0.5
    assert embeddings.bulk_duty_cycle() == 0.2


def test_a_failing_idle_probe_keeps_the_default(duty_config):
    def probe():
        raise OSError("no load average here")

    with embeddings.bulk_backlog_boost(probe):
        assert embeddings.bulk_duty_cycle() == 0.2


def test_pending_rows_at_least_counts_both_kinds(env):
    tier = env.tiers["member-alice"]
    assert not embedding_repair.pending_rows_at_least(tier, 1)
    tier.set_semantic_if_absent("project.a", "x", 1.0, "user_explicit")
    tier.write_episodic("An episode written with no vector yet.", defer_embedding=True)
    assert embedding_repair.pending_rows_at_least(tier, 2)
    assert not embedding_repair.pending_rows_at_least(tier, 3)


def _visit(monkeypatch, *, backlog, load, turn):
    store = SimpleNamespace(embed_fn=lambda text: None, has_pending_embeddings=lambda: True)
    seen = []
    store.backfill_missing_embeddings = lambda **kw: seen.append(embeddings.bulk_duty_cycle())
    monkeypatch.setattr("kiro_crew.memory_stores.require_memory_store", lambda name: name)
    monkeypatch.setattr(
        "kiro_crew.memory_provider.resolver.builtin_background_allowed", lambda name: True
    )
    monkeypatch.setattr("kiro_crew.slack.gateway.peek_ready_shared_embedder", lambda: object())
    monkeypatch.setattr("kiro_crew.slack.gateway.reconcile_store_embedding_space", lambda s: 0)
    monkeypatch.setattr("kiro_crew.slack.gateway.store_embedding_space_is_stale", lambda s: False)
    monkeypatch.setattr(embedding_repair, "pending_rows_at_least", lambda s, n: backlog >= n)
    monkeypatch.setattr("os.getloadavg", lambda: (load, load, load), raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    monkeypatch.setattr("kiro_crew.memory_dream.person_turn_running", lambda gateway: turn)
    gateway = object.__new__(GatewayOrchestrator)
    gateway._auto_migrate_task = None
    gateway._memory_repair_stop = threading.Event()
    assert gateway._repair_store_once("member-alice", store, fresh=False)
    return seen


@pytest.mark.parametrize(
    ("backlog", "load", "turn", "duty"),
    [
        (500, 1.0, False, 0.5),  # big backlog, 0.25 load per core, no turn
        (499, 1.0, False, 0.2),  # backlog under the line
        (500, 2.0, False, 0.2),  # 0.5 load per core is busy
        (500, 1.0, True, 0.2),  # a chat turn is running
    ],
)
def test_visit_boosts_a_big_backlog_only_on_an_idle_host(
    duty_config, monkeypatch, backlog, load, turn, duty
):
    assert _visit(monkeypatch, backlog=backlog, load=load, turn=turn) == [duty]
    assert embeddings.bulk_duty_cycle() == 0.2
