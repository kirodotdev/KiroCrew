"""Tests for audio uploads through ``POST /api/upload/file``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.files import api_upload_file

MP3_ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" + b"\x00" * 64
MP3_FRAME = b"\xff\xfb" + b"\x00" * 64
M4A = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00" + b"\x00" * 64
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64
OGG = b"OggS" + b"\x00" * 64
FLAC = b"fLaC" + b"\x00" * 64


def _make_app() -> web.Application:
    app = web.Application()
    app["state"] = MagicMock()
    app.router.add_post("/api/upload/file", api_upload_file)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.files._sel") as sel:
        sel.return_value = MagicMock()
        yield sel.return_value


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "uploads"
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", target)
    return target


async def _post(payload: bytes, filename: str, content_type: str):
    form = aiohttp.FormData()
    form.add_field("file", payload, filename=filename, content_type=content_type)
    async with TestClient(TestServer(_make_app())) as client:
        response = await client.post("/api/upload/file", data=form)
        return response.status, await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "filename", "content_type"),
    [
        (MP3_ID3, "tagged.mp3", "audio/mpeg"),
        (MP3_FRAME, "bare.mp3", "audio/mpeg"),
        (M4A, "memo.m4a", "audio/mp4"),
        (WAV, "memo.wav", "audio/wav"),
        (OGG, "memo.ogg", "audio/ogg"),
        (OGG, "memo.oga", "audio/ogg"),
        (OGG, "memo.opus", "audio/opus"),
        (FLAC, "memo.flac", "audio/flac"),
    ],
)
async def test_accepted_audio_lands_on_disk_byte_for_byte(
    upload_dir: Path,
    mock_sel,
    payload: bytes,
    filename: str,
    content_type: str,
) -> None:
    status, body = await _post(payload, filename, content_type)
    assert status == 200, body
    assert len(body["paths"]) == 1, body
    written = Path(body["paths"][0])
    assert written.parent == upload_dir
    assert written.read_bytes() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("payload.mp3", "audio/mpeg"),
        ("payload.m4a", "audio/mp4"),
        ("payload.wav", "audio/wav"),
        ("payload.ogg", "audio/ogg"),
        ("payload.oga", "audio/ogg"),
        ("payload.opus", "audio/opus"),
        ("payload.flac", "audio/flac"),
    ],
)
async def test_non_audio_bytes_are_refused_before_write(
    upload_dir: Path,
    mock_sel,
    filename: str,
    content_type: str,
) -> None:
    status, body = await _post(b"<html><script>alert(1)</script></html>", filename, content_type)
    assert status == 400, body
    assert body["code"] == "audio_content_mismatch", body
    assert list(upload_dir.glob("*")) == []


@pytest.mark.asyncio
async def test_audio_uses_the_buffered_file_cap(
    upload_dir: Path,
    mock_sel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files._MAX_UPLOAD_BYTES", 64)
    status, body = await _post(OGG + b"\x00" * 128, "long.ogg", "audio/ogg")
    assert status == 413, body
    assert body["code"] == "audio_too_large", body
    assert list(upload_dir.glob("*")) == []
