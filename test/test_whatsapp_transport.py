"""WhatsApp transport tests: the inbound gauntlet + authorize + targets.

Events are plain namespace fakes shaped like neonize's protobuf messages —
the transport reads them via getattr, so no optional dependency is needed.
The matrix covers the five gauntlet stages in order (shape, replay flood,
echo, group gate, authorize) because ordering IS the contract: e.g. an
unconfigured group must be dropped before it can produce an audit row.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.whatsapp.jids import OwnIdentity
from kiro_crew.whatsapp.transport import WhatsAppTransport

OWN_JID = "447700900000@s.whatsapp.net"
OWN_LID = "111222333@lid"
FRIEND = "447700900111@s.whatsapp.net"
GROUP = "120363000000000001@g.us"


class FakeClient:
    """The slice of WhatsAppClient the transport touches."""

    def __init__(self) -> None:
        self.me = OwnIdentity(jid=OWN_JID, lid=OWN_LID)
        self.connected_at: float | None = 1_000_000.0
        self.is_connected = True
        self.on_message = None
        self.sent: list[tuple[str, str]] = []
        self._next_ids = iter(f"SENT{i}" for i in range(1, 100))

    async def send_text(self, jid: str, text: str) -> list[str]:
        message_id = next(self._next_ids)
        self.sent.append((jid, text))
        return [message_id]


def jid_ns(value: str) -> SimpleNamespace:
    user, _, server = value.partition("@")
    return SimpleNamespace(User=user, Server=server)


def event(
    *,
    chat: str,
    sender: str,
    text: str = "hello",
    from_me: bool = False,
    is_group: bool = False,
    message_id: str = "MSG1",
    timestamp: float = 1_000_500.0,
    mentions: list[str] | None = None,
    quoted_participant: str = "",
    quoted_stanza: str = "",
) -> SimpleNamespace:
    if mentions or quoted_participant or quoted_stanza:
        content = SimpleNamespace(
            conversation="",
            extendedTextMessage=SimpleNamespace(
                text=text,
                contextInfo=SimpleNamespace(
                    mentionedJID=mentions or [],
                    participant=quoted_participant,
                    stanzaID=quoted_stanza,
                ),
            ),
        )
    else:
        content = SimpleNamespace(conversation=text, extendedTextMessage=None)
    return SimpleNamespace(
        Info=SimpleNamespace(
            ID=message_id,
            Timestamp=timestamp,
            MessageSource=SimpleNamespace(
                Chat=jid_ns(chat),
                Sender=jid_ns(sender),
                SenderAlt=None,
                IsFromMe=from_me,
                IsGroup=is_group,
            ),
        ),
        Message=content,
    )


class Harness:
    def __init__(self, **transport_kwargs) -> None:
        self.client = FakeClient()
        self.dispatched = []

        async def dispatch(msg):
            self.dispatched.append(msg)

        self.transport = WhatsAppTransport(self.client, dispatch, **transport_kwargs)


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.mark.asyncio
class TestGauntlet:
    async def test_own_typed_self_chat_message_dispatches(self, harness):
        await harness.transport.receive(
            event(chat=OWN_JID, sender=OWN_JID, from_me=True, text="/status please")
        )
        assert len(harness.dispatched) == 1
        msg = harness.dispatched[0]
        assert msg.user_id == "447700900000"
        assert msg.conversation_id == OWN_JID
        assert msg.channel_type == "whatsapp"

    async def test_own_echo_is_dropped(self, harness):
        ids = await harness.transport._send_tracked(OWN_JID, "agent reply")
        await harness.transport.receive(
            event(chat=OWN_JID, sender=OWN_JID, from_me=True, message_id=ids[0])
        )
        assert harness.dispatched == []

    async def test_untracked_from_me_after_echo_still_dispatches(self, harness):
        await harness.transport._send_tracked(OWN_JID, "agent reply")
        await harness.transport.receive(
            event(chat=OWN_JID, sender=OWN_JID, from_me=True, message_id="TYPED1")
        )
        assert len(harness.dispatched) == 1

    async def test_replayed_history_is_dropped(self, harness):
        harness.client.connected_at = 1_000_000.0
        await harness.transport.receive(
            event(chat=OWN_JID, sender=OWN_JID, from_me=True, timestamp=999_000.0)
        )
        assert harness.dispatched == []

    async def test_recent_message_within_grace_passes(self, harness):
        harness.client.connected_at = 1_000_000.0
        await harness.transport.receive(
            event(chat=OWN_JID, sender=OWN_JID, from_me=True, timestamp=999_970.0)
        )
        assert len(harness.dispatched) == 1

    async def test_empty_text_and_malformed_events_drop(self, harness):
        await harness.transport.receive(event(chat=OWN_JID, sender=OWN_JID, text="  "))
        await harness.transport.receive(SimpleNamespace(Info=None, Message=None))
        assert harness.dispatched == []


@pytest.mark.asyncio
class TestDMPolicy:
    async def test_self_policy_denies_friends(self, harness):
        await harness.transport.receive(event(chat=FRIEND, sender=FRIEND))
        assert harness.dispatched == []

    async def test_allowlist_admits_listed_number(self):
        h = Harness(dm_policy="allowlist", allowed_wa_ids=["447700900111"])
        await h.transport.receive(event(chat=FRIEND, sender=FRIEND))
        assert len(h.dispatched) == 1

    async def test_allowlist_still_denies_unlisted(self):
        h = Harness(dm_policy="allowlist", allowed_wa_ids=["447700900222"])
        await h.transport.receive(event(chat=FRIEND, sender=FRIEND))
        assert h.dispatched == []

    async def test_unknown_policy_fails_closed_even_for_self(self):
        h = Harness(dm_policy="everyone")
        await h.transport.receive(event(chat=OWN_JID, sender=OWN_JID, from_me=True))
        assert h.dispatched == []

    async def test_disabled_denies_all(self):
        h = Harness(dm_policy="disabled")
        await h.transport.receive(event(chat=OWN_JID, sender=OWN_JID, from_me=True))
        assert h.dispatched == []


@pytest.mark.asyncio
class TestGroups:
    def cfg(self, mode="mention", rules=""):
        return [{"jid": GROUP, "name": "G", "mode": mode, "rules": rules, "cooldown_s": 0}]

    async def test_unconfigured_group_is_invisible(self, harness):
        await harness.transport.receive(
            event(chat=GROUP, sender=FRIEND, is_group=True, mentions=[OWN_JID])
        )
        assert harness.dispatched == []

    async def test_mention_of_own_jid_dispatches(self):
        h = Harness(groups=self.cfg())
        await h.transport.receive(
            event(chat=GROUP, sender=FRIEND, is_group=True, mentions=[OWN_JID])
        )
        assert len(h.dispatched) == 1
        assert h.dispatched[0].is_mention

    async def test_mention_of_lid_alias_dispatches(self):
        h = Harness(groups=self.cfg())
        await h.transport.receive(
            event(chat=GROUP, sender=FRIEND, is_group=True, mentions=[OWN_LID])
        )
        assert len(h.dispatched) == 1

    async def test_unaddressed_group_message_is_dropped_in_mention_mode(self):
        h = Harness(groups=self.cfg())
        await h.transport.receive(event(chat=GROUP, sender=FRIEND, is_group=True))
        assert h.dispatched == []

    async def test_reply_to_agent_message_counts_as_addressed(self):
        h = Harness(groups=self.cfg())
        ids = await h.transport._send_tracked(GROUP, "agent said this")
        await h.transport.receive(
            event(chat=GROUP, sender=FRIEND, is_group=True, quoted_stanza=ids[0])
        )
        assert len(h.dispatched) == 1

    async def test_rules_mode_unprompted_dispatches_with_verdict(self):
        h = Harness(groups=self.cfg(mode="rules", rules="Answer python questions."))
        await h.transport.receive(
            event(chat=GROUP, sender=FRIEND, is_group=True, text="how do dicts work?")
        )
        assert len(h.dispatched) == 1
        verdict = h.transport.pending_verdicts.get(id(h.dispatched[0]))
        assert verdict is None  # popped after dispatch completes

    async def test_non_operator_group_command_dies_silently(self):
        h = Harness(groups=self.cfg())
        await h.transport.receive(
            event(chat=GROUP, sender=FRIEND, is_group=True, text="/new", mentions=[OWN_JID])
        )
        assert h.dispatched == []

    async def test_operator_group_command_dispatches(self):
        h = Harness(groups=self.cfg())
        await h.transport.receive(
            event(chat=GROUP, sender=OWN_JID, from_me=True, is_group=True, text="/new")
        )
        assert len(h.dispatched) == 1


@pytest.mark.asyncio
class TestOutboundAndTargets:
    async def test_send_message_tracks_every_chunk_id(self, harness):
        message_id = await harness.transport.send_message(FRIEND, "hi there")
        assert message_id == "SENT1"
        assert harness.transport.echo.is_own_echo(FRIEND, "SENT1")

    async def test_configured_targets_offline_reason(self, harness):
        harness.client.is_connected = False
        targets = harness.transport.configured_targets()
        assert targets and not targets[0].available
        assert "pair" in targets[0].unavailable_reason.lower()

    async def test_resolve_configured_target_roundtrip(self, harness):
        targets = harness.transport.configured_targets()
        resolved = await harness.transport.resolve_configured_target(targets[0].target_id)
        assert resolved == (OWN_JID, None)

    async def test_capabilities_are_honest(self, harness):
        caps = harness.transport.capabilities
        assert not caps.streaming and not caps.edit and caps.max_buttons == 0
        assert caps.max_message_chars == 4096
        assert caps.supports_proactive_send
