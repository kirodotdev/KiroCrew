"""``route_for``: the one place a memory provider is chosen.

With no provider configured the answer is the shared builtin route and nothing is
read. A configured provider answers only when its app is enabled, policy allows it,
the owner consented to its egress and it can hold the store; otherwise the route
says why, and Crew's own memory never stands in. A session is frozen to the choice
it started with.
"""

from __future__ import annotations

import json

import pytest
from memory_provider_helpers import FIXTURE_ID, READONLY_ID, fixture_provider, set_provider

from kiro_crew.config import loader
from kiro_crew.history import ConversationLog
from kiro_crew.memory_provider import consent, resolver

pytestmark = pytest.mark.xdist_group("memory_provider")


@pytest.fixture
def fixture(monkeypatch, tmp_path):
    with fixture_provider(monkeypatch, tmp_path / "provider.json") as served:
        yield served


def test_no_provider_is_the_builtin_route_without_reading_anything(monkeypatch) -> None:
    def refuse(*_a, **_k):
        raise AssertionError("the default route read a session record")

    monkeypatch.setattr(resolver, "_metadata_log", refuse)
    route = resolver.route_for("default", session_key="dashboard:any")
    assert route is resolver.BUILTIN_ROUTE
    assert route.builtin_answers and route.is_builtin and not route.replaces
    assert resolver.builtin_writes_allowed("member-x")
    assert resolver.builtin_background_allowed()


def test_replace_routes_to_the_provider(fixture) -> None:
    set_provider("replace", FIXTURE_ID)
    route = resolver.route_for("default")
    assert route.replaces and not route.builtin_answers
    assert route.provider is fixture.fixture and route.unavailable == ""
    assert not resolver.builtin_writes_allowed("default")
    assert not resolver.builtin_background_allowed()


def test_augment_keeps_crew_memory_answering(fixture) -> None:
    set_provider("augment", READONLY_ID)
    route = resolver.route_for("default")
    assert route.augments and route.builtin_answers and route.provider is fixture.readonly


def test_a_store_pinned_to_builtin_stays_on_crew_memory(fixture) -> None:
    set_provider("replace", FIXTURE_ID)
    path = loader.config_path()
    cfg = loader.KiroCrewConfig.load()
    cfg.memory_stores["team"] = loader.MemoryStoreConfig(provider_mode="builtin")
    cfg.save()
    loader._invalidate_config_cache()
    assert path.is_file()
    assert resolver.route_for("team") is resolver.BUILTIN_ROUTE
    assert resolver.route_for("default").replaces


def test_a_session_keeps_the_choice_it_started_with(fixture) -> None:
    log = ConversationLog()
    log.update_metadata("dashboard:early", {"workspace": "default"})
    set_provider("replace", FIXTURE_ID, switched_at=1.0)
    assert resolver.route_for("default", session_key="dashboard:early").replaces
    stored = log.get_metadata("dashboard:early")[resolver.SNAPSHOT_FIELD]
    assert stored["mode"] == "replace" and stored["id"] == FIXTURE_ID
    # The owner switches back: the frozen chat stays, a new chat takes builtin.
    set_provider("builtin", FIXTURE_ID, switched_at=2.0)
    assert resolver.route_for("default", session_key="dashboard:early").replaces
    log.update_metadata("dashboard:late", {"workspace": "default"})
    assert resolver.route_for("default", session_key="dashboard:late").is_builtin


def test_a_chat_created_before_the_switch_stays_on_crew_memory(fixture) -> None:
    log = ConversationLog()
    log.update_metadata("dashboard:old", {"workspace": "default"})
    set_provider("replace", FIXTURE_ID, switched_at=4_000_000_000.0)
    assert resolver.route_for("default", session_key="dashboard:old").is_builtin


@pytest.mark.parametrize(
    "setup, reason",
    [
        (lambda mp, f: mp.setattr(resolver, "_shared_crew_on", lambda: True), "Shared Crew"),
        (lambda mp, f: mp.setattr(resolver, "_governance_permits", lambda key: False), "policy"),
        (lambda mp, f: setattr(f.health, "port", None), "describe"),
    ],
    ids=["shared_crew", "policy", "unhealthy"],
)
def test_an_unavailable_provider_never_falls_back(monkeypatch, fixture, setup, reason) -> None:
    set_provider("replace", FIXTURE_ID)
    fixture.fixture._caps = None
    setup(monkeypatch, fixture)
    route = resolver.route_for("default")
    assert route.replaces and route.provider is None
    assert reason in route.unavailable
    assert not resolver.builtin_writes_allowed("default")


def test_a_missing_app_is_unavailable(fixture) -> None:
    set_provider("replace", "app:gone/provider")
    route = resolver.route_for("default")
    assert route.replaces and route.provider is None and "not available" in route.unavailable


def test_a_remote_provider_needs_the_owners_consent(monkeypatch, fixture) -> None:
    caps = fixture.fixture.describe()
    remote = type(caps)(**{**caps.__dict__, "egress": "remote"})
    monkeypatch.setattr(fixture.fixture, "describe", lambda: remote)
    set_provider("replace", FIXTURE_ID)
    assert "agreed" in resolver.route_for("default").unavailable
    consent.grant_egress(FIXTURE_ID, "1.0.0")
    assert resolver.route_for("default").provider is fixture.fixture
    consent.grant_egress(FIXTURE_ID, "0.9.0")
    assert resolver.route_for("default").provider is None


def test_a_single_store_provider_cannot_hold_a_bot_store(monkeypatch, fixture) -> None:
    caps = fixture.fixture.describe()
    single = type(caps)(**{**caps.__dict__, "multi_store": False})
    monkeypatch.setattr(fixture.fixture, "describe", lambda: single)
    set_provider("replace", FIXTURE_ID)
    assert resolver.route_for("default").provider is fixture.fixture
    assert "one memory" in resolver.route_for("member-alice").unavailable


def test_a_resolver_fault_reads_as_unavailable_never_as_crew_memory(monkeypatch) -> None:
    from kiro_crew.memory_provider import chokepoints

    def broken(*_a, **_k):
        raise OSError("disk")

    monkeypatch.setattr(chokepoints, "route_for", broken)
    assert chokepoints.session_route("default", "dashboard:x") is resolver.BUILTIN_ROUTE
    set_provider("replace", FIXTURE_ID)
    route = chokepoints.session_route("default", "dashboard:x")
    assert route.replaces and route.provider is None and route.unavailable
    set_provider("augment", FIXTURE_ID)
    route = chokepoints.session_route("default", "dashboard:x")
    assert route.augments and route.builtin_answers and route.provider is None
    set_provider("builtin", FIXTURE_ID)
    assert chokepoints.session_route("default", "dashboard:x") is resolver.BUILTIN_ROUTE


def test_the_configured_context_timeout_reaches_the_client(fixture) -> None:
    set_provider(
        "replace", FIXTURE_ID, timeout_secs=7
    )  # flake-ok: a config value under test, not a wait
    route = resolver.route_for("default")
    assert route.provider is not None and route.provider.context_timeout == 7.0


def test_a_key_with_no_session_record_follows_a_switch_back(fixture) -> None:
    set_provider("replace", FIXTURE_ID)
    assert resolver.route_for("default", session_key="dashboard:ui").replaces
    # The owner's switch writes config and does not clear the in-process cache.
    path = loader.config_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["memory"]["provider"] = {"mode": "builtin", "id": FIXTURE_ID}
    path.write_text(json.dumps(data), encoding="utf-8")
    loader._invalidate_config_cache()
    assert resolver.route_for("default", session_key="dashboard:ui").is_builtin
