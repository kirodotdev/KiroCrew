"""The Goals tab's spend line: the credits a Bot's work used, read from the
``workstreams`` fold of the Bot's own thread slot.

What these tests pin:

* the total counts each worker session ONCE, however many tasks it served;
* a board whose workers wrote no charge reads ``not_reported``, never ``0``;
* a fold that dropped boards, tasks, units or spenders says ``partial``;
* a slot fold that is not warm yet reads ``pending`` and asks for a prime,
  rather than folding on the request path;
* the GET route carries the line; a write route does not read the fold.
"""

from __future__ import annotations

from typing import Any

from guide_route_helpers import FakeState
from member_goals_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    ORDINARY,
    _owner,
    _owner_ok,
    _quiet_sel,
    _seed,
    _slug,
)

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, _invalidate_config_cache
from kiro_crew.crew_log import projection, slot_prime
from kiro_crew.crew_log.entry_types import WORK_ENTRY_TYPE
from kiro_crew.crew_log.projection_catalogue import Projection
from kiro_crew.crew_log.schema import Entry
from kiro_crew.dashboard.handlers import member_goals as goal_routes

CONDUCTOR = "chat-1-lead"
WORKER_A = "chat-2-worker-a"
WORKER_B = "chat-3-worker-b"
NOON = 1_767_441_600_000

_seq = iter(range(1, 10_000))


def _entry(etype: str, data: dict[str, Any]) -> Entry:
    return Entry(type=etype, seq=next(_seq), time=NOON, src="test", data=data)


def _opened(slot: str) -> Entry:
    return _entry("session/opened", {"slot": slot})


def _work(action: str, **data: Any) -> Entry:
    return _entry(WORK_ENTRY_TYPE, {"action": action, "slot": CONDUCTOR, **data})


def _turn(credits: float | None) -> Entry:
    body: dict[str, Any] = {"duration_ms": 1_000}
    if credits is not None:
        body["credits"] = credits
    return _entry("turn/completed", body)


def _fold(entries: list[Entry]) -> dict[str, Any]:
    state = projection.initial("workstreams")
    bind = projection._FOLDS["workstreams"].bind_slot
    assert bind is not None
    bind(state.state, CONDUCTOR)
    return projection.projection_of(projection.advance(state, entries)).value


def _board(*charges: tuple[str, float | None]) -> list[Entry]:
    """One board: WORKER_A bound to two tasks, WORKER_B to one, then their charges."""
    entries = [
        _opened(CONDUCTOR),
        _work("goal", goal="ship the beta", round=1),
        _work("create", item_id="it_1", title="one", round=1),
        _work("create", item_id="it_2", title="two", round=1),
        _work("create", item_id="it_3", title="three", round=1),
        _work("bind", item_id="it_1", worker_session_key=WORKER_A),
        _work("bind", item_id="it_2", worker_session_key=WORKER_A),
        _work("bind", item_id="it_3", worker_session_key=WORKER_B),
    ]
    for worker, credits in charges:
        entries += [_opened(worker), _turn(credits)]
    return entries


def _bind(member: str) -> str:
    """Bind *member*'s DM thread, as opening its chat does, and return the slug."""
    slug = _slug(member)
    members_mod.write_dm_binding(
        slug, member=member, slot_key=members_mod.DM_SLOT_KEY_PREFIX + slug
    )
    return slug


def test_each_worker_is_counted_once() -> None:
    """WORKER_A served two tasks; its 2.5 is in the total once, not twice."""
    value = _fold(_board((WORKER_A, 2.5), (WORKER_B, 1.25)))
    assert goal_routes.work_spend(value) == {
        "status": "reported",
        "credits": 3.75,
        "partial": False,
    }


def test_no_charge_is_not_reported_rather_than_zero() -> None:
    """Workers that billed no credits wrote no ``credits`` key: nothing was metered."""
    value = _fold(_board((WORKER_A, None), (WORKER_B, None)))
    assert goal_routes.work_spend(value) == {"status": "not_reported", "partial": False}


def test_an_empty_fold_is_not_reported() -> None:
    assert goal_routes.work_spend(_fold([])) == {"status": "not_reported", "partial": False}


def test_a_short_fold_says_partial() -> None:
    value = _fold(_board((WORKER_A, 2.0)))
    for key in ("omitted", "units_omitted", "spenders_omitted"):
        short = dict(value, **{key: 1})
        assert goal_routes.work_spend(short)["partial"] is True, key
    board = dict(value["items"][0], tasks_omitted=3)
    assert goal_routes.work_spend(dict(value, items=[board]))["partial"] is True


def test_a_malformed_value_is_not_reported() -> None:
    assert goal_routes.work_spend(None) == {"status": "not_reported", "partial": False}
    assert goal_routes.work_spend({"items": "x"}) == {"status": "not_reported", "partial": False}


