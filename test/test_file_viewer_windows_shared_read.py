"""The file viewer reads a file another Windows program holds open.

Word and Excel hold every open document with delete access, and the CRT open (``os.open``
and the builtin ``open``) grants no delete sharing, so a CRT open of a document open in
either is a sharing violation. On Windows the viewer's routes open through
``_open_rb_nofollow`` and ``/api/file-watch`` reads through
``platform_compat.open_file_for_shared_read``, which grants read, write and delete
sharing, so the document reads as when it is closed; a holder that shares no reading
answers 503 ``file_busy`` with one warning that names the cause.

NTFS share modes do not exist on Linux or macOS, so the tests stand them in. While
``_open_rb_nofollow`` or the file handlers' ``open_file_for_shared_read`` runs, and only
then, the process reads as Windows (no ``O_NOFOLLOW``, ``IS_POSIX`` false, a fake
``kernel32`` and ``msvcrt``), and every open of a held file is judged by the NTFS share
rule: refused when the holder does not share reading, or holds an access the opener does
not share. A CRT open (``os.open``, or the builtin ``open`` a file handler calls) carries
the CRT's read and write sharing and fails as CPython does on Windows,
``PermissionError`` with no ``winerror``; ``CreateFileW`` carries the caller's share mode
and fails with last error 32.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import errno
import json
import os
import types
from collections.abc import Iterator
from ctypes import wintypes
from typing import Callable, NamedTuple
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from ooxml_fixtures import docx_para, write_docx

from kiro_crew import platform_compat as pc
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers import files
from kiro_crew.testing.wait import async_wait_until

#: The FILE_SHARE_* bits; a holder's access uses the same bits for read, write and delete.
_READ, _WRITE, _DELETE = 0x1, 0x2, 0x4
#: What the CRT's ``_wopen`` shares: read and write, never delete.
_CRT_SHARE = _READ | _WRITE
_ERROR_SHARING_VIOLATION = 32


class _Holder(NamedTuple):
    """Another program's handle on a file: what it opened for and what it shares."""

    access: int
    share: int


#: As reported from a Windows 11 box: Word and Excel hold the open document with delete
#: access; a program that shares nothing refuses every other open.
_WORD = _Holder(access=_READ | _WRITE | _DELETE, share=_READ)
_EXCEL = _Holder(access=_READ | _WRITE | _DELETE, share=_READ | _WRITE)
_SHARES_NOTHING = _Holder(access=_READ | _WRITE, share=0)


def _refuses(holder: _Holder, share: int) -> bool:
    """The NTFS share rule for an opener that reads with *share*."""
    return not holder.share & _READ or bool(holder.access & ~share)


