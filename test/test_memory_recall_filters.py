"""``memory_recall`` kind and time filters: parsing, store behavior, wire hand-off.

Omitting every filter must leave today's answer untouched, so most store tests
compare a filtered answer with the unfiltered one over the same rows.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from member_memory_helpers import declare_v2_store
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew import memory_stores
from kiro_crew.dashboard.handlers import memory_member
from kiro_crew.mcp_tools import learn
from kiro_crew.memory_recall_filters import RecallFilters, parse_filters
from kiro_crew.vector_memory import VectorMemoryStore, open_member_database

env = _member_env


@pytest.fixture(autouse=True)
def _isolated(_floor_monkeypatch):
    """Keep these tests off the ambient environment's session and memory roots."""


@pytest.fixture(params=["v1", "v2"])
def store(request, tmp_path, monkeypatch):
    root = tmp_path / "memory_stores"
    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: root)
    if request.param == "v2":
        tier = open_member_database(
            declare_v2_store(tmp_path, "member-alice") / "memory.db",
            member_id="alice",
            store_id="member-alice",
            embedding_dim=2,
        )
    else:
        tier = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
        tier.init()
    # Recall here is about which rows are eligible, not about vector dedup.
    tier._faiss_index = None
    tier.embed_fn = lambda _query: [1, 0]
    yield tier
    tier.close()


def _stamp(
    store: VectorMemoryStore,
    table: str,
    column: str,
    identity: str,
    when: str,
    stamped: tuple[str, ...] = ("created_at",),
) -> None:
    assignments = ", ".join(f"{name}=?" for name in stamped)
    store.db.execute(
        f"UPDATE {table} SET {assignments} WHERE {column}=?", (*[when] * len(stamped), identity)
    )
    store.db.commit()
    store._invalidate_episodic_scoring()


def _fact(store, key: str, text: str, written: str, **kwargs) -> None:
    assert store.set_semantic(key, text, 1, "user_explicit", **kwargs) is None
    _stamp(store, store._sem_rel, "key", key, written, ("created_at", "updated_at"))


def _rewrite(store, key: str, text: str, written: str) -> None:
    """Set a new value on an existing fact; the upsert keeps its ``created_at``."""
    assert store.set_semantic(key, text, 1, "user_explicit") is None
    _stamp(store, store._sem_rel, "key", key, written, ("updated_at",))


def _episode(store, text: str, written: str, **kwargs) -> str:
    assert store.write_episodic(text, embedding=[1, 0], **kwargs)
    row = next(r for r in store.get_episodic_list() if r["text"] == text)
    _stamp(store, store._epi_rel, "id", row["id"], written)
    return row["id"]


def _keys(result: dict) -> list[str]:
    return sorted(row["key"] for row in result["retrieval"]["facts"])


def _texts(result: dict) -> list[str]:
    return sorted(row["text"] for row in result["retrieval"]["episodes"])


def _parsed(raw: dict) -> RecallFilters | None:
    """The filters ``raw`` parses to, with no warning expected."""
    filters, warning = parse_filters(raw)
    assert warning == ""
    return filters


def _at(text: str) -> RecallFilters:
    filters = _parsed({"as_of": text})
    assert filters is not None
    return filters


# ---- parsing ---------------------------------------------------------------------------


def test_no_filter_arguments_parse_to_none():
    assert parse_filters({}) == (None, "")
    assert parse_filters({"kind": "", "since": None}) == (None, "")


def test_a_bare_date_is_midnight_utc_and_an_offset_is_converted():
    filters = _parsed({"since": "2026-10-01", "until": "2026-11-01T01:00:00+01:00"})
    assert filters.since == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert filters.until == datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "raw,message",
    [
        ({"kind": "note"}, "kind ignored: it must be one of fact, episode, lesson"),
        ({"kind": 3}, "kind ignored: it must be one of fact, episode, lesson"),
        ({"since": "last october"}, "since ignored: it must be an ISO date or timestamp"),
        ({"as_of": 20261001}, "as_of ignored: it must be an ISO date or timestamp"),
        ({"until": "x" * 200}, "until ignored: it must be an ISO date or timestamp"),
    ],
)
def test_a_malformed_filter_is_dropped_with_a_warning(raw, message):
    assert parse_filters(raw) == (None, "Warning: " + message)


