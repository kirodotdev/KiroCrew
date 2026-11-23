"""A worker thread folds or prewarms against the home its dispatcher resolved.

Each test points ``KIROCREW_HOME`` at a second directory between the moment the work is
queued and the moment the worker runs it: the window a test body's
``patch.dict(os.environ, ...)`` exit opens. The worker must still use the first home and
never resolve the second. The second is a ``tmp_path`` directory, so a regression creates
a scratch directory, never the operator's real ``~/.kiro``.
"""

from __future__ import annotations

import asyncio
import queue
from pathlib import Path

import pytest

from kiro_crew.acp import node_compile_cache as ncc
from kiro_crew.config import paths
from kiro_crew.crew_log import bus, eager, slot_prime
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


def test_the_prewarm_worker_keeps_the_home_its_caller_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handed: list[tuple] = []

    class _RecordingThread:
        def __init__(self, *, target, args, name, daemon):
            handed.append((target, args))

        def start(self):
            pass

    def _no_backend(*_args, **_kwargs):
        raise RuntimeError("no sandbox backend")

    monkeypatch.setattr(ncc.threading, "Thread", _RecordingThread, raising=True)
    monkeypatch.setattr(ncc, "_prewarm_started", set(), raising=True)
    monkeypatch.setattr("kiro_crew.sandbox_spawn.sandboxed_spawn_argv", _no_backend)
    home = paths.data_home()
    ncc.ensure_engine_bundle_cache([str(tmp_path / "node"), str(tmp_path / "acp-server.js")])
    [(target, args)] = handed

    moved = tmp_path / "moved"
    monkeypatch.setenv("KIROCREW_HOME", str(moved))
    target(*args)  # the fake wrap fails closed after the scratch parent exists

    assert (home / "run" / "node-prewarm").is_dir()
    assert not moved.exists(), "the prewarm read KIROCREW_HOME on its own thread"


def test_the_eager_fold_worker_folds_under_the_home_of_each_wake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eager.stop_for_tests()
    seen: list[Path] = []
    real_fold = eager._fold_batch

    def _recording_fold(batch, closers=None):
        seen.append(paths.data_home())
        real_fold(batch, closers)

    monkeypatch.setattr(eager, "_fold_batch", _recording_fold)
    home = paths.data_home()
    moved = tmp_path / "moved"
    try:
        with eager.paused() as held:
            assert held
            eager.note_commit("s-home-pin", "turn/started", 1)
            monkeypatch.setenv("KIROCREW_HOME", str(moved))
        assert eager.drain(timeout=FALLBACK_TIMEOUT_SECS)
    finally:
        eager.stop_for_tests()

    assert seen == [home]
    assert not moved.exists(), "the fold read KIROCREW_HOME on the worker's thread"


def test_a_bus_subscriber_on_a_pinned_thread_reads_the_pin_and_its_loop_work_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pin is per thread: a loop callback a subscriber schedules resolves live."""
    live = tmp_path / "live"
    monkeypatch.setenv("KIROCREW_HOME", str(live))
    loop = asyncio.new_event_loop()
    try:
        resolved: asyncio.Future = loop.create_future()

        def _resolve() -> None:
            resolved.set_result(paths.config_dir())

        inline: list[Path] = []

        def _schedule(_event: object) -> None:
            inline.append(paths.data_home())
            loop.call_soon_threadsafe(_resolve)

        bus.reset_for_tests()  # no subscriber an earlier test left gets this event
        dispose = bus.subscribe(bus.TREE_ADVANCED, _schedule)
        pinned = tmp_path / "pinned"
        try:
            paths.run_with_data_home(pinned, bus.publish, bus.TREE_ADVANCED, object())
        finally:
            dispose()
            bus.reset_for_tests()
        bounded = asyncio.wait_for(resolved, FALLBACK_TIMEOUT_SECS)
        assert loop.run_until_complete(bounded) == live.resolve()
    finally:
        loop.close()
    assert inline == [pinned], "a subscriber on the fold thread reads the wake's home"


def test_wakes_from_two_homes_never_merge_and_each_folds_under_its_own_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eager.stop_for_tests()
    first = paths.data_home()
    second = tmp_path / "second"
    pending: "queue.Queue[eager._Wake]" = queue.Queue()
    for wake in (
        eager._Wake("u", "turn/started", 2, "", second),
        eager._Wake("u", "session/closed", 3, "", first),
        eager._Wake("u", "session/closed", 4, "", second),
    ):
        pending.put_nowait(wake)
    monkeypatch.setattr(eager, "_queue", pending)
    batch, closers, taken = eager._coalesce(eager._Wake("u", "turn/started", 1, "", first))
    assert taken == 4

    folds: list[tuple] = []
    monkeypatch.setattr(
        eager,
        "_fold_batch",
        lambda share, shut: folds.append(
            (paths.data_home(), {key: wake.seq for key, wake in share.items()}, shut)
        ),
    )
    eager._fold_coalesced(batch, closers)

    assert folds == [
        (first, {("u", "", "turn/started"): 1, ("u", "", "session/closed"): 3}, {"u": 3}),
        (second, {("u", "", "turn/started"): 2, ("u", "", "session/closed"): 4}, {"u": 4}),
    ]
    assert not second.exists(), "a pin never creates its home"


def test_the_cold_cell_prime_folds_the_home_its_key_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    submitted: list[tuple] = []

    class _Pool:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    seen: list[tuple] = []
    monkeypatch.setattr(slot_prime, "_pool", lambda: _Pool())
    monkeypatch.setattr(
        slot_prime, "_run", lambda key, slot, name: seen.append((key[0], paths.data_home()))
    )
    home = paths.data_home()
    key = (str(home), "s-prime-pin", "work")
    monkeypatch.setattr(
        "kiro_crew.crew_log.projection_slot_memo.slot_fold_over_ceiling", lambda slot, name: False
    )
    moved = tmp_path / "moved"
    try:
        assert slot_prime.schedule("s-prime-pin", "work")
        [(run, args)] = submitted
        assert args[0] == home and args[2] == key
        monkeypatch.setenv("KIROCREW_HOME", str(moved))
        run(*args)
    finally:
        with slot_prime._lock:  # the stub never settles the prime it was handed
            slot_prime._state.pop(key, None)
        slot_prime.reset_for_tests()
    assert seen == [(str(home), home)]
    assert not moved.exists(), "the prime read KIROCREW_HOME on its pool thread"