class _WindowsOpens:
    """The CRT and Win32 opens of a Windows box where *held* files are held."""

    def __init__(self, held: dict[str, _Holder]) -> None:
        self.held = held
        self.share_modes: list[int] = []
        #: Every held path an open was refused on, in order.
        self.refused: list[str] = []
        self.last_error = 0
        self._handles: dict[int, str] = {}
        self._real_open = os.open

    def _crt_refuses(self, path) -> bool:
        holder = self.held.get(os.fspath(path))
        if holder is not None and _refuses(holder, _CRT_SHARE):
            self.refused.append(os.fspath(path))
            return True
        return False

    def crt_open(self, path, flags, *args, **kwargs):
        if self._crt_refuses(path):
            raise PermissionError(errno.EACCES, "Permission denied", path)
        return self._real_open(path, flags, *args, **kwargs)

    def crt_builtin_open(self, file, *args, **kwargs):
        """The builtin ``open``, which on Windows opens a name with the CRT too."""
        if not isinstance(file, int) and self._crt_refuses(file):
            raise PermissionError(errno.EACCES, "Permission denied", file)
        return builtins.open(file, *args, **kwargs)

    def create_file(self, path, _access, share, _security, _disposition, _flags, _template):
        self.share_modes.append(share)
        holder = self.held.get(path)
        if holder is not None and _refuses(holder, share):
            self.refused.append(path)
            self.last_error = _ERROR_SHARING_VIOLATION
            return wintypes.HANDLE(-1).value
        handle = 0x100 + len(self.share_modes)
        self._handles[handle] = path
        return handle

    def open_osfhandle(self, handle, _flags):
        return self._real_open(self._handles.pop(handle), os.O_RDONLY)

    @staticmethod
    def win_error(code):
        exc = PermissionError(errno.EACCES, "The file is being used by another process")
        exc.winerror = code
        return exc

    @contextlib.contextmanager
    def windows(self) -> Iterator[None]:
        """The process as on Windows, its opens this box's CRT and Win32 opens."""
        kernel32 = types.SimpleNamespace(
            CreateFileW=lambda *a: self.create_file(*a), CloseHandle=lambda _h: True
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.delattr(os, "O_NOFOLLOW", raising=False)
            mp.setattr(os, "open", self.crt_open)
            mp.setattr(pc, "IS_POSIX", False)
            mp.setattr(pc, "IS_WINDOWS", True)
            mp.setattr(pc.ctypes, "WinDLL", lambda *_a, **_k: kernel32, raising=False)
            mp.setattr(pc.ctypes, "get_last_error", lambda: self.last_error, raising=False)
            mp.setattr(pc.ctypes, "WinError", self.win_error, raising=False)
            mp.setattr(
                pc,
                "msvcrt",
                types.SimpleNamespace(open_osfhandle=self.open_osfhandle),
                raising=False,
            )
            yield


class _HandlersPlatformCompat:
    """``platform_compat`` as the file handlers read it, its shared read run on Windows."""

    def __init__(self, windows: Callable[[], contextlib.AbstractContextManager[None]]) -> None:
        self._windows = windows

    def __getattr__(self, name: str):
        return getattr(pc, name)

    def open_file_for_shared_read(self, path: str) -> int:
        with self._windows():
            return pc.open_file_for_shared_read(path)


@pytest.fixture
def windows_opens(monkeypatch) -> Callable[[dict[str, _Holder]], _WindowsOpens]:
    """Run the file handlers' opens as on Windows, with the given files held."""

    real = files._open_rb_nofollow

    def install(held: dict[str, _Holder]) -> _WindowsOpens:
        opens = _WindowsOpens(held)

        def on_windows(path: str) -> int:
            with opens.windows():
                return real(path)

        monkeypatch.setattr(files, "_open_rb_nofollow", on_windows)
        monkeypatch.setattr(files, "platform_compat", _HandlersPlatformCompat(opens.windows))
        monkeypatch.setattr(files, "open", opens.crt_builtin_open, raising=False)
        return opens

    return install


@pytest.fixture
def mock_sel():
    with (
        patch("kiro_crew.sel.sel") as m,
        patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=False),
    ):
        instance = MagicMock()
        m.return_value = instance
        yield instance


_ROUTES: dict[str, Callable] = {
    "/api/file-read": handlers.api_file_read,
    "/api/file-raw": handlers.api_file_raw,
    "/api/file-download": handlers.api_file_download,
    "/api/file-stream": handlers.api_file_stream,
    "/api/file-sheet": handlers.api_file_sheet,
    "/api/file-office-preview": handlers.api_file_office_preview,
    "/api/file-office-slides": handlers.api_file_office_slides,
}


async def _get(route: str, path: str) -> tuple[int, bytes]:
    app = web.Application()
    app.router.add_get(route, _ROUTES[route])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=path):
        async with TestClient(TestServer(as_owner(app))) as client:
            resp = await client.get(route, params={"path": path})
            return resp.status, await resp.read()


