"""``session.search_scan_window``: how many recent transcripts a session search reads.

A session search reads only the ``session.search_scan_window`` most recently
changed transcripts, read from config at each search. Every surface that shares
the result (the dashboard search, ``search_chat_history`` and the channel session
picker) says when that window left older sessions out, so an empty result never
reads as "not in history".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import discord_sessions_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from config_loader_helpers import _load_from_dict
from discord_sessions_helpers import _ConversationLog, _dispatcher, _message

from kiro_crew import history, mcp_core
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import (
    DEFAULT_SEARCH_SCAN_WINDOW,
    SEARCH_SCAN_WINDOW_MAX,
    SEARCH_SCAN_WINDOW_MIN,
)
from kiro_crew.dashboard.handlers import api_sessions_search
from kiro_crew.history import ConversationLog
from kiro_crew.history_search import (
    SearchRows,
    configured_search_scan_window,
    search_candidate_cap,
    search_coverage,
    search_coverage_note,
)
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_isolated_home = discord_sessions_helpers._isolated_home


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


def _configure_window(monkeypatch, window: int) -> None:
    """Serve *window* as ``session.search_scan_window``, as the config watcher would."""
    monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", None)
    cfg = SimpleNamespace(session=SimpleNamespace(search_scan_window=window))
    monkeypatch.setattr(live, "snapshot", lambda: cfg)


def _three_sessions(base: Path) -> None:
    _session(base, "oldest", "first", "the platypus plan", 1000)
    _session(base, "middle", "second", "the platypus notes", 2000)
    _session(base, "newest", "third", "the platypus recap", 3000)


class TestTheConfigKey:
    def test_default_bounds_and_clamp(self) -> None:
        assert _load_from_dict({}).session.search_scan_window == DEFAULT_SEARCH_SCAN_WINDOW
        assert DEFAULT_SEARCH_SCAN_WINDOW > 500, "the default must raise the old fixed window"
        low = _load_from_dict({"session": {"search_scan_window": 1}})
        high = _load_from_dict({"session": {"search_scan_window": 10**9}})
        assert low.session.search_scan_window == SEARCH_SCAN_WINDOW_MIN
        assert high.session.search_scan_window == SEARCH_SCAN_WINDOW_MAX
        set_ = _load_from_dict({"session": {"search_scan_window": 7500}})
        assert set_.session.search_scan_window == 7500

    def test_an_unreadable_config_reads_as_the_default(self, monkeypatch) -> None:
        monkeypatch.setattr(live, "snapshot", lambda: None)

        def _boom() -> KiroCrewConfig:
            raise OSError("torn config")

        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_boom))
        assert configured_search_scan_window() == DEFAULT_SEARCH_SCAN_WINDOW

    def test_a_process_without_the_watcher_reads_the_config_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The MCP server has no config watcher, so it reads the file at each search."""
        monkeypatch.setattr(live, "snapshot", lambda: None)
        path = tmp_path / "config.json"
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
        path.write_text(json.dumps({"session": {"search_scan_window": 4321}}), encoding="utf-8")
        assert configured_search_scan_window() == 4321
        path.write_text(json.dumps({"session": {"search_scan_window": 1234}}), encoding="utf-8")
        assert configured_search_scan_window() == 1234


