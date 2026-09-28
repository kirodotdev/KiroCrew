"""WhatsApp inbound media ingestion: the filename, the ceiling, and the offload.

``neonize`` is the optional ``[whatsapp]`` extra and is not installed here, but
nothing in :mod:`kiro_crew.whatsapp.attachments` needs it: it takes a
:class:`~kiro_crew.whatsapp.media.MediaDescription` (already read off the
protobuf) plus any object exposing ``download_media``, so a plain fake is the
whole test double.

Three things are pinned here because each one broke silently before it broke
visibly:

* the name handed to the shared ingest layer, since that layer picks a document
  parser and a transcription decoder from its extension;
* :data:`MAX_MEDIA_BYTES`, which bounds what the gateway holds in memory and must
  be measured against the bytes that ARRIVE rather than the length the sender
  declared;
* that the write happens on a worker thread, because ``TMPDIR`` is not guaranteed
  to be local disk and the gateway has one event loop.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.messaging.attachments import DOCUMENT, IMAGE, OTHER, classify, cleanup, safe_suffix
from kiro_crew.whatsapp import attachments as wa_attachments
from kiro_crew.whatsapp.attachments import (
    AUDIO_MIMETYPES,
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_MEDIA_BYTES,
    attachment_for,
    ingest_media,
)
from kiro_crew.whatsapp.media import (
    KIND_AUDIO,
    KIND_DOCUMENT,
    KIND_IMAGE,
    KIND_STICKER,
    KIND_VOICE,
    MediaDescription,
)

#: A real 2x2 PNG. The shared layer sniffs an image's leading bytes and refuses
#: anything that is not a raster, so a magic-only stub would never reach the
#: branches under test.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)

#: What WhatsApp actually declares on a voice note.
_VOICE_MIMETYPE = "audio/ogg; codecs=opus"


class _FakeClient:
    """The only thing :func:`ingest_media` asks of a client."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls = 0

    async def download_media(self, _message: Any) -> Any:
        self.calls += 1
        return self.payload


def _temp_suffix(desc: MediaDescription) -> str:
    """The suffix the shared layer will put on the downloaded temp file.

    Derived exactly as :func:`kiro_crew.messaging.attachments.ingest_attachments`
    derives it, so a test asserts what the transcription backend will really see
    rather than what the attachment merely hints at.
    """
    att = attachment_for(desc, "msg-1")
    return safe_suffix(att.suffix_hint or att.name.rsplit(".", 1)[-1])


