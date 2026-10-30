"""Shared slots, requests and fixtures for the work-ledger projection suites.

``_isolated`` is autouse and ``recorded`` is requested by name: each suite binds them
(``_isolated = work_ledger_projection_helpers._isolated``) so pytest registers them for it.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.crew_log import projection
from kiro_crew.dashboard.handlers import work_ledger as routes

CONDUCTOR = "chat-9-conductor"
WORKER = "chat-9-worker"
ACCEPTANCE = {"kind": "human_approval"}


class _Slot:
    """The slot attributes the routes read, and nothing else."""

    def __init__(self, created_by: str = "", workspace: str = "default") -> None:
        self._created_by = created_by
        self.workspace = workspace
        self.running = False


_SLOTS: dict[str, _Slot] = {}


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse pin of test_work_ledger_projection.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _isolated(tmp_path, monkeypatch):
    """An isolated data home and an open route; the gate itself stays live."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _SLOTS.clear()
    # Each test runs on its own event loop and an ``asyncio.Lock`` binds to the
    # loop it is first contended on, so a lock left from one test would fail the
    # next test's contended wait with "bound to a different event loop".
    routes._BOARD_LOCKS._locks.clear()

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(routes, "reaches_a_channel", lambda state, sk: False)
    yield
    _SLOTS.clear()


@pytest.fixture
def recorded(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    """The crew log on, every caller resolving to ``unit:<key>``, and every
    ``work/recorded`` append captured instead of written."""
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    monkeypatch.setattr(
        routes.crew_log_emit,
        "on_work_recorded",
        lambda unit, data: calls.append((unit, data)) or True,
    )
    return calls


def _req(method: str, path: str, *, body: Any = ..., sk: str) -> web.Request:
    app = web.Application()
    state = MagicMock()
    state.get_slot = MagicMock(side_effect=lambda key: _SLOTS.get(key))
    app["state"] = state
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": sk})
    req["internal_auth"] = True
    if body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


async def _record(sk: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_ledger_record(
        _req("POST", "/api/work-ledger/record", body=body, sk=sk)
    )
    return resp.status, json.loads(resp.text)


async def _report(sk: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_report(_req("POST", "/api/work-ledger/report", body=body, sk=sk))
    return resp.status, json.loads(resp.text)


async def _board() -> str:
    """goal, create, bind -- through the routes, so each is itself recorded."""
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
    assert status == 200, body
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "item one", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    status, body = await _record(
        CONDUCTOR, {"action": "bind", "item_id": item_id, "worker_session_key": WORKER}
    )
    assert status == 200, body
    return item_id


def _rendered(slot: str, item_id: str) -> dict[str, Any]:
    """A ``work`` fold's rendered value: the shape ``_work_render`` produces."""
    return {
        "conductor": {
            "schema": 1,
            "slot_key": slot,
            "goal": "ship it",
            "round": 2,
            "depth": 0,
            "parent_item": None,
            "created_at": "2026-09-20T10:00:00",
            "entries": 2,
        },
        "items": [
            {
                "schema": 1,
                "item_id": item_id,
                "title": "item one",
                "acceptance": ACCEPTANCE,
                "state": "open",
                "verdict": None,
                "decision": "",
                "worker_session_key": WORKER,
                "round": 1,
                "fails": 0,
                "status": "progress",
                "summary": "half way",
                "artifacts": {},
                "pr": 12,
                "last_report_at": "2026-09-20T10:05:00",
                "created_at": "2026-09-20T10:01:00",
                "closed_at": None,
                "events": [
                    {
                        "id": "e1",
                        "ts": "2026-09-20T10:01:00",
                        "item_id": item_id,
                        "kind": "create",
                        "status": None,
                        "text": "item one",
                    },
                    {
                        "id": "e2",
                        "ts": "2026-09-20T10:05:00",
                        "item_id": item_id,
                        "kind": "report",
                        "status": "progress",
                        "text": "half way",
                    },
                ],
            }
        ],
        "omitted": 0,
    }


async def _rebuild(sk: str) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_ledger_rebuild(
        _req("POST", "/api/work-ledger/rebuild", body={}, sk=sk)
    )
    return resp.status, json.loads(resp.text)


def _capture_probes(monkeypatch) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        routes.crew_log_emit, "work_entry_fits", lambda entry: probes.append(entry) or True
    )
    return probes


def _width(entry: dict[str, Any]) -> int:
    return len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))


def _real_units(monkeypatch, *, worker_lands: bool = True) -> None:
    """Real conductor and worker units; the routes' appends go to them for real."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    units = {CONDUCTOR: "u-conductor", WORKER: "u-worker"}
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: units[key])
    CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    CrewLog.create(lg.KIND_SESSION, "u-worker", owner="raymond", agent="kirocrew", slot=WORKER)

    def _append(unit: str, data: dict[str, Any]) -> bool:
        # ``worker_lands=False`` is a worker's report whose record does not land: the
        # board's copy in the conductor's unit (the one the fold reads) is refused.
        if data.get("actor") == "worker" and not worker_lands:
            return False
        projection.open_session_log(unit).append("work/recorded", data, src="gateway")
        return True

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _append)
