"""Conductor work ledger store: concurrent writers, the lock files, and the undo's lock holds.

Two writers against one item leave a parseable record and an uninterleaved event log, the caps
and the single binding hold under concurrent writers, the path guards share one containment
check, the lock files keep their bytes and owner-only mode, and the undo of a write waits for
every lock the writers of its files hold.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from unittest import mock

import pytest
import work_ledger_helpers
from windows_sim import read_sharing_violation
from work_ledger_helpers import CONDUCTOR, WORKER, _bytes_on_disk, _lift_goal_cap, _new_item

from kiro_crew import atomic_write, platform_compat
from kiro_crew import work_ledger as wl

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_home = work_ledger_helpers._isolated_home


# ── concurrency ───────────────────────────────────────────────────────────


def test_two_concurrent_writers_leave_a_parseable_item_and_a_clean_event_log():
    """One report loop and one conductor loop against the SAME item.

    Threads rather than processes so the test runs unchanged on Windows: the locks
    are taken on separate descriptors, which serialise across threads exactly as
    they do across processes, and nothing here uses a POSIX-only API.
    """
    item_id = _new_item()
    rounds = 25
    errors: list[BaseException] = []

    def report() -> None:
        try:
            for index in range(rounds):
                wl.apply_worker_report(
                    CONDUCTOR,
                    item_id,
                    status="blocked" if index % 2 else "done",
                    summary=f"worker {index}",
                    artifacts={"step": str(index)},
                )
        except BaseException as exc:  # pragma: no cover - surfaced by the assert
            errors.append(exc)

    def decide() -> None:
        try:
            for index in range(rounds):
                wl.apply_conductor_action(
                    CONDUCTOR, "decide", item_id=item_id, decision=f"conductor {index}"
                )
        except BaseException as exc:  # pragma: no cover - surfaced by the assert
            errors.append(exc)

    threads = [threading.Thread(target=report), threading.Thread(target=decide)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not errors, errors
    assert not any(thread.is_alive() for thread in threads)

    # The item still parses, and holds one writer's field beside the other's.
    stored = json.loads(wl.item_path(CONDUCTOR, item_id).read_text(encoding="utf-8"))
    assert stored["item_id"] == item_id
    assert stored["decision"].startswith("conductor ")
    assert stored["summary"].startswith("worker ")

    # Every line is a whole JSON object — no interleaving, no partial line.
    raw = wl.item_events_path(CONDUCTOR, item_id).read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert lines
    for line in lines:
        parsed = json.loads(line)
        assert parsed["item_id"] == item_id
        assert parsed["kind"] in wl.EVENT_KINDS
    assert len(wl.read_events(CONDUCTOR, item_id)) == len(lines)


def test_the_item_cap_holds_under_concurrent_creates():
    wl.ensure_conductor(CONDUCTOR, goal="g")
    _lift_goal_cap()
    refused: list[str] = []

    def create() -> None:
        for _ in range(12):
            try:
                wl.apply_conductor_action(CONDUCTOR, "create", title="t", acceptance={})
            except wl.WorkLedgerError as exc:
                refused.append(exc.code)

    threads = [threading.Thread(target=create) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert len(wl.list_work_items(CONDUCTOR)) == wl.MAX_ITEMS_PER_CONDUCTOR
    assert refused and set(refused) == {wl.CODE_ITEM_CAP_EXCEEDED}


def test_the_lock_order_is_conductor_item_binding_and_is_documented():
    doc = wl.__doc__ or ""
    assert "conductor -> item -> binding(worker)" in doc
    assert "conductor" in (wl.conductor_lock.__doc__ or "").lower()
    assert "FIRST" in (wl.conductor_lock.__doc__ or "")
    assert "SECOND" in (wl.item_lock.__doc__ or "")
    assert "THIRD" in (wl.binding_lock.__doc__ or "")


def test_two_conductors_binding_one_worker_at_once_yield_exactly_one_binding():
    conductors = [f"chat-{n}-c" for n in range(4)]
    items = []
    for key in conductors:
        wl.ensure_conductor(key, goal="g")
        items.append(
            (
                key,
                wl.apply_conductor_action(key, "create", title="t", acceptance={})["item"].item_id,
            )
        )
    # One record per thread, keyed by the conductor that thread bound with, so a
    # thread that dies silently or never finishes still leaves a named entry. The
    # Catching only ``wl.WorkLedgerError`` and appending nothing on anything
    # else lets a Windows sharing violation (a bare ``OSError``) kill a thread
    # without a trace and shorten the count, so the test reports a bare count
    # mismatch and throws away the one fact that names the cause. Each thread
    # starts as ``"never-started"`` and is overwritten only
    # when its body actually runs, so a thread that never scheduled is
    # distinguishable from one that ran and died.
    records: dict[str, str] = {key: "never-started" for key, _ in items}

    # ``records`` keeps the COUNTED outcome verbatim ("bound" or the error code) so
    # the two assertions below count only that. ``details`` keeps, for the same
    # thread, the field and message behind a WorkLedgerError -- an ``invalid_value``
    # says nothing about WHICH validator refused or WHAT value it saw. Recording
    # ``exc.field`` and the message names the choke point for a Windows-shard
    # occurrence. This is diagnosis, not tolerance: the assertions are unaffected.
    details: dict[str, str] = {key: "" for key, _ in items}

    def bind(key: str, item_id: str) -> None:
        try:
            wl.apply_conductor_action(key, "bind", item_id=item_id, worker_session_key=WORKER)
            records[key] = "bound"
        except wl.WorkLedgerError as exc:
            records[key] = exc.code
            details[key] = f"field={exc.field!r} msg={exc}"
        except BaseException as exc:  # noqa: BLE001 — diagnostic: never swallow, always name
            records[key] = f"{type(exc).__name__}: {exc}"

    threads = {key: threading.Thread(target=bind, args=(key, item_id)) for key, item_id in items}
    for thread in threads.values():
        thread.start()
    for thread in threads.values():
        thread.join(timeout=120)

    # A ``join`` that timed out returns with the thread still alive, which is
    # indistinguishable from a silent death by the records alone — mark those
    # explicitly so a hung thread names itself instead of masquerading as one that
    # appended nothing.
    for key, thread in threads.items():
        if thread.is_alive():
            records[key] = f"still-running-after-120s (last record: {records[key]})"

    outcomes = list(records.values())
    _detail = "; ".join(
        f"{key}={records[key]}" + (f" ({details[key]})" if details[key] else "") for key, _ in items
    )
    assert outcomes.count("bound") == 1, (
        f"expected exactly one thread to bind the worker, got "
        f"{outcomes.count('bound')} — per-thread outcomes: {_detail}"
    )
    assert outcomes.count(wl.CODE_ALREADY_BOUND) == 3, (
        f"expected 3 threads to report {wl.CODE_ALREADY_BOUND!r}, got "
        f"{outcomes.count(wl.CODE_ALREADY_BOUND)} — a shortfall means a thread hit "
        f"something other than a clean already-bound refusal (a bare OSError from a "
        f"Windows sharing violation, or a thread that never finished); per-thread "
        f"outcomes: {_detail}"
    )
    binding = wl.read_binding(WORKER)
    assert binding is not None
    bound = [pair for pair in items if pair == binding]
    assert len(bound) == 1
    for key, item_id in items:
        item = wl.read_work_item(key, item_id)
        assert item is not None
        assert (item.worker_session_key == WORKER) == ((key, item_id) == binding)


def test_the_guards_tolerate_a_prefixed_leaf_resolve(monkeypatch):
    """A leaf resolve that comes back extended-length-prefixed must not refuse.

    ``ntpath.realpath`` keeps Windows' ``\\\\?\\`` prefix when its prefix-strip
    re-check races a concurrent swap of the same file -- exactly what a losing
    ``bind`` sees while the winner replaces the binding record it is composing
    the path of. A guard that compares that prefixed child against an
    unprefixed parent reads the spelling as an escape and turns a clean
    ``already_bound`` refusal into ``invalid_value``, which is Windows-only
    because POSIX ``realpath`` has no prefix re-check. The guards therefore go
    through ``resolved_within``, which strips the prefix from BOTH sides before
    comparing. POSIX cannot produce the prefixed spelling natively, so this
    simulates it where it arises: ``Path.resolve`` returning the
    extended-length form of the correct answer.
    """
    import kiro_crew.session_ledger as sl

    original_resolve = Path.resolve

    def prefixing(self: Path, *args, **kwargs) -> Path:
        real = original_resolve(self, *args, **kwargs)
        text = str(real)
        # Only a drive-absolute spelling can legally carry the prefix; POSIX
        # paths get a synthetic one through the fold's own contract instead.
        return Path(f"\\\\?\\{text}") if text[1:2] == ":" else real

    # The pure half: the shared fold must strip both prefix spellings.
    assert sl.strip_extended_length_prefix(Path("\\\\?\\C:\\store\\bindings\\w.json")) == Path(
        "C:\\store\\bindings\\w.json"
    )
    assert sl.strip_extended_length_prefix(Path("\\\\?\\UNC\\host\\share\\x")) == Path(
        "\\\\host\\share\\x"
    )

    # The integration half: both guards answer a real path, not a refusal,
    # when every resolve is prefixed the way the Windows race spells it.
    monkeypatch.setattr(Path, "resolve", prefixing)
    assert wl.binding_path(WORKER).name == f"{wl._store_name(WORKER)}.json"
    assert wl.conductor_dir(CONDUCTOR).name == wl._store_name(CONDUCTOR)
    # And hostile keys are still refused with the guards' own code.
    with pytest.raises(wl.WorkLedgerError) as excinfo:
        wl.conductor_dir("evil/../key")
    assert excinfo.value.code == wl.CODE_INVALID_VALUE
    with pytest.raises(wl.WorkLedgerError) as excinfo:
        wl.binding_path("has\0null")
    assert excinfo.value.code == wl.CODE_INVALID_VALUE


def test_a_planted_link_at_a_guarded_leaf_is_refused(tmp_path):
    """A pre-planted symlink at either guard's composed leaf must be refused.

    ``resolved_within`` resolves the composed leaf, so a link whose target sits
    outside the base lands outside the resolved base and reads as an escape.
    This pins that the shared-helper path keeps the containment the guards had
    when each spelled the check inline. Skipped where symlinks cannot be
    created (Windows without privilege), matching how the defense is exercised
    there; the prefix-tolerance half has its own test above.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.json").write_text("{}", encoding="utf-8")

    linked_dir = wl._work_ledger_root() / wl._store_name(CONDUCTOR)
    linked_dir.parent.mkdir(parents=True, exist_ok=True)
    linked_binding = wl.bindings_dir() / f"{wl._store_name(WORKER)}.json"
    linked_binding.parent.mkdir(parents=True, exist_ok=True)
    try:
        linked_dir.symlink_to(outside, target_is_directory=True)
        linked_binding.symlink_to(outside / "victim.json")
    except OSError:
        pytest.skip("cannot create symlinks on this platform/account")
    with pytest.raises(wl.WorkLedgerError) as excinfo:
        wl.conductor_dir(CONDUCTOR)
    assert excinfo.value.code == wl.CODE_INVALID_VALUE
    with pytest.raises(wl.WorkLedgerError) as excinfo:
        wl.binding_path(WORKER)
    assert excinfo.value.code == wl.CODE_INVALID_VALUE


