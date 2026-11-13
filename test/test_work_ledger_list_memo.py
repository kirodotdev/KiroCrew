"""Conductor work ledger store: listing a board parses only the item files that changed.

The Crew worker list and the board list every item of a root on each read, and a root
keeps its whole history. A listing keeps each parsed item beside the stat it was read
under and parses again only a file whose stat moved; listings of one board that overlap
share one walk. Every caller still gets its own objects.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, WORKER, _new_item

from kiro_crew import work_ledger as wl
from kiro_crew import work_ledger_store as store
from kiro_crew.testing.wait import wait_until

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_home = work_ledger_helpers._isolated_home


class _CountingJson:
    """``json`` for the store module, counting ``loads``; *gate* holds the first one."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.loads_calls = 0
        self.gate = gate
        self._lock = threading.Lock()

    def loads(self, data: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self.loads_calls += 1
            first = self.loads_calls == 1
        if first and self.gate is not None:
            assert self.gate.wait(10)
        return json.loads(data, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(json, name)


def _board(count: int) -> list[str]:
    ids = [_new_item(title=f"item {n}") for n in range(count)]
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=ids[0], worker_session_key=WORKER)
    return ids


def _dicts(items: list[wl.WorkItem]) -> list[dict[str, Any]]:
    return [item.to_dict() for item in items]


def test_a_second_listing_of_an_unchanged_board_parses_nothing(monkeypatch):
    _board(5)
    first = wl.list_work_items(CONDUCTOR)
    counting = _CountingJson()
    monkeypatch.setattr(wl, "json", counting)
    again = wl.list_work_items(CONDUCTOR)
    assert counting.loads_calls == 0
    assert _dicts(again) == _dicts(first)


def test_a_changed_item_file_is_parsed_again_and_only_it(monkeypatch):
    ids = _board(5)
    wl.list_work_items(CONDUCTOR)
    wl.apply_worker_report(CONDUCTOR, ids[0], status="progress", summary="half way", artifacts={})
    counting = _CountingJson()
    monkeypatch.setattr(wl, "json", counting)
    items = {item.item_id: item for item in wl.list_work_items(CONDUCTOR)}
    assert items[ids[0]].summary == "half way"
    assert counting.loads_calls == 1


def test_a_caller_mutating_a_listed_item_does_not_change_the_next_listing():
    item_id = _new_item(title="first title", acceptance={"kind": "pr_checks", "nested": {"a": [1]}})
    first = wl.list_work_items(CONDUCTOR)[0]
    first.title = "changed by a caller"
    first.acceptance["nested"]["a"].append(2)
    first.artifacts["planted"] = "x"
    again = wl.list_work_items(CONDUCTOR)[0]
    assert again.item_id == item_id
    assert again.title == "first title"
    assert again.acceptance["nested"] == {"a": [1]}
    assert "planted" not in again.artifacts


def test_a_removed_item_file_leaves_the_listing():
    ids = _board(3)
    listed = [item.item_id for item in wl.list_work_items(CONDUCTOR)]
    wl.item_path(CONDUCTOR, ids[1]).unlink()
    assert [item.item_id for item in wl.list_work_items(CONDUCTOR)] == [
        item_id for item_id in listed if item_id != ids[1]
    ]


def test_a_board_over_the_item_budget_is_not_kept_and_still_lists_whole(monkeypatch):
    monkeypatch.setattr(wl, "_LISTING_MEMO_ITEMS", 3)
    ids = _board(5)
    store._LISTING_MEMO.clear()
    listed = wl.list_work_items(CONDUCTOR)
    assert sorted(item.item_id for item in listed) == sorted(ids)
    assert str(wl.items_dir(CONDUCTOR)) not in store._LISTING_MEMO


def test_the_kept_items_never_exceed_the_budget_oldest_board_out_first(monkeypatch):
    monkeypatch.setattr(wl, "_LISTING_MEMO_ITEMS", 3)
    other = "chat-9-other"
    _board(2)
    wl.ensure_conductor(other, goal="another board")
    for n in range(2):
        wl.apply_conductor_action(
            other, "create", title=f"o{n}", acceptance={"kind": "human_approval"}
        )
    store._LISTING_MEMO.clear()
    wl.list_work_items(CONDUCTOR)
    wl.list_work_items(other)
    assert sum(len(kept) for kept in store._LISTING_MEMO.values()) <= 3
    assert list(store._LISTING_MEMO) == [str(wl.items_dir(other))]
    assert len(wl.list_work_items(CONDUCTOR)) == 2


def _list_together(
    threads: int,
) -> tuple[list[Any], list[BaseException], list[threading.Thread]]:
    results: list[Any] = []
    errors: list[BaseException] = []
    start = threading.Barrier(threads)

    def run() -> None:
        start.wait(10)
        try:
            results.append(wl.list_work_items(CONDUCTOR))
        except BaseException as exc:  # noqa: BLE001 - the test inspects what each got
            errors.append(exc)

    workers = [threading.Thread(target=run) for _ in range(threads)]
    for worker in workers:
        worker.start()
    return results, errors, workers


def _until_joined(count: int) -> None:
    """Return once the board's walk in flight has *count* listings waiting on it."""

    def joined() -> bool:
        with store._LISTINGS_LOCK:
            flight = next(iter(store._LISTINGS.values()), None)
            return flight is not None and flight.joined >= count

    wait_until(joined, describe=lambda: f"fewer than {count} listings joined the walk")


def test_six_overlapping_listings_of_one_board_walk_it_once(monkeypatch):
    _board(4)
    store._LISTING_MEMO.clear()
    gate = threading.Event()
    counting = _CountingJson(gate)
    monkeypatch.setattr(wl, "json", counting)
    results, errors, workers = _list_together(6)
    # The first parse is held until every other listing has found the walk.
    _until_joined(5)
    gate.set()
    for worker in workers:
        worker.join(10)
    assert errors == []
    assert counting.loads_calls == 4
    assert len(results) == 6
    assert all(_dicts(r) == _dicts(results[0]) for r in results)
    # Each listing has its own objects.
    assert len({id(r[0]) for r in results}) == 6


def test_an_error_in_the_shared_walk_reaches_every_listing(monkeypatch):
    _board(2)
    store._LISTING_MEMO.clear()
    gate = threading.Event()

    class _Boom(_CountingJson):
        def loads(self, data: Any, *args: Any, **kwargs: Any) -> Any:
            super().loads(data, *args, **kwargs)
            raise RuntimeError("boom")

    monkeypatch.setattr(wl, "json", _Boom(gate))
    results, errors, workers = _list_together(3)
    _until_joined(2)
    gate.set()
    for worker in workers:
        worker.join(10)
    assert results == []
    assert len(errors) == 3 and all(isinstance(e, RuntimeError) for e in errors)
    assert store._LISTINGS == {}


def test_a_listing_started_after_a_write_never_joins_a_walk_from_before_it(monkeypatch):
    ids = _board(2)
    store._LISTING_MEMO.clear()
    gate = threading.Event()
    monkeypatch.setattr(wl, "json", _CountingJson(gate))
    early: list[Any] = []
    leader = threading.Thread(target=lambda: early.append(wl.list_work_items(CONDUCTOR)))
    leader.start()
    _until_joined(0)
    monkeypatch.setattr(wl, "json", json)
    wl.apply_worker_report(CONDUCTOR, ids[0], status="progress", summary="after", artifacts={})
    late = {item.item_id: item for item in wl.list_work_items(CONDUCTOR)}
    gate.set()
    leader.join(10)
    assert late[ids[0]].summary == "after"
    assert early and len(early[0]) == 2
