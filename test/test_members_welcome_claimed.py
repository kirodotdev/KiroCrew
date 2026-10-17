"""Mate's roster row says whether its once-only welcome marker is claimed.

The Crewmates page lands on Mate only while the marker is unclaimed and the
thread is empty (rfc-mate-landing-once): a welcome that was claimed and then
failed leaves the thread empty for good, so the page needs the marker to stop
landing there.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    return app


def _fake_config():
    return SimpleNamespace(
        agents={
            "mate": KiroCrewAgentConfig(kiro_agent="kirocrew", source="builtin"),
            "alpha": KiroCrewAgentConfig(kiro_agent="alpha"),
        },
        default_agent="",
        memory_stores={},
        degraded_sections=frozenset(),
    )


async def _rows(state) -> dict[str, dict]:
    async with TestClient(TestServer(_members_app(state))) as client:
        body = await (await client.get("/api/members")).json()
    return {r["name"]: r for r in body["members"]}


@pytest.mark.asyncio
async def test_mate_row_reports_the_welcome_marker_before_and_after_the_claim(tmp_path):
    from kiro_crew import members as members_mod
    from kiro_crew.dashboard.mate_welcome import claim_greeting_marker

    fake = _fake_config()
    state = _make_state(tmp_path)
    with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=fake):
        slug = members_mod.member_slug("mate", fake)
        unowed = await _rows(state)
        assert members_mod.mark_welcome_owed("mate", config=fake) is True
        before = await _rows(state)
        assert claim_greeting_marker(slug, f"member-{slug}") is True
        after = await _rows(state)
    # A Mate created before the owed record (or whose write failed) owes none.
    assert unowed["mate"]["welcome_owed"] is False
    assert before["mate"]["welcome_owed"] is True
    assert before["mate"]["welcome_claimed"] is False
    # Claimed and the thread still empty: the failed-welcome case.
    assert after["mate"]["welcome_claimed"] is True
    assert after["mate"]["has_dm_message"] is False
    # Only Mate's row carries the field.
    assert "welcome_claimed" not in before["alpha"]
    assert "welcome_owed" not in before["alpha"]
    assert "welcome_claimed" not in after["alpha"]


@pytest.mark.asyncio
async def test_a_marker_that_cannot_be_checked_leaves_the_field_off(tmp_path):
    fake = _fake_config()
    state = _make_state(tmp_path)
    with (
        patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=fake),
        patch(
            "kiro_crew.dashboard.mate_welcome.greeting_marker_path",
            side_effect=OSError("unreadable"),
        ),
    ):
        rows = await _rows(state)
    assert "welcome_claimed" not in rows["mate"]
