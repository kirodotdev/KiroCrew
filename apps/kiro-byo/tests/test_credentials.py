from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_byo.config import load_config
from kiro_byo.credential_store import (
    MemoryCredentialStore,
    decode_provider_secrets,
)
from kiro_byo.discovery import DiscoveredModel, DiscoveryError
from kiro_byo.paths import crew_data_home
from kiro_byo.provider_admin import configure_provider, migrate_plaintext_credentials


class CredentialMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "kiro-byo"
        (self.home / "config").mkdir(parents=True)
        self.old_home = os.environ.get("KIRO_BYO_HOME")
        self.old_crew = os.environ.get("KIRO_CREW_HOME")
        self.old_crew_legacy = os.environ.get("KIROCREW_HOME")
        os.environ["KIRO_BYO_HOME"] = str(self.home)
        os.environ.pop("KIRO_CREW_HOME", None)
        os.environ.pop("KIROCREW_HOME", None)
        self.store = MemoryCredentialStore()

    def tearDown(self) -> None:
        if self.old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = self.old_home
        if self.old_crew is None:
            os.environ.pop("KIRO_CREW_HOME", None)
        else:
            os.environ["KIRO_CREW_HOME"] = self.old_crew
        if self.old_crew_legacy is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self.old_crew_legacy
        self.tmp.cleanup()

    def _write_config(self, payload: dict, name: str = "providers.json") -> Path:
        path = self.home / "config" / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_plaintext_key_and_secret_headers_migrate_out_of_config(self) -> None:
        payload = {
            "default_model": "demo/model-a",
            "providers": [
                {
                    "id": "demo",
                    "name": "Demo",
                    "type": "openai-compatible",
                    "base_url": "https://example.test/v1",
                    "api_key": "super-secret",
                    "headers": {
                        "Authorization": "Bearer alternate-secret",
                        "X-Trace": "public-value",
                    },
                    "models": [{"id": "model-a", "name": "Model A"}],
                    "default_model": "model-a",
                }
            ],
        }
        path = self._write_config(payload)
        backup = self._write_config(payload, "providers.json.bak")

        report = migrate_plaintext_credentials(store=self.store, path=path)
        self.assertTrue(report.changed)
        self.assertEqual(report.migrated_providers, ("demo",))
        self.assertEqual(report.migrated_secret_headers, 1)
        self.assertIn(str(backup), report.sanitized_backup_files)

        raw = json.loads(path.read_text(encoding="utf-8"))
        provider = raw["providers"][0]
        self.assertNotIn("api_key", provider)
        self.assertEqual(provider["credential_ref"], "provider:demo")
        self.assertEqual(provider["headers"], {"X-Trace": "public-value"})

        backup_raw = json.loads(backup.read_text(encoding="utf-8"))
        backup_provider = backup_raw["providers"][0]
        self.assertNotIn("api_key", backup_provider)
        self.assertNotIn("Authorization", backup_provider.get("headers", {}))

        secrets = decode_provider_secrets(self.store.get("provider:demo"))
        self.assertEqual(secrets.api_key, "super-secret")
        self.assertEqual(secrets.headers["Authorization"], "Bearer alternate-secret")

        loaded = load_config(self.store)
        spec = loaded.providers[0]
        self.assertEqual(spec.api_key, "super-secret")
        self.assertEqual(spec.headers["Authorization"], "Bearer alternate-secret")
        self.assertEqual(spec.headers["X-Trace"], "public-value")

    def test_configure_provider_writes_only_credential_ref(self) -> None:
        self._write_config({"default_model": "", "providers": []})
        discovered = (
            "demo",
            "Demo Provider",
            [DiscoveredModel("m1", "Model One"), DiscoveredModel("m2", "Model Two")],
        )
        with patch("kiro_byo.provider_admin.discover_models", return_value=discovered):
            result = configure_provider(
                base_url="https://example.test/v1",
                api_key="secret-key",
                store=self.store,
            )
        self.assertTrue(result["credential"]["configured"])
        raw_text = (self.home / "config" / "providers.json").read_text(encoding="utf-8")
        self.assertNotIn("secret-key", raw_text)
        raw = json.loads(raw_text)
        self.assertNotIn("api_key", raw["providers"][0])
        self.assertEqual(raw["providers"][0]["credential_ref"], "provider:demo")
        self.assertEqual(decode_provider_secrets(self.store.get("provider:demo")).api_key, "secret-key")


    def test_env_credential_ref_does_not_require_native_store(self) -> None:
        os.environ["KIRO_TEST_PROVIDER_KEY"] = "env-secret"
        try:
            self._write_config({
                "credential_schema_version": 1,
                "default_model": "demo/m1",
                "providers": [{
                    "id": "demo", "type": "openai-compatible",
                    "base_url": "https://example.test/v1",
                    "credential_ref": "env:KIRO_TEST_PROVIDER_KEY",
                    "models": [{"id": "m1"}],
                    "default_model": "m1",
                }],
            })
            loaded = load_config()
            self.assertEqual(loaded.providers[0].api_key, "env-secret")
        finally:
            os.environ.pop("KIRO_TEST_PROVIDER_KEY", None)

    def test_pre_migration_config_still_loads_for_upgrade_safety(self) -> None:
        self._write_config({
            "default_model": "demo/m1",
            "providers": [{
                "id": "demo", "type": "openai-compatible",
                "base_url": "https://example.test/v1",
                "api_key": "legacy-key",
                "models": [{"id": "m1"}],
                "default_model": "m1",
            }],
        })
        loaded = load_config(self.store)
        self.assertEqual(loaded.providers[0].api_key, "legacy-key")

    def test_runtime_rejects_new_plaintext_secret_config(self) -> None:
        self._write_config({
            "credential_schema_version": 1,
            "default_model": "demo/m1",
            "providers": [{
                "id": "demo", "type": "openai-compatible",
                "base_url": "https://example.test/v1",
                "api_key": "do-not-accept",
                "models": [{"id": "m1"}],
                "default_model": "m1",
            }],
        })
        from kiro_byo.config import ConfigError
        with self.assertRaises(ConfigError):
            load_config(self.store)

    def test_provider_discovery_error_redacts_api_key(self) -> None:
        self._write_config({"credential_schema_version": 1, "default_model": "", "providers": []})
        with patch(
            "kiro_byo.provider_admin.discover_models",
            side_effect=DiscoveryError("upstream echoed secret-key in response"),
        ):
            with self.assertRaises(DiscoveryError) as cm:
                configure_provider(
                    base_url="https://example.test/v1",
                    api_key="secret-key",
                    store=self.store,
                )
        self.assertNotIn("secret-key", str(cm.exception))
        self.assertIn("[REDACTED]", str(cm.exception))

    def test_crew_data_home_prefers_explicit_env_then_recorded_state(self) -> None:
        data = self.home / "data"
        data.mkdir()
        (data / "integration.json").write_text(
            json.dumps({"kiro_crew_home": "X:/custom/crew-data"}), encoding="utf-8"
        )
        self.assertEqual(str(crew_data_home()), "X:/custom/crew-data")
        os.environ["KIRO_CREW_HOME"] = "Y:/portable/crew"
        self.assertEqual(str(crew_data_home()), "Y:/portable/crew")


if __name__ == "__main__":
    unittest.main()
