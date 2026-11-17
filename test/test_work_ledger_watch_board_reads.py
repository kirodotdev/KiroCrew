"""What one work-ledger watch tick reads from the store.

A watch ticks every patrol interval for as long as its conductor's goal runs, so
its read must not grow with closed items, and a tick over a board nothing wrote
since the last one must not read the store again. Neither saving may cost a wake:
a report written here or by another process still wakes on its tick, and a stall
is still judged on a board that was not re-read.
"""

from __future__ import annotations

import json
import os
import time
import types

import pytest

from kiro_crew import work_ledger, work_ledger_store
from kiro_crew.probes import work_ledger as probe_module

CONDUCTOR = "chat-conductor"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


class _Job:
    id = "loop-1"


class _Ctx:
    message = json.dumps({"conductor": CONDUCTOR})
    job = _Job()


def _tick():
    probe = probe_module.WorkLedgerProbe()
    probe.identity(_Ctx())
    return probe.observe(_Ctx())


def _briefs(tick) -> str:
    return "\n".join(obs.brief for obs in tick.observations)


def _create(title: str) -> str:
    result = work_ledger.apply_conductor_action(
        CONDUCTOR, "create", title=title, acceptance={"kind": "human_approval"}
    )
    return str(result["item"].item_id)


def _close(item_id: str) -> None:
    work_ledger.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="pass")
    work_ledger.apply_conductor_action(
        CONDUCTOR, "close", item_id=item_id, state="accepted", decision="verified"
    )


def _board(*, closed: int = 0) -> str:
    """A ledger with one open item and *closed* accepted ones; the open item's id."""
    work_ledger.ensure_conductor(CONDUCTOR, goal="ship it")
    for n in range(closed):
        _close(_create(f"finished {n}"))
    return _create("still open")


def _store_dirs() -> list[str]:
    items = work_ledger.items_dir(CONDUCTOR)
    return [str(items.parent), str(items)]


def _age_store(stamps: list[int] | None = None) -> list[int]:
    """Set the store's directory mtimes a minute back (or to *stamps*); the stamps."""
    stamps = stamps or [time.time_ns() - 60 * 10**9] * 2
    for path, ns in zip(_store_dirs(), stamps):
        os.utime(path, ns=(ns, ns))
    return stamps


def _count_reads(monkeypatch) -> dict[str, list[str]]:
    """Record every store read a tick makes, by function."""
    seen: dict[str, list[str]] = {"read_conductor": [], "list_work_items": [], "read_events": []}
    for name in seen:
        real = getattr(work_ledger, name)

        def spy(key, *args, _real=real, _name=name, **kwargs):
            seen[_name].append(args[0] if args else key)
            return _real(key, *args, **kwargs)

        monkeypatch.setattr(work_ledger, name, spy)
    return seen


def test_a_tick_reads_no_event_tail_for_a_closed_item(monkeypatch):
    open_item = _board(closed=3)
    reads = _count_reads(monkeypatch)
    _tick()
    assert reads["read_events"] == [open_item]


def test_a_board_nothing_wrote_since_the_last_tick_is_not_read_again(monkeypatch):
    _board(closed=2)
    _age_store()
    first = _tick()
    reads = _count_reads(monkeypatch)
    second = _tick()
    assert reads == {"read_conductor": [], "list_work_items": [], "read_events": []}
    assert second.epoch == first.epoch


def test_a_stall_is_still_judged_on_a_board_that_was_not_read_again(monkeypatch):
    item = _board()
    _age_store()
    monkeypatch.setattr(work_ledger, "is_stale", lambda _item, **_kw: False)
    assert "reason=stall" not in _briefs(_tick())
    monkeypatch.setattr(work_ledger, "is_stale", lambda _item, **_kw: True)
    reads = _count_reads(monkeypatch)
    stalled = _briefs(_tick())
    assert reads["list_work_items"] == []
    assert f"item={item} reason=stall" in stalled