def test_the_guards_share_one_containment_helper(monkeypatch):
    """Both guards must route through ``session_ledger.resolved_within``.

    The helper is where the prefix-stripped, single-base-resolve comparison
    lives; a guard that re-inlines its own two-``resolve()`` comparison
    silently reintroduces the Windows race the helper exists to close, with
    every existing test still green on POSIX. Patching the helper to refuse
    and watching both guards refuse is what makes the routing itself a tested
    property rather than a convention.
    """
    import kiro_crew.work_ledger as wl_module

    monkeypatch.setattr(wl_module, "resolved_within", lambda base, name: None)
    with pytest.raises(wl.WorkLedgerError) as excinfo:
        wl.binding_path(WORKER)
    assert excinfo.value.code == wl.CODE_INVALID_VALUE
    with pytest.raises(wl.WorkLedgerError) as excinfo:
        wl.conductor_dir(CONDUCTOR)
    assert excinfo.value.code == wl.CODE_INVALID_VALUE


def test_a_contended_item_read_still_refuses_with_already_bound():
    """A losing bind must refuse PERMANENTLY, not fail as if the write broke.

    ``_refuse_if_worker_holds_open_item`` reads the prior item under the WORKER's
    binding lock, while that item's own conductor holds a DIFFERENT lock, so the
    read is unserialized against a correct concurrent writer. On Windows that read
    raises ``PermissionError``, which escapes the guard's ``WorkLedgerError`` arm and
    reaches the dashboard route as a transient 503 "try again" -- telling a conductor
    to retry a binding that is legitimately taken until the item closes.

    ``read_sharing_violation`` reproduces the fault on any OS, so this drives the
    exact path a Windows host takes. It does NOT prove the real OS behaviour, only
    that the read survives one contended window and the refusal stays permanent.
    """
    holder, loser = "chat-hold-c", "chat-lose-c"
    for key in (holder, loser):
        wl.ensure_conductor(key, goal="g")
    held_item = wl.apply_conductor_action(holder, "create", title="t", acceptance={})[
        "item"
    ].item_id
    loser_item = wl.apply_conductor_action(loser, "create", title="t", acceptance={})[
        "item"
    ].item_id
    wl.apply_conductor_action(holder, "bind", item_id=held_item, worker_session_key=WORKER)

    with (
        mock.patch.object(platform_compat, "IS_WINDOWS", True),
        mock.patch.object(atomic_write, "_REPLACE_BACKOFF_SECONDS", 0),
        read_sharing_violation(match=f"{held_item}.json", times=1) as state,
    ):
        with pytest.raises(wl.WorkLedgerError) as caught:
            wl.apply_conductor_action(loser, "bind", item_id=loser_item, worker_session_key=WORKER)

    assert caught.value.code == wl.CODE_ALREADY_BOUND, (
        f"a contended read of the prior item must still refuse with "
        f"{wl.CODE_ALREADY_BOUND!r}, got {caught.value.code!r}: {caught.value}"
    )
    assert state["n"] >= 2, (
        "the guard's read of the prior item must be retried after the simulated "
        f"sharing violation; intercepted reads: {state['n']}"
    )
    # The loser's own item keeps no binding, and the holder's keeps the one it won.
    assert wl.read_binding(WORKER) == (holder, held_item)


