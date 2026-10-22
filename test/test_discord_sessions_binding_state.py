"""Discord ``!sessions``: the binding and its resume expectation under races and failures.

A reply in a dashboard-connected conversation resumes that session, a binding lost
under the conversation is noticed, the expectation store keeps its invariants,
routing holds under concurrent rebinding, a failed write fails closed, and
settlement reconciles the live state.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import discord_sessions_helpers
import pytest
from discord_sessions_helpers import (
    _Client,
    _dispatcher,
    _interaction,
    _log,
    _log_with_titles,
    _message,
    _picker_button,
)

from kiro_crew.config.paths import config_dir
from kiro_crew.discord.transport_dispatch import DiscordDispatcher
from kiro_crew.messaging import resume_expectation
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session_map import ConversationOwnershipConflict
from kiro_crew.testing.wait import await_until

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_isolated_home = discord_sessions_helpers._isolated_home


def _store_path() -> Path:
    trust = config_dir() / resume_expectation._TRUST_SUBDIR
    trust.mkdir(parents=True, exist_ok=True)
    return trust / resume_expectation.store_filename("discord")


def _gate_send(client, marker: str, during) -> list[str]:
    fired: list[str] = []
    real_send = client.send_message

    async def _send(channel_id: str, text: str, **kwargs):
        if marker in text and not fired:
            fired.append(text)
            await during()
        return await real_send(channel_id, text, **kwargs)

    client.send_message = _send  # type: ignore[method-assign]
    return fired


async def _attach(dispatcher: DiscordDispatcher, client: _Client) -> ChannelLink:
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    return ChannelLink(channel_type="discord", channel_id="c1")


class TestDashboardConnectedConversationResumes:
    """The reported bug: replying to a message from Kiro Crew forked a new session.

    A dashboard session connected to a Discord conversation, sent into it, and
    the user replied there. Instead of continuing that session, the reply started
    a brand-new one with no history — so the user was answered by an agent that
    had never seen the conversation it was replying inside.

    The inbound resolver was never at fault. ``resumed_session`` filters on the
    binding's inbound marker, and the dashboard's connect never set it, so the
    resolver correctly found no owner and the dispatcher fell through to Discord's
    own route-derived session key. These tests pin the marker's effect on routing
    from both sides.
    """

    @pytest.mark.asyncio
    async def test_reply_resumes_the_connected_session(self) -> None:
        dispatcher, _client, sessions = _dispatcher({"u1"}, _log())
        # What a dashboard connect leaves behind once the transport declares
        # `supports_session_resume` — the binding plus its inbound marker.
        sessions.set_mirror_link(
            "dashboard:chat-1",
            ChannelLink(channel_type="discord", channel_id="c1"),
            accepts_inbound=True,
        )

        assert dispatcher._session_resume.resumed_session("c1") == "dashboard:chat-1"
        assert dispatcher._inbound_session_key("u1", "c1") == "dashboard:chat-1"

    @pytest.mark.asyncio
    async def test_an_outbound_only_binding_still_forks(self) -> None:
        """The bug's exact mechanism, pinned so the marker cannot be dropped.

        Same binding, no inbound marker — which is all main's connect could
        write. The reply must NOT resolve to the connected session, and the key
        the dispatcher falls back to is a Discord-native one, i.e. a different
        conversation with none of the dashboard transcript. This is the
        before-state; `accepts_inbound` is the whole difference.
        """
        dispatcher, _client, sessions = _dispatcher({"u1"}, _log())
        sessions.set_mirror_link(
            "dashboard:chat-1",
            ChannelLink(channel_type="discord", channel_id="c1"),
        )

        assert dispatcher._session_resume.resumed_session("c1") is None
        forked = dispatcher._inbound_session_key("u1", "c1")
        assert forked != "dashboard:chat-1"
        assert forked.startswith("discord:")

    @pytest.mark.asyncio
    async def test_two_owners_route_nowhere_which_is_why_exclusivity_ships_here(
        self,
    ) -> None:
        """Why the ownership rule belongs with the inbound marker.

        "No owner" and "two owners" land on the same ``None``. So setting the
        inbound marker without enforcing one-session-per-conversation would not
        fix the fork — it would only move it: a duplicated binding sends the
        reply to NO session, silently forking exactly as before, and now with no
        way for the user to tell why.
        """
        dispatcher, _client, sessions = _dispatcher({"u1"}, _log())
        link = ChannelLink(channel_type="discord", channel_id="c1")
        # Planted past the writer's guard: the state a map file can still hold.
        sessions.mirror_links["dashboard:chat-1"] = link
        sessions.inbound_keys.add("dashboard:chat-1")
        sessions.mirror_links["dashboard:chat-2"] = link
        sessions.inbound_keys.add("dashboard:chat-2")

        assert dispatcher._session_resume.resumed_session("c1") is None
        assert dispatcher._inbound_session_key("u1", "c1").startswith("discord:")

    @pytest.mark.asyncio
    async def test_inchannel_link_reports_the_refusal_instead_of_failing(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log())
        loc = ChannelLink(channel_type="discord", channel_id="c1")
        # Planted past the writer's guard: the duplicate state a map file can
        # still hold, and the only state that reaches the handler's writer.
        for occupant in ("dashboard:chat-7", "dashboard:chat-8"):
            sessions.mirror_links[occupant] = loc
            sessions.inbound_keys.add(occupant)
        assert (
            dispatcher._session_resume.resumed_session("c1") is None
        ), "setup no longer produces the ambiguous state this test needs"

        await dispatcher.handle_message(_message("!link"))

        assert any(
            "`!unlink`" in text for text, _ in client.sent
        ), f"the refusal was not reported to the channel: {client.sent}"
        assert set(sessions.mirror_links) == {
            "dashboard:chat-7",
            "dashboard:chat-8",
        }, "an occupant was displaced, or the link was written anyway"

    @pytest.mark.asyncio
    async def test_two_outbound_mirrors_are_still_allowed(self) -> None:
        _dispatcher_, _client, sessions = _dispatcher({"u1"}, _log())
        loc = ChannelLink(channel_type="discord", channel_id="c1")
        sessions.set_mirror_link("dashboard:chat-1", loc)
        # Must not raise.
        sessions.set_mirror_link("dashboard:chat-2", loc)
        assert sessions.find_mirror_sessions(loc, inbound_only=True) == []


class TestBindingLostUnderTheConversation:

    @pytest.mark.asyncio
    async def test_a_vanished_binding_is_refused_exactly_once(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = await _attach(dispatcher, client)
        store = dispatcher._session_resume._expectations
        real_retire = store.retire_if

        async def _retire_after_durability(channel_id: str, version: int):
            assert link not in sessions.flushed[-1].values()
            return await real_retire(channel_id, version)

        store.retire_if = _retire_after_durability  # type: ignore[method-assign]
        sessions.clear_mirror_links_at(link)
        sessions.last_key = ""
        await dispatcher.handle_message(_message("what did we decide?"))
        assert "Detached" in client.sent[-1][0]
        assert "Launch plan" in client.sent[-1][0]
        assert sessions.last_key == "", "the refused message still ran a turn"
        await dispatcher.handle_message(_message("resending this"))
        assert len([t for t, _ in client.sent if "Detached" in t]) == 1
        assert sessions.last_key == dispatcher._session_key("u1")

    @pytest.mark.asyncio
    async def test_a_synthetic_nudge_bypasses_detach_routing(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        await dispatcher._session_resume._expectations.record(
            "c1", "dashboard:chat-1", "Launch plan"
        )
        nudge = _message("[auto-nudge cycle 1]\ncheck")
        await dispatcher.handle_message(nudge, interpret_commands=False)
        assert not any("Detached" in text for text, _ in client.sent)
        assert sessions.last_key == dispatcher._session_key("u1")

    @pytest.mark.asyncio
    async def test_the_binding_is_durably_gone_before_the_record_is(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        await _attach(dispatcher, client)
        store = dispatcher._session_resume._expectations
        sessions.flush_error = OSError("disk full")
        await dispatcher.handle_message(_message("!unlink"))
        assert any("NOT completed" in text for text, _ in client.sent)
        assert await store.get("c1") is not None, "failed durability retired the record"
        assert not sessions.flushed
        await dispatcher.handle_message(_message("is this still attached?"))
        assert await store.get("c1") is not None, "failed settlement retired the record"
        sessions.flush_error = None
        real_retire, durable_first = store.retire_if, []

        async def _observe_retire(channel_id: str, version: int):
            durable_first.append(bool(sessions.flushed))
            return await real_retire(channel_id, version)

        store.retire_if = _observe_retire  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("!unlink"))
        link = dispatcher._session_resume.link_for("c1")
        assert sessions.flushed and not any(link in snap.values() for snap in sessions.flushed)
        assert durable_first == [True], "the retry retired the record before durability"
        retired = await store.get("c1")
        assert retired is not None and retired.retired

    @pytest.mark.asyncio
    async def test_a_same_key_rebind_during_unlink_cannot_defeat_the_exit(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log())
        link = await _attach(dispatcher, client)
        store = dispatcher._session_resume._expectations
        real_aflush = sessions.aflush

        async def _rebind_same_key_during_aflush() -> None:
            await real_aflush()
            sessions.mirror_links["dashboard:chat-1"] = link
            sessions.inbound_keys.add("dashboard:chat-1")

        sessions.aflush = _rebind_same_key_during_aflush  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("!unlink"))
        expected = await store.get("c1")
        assert expected is not None and expected.retired, "a raced owner suppressed retirement"
        sessions.last_key = ""
        await dispatcher.handle_message(_message("still here?"))
        assert sessions.last_key == "", "the rebound session was entered in silence"
        assert "Now linked to" in client.sent[-1][0], client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_releasing_an_unrecorded_binding_leaves_evidence_before_mutation(self) -> None:
        """A failed release records evidence first, then changes NOTHING -- live map too.

        The in-memory clear happens before the flush, so a flush failure would leave the
        binding gone in this process while the command reported failure. Two things were
        wrong with that pair: the very next message was refused as detached moments
        after being told the unlink did not happen, and the map on disk
        still held the binding, so a restart revived it and split one history in two -- the
        exact harm the sibling test names. The release now rolls the clear back, so the
        report, the live map and the persisted map all agree and the conversation carries
        on where the user was told it would.
        """
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = ChannelLink(channel_type="discord", channel_id="c1")
        # A dashboard-created binding that never carried a message: no record exists.
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        store = dispatcher._session_resume._expectations
        sessions.flush_error = OSError("disk full")
        await dispatcher.handle_message(_message("!unlink"))
        assert any("NOT completed" in text for text, _ in client.sent)
        expected = await store.get("c1")
        assert expected is not None and not expected.retired, "mutated without durable evidence"
        assert dispatcher._session_resume.resumed_session("c1") == "dashboard:chat-1"
        await dispatcher.handle_message(_message("where did we land?"))
        assert sessions.last_key == "dashboard:chat-1", "the resumed session must carry on"
        assert not any("Detached" in text for text, _ in client.sent)

    @pytest.mark.asyncio
    async def test_a_rolled_back_release_keeps_a_genuine_bindings_recorded_peer(self) -> None:
        """The rollback restores each occupant's OWN row, not the bare location.

        A row made from the dashboard or a ``!sessions`` pick records the peer it
        was admitted for under the gateway's admission. Restoring the location
        argument would put the binding back without that record, and every later
        dashboard reply into this DM would be refused until the session was
        re-linked -- a failed unlink silently costing the mirror its deliveries. The
        signed row goes back byte-for-byte and still delivers.
        """
        import dataclasses

        from kiro_crew.dashboard.chat_runner import _recipient_principal
        from kiro_crew.mirror_admission import sign_mirror_admission

        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = ChannelLink(channel_type="discord", channel_id="c1", principal="u1")
        genuine = dataclasses.replace(
            link, admission=sign_mirror_admission("dashboard:chat-1", link)
        )
        sessions.set_mirror_link("dashboard:chat-1", genuine, accepts_inbound=True)
        sessions.flush_error = OSError("disk full")
        await dispatcher.handle_message(_message("!unlink"))
        assert any("NOT completed" in text for text, _ in client.sent)
        restored = sessions.get_mirror_link("dashboard:chat-1")
        assert restored == genuine and restored.principal == "u1"
        assert restored.admission == genuine.admission
        assert "dashboard:chat-1" in sessions.inbound_keys
        assert _recipient_principal("dashboard:chat-1", restored, dispatcher.transport) == "u1"

    @pytest.mark.asyncio
    async def test_a_rolled_back_release_does_not_launder_a_forged_row(self) -> None:
        """The forgery a re-signing rollback would have admitted: a row planted in
        the agent-writable map (no valid admission), sitting unverifiable since a
        restart, then a failed unlink persistence. The rollback puts the binding
        back WITHOUT its peer -- nothing mints -- so the send stays refused and one
        audit row says why.
        """
        from kiro_crew.dashboard.chat_runner import _recipient_principal

        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        forged = ChannelLink(
            channel_type="discord", channel_id="c1", principal="u1", admission="f" * 64
        )
        sessions.set_mirror_link("dashboard:chat-1", forged, accepts_inbound=True)
        sessions.flush_error = OSError("disk full")
        await dispatcher.handle_message(_message("!unlink"))
        assert any("NOT completed" in text for text, _ in client.sent)
        restored = sessions.get_mirror_link("dashboard:chat-1")
        assert restored == ChannelLink(channel_type="discord", channel_id="c1")
        assert restored.principal is None and restored.admission is None
        assert "dashboard:chat-1" in sessions.inbound_keys
        assert _recipient_principal("dashboard:chat-1", restored, dispatcher.transport) == ""

    @pytest.mark.asyncio
    async def test_a_rolled_back_release_leaves_a_binding_made_during_the_flush_in_place(
        self,
    ) -> None:
        """The release clears the occupants, then awaits the flush; the dashboard takes
        no lock of ours, so a new binding for a cleared key can land during that await.
        When the flush then fails, the rollback owns only its CLEAR: it puts a row back
        where the key is still empty and leaves the newer binding -- deliberate state
        -- exactly as it found it, the same rule the pick applies to displaced rows.
        """
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = ChannelLink(channel_type="discord", channel_id="c1", principal="u1")
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        elsewhere = ChannelLink(channel_type="telegram", channel_id="tg-7")
        real_aflush = sessions.aflush

        async def _bind_then_fail() -> None:
            sessions.set_mirror_link("dashboard:chat-1", elsewhere, accepts_inbound=True)
            sessions.flush_error = OSError("disk full")
            await real_aflush()

        sessions.aflush = _bind_then_fail  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("!unlink"))
        assert any("NOT completed" in text for text, _ in client.sent)
        assert sessions.mirror_links["dashboard:chat-1"] == elsewhere
        assert "dashboard:chat-1" in sessions.inbound_keys

    @pytest.mark.asyncio
    async def test_a_rolled_back_release_restores_a_mirror_beside_a_slack_thread_link(
        self,
    ) -> None:
        """The ownership question is asked of the row the clear removed -- the
        entry's own mirror -- never of ``get_mirror_link``. That reader also answers
        for bindings the clear never touched: a slot that still carries its legacy
        Slack thread link reads back as a synthesized Slack mirror, so judged there
        the rollback would call the key "still bound", skip the restore, and leave
        the Discord binding cleared in this process while the user is told the
        release did NOT complete and nothing changed.
        """
        import dataclasses

        from kiro_crew.mirror_admission import sign_mirror_admission

        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = ChannelLink(channel_type="discord", channel_id="c1", principal="u1")
        genuine = dataclasses.replace(
            link, admission=sign_mirror_admission("dashboard:chat-1", link)
        )
        sessions.set_mirror_link("dashboard:chat-1", genuine, accepts_inbound=True)
        sessions.slack_threads["dashboard:chat-1"] = ("1700000000.000100", "C-ops")
        sessions.flush_error = OSError("disk full")
        await dispatcher.handle_message(_message("!unlink"))
        assert any("NOT completed" in text for text, _ in client.sent)
        restored = sessions.mirror_links.get("dashboard:chat-1")
        assert restored is not None, "the rollback skipped the restore it promised"
        assert restored == link and restored.principal == "u1"
        assert restored.admission == genuine.admission
        assert "dashboard:chat-1" in sessions.inbound_keys
        assert dispatcher._session_resume.resumed_session("c1") == "dashboard:chat-1"

    @pytest.mark.parametrize("recorded", [False, True], ids=["bare", "recorded"])
    @pytest.mark.asyncio
    async def test_a_rebind_inside_unlink_retire_keeps_its_record(self, recorded: bool) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = await _attach(dispatcher, client)
        resume, store = dispatcher._session_resume, dispatcher._session_resume._expectations
        real_retire_if = store.retire_if

        async def _rebind(channel_id: str, version: int):
            assert resume._bind_lock.locked(), "release settlement escaped the bind lock"
            applied = await real_retire_if(channel_id, version)
            sessions.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)
            if recorded:
                await store.record(channel_id, "dashboard:chat-2", "Pricing review")
            return applied

        store.retire_if = _rebind  # type: ignore[method-assign]
        await resume.leave_resumed_session("c1")
        current = await store.get("c1")
        assert current is not None and current.key == (
            "dashboard:chat-2" if recorded else "dashboard:chat-1"
        )
        assert current.retired is not recorded
        assert resume.resumed_session("c1") == "dashboard:chat-2"

    @pytest.mark.asyncio
    async def test_unlink_leaves_nothing_to_report(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        await _attach(dispatcher, client)
        await dispatcher.handle_message(_message("!unlink"))
        await dispatcher.handle_message(_message("hello"))
        assert not any("Detached" in text for text, _ in client.sent)
        assert sessions.last_key == dispatcher._session_key("u1")

    @pytest.mark.parametrize("delivered", [True, False], ids=["sent", "send-failed"])
    @pytest.mark.asyncio
    async def test_a_moved_link_is_announced_and_adopted_only_once_delivered(
        self, delivered
    ) -> None:
        log = _log_with_titles("Launch plan", "Pricing review")
        dispatcher, client, sessions = _dispatcher({"u1"}, log)
        link = await _attach(dispatcher, client)
        before = await dispatcher._session_resume._expectations.get("c1")
        sessions.clear_mirror_links_at(link)
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        sessions.last_key = ""
        client.send_fails = not delivered
        await dispatcher.handle_message(_message("carry on"))
        assert sessions.last_key == "", "the message ran in the swapped session unannounced"
        record = await dispatcher._session_resume._expectations.get("c1")
        if not delivered:
            assert record == before, "the adopt applied without the user being told"
            return
        assert "Now linked to" in client.sent[-1][0], client.sent[-1][0]
        assert record is not None and record.key == "dashboard:chat-1"
        await dispatcher.handle_message(_message("carry on"))
        assert sessions.last_key == "dashboard:chat-1"

    @pytest.mark.parametrize(
        "command, refused",
        [
            pytest.param("!compact", True, id="compact"),
            pytest.param("!stop", True, id="stop"),
            pytest.param("!link", True, id="link"),
            pytest.param("!sessions", False, id="sessions-recovers"),
            pytest.param("!new", False, id="new-leaves"),
            pytest.param("!unlink", False, id="unlink-leaves"),
            pytest.param("!help", False, id="help-inert"),
        ],
    )
    @pytest.mark.asyncio
    async def test_session_targeting_commands_are_refused_before_they_act(
        self, command, refused
    ) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = await _attach(dispatcher, client)
        sessions.clear_mirror_links_at(link)
        await dispatcher.handle_message(_message(command))
        got = [text for text, _ in client.sent if "Detached" in text]
        assert bool(got) is refused, f"{command} refusal={bool(got)}: {client.sent}"
        if refused:
            assert sessions.targeted == [], f"{command} reached a session anyway"

    @pytest.mark.parametrize("attached", [True, False], ids=["with-record", "no-record"])
    @pytest.mark.parametrize("leave_command", ["!new", "!unlink"])
    @pytest.mark.asyncio
    async def test_ambiguity_refuses_every_message_and_never_runs_natively(
        self, attached, leave_command
    ) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = (
            await _attach(dispatcher, client)
            if attached
            else ChannelLink(channel_type="discord", channel_id="c1")
        )
        for key in ("dashboard:chat-8", "dashboard:chat-9"):
            sessions.mirror_links[key] = link
            sessions.inbound_keys.add(key)
        assert dispatcher._session_resume.resolve_inbound("c1").ambiguous
        sessions.last_key = ""
        await dispatcher.handle_message(_message("first"))
        await dispatcher.handle_message(_message("!compact"))
        assert len([t for t, _ in client.sent if "Ambiguous link" in t]) == 2
        assert sessions.last_key == "", "a message ran while routing was denied"
        assert sessions.targeted == [], "!compact reached a session anyway"
        if attached:
            assert await dispatcher._session_resume._expectations.get("c1") is not None
        elsewhere = ChannelLink(channel_type="discord", channel_id="c2")
        sessions.mirror_links["dashboard:elsewhere"] = elsewhere
        await dispatcher.handle_message(_message(leave_command))
        assert not dispatcher._session_resume.resolve_inbound(
            "c1"
        ).ambiguous, f"{leave_command} left ambiguous bindings: {sessions.mirror_links}"
        assert sessions.mirror_links["dashboard:elsewhere"] == elsewhere, "cleared c2"
        await dispatcher.handle_message(_message("after leaving"))
        assert sessions.last_key == dispatcher._session_key("u1"), client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_a_dashboard_created_binding_is_tracked_from_its_first_message(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = ChannelLink(channel_type="discord", channel_id="c1")
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        dispatcher._session_resume.conv_log.metadata["dashboard:chat-1"] = {"title": "Launch plan"}
        await dispatcher.handle_message(_message("hello"))
        assert sessions.last_key == "dashboard:chat-1", "the bound session did not run"
        assert not any("Detached" in text for text, _ in client.sent)
        tracked = await dispatcher._session_resume._expectations.get("c1")
        assert tracked is not None and tracked.key == "dashboard:chat-1"
        assert tracked.title == "Launch plan"

    @pytest.mark.parametrize("delivered", [True, False], ids=["sent", "send-failed"])
    @pytest.mark.asyncio
    async def test_pre_notice_waiters_are_refused_before_settlement(
        self, delivered, monkeypatch
    ) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        store = dispatcher._session_resume._expectations
        await store.record("c1", "dashboard:chat-1", "Launch plan")
        client.send_fails = not delivered
        third_in_governance = asyncio.Event()
        release_third = asyncio.Event()
        governance_calls = 0

        async def _governance(_channel: str) -> bool:
            nonlocal governance_calls
            governance_calls += 1
            if governance_calls == 3:
                third_in_governance.set()
                # flake-ok: pre-existing unbounded wait in the stubbed governance check; the test sets release_third itself, moved verbatim, tracked for a de-flake pass
                await release_third.wait()
            return True

        monkeypatch.setitem(
            dispatcher.handle_message.__globals__,
            "channel_inbound_permitted",
            _governance,
        )
        siblings: list[asyncio.Task[None]] = []
        detached_sends = 0
        real_send = client.send_message

        async def _gate_waiters(channel_id: str, text: str, **kwargs):
            nonlocal detached_sends
            if "Detached" in text:
                detached_sends += 1
                if detached_sends == 1:
                    siblings.append(
                        asyncio.create_task(dispatcher.handle_message(_message("second")))
                    )
                    # flake-ok: pre-existing zero yield that lets the second sibling queue on the routing lock before the count is read; moved verbatim, tracked for a de-flake pass
                    await asyncio.sleep(0)
                    assert len(dispatcher._routing_locks["c1"][1]) == 2
                    siblings.append(
                        asyncio.create_task(dispatcher.handle_message(_message("third")))
                    )
                    await await_until(
                        third_in_governance, what="the third sibling entering governance"
                    )
                elif detached_sends == 2:
                    release_third.set()
            return await real_send(channel_id, text, **kwargs)

        client.send_message = _gate_waiters  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("first"))
        assert len(siblings) == 2
        # flake-ok: pre-existing unbounded await of the sibling task; moved verbatim, tracked for a de-flake pass
        await siblings[0]
        release_third.set()
        # flake-ok: pre-existing unbounded await of the sibling task once release_third is set; moved verbatim, tracked for a de-flake pass
        await siblings[1]
        assert detached_sends == 3, "a pre-notice waiter was not refused"
        record = await store.get("c1")
        assert sessions.last_key == "", "a pre-notice waiter ran natively"
        if delivered:
            assert len([t for t, _ in client.sent if "Detached" in t]) == 3
            assert record is not None and record.retired, "the last notice did not settle"
            await dispatcher.handle_message(_message("after"))
            assert sessions.last_key == dispatcher._session_key("u1")
        else:
            assert record is not None, "an undelivered notice consumed the record"
            assert sessions.last_key == "", "a message ran with nothing delivered"

    @pytest.mark.asyncio
    async def test_a_second_channel_is_not_blocked_by_the_first(self, monkeypatch) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        monkeypatch.setitem(
            dispatcher.handle_message.__globals__,
            "channel_inbound_permitted",
            client.is_thread_channel,
        )
        store = dispatcher._session_resume._expectations
        for channel in ("c1", "c2"):
            await store.record(channel, "dashboard:chat-1", "Launch plan")
        c2_routed = asyncio.Event()
        crossed: list[bool] = []
        real_route = dispatcher._session_resume.route
        real_send = client.send_message

        async def _signal_when_c2_routes(channel_id: str):
            decision = await real_route(channel_id)
            if channel_id == "c2":
                c2_routed.set()
            return decision

        async def _park_first_send_until_c2_routes(channel_id: str, text: str, **kwargs):
            if "Detached" in text and channel_id == "c1" and not crossed:
                try:
                    await await_until(c2_routed, what="c2 routing while c1's send is parked")
                    crossed.append(True)
                except AssertionError:
                    crossed.append(False)
                finally:
                    c2_routed.set()
            return await real_send(channel_id, text, **kwargs)

        dispatcher._session_resume.route = _signal_when_c2_routes  # type: ignore[method-assign]
        client.send_message = _park_first_send_until_c2_routes  # type: ignore[method-assign]
        await asyncio.gather(
            dispatcher.handle_message(_message("one", channel_id="c1")),
            dispatcher.handle_message(_message("two", channel_id="c2")),
        )
        assert crossed == [True], "c2 could not route while c1 held its own lock"
        assert len([t for t, _ in client.sent if "Detached" in t]) == 2
        assert sessions.last_key == "", "a refused message ran"


class TestExpectationStoreInvariants:

    @pytest.mark.asyncio
    async def test_the_store_is_gated_from_agent_tools_yet_writable_by_the_gateway(
        self, monkeypatch
    ) -> None:
        from kiro_crew.security import is_sensitive_path

        restricted: list[str] = []
        pc = resume_expectation.platform_compat
        monkeypatch.setattr(pc, "restrict_to_owner", restricted.append)
        store = resume_expectation.ResumeExpectations("discord")
        await store.record("c1", "dashboard:chat-1", "Launch plan")
        assert restricted, "the record was written without restricting it to its owner"
        path = store._loaded_from
        assert path is not None and is_sensitive_path(str(path)), (
            f"{path} is reachable by agent file tools -- an injected agent could "
            "delete the record and the binding together and route silently"
        )
        assert path.exists() and (await store.get("c1")) is not None

    # Read inbound and written from the picker, both ON the loop, so the recorded
    # threads are the real filesystem helpers' -- not one mocked call.
    @pytest.mark.asyncio
    async def test_every_store_filesystem_helper_runs_off_the_loop_thread(
        self, monkeypatch
    ) -> None:
        threads: list[int] = []
        for name in ("_resolve", "_write"):
            real = getattr(resume_expectation, name)

            def _record(*args, _real=real, **kwargs):
                threads.append(threading.get_ident())
                return _real(*args, **kwargs)

            monkeypatch.setattr(resume_expectation, name, _record)
        store = resume_expectation.ResumeExpectations("discord")
        loop_thread = threading.get_ident()
        await store.record("c1", "dashboard:chat-1", "Launch plan")
        current = await store.get("c1")
        assert current is not None
        assert await store.retire_if("c1", current.version) is True
        assert await store.retire_if("c1", current.version) is False
        assert threads, "no filesystem helper ran at all"
        assert loop_thread not in threads, f"store I/O ran on the loop: {threads}"

    def test_the_store_exposes_no_synchronous_accessor(self) -> None:
        cls = resume_expectation.ResumeExpectations
        public = [n for n in vars(cls) if not n.startswith("_")]
        assert public, "the store lost its public surface"
        for name in public:
            assert asyncio.iscoroutinefunction(getattr(cls, name)), f"{name} is sync"

    @pytest.mark.asyncio
    async def test_concurrent_records_do_not_lose_each_other(self) -> None:
        store = resume_expectation.ResumeExpectations("discord")
        await asyncio.gather(
            store.record("c1", "dashboard:chat-1", "One"),
            store.record("c2", "dashboard:chat-2", "Two"),
        )
        reloaded = resume_expectation.ResumeExpectations("discord")
        for channel, key in (("c1", "dashboard:chat-1"), ("c2", "dashboard:chat-2")):
            record = await reloaded.get(channel)
            assert record is not None and record.key == key, f"{channel} was lost"


class TestRoutingUnderConcurrentRebinding:

    @pytest.mark.asyncio
    async def test_an_unlink_landing_inside_the_store_read_never_falls_native(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = await _attach(dispatcher, client)
        native = dispatcher._session_key("u1")
        sessions.last_key = ""
        store = dispatcher._session_resume._expectations
        real_get = store.get

        async def _unlink_during_get(channel_id: str):
            record = await real_get(channel_id)
            # The dashboard releasing the conversation mid-decision.
            sessions.clear_mirror_links_at(link)
            return record

        store.get = _unlink_during_get  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("what did we decide?"))
        assert sessions.last_key != native, "the message ran natively with no warning"
        assert sessions.last_key == "", f"the message ran at all: {sessions.last_key}"
        assert "Detached" in client.sent[-1][0], client.sent[-1][0]

    @pytest.mark.parametrize(
        "notice, rebind_key, rebind_title, survivor",
        [
            pytest.param(
                "Detached",
                "dashboard:chat-1",
                "Pricing review",
                "dashboard:chat-1",
                id="picker-bind-vs-stale-clear",
            ),
            pytest.param(
                "Now linked to",
                "dashboard:chat-2",
                "Budget",
                "dashboard:chat-2",
                id="third-rebind-vs-stale-adopt",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_record_written_during_the_send_survives_the_stale_settle(
        self, notice, rebind_key, rebind_title, survivor
    ) -> None:
        log = _log_with_titles("Launch plan", "Pricing review", "Budget")
        dispatcher, client, sessions = _dispatcher({"u1"}, log)
        link = await _attach(dispatcher, client)
        sessions.clear_mirror_links_at(link)
        if notice == "Now linked to":
            # An owner must be live for the decision to be a hotswap rather than a loss.
            sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        fired: list[str] = []
        real_send = client.send_message

        async def _rebind_inside_send(channel_id: str, text: str, **kwargs):
            if notice in text and not fired:
                fired.append(text)
                await dispatcher._session_resume._expectations.record(
                    "c1", rebind_key, rebind_title
                )
            return await real_send(channel_id, text, **kwargs)

        client.send_message = _rebind_inside_send  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("what did we decide?"))
        assert fired, "the race never fired -- the test proves nothing"
        current = await dispatcher._session_resume._expectations.get("c1")
        assert (
            current is not None and current.key == survivor and not current.retired
        ), "the stale settle overwrote the record written during the send"

    @pytest.mark.parametrize(
        "text",
        ["hello", "!compact", "!stop", "!link"],
        ids=["message", "compact", "stop", "link"],
    )
    @pytest.mark.asyncio
    async def test_nothing_re_resolves_the_binding_after_the_decision(self, text) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        await _attach(dispatcher, client)
        resume = dispatcher._session_resume
        real_route = resume.route

        def _forbidden(channel_id: str):
            raise AssertionError("the binding was re-resolved after the decision")

        async def _route_then_seal(channel_id: str):
            decision = await real_route(channel_id)
            resume.resolve_inbound = _forbidden  # type: ignore[method-assign]
            return decision

        resume.route = _route_then_seal  # type: ignore[method-assign]
        await dispatcher.handle_message(_message(text))
        targeted = [key for _verb, key in sessions.targeted]
        assert all(
            k == "dashboard:chat-1" for k in targeted
        ), f"{text} acted on {targeted} instead of the decided session"
        if text == "hello":
            assert sessions.last_key == "dashboard:chat-1"

    @pytest.mark.parametrize("rebound", [False, True], ids=["unlink", "rebind"])
    @pytest.mark.asyncio
    async def test_a_bootstrap_owner_that_moves_mid_decision_is_refused(self, rebound) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = ChannelLink(channel_type="discord", channel_id="c1")
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        resume = dispatcher._session_resume

        async def _move_during_title(session_key: str) -> str:
            # The dashboard moves the conversation while the decision reads the title.
            sessions.clear_mirror_links_at(link)
            if rebound:
                sessions.set_mirror_link("dashboard:chat-9", link, accepts_inbound=True)
            return "Launch plan"

        resume._title_of = _move_during_title  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("what did we decide?"))
        assert sessions.last_key == "", f"the message ran at all: {sessions.last_key}"
        assert "changing right now" in client.sent[-1][0], client.sent[-1][0]
        assert await resume._expectations.get("c1") is None, "the moved owner was recorded"


class TestPersistenceFailureIsFailClosed:

    @staticmethod
    def _break_writes(monkeypatch) -> dict:
        broken = {"fail": True}
        real_write = resume_expectation.atomic_write

        def _enospc(*args, **kwargs):
            if broken["fail"]:
                raise OSError(28, "No space left on device")
            return real_write(*args, **kwargs)

        monkeypatch.setattr(resume_expectation, "atomic_write", _enospc)
        return broken

    @pytest.mark.asyncio
    async def test_the_store_raises_instead_of_reporting_a_lost_write(self, monkeypatch) -> None:
        store = resume_expectation.ResumeExpectations("discord")
        self._break_writes(monkeypatch)
        with pytest.raises(resume_expectation.ExpectationStoreError):
            await store.record("c1", "dashboard:chat-0", "Launch plan")
        assert await store.get("c1") is None, "in-memory state kept an undurable record"

    @pytest.mark.parametrize("side", ["write", "read"])
    @pytest.mark.asyncio
    async def test_a_store_it_cannot_trust_refuses_until_repaired(self, side, monkeypatch) -> None:
        """Read and write failures refuse until the store is repaired."""
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = ChannelLink(channel_type="discord", channel_id="c1")
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        broken = self._break_writes(monkeypatch) if side == "write" else None
        if broken is None:
            _store_path().write_text('{"c1": {"key": "dash', encoding="utf-8")

        await dispatcher.handle_message(_message("hello"))
        assert sessions.last_key == "", "the turn ran on a store it could not trust"
        assert "Can't read or save" in client.sent[-1][0], client.sent[-1][0]
        _store_path().unlink() if broken is None else broken.update(fail=False)
        await dispatcher.handle_message(_message("hello again"))
        assert sessions.last_key == "dashboard:chat-1", "no recovery after repair"

    @pytest.mark.parametrize("mode", ["alone", "concurrent-bind", "claim-lost"])
    @pytest.mark.asyncio
    async def test_a_pick_that_cannot_take_effect_binds_nothing(self, mode, monkeypatch) -> None:
        """A failed pick binds nothing and never rolls back a concurrent bind."""
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        await dispatcher.handle_message(_message("!sessions"))
        custom_id, message_id = _picker_button(client)
        target = ChannelLink(channel_type="discord", channel_id="c1")
        fired: list[str] = []

        async def _fail_the_write(channel_id, key, title):
            if mode == "concurrent-bind":  # the dashboard completing the same bind
                sessions.set_mirror_link(key, target, accepts_inbound=True)
            fired.append(key)
            raise resume_expectation.ExpectationStoreError("no space left on device")

        def _lose_the_claim(key, link, *, accepts_inbound=False):
            fired.append(key)
            raise ConversationOwnershipConflict("claimed while we were writing")

        if mode == "claim-lost":
            monkeypatch.setattr(sessions, "set_mirror_link", _lose_the_claim)
        else:
            monkeypatch.setattr(dispatcher._session_resume._expectations, "record", _fail_the_write)

        await dispatcher.on_interaction(_interaction(custom_id, message_id))
        assert fired, "the pick never reached the failure -- the test proves nothing"
        assert sessions.find_mirror_sessions(target, inbound_only=True) == (
            [fired[0]] if mode == "concurrent-bind" else []
        ), "a bind outlived an unpersisted record, or a rollback erased a live one"
        edits = [text for _mid, text, _c in client.edits]
        if mode == "claim-lost":
            assert any("Another session just connected" in t for t in edits), edits
            assert not any(
                "Couldn't save" in t for t in edits
            ), f"a claim race was reported as a storage failure: {edits}"
            return
        assert any("NOT resumed" in t for t in edits), edits

    @pytest.mark.asyncio
    async def test_a_settle_that_cannot_persist_refuses_again(self, monkeypatch) -> None:
        """An unpersisted settle is not done, so the refusal is still owed."""
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        sessions.clear_mirror_links_at(await _attach(dispatcher, client))
        self._break_writes(monkeypatch)

        await dispatcher.handle_message(_message("what did we decide?"))
        await dispatcher.handle_message(_message("again"))
        assert (
            len([t for t, _ in client.sent if "Detached" in t]) == 2
        ), "the unpersisted settle was treated as done"
        assert sessions.last_key == "", "a refused message ran"

    @pytest.mark.parametrize(
        "payload, loads",
        [
            pytest.param(
                '{"c1": {"key": "dashboard:chat-0", "title": "T", "version": 3}}',
                True,
                id="well-formed-row",
            ),
            pytest.param(
                '{"c1": {"key": "dashboard:chat-0", "title": "T"}}', False, id="row-missing-version"
            ),
            pytest.param("{not json", False, id="malformed-json"),
            pytest.param('["c1"]', False, id="wrong-toplevel-shape"),
            pytest.param('{"c1": {"title": "T"}}', False, id="row-missing-key"),
            pytest.param('{"c1": {"key": "k", "version": "x"}}', False, id="non-numeric-version"),
            pytest.param('{"c1": {"key": "k", "version": true}}', False, id="boolean-version"),
            pytest.param(
                '{"c1": {"key": "k", "version": 1, "retired": "yes"}}',
                False,
                id="non-boolean-retired",
            ),
            pytest.param('{"c1": {"key": "k", "version": "1"}}', False, id="string-version"),
            pytest.param('{"c1": {"key": "k", "version": 1.25}}', False, id="fractional-version"),
            pytest.param(b"\xff\xfe{}", False, id="invalid-utf8-bytes"),
            pytest.param('{"c1": {"key": "k", "version": 1e999}}', False, id="infinite-version"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_store_that_cannot_be_trusted_is_not_an_empty_store(
        self, payload, loads
    ) -> None:
        """Corruption read as "no records" says never-attached, so the turn runs
        natively -- this bug's fail-open form."""
        path = _store_path()
        path.write_bytes(payload) if isinstance(payload, bytes) else path.write_text(payload)
        store = resume_expectation.ResumeExpectations("discord")
        if loads:
            record = await store.get("c1")
            assert record is not None and record.version == 3
            return
        with pytest.raises(resume_expectation.ExpectationStoreError):
            await store.get("c1")


