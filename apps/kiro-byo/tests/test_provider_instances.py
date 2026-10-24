from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_byo import provider_admin as provider_admin_mod
from kiro_byo.config import load_config
from kiro_byo.credential_store import MemoryCredentialStore, decode_provider_secrets
from kiro_byo.discovery import DiscoveredModel
from kiro_byo.paths import use_app_home
from kiro_byo.provider_admin import (
    clear_provider_configuration, configure_provider, delete_provider, list_providers,
    refresh_provider_models, set_default_model, set_mode_selection,
)


class ProviderInstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = MemoryCredentialStore()
        self.discovery = (
            "openai",
            "OpenAI",
            [DiscoveredModel("gpt-test", "GPT Test", input_modalities=("text", "image"))],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_same_vendor_can_have_multiple_independent_instances(self) -> None:
        with use_app_home(self.home), patch(
            "kiro_byo.provider_admin._discover_for_preset", return_value=self.discovery
        ):
            first = configure_provider(
                base_url="https://api.openai.com/v1",
                api_key="key-one",
                preset_id="openai",
                create_new=True,
                store=self.store,
            )
            second = configure_provider(
                base_url="https://api.openai.com/v1",
                api_key="key-two",
                preset_id="openai",
                create_new=True,
                store=self.store,
            )
            providers = list_providers(store=self.store)
            cfg = load_config(self.store)

        self.assertEqual(first["id"], "openai")
        self.assertEqual(second["id"], "openai-2")
        self.assertEqual(first["name"], "OpenAI")
        self.assertEqual(second["name"], "OpenAI #2")
        self.assertEqual([p["instance_id"] for p in providers], ["openai", "openai-2"])
        self.assertEqual([p["preset_id"] for p in providers], ["openai", "openai"])
        self.assertEqual(decode_provider_secrets(self.store.get("provider:openai")).api_key, "key-one")
        self.assertEqual(decode_provider_secrets(self.store.get("provider:openai-2")).api_key, "key-two")
        self.assertEqual(len(cfg.providers), 2)
        public = [public_id for public_id, _model in cfg.public_models]
        self.assertEqual(len(public), 2)
        self.assertEqual(len(set(public)), 2)

    def test_legacy_setup_updates_same_family_and_endpoint_instead_of_duplicating(self) -> None:
        with use_app_home(self.home), patch(
            "kiro_byo.provider_admin._discover_for_preset", return_value=self.discovery
        ):
            first = configure_provider(
                base_url="https://api.openai.com/v1",
                api_key="old-key",
                preset_id="openai",
                store=self.store,
            )
            second = configure_provider(
                base_url="https://api.openai.com/v1",
                api_key="new-key",
                preset_id="openai",
                store=self.store,
            )
            providers = list_providers(store=self.store)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(providers), 1)
        self.assertEqual(decode_provider_secrets(self.store.get("provider:openai")).api_key, "new-key")

    def test_deleting_one_instance_does_not_delete_another_instances_credential(self) -> None:
        with use_app_home(self.home), patch(
            "kiro_byo.provider_admin._discover_for_preset", return_value=self.discovery
        ):
            configure_provider(
                base_url="https://api.openai.com/v1",
                api_key="key-one",
                preset_id="openai",
                create_new=True,
                store=self.store,
            )
            configure_provider(
                base_url="https://api.openai.com/v1",
                api_key="key-two",
                preset_id="openai",
                create_new=True,
                store=self.store,
            )
            delete_provider("openai", store=self.store)
            providers = list_providers(store=self.store)

        self.assertIsNone(self.store.get("provider:openai"))
        self.assertIsNotNone(self.store.get("provider:openai-2"))
        self.assertEqual([p["id"] for p in providers], ["openai-2"])



    def test_refresh_repairs_stale_provider_and_root_defaults(self) -> None:
        with use_app_home(self.home), patch(
            "kiro_byo.provider_admin._discover_for_preset", return_value=self.discovery
        ):
            configure_provider(
                base_url="https://api.openai.com/v1", api_key="key-one",
                preset_id="openai", store=self.store,
            )
            set_default_model("openai", "gpt-test", store=self.store)

        replacement = ("openai", "OpenAI", [DiscoveredModel("gpt-next", "GPT Next")])
        with use_app_home(self.home), patch(
            "kiro_byo.provider_admin._discover_for_preset", return_value=replacement
        ):
            refresh_provider_models("openai", store=self.store)
            cfg = load_config(self.store)

        raw = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["providers"][0]["default_model"], "gpt-next")
        self.assertEqual(raw["default_model"], "openai/gpt-next")
        self.assertIsNotNone(cfg.resolve_model(cfg.default_model))

    def test_delete_repairs_root_default_without_creating_invalid_provider_slash(self) -> None:
        with use_app_home(self.home), patch(
            "kiro_byo.provider_admin._discover_for_preset", return_value=self.discovery
        ):
            configure_provider(base_url="https://api.openai.com/v1", api_key="one", preset_id="openai", create_new=True, store=self.store)
            configure_provider(base_url="https://api.openai.com/v1", api_key="two", preset_id="openai", create_new=True, store=self.store)
            set_default_model("openai", "gpt-test", store=self.store)
            delete_provider("openai", store=self.store)
            cfg = load_config(self.store)

        raw = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["default_model"], "openai-2/gpt-test")
        self.assertIsNotNone(cfg.resolve_model(cfg.default_model))

    def test_threaded_management_mutations_do_not_overwrite_each_other(self) -> None:
        with use_app_home(self.home), patch(
            "kiro_byo.provider_admin._discover_for_preset", return_value=self.discovery
        ):
            configure_provider(
                base_url="https://api.openai.com/v1", api_key="one",
                preset_id="openai", store=self.store,
            )

            entered = threading.Event()
            release = threading.Event()
            real_write = provider_admin_mod._write_raw

            def blocking_write(raw, path=None):
                if threading.current_thread().name == "mode-writer":
                    entered.set()
                    assert release.wait(2)
                return real_write(raw, path)

            errors: list[BaseException] = []

            def mode_writer():
                try:
                    set_mode_selection("sky", "tiga")
                except BaseException as exc:  # pragma: no cover - assertion reports below
                    errors.append(exc)

            def default_writer():
                try:
                    set_default_model("openai", "gpt-test", store=self.store)
                except BaseException as exc:  # pragma: no cover
                    errors.append(exc)

            config_file = self.home / "config" / "providers.json"
            with patch("kiro_byo.provider_admin.config_path", return_value=config_file), \
                 patch("kiro_byo.provider_admin._write_raw", side_effect=blocking_write):
                first = threading.Thread(target=mode_writer, name="mode-writer")
                second = threading.Thread(target=default_writer, name="default-writer")
                first.start()
                assert entered.wait(1)
                second.start()
                # Without provider_admin's mutation lock, the second write can
                # commit here and then be overwritten by the stale first snapshot.
                time.sleep(0.05)
                release.set()
                first.join(2); second.join(2)

            self.assertFalse(errors)
            raw = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["mode"], {"id": "tiga", "type": "sky"})
            self.assertEqual(raw["default_model"], "openai/gpt-test")

    def test_full_provider_purge_removes_config_and_all_owned_instance_credentials(self) -> None:
        with use_app_home(self.home), patch(
            "kiro_byo.provider_admin._discover_for_preset", return_value=self.discovery
        ):
            configure_provider(base_url="https://api.openai.com/v1", api_key="one", preset_id="openai", create_new=True, store=self.store)
            configure_provider(base_url="https://api.openai.com/v1", api_key="two", preset_id="openai", create_new=True, store=self.store)
            result = clear_provider_configuration(store=self.store)
        self.assertEqual(result["providers"], 2)
        self.assertEqual(result["credentials"], 2)
        self.assertIsNone(self.store.get("provider:openai"))
        self.assertIsNone(self.store.get("provider:openai-2"))
        self.assertFalse((self.home / "config" / "providers.json").exists())

    def test_provider_instance_schema_is_persisted_without_rewriting_old_ids(self) -> None:
        with use_app_home(self.home), patch(
            "kiro_byo.provider_admin._discover_for_preset", return_value=self.discovery
        ):
            configure_provider(
                base_url="https://api.openai.com/v1",
                api_key="key-one",
                preset_id="openai",
                create_new=True,
                store=self.store,
            )
        raw = json.loads((self.home / "config" / "providers.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["provider_instance_schema_version"], 1)
        self.assertEqual(raw["providers"][0]["id"], "openai")


if __name__ == "__main__":
    unittest.main()
