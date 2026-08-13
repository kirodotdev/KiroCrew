"""Channel-neutral attachment ingestion.

Focused on the guarantees that did NOT exist when this logic lived inside
``slack/files.py``:

* size enforced on the DOWNLOADED bytes, not on channel-reported metadata
* image content validated by signature, with the true type winning over a
  mislabelled one
* a per-message attachment cap
* rejections returned to the caller instead of silently swallowed
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from kiro_crew.messaging import attachments
from kiro_crew.messaging.attachments import (
    AUDIO,
    DOCUMENT,
    IMAGE,
    OTHER,
    TEXT,
    VIDEO,
    Attachment,
    IngestLimits,
    IngestResult,
    append_attachment_context,
    classify,
    cleanup,
    ingest_attachments,
    safe_suffix,
    sniff_image_mime,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_GIF = b"GIF89a" + b"\x00" * 32
_BMP = b"BM" + b"\x00" * 32
_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16


def _writer(content: bytes):
    """A download callback that writes fixed bytes."""

    async def _download(url: str, dest: str) -> None:
        with open(dest, "wb") as fh:
            fh.write(content)

    return _download


async def _boom(url: str, dest: str) -> None:
    raise RuntimeError("network error")


class TestClassify:
    @pytest.mark.parametrize(
        "mime,expected",
        [
            ("image/png", IMAGE),
            ("image/jpeg", IMAGE),
            ("text/plain", TEXT),
            ("application/json", TEXT),
            ("audio/webm", AUDIO),
            ("video/mp4", VIDEO),
            ("application/octet-stream", OTHER),
            ("", OTHER),
        ],
    )
    def test_mimetype_mapping(self, mime, expected):
        assert classify(mime) == expected

    def test_documents_detected_by_filename(self):
        assert classify("application/pdf", "report.pdf") == DOCUMENT

    def test_svg_is_not_an_image(self):
        """SVG is scriptable XML; the ACP encoder cannot inline it."""
        assert classify("image/svg+xml") == OTHER


class TestSafeSuffix:
    @pytest.mark.parametrize(
        "hint,expected",
        [
            ("png", ".png"),
            ("PNG", ".PNG"),
            ("", ".bin"),
            ("../../etc/passwd", ".etcpasswd"),
            ("tar.gz", ".targz"),
            ("a b/c", ".abc"),
        ],
    )
    def test_never_yields_a_path_component(self, hint, expected):
        out = safe_suffix(hint)
        assert out == expected
        assert "/" not in out and ".." not in out.strip(".")


class TestSniffImageMime:
    @pytest.mark.parametrize(
        "content,expected",
        [
            (_PNG, "image/png"),
            (_JPEG, "image/jpeg"),
            (_GIF, "image/gif"),
            (_BMP, "image/bmp"),
            (_WEBP, "image/webp"),
        ],
    )
    def test_detects_real_types(self, tmp_path, content, expected):
        p = tmp_path / "f"
        p.write_bytes(content)
        assert sniff_image_mime(str(p)) == expected

    def test_non_image_returns_none(self, tmp_path):
        p = tmp_path / "f"
        p.write_bytes(b"#!/bin/sh\nrm -rf /\n")
        assert sniff_image_mime(str(p)) is None

    def test_riff_that_is_not_webp_is_rejected(self, tmp_path):
        """A RIFF/WAVE audio file shares the 'RIFF' prefix with WebP."""
        p = tmp_path / "a.webp"
        p.write_bytes(b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 16)
        assert sniff_image_mime(str(p)) is None


@pytest.mark.asyncio
class TestIngestAttachments:
    async def test_image_is_downloaded_and_returned(self):
        result = await ingest_attachments(
            [Attachment(name="a.png", mimetype="image/png", size=40, url="u", suffix_hint="png")],
            download=_writer(_PNG),
            source="test",
        )
        assert len(result.image_paths) == 1
        assert result.rejections == []
        assert os.path.exists(result.image_paths[0])
        cleanup(result.temp_paths)

    async def test_size_enforced_on_downloaded_bytes_not_metadata(self):
        """The hole this closes: Slack trusted the channel's `size`, which
        defaults to 0 when absent, so a lying or missing value bypassed the cap
        entirely. The post-download check is the one that actually holds."""
        result = await ingest_attachments(
            # Metadata claims 0 bytes; the real payload is far over the cap.
            [Attachment(name="a.png", mimetype="image/png", size=0, url="u", suffix_hint="png")],
            download=_writer(_PNG + b"\x00" * 4096),
            source="test",
            limits=IngestLimits(max_image_bytes=64),
        )
        assert result.image_paths == []
        assert any("too large" in r for r in result.rejections)

    async def test_non_image_masquerading_as_image_is_rejected(self):
        result = await ingest_attachments(
            [Attachment(name="evil.png", mimetype="image/png", size=20, url="u", suffix_hint="png")],
            download=_writer(b"#!/bin/sh\nrm -rf /\n"),
            source="test",
        )
        assert result.image_paths == []
        assert any("not a readable image" in r for r in result.rejections)

    async def test_mislabelled_but_valid_image_still_works(self):
        """A real JPEG the channel called image/png must NOT be rejected -- and
        the temp file is retyped so the ACP encoder emits truthful mimeType."""
        result = await ingest_attachments(
            [Attachment(name="a.png", mimetype="image/png", size=40, url="u", suffix_hint="png")],
            download=_writer(_JPEG),
            source="test",
        )
        assert len(result.image_paths) == 1
        path = result.image_paths[0]
        # The declared suffix is REPLACED, not appended: "x.png.jpg" would still
        # claim to be a PNG, which defeats the point of retyping.
        assert path.endswith(".jpg")
        assert ".png" not in path
        cleanup(result.temp_paths)

    async def test_correctly_labelled_image_keeps_its_path(self):
        """No rename when the declared suffix already matches the real type."""
        result = await ingest_attachments(
            [Attachment(name="a.png", mimetype="image/png", size=40, url="u", suffix_hint="png")],
            download=_writer(_PNG),
            source="test",
        )
        assert result.image_paths[0].endswith(".png")
        cleanup(result.temp_paths)

    async def test_text_is_inlined_and_redacted(self):
        secret = "ghp_" + "a" * 36
        result = await ingest_attachments(
            [Attachment(name="n.txt", mimetype="text/plain", size=50, url="u")],
            download=_writer(f"token={secret}\nhello".encode()),
            source="test",
        )
        assert len(result.text_blocks) == 1
        block = result.text_blocks[0]
        assert "[File: n.txt]" in block and "hello" in block
        assert secret not in block

    async def test_text_is_truncated(self):
        result = await ingest_attachments(
            [Attachment(name="n.txt", mimetype="text/plain", size=10_000, url="u")],
            download=_writer(b"x" * 5000),
            source="test",
            limits=IngestLimits(max_text_inject=100),
        )
        assert "truncated" in result.text_blocks[0]

    async def test_video_is_rejected_with_a_reason(self):
        """Out of scope is not the same as silently dropped."""
        result = await ingest_attachments(
            [Attachment(name="clip.mp4", mimetype="video/mp4", size=99, url="u")],
            download=_writer(b"\x00"),
            source="test",
        )
        assert result.image_paths == [] and result.text_blocks == []
        assert any("video is not supported" in r for r in result.rejections)

    async def test_unsupported_type_is_rejected_with_a_reason(self):
        result = await ingest_attachments(
            [Attachment(name="x.bin", mimetype="application/octet-stream", size=9, url="u")],
            download=_writer(b"\x00"),
            source="test",
        )
        assert any("unsupported type" in r for r in result.rejections)

    async def test_missing_url_is_reported(self):
        result = await ingest_attachments(
            [Attachment(name="a.png", mimetype="image/png", size=10, url="")],
            download=_writer(_PNG),
            source="test",
        )
        assert any("no download URL" in r for r in result.rejections)

    async def test_download_failure_is_reported_and_leaves_no_temp_file(self, tmp_path, monkeypatch):
        import tempfile as _tempfile

        created: list[str] = []
        real_mkstemp = _tempfile.mkstemp

        def _tracking_mkstemp(*a, **kw):
            fd, path = real_mkstemp(*a, **kw)
            created.append(path)
            return fd, path

        monkeypatch.setattr(_tempfile, "mkstemp", _tracking_mkstemp)

        result = await ingest_attachments(
            [Attachment(name="a.png", mimetype="image/png", size=10, url="u")],
            download=_boom,
            source="test",
        )
        assert result.image_paths == []
        assert any("download failed" in r for r in result.rejections)
        assert [p for p in created if os.path.exists(p)] == []

    async def test_audio_skipped_by_default(self):
        result = await ingest_attachments(
            [Attachment(name="v.webm", mimetype="audio/webm", size=10, url="u")],
            download=_writer(b"\x00"),
            source="test",
        )
        assert result.audio_paths == []
        assert result.rejections == []  # transcribed upstream, not a rejection

    async def test_audio_returned_when_opted_in(self):
        result = await ingest_attachments(
            [Attachment(name="v.webm", mimetype="audio/webm", size=10, url="u")],
            download=_writer(b"\x00" * 10),
            source="test",
            handle_audio=True,
        )
        assert len(result.audio_paths) == 1
        cleanup(result.temp_paths)

    async def test_attachment_count_is_capped_and_reported(self):
        atts = [
            Attachment(name=f"a{i}.png", mimetype="image/png", size=40, url="u", suffix_hint="png")
            for i in range(5)
        ]
        result = await ingest_attachments(
            atts,
            download=_writer(_PNG),
            source="test",
            limits=IngestLimits(max_attachments=2),
        )
        assert len(result.image_paths) == 2
        assert any("more attachment(s) ignored" in r for r in result.rejections)
        cleanup(result.temp_paths)

    async def test_one_bad_attachment_does_not_lose_the_others(self):
        atts = [
            Attachment(name="bad.png", mimetype="image/png", size=10, url=""),
            Attachment(name="ok.png", mimetype="image/png", size=40, url="u", suffix_hint="png"),
        ]
        result = await ingest_attachments(atts, download=_writer(_PNG), source="test")
        assert len(result.image_paths) == 1
        assert len(result.rejections) == 1
        cleanup(result.temp_paths)

    async def test_temp_paths_covers_images_and_audio(self):
        result = await ingest_attachments(
            [
                Attachment(name="a.png", mimetype="image/png", size=40, url="u", suffix_hint="png"),
                Attachment(name="v.webm", mimetype="audio/webm", size=10, url="u"),
            ],
            download=_writer(_PNG),
            source="test",
            handle_audio=True,
        )
        assert len(result.temp_paths) == 2
        cleanup(result.temp_paths)
        assert [p for p in result.temp_paths if os.path.exists(p)] == []


@pytest.mark.asyncio
class TestChannelDeclaredAudio:
    """A channel may treat a non-``audio/*`` type as audio.

    Slack ships voice memos as ``video/webm``. Its transcription path has always
    known that; the generic classifier did not, so the same file was transcribed
    AND rejected as unsupported video, putting a contradictory note next to the
    transcript the user actually got.
    """

    async def test_declared_audio_is_not_rejected_as_video(self):
        result = await ingest_attachments(
            [Attachment(name="memo.webm", mimetype="video/webm", size=10, url="u")],
            download=_writer(b"\x00" * 10),
            source="test",
            audio_mimetypes=("audio/", "video/webm"),
        )
        assert result.rejections == []
        assert result.audio_paths == []  # handle_audio=False -> silently skipped
        assert result.text_blocks == []

    async def test_same_type_is_still_video_without_the_override(self):
        """The override is opt-in: a real video upload is still rejected."""
        result = await ingest_attachments(
            [Attachment(name="clip.webm", mimetype="video/webm", size=10, url="u")],
            download=_writer(b"\x00" * 10),
            source="test",
        )
        assert any("video is not supported" in r for r in result.rejections)

    async def test_declared_audio_can_be_returned_for_transcription(self):
        result = await ingest_attachments(
            [Attachment(name="memo.webm", mimetype="video/webm", size=10, url="u")],
            download=_writer(b"\x00" * 10),
            source="test",
            handle_audio=True,
            audio_mimetypes=("audio/", "video/webm"),
        )
        assert len(result.audio_paths) == 1
        cleanup(result.temp_paths)

    async def test_other_video_types_are_unaffected_by_the_override(self):
        result = await ingest_attachments(
            [Attachment(name="clip.mp4", mimetype="video/mp4", size=10, url="u")],
            download=_writer(b"\x00" * 10),
            source="test",
            audio_mimetypes=("audio/", "video/webm"),
        )
        assert any("video is not supported" in r for r in result.rejections)


class TestClassifyOverride:
    def test_override_wins_over_the_video_prefix(self):
        assert classify("video/webm", audio_mimetypes=("video/webm",)) == AUDIO

    def test_without_override_it_is_video(self):
        assert classify("video/webm") == VIDEO

    def test_override_does_not_leak_to_other_video_types(self):
        assert classify("video/mp4", audio_mimetypes=("video/webm",)) == VIDEO


@pytest.mark.asyncio
class TestBlockingWorkLeavesTheEventLoop:
    """Filesystem work must not run on the gateway's event loop thread.

    ``ingest_attachments`` is awaited directly from the Slack Socket Mode
    callback (``_on_event`` -> ``_route_message`` -> ``process_slack_files``),
    so a synchronous call here stalls EVERY other session's streaming. TMPDIR is
    not guaranteed to be local either -- a network- or FUSE-backed temp dir can
    block for a long time.

    These assert the invariant directly (the call runs on a DIFFERENT thread than
    the loop) rather than asserting it was wrapped in ``to_thread``, so they
    cannot be satisfied by a wrapper that is later unwrapped.
    """

    async def test_temp_file_creation_is_off_the_loop_thread(self, monkeypatch):
        loop_thread = threading.get_ident()
        observed: list[int] = []
        real_make = attachments._make_temp

        # Spy on THIS module's helper, not tempfile.mkstemp: patching the module
        # attribute would also catch the SEL audit subsystem's own .sel_hmac_*.tmp
        # write, which is a separate (pre-existing, repo-wide) call site and not
        # what this test is about.
        def _spy(suffix: str) -> str:
            observed.append(threading.get_ident())
            return real_make(suffix)

        monkeypatch.setattr(attachments, "_make_temp", _spy)
        result = await ingest_attachments(
            [Attachment(name="a.png", mimetype="image/png", size=40, url="u", suffix_hint="png")],
            download=_writer(_PNG),
            source="test",
        )
        cleanup(result.temp_paths)

        assert observed, "temp creation was never called"
        assert loop_thread not in observed, "temp creation ran on the event loop thread"

    async def test_text_read_is_off_the_loop_thread(self, monkeypatch):
        loop_thread = threading.get_ident()
        observed: list[int] = []
        real_reader = attachments._read_text_file

        def _spy(path: str, limit: int) -> str:
            observed.append(threading.get_ident())
            return real_reader(path, limit)

        monkeypatch.setattr(attachments, "_read_text_file", _spy)
        await ingest_attachments(
            [Attachment(name="n.txt", mimetype="text/plain", size=20, url="u")],
            download=_writer(b"hello"),
            source="test",
        )

        assert observed, "text read was never called"
        assert loop_thread not in observed, "text read ran on the event loop thread"

    async def test_image_signature_sniff_is_off_the_loop_thread(self, monkeypatch):
        loop_thread = threading.get_ident()
        observed: list[int] = []
        real_sniff = attachments.sniff_image_mime

        def _spy(path: str):
            observed.append(threading.get_ident())
            return real_sniff(path)

        monkeypatch.setattr(attachments, "sniff_image_mime", _spy)
        result = await ingest_attachments(
            [Attachment(name="a.png", mimetype="image/png", size=40, url="u", suffix_hint="png")],
            download=_writer(_PNG),
            source="test",
        )
        cleanup(result.temp_paths)

        assert observed, "sniff was never called"
        assert loop_thread not in observed, "signature sniff ran on the event loop thread"

    async def test_the_loop_stays_responsive_during_a_slow_blocking_step(self, monkeypatch):
        """End-to-end: a concurrent task keeps running while a SLOW read happens.

        The blocking step is given real duration (a synchronous sleep inside the
        text reader). Offloaded, the ticker keeps advancing; inline, the loop is
        frozen for the whole 150ms and the tick count barely moves. Without the
        induced delay this test would pass either way, since the awaited download
        yields to the loop on its own.
        """
        real_reader = attachments._read_text_file

        def _slow_read(path: str, limit: int) -> str:
            time.sleep(0.15)  # blocking, as a large/slow filesystem read would be
            return real_reader(path, limit)

        monkeypatch.setattr(attachments, "_read_text_file", _slow_read)

        ticks = 0
        stop = False

        async def _ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.005)

        t = asyncio.create_task(_ticker())
        await asyncio.sleep(0.01)
        before = ticks

        await ingest_attachments(
            [Attachment(name="n.txt", mimetype="text/plain", size=20, url="u")],
            download=_writer(b"hello"),
            source="test",
        )

        during = ticks - before
        stop = True
        await t

        # 150ms of blocking work at a 5ms tick interval leaves room for ~20+
        # ticks when offloaded; a frozen loop yields approximately none.
        assert during >= 5, f"loop was starved during the blocking read (ticks={during})"


# ── append_attachment_context (shared utility) ────────────────────────────────


class TestAppendAttachmentContext:
    """append_attachment_context is channel-neutral and lives in messaging/."""

    def test_image_paths_appended(self):
        result = IngestResult(image_paths=["/tmp/a.png", "/tmp/b.jpg"])
        out = append_attachment_context("hello", result)
        assert out == "hello\n/tmp/a.png\n/tmp/b.jpg"

    def test_text_blocks_appended(self):
        result = IngestResult(text_blocks=["[File: x.txt]\ncontent"])
        out = append_attachment_context("msg", result)
        assert out == "msg\n\n[File: x.txt]\ncontent"

    def test_rejections_appended(self):
        result = IngestResult(rejections=["[Video not supported]"])
        out = append_attachment_context("msg", result)
        assert out == "msg\n\n[Video not supported]"

    def test_all_combined(self):
        result = IngestResult(
            image_paths=["/tmp/img.png"],
            text_blocks=["[File: a.txt]\nhi"],
            rejections=["[nope]"],
        )
        out = append_attachment_context("user text", result)
        assert "/tmp/img.png" in out
        assert "[File: a.txt]\nhi" in out
        assert "[nope]" in out

    def test_empty_text_with_images(self):
        result = IngestResult(image_paths=["/tmp/x.png"])
        out = append_attachment_context("", result)
        assert out == "/tmp/x.png"

    def test_empty_result_returns_text_unchanged(self):
        result = IngestResult()
        assert append_attachment_context("unchanged", result) == "unchanged"

    def test_reexport_from_discord(self):
        """discord/attachments re-exports the same function."""
        from kiro_crew.discord.attachments import append_attachment_context as discord_fn

        assert discord_fn is append_attachment_context
