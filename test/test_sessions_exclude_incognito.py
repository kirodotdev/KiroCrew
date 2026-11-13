"""The ``exclude_incognito`` arm of the three session routes a peer serves.

A hub's crew-scoped reads (``/api/crew-sessions/*``) send ``exclude_incognito=1``
to ``GET /api/sessions``, ``GET /api/sessions/search`` and
``GET /api/sessions/{key}`` and require ``INCOGNITO_EXCLUDED_HEADER`` on the
reply. These tests pin the peer half of that contract on a real
``ConversationLog``: restricted sessions are dropped BEFORE the limit, the
verdict is re-judged against the on-disk line under the same lock hold as the
preview or snippet read, a header spelled with capitals is restricted like one
spelled lowercase, the header is set only on the guarded arm, and the
dashboard's own reads (no parameter) are unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_session_detail, api_sessions, api_sessions_search
from kiro_crew.dashboard.handlers.sessions_parts import history_reads
from kiro_crew.history import INCOGNITO_EXCLUDED_HEADER, ConversationLog, TranscriptBusy
from kiro_crew.warm_task_brief_contract import WARM_TASK_BRIEF_RESULT_KIND


def _make_app(log: ConversationLog) -> web.Application:
    state = SimpleNamespace(conversation_log=log, _slots={})
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/sessions", api_sessions)
    app.router.add_get("/api/sessions/search", api_sessions_search)
    app.router.add_get("/api/sessions/{key}", api_session_detail)
    return app


def _three_sessions(log: ConversationLog) -> None:
    """Newest first on a list: ``hidden`` (incognito), then ``shown``, then ``old``."""
    log.append("old", "user", "needle in the oldest")
    log.append("shown", "user", "needle in the shown one")
    log.append("hidden", "user", "needle in the hidden one")
    log.update_metadata("hidden", {"memory_mode": "incognito"})


def _tighten_once_then_lock(log: ConversationLog, key: str):
    """A ``_locked`` stand-in that tightens *key*'s line to incognito the FIRST
    time the route takes that key's lock, then behaves as the real lock. Models a
    writer landing between the metadata scan and the lock-held re-judge; the
    tightening itself runs through the real lock so it cannot recurse."""
    real_locked = log._locked
    fired = {"done": False}

    def _locked(k):
        if k == key and not fired["done"]:
            fired["done"] = True
            log.update_metadata(key, {"memory_mode": "incognito"})
        return real_locked(k)

    return _locked


class TestListExcludesIncognito:
    @pytest.mark.asyncio
    async def test_restricted_rows_are_dropped_before_the_limit(self, tmp_path):
        """``limit=1`` with the newest session restricted still yields the eligible
        one, so a restricted session at the head cannot hide an eligible one."""
        log = ConversationLog(base_dir=tmp_path)
        _three_sessions(log)
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions?limit=1&preview=1&exclude_incognito=1")
            assert resp.status == 200
            assert resp.headers.get(INCOGNITO_EXCLUDED_HEADER) == "1"
            body = await resp.json()
            keys = [s["key"] for s in body["sessions"]]
            assert len(keys) == 1 and keys[0] in {"shown", "old"}
            assert body["total"] == 2
            assert body["has_more"] is True
            assert "hidden" not in await resp.text()

    @pytest.mark.asyncio
    async def test_a_capitalised_mode_is_restricted_too(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("shown", "user", "hello")
        log.append("hidden", "user", "hello")
        log.update_metadata("hidden", {"memory_mode": "Incognito"})
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions?exclude_incognito=1")
            assert [s["key"] for s in (await resp.json())["sessions"]] == ["shown"]

    @pytest.mark.asyncio
    async def test_a_line_tightened_after_the_scan_is_dropped_with_its_preview(
        self, tmp_path, monkeypatch
    ):
        """The race the gate exists for: the metadata scan saw ``persistent``, the
        line turns incognito before the preview read, and the row must go -- the
        on-disk line is re-judged under the lock beside that read."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("shown", "user", "hello")
        log.append("late", "user", "secret said late")
        monkeypatch.setattr(log, "_locked", _tighten_once_then_lock(log, "late"))
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions?preview=1&exclude_incognito=1")
            text = await resp.text()
            assert [s["key"] for s in (await resp.json())["sessions"]] == ["shown"]
            assert "secret said late" not in text

    @pytest.mark.asyncio
    async def test_the_dashboard_read_is_unchanged(self, tmp_path):
        """Without the parameter the History browser still lists a restricted
        session (keeping it is the point) and the reply carries no header."""
        log = ConversationLog(base_dir=tmp_path)
        _three_sessions(log)
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions?preview=1")
            assert INCOGNITO_EXCLUDED_HEADER not in resp.headers
            assert "hidden" in [s["key"] for s in (await resp.json())["sessions"]]


