"""Open-slots registry tests: the registry survives crashes, cancellation and damage.

Durable writes and the previous generation, a cancelled or abandoned restore, the
count of tabs a restore could not show, unreadable and undecodable generations, two
concurrent writers, and cancellation before the per-tab loop.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import sys
import threading
from contextlib import contextmanager, suppress
from pathlib import Path
from unittest.mock import patch

import pytest
from chat_test_helpers import _make_state
from open_slots_persistence_helpers import _seed_session

from kiro_crew.dashboard.chat_persistence import (
    ENV_AUTHORITY_RESTORED,
    restore_open_slots,
    restore_open_slots_async,
)
from kiro_crew.testing.wait import async_wait_until

# --- durability across an unclean reboot ------------------------------------ #


def test_persist_fsyncs_the_file_and_its_directory(tmp_path, monkeypatch):
    """The snapshot write asks for both fsyncs, which is what survives a power loss.

    ``atomic_write`` alone is atomic against a concurrent READER -- the rename
    publishes all of the file or none of it -- and that is a different property from
    surviving an unclean reboot. Without the file fsync the data blocks need not have
    reached the disk when the rename does; without the directory fsync the rename
    itself need not have. A filesystem that commits metadata ahead of data then
    brings the file back present and ZERO-LENGTH, which is the reported failure.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = True
    state.get_or_create_slot("chat-1-foo")

    synced: list[str] = []
    coordinator = state._persistence_coordinator
    real_fsync_dir = coordinator._fsync_dir
    monkeypatch.setattr(
        coordinator,
        "_fsync_dir",
        lambda directory: (synced.append(str(directory)), real_fsync_dir(directory))[1],
    )
    seen_kwargs: list[dict] = []
    real_writer = coordinator._atomic_write_provider()

    def _recording_writer(path, content, **kwargs):
        seen_kwargs.append(dict(kwargs))
        return real_writer(path, content, **kwargs)

    monkeypatch.setattr(coordinator, "_atomic_write_provider", lambda: _recording_writer)

    state._persist_open_slots()

    assert synced == [str(tmp_path)]
    # The snapshot's own write, not the rotation's (there was no prior generation).
    assert seen_kwargs[-1]["fsync"] is True
    assert seen_kwargs[-1]["mode"] == 0o600


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="no directory descriptor to refuse: fsync_dir returns before it reaches os.fsync",
)
def test_a_device_that_refuses_the_directory_sync_keeps_the_write_retryable(tmp_path, monkeypatch):
    """An ``EIO`` from the directory fsync must not publish the write as durable.

    POSIX only, and that is the platform behaviour rather than a gap: Windows has no
    directory descriptor to open, so ``fsync_dir`` returns before it reaches
    ``os.fsync`` and there is no refusal to inject. Stubbing the open, the sync and
    the close to force this path there would pin a sequence that platform cannot
    produce. What Windows does instead -- skip the directory sync quietly and still
    publish the write -- is what the other persist tests in this file assert when
    they run there.

    The skip that keeps the fsync pair off the 5s cadence reads a cache of the set
    last written. Published after a directory sync that FAILED, it suppresses the
    retry of the very write whose rename may not have reached the disk -- so the
    power loss the sync exists for finds the newly opened tab missing. The cache has
    to be forgotten instead, which is what the failure reaching the caller's
    ``except`` does. The helper is quiet where a directory sync cannot be expressed
    at all (Windows, a filesystem that rejects it), so nothing is traded for this.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = True
    state.get_or_create_slot("chat-1-foo")
    coordinator = state._persistence_coordinator

    # At ``os.fsync``, not at ``_fsync_dir``: the decision under test lives INSIDE
    # the helper (whether an EIO is raised or downgraded to a warning), and a fault
    # armed at the wrapper would pass with the downgrade still in place. Directory
    # descriptors only -- the file fsync inside the atomic write must still work.
    real_fsync = os.fsync

    def _refuse_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "simulated device failure syncing the directory")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _refuse_directory_fsync)
    state._persist_open_slots()
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert coordinator._open_slots_written is None, (
        "a failed directory sync published the write as durable, so the next flush "
        "will skip the retry"
    )

    # And the retry really happens: the same set, with the sync working, writes.
    writes: list[object] = []
    real_writer = coordinator._atomic_write_provider()

    def _counting_writer(path, content, **kwargs):
        writes.append(path)
        return real_writer(path, content, **kwargs)

    monkeypatch.setattr(coordinator, "_atomic_write_provider", lambda: _counting_writer)
    state._persist_open_slots()
    assert writes, "the flush after a failed directory sync skipped the write"


def test_persist_skips_the_write_when_the_key_set_is_unchanged(tmp_path, monkeypatch):
    """A 5s flush that would rewrite the same set does no durable work at all.

    The fsync pair is what makes the write expensive, and almost every periodic
    flush writes a file byte-identical to the one already there -- the set only
    changes when a tab is opened, closed or restored. Without this the fix would
    trade a silent data loss for an fsync every five seconds for the life of the
    process.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = True
    state.get_or_create_slot("chat-1-foo")

    writes: list[object] = []
    coordinator = state._persistence_coordinator
    real_writer = coordinator._atomic_write_provider()

    def _counting_writer(path, content, **kwargs):
        writes.append(path)
        return real_writer(path, content, **kwargs)

    monkeypatch.setattr(coordinator, "_atomic_write_provider", lambda: _counting_writer)

    state._persist_open_slots()
    first = len(writes)
    assert first >= 1
    state._persist_open_slots()
    state._persist_open_slots()
    assert len(writes) == first, "an unchanged key set must not be rewritten"

    # A real change still lands.
    state.get_or_create_slot("chat-2-bar")
    state._persist_open_slots()
    assert len(writes) > first
    payload = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert set(payload["keys"]) == {"chat-1-foo", "chat-2-bar"}


