"""``GET /api/project/session-files``: the files one chat session edited."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import session_files
from kiro_crew.dashboard.session_files import (
    _change_paths,
    api_project_session_files,
    newest_first,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


@pytest.fixture(autouse=True)
def local_session(_floor_monkeypatch):
    _floor_monkeypatch.setattr(session_files, "session_runs_remotely", lambda key: False)


class _Slot(_ChatSlot):
    def __init__(self, key: str = "s", project: str = "", app: str = "") -> None:
        super().__init__(key)
        self.key = key
        self.linked_session_key = ""
        self.project = project
        self._app = app


class _State:
    def __init__(self, log: ConversationLog | None, **slots: _Slot) -> None:
        self._slots = dict(slots)
        self.conversation_log = log

    def get_slot(self, key: str):
        return self._slots.get(key)


def _app(state: _State, request_app: str = "") -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/project/session-files", api_project_session_files)
    if request_app:

        @web.middleware
        async def as_app(request, handler):
            request["app"] = request_app
            return await handler(request)

        app.middlewares.append(as_app)
    return app


def _p(*parts: str) -> str:
    """An absolute path on this platform (a drive-less one is not, on Windows)."""
    return os.path.join(os.path.abspath(os.sep), *parts)


def _write_transcript(log: ConversationLog, key: str, rows: list[dict]) -> None:
    path = log._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


_T1, _T2, _T3 = "2026-01-01T00:00:01", "2026-01-01T00:00:02", "2026-01-01T00:00:03"


def _change(ts: str, *paths: str, role: str = "assistant", **extra) -> dict:
    return {
        "role": role,
        "content": "",
        "ts": ts,
        "meta": {"file_changes": [{"path": p, **extra} for p in paths]},
    }


async def _get(state: _State, slot: str = "s", request_app: str = "") -> tuple[int, dict]:
    async with TestClient(TestServer(_app(state, request_app))) as client:
        resp = await client.get("/api/project/session-files", params={"slot": slot})
        return resp.status, await resp.json()


class TestOrdering:
    def test_a_path_changed_twice_is_listed_once_at_its_latest_change(self):
        assert newest_first([("/a", "1"), ("/b", "2"), ("/a", "3")], [], "9") == [
            ("/a", "3"),
            ("/b", "2"),
        ]

    def test_the_running_turn_is_newest(self):
        assert newest_first([("/a", "1"), ("/b", "2")], ["/a"], "9") == [("/a", "9"), ("/b", "2")]

    def test_only_assistant_rows_and_proven_writes_count(self):
        rows = [
            _change("1", "/user", role="user"),
            _change("2", "/unproven", omitted_reason="unverified_write"),
            _change("3", "/ok"),
        ]
        assert list(_change_paths(rows)) == [("/ok", "3")]


class TestRoute:
    @pytest.mark.asyncio
    async def test_lists_the_transcript_newest_first_with_deleted_files_flagged(
        self, tmp_path: Path, mock_sel
    ):
        log = ConversationLog(tmp_path / "history")
        kept = tmp_path / "proj" / "kept.py"
        kept.parent.mkdir()
        kept.write_text("x")
        gone = tmp_path / "proj" / "gone.py"
        _write_transcript(
            log,
            "dashboard:s",
            [_change("2026-01-01T00:00:00", str(gone)), _change("2026-01-02T00:00:00", str(kept))],
        )
        status, body = await _get(_State(log, s=_Slot(project=str(kept.parent))))
        assert status == 200
        assert body == {
            "files": [
                {"path": str(kept), "ts": "2026-01-02T00:00:00", "exists": True},
                {"path": str(gone), "ts": "2026-01-01T00:00:00", "exists": False},
            ],
            "truncated": False,
            "withheld": False,
        }

    @pytest.mark.asyncio
    async def test_the_running_turn_comes_first(self, tmp_path: Path, mock_sel):
        log = ConversationLog(tmp_path / "history")
        _write_transcript(log, "dashboard:s", [_change("2026-01-01T00:00:00", _p("old", "a.py"))])
        slot = _Slot()
        slot._file_changes = [{"path": _p("new", "b.py"), "content": ""}]
        status, body = await _get(_State(log, s=slot))
        assert status == 200
        assert [f["path"] for f in body["files"]] == [_p("new", "b.py"), _p("old", "a.py")]

    @pytest.mark.asyncio
    async def test_a_failed_write_of_the_running_turn_is_not_listed(self, tmp_path: Path, mock_sel):
        slot = _Slot()
        slot._file_changes = [
            {"path": _p("refused.py"), "content": "", "unverified_write": True},
            {"path": _p("written.py"), "content": ""},
        ]
        _, body = await _get(_State(ConversationLog(tmp_path / "history"), s=slot))
        assert [f["path"] for f in body["files"]] == [_p("written.py")]

    @pytest.mark.asyncio
    async def test_edits_in_the_rotated_head_are_listed_before_the_live_ones(
        self, tmp_path: Path, mock_sel, monkeypatch
    ):
        log = ConversationLog(tmp_path / "history")
        _write_transcript(log, "dashboard:s", [_change("2", _p("live.py"))])
        monkeypatch.setattr(
            log, "read_rotated_messages", lambda key: [_change("1", _p("archived.py"))]
        )
        _, body = await _get(_State(log, s=_Slot()))
        assert [f["path"] for f in body["files"]] == [_p("live.py"), _p("archived.py")]

    @pytest.mark.asyncio
    async def test_a_later_members_archived_edit_outranks_an_earlier_members_live_one(
        self, tmp_path: Path, mock_sel, monkeypatch
    ):
        log = ConversationLog(tmp_path / "history")
        _write_transcript(
            log,
            "dashboard:old",
            [_change(_T1, _p("a.py")), _change(_T2, _p("b.py"))],
        )
        _write_transcript(log, "dashboard:s", [])
        monkeypatch.setattr(log, "chained_keys", lambda key: ["dashboard:old", "dashboard:s"])
        archived = {"dashboard:s": [_change(_T3, _p("a.py"))]}
        monkeypatch.setattr(log, "read_rotated_messages", lambda key: archived.get(key, []))
        _, body = await _get(_State(log, s=_Slot()))
        assert [(f["path"], f["ts"]) for f in body["files"]] == [
            (_p("a.py"), _T3),
            (_p("b.py"), _T2),
        ]

    @pytest.mark.asyncio
    async def test_a_row_timestamp_that_is_not_one_is_served_empty(
        self, tmp_path: Path, mock_sel  # noqa: F811
    ):
        # A transcript line is writable by the agent's shell, so its ts can hold
        # any text, a credential included; only a stamp is served.
        log = ConversationLog(tmp_path / "history")
        forged = "AKIA" + "QWERTYUIOPASDFGH"
        _write_transcript(
            log, "dashboard:s", [_change(forged, _p("a.py")), _change(_T1 + "Z", _p("b.py"))]
        )
        _, body = await _get(_State(log, s=_Slot()))
        assert [(f["path"], f["ts"]) for f in body["files"]] == [
            (_p("b.py"), _T1 + "Z"),
            (_p("a.py"), ""),
        ]

    @pytest.mark.asyncio
    async def test_an_unreadable_transcript_is_an_error_not_an_empty_list(
        self, tmp_path: Path, mock_sel, monkeypatch
    ):
        log = ConversationLog(tmp_path / "history")

        def refuse(key):
            raise PermissionError("no access")

        monkeypatch.setattr(log, "read_file_change_messages", refuse)
        status, body = await _get(_State(log, s=_Slot()))
        assert status == 503
        assert body["code"] == "transcript_unreadable"

    @pytest.mark.asyncio
    async def test_a_relative_path_resolves_against_the_project_or_is_dropped(
        self, tmp_path: Path, mock_sel
    ):
        log = ConversationLog(tmp_path / "history")
        _write_transcript(log, "dashboard:s", [_change("1", "rel.py")])
        _write_transcript(log, "dashboard:t", [_change("1", "rel.py")])
        proj = tmp_path / "proj"
        state = _State(log, s=_Slot("s", project=str(proj)), t=_Slot("t"))
        _, with_project = await _get(state, "s")
        assert [f["path"] for f in with_project["files"]] == [str(proj / "rel.py")]
        _, without = await _get(state, "t")
        assert without == {"files": [], "truncated": False, "withheld": True}

    @pytest.mark.asyncio
    async def test_a_sensitive_path_is_withheld(self, tmp_path: Path, mock_sel, monkeypatch):
        log = ConversationLog(tmp_path / "history")
        _write_transcript(log, "dashboard:s", [_change("1", _p("secret", "key"), _p("ok.py"))])
        monkeypatch.setattr(
            session_files, "is_sensitive_path", lambda p: p.startswith(_p("secret"))
        )
        _, body = await _get(_State(log, s=_Slot()))
        assert [f["path"] for f in body["files"]] == [_p("ok.py")]
        assert (body["truncated"], body["withheld"]) == (False, True)

    @pytest.mark.asyncio
    async def test_the_list_is_capped(self, tmp_path: Path, mock_sel, monkeypatch):
        log = ConversationLog(tmp_path / "history")
        monkeypatch.setattr(session_files, "MAX_SESSION_FILES", 2)
        _write_transcript(log, "dashboard:s", [_change("1", _p("a"), _p("b"), _p("c"))])
        _, body = await _get(_State(log, s=_Slot()))
        assert len(body["files"]) == 2
        assert (body["truncated"], body["withheld"]) == (True, False)

    @pytest.mark.asyncio
    async def test_a_remote_session_lists_nothing(self, tmp_path: Path, mock_sel, monkeypatch):
        log = ConversationLog(tmp_path / "history")
        _write_transcript(log, "dashboard:s", [_change("1", _p("a.py"))])
        monkeypatch.setattr(session_files, "session_runs_remotely", lambda key: True)
        _, body = await _get(_State(log, s=_Slot()))
        assert body == {"files": [], "truncated": False, "withheld": False}

    @pytest.mark.asyncio
    async def test_missing_slot_and_missing_param(self, tmp_path: Path, mock_sel):
        state = _State(ConversationLog(tmp_path / "history"))
        status, _ = await _get(state, "nope")
        assert status == 404
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.get("/api/project/session-files")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_an_app_token_reaches_only_a_slot_it_owns(self, tmp_path: Path, mock_sel):
        log = ConversationLog(tmp_path / "history")
        _write_transcript(log, "dashboard:s", [_change("1", _p("a.py"))])
        status, _ = await _get(_State(log, s=_Slot()), request_app="some-app")
        assert status == 404


class TestOmittedFiles:
    @pytest.mark.asyncio
    async def test_a_turn_that_left_files_out_marks_the_list_incomplete(
        self, tmp_path: Path, mock_sel
    ):
        log = ConversationLog(tmp_path / "history")
        row = _change("1", _p("a.py"))
        row["meta"]["file_changes_omitted_files"] = 3
        _write_transcript(log, "dashboard:s", [row])
        _, body = await _get(_State(log, s=_Slot()))
        assert [f["path"] for f in body["files"]] == [_p("a.py")]
        assert (body["truncated"], body["withheld"]) == (False, True)

    @pytest.mark.asyncio
    async def test_a_turn_whose_every_file_was_left_out_still_marks_it(
        self, tmp_path: Path, mock_sel
    ):
        log = ConversationLog(tmp_path / "history")
        row = _change("1")
        row["meta"]["file_changes_omitted_files"] = 2
        _write_transcript(log, "dashboard:s", [row])
        _, body = await _get(_State(log, s=_Slot()))
        assert body == {"files": [], "truncated": False, "withheld": True}


class TestRedactedPaths:
    @pytest.mark.asyncio
    async def test_a_path_recorded_redacted_is_withheld_not_shown_deleted(
        self, tmp_path: Path, mock_sel
    ):
        log = ConversationLog(tmp_path / "history")
        masked = _p("proj", "[REDACTED: credential].txt")
        _write_transcript(log, "dashboard:s", [_change("1", masked, _p("ok.py"))])
        _, body = await _get(_State(log, s=_Slot()))
        assert [f["path"] for f in body["files"]] == [_p("ok.py")]
        assert body["withheld"] is True
