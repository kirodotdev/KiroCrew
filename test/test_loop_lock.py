"""Tests for ``kiro_crew.loop_lock.LoopBoundLock`` and its CI guard (#4800).

The defect class: a module-global ``asyncio.Lock`` binds to the event loop it
is first used on, and acquiring it from a *different* loop raises
``RuntimeError`` on Python 3.10+ — which, swallowed by a blanket ``except``,
turned into three order-dependent CI flake classes (#4177, #4789).
``LoopBoundLock`` is the shared remedy; ``scripts/check_loop_bound_locks.py``
is the gate that keeps bare declarations from reintroducing the class.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import threading

import pytest

from kiro_crew.loop_lock import LoopBoundLock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(REPO_ROOT, "scripts", "check_loop_bound_locks.py")


def _contended_use(lock) -> None:
    """Acquire ``lock`` while a second task waits on it — the shape that binds.

    ``asyncio.Lock.acquire``'s uncontended fast path never touches the loop;
    the loop is bound (and checked) only when a waiter takes the slow path. So
    the cross-loop ``RuntimeError`` fires exactly when a lock that was ever
    CONTENDED is contended again on a different loop — which is what a shared
    module-global lock sees under pytest-asyncio's fresh-loop-per-test.
    """

    async def _grab() -> None:
        async with lock:
            pass

    async def _run() -> None:
        async with lock:
            waiter = asyncio.create_task(_grab())
            await asyncio.sleep(0.01)  # let the waiter enqueue → slow path → bind
        await waiter

    asyncio.run(_run())


# ── the defect, as a control ─────────────────────────────────────────────────


def test_bare_asyncio_lock_raises_across_loops() -> None:
    """Control: the defect this module exists for is real on this Python.

    A bare ``asyncio.Lock`` contended on loop A then contended on loop B
    raises ``RuntimeError``. If this ever stops failing, the wrapper (and the
    CI gate) can be reconsidered.
    """
    lock = asyncio.Lock()
    _contended_use(lock)  # binds the lock to loop A
    with pytest.raises(RuntimeError):
        _contended_use(lock)  # loop B — the #4789 crash


# ── LoopBoundLock behaviour ──────────────────────────────────────────────────


def test_loop_bound_lock_survives_a_loop_change() -> None:
    """The headline contract: sequential loops — contended, the shape that
    kills a bare lock (see the control test above) — acquire without raising."""
    lock = LoopBoundLock()
    _contended_use(lock)
    _contended_use(lock)  # fresh loop — must NOT raise


def test_loop_bound_lock_keeps_one_inner_lock_per_loop() -> None:
    """Each loop gets its own underlying asyncio.Lock."""
    lock = LoopBoundLock()
    inner: list[object] = []

    async def _grab() -> None:
        async with lock:
            inner.append(lock._bound())

    asyncio.run(_grab())
    asyncio.run(_grab())
    assert inner[0] is not inner[1]


def test_loop_bound_lock_does_not_retain_closed_loops() -> None:
    """Regression (GPT review on #5367): weak keys alone do not bound the
    table — a contended asyncio.Lock strongly references its loop, so the
    value pins the weak key. The insert-time sweep must keep the table at
    the number of live loops, not the number of loops ever seen."""
    lock = LoopBoundLock()
    for _ in range(10):
        _contended_use(lock)  # contention pins each loop via the lock's waiters
    # The 10th closed loop's entry may linger until the NEXT insert sweeps;
    # anything more than a single trailing entry is the unbounded growth.
    assert len(lock._locks) <= 1, f"retained {len(lock._locks)} closed-loop entries"


def test_concurrent_loops_emit_one_warning(caplog) -> None:
    """A second LIVE loop using the lock logs a one-shot WARNING (Design
    review on #5367): the bare lock failed CLOSED here with RuntimeError, so
    the per-loop contract must stay loud rather than become a silent race."""
    import logging

    lock = LoopBoundLock()
    barrier = threading.Barrier(2, timeout=10)

    def _loop_worker() -> None:
        async def _work() -> None:
            async with lock:
                barrier.wait()
                await asyncio.sleep(0.05)

        asyncio.run(_work())

    with caplog.at_level(logging.WARNING, logger="kiro_crew.loop_lock"):
        threads = [threading.Thread(target=_loop_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

    warnings = [r for r in caplog.records if "second live event loop" in r.getMessage()]
    assert len(warnings) == 1, f"expected one one-shot warning, got {len(warnings)}"


def test_sequential_loops_do_not_warn(caplog) -> None:
    """The common case — pytest-asyncio's fresh loop per test, prior loop
    CLOSED — must stay silent: the sweep removes the dead entry before the
    live-sibling check."""
    import logging

    lock = LoopBoundLock()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.loop_lock"):
        _contended_use(lock)
        _contended_use(lock)
    assert not [r for r in caplog.records if "second live event loop" in r.getMessage()]


def test_loop_bound_lock_concurrent_loops_do_not_corrupt_each_other() -> None:
    """Two loops in two threads share one LoopBoundLock without cross-talk.

    This is the failure mode a single rebound-pointer design has: loop B's
    first acquire replaces the pointer, and loop A's release then unlocks B's
    critical section (silent mutual-exclusion loss) while raising in B. With
    per-loop inner locks, each loop acquires, holds, and releases its OWN lock
    — no RuntimeError, and each loop's release only ever touches its own
    holder. NOTE the documented contract: exclusion is per-loop; the two loops
    are deliberately NOT mutually excluded against each other.
    """
    lock = LoopBoundLock()
    barrier = threading.Barrier(2, timeout=10)
    errors: list[BaseException] = []

    def _loop_worker() -> None:
        async def _work() -> None:
            async with lock:
                # Hold while the OTHER thread's loop acquires and releases:
                # cross-corruption would surface as RuntimeError on release.
                barrier.wait()
                await asyncio.sleep(0.05)
            assert lock.locked() is False  # own loop's lock cleanly released

        try:
            asyncio.run(_work())
        except BaseException as exc:  # noqa: BLE001 — collected for the assert
            errors.append(exc)

    threads = [threading.Thread(target=_loop_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert errors == [], f"cross-loop corruption: {errors!r}"


@pytest.mark.asyncio
async def test_loop_bound_lock_mutual_exclusion() -> None:
    """Within one loop it is still a real lock: two tasks serialize."""
    lock = LoopBoundLock()
    order: list[str] = []

    async def _worker(name: str) -> None:
        async with lock:
            order.append(f"{name}:in")
            await asyncio.sleep(0.01)
            order.append(f"{name}:out")

    await asyncio.gather(_worker("a"), _worker("b"))
    # Whichever ran first must have exited before the other entered.
    assert order[1].endswith(":out"), f"critical sections interleaved: {order}"


@pytest.mark.asyncio
async def test_loop_bound_lock_acquire_release_locked_surface() -> None:
    """The explicit acquire()/release()/locked() surface tests rely on works."""
    lock = LoopBoundLock()
    assert lock.locked() is False
    assert await lock.acquire() is True
    assert lock.locked() is True
    lock.release()
    assert lock.locked() is False


def test_loop_bound_lock_locked_outside_a_loop_reports_any_holder() -> None:
    """Sync status probes work outside any loop, and see a held lock."""
    lock = LoopBoundLock()
    assert lock.locked() is False  # never bound

    async def _hold_and_probe() -> bool:
        await lock.acquire()
        try:
            return lock.locked()
        finally:
            lock.release()

    assert asyncio.run(_hold_and_probe()) is True
    assert lock.locked() is False  # released again


@pytest.mark.asyncio
async def test_loop_bound_lock_release_before_acquire_raises() -> None:
    lock = LoopBoundLock()
    with pytest.raises(RuntimeError):
        lock.release()


def test_converted_module_globals_are_loop_bound() -> None:
    """Spot-check converted declaration sites: the globals are LoopBoundLock."""
    from kiro_crew import tips
    from kiro_crew.mcp_gateway import evaluate

    assert isinstance(tips._tips_init_lock, LoopBoundLock)
    assert isinstance(evaluate._PASS_LOCK, LoopBoundLock)


def test_converted_registries_hand_out_loop_bound_locks() -> None:
    """The registry form (#4800 review finding): dict-cached locks created
    inside coroutines are handed across loops on a repeated key, so the five
    known registries must store LoopBoundLock values."""
    from kiro_crew.dashboard.handlers import worktree

    async def _get():
        return worktree._repo_lock("probe-root")

    lock = asyncio.run(_get())
    try:
        assert isinstance(lock, LoopBoundLock)
    finally:
        worktree._REPO_LOCKS.pop("probe-root", None)


# ── the CI guard ─────────────────────────────────────────────────────────────


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_loop_bound_locks", GUARD)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_guard_self_test_passes(tmp_path) -> None:
    # TMPDIR/TEMP/TMP point the child's tempfile module at this test's own
    # directory, so the self-test's probe files cannot land in (or leak into)
    # the shared session temp root.
    env = dict(os.environ, TMPDIR=str(tmp_path), TEMP=str(tmp_path), TMP=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, GUARD, "--test"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(tmp_path),
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_guard_tree_is_clean(tmp_path) -> None:
    """The real run over src/kiro_crew finds nothing — the declaration-form
    conversion is total."""
    proc = subprocess.run(
        [sys.executable, GUARD],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_guard_reddens_on_a_planted_bare_declaration(tmp_path) -> None:
    """Mutation check: plant the exact defect and assert the guard flags it."""
    guard = _load_guard()
    planted = tmp_path / "planted.py"
    planted.write_text("import asyncio\n_NEW_LOCK = asyncio.Lock()\n", encoding="utf-8")
    violations = guard.scan_file(str(planted))
    assert len(violations) == 1
    assert "asyncio.Lock()" in violations[0][1]


def test_guard_accepts_the_remedy(tmp_path) -> None:
    guard = _load_guard()
    ok = tmp_path / "ok.py"
    ok.write_text(
        "from kiro_crew.loop_lock import LoopBoundLock\n_L = LoopBoundLock()\n",
        encoding="utf-8",
    )
    assert guard.scan_file(str(ok)) == []
