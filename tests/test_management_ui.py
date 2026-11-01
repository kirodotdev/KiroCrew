from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from kiro_byo.credential_store import MemoryCredentialStore, ProviderSecrets, encode_provider_secrets
from kiro_byo.web_ui import _ManagementServer


class ManagementUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "config").mkdir()
        (self.home / "data").mkdir()
        (self.home / "config" / "providers.json").write_text(
            json.dumps(
                {
                    "default_model": "demo/m1",
                    "providers": [
                        {
                            "id": "demo",
                            "name": "Demo",
                            "type": "openai-compatible",
                            "base_url": "https://example.test/v1",
                            "credential_ref": "provider:demo",
                            "models": [{"id": "m1", "name": "Model One"}],
                            "default_model": "m1",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = str(self.home)
        self.store = MemoryCredentialStore()
        self.store.set("provider:demo", encode_provider_secrets(ProviderSecrets(api_key="never-return-this")))
        self.server = _ManagementServer(("127.0.0.1", 0), self.store, "test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = self.old_home
        self.tmp.cleanup()

    def test_app_shell_defers_heavy_trace_and_integration_audit(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "ui" / "index.mjs").read_text(encoding="utf-8")
        # 0.2.0 loads the bounded diagnostics projection at startup because the
        # overview uses bounded request/run state. Heavy Runtime Trace and the
        # KiroCrew integration audit remain lazy.
        self.assertIn("Promise.allSettled([loadStatus(),loadPresets(),loadOps()])", source)
        self.assertIn("if(tab==='records')Promise.allSettled([loadOps(),loadTrace(),loadRecords()])", source)
        self.assertIn("if(section==='crew')onAuditReload", source)
        initial = source.split("useEffect(()=>{Promise.allSettled([loadStatus(),loadPresets(),loadOps()])", 1)[1].split("},[loadStatus,loadPresets,loadOps,loadRecords,tab])", 1)[0]
        self.assertNotIn("loadTrace()", initial)
        self.assertNotIn("loadRecords()", initial.split("const id=setInterval", 1)[0])
        self.assertIn("if(tab==='records')loadRecords().catch", initial)
        self.assertNotIn("loadAudit()", initial)

    def test_api_requires_management_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/api/status", timeout=2)
        self.assertEqual(cm.exception.code, 403)


    def test_status_is_explicitly_kirocrew_scoped(self) -> None:
        req = urllib.request.Request(
            self.base + "/api/status",
            headers={"X-Kiro-BYO-Token": "test-token"},
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["target_client"], "KiroCrew")
        self.assertEqual(payload["product"], "Kiro BYO")
        self.assertEqual(payload["runtime_capabilities"]["source"], "runtime-introspection")
        self.assertEqual(payload["backend_card"]["source"], "KiroCrew")
        self.assertEqual(
            payload["native_tools"],
            [
                "read", "glob", "grep", "read_files", "list_directory",
                "write", "edit", "delete_file", "shell", "execute_shell_process",
                "control_shell_process", "code_symbols", "web_fetch", "web_search", "update_plan",
            ],
        )
        self.assertNotIn("capability", payload)
        self.assertNotIn("capability_matrix", payload)
        self.assertEqual(payload["jev_owner"], "KiroCrew")

    def test_reasoning_language_preference_round_trips_without_touching_provider(self) -> None:
        status_req = urllib.request.Request(
            self.base + "/api/status",
            headers={"X-Kiro-BYO-Token": "test-token"},
        )
        with urllib.request.urlopen(status_req, timeout=2) as response:
            status = json.loads(response.read().decode("utf-8"))
        self.assertEqual(status["reasoning_language"]["reasoning_language"], "auto")
        self.assertIn(
            {"value": "zh-TW", "label": "繁體中文"},
            status["reasoning_language"]["options"],
        )

        put_req = urllib.request.Request(
            self.base + "/api/preferences/reasoning-language",
            data=json.dumps({"value": "ja"}).encode("utf-8"),
            headers={
                "X-Kiro-BYO-Token": "test-token",
                "Content-Type": "application/json",
            },
            method="PUT",
        )
        with urllib.request.urlopen(put_req, timeout=2) as response:
            saved = json.loads(response.read().decode("utf-8"))
        self.assertEqual(saved["reasoning_language"], "ja")

        raw = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["preferences"]["reasoning_language"], "ja")
        self.assertEqual(raw["providers"][0]["id"], "demo")
        self.assertNotIn("api_key", raw["providers"][0])

    def test_diagnostics_endpoint_is_metadata_only(self) -> None:
        req = urllib.request.Request(
            self.base + "/api/diagnostics?limit=5",
            headers={"X-Kiro-BYO-Token": "test-token"},
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["client"], "kiro-crew")
        self.assertFalse(payload["content_recording"])
        self.assertIn("summary", payload)
        self.assertIn("turns", payload)

    def test_status_never_returns_secret(self) -> None:
        req = urllib.request.Request(
            self.base + "/api/status",
            headers={"X-Kiro-BYO-Token": "test-token"},
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            body = response.read().decode("utf-8")
        self.assertNotIn("never-return-this", body)
        payload = json.loads(body)
        self.assertTrue(payload["providers"][0]["credential"]["configured"])
        self.assertNotIn("api_key", payload["providers"][0])


if __name__ == "__main__":
    unittest.main()

class ManagementVisualConsistencyTests(unittest.TestCase):
    """Keep sidebar and failure labels consistent with the BYO management UI."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.app = (root / "ui" / "index.mjs").read_text(encoding="utf-8")
        cls.legacy = (root / "src" / "kiro_byo" / "web_ui.py").read_text(encoding="utf-8")

    def test_navigation_uses_semantic_svg_and_compact_active_marker(self) -> None:
        self.assertIn("function NavIcon({name})", self.app)
        self.assertIn("h(NavIcon,{name:id})", self.app)
        self.assertIn(".ka-navbtn.on:before", self.app)
        self.assertIn("height:20px;width:3px", self.app)
        self.assertNotIn("left:-11px;top:7px;bottom:7px", self.app)
        self.assertIn(".ka-navbtn:focus-visible", self.app)

    def test_sidebar_describes_mode_pack_not_type_and_avoids_nav_overlap(self) -> None:
        self.assertIn("function sidebarModeDetails(mode)", self.app)
        self.assertIn("catalogPack(mode.catalog||{},mode.pack)", self.app)
        self.assertIn("description:localizedModeDescription(mode.pack,pack?.description", self.app)
        self.assertIn("name:pack?.short_name||modePackName(mode)", self.app)
        self.assertIn("className:'ka-side-navwrap'", self.app)
        self.assertIn(".ka-side-navwrap{flex:1 1 auto;min-height:0;overflow-y:auto", self.app)
        self.assertIn(".ka-side-navwrap .ka-nav{min-height:max-content}", self.app)
        for label in ("当前模式", "运行配置", "集成就绪", "查看模式设置"):
            from test_app_package import assert_i18n_surface
            assert_i18n_surface(label, self.app)
        card = self.app.split("h('div',{className:'ka-side-config'},", 1)[1].split("h('main',{className:'ka-main'},", 1)[0]
        for forbidden in ("policy_label", "modePolicyName", "Multi Type", "新会话默认", "执行方式", "并行能力", "质量检查", "modeDetails.workers", "modeDetails.reviewer", "modeDetails.parallel"):
            self.assertNotIn(forbidden, card)

    def test_tiga_public_pack_description_is_mode_level(self) -> None:
        import json
        payload=json.loads((Path(__file__).resolve().parents[1] / 'src' / 'kiro_byo' / 'policy_packs' / 'tiga.json').read_text('utf-8'))
        self.assertIn('Tiga', payload['description'])
        self.assertNotIn('Type', payload['description'])
        self.assertNotIn('形态', payload['description'])

    def test_result_labels_are_neutral_and_numbers_use_status_colors(self) -> None:
        # Labels inherit the ordinary caption color; only the numerical value
        # receives semantic status coloring, including when value is zero.
        # Records center counts are neutral categories, not outcomes; those
        # remain color-coded in the appropriate status-specific components.
        self.assertIn("v:recordSummary.turns==null?'—':recordNumber(recordSummary.turns)", self.app)
        self.assertIn("v:recordSummary.protected_runs==null?'—':recordNumber(recordSummary.protected_runs)", self.app)
        self.assertIn("v:String(data.summary?.errors||0),tone:'ka-err'", self.app)
        self.assertNotIn('labelTone', self.app)
        self.assertIn('h(\'span\',null,tr("ui.3e3c8068bb0e"))', self.app)
        self.assertIn("h('span',null,h('b',{className:'ka-err'},failed)", self.app)
        self.assertIn(".ka-quick b.ka-err{color:var(--ka-danger)}", self.app)
        self.assertIn('<div class="m-card fail"><span>Failed</span>', self.legacy)
        self.assertIn('.m-card.fail b,.m-card .fail-text', self.legacy)
        self.assertNotIn('.m-card.fail span,.m-card.fail b', self.legacy)
        self.assertIn('.badge-fail{background:var(--fail-bg)', self.legacy)

    def test_overview_reads_hourly_from_full_diagnostics(self) -> None:
        self.assertIn('Array.isArray(status.diagnostics?.hourly)?status.diagnostics:', self.app)
        self.assertIn('Array.isArray(ops?.summary?.hourly)?ops.summary:', self.app)
        self.assertIn('h(LatencyTrend,{hourly})', self.app)
        self.assertIn('bucket.avg_ms', self.app)
        self.assertIn('if(!valid.length)return', self.app)

    def test_task_model_and_context_controls_shrink_with_card(self) -> None:
        self.assertIn('.ka-task-card{container-type:inline-size}', self.app)
        self.assertIn('grid-template-columns:minmax(0,.8fr) minmax(0,1.1fr) minmax(0,1.1fr)', self.app)
        self.assertIn('.ka-task-field .ka-select-wrap{width:100%;min-width:0;max-width:100%}', self.app)
        self.assertIn('@container (max-width:620px){.ka-task-meta{grid-template-columns:repeat(2,minmax(0,1fr))', self.app)
        self.assertIn('.ka-task-meta>.ka-task-field:nth-child(3){grid-column:1/-1}', self.app)
