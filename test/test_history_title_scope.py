"""Tests for the ``title:`` scope of session search.

Covers :func:`kiro_crew.history_search_query.split_title_scope`, the prefix parse,
and what ``ConversationLog.search_sessions`` does with it: a title-only query is
answered from titles alone (no transcript read, not held to the scan window), and
text after the term is searched as usual among the sessions whose title matches.
The dashboard route passes the query through unchanged, which the last test pins.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import history, mcp_core
from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.dashboard.handlers import api_sessions_search
from kiro_crew.history import ConversationLog
from kiro_crew.history_search import SessionCatalogProjection
from kiro_crew.history_search_query import TitleScope, split_title_scope


def _session(base: Path, key: str, title: str, content: str, mtime: int) -> None:
    """One transcript titled *title* holding one user message, modified at *mtime*."""
    path = base / f"{key}.jsonl"
    path.write_text(
        json.dumps({"_type": "metadata", "title": title})
        + "\n"
        + json.dumps({"role": "user", "content": content})
        + "\n",
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))


def _keys(rows: list[dict]) -> list[str]:
    return [row["key"] for row in rows]


class TestSplitTitleScope:
    @pytest.mark.parametrize("query", ["parity", "deploy title:parity", "titles:parity", "title"])
    def test_no_leading_prefix_is_no_scope(self, query: str) -> None:
        assert split_title_scope(query) is None

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("title:Parity", TitleScope("parity", False, "")),
            ("TITLE:parity deploy  now", TitleScope("parity", False, "deploy  now")),
            ("  title: parity", TitleScope("parity", False, "")),
            ('title:"Wave  Plan" notes', TitleScope("wave plan", True, "notes")),
            ('title:"wave pl', TitleScope("wave pl", True, "")),
            ("title:", TitleScope("", False, "")),
            ('title:""', TitleScope("", True, "")),
        ],
    )
    def test_splits_the_term_from_the_rest(self, query: str, expected: TitleScope) -> None:
        assert split_title_scope(query) == expected


class TestTitleOnlySearch:
    def test_matches_titles_and_never_transcript_text(self, tmp_path: Path) -> None:
        _session(tmp_path, "titled", "Parity sweep", "nothing relevant", 1000)
        _session(tmp_path, "mention", "Other work", "we touched parity in passing", 2000)
        plain = ConversationLog(base_dir=tmp_path).search_sessions("parity")
        assert sorted(_keys(plain)) == ["mention", "titled"]
        assert _keys(ConversationLog(base_dir=tmp_path).search_sessions("title:parity")) == [
            "titled"
        ]

    def test_rows_are_newest_first_and_carry_no_snippet(self, tmp_path: Path) -> None:
        _session(tmp_path, "older", "parity one", "parity parity parity", 1000)
        _session(tmp_path, "newer", "parity two", "x", 2000)
        rows = ConversationLog(base_dir=tmp_path).search_sessions("Title:PARITY")
        assert _keys(rows) == ["newer", "older"]
        assert all("snippet" not in row for row in rows)

    def test_a_quoted_term_must_appear_as_typed(self, tmp_path: Path) -> None:
        _session(tmp_path, "phrase", "Wave plan review", "x", 2000)
        _session(tmp_path, "apart", "Plan the wave", "x", 1000)
        log = ConversationLog(base_dir=tmp_path)
        assert _keys(log.search_sessions('title:"wave plan"')) == ["phrase"]
        assert _keys(log.search_sessions("title:wave")) == ["phrase", "apart"]

    def test_the_term_uses_the_content_gate_spelling_rules(self, tmp_path: Path) -> None:
        """A forge reference in the term is digit-bounded, as in a plain search."""
        _session(tmp_path, "ref", "Review #4411", "x", 2000)
        _session(tmp_path, "longer", "Review #44110", "x", 1000)
        assert _keys(ConversationLog(base_dir=tmp_path).search_sessions("title:#4411")) == ["ref"]

    @pytest.mark.parametrize("query", ["title:", "title:  ", 'title:""', 'title:"" parity'])
    def test_a_prefix_naming_no_title_matches_nothing(self, tmp_path: Path, query: str) -> None:
        _session(tmp_path, "titled", "parity", "parity", 1000)
        assert ConversationLog(base_dir=tmp_path).search_sessions(query) == []

    def test_reads_no_transcript_content(self, tmp_path: Path, monkeypatch) -> None:
        _session(tmp_path, "titled", "parity sweep", "x", 1000)

        def _refuse(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("a title-only search must not scan transcript content")

        monkeypatch.setattr(SessionCatalogProjection, "_index_shortlist", _refuse)
        monkeypatch.setattr(ConversationLog, "_content_snippet", _refuse)
        assert _keys(ConversationLog(base_dir=tmp_path).search_sessions("title:parity")) == [
            "titled"
        ]

    def test_is_not_held_to_the_scan_window(self, tmp_path: Path, monkeypatch) -> None:
        _session(tmp_path, "old", "parity sweep", "parity notes", 1000)
        _session(tmp_path, "new", "unrelated", "x", 2000)
        monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", 1)
        log = ConversationLog(base_dir=tmp_path)
        assert _keys(log.search_sessions("parity")) == []
        assert _keys(log.search_sessions("title:parity")) == ["old"]
        assert _keys(log.search_sessions("title:parity notes")) == ["old"]

    def test_keys_restrict_the_rows(self, tmp_path: Path) -> None:
        _session(tmp_path, "mine", "parity one", "x", 1000)
        _session(tmp_path, "theirs", "parity two", "x", 2000)
        rows = ConversationLog(base_dir=tmp_path).search_sessions("title:parity", keys={"mine"})
        assert _keys(rows) == ["mine"]

    def test_respects_limit(self, tmp_path: Path) -> None:
        for i in range(4):
            _session(tmp_path, f"s{i}", f"parity {i}", "x", 1000 + i)
        rows = ConversationLog(base_dir=tmp_path).search_sessions("title:parity", limit=2)
        assert _keys(rows) == ["s3", "s2"]


class TestTitleScopedContentSearch:
    def test_the_rest_is_searched_among_title_matches(self, tmp_path: Path) -> None:
        _session(tmp_path, "both", "parity sweep", "the deploy failed", 1000)
        _session(tmp_path, "title-only", "parity notes", "nothing here", 2000)
        _session(tmp_path, "content-only", "misc", "the deploy failed", 3000)
        rows = ConversationLog(base_dir=tmp_path).search_sessions("title:parity deploy")
        assert _keys(rows) == ["both"]
        assert "deploy" in rows[0]["snippet"]

    def test_the_rest_also_matches_the_title(self, tmp_path: Path) -> None:
        """The rest is an ordinary query, so a title hit for it counts too."""
        _session(tmp_path, "titled", "parity deploy plan", "x", 1000)
        rows = ConversationLog(base_dir=tmp_path).search_sessions("title:parity deploy")
        assert _keys(rows) == ["titled"]

    def test_the_scan_window_counts_title_matches(self, tmp_path: Path, monkeypatch) -> None:
        """The window is the newest title matches, so the cap still bounds transcript reads."""
        _session(tmp_path, "older", "parity one", "deploy", 1000)
        _session(tmp_path, "newer", "parity two", "deploy", 2000)
        _session(tmp_path, "newest", "unrelated", "deploy", 3000)
        monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", 1)
        rows = ConversationLog(base_dir=tmp_path).search_sessions("title:parity deploy")
        assert _keys(rows) == ["newer"]

    def test_a_quoted_term_with_rest(self, tmp_path: Path) -> None:
        _session(tmp_path, "phrase", "wave plan", "rollout steps", 1000)
        _session(tmp_path, "apart", "plan the wave", "rollout steps", 2000)
        rows = ConversationLog(base_dir=tmp_path).search_sessions('title:"wave plan" rollout')
        assert _keys(rows) == ["phrase"]


@pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")
def test_a_title_scoped_search_keeps_other_sessions_count_memos(tmp_path: Path) -> None:
    """The content-memo prune runs only for a search that sees every window row.

    The prune keeps the entries whose digest the index shortlist vouches for, so
    a search whose window the title narrowed must not run it: it would evict the
    memo entries of every session whose title did not match.
    """
    for key, title in (("a", "parity one"), ("b", "other"), ("c", "misc")):
        # Distinct text per session: the memo is keyed by content digest.
        _session(tmp_path, key, title, f"deploy notes for {key}", 1000)
    log = ConversationLog(base_dir=tmp_path)
    for key in ("a", "b", "c"):
        assert log._catalog_projection.index_session(key)
    memo = log._catalog_projection._count_memo
    assert memo is not None
    log.search_sessions("deploy")
    entries = memo.stats()["entries"]
    assert entries >= 3, "precondition: every session's count is memoized"
    # Budget pressure, the only state in which the prune acts.
    memo._refused_since_prune = 1

    assert _keys(log.search_sessions("title:parity deploy")) == ["a"]

    assert memo.stats()["entries"] == entries
    assert memo.refused_since_prune() == 1, "the narrowed search left the prune undone"


def test_the_chat_history_tool_excerpts_the_text_after_the_term(
    tmp_path: Path, monkeypatch
) -> None:
    """``search_chat_history`` excerpts what the transcript was searched for.

    The term's characters are not part of the content search, so an earlier
    message that merely repeats the title's words must not take the excerpt from
    the message holding the text after the term.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:global-v1")
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.append("dashboard_chat-1", "user", "修复 the cache first")
    log.append("dashboard_chat-1", "assistant", "the deploy failed on step two")
    log.update_metadata("dashboard_chat-1", {"title": "修复 sweep"})
    log.append("dashboard_chat-2", "user", "the deploy failed on step two")
    out = mcp_core._call_tool_inner("search_chat_history", {"query": "title:修复 deploy"})
    assert "dashboard_chat-1" in out and "dashboard_chat-2" not in out
    assert "<<<deploy>>>" in out


def _make_app(log: ConversationLog) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(conversation_log=log)
    app.router.add_get("/api/sessions/search", api_sessions_search)
    return app


@pytest.mark.asyncio
async def test_the_search_route_passes_the_scope_through(tmp_path: Path) -> None:
    _session(tmp_path, "titled", "Parity sweep", "x", 1000)
    _session(tmp_path, "mention", "Other", "parity in passing", 2000)
    async with TestClient(TestServer(_make_app(ConversationLog(base_dir=tmp_path)))) as client:
        resp = await client.get("/api/sessions/search", params={"q": "title:parity"})
        assert resp.status == 200
        assert _keys((await resp.json())["sessions"]) == ["titled"]
