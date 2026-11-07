"""The dashboard -> channel mirror sends pictures, never this host's paths.

The reply leg uploads a local image through the channel's own picture send under
the channel renderers' gates, and names every picture it does not send; the
user-message echo names a dashboard attachment instead of printing its path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

# A real SEL under tmp_path, so the legs' audit writes stay out of the user's log.
from test_cross_surface_mirror import audit  # noqa: F401

from kiro_crew.dashboard.chat_runner import (
    _deliver_cross_surface_reply,
    _mirror_echo_text,
    _name_local_attachments,
)
from kiro_crew.discord.transport import DiscordTransport
from kiro_crew.feishu.transport import FeishuTransport
from kiro_crew.messaging import upload_gate
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.outbound_files import ExtractLimits, OutboundFile, upload_filename
from kiro_crew.teams.transport import TeamsTransport
from kiro_crew.telegram.transport import TelegramTransport
from kiro_crew.webex.transport import WebexTransport
from kiro_crew.wecom.transport import WeComTransport
from kiro_crew.whatsapp.transport import WhatsAppTransport

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_FILE = OutboundFile(path="/work/chart.png", data=_PNG, alt="", mime="image/png")


def _transport(*, limits: ExtractLimits | None = ExtractLimits(max_files=10)) -> SimpleNamespace:
    return SimpleNamespace(
        channel_type="feishu",
        capabilities=SimpleNamespace(
            supports_proactive_send=True,
            files_outbound=True,
            max_message_chars=4000,
            max_message_bytes=0,
        ),
        outbound_image_limits=AsyncMock(return_value=limits),
        send_message=AsyncMock(return_value=""),
        send_outbound_image=AsyncMock(return_value=True),
        may_send_to=lambda conversation_id, thread_id=None, principal="": True,
    )


def _state(tmp_path, monkeypatch, transport, *, restricted: bool = False):
    work = tmp_path / "work"
    work.mkdir()
    (work / "chart.png").write_bytes(_PNG)
    state = _make_state(tmp_path)
    state.register_channel_transport(transport)
    state.sessions.get_mirror_link = MagicMock(
        return_value=ChannelLink("feishu", channel_id="ou_owner", thread_id=None)
    )
    state.sessions.get_provider = MagicMock(
        return_value=SimpleNamespace(cwd=str(work), work_scratch_dir=None)
    )
    # The shared ceiling's decision, patched where ``uploads_restricted`` reads it.
    monkeypatch.setattr(upload_gate, "session_is_restricted", AsyncMock(return_value=restricted))
    return state, work


def _sent(tp: SimpleNamespace) -> list[str]:
    return [c.args[1] for c in tp.send_message.await_args_list]


@pytest.mark.asyncio
@pytest.mark.usefixtures("audit")
async def test_a_reply_image_under_the_cwd_is_uploaded_not_printed(tmp_path, monkeypatch):
    tp = _transport()
    state, work = _state(tmp_path, monkeypatch, tp)
    path = work / "chart.png"

    await _deliver_cross_surface_reply(state, "dashboard:c1", f"Here it is\n\n![chart]({path})")

    (sent,) = _sent(tp)
    assert str(path) not in sent and "Here it is" in sent
    (_, image), kwargs = tp.send_outbound_image.await_args
    assert image.data == _PNG and kwargs == {"thread_id": None}


@pytest.mark.asyncio
@pytest.mark.usefixtures("audit")
async def test_a_channel_with_no_picture_send_names_the_picture(tmp_path, monkeypatch):
    tp = _transport(limits=None)
    state, work = _state(tmp_path, monkeypatch, tp)

    await _deliver_cross_surface_reply(state, "dashboard:c1", f"see ![c]({work / 'chart.png'})")

    tp.send_outbound_image.assert_not_awaited()
    (sent,) = _sent(tp)
    assert str(work) not in sent and "🖼 chart.png" in sent


@pytest.mark.asyncio
@pytest.mark.usefixtures("audit")
async def test_a_restricted_session_sends_no_file_and_names_the_picture(tmp_path, monkeypatch):
    tp = _transport()
    state, work = _state(tmp_path, monkeypatch, tp, restricted=True)

    await _deliver_cross_surface_reply(state, "dashboard:c1", f"![chart]({work / 'chart.png'})")

    tp.send_outbound_image.assert_not_awaited()
    (sent,) = _sent(tp)
    assert str(work) not in sent and "🖼 chart.png" in sent


@pytest.mark.asyncio
@pytest.mark.usefixtures("audit")
async def test_a_refused_reference_is_named_with_its_reason(tmp_path, monkeypatch):
    tp = _transport()
    state, _ = _state(tmp_path, monkeypatch, tp)
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(_PNG)

    await _deliver_cross_surface_reply(state, "dashboard:c1", f"see ![x]({outside})")

    tp.send_outbound_image.assert_not_awaited()
    answer, note = _sent(tp)
    assert str(tmp_path) not in answer + note
    assert "🖼 elsewhere.png" in answer and note.startswith("⚠ elsewhere.png not sent")


@pytest.mark.asyncio
@pytest.mark.usefixtures("audit")
async def test_a_failed_upload_is_named_not_pathed(tmp_path, monkeypatch):
    tp = _transport()
    tp.send_outbound_image = AsyncMock(side_effect=RuntimeError("down"))
    state, work = _state(tmp_path, monkeypatch, tp)

    await _deliver_cross_surface_reply(state, "dashboard:c1", f"![c]({work / 'chart.png'})")

    (note,) = _sent(tp)
    assert note == "⚠ Couldn't upload: chart.png"


def test_the_echo_names_every_dashboard_attachment_instead_of_its_path() -> None:
    uploads = "/home/u/.kiro/crew/uploads"
    files = tuple(f"{uploads}/{'ab' * 16}_quarterly deck {n}.pptx" for n in (1, 2, 3))
    message = f"look\n![image]({uploads}/{'cd' * 16}_Screenshot.png)\n" + " ".join(
        f"[attached_file {n}] {path}" for n, path in enumerate(files, 1)
    )

    echo = _name_local_attachments(message, files)

    assert uploads not in echo and "ab" * 16 not in echo
    assert "🖼 Screenshot.png" in echo
    assert all(f"📎 quarterly deck {n}.pptx" in echo for n in (1, 2, 3))


@pytest.mark.asyncio
async def test_both_echo_legs_read_the_turns_own_files() -> None:
    turn = SimpleNamespace(_attachment_meta={"files": ["/up/deck.pptx"]}, _attachments=())

    echo = await _mirror_echo_text(turn, "[attached_file 1] /up/deck.pptx")

    assert echo == "📎 deck.pptx"


def test_a_marker_with_a_non_ascii_digit_is_left_as_typed() -> None:
    assert _name_local_attachments("see [attached_file ²] /tmp/demo.txt") == (
        "see [attached_file ²] /tmp/demo.txt"
    )


def test_a_message_without_attachments_is_unchanged() -> None:
    assert _name_local_attachments("plain `![x](/a.png)` text") == "plain `![x](/a.png)` text"


@pytest.mark.asyncio
async def test_feishu_uploads_to_an_allow_listed_dm_only() -> None:
    client = SimpleNamespace(send_new_image=AsyncMock(return_value=True))
    transport = FeishuTransport(
        client,  # type: ignore[arg-type]
        allowed_open_ids=["ou_owner"],
        allow_group=True,
        allowed_group_ids=["oc_group"],
    )

    assert await transport.outbound_image_limits("oc_group") is None
    assert await transport.outbound_image_limits("ou_owner") is not None
    assert await transport.send_outbound_image("oc_group", _FILE) is False
    client.send_new_image.assert_not_awaited()
    assert await transport.send_outbound_image("ou_owner", _FILE) is True
    client.send_new_image.assert_awaited_once_with("open_id", "ou_owner", _PNG)


def _bare(cls: type, **attrs: object) -> object:
    """A transport built without ``__init__``, carrying only what the verb reads."""
    transport = object.__new__(cls)
    for name, value in attrs.items():
        setattr(transport, name, value)
    return transport


@pytest.mark.asyncio
async def test_telegram_sends_through_the_renderers_media_call() -> None:
    client = SimpleNamespace(send_media_group=AsyncMock(return_value=[7]))
    transport = _bare(TelegramTransport, _client=client)

    assert await transport.outbound_image_limits("42") is not None  # type: ignore[attr-defined]
    assert await transport.send_outbound_image("42", _FILE, thread_id="9")  # type: ignore[attr-defined]
    client.send_media_group.assert_awaited_once_with(
        42, [_FILE], message_thread_id=9, disable_notification=True
    )


@pytest.mark.asyncio
async def test_discord_sends_through_the_renderers_multipart_call() -> None:
    client = SimpleNamespace(send_message_with_files=AsyncMock(return_value="m1"))
    transport = _bare(DiscordTransport, _client=client)

    assert await transport.outbound_image_limits("c1") is not None  # type: ignore[attr-defined]
    assert await transport.send_outbound_image("c1", _FILE)  # type: ignore[attr-defined]
    client.send_message_with_files.assert_awaited_once_with("c1", "", [_FILE])


@pytest.mark.asyncio
async def test_wecom_sends_through_send_document() -> None:
    transport = _bare(WeComTransport)
    transport.send_document = AsyncMock(return_value=None)  # type: ignore[attr-defined]

    assert await transport.outbound_image_limits("u1") is not None  # type: ignore[attr-defined]
    assert await transport.send_outbound_image("u1", _FILE) is False  # type: ignore[attr-defined]
    (conversation, sent), _ = transport.send_document.await_args  # type: ignore[attr-defined]
    assert conversation == "u1" and sent.data == _PNG and sent.path == upload_filename(_FILE, 0)


@pytest.mark.asyncio
async def test_whatsapp_applies_the_renderers_decode_screen(monkeypatch) -> None:
    from kiro_crew.whatsapp import files

    transport = _bare(WhatsAppTransport)
    transport.send_image = AsyncMock(return_value="w1")  # type: ignore[attr-defined]
    monkeypatch.setattr(files, "_decode_refusal", lambda file: object())

    assert await transport.outbound_image_limits("j") is not None  # type: ignore[attr-defined]
    assert await transport.send_outbound_image("j", _FILE) is False  # type: ignore[attr-defined]
    transport.send_image.assert_not_awaited()  # type: ignore[attr-defined]
    monkeypatch.setattr(files, "_decode_refusal", lambda file: None)
    assert await transport.send_outbound_image("j", _FILE) is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_webex_uploads_to_a_direct_room_only() -> None:
    client = SimpleNamespace(
        _room_type_of=AsyncMock(side_effect=lambda room: "direct" if room == "dm" else "group"),
        send_file=AsyncMock(return_value="x1"),
    )
    transport = _bare(WebexTransport, _client=client)

    assert await transport.outbound_image_limits("space") is None  # type: ignore[attr-defined]
    assert await transport.outbound_image_limits("dm") is not None  # type: ignore[attr-defined]
    assert await transport.outbound_image_limits("a@b.example") is not None  # type: ignore[attr-defined]
    assert await transport.send_outbound_image("dm", _FILE)  # type: ignore[attr-defined]
    assert client.send_file.await_args.kwargs["data"] == _PNG


@pytest.mark.asyncio
async def test_teams_sends_inline_rasters_only() -> None:
    store = SimpleNamespace(ensure_loaded=AsyncMock(), get=lambda conversation: "https://svc")
    client = SimpleNamespace(send_inline_image=AsyncMock(return_value="t1"))
    transport = _bare(TeamsTransport, _client=client, _store=store)
    gif = OutboundFile(path="/work/a.gif", data=b"GIF89a", alt="", mime="image/gif")

    assert await transport.send_outbound_image("a:1", gif) is False  # type: ignore[attr-defined]
    assert await transport.send_outbound_image("a:1", _FILE) is True  # type: ignore[attr-defined]
    assert client.send_inline_image.await_args.args[2] == "https://svc"
