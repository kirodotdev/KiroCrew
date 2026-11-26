"""The memory page's record list sorts, filters and counts across the whole store."""

from __future__ import annotations

import pytest
from test_memory_edit import store  # noqa: F401  (the v1 / v2 store fixture)

from kiro_crew import memory_edit, memory_record_facets


def _lesson(store, key, rule, category, applies, repo_scope=None, confidence=0.5):  # noqa: F811
    value = {"rule": rule, "category": category, "applies": applies}
    if repo_scope is not None:
        value["repo_scope"] = repo_scope
    assert store.set_semantic(key, value, confidence, "user_explicit") is None


def _seed(store):  # noqa: F811
    assert store.set_semantic("user.timezone", "America/Los_Angeles", 1.0, "user_explicit") is None
    assert store.set_semantic("project.flaky", "shard 8", 0.81, "taskrunner") is None
    _lesson(store, "lesson.a", "Work in a worktree", "tool", "always", confidence=1.0)
    _lesson(store, "lesson.b", "Test before merge", "tool", "on_topic", "website", 0.9)
    _lesson(store, "lesson.c", "Reply in Chinese", "preference", "always", confidence=0.85)
    assert store.write_episodic(
        "Reran shard 8 after a worktree timeout.", conversation_id="c1", tags=["worktree"]
    )


def _ids(body):
    return [entry["id"] for entry in body["entries"]]


def test_confidence_sort_runs_both_ways_and_keeps_experiences_last(store):  # noqa: F811
    _seed(store)
    up = memory_edit.list_records(store, {}, sort="confidence", descending=False)
    down = memory_edit.list_records(store, {}, sort="confidence", descending=True)
    assert _ids(up)[:2] == ["project.flaky", "lesson.c"]
    assert _ids(down)[:2] in (["lesson.a", "user.timezone"], ["user.timezone", "lesson.a"])
    for body in (up, down):
        assert body["entries"][-1]["kind"] == "episode"
        assert body["total"] == 6


def test_filters_list_only_matching_lessons_and_counts_ignore_their_own_group(store):  # noqa: F811
    _seed(store)
    body = memory_edit.list_records(
        store,
        {"kind": "directive", "filters": {"category": ["tool"], "applies": ["always"]}},
        facets=True,
    )
    assert _ids(body) == ["lesson.a"]
    facets = body["facets"]
    # Category is counted with the applies filter only: two "always" lessons.
    assert facets["category"] == {"tool": 1, "preference": 1}
    # Applies is counted with the category filter only: both tool lessons.
    assert facets["applies"] == {"always": 1, "on_topic": 1}
    assert facets["scope"] == {"": 1}
    # The kind counts ignore the kind, so each tab shows what it would list.
    assert facets["kind"] == {"directive": 1}


def test_scope_and_tag_and_confidence_range_filter(store):  # noqa: F811
    _seed(store)
    assert _ids(memory_edit.list_records(store, {"filters": {"scope": ["website"]}})) == [
        "lesson.b"
    ]
    episodes = memory_edit.list_records(store, {"filters": {"tag": ["worktree"]}})
    assert [entry["kind"] for entry in episodes["entries"]] == ["episode"]
    ranged = memory_edit.list_records(store, {"filters": {"conf_min": 0.88, "conf_max": 0.95}})
    assert _ids(ranged) == ["lesson.b"]


def test_select_all_matching_reaches_only_the_filtered_rows(store):  # noqa: F811
    _seed(store)
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        {
            "selection": {"query": {"q": "", "filters": {"category": ["preference"]}}},
            "operation": {"type": "forget"},
        },
    )
    assert preview["matched_count"] == 1


def test_best_match_puts_key_hits_first_and_needs_words(store):  # noqa: F811
    _seed(store)
    body = memory_edit.list_records(store, {"q": "worktree"}, sort="match")
    assert body["entries"][0]["match"] >= body["entries"][-1]["match"]
    with pytest.raises(memory_edit.MemoryEditError):
        memory_edit.list_records(store, {}, sort="match")