# ── the filename handed to the shared layer ─────────────────────────────────
class TestSuffix:
    def test_a_voice_note_gets_the_pinned_ogg_suffix(self) -> None:
        """``.ogg``, never the stdlib's ``.oga``.

        WhatsApp sends voice notes with no filename at all, and the transcription
        backend selects its decoder from the suffix, so the derived one is the
        only thing standing between a voice note and an empty transcript.
        """
        desc = MediaDescription(kind=KIND_VOICE, mimetype=_VOICE_MIMETYPE, is_voice_note=True)
        assert attachment_for(desc, "msg-1").name == "voice.ogg"
        assert _temp_suffix(desc) == ".ogg"

    def test_the_ogg_override_is_load_bearing(self) -> None:
        """Without the pin the stdlib answers a suffix the decoder rejects.

        Asserting the override's VALUE alone would keep passing if the stdlib
        started agreeing; asserting the disagreement is what shows the entry is
        still doing work.
        """
        assert mimetypes.guess_extension("audio/ogg") != ".ogg"
        assert wa_attachments._SUFFIX_OVERRIDES["audio/ogg"] == ".ogg"

    def test_a_document_declared_generic_keeps_its_own_extension(self) -> None:
        """``report.pdf`` sent as ``application/octet-stream`` stays ``report.pdf``.

        The declared type is only the sender's claim, and ``octet-stream`` derives
        ``.bin``: appending that produces ``report.pdf.bin``, which matches no
        document parser, so the shared layer refuses the file WITHOUT downloading
        it. The extension on the name is the more specific claim.
        """
        desc = MediaDescription(
            kind=KIND_DOCUMENT,
            mimetype="application/octet-stream",
            filename="report.pdf",
            file_length=4096,
        )
        att = attachment_for(desc, "msg-1")
        assert att.name == "report.pdf"
        assert classify(att.mimetype, att.name) == DOCUMENT

    def test_a_doubled_suffix_is_what_refusal_looks_like(self) -> None:
        """The mechanism the test above guards, stated directly.

        Pinned so a reader can see WHY the name matters: the same bytes under the
        doubled name reach the shared layer's "unsupported type" branch.
        """
        assert classify("application/octet-stream", "report.pdf.bin") == OTHER

    def test_a_document_with_no_extension_takes_the_derived_one(self) -> None:
        """The gap the derived suffix exists to fill: a name with nothing to read."""
        desc = MediaDescription(kind=KIND_DOCUMENT, mimetype="application/pdf", filename="report")
        att = attachment_for(desc, "msg-1")
        assert att.name == "report.pdf"
        assert classify(att.mimetype, att.name) == DOCUMENT

    def test_a_name_that_already_carries_the_derived_suffix_is_not_doubled(self) -> None:
        desc = MediaDescription(kind=KIND_DOCUMENT, mimetype="application/pdf", filename="a.pdf")
        assert attachment_for(desc, "msg-1").name == "a.pdf"

    def test_an_extension_in_another_case_is_still_an_extension(self) -> None:
        """``Report.PDF`` must not become ``Report.PDF.pdf``."""
        desc = MediaDescription(
            kind=KIND_DOCUMENT, mimetype="application/pdf", filename="Report.PDF"
        )
        assert attachment_for(desc, "msg-1").name == "Report.PDF"

    def test_the_pinned_suffix_outranks_a_sender_chosen_one(self) -> None:
        """A sender-named ``note.oga`` reaches the unrecognised suffix by another route.

        The name is left alone (it is what the user sees quoted back), but the
        temp file the transcription backend opens gets the pinned ``.ogg``.
        """
        desc = MediaDescription(kind=KIND_AUDIO, mimetype="audio/ogg", filename="note.oga")
        assert attachment_for(desc, "msg-1").name == "note.oga"
        assert _temp_suffix(desc) == ".ogg"

    def test_an_unknown_declared_type_leaves_the_name_alone(self) -> None:
        desc = MediaDescription(kind=KIND_DOCUMENT, mimetype="application/x-nope", filename="thing")
        assert attachment_for(desc, "msg-1").name == "thing"

    @pytest.mark.parametrize(
        ("kind", "mimetype", "want"),
        [
            (KIND_IMAGE, "image/jpeg", "image.jpg"),
            (KIND_STICKER, "image/webp", "sticker.webp"),
            (KIND_VOICE, _VOICE_MIMETYPE, "voice.ogg"),
            (KIND_AUDIO, "audio/mpeg", "audio.mp3"),
        ],
    )
    def test_a_nameless_kind_gets_a_name_for_its_kind(
        self, kind: str, mimetype: str, want: str
    ) -> None:
        assert attachment_for(MediaDescription(kind=kind, mimetype=mimetype), "m").name == want


class TestAttachmentFields:
    def test_the_message_id_is_the_source_handle(self) -> None:
        """Opaque, but never blank: the shared layer rejects a blank url unread."""
        desc = MediaDescription(kind=KIND_IMAGE, mimetype="image/png", file_length=len(_PNG))
        assert attachment_for(desc, "3EB0ABC").url == "3EB0ABC"
        assert attachment_for(desc, "").url

    def test_the_declared_length_rides_along(self) -> None:
        desc = MediaDescription(kind=KIND_IMAGE, mimetype="image/png", file_length=1234)
        assert attachment_for(desc, "m").size == 1234

    def test_the_parameterized_type_is_stripped_before_it_is_compared(self) -> None:
        """The shared classifier matches image types by EXACT string."""
        desc = MediaDescription(kind=KIND_VOICE, mimetype=_VOICE_MIMETYPE)
        assert attachment_for(desc, "m").mimetype == "audio/ogg"


