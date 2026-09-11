"""Memory list search filters the selected store before cutting a result page."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew.dashboard.handlers import memory

env = _member_env


@pytest.mark.parametrize("name", ["", "member-alice"], ids=["v1", "v2"])
def test_semantic_search_finds_later_pages_and_offsets_only_live_matches(env, name):
    tier = env.tiers[name]
    for index in range(120):
        assert (
            tier.set_semantic(f"pref.a{index:03d}", "ordinary entry", 1.0, "user_explicit") is None
        )
    for suffix in ("first", "removed", "third"):
        assert tier.set_semantic(f"pref.z{suffix}", "needle", 1.0, "user_explicit") is None
    tier.delete_semantic("pref.zremoved", "user_explicit")
    env.tiers["member-bob"].set_semantic("pref.foreign", "needle", 1.0, "user_explicit")
    assert all(not row["key"].startswith("pref.z") for row in tier.get_all_semantic(limit=100))
    assert [row["key"] for row in tier.get_all_semantic(limit=100, q="needle")] == [
        "pref.zfirst",
        "pref.zthird",
    ]
    assert [row["key"] for row in tier.get_all_semantic(limit=1, offset=1, q="needle")] == [
        "pref.zthird"
    ]


@pytest.mark.parametrize("name", ["", "member-alice"], ids=["v1", "v2"])
def test_episode_search_filters_before_pagination_and_intersects_tags(env, name):
    tier = env.tiers[name]
    for day in (1, 2, 3):
        with patch("kiro_crew.vector_memory._now_iso", return_value=f"2020-01-0{day}T00:00:00Z"):
            assert tier.write_episodic(
                f"Needle matched episode {day}", tags=["selected"], defer_embedding=True
            )
    removed = tier.get_episodic_list()[1]["id"]
    tier.delete_episodic(removed)
    with patch("kiro_crew.vector_memory._now_iso", return_value="2021-01-01T00:00:00Z"):
        for index in range(120):
            assert tier.write_episodic(
                f"Unrelated background entry {index}", tags=["selected"], defer_embedding=True
            )
        assert tier.write_episodic("Needle with another tag", tags=["other"], defer_embedding=True)
    env.tiers["member-bob"].write_episodic(
        "Needle from another member", tags=["selected"], defer_embedding=True
    )
    assert all("matched" not in row["text"] for row in tier.get_episodic_list(limit=100))
    rows = tier.get_episodic_list(limit=100, q="needle", tag_filter=["selected"])
    assert [row["text"] for row in rows] == ["Needle matched episode 3", "Needle matched episode 1"]
    assert (
        tier.get_episodic_list(limit=1, offset=1, q="needle", tag_filter=["selected"])[0]["text"]
        == "Needle matched episode 1"
    )


@pytest.mark.parametrize("name", ["", "member-alice"], ids=["v1", "v2"])
@pytest.mark.parametrize("query", ["上海", "STRASSE", "ABC", '"literal"', "false"])
def test_semantic_search_decodes_nested_json_and_normalizes_unicode(env, name, query):
    tier = env.tiers[name]
    value = {"nested": [{"label": '上海 Straße ＡＢＣ says "literal"', "flag": False}]}
    assert tier.set_semantic("pref.nested", value, 1.0, "user_explicit") is None
    assert "\\u4e0a" in tier.get_semantic("pref.nested")["value_json"]
    assert [row["key"] for row in tier.get_all_semantic(q=query)] == ["pref.nested"]


@pytest.mark.parametrize("query", ["%", "_"])
def test_search_treats_sql_wildcards_as_literal_text(env, query):
    tier = env.tiers["member-alice"]
    tier.set_semantic("pref.literal", "100% snake_case", 1.0, "user_explicit")
    tier.set_semantic("pref.other", "Complete plain coverage", 1.0, "user_explicit")
    tier.write_episodic("Stored 100% snake_case coverage", defer_embedding=True)
    tier.write_episodic("Stored complete plain coverage", defer_embedding=True)
    assert [row["key"] for row in tier.get_all_semantic(q=query)] == ["pref.literal"]
    assert [row["text"] for row in tier.get_episodic_list(q=query)] == [
        "Stored 100% snake_case coverage"
    ]
    assert tier.get_all_semantic(q="' OR 1=1 --") == []
    assert tier.get_episodic_list(q="' OR 1=1 --") == []


def test_episode_search_normalizes_text_and_decoded_unicode_tags(env):
    tier = env.tiers["member-alice"]
    tier.write_episodic("Reviewed the Straße deployment", tags=["上海"], defer_embedding=True)
    for query in ("STRASSE", "上海"):
        assert len(tier.get_episodic_list(q=query)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "member-alice"], ids=["v1", "v2"])
@pytest.mark.parametrize("kind", ["semantic", "episodic"])
async def test_http_search_reaches_selected_store_and_preserves_call_shape_without_q(
    env, kind, name
):
    tier = env.tiers[name]
    store_name = name or "default"
    if kind == "semantic":
        tier.set_semantic("pref.alpha", "not selected", 1.0, "user_explicit")
        tier.set_semantic("pref.beta", "上海 needle", 1.0, "user_explicit")
        method = "get_all_semantic"
        handler = memory.api_memory_semantic
        expected = {"limit": 1000, "offset": 0}
    else:
        tier.write_episodic("上海 needle selected result", defer_embedding=True)
        tier.write_episodic("An unrelated historical result", defer_embedding=True)
        method = "get_episodic_list"
        handler = memory.api_memory_episodic_list
        expected = {"limit": 50, "offset": 0, "tag_filter": None}
    response = await handler(
        request(env, owner=True, query={"store": store_name, "q": "上海", "limit": "1"})
    )
    assert response.status == 200
    entries = json.loads(response.text)["entries"]
    assert len(entries) == 1
    if kind == "semantic":
        assert entries[0]["key"] == "pref.beta"
    else:
        assert "上海" in entries[0]["text"]
    if name:
        assert "derived_from" in entries[0]
    with patch.object(tier, method, wraps=getattr(tier, method)) as read:
        response = await handler(request(env, owner=True, query={"store": store_name}))
    assert response.status == 200
    read.assert_called_once_with(**expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler,method",
    [
        (memory.api_memory_semantic, "get_all_semantic"),
        (memory.api_memory_episodic_list, "get_episodic_list"),
    ],
)
async def test_http_query_bound_is_enforced_without_reading_rows(env, handler, method):
    tier = env.tiers["member-alice"]
    with patch.object(tier, method, wraps=getattr(tier, method)) as read:
        refused = await handler(
            request(env, owner=True, query={"store": "member-alice", "q": "x" * 2001})
        )
        assert refused.status == 400
        assert json.loads(refused.text)["code"] == "invalid_memory_query"
        read.assert_not_called()
        accepted = await handler(
            request(env, owner=True, query={"store": "member-alice", "q": "x" * 2000})
        )
        assert accepted.status == 200


@pytest.mark.asyncio
async def test_query_does_not_allow_an_internal_session_to_read_another_member(env):
    env.tiers["member-bob"].set_semantic("pref.secret", "needle", 1.0, "user_explicit")
    response = await memory.api_memory_semantic(
        request(env, internal=True, query={"store": "member-bob", "q": "needle"})
    )
    assert response.status == 403
    assert "entries" not in json.loads(response.text)
