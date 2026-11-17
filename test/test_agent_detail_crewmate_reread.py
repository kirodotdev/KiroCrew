"""The detail PATCH re-reads a crewmate template from the folder it was found in.

The scan finds templates in the user-level agents folder AND the crewmate folder
(``mates/agents``). The locked re-read before the write must fence the file against
that same folder; fencing it against the user-level folder only refused every
crewmate template as "changed on disk".
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import agents as agents_handlers
from kiro_crew.dashboard.handlers.agents import api_agent_detail


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _patch_request(name: str, body: dict):
    request = MagicMock(spec=web.Request)
    request.method = "PATCH"
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}

    async def _json():
        return body

    request.json = _json
    return request


def _layout(tmp_path):
    user_dir = tmp_path / "agents"
    mates_dir = tmp_path / "mates" / "agents"
    user_dir.mkdir()
    mates_dir.mkdir(parents=True)
    spec = mates_dir / "mate-one.json"
    spec.write_text(json.dumps({"name": "mate-one", "model": "claude-old"}))
    return user_dir, mates_dir, spec


@pytest.mark.asyncio
async def test_a_crewmate_template_model_patch_is_written(tmp_path):
    user_dir, mates_dir, spec = _layout(tmp_path)
    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", user_dir),
        patch("kiro_crew.config.paths.mate_agents_dir", lambda: mates_dir),
    ):
        resp = await api_agent_detail(_patch_request("mate-one", {"model": "claude-new"}))

    assert resp.status == 200, resp.text
    assert json.loads(spec.read_text(encoding="utf-8"))["model"] == "claude-new"


@pytest.mark.asyncio
async def test_a_crewmate_template_removed_before_the_locked_reread_still_refuses(
    tmp_path, monkeypatch
):
    user_dir, mates_dir, spec = _layout(tmp_path)
    real = agents_handlers._agent_detail_candidates

    def _scan_then_remove(name):
        found = real(name)
        spec.unlink()
        return found

    monkeypatch.setattr(agents_handlers, "_agent_detail_candidates", _scan_then_remove)
    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", user_dir),
        patch("kiro_crew.config.paths.mate_agents_dir", lambda: mates_dir),
    ):
        resp = await api_agent_detail(_patch_request("mate-one", {"model": "claude-new"}))

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "agent_changed"
    assert not spec.exists()


@pytest.mark.asyncio
async def test_a_crewmate_template_swapped_for_a_symlink_before_the_reread_refuses(
    tmp_path, monkeypatch
):
    user_dir, mates_dir, spec = _layout(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"name": "mate-one", "model": "claude-outside"}))
    real = agents_handlers._agent_detail_candidates

    def _scan_then_swap(name):
        found = real(name)
        spec.unlink()
        spec.symlink_to(outside)
        return found

    monkeypatch.setattr(agents_handlers, "_agent_detail_candidates", _scan_then_swap)
    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", user_dir),
        patch("kiro_crew.config.paths.mate_agents_dir", lambda: mates_dir),
    ):
        resp = await api_agent_detail(_patch_request("mate-one", {"model": "claude-new"}))

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "agent_changed"
    assert json.loads(outside.read_text(encoding="utf-8"))["model"] == "claude-outside"
