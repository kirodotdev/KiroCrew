"""Owner gate on Issue Radar's crew and local-state write routes.

Every route below writes state the owner's own tooling then acts on: a crew record
the watchdog launches with an auto-approve grant, the connected-repo set the read
routes serve from the owner's provider CLI, and the per-repo settings and records
every crew reads. So each one answers a non-owner dashboard subject, this app's
own token and a foreign app token with the shared 403 ``owner_only``, and never
reaches its store write.

Two callers stay open on purpose, and each has a test here: the internal-secret
legs of ``PUT /crew/work`` and ``PUT /investigation``, which are how the
``issue_radar_crew_record`` and ``issue_radar_record_investigation`` MCP tools
write. The owner's own dashboard request still reaches every store write.

No network, no subprocess: store writers are mocks, SEL is a mock, and the
connected-repo gate is patched true.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import (
    crew_routes,
    crew_store,
    provider,
    routes,
    store,
)

OWNER = "owner-1"
BASE = "/api/apps/issue-radar"

CALLERS = [
    pytest.param("mallory", "", id="non-owner-dashboard-subject"),
    pytest.param(OWNER, "issue-radar", id="own-app-token"),
    pytest.param(OWNER, "other-app", id="foreign-app-token"),
]

LIVE_CREW = {"id": "c_0000abcd", "slot_key": "crew-c_0000abcd", "enabled": True}


def _req(
    method: str,
    path: str,
    payload: object = None,
    *,
    user: str | None,
    app: str | None,
    internal_auth: bool = False,
) -> web.Request:
    request = make_mocked_request(method, path)
    request.app["state"] = SimpleNamespace(owner_id=OWNER)
    if user is not None:
        request["user"] = user
    if app is not None:
        request["app"] = app
    if internal_auth:
        request["internal_auth"] = True

    async def _json(*_a: object, **_k: object) -> object:
        return payload

    request.json = _json  # type: ignore[method-assign]
    return request


def _text(resp: web.Response) -> str:
    return (resp.text or "")[:200]


def _refused(resp: web.Response) -> bool:
    return resp.status == 403 and json.loads(resp.text or "{}").get("code") == "owner_only"


@pytest.fixture(autouse=True)
def _quiet(tmp_path):
    with (
        mock.patch("kiro_crew.sel.sel"),
        mock.patch.object(routes, "_audit"),
        mock.patch.object(routes, "_scope", return_value=tmp_path),
        mock.patch.object(routes, "_connected", return_value=True),
        mock.patch.object(crew_routes, "_revoke_execution", new=mock.AsyncMock()),
        mock.patch.object(
            crew_routes, "_require_crew", new=mock.AsyncMock(return_value=(LIVE_CREW, None))
        ),
        mock.patch.object(crew_routes, "_crew_live_unit", return_value="unit-1"),
    ):
        yield


def _verified_client() -> mock.Mock:
    client = mock.Mock()
    client.verify_repo_access.return_value = {"full_name": "o/r", "private": True}
    return client


#: (id, handler, method, path, body, store target, writer name, return value).
#: Each row is one gated write; the writer is what must not run for a refused caller.
_ROUTES: list[tuple[str, Callable[..., Any], str, str, object, Any, str, object]] = [
    (
        "crew_create",
        crew_routes._handle_crew_create,
        "POST",
        "/crews",
        {"owner": "o", "repo": "r", "name": "Orion"},
        crew_store,
        "create_crew",
        {"id": "c_1"},
    ),
    (
        "crew_update",
        crew_routes._handle_crew_update,
        "PUT",
        "/crew",
        {"owner": "o", "repo": "r", "id": "c_0000abcd", "unattended": True},
        crew_store,
        "update_crew",
        {"id": "c_0000abcd"},
    ),
    (
        "crew_retire",
        crew_routes._handle_crew_retire,
        "DELETE",
        "/crew",
        {"owner": "o", "repo": "r", "id": "c_0000abcd"},
        crew_store,
        "retire_crew",
        {"id": "c_0000abcd"},
    ),
    (
        "crew_pause",
        crew_routes._handle_crew_pause,
        "POST",
        "/crew/pause",
        {"owner": "o", "repo": "r", "id": "c_0000abcd", "paused": True, "reason": "x"},
        crew_store,
        "set_crew_paused",
        {"id": "c_0000abcd"},
    ),
    (
        "crew_resume",
        crew_routes._handle_crew_pause,
        "POST",
        "/crew/pause",
        {"owner": "o", "repo": "r", "id": "c_0000abcd", "paused": False},
        crew_store,
        "set_crew_paused",
        {"id": "c_0000abcd"},
    ),
    (
        "crews_settings_put",
        crew_routes._handle_crews_settings_put,
        "PUT",
        "/crews/settings",
        {"owner": "o", "repo": "r", "settings": {"claim_ttl_hours": 1}},
        crew_store,
        "write_settings",
        {},
    ),
    (
        "crew_work_dashboard",
        crew_routes._handle_crew_work,
        "PUT",
        "/crew/work",
        {
            "owner": "o",
            "repo": "r",
            "crew_id": "c_0000abcd",
            "number": 42,
            "phase": "skipped",
            "event": "pass",
            "event_kind": "skip",
        },
        crew_store,
        "commit_work_progress",
        {"item": {}, "event": {}, "skip": {}},
    ),
    (
        "connect",
        routes._handle_connect,
        "POST",
        "/connect",
        {"url": "https://github.com/o/r"},
        store,
        "add_connected_repo",
        None,
    ),
    (
        "disconnect",
        routes._handle_disconnect,
        "DELETE",
        "/repos?owner=o&repo=r",
        None,
        store,
        "remove_connected_repo",
        True,
    ),
    (
        "settings_put",
        routes._handle_put_settings,
        "PUT",
        "/settings",
        {"owner": "o", "repo": "r", "settings": {"revision": 0}},
        store,
        "write_repo_settings",
        {"revision": 1},
    ),
    (
        "settings_role",
        routes._handle_add_settings_label,
        "POST",
        "/settings/role",
        {"owner": "o", "repo": "r", "role": "triage", "label": "x"},
        store,
        "add_setting_label",
        {},
    ),
    (
        "investigation_put",
        routes._handle_put_investigation,
        "PUT",
        "/investigation",
        {"owner": "o", "repo": "r", "number": 5, "status": "resolved"},
        store,
        "write_investigation",
        {},
    ),
]

_ROUTE_PARAMS = [pytest.param(*row[1:], id=row[0]) for row in _ROUTES]


async def _drive(handler, method, path, body, target, writer, ret, **who):
    with (
        mock.patch.object(provider, "client_for", return_value=_verified_client()),
        mock.patch.object(target, writer, return_value=ret) as write,
    ):
        resp = await handler(_req(method, f"{BASE}{path}", body, **who))
    return resp, write


@pytest.mark.parametrize("user,app", CALLERS)
@pytest.mark.parametrize("handler,method,path,body,target,writer,ret", _ROUTE_PARAMS)
@pytest.mark.asyncio
async def test_write_route_refuses_non_owner(
    handler, method, path, body, target, writer, ret, user, app
):
    resp, write = await _drive(handler, method, path, body, target, writer, ret, user=user, app=app)
    assert _refused(resp), f"status={resp.status} body={_text(resp)}"
    write.assert_not_called()


@pytest.mark.parametrize("handler,method,path,body,target,writer,ret", _ROUTE_PARAMS)
@pytest.mark.asyncio
async def test_owner_still_writes(handler, method, path, body, target, writer, ret):
    resp, write = await _drive(handler, method, path, body, target, writer, ret, user=OWNER, app="")
    assert resp.status == 200, f"status={resp.status} body={_text(resp)}"
    write.assert_called_once()


@pytest.mark.asyncio
async def test_crew_work_agent_leg_still_records():
    """The ``issue_radar_crew_record`` MCP tool's internal-secret write still lands."""
    key = provider.RepoKey(owner="o", repo="r")
    with (
        mock.patch.object(
            crew_routes,
            "_session_identity",
            new=mock.AsyncMock(return_value=(key, LIVE_CREW, None)),
        ),
        mock.patch.object(
            crew_store, "commit_work_progress", return_value={"item": {}, "event": {}, "skip": {}}
        ) as commit,
    ):
        resp = await crew_routes._handle_crew_work(
            _req(
                "PUT",
                f"{BASE}/crew/work",
                {
                    "number": 42,
                    "phase": "investigating",
                    "event": "looking",
                    "event_kind": "investigate",
                },
                user=None,
                app=None,
                internal_auth=True,
            )
        )
    assert resp.status == 200, f"status={resp.status} body={_text(resp)}"
    commit.assert_called_once()


@pytest.mark.asyncio
async def test_investigation_agent_put_still_records():
    """The ``issue_radar_record_investigation`` MCP tool's internal-secret PUT still lands."""
    with mock.patch.object(store, "write_investigation", return_value={}) as write:
        resp = await routes._handle_put_investigation(
            _req(
                "PUT",
                f"{BASE}/investigation",
                {"owner": "o", "repo": "r", "number": 5, "status": "resolved"},
                user=None,
                app=None,
                internal_auth=True,
            )
        )
    assert resp.status == 200, f"status={resp.status} body={_text(resp)}"
    write.assert_called_once()
