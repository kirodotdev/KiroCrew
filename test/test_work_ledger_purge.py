"""Conductor work ledger store: the maintenance purge and the census it decides by.

Retention reads the actual latest activity, a purge refuses a ledger with an open, torn or
unknown-state item unless the caller asks, it holds the conductor lock and every item lock
through the removal and never unlinks a lock file whose handle is held, and a writer that
arrives after the purge refuses rather than rebuilding the store.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, WORKER, _new_item

from kiro_crew import platform_compat
from kiro_crew import work_ledger as wl

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_home = work_ledger_helpers._isolated_home


# ── maintenance ───────────────────────────────────────────────────────────


def _pin_purge_clock(monkeypatch, directory, *, age):
    """Evaluate age against the real census/mtime; leave parsing and locks intact."""
    latest = wl._newest_activity(directory, wl.census_items(directory))
    assert latest is not None
    moment = latest + age

    class EvaluationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(wl, "datetime", EvaluationClock)
    return latest


@pytest.mark.parametrize("residue", [False, True], ids=["closed-item", "headerless-residue"])
@pytest.mark.parametrize(
    "age,idle_for,removed",
    [
        # NO WINDOW: age is not consulted, so nothing about the store's
        # timestamps can refuse. The negative age is a ``latest`` reading AHEAD
        # of the clock -- what a file mtime does where the filesystem's
        # resolution is finer than the clock's advance -- and the elapsed time is
        # then negative, which is less than a zero window.
        (timedelta(microseconds=-1), timedelta(0), True),
        (timedelta(seconds=1), timedelta(0), True),
        # Less than no window is still no window, in both clock directions.
        (timedelta(microseconds=-1), timedelta(days=-1), True),
        (timedelta(seconds=1), timedelta(days=-1), True),
        # A POSITIVE window does judge age, and there the same future reading
        # REFUSES: the caller asked for a judgement, a store whose newest write
        # reads ahead of the clock has just been written to, and refusing is the
        # conservative half of an irreversible delete.
        (timedelta(microseconds=-1), timedelta(days=30), False),
        (timedelta(days=30, microseconds=-1), timedelta(days=30), False),
        (timedelta(days=30), timedelta(days=30), True),
        (timedelta(days=30, microseconds=1), timedelta(days=30), True),
    ],
    ids=[
        "zero-window-future",
        "zero-window-past",
        "negative-window-future",
        "negative-window-past",
        "positive-window-future-refused",
        "inside-window",
        "at-boundary",
        "past-boundary",
    ],
)
def test_purge_retention_uses_actual_latest_activity(monkeypatch, residue, age, idle_for, removed):
    if residue:
        wl.ensure_conductor(CONDUCTOR, goal="g")
        directory = wl.conductor_dir(CONDUCTOR)
        (directory / "conductor.json").unlink()
    else:
        item_id = _new_item()
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
        directory = wl.conductor_dir(CONDUCTOR)
    before = {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }
    latest = _pin_purge_clock(monkeypatch, directory, age=age)
    assert wl._newest_activity(directory, wl.census_items(directory)) == latest

    if removed:
        assert wl.purge_conductor(CONDUCTOR, allow_unreadable=residue, idle_for=idle_for) is True
        assert not directory.exists()
    else:
        with pytest.raises(wl.WorkLedgerError, match="retention window") as caught:
            wl.purge_conductor(CONDUCTOR, allow_unreadable=residue, idle_for=idle_for)
        assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
        assert directory.is_dir()
        assert {path: (directory / path).read_bytes() for path in before} == before


def test_purge_retention_skips_the_age_gate_when_there_is_no_activity_to_read(monkeypatch):
    """``latest is None`` reaches the same verdict as a non-positive window: no refusal.

    A store whose newest activity cannot be read at all has no age to judge, so
    the gate is skipped even under a wide window. Forced rather than staged: a
    directory that exists can always be statted, so ``_newest_activity`` returns
    ``None`` only if every candidate is unavailable.
    """
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    monkeypatch.setattr(wl, "_newest_activity", lambda *a, **k: None)

    assert (
        wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(days=30)) is True
    )
    assert not directory.exists()


@pytest.mark.parametrize(
    "idle_for", [timedelta(0), timedelta(days=-1)], ids=["zero-window", "negative-window"]
)
def test_a_window_that_does_not_judge_age_still_refuses_an_open_item(monkeypatch, idle_for):
    """Declining a retention window declines AGE, and nothing else.

    The open-item, unreadable-record, lock and ownership refusals are independent
    of the window, so a caller that passes no window still cannot delete a ledger
    a worker is live on.
    """
    _new_item()
    directory = wl.conductor_dir(CONDUCTOR)
    _pin_purge_clock(monkeypatch, directory, age=timedelta(microseconds=-1))

    with pytest.raises(wl.WorkLedgerError, match="open item") as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=idle_for)
    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert directory.is_dir()


def test_purge_conductor_removes_the_ledger_under_the_conductor_lock(monkeypatch):
    """The removal happens INSIDE the conductor lock, so nothing it deletes can be
    half-written by a ``goal`` or ``create`` holding that same lock.

    Asserted by counting what was gone while the lock was held rather than by the
    exit code: a purge that ran entirely outside the lock would remove the same
    directory and return the same value.
    """
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    assert (directory / "items" / f"{item_id}.json").exists()
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))

    real_lock = wl.conductor_lock
    inside: list[bool] = []

    @contextlib.contextmanager
    def _watched(slot_key: str, **kwargs):
        with real_lock(slot_key, **kwargs):
            yield
            # Recorded on the way OUT, still under the hold: the item RECORD must
            # already be gone by the time the lock is released. The items
            # directory itself outlives the hold by design -- it still holds the
            # item lock files, which go only after their handles are closed.
            inside.append(not (directory / "items" / f"{item_id}.json").exists())

    with mock.patch.object(wl, "conductor_lock", _watched):
        assert wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0)) is True

    assert inside == [True], "the ledger was removed outside the conductor lock"
    assert not directory.exists()
    assert wl.read_conductor(CONDUCTOR) is None


def test_purge_conductor_refuses_a_conductor_with_no_items_at_all(monkeypatch):
    """Finished-LOOKING is not finished, and the store applies the rule itself so a
    caller-built report cannot turn an empty conductor into a purgeable one --
    whatever its header looks like."""
    wl.ensure_conductor(CONDUCTOR, goal="never dispatched")
    directory = wl.conductor_dir(CONDUCTOR)
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=timedelta(0))

    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert (directory / "conductor.json").exists()


def test_newest_activity_falls_back_to_the_directory_when_the_header_is_gone(monkeypatch):
    """A header that cannot be statted must not make the newest close the only
    reading: ``atomic_write`` renames into the directory, so a store written to a
    minute ago is fresh by its directory even with no header to say so."""
    from datetime import timedelta

    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    path = wl.item_path(CONDUCTOR, item_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    # nosemgrep: kirocrew.test-naive-local-time -- an aware stamp 90 days back; only its distance from now is read
    record["closed_at"] = (datetime.now().astimezone() - timedelta(days=90)).isoformat()
    path.write_text(json.dumps(record), encoding="utf-8")
    (directory / "conductor.json").unlink()  # the directory is written to NOW
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=timedelta(days=30))

    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert "retention window" in str(caught.value)
    assert path.exists(), "the closed item survives"


def test_purge_conductor_refuses_a_torn_header_unless_the_caller_asks(monkeypatch):
    """The header is re-read under the lock with the store's own ``header_damage``
    check -- the one the sweep's scanner uses -- so the recheck cannot trust a
    report over the store's account of itself."""
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    (directory / "conductor.json").write_text("[]", encoding="utf-8")
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))
    assert wl.header_damage(directory) == "conductor record is not a JSON object"

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0))

    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert "conductor record" in str(caught.value)
    assert directory.is_dir()
    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=timedelta(0)) is True