def test_persist_keeps_the_previous_generation_beside_the_snapshot(tmp_path, monkeypatch):
    """The set being replaced is kept as ``open_slots.json.prev``."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = True
    state.get_or_create_slot("chat-1-foo")
    state._persist_open_slots()
    assert not (tmp_path / "open_slots.json.prev").exists(), "nothing to keep on the first write"

    state.get_or_create_slot("chat-2-bar")
    state._persist_open_slots()

    previous = json.loads((tmp_path / "open_slots.json.prev").read_text(encoding="utf-8"))
    assert set(previous["keys"]) == {"chat-1-foo"}
    current = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert set(current["keys"]) == {"chat-1-foo", "chat-2-bar"}


def test_persist_does_not_promote_a_damaged_generation_into_the_aside(tmp_path, monkeypatch):
    """A zero-length current file must not overwrite the last good aside.

    The aside IS the answer the restore falls back to, so copying the damage over it
    would destroy the open-tab set in exactly the situation the aside exists for.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = True
    for key in ("chat-1-foo", "chat-2-bar"):
        state.get_or_create_slot(key)
    state._persist_open_slots()
    state.get_or_create_slot("chat-3-baz")
    state._persist_open_slots()
    assert set(
        json.loads((tmp_path / "open_slots.json.prev").read_text(encoding="utf-8"))["keys"]
    ) == {"chat-1-foo", "chat-2-bar"}

    # The reported crash shape: present, zero-length.
    (tmp_path / "open_slots.json").write_text("", encoding="utf-8")
    state.get_or_create_slot("chat-4-qux")
    state._persist_open_slots()

    assert set(
        json.loads((tmp_path / "open_slots.json.prev").read_text(encoding="utf-8"))["keys"]
    ) == {"chat-1-foo", "chat-2-bar"}


@pytest.mark.parametrize("damaged", ["", "   ", '{"keys": ', '{"ts": 1.0}', "[]"])
def test_restore_falls_back_to_the_previous_generation(tmp_path, monkeypatch, damaged):
    """A current file holding no answer restores from the aside instead of nothing.

    The whole loss mechanism in one test. A truncated file reads as 0 keys, the
    restore flips ``open_slots_restored`` on that reading, and the next 5s flush
    prunes the seed -- so the tabs go with no close, no delete and no trace.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-kept")
    (tmp_path / "open_slots.json").write_text(damaged, encoding="utf-8")
    (tmp_path / "open_slots.json.prev").write_text(
        json.dumps({"keys": ["chat-1-kept"], "ts": 0.0}), encoding="utf-8"
    )

    state2 = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state2) == 1
    assert "chat-1-kept" in state2._slots


def test_restore_does_not_resurrect_a_deliberately_emptied_set(tmp_path, monkeypatch):
    """``{"keys": []}`` is an ANSWER, so the aside is not consulted.

    Closing the last tab writes exactly that. Falling back on it would reopen every
    tab the user dismissed, on every restart, forever -- so the fallback must key on
    the file holding NO answer (missing, zero-length, truncated, wrong shape) rather
    than on the key list being short.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-closed-by-hand")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": [], "ts": 1.0}), encoding="utf-8")
    (tmp_path / "open_slots.json.prev").write_text(
        json.dumps({"keys": ["chat-1-closed-by-hand"], "ts": 0.0}), encoding="utf-8"
    )

    state2 = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state2) == 0
    assert state2._slots == {}


