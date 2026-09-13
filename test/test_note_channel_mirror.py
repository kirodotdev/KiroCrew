"""Tests for ``/note`` channel delivery (``chat_note_mirror``).

The context half of a note is surface-agnostic (``drain_pending_context`` runs in
``_run_chat``, which every inbound surface shares) while the visible half went to
the dashboard only. These pin the leg that closes that gap: the note's visible
line reaches the Slack thread or the non-Slack channel a session is bound to, and
is a silent no-op for a dashboard-only session, a paused mirror, an ungoverned
egress, a non-proactive transport and an unresolvable link.

No live channel is required: the Slack leg takes a stub client and the neutral
leg takes a fake ``MessagingTransport`` through the real send ladder, so the
nine transports that cannot be reached from a corporate network are still
covered by the same assertions as Slack.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from test_chat_mirror import _caps_transport, _real_caps_transport

from kiro_crew.dashboard.chat import api_chat_slot_note
from kiro_crew.dashboard.chat_note_mirror import (
    _note_channel_text,
    _snapshot_slack_link,
    mirror_note_to_channels,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.slack.format import SLACK_MSG_LIMIT


def _slack_state(tmp_path, *, thread="1785370133.085469", channel="C123"):
    """A state whose session is bound to a Slack thread, with a stub client."""
    state = _make_state(tmp_path)
    state.slack_client = SimpleNamespace(post_message=AsyncMock(return_value="ts-1"))
    state.sessions.get_slack_link = MagicMock(return_value=(thread, channel))
    return state


def _authorize_slack_recipient(monkeypatch, *channels: str) -> None:
    """Put *channels* on the REAL tracked-channel roster for one test.

    Patches the roster the shipped ``is_tracked_channel`` reads rather than
    stubbing the predicate, so these tests exercise the real authorization
    decision. ``monkeypatch`` restores the global, which matters because the set
    is process-wide and a leak would silently authorize later tests.
    """
    monkeypatch.setattr("kiro_crew.slack.handler._tracking_channels", set(channels))


def _slot(state, name="s1", *, thread="", channel=""):
    slot = _ChatSlot(name)
    slot._slack_thread_ts = thread
    slot._slack_channel = channel
    slot._slack_linked = bool(thread and channel)
    state._slots[name] = slot
    return slot


async def _mirror(state, slot, session_key, content, source="note") -> list[str]:
    """Run the mirror and report which channels CONFIRMED a delivery.

    ``mirror_note_to_channels`` returns nothing -- the report had no production
    consumer and is gone. But "confirmed delivery" is a real behavioural property,
    not a report: an unconfirmed send, a mid-send revocation and a stalled leg all
    turn on it, and production learns it from the log rather than from a return
    value. So these tests observe each LEG's own outcome, which is the same thing
    the deleted list was built from -- preserving the old assertions exactly, with
    no production surface kept alive to serve them.

    The transport leg answers a BOOL, so which channel the ladder selected is read
    from the log record ``deliver_to_channel`` emits -- deliberately the same
    surface production reads it from. Asserting on the leg's return instead would
    have meant widening that return to carry a value only these tests consume,
    which is the shape that was just subtracted.

    Deliberately NOT measured by "was ``send_message`` awaited": that counts
    ATTEMPTS, and a leg that sent a chunk and then refused the rest is precisely
    the case these tests distinguish.
    """
    import logging as _logging

    import kiro_crew.dashboard.chat_note_mirror as mod

    picked: list[str] = []

    class _PickedType(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            if record.msg == "channel send: delivered to %s for %s" and record.args:
                picked.append(str(record.args[0]))

    _msg_logger = _logging.getLogger("kiro_crew.dashboard.handlers.messaging")
    _handler = _PickedType()
    _msg_logger.addHandler(_handler)
    _prior_level = _msg_logger.level
    _msg_logger.setLevel(_logging.INFO)

    # The legs return NOTHING, so delivery is derived from what each leg OBSERVABLY
    # did rather than from a value it handed back. Both legs now log their own
    # confirmed delivery, so both are read the same way:
    #   slack     -- "slack egress: delivered to %s for %s"
    #   transport -- "channel send: delivered to %s for %s"
    # A client call is NOT the signal: post_message is awaited even when the post
    # raises, returns no ts, or is revoked between chunks, none of which delivered.
    slack_ok: list[str] = []

    class _SlackOk(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            if record.msg == "slack egress: delivered to %s for %s":
                slack_ok.append("slack")

    _slack_logger = _logging.getLogger("kiro_crew.dashboard.slack_egress")
    _slack_handler = _SlackOk()
    _slack_logger.addHandler(_slack_handler)
    _slack_prior = _slack_logger.level
    _slack_logger.setLevel(_logging.INFO)

    legs_run: list[str] = []
    real_run_leg = mod._run_leg

    async def _recording_run_leg(leg, key, coro, **kwargs):
        # **kwargs forwarded rather than named, so the double stays a pass-through
        # and a future bound added to the real signature does not silently bypass it.
        await real_run_leg(leg, key, coro, **kwargs)
        legs_run.append(leg)

    mod._run_leg = _recording_run_leg  # type: ignore[assignment]
    try:
        # Snapshot the way the handler does, on this side of the dispatch boundary.
        from kiro_crew.dashboard.handlers.messaging import snapshot_channel_link

        await mirror_note_to_channels(
            state,
            slot,
            session_key,
            content,
            source,
            slack_link=_snapshot_slack_link(slot, state, session_key),
            channel_link=snapshot_channel_link(state, session_key, skip_paused=True),
        )
    finally:
        mod._run_leg = real_run_leg  # type: ignore[assignment]
        _msg_logger.removeHandler(_handler)
        _msg_logger.setLevel(_prior_level)
        _slack_logger.removeHandler(_slack_handler)
        _slack_logger.setLevel(_slack_prior)

    # Slack first: the legs run in that order and callers assert on the order.
    return slack_ok[:1] + picked


class TestNoteText:
    def test_default_source_is_not_echoed_as_a_label(self) -> None:
        """Every sourceless caller shares source="note"; "[note · note]" is noise."""
        assert _note_channel_text("hello", "note") == "📝 [note]\nhello\n— end of note —"

    def test_named_source_is_shown(self) -> None:
        assert (
            _note_channel_text("hello", "board-sync")
            == "📝 [note · board-sync]\nhello\n— end of note —"
        )

    def test_a_partial_delivery_is_distinguishable_from_a_whole_note(self) -> None:
        """A delivered PREFIX must not read as the whole note.

        The send is bounded, and chunking happens inside the shared governed sends, so
        a deadline firing mid-sequence leaves early chunks posted and later ones never
        sent. Without a terminator the last surviving chunk reads as the end of the
        note, and a reader cannot tell a truncated delivery from a short one -- silent
        partial data. The terminator is applied at render time, BEFORE the coroutine
        reaches its deadline, so the chunk carrying it is by construction the final one
        and its absence is the signal.
        """
        rendered = _note_channel_text("alpha\nbeta\ngamma", "note")

        assert rendered.endswith("— end of note —"), (
            "the rendered note carries no terminator, so any delivered prefix reads as "
            f"a complete note; got {rendered!r}"
        )
        # What a reader sees when the deadline fires after the first chunk.
        prefix = rendered.split("\n")[0]
        assert "— end of note —" not in prefix, (
            "the terminator survived into the first chunk, so a truncated delivery "
            "would still look complete and the marker would prove nothing"
        )


class TestSlackLeg:
    @pytest.mark.asyncio
    async def test_delivers_to_the_linked_thread(self, tmp_path, monkeypatch) -> None:
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        delivered = await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert delivered == ["slack"]
        state.slack_client.post_message.assert_awaited_once()
        args = state.slack_client.post_message.await_args.args
        assert args[0] == "C123"
        assert "hi" in args[1]
        assert args[2] == "1785370133.085469"

    @pytest.mark.asyncio
    async def test_resolves_the_link_from_the_session_map_when_the_slot_is_bare(
        self, tmp_path, monkeypatch
    ) -> None:
        """A channel-born session surfaced into a slot may not have hydrated attrs."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state)  # no slot-level link

        delivered = await _mirror(state, slot, "slack:1785370133.085469", "x", "s")

        assert delivered == ["slack"]

    @pytest.mark.asyncio
    async def test_no_client_is_a_silent_noop(self, tmp_path) -> None:
        state = _slack_state(tmp_path)
        state.slack_client = None
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []

    @pytest.mark.asyncio
    async def test_paused_mirror_suppresses_delivery(self, tmp_path, monkeypatch) -> None:
        """Disconnect means "not into this conversation" — the note is not pushed."""
        state = _slack_state(tmp_path)
        slot = _slot(state, thread="1785370133.085469", channel="C123")
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_note_mirror.slack_mirror_is_paused",
            lambda *a, **k: True,
        )

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_foreign_namespaced_channel_id_is_refused(self, tmp_path) -> None:
        """A legacy ``discord:...`` id must never be posted at the Slack client."""
        state = _slack_state(tmp_path, channel="discord:999")
        slot = _slot(state)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_session_with_no_slack_thread_is_refused_silently(
        self, tmp_path, monkeypatch
    ) -> None:
        """A session with no Slack THREAD is refused and files no denial row.

        The snapshot returns ("", "") unless BOTH coordinates are present, so this
        branch cannot tell a never-bound session from a channel without a thread. A row
        here therefore carried no per-session fact while filing one denial per note for
        every threadless session, which is the flood the missing-client branch already
        exempts. What still matters is that nothing is DELIVERED, which is asserted.
        """
        rows: list[str] = []

        class _Sel:
            def log_api_access(self, **kw):
                rows.append(f"{kw.get('outcome', '')} {kw.get('resources', '')}")

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())

        # Channel bound, thread absent: a note has no thread to mirror into.
        state = _slack_state(tmp_path, thread="", channel="C123")
        slot = _slot(state, thread="", channel="C123")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()

        denials = [r for r in rows if r.startswith("denied")]
        assert denials == [], (
            "a threadless session must not file a per-note denial row; the snapshot "
            f"cannot distinguish it from a never-bound one. Got {rows}"
        )

    @pytest.mark.asyncio
    async def test_a_slackless_install_writes_no_denial_row_per_note(
        self, tmp_path, monkeypatch
    ) -> None:
        """The DEPLOYMENT-wide case: no Slack configured at all -> no row, ever.

        On a Telegram/Discord-only install `_snapshot_slack_link` finds neither slot
        attributes nor a persisted row, so it returns ("", "") for EVERY session. The
        sibling test above covers a session that genuinely lacks a thread while Slack
        IS configured, and that row is worth writing. This one is the case where the
        same branch would fire on every note in the deployment and carry no
        per-session information -- a denial flood that buries the real refusals it
        sits beside, which is the opposite of what the audit stream is for.

        `_deliver_slack_governed` already exempts a missing client, but this leg
        returns before ever calling it, so the exemption has to be made in
        `_deliver_slack` or it does not reach the note leg at all.
        """
        rows: list[str] = []

        class _Sel:
            def log_api_access(self, **kw):
                rows.append(f"{kw.get('outcome', '')} {kw.get('resources', '')}")

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())

        # A Slack-less deployment: no client, and no link to be found for anyone.
        state = _slack_state(tmp_path, thread="", channel="")
        state.slack_client = None
        slot = _slot(state, thread="", channel="")

        # Three notes, because the defect is per-NOTE: one row would already be a
        # flood at scale, and asserting on a single call could pass on an off-by-one.
        for _ in range(3):
            assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []

        denials = [r for r in rows if r.startswith("denied")]
        assert denials == [], (
            "a Slack-less deployment wrote a per-note SEL denial row; at any real "
            "note volume this buries the per-session refusals an operator is "
            f"actually filtering for (got {len(denials)} row(s): {denials})"
        )

    @pytest.mark.asyncio
    async def test_delivery_failure_is_swallowed(self, tmp_path, monkeypatch) -> None:
        """The transcript line is the note's contract; a channel outage is not fatal."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        state.slack_client.post_message = AsyncMock(side_effect=RuntimeError("slack down"))
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []

    @pytest.mark.asyncio
    async def test_degraded_governance_denies_egress(self, tmp_path, monkeypatch) -> None:
        """Slack bypasses the ladder, so its own gate must fail CLOSED."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.resolve_active_scope",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("profile store down")),
        )
        state = _slack_state(tmp_path)
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()


class TestBroadcastMentionDefang:
    """A caller-controlled note must not mass-notify a channel.

    The note body is written by a cron or an app, and Slack PARSES `<!channel>` /
    `<!everyone>` while Telegram and Discord parse `@everyone`. An unescaped one
    turns a background note into a notification for everyone in the conversation.
    These are the negative control for that defang: each asserts the raw grammar
    is ABSENT from what reached the wire, so removing the defang fails them.
    """

    @pytest.mark.asyncio
    async def test_slack_broadcast_grammar_is_defanged(self, tmp_path, monkeypatch) -> None:
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        await _mirror(
            state, slot, "dashboard:chat-1", "deploy done <!channel> please check", "note"
        )

        sent = state.slack_client.post_message.await_args.args[1]
        assert "<!channel>" not in sent, "raw Slack broadcast grammar reached the thread"
        assert "<\u200b!channel>" in sent

    @pytest.mark.asyncio
    async def test_at_mention_is_defanged_for_slack(self, tmp_path, monkeypatch) -> None:
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        await _mirror(state, slot, "dashboard:chat-1", "ping @everyone now", "note")

        sent = state.slack_client.post_message.await_args.args[1]
        assert "@everyone" not in sent
        assert "@\u200beveryone" in sent

    @pytest.mark.asyncio
    async def test_transport_leg_defangs_via_the_shared_sink(self, tmp_path) -> None:
        """The shared send applies `display_safe_for`, so the raw grammar cannot land."""
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )
        slot = _slot(state)

        delivered = await _mirror(state, slot, "dashboard:chat-1", "ping @everyone now", "note")

        assert delivered == ["telegram"]
        sent = tp.send_message.await_args.args[1]
        assert "@everyone" not in sent


class TestSlackChunking:
    @pytest.mark.asyncio
    async def test_a_long_note_is_split_rather_than_truncated(self, tmp_path, monkeypatch) -> None:
        """Slack truncates past its limit and still answers with a ts, so an
        unchunked long note (a diff or log tail) would be silently cut short on
        Slack while a transport session received it whole."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")
        body = "x" * (SLACK_MSG_LIMIT * 2 + 500)

        delivered = await _mirror(state, slot, "dashboard:chat-1", body, "note")

        assert delivered == ["slack"]
        calls = state.slack_client.post_message.await_args_list
        assert len(calls) > 1, "a note longer than the Slack limit was posted in one message"
        for c in calls:
            assert len(c.args[1]) <= SLACK_MSG_LIMIT

    @pytest.mark.asyncio
    async def test_an_unconfirmed_slack_post_is_not_reported_as_delivered(
        self, tmp_path, monkeypatch
    ) -> None:
        """An empty ts is Slack's refusal shape."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        state.slack_client.post_message = AsyncMock(return_value="")
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []


class TestTransportLeg:
    @pytest.mark.asyncio
    async def test_delivers_through_the_shared_governed_send(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )
        slot = _slot(state)

        delivered = await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert delivered == ["telegram"]
        tp.send_message.assert_awaited_once()
        assert "hi" in tp.send_message.await_args.args[1]

    @pytest.mark.asyncio
    async def test_an_unconfirmed_send_is_not_reported_as_delivered(self, tmp_path) -> None:
        """A transport that returns an empty id on rejection must not be listed.

        `returns_message_id` is the transport's own declaration that an empty id
        means refusal, so trusting "no exception" would report a delivery the user
        never received.
        """
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        tp.send_message = AsyncMock(return_value="")
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )
        slot = _slot(state)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        tp.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_origin_link_is_tried_before_mirror(self, tmp_path) -> None:
        """A Discord-born session records an origin and no mirror."""
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="origin-1", thread_id=None)
        )
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _slot(state)

        assert await _mirror(state, slot, "telegram:1", "hi", "note") == ["telegram"]
        assert tp.send_message.await_args.args[0] == "origin-1"

    @pytest.mark.asyncio
    async def test_a_paused_origin_does_not_withhold_the_active_mirror(
        self, tmp_path, monkeypatch
    ) -> None:
        """The two bindings mute independently, so a paused origin must SKIP and
        let the ladder continue rather than ending the walk."""
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="paused-origin", thread_id=None)
        )
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="live-mirror", thread_id=None)
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.messaging.mirror_is_paused",
            lambda _s, _k, origin=False: origin,
        )
        slot = _slot(state)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == ["telegram"]
        assert tp.send_message.await_args.args[0] == "live-mirror"

    @pytest.mark.asyncio
    async def test_non_proactive_transport_is_skipped(self, tmp_path) -> None:
        """A transport that cannot send proactively must be refused by the shared send.

        SYNTHETIC capabilities, via the helper the sibling suite keeps for this:
        every shipped channel now declares ``supports_proactive_send=True``, so
        this branch has no real subject and pinning it to a live channel would
        make the test vacuous the moment that declaration flipped — which is
        exactly what a WeCom-based version of this test hit. The link names the
        same channel as the transport, or the skip would happen for the unrelated
        reason that no transport is registered for the link's type.
        """
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _caps_transport("telegram", supports_proactive_send=False)
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )
        slot = _slot(state)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        tp.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_slack_link_does_not_ride_the_ladder(self, tmp_path) -> None:
        """Slack has its own leg and is absent from the registry by design."""
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("slack", channel_id="C1", thread_id="1.1")
        )
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _slot(state)

        assert await _mirror(state, slot, "slack:1.1", "hi", "note") == []
        tp.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unlinked_session_is_a_noop(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        state.slack_client = None
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _slot(state)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []


class TestSlackRecipientReauthorization:
    """A persisted Slack link must not outlive the roster that authorized it.

    The link is written once and the roster changes underneath it, so the
    authorization has to be re-asked at SEND time. These pin that: with the
    recipient off the roster the note must not reach Slack, and with nothing able
    to name the recipient the answer must be refusal rather than a pass.
    """

    @pytest.mark.asyncio
    async def test_a_revoked_recipient_receives_nothing(self, tmp_path, monkeypatch) -> None:
        """The roster does not list the channel: revocation must take effect."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch)  # empty roster = revoked
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        delivered = await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert delivered == []
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_still_authorized_recipient_receives_it(self, tmp_path, monkeypatch) -> None:
        """The positive half, so the refusal above is not vacuously green."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == ["slack"]

    @pytest.mark.asyncio
    async def test_an_unnameable_recipient_is_refused_not_allowed(
        self, tmp_path, monkeypatch
    ) -> None:
        """Fail-closed: no principal from the key AND no tracked channel means the
        check cannot answer, and 'cannot tell' must not become 'allowed' on an
        egress boundary."""
        state = _slack_state(tmp_path, channel="D999")
        _authorize_slack_recipient(monkeypatch, "C123")  # a DIFFERENT channel
        slot = _slot(state, thread="1785370133.085469", channel="D999")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()


class TestBindingChangedDuringGovernanceAwait:
    """The governance gate is an await, so the binding can move underneath it.

    `deliver_to_channel` re-walks the ladder after the gate and refuses on any
    disagreement. These pin the two shapes of that window: the conversation is
    unlinked mid-flight, and it is paused mid-flight.
    """

    @pytest.mark.asyncio
    async def test_an_unlink_during_the_await_refuses_the_send(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(return_value=None)
        # No THIRD read: `_deliver_via_transport` does not pre-read the link to label
        # the result, since a pause-blind label can name a transport the ladder rejects.
        link = ChannelLink("telegram", channel_id="123", thread_id=None)
        state.sessions.get_mirror_link = MagicMock(side_effect=[link, None])
        slot = _slot(state)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        tp.send_message.assert_not_awaited()
        assert (
            state.sessions.get_mirror_link.call_count == 2
        ), "the post-await revalidation never re-read the binding"

    @pytest.mark.asyncio
    async def test_a_pause_during_the_await_refuses_the_send(self, tmp_path, monkeypatch) -> None:
        """Paused only on the SECOND look, which is the race the revalidation exists
        for: the walk admitted the binding, then the user disconnected while the
        governance gate ran."""
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )
        looks = {"n": 0}

        def _paused_on_second_look(_s, _k, origin=False):
            looks["n"] += 1
            return looks["n"] > 1

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.messaging.mirror_is_paused", _paused_on_second_look
        )
        slot = _slot(state)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        tp.send_message.assert_not_awaited()
        assert looks["n"] >= 2, "the post-await revalidation never re-checked the pause state"


