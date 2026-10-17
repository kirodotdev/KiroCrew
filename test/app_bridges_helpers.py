"""Fixtures and helpers shared by the ``test_app_bridges*.py`` modules.

``app_env`` is defined as ``app_env_fixture`` under ``@pytest.fixture(name="app_env")`` so a
test module can import it without each test's ``app_env`` parameter shadowing the import.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME


def _make_app_source(tmp_path, name="test-app", **extras):
    """Create a minimal app source with agents and skills."""
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "A test app",
        "author": "tester",
        "agents": ["agents/my-agent.json"],
        "skills": ["skills/my-skill"],
        "crons": [{"name": "refresh", "every": 3600, "agent": "my-agent", "message": "go"}],
        "permissions": {"cron": True},
        **extras,
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    # Create agent file
    (src / "agents").mkdir()
    (src / "agents" / "my-agent.json").write_text(json.dumps({"name": "my-agent", "model": "auto"}))
    # Create skill directory
    (src / "skills" / "my-skill").mkdir(parents=True)
    (src / "skills" / "my-skill" / "SKILL.md").write_text("# My Skill\nDoes things.")
    return src


@pytest.fixture(name="app_env")
def app_env_fixture(tmp_path, monkeypatch):
    """Set up isolated KIROCREW_HOME and KIRO agents dir."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))

    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    # Patch the KIRO_AGENTS_DIR in bridges module
    import kiro_crew.apps.bridges as bridges_mod
    import kiro_crew.apps.execution as execution_mod

    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(
        execution_mod,
        "third_party_execution_allowed",
        lambda: True,
    )

    # Patch _mcp_json_path to avoid file descriptor errors in tests
    mcp_path = tmp_path / "mcp.json"
    monkeypatch.setattr(bridges_mod, "_mcp_json_path", lambda: mcp_path)

    return {"home": home, "kiro_agents": kiro_agents}


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_app_bridges*.py modules calls monkeypatch.undo()
def _stub_venv_probe(monkeypatch):
    """Replace the interpreter usability probe with its runnability check.

    The real probe executes the candidate interpreter under the OS sandbox;
    on hosts without a sandbox backend (Windows CI) it correctly reports "no
    positive evidence" and the venv is never preferred - which would flip
    every rewrite-plumbing expectation in this module per-platform. The
    plumbing is what these tests pin; the probe has its own dedicated tests.
    """
    from kiro_crew.apps import interpreter as _interp

    monkeypatch.setattr(
        _interp,
        "_venv_is_usable",
        lambda root: _interp._runnable(_interp.venv_python_path(root)),
    )
