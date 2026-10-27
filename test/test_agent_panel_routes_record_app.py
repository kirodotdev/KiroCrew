"""The installed app's record door: ``POST /api/member-records/{slug}``.

An installed app appends one custom record to the crewmate the path names; it goes
through the SAME door a script cron's record does, so the entry, the caps, the quota and
the ``custom`` fold are the cron door's. These tests pin what is this route's own: who
may call it (an app token and nothing else), how the crewmate is resolved from the path,
the ``<app>__<type>`` key the record is stored under, that ``writer`` is the token's app
claim and never the body's, and that the route sits behind a ``permissions.api`` grant
rather than inside the app's own namespace.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from agent_panel_routes_helpers import (  # noqa: F401 -- fixtures are used by name
    CREW,
    SLUG,
    _crew_log_on,
    _crew_slot,
    _mounted,
    _unit_for,
)
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from off_loop_helpers import off_loop

from kiro_crew import members as members_mod
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.dashboard import token_auth_app_scope
from kiro_crew.dashboard.handlers import agent_panel as routes
from kiro_crew.dashboard.handlers import (
    agent_panel_record,
    agent_panel_record_app,
    member_dashboard,
)

pytestmark = pytest.mark.asyncio

APP = "some-app"
#: The app part of a stored key: the kebab id with its hyphens written as underscores.
APP_KEY = "some_app"
RECORD_PATH = f"/api/member-records/{SLUG}"
CRON_RECORD_PATH = "/api/agent-panel/dashboard/record"


def _as_app(app_name: str | None):
    """The mark ``token_auth_middleware`` leaves for an app-token caller, or none for
    the owner's own cookie."""

    @web.middleware
    async def _mw(request, handler):
        if app_name is not None:
            request["app"] = app_name
        return await handler(request)

    return _mw


def _app_client(app_name: str | None = APP, *, internal: bool = False) -> TestClient:
    app = _mounted(internal=internal)
    app.middlewares.insert(0, _as_app(app_name))
    return TestClient(TestServer(app))


@pytest.fixture(name="installed_app")
def installed_app_fixture(monkeypatch):
    """A crewmate ``SLUG`` resolves to, with a live DM unit, and a fresh quota window.

    The slug lookup is stubbed at the seam the route reads it through (the config holds
    no such crew in a test data home); what is under test is the route's own resolution
    of the path into a writer, not the config loader.
    """
    monkeypatch.setattr(members_mod, "crew_panel_enabled", lambda: True)
    monkeypatch.setattr(
        routes, "_member_names_for_slug", lambda _cfg, slug: [CREW] if slug == SLUG else []
    )
    unit = _unit_for(CREW)
    monkeypatch.setattr(member_dashboard, "_newest_unit", lambda _slot: unit)
    agent_panel_record._recent_records.clear()
    return unit


async def _post(c: TestClient, body: Any, path: str = RECORD_PATH):
    return await c.post(path, json=body)


def _custom_fold() -> dict[str, Any]:
    crew_log_projection.forget_slot_folds()
    value = off_loop(crew_log_projection.read_slot_projection, _crew_slot(CREW), "custom").value
    assert isinstance(value, dict)
    return value


# ----------------------------------------------------------------- who may write


async def test_the_owners_own_cookie_is_refused(installed_app):
    """The door carries an APP's provenance, so a caller with no app claim -- the owner's
    cookie -- is refused; the owner writes a page value through ``dashboard_write``."""
    async with _app_client(None) as c:
        resp = await _post(c, {"type": "scan", "data": {"n": 1}})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "app_token_required"


async def test_an_internal_caller_with_no_app_claim_is_refused(installed_app):
    """A cron or MCP caller holds the internal secret and no app claim; its door is the
    cron one, and this route does not become a second way in for it."""
    async with _app_client(None, internal=True) as c:
        resp = await _post(c, {"type": "scan", "data": {"n": 1}})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "app_token_required"