class TestSlackRevokedDuringGovernanceAwait:
    """The Slack leg's authorization inputs must be re-read AFTER the await.

    `channel_egress_permitted` runs off-loop (a profile-directory walk with a
    possible reload), and every input to the decision before it can change inside
    that window. These drive the change to land DURING the await, which a
    happy-path assertion cannot detect.
    """

    def _slow_gate(self, monkeypatch, on_await):
        """Make the governance gate run *on_await* while it 'walks the profile dir'."""

        def _gate(*_a, **_k):
            on_await()
            return True

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.channel_egress_permitted", _gate)

    @pytest.mark.asyncio
    async def test_roster_revocation_during_the_await_refuses(self, tmp_path, monkeypatch) -> None:
        """Authorized before the gate, revoked while it ran -> must NOT post."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        def _revoke():
            # The roster the shipped is_tracked_channel reads, emptied mid-await.
            import kiro_crew.slack.handler as h

            h._tracking_channels = set()

        self._slow_gate(monkeypatch, _revoke)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unlink_during_the_await_refuses(self, tmp_path, monkeypatch) -> None:
        """The thread is unlinked while the gate runs -> must NOT post to the old id."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        def _unlink():
            slot._slack_thread_ts = ""
            slot._slack_channel = ""
            state.sessions.get_slack_link = MagicMock(return_value=("", ""))

        self._slow_gate(monkeypatch, _unlink)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pause_during_the_await_refuses(self, tmp_path, monkeypatch) -> None:
        """The thread is disconnected while the gate runs -> must NOT post."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")
        looks = {"n": 0}

        def _paused_after_the_gate(_s, _k):
            looks["n"] += 1
            return looks["n"] > 1

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_note_mirror.slack_mirror_is_paused", _paused_after_the_gate
        )

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()
        assert looks["n"] >= 2, "the pause state was never re-checked after the await"

    @pytest.mark.asyncio
    async def test_unlink_during_the_authorization_await_refuses(
        self, tmp_path, monkeypatch
    ) -> None:
        """The AUTHORIZATION await is a window too, not just the governance one.

        The owner-DM basis resolves the owner's DM through `conversations.open`, a
        network call. Revalidating only after the governance
        gate would let a thread unlinked while `open_dm` was in flight still post to
        the coordinates captured before it — the last check has to come after
        EVERY await, not after the first one.
        """
        state = _slack_state(tmp_path, channel="D777")
        _authorize_slack_recipient(monkeypatch)  # forces the owner-DM basis
        state.owner_id = "U_OWNER"
        slot = _slot(state, thread="1785370133.085469", channel="D777")

        async def _open_dm_then_unlink(_user):
            # The unlink lands DURING this await, exactly as a real one would.
            slot._slack_thread_ts = ""
            slot._slack_channel = ""
            state.sessions.get_slack_link = MagicMock(return_value=("", ""))
            return "D777"

        state.slack_client.open_dm = AsyncMock(side_effect=_open_dm_then_unlink)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.open_dm.assert_awaited_once()
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pause_during_the_authorization_await_refuses(
        self, tmp_path, monkeypatch
    ) -> None:
        """Same window, the pause half."""
        state = _slack_state(tmp_path, channel="D777")
        _authorize_slack_recipient(monkeypatch)
        state.owner_id = "U_OWNER"
        slot = _slot(state, thread="1785370133.085469", channel="D777")
        paused = {"v": False}
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_note_mirror.slack_mirror_is_paused",
            lambda _s, _k: paused["v"],
        )

        async def _open_dm_then_pause(_user):
            paused["v"] = True
            return "D777"

        state.slack_client.open_dm = AsyncMock(side_effect=_open_dm_then_pause)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_authorization_is_asked_once_per_note(self, tmp_path, monkeypatch) -> None:
        """One note, one authorization decision.

        A pre-gate fail-fast calling the same auditing predicate as the
        post-gate check would make a single mirrored note write two identical allow
        rows to the SEL and resolve the owner DM twice.
        """
        state = _slack_state(tmp_path, channel="D777")
        _authorize_slack_recipient(monkeypatch)
        state.owner_id = "U_OWNER"
        state.slack_client.open_dm = AsyncMock(return_value="D777")
        events: list[str] = []

        class _Sel:
            def log_api_access(self, **kw):
                if kw.get("operation") == "chat.note.slack_authorize":
                    events.append(kw.get("outcome", ""))

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())
        slot = _slot(state, thread="1785370133.085469", channel="D777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == ["slack"]
        assert events == ["allowed"], f"expected exactly one authorization audit row, got {events}"
        state.slack_client.open_dm.assert_awaited_once()


class TestSlackAuthorizationIsAudited:
    """EVERY authorization outcome emits a SEL event, allow as well as deny.

    A denial-only audit answers "was anything refused" but not "who was this note
    authorized to reach", and on a security control the successful decision is the
    one an operator needs to reconstruct what left the building.
    """

    def _capture(self, monkeypatch):
        events: list[tuple[str, str]] = []

        class _Sel:
            def log_api_access(self, **kw):
                if kw.get("operation") == "chat.note.slack_authorize":
                    events.append((kw.get("outcome", ""), kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())
        return events

    @pytest.mark.asyncio
    async def test_a_successful_authorization_is_audited(self, tmp_path, monkeypatch) -> None:
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        events = self._capture(monkeypatch)
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == ["slack"]
        allowed = [e for e in events if e[0] == "allowed"]
        assert allowed, "an authorized Slack note emitted no allow event"
        assert any("tracked_channel" in r for _o, r in allowed)

    @pytest.mark.asyncio
    async def test_a_refused_authorization_is_audited(self, tmp_path, monkeypatch) -> None:
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch)  # empty roster
        events = self._capture(monkeypatch)
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        assert [e for e in events if e[0] == "denied"], "a refusal emitted no deny event"


class TestSlackEarlyRefusalsAreAudited:
    """The refusals that fire BEFORE authorization must audit too.

    The pause check and the namespace check both sit ahead of the governance gate
    and the recipient-authorization step, so a note stopped by either must file its
    own SEL row. A refusal with no row is the one shape an operator cannot
    reconstruct from the log: a delivery that never happened and never explained
    itself is indistinguishable from a note nobody ever wrote.

    Each carries its OWN reason code rather than a shared "refused", because the
    two mean different things to whoever is reading the rows -- a disconnected
    thread is the user's own choice, while a foreign namespaced id is a caller
    handing this function a destination it must not post to.
    """

    def _capture(self, monkeypatch):
        rows: list[tuple[str, str]] = []

        class _Sel:
            def log_api_access(self, **kw):
                if kw.get("operation") == "chat.note.slack_send":
                    rows.append((kw.get("outcome", ""), kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())
        return rows

    @pytest.mark.asyncio
    async def test_a_paused_thread_records_its_own_denial_reason(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _slack_state(tmp_path)
        slot = _slot(state, thread="1785370133.085469", channel="C123")
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_note_mirror.slack_mirror_is_paused",
            lambda *a, **k: True,
        )
        rows = self._capture(monkeypatch)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()
        assert (
            "denied",
            "session=dashboard:chat-1 reason=thread_disconnected",
        ) in rows, f"the paused refusal emitted no denial row naming its reason: {rows}"

    @pytest.mark.asyncio
    async def test_a_foreign_namespaced_id_records_its_own_denial_reason(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _slack_state(tmp_path, channel="discord:999")
        slot = _slot(state)
        rows = self._capture(monkeypatch)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()
        assert (
            "denied",
            "session=dashboard:chat-1 reason=foreign_namespace",
        ) in rows, f"the foreign-namespace refusal emitted no denial row naming its reason: {rows}"

    @pytest.mark.asyncio
    async def test_the_two_refusals_are_distinguishable_in_the_log(
        self, tmp_path, monkeypatch
    ) -> None:
        """A shared reason code would make the rows unfilterable, which is the
        whole point of auditing them separately."""
        state = _slack_state(tmp_path, channel="discord:999")
        slot = _slot(state)
        foreign = self._capture(monkeypatch)
        await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        state2 = _slack_state(tmp_path)
        slot2 = _slot(state2, thread="1785370133.085469", channel="C123")
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_note_mirror.slack_mirror_is_paused",
            lambda *a, **k: True,
        )
        paused = self._capture(monkeypatch)
        await _mirror(state2, slot2, "dashboard:chat-1", "hi", "note")

        assert [r[1] for r in foreign] != [r[1] for r in paused], (
            "both early refusals logged the same reason code, so an operator "
            f"cannot tell them apart: {foreign} vs {paused}"
        )


class TestNonSlackPrincipalDoesNotShortCircuitAuthorization:
    """A principal is a PLATFORM id, and the roster it is tested against is SLACK's.

    A direct session on another surface that is ALSO linked to Slack names its own
    platform's user id in its key. Feeding that to Slack's user roster asks a
    question about the wrong namespace, and because the principal branch RETURNS,
    a miss there also skipped the tracked-channel and owner-DM authorities that
    could legitimately have named the recipient -- so the note was refused for a
    Slack destination that was in fact authorized.

    Every other test in this file uses the LEGACY ``dashboard:chat-1`` key, which
    the canonical parser rejects, so the principal is empty and this branch never
    fires. That is why the gap survived: the canonical cross-surface key is the
    only shape that reaches it.
    """

    @pytest.mark.asyncio
    async def test_a_telegram_direct_session_still_reaches_tracked_channel_authorization(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _slack_state(tmp_path)
        # C123 IS authorized, on the real roster. The telegram id deliberately is
        # not -- it could not be, it is not a Slack identity at all.
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")
        bases: list[str] = []

        class _Sel:
            def log_api_access(self, **kw):
                if kw.get("operation") == "chat.note.slack_authorize":
                    bases.append(str(kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())

        delivered = await _mirror(
            state, slot, "telegram:kirocrew:direct:U_TELEGRAM_ONLY", "hi", "note"
        )

        assert delivered == ["slack"], (
            "a telegram-origin session linked to an AUTHORIZED Slack channel was "
            f"refused; authorization rows={bases}"
        )
        assert any("basis=tracked_channel" in b for b in bases), (
            "authorization did not reach the tracked-channel authority -- the "
            f"principal branch answered first: {bases}"
        )
        assert not any("basis=session_principal" in b for b in bases), (
            "a non-Slack platform id was tested against the Slack user roster: " f"{bases}"
        )

    @pytest.mark.asyncio
    async def test_a_slack_origin_principal_is_still_authorized_by_the_roster(
        self, tmp_path, monkeypatch
    ) -> None:
        """The positive half: gating on surface must not disable the principal path.

        Without this, the fix above could 'pass' by never consulting a principal at
        all, which would drop the one authority that covers a Slack DM whose channel
        no roster names.
        """
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch)  # empty channel roster
        monkeypatch.setattr(
            "kiro_crew.slack.handler._allowed_users", {"U_SLACK_PEER"}, raising=False
        )
        slot = _slot(state, thread="1785370133.085469", channel="C999")
        bases: list[str] = []

        class _Sel:
            def log_api_access(self, **kw):
                if kw.get("operation") == "chat.note.slack_authorize":
                    bases.append(str(kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())

        await _mirror(state, slot, "slack:kirocrew:direct:U_SLACK_PEER", "hi", "note")

        assert any("basis=session_principal" in b for b in bases), (
            "a SLACK-origin key stopped consulting its principal, so the surface "
            f"gate is too broad: {bases}"
        )


class TestPrincipalAuthorityIsBoundToItsDestination:
    """Roster membership names a PERSON; it must not authorize an arbitrary channel.

    The principal branch answers for the whole ladder, so granting on membership
    alone let an allowed principal's session authorize ANY bound ``channel_id`` --
    including a channel whose own authority had been revoked by untracking it --
    because the tracked-channel and configured-channel authorities never ran.
    """

    def _capture(self, monkeypatch) -> list[dict]:
        rows: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                if kw.get("operation") == "chat.note.slack_authorize":
                    rows.append(kw)

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())
        return rows

    @pytest.mark.asyncio
    async def test_an_allowed_principal_cannot_authorize_a_channel_that_is_not_its_dm(
        self, tmp_path, monkeypatch
    ) -> None:
        """A revoked channel must stay refused even when the principal is allowed."""
        state = _slack_state(tmp_path, channel="C_REVOKED")
        _authorize_slack_recipient(monkeypatch)  # nothing tracked: revocation happened
        # is_allowed_user IS is_owner -- multi-user Slack access is disabled -- so the
        # principal only ever passes when it is the owner id.
        monkeypatch.setattr("kiro_crew.slack.handler._owner_id", "U_SLACK_PEER", raising=False)
        state.slack_client.open_dm = AsyncMock(return_value="D_PEER")
        slot = _slot(state, thread="1785370133.085469", channel="C_REVOKED")
        rows = self._capture(monkeypatch)

        delivered = await _mirror(state, slot, "slack:kirocrew:direct:U_SLACK_PEER", "hi", "note")

        assert delivered == [], (
            f"an allowed principal authorized a send to a channel that is not its DM "
            f"and that no channel authority names; rows={rows}"
        )
        granted = [
            r
            for r in rows
            if r.get("outcome") == "allowed" and "session_principal" in str(r.get("resources"))
        ]
        assert granted == [], f"the principal basis was GRANTED for a non-DM destination: {granted}"

    @pytest.mark.asyncio
    async def test_an_allowed_principal_still_authorizes_its_own_dm(
        self, tmp_path, monkeypatch
    ) -> None:
        """The positive half: binding to the destination must not disable the authority."""
        state = _slack_state(tmp_path, thread="1785370133.085469", channel="D_PEER")
        _authorize_slack_recipient(monkeypatch)
        # is_allowed_user IS is_owner -- multi-user Slack access is disabled -- so the
        # principal only ever passes when it is the owner id.
        monkeypatch.setattr("kiro_crew.slack.handler._owner_id", "U_SLACK_PEER", raising=False)
        state.slack_client.open_dm = AsyncMock(return_value="D_PEER")
        slot = _slot(state, thread="1785370133.085469", channel="D_PEER")
        rows = self._capture(monkeypatch)

        await _mirror(state, slot, "slack:kirocrew:direct:U_SLACK_PEER", "hi", "note")

        granted = [
            r
            for r in rows
            if r.get("outcome") == "allowed" and "session_principal" in str(r.get("resources"))
        ]
        assert granted, (
            f"a principal's OWN DM stopped being authorized, so the destination "
            f"binding is too strict; rows={rows}"
        )


class TestOwnerDmIsAuthorized:
    """A dashboard-created link targets the owner's DM, which no roster names.

    It carries no principal (a ``dashboard:`` key names no peer) and its ``D…`` id
    is not a tracked channel, so both other authorities decline the intended
    destination. It is authorized through the owner identity that created it.
    """

    @pytest.mark.asyncio
    async def test_the_owners_dm_is_delivered_to(self, tmp_path, monkeypatch) -> None:
        state = _slack_state(tmp_path, channel="D777")
        _authorize_slack_recipient(monkeypatch)  # no tracked channels at all
        state.owner_id = "U_OWNER"
        state.slack_client.open_dm = AsyncMock(return_value="D777")
        slot = _slot(state, thread="1785370133.085469", channel="D777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == ["slack"]
        state.slack_client.open_dm.assert_awaited_with("U_OWNER")

    @pytest.mark.asyncio
    async def test_a_different_dm_is_refused(self, tmp_path, monkeypatch) -> None:
        """Someone else's DM must not ride the owner-DM allowance."""
        state = _slack_state(tmp_path, channel="D999")
        _authorize_slack_recipient(monkeypatch)
        state.owner_id = "U_OWNER"
        state.slack_client.open_dm = AsyncMock(return_value="D777")
        slot = _slot(state, thread="1785370133.085469", channel="D999")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unresolvable_owner_dm_is_refused(self, tmp_path, monkeypatch) -> None:
        """Cannot establish identity -> refuse, never pass."""
        state = _slack_state(tmp_path, channel="D777")
        _authorize_slack_recipient(monkeypatch)
        state.owner_id = "U_OWNER"
        state.slack_client.open_dm = AsyncMock(side_effect=RuntimeError("slack down"))
        slot = _slot(state, thread="1785370133.085469", channel="D777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        state.slack_client.post_message.assert_not_awaited()


def _configure_slack_channel(monkeypatch, channel_id: str, activation: str) -> None:
    """Put *channel_id* in the real ``slack_channels`` config at *activation*.

    Patches ``KiroCrewConfig.load`` rather than the authority predicate, so these
    tests exercise the shipped decision. *activation* is passed through verbatim so
    a test can pin a NON-offered mode (``review``/``off``) and prove it is not an
    authority.
    """
    from kiro_crew.config.loader import ChannelConfig, KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.slack_channels = {channel_id: ChannelConfig(activation=activation)}
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))


