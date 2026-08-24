"""Tests for video uploads through ``api_upload_file`` (POST /api/upload/file).

Video is the one accepted type that does NOT take the in-memory route: the part
streams to disk chunk by chunk under its own, much larger ceiling, because a
screen recording is routinely bigger than the whole document cap and cannot be
buffered. That makes three properties worth pinning, none of which the existing
upload suite covers:

* the container signature is still checked BEFORE any byte is written, so an
  allowed extension cannot smuggle arbitrary content (CWE-434), and a refused
  upload leaves nothing behind on disk;
* the document cap does not apply to video, and the video cap does;
* the accepted set is narrower than what ``<video>`` will attempt — ``.mkv``
  shares WebM's signature but stays rejected on purpose.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import part_stream
from kiro_crew.dashboard.handlers import files as files_mod
from kiro_crew.dashboard.handlers.files import api_upload_file

#: QuickTime / MP4 family: a box length, then the ``ftyp`` box at offset 4. The
#: brand differs between .mov ('qt  ') and .mp4 ('isom'); the box does not, and
#: the box is what the sniffer keys on.
MOV_HEADER = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00"
MP4_HEADER = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00"
#: EBML magic, shared by WebM and Matroska.
WEBM_HEADER = b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1f"


def _make_app() -> web.Application:
    app = web.Application()
    app["state"] = MagicMock()
    app.router.add_post("/api/upload/file", api_upload_file)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.files._sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "uploads"
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", target)
    return target


async def _post(payload: bytes, filename: str, content_type: str):
    """POST one part and return ``(status, json_body)``."""
    form = aiohttp.FormData()
    form.add_field("file", payload, filename=filename, content_type=content_type)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        return resp.status, await resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "name", "mime"),
    [
        (MOV_HEADER, "screen.mov", "video/quicktime"),
        (MP4_HEADER, "clip.mp4", "video/mp4"),
        (WEBM_HEADER, "capture.webm", "video/webm"),
    ],
)
async def test_accepted_container_lands_on_disk_byte_for_byte(
    upload_dir: Path,
    mock_sel,
    header: bytes,
    name: str,
    mime: str,
) -> None:
    """Each accepted container uploads, and the streamed bytes are unchanged.

    Byte equality is the assertion that matters here: the streaming path writes
    the buffered header first and the remaining chunks after it, so an off-by-one
    in that seam would produce a file that is subtly corrupt rather than one that
    fails loudly.
    """
    payload = header + bytes(range(256)) * 40
    status, body = await _post(payload, name, mime)
    assert status == 200, body
    assert len(body["paths"]) == 1, body
    written = Path(body["paths"][0])
    assert written.parent == upload_dir
    assert written.read_bytes() == payload


@pytest.mark.asyncio
async def test_video_is_exempt_from_the_document_cap(
    upload_dir: Path,
    mock_sel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A video over ``_MAX_UPLOAD_BYTES`` but under the video cap is accepted.

    Shrinking the document cap rather than sending a real 60 MB body keeps the
    test fast while still proving which ceiling the video branch consults — the
    whole point of the separate constant.
    """
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files._MAX_UPLOAD_BYTES", 64)
    payload = MOV_HEADER + b"\x00" * 4096
    status, body = await _post(payload, "long.mov", "video/quicktime")
    assert status == 200, body
    assert Path(body["paths"][0]).stat().st_size == len(payload)


