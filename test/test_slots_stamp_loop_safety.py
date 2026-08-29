"""The event loop must never WAIT on the cross-thread slots stamp.

The two emitting paths run on different threads, so a foreign broadcast holding the
coalescing lock would stall the event loop serving every HTTP snapshot, WS frame and heartbeat
behind it. The stamp has its own lock and is serialized BY the serving loop, so a
loop-side stamp is never queued behind a foreign holder.
"""

import asyncio
import threading
import time

from kiro_crew.dashboard.state import DashboardState


def _state() -> DashboardState:
    state = DashboardState.__new__(DashboardState)
    state._slots = {}
    state._slots_generation = 0
    state._slots_broadcast_lock = threading.RLock()
    state._slots_stamp_lock = threading.RLock()
    state._serving_loop = None
    return state


def test_loop_stamp_does_not_wait_for_a_foreign_lock_holder() -> None:
    state = _state()
    held = threading.Event()
    release = threading.Event()

    def hold_the_lock() -> None:
        with state._slots_broadcast_lock:
            held.set()
            release.wait(timeout=10)

    async def drive() -> float:
        state.bind_serving_loop(asyncio.get_running_loop())
        holder = threading.Thread(target=hold_the_lock, daemon=True)
        holder.start()
        assert held.wait(timeout=5), "foreign thread never took the coalescing lock"
        # The event loop stamps while the foreign thread still holds the coalescing lock: it
        # owns its own stamp lock, so it proceeds at once instead of queueing behind.
        start = time.monotonic()
        await asyncio.to_thread(lambda: None)
        generation, rows = state.stamped_slot_rows()
        elapsed = time.monotonic() - start
        assert generation == 1, generation
        assert rows == ()
        release.set()
        holder.join(timeout=5)
        return elapsed

    elapsed = asyncio.run(drive())
    # A blocked loop would sit here for the holder's full 10s wait.
    assert elapsed < 2.0, "loop waited %.2fs on the foreign lock holder" % elapsed


def test_an_off_loop_stamper_hands_the_stamp_to_the_loop_thread() -> None:
    """The handoff is what stops a FOREIGN stamper holding the stamp lock.

    Splitting the lock keeps a coalescing caller off the event loop's back; it does not stop a
    second STAMPER from being the holder. Asserted on the executing thread rather than on
    timing, so it fails for the intended reason instead of flaking.
    """
    state = _state()
    seen: "list[tuple[int, tuple]]" = []
    ran_on: "list[int]" = []
    real_stamp = state._stamp_slot_rows_now

    def recording_stamp() -> "tuple[int, tuple]":
        ran_on.append(threading.get_ident())
        return real_stamp()

    state._stamp_slot_rows_now = recording_stamp  # type: ignore[method-assign]

    async def drive() -> int:
        state.bind_serving_loop(asyncio.get_running_loop())

        def stamp_from_a_foreign_thread() -> None:
            seen.append(state.stamped_slot_rows())

        worker = threading.Thread(target=stamp_from_a_foreign_thread, daemon=True)
        worker.start()
        # Let the event loop run its callbacks so the handed-over stamp can execute.
        for _ in range(200):
            if seen:
                break
            await asyncio.sleep(0.01)
        worker.join(timeout=5)
        return threading.get_ident()

    loop_thread = asyncio.run(drive())
    assert len(seen) == 1, seen
    generation, rows = seen[0]
    assert generation == 1, generation
    assert rows == ()
    assert ran_on == [loop_thread], (
        "the foreign caller stamped on its OWN thread, so it can hold the stamp lock "
        "while the event loop waits for it"
    )


def test_a_stamp_with_no_loop_at_all_still_works() -> None:
    """~30 sync call sites and test doubles have no loop to hand work to."""
    state = _state()
    assert state.stamped_slot_rows() == (1, ())
    assert state.stamped_slot_rows() == (2, ())
