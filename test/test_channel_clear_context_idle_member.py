"""A live channel member must not refuse clear-context for its whole lifetime.

`run_channel_agent` takes the session lease once at spawn and releases it only when the
member dies, so a busy probe that reads the LEASE answers "busy" for as long as the member
exists. The contract this PR ships says a 409 is retryable once the named roles finish, and
the banner tells the user to retry when idle -- so a lease-scoped probe makes that retry
unsatisfiable on any channel with live members, idle ones included.

These drive the REAL `discard_conversation`, not a stub: a stubbed teardown returns whatever
the test says and so cannot observe the predicate at all.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import armed_agent, armed_cwd

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager


def _provider_factory(*, active_turn: bool):
    def factory(session_key=None, agent=None, channel_id=None, cwd=None, **kwargs):
        provider = MagicMock()
        provider.start = MagicMock(return_value=asyncio.sleep(0))
        provider.shutdown = MagicMock(return_value=asyncio.sleep(0))
        provider.cwd = cwd or "/unset"
        provider.context_usage_pct = MagicMock(return_value=0.0)
        provider.is_alive = MagicMock(return_value=True)
        provider.is_process_alive = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=active_turn)
        provider.runtime_info = MagicMock(return_value=(None, None))
        return provider

    return factory


async def _member_holding_its_lifetime_lease(mgr: SessionManager, key: str) -> None:
    """Reproduce what `run_channel_agent` does: acquire once, mark, never release."""
    await mgr.get_or_create(key)
    marked = mgr.mark_lifecycle_lease(key)
    assert marked, "precondition: no session was registered, so nothing carried the lease"


class TestAnIdleListeningMemberDoesNotRefuseForever:
    @pytest.mark.asyncio
    async def test_clear_context_clears_while_a_member_holds_its_lifecycle_lease(self):
        mgr = SessionManager(
            KiroCrewConfig(), provider_factory=_provider_factory(active_turn=False)
        )
        key = "channel:ch-ops:analyst"
        await _member_holding_its_lifetime_lease(mgr, key)

        boundary = mgr._allocation_boundary()
        folded = mgr._fold_key(key)
        assert boundary._sessions[
            folded
        ].semaphore.locked(), (
            "precondition: the member is not holding the lease, so this proves nothing"
        )

        torn_down = await mgr.discard_conversation(
            key, skip_if_busy=True, refuse_only_on_active_turn=True
        )
        assert torn_down, (
            "an IDLE listening member refused the clear, so the 409 the banner tells the user "
            "to retry when idle can never succeed on any channel with live members"
        )

    @pytest.mark.asyncio
    async def test_a_member_with_a_reply_in_flight_still_refuses(self):
        mgr = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory(active_turn=True))
        key = "channel:ch-ops:researcher"
        await _member_holding_its_lifetime_lease(mgr, key)

        torn_down = await mgr.discard_conversation(
            key, skip_if_busy=True, refuse_only_on_active_turn=True
        )
        assert not torn_down, (
            "the teardown ran while the member had a reply in flight, which destroys the "
            "streaming turn the refusal exists to protect"
        )

    @pytest.mark.asyncio
    async def test_a_plain_turn_lease_still_refuses_without_asking_the_provider(self):
        # The dashboard holder takes the lease for ONE turn, and may hold it before any prompt
        # is in flight, which `has_active_turn` cannot see. That holder must stay strict.
        mgr = SessionManager(
            KiroCrewConfig(), provider_factory=_provider_factory(active_turn=False)
        )
        key = "dashboard:chat-1"
        await mgr.get_or_create(key)

        torn_down = await mgr.discard_conversation(key, skip_if_busy=True)
        assert not torn_down, (
            "a turn-scoped lease was treated as idle, so a teardown can land on a turn that "
            "has acquired but not yet put a prompt in flight"
        )


class TestAClearDuringPreStreamSetupDoesNotDestroyTheSession:
    """A member's turn begins when it DEQUEUES, not when the provider registers a prompt.

    Between dequeue and the prompt going out the member builds its context, and the provider
    reports no active turn for that whole window. A clear arriving there tears the session
    down mid-setup, so the message is dropped with a visible error.
    """

    @pytest.mark.asyncio
    async def test_a_declared_turn_refuses_the_clear_before_the_prompt_is_in_flight(self):
        mgr = SessionManager(
            KiroCrewConfig(), provider_factory=_provider_factory(active_turn=False)
        )
        key = "channel:ch-ops:dev"
        await _member_holding_its_lifetime_lease(mgr, key)

        # The pre-stream window: dequeued and working, but the provider still reports idle.
        assert mgr.set_lifecycle_turn_active(key, True), "precondition: nothing was registered"
        provider = mgr._allocation_boundary()._sessions[mgr._fold_key(key)].provider
        assert (
            provider.has_active_turn() is False
        ), "precondition: the provider already reports a turn, so this is not the window"

        cleared = await mgr.discard_conversation(
            key, skip_if_busy=True, refuse_only_on_active_turn=True
        )
        assert not cleared, (
            "the clear tore the session down while the member was mid-setup, so its message "
            "is dropped with an error instead of being served"
        )
        assert (
            provider.shutdown.call_count == 0
        ), "the provider was shut down during the pre-stream window"

    @pytest.mark.asyncio
    async def test_the_clear_succeeds_once_the_turn_is_released(self):
        # The refusal must be the retryable kind the banner promises, not a new permanent one.
        mgr = SessionManager(
            KiroCrewConfig(), provider_factory=_provider_factory(active_turn=False)
        )
        key = "channel:ch-ops:dev"
        await _member_holding_its_lifetime_lease(mgr, key)
        mgr.set_lifecycle_turn_active(key, True)
        mgr.set_lifecycle_turn_active(key, False)

        cleared = await mgr.discard_conversation(
            key, skip_if_busy=True, refuse_only_on_active_turn=True
        )
        assert cleared, (
            "a released turn still refused the clear, so the retry-when-idle the banner "
            "offers can never succeed"
        )


class TestAReacquiredSessionKeepsItsTurnProtection:
    """A reacquire happens MID-TURN, so the fresh session must carry the turn as well.

    Both recovery paths mint a new session whose turn flag defaults False. Marking only the
    lease leaves the setup that follows unprotected, so a clear arriving there tears down the
    provider the replayed message is about to stream.
    """

    @pytest.mark.asyncio
    async def test_a_clear_cannot_tear_down_a_session_reacquired_mid_turn(self):
        import kiro_crew.channel as channel_mod

        mgr = SessionManager(
            KiroCrewConfig(), provider_factory=_provider_factory(active_turn=False)
        )
        key = "channel:ch-ops:dev"
        agent = SimpleNamespace(
            session_key=key,
            agent_name=None,
            approval_policy=SimpleNamespace(value=""),
        )

        client = await channel_mod._reacquire_cleared_session(mgr, agent)
        assert client is not None, "precondition: the reacquire failed"

        session = mgr._allocation_boundary()._sessions[mgr._fold_key(key)]
        assert session.lifecycle_lease, "precondition: the lease was not declared"

        cleared = await mgr.discard_conversation(
            key, skip_if_busy=True, refuse_only_on_active_turn=True
        )
        assert not cleared, (
            "a clear tore down a session reacquired mid-turn, so the message being replayed "
            "streams a provider that is already gone"
        )


class TestAnAgentOnlyArmDoesNotForceAColdStartEveryTurn:
    """An arm that states no directory states no directory REQUIREMENT.

    A cwd-less claim -- which is what a channel turn is -- cannot state agreement about a
    directory, so its binding has to settle the question. When the arm carries no directory
    there is nothing to settle, and demanding one refuses every such claim forever: the
    session is retired and cold started on each turn.
    """

    @pytest.mark.asyncio
    async def test_a_cwdless_claim_reuses_the_session_under_an_agent_only_arm(self):
        mgr = SessionManager(
            KiroCrewConfig(), provider_factory=_provider_factory(active_turn=False)
        )
        key = "channel:ch-ops:analyst"
        boundary = mgr._allocation_boundary()
        folded = mgr._fold_key(key)

        # An AGENT-only arm: no directory is stated, which is what `None` records.
        mgr.mark_retire_on_next_claim(key, None, agent="kirocrew")
        assert (
            armed_cwd(boundary, folded) is None
        ), "precondition: a directory was armed, so this is not an agent-only arm"
        assert armed_agent(boundary, folded) is not None, "precondition: no agent was armed"

        # The arm covers a session not yet registered, so the first claim registers one and
        # leaves the arm standing.
        first, _, _ = await mgr.get_or_create(key)
        mgr.release(key)

        # The turn after: this claim states no directory, the arm states no directory
        # requirement, and the agent matches -- so the arm is answered and SPENT.
        second, _, _ = await mgr.get_or_create(key)
        mgr.release(key)
        assert armed_agent(boundary, folded) is None, (
            "the arm was never answered, so it stands over this key forever and every turn "
            "pays a cold start"
        )
        assert second is first, (
            "a cwd-less claim was refused its own session under an agent-only arm, which is a "
            "cold start on every turn"
        )


class TestABackgroundSweepLeavesAListeningMemberAlone:
    """A sweep must not tear down the provider a channel listener has cached.

    `run_channel_agent` fetches its provider ONCE and streams every later message through
    that same object, with no re-fetch anywhere -- so a sweep that recycles the session
    leaves the listener driving a dead provider, and its stream error is caught, reported
    and swallowed, leaving the same dead object in place for every message after.

    The user-initiated clear reads the TURN, because the retry it offers has to be able to
    succeed. A sweep reads the LEASE, because a live member is exactly what it must skip.
    """

    @pytest.mark.asyncio
    async def test_an_idle_sweep_does_not_recycle_a_member_that_holds_its_lease(self):
        mgr = SessionManager(
            KiroCrewConfig(), provider_factory=_provider_factory(active_turn=False)
        )
        key = "channel:ch-ops:scribe"
        await _member_holding_its_lifetime_lease(mgr, key)

        boundary = mgr._allocation_boundary()
        folded = mgr._fold_key(key)
        cached_provider = boundary._sessions[folded].provider

        # What every skip_if_busy SWEEP does -- idle cleanup and compaction alike.
        recycled = await mgr.reset(key, skip_if_busy=True)
        assert not recycled, (
            "a background sweep recycled a listening member, so the provider its loop cached "
            "is dead and every later message drives a dead session with nothing to re-fetch it"
        )

        # The subsequent message: the listener still holds a registered, live provider.
        still_registered = boundary._sessions.get(folded)
        assert still_registered is not None, "the sweep removed the member's session entirely"
        assert still_registered.provider is cached_provider, (
            "the provider the listener cached was replaced, so its next message streams "
            "through an object no longer registered for the key"
        )
        assert cached_provider.shutdown.call_count == 0, (
            "the sweep shut the cached provider down, which is the teardown the listener "
            "cannot observe and cannot recover from"
        )

    @pytest.mark.asyncio
    async def test_the_user_initiated_clear_still_clears_the_same_member(self):
        # The two callers must not collapse back into one answer: the sweep above is refused
        # while this clear, on an identical idle member, still succeeds.
        mgr = SessionManager(
            KiroCrewConfig(), provider_factory=_provider_factory(active_turn=False)
        )
        key = "channel:ch-ops:scribe"
        await _member_holding_its_lifetime_lease(mgr, key)

        cleared = await mgr.discard_conversation(
            key, skip_if_busy=True, refuse_only_on_active_turn=True
        )
        assert cleared, (
            "exempting sweeps also blocked the user's clear, so the retry-when-idle refusal "
            "is unsatisfiable again"
        )

    @pytest.mark.asyncio
    async def test_a_session_the_listener_acquires_still_answers_a_user_clear(self):
        """The INVARIANT, stated as the outcome it protects rather than as a marker.

        A session held for a member's whole listening life must stay distinguishable from a
        turn in flight, or the user's clear is refused for as long as the member exists. So
        each acquire path is driven, its TURN released as a finished turn releases it, and the
        clear must then succeed -- the lease is still held, and holding it alone may not refuse.
        Naming no mechanism, a rewrite that keeps clears working passes however it does it,
        and dismantling the arm store does not make this test wrong.
        """
        import kiro_crew.channel as channel_mod

        for helper in (
            channel_mod._reacquire_cleared_session,
            channel_mod._reset_busy_session,
        ):
            mgr = SessionManager(
                KiroCrewConfig(), provider_factory=_provider_factory(active_turn=False)
            )
            key = f"channel:ch-ops:{helper.__name__.strip('_')}"
            agent = SimpleNamespace(
                session_key=key,
                agent_name=None,
                approval_policy=SimpleNamespace(value=""),
            )
            assert (
                await helper(mgr, agent) is not None
            ), f"precondition: {helper.__name__} did not acquire, so this proves nothing"

            # Both helpers run MID-TURN, so each declares the turn: a clear is refused here
            # by design. The lease outlives the turn, which is what the next step isolates.
            assert not await mgr.discard_conversation(
                key, skip_if_busy=True, refuse_only_on_active_turn=True
            ), f"{helper.__name__} left its mid-turn session clearable"

            mgr.set_lifecycle_turn_active(key, False)
            cleared = await mgr.discard_conversation(
                key, skip_if_busy=True, refuse_only_on_active_turn=True
            )
            assert cleared, (
                f"the session {helper.__name__} acquired refuses the user's clear with NO turn "
                "running, so a member holding it can never be cleared"
            )

    def test_no_acquire_site_sits_outside_the_functions_these_controls_drive(self):
        """Completeness only: a NEW acquire site must not appear without a control.

        Discovery is from source because an unexercised site is invisible at runtime, but the
        assertion is a SUBSET rather than a source-text match: removing acquire sites (which
        is what dismantling the arm store does) shrinks the left side and still passes, while
        adding one in a function no control drives fails.
        """
        import re
        from pathlib import Path

        import kiro_crew.channel as channel_mod

        # Explicit encoding: the default is the PLATFORM codec, and this module carries
        # non-ASCII, so a Windows runner decodes it as cp1252 and raises.
        source = Path(channel_mod.__file__).read_text(encoding="utf-8").splitlines()
        enclosing = re.compile(r"^(?:async )?def (\w+)")

        sites: dict[int, str] = {}
        for i, line in enumerate(source):
            if "sessions.get_or_create(" not in line:
                continue
            for j in range(i, -1, -1):
                m = enclosing.match(source[j])
                if m:
                    sites[i + 1] = m.group(1)
                    break
        assert sites, "precondition: the acquire spelling changed, so this reads nothing"

        driven = {
            "run_channel_agent",
            "_reacquire_cleared_session",
            "_reset_busy_session",
        }
        strays = {line: fn for line, fn in sites.items() if fn not in driven}
        assert not strays, (
            "these acquire sites are in functions no control here drives, so nothing checks "
            f"that a clear still works against the session they hold: {strays}"
        )