class TestConfiguredChannelIsAuthorized:
    """A channel configured in ``slack_channels`` is a real note destination.

    ``list_slack_channels`` offers exactly these channels in the dashboard's Slack
    link picker, so a user can bind a session to one -- but such a channel carries
    no principal (a ``dashboard:`` key names no peer), is not on the tracking
    roster (a SEPARATE config key), and is not a ``D…`` owner DM. Before this
    authority existed all three declined and the note was silently dropped into a
    destination the product itself had offered.
    """

    #: Spelled out LITERALLY, not derived from ``OFFERED_ACTIVATIONS``: a list
    #: comprehended from the production set would lose its case along with the
    #: regression, so dropping a mode would silently reduce the parameter list
    #: instead of failing. ``test_the_offered_set_is_exactly_these`` is what keeps
    #: the two from drifting apart.
    OFFERED = ["always", "mention", "observe"]

    def test_the_offered_set_is_exactly_these(self) -> None:
        """The literal list above must stay equal to the shipped authority set.

        Without this, adding a fourth offered mode to production would go untested
        while every existing case still passed.
        """
        import kiro_crew.dashboard.slack_egress as se

        assert se.OFFERED_ACTIVATIONS == frozenset(self.OFFERED)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("activation", OFFERED)
    async def test_an_active_configured_channel_is_delivered_to(
        self, tmp_path, monkeypatch, activation
    ) -> None:
        """EVERY offered activation must authorize, not just ``always``.

        ``list_slack_channels`` offers all three, so a regression that stopped
        honouring ``mention`` or ``observe`` would silently drop notes into channels
        the picker still presents.
        """
        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)  # nothing tracked
        _configure_slack_channel(monkeypatch, "C777", activation)
        state.owner_id = ""
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == ["slack"]

    @pytest.mark.asyncio
    async def test_the_config_read_never_runs_on_the_event_loop(
        self, tmp_path, monkeypatch
    ) -> None:
        """``KiroCrewConfig.load`` does filesystem work, so it must run off the loop.

        Both call sites -- the basis ladder and the per-send revalidation -- go through
        ``asyncio.to_thread``, matching the convention ``_egress_permitted`` already uses
        for the same reason. A direct call from either coroutine would stall the
        gateway's whole event loop on a ``stat`` pair and a possible parse. The
        discriminator is the THREAD: pytest-asyncio drives the loop on the main thread,
        so anything dispatched to a worker is provably not blocking it.
        """
        import threading

        import kiro_crew.dashboard.slack_egress as se

        threads: list[bool] = []

        def _recording_authority(channel_id: str) -> bool:
            threads.append(threading.current_thread() is threading.main_thread())
            return True

        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)
        monkeypatch.setattr(se, "_configured_channel_active", _recording_authority)
        state.owner_id = ""
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == ["slack"]
        assert len(threads) >= 2, (
            f"expected the ladder AND the per-send revalidation to consult the "
            f"authority, saw {len(threads)} call(s)"
        )
        assert not any(
            threads
        ), "the config read ran on the event-loop thread, blocking the gateway"

    def test_the_last_await_is_the_combined_authority_read(self) -> None:
        """Nothing may await after the combined authority read in ``_permitted_to_send``.

        The synchronous tail is what closes the window that read opens, so any await
        placed after it leaves one of the two authorities stale for its own duration
        -- which is the whole reason governance and the configured-channel check now
        share a single hop. Asserted structurally rather than behaviourally because
        the guarantee IS the absence of a yield point: only a read of the source can
        fail while that yield exists.
        """
        import ast
        import inspect

        import kiro_crew.dashboard.slack_egress as se

        tree = ast.parse(inspect.getsource(se))
        target = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_permitted_to_send"
        )
        awaits = sorted(
            (n.lineno, ast.dump(n.value)) for n in ast.walk(target) if isinstance(n, ast.Await)
        )

        assert awaits, "the send path performs no authority read at all"
        last_line, last_dump = awaits[-1]
        assert "_send_time_authorities" in last_dump, (
            f"the final await at line {last_line} is not the combined authority read, "
            f"so one of the two authorities goes stale before the send"
        )

    @pytest.mark.asyncio
    async def test_deactivation_during_the_governance_read_refuses(
        self, tmp_path, monkeypatch
    ) -> None:
        """A revocation landing INSIDE the governance read must refuse this send.

        The race: the configured-channel authority and the governance gate are both
        read before the send. If they are two separate awaits, whichever runs first
        goes stale while the other runs -- so a deactivation arriving during the
        governance read leaves a stale ``True`` and ``post_message`` delivers after
        the authority was withdrawn.

        Driven deterministically rather than by timing: the governance stub itself
        performs the deactivation, so it lands provably inside that read. Under the
        two-await ordering the config value was already captured and the note ships;
        with both reads in one hop -- governance first, config second -- the config
        read sees the deactivation and the send refuses.
        """
        import kiro_crew.dashboard.slack_egress as se

        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)
        _configure_slack_channel(monkeypatch, "C777", "always")
        state.owner_id = ""

        calls = {"n": 0}

        def _governance_that_revokes(*args, **kwargs):
            # The FIRST call is the early gate, which runs before the basis ladder;
            # revoking there would make the ladder decline and the send would refuse
            # for the wrong reason, passing under either ordering. Revoke only on the
            # final authority read, which is the window under test.
            calls["n"] += 1
            if calls["n"] >= 2:
                _configure_slack_channel(monkeypatch, "C777", "off")
            return True

        monkeypatch.setattr(se, "channel_egress_permitted", _governance_that_revokes)
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == [], (
            "the channel was deactivated during the governance read, so this send "
            "must refuse -- a stale authorization reached post_message"
        )
        assert calls["n"] >= 2, (
            f"the governance gate was asked only {calls['n']} time(s), so the "
            f"revocation never landed in the window under test"
        )

    @pytest.mark.asyncio
    async def test_an_unconfigured_channel_is_still_refused(self, tmp_path, monkeypatch) -> None:
        """The authority is the CONFIG entry, not the ``C`` prefix."""
        state = _slack_state(tmp_path, channel="C999")
        _authorize_slack_recipient(monkeypatch)
        _configure_slack_channel(monkeypatch, "C777", "always")
        state.owner_id = ""
        slot = _slot(state, thread="1785370133.085469", channel="C999")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []

    def test_the_picker_and_the_egress_authority_share_one_spelling(self) -> None:
        """The offered-activation set must exist exactly ONCE in the source.

        The egress authority honours a configured channel only in an activation the
        dashboard's link picker actually OFFERS. Two literal copies of that set drift
        apart silently, and the drift is invisible in both directions: widening it
        lets a note reach a channel the product never let the user bind to, narrowing
        it starves delivery to channels the picker still lists. So the picker imports
        the constant instead of repeating the tuple, and this test fails if a second
        literal spelling reappears anywhere in the dashboard package.
        """
        import pathlib

        import kiro_crew.dashboard.chat_slack as chat_slack
        import kiro_crew.dashboard.slack_egress as se

        assert (
            chat_slack.OFFERED_ACTIVATIONS is se.OFFERED_ACTIVATIONS
        ), "the picker must consume the egress module's constant, not its own copy"
        assert se.OFFERED_ACTIVATIONS == frozenset({"always", "mention", "observe"})

        # The literal may appear only where the constant is DECLARED. Any other
        # occurrence in the package is a second spelling that can drift.
        #
        # ``encoding`` is explicit because ``read_text()`` otherwise inherits the
        # platform default -- UTF-8 on Linux but cp1252 on Windows, which raises
        # UnicodeDecodeError on the first non-ASCII byte in the package. The source
        # tree is UTF-8, so naming it is what makes this scan platform-independent
        # rather than passing everywhere except the Windows shard.
        root = pathlib.Path(se.__file__).parent
        offenders = sorted(
            path.name
            for path in root.rglob("*.py")
            if '"always", "mention", "observe"' in path.read_text(encoding="utf-8")
        )
        assert offenders == ["slack_egress.py"], (
            f"the offered-activation literal must be spelled once, in the module that "
            f"declares the constant; found it in {offenders}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("activation", ["review", "off"])
    async def test_a_non_offered_activation_is_refused(
        self, tmp_path, monkeypatch, activation
    ) -> None:
        """Only the modes ``list_slack_channels`` OFFERS may authorize a send.

        ``review`` and ``off`` are configured but never presented as destinations,
        so treating them as an authority would widen egress past what the product
        lets a user bind to.
        """
        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)
        _configure_slack_channel(monkeypatch, "C777", activation)
        state.owner_id = ""
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []

    @pytest.mark.asyncio
    async def test_deactivation_mid_send_refuses_the_remainder(self, tmp_path, monkeypatch) -> None:
        """The basis is REVALIDATED, not captured -- the TOCTOU property holds.

        Keyed on the AUTHORITY, not on a config-read count: something else on this
        path already reads the config once, so counting loads could pass because the
        first authorization read saw a deactivated channel rather than because the
        basis was re-asked. Here the authority answers True once and False after, so
        a captured basis would send and only a re-asked one refuses.
        """
        import kiro_crew.dashboard.slack_egress as se

        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)
        state.owner_id = ""
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        answers = [True]
        calls = {"n": 0}

        def _authority(channel_id: str) -> bool:
            calls["n"] += 1
            return answers.pop(0) if answers else False

        monkeypatch.setattr(se, "_configured_channel_active", _authority)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []
        assert calls["n"] >= 2, "the authority was asked once, so revalidation is absent"


class TestLadderSelectsTheChannelType:
    @pytest.mark.asyncio
    async def test_a_paused_origin_of_a_different_type_does_not_block_the_mirror(
        self, tmp_path, monkeypatch
    ) -> None:
        """The caller must not name the type: a pause-blind read would name the
        paused ORIGIN's transport while the pause-aware ladder selects the active
        MIRROR's, and the type-match guard would then reject the live destination."""
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="paused-origin", thread_id=None)
        )
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="live-mirror", thread_id=None)
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.messaging.mirror_is_paused",
            lambda _s, _k, origin=False: origin,
        )
        slot = _slot(state)

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == ["telegram"]
        assert tp.send_message.await_args.args[0] == "live-mirror"


class TestLegBudgetScalesWithChunkCount:
    """A long note's budget must cover every chunk the leg will really send."""

    def test_the_budget_scales_without_a_ceiling(self):
        """No cap on the TOTAL, because a capped total is a flat deadline again.

        A ceiling stops the budget growing past some length. Past that point the
        bound stops covering the work: it expires mid-loop and the leg reports
        undelivered having already posted a prefix -- the slice-truncation chunking
        removed, returning as a timeout. Pinned as a RATIO so retuning the per-chunk
        figure stays green while re-flattening the curve does not.
        """
        from kiro_crew.dashboard.chat_note_mirror import _CHUNK_TIMEOUT_S, _leg_budget

        cap = 4000
        one = _leg_budget("x" * cap, cap)
        forty = _leg_budget("x" * (cap * 40), cap)

        assert one == pytest.approx(_CHUNK_TIMEOUT_S)
        assert forty == pytest.approx(_CHUNK_TIMEOUT_S * 40), (
            "the budget must stay proportional at 40 chunks; a smaller value means a "
            f"ceiling is clamping it again (got {forty})"
        )

    def test_the_budget_counts_the_legs_real_cap_not_one_shared_estimate(self):
        """Budgeting from the platform's own cap is what makes the count right."""
        from kiro_crew.dashboard.chat_note_mirror import _CHUNK_TIMEOUT_S, _leg_budget

        text = "x" * 14_000
        # A 7000-char cap sends 2 chunks; a 3500-char cap sends 4. The budgets must
        # differ accordingly -- one shared estimate cannot be right for both legs.
        assert _leg_budget(text, 7000) == pytest.approx(_CHUNK_TIMEOUT_S * 2)
        assert _leg_budget(text, 3500) == pytest.approx(_CHUNK_TIMEOUT_S * 4)


class TestLongNoteIsNotTruncatedByTheBudget:
    """The regression a capped total causes: a valid long note losing its tail."""

    @pytest.mark.asyncio
    async def test_every_chunk_of_a_long_note_is_delivered(self, tmp_path, monkeypatch):
        """A note with more chunks than a flat budget covers must still arrive whole.

        Each chunk costs real (small) time here, and there are more chunks than a
        capped total divided by the per-chunk allowance would cover. Under a capped
        total the leg is cancelled part-way and the tail never sends; under a
        proportional budget every chunk gets its own allowance. The TAIL is the
        assertion, because a part count alone would also pass if chunking collapsed.
        """
        from kiro_crew.dashboard import chat_note_mirror as mod

        # Scaled down so the test is fast: 40ms allowed per chunk, ~4ms spent.
        monkeypatch.setattr(mod, "_CHUNK_TIMEOUT_S", 0.04)
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        link = ChannelLink("telegram", channel_id="123", thread_id=None)
        state.sessions.get_origin_link = MagicMock(return_value=link)
        state.sessions.get_mirror_link = MagicMock(return_value=None)

        sent: list[str] = []

        async def _slow_send(_ch, text, thread_id=None):
            await asyncio.sleep(0.004)
            sent.append(text)
            return "mid-%d" % len(sent)

        tp.send_message = AsyncMock(side_effect=_slow_send)
        slot = _slot(state)

        cap = tp.capabilities.max_message_chars
        chunks = 30
        body = "z" * (cap * (chunks - 1)) + "TAIL-OF-NOTE"

        await _mirror(state, slot, "dashboard:chat-1", body, "note")

        assert len(sent) >= chunks, (
            f"only {len(sent)} of ~{chunks} chunks were sent -- the budget expired "
            "mid-send and the remainder was dropped"
        )
        assert "TAIL-OF-NOTE" in "".join(sent), (
            "the note's tail never arrived: the leg was cancelled after delivering a "
            "prefix, which is the truncation chunking exists to prevent"
        )

    @pytest.mark.asyncio
    async def test_a_note_past_the_old_refusal_limit_is_still_delivered(self, tmp_path):
        """A note projecting a large budget must DELIVER, not be dropped up front.

        There is no up-front size refusal any more, and none is reachable: a capped
        note cannot project past the old 600s ceiling on any real transport divisor.
        A reintroduced refusal would silently drop a note that the per-leg `wait_for`
        already bounds, so the TAIL is asserted -- a chunk count alone would also
        pass if chunking collapsed to one part.
        """
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        link = ChannelLink("telegram", channel_id="123", thread_id=None)
        state.sessions.get_origin_link = MagicMock(return_value=link)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _slot(state)

        sent: list[str] = []

        async def _record(_ch, text, thread_id=None):
            sent.append(text)
            return "mid-%d" % len(sent)

        tp.send_message = AsyncMock(side_effect=_record)

        # Well past the budget the deleted guard would have refused at.
        over = 80
        body = "y" * (tp.capabilities.max_message_chars * over) + "TAIL-OF-NOTE"

        await _mirror(state, slot, "dashboard:chat-1", body, "note")

        assert tp.send_message.await_count > 0, (
            "a long note sent ZERO chunks -- an up-front size refusal was "
            "reintroduced, dropping a note the per-leg bound already covers"
        )
        assert "TAIL-OF-NOTE" in "".join(sent), (
            "the note's tail never arrived: the leg was cancelled after delivering a "
            "prefix, which is the truncation chunking exists to prevent"
        )

    def test_a_multi_chunk_leg_gets_no_cancellable_deadline(self):
        """A chunk LOOP must not be wrapped in a cancelling timeout.

        When such a deadline fires the early chunks are already posted, so
        cancellation leaves a misleading PREFIX in the channel with the remainder
        silently gone -- the truncation chunking exists to prevent, returning as a
        timeout instead of a slice. A reader seeing three of eight parts cannot tell
        that from a note that was only ever three parts.

        A SINGLE-chunk leg is different in kind: there is no earlier chunk to orphan,
        so cancelling delivers nothing, which is the honest failure mode. So the bound
        is kept exactly where it cannot truncate.
        """
        from kiro_crew.dashboard import chat_note_mirror as mod

        assert mod._leg_parts("x" * 100, 3900) == 1, "single-chunk precondition"
        assert mod._leg_parts("x" * 40000, 3900) == 11, "multi-chunk precondition"

    @pytest.mark.asyncio
    async def test_a_slack_only_session_is_not_dropped_by_a_phantom_transport_budget(
        self, tmp_path, monkeypatch
    ) -> None:
        """Budgets are PER LEG, so one leg's budget cannot suppress the other's.

        The normal Slack-only case has `channel_link is None`, so the transport cap
        falls back to the deliberately-small `_FALLBACK_CHUNK_CHARS` and a budget is
        computed for a leg that `_deliver_via_transport` refuses anyway. That phantom
        budget is ~4x the real Slack one (1000-char divisor vs 3900). Under a shared
        `max(...)`, or under a reinstated up-front refusal keyed on it, it would drop
        a Slack leg comfortably inside its own budget -- a silent non-delivery caused
        entirely by a leg that never runs.
        """
        from kiro_crew.dashboard import chat_note_mirror as mod

        _authorize_slack_recipient(monkeypatch, "C123")
        state = _slack_state(tmp_path)
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        body = "z" * (mod._FALLBACK_CHUNK_CHARS * 80)
        # The precondition that makes this non-vacuous: the phantom divisor really
        # does project a larger budget than the Slack leg's own.
        assert mod._leg_budget(body, mod._FALLBACK_CHUNK_CHARS) > mod._leg_budget(
            body, SLACK_MSG_LIMIT
        )

        await _mirror(state, slot, "dashboard:chat-1", body, "note")

        assert state.slack_client.post_message.await_count > 0, (
            "the Slack leg was dropped because of a budget computed for a transport "
            "leg that never runs; a Slack-only session is the normal case, so this "
            "silently drops the mirror for most sessions with a long note"
        )


