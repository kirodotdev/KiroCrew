"""Tests for the `kirocrew app` CLI subcommand."""
from __future__ import annotations

import json

import pytest

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app_source(tmp_path, name="cli-test-app"):
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "CLI Test App",
        "description": "App for CLI testing",
        "author": "tester",
        "agents": ["agents/test-agent.json"],
        "skills": ["skills/test-skill"],
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    (src / "agents").mkdir()
    (src / "agents" / "test-agent.json").write_text('{"name": "test-agent"}')
    (src / "skills" / "test-skill").mkdir(parents=True)
    (src / "skills" / "test-skill" / "SKILL.md").write_text("# Test Skill")
    return src


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    import kiro_crew.apps.bridges as bridges_mod
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    return {"home": home, "kiro_agents": kiro_agents}


# ---------------------------------------------------------------------------
# _handle_app unit tests (call the function directly, not subprocess)
# ---------------------------------------------------------------------------

class TestHandleApp:
    """Test _handle_app by simulating argparse Namespace objects."""

    def test_install_and_list(self, tmp_path, app_env):
        import argparse

        from kiro_crew.cli import _handle_app

        src = _make_app_source(tmp_path)

        # Install
        ns = argparse.Namespace(app_action="install", source=str(src))
        _handle_app(ns)  # should not raise

        # List
        from kiro_crew.apps.manager import list_apps
        apps = list_apps()
        assert len(apps) == 1
        assert apps[0]["name"] == "cli-test-app"

    def test_enable_disable(self, tmp_path, app_env):
        import argparse

        from kiro_crew.cli import _handle_app

        src = _make_app_source(tmp_path)
        install_app(src)

        # Enable
        ns = argparse.Namespace(app_action="enable", name="cli-test-app")
        _handle_app(ns)
        from kiro_crew.apps.manager import _read_installed
        meta = _read_installed("cli-test-app")
        assert meta is not None
        assert meta.enabled is True

        # Disable
        ns = argparse.Namespace(app_action="disable", name="cli-test-app")
        _handle_app(ns)
        meta = _read_installed("cli-test-app")
        assert meta is not None
        assert meta.enabled is False

    def test_info(self, tmp_path, app_env, capsys):
        import argparse

        from kiro_crew.cli import _handle_app

        src = _make_app_source(tmp_path)
        install_app(src)

        ns = argparse.Namespace(app_action="info", name="cli-test-app")
        _handle_app(ns)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["name"] == "cli-test-app"
        assert "manifest" in data

    def test_uninstall(self, tmp_path, app_env):
        import argparse

        from kiro_crew.cli import _handle_app

        src = _make_app_source(tmp_path)
        install_app(src)

        ns = argparse.Namespace(app_action="uninstall", name="cli-test-app", keep_data=False)
        _handle_app(ns)
        from kiro_crew.apps.manager import get_app
        assert get_app("cli-test-app") is None

    def test_install_invalid_source(self, app_env):
        import argparse

        from kiro_crew.cli import _handle_app

        ns = argparse.Namespace(app_action="install", source="/nonexistent")
        with pytest.raises(SystemExit):
            _handle_app(ns)

    def test_no_action_prints_usage(self, app_env, capsys):
        import argparse

        from kiro_crew.cli import _handle_app

        ns = argparse.Namespace(app_action=None)
        _handle_app(ns)
        captured = capsys.readouterr()
        assert "Usage" in captured.out
