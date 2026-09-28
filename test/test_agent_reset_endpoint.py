"""Tests for api_agent_reset — POST /api/agents/detail/{name}/reset.

The server-side transaction replacing the panel's client-orchestrated
rebind-then-delete: the origin must exist and the crew must
still be bound to the copy BEFORE anything is mutated, and the copy is deleted
only after the rebind persisted.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import agent_state
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.handlers.agents import api_agent_reset


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _reset_request(name: str, body):
    request = MagicMock(spec=web.Request)
    request.method = "POST"
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}

    async def _json():
        return body

    request.json = _json
    return request


def _write_template(agents_dir, stem: str, **extra) -> None:
    spec = {"name": stem, "model": "claude-x", "tools": ["ReadFile"]}
    spec.update(extra)
    (agents_dir / f"{stem}.json").write_text(json.dumps(spec), encoding="utf-8")


def _seed(agents_dir, *, origin_exists: bool = True) -> None:
    """Crew 'design-crew' bound to private copy 'design-crew' forked from 'kirocrew'."""
    if origin_exists:
        _write_template(agents_dir, "kirocrew")
    _write_template(agents_dir, "design-crew")
    agent_state.set_fork_info("design-crew", forked_from="kirocrew", private_to="design-crew")
    cfg = KiroCrewConfig()
    cfg.agents = {"design-crew": KiroCrewAgentConfig(kiro_agent="design-crew")}
    cfg.default_agent = "design-crew"
    cfg.save()


@pytest.mark.asyncio
async def test_reset_happy_path(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir)

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_reset(_reset_request("design-crew", {"crew": "design-crew"}))

    assert resp.status == 200
    assert json.loads(resp.text)["template"] == "kirocrew"
    # Rebound to the origin, copy removed, lineage pruned.
    assert KiroCrewConfig.load().agents["design-crew"].kiro_agent == "kirocrew"
    assert not (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is None


@pytest.mark.asyncio
async def test_reset_refuses_when_origin_missing_and_keeps_copy(tmp_path):
    """The finding's failure: origin deleted after panel load. The rebind must
    NOT happen and the copy must survive — never a crew bound to nothing."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir, origin_exists=False)

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_reset(_reset_request("design-crew", {"crew": "design-crew"}))

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "origin_missing"
    assert KiroCrewConfig.load().agents["design-crew"].kiro_agent == "design-crew"
    assert (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is not None


@pytest.mark.asyncio
async def test_reset_refuses_non_private_copy(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "shared-template")
    cfg = KiroCrewConfig()
    cfg.agents = {"design-crew": KiroCrewAgentConfig(kiro_agent="shared-template")}
    cfg.default_agent = "design-crew"
    cfg.save()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_reset(_reset_request("shared-template", {"crew": "design-crew"}))

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "not_a_private_copy"
    assert (agents_dir / "shared-template.json").exists()


@pytest.mark.asyncio
async def test_reset_refuses_stale_binding(tmp_path):
    """Crew moved to another template since the panel loaded: nothing mutates."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir)
    cfg = KiroCrewConfig.load()
    cfg.agents["design-crew"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.save()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_reset(_reset_request("design-crew", {"crew": "design-crew"}))

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "stale_binding"
    assert (agents_dir / "design-crew.json").exists()


@pytest.mark.asyncio
async def test_reset_removes_copy_whose_file_stem_differs_from_name(tmp_path):
    """F2: the delete must resolve the copy's ACTUAL file, not
    reconstruct `<name>.json`. With a stem/name divergence the old code missed
    the file yet pruned lineage, leaving the customized file to appear shared."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "kirocrew")
    # File stem 'odd-stem' carries declared name 'design-copy'.
    (agents_dir / "odd-stem.json").write_text(
        json.dumps({"name": "design-copy", "model": "claude-x", "tools": ["ReadFile"]}),
        encoding="utf-8",
    )
    agent_state.set_fork_info("design-copy", forked_from="kirocrew", private_to="design-crew")
    cfg = KiroCrewConfig()
    cfg.agents = {"design-crew": KiroCrewAgentConfig(kiro_agent="design-copy")}
    cfg.default_agent = "design-crew"
    cfg.save()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_reset(_reset_request("design-copy", {"crew": "design-crew"}))

    assert resp.status == 200
    assert KiroCrewConfig.load().agents["design-crew"].kiro_agent == "kirocrew"
    # The ACTUAL file is gone — and only then is lineage pruned.
    assert not (agents_dir / "odd-stem.json").exists()
    assert agent_state.get_fork_info("design-copy") is None


@pytest.mark.asyncio
async def test_reset_keeps_copy_bound_by_another_crew(tmp_path):
    """a pre-existing foreign binding to the private copy must
    block the cleanup unlink; the reset (rebind to origin) still commits."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir)
    cfg = KiroCrewConfig.load()
    cfg.agents["other-crew"] = KiroCrewAgentConfig(kiro_agent="design-crew")
    cfg.save()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_reset(_reset_request("design-crew", {"crew": "design-crew"}))

    assert resp.status == 200
    assert KiroCrewConfig.load().agents["design-crew"].kiro_agent == "kirocrew"
    # The copy survives with lineage intact — other-crew still resolves it.
    assert (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is not None


@pytest.mark.asyncio
async def test_reset_keeps_copy_bound_by_file_stem(tmp_path):
    """a stem binding (stem != declared name) resolves the same
    file, so it too must block the reset cleanup's unlink."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "kirocrew")
    _write_template(agents_dir, "design-crew-file", name="design-crew")
    agent_state.set_fork_info("design-crew", forked_from="kirocrew", private_to="design-crew")
    cfg = KiroCrewConfig()
    cfg.agents = {
        "design-crew": KiroCrewAgentConfig(kiro_agent="design-crew"),
        "other-crew": KiroCrewAgentConfig(kiro_agent="design-crew-file"),
    }
    cfg.default_agent = "design-crew"
    cfg.save()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_reset(_reset_request("design-crew", {"crew": "design-crew"}))

    assert resp.status == 200
    assert KiroCrewConfig.load().agents["design-crew"].kiro_agent == "kirocrew"
    assert (agents_dir / "design-crew-file.json").exists()
    assert agent_state.get_fork_info("design-crew") is not None
