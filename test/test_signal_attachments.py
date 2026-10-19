"""Signal inbound files: envelope parsing, getAttachment, the shared ingest, the gates.

The client's peer is a recording fake and the transport's client a recording
subclass, so these exercise the Signal semantics without a child process; the
real framing against a fake ``signal-cli`` is ``test_signal_files_e2e.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.imessage.rpc import STDOUT_LINE_LIMIT
from kiro_crew.messaging.attachments import cleanup
from kiro_crew.signal_cli import attachments as sig_attachments
from kiro_crew.signal_cli import transport as transport_mod
from kiro_crew.signal_cli.attachments import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_MEDIA_BYTES,
    attachment_for,
    ingest_inbound_attachments,
)
from kiro_crew.signal_cli.client import (
    KIND_DATA,
    KIND_NOTE_TO_SELF,
    SignalAttachmentRef,
    SignalClient,
    SignalInbound,
    parse_receive,
)
from kiro_crew.signal_cli.rpc import RpcError, RpcTransportError
from kiro_crew.signal_cli.transport import SignalTransport
from kiro_crew.testing.wait import await_until

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels" / "signal"

ACCOUNT = "+447700900001"
OWNER = "+447700900002"
OWNER_ACI = "bbbbbbbb-2222-4222-8222-222222222222"
STRANGER = "+447700900009"

#: A real 2x2 PNG: the shared layer sniffs leading bytes, so a stub would be refused.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)


def _params(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["params"]


class FakePeer:
    """Records calls; serves ``getAttachment`` from a dict of id -> bytes."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.files = files or {}
        self.fail: BaseException | None = None
        self.ts = 1700000001000

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params or {})))
        if self.fail is not None:
            raise self.fail
        if method == "getAttachment":
            data = self.files.get(str((params or {}).get("id")))
            if data is None:
                raise RpcError(-1, "Could not find attachment with ID")
            return {"data": base64.b64encode(data).decode("ascii")}
        if method in ("send", "sendReaction"):
            self.ts += 1
            return {"timestamp": self.ts}
        return {}


def _client(files: dict[str, bytes] | None = None) -> tuple[SignalClient, FakePeer]:
    client = SignalClient(ACCOUNT)
    peer = FakePeer(files)
    client._peer = peer  # type: ignore[assignment]
    client.ready.set()
    return client, peer


def _dm(attachments: list[dict[str, Any]], text: str | None = "look") -> dict[str, Any]:
    return {
        "account": ACCOUNT,
        "envelope": {
            "sourceNumber": OWNER,
            "sourceUuid": OWNER_ACI,
            "timestamp": 1700000000500,
            "dataMessage": {
                "timestamp": 1700000000500,
                "message": text,
                "attachments": attachments,
            },
        },
    }


