"""Tests for kiro_crew.feishu.transport (FeishuTransport, Layer 1)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.feishu.client import LarkInbound
from kiro_crew.feishu.transport import (
    _SEEN_KEEP,
    _SEEN_MAX,
    FEISHU_CAPABILITIES,
    FeishuTransport,
)
from kiro_crew.messaging.transport import InboundMessage


class FakeClient:
    """Minimal LarkClient stand-in recording lifecycle + sends."""

    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.replies: list[tuple[str, str]] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def send_reply(self, message_id: str, text: str) -> bool:
        self.replies.append((message_id, text))
        return True


def _inbound(
    open_id: str = "ou_abc",
    text: str = "hi",
    message_id: str = "msg1",
    chat_type: str = "p2p",
    chat_id: str = "",
) -> LarkInbound:
    return LarkInbound(
        open_id=open_id,
        text=text,
        message_id=message_id,
        chat_type=chat_type,
        chat_id=chat_id,
    )


class TestCapabilities:
    def test_feishu_shape(self) -> None:
        cap = FEISHU_CAPABILITIES
        assert cap.streaming is False
        assert cap.edit is False
        assert cap.max_buttons == 0
        assert cap.supports_proactive_send is False
        assert cap.max_message_chars == 4000


class TestConfiguredTargets:
    def test_targets_reflect_allowed_open_ids(self) -> None:
        transport = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc", "ou_def"])
        targets = transport.configured_targets()
        assert len(targets) == 2
        ids = {t.target_id for t in targets}
        assert ids == {"user:ou_abc", "user:ou_def"}
        # All targets are unavailable (no proactive send).
        assert all(not t.available for t in targets)

    def test_empty_allowed_gives_no_targets(self) -> None:
        transport = FeishuTransport(FakeClient(), allowed_open_ids=[])
        assert transport.configured_targets() == []


class TestAuthorize:
    def test_allowed_open_id_passes(self) -> None:
        t = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc"])
        assert t.authorize(_msg("ou_abc")) is True

    def test_unknown_open_id_denied(self) -> None:
        t = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc"])
        with patch("kiro_crew.feishu.transport.sel") as mock_sel:
            assert t.authorize(_msg("ou_stranger")) is False
        mock_sel().log_api_access.assert_called_once()

    def test_empty_open_id_denied(self) -> None:
        t = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc"])
        with patch("kiro_crew.feishu.transport.sel") as mock_sel:
            assert t.authorize(_msg("")) is False
        mock_sel().log_api_access.assert_called_once()

    def test_empty_allowlist_denies_everyone(self) -> None:
        t = FeishuTransport(FakeClient(), allowed_open_ids=[])
        with patch("kiro_crew.feishu.transport.sel") as mock_sel:
            assert t.authorize(_msg("ou_abc")) is False
        mock_sel().log_api_access.assert_called_once()


class TestChatTypeGate:
    """Only p2p and group are served; anything else is denied, not assumed DM.

    The gate keys on the chat type, so a frame that omits it (or names a type
    Feishu adds later) must NOT fall through the ungated direct-message path —
    that would run a turn, and post its reply, in a context whose
    authorisation was never evaluated.
    """

    def _t(self, dispatched: list) -> "FeishuTransport":
        async def dispatch(inbound: LarkInbound) -> None:
            dispatched.append(inbound)

        return FeishuTransport(
            FakeClient(),
            allowed_open_ids=["ou_abc"],
            allow_group=True,
            allowed_group_ids=["grp_allowed"],
            dispatch=dispatch,
        )

    @pytest.mark.asyncio
    async def test_absent_chat_type_is_denied(self) -> None:
        dispatched: list[LarkInbound] = []
        t = self._t(dispatched)
        with patch("kiro_crew.feishu.transport.sel") as mock_sel:
            await t.receive(_inbound("ou_abc", "hi", chat_type="", chat_id=""))
        assert dispatched == []
        mock_sel().log_api_access.assert_called_once()
        assert mock_sel().log_api_access.call_args.kwargs["outcome"] == "denied_unknown_chat_type"

    @pytest.mark.asyncio
    async def test_unknown_chat_type_is_denied_even_for_an_allowed_sender(
        self,
    ) -> None:
        # The sender IS on the open_id allow-list; the chat type alone must
        # stop this, which is what makes the gate fail closed on a type the
        # code does not recognise.
        dispatched: list[LarkInbound] = []
        t = self._t(dispatched)
        with patch("kiro_crew.feishu.transport.sel") as mock_sel:
            await t.receive(
                _inbound("ou_abc", "hi", chat_type="topic_group", chat_id="grp_allowed")
            )
        assert dispatched == []
        mock_sel().log_api_access.assert_called_once()

    @pytest.mark.asyncio
    async def test_explicit_p2p_still_serves(self) -> None:
        dispatched: list[LarkInbound] = []
        t = self._t(dispatched)
        with patch("kiro_crew.feishu.transport.sel"):
            await t.receive(_inbound("ou_abc", "hi", chat_type="p2p", chat_id=""))
        assert len(dispatched) == 1

    @pytest.mark.asyncio
    async def test_allowlisted_group_still_serves(self) -> None:
        dispatched: list[LarkInbound] = []
        t = self._t(dispatched)
        with patch("kiro_crew.feishu.transport.sel"):
            await t.receive(_inbound("ou_abc", "hi", chat_type="group", chat_id="grp_allowed"))
        assert len(dispatched) == 1


class TestGroupGate:
    @pytest.mark.asyncio
    async def test_group_denied_when_allow_group_false(self) -> None:
        dispatched: list[LarkInbound] = []

        async def dispatch(inbound: LarkInbound) -> None:
            dispatched.append(inbound)

        t = FeishuTransport(
            FakeClient(),
            allowed_open_ids=["ou_abc"],
            allow_group=False,
            dispatch=dispatch,
        )
        with patch("kiro_crew.feishu.transport.sel") as mock_sel:
            await t.receive(_inbound("ou_abc", "hi", chat_type="group", chat_id="grp1"))
        assert dispatched == []
        mock_sel().log_api_access.assert_called_once()

    @pytest.mark.asyncio
    async def test_group_denied_when_chat_id_not_allowlisted(self) -> None:
        dispatched: list[LarkInbound] = []

        async def dispatch(inbound: LarkInbound) -> None:
            dispatched.append(inbound)

        t = FeishuTransport(
            FakeClient(),
            allowed_open_ids=["ou_abc"],
            allow_group=True,
            allowed_group_ids=["grp_allowed"],
            dispatch=dispatch,
        )
        with patch("kiro_crew.feishu.transport.sel") as mock_sel:
            await t.receive(_inbound("ou_abc", "hi", chat_type="group", chat_id="grp_other"))
        assert dispatched == []
        mock_sel().log_api_access.assert_called_once()

    @pytest.mark.asyncio
    async def test_group_allowed_when_flag_and_chat_id_match(self) -> None:
        dispatched: list[LarkInbound] = []

        async def dispatch(inbound: LarkInbound) -> None:
            dispatched.append(inbound)

        t = FeishuTransport(
            FakeClient(),
            allowed_open_ids=["ou_abc"],
            allow_group=True,
            allowed_group_ids=["grp_allowed"],
            dispatch=dispatch,
        )
        await t.receive(_inbound("ou_abc", "hello", chat_type="group", chat_id="grp_allowed"))
        assert len(dispatched) == 1
        assert dispatched[0].text == "hello"


class TestReceive:
    @pytest.mark.asyncio
    async def test_authorized_dispatches_inbound(self) -> None:
        dispatched: list[LarkInbound] = []

        async def dispatch(inbound: LarkInbound) -> None:
            dispatched.append(inbound)

        t = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc"], dispatch=dispatch)
        await t.receive(_inbound("ou_abc", "hello"))
        assert len(dispatched) == 1
        assert dispatched[0].text == "hello"
        assert dispatched[0].message_id == "msg1"

    @pytest.mark.asyncio
    async def test_empty_text_dropped(self) -> None:
        dispatched: list[LarkInbound] = []

        async def dispatch(inbound: LarkInbound) -> None:
            dispatched.append(inbound)

        t = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc"], dispatch=dispatch)
        await t.receive(_inbound("ou_abc", ""))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_non_larkinbound_envelope_dropped(self) -> None:
        dispatched: list[LarkInbound] = []

        async def dispatch(inbound: LarkInbound) -> None:
            dispatched.append(inbound)

        t = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc"], dispatch=dispatch)
        await t.receive({"not": "a LarkInbound"})
        assert dispatched == []


class TestRedeliveryDedup:
    """The window is the LAST gate, so only turns-to-be occupy it."""

    @staticmethod
    def _collector():
        seen: list[LarkInbound] = []

        async def dispatch(inbound: LarkInbound) -> None:
            seen.append(inbound)

        return seen, dispatch

    @pytest.mark.asyncio
    async def test_duplicate_message_id_dispatches_once(self) -> None:
        """lark's WS may redeliver an event; a repeated turn re-runs tool side
        effects that are not idempotent."""
        dispatched, dispatch = self._collector()
        t = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc"], dispatch=dispatch)

        await t.receive(_inbound("ou_abc", "hello", message_id="m1"))
        await t.receive(_inbound("ou_abc", "hello", message_id="m1"))

        assert len(dispatched) == 1

    @pytest.mark.asyncio
    async def test_unauthorized_traffic_cannot_evict_an_authorized_id(self) -> None:
        """The reason the window sits behind ``authorize``.

        Recorded any earlier, traffic that never drives a turn -- an
        unauthorized sender, a group the bot merely sits in, a sticker -- burns
        slots, and crossing the cap evicts 300 at once. The victim is an
        AUTHORIZED id, so Feishu's redelivery of that message drives its turn a
        second time: exactly the case dedup exists to prevent, caused by dedup's
        own bookkeeping.
        """
        dispatched, dispatch = self._collector()
        t = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc"], dispatch=dispatch)

        await t.receive(_inbound("ou_abc", "hello", message_id="authorized-1"))
        assert len(dispatched) == 1

        # Well past _SEEN_MAX, all from a sender the allow-list rejects.
        for i in range(_SEEN_MAX * 2):
            await t.receive(_inbound("ou_stranger", "spam", message_id=f"unauth-{i}"))

        assert len(dispatched) == 1, "unauthorized traffic reached the dispatcher"
        assert len(t._seen) == 1, f"unauthorized traffic occupied the window ({len(t._seen)})"

        # The redelivery must still be recognised as a duplicate.
        await t.receive(_inbound("ou_abc", "hello", message_id="authorized-1"))
        assert len(dispatched) == 1, "an authorized id was evicted and its turn repeated"

    @pytest.mark.asyncio
    async def test_eviction_is_arrival_ordered_and_batched(self) -> None:
        """Crossing ``_SEEN_MAX`` trims to ``_SEEN_KEEP`` in one pass, oldest
        first. Arrival order matters: a set's iteration order is arbitrary, so
        trimming one would drop random ids -- letting an old redelivery through
        while evicting a fresh one."""
        _dispatched, dispatch = self._collector()
        t = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc"], dispatch=dispatch)

        for i in range(_SEEN_MAX + 1):
            await t.receive(_inbound("ou_abc", "hi", message_id=f"id-{i}"))

        assert len(t._seen) == _SEEN_KEEP
        assert "id-0" not in t._seen, "oldest id survived the trim"
        assert f"id-{_SEEN_MAX}" in t._seen, "newest id was evicted"

    @pytest.mark.asyncio
    async def test_a_missing_message_id_is_not_recorded(self) -> None:
        """An id-less frame cannot be deduplicated, and must not insert an empty
        key that would then swallow every other id-less frame."""
        dispatched, dispatch = self._collector()
        t = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc"], dispatch=dispatch)

        await t.receive(_inbound("ou_abc", "one", message_id=""))
        await t.receive(_inbound("ou_abc", "two", message_id=""))

        assert len(dispatched) == 2
        assert "" not in t._seen


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_connect_disconnect_delegate(self) -> None:
        client = FakeClient()
        t = FeishuTransport(client, allowed_open_ids=["ou_abc"])
        await t.connect()
        assert client.started is True
        await t.disconnect()
        assert client.closed is True


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_delegates_to_client(self) -> None:
        client = FakeClient()
        t = FeishuTransport(client, allowed_open_ids=["ou_abc"])
        await t.send_message("msg_anchor", "reply text")
        assert client.replies == [("msg_anchor", "reply text")]

    @pytest.mark.asyncio
    async def test_a_refused_reply_RAISES_rather_than_returning(self) -> None:
        """The load-bearing half of ``returns_message_id=False``.

        A Feishu reply carries no message id, so the return value cannot express
        failure and every caller reads "nothing raised" as delivery. Discarding
        ``send_reply``'s answer therefore made a dropped message indistinguishable
        from a delivered one -- and a mirror leg then persists the link and reports
        success for a reply the user never saw.
        """
        client = FakeClient()
        client.send_reply = _refusing_reply  # type: ignore[method-assign]
        t = FeishuTransport(client, allowed_open_ids=["ou_abc"])
        with pytest.raises(RuntimeError):
            await t.send_message("msg_anchor", "reply text")

    @pytest.mark.asyncio
    async def test_no_anchor_raises_too(self) -> None:
        # Same reason: returning "" here would report a send that never happened.
        t = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc"])
        with pytest.raises(RuntimeError):
            await t.send_message("", "reply text")

    def test_the_capability_declares_the_id_less_convention(self) -> None:
        t = FeishuTransport(FakeClient(), allowed_open_ids=["ou_abc"])
        assert t.capabilities.returns_message_id is False


async def _refusing_reply(message_id: str, text: str) -> bool:
    return False


def _msg(user_id: str) -> InboundMessage:
    return InboundMessage(
        channel_type="feishu", user_id=user_id, conversation_id=user_id, text="hi"
    )
