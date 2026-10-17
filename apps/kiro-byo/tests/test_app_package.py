from __future__ import annotations

import json
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_byo.integration_manager import (
    IntegrationTarget,
    _created_file_is_owned,
    _write_runtime_snapshot,
    compatibility_contract_issues,
    install_from_app,
    integration_status,
    quick_integration_status,
    _app_build_id,
    uninstall_from_app,
)


ROOT = Path(__file__).resolve().parents[1]


class AppPackageTests(unittest.TestCase):
    def test_manifest_is_installable_kirocrew_app(self) -> None:
        manifest = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "kiro-byo")
        self.assertEqual(manifest["displayName"], "Kiro BYO")
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(manifest["minKiroCrewVersion"], "0.9.0")
        self.assertEqual(manifest["backend"]["hooks"]["routes"], "backend.routes:register_routes")
        self.assertEqual(manifest["ui"]["pages"][0]["route"], "/kiro-byo")
        self.assertEqual(manifest["platform"]["os"], ["windows"])


    def test_brand_rename_preserves_compatibility_identities(self) -> None:
        import tomllib
        from kiro_byo.protocol import AGENT_NAME

        manifest = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        ui = (ROOT / "ui" / "index.mjs").read_text(encoding="utf-8")

        self.assertEqual(manifest["displayName"], "Kiro BYO")
        self.assertEqual(manifest["ui"]["pages"][0]["label"], "Kiro BYO")
        self.assertEqual(AGENT_NAME, "Kiro BYO")
        self.assertIn("Provider-neutral ACP Harness for KiroCrew", readme)
        self.assertIn("Kiro BYO", ui)

        # Compatibility-sensitive IDs deliberately remain historical.
        self.assertEqual(manifest["name"], "kiro-byo")
        self.assertEqual(manifest["permissions"]["api"], ["/api/apps/kiro-byo/*"])
        self.assertEqual(manifest["ui"]["pages"][0]["route"], "/kiro-byo")
        self.assertEqual(pyproject["project"]["name"], "kiro-byo")
        self.assertEqual(pyproject["project"]["scripts"]["kiro-byo"], "kiro_byo.cli:main")
        self.assertTrue((ROOT / "src" / "kiro_byo").is_dir())
        self.assertTrue((ROOT / "integration" / "kiro_crew" / "v0.9-adaptive" / "overlay" / "src" / "kiro_crew" / "acp" / "harness" / "kiro_byo.py").is_file())


    def test_overlay_dependency_contract_fails_closed_when_symbol_moves(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            crew = Path(td) / "kiro_crew"
            source = crew / "agent.py"
            source.parent.mkdir(parents=True)
            source.write_text("def still_here():\n    return True\n", encoding="utf-8")
            compat = {
                "required_contract_symbols": {
                    "src/kiro_crew/agent.py": ["required_symbol"],
                }
            }
            issues = compatibility_contract_issues(compat, crew)
            self.assertEqual(len(issues), 1)
            self.assertIn("overlay contract moved", issues[0])
            # A comment or string mentioning the old name must not satisfy the
            # contract: the overlay imports a real module-level binding.
            source.write_text(
                "# required_symbol\nNOTE = 'required_symbol'\ndef still_here():\n    return True\n",
                encoding="utf-8",
            )
            self.assertEqual(len(compatibility_contract_issues(compat, crew)), 1)
            source.write_text("def required_symbol():\n    return True\n", encoding="utf-8")
            self.assertEqual(compatibility_contract_issues(compat, crew), [])

    def test_overlay_dynamic_export_contract_is_structural(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            crew = Path(td) / "kiro_crew"
            source = crew / "agent.py"
            source.parent.mkdir(parents=True)
            compat = {
                "required_contract_symbols": {
                    "src/kiro_crew/agent.py": ["_EXPORTS_BY_OWNER", "__getattr__"],
                },
                "required_contract_exports": {
                    "src/kiro_crew/agent.py": {
                        "_EXPORTS_BY_OWNER": ["dynamic_api"],
                    },
                },
            }
            source.write_text(
                "from typing import TYPE_CHECKING\n"
                "_EXPORTS_BY_OWNER = {'owner': ('dynamic_api',)}\n"
                "if not TYPE_CHECKING:\n"
                "    def __getattr__(name):\n"
                "        return name\n",
                encoding="utf-8",
            )
            self.assertEqual(compatibility_contract_issues(compat, crew), [])
            source.write_text(
                "from typing import TYPE_CHECKING\n"
                "_EXPORTS_BY_OWNER = {'owner': ('other_api',)}\n"
                "if not TYPE_CHECKING:\n"
                "    def __getattr__(name):\n"
                "        return name\n"
                "# dynamic_api\n",
                encoding="utf-8",
            )
            issues = compatibility_contract_issues(compat, crew)
            self.assertEqual(len(issues), 1)
            self.assertIn("overlay export contract moved", issues[0])

    def test_runtime_snapshot_keeps_overlay_contract_for_future_self_heal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compat = {
                "family_regex": r"^\d+\.\d+\.\d+$",
                "verified_family_regex": r"^0\.9\.0$",
                "min_version": "0.9.0",
                "mode": "adaptive_semantic",
                "transformer": "adaptive09",
                "generated_files": [],
                "created_files": [],
                "required_contract_symbols": {
                    "src/kiro_crew/agent.py": ["required_symbol"],
                },
                "required_contract_exports": {
                    "src/kiro_crew/agent.py": {"_EXPORTS_BY_OWNER": ["dynamic_api"]},
                },
            }
            target = IntegrationTarget(
                "0.9.0", root / "kiro_crew", root, root / "adapter",
                "test", {"files": []}, compat,
            )
            _overlay, _manifest, compatibility_path = _write_runtime_snapshot(
                target, root / "home", {}, {},
            )
            saved = json.loads(compatibility_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["required_contract_symbols"], compat["required_contract_symbols"])
            self.assertEqual(saved["required_contract_exports"], compat["required_contract_exports"])
            self.assertEqual(saved["verified_family_regex"], compat["verified_family_regex"])
            self.assertEqual(saved["min_version"], "0.9.0")

        bootstrap = (ROOT / "src" / "kiro_byo" / "bootstrap_patch.py").read_text(encoding="utf-8")
        self.assertIn("compatibility_contract_issues", bootstrap)
        self.assertIn("self-repair skipped", bootstrap)

    def test_release_tree_contains_no_runtime_or_secret_state(self) -> None:
        # Tests and local runs must never leak mutable runtime state into a
        # distributable source/app bundle. Templates/examples are allowed; live
        # session/provider state is not.
        forbidden_roots = (
            ROOT / "data" / "sessions",
            ROOT / "data" / "telemetry",
            ROOT / "data" / "runtime_trace",
            ROOT / "data" / "audit",
        )
        leaked = [
            path.relative_to(ROOT).as_posix()
            for root in forbidden_roots
            if root.exists()
            for path in root.rglob("*")
            if path.is_file()
        ]
        forbidden_files = (
            ROOT / "config" / "providers.json",
            ROOT / "config" / "native_tools.json",
        )
        leaked.extend(path.relative_to(ROOT).as_posix() for path in forbidden_files if path.is_file())
        self.assertEqual(leaked, [], f"runtime/secret state leaked into release tree: {leaked}")

    def test_release_version_surfaces_are_consistent(self) -> None:
        import tomllib
        from kiro_byo import __version__
        from kiro_byo.protocol import AGENT_VERSION

        manifest = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(expected, "0.1.0")
        self.assertEqual(manifest["version"], expected)
        self.assertEqual(pyproject["project"]["version"], expected)
        self.assertEqual(__version__, expected)
        self.assertEqual(AGENT_VERSION, expected)

    def test_app_ui_is_provider_first_and_capability_dynamic(self) -> None:
        ui = (ROOT / "ui" / "index.mjs").read_text(encoding="utf-8")
        self.assertIn("连接并发现模型", ui)
        self.assertIn("thinkingText", ui)
        self.assertIn("effortText", ui)
        self.assertIn("深度思考可开关", ui)
        self.assertIn("思考强度可调", ui)
        self.assertIn("status.overview", ui)
        self.assertIn("ka-side-status", ui)
        self.assertIn("ka-page-head", ui)
        self.assertIn("返回系统总览", ui)
        self.assertIn(".ka-overview-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));grid-auto-rows:210px", ui)
        self.assertIn(".ka-overview-grid>.ka-overview-card{box-sizing:border-box;height:100%;min-height:0;margin:0", ui)
        self.assertIn("className:'ka-overview-card'", ui)
        self.assertIn("运行来源 / 部署诊断", ui)
        self.assertIn("`${nativeToolCount} 个可用`", ui)
        self.assertNotIn("ka-overview-stack", ui)
        self.assertNotIn("?token=", ui)
        self.assertIn("title:'模式'", ui)
        self.assertIn("['modes','模式','◆']", ui)
        self.assertIn("else if(tab==='modes') body=h(ModeSelector", ui)
        self.assertNotIn("h(ModeSelector,{router:router||{},onReload})", ui)
        self.assertNotIn("'默认'", ui)
        self.assertIn("{id:'dyna',label:'Dyna'}", ui)
        self.assertIn("{id:'gaia',label:'Gaia'}", ui)
        self.assertIn("Coming soon", ui)
        self.assertIn("这里只展示，不在此切换", ui)
        self.assertIn("在 KiroCrew 对话框中选择当前 Mode 声明的 Type", ui)
        self.assertIn("localized_name", ui)
        self.assertNotIn("changePolicy", ui)
        self.assertIn("status.mode||{}", ui)
        self.assertNotIn("Kiro BYO " + "Auto", ui)
        self.assertNotIn("AutoRouter", ui)


    def test_overview_grid_resets_generic_adjacent_panel_margin(self) -> None:
        ui = (ROOT / "ui" / "index.mjs").read_text(encoding="utf-8")
        # Generic stacked panels intentionally use .ka-panel+.ka-panel margin,
        # but overview cards are CSS-grid items. The stronger/later selector
        # must reset that margin or cards 2-4 shift down by 12px.
        self.assertIn(".ka-panel+.ka-panel{margin-top:12px}", ui)
        self.assertIn(".ka-overview-grid>.ka-overview-card{box-sizing:border-box;height:100%;min-height:0;margin:0", ui)
        self.assertLess(ui.index(".ka-panel+.ka-panel{margin-top:12px}"), ui.index(".ka-overview-grid>.ka-overview-card{"))

    def test_created_harness_upgrade_recognizes_previous_runtime_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_home = root / "home"
            crew_version = "0.9.0-insider.3"
            rel = "src/kiro_crew/acp/harness/kiro_byo.py"
            live = root / "site" / "kiro_crew" / "acp" / "harness" / "kiro_byo.py"
            packaged = root / "new" / rel
            previous = app_home / "data" / "integration-runtime" / crew_version / "overlay" / rel
            for path in (live, packaged, previous):
                path.parent.mkdir(parents=True, exist_ok=True)
            live.write_text("OLD KIRO API\n", encoding="utf-8")
            previous.write_text("OLD KIRO API\n", encoding="utf-8")
            packaged.write_text("NEW KIRO API\n", encoding="utf-8")
            self.assertTrue(_created_file_is_owned(
                target=live, packaged_overlay=packaged, app_home=app_home,
                crew_version=crew_version, relative=rel,
            ))
            live.write_text("FOREIGN EXTENSION\n", encoding="utf-8")
            self.assertFalse(_created_file_is_owned(
                target=live, packaged_overlay=packaged, app_home=app_home,
                crew_version=crew_version, relative=rel,
            ))


    def test_future_crew_version_can_pass_semantic_preflight_but_is_marked_unaudited(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "app"
            app_home = root / "home"
            crew_pkg = root / "site-packages" / "kiro_crew"
            site_packages = crew_pkg.parent
            crew_version = "0.9.1-insider.1"
            rel = "src/kiro_crew/acp/harness/kiro_byo.py"
            adapter = app / "integration" / "kiro_crew" / "v0.9-adaptive"
            live = crew_pkg / "acp" / "harness" / "kiro_byo.py"
            overlay = adapter / "overlay" / rel
            for path in (live, overlay):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("CURRENT KIRO API HARNESS\n", encoding="utf-8")
            (adapter / "manifest.json").write_text(json.dumps({"files": [rel]}), encoding="utf-8")
            (adapter / "compatibility.json").write_text(json.dumps({
                "family_regex": r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z._-]*)?$",
                "verified_family_regex": r"^0\.9\.0(?:-insider\.[0-9]+)?$",
                "baseline": "semantic-test",
                "mode": "adaptive_semantic",
                "transformer": "adaptive09",
                "generated_files": [],
                "created_files": [rel],
            }), encoding="utf-8")
            app.mkdir(parents=True, exist_ok=True)
            (app / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=(crew_version, crew_pkg, site_packages),
            ):
                status = integration_status(app, app_home)
            self.assertTrue(status["supported"])
            self.assertFalse(status["version_audited"])
            self.assertEqual(status["crew_version"], crew_version)

    def test_quick_integration_status_detects_same_version_build_and_restart_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "app"
            app_home = root / "home"
            crew_pkg = root / "site-packages" / "kiro_crew"
            site_packages = crew_pkg.parent
            runtime = site_packages / "kiro_byo"
            (app / "src" / "kiro_byo").mkdir(parents=True)
            (app / "integration" / "kiro_crew").mkdir(parents=True)
            (app / "VERSION").write_text("0.1.0\n", encoding="utf-8")
            (app / "src" / "kiro_byo" / "runtime.py").write_text("BUILD = 1\n", encoding="utf-8")
            (app / "integration" / "kiro_crew" / "profile.json").write_text("{}\n", encoding="utf-8")
            app_build = _app_build_id(app)
            runtime.mkdir(parents=True)
            (runtime / "_bootstrap_state.json").write_text(json.dumps({
                "kiro_byo_version": "0.1.0",
                "kiro_byo_build": app_build,
                "kiro_byo_home": str(app_home),
            }), encoding="utf-8")
            data = app_home / "data"
            data.mkdir(parents=True)
            (data / "integration.json").write_text(json.dumps({
                "detected_version": "0.9.0-insider.6",
                "installed_by_pid": 101,
            }), encoding="utf-8")
            # The process that performed the install is still running: files are
            # current on disk, but core Python/frontend code has not crossed the
            # restart boundary yet.
            (data / "activation.json").write_text(json.dumps({
                "pid": 101,
                "kiro_byo_build": app_build,
            }), encoding="utf-8")
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=("0.9.0-insider.6", crew_pkg, site_packages),
            ):
                pending = quick_integration_status(app, app_home)
            self.assertTrue(pending["installed"])
            self.assertFalse(pending["active"])
            self.assertTrue(pending["restart_required"])

            # A fresh Gateway imports bootstrap_patch and records a different PID.
            (data / "activation.json").write_text(json.dumps({
                "pid": 202,
                "kiro_byo_build": app_build,
            }), encoding="utf-8")
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=("0.9.0-insider.6", crew_pkg, site_packages),
            ):
                active = quick_integration_status(app, app_home)
            self.assertTrue(active["active"])
            self.assertFalse(active["restart_required"])

            # Keeping public version 0.1.0 must not hide a newer same-version build.
            (app / "src" / "kiro_byo" / "runtime.py").write_text("BUILD = 2\n", encoding="utf-8")
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=("0.9.0-insider.6", crew_pkg, site_packages),
            ):
                stale = quick_integration_status(app, app_home)
            self.assertFalse(stale["installed"])
            self.assertFalse(stale["runtime_current"])

    def test_integration_status_rejects_stale_runtime_even_when_seams_are_current(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "app"
            app_home = root / "home"
            crew_pkg = root / "site-packages" / "kiro_crew"
            site_packages = crew_pkg.parent
            crew_version = "0.9.0-insider.3"
            rel = "src/kiro_crew/acp/harness/kiro_byo.py"
            adapter = app / "integration" / "kiro_crew" / "v0.9-adaptive"
            live = crew_pkg / "acp" / "harness" / "kiro_byo.py"
            overlay = adapter / "overlay" / rel
            runtime = site_packages / "kiro_byo"
            for path in (live, overlay):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("CURRENT KIRO API HARNESS\n", encoding="utf-8")
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "_bootstrap_state.json").write_text(json.dumps({
                "kiro_byo_version": "0.9.16",
                "kiro_byo_home": str(app_home),
            }), encoding="utf-8")
            app.mkdir(parents=True, exist_ok=True)
            (app / "VERSION").write_text("0.9.18\n", encoding="utf-8")
            (adapter / "manifest.json").write_text(json.dumps({"files": [rel]}), encoding="utf-8")
            (adapter / "compatibility.json").write_text(json.dumps({
                "family_regex": r"0\.9\.0-insider\.[0-9]+",
                "baseline": "0.9 adaptive registry/capabilities (shared AcpRuntime)",
                "mode": "adaptive_semantic",
                "transformer": "adaptive09",
                "generated_files": [],
                "created_files": [rel],
            }), encoding="utf-8")
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=(crew_version, crew_pkg, site_packages),
            ):
                status = integration_status(app, app_home)
            self.assertTrue(status["supported"])
            self.assertFalse(status["installed"])
            self.assertFalse(status["runtime_current"])
            self.assertEqual(status["runtime_version"], "0.9.16")
            self.assertEqual(status["app_version"], "0.9.18")
            self.assertTrue(any(x.get("state") == "stale-runtime" for x in status["pending_files"]))

    def test_integration_status_accepts_matching_runtime_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "app"
            app_home = root / "home"
            crew_pkg = root / "site-packages" / "kiro_crew"
            site_packages = crew_pkg.parent
            crew_version = "0.9.0-insider.3"
            rel = "src/kiro_crew/acp/harness/kiro_byo.py"
            adapter = app / "integration" / "kiro_crew" / "v0.9-adaptive"
            live = crew_pkg / "acp" / "harness" / "kiro_byo.py"
            overlay = adapter / "overlay" / rel
            runtime = site_packages / "kiro_byo"
            for path in (live, overlay):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("CURRENT KIRO API HARNESS\n", encoding="utf-8")
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "_bootstrap_state.json").write_text(json.dumps({
                "kiro_byo_version": "0.9.18",
                "kiro_byo_home": str(app_home),
            }), encoding="utf-8")
            app.mkdir(parents=True, exist_ok=True)
            (app / "VERSION").write_text("0.9.18\n", encoding="utf-8")
            (adapter / "manifest.json").write_text(json.dumps({"files": [rel]}), encoding="utf-8")
            (adapter / "compatibility.json").write_text(json.dumps({
                "family_regex": r"0\.9\.0-insider\.[0-9]+",
                "baseline": "0.9 adaptive registry/capabilities (shared AcpRuntime)",
                "mode": "adaptive_semantic",
                "transformer": "adaptive09",
                "generated_files": [],
                "created_files": [rel],
            }), encoding="utf-8")
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=(crew_version, crew_pkg, site_packages),
            ):
                status = integration_status(app, app_home)
            self.assertTrue(status["installed"])
            self.assertTrue(status["runtime_current"])

    def test_previous_created_file_snapshot_is_supported_as_update(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "app"
            app_home = root / "home"
            crew_pkg = root / "site-packages" / "kiro_crew"
            site_packages = crew_pkg.parent
            crew_version = "0.9.0-insider.3"
            rel = "src/kiro_crew/acp/harness/kiro_byo.py"
            adapter = app / "integration" / "kiro_crew" / "v0.9-adaptive"
            live = crew_pkg / "acp" / "harness" / "kiro_byo.py"
            overlay = adapter / "overlay" / rel
            previous = app_home / "data" / "integration-runtime" / crew_version / "overlay" / rel
            for path in (live, overlay, previous):
                path.parent.mkdir(parents=True, exist_ok=True)
            live.write_text("OLD KIRO API\n", encoding="utf-8")
            previous.write_text("OLD KIRO API\n", encoding="utf-8")
            overlay.write_text("NEW KIRO API\n", encoding="utf-8")
            (adapter / "manifest.json").write_text(json.dumps({"files": [rel]}), encoding="utf-8")
            (adapter / "compatibility.json").write_text(json.dumps({
                "family_regex": r"0\.9\.0-insider\.[0-9]+",
                "baseline": "0.9 adaptive registry/capabilities (shared AcpRuntime)",
                "mode": "adaptive_semantic",
                "transformer": "adaptive09",
                "generated_files": [],
                "created_files": [rel],
            }), encoding="utf-8")
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=(crew_version, crew_pkg, site_packages),
            ):
                status = integration_status(app, app_home)
            self.assertTrue(status["supported"])
            self.assertFalse(status["installed"])
            self.assertEqual(status["pending_files"][0]["state"], "changed")


    def test_install_upgrades_previous_owned_created_harness(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "app"
            app_home = root / "home"
            crew_pkg = root / "site-packages" / "kiro_crew"
            site_packages = crew_pkg.parent
            crew_version = "0.9.0-insider.3"
            rel = "src/kiro_crew/acp/harness/kiro_byo.py"
            adapter = app / "integration" / "kiro_crew" / "v0.9-adaptive"
            live = crew_pkg / "acp" / "harness" / "kiro_byo.py"
            overlay = adapter / "overlay" / rel
            previous = app_home / "data" / "integration-runtime" / crew_version / "overlay" / rel
            for path in (live, overlay, previous):
                path.parent.mkdir(parents=True, exist_ok=True)
            live.write_text("OLD KIRO API\n", encoding="utf-8")
            previous.write_text("OLD KIRO API\n", encoding="utf-8")
            overlay.write_text("NEW KIRO API\n", encoding="utf-8")
            (adapter / "manifest.json").write_text(json.dumps({"files": [rel]}), encoding="utf-8")
            (adapter / "compatibility.json").write_text(json.dumps({
                "family_regex": r"0\.9\.0-insider\.[0-9]+",
                "baseline": "0.9 adaptive registry/capabilities (shared AcpRuntime)",
                "mode": "adaptive_semantic",
                "transformer": "adaptive09",
                "generated_files": [],
                "created_files": [rel],
            }), encoding="utf-8")
            runtime_src = app / "src" / "kiro_byo"
            runtime_src.mkdir(parents=True)
            (runtime_src / "__init__.py").write_text('__version__ = "0.9.18"\n', encoding="utf-8")
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=(crew_version, crew_pkg, site_packages),
            ):
                result = install_from_app(app, app_home, version="0.9.18")
            self.assertTrue(result["ok"])
            self.assertEqual(live.read_text(encoding="utf-8"), "NEW KIRO API\n")
            snap = app_home / "data" / "integration-runtime" / crew_version / "overlay" / rel
            self.assertEqual(snap.read_text(encoding="utf-8"), "NEW KIRO API\n")


    def test_install_does_not_block_on_owned_patched_seam_when_pristine_backup_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "app"
            app_home = root / "home"
            crew_pkg = root / "site-packages" / "kiro_crew"
            site_packages = crew_pkg.parent
            crew_version = "0.9.0-insider.1"
            rel = "src/kiro_crew/a.py"
            live = crew_pkg / "a.py"
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_text("PATCHED:owned\n", encoding="utf-8")
            adapter = app / "integration" / "kiro_crew" / "v-test"
            adapter.mkdir(parents=True)
            (adapter / "manifest.json").write_text(json.dumps({"files": [rel]}), encoding="utf-8")
            (adapter / "compatibility.json").write_text(json.dumps({
                "family_regex": r"0\.9\.0-insider\.[0-9]+",
                "baseline": "test",
                "mode": "adaptive_semantic",
                "transformer": "adaptive09",
                "generated_files": [rel],
                "created_files": [],
            }), encoding="utf-8")
            runtime_src = app / "src" / "kiro_byo"
            runtime_src.mkdir(parents=True)
            (runtime_src / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")

            def fake_inspect(_rel: str, text: str):
                return types.SimpleNamespace(
                    state="patched" if text.startswith("PATCHED:") else "clean", detail=""
                )

            def fake_transform(_rel: str, text: str) -> str:
                return "PATCHED:" + text

            transformer = (frozenset([rel]), RuntimeError, fake_inspect, fake_transform)
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=(crew_version, crew_pkg, site_packages),
            ), patch(
                "kiro_byo.integration_manager._transformer_for",
                return_value=transformer,
            ):
                result = install_from_app(app, app_home, version="0.1.0")

            self.assertTrue(result["ok"])
            self.assertIn(rel, result["baseline_missing_files"])
            self.assertEqual(live.read_text(encoding="utf-8"), "PATCHED:owned\n")


    def test_install_and_uninstall_patch_and_restore_byo_dashboard_picker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "app"
            app_home = root / "home"
            crew_pkg = root / "site-packages" / "kiro_crew"
            site_packages = crew_pkg.parent
            crew_version = "0.9.0-insider.1"
            rel = "src/kiro_crew/a.py"
            live = crew_pkg / "a.py"
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_text("BASE\n", encoding="utf-8")
            dashboard = crew_pkg / "static" / "dist"
            dist = dashboard / "assets"
            dist.mkdir(parents=True)
            index_html = dashboard / "index.html"
            original_html = '<!doctype html><html><head></head><body></body></html>'
            index_html.write_text(original_html, encoding="utf-8")
            js = dist / "index-abc.js"
            # The stable ModelDropdownList DOM contract survives bundling; the
            # installer must not depend on minified withAutoFirst/Jev syntax.
            original_js = 'const x={"data-model-id":m.name};'
            js.write_text(original_js, encoding="utf-8")

            adapter = app / "integration" / "kiro_crew" / "v-test"
            adapter.mkdir(parents=True)
            (adapter / "manifest.json").write_text(json.dumps({"files": [rel]}), encoding="utf-8")
            (adapter / "compatibility.json").write_text(json.dumps({
                "family_regex": r"0\.9\.0-insider\.[0-9]+",
                "baseline": "test",
                "mode": "adaptive_semantic",
                "transformer": "adaptive09",
                "generated_files": [rel],
                "created_files": [],
            }), encoding="utf-8")
            runtime_src = app / "src" / "kiro_byo"
            runtime_src.mkdir(parents=True)
            (runtime_src / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")

            def fake_inspect(_rel: str, text: str):
                return types.SimpleNamespace(
                    state="patched" if text.startswith("PATCHED:") else "clean", detail=""
                )

            def fake_transform(_rel: str, text: str) -> str:
                return "PATCHED:" + text

            transformer = (frozenset([rel]), RuntimeError, fake_inspect, fake_transform)
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=(crew_version, crew_pkg, site_packages),
            ), patch(
                "kiro_byo.integration_manager._transformer_for",
                return_value=transformer,
            ):
                result = install_from_app(app, app_home, version="0.1.0")
                self.assertTrue(result["ok"])
                patched_html = index_html.read_text(encoding="utf-8")
                self.assertIn("kiro-byo:model-picker-guard", patched_html)
                self.assertIn('data-model-id^="kiro-byo-type:"', patched_html)
                # Content-hashed JS is no longer rewritten.
                self.assertEqual(js.read_text(encoding="utf-8"), original_js)
                state = json.loads((app_home / "data" / "integration.json").read_text(encoding="utf-8"))
                self.assertEqual(len(state["dashboard_assets"]), 1)
                self.assertTrue(state["dashboard_assets"][0].endswith("static/dist/index.html"))
                uninstalled = uninstall_from_app(app, app_home)

            self.assertTrue(uninstalled["ok"])
            self.assertEqual(index_html.read_text(encoding="utf-8"), original_html)
            self.assertEqual(js.read_text(encoding="utf-8"), original_js)
            self.assertEqual(live.read_text(encoding="utf-8"), "BASE\n")

    def test_app_ui_uses_host_theme_tokens_and_diagnostics_clear(self) -> None:
        ui = (ROOT / "ui" / "index.mjs").read_text(encoding="utf-8")
        self.assertIn("useTheme", ui)
        self.assertIn("--ka-surface:var(--card,", ui)
        self.assertIn("--ka-input:var(--input,", ui)
        self.assertIn("清除诊断", ui)
        self.assertIn("method:'DELETE'", ui)
        self.assertIn("getAttribute('data-mode')", ui)
        self.assertIn("runtime_current", ui)
        self.assertIn("语义兼容 · 未审计版本", ui)
        self.assertIn("--ka-surface-2:var(--panel,", ui)
        self.assertIn("--ka-hover:var(--bg-hover,", ui)
        self.assertIn("data-ka-scheme", ui)
        self.assertIn("function KSelect", ui)
        self.assertNotIn("h('select'", ui)
        self.assertIn("background:var(--ka-surface);border-radius:14px", ui)
        self.assertNotIn(".ka-select option{", ui)

    def test_hook_loader_synthetic_package_can_import_bundled_runtime(self) -> None:
        # KiroCrew loads hook files under a synthetic per-app package instead of
        # adding the app root to global sys.path.  The App must therefore use
        # package-relative imports for its bundled runtime.
        root_name = "_kirocrew_app_test_kiro_byo"
        root_pkg = types.ModuleType(root_name)
        root_pkg.__path__ = [str(ROOT)]
        backend_pkg = types.ModuleType(root_name + ".backend")
        backend_pkg.__path__ = [str(ROOT / "backend")]
        fake_kiro = types.ModuleType("kiro_crew")
        fake_apps = types.ModuleType("kiro_crew.apps")
        fake_rr = types.ModuleType("kiro_crew.apps.route_registry")

        class AppRoute:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        fake_rr.AppRoute = AppRoute
        additions = {
            root_name: root_pkg,
            root_name + ".backend": backend_pkg,
            "kiro_crew": fake_kiro,
            "kiro_crew.apps": fake_apps,
            "kiro_crew.apps.route_registry": fake_rr,
        }
        old = {name: sys.modules.get(name) for name in additions}
        try:
            sys.modules.update(additions)
            spec = importlib.util.spec_from_file_location(
                root_name + ".backend.routes", ROOT / "backend" / "routes.py"
            )
            self.assertIsNotNone(spec)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            self.assertEqual(module._version(), "0.1.0")
            self.assertTrue(callable(module.register_routes))
        finally:
            for name in list(sys.modules):
                if name == root_name or name.startswith(root_name + "."):
                    sys.modules.pop(name, None)
            for name, value in old.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

    def test_install_rolls_back_all_crew_seams_when_postwrite_step_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "app"
            crew_pkg = root / "site-packages" / "kiro_crew"
            site_packages = crew_pkg.parent
            app_home = root / "app-data"
            adapter = app / "integration" / "kiro_crew" / "v-test"
            rels = [
                "src/kiro_crew/a.py",
                "src/kiro_crew/b.py",
            ]
            created_rel = "src/kiro_crew/acp/harness/kiro_byo.py"
            for index, rel in enumerate(rels, 1):
                target = crew_pkg / rel[len("src/kiro_crew/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"BASE-{index}\n", encoding="utf-8")
            created_overlay = adapter / "overlay" / created_rel
            created_overlay.parent.mkdir(parents=True, exist_ok=True)
            created_overlay.write_text("CREATED-HARNESS\n", encoding="utf-8")
            (adapter / "manifest.json").write_text(
                json.dumps({"files": rels + [created_rel]}), encoding="utf-8"
            )
            (adapter / "compatibility.json").write_text(
                json.dumps({
                    "family_regex": r"0\.9\.0-insider\.1",
                    "baseline": "0.9.0-insider.1",
                    "mode": "adaptive_semantic",
                    "transformer": "adaptive09",
                    "generated_files": rels,
                    "created_files": [created_rel],
                }),
                encoding="utf-8",
            )
            runtime_src = app / "src" / "kiro_byo"
            runtime_src.mkdir(parents=True)
            (runtime_src / "__init__.py").write_text('__version__ = "0.9.10"\n', encoding="utf-8")

            def fake_inspect(rel: str, text: str):
                state = "patched" if text.startswith("PATCHED:") else "clean"
                return types.SimpleNamespace(state=state, detail="")

            def fake_transform(rel: str, text: str) -> str:
                return f"PATCHED:{rel}\n{text}"

            transformer = (frozenset(rels), RuntimeError, fake_inspect, fake_transform)
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=("0.9.0-insider.1", crew_pkg, site_packages),
            ), patch(
                "kiro_byo.integration_manager._transformer_for",
                return_value=transformer,
            ), patch(
                "kiro_byo.integration_manager._write_runtime_snapshot",
                side_effect=RuntimeError("synthetic failure after Crew writes"),
            ):
                with self.assertRaisesRegex(Exception, "synthetic failure"):
                    install_from_app(app, app_home, version="0.9.10")

            self.assertEqual((crew_pkg / "a.py").read_text(encoding="utf-8"), "BASE-1\n")
            self.assertEqual((crew_pkg / "b.py").read_text(encoding="utf-8"), "BASE-2\n")
            self.assertFalse((crew_pkg / "acp" / "harness" / "kiro_byo.py").exists())


    def test_install_failure_after_runtime_copy_restores_all_integration_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "app"
            app_home = root / "home"
            crew_pkg = root / "site-packages" / "kiro_crew"
            site_packages = crew_pkg.parent
            crew_version = "0.9.0-insider.1"
            rel = "src/kiro_crew/a.py"
            live = crew_pkg / "a.py"
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_text("BASE\n", encoding="utf-8")

            adapter = app / "integration" / "kiro_crew" / "v-test"
            adapter.mkdir(parents=True)
            (adapter / "manifest.json").write_text(json.dumps({"files": [rel]}), encoding="utf-8")
            (adapter / "compatibility.json").write_text(json.dumps({
                "family_regex": r"0\.9\.0-insider\.1",
                "baseline": "test",
                "mode": "adaptive_semantic",
                "transformer": "adaptive09",
                "generated_files": [rel],
                "created_files": [],
            }), encoding="utf-8")

            runtime_src = app / "src" / "kiro_byo"
            runtime_src.mkdir(parents=True)
            (runtime_src / "__init__.py").write_text('NEW = True\n', encoding="utf-8")
            (runtime_src / "new_module.py").write_text('NEW_MODULE = True\n', encoding="utf-8")

            runtime_dst = site_packages / "kiro_byo"
            runtime_dst.mkdir(parents=True)
            (runtime_dst / "__init__.py").write_text('OLD = True\n', encoding="utf-8")
            (runtime_dst / "stale.py").write_text('STALE = True\n', encoding="utf-8")
            (runtime_dst / "_bootstrap_state.json").write_text('{"old":true}\n', encoding="utf-8")
            pth = site_packages / "kiro_byo_bootstrap.pth"
            pth.write_text("OLD PTH\n", encoding="utf-8")
            integration = app_home / "data" / "integration.json"
            integration.parent.mkdir(parents=True, exist_ok=True)
            integration.write_text('{"old":true}\n', encoding="utf-8")
            snapshot = app_home / "data" / "integration-runtime" / crew_version
            snapshot.mkdir(parents=True)
            (snapshot / "old.txt").write_text("OLD SNAPSHOT\n", encoding="utf-8")

            def fake_inspect(_rel: str, text: str):
                return types.SimpleNamespace(state="patched" if text.startswith("PATCHED:") else "clean", detail="")

            def fake_transform(_rel: str, text: str) -> str:
                return "PATCHED:" + text

            transformer = (frozenset([rel]), RuntimeError, fake_inspect, fake_transform)
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=(crew_version, crew_pkg, site_packages),
            ), patch(
                "kiro_byo.integration_manager._transformer_for",
                return_value=transformer,
            ), patch(
                "kiro_byo.integration_manager._app_build_id",
                side_effect=RuntimeError("synthetic failure after runtime copy"),
            ):
                with self.assertRaisesRegex(Exception, "synthetic failure after runtime copy"):
                    install_from_app(app, app_home, version="0.1.0")

            self.assertEqual(live.read_text(encoding="utf-8"), "BASE\n")
            self.assertEqual((runtime_dst / "__init__.py").read_text(encoding="utf-8"), 'OLD = True\n')
            self.assertTrue((runtime_dst / "stale.py").is_file())
            self.assertFalse((runtime_dst / "new_module.py").exists())
            self.assertEqual((runtime_dst / "_bootstrap_state.json").read_text(encoding="utf-8"), '{"old":true}\n')
            self.assertEqual(pth.read_text(encoding="utf-8"), "OLD PTH\n")
            self.assertEqual(integration.read_text(encoding="utf-8"), '{"old":true}\n')
            self.assertEqual((snapshot / "old.txt").read_text(encoding="utf-8"), "OLD SNAPSHOT\n")


    def test_install_then_uninstall_restores_crew_and_removes_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "app"
            crew_pkg = root / "site-packages" / "kiro_crew"
            site_packages = crew_pkg.parent
            app_home = root / "app-data"
            adapter = app / "integration" / "kiro_crew" / "v-test"
            generated_rel = "src/kiro_crew/a.py"
            created_rel = "src/kiro_crew/acp/harness/kiro_byo.py"

            generated_live = crew_pkg / "a.py"
            generated_live.parent.mkdir(parents=True, exist_ok=True)
            generated_live.write_text("BASE\n", encoding="utf-8")
            created_overlay = adapter / "overlay" / created_rel
            created_overlay.parent.mkdir(parents=True, exist_ok=True)
            created_overlay.write_text("CREATED-HARNESS\n", encoding="utf-8")
            (adapter / "manifest.json").write_text(
                json.dumps({"files": [generated_rel, created_rel]}), encoding="utf-8"
            )
            (adapter / "compatibility.json").write_text(
                json.dumps({
                    "family_regex": r"0\.9\.0-insider\.1",
                    "baseline": "test",
                    "mode": "adaptive_semantic",
                    "transformer": "adaptive09",
                    "generated_files": [generated_rel],
                    "created_files": [created_rel],
                }), encoding="utf-8"
            )
            runtime_src = app / "src" / "kiro_byo"
            runtime_src.mkdir(parents=True)
            (runtime_src / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")

            def fake_inspect(rel: str, text: str):
                state = "patched" if text.startswith("PATCHED:") else "clean"
                return types.SimpleNamespace(state=state, detail="")

            def fake_transform(rel: str, text: str) -> str:
                return f"PATCHED:{rel}\n{text}"

            transformer = (frozenset([generated_rel]), RuntimeError, fake_inspect, fake_transform)
            with patch(
                "kiro_byo.integration_manager.running_crew",
                return_value=("0.9.0-insider.1", crew_pkg, site_packages),
            ), patch(
                "kiro_byo.integration_manager._transformer_for",
                return_value=transformer,
            ):
                installed = install_from_app(app, app_home, version="1.0.0")
                self.assertTrue(installed["ok"])
                self.assertTrue(generated_live.read_text(encoding="utf-8").startswith("PATCHED:"))
                self.assertTrue((crew_pkg / "acp" / "harness" / "kiro_byo.py").is_file())
                self.assertTrue((site_packages / "kiro_byo" / "_bootstrap_state.json").is_file())
                result = uninstall_from_app(app, app_home)

            self.assertTrue(result["ok"])
            self.assertTrue(result["restart_required"])
            self.assertEqual(generated_live.read_text(encoding="utf-8"), "BASE\n")
            self.assertFalse((crew_pkg / "acp" / "harness" / "kiro_byo.py").exists())
            self.assertFalse((site_packages / "kiro_byo").exists())
            self.assertFalse((site_packages / "kiro_byo_bootstrap.pth").exists())
            self.assertFalse((app_home / "data" / "integration.json").exists())

    def test_uninstall_refuses_to_overwrite_post_install_foreign_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_home = root / "app-data"
            crew_pkg = root / "site-packages" / "kiro_crew"
            site_packages = crew_pkg.parent
            rel = "src/kiro_crew/a.py"
            live = crew_pkg / "a.py"
            backup = app_home / "data" / "backups" / "packaged-0.9.0-insider.1" / "a.py"
            overlay_root = app_home / "data" / "integration-runtime" / "0.9.0-insider.1" / "overlay"
            snapshot = overlay_root / rel
            for path in (live, backup, snapshot):
                path.parent.mkdir(parents=True, exist_ok=True)
            live.write_text("FOREIGN CHANGE\n", encoding="utf-8")
            backup.write_text("BASE\n", encoding="utf-8")
            snapshot.write_text("KIRO PATCH\n", encoding="utf-8")
            (app_home / "data" / "integration.json").write_text(json.dumps({
                "detected_version": "0.9.0-insider.1",
                "crew_package": str(crew_pkg),
                "site_packages": str(site_packages),
                "backup_root": str(backup.parent),
                "runtime_overlay_root": str(overlay_root),
                "generated_files": [rel],
                "created_files": [],
            }), encoding="utf-8")

            with self.assertRaisesRegex(Exception, "changed after Kiro BYO installed"):
                uninstall_from_app(root / "app", app_home)
            self.assertEqual(live.read_text(encoding="utf-8"), "FOREIGN CHANGE\n")

    def test_backend_exposes_provider_and_integration_routes(self) -> None:
        routes = (ROOT / "backend" / "routes.py").read_text(encoding="utf-8")
        for route in ("/providers", "/presets", "/diagnostics", "/runtime-trace", "/integration/install", "/integration/uninstall"):
            self.assertIn(route, routes)
        self.assertIn("use_app_home", routes)
        self.assertIn("default_credential_store", routes)
        self.assertIn('AppRoute("DELETE", "/diagnostics"', routes)
        self.assertIn('AppRoute("DELETE", "/runtime-trace"', routes)


def test_provider_ui_render_regression_is_guarded() -> None:
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    assert "const customEndpoint=!!(" in ui
    assert "preset.id&&preset.id!==\'custom\'" in ui
    assert "class KiroByoErrorBoundary extends Component" in ui
    assert "function KiroByoAppBody()" in ui
    assert "h(KiroByoErrorBoundary,null,h(KiroByoAppBody))" in ui


def test_integration_ui_surfaces_source_audit() -> None:
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    assert "Source audit" in ui
    assert "i.verified_through" in ui


def test_ui_surfaces_runtime_inspector_without_replacing_metadata_telemetry() -> None:
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    assert "Runtime Inspector" in ui
    assert "/runtime-trace?limit=100" in ui
    assert "metadata 不保存正文" in ui
    telemetry = (ROOT / "src" / "kiro_byo" / "telemetry.py").read_text(encoding="utf-8")
    assert "Prompt text, assistant text, tool arguments/results" in telemetry


if __name__ == "__main__":
    unittest.main()
