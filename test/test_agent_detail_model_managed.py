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
    data = json.loads(cfg.read_text(encoding="utf-8"))
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
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "model" not in data
    assert "model_managed" not in data
    assert agent_state.get_model_managed("kirocrew") is True


@pytest.mark.asyncio
async def test_patch_without_model_lifts_stale_bookkeeping_keys(tmp_path):
    """A PATCH that never touches ``model`` still runs the shared strip/lift rule.

    Regression for a design-review follow-up on #2570: this PATCH handler used
    to unconditionally ``data.pop(...)`` these two keys, discarding a legacy
    value already on disk instead of lifting it into the sidecar the way the
    other three writers (PUT, migrate_agent_specs, _refresh_dynamic_fields) do.
    Routing through ``agent_state.lift_and_strip_bookkeeping`` closes that gap.
    """
    cfg = tmp_path / "kirocrew.json"
    cfg.write_text(
        json.dumps({"name": "kirocrew", "model_managed": False, "cc_model": "claude-sonnet-4.6"})
    )
    request = _patch_request("kirocrew", {})

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
        resp = await api_agent_detail(request)

    assert resp.status == 200
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "model_managed" not in data
    assert "cc_model" not in data
    assert agent_state.get_model_managed("kirocrew") is False
    assert agent_state.get_cc_model("kirocrew") == "claude-sonnet-4.6"