def test_a_report_written_in_this_process_wakes_on_the_next_tick_even_with_unmoved_mtimes():
    item = _board()
    stamps = _age_store()
    _tick()
    work_ledger.apply_worker_report(CONDUCTOR, item, status="blocked", summary="no creds")
    # Only the change mark says the store moved.
    _age_store(stamps)
    assert f"item={item} status=blocked" in _briefs(_tick())


def test_a_report_written_by_another_process_wakes_on_the_next_tick(monkeypatch):
    item = _board()
    _age_store()
    _tick()
    # Another process's write moves no change mark here; only the directory mtime.
    unmoved = work_ledger.change_mark(CONDUCTOR)
    monkeypatch.setattr(work_ledger, "change_mark", lambda _key: unmoved)
    work_ledger.apply_worker_report(CONDUCTOR, item, status="question", summary="which one?")
    assert f"item={item} status=question" in _briefs(_tick())


def test_an_unchanged_board_is_read_again_once_its_last_read_is_too_old(monkeypatch):
    _board()
    _age_store()
    _tick()
    later = time.time() + probe_module._REUSE_MAX_AGE_SECS + 1
    monkeypatch.setattr(probe_module, "time", types.SimpleNamespace(time=lambda: later))
    reads = _count_reads(monkeypatch)
    _tick()
    assert reads["list_work_items"] == [CONDUCTOR]


def test_a_board_written_within_the_mtime_race_window_is_not_kept(monkeypatch):
    _board()
    stamps = _age_store()
    # The tick starts 1 s after the store last changed, inside the race window.
    soon = max(stamps) / 1e9 + 1
    monkeypatch.setattr(probe_module, "time", types.SimpleNamespace(time=lambda: soon))
    _tick()
    reads = _count_reads(monkeypatch)
    _tick()
    assert reads["list_work_items"] == [CONDUCTOR]


def test_a_board_bigger_than_the_kept_item_budget_is_read_on_every_tick(monkeypatch):
    _board(closed=1)
    _age_store()
    monkeypatch.setattr(probe_module, "_KEPT_ITEMS", 1)
    _tick()
    reads = _count_reads(monkeypatch)
    _tick()
    assert reads["list_work_items"] == [CONDUCTOR]


def test_a_board_with_an_unreadable_item_file_is_read_on_every_tick(monkeypatch):
    torn = _board(closed=1)
    work_ledger.item_path(CONDUCTOR, torn).write_text("{ not json", encoding="utf-8")
    _age_store()
    _tick()
    reads = _count_reads(monkeypatch)
    _tick()
    assert reads["list_work_items"] == [CONDUCTOR]


def test_a_read_whose_event_tail_failed_is_retried_on_the_next_tick(monkeypatch):
    item = _board()
    _age_store()
    with monkeypatch.context() as failing:
        # ``read_events`` answers ``[]`` for an I/O error on the log.
        failing.setattr(work_ledger, "read_events", lambda *_a, **_kw: [])
        _tick()
    reads = _count_reads(monkeypatch)
    _tick()
    assert reads["read_events"] == [item]


def test_a_board_with_no_items_is_read_on_every_tick(monkeypatch):
    work_ledger.ensure_conductor(CONDUCTOR, goal="ship it")
    work_ledger.items_dir(CONDUCTOR).mkdir(parents=True, exist_ok=True)
    _age_store()
    _tick()
    reads = _count_reads(monkeypatch)
    _tick()
    assert reads["list_work_items"] == [CONDUCTOR]


def test_a_read_that_joined_a_listing_already_in_flight_is_not_kept(monkeypatch):
    item = _board()
    stale = work_ledger.list_work_items(CONDUCTOR)
    # The item closes while a listing begun before the close is still running.
    _close(item)
    _age_store()
    flight = work_ledger_store._Listing(work_ledger.change_mark(CONDUCTOR))
    flight.items = stale
    flight.done.set()
    with monkeypatch.context() as walking:
        walking.setitem(work_ledger._LISTINGS, str(work_ledger.items_dir(CONDUCTOR)), flight)
        _tick()
    reads = _count_reads(monkeypatch)
    _tick()
    assert reads["list_work_items"] == [CONDUCTOR]