class TestRevocationMidSendStopsRemainingChunks:
    """A multi-part note must stop the moment the destination is withdrawn.

    Every send is an await, so a long note spans a window in which the thread can
    be unlinked, the mirror disconnected, or the roster revoked. Checking only
    before part 1 would keep pushing parts 2..N at a conversation that is no
    longer permitted to receive them.

    Parts already delivered cannot be recalled, so these assert the ABORT
    contract: no FURTHER part is sent, and the delivery is reported as failed so
    the leg is logged as undelivered rather than counted a success.
    """

    @pytest.mark.asyncio
    async def test_slack_unlink_after_the_first_chunk_stops_the_rest(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")
        body = "x" * (SLACK_MSG_LIMIT * 3)

        async def _post_then_unlink(_ch, _text, _ts=None):
            # The unlink lands while part 1 is in flight.
            slot._slack_thread_ts = ""
            slot._slack_channel = ""
            state.sessions.get_slack_link = MagicMock(return_value=("", ""))
            return "ts-1"

        state.slack_client.post_message = AsyncMock(side_effect=_post_then_unlink)

        delivered = await _mirror(state, slot, "dashboard:chat-1", body, "note")

        assert delivered == [], "a revoked mid-send delivery was reported as success"
        assert (
            state.slack_client.post_message.await_count == 1
        ), "chunks kept going after the thread was unlinked"

    @pytest.mark.asyncio
    async def test_slack_roster_revocation_after_the_first_chunk_stops_the_rest(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")
        body = "x" * (SLACK_MSG_LIMIT * 3)

        async def _post_then_revoke(_ch, _text, _ts=None):
            import kiro_crew.slack.handler as h

            h._tracking_channels = set()
            return "ts-1"

        state.slack_client.post_message = AsyncMock(side_effect=_post_then_revoke)

        delivered = await _mirror(state, slot, "dashboard:chat-1", body, "note")

        assert delivered == []
        assert state.slack_client.post_message.await_count == 1

    @pytest.mark.asyncio
    async def test_slack_pause_after_the_first_chunk_stops_the_rest(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")
        body = "x" * (SLACK_MSG_LIMIT * 3)
        paused = {"v": False}
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_note_mirror.slack_mirror_is_paused",
            lambda _s, _k: paused["v"],
        )

        async def _post_then_pause(_ch, _text, _ts=None):
            paused["v"] = True
            return "ts-1"

        state.slack_client.post_message = AsyncMock(side_effect=_post_then_pause)

        delivered = await _mirror(state, slot, "dashboard:chat-1", body, "note")

        assert delivered == []
        assert state.slack_client.post_message.await_count == 1

    @pytest.mark.asyncio
    async def test_a_whole_multi_chunk_note_still_delivers_when_nothing_is_revoked(
        self, tmp_path, monkeypatch
    ) -> None:
        """The positive half — without it the aborts above could pass vacuously."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")
        body = "x" * (SLACK_MSG_LIMIT * 3)

        delivered = await _mirror(state, slot, "dashboard:chat-1", body, "note")

        assert delivered == ["slack"]
        assert state.slack_client.post_message.await_count > 1, "this note was not multi-chunk"

    @pytest.mark.asyncio
    async def test_transport_unlink_after_the_first_chunk_stops_the_rest(self, tmp_path) -> None:
        """Same contract on the shared governed send."""
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        link = ChannelLink("telegram", channel_id="123", thread_id=None)
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(return_value=link)

        async def _send_then_unlink(_ch, _text, thread_id=None):
            state.sessions.get_mirror_link = MagicMock(return_value=None)
            return "mid-1"

        tp.send_message = AsyncMock(side_effect=_send_then_unlink)
        slot = _slot(state)
        body = "y" * (tp.capabilities.max_message_chars * 3)

        delivered = await _mirror(state, slot, "dashboard:chat-1", body, "note")

        assert delivered == [], "a revoked mid-send transport delivery reported success"
        assert tp.send_message.await_count == 1, "chunks kept going after the binding was cleared"

    @pytest.mark.asyncio
    async def test_transport_multi_chunk_still_delivers_when_nothing_is_revoked(
        self, tmp_path
    ) -> None:
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )
        slot = _slot(state)
        body = "y" * (tp.capabilities.max_message_chars * 3)

        delivered = await _mirror(state, slot, "dashboard:chat-1", body, "note")

        assert delivered == ["telegram"]
        assert tp.send_message.await_count > 1, "this note was not multi-chunk"

    @pytest.mark.asyncio
    async def test_transport_unlink_during_the_mid_send_re_resolve_stops_the_rest(
        self, tmp_path, monkeypatch
    ) -> None:
        """The mid-send re-resolve is ITSELF an await, so the walk before it goes stale.

        This is the window a pre-await walk structurally cannot see. The resolver
        was handed the link captured BEFORE the await, so when revocation lands
        during the resolution it still answers with the same conversation and the
        id comparison passes -- the send then reaches a destination whose
        permission was withdrawn. Only a walk placed AFTER the await catches it,
        which is what the first part already did and parts 2..N did not.
        """
        import kiro_crew.dashboard.chat_runner as cr

        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        link = ChannelLink("telegram", channel_id="123", thread_id=None)
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(return_value=link)

        real_resolve = cr._resolve_channel_target
        calls = {"n": 0}

        def _resolve_then_unlink(st, key, lk):
            calls["n"] += 1
            out = real_resolve(st, key, lk)
            if calls["n"] >= 3:
                # Timed INSIDE the re-resolution: call 1 is the pre-send resolve and call 2
                # is part 1's own re-ask, so the mid-send re-resolve here is the third.
                state.sessions.get_mirror_link = MagicMock(return_value=None)
            return out

        monkeypatch.setattr(cr, "_resolve_channel_target", _resolve_then_unlink)
        slot = _slot(state)
        body = "y" * (tp.capabilities.max_message_chars * 3)

        delivered = await _mirror(state, slot, "dashboard:chat-1", body, "note")

        assert calls["n"] >= 3, "the mid-send re-resolve never ran, so this proved nothing"
        assert delivered == [], "a binding revoked during the re-resolve reported success"
        assert (
            tp.send_message.await_count == 1
        ), "parts kept going after the binding was revoked inside the re-resolve window"


class TestGovernanceRevokedDuringAuthorization:
    """The gate ran BEFORE the authorization await, so a revocation inside it posted.

    ``_slack_recipient_authorized`` awaits ``conversations.open`` to resolve the
    owner-DM basis. An admin narrowing the ``channels`` scope during that call was
    invisible: the coordinate check that follows compares the thread and channel,
    not the egress permission, so the note went out after the revocation. Asking
    the gate again after the await is what turns that window into a refusal.
    """

    @pytest.mark.asyncio
    async def test_egress_revoked_during_the_authorization_await_refuses(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _slack_state(tmp_path, channel="D777")
        # Empty roster, so authorization falls through to the owner-DM basis and
        # its `conversations.open` await is the window under test.
        _authorize_slack_recipient(monkeypatch)
        state.owner_id = "U_OWNER"
        permitted = {"v": True}
        denials: list[str] = []

        class _Sel:
            def log_api_access(self, **kw):
                if kw.get("outcome") == "denied":
                    denials.append(str(kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())
        monkeypatch.setattr(
            "kiro_crew.dashboard.slack_egress.channel_egress_permitted",
            lambda *_a, **_k: permitted["v"],
        )

        async def _open_dm_then_revoke(_owner):
            # The admin narrows the `channels` scope while `conversations.open` is
            # in flight -- i.e. strictly after the gate already answered yes.
            permitted["v"] = False
            return "D777"

        state.slack_client.open_dm = AsyncMock(side_effect=_open_dm_then_revoke)
        slot = _slot(state, thread="1785370133.085469", channel="D777")

        delivered = await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert delivered == [], "a note posted after slack egress was revoked mid-authorization"
        state.slack_client.post_message.assert_not_awaited()
        assert any(
            "reason=not_permitted_mid_send_before_part_1" in d for d in denials
        ), f"the mid-flight revocation was not audited; denials={denials}"

    @pytest.mark.asyncio
    async def test_the_same_path_still_delivers_when_egress_is_never_revoked(
        self, tmp_path, monkeypatch
    ) -> None:
        """The positive half -- without it the refusal above could pass vacuously
        by breaking the owner-DM basis rather than by catching the revocation."""
        state = _slack_state(tmp_path, channel="D777")
        _authorize_slack_recipient(monkeypatch)
        state.owner_id = "U_OWNER"
        monkeypatch.setattr(
            "kiro_crew.dashboard.slack_egress.channel_egress_permitted",
            lambda *_a, **_k: True,
        )
        state.slack_client.open_dm = AsyncMock(return_value="D777")
        slot = _slot(state, thread="1785370133.085469", channel="D777")

        delivered = await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert delivered == ["slack"]
        state.slack_client.post_message.assert_awaited_once()


class TestOneLegCannotStarveTheOther:
    """A stalled or failing leg must not cost a healthy sibling its delivery.

    The two legs were awaited in SEQUENCE under one shared budget, which made
    each the other's failure domain: a Slack send that hung consumed the whole
    budget and the transport leg never ran, so the note was permanently absent
    from a healthy second channel. The mirror image is just as damaging -- a
    sibling stalling after Slack had already delivered discarded the record of a
    real send, and a caller acting on `[]` re-writes a note the channel has shown.
    """

    def _healthy_transport(self, state):
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )
        return tp

    @pytest.mark.asyncio
    async def test_a_stalled_slack_leg_still_lets_the_transport_leg_deliver(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        tp = self._healthy_transport(state)
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        async def _hang(*_a, **_k):
            await asyncio.sleep(3600)

        state.slack_client.post_message = AsyncMock(side_effect=_hang)
        # Patches the constant the budget is DERIVED from: a leg's bound is
        # `_CHUNK_TIMEOUT_S` per chunk it will send, so for this one-chunk note that
        # IS the leg bound. Short enough to assert the bound rather than wait one
        # out, but ample for the healthy leg, which does a real to_thread
        # profile-directory walk. An earlier revision patched `_LEG_TIMEOUT_S`, which
        # no production code read -- so this test sat for the real 8s budget and
        # asserted a bound it did not control. The runtime is the tell: 8.02s before,
        # ~1s after.
        monkeypatch.setattr("kiro_crew.dashboard.chat_note_mirror._CHUNK_TIMEOUT_S", 1.0)

        delivered = await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert delivered == ["telegram"], (
            "a hung Slack leg starved a healthy transport channel; the note is "
            f"permanently absent from it (delivered={delivered})"
        )
        tp.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_stalled_transport_leg_still_reports_the_completed_slack_send(
        self, tmp_path, monkeypatch
    ) -> None:
        """The completed-results half: Slack really delivered, so it must be
        reported even though its sibling never finished."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        tp = self._healthy_transport(state)
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        async def _hang(*_a, **_k):
            await asyncio.sleep(3600)

        tp.send_message = AsyncMock(side_effect=_hang)
        # Same reasoning as the sibling above: bound the CHUNK, which is what the
        # leg budget is built from, rather than a constant nothing reads.
        monkeypatch.setattr("kiro_crew.dashboard.chat_note_mirror._CHUNK_TIMEOUT_S", 1.0)

        delivered = await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert delivered == ["slack"], (
            "a hung transport leg discarded the record of a Slack send that had "
            f"already completed (delivered={delivered})"
        )
        state.slack_client.post_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_raising_slack_leg_still_lets_the_transport_leg_deliver(
        self, tmp_path, monkeypatch
    ) -> None:
        """`channel_egress_permitted` re-raises `PlatformCompositionError` rather
        than degrading, so before this the raise propagated out of the mirror and
        the transport leg was never reached."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        tp = self._healthy_transport(state)
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        def _explode(*_a, **_k):
            raise RuntimeError("invalid platform composition")

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.channel_egress_permitted", _explode)

        delivered = await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert delivered == [
            "telegram"
        ], f"a raising Slack leg starved a healthy transport channel (delivered={delivered})"
        tp.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_both_healthy_legs_still_deliver_in_order(self, tmp_path, monkeypatch) -> None:
        """The positive half. Running the legs concurrently must not reorder the
        report, which a caller may compare."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        tp = self._healthy_transport(state)
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        delivered = await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert delivered == ["slack", "telegram"]
        state.slack_client.post_message.assert_awaited_once()
        tp.send_message.assert_awaited_once()


class TestTrackedChannelRevokedDuringGovernanceRecheck:
    """The governance re-check awaits, so the approval read before it goes stale.

    Closing the earlier governance window put a second await AFTER the recipient
    approval, which made the approval the stale one: untracking the channel during
    that await left a verdict read before it still standing, and the note posted to
    a conversation whose authorization had been withdrawn. Adding a third await
    would only move the window one layer down, so the last word before the send has
    to be SYNCHRONOUS.
    """

    def _capture_denials(self, monkeypatch) -> list[str]:
        denials: list[str] = []

        class _Sel:
            def log_api_access(self, **kw):
                if kw.get("outcome") == "denied":
                    denials.append(str(kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())
        return denials

    @pytest.mark.asyncio
    async def test_untracking_during_the_governance_recheck_await_refuses(
        self, tmp_path, monkeypatch
    ) -> None:
        import kiro_crew.slack.handler as h

        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")
        denials = self._capture_denials(monkeypatch)
        calls = {"n": 0}

        def _gate(*_a, **_k):
            calls["n"] += 1
            if calls["n"] >= 2:
                # INSIDE the governance re-check await -- strictly after the
                # tracked-channel approval was read, and before the send.
                h._tracking_channels = set()
            return True

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.channel_egress_permitted", _gate)

        delivered = await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert calls["n"] >= 2, "the governance re-check never ran, so this proved nothing"
        assert delivered == [], "a note posted to a channel untracked mid-flight"
        state.slack_client.post_message.assert_not_awaited()
        assert any(
            "reason=recipient_authorization_revoked_before_part_1" in d for d in denials
        ), f"the mid-flight revocation was not audited; denials={denials}"

    @pytest.mark.asyncio
    async def test_the_same_path_delivers_when_tracking_is_never_revoked(
        self, tmp_path, monkeypatch
    ) -> None:
        """The positive half -- without it the refusal above could pass by breaking
        the tracked-channel basis rather than by catching the revocation."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")
        calls = {"n": 0}

        def _gate(*_a, **_k):
            calls["n"] += 1
            return True

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.channel_egress_permitted", _gate)

        delivered = await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert delivered == ["slack"]
        assert calls["n"] >= 2, "the governance gate was not asked twice on the happy path"
        state.slack_client.post_message.assert_awaited_once()


class TestTransportRetargetAcrossDispatch:
    """The transport leg must not deliver into a binding it was not authored for.

    The Slack leg was hardened with a pre-dispatch snapshot; this leg was not, and
    the shared ladder's own revalidation compares against the walk it performs
    INSIDE the call -- which, for a background task, already happens after any
    rebind. So a note accepted for channel A was delivered to channel B.
    """

    def _bound(self, state, channel_type, channel_id, *, origin=False):
        tp = _real_caps_transport(channel_type)
        state.register_channel_transport(tp)
        link = ChannelLink(channel_type, channel_id=channel_id, thread_id=None)
        if origin:
            state.sessions.get_origin_link = MagicMock(return_value=link)
        else:
            state.sessions.get_mirror_link = MagicMock(return_value=link)
        return tp

    @pytest.mark.asyncio
    async def test_a_rebind_before_the_task_runs_refuses_rather_than_retargets(
        self, tmp_path
    ) -> None:
        """Authored for A, rebound to B before the task runs -> refuse, not deliver."""
        from kiro_crew.dashboard.handlers.messaging import snapshot_channel_link

        state = _make_state(tmp_path)
        state.slack_client = None
        state.sessions.get_origin_link = MagicMock(return_value=None)
        tp_a = self._bound(state, "telegram", "A1")
        tp_b = _real_caps_transport("discord")
        state.register_channel_transport(tp_b)
        slot = _slot(state)

        # 1. Caller side of the dispatch boundary: authored for telegram/A1.
        authored = snapshot_channel_link(state, "dashboard:chat-1", skip_paused=True)
        assert authored is not None and authored[0].channel_id == "A1"

        # 2. The rebind lands in the gap a background task opens.
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="B2", thread_id=None)
        )

        # 3. The task finally runs.
        await mirror_note_to_channels(
            state,
            slot,
            "dashboard:chat-1",
            "hello",
            "note",
            slack_link=("", ""),
            channel_link=authored,
        )

        assert tp_b.send_message.await_count == 0, (
            "a note authored for telegram/A1 was delivered into the replacement "
            "discord/B2 binding"
        )
        assert tp_a.send_message.await_count == 0, "delivered on a binding that is gone"

    @pytest.mark.asyncio
    async def test_an_unbound_slot_at_authoring_refuses_a_channel_that_binds_later(
        self, tmp_path, monkeypatch
    ) -> None:
        """No binding at POST -> a binding appearing later is NOT a licence to deliver.

        The snapshot for an unbound slot is a legitimately captured ``None``, and
        while that was passed as the same ``None`` that means "no snapshot given",
        the helper fell back to comparing against its OWN walk -- which for a
        background task runs after the bind. So an unbound slot at authoring time
        accepted whatever conversation appeared in the gap, which is delivery to a
        recipient nothing authorized at authoring.
        """
        from kiro_crew.dashboard.handlers.messaging import snapshot_channel_link

        state = _make_state(tmp_path)
        state.slack_client = None
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _slot(state)

        rows: list[str] = []

        class _RecordingSel:
            def log_tool_invocation(self, **kw):
                rows.append(f"{kw.get('outcome', '')} {kw.get('resources', '')}")

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.messaging._sel",
            lambda: _RecordingSel(),
        )

        # 1. Caller side: the slot is unbound, so the capture is a real None.
        authored = snapshot_channel_link(state, "dashboard:chat-1", skip_paused=True)
        assert authored is None, "fixture must start unbound for this to test anything"

        # 2. A channel binds in the gap the background task opens.
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="LATE", thread_id=None)
        )

        # 3. The task runs and must refuse.
        await mirror_note_to_channels(
            state,
            slot,
            "dashboard:chat-1",
            "hello",
            "note",
            slack_link=("", ""),
            channel_link=authored,
        )

        assert tp.send_message.await_count == 0, (
            "a note authored while the slot was UNBOUND was delivered into the "
            "telegram/LATE binding that appeared afterwards"
        )
        denials = [r for r in rows if r.startswith("denied")]
        assert len(denials) == 1, (
            "an unbound slot on an install WITH channel transports must file exactly "
            f"one denial row; got {rows}"
        )
        assert "reason=unbound_at_authoring" in denials[0], (
            "the denial must name the late-binding refusal by its own stable code, "
            f"so it is filterable apart from every other refusal; got {denials[0]}"
        )

    @pytest.mark.asyncio
    async def test_a_slack_only_binding_files_no_denial_on_the_transport_leg(
        self, tmp_path, monkeypatch
    ) -> None:
        """A session bound ONLY to Slack was delivered, so nothing was refused.

        The transport walk runs with ``skip_slack=True``, so for a session whose only
        live binding is its Slack row that walk legitimately finds nothing and captures
        a real ``None``. Reading that ``None`` as "the slot had NO binding at all" is
        false: the note went out on the sibling Slack leg. Auditing it as a denial put
        a refusal row on every ``/note`` for the commonest binding there is, which is
        worse than filing none -- an operator filtering egress denials would have to
        read past one phantom per note to find a real refusal.
        """
        from kiro_crew.dashboard.handlers.messaging import snapshot_channel_link

        state = _make_state(tmp_path)
        # An install that DOES register a non-Slack transport: the empty-registry arm
        # would otherwise return silently and the test would pass without the fix.
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        assert state.channel_transports, "precondition: registry must be populated"

        # Bound to Slack and to nothing else.
        state.slack_client = MagicMock()
        state.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("slack", channel_id="C-ONLY", thread_id="1.1")
        )
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _slot(state)

        rows: list[str] = []

        class _RecordingSel:
            def log_tool_invocation(self, **kw):
                rows.append(f"{kw.get('outcome', '')} {kw.get('resources', '')}")

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.messaging._sel",
            lambda: _RecordingSel(),
        )

        authored = snapshot_channel_link(
            state, "dashboard:chat-1", skip_paused=True, skip_slack=True
        )
        assert authored is None, (
            "precondition: the skip_slack walk must find nothing for a Slack-only "
            "binding, which is the captured None this test is about"
        )

        await mirror_note_to_channels(
            state,
            slot,
            "dashboard:chat-1",
            "hello",
            "note",
            slack_link=("C-ONLY", "1.1"),
            channel_link=authored,
        )

        denials = [r for r in rows if r.startswith("denied")]
        assert not denials, (
            "a Slack-only-bound session filed an egress DENIAL for a note that was "
            "delivered on its Slack leg -- the transport walk skipped the one binding "
            f"it had, so this asserts an absence the session does not have; got {denials}"
        )

    @pytest.mark.asyncio
    async def test_a_paused_origin_still_delivers_to_the_active_mirror(
        self, tmp_path, monkeypatch
    ) -> None:
        """THE REGRESSION CONTROL. An earlier attempt at this hazard read the link
        pause-BLIND and passed a channel TYPE to match; with a paused origin on one
        transport and an active mirror on another it named the origin's type while
        the pause-aware ladder selected the mirror, and the guard then rejected the
        live destination. This goes red if that mistake comes back.
        """
        from kiro_crew.dashboard.handlers.messaging import snapshot_channel_link

        state = _make_state(tmp_path)
        state.slack_client = None
        tp_origin = self._bound(state, "telegram", "ORIG", origin=True)
        tp_mirror = self._bound(state, "discord", "MIRR")
        # Only the ORIGIN row is paused; the mirror is live.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.messaging.mirror_is_paused",
            lambda _s, _k, origin=False: bool(origin),
        )
        slot = _slot(state)

        authored = snapshot_channel_link(state, "dashboard:chat-1", skip_paused=True)
        assert authored is not None, "the pause-aware walk found no destination at all"
        assert authored[0].channel_type == "discord", (
            "the snapshot named the PAUSED origin -- this is the pause-blind read that "
            f"broke before (got {authored[0].channel_type})"
        )

        await mirror_note_to_channels(
            state,
            slot,
            "dashboard:chat-1",
            "hello",
            "note",
            slack_link=("", ""),
            channel_link=authored,
        )

        tp_mirror.send_message.assert_awaited_once()
        assert tp_origin.send_message.await_count == 0, "delivered into the paused origin"

    @pytest.mark.asyncio
    async def test_an_unchanged_binding_still_delivers(self, tmp_path) -> None:
        """The guard must not pass by refusing everything."""
        from kiro_crew.dashboard.handlers.messaging import snapshot_channel_link

        state = _make_state(tmp_path)
        state.slack_client = None
        state.sessions.get_origin_link = MagicMock(return_value=None)
        tp = self._bound(state, "telegram", "A1")
        slot = _slot(state)

        authored = snapshot_channel_link(state, "dashboard:chat-1", skip_paused=True)
        await mirror_note_to_channels(
            state,
            slot,
            "dashboard:chat-1",
            "hello",
            "note",
            slack_link=("", ""),
            channel_link=authored,
        )

        tp.send_message.assert_awaited_once()


class TestBothLegs:
    @pytest.mark.asyncio
    async def test_a_slack_thread_and_a_channel_mirror_both_receive_it(
        self, tmp_path, monkeypatch
    ) -> None:
        """A dashboard slot can hold both bindings, and each has a user in it."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        delivered = await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert delivered == ["slack", "telegram"]
        state.slack_client.post_message.assert_awaited_once()
        tp.send_message.assert_awaited_once()


class TestEndpointReportsDelivery:
    @asynccontextmanager
    async def _endpoint(self, state):
        """Serve the real note endpoint, and DRAIN the mirror it dispatches.

        The handler backgrounds the mirror deliberately, so closing the client is
        only half of teardown: the dispatched task outlives the test as either
        pending work the loop destroys, or an exception nobody ever retrieved.
        Neither shows up as a failure -- the suite still reports green -- so the
        drain lives HERE, in the one place every endpoint test already goes
        through, rather than in a `finally` hand-copied per test where the next
        test added would simply omit it.

        Cancel-then-gather, in that order, because the two leak shapes need
        different halves: cancelling ends a task that would never finish, and
        gathering is what RETRIEVES the exception from one that already failed.
        """
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            yield client
        finally:
            await client.close()
            # Snapshot first: the handler attaches a done-callback that discards
            # the task from this set as it finishes, so iterating it live would
            # mutate it under us. `getattr` because one test deliberately serves
            # a state that carries no registry at all.
            pending = list(getattr(state, "_background_tasks", None) or ())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_an_unbound_immediate_note_still_files_its_denial(
        self, tmp_path, monkeypatch
    ) -> None:
        """An unbound slot on an install WITH transports must audit, not fall silent.

        Three outcomes share this shape and only one is silent by right: no transports at
        all carries nothing per-session, a Slack-only binding was served on the sibling leg,
        and transports present plus a slot unbound when the note was written is a permission
        decision. The dispatch is the only path that reaches that decision's SEL row, so the
        gate has to let an auditable refusal through even though nothing will be delivered.
        """
        rows: list[str] = []

        class _RecordingSel:
            def log_tool_invocation(self, **kw):
                rows.append(f"{kw.get('outcome', '')} {kw.get('resources', '')}")

        state = _slack_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=("", ""))
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.messaging._sel", lambda: _RecordingSel())
        slot = _slot(state)
        assert (
            not slot._slack_thread_ts and not slot._slack_channel
        ), "fixture must start UNBOUND or this asserts nothing"
        transport = _real_caps_transport("telegram")
        state.register_channel_transport(transport)

        async with self._endpoint(state) as client:
            resp = await client.post("/api/chat/slots/s1/note", json={"content": "hello"})
            assert resp.status == 200
            # Awaited rather than polled: the dispatch is backgrounded, and a timed poll
            # would make this assert on wall-clock luck under a loaded run.
            dispatched = list(getattr(state, "_background_tasks", None) or ())
            if dispatched:
                await asyncio.gather(*dispatched, return_exceptions=True)

        assert (
            transport.send_message.await_count == 0
        ), "an unbound slot has no authorized destination, so nothing may be delivered"
        denials = [r for r in rows if r.startswith("denied")]
        assert len(denials) == 1 and "reason=unbound_at_authoring" in denials[0], (
            "an unbound immediate note on an install WITH transports filed no denial row, "
            f"so the refusal is invisible to the audit log; rows={rows}"
        )

    @pytest.mark.asyncio
    async def test_every_unbound_note_files_its_own_denial(self, tmp_path, monkeypatch) -> None:
        """A repeated egress denial must be audited each time, not only the first.

        Suppressing repeats leaves later denials with no SEL record at all, and a denial
        stream that is incomplete by design cannot be read as an absence of denials -- an
        operator counting refusals on a session would see one where three occurred.
        """
        rows: list[str] = []

        class _RecordingSel:
            def log_tool_invocation(self, **kw):
                rows.append(f"{kw.get('outcome', '')} {kw.get('resources', '')}")

        state = _slack_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=("", ""))
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.messaging._sel", lambda: _RecordingSel())
        slot = _slot(state)
        assert (
            not slot._slack_thread_ts and not slot._slack_channel
        ), "fixture must start UNBOUND or this asserts nothing"
        state.register_channel_transport(_real_caps_transport("telegram"))

        async with self._endpoint(state) as client:
            for i in range(3):
                resp = await client.post("/api/chat/slots/s1/note", json={"content": f"note {i}"})
                assert resp.status == 200
                dispatched = list(getattr(state, "_background_tasks", None) or ())
                if dispatched:
                    await asyncio.gather(*dispatched, return_exceptions=True)

        denials = [r for r in rows if r.startswith("denied")]
        assert len(denials) == 3, (
            "three unbound notes filed fewer than three denial rows, so a repeated egress "
            f"refusal is invisible to the audit log; denials={denials} rows={rows}"
        )

    @pytest.mark.asyncio
    async def test_the_channel_still_receives_the_note(self, tmp_path, monkeypatch) -> None:
        """The subtraction removed the REPORT, not the delivery. Without this the
        change could quietly drop the feature the whole PR exists to add."""
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        _slot(state, thread="1785370133.085469", channel="C123")
        async with self._endpoint(state) as client:
            resp = await client.post("/api/chat/slots/s1/note", json={"content": "hello"})
            body = await resp.json()
            # Backgrounded, so let the dispatched task reach its send.
            for _ in range(200):
                if state.slack_client.post_message.await_count:
                    break
                await asyncio.sleep(0.01)

        assert resp.status == 200
        assert body["appended"] is True
        state.slack_client.post_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_post_does_not_wait_for_the_mirror(self, tmp_path, monkeypatch) -> None:
        """A wedged channel must not hold the POST open at all.

        For a note that is NOT held, both halves are committed before the mirror
        runs, so a client that gives up on a slow response and retries writes them
        a SECOND time. Awaiting the mirror -- even under a bound -- put a hung
        transport on this request's critical path; dispatching it in the background
        takes it off.

        The HELD case does not need this protection at all: the dispatch is
        guarded by ``if not deferred``, so while a turn is running nothing is
        mirrored. That closes the orphan-post hole — if the slot acquires a foreign
        binding before
        flush BOTH halves are dropped rather than retargeted, so the channel
        could otherwise carry a note the transcript never recorded. See
        ``test_a_held_note_does_not_reach_the_channel``. Mirroring at flush time,
        once the halves land, remains the enhancement.
        """
        state = _make_state(tmp_path)
        state.slack_client = None
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        _slot(state)

        async def _never_returns(*_a, **_k):
            await asyncio.sleep(3600)

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_note_mirror.mirror_note_to_channels", _never_returns
        )
        async with self._endpoint(state) as client:
            # The bound is on the REQUEST, not on the mirror: if the handler awaits
            # the mirror at all this raises, which is the pre-fix failure.
            resp = await asyncio.wait_for(
                client.post("/api/chat/slots/s1/note", json={"content": "hello"}),
                timeout=5.0,
            )
            body = await resp.json()

        assert resp.status == 200
        assert body["appended"] is True, "the note itself must still be written"

    @pytest.mark.asyncio
    async def test_a_relink_before_the_task_runs_does_not_retarget_the_note(
        self, tmp_path, monkeypatch
    ) -> None:
        """A note authored for thread A must never surface in thread B.

        Reproduces the dispatch boundary explicitly rather than racing it: snapshot
        the coordinates as the handler does, THEN relink the session, THEN run the
        mirror -- which is precisely the interleaving a background task allows. A
        late link lookup would resolve the REPLACEMENT thread here and deliver a
        note into a conversation it was never authorized for.

        Asserts on the DELIVERED RECIPIENT, not on a helper having been consulted.
        The correct outcome is no delivery at all: the live link does not match
        the snapshot, and posting to the new thread is the exposure while posting to
        the old one may be equally wrong now the session has moved. The note is not
        swallowed -- its transcript and context halves are written by the endpoint
        regardless, and the refusal emits a `denied` SEL record.
        """
        state = _slack_state(tmp_path, thread="ts-A", channel="C_A")
        _authorize_slack_recipient(monkeypatch, "C_A", "C_B")
        slot = _slot(state, thread="ts-A", channel="C_A")
        denials: list[str] = []

        class _Sel:
            def log_api_access(self, **kw):
                if kw.get("outcome") == "denied":
                    denials.append(str(kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())

        # 1. The caller's side of the dispatch boundary.
        snapshot = _snapshot_slack_link(slot, state, "dashboard:chat-1")
        assert snapshot == ("ts-A", "C_A"), "precondition: the note is authored for thread A"

        # 2. The relink lands in the gap a background task opens.
        slot._slack_thread_ts = "ts-B"
        slot._slack_channel = "C_B"
        state.sessions.get_slack_link = MagicMock(return_value=("ts-B", "C_B"))

        # 3. The task finally runs.
        await mirror_note_to_channels(
            state,
            slot,
            "dashboard:chat-1",
            "hello",
            "note",
            slack_link=snapshot,
            channel_link=None,
        )

        recipients = [c.args[0] for c in state.slack_client.post_message.await_args_list]
        assert "C_B" not in recipients, (
            "a note authored for C_A was delivered into the replacement thread C_B "
            f"(recipients={recipients})"
        )
        assert recipients == [], f"a stale-coordinate note was delivered at all: {recipients}"
        assert any(
            "reason=link_changed_during_recheck_before_part_1" in d for d in denials
        ), f"the refusal was not surfaced in the SEL; denials={denials}"

    def test_the_snapshot_prefers_the_persisted_link_over_stale_slot_fields(
        self, tmp_path, monkeypatch
    ) -> None:
        """A complete PERSISTED link wins over slot attributes naming an older thread.

        Several writers persist a new Slack link WITHOUT touching the slot fields --
        ``slack.handler._on_applied`` at the privacy-mode apply, the interactions
        handler, and the runner's relink -- so a slot bound earlier keeps STALE
        attributes while the map already names the replacement. Reading the slot
        first posted the note into the conversation the session had moved off.

        The reverse cannot happen (``link_slack`` writes both surfaces and restore
        hydrates the slot FROM the map), which is why preferring the map is safe
        rather than merely a different guess.
        """
        state = _slack_state(tmp_path, thread="ts-B", channel="C_B")
        # The slot still carries the SUPERSEDED binding.
        slot = _slot(state, thread="ts-A", channel="C_A")

        assert _snapshot_slack_link(slot, state, "dashboard:chat-1") == (
            "ts-B",
            "C_B",
        ), "a complete persisted link must win over stale slot fields"

    def test_an_incomplete_persisted_link_is_unbound_not_a_slot_fallback(
        self, tmp_path, monkeypatch
    ) -> None:
        """With a session store present, a partial persisted row means UNBOUND.

        An earlier revision fell back to the slot fields here, for a dashboard slot
        linked in-process before its row is written. That fallback could not tell
        "not written yet" from "cleared or reassigned", so clearing the row
        resurrected the superseded slot binding and the note went to the conversation
        the session had moved off. Refusing costs that one in-process note.

        Paired with the slot-store case below so this cannot be satisfied by a
        function that returns empty for everything.
        """
        state = _slack_state(tmp_path, thread="", channel="")
        slot = _slot(state, thread="ts-A", channel="C_A")

        assert _snapshot_slack_link(slot, state, "dashboard:chat-1") == ("", ""), (
            "an incomplete persisted link fell back to stale slot fields, which is "
            "how a cleared binding resurrects the conversation the session left"
        )

    def test_slot_fields_are_the_binding_when_there_is_no_session_store(self, tmp_path) -> None:
        """No store means the slot fields are the only binding, not the older of two."""
        state = _slack_state(tmp_path, thread="", channel="")
        state.sessions = None
        slot = _slot(state, thread="ts-A", channel="C_A")

        assert _snapshot_slack_link(slot, state, "dashboard:chat-1") == ("ts-A", "C_A"), (
            "with no session store the slot fields are the whole truth, so refusing "
            "here would strand every install that has no persisted map"
        )

    @pytest.mark.asyncio
    async def test_a_state_without_a_task_registry_does_not_500_the_note(self, tmp_path) -> None:
        """The DISPATCH must not be able to fail this POST either.

        For a note that is NOT held, both halves are committed before the mirror is
        dispatched, so any raise from the dispatch 500s a request whose work is done
        and a client retry writes them twice. A partially-constructed state
        (`DashboardState.__new__`, which fixtures across the suite use) carries no
        `_background_tasks`, so registering the task unguarded is enough to break
        that contract -- which is exactly the regression this pins. A HELD note
        never reaches this dispatch at all now that it is guarded by
        ``if not deferred``: see ``test_a_held_note_does_not_reach_the_channel``.
        """
        state = _make_state(tmp_path)
        state.slack_client = None
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _slot(state)
        # Reproduce the fixture shape that broke it, and ASSERT that shape, so a
        # future fixture that happens to set a registry cannot make this pass
        # vacuously while testing nothing.
        state.__dict__.pop("_background_tasks", None)
        assert not hasattr(
            state, "_background_tasks"
        ), "precondition: this test is only meaningful on a state with NO task registry"
        async with self._endpoint(state) as client:
            resp = await client.post("/api/chat/slots/s1/note", json={"content": "hello"})
            body = await resp.json()

        assert resp.status == 200, "a state without a task registry 500'd an already-written note"
        assert body["appended"] is True
        assert any(m.get("cls") == "reconcile-note" for m in slot.messages)
        assert len(slot._pending_context) == 1

    @pytest.mark.asyncio
    async def test_a_mirror_failure_does_not_500_an_already_committed_note(
        self, tmp_path, monkeypatch
    ) -> None:
        """Both halves are written before the mirror runs, so a raise here would
        return 500 on a POST that already committed them, and the caller's retry
        would append and queue them a SECOND time."""
        state = _make_state(tmp_path)
        state.slack_client = None
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _slot(state)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_note_mirror.mirror_note_to_channels",
            AsyncMock(side_effect=RuntimeError("invalid platform composition")),
        )
        async with self._endpoint(state) as client:
            resp = await client.post("/api/chat/slots/s1/note", json={"content": "hello"})
            body = await resp.json()

        assert resp.status == 200, "a best-effort mirror failure became a 500"
        assert body["appended"] is True
        assert any(m.get("cls") == "reconcile-note" for m in slot.messages)
        assert len(slot._pending_context) == 1

    @pytest.mark.asyncio
    async def test_a_held_note_does_not_reach_the_channel(self, tmp_path, monkeypatch) -> None:
        """A HELD note must NOT be mirrored: neither half is committed yet.

        This inverts an earlier assertion that a held note SHOULD still deliver,
        on the reasoning that the hold protects transcript replay accounting and a
        channel post cannot affect it. That traded a lost best-effort delivery for
        a worse failure. While held, the visible write is skipped and the context
        sits in ``_deferred_notes``, so a foreign rebind before the flush drops
        BOTH halves — and a mirror dispatched at POST would already have published
        a channel note asserting content the session never received. An orphan post
        is a data-integrity defect; a dropped best-effort mirror is not.

        The endpoint still reports the hold honestly (``visibleDeferred`` true,
        ``appended`` false, ``deliveryConditional`` true), so no caller is told the
        note was delivered. The mirror is deferred WITH the halves, not lost:
        ``TestAHeldNoteMirrorsAtFlush`` pins that the flush dispatches it once they
        land, which is why the drain below is a real wait -- nothing may arrive
        before that flush.
        """
        state = _slack_state(tmp_path)
        _authorize_slack_recipient(monkeypatch, "C123")
        slot = _slot(state, thread="1785370133.085469", channel="C123")
        # The flag the deferred-arm tests already use: ``running`` is a derived
        # property with no setter, and both are read by the same guard.
        slot._in_stage_execution = True
        async with self._endpoint(state) as client:
            body = await (
                await client.post("/api/chat/slots/s1/note", json={"content": "hello"})
            ).json()
            # Drain generously: a PASSING assertion here is an ABSENCE, so the wait
            # has to be long enough that "nothing arrived" cannot just mean "not
            # yet". This is the same 2s budget the positive tests use to observe a
            # send, so it is proven sufficient for one to land if one were coming.
            for _ in range(200):
                if state.slack_client.post_message.await_count:
                    break
                await asyncio.sleep(0.01)

        assert body["visibleDeferred"] is True
        assert body["appended"] is False
        assert body["deliveryConditional"] is True
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_dispatched_mirror_task_outlives_the_endpoint(
        self, tmp_path, monkeypatch
    ) -> None:
        """A backgrounded mirror must not survive the test that dispatched it.

        The leak is INVISIBLE to an ordinary assertion: a task still pending when
        the loop goes away only ever produced a "Task was destroyed but it is
        pending!" line on stderr while the suite reported green, so nothing failed
        and the next endpoint test inherited the same hole. This asserts the
        property directly instead -- capture the task the handler registered, then
        require it finished once the helper's drain has run.

        Uses a mirror that never returns on purpose: with a fast one the task
        completes by itself and the assertion would hold whether or not anything
        drained it, which is the shape that would pass vacuously.

        The slot is BOUND for the same reason: the dispatch is gated on a destination
        existing, so a destination-less slot registers no task and there is nothing to
        leak -- the precondition below is what keeps that from reading as a pass.
        """
        state = _slack_state(tmp_path)
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        _slot(state, thread="1785370133.085469", channel="C123")

        async def _never_returns(*_a, **_k):
            await asyncio.sleep(3600)

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_note_mirror.mirror_note_to_channels", _never_returns
        )

        dispatched: list[asyncio.Task] = []
        async with self._endpoint(state) as client:
            await client.post("/api/chat/slots/s1/note", json={"content": "hello"})
            for _ in range(200):
                if getattr(state, "_background_tasks", None):
                    dispatched = list(state._background_tasks)
                    break
                await asyncio.sleep(0.01)

        assert dispatched, "precondition: the handler registered no background task to leak"
        assert all(t.done() for t in dispatched), (
            "a dispatched mirror task was still live after the endpoint helper "
            f"returned: {[t for t in dispatched if not t.done()]}"
        )


