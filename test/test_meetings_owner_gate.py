"""The Meetings write routes that change a meeting or drive its agents are owner-only.

The dictionary and calendar-sync writes stay open: the meetings skill tells
agents to call them over the internal transport, which carries no owner claim.

An allow-listed channel user holds a dashboard token whose ``app`` claim is ``""``
but whose subject is not the owner; an app token carries a non-empty ``app``.
Both must be refused by ``_common.require_owner`` before a handler runs, while
the owner still reaches every route and the read routes stay open.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from meetings_helpers import (  # noqa: F401
    app_fixture,
    client_for,
    enabled_fixture,
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend.routes import _common

BASE = k.API_BASE
OWNER = "owner-sub"
MID = "standup"

pytestmark = pytest.mark.asyncio

GATED = {
    ("PUT", "/config"): "meetings.put_config",
    ("PUT", "/calendar/credentials"): "meetings.calendar_credentials",
    ("POST", "/calendar/credentials/forget"): "meetings.calendar_disconnect",
    ("POST", "/calendar/oauth/start"): "meetings.calendar_oauth_start",
    ("PATCH", "/meetings/{meeting_id}"): "meetings.rename",
    ("DELETE", "/meetings/{meeting_id}"): "meetings.delete",
    ("POST", "/meetings/{meeting_id}/init"): "meetings.init",
    ("POST", "/meetings/{meeting_id}/start"): "meetings.start",
    ("POST", "/meetings/{meeting_id}/dispatch"): "meetings.dispatch",
    ("POST", "/meetings/{meeting_id}/message"): "meetings.message",
    ("PUT", "/meetings/{meeting_id}/outputs"): "meetings.put_output",
    ("DELETE", "/meetings/{meeting_id}/outputs"): "meetings.delete_output",
    ("POST", "/meetings/{meeting_id}/tasks"): "meetings.add_task",
    ("PATCH", "/meetings/{meeting_id}/tasks"): "meetings.update_task",
    ("DELETE", "/meetings/{meeting_id}/tasks"): "meetings.delete_task",
    ("POST", "/meetings/{meeting_id}/tasks/file"): "meetings.file_task",
    ("POST", "/meetings/{meeting_id}/tasks/review"): "meetings.review_task",
    ("POST", "/meetings/{meeting_id}/status"): "meetings.status",
    ("POST", "/meetings/{meeting_id}/stop"): "meetings.stop",
    ("POST", "/meetings/{meeting_id}/attachments"): "meetings.attachments",
    ("POST", "/meetings/{meeting_id}/agents"): "meetings.toggle_agent",
    ("POST", "/meetings/{meeting_id}/mute"): "meetings.mute_agent",
    ("POST", "/meetings/{meeting_id}/reset"): "meetings.reset_agents",
}

CALLERS = {
    "non_owner": {"X-Test-User": "channel-user"},
    "meetings_app_token": {"X-Test-App": "meetings"},
    "other_app_token": {"X-Test-App": "other-app"},
}


def _wire(app: web.Application) -> web.Application:
    class _State:
        owner_id = OWNER
        sessions = None
        context_builder = None

    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", OWNER)
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app["state"] = _State()
    app.middlewares.append(_identity)
    return app


def _url(template: str) -> str:
    return BASE + template.replace("{meeting_id}", MID)


@pytest.fixture(name="records")
def records_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        _common,
        "audit",
        lambda op, resource, *, outcome, error="": seen.append((op, resource, outcome)),
    )
    return seen


@pytest.mark.parametrize("who", sorted(CALLERS))
@pytest.mark.parametrize("route_key", sorted(GATED), ids=lambda key: f"{key[0]} {key[1]}")
async def test_a_non_owner_caller_is_refused_before_the_handler(app, records, route_key, who):
    method, template = route_key
    reached: list[str] = []
    for resource in app.router.resources():
        if resource.canonical == BASE + template:
            for route in resource:
                if route.method == method:
                    reached.append(route.method)
    assert reached == [method], f"{method} {template} is not registered"

    async with client_for(_wire(app)) as client:
        resp = await client.request(method, _url(template), headers=CALLERS[who], json={})
        body = await resp.json()
    assert resp.status == 403
    assert body["code"] == "dashboard_owner_required"
    assert (GATED[route_key], f"{_url(template)} reason:non-owner", "denied") in records
    # The refusal happens before the handler: no allow, and no handler-level record.
    assert [r for r in records if r[2] != "denied"] == []


@pytest.mark.parametrize("route_key", sorted(GATED), ids=lambda key: f"{key[0]} {key[1]}")
async def test_the_owner_passes_the_gate_and_it_is_audited(app, records, route_key):
    method, template = route_key
    async with client_for(_wire(app)) as client:
        resp = await client.request(method, _url(template), json={})
        text = await resp.text()
    assert "dashboard_owner_required" not in text, text
    assert (GATED[route_key], f"{_url(template)} owner-check", "allowed") in records


async def test_only_the_named_write_routes_carry_the_gate(app, records):
    """Probe every registered route as a non-owner and collect the ones refused.

    Pins both directions: a named write route that loses its gate, and a read
    route (or an unnamed write route) that gains one, both change this set.
    ``/import`` gates itself inline with the same predicate and denial code.
    """
    refused: set[tuple[str, str]] = set()
    async with client_for(_wire(app)) as client:
        for resource in app.router.resources():
            template = resource.canonical[len(BASE) :]
            for route in resource:
                if route.method == "HEAD":
                    continue
                resp = await client.request(
                    route.method, _url(template), headers=CALLERS["non_owner"], json={}
                )
                if resp.status == 403 and "dashboard_owner_required" in await resp.text():
                    refused.add((route.method, template))
    assert refused == set(GATED) | {("POST", "/meetings/{meeting_id}/import")}


@pytest.mark.parametrize(
    "template",
    ["/config", "/dictionary", "/meetings", "/agents", "/status", "/task-providers"],
)
async def test_the_owner_still_reads(app, template):
    async with client_for(_wire(app)) as client:
        resp = await client.get(_url(template))
    assert resp.status == 200, await resp.text()
