"""The state-import upload spool stages under the data home, with no call on the loop.

On a host whose ``/tmp`` is a tmpfs, a spool written there is held in RAM for the
whole upload (up to the 2 GiB import cap). ``_read_upload_file`` therefore spools
into a per-request directory of ``<data home>/staging``. The data home may be NFS,
so ``part_stream.stream_part_off_loop`` keeps every filesystem call, the create and
the discard included, on one worker thread; and the staging leaf is agent-writable,
so neither the create nor the discard follows a spool name swapped for a link.
"""

from __future__ import annotations

import asyncio
import contextlib
import stat
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.multipart import BodyPartReader

from kiro_crew import pinned_fs, staging
from kiro_crew.dashboard import part_stream
from kiro_crew.dashboard.handlers import portability as ph

#: Ceiling on any wait a test must unblock itself; far above a healthy run, so
#: reaching it means the run is lost rather than slow.
_LOST_RUN_DEADLINE = 30.0


class _Part(BodyPartReader):
    """A ``file`` part that hands out *chunks*, then EOF (or blocks, when asked).

    Subclasses the real reader so the handler's ``isinstance`` gate accepts it;
    ``__init__`` is replaced because no HTTP body backs it.
    """

    name = "file"  # type: ignore[assignment]

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.reading = asyncio.Event()
        self.unblock = asyncio.Event()
        self.block = False

    async def read_chunk(self, size: int = 0) -> bytes:  # type: ignore[override]
        if self._chunks:
            return self._chunks.pop(0)
        if self.block:
            self.reading.set()
            await self.unblock.wait()
        return b""


class _Reader:
    def __init__(self, part: _Part) -> None:
        self._part = part

    async def next(self) -> _Part:
        return self._part


class _Request(dict):
    """The parts of a request the import handlers read."""

    content_type = "multipart/form-data"

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
    return root


def _staged(home: Path) -> list[Path]:
    root = home / staging.STAGING_DIRNAME
    return sorted(root.iterdir()) if root.exists() else []


def test_the_spool_lands_under_staging_not_the_system_temp_dir(home: Path) -> None:
    payload = b"PK\x03\x04" + b"x" * 200_000
    part = _Part([payload[:100_000], payload[100_000:]])

    zip_path, err = asyncio.run(ph._read_upload_file(_Request(part)))  # type: ignore[arg-type]

    assert err is None and zip_path is not None
    assert zip_path.parent.parent == home / staging.STAGING_DIRNAME
    assert not zip_path.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert zip_path.parent.name.startswith("kc_import_")
    assert zip_path.read_bytes() == payload
    if sys.platform != "win32":
        assert stat.S_IMODE(zip_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(zip_path.stat().st_mode) == 0o600

    ph._discard_import_spool(zip_path.parent)
    assert _staged(home) == []


def test_an_oversized_upload_answers_413_with_no_spool_call_on_the_loop(
    home: Path, monkeypatch
) -> None:
    """The create and the discard run on a worker, never the serving loop's thread."""
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    real_open, real_discard = ph._open_import_spool, ph._discard_import_spool

    def _open(spool: Path):
        seen["open"] = threading.get_ident()
        return real_open(spool)

    def _discard(spool: Path) -> None:
        seen["discard"] = threading.get_ident()
        real_discard(spool)

    monkeypatch.setattr(ph, "_open_import_spool", _open)
    monkeypatch.setattr(ph, "_discard_import_spool", _discard)
    monkeypatch.setattr(ph, "_MAX_IMPORT_BYTES", 1024)

    zip_path, err = asyncio.run(
        ph._read_upload_file(_Request(_Part([b"x" * 800, b"x" * 800])))  # type: ignore[arg-type]
    )

    assert zip_path is None and err is not None and err.status == 413
    assert set(seen) == {"open", "discard"}
    assert loop_thread not in seen.values()
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
            done, _ = await asyncio.wait(
                {task, reading}, timeout=_LOST_RUN_DEADLINE, return_when=asyncio.FIRST_COMPLETED
            )
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


def test_a_cancellation_during_the_create_discards_after_it(tmp_path: Path) -> None:
    """The discard is queued behind a create still in flight, so it cannot run first."""
    dest = tmp_path / "spool.bin"
    release = threading.Event()
    order: list[str] = []

    def _discard() -> None:
        order.append("discard")
        dest.unlink(missing_ok=True)

    async def _scenario() -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def _open_sink():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(_LOST_RUN_DEADLINE), "the test never released the create"
            order.append("open")
            return open(dest, "xb")

        task = asyncio.create_task(
            part_stream.stream_part_off_loop(_Part([b"x"]), _open_sink, _discard, max_bytes=8)
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=_LOST_RUN_DEADLINE)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=_LOST_RUN_DEADLINE)
        finally:
            release.set()

    asyncio.run(_scenario())
    assert order == ["open", "discard"]
    assert not dest.exists()


@pytest.mark.skipif(not pinned_fs.supports_pinned_walk(), reason="needs dir_fd calls")
def test_a_spool_name_swapped_for_a_link_is_never_followed(home: Path, tmp_path: Path) -> None:
    """Neither the create nor the discard reaches through a link into another directory."""
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    kept = hidden / ph._SPOOL_FILE
    kept.write_bytes(b"not the spool")
    spool = staging.staging_dir() / "kc_import_swapped"
    spool.symlink_to(hidden, target_is_directory=True)

    with pytest.raises(pinned_fs.PinnedPathRefusal):
        ph._open_import_spool(spool)
    ph._discard_import_spool(spool)

    assert kept.read_bytes() == b"not the spool"
    assert sorted(p.name for p in hidden.iterdir()) == [ph._SPOOL_FILE]


def test_a_handler_removes_the_spool_once_it_is_done(home: Path, monkeypatch) -> None:
    """The preview handler's ``finally`` removes the file AND its directory."""
    monkeypatch.setattr(ph, "_sel", lambda: MagicMock())
    part = _Part([b"not a zip archive at all"])

    resp = asyncio.run(ph.api_portability_preview(_Request(part)))  # type: ignore[arg-type]

    assert resp.status == 200
    assert _staged(home) == []
