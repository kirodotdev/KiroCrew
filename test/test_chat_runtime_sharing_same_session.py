"""Chat runtime sharing: one chat's two concurrent starts never share a process.

Split out of ``test_chat_runtime_sharing.py`` for the file line cap; the shared
fakes (``FakeRuntime``, ``spawner``, ``a_key``) stay there and are imported from it.
"""

from __future__ import annotations

import asyncio

from test_chat_runtime_sharing import FakeRuntime, a_key, spawner

from kiro_crew.runtime_ownership import RuntimeOwnership


class TestTwoStartsOfOneSessionDoNotShareAProcess:
    """One chat's two concurrent starts resume ONE persisted session id.

    The allocator carries a race budget for this shape, and a person's
    FOREGROUND claim deliberately does not join a BACKGROUND start -- it
    cold-starts at its own priority -- so both run at once and both resume the
    same id. On ONE process that id is a single slot: the two loads share
    ``_load_replay_frames[sid]`` and ``_session_queues[sid]``, and the load that
    reads the counter second finds no replayed turn, so it calls
    ``evict_session(sid)`` and takes the winning start's live native session and
    its response routing with it. Two processes is what the cap of 1 gives and
    what ``_Entry.admits`` keeps at every higher cap.
    """

    def test_the_second_start_of_one_session_founds_its_own_process(self):
        """Drop the ``leases.values()`` clause from ``_Entry.admits`` and this
        reports ``joined is True`` on the first start's runtime -- the eviction
        the class docstring describes."""
        reg = RuntimeOwnership()
        first_rt, second_rt = FakeRuntime(pid=9401), FakeRuntime(pid=9402)
        spawn = spawner(first_rt, second_rt)

        background = asyncio.run(reg.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=25))
        foreground = asyncio.run(reg.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=25))

        assert background.runtime is first_rt and background.joined is False
        assert foreground.joined is False, "a second start of one session must not join"
        assert foreground.runtime is second_rt
        assert len(spawn.calls) == 2, "one process each, as when sharing is off"

    def test_a_different_session_still_joins(self):
        """The exclusion is per session key, not a cap of one in disguise."""
        reg = RuntimeOwnership()
        rt = FakeRuntime(pid=9403)
        spawn = spawner(rt)

        asyncio.run(reg.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=25))
        other = asyncio.run(reg.acquire(a_key(), "dashboard:chat-2-1", spawn, cap=25))

        assert other.joined is True and other.runtime is rt
        assert len(spawn.calls) == 1

    def test_the_exclusion_lifts_once_the_session_holds_no_lease_there(self):
        """It excludes a LEASE the session holds, not the session itself.

        A session key that kept its exclusion past its own release would be
        barred from the process its history is on for the rest of the gateway's
        life. Here the chat shares a process with a second chat, races itself,
        then recycles: the recycled chat must land back on that process, which
        its co-tenant has kept alive.
        """
        reg = RuntimeOwnership()
        shared_rt, loser_rt = FakeRuntime(pid=9404), FakeRuntime(pid=9405)
        spawn = spawner(shared_rt, loser_rt)

        mine = asyncio.run(reg.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=25))
        asyncio.run(reg.acquire(a_key(), "dashboard:chat-2-1", spawn, cap=25))
        loser = asyncio.run(reg.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=25))
        assert loser.runtime is loser_rt

        assert asyncio.run(reg.release(loser.lease)) is loser_rt, "the race loser's own process"
        assert asyncio.run(reg.release(mine.lease)) is None, "the co-tenant keeps it alive"
        again = asyncio.run(reg.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=25))

        assert again.joined is True and again.runtime is shared_rt
        assert len(spawn.calls) == 2, "the rejoin must not found a third process"

    def test_the_second_start_does_not_wait_out_the_first_ones_launch(self):
        """The founding-waiter arm: a launch it may not join is not worth waiting.

        A waiter parked on a founding it must then leave pays a whole subprocess
        launch for nothing -- and when the waiter is the person's foreground
        claim and the founder the background start, that is exactly the wait the
        allocator's priorities exist to avoid. So it founds concurrently.

        The first launch cannot finish until the second start has founded, which
        is the ordering under test: remove the ``f.sessions`` clause and the
        second start parks on the founding instead and the ceiling fires.
        """

        async def scenario():
            reg = RuntimeOwnership()
            first_rt, second_rt = FakeRuntime(pid=9406), FakeRuntime(pid=9407)
            launching = asyncio.Event()
            second_founded = asyncio.Event()

            async def slow_spawn() -> FakeRuntime:
                launching.set()
                await second_founded.wait()
                return first_rt

            async def quick_spawn() -> FakeRuntime:
                second_founded.set()
                return second_rt

            first = asyncio.ensure_future(
                reg.acquire(a_key(), "dashboard:chat-1-1", slow_spawn, cap=25)
            )
            await launching.wait()
            # A ceiling only so a regression names itself instead of hanging:
            # on the fixed code nothing here waits on a clock.
            second = await asyncio.wait_for(
                reg.acquire(a_key(), "dashboard:chat-1-1", quick_spawn, cap=25), timeout=30
            )
            return second.runtime, second.joined, (await first).runtime, first_rt, second_rt

        placed, joined, founder_rt, first_rt, second_rt = asyncio.run(scenario())
        assert placed is second_rt, "the second start waited out a launch it may not join"
        assert joined is False
        assert founder_rt is first_rt
