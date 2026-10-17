"""A manifest that schedules ``crons`` must declare ``permissions.cron``.

The rule is enforced where an app is admitted with a new manifest -- install,
update and self-registration -- and never at load time, so an app installed
before it declared the grant keeps loading and firing its crons. The repo's own
scaffold and example manifests satisfy it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kiro_crew.apps.bridges import load_app_cron_defs, register_app
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    get_app_manifest,
    install_app,
    register_external_app,
    update_app,
)
from kiro_crew.apps.manifest import (
    CRON_PERMISSION_REQUIRED_ERROR,
    AppManifest,
    cron_permission_error,
)
from kiro_crew.apps.scaffold import scaffold_app

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CRON = {"name": "refresh", "every": 3600, "message": "go"}


def _manifest(**overrides) -> dict:
    base = {
        "name": "cron-app",
        "version": "1.0.0",
        "displayName": "Cron App",
        "description": "An app that schedules a cron",
        "author": "tester",
    }
    base.update(overrides)
    return base


def _write_source(tmp_path: Path, folder: str, manifest: dict) -> Path:
    src = tmp_path / "source" / folder
    src.mkdir(parents=True)
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return src


def _drop_cron_grant(manifest_path: Path) -> None:
    """Rewrite an installed manifest to the shape an app had before the rule."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data.pop("permissions", None)
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    import kiro_crew.apps.bridges as bridges_mod
    import kiro_crew.apps.execution as execution_mod

    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(bridges_mod, "_mcp_json_path", lambda: tmp_path / "mcp.json")
    monkeypatch.setattr(execution_mod, "third_party_execution_allowed", lambda: True)
    return home


class TestValidateRule:
    def test_crons_without_the_grant_is_one_error_naming_the_fix(self):
        errors = AppManifest.from_dict(_manifest(crons=[_CRON])).validate()
        assert errors == [CRON_PERMISSION_REQUIRED_ERROR]
        assert '"cron": true' in CRON_PERMISSION_REQUIRED_ERROR
        assert '"permissions"' in CRON_PERMISSION_REQUIRED_ERROR

    def test_crons_with_the_grant_is_valid(self):
        m = AppManifest.from_dict(_manifest(crons=[_CRON], permissions={"cron": True}))
        assert m.validate() == []
        assert cron_permission_error(m) == ""

    @pytest.mark.parametrize("permissions", [None, {}, {"cron": True}, {"cron": False}])
    def test_no_crons_is_valid_with_or_without_the_grant(self, permissions):
        overrides = {"crons": []}
        if permissions is not None:
            overrides["permissions"] = permissions
        assert AppManifest.from_dict(_manifest(**overrides)).validate() == []

    @pytest.mark.parametrize("value", [False, "true", 1, None])
    def test_a_grant_that_is_not_literal_true_is_refused(self, value):
        m = AppManifest.from_dict(_manifest(crons=[_CRON], permissions={"cron": value}))
        assert CRON_PERMISSION_REQUIRED_ERROR in m.validate()

    def test_a_cron_shipped_paused_still_needs_the_grant(self):
        m = AppManifest.from_dict(_manifest(crons=[{**_CRON, "enabled": False}]))
        assert CRON_PERMISSION_REQUIRED_ERROR in m.validate()


class TestInstallAndUpdate:
    def test_install_refuses_crons_without_the_grant(self, tmp_path, app_home):
        src = _write_source(tmp_path, "v1", _manifest(crons=[_CRON]))
        result = install_app(src)
        assert not result.ok
        assert CRON_PERMISSION_REQUIRED_ERROR in result.error
        assert _read_installed("cron-app") is None

    def test_install_accepts_crons_with_the_grant(self, tmp_path, app_home):
        src = _write_source(tmp_path, "v1", _manifest(crons=[_CRON], permissions={"cron": True}))
        assert install_app(src).ok

    def test_update_refuses_and_leaves_the_installed_app_in_place(self, tmp_path, app_home):
        v1 = _write_source(tmp_path, "v1", _manifest(crons=[_CRON], permissions={"cron": True}))
        assert install_app(v1).ok
        v2 = _write_source(tmp_path, "v2", _manifest(version="1.1.0", crons=[_CRON]))
        result = update_app(v2)
        assert not result.ok
        assert CRON_PERMISSION_REQUIRED_ERROR in result.error
        installed = get_app_manifest("cron-app")
        assert installed is not None
        assert installed.version == "1.0.0"
        assert installed.permissions.cron is True


