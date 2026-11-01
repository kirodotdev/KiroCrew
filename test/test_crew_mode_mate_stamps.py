"""``crew_mode.stamp_rows``: the mate tree's ``mate_root`` / ``mate_dispatched_by`` stamps.

A mate (crewmate) thread is a root of its own kind, kept apart from Crew Mode
(``docs/request-for-change/rfc-crewmate-workers-under-the-crewmate.md``, Design 1).
Pure-function tests on slot payload dicts, plus one ``slot_patch`` test: the walk is
kept off the crew log, and both remembered-roots sets are swapped per test.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import crew_mode
from kiro_crew.dashboard.state import DashboardState, SlotOrigin
from kiro_crew.dashboard.websocket_hub import SLOT_PATCH_WS_FLAG
from kiro_crew.members_parts.member_space import DM_SLOT_MODE

MATE = "dm-ada"
CHILD = "chat-child"
GRAND = "chat-grand"
ROOT = "chat-root"
PLAIN = "kirocrew-research"
CREW_FIELDS = (crew_mode.ROOT_FIELD, crew_mode.WORKER_FIELD, crew_mode.DISPATCHER_FIELD)
MATE_FIELDS = (crew_mode.MATE_ROOT_FIELD, crew_mode.MATE_DISPATCHER_FIELD)


@pytest.fixture(autouse=True)
def _fresh_known_roots(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "_known_mate_roots", set())


def _row(key: str, agent: str = PLAIN, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": key,
        "agent": agent,
        "agent_kind": "template",
        "mode": "",
        "memory_mode": "persistent",
    }
    row.update(fields)
    return row


def _mate(key: str = MATE) -> dict[str, Any]:
    return _row(key, "ada", agent_kind="member", mode=DM_SLOT_MODE)


def _stamp(rows: list[dict[str, Any]], **kw: Any) -> dict[str, dict[str, Any]]:
    kw.setdefault("tree_slots", ())
    kw.setdefault("parents", {})
    crew_mode.stamp_rows(rows, **kw)
    return {r["key"]: r for r in rows}


def _mate_of(row: dict[str, Any]) -> tuple[str, str]:
    return crew_mode.mate_stamps_of(row)


def test_the_mate_slot_mode_is_the_dm_slot_mode() -> None:
    assert crew_mode.MATE_SLOT_MODE == DM_SLOT_MODE


def test_a_mate_thread_is_a_root_and_holds_its_child_and_grandchild() -> None:
    rows = _stamp(
        [
            _mate(),
            _row(CHILD, created_by=MATE),
            _row(GRAND, "kirocrew-worker", created_by=CHILD),
        ]
    )
    assert crew_mode.known_mate_roots() == {MATE}
    assert not any(f in rows[MATE] for f in MATE_FIELDS)
    assert _mate_of(rows[CHILD]) == (MATE, MATE)
    assert _mate_of(rows[GRAND]) == (MATE, CHILD)
    for row in rows.values():
        assert row[crew_mode.ROOT_FIELD] is False
        assert crew_mode.WORKER_FIELD not in row


def test_a_conductor_a_mate_opened_is_a_mate_descendant_with_its_workers() -> None:
    """A conductor with a creator is no Crew Mode root, so its workers walk on to the mate."""
    rows = _stamp(
        [
            _mate(),
            _row(CHILD, crew_mode.DEFAULT_CREW_MODE_AGENT, created_by=MATE),
            _row(GRAND, "kirocrew-worker", created_by=CHILD),
        ]
    )
    assert _mate_of(rows[CHILD]) == (MATE, MATE)
    assert _mate_of(rows[GRAND]) == (MATE, CHILD)
    assert crew_mode.known_roots() == frozenset()


def test_a_closed_intermediate_creator_still_resolves_to_the_mate() -> None:
    """Past a closed creator the walk follows the persisted lineage, then the live ancestor."""
    rows = _stamp([_mate(), _row(GRAND, created_by=CHILD)], parents={CHILD: MATE})
    assert _mate_of(rows[GRAND]) == (MATE, CHILD)

    rows = _stamp([_mate(), _row(GRAND, parent={"slot": CHILD, "key": MATE, "ancestor": True})])
    assert _mate_of(rows[GRAND]) == (MATE, CHILD)


def test_a_closed_mate_thread_still_holds_its_sessions() -> None:
    _stamp([_mate(), _row(CHILD, created_by=MATE)])
    rows = _stamp([_row(CHILD, created_by=MATE)])
    assert _mate_of(rows[CHILD]) == (MATE, MATE)


def test_ordinary_sessions_get_no_mate_stamp() -> None:
    rows = _stamp([_row("a"), _row("b", created_by="a"), _row("c", created_by="gone")])
    for row in rows.values():
        assert not any(f in row for f in MATE_FIELDS)
    assert crew_mode.known_mate_roots() == frozenset()


def test_a_stale_mate_stamp_is_cleared_when_the_row_leaves_the_tree() -> None:
    row = _row(CHILD, created_by="other", mate_root=MATE, mate_dispatched_by=MATE)
    rows = _stamp([_mate(), row])
    assert not any(f in rows[CHILD] for f in MATE_FIELDS)


def _crew_fixture() -> list[dict[str, Any]]:
    """A Crew Mode tree, a mate tree and plain rows side by side."""
    return [
        _row(ROOT, crew_mode.DEFAULT_CREW_MODE_AGENT),
        _row("w1", "kirocrew-worker", created_by=ROOT),
        _row("w2", "kirocrew-worker", created_by="w1"),
        _row("w3", created_by="closed-w", parent={"slot": "closed-w", "key": ROOT}),
        _mate(),
        _row(CHILD, created_by=MATE),
        _row(GRAND, crew_mode.DEFAULT_CREW_MODE_AGENT, created_by=CHILD),
        _row("p1"),
        _row("p2", created_by="p1"),
    ]


def test_crew_mode_stamps_are_unchanged_by_the_mate_walk(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pair test: every Crew Mode field matches the walk with no mate roots (main's)."""
    with_mates = _stamp(_crew_fixture())
    roots_with = crew_mode.known_roots()

    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "_row_is_mate_root", lambda _row: False)
    without = _stamp(_crew_fixture())

    assert crew_mode.known_roots() == roots_with == {ROOT}
    for key, row in without.items():
        assert {f: row.get(f) for f in CREW_FIELDS} == {
            f: with_mates[key].get(f) for f in CREW_FIELDS
        }, key
    assert with_mates["w2"][crew_mode.WORKER_FIELD] == ROOT
    assert crew_mode.list_view(list(with_mates.values()), "", agent=False) == [
        r for r in with_mates.values() if r["key"] in {ROOT, MATE, CHILD, GRAND, "p1", "p2"}
    ]


