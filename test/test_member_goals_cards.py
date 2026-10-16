"""Crewmate Goals: cards proposed and applied in a chat, their Undo, and the nudges.

Split from ``test_member_goals.py`` to stay under the file line cap; the
rules it pins are listed there.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from guide_route_helpers import FakeState, run_guide_app
from member_goals_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    DEFAULT,
    ORDINARY,
    OWNER_ROUTES,
    TOKEN,
    _Crons,
    _gid,
    _goal,
    _jobs_by_cadence,
    _owner,
    _owner_ok,
    _quiet_sel,
    _reminder_state,
    _schedules,
    _seed,
    _slug,
)

from kiro_crew import change_card_catalog as catalog
from kiro_crew import mcp_mate, member_goals
from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, _invalidate_config_cache
from kiro_crew.cron import CronService
from kiro_crew.dashboard import goal_reminder
from kiro_crew.dashboard.handlers import member_goals as goal_routes
from kiro_crew.testing.wait import await_until

# ── a crewmate made on a crewmate.create card ──

NEW_MEMBER = "rover"
NEW_MEMBER_ID = "m-rover01"


class _CardState(FakeState):
    """A dashboard state the card hook can publish to, with a real cron store."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__()
        from kiro_crew.dashboard.change_cards import CardStore

        self._change_card_store = CardStore(tmp_path / "cards.json")
        self.crons = _Crons(tmp_path / "crons")
        self.agent_bodies: list[dict[str, Any]] = []
        self.cron_bodies: list[dict[str, Any]] = []

    def broadcast_ws(self, kind: str, payload: dict[str, Any]) -> None:
        self.frames.append((kind, payload))

    def push_refresh(self, kind: str) -> None:
        self.frames.append(("refresh", {"kind": kind}))


def _create_card_app(state: _CardState) -> web.Application:
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    async def create_agent(request: web.Request) -> web.Response:
        # Stands in for POST /api/agents: it records the body and publishes the
        # crewmate with the immutable member_id the real route allocates.
        body = await request.json()
        state.agent_bodies.append(body)
        cfg = KiroCrewConfig.load()
        cfg.agents[NEW_MEMBER] = KiroCrewAgentConfig(
            kiro_agent=DEFAULT,
            member_id=NEW_MEMBER_ID,
            description=body.get("description", ""),
        )
        cfg.save()
        _invalidate_config_cache()
        return web.json_response({"ok": True, "name": NEW_MEMBER, "member_id": NEW_MEMBER_ID})

    async def create_cron(request: web.Request) -> web.Response:
        body = await request.json()
        state.cron_bodies.append(body)
        job = state.crons.svc.add_job(body["name"], body["message"], cron_expr=body["cron"])
        return web.json_response({"ok": True, "id": job.id})

    app = web.Application(middlewares=[_owner_auth, card_routes.change_card_middleware])
    app["state"] = state
    card_routes.register_change_card_routes(app)
    app.router.add_post("/api/agents", create_agent)
    app.router.add_post("/api/crons", create_cron)
    app.router.add_put(
        "/api/members/{slug}/goals/long-term", goal_routes.api_member_goals_long_term
    )
    app.router.add_put(
        "/api/members/{slug}/goals/key-results", goal_routes.api_member_goals_key_results
    )
    app.router.add_put(
        "/api/members/{slug}/goals/schedules", goal_routes.api_member_goals_schedules
    )
    return app


@web.middleware
async def _owner_auth(request: web.Request, handler: Any) -> web.StreamResponse:
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


def _propose_create(state: _CardState, params: dict[str, Any]) -> dict[str, Any]:
    from kiro_crew.dashboard import change_cards as cards

    params = catalog.validate_params(catalog.KIND_CREWMATE_CREATE, params)
    before = asyncio.run(
        cards.read_state(catalog.KIND_CREWMATE_CREATE, params, [], state=state, app=None)
    )
    preview = catalog.build_preview(catalog.KIND_CREWMATE_CREATE, params, before, {})
    rec = state._change_card_store.propose(
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        kind=catalog.KIND_CREWMATE_CREATE,
        params=params,
        reason="make rover",
        preview=preview,
        before=before,
        context={},
    )
    return state._change_card_store.public(rec)


def _fill(value: Any, fills: list[dict[str, Any]], responses: list[Any]) -> Any:
    """The browser's ``runPlan`` fills, for a step's body or its path."""
    for f in fills:
        got = responses[f["step"]][f["key"]]
        if isinstance(value, str):
            value = value.replace(catalog.placeholder(f), got)
        else:
            value = {**value, f["field"]: got}
    return value


def _apply_create(state: _CardState, card: dict[str, Any]) -> list[tuple[int, Any]]:
    """Send the card's apply plan the way the dashboard does, stopping at a refusal."""
    from aiohttp.test_utils import TestClient, TestServer

    async def main() -> list[tuple[int, Any]]:
        client = TestClient(TestServer(_create_card_app(state)))
        await client.start_server()
        out: list[tuple[int, Any]] = []
        responses: list[Any] = []
        try:
            for i, step in enumerate(card["plan"]["apply"]):
                fills = step.get("fill") or []
                path = _fill(step["path"], [f for f in fills if f.get("in") == "path"], responses)
                body = step["body"]
                body_fills = [f for f in fills if f.get("in") != "path"]
                if body_fills:
                    body = _fill(body, body_fills, responses)
                r = await client.request(
                    step["method"],
                    path,
                    json=body,
                    headers={
                        "X-Card-Id": card["id"],
                        "X-Card-Revision": str(card["revision"]),
                        "X-Card-Op": "apply",
                        "X-Card-Step": str(i),
                    },
                )
                data = await r.json()
                out.append((r.status, data))
                responses.append(data)
                if r.status >= 400:
                    break
            from kiro_crew.dashboard.handlers import change_cards as card_routes

            if card_routes._BACKGROUND:
                await asyncio.gather(*list(card_routes._BACKGROUND))
            return out
        finally:
            await client.close()

    return asyncio.run(main())


