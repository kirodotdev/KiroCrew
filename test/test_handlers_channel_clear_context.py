"""Tests for /api/channels/{id}/clear-context handler."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers_channel import api_channel_clear_context


def _make_agent(agent_id: str, role: str, session_key: str):
    agent = MagicMock()
    agent.id = agent_id
    agent.role = role
    agent.session_key = session_key
    return agent


def _make_channel(ch_id: str, agents: dict):
    ch = MagicMock()
    ch.id = ch_id
    ch.members = agents
    ch.messages = [MagicMock(), MagicMock()]
    ch._msg_index = {"msg1": MagicMock(), "msg2": MagicMock()}
    ch.exchange_counts = {("a", "b"): 3}
    ch._save = MagicMock()
    return ch


def _make_request(ch_id: str, body: dict, channel=None, sessions=None):
    request = MagicMock()
    request.match_info = {"id": ch_id}
    request.json = AsyncMock(return_value=body)

    mgr = MagicMock()
    mgr.get.return_value = channel
    request.app = {
        "state": MagicMock(sessions=sessions or AsyncMock()),
        "channel_manager": mgr,
    }
    return request


class TestAClearInTheInterTurnWindowIsRefused:
    """Acknowledged-but-undequeued work must refuse the clear, not be wiped out from under it.

    A post is acknowledged to its sender and persisted the moment it reaches the inbox, but
    the turn is declared only when the member dequeues it. In between, the session reports no
    active turn -- so a clear that consults the turn alone succeeds, wipes the log, and the
    member then runs the erased prompt and answers into whatever replaced it.
    """

    @pytest.mark.asyncio
    async def test_a_queued_message_refuses_its_member_and_spares_the_log(self):
        agent = _make_agent("a1", "Researcher", "channel:ch1:a1")
        agent.inbox = asyncio.Queue()
        agent.inbox.put_nowait(MagicMock())

        ch = _make_channel("ch1", {"a1": agent})
        log_before = list(ch.messages)
        assert log_before, "precondition: the channel log was already empty"

        sessions = AsyncMock()
        # The session would answer the clear: it declares no turn in this window.
        sessions.discard_conversation = AsyncMock(return_value=True)

        request = _make_request("ch1", {"scope": "all"}, channel=ch, sessions=sessions)
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        body = json.loads(resp.body.decode())
        assert body.get("busy") == ["Researcher"], (
            "a member holding an acknowledged message was not refused, so the clear is "
            f"acknowledged for work that still runs; body={body}"
        )
        assert not body.get("cleared"), f"the clear was acknowledged; body={body}"
        assert sessions.discard_conversation.await_count == 0, (
            "the session was discarded for a member with queued work, which is the "
            "acknowledgement the sender sees before its prompt is erased"
        )
        assert list(ch.messages) == log_before, (
            "the log was wiped while a member still held an acknowledged prompt, so its reply "
            "lands in a conversation that no longer contains the question"
        )

    @pytest.mark.asyncio
    async def test_a_hung_teardown_releases_the_log_lock_and_refuses_the_member(self):
        """`post` shares `_log_lock`, so an unbounded shutdown wedges the whole channel.

        The teardown is awaited while the lock is held and has no timeout of its own at this
        layer, so one provider that never returns from `shutdown()` stops every message in the
        channel rather than just this clear. The member is reported REFUSED, not cleared: the
        teardown is still running, so answering cleared would speak for unfinished work.
        """
        import kiro_crew.dashboard.handlers_channel as hc

        agent = _make_agent("a1", "Researcher", "channel:ch1:a1")
        ch = _make_channel("ch1", {"a1": agent})
        # A real lock: the fixture's Mock answers `.locked()` truthy, so the release
        # assertion below would pass against any behaviour at all.
        ch._log_lock = asyncio.Lock()
        log_before = list(ch.messages)

        hung = asyncio.Event()
        sessions = AsyncMock()

        async def _never_returns(*_a, **_k):
            await hung.wait()
            return True

        sessions.discard_conversation = AsyncMock(side_effect=_never_returns)
        # Explicit: still registered, so nothing destructive has committed and a refusal is
        # the truthful answer. A bare AsyncMock would answer truthy here by accident.
        sessions.has_session = MagicMock(return_value=True)

        request = _make_request("ch1", {"scope": "all"}, channel=ch, sessions=sessions)
        with patch.object(hc, "_CLEAR_DISCARD_TIMEOUT_SECS", 0.05):
            with patch(
                "kiro_crew.dashboard.handlers_channel._mgr",
                return_value=MagicMock(get=MagicMock(return_value=ch)),
            ):
                resp = await asyncio.wait_for(api_channel_clear_context(request), timeout=5.0)

        body = json.loads(resp.body.decode())
        assert body.get("busy") == ["Researcher"], (
            f"a member whose teardown never returned was not refused; body={body}"
        )
        assert not body.get("cleared"), f"unfinished teardown reported as cleared; body={body}"
        assert list(ch.messages) == log_before, "the log was wiped on a refused clear"
        assert not ch._log_lock.locked(), "the lock outlived the request, wedging every post"
        hung.set()

    @pytest.mark.asyncio
    async def test_a_refused_member_is_not_discarded_after_the_answer(self):
        """The refusal and the teardown must not BOTH win.

        The teardown is shielded so the deadline releases `_log_lock` without cancelling it,
        and `discard_conversation` pops the session and clears the SID EARLY, before the slow
        `provider.shutdown()`. So a member whose `wait_for` expires before its own pop is
        reported busy -- "Nothing was cleared" -- and the shielded task then pops and clears
        the SID anyway, discarding the session the API just said it had kept. The SID is
        dropped rather than retained, so the next turn cold-starts with no history.

        Reached by the shared deadline rather than by an extreme: an earlier member's slow
        shutdown exhausts it, so a later member gets `timeout ~= 0`.
        """
        import kiro_crew.dashboard.handlers_channel as hc

        agent = _make_agent("a1", "Researcher", "channel:ch1:a1")
        ch = _make_channel("ch1", {"a1": agent})
        ch._log_lock = asyncio.Lock()

        state = {"destroyed": False}
        sessions = AsyncMock()

        async def _pops_late(*_a, **_k):
            # The real ordering: the destructive half lands, then the slow shutdown.
            await asyncio.sleep(0.05)
            state["destroyed"] = True
            await asyncio.sleep(0.2)
            return True

        sessions.discard_conversation = AsyncMock(side_effect=_pops_late)
        sessions.has_session = MagicMock(side_effect=lambda _k: not state["destroyed"])

        request = _make_request("ch1", {"scope": "all"}, channel=ch, sessions=sessions)
        with patch.object(hc, "_CLEAR_DISCARD_TIMEOUT_SECS", 0.01):
            with patch(
                "kiro_crew.dashboard.handlers_channel._mgr",
                return_value=MagicMock(get=MagicMock(return_value=ch)),
            ):
                resp = await asyncio.wait_for(api_channel_clear_context(request), timeout=5.0)
        body = json.loads(resp.body.decode())
        refused = "Researcher" in (body.get("busy") or [])

        # Let anything the handler left running finish, the way the process would.
        await asyncio.sleep(0.4)

        assert not (refused and state["destroyed"]), (
            "the API reported this member as NOT cleared, then its teardown discarded the "
            "session and its SID anyway -- the next turn starts with no history and the 409 "
            f"that promised otherwise is a lie; body={body}"
        )

    @pytest.mark.asyncio
    async def test_the_whole_clear_shares_one_deadline_across_members(self):
        """N members must not cost N timeouts: `post` waits on the same `_log_lock`.

        A per-member bound is spent once per member, so a clear-all on a channel whose
        providers are all slow holds the lock for N x the bound and stalls every message in
        the channel for that long. The deadline is taken once, before the first member.
        """
        import kiro_crew.dashboard.handlers_channel as hc

        members = {
            f"a{i}": _make_agent(f"a{i}", f"Role{i}", f"channel:ch1:a{i}") for i in range(1, 5)
        }
        ch = _make_channel("ch1", members)
        ch._log_lock = asyncio.Lock()

        hung = asyncio.Event()
        sessions = AsyncMock()

        async def _never_returns(*_a, **_k):
            await hung.wait()
            return True

        sessions.discard_conversation = AsyncMock(side_effect=_never_returns)
        sessions.has_session = MagicMock(return_value=True)

        request = _make_request("ch1", {"scope": "all"}, channel=ch, sessions=sessions)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with patch.object(hc, "_CLEAR_DISCARD_TIMEOUT_SECS", 0.2):
            with patch(
                "kiro_crew.dashboard.handlers_channel._mgr",
                return_value=MagicMock(get=MagicMock(return_value=ch)),
            ):
                resp = await asyncio.wait_for(api_channel_clear_context(request), timeout=5.0)
        elapsed = loop.time() - started
        hung.set()

        assert len(members) == 4, "fewer than two members cannot show the multiplication"
        assert resp.status == 409, "every member was busy, so this is a total refusal"
        assert elapsed < 0.2 * len(members), (
            f"the clear spent its bound once per member: {elapsed:.2f}s across {len(members)} "
            f"members against a 0.2s deadline, so every post waited that long"
        )

    @pytest.mark.asyncio
    async def test_a_slow_shutdown_after_the_pop_is_reported_cleared_not_refused(self):
        """A timeout is only a refusal while nothing destructive has committed.

        `discard_conversation` pops the session and clears the SID BEFORE awaiting
        `provider.shutdown()`, so a shutdown that outruns the bound leaves the conversation
        already gone. Reporting that member refused answers "Nothing was cleared" while its
        next turn starts empty -- the transcript is the only witness, and it is wiped.
        """
        import kiro_crew.dashboard.handlers_channel as hc

        agent = _make_agent("a1", "Researcher", "channel:ch1:a1")
        ch = _make_channel("ch1", {"a1": agent})
        ch._log_lock = asyncio.Lock()

        hung = asyncio.Event()
        sessions = AsyncMock()

        async def _destroys_then_hangs(*_a, **_k):
            await hung.wait()
            return True

        sessions.discard_conversation = AsyncMock(side_effect=_destroys_then_hangs)
        # The registry AFTER the pop: the destructive half already committed.
        sessions.has_session = MagicMock(return_value=False)

        request = _make_request("ch1", {"scope": "all"}, channel=ch, sessions=sessions)
        with patch.object(hc, "_CLEAR_DISCARD_TIMEOUT_SECS", 0.05):
            with patch(
                "kiro_crew.dashboard.handlers_channel._mgr",
                return_value=MagicMock(get=MagicMock(return_value=ch)),
            ):
                resp = await asyncio.wait_for(api_channel_clear_context(request), timeout=5.0)

        body = json.loads(resp.body.decode())
        assert resp.status == 200, f"a committed clear answered a refusal status; body={body}"
        assert body.get("cleared") == ["Researcher"], (
            "the session was already discarded, so answering anything but cleared tells the "
            f"user their history survived; body={body}"
        )
        assert not body.get("busy"), f"a committed teardown was reported refused; body={body}"
        assert not ch._log_lock.locked(), "the lock outlived the request, wedging every post"
        hung.set()

    @pytest.mark.asyncio
    async def test_an_empty_inbox_still_clears(self):
        """The refusal must be the queued message, not the presence of an inbox."""
        agent = _make_agent("a1", "Researcher", "channel:ch1:a1")
        agent.inbox = asyncio.Queue()

        ch = _make_channel("ch1", {"a1": agent})
        sessions = AsyncMock()
        sessions.discard_conversation = AsyncMock(return_value=True)

        request = _make_request("ch1", {"scope": "all"}, channel=ch, sessions=sessions)
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        body = json.loads(resp.body.decode())
        assert body.get("cleared") == ["Researcher"], (
            f"an idle member was refused, so no channel can ever be cleared; body={body}"
        )
        assert ch.messages == [], "the log survived a fully clean clear"


class TestChannelClearContext:
    @pytest.mark.asyncio
    async def test_returns_404_when_channel_not_found(self):
        request = _make_request("nonexistent", {}, channel=None)
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=None)),
        ):
            resp = await api_channel_clear_context(request)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_clears_all_agents(self):
        agents = {
            "a1": _make_agent("a1", "Researcher", "channel:ch1:a1"),
            "a2": _make_agent("a2", "Writer", "channel:ch1:a2"),
        }
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        sessions.discard_conversation = AsyncMock()

        request = _make_request("ch1", {"scope": "all"}, channel=ch, sessions=sessions)
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        body = json.loads(resp.body)
        assert body["ok"] is True
        assert set(body["cleared"]) == {"Researcher", "Writer"}
        assert sessions.discard_conversation.call_count == 2
        assert ch.messages == []
        assert ch._msg_index == {}
        assert ch.exchange_counts == {}
        ch._save.assert_called_once()

    @pytest.mark.asyncio
    async def test_clears_single_agent(self):
        agents = {
            "a1": _make_agent("a1", "Researcher", "channel:ch1:a1"),
            "a2": _make_agent("a2", "Writer", "channel:ch1:a2"),
        }
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        sessions.discard_conversation = AsyncMock()

        request = _make_request(
            "ch1", {"scope": "agent", "agent_id": "a1"}, channel=ch, sessions=sessions
        )
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["cleared"] == ["Researcher"]
        assert "busy" not in body, (
            "the success path carries no busy list -- it is constantly empty there, since a "
            f"non-empty one returns ok:false above; got {body}"
        )
        # skip_if_busy: a turn can be streaming on the channel agent, so forcing the
        # teardown would drop that reply; the refusal is reported in `busy` instead.
        # `refuse_only_on_active_turn` is pinned deliberately: without it a member's
        # lifetime lease refuses this clear for as long as the member exists.
        sessions.discard_conversation.assert_called_once_with(
            "channel:ch1:a1", skip_if_busy=True, refuse_only_on_active_turn=True
        )
        # Messages and exchange_counts NOT cleared for single-agent scope
        assert len(ch.messages) == 2

    @pytest.mark.asyncio
    async def test_a_key_with_no_live_session_counts_as_cleared_not_busy(self):
        """A member with nothing registered must not be reported busy.

        `discard_conversation` reports False ONLY for a `skip_if_busy` refusal over a LIVE
        session -- an absent key takes the teardown path and answers True -- so the endpoint
        gets its "nothing to clear is already cleared" behaviour from that contract, with no
        membership probe. Stubbing False here alongside an absent session would fabricate a
        return the lifecycle service cannot produce; the contract itself is pinned by
        ``TestDiscardConversation.test_an_absent_key_is_not_reported_as_a_busy_refusal``.
        """
        agents = {"a1": _make_agent("a1", "Researcher", "channel:ch1:a1")}
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        sessions.discard_conversation = AsyncMock(return_value=True)

        request = _make_request(
            "ch1", {"scope": "agent", "agent_id": "a1"}, channel=ch, sessions=sessions
        )
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        assert resp.status == 200, (
            "a key with no live session is already in the state the caller asked for, so "
            f"it must not answer 409; got {resp.status}"
        )
        body = json.loads(resp.body)
        assert body.get("busy") in (None, []), f"and it must not be reported busy; got {body}"
        assert "Researcher" in body.get("cleared", []), (
            f"the absent member must be reported cleared; got {body}"
        )

    @pytest.mark.asyncio
    async def test_a_total_refusal_does_not_destroy_the_shared_message_log(self):
        """The 409 must be answered BEFORE the buffer wipe, not after it.

        `scope="all"` on a channel where every member is mid-turn clears nothing, and
        the wipe below the reset loop is SHARED channel state that `_save()` persists.
        Answering 409 after running it destroyed the transcript the response reports as
        untouched -- silent, irreversible, and the opposite of what the caller is told.
        Asserts the buffers survive AND that nothing was persisted, since either alone
        would pass while the other still lost the log.
        """
        agents = {
            "a1": _make_agent("a1", "Researcher", "channel:ch1:a1"),
            "a2": _make_agent("a2", "Analyst", "channel:ch1:a2"),
        }
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        sessions.discard_conversation = AsyncMock(return_value=False)

        request = _make_request("ch1", {"scope": "all"}, channel=ch, sessions=sessions)
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        assert resp.status == 409, f"a clear that cleared nothing must not answer 200; got {resp.status}"
        assert len(ch.messages) == 2, (
            "the shared message log must SURVIVE a total refusal -- the 409 reports it "
            f"untouched, so wiping it makes the response a lie; got {len(ch.messages)}"
        )
        assert len(ch._msg_index) == 2, f"the message index must survive too; got {ch._msg_index}"
        assert ch.exchange_counts == {("a", "b"): 3}, f"and the counts; got {ch.exchange_counts}"
        ch._save.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_broadcast_carries_only_what_its_listener_reads(self):
        """A field no consumer reads is a claim about the wire that nothing checks.

        The gate above the broadcast forces `scope == "all"` and `busy == []`, so a per-agent
        id and a busy list are not merely unread here -- they are empty BY CONSTRUCTION, and a
        later reader trusting either would be reading a constant. The sole listener keys on
        `channel_id` and `scope`; the payload now states exactly that and nothing more.
        """
        agents = {"a1": _make_agent("a1", "Researcher", "channel:ch1:a1")}
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        sessions.discard_conversation = AsyncMock(return_value=True)
        sessions.has_session = MagicMock(return_value=True)

        request = _make_request("ch1", {"scope": "all"}, channel=ch, sessions=sessions)
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        assert resp.status == 200
        sent = [c.args for c in ch._broadcast.call_args_list if c.args and c.args[0] == "channel_context_cleared"]
        assert len(sent) == 1, f"a clean clear must announce itself exactly once; got {sent}"
        assert set(sent[0][1]) == {"channel_id", "scope"}, (
            "the payload must carry only the fields the listener reads -- anything else is "
            f"an unchecked wire claim; got {sorted(sent[0][1])}"
        )

    @pytest.mark.asyncio
    async def test_a_partial_clear_does_not_announce_a_wipe_to_other_tabs(self):
        """The broadcast is what OTHER tabs act on, so it must follow the wipe, not the request.

        A listener handles `channel_context_cleared` by REPLACING its retained transcript with an
        empty list. Announcing it unconditionally meant a partial clear -- which deliberately keeps
        the shared log for the busy member -- destroyed that same log in every other tab, out of
        band from the response and with nothing to restore it. The event now follows the wipe.
        """
        agents = {
            "a1": _make_agent("a1", "Researcher", "channel:ch1:a1"),
            "a2": _make_agent("a2", "Analyst", "channel:ch1:a2"),
        }
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        sessions.discard_conversation = AsyncMock(side_effect=[True, False])
        sessions.has_session = MagicMock(return_value=True)

        request = _make_request("ch1", {"scope": "all"}, channel=ch, sessions=sessions)
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        assert resp.status == 200
        body = json.loads(resp.body)
        assert body.get("busy"), f"precondition: this must be the partial path; got {body}"
        assert len(ch.messages) == 2, "precondition: the shared log survived the partial clear"
        events = [c.args[0] for c in ch._broadcast.call_args_list if c.args]
        assert "channel_context_cleared" not in events, (
            "a partial clear kept the shared log, so announcing it tells every other tab to "
            f"replace that log with an empty list; broadcast {events}"
        )

    @pytest.mark.asyncio
    async def test_a_partial_clear_leaves_the_shared_log_the_busy_member_still_references(self):
        """A PARTIAL clear-all must not wipe shared state, only a fully clean one may.

        The total refusal answers 409 above the wipe, and a fully clean clear reaches it
        legitimately -- but the PARTIAL case answers 200 and fell through to the same
        unconditional wipe. The busy member keeps the LLM context that quotes the shared
        transcript, so emptying it strands that member: its in-flight reply appends to a
        log the rest of its context still refers to, and no path restores what was lost.
        """
        agents = {
            "a1": _make_agent("a1", "Researcher", "channel:ch1:a1"),
            "a2": _make_agent("a2", "Analyst", "channel:ch1:a2"),
        }
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        # a1 clears, a2 is mid-turn: the 200 partial path, not the 409 total one.
        sessions.discard_conversation = AsyncMock(side_effect=[True, False])
        sessions.has_session = MagicMock(return_value=True)

        request = _make_request("ch1", {"scope": "all"}, channel=ch, sessions=sessions)
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        assert resp.status == 200, (
            "a partial clear cleared something, so the contract's 200 stands -- `ok` is what "
            f"marks it incomplete; got {resp.status}"
        )
        body = json.loads(resp.body)
        assert (
            body.get("ok") is False
        ), f"a caller reading only `ok` must see a partial clear as incomplete; got {body}"
        assert body.get("busy"), f"precondition: the partial path must report a busy member; got {body}"
        assert len(ch.messages) == 2, (
            "the shared log must SURVIVE a partial clear -- the member reported busy keeps "
            f"the context that references it; got {len(ch.messages)}"
        )
        assert len(ch._msg_index) == 2, f"the message index must survive too; got {ch._msg_index}"
        assert ch.exchange_counts == {("a", "b"): 3}, f"and the counts; got {ch.exchange_counts}"

    @pytest.mark.asyncio
    async def test_the_refusal_carries_a_machine_readable_code(self):
        """`error` prose is advisory and untranslatable; `code` is the contract.

        The dashboard renders `res.error` verbatim into a localized UI, so a coded
        response is what lets a caller branch on the cause. Pinned here as well as in
        the repo-wide ratchet so the reason is readable at the site that owes it.
        """
        agents = {"a1": _make_agent("a1", "Researcher", "channel:ch1:a1")}
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        sessions.discard_conversation = AsyncMock(return_value=False)

        request = _make_request(
            "ch1", {"scope": "agent", "agent_id": "a1"}, channel=ch, sessions=sessions
        )
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        body = json.loads(resp.body)
        assert body.get("code") == "turn_in_flight", (
            "the refusal must carry a machine-readable code, not prose alone; " f"got {body}"
        )

    @pytest.mark.asyncio
    async def test_a_total_refusal_answers_409_not_a_false_success(self):
        """Reporting the refusal into a field nothing reads IS the silent no-op.

        An earlier version answered 200 with a `busy` list and no reader, so the caller
        rendered a clear that never happened -- a success signal that is not proof of
        effect. When NOTHING cleared the endpoint now fails, which reaches the user through
        the caller's existing error path. Declining the reset is still right: forcing it
        would tear down a streaming reply.
        """
        agents = {"a1": _make_agent("a1", "Researcher", "channel:ch1:a1")}
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        sessions.discard_conversation = AsyncMock(return_value=False)

        request = _make_request(
            "ch1", {"scope": "agent", "agent_id": "a1"}, channel=ch, sessions=sessions
        )
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        assert resp.status == 409, f"a clear that cleared nothing must not answer 200; got {resp.status}"
        body = json.loads(resp.body)
        assert "Researcher" in body.get("error", ""), (
            "and the error must name what refused, or the user cannot tell what to retry; "
            f"got {body}"
        )
        assert body["busy"] == ["Researcher"]

    @pytest.mark.asyncio
    async def test_a_member_holding_no_live_session_is_not_reported_cleared(self):
        """`discard_conversation` answers True for a key holding nothing.

        At the reporting site that is indistinguishable from a real clear, so an idle member
        with no session was credited to `cleared` and the response named work this endpoint
        did not do. Presence is read BEFORE the discard, and only a session that existed can
        be reported cleared.
        """
        agents = {"a1": _make_agent("a1", "Researcher", "channel:ch1:a1")}
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        sessions.discard_conversation = AsyncMock(return_value=True)
        # The registry answer is the whole point, so it is set explicitly: a bare AsyncMock
        # returns a truthy Mock and the assertion below could not fail.
        sessions.has_session = MagicMock(return_value=False)

        request = _make_request(
            "ch1", {"scope": "agent", "agent_id": "a1"}, channel=ch, sessions=sessions
        )
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        body = json.loads(resp.body)
        assert body.get("cleared") == [], (
            "a member holding no live session was reported cleared, so the response credits a "
            f"clear that never happened; got {body}"
        )

    @pytest.mark.asyncio
    async def test_a_member_holding_a_live_session_is_still_reported_cleared(self):
        """The positive control: gating on presence must not silence a REAL clear."""
        agents = {"a1": _make_agent("a1", "Researcher", "channel:ch1:a1")}
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        sessions.discard_conversation = AsyncMock(return_value=True)
        sessions.has_session = MagicMock(return_value=True)

        request = _make_request(
            "ch1", {"scope": "agent", "agent_id": "a1"}, channel=ch, sessions=sessions
        )
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        body = json.loads(resp.body)
        assert body.get("cleared") == ["Researcher"], f"a real clear went unreported; got {body}"

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_agent_id(self):
        agents = {"a1": _make_agent("a1", "Researcher", "channel:ch1:a1")}
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()

        request = _make_request(
            "ch1", {"scope": "agent", "agent_id": "nonexistent"}, channel=ch, sessions=sessions
        )
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_returns_400_on_missing_body(self):
        """A request with no parseable body returns 400 (not a silent clear-all)."""
        agents = {"a1": _make_agent("a1", "Researcher", "channel:ch1:a1")}
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()

        request = _make_request("ch1", {}, channel=ch, sessions=sessions)
        request.json = AsyncMock(side_effect=Exception("no body"))
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_returns_400_when_scope_agent_but_no_agent_id(self):
        """scope=agent without agent_id returns 400 (not a silent clear-all)."""
        agents = {"a1": _make_agent("a1", "Researcher", "channel:ch1:a1")}
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()

        request = _make_request(
            "ch1", {"scope": "agent"}, channel=ch, sessions=sessions
        )
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        assert resp.status == 400
