"""Write-safety coverage for agent-spec bookkeeping migration."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import kiro_crew.agent as agent_mod
from conftest import requires_symlinks


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", directory)
    return directory


def test_valid_spec_is_cleaned_and_counted(agents_dir, monkeypatch):
    path = agents_dir / "agent.json"
    path.write_text(json.dumps({"name": "agent", "model_managed": True}), encoding="utf-8")

    real_reader = agent_mod._read_agent_spec
    seen = []

    def _reader(spec_path, **attribution):
        seen.append(attribution)
        return real_reader(spec_path, **attribution)

    monkeypatch.setattr(agent_mod, "_read_agent_spec", _reader)

    assert agent_mod.migrate_agent_specs() == 1
    assert "model_managed" not in json.loads(path.read_text(encoding="utf-8"))
    assert seen == [{"operation": "migrate_agent_specs", "source": "unknown"}]


def test_oversized_spec_is_not_rewritten(agents_dir, monkeypatch):
    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 64)
    path = agents_dir / "large.json"
    original = json.dumps({"name": "large", "cc_model": "x", "pad": "y" * 512})
    path.write_text(original, encoding="utf-8")

    assert agent_mod.migrate_agent_specs() == 0
    assert path.read_text(encoding="utf-8") == original


def test_reader_denial_keeps_migrate_attribution(agents_dir, monkeypatch):
    from kiro_crew import agent_discovery

    path = agents_dir / "denied.json"
    original = json.dumps({"name": "denied", "model_managed": True})
    path.write_text(original, encoding="utf-8")
    security_log = MagicMock()
    monkeypatch.setattr(agent_discovery, "is_sensitive_path", lambda value: True)
    monkeypatch.setattr(agent_discovery, "_sel", lambda: security_log)

    assert agent_mod.migrate_agent_specs() == 0
    assert path.read_text(encoding="utf-8") == original
    security_log.log_api_access.assert_called_once_with(
        caller="agent_discovery",
        operation="migrate_agent_specs",
        outcome="denied",
        source="unknown",
        resources=str(path.resolve()),
        error="sensitive path rejected",
    )


@requires_symlinks
def test_symlink_is_refused_before_read_or_rewrite(agents_dir, tmp_path):
    target = tmp_path / "outside.json"
    original = json.dumps({"name": "victim", "model_managed": True})
    target.write_text(original, encoding="utf-8")
    link = agents_dir / "linked.json"
    link.symlink_to(target)

    assert agent_mod.migrate_agent_specs() == 0
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == original