def test_census_reads_an_item_shaped_file_with_a_foreign_stem_as_unreadable(monkeypatch):
    """A ``.json`` under ``items/`` whose stem is not an id the store minted is a
    renamed or hand-moved record. Skipped, a plain purge would delete it with the
    rest of the contents; counted unreadable, it needs ``allow_unreadable``."""
    item_id = _new_item()
    other = wl.apply_conductor_action(
        CONDUCTOR, "create", title="second", acceptance={"kind": "human_approval"}
    )["item"].item_id
    for each in (item_id, other):
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=each, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    stray = directory / "items" / "renamed-by-hand.json"
    wl.item_path(CONDUCTOR, item_id).rename(stray)
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))
    # Skipped rather than counted, the census would read "one closed item, nothing
    # unreadable" -- a finished ledger -- and a plain purge would remove the stray
    # with the rest of the contents.

    census = wl.census_items(directory)

    assert (census.closed, census.unreadable) == (1, 1)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0))
    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert stray.exists()


def test_purge_conductor_removes_a_headerless_itemless_residue_with_allow_unreadable(monkeypatch):
    """The no-items refusal is for a conductor WITH a header. A directory with
    neither -- the residue of a purge whose lock file could not go -- is damage,
    refused by a plain purge and removed when the caller asks."""
    wl.ensure_conductor(CONDUCTOR, goal="g")
    directory = wl.conductor_dir(CONDUCTOR)
    (directory / "conductor.json").unlink()
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0))
    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert "no conductor record" in str(caught.value)

    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=timedelta(0)) is True
    assert not directory.exists()


@pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="symlink creation needs no privilege on POSIX"
)
def test_the_content_walker_unlinks_a_linked_items_directory_as_a_name(tmp_path):
    """Defence in depth behind the census and the purge's own refusal: even called
    directly on a store whose ``items/`` is a link, the walker removes the LINK
    and never descends into the target."""
    wl.ensure_conductor(CONDUCTOR, goal="g")
    directory = wl.conductor_dir(CONDUCTOR)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.json").write_text("{}", encoding="utf-8")
    (elsewhere / "nested").mkdir()
    (directory / "items").symlink_to(elsewhere, target_is_directory=True)

    failures = wl._remove_contents_locked(directory)

    assert failures == 0
    assert not (directory / "items").exists() and not (directory / "items").is_symlink()
    assert (elsewhere / "precious.json").exists() and (elsewhere / "nested").is_dir()


def test_purge_conductor_is_a_no_op_for_a_ledger_that_does_not_exist():
    assert (
        wl.purge_conductor("chat-never-conducted", allow_unreadable=False, idle_for=timedelta(0))
        is False
    )


def test_purge_conductor_refuses_a_key_that_could_escape_the_root():
    """The same shape gate every path constructor passes through — a delete must
    not be the one call that takes an unchecked key."""
    with pytest.raises(wl.WorkLedgerError) as info:
        wl.purge_conductor("../../etc", allow_unreadable=False, idle_for=timedelta(0))
    assert info.value.code == wl.CODE_INVALID_VALUE


def test_purge_conductor_refuses_a_ledger_that_still_has_an_open_item(monkeypatch):
    """The census runs INSIDE the hold, so a caller's stale eligibility snapshot is
    caught here rather than acted on. ``_create_item`` takes this same lock across
    its whole transaction, so an item cannot appear between the check and the
    removal."""
    item_id = _new_item()
    _pin_purge_clock(monkeypatch, wl.conductor_dir(CONDUCTOR), age=timedelta(seconds=1))

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0))

    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert wl.read_work_item(CONDUCTOR, item_id) is not None
    assert wl.conductor_dir(CONDUCTOR).is_dir()


def test_purge_conductor_refuses_a_torn_item_unless_the_caller_asks(monkeypatch):
    """A torn record reads as absent to ``list_work_items``, so "every item is
    closed" must not be provable by damaging one — the refusal is the store's, not
    the caller's."""
    item_id = _new_item()
    wl.item_path(CONDUCTOR, item_id).write_text("{tor", encoding="utf-8")
    _pin_purge_clock(monkeypatch, wl.conductor_dir(CONDUCTOR), age=timedelta(seconds=1))

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0))
    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert wl.conductor_dir(CONDUCTOR).is_dir()

    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=timedelta(0)) is True
    assert not wl.conductor_dir(CONDUCTOR).exists()


def test_purge_conductor_reads_an_unknown_item_state_as_damage_not_as_closure(monkeypatch):
    """An unrecognised ``state`` is not evidence of closure, so the purge refuses.

    The classification is asserted too, not just the refusal: the census counts
    such an item as UNREADABLE, so ``allow_unreadable`` — the operator's explicit
    "yes, remove what you cannot parse" — clears it, while counting it as an open
    item would refuse forever and leave the ledger permanently uncollectable.
    """
    item_id = _new_item()
    path = wl.item_path(CONDUCTOR, item_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["state"] = "finished-ish"
    path.write_text(json.dumps(record), encoding="utf-8")
    _pin_purge_clock(monkeypatch, wl.conductor_dir(CONDUCTOR), age=timedelta(seconds=1))

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0))
    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert wl.conductor_dir(CONDUCTOR).is_dir()

    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=timedelta(0)) is True
    assert not wl.conductor_dir(CONDUCTOR).exists()


