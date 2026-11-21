"""The ``start`` / ``end`` line window of ``GET /api/file-read``.

A client showing a few lines around a ``path:line`` reference asks for those
lines and gets a body cut from what the full read would serve -- after the owner
gate, the path validation, the read cap and the credential pass -- under a fixed
cap of 40 lines and 4096 characters. These tests pin the bounds and the clamping,
the 1-based numbering, that the slice is cut from the redacted body, the audit
row's window, that the binary and sensitive-path verdicts are untouched, and that
a read with neither parameter is the read it was.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from redaction_structured_cases import PEM_BLOCK, PEM_BODY, github_classic

from kiro_crew.dashboard.handlers import files
from kiro_crew.dashboard.handlers.files import (
    _FILE_READ_WINDOW_MAX_CHARS,
    _FILE_READ_WINDOW_MAX_LINES,
    _line_window,
    api_file_read,
)


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-read", api_file_read)
    return as_owner(app)


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture
def home_patch(tmp_path):
    """Patch expanduser and realpath so tmp_path is treated as $HOME."""
    real_realpath = os.path.realpath

    def fake_expanduser(p):
        return p.replace("~", str(tmp_path))

    with (
        patch("os.path.expanduser", side_effect=fake_expanduser),
        patch("os.path.realpath", side_effect=real_realpath),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        yield tmp_path


@pytest.fixture
def redaction_on(tmp_path, monkeypatch):
    """The owner's switch ships OFF, which stands the pass down on the owner's own
    view; the slice cases are about the pass, so it is ON here."""
    from kiro_crew.security import redaction_switch

    monkeypatch.setattr(redaction_switch, "_path", lambda: tmp_path / "redaction.json")
    monkeypatch.setattr(redaction_switch, "DEFAULT_REDACTION_ENABLED", True)


def _numbered(tmp_path, count: int, name: str = "lines.txt", width: int = 0):
    """A file of *count* lines ``line 1`` .. ``line N``, each padded to *width*."""
    f = tmp_path / name
    f.write_text("".join(f"line {n}".ljust(width) + "\n" for n in range(1, count + 1)))
    return f


def _success_rows(mock_sel):
    return [
        call.kwargs
        for call in mock_sel.log_tool_invocation.call_args_list
        if call.kwargs.get("outcome") == "success"
    ]


class TestLineWindowHelper:
    def test_lines_are_one_based_and_inclusive(self):
        body, first, last, total, cut = _line_window("a\nb\nc\nd\n", 2, 3)
        assert (body, first, last, total, cut) == ("b\nc\n", 2, 3, 4, False)

    def test_the_body_is_a_substring_of_the_text(self):
        text = "a\r\nb\r\nlast without newline"
        body, first, last, total, cut = _line_window(text, 2, 3)
        assert body == "b\r\nlast without newline"
        assert (first, last, total, cut) == (2, 3, 3, False)
        assert text.find(body) == 3

    def test_a_trailing_newline_does_not_count_as_a_line(self):
        assert _line_window("a\nb\n", 1, 9)[2:4] == (2, 2)
        assert _line_window("a\nb", 1, 9)[2:4] == (2, 2)
        assert _line_window("\n", 1, 9)[2:4] == (1, 1)

    def test_an_empty_text_has_no_lines(self):
        assert _line_window("", 1, 5) == ("", 0, 0, 0, False)

    def test_a_start_past_the_end_is_an_empty_window_not_a_cut(self):
        assert _line_window("a\nb\n", 3, 5) == ("", 0, 0, 2, False)

    def test_clamping_to_the_text_is_not_a_cut(self):
        body, first, last, total, cut = _line_window("a\nb\nc\n", 2, 50)
        assert (body, first, last, total, cut) == ("b\nc\n", 2, 3, 3, False)

    def test_the_line_cap_cuts_and_says_so(self):
        text = "".join(f"{n}\n" for n in range(1, 101))
        body, first, last, total, cut = _line_window(text, 5, 100)
        assert (first, last, total, cut) == (5, 5 + _FILE_READ_WINDOW_MAX_LINES - 1, 100, True)
        assert body == "".join(f"{n}\n" for n in range(5, 5 + _FILE_READ_WINDOW_MAX_LINES))

    def test_the_character_cap_keeps_lines_whole(self):
        # 1000 characters plus the newline: four lines fit under 4096, the fifth
        # would cross it and is dropped whole, never cut.
        text = "".join(("x" * 1000) + "\n" for _ in range(10))
        body, first, last, total, cut = _line_window(text, 1, 10)
        assert (first, last, total, cut) == (1, 4, 10, True)
        assert len(body) == 4 * 1001
        assert body.endswith("x\n")

    def test_a_first_line_over_the_cap_is_cut_rather_than_dropped(self):
        text = ("y" * 10_000) + "\nsecond\n"
        body, first, last, total, cut = _line_window(text, 1, 2)
        assert (first, last, total, cut) == (1, 1, 2, True)
        assert body == "y" * _FILE_READ_WINDOW_MAX_CHARS

    def test_the_cap_is_fixed(self):
        assert (_FILE_READ_WINDOW_MAX_LINES, _FILE_READ_WINDOW_MAX_CHARS) == (40, 4096)


class TestFileReadLineWindow:
    @pytest.mark.asyncio
    async def test_window_returns_the_named_lines_with_their_bounds(
        self, tmp_path, mock_sel, home_patch
    ):
        f = _numbered(tmp_path, 10)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&start=3&end=5")
            assert resp.status == 200
            assert await resp.text() == "line 3\nline 4\nline 5\n"
            assert resp.headers["X-Line-Start"] == "3"
            assert resp.headers["X-Line-End"] == "5"
            assert resp.headers["X-Line-Count"] == "10"
            assert "X-Window-Truncated" not in resp.headers
            assert "X-Truncated" not in resp.headers
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="dashboard",
                tool_name="file_read",
                outcome="success",
                resources=str(f),
                metadata={"line_start": 3, "line_end": 5, "line_total": 10},
            )

    @pytest.mark.asyncio
    async def test_start_alone_opens_a_window_to_the_cap(self, tmp_path, mock_sel, home_patch):
        f = _numbered(tmp_path, 100)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&start=50")
            assert resp.status == 200
            assert resp.headers["X-Line-Start"] == "50"
            assert resp.headers["X-Line-End"] == str(50 + _FILE_READ_WINDOW_MAX_LINES - 1)
            # The default end IS the cap, so nothing the request named was dropped.
            assert "X-Window-Truncated" not in resp.headers

    @pytest.mark.asyncio
    async def test_end_alone_starts_at_line_one(self, tmp_path, mock_sel, home_patch):
        f = _numbered(tmp_path, 10)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&end=2")
            assert resp.status == 200
            assert await resp.text() == "line 1\nline 2\n"
            assert (resp.headers["X-Line-Start"], resp.headers["X-Line-End"]) == ("1", "2")

    @pytest.mark.asyncio
    async def test_window_is_clamped_to_the_file(self, tmp_path, mock_sel, home_patch):
        f = _numbered(tmp_path, 10)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&start=9&end=500")
            assert resp.status == 200
            assert await resp.text() == "line 9\nline 10\n"
            assert resp.headers["X-Line-End"] == "10"
            assert "X-Window-Truncated" not in resp.headers

    @pytest.mark.asyncio
    async def test_window_past_the_last_line_is_empty(self, tmp_path, mock_sel, home_patch):
        f = _numbered(tmp_path, 10)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&start=11")
            assert resp.status == 200
            assert await resp.text() == ""
            assert (resp.headers["X-Line-Start"], resp.headers["X-Line-End"]) == ("0", "0")
            assert resp.headers["X-Line-Count"] == "10"

    @pytest.mark.asyncio
    async def test_window_caps_the_lines_and_says_so(self, tmp_path, mock_sel, home_patch):
        f = _numbered(tmp_path, 200)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&start=1&end=200")
            assert resp.status == 200
            body = await resp.text()
            assert body.count("\n") == _FILE_READ_WINDOW_MAX_LINES
            assert resp.headers["X-Line-End"] == str(_FILE_READ_WINDOW_MAX_LINES)
            assert resp.headers["X-Window-Truncated"] == "true"
            # The read cap was not hit: only the window was cut.
            assert "X-Truncated" not in resp.headers

    @pytest.mark.asyncio
    async def test_window_caps_the_characters_keeping_lines_whole(
        self, tmp_path, mock_sel, home_patch
    ):
        f = _numbered(tmp_path, 10, width=1000)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&start=1&end=10")
            assert resp.status == 200
            body = await resp.text()
            assert len(body) == 4 * 1001 <= _FILE_READ_WINDOW_MAX_CHARS
            assert resp.headers["X-Line-End"] == "4"
            assert resp.headers["X-Window-Truncated"] == "true"

    @pytest.mark.asyncio
    async def test_a_minified_line_is_cut_at_the_cap(self, tmp_path, mock_sel, home_patch):
        f = tmp_path / "bundle.min.js"
        f.write_text(("z" * 100_000) + "\n", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&start=1&end=1")
            assert resp.status == 200
            assert len(await resp.text()) == _FILE_READ_WINDOW_MAX_CHARS
            assert resp.headers["X-Window-Truncated"] == "true"

    @pytest.mark.asyncio
    async def test_window_past_the_read_cap_sees_only_the_capped_body(
        self, tmp_path, mock_sel, home_patch
    ):
        # 300,000 two-character lines: the read cap keeps 512,000 characters, so
        # the body has 256,000 lines, and a window past them is empty while
        # X-Truncated says why the file's later lines are not there.
        f = tmp_path / "huge.txt"
        f.write_text("x\n" * 300_000, encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&start=299000&end=299005")
            assert resp.status == 200
            assert await resp.text() == ""
            assert resp.headers["X-Truncated"] == "true"
            assert resp.headers["X-Line-Count"] == "256000"
            assert (resp.headers["X-Line-Start"], resp.headers["X-Line-End"]) == ("0", "0")

    @pytest.mark.asyncio
    async def test_window_keeps_crlf_lines_as_written(self, tmp_path, mock_sel, home_patch):
        f = tmp_path / "dos.txt"
        f.write_bytes(b"a\r\nb\r\nc\r\n")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&start=2&end=2")
            assert resp.status == 200
            assert await resp.read() == b"b\r\n"

    @pytest.mark.asyncio
    async def test_window_keeps_the_file_content_type(self, tmp_path, mock_sel, home_patch):
        f = tmp_path / "data.json"
        f.write_text('{\n  "a": 1\n}\n', encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&start=2&end=2")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("application/json")
            assert await resp.text() == '  "a": 1\n'

    @pytest.mark.asyncio
    async def test_window_is_cut_from_the_redacted_body(
        self, tmp_path, mock_sel, home_patch, redaction_on
    ):
        # The pass runs on the whole capped body first; the window is a slice of
        # its output. A synthetic token shape, never a real secret.
        key = github_classic(seed=131)
        f = tmp_path / "notes.txt"
        f.write_text(f"first\ntoken = {key}\nthird\n", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&start=2&end=2")
            assert resp.status == 200
            body = await resp.text()
            assert key not in body
            assert body.startswith("token = [REDACTED")
            assert resp.headers["X-Redacted"] == "true"
            assert resp.headers["X-Line-Count"] == "3"

    @pytest.mark.asyncio
    async def test_window_into_a_multi_line_secret_shows_none_of_it(
        self, tmp_path, mock_sel, home_patch, redaction_on
    ):
        # A window that starts inside a private-key block, cut from the file as
        # written, would hold base64 body lines the pass cannot recognise without
        # their header. Cut from the redacted body, the block is already one tag,
        # so no line of the key can be named. Synthetic key material only.
        f = tmp_path / "config.txt"
        f.write_text(f"before\n{PEM_BLOCK}\nafter\n", encoding="utf-8")
        body_lines = PEM_BODY.split("\n")
        async with TestClient(TestServer(_make_app())) as client:
            for start in range(1, 9):
                resp = await client.get(f"/api/file-read?path={f}&start={start}&end={start}")
                assert resp.status == 200
                body = await resp.text()
                assert not any(line in body for line in body_lines), start
                assert resp.headers["X-Redacted"] == "true"

    @pytest.mark.asyncio
    async def test_window_on_a_binary_file_is_the_binary_envelope(
        self, tmp_path, mock_sel, home_patch
    ):
        f = tmp_path / "blob"
        f.write_bytes(b"\x00\x01\x02text\n" * 100)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&start=1&end=3")
            assert resp.status == 200
            assert resp.headers["X-File-Binary"] == "true"
            assert (await resp.json()) == {"binary": True, "content": ""}
            assert "X-Line-Start" not in resp.headers

    @pytest.mark.asyncio
    async def test_window_on_a_sensitive_path_is_still_refused(self, mock_sel, home_patch):
        ssh_dir = home_patch / ".ssh"
        ssh_dir.mkdir()
        key_file = ssh_dir / "id_rsa"
        key_file.write_text("secret\n")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={key_file}&start=1&end=1")
            assert resp.status == 400
            assert _success_rows(mock_sel) == []

    @pytest.mark.asyncio
    async def test_window_on_a_missing_file_is_404(self, tmp_path, mock_sel, home_patch):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={tmp_path}/absent.txt&start=1")
            assert resp.status == 404
            assert resp.headers["X-Path-Kind"] == "missing"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        [
            "start=0",
            "end=0",
            "start=-1",
            "start=+2",
            "start=abc",
            "start=1.5",
            "start=5&end=4",
            "start=",
            "end=1e3",
            "start=12345678901",
        ],
    )
    async def test_a_malformed_window_is_refused_before_the_read(
        self, query, tmp_path, mock_sel, home_patch
    ):
        f = _numbered(tmp_path, 10)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}&{query}")
            assert resp.status == 400
            assert await resp.json() == {"error": "invalid input"}
            mock_sel.log_tool_invocation.assert_called_once_with(
                session_key="dashboard",
                tool_name="file_read",
                outcome="denied",
                resources=str(f),
            )

    @pytest.mark.asyncio
    async def test_head_ignores_the_window(self, tmp_path, mock_sel, home_patch):
        f = _numbered(tmp_path, 10)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.head(f"/api/file-read?path={f}&start=3&end=5")
            assert resp.status == 200
            assert resp.headers["X-Path-Kind"] == "file"
            assert "X-Line-Start" not in resp.headers
            assert await resp.read() == b""

    @pytest.mark.asyncio
    async def test_without_the_parameters_the_read_is_unchanged(
        self, tmp_path, mock_sel, home_patch
    ):
        f = _numbered(tmp_path, 100)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}")
            assert resp.status == 200
            assert await resp.text() == f.read_text()
            assert not any(name.startswith("X-Line-") for name in resp.headers)
            assert "X-Window-Truncated" not in resp.headers
            # The audit row keeps its shape: no metadata key appears.
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="dashboard",
                tool_name="file_read",
                outcome="success",
                resources=str(f),
            )

    def test_the_window_helpers_run_on_the_facade(self):
        # The composition contract: the owner's functions read the facade's
        # globals, so the cap a test rebinds there is the one they see.
        assert files._line_window.__globals__ is vars(files)
        assert files._parse_line_window.__globals__ is vars(files)
