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
    _runtime_matches_app,
    uninstall_from_app,
)


ROOT = Path(__file__).resolve().parents[1]

def assert_i18n_surface(text: str, ui: str) -> None:
    """The visible wording lives in locale assets, not hardcoded JSX."""
    messages = json.loads((ROOT / "src/kiro_byo/locales/zh-CN.json").read_text(encoding="utf-8"))
    matched = [key for key, value in messages.items() if text in value]
    assert matched, f"No zh-CN translation contains {text!r}"
    assert any(f'tr("{key}"' in ui or f"tr('{key}'" in ui for key in matched), (
        f"The translation for {text!r} is not used in the UI"
    )



class AppPackageTests(unittest.TestCase):
    def test_manifest_is_installable_kirocrew_app(self) -> None:
        manifest = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "kiro-byo")
        self.assertEqual(manifest["displayName"], "Kiro BYO")
        self.assertEqual(manifest["version"], (ROOT / "VERSION").read_text(encoding="utf-8").strip())
        self.assertEqual(manifest["minKiroCrewVersion"], "0.12.0")
        self.assertEqual(manifest["backend"]["hooks"]["routes"], "backend.routes:register_routes")
        self.assertEqual(manifest["ui"]["pages"][0]["route"], "/kiro-byo")
        self.assertEqual(manifest["platform"]["os"], ["macos", "linux", "windows"])


    def test_store_card_has_explicit_opaque_icon(self) -> None:
        import struct
        manifest = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["iconPath"], "assets/icon.png")
        icon = ROOT / manifest["iconPath"]
        self.assertTrue(icon.is_file())
        raw = icon.read_bytes()
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
        width, height, depth, color = struct.unpack(">IIBB", raw[16:26])
        self.assertEqual((width, height, depth, color), (512, 512, 8, 2))
        self.assertEqual(manifest["ui"]["pages"][0]["iconUrl"], "icon.svg")
        self.assertTrue((ROOT / "ui/icon.svg").is_file())
        # Supplied SSSP-BYO identity must be used by the app card,
        # the Crew sidebar and BYO's own heading.
        self.assertTrue((ROOT / "assets/logo_bw_text.png").is_file())
        svg = (ROOT / "ui/icon.svg").read_text(encoding="utf-8")
        self.assertIn('viewBox="0 0 746 324"', svg)
        self.assertIn('data:image/png;base64,', svg)
        self.assertIn('prefers-color-scheme:dark', svg)
        ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
        self.assertIn("h('img',{src:UI_ICON_URL,alt:'SSSP-BYO'})", ui)
        self.assertIn("new URL('./icon.svg',import.meta.url).href", ui)
        self.assertNotIn("h('div',{className:'ka-logo'},'K')", ui)


    def test_release_tree_contains_no_runtime_or_secret_state(self) -> None:
        # Tests and local runs must never leak mutable runtime state into a
        # distributable source/app bundle. Templates/examples are allowed; live
        # session/provider state is not.
        forbidden_roots = (
            ROOT / "data" / "sessions",
            ROOT / "data" / "telemetry",
            ROOT / "data" / "runtime_trace",
            ROOT / "data" / "agent_runtime",
            ROOT / "data" / "runs",
            ROOT / "data" / "agent_workspaces",
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
            ROOT / "config" / "acp_agents.json",
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
        self.assertEqual(manifest["version"], expected)
        self.assertEqual(pyproject["project"]["version"], expected)
        self.assertEqual(__version__, expected)
        self.assertEqual(AGENT_VERSION, expected)


    def test_app_ui_is_provider_first_and_capability_dynamic(self) -> None:
        ui = (ROOT / "ui" / "index.mjs").read_text(encoding="utf-8")
        # Provider/model capability flow remains dynamic.
        assert_i18n_surface('测试连接', ui)
        assert_i18n_surface('选择供应商…', ui)
        self.assertIn("ka-provider-left", ui)
        self.assertIn("thinkingText", ui)
        self.assertIn("effortText", ui)
        assert_i18n_surface('支持深度思考', ui)
        assert_i18n_surface('思考可开关', ui)
        assert_i18n_surface('思考力度', ui)
        self.assertNotIn("?token=", ui)

        # The compact desktop shell now includes a dedicated BYO Tasks surface
        # for per-cron model routing while keeping schedule ownership in Crew.
        # Whole-page scrolling is forbidden; dense lists/tables scroll locally.
        self.assertIn("status.overview", ui)
        self.assertIn("ka-side-status", ui)
        self.assertIn("ka-page-head", ui)
        assert_i18n_surface('返回系统总览', ui)
        self.assertIn("['modes',tr(\"ui.ed0eea8f20ba\")]".replace('\"','"'), ui)
        self.assertIn("['tasks',tr(\"ui.3172b317f9fc\")]".replace('\"','"'), ui)
        self.assertIn("function TasksPage(", ui)
        assert_i18n_surface('任务执行', ui)
        self.assertIn("/tasks/${encodeURIComponent(t.id)}/model", ui)
        self.assertIn("/tasks/background-model", ui)
        assert_i18n_surface('后台系统任务', ui)
        assert_i18n_surface('运行性能趋势', ui)
        self.assertNotIn("title:'当前执行'", ui)
        self.assertNotIn("Generic ACP v1", ui)
        self.assertNotIn("JetBrains ACP", ui)
        self.assertIn("/tasks/${encodeURIComponent(t.id)}/execution", ui)
        self.assertIn("{value:'',label:tr('linkage.inherit_model')}", ui)
        assert_i18n_surface('每次新会话 · 最小上下文（推荐）', ui)
        self.assertIn("ka-task-card", ui)
        self.assertNotIn("自动（由 ${modePackName(router)} · ${modePolicyName(router)} 路由）", ui)
        self.assertIn(".ka-main{", ui)
        self.assertIn("overflow:hidden", ui)
        self.assertIn(".ka-content{flex:1 1 auto;min-height:0;overflow:hidden}", ui)
        self.assertIn(".ka-scroll{height:100%;min-height:0;overflow:auto", ui)

        # Mode packs remain a dynamic policy surface. Type selection belongs to
        # BYO while KiroCrew's shared model controls contain real models only.
        # The shell must not hard-code Tiga because future packs reuse it.
        self.assertIn("modePackName(mode)", ui)
        self.assertNotIn("Tiga 根据形态、任务特征和模型状态决定执行方式", ui)
        assert_i18n_surface('KiroCrew 只选择真实模型；选择“自动”时由当前 BYO Type 路由。', ui)
        self.assertNotIn("changeType", ui)
        self.assertIn("onClick:()=>setViewingType(pol.id)", ui)
        assert_i18n_surface('点击卡片仅查看详情', ui)
        self.assertIn("v:modePackName(mode),sub:'Mode'", ui)
        self.assertIn("h('div',{className:'ka-side-config-mode'},modeDetails.name)", ui)
        assert_i18n_surface('最近 60 小时模型使用情况', ui)
        assert_i18n_surface('模型使用占比', ui)
        assert_i18n_surface('最近一次任务特征', ui)
        self.assertIn("modelRangeText", ui)
        assert_i18n_surface('这里只展示当前产品实际支持和管理的宿主集成。', ui)
        self.assertNotIn("未物理测试", ui)
        assert_i18n_surface('实际选择来源', ui)
        self.assertNotIn("典型执行结构", ui)
        self.assertNotIn("形态说明 / 实时决策", ui)
        self.assertNotIn("changePolicy", ui)
        self.assertIn("function AccountsPage()", ui)
        self.assertIn("label:p.short_name||p.display_name||p.id", ui)

        # Runtime capabilities remain available through Records / Settings;
        # Tasks only manages schedule routing, not agent/workspace control.
        self.assertNotIn("查看执行、智能体协作、工作区与恢复状态", ui)
        self.assertNotIn("查看 Diff", ui)
        self.assertNotIn("采用方案", ui)
        self.assertNotIn("外部智能体", ui)
        self.assertIn("KiroCrew", ui)
        assert_i18n_surface('ACP 兼容性', ui)
        self.assertNotIn("/workspaces/", ui)
        self.assertNotIn("/acp-agents", ui)
        self.assertIn("/acp-conformance", ui)
        self.assertIn("['records',tr(\"ui.9058a1c2c59e\")]".replace('\"','"'), ui)
        self.assertIn("function RecordsPage(", ui)
        self.assertNotIn("function DiagnosticsPage(", ui)

        # Language settings stay independent from model thinking/effort.
        assert_i18n_surface('推理呈现语言', ui)
        assert_i18n_surface('生成时指示', ui)
        self.assertIn("settings?.options", ui)
        self.assertNotIn("AutoRouter", ui)


    def test_overview_uses_compact_one_screen_layout(self) -> None:
        ui = (ROOT / "ui" / "index.mjs").read_text(encoding="utf-8")
        # The 0.2.0 overview is intentionally compact rather than a vertically
        # stacked card feed. Core shell regions do not whole-page scroll.
        self.assertIn("function OverviewPage", ui)
        self.assertIn(".ka-main{", ui)
        self.assertIn("overflow:hidden", ui)
        self.assertIn(".ka-page{height:100%;min-height:0", ui)
        self.assertIn("ka-overview-main", ui)
        self.assertNotIn("ka-overview-stack", ui)


    def test_app_ui_uses_host_theme_tokens_and_unified_records_clear(self) -> None:
        ui = (ROOT / "ui" / "index.mjs").read_text(encoding="utf-8")
        self.assertIn("useTheme", ui)
        self.assertIn("--ka-surface:var(--card,", ui)
        self.assertIn("--ka-input:var(--input,", ui)
        self.assertIn("--ka-surface-2:var(--panel,", ui)
        self.assertIn("--ka-hover:var(--bg-hover,", ui)
        self.assertIn("getAttribute('data-mode')", ui)
        self.assertIn("data-ka-scheme", ui)
        self.assertIn("function KSelect", ui)
        self.assertNotIn("h('select'", ui)
        self.assertNotIn(".ka-select option{", ui)
        assert_i18n_surface('清除可安全删除的记录', ui)
        self.assertIn("api('/records',{method:'DELETE'})", ui)
        self.assertIn("method:'DELETE'", ui)
        self.assertIn("runtime_current", ui)
        assert_i18n_surface('兼容 Profile', ui)
        self.assertIn("Agent Harness", ui)
        self.assertIn("/harness/activate", ui)
        assert_i18n_surface('Harness 未启用', ui)
        assert_i18n_surface('三个独立状态', ui)
        self.assertIn("class KiroByoErrorBoundary extends Component", ui)
        # Fixed shell + local scroll is a release-level UI invariant.
        self.assertIn("height:100%;min-height:0;overflow:hidden", ui)
        self.assertIn("scrollbar-gutter:stable", ui)


    def test_backend_exposes_provider_and_integration_routes(self) -> None:
        routes = (ROOT / "backend" / "routes.py").read_text(encoding="utf-8")
        for route in ("/providers", "/presets", "/tasks", "/tasks/background-model", "/tasks/{job_id}/model", "/tasks/{job_id}/execution", "/diagnostics", "/runtime-trace", "/acp-conformance", "/harness/activate", "/integration/install", "/integration/uninstall"):
            self.assertIn(route, routes)
        for removed_route in ("/workspaces/{workspace_id}/diff", "/workspaces/{workspace_id}/apply", "/acp-agents"):
            self.assertNotIn(removed_route, routes)
        self.assertIn("use_app_home", routes)
        self.assertIn("default_credential_store", routes)
        self.assertIn('AppRoute("DELETE", "/diagnostics"', routes)
        self.assertIn('AppRoute("DELETE", "/runtime-trace"', routes)
        self.assertIn('agent["acp_backend"] = "kiro-byo"', routes)
        self.assertIn('update_config_locked', routes)


def test_mode_and_records_ui_do_not_invent_runtime_facts() -> None:
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    assert "metaMap" not in ui
    assert "主智能体" not in ui
    assert "调查员" not in ui
    assert "审查员" not in ui
    assert "function RecordsPage(" in ui
    assert_i18n_surface("原始事件数据", ui)
    # Unknown route signals are rendered as unknown rather than filled with
    # attractive fixed percentages.
    for stale in ("breadth??.45", "risk??.35", "write_intensity??.25", "verification_value??.8"):
        assert stale not in ui


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
    assert "i.source_contract_status" in ui
    assert "i.source_contract_checked_version" in ui
    assert "i.version_audited" in ui


def test_records_ui_keeps_raw_runtime_trace_behind_details_without_replacing_metadata_telemetry() -> None:
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    assert "function RecordsPage(" in ui
    assert "/runtime-trace?limit=100" in ui
    assert_i18n_surface("原始事件数据", ui)
    assert "Runtime Inspector" not in ui
    telemetry = (ROOT / "src" / "kiro_byo" / "telemetry.py").read_text(encoding="utf-8")
    assert "Prompt text, assistant text, tool arguments/results" in telemetry


if __name__ == "__main__":
    unittest.main()
