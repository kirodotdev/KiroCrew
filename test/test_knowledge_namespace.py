"""Tests for namespace folding in add_source handler."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers.knowledge import add_source


def _make_request(body: dict, app_extras: dict | None = None) -> MagicMock:
    """Build a minimal aiohttp-like request mock.

    Provides a mock sync scheduler whose connector validates any config, so
    local_folder sources take the production path (connector present) instead
    of the no-connector branch that requires https:// URIs.
    """
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.query = {}
    connector = MagicMock()
    connector.validate_config.return_value = (True, None)
    sync_scheduler = MagicMock()
    sync_scheduler.get_connector.return_value = connector
    app = {"knowledge_sync": sync_scheduler, "knowledge_watcher": None}
    if app_extras:
        app.update(app_extras)
    req.app = app
    return req


@pytest.mark.asyncio
async def test_add_source_folds_namespace_into_properties():
    """Top-level namespace is folded into properties for local_folder."""
    body = {
        "name": "test",
        "source_type": "local_folder",
        "uri": "/tmp/test_folder",
        "properties": {"recursive": True},
        "namespace": "my-ns",
    }
    req = _make_request(body)

    with (
        patch(
            "kiro_crew.dashboard.handlers.knowledge.is_sensitive_path",
            return_value=False,
        ),
        patch.object(Path, "resolve", return_value=Path("/tmp/test_folder")),
        patch.object(Path, "is_dir", return_value=True),
    ):
        store_mock = MagicMock()
        store_mock.get_source_by_uri.return_value = None
        store_mock.add_source.return_value = "sid-123"
        with patch(
            "kiro_crew.dashboard.handlers.knowledge._store",
            return_value=store_mock,
        ):
            resp = await add_source(req)

    assert resp.status == 201
    # Verify namespace was injected into properties
    call_kwargs = store_mock.add_source.call_args
    props = call_kwargs.kwargs.get("properties") or call_kwargs[1].get("properties")
    assert props["namespace"] == "my-ns"
    assert props["sync_status"] == "pending_confirmation"


@pytest.mark.asyncio
async def test_add_source_namespace_from_properties_directly():
    """Namespace already in properties is preserved (no double-fold)."""
    body = {
        "name": "test",
        "source_type": "local_folder",
        "uri": "/tmp/test_folder",
        "properties": {"recursive": True, "namespace": "existing-ns"},
    }
    req = _make_request(body)

    with (
        patch(
            "kiro_crew.dashboard.handlers.knowledge.is_sensitive_path",
            return_value=False,
        ),
        patch.object(Path, "resolve", return_value=Path("/tmp/test_folder")),
        patch.object(Path, "is_dir", return_value=True),
    ):
        store_mock = MagicMock()
        store_mock.get_source_by_uri.return_value = None
        store_mock.add_source.return_value = "sid-456"
        with patch(
            "kiro_crew.dashboard.handlers.knowledge._store",
            return_value=store_mock,
        ):
            resp = await add_source(req)

    assert resp.status == 201
    call_kwargs = store_mock.add_source.call_args
    props = call_kwargs.kwargs.get("properties") or call_kwargs[1].get("properties")
    assert props["namespace"] == "existing-ns"


@pytest.mark.asyncio
async def test_add_source_rejects_non_string_namespace():
    """Non-string namespace returns 400."""
    body = {
        "name": "test",
        "source_type": "local_folder",
        "uri": "/tmp/test_folder",
        "properties": {},
        "namespace": 12345,
    }
    req = _make_request(body)

    with patch(
        "kiro_crew.dashboard.handlers.knowledge._store",
        return_value=MagicMock(),
    ):
        resp = await add_source(req)

    assert resp.status == 400
    data = json.loads(resp.body)
    assert "namespace must be a string" in data["error"]


@pytest.mark.asyncio
async def test_add_source_rejects_non_dict_properties():
    """Non-dict properties are rejected with 400 (was a 500 TypeError before)."""
    body = {
        "name": "test",
        "source_type": "local_folder",
        "uri": "/tmp/test_folder",
        "properties": "not-a-dict",
        "namespace": "my-ns",
    }
    req = _make_request(body)

    with patch(
        "kiro_crew.dashboard.handlers.knowledge._store",
        return_value=MagicMock(),
    ):
        resp = await add_source(req)

    assert resp.status == 400
    data = json.loads(resp.body)
    assert "properties must be an object" in data["error"]