def test_create_item_refuses_when_the_header_is_gone():
    """A create that was waiting behind a purge must not write an item into a store
    whose header is gone -- that would be a ledger destroyed down to the records
    that made it one. The pre-lock snapshot is not a substitute; it describes a
    ledger that does not exist."""
    record = wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    (wl.conductor_dir(CONDUCTOR) / "conductor.json").unlink()

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl._create_item(CONDUCTOR, record, "late", {"kind": "human_approval"}, None)

    assert caught.value.code == wl.CODE_NO_LEDGER
    assert not list(wl.items_dir(CONDUCTOR).glob("it_*.json")), "no header-less item"


def test_a_late_report_on_a_purged_ledger_refuses_and_rebuilds_nothing(monkeypatch):
    """A worker whose binding outlived its conductor reports against a purged
    ledger. The item lock must not recreate ``<store>/items/<id>.lock`` on the way
    to ``unknown_item`` -- a lock-only store with no header and no items is one the
    sweep keeps forever -- so the lock is taken non-creating and a missing lock
    file IS the missing item."""
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))
    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0)) is True
    assert not directory.exists()

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="late")

    assert caught.value.code == wl.CODE_UNKNOWN_ITEM
    assert not directory.exists(), "the report must not rebuild the purged store"


def test_an_item_that_lost_only_its_lock_file_is_still_writable():
    """The other half of non-creating item locks: a store that still HOLDS the item
    record but lost its lock file (hand-removed) exists, so the lock is recreated
    for it rather than the item being reported unknown."""
    item_id = _new_item()
    wl._item_lock_path(CONDUCTOR, item_id).unlink()

    result = wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="still here")

    assert result["item"].summary == "still here"
    assert wl._item_lock_path(CONDUCTOR, item_id).exists()


def test_the_lost_lock_recreate_loses_a_purge_race_without_rebuilding_the_store(monkeypatch):
    """The recreate of a hand-removed item lock happens UNDER the conductor lock
    with the record re-checked inside the hold. Simulated: the purge completes in
    the gap between the writer's first ``exists()`` check and its acquire of the
    conductor lock. A creating open in that gap would rebuild ``items/<id>.lock``
    in the removed store; the locked recreate finds no ledger and touches
    nothing."""
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    wl._item_lock_path(CONDUCTOR, item_id).unlink()
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))

    real_lock = wl._existing_conductor_lock
    raced: list[bool] = []

    def _purge_then_lock(slot_key):
        if not raced:
            raced.append(True)
            assert (
                wl.purge_conductor(slot_key, allow_unreadable=False, idle_for=timedelta(0)) is True
            )  # the race: purge wins
        return real_lock(slot_key)

    monkeypatch.setattr(wl, "_existing_conductor_lock", _purge_then_lock)

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="late")

    assert caught.value.code in {wl.CODE_NO_LEDGER, wl.CODE_UNKNOWN_ITEM}
    assert raced == [True]
    assert not directory.exists(), "the losing writer must not rebuild the purged store"


def test_a_goal_on_a_purged_ledger_refuses_and_rebuilds_nothing(monkeypatch):
    """``conductor_lock`` creates the store around its lock file, so a ``goal`` that
    waited behind a purge would rebuild the directory before refusing on the
    missing header. The writer takes the non-creating form: a missing lock file is
    a missing ledger, and nothing is written."""
    record = wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    item_id = wl.apply_conductor_action(
        CONDUCTOR, "create", title="t", acceptance={"kind": "human_approval"}
    )["item"].item_id
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))
    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0)) is True

    with pytest.raises(wl.WorkLedgerError) as goal_refused:
        wl._write_goal(CONDUCTOR, record, "late goal", None)
    with pytest.raises(wl.WorkLedgerError) as create_refused:
        wl._create_item(CONDUCTOR, record, "late item", {"kind": "human_approval"}, None)

    assert goal_refused.value.code == wl.CODE_NO_LEDGER
    assert create_refused.value.code == wl.CODE_NO_LEDGER
    assert not directory.exists(), "neither writer may rebuild the purged store"