async def test_the_door_closes_with_the_crew_panel_switch(installed_app, monkeypatch):
    monkeypatch.setattr(members_mod, "crew_panel_enabled", lambda: False)
    async with _app_client() as c:
        resp = await _post(c, {"type": "scan", "data": {"n": 1}})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "crew_panel_disabled"


async def test_a_slug_no_crewmate_has_is_a_404(installed_app):
    async with _app_client() as c:
        resp = await _post(c, {"type": "scan", "data": {"n": 1}}, "/api/member-records/nobody")
        assert resp.status == 404, await resp.text()
        assert (await resp.json())["code"] == "member_not_found"


async def test_a_malformed_slug_is_a_404_not_a_lookup(installed_app, monkeypatch):
    """A slug that fails ``validate_slug`` never reaches the config scan."""
    looked_up: list[str] = []
    monkeypatch.setattr(
        routes, "_member_names_for_slug", lambda _cfg, slug: looked_up.append(slug) or []
    )
    async with _app_client() as c:
        resp = await _post(
            c, {"type": "scan", "data": {"n": 1}}, "/api/member-records/Not%20A%20Slug"
        )
        assert resp.status == 404, await resp.text()
    assert looked_up == []


async def test_a_slug_two_crewmates_share_is_a_409(installed_app, monkeypatch):
    """An entry landing in the wrong crewmate's log is the outcome the check refuses."""
    monkeypatch.setattr(routes, "_member_names_for_slug", lambda _cfg, _slug: [CREW, "Fleet Crew"])
    async with _app_client() as c:
        resp = await _post(c, {"type": "scan", "data": {"n": 1}})
        assert resp.status == 409, await resp.text()
        assert (await resp.json())["code"] == "ambiguous_slug"


async def test_the_cron_door_still_turns_an_app_token_away(installed_app):
    """Two doors, two resolvers: an app holding a token does not get the cron door too."""
    async with _app_client(internal=True) as c:
        resp = await _post(c, {"type": "scan", "data": {"n": 1}}, CRON_RECORD_PATH)
        assert resp.status == 404, await resp.text()


# ------------------------------------------------------------------- the grant


def test_the_route_is_behind_a_permissions_api_grant_not_the_apps_own_namespace(monkeypatch):
    """Owning ``/api/apps/<name>`` grants nothing here; only the declared and approved
    ``permissions.api`` entry does. Pinned against the scope gate itself."""
    path = RECORD_PATH
    assert not token_auth_app_scope._app_owns_path(APP, path)
    monkeypatch.setattr(token_auth_app_scope, "_app_api_allowlist", lambda _name: ())
    assert token_auth_app_scope.app_token_path_allowed(APP, path) is False
    monkeypatch.setattr(
        token_auth_app_scope, "_app_api_allowlist", lambda _name: ("/api/member-records/*",)
    )
    assert token_auth_app_scope.app_token_path_allowed(APP, path) is True
    # The grant reaches the record routes and nothing beside them.
    assert token_auth_app_scope.app_token_path_allowed(APP, "/api/member-records") is True
    assert token_auth_app_scope.app_token_path_allowed(APP, "/api/members/x/panel") is False


# ------------------------------------------------------------ what a record may be


@pytest.mark.parametrize(
    "body, code",
    [
        (["a", "list"], "invalid_body"),
        ({"data": {"n": 1}}, "bad_record_type"),
        ({"type": "scan/now", "data": {"n": 1}}, "bad_record_type"),
        # A page's path segment admits neither '.' nor '-', so an app's type takes neither.
        ({"type": "stock.check", "data": {"n": 1}}, "bad_record_type"),
        ({"type": "pr-status", "data": {"n": 1}}, "bad_record_type"),
        # '__' is the key separator, reserved from every caller.
        ({"type": "pr__status", "data": {"n": 1}}, "bad_record_type"),
        ({"type": "scan"}, "invalid_record"),
        ({"type": "scan", "data": [1]}, "invalid_record"),
        ({"type": "scan", "data": {"blob": "n" * 9000}}, "record_too_large"),
    ],
)
async def test_a_malformed_record_is_refused_before_anything_is_appended(
    installed_app, monkeypatch, body, code
):
    appended: list[Any] = []
    monkeypatch.setattr(
        crew_log_emit, "on_dashboard_custom", lambda *a, **k: appended.append(a) or True
    )
    async with _app_client() as c:
        resp = await _post(c, body)
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == code
    assert appended == []