def test_pre_restore_merge_reads_the_aside_when_the_snapshot_is_damaged(tmp_path, monkeypatch):
    """A boot-window flush merges the aside's keys, not an empty read.

    The merge exists so a flush firing before the restore cannot shrink the seed. A
    crash that truncated the live file is precisely when the seed it must preserve is
    in the other file, so reading only the damaged one would re-open the same hole
    one layer down.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = False  # the boot restore has NOT run yet
    (tmp_path / "open_slots.json").write_text("", encoding="utf-8")
    (tmp_path / "open_slots.json.prev").write_text(
        json.dumps({"keys": ["chat-7-seeded", "chat-8-seeded"], "ts": 0.0}), encoding="utf-8"
    )
    state.get_or_create_slot("chat-9-booted")

    state._persist_open_slots()

    payload = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert set(payload["keys"]) == {"chat-7-seeded", "chat-8-seeded", "chat-9-booted"}


# --- a cancelled restore must not claim it finished -------------------------- #


def test_cancelled_restore_keeps_the_keys_it_never_read(tmp_path, monkeypatch):
    """Cancel after 1 of 3 tabs and the snapshot still lists all three.

    Both drivers flip ``open_slots_restored`` in a ``finally``, which tells the
    persist writers that ``_slots`` is now the authoritative open-tab set. That is
    true only for a restore that RAN TO THE END: cancelled at tab 1 of 3, the latch
    flips anyway and the next flush prunes the file to the one tab that made it.
    Reproduced here as the file shrinking, which is the user-visible loss.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-a", "chat-2-b", "chat-3-c"]
    for key in keys:
        _seed_session(state, key)
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": keys, "ts": 0.0}), encoding="utf-8"
    )

    state2 = _make_state(tmp_path / "sessions")
    from kiro_crew.dashboard import chat_persistence

    # The cut-short is a GATE, not a race. The driver reads each tab in a worker
    # thread (``asyncio.to_thread(_prefetch_open_slot_inputs, ...)``) and awaits
    # that read, so the second tab's read is held until the test releases it: the
    # task is parked at that await when the cancel lands, on every runner speed.
    # Polling for "one slot applied" and then cancelling is timing-based: a fast
    # loop applies tabs 2 and 3 and RETURNS between the poll and the cancel, which
    # makes the cancel a no-op and ``await task`` a normal return.
    # ``threading.Event`` because the gate is waited on from the worker thread,
    # not from the loop.
    real_prefetch = chat_persistence._prefetch_open_slot_inputs
    second_read_entered = threading.Event()
    release_second_read = threading.Event()
    reads = 0

    def _gated_prefetch(*args, **kwargs):
        # Sequential by construction: the driver awaits each read before starting
        # the next, so a plain counter is enough and no two reads overlap.
        nonlocal reads
        reads += 1
        if reads == 2:
            second_read_entered.set()
            # BOUNDED, so a cancel that never comes cannot pin the worker thread --
            # asyncio.run's executor shutdown waits for it, and an unbounded wait
            # turns a failed test into a hung one.
            release_second_read.wait(timeout=10)
        return real_prefetch(*args, **kwargs)

    monkeypatch.setattr(chat_persistence, "_prefetch_open_slot_inputs", _gated_prefetch)

    async def _cancel_at_the_second_tab() -> None:
        task = asyncio.ensure_future(restore_open_slots_async(state2))

        try:
            # BOUNDED. Unbounded, a restore that finishes without reaching a second
            # read -- which a failing first read would produce -- spins this worker
            # forever instead of reporting the failure, and the suite reports a
            # timeout somewhere else entirely. async_wait_until waits on the signal
            # (the second read entered) rather than a raw fixed sleep, per testing
            # rule D1.
            await async_wait_until(
                second_read_entered.is_set,
                timeout=10,
                describe=lambda: "the restore never reached its second tab",
            )
        finally:
            # Cancel FIRST: ``Task.cancel`` cancels the awaited to_thread future
            # synchronously, so by the time the gate opens the driver is already
            # unwinding through its carry guard with tab 2 in hand. Then release the
            # thread, so it cannot outlive the loop. Then AWAIT whatever state the
            # wait left the restore in: on the timeout path it is still running, and
            # an un-awaited cancellation surfaces as "Task was destroyed but it is
            # pending" against the next test.
            task.cancel()
            release_second_read.set()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(_cancel_at_the_second_tab())

    # The latch stays DOWN, because this pass never reached the end of the seed.
    # Both defences are wanted and neither subsumes the other: the latch covers the
    # suspension points where there is nothing yet to carry (the snapshot read), and
    # the carry covers the keys this pass can name. Holding the latch down parks the
    # persist writers in merge mode for the rest of the process -- which is right
    # here rather than a stuck flag, because a process cancelled mid-restore never
    # learned the whole set. A surface that legitimately has nothing to restore
    # releases it through the drivers' own "nothing to enumerate" exits.
    assert state2.open_slots_restored is False
    assert len(state2._slots) == 1, "the premise: the restore was cut short at tab 2 of 3"
    # The keys it never read are carried, so the flush below cannot prune them.
    assert set(keys) - set(state2._slots) <= set(state2.unrestored_slot_keys)

    state2._persist_open_slots()
    payload = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert set(payload["keys"]) == set(keys)


def test_abandoned_sync_restore_keeps_the_keys_it_never_read(tmp_path, monkeypatch):
    """The generator driver owes the same carry: it is ended by a throw, not a return."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-a", "chat-2-b", "chat-3-c"]
    for key in keys:
        _seed_session(state, key)
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": keys, "ts": 0.0}), encoding="utf-8"
    )

    state2 = _make_state(tmp_path / "sessions")
    from kiro_crew.dashboard.chat_persistence import _restore_open_slots_steps

    steps = _restore_open_slots_steps(state2)
    assert next(steps) == 1
    steps.close()  # GeneratorExit at the yield -- an abandoned restore
    state2.open_slots_restored = True  # what restore_open_slots' own finally does

    assert set(keys) - set(state2._slots) <= set(state2.unrestored_slot_keys)
    state2._persist_open_slots()
    payload = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert set(payload["keys"]) == set(keys)


# --- the drop leaves a trace ------------------------------------------------ #


def test_restore_publishes_the_count_of_tabs_it_could_not_show(tmp_path, monkeypatch):
    """``unrestored_slot_notice`` is what the dashboard's notice reads.

    One frozen reading taken when the restore finished, rather than the live
    ``unrestored_slot_keys`` the persist writers fold in on every flush: a notice
    built on the live set would report a different number each time it was asked.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-readable")
    _seed_session(state, "chat-2-unreadable")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-readable", "chat-2-unreadable"], "ts": 0.0}),
        encoding="utf-8",
    )

    state2 = _make_state(tmp_path / "sessions")
    real_status = state2.conversation_log.get_metadata_status

    def _one_unreadable(history_key, *args, **kwargs):
        if history_key.endswith("chat-2-unreadable"):
            return {}, False  # the read FAILED -- not "the session is gone"
        return real_status(history_key, *args, **kwargs)

    monkeypatch.setattr(state2.conversation_log, "get_metadata_status", _one_unreadable)

    assert restore_open_slots(state2) == 1
    assert state2.unrestored_slot_notice == {
        "count": 1,
        "keys": ["chat-2-unreadable"],
        "unknowable": False,
    }


