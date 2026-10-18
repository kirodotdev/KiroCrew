"""The request builders and patch helpers the Issue Radar route-layer coverage tests share.

``test_issue_radar_routes_coverage.py`` and its ``_pull_actions``,
``_label_and_issue_writes`` and ``_model_inputs`` siblings import what they use from here by
name: ``BASE``, the app's API path prefix; ``SHA`` and ``OTHER_SHA``, two distinct
40-character commit hashes; ``_key``, a provider repository key; ``_get``, a mocked GET
request; ``_json_request``, a mocked request on an app carrying an unconfigured dashboard
owner, whose ``.json()`` returns the given body or raises for ``None`` to model a malformed
payload; ``_body``, a response's decoded JSON; and ``_connected`` and ``_writable``, which
patch the connected-repository gate and the write-permission check. Nothing here runs a
provider subprocess or touches the network.
"""

import json
from unittest import mock
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dashboard_owner_helpers import NoConfiguredOwner

from kiro_crew.apps.builtins.issue_radar.backend import provider, routes, store

BASE = "/api/apps/issue-radar"
SHA = "a" * 40
OTHER_SHA = "b" * 40


def _key(owner: str = "o", repo: str = "r") -> provider.RepoKey:
    return provider.key_from_parts(owner, repo)


def _get(path: str, query: dict | None = None) -> web.Request:
    """A real (mocked) aiohttp GET request for a handler under test.

    aiohttp's own ``make_mocked_request`` rather than a duck-typed stub: the
    handlers are annotated ``(web.Request) -> web.Response`` and a stand-in would
    fail the repo's mypy gate.
    """
    full = f"{BASE}/{path}"
    if query:
        full = f"{full}?{urlencode(query)}"
    return make_mocked_request("GET", full)


def _json_request(method: str, path: str, body: object) -> web.Request:
    """A request whose ``.json()`` resolves to ``body`` (or raises, for ``None``).

    Passing ``None`` models a malformed payload: ``request.json()`` raising is
    exactly what the handlers' ``except Exception -> 400`` branch is written for.
    """
    req = make_mocked_request(method, f"{BASE}/{path}")
    if "state" not in req.app:
        req.app["state"] = NoConfiguredOwner()
    req["user"] = "local-app"
    req["app"] = ""
    if body is None:
        req.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    else:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.Response) -> dict:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _connected(value: bool = True):
    return mock.patch.object(store, "is_repo_connected", return_value=value)


def _writable(value: bool | None = True):
    return mock.patch.object(routes, "_repo_can_write", return_value=value)
