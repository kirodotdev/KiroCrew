"""``crew_mode.stamp_rows``: which rows are Crew Mode roots, and which a root holds.

Pure-function tests on slot payload dicts: ``tree_slots=()`` and ``parents={}`` keep
the walk off the crew log, and the remembered-roots set is swapped per test so no
test reads another's roots.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import crew_mode

ROOT = "root"
CHILD = "child"
PLAIN = "kirocrew-research"


@pytest.fixture(autouse=True)
def _fresh_known_roots(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())


def _row(key: str, agent: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": key,
        "agent": agent,
        "agent_kind": "template",
        "mode": "",
        "memory_mode": "persistent",
    }
    row.update(fields)
    return row


def _stamp(rows: list[dict[str, Any]], **kw: Any) -> dict[str, dict[str, Any]]:
    kw.setdefault("tree_slots", ())
    crew_mode.stamp_rows(rows, **kw)
    return {r["key"]: r for r in rows}


def test_a_reopened_child_on_its_conductors_template_is_that_conductors_worker() -> None:
    """A child created without an agent inherits the conductor template. Reopened
    from History, only its saved metadata line names its creator: it is still the
    conductor's worker, never a root of its own."""
    rows = _stamp(
        [
            _row(ROOT, crew_mode.DEFAULT_CREW_MODE_AGENT),
            _row(CHILD, crew_mode.DEFAULT_CREW_MODE_AGENT),
        ],
        saved_creators={CHILD: ROOT},
    )
    assert rows[ROOT][crew_mode.ROOT_FIELD] is True
    assert rows[CHILD][crew_mode.ROOT_FIELD] is False
    assert rows[CHILD][crew_mode.WORKER_FIELD] == ROOT
    assert crew_mode.known_roots() == {ROOT}


def test_a_root_switched_to_a_plain_agent_is_forgotten_and_its_close_frees_its_sessions() -> None:
    """Switching an idle root off every conductor ends its Crew Mode; closing it
    afterwards must not stamp the sessions it created back into Crew Mode."""
    worker = _row("w", "kirocrew-worker", created_by=ROOT)
    rows = _stamp([_row(ROOT, crew_mode.DEFAULT_CREW_MODE_AGENT), dict(worker)])
    assert rows["w"][crew_mode.WORKER_FIELD] == ROOT
    assert crew_mode.known_roots() == {ROOT}

    rows = _stamp([_row(ROOT, PLAIN), dict(worker)])
    assert rows[ROOT][crew_mode.ROOT_FIELD] is False
    assert crew_mode.WORKER_FIELD not in rows["w"]
    assert crew_mode.known_roots() == frozenset()

    rows = _stamp([dict(worker)], parents={})
    assert crew_mode.WORKER_FIELD not in rows["w"]


def test_a_root_that_closes_while_a_root_still_holds_its_workers() -> None:
    """The remembered set shrinks only for a LIVE row that stopped being a root: a
    root whose tab closes stays known, so its workers stay out of the person's list."""
    worker = _row("w", "kirocrew-worker", created_by=ROOT)
    _stamp([_row(ROOT, crew_mode.DEFAULT_CREW_MODE_AGENT), dict(worker)])
    rows = _stamp([dict(worker)], parents={})
    assert rows["w"][crew_mode.WORKER_FIELD] == ROOT
    assert crew_mode.known_roots() == {ROOT}
