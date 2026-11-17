"""``POST /api/apps/{name}/update`` over a marketplace app ends in the refusal.

The dashboard's Update button and ``kirocrew app update <name>`` send no body
source, so the route falls back to the record's own source, ``market:<ref>@<n>``.
That is not a folder: before the refusal it would stop the app, deregister it,
then fail in ``update_app`` with "source is not a directory". The route refuses
a ``market:`` source up front, before it stops or deregisters anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import (
    APP,
    _deregister_into,
    _make_app,
    _make_app_source,
    _setup_env,
)

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.manager import _read_installed, install_app

SOURCE = f"market:acme/{APP}@3"
SHA = "c" * 64
HINT = (
    "This app came from the marketplace, and updating it from a folder would "
    "overwrite it. Uninstall it first, or install the folder under another name."
)


@pytest.fixture
def market_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = _setup_env(tmp_path, monkeypatch)
    assert install_app(
        _make_app_source(tmp_path),
        expected_name=APP,
        market_source=SOURCE,
        source_commit=SHA,
    ).ok
    return home


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the lifecycle steps the route takes, without running any of them."""
    seen: list[str] = []
    monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: seen.append("stop"))
    monkeypatch.setattr(routes_mod, "deregister_app", _deregister_into(seen))
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: seen.append("start"))
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [None, {"source": SOURCE}, {"source": "/some/app/folder"}, {"source": f"registry:{APP}"}],
    ids=["no-body", "market-body", "folder-override", "registry-source"],
)
async def test_update_of_a_market_app_is_refused_before_anything_stops(
    market_home: Path, calls: list[str], monkeypatch: pytest.MonkeyPatch, body: Any
) -> None:
    log = MagicMock()
    monkeypatch.setattr(routes_mod, "sel", lambda: log)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json=body)
        payload = await resp.json()

    assert resp.status == 400
    assert payload["code"] == "app_update_market_source"
    assert HINT in payload["error"]
    assert "source is not a directory" not in payload["error"]
    assert calls == [], calls
    meta = _read_installed(APP)
    assert meta is not None and meta.source == SOURCE and meta.sourceCommit == SHA
    recorded = [c.kwargs for c in log.log_api_access.call_args_list]
    assert [(c["caller"], c["operation"], c["outcome"]) for c in recorded] == [
        ("dashboard", "provenance", "rejected")
    ]
    assert recorded[0]["resources"] == APP and recorded[0]["error"]