@pytest.fixture
def _card_owner(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


SCHEDULED = {
    "name": "Rover",
    "goal": "Keep the docs current",
    "schedule": {"cron_expr": "0 9 * * 1"},
}


def test_a_create_card_writes_its_goal_as_the_long_term_goal_not_the_description(
    tmp_path: Path, _card_owner
) -> None:
    _seed()
    state = _CardState(tmp_path)
    card = _propose_create(state, SCHEDULED)
    results = _apply_create(state, card)
    assert [status for status, _ in results] == [200, 200, 200], results
    (agent_body,) = state.agent_bodies
    assert agent_body.get("description", "") == ""
    assert KiroCrewConfig.load().agents[NEW_MEMBER].description == ""
    rec = member_goals.read_goals(NEW_MEMBER_ID, NEW_MEMBER)
    assert _goal(rec) == "Keep the docs current"
    assert member_goals.member_goals_path(NEW_MEMBER_ID).parent.name == "member-goals"
    assert state._change_card_store.get(card["id"])["status"] == "applied"


def test_a_create_cards_schedule_stays_its_work_schedule(tmp_path: Path, _card_owner) -> None:
    _seed()
    state = _CardState(tmp_path)
    card = _propose_create(state, SCHEDULED)
    _apply_create(state, card)
    (cron_body,) = state.cron_bodies
    assert cron_body == {
        "name": "Rover schedule",
        "message": "Keep the docs current",
        "agent": catalog.DEFAULT_CREWMATE_TEMPLATE,
        "silent": False,
        "hide_in_chat": False,
        "strict_schedule": True,
        "cron": "0 9 * * 1",
        "member_id": NEW_MEMBER_ID,
    }
    (job,) = state.crons.svc.list_jobs(include_disabled=True)
    assert job.managed_by in ("", None)
    assert all(not _schedules(state, NEW_MEMBER_ID, c) for c in member_goals.CADENCES)


def test_a_create_card_whose_goal_write_fails_says_so_on_the_card(
    tmp_path: Path, _card_owner, _floor_monkeypatch
) -> None:
    _seed()
    state = _CardState(tmp_path)

    def broken(*_a: Any, **_kw: Any) -> Any:
        raise OSError("disk full")

    _floor_monkeypatch.setattr(member_goals, "set_goal", broken)
    card = _propose_create(state, SCHEDULED)
    results = _apply_create(state, card)
    assert [status for status, _ in results] == [200, 500]
    assert results[1][1]["code"] == "goals_write_failed"
    rec = state._change_card_store.get(card["id"])
    assert rec["status"] == "partial"
    assert rec["error"]["step"] == catalog.CREWMATE_CREATE_GOAL_STEP
    assert rec["result"]["summary"] == (
        "Created crewmate “Rover”, but its goal or its schedule was not saved"
    )
    # The schedule after the failed goal never ran, and Undo still removes the crewmate.
    assert state.cron_bodies == []
    assert [s["path"] for s in rec["plan"]["undo"]] == [f"/api/agents/{NEW_MEMBER}"]


def _propose_check_ins(state: _CardState, params: dict[str, Any]) -> dict[str, Any]:
    from kiro_crew.dashboard import change_cards as cards

    kind = catalog.KIND_CREWMATE_CHECK_INS
    params = catalog.validate_params(kind, {"member": ORDINARY, **params})
    before = asyncio.run(cards.read_state(kind, params, [], state=state, app=None))
    preview = catalog.build_preview(kind, params, before, {"label": "Scout"})
    rec = state._change_card_store.propose(
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        kind=kind,
        params=params,
        reason="check in on the goal",
        preview=preview,
        before=before,
        context={},
    )
    return state._change_card_store.public(rec)


def _run_card(state: _CardState, card_id: str, op: str) -> list[tuple[int, Any]]:
    """Send the card's *op* plan through the real card middleware, as the dashboard does."""
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import change_cards as card_routes

    card = state._change_card_store.public(state._change_card_store.get(card_id))

    async def main() -> list[tuple[int, Any]]:
        client = TestClient(TestServer(_create_card_app(state)))
        await client.start_server()
        out: list[tuple[int, Any]] = []
        try:
            for i, step in enumerate(card["plan"][op]):
                r = await client.request(
                    step["method"],
                    step["path"],
                    json=step["body"],
                    headers={
                        "X-Card-Id": card["id"],
                        "X-Card-Revision": str(card["revision"]),
                        "X-Card-Op": op,
                        "X-Card-Step": str(i),
                    },
                )
                out.append((r.status, await r.json()))
                if r.status >= 400:
                    break
            if card_routes._BACKGROUND:
                await asyncio.gather(*list(card_routes._BACKGROUND))
            return out
        finally:
            await client.close()

    return asyncio.run(main())


def test_a_check_ins_card_applies_and_undoes_through_the_card_hook(
    tmp_path: Path, _card_owner
) -> None:
    """The schedules route is hooked: Apply finishes the card and Undo reverses only its fields."""
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    # State the card does not touch: the monthly review and its day, and the timezone.
    member_goals.set_schedules(
        slug, ORDINARY, {"monthly": True, "month_day": 12, "timezone": "UTC"}
    )

    def schedules() -> dict[str, Any]:
        return dict(member_goals.read_goals(slug, ORDINARY)["schedules"])

    # A card previewed before the record changed is refused, writing nothing.
    stale = _propose_check_ins(state, {"weekly": True})
    member_goals.set_schedules(slug, ORDINARY, {"weekday": 4})
    held = schedules()
    ((status, body),) = _run_card(state, stale["id"], catalog.OP_APPLY)
    assert (status, body["code"]) == (409, "changed_since_preview")
    assert schedules() == held
    assert state._change_card_store.get(stale["id"])["status"] == "pending"

    card = _propose_check_ins(state, {"daily": True, "time": "08:30"})
    ((status, body),) = _run_card(state, card["id"], catalog.OP_APPLY)
    assert status == 200, body
    rec = state._change_card_store.get(card["id"])
    assert rec["status"] == "applied"
    (undo,) = rec["plan"]["undo"]
    assert undo["path"] == f"/api/members/{slug}/goals/schedules"
    assert undo["body"] == {
        "member": ORDINARY,
        "daily": False,
        "time": "09:00",
        "expected": {"daily": True, "time": "08:30"},
    }
    applied = schedules()
    assert (applied["daily"], applied["time"]) == (True, "08:30")
    assert (applied["monthly"], applied["month_day"]) == (True, 12)
    assert (applied["weekday"], applied["timezone"]) == (4, "UTC")
    assert len(_schedules(state, slug, "daily")) == 1

    ((status, body),) = _run_card(state, card["id"], catalog.OP_UNDO)
    assert status == 200, body
    assert state._change_card_store.get(card["id"])["status"] == "undone"
    assert schedules() == {**applied, "daily": False, "time": "09:00"}
    assert _schedules(state, slug, "daily") == []
    assert len(_schedules(state, slug, "monthly")) == 1


def test_a_heartbeat_card_turns_it_on_at_its_interval_and_undo_turns_it_off(
    tmp_path: Path, _card_owner
) -> None:
    """The heartbeat turns on through an applied card, and the card names its cost."""
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    card = _propose_check_ins(state, {"heartbeat": True, "interval": 60})
    from kiro_crew.dashboard import change_cards as cards

    kind = catalog.KIND_CREWMATE_CHECK_INS
    params = catalog.validate_params(kind, {"member": ORDINARY, "heartbeat": True, "interval": 60})
    before = asyncio.run(cards.read_state(kind, params, [], state=state, app=None))
    preview = catalog.build_preview(kind, params, before, {"label": "Scout"})
    rows = {c["field"]: c for c in preview["changes"]}
    assert rows["heartbeat"]["after"] == "On" and "credits" in rows["heartbeat"]["label"]
    assert rows["interval"]["before"] == "Every 30 min"
    assert rows["interval"]["after"] == "Every 1 h"
    ((status, body),) = _run_card(state, card["id"], catalog.OP_APPLY)
    assert status == 200, body
    sched = member_goals.read_goals(slug, ORDINARY)["schedules"]
    assert (sched["heartbeat"], sched["interval"]) == (True, 60)
    (job,) = _schedules(state, slug, "heartbeat")
    assert job.schedule.cron_expr == "0 * * * *"
    ((status, body),) = _run_card(state, card["id"], catalog.OP_UNDO)
    assert status == 200, body
    sched = member_goals.read_goals(slug, ORDINARY)["schedules"]
    assert (sched["heartbeat"], sched["interval"]) == (False, 30)
    assert _schedules(state, slug, "heartbeat") == []


@pytest.mark.parametrize("interval", [0, 45, True, "30"])
def test_a_heartbeat_card_refuses_an_interval_outside_the_list(interval: Any) -> None:
    with pytest.raises(Exception) as exc:
        catalog.validate_params(
            catalog.KIND_CREWMATE_CHECK_INS,
            {"member": ORDINARY, "heartbeat": True, "interval": interval},
        )
    assert "interval" in str(exc.value)


def test_a_path_fill_takes_only_the_identifier_the_earlier_step_returned() -> None:
    from kiro_crew.dashboard.handlers.change_cards import match_step

    p = catalog.validate_params(catalog.KIND_CREWMATE_CREATE, {"name": "Rover", "goal": "g"})
    goal = catalog.build_preview(catalog.KIND_CREWMATE_CREATE, p, {"exists": False}, {})["apply"][
        catalog.CREWMATE_CREATE_GOAL_STEP
    ]
    body = {"member": NEW_MEMBER, "long_term": "g", "expected": ""}
    ev = [{"name": NEW_MEMBER, "member_id": NEW_MEMBER_ID}]

    def check(path: str, evidence: list[dict[str, Any]] = ev) -> str | None:
        return match_step(goal, method="PUT", path=path, query={}, body=body, evidence=evidence)

    assert check(f"/api/members/{NEW_MEMBER_ID}/goals/long-term") is None
    assert check("/api/members/scout/goals/long-term") == "path"
    bad = [{"name": NEW_MEMBER, "member_id": "../scout"}]
    assert check("/api/members/../scout/goals/long-term", bad) == "fill:slug"


def test_deleting_a_crewmate_removes_its_goal_check_ins(tmp_path: Path, _floor_monkeypatch) -> None:
    """Its check-ins are listed nowhere else, so the delete has to take them."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_delete

    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    _seed()
    gone, kept = _slug(ORDINARY), _slug(ASSISTANT_MEMBER_NAME)
    svc = CronService(base_dir=tmp_path / "crons")

    async def go() -> int:
        await svc.add_job_async("ordinary", "do it", every_secs=3600)
        for slug in (gone, kept):
            for cadence in member_goals.CADENCES:
                await svc.add_managed_job_async(
                    goal_reminder.managed_key(slug, cadence),
                    name=f"{slug} {cadence}",
                    message="x",
                    cron_expr="0 9 * * *",
                )
        request = MagicMock(spec=web.Request)
        request.method = "DELETE"
        request.match_info = {"name": ORDINARY}
        request.app = {"state": SimpleNamespace(crons=svc, push_refresh=MagicMock())}
        request.get = lambda key, default=None: default
        return (await api_kirocrew_agent_delete(request)).status

    assert asyncio.run(go()) == 200
    names = sorted(j.name for j in svc.list_jobs(include_disabled=True))
    assert names == sorted(["ordinary", *(f"{kept} {c}" for c in member_goals.CADENCES)])


def test_goal_strings_leave_the_record_scrubbed(_floor_monkeypatch) -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, f"use {TOKEN} for releases", by="crewmate")
    member_goals.set_key_results(slug, ORDINARY, [{"text": f"kr {TOKEN}", "target": "1"}])
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    member_goals.record_key_result(slug, ORDINARY, kid, TOKEN[:40])
    view = member_goals.public_view(member_goals.read_goals(slug, ORDINARY))
    assert TOKEN not in json.dumps(view)
    assert "[REDACTED" in _goal(view)


def test_the_goals_tab_saves_over_a_goal_it_was_shown_scrubbed() -> None:
    """The page sends back the scrubbed text as expected; that is not a conflict."""
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, f"use {TOKEN}", by="crewmate")
    shown = _goal(member_goals.public_view(member_goals.read_goals(slug, ORDINARY)))
    assert shown != f"use {TOKEN}"
    gid = _gid(slug, ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "use the vault", by="user", goal_id=gid, expected=shown)
    member_goals.set_key_results(slug, ORDINARY, [{"text": f"kr {TOKEN}", "target": "1"}])
    shape = member_goals.public_view(member_goals.read_goals(slug, ORDINARY))["key_results"]
    member_goals.set_key_results(slug, ORDINARY, [{"text": "kr", "target": "1"}], expected=shape)
    with pytest.raises(member_goals.GoalError):
        member_goals.set_goal(
            slug, ORDINARY, "x", by="user", goal_id=gid, expected="something else"
        )


def test_a_report_is_saved_scrubbed() -> None:
    from kiro_crew import artifacts as artifacts_mod

    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    saved = goal_reminder.save_report(
        slug,
        ORDINARY,
        "Scout",
        kind="weekly",
        title=f"Week {TOKEN}",
        markdown=f"token {TOKEN}",
        session_key="s",
    )
    art = artifacts_mod.get_default_store().get(saved["slug"])
    assert TOKEN not in (art.content or "") and TOKEN not in art.name


def test_wide_readings_never_lock_the_record(_floor_monkeypatch) -> None:
    """CJK readings are 3 bytes a character: the oldest go first so it still reads."""
    _seed()
    slug = _slug(ORDINARY)
    clock = [1_000_000_000.0]

    def tick() -> float:
        clock[0] += 1
        return clock[0]

    _floor_monkeypatch.setattr(member_goals, "_now", tick)
    member_goals.set_goal(slug, ORDINARY, "目" * member_goals.LONG_TERM_MAX_CHARS, by="user")
    items = [{"text": "指" * 200, "target": "目" * 40} for _ in range(3)]
    items = [{**i, "text": f"{n}" + i["text"][1:]} for n, i in enumerate(items)]
    member_goals.set_key_results(slug, ORDINARY, items)
    ids = [k["id"] for k in member_goals.read_goals(slug, ORDINARY)["key_results"]]
    for n in range(member_goals.MAX_READINGS):
        for kid in ids:
            member_goals.record_key_result(slug, ORDINARY, kid, f"{n:03d}" + "读" * 37)
    raw = member_goals.member_goals_path(slug).read_bytes()
    assert len(raw) <= member_goals._RECORD_MAX_BYTES
    rec = member_goals.read_goals(slug, ORDINARY)
    newest = rec["key_results"][0]["readings"][-1]["value"]
    assert newest.startswith(f"{member_goals.MAX_READINGS - 1:03d}")
    assert len(rec["key_results"][0]["readings"]) < member_goals.MAX_READINGS


def test_a_key_results_card_undo_restores_ids_and_readings(tmp_path: Path) -> None:
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    one = [{"text": "PRs described", "target": "27"}]
    member_goals.set_key_results(slug, ORDINARY, one)
    original = member_goals.read_goals(slug, ORDINARY)["key_results"][0]
    member_goals.record_key_result(slug, ORDINARY, original["id"], "19")
    before = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    new = [{"text": "notes out", "target": "yes"}]
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS, {"member": ORDINARY, "items": new}
    )
    (apply,) = catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, {})["apply"]
    assert _owner(state, "PUT", apply["path"], apply["body"])[0] == 200
    # A reading after the apply is not a conflict for the Undo.
    after = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    member_goals.record_key_result(slug, ORDINARY, kid, "no")
    now = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    assert not cards.undo_snapshot_changed(catalog.KIND_CREWMATE_KEY_RESULTS, now, after)
    (undo,), _ = catalog.build_undo(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, [{}], 1)
    status, body = _owner(state, "PUT", undo["path"], undo["body"])
    assert status == 200, body
    (restored,) = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert restored["id"] == original["id"]
    assert [r["value"] for r in restored["readings"]] == ["19"]
    assert restored["current"] == "19"


def test_a_key_results_cards_public_plan_carries_no_unscrubbed_reading(tmp_path: Path) -> None:
    """The plan reaches every dashboard socket (CardStore.public): readings scrubbed."""
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "PRs described", "target": "27"}])
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    member_goals.record_key_result(slug, ORDINARY, kid, TOKEN)
    before = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS,
        {"member": ORDINARY, "items": [{"text": "notes out", "target": "yes"}]},
    )
    preview = catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, {})
    store = cards.CardStore(None)
    rec = store.propose(
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        kind=catalog.KIND_CREWMATE_KEY_RESULTS,
        params=params,
        reason="",
        preview=preview,
        before=before,
        context={},
    )
    store.begin_step(rec, revision=rec["revision"], op="apply", index=0)
    store.record_success(rec, op="apply", index=0, evidence={})
    undo, reason = catalog.build_undo(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, [{}], 1)
    store.complete_apply(rec, after={}, undo=undo, undo_reason=reason)
    public = json.dumps(store.public(rec))
    assert TOKEN not in public
    # The card names milestones the way the Goals tab does.
    assert store.public(rec)["title"] == f"Set the milestones for “{ORDINARY}”"
    (step,) = store.public(rec)["plan"]["undo"]
    assert [k["id"] for k in step["body"]["restore"]] == [kid]


def test_a_key_results_card_undo_keeps_readings_recorded_since_on_a_kept_key_result(
    tmp_path: Path,
) -> None:
    """Undo puts back what the card removed; what it kept stays as it is now."""
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    a, b = {"text": "PRs described", "target": "27"}, {"text": "notes out", "target": "yes"}
    member_goals.set_key_results(slug, ORDINARY, [a, b])
    ids = [k["id"] for k in member_goals.read_goals(slug, ORDINARY)["key_results"]]
    member_goals.record_key_result(slug, ORDINARY, ids[0], "5")
    member_goals.record_key_result(slug, ORDINARY, ids[1], "no")
    before = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS, {"member": ORDINARY, "items": [a]}
    )
    (apply,) = catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, {})["apply"]
    assert _owner(state, "PUT", apply["path"], apply["body"])[0] == 200
    member_goals.record_key_result(slug, ORDINARY, ids[0], "9")
    (undo,), _ = catalog.build_undo(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, [{}], 1)
    status, body = _owner(state, "PUT", undo["path"], undo["body"])
    assert status == 200, body
    kept, back = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert (kept["id"], kept["current"]) == (ids[0], "9")
    assert [r["value"] for r in kept["readings"]] == ["5", "9"]
    assert (back["id"], back["current"]) == (ids[1], "no")


def test_saving_the_scrubbed_text_shown_keeps_the_real_stored_text() -> None:
    """The page holds scrubbed text; saving it back must not store the placeholders."""
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, f"Ship with {TOKEN}", by="user")
    shown = member_goals.public_view(member_goals.read_goals(slug, ORDINARY))
    member_goals.set_goal(slug, ORDINARY, _goal(shown), by="user", goal_id=_gid(slug, ORDINARY))
    assert _goal(member_goals.read_goals(slug, ORDINARY)) == f"Ship with {TOKEN}"
    member_goals.set_key_results(
        slug,
        ORDINARY,
        [{"text": f"key {TOKEN}", "target": "1"}, {"text": "B", "target": "2"}],
    )
    view = member_goals.public_view(member_goals.read_goals(slug, ORDINARY))["key_results"]
    assert TOKEN not in json.dumps(view)
    # Edit only B, sending A back as the page shows it.
    items = [
        {"id": view[0]["id"], "text": view[0]["text"], "target": view[0]["target"]},
        {"id": view[1]["id"], "text": "B2", "target": "2"},
    ]
    member_goals.set_key_results(slug, ORDINARY, items)
    a, b = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert a["text"] == f"key {TOKEN}" and b["text"] == "B2"
    # Typing the placeholder text deliberately is a real edit only when it
    # differs from the scrubbed form; the same form keeps the stored text.
    assert member_goals.keep_unscrubbed("plain", "plain2") == "plain2"


def test_a_key_results_card_undo_puts_back_the_unscrubbed_records(
    tmp_path: Path, _card_owner
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": f"key {TOKEN}", "target": "1"}])
    rec = _propose_card(
        state,
        catalog.KIND_CREWMATE_KEY_RESULTS,
        {"member": ORDINARY, "items": [{"text": "B", "target": "2"}]},
    )
    assert _run_card_plan(state, rec, "apply") == [200]
    assert TOKEN not in json.dumps(state._change_card_store.public(rec))
    assert _run_card_plan(state, rec, "undo") == [200]
    (back,) = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert back["text"] == f"key {TOKEN}"


def test_undoing_a_card_that_added_the_first_milestones_still_checks_their_ids(
    tmp_path: Path,
) -> None:
    """``restore: []`` (no milestones before the card) still runs the locked id check."""
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    a = {"text": "PRs described", "target": "27"}
    before = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS, {"member": ORDINARY, "items": [a]}
    )
    (apply,) = catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, {})["apply"]
    status, applied = _owner(state, "PUT", apply["path"], apply["body"])
    assert status == 200, applied
    evidence = [catalog.extract_evidence(catalog.KIND_CREWMATE_KEY_RESULTS, "apply", 0, applied)]
    first = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    member_goals.remove_key_result(slug, ORDINARY, first)
    member_goals.set_key_results(slug, ORDINARY, [a])
    new = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    member_goals.record_key_result(slug, ORDINARY, new, "9")
    (undo,), _ = catalog.build_undo(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, evidence, 1)
    assert undo["body"]["restore"] == []
    status, body = _owner(state, "PUT", undo["path"], undo["body"])
    assert status == 409, body
    (kept,) = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert (kept["id"], kept["current"]) == (new, "9")


def test_a_stale_key_results_undo_refuses_a_set_recreated_with_the_same_wording(
    tmp_path: Path,
) -> None:
    """Same wording, new ids: the older card's Undo must not replace the new set."""
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    a, b = {"text": "PRs described", "target": "27"}, {"text": "notes out", "target": "yes"}
    member_goals.set_key_results(slug, ORDINARY, [a])
    first_a = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    before = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS, {"member": ORDINARY, "items": [a, b]}
    )
    (apply,) = catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, {})["apply"]
    status, applied = _owner(state, "PUT", apply["path"], apply["body"])
    assert status == 200, applied
    evidence = [catalog.extract_evidence(catalog.KIND_CREWMATE_KEY_RESULTS, "apply", 0, applied)]
    after = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    # Remove A, then recreate [A, B] with identical wording: A comes back under a new id.
    member_goals.remove_key_result(slug, ORDINARY, first_a)
    member_goals.set_key_results(slug, ORDINARY, [b])
    member_goals.set_key_results(slug, ORDINARY, [a, b])
    new_a = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    assert new_a != first_a
    member_goals.record_key_result(slug, ORDINARY, new_a, "9")
    now = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    assert cards.undo_snapshot_changed(catalog.KIND_CREWMATE_KEY_RESULTS, now, after)
    # And the locked compare-and-set refuses it too, if the snapshot check is passed.
    (undo,), _ = catalog.build_undo(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, evidence, 1)
    status, body = _owner(state, "PUT", undo["path"], undo["body"])
    assert status == 409, body
    recreated = member_goals.read_goals(slug, ORDINARY)["key_results"][0]
    assert (recreated["id"], recreated["current"]) == (new_a, "9")