class TestChunkCountIsProjectedOnTheSentForm:
    """A leg's bound comes from the text that leg actually chunks, not the raw note."""

    @staticmethod
    async def _slack_timeout_for(mod, monkeypatch, tmp_path, text):
        """The ``timeout`` the Slack leg is handed for *text*."""
        seen: dict[str, float | None] = {}

        async def _capture(leg, key, coro, **kwargs):
            seen[leg] = kwargs.get("timeout")
            coro.close()

        monkeypatch.setattr(mod, "_run_leg", _capture)
        state = _slack_state(tmp_path, thread="1.1", channel="C1")
        slot = _slot(state, thread="1.1", channel="C1")
        await mod.mirror_note_to_channels(
            state,
            slot,
            "dashboard:chat-1",
            text,
            "note",
            slack_link=("1.1", "C1"),
            channel_link=None,
        )
        return seen

    @pytest.mark.asyncio
    async def test_a_note_that_defangs_past_the_cap_is_bounded_for_two_chunks(
        self, tmp_path, monkeypatch
    ):
        """Under-counting chunks starves the bound: it grants one chunk's grace for two.

        ``display_safe`` inserts a zero-width space per ``@`` before the leg chunks, so
        a note just inside the cap crosses it on defang and sends two parts.
        """
        from kiro_crew.dashboard import chat_note_mirror as mod
        from kiro_crew.messaging.display_safety import redact_for_display  # noqa: I001
        from kiro_crew.messaging.link import SLACK_NAMESPACE
        from kiro_crew.messaging.renderer import display_safe
        from kiro_crew.platform import redact_via_context

        def _derived(content):
            """The text the mirror projects from, label and redaction included."""
            out, _ = redact_for_display(mod._note_channel_text(content, "note"), redact_via_context)
            return out

        div = mod._FALLBACK_CHUNK_CHARS
        mentions = 5
        overhead = len(_derived(""))
        # Land len(text) EXACTLY on a divisor multiple, so the extra chunk comes from
        # defanging rather than from the raw length already spanning the boundary.
        content = "@" * mentions + "x" * (div * 2 - mentions - overhead)
        text = _derived(content)
        assert (
            len(text) == div * 2
        ), f"band precondition: len(text)={len(text)} must be exactly {div * 2}"

        seen = await self._slack_timeout_for(mod, monkeypatch, tmp_path, content)

        # The invariant is unchanged: the bound must be projected on the DEFANGED form,
        # which sends strictly more chunks than raw at the budget divisor.
        raw_parts = mod._leg_parts(text, mod._FALLBACK_CHUNK_CHARS)
        sent_parts = mod._leg_parts(display_safe(text), mod._FALLBACK_CHUNK_CHARS)
        assert sent_parts > raw_parts, (
            f"band precondition: defanging must add a chunk at the budget divisor "
            f"(raw={raw_parts}, defanged={sent_parts})"
        )
        assert (
            SLACK_NAMESPACE in seen
        ), f"the Slack leg never ran, so this test cannot see its bound; keys={list(seen)}"
        assert seen[SLACK_NAMESPACE] == pytest.approx(mod._CHUNK_TIMEOUT_S * sent_parts), (
            f"the Slack leg was handed {seen[SLACK_NAMESPACE]!r}s for a note projecting "
            f"{sent_parts} chunks; it must get {mod._CHUNK_TIMEOUT_S * sent_parts}s, one "
            "chunk's grace per chunk. Budgeting the RAW text would grant "
            f"{mod._CHUNK_TIMEOUT_S * raw_parts}s and expire with parts unsent, the "
            "reader seeing a truncated note as complete"
        )

    @pytest.mark.asyncio
    async def test_a_genuinely_single_chunk_note_keeps_its_deadline(self, tmp_path, monkeypatch):
        """The bound must survive where it cannot truncate, or the fix is a removal."""
        from kiro_crew.dashboard import chat_note_mirror as mod
        from kiro_crew.messaging.link import SLACK_NAMESPACE

        seen = await self._slack_timeout_for(mod, monkeypatch, tmp_path, "x" * 100)

        assert (
            SLACK_NAMESPACE in seen
        ), f"the Slack leg never ran, so this control proves nothing; keys={list(seen)}"
        assert seen[SLACK_NAMESPACE] == pytest.approx(mod._CHUNK_TIMEOUT_S), (
            "a one-chunk leg lost its bound; cancelling it delivers nothing, which is "
            f"the honest failure mode this module keeps (got {seen[SLACK_NAMESPACE]!r})"
        )