def test_a_clean_restore_reports_nothing_dropped(tmp_path, monkeypatch):
    """A reported zero, which is a different answer from no report at all."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-fine")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-fine"], "ts": 0.0}), encoding="utf-8"
    )

    state2 = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state2) == 1
    assert state2.unrestored_slot_notice == {"count": 0, "keys": [], "unknowable": False}


def _read_unrestored_endpoint(state) -> dict:
    """What ``GET /api/chat/slots/unrestored`` answers for *state*, through the handler.

    The notice's own attribute is one step short of the question the browser asks:
    the endpoint is where ``None`` becomes ``reported: false``, and the frontend polls
    every 5s for as long as that is what it reads.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_api.slot_detail import api_chat_slots_unrestored

    response = asyncio.run(api_chat_slots_unrestored(SimpleNamespace(app={"state": state})))
    return json.loads(response.text)


@pytest.mark.parametrize("driver", ["sync", "async"])
@pytest.mark.parametrize("registry", ["empty", "missing"])
def test_a_restore_with_nothing_to_restore_reports_a_zero(tmp_path, monkeypatch, driver, registry):
    """The default path: an empty or absent registry answers, rather than staying mute.

    A fresh install has no ``open_slots.json`` at all, and a user whose last session
    closed every tab has one holding ``{"keys": []}``. Both restore nothing, which is
    a settled answer -- and an unreported notice is not "nothing was dropped" but "no
    one has measured yet", which the frontend re-asks every 5s for the life of the
    page. Both drivers, because each has its own copy of the early return.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    if registry == "empty":
        (tmp_path / "open_slots.json").write_text(
            json.dumps({"keys": [], "ts": 0.0}), encoding="utf-8"
        )

    state = _make_state(tmp_path / "sessions")
    if driver == "sync":
        assert restore_open_slots(state) == 0
    else:
        assert asyncio.run(restore_open_slots_async(state)) == 0

    assert state.unrestored_slot_notice == {"count": 0, "keys": [], "unknowable": False}
    assert _read_unrestored_endpoint(state) == {
        "reported": True,
        "count": 0,
        "unknowable": False,
    }
    # The latch half is untouched by the report: the set was knowable and empty.
    assert state.open_slots_restored is True


@pytest.mark.parametrize("driver", ["sync", "async"])
def test_an_unknowable_registry_reports_itself_as_unknowable(tmp_path, monkeypatch, driver):
    """The other half of the same exit: no count, and not silence either.

    Every generation refused by the OS tells this process nothing about the tabs that
    were open. A zero would claim nothing was lost on the one boot that cannot know,
    and ``reported: false`` reads as "still restoring", which hides the notice and
    leaves the page re-asking forever -- the silence the notice exists to end. So the
    restore reports, and the report says the set could not be learned.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seed = {"keys": ["chat-A-idle"], "ts": 0.0}
    (tmp_path / "open_slots.json").write_text(json.dumps(seed), encoding="utf-8")
    (tmp_path / "open_slots.json.prev").write_text(json.dumps(seed), encoding="utf-8")

    state = _make_state(tmp_path / "sessions")
    with _refuse_read_text("open_slots.json", "open_slots.json.prev"):
        if driver == "sync":
            assert restore_open_slots(state) == 0
        else:
            assert asyncio.run(restore_open_slots_async(state)) == 0

    assert state.open_slots_knowable is False
    assert state.open_slots_restored is False  # the prune latch is a separate answer
    assert _read_unrestored_endpoint(state) == {
        "reported": True,
        "count": 0,
        "unknowable": True,
    }


@pytest.mark.parametrize("driver", ["sync", "async"])
def test_a_restore_that_never_finished_stays_unreported(tmp_path, monkeypatch, driver):
    """The only state that is still silence: nobody has measured yet.

    A pass cancelled or abandoned before it got through the seed has no answer about
    the keys it never read -- not a count, and not "the set is unknowable" either,
    since the registry read succeeded. ``reported: false`` is what the dashboard's
    poll is for, and this is the state it waits on.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    for key in ("chat-1-first", "chat-2-second", "chat-3-third"):
        _seed_session(state, key)
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-first", "chat-2-second", "chat-3-third"], "ts": 0.0}),
        encoding="utf-8",
    )

    state2 = _make_state(tmp_path / "sessions")
    if driver == "sync":
        from kiro_crew.dashboard.chat_persistence import _restore_open_slots_steps

        steps = _restore_open_slots_steps(state2)
        assert next(steps) == 1
        steps.close()  # GeneratorExit at the yield -- an abandoned restore
    else:
        task_holder: dict[str, asyncio.Task] = {}

        async def _cancel_mid_restore():
            task = asyncio.ensure_future(restore_open_slots_async(state2))
            task_holder["task"] = task
            await asyncio.sleep(0)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        asyncio.run(_cancel_mid_restore())
        assert task_holder["task"].cancelled()

    assert state2.unrestored_slot_notice is None
    assert _read_unrestored_endpoint(state2) == {
        "reported": False,
        "count": 0,
        "unknowable": False,
    }


@pytest.mark.parametrize("driver", ["sync", "async"])
def test_a_process_with_no_conversation_log_reports_a_zero(tmp_path, monkeypatch, driver):
    """Persistence off is still an answer: no listed tab went unshown.

    The earliest exit of both drivers, and the one that already flips the latch -- a
    process that reports no drops and licenses pruning in the same breath is
    consistent, while one that licenses pruning and never reports is not.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.conversation_log = None

    if driver == "sync":
        assert restore_open_slots(state) == 0
    else:
        assert asyncio.run(restore_open_slots_async(state)) == 0

    assert _read_unrestored_endpoint(state) == {
        "reported": True,
        "count": 0,
        "unknowable": False,
    }