def test_paging_a_sorted_list_never_skips_or_repeats(store):  # noqa: F811
    _seed(store)
    whole = _ids(memory_edit.list_records(store, {}, sort="updated", limit=100))
    pages = []
    for offset in range(0, 6, 2):
        pages += _ids(memory_edit.list_records(store, {}, sort="updated", limit=2, offset=offset))
    assert pages == whole


def _stamp(store, key, created, updated):  # noqa: F811
    """Set one fact's created and updated times wherever this lineage keeps them."""
    with store._db_lock:
        if store.db.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'memory_items' AND type = 'table'"
        ).fetchone():
            store.db.execute(
                "UPDATE memory_items SET created_at = ?, updated_at = ? WHERE id = ?",
                (created, updated, "key:" + key),
            )
        else:
            store.db.execute(
                "UPDATE semantic_memory SET created_at = ?, updated_at = ? WHERE key = ?",
                (created, updated, key),
            )
        store.db.commit()


def test_a_date_range_narrows_the_list_and_every_count_on_the_sort_field(store):  # noqa: F811
    _seed(store)
    for key in ("user.timezone", "project.flaky", "lesson.a", "lesson.b", "lesson.c"):
        _stamp(store, key, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
    _stamp(store, "lesson.a", "2026-01-01T00:00:00+00:00", "2026-03-10T08:00:00+00:00")
    _stamp(store, "user.timezone", "2026-03-05T00:00:00+00:00", "2026-03-05T00:00:00+00:00")
    march = {"since": "2026-03-01T00:00:00Z", "until": "2026-04-01T00:00:00Z"}
    by_updated = memory_edit.list_records(store, {"filters": march}, facets=True)
    assert sorted(_ids(by_updated)) == ["lesson.a", "user.timezone"]
    assert by_updated["total"] == 2
    assert by_updated["facets"]["kind"] == {"directive": 1, "fact": 1}
    by_created = memory_edit.list_records(
        store, {"filters": {**march, "date_field": "created"}}, facets=True
    )
    assert _ids(by_created) == ["user.timezone"]
    assert by_created["facets"]["kind"] == {"fact": 1}
    # The bounds are half-open: an event at `until` is outside.
    edge = {"since": "2026-03-05T00:00:00+00:00", "until": "2026-03-10T08:00:00+00:00"}
    assert _ids(memory_edit.list_records(store, {"filters": edge})) == ["user.timezone"]
    # Clearing the range restores every row.
    assert memory_edit.list_records(store, {}, facets=True)["total"] == 6


@pytest.mark.parametrize(
    "filters",
    [
        {"applies": ["sometimes"]},
        {"conf_min": 2},
        {"conf_min": 0.9, "conf_max": 0.1},
        {"x": ["y"]},
        {"since": "yesterday"},
        {"since": "2026-02-01", "until": "2026-01-01"},
        {"since": "2026-02-01", "date_field": "deleted"},
    ],
)
def test_bad_filters_are_a_client_error(store, filters):  # noqa: F811
    with pytest.raises(memory_edit.MemoryEditError):
        memory_edit.list_records(store, {"filters": filters})


def test_unusable_scope_has_its_own_facet_value():
    assert (
        memory_record_facets.lesson_facets('{"rule": "x", "repo_scope": ["a"]}')["scope"]
        == memory_record_facets.SCOPE_UNUSABLE
    )
    assert memory_record_facets.lesson_facets('"legacy text"') == {
        "category": "knowledge",
        "applies": "unstated",
        "scope": "",
    }


def test_a_sorted_list_keeps_legacy_rows_with_no_id(tmp_path):
    from test_memory_edit import _store_with_null_id_rows

    tier = _store_with_null_id_rows(tmp_path)
    try:
        plain = memory_edit.list_records(tier, {})
        for sort in ("updated", "key", "confidence"):
            body = memory_edit.list_records(tier, {}, sort=sort)
            assert body["total"] == plain["total"] == 5
            assert len(body["entries"]) == 5
    finally:
        tier.close()


def test_a_repeated_tag_counts_its_record_once(store):  # noqa: F811
    assert store.write_episodic("Reran shard 8.", conversation_id="c1", tags=["ci", "ci"])
    body = memory_edit.list_records(store, {}, facets=True)
    assert body["facets"]["tag"] == {"ci": 1}


def test_a_write_between_the_two_passes_does_not_shift_the_page(tmp_path, monkeypatch):
    import sqlite3

    from kiro_crew.vector_memory import VectorMemoryStore

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    tier = VectorMemoryStore(db_path=tmp_path / "memory.db")
    tier.init()
    try:
        assert tier.set_semantic("user.b", "kept", 1.0, "user_explicit") is None
        real_rows = memory_edit._rows

        def rows_then_write(store, query, identities=None, **kwargs):  # noqa: F811
            yield from real_rows(store, query, identities, **kwargs)
            if kwargs.get("summaries"):
                # Another process commits a row that sorts first, between the passes.
                other = sqlite3.connect(tmp_path / "memory.db")
                other.execute(
                    "INSERT INTO semantic_memory (key, value_json, confidence, source, "
                    "created_at, updated_at) VALUES ('user.a', '\"new\"', 1.0, 'taskrunner', "
                    "'2026-01-01', '2026-01-01')"
                )
                other.commit()
                other.close()

        monkeypatch.setattr(memory_edit, "_rows", rows_then_write)
        body = memory_edit.list_records(
            tier, {"filters": {"source": ["user_explicit"]}}, sort="key"
        )
        assert [(entry["id"], entry["source"]) for entry in body["entries"]] == [
            ("user.b", "user_explicit")
        ]
    finally:
        tier.close()


def test_the_unusable_scope_value_is_no_real_scope(store):  # noqa: F811
    from kiro_crew.project_scope import scope_is_admissible

    assert not scope_is_admissible(memory_record_facets.SCOPE_UNUSABLE)
    _lesson(store, "lesson.real", "Real scope", "tool", "always", "!unusable")
    _lesson(store, "lesson.bad", "Bad scope", "tool", "always", ["a", "b"])
    body = memory_edit.list_records(
        store, {"filters": {"scope": [memory_record_facets.SCOPE_UNUSABLE]}}
    )
    assert [entry["id"] for entry in body["entries"]] == ["lesson.bad"]


def test_channel_and_crew_filter_and_count_on_a_bot_store_only(store):  # noqa: F811
    from kiro_crew.memory_schema import LINEAGE_CREW, MemoryFacets

    slack = MemoryFacets(surface="slack", crew="reviewer")
    assert store.set_semantic("user.editor", "vim", 0.9, "user_explicit", facets=slack) is None
    assert store.write_episodic("Reviewed a PR from cron.", facets=MemoryFacets(surface="cron"))
    assert store.set_semantic("user.shell", "zsh", 0.9, "user_explicit") is None
    body = memory_edit.list_records(store, {"filters": {"surface": ["slack"]}}, facets=True)
    if store._lineage != LINEAGE_CREW:
        # A Global V1 row carries no channel or crew, so it counts toward no value.
        assert body["facets"]["surface"] == {} and body["facets"]["crew"] == {}
        assert body["total"] == 0
        return
    assert _ids(body) == ["user.editor"]
    # Surface ignores its own filter; crew is counted within it.
    assert body["facets"]["surface"] == {"slack": 1, "cron": 1, "": 1}
    assert body["facets"]["crew"] == {"reviewer": 1}
    both = memory_edit.list_records(store, {"filters": {"crew": ["", "reviewer"]}}, facets=True)
    assert both["total"] == 3


def test_a_prefixed_source_counts_and_filters_under_its_prefix():
    """One value per chat session would list thousands of one-row sources."""
    group = memory_record_facets.source_group
    assert group("consolidation:dashboard:chat-1-179") == "consolidation"
    assert group("consolidation") == "consolidation"
    assert group("user_explicit") == "user_explicit"
    assert group(":odd") == ":odd"
    assert group("") == memory_record_facets.SOURCE_UNKNOWN
    summaries = [
        {
            "kind": "fact",
            "confidence": 0.9,
            "facets": memory_record_facets.summary_facets(
                "fact", "1", "", f"consolidation:dashboard:chat-{n}"
            ),
        }
        for n in range(3)
    ]
    counts = memory_record_facets.count_facets(summaries, {}, "all")
    assert counts["source"] == {"consolidation": 3}
    assert all(memory_record_facets.matches(s, {"source": ["consolidation"]}) for s in summaries)


def test_a_repeated_listing_reuses_its_pass_until_the_store_changes(
    store, monkeypatch  # noqa: F811
):
    _seed(store)
    passes = []
    real = memory_edit._rows

    def counted(*args, **kwargs):
        if kwargs.get("summaries"):
            passes.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(memory_edit, "_rows", counted)
    first = memory_edit.list_records(store, {}, sort="updated", facets=True)
    again = memory_edit.list_records(store, {"filters": {"category": ["tool"]}}, sort="key")
    assert len(passes) == 1
    assert sorted(_ids(again)) == ["lesson.a", "lesson.b"]
    assert store.set_semantic("user.editor", "vim", 0.9, "user_explicit") is None
    after = memory_edit.list_records(store, {}, sort="updated", facets=True)
    assert len(passes) == 2
    assert after["total"] == first["total"] + 1
    assert "user.editor" in _ids(after)


def test_a_ref_filter_lists_exactly_the_named_records(store):  # noqa: F811
    _seed(store)
    ref = memory_record_facets.record_ref
    wanted = [ref("directive", "lesson.a"), ref("fact", "user.timezone")]
    body = memory_edit.list_records(
        store, {"filters": {"ref": wanted}}, sort="updated", facets=True
    )
    assert sorted(_ids(body)) == ["lesson.a", "user.timezone"]
    assert body["facets"]["kind"] == {"directive": 1, "fact": 1}
    with pytest.raises(memory_edit.MemoryEditError):
        memory_edit.list_records(store, {"filters": {"ref": ["not-a-ref"]}}, sort="updated")


def test_no_page_is_larger_than_one_hundred(store):  # noqa: F811
    _seed(store)
    with pytest.raises(memory_edit.MemoryEditError):
        memory_edit.list_records(store, {}, sort="updated", limit=101)


def test_counts_are_reused_until_the_store_changes(store, monkeypatch):  # noqa: F811
    _seed(store)
    counted = []
    real = memory_record_facets.count_facets
    monkeypatch.setattr(
        memory_record_facets,
        "count_facets",
        lambda *args: counted.append(1) or real(*args),
    )
    tool = {"filters": {"category": ["tool"]}}
    first = memory_edit.list_records(store, tool, sort="updated", facets=True)
    again = memory_edit.list_records(store, tool, sort="key", offset=0, facets=True)
    assert len(counted) == 1 and again["facets"] == first["facets"]
    # Another filter set is counted on its own.
    memory_edit.list_records(store, {}, sort="updated", facets=True)
    assert len(counted) == 2
    _lesson(store, "lesson.d", "Rebase before push", "tool", "always", confidence=0.9)
    after = memory_edit.list_records(store, tool, sort="updated", facets=True)
    assert len(counted) == 3
    assert after["facets"]["category"]["tool"] == first["facets"]["category"]["tool"] + 1
    # The counts of the list the write replaced went with it.
    live = {id(entry[1]) for entry in memory_edit._SUMMARY_CACHE.values()}
    assert all(id(held) in live for held, _counts in memory_edit._FACET_CACHE.values())
