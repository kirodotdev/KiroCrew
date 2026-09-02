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

from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.whatsapp.jids import OwnIdentity
from kiro_crew.whatsapp.transport import WhatsAppTransport

OWN_JID = "447700900000@s.whatsapp.net"
OWN_LID = "111222333@lid"
FRIEND = "447700900111@s.whatsapp.net"
FRIEND_LID = "999888777@lid"
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
        #: ``@lid`` alias -> phone JID, as the real resolver would answer.
        self.lid_map: dict[str, str] = {}
        #: every alias lookup, so a test can assert the result is cached.
        self.lid_lookups: list[str] = []

    async def send_text(self, jid: str, text: str, on_sent=None) -> list[str]:
        """Chunks on blank lines, and invokes ``on_sent`` per chunk.

        Calling ``on_sent`` is the faithful part: the real client hands each id
        over the moment its send lands, and the echo tracker depends on that
        ordering. A fake that only returned the list would let the echo tests
        pass against a client that never called back.
        """
        ids: list[str] = []
        for chunk in [c for c in text.split("\n\n") if c] or [text]:
            message_id = next(self._next_ids)
            self.sent.append((jid, chunk))
            ids.append(message_id)
            if on_sent is not None:
                on_sent(message_id)
        return ids

    async def phone_for_lid(self, lid_jid: str) -> str:
        self.lid_lookups.append(lid_jid)
        return self.lid_map.get(lid_jid, "")


class Msg(SimpleNamespace):
    """A message fake that answers ``HasField`` the way a protobuf does.

    The inbound path probes presence with ``HasField`` because a protobuf's
    singular submessage is never None: reading an absent one returns a default
    instance, so truthiness would report every field present. A fake without
    ``HasField`` would send that path down its non-protobuf degradation branch and
    the tests would exercise a shape production never sees.
    """

    def HasField(self, name: str) -> bool:  # noqa: N802: protobuf's own spelling
        if not hasattr(self, name):
            raise ValueError(name)  # what a protobuf raises for an unknown field
        return getattr(self, name) not in (None, "", 0)


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
    image: bool = False,
) -> SimpleNamespace:
    if image:
        # `describe` probes with HasField, so an absent sibling must be falsy.
        content = Msg(
            conversation="",
            extendedTextMessage=None,
            imageMessage=Msg(caption=text, mimetype="image/jpeg", fileLength=1024),
        )
    elif mentions or quoted_participant or quoted_stanza:
        content = Msg(
            conversation="",
            extendedTextMessage=Msg(
                text=text,
                contextInfo=SimpleNamespace(
                    mentionedJID=mentions or [],
                    participant=quoted_participant,
                    stanzaID=quoted_stanza,
                ),
            ),
        )
    else:
        content = Msg(conversation=text, extendedTextMessage=None)
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

    async def test_an_unlisted_number_is_not_a_resolvable_target(self):
        """The dashboard mirror-link path offers what `configured_targets` lists
        and then round-trips the chosen id back through the resolver, which is the
        ONLY allowlist check on that path. Resolving an id the list never offered
        would let a proactive send open a conversation with an arbitrary number.
        """
        h = Harness(dm_policy="allowlist", allowed_wa_ids=["447700900111"])
        offered = {t.target_id for t in h.transport.configured_targets()}
        assert "user:447700900999" not in offered

        assert await h.transport.resolve_configured_target("user:447700900999") is None
        # Every id the list DOES offer still resolves.
        for target_id in offered:
            assert await h.transport.resolve_configured_target(target_id) is not None

    async def test_capabilities_are_honest(self, harness):
        """These flags are a claim other code trusts, so each one must match a
        behaviour the channel actually has.
        """
        caps = harness.transport.capabilities
        # The Web protocol exposes an edit, so the renderer streams by editing
        # one bubble; reactions are used for receipts.
        assert caps.streaming and caps.edit and caps.reactions
        # No interactive widget exists for a personal account: the send succeeds
        # and renders as nothing, so the trailer degrades to a numbered list.
        assert caps.max_buttons == 0
        assert caps.max_message_chars == 4096
        # No 24-hour customer-service window on this protocol, unlike the Cloud API.
        assert caps.supports_proactive_send
        # Inbound routing derives its key from the chat, never a mirror binding.
        assert not caps.supports_session_resume
        # Both media directions ship: ingest through whatsapp/attachments.py and
        # upload through whatsapp/files.py. Asserted because a flag other code
        # trusts is exactly what goes stale first.
        assert caps.files_inbound and caps.files_outbound


