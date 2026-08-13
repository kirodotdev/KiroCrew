"""Tests for kiro_crew.apps.scaffold — app scaffolding."""
from __future__ import annotations

import json

import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.scaffold import scaffold_app

# Every file scaffold_app writes and every directory it creates (all options
# on), relative to the app dir. The containment tests parameterize over these,
# and test_site_list_matches_what_scaffold_creates pins the list against what
# scaffold_app actually produces — so a newly added write site that is not
# also added here (and thereby covered by the symlink cases) fails the suite.
_SCAFFOLD_FILES = [
    "app.json",
    "agents/sample-agent.json",
    "skills/sample-skill/SKILL.md",
    "backend/server.py",
    "ui/package.json",
    "ui/vite.config.ts",
    "ui/src/App.tsx",
    "ui/.gitignore",
    "README.md",
]
_SCAFFOLD_DIRS = [
    "agents",
    "skills",
    "skills/sample-skill",
    "backend",
    "ui",
    "ui/src",
]


def _scaffold_all(output_dir, name):
    return scaffold_app(
        output_dir, name,
        include_backend=True, include_ui=True, include_cron=True,
    )


class TestWriteContainment:
    """Every write site refuses a path that resolves outside the app dir.

    Path.exists() is False for a dangling symlink, so an existence test on the
    joined path falls through to a write that follows the link and lands
    outside the app directory; a symlinked parent directory escapes the same
    way. Both shapes are exercised per write site. A refusal aborts the
    scaffold (never a silent skip: a skipped write would leave a partial app
    while the CLI reports success), and nothing lands outside the app dir.
    """

    def test_site_list_matches_what_scaffold_creates(self, tmp_path):
        app_dir = _scaffold_all(tmp_path, "probe")
        # as_posix(): the pinned lists use forward slashes; str() of a relative
        # WindowsPath yields backslashes and fails the comparison on Windows.
        files = {p.relative_to(app_dir).as_posix() for p in app_dir.rglob("*") if p.is_file()}
        dirs = {p.relative_to(app_dir).as_posix() for p in app_dir.rglob("*") if p.is_dir()}
        assert files == set(_SCAFFOLD_FILES)
        assert dirs == set(_SCAFFOLD_DIRS)

    @requires_symlinks
    @pytest.mark.parametrize("relpath", _SCAFFOLD_FILES)
    def test_write_refuses_to_follow_an_escaping_symlink(self, tmp_path, relpath):
        # requires_symlinks: a DANGLING file symlink has no junction equivalent
        # (junctions target existing directories), so unelevated Windows skips.
        outside = tmp_path / "outside-target"
        out = tmp_path / "out"
        link = out / "victim" / relpath
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert not outside.exists(), f"{relpath} wrote through a dangling symlink"

    @pytest.mark.parametrize("reldir", _SCAFFOLD_DIRS)
    def test_mkdir_refuses_an_escaping_dir_symlink(self, tmp_path, reldir):
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        out = tmp_path / "out"
        link = out / "victim" / reldir
        link.parent.mkdir(parents=True, exist_ok=True)
        # make_dir_link: a junction on Windows needs no privilege and resolves
        # through the same reparse machinery, so this stays exercised there.
        make_dir_link(link, outside)

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert list(outside.iterdir()) == [], (
            f"{reldir} let writes land outside the app dir"
        )

    def test_app_dir_symlink_escaping_output_dir_is_refused(self, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        out = tmp_path / "out"
        out.mkdir()
        make_dir_link(out / "victim", outside)

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert list(outside.iterdir()) == []

    def test_name_with_traversal_is_refused(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()

        with pytest.raises(ValueError):
            scaffold_app(out, "../evil")

        assert not (tmp_path / "evil").exists()

    def test_absolute_name_is_refused(self, tmp_path):
        """joinpath discards the root for an absolute component, so an absolute
        name would compare equal trivially while writing outside --dir."""
        out = tmp_path / "out"
        out.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        with pytest.raises(ValueError):
            scaffold_app(out, str(elsewhere / "evil"))

        assert not (elsewhere / "evil").exists()

    def test_app_dir_alias_of_a_sibling_project_is_refused(self, tmp_path):
        """An IN-ROOT alias passes plain containment: out/victim -> out/existing
        resolves inside the output dir, and scaffolding "victim" would truncate
        the sibling project's files. Exact-path equality refuses it."""
        out = tmp_path / "out"
        out.mkdir()
        existing = out / "existing"
        existing.mkdir()
        (existing / "app.json").write_text('{"name": "existing"}', encoding="utf-8")
        make_dir_link(out / "victim", existing)

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert (existing / "app.json").read_text(encoding="utf-8") == (
            '{"name": "existing"}'
        )

    def test_in_app_alias_of_a_sibling_dir_is_refused(self, tmp_path):
        """Same alias shape one level down: agents -> ./real inside the app dir
        resolves inside the root but is not the lexical path; refused."""
        out = tmp_path / "out"
        app_dir = out / "victim"
        (app_dir / "real").mkdir(parents=True)
        make_dir_link(app_dir / "agents", app_dir / "real")

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert list((app_dir / "real").iterdir()) == []

    @requires_symlinks
    def test_symlink_loop_raises_a_clear_error(self, tmp_path):
        """A self-referential symlink makes resolve() raise; the scaffold must
        abort with the containment ValueError, not an unexplained OSError.
        requires_symlinks: a junction cannot point at itself pre-creation, so
        the loop shape has no junction equivalent on unelevated Windows."""
        out = tmp_path / "out"
        app_dir = out / "victim"
        app_dir.mkdir(parents=True)
        (app_dir / "agents").symlink_to(app_dir / "agents")

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert not (app_dir / "agents" / "sample-agent.json").exists()

    def test_scaffold_through_a_symlinked_output_dir_succeeds(self, tmp_path):
        """A symlink in the user's own --dir is not an escape: both sides are
        resolved, so a symlinked home or /tmp on macOS compares equal."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        make_dir_link(link, real)

        app_dir = _scaffold_all(link, "my-app")

        assert (real / "my-app" / "app.json").is_file()
        assert (app_dir / "README.md").is_file()


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

    def test_scaffold_cli_refusal_prints_clean_error(self, tmp_path, monkeypatch, capsys):
        """A containment refusal exits 1 with the app actions' clean error
        contract on stderr, not a raw ValueError traceback."""
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        import argparse

        from kiro_crew.cli_commands import _handle_app
        out = tmp_path / "out"
        out.mkdir()
        ns = argparse.Namespace(app_action="init", name="../evil", dir=str(out), backend=False)
        with pytest.raises(SystemExit) as excinfo:
            _handle_app(ns)
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        # Pin the contract (clean error prefix on stderr), not the specific
        # refusal wording, which differs by escape shape.
        assert captured.err.startswith("\u274c ")
        assert not (tmp_path / "evil").exists()

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
