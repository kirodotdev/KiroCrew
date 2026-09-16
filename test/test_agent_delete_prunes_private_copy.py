"""Crew delete removes the crew's corroborated private template copy.

A private copy exists for exactly one crew (``private_to`` in the agent_state
sidecar) and that crew's binding points at it. When the crew is deleted the
copy has no reader left, so the file and its lineage record go with it —
otherwise a template named after a dead crew keeps surfacing in the Agent
Templates tab. Removal is corroborated on BOTH sides and best-effort: a copy
whose lineage names another crew, or one the deleted crew was not bound to,
stays untouched, and a file that cannot be removed never blocks the delete.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import agent_state
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, config_local_path
from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_delete


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _delete_request(name: str):
    request = MagicMock(spec=web.Request)
    request.method = "DELETE"
    request.match_info = {"name": name}
    # No dashboard state: the handler's session/refresh hooks are all
    # None-guarded, which keeps the test on the delete path itself.
    request.app = {"state": None}
    return request


def _write_template(agents_dir, stem: str, **extra) -> None:
    spec = {"name": stem, "model": "claude-x", "tools": ["ReadFile"]}
    spec.update(extra)
    (agents_dir / f"{stem}.json").write_text(json.dumps(spec), encoding="utf-8")


def _seed(agents_dir, *, bound_to: str = "design-crew", private_to: str = "design-crew") -> None:
    """Default crew 'kirocrew' on the shared template; crew 'design-crew' with a
    private copy 'design-crew' forked from 'kirocrew'.

    ``bound_to`` / ``private_to`` let a test break one side of the
    corroboration.
    """
    _write_template(agents_dir, "kirocrew")
    _write_template(agents_dir, "design-crew")
    agent_state.set_fork_info("design-crew", forked_from="kirocrew", private_to=private_to)
    cfg = KiroCrewConfig()
    cfg.agents = {
        "kirocrew": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "design-crew": KiroCrewAgentConfig(kiro_agent=bound_to),
    }
    cfg.default_agent = "kirocrew"
    cfg.save()


@pytest.mark.asyncio
async def test_delete_removes_corroborated_private_copy_and_lineage(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir)

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_kirocrew_agent_delete(_delete_request("design-crew"))

    assert resp.status == 200
    assert "design-crew" not in KiroCrewConfig.load().agents
    assert not (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is None
    # The shared origin is not the crew's to take with it.
    assert (agents_dir / "kirocrew.json").exists()
    # The unlink ran under the overlay's own sidecar lock too (a `config set
    # --local` binding cannot land between the check and the unlink); the
    # lockfile is created on first hold and nothing else here touches it.
    local = config_local_path()
    assert (local.parent / (local.name + ".lock")).exists()
    assert not local.exists()  # the check writes nothing to the overlay


@pytest.mark.asyncio
async def test_delete_keeps_copy_whose_lineage_names_another_crew(tmp_path):
    """private_to mismatch: the file carries another crew's customizations."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir, private_to="other-crew")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_kirocrew_agent_delete(_delete_request("design-crew"))

    assert resp.status == 200
    assert "design-crew" not in KiroCrewConfig.load().agents
    assert (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") == {
        "forked_from": "kirocrew",
        "private_to": "other-crew",
    }


