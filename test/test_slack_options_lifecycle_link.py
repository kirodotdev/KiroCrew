"""OPTIONS controls across a thread link, and several outstanding at once.

This file pins the link-time backfill that replays context into a freshly linked
thread, the refusal of a slot key that is not a real string, unlinking that expires
the control rather than orphaning its record, and several outstanding controls: a
newer one never displaces the record of an older live one, every outstanding one is
expired, and answering one forgets only that one. The two behaviours this family
locks in are described in ``test_slack_options_lifecycle.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, drain_background_tasks
from slack_options_lifecycle_helpers import (
    _context_text,
    _is_live_control,
    _recs,
    _set_recs,
    _slack_app,
)

from kiro_crew.slack.outbound import PostedOptions


class TestLinkTimeBackfill:
    """Replaying context into a freshly-linked thread."""

    @pytest.mark.asyncio
    async def test_the_replay_is_a_background_task_the_caller_must_await(
        self, tmp_path, monkeypatch
    ):
        """Pins WHY every case below drains: the handler answers before it posts.

        The endpoint returns 200 as soon as the link is persisted and hands the
        replay to ``asyncio.create_task``, so whether the replay has run by the time
        the response is read depends only on how the loop was scheduled. Asserting on
        the replay without awaiting it is a load-dependent coin flip, and it failed
        as ``'NoneType' object has no attribute 'args'`` on a DIFFERENT test each CI
        run — which reads as a flaky suite rather than a missing await.

        If this ever fails because the handler became synchronous, delete the drains
        rather than weakening this.
        """
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "Your call.\n\n[OPTIONS: A | B]")
        slot.drain()

        # GATE the replay's own call, so "still pending" is a fact rather than a bet on
        # the scheduler. Asserting it bare would be the same race inverted: it happens
        # to hold because the replay suspends on a thread-pool hop that usually
        # outlasts the loopback response, and it would flake on a host where the warm
        # executor wins -- in the one test that documents why the drains exist.
        release = asyncio.Event()

        async def _gated_post_blocks(*_args, **_kwargs):
            # flake-ok: pre-existing gate on the replay's own post, released by the test right after its pending-task check; moved verbatim and tracked for a de-flake pass
            await release.wait()
            return "opt_ts"

        state.slack_client.post_blocks = AsyncMock(side_effect=_gated_post_blocks)

        async with TestClient(TestServer(_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 200
            assert [t for t in state._background_tasks if not t.done()], (
                "the replay must still be pending here, or these tests are not "
                "exercising the background path the gateway takes"
            )
            release.set()
            await drain_background_tasks(state)
            assert not [t for t in state._background_tasks if not t.done()]
        assert state.slack_client.post_blocks.await_args is not None

    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.slack_client = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="C1")
        state.slack_client.post_message = AsyncMock(return_value="ts1")
        state.slack_client.post_blocks = AsyncMock(return_value="opt_ts")
        state.owner_id = "U1"
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.sessions.set_slack_link = MagicMock()
        state.push_slots_update = MagicMock()
        return state

    @pytest.mark.asyncio
    async def test_replayed_options_are_a_control_not_literal_text(self, tmp_path, monkeypatch):
        """The backfill posted bodies verbatim, so the tag arrived as text.

        This is the path that carries the reply when the Slack link is created
        after the turn already finished — the mirror has nothing to send by
        then, so the backfill is the only thing that puts the answer in Slack.
        """
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "which one?")
        slot.append("assistant", "Your call.\n\n[OPTIONS: Ship it | Hold off]")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 200
            await drain_background_tasks(state)

        texts = [c.args[1] for c in state.slack_client.post_message.await_args_list]
        assert all("[OPTIONS:" not in t for t in texts)
        blocks = state.slack_client.post_blocks.await_args.args[1]
        assert _is_live_control(blocks)

    @pytest.mark.asyncio
    async def test_the_newest_reply_stays_answerable_and_is_recorded(self, tmp_path, monkeypatch):
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "Latest.\n\n[OPTIONS: A | B]")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})
            await drain_background_tasks(state)

        assert _is_live_control(state.slack_client.post_blocks.await_args.args[1])
        assert _recs(state, slot)
        assert _recs(state, slot)[0].choices == ("A", "B")

    @pytest.mark.asyncio
    async def test_a_superseded_question_is_replayed_spent(self, tmp_path, monkeypatch):
        """The user already answered it — replaying it live would re-ask it."""
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "Older.\n\n[OPTIONS: A | B]")
        slot.append("user", "A")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})
            await drain_background_tasks(state)

        blocks = state.slack_client.post_blocks.await_args.args[1]
        assert not _is_live_control(blocks)
        assert _context_text(blocks) == "~A~  |  ~B~"
        assert _recs(state, slot) == ()

    @pytest.mark.asyncio
    async def test_a_trailing_system_row_does_not_spend_the_newest_reply(
        self, tmp_path, monkeypatch
    ):
        """The transcript holds rows that are never replayed.

        A completed turn appends one, so the last reply is not the last row.
        Judging "newest" by raw position spent the very control the replay
        exists to deliver — the user saw a struck-through question again.
        """
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "Your call.\n\n[OPTIONS: Ship it | Hold off]")
        slot.append("done", "turn complete")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})
            await drain_background_tasks(state)

        blocks = state.slack_client.post_blocks.await_args.args[1]
        assert _is_live_control(blocks)
        assert _recs(state, slot)

    @pytest.mark.asyncio
    async def test_a_users_own_options_syntax_survives_the_replay(self, tmp_path, monkeypatch):
        """A person can type the OPTIONS syntax — quoting it, or discussing it.

        Routing user rows through the agent-authored path lifted the tag out of
        their words and rendered it as struck-through choices they never offered.
        Their text has to come back verbatim.
        """
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        # The tag must END the message: the parser is line-anchored, so trailing
        # words after it would make this pass whatever the code did.
        slot.append("user", "should I paste this?\n\n[OPTIONS: A | B]")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})
            await drain_background_tasks(state)

        texts = [c.args[1] for c in state.slack_client.post_message.await_args_list]
        assert any("[OPTIONS: A | B]" in t for t in texts)
        state.slack_client.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_only_the_newest_reply_is_answerable(self, tmp_path, monkeypatch):
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "First.\n\n[OPTIONS: A | B]")
        slot.append("user", "A")
        slot.append("assistant", "Second.\n\n[OPTIONS: C | D]")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})
            await drain_background_tasks(state)

        posts = state.slack_client.post_blocks.await_args_list
        assert len(posts) == 2
        assert not _is_live_control(posts[0].args[1])
        assert _is_live_control(posts[1].args[1])


class TestNonStringKeyIsRefused:
    """A key that is not a real string must never reach slot-key normalization.

    ``_normalize_slot_key`` strips a repeated ``dashboard_`` prefix with an
    unbounded ``while`` loop, which terminates only for a genuine ``str``.
    Anything whose ``startswith`` is always truthy -- a MagicMock standing in
    for a Slack payload field, for instance -- spins forever AND manufactures a
    fresh child object every iteration. Silent, so no test fails and no timeout
    trips; it simply eats memory until the process dies. On CI that presented
    as a runner shutdown at a random point in the shard with zero FAILED lines.

    The click path reaches this lookup with a thread id taken straight off the
    interaction payload, so the type is not guaranteed at the call site -- the
    guard belongs here.
    """

    @pytest.mark.timeout(5)
    def test_a_non_string_key_returns_none_instead_of_spinning(self):
        from kiro_crew.dashboard import chat_utils

        state = MagicMock()

        assert chat_utils.slack_options_slot(state, MagicMock()) is None
        # Refused before any lookup: proves we never entered normalization.
        state.get_slot.assert_not_called()

    @pytest.mark.timeout(5)
    def test_a_real_string_key_still_reaches_the_lookup(self):
        from kiro_crew.dashboard import chat_utils

        state = MagicMock()
        sentinel = object()
        state.get_slot.return_value = sentinel

        assert chat_utils.slack_options_slot(state, "chat-1-123") is sentinel
        state.get_slot.assert_called_once()


class TestUnlinkSpendsTheControl:
    """Unlinking must EXPIRE the OPTIONS control, not merely orphan the record.

    Two failure modes, both closed by expiring rather than forgetting:

    1. Forget-only leaves the buttons live in Slack. After the link is gone a
       click answers a question from a conversation this thread is not
       attached to, landing that stale answer in a brand-new session.
    2. Doing nothing leaves the record unreachable once the thread -> slot
       reverse index is popped, so the next dashboard turn's expiry strikes every
       choice through — erasing an answer the user had already given.

    Expiry does both halves: it strikes the choices through in Slack AND clears
    the record. It has to run before the link is torn down, because popping the
    index is what makes the record unreachable.
    """

    @pytest.mark.asyncio
    async def test_unlink_expires_a_live_options_control(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_unlink

        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        # Returns into the JSON body, so it has to be a real bool not a mock.
        state.sessions.clear_slack_link = MagicMock(return_value=True)
        slack = MagicMock()
        slack.update_message = AsyncMock()
        slack.post_message = AsyncMock()
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "thread-1"
        state._slack_to_slot["thread-1"] = slot.key

        _set_recs(
            state,
            slot,
            (
                PostedOptions(
                    channel="C-1",
                    ts="opt-1",
                    choices=("A", "B"),
                    blocks=(),
                ),
            ),
        )

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/slack-unlink")
            assert resp.status == 200

        # The buttons are struck through in Slack, so a click after the unlink
        # cannot inject a stale answer into a new session.
        slack.update_message.assert_awaited_once()
        assert slack.update_message.await_args.args[0] == "C-1"
        assert slack.update_message.await_args.args[1] == "opt-1"
        # And the record is gone, so a later dashboard turn has nothing to strike
        # through and cannot erase the user's selection.
        assert _recs(state, slot) == ()
        # The reverse index is still dropped — spending the control must not come
        # at the cost of the thread continuing to resolve here.
        assert "thread-1" not in state._slack_to_slot

    @pytest.mark.asyncio
    async def test_a_relink_during_the_expiry_await_is_not_clobbered(self, tmp_path, monkeypatch):
        """Unlink must not tear down a link that replaced the one it captured.

        Neither plain ordering is safe. Tearing down BEFORE expiry leaves the
        buttons live with the reverse index already gone, so a click resolves to
        nothing and starts a brand-new session with a stale answer. Expiring
        first, then tearing down unconditionally, lets a relink that landed during
        the (Slack-bound) await get its in-memory fields wiped while its persisted
        link survives. Compare-and-clear is the only ordering that closes both, so
        this test pins the relink half.
        """
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_unlink

        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        state.sessions.clear_slack_link = MagicMock(return_value=True)
        slack = MagicMock()
        slack.post_message = AsyncMock()
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "thread-1"
        state._slack_to_slot["thread-1"] = slot.key

        # Another tab relinks the slot to a NEW thread while expiry awaits Slack.
        async def _relink_midway(_state, _session_key):
            slot._slack_linked = True
            slot._slack_channel = "C-2"
            slot._slack_thread_ts = "thread-2"
            _state._slack_to_slot["thread-2"] = slot.key

        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.expire_slack_options", _relink_midway)

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/slack-unlink")
            assert resp.status == 200

        # The replacement link survives intact -- fields AND reverse index.
        assert slot._slack_linked is True
        assert slot._slack_channel == "C-2"
        assert slot._slack_thread_ts == "thread-2"
        assert state._slack_to_slot.get("thread-2") == slot.key

    @pytest.mark.asyncio
    async def test_an_unraced_unlink_still_tears_the_link_down(self, tmp_path, monkeypatch):
        """The other half: with no relink, persistence stays clear and teardown completes.

        Guards the opposite failure from the test above -- treating every equal
        comparison as "a relink landed" would turn every ordinary unlink into a
        silent no-op.
        """
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_unlink
        from kiro_crew.dashboard.chat_utils import effective_session_key

        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.post_message = AsyncMock()
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "thread-1"
        state._slack_to_slot["thread-1"] = slot.key
        session_key = effective_session_key(slot)
        state.sessions.set_slack_link(session_key, "thread-1", "C-1")

        async def _no_relink(_state, _session_key, ts=None):
            return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.expire_slack_options", _no_relink)

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/slack-unlink")
            assert resp.status == 200
            assert (await resp.json())["was_linked"] is True

        assert slot._slack_linked is False
        assert slot._slack_channel == ""
        assert "thread-1" not in state._slack_to_slot
        assert state.sessions.get_slack_link(session_key) in (
            (None, None),
            ("", ""),
        ), "an unraced unlink must leave persistence clear"


class TestEveryOutstandingControlIsExpired:
    """A newer control must not displace the record of an older live one.

    One turn can post more than one OPTIONS message, and a single slot is
    reachable from several posting paths. When the record was a single slot the
    newer post overwrote the older one, so the older control stayed on screen
    with nothing tracking it — clicking it answered a question the conversation
    had already moved past, and the answer landed as if it were current.
    """

    @pytest.mark.asyncio
    async def test_expiry_drains_all_recorded_controls(self, tmp_path):
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.update_message = AsyncMock()
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")

        first = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        second = PostedOptions(channel="C-1", ts="opt-2", choices=("B",), blocks=())
        remember_slack_options(state, slot.key, first)
        remember_slack_options(state, slot.key, second)

        # Both are tracked — the second did not displace the first.
        assert _recs(state, slot) == (first, second)

        await expire_slack_options(state, slot.key)

        edited = {call.args[1] for call in slack.update_message.await_args_list}
        assert edited == {"opt-1", "opt-2"}, edited
        assert _recs(state, slot) == ()

    @pytest.mark.asyncio
    async def test_a_cron_control_is_tracked_and_spent_under_its_session_key(self, tmp_path):
        """A cron slot's name is not its session key folded, so it must still resolve.

        A persistent cron runs on ``cron:<id>`` but its slot is named
        ``cron-<id>``. The filename fold turns the colon into an underscore, so
        the lookup asked for ``cron_<id>`` and matched nothing: the control was
        never recorded, and the follow-up turn had nothing to expire — leaving a
        live control answerable into a question the cron had moved past. The
        resolver falls back to asking the slots their own identity, so both the
        record and the expiry reach the same slot.
        """
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            remember_slack_options,
            slack_options_slot,
        )

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.update_message = AsyncMock()
        state.slack_client = slack

        # Exactly how cron_inject builds it: slot named cron-<id>, real key on
        # linked_session_key.
        slot = state.get_or_create_slot("cron-42")
        slot.linked_session_key = "cron:42"
        session_key = "cron:42"

        # The folded spelling really does miss -- that is the defect.
        assert state.get_slot("cron_42") is None
        assert slack_options_slot(state, session_key) is slot

        posted = PostedOptions(channel="C-1", ts="opt-cron", choices=("A",), blocks=())
        remember_slack_options(state, session_key, posted)
        assert _recs(state, slot) == (posted,), "the cron control must be tracked"

        await expire_slack_options(state, session_key)

        edited = {call.args[1] for call in slack.update_message.await_args_list}
        assert edited == {"opt-cron"}, edited
        assert _recs(state, slot) == ()

    @pytest.mark.asyncio
    async def test_a_transient_edit_failure_keeps_the_record_for_a_later_retry(self, tmp_path):
        """A failed Slack edit must not orphan a control that is still live.

        Records stay tracked across the edit and only settled ones are removed, so
        a transient failure is simply never removed and a later turn retries it.
        Dropping it would leave the buttons on screen with nothing tracking them —
        a later click then injects an answer to a superseded question.
        """
        from slack_sdk.errors import SlackApiError

        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.update_message = AsyncMock(
            side_effect=SlackApiError("ratelimited", MagicMock(status_code=429))
        )
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        posted = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        remember_slack_options(state, slot.key, posted)

        await expire_slack_options(state, slot.key)

        assert _recs(state, slot) == (posted,), (
            "a transient edit failure must leave the control tracked so a later "
            "turn can spend it, not orphan it live on screen"
        )

    @pytest.mark.asyncio
    async def test_a_permanent_edit_failure_is_not_retried_forever(self, tmp_path):
        """A message that can never be edited must not be retried every turn.

        The mirror of the test above: keeping EVERY failure would mean a deleted
        message or a channel we are not in burns an API call on every
        subsequent turn, forever. A 4xx that is not a rate limit will fail
        identically next time, so the record is settled.
        """
        from slack_sdk.errors import SlackApiError

        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.update_message = AsyncMock(
            side_effect=SlackApiError("message_not_found", MagicMock(status_code=404))
        )
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        posted = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        remember_slack_options(state, slot.key, posted)

        await expire_slack_options(state, slot.key)

        assert _recs(state, slot) == (), (
            "a permanently-failing edit must be treated as spent, or every later "
            "turn retries it forever"
        )

    def test_a_cron_owned_control_is_forgotten_on_selection(self):
        """The forget must cover the key the record actually used.

        The record sites resolve the owner through the PERSISTED thread index, so
        a control on a cron-linked thread is filed under ``cron:<id>``. The
        ownership helpers only consulted the dashboard SLOT index (plus the
        syntactic ``slack:<ts>``), which cannot see a cron link -- so the
        selection's forget missed the record entirely and the next expiry edited
        over the user's answer. It also defeats the under-lock skip, which
        relies on the forget having removed the record.
        """
        from kiro_crew.dashboard.chat_utils import (
            slack_options_owner_key,
            slack_options_session_keys,
        )

        state = MagicMock()
        state._slack_to_slot = {}
        state._slots = {}
        state.get_linked_slot = MagicMock(return_value=None)
        state.sessions.get_session_for_thread = MagicMock(return_value="cron:job-7")

        assert slack_options_owner_key(state, "thread-1") == "cron:job-7", (
            "recording must file the control under the cron session that owns the "
            "thread, not the syntactic slack:<ts> key"
        )
        keys = slack_options_session_keys(state, "thread-1")
        assert (
            "cron:job-7" in keys
        ), "clearing must cover the cron key or the record outlives the selection"

    def test_an_unresolvable_owner_falls_back_without_inventing_a_key(self):
        """A stub or a miss must not become a bogus session key.

        The persisted index is typed ``str | None``; anything else means a caller
        handed us a mock. Guards the helper against silently recording under a
        MagicMock's repr.
        """
        from kiro_crew.dashboard.chat_utils import (
            canonical_key,
            slack_options_owner_key,
            slack_options_session_keys,
        )

        state = MagicMock()
        state._slack_to_slot = {}
        state._slots = {}
        state.get_linked_slot = MagicMock(return_value=None)
        state.sessions.get_session_for_thread = MagicMock(return_value=MagicMock())

        assert slack_options_owner_key(state, "thread-1") == canonical_key("thread-1")
        assert slack_options_session_keys(state, "thread-1") == [canonical_key("thread-1")]

    @pytest.mark.asyncio
    async def test_a_selection_made_under_the_lock_is_not_overwritten(self, tmp_path):
        """A click that wins the race must not have its answer erased.

        Expiry and the Send handler both edit the SAME Slack message, and Slack
        offers no compare-and-set. They now share ``options_edit_lock`` per
        message, and the expiry re-reads its record INSIDE that lock -- so a Send
        that already rewrote the message and dropped the record makes the expiry
        skip its edit entirely, rather than landing late and replacing the
        selection with a spent summary.
        """
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            forget_slack_options,
            remember_slack_options,
        )
        from kiro_crew.slack.outbound import options_edit_lock

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        posted = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        remember_slack_options(state, slot.key, posted)

        slack = MagicMock()
        slack.update_message = AsyncMock()
        state.slack_client = slack

        # Hold the lock, then let a "click" rewrite the message and forget the
        # record while the expiry is queued behind us.
        lock = options_edit_lock("C-1", "opt-1")
        async with lock:
            expiring = asyncio.create_task(expire_slack_options(state, slot.key))
            # flake-ok: pre-existing one-tick yield so the queued expiry reaches the held lock; moved verbatim and tracked for a de-flake pass
            await asyncio.sleep(0)  # let the expiry reach the lock and block
            forget_slack_options(state, slot.key, "opt-1")
        # flake-ok: pre-existing await of the expiry task the test released by leaving the lock; moved verbatim and tracked for a de-flake pass
        await expiring

        assert slack.update_message.await_count == 0, (
            "the expiry must skip its edit once the click dropped the record -- "
            "editing anyway erases the answer the user just gave"
        )
        assert _recs(state, slot) == ()

    @pytest.mark.asyncio
    async def test_a_click_during_a_failing_edit_is_not_resurrected(self, tmp_path):
        """A control answered mid-await must stay answered.

        The hazard is specific to retaining transient failures. A click landing
        while the expiry's edit is being rate-limited renders the SELECTED summary
        and forgets the record. If the expiry then wrote its unsettled records
        back, it would resurrect the one just answered, and every later turn would
        re-edit that message -- replacing the user's visible answer with a spent
        summary. Only settled records may be removed; nothing is ever re-added.
        """
        from slack_sdk.errors import SlackApiError

        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            forget_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        posted = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())

        async def _rate_limited_then_click(*_a, **_k):
            # The click lands while this edit is in flight: it answers the
            # control and drops the record, exactly as the submit path does.
            forget_slack_options(state, slot.key, "opt-1")
            raise SlackApiError("ratelimited", MagicMock(status_code=429))

        slack = MagicMock()
        slack.update_message = AsyncMock(side_effect=_rate_limited_then_click)
        state.slack_client = slack

        remember_slack_options(state, slot.key, posted)
        await expire_slack_options(state, slot.key)

        assert _recs(state, slot) == (), (
            "a control answered while the edit was failing must NOT come back -- "
            "re-adding it makes every later turn overwrite the user's answer"
        )

    @pytest.mark.asyncio
    async def test_recording_the_same_control_twice_queues_one_edit(self, tmp_path):
        """A retry, or two paths recording one post, is not two live controls."""
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.update_message = AsyncMock()
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        posted = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        remember_slack_options(state, slot.key, posted)
        remember_slack_options(state, slot.key, posted)

        assert _recs(state, slot) == (posted,)

        await expire_slack_options(state, slot.key)
        slack.update_message.assert_awaited_once()


class TestForgettingIsScopedToTheClickedControl:
    """Answering one control must not un-track the others.

    Once several controls can be outstanding at once, clearing the whole
    collection on a click leaves the other ones on screen with nothing tracking
    them — so a later click on one answers a superseded question and no expiry
    can ever reach it. A click spends exactly the control it was made on.

    The unscoped form is still correct for an unlink, where the entire
    conversation is detaching and every control should stop being tracked.
    """

    def test_a_click_forgets_only_the_control_it_answered(self, tmp_path):
        from kiro_crew.dashboard.chat_utils import (
            forget_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        first = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        second = PostedOptions(channel="C-1", ts="opt-2", choices=("B",), blocks=())
        remember_slack_options(state, slot.key, first)
        remember_slack_options(state, slot.key, second)

        forget_slack_options(state, slot.key, "opt-1")

        # The answered one is spent; the other is STILL tracked, so a later turn
        # can expire it instead of leaving it clickable forever.
        assert _recs(state, slot) == (second,)

    def test_omitting_the_ts_still_clears_everything(self, tmp_path):
        """The unlink path detaches the whole conversation, so all of them go."""
        from kiro_crew.dashboard.chat_utils import (
            forget_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        remember_slack_options(
            state, slot.key, PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        )
        remember_slack_options(
            state, slot.key, PostedOptions(channel="C-1", ts="opt-2", choices=("B",), blocks=())
        )

        forget_slack_options(state, slot.key)

        assert _recs(state, slot) == ()
