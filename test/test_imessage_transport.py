"""Tests for kiro_crew.imessage.transport (IMessageTransport, Layer 1).

Every inbound shape is driven from a recorded bridge payload under
``test/fixtures/channels/imessage/``, so the parser is pinned against a real
sample rather than against prose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.imessage.client import IMessageInbound, parse_inbound
from kiro_crew.imessage.transport import (
    IMESSAGE_CAPABILITIES,
    IMESSAGE_SAFE_MESSAGE_CHARS,
    IMessageTransport,
)
from kiro_crew.messaging.transport import InboundMessage

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels" / "imessage"

OWNER = "+15551234567"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _recorded(name: str) -> IMessageInbound:
    inbound = parse_inbound(_fixture(name)["params"]["message"])
    assert inbound is not None
    return inbound


class FakeClient:
    """Minimal IMessageClient stand-in recording lifecycle + sends."""

    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.sent: list[tuple[str, str]] = []
        #: Bodies this fake claims to have sent, consumed on match — the same
        #: consume-once contract the real ledger has. The real one is tested
        #: against the real client in test_imessage_client.py; here it is a seam
        #: so these tests can assert the transport CONSULTS it, and when.
        self.own_echo_texts: list[str] = []
        self.echo_checks: list[IMessageInbound] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def send(self, to: str, text: str) -> str:
        self.sent.append((to, text))
        return "GUID-OUT"

    def is_own_echo(self, inbound: IMessageInbound) -> bool:
        self.echo_checks.append(inbound)
        if inbound.text in self.own_echo_texts:
            self.own_echo_texts.remove(inbound.text)
            return True
        return False


def _transport(*allowed: str) -> tuple[IMessageTransport, FakeClient, list[IMessageInbound]]:
    client = FakeClient()
    dispatched: list[IMessageInbound] = []

    async def dispatch(inbound: IMessageInbound) -> None:
        dispatched.append(inbound)

    transport = IMessageTransport(
        client,  # type: ignore[arg-type]
        allowed_handles=allowed,
        dispatch=dispatch,
    )
    return transport, client, dispatched


def _msg(user_id: str) -> InboundMessage:
    return InboundMessage(
        channel_type="imessage", user_id=user_id, conversation_id=user_id, text="hi"
    )


class TestCapabilities:
    def test_imessage_shape(self) -> None:
        cap = IMESSAGE_CAPABILITIES
        # No message mutation exists on this platform at all.
        assert cap.streaming is False
        assert cap.edit is False
        assert cap.reactions is False
        # v1 is text-only in both directions.
        assert cap.files_inbound is False
        assert cap.files_outbound is False
        assert cap.rich_blocks is False
        assert cap.threads is False
        assert cap.max_buttons == 0  # no tappable choices
        assert cap.max_message_chars == IMESSAGE_SAFE_MESSAGE_CHARS
        # A Mac may message a handle at any time; there is no 24h window.
        assert cap.supports_proactive_send is True
        # Inbound routes off the handle, not a mirrored session binding.
        assert cap.supports_session_resume is False

    def test_the_char_cap_is_declared_conservatively(self) -> None:
        # iMessage publishes no maximum. This field is a claim other code trusts
        # (the dashboard mirror leg chunks against it), so under-declaring costs
        # an extra message while over-declaring risks a silent refusal.
        assert 1000 <= IMESSAGE_SAFE_MESSAGE_CHARS <= 8000

    def test_channel_type_is_the_registry_key(self) -> None:
        assert IMessageTransport.channel_type == "imessage"


class TestAuthorize:
    def test_an_allowlisted_handle_is_permitted(self) -> None:
        transport, _, _ = _transport(OWNER)
        assert transport.authorize(_msg(OWNER)) is True

    def test_an_empty_allowlist_authorizes_nobody(self) -> None:
        # There is no org boundary in front of iMessage: anyone who knows the
        # number can send to it, so an unconfigured channel must answer no one.
        transport, _, _ = _transport()
        with patch("kiro_crew.imessage.transport.sel") as mock_sel:
            assert transport.authorize(_msg(OWNER)) is False
        mock_sel().log_api_access.assert_called_once()

    def test_a_stranger_is_denied_and_audited(self) -> None:
        transport, _, _ = _transport(OWNER)
        with patch("kiro_crew.imessage.transport.sel") as mock_sel:
            assert transport.authorize(_msg("+15559999999")) is False
        mock_sel().log_api_access.assert_called_once()

    def test_an_empty_handle_is_denied_and_audited(self) -> None:
        transport, _, _ = _transport(OWNER)
        with patch("kiro_crew.imessage.transport.sel") as mock_sel:
            assert transport.authorize(_msg("")) is False
        mock_sel().log_api_access.assert_called_once()

    def test_handle_formatting_does_not_change_the_verdict(self) -> None:
        transport, _, _ = _transport("+1 (555) 123-4567")
        assert transport.authorize(_msg("+15551234567")) is True

    def test_email_case_does_not_change_the_verdict(self) -> None:
        transport, _, _ = _transport("Me@Example.com")
        assert transport.authorize(_msg("me@example.COM")) is True

    def test_the_audit_never_logs_a_whole_handle(self) -> None:
        # The caller is a phone number or an email address.
        transport, _, _ = _transport(OWNER)
        with patch("kiro_crew.imessage.transport.sel") as mock_sel:
            transport.authorize(_msg("+15559999999"))
        caller = mock_sel().log_api_access.call_args.kwargs["caller"]
        assert caller == "+15***"
        assert "9999" not in caller


class TestReceive:
    @pytest.mark.asyncio
    async def test_a_recorded_direct_message_is_dispatched(self) -> None:
        transport, _, dispatched = _transport(OWNER)
        await transport.receive(_recorded("watch_message_direct"))
        assert len(dispatched) == 1
        assert dispatched[0].text == "what's the CI status on the upload PR?"
        assert dispatched[0].chat_selector == {"chat_guid": "iMessage;-;+15551234567"}

    @pytest.mark.asyncio
    async def test_a_recorded_group_message_fails_closed_and_is_audited(self) -> None:
        # A reply in a group would deliver tool output to members who are not on
        # the allowlist.
        transport, _, dispatched = _transport(OWNER, "+15559876543")
        with patch("kiro_crew.imessage.transport.sel") as mock_sel:
            await transport.receive(_recorded("watch_message_group"))
        assert dispatched == []
        outcome = mock_sel().log_api_access.call_args.kwargs["outcome"]
        assert outcome == "denied_group_chat"

    @pytest.mark.asyncio
    async def test_the_group_gate_runs_even_for_an_allowlisted_sender(self) -> None:
        transport, _, dispatched = _transport("+15559876543")
        with patch("kiro_crew.imessage.transport.sel"):
            await transport.receive(_recorded("watch_message_group"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_own_messages_are_dropped_without_an_audit_event(self) -> None:
        # The all-chat watch echoes the agent's own replies. Auditing them would
        # write one entry per outbound message, and it is not a denial anyway.
        transport, _, dispatched = _transport(OWNER)
        recorded = _recorded("watch_message_direct")
        recorded.is_from_me = True
        with patch("kiro_crew.imessage.transport.sel") as mock_sel:
            await transport.receive(recorded)
        assert dispatched == []
        mock_sel().log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_own_messages_are_dropped_before_the_group_gate(self) -> None:
        # The agent's own reply in a group is still its own reply, not a denial.
        transport, _, dispatched = _transport(OWNER)
        recorded = _recorded("watch_message_group")
        recorded.is_from_me = True
        with patch("kiro_crew.imessage.transport.sel") as mock_sel:
            await transport.receive(recorded)
        assert dispatched == []
        mock_sel().log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_empty_text_is_dropped(self) -> None:
        transport, _, dispatched = _transport(OWNER)
        recorded = _recorded("watch_message_direct")
        recorded.text = ""
        await transport.receive(recorded)
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_an_own_echo_is_dropped_even_when_the_platform_calls_it_inbound(
        self,
    ) -> None:
        # The self-chat loop of issue #5246: the agent's own reply arrives with
        # is_from_me FALSE, from a handle that is on the allowlist because it is
        # the user's own. Nothing about the row says "this is ours" -- only the
        # client's record of having sent it does.
        transport, client, dispatched = _transport(OWNER)
        recorded = _recorded("watch_message_direct")
        recorded.is_from_me = False
        client.own_echo_texts.append(recorded.text)
        with patch("kiro_crew.imessage.transport.sel") as mock_sel:
            await transport.receive(recorded)
        assert dispatched == []
        # Own traffic, not a denial: auditing it would write one entry per
        # outbound message, exactly as for the is_from_me case above.
        mock_sel().log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_attributed_copy_does_not_consume_the_echo_record(self) -> None:
        # Ordering guard. If the is_from_me copy were checked against the ledger
        # it would consume the entry, and the UNattributed echo that follows
        # would then be dispatched -- restoring the loop while looking fixed.
        transport, client, dispatched = _transport(OWNER)
        attributed = _recorded("watch_message_direct")
        attributed.is_from_me = True
        client.own_echo_texts.append(attributed.text)
        await transport.receive(attributed)
        assert client.echo_checks == []

        echo = _recorded("watch_message_direct")
        echo.is_from_me = False
        await transport.receive(echo)
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_a_group_row_cannot_spend_the_record_the_real_echo_needs(self) -> None:
        # The echo check consumes, so it has to run AFTER the gates that drop a
        # row anyway. A group message carrying the same words would otherwise
        # spend the record on its way out, and the DM echo that follows would
        # find nothing and be answered -- the loop, reopened by a side effect.
        transport, client, dispatched = _transport(OWNER)
        group = _recorded("watch_message_group")
        client.own_echo_texts.append(group.text)
        with patch("kiro_crew.imessage.transport.sel"):
            await transport.receive(group)
        assert client.echo_checks == []
        assert client.own_echo_texts == [group.text], "the group row spent the record"

    @pytest.mark.asyncio
    async def test_an_unlisted_sender_cannot_spend_the_record_either(self) -> None:
        transport, client, _dispatched = _transport("+15559876543")
        recorded = _recorded("watch_message_direct")
        client.own_echo_texts.append(recorded.text)
        with patch("kiro_crew.imessage.transport.sel"):
            await transport.receive(recorded)
        assert client.echo_checks == []
        assert client.own_echo_texts == [recorded.text]

    @pytest.mark.asyncio
    async def test_a_message_that_is_not_an_echo_still_reaches_the_agent(self) -> None:
        # The guard must not swallow ordinary traffic from the same handle.
        transport, client, dispatched = _transport(OWNER)
        client.own_echo_texts.append("something else entirely")
        await transport.receive(_recorded("watch_message_direct"))
        assert len(dispatched) == 1

    @pytest.mark.asyncio
    async def test_an_unauthorized_sender_gets_no_reply_at_all(self) -> None:
        # An unknown sender must learn nothing about what they reached.
        transport, client, dispatched = _transport(OWNER)
        recorded = _recorded("watch_message_direct")
        recorded.handle = "+15559999999"
        with patch("kiro_crew.imessage.transport.sel"):
            await transport.receive(recorded)
        assert dispatched == []
        assert client.sent == []

    @pytest.mark.asyncio
    async def test_a_foreign_envelope_is_ignored(self) -> None:
        transport, _, dispatched = _transport(OWNER)
        await transport.receive({"not": "an inbound"})
        await transport.receive(None)
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_a_transport_with_no_dispatch_does_not_raise(self) -> None:
        client = FakeClient()
        transport = IMessageTransport(client, allowed_handles=[OWNER])  # type: ignore[arg-type]
        await transport.receive(_recorded("watch_message_direct"))


class TestConfiguredTargets:
    def test_each_allowlisted_handle_is_an_advertised_target(self) -> None:
        transport, _, _ = _transport(OWNER, "me@example.com")
        targets = transport.configured_targets()
        assert {t.target_id for t in targets} == {f"user:{OWNER}", "user:me@example.com"}
        assert all(t.label.startswith("iMessage · ") for t in targets)

    def test_an_empty_allowlist_advertises_nothing(self) -> None:
        transport, _, _ = _transport()
        assert transport.configured_targets() == []

    @pytest.mark.asyncio
    async def test_an_allowlisted_target_resolves_to_its_handle(self) -> None:
        transport, _, _ = _transport(OWNER)
        assert await transport.resolve_configured_target(f"user:{OWNER}") == (OWNER, None)

    @pytest.mark.asyncio
    async def test_a_target_outside_the_allowlist_does_not_resolve(self) -> None:
        transport, _, _ = _transport(OWNER)
        assert await transport.resolve_configured_target("user:+15559999999") is None

    @pytest.mark.asyncio
    async def test_a_malformed_target_does_not_resolve(self) -> None:
        transport, _, _ = _transport(OWNER)
        assert await transport.resolve_configured_target(OWNER) is None
        assert await transport.resolve_configured_target(f"room:{OWNER}") is None


class TestOutboundAndLifecycle:
    @pytest.mark.asyncio
    async def test_send_message_routes_to_the_handle(self) -> None:
        transport, client, _ = _transport(OWNER)
        assert await transport.send_message(OWNER, "hello") == "GUID-OUT"
        assert client.sent == [(OWNER, "hello")]

    @pytest.mark.asyncio
    async def test_the_handle_is_its_own_conversation(self) -> None:
        transport, _, _ = _transport(OWNER)
        assert await transport.resolve_conversation(OWNER) == OWNER

    @pytest.mark.asyncio
    async def test_history_is_not_read_back_out_of_the_messages_database(self) -> None:
        # Reading it would pull in messages the user never addressed to the agent.
        transport, _, _ = _transport(OWNER)
        assert await transport.fetch_history(OWNER) == []

    @pytest.mark.asyncio
    async def test_connect_and_disconnect_drive_the_client(self) -> None:
        transport, client, _ = _transport(OWNER)
        await transport.connect()
        assert client.started
        await transport.disconnect()
        assert client.closed

    def test_the_client_is_exposed_not_hidden(self) -> None:
        transport, client, _ = _transport(OWNER)
        assert transport.client is client
