"""Tests for kiro_crew.signal_cli.client (envelope classification, sends, echo ledger).

No child process: the client's peer is replaced with a recording fake, so the
Signal semantics are exercised without signal-cli. The real framing is driven
end to end against a fake binary in ``test_signal_e2e.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew.signal_cli import client as client_mod
from kiro_crew.signal_cli.client import (
    KIND_DATA,
    KIND_NOTE_TO_SELF,
    KIND_OTHER,
    KIND_SYNC_OTHER,
    SignalClient,
    SignalInbound,
    coerce_identifier,
    identifier_problem,
    normalize_identifier,
    parse_receive,
    receive_envelope,
)
from kiro_crew.signal_cli.files import data_uri
from kiro_crew.signal_cli.rpc import RpcError, RpcTransportError
from kiro_crew.testing.wait import await_until

ACCOUNT = "+447700900001"
ACCOUNT_ACI = "aaaaaaaa-1111-4111-8111-111111111111"
OWNER = "+447700900002"
OWNER_ACI = "bbbbbbbb-2222-4222-8222-222222222222"


def _dm(text: str = "hi", *, number: str | None = OWNER, uuid: str = OWNER_ACI, **extra: Any):
    data: dict[str, Any] = {"timestamp": 1700000000100, "message": text}
    data.update(extra)
    return {
        "account": ACCOUNT,
        "envelope": {
            "source": number or uuid,
            "sourceNumber": number,
            "sourceUuid": uuid,
            "sourceName": "Owner",
            "sourceDevice": 1,
            "timestamp": 1700000000100,
            "dataMessage": data,
        },
    }


def _sync(text: str = "note", *, dest_number: str | None = ACCOUNT, dest_uuid: str = ACCOUNT_ACI):
    return {
        "account": ACCOUNT,
        "envelope": {
            "source": ACCOUNT,
            "sourceNumber": ACCOUNT,
            "sourceUuid": ACCOUNT_ACI,
            "sourceDevice": 1,
            "timestamp": 1700000000200,
            "syncMessage": {
                "sentMessage": {
                    "destination": dest_number or dest_uuid,
                    "destinationNumber": dest_number,
                    "destinationUuid": dest_uuid,
                    "timestamp": 1700000000200,
                    "message": text,
                }
            },
        },
    }


class FakePeer:
    """Records calls; answers ``send`` with an incrementing timestamp."""

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
        if method == "send":
            self.ts += 1
            return {"timestamp": self.ts}
        return {}


def _client() -> tuple[SignalClient, FakePeer]:
    client = SignalClient(ACCOUNT)
    peer = FakePeer()
    client._peer = peer  # type: ignore[assignment]
    client.ready.set()
    return client, peer


class TestNormalize:
    def test_a_leading_00_becomes_plus(self) -> None:
        assert normalize_identifier("0044 7700 900001") == "+447700900001"

    def test_coerce_gives_a_json_integer_its_plus(self) -> None:
        assert coerce_identifier(447700900001) == "+447700900001"
        assert coerce_identifier(True) == "" and coerce_identifier(1.5) == ""
        assert coerce_identifier("  +1 ") == "+1"

    @pytest.mark.parametrize(
        "value", ["+447700900001", "0044 7700 900001", ACCOUNT_ACI.upper(), 447700900001]
    )
    def test_usable_identities(self, value: Any) -> None:
        assert identifier_problem(value) == ""

    @pytest.mark.parametrize("value", ["07700900001", "+12345", "+1234567890123456", "bob"])
    def test_unusable_identities(self, value: str) -> None:
        assert "neither" in identifier_problem(value)

    def test_formatting_is_dropped_from_numbers(self) -> None:
        assert normalize_identifier("+44 (7700) 900001") == "+447700900001"

    def test_an_aci_folds_to_lowercase(self) -> None:
        assert normalize_identifier(ACCOUNT_ACI.upper()) == ACCOUNT_ACI

    @pytest.mark.parametrize("value", [None, "", "   ", 5])
    def test_empty_or_wrong_type_is_empty(self, value: Any) -> None:
        assert normalize_identifier(value) == ""

    @pytest.mark.parametrize(
        "value", ["+44 7700 900001", "0044 7700 900001", "0044-7700-900001", 447700900001]
    )
    def test_the_account_is_normalized_once_for_argv_and_self_matching(
        self, value: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # signal-cli's -a takes the bare +E.164 only; doctor reads the same form.
        monkeypatch.setattr(client_mod, "resolve_bridge_path", lambda: "/opt/signal-cli")
        client = SignalClient(value)
        assert client.account == ACCOUNT == client_mod.account_identifier(value)
        argv = client._argv()
        assert argv[argv.index("-a") + 1] == ACCOUNT
        assert client.is_self(ACCOUNT) and client.self_ids() == {ACCOUNT}


class TestReceiveParsing:
    def test_the_on_start_shape_is_read(self) -> None:
        assert receive_envelope(_dm())["dataMessage"]["message"] == "hi"  # type: ignore[index]

    def test_the_subscribe_receive_shape_is_read(self) -> None:
        wrapped = {"subscription": 0, "result": _dm("wrapped")}
        inbound = parse_receive(wrapped)
        assert inbound is not None and inbound.text == "wrapped"
        assert inbound.account == ACCOUNT

    @pytest.mark.parametrize("params", [{}, {"envelope": "x"}, {"result": []}, None, []])
    def test_no_envelope_is_none(self, params: Any) -> None:
        assert parse_receive(params) is None

    def test_a_dm_is_data_from_the_sender(self) -> None:
        inbound = parse_receive(_dm("hello"), ACCOUNT)
        assert inbound is not None
        assert inbound.kind == KIND_DATA
        assert inbound.text == "hello"
        assert inbound.sender == OWNER
        assert inbound.reply_to == OWNER
        assert inbound.timestamp == 1700000000100
        assert not inbound.is_group

    def test_a_numberless_sender_is_named_by_its_aci(self) -> None:
        inbound = parse_receive(_dm(number=None), ACCOUNT)
        assert inbound is not None
        assert inbound.sender == OWNER_ACI
        assert inbound.sender_ids == {OWNER_ACI}

    def test_the_legacy_source_fills_a_missing_number(self) -> None:
        params = _dm()
        env = params["envelope"]
        env["sourceNumber"] = None
        env["sourceUuid"] = None
        env["source"] = OWNER
        inbound = parse_receive(params)
        assert inbound is not None and inbound.source_number == OWNER

    @pytest.mark.parametrize(
        "group", [{"groupInfo": {"groupId": "g==", "type": "DELIVER"}}, {"groupV2": {"id": "g"}}]
    )
    def test_a_group_message_is_marked(self, group: dict[str, Any]) -> None:
        inbound = parse_receive(_dm(**group))
        assert inbound is not None and inbound.is_group

    def test_attachments_are_counted(self) -> None:
        inbound = parse_receive(_dm(attachments=[{"id": "1"}, {"id": "2"}]))
        assert inbound is not None and inbound.attachments == 2

    def test_a_sync_to_the_account_is_note_to_self(self) -> None:
        inbound = parse_receive(_sync("remember"), ACCOUNT)
        assert inbound is not None
        assert inbound.kind == KIND_NOTE_TO_SELF
        assert inbound.reply_to == ACCOUNT
        assert inbound.text == "remember"

    def test_note_to_self_matches_by_a_trusted_aci_when_the_number_is_absent(self) -> None:
        inbound = parse_receive(_sync(dest_number=None), ACCOUNT, [ACCOUNT_ACI])
        assert inbound is not None and inbound.kind == KIND_NOTE_TO_SELF

    def test_an_untrusted_aci_is_not_taken_from_the_envelope_itself(self) -> None:
        # The envelope's own sourceUuid is not evidence of who the account is.
        inbound = parse_receive(_sync(dest_number=None), ACCOUNT)
        assert inbound is not None and inbound.kind == KIND_SYNC_OTHER

    def test_a_sync_from_a_foreign_source_is_never_note_to_self(self) -> None:
        # Destination = the account, but the source is somebody else.
        params = _sync()
        params["envelope"]["source"] = OWNER
        params["envelope"]["sourceNumber"] = OWNER
        params["envelope"]["sourceUuid"] = OWNER_ACI
        inbound = parse_receive(params, ACCOUNT)
        assert inbound is not None and inbound.kind == KIND_SYNC_OTHER

    def test_a_sync_to_someone_else_is_never_input(self) -> None:
        inbound = parse_receive(_sync(dest_number=OWNER, dest_uuid=OWNER_ACI), ACCOUNT)
        assert inbound is not None and inbound.kind == KIND_SYNC_OTHER

    def test_a_group_sync_is_not_note_to_self(self) -> None:
        params = _sync()
        params["envelope"]["syncMessage"]["sentMessage"]["groupInfo"] = {"groupId": "g"}
        inbound = parse_receive(params, ACCOUNT)
        assert inbound is not None and inbound.kind == KIND_SYNC_OTHER

    @pytest.mark.parametrize(
        "content",
        [
            {"receiptMessage": {"when": 1, "isRead": True, "timestamps": [1]}},
            {"typingMessage": {"action": "STARTED", "timestamp": 1}},
            {"syncMessage": {"readMessages": [{"sender": OWNER, "timestamp": 1}]}},
        ],
    )
    def test_receipts_typing_and_other_syncs_are_other(self, content: dict[str, Any]) -> None:
        params = {"envelope": {"sourceNumber": OWNER, "timestamp": 1, **content}}
        inbound = parse_receive(params)
        assert inbound is not None and inbound.kind == KIND_OTHER

    def test_wrong_types_are_treated_as_absent(self) -> None:
        params = {"envelope": {"sourceNumber": 5, "timestamp": "x", "dataMessage": {"message": 3}}}
        inbound = parse_receive(params)
        assert inbound is not None
        assert (inbound.sender, inbound.text, inbound.timestamp) == ("", "", 0)


class TestSend:
    @pytest.mark.asyncio
    async def test_a_dm_send_names_the_recipient_and_returns_the_timestamp(self) -> None:
        client, peer = _client()
        mid = await client.send(OWNER, "hello")
        assert peer.calls == [("send", {"recipient": [OWNER], "message": "hello"})]
        assert mid == str(peer.ts)

    @pytest.mark.asyncio
    async def test_a_send_to_the_account_goes_to_note_to_self(self) -> None:
        client, peer = _client()
        await client.send(ACCOUNT, "to me")
        assert peer.calls == [("send", {"message": "to me", "noteToSelf": True})]

    @pytest.mark.asyncio
    async def test_empty_text_sends_nothing(self) -> None:
        client, peer = _client()
        assert await client.send(OWNER, "") == ""
        assert peer.calls == []

    @pytest.mark.asyncio
    async def test_a_failure_raises_rather_than_returning_empty(self) -> None:
        client, peer = _client()
        peer.fail = RpcError(-1, "Failed to send message")
        with pytest.raises(RpcError):
            await client.send(OWNER, "x")

    @pytest.mark.asyncio
    async def test_no_peer_raises(self) -> None:
        client = SignalClient(ACCOUNT)
        with pytest.raises(RpcTransportError):
            await client.send(OWNER, "x")


class TestUntrustedIdentity:
    @pytest.mark.asyncio
    async def test_a_changed_safety_number_is_reported_with_the_fix(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client, peer = _client()
        states: list[tuple[bool, str]] = []
        client.on_state_change = lambda ok, err: states.append((ok, err))
        peer.fail = RpcError(-4, "Failed to send message due to untrusted identities")
        with caplog.at_level("WARNING", logger="kiro_crew.signal_cli.client"):
            with pytest.raises(RpcError):
                await client.send(OWNER, "x")
        assert len(states) == 1
        connected, error = states[0]
        assert connected is True  # the channel itself is fine
        assert error.startswith("untrusted identity for +447***")
        assert "trust -a" in error and OWNER not in error
        assert "untrusted identity" in caplog.text and OWNER not in caplog.text
        # The operator's own number is personal data as well: the command names
        # it by a placeholder, in the status and in the log.
        assert "`signal-cli -a <your number> trust -a <number>`" in error
        assert ACCOUNT not in error and ACCOUNT not in caplog.text

    @pytest.mark.asyncio
    async def test_other_send_failures_do_not_touch_the_status(self) -> None:
        client, peer = _client()
        states: list[tuple[bool, str]] = []
        client.on_state_change = lambda ok, err: states.append((ok, err))
        peer.fail = RpcError(-1, "Failed to send message")
        with pytest.raises(RpcError):
            await client.send(OWNER, "x")
        assert states == []


class TestEchoLedger:
    @pytest.mark.asyncio
    async def test_the_echo_of_a_note_to_self_reply_is_suppressed_once(self) -> None:
        client, peer = _client()
        await client.send(ACCOUNT, "the answer")
        echo = parse_receive(_sync("the answer"), ACCOUNT)
        assert echo is not None
        echo.timestamp = peer.ts  # an echo carries the send's own timestamp
        assert client.is_own_echo(echo) is True
        # Consumed: the owner genuinely writing the same words later is input.
        assert client.is_own_echo(echo) is False

    @pytest.mark.asyncio
    async def test_the_echo_matches_by_timestamp_even_when_the_body_differs(self) -> None:
        client, peer = _client()
        await client.send(ACCOUNT, "answer")
        echo = SignalInbound(kind=KIND_NOTE_TO_SELF, text="(altered)", timestamp=peer.ts)
        assert client.is_own_echo(echo) is True

    @pytest.mark.asyncio
    async def test_an_owner_reply_equal_to_a_recent_send_is_not_swallowed(self) -> None:
        # Once the send's timestamp is known it alone identifies the echo, so
        # the owner typing "1" (an approval answer) right after the agent sent
        # "1" to Note to Self is input, not an echo.
        client, peer = _client()
        await client.send(ACCOUNT, "1")
        reply = SignalInbound(kind=KIND_NOTE_TO_SELF, text="1", timestamp=peer.ts + 5)
        assert client.is_own_echo(reply) is False
        assert len(client._own_sends) == 1  # the real echo can still claim it

    @pytest.mark.asyncio
    async def test_an_in_flight_send_still_matches_by_body(self) -> None:
        # Before signal-cli reports a timestamp the body is all there is.
        client, _ = _client()
        record = client._remember_own_send("in flight")
        echo = SignalInbound(kind=KIND_NOTE_TO_SELF, text="in flight", timestamp=42)
        assert client.is_own_echo(echo) is True
        assert record not in client._own_sends

    @pytest.mark.asyncio
    async def test_a_dm_send_is_not_remembered(self) -> None:
        # Only self-addressed traffic can come back as input; a DM reply leaves
        # no record that could swallow a genuine message.
        client, _ = _client()
        await client.send(OWNER, "same words")
        assert client._own_sends == []

    @pytest.mark.asyncio
    async def test_a_dm_from_another_person_is_never_an_echo(self) -> None:
        client, _ = _client()
        await client.send(ACCOUNT, "same words")
        dm = parse_receive(_dm("same words"), ACCOUNT)
        assert dm is not None
        assert client.is_own_echo(dm) is False
        assert len(client._own_sends) == 1  # not consumed

    @pytest.mark.asyncio
    async def test_the_ttl_starts_when_the_send_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [1000.0]
        monkeypatch.setattr(client_mod, "_monotonic", lambda: now[0])
        client, _ = _client()
        await client.send(ACCOUNT, "late echo")
        now[0] += client_mod.ECHO_TTL_S + 1
        echo = parse_receive(_sync("late echo"), ACCOUNT)
        assert echo is not None and client.is_own_echo(echo) is False
        assert client._own_sends == []  # pruned

    @pytest.mark.asyncio
    async def test_a_definitive_rejection_forgets_the_record(self) -> None:
        client, peer = _client()
        peer.fail = RpcError(-32602, "bad params")
        with pytest.raises(RpcError):
            await client.send(ACCOUNT, "never sent")
        assert client._own_sends == []

    @pytest.mark.asyncio
    async def test_an_unresolved_attachment_only_send_still_recognises_its_echo(self) -> None:
        # The send never reported a timestamp, so the record's key is all there
        # is. The re-synced copy carries each attachment's id, type and size but
        # not its bytes, so the key is the type+size shape both sides can see.
        client, peer = _client()
        peer.fail = RpcTransportError("timed out")
        png = b"\x89PNG" + b"\0" * 63  # 67 bytes: exercises base64 padding
        uri = data_uri(png, "image/png", "chart.png")
        with pytest.raises(RpcTransportError):
            await client.send(ACCOUNT, "", attachments=[uri])
        assert client._own_sends[0].body_digest  # never the unmatchable empty key
        params = _sync("")
        sent = params["envelope"]["syncMessage"]["sentMessage"]
        sent["message"] = None
        sent["attachments"] = [
            {"id": "Xq3.png", "contentType": "image/png", "filename": "chart.png", "size": 67}
        ]
        echo = parse_receive(params, ACCOUNT)
        assert echo is not None and echo.kind == KIND_NOTE_TO_SELF
        assert client.is_own_echo(echo) is True
        assert client._own_sends == []

    @pytest.mark.asyncio
    async def test_an_attachment_key_never_matches_a_different_file_or_a_typed_body(
        self,
    ) -> None:
        client, peer = _client()
        peer.fail = RpcTransportError("timed out")
        uri = data_uri(b"abc", "image/png")
        with pytest.raises(RpcTransportError):
            await client.send(ACCOUNT, "", attachments=[uri])
        other = SignalInbound(
            kind=KIND_NOTE_TO_SELF, timestamp=7, attachment_shape=(("image/png", 4),)
        )
        # Someone typing the attachment key's exact text: NUL is stripped from
        # every body, so it cannot spell the key, digest or not.
        typed = SignalInbound(kind=KIND_NOTE_TO_SELF, text=client_mod.outbound_echo_key("", [uri]))
        assert client.is_own_echo(other) is False
        assert client.is_own_echo(typed) is False
        assert len(client._own_sends) == 1

    @pytest.mark.asyncio
    async def test_an_ambiguous_failure_keeps_the_record(self) -> None:
        client, peer = _client()
        peer.fail = RpcTransportError("timed out")
        with pytest.raises(RpcTransportError):
            await client.send(ACCOUNT, "maybe sent")
        assert len(client._own_sends) == 1
        assert client._own_sends[0].expires_at is not None

    @pytest.mark.asyncio
    async def test_the_ledger_keeps_a_fixed_size_digest_and_never_the_body(self) -> None:
        # A long answer and a one-word reply cost the ledger the same, and no
        # message text outlives its send in it.
        client, peer = _client()
        peer.fail = RpcTransportError("timed out")  # unresolved: only the body can match
        long_body = "a long private answer " * 5000
        for body in (long_body, "ok"):
            with pytest.raises(RpcTransportError):
                await client.send(ACCOUNT, body)
        for record in client._own_sends:
            assert set(vars(record)) == {"body_digest", "expires_at", "timestamp"}
            assert isinstance(record.body_digest, bytes) and len(record.body_digest) == 32
        assert "private answer" not in repr(client._own_sends)
        # ... and the digest still matches the echo exactly as the text did.
        echo = SignalInbound(kind=KIND_NOTE_TO_SELF, text=long_body, timestamp=1)
        assert client.is_own_echo(echo) is True
        assert client.is_own_echo(SignalInbound(kind=KIND_NOTE_TO_SELF, text="ok!")) is False
        assert client.is_own_echo(SignalInbound(kind=KIND_NOTE_TO_SELF, text=" ok ")) is True
        assert client._own_sends == []

    def test_a_body_with_a_lone_surrogate_is_remembered_and_matched(self) -> None:
        # A JSON body can carry a lone surrogate, which strict UTF-8 refuses.
        client, _ = _client()
        client._remember_own_send("\ud83d broken")
        echo = SignalInbound(kind=KIND_NOTE_TO_SELF, text="\ud83d broken", timestamp=9)
        assert client.is_own_echo(echo) is True


class TestInboundNotifications:
    @pytest.mark.asyncio
    async def test_a_receive_is_classified_and_handed_on(self) -> None:
        client, _ = _client()
        seen: list[SignalInbound] = []

        async def handler(inbound: SignalInbound) -> None:
            seen.append(inbound)

        client.set_message_handler(handler)
        await client._on_notification("receive", _dm("one"))
        await client._on_notification("other", _dm("ignored"))
        assert [i.text for i in seen] == ["one"]

    @pytest.mark.asyncio
    async def test_a_redelivered_envelope_is_dropped(self) -> None:
        client, _ = _client()
        seen: list[SignalInbound] = []

        async def handler(inbound: SignalInbound) -> None:
            seen.append(inbound)

        client.set_message_handler(handler)
        await client._on_notification("receive", _dm("one"))
        await client._on_notification("receive", _dm("one"))
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_the_account_aci_is_learned_from_its_own_sync(self) -> None:
        client, _ = _client()

        async def ignore(_inbound: SignalInbound) -> None:
            return None

        client.set_message_handler(ignore)
        assert not client.is_self(ACCOUNT_ACI)
        await client._on_notification("receive", _sync(dest_number=OWNER, dest_uuid=OWNER_ACI))
        assert client.is_self(ACCOUNT_ACI)

    @pytest.mark.asyncio
    async def test_a_foreign_sync_teaches_nothing_and_never_passes_as_the_owner(self) -> None:
        client, _ = _client()
        seen: list[SignalInbound] = []

        async def handler(inbound: SignalInbound) -> None:
            seen.append(inbound)

        client.set_message_handler(handler)
        forged = _sync("rm -rf", dest_number=ACCOUNT, dest_uuid=ACCOUNT_ACI)
        forged["envelope"].update(
            source=OWNER, sourceNumber=OWNER, sourceUuid="dddddddd-4444-4444-8444-444444444444"
        )
        await client._on_notification("receive", forged)
        assert client.self_ids() == {ACCOUNT}
        assert [i.kind for i in seen] == [KIND_SYNC_OTHER]

    @pytest.mark.asyncio
    async def test_an_aci_account_learns_its_number_only_from_its_own_sync(self) -> None:
        client = SignalClient(ACCOUNT_ACI)
        client.ready.set()
        foreign = _sync()
        foreign["envelope"].update(sourceNumber=OWNER, sourceUuid=OWNER_ACI)
        await client._on_notification("receive", foreign)
        assert client.self_ids() == {ACCOUNT_ACI}
        await client._on_notification("receive", _sync())
        assert client.self_ids() == {ACCOUNT_ACI, ACCOUNT}

    @pytest.mark.asyncio
    async def test_a_notification_before_the_probe_waits_for_adoption(self) -> None:
        client = SignalClient(ACCOUNT)
        seen: list[SignalInbound] = []

        async def handler(inbound: SignalInbound) -> None:
            seen.append(inbound)

        client.set_message_handler(handler)
        task = asyncio.create_task(client._on_notification("receive", _dm("early")))
        await asyncio.sleep(0)
        assert seen == [] and not task.done()
        client._set_state(True, "")
        await await_until(task, what="the early notification is delivered")
        assert [i.text for i in seen] == ["early"]


class TestTypingAndReceipts:
    @pytest.mark.asyncio
    async def test_typing_names_the_recipient(self) -> None:
        client, peer = _client()
        await client.send_typing(OWNER)
        assert peer.calls == [("sendTyping", {"recipient": [OWNER]})]

    @pytest.mark.asyncio
    async def test_typing_and_receipts_are_skipped_for_note_to_self(self) -> None:
        client, peer = _client()
        await client.send_typing(ACCOUNT)
        await client.mark_read(ACCOUNT, 5)
        assert peer.calls == []

    @pytest.mark.asyncio
    async def test_a_read_receipt_targets_the_message_timestamp(self) -> None:
        client, peer = _client()
        await client.mark_read(OWNER, 1700000000100)
        assert peer.calls == [
            (
                "sendReceipt",
                {"recipient": OWNER, "targetTimestamp": [1700000000100], "type": "read"},
            )
        ]

    @pytest.mark.asyncio
    async def test_a_timeout_does_not_disable_typing_or_receipts(self) -> None:
        client, peer = _client()
        peer.fail = RpcTransportError("sendTyping timed out after 10s")
        await client.send_typing(OWNER)
        await client.mark_read(OWNER, 5)
        assert client.typing_supported and client.read_supported
        peer.fail = None
        await client.send_typing(OWNER)
        assert peer.calls[-1] == ("sendTyping", {"recipient": [OWNER]})

    @pytest.mark.asyncio
    async def test_a_rejection_disables_the_feature_for_the_process(self) -> None:
        client, peer = _client()
        peer.fail = RpcError(-32601, "nope")
        await client.send_typing(OWNER)
        await client.send_typing(OWNER)
        assert len(peer.calls) == 1
        assert client.typing_supported is False


class TestStartFailure:
    @pytest.mark.asyncio
    async def test_no_binary_is_reported_with_where_it_looked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_mod, "resolve_bridge_path", lambda: "")
        client = SignalClient(ACCOUNT)
        with pytest.raises(RpcTransportError, match="signal-cli is not installed"):
            await client.start()

    @pytest.mark.asyncio
    async def test_no_account_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client_mod, "resolve_bridge_path", lambda: "/x/signal-cli")
        with pytest.raises(RpcTransportError, match="signal.account"):
            await SignalClient("").start()

    def test_the_argv_has_no_output_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client_mod, "resolve_bridge_path", lambda: "/x/signal-cli")
        assert SignalClient(ACCOUNT)._argv() == ["/x/signal-cli", "-a", ACCOUNT, "jsonRpc"]
        assert SignalClient(ACCOUNT, data_dir="/d")._argv() == [
            "/x/signal-cli",
            "--data-dir",
            "/d",
            "-a",
            ACCOUNT,
            "jsonRpc",
        ]