@pytest.mark.parametrize("existing", [False, True])
def test_a_stale_key_results_undo_refuses_a_newer_change_to_goal_links(
    tmp_path: Path,
    existing: bool,
) -> None:
    """A links-only edit must survive both the snapshot and locked Undo checks."""
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_goal(slug, ORDINARY, "Document", by="user")
    g1, g2 = [g["id"] for g in member_goals.read_goals(slug, ORDINARY)["goals"]]
    a = {"text": "PRs described", "target": "27"}
    if existing:
        member_goals.set_key_results(slug, ORDINARY, [a])
    before = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS,
        {"member": ORDINARY, "items": [{**a, "goal_ids": [g1]}]},
    )
    (apply,) = catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, {})["apply"]
    status, applied = _owner(state, "PUT", apply["path"], apply["body"])
    assert status == 200, applied
    kid = applied["key_results"][0]["id"]
    evidence = [catalog.extract_evidence(catalog.KIND_CREWMATE_KEY_RESULTS, "apply", 0, applied)]
    assert evidence == [{"ids": [kid], "links": {kid: [g1]}}]
    after = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    member_goals.set_key_results(slug, ORDINARY, [{**a, "goal_ids": [g2]}])
    member_goals.record_key_result(slug, ORDINARY, kid, "9")
    now = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    assert cards.undo_snapshot_changed(catalog.KIND_CREWMATE_KEY_RESULTS, now, after)
    (undo,), _ = catalog.build_undo(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, evidence, 1)
    assert undo["body"]["expected_links"] == {kid: [g1]}
    status, body = _owner(state, "PUT", undo["path"], undo["body"])
    assert (status, body.get("code")) == (409, "changed_meanwhile"), body
    (kept,) = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert (kept["id"], kept["goal_ids"], kept["current"]) == (kid, [g2], "9")


