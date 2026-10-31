"""Proactive delivery of unattended output to a Feishu DM.

A background ``spawn_run`` completion in a Feishu-born session is delivered to
the parent through the gateway's cross-surface reply leg, which routes every
non-Slack channel through ``_resolve_channel_target`` and refuses any transport
whose ``supports_proactive_send`` is False. The Feishu v1 integration declared
``supports_proactive_send=False`` and had no proactive-send path (replies were
anchored to an inbound ``message_id``), so an async completion -- which has no
inbound message to anchor to -- was silently dropped: it rendered in the
dashboard slot but never reached Feishu, with no outbound/send record and no
exception. The user reported "飞书上没收到这个结果" (did not receive the result on
Feishu).

These tests pin the proactive-send behaviour the fix adds:

* the transport advertises ``supports_proactive_send``;
* a proactive ``send_message`` to an allow-listed peer goes out over the
  client's ``im.v1.message.create`` path, so an unattended completion reaches
  Feishu -- and keeps doing so after a restart, because the send gate is the
  allow-list alone;
* ``configured_targets`` advertises configured peers only, each available, so
  the owner-DM resolver never reads a learned peer as the operator.
"""

from __future__ import annotations

import pytest

from kiro_crew.feishu.transport import FEISHU_CAPABILITIES, FeishuTransport


class FakeClient:
    """LarkClient stand-in recording both reply and proactive sends."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.proactive: list[tuple[str, str]] = []

    async def start(self) -> None:  # pragma: no cover - lifecycle
        pass

    async def close(self) -> None:  # pragma: no cover - lifecycle
        pass

    async def send_reply(self, message_id: str, text: str) -> bool:
        self.replies.append((message_id, text))
        return True

    async def send_proactive(self, receive_id: str, text: str) -> bool:
        self.proactive.append((receive_id, text))
        return True


class TestProactiveCapability:
    def test_transport_supports_proactive_send(self) -> None:
        # The transport-wide flag must be True so the shared cross-surface send
        # ladder (``_resolve_channel_target``) does not refuse the send outright.
        assert FEISHU_CAPABILITIES.supports_proactive_send is True

    def test_returns_no_message_id_contract_unchanged(self) -> None:
        # A Feishu send carries no message id back: empty return is SUCCESS and a
        # failure raises. This contract is unchanged by the fix.
        assert FEISHU_CAPABILITIES.returns_message_id is False


class TestConfiguredTargets:
    def test_allowlisted_peers_are_advertised_available(self) -> None:
        client = FakeClient()
        transport = FeishuTransport(client, allowed_open_ids=["ou_abc", "ou_def"])
        targets = {t.target_id: t for t in transport.configured_targets()}
        assert set(targets) == {"user:ou_abc", "user:ou_def"}
        assert all(t.available for t in targets.values())

    def test_targets_are_drawn_only_from_the_allowlist(self) -> None:
        # No learned/inbound peer leaks into configured_targets: the owner-DM
        # resolver infers the operator from the sole available direct target, so
        # a peer the user never configured must never appear there.
        client = FakeClient()
        transport = FeishuTransport(client, allowed_open_ids=["ou_abc"])
        ids = {t.target_id for t in transport.configured_targets()}
        assert ids == {"user:ou_abc"}


class TestProactiveDelivery:
    @pytest.mark.asyncio
    async def test_resolve_configured_target_allowlisted_peer(self) -> None:
        client = FakeClient()
        transport = FeishuTransport(client, allowed_open_ids=["ou_abc"])
        resolved = await transport.resolve_configured_target("user:ou_abc")
        assert resolved == ("ou_abc", None)

    @pytest.mark.asyncio
    async def test_resolve_configured_target_unlisted_peer_refused(self) -> None:
        client = FakeClient()
        transport = FeishuTransport(client, allowed_open_ids=["ou_abc"])
        assert await transport.resolve_configured_target("user:ou_not_listed") is None

    @pytest.mark.asyncio
    async def test_proactive_send_reaches_feishu(self) -> None:
        client = FakeClient()
        transport = FeishuTransport(client, allowed_open_ids=["ou_abc"])
        # The async completion delivery: no inbound message_id to anchor to, so
        # the conversation id is the peer's own open_id.
        result = await transport.send_message("ou_abc", "H100 pricing is ...")
        assert result == ""  # empty id == success (no message id contract)
        assert client.proactive == [("ou_abc", "H100 pricing is ...")]
        assert client.replies == []  # NOT routed through the inbound-reply path

    @pytest.mark.asyncio
    async def test_proactive_send_to_unauthorized_peer_refused(self) -> None:
        client = FakeClient()
        transport = FeishuTransport(client, allowed_open_ids=["ou_abc"])
        with pytest.raises(RuntimeError):
            await transport.send_message("ou_not_allowed", "secret")
        assert client.proactive == []

    def test_may_send_to_allowlisted_peer(self) -> None:
        client = FakeClient()
        transport = FeishuTransport(client, allowed_open_ids=["ou_abc"])
        assert transport.may_send_to("ou_abc") is True

    def test_may_send_to_unlisted_peer_refused(self) -> None:
        client = FakeClient()
        transport = FeishuTransport(client, allowed_open_ids=["ou_abc"])
        assert transport.may_send_to("ou_not_listed") is False

    @pytest.mark.asyncio
    async def test_proactive_send_survives_a_restart(self) -> None:
        # After a gateway restart there is no in-process inbound history, yet a
        # persisted mirror binding to an allow-listed peer must still deliver.
        # The send gate is the allow-list, so a fresh transport sends fine.
        client = FakeClient()
        transport = FeishuTransport(client, allowed_open_ids=["ou_abc"])
        result = await transport.send_message("ou_abc", "result after restart")
        assert result == ""
        assert client.proactive == [("ou_abc", "result after restart")]