def test_the_get_route_carries_the_spend_line(_floor_monkeypatch) -> None:
    _seed()
    slug = _bind(ORDINARY)
    value = _fold(_board((WORKER_A, 2.5), (WORKER_B, 1.25)))
    asked: list[tuple[str, str, bool]] = []

    def read(slot: str, name: str, *, cold: bool = True, **_kw: Any) -> Projection:
        asked.append((slot, name, cold))
        return Projection(name=name, seq=1, value=value)

    _floor_monkeypatch.setattr(projection, "read_slot_projection", read)
    status, body = _owner(FakeState(), "GET", f"/api/members/{slug}/goals?member={ORDINARY}")
    assert status == 200, body
    assert body["spend"] == {"status": "reported", "credits": 3.75, "partial": False}
    # The request path never folds cold, and reads the fold the Bot's pages read.
    assert [(name, cold) for _slot, name, cold in asked] == [("workstreams", False)]
    assert slug in asked[0][0]


def test_a_cold_slot_reads_pending_and_asks_for_a_prime(_floor_monkeypatch) -> None:
    _seed()
    slug = _bind(ORDINARY)
    primed: list[tuple[str, str]] = []

    def read(slot: str, name: str, **_kw: Any) -> Projection:
        raise projection.SlotFoldNotReady(slot, name)

    def schedule(slot: str, name: str, *, for_reader: bool = False) -> bool:
        primed.append((name, str(for_reader)))
        return True

    _floor_monkeypatch.setattr(projection, "read_slot_projection", read)
    _floor_monkeypatch.setattr(slot_prime, "schedule", schedule)
    status, body = _owner(FakeState(), "GET", f"/api/members/{slug}/goals?member={ORDINARY}")
    assert status == 200, body
    assert body["spend"] == {"status": "pending"}
    assert primed == [("workstreams", "True")]


def test_an_unreadable_fold_leaves_the_line_out(_floor_monkeypatch) -> None:
    """A broken read keeps the tab working: the goals still load, with no spend line."""
    _seed()
    slug = _bind(ORDINARY)

    def read(*_a: Any, **_kw: Any) -> Projection:
        raise OSError("disk")

    _floor_monkeypatch.setattr(projection, "read_slot_projection", read)
    status, body = _owner(FakeState(), "GET", f"/api/members/{slug}/goals?member={ORDINARY}")
    assert status == 200, body
    assert body["spend"] is None


def test_a_write_does_not_read_the_fold(_floor_monkeypatch) -> None:
    _seed()
    slug = _slug(ORDINARY)

    def read(*_a: Any, **_kw: Any) -> Projection:
        raise AssertionError("a goal write must not read the crew log")

    _floor_monkeypatch.setattr(projection, "read_slot_projection", read)
    status, body = _owner(
        FakeState(),
        "PUT",
        f"/api/members/{slug}/goals/long-term",
        {"member": ORDINARY, "long_term": "ship the beta"},
    )
    assert status == 200, body
    assert "spend" not in body


def test_spend_is_shown_only_to_the_bot_the_thread_is_bound_to(_floor_monkeypatch) -> None:
    """``scout`` and ``Scout!`` share a slug and so a thread slot. The thread is
    ``scout``'s: ``scout`` sees its spend, ``Scout!`` sees none and reads nothing.
    The same holds after ``scout`` is deleted and ``Scout!`` keeps the slug."""
    _seed()
    cfg = KiroCrewConfig.load()
    cfg.agents["Scout!"] = KiroCrewAgentConfig(kiro_agent=cfg.default_agent)
    cfg.save()
    _invalidate_config_cache()
    slug = _bind(ORDINARY)
    assert _slug("Scout!") == slug
    asked: list[str] = []

    def read(slot: str, name: str, **_kw: Any) -> Projection:
        asked.append(name)
        return Projection(name=name, seq=1, value=_fold(_board((WORKER_A, 2.5))))

    _floor_monkeypatch.setattr(projection, "read_slot_projection", read)
    path = f"/api/members/{slug}/goals?member="
    status, body = _owner(FakeState(), "GET", path + "Scout!")
    assert status == 200, body
    assert body["spend"] is None
    assert asked == []
    status, body = _owner(FakeState(), "GET", path + ORDINARY)
    assert body["spend"]["credits"] == 2.5
    del cfg.agents[ORDINARY]
    cfg.save()
    _invalidate_config_cache()
    asked.clear()
    status, body = _owner(FakeState(), "GET", path + "Scout!")
    assert status == 200, body
    assert body["spend"] is None
    assert asked == []


def test_a_bot_with_no_thread_reads_nothing(_floor_monkeypatch) -> None:
    _seed()
    slug = _slug(ORDINARY)
    asked: list[str] = []

    def read(slot: str, name: str, **_kw: Any) -> Projection:
        asked.append(name)
        return Projection(name=name, seq=1, value={})

    _floor_monkeypatch.setattr(projection, "read_slot_projection", read)
    status, body = _owner(FakeState(), "GET", f"/api/members/{slug}/goals?member={ORDINARY}")
    assert status == 200, body
    assert body["spend"] is None
    assert asked == []
