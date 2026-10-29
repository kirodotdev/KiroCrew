"""An apps route answers a JSON body that is not an object with a 400.

Each of these handlers parses with ``await request.json()`` and then reads its
fields with ``body.get()``. A list, string or number is valid JSON, so the parse
succeeds and the ``.get()`` raised ``AttributeError``: a 500 for a client mistake.
Each now returns ``body_not_object_error``'s 400 right after the parse.

Every route also gets an empty object, so the guard is shown to let an object
through to the route's own field checks, as before.

``POST /api/apps/{name}/uninstall`` is not here: its parse sits inside a broad
``except`` that keeps the safe defaults, so a list body already uninstalls with
the data kept rather than answering a 500.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _install, _make_app, _setup_env

#: (method, path) of every apps route that parses a body and reads it with ``.get()``.
_ROUTES = [
    ("post", f"/api/apps/{APP}/update"),
    ("post", "/api/apps/register"),
    ("post", "/api/apps/install"),
    ("post", "/api/apps/registry/install"),
    ("post", "/api/apps/registry/install-stream"),
    ("put", "/api/apps/registries"),
    ("post", f"/api/apps/{APP}/dev"),
]

_NON_OBJECTS = [[1, 2], [], "text", 7, None]


async def _send(tmp_path, monkeypatch, method: str, path: str, body: Any) -> tuple[int, Any]:
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    client = TestClient(TestServer(_make_app(dashboard_user="owner")))
    await client.start_server()
    try:
        # The JSON text itself, not ``json=``: aiohttp sends no body at all for
        # ``json=None``, and ``null`` is one of the bodies under test.
        resp = await getattr(client, method)(
            path, data=json.dumps(body), headers={"Content-Type": "application/json"}
        )
        try:
            payload = await resp.json()
        except Exception:  # noqa: BLE001 -- a non-JSON answer is checked by status
            payload = await resp.text()
        return resp.status, payload
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _NON_OBJECTS, ids=repr)
@pytest.mark.parametrize("method,path", _ROUTES)
async def test_a_non_object_body_is_a_400(tmp_path, monkeypatch, method, path, body) -> None:
    status, payload = await _send(tmp_path, monkeypatch, method, path, body)
    assert status == 400, payload
    assert payload == {"error": "body must be a JSON object", "code": "body_not_object"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", _ROUTES)
async def test_an_object_body_passes_the_guard(tmp_path, monkeypatch, method, path) -> None:
    status, payload = await _send(tmp_path, monkeypatch, method, path, {})
    assert status != 500, payload
    assert not (isinstance(payload, dict) and payload.get("code") == "body_not_object")