def test_a_torn_but_present_header_refuses_both_writers_under_the_lock():
    """The header holds the create count the goal's spend is measured against, and
    the pre-lock snapshot may hold it low -- every writer that held the lock since
    the snapshot was read is missing from it. So a header that is present but does
    not read under the lock refuses the write instead of standing the snapshot in
    for it: a create mints no record and bumps nothing, a goal rewrites nothing,
    and the torn bytes stay exactly as found for the rebuild to replace. The code is
    the one the same header reads as everywhere else -- corruption reads as absent
    -- and the message says the header is present."""
    record = wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    header = wl.conductor_dir(CONDUCTOR) / "conductor.json"
    header.write_text("{tor", encoding="utf-8")
    torn = header.read_bytes()

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl._create_item(CONDUCTOR, record, "after the tear", {"kind": "human_approval"}, None)
    assert caught.value.code == wl.CODE_NO_LEDGER
    assert "present but unreadable" in str(caught.value)
    assert "rebuild_from_projection" in str(caught.value)
    assert header.read_bytes() == torn, "the snapshot was not written over the torn header"
    assert not wl.items_dir(CONDUCTOR).exists(), "no record was minted"

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl._write_goal(CONDUCTOR, record, "new goal", 3)
    assert caught.value.code == wl.CODE_NO_LEDGER
    assert "present but unreadable" in str(caught.value)
    assert header.read_bytes() == torn


def test_census_reads_a_misnamed_item_as_unreadable_not_closed(monkeypatch):
    """``read_work_item`` treats a record whose stored id names another item as
    absent; the census applies the same rule, or a terminal ``state`` in a
    hand-moved file would count as closed and a plain purge would delete a record
    the store itself refuses to read."""
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    path = wl.item_path(CONDUCTOR, item_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["item_id"] = "it_00000000"
    path.write_text(json.dumps(record), encoding="utf-8")
    _pin_purge_clock(monkeypatch, wl.conductor_dir(CONDUCTOR), age=timedelta(seconds=1))

    census = wl.census_items(wl.conductor_dir(CONDUCTOR))

    assert (census.closed, census.unreadable, census.open_items) == (0, 1, 0)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0))
    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert path.exists(), "a plain purge must not delete a record the store will not read"


def test_purge_conductor_keeps_the_header_when_any_content_survives(monkeypatch):
    """Removal is ordered and the header goes LAST, only once everything else is
    gone -- so a failed removal leaves an identifiable store, never a header-less
    pile of items."""
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))
    original = Path.unlink

    def _refuse_item_record(self, *args, **kwargs):
        if self.name == f"{item_id}.json":
            raise PermissionError(32, "sharing violation")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _refuse_item_record)

    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0)) is False

    assert (directory / "conductor.json").exists(), "the header must survive a failure"
    assert (directory / "items" / f"{item_id}.json").exists()
    assert wl.read_conductor(CONDUCTOR) is not None, "the store stays identifiable"


def test_purge_conductor_removal_failures_are_counted_not_ignored(monkeypatch):
    """``rmtree(ignore_errors=True)`` would report success over a subtree it left
    standing; the count is what the header decision depends on."""
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    (directory / "stray.txt").write_text("x", encoding="utf-8")
    real_unlink = Path.unlink

    def _refuse_stray(self, *args, **kwargs):
        if self.name == "stray.txt":
            raise OSError(13, "Permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _refuse_stray)

    with wl.conductor_lock(CONDUCTOR):
        assert wl._remove_contents_locked(directory) == 1
    assert (directory / "conductor.json").exists()


def test_purge_conductor_keeps_the_breadcrumb_until_the_header_is_gone(monkeypatch):
    """A header unlink that fails must leave a store that still NAMES itself, so a
    later purge can still be aimed at it. Deleting the breadcrumb first would
    strand the ledger: header present, no key, no primitive that can reach it."""
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))
    original = Path.unlink

    def _refuse_header(self, *args, **kwargs):
        if self.name == "conductor.json":
            raise PermissionError(32, "sharing violation")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _refuse_header)
    latest = wl._newest_activity(directory, wl.census_items(directory))
    assert latest is not None
    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0)) is False

    assert (directory / "conductor.json").exists()
    assert (directory / "slot_key").exists(), "the store must still name itself"
    assert not (directory / "items" / f"{item_id}.json").exists(), "items went first"


@pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="the detached-inode shape needs POSIX unlink semantics"
)
def test_a_writer_queued_behind_a_purge_refuses_instead_of_publishing():
    """The lock-inode residual, closed: a writer that acquires a lock whose store
    was purged while it waited finds the inode it holds is not the one at
    the path, and refuses. Simulated by acquiring the lock, removing the store
    underneath, and then running the post-acquire check the lock takes."""
    wl.ensure_conductor(CONDUCTOR, goal="g")
    directory = wl.conductor_dir(CONDUCTOR)
    lock_path = directory / ".lock"
    import shutil

    from kiro_crew import session_ledger as sl

    with open(lock_path, "r+") as handle:
        # The purge runs to completion "while this writer waits": the store and
        # the lock inode it holds are gone from the path.
        shutil.rmtree(directory)
        with pytest.raises(OSError, match="removed while waiting"):
            sl.require_lock_inode(handle.fileno(), lock_path)
        # A fresh store at the path is a DIFFERENT inode: still refused.
        wl.ensure_conductor(CONDUCTOR, goal="new life")
        with pytest.raises(OSError, match="replaced while waiting"):
            sl.require_lock_inode(handle.fileno(), lock_path)
    # And the legitimate case: the inode held is the one at the path.
    with open(lock_path, "r+") as handle:
        sl.require_lock_inode(handle.fileno(), lock_path)


def test_every_store_lock_runs_the_inode_check_after_acquiring(monkeypatch):
    """The check is wired into ``_open_lock`` itself, so no store lock -- conductor,
    item or binding -- can be taken without it."""
    from kiro_crew import session_ledger as sl

    seen: list[str] = []
    real = sl.require_lock_inode

    def _spy(fd, path):
        seen.append(Path(path).name)
        return real(fd, path)

    monkeypatch.setattr(wl, "require_lock_inode", _spy)
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER)

    assert ".lock" in seen, "conductor lock"
    assert f"{item_id}.lock" in seen, "item lock"
    assert f"{wl._store_name(WORKER)}.lock" in seen, "binding lock"


@pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="flock-based holder simulation is POSIX-only"
)
def test_purge_conductor_refuses_while_a_worker_holds_an_item_lock(monkeypatch):
    """A file being written reads as unreadable to the census, so a census alone
    cannot tell "damaged" from "being written". The item lock can: a live writer
    holds it for its whole read-modify-write, and the purge takes every item lock
    non-blocking before deciding. Held lock -> refusal, even with allow_unreadable."""
    import os

    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    # Torn on disk, as a mid-write record looks, AND its lock held by "the writer".
    wl.item_path(CONDUCTOR, item_id).write_text("{mid-wr", encoding="utf-8")
    lock_path = wl._item_lock_path(CONDUCTOR, item_id)
    lock_path.touch()
    _pin_purge_clock(monkeypatch, wl.conductor_dir(CONDUCTOR), age=timedelta(seconds=1))
    holder = os.open(str(lock_path), os.O_RDWR)
    try:
        assert platform_compat.try_acquire_lock(holder, exclusive=True)
        with pytest.raises(wl.WorkLedgerError) as caught:
            wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=timedelta(0))
        assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
        assert "being written" in str(caught.value)
        assert wl.item_path(CONDUCTOR, item_id).exists(), "the item must survive"
        platform_compat.release_lock(holder)
    finally:
        os.close(holder)
    # Writer gone: the same torn record is now genuinely damage, and the flag clears it.
    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=timedelta(0)) is True


