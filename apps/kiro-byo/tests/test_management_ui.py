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

    def test_app_shell_defers_heavy_diagnostics_and_integration_audit(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "ui" / "index.mjs").read_text(encoding="utf-8")
        self.assertIn("Promise.allSettled([loadStatus(),loadPresets()])", source)
        self.assertIn("if(tab==='diagnostics')loadDiagnostics()", source)
        self.assertIn("if(tab==='integration')loadIntegration()", source)
        initial = source.split("useEffect(()=>{Promise.allSettled([loadStatus(),loadPresets()])", 1)[1].split("},[loadStatus,loadPresets])", 1)[0]
        self.assertNotIn("loadDiagnostics", initial)
        self.assertNotIn("loadIntegration", initial)

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
                "control_shell_process", "code_symbols", "web_fetch", "web_search",
            ],
        )
        self.assertNotIn("capability", payload)
        self.assertNotIn("capability_matrix", payload)
        self.assertEqual(payload["jev_owner"], "KiroCrew")

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