def test_the_drop_is_recorded_on_the_dropped_sessions_own_log(tmp_path, monkeypatch):
    """Each kept key earns a ``session/unrestored`` entry on its own crew log.

    The record a reader goes looking for. The question is always "what happened to
    this conversation", asked of the conversation -- and before this there was no
    answer anywhere: no ``session/closed`` (the gateway did not stop serving it), no
    delete, and the restore's own warning lives in a gateway log that rotates away.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-2-unreadable")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-2-unreadable"], "ts": 0.0}), encoding="utf-8"
    )

    state2 = _make_state(tmp_path / "sessions")
    monkeypatch.setattr(state2.conversation_log, "get_metadata_status", lambda *a, **k: ({}, False))

    from kiro_crew.crew_log import emit as crew_log_emit

    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(
        crew_log_emit, "slot_previous_store", lambda slot: ("sid-for-" + slot, True, True)
    )
    monkeypatch.setattr(
        crew_log_emit,
        "on_open_tab_unrestored",
        lambda sid, **fields: recorded.append((sid, fields)),
    )

    assert restore_open_slots(state2) == 0
    assert recorded == [("sid-for-chat-2-unreadable", {"listed": 1, "restored": 0, "kept": 1})]


def test_recording_the_drop_cannot_break_the_restore(tmp_path, monkeypatch):
    """A crew log that refuses the account still leaves the tab restored.

    The record is a courtesy on the startup path. A restore that aborted because it
    could not write a note about a tab it already rebuilt would turn the diagnostic
    into a second, larger outage.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-fine")
    _seed_session(state, "chat-2-unreadable")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-fine", "chat-2-unreadable"], "ts": 0.0}), encoding="utf-8"
    )

    state2 = _make_state(tmp_path / "sessions")
    real_status = state2.conversation_log.get_metadata_status
    monkeypatch.setattr(
        state2.conversation_log,
        "get_metadata_status",
        lambda history_key, *a, **k: (
            ({}, False)
            if history_key.endswith("chat-2-unreadable")
            else real_status(history_key, *a, **k)
        ),
    )

    from kiro_crew.crew_log import emit as crew_log_emit

    monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)

    def _boom(slot):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(crew_log_emit, "slot_previous_store", _boom)

    assert restore_open_slots(state2) == 1
    assert "chat-1-fine" in state2._slots
    # The notice is published BEFORE the per-key emit, so it survives the failure.
    assert state2.unrestored_slot_notice == {
        "count": 1,
        "keys": ["chat-2-unreadable"],
        "unknowable": False,
    }


# --- an unreadable generation is not an empty one --------------------------- #


@contextmanager
def _refuse_read_text(*names: str):
    """Make ``Path.read_text`` refuse *names* with a Windows-style sharing violation.

    ``builtin_open_sharing_violation`` cannot express this: ``Path.read_text`` opens
    through ``io.open`` rather than the ``builtins.open`` that shim patches, so a
    fault armed there never fires and the test would pass while reading the real
    file -- the shape of vacuous pass this file's other probes guard against.

    A context manager rather than a monkeypatch for the whole test, because the
    assertions read the same files back: a fault still armed then refuses the test's
    own read and the failure lands on the probe instead of on the behaviour.
    """
    real = Path.read_text
    wanted = frozenset(names)

    def _patched(self, *args, **kwargs):
        if self.name in wanted:
            raise PermissionError(f"[WinError 32] simulated sharing violation opening {self}")
        return real(self, *args, **kwargs)

    with patch.object(Path, "read_text", _patched):
        yield


def test_pre_restore_merge_skips_the_write_when_the_aside_is_unreadable(tmp_path, monkeypatch):
    """A damaged live file plus an unreadable aside must publish nothing.

    The one combination where the merge read learns nothing at all. Answering it with
    an empty seed merges the live slots against nothing and publishes them alone,
    which overwrites the intact aside's set with whatever happened to be open in the
    boot window -- the loss this whole mechanism exists to prevent, arrived at from
    the other side.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = False  # the boot restore has NOT run yet
    (tmp_path / "open_slots.json").write_text("{not valid json", encoding="utf-8")
    aside = {"keys": ["chat-A-idle", "chat-B-idle"], "ts": 0.0}
    (tmp_path / "open_slots.json.prev").write_text(json.dumps(aside), encoding="utf-8")
    state.get_or_create_slot("chat-C-boot")

    with _refuse_read_text("open_slots.json.prev"):
        state._persist_open_slots()

    assert json.loads((tmp_path / "open_slots.json.prev").read_text()) == aside
    assert (tmp_path / "open_slots.json").read_text(
        encoding="utf-8"
    ) == "{not valid json", (
        "the write was not skipped: an unreadable aside was read as an empty seed"
    )


def test_an_absent_aside_is_an_answer_not_a_transient_failure(tmp_path, monkeypatch):
    """A damaged live file with NO aside still writes -- absence is a fact.

    The negative control for the test above. If "unknown" were inferred from an empty
    result rather than from a refused read, this write would be skipped forever and a
    home that never had an aside could never persist its open tabs again.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = False
    (tmp_path / "open_slots.json").write_text("", encoding="utf-8")
    state.get_or_create_slot("chat-C-boot")

    state._persist_open_slots()

    payload = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert payload["keys"] == ["chat-C-boot"]


