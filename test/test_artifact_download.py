"""``/api/artifact-downloads`` hands a browser-built file back from a real URL.

An Android in-app browser cannot save a ``blob:`` download, so the artifact page
POSTs the bytes it would have saved and downloads them from the returned URL.
These tests pin that the file comes back byte for byte as an attachment that
never runs as same-origin script, that the stash stays bounded, and that both
routes sit behind dashboard auth.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.constants import ARTIFACT_MAX_CONTENT_BYTES
from kiro_crew.dashboard.handlers import artifact_download
from kiro_crew.dashboard.token_auth import token_auth_middleware


@pytest.fixture(autouse=True)
def _empty_stash() -> Iterator[None]:
    with artifact_download._lock:
        artifact_download._stash.clear()
    yield
    with artifact_download._lock:
        artifact_download._stash.clear()


def _app(*, auth: bool = False) -> web.Application:
    app = web.Application(middlewares=[token_auth_middleware()] if auth else [])
    artifact_download.register_artifact_download_routes(app)
    return app


async def _roundtrip(payload: Any) -> tuple[int, Any, list[tuple[int, dict[str, str], bytes]]]:
    """POST ``payload``, then GET the returned URL twice."""
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/artifact-downloads", json=payload)
        data = await resp.json()
        gets = []
        if resp.status == 200:
            for _ in range(2):
                got = await client.get(data["url"])
                gets.append((got.status, dict(got.headers), await got.read()))
        return resp.status, data, gets


def test_html_comes_back_byte_for_byte_as_a_sandboxed_attachment() -> None:
    html = "<meta http-equiv=Content-Security-Policy content=\"script-src 'none'\"><b>hé</b>"
    status, data, gets = asyncio.run(
        _roundtrip({"filename": "CR Queue-v2.html", "type": "text/html", "content": html})
    )

    assert status == 200
    assert data["url"].startswith("/api/artifact-downloads/")
    got_status, headers, body = gets[0]
    assert got_status == 200
    assert body == html.encode("utf-8")
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Content-Disposition"] == (
        "attachment; filename=\"CR Queue-v2.html\"; filename*=UTF-8''CR%20Queue-v2.html"
    )
    assert headers["Content-Security-Policy"] == "sandbox"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "private, no-store"


def test_the_url_stays_readable_for_the_host_apps_second_fetch() -> None:
    # A WebView aborts its own fetch of a download and the host app fetches the
    # URL again, so a single-use URL would hand the host app a 404.
    _status, _data, gets = asyncio.run(
        _roundtrip({"filename": "a.md", "type": "text/plain", "content": "x"})
    )

    assert [g[0] for g in gets] == [200, 200]


def test_a_name_at_the_artifact_name_cap_still_fits_with_its_suffix() -> None:
    name = "a" * 200 + "-v12.md"
    status, _data, gets = asyncio.run(
        _roundtrip({"filename": name, "type": "text/plain", "content": "x"})
    )

    assert status == 200
    assert gets[0][0] == 200


def test_a_non_ascii_name_keeps_a_plain_ascii_fallback() -> None:
    _status, _data, gets = asyncio.run(
        _roundtrip({"filename": 'rép"ort.md', "type": "text/plain", "content": "x"})
    )

    disposition = gets[0][1]["Content-Disposition"]
    assert disposition.startswith('attachment; filename="r_p_ort.md";')
    assert "filename*=UTF-8''r%C3%A9p%22ort.md" in disposition


_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def test_base64_text_keeps_its_bytes_and_drops_the_forced_charset() -> None:
    # A legacy svg in Latin-1: decoding it as UTF-8 would corrupt the file.
    latin1 = '<?xml version="1.0" encoding="ISO-8859-1"?><svg>caf\xe9</svg>'.encode("latin-1")
    payload = {
        "filename": "old.svg",
        "type": "image/svg+xml",
        "content": base64.b64encode(latin1).decode("ascii"),
        "encoding": "base64",
    }
    status, _data, gets = asyncio.run(_roundtrip(payload))

    assert status == 200
    _got, headers, body = gets[0]
    assert body == latin1
    assert headers["Content-Type"] == "image/svg+xml"
    assert headers["Content-Security-Policy"] == "sandbox"
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_a_png_arrives_base64_and_comes_back_byte_for_byte() -> None:
    payload = {
        "filename": "share.png",
        "type": "image/png",
        "content": base64.b64encode(_PNG).decode("ascii"),
    }
    status, _data, gets = asyncio.run(_roundtrip(payload))

    assert status == 200
    got_status, headers, body = gets[0]
    assert got_status == 200
    assert body == _PNG
    assert headers["Content-Type"] == "image/png"
    assert headers["Content-Disposition"].startswith('attachment; filename="share.png"')
    assert headers["Content-Security-Policy"] == "sandbox"
    assert headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    "content",
    [
        "not base64!",
        # Non-ASCII: b64decode raises ValueError, not binascii.Error.
        "\u00e9",
        # Valid base64 that is not a png: nothing else may ride the png type.
        base64.b64encode(b"<html><script>x</script>").decode("ascii"),
    ],
)
def test_a_png_that_is_not_one_is_refused(content: str) -> None:
    status, _data, gets = asyncio.run(
        _roundtrip({"filename": "a.png", "type": "image/png", "content": content})
    )

    assert status == 400
    assert gets == []


@pytest.mark.parametrize(
    "payload",
    [
        {"filename": "a.html", "type": "application/x-sh", "content": "x"},
        {"filename": "", "type": "text/plain", "content": "x"},
        {"filename": "../a.txt", "type": "text/plain", "content": "x"},
        {"filename": "a.txt", "type": "text/plain"},
        ["not", "an", "object"],
    ],
)
def test_bad_requests_are_refused(payload: Any) -> None:
    status, _data, gets = asyncio.run(_roundtrip(payload))

    assert status == 400
    assert gets == []


def test_the_largest_stored_artifact_fits_within_the_dashboard_body_limit() -> None:
    # Any artifact the store keeps must be downloadable, and the request that
    # carries it must still fit under the dashboard app's own 60 MiB body limit.
    assert artifact_download._MAX_BYTES > ARTIFACT_MAX_CONTENT_BYTES
    assert artifact_download._MAX_TOTAL_BYTES >= artifact_download._MAX_BYTES
    assert artifact_download._MAX_REQUEST_BYTES < 60 * 1024 * 1024


def test_a_file_over_the_cap_is_413(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_download, "_MAX_BYTES", 4)
    status, data, gets = asyncio.run(
        _roundtrip({"filename": "a.txt", "type": "text/plain", "content": "12345"})
    )

    assert status == 413
    assert data["code"] == "too_large"
    assert gets == []


def test_an_unknown_id_is_404() -> None:
    async def _run() -> int:
        async with TestClient(TestServer(_app())) as client:
            return (await client.get("/api/artifact-downloads/nope")).status

    assert asyncio.run(_run()) == 404


def test_the_stash_is_bounded_and_entries_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(artifact_download, "time", SimpleNamespace(time=lambda: now[0]))

    async def _run() -> tuple[list[int], int]:
        async with TestClient(TestServer(_app())) as client:
            urls = []
            for i in range(artifact_download._MAX_ENTRIES + 2):
                resp = await client.post(
                    "/api/artifact-downloads",
                    json={"filename": f"{i}.txt", "type": "text/plain", "content": "x"},
                )
                urls.append((await resp.json())["url"])
            firsts = [(await client.get(u)).status for u in urls[:3]]
            now[0] += artifact_download._TTL_SECS + 1
            last = (await client.get(urls[-1])).status
            return firsts, last

    firsts, last = asyncio.run(_run())

    assert firsts == [404, 404, 200]  # the two oldest were evicted
    assert last == 404  # and everything expires
    assert len(artifact_download._stash) == 0


def test_both_routes_are_behind_dashboard_auth() -> None:
    artifact_download._stash["known"] = (10**12, "text/plain", "a.txt", b"secret body")

    async def _run() -> tuple[int, int, bytes]:
        async with TestClient(TestServer(_app(auth=True))) as client:
            post = await client.post(
                "/api/artifact-downloads",
                json={"filename": "a.txt", "type": "text/plain", "content": "x"},
            )
            get = await client.get("/api/artifact-downloads/known")
            return post.status, get.status, await get.read()

    post_status, get_status, body = asyncio.run(_run())

    assert post_status in (401, 403)
    assert get_status in (401, 403)
    assert b"secret body" not in body
