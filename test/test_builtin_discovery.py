"""Property tests for Builtin Auto-Discovery.

Feature: app-sdk-gateway-hooks
Properties 7, 8: Discovery finds valid manifests with correct classification.
"""
from __future__ import annotations

import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.discovery import discover_builtin_apps

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_manifest(app_dir: Path, manifest: dict) -> None:
    """Write an app.json manifest to a directory."""
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "app.json").write_text(json.dumps(manifest, indent=2))


def _valid_manifest(name: str) -> dict:
    """Create a minimal valid manifest."""
    return {
        "name": name,
        "version": "1.0.0",
        "displayName": name.replace("-", " ").title(),
        "description": f"Test app {name}",
        "author": "test",
    }


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def _app_name() -> st.SearchStrategy[str]:
    """Generate valid kebab-case app names (no trailing hyphens)."""
    return st.from_regex(r"[a-z][a-z0-9]+(-[a-z0-9]+)*", fullmatch=True).filter(lambda s: len(s) <= 15)


# ---------------------------------------------------------------------------
# Property 7: Builtin discovery finds all valid manifests
# ---------------------------------------------------------------------------


class TestBuiltinDiscovery:
    """Property 7: Builtin discovery finds all valid manifests.

    **Validates: Requirements 3.1**
    """

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        valid_names=st.lists(_app_name(), min_size=0, max_size=5, unique=True),
        invalid_count=st.integers(min_value=0, max_value=3),
    )
    def test_discovers_exactly_valid_manifests(
        self, valid_names: list[str], invalid_count: int, tmp_path: Path,
    ) -> None:
        """Discovery returns exactly K entries for K valid manifests."""
        import uuid
        work_dir = tmp_path / uuid.uuid4().hex
        work_dir.mkdir()

        # Create valid app directories
        for name in valid_names:
            _write_manifest(work_dir / name, _valid_manifest(name))

        # Create invalid directories (no manifest or bad manifest)
        for i in range(invalid_count):
            bad_dir = work_dir / f"invalid-{i}"
            bad_dir.mkdir()
            if i % 2 == 0:
                # Missing app.json
                pass
            else:
                # Invalid JSON
                (bad_dir / "app.json").write_text("not json{{{")

        # Also create non-directory files (should be skipped)
        (work_dir / "README.md").write_text("# Builtins")

        apps = discover_builtin_apps(work_dir)
        discovered_names = {a["name"] for a in apps}

        assert discovered_names == set(valid_names)
        assert len(apps) == len(valid_names)

    def test_empty_directory_returns_empty(self, tmp_path: Path) -> None:
        """Empty builtins directory returns empty list."""
        apps = discover_builtin_apps(tmp_path)
        assert apps == []

    def test_nonexistent_directory_returns_empty(self, tmp_path: Path) -> None:
        """Non-existent directory returns empty list."""
        apps = discover_builtin_apps(tmp_path / "nonexistent")
        assert apps == []

    def test_skips_hidden_and_underscore_dirs(self, tmp_path: Path) -> None:
        """Directories starting with . or _ are skipped."""
        _write_manifest(tmp_path / ".hidden", _valid_manifest("hidden"))
        _write_manifest(tmp_path / "__pycache__", _valid_manifest("pycache"))
        _write_manifest(tmp_path / "valid-app", _valid_manifest("valid-app"))

        apps = discover_builtin_apps(tmp_path)
        assert len(apps) == 1
        assert apps[0]["name"] == "valid-app"

    def test_skips_manifest_with_validation_errors(self, tmp_path: Path) -> None:
        """Manifests that fail validation are skipped."""
        # Missing required fields
        bad_dir = tmp_path / "bad-app"
        bad_dir.mkdir()
        (bad_dir / "app.json").write_text(json.dumps({"name": "bad-app"}))

        _write_manifest(tmp_path / "good-app", _valid_manifest("good-app"))

        apps = discover_builtin_apps(tmp_path)
        assert len(apps) == 1
        assert apps[0]["name"] == "good-app"


# ---------------------------------------------------------------------------
# Property 8: Discovered builtins have correct classification
# ---------------------------------------------------------------------------