def test_acquiring_a_lock_does_not_truncate_the_lock_file():
    """The lock-file open must be WRITABLE but MUST NOT truncate.

    This is the property whose absence made
    ``test_two_conductors_binding_one_worker_at_once_yield_exactly_one_binding``
    fail on Windows only. ``msvcrt.locking`` needs a writable handle, so the fd
    cannot be opened ``"r"``; but ``"w"`` truncates on open, and on Windows a
    truncating open of a lock file whose first byte another holder already locked
    raises a sharing violation instead of waiting — so the second, contending
    acquirer crashes with a bare ``OSError`` before it reaches ``file_lock`` and
    the bind it was serialising is never mutually excluded. POSIX ``flock``
    tolerates the truncate, which is why the defect was invisible on Linux.

    Truncation is the direct, platform-independent observable: seed the lock file
    with bytes, acquire and release the lock, and assert the bytes survived. Under
    the old ``open(path, "w")`` this test fails on every platform (the file is
    emptied); under ``open_lock_file``'s non-truncating open it passes, and the same
    non-truncating open is what stops the Windows sharing violation.
    """
    wl.ensure_conductor(CONDUCTOR, goal="g")
    lock_path = wl.conductor_dir(CONDUCTOR) / wl._LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = b"held by a prior acquirer\n"
    lock_path.write_bytes(sentinel)
    with wl.conductor_lock(CONDUCTOR):
        pass
    assert lock_path.read_bytes() == sentinel


