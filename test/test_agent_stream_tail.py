"""The sub-agent result stream reads only appended bytes, off the event loop.

``ResultTail`` replaces a whole-file ``read_text`` on every one-second poll. The
text it yields, joined across polls, must equal what ``read_text`` gives for the
final file; it must read only the new bytes; and a file rewritten shorter or
replaced by another file is read again from its start.
"""

from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_handlers_core_coverage_helpers import fake_sel_fixture  # noqa: F401

from kiro_crew import session_workspace_tail
from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.session_workspace_tail import ResultTail


def _append(path: Path, data: bytes) -> None:
    with open(path, "ab") as handle:
        handle.write(data)


def test_appended_text_is_read_once_and_joins_to_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "agent-a1.md"
    tail = ResultTail(path)
    assert tail.read_new() == ""  # not written yet

    pieces = [b"first line\r\n", b"caf\xc3", b"\xa9 \xf0\x9f\x91", b"\xbb\r", b"\nlast\rend"]
    seen = []
    for piece in pieces:
        _append(path, piece)
        seen.append(tail.read_new())
    assert tail.read_new() == ""

    assert "".join(seen) == path.read_text(encoding="utf-8")
    # A character split across two appends is held back, never emitted in halves.
    assert seen[1] == "caf"
    assert seen[2] == "é "


def test_each_read_takes_only_the_new_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "agent-a1.md"
    path.write_bytes(b"x" * 10_000)
    tail = ResultTail(path)
    tail.read_new()

    read_sizes: list[int] = []

    class _CountingFile(io.FileIO):
        def readall(self) -> bytes:
            data = super().readall()
            read_sizes.append(len(data))
            return data

    def _counting_open(file, mode="r", *args, **kwargs):
        assert mode == "rb"
        return io.BufferedReader(_CountingFile(file, "r"))

    monkeypatch.setattr(session_workspace_tail, "open", _counting_open, raising=False)
    _append(path, b"tail")
    assert tail.read_new() == "tail"
    assert read_sizes == [4]


def test_a_file_rewritten_shorter_is_read_from_its_start(tmp_path: Path) -> None:
    path = tmp_path / "agent-a1.md"
    path.write_text("a long partial answer", encoding="utf-8")
    tail = ResultTail(path)
    assert tail.read_new() == "a long partial answer"

    with open(path, "w", encoding="utf-8") as handle:  # write_result's "w" mode
        handle.write("final")
    assert tail.read_new() == "final"


def test_a_replaced_file_is_read_from_its_start(tmp_path: Path) -> None:
    path = tmp_path / "agent-a1.md"
    path.write_text("old", encoding="utf-8")
    tail = ResultTail(path)
    assert tail.read_new() == "old"

    replacement = tmp_path / "agent-a1.md.new"
    replacement.write_text("new and longer", encoding="utf-8")
    os.replace(replacement, path)
    assert tail.read_new() == "new and longer"


def _stream_app(path: Path, monkeypatch: pytest.MonkeyPatch) -> web.Application:
    monkeypatch.setattr("kiro_crew.session_workspace.result_path", lambda *_a: path)
    app = web.Application()
    app["state"] = SimpleNamespace(
        subagents=SimpleNamespace(get=lambda _a: SimpleNamespace(done=True))
    )
    app.router.add_get(
        "/api/sessions/{id}/agents/{agent_id}/stream", core_mod.api_session_agent_stream
    )
    return app


def test_finish_releases_a_held_final_carriage_return(tmp_path: Path) -> None:
    path = tmp_path / "agent-a1.md"
    path.write_bytes(b"answer\r")
    tail = ResultTail(path)
    assert tail.read_new() == "answer"  # the "\r" may still become "\r\n"
    assert tail.finish() == "\n"
    assert "answer\n" == path.read_text(encoding="utf-8")


def test_finish_drops_an_incomplete_character_instead_of_raising(tmp_path: Path) -> None:
    path = tmp_path / "agent-a1.md"
    path.write_bytes(b"caf\xc3")
    tail = ResultTail(path)
    assert tail.read_new() == "caf"
    assert tail.finish() == ""


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("line one\r\ncafé 👻\rend\n", id="ends-in-newline"),
        pytest.param("partial answer\r", id="ends-in-carriage-return"),
    ],
)
@pytest.mark.asyncio
async def test_the_stream_bytes_match_a_whole_file_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_sel, body: str
) -> None:
    path = tmp_path / "agent-a1.md"
    path.write_bytes(body.encode("utf-8"))
    whole = json.dumps(path.read_text(encoding="utf-8"))
    expected = f"data: {whole}\n\nevent: done\ndata: {{}}\n\n"

    async with TestClient(TestServer(_stream_app(path, monkeypatch))) as client:
        resp = await client.get("/api/sessions/s1/agents/a1/stream")
        assert await resp.read() == expected.encode()


@pytest.mark.asyncio
async def test_the_file_is_read_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_sel
) -> None:
    path = tmp_path / "agent-a1.md"
    path.write_text("output", encoding="utf-8")
    loop_thread = threading.get_ident()
    readers: list[int] = []
    real_read_new = ResultTail.read_new

    def _recording_read_new(self: ResultTail) -> str:
        readers.append(threading.get_ident())
        return real_read_new(self)

    monkeypatch.setattr(ResultTail, "read_new", _recording_read_new)
    async with TestClient(TestServer(_stream_app(path, monkeypatch))) as client:
        resp = await client.get("/api/sessions/s1/agents/a1/stream")
        assert "output" in await resp.text()

    assert readers and all(ident != loop_thread for ident in readers)
