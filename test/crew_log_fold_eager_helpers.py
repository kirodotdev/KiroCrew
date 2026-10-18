"""Shared pieces for ``test_crew_log_fold_eager.py`` and ``test_crew_log_fold_eager_memory.py``.

The autouse ``_isolated_home`` gives every test its own data home and stops the eager
worker and clears the slot folds and emitter caches around it. ``GATEWAY`` is the entry
source, ``WORK_SLOT`` the board key the work entries name, ``_slot_log`` and
``_work_entry`` write a slot unit's work entries, and ``_memo_keys`` lists the warm memos
held.
"""

from __future__ import annotations

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, eager, emit
from kiro_crew.crew_log import projection as crew_log

GATEWAY = "gateway"

#: The board key the work entries name. A ``work/recorded`` entry carries the slot it
#: belongs to, and the fold joins on THAT rather than on the unit's header, so the two
#: must agree for the eager fold to land where a reader would look.
WORK_SLOT = "dashboard:9"


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse home pin moved verbatim; the root conftest floor pins KIROCREW_HOME independently
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    emit.reset_caches()
    yield
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    # Drop the leased CrewLog handle the emitter cached in ``emit._open`` for the
    # emitter-backed cases: its store lease holds a descriptor released only when the
    # handle is dropped, and with ``tmp_path_retention_policy = failed`` pytest removes a
    # passing case's ``tmp_path`` at teardown. On Windows a live descriptor makes that
    # removal fail silently (errors ignored), leaving host state -- a cached handle
    # pointing at a removed home -- for the rest of the worker. Clearing the cache here
    # mirrors the setup half above, the same pairing the rest of the suite uses.
    emit.reset_caches()


def _slot_log(unit_id: str, slot: str) -> CrewLog:
    """A session log whose HEADER names *slot*, which is what the slot read joins on."""
    return CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)


def _work_entry(handle: CrewLog, item_id: str, slot: str = WORK_SLOT) -> int:
    """One ``work/recorded`` entry naming *slot*'s board, returning its seq."""
    entry = handle.append(
        "work/recorded",
        {
            "slot": slot,
            "actor": "conductor",
            "by": slot,
            "action": "create",
            "item_id": item_id,
            "title": "a work item",
        },
        src=GATEWAY,
    )
    return int(entry.seq)


def _memo_keys() -> set[tuple[str, str]]:
    """The (slot, fold) pairs currently held warm, whatever data home they are under."""
    return {(key[1], key[2]) for key in crew_log._slot_memos}
