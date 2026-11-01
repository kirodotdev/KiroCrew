"""The SQL episode tag filter matches a whole stored tag, non-ASCII and wildcards included.

``tags`` is stored as ``json.dumps`` of the tag list, so ``café`` is on disk as
``"caf\\u00e9"``, and a raw ``LIKE`` pattern treats ``_`` and ``%`` as wildcards. The
dashboard list (``get_episodic_list``) and the keyword fallback
(``_fts5_episodic_search``) must answer the same as the decoded ``matches_tags``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from member_memory_helpers import env as _member_env
from vector_memory_coverage_helpers import _store

from kiro_crew.vector_memory_runtime import episodic_search

env = _member_env

_TAGGED = (
    ("Rollout notes for the café team", ["café"]),
    ("Rollout notes for the snake case tag", ["my_tag"]),
    ("Rollout notes for the lookalike tag", ["myxtag"]),
    ("Rollout notes for the percent tag", ["100%"]),
    ("Rollout notes for the plain ops tag", ["ops"]),
    ("Rollout notes for the cron tag", ["cron"]),
)

_CASES = [
    (["café"], ["Rollout notes for the café team"]),
    (["CAFÉ"], ["Rollout notes for the café team"]),
    (["my_tag"], ["Rollout notes for the snake case tag"]),
    (["my%"], []),
    (["100%"], ["Rollout notes for the percent tag"]),
    (["ops"], ["Rollout notes for the plain ops tag"]),
    (["cr"], []),
    (["ops", "cron"], ["Rollout notes for the cron tag", "Rollout notes for the plain ops tag"]),
]
_IDS = [
    "unicode",
    "unicode-upper",
    "underscore",
    "percent-wildcard",
    "percent",
    "ascii",
    "prefix",
    "any",
]


@pytest.mark.parametrize("tags,expected", _CASES, ids=_IDS)
@pytest.mark.parametrize("name", ["", "member-alice"], ids=["v1", "v2"])
def test_list_filter_matches_the_whole_stored_tag(env, name, tags, expected):
    tier = env.tiers[name]
    for text, row_tags in _TAGGED:
        assert tier.write_episodic(text, tags=row_tags, defer_embedding=True)
    rows = tier.get_episodic_list(limit=100, tag_filter=tags)
    assert sorted(r["text"] for r in rows) == sorted(expected)


@pytest.mark.parametrize("tags,expected", _CASES, ids=_IDS)
def test_keyword_search_agrees_with_the_decoded_matcher(tmp_path: Path, tags, expected):
    store = _store(tmp_path)
    for text, row_tags in _TAGGED:
        assert store.write_episodic(text, tags=row_tags, defer_embedding=True)
    rows = store._fts5_episodic_search("rollout notes", limit=100, tag_filter=tags)
    assert sorted(r["text"] for r in rows) == sorted(expected)
    every = store._fts5_episodic_search("rollout notes", limit=100)
    decoded = [r["text"] for r in every if episodic_search.matches_tags(r, tags)]
    assert sorted(decoded) == sorted(expected)


def test_the_filter_is_parameterized_and_escapes_its_escape_char():
    sql, params = episodic_search.tag_filter_sql(["a\\b", "x_%"])
    assert sql == "(tags LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"
    assert params == ('%"a\\\\\\\\b"%', '%"x\\_\\%"%')