def test_key_results_undo_ignores_link_order_and_keeps_fresh_readings(
    tmp_path: Path,
    _card_owner,
) -> None:
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_goal(slug, ORDINARY, "Document", by="user")
    g1, g2 = [g["id"] for g in member_goals.read_goals(slug, ORDINARY)["goals"]]
    item = {"text": "PRs described", "target": "27"}
    member_goals.set_key_results(slug, ORDINARY, [{**item, "goal_ids": [g1]}])
    rec = _propose_card(
        state,
        catalog.KIND_CREWMATE_KEY_RESULTS,
        {"member": ORDINARY, "items": [{**item, "goal_ids": [g1, g2]}]},
    )
    assert _run_card_plan(state, rec, "apply") == [200]
    after = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    kid = after[cards.KR_RECORDS_KEY][0]["id"]
    member_goals.set_key_results(slug, ORDINARY, [{**item, "goal_ids": [g2, g1]}])
    member_goals.record_key_result(slug, ORDINARY, kid, "9")
    now = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    assert not cards.undo_snapshot_changed(catalog.KIND_CREWMATE_KEY_RESULTS, now, after)
    assert _run_card_plan(state, rec, "undo") == [200]
    (restored,) = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert (restored["id"], restored["goal_ids"], restored["current"]) == (kid, [g1], "9")
    assert [r["value"] for r in restored["readings"]] == ["9"]


