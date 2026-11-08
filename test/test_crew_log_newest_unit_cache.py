"""The relay's newest-unit lookup and the slot index read each unit's head once.

Both run on the crew-log writer thread for every worker charge relayed into a board, and
each file a lookup opens there releases and re-takes the GIL. Under load a lookup that
re-read every unit of a busy board, or every header under the store root, held the writer
for over 30 s. These tests pin that a settled answer is served without reading the units
again, and that a unit whose opening entry has not landed is still never cached.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from crew_log_type_helpers import minimal_data

from kiro_crew.crew_log import projection, projection_readers, store, store_units
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.store import CrewLog

BOARD = "chat-board"


def _unit(unit_id: str, *, slot: str = BOARD, opened: bool = True) -> None:
    handle = CrewLog.create(KIND_SESSION, unit_id, owner="o", agent="kirocrew", slot=slot)
    if opened:
        data = {**minimal_data(KIND_SESSION, "session/opened"), "slot": slot, "resumed": False}
        handle.append("session/opened", data, src="gateway")
    handle.release_ownership()


@pytest.fixture
def head_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every unit-head read the lookups make: header proofs and opening-entry reads."""
    seen: list[str] = []
    real_proved = store_units._proved_header
    real_head = store_units.read_head

    def proved(directory: Path) -> "dict[str, Any] | None":
        seen.append(f"header:{directory.name}")
        return real_proved(directory)

    def head(path: Path) -> Any:
        seen.append(f"head:{path.parent.name}")
        return real_head(path)

    # Through the facade: it carries the patch to every module of the family that binds it.
    monkeypatch.setattr(store, "_proved_header", proved)
    monkeypatch.setattr(store, "read_head", head)
    monkeypatch.setattr(store, "_slot_index", None)
    store_units._SLOT_INDEX_TRUST[0] = None
    memo = getattr(store_units, "_PROVED_ROWS", None)
    if memo is not None:
        memo[0] = None
    projection_readers._NEWEST_UNITS.clear()
    yield seen
    projection_readers._NEWEST_UNITS.clear()


def test_a_board_of_chain_starts_is_read_once(head_reads: list[str]) -> None:
    """A conductor restarted many times leaves many units with no predecessor link."""
    for index in range(6):
        _unit(f"acp-start-{index}")
    newest = projection_readers.newest_unit_in_succession(BOARD)
    assert newest.startswith("acp-start-")
    head_reads.clear()
    for _ in range(20):
        assert projection_readers.newest_unit_in_succession(BOARD) == newest
    assert [read for read in head_reads if read.startswith("head:")] == []


def test_a_unit_whose_opening_entry_has_not_landed_is_not_cached(head_reads: list[str]) -> None:
    _unit("acp-landed")
    _unit("acp-pending", opened=False)
    projection_readers.newest_unit_in_succession(BOARD)
    assert BOARD not in projection_readers._NEWEST_UNITS


def test_an_order_built_on_a_failed_header_read_is_not_cached(
    head_reads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A header that will not read sorts its unit on a default; that guess is not kept."""
    _unit("acp-first")
    _unit("acp-second")
    real = projection_readers.unit_header_created_at

    def flaky(kind: str, unit_id: str) -> "int | None":
        return None if unit_id == "acp-second" else real(kind, unit_id)

    monkeypatch.setattr(projection, "unit_header_created_at", flaky)
    projection_readers.newest_unit_in_succession(BOARD)
    assert BOARD not in projection_readers._NEWEST_UNITS

    monkeypatch.setattr(projection, "unit_header_created_at", real)
    projection_readers.newest_unit_in_succession(BOARD)
    assert BOARD in projection_readers._NEWEST_UNITS


def test_a_rebuild_reads_only_the_headers_of_new_units(head_reads: list[str]) -> None:
    for index in range(5):
        _unit(f"acp-old-{index}", slot="chat-other")
    store.session_units_by_slot()
    head_reads.clear()
    _unit("acp-new", slot="chat-other")
    units = store.session_units_for_slot("chat-other")
    assert "acp-new" in units and len(units) == 6
    headers = [read for read in head_reads if read.startswith("header:")]
    assert len(headers) == 1 and headers[0].endswith(store._store_name("acp-new"))


def test_dropping_the_index_reads_every_header_again(head_reads: list[str]) -> None:
    for index in range(3):
        _unit(f"acp-fresh-{index}", slot="chat-other")
    store.session_units_by_slot()
    head_reads.clear()
    store_units._SLOT_INDEX_TRUST[0] = None
    store._slot_index = None  # type: ignore[attr-defined]
    store.session_units_by_slot()
    assert len([read for read in head_reads if read.startswith("header:")]) == 3