async def test_the_prefixed_key_is_what_has_to_fit_the_type_rule(installed_app, monkeypatch):
    """A type that fits on its own but not once the app id is in front is refused, and
    the refusal names the stored key so the app knows what to shorten."""
    appended: list[Any] = []
    monkeypatch.setattr(
        crew_log_emit, "on_dashboard_custom", lambda *a, **k: appended.append(a) or True
    )
    name = "s" * 60  # fits alone; "some_app__" + 60 is 70
    async with _app_client() as c:
        resp = await _post(c, {"type": name, "data": {"n": 1}})
        assert resp.status == 400, await resp.text()
        body = await resp.json()
        assert body["code"] == "bad_record_type"
        assert f"{APP_KEY}__{name}" in body["error"]
    assert appended == []


def test_the_key_splits_one_way_for_every_kebab_id_and_type():
    """``app_record_type`` is injective over kebab ids and ``[A-Za-z0-9_]+`` types: the
    pairs a plain hyphen join would have collapsed stay apart, and the first ``__`` is
    always the separator."""
    key = agent_panel_record_app.app_record_type
    assert key("github", "pr_status") == "github__pr_status"
    assert key("github-pr", "status") == "github_pr__status"
    assert key("some-app", "scan") != key("some", "app_scan")
    assert key("a", "_b") == "a___b"
    assert key("a", "_b") != key("a_b", "")  # and the latter type is refused anyway
    for app_name, type_ in (("github", "pr_status"), ("github-pr", "status"), ("a", "_b")):
        stored = key(app_name, type_)
        head, sep, tail = stored.partition("__")
        assert sep and head == app_name.replace("-", "_") and tail == type_
        assert re.fullmatch(r"[A-Za-z0-9_]+", stored), stored  # one bindable path segment


async def test_the_cron_door_cannot_spell_an_apps_key(installed_app, monkeypatch):
    """A cron's type may not contain ``__``, so no cron record lands on an app's key."""
    from agent_panel_routes_helpers import _client

    monkeypatch.setattr(members_mod, "crew_panel_enabled", lambda: True)
    refused = agent_panel_record._record_body_refusal({"type": "some_app__scan", "data": {}})
    assert refused is not None and refused[0] == "bad_record_type"
    assert "reserved" in refused[1]
    async with _client() as c:
        resp = await c.post(
            CRON_RECORD_PATH,
            json={"type": "some_app__scan", "data": {"n": 1}},
            headers={"X-Session-Key": "cron:stock-poll"},
        )
        assert resp.status in (400, 403), await resp.text()


async def test_the_quota_is_per_app_and_refuses_past_the_window(installed_app, monkeypatch):
    appended: list[Any] = []
    monkeypatch.setattr(
        crew_log_emit, "on_dashboard_custom", lambda *a, **k: appended.append(a) or True
    )
    async with _app_client() as c:
        for _ in range(agent_panel_record.RECORD_QUOTA_PER_MINUTE):
            resp = await _post(c, {"type": "scan", "data": {"n": 1}})
            assert resp.status == 200, await resp.text()
        resp = await _post(c, {"type": "scan", "data": {"n": 1}})
        assert resp.status == 429, await resp.text()
        assert (await resp.json())["code"] == "quota_exceeded"
    # Another app's window is its own.
    async with _app_client("other-app") as c:
        resp = await _post(c, {"type": "scan", "data": {"n": 1}})
        assert resp.status == 200, await resp.text()
    assert len(appended) == agent_panel_record.RECORD_QUOTA_PER_MINUTE + 1