def test_every_store_lock_form_leaves_the_lock_file_bytes_alone():
    """The same property for every lock the store takes: the conductor's and an
    item's, creating and non-creating, and the purge's non-blocking hold over every
    item lock. Bytes are read after release, never under the lock, because a
    Windows region lock is mandatory and a locked read would answer EACCES."""
    item_id = _new_item()
    conductor_lock_path = wl.conductor_dir(CONDUCTOR) / wl._LOCK_FILE
    item_lock_path = wl._item_lock_path(CONDUCTOR, item_id)
    sentinel = b"held by a prior acquirer\n"
    for path in (conductor_lock_path, item_lock_path):
        path.write_bytes(sentinel)

    with wl.conductor_lock(CONDUCTOR, create=False):
        pass
    with wl.item_lock(CONDUCTOR, item_id):
        pass
    with wl.item_lock(CONDUCTOR, item_id, create=False):
        pass
    with wl._hold_every_item_lock(wl.conductor_dir(CONDUCTOR)):
        pass

    assert conductor_lock_path.read_bytes() == sentinel
    assert item_lock_path.read_bytes() == sentinel


def test_a_non_creating_store_lock_refuses_a_missing_lock_file_and_creates_nothing():
    """``create=False`` is the deleter's form: locking a store that is gone must
    raise ``FileNotFoundError`` and leave no directory or lock file behind."""
    directory = wl.conductor_dir(CONDUCTOR)
    assert not directory.exists()
    with pytest.raises(FileNotFoundError):
        with wl.conductor_lock(CONDUCTOR, create=False):
            pass
    assert not directory.exists()


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
def test_store_lock_files_are_created_owner_only():
    """Every other file in a ledger store is written ``0o600``; its lock files are
    created the same way, whatever the process umask lets a plain create through."""
    import os
    import stat

    def _mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    old_umask = os.umask(0o022)
    try:
        item_id = _new_item()
        conductor_lock_path = wl.conductor_dir(CONDUCTOR) / wl._LOCK_FILE
        item_lock_path = wl._item_lock_path(CONDUCTOR, item_id)
        assert _mode(conductor_lock_path) == 0o600
        assert _mode(item_lock_path) == 0o600
        # The locked recreate path restores a hand-removed item lock for a live item.
        item_lock_path.unlink()
        with wl.item_lock(CONDUCTOR, item_id, create=False):
            pass
        assert _mode(item_lock_path) == 0o600
    finally:
        os.umask(old_umask)


