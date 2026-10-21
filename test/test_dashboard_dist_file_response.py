"""The React build's routes are served without the loop's default executor.

``/`` resolves on its own pool, so when other work holds every default worker the
page shell still loads; the build's scripts must too, or the dashboard stays blank.
The remaining tests pin what the response keeps from ``web.FileResponse``.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from aiohttp import ClientError, web
from aiohttp.test_utils import TestClient, TestServer

from conftest import requires_symlinks
from kiro_crew.dashboard import dist_file_response
from kiro_crew.dashboard.dist_file_response import _CHUNK_SIZE
from kiro_crew.dashboard.server import _register_dist_static_routes
from kiro_crew.testing.wait import default_timeout


def _dist_with(tmp_path: Path, name: str, body: bytes) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / name).write_bytes(body)
    return dist


def _with_fractional_mtime(dist: Path, name: str) -> Path:
    """A sub-second mtime, so a date validator echoing ``Last-Modified`` is not exact."""
    stamp = 1_700_000_000.5
    os.utime(dist / "assets" / name, (stamp, stamp))
    return dist


def _app(dist: Path) -> web.Application:
    app = web.Application()
    _register_dist_static_routes(app, dist)
    return app


@pytest.mark.asyncio
async def test_a_script_is_served_while_every_default_worker_is_busy(tmp_path: Path) -> None:
    dist = _dist_with(tmp_path, "main-abc.js", b"export const x = 1\n")
    loop = asyncio.get_running_loop()
    default_pool = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(default_pool)
    release = threading.Event()
    async with TestClient(TestServer(_app(dist))) as client:
        # Parking every default worker IS the condition under test, and `release` is set
        # in `finally`, so a regression fails the bounded `wait_for` below, not a hang.
        busy = [
            loop.run_in_executor(
                None, release.wait
            )  # flake-ok: starving the default pool is the test
            for _ in range(2)
        ]
        try:
            resp = await asyncio.wait_for(
                client.get("/assets/main-abc.js"), timeout=default_timeout()
            )
            assert resp.status == 200
            assert await resp.read() == b"export const x = 1\n"
            miss = await asyncio.wait_for(
                client.get("/assets/missing.js"), timeout=default_timeout()
            )
            assert miss.status == 404
        finally:
            release.set()
            await asyncio.gather(*busy)


@pytest.mark.asyncio
async def test_a_file_larger_than_one_chunk_arrives_whole(tmp_path: Path) -> None:
    body = bytes(range(256)) * ((_CHUNK_SIZE * 2) // 256) + b"tail-bytes"
    async with TestClient(TestServer(_app(_dist_with(tmp_path, "big.js", body)))) as client:
        resp = await client.get("/assets/big.js")
        assert resp.status == 200
        assert resp.content_length == len(body)
        assert await resp.read() == body


@pytest.mark.asyncio
async def test_head_answers_the_length_without_a_body(tmp_path: Path) -> None:
    async with TestClient(TestServer(_app(_dist_with(tmp_path, "a.js", b"12345")))) as client:
        resp = await client.head("/assets/a.js")
        assert resp.status == 200
        assert resp.headers["Content-Length"] == "5"
        assert await resp.read() == b""


@pytest.mark.asyncio
async def test_a_matching_validator_answers_not_modified(tmp_path: Path) -> None:
    async with TestClient(
        TestServer(_app(_with_fractional_mtime(_dist_with(tmp_path, "a.js", b"x"), "a.js")))
    ) as client:
        first = await client.get("/assets/a.js")
        etag = first.headers["ETag"]
        assert first.headers["Last-Modified"]
        again = await client.get("/assets/a.js", headers={"If-None-Match": etag})
        assert again.status == 304
        assert again.headers["ETag"] == etag
        since = await client.get(
            "/assets/a.js", headers={"If-Modified-Since": first.headers["Last-Modified"]}
        )
        assert since.status == 304
        other = await client.get("/assets/a.js", headers={"If-None-Match": '"nope"'})
        assert other.status == 200
        assert await other.read() == b"x"


@pytest.mark.asyncio
async def test_a_failed_precondition_answers_412(tmp_path: Path) -> None:
    async with TestClient(
        TestServer(_app(_with_fractional_mtime(_dist_with(tmp_path, "a.js", b"x"), "a.js")))
    ) as client:
        first = await client.get("/assets/a.js")
        etag = first.headers["ETag"]
        stale = await client.get("/assets/a.js", headers={"If-Match": '"stale"'})
        assert stale.status == 412
        assert (await client.get("/assets/a.js", headers={"If-Match": etag})).status == 200
        weak = await client.get("/assets/a.js", headers={"If-Match": f"W/{etag}"})
        assert weak.status == 412
        before = await client.get(
            "/assets/a.js", headers={"If-Unmodified-Since": "Mon, 01 Jan 2001 00:00:00 GMT"}
        )
        assert before.status == 412
        unchanged = await client.get(
            "/assets/a.js", headers={"If-Unmodified-Since": first.headers["Last-Modified"]}
        )
        assert unchanged.status == 200


@pytest.mark.asyncio
async def test_a_byte_range_is_answered_as_file_response_does(tmp_path: Path) -> None:
    """A video under ``/app-assets`` seeks with ``Range``; WebKit will not play without it."""
    dist = _with_fractional_mtime(_dist_with(tmp_path, "clip.mp4", b"abcdef"), "clip.mp4")
    async with TestClient(TestServer(_app(dist))) as client:
        first = await client.get("/assets/clip.mp4", headers={"Range": "bytes=1-2"})
        assert first.status == 206
        assert first.headers["Content-Range"] == "bytes 1-2/6"
        assert first.headers["Accept-Ranges"] == "bytes"
        assert await first.read() == b"bc"
        cases = {
            "bytes=4-": (206, b"ef", "bytes 4-5/6"),
            "bytes=-2": (206, b"ef", "bytes 4-5/6"),
            "bytes=-100": (206, b"abcdef", "bytes 0-5/6"),
            "bytes=2-99": (206, b"cdef", "bytes 2-5/6"),
        }
        for header, (status, body, content_range) in cases.items():
            resp = await client.get("/assets/clip.mp4", headers={"Range": header})
            assert (resp.status, await resp.read()) == (status, body), header
            assert resp.headers["Content-Range"] == content_range, header
        for header in ("bytes=6-", "bytes=0-1,3-4", "pages=1-2"):
            resp = await client.get("/assets/clip.mp4", headers={"Range": header})
            assert resp.status == 416, header
            assert resp.headers["Content-Range"] == "bytes */6", header
        stale = await client.get(
            "/assets/clip.mp4",
            headers={"Range": "bytes=1-2", "If-Range": "Mon, 01 Jan 2001 00:00:00 GMT"},
        )
        assert (stale.status, await stale.read()) == (200, b"abcdef")
        fresh = await client.get(
            "/assets/clip.mp4",
            headers={"Range": "bytes=1-2", "If-Range": first.headers["Last-Modified"]},
        )
        assert (fresh.status, await fresh.read()) == (206, b"bc")


@pytest.mark.asyncio
async def test_brotli_is_preferred_and_the_type_is_the_original_files(tmp_path: Path) -> None:
    dist = _dist_with(tmp_path, "a.js", b"plain")
    (dist / "assets" / "a.js.br").write_bytes(b"br-bytes")
    (dist / "assets" / "a.js.gz").write_bytes(gzip.compress(b"plain"))
    async with TestClient(TestServer(_app(dist)), auto_decompress=False) as client:
        resp = await client.get("/assets/a.js", headers={"Accept-Encoding": "gzip, br"})
        assert resp.headers["Content-Encoding"] == "br"
        assert resp.headers["Vary"] == "Accept-Encoding"
        assert resp.content_type == "text/javascript"
        assert await resp.read() == b"br-bytes"
        plain = await client.get("/assets/a.js", headers={"Accept-Encoding": "identity"})
        assert "Content-Encoding" not in plain.headers
        assert await plain.read() == b"plain"


@requires_symlinks
@pytest.mark.asyncio
async def test_a_linked_sibling_is_not_served(tmp_path: Path) -> None:
    dist = _dist_with(tmp_path, "a.js", b"plain")
    (tmp_path / "secret.gz").write_bytes(b"outside the build")
    os.symlink(tmp_path / "secret.gz", dist / "assets" / "a.js.gz")
    async with TestClient(TestServer(_app(dist)), auto_decompress=False) as client:
        resp = await client.get("/assets/a.js", headers={"Accept-Encoding": "gzip"})
        assert resp.status == 200
        assert "Content-Encoding" not in resp.headers
        assert await resp.read() == b"plain"


def test_a_sibling_swapped_after_its_check_is_not_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opened sibling must be the file ``lstat`` approved, not one put there since."""
    dist = _dist_with(tmp_path, "a.js", b"plain")
    sibling = dist / "assets" / "a.js.gz"
    sibling.write_bytes(b"opened-bytes")
    checked = tmp_path / "checked.gz"
    checked.write_bytes(b"checked-bytes")
    real_lstat = Path.lstat

    def _lstat_of_another_file(self: Path) -> os.stat_result:
        return real_lstat(checked) if self == sibling else real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _lstat_of_another_file)
    fobj, _st, encoding = dist_file_response._open_variant(dist / "assets" / "a.js", "gzip")
    try:
        assert encoding is None
        assert fobj.read() == b"plain"
    finally:
        fobj.close()