# ── envelope parsing ────────────────────────────────────────────────────────
class TestParsing:
    def test_the_recorded_shape_yields_a_fetchable_ref(self) -> None:
        inbound = parse_receive(_params("receive_dm_attachments"), ACCOUNT)
        assert inbound is not None and inbound.kind == KIND_DATA
        assert inbound.attachments == 1
        assert inbound.attachment_refs == [
            SignalAttachmentRef(
                id="Xq3vT0aZ9bQ1.png",
                content_type="image/png",
                filename="chart.png",
                size=67,
                voice_note=False,
            )
        ]

    def test_entries_without_an_id_or_of_the_wrong_type_are_skipped(self) -> None:
        inbound = parse_receive(
            _dm([{"contentType": "image/png"}, "junk", {"id": 7}, {"id": "a.aac", "size": -5}])
        )
        assert inbound is not None
        assert [r.id for r in inbound.attachment_refs] == ["a.aac"]
        assert inbound.attachment_refs[0].size == 0  # a negative claim reads as absent

    def test_a_voice_note_is_flagged(self) -> None:
        inbound = parse_receive(
            _dm([{"id": "v.aac", "contentType": "audio/aac", "isVoiceNote": True}], text=None)
        )
        assert inbound is not None and inbound.text == ""
        assert inbound.attachment_refs[0].voice_note is True

    def test_a_long_text_attachment_is_not_the_body_a_second_time(self) -> None:
        # Signal sends a body over ~2000 chars as a text/x-signal-plain
        # attachment, and signal-cli ALSO inlines it into ``message``: fetching
        # that entry would hand the person's text to the turn twice.
        body = "x" * 2500
        inbound = parse_receive(
            _dm(
                [
                    {"id": "long.txt", "contentType": "text/x-signal-plain", "size": 2500},
                    {"id": "a.png", "contentType": "image/png", "size": 67},
                ],
                text=body,
            )
        )
        assert inbound is not None and inbound.text == body
        assert [r.id for r in inbound.attachment_refs] == ["a.png"]
        assert inbound.attachments == 1

    def test_a_long_text_attachment_without_a_body_is_kept(self) -> None:
        # Without an inlined body the attachment is the only copy of the text.
        inbound = parse_receive(
            _dm([{"id": "long.txt", "contentType": "text/x-signal-plain"}], text=None)
        )
        assert inbound is not None
        assert [r.id for r in inbound.attachment_refs] == ["long.txt"]

    def test_the_recorded_note_to_self_reaction_is_a_reaction(self) -> None:
        inbound = parse_receive(_params("receive_reaction_note_to_self"), ACCOUNT)
        assert inbound is not None and inbound.kind == KIND_NOTE_TO_SELF
        assert inbound.is_reaction is True and inbound.text == ""

    def test_spool_text_is_the_original_once_rewritten(self) -> None:
        inbound = SignalInbound(kind=KIND_DATA, text="hello")
        assert inbound.spool_text == "hello"
        inbound.original_text = ""
        inbound.text = "/tmp/x.png\n\n[Attached file: x]"
        assert inbound.spool_text == ""


# ── getAttachment ───────────────────────────────────────────────────────────
class TestGetAttachment:
    @pytest.mark.asyncio
    async def test_the_bytes_are_decoded_from_the_answer(self) -> None:
        client, peer = _client({"a.png": _PNG})
        assert await client.get_attachment("a.png", OWNER) == _PNG
        assert peer.calls == [("getAttachment", {"id": "a.png", "recipient": OWNER})]

    @pytest.mark.asyncio
    async def test_a_missing_copy_is_a_spoken_refusal(self) -> None:
        client, _ = _client()
        with pytest.raises(ValueError, match="no stored copy"):
            await client.get_attachment("gone.png", OWNER)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", [{}, {"data": 5}, {"data": "not base64!"}])
    async def test_an_unreadable_answer_is_a_spoken_refusal(self, answer: Any) -> None:
        client, peer = _client()

        async def call(*_a: Any, **_k: Any) -> dict[str, Any]:
            return answer

        peer.call = call  # type: ignore[method-assign]
        with pytest.raises(ValueError):
            await client.get_attachment("a.png", OWNER)

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_not_disguised(self) -> None:
        client, peer = _client()
        peer.fail = RpcTransportError("signal-cli is not running")
        with pytest.raises(RpcTransportError):
            await client.get_attachment("a.png", OWNER)


