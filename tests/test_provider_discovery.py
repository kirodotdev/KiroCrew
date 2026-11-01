from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from kiro_byo.discovery import discover_models


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return self._payload
        return self._payload[:size]


class ProviderDiscoveryTests(unittest.TestCase):
    def test_anthropic_catalog_can_use_x_api_key_auth(self) -> None:
        payload = {
            "data": [
                {
                    "id": "claude-sonnet-5",
                    "display_name": "Claude Sonnet 5",
                    "capabilities": {"tool_use": {"supported": True}},
                }
            ]
        }
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            captured["timeout"] = timeout
            return _Response(payload)

        with patch("kiro_byo.discovery.urllib.request.urlopen", side_effect=fake_urlopen):
            provider_id, provider_name, models = discover_models(
                "https://api.anthropic.com/v1",
                "secret",
                auth_style="x-api-key",
                extra_headers={"anthropic-version": "2023-06-01"},
            )

        self.assertEqual(captured["url"], "https://api.anthropic.com/v1/models")
        self.assertEqual(captured["headers"]["x-api-key"], "secret")
        self.assertEqual(captured["headers"]["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", captured["headers"])
        self.assertEqual(provider_id, "anthropic")
        self.assertEqual(provider_name, "Anthropic / Claude")
        self.assertEqual(models[0].name, "Claude Sonnet 5")
        self.assertTrue(models[0].tool_use)

    def test_cohere_models_envelope_uses_name_as_model_id_and_filters_non_chat(self) -> None:
        payload = {
            "models": [
                {
                    "name": "command-a-plus-05-2026",
                    "endpoints": ["chat"],
                    "context_length": 128000,
                },
                {
                    "name": "embed-v5.0-pro",
                    "endpoints": ["embed"],
                    "context_length": 4096,
                },
            ]
        }
        with patch("kiro_byo.discovery.urllib.request.urlopen", return_value=_Response(payload)):
            provider_id, _, models = discover_models(
                "https://api.cohere.ai/compatibility/v1",
                "secret",
                endpoint="https://api.cohere.com/v1/models",
            )

        self.assertEqual(provider_id, "cohere")
        self.assertEqual([model.id for model in models], ["command-a-plus-05-2026"])
        self.assertEqual(models[0].context_window, 128000)

    def test_xai_capabilities_publish_exact_reasoning_vocabulary(self) -> None:
        payload = {
            "models": [{
                "id": "grok-4.7",
                "context_length": 500000,
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
                "capabilities": {
                    "reasoning_effort": ["low", "medium", "high", "xhigh"],
                    "default_reasoning_effort": "high",
                },
            }]
        }
        with patch("kiro_byo.discovery.urllib.request.urlopen", return_value=_Response(payload)):
            provider_id, _, models = discover_models(
                "https://api.x.ai/v1", "secret",
                endpoint="https://api.x.ai/v1/language-models",
            )
        self.assertEqual(provider_id, "grok")
        self.assertEqual(models[0].effort_supported_levels, ("low", "medium", "high", "xhigh"))
        self.assertEqual(models[0].effort_default_level, "high")
        self.assertFalse(bool(models[0].reasoning_can_disable))
        self.assertEqual(models[0].input_modalities, ("text", "image"))

    def test_openai_shape_accepts_qianfan_metadata_aliases(self) -> None:
        payload = {
            "data": [
                {
                    "id": "ernie-example",
                    "type": "chat",
                    "context_length": 163840,
                    "max_completions_tokens": 65536,
                    "architecture": {
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                    },
                },
                {"id": "embedding-example", "type": "embeddings"},
            ]
        }
        with patch("kiro_byo.discovery.urllib.request.urlopen", return_value=_Response(payload)):
            provider_id, _, models = discover_models("https://qianfan.baidubce.com/v2", "secret")

        self.assertEqual(provider_id, "qianfan")
        self.assertEqual([model.id for model in models], ["ernie-example"])
        self.assertEqual(models[0].context_window, 163840)
        self.assertEqual(models[0].max_output_tokens, 65536)
        self.assertEqual(models[0].input_modalities, ("text", "image"))


if __name__ == "__main__":
    unittest.main()