# ── the byte ceiling ────────────────────────────────────────────────────────
class TestMediaCeiling:
    def test_the_ceiling_is_24_mib(self) -> None:
        """OUR policy, not a platform figure, so the number itself is the contract.

        Neither whatsmeow nor neonize declares a byte cap, so this is what bounds
        the bytes one inbound message can make the gateway hold in memory.
        """
        assert MAX_MEDIA_BYTES == 24 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_a_download_past_the_ceiling_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wa_attachments, "MAX_MEDIA_BYTES", len(_PNG) - 1)
        client = _FakeClient(_PNG)
        result = await ingest_media(
            client,
            object(),
            MediaDescription(kind=KIND_IMAGE, mimetype="image/png", file_length=len(_PNG)),
            "m",
        )
        try:
            assert result.image_paths == []
            assert result.rejections, "an oversize object must be spoken, not swallowed"
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_an_undeclared_length_is_refused_before_any_fetch(self) -> None:
        """The one input that made the shared pre-check skip itself.

        The shared layer refuses on `att.size and att.size > cap`, so a declared
        length of 0 skipped the pre-download check entirely and the ceiling could
        only act once the whole object was already in memory. Every media protobuf
        carries `fileLength` and `describe` reads it for every kind, so an absent
        length is not a legitimate shape.
        """
        client = _FakeClient(_PNG)
        result = await ingest_media(
            client, object(), MediaDescription(kind=KIND_IMAGE, mimetype="image/png"), "m"
        )
        try:
            assert result.image_paths == [], "media with no declared length was fetched"
            assert client.calls == 0, "the fetch must not happen at all"
            assert result.rejections, "the refusal must be surfaced, not silent"
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_an_oversized_declaration_is_refused_before_any_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refusing on the CLAIM is the only bound that precedes the allocation.

        Deliberately AUDIO at 24.5 MiB, which is the one window this check owns:
        the shared layer's own pre-check uses a per-kind cap, and audio's is 25 MiB,
        so a declaration above this channel's 24 MiB ceiling but under that cap
        passes upstream and only this refusal stops it. An image fixture would be
        caught by the shared cap instead, and the test would pass with this check
        deleted. Memory is stubbed ABUNDANT for the same reason: otherwise the
        starved-host guard satisfies it on a busy machine.
        """
        monkeypatch.setattr(wa_attachments, "host_available_mib", lambda: 1024 * 1024)
        client = _FakeClient(_PNG)
        result = await ingest_media(
            client,
            object(),
            MediaDescription(
                kind=KIND_AUDIO,
                mimetype="audio/ogg",
                file_length=24 * 1024 * 1024 + 512 * 1024,
            ),
            "m",
        )
        try:
            assert client.calls == 0, "an oversized declaration must not be fetched"
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_a_starved_host_refuses_before_allocating(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fetch must not be the allocation that tips a starved host over."""
        monkeypatch.setattr(wa_attachments, "host_available_mib", lambda: 1)
        client = _FakeClient(_PNG)
        result = await ingest_media(
            client,
            object(),
            MediaDescription(kind=KIND_IMAGE, mimetype="image/png", file_length=8 * 1024 * 1024),
            "m",
        )
        try:
            assert client.calls == 0
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_an_unreadable_memory_reading_still_allows_the_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0 means "could not determine", never "no memory".

        Treating it as no memory would disable inbound media on any host whose
        reading is unavailable, which is the failure mode the probe's own contract
        warns about.
        """
        monkeypatch.setattr(wa_attachments, "host_available_mib", lambda: 0)
        client = _FakeClient(_PNG)
        result = await ingest_media(
            client,
            object(),
            MediaDescription(kind=KIND_IMAGE, mimetype="image/png", file_length=len(_PNG)),
            "m",
        )
        try:
            assert len(result.image_paths) == 1
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_exactly_the_ceiling_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The bound is inclusive, so the two tests together pin the comparison."""
        monkeypatch.setattr(wa_attachments, "MAX_MEDIA_BYTES", len(_PNG))
        client = _FakeClient(_PNG)
        result = await ingest_media(
            client,
            object(),
            MediaDescription(kind=KIND_IMAGE, mimetype="image/png", file_length=len(_PNG)),
            "m",
        )
        try:
            assert len(result.image_paths) == 1
            assert Path(result.image_paths[0]).read_bytes() == _PNG
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_the_ceiling_binds_on_what_arrives_not_on_what_was_declared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``file_length`` is the sender's claim, so it cannot be the bound.

        A sender who declares one byte and ships megabytes must still be refused,
        which is the only reason this check lives after the fetch rather than
        beside the shared layer's advisory size test.
        """
        monkeypatch.setattr(wa_attachments, "MAX_MEDIA_BYTES", len(_PNG) - 1)
        client = _FakeClient(_PNG)
        desc = MediaDescription(kind=KIND_IMAGE, mimetype="image/png", file_length=1)
        result = await ingest_media(client, object(), desc, "m")
        try:
            assert result.image_paths == []
            assert result.rejections
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_a_download_that_returns_no_bytes_is_a_rejection_not_a_raise(self) -> None:
        """Losing the whole message over one failed photo is worse than saying so."""
        result = await ingest_media(
            _FakeClient(None),
            object(),
            MediaDescription(kind=KIND_IMAGE, mimetype="image/png", file_length=len(_PNG)),
            "m",
        )
        try:
            assert result.image_paths == []
            assert result.rejections
        finally:
            cleanup(result.temp_paths)


# ── the offload and the wiring ──────────────────────────────────────────────
class TestIngestWiring:
    @pytest.mark.asyncio
    async def test_the_write_happens_off_the_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``TMPDIR`` is not guaranteed to be local disk.

        A network- or FUSE-backed temp dir makes an inline write stall the one
        gateway loop, and with it every other session and the liveness heartbeat.
        """
        on_loop: list[bool] = []

        class _RecordingPath:
            """A ``Path`` that reports which thread its write ran on.

            Delegates everything else, because the module builds a ``Path`` for
            the attachment's own name too. Composition rather than a subclass:
            ``pathlib.Path`` is not subclassable before 3.12 and this repo
            supports 3.10.
            """

            def __init__(self, dest: str) -> None:
                self._real = Path(dest)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real, name)

            def write_bytes(self, data: bytes) -> int:
                try:
                    asyncio.get_running_loop()
                    on_loop.append(True)
                except RuntimeError:
                    on_loop.append(False)
                return self._real.write_bytes(data)

        monkeypatch.setattr(wa_attachments, "Path", _RecordingPath)
        result = await ingest_media(
            _FakeClient(_PNG),
            object(),
            MediaDescription(kind=KIND_IMAGE, mimetype="image/png", file_length=len(_PNG)),
            "m",
        )
        try:
            assert on_loop == [False], "the write must run on a worker thread"
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_the_shared_layer_is_handed_this_channels_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One media object per message, audio handled here, ``audio/ogg`` declared."""
        seen: dict[str, Any] = {}

        async def _capture(attachments: list[Any], **kwargs: Any) -> Any:
            seen["attachments"] = attachments
            seen.update(kwargs)
            return wa_attachments.IngestResult()

        monkeypatch.setattr(wa_attachments, "ingest_attachments", _capture)
        await ingest_media(
            _FakeClient(_PNG),
            object(),
            MediaDescription(kind=KIND_IMAGE, mimetype="image/png", file_length=len(_PNG)),
            "m",
        )
        assert seen["source"] == "whatsapp"
        assert seen["handle_audio"] is True
        assert seen["audio_mimetypes"] == AUDIO_MIMETYPES
        assert seen["limits"].max_attachments == MAX_ATTACHMENTS_PER_MESSAGE
        assert MAX_ATTACHMENTS_PER_MESSAGE == 1

    @pytest.mark.asyncio
    async def test_a_voice_note_is_transcribed_even_with_no_audio_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The KIND is the trigger, not the shared layer's bookkeeping.

        A voice note whose bytes the shared layer classified some other way still
        has to reach transcription, or the operator's spoken message is silence.
        """
        called: list[str] = []

        async def _capture(result: Any, source: str) -> Any:
            called.append(source)
            return result

        async def _no_paths(_attachments: list[Any], **_kwargs: Any) -> Any:
            return wa_attachments.IngestResult()

        monkeypatch.setattr(wa_attachments, "ingest_attachments", _no_paths)
        monkeypatch.setattr(wa_attachments, "transcribe_audio_attachments", _capture)
        await ingest_media(
            _FakeClient(b""), object(), MediaDescription(kind=KIND_VOICE, mimetype="audio/ogg"), "m"
        )
        assert called == ["WhatsApp"]

    @pytest.mark.asyncio
    async def test_an_image_is_not_sent_to_transcription(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[str] = []

        async def _capture(result: Any, source: str) -> Any:
            called.append(source)
            return result

        monkeypatch.setattr(wa_attachments, "transcribe_audio_attachments", _capture)
        result = await ingest_media(
            _FakeClient(_PNG),
            object(),
            MediaDescription(kind=KIND_IMAGE, mimetype="image/png", file_length=len(_PNG)),
            "m",
        )
        try:
            assert called == []
            assert classify("image/png", "image.png") == IMAGE
        finally:
            cleanup(result.temp_paths)