def test_restore_holds_the_prune_latch_down_when_the_set_is_unknowable(tmp_path, monkeypatch):
    """Every generation refused by the OS leaves the writers in merge mode.

    ``open_slots_restored`` is the writers' licence to treat the live slot map as the
    authoritative open-tab set. A process that never managed to READ the registry has
    no basis for that, so the latch stays down for its whole life -- which is the
    correct posture rather than a stuck flag.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seed = {"keys": ["chat-A-idle", "chat-B-idle"], "ts": 0.0}
    (tmp_path / "open_slots.json").write_text(json.dumps(seed), encoding="utf-8")
    (tmp_path / "open_slots.json.prev").write_text(json.dumps(seed), encoding="utf-8")

    state = _make_state(tmp_path / "sessions")
    # BOTH generations: an aside that still answers makes the set knowable.
    with _refuse_read_text("open_slots.json", "open_slots.json.prev"):
        assert restore_open_slots(state) == 0

    assert state.open_slots_knowable is False
    assert state.open_slots_restored is False

    # And the flush that follows cannot shrink the file.
    state.get_or_create_slot("chat-C-boot")
    state._persist_open_slots()
    payload = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert set(payload["keys"]) == {"chat-A-idle", "chat-B-idle", "chat-C-boot"}


def test_a_readable_empty_registry_still_releases_the_latch(tmp_path, monkeypatch):
    """The negative control: a registry that reads as empty is knowable.

    Without this, "unknowable" could be inferred from an empty read and every home
    with no open tabs would be held in merge mode forever.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")

    assert restore_open_slots(state) == 0
    assert state.open_slots_knowable is True
    assert state.open_slots_restored is True


def test_a_remote_only_seed_is_not_reported_as_a_dropped_tab(tmp_path, monkeypatch):
    """A tab awaiting lazy transcript retrieval is kept, not announced.

    It stays in the reopen seed across every boot by design, so counting it would
    raise "N tabs were not restored" on every restart of a system behaving exactly as
    intended -- and a warning that repeats forever is one the user learns to ignore,
    which costs the real loss its only visible signal.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv(ENV_AUTHORITY_RESTORED, "1")
    _make_state(tmp_path / "sessions")
    # Listed in the restored table with no transcript on disk: the cold-replacement
    # shape that _apply_restored_open_slot keeps as a remote-only reopen seed.
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-9-remote"], "ts": 0.0}), encoding="utf-8"
    )

    state2 = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state2) == 0
    # Kept, so the next flush cannot prune it ...
    assert "chat-9-remote" in set(state2.unrestored_slot_keys)
    # ... and NOT announced.
    assert state2.unrestored_slot_notice == {"count": 0, "keys": [], "unknowable": False}


# --- undecodable bytes are damage, not a failed read ------------------------ #


def test_restore_falls_back_when_the_registry_is_not_valid_utf8(tmp_path, monkeypatch):
    """A torn write can leave bytes that are not UTF-8 at all.

    ``read_text`` reports that as ``UnicodeDecodeError``, which is a ``ValueError``
    and so is NOT caught by an ``except OSError``. Escaping, it aborts dashboard
    startup -- from a file whose whole purpose is to be recoverable, and without ever
    consulting the generation beside it. So it is damage, and damage falls back.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-kept")
    (tmp_path / "open_slots.json").write_bytes(b'{"keys": ["chat-1-kept\xff"]}')
    (tmp_path / "open_slots.json.prev").write_text(
        json.dumps({"keys": ["chat-1-kept"], "ts": 0.0}), encoding="utf-8"
    )

    state2 = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state2) == 1
    assert "chat-1-kept" in state2._slots
    # Damage is an ANSWER, so the latch releases: the set is known, from the aside.
    assert state2.open_slots_knowable is True