@pytest.mark.asyncio
async def test_a_file_that_shrinks_while_sent_closes_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short body cannot leave the connection open for a next response to be misread."""
    dist = _dist_with(tmp_path, "a.js", b"short")
    real_open = dist_file_response._open_variant

    def _declares_more_than_it_holds(path: Path, accept: str):  # type: ignore[no-untyped-def]
        opened = real_open(path, accept)
        with open(path, "r+b") as same_inode:  # truncated after the size was read
            same_inode.truncate(2)
        return opened

    monkeypatch.setattr(dist_file_response, "_open_variant", _declares_more_than_it_holds)
    async with TestClient(TestServer(_app(dist))) as client:
        resp = await client.get("/assets/a.js")
        assert resp.status == 200
        # Bounded: an open connection would leave the read waiting for bytes that never come.
        with pytest.raises(ClientError):
            await asyncio.wait_for(resp.read(), timeout=default_timeout())


@pytest.mark.asyncio
async def test_a_bundled_feature_video_answers_a_range_with_206(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "app-assets" / "feature-videos").mkdir(parents=True)
    (dist / "app-assets" / "feature-videos" / "feature-tips.mp4").write_bytes(b"0123456789")
    async with TestClient(TestServer(_app(dist))) as client:
        resp = await client.get(
            "/app-assets/feature-videos/feature-tips.mp4", headers={"Range": "bytes=2-5"}
        )
        assert resp.status == 206
        assert resp.headers["Content-Range"] == "bytes 2-5/10"
        assert resp.content_type == "video/mp4"
        assert await resp.read() == b"2345"


def test_the_content_type_survives_an_aiohttp_without_its_own_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aiohttp 3.9, inside the declared floor, has no ``CONTENT_TYPES``: the stdlib guesses."""
    monkeypatch.setattr(dist_file_response, "_AIOHTTP_CONTENT_TYPES", None)
    # The stdlib also reads the host's mime.types, which may spell it either way.
    guessed = dist_file_response._guess_type(Path("main-abc.js"))
    assert guessed in {"text/javascript", "application/javascript"}
    assert dist_file_response._guess_type(Path("blob.unknown-ext")) is None