class TestSettlementReconcilesLiveState:
    """Settlement reconciles dashboard rebinds that do not bump store versions."""

    @pytest.mark.parametrize(
        "rebind_keys, expect_second_runs",
        [
            pytest.param(["dashboard:chat-1"], False, id="different-key"),
            pytest.param(["dashboard:chat-0"], True, id="same-key"),
            pytest.param([], True, id="absent"),
            pytest.param(["dashboard:chat-1", "dashboard:chat-2"], False, id="ambiguous"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_owner_arriving_during_the_send_is_never_entered_silently(
        self, rebind_keys, expect_second_runs
    ) -> None:
        """Barrier-controlled: the rebind lands strictly inside the notice send."""
        log = _log_with_titles("Launch plan", "Pricing review", "Budget")
        dispatcher, client, sessions = _dispatcher({"u1"}, log)
        link = await _attach(dispatcher, client)
        sessions.clear_mirror_links_at(link)

        async def _rebind():
            for key in rebind_keys:
                sessions.mirror_links[key] = link
                sessions.inbound_keys.add(key)

        released = _gate_send(client, "Detached", _rebind)
        await dispatcher.handle_message(_message("what did we decide?"))
        assert released, "the interleaving never fired -- the test proves nothing"
        sessions.last_key = ""
        await dispatcher.handle_message(_message("resending this"))
        if expect_second_runs:
            assert sessions.last_key != "", "the resend was refused with nothing pending"
        else:
            assert (
                sessions.last_key == ""
            ), f"the resend entered {sessions.last_key} without a notice"
            assert "NOT processed" in client.sent[-1][0], client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_an_adopt_whose_owner_moved_again_adopts_nothing(self) -> None:
        """The notice named B; if the owner is C by the time it lands, adopting B
        records a link the user was never in."""
        log = _log_with_titles("Launch plan", "Pricing review", "Budget")
        dispatcher, client, sessions = _dispatcher({"u1"}, log)
        link = await _attach(dispatcher, client)
        before = await dispatcher._session_resume._expectations.get("c1")
        assert before is not None and before.key == "dashboard:chat-0"
        sessions.clear_mirror_links_at(link)
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

        async def _move_again():
            sessions.clear_mirror_links_at(link)
            sessions.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)

        moved = _gate_send(client, "Now linked to", _move_again)
        await dispatcher.handle_message(_message("carry on"))
        assert moved, "the race never fired -- the test proves nothing"
        assert (
            await dispatcher._session_resume._expectations.get("c1") == before
        ), "adopted a session whose notice no longer described the live state"
        sessions.last_key = ""
        await dispatcher.handle_message(_message("resending"))
        assert sessions.last_key == "", "the resend entered the third owner in silence"
        assert (
            "Launch plan" in client.sent[-1][0]
        ), f"the notice named a link the user was never in: {client.sent[-1][0]}"

    @pytest.mark.parametrize("mode", ["bare", "same-key", "recorded", "adopt-fails"])
    @pytest.mark.asyncio
    async def test_an_owner_arriving_during_retirement_keeps_durable_evidence(
        self, mode: str, monkeypatch
    ) -> None:
        log = _log_with_titles("Launch plan", "Pricing review")
        dispatcher, client, sessions = _dispatcher({"u1"}, log)
        link = await _attach(dispatcher, client)
        sessions.clear_mirror_links_at(link)
        store = dispatcher._session_resume._expectations
        real_aflush = sessions.aflush
        raced: list[str] = []

        async def _rebind_during_aflush() -> None:
            await real_aflush()
            rebind_key = "dashboard:chat-0" if mode == "same-key" else "dashboard:chat-1"
            sessions.mirror_links[rebind_key] = link
            sessions.inbound_keys.add(rebind_key)
            if mode == "recorded":
                await store.record("c1", "dashboard:chat-1", "Pricing review")
            raced.append("c1")

        sessions.aflush = _rebind_during_aflush  # type: ignore[method-assign]
        if mode == "adopt-fails":
            real_write = resume_expectation.atomic_write
            writes = {"count": 0}

            def _fail_second_write(*args, **kwargs):
                writes["count"] += 1
                if writes["count"] == 2:
                    raise OSError(28, "No space left on device")
                return real_write(*args, **kwargs)

            monkeypatch.setattr(resume_expectation, "atomic_write", _fail_second_write)
        await dispatcher.handle_message(_message("what did we decide?"))
        assert raced, "the rebind did not land inside aflush"
        current = await store.get("c1")
        assert current is not None, "retirement deleted the evidence beside a live owner"
        if mode == "recorded":
            assert current.key == "dashboard:chat-1" and not current.retired
            return
        assert current.key == "dashboard:chat-0" and current.retired
        sessions.last_key = ""
        await dispatcher.handle_message(_message("resending this"))
        assert sessions.last_key == "", "the resend entered the new owner in silence"
        assert "Now linked to" in client.sent[-1][0], client.sent[-1][0]
        current = await store.get("c1")
        assert current is not None and current.retired is (mode == "adopt-fails")
