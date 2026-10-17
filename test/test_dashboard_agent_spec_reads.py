"""Concurrency coverage for the dashboard's authoritative PATCH re-read."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers.agents as agents_mod
from kiro_crew.dashboard.handlers.agents import api_agent_detail


@pytest.fixture
def owner_request(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _patch_request(name, body):
    request = MagicMock(spec=web.Request)
    request.method = "PATCH"
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}

    async def _json():
        return body

    request.json = _json
    return request


@pytest.mark.asyncio
async def test_refused_under_lock_reread_returns_agent_changed(
    tmp_path, monkeypatch, owner_request
):
    path = tmp_path / "kirocrew.json"
    path.write_text(json.dumps({"name": "kirocrew", "model": "old"}), encoding="utf-8")
    real_reader = agents_mod._read_agent_spec
    calls = 0

    def _reader(spec_file, **attribution):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_reader(spec_file, **attribution)
        return None

    monkeypatch.setattr(agents_mod, "_read_agent_spec", _reader)
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
        response = await api_agent_detail(_patch_request("kirocrew", {"model": "new"}))

    assert response.status == 409
    assert json.loads(response.text)["code"] == "agent_changed"
    assert json.loads(path.read_text(encoding="utf-8"))["model"] == "old"


@pytest.mark.asyncio
async def test_non_object_under_lock_reread_returns_409_not_type_error(
    tmp_path, monkeypatch, owner_request
):
    path = tmp_path / "kirocrew.json"
    path.write_text(json.dumps({"name": "kirocrew", "model": "old"}), encoding="utf-8")
    real_reader = agents_mod._read_agent_spec
    calls = 0

    def _reader(spec_file, **attribution):
        nonlocal calls
        calls += 1
        if calls == 2:
            spec_file.write_text("[]", encoding="utf-8")
        return real_reader(spec_file, **attribution)

    monkeypatch.setattr(agents_mod, "_read_agent_spec", _reader)
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
        response = await api_agent_detail(_patch_request("kirocrew", {"model": "new"}))

    assert response.status == 409
    assert json.loads(response.text)["code"] == "agent_changed"
