"""Issue Radar's forge-write routes answer only to the dashboard owner.

Every route below writes to GitHub/GitLab as the OWNER's gh/glab login: the PR
writes through ``_pr_action_preamble``, the issue writes in their own handlers. A non-owner dashboard subject must be refused with
the shared ``owner_only`` 403 before the body is read, and the provider client
must never be built. An app token, this app's own included, is refused the
same way. The owner still passes the gate.

The handlers are the ones ``register_routes`` puts on the router, so the
``_require_enabled`` wrapper and the crew table's agent gate are included.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import provider, routes

OWNER = "owner-user"
BASE = "/api/apps/issue-radar"
FORGE_WRITES = (
    "/pull/state",
    "/pull/review",
    "/pull/comment",
    "/pull/auto-merge",
    "/pull/merge",
    "/pull/run",
    "/pulls/bulk",
    "/issue/comment",
)
#: Issue writes that gate in the handler; the owner then reaches body validation.
ISSUE_WRITES = (
    "/labels/apply",
    "/labels/apply-bulk",
    "/issue/state",
    "/issue/assignees",
    "/labels/create",
)


def _handler(app: web.Application, path: str):
    for resource in app.router.resources():
        if resource.get_info().get("path") == BASE + path:
            for route in resource:
                if route.method == "POST":
                    return route.handler
    raise AssertionError(f"{path} is not registered")


def _drive(path: str, *, user: str, app_name: str) -> tuple[web.Response, mock.MagicMock]:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id=OWNER)
    routes.register_routes(app)
    req = make_mocked_request("POST", BASE + path, app=app)
    req["user"] = user
    req["app"] = app_name
    req.json = mock.AsyncMock(return_value={"owner": "o", "repo": "r", "number": 7})  # type: ignore[method-assign]
    client_for = mock.MagicMock()
    with (
        mock.patch.object(routes, "is_app_enabled", return_value=True),
        mock.patch.object(routes, "_connected", return_value=False),
        mock.patch.object(routes, "_audit"),
        mock.patch.object(provider, "client_for", client_for),
    ):
        resp = asyncio.run(_handler(app, path)(req))
    return resp, client_for


def _code(resp: web.Response) -> str:
    assert isinstance(resp.body, bytes)
    return str(json.loads(resp.body.decode("utf-8")).get("code", ""))


@pytest.mark.parametrize("path", FORGE_WRITES + ISSUE_WRITES)
def test_a_non_owner_dashboard_subject_is_refused(path: str) -> None:
    resp, client_for = _drive(path, user="allowlisted-channel-user", app_name="")
    assert (resp.status, _code(resp)) == (403, "owner_only")
    client_for.assert_not_called()


@pytest.mark.parametrize("path", FORGE_WRITES)
def test_the_owner_passes_the_gate(path: str) -> None:
    # ``_connected`` answers False, so passing the gate is read as the next check's 404.
    resp, _ = _drive(path, user=OWNER, app_name="")
    assert (resp.status, _code(resp)) == (404, "repo_not_connected")


@pytest.mark.parametrize("path", ISSUE_WRITES)
def test_the_owner_passes_the_issue_write_gate(path: str) -> None:
    # The body carries no change, so passing the gate is read as a validation 400.
    resp, _ = _drive(path, user=OWNER, app_name="")
    assert resp.status == 400
    assert _code(resp) != "owner_only"


@pytest.mark.parametrize("path", FORGE_WRITES + ISSUE_WRITES)
def test_the_apps_own_token_is_refused(path: str) -> None:
    resp, client_for = _drive(path, user="issue-radar", app_name="issue-radar")
    assert (resp.status, _code(resp)) == (403, "owner_only")
    client_for.assert_not_called()