def test_a_valid_filter_beside_a_malformed_one_still_applies():
    filters, warning = parse_filters({"kind": "fact", "since": "last october"})
    assert filters == RecallFilters(kind="fact")
    assert warning == "Warning: since ignored: it must be an ISO date or timestamp"
    filters, warning = parse_filters({"kind": "gossip", "until": "2026-11-01"})
    assert filters == RecallFilters(until=datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert warning.startswith("Warning: kind ignored")


def test_since_not_earlier_than_until_drops_both_and_keeps_the_rest():
    raw = {"kind": "episode", "since": "2026-11-01", "until": "2026-10-01", "as_of": "2026-10-15"}
    filters, warning = parse_filters(raw)
    assert filters == RecallFilters(
        kind="episode", as_of=datetime(2026, 10, 15, tzinfo=timezone.utc)
    )
    assert warning == "Warning: since and until ignored: since must be earlier than until"
    assert parse_filters({"since": "2026-10-01", "until": "2026-10-01"})[0] is None


def test_time_filters_beside_kind_lesson_are_dropped_and_the_kind_kept():
    filters, warning = parse_filters(
        {"kind": "lesson", "since": "2026-10-01", "as_of": "2026-10-15"}
    )
    assert filters == RecallFilters(kind="lesson")
    assert warning == (
        "Warning: since, as_of ignored: time filters apply to facts and episodes, not lessons"
    )


def test_every_filter_dropped_leaves_no_filter_object():
    filters, warning = parse_filters({"kind": "note", "since": "soon"})
    assert filters is None
    assert warning.count(";") == 1


# ---- store behavior --------------------------------------------------------------------


def test_omitting_filters_returns_exactly_todays_answer(store):
    _fact(store, "project.alpha", "alpha release notes", "2026-09-10T00:00:00+00:00")
    _episode(store, "alpha release meeting happened", "2026-10-10T00:00:00+00:00")
    baseline = store.recall("alpha release", cap=2000)
    assert store.recall("alpha release", cap=2000, filters=None) == baseline
    assert "filters" not in baseline["retrieval"]
    assert _keys(baseline) == ["project.alpha"] and len(_texts(baseline)) == 1


def test_kind_returns_only_that_part_of_the_answer(store):
    _fact(store, "project.alpha", "alpha release notes", "2026-09-10T00:00:00+00:00")
    _episode(store, "alpha release meeting happened", "2026-10-10T00:00:00+00:00")
    store.write_lesson("alpha release notes always get a changelog")
    both = store.recall("alpha release", cap=3000)
    assert both["lessons_context"] and _keys(both) and _texts(both)

    facts = store.recall("alpha release", cap=3000, filters=_parsed({"kind": "fact"}))
    assert _keys(facts) == ["project.alpha"] and not _texts(facts) and not facts["lessons_context"]
    episodes = store.recall("alpha release", cap=3000, filters=_parsed({"kind": "episode"}))
    assert _texts(episodes) == _texts(both) and not _keys(episodes)
    assert not episodes["lessons_context"]
    lessons = store.recall("alpha release", cap=3000, filters=_parsed({"kind": "lesson"}))
    assert lessons["lessons_context"] == both["lessons_context"]
    assert not _keys(lessons) and not _texts(lessons)
    assert facts["retrieval"]["filters"] == {"kind": "fact"}


def test_since_and_until_keep_what_was_written_in_the_half_open_range(store):
    for month, day in ((9, 30), (10, 1), (10, 31), (11, 1)):
        when = f"2026-{month:02d}-{day:02d}T12:00:00+00:00"
        _fact(store, f"project.plan_{month}_{day}", f"launch plan {month}-{day}", when)
        _episode(store, f"launch plan episode {month}-{day}", when)
    october = _parsed({"since": "2026-10-01", "until": "2026-11-01"})
    result = store.recall("launch plan", cap=3000, filters=october)
    assert _keys(result) == ["project.plan_10_1", "project.plan_10_31"]
    assert _texts(result) == ["launch plan episode 10-1", "launch plan episode 10-31"]
    only_since = store.recall(
        "launch plan", cap=3000, filters=_parsed({"since": "2026-10-31T12:00:00Z"})
    )
    assert _keys(only_since) == ["project.plan_10_31", "project.plan_11_1"]
    only_until = store.recall(
        "launch plan", cap=3000, filters=_parsed({"until": "2026-10-01T12:00:00Z"})
    )
    assert _keys(only_until) == ["project.plan_9_30"]


def test_a_time_filter_leaves_lessons_out_of_a_mixed_query(store):
    _fact(store, "project.alpha", "alpha release notes", "2026-10-10T00:00:00+00:00")
    store.write_lesson("alpha release notes always get a changelog")
    result = store.recall("alpha release", cap=3000, filters=_parsed({"since": "2026-10-01"}))
    assert _keys(result) == ["project.alpha"] and not result["lessons_context"]


def test_as_of_finds_a_fact_whose_window_closed_and_hides_one_not_yet_true(store):
    _fact(
        store,
        "project.owner_old",
        "owner of billing is Dana",
        "2026-01-05T00:00:00+00:00",
        metadata={"valid_from": "2026-01-01", "valid_until": "2026-03-10"},
    )
    _fact(
        store,
        "project.owner_new",
        "owner of billing is Eli",
        "2026-03-10T00:00:00+00:00",
        metadata={"valid_from": "2026-03-10"},
    )
    now = store.recall("owner of billing", cap=3000)
    assert _keys(now) == ["project.owner_new"]

    march_3 = store.recall("owner of billing", cap=3000, filters=_at("2026-03-03"))
    assert _keys(march_3) == ["project.owner_old"]
    assert march_3["retrieval"]["filters"] == {"as_of": "2026-03-03T00:00:00+00:00"}
    # The window is half open: the instant the old one closed belongs to the new one.
    boundary = store.recall("owner of billing", cap=3000, filters=_at("2026-03-10"))
    assert _keys(boundary) == ["project.owner_new"]
    before_any = store.recall("owner of billing", cap=3000, filters=_at("2025-12-31"))
    assert _keys(before_any) == []
    # The widening ends with the call: today's answer is unchanged afterwards.
    assert store.recall("owner of billing", cap=3000) == now


def test_as_of_treats_an_undated_record_as_true_from_when_it_was_written(store):
    _fact(store, "project.alpha", "alpha release notes", "2026-02-01T00:00:00+00:00")
    _episode(store, "alpha release meeting happened", "2026-02-01T00:00:00+00:00")
    after = store.recall("alpha release", cap=3000, filters=_at("2026-02-02"))
    assert _keys(after) == ["project.alpha"] and len(_texts(after)) == 1
    before = store.recall("alpha release", cap=3000, filters=_at("2026-01-31"))
    assert not _keys(before) and not _texts(before)


def test_as_of_does_not_resurrect_a_record_whose_status_is_not_active(store):
    _fact(
        store,
        "project.ended",
        "ended budget figure",
        "2026-01-05T00:00:00+00:00",
        metadata={"status": "expired", "valid_until": "2026-06-01"},
    )
    result = store.recall("budget figure", cap=3000, filters=_at("2026-02-01"))
    assert _keys(result) == []


def test_a_rewritten_fact_is_dated_by_when_its_current_value_was_set(store):
    """An upsert keeps ``created_at``; the value returned was written at ``updated_at``."""
    _fact(store, "project.owner", "owner of billing is Dana", "2026-01-05T00:00:00+00:00")
    _rewrite(store, "project.owner", "owner of billing is Eli", "2026-10-10T00:00:00+00:00")
    row = store.db.execute(
        f"SELECT created_at FROM {store._sem_rel} WHERE key = ?", ("project.owner",)
    ).fetchone()
    assert row[0].startswith("2026-01-05")

    def keys(**raw):
        return _keys(store.recall("owner of billing", cap=3000, filters=_parsed(raw)))

    assert keys(since="2026-10-01", until="2026-11-01") == ["project.owner"]
    assert keys(since="2026-01-01", until="2026-02-01") == []
    # The value on record was set in October, so it does not answer for February.
    assert keys(as_of="2026-02-01") == []
    assert keys(as_of="2026-10-15") == ["project.owner"]


def test_as_of_is_judged_by_the_check_recall_applies_now(store, monkeypatch):
    """One definition of "valid at an instant": ``eligible``, handed the ``as_of`` instant."""
    from kiro_crew import memory_record_metadata as record_meta

    seen: list[str | None] = []
    real = record_meta.eligible

    def spy(metadata, *, now=None):
        seen.append(now)
        return real(metadata, now=now)

    monkeypatch.setattr(record_meta, "eligible", spy)
    _fact(
        store,
        "project.owner_old",
        "owner of billing is Dana",
        "2026-01-05T00:00:00+00:00",
        metadata={"valid_from": "2026-01-01", "valid_until": "2026-03-10"},
    )
    result = store.recall("owner of billing", cap=3000, filters=_at("2026-03-03"))
    assert _keys(result) == ["project.owner_old"]
    assert "2026-03-03T00:00:00+00:00" in seen


# ---- wire hand-off ---------------------------------------------------------------------


@pytest.fixture
def seen(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(learn.mcp_core, "_resolve_session_key_strict", lambda: "dashboard:alice")
    monkeypatch.setattr(
        learn.mcp_core,
        "_get",
        lambda path, **_kw: calls.append(path) or {"retrieval": {"facts": [], "episodes": []}},
    )
    return calls


def test_the_tool_sends_only_the_query_when_no_filter_is_given(seen):
    learn.memory_recall("memory_recall", {"query": "alpha release"})
    assert seen == ["/api/memory/recall?q=alpha+release"]


def test_the_tool_forwards_the_filters_it_was_given(seen):
    learn.memory_recall(
        "memory_recall",
        {"query": "alpha", "kind": "fact", "since": "2026-10-01", "as_of": "2026-10-15"},
    )
    assert seen == [
        "/api/memory/recall?q=alpha&kind=fact"
        "&since=2026-10-01T00%3A00%3A00%2B00%3A00&as_of=2026-10-15T00%3A00%3A00%2B00%3A00"
    ]


def test_the_tool_answers_a_bad_filter_as_without_it_and_says_so(seen):
    plain = learn.memory_recall("memory_recall", {"query": "alpha"})
    result = learn.memory_recall("memory_recall", {"query": "alpha", "since": "october"})
    assert seen == ["/api/memory/recall?q=alpha"] * 2
    assert result == plain + "\nWarning: since ignored: it must be an ISO date or timestamp"
    assert not plain.startswith("Error")


def test_the_tool_forwards_the_valid_filters_beside_a_dropped_one(seen):
    result = learn.memory_recall(
        "memory_recall",
        {"query": "alpha", "kind": "episode", "since": "2026-11-01", "until": "2026-10-01"},
    )
    assert seen == ["/api/memory/recall?q=alpha&kind=episode"]
    assert result.endswith("\nWarning: since and until ignored: since must be earlier than until")


def test_the_tool_keeps_the_kind_and_drops_time_filters_for_lessons(seen):
    result = learn.memory_recall(
        "memory_recall", {"query": "alpha", "kind": "lesson", "as_of": "2026-10-15"}
    )
    assert seen == ["/api/memory/recall?q=alpha&kind=lesson"]
    assert result.endswith("not lessons")


def test_the_tool_adds_no_warning_line_to_a_clean_call(seen):
    assert "Warning" not in learn.memory_recall("memory_recall", {"query": "a", "kind": "fact"})


def test_the_tool_schema_lists_the_optional_filters_and_requires_only_query():
    schema = next(t for t in learn.schemas() if t["name"] == "memory_recall")["inputSchema"]
    assert set(schema["properties"]) == {"query", "kind", "since", "until", "as_of"}
    assert schema["required"] == ["query"]


@pytest.mark.asyncio
async def test_the_route_answers_a_bad_filter_as_without_it_with_a_warning(env):
    async def answer(**filters):
        response = await memory_member.api_memory_recall(
            request(env, query={"q": "alpha", "store": "member-alice", **filters}, owner=True)
        )
        assert response.status == 200
        return json.loads(response.text)

    tier = env.tiers["member-alice"]
    tier._faiss_index = None
    assert tier.set_semantic("project.alpha", "alpha release notes", 1, "user_explicit") is None
    plain = await answer()
    assert [row["key"] for row in plain["retrieval"]["facts"]] == ["project.alpha"]
    assert "warning" not in plain
    for filters, text in [
        ({"until": "tomorrow-ish"}, "until ignored: it must be an ISO date or timestamp"),
        ({"kind": "note"}, "kind ignored: it must be one of fact, episode, lesson"),
        (
            {"since": "2026-11-01", "until": "2026-10-01"},
            "since and until ignored: since must be earlier than until",
        ),
        (
            {"kind": "lesson", "as_of": "2026-10-01"},
            "as_of ignored: time filters apply to facts and episodes, not lessons",
        ),
    ]:
        got = await answer(**filters)
        assert got.pop("warning") == "Warning: " + text
        if "kind" not in filters or filters["kind"] != "lesson":
            assert got == plain


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "member-alice"])
async def test_the_route_applies_as_of_and_kind_to_the_real_store(env, name):
    tier = env.tiers[name]
    tier._faiss_index = None
    assert (
        tier.set_semantic(
            "project.owner_old",
            "owner of billing is Dana",
            1,
            "user_explicit",
            metadata={"valid_from": "2026-01-01", "valid_until": "2026-03-10"},
        )
        is None
    )
    query = {"q": "owner of billing", "store": name or "default"}

    async def keys(**filters):
        response = await memory_member.api_memory_recall(
            request(env, query={**query, **filters}, owner=True)
        )
        assert response.status == 200
        return [row["key"] for row in json.loads(response.text)["retrieval"]["facts"]]

    assert await keys() == []
    assert await keys(as_of="2026-03-03") == ["project.owner_old"]
    assert await keys(as_of="2026-03-03", kind="episode") == []