# ── the undo holds the locks the writers of its files hold ────────────────


def test_the_undo_waits_for_a_writer_holding_the_item_it_rewrites():
    """An existing item's record is not rewritten under the writer holding it.

    The route's write returns before the undo runs, so the item lock is free in
    between and another writer can take it. The undo then rewrites that item's
    record and event log from bytes older than the writer's, so without the item
    lock it replaces a record mid-write: torn on POSIX, a refused open on
    Windows. The dashboard's board lock cannot stand in for it -- that one is
    in-process, and this lock is what a second gateway obeys.
    """
    item_id = _new_item()
    snapshot = wl.snapshot_for_write(CONDUCTOR, item_id=item_id)
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="ship it")
    started, finished = threading.Event(), threading.Event()

    def _undo() -> None:
        started.set()
        wl.restore_snapshot(CONDUCTOR, snapshot, item_id=item_id)
        finished.set()

    undo = threading.Thread(target=_undo, daemon=True)
    with wl.item_lock(CONDUCTOR, item_id, create=False):
        undo.start()
        assert started.wait(10)
        assert not finished.wait(1.5), "the undo rewrote the record under a lock held here"
        # Still the writer's bytes, not the snapshot's: nothing was put back yet.
        assert _bytes_on_disk(item_id)[0] != snapshot[str(wl.item_path(CONDUCTOR, item_id))]
    undo.join(60)
    assert finished.is_set(), "the undo must proceed once the writer lets the lock go"
    assert _bytes_on_disk(item_id) == (
        snapshot[str(wl.item_path(CONDUCTOR, item_id))],
        snapshot[str(wl.item_events_path(CONDUCTOR, item_id))],
    )


def test_the_undo_waits_for_a_conductor_holding_the_binding_it_rewrites():
    """The same gap at the binding, whose lock is third in the lock order.

    ``bind`` snapshots ``bindings/<worker>.json``, and two conductors binding one
    worker serialise on that lock so neither sees it free while the other writes.
    An undo that rewrites the file without the lock lands between one's read and
    its write.
    """
    item_id = _new_item()
    snapshot = wl.snapshot_for_write(CONDUCTOR, item_id=item_id, worker_session_key=WORKER)
    started, finished = threading.Event(), threading.Event()

    def _undo() -> None:
        started.set()
        wl.restore_snapshot(CONDUCTOR, snapshot, item_id=item_id, worker_session_key=WORKER)
        finished.set()

    undo = threading.Thread(target=_undo, daemon=True)
    with wl.binding_lock(WORKER):
        undo.start()
        assert started.wait(10)
        assert not finished.wait(1.5), "the undo rewrote a binding under a lock held here"
    undo.join(60)
    assert finished.is_set(), "the undo must proceed once the binding lock is free"