class TestSearchExcludesIncognito:
    @pytest.mark.asyncio
    async def test_restricted_matches_are_dropped_before_the_limit(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _three_sessions(log)
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/search?q=needle&limit=1&exclude_incognito=1")
            assert resp.status == 200
            assert resp.headers.get(INCOGNITO_EXCLUDED_HEADER) == "1"
            keys = [s["key"] for s in (await resp.json())["sessions"]]
            assert len(keys) == 1 and keys[0] != "hidden"
            assert "hidden" not in await resp.text()

    @pytest.mark.asyncio
    async def test_many_restricted_matches_above_cannot_hide_an_eligible_one(self, tmp_path):
        """The ranking is asked for its whole scan window, not ``limit * k``: with
        more restricted matches ranked above the eligible one than any multiple of
        a small limit, the eligible match is still returned."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("shown", "user", "needle once")
        for i in range(25):
            key = f"hidden{i:02d}"
            # More repetitions rank these above ``shown`` on term frequency.
            log.append(key, "user", " ".join(["needle"] * 5))
            log.update_metadata(key, {"memory_mode": "incognito"})
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/search?q=needle&limit=1&exclude_incognito=1")
            assert resp.status == 200
            assert [s["key"] for s in (await resp.json())["sessions"]] == ["shown"]

    @pytest.mark.asyncio
    async def test_the_snippet_is_read_under_the_same_lock_as_the_verdict(
        self, tmp_path, monkeypatch
    ):
        """A match whose line tightens after ranking loses both its row and the
        snippet the ranking had already extracted."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("shown", "user", "needle one")
        log.append("late", "user", "needle secret late")
        monkeypatch.setattr(log, "_locked", _tighten_once_then_lock(log, "late"))
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/search?q=needle&exclude_incognito=1")
            text = await resp.text()
            assert [s["key"] for s in (await resp.json())["sessions"]] == ["shown"]
            assert "secret late" not in text

    @pytest.mark.asyncio
    async def test_the_dashboard_search_is_unchanged(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _three_sessions(log)
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/search?q=needle")
            assert INCOGNITO_EXCLUDED_HEADER not in resp.headers
            assert "hidden" in [s["key"] for s in (await resp.json())["sessions"]]

    @pytest.mark.asyncio
    async def test_a_title_scoped_search_snippets_the_rest_not_the_title(self, tmp_path):
        """The re-read snippet looks for what the ranking searched: the text after a
        ``title:`` term, never the title words as content needles."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("s", "user", "The budget is unchanged. " + "filler words here. " * 40)
        log.append("s", "assistant", "The timeout is now 30 seconds.")
        log.set_title("s", "Retry budget discussion")
        q = 'title:"Retry budget discussion" timeout'
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get(
                "/api/sessions/search", params={"q": q, "exclude_incognito": "1"}
            )
            (row,) = (await resp.json())["sessions"]
            assert "timeout" in row["snippet"]
            assert "budget is unchanged" not in row["snippet"]

    def test_helper_is_the_one_the_route_uses(self):
        """The guarded search arm is a named helper so the lock-held re-judge is
        one function, not a pattern each caller re-types."""
        assert callable(history_reads._search_excluding_incognito)


class TestDetailHeader:
    @pytest.mark.asyncio
    async def test_guarded_read_confirms_and_withholds_restricted(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _three_sessions(log)
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/shown?exclude_incognito=1")
            assert resp.headers.get(INCOGNITO_EXCLUDED_HEADER) == "1"
            assert [m["content"] for m in await resp.json()] == ["needle in the shown one"]
            resp = await client.get("/api/sessions/hidden?exclude_incognito=1")
            assert resp.headers.get(INCOGNITO_EXCLUDED_HEADER) == "1"
            assert await resp.json() == []
            resp = await client.get("/api/sessions/hidden")
            assert INCOGNITO_EXCLUDED_HEADER not in resp.headers
            assert len(await resp.json()) == 1


def _long_session(log: ConversationLog, key: str = "long") -> None:
    """Six rows: three a recall reads, a tool row, a system row and a task brief."""
    log.append(key, "user", "first question")
    log.append(key, "tool", "a tool step")
    log.append(key, "assistant", "first answer")
    log.append(key, "system", "an internal row")
    log.append(key, "assistant", "a task brief", extra_meta={"kind": WARM_TASK_BRIEF_RESULT_KIND})
    log.append(key, "inject", "a note left for later")


class TestDetailCountedRead:
    """``max_messages`` on the guarded arm: the newest recall rows, read from the end."""

    @pytest.mark.asyncio
    async def test_a_counted_read_answers_the_newest_recall_rows(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _long_session(log)
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/long?exclude_incognito=1&max_messages=2")
            assert resp.headers.get(INCOGNITO_EXCLUDED_HEADER) == "1"
            assert await resp.json() == [
                {"role": "assistant", "content": "first answer"},
                {"role": "inject", "content": "a note left for later"},
            ]

    @pytest.mark.asyncio
    async def test_a_counted_read_does_not_read_the_whole_transcript(self, tmp_path, monkeypatch):
        log = ConversationLog(base_dir=tmp_path)
        _long_session(log)
        whole_reads: list[str] = []
        real = log._read_messages
        monkeypatch.setattr(log, "_read_messages", lambda key: whole_reads.append(key) or real(key))
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/long?exclude_incognito=1&max_messages=2")
            assert len(await resp.json()) == 2
            assert whole_reads == []
            # The same read with no count is the whole transcript, as before.
            resp = await client.get("/api/sessions/long?exclude_incognito=1")
            assert len(await resp.json()) == 6
            assert whole_reads == ["long"]

    @pytest.mark.asyncio
    async def test_a_counted_read_of_a_restricted_transcript_is_withheld(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _three_sessions(log)
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/hidden?exclude_incognito=1&max_messages=5")
            assert resp.headers.get(INCOGNITO_EXCLUDED_HEADER) == "1"
            assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_a_counted_read_of_a_busy_transcript_is_retryable(self, tmp_path, monkeypatch):
        log = ConversationLog(base_dir=tmp_path)
        _long_session(log)

        def busy(*_args, **_kwargs):
            raise TranscriptBusy("held by a writer")

        monkeypatch.setattr(log, "derive_recent", busy)
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/long?exclude_incognito=1&max_messages=5")
            assert resp.status == 503
            assert (await resp.json())["code"] == "transcript_busy"

    @pytest.mark.asyncio
    async def test_a_count_past_the_ceiling_is_clamped(self, tmp_path, monkeypatch):
        log = ConversationLog(base_dir=tmp_path)
        _long_session(log)
        asked: list[int] = []
        real = log.derive_recent
        monkeypatch.setattr(
            log,
            "derive_recent",
            lambda key, count, roles: asked.append(count) or real(key, count, roles),
        )
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/long?exclude_incognito=1&max_messages=100000")
            assert resp.status == 200
        assert asked == [history_reads._GUARDED_TAIL_MAX_MESSAGES]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", ["0", "-3", "many", ""])
    async def test_a_count_that_is_not_a_positive_number_reads_the_whole_transcript(
        self, tmp_path, count
    ):
        log = ConversationLog(base_dir=tmp_path)
        _long_session(log)
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get(f"/api/sessions/long?exclude_incognito=1&max_messages={count}")
            assert [m["role"] for m in await resp.json()] == [
                "user",
                "tool",
                "assistant",
                "system",
                "assistant",
                "inject",
            ]

    @pytest.mark.asyncio
    async def test_the_dashboard_read_ignores_the_count(self, tmp_path):
        """Without ``exclude_incognito`` the route is the owner's whole-transcript read."""
        log = ConversationLog(base_dir=tmp_path)
        _long_session(log)
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/long?max_messages=2")
            assert INCOGNITO_EXCLUDED_HEADER not in resp.headers
            assert len(await resp.json()) == 6
