"""Tests for the api_agent_detail DELETE reference guard.

Deleting an agent template that config still points at leaves a dangling
reference: the kiro-cli fallback, or a crew's ``kiro_agent`` binding, would name
a template that no longer exists. The dashboard withholds the control, but that
check reads a cached snapshot and cannot be race-free — so the handler is the
authority and refuses with 409.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.handlers.agents import api_agent_detail


def _delete_request(name: str):
    request = MagicMock(spec=web.Request)
    request.method = "DELETE"
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}
    return request


def _agent_file(tmp_path, name: str):
    f = tmp_path / f"{name}.json"
    f.write_text(json.dumps({"name": name}), encoding="utf-8")
    return f


async def _delete(tmp_path, name: str, cfg: KiroCrewConfig):
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path), patch.object(
        KiroCrewConfig, "load", staticmethod(lambda: cfg)
    ):
        return await api_agent_detail(_delete_request(name))


@pytest.mark.asyncio
async def test_delete_refuses_the_fallback_template(tmp_path):
    f = _agent_file(tmp_path, "scratch")
    cfg = KiroCrewConfig()
    cfg.agent.default_agent = "scratch"

    resp = await _delete(tmp_path, "scratch", cfg)

    assert resp.status == 409
    # The code is the contract consumers branch on; the prose may be reworded.
    assert json.loads(resp.body)["code"] == "agent_is_default"
    assert f.exists(), "a refused delete must not unlink the file"


@pytest.mark.asyncio
async def test_delete_refuses_a_crew_bound_template(tmp_path):
    f = _agent_file(tmp_path, "scratch")
    cfg = KiroCrewConfig()
    cfg.agents = {"researcher": KiroCrewAgentConfig(kiro_agent="scratch")}

    resp = await _delete(tmp_path, "scratch", cfg)

    assert resp.status == 409
    # The message names the crews so the caller can act without guessing.
    assert "researcher" in json.loads(resp.body)["error"]
    assert json.loads(resp.body)["code"] == "agent_in_use"
    assert f.exists()


@pytest.mark.asyncio
async def test_delete_matches_the_filename_stem_too(tmp_path):
    """Config may record the stem rather than the JSON's own "name" field."""
    f = tmp_path / "scratch.json"
    f.write_text(json.dumps({"name": "Scratch Pad"}), encoding="utf-8")
    cfg = KiroCrewConfig()
    cfg.agent.default_agent = "scratch"

    resp = await _delete(tmp_path, "scratch", cfg)

    assert resp.status == 409
    assert f.exists()


@pytest.mark.asyncio
async def test_delete_refuses_when_config_records_the_stem_but_request_uses_the_name(tmp_path):
    """The sharp alias case: the two identifiers differ AND the request uses the
    display name, so `name` never carries the stem that config actually binds."""
    f = tmp_path / "scratch.json"
    f.write_text(json.dumps({"name": "Scratch Pad"}), encoding="utf-8")
    cfg = KiroCrewConfig()
    cfg.agents = {"researcher": KiroCrewAgentConfig(kiro_agent="scratch")}

    resp = await _delete(tmp_path, "Scratch Pad", cfg)

    assert resp.status == 409
    assert f.exists(), "a crew bound by the stem still pins this template"


@pytest.mark.asyncio
async def test_delete_refuses_the_top_level_default_the_page_picker_writes(tmp_path):
    """`/api/config/default-agent` — the picker on the templates page — writes
    top-level `default_agent`, not `agent.default_agent`. Guarding only the
    latter would let the UI delete the very template it just made default."""
    f = _agent_file(tmp_path, "scratch")
    cfg = KiroCrewConfig()
    cfg.default_agent = "scratch"

    resp = await _delete(tmp_path, "scratch", cfg)

    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "agent_is_default"
    assert f.exists()


@pytest.mark.asyncio
async def test_delete_allows_an_unreferenced_template(tmp_path):
    f = _agent_file(tmp_path, "scratch")
    cfg = KiroCrewConfig()
    cfg.agent.default_agent = "kirocrew"
    cfg.default_agent = "kirocrew"
    cfg.agents = {"researcher": KiroCrewAgentConfig(kiro_agent="kirocrew")}

    resp = await _delete(tmp_path, "scratch", cfg)

    assert resp.status == 200
    assert not f.exists()
