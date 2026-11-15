"""Chat runtime sharing: a session leaving a shared process.

Split out of ``test_chat_runtime_sharing`` by the file line cap. Its doubles
(``KillableRuntime``, ``FakeHandle``, ``shared_provider``) stay there and are
imported from it, as ``test_chat_runtime_sharing_cleanup`` does.
"""

from __future__ import annotations

import asyncio

import pytest
from test_chat_runtime_sharing import (
    FakeHandle,
    KillableRuntime,
    a_key,
    shared_provider,
    spawner,
)

import kiro_crew.runtime_ownership as ro


class TestSharedShutdownAsksTheTableWhoIsLeft:
    """The lease table, not the provider, decides whether the process dies.

    Driven through the real ``shutdown`` because the bug this arm exists to
    prevent -- a session's close ending its co-tenants' process -- lives in the
    branch choice, not in the table.
    """

    def test_a_joining_session_leaving_does_not_kill_the_shared_process(self):
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9200)
            live = ro.RUNTIME_OWNERSHIP
            one = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            two = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))

            provider, handle = shared_provider(rt, "dashboard:chat-2-1", two.lease)
            asyncio.run(provider.shutdown())

            assert rt.kills == [], "a joining session killed its co-tenant's process"
            assert rt.is_alive()
            assert handle.destroyed == 1, "the joiner must still be evicted from the process"
            # The remaining lease still holds it, so ITS release is the one that
            # hands the runtime back to be killed.
            assert asyncio.run(live.release(one.lease)) is rt
        finally:
            ro._reset_for_tests()

    def test_a_resume_racing_the_eviction_keeps_its_queue(self):
        """The lease is returned only after the eviction, so a resume cannot join early.

        Unregistering pops the session queue by session id with no owner check. A
        resume of this same chat that joined while the old session was still being
        evicted would register that id on this process and lose its queue to the
        eviction. Holding the lease until the eviction ends keeps it off.
        """
        ro._reset_for_tests()
        try:
            rt, elsewhere = KillableRuntime(pid=9203), KillableRuntime(pid=9204)
            live = ro.RUNTIME_OWNERSHIP
            leaving = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            other = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))
            provider, handle = shared_provider(rt, "dashboard:chat-1-1", leaving.lease)
            resumed: list = []

            async def destroy_while_a_resume_starts() -> None:
                handle.destroyed += 1
                placed = await live.acquire(
                    a_key(), "dashboard:chat-1-1", spawner(elsewhere), cap=10
                )
                resumed.append(placed)

            handle.destroy = destroy_while_a_resume_starts  # type: ignore[method-assign]
            asyncio.run(provider.shutdown())

            assert handle.destroyed == 1
            assert resumed[0].runtime is elsewhere, (
                "a resume of the chat being evicted joined its process, where the "
                "eviction would drop the queue it just registered"
            )
            assert rt.kills == []
            assert asyncio.run(live.release(other.lease)) is rt
        finally:
            ro._reset_for_tests()

    def test_the_founder_leaving_first_does_not_kill_it_either(self):
        """The founder cannot be told apart at teardown time, and must not try."""
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9201)
            live = ro.RUNTIME_OWNERSHIP
            one = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            two = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))

            provider, handle = shared_provider(rt, "dashboard:chat-1-1", one.lease)
            asyncio.run(provider.shutdown())

            assert rt.kills == []
            assert rt.is_alive()
            assert handle.destroyed == 1
            assert asyncio.run(live.release(two.lease)) is rt
        finally:
            ro._reset_for_tests()

    def test_the_last_session_out_kills_the_process(self):
        """The other direction: a shared process must not be leaked either."""
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9202)
            live = ro.RUNTIME_OWNERSHIP
            one = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            two = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))

            first, _ = shared_provider(rt, "dashboard:chat-1-1", one.lease)
            asyncio.run(first.shutdown())
            second, second_handle = shared_provider(rt, "dashboard:chat-2-1", two.lease)
            asyncio.run(second.shutdown())

            assert len(rt.kills) == 1, "the last holder must end the process"
            assert not rt.is_alive()
            assert second_handle.destroyed == 1
        finally:
            ro._reset_for_tests()

    def test_a_same_key_race_loser_does_not_kill_the_winners_process(self):
        """Two starts for ONE session key; the loser tears down; the winner lives.

        The allocator produces this shape on purpose -- it carries a race budget
        for it -- so the loser's shutdown must not end the registered winner's
        process. The loser takes its OWN process (``_Entry.admits``), and every
        acquisition minting its own lease is what keeps the two releasable
        independently; this is the pin that catches a table keyed by session.
        """
        ro._reset_for_tests()
        try:
            rt, loser_rt = KillableRuntime(pid=9203), KillableRuntime(pid=9213)
            spawn = spawner(rt, loser_rt)
            live = ro.RUNTIME_OWNERSHIP
            winner = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=10))
            loser = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=10))
            assert winner.lease != loser.lease
            assert loser.runtime is loser_rt, "a second start of one session needs its own process"

            losing, losing_handle = shared_provider(loser_rt, "dashboard:chat-1-1", loser.lease)
            asyncio.run(losing.shutdown())

            assert rt.kills == [], "the race loser killed the winner's process"
            assert rt.is_alive()
            assert len(loser_rt.kills) == 1, "the loser must still end its own process"
            assert losing_handle.destroyed == 1
            assert asyncio.run(live.release(winner.lease)) is rt
        finally:
            ro._reset_for_tests()

    def test_an_in_flight_turn_is_cancelled_before_the_handle_goes(self):
        """An abandoned prompt would keep running on a process nothing may kill."""
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9204)
            live = ro.RUNTIME_OWNERSHIP
            acq = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))

            provider, handle = shared_provider(rt, "dashboard:chat-1-1", acq.lease)
            handle.is_turn_active = True
            asyncio.run(provider.shutdown())

            assert handle.cancelled == 1
            assert handle.destroyed == 1
            assert rt.kills == []
        finally:
            ro._reset_for_tests()

    def test_the_transcript_survives_the_destroy(self):
        """A shared session HAS to destroy its handle, so it must ask to keep it.

        The sole-owner arm keeps the transcript by never reaching ``destroy`` for
        a persistent session. This arm reaches it every time.
        """
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9205)
            live = ro.RUNTIME_OWNERSHIP
            acq = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            provider, handle = shared_provider(rt, "dashboard:chat-1-1", acq.lease)

            asyncio.run(provider.shutdown())

            assert handle.keep_transcript is True
            assert handle.destroyed == 1
        finally:
            ro._reset_for_tests()

    def test_a_non_persistent_shared_session_does_not_keep_its_transcript(self):
        """The one mode whose files are meant to go. Guards the arm above."""
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9206)
            live = ro.RUNTIME_OWNERSHIP
            acq = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            provider, handle = shared_provider(rt, "dashboard:chat-1-1", acq.lease)
            provider.memory_mode = "incognito"

            asyncio.run(provider.shutdown())

            assert handle.keep_transcript is False
        finally:
            ro._reset_for_tests()

    def test_the_lease_is_released_even_when_the_cancel_is_cancelled(self):
        """A lease never returned keeps its entry alive and strands the process.

        The restart path awaits ``shutdown`` under a timeout larger than the
        cancel budget, so a slow cancel is exactly where a ``CancelledError``
        lands -- and ``CancelledError`` is a ``BaseException``, which is why the
        release is in a ``finally`` rather than after an ``except Exception``.
        """
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9207)
            live = ro.RUNTIME_OWNERSHIP
            acq = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            other = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))
            provider, handle = shared_provider(rt, "dashboard:chat-1-1", acq.lease)
            handle.is_turn_active = True

            async def cancel_forever() -> None:
                raise asyncio.CancelledError()

            handle.cancel = cancel_forever  # type: ignore[method-assign]

            with pytest.raises(asyncio.CancelledError):
                asyncio.run(provider.shutdown())

            assert live.leases_on_runtime(rt) == 1, "the lease was not released"
            assert handle.destroyed == 1, "the session was not evicted from the process"
            assert asyncio.run(live.release(other.lease)) is rt
        finally:
            ro._reset_for_tests()

    def test_a_failed_start_releases_a_shared_runtime_rather_than_killing_it(self):
        """Structural, because the behavioural reach is the whole of ``start()``.

        The cleanup arm in ``AcpProvider.start`` runs when session setup fails
        after the spawn. On a shared process a kill there would end co-tenants'
        sessions over a failure that is not theirs -- and would be refused by the
        gate anyway, which turns the leak the arm exists to prevent into a
        permanent one. What is pinned is that the arm has a shared branch, that
        the branch releases, and that it evicts the session it may have created.

        A source assertion is weaker than driving the path, and it is named as
        such rather than dressed up: it catches the branch being deleted or
        stripped, which is what a later edit does, and not a subtly wrong ordering
        inside it.
        """
        import re
        from pathlib import Path

        import kiro_crew.providers.acp_runtime_start as start_mod

        source = Path(start_mod.__file__).read_text(encoding="utf-8")
        arm_start = source.index("except BaseException as setup_exc:")
        region = source[arm_start : source.index("raise\n", arm_start)]
        shared = region[region.index("if share is not None and share.lease is not None:") :]
        shared = shared[: shared.index("else:")]
        assert "await share.abandon(runtime" in shared, (
            "the shared branch of the failed-setup cleanup no longer leaves through "
            "the placement (evict, release, kill only the last holder)"
        )
        # The kill of the runtime it was handed belongs to the sole-owner branch.
        assert not re.search(r"await runtime\.kill\(", shared), (
            "the shared branch kills the runtime it was handed; only the runtime "
            "the release HANDS BACK may be killed"
        )
        # The behaviour of ``abandon`` itself is driven in TestChatSharePlacement.

    def test_a_cancellation_during_the_release_cannot_orphan_the_lease(self):
        """The release is an await, so a cancellation can land inside it.

        Clearing the slot before the release completes would leave the lease
        neither released nor recoverable: the slot is empty, so no retry can find
        it, and the gate then refuses every kill of that pid for the gateway's
        life. That is the leak this arm exists to prevent, made permanent by the
        bookkeeping -- so the release is shielded and the slot is cleared only once
        it returns.
        """
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9209)
            live = ro.RUNTIME_OWNERSHIP
            acq = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            other = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))
            provider, handle = shared_provider(rt, "dashboard:chat-1-1", acq.lease)

            async def drive() -> None:
                task = asyncio.create_task(provider.shutdown())
                # Let the shutdown reach its awaits, then cancel it there.
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            asyncio.run(drive())

            assert live.leases_on_runtime(rt) == 1, (
                "the cancelled shutdown left its lease outstanding, so the gate will "
                "refuse every kill of this pid"
            )
            assert provider._runtime_lease is None, "the slot still names a released lease"
            # And the surviving session's own release still ends the process.
            assert asyncio.run(live.release(other.lease)) is rt
            assert handle is not None
        finally:
            ro._reset_for_tests()

    def test_a_sole_owner_session_is_untouched_by_this_arm(self):
        """G1: with sharing off nothing sets the flag, so the old arm still runs.

        The distinguishing observation is the DESTROY: the sole-owner arm keeps a
        persistent session's handle and kills the process, which is the behaviour
        that shipped before a cap existed.
        """
        from kiro_crew.acp.session_provider import AcpSessionProvider

        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9208)
            handle = FakeHandle()
            provider = AcpSessionProvider(
                handle,  # type: ignore[arg-type]
                rt,  # type: ignore[arg-type]
                owns_runtime=True,
                session_key="dashboard:chat-1-1",
            )
            provider.memory_mode = "persistent"

            asyncio.run(provider.shutdown())

            assert handle.destroyed == 0, "the sole-owner arm must not destroy the handle"
            assert len(rt.kills) == 1
        finally:
            ro._reset_for_tests()
