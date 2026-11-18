"""The detail PATCH refuses crewmate files by name and still writes user-level ones.

The scan finds templates in the user-level agents folder AND the crewmate folder
(``mates/agents``). A crewmate file has one writer, the crewmate editor, so the
generic PATCH (template pane and ``template.update`` cards) refuses it with its own
code instead of the misleading "changed on disk". User-level files still write,
and the locked re-read still refuses a file removed or swapped for a symlink.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import change_card_catalog as catalog
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


def _layout(tmp_path, *, crewmate: bool = False):
    user_dir = tmp_path / "agents"
    mates_dir = tmp_path / "mates" / "agents"
    user_dir.mkdir()
    mates_dir.mkdir(parents=True)
    spec = (mates_dir if crewmate else user_dir) / "mate-one.json"
    spec.write_text(
        json.dumps(
            {
                "name": "mate-one",
                "model": "claude-old",
                "prompt": "old prompt",
                "tools": ["read", "write"],
                "allowedTools": ["read", "write"],
            }
        )
    )
    return user_dir, mates_dir, spec


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"model": "claude-new"},
        {"prompt": "new prompt"},
        {"tools": ["read"]},
        {"allowedTools": ["read"]},
    ],
)
async def test_a_crewmate_file_is_refused_and_left_untouched(tmp_path, body):
    user_dir, mates_dir, spec = _layout(tmp_path, crewmate=True)
    before = spec.read_bytes()
    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", user_dir),
        patch("kiro_crew.config.paths.mate_agents_dir", lambda: mates_dir),
    ):
        resp = await api_agent_detail(_patch_request("mate-one", body))

    assert resp.status == 409
    payload = json.loads(resp.text)
    assert payload["code"] == "crewmate_editor_required"
    assert "Bots page" in payload["error"]
    assert "crewmate" not in payload["error"].lower()
    assert spec.read_bytes() == before


@pytest.mark.asyncio
async def test_a_template_update_card_on_a_crewmate_lands_on_the_refusal(tmp_path):
    """The card's apply step is the same PATCH, so it gets the same true answer."""
    user_dir, mates_dir, spec = _layout(tmp_path, crewmate=True)
    before = spec.read_bytes()
    params = catalog.validate_params(
        "template.update", {"template": "mate-one", "fields": {"allowedTools": ["read"]}}
    )
    preview = catalog.build_preview(
        "template.update",
        params,
        {"exists": True, "fields": {"allowedTools": ["read", "write"]}},
        {"members": []},
    )
    (apply_step,) = preview["apply"]
    assert apply_step["method"] == "PATCH"
    assert apply_step["path"] == "/api/agents/detail/mate-one"
    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", user_dir),
        patch("kiro_crew.config.paths.mate_agents_dir", lambda: mates_dir),
    ):
        resp = await api_agent_detail(_patch_request("mate-one", apply_step["body"]))

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "crewmate_editor_required"
    assert spec.read_bytes() == before


@pytest.mark.asyncio
async def test_a_user_level_template_model_patch_is_written(tmp_path):
    user_dir, mates_dir, spec = _layout(tmp_path)
    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", user_dir),
        patch("kiro_crew.config.paths.mate_agents_dir", lambda: mates_dir),
    ):
        resp = await api_agent_detail(_patch_request("mate-one", {"model": "claude-new"}))

    assert resp.status == 200, resp.text
    assert json.loads(spec.read_text(encoding="utf-8"))["model"] == "claude-new"


@pytest.mark.asyncio
async def test_a_template_removed_before_the_locked_reread_still_refuses(tmp_path, monkeypatch):
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
async def test_a_template_swapped_for_a_symlink_before_the_reread_refuses(tmp_path, monkeypatch):
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
