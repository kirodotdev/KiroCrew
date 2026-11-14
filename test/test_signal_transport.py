"""Tests for kiro_crew.signal_cli.transport (SignalTransport, Layer 1).

Inbound shapes are parsed from the recorded-shape fixtures under
``test/fixtures/channels/signal/`` (taken from signal-cli's own jsonRpc man
page), so the classifier is pinned against the wire format, not prose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.messaging.transport import InboundMessage, delivery_confirmed
from kiro_crew.signal_cli.client import SignalClient, SignalInbound, parse_receive
from kiro_crew.signal_cli.transport import (
    SIGNAL_CAPABILITIES,
    SignalTransport,
    allowed_numbers_from_config,
    warn_unusable_entries,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels" / "signal"

ACCOUNT = "+447700900001"
ACCOUNT_ACI = "aaaaaaaa-1111-4111-8111-111111111111"
OWNER = "+447700900002"
OWNER_ACI = "bbbbbbbb-2222-4222-8222-222222222222"
STRANGER = "+447700900009"


def _params(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["params"]


def _recorded(name: str, client: SignalClient) -> SignalInbound:
    inbound = parse_receive(_params(name), client.account, client.self_ids())
    assert inbound is not None
    return inbound


class RecordingClient(SignalClient):
    """The real client (real ledger, real identity), with sends recorded."""

    def __init__(self) -> None:
        super().__init__(ACCOUNT)
        self.sent: list[tuple[str, str]] = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def send(self, recipient: str, text: str) -> str:
        self.sent.append((recipient, text))
        return "1700000009999"


def _transport(
    *allowed: str,
) -> tuple[SignalTransport, RecordingClient, list[SignalInbound]]:
    client = RecordingClient()
    dispatched: list[SignalInbound] = []

    async def dispatch(inbound: SignalInbound) -> None:
        dispatched.append(inbound)

    transport = SignalTransport(client, allowed_numbers=allowed, dispatch=dispatch)
    return transport, client, dispatched


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    class _Sel:
        def log_api_access(self, **kwargs: Any) -> None:
            recorded.append(kwargs)

    monkeypatch.setattr("kiro_crew.signal_cli.transport.sel", lambda: _Sel())
    return recorded


class TestCapabilities:
    def test_declared_capabilities(self) -> None:
        caps = SIGNAL_CAPABILITIES
        assert (caps.streaming, caps.edit, caps.threads, caps.max_buttons) == (
            False,
            False,
            False,
            0,
        )
        assert caps.supports_proactive_send is True
        assert caps.max_message_chars == 2000
        # send answers with the message timestamp and raises on failure.
        assert caps.returns_message_id is True
        assert delivery_confirmed(caps, "1700000000000")
        assert not delivery_confirmed(caps, "")

    def test_channel_type(self) -> None:
        assert SignalTransport.channel_type == "signal"


class TestAllowList:
    def test_the_config_reading_normalizes_and_drops_junk(self) -> None:
        assert allowed_numbers_from_config(
            ["+44 7700 900002", "", None, True, "0044 7700 900009", OWNER_ACI.upper()]
        ) == [OWNER, STRANGER, OWNER_ACI]
        # An unquoted JSON number gains its "+".
        assert allowed_numbers_from_config([447700900002]) == [OWNER]

    def test_unusable_entries_are_warned_about_redacted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="kiro_crew.signal_cli.transport"):
            assert warn_unusable_entries(["07700900777", OWNER, "", "+12"]) == 2
        assert "0770***" in caplog.text and "07700900777" not in caplog.text

    def test_reconfigure_warns_about_unusable_entries(
        self, caplog: pytest.LogCaptureFixture, audits: list[dict[str, Any]]
    ) -> None:
        transport, _, _ = _transport(OWNER)
        with caplog.at_level("WARNING", logger="kiro_crew.signal_cli.transport"):
            transport.reconfigure(type("S", (), {"allowed_numbers": [OWNER, "bob"]})())
        assert "never match" in caplog.text
        assert allowed_numbers_from_config("nope") is None

    def test_an_empty_allow_list_authorizes_nobody(self, audits: list[dict[str, Any]]) -> None:
        transport, _, _ = _transport()
        msg = InboundMessage(channel_type="signal", user_id=OWNER, conversation_id=OWNER, text="x")
        assert transport.authorize(msg) is False
        assert audits[0]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_an_allowed_sender_is_dispatched_with_its_route(
        self, audits: list[dict[str, Any]]
    ) -> None:
        transport, client, dispatched = _transport(OWNER)
        await transport.receive(_recorded("receive_dm", client))
        assert len(dispatched) == 1
        assert dispatched[0].principal == OWNER
        assert dispatched[0].conversation_id == OWNER
        assert dispatched[0].person_origin is True
        assert audits == []

    @pytest.mark.asyncio
    async def test_a_sender_listed_by_aci_is_matched_and_answered_by_aci(self) -> None:
        transport, client, dispatched = _transport(OWNER_ACI)
        await transport.receive(_recorded("receive_dm", client))
        assert [d.conversation_id for d in dispatched] == [OWNER_ACI]

    @pytest.mark.asyncio
    async def test_a_numberless_sender_is_matched_by_aci(self) -> None:
        transport, client, dispatched = _transport(OWNER_ACI)
        params = _params("receive_dm")
        params["envelope"]["sourceNumber"] = None
        params["envelope"]["source"] = OWNER_ACI
        inbound = parse_receive(params, ACCOUNT)
        assert inbound is not None
        await transport.receive(inbound)
        assert [d.conversation_id for d in dispatched] == [OWNER_ACI]

    @pytest.mark.asyncio
    async def test_an_unknown_sender_is_dropped_silently_with_an_audit(
        self, audits: list[dict[str, Any]]
    ) -> None:
        transport, client, dispatched = _transport(STRANGER)
        await transport.receive(_recorded("receive_dm", client))
        assert dispatched == []
        assert client.sent == []  # no reply: an unknown sender learns nothing
        assert [a["outcome"] for a in audits] == ["denied"]
        assert OWNER not in audits[0]["caller"]  # redacted

    def test_reconfigure_replaces_the_roster(self, audits: list[dict[str, Any]]) -> None:
        transport, _, _ = _transport(OWNER)
        transport.reconfigure(type("S", (), {"allowed_numbers": [STRANGER]})())
        assert transport.may_send_to(STRANGER) and not transport.may_send_to(OWNER)
        assert audits[-1]["outcome"] == "allow_list_changed"

    def test_reconfigure_adopts_an_empty_list_and_keeps_a_malformed_one(self) -> None:
        transport, _, _ = _transport(OWNER)
        transport.reconfigure(type("S", (), {"allowed_numbers": "bad"})())
        assert transport.may_send_to(OWNER)
        transport.reconfigure(type("S", (), {"allowed_numbers": []})())
        assert not transport.may_send_to(OWNER)


class TestOperator:
    """The operator is narrower than the allow-list (approvals, commands, steer)."""

    def test_the_account_is_the_operator_when_it_is_listed(self) -> None:
        transport, _, _ = _transport(ACCOUNT, OWNER)
        assert transport.is_operator(ACCOUNT) is True
        # A contact on the same list may chat, not act as the agent.
        assert transport.is_operator(OWNER) is False

    def test_the_sole_entry_is_the_operator(self) -> None:
        transport, _, _ = _transport(OWNER)
        assert transport.is_operator(OWNER) is True
        assert transport.is_operator(STRANGER) is False

    def test_several_entries_without_the_account_make_nobody_the_operator(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="kiro_crew.signal_cli.transport"):
            transport, _, _ = _transport(OWNER, STRANGER)
        assert transport.is_operator(OWNER) is False
        assert transport.is_operator(STRANGER) is False
        assert caplog.text.count("no sender is the operator") == 1
        assert "tool approvals and operator commands" in caplog.text

    def test_a_single_entry_or_a_listed_account_logs_no_operator_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="kiro_crew.signal_cli.transport"):
            _transport(OWNER)
            _transport(ACCOUNT, OWNER, STRANGER)
        assert "no sender is the operator" not in caplog.text

    def test_an_empty_identity_or_list_is_never_the_operator(self) -> None:
        transport, _, _ = _transport()
        assert transport.is_operator("") is False
        assert transport.is_operator(OWNER) is False

    def test_reconfigure_recomputes_the_operator(
        self, caplog: pytest.LogCaptureFixture, audits: list[dict[str, Any]]
    ) -> None:
        transport, _, _ = _transport(OWNER)
        assert transport.is_operator(OWNER) is True
        with caplog.at_level("WARNING", logger="kiro_crew.signal_cli.transport"):
            transport.reconfigure(type("S", (), {"allowed_numbers": [OWNER, STRANGER]})())
        assert transport.is_operator(OWNER) is False
        assert "no sender is the operator" in caplog.text
        transport.reconfigure(type("S", (), {"allowed_numbers": [ACCOUNT, OWNER, STRANGER]})())
        assert transport.is_operator(ACCOUNT) is True
        assert transport.is_operator(OWNER) is False

    @pytest.mark.asyncio
    async def test_receive_carries_the_verdict_onto_the_inbound(
        self, audits: list[dict[str, Any]]
    ) -> None:
        transport, client, dispatched = _transport(ACCOUNT, OWNER)
        await transport.receive(_recorded("receive_dm", client))
        await transport.receive(_recorded("receive_note_to_self", client))
        assert [(d.principal, d.operator) for d in dispatched] == [
            (OWNER, False),
            (ACCOUNT, True),
        ]


class TestDenialAudits:
    @pytest.mark.asyncio
    async def test_the_linked_setup_audits_each_refused_sender_once_per_interval(
        self, audits: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The account itself is allow-listed: signal-cli is a linked device, so
        # every DM and group message the owner receives arrives here. A refusal
        # is still audited -- it may be someone trying the agent -- but at the
        # bot number's rate, so a busy inbox does not flood the log.
        import kiro_crew.signal_cli.transport as transport_mod

        now = [1000.0]
        monkeypatch.setattr(transport_mod, "_monotonic", lambda: now[0])
        transport, client, dispatched = _transport(ACCOUNT)
        for name in ("receive_dm", "receive_group", "receive_dm", "receive_group"):
            await transport.receive(_recorded(name, client))
        assert dispatched == []
        assert [a["outcome"] for a in audits] == ["denied", "denied_group_chat"]
        assert all(OWNER not in a["caller"] for a in audits)  # redacted
        now[0] += transport_mod.DENIAL_AUDIT_INTERVAL_S - 1
        await transport.receive(_recorded("receive_dm", client))
        assert len(audits) == 2
        now[0] += 1
        await transport.receive(_recorded("receive_dm", client))
        assert [a["outcome"] for a in audits] == ["denied", "denied_group_chat", "denied"]

    @pytest.mark.asyncio
    async def test_a_bot_number_audits_each_sender_once_per_interval(
        self, audits: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.signal_cli.transport as transport_mod

        now = [1000.0]
        monkeypatch.setattr(transport_mod, "_monotonic", lambda: now[0])
        transport, client, _ = _transport(STRANGER)
        for _ in range(3):
            await transport.receive(_recorded("receive_dm", client))
        await transport.receive(_recorded("receive_group", client))
        assert [a["outcome"] for a in audits] == ["denied", "denied_group_chat"]
        now[0] += transport_mod.DENIAL_AUDIT_INTERVAL_S
        await transport.receive(_recorded("receive_dm", client))
        assert [a["outcome"] for a in audits] == ["denied", "denied_group_chat", "denied"]


class TestRefusals:
    @pytest.mark.asyncio
    async def test_a_group_message_is_refused_even_from_an_allowed_sender(
        self, audits: list[dict[str, Any]]
    ) -> None:
        transport, client, dispatched = _transport(OWNER)
        await transport.receive(_recorded("receive_group", client))
        assert dispatched == []
        assert client.sent == []
        assert [a["outcome"] for a in audits] == ["denied_group_chat"]

    @pytest.mark.asyncio
    async def test_the_owners_message_to_a_third_party_is_never_input(
        self, audits: list[dict[str, Any]]
    ) -> None:
        transport, client, dispatched = _transport(ACCOUNT, OWNER)
        await transport.receive(_recorded("receive_sync_to_other", client))
        assert dispatched == []
        assert audits == []

    @pytest.mark.asyncio
    async def test_receipts_and_empty_bodies_are_ignored(self) -> None:
        transport, client, dispatched = _transport(OWNER)
        await transport.receive(_recorded("receive_receipt", client))
        empty = _recorded("receive_dm", client)
        empty.text = ""
        await transport.receive(empty)
        await transport.receive("not an inbound")
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_a_data_message_from_the_account_itself_is_own_traffic(
        self, audits: list[dict[str, Any]]
    ) -> None:
        transport, client, dispatched = _transport(ACCOUNT)
        inbound = _recorded("receive_dm", client)
        inbound.source_number = ACCOUNT
        inbound.source_uuid = ""
        await transport.receive(inbound)
        assert dispatched == [] and audits == []


class TestNoteToSelf:
    @pytest.mark.asyncio
    async def test_note_to_self_is_the_owner_when_the_account_is_allowed(self) -> None:
        transport, client, dispatched = _transport(ACCOUNT)
        await transport.receive(_recorded("receive_note_to_self", client))
        assert len(dispatched) == 1
        assert dispatched[0].conversation_id == ACCOUNT
        assert client.is_self(dispatched[0].conversation_id)

    @pytest.mark.asyncio
    async def test_note_to_self_is_refused_when_the_account_is_not_allowed(
        self, audits: list[dict[str, Any]]
    ) -> None:
        transport, client, dispatched = _transport(OWNER)
        await transport.receive(_recorded("receive_note_to_self", client))
        assert dispatched == []
        assert [a["outcome"] for a in audits] == ["denied"]

    @pytest.mark.asyncio
    async def test_the_echo_of_our_own_reply_is_suppressed_and_consumed_once(self) -> None:
        transport, client, dispatched = _transport(ACCOUNT)
        inbound = _recorded("receive_note_to_self", client)
        record = client._remember_own_send(inbound.text)
        record.expires_at = 10**12
        await transport.receive(inbound)
        assert dispatched == []
        again = _recorded("receive_note_to_self", client)
        await transport.receive(again)
        assert len(dispatched) == 1

    @pytest.mark.asyncio
    async def test_a_refused_message_does_not_spend_the_echo_record(self) -> None:
        # The ledger is the LAST gate: a group message carrying the same body
        # must not consume the record the real echo needs.
        transport, client, dispatched = _transport(ACCOUNT)
        note = _recorded("receive_note_to_self", client)
        record = client._remember_own_send(note.text)
        record.expires_at = 10**12
        group = _recorded("receive_group", client)
        group.text = note.text
        await transport.receive(group)
        assert len(client._own_sends) == 1
        await transport.receive(note)
        assert dispatched == []


class TestOutbound:
    @pytest.mark.asyncio
    async def test_send_message_returns_the_clients_id(self) -> None:
        transport, client, _ = _transport(OWNER)
        assert await transport.send_message(OWNER, "hi") == "1700000009999"
        assert client.sent == [(OWNER, "hi")]

    def test_may_send_to_checks_the_roster(self) -> None:
        transport, _, _ = _transport(OWNER)
        assert transport.may_send_to("+44 7700 900002")
        assert not transport.may_send_to(STRANGER)
        assert not transport.may_send_to("")

    def test_note_to_self_is_sendable_when_any_account_identity_is_allowed(self) -> None:
        transport, client, _ = _transport(ACCOUNT_ACI)
        assert not transport.may_send_to(ACCOUNT)
        client._learn_self_ids([ACCOUNT_ACI])
        assert transport.may_send_to(ACCOUNT)

    @pytest.mark.asyncio
    async def test_configured_targets_round_trip(self) -> None:
        transport, _, _ = _transport(OWNER, ACCOUNT)
        targets = {t.target_id: t.label for t in transport.configured_targets()}
        assert targets == {
            f"user:{ACCOUNT}": "Signal · Note to Self",
            f"user:{OWNER}": f"Signal · {OWNER}",
        }
        assert await transport.resolve_configured_target(f"user:{OWNER}") == (OWNER, None)
        assert await transport.resolve_configured_target(f"user:{STRANGER}") is None
        assert await transport.resolve_configured_target("room:x") is None

    @pytest.mark.asyncio
    async def test_lifecycle_delegates_to_the_client(self) -> None:
        transport, client, _ = _transport(OWNER)
        await transport.connect()
        await transport.disconnect()
        assert client.started and client.closed
        assert await transport.fetch_history(OWNER) == []