def test_pre_restore_merge_survives_a_registry_that_is_not_valid_utf8(tmp_path, monkeypatch):
    """The merge read owes the same handling, and on the same reasoning.

    Its caller wraps everything in a broad ``except``, so an escaping decode error
    costs the snapshot rather than the gateway -- but it would still skip the write
    while the aside beside it held the seed the merge exists to preserve.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = False
    (tmp_path / "open_slots.json").write_bytes(b"\xff\xfe not text at all")
    (tmp_path / "open_slots.json.prev").write_text(
        json.dumps({"keys": ["chat-A-idle"], "ts": 0.0}), encoding="utf-8"
    )
    state.get_or_create_slot("chat-C-boot")

    state._persist_open_slots()

    payload = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert set(payload["keys"]) == {"chat-A-idle", "chat-C-boot"}


def test_a_refused_current_generation_does_not_authorize_pruning(tmp_path, monkeypatch):
    """The aside answering does not make the LIVE set known.

    The aside is a generation behind by construction. A current file holding [A, B]
    whose read is refused, beside an aside holding [A], would otherwise restore [A],
    call it authoritative, and let the next flush delete B from a live file nobody
    ever managed to read. Restoring the stale set is still right; licensing a prune
    off it is not.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    for key in ("chat-A-filed", "chat-B-unfiled"):
        _seed_session(state, key)
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-A-filed", "chat-B-unfiled"], "ts": 1.0}), encoding="utf-8"
    )
    (tmp_path / "open_slots.json.prev").write_text(
        json.dumps({"keys": ["chat-A-filed"], "ts": 0.0}), encoding="utf-8"
    )

    state2 = _make_state(tmp_path / "sessions")
    with _refuse_read_text("open_slots.json"):
        # The stale set still comes back -- stale tabs beat absent ones.
        assert restore_open_slots(state2) == 1
    assert "chat-A-filed" in state2._slots
    assert state2.open_slots_knowable is False
    assert state2.open_slots_restored is False

    # And B survives: the writers are in merge mode, and the merge read of the live
    # file is refused too, so the flush declines to publish anything at all.
    with _refuse_read_text("open_slots.json"):
        state2._persist_open_slots()
    payload = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert set(payload["keys"]) == {"chat-A-filed", "chat-B-unfiled"}


# --- two writers must not leave the cache describing the loser's write ------ #


def test_interleaved_writers_leave_the_cache_matching_the_file(tmp_path, monkeypatch):
    """The periodic flush and the pre-restart save are serialized against each other.

    ``atomic_write`` makes each write atomic against a READER, which says nothing
    about two writers. Unserialized, the damage lands in the CACHE rather than in the
    file: the writer that sampled ``[A, B]`` can publish that as "what is on disk"
    after the writer that sampled ``[A]`` has already landed. Every later flush then
    compares against ``[A, B]``, matches, and skips the write that would have
    repaired the file -- so B is gone at the next boot, from a cache line, with the
    file never corrupt at any instant.

    The interleaving is forced rather than hoped for: the first writer is parked
    inside its own write, and the second is observed BLOCKING on the section's lock
    rather than inferred to be blocked from still being alive after a short join. A
    liveness check is a statement about the scheduler -- on a loaded runner the main
    thread can be held past the park's own deadline, and the first writer would then
    have released the lock before the check runs, reporting broken serialization that
    did not happen. The park's deadline is also asserted, so an expired park fails as
    itself instead of as the thing it was holding still.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = True
    state.get_or_create_slot("chat-A-first")
    state._persist_open_slots()

    coordinator = state._persistence_coordinator
    real_writer = coordinator._atomic_write_provider()
    parked = threading.Event()
    released = threading.Event()
    park_expired: list[str] = []
    contended = threading.Event()

    class _SignallingLock:
        """The section's own lock, announcing the moment a thread BLOCKS on it.

        The positive signal the test needs: a non-blocking attempt that FAILS means
        another thread holds the section, which is serialization observed rather
        than inferred. With the lock gone the attempt succeeds, nothing is
        announced, and the wait below says so by name.
        """

        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            if not self._inner.acquire(blocking=False):
                contended.set()
                self._inner.acquire()
            return self

        def __exit__(self, *_exc):
            self._inner.release()
            return False

    monkeypatch.setattr(
        coordinator, "_open_slots_lock", _SignallingLock(coordinator._open_slots_lock)
    )

    def _parking_writer(path, content, **kwargs):
        # Park only the snapshot write of the FIRST writer, inside the critical
        # section, which is exactly where the lock has to hold. The deadline is
        # generous and recorded: an expired park releases the section early, which
        # would make a serialization check report a scheduler delay as a defect.
        if Path(path).name == "open_slots.json" and not parked.is_set():
            parked.set()
            if not released.wait(timeout=120):
                park_expired.append(str(path))
        return real_writer(path, content, **kwargs)

    monkeypatch.setattr(coordinator, "_atomic_write_provider", lambda: _parking_writer)

    # Writer one samples {A, B} and then parks mid-write.
    state.get_or_create_slot("chat-B-second")
    first = threading.Thread(target=state._persist_open_slots, name="flush-one")
    second = threading.Thread(target=state._persist_open_slots, name="flush-two")
    first.start()
    try:
        assert parked.wait(timeout=30), "the first writer never reached its write"

        # Writer two runs while writer one holds the section. Serialized, it blocks on
        # the lock and says so; a missing lock lets it sample, write and publish its
        # own cache value here, and announces nothing.
        state._slots.pop("chat-B-second")
        second.start()
        assert contended.wait(timeout=30), (
            "the second writer never blocked on the section's lock, so the two "
            "writes were not serialized against each other"
        )
    finally:
        # Always, so a failed assertion above ends the park instead of leaving two
        # threads holding a tmp_path the fixture is about to remove.
        released.set()
        first.join(timeout=30)
        if second.is_alive() or second.ident is not None:
            second.join(timeout=30)

    assert not park_expired, f"the park expired before it was released: {park_expired}"
    assert not first.is_alive() and not second.is_alive()

    on_disk = set(json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))["keys"])
    cached = coordinator._open_slots_written
    assert cached is not None
    assert cached[1] == frozenset(on_disk), (
        "the cached set describes a write other than the one on disk, so a later "
        f"flush would skip the repair (cached={sorted(cached[1])}, disk={sorted(on_disk)})"
    )

    # And the cache is still TRUSTWORTHY: a flush with the live set unchanged skips,
    # while a real change still lands.
    writes: list[object] = []

    def _counting_writer(path, content, **kwargs):
        writes.append(Path(path).name)
        return real_writer(path, content, **kwargs)

    monkeypatch.setattr(coordinator, "_atomic_write_provider", lambda: _counting_writer)
    state._persist_open_slots()
    assert writes == [], "the cache no longer matches the live set"
    state.get_or_create_slot("chat-C-third")
    state._persist_open_slots()
    assert "open_slots.json" in writes
    final = set(json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))["keys"])
    assert "chat-C-third" in final


# --- cancellation BEFORE the per-tab loop ----------------------------------- #


def _seeded_three(tmp_path, monkeypatch) -> list[str]:
    """A home with three listed, restorable tabs. Returns the listed keys."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-a", "chat-2-b", "chat-3-c"]
    for key in keys:
        _seed_session(state, key)
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": keys, "ts": 0.0}), encoding="utf-8"
    )
    return keys


