"""A wrong-typed ``name`` or ``source`` in an app route body is a 400, never a 500.

Each of these routes checked only that the field was truthy, so a list, an object
or a number reached code that needs a string (a dict key, ``Path(...)``,
``str.startswith``) and raised inside the handler.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _install, _make_app, _setup_env


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        (f"/api/apps/{APP}/update", {"source": None}),
        (f"/api/apps/{APP}/update", {"source": 5}),
        ("/api/apps/install", {"source": 1}),
        ("/api/apps/install", {"source": ["a"]}),
        ("/api/apps/register", {"name": ["x"], "version": "1", "displayName": "X"}),
        ("/api/apps/register", {"name": 1, "version": "1", "displayName": "X"}),
        ("/api/apps/registry/install", {"name": ["x"]}),
        ("/api/apps/registry/install", {"name": {"a": 1}}),
    ],
)
async def test_a_wrong_typed_field_is_a_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, body: dict
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(path, json=body)
        assert resp.status == 400, (path, body, resp.status, await resp.text())
        assert (await resp.json())["code"] == "invalid_field_type"
