"""Default Type + "<Mode> — <Type>" labels outside the picker list.

Regression for: under Kiro BYO the composer chip / status bar showed "auto" for
a session with no pinned model (and "kiro-byo-type:tiga:glitter" once a Type was
chosen), and the pin row read "将 auto 置顶到 default 代理".
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiro_byo import crew_dashboard_patch as dp  # noqa: E402
from kiro_byo import crew_patch_common as common  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self.payload = payload
        self.body = json.dumps(payload).encode()


class ResolvedModelWrapperTest(unittest.TestCase):
    def _run(self, *, backend: str, native_model: str, mode_type: str = "multi", models=("m",)):
        native_payload = {"model": native_model, "agent": "", "pinned": False}

        async def native(_request):
            return _Resp(native_payload)

        fake_records = types.ModuleType("kiro_crew.dashboard.agent_admin.crew_records")
        fake_records.api_kirocrew_agent_resolved_model = native
        fake_manager = types.ModuleType("kiro_crew.apps.manager")
        fake_manager.app_data_dir = lambda _app: Path("/nonexistent/kiro-byo")
        fake_config = types.ModuleType("kiro_byo.config")
        fake_config.load_catalog_config = lambda _p: types.SimpleNamespace(
            mode_pack="tiga", mode_type=mode_type, models=list(models)
        )
        fake_paths = types.ModuleType("kiro_byo.paths")

        @contextmanager
        def use_app_home(_home):
            yield

        fake_paths.use_app_home = use_app_home
        stubs = {
            "kiro_crew.dashboard.agent_admin.crew_records": fake_records,
            "kiro_crew.apps.manager": fake_manager,
            "kiro_byo.config": fake_config,
            "kiro_byo.paths": fake_paths,
        }
        saved = {k: sys.modules.get(k) for k in stubs}
        sys.modules.update(stubs)
        try:
            ns = {
                "asyncio": asyncio,
                "json": json,
                "logger": logging.getLogger("test"),
                "web": types.SimpleNamespace(Request=object, Response=object, json_response=_Resp),
                "ACP_BACKEND_KIRO_BYO": "kiro-byo",
                "KiroCrewConfig": types.SimpleNamespace(
                    load=lambda: types.SimpleNamespace(agent=types.SimpleNamespace(acp_backend=backend))
                ),
                "_normalize_model_key": lambda s: s.strip().lower(),
            }
            exec(common._KIRO_BYO_RESOLVED_MODEL_HELPER, ns)
            return asyncio.run(ns["_kiro_byo_resolved_model"](None)).payload
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_byo_unpinned_reports_default_type(self):
        self.assertEqual(self._run(backend="kiro-byo", native_model="")["model"], "kiro-byo-type:tiga:multi")
        self.assertEqual(self._run(backend="kiro-byo", native_model="auto")["model"], "kiro-byo-type:tiga:multi")

    def test_official_backend_untouched(self):
        self.assertEqual(self._run(backend="", native_model="")["model"], "")

    def test_pinned_model_untouched(self):
        self.assertEqual(
            self._run(backend="kiro-byo", native_model="kiro-byo-type:tiga:glitter")["model"],
            "kiro-byo-type:tiga:glitter",
        )

    def test_no_executable_model_keeps_native(self):
        self.assertEqual(self._run(backend="kiro-byo", native_model="", models=())["model"], "")


class LabelScriptTest(unittest.TestCase):
    def test_label_map_names_mode_and_type(self):
        labels = dp.type_label_map()
        self.assertEqual(labels["kiro-byo-type:tiga:multi"]["full"], "Tiga — Multi")
        self.assertEqual(labels["kiro-byo-type:tiga:multi"]["type"], "Multi Type")
        self.assertEqual(labels["kiro-byo-type:tiga:glitter"]["full"], "Tiga — Glitter")


    def test_same_local_type_id_in_different_modes_never_collides(self):
        dyna = types.SimpleNamespace(
            id="dyna", short_name="Dyna", display_name="Dyna Pack",
            policies=(types.SimpleNamespace(id="power", display_name="Strong Type"),),
        )
        gaia = types.SimpleNamespace(
            id="gaia", short_name="Gaia", display_name="Gaia Pack",
            policies=(types.SimpleNamespace(id="power", display_name="Supreme Type"),),
        )
        with patch("kiro_byo.policy_packs.load_policy_packs", return_value=(dyna, gaia)):
            labels = dp.type_label_map()
        self.assertEqual(labels["kiro-byo-type:dyna:power"]["full"], "Dyna — Strong")
        self.assertEqual(labels["kiro-byo-type:dyna:power"]["type"], "Strong Type")
        self.assertEqual(labels["kiro-byo-type:gaia:power"]["full"], "Gaia — Supreme")
        self.assertEqual(labels["kiro-byo-type:gaia:power"]["type"], "Supreme Type")
        self.assertEqual(set(labels), {"kiro-byo-type:dyna:power", "kiro-byo-type:gaia:power"})

    def test_guard_has_chat_scoped_type_label_script_and_is_idempotent(self):
        shell = "<html><head></head><body></body></html>"
        upgraded, changed = dp._inject_guard(shell)
        self.assertTrue(changed)
        self.assertEqual(upgraded.count('id="kiro-byo-model-label"'), 1)
        self.assertEqual(upgraded.count(dp.GUARD_END), 1)
        self.assertEqual(dp._inject_guard(upgraded), (upgraded, False))
        self.assertIn('kiro-byo-chat-surface', upgraded)
        self.assertIn('[data-model-id^="kiro-byo-type:"]', upgraded)
        self.assertIn('Tiga — Multi', upgraded)
        self.assertIn('display: none !important', upgraded)


if __name__ == "__main__":
    unittest.main()