@pytest.mark.asyncio
class TestGovernanceDenyStopsTheDownload:
    """A `channels` deny must stop the FETCH, not just the reply.

    The dispatcher's own gate runs after `receive()` has already ingested media,
    so a gate only there leaves a denied channel performing an authenticated
    download onto the operator's host on every media message.
    """

    async def _deny(self, monkeypatch):
        import kiro_crew.messaging.dispatch as mod

        async def deny(_ct):
            return False

        # Patched on `dispatch`, not on `identity`: the name is bound at import,
        # so patching the defining module leaves the bound reference in place and
        # the test would pass against an ungated fetch.
        monkeypatch.setattr(mod, "channel_inbound_permitted", deny)

    async def test_a_denied_channel_never_reaches_the_fetch(self, harness, monkeypatch):
        import kiro_crew.whatsapp.transport as mod

        calls = []

        async def spy(*a, **kw):
            calls.append(a)
            raise AssertionError("a denied channel must not download media")

        monkeypatch.setattr(mod, "ingest_media", spy)
        await self._deny(monkeypatch)
        await harness.transport.receive(
            event(chat=OWN_JID, sender=OWN_JID, from_me=True, text="hi", image=True)
        )
        assert calls == []
        assert harness.dispatched == []

    async def test_a_denied_channel_still_admits_a_bare_cancel(self, harness, monkeypatch):
        """The documented exemption: a runaway turn must remain stoppable."""
        await self._deny(monkeypatch)
        await harness.transport.receive(
            event(chat=OWN_JID, sender=OWN_JID, from_me=True, text="/stop")
        )
        assert len(harness.dispatched) == 1

    async def test_media_captioned_as_a_cancel_is_still_gated(self, harness, monkeypatch):
        """The exemption reads the ENVELOPE, not the caption.

        `msg.attachments` is empty at the point the fetch is decided, so an
        exemption keyed on it would admit a policy-denied media message whose
        caption happens to be `/stop`, and the download would already have run.
        """
        import kiro_crew.whatsapp.transport as mod

        async def spy(*a, **kw):
            raise AssertionError("a denied channel must not download media")

        monkeypatch.setattr(mod, "ingest_media", spy)
        await self._deny(monkeypatch)
        await harness.transport.receive(
            event(chat=OWN_JID, sender=OWN_JID, from_me=True, text="/stop", image=True)
        )
        assert harness.dispatched == []


@pytest.mark.asyncio
class TestGroupMediaNeedsIndividualAdmission:
    """Being admitted to the conversation is not being admitted to the machine.

    Step 5 authorizes the group SURFACE, so a configured group never reaches
    ``authorize`` and membership alone would let any member trigger an
    authenticated whole-blob download into the gateway's heap. In ``rules`` mode an
    unaddressed message already answers ``respond=True``, and the per-group
    cooldown does not bound the fetch because it only starts once a reply actually
    delivered, which a sentinel-silenced turn never does.
    """

    def _msg(self, user_id: str):
        return InboundMessage(
            channel_type="whatsapp", user_id=user_id, conversation_id=GROUP, text="x"
        )

    async def test_a_dm_sender_is_unaffected(self, harness):
        """A DM has already passed `authorize`, so the DM policy is the answer."""
        assert harness.transport._may_fetch_media(self._msg("447700900111"), is_group=False)

    async def test_an_unlisted_group_member_may_not_trigger_a_fetch(self):
        h = Harness(dm_policy="allowlist", allowed_wa_ids=["447700900111"])
        assert not h.transport._may_fetch_media(self._msg("447700900999"), is_group=True)

    async def test_an_allowed_group_member_may(self):
        h = Harness(dm_policy="allowlist", allowed_wa_ids=["447700900111"])
        assert h.transport._may_fetch_media(self._msg("447700900111"), is_group=True)

    async def test_the_account_owner_may(self, harness):
        assert harness.transport._may_fetch_media(self._msg("447700900000"), is_group=True)

    async def test_dm_policy_open_does_not_hand_the_capability_back(self):
        """`open` resolves to "anyone with a user id", so consulting `authorize`
        here would re-admit every group member. The gate is individual admission,
        deliberately independent of `dm_policy`.
        """
        h = Harness(dm_policy="open")
        assert not h.transport._may_fetch_media(self._msg("447700900999"), is_group=True)
        # ...while the same sender is still free to CHAT under that policy.
        assert h.transport.authorize(self._msg("447700900999"))