def test_the_windows_open_shares_read_write_and_delete(tmp_path, windows_opens):
    doc = tmp_path / "report.docx"
    doc.write_bytes(b"PK\x03\x04 document")
    opens = windows_opens({})

    fd = files._open_rb_nofollow(str(doc))

    with os.fdopen(fd, "rb") as fh:
        assert fh.read() == b"PK\x03\x04 document"
    assert opens.share_modes == [_READ | _WRITE | _DELETE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "name", "holder"),
    [
        ("/api/file-read", "report.docx", _WORD),
        ("/api/file-office-preview", "report.docx", _WORD),
        ("/api/file-download", "budget.xlsx", _EXCEL),
    ],
)
async def test_a_document_open_in_office_reads_as_when_closed(
    tmp_path, mock_sel, windows_opens, route, name, holder
):
    path = tmp_path / name
    write_docx(str(path), docx_para("Quarterly figures"))
    windows_opens({})
    closed = await _get(route, str(path))
    assert closed[0] == 200

    windows_opens({str(path): holder})

    assert await _get(route, str(path)) == closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "name"),
    [
        ("/api/file-read", "notes.csv"),
        ("/api/file-raw", "chart.png"),
        ("/api/file-download", "notes.csv"),
        ("/api/file-stream", "talk.mp4"),
        ("/api/file-sheet", "budget.xlsx"),
        ("/api/file-office-preview", "report.docx"),
        ("/api/file-office-slides", "deck.pptx"),
    ],
)
async def test_a_holder_that_shares_nothing_answers_busy_and_names_the_cause(
    tmp_path, mock_sel, windows_opens, caplog, route, name
):
    path = tmp_path / name
    path.write_bytes(b"held")
    windows_opens({str(path): _SHARES_NOTHING})

    with caplog.at_level("WARNING"):
        status, body = await _get(route, str(path))

    assert status == 503, body
    assert json.loads(body) == {"error": files._FILE_BUSY_ERROR, "code": "file_busy"}
    assert any("another program has the file open" in r.getMessage() for r in caplog.records)
    outcomes = [c.kwargs.get("outcome") for c in mock_sel.log_tool_invocation.call_args_list]
    assert outcomes == ["failure"]


async def _first_watch_frame(path: str, opens: _WindowsOpens) -> str:
    """The ``content`` of the first frame ``/api/file-watch`` sends for *path*.

    It waits for that frame or a refused open of the file, whichever comes first: a
    refused read sends no frame, and the stream reads again only when the file changes.
    """
    app = web.Application()
    app.router.add_get("/api/file-watch", handlers.api_file_watch)
    buf = bytearray()
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=path):
        async with TestClient(TestServer(as_owner(app))) as client:
            resp = await client.get("/api/file-watch", params={"path": path})
            assert resp.status == 200

            async def pump() -> None:
                async for chunk in resp.content.iter_any():
                    buf.extend(chunk)

            reader = asyncio.create_task(pump())
            try:
                await async_wait_until(
                    lambda: b"\n\n" in buf or opens.refused,
                    describe=lambda: (bytes(buf), opens.refused),
                )
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
                resp.close()
    assert not opens.refused, f"the watch read was refused on {opens.refused}"
    event = bytes(buf).partition(b"\n\n")[0].decode()
    return json.loads(event.removeprefix("data: "))["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "holder"), [("report.docx", _WORD), ("budget.xlsx", _EXCEL)])
async def test_the_file_watch_streams_a_document_open_in_office_as_when_closed(
    tmp_path, mock_sel, windows_opens, name, holder
):
    # The watch reads the resolved path, so the held name is the resolved one.
    path = os.path.realpath(tmp_path / name)
    write_docx(path, docx_para("Quarterly figures"))
    closed = await _first_watch_frame(path, windows_opens({}))
    assert closed

    opens = windows_opens({path: holder})

    assert await _first_watch_frame(path, opens) == closed
    assert opens.share_modes == [_READ | _WRITE | _DELETE]