@pytest.mark.parametrize(
    "links", [[], "bad", {"k00000001": None}, {"k00000001": "g00000001"}, {"k00000001": [1]}]
)
def test_key_results_restore_rejects_malformed_expected_links(tmp_path: Path, links: Any) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "PRs described", "target": "27"}])
    before = member_goals.read_goals(slug, ORDINARY)
    status, body = _owner(
        state,
        "PUT",
        f"/api/members/{slug}/goals/key-results",
        {"member": ORDINARY, "items": [], "restore": [], "expected_links": links},
    )
    assert (status, body.get("code")) == (400, "invalid_expected"), body
    assert member_goals.read_goals(slug, ORDINARY) == before


def _propose_card(state: _CardState, kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """A card proposed the way ``propose_change`` builds it; returns its stored record."""
    from kiro_crew.dashboard import change_cards as cards

    params = catalog.validate_params(kind, params)
    before = asyncio.run(cards.read_state(kind, params, [], state=state, app=None))
    preview = catalog.build_preview(kind, params, before, {})
    return state._change_card_store.propose(
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        kind=kind,
        params=params,
        reason="",
        preview=preview,
        before=before,
        context={},
    )


def _run_card_plan(state: _CardState, rec: dict[str, Any], op: str) -> list[int]:
    """Send the card's *op* plan the way the dashboard does; the step statuses."""
    from aiohttp.test_utils import TestClient, TestServer

    card = state._change_card_store.public(rec)

    async def main() -> list[int]:
        client = TestClient(TestServer(_create_card_app(state)))
        await client.start_server()
        try:
            statuses = []
            for i, step in enumerate(card["plan"][op]):
                r = await client.request(
                    step["method"],
                    step["path"],
                    json=step["body"],
                    headers={
                        "X-Card-Id": card["id"],
                        "X-Card-Revision": str(card["revision"]),
                        "X-Card-Op": op,
                        "X-Card-Step": str(i),
                    },
                )
                statuses.append(r.status)
                if r.status >= 400:
                    break
            from kiro_crew.dashboard.handlers import change_cards as card_routes

            if card_routes._BACKGROUND:
                await asyncio.gather(*list(card_routes._BACKGROUND))
            return statuses
        finally:
            await client.close()

    return asyncio.run(main())


def test_a_goal_card_never_shows_an_unscrubbed_previous_goal(tmp_path: Path, _card_owner) -> None:
    """The change line, the Apply ``expected`` and the Undo body are public: scrubbed."""
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    member_goals.set_goal(slug, ORDINARY, f"Ship with {TOKEN}", by="user")
    rec = _propose_card(
        state,
        catalog.KIND_CREWMATE_GOAL,
        {"member": ORDINARY, "long_term": "Ship", "goal_id": _gid(slug, ORDINARY)},
    )
    store = state._change_card_store
    assert TOKEN not in json.dumps(store.public(rec))
    assert TOKEN in json.dumps(rec["before"])  # the raw goal stays gateway-private
    # The route accepts the scrubbed text as the compare-and-set's ``expected``.
    assert _run_card_plan(state, rec, "apply") == [200]
    assert _goal(member_goals.read_goals(slug, ORDINARY)) == "Ship"
    assert rec["status"] == "applied", rec.get("error")
    assert TOKEN not in json.dumps(store.public(rec))
    assert _run_card_plan(state, rec, "undo") == [200]
    # Undo puts the real goal back, read from the card's private snapshot: the
    # public plan only ever carried the scrubbed text.
    restored = _goal(member_goals.read_goals(slug, ORDINARY))
    assert restored == f"Ship with {TOKEN}"
    assert TOKEN not in json.dumps(store.public(rec))


def test_a_key_results_card_undo_keeps_a_reading_recorded_before_apply(
    tmp_path: Path, _card_owner
) -> None:
    """Undo restores the records the Apply replaced, not the ones the preview read."""
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "PRs described", "target": "27"}])
    (original,) = member_goals.read_goals(slug, ORDINARY)["key_results"]
    member_goals.record_key_result(slug, ORDINARY, original["id"], "1")
    rec = _propose_card(
        state,
        catalog.KIND_CREWMATE_KEY_RESULTS,
        {"member": ORDINARY, "items": [{"text": "notes out", "target": "yes"}]},
    )
    member_goals.record_key_result(slug, ORDINARY, original["id"], "2")
    assert _run_card_plan(state, rec, "apply") == [200]
    assert rec["status"] == "applied", rec.get("error")
    assert _run_card_plan(state, rec, "undo") == [200]
    (restored,) = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert restored["id"] == original["id"]
    assert [r["value"] for r in restored["readings"]] == ["1", "2"]
    assert restored["current"] == "2"


def test_two_concurrent_schedule_saves_both_keep_their_check_in(
    tmp_path: Path, _floor_monkeypatch
) -> None:
    """A save that read the record first can never remove the next save's schedule.

    The first request's reconciliation is held until the second request either
    waits for the crewmate's lock or has finished its own reconciliation; without
    the lock it is the second, and the first then reconciles from its stale record.
    """
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    path = f"/api/members/{slug}/goals/schedules"
    real_sync = goal_reminder.sync_schedule
    real_lock = goal_routes._schedule_lock
    calls: list[str] = []

    async def go(c):
        first_syncing = asyncio.Event()
        # Set when the second request waits for the lock, or -- with no lock --
        # once it has finished its own reconciliation.
        second_arrived = asyncio.Event()

        class Watched:
            def __init__(self, lock: asyncio.Lock) -> None:
                self.lock = lock

            async def __aenter__(self) -> None:
                if self.lock.locked():
                    second_arrived.set()
                await self.lock.acquire()

            async def __aexit__(self, *_exc: Any) -> None:
                self.lock.release()

        async def sync(*args: Any, **kwargs: Any) -> None:
            calls.append("sync")
            if len(calls) == 1:
                first_syncing.set()
                await await_until(second_arrived, what="the second schedule save")
                await real_sync(*args, **kwargs)
                return
            await real_sync(*args, **kwargs)
            second_arrived.set()

        _floor_monkeypatch.setattr(goal_reminder, "sync_schedule", sync)
        _floor_monkeypatch.setattr(goal_routes, "_schedule_lock", lambda s: Watched(real_lock(s)))

        async def put(body: dict) -> int:
            r = await c.request("PUT", path, json=body, headers={"X-Test-Auth": "owner"})
            return r.status

        tasks = [asyncio.ensure_future(put({"member": ORDINARY, "daily": True}))]
        try:
            await await_until(first_syncing, what="the first schedule save's reconciliation")
            tasks.append(asyncio.ensure_future(put({"member": ORDINARY, "weekly": True})))
            return await await_until(asyncio.gather(*tasks), what="both schedule saves")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    assert run_guide_app(go, state, extra_routes=OWNER_ROUTES) == [200, 200]
    jobs = _jobs_by_cadence(state, slug)
    assert (len(jobs["daily"]), len(jobs["weekly"]), jobs["monthly"]) == (1, 1, [])


def test_two_key_results_with_the_same_text_and_target_are_refused() -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    twin = [{"text": "PRs described", "target": "27"}] * 2
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_key_results(slug, ORDINARY, twin)
    assert exc.value.code == "duplicate_key_result"
    # The same pair once the record's own cleaning has run is the same pair.
    spaced = [{"text": "PRs described", "target": "27"}, {"text": "PRs\tdescribed", "target": "27"}]
    with pytest.raises(member_goals.GoalError):
        member_goals.set_key_results(slug, ORDINARY, spaced)
    assert member_goals.read_goals(slug, ORDINARY)["key_results"] == []
    for items in (twin, spaced):
        with pytest.raises(catalog.CardCatalogError) as card_exc:
            catalog.validate_params(
                catalog.KIND_CREWMATE_KEY_RESULTS, {"member": ORDINARY, "items": items}
            )
        assert "same text and target" in str(card_exc.value)


