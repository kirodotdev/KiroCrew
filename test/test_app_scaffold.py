"""Tests for kiro_crew.apps.scaffold — app scaffolding."""
from __future__ import annotations

import json

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.scaffold import scaffold_app


class TestScaffold:
    def test_basic_scaffold(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "my-test-app")
        assert app_dir.is_dir()
        assert (app_dir / "app.json").is_file()
        assert (app_dir / "agents" / "sample-agent.json").is_file()
        assert (app_dir / "skills" / "sample-skill" / "SKILL.md").is_file()
        assert (app_dir / "README.md").is_file()

        # Manifest should be valid
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert m.name == "my-test-app"
        assert m.validate() == []

    def test_scaffold_with_backend(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "backend-app", include_backend=True)
        assert (app_dir / "backend" / "server.py").is_file()
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert m.backend.entryPoint == "backend/server.py"

    def test_scaffold_without_backend(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "no-backend")
        assert not (app_dir / "backend").exists()

    def test_custom_metadata(self, tmp_path):
        app_dir = scaffold_app(
            tmp_path, "custom-app",
            display_name="Custom App",
            description="A custom description",
            author="testuser",
        )
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert m.displayName == "Custom App"
        assert m.description == "A custom description"
        assert m.author == "testuser"

    def test_agent_is_valid_json(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "json-check")
        agent = json.loads((app_dir / "agents" / "sample-agent.json").read_text(encoding="utf-8"))
        assert agent["name"] == "sample-agent"
        assert "model" in agent

    def test_skill_has_frontmatter(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "skill-check")
        content = (app_dir / "skills" / "sample-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert "---" in content
        assert "description:" in content

    def test_readme_has_name(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "readme-check")
        readme = (app_dir / "README.md").read_text(encoding="utf-8")
        assert "readme-check" in readme
        assert "kirocrew app install" in readme

    def test_scaffold_installable(self, tmp_path, monkeypatch):
        """Scaffolded app can be installed by the app manager."""
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        app_dir = scaffold_app(tmp_path / "output", "installable-app")
        from kiro_crew.apps.manager import install_app
        result = install_app(app_dir)
        assert result.ok, result.error

    def test_scaffold_cli_integration(self, tmp_path, monkeypatch, capsys):
        """Test the CLI init command via _handle_app."""
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        import argparse

        from kiro_crew.cli_commands import _handle_app
        ns = argparse.Namespace(app_action="init", name="cli-scaffolded", dir=str(tmp_path), backend=False)
        _handle_app(ns)
        captured = capsys.readouterr()
        assert "Scaffolded" in captured.out
        assert (tmp_path / "cli-scaffolded" / "app.json").is_file()

    def test_scaffold_with_ui(self, tmp_path):
        """--ui generates ui/ directory with package.json, vite config, and App.tsx."""
        app_dir = scaffold_app(tmp_path, "ui-app", include_ui=True)
        assert (app_dir / "ui" / "package.json").is_file()
        assert (app_dir / "ui" / "vite.config.ts").is_file()
        assert (app_dir / "ui" / "src" / "App.tsx").is_file()
        assert (app_dir / "ui" / ".gitignore").is_file()

        # package.json should reference the app name
        pkg = json.loads((app_dir / "ui" / "package.json").read_text(encoding="utf-8"))
        assert pkg["name"] == "ui-app-ui"
        assert "react" in pkg["dependencies"]
        assert "vite" in pkg["devDependencies"]

        # vite config should externalize shared modules
        vite_cfg = (app_dir / "ui" / "vite.config.ts").read_text(encoding="utf-8")
        assert "@kirocrew/app-sdk" in vite_cfg
        assert "@kirocrew/app-sdk/ui" in vite_cfg

        # App.tsx should have a valid component
        app_tsx = (app_dir / "ui" / "src" / "App.tsx").read_text(encoding="utf-8")
        assert "useAppApi" in app_tsx
        assert "PageHeader" in app_tsx

    def test_scaffold_with_ui_manifest_valid(self, tmp_path):
        """--ui scaffold produces a valid manifest with ui fields."""
        app_dir = scaffold_app(tmp_path, "ui-valid", include_ui=True)
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert m.validate() == []
        assert m.ui.entry == "dist/index.mjs"
        assert len(m.ui.pages) == 1
        assert m.ui.pages[0].route == "/apps/ui-valid"

    def test_scaffold_without_ui(self, tmp_path):
        """Without --ui, no ui/ directory is created."""
        app_dir = scaffold_app(tmp_path, "no-ui")
        assert not (app_dir / "ui").exists()

    def test_scaffold_with_cron(self, tmp_path):
        """--cron generates a sample cron entry in app.json."""
        app_dir = scaffold_app(tmp_path, "cron-app", include_cron=True)
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert m.validate() == []
        assert len(m.crons) == 1
        assert m.crons[0].name == "cron-app-check"
        assert m.crons[0].every == 300

    def test_scaffold_without_cron(self, tmp_path):
        """Without --cron, no crons in manifest."""
        app_dir = scaffold_app(tmp_path, "no-cron")
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert len(m.crons) == 0

    def test_scaffold_all_options(self, tmp_path):
        """All flags together produce a valid manifest."""
        app_dir = scaffold_app(
            tmp_path, "full-app",
            include_backend=True, include_ui=True, include_cron=True,
        )
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert m.validate() == []
        assert m.backend.entryPoint == "backend/server.py"
        assert m.ui.entry == "dist/index.mjs"
        assert len(m.crons) == 1
        assert (app_dir / "backend" / "server.py").is_file()
        assert (app_dir / "ui" / "src" / "App.tsx").is_file()
