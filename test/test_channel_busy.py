"""The hand-off from a channel's busy path to the dashboard turn driving a resumed session.

``dashboard/channel_busy.py`` is what a Discord, Telegram or Teams dispatcher calls
when a message arrives for a resumed ``dashboard:`` session that is mid-turn. These
tests pin its three answers against a real ``DashboardState`` and slot: nothing to do
when no dashboard turn holds the session, a queue entry carrying channel provenance
and the admission containment when one does, and a refusal (nothing queued) when the
message carries attachments the dashboard queue cannot hold.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import channel_busy
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard.channel_busy import (
    CHANNEL_ORIGIN_META_KEY,
    CHANNEL_ORIGIN_PRINCIPAL_KEY,
    HANDOFF_ATTACHMENTS_REFUSED,
    HANDOFF_NOT_DASHBOARD_TURN,
    HANDOFF_QUEUED,
    channel_binding_released,
    channel_origin_address,
    channel_origin_principal,
    dashboard_turn_in_progress,
    hand_to_dashboard_turn,
)
from kiro_crew.dashboard.chat_utils import (
    CHANNEL_COMMAND_TOKEN_MAX,
    CHANNEL_COMMAND_UNAVAILABLE,
    dashboard_command_word,
    displayable_channel_command,
    suppressed_channel_command,
)
from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY
from kiro_crew.messaging.link import ChannelLink


def _state(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    # Nothing here asserts durability, and a queued send kicks off a real flush on
    # the default executor (``chat_delivery.start_queue_persist``); a worker that
    # outlived the test would write into a directory pytest has already removed.
    # Same idiom as test_queue_cancel.py.
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_delivery.start_queue_persist", lambda st, sl: None
    )
    return state


async def _running_slot(state: Any, name: str = "chat-1") -> tuple[Any, asyncio.Future]:
    """A slot whose dashboard turn is in flight: ``slot.task`` is a pending future."""
    slot = state.get_or_create_slot(name)
    pending: asyncio.Future = asyncio.get_running_loop().create_future()
    slot.task = pending
    assert slot.running
    return slot, pending


class TestDashboardTurnInProgress:
    def test_a_non_dashboard_key_never_has_a_dashboard_turn(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        assert dashboard_turn_in_progress(state, "telegram:7:gen0") is False

    def test_no_open_slot_means_no_dashboard_turn(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        assert dashboard_turn_in_progress(state, "dashboard:absent") is False

    def test_an_idle_slot_is_not_a_dashboard_turn(self, tmp_path, monkeypatch) -> None:
        """A channel turn on the resumed key leaves the slot idle from the
        dashboard's point of view -- it holds the session lease, not ``slot.task``
        -- so the channel keeps its own steer/queue path for that case."""
        state = _state(tmp_path, monkeypatch)
        state.get_or_create_slot("chat-1")
        assert dashboard_turn_in_progress(state, "dashboard:chat-1") is False

    @pytest.mark.asyncio
    async def test_a_live_task_is_a_dashboard_turn(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        _slot, pending = await _running_slot(state)
        try:
            assert dashboard_turn_in_progress(state, "dashboard:chat-1") is True
        finally:
            pending.cancel()

    def test_between_stages_counts_as_a_dashboard_turn(self, tmp_path, monkeypatch) -> None:
        """Between a plan's stages the task is gone but the plan is live; a message
        admitted then must wait in the slot queue rather than start a rival turn --
        the same predicate the peer send path reads."""
        state = _state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")
        slot._in_stage_execution = True
        assert dashboard_turn_in_progress(state, "dashboard:chat-1") is True


class TestHandToDashboardTurn:
    def test_no_dashboard_turn_hands_nothing_off(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")

        outcome = hand_to_dashboard_turn(state, "dashboard:chat-1", "follow-up")

        assert outcome == HANDOFF_NOT_DASHBOARD_TURN
        assert slot._queue == []

    def test_no_slot_hands_nothing_off(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        assert hand_to_dashboard_turn(state, "dashboard:absent", "x") == HANDOFF_NOT_DASHBOARD_TURN
        assert hand_to_dashboard_turn(None, "dashboard:chat-1", "x") == HANDOFF_NOT_DASHBOARD_TURN

    @pytest.mark.asyncio
    async def test_a_running_dashboard_turn_takes_the_message_onto_the_slot_queue(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        slot.linked_session_key = "discord:u1:gen0"
        try:
            outcome = hand_to_dashboard_turn(state, "dashboard:chat-1", "and the weather?")
        finally:
            pending.cancel()

        assert outcome == HANDOFF_QUEUED
        assert [q["content"] for q in slot._queue] == ["and the weather?"]
        entry = slot._queue[0]
        # An allow-listed human typed it into the conversation bound to this session,
        # and that conversation is a channel: both provenance flags, so the drain
        # keeps the LINKED exemption AND channel authority for any directive.
        assert entry.get("_directive_user_origin") is True
        assert entry.get("_directive_channel_origin") is True
        # The admission containment is stamped with ``linked`` already held, so the
        # drain's re-validation does not drop the entry for the link that made this
        # hand-off possible in the first place.
        admitted = entry["meta"][QUEUED_CONTAINMENT_META_KEY]
        assert admitted["linked"] is True
        # Announced to open dashboard clients like any queued send.
        events = [call.args[0] for call in state.broadcast_ws.call_args_list]
        assert "queue_push" in events

    @pytest.mark.asyncio
    async def test_attachments_are_refused_rather_than_queued_without_their_files(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        try:
            outcome = hand_to_dashboard_turn(
                state, "dashboard:chat-1", "see attached", has_attachments=True
            )
        finally:
            pending.cancel()

        assert outcome == HANDOFF_ATTACHMENTS_REFUSED
        assert slot._queue == []

    def test_attachments_do_not_matter_when_nothing_is_handed_off(
        self, tmp_path, monkeypatch
    ) -> None:
        """An idle slot answers "not mine" before the attachment rule: the channel's
        own queue carries attachments fine, so it must get the message."""
        state = _state(tmp_path, monkeypatch)
        state.get_or_create_slot("chat-1")
        outcome = hand_to_dashboard_turn(
            state, "dashboard:chat-1", "see attached", has_attachments=True
        )
        assert outcome == HANDOFF_NOT_DASHBOARD_TURN

    @pytest.mark.asyncio
    async def test_the_entry_goes_through_the_dashboards_own_producer(
        self, tmp_path, monkeypatch
    ) -> None:
        """One queue, one producer: the hand-off calls ``queue_for_next_turn`` rather
        than appending to the slot directly, so persistence, the crew log and the
        broadcast stay the dashboard's own."""
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        calls: list[dict[str, Any]] = []

        def _producer(st: Any, sl: Any, text: str, **kwargs: Any) -> str:
            calls.append({"state": st, "slot": sl, "text": text, **kwargs})
            return "q-1"

        monkeypatch.setattr(channel_busy, "queue_for_next_turn", _producer)
        try:
            outcome = hand_to_dashboard_turn(state, "dashboard:chat-1", "hello")
        finally:
            pending.cancel()

        assert outcome == HANDOFF_QUEUED
        assert calls == [
            {
                "state": state,
                "slot": slot,
                "text": "hello",
                "directive_user_origin": True,
                "channel_origin": True,
                "channel_address": None,
            }
        ]