def test_goal_card_params_read_as_the_record_stores_them() -> None:
    """A tab, a line break and a format character: the after-read matches the params."""
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    coder = "\U0001f469\u200d\U0001f4bb"
    goal = catalog.validate_params(
        catalog.KIND_CREWMATE_GOAL,
        {"member": ORDINARY, "long_term": f"Ship\tit\nthen {coder} rest"},
    )
    added = member_goals.set_goal(slug, ORDINARY, goal["long_term"], by="user")
    after = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_GOAL)
    assert goal["long_term"] == "Ship it\nthen \U0001f469\U0001f4bb rest"
    # An added goal is found by the id its apply answered with.
    ev = [{"goal_id": added["goal_id"]}]
    assert cards.after_shows_the_write(catalog.KIND_CREWMATE_GOAL, goal, after, ev)
    assert not cards.after_shows_the_write(catalog.KIND_CREWMATE_GOAL, goal, after, [{}])
    krs = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS,
        {"member": ORDINARY, "items": [{"text": f"two\nlines {coder}", "target": "1\u200d0"}]},
    )
    member_goals.set_key_results(slug, ORDINARY, krs["items"])
    after = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    assert krs["items"] == [{"text": "two lines \U0001f469\U0001f4bb", "target": "10"}]
    assert cards.after_shows_the_write(catalog.KIND_CREWMATE_KEY_RESULTS, krs, after, [{}])


def test_card_carrying_scrubbed_text_still_reads_back_as_its_write(
    _floor_monkeypatch,
) -> None:
    """A card that repeats a credential-bearing item in its scrubbed form keeps Undo."""
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, f"use {TOKEN} for releases", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": f"kr {TOKEN}", "target": "1"}])
    shown = member_goals.public_view(member_goals.read_goals(slug, ORDINARY))
    gid = _gid(slug, ORDINARY)
    goal = {"member": ORDINARY, "long_term": _goal(shown), "goal_id": gid}
    member_goals.set_goal(slug, ORDINARY, goal["long_term"], by="user", goal_id=gid)
    after = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_GOAL)
    assert TOKEN in after["goals"][0]["text"]
    assert cards.after_shows_the_write(catalog.KIND_CREWMATE_GOAL, goal, after, [{}])
    kr_shown = shown["key_results"][0]
    items = [
        {"text": kr_shown["text"], "target": kr_shown["target"]},
        {"text": "second", "target": "2"},
    ]
    krs = {"member": ORDINARY, "items": items}
    member_goals.set_key_results(slug, ORDINARY, items)
    after = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    assert TOKEN in after["key_results"][0]["text"]
    assert cards.after_shows_the_write(catalog.KIND_CREWMATE_KEY_RESULTS, krs, after, [{}])
    other = {"member": ORDINARY, "items": [{"text": "else", "target": "2"}]}
    assert not cards.after_shows_the_write(catalog.KIND_CREWMATE_KEY_RESULTS, other, after, [{}])


def _owner_raw(state: FakeState, method: str, path: str, **kw: Any) -> tuple[int, dict]:
    async def go(c):
        r = await c.request(method, path, headers={"X-Test-Auth": "owner"}, **kw)
        return r.status, await r.json()

    return run_guide_app(go, state, extra_routes=OWNER_ROUTES)