class TestInstalledBeforeTheRule:
    def test_an_installed_app_without_the_grant_keeps_registering_its_crons(
        self, tmp_path, app_home
    ):
        src = _write_source(tmp_path, "v1", _manifest(crons=[_CRON], permissions={"cron": True}))
        assert install_app(src).ok
        _drop_cron_grant(app_home / "apps" / "cron-app" / APP_MANIFEST_FILENAME)

        loaded = get_app_manifest("cron-app")
        assert loaded is not None
        assert loaded.permissions.cron is False
        assert loaded.crons

        result = register_app("cron-app")
        assert result.errors == []
        assert [d["name"] for d in load_app_cron_defs("cron-app")] == ["cron-app/refresh"]


class TestSelfRegistration:
    def test_first_registration_without_the_grant_is_refused(self, app_home):
        result = register_external_app(
            "cron-app", "1.0.0", "Cron App", manifest_data=_manifest(crons=[_CRON])
        )
        assert not result.ok
        assert CRON_PERMISSION_REQUIRED_ERROR in result.error
        assert _read_installed("cron-app") is None

    def test_registration_with_the_grant_is_accepted(self, app_home):
        result = register_external_app(
            "cron-app",
            "1.0.0",
            "Cron App",
            manifest_data=_manifest(crons=[_CRON], permissions={"cron": True}),
        )
        assert result.ok, result.error

    def test_relaunch_at_the_recorded_version_keeps_an_app_registered_before_the_rule(
        self, app_home
    ):
        assert register_external_app(
            "cron-app",
            "1.0.0",
            "Cron App",
            manifest_data=_manifest(crons=[_CRON], permissions={"cron": True}),
        ).ok
        _drop_cron_grant(app_home / "apps" / "cron-app" / APP_MANIFEST_FILENAME)

        result = register_external_app(
            "cron-app", "1.0.0", "Cron App", manifest_data=_manifest(crons=[_CRON])
        )
        assert result.ok, result.error
        recorded = get_app_manifest("cron-app")
        assert recorded is not None
        assert recorded.crons

    def test_a_new_version_without_the_grant_is_refused(self, app_home):
        assert register_external_app(
            "cron-app",
            "1.0.0",
            "Cron App",
            manifest_data=_manifest(crons=[_CRON], permissions={"cron": True}),
        ).ok
        _drop_cron_grant(app_home / "apps" / "cron-app" / APP_MANIFEST_FILENAME)

        result = register_external_app(
            "cron-app",
            "1.1.0",
            "Cron App",
            manifest_data=_manifest(version="1.1.0", crons=[_CRON]),
        )
        assert not result.ok
        assert CRON_PERMISSION_REQUIRED_ERROR in result.error
        recorded = get_app_manifest("cron-app")
        assert recorded is not None
        assert recorded.version == "1.0.0"

    def test_relaunch_that_drops_the_grant_is_refused(self, app_home):
        assert register_external_app(
            "cron-app",
            "1.0.0",
            "Cron App",
            manifest_data=_manifest(crons=[_CRON], permissions={"cron": True}),
        ).ok
        result = register_external_app(
            "cron-app", "1.0.0", "Cron App", manifest_data=_manifest(crons=[_CRON])
        )
        assert not result.ok
        assert CRON_PERMISSION_REQUIRED_ERROR in result.error
        recorded = get_app_manifest("cron-app")
        assert recorded is not None
        assert recorded.permissions.cron is True


class TestShippedManifests:
    def test_the_cron_scaffold_validates(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "cron-app", include_cron=True)
        m = AppManifest.from_json_file(app_dir / APP_MANIFEST_FILENAME)
        assert m.crons
        assert m.permissions.cron is True
        assert m.validate() == []

    def test_the_full_app_example_validates(self):
        path = _REPO_ROOT / "docs" / "app-kit" / "examples" / "full-app" / APP_MANIFEST_FILENAME
        m = AppManifest.from_json_file(path)
        assert m.crons
        assert cron_permission_error(m) == ""

    def test_the_reference_full_example_declares_the_grant(self):
        text = (_REPO_ROOT / "docs" / "app-kit" / "manifest-reference.md").read_text(
            encoding="utf-8"
        )
        section = text.split("## Full Example", 1)[1]
        block = re.search(r"```json\n(.*?)\n```", section, re.S)
        assert block is not None
        m = AppManifest.from_dict(json.loads(block.group(1)))
        assert m.crons
        assert cron_permission_error(m) == ""

    def test_every_builtin_that_ships_crons_declares_the_grant(self):
        builtins = _REPO_ROOT / "src" / "kiro_crew" / "apps" / "builtins"
        with_crons = []
        for path in sorted(builtins.glob(f"*/{APP_MANIFEST_FILENAME}")):
            m = AppManifest.from_json_file(path)
            if m.crons:
                with_crons.append(m.name)
                assert cron_permission_error(m) == "", m.name
        assert with_crons