class TestTheWindowIsReadAtEachSearch:
    def test_the_configured_window_bounds_the_search(self, tmp_path: Path, monkeypatch) -> None:
        _three_sessions(tmp_path)
        _configure_window(monkeypatch, 2)
        rows = ConversationLog(base_dir=tmp_path).search_sessions("platypus")
        assert sorted(_keys(rows)) == ["middle", "newest"]
        assert search_coverage(rows) == (2, 3)

    def test_a_config_change_applies_to_the_next_search(self, tmp_path: Path, monkeypatch) -> None:
        _three_sessions(tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        _configure_window(monkeypatch, 2)
        assert "oldest" not in _keys(log.search_sessions("platypus"))
        _configure_window(monkeypatch, 3)
        rows = log.search_sessions("platypus")
        assert "oldest" in _keys(rows)
        assert search_coverage(rows) is None, "a window that reached every session says nothing"

    def test_the_facade_override_wins_over_the_config(self, tmp_path: Path, monkeypatch) -> None:
        _three_sessions(tmp_path)
        _configure_window(monkeypatch, 3)
        monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", 1)
        rows = ConversationLog(base_dir=tmp_path).search_sessions("platypus")
        assert _keys(rows) == ["newest"]
        assert search_coverage(rows) == (1, 3)


_STATELESS_RUNS = ("cron_0939f281_6226e0c6", "cron_0939f281_1fe84cbf", "cron_0939f281_85db7b2c")


def _chats_behind_stateless_runs(base: Path) -> None:
    """Two chats, then three newer stateless cron runs that also match."""
    _session(base, "dashboard_chat-1", "release notes", "the platypus plan", 1000)
    _session(base, "dashboard_chat-2", "notes recap", "the platypus recap", 2000)
    for offset, key in enumerate(_STATELESS_RUNS):
        _session(base, key, "poll", "platypus poll found nothing", 3000 + offset)


class TestStatelessCronRunsTakeNoWindowSlot:
    """A stateless job writes a transcript per fire; those must not crowd chats out."""

    def test_newer_runs_do_not_push_chats_out_of_the_window(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _chats_behind_stateless_runs(tmp_path)
        _configure_window(monkeypatch, 2)
        rows = ConversationLog(base_dir=tmp_path).search_sessions("platypus")
        assert sorted(_keys(rows)) == ["dashboard_chat-1", "dashboard_chat-2"]
        assert search_coverage(rows) is None, "runs are not counted as unsearched sessions"

    def test_persistent_and_agent_sequence_cron_sessions_are_searched(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _session(tmp_path, "cron_0939f281", "reminder", "the platypus reminder", 1000)
        _session(tmp_path, "cron_26ee973e_builder-agent", "builder", "platypus build", 2000)
        _session(tmp_path, _STATELESS_RUNS[0], "poll", "platypus poll", 3000)
        _configure_window(monkeypatch, 2)
        rows = ConversationLog(base_dir=tmp_path).search_sessions("platypus")
        assert sorted(_keys(rows)) == ["cron_0939f281", "cron_26ee973e_builder-agent"]

    def test_a_run_that_opened_its_own_chat_tab_is_searched(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """``new_chat_per_run`` re-tags its run ``tab``: a chat the reader keeps."""
        tab_run, ephemeral_run = _STATELESS_RUNS[:2]
        for offset, (key, tag) in enumerate(((tab_run, "tab"), (ephemeral_run, "ephemeral"))):
            path = tmp_path / f"{key}.jsonl"
            path.write_text(
                json.dumps({"_type": "metadata", "title": "run", "cron_run": tag})
                + "\n"
                + json.dumps({"role": "user", "content": "the platypus reply"})
                + "\n",
                encoding="utf-8",
            )
            os.utime(path, (1000 + offset, 1000 + offset))
        _configure_window(monkeypatch, 2)
        log = ConversationLog(base_dir=tmp_path)
        for _ in range(2):  # the second search answers from the listing caches
            assert _keys(log.search_sessions("platypus")) == [tab_run]

    def test_the_backfill_never_indexes_a_run(self, tmp_path: Path, monkeypatch) -> None:
        _chats_behind_stateless_runs(tmp_path)
        _configure_window(monkeypatch, 3)
        monkeypatch.setattr("kiro_crew.history_search._INDEX_QUIET_WINDOW_SECS", 0.0)
        log = ConversationLog(base_dir=tmp_path)
        projection = log._catalog_projection
        if not projection.search_index.available:
            pytest.skip("no sqlite FTS on this interpreter")
        log.update_metadata(_STATELESS_RUNS[0].replace("_", ":"), {"cron_run": "tab"})
        assert projection.backfill_index(budget_secs=FALLBACK_TIMEOUT_SECS)["remaining"] == 0
        assert projection.search_index.indexed_keys() == {
            "dashboard_chat-1",
            "dashboard_chat-2",
            _STATELESS_RUNS[0],
        }, "a run with its own chat tab is a conversation and is indexed"


class TestStatelessRunKey:
    def test_matches_what_a_stateless_job_mints_and_nothing_else(self) -> None:
        from kiro_crew.cron_service.identity import (
            build_cron_session_context,
            is_stateless_cron_run_key,
        )

        def job(persistent: bool) -> SimpleNamespace:
            return SimpleNamespace(id="0939f281", message="poll", persistent_session=persistent)

        stateless_key, _ = build_cron_session_context(job(persistent=False))  # type: ignore[arg-type]
        persistent_key, _ = build_cron_session_context(job(persistent=True))  # type: ignore[arg-type]
        assert is_stateless_cron_run_key(stateless_key)
        assert is_stateless_cron_run_key(history._safe_key(stateless_key))
        assert not is_stateless_cron_run_key(persistent_key)
        assert not is_stateless_cron_run_key(history._safe_key(persistent_key))
        for other in ("cron:0939f281:builder-agent", "cron_0939f281_6226e0c6x", "dashboard_chat-1"):
            assert not is_stateless_cron_run_key(other), other


class TestCoverage:
    def test_a_title_scoped_search_counts_the_title_matches(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _session(tmp_path, "a", "parity one", "deploy", 1000)
        _session(tmp_path, "b", "parity two", "deploy", 2000)
        _session(tmp_path, "c", "parity three", "deploy", 3000)
        _session(tmp_path, "d", "unrelated", "deploy", 4000)
        _configure_window(monkeypatch, 2)
        rows = ConversationLog(base_dir=tmp_path).search_sessions("title:parity deploy")
        assert _keys(rows) == ["c", "b"]
        assert search_coverage(rows) == (2, 3)

    def test_a_title_scoped_count_leaves_out_private_sessions(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A title count names a topic, so incognito and temporary titles are not counted."""

        def store(name: str, public: dict[str, int]) -> Path:
            base = tmp_path / name
            base.mkdir()
            for i, mode in enumerate(("incognito", "temporary", "incognito")):
                path = base / f"private{i}.jsonl"
                path.write_text(
                    json.dumps({"_type": "metadata", "title": "hidden topic", "memory_mode": mode})
                    + "\n"
                    + json.dumps({"role": "user", "content": "deploy"})
                    + "\n",
                    encoding="utf-8",
                )
                os.utime(path, (1000 + i, 1000 + i))
            for key, mtime in public.items():
                _session(base, key, f"hidden {key}", "deploy", mtime)
            return base

        _configure_window(monkeypatch, 2)

        def coverage(base: Path) -> tuple[int, int] | None:
            return search_coverage(
                ConversationLog(base_dir=base).search_sessions("title:hidden deploy")
            )

        assert coverage(store("private-only", {})) is None
        # Two visible matches fit a window of two, so no note: reporting that a
        # private match pushed one of them out would reveal the private match.
        assert coverage(store("two-public", {"public-old": 500, "public-new": 5000})) is None
        three = {"public-old": 500, "public-mid": 4000, "public-new": 5000}
        assert coverage(store("three-public", three)) == (2, 3)

    def test_a_content_count_leaves_out_private_sessions(self, tmp_path: Path, monkeypatch) -> None:
        """A plain search's counts leave out incognito and temporary sessions.

        A caller that hides those rows compares the note's total with a listing
        that leaves them out, so counting them would tell it how many there are.
        """
        _three_sessions(tmp_path)
        for i, mode in enumerate(("incognito", "temporary")):
            path = tmp_path / f"private{i}.jsonl"
            path.write_text(
                json.dumps({"_type": "metadata", "title": "secret", "memory_mode": mode})
                + "\n"
                + json.dumps({"role": "user", "content": "the platypus secret"})
                + "\n",
                encoding="utf-8",
            )
            os.utime(path, (500 + i, 500 + i))
        _configure_window(monkeypatch, 2)
        rows = ConversationLog(base_dir=tmp_path).search_sessions("platypus")
        assert search_coverage(rows) == (2, 3)

    def test_private_sessions_inside_the_window_do_not_change_the_counts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The counts read the same whether or not private sessions fill the window.

        Otherwise the window's size minus *searched* would count the private
        sessions among the newest ones.
        """

        def store(name: str, *, private: bool) -> ConversationLog:
            # Each store is written whole before its first search.
            base = tmp_path / name
            base.mkdir()
            for i, word in enumerate(("plan", "notes", "recap", "memo", "draft"), start=1):
                _session(base, f"p{i}", f"public {i}", f"the platypus {word}", 1000 * i)
            _session(base, "s1", "secret one", "the platypus aside", 100)
            _session(base, "s2", "secret two", "the platypus note", 200)
            for i, mode in enumerate(("incognito", "temporary") if private else ()):
                path = base / f"private{i}.jsonl"
                path.write_text(
                    json.dumps({"_type": "metadata", "title": "secret", "memory_mode": mode})
                    + "\n"
                    + json.dumps({"role": "user", "content": "the platypus secret"})
                    + "\n",
                    encoding="utf-8",
                )
                os.utime(path, (9000 + i, 9000 + i))
            return ConversationLog(base_dir=base)

        without, with_private = store("without", private=False), store("with", private=True)
        _configure_window(monkeypatch, 3)
        assert search_coverage(without.search_sessions("platypus")) == (3, 7)
        rows = with_private.search_sessions("platypus")
        assert search_coverage(rows) == (3, 7)
        assert "p4" not in _keys(rows), "the private sessions still take their window slots"
        # The private "secret" titles are the newest title matches, yet the title
        # count reads as it does without them.
        _configure_window(monkeypatch, 1)
        assert search_coverage(without.search_sessions("title:secret platypus")) == (1, 2)
        assert search_coverage(with_private.search_sessions("title:secret platypus")) == (1, 2)

    def test_a_search_restricted_to_keys_reports_no_count(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Counting a caller's sessions past the window would judge every transcript."""
        _three_sessions(tmp_path)
        _configure_window(monkeypatch, 2)
        rows = ConversationLog(base_dir=tmp_path).search_sessions(
            "platypus", keys={"oldest", "newest"}
        )
        assert _keys(rows) == ["newest"]
        assert search_coverage(rows) is None

    def test_a_joined_search_keeps_the_counts(self) -> None:
        rows = SearchRows([{"key": "a"}], searched=2, eligible=5)
        copied = rows.copy_rows()
        assert copied == rows and copied[0] is not rows[0]
        assert search_coverage(copied) == (2, 5)

    def test_the_note(self) -> None:
        assert search_coverage_note([{"key": "a"}]) == ""
        assert search_coverage_note(SearchRows([], searched=3, eligible=3)) == ""
        assert search_coverage_note(SearchRows([], searched=2000, eligible=2150)) == (
            "Searched the 2,000 most recent of 2,150 sessions; older sessions were not searched."
        )


def _make_app(log: ConversationLog) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(conversation_log=log)
    app.router.add_get("/api/sessions/search", api_sessions_search)
    return app


@pytest.mark.asyncio
async def test_the_dashboard_search_reports_the_cut(tmp_path: Path, monkeypatch) -> None:
    _three_sessions(tmp_path)
    _configure_window(monkeypatch, 2)
    async with TestClient(TestServer(_make_app(ConversationLog(base_dir=tmp_path)))) as client:
        body = await (await client.get("/api/sessions/search", params={"q": "platypus"})).json()
        assert body["scan"] == {"searched": 2, "total": 3}
        _configure_window(monkeypatch, 3)
        body = await (await client.get("/api/sessions/search", params={"q": "platypus"})).json()
        assert "scan" not in body


@pytest.mark.asyncio
async def test_the_incognito_excluding_search_reports_the_cut(tmp_path: Path, monkeypatch) -> None:
    """A remote crew read (``exclude_incognito``) keeps the scan counts and its rows."""
    _three_sessions(tmp_path)
    _configure_window(monkeypatch, 2)
    async with TestClient(TestServer(_make_app(ConversationLog(base_dir=tmp_path)))) as client:
        params = {"q": "platypus", "exclude_incognito": "1"}
        body = await (await client.get("/api/sessions/search", params=params)).json()
        assert sorted(row["key"] for row in body["sessions"]) == ["middle", "newest"]
        assert body["scan"] == {"searched": 2, "total": 3}


def test_the_candidate_cap_is_the_ceiling_except_for_a_bare_title_query(monkeypatch) -> None:
    _configure_window(monkeypatch, 300)
    assert search_candidate_cap("platypus") == SEARCH_SCAN_WINDOW_MAX
    assert search_candidate_cap("title:plan platypus") == SEARCH_SCAN_WINDOW_MAX
    assert search_candidate_cap("title:plan") == 300


def test_search_chat_history_says_when_older_sessions_were_not_searched(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:global-v1")
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.append("dashboard_chat-1", "user", "the platypus plan")
    log.append("dashboard_chat-2", "user", "unrelated")
    log.append("dashboard_chat-3", "user", "unrelated too")
    for i, key in enumerate(("dashboard_chat-1", "dashboard_chat-2", "dashboard_chat-3")):
        os.utime(log._path(key), (1000 + i, 1000 + i))
    _configure_window(monkeypatch, 2)

    out = mcp_core._call_tool_inner("search_chat_history", {"query": "platypus"})
    assert out == (
        "No matching conversations found. Searched the 2 most recent of 3 sessions; "
        "older sessions were not searched."
    )
    out = mcp_core._call_tool_inner("search_chat_history", {"query": "unrelated"})
    assert "dashboard_chat-2" in out
    assert "_Searched the 2 most recent of 3 sessions; older sessions were not searched._" in out

    _configure_window(monkeypatch, 3)
    out = mcp_core._call_tool_inner("search_chat_history", {"query": "platypus"})
    assert "dashboard_chat-1" in out and "Searched the" not in out


def test_search_chat_history_reads_the_window_once(tmp_path: Path, monkeypatch) -> None:
    """The tool asks for the window's ceiling, so only the backend reads the window.

    Two reads could disagree when the config changes between them, and a smaller
    first read would cap the candidates below what the backend scanned.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:global-v1")
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.append("dashboard_chat-1", "user", "the platypus plan")
    monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", None)
    reads: list[int] = []
    cfg = SimpleNamespace(session=SimpleNamespace(search_scan_window=500))

    def _snapshot() -> SimpleNamespace:
        reads.append(1)
        return cfg

    monkeypatch.setattr(live, "snapshot", _snapshot)
    assert mcp_core._search_history_scan("platypus") == SEARCH_SCAN_WINDOW_MAX
    assert mcp_core._search_history_scan("title:plan platypus") == SEARCH_SCAN_WINDOW_MAX
    assert reads == []
    assert "dashboard_chat-1" in mcp_core._call_tool_inner(
        "search_chat_history", {"query": "platypus"}
    )
    assert reads == [1]


def test_a_title_only_chat_history_search_is_capped_by_the_window(
    tmp_path: Path, monkeypatch
) -> None:
    """A bare ``title:`` search reads titles only, so the window caps its rows."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:global-v1")
    _configure_window(monkeypatch, 2)
    assert mcp_core._search_history_scan("title:plan") == 2
    seen: list[int] = []
    original = ConversationLog.search_sessions

    def _spy(self, query, limit=50, **kwargs):
        seen.append(limit)
        return original(self, query, limit, **kwargs)

    monkeypatch.setattr(ConversationLog, "search_sessions", _spy)
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.append("dashboard_chat-1", "user", "x")
    log.update_metadata("dashboard_chat-1", {"title": "plan one"})
    out = mcp_core._call_tool_inner("search_chat_history", {"query": "title:plan"})
    assert "dashboard_chat-1" in out
    assert seen == [2]


class _CutShortLog(_ConversationLog):
    """A shared search whose scan window left older sessions out."""

    def search_sessions(self, query: str, limit: int = 50) -> list[dict]:
        return SearchRows(super().search_sessions(query, limit), searched=2000, eligible=2150)


def _cut_short_log(*titles: str) -> _CutShortLog:
    return _CutShortLog(
        [
            {"key": f"dashboard_chat-{i}", "title": title, "memory_mode": "persistent"}
            for i, title in enumerate(titles)
        ],
        {f"dashboard:chat-{i}": [] for i in range(len(titles))},
    )


_NOTE = "Searched the 2,000 most recent of 2,150 sessions; older sessions were not searched."


@pytest.mark.asyncio
async def test_the_channel_picker_says_when_older_sessions_were_not_searched() -> None:
    dispatcher, client, _ = _dispatcher({"u1"}, _cut_short_log("Codex compaction"))

    await dispatcher.handle_message(_message("!sessions codex"))
    assert _NOTE in client.sent[-1][0]

    await dispatcher.handle_message(_message("!sessions missing topic"))
    text, components = client.sent[-1]
    assert components is None
    assert "No sessions matched `missing topic`" in text and _NOTE in text


@pytest.mark.asyncio
async def test_the_channel_listing_carries_no_note() -> None:
    """A bare listing is not a search, so no window cut it short."""
    dispatcher, client, _ = _dispatcher({"u1"}, _cut_short_log("One", "Two"))
    await dispatcher.handle_message(_message("!sessions"))
    assert "Searched the" not in client.sent[-1][0]