# ── the shared Attachment ───────────────────────────────────────────────────
class TestAttachmentFor:
    def test_a_voice_note_gets_the_aac_suffix_the_decoder_needs(self) -> None:
        att = attachment_for(SignalAttachmentRef(id="v", content_type="audio/aac", voice_note=True))
        assert (att.name, att.suffix_hint, att.mimetype, att.url) == (
            "voice.aac",
            "aac",
            "audio/aac",
            "v",
        )

    def test_a_document_keeps_its_own_extension(self) -> None:
        att = attachment_for(
            SignalAttachmentRef(id="d", content_type="application/octet-stream", filename="r.pdf")
        )
        assert att.name == "r.pdf" and att.suffix_hint == "pdf"

    @pytest.mark.parametrize("name", ["../../etc/passwd", "..\\..\\x\\passwd", "/abs/passwd"])
    def test_a_sender_path_is_reduced_to_its_last_component(self, name: str) -> None:
        att = attachment_for(SignalAttachmentRef(id="d", content_type="text/plain", filename=name))
        # Only the final component survives; the derived suffix fills the gap.
        assert att.name == "passwd.txt"

    def test_the_declared_size_rides_along_as_advisory(self) -> None:
        assert (
            attachment_for(SignalAttachmentRef(id="i", content_type="image/png", size=9)).size == 9
        )


