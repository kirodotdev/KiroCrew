"""Tests for kiro_crew.wecom.transport (WeComTransport, Layer 1)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.messaging.outbound_files import OutboundFile
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.wecom.client import (
    WECOM_MAX_REPLY_BYTES,
    WECOM_SAFE_REPLY_CHARS,
    WeComInbound,
)
from kiro_crew.wecom.transport import (
    WECOM_CAPABILITIES,
    WeComSendError,
    WeComTransport,
)


class FakeClient:
    """Minimal WeComClient stand-in recording lifecycle + sends."""

    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.replies: list[tuple[str, str]] = []
        self.dedupe_calls: list[str] = []
        #: msgids released because their dispatch raised (see forget_msgid).
        self.forgotten: list[str] = []
        self.pushed: list[tuple[str, str]] = []
        self.push_ok = True
        #: (data, media_type, filename) for each upload_media call.
        self.uploaded: list[tuple[bytes, str, str]] = []
        #: (chat_id, media_id, media_type) for each send_file_proactive call.
        self.files_sent: list[tuple[str, str, str]] = []
        self.upload_media_id = "mid-1"
        self.file_send_ok = True
        #: Optional callbacks fired INSIDE the corresponding await, so a test can
        #: mutate the transport's allow-list mid-flight (models a live reconfigure
        #: landing during the upload / between the file and caption pushes).
        self.on_upload = None
        self.on_file_send = None

    async def send_proactive(self, chat_id: str, content: str) -> bool:
        self.pushed.append((chat_id, content))
        return self.push_ok

    async def upload_media(self, data: bytes, media_type: str, filename: str) -> str:
        self.uploaded.append((data, media_type, filename))
        if self.on_upload is not None:
            self.on_upload()
        return self.upload_media_id

    async def send_file_proactive(
        self, chat_id: str, media_id: str, *, media_type: str = "file"
    ) -> bool:
        self.files_sent.append((chat_id, media_id, media_type))
        if self.on_file_send is not None:
            self.on_file_send()
        return self.file_send_ok

    def forget_msgid(self, msgid: str) -> None:
        self.forgotten.append(msgid)

    def already_delivered(self, msgid: str) -> bool:
        """Record-and-report, like the real client's bounded window.

        Present because the transport consults it on every inbound frame; a fake
        that omitted it would make ``receive`` raise instead of testing the gate.
        """
        self.dedupe_calls.append(msgid)
        return False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def send_reply(self, url: str, content: str) -> None:
        self.replies.append((url, content))


def _inbound(userid: str = "Wei", text: str = "hi") -> WeComInbound:
    return WeComInbound(
        userid=userid, text=text, response_url="https://r", req_id="rq1", chatid="", msgid="m-1"
    )


class TestCapabilities:
    def test_wecom_shape(self) -> None:
        cap = WECOM_CAPABILITIES
        assert cap.streaming is True
        assert cap.edit is True
        assert cap.max_buttons == 0  # no tappable chips; OPTIONS degrade to text
        # aibot_send_msg exists on the long connection, so the transport CAN push.
        # Whether a given peer is reachable is a per-target question -- it must
        # have written to the bot first -- answered by configured_targets().
        assert cap.supports_proactive_send is True
        # A CHARACTER budget derived from the platform's 20480-BYTE cap, so the
        # shared character splitter cannot produce an over-cap frame.
        assert cap.max_message_chars == WECOM_SAFE_REPLY_CHARS
        assert cap.max_message_chars * 4 <= WECOM_MAX_REPLY_BYTES


class TestConfiguredTargets:
    """A target is offered only when WeCom would actually deliver to it."""

    def test_an_allowlisted_user_is_unavailable_until_they_write_first(self) -> None:
        transport = WeComTransport(FakeClient(), allowed_users=["Wei"])

        (target,) = transport.configured_targets()
        assert target.target_id == "user:Wei"
        assert target.available is False
        assert "written to the bot" in target.unavailable_reason

    def test_writing_to_the_bot_makes_the_target_available(self) -> None:
        transport = WeComTransport(FakeClient(), allowed_users=["Wei"])
        transport.note_warm_chat("Wei")

        (target,) = transport.configured_targets()
        assert target.available is True
        assert target.unavailable_reason == ""

    def test_the_allow_all_policy_lists_the_peers_it_has_actually_heard_from(self) -> None:
        # Under allow-everyone there is no configured list to draw on, so the warm
        # peers ARE the addressable set. The old placeholder row advertised a
        # target that could never be resolved.
        transport = WeComTransport(FakeClient(), allow_all=True)
        assert transport.configured_targets() == []

        transport.note_warm_chat("Wei")
        assert [x.target_id for x in transport.configured_targets()] == ["user:Wei"]

    @pytest.mark.asyncio
    async def test_resolution_allows_an_unwarmed_but_authorized_target(self) -> None:
        # Warmth is process-local while a mirror binding is persisted, so after a
        # restart it is UNKNOWN rather than false. Refusing on it would silently
        # disable every mirrored send until the user happened to write again;
        # WeCom's own refusal is the authority on deliverability.
        transport = WeComTransport(FakeClient(), allowed_users=["Wei"])
        assert await transport.resolve_configured_target("user:Wei") == ("Wei", None)

    @pytest.mark.asyncio
    async def test_resolution_returns_the_conversation_for_a_warm_target(self) -> None:
        transport = WeComTransport(FakeClient(), allowed_users=["Wei"])
        transport.note_warm_chat("Wei")
        assert await transport.resolve_configured_target("user:Wei") == ("Wei", None)

    @pytest.mark.asyncio
    async def test_resolution_refuses_a_userid_that_left_the_allowlist(self) -> None:
        # The allow-list can narrow between advertising an id and resolving it, so
        # membership is rechecked at the side-effect boundary rather than trusted.
        transport = WeComTransport(FakeClient(), allowed_users=["Wei"])
        transport.note_warm_chat("Stranger")
        assert await transport.resolve_configured_target("user:Stranger") is None

    @pytest.mark.asyncio
    async def test_resolution_refuses_a_malformed_target_id(self) -> None:
        transport = WeComTransport(FakeClient(), allowed_users=["Wei"])
        for bad in ("", "Wei", "policy:all", "user:", "group:room"):
            assert await transport.resolve_configured_target(bad) is None


class TestProactiveSend:
    @pytest.mark.asyncio
    async def test_send_message_pushes_to_the_conversation(self) -> None:
        client = FakeClient()
        transport = WeComTransport(client, allowed_users=["Wei"])
        transport.note_warm_chat("Wei")

        assert await transport.send_message("Wei", "hello") == ""
        assert client.pushed == [("Wei", "hello")]

    @pytest.mark.asyncio
    async def test_an_empty_conversation_id_raises_rather_than_reporting_success(self) -> None:
        # A return reads as delivery to the mirror caller, which then persists the
        # link and reports success. Silence here loses the message.
        client = FakeClient()
        transport = WeComTransport(client, allow_all=True)
        with pytest.raises(WeComSendError):
            await transport.send_message("", "hi")
        assert client.pushed == []

    @pytest.mark.asyncio
    async def test_a_userid_removed_from_the_allowlist_can_no_longer_be_pushed_to(self) -> None:
        # A mirror binding is PERSISTED, so it outlives the allow-list entry that
        # justified it. Without a recheck at the send boundary, a removed userid
        # keeps receiving the session's replies.
        client = FakeClient()
        transport = WeComTransport(client, allowed_users=["Wei"])
        transport.note_warm_chat("Wei")
        assert await transport.send_message("Wei", "ok") == ""

        narrowed = WeComTransport(client, allowed_users=["SomeoneElse"])
        narrowed.note_warm_chat("Wei")  # still warm, but not allowed
        with pytest.raises(WeComSendError, match="not currently authorized"):
            await narrowed.send_message("Wei", "leak?")

    @pytest.mark.asyncio
    async def test_an_unwarmed_but_authorized_conversation_is_still_attempted(self) -> None:
        # Survives a gateway restart: warmth resets while the mirror link persists,
        # so the send must not be refused locally on a fact only WeCom knows.
        client = FakeClient()
        transport = WeComTransport(client, allowed_users=["Wei"])
        assert await transport.send_message("Wei", "hi") == ""
        assert client.pushed == [("Wei", "hi")]

    @pytest.mark.asyncio
    async def test_an_unauthorized_conversation_is_refused_at_the_send_boundary(self) -> None:
        client = FakeClient()
        transport = WeComTransport(client, allowed_users=["Wei"])
        with pytest.raises(WeComSendError, match="not currently authorized"):
            await transport.send_message("Stranger", "hi")
        assert client.pushed == []

    @pytest.mark.asyncio
    async def test_a_failed_push_raises_rather_than_reporting_success(self) -> None:
        client = FakeClient()
        client.push_ok = False
        transport = WeComTransport(client, allow_all=True)
        transport.note_warm_chat("Wei")
        with pytest.raises(WeComSendError):
            await transport.send_message("Wei", "hi")


class TestSendDocument:
    """The file leg: native media upload plus caption forwarding."""

    @staticmethod
    def _doc(alt: str = "") -> OutboundFile:
        return OutboundFile(path="/tmp/report.pdf", data=b"bytes", alt=alt, mime="application/pdf")

    @pytest.mark.asyncio
    async def test_uploads_and_sends_the_file(self) -> None:
        client = FakeClient()
        transport = WeComTransport(client, allowed_users=["Wei"])
        result = await transport.send_document("Wei", self._doc())
        assert result == "mid-1"
        assert client.uploaded == [(b"bytes", "file", "report.pdf")]
        assert client.files_sent == [("Wei", "mid-1", "file")]
        # No caption -> no companion text push.
        assert client.pushed == []

    @pytest.mark.asyncio
    async def test_an_oversize_image_downgrades_to_file_instead_of_failing(self) -> None:
        # A .png maps to WeCom's image type (2 MB cap). A 3 MB screenshot exceeds
        # that cap but fits the 20 MB file cap, so it must upload as `file` — a
        # downloadable card is strictly better than the whole send failing.
        client = FakeClient()
        transport = WeComTransport(client, allowed_users=["Wei"])
        big_png = OutboundFile(
            path="/tmp/shot.png", data=b"x" * 3_000_000, alt="", mime="image/png"
        )
        result = await transport.send_document("Wei", big_png)
        assert result == "mid-1"
        assert client.uploaded == [(b"x" * 3_000_000, "file", "shot.png")]
        assert client.files_sent == [("Wei", "mid-1", "file")]

    @pytest.mark.asyncio
    async def test_a_small_image_still_uploads_as_image(self) -> None:
        # Below the 2 MB image cap, a .png keeps its native image type.
        client = FakeClient()
        transport = WeComTransport(client, allowed_users=["Wei"])
        small_png = OutboundFile(path="/tmp/icon.png", data=b"x" * 1000, alt="", mime="image/png")
        result = await transport.send_document("Wei", small_png)
        assert result == "mid-1"
        assert client.uploaded == [(b"x" * 1000, "image", "icon.png")]
        assert client.files_sent == [("Wei", "mid-1", "image")]

    @pytest.mark.asyncio
    async def test_a_nonempty_caption_is_delivered_as_a_companion_text_push(self) -> None:
        # The prior behaviour dropped the caller's description silently: WeCom's
        # media frame carries no text field, so the caption must go as a separate
        # aibot_send_msg push AFTER the file lands (Opus finding).
        client = FakeClient()
        transport = WeComTransport(client, allowed_users=["Wei"])
        result = await transport.send_document("Wei", self._doc(), caption="Q3 numbers")
        assert result == "mid-1"
        assert client.files_sent == [("Wei", "mid-1", "file")]
        assert client.pushed == [("Wei", "Q3 numbers")]

    @pytest.mark.asyncio
    async def test_an_empty_caption_sends_no_companion_push(self) -> None:
        client = FakeClient()
        transport = WeComTransport(client, allowed_users=["Wei"])
        await transport.send_document("Wei", self._doc(), caption="")
        assert client.pushed == []

    @pytest.mark.asyncio
    async def test_a_failed_caption_push_still_reports_the_file_delivered(self) -> None:
        # The file already reached the user, so a dropped companion caption is
        # logged, not turned into a delivery failure.
        client = FakeClient()
        client.push_ok = False
        transport = WeComTransport(client, allowed_users=["Wei"])
        result = await transport.send_document("Wei", self._doc(), caption="see attached")
        assert result == "mid-1"
        assert client.pushed == [("Wei", "see attached")]

    @pytest.mark.asyncio
    async def test_a_failed_file_send_returns_none_and_sends_no_caption(self) -> None:
        client = FakeClient()
        client.file_send_ok = False
        transport = WeComTransport(client, allowed_users=["Wei"])
        result = await transport.send_document("Wei", self._doc(), caption="unsent")
        assert result is None
        # The file leg failed, so the caption must not be pushed on its own.
        assert client.pushed == []

    @pytest.mark.asyncio
    async def test_an_unauthorized_conversation_is_refused(self) -> None:
        client = FakeClient()
        transport = WeComTransport(client, allowed_users=["Wei"])
        assert await transport.send_document("Stranger", self._doc(), caption="x") is None
        assert client.uploaded == [] and client.pushed == []

    @pytest.mark.asyncio
    async def test_revocation_during_upload_suppresses_the_file_push(self) -> None:
        # The allow-list can be replaced by a live reconfigure while the chunked
        # upload is in flight (a departing/compromised userid revoked mid-upload).
        # A media frame cannot be recalled, so authorization must be rechecked
        # AFTER the upload, before the push — a single check at entry is a TOCTOU
        # window (GPT security finding).
        client = FakeClient()
        transport = WeComTransport(client, allowed_users=["Wei"])
        client.on_upload = lambda: setattr(transport, "_allowed", frozenset())
        result = await transport.send_document("Wei", self._doc(), caption="secret")
        assert result is None
        assert client.uploaded  # upload happened
        assert client.files_sent == []  # but the file was NOT pushed
        assert client.pushed == []  # and neither was the caption

    @pytest.mark.asyncio
    async def test_revocation_before_caption_suppresses_only_the_caption(self) -> None:
        # The caption is a SECOND delivery past its own await, so a revocation
        # landing between the file push and the caption push must stop the caption
        # even though the file already (irrecoverably) landed.
        client = FakeClient()
        transport = WeComTransport(client, allowed_users=["Wei"])
        client.on_file_send = lambda: setattr(transport, "_allowed", frozenset())
        result = await transport.send_document("Wei", self._doc(), caption="secret")
        assert result == "mid-1"  # the file leg completed and is reported delivered
        assert client.files_sent == [("Wei", "mid-1", "file")]
        assert client.pushed == []  # the caption was withheld from the revoked peer

    def test_files_outbound_stays_false(self) -> None:
        # The flag gates renderer inline-image EXTRACTION, which WeCom has no path
        # for; the file_send document path is gated by DOCUMENT_CHANNELS instead,
        # so it must not be declared True (GPT finding).
        assert WECOM_CAPABILITIES.files_outbound is False


class TestAuthorize:
    def test_owner_allowed(self) -> None:
        t = WeComTransport(FakeClient(), owner_id="Wei")
        assert t.authorize(_msg("Wei")) is True

    def test_allowlist_member_allowed(self) -> None:
        t = WeComTransport(FakeClient(), allowed_users=["Wei", "LiHaoYi"])
        assert t.authorize(_msg("LiHaoYi")) is True

    def test_unknown_denied(self) -> None:
        t = WeComTransport(FakeClient(), owner_id="Wei", allowed_users=["LiHaoYi"])
        with patch("kiro_crew.wecom.transport.sel") as mock_sel:
            assert t.authorize(_msg("stranger")) is False
        mock_sel().log_api_access.assert_called_once()

    def test_empty_userid_denied(self) -> None:
        t = WeComTransport(FakeClient(), owner_id="Wei")
        with patch("kiro_crew.wecom.transport.sel"):
            assert t.authorize(_msg("")) is False

    def test_empty_allowlist_and_no_owner_denies_everyone(self) -> None:
        t = WeComTransport(FakeClient())  # fail closed
        with patch("kiro_crew.wecom.transport.sel"):
            assert t.authorize(_msg("anyone")) is False

    def test_allow_all_admits_any_userid(self) -> None:
        t = WeComTransport(FakeClient(), allow_all=True)  # explicit opt-in
        assert t.authorize(_msg("anyone")) is True
        assert t.authorize(_msg("someone-else")) is True

    def test_allow_all_still_denies_empty_userid(self) -> None:
        # Even under allow-all, an anonymous/malformed frame never dispatches.
        t = WeComTransport(FakeClient(), allow_all=True)
        with patch("kiro_crew.wecom.transport.sel"):
            assert t.authorize(_msg("")) is False

    def test_allow_all_off_is_not_inferred_from_empty_list(self) -> None:
        # The everybody grant is ONLY the explicit flag — an empty allow-list
        # plus allow_all=False stays fail-closed.
        t = WeComTransport(FakeClient(), allowed_users=[], allow_all=False)
        with patch("kiro_crew.wecom.transport.sel"):
            assert t.authorize(_msg("anyone")) is False


class TestReceive:
    @pytest.mark.asyncio
    async def test_authorized_dispatches_inbound(self) -> None:
        dispatched: list[WeComInbound] = []

        async def dispatch(inbound: WeComInbound) -> None:
            dispatched.append(inbound)

        t = WeComTransport(FakeClient(), owner_id="Wei", dispatch=dispatch)
        await t.receive(_inbound("Wei", "hello"))
        assert len(dispatched) == 1
        assert dispatched[0].text == "hello"
        assert dispatched[0].req_id == "rq1"

    @pytest.mark.asyncio
    async def test_unauthorized_does_not_dispatch(self) -> None:
        dispatched: list[WeComInbound] = []

        async def dispatch(inbound: WeComInbound) -> None:
            dispatched.append(inbound)

        t = WeComTransport(FakeClient(), owner_id="Wei", dispatch=dispatch)
        with patch("kiro_crew.wecom.transport.sel"):
            await t.receive(_inbound("stranger", "hello"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_empty_text_dropped(self) -> None:
        dispatched: list[WeComInbound] = []

        async def dispatch(inbound: WeComInbound) -> None:
            dispatched.append(inbound)

        t = WeComTransport(FakeClient(), owner_id="Wei", dispatch=dispatch)
        await t.receive(_inbound("Wei", ""))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_non_wecom_envelope_dropped(self) -> None:
        dispatched: list[WeComInbound] = []

        async def dispatch(inbound: WeComInbound) -> None:
            dispatched.append(inbound)

        t = WeComTransport(FakeClient(), owner_id="Wei", dispatch=dispatch)
        await t.receive({"not": "a WeComInbound"})
        assert dispatched == []


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_connect_disconnect_delegate(self) -> None:
        client = FakeClient()
        t = WeComTransport(client, owner_id="Wei")
        await t.connect()
        assert client.started is True
        await t.disconnect()
        assert client.closed is True


def _msg(userid: str) -> InboundMessage:
    return InboundMessage(channel_type="wecom", user_id=userid, conversation_id=userid, text="hi")


class TestDedupeIsReleasedOnFailure:
    """A dispatch that raised did not deliver, so its msgid must not be consumed."""

    @pytest.mark.asyncio
    async def test_a_raising_dispatch_forgets_the_msgid(self) -> None:
        client = FakeClient()

        async def boom(inbound: WeComInbound) -> None:
            raise RuntimeError("the turn died")

        transport = WeComTransport(client, allowed_users=["Wei"], dispatch=boom)

        with pytest.raises(RuntimeError):
            await transport.receive(_inbound("Wei", "hello"))

        assert client.forgotten == ["m-1"], (
            "the msgid stayed in the dedupe window, so WeCom's redelivery is dropped "
            "as a duplicate and the user's message is never handled"
        )