def test_live_stamp_rows_carry_the_mate_stamps_and_live_stamps_keep_their_shape() -> None:
    slots = {
        MATE: SimpleNamespace(key=MATE, agent="ada", agent_kind="member", mode=DM_SLOT_MODE),
        CHILD: SimpleNamespace(key=CHILD, agent=PLAIN, agent_kind="template", _created_by=MATE),
    }
    stamped = crew_mode.live_stamp_rows(slots)
    assert _mate_of(stamped[CHILD]) == (MATE, MATE)
    assert _mate_of(stamped[MATE]) == ("", "")
    assert crew_mode.live_stamps(slots) == {MATE: ("", False), CHILD: ("", False)}


# ── the slot_patch frame ───────────────────────────────────────────────────


class _WS:
    def __init__(self) -> None:
        self.closed = False
        self.send_str = AsyncMock()
        self._flags = {"_is_dashboard_user": True, SLOT_PATCH_WS_FLAG: True}

    def get(self, key: str, default: Any = None) -> Any:
        return self._flags.get(key, default)

    def patches(self) -> list[list[dict[str, Any]]]:
        frames = [json.loads(call.args[0]) for call in self.send_str.call_args_list]
        return [f["data"]["slots"] for f in frames if f["type"] == "slot_patch"]


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: False)
    yield DashboardState(
        sessions=MagicMock(count=0), crons=MagicMock(), lessons=MagicMock(), start_time=0.0
    )
    loop.close()
    asyncio.set_event_loop(None)


def test_the_lineage_patch_restates_the_mate_stamps_only_when_they_move(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mate = state.get_or_create_slot(MATE, origin=SlotOrigin.USER)
    mate.mode = DM_SLOT_MODE
    child = state.get_or_create_slot(CHILD, origin=SlotOrigin.USER)
    ws = _WS()
    state.register_ws(ws)
    parents: dict[str, object] = {MATE: None, CHILD: None}

    def fake_attach(rows: list[dict[str, Any]], _aliases: Any = None) -> None:
        for row in rows:
            row["parent"] = parents.get(row["key"])

    monkeypatch.setattr("kiro_crew.dashboard.state._attach_slot_parents", fake_attach)

    state.push_lineage_patch()
    child._created_by = MATE
    parents[CHILD] = {"slot": MATE, "key": MATE}
    state.push_lineage_patch()
    state.push_lineage_patch()

    first, second = ws.patches()
    assert all("mate_root" not in p for p in first)
    assert second == [
        {
            "key": CHILD,
            "parent": {"slot": MATE, "key": MATE},
            "lineage_pending": False,
            "mate_root": MATE,
            "mate_dispatched_by": MATE,
        }
    ]