@pytest.mark.parametrize("victim", ["_read_open_slots_snapshot", "_build_kiro_model_map"])
def test_cancelling_before_the_first_tab_cannot_prune_the_registry(tmp_path, monkeypatch, victim):
    """A shutdown landing on either pre-loop await leaves the file intact.

    These two awaits are suspension points like any other, and they sit AHEAD of
    the per-tab loop -- so the carry guard that protects a mid-loop cancellation has
    nothing to carry at the first (the seed is not read yet) and was not yet armed at
    the second. With the latch flipping unconditionally, the next flush then saw an
    empty slot map it was licensed to treat as authoritative and wrote ``[]``, which
    the boot after that reads as a registry the user deliberately emptied.
    """
    keys = _seeded_three(tmp_path, monkeypatch)
    state2 = _make_state(tmp_path / "sessions")
    from kiro_crew.dashboard import chat_persistence as cp

    entered = threading.Event()
    release = threading.Event()
    real = getattr(cp, victim)

    def _parking(*args, **kwargs):
        # Runs on the to_thread worker. It announces arrival and is then HELD until
        # the test releases it, which is what makes the cancellation below land while
        # the await is genuinely suspended. A fixed sleep here would be a bet that
        # the host gets to `task.cancel()` within it: lose the bet and the read
        # completes first, the cancellation lands somewhere else entirely, and the
        # test passes while exercising nothing.
        entered.set()
        release.wait(timeout=30)
        return real(*args, **kwargs)

    monkeypatch.setattr(cp, victim, _parking)

    async def _cancel_inside_the_await() -> None:
        task = asyncio.ensure_future(restore_open_slots_async(state2))
        try:
            await async_wait_until(
                entered.is_set,
                timeout=10,
                describe=lambda: f"{victim} was never entered; slots={sorted(state2._slots)}",
            )
            task.cancel()
        finally:
            # Release before awaiting the task: the worker is parked, and leaving it
            # parked would hold a thread for the full 30s even after the loop side
            # has been cancelled. Cancel again for the timeout path, where the wait
            # above failed and nothing has cancelled yet.
            release.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(_cancel_inside_the_await())

    assert state2._slots == {}, "the premise: no tab was restored before the cancel"
    assert state2.open_slots_restored is False, (
        "an unfinished restore licensed the writers to treat an empty slot map as "
        "the authoritative open-tab set"
    )

    # The flush that a shutdown runs next must merge, not prune.
    state2._persist_open_slots()
    payload = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert set(payload["keys"]) == set(keys)


def test_a_restore_with_nothing_to_do_still_releases_the_latch(tmp_path, monkeypatch):
    """The control: "nothing to enumerate" is a completion, not an abort.

    Without it the latch would be an off-switch for every home with no open tabs and
    for a surface that never restores at all, which is the failure the unconditional
    flip was guarding against in the first place.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")

    assert asyncio.run(restore_open_slots_async(state)) == 0
    assert state.open_slots_restored is True

    # And a present-but-empty registry reads the same way.
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": [], "ts": 0.0}), encoding="utf-8")
    state2 = _make_state(tmp_path / "sessions")
    assert asyncio.run(restore_open_slots_async(state2)) == 0
    assert state2.open_slots_restored is True


def test_a_raise_in_the_snapshot_read_cannot_prune_the_registry(tmp_path, monkeypatch):
    """The synchronous driver owes the same guarantee on its own read.

    Its reads are inline, so a GeneratorExit cannot land on them -- but an exception
    from the read itself propagates through the same ``finally``, and that exit has
    enumerated nothing either.
    """
    keys = _seeded_three(tmp_path, monkeypatch)
    state2 = _make_state(tmp_path / "sessions")
    from kiro_crew.dashboard import chat_persistence as cp

    def _boom():
        raise RuntimeError("snapshot read exploded")

    monkeypatch.setattr(cp, "_read_open_slots_snapshot", _boom)

    with pytest.raises(RuntimeError):
        restore_open_slots(state2)

    assert state2.open_slots_restored is False
    state2._persist_open_slots()
    payload = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert set(payload["keys"]) == set(keys)
