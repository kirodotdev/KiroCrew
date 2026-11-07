from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kiro_byo.config import ModelSpec, ProviderSpec
from kiro_byo.credential_store import MemoryCredentialStore
from kiro_byo.discovery import DiscoveredModel, DiscoveryError, discover_models
from kiro_byo.provider_admin import configure_provider
from kiro_byo.provider_catalog import (
    connection_identity,
    merge_official_model_profile,
    model_capability_from_row,
    product_base_urls,
    public_platforms,
)
from kiro_byo.providers import registry


class DynamicModelCatalogTests(unittest.TestCase):
    def test_model_studio_rich_catalogue_and_pagination_are_normalized(self) -> None:
        first = {
            "output": {
                "total": 2,
                "page_no": 1,
                "page_size": 1,
                "models": [{
                    "model": "qwen-future-chat",
                    "display_name": "Qwen Future Chat",
                    "features": ["function-calling"],
                    "capabilities": ["TG", "Reasoning"],
                    "inference_metadata": {
                        "request_modality": ["Text", "Image"],
                        "response_modality": ["Text"],
                    },
                    "model_info": {"context_window": 262144, "max_output_tokens": 32768},
                    "endpoint_id": "ep-qwen-future",
                    "status": "preview",
                }],
            }
        }
        second = {
            "output": {
                "total": 2,
                "page_no": 2,
                "page_size": 1,
                "models": [{
                    "model": "wan-image-next",
                    "capabilities": ["IG"],
                    "inference_metadata": {
                        "request_modality": ["Text"],
                        "response_modality": ["Image"],
                    },
                }],
            }
        }
        with patch("kiro_byo.discovery._fetch_json", side_effect=[first, second]) as fetch:
            provider_id, _name, models = discover_models(
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "secret",
                endpoint="https://dashscope.aliyuncs.com/api/v1/models",
            )
        self.assertEqual(provider_id, "qwen")
        self.assertEqual(len(models), 1)
        model = models[0]
        self.assertEqual(model.id, "qwen-future-chat")
        self.assertEqual(model.name, "Qwen Future Chat")
        self.assertEqual(model.context_window, 262144)
        self.assertEqual(model.max_output_tokens, 32768)
        self.assertEqual(model.input_modalities, ("text", "image"))
        self.assertEqual(model.output_modalities, ("text",))
        self.assertTrue(model.tool_use)
        self.assertTrue(model.reasoning_supported)
        self.assertEqual(model.invocation_target, "ep-qwen-future")
        self.assertEqual(model.invocation_target_kind, "endpoint")
        self.assertEqual(model.lifecycle, "preview")
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("page_no=2", fetch.call_args_list[1].args[0])

    def test_catalogue_lifecycle_drops_shutdown_and_marks_retiring(self) -> None:
        payload = {
            "data": [
                {"id": "chat-old", "status": "shutdown"},
                {"id": "chat-soon", "inference_offline_info": {"offline_time": "2026-12-01T00:00:00Z"}},
            ]
        }
        with patch("kiro_byo.discovery._fetch_json", return_value=payload):
            _pid, _name, models = discover_models("https://api.example.invalid/v1", "")
        self.assertEqual([model.id for model in models], ["chat-soon"])
        self.assertEqual(models[0].lifecycle, "retiring")

    def test_invocation_target_is_distinct_from_catalog_identity(self) -> None:
        provider = ProviderSpec(
            id="managed",
            name="Managed",
            type="openai-compatible",
            base_url="https://example.invalid/v1",
            api_key="",
            models=(),
        )
        model = ModelSpec(
            "managed", "Managed", "claude-sonnet", "Claude Sonnet",
            invocation_target="us.anthropic.claude-sonnet-inference-profile",
            invocation_target_kind="inference-profile",
        )
        adapter = MagicMock()
        adapter.completion_turn.return_value = MagicMock()
        with patch("kiro_byo.providers.registry.adapter_for", return_value=adapter):
            registry.chat_completion_turn(
                provider, model.model_id, [{"role": "user", "content": "hi"}], lambda _text: None,
                model_spec=model,
            )
        self.assertEqual(model.model_id, "claude-sonnet")
        self.assertEqual(model.wire_model_id, "us.anthropic.claude-sonnet-inference-profile")
        self.assertEqual(adapter.completion_turn.call_args.args[1], model.wire_model_id)

    def test_product_overlay_adds_plan_without_new_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config").mkdir(parents=True)
            (home / "config" / "provider_products.override.json").write_text(json.dumps({
                "schema_version": 1,
                "platforms": [{
                    "id": "xiaomi",
                    "display_name": "小米 MiMo / Xiaomi",
                    "category": "china",
                    "products": [{
                        "id": "coding-plan",
                        "legacy_preset_id": "xiaomi",
                        "display_name": "Coding Plan",
                        "adapter_transport": "xiaomi-mimo-chat",
                        "default_profile_id": "jp",
                        "profiles": [{
                            "id": "jp",
                            "display_name": "Coding Plan · Japan",
                            "base_url": "https://coding-plan-jp.example.invalid/v1",
                            "entitlement": "subscription",
                            "region": "jp",
                            "catalog_policy": "live",
                        }],
                    }],
                }],
            }), encoding="utf-8")
            old = os.environ.get("KIRO_BYO_HOME")
            os.environ["KIRO_BYO_HOME"] = str(home)
            try:
                xiaomi = next(row for row in public_platforms() if row["id"] == "xiaomi")
                product = next(row for row in xiaomi["products"] if row["id"] == "coding-plan")
                self.assertEqual(product["adapter_transport"], "xiaomi-mimo-chat")
                identity = connection_identity("xiaomi", "https://coding-plan-jp.example.invalid/v1")
                self.assertEqual((identity.platform_id, identity.product_id, identity.profile_id), ("xiaomi", "coding-plan", "jp"))
                self.assertEqual(identity.entitlement, "subscription")
                self.assertIn("https://coding-plan-jp.example.invalid/v1", product_base_urls("xiaomi"))
                # Registry remains protocol-oriented: product metadata did not
                # create a "coding plan adapter".
                self.assertNotIn("xiaomi-coding-plan", registry._ADAPTERS)
                self.assertIn("xiaomi-mimo-chat", registry._ADAPTERS)
            finally:
                if old is None:
                    os.environ.pop("KIRO_BYO_HOME", None)
                else:
                    os.environ["KIRO_BYO_HOME"] = old

    def test_live_catalogue_facts_beat_stale_curated_defaults(self) -> None:
        row = merge_official_model_profile("xiaomi", {
            "id": "mimo-v2.6-pro",
            "name": "mimo-v2.6-pro",
            "context_window": 777777,
            "tool_use": False,
            "reasoning": {"mode": "toggle", "supported": True, "source": "provider-discovery"},
            "capability_source": "provider-discovery",
        })
        cap = model_capability_from_row(row)
        self.assertEqual(cap["context_window"], 777777)
        self.assertFalse(cap["tool_use"])
        self.assertEqual(cap["reasoning"]["source"], "provider-discovery")

    def test_product_overlay_cannot_claim_a_new_wire_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config").mkdir(parents=True)
            (home / "config" / "provider_products.override.json").write_text(json.dumps({
                "schema_version": 1,
                "platforms": [{
                    "id": "xiaomi",
                    "products": [{
                        "id": "fake-protocol-plan",
                        "legacy_preset_id": "xiaomi",
                        "display_name": "Fake Protocol Plan",
                        "adapter_transport": "anthropic-messages",
                        "profiles": [{
                            "id": "default",
                            "base_url": "https://fake-plan.example.invalid/v1",
                        }],
                    }],
                }],
            }), encoding="utf-8")
            old = os.environ.get("KIRO_BYO_HOME")
            os.environ["KIRO_BYO_HOME"] = str(home)
            try:
                xiaomi = next(row for row in public_platforms() if row["id"] == "xiaomi")
                self.assertNotIn("fake-protocol-plan", {row["id"] for row in xiaomi["products"]})
                self.assertNotIn("https://fake-plan.example.invalid/v1", product_base_urls("xiaomi"))
            finally:
                if old is None:
                    os.environ.pop("KIRO_BYO_HOME", None)
                else:
                    os.environ["KIRO_BYO_HOME"] = old

    def test_new_platform_overlay_must_reuse_code_owned_transport(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config").mkdir(parents=True)
            (home / "config" / "provider_products.override.json").write_text(json.dumps({
                "schema_version": 1,
                "platforms": [{
                    "id": "evil-platform",
                    "products": [{
                        "id": "fake",
                        "legacy_preset_id": "openai",
                        "adapter_transport": "anthropic-messages",
                        "profiles": [{"id": "default", "base_url": "https://evil.example.invalid/v1"}],
                    }],
                }],
            }), encoding="utf-8")
            old = os.environ.get("KIRO_BYO_HOME")
            os.environ["KIRO_BYO_HOME"] = str(home)
            try:
                self.assertNotIn("evil-platform", {row["id"] for row in public_platforms()})
                self.assertNotIn("https://evil.example.invalid/v1", product_base_urls("openai"))
            finally:
                if old is None:
                    os.environ.pop("KIRO_BYO_HOME", None)
                else:
                    os.environ["KIRO_BYO_HOME"] = old

    def test_malformed_override_schema_is_ignored_fail_safe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config").mkdir(parents=True)
            (home / "config" / "provider_products.override.json").write_text(
                json.dumps({"schema_version": "not-a-number", "platforms": []}), encoding="utf-8"
            )
            (home / "config" / "model_catalog.override.json").write_text(
                json.dumps({"schema_version": "broken", "providers": {"xiaomi": {}}}), encoding="utf-8"
            )
            old = os.environ.get("KIRO_BYO_HOME")
            os.environ["KIRO_BYO_HOME"] = str(home)
            try:
                self.assertTrue(any(row["id"] == "xiaomi" for row in public_platforms()))
                row = merge_official_model_profile("xiaomi", {"id": "mimo-v2.6-pro"})
                self.assertEqual(row["context_window"], 1_000_000)
            finally:
                if old is None:
                    os.environ.pop("KIRO_BYO_HOME", None)
                else:
                    os.environ["KIRO_BYO_HOME"] = old

    def test_local_model_override_can_describe_future_model_without_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config").mkdir(parents=True)
            (home / "config" / "model_catalog.override.json").write_text(json.dumps({
                "schema_version": 1,
                "providers": {
                    "xiaomi": {
                        "mimo-v2.7-pro": {
                            "context_window": 1500000,
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                            "tool_use": True,
                            "reasoning": {
                                "mode": "toggle",
                                "supported": True,
                                "can_disable": True,
                                "default_enabled": True,
                            },
                        }
                    }
                },
            }), encoding="utf-8")
            old = os.environ.get("KIRO_BYO_HOME")
            os.environ["KIRO_BYO_HOME"] = str(home)
            try:
                row = merge_official_model_profile("xiaomi", {
                    "id": "mimo-v2.7-pro",
                    "name": "mimo-v2.7-pro",
                    "catalog_source": "provider-discovery",
                })
                cap = model_capability_from_row(row)
                self.assertEqual(cap["context_window"], 1500000)
                self.assertTrue(cap["tool_use"])
                self.assertEqual(cap["input_modalities"], ["text", "image"])
                self.assertEqual(cap["reasoning"]["mode"], "toggle")
                self.assertEqual(cap["reasoning"]["source"], "local-override")
                self.assertEqual(row["capability_source"], "local-override")
            finally:
                if old is None:
                    os.environ.pop("KIRO_BYO_HOME", None)
                else:
                    os.environ["KIRO_BYO_HOME"] = old

    def test_saved_catalogue_is_used_as_cache_when_live_discovery_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config").mkdir(parents=True)
            (home / "config" / "providers.json").write_text(
                json.dumps({"credential_schema_version": 1, "default_model": "", "providers": []}),
                encoding="utf-8",
            )
            old = os.environ.get("KIRO_BYO_HOME")
            os.environ["KIRO_BYO_HOME"] = str(home)
            try:
                store = MemoryCredentialStore()
                live = ("xiaomi", "Xiaomi", [DiscoveredModel("mimo-v2.7-pro", "mimo-v2.7-pro")])
                with patch("kiro_byo.provider_admin.discover_models", return_value=live):
                    first = configure_provider(
                        base_url="https://api.xiaomimimo.com/v1",
                        api_key="secret", preset_id="xiaomi", store=store,
                    )
                self.assertEqual(first["models"][0]["catalog_source"], "provider-discovery")
                with patch("kiro_byo.provider_admin.discover_models", side_effect=DiscoveryError("temporary outage")):
                    second = configure_provider(
                        base_url="https://api.xiaomimimo.com/v1",
                        api_key="secret", preset_id="xiaomi", store=store,
                    )
                self.assertEqual([row["id"] for row in second["models"]], ["mimo-v2.7-pro"])
                self.assertEqual(second["models"][0]["catalog_source"], "cache")
            finally:
                if old is None:
                    os.environ.pop("KIRO_BYO_HOME", None)
                else:
                    os.environ["KIRO_BYO_HOME"] = old


if __name__ == "__main__":
    unittest.main()