class TestBuiltinClassification:
    """Property 8: Discovered builtins have correct classification.

    **Validates: Requirements 3.2**
    """

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(name=_app_name())
    def test_discovered_app_has_required_fields(self, name: str, tmp_path: Path) -> None:
        """Each discovered app has name, version, displayName, description."""
        import uuid
        work_dir = tmp_path / uuid.uuid4().hex
        work_dir.mkdir()
        _write_manifest(work_dir / name, _valid_manifest(name))
        apps = discover_builtin_apps(work_dir)

        assert len(apps) == 1
        app = apps[0]
        assert app["name"] == name
        assert app["version"] == "1.0.0"
        assert app["displayName"]
        assert app["description"]

    def test_preserves_extra_fields(self, tmp_path: Path) -> None:
        """Extra fields like defaultEnabled and highlights are preserved."""
        manifest = _valid_manifest("my-app")
        manifest["defaultEnabled"] = False
        manifest["highlights"] = ["Feature 1", "Feature 2"]
        manifest["tags"] = ["test", "demo"]

        _write_manifest(tmp_path / "my-app", manifest)
        apps = discover_builtin_apps(tmp_path)

        assert len(apps) == 1
        app = apps[0]
        assert app["defaultEnabled"] is False
        assert app["highlights"] == ["Feature 1", "Feature 2"]
        assert app["tags"] == ["test", "demo"]

    def test_preserves_permissions_and_ui(self, tmp_path: Path) -> None:
        """Permissions and UI config are preserved in discovery output."""
        manifest = _valid_manifest("ui-app")
        manifest["permissions"] = {"api": ["/api/test"], "events": ["test_event"], "cron": True}
        manifest["ui"] = {"pages": [{"route": "/test", "label": "Test", "icon": "Zap"}]}

        _write_manifest(tmp_path / "ui-app", manifest)
        apps = discover_builtin_apps(tmp_path)

        assert len(apps) == 1
        app = apps[0]
        assert app["permissions"]["api"] == ["/api/test"]
        assert app["permissions"]["cron"] is True
        assert app["ui"]["pages"][0]["route"] == "/test"

    def test_preserves_backend_hooks(self, tmp_path: Path) -> None:
        """Backend hooks config is preserved in discovery output."""
        manifest = _valid_manifest("hooks-app")
        manifest["backend"] = {
            "hooks": {
                "routes": "backend.routes:register_routes",
                "on_startup": "backend.hooks:startup",
            }
        }

        _write_manifest(tmp_path / "hooks-app", manifest)
        apps = discover_builtin_apps(tmp_path)

        assert len(apps) == 1
        app = apps[0]
        assert app["backend"]["hooks"]["routes"] == "backend.routes:register_routes"
        assert app["backend"]["hooks"]["on_startup"] == "backend.hooks:startup"


# ---------------------------------------------------------------------------
# Store visibility: hidden builtins stay installed but drop out of Browse
# ---------------------------------------------------------------------------


class TestHiddenBuiltins:
    """The `hidden` manifest flag hides a builtin from the App Store Browse grid
    (filter at website AppsPage) while keeping it installed, routable, and on the
    Installed tab. `hidden` is not a _KNOWN_FIELDS key, so it must survive as an
    ``extra`` field through discovery and reach the frontend as manifest.hidden.
    """

    def test_hidden_flag_preserved_through_discovery(self, tmp_path: Path) -> None:
        """A `hidden: true` manifest field is preserved in discovery output."""
        manifest = _valid_manifest("hidden-app")
        manifest["hidden"] = True
        _write_manifest(tmp_path / "hidden-app", manifest)

        apps = discover_builtin_apps(tmp_path)
        assert len(apps) == 1
        assert apps[0]["hidden"] is True

    def test_shipped_workflows_and_deploy_web_are_hidden(self) -> None:
        """The shipped `workflows` and `deploy-web` builtins ship hidden from the
        store. Regression: these two must not appear in the Browse grid."""
        shipped = {a["name"]: a for a in discover_builtin_apps()}
        for name in ("workflows", "deploy-web"):
            assert name in shipped, f"{name} builtin not discovered"
            assert shipped[name].get("hidden") is True, (
                f"{name} must ship with hidden=True so it is excluded from the "
                f"App Store Browse grid (got {shipped[name].get('hidden')!r})"
            )
