"""Shared fixtures and helpers for the crewmate Goals tests (``test_member_goals*.py``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from guide_route_helpers import FakeState, agent, run_guide_app

from kiro_crew import member_goals
from kiro_crew import members as members_mod
from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, _invalidate_config_cache
from kiro_crew.config.paths import data_home
from kiro_crew.cron import CronService
from kiro_crew.dashboard import goal_reminder
from kiro_crew.dashboard.handlers import guide as guide_routes
from kiro_crew.dashboard.handlers import member_goals as goal_routes
from kiro_crew.dashboard.handlers import member_self as self_routes

#: A credential-shaped string the goal text must never carry through.
TOKEN = "ghp_" + "a" * 36
DEFAULT = "kirocrew"


ORDINARY = "scout"


READ = ("POST", "/api/member/self/goals/read", goal_routes.api_member_self_goals_read)


PROPOSE = ("POST", "/api/member/self/propose", self_routes.api_member_self_propose)


OWNER_ROUTES = [
    ("GET", "/api/members/{slug}/goals", goal_routes.api_member_goals_get),
    ("PUT", "/api/members/{slug}/goals/long-term", goal_routes.api_member_goals_long_term),
    ("PUT", "/api/members/{slug}/goals/schedules", goal_routes.api_member_goals_schedules),
    ("PUT", "/api/members/{slug}/goals/key-results", goal_routes.api_member_goals_key_results),
    (
        "DELETE",
        "/api/members/{slug}/goals/key-results/{kr_id}",
        goal_routes.api_member_goals_key_result_delete,
    ),
    (
        "POST",
        "/api/members/{slug}/goals/key-results/{kr_id}/restore",
        goal_routes.api_member_goals_key_result_restore,
    ),
    (
        "DELETE",
        "/api/members/{slug}/goals/items/{goal_id}",
        goal_routes.api_member_goals_goal_delete,
    ),
    (
        "POST",
        "/api/members/{slug}/goals/items/{goal_id}/restore",
        goal_routes.api_member_goals_goal_restore,
    ),
]


KR = ("POST", "/api/member/self/goals/key-result", goal_routes.api_member_self_key_result)


REPORT = ("POST", "/api/member/self/goals/report", goal_routes.api_member_self_goal_report)


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch):
    class _Null:
        def log_api_access(self, **_kw):
            return None

    _floor_monkeypatch.setattr(guide_routes, "sel", lambda: _Null())


@pytest.fixture(autouse=True)
def _owner_ok(_floor_monkeypatch):
    async def allow(_request, _operation):
        return None

    _floor_monkeypatch.setattr(goal_routes, "require_owner_dashboard_request", allow)


def _seed() -> None:
    cfg = KiroCrewConfig()
    cfg.agents = {
        DEFAULT: KiroCrewAgentConfig(kiro_agent=DEFAULT),
        ASSISTANT_MEMBER_NAME: KiroCrewAgentConfig(kiro_agent=DEFAULT),
        ORDINARY: KiroCrewAgentConfig(kiro_agent=DEFAULT, display_name="Scout"),
    }
    cfg.default_agent = DEFAULT
    cfg.save()
    _invalidate_config_cache()


def _slug(member: str) -> str:
    _invalidate_config_cache()
    return members_mod.member_slug(member)


def _thread(state: FakeState, member: str, *, user_sent: bool = True):
    slug = _slug(member)
    key = members_mod.DM_SLOT_KEY_PREFIX + slug
    members_mod.write_dm_binding(slug, member=member, slot_key=key)
    slot = state.open_slot(key)
    slot.mode = members_mod.DM_SLOT_MODE
    slot.agent = member
    slot._turn_user_sent = user_sent
    return slot


def _call(state: FakeState, route, body: dict, key: str) -> tuple[int, dict]:
    method, path, _ = route

    async def go(c):
        r = await c.request(method, path, json=body, headers=agent(f"dashboard:{key}"))
        return r.status, await r.json()

    return run_guide_app(go, state, extra_routes=[route])


def _goal(rec: dict) -> str:
    """The first goal's text, or "" with no goal (records and public views alike)."""
    return rec["goals"][0]["text"] if rec["goals"] else ""


def _gid(slug: str, member: str, n: int = 0) -> str:
    return member_goals.read_goals(slug, member)["goals"][n]["id"]


class _Crons:
    """A real cron store in the test's data home, served on the app's loop."""

    def __init__(self, base: Path) -> None:
        self.svc = CronService(base_dir=base)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.svc, name)


def _owner(state: FakeState, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    async def go(c):
        r = await c.request(method, path, json=body, headers={"X-Test-Auth": "owner"})
        return r.status, await r.json()

    return run_guide_app(go, state, extra_routes=OWNER_ROUTES)


def _rules_dir() -> Path:
    return data_home() / "member-records" / members_mod.RULES_DIR_NAME


def _reminder_state(tmp_path: Path) -> FakeState:
    state = FakeState()
    state.crons = _Crons(tmp_path / "crons")  # type: ignore[attr-defined]
    return state


def _schedules(state: FakeState, slug: str, cadence: str = "daily") -> list[Any]:
    key = goal_reminder.managed_key(slug, cadence)
    return [j for j in state.crons.svc.list_jobs(include_disabled=True) if j.managed_by == key]


def _jobs_by_cadence(state: FakeState, slug: str) -> dict[str, list[Any]]:
    return {c: _schedules(state, slug, c) for c in member_goals.CADENCES}