def test_purge_conductor_holds_every_item_lock_through_the_removal(monkeypatch):
    """Not just checked and dropped: the locks stay held while the census runs and
    the files go, so a writer cannot slip in between the two."""
    ids = [_new_item()]
    ids.append(
        wl.apply_conductor_action(
            CONDUCTOR, "create", title="second", acceptance={"kind": "human_approval"}
        )["item"].item_id
    )
    for item_id in ids:
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))

    held_during_removal: list[bool] = []
    real_remove = wl._remove_contents_locked

    def _probe(dir_path):
        # While removal runs, every item lock must be unavailable to a newcomer.
        import os

        for item_id in ids:
            fd = os.open(str(wl._item_lock_path(CONDUCTOR, item_id)), os.O_RDWR)
            try:
                held_during_removal.append(not platform_compat.try_acquire_lock(fd, exclusive=True))
            finally:
                os.close(fd)
        return real_remove(dir_path)

    monkeypatch.setattr(wl, "_remove_contents_locked", _probe)
    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0)) is True
    assert held_during_removal == [True, True], "both item locks held through removal"
    assert not directory.exists()


def test_purge_never_unlinks_a_lock_file_while_its_handle_is_held(monkeypatch):
    """Windows: a handle has no FILE_SHARE_DELETE, so unlinking a held lock raises.
    Every lock the purge holds -- the conductor's and each item's -- must therefore
    be unlinked only after its handle is closed, or the directory stays non-empty
    and the purge reports False over a store it already emptied. Simulated portably
    by tracking BOTH ways a lock handle can be opened -- ``os.open``, which
    ``open_lock_file`` uses, and the module's ``open`` -- and making every
    lock-file unlink raise while its handle is open. Tracking one path alone would
    leave a lock opened the other way invisible here, and a shell that ran inside
    the conductor hold would pass here and fail on the Windows runners."""
    import builtins
    import os

    ids = [_new_item()]
    ids.append(
        wl.apply_conductor_action(
            CONDUCTOR, "create", title="second", acceptance={"kind": "human_approval"}
        )["item"].item_id
    )
    for item_id in ids:
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))

    open_locks: set[Path] = set()
    real_os_open = os.open
    real_close = os.close
    real_open = builtins.open
    fd_paths: dict[int, Path] = {}

    def _tracking_os_open(path, flags, *args, **kwargs):
        fd = real_os_open(path, flags, *args, **kwargs)
        p = Path(path)
        if p.name.endswith(".lock"):
            fd_paths[fd] = p
            open_locks.add(p.resolve())
        return fd

    def _tracking_close(fd):
        p = fd_paths.pop(fd, None)
        if p is not None:
            open_locks.discard(p.resolve())
        return real_close(fd)

    class _TrackedHandle:
        """A file object's ``fileno()`` and ``with`` protocol, tracked while open."""

        def __init__(self, handle, path: Path):
            self._handle, self._path = handle, path
            open_locks.add(path.resolve())

        def fileno(self):
            return self._handle.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            open_locks.discard(self._path.resolve())
            self._handle.close()

    def _tracking_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        p = Path(path)
        return _TrackedHandle(handle, p) if p.name.endswith(".lock") else handle

    real_unlink = Path.unlink

    def _windows_unlink(self, *args, **kwargs):
        if self.name.endswith(".lock") and self.resolve() in open_locks:
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(os, "open", _tracking_os_open)
    monkeypatch.setattr(os, "close", _tracking_close)
    monkeypatch.setattr(wl, "open", _tracking_open, raising=False)
    monkeypatch.setattr(Path, "unlink", _windows_unlink)

    assert (
        wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0)) is True
    ), "the purge must succeed under Windows unlink rules"
    assert not directory.exists()
    assert not open_locks, "every lock handle was closed"


