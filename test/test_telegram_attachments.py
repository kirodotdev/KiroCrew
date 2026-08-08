"""Telegram inbound attachment support — unit tests.

Covers:
- Client ``_dispatch`` extraction of photos, documents, audio, voice, video
- ``TelegramInbound.attachments`` population and ``caption`` fallback
- ``telegram/attachments.py`` — ``_to_attachment`` mapping + ``process_telegram_attachments``
- ``transport.receive()`` accepting attachment-only messages
- Capability flag ``files_inbound=True``
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.telegram.attachments import (
    _to_attachment,
    append_attachment_context,
    process_telegram_attachments,
)
from kiro_crew.telegram.client import TelegramClient, TelegramInbound
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

# ── Capability declaration ─────────────────────────────────────────────────────


class TestCapabilityFlag:
    def test_files_inbound_enabled(self):
        assert TELEGRAM_CAPABILITIES.files_inbound is True

    def test_files_outbound_still_disabled(self):
        assert TELEGRAM_CAPABILITIES.files_outbound is False


# ── Client _dispatch extracts attachments from Telegram updates ────────────────


class TestDispatchExtraction:
    """Test that _dispatch populates TelegramInbound.attachments correctly."""

    def _make_client(self) -> tuple[TelegramClient, list[TelegramInbound]]:
        """Build a client that captures dispatched inbound messages."""
        received: list[TelegramInbound] = []

        async def on_message(inbound: TelegramInbound) -> None:
            received.append(inbound)

        client = TelegramClient.__new__(TelegramClient)
        client._on_message = on_message
        client._on_callback = None
        client._handler_tasks = set()
        return client, received

    def _run(self, client: TelegramClient, update: dict) -> None:
        """Dispatch one update and drain the created task."""
        async def _go():
            client._dispatch(update)
            tasks = list(client._handler_tasks)
            if tasks:
                await asyncio.gather(*tasks)
        asyncio.run(_go())

    def _base_update(self, **media: Any) -> dict:
        return {
            "message": {
                "message_id": 1,
                "chat": {"id": 100, "type": "private"},
                "from": {"id": 42, "username": "testuser"},
                **media,
            }
        }

    def test_photo_extracts_largest(self):
        client, received = self._make_client()
        update = self._base_update(
            caption="look at this",
            photo=[
                {"file_id": "small", "file_unique_id": "s1", "width": 90, "height": 90, "file_size": 1000},
                {"file_id": "medium", "file_unique_id": "m1", "width": 320, "height": 320, "file_size": 5000},
                {"file_id": "large", "file_unique_id": "l1", "width": 800, "height": 800, "file_size": 50000},
            ],
        )
        self._run(client, update)
        assert len(received) == 1
        inbound = received[0]
        assert inbound.text == "look at this"
        assert len(inbound.attachments) == 1
        assert inbound.attachments[0]["file_id"] == "large"
        assert inbound.attachments[0]["mime_type"] == "image/jpeg"
        assert inbound.attachments[0]["file_name"] == "photo.jpg"

    def test_document_extracted(self):
        client, received = self._make_client()
        update = self._base_update(
            text="here's the doc",
            document={
                "file_id": "doc1",
                "file_unique_id": "d1",
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
                "file_size": 100000,
            },
        )
        self._run(client, update)
        assert len(received) == 1
        assert len(received[0].attachments) == 1
        assert received[0].attachments[0]["file_name"] == "report.pdf"

    def test_voice_extracted(self):
        client, received = self._make_client()
        update = self._base_update(
            voice={
                "file_id": "voice1",
                "file_unique_id": "v1",
                "mime_type": "audio/ogg",
                "file_size": 8000,
                "duration": 5,
            },
        )
        self._run(client, update)
        assert len(received) == 1
        # No text or caption → text is empty, but attachment is present
        assert received[0].text == ""
        assert len(received[0].attachments) == 1
        assert received[0].attachments[0]["mime_type"] == "audio/ogg"

    def test_caption_used_when_no_text(self):
        client, received = self._make_client()
        update = self._base_update(
            caption="my caption",
            document={"file_id": "f1", "file_unique_id": "u1", "file_name": "x.txt", "mime_type": "text/plain"},
        )
        self._run(client, update)
        assert received[0].text == "my caption"

    def test_sticker_not_extracted(self):
        client, received = self._make_client()
        update = self._base_update(
            sticker={"file_id": "stk1", "file_unique_id": "s1", "type": "regular"},
        )
        self._run(client, update)
        assert len(received) == 1
        # Sticker-only → no text AND no attachments
        assert received[0].text == ""
        assert received[0].attachments == []

    def test_video_extracted(self):
        client, received = self._make_client()
        update = self._base_update(
            caption="check this",
            video={
                "file_id": "vid1",
                "file_unique_id": "v1",
                "mime_type": "video/mp4",
                "file_size": 500000,
            },
        )
        self._run(client, update)
        assert len(received) == 1
        assert received[0].text == "check this"
        assert len(received[0].attachments) == 1
        assert received[0].attachments[0]["mime_type"] == "video/mp4"


# ── _to_attachment normalization ───────────────────────────────────────────────


class TestToAttachment:
    def test_maps_photo(self):
        raw = {
            "file_id": "abc123",
            "file_unique_id": "u1",
            "file_name": "photo.jpg",
            "mime_type": "image/jpeg",
            "file_size": 50000,
        }
        att = _to_attachment(raw)
        assert att.name == "photo.jpg"
        assert att.mimetype == "image/jpeg"
        assert att.size == 50000
        assert att.url == "abc123"  # file_id is the "url" for Telegram
        assert att.suffix_hint == "jpg"

    def test_maps_document_with_no_name(self):
        raw = {"file_id": "x", "file_unique_id": "u", "mime_type": "application/pdf"}
        att = _to_attachment(raw)
        assert att.name == "file"  # fallback
        assert att.mimetype == "application/pdf"

    def test_non_dict_returns_unknown(self):
        att = _to_attachment("not a dict")
        assert att.name == "unknown"

    def test_missing_size_defaults_zero(self):
        raw = {"file_id": "x", "file_unique_id": "u", "file_name": "a.txt", "mime_type": "text/plain"}
        att = _to_attachment(raw)
        assert att.size == 0


# ── process_telegram_attachments ───────────────────────────────────────────────


class TestProcessTelegramAttachments:
    @pytest.mark.asyncio
    async def test_image_downloaded(self):
        """An image attachment is downloaded and its path returned."""
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        client = MagicMock()

        async def fake_download(file_id: str, dest: str) -> None:
            with open(dest, "wb") as f:
                f.write(png_header)

        client.download_file = AsyncMock(side_effect=fake_download)

        attachments = [
            {
                "file_id": "photo_abc",
                "file_unique_id": "u1",
                "file_name": "photo.jpg",
                "mime_type": "image/jpeg",
                "file_size": len(png_header),
            }
        ]
        result = await process_telegram_attachments(client, attachments)
        assert len(result.image_paths) == 1
        assert os.path.exists(result.image_paths[0])
        # Clean up
        for p in result.temp_paths:
            os.unlink(p)

    @pytest.mark.asyncio
    async def test_document_extracted(self):
        """A text document is read and injected as a text block."""
        client = MagicMock()

        async def fake_download(file_id: str, dest: str) -> None:
            with open(dest, "w") as f:
                f.write("hello world")

        client.download_file = AsyncMock(side_effect=fake_download)

        attachments = [
            {
                "file_id": "doc1",
                "file_unique_id": "u2",
                "file_name": "notes.txt",
                "mime_type": "text/plain",
                "file_size": 11,
            }
        ]
        result = await process_telegram_attachments(client, attachments)
        assert len(result.text_blocks) == 1
        assert "hello world" in result.text_blocks[0]
        assert result.image_paths == []

    @pytest.mark.asyncio
    async def test_video_rejected(self):
        """Video attachments produce a rejection, not a download."""
        client = MagicMock()
        client.download_file = AsyncMock()

        attachments = [
            {
                "file_id": "vid1",
                "file_unique_id": "u3",
                "file_name": "clip.mp4",
                "mime_type": "video/mp4",
                "file_size": 500000,
            }
        ]
        result = await process_telegram_attachments(client, attachments)
        assert result.image_paths == []
        assert len(result.rejections) == 1
        assert "video" in result.rejections[0].lower()
        client.download_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_voice_transcribed_when_available(self):
        """Voice attachments trigger transcription when STT is available."""
        ogg_data = b"OggS" + b"\x00" * 100  # fake OGG header
        client = MagicMock()

        async def fake_download(file_id: str, dest: str) -> None:
            with open(dest, "wb") as f:
                f.write(ogg_data)

        client.download_file = AsyncMock(side_effect=fake_download)

        attachments = [
            {
                "file_id": "voice1",
                "file_unique_id": "u4",
                "file_name": "file",
                "mime_type": "audio/ogg",
                "file_size": len(ogg_data),
            }
        ]
        with patch("kiro_crew.transcribe.is_available", return_value=True), \
             patch("kiro_crew.transcribe.transcribe_audio", return_value="hello from voice"):
            result = await process_telegram_attachments(client, attachments)

        assert len(result.text_blocks) == 1
        assert "hello from voice" in result.text_blocks[0]
        assert "transcription" in result.text_blocks[0].lower()

    @pytest.mark.asyncio
    async def test_voice_rejected_when_stt_unavailable(self):
        """Voice attachments produce a rejection when STT is not available."""
        ogg_data = b"OggS" + b"\x00" * 100
        client = MagicMock()

        async def fake_download(file_id: str, dest: str) -> None:
            with open(dest, "wb") as f:
                f.write(ogg_data)

        client.download_file = AsyncMock(side_effect=fake_download)

        attachments = [
            {
                "file_id": "voice2",
                "file_unique_id": "u5",
                "file_name": "file",
                "mime_type": "audio/ogg",
                "file_size": len(ogg_data),
            }
        ]
        with patch("kiro_crew.transcribe.is_available", return_value=False):
            result = await process_telegram_attachments(client, attachments)

        assert len(result.rejections) == 1
        assert "unavailable" in result.rejections[0].lower()


# ── append_attachment_context re-export ────────────────────────────────────────


class TestReExport:
    def test_telegram_exports_shared_function(self):
        from kiro_crew.messaging.attachments import append_attachment_context as shared_fn

        assert append_attachment_context is shared_fn

    def test_transcription_block_is_shared_not_duplicated(self):
        """Both channel adapters must call the SHARED transcription helper.

        Design Review flagged that Telegram's transcription block was a
        byte-for-byte copy of Discord's, so the transcript wording and the
        STT-unavailable handling could drift between channels. Guard the fix at
        the source level: neither adapter may re-implement the loop, and both
        must delegate to ``transcribe_audio_attachments``.
        """
        import inspect

        from kiro_crew.discord import attachments as discord_att
        from kiro_crew.telegram import attachments as tg_att

        cases = (
            (discord_att, discord_att.process_discord_attachments),
            (tg_att, tg_att.process_telegram_attachments),
        )
        for mod, fn in cases:
            # Scope the delegation check to the FUNCTION BODY, not the module:
            # the module always mentions the helper on its import line, so a
            # module-level substring check passes even when the call site is
            # gone (verified by mutation -- it did).
            fn_src = inspect.getsource(fn)
            assert (
                "transcribe_audio_attachments" in fn_src
            ), f"{mod.__name__}: the helper must be CALLED, not merely imported"
            # And the duplicated implementation's tell-tale strings must be
            # absent from the whole module.
            mod_src = inspect.getsource(mod)
            assert (
                "[End of transcription]" not in mod_src
            ), f"{mod.__name__} still inlines the transcript block"
            assert (
                "stt_available" not in mod_src
            ), f"{mod.__name__} still does its own STT-availability check"
