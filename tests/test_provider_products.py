from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_byo.config import load_config
from kiro_byo.credential_store import MemoryCredentialStore
from kiro_byo.paths import use_app_home
from kiro_byo.provider_admin import configure_provider, list_provider_platforms
from kiro_byo.provider_catalog import connection_identity, credential_connection_profile


class ProviderProductModelTests(unittest.TestCase):
    def test_xiaomi_api_and_token_plan_share_one_adapter(self) -> None:
        platforms = {row["id"]: row for row in list_provider_platforms()}
        xiaomi = platforms["xiaomi"]
        products = {row["id"]: row for row in xiaomi["products"]}
        self.assertEqual(set(products), {"api", "token-plan"})
        self.assertEqual(products["api"]["adapter_transport"], "xiaomi-mimo-chat")
        self.assertEqual(products["token-plan"]["adapter_transport"], "xiaomi-mimo-chat")
        profiles = {row["id"]: row for row in products["token-plan"]["profiles"]}
        self.assertEqual(set(profiles), {"cn", "sgp", "ams"})
        self.assertTrue(all(row["entitlement"] == "subscription" for row in profiles.values()))

    def test_exact_endpoint_resolves_product_independently_of_legacy_preset(self) -> None:
        identity = connection_identity("xiaomi", "https://token-plan-sgp.xiaomimimo.com/v1")
        self.assertEqual(identity.platform_id, "xiaomi")
        self.assertEqual(identity.product_id, "token-plan")
        self.assertEqual(identity.profile_id, "sgp")
        self.assertEqual(identity.entitlement, "subscription")

    def test_minimax_region_is_a_profile_not_an_adapter(self) -> None:
        identity = connection_identity("minimax", "https://api.minimax.cn/anthropic/v1")
        self.assertEqual(identity.platform_id, "minimax")
        self.assertEqual(identity.product_id, "api")
        self.assertEqual(identity.profile_id, "cn")


    def test_minimax_payg_and_token_plan_share_one_adapter(self) -> None:
        platforms = {row["id"]: row for row in list_provider_platforms()}
        minimax = platforms["minimax"]
        products = {row["id"]: row for row in minimax["products"]}
        self.assertEqual(set(products), {"api", "token-plan"})
        self.assertEqual(products["api"]["adapter_transport"], "minimax-messages")
        self.assertEqual(products["token-plan"]["adapter_transport"], "minimax-messages")
        self.assertEqual(products["token-plan"]["account_capabilities"], ["quota"])
        self.assertIn("sk-cp", products["token-plan"]["credential_prefixes"])
        hinted = credential_connection_profile("minimax", "sk-cp-example")
        self.assertIsNotNone(hinted)
        assert hinted is not None
        self.assertEqual(hinted[1].id, "token-plan")
        self.assertEqual(hinted[2].entitlement, "subscription")


    def test_configure_minimax_subscription_key_persists_token_plan_identity(self) -> None:
        from kiro_byo.discovery import DiscoveredModel
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            store = MemoryCredentialStore()
            discovered = ("minimax", "MiniMax", [DiscoveredModel("MiniMax-M3", "MiniMax M3")])
            with use_app_home(home), patch("kiro_byo.provider_admin._discover_for_preset", return_value=discovered):
                result = configure_provider(
                    base_url="https://api.minimax.io/anthropic/v1",
                    api_key="sk-cp-example", preset_id="minimax", store=store,
                )
                raw = json.loads((home / "config" / "providers.json").read_text(encoding="utf-8"))
                cfg = load_config(store)
        provider = raw["providers"][0]
        self.assertEqual((provider["product_id"], provider["profile_id"], provider["entitlement"]),
                         ("token-plan", "global", "subscription"))
        self.assertEqual(result["product_id"], "token-plan")
        self.assertEqual(cfg.providers[0].product_id, "token-plan")
        self.assertEqual(cfg.providers[0].entitlement, "subscription")

    def test_minimax_saved_token_plan_identity_survives_shared_inference_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            store = MemoryCredentialStore()
            config_dir = home / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "providers.json").write_text(json.dumps({
                "credential_schema_version": 1,
                "providers": [{
                    "id": "minimax", "name": "MiniMax Token Plan", "type": "minimax-messages",
                    "preset_id": "minimax", "base_url": "https://api.minimax.io/anthropic/v1",
                    "platform_id": "minimax", "product_id": "token-plan", "profile_id": "global",
                    "entitlement": "subscription",
                    "models": [{"id": "MiniMax-M3", "name": "MiniMax M3"}],
                }],
            }), encoding="utf-8")
            with use_app_home(home):
                cfg = load_config(store)
        provider = cfg.providers[0]
        self.assertEqual(provider.product_id, "token-plan")
        self.assertEqual(provider.entitlement, "subscription")

    def test_custom_preset_is_not_reclassified_by_matching_an_official_url(self) -> None:
        identity = connection_identity("custom", "https://api.openai.com/v1")
        self.assertEqual(identity.platform_id, "custom")
        self.assertEqual(identity.product_id, "compatible-endpoint")
        self.assertEqual(identity.entitlement, "relay")

    def test_configure_xiaomi_token_plan_persists_commercial_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            store = MemoryCredentialStore()
            with use_app_home(home):
                result = configure_provider(
                    base_url="https://api.xiaomimimo.com/v1",
                    api_key="tp-example",
                    preset_id="xiaomi",
                    store=store,
                )
                raw = json.loads((home / "config" / "providers.json").read_text(encoding="utf-8"))
                cfg = load_config(store)
        provider = raw["providers"][0]
        self.assertEqual(provider["base_url"], "https://token-plan-cn.xiaomimimo.com/v1")
        self.assertEqual(provider["type"], "xiaomi-mimo-chat")
        self.assertEqual(provider["platform_id"], "xiaomi")
        self.assertEqual(provider["product_id"], "token-plan")
        self.assertEqual(provider["profile_id"], "cn")
        self.assertEqual(provider["entitlement"], "subscription")
        self.assertEqual(raw["provider_product_schema_version"], 1)
        self.assertEqual(result["product_id"], "token-plan")
        self.assertEqual(cfg.providers[0].product_id, "token-plan")
        self.assertEqual(cfg.providers[0].type, "xiaomi-mimo-chat")


    def test_known_official_endpoint_reconciles_stale_saved_product_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            store = MemoryCredentialStore()
            config_dir = home / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "providers.json").write_text(json.dumps({
                "credential_schema_version": 1,
                "providers": [{
                    "id": "xiaomi", "name": "Xiaomi", "type": "xiaomi-mimo-chat",
                    "preset_id": "xiaomi",
                    "base_url": "https://token-plan-sgp.xiaomimimo.com/v1",
                    "platform_id": "xiaomi", "product_id": "api", "profile_id": "payg", "entitlement": "usage",
                    "models": [{"id": "mimo-v2.6-pro", "name": "MiMo"}],
                }],
            }), encoding="utf-8")
            with use_app_home(home):
                cfg = load_config(store)
        provider = cfg.providers[0]
        self.assertEqual((provider.platform_id, provider.product_id, provider.profile_id, provider.entitlement),
                         ("xiaomi", "token-plan", "sgp", "subscription"))

    def test_future_plan_credential_prefix_can_route_via_product_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            store = MemoryCredentialStore()
            config_dir = home / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "provider_products.override.json").write_text(json.dumps({
                "schema_version": 1,
                "platforms": [{
                    "id": "xiaomi",
                    "products": [{
                        "id": "coding-plan",
                        "legacy_preset_id": "xiaomi",
                        "display_name": "Coding Plan",
                        "adapter_transport": "xiaomi-mimo-chat",
                        "credential_prefixes": ["cp-"],
                        "default_profile_id": "jp",
                        "profiles": [{
                            "id": "jp", "display_name": "Japan",
                            "base_url": "https://coding-plan-jp.example.invalid/v1",
                            "entitlement": "subscription", "region": "jp",
                        }],
                    }],
                }],
            }), encoding="utf-8")
            discovered = ("xiaomi", "Xiaomi", [])
            from kiro_byo.discovery import DiscoveredModel
            discovered = ("xiaomi", "Xiaomi", [DiscoveredModel("future-chat", "Future Chat")])
            with use_app_home(home), patch("kiro_byo.provider_admin._discover_for_preset", return_value=discovered):
                result = configure_provider(
                    base_url="https://api.xiaomimimo.com/v1",
                    api_key="cp-example", preset_id="xiaomi", store=store,
                )
            raw = json.loads((config_dir / "providers.json").read_text(encoding="utf-8"))
        provider = raw["providers"][0]
        self.assertEqual(provider["base_url"], "https://coding-plan-jp.example.invalid/v1")
        self.assertEqual((provider["product_id"], provider["profile_id"], provider["entitlement"]),
                         ("coding-plan", "jp", "subscription"))
        self.assertEqual(provider["type"], "xiaomi-mimo-chat")
        self.assertEqual(result["product_id"], "coding-plan")

    def test_old_instance_without_product_fields_is_inferred_at_load(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            store = MemoryCredentialStore()
            config_dir = home / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "providers.json").write_text(json.dumps({
                "credential_schema_version": 1,
                "providers": [{
                    "id": "minimax",
                    "name": "MiniMax",
                    "type": "minimax-messages",
                    "preset_id": "minimax",
                    "base_url": "https://api.minimax.cn/anthropic/v1",
                    "models": [{"id": "m", "name": "m"}],
                }],
            }), encoding="utf-8")
            with use_app_home(home):
                cfg = load_config(store)
        provider = cfg.providers[0]
        self.assertEqual(provider.platform_id, "minimax")
        self.assertEqual(provider.product_id, "api")
        self.assertEqual(provider.profile_id, "cn")
        self.assertEqual(provider.type, "minimax-messages")


if __name__ == "__main__":
    unittest.main()
