"""Conductor work ledger store: a board read resolves the store path once, not per item.

Every item's event tail and every open item's undelivered record is a path inside the
store, and each one went through ``resolved_within`` (two full resolves, one ``lstat`` per
path component). The store keeps the containment answer and serves it while one
``lstat`` still finds the same non-link object, or the leaf is still absent under the
same parent directory. A link swapped in after the answer was kept must still be refused.
"""

from __future__ import annotations

import os
import shutil

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, WORKER, _new_item

from kiro_crew import work_ledger as wl

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_home = work_ledger_helpers._isolated_home

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the kept answer is POSIX only")


@pytest.fixture(autouse=True)
def _link_free_home(_isolated_home, tmp_path, _floor_monkeypatch):
    """A data home spelled without links (macOS spells its temp dir through one), the
    only kind whose answers are kept."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path.resolve() / "home"))


@pytest.fixture
def store_resolves(monkeypatch):
    """Every ``(base, name)`` that reached the full containment resolve, bindings aside."""
    calls: list[tuple[str, str]] = []
    real = wl.resolved_within

    def counting(base, name):
        if base != wl.bindings_dir():
            calls.append((str(base), name))
        return real(base, name)

    monkeypatch.setattr(wl, "resolved_within", counting)
    return calls


def _three_open_items_with_a_bound_worker() -> list[str]:
    item_ids = [_new_item(title=f"item {n}") for n in range(3)]
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_ids[0], worker_session_key=WORKER)
    wl.undelivered_dir().mkdir(parents=True, exist_ok=True)
    return item_ids


def test_repeated_board_reads_of_three_items_do_no_full_resolve_after_the_first(
    store_resolves,
):
    item_ids = _three_open_items_with_a_bound_worker()
    for item in wl.list_work_items(CONDUCTOR):
        wl.read_events(CONDUCTOR, item.item_id, limit=20)
        wl.read_undelivered(CONDUCTOR, item)
    store_resolves.clear()

    for _ in range(3):
        items = wl.list_work_items(CONDUCTOR)
        assert sorted(item.item_id for item in items) == sorted(item_ids)
        for item in items:
            assert wl.read_events(CONDUCTOR, item.item_id, limit=20)
            assert wl.read_undelivered(CONDUCTOR, item) is None

    assert store_resolves == []


def test_a_store_swapped_for_a_link_outside_the_root_after_a_kept_read_is_refused(tmp_path):
    item_id = _new_item()
    assert wl.read_events(CONDUCTOR, item_id)
    store = wl.conductor_dir(CONDUCTOR)

    outside = tmp_path / "outside"
    store.rename(outside)
    try:
        store.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks on this platform/account")

    with pytest.raises(wl.WorkLedgerError) as excinfo:
        wl.conductor_dir(CONDUCTOR)
    assert excinfo.value.code == wl.CODE_INVALID_VALUE
    with pytest.raises(wl.WorkLedgerError) as excinfo:
        wl.read_events(CONDUCTOR, item_id)
    assert excinfo.value.code == wl.CODE_INVALID_VALUE


def test_a_link_planted_at_an_absent_undelivered_record_after_a_kept_read_is_refused(tmp_path):
    item_id = _three_open_items_with_a_bound_worker()[0]
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert wl.read_undelivered(CONDUCTOR, item) is None
    record = wl.undelivered_path(WORKER)
    assert not record.exists()

    victim = tmp_path / "victim.json"
    victim.write_text("{}", encoding="utf-8")
    try:
        record.symlink_to(victim)
    except OSError:
        pytest.skip("cannot create symlinks on this platform/account")

    with pytest.raises(wl.WorkLedgerError) as excinfo:
        wl.undelivered_path(WORKER)
    assert excinfo.value.code == wl.CODE_INVALID_VALUE


def test_a_root_link_to_a_missing_directory_installed_after_an_absent_store_read_bootstraps(
    tmp_path,
):
    root = wl._work_ledger_root()
    root.mkdir(parents=True, exist_ok=True)
    assert not wl.conductor_dir(CONDUCTOR).exists()
    shutil.rmtree(root)
    target = tmp_path.resolve() / "not-yet-made"
    try:
        root.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks on this platform/account")

    item_id = _new_item(title="through the new root")

    assert wl.conductor_dir(CONDUCTOR).is_relative_to(target)
    assert wl.read_events(CONDUCTOR, item_id)


def test_a_store_removed_and_made_again_is_found_after_a_kept_read():
    item_id = _new_item()
    assert wl.read_events(CONDUCTOR, item_id)
    store = wl.conductor_dir(CONDUCTOR)
    shutil.rmtree(store)

    fresh = _new_item(title="after the removal")
    assert wl.conductor_dir(CONDUCTOR) == store
    assert [event.item_id for event in wl.read_events(CONDUCTOR, fresh)] == [fresh]


def test_a_work_ledger_root_link_repointed_at_a_replacement_after_a_read_reaches_it(tmp_path):
    first, second = tmp_path.resolve() / "first", tmp_path.resolve() / "second"
    first.mkdir()
    root = wl._work_ledger_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        root.symlink_to(first, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks on this platform/account")
    item_id = _new_item(title="on the first root")
    assert wl.read_events(CONDUCTOR, item_id)
    assert wl.conductor_dir(CONDUCTOR).is_relative_to(first)

    shutil.copytree(first, second)
    root.unlink()
    root.symlink_to(second, target_is_directory=True)

    assert wl.conductor_dir(CONDUCTOR).is_relative_to(second)
    assert wl.read_events(CONDUCTOR, item_id)
