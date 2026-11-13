"""``GET /api/project/session-file-diff``: the texts of one edit this chat made.

The app, the fake slot and state, and the transcript writer are
``test_session_files.py``'s, imported so its fixtures register here too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_session_files import (  # noqa: F401
    _change,
    _p,
    _Slot,
    _State,
    _write_transcript,
    local_session,
    mock_sel,
)

from kiro_crew.dashboard import session_files
from kiro_crew.dashboard.session_files import (
    api_project_session_file_diff,
    change_pair,
    latest_change,
)
from kiro_crew.history import ConversationLog


async def _diff(
    state: _State, path: str, slot: str = "s", request_app: str = ""
) -> tuple[int, dict]:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/project/session-file-diff", api_project_session_file_diff)
    if request_app:

        @web.middleware
        async def as_app(request, handler):
            request["app"] = request_app
            return await handler(request)

        app.middlewares.append(as_app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/project/session-file-diff", params={"slot": slot, "path": path}
        )
        return resp.status, await resp.json()


class TestPure:
    def test_the_newest_change_to_the_file_wins(self):
        a = _p("proj", "a.py")
        rows = [
            _change("1", a, before="v0", after="v1"),
            _change("2", _p("proj", "b.py"), before="", after="b"),
            _change("3", "a.py", before="v1", after="v2"),
        ]
        assert latest_change(rows, a, _p("proj"))["after"] == "v2"

    def test_an_unproven_write_or_a_user_row_is_not_a_change(self):
        a = _p("a.py")
        rows = [
            _change("1", a, before="v0", after="v1"),
            _change("2", a, before="?", after="?", omitted_reason="unverified_write"),
            _change("3", a, role="user", before="x", after="y"),
        ]
        assert latest_change(rows, a, "")["after"] == "v1"

    def test_a_cut_or_dropped_snapshot_is_no_pair(self):
        whole = {"before_fragment": False}
        assert change_pair({"before": "a", "after": "b", **whole}) == ("a", "b")
        assert change_pair({"before": "", "after": "new", **whole}) == ("", "new")
        assert change_pair({"before": "a", "after": "b", "truncated": True, **whole}) is None
        cut = {"before": "", "after": "", "content_omitted": True, **whole}
        assert change_pair(cut) is None
        assert change_pair({"before": "", "after": "", **whole}) is None

    def test_a_fragment_before_is_no_pair(self):
        # The before is the replaced span, not the file: diffing it against the
        # whole after would show nearly the whole file as this chat's addition.
        change = {"before": "old line\n", "after": "a\nnew line\nb\n", "before_fragment": True}
        assert change_pair(change) is None

    def test_an_entry_from_before_the_mark_is_no_pair(self):
        # Written before the turn runner recorded whether a before is whole, so
        # it may be a span; the tab shows the working-tree diff instead.
        assert change_pair({"before": "a", "after": "b"}) is None


class TestRoute:
    @pytest.mark.asyncio
    async def test_stored_texts_are_redacted_when_served(
        self, tmp_path: Path, mock_sel  # noqa: F811
    ):
        # A transcript line is writable by the agent's own shell, so a stored
        # text redacted at capture may not be redacted any more when read.
        log = ConversationLog(tmp_path / "history")
        target = _p("proj", "config.py")
        key = "AKIA" + "QWERTYUIOPASDFGH"
        _write_transcript(
            log,
            "dashboard:s",
            [
                _change(
                    "1",
                    target,
                    before=f"old = '{key}'\n",
                    after=f"new = '{key}'\n",
                    before_fragment=False,
                )
            ],
        )
        status, body = await _diff(_State(log, s=_Slot()), target)
        assert status == 200 and body["available"] is True
        assert key not in body["before"] and key not in body["after"]
        assert "[REDACTED" in body["before"] and "[REDACTED" in body["after"]

    @pytest.mark.asyncio
    async def test_a_fragment_before_is_unavailable(self, tmp_path: Path, mock_sel):  # noqa: F811
        log = ConversationLog(tmp_path / "history")
        target = _p("proj", "a.py")
        _write_transcript(
            log,
            "dashboard:s",
            [_change("1", target, before="x\n", after="a\ny\nb\n", before_fragment=True)],
        )
        status, body = await _diff(_State(log, s=_Slot()), target)
        assert status == 200
        assert body["available"] is False and body["before"] == body["after"] == ""

    @pytest.mark.asyncio
    async def test_returns_the_recorded_texts_not_the_disk(
        self, tmp_path: Path, mock_sel  # noqa: F811
    ):
        log = ConversationLog(tmp_path / "history")
        target = tmp_path / "notes.md"
        # Outside any repository and committed since: only the record knows the edit.
        target.write_text("changed again on disk")
        _write_transcript(
            log,
            "dashboard:s",
            [_change("1", str(target), before="a\n", after="b\n", before_fragment=False)],
        )
        status, body = await _diff(_State(log, s=_Slot()), str(target))
        assert status == 200
        assert body == {"path": str(target), "available": True, "before": "a\n", "after": "b\n"}

    @pytest.mark.asyncio
    async def test_an_edit_only_in_the_rotated_head_is_found_with_its_texts(
        self, tmp_path: Path, mock_sel, monkeypatch  # noqa: F811
    ):
        log = ConversationLog(tmp_path / "history")
        target = _p("proj", "old.py")
        _write_transcript(log, "dashboard:s", [_change("2", _p("proj", "live.py"))])
        monkeypatch.setattr(
            log,
            "read_rotated_messages",
            lambda key: [_change("1", target, before="a", after="b", before_fragment=False)],
        )
        status, body = await _diff(_State(log, s=_Slot()), target)
        assert status == 200
        assert (body["available"], body["before"], body["after"]) == (True, "a", "b")

    @pytest.mark.asyncio
    async def test_an_app_token_reaches_only_a_slot_it_owns(
        self, tmp_path: Path, mock_sel  # noqa: F811
    ):
        log = ConversationLog(tmp_path / "history")
        target = _p("a.py")
        _write_transcript(log, "dashboard:s", [_change("1", target, before="a", after="b")])
        status, body = await _diff(_State(log, s=_Slot()), target, request_app="some-app")
        assert status == 404
        assert "before" not in body

    @pytest.mark.asyncio
    async def test_a_dropped_snapshot_is_unavailable(self, tmp_path: Path, mock_sel):  # noqa: F811
        log = ConversationLog(tmp_path / "history")
        target = _p("proj", "big.bin")
        _write_transcript(
            log,
            "dashboard:s",
            [_change("1", target, before="", after="", truncated=True, content_omitted=True)],
        )
        status, body = await _diff(_State(log, s=_Slot()), target)
        assert status == 200
        assert body["available"] is False and body["before"] == body["after"] == ""

    @pytest.mark.asyncio
    async def test_the_running_turn_still_changing_the_file_is_unavailable(
        self, tmp_path: Path, mock_sel  # noqa: F811
    ):
        log = ConversationLog(tmp_path / "history")
        target = _p("proj", "a.py")
        _write_transcript(log, "dashboard:s", [_change("1", target, before="a", after="b")])
        slot = _Slot()
        slot._file_changes = [{"path": target}]
        status, body = await _diff(_State(log, s=slot), target)
        assert status == 200
        assert body["available"] is False

    @pytest.mark.asyncio
    async def test_a_file_this_chat_did_not_edit_is_not_found(
        self, tmp_path: Path, mock_sel  # noqa: F811
    ):
        log = ConversationLog(tmp_path / "history")
        _write_transcript(log, "dashboard:s", [_change("1", _p("a.py"), before="a", after="b")])
        status, body = await _diff(_State(log, s=_Slot()), _p("other.py"))
        assert status == 404
        assert body["code"] == "not_edited"

    @pytest.mark.asyncio
    async def test_what_the_list_withholds_is_not_found(
        self, tmp_path: Path, mock_sel, monkeypatch  # noqa: F811
    ):
        log = ConversationLog(tmp_path / "history")
        secret = _p("proj", "creds.env")
        _write_transcript(log, "dashboard:s", [_change("1", secret, before="a", after="b")])
        monkeypatch.setattr(session_files, "is_sensitive_path", lambda p: p == secret)
        status, body = await _diff(_State(log, s=_Slot()), secret)
        assert status == 404
        status, body = await _diff(_State(log, s=_Slot()), _p("proj", "[REDACTED]"))
        assert status == 404

    @pytest.mark.asyncio
    async def test_a_remote_session_shows_nothing(
        self, tmp_path: Path, mock_sel, monkeypatch  # noqa: F811
    ):
        log = ConversationLog(tmp_path / "history")
        target = _p("a.py")
        _write_transcript(log, "dashboard:s", [_change("1", target, before="a", after="b")])
        monkeypatch.setattr(session_files, "session_runs_remotely", lambda key: True)
        status, _ = await _diff(_State(log, s=_Slot()), target)
        assert status == 404

    @pytest.mark.asyncio
    async def test_a_relative_or_missing_path_and_a_missing_slot(
        self, tmp_path: Path, mock_sel  # noqa: F811
    ):
        state = _State(ConversationLog(tmp_path / "history"), s=_Slot())
        assert (await _diff(state, "a.py"))[0] == 400
        assert (await _diff(state, ""))[0] == 400
        # Never expanded: a `~user` would be an account lookup on the event loop.
        assert (await _diff(state, "~someone/a.py"))[0] == 400
        assert (await _diff(state, _p("a.py"), slot=""))[0] == 400
        assert (await _diff(state, _p("a.py"), slot="nope"))[0] == 404

    @pytest.mark.asyncio
    async def test_an_unreadable_transcript_is_an_error(
        self, tmp_path: Path, mock_sel, monkeypatch  # noqa: F811
    ):
        log = ConversationLog(tmp_path / "history")

        def broken(key: str) -> list[dict]:
            raise OSError("unreadable")

        monkeypatch.setattr(log, "read_messages", broken)
        status, body = await _diff(_State(log, s=_Slot()), _p("a.py"))
        assert status == 503
        assert body["code"] == "transcript_unreadable"
