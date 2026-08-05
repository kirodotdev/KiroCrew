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

    def test_shipped_workflows_is_hidden_and_deploy_web_is_gone(self) -> None:
        """`workflows` ships hidden; `deploy-web` was DELETED in the Artifact
        Deploy fold-in (capability lives in src/kiro_crew/deploy/ + /deploy
        console) and must not be discovered as a builtin at all."""
        shipped = {a["name"]: a for a in discover_builtin_apps()}
        assert "workflows" in shipped, "workflows builtin not discovered"
        assert shipped["workflows"].get("hidden") is True
        assert "deploy-web" not in shipped, (
            "deploy-web builtin should no longer exist — folded into Artifacts core"
        )


# ---------------------------------------------------------------------------


class TestAgentsAndSkillsSurviveDiscovery:
    """``agents`` / ``skills`` are typed ``AppManifest`` fields, not ``extra``.

    So ``_manifest_to_builtin_dict`` has to copy them EXPLICITLY. When it did
    not, both were silently stripped from the dict that ``register_builtin_apps``
    persists as the installed ``app.json`` — and ``bridges.register_app``
    re-reads that stripped file, so a builtin's declared agents were never
    symlinked into ``~/.kiro/agents`` and its skills were never registered. The
    manifest looked correct on disk in the package and the app was simply inert.

    A round-trip assertion is the only thing that catches this: every per-field
    unit test passed while the aggregate dict was missing two keys.
    """

    def test_declared_agents_and_skills_reach_discovery_output(self, tmp_path: Path) -> None:
        manifest = _valid_manifest("agentful-app")
        manifest["agents"] = ["agents/one.json", "agents/two.json"]
        manifest["skills"] = ["skills/the-skill"]
        _write_manifest(tmp_path / "agentful-app", manifest)

        apps = discover_builtin_apps(tmp_path)
        assert len(apps) == 1
        assert apps[0]["agents"] == ["agents/one.json", "agents/two.json"]
        assert apps[0]["skills"] == ["skills/the-skill"]

    def test_absent_agents_and_skills_stay_absent(self, tmp_path: Path) -> None:
        """Omit the keys rather than emitting empty lists.

        ``register_builtin_apps`` merges this dict over existing state, so an
        empty list would be a meaningful value that could clear a real one.
        """
        _write_manifest(tmp_path / "plain-app", _valid_manifest("plain-app"))

        apps = discover_builtin_apps(tmp_path)
        assert "agents" not in apps[0]
        assert "skills" not in apps[0]

    def test_shipped_manifests_keep_their_agents_and_skills(self) -> None:
        """The regression this guards, on the real shipped manifests.

        An app that dispatches to its own agents by name does so as its PRIMARY
        function, so a stripped ``agents`` list is not a degraded feature but a
        dead one — and the failure is silent, because the manifest on disk in the
        package still looks correct.

        Derived from what the packaged ``app.json`` files actually declare rather
        than a hardcoded app list: the invariant is "whatever a manifest declares
        survives discovery", which is what makes this hold for every builtin
        including ones added later. A named list would instead have to be edited
        by the very change most likely to break the round-trip.
        """
        import kiro_crew.apps.builtins as builtins_pkg

        builtins_dir = Path(builtins_pkg.__file__).resolve().parent
        shipped = {a["name"]: a for a in discover_builtin_apps()}

        checked = 0
        for manifest_path in sorted(builtins_dir.glob("*/app.json")):
            declared = json.loads(manifest_path.read_text(encoding="utf-8"))
            name = declared.get("name")
            for field in ("agents", "skills"):
                if not declared.get(field):
                    continue
                assert name in shipped, f"{name} builtin not discovered"
                assert shipped[name].get(field) == declared[field], (
                    f"{name} declares {field} in its manifest but discovery "
                    f"dropped or altered them"
                )
                checked += 1

        assert checked, (
            "no shipped builtin declares agents or skills, so this guard proved "
            "nothing — if that is now genuinely true, delete it"
        )
