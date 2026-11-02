"""The state-import upload spool stages under the data home, never the system temp dir.

On a host whose ``/tmp`` is a tmpfs, a spool written there is held in RAM for the
whole upload (up to the 2 GiB import cap). ``_read_upload_file`` therefore spools
into a per-call subdirectory of ``<data home>/staging``, and every exit path --
success, the size cap, a failed read and a cancelled request -- leaves nothing
behind in that leaf.
"""

from __future__ import annotations

import asyncio
import contextlib
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.multipart import BodyPartReader

from kiro_crew import staging
from kiro_crew.dashboard.handlers import portability as ph

#: Ceiling on any wait the cancellation test must unblock itself; far above a
#: healthy run, so reaching it means the run is lost rather than slow.
_LOST_RUN_DEADLINE = 30.0


class _Part(BodyPartReader):
    """A ``file`` part that hands out *chunks*, then EOF.

    Subclasses the real reader so the handler's ``isinstance`` gate accepts it;
    ``__init__`` is replaced because no HTTP body backs it.
    """

    name = "file"  # type: ignore[assignment]

    def __init__(self, chunks: list[bytes], *, fail: BaseException | None = None) -> None:
        self._chunks = list(chunks)
        self._fail = fail
        self.reading = asyncio.Event()
        self.unblock = asyncio.Event()
        self.block = False

    async def read_chunk(self, size: int = 0) -> bytes:  # type: ignore[override]
        if self._chunks:
            return self._chunks.pop(0)
        if self.block:
            self.reading.set()
            await self.unblock.wait()
        if self._fail is not None:
            raise self._fail
        return b""


class _Reader:
    def __init__(self, part: _Part) -> None:
        self._part = part

    async def next(self) -> _Part:
        return self._part


class _Request(dict):
    """The two things the import handlers read off a request."""

    def __init__(self, part: _Part) -> None:
        super().__init__(user="owner")
        self._part = part

    async def multipart(self) -> _Reader:
        return _Reader(self._part)


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    """A data home under ``tmp_path`` that ``staging_dir`` resolves against."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(staging, "config_dir", lambda: root)
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    return root


def _staged(home: Path) -> list[Path]:
    root = home / staging.STAGING_DIRNAME
    return sorted(root.iterdir()) if root.exists() else []


def test_the_spool_lands_under_staging_not_the_system_temp_dir(home: Path) -> None:
    payload = b"PK\x03\x04" + b"x" * 200_000
    part = _Part([payload[:100_000], payload[100_000:]])

    zip_path, err = asyncio.run(ph._read_upload_file(_Request(part)))  # type: ignore[arg-type]

    assert err is None and zip_path is not None
    staging_root = (home / staging.STAGING_DIRNAME).resolve()
    assert zip_path.resolve().parent.parent == staging_root
    assert not zip_path.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert zip_path.parent.name.startswith("kc_import_")
    assert zip_path.read_bytes() == payload
    if sys.platform != "win32":
        assert stat.S_IMODE(zip_path.parent.stat().st_mode) == 0o700

    ph._discard_import_spool(zip_path)
    assert _staged(home) == []


def test_an_oversized_upload_answers_413_and_leaves_nothing(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(ph, "_MAX_IMPORT_BYTES", 1024)
    part = _Part([b"x" * 800, b"x" * 800])

    zip_path, err = asyncio.run(ph._read_upload_file(_Request(part)))  # type: ignore[arg-type]

    assert zip_path is None and err is not None and err.status == 413
    assert _staged(home) == []


def test_a_failed_read_propagates_and_leaves_nothing(home: Path) -> None:
    part = _Part([b"PK\x03\x04partial"], fail=ConnectionResetError("client went away"))

    with pytest.raises(ConnectionResetError):
        asyncio.run(ph._read_upload_file(_Request(part)))  # type: ignore[arg-type]

    assert _staged(home) == []


def test_a_cancelled_upload_leaves_nothing(home: Path) -> None:
    async def _scenario() -> None:
        part = _Part([b"PK\x03\x04partial"])
        part.block = True
        task = asyncio.create_task(ph._read_upload_file(_Request(part)))  # type: ignore[arg-type]
        try:
            # Bounded: a setup failure ends the task before ``reading`` is set, and
            # waiting on the task too surfaces that failure instead of hanging.
            reading = asyncio.create_task(part.reading.wait())
            done, _ = await asyncio.wait({task, reading}, timeout=_LOST_RUN_DEADLINE)
            if task in done:
                reading.cancel()
                task.result()  # re-raises the setup failure
                pytest.fail("the upload finished before it reached the blocking read")
            assert reading in done, "the upload never reached the blocking read"
            assert len(_staged(home)) == 1  # the spool exists while the upload streams
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=_LOST_RUN_DEADLINE)
        finally:
            part.unblock.set()
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(task, timeout=_LOST_RUN_DEADLINE)

    asyncio.run(_scenario())
    assert _staged(home) == []


def test_a_handler_removes_the_spool_once_it_is_done(home: Path, monkeypatch) -> None:
    """The preview handler's ``finally`` removes the file AND its directory."""
    monkeypatch.setattr(ph, "_sel", lambda: MagicMock())
    part = _Part([b"not a zip archive at all"])

    resp = asyncio.run(ph.api_portability_preview(_Request(part)))  # type: ignore[arg-type]

    assert resp.status == 200
    assert _staged(home) == []


def test_discard_keeps_a_directory_it_did_not_create(tmp_path: Path) -> None:
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    zip_path = foreign / "import.zip"
    zip_path.write_bytes(b"x")

    ph._discard_import_spool(zip_path)

    assert not zip_path.exists()
    assert foreign.is_dir()
