"""The cold-start welcome's recap: ``member_recap`` and ``GET /api/members/{slug}/recap``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import member_recap
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.members import write_dm_binding

CREW = "oncall"


def _session(key, title, modified, agent=CREW, **extra):
    return {"key": key, "title": title, "modified": modified, "agent": agent, **extra}


class TestRecentSessions:
    def test_newest_first_capped_and_only_this_crewmate(self):
        rows = [_session(f"dashboard_chat-{i}", f"task {i}", float(i)) for i in range(5)]
        rows.append(_session("dashboard_chat-x", "someone else's", 99.0, agent="other"))
        picked = member_recap.recent_sessions(rows, CREW)
        assert [r["title"] for r in picked] == ["task 4", "task 3", "task 2"]

    def test_skips_member_threads_incognito_and_untitled_rows(self):
        rows = [
            _session("dashboard_member-oncall", "the thread itself", 5.0),
            _session("dashboard_chat-1", "private", 4.0, memory_mode="incognito"),
            _session("dashboard_chat-2", "dashboard_chat-2", 3.0),
            _session("dashboard_chat-3", "kept", 2.0),
        ]
        assert [r["title"] for r in member_recap.recent_sessions(rows, CREW)] == ["kept"]


class TestRecap:
    def test_open_goals_are_paused_and_never_listed_twice(self):
        thread = {"goal": "Rotate the pager keys", "phase": "waiting", "next": "confirm with Sam"}
        recent = [_session("chat-1", "Fix flaky alarm", 2.0), _session("chat-2", "Old title", 1.0)]
        ledgers = [{}, {"goal": "Migrate dashboards", "phase": "implementing", "next": ""}]
        out = member_recap.recap(thread, recent, ledgers)
        assert out["paused"] == [
            {"goal": "Rotate the pager keys", "next": "confirm with Sam"},
            {"goal": "Migrate dashboards", "next": ""},
        ]
        assert out["recent"] == [{"title": "Fix flaky alarm", "ts": 2.0}]

    def test_finished_goals_are_not_paused(self):
        for phase in ("done", "abandoned"):
            assert member_recap.open_goal({"goal": "Ship it", "phase": phase}) is None
        assert member_recap.open_goal({"goal": "", "phase": "working"}) is None

    def test_a_key_with_no_footer_is_redacted_before_the_fold(self):
        # Short enough that no length-based detector catches it after the fold.
        body = "MC4CAQAwBQYDK2VwBCIE"
        # The header is assembled so this file's own text carries no key marker.
        header = "-" * 5 + "BEGIN " + "PRIVATE" + " KEY" + "-" * 5
        pem = "rotate\n" + header + "\n" + body + "\n"
        line = member_recap._clip(pem)
        assert body not in line
        assert "\n" not in line

    def test_items_are_one_redacted_capped_line(self):
        line = member_recap._clip("deploy\n  with  AKIAIOSFODNN7EXAMPLE " + "x" * 300)
        assert "\n" not in line
        assert "AKIAIOSFODNN7EXAMPLE" not in line
        assert len(line) <= member_recap.MAX_ITEM_CHARS


def _app(log) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_member_recap

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = request.headers.get("X-Test-User", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = SimpleNamespace(conversation_log=log, _slots={})
    app.router.add_get("/api/members/{slug}/recap", api_member_recap)
    return app


def _config():
    return SimpleNamespace(
        agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew")},
        default_agent=CREW,
        memory_stores={},
    )


class TestRecapRoute:
    @pytest.mark.asyncio
    async def test_returns_the_thread_goal_and_recent_sessions(self):
        write_dm_binding("oncall", member=CREW, slot_key="member-oncall")
        log = SimpleNamespace(
            list_sessions=lambda: [_session("dashboard_chat-1", "Fix alarm", 1.0)]
        )
        ledgers = {"member-oncall": {"goal": "Rotate keys", "phase": "working", "next": ""}}
        with (
            patch(
                "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=_config()
            ),
            patch(
                "kiro_crew.session_ledger.read_state", side_effect=lambda k, *a: ledgers.get(k, {})
            ),
        ):
            async with TestClient(TestServer(_app(log))) as client:
                resp = await client.get("/api/members/oncall/recap", params={"member": CREW})
                assert resp.status == 200
                data = await resp.json()
        assert data == {
            "slug": "oncall",
            "member": CREW,
            "paused": [{"goal": "Rotate keys", "next": ""}],
            "recent": [{"title": "Fix alarm", "ts": 1.0}],
        }

    @pytest.mark.asyncio
    async def test_unknown_member_and_missing_name_are_refused(self):
        log = SimpleNamespace(list_sessions=lambda: [])
        with patch(
            "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=_config()
        ):
            async with TestClient(TestServer(_app(log))) as client:
                assert (await client.get("/api/members/oncall/recap")).status == 400
                other = await client.get("/api/members/nobody/recap", params={"member": "nobody"})
                assert other.status == 404

    @pytest.mark.asyncio
    async def test_a_non_owner_caller_is_refused(self):
        log = SimpleNamespace(list_sessions=lambda: [])
        with patch(
            "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=_config()
        ):
            async with TestClient(TestServer(_app(log))) as client:
                resp = await client.get(
                    "/api/members/oncall/recap",
                    params={"member": CREW},
                    headers={"X-Test-User": "someone-else"},
                )
                assert resp.status in (401, 403)
