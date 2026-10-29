"""The Crew Mode dispatch notice: one row per person turn, only for a conductor.

Each case names the rule it guards and fails when the rule is removed. These
fail on current main, where ``crew_dispatch_notice`` does not exist and a
successful dispatch writes no row at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import crew_mode
from kiro_crew.dashboard import crew_dispatch_notice as cdn


@pytest.fixture
def appended(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every ``append_and_surface`` call the notice writer makes.

    Patched on ``kiro_crew.dashboard.state`` because the writer imports it from
    there at call time.
    """
    rows: list[dict[str, Any]] = []

    def _fake_append(
        state: Any, slot: Any, role: str, content: str, cls: str = "", *, meta: Any = None, **_: Any
    ) -> dict[str, Any]:
        row = {"role": role, "content": content, "cls": cls, "meta": meta or {}}
        rows.append(row)
        return row

    import kiro_crew.dashboard.state as state_mod

    monkeypatch.setattr(state_mod, "append_and_surface", _fake_append, raising=True)
    return rows


def _root_slot(key: str = "r", *, turn_generation: int = 1) -> SimpleNamespace:
    """A Crew Mode root conductor slot: a conductor template, no creator."""
    return SimpleNamespace(
        key=key,
        agent="kirocrew-conductor",
        agent_kind="template",
        crew_mode_agent="kirocrew-conductor",
        mode="",
        memory_mode="persistent",
        _created_by="",
        _app="",
        instance_id="",
        running=True,
        turn_running=True,
        messages=[{"role": "user", "content": "dispatch two workers"}],
        _disk_older_count=0,
        _turn_generation=turn_generation,
        _crew_dispatch_notice_turn_gen=-1,
    )


def _worker_slot(key: str = "w", *, turn_generation: int = 1) -> SimpleNamespace:
    """A crew member / worker slot: has a creator, so it is never a root."""
    slot = _root_slot(key, turn_generation=turn_generation)
    slot._created_by = "r"  # a creator → _live_creator_of non-empty → not a root
    return slot


_CREW_RESULT = {"ok": True, "target": "w1", "crew_mode": True}
_PLAIN_RESULT = {"ok": True, "target": "s1"}  # no crew_mode key: a plain chat


def test_conductor_dispatch_writes_one_notice(appended: list[dict[str, Any]]) -> None:
    """A Crew Mode conductor's successful dispatch writes exactly one notice row."""
    slot = _root_slot()
    assert crew_mode.slot_is_crew_mode_root(slot)  # precondition the gate reads
    wrote = cdn.maybe_write_dispatch_notice(object(), slot, _CREW_RESULT)
    assert wrote is True
    assert len(appended) == 1
    assert appended[0]["role"] == "notice"
    assert appended[0]["meta"]["kind"] == cdn.CREW_DISPATCH_NOTICE_KIND


def test_n_dispatches_one_person_turn_write_one_notice(appended: list[dict[str, Any]]) -> None:
    """N dispatches in ONE person turn (same _turn_generation) → one notice."""
    slot = _root_slot(turn_generation=7)
    for _ in range(4):
        cdn.maybe_write_dispatch_notice(object(), slot, _CREW_RESULT)
    assert len(appended) == 1


def test_a_new_person_turn_writes_again(appended: list[dict[str, Any]]) -> None:
    """A later person turn (a bumped generation) earns its own notice."""
    slot = _root_slot(turn_generation=1)
    cdn.maybe_write_dispatch_notice(object(), slot, _CREW_RESULT)
    slot._turn_generation = 2  # the task.setter bumps this once per turn
    cdn.maybe_write_dispatch_notice(object(), slot, _CREW_RESULT)
    assert len(appended) == 2


def test_plain_chat_dispatch_writes_nothing(appended: list[dict[str, Any]]) -> None:
    """A dispatch from a plain (non-Crew-Mode) chat writes no notice."""
    slot = _root_slot()
    wrote = cdn.maybe_write_dispatch_notice(object(), slot, _PLAIN_RESULT)
    assert wrote is False
    assert appended == []


def test_member_sub_dispatch_writes_nothing(appended: list[dict[str, Any]]) -> None:
    """A crew member's OWN sub-dispatch (caller is a worker, not a root) → nothing."""
    slot = _worker_slot()
    assert not crew_mode.slot_is_crew_mode_root(slot)
    wrote = cdn.maybe_write_dispatch_notice(object(), slot, _CREW_RESULT)
    assert wrote is False
    assert appended == []


# ── route wiring: the create route reaches the notice writer only on success ──


def _run_create_route(tmp_path, monkeypatch, *, refuse: bool) -> list[dict[str, Any]]:
    """Drive the create route and record every maybe_write_dispatch_notice call.

    Returns the list of recorded calls, so a refused create (which returns
    before the post-create block) records nothing and a successful one records
    one. The create itself is stubbed so this pins only the ROUTE WIRING, not
    the crew-mode tree resolution the helper unit tests already cover.
    """
    import asyncio
    import json
    from unittest.mock import MagicMock

    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import create_rate_limit
    from kiro_crew.dashboard import session_control as sc
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.handlers import session_control as handlers_sc
    from kiro_crew.dashboard.handlers import work_ledger as ledger_routes

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    create_rate_limit.reset_for_tests()

    async def _holds(key: str) -> bool:
        return False  # no ledger: keeps the post-create block cheap, still runs

    monkeypatch.setattr(ledger_routes, "holds_ledger", _holds)

    async def _create_ok(state: Any, **kw: Any) -> dict[str, Any]:
        return {"ok": True, "target": "w1", "title": "worker", "crew_mode": True}

    async def _create_refused(state: Any, **kw: Any) -> dict[str, Any]:
        raise sc.SessionControlError("nope", code="refused")

    monkeypatch.setattr(sc, "create_session", _create_refused if refuse else _create_ok)

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cdn,
        "maybe_write_dispatch_notice",
        lambda state, slot, result: calls.append({"result": result}) or True,
    )

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/create"
    request.method = "POST"
    request.headers = {"X-Session-Key": slot_history_key(caller)}
    request.get = lambda key, default=None: (
        True if key in ("internal_auth", "peer_verified") else default
    )

    async def _json() -> dict[str, Any]:
        return {"title": "worker"}

    request.json = _json
    resp = asyncio.run(handlers_sc.api_session_control_create(request))
    assert resp.status == (400 if refuse else 200), json.loads(resp.body.decode())
    create_rate_limit.reset_for_tests()
    return calls


def test_route_calls_the_notice_writer_on_a_successful_create(tmp_path, monkeypatch) -> None:
    """A successful, non-dry-run create reaches _write_crew_dispatch_notice."""
    calls = _run_create_route(tmp_path, monkeypatch, refuse=False)
    assert len(calls) == 1
    assert calls[0]["result"].get("crew_mode") is True


def test_route_skips_the_notice_writer_on_a_refused_create(tmp_path, monkeypatch) -> None:
    """A refused dispatch returns before the post-create block, so no notice."""
    calls = _run_create_route(tmp_path, monkeypatch, refuse=True)
    assert calls == []
