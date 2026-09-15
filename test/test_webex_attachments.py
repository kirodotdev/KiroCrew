"""Tests for kiro_crew.webex.attachments — inbound file ingest.

Everything channel-neutral (caps, classification, signature sniffing, temp-file
ownership, the SEL audit) belongs to ``messaging/attachments.py`` and is tested
there. What is Webex-specific and tested here: an opaque content URL becomes a
described :class:`Attachment` via a HEAD probe, a probe that fails still yields an
entry so the failure surfaces to the user instead of the file disappearing, and
``process_webex_attachments`` wires the probe, the download and the audio
transcription into the shared ingest.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.messaging.attachments import cleanup
from kiro_crew.webex.attachments import process_webex_attachments, to_attachments


class FakeClient:
    """A client whose HEAD answers are scripted per URL.

    ``bodies`` makes a download actually write bytes, which the shared ingest
    needs: it classifies on the file's SIGNATURE, so a download that writes
    nothing is indistinguishable from a corrupt file and never reaches the
    interesting paths.
    """

    def __init__(
        self,
        answers: dict[str, tuple[str, str, int]] | None = None,
        bodies: dict[str, bytes] | None = None,
    ) -> None:
        self.answers = answers or {}
        self.bodies = bodies or {}
        self.head_calls: list[str] = []
        self.downloads: list[tuple[str, str]] = []

    async def head_content(self, url: str) -> tuple[str, str, int]:
        self.head_calls.append(url)
        return self.answers.get(url, ("", "", 0))

    async def download_content(self, url: str, dest: str) -> None:
        self.downloads.append((url, dest))
        body = self.bodies.get(url)
        if body is not None:
            with open(dest, "wb") as fh:
                fh.write(body)


class TestToAttachments:
    @pytest.mark.asyncio
    async def test_a_probe_describes_the_file(self) -> None:
        url = "https://webexapis.com/v1/contents/C1"
        client = FakeClient({url: ("report.pdf", "application/pdf", 2048)})

        [attachment] = await to_attachments(client, (url,))

        assert attachment.name == "report.pdf"
        assert attachment.mimetype == "application/pdf"
        assert attachment.size == 2048
        assert attachment.url == url

    @pytest.mark.asyncio
    async def test_a_failed_probe_still_yields_an_entry(self) -> None:
        """Silence is the wrong failure here.

        The shared ingest turns an unusable attachment into a visible rejection
        line. Dropping it instead would leave the user believing the agent saw
        their file, which is worse than an error.
        """
        url = "https://webexapis.com/v1/contents/C1"

        [attachment] = await to_attachments(FakeClient(), (url,))

        assert attachment.name == "attachment"
        assert attachment.url == url

    @pytest.mark.asyncio
    async def test_order_is_preserved(self) -> None:
        # The prompt reads the attachments in the order the user attached them.
        urls = tuple(f"https://webexapis.com/v1/contents/C{i}" for i in range(4))
        client = FakeClient({u: (f"f{i}.txt", "text/plain", 1) for i, u in enumerate(urls)})

        names = [a.name for a in await to_attachments(client, urls)]

        assert names == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]

    @pytest.mark.asyncio
    async def test_no_urls_makes_no_requests(self) -> None:
        client = FakeClient()
        assert await to_attachments(client, ()) == []
        assert client.head_calls == []

    @pytest.mark.asyncio
    async def test_a_hostile_filename_cannot_steer_the_temp_path(self) -> None:
        """The name comes from a Content-Disposition header, so it is untrusted.

        ``safe_suffix`` keeps exactly one leading dot (a suffix needs one) and
        strips every other non-alphanumeric character, so no separator, traversal
        segment or NUL can reach the path a temp file is created at.
        """
        url = "https://webexapis.com/v1/contents/C1"
        client = FakeClient({url: ("../../etc/passwd", "text/plain", 10)})

        [attachment] = await to_attachments(client, (url,))

        hint = attachment.suffix_hint
        assert hint.startswith(".") and hint.count(".") == 1
        assert hint[1:].isalnum()
        for hostile in ("/", "\\", "..", "\x00"):
            assert hostile not in hint


class TestProcessWebexAttachments:
    """The Webex half of ingest: probe -> download -> shared ingest -> transcribe."""

    @pytest.mark.asyncio
    async def test_a_text_file_becomes_prompt_material(self) -> None:
        url = "https://webexapis.com/v1/contents/C1"
        client = FakeClient(
            {url: ("notes.txt", "text/plain", 11)},
            {url: b"hello there"},
        )
        inbound = SimpleNamespace(file_urls=(url,))

        result = await process_webex_attachments(client, inbound)  # type: ignore[arg-type]

        try:
            # The download went through the client's own content endpoint, which is
            # what carries the bot's Authorization header — a plain fetch of that URL
            # is unauthenticated and would 401.
            assert client.downloads and client.downloads[0][0] == url
            assert any("hello there" in block for block in result.text_blocks)
        finally:
            # ``finally`` so a failed assertion still drops the downloaded bytes:
            # a bypassed cleanup leaves temp residue that trips the suite's
            # residue reporting and mis-attributes it to the next test.
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_no_files_does_no_work(self) -> None:
        client = FakeClient()
        inbound = SimpleNamespace(file_urls=())

        result = await process_webex_attachments(client, inbound)  # type: ignore[arg-type]

        assert client.head_calls == []
        assert client.downloads == []
        assert result.text_blocks == []

    @pytest.mark.asyncio
    async def test_audio_is_transcribed_here(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``handle_audio=True`` because Webex offers no server-side transcript.

        iLink voice clips arrive with one and are preferred; a Webex clip has to be
        downloaded and run through local STT, so this is the channel that must ask
        the shared ingest to keep the audio bytes.
        """
        import os

        url = "https://webexapis.com/v1/contents/C1"
        client = FakeClient(
            {url: ("voice.ogg", "audio/ogg", 36)},
            {url: b"OggS" + b"\x00" * 32},
        )
        transcribed: list[str] = []

        async def _transcribe(path: str) -> str:
            assert os.path.exists(path), "STT must run against the downloaded bytes"
            transcribed.append(path)
            return "spoken words"

        monkeypatch.setattr("kiro_crew.transcribe.is_available", lambda: True)
        monkeypatch.setattr("kiro_crew.transcribe.transcribe_audio", _transcribe)
        inbound = SimpleNamespace(file_urls=(url,))

        result = await process_webex_attachments(client, inbound)  # type: ignore[arg-type]

        try:
            assert transcribed == result.audio_paths
            assert any("spoken words" in block for block in result.text_blocks)
        finally:
            cleanup(result.temp_paths)