@pytest.mark.parametrize(
    "method, suffix, kw, want",
    [
        ("PUT", "/long-term", {"data": b"{not json"}, (400, "invalid_json")),
        ("PUT", "/long-term", {"json": ["a list"]}, (400, "invalid_json")),
        ("PUT", "/long-term", {"json": {"member": ORDINARY}}, (400, "missing_long_term")),
        (
            "PUT",
            "/long-term",
            {"json": {"member": ORDINARY, "long_term": "x", "expected": 3}},
            (400, "invalid_expected"),
        ),
        ("PUT", "/long-term", {"json": {"long_term": "x"}}, (400, "missing_member")),
        (
            "PUT",
            "/long-term",
            {"json": {"member": "nobody", "long_term": "x"}},
            (404, "member_not_found"),
        ),
        ("PUT", "/key-results", {"json": {"member": ORDINARY}}, (400, "missing_items")),
        (
            "PUT",
            "/key-results",
            {"json": {"member": ORDINARY, "items": [], "expected": "x"}},
            (400, "invalid_expected"),
        ),
        (
            "PUT",
            "/key-results",
            {"json": {"member": ORDINARY, "items": [], "restore": "x"}},
            (400, "invalid_restore"),
        ),
        (
            "PUT",
            "/key-results",
            {"json": {"member": ORDINARY, "items": [{"text": "a", "target": "1"}] * 2}},
            (400, "duplicate_key_result"),
        ),
        (
            "PUT",
            "/schedules",
            {"json": {"member": ORDINARY, "timezone": "Mars/Base"}},
            (400, "invalid_timezone"),
        ),
        ("PUT", "/schedules", {"json": {"member": ORDINARY}}, (400, "invalid_schedule")),
        ("GET", "?member=nobody", {}, (404, "member_not_found")),
    ],
)
def test_the_owner_goal_routes_refuse_malformed_requests(
    tmp_path: Path, method: str, suffix: str, kw: dict, want: tuple[int, str]
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    status, body = _owner_raw(state, method, f"/api/members/{slug}/goals{suffix}", **kw)
    assert (status, body.get("code")) == want, body


def test_the_owner_goal_routes_refuse_a_bad_slug_and_an_unreadable_record(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    status, body = _owner_raw(state, "GET", f"/api/members/BAD..slug/goals?member={ORDINARY}")
    assert (status, body["code"]) == (400, "invalid_member_slug")
    path = member_goals.member_goals_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    for method, suffix, kw in (
        ("GET", f"?member={ORDINARY}", {}),
        ("PUT", "/long-term", {"json": {"member": ORDINARY, "long_term": "x"}}),
        ("PUT", "/schedules", {"json": {"member": ORDINARY, "daily": True}}),
    ):
        status, body = _owner_raw(state, method, f"/api/members/{slug}/goals{suffix}", **kw)
        assert (status, body["code"]) == (500, "goals_unreadable"), (method, suffix)


def test_a_schedule_that_cannot_be_written_turns_its_switch_back_off(
    tmp_path: Path, _floor_monkeypatch
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")

    async def broken(*_a: Any, **_kw: Any) -> None:
        raise OSError("cron store down")

    _floor_monkeypatch.setattr(goal_reminder, "sync_schedule", broken)
    status, body = _owner_raw(
        state,
        "PUT",
        f"/api/members/{slug}/goals/schedules",
        json={"member": ORDINARY, "daily": True},
    )
    assert (status, body["code"]) == (503, "schedule_failed")
    assert member_goals.read_goals(slug, ORDINARY)["schedules"]["daily"] is False


@pytest.mark.parametrize(
    "tool, args, path, reply, want",
    [
        ("goal_read", {}, "/api/member/self/goals/read", {"long_term": "Ship"}, '"Ship"'),
        (
            "goal_read",
            {},
            "/api/member/self/goals/read",
            {"error": "not here", "code": "not_a_crewmate"},
            "needs your own chat",
        ),
        (
            "key_result_progress",
            {"id": "k00000001", "current": "3"},
            "/api/member/self/goals/key-result",
            {"ok": True},
            "shows on your Goals tab",
        ),
        (
            "key_result_progress",
            {"id": "k00000001", "current": "3"},
            "/api/member/self/goals/key-result",
            {"error": "no key result has that id", "code": "key_result_not_found"},
            "The reading was not saved",
        ),
        (
            "save_goal_report",
            {"kind": "weekly", "title": "Week 41", "markdown": "# done"},
            "/api/member/self/goals/report",
            {"ok": True, "title": "Week 41", "url": "/artifacts/week-41"},
            "[Week 41](/artifacts/week-41)",
        ),
        (
            "save_goal_report",
            {"kind": "weekly", "title": "Week 41", "markdown": "# done"},
            "/api/member/self/goals/report",
            {"error": "the report could not be saved", "code": "report_save_failed"},
            "The report was not saved",
        ),
    ],
)
def test_the_mate_goal_tools_post_their_route_and_word_the_outcome(
    _floor_monkeypatch, tool: str, args: dict, path: str | None, reply: dict, want: str
) -> None:
    sent: list[tuple[str, dict, str]] = []

    def post(p: str, body: dict, *, session_key: str) -> dict:
        sent.append((p, body, session_key))
        return reply

    _floor_monkeypatch.setattr(mcp_mate, "_strict_session_key", lambda: ("dashboard:m", ""))
    _floor_monkeypatch.setattr(mcp_mate, "_post", post)
    out = mcp_mate._call_tool_inner(tool, args)
    assert want in out, out
    assert [s[0] for s in sent] == ([path] if path else [])
    assert all(s[2] == "dashboard:m" for s in sent)


def test_the_mate_goal_tools_refuse_off_their_own_chat(_floor_monkeypatch) -> None:
    _floor_monkeypatch.setattr(mcp_mate, "_strict_session_key", lambda: ("", "no strict key"))
    out = mcp_mate._call_tool_inner("goal_read", {})
    assert "needs your own chat in the dashboard" in out
    assert mcp_mate._call_tool_inner("nope", {}).startswith("Error: unknown tool")


def test_every_surface_bounds_a_goal_field_by_the_record_s_own_limit() -> None:
    """A card, a tool call and the record all validate one field against one limit."""
    from kiro_crew.validation import MCP_MATE_SCHEMAS

    g = member_goals
    card_goal = catalog.KINDS[catalog.KIND_CREWMATE_GOAL].params_schema["properties"]
    card_krs = catalog.KINDS[catalog.KIND_CREWMATE_KEY_RESULTS].params_schema["properties"]
    assert card_goal["long_term"]["maxLength"] == g.LONG_TERM_MAX_CHARS
    # No cap on how many: only each field's length is bounded.
    assert "maxItems" not in card_krs["items"]
    kr_card = card_krs["items"]["items"]["properties"]
    assert kr_card["text"]["maxLength"] == g.KR_TEXT_MAX_CHARS
    assert kr_card["target"]["maxLength"] == g.KR_VALUE_MAX_CHARS

    def spec(tool: str, name: str) -> int:
        return next(f.max_len for f in MCP_MATE_SCHEMAS[tool].fields if f.name == name)

    assert spec("set_goal", "long_term") == g.LONG_TERM_MAX_CHARS
    assert spec("key_result_progress", "current") == g.KR_READING_MAX_CHARS
    assert spec("save_goal_report", "title") == g.REPORT_TITLE_MAX_CHARS
    assert spec("save_goal_report", "markdown") == g.REPORT_MAX_CHARS
    assert goal_routes.REPORT_MAX_CHARS == g.REPORT_MAX_CHARS

    tools = {t["name"]: t["inputSchema"]["properties"] for t in mcp_mate._tool_definitions()}
    assert tools["set_goal"]["long_term"]["maxLength"] == g.LONG_TERM_MAX_CHARS
    assert "maxItems" not in tools["set_key_results"]["items"]
    kr_tool = tools["set_key_results"]["items"]["items"]["properties"]
    assert kr_tool["text"]["maxLength"] == g.KR_TEXT_MAX_CHARS
    assert kr_tool["target"]["maxLength"] == g.KR_VALUE_MAX_CHARS
    assert tools["key_result_progress"]["current"]["maxLength"] == g.KR_READING_MAX_CHARS
    assert tools["save_goal_report"]["title"]["maxLength"] == g.REPORT_TITLE_MAX_CHARS
    assert tools["save_goal_report"]["markdown"]["maxLength"] == g.REPORT_MAX_CHARS
    assert not {"short_goal_add", "short_goal_update"} & set(tools)


def _flagged(handler):
    """The key-results route as a card's Apply step reaches it (flag set)."""
    from kiro_crew.dashboard.handlers.change_cards import REQ_CARD_KEY_RESULTS_APPLY

    async def route(request):
        request[REQ_CARD_KEY_RESULTS_APPLY] = True
        return await handler(request)

    return route


@pytest.mark.parametrize("checkins_on", [False, True])
def test_confirming_a_milestones_card_always_tells_the_crewmate(
    tmp_path: Path, _floor_monkeypatch, checkins_on: bool
) -> None:
    from kiro_crew.dashboard import goal_setup

    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    if checkins_on:
        member_goals.set_schedules(slug, ORDINARY, {"daily": True})
    told: list[tuple[str, str]] = []
    _floor_monkeypatch.setattr(
        goal_setup, "schedule_milestones_nudge", lambda _st, s, m: told.append((s, m))
    )
    routes = [
        (
            "PUT",
            "/card/{slug}/goals/key-results",
            _flagged(goal_routes.api_member_goals_key_results),
        )
    ]
    body = {"member": ORDINARY, "items": [{"text": "notes out", "target": "yes"}], "expected": []}

    async def go(c):
        r = await c.request(
            "PUT", f"/card/{slug}/goals/key-results", json=body, headers={"X-Test-Auth": "owner"}
        )
        return r.status

    assert run_guide_app(go, state, extra_routes=routes) == 200
    assert told == [(slug, ORDINARY)]


def test_a_goals_page_milestone_edit_sends_no_milestones_nudge(
    tmp_path: Path, _floor_monkeypatch
) -> None:
    from kiro_crew.dashboard import goal_setup

    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    told: list = []
    _floor_monkeypatch.setattr(goal_setup, "schedule_milestones_nudge", lambda *a: told.append(a))
    body = {"member": ORDINARY, "items": [{"text": "notes out", "target": "yes"}], "expected": []}
    assert _owner(state, "PUT", f"/api/members/{slug}/goals/key-results", body)[0] == 200
    assert told == []


def test_only_a_key_results_cards_apply_step_carries_the_confirm_flag() -> None:
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    for kind, op, flagged in [
        (catalog.KIND_CREWMATE_KEY_RESULTS, catalog.OP_APPLY, True),
        (catalog.KIND_CREWMATE_KEY_RESULTS, catalog.OP_UNDO, False),
        (catalog.KIND_CREWMATE_GOAL, catalog.OP_APPLY, False),
    ]:
        request: dict = {}
        card_routes._arm_card_undo_cron_guard(request, {"kind": kind, "before": {}}, op, {})
        assert bool(request.get(card_routes.REQ_CARD_KEY_RESULTS_APPLY)) is flagged, (kind, op)


def test_saving_a_goal_on_the_goals_tab_nudges_the_crewmate_naming_that_goal(
    tmp_path: Path, _floor_monkeypatch
) -> None:
    from kiro_crew.dashboard import goal_setup

    calls: list[tuple] = []
    _floor_monkeypatch.setattr(goal_setup, "schedule_nudge", lambda *a: calls.append(a[1:]))
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    path = f"/api/members/{slug}/goals/long-term"
    _, first = _owner(state, "PUT", path, {"member": ORDINARY, "long_term": "Ship the docs"})
    _, second = _owner(state, "PUT", path, {"member": ORDINARY, "long_term": "Hire two people"})
    # A typo fix is not a new goal; a real rewording of goal 1 is.
    _owner(
        state,
        "PUT",
        path,
        {"member": ORDINARY, "long_term": "Ship the doc", "goal_id": first["goal_id"]},
    )
    _owner(
        state,
        "PUT",
        path,
        {"member": ORDINARY, "long_term": "Cut build time", "goal_id": first["goal_id"]},
    )
    assert calls == [
        (slug, ORDINARY, first["goal_id"]),
        (slug, ORDINARY, second["goal_id"]),
        (slug, ORDINARY, first["goal_id"]),
    ]


def test_removing_a_goal_on_the_goals_tab_nudges_the_crewmate_and_undo_does_not(
    tmp_path: Path, _floor_monkeypatch
) -> None:
    from kiro_crew.dashboard import goal_setup

    removed: list[tuple] = []
    _floor_monkeypatch.setattr(goal_setup, "schedule_nudge", lambda *a: None)
    _floor_monkeypatch.setattr(
        goal_setup, "schedule_removed_nudge", lambda *a: removed.append(a[1:])
    )
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    _, saved = _owner(
        state,
        "PUT",
        f"/api/members/{slug}/goals/long-term",
        {"member": ORDINARY, "long_term": "Ship the docs"},
    )
    gid = saved["goal_id"]
    _owner(state, "DELETE", f"/api/members/{slug}/goals/items/{gid}?member={ORDINARY}", None)
    _owner(state, "POST", f"/api/members/{slug}/goals/items/{gid}/restore", {"member": ORDINARY})
    assert removed == [(slug, ORDINARY, gid)]


def test_the_key_results_card_shows_and_checks_each_milestones_goals() -> None:
    snap = {
        "exists": True,
        "slug": "scout",
        "has_goal": True,
        "goals": [{"id": "g00000001"}, {"id": "g00000002"}],
        "key_results": [{"text": "Docs live", "target": "yes"}],
        "key_results_records": [{"id": "k00000001", "goal_ids": ["g00000001"]}],
    }
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS,
        {
            "member": ORDINARY,
            "items": [
                {"text": "Docs live", "target": "yes", "goal_ids": ["g00000001", "g00000002"]},
                {"text": "Two hires", "target": "2", "goal_ids": ["g00000002"]},
            ],
        },
    )
    preview = catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, params, snap, {})
    (row,) = preview["changes"]
    assert row["add"] == ["Docs live → yes · #1 #2", "Two hires → 2 · #2"]
    assert row["remove"] == ["Docs live → yes · #1"]
    (step,) = preview["apply"]
    assert step["body"]["items"][1]["goal_ids"] == ["g00000002"]
    # A change of links alone is a change; the same links are not.
    same = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS,
        {
            "member": ORDINARY,
            "items": [{"text": "Docs live", "target": "yes", "goal_ids": ["g00000001"]}],
        },
    )
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, same, snap, {})
    assert exc.value.code == "no_change"
    unknown = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS,
        {"member": ORDINARY, "items": [{"text": "x", "target": "1", "goal_ids": ["g0000beef"]}]},
    )
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, unknown, snap, {})
    assert exc.value.code == "not_found"