@pytest.mark.asyncio
async def test_delete_keeps_copy_the_crew_was_not_bound_to(tmp_path):
    """Binding mismatch: lineage alone is not enough to take a file away."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir, bound_to="kirocrew")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_kirocrew_agent_delete(_delete_request("design-crew"))

    assert resp.status == 200
    assert "design-crew" not in KiroCrewConfig.load().agents
    assert (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is not None


@pytest.mark.asyncio
async def test_delete_keeps_copy_another_crew_is_still_bound_to(tmp_path):
    """A foreign binding on the copy (pre-dating the bind-time guard) is live
    use: deleting the file would break that crew with "Mode not found"."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir)
    cfg = KiroCrewConfig.load()
    cfg.agents["other-crew"] = KiroCrewAgentConfig(kiro_agent="design-crew")
    cfg.save()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_kirocrew_agent_delete(_delete_request("design-crew"))

    assert resp.status == 200
    assert "design-crew" not in KiroCrewConfig.load().agents
    assert (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is not None


@pytest.mark.asyncio
async def test_delete_succeeds_when_copy_cannot_be_removed(tmp_path, caplog):
    """Best-effort: a locked file keeps its lineage (so private content does
    not surface as shared) and the crew is still removed."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir)
    caplog.set_level(logging.DEBUG, logger="kiro_crew.dashboard.handlers.agents")

    def _refuse(self, missing_ok=False):
        raise PermissionError("locked")

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch("pathlib.Path.unlink", _refuse),
    ):
        resp = await api_kirocrew_agent_delete(_delete_request("design-crew"))

    assert resp.status == 200
    assert "design-crew" not in KiroCrewConfig.load().agents
    assert (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is not None
    # The unlink was attempted and refused — not skipped upstream.
    assert "could not remove superseded copy" in caplog.text


@pytest.mark.asyncio
async def test_delete_finds_lineage_when_crew_is_bound_by_file_stem(tmp_path):
    """Lineage is keyed by the copy's declared name; a binding may resolve the
    same file by its stem. The record must be found under either name."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "kirocrew")
    # File stem 'odd-stem' carries declared name 'design-copy'; lineage is
    # recorded under the declared name, the crew is bound by the stem.
    (agents_dir / "odd-stem.json").write_text(
        json.dumps({"name": "design-copy", "model": "claude-x"}), encoding="utf-8"
    )
    agent_state.set_fork_info("design-copy", forked_from="kirocrew", private_to="design-crew")
    cfg = KiroCrewConfig()
    cfg.agents = {
        "kirocrew": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "design-crew": KiroCrewAgentConfig(kiro_agent="odd-stem"),
    }
    cfg.default_agent = "kirocrew"
    cfg.save()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_kirocrew_agent_delete(_delete_request("design-crew"))

    assert resp.status == 200
    assert not (agents_dir / "odd-stem.json").exists()
    assert agent_state.get_fork_info("design-copy") is None
    assert (agents_dir / "kirocrew.json").exists()


@pytest.mark.asyncio
async def test_delete_keeps_lineage_of_unreadable_copy(tmp_path, caplog):
    """A malformed copy is still a file on disk. Its lineage must survive, or
    repairing the file would surface private content as a shared template."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir)
    (agents_dir / "design-crew.json").write_text("{not json", encoding="utf-8")
    caplog.set_level(logging.DEBUG, logger="kiro_crew.dashboard.handlers.agents")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_kirocrew_agent_delete(_delete_request("design-crew"))

    assert resp.status == 200
    assert "design-crew" not in KiroCrewConfig.load().agents
    assert (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") == {
        "forked_from": "kirocrew",
        "private_to": "design-crew",
    }
    assert "did not resolve to a readable spec; keeping its lineage" in caplog.text


@pytest.mark.asyncio
async def test_delete_keeps_lineage_of_malformed_copy_with_divergent_stem(tmp_path):
    """The hard case: the copy's file stem differs from its declared name AND
    the file is malformed, so no name known here can even locate it. Lineage
    stays — an inert record is cheaper than a private copy listed as shared."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "kirocrew")
    (agents_dir / "odd-stem.json").write_text("{not json", encoding="utf-8")
    agent_state.set_fork_info("design-copy", forked_from="kirocrew", private_to="design-crew")
    cfg = KiroCrewConfig()
    cfg.agents = {
        "kirocrew": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "design-crew": KiroCrewAgentConfig(kiro_agent="design-copy"),
    }
    cfg.default_agent = "kirocrew"
    cfg.save()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_kirocrew_agent_delete(_delete_request("design-crew"))

    assert resp.status == 200
    assert (agents_dir / "odd-stem.json").exists()
    assert agent_state.get_fork_info("design-copy") is not None


@pytest.mark.asyncio
async def test_delete_keeps_lineage_when_copy_file_is_already_gone(tmp_path):
    """An unresolved copy is never proof of absence, so the record stays; it
    is inert without a spec behind it and the crew is still removed."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir)
    (agents_dir / "design-crew.json").unlink()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_kirocrew_agent_delete(_delete_request("design-crew"))

    assert resp.status == 200
    assert "design-crew" not in KiroCrewConfig.load().agents
    assert agent_state.get_fork_info("design-crew") is not None


@pytest.mark.asyncio
async def test_delete_keeps_copy_bound_only_through_config_local_overlay(tmp_path):
    """config.local.json deep-merges over config.json, so a binding that lives
    only in the overlay is another crew's EFFECTIVE binding: the file stays."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir)
    config_local_path().write_text(
        json.dumps({"agents": {"other-crew": {"kiro_agent": "design-crew"}}}),
        encoding="utf-8",
    )

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_kirocrew_agent_delete(_delete_request("design-crew"))

    assert resp.status == 200
    assert "design-crew" not in KiroCrewConfig.load().agents
    assert (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is not None


@pytest.mark.asyncio
async def test_delete_keeps_copy_when_config_local_overlay_is_unreadable(tmp_path):
    """A binding that cannot be ruled out keeps the file (fail closed)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed(agents_dir)
    config_local_path().write_text("{not json", encoding="utf-8")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_kirocrew_agent_delete(_delete_request("design-crew"))

    assert resp.status == 200
    assert "design-crew" not in KiroCrewConfig.load().agents
    assert (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is not None