@pytest.mark.asyncio
async def test_video_over_its_own_cap_is_refused_and_leaves_nothing(
    upload_dir: Path,
    mock_sel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the video ceiling the request 413s and the partial file is removed.

    The partial matters: the streaming route has already created and written the
    destination by the time the cap trips, so a missing cleanup would leave a
    truncated recording in uploads/ that the composer would happily attach.
    """
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files._MAX_VIDEO_UPLOAD_BYTES", 512)
    status, body = await _post(MOV_HEADER + b"\x00" * 8192, "huge.mov", "video/quicktime")
    assert status == 413, body
    assert "too large" in body["error"].lower(), body
    # The code, not the prose, is what a localized UI can branch on.
    assert body["code"] == "video_too_large", body
    assert list(upload_dir.glob("*")) == []


@pytest.mark.asyncio
async def test_html_wearing_a_mov_extension_is_refused_before_any_write(
    upload_dir: Path,
    mock_sel,
) -> None:
    """The signature gate runs on the header, so nothing reaches disk (CWE-434).

    This is the case the streaming route could most easily regress: writing the
    first chunk before judging it would put attacker-chosen bytes in uploads/
    under a name the rest of the app treats as a playable video.
    """
    status, body = await _post(
        b"<html><script>alert(1)</script></html>" + b"x" * 128,
        "payload.mov",
        "video/quicktime",
    )
    assert status == 400, body
    assert body["code"] == "video_content_mismatch", body
    # Asserted on the CODE plus the presence of a remedy, not on the sentence:
    # the code is the contract, and pinning exact prose makes every wording
    # improvement look like a regression. The remedy must be there, though --
    # "does not match its type" alone tells the user nothing to do next.
    for ext in (".mp4", ".mov", ".webm"):
        assert ext in body["error"], body
    assert list(upload_dir.glob("*")) == []


@pytest.mark.asyncio
async def test_file_shorter_than_the_sniff_window_is_refused(
    upload_dir: Path,
    mock_sel,
) -> None:
    """A part that ends before the header can be judged is refused, not written.

    Guards the streaming loop's tail branch: the buffered header is still
    unverified when the part ends, and treating "never got enough bytes" as
    "passed" would be the natural bug.
    """
    status, body = await _post(b"\x00\x00", "tiny.mov", "video/quicktime")
    assert status == 400, body
    assert list(upload_dir.glob("*")) == []


@pytest.mark.asyncio
async def test_a_playable_but_unaccepted_container_names_the_way_out(
    upload_dir: Path,
    mock_sel,
) -> None:
    """Refusing ``.mkv`` says which containers DO work.

    A browser plays several containers this boundary refuses, so a bare
    "Unsupported file type: .mkv" reads as "video is not supported" when the
    actual remedy is a re-encode. Asserted on the accepted list rather than the
    exact sentence so rewording the message does not fail this test.
    """
    status, body = await _post(WEBM_HEADER + b"\x00" * 256, "capture.mkv", "video/x-matroska")
    assert status == 400, body
    for ext in (".mp4", ".mov", ".webm"):
        assert ext in body["error"], body


@pytest.mark.asyncio
async def test_an_audio_container_is_not_told_to_re_encode_into_video(
    upload_dir: Path,
    mock_sel,
) -> None:
    """``.m4a`` is refused WITHOUT the accepted-video-containers hint.

    The hint's whole purpose is "your video needs a re-encode"; attaching it to
    an audio upload tells that sender to wrap audio in a video container, which
    is worse guidance than the bare refusal. ``.m4a`` shares the MP4 ``ftyp``
    magic, so only the extension set separates the two cases -- which is exactly
    why this is easy to reintroduce.
    """
    status, body = await _post(MP4_HEADER + b"\x00" * 256, "voice.m4a", "audio/mp4")
    assert status == 400, body
    assert ".webm" not in body["error"], body
    assert "accepted video containers" not in body["error"], body


@pytest.mark.asyncio
async def test_cancellation_mid_stream_leaves_nothing_on_disk(
    upload_dir: Path,
    mock_sel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled upload removes its partial file and its siblings.

    ``asyncio.CancelledError`` derives from ``BaseException``, so a bare
    ``except Exception`` around the streaming write does not see it. Without
    naming it, a gateway shutdown or client disconnect part-way through a
    recording leaves a truncated video in uploads/ that is indistinguishable
    from a complete one to everything downstream — the composer would attach it
    and the player would stop early with no error.

    Simulated by raising from inside the sink's write rather than cancelling the
    task: the property under test is that the handler's cleanup runs when a
    BaseException escapes the stream, and this reaches it deterministically. The
    temp already exists at that point — the sink is opened before the first
    write — so an empty uploads/ is real evidence the unlink ran.

    The injection point is the SINK's write, not ``os.write``: writes go through
    a buffered writer that owns its own raw file, so patching a module's
    ``os.write`` would no longer intercept them and this test would pass
    vacuously while proving nothing.
    """

    def refuse(_self, _data) -> int:  # noqa: ANN001, ANN202
        raise asyncio.CancelledError()

    monkeypatch.setattr(part_stream._TempSink, "write", refuse)
    form = aiohttp.FormData()
    form.add_field(
        "file",
        MOV_HEADER + b"\x00" * 512,
        filename="interrupted.mov",
        content_type="video/quicktime",
    )
    async with TestClient(TestServer(_make_app())) as client:
        with pytest.raises(BaseException):  # noqa: B017,PT011 - CancelledError
            await client.post("/api/upload/file", data=form)
    monkeypatch.undo()
    # Neither the destination nor the `.part` temp may survive: a truncated
    # recording that looks complete is worse than a failed upload to retry.
    assert list(upload_dir.glob("*")) == []


@pytest.mark.asyncio
async def test_dest_is_never_published_when_the_commit_rename_fails(
    upload_dir: Path,
    mock_sel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the atomic publish fails, `dest` does not exist and no temp survives.

    This pins the invariant the whole shape exists for: `dest` is created only
    by the rename, from a file already complete, so there is no failure path
    that leaves something at `dest` for the composer to attach and the player to
    truncate. Three separate blocking findings on this function were all that
    same defect — an ignored short write, a cancellation skipping cleanup, a
    cancellation racing the open — so the test is on the invariant rather than
    on any one of those paths.
    """

    def boom(src, dst) -> None:  # noqa: ANN001 - os.replace signature
        raise OSError("replace failed")

    monkeypatch.setattr(part_stream.os, "replace", boom)
    with pytest.raises(Exception):
        await _post(MOV_HEADER + b"\x00" * 512, "doomed.mov", "video/quicktime")
    monkeypatch.undo()
    # Neither the destination nor the `.part` temp may survive.
    assert list(upload_dir.glob("*")) == []


@pytest.mark.asyncio
async def test_streaming_bypasses_the_app_client_max_size(
    upload_dir: Path,
    mock_sel,
) -> None:
    """A video larger than the app's ``client_max_size`` still uploads.

    The whole 512 MB ceiling rests on this: the dashboard builds its
    ``web.Application`` with ``client_max_size=60 MB``, which aiohttp enforces in
    ``Request.read()`` / ``.post()`` but NOT on the streaming ``multipart()``
    reader this handler uses. If that were wrong the feature would be dead for
    exactly the 60-150 MB recordings it exists to carry, and no cap-shrinking
    test would show it.

    Driven with a deliberately tiny ``client_max_size`` rather than a real 60 MB
    body: the property under test is "the limit does not apply", which a 2 KB
    limit demonstrates as well as a 60 MB one and in a fraction of the time.
    """
    app = web.Application(client_max_size=2048)
    app["state"] = MagicMock()
    app.router.add_post("/api/upload/file", api_upload_file)

    payload = MOV_HEADER + b"\x00" * 8192  # 4x the app's limit
    form = aiohttp.FormData()
    form.add_field("file", payload, filename="big.mov", content_type="video/quicktime")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/upload/file", data=form)
        body = await resp.json()
    assert resp.status == 200, body
    assert Path(body["paths"][0]).read_bytes() == payload


def _website_source(name: str) -> str:
    """Read a frontend source file from the repo, for the cross-language pins."""
    root = Path(__file__).resolve().parents[1]
    return (root / "website" / "src" / name).read_text(encoding="utf-8")


def test_accept_list_covers_every_accepted_extension() -> None:
    """The composer's `accept` MIME list matches the server's accepted set.

    This pin only works from the Python side: a vitest cannot read
    ``_ALLOWED_VIDEO_EXT``, so a frontend-only assertion is one-sided and cannot
    catch the drift that matters — an extension the server accepts but the
    picker filters out of the photo library, which is invisible until a user
    cannot find their own recording.
    """
    accept = re.search(
        r"const VIDEO_ACCEPT = '([^']+)'", _website_source("components/ChatInput.tsx")
    )
    assert accept, "VIDEO_ACCEPT not found in ChatInput.tsx"
    offered = set(accept.group(1).split(","))
    # Every accepted extension needs a MIME a picker can filter on. `.m4v` is the
    # trap: its `video/x-m4v` type is not implied by `video/mp4`.
    required = {"video/mp4", "video/x-m4v", "video/quicktime", "video/webm"}
    assert required <= offered, (offered, required - offered)
    assert set(files_mod._ALLOWED_VIDEO_EXT) == {".mp4", ".m4v", ".mov", ".webm"}


def test_client_video_cap_matches_the_server_ceiling() -> None:
    """`VIDEO_MAX_BYTES` in fileTokens.ts equals ``_MAX_VIDEO_UPLOAD_BYTES``.

    ChatPane pre-checks against the client copy because it has no error surface,
    so a server cap raised without the mirror leaves that pane silently refusing
    recordings the server would take. Asserted here rather than in the vitest
    that reads the constant: only this side can see both numbers.
    """
    src = _website_source("utils/fileTokens.ts")
    match = re.search(r"VIDEO_MAX_BYTES = (\d+) \* 1024 \* 1024", src)
    assert match, "VIDEO_MAX_BYTES not found in fileTokens.ts"
    assert int(match.group(1)) * 1024 * 1024 == files_mod._MAX_VIDEO_UPLOAD_BYTES


def test_client_video_regex_matches_the_server_extension_set() -> None:
    """`VIDEO_EXT` in fileTokens.ts lists exactly the server's extensions.

    Third spelling of the same set. A regex that accepts more than the server
    exempts a file from the client size guard only for the server to refuse it;
    one that accepts less applies the 50 MB document cap to a legal recording.
    """
    src = _website_source("utils/fileTokens.ts")
    match = re.search(r"VIDEO_EXT = /\\\.\(([^)]+)\)\$/i", src)
    assert match, "VIDEO_EXT not found in fileTokens.ts"
    from_regex = {f".{alt}" for alt in match.group(1).split("|")}
    assert from_regex == set(files_mod._ALLOWED_VIDEO_EXT), from_regex


@pytest.mark.asyncio
async def test_mkv_stays_unsupported_despite_a_valid_ebml_signature(
    upload_dir: Path,
    mock_sel,
) -> None:
    """``.mkv`` is excluded on purpose and is refused on EXTENSION, not content.

    It carries the same EBML magic WebM does, so only the allowlist separates
    them. Pinned so a future "just add mkv, the magic already matches" change
    has to argue with browser playback rather than pass silently.
    """
    status, body = await _post(WEBM_HEADER + b"\x00" * 256, "capture.mkv", "video/x-matroska")
    assert status == 400, body
    assert "Unsupported file type" in body["error"], body
    assert body["code"] == "unsupported_file_type", body
    assert list(upload_dir.glob("*")) == []
