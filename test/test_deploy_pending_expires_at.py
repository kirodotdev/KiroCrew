"""A pending deploy states when it expires: on the list route and in both tool replies."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.deploy import handlers, pending
from kiro_crew.mcp_tools import artifacts
from kiro_crew.testing.clock import ManualClock

# 11:59:45 UTC, so the 1-hour deadline reads 12:59 UTC.
_CREATED = datetime(2026, 11, 6, 11, 59, 45, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path: Path, _floor_monkeypatch):
    store = tmp_path / "deploy" / "pending-deploys.json"
    _floor_monkeypatch.setattr(pending, "_store_path", lambda: store)
    return store


class _State:
    _restricted_keys: set = set()
    _slots: dict = {}


class _Req:
    headers = {"X-Session-Key": "dashboard:ui"}
    app = {"state": _State()}
    match_info: dict = {}


def _listed() -> list[dict]:
    resp = asyncio.run(handlers._handle_pending_list(_Req()))
    assert resp.status == 200
    return json.loads(resp.body)["pending"]


def test_list_route_returns_expires_at_epoch(monkeypatch):
    ManualClock(start=_CREATED + 60).install(monkeypatch, pending)
    pending.add_pending({"site_id": "s", "created_at_epoch": _CREATED})
    [entry] = _listed()
    assert entry.get("expires_at_epoch") == _CREATED + 3600


def test_deadline_is_derived_not_stored(_isolate_store, monkeypatch):
    """An entry saved before the field existed still gets it, and the file stays as it was."""
    ManualClock(start=_CREATED + 60).install(monkeypatch, pending)
    _isolate_store.parent.mkdir(parents=True)
    _isolate_store.write_text(json.dumps([{"id": "old", "created_at_epoch": _CREATED}]))
    [entry] = _listed()
    assert entry.get("expires_at_epoch") == _CREATED + 3600
    assert "expires_at_epoch" not in json.loads(_isolate_store.read_text())[0]


def _tool(monkeypatch, post_reply: dict) -> tuple[str, dict]:
    ManualClock(start=_CREATED).install(monkeypatch, artifacts)
    with (
        patch("kiro_crew.mcp_core._post", return_value=post_reply),
        patch("kiro_crew.deploy.pending.add_pending") as add,
    ):
        out = artifacts.deploy_artifact(
            "deploy_artifact", {"site_id": "s", "artifact_slug": "a", "ttl_hours": 72}
        )
    assert add.call_count == 1
    return out, add.call_args[0][0]


def test_preview_reply_states_confirm_by_time(monkeypatch):
    out, stored = _tool(
        monkeypatch, {"requires_confirm": True, "public": True, "bytes": 1, "scan": "clean"}
    )
    assert "Confirm within 1 hour, by 12:59 UTC" in out
    assert "Site lifetime after deploy (TTL): 72 hours" in out
    assert stored.get("created_at_epoch") == _CREATED


def test_scan_blocked_reply_states_confirm_by_time(monkeypatch):
    out, stored = _tool(
        monkeypatch, {"blocked": True, "count": 1, "findings": "internal-host reference"}
    )
    assert "Confirm within 1 hour, by 12:59 UTC" in out
    assert stored.get("created_at_epoch") == _CREATED


def test_tool_description_names_the_window():
    [tool] = [t for t in artifacts.schemas() if t["name"] == "deploy_artifact"]
    assert "expires 1 hour after it is made" in tool["description"]
