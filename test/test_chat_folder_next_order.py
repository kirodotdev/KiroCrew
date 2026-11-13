"""A new chat folder sorts after every folder already in the store.

Deletes leave gaps in the stored ``order`` values, so the store length is not
the next free position: with ``A`` and ``B`` deleted out of ``A, B, C, D`` the
length is 2, which is ``C``'s order, and a folder created with it ties ``C``
and sorts between the survivors by name. Each writer that appends a folder row
is driven here against that post-delete store.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import arrival_folders, channel_folders, mate_folders
from kiro_crew.dashboard.chat_folders_records import create_folder_record

# The two survivors of deleting the first two of four folders ordered 0..3.
_SURVIVORS = (("C", 2), ("D", 3))


def _row(name: str, order: Any) -> dict[str, Any]:
    return {
        "id": f"id-{name}",
        "name": name,
        "order": order,
        "collapsed": False,
        "hidden": False,
        "parent_id": "",
        "project_dir": "",
    }


@pytest.fixture(name="state")
def state_fixture(tmp_path: Any) -> Any:
    state = _make_state(tmp_path)

    def _seed(folders: list[dict[str, Any]]) -> tuple[bool, None]:
        folders.extend(_row(name, order) for name, order in _SURVIVORS)
        return True, None

    asyncio.run(state.mutate_folders(_seed))
    return state


def _assert_new_rows_sort_last(state: Any, new_ids: list[str]) -> None:
    survivor_max = max(order for _, order in _SURVIVORS)
    by_id = {str(f["id"]): f for f in state._folders}
    assert new_ids and all(new_ids)
    for fid in new_ids:
        assert by_id[fid]["order"] > survivor_max, by_id[fid]


def test_first_folder_in_an_empty_store_gets_order_zero(tmp_path: Any) -> None:
    state = _make_state(tmp_path)
    folder = asyncio.run(create_folder_record(state, name="E"))
    assert folder["order"] == 0


def test_orders_are_read_the_way_the_sidebar_reads_them(tmp_path: Any) -> None:
    """Missing, boolean and non-numeric orders sort as 0 in the sidebar, and a
    float truncates, so the highest position these rows hold is 1."""
    state = _make_state(tmp_path)
    rows = [_row("a", 0), _row("b", True), _row("c", "9"), _row("d", 1.9)]
    del rows[0]["order"]

    def _seed(folders: list[dict[str, Any]]) -> tuple[bool, None]:
        folders.extend(rows)
        return True, None

    asyncio.run(state.mutate_folders(_seed))
    folder = asyncio.run(create_folder_record(state, name="E"))
    assert folder["order"] == 2


def test_folder_api_create_sorts_after_survivors(state: Any) -> None:
    folder = asyncio.run(create_folder_record(state, name="E"))
    _assert_new_rows_sort_last(state, [str(folder["id"])])


def test_mate_home_sorts_after_survivors(state: Any) -> None:
    fid = asyncio.run(mate_folders.ensure_mate_home(state, "member:member-radar-0123", "Radar"))
    _assert_new_rows_sort_last(state, [fid])


def test_channel_folder_sorts_after_survivors(state: Any) -> None:
    fid = asyncio.run(channel_folders.ensure_channel_folder(state, "discord", "Discord"))
    _assert_new_rows_sort_last(state, [fid])


def test_arrival_folders_sort_after_survivors(state: Any) -> None:
    filing = asyncio.run(arrival_folders.arrival_folder_id(state, origin="mac", request_app=""))
    created = [str(f["id"]) for f in state._folders if f["name"] in ("Imported", "from mac")]
    assert filing.folder_id in created
    assert len(created) == 2
    _assert_new_rows_sort_last(state, created)