def test_goal_refuses_when_the_header_is_gone():
    """A ``goal`` that waited behind a purge must not rewrite the stale pre-lock
    header into the removed store -- that would resurrect a header with no
    breadcrumb, which no later purge could name."""
    record = wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    directory = wl.conductor_dir(CONDUCTOR)
    (directory / "conductor.json").unlink()

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl._write_goal(CONDUCTOR, record, "new goal", None)

    assert caught.value.code == wl.CODE_NO_LEDGER
    assert not (directory / "conductor.json").exists(), "nothing resurrected"


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="in-hold unlink is the POSIX path")
def test_every_lock_inode_is_unlinked_inside_the_holds(monkeypatch):
    """Conductor lock and every item lock are unlinked while still held, so a
    writer queued on any of them acquires a detached inode and refuses; nothing is
    handed a second inode while a first is held."""
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    _pin_purge_clock(monkeypatch, directory, age=timedelta(seconds=1))
    conductor_lock_path = directory / ".lock"
    item_lock_path = wl._item_lock_path(CONDUCTOR, item_id)
    assert item_lock_path.exists()

    seen: list[tuple[bool, bool]] = []
    real_release = wl.release_lock

    def _observe(fd):
        seen.append((conductor_lock_path.exists(), item_lock_path.exists()))
        return real_release(fd)

    monkeypatch.setattr(wl, "release_lock", _observe)

    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0)) is True
    # ``release_lock`` fires for the item hold(s) via _hold_every_item_lock; the
    # conductor lock is released by file_lock's own exit, so what we can observe
    # is that by the time the FIRST release happens, both lock paths are gone.
    assert seen and seen[0] == (False, False), seen
    assert not directory.exists()


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="in-hold unlink is the POSIX path")
def test_the_post_release_shell_never_touches_a_lock_the_hold_already_removed():
    """Same property as the session half: a lock path unlinked inside the hold is
    never unlinked again after release, because a refused writer may have rebuilt
    the store with a fresh inode there by then."""
    import os

    item_id = _new_item()
    directory = wl.conductor_dir(CONDUCTOR)
    conductor_lock = directory / ".lock"
    item_lock = wl._item_lock_path(CONDUCTOR, item_id)
    item_lock.touch()
    # The hold removed both; a writer then rebuilt fresh locks at both paths.
    conductor_lock.unlink()
    conductor_lock.touch()
    item_lock.unlink()
    item_lock.touch()
    fresh = (os.stat(conductor_lock).st_ino, os.stat(item_lock).st_ino)

    wl._remove_lock_shell(directory, conductor_lock_gone=True, item_locks_left=[])

    assert conductor_lock.exists() and item_lock.exists()
    assert (os.stat(conductor_lock).st_ino, os.stat(item_lock).st_ino) == fresh
    # The Windows-shaped case: the hold could not unlink, so the shell may.
    (directory / "items" / f"{item_id}.json").unlink()
    (directory / "items" / f"{item_id}.jsonl").unlink()
    (directory / "conductor.json").unlink()
    (directory / "slot_key").unlink()
    wl._remove_lock_shell(directory, conductor_lock_gone=False, item_locks_left=[item_lock])
    assert not directory.exists()


def test_census_tolerates_a_naive_closed_at_beside_an_aware_one():
    """A naive stamp beside an offset-bearing one must not make the ``>`` raise
    TypeError out of ``census_items`` -- which ``scan()`` promises never raises,
    and which ``purge_conductor`` runs under the lock mid-``--purge``."""
    first = _new_item()
    second = wl.apply_conductor_action(
        CONDUCTOR, "create", title="second", acceptance={"kind": "human_approval"}
    )["item"].item_id
    for item_id in (first, second):
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    stamps = {first: "2026-01-10T09:00:00", second: "2026-01-10T09:00:00+00:00"}
    for item_id, stamp in stamps.items():
        path = wl.item_path(CONDUCTOR, item_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["closed_at"] = stamp
        path.write_text(json.dumps(record), encoding="utf-8")

    census = wl.census_items(wl.conductor_dir(CONDUCTOR))

    assert census.closed == 2
    assert census.newest_closed_at in stamps.values()


def test_a_purge_racing_a_finished_purge_recreates_nothing(monkeypatch):
    """Two sweeps scan the same store; the first deletes it; the second reaches its
    lock. A lock that CREATES would rebuild the directory -- a lock-only,
    breadcrumb-less store no later purge can name -- and only then find nothing to
    guard. The purge's lock must refuse instead."""
    import shutil

    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    directory = wl.conductor_dir(CONDUCTOR)
    real_lock = wl.conductor_lock

    @contextlib.contextmanager
    def _first_sweep_wins(slot_key, **kwargs):
        shutil.rmtree(directory)  # the other sweep finished just before our acquire
        with real_lock(slot_key, **kwargs):
            yield

    monkeypatch.setattr(wl, "conductor_lock", _first_sweep_wins)

    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0)) is False
    assert not directory.exists(), "the purge must not recreate the store it found gone"


def test_a_writer_lock_still_creates_the_store():
    """The non-creating form is the purge's alone: a writer bringing a conductor
    into being by locking it is the normal path and must keep working."""
    directory = wl.conductor_dir("chat-70-fresh")
    assert not directory.exists()
    with wl.conductor_lock("chat-70-fresh"):
        assert (directory / ".lock").exists()
