"""Tests for api_agent_detail PATCH model_managed marker behavior.

An explicit model pick must freeze the choice (model_managed=False); clearing
the model (auto) must resume tracking the shipped default (model_managed=True).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import agent_state
from kiro_crew.dashboard.handlers.agents import api_agent_detail


def _patch_request(name: str, body: dict):
    request = MagicMock(spec=web.Request)
    request.method = "PATCH"
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}

    async def _json():
        return body

    request.json = _json
    return request


@pytest.mark.asyncio
async def test_patch_explicit_model_freezes(tmp_path):
    cfg = tmp_path / "kirocrew.json"
    cfg.write_text(json.dumps({"name": "kirocrew", "model": "claude-old", "model_managed": True}))
    request = _patch_request("kirocrew", {"model": "claude-new"})

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
        resp = await api_agent_detail(request)

    assert resp.status == 200
    data = json.loads(cfg.read_text())
    assert data["model"] == "claude-new"
    # Spec stays schema-clean; managed-state goes to the sidecar.
    assert "model_managed" not in data
    assert agent_state.get_model_managed("kirocrew") is False


@pytest.mark.asyncio
async def test_patch_clear_model_resumes_tracking(tmp_path):
    cfg = tmp_path / "kirocrew.json"
    cfg.write_text(
        json.dumps({"name": "kirocrew", "model": "claude-pinned", "model_managed": False})
    )
    request = _patch_request("kirocrew", {"model": ""})

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
        resp = await api_agent_detail(request)

    assert resp.status == 200
    data = json.loads(cfg.read_text())
    assert "model" not in data
    assert "model_managed" not in data
    assert agent_state.get_model_managed("kirocrew") is True