# ------------------------------------------------- the append, and what it stamps


async def test_a_record_lands_under_the_apps_key_stamped_app_and_folds_for_the_page(
    installed_app,
):
    """The round trip: the app sends type and data, the gateway stores it under
    ``<app>__<type>`` with ``writer: app:<id>``, and the ``custom`` fold answers it."""
    app = _mounted(internal=False)
    app.middlewares.insert(0, _as_app(APP))
    async with TestClient(TestServer(app)) as c:
        resp = await _post(c, {"type": "scan", "data": {"open": 3, "label": "ok"}})
        assert resp.status == 200, await resp.text()
        assert await resp.json() == {
            "ok": True,
            "recorded": {"type": f"{APP_KEY}__scan", "writer": f"app:{APP}", "member": SLUG},
        }
        sent = app["state"].broadcasts
        assert ("dashboard_value_written", {"slug": SLUG}) in sent, f"nothing pushed: {sent}"
    folded = _custom_fold()
    assert "scan" not in folded["latest"], "the bare type must not be a key"
    row = folded["latest"][f"{APP_KEY}__scan"]
    assert row["data"] == {"open": 3, "label": "ok"}
    assert row["writer"] == f"app:{APP}"
    assert row["member"] == SLUG
    assert folded["wrote"] == 1


async def test_the_body_cannot_set_who_wrote_it_or_for_whom(installed_app):
    """``writer``, ``member``, ``crew_key`` and ``src`` in the body are IGNORED: the
    entry carries the token's app claim and the path's crewmate, nothing the app said."""
    async with _app_client() as c:
        resp = await _post(
            c,
            {
                "type": "scan",
                "data": {"n": 1},
                "writer": "cron:stock-poll",
                "member": "someone-else",
                "crew_key": "f" * 64,
                "src": "gateway",
            },
        )
        assert resp.status == 200, await resp.text()
    row = _custom_fold()["latest"][f"{APP_KEY}__scan"]
    assert row["writer"] == f"app:{APP}"
    assert row["member"] == SLUG


async def test_two_apps_writing_the_same_type_name_never_share_a_key(installed_app):
    async with _app_client(APP) as c:
        assert (await _post(c, {"type": "scan", "data": {"n": 1}})).status == 200
    async with _app_client("other-app") as c:
        assert (await _post(c, {"type": "scan", "data": {"n": 2}})).status == 200
    latest = _custom_fold()["latest"]
    assert latest[f"{APP_KEY}__scan"]["data"] == {"n": 1}
    assert latest["other_app__scan"]["data"] == {"n": 2}
    assert latest[f"{APP_KEY}__scan"]["writer"] == f"app:{APP}"
    assert latest["other_app__scan"]["writer"] == "app:other-app"


async def test_a_crewmate_whose_thread_never_ran_has_no_log_to_append_to(
    installed_app, monkeypatch
):
    monkeypatch.setattr(member_dashboard, "_newest_unit", lambda _slot: "")
    async with _app_client() as c:
        resp = await _post(c, {"type": "scan", "data": {"n": 1}})
        assert resp.status == 409, await resp.text()
        assert (await resp.json())["code"] == "crew_log_unavailable"


async def test_an_append_that_does_not_land_is_reported_as_failed(installed_app, monkeypatch):
    monkeypatch.setattr(crew_log_emit, "on_dashboard_custom", lambda *_a, **_k: False)
    app = _mounted(internal=False)
    app.middlewares.insert(0, _as_app(APP))
    async with TestClient(TestServer(app)) as c:
        resp = await _post(c, {"type": "scan", "data": {"n": 1}})
        assert resp.status == 503, await resp.text()
        assert (await resp.json())["code"] == "append_failed"
        assert not app["state"].broadcasts