def _write_after_card_snapshot(monkeypatch, write):
    """Land an owner-store write after the card check, before its handler runs."""
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    original = card_routes._before_value
    calls = []

    async def before_value(*args, **kwargs):
        result = await original(*args, **kwargs)
        await asyncio.to_thread(write)
        calls.append(True)
        return result

    monkeypatch.setattr(card_routes, "_before_value", before_value)
    return calls


@pytest.mark.parametrize("op", ["apply", "undo"])
@pytest.mark.parametrize("race", ["changed", "same", "unrelated"])
def test_check_ins_card_locked_compare_after_snapshot(tmp_path, _card_owner, monkeypatch, op, race):
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    # weekday is a supplied no-op, not a field Apply or Undo should write.
    weekday = member_goals.read_goals(slug, ORDINARY)["schedules"]["weekday"]
    card = _propose_check_ins(state, {"time": "08:30", "weekday": weekday})
    if op == "undo":
        assert _run_card(state, card["id"], "apply")[0][0] == 200
    baseline = member_goals.read_goals(slug, ORDINARY)["schedules"]
    concurrent = (
        {"weekday": 4, "month_day": 12, "timezone": "UTC"}
        if race == "unrelated"
        else {"time": "10:00" if race == "changed" else baseline["time"]}
    )
    calls = _write_after_card_snapshot(
        monkeypatch, lambda: member_goals.set_schedules(slug, ORDINARY, concurrent)
    )
    ((status, body),) = _run_card(state, card["id"], op)
    assert calls == [True]
    after = member_goals.read_goals(slug, ORDINARY)["schedules"]
    if race == "changed":
        assert (status, body["code"]) == (409, "changed_meanwhile"), body
        assert after == {**baseline, **concurrent}
    else:
        assert status == 200, body
        assert after == {
            **baseline,
            **concurrent,
            "time": "08:30" if op == "apply" else "09:00",
        }


@pytest.mark.parametrize("race", ["links", "recreated", "reading", "same"])
def test_milestones_card_locked_compare_after_snapshot(tmp_path, _card_owner, monkeypatch, race):
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    first = member_goals.set_goal(slug, ORDINARY, "Ship", by="user")["goal_id"]
    second = member_goals.set_goal(slug, ORDINARY, "Document", by="user")["goal_id"]
    item = {"text": "PRs described", "target": "27", "goal_ids": [first]}
    member_goals.set_key_results(slug, ORDINARY, [item])
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    member_goals.record_key_result(slug, ORDINARY, kid, "1")
    rec = _propose_card(
        state,
        catalog.KIND_CREWMATE_KEY_RESULTS,
        {"member": ORDINARY, "items": [{**item, "goal_ids": [first, second]}]},
    )
    landed = []

    def write():
        if race == "reading":
            member_goals.record_key_result(slug, ORDINARY, kid, "2")
        else:
            if race == "recreated":
                member_goals.set_key_results(slug, ORDINARY, [])
            member_goals.set_key_results(
                slug, ORDINARY, [{**item, "goal_ids": [second]}] if race == "links" else [item]
            )
        landed.append(member_goals.read_goals(slug, ORDINARY)["key_results"])

    calls = _write_after_card_snapshot(monkeypatch, write)
    ((status, body),) = _run_card(state, rec["id"], "apply")
    assert calls == [True]
    after = member_goals.read_goals(slug, ORDINARY)["key_results"]
    if race in ("links", "recreated"):
        assert (status, body["code"]) == (409, "changed_meanwhile"), body
        assert after == landed[0]
    else:
        assert status == 200, body
        assert after == [{**landed[0][0], "goal_ids": [first, second]}]
        assert [r["value"] for r in after[0]["readings"]] == (
            ["1", "2"] if race == "reading" else ["1"]
        )


@pytest.mark.parametrize("changed", [True, False])
def test_schedule_failed_sync_rollback_compares_its_write(tmp_path, monkeypatch, changed):
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")

    async def broken(*args, **kwargs):
        # Another process can save despite the route's per-loop schedule lock.
        await asyncio.to_thread(
            member_goals.set_schedules,
            slug,
            ORDINARY,
            {"daily": True, "time": "10:00" if changed else "09:00"},
        )
        raise OSError("cron store down")

    monkeypatch.setattr(goal_reminder, "sync_schedule", broken)
    status, body = _owner_raw(
        state,
        "PUT",
        f"/api/members/{slug}/goals/schedules",
        json={"member": ORDINARY, "daily": True},
    )
    assert (status, body["code"]) == (503, "schedule_failed")
    schedules = member_goals.read_goals(slug, ORDINARY)["schedules"]
    assert schedules["daily"] is changed
    assert schedules["time"] == ("10:00" if changed else "09:00")


@pytest.mark.parametrize(
    "expected",
    [[], {}, {"time": 9}, {"daily": 1}, {"weekday": True}, {"other": "09:00"}],
)
def test_schedule_expectation_shape_is_refused(tmp_path, expected):
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    before = member_goals.read_goals(slug, ORDINARY)
    status, body = _owner_raw(
        state,
        "PUT",
        f"/api/members/{slug}/goals/schedules",
        json={"member": ORDINARY, "time": "08:30", "expected": expected},
    )
    assert (status, body["code"]) == (400, "invalid_expected"), body
    assert member_goals.read_goals(slug, ORDINARY) == before


@pytest.mark.parametrize(
    "expectation",
    [
        {"expected": [None]},
        {"expected": [{"text": "PRs described"}]},
        {"expected_ids": ["bad"]},
        {"expected_links": {"bad": []}},
        {"expected_links": {"k00000001": [None]}},
        {"expected_ids": [], "expected_links": {"k00000001": []}},
    ],
)
def test_milestone_apply_expectation_shape_is_refused(tmp_path, expectation):
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    before = member_goals.read_goals(slug, ORDINARY)
    status, body = _owner_raw(
        state,
        "PUT",
        f"/api/members/{slug}/goals/key-results",
        json={"member": ORDINARY, "items": [], **expectation},
    )
    assert (status, body["code"]) == (400, "invalid_expected"), body
    assert member_goals.read_goals(slug, ORDINARY) == before