def test_the_undo_leaves_a_second_gateways_write_alone_and_flags_the_cache():
    """An undo puts back only what its own write left; another gateway's survives.

    Two gateways share one store. A's write commits, its crew-log append fails,
    so A undoes. B's write lands in that gap and B has been told it is recorded.
    The per-file locks order neither, so bytes older than B's would replace B's
    record -- dropping a write the record holds, with nothing to announce it.
    Given what A's own write left behind, the undo passes that file over and
    flags the cache, which is the state a rebuild reconciles.
    """
    item_id = _new_item()
    snapshot = wl.snapshot_for_write(CONDUCTOR, item_id=item_id)
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="A ships it")
    left_by_a = wl.current_bytes(snapshot)
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="B ships it")
    landed_by_b = _bytes_on_disk(item_id)

    wl.restore_snapshot(CONDUCTOR, snapshot, item_id=item_id, expected=left_by_a)

    assert _bytes_on_disk(item_id) == landed_by_b, "B's committed record was replaced"
    assert b"B ships it" in landed_by_b[0]
    assert wl.cache_dirty(CONDUCTOR), "a partial undo must flag the cache for a rebuild"
    wl.clear_cache_dirty(CONDUCTOR)


def test_the_undo_still_puts_back_the_files_its_own_write_left():
    """The comparison must not turn every undo into a no-op.

    Nothing writes in the gap here, so every snapshotted file still holds what
    the write left and the undo is the plain one: the bytes go back and the cache
    is untouched. Without this, skipping everything would read as a clean undo.
    """
    item_id = _new_item()
    snapshot = wl.snapshot_for_write(CONDUCTOR, item_id=item_id)
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="ship it")

    wl.restore_snapshot(CONDUCTOR, snapshot, item_id=item_id, expected=wl.current_bytes(snapshot))

    assert _bytes_on_disk(item_id) == (
        snapshot[str(wl.item_path(CONDUCTOR, item_id))],
        snapshot[str(wl.item_events_path(CONDUCTOR, item_id))],
    )
    assert wl.cache_dirty(CONDUCTOR) is None


def test_the_undo_of_a_purged_board_recreates_nothing():
    """A store removed after the snapshot holds no mutation to take back.

    Every file the undo writes is the board's own, so recreating one to put bytes
    back resurrects a board an operator deleted -- and a creating lock taken to
    do it rebuilds the directory the sweep removed, leaving the lock-only store
    the sweep then keeps forever.
    """
    item_id = _new_item()
    snapshot = wl.snapshot_for_write(CONDUCTOR, item_id=item_id)
    directory = wl.conductor_dir(CONDUCTOR)
    shutil.rmtree(wl.items_dir(CONDUCTOR), ignore_errors=True)
    shutil.rmtree(directory, ignore_errors=True)

    wl.restore_snapshot(CONDUCTOR, snapshot, item_id=item_id)

    assert not directory.exists(), "the undo must not recreate the store it found gone"
    assert not wl.item_path(CONDUCTOR, item_id).exists()


def test_the_undo_still_puts_every_snapshotted_file_back():
    """The partner the two refusals above need: an undo that did nothing at all
    would pass them. A healthy board's undo restores the snapshotted bytes
    exactly."""
    item_id = _new_item()
    snapshot = wl.snapshot_for_write(CONDUCTOR, item_id=item_id)
    before = _bytes_on_disk(item_id)
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="ship it")
    assert _bytes_on_disk(item_id) != before

    wl.restore_snapshot(CONDUCTOR, snapshot, item_id=item_id)

    assert _bytes_on_disk(item_id) == before


def test_the_undo_removes_an_item_the_write_created():
    """A create's undo takes the new item away, and names it twice without hanging.

    ``created_item`` and ``item_id`` are the same id on a create, and one thread
    cannot hold one lock file twice -- the second acquire would wait on the first
    until the ceiling. So the locks this takes are deduplicated, and this test is
    what fails if they stop being.
    """
    wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    snapshot = wl.snapshot_for_write(CONDUCTOR)
    created = wl.apply_conductor_action(
        CONDUCTOR, "create", title="port the gate", acceptance={"kind": "human_approval"}
    )["item"].item_id
    assert wl.item_path(CONDUCTOR, created).exists()

    wl.restore_snapshot(CONDUCTOR, snapshot, created_item=created, item_id=created)

    assert not wl.item_path(CONDUCTOR, created).exists()
    assert not wl.item_events_path(CONDUCTOR, created).exists()