# ── ingest ──────────────────────────────────────────────────────────────────
class TestIngest:
    def test_the_ceiling_fits_a_base64_answer_on_one_line(self) -> None:
        assert MAX_MEDIA_BYTES > 0
        assert len(base64.b64encode(b"\0" * MAX_MEDIA_BYTES)) < STDOUT_LINE_LIMIT
        assert MAX_ATTACHMENTS_PER_MESSAGE == 4

    @pytest.mark.asyncio
    async def test_an_image_lands_as_an_image_path(self) -> None:
        client, _ = _client({"a.png": _PNG})
        refs = [SignalAttachmentRef(id="a.png", content_type="image/png", size=len(_PNG))]
        result = await ingest_inbound_attachments(client, refs, OWNER)
        try:
            assert len(result.image_paths) == 1
            assert Path(result.image_paths[0]).read_bytes() == _PNG
            assert result.rejections == []
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_an_oversized_declaration_is_refused_before_any_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sig_attachments, "host_available_mib", lambda: 1024 * 1024)
        client, peer = _client({"a.aac": b"x"})
        refs = [SignalAttachmentRef(id="a.aac", content_type="audio/aac", size=MAX_MEDIA_BYTES + 1)]
        result = await ingest_inbound_attachments(client, refs, OWNER)
        try:
            assert peer.calls == []
            assert result.rejections and "limit" in result.rejections[0]
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_an_understated_object_is_refused_after_the_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sig_attachments, "MAX_MEDIA_BYTES", len(_PNG) - 1)
        client, _ = _client({"a.png": _PNG})
        refs = [SignalAttachmentRef(id="a.png", content_type="image/png", size=1)]
        result = await ingest_inbound_attachments(client, refs, OWNER)
        try:
            assert result.image_paths == [] and result.rejections
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_a_starved_host_refuses_before_allocating(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sig_attachments, "host_available_mib", lambda: 1)
        client, peer = _client({"a.png": _PNG})
        refs = [SignalAttachmentRef(id="a.png", content_type="image/png", size=4 * 1024 * 1024)]
        result = await ingest_inbound_attachments(client, refs, OWNER)
        try:
            assert peer.calls == [] and result.rejections
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_a_missing_copy_becomes_a_rejection_not_a_lost_message(self) -> None:
        client, _ = _client({"b.png": _PNG})
        refs = [
            SignalAttachmentRef(id="gone.png", content_type="image/png"),
            SignalAttachmentRef(id="b.png", content_type="image/png"),
        ]
        result = await ingest_inbound_attachments(client, refs, OWNER)
        try:
            assert len(result.image_paths) == 1
            assert any("no stored copy" in r for r in result.rejections)
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_a_voice_note_goes_through_the_shared_transcription(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []

        async def _transcribe(result: Any, source: str) -> Any:
            seen.append([Path(p).suffix for p in result.audio_paths])
            assert source == "Signal"
            result.text_blocks.append("[Voice memo transcription]\nhi\n[End of transcription]")
            return result

        monkeypatch.setattr(sig_attachments, "transcribe_audio_attachments", _transcribe)
        client, _ = _client({"v.aac": b"\xff\xf1fake-aac"})
        refs = [SignalAttachmentRef(id="v.aac", content_type="audio/aac", voice_note=True)]
        result = await ingest_inbound_attachments(client, refs, OWNER)
        try:
            assert seen == [[".aac"]]
            assert "Voice memo transcription" in result.text_blocks[0]
        finally:
            cleanup(result.temp_paths)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc", [asyncio.CancelledError, RuntimeError])
    async def test_an_interrupted_transcription_deletes_the_decrypted_files(
        self, monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
    ) -> None:
        # The caller owns temp_paths only once ingest returns; a transcription
        # cancelled after the ingest wrote the files must not leave the
        # sender's decrypted bytes on disk with nobody holding their paths.
        staged: list[str] = []

        async def _transcribe(result: Any, source: str) -> Any:
            staged.extend(result.temp_paths)
            raise exc()

        monkeypatch.setattr(sig_attachments, "transcribe_audio_attachments", _transcribe)
        client, _ = _client({"v.aac": b"\xff\xf1fake-aac", "a.png": _PNG})
        refs = [
            SignalAttachmentRef(id="a.png", content_type="image/png"),
            SignalAttachmentRef(id="v.aac", content_type="audio/aac", voice_note=True),
        ]
        with pytest.raises(exc):
            await ingest_inbound_attachments(client, refs, OWNER)
        assert len(staged) == 2
        assert [p for p in staged if Path(p).exists()] == []

    @pytest.mark.asyncio
    async def test_more_than_the_cap_is_counted_not_fetched(self) -> None:
        files = {f"{i}.png": _PNG for i in range(MAX_ATTACHMENTS_PER_MESSAGE + 2)}
        client, peer = _client(files)
        refs = [SignalAttachmentRef(id=i, content_type="image/png") for i in files]
        result = await ingest_inbound_attachments(client, refs, OWNER)
        try:
            assert len(peer.calls) == MAX_ATTACHMENTS_PER_MESSAGE
            assert any("2 more attachment" in r for r in result.rejections)
        finally:
            cleanup(result.temp_paths)


# ── the transport's gates ───────────────────────────────────────────────────
class RecordingClient(SignalClient):
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        super().__init__(ACCOUNT)
        self.files = files or {}
        self.fetched: list[str] = []
        self.sent: list[tuple[str, str, list[str]]] = []

    async def get_attachment(self, attachment_id: str, sender: str) -> bytes:
        self.fetched.append(attachment_id)
        return self.files[attachment_id]

    async def send(self, recipient: str, text: str, *, attachments: Any = ()) -> str:
        self.sent.append((recipient, text, list(attachments)))
        return "1700000009999"


@pytest.fixture
def quiet_sel(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Sel:
        def log_api_access(self, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(transport_mod, "sel", lambda: _Sel())


def _transport(
    client: RecordingClient, *allowed: str, permitted: bool = True, monkeypatch: Any
) -> tuple[SignalTransport, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    async def dispatch(inbound: SignalInbound) -> None:
        # A temp path is a line of its own; on Windows it starts with a drive.
        paths = [ln for ln in inbound.text.splitlines() if os.path.isabs(ln)]
        seen.append(
            {
                "text": inbound.text,
                "spool": inbound.spool_text,
                "paths": paths,
                "exist": [Path(p).exists() for p in paths],
            }
        )

    async def _permitted(_channel: str, **_kwargs: Any) -> bool:
        return permitted

    monkeypatch.setattr(transport_mod, "inbound_permitted", _permitted)
    return SignalTransport(client, allowed_numbers=allowed, dispatch=dispatch), seen


class TestTransportGates:
    @pytest.mark.asyncio
    async def test_an_allowed_sender_s_picture_reaches_the_turn_and_is_deleted_after(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None
    ) -> None:
        client = RecordingClient({"Xq3vT0aZ9bQ1.png": _PNG})
        transport, seen = _transport(client, OWNER, monkeypatch=monkeypatch)
        await transport.receive(parse_receive(_params("receive_dm_attachments"), ACCOUNT))
        assert client.fetched == ["Xq3vT0aZ9bQ1.png"]
        assert len(seen) == 1 and seen[0]["exist"] == [True]
        assert seen[0]["text"].startswith("what is in this picture?\n")
        # The spool keeps what the person wrote, never the temp path.
        assert seen[0]["spool"] == "what is in this picture?"
        assert not Path(seen[0]["paths"][0]).exists()

    @pytest.mark.asyncio
    async def test_an_attachment_only_message_is_admitted(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None
    ) -> None:
        client = RecordingClient({"a.png": _PNG})
        transport, seen = _transport(client, OWNER, monkeypatch=monkeypatch)
        await transport.receive(
            parse_receive(_dm([{"id": "a.png", "contentType": "image/png"}], text=None), ACCOUNT)
        )
        assert len(seen) == 1 and seen[0]["spool"] == "" and len(seen[0]["paths"]) == 1

    @pytest.mark.asyncio
    async def test_a_long_message_reaches_the_turn_once(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None
    ) -> None:
        body = "long " * 500
        client = RecordingClient({"long.txt": body.encode()})
        transport, seen = _transport(client, OWNER, monkeypatch=monkeypatch)
        await transport.receive(
            parse_receive(
                _dm([{"id": "long.txt", "contentType": "text/x-signal-plain"}], text=body),
                ACCOUNT,
            )
        )
        assert client.fetched == []
        assert len(seen) == 1 and seen[0]["text"] == body

    @pytest.mark.asyncio
    async def test_a_stranger_s_attachment_is_never_fetched(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None
    ) -> None:
        client = RecordingClient({"Xq3vT0aZ9bQ1.png": _PNG})
        transport, seen = _transport(client, STRANGER, monkeypatch=monkeypatch)
        await transport.receive(parse_receive(_params("receive_dm_attachments"), ACCOUNT))
        assert client.fetched == [] and seen == []

    @pytest.mark.asyncio
    async def test_a_governance_deny_stops_the_download_not_only_the_reply(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None
    ) -> None:
        client = RecordingClient({"Xq3vT0aZ9bQ1.png": _PNG})
        transport, seen = _transport(client, OWNER, permitted=False, monkeypatch=monkeypatch)
        await transport.receive(parse_receive(_params("receive_dm_attachments"), ACCOUNT))
        assert client.fetched == [] and seen == []

    @pytest.mark.asyncio
    async def test_a_group_attachment_is_never_fetched(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None
    ) -> None:
        params = _params("receive_dm_attachments")
        params["envelope"]["dataMessage"]["groupInfo"] = {"groupId": "g", "type": "DELIVER"}
        client = RecordingClient({"Xq3vT0aZ9bQ1.png": _PNG})
        transport, seen = _transport(client, OWNER, monkeypatch=monkeypatch)
        await transport.receive(parse_receive(params, ACCOUNT))
        assert client.fetched == [] and seen == []

    @pytest.mark.asyncio
    async def test_temp_files_are_deleted_even_when_the_dispatch_raises(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None
    ) -> None:
        client = RecordingClient({"a.png": _PNG})
        held: list[str] = []

        async def dispatch(inbound: SignalInbound) -> None:
            held.extend(ln for ln in inbound.text.splitlines() if os.path.isabs(ln))
            raise RuntimeError("turn blew up")

        async def _permitted(_channel: str, **_kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(transport_mod, "inbound_permitted", _permitted)
        transport = SignalTransport(client, allowed_numbers=[OWNER], dispatch=dispatch)
        with pytest.raises(RuntimeError):
            await transport.receive(
                parse_receive(_dm([{"id": "a.png", "contentType": "image/png"}]), ACCOUNT)
            )
        assert len(held) == 1 and not Path(held[0]).exists()

    @pytest.mark.asyncio
    async def test_a_reaction_never_becomes_a_turn(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None
    ) -> None:
        client = RecordingClient()
        transport, seen = _transport(client, OWNER, ACCOUNT, monkeypatch=monkeypatch)
        # The channel's own status reaction synced back in Note to Self ...
        await transport.receive(parse_receive(_params("receive_reaction_note_to_self"), ACCOUNT))
        # ... and the owner reacting to one of the agent's DMs.
        dm = _dm([], text=None)
        dm["envelope"]["dataMessage"]["reaction"] = {
            "emoji": "👍",
            "targetAuthorNumber": ACCOUNT,
            "targetSentTimestamp": 1,
            "isRemove": False,
        }
        await transport.receive(parse_receive(dm, ACCOUNT))
        assert seen == []


# ── a message steered into a running turn ───────────────────────────────────
class _SteerRig:
    """The real transport and dispatcher, with a stand-in turn the test releases."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, steer_lands: bool = True) -> None:
        from test_signal_dispatch import FakeProvider, FakeSessions

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.signal_cli import transport_dispatch
        from kiro_crew.signal_cli.transport_dispatch import SignalDispatcher

        self.sessions = FakeSessions()
        self.dispatcher = SignalDispatcher(
            sessions=self.sessions,  # type: ignore[arg-type]
            ctx_builder=object(),  # type: ignore[arg-type]
            cfg=KiroCrewConfig(),
            conv_log=None,
        )
        self.client = RecordingClient({"a.png": _PNG})
        self.dispatcher.client = self.client
        self.transport = SignalTransport(
            self.client, allowed_numbers=[OWNER], dispatch=self.dispatcher.handle_message
        )
        self.dispatcher.transport = self.transport
        self.key = self.dispatcher._session_key(OWNER)
        self.provider = FakeProvider()
        if not steer_lands:

            async def _decline(text: str) -> bool:
                self.provider.steered.append(text)
                return False

            self.provider.steer = _decline  # type: ignore[method-assign]
        self.sessions.providers[self.key] = self.provider
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail: BaseException | None = None

        async def _yes(_channel: str, **_kwargs: Any) -> bool:
            return True

        async def held_turn(turn: Any, **_kwargs: Any) -> None:
            # Holds the session the way a running turn does, until released.
            self.sessions.busy.add(turn.session_key)
            self.started.set()
            try:
                await self.release.wait()
                if self.fail is not None:
                    raise self.fail
            finally:
                self.sessions.busy.discard(turn.session_key)

        self.staged: list[str] = []
        real_ingest = transport_mod.ingest_inbound_attachments

        async def recording_ingest(*args: Any) -> Any:
            result = await real_ingest(*args)
            self.staged.extend(result.temp_paths)
            return result

        monkeypatch.setattr(transport_mod, "ingest_inbound_attachments", recording_ingest)
        monkeypatch.setattr(transport_mod, "inbound_permitted", _yes)
        monkeypatch.setattr(transport_dispatch, "inbound_permitted", _yes)
        monkeypatch.setattr(transport_dispatch, "drive_turn", held_turn)

    async def start_turn(self) -> "asyncio.Future[None]":
        turn = asyncio.ensure_future(
            self.transport.receive(parse_receive(_dm([], text="first"), ACCOUNT))
        )
        await await_until(self.started, what="the first turn holds the session")
        return turn

    async def send_picture(self) -> list[str]:
        """Deliver a captioned picture mid-turn; return the files it staged."""
        await self.transport.receive(
            parse_receive(_dm([{"id": "a.png", "contentType": "image/png"}], text="and this"))
        )
        assert len(self.staged) == 1
        return list(self.staged)

    def receipts(self) -> list[str]:
        return [text for _recipient, text, _files in self.client.sent]


class TestSteeredFiles:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", ["done", "error", "cancel"])
    async def test_a_steered_message_s_files_live_until_the_turn_ends(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None, ending: str
    ) -> None:
        rig = _SteerRig(monkeypatch)
        turn = await rig.start_turn()
        try:
            paths = await rig.send_picture()
            # The steered text names the staged file ...
            assert [p in rig.provider.steered[0] for p in paths] == [True]
            # ... and the steer returned and the transport's dispatch is over, but the
            # turn the text was folded into has not read the file yet.
            assert Path(paths[0]).read_bytes() == _PNG
            assert any("Folded into" in t for t in rig.receipts())
        finally:
            if ending == "cancel":
                turn.cancel()
            else:
                rig.fail = RuntimeError("turn blew up") if ending == "error" else None
                rig.release.set()
        if ending == "done":
            await await_until(turn, what="the turn ends")
        else:
            with pytest.raises(asyncio.CancelledError if ending == "cancel" else RuntimeError):
                await await_until(turn, what="the turn ends")
        # Never leaked: the turn deleted them however it ended.
        assert not Path(paths[0]).exists()
        assert rig.dispatcher._turn_files == {}

    @pytest.mark.asyncio
    async def test_a_declined_steer_gives_the_files_back_to_be_deleted_at_once(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None
    ) -> None:
        rig = _SteerRig(monkeypatch, steer_lands=False)
        turn = await rig.start_turn()
        try:
            paths = await rig.send_picture()
            assert len(rig.provider.steered) == 1 and not Path(paths[0]).exists()
            assert rig.dispatcher._turn_files[rig.key].paths == []
            assert any("please resend" in t for t in rig.receipts())
        finally:
            rig.release.set()
            await await_until(turn, what="the turn ends")

    @pytest.mark.asyncio
    async def test_files_are_not_steered_into_a_turn_no_signal_dispatch_owns(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None
    ) -> None:
        # A dashboard turn holds the mirrored session: nothing of this channel's
        # would delete the files after it read them, so the message is not
        # steered and the transport deletes them.
        rig = _SteerRig(monkeypatch)
        rig.sessions.busy.add(rig.key)
        paths = await rig.send_picture()
        assert rig.provider.steered == [] and not Path(paths[0]).exists()
        assert any("please resend" in t for t in rig.receipts())
        assert rig.dispatcher._turn_files == {}

    @pytest.mark.asyncio
    async def test_a_steered_text_message_still_needs_no_owner(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None
    ) -> None:
        rig = _SteerRig(monkeypatch)
        rig.sessions.busy.add(rig.key)
        await rig.transport.receive(parse_receive(_dm([], text="also this"), ACCOUNT))
        assert rig.provider.steered == ["also this"]


class TestRestartSpool:
    @pytest.mark.asyncio
    async def test_an_uncaptioned_picture_refused_at_shutdown_is_spooled_for_a_notice(
        self, monkeypatch: pytest.MonkeyPatch, quiet_sel: None
    ) -> None:
        import kiro_crew.messaging.dispatch as shared_dispatch
        from kiro_crew.messaging.inbound_spool import _refusal_entry

        rig = _SteerRig(monkeypatch)
        spooled: list[Any] = []

        async def capture(*, channel_type: str, route: Any) -> bool:
            spooled.append((channel_type, route))
            return True

        monkeypatch.setattr(shared_dispatch, "spool_refused_turn", capture)
        rig.sessions.reserve_inbound_callback = lambda: None  # type: ignore[attr-defined]
        await rig.transport.receive(
            parse_receive(_dm([{"id": "a.png", "contentType": "image/png"}], text=None))
        )
        [(channel_type, route)] = spooled
        assert channel_type == "signal" and route.text == ""
        assert route.attachments_dropped == 1
        # The shared spool keeps it (an empty body with no count was dropped).
        entry = _refusal_entry("signal", route)
        assert entry is not None and entry.attachments_dropped == 1
        assert rig.started.is_set() is False