@pytest.mark.asyncio
class TestLinkedIdentityAliases:
    """WhatsApp multi-device addresses a sender either by their phone number or
    by their Linked Identity (``<id>@lid``), and the two user parts are
    UNRELATED strings. The DM allowlist is written in phone numbers, so without
    folding the alias an allow-listed person is silently ignored -- it fails
    closed, which is safe, but reads to the operator as a broken channel.
    """

    async def test_an_allowlisted_peer_addressed_by_lid_is_authorized(self):
        h = Harness(dm_policy="allowlist", allowed_wa_ids=["447700900111"])
        h.client.lid_map[FRIEND_LID] = FRIEND
        await h.transport.receive(event(chat=FRIEND_LID, sender=FRIEND_LID, text="hi"))
        assert len(h.dispatched) == 1
        # Attributed to the phone number, so config, audit and the allowlist all
        # name the same human.
        assert h.dispatched[0].user_id == "447700900111"

    async def test_the_same_peer_by_phone_needs_no_lookup(self):
        h = Harness(dm_policy="allowlist", allowed_wa_ids=["447700900111"])
        await h.transport.receive(event(chat=FRIEND, sender=FRIEND, text="hi"))
        assert len(h.dispatched) == 1
        assert h.client.lid_lookups == []

    async def test_an_unresolvable_lid_still_fails_closed(self):
        """A resolver outage must not open the door: no mapping means the alias
        is kept, so it does not match the phone-number allowlist and is denied.
        """
        h = Harness(dm_policy="allowlist", allowed_wa_ids=["447700900111"])
        await h.transport.receive(event(chat=FRIEND_LID, sender=FRIEND_LID, text="hi"))
        assert h.dispatched == []

    async def test_a_stranger_by_lid_is_denied_even_when_resolvable(self):
        """Resolution is identity folding, not authorization."""
        h = Harness(dm_policy="allowlist", allowed_wa_ids=["447700900111"])
        h.client.lid_map["555@lid"] = "447700900222@s.whatsapp.net"
        await h.transport.receive(event(chat="555@lid", sender="555@lid", text="hi"))
        assert h.dispatched == []

    async def test_the_alias_is_resolved_once_per_sender(self):
        h = Harness(dm_policy="allowlist", allowed_wa_ids=["447700900111"])
        h.client.lid_map[FRIEND_LID] = FRIEND
        for i in range(3):
            await h.transport.receive(
                event(chat=FRIEND_LID, sender=FRIEND_LID, text="hi", message_id=f"M{i}")
            )
        assert len(h.dispatched) == 3
        assert h.client.lid_lookups == [FRIEND_LID]


@pytest.mark.asyncio
class TestOwnOutgoingMessages:
    """``from_me`` means the ACCOUNT sent it, which includes the operator texting
    an ordinary contact from their phone. Treating that as a command puts the
    agent into their private conversations and replies in the contact's chat.
    """

    async def test_texting_a_contact_is_not_a_command(self, harness):
        await harness.transport.receive(
            event(chat=FRIEND, sender=OWN_JID, from_me=True, text="see you at 8")
        )
        assert harness.dispatched == [], "the agent answered a private conversation"

    async def test_the_self_chat_is_still_the_command_surface(self, harness):
        await harness.transport.receive(
            event(chat=OWN_JID, sender=OWN_JID, from_me=True, text="/status")
        )
        assert len(harness.dispatched) == 1

    async def test_the_operator_can_still_address_a_configured_group(self):
        h = Harness(
            groups=[{"jid": GROUP, "name": "G", "mode": "mention", "rules": "", "cooldown_s": 0}]
        )
        await h.transport.receive(
            event(chat=GROUP, sender=OWN_JID, from_me=True, is_group=True, text="what is next?")
        )
        assert len(h.dispatched) == 1