class TestAStalledMultiChunkLegCannotStayLiveForever:
    """A leg that stops answering must end, so its sibling runs and its task is freed.

    The unbounded arm was chosen to avoid truncating a chunk loop mid-send. But the
    two helpers a leg calls ALREADY abort mid-send and leave a delivered prefix when
    a binding is revoked, each saying so in its own comment -- so a partial note is
    behaviour this chain already accepts, while a leg that never returns is not. It
    holds its background task for the life of the process, and because the legs run
    SEQUENTIALLY it also means the healthy leg is never reached at all.
    """

    @pytest.mark.asyncio
    async def test_a_stalled_multi_chunk_leg_does_not_hang_the_mirror(self, tmp_path, monkeypatch):
        """The mirror must RETURN even when a multi-chunk leg never answers.

        Returning is what frees the entry in ``_background_tasks``: the dispatcher
        adds a done-callback that discards it, so a task that never completes is
        never discarded. Asserting the coroutine terminates is therefore the same
        assertion as "the task does not remain live", reachable without a dispatcher.
        """
        from kiro_crew.dashboard import chat_note_mirror as mod
        from kiro_crew.slack.format import SLACK_MSG_LIMIT

        stalled = asyncio.Event()

        async def _never_answers(*_args, **_kwargs):
            stalled.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(mod, "_deliver_slack", _never_answers)
        # Small enough that the bound fires inside the window and stays under the ceiling.
        monkeypatch.setattr(mod, "_CHUNK_TIMEOUT_S", 0.05)

        content = "x" * (SLACK_MSG_LIMIT * 2 + 500)
        state = _slack_state(tmp_path, thread="1.1", channel="C1")
        slot = _slot(state, thread="1.1", channel="C1")

        parts = mod._leg_parts(content, SLACK_MSG_LIMIT)
        assert parts > 1, f"precondition: the note must be multi-chunk, got {parts}"

        try:
            await asyncio.wait_for(
                mod.mirror_note_to_channels(
                    state,
                    slot,
                    "dashboard:chat-1",
                    content,
                    "note",
                    slack_link=("1.1", "C1"),
                    channel_link=None,
                ),
                timeout=5,
            )
        except asyncio.TimeoutError:  # pragma: no cover - the defect this test pins
            raise AssertionError(
                f"the mirror never returned: a stalled {parts}-chunk leg was handed no "
                "deadline, so it stays live for the life of the process, holds its "
                "background task, and blocks the sibling leg that runs after it"
            ) from None

        assert stalled.is_set(), "the Slack leg never ran, so this test proves nothing"

    @pytest.mark.asyncio
    async def test_a_healthy_multi_chunk_note_still_delivers_every_part(
        self, tmp_path, monkeypatch
    ):
        """Negative control: the bound must not truncate a leg that IS answering.

        Without this, cutting the leg short would satisfy the test above while
        losing the delivery the chunking exists to complete.
        """
        from kiro_crew.dashboard import chat_note_mirror as mod
        from kiro_crew.slack.format import SLACK_MSG_LIMIT

        ran: list[str] = []

        async def _answers(*_args, **_kwargs):
            ran.append("slack")

        monkeypatch.setattr(mod, "_deliver_slack", _answers)

        content = "x" * (SLACK_MSG_LIMIT * 2 + 500)
        state = _slack_state(tmp_path, thread="1.1", channel="C1")
        slot = _slot(state, thread="1.1", channel="C1")

        await asyncio.wait_for(
            mod.mirror_note_to_channels(
                state,
                slot,
                "dashboard:chat-1",
                content,
                "note",
                slack_link=("1.1", "C1"),
                channel_link=None,
            ),
            timeout=5,
        )

        assert ran == ["slack"], f"the healthy leg was not delivered once: {ran}"