class TestTheEntryCarriesItsConversation:
    """A handed-off entry records WHERE it came from, because the drain may have to
    report a drop to that conversation after the binding it rode in on is gone."""

    @pytest.mark.asyncio
    async def test_the_conversation_rides_on_the_entry(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        try:
            outcome = hand_to_dashboard_turn(
                state,
                "dashboard:chat-1",
                "and the weather?",
                origin=ChannelLink("discord", channel_id="c1"),
            )
        finally:
            pending.cancel()

        assert outcome == HANDOFF_QUEUED
        assert slot._queue[0]["meta"][CHANNEL_ORIGIN_META_KEY] == {
            "channel_type": "discord",
            "channel_id": "c1",
            "thread_id": None,
        }
        # Beside the containment snapshot, never in place of it: that snapshot is
        # the drain's own authorization input.
        assert slot._queue[0]["meta"][QUEUED_CONTAINMENT_META_KEY]

    @pytest.mark.asyncio
    async def test_the_admitted_principal_rides_the_stamp(self, tmp_path, monkeypatch) -> None:
        """The dispatcher's allow-list admitted a USER; the drop notice's DM leg is
        judged against a roster of users, so that identity travels with the address."""
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        try:
            hand_to_dashboard_turn(
                state,
                "dashboard:chat-1",
                "and the weather?",
                origin=ChannelLink("discord", channel_id="c1"),
                principal="u1",
            )
        finally:
            pending.cancel()

        stamp = slot._queue[0]["meta"][CHANNEL_ORIGIN_META_KEY]
        assert stamp[CHANNEL_ORIGIN_PRINCIPAL_KEY] == "u1"
        assert channel_origin_principal(slot._queue[0]["meta"]) == "u1"
        # The address still reads back as a plain link.
        link = channel_origin_address(slot._queue[0]["meta"])
        assert link is not None and (link.channel_type, link.channel_id) == ("discord", "c1")

    def test_a_stamp_without_a_principal_reads_as_nobody(self) -> None:
        assert channel_origin_principal(None) == ""
        assert channel_origin_principal({}) == ""
        assert (
            channel_origin_principal(
                {CHANNEL_ORIGIN_META_KEY: {"channel_type": "discord", "channel_id": "c1"}}
            )
            == ""
        )
        # Not a string is not a principal; a stamp that is not an address has none.
        assert (
            channel_origin_principal(
                {
                    CHANNEL_ORIGIN_META_KEY: {
                        "channel_type": "discord",
                        "channel_id": "c1",
                        CHANNEL_ORIGIN_PRINCIPAL_KEY: 7,
                    }
                }
            )
            == ""
        )
        assert (
            channel_origin_principal(
                {CHANNEL_ORIGIN_META_KEY: {"channel_type": "discord", "principal": "u1"}}
            )
            == ""
        )

    def test_an_entry_without_the_stamp_has_no_address(self) -> None:
        assert channel_origin_address(None) is None
        assert channel_origin_address({}) is None
        # A half-written stamp is not an address: a notice cannot be sent to it.
        assert (
            channel_origin_address({CHANNEL_ORIGIN_META_KEY: {"channel_type": "discord"}}) is None
        )

    def test_a_stamped_entry_reads_back_as_its_link(self) -> None:
        link = channel_origin_address(
            {CHANNEL_ORIGIN_META_KEY: {"channel_type": "telegram", "channel_id": "7"}}
        )
        assert link is not None
        assert (link.channel_type, link.channel_id) == ("telegram", "7")


class TestABindingReleasedWhileTheEntryWaited:
    """The binding IS the entry's reply route: a channel resumes a dashboard session
    through an inbound-capable mirror link, so the entry's OWN room gone from the
    session's mirror rooms at the drain means the conversation left the session and
    the message cannot be answered where it was sent -- whatever other rooms the
    session still holds."""

    TELEGRAM = {CHANNEL_ORIGIN_META_KEY: {"channel_type": "telegram", "channel_id": "7"}}

    def test_a_channel_entry_is_released_when_the_mirror_is_gone(self) -> None:
        meta = {CHANNEL_ORIGIN_META_KEY: {"channel_type": "discord", "channel_id": "c1"}}
        assert channel_binding_released({"mirrored": False, "mirror_identity": ""}, meta) is True

    def test_a_live_mirror_keeps_the_entry(self) -> None:
        meta = {CHANNEL_ORIGIN_META_KEY: {"channel_type": "discord", "channel_id": "c1"}}
        now = {"mirrored": True, "mirror_identity": "discord:c1:"}
        assert channel_binding_released(now, meta) is False

    def test_the_origin_room_leaving_beside_a_surviving_room_releases(self) -> None:
        """GPT r17 (security): a Telegram origin beside a Slack thread; Telegram
        ``/unlink``s before the drain. ``mirrored`` stays True and the identity arm
        of ``newly_held_constraints`` sees only a narrowing, so on the r16 head the
        REVOKED prompt drained and ran in the dashboard session. The entry's own
        room is what must remain."""
        both = "telegram:7:|slack:C0FFEE:1758.0001"
        before = {"mirrored": True, "mirror_identity": both}
        assert channel_binding_released(before, self.TELEGRAM) is False
        slack_only = {"mirrored": True, "mirror_identity": "slack:C0FFEE:1758.0001"}
        assert channel_binding_released(slack_only, self.TELEGRAM) is True

    def test_a_mirror_moved_to_another_conversation_releases_too(self) -> None:
        """A -> B: the origin room is not among the rooms either. The identity arm
        reports the newcomer as ``mirror_retarget``; this reports the sender's route
        gone. Both are true and both are said."""
        moved = {"mirrored": True, "mirror_identity": "discord:c9:"}
        assert channel_binding_released(moved, self.TELEGRAM) is True

    def test_a_room_bound_beside_the_origin_is_not_a_release(self) -> None:
        both = {"mirrored": True, "mirror_identity": "telegram:7:|slack:C0FFEE:1758.0001"}
        assert channel_binding_released(both, self.TELEGRAM) is False

    def test_a_slack_thread_origin_is_matched_as_its_thread_room(self) -> None:
        slack = {
            CHANNEL_ORIGIN_META_KEY: {
                "channel_type": "slack",
                "channel_id": "C0FFEE",
                "thread_id": "1758.0001",
            }
        }
        both = {"mirrored": True, "mirror_identity": "telegram:7:|slack:C0FFEE:1758.0001"}
        assert channel_binding_released(both, slack) is False
        telegram_only = {"mirrored": True, "mirror_identity": "telegram:7:"}
        assert channel_binding_released(telegram_only, slack) is True

    def test_the_probe_failure_snapshot_keeps_the_boolean(self) -> None:
        """A drain-side probe that failed omits ``mirror_identity`` and records
        ``mirrored`` True (fail closed); there is no room set to match, so this reads
        the boolean -- and ``newly_held_constraints`` drops the entry on the
        unverifiable mirror regardless."""
        unverified = {"mirrored": True, "mirror_unverified": True}
        assert channel_binding_released(unverified, self.TELEGRAM) is False
        assert channel_binding_released({"mirrored": False}, self.TELEGRAM) is True

    def test_a_composer_entry_is_never_released(self) -> None:
        """Composer text, a peer send and automation lose nothing when a mirror
        disappears, so this constraint is scoped to the channel hand-off alone."""
        assert channel_binding_released({"mirrored": False, "mirror_identity": ""}, None) is False
        assert (
            channel_binding_released({"mirrored": False, "mirror_identity": ""}, {"sendId": "s-1"})
            is False
        )


class TestTheReplyGoesToTheConversationThatAsked:
    """``channel_origin_still_bound`` is the publication-time half of the binding
    check: the drain re-validated the binding when it consumed the entry, but the
    channel-neutral reply leg resolves the mirror LIVE when it delivers, so a rebind
    that lands while the turn runs -- the sender releases the session, another
    conversation resumes it -- would hand the sender's reply to the newcomer. The
    sender's room must still be among the rooms the probe names at publication."""

    STAMP = {"channel_type": "telegram", "channel_id": "7", "thread_id": None, "principal": "u1"}

    def test_the_sending_conversation_still_bound_publishes(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")
        state.sessions.get_mirror_link = MagicMock(return_value=ChannelLink("telegram", "7"))
        assert channel_busy.channel_origin_still_bound(state, slot, self.STAMP) is True

    def test_a_mirror_retargeted_mid_turn_withholds(self, tmp_path, monkeypatch) -> None:
        """GPT's scenario: A handed the message off, A unlinked and B bound while it
        ran. B is the mirror now; it is not the conversation that asked."""
        state = _state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")
        state.sessions.get_mirror_link = MagicMock(return_value=ChannelLink("telegram", "8"))
        assert channel_busy.channel_origin_still_bound(state, slot, self.STAMP) is False
        # A different channel type at the same conversation id is another room too.
        state.sessions.get_mirror_link = MagicMock(return_value=ChannelLink("discord", "7"))
        assert channel_busy.channel_origin_still_bound(state, slot, self.STAMP) is False

    def test_a_released_binding_withholds(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        assert channel_busy.channel_origin_still_bound(state, slot, self.STAMP) is False

    def test_a_room_bound_beside_the_sender_is_not_a_change(self, tmp_path, monkeypatch) -> None:
        """A Slack thread linked onto a Telegram-mirrored session widens the audience
        the way the drain already admits: the sender is still a room, so publish."""
        state = _state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")
        state.sessions.get_mirror_link = MagicMock(return_value=ChannelLink("telegram", "7"))
        state.sessions.get_slack_link = MagicMock(return_value=("1758.0001", "C0FFEE"))
        assert channel_busy.channel_origin_still_bound(state, slot, self.STAMP) is True

    def test_a_slack_thread_origin_is_read_from_the_thread_binding(
        self, tmp_path, monkeypatch
    ) -> None:
        """The Slack linked-thread hand-off stamps ``slack:<channel>:<thread>``; the
        probe names that room from the thread binding, not from the mirror row --
        so a Telegram mirror beside it neither hides nor replaces it."""
        state = _state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")
        stamp = {"channel_type": "slack", "channel_id": "C0FFEE", "thread_id": "1758.0001"}
        state.sessions.get_mirror_link = MagicMock(return_value=ChannelLink("telegram", "7"))
        state.sessions.get_slack_link = MagicMock(return_value=("1758.0001", "C0FFEE"))
        assert channel_busy.channel_origin_still_bound(state, slot, stamp) is True
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        assert channel_busy.channel_origin_still_bound(state, slot, stamp) is False

    def test_an_unreadable_store_fails_closed(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")
        state.sessions.get_mirror_link = MagicMock(side_effect=RuntimeError("store gone"))
        assert channel_busy.channel_origin_still_bound(state, slot, self.STAMP) is False

    def test_a_stamp_naming_no_conversation_fails_closed(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")
        state.sessions.get_mirror_link = MagicMock(return_value=ChannelLink("telegram", "7"))
        assert channel_busy.channel_origin_still_bound(state, slot, {"principal": "u1"}) is False
        assert channel_busy.channel_origin_still_bound(state, slot, "telegram:7:") is False


class TestTheDrainDropsAndReportsIt:
    """The dashboard drain's own drop notice lands on the slot transcript, which the
    channel user is not reading, and its sender notice keys on a sender SLOT, which a
    channel has none of. The channel is told through its own transport instead."""

    @pytest.mark.asyncio
    async def test_the_entry_is_dropped_and_the_conversation_is_told(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        # Mirrored at admission (the conversation resumes this session), gone at the
        # drain (it ran `!unlink`, `!new`, or rotated away while the entry waited).
        probes = iter(["discord:c1:", ""])
        monkeypatch.setattr(
            "kiro_crew.dashboard.session_control._probe_channel_mirror",
            lambda st, sl: next(probes, ""),
        )
        hand_to_dashboard_turn(
            state,
            "dashboard:chat-1",
            "and the weather?",
            origin=ChannelLink("discord", channel_id="c1"),
        )
        assert len(slot._queue) == 1
        told: list[tuple[str, dict, str]] = []

        async def _notify(
            st: Any, session_key: str, origin: Any, *, reason: str, principal: str = ""
        ) -> bool:
            told.append((session_key, origin.to_dict(), reason))
            return True

        monkeypatch.setattr(channel_busy, "notify_channel_origin_dropped", _notify)
        try:
            cr._drop_stale_admissions(state, slot)
            await asyncio.sleep(0)  # the notice is fire-and-forget
        finally:
            pending.cancel()

        assert slot._queue == []
        assert told, "the channel conversation was told nothing"
        session_key, address, reason = told[0]
        assert session_key == "dashboard:chat-1"
        assert address["channel_id"] == "c1"
        assert "left the session" in reason
        # The slot keeps its own notice too: both records exist, neither replaces
        # the other.
        assert any(
            "Queued message dropped" in (m.get("content") or "")
            for m in slot.messages
            if isinstance(m, dict)
        )

    @pytest.mark.asyncio
    async def test_the_origin_unlinking_beside_a_surviving_room_drops_the_prompt(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT r17 (security), end to end: the session mirrors a Telegram chat AND a
        Slack thread; a Telegram user hands a message off; Telegram ``/unlink``s
        before the drain while the Slack thread stays. ``mirrored`` is True at both
        ends and the Slack room was admitted, so ``newly_held_constraints`` reports
        nothing -- on the r16 head the entry drained and the REVOKED prompt ran in
        the dashboard session. Red with the boolean check, green with the room match:
        dropped, the Telegram conversation told its route is gone, nothing run."""
        from kiro_crew.dashboard import session_control as _sc

        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        state.sessions.get_mirror_link = MagicMock(return_value=ChannelLink("telegram", "7"))
        state.sessions.get_slack_link = MagicMock(return_value=("1758.0001", "C0FFEE"))
        hand_to_dashboard_turn(
            state,
            "dashboard:chat-1",
            "/workflow deploy",
            origin=ChannelLink("telegram", channel_id="7"),
            principal="u1",
        )
        (entry,) = slot._queue
        admitted = entry["meta"][_sc.QUEUED_CONTAINMENT_META_KEY]
        assert admitted["mirrored"] is True
        assert _sc.mirror_audience(admitted["mirror_identity"]) == {
            "telegram:7:",
            "slack:C0FFEE:1758.0001",
        }
        # Telegram leaves; the Slack thread remains. Any-mirror-survives says
        # "bound"; the identity arm says "narrowing".
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        now = _sc.containment_snapshot(state, slot, on_probe_failure=True)
        assert now["mirrored"] is True and now["mirror_identity"] == "slack:C0FFEE:1758.0001"
        assert _sc.newly_held_constraints(now, entry["meta"]) == []
        told: list[tuple[dict, str]] = []

        async def _notify(
            st: Any, session_key: str, origin: Any, *, reason: str, principal: str = ""
        ) -> bool:
            told.append((origin.to_dict(), reason))
            return True

        monkeypatch.setattr(channel_busy, "notify_channel_origin_dropped", _notify)
        try:
            cr._drop_stale_admissions(state, slot)
            await asyncio.sleep(0)
        finally:
            pending.cancel()

        assert slot._queue == [], "the revoked hand-off survived the drain"
        address, reason = told[0]
        assert (address["channel_type"], address["channel_id"]) == ("telegram", "7")
        assert reason == "the channel conversation that queued it left the session"
        assert any(
            "Queued message dropped: the channel conversation that queued it left the session"
            in (m.get("content") or "")
            for m in slot.messages
            if isinstance(m, dict)
        )

    @pytest.mark.asyncio
    async def test_a_room_bound_beside_the_origin_does_not_drop_it(
        self, tmp_path, monkeypatch
    ) -> None:
        """The narrowing rule's other side is untouched: the origin still among the
        rooms, a Slack thread bound beside it since admission -- the thread is the
        identity arm's ``mirror_retarget`` (a room the admission never saw), never a
        release of the sender's route."""
        from kiro_crew.dashboard import session_control as _sc

        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        state.sessions.get_mirror_link = MagicMock(return_value=ChannelLink("telegram", "7"))
        hand_to_dashboard_turn(
            state, "dashboard:chat-1", "and the weather?", origin=ChannelLink("telegram", "7")
        )
        (entry,) = slot._queue
        state.sessions.get_slack_link = MagicMock(return_value=("1758.0001", "C0FFEE"))
        now = _sc.containment_snapshot(state, slot, on_probe_failure=True)
        try:
            assert channel_busy.channel_binding_released(now, entry["meta"]) is False
            assert _sc.newly_held_constraints(now, entry["meta"]) == ["mirror_retarget"]
        finally:
            pending.cancel()

    @pytest.mark.asyncio
    async def test_the_notice_task_is_held_until_it_finishes(self, tmp_path, monkeypatch) -> None:
        """The notice is fire-and-forget, not fire-and-lose: the send suspends
        off-loop inside the governed ladder, and a task nobody references is
        garbage-collectable while pending, so the conversation would learn nothing.
        The task is held on the state's background set until it is done."""
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        probes = iter(["discord:c1:", ""])
        monkeypatch.setattr(
            "kiro_crew.dashboard.session_control._probe_channel_mirror",
            lambda st, sl: next(probes, ""),
        )
        hand_to_dashboard_turn(
            state,
            "dashboard:chat-1",
            "and the weather?",
            origin=ChannelLink("discord", channel_id="c1"),
        )
        released = asyncio.Event()

        async def _notify(
            st: Any, session_key: str, origin: Any, *, reason: str, principal: str = ""
        ) -> bool:
            await released.wait()
            return True

        monkeypatch.setattr(channel_busy, "notify_channel_origin_dropped", _notify)
        before = set(state._background_tasks)
        try:
            cr._drop_stale_admissions(state, slot)
            held = state._background_tasks - before
            assert len(held) == 1, "the drop notice task is not held anywhere"
            (task,) = held
            await asyncio.sleep(0)
            assert not task.done()
            assert task in state._background_tasks
            released.set()
            await task
        finally:
            pending.cancel()

        assert task not in state._background_tasks

    @pytest.mark.asyncio
    async def test_an_invalid_governance_ceiling_in_the_notice_is_logged_not_lost(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """The notice lets a ``PlatformCompositionError`` through (see
        ``TestTheChannelIsToldThroughItsOwnTransport``), and the drop sweep runs it
        fire-and-forget: without a done-callback that reads the exception, the only
        trace of a broken ceiling would be asyncio's "never retrieved" line at the
        task's garbage collection, if at all. The held task logs it as an error with
        the traceback, through the redacting logger every background task uses."""
        from kiro_crew.platform.context import PlatformCompositionError

        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        probes = iter(["discord:c1:", ""])
        monkeypatch.setattr(
            "kiro_crew.dashboard.session_control._probe_channel_mirror",
            lambda st, sl: next(probes, ""),
        )
        hand_to_dashboard_turn(
            state,
            "dashboard:chat-1",
            "and the weather?",
            origin=ChannelLink("discord", channel_id="c1"),
        )

        async def _notify(
            st: Any, session_key: str, origin: Any, *, reason: str, principal: str = ""
        ) -> bool:
            raise PlatformCompositionError("governance ceiling invalid: overlapping scopes")

        monkeypatch.setattr(channel_busy, "notify_channel_origin_dropped", _notify)
        before = set(state._background_tasks)
        try:
            with caplog.at_level("ERROR", logger="kiro_crew.dashboard.state"):
                cr._drop_stale_admissions(state, slot)
                (task,) = state._background_tasks - before
                with pytest.raises(PlatformCompositionError):
                    await task
                await asyncio.sleep(0)  # let the done-callbacks run
        finally:
            pending.cancel()

        assert slot._queue == [], "the drop itself never waits on the notice"
        assert task not in state._background_tasks
        logged = [r for r in caplog.records if "Background task failed" in r.getMessage()]
        assert logged, "the propagated PlatformCompositionError left no error log"
        assert "PlatformCompositionError" in logged[0].getMessage()
        assert "overlapping scopes" in logged[0].getMessage()


class TestTheDrainAsksTheRefusalOnTheHarnessAxis:
    """``_start_next_queued_turn`` asks ``refuses_as_channel_command`` on the same
    axis the turn's refusal is asked on: the claude harness answers on the
    ``claude_code`` seam AND on the ``acp`` seam spawning the claude backend, so a
    channel ``/review`` under either drains alone (and is then refused) instead of
    being buried behind the merge banner; under another backend it is prose and
    merges."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "provider,acp_backend,stops",
        [("acp", "claude", True), ("claude_code", "", True), ("acp", "kas", False)],
    )
    async def test_the_merge_stops_at_a_harness_command_on_either_axis(
        self, tmp_path, monkeypatch, provider, acp_backend, stops
    ) -> None:
        state = _state(tmp_path, monkeypatch)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="c1")
        )
        slot, pending = await _running_slot(state)
        for text in ("and the weather?", "/review it"):
            hand_to_dashboard_turn(
                state,
                "dashboard:chat-1",
                text,
                origin=ChannelLink("discord", channel_id="c1"),
                principal="u1",
            )
        pending.cancel()
        slot.task = None
        cfg = await asyncio.to_thread(cr.KiroCrewConfig.load)
        cfg.dashboard.merge_queued_messages = True
        cfg.agent.provider = provider
        cfg.agent.acp_backend = acp_backend
        runs: list[dict[str, Any]] = []

        def _run(state_: Any, slot_: Any, message: str, **kwargs: Any) -> Any:
            runs.append({"message": message, **kwargs})
            return MagicMock()

        monkeypatch.setattr(cr, "_run_chat", _run)
        monkeypatch.setattr(cr, "spawn_guarded_turn", lambda *a, **kw: MagicMock())
        monkeypatch.setattr(cr.KiroCrewConfig, "load", lambda: cfg)

        assert await cr._start_next_queued_turn(state, slot) is True

        assert runs
        if stops:
            assert runs[0]["message"] == "and the weather?", (provider, acp_backend)
            assert [e["content"] for e in slot._queue] == ["/review it"]
        else:
            assert "/review it" in runs[0]["message"] and "merged" in runs[0]["message"]
            assert slot._queue == []


class _Transport:
    """A channel transport that records what it was asked to send."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, str, Any]] = []
        self.fail = fail

    async def send_message(self, channel_id: str, text: str, *, thread_id: Any = None) -> None:
        if self.fail:
            raise RuntimeError("transport down")
        self.sent.append((channel_id, text, thread_id))


class TestTheChannelIsToldThroughItsOwnTransport:
    """``notify_channel_origin_dropped`` is the channel user's only word that their
    queued message will never run. It is addressed from the entry's stamp, walks the
    governed send ladder (``_resolve_channel_target``), and is best-effort: a refusal
    or a failure sends nothing and raises nothing, because the drop itself is the
    authorization decision and never waits on the report."""

    @pytest.mark.asyncio
    async def test_the_notice_reaches_the_stamped_conversation(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        transport = _Transport()
        origin = ChannelLink("telegram", channel_id="7", thread_id="42")
        resolved: list[tuple[Any, str, Any, Any]] = []

        def _resolve(st: Any, session_key: str, link: Any, *, principal: Any = "unset") -> Any:
            resolved.append((st, session_key, link, principal))
            return link, transport

        monkeypatch.setattr(cr, "_resolve_channel_target", _resolve)

        sent = await channel_busy.notify_channel_origin_dropped(
            state,
            "dashboard:chat-1",
            origin,
            reason="the channel conversation that queued it left the session",
            principal="7",
        )

        assert sent is True
        # Resolved for THIS session against the entry's own stamp, not the slot's
        # current link (which is exactly what may be gone) -- and asked with the
        # principal the dispatcher admitted, because a ``dashboard:`` key names
        # nobody and a DM roster is a roster of users.
        assert resolved == [(state, "dashboard:chat-1", origin, "7")]
        assert len(transport.sent) == 1
        channel_id, text, thread_id = transport.sent[0]
        assert (channel_id, thread_id) == ("7", "42")
        assert text.startswith("⚠️ Dropped your queued message: ")
        assert (
            "the channel conversation that queued it left the session after it was queued" in text
        )
        assert "Send it again if it still applies." in text

    @pytest.mark.asyncio
    async def test_without_a_stamped_principal_the_ladder_derives_its_own(
        self, tmp_path, monkeypatch
    ) -> None:
        """No principal on the stamp means the ladder falls back to deriving one from
        the session key (``None``, never ``""`` handed in as an established
        recipient) -- for a dashboard key that is nobody, and the DM leg refuses."""
        state = _state(tmp_path, monkeypatch)
        asked: list[Any] = []

        def _resolve(st: Any, session_key: str, link: Any, *, principal: Any = "unset") -> Any:
            asked.append(principal)
            return None

        monkeypatch.setattr(cr, "_resolve_channel_target", _resolve)

        sent = await channel_busy.notify_channel_origin_dropped(
            state, "dashboard:chat-1", ChannelLink("discord", channel_id="c1"), reason="r"
        )

        assert sent is False
        assert asked == [None]

    @pytest.mark.asyncio
    async def test_a_conversation_the_ladder_refuses_gets_nothing(
        self, tmp_path, monkeypatch
    ) -> None:
        """Governance or the recipient allow-list refusing at send time is a ``None``
        from the ladder: no send, and the caller learns nothing was sent."""
        state = _state(tmp_path, monkeypatch)
        monkeypatch.setattr(cr, "_resolve_channel_target", lambda st, key, link, **kw: None)

        sent = await channel_busy.notify_channel_origin_dropped(
            state, "dashboard:chat-1", ChannelLink("discord", channel_id="c1"), reason="r"
        )

        assert sent is False

    @pytest.mark.asyncio
    async def test_a_ladder_that_raises_sends_nothing_and_does_not_propagate(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _state(tmp_path, monkeypatch)

        def _boom(st: Any, key: str, link: Any, **kw: Any) -> Any:
            raise RuntimeError("governance store unreadable")

        monkeypatch.setattr(cr, "_resolve_channel_target", _boom)

        sent = await channel_busy.notify_channel_origin_dropped(
            state, "dashboard:chat-1", ChannelLink("discord", channel_id="c1"), reason="r"
        )

        assert sent is False

    @pytest.mark.asyncio
    async def test_an_invalid_governance_ceiling_propagates_out_of_the_ladder_leg(
        self, tmp_path, monkeypatch
    ) -> None:
        """``channel_egress_permitted`` re-raises ``PlatformCompositionError`` on
        purpose -- its docstring: swallowing it "would let a broken ceiling read as
        an ordinary skip" -- and the ladder carries it up. The notice's own
        fail-closed arm catches every ordinary failure (the test above) and must not
        catch this one, or a malformed profile at this egress would leave no
        warning, no traceback and no audit row, only a debug line."""
        from kiro_crew.platform.context import PlatformCompositionError

        state = _state(tmp_path, monkeypatch)

        def _broken_ceiling(st: Any, key: str, link: Any, **kw: Any) -> Any:
            raise PlatformCompositionError("governance ceiling invalid")

        monkeypatch.setattr(cr, "_resolve_channel_target", _broken_ceiling)

        with pytest.raises(PlatformCompositionError, match="governance ceiling invalid"):
            await channel_busy.notify_channel_origin_dropped(
                state, "dashboard:chat-1", ChannelLink("discord", channel_id="c1"), reason="r"
            )

    @pytest.mark.asyncio
    async def test_a_failing_transport_is_reported_as_not_sent(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        transport = _Transport(fail=True)
        monkeypatch.setattr(
            cr, "_resolve_channel_target", lambda st, key, link, **kw: (link, transport)
        )

        sent = await channel_busy.notify_channel_origin_dropped(
            state, "dashboard:chat-1", ChannelLink("discord", channel_id="c1"), reason="r"
        )

        assert sent is False
        assert transport.sent == []

    @pytest.mark.asyncio
    async def test_a_slack_thread_is_told_through_the_slack_client(
        self, tmp_path, monkeypatch
    ) -> None:
        """Slack's client is not a registered transport, so the shared ladder answers
        None for a Slack address by design; the linked-thread intercept's stamp is
        served by that client instead, at the thread the entry named."""
        state = _state(tmp_path, monkeypatch)
        state.slack_client = MagicMock(post_message=AsyncMock())

        def _never(st: Any, key: str, link: Any, **kw: Any) -> Any:
            raise AssertionError("the ladder must not be asked about a Slack address")

        monkeypatch.setattr(cr, "_resolve_channel_target", _never)

        sent = await channel_busy.notify_channel_origin_dropped(
            state,
            "dashboard:chat-1",
            ChannelLink("slack", channel_id="C1", thread_id="1700000000.000100"),
            reason="the channel conversation that queued it left the session",
        )

        assert sent is True
        state.slack_client.post_message.assert_awaited_once()
        channel_id, text, thread_ts = state.slack_client.post_message.await_args.args
        assert (channel_id, thread_ts) == ("C1", "1700000000.000100")
        assert text.startswith("⚠️ Dropped your queued message: ")
        assert "left the session" in text

    @pytest.mark.asyncio
    async def test_a_slack_thread_is_not_posted_into_when_governance_denies(
        self, tmp_path, monkeypatch
    ) -> None:
        """The Slack leg is an egress like the others: it asks the channels-scope
        governance gate the ladder asks (``chat_runner.channel_egress_permitted``,
        SEL-audited) before the client posts, so a session whose profile denies the
        ``channels``/``slack`` scope gets nothing -- the same decision, the same row,
        whichever leg carries the text."""
        state = _state(tmp_path, monkeypatch)
        state.slack_client = MagicMock(post_message=AsyncMock())
        asked: list[tuple[str, str]] = []

        def _deny(session_key: str, channel_type: str) -> bool:
            asked.append((session_key, channel_type))
            return False

        monkeypatch.setattr(cr, "channel_egress_permitted", _deny)

        sent = await channel_busy.notify_channel_origin_dropped(
            state,
            "dashboard:chat-1",
            ChannelLink("slack", channel_id="C1", thread_id="1700000000.000100"),
            reason="r",
        )

        assert sent is False
        state.slack_client.post_message.assert_not_awaited()
        assert asked == [("dashboard:chat-1", "slack")]

    @pytest.mark.asyncio
    async def test_a_slack_thread_gets_nothing_when_the_governance_gate_raises(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _state(tmp_path, monkeypatch)
        state.slack_client = MagicMock(post_message=AsyncMock())

        def _boom(session_key: str, channel_type: str) -> bool:
            raise RuntimeError("governance store unreadable")

        monkeypatch.setattr(cr, "channel_egress_permitted", _boom)

        sent = await channel_busy.notify_channel_origin_dropped(
            state,
            "dashboard:chat-1",
            ChannelLink("slack", channel_id="C1", thread_id="1.2"),
            reason="r",
        )

        assert sent is False
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_invalid_governance_ceiling_propagates_out_of_the_slack_leg(
        self, tmp_path, monkeypatch
    ) -> None:
        """The Slack leg asks the same gate directly, so the same one exception goes
        through it: an ordinary raising gate sends nothing quietly (the test above);
        an invalid ceiling is re-raised, and nothing is posted on the way out."""
        from kiro_crew.platform.context import PlatformCompositionError

        state = _state(tmp_path, monkeypatch)
        state.slack_client = MagicMock(post_message=AsyncMock())

        def _broken_ceiling(session_key: str, channel_type: str) -> bool:
            raise PlatformCompositionError("governance ceiling invalid")

        monkeypatch.setattr(cr, "channel_egress_permitted", _broken_ceiling)

        with pytest.raises(PlatformCompositionError, match="governance ceiling invalid"):
            await channel_busy.notify_channel_origin_dropped(
                state,
                "dashboard:chat-1",
                ChannelLink("slack", channel_id="C1", thread_id="1.2"),
                reason="r",
            )
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_slack_thread_gets_nothing_without_a_client_or_a_thread(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _state(tmp_path, monkeypatch)
        state.slack_client = None
        assert (
            await channel_busy.notify_channel_origin_dropped(
                state, "dashboard:chat-1", ChannelLink("slack", "C1", "t1"), reason="r"
            )
            is False
        )
        state.slack_client = MagicMock(post_message=AsyncMock(side_effect=RuntimeError("down")))
        # No thread: a Slack link without one is not a conversation the notice can reach.
        assert (
            await channel_busy.notify_channel_origin_dropped(
                state, "dashboard:chat-1", ChannelLink("slack", channel_id="C1"), reason="r"
            )
            is False
        )
        state.slack_client.post_message.assert_not_awaited()
        # A failing client is best-effort like every other leg.
        assert (
            await channel_busy.notify_channel_origin_dropped(
                state, "dashboard:chat-1", ChannelLink("slack", "C1", "t1"), reason="r"
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_the_drain_delivers_the_drop_to_the_channel_end_to_end(
        self, tmp_path, monkeypatch
    ) -> None:
        """Hand-off, binding released while queued, drain: the conversation that
        typed the message receives the drop notice with the reason, at the address
        the entry carried."""
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        probes = iter(["discord:c1:", ""])
        monkeypatch.setattr(
            "kiro_crew.dashboard.session_control._probe_channel_mirror",
            lambda st, sl: next(probes, ""),
        )
        transport = _Transport()
        asked: list[Any] = []

        def _resolve(st: Any, key: str, link: Any, *, principal: Any = "unset") -> Any:
            asked.append(principal)
            return link, transport

        monkeypatch.setattr(cr, "_resolve_channel_target", _resolve)
        hand_to_dashboard_turn(
            state,
            "dashboard:chat-1",
            "and the weather?",
            origin=ChannelLink("discord", channel_id="c1"),
            principal="u1",
        )
        try:
            cr._drop_stale_admissions(state, slot)
            # The notice hops off-loop (``asyncio.to_thread``) and back; give the
            # fire-and-forget task the turns it needs.
            for _ in range(20):
                await asyncio.sleep(0)
                if transport.sent:
                    break
            else:
                await asyncio.sleep(0.05)
        finally:
            pending.cancel()

        assert slot._queue == []
        assert len(transport.sent) == 1
        channel_id, text, thread_id = transport.sent[0]
        assert (channel_id, thread_id) == ("c1", None)
        assert "left the session" in text
        # The ladder judged the DM against the user the dispatcher admitted, not
        # against the dashboard key's empty principal.
        assert asked == ["u1"]


class TestChannelTextIsProseOnTheDashboard:
    """The channel's own command intercept already ran everything that conversation
    may command. What it forwarded is what its user meant the model to READ, so the
    dashboard must not re-read it as a command on the owner's authority."""

    def test_a_channel_message_naming_a_dashboard_command_is_not_one(self) -> None:
        assert dashboard_command_word("/workflow deploy prod", channel_origin=True) == ""
        assert dashboard_command_word("/goal ship it", channel_origin=True) == ""

    def test_composer_text_keeps_its_command_word(self) -> None:
        assert dashboard_command_word("/workflow deploy prod", channel_origin=False) == "/workflow"
        assert dashboard_command_word("  ", channel_origin=False) == ""
        assert dashboard_command_word("just prose", channel_origin=False) == "just"

    def test_a_suppressed_command_is_named_so_its_author_can_be_told(self) -> None:
        """Prose is the right reading, silence is not: the token the dashboard would
        have run from its own composer is reported back so the turn can refuse it
        with a notice instead of streaming ``/compact`` to the model as text."""
        assert (
            suppressed_channel_command("/compact", channel_origin=True, cc_provider=False)
            == "/compact"
        )
        assert (
            suppressed_channel_command("/goal ship it", channel_origin=True, cc_provider=False)
            == "/goal"
        )
        # Under claude_code every leading slash is a harness command from the composer.
        assert (
            suppressed_channel_command("/review this", channel_origin=True, cc_provider=True)
            == "/review"
        )

    def test_a_quick_prompt_is_a_prose_shortcut_not_a_command(self) -> None:
        """``/plain summarise the log`` from a linked thread is routed as prose on
        every provider: the token is in no command set (``is_harness_slash_command``
        excludes it even under claude_code) and ``ContextBuilder.build_message``
        expands it for every surface alike, so there is nothing to refuse."""
        for cc_provider in (False, True):
            assert (
                suppressed_channel_command(
                    "/plain summarise the log", channel_origin=True, cc_provider=cc_provider
                )
                == ""
            ), cc_provider
            assert (
                suppressed_channel_command("/PLAIN", channel_origin=True, cc_provider=cc_provider)
                == ""
            )

    def test_nothing_is_suppressed_for_prose_or_for_the_composer(self) -> None:
        # Channel prose, including a leading token no surface reads as a command.
        assert (
            suppressed_channel_command("hello there", channel_origin=True, cc_provider=False) == ""
        )
        assert (
            suppressed_channel_command(
                "/usr/bin/python3 fails", channel_origin=True, cc_provider=False
            )
            == ""
        )
        assert suppressed_channel_command("   ", channel_origin=True, cc_provider=False) == ""
        # The composer's own text is never suppressed: it keeps its command word.
        assert suppressed_channel_command("/compact", channel_origin=False, cc_provider=False) == ""
        assert suppressed_channel_command("/review", channel_origin=False, cc_provider=True) == ""

    def test_a_path_shaped_token_is_prose_even_under_claude_code(self) -> None:
        """claude_code reads every leading slash from the composer as a harness
        command, so the channel gate would refuse a message that merely opens with a
        path. A second ``/`` or a ``.`` past the first segment is a file, not a
        command anyone has: it stays prose. A bare command word is still named."""
        for token in ("/usr/bin/python3 --version", "/etc/hosts", "/setup.py fails", "/a.b"):
            assert suppressed_channel_command(token, channel_origin=True, cc_provider=True) == ""
        assert (
            suppressed_channel_command("/review this", channel_origin=True, cc_provider=True)
            == "/review"
        )
        assert (
            suppressed_channel_command("/compact", channel_origin=True, cc_provider=True)
            == "/compact"
        )

    def test_the_notice_names_the_token(self) -> None:
        notice = CHANNEL_COMMAND_UNAVAILABLE.format(command="/compact")
        assert "`/compact` is not available from a linked conversation" in notice
        assert "was not sent" in notice

    def test_the_named_token_is_redacted_and_bounded(self) -> None:
        """Under claude_code the refused token is the channel user's own text, so
        it is named the way the intercept names their message -- through the same
        redactors -- and cut so a pasted blob is not repeated whole."""
        assert displayable_channel_command("/compact") == "/compact"
        shown = displayable_channel_command("/ghp_" + "A" * 36)
        assert "ghp_" + "A" * 36 not in shown
        assert "[REDACTED" in shown
        long = displayable_channel_command("/" + "x" * 500)
        assert len(long) == CHANNEL_COMMAND_TOKEN_MAX
        assert long.endswith("…")


class TestARecoveryOfAHandOffKeepsItsConversation:
    """A runner recovery of a handed-off turn is still that conversation's message.

    ``_queue_recovery`` requeues the sender's own words under a fresh queue id with
    both provenance flags. Before this the requeued entry carried no conversation
    stamp -- the drained row's meta never does -- so once the binding was released
    it was neither dropped nor reported and ran in the session the conversation had
    left. The drain hands the stamp to the turn as a parameter and the
    recovery requeue stamps it back, so the drop-and-notify contract covers the
    retry too.
    """

    @pytest.mark.asyncio
    async def test_the_drain_hands_the_conversation_to_the_turn(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _state(tmp_path, monkeypatch)
        # Bound at admission and still bound at the drain, so the entry drains.
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="c1")
        )
        slot, pending = await _running_slot(state)
        hand_to_dashboard_turn(
            state,
            "dashboard:chat-1",
            "and the weather?",
            origin=ChannelLink("discord", channel_id="c1"),
            principal="u1",
        )
        pending.cancel()
        slot.task = None
        runs: list[dict[str, Any]] = []

        def _run(state_: Any, slot_: Any, message: str, **kwargs: Any) -> Any:
            runs.append({"message": message, **kwargs})
            return MagicMock()

        monkeypatch.setattr(cr, "_run_chat", _run)
        monkeypatch.setattr(cr, "spawn_guarded_turn", lambda *a, **kw: MagicMock())

        assert await cr._start_next_queued_turn(state, slot) is True

        assert runs and runs[0]["message"] == "and the weather?"
        assert runs[0]["_channel_origin"] == {
            "channel_type": "discord",
            "channel_id": "c1",
            "thread_id": None,
            CHANNEL_ORIGIN_PRINCIPAL_KEY: "u1",
        }
        # The stamp is a parameter, not a row field: the drained row's meta is clean.
        row = next(
            m for m in slot.messages if m.get("role") == "user" and "weather" in m["content"]
        )
        assert CHANNEL_ORIGIN_META_KEY not in (row.get("meta") or {})

    @pytest.mark.asyncio
    async def test_a_composer_entry_hands_no_conversation(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _state(tmp_path, monkeypatch)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = state.get_or_create_slot("chat-1")
        queue_for_next_turn(state, slot, "composer text", directive_user_origin=True)
        runs: list[dict[str, Any]] = []

        def _run(state_: Any, slot_: Any, message: str, **kwargs: Any) -> Any:
            runs.append({"message": message, **kwargs})
            return MagicMock()

        monkeypatch.setattr(cr, "_run_chat", _run)
        monkeypatch.setattr(cr, "spawn_guarded_turn", lambda *a, **kw: MagicMock())

        assert await cr._start_next_queued_turn(state, slot) is True
        assert runs and "_channel_origin" not in runs[0]

    @pytest.mark.asyncio
    async def test_the_recovery_requeue_carries_the_conversation(
        self, tmp_path, monkeypatch
    ) -> None:
        """Drive the real ``_run_chat`` into a recovery: the backend dies during
        setup and the turn is requeued. The requeued ENTRY names the conversation
        beside the fresh admission stamp the recovery records (the row it drains
        into keeps queue plumbing off, so the entry is what is inspected)."""
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.dashboard.state import _ChatSlot

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(side_effect=AcpProcessDied("died in setup"))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        # Still bound: the requeued hand-off passes the drain (the unbound case is
        # the next test's).
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="c1")
        )
        state.sessions.record_failure = AsyncMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()
        slot = state.get_or_create_slot("chat-1")
        slot.append("user", "and the weather?", "msg msg-u")
        stamp = {
            "channel_type": "discord",
            "channel_id": "c1",
            "thread_id": None,
            "principal": "u1",
        }
        inserted: list[dict[str, Any]] = []
        real_insert = _ChatSlot.queue_insert

        def _recording_insert(self_: Any, *args: Any, **kwargs: Any) -> str:
            qid = real_insert(self_, *args, **kwargs)
            inserted.append(dict(next(i for i in self_._queue if i["id"] == qid)))
            return qid

        monkeypatch.setattr(_ChatSlot, "queue_insert", _recording_insert)

        await cr._run_chat(
            state,
            slot,
            "and the weather?",
            _directive_user_origin=True,
            _directive_channel_origin=True,
            _channel_origin=stamp,
        )

        assert inserted, "the failure never reached a requeue -- the test proves nothing"
        entry = inserted[0]
        assert entry["content"] == "and the weather?"
        assert entry.get("_directive_channel_origin") is True
        meta = entry.get("meta") or {}
        assert meta.get(CHANNEL_ORIGIN_META_KEY) == stamp
        assert meta[CHANNEL_ORIGIN_META_KEY] is not stamp, "the requeue aliased the turn's stamp"
        assert QUEUED_CONTAINMENT_META_KEY in meta
        link = channel_origin_address(meta)
        assert link is not None and (link.channel_type, link.channel_id) == ("discord", "c1")
        assert channel_origin_principal(meta) == "u1"
        # And it drained: the row keeps the plumbing off, exactly as the original does.
        requeued = [m for m in slot.messages if m.get("role") == "inject"]
        assert requeued and CHANNEL_ORIGIN_META_KEY not in (requeued[0].get("meta") or {})

    @pytest.mark.asyncio
    async def test_a_requeued_hand_off_is_released_and_reported_like_the_original(
        self, tmp_path, monkeypatch
    ) -> None:
        """The end of the chain the stamp exists for: a recovery entry on a slot whose
        binding went away while the retry waited is dropped at the drain and the
        conversation is told -- the same contract the original entry has."""
        state = _state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")
        # What ``_queue_recovery`` inserts for a handed-off turn: the fresh admission
        # stamp (mirrored at requeue) and the conversation, with both flags.
        probes = iter(["discord:c1:", ""])
        monkeypatch.setattr(
            "kiro_crew.dashboard.session_control._probe_channel_mirror",
            lambda st, sl: next(probes, ""),
        )
        from kiro_crew.dashboard.chat_utils import SYNTHETIC_RECOVERY_KIND, RecoveryPayload
        from kiro_crew.dashboard.session_control import containment_meta

        slot.queue_insert(
            0,
            "and the weather?",
            kind=SYNTHETIC_RECOVERY_KIND,
            payload=RecoveryPayload.ORIGINAL,
            meta={
                **containment_meta(state, slot),
                CHANNEL_ORIGIN_META_KEY: {"channel_type": "discord", "channel_id": "c1"},
            },
            directive_user_origin=True,
            directive_channel_origin=True,
        )
        told: list[tuple[str, dict, str]] = []

        async def _notify(
            st: Any, session_key: str, origin: Any, *, reason: str, principal: str = ""
        ) -> bool:
            told.append((session_key, origin.to_dict(), reason))
            return True

        monkeypatch.setattr(channel_busy, "notify_channel_origin_dropped", _notify)

        cr._drop_stale_admissions(state, slot)
        await asyncio.sleep(0)

        assert slot._queue == []
        assert told and told[0][1]["channel_id"] == "c1" and "left the session" in told[0][2]
