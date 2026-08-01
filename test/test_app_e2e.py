"""End-to-end integration tests for the app platform.

Tests the full lifecycle: manifest parsing → install → register → enable →
disable → deregister → uninstall. Also validates OncallWatchTower's actual
app.json manifest.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.bridges import deregister_app, register_app
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    disable_app,
    enable_app,
    get_app,
    install_app,
    list_apps,
    uninstall_app,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.permissions import validate_permissions

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_full_app(tmp_path, name="e2e-app"):
    """Create a full app with agents, skills, crons, and backend."""
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.2.3",
        "displayName": "E2E Test App",
        "description": "Full lifecycle test app",
        "author": "tester",
        "agents": ["agents/analyst.json", "agents/fetcher.json"],
        "skills": ["skills/triage", "skills/diagnosis"],
        "sops": ["sops/runbook.sop.md"],
        "crons": [
            {"name": "refresh", "every": 3600, "agent": "fetcher", "message": "refresh data"},
            {"name": "daily-report", "cron_expr": "0 9 * * MON-FRI", "message": "daily report"},
        ],
        "permissions": {
            "mcpTools": ["ToolA", "ToolB"],
            "storage": True,
            "cron": True,
        },
        "tags": ["testing", "e2e"],
        "jobFamilies": ["SDE"],
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))

    # Agents
    (src / "agents").mkdir()
    (src / "agents" / "analyst.json").write_text(json.dumps({"name": "analyst", "model": "auto"}))
    (src / "agents" / "fetcher.json").write_text(json.dumps({"name": "fetcher", "model": "auto"}))

    # Skills
    for skill in ("triage", "diagnosis"):
        d = src / "skills" / skill
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"# {skill.title()}\nDomain knowledge for {skill}.")

    # SOPs
    (src / "sops").mkdir()
    (src / "sops" / "runbook.sop.md").write_text("# Runbook\nStep 1: Check logs.")

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
    # Patch _MCP_JSON_PATH to avoid file descriptor errors in tests
    mcp_path = tmp_path / "mcp.json"
    monkeypatch.setattr(bridges_mod, "_MCP_JSON_PATH", mcp_path)
    import kiro_crew.apps.backend as bmod
    bmod._processes.clear()
    bmod._allocated_ports.clear()
    monkeypatch.setattr(
        "kiro_crew.apps.execution.third_party_execution_allowed", lambda: True
    )
    return {"home": home, "kiro_agents": kiro_agents}


# ---------------------------------------------------------------------------
# Full lifecycle test
# ---------------------------------------------------------------------------

class TestFullLifecycle:
    def test_install_register_enable_disable_uninstall(self, tmp_path, app_env):
        src = _make_full_app(tmp_path)

        # 1. Install
        result = install_app(src)
        assert result.ok, result.error
        assert result.name == "e2e-app"

        # Verify installed
        meta = _read_installed("e2e-app")
        assert meta is not None
        assert meta.version == "1.2.3"
        assert meta.enabled is False

        # 2. Register resources
        reg = register_app("e2e-app")
        assert len(reg.agents) == 2
        assert len(reg.skills) == 2
        assert len(reg.crons) == 2
        assert reg.errors == []

        # Verify agent symlinks
        kiro = app_env["kiro_agents"]
        assert (kiro / "e2e-app--analyst.json").is_symlink()
        assert (kiro / "e2e-app--fetcher.json").is_symlink()

        # Verify skill symlinks
        skills_dir = app_env["home"] / "skills" / "e2e-app"
        assert (skills_dir / "triage" / "SKILL.md").is_file()
        assert (skills_dir / "diagnosis" / "SKILL.md").is_file()

        # 3. Enable
        enable_result = enable_app("e2e-app")
        assert enable_result.ok
        meta = _read_installed("e2e-app")
        assert meta.enabled is True

        # 4. Listing
        apps = list_apps()
        assert len(apps) == 1
        assert apps[0]["name"] == "e2e-app"
        assert apps[0]["enabled"] is True

        # 5. Get app info
        info = get_app("e2e-app")
        assert info is not None
        assert info["manifest"]["agents"] == ["agents/analyst.json", "agents/fetcher.json"]

        # 6. Disable
        disable_result = disable_app("e2e-app")
        assert disable_result.ok
        meta = _read_installed("e2e-app")
        assert meta.enabled is False

        # 7. Deregister
        dereg = deregister_app("e2e-app")
        assert dereg.errors == []
        assert not (kiro / "e2e-app--analyst.json").exists()
        assert not (skills_dir).exists()

        # 8. Uninstall
        uninstall_result = uninstall_app("e2e-app")
        assert uninstall_result.ok
        assert get_app("e2e-app") is None
        assert list_apps() == []


# ---------------------------------------------------------------------------
# OncallWatchTower manifest validation
# ---------------------------------------------------------------------------

class TestOncallWatchTowerManifest:
    """Validate OncallWatchTower's actual app.json against our manifest parser."""

    @pytest.fixture()
    def owt_manifest_path(self):
        # Navigate from test dir to the OncallWatchTower package
        candidates = [
            Path(__file__).parent.parent.parent / "OncallWatchTower" / "app.json",
            Path(__file__).parent.parent.parent.parent / "OncallWatchTower" / "app.json",
        ]
        for p in candidates:
            if p.is_file():
                return p
        pytest.skip("OncallWatchTower app.json not found in workspace")

    def test_parse_manifest(self, owt_manifest_path):
        m = AppManifest.from_json_file(owt_manifest_path)
        assert m.name == "oncall-watchtower"
        assert m.version == "0.2.0"
        assert m.displayName == "Oncall Watch Tower"

    def test_validate_manifest(self, owt_manifest_path):
        m = AppManifest.from_json_file(owt_manifest_path)
        errors = m.validate()
        assert errors == [], f"Validation errors: {errors}"

    def test_agents_declared(self, owt_manifest_path):
        m = AppManifest.from_json_file(owt_manifest_path)
        assert len(m.agents) >= 10  # OWT has 13 agents
        assert any("ticket-analyst" in a for a in m.agents)
        assert any("pipeline-analyst" in a for a in m.agents)

    def test_skills_declared(self, owt_manifest_path):
        m = AppManifest.from_json_file(owt_manifest_path)
        assert len(m.skills) >= 20  # OWT has 24 skills
        assert any("ticket-triage" in s for s in m.skills)

    def test_crons_declared(self, owt_manifest_path):
        m = AppManifest.from_json_file(owt_manifest_path)
        assert len(m.crons) == 3
        names = {c.name for c in m.crons}
        assert "refresh-pipelines" in names
        assert "refresh-tickets" in names
        assert "refresh-reviews" in names

    def test_ui_pages_declared(self, owt_manifest_path):
        m = AppManifest.from_json_file(owt_manifest_path)
        assert len(m.ui.pages) == 3
        routes = {p.route for p in m.ui.pages}
        assert "/apps/oncall-watchtower" in routes

    def test_backend_declared(self, owt_manifest_path):
        m = AppManifest.from_json_file(owt_manifest_path)
        assert m.backend.entryPoint == "src/backend/app.py"
        assert m.backend.healthCheck == "/api/version"

    def test_permissions_declared(self, owt_manifest_path):
        m = AppManifest.from_json_file(owt_manifest_path)
        assert m.permissions.storage is True
        assert m.permissions.cron is True
        assert len(m.permissions.mcpTools) >= 5

    def test_permissions_validation(self, owt_manifest_path):
        m = AppManifest.from_json_file(owt_manifest_path)
        check = validate_permissions(m)
        assert check.allowed is True
        assert check.denied == []

    def test_round_trip(self, owt_manifest_path):
        m = AppManifest.from_json_file(owt_manifest_path)
        serialized = json.loads(m.to_json())
        m2 = AppManifest.from_dict(serialized)
        assert m2.to_dict() == m.to_dict()

    def test_tags_and_job_families(self, owt_manifest_path):
        m = AppManifest.from_json_file(owt_manifest_path)
        assert "oncall" in m.tags
        assert "SDE" in m.jobFamilies
