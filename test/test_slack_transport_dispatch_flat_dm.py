"""Slack transport dispatch: one session per 1:1 DM (``slack.dm_single_session``).

Covers ``flat_dm_session_key`` (which conversations collapse to one
channel-keyed session and which never do) and its wiring into
``handle_message_transport``: the key a turn runs under, also when the thread
is already claimed or linked to the dashboard, where the reply is posted,
whether the turn claims a thread, and how the ``!incognito`` and
``!temporary`` modifiers link and confirm in a DM. The message timestamp lives
in ``slack_transport_dispatch_helpers.py``, which also binds the golden-transcript
fakes from ``test_slack_golden_transcript``.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from unittest.mock import AsyncMock

import pytest
from slack_transport_dispatch_helpers import (
    _MSG_TS,
    FakeSessions,
    RecordingSlackClient,
    ScriptedProvider,
    make_event,
)

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN
from kiro_crew.messaging.link import canonical_key
from kiro_crew.session import BACKGROUND_KEY
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack import transport_dispatch


class _LinkCapturingSessions(FakeSessions):
    """FakeSessions that records acquired session keys and Slack link writes."""

    def __init__(self, provider):
        super().__init__(provider)
        self.keys: list = []
        self.links: list = []
        # Who the thread index says owns this thread; None = unclaimed.
        self.thread_owner: str | None = None

    def get_session_for_thread(self, thread_ts):
        return self.thread_owner

    async def get_or_create(self, session_key, agent=None, channel_id=None, start_priority=None):
        self.keys.append(session_key)
        return await super().get_or_create(session_key, agent=agent, channel_id=channel_id)

    @property
    def turn_keys(self) -> list:
        """The keys real TURNS ran under.

        The auto-title that follows a successful turn acquires the shared
        ``BACKGROUND_KEY`` session to name the conversation. That is not a
        routing decision this feature has any say over, so it is excluded here
        rather than pinned into every assertion below.
        """
        return [k for k in self.keys if k != BACKGROUND_KEY]

    def set_slack_link(self, key, thread_ts, channel_id):
        # Deliberately NOT delegating to super(): the base fake stores links in a
        # dict under this same attribute, so with the list used here a real link
        # write raised TypeError and the assertions below failed for the wrong
        # reason instead of showing the offending link.
        self.links.append((key, thread_ts, channel_id))


class TestFlatDmSessionKey:
    """``slack.dm_single_session``: one session per 1:1 DM, replies at channel root."""

    _DM = "D0AP0870FFH"

    @pytest.mark.parametrize(
        ("channel", "thread_ts", "enabled", "expected"),
        [
            pytest.param(_DM, None, False, None, id="test_disabled_keeps_the_per_message_key"),
            pytest.param(_DM, None, True, f"slack:{_DM}", id="test_top_level_dm_keys_by_channel"),
            # In a 1:1 DM a thread is a layout habit, not a new topic: splitting it
            # off would leave the branch without the conversation it replies to.
            pytest.param(
                _DM,
                "1700000000.000001",
                True,
                f"slack:{_DM}",
                id="test_a_threaded_reply_in_a_dm_keys_by_channel_too",
            ),
            pytest.param("C1", None, True, None, id="test_a_group_channel_never_collapses"),
            # An mpim is shared with other people, so it may not become one session.
            pytest.param("G1", None, True, None, id="test_a_group_dm_never_collapses"),
        ],
    )
    def test_flat_dm_session_key(self, channel, thread_ts, enabled, expected):
        assert (
            transport_dispatch.flat_dm_session_key(channel, thread_ts, enabled=enabled) == expected
        )


class TestFlatDmTransportWiring:
    def _prep(self, monkeypatch):
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(
            transport_dispatch, "_hydrate_thread_overrides", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

    def _provider(self):
        return ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text="hi"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )

    def _run(self, monkeypatch, *, channel, thread_ts, enabled, text="do it"):
        self._prep(monkeypatch)
        slack = RecordingSlackClient()
        sessions = _LinkCapturingSessions(self._provider())
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel=channel,
                text=text,
                thread_ts=thread_ts,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=None,
                dm_single_session=enabled,
            )
        )
        return slack, sessions

    @pytest.mark.parametrize(
        ("channel", "thread_ts", "enabled", "expected"),
        [
            # HARD INVARIANT: off by default, the DM key is byte-for-byte unchanged.
            pytest.param(
                "D0AP0870FFH",
                None,
                False,
                canonical_key(_MSG_TS),
                id="test_dm_key_unchanged_when_disabled",
            ),
            pytest.param(
                "D0AP0870FFH",
                None,
                True,
                "slack:D0AP0870FFH",
                id="test_enabled_runs_the_turn_under_the_channel_key",
            ),
            pytest.param(
                "D0AP0870FFH",
                "1700000000.000001",
                True,
                "slack:D0AP0870FFH",
                id="test_a_threaded_dm_reply_joins_the_same_session",
            ),
            pytest.param(
                "C1",
                None,
                True,
                canonical_key(_MSG_TS),
                id="test_a_group_channel_is_unaffected_when_enabled",
            ),
        ],
    )
    def test_turn_key(self, monkeypatch, channel, thread_ts, enabled, expected):
        _slack, sessions = self._run(
            monkeypatch, channel=channel, thread_ts=thread_ts, enabled=enabled
        )
        assert sessions.turn_keys == [expected]

    @pytest.mark.parametrize(
        "thread_ts",
        [
            # The session is keyed by the channel, so linking it to this message's ts
            # would hand the dashboard mirror a thread to post into while the
            # conversation itself is flat.
            pytest.param(None, id="test_enabled_does_not_claim_a_thread"),
            # Several threads would each overwrite the session's scalar
            # slack_thread_ts, so the dashboard mirror would follow whichever spoke
            # last. Routing needs no claim: the flat key is derived from the channel.
            pytest.param("1700000000.000001", id="test_a_threaded_dm_reply_claims_no_thread"),
        ],
    )
    def test_flat_dm_claims_no_thread(self, monkeypatch, thread_ts):
        _slack, sessions = self._run(
            monkeypatch, channel="D0AP0870FFH", thread_ts=thread_ts, enabled=True
        )
        assert sessions.links == []

    def test_enabled_posts_at_channel_root(self, monkeypatch):
        slack, _sessions = self._run(
            monkeypatch, channel="D0AP0870FFH", thread_ts=None, enabled=True
        )
        posted = [kw for m, kw in slack.transcript if m in ("post_message", "post_blocks")]
        assert posted, "the turn posted nothing"
        # Flat means no thread: a thread_ts here would bury the reply in a thread.
        assert all(kw["thread_ts"] is None for kw in posted)

    def test_a_threaded_dm_reply_still_answers_inside_its_thread(self, monkeypatch):
        # The session merged; the layout did not. Answering at channel root would
        # strand the reply away from the question it answers.
        thread_ts = "1700000000.000001"
        slack, _sessions = self._run(
            monkeypatch, channel="D0AP0870FFH", thread_ts=thread_ts, enabled=True
        )
        posted = [kw for m, kw in slack.transcript if m in ("post_message", "post_blocks")]
        assert posted, "the turn posted nothing"
        assert all(kw["thread_ts"] == thread_ts for kw in posted)

    def test_a_thread_claimed_before_the_flag_does_not_split_the_dm(self, monkeypatch):
        # A thread claimed by its own per-thread session (the shape this feature
        # replaces, e.g. from before the flag was on) must not pull the turn back
        # out of the merged conversation.
        self._prep(monkeypatch)
        thread_ts = "1700000000.000001"
        slack = RecordingSlackClient()
        sessions = _LinkCapturingSessions(self._provider())
        sessions.thread_owner = canonical_key(thread_ts)
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="D0AP0870FFH",
                text="do it",
                thread_ts=thread_ts,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=None,
                dm_single_session=True,
            )
        )
        assert sessions.turn_keys == ["slack:D0AP0870FFH"]

    def test_a_dashboard_linked_thread_still_wins(self, monkeypatch):
        # Link-to-Dashboard is a real binding to somewhere else, not the shape
        # this feature replaces, so it keeps ownership of its thread.
        self._prep(monkeypatch)
        thread_ts = "1700000000.000001"
        slack = RecordingSlackClient()
        sessions = _LinkCapturingSessions(self._provider())
        sessions.thread_owner = "chat-7-1700000000"
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="D0AP0870FFH",
                text="do it",
                thread_ts=thread_ts,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=None,
                dm_single_session=True,
            )
        )
        assert sessions.turn_keys == ["chat-7-1700000000"]

    def _clear_modifier_state(self, monkeypatch):
        # Both modifiers are idempotent through module-level LRUs keyed by
        # session_key, so a second test reusing the same DM would short-circuit
        # before reaching the code under test.
        monkeypatch.setattr(slack_handler, "_thread_temporary", OrderedDict())
        monkeypatch.setattr(slack_handler, "_thread_incognito", OrderedDict())

    @pytest.mark.parametrize("token", ["!incognito", "!temporary"])
    def test_a_privacy_modifier_in_a_flat_dm_claims_no_thread(self, monkeypatch, token):
        # The modifiers call set_slack_link themselves. Reached with this
        # message's ts they bind the channel-keyed session to a thread, which
        # reroutes later threaded replies into the flat session and hands the
        # dashboard mirror a thread to post into -- the exact claim the
        # self-link guard refuses for a flat DM.
        self._clear_modifier_state(monkeypatch)
        _slack, sessions = self._run(
            monkeypatch,
            channel="D0AP0870FFH",
            thread_ts=None,
            enabled=True,
            text=f"{token} do it",
        )
        assert sessions.turn_keys == ["slack:D0AP0870FFH"]
        assert sessions.links == []

    @pytest.mark.parametrize("token", ["!incognito", "!temporary"])
    def test_a_privacy_modifier_in_a_flat_dm_confirms_at_channel_root(self, monkeypatch, token):
        self._clear_modifier_state(monkeypatch)
        slack, _sessions = self._run(
            monkeypatch,
            channel="D0AP0870FFH",
            thread_ts=None,
            enabled=True,
            text=f"{token} do it",
        )
        posted = [kw for m, kw in slack.transcript if m in ("post_message", "post_blocks")]
        assert posted, "the turn posted nothing"
        # Includes the modifier's own confirmation: a flat DM has no thread, and
        # thread_ts="" would be forwarded to Slack verbatim rather than omitted.
        assert all(kw["thread_ts"] is None for kw in posted)

    @pytest.mark.parametrize("token", ["!incognito", "!temporary"])
    def test_a_privacy_modifier_in_a_dm_thread_claims_no_thread_either(self, monkeypatch, token):
        # The modifiers call set_slack_link themselves, so the flat session would
        # get bound to whichever thread last carried a modifier.
        self._clear_modifier_state(monkeypatch)
        _slack, sessions = self._run(
            monkeypatch,
            channel="D0AP0870FFH",
            thread_ts="1700000000.000001",
            enabled=True,
            text=f"{token} do it",
        )
        assert sessions.turn_keys == ["slack:D0AP0870FFH"]
        assert sessions.links == []

    @pytest.mark.parametrize("token", ["!incognito", "!temporary"])
    def test_a_privacy_modifier_in_a_dm_thread_confirms_in_that_thread(self, monkeypatch, token):
        self._clear_modifier_state(monkeypatch)
        thread_ts = "1700000000.000001"
        slack, _sessions = self._run(
            monkeypatch,
            channel="D0AP0870FFH",
            thread_ts=thread_ts,
            enabled=True,
            text=f"{token} do it",
        )
        posted = [kw for m, kw in slack.transcript if m in ("post_message", "post_blocks")]
        assert posted, "the turn posted nothing"
        # Includes the modifier's own confirmation: not linking a thread must not
        # cost us answering in it.
        assert all(kw["thread_ts"] == thread_ts for kw in posted)

    @pytest.mark.parametrize("token", ["!incognito", "!temporary"])
    def test_a_privacy_modifier_claims_the_thread_when_the_flag_is_off(self, monkeypatch, token):
        # Preserved behaviour: a thread-scoped session registers its thread so
        # follow-ups pass the mention/observe in_active_thread gate.
        self._clear_modifier_state(monkeypatch)
        thread_ts = "1700000000.000001"
        _slack, sessions = self._run(
            monkeypatch,
            channel="D0AP0870FFH",
            thread_ts=thread_ts,
            enabled=False,
            text=f"{token} do it",
        )
        assert (canonical_key(thread_ts), thread_ts, "D0AP0870FFH") in sessions.links
