from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_byo.config import load_config
from kiro_byo.credential_store import MemoryCredentialStore, decode_provider_secrets
from kiro_byo.discovery import DiscoveredModel
from kiro_byo.paths import use_app_home
from kiro_byo.provider_admin import clear_provider_configuration, configure_provider, delete_provider, list_providers


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