class TestEverySlackSendLegFilesATerminalSelRow:
    """Every terminal outcome of a Slack send must leave exactly one SEL row.

    Refusals are not the only terminal outcome: success and both delivery failures
    must each file a row too, not a logger line alone. An operator asking "did this
    note reach Slack" otherwise has to infer the answer from the ABSENCE of a denial,
    which is indistinguishable from a note that was never written. The transport leg
    already files all three (``completed``/``delivered``, ``error``/``transport_error``,
    ``error``/``empty_message_id``); these are its Slack analogues.
    """

    @staticmethod
    def _capture(monkeypatch) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []

        class _Sel:
            def log_api_access(self, **kw):
                rows.append((kw.get("outcome", ""), kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())
        return rows

    @pytest.mark.asyncio
    async def test_a_confirmed_send_files_a_completed_row(self, tmp_path, monkeypatch) -> None:
        rows = self._capture(monkeypatch)
        _authorize_slack_recipient(monkeypatch, "C123")
        state = _slack_state(tmp_path)
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        done = [r for r in rows if r[0] == "completed"]
        assert len(done) == 1, f"a delivered note filed no single completed row: {rows}"
        assert "reason=delivered" in done[0][1], (
            "the success row needs its own stable reason code so an operator can "
            f"filter deliveries apart from refusals; got {done[0][1]!r}"
        )

    @pytest.mark.asyncio
    async def test_a_raising_send_files_an_error_row(self, tmp_path, monkeypatch) -> None:
        rows = self._capture(monkeypatch)
        _authorize_slack_recipient(monkeypatch, "C123")
        state = _slack_state(tmp_path)
        state.slack_client.post_message = AsyncMock(side_effect=RuntimeError("slack down"))
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []

        errors = [r for r in rows if r[0] == "error" and "reason=slack_error" in r[1]]
        assert len(errors) == 1, f"a failed send filed no error row: {rows}"

    @pytest.mark.asyncio
    async def test_an_unconfirmed_send_files_an_error_row(self, tmp_path, monkeypatch) -> None:
        rows = self._capture(monkeypatch)
        _authorize_slack_recipient(monkeypatch, "C123")
        state = _slack_state(tmp_path)
        state.slack_client.post_message = AsyncMock(return_value="")
        slot = _slot(state, thread="1785370133.085469", channel="C123")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == []

        errors = [r for r in rows if r[0] == "error" and "reason=empty_ts" in r[1]]
        assert len(errors) == 1, (
            "an empty ts is Slack's refusal shape and must be auditable apart from a "
            f"raise, so it carries its own reason code; got {rows}"
        )

    @pytest.mark.asyncio
    async def test_no_leg_returns_without_a_terminal_row(self, tmp_path, monkeypatch) -> None:
        """The invariant behind the three cases: never zero terminal rows."""
        terminal = {"completed", "error", "denied"}
        for label, client in (
            ("ok", AsyncMock(return_value="1.2")),
            ("raise", AsyncMock(side_effect=RuntimeError("x"))),
            ("empty", AsyncMock(return_value="")),
        ):
            rows = self._capture(monkeypatch)
            _authorize_slack_recipient(monkeypatch, "C123")
            state = _slack_state(tmp_path)
            state.slack_client.post_message = client
            slot = _slot(state, thread="1785370133.085469", channel="C123")

            await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

            assert [r for r in rows if r[0] in terminal], (
                f"the {label} leg returned with no terminal SEL row, so this outcome "
                "is invisible to an operator auditing the egress"
            )


class TestASlackOriginDoesNotStrandTheMirror:
    """A Slack ORIGIN must not withhold the note from an active non-Slack mirror.

    Slack never registers into ``channel_transports``, so this ladder cannot deliver
    it and the note's Slack half travels on its own leg. Returning the Slack origin
    here handed the transport leg a link it could not resolve, and the mirror that
    COULD have received the note was never reached. The skip is OPT-IN, so a caller
    that does not ask for it keeps the base first-live-row behaviour and no other
    sender's delivery set moves with this change.
    """

    def test_a_slack_origin_is_skipped_for_the_live_mirror(self, tmp_path) -> None:
        from kiro_crew.dashboard.handlers.messaging import snapshot_channel_link
        from kiro_crew.messaging.link import ChannelLink

        state = _make_state(tmp_path)
        slack_origin = ChannelLink(channel_type="slack", channel_id="C1", thread_id="1.1")
        telegram_mirror = ChannelLink(channel_type="telegram", channel_id="T9", thread_id=None)
        state.sessions.get_origin_link = lambda _k: slack_origin
        state.sessions.get_mirror_link = lambda _k: telegram_mirror

        walked = snapshot_channel_link(state, "dashboard:chat-1", skip_paused=True, skip_slack=True)

        assert walked is not None, (
            "the walk returned nothing with a live telegram mirror present, so the note "
            "reaches no channel at all"
        )
        link, is_origin = walked
        assert link.channel_type == "telegram", (
            "the walk stopped at the Slack origin, so the transport leg gets a link this "
            f"ladder cannot resolve and the telegram mirror is never delivered to; got "
            f"{link.channel_type!r}"
        )
        assert (
            is_origin is False
        ), f"the telegram row is the MIRROR, not the origin; got is_origin={is_origin!r}"

    def test_the_skip_is_opt_in_so_other_callers_keep_base_behaviour(self, tmp_path) -> None:
        from kiro_crew.dashboard.handlers.messaging import snapshot_channel_link
        from kiro_crew.messaging.link import ChannelLink

        state = _make_state(tmp_path)
        state.sessions.get_origin_link = lambda _k: ChannelLink(
            channel_type="slack", channel_id="C1", thread_id="1.1"
        )
        state.sessions.get_mirror_link = lambda _k: ChannelLink(
            channel_type="telegram", channel_id="T9", thread_id=None
        )

        walked = snapshot_channel_link(state, "dashboard:chat-1", skip_paused=True)

        assert walked is not None, "the default walk still returns the first live row"
        assert walked[0].channel_type == "slack", (
            "without skip_slack the walk must stop on the first live row exactly as the "
            "base ladder did, so the compaction notice and the shared send helper keep "
            f"their delivery sets; got {walked[0].channel_type!r}"
        )

    def test_a_slack_only_session_still_walks_to_nothing(self, tmp_path) -> None:
        from kiro_crew.dashboard.handlers.messaging import snapshot_channel_link
        from kiro_crew.messaging.link import ChannelLink

        state = _make_state(tmp_path)
        state.sessions.get_origin_link = lambda _k: ChannelLink(
            channel_type="slack", channel_id="C1", thread_id="1.1"
        )
        state.sessions.get_mirror_link = lambda _k: None

        assert (
            snapshot_channel_link(state, "dashboard:chat-1", skip_paused=True, skip_slack=True)
            is None
        ), (
            "a Slack-only session has no transport-deliverable link, so the walk must "
            "return None and let the Slack leg carry it alone"
        )


class TestARaisingLegFilesATerminalSelRow:
    """A raise is terminal too, so it must leave a row like the stall arm does.

    `_run_leg` absorbs a raise so the sibling leg still runs, and that absorption must
    file a row rather than a logger line alone, matching the TimeoutError sibling. A
    governance composition fault propagates out of the chain before any SEL write of its
    own, so a logger-only arm would leave a refused Slack leg with no denial or error row
    at all while the transport leg audits the same class of ending.
    """

    @pytest.mark.asyncio
    async def test_a_raising_slack_leg_files_an_error_row(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_note_mirror as mod

        rows: list[tuple[str, str]] = []

        class _Sel:
            def log_api_access(self, **kw):
                rows.append((kw.get("outcome", ""), kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())

        async def _raises(*_a, **_k):
            raise RuntimeError("malformed governance ceiling")

        monkeypatch.setattr(mod, "_deliver_slack", _raises)
        monkeypatch.setattr(mod, "_deliver_via_transport", _raises)
        state = _slack_state(tmp_path, thread="1.1", channel="C1")
        slot = _slot(state, thread="1.1", channel="C1")

        await mod.mirror_note_to_channels(
            state,
            slot,
            "dashboard:chat-1",
            "hi",
            "note",
            slack_link=("1.1", "C1"),
            channel_link=None,
        )

        errors = [r for r in rows if "reason=leg_error" in r[1]]
        assert errors, (
            "a raising Slack leg filed no SEL row, so a refusal that propagated as an "
            f"exception is invisible to an operator filtering the stream; rows={rows}"
        )
        assert errors[0][0] == "error", (
            "a raise is an error outcome, not a denial -- the chain did not refuse, it "
            f"faulted; got {errors[0][0]!r}"
        )


class TestATimedOutLegFilesATerminalSelRow:
    """A stall is a terminal outcome, so it must leave an SEL row like the others.

    `_run_leg` absorbs a stall so the sibling leg still runs, and that absorption must
    file a row rather than a logger line alone. A logger-only arm makes the timeout the
    ONE ending with no trace in the audit stream, while the module's own dispatch comment
    claims both legs record their terminal outcome to the SEL -- so an operator filtering
    the stream for a note that never arrived finds nothing at all.
    """

    @pytest.mark.asyncio
    async def test_a_stalled_slack_leg_files_an_error_row(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_note_mirror as mod

        rows: list[tuple[str, str]] = []

        class _Sel:
            def log_api_access(self, **kw):
                rows.append((kw.get("outcome", ""), kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.slack_egress.sel", lambda: _Sel())
        monkeypatch.setattr(mod, "_CHUNK_TIMEOUT_S", 0.01)

        async def _never_returns(*_a, **_k):
            await asyncio.sleep(30)
            return True

        monkeypatch.setattr(mod, "_deliver_slack", _never_returns)
        monkeypatch.setattr(mod, "_deliver_via_transport", _never_returns)
        state = _slack_state(tmp_path, thread="1.1", channel="C1")
        slot = _slot(state, thread="1.1", channel="C1")

        await asyncio.wait_for(
            mod.mirror_note_to_channels(
                state,
                slot,
                "dashboard:chat-1",
                "hi",
                "note",
                slack_link=("1.1", "C1"),
                channel_link=None,
            ),
            timeout=5,
        )

        timeouts = [r for r in rows if "reason=leg_timeout" in r[1]]
        assert timeouts, (
            "a stalled Slack leg filed no SEL row, so a note that never arrived is "
            f"invisible to an operator filtering the egress stream; rows={rows}"
        )
        assert timeouts[0][0] == "error", (
            "a stall is an error outcome, not a denial -- nothing refused this send; "
            f"got {timeouts[0][0]!r}"
        )

    @pytest.mark.asyncio
    async def test_a_stalled_transport_leg_files_its_row_on_its_own_stream(
        self, tmp_path, monkeypatch
    ) -> None:
        """The transport leg's row belongs in the TRANSPORT stream, not Slack's.

        Filing it through the Slack emitter would put a channel-transport decision
        under the Slack namespace, where an operator auditing this leg will not look.
        """
        from kiro_crew.dashboard import chat_note_mirror as mod

        rows: list[tuple[str, str]] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                rows.append((kw.get("outcome", ""), kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.handlers.messaging._sel", lambda: _Sel())
        monkeypatch.setattr(mod, "_CHUNK_TIMEOUT_S", 0.01)

        async def _never_returns(*_a, **_k):
            await asyncio.sleep(30)
            return True

        monkeypatch.setattr(mod, "_deliver_slack", _never_returns)
        monkeypatch.setattr(mod, "_deliver_via_transport", _never_returns)
        state = _make_state(tmp_path)
        state.slack_client = None
        tp = _real_caps_transport("telegram")
        state.register_channel_transport(tp)
        link = ChannelLink("telegram", channel_id="123", thread_id=None)
        slot = _slot(state)

        await asyncio.wait_for(
            mod.mirror_note_to_channels(
                state,
                slot,
                "dashboard:chat-1",
                "hi",
                "note",
                slack_link=("", ""),
                channel_link=(link, True),
            ),
            timeout=5,
        )

        timeouts = [r for r in rows if "reason=leg_timeout" in r[1]]
        assert timeouts, (
            "a stalled transport leg filed no row on the transport stream; " f"rows={rows}"
        )
        assert "channel_type=telegram" in timeouts[0][1], (
            "the row must name the channel type so an operator can tell WHICH "
            f"transport stalled; got {timeouts[0][1]!r}"
        )

    def test_run_leg_has_no_arm_that_disables_the_deadline(self):
        """The subtraction: `timeout` is `float`, so no call can opt out of a bound."""
        import inspect

        from kiro_crew.dashboard import chat_note_mirror as mod

        sig = inspect.signature(mod._run_leg)
        assert sig.parameters["timeout"].annotation == "float", (
            "an optional timeout re-admits the unbounded arm, and a leg that never "
            f"returns keeps its task for the process lifetime; got "
            f"{sig.parameters['timeout'].annotation!r}"
        )
        assert "audit_timeout" in sig.parameters, (
            "the timeout audit must stay a required parameter: a default would let a "
            "new call site drop the row silently"
        )


class TestTheTransportDenialRowIsNotADeploymentWideFlood:
    """The unbound-slot refusal is audited, EXCEPT on an install with no channel surface.

    The exemption is what keeps the row from being a deployment-wide flood: with an
    empty registry the refusal is about the INSTALL rather than the request, so it would
    fire for every note on every session while naming no destination. Where a surface
    DOES exist the refusal is a real permission decision -- it is what stops a binding
    that arrived after authoring from receiving the note -- and an unaudited one would
    be the only denial on this leg with no SEL record. These pin both sides.
    """

    @pytest.mark.asyncio
    async def test_no_transport_configured_files_no_row(self, tmp_path, monkeypatch) -> None:
        rows: list[tuple[str, str]] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                rows.append((kw.get("outcome", ""), kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.handlers.messaging._sel", lambda: _Sel())
        state = _make_state(tmp_path)
        state.slack_client = None
        # Registry empty AND the link lookups None: without the second, `authored_link`
        # is truthy, the branch is never reached, and this test passes vacuously.
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        assert not state.channel_transports, "precondition: registry must be empty"
        slot = _slot(state)

        await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert not [r for r in rows if r[0] == "denied"], (
            "an install with no channel transports filed a per-note denial row, "
            f"which is the deployment-wide flood the Slack leg already exempts; {rows}"
        )

    @pytest.mark.asyncio
    async def test_a_configured_transport_files_the_row(self, tmp_path, monkeypatch) -> None:
        """WITH a channel surface, the unbound-slot refusal files its own denial row.

        This is the discriminating half: it is the same refusal as the test above and
        differs only in whether a transport is registered, so a blanket exemption on
        either side fails exactly one of the two.
        """
        rows: list[tuple[str, str]] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                rows.append((kw.get("outcome", ""), kw.get("resources", "")))

        monkeypatch.setattr("kiro_crew.dashboard.handlers.messaging._sel", lambda: _Sel())
        state = _make_state(tmp_path)
        state.slack_client = None
        state.register_channel_transport(_real_caps_transport("telegram"))
        # A channel surface EXISTS but this slot was never bound to one.
        state.sessions.get_origin_link = MagicMock(return_value=None)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        assert state.channel_transports, "precondition: registry must be populated"
        slot = _slot(state)

        await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        denials = [r for r in rows if r[0] == "denied"]
        assert len(denials) == 1, (
            "an unbound slot on an install WITH a channel surface must file exactly one "
            f"denial row; got {rows}"
        )
        assert "reason=unbound_at_authoring" in denials[0][1], (
            "the row must carry the late-binding refusal's own stable reason code so an "
            f"operator can filter it apart from every other denial; got {denials[0]}"
        )


class TestAHeldNoteMirrorsAtFlush:
    """A note held mid-turn reaches the channel once the flush commits both halves.

    Held is the COMMON case: cron, app and subagent writers land mid-turn, which is
    exactly the producer class this feature exists for. Mirroring at POST would
    publish a line asserting content the session never received, so the dispatch
    waits for the flush -- but it must not be dropped, or the feature covers only
    the rarer immediate case.

    The destinations are snapshotted at AUTHORING time and carried on the held
    record, so a rebind during the hold makes the send REFUSE rather than retarget.
    """

    def test_the_flush_writes_both_halves_and_dispatches_the_mirror(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        state.slack_client = None
        slot = _slot(state)
        fired: list[str] = []
        slot._deferred_notes.append(
            {
                "content": "held body",
                "cls": "reconcile-note",
                "context": {"role": "user", "content": "held ctx"},
                "session": None,
                "mirror": lambda: fired.append("dispatched"),
            }
        )

        written = slot.flush_deferred_notes()

        assert written == 1, f"the flush dropped the held note instead of writing it: {written}"
        assert any(
            m.get("cls") == "reconcile-note" and "held body" in (m.get("content") or "")
            for m in slot.messages
        ), "the visible half never landed at flush, so the note was lost rather than held"
        assert len(slot._pending_context) == 1, "the context half never reached the queue at flush"
        assert fired == ["dispatched"], (
            "the flush committed both halves and did NOT dispatch the mirror, so a held "
            "note still reaches no channel -- the dominant producer case stays invisible"
        )

    def test_a_mirror_failure_at_flush_does_not_lose_the_note(self, tmp_path) -> None:
        """The dispatch is best-effort: a raise must not trigger the suffix restore."""
        state = _make_state(tmp_path)
        state.slack_client = None
        slot = _slot(state)

        def _boom():
            raise RuntimeError("transport exploded")

        slot._deferred_notes.append(
            {
                "content": "held body",
                "cls": "reconcile-note",
                "context": {"role": "user", "content": "held ctx"},
                "session": None,
                "mirror": _boom,
            }
        )

        written = slot.flush_deferred_notes()

        assert written == 1, "a mirror failure was allowed to un-write a committed note"
        assert not slot._deferred_notes, (
            "the note was restored to the held list after its halves had already "
            "committed, so the next flush writes it a SECOND time"
        )

    def test_a_rebound_slot_drops_the_held_note_without_mirroring(self, tmp_path) -> None:
        """The rebind guard runs FIRST: a dropped note must not reach a channel."""
        state = _make_state(tmp_path)
        state.slack_client = None
        slot = _slot(state)
        fired: list[str] = []
        slot._deferred_notes.append(
            {
                "content": "held body",
                "cls": "reconcile-note",
                "context": {"role": "user", "content": "held ctx"},
                "session": "dashboard:someone-else",
                "mirror": lambda: fired.append("dispatched"),
            }
        )

        written = slot.flush_deferred_notes()

        assert written == 0, "a note authorized for another session was written anyway"
        assert fired == [], (
            "a note dropped for a foreign rebind still dispatched its mirror, which "
            "publishes to a destination the note was never authorized for"
        )


class TestAMovedGovernanceCeilingRefusesTheSend:
    """A permit describes the ceiling installed when it was read, not a later one.

    ``_send_time_authorities`` decides the permit and THEN reads the configured
    channel, both inside one worker-thread hop. That ordering closes the two-await
    staleness window but not the window inside the hop: a centrally distributed
    policy can install a new ceiling while the config read runs, and the permit then
    describes a ceiling that is not in force. Nothing downstream catches that,
    because the coordinate check compares the thread and channel and the authority
    check compares the recipient basis; neither re-asks governance.

    Sampling the generation before the permit and re-reading it in the synchronous
    tail turns that window into a refusal. Both tests drive the change
    deterministically from inside the authority read rather than by timing.
    """

    @pytest.mark.asyncio
    async def test_a_ceiling_installed_during_the_authority_read_refuses(
        self, tmp_path, monkeypatch
    ) -> None:
        """A ceiling swap between the permit and the send must refuse."""
        import kiro_crew.dashboard.slack_egress as se
        import kiro_crew.platform.governance_profiles as gp

        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)
        _configure_slack_channel(monkeypatch, "C777", "always")
        state.owner_id = ""

        ceiling = {"v": 7}
        monkeypatch.setattr(se, "governance_answer_generation", lambda: ceiling["v"])
        monkeypatch.setattr(gp, "governance_answer_generation", lambda: ceiling["v"])

        calls = {"n": 0}

        def _governance_then_refresh(*args, **kwargs):
            # Call 1 is the early gate, before the basis ladder: bumping there would
            # make the send refuse for a reason that holds without the fix.
            calls["n"] += 1
            if calls["n"] >= 2:
                ceiling["v"] += 1
            return True

        monkeypatch.setattr(se, "channel_egress_permitted", _governance_then_refresh)
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == [], (
            "a new governance ceiling was installed after the egress permit was read, "
            "so this send must refuse -- it delivered on the superseded permit"
        )
        assert calls["n"] >= 2, (
            f"the governance gate was asked only {calls['n']} time(s), so the ceiling "
            f"never moved inside the window under test"
        )

    @pytest.mark.asyncio
    async def test_an_unreadable_generation_refuses_rather_than_sending(
        self, tmp_path, monkeypatch
    ) -> None:
        """A counter that cannot be read is not evidence the permit still holds."""
        import kiro_crew.dashboard.slack_egress as se
        import kiro_crew.platform.governance_profiles as gp

        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)
        _configure_slack_channel(monkeypatch, "C777", "always")
        state.owner_id = ""

        def _unreadable():
            raise RuntimeError("governance generation unavailable")

        monkeypatch.setattr(se, "governance_answer_generation", _unreadable)
        monkeypatch.setattr(gp, "governance_answer_generation", _unreadable)
        monkeypatch.setattr(se, "channel_egress_permitted", lambda *a, **k: True)
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == [], (
            "the governance generation could not be read, so the permit could not be "
            "re-confirmed -- the send must refuse rather than assume it still holds"
        )


class TestTheConfiguredChannelAuthorityIsTheActivationValue:
    """The send-adjacent re-check compares the ACTIVATION, not a global counter.

    An earlier revision bracketed the activation read between two whole-config change
    detectors -- the in-process cache generation and a stat fingerprint -- and refused
    if either moved. Both count activity on the entire config, so a save touching an
    unrelated key refused a delivery whose own authority never moved, abandoning a
    multi-part note half-posted. The value answers the question actually being asked,
    and it is cross-process on its own: ``KiroCrewConfig.load`` is keyed on a stat
    fingerprint, so a foreign rewrite misses the cache and is re-read from disk.
    """

    @pytest.mark.asyncio
    async def test_an_unrelated_config_save_mid_send_still_delivers(
        self, tmp_path, monkeypatch
    ) -> None:
        """A save that never touched this channel must not abort the send.

        The discriminating case. The activation is held TRUE throughout while a global
        config counter advances on every read: a guard keyed on that counter refuses
        here, and a guard keyed on the activation value delivers. ``raising=False``
        because the counter is deliberately not imported -- the patch is inert against
        this code, and only a counter-keyed guard would read it.
        """
        import kiro_crew.dashboard.slack_egress as se
        import kiro_crew.platform.governance_profiles as gp

        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)
        _configure_slack_channel(monkeypatch, "C777", "always")
        state.owner_id = ""
        monkeypatch.setattr(se, "governance_answer_generation", lambda: 7)
        monkeypatch.setattr(gp, "governance_answer_generation", lambda: 7)
        monkeypatch.setattr(se, "channel_egress_permitted", lambda *a, **k: True)

        counter = {"n": 20}

        def _unrelated_save() -> int:
            counter["n"] += 1
            return counter["n"]

        monkeypatch.setattr(se, "config_generation", _unrelated_save, raising=False)
        monkeypatch.setattr(se, "config_fingerprint", _unrelated_save, raising=False)
        monkeypatch.setattr(se, "_configured_channel_active", lambda _c: True)
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == ["slack"], (
            "an unrelated config save moved the global counters while this channel's "
            "own activation never changed, so the send must still deliver -- refusing "
            "here abandons a multi-part note half-posted on someone else's save"
        )

    @pytest.mark.asyncio
    async def test_a_deactivated_channel_refuses_before_sending(
        self, tmp_path, monkeypatch
    ) -> None:
        """The control: the activation value is still a real revocation check."""
        import kiro_crew.dashboard.slack_egress as se
        import kiro_crew.platform.governance_profiles as gp

        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)
        _configure_slack_channel(monkeypatch, "C777", "always")
        state.owner_id = ""
        monkeypatch.setattr(se, "governance_answer_generation", lambda: 7)
        monkeypatch.setattr(gp, "governance_answer_generation", lambda: 7)
        monkeypatch.setattr(se, "channel_egress_permitted", lambda *a, **k: True)

        reads = {"n": 0}

        def _deactivated_on_the_resend(_channel_id: str) -> bool:
            # Active for the basis ladder, deactivated by the per-send revalidation --
            # the revocation the plain re-read exists to catch.
            reads["n"] += 1
            return reads["n"] < 2

        monkeypatch.setattr(se, "_configured_channel_active", _deactivated_on_the_resend)
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == [], (
            "the channel was deactivated before the send, so this must refuse -- a "
            "guard that delivers here is not checking the authority at all"
        )

    @pytest.mark.asyncio
    async def test_an_unreadable_config_refuses_rather_than_sending(
        self, tmp_path, monkeypatch
    ) -> None:
        """FAIL-CLOSED: a config that cannot be read has not re-confirmed anything."""
        import kiro_crew.dashboard.slack_egress as se
        import kiro_crew.platform.governance_profiles as gp

        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)
        _configure_slack_channel(monkeypatch, "C777", "always")
        state.owner_id = ""
        monkeypatch.setattr(se, "governance_answer_generation", lambda: 7)
        monkeypatch.setattr(gp, "governance_answer_generation", lambda: 7)
        monkeypatch.setattr(se, "channel_egress_permitted", lambda *a, **k: True)

        reads = {"n": 0}

        def _unreadable_on_the_resend(_channel_id: str):
            reads["n"] += 1
            if reads["n"] >= 2:
                raise OSError("config unreadable")
            return True

        monkeypatch.setattr(se, "KiroCrewConfig", _Unreadable(_unreadable_on_the_resend))
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == [], (
            "the config could not be read before the send, and 'cannot tell' is not "
            "'still authorized' -- this boundary must refuse"
        )

    def test_the_send_adjacent_recheck_does_no_filesystem_work(self) -> None:
        """The send-adjacent re-checks must not stat: they run on the event loop.

        Pinned as source rather than behaviour because the harm is latency under a
        slow or networked config path, which a passing functional test cannot show.
        The activation read itself is legal -- it runs inside ``_send_time_authorities``
        under ``asyncio.to_thread`` -- so what is pinned is that no filesystem work
        happens between the last await and ``post_message``.
        """
        import inspect

        import kiro_crew.dashboard.slack_egress as se

        assert not inspect.iscoroutinefunction(se.governance_ceiling_unchanged), (
            "governance_ceiling_unchanged is a coroutine, which reopens the very "
            "window the send-adjacent re-check exists to close"
        )
        assert "config_fingerprint" not in inspect.getsource(se.governance_ceiling_unchanged)

        body = inspect.getsource(se._deliver_slack_governed)
        tail = body[body.index("def _authority_still_holds") : body.index("async def _permitted")]
        for banned in ("KiroCrewConfig", "_configured_channel_active", "config_fingerprint"):
            assert banned not in tail, (
                f"_authority_still_holds reaches {banned}; it runs on the event loop "
                f"between the last await and post_message, so that is blocking IO"
            )
        assert "configured_active is True" in tail, (
            "the send-adjacent re-check no longer compares the activation value it "
            "was handed, so it is not asserting the authority at all"
        )


class _Unreadable:
    """Stands in for ``KiroCrewConfig``, raising from ``load`` on a chosen read."""

    def __init__(self, probe) -> None:
        self._probe = probe

    def load(self):
        self._probe("")
        return _AlwaysActiveConfig()


class _AlwaysActiveConfig:
    """A config whose single channel is always in an offered activation."""

    class _Entry:
        activation = "always"

    @property
    def slack_channels(self) -> dict:
        return {"C777": self._Entry()}


class TestNoDeliveredChunkIsEverDeleted:
    """The egress path never deletes a message it delivered.

    An earlier revision retracted a chunk that landed in a channel deactivated inside
    the ``post_message`` round trip. Two facts made that a net loss. The per-chunk
    ``_send_time_authorities`` re-read already catches the deactivation -- it stats
    through ``KiroCrewConfig.load``, so it sees a foreign writer -- and the retraction's
    own failure arms left the chunk in place anyway, so it never guaranteed removal.
    What it did add was a destructive call on an authority read after the fact, which
    on an unreadable config would have deleted a valid delivery.
    """

    @pytest.mark.asyncio
    async def test_a_deactivation_after_the_send_does_not_delete_the_chunk(
        self, tmp_path, monkeypatch
    ) -> None:
        """The discriminating case: deactivate DURING the send, expect no delete.

        Patches the SEPARATE raising form the retracting revision read as well as the
        fail-closed name: patching only the latter would leave that revision reading the
        real config, and this test would pass against the very code it discriminates.
        """
        import kiro_crew.dashboard.slack_egress as se
        import kiro_crew.platform.governance_profiles as gp

        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)
        _configure_slack_channel(monkeypatch, "C777", "always")
        state.owner_id = ""
        monkeypatch.setattr(se, "governance_answer_generation", lambda: 7)
        monkeypatch.setattr(gp, "governance_answer_generation", lambda: 7)
        monkeypatch.setattr(se, "channel_egress_permitted", lambda *a, **k: True)
        state.slack_client.delete_message = AsyncMock(return_value=None)

        active = {"v": True}

        async def _post_then_deactivate(channel, text, thread_ts=None):
            # The deactivation lands INSIDE the round trip, so the chunk genuinely
            # goes out before any authority could observe it. Otherwise vacuous.
            active["v"] = False
            return "ts-1"

        state.slack_client.post_message = AsyncMock(side_effect=_post_then_deactivate)
        monkeypatch.setattr(se, "_configured_channel_active", lambda _c: active["v"])
        # ``raising=False``: that name is deliberately gone now, so the patch is inert
        # against the fixed code and is the live lever against the retracting one.
        monkeypatch.setattr(
            se, "_configured_channel_active_or_raise", lambda _c: active["v"], raising=False
        )
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        await _mirror(state, slot, "dashboard:chat-1", "hi", "note")

        assert state.slack_client.delete_message.await_count == 0, (
            "the egress path deleted a message it had already delivered; the "
            "deactivation is caught by the next per-chunk re-read, and deleting on a "
            "post-hoc authority read destroys a delivery on an unreadable config"
        )


class TestEveryDenialGoesThroughTheOneEmitter:
    """No refusal on this leg may reach SEL except through ``audit_egress``.

    Six branches once called ``sel().log_api_access`` directly with prose in
    ``error=``, so an operator filtering rows by reason code missed exactly those six
    denials while the log still read as complete. Pinned as source because the harm
    is the SHAPE of a row that is present, which a passing delivery test cannot show.
    """

    def _module_source(self) -> str:
        import inspect

        import kiro_crew.dashboard.slack_egress as se

        return inspect.getsource(se)

    def test_the_module_reaches_sel_from_exactly_one_place(self) -> None:
        """Every denial routes through the emitter, and the emitter is what emits."""
        import inspect

        import kiro_crew.dashboard.slack_egress as se

        source = self._module_source()
        direct = source.count("sel().log_api_access")
        assert direct == 1, (
            f"{direct} direct SEL calls in slack_egress; every denial must go through "
            f"audit_egress so its reason is a filterable code, not prose in error="
        )
        assert "sel().log_api_access" in inspect.getsource(se.audit_egress), (
            "the one direct SEL call is no longer inside audit_egress, so this module "
            "has an emitter that does not emit"
        )

    def test_every_denial_reason_is_a_stable_code(self) -> None:
        """A reason an operator cannot filter on is not a reason code."""
        import re

        reasons = re.findall(r'reason=f?"([^"]*)"', self._module_source())

        assert len(reasons) >= 6, (
            f"only {len(reasons)} reason= literals found, so this census is not "
            f"reaching the folded denial sites"
        )
        for reason in reasons:
            bare = re.sub(r"\{[^}]*\}", "n", reason)
            assert re.fullmatch(r"[a-z0-9_]+", bare), (
                f"reason {reason!r} is not a stable lower_snake code; an operator "
                f"filters rows by this value, so prose makes the row unfilterable"
            )


class TestTheProactiveSlackTierMapIsMechanical:
    """The three-tier map must not live only in a docstring.

    The sets below OWN the tier map: they are the executable enumeration, and they are
    the point: two reviewers observed that the map was written in comments, and a
    comment cannot fail, so nothing stopped a fourth plain-client sender arriving or
    the consolidation being forgotten. The deferred inventory that sits around them --
    the resolve-then-send sites and the two unreachable copies of the gate body -- is
    recorded in ``docs/system-specs/modules/messaging.md``.

    Scoped to ``dashboard`` deliberately: the ``slack`` package answers inbound
    events, where replying to a message the user just sent has no proactive TOCTOU to
    close. A new direct sender fails this test, so its author has to say which tier it
    joins rather than leave it unclassified. Pinned by MODULE, not by line, so
    ordinary edits inside these files do not move it.
    """

    _HARDENED = {"slack_egress.py"}
    _GATE_ONLY = {"chat_compaction_notice.py"}
    _REACTIVE = {"chat_runner.py", "chat_slack.py"}
    _PLAIN_CLIENT = {"server.py", "hooks.py", "messaging.py"}

    def _senders(self) -> set[str]:
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard"
        found = set()
        for path in root.rglob("*.py"):
            if "post_message(" in path.read_text(encoding="utf-8"):
                found.add(path.name)
        return found

    def test_no_unclassified_dashboard_module_sends_to_slack(self) -> None:
        """Every direct sender belongs to a named tier."""
        classified = self._HARDENED | self._GATE_ONLY | self._REACTIVE | self._PLAIN_CLIENT
        found = self._senders()

        assert found - classified == set(), (
            f"{sorted(found - classified)} reach Slack directly but are in no tier of "
            f"the proactive-egress map. Classify each one: adopt the hardened chain in "
            f"slack_egress, or record why it stays on the plain client."
        )
        assert classified - found == set(), (
            f"{sorted(classified - found)} no longer send to Slack, so this census is "
            f"stale -- drop them from the tier map here and in the module comment"
        )

    def test_the_plain_client_tier_has_not_grown(self) -> None:
        """The unhardened tier is the consolidation debt; it must not widen quietly."""
        found = self._senders()
        plain = found & self._PLAIN_CLIENT

        assert plain == self._PLAIN_CLIENT, (
            f"the plain-client tier changed: expected {sorted(self._PLAIN_CLIENT)}, "
            f"found {sorted(plain)}. Growing it widens the gap this feature's chain "
            f"exists to close; shrinking it means the consolidation landed and this "
            f"baseline plus the tier comment should both come down."
        )


class TestTheDeferredInventoryCannotAgeSilently:
    """The deferred sites are pinned executably, so the inventory cannot be decoration.

    The tier map above trips on a NEW direct sender or a tier move, which leaves the
    resolve-then-send half of the inventory carried by prose alone -- and a deferral only
    prose carries is one that gets carried silently. Each site below is asserted still
    UNHARDENED, so adopting the chain at one of them fails here and the entry in
    ``docs/system-specs/modules/messaging.md`` has to come down with it, and the doc has
    to keep naming every site, so the list and the code cannot drift apart.

    The predicate is discriminating rather than a spelling check: this feature's own
    hardened leg carries a marker and is asserted to, so a scan that could not tell
    hardened from deferred fails its own control instead of passing everything.
    """

    _MARKERS = ("send_parts_revalidating", "deliver_to_channel(", "_deliver_slack_governed")

    _DEFERRED = {
        "state._notify_inbound_unbind": (
            "src/kiro_crew/dashboard/state.py",
            "_notify_inbound_unbind",
        ),
        "chat_mirror.api_chat_slot_mirror_link": (
            "src/kiro_crew/dashboard/chat_mirror.py",
            "api_chat_slot_mirror_link",
        ),
        "slack/gateway._deliver_channel_reply": (
            "src/kiro_crew/slack/gateway.py",
            "_deliver_channel_reply",
        ),
        "handlers/messaging._deliver_channel_dm": (
            "src/kiro_crew/dashboard/handlers/messaging.py",
            "_deliver_channel_dm",
        ),
        "chat_compaction_notice._deliver_via_transport": (
            "src/kiro_crew/dashboard/chat_compaction_notice.py",
            "_deliver_via_transport",
        ),
    }

    _HARDENED_CONTROL = (
        "src/kiro_crew/dashboard/chat_note_mirror.py",
        "_deliver_via_transport",
    )

    _DOC = "docs/system-specs/modules/messaging.md"

    def _body(self, relative: str, function: str) -> str:
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        source = (root / relative).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == function:
                    return ast.get_source_segment(source, node) or ""
        raise AssertionError(
            f"{function} is gone from {relative}: the deferred inventory names a symbol "
            f"that no longer exists, so the list is stale rather than satisfied"
        )

    def test_the_scan_can_tell_a_hardened_leg_from_a_deferred_one(self) -> None:
        """Control: the hardened leg must trip the predicate the deferred sites must not."""
        body = self._body(*self._HARDENED_CONTROL)

        assert any(marker in body for marker in self._MARKERS), (
            f"none of {self._MARKERS} appears in the hardened leg, so this scan cannot "
            f"distinguish adoption from deferral and every assertion below is vacuous"
        )

    def test_every_deferred_site_is_still_deferred(self) -> None:
        """Adopting the chain at one of these is progress that must move the inventory."""
        adopted = sorted(
            name
            for name, where in self._DEFERRED.items()
            if any(marker in self._body(*where) for marker in self._MARKERS)
        )

        assert adopted == [], (
            f"{adopted} now route through the hardened chain. That is the consolidation "
            f"landing: drop each one from this map AND from the deferred inventory in "
            f"{self._DOC}, so the recorded debt matches the code"
        )

    def test_the_deferred_inventory_names_every_site(self) -> None:
        """The recorded debt and the pinned debt are the same list, or this fails."""
        import pathlib

        doc = (pathlib.Path(__file__).resolve().parents[1] / self._DOC).read_text(encoding="utf-8")
        missing = sorted(name for name, where in self._DEFERRED.items() if where[1] not in doc)

        assert missing == [], (
            f"{missing} are pinned here but absent from the inventory in {self._DOC}: a "
            f"reader of the spec would not learn the site is unhardened"
        )


class TestTheRevalidationProtocolCannotDivergeSilently:
    """The protocol is spelled TWICE, so a test -- not prose -- has to hold them level.

    ``deliver_to_channel._still_permitted`` and ``slack_egress._permitted_to_send`` each
    write out sample-off-loop -> re-check the governance token -> re-authorize the
    destination -> refuse on mismatch. Only the per-part driver
    (``send_parts_revalidating``) is shared code, so a step added to or removed from one
    copy does not reach the other, and the module docs recorded that as a deferral with
    nothing enforcing it. A reviewer's objection was precisely that: a deferral only prose
    carries is one that gets carried silently.

    So this asserts the STEPS, not the text. The destination step legitimately differs by
    transport -- the channel leg re-walks the binding ladder, the Slack leg re-reads the
    recipient basis -- which is why consolidating the bodies would be a behaviour change
    to the Slack path rather than a refactor, and why the enforcement lives here instead.
    Extracted by AST so ordinary edits inside either function do not move it.
    """

    _REQUIRED = ("to_thread", "governance_ceiling_unchanged")
    _DESTINATION_RECHECK = ("_binding_unchanged", "_slack_recipient_basis")

    _CHAINS = {
        "deliver_to_channel._still_permitted": (
            "src/kiro_crew/dashboard/handlers/messaging.py",
            "_still_permitted",
        ),
        "slack_egress._permitted_to_send": (
            "src/kiro_crew/dashboard/slack_egress.py",
            "_permitted_to_send",
        ),
    }

    def _source_of(self, relative: str, function: str) -> str:
        import ast
        import pathlib

        path = pathlib.Path(__file__).resolve().parents[1] / relative
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        found = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == function
        ]
        assert found, f"{function} no longer exists in {relative}; the census is stale"
        return "\n".join(ast.get_source_segment(text, node) for node in found)

    def test_both_chains_carry_every_revalidation_step(self) -> None:
        """Neither copy may lose a step the other still performs."""
        for label, (relative, function) in self._CHAINS.items():
            body = self._source_of(relative, function)
            missing = [step for step in self._REQUIRED if step not in body]
            assert not missing, (
                f"{label} no longer performs {missing}. Both revalidation chains must "
                f"sample off the event loop and re-check the governance token before a "
                f"part goes out; a step present in only one copy is the drift this "
                f"pins. Add it back, or consolidate the two chains and delete this test."
            )
            assert any(step in body for step in self._DESTINATION_RECHECK), (
                f"{label} re-checks no destination: it must re-authorize where the part "
                f"is going, via one of {list(self._DESTINATION_RECHECK)}. Without it a "
                f"revoked destination still receives the remaining parts."
            )

    def test_the_shared_driver_is_the_only_shared_body(self) -> None:
        """The sharing claim in the module docs is a count, so hold it to the count."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        consumers = sorted(
            path.name
            for path in root.rglob("*.py")
            if "send_parts_revalidating(" in path.read_text(encoding="utf-8")
            and path.name != "renderer.py"
        )

        assert consumers == ["messaging.py", "slack_egress.py"], (
            f"send_parts_revalidating now has consumers {consumers}, not the two this "
            f"feature documents. A third caller means the protocol is spelled a third "
            f"time: add it to the chains checked above, or consolidate."
        )


class TestAProfileTighteningDuringTheAuthorityReadRefusesTheSend:
    """A permit is ceiling ∩ profile, so the guard must watch both halves.

    ``governance_permits`` resolves the boot-frozen ceiling intersected with the
    surface's active profile. The profile layer reloads from disk on an mtime change
    and publishes a new snapshot WITHOUT installing a context, so the ceiling counter
    cannot move for a profile-only tightening. A guard reading that counter alone
    therefore reports "unchanged" for a profile withdrawn inside the await window,
    and the note posts on a permit its own profile layer has already revoked.

    ``governance_answer_generation`` closes it by comparing the composite. These
    tests hold the CEILING half constant on purpose: a change that moved the ceiling
    would refuse on the old code too, so only a moved profile half under a still
    ceiling can discriminate this fix from its absence.

    The moved half is driven by an ADVANCING counter rather than by a bump inside the
    permit gate, because that gate is called more than once per send (a pre-flight
    hop plus the one inside the authority read), so a bump there depends on the call
    count rather than on the window under test.
    """

    @pytest.mark.asyncio
    async def test_a_profile_publication_after_the_sample_refuses(
        self, tmp_path, monkeypatch
    ) -> None:
        """A profile publication after the authority hop must refuse the send."""
        import kiro_crew.dashboard.slack_egress as se
        import kiro_crew.platform.governance_profiles as gp

        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)
        _configure_slack_channel(monkeypatch, "C777", "always")
        state.owner_id = ""

        monkeypatch.setattr(se, "channel_egress_permitted", lambda *a, **k: True)
        # Patch the binding slack_egress ITSELF calls. Patching the defining module
        # is not equivalent: the tail resolves this name from se's own globals.
        monkeypatch.setattr(gp, "poll_profiles_fresh", lambda: None)
        monkeypatch.setattr(se, "poll_profiles_fresh", lambda: None)

        seen = {"n": 5}

        def _advancing() -> int:
            # Advances per call, so the tail's read differs from the hop's sample --
            # the stand-in for a publication landing between the two.
            seen["n"] += 1
            return seen["n"]

        monkeypatch.setattr(se, "governance_answer_generation", _advancing)
        monkeypatch.setattr(gp, "governance_answer_generation", _advancing)
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == [], (
            "the profile layer published after its answer was sampled, so this send "
            "must refuse -- the permission it holds may already be revoked"
        )
        assert seen["n"] >= 7, (
            f"the composite was read only {seen['n'] - 5} time(s), so the hop and the "
            f"tail never compared two different values"
        )

    @pytest.mark.asyncio
    async def test_a_still_profile_and_ceiling_still_delivers(self, tmp_path, monkeypatch) -> None:
        """The positive control: nothing moved, so the send must still go out."""
        import kiro_crew.dashboard.slack_egress as se
        import kiro_crew.platform.governance_profiles as gp

        state = _slack_state(tmp_path, channel="C777")
        _authorize_slack_recipient(monkeypatch)
        _configure_slack_channel(monkeypatch, "C777", "always")
        state.owner_id = ""

        monkeypatch.setattr(se, "channel_egress_permitted", lambda *a, **k: True)
        monkeypatch.setattr(gp, "poll_profiles_fresh", lambda: None)
        monkeypatch.setattr(se, "poll_profiles_fresh", lambda: None)
        monkeypatch.setattr(se, "governance_answer_generation", lambda: 11)
        monkeypatch.setattr(gp, "governance_answer_generation", lambda: 11)
        slot = _slot(state, thread="1785370133.085469", channel="C777")

        assert await _mirror(state, slot, "dashboard:chat-1", "hi", "note") == [
            "slack"
        ], "neither the ceiling nor the profile moved, so this send must proceed"

    def test_the_composite_folds_in_the_profile_layer(self) -> None:
        """The counter the guard reads must be the one a profile publication moves.

        Pinned directly because the whole defect was a guard watching a counter that
        a profile-only change cannot move, which a delivery test alone cannot show.
        """
        import kiro_crew.platform.governance_profiles as gp

        store = gp.ProfileStore()
        before = gp.governance_answer_generation()
        gp._publish_snapshot(store, gp._Snapshot(by_name={}, by_bind={}, loaded=True))
        after = gp.governance_answer_generation()

        assert after != before, (
            "a profile publication did not move the composite, so the send-adjacent "
            "guard is still blind to a profile-layer tightening"
        )

    def test_no_generation_consumer_reads_a_magnitude(self) -> None:
        """Every consumer compares the token; none reads how far it moved.

        The composite is a SUM of two counters, so one edit can move it by more than
        one and an unrelated profile publication moves it at all. Arithmetic or ordering
        on it would read either as a different event, which is exactly the
        per-reload-attempt bump this change removed. Mechanical because the SHAPE is the
        invariant: a behaviour test on today's four consumers cannot see a fifth appear.
        """
        import pathlib
        import re

        src = pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        seen, offenders = [], []
        for path in src.rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "governance_answer_generation(" not in line:
                    continue
                seen.append(path.name)
                if re.search(r"governance_answer_generation\(\)\s*(?!->)[-+<>]", line):
                    offenders.append(f"{path.name}: {line.strip()}")
        assert len(set(seen)) >= 4, (
            f"only found the accessor in {sorted(set(seen))}: the scan is not reaching "
            f"the consumers, so a clean result would prove nothing"
        )
        assert not offenders, (
            f"a consumer does arithmetic or ordering on an opaque comparison-only "
            f"token: {offenders}"
        )

    def test_a_publication_moves_the_generation_exactly_once(self, tmp_path, monkeypatch) -> None:
        """One publication, one bump — and a reload that publishes nothing, none.

        An earlier revision bumped inside the publication AND again after ``_reload``
        returned. Both are on the reload path, so every profile reload moved the token
        twice, and the arm that deliberately publishes NOTHING — an unreadable profile
        directory with a warm prior snapshot, which keeps serving that snapshot — moved
        it anyway. Senders compare this token mid-send and read any movement as a
        revoked authority, so a spurious move abandons a multi-part send after a
        delivered prefix. Driven through ``_ensure_fresh`` because that is where the
        second bump lived: calling the publication directly cannot see it, and a
        magnitude-blind `!=` assertion cannot either.
        """
        import json

        import kiro_crew.platform.governance_profiles as gp

        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(gp, "_profiles_dir", lambda: profiles)

        store = gp.ProfileStore()
        before = gp._profile_generation()
        assert store._ensure_fresh() is True
        assert store._snap.by_name, "the fixture did not prime a warm snapshot"
        assert gp._profile_generation() - before == 1, (
            "one profile publication moved the generation more than once, so a "
            "mid-send re-ask sees more movement than the profile layer performed"
        )

        # A NON-directory at the profiles path is the arm that keeps the warm snapshot
        # and publishes nothing; a MISSING directory is benign absence and does publish.
        not_a_directory = tmp_path / "profiles-file"
        not_a_directory.write_text("", encoding="utf-8")
        monkeypatch.setattr(gp, "_profiles_dir", lambda: not_a_directory)
        steady = gp._profile_generation()
        store._ensure_fresh()
        assert store._snap.by_name, "the warm snapshot was not preserved"
        assert gp._profile_generation() == steady, (
            "a reload that published nothing still moved the generation, so an "
            "unreadable profile directory revokes an in-flight send's authority"
        )

    def test_a_publication_cannot_be_seen_before_its_generation_bump(self) -> None:
        """The snapshot and its generation must land in ONE hold of the reader lock.

        Installing the snapshot first and bumping second leaves a window where a
        reader sees the NEW answers under the OLD generation, so a sender re-confirms
        an authority already replaced. Holding the reader lock must therefore block
        the whole publication, not just the bump.
        """
        import threading

        import kiro_crew.platform.governance_profiles as gp

        store = gp.ProfileStore()
        original = store._snap
        fresh = gp._Snapshot(by_name={}, by_bind={}, loaded=True)
        published = threading.Event()

        def _publish() -> None:
            gp._publish_snapshot(store, fresh)
            published.set()

        worker = threading.Thread(target=_publish, daemon=True)
        with gp._PROFILE_GENERATION_LOCK:
            worker.start()
            assert not published.wait(0.25), (
                "the publication completed while a generation reader held the lock, "
                "so the snapshot and its bump are not one critical section"
            )
            assert store._snap is original, (
                "the snapshot was installed while the reader lock was held, so a "
                "reader can observe it carrying the previous generation"
            )

        assert published.wait(5), "the publication never completed after release"
        worker.join(timeout=5)
        assert store._snap is fresh, "the publication did not install the snapshot"
