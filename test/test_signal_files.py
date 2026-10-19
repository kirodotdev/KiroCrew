"""Signal outbound files and status reactions: client verbs, planning, the renderer.

The renderer's client is a recording fake; the real framing against a fake
``signal-cli`` is ``test_signal_files_e2e.py``.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from kiro_crew.messaging import privacy_mode
from kiro_crew.messaging.outbound_files import OutboundFile, Rejection
from kiro_crew.signal_cli import files as files_mod
from kiro_crew.signal_cli.client import KIND_NOTE_TO_SELF, SignalClient, SignalInbound
from kiro_crew.signal_cli.files import (
    SIGNAL_MAX_REJECTION_LINES,
    data_uri,
    plan_uploads,
    rejection_note,
)
from kiro_crew.signal_cli.renderer import (
    REACTION_DONE,
    REACTION_FAILED,
    REACTION_WORKING,
    SignalRenderer,
)
from kiro_crew.signal_cli.rpc import RpcError, RpcTransportError
from kiro_crew.signal_cli.transport import SIGNAL_CAPABILITIES, SignalTransport

ACCOUNT = "+447700900001"
OWNER = "+447700900002"
STRANGER = "+447700900009"
SESSION = "signal:kirocrew:direct:+447700900002"

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)


class FakePeer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: BaseException | None = None
        self.ts = 1700000001000

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params or {})))
        if self.fail is not None:
            raise self.fail
        self.ts += 1
        return {"timestamp": self.ts}


def _client() -> tuple[SignalClient, FakePeer]:
    client = SignalClient(ACCOUNT)
    peer = FakePeer()
    client._peer = peer  # type: ignore[assignment]
    client.ready.set()
    return client, peer


def _decode(uri: str) -> tuple[str, str, bytes]:
    head, payload = uri.split(",", 1)
    assert head.startswith("data:") and head.endswith(";base64")
    parts = head[len("data:") : -len(";base64")].split(";")
    mime = parts[0]
    name = unquote(parts[1].split("=", 1)[1]) if len(parts) > 1 else ""
    return mime, name, base64.b64decode(payload)


# ── client verbs ────────────────────────────────────────────────────────────
class TestClientSend:
    @pytest.mark.asyncio
    async def test_attachments_ride_on_send(self) -> None:
        client, peer = _client()
        await client.send(OWNER, "caption", attachments=["data:image/png;base64,AA=="])
        assert peer.calls == [
            (
                "send",
                {
                    "recipient": [OWNER],
                    "message": "caption",
                    "attachments": ["data:image/png;base64,AA=="],
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_an_attachment_only_send_goes_out(self) -> None:
        client, peer = _client()
        mid = await client.send(ACCOUNT, "", attachments=["data:image/png;base64,AA=="])
        assert mid == str(peer.ts)
        assert peer.calls[0][1]["noteToSelf"] is True

    @pytest.mark.asyncio
    async def test_its_echo_matches_by_timestamp_only(self) -> None:
        client, peer = _client()
        await client.send(ACCOUNT, "", attachments=["data:image/png;base64,AA=="])
        # The owner's own caption-less picture is NOT swallowed as the echo ...
        mine = SignalInbound(kind=KIND_NOTE_TO_SELF, text="", source_number=ACCOUNT, timestamp=5)
        assert client.is_own_echo(mine) is False
        # ... the echo carrying the send's timestamp is.
        echo = SignalInbound(
            kind=KIND_NOTE_TO_SELF, text="", source_number=ACCOUNT, timestamp=peer.ts
        )
        assert client.is_own_echo(echo) is True


class TestReactions:
    @pytest.mark.asyncio
    async def test_a_dm_reaction_names_recipient_author_and_timestamp(self) -> None:
        client, peer = _client()
        await client.send_reaction(OWNER, "✅", OWNER, 1700000000100)
        assert peer.calls == [
            (
                "sendReaction",
                {
                    "recipient": [OWNER],
                    "emoji": "✅",
                    "targetAuthor": OWNER,
                    "targetTimestamp": 1700000000100,
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_a_note_to_self_reaction_goes_to_note_to_self(self) -> None:
        client, peer = _client()
        await client.send_reaction(ACCOUNT, "⏳", ACCOUNT, 7)
        assert peer.calls == [
            (
                "sendReaction",
                {"noteToSelf": True, "emoji": "⏳", "targetAuthor": ACCOUNT, "targetTimestamp": 7},
            )
        ]

    @pytest.mark.asyncio
    async def test_nothing_is_sent_without_a_target(self) -> None:
        client, peer = _client()
        await client.send_reaction(OWNER, "✅", "", 7)
        await client.send_reaction(OWNER, "✅", OWNER, 0)
        await client.send_reaction("", "✅", OWNER, 7)
        assert peer.calls == []

    @pytest.mark.asyncio
    async def test_a_timeout_is_swallowed_and_retried_next_time(self) -> None:
        client, peer = _client()
        peer.fail = RpcTransportError("sendReaction timed out after 10s")
        await client.send_reaction(OWNER, "✅", OWNER, 7)
        assert client.reactions_supported is True

    @pytest.mark.asyncio
    async def test_an_unsupported_method_switches_reactions_off(self) -> None:
        client, peer = _client()
        peer.fail = RpcError(-32601, "Method not implemented")
        await client.send_reaction(OWNER, "✅", OWNER, 7)
        await client.send_reaction(OWNER, "✅", OWNER, 7)
        assert len(peer.calls) == 1 and client.reactions_supported is False


# ── planning and encoding ───────────────────────────────────────────────────
class TestDataUri:
    def test_the_bytes_and_the_name_round_trip(self) -> None:
        uri = data_uri(b"\x89PNG..", "image/png", "my chart;v=1,final.png")
        assert _decode(uri) == ("image/png", "my chart;v=1,final.png", b"\x89PNG..")
        # Nothing in the name can close the parameter early.
        head = uri.split(",", 1)[0]
        assert head.count(";") == 2 and head.count("=") == 1

    @pytest.mark.parametrize("mime", ["", "png", "image/", "a/b/c", "IMAGE/PNG; q=1"])
    def test_an_unparseable_type_falls_back(self, mime: str) -> None:
        got = _decode(data_uri(b"x", mime))[0]
        assert got in ("application/octet-stream", "image/png")


class TestPlan:
    @pytest.mark.asyncio
    async def test_a_picture_in_the_root_is_planned_and_its_markup_cut(
        self, tmp_path: Path
    ) -> None:
        pic = tmp_path / "chart.png"
        pic.write_bytes(_PNG)
        plan = await plan_uploads(
            f"Here:\n\n![the chart]({pic})\n\nDone.", within_root=str(tmp_path), session_key=SESSION
        )
        assert [f.data for f in plan.files] == [_PNG]
        assert plan.files[0].alt == "the chart"
        assert str(pic) not in plan.text and "Done." in plan.text

    @pytest.mark.asyncio
    async def test_a_picture_outside_the_root_is_refused_and_left_visible(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "work"
        root.mkdir()
        outside = tmp_path / "secret.png"
        outside.write_bytes(_PNG)
        text = f"![x]({outside})"
        plan = await plan_uploads(text, within_root=str(root), session_key=SESSION)
        assert plan.files == [] and plan.rejections
        assert str(outside) in plan.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("root", ["", "relative/dir"])
    async def test_no_absolute_root_uploads_nothing(self, root: str, tmp_path: Path) -> None:
        pic = tmp_path / "c.png"
        pic.write_bytes(_PNG)
        text = f"![c]({pic})"
        plan = await plan_uploads(text, within_root=root, session_key=SESSION)
        assert plan.files == [] and plan.text == text

    @pytest.mark.asyncio
    async def test_a_restricted_session_ships_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audits: list[dict[str, Any]] = []

        class _Sel:
            def log_api_access(self, **kwargs: Any) -> None:
                audits.append(kwargs)

        monkeypatch.setattr("kiro_crew.messaging.upload_gate.sel", lambda: _Sel())
        monkeypatch.setattr(privacy_mode, "is_restricted", lambda key: key == SESSION)
        pic = tmp_path / "c.png"
        pic.write_bytes(_PNG)
        text = f"![c]({pic})"
        plan = await plan_uploads(text, within_root=str(tmp_path), session_key=SESSION)
        assert plan.files == [] and plan.text == text
        assert [r.reason for r in plan.rejections] == ["restricted_session"]
        assert audits and audits[0]["operation"] == "signal_dispatch.upload_files"


class TestRejectionNote:
    def test_empty_is_empty(self) -> None:
        assert rejection_note([]) == ""

    def test_long_lists_are_counted_and_long_paths_keep_their_tail(self) -> None:
        long_dest = "/very/" + "deep/" * 20 + "chart.png"
        rejections = [Rejection(long_dest, "missing", "the file does not exist")] * (
            SIGNAL_MAX_REJECTION_LINES + 2
        )
        note = rejection_note(rejections)
        lines = note.splitlines()
        assert lines[0] == "Not sent:"
        assert lines[1].startswith("• …") and "chart.png: the file does not exist" in lines[1]
        assert lines[-1] == "• and 2 more"


# ── the renderer ────────────────────────────────────────────────────────────
class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list[str]]] = []
        self.reactions: list[tuple[str, str, str, int]] = []
        self.fail_text = False
        self.fail_files = False

    async def send(self, recipient: str, text: str, *, attachments: Any = ()) -> str:
        if attachments and self.fail_files:
            raise RpcTransportError("upload failed")
        if not attachments and self.fail_text:
            raise RpcTransportError("signal-cli is not running")
        self.sent.append((recipient, text, list(attachments)))
        return str(len(self.sent))

    async def send_reaction(self, recipient: str, emoji: str, author: str, ts: int) -> None:
        self.reactions.append((recipient, emoji, author, ts))

    async def send_typing(self, recipient: str) -> None:
        return None

    async def mark_read(self, recipient: str, timestamp: int) -> None:
        return None


def _renderer(client: FakeClient, root: Path | None = None, **kwargs: Any) -> SignalRenderer:
    return SignalRenderer(
        client,  # type: ignore[arg-type]
        OWNER,
        SIGNAL_CAPABILITIES,
        reply_timestamp=1700000000100,
        session_key=SESSION,
        react_author=kwargs.pop("react_author", OWNER),
        react_timestamp=1700000000100,
        upload_root=(lambda: str(root)) if root is not None else None,
        **kwargs,
    )


class TestRendererReactions:
    @pytest.mark.asyncio
    async def test_working_then_done(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_turn_start()
        await renderer.on_text_chunk("ok")
        await renderer.on_done("end_turn")
        await renderer.close()
        assert client.reactions == [
            (OWNER, REACTION_WORKING, OWNER, 1700000000100),
            (OWNER, REACTION_DONE, OWNER, 1700000000100),
        ]

    @pytest.mark.asyncio
    async def test_an_errored_turn_ends_on_the_warning_sign(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_turn_start()
        await renderer.close()  # never reached on_done: the error path
        assert [r[1] for r in client.reactions] == [REACTION_WORKING, REACTION_FAILED]

    @pytest.mark.asyncio
    async def test_an_undelivered_answer_ends_on_the_warning_sign_and_still_raises(self) -> None:
        client = FakeClient()
        client.fail_text = True
        renderer = _renderer(client)
        await renderer.on_text_chunk("ok")
        with pytest.raises(RpcTransportError):
            await renderer.on_done("end_turn")
        assert [r[1] for r in client.reactions] == [REACTION_FAILED]

    @pytest.mark.asyncio
    async def test_no_target_means_no_reaction(self) -> None:
        client = FakeClient()
        renderer = _renderer(client, react_author="")
        await renderer.on_turn_start()
        await renderer.on_done("end_turn")
        assert client.reactions == []

    @pytest.mark.asyncio
    async def test_a_failing_reaction_never_fails_the_turn(self) -> None:
        client = FakeClient()

        async def boom(*_a: Any) -> None:
            raise RuntimeError("bad")

        client.send_reaction = boom  # type: ignore[method-assign]
        renderer = _renderer(client)
        await renderer.on_turn_start()
        await renderer.on_text_chunk("ok")
        await renderer.on_done("end_turn")
        assert [s[1] for s in client.sent] == ["ok"]


class TestRendererUploads:
    @pytest.mark.asyncio
    async def test_the_text_goes_first_then_the_picture_with_its_caption(
        self, tmp_path: Path
    ) -> None:
        pic = tmp_path / "chart.png"
        pic.write_bytes(_PNG)
        client = FakeClient()
        renderer = _renderer(client, tmp_path)
        await renderer.on_text_chunk(f"**Revenue** is up.\n\n![Q3 revenue]({pic})")
        await renderer.on_done("end_turn")
        assert len(client.sent) == 2
        assert client.sent[0] == (OWNER, "Revenue is up.", [])
        recipient, caption, attachments = client.sent[1]
        assert (recipient, caption) == (OWNER, "Q3 revenue")
        assert _decode(attachments[0]) == ("image/png", "chart.png", _PNG)

    @pytest.mark.asyncio
    async def test_a_failed_upload_is_named_not_swallowed(self, tmp_path: Path) -> None:
        pic = tmp_path / "chart.png"
        pic.write_bytes(_PNG)
        client = FakeClient()
        client.fail_files = True
        renderer = _renderer(client, tmp_path)
        await renderer.on_text_chunk(f"See ![c]({pic})")
        await renderer.on_done("end_turn")
        assert client.sent[0][1] == "See"
        assert client.sent[-1][1].startswith("Not sent:\n•") and "chart.png" in client.sent[-1][1]
        # The answer itself was delivered, so the turn is a success.
        assert client.reactions[-1][1] == REACTION_DONE

    @pytest.mark.asyncio
    async def test_a_refused_reference_keeps_its_path_and_is_named(self, tmp_path: Path) -> None:
        client = FakeClient()
        renderer = _renderer(client, tmp_path)
        missing = tmp_path / "missing.png"
        await renderer.on_text_chunk(f"See ![c]({missing})")
        await renderer.on_done("end_turn")
        assert all(not s[2] for s in client.sent)
        assert client.sent[-1][1].startswith("Not sent:")

    @pytest.mark.asyncio
    async def test_without_a_root_nothing_is_uploaded(self, tmp_path: Path) -> None:
        pic = tmp_path / "chart.png"
        pic.write_bytes(_PNG)
        client = FakeClient()
        renderer = _renderer(client, None)
        await renderer.on_text_chunk(f"![c]({pic})")
        await renderer.on_done("end_turn")
        assert all(not s[2] for s in client.sent)

    @pytest.mark.asyncio
    async def test_an_errored_turn_uploads_nothing(self, tmp_path: Path) -> None:
        pic = tmp_path / "chart.png"
        pic.write_bytes(_PNG)
        client = FakeClient()
        renderer = _renderer(client, tmp_path)
        await renderer.on_text_chunk(f"![c]({pic})")
        await renderer.on_done("error")
        assert all(not s[2] for s in client.sent)


# ── display redaction of what the files path sends ─────────────────────────
#: Built by concatenation so no scanner flags this file for holding a key.
_FAKE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"

#: The splitters a display collapses: a credential written across one of them
#: matches nothing as written and reads whole once the markup is gone.
_SPLITTERS = ("", "**", "`", "_")


def _split_key(splitter: str) -> str:
    return f"{_FAKE_KEY[:4]}{splitter}{_FAKE_KEY[4:]}{splitter}"


def _reassemblable(text: str) -> bool:
    """Whether *text* shows the key, as written or with the markup collapsed."""
    collapsed = text.replace("*", "").replace("`", "").replace("_", "")
    return _FAKE_KEY in text or _FAKE_KEY in collapsed


class TestRedactionOfFileTexts:
    """Every text the files path builds passes the display screen the answer does."""

    @pytest.mark.parametrize("splitter", _SPLITTERS)
    @pytest.mark.asyncio
    async def test_a_key_in_a_refused_destination_never_reaches_the_note(
        self, splitter: str, tmp_path: Path
    ) -> None:
        client = FakeClient()
        renderer = _renderer(client, tmp_path)
        # Outside the root, so the reference is refused and the note quotes it.
        await renderer.on_text_chunk(f"See ![c](/etc/{_split_key(splitter)}.png)")
        await renderer.on_done("end_turn")
        notes = [text for _r, text, _a in client.sent if text.startswith("Not sent:")]
        assert len(notes) == 1 and "[REDACTED" in notes[0]
        for _recipient, text, _attachments in client.sent:
            assert not _reassemblable(text), text

    @pytest.mark.parametrize("splitter", _SPLITTERS)
    @pytest.mark.asyncio
    async def test_a_key_in_a_refusal_reason_never_reaches_the_note(self, splitter: str) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        plan = files_mod.UploadPlan(
            text="",
            rejections=[Rejection("/w/chart.png", "x", f"refused: {_split_key(splitter)}")],
        )
        note = await renderer._send_files(plan)
        assert client.sent == [(OWNER, note, [])]
        assert note.startswith("Not sent:") and not _reassemblable(note)

    @pytest.mark.asyncio
    async def test_a_screened_note_raises_the_redaction_notice(self, tmp_path: Path) -> None:
        # The answer itself is clean: only the note's placeholder can raise it.
        client = FakeClient()
        renderer = _renderer(client, tmp_path)
        plan = files_mod.UploadPlan(
            text="Here.", rejections=[Rejection(f"/w/{_split_key('**')}.png", "x", "no")]
        )
        renderer._plan_uploads = lambda: _resolved(plan)  # type: ignore[method-assign]
        await renderer.on_text_chunk("Here.")
        await renderer.on_done("end_turn")
        texts = [text for _r, text, _a in client.sent]
        assert texts[0] == "Here." and texts[1].startswith("Not sent:")
        assert texts[-1].startswith("Security notice:")

    @pytest.mark.parametrize("splitter", _SPLITTERS)
    @pytest.mark.asyncio
    async def test_a_key_in_a_picture_caption_is_redacted(
        self, splitter: str, tmp_path: Path
    ) -> None:
        pic = tmp_path / "chart.png"
        pic.write_bytes(_PNG)
        client = FakeClient()
        renderer = _renderer(client, tmp_path)
        await renderer.on_text_chunk(f"![alt {_split_key(splitter)}]({pic})")
        await renderer.on_done("end_turn")
        captions = [text for _r, text, attachments in client.sent if attachments]
        assert len(captions) == 1
        for _recipient, text, _attachments in client.sent:
            assert not _reassemblable(text), text

    @pytest.mark.parametrize("splitter", _SPLITTERS)
    @pytest.mark.asyncio
    async def test_a_key_in_a_file_send_caption_is_redacted(self, splitter: str) -> None:
        client, peer = _client()
        transport = SignalTransport(client, allowed_numbers=[OWNER])
        file = OutboundFile(path="/w/r.pdf", data=b"%PDF-1.7", alt="", mime="application/pdf")
        await transport.send_document(OWNER, file, caption=f"key {_split_key(splitter)}")
        message = peer.calls[0][1]["message"]
        assert "[REDACTED" in message and not _reassemblable(message)


async def _resolved(plan: files_mod.UploadPlan) -> files_mod.UploadPlan:
    return plan


# ── file_send's native leg ──────────────────────────────────────────────────
class TestSendDocument:
    @pytest.mark.asyncio
    async def test_the_validated_bytes_travel_under_the_admitted_name(self) -> None:
        client, peer = _client()
        transport = SignalTransport(client, allowed_numbers=[OWNER])
        file = OutboundFile(
            path="/work/report.pdf", data=b"%PDF-1.7", alt="", mime="application/pdf"
        )
        mid = await transport.send_document(OWNER, file, caption="the report")
        assert mid == str(peer.ts)
        method, params = peer.calls[0]
        assert method == "send" and params["message"] == "the report"
        assert _decode(params["attachments"][0]) == ("application/pdf", "report.pdf", b"%PDF-1.7")

    @pytest.mark.asyncio
    async def test_a_credential_shaped_name_is_replaced_and_keeps_its_extension(self) -> None:
        client, peer = _client()
        transport = SignalTransport(client, allowed_numbers=[OWNER])
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        file = OutboundFile(
            path=f"/work/{key}.pdf", data=b"%PDF-1.7", alt="", mime="application/pdf"
        )
        await transport.send_document(OWNER, file)
        uri = peer.calls[0][1]["attachments"][0]
        assert _decode(uri) == ("application/pdf", "file.pdf", b"%PDF-1.7")
        assert key not in uri

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("report.pdf", "report.pdf"),
            ("", "file"),
            ("AKIA" + "IOSFODNN7EXAMPLE", "file"),
            ("ghp_" + "a" * 36 + ".tar.gz", "file.gz"),
            # A POSIX basename cannot hold the ``//`` of an address, so this
            # reaches the screen only through a caller that hands over a name;
            # the screen holds either way.
            ("https://collect.example.net?d=" + "QUJD" * 60 + ".txt", "file.txt"),
            ("https://collect.example.net?d=" + "QUJD" * 60, "file"),
            # The extension is kept only when it is plain.
            ("AKIA" + "IOSFODNN7EXAMPLE.p df", "file"),
        ],
    )
    def test_the_document_name_passes_the_shared_upload_name_screen(
        self, name: str, expected: str
    ) -> None:
        from kiro_crew.messaging.outbound_files import filename_passes_screen

        sent = files_mod.document_filename(name)
        assert sent == expected
        assert filename_passes_screen(sent)

    @pytest.mark.asyncio
    async def test_a_picture_named_like_a_key_is_uploaded_under_the_fallback(
        self, tmp_path: Path
    ) -> None:
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        pic = tmp_path / f"{key}.png"
        pic.write_bytes(_PNG)
        client = FakeClient()
        renderer = _renderer(client, tmp_path)
        await renderer.on_text_chunk(f"![chart]({pic})")
        await renderer.on_done("end_turn")
        uris = [uri for _r, _t, attachments in client.sent for uri in attachments or []]
        assert len(uris) == 1
        assert _decode(uris[0])[1] == "image_0.png" and key not in uris[0]

    @pytest.mark.asyncio
    async def test_a_conversation_off_the_roster_is_refused(self) -> None:
        client, peer = _client()
        transport = SignalTransport(client, allowed_numbers=[OWNER])
        file = OutboundFile(path="/w/r.pdf", data=b"x", alt="", mime="application/pdf")
        with pytest.raises(PermissionError):
            await transport.send_document(STRANGER, file)
        assert peer.calls == []

    def test_signal_is_a_document_channel(self) -> None:
        from kiro_crew.dashboard.upload_destination import DOCUMENT_CHANNELS

        assert "signal" in DOCUMENT_CHANNELS


def test_capabilities_declare_files_and_reactions() -> None:
    caps = SIGNAL_CAPABILITIES
    assert (caps.reactions, caps.files_inbound, caps.files_outbound) == (True, True, True)


def test_the_limits_are_whatsapp_s() -> None:
    limits = files_mod.signal_limits()
    assert (limits.max_files, limits.max_file_bytes, limits.max_total_bytes) == (
        4,
        8 * 1024 * 1024,
        16 * 1024 * 1024,
    )
