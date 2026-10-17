from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from kiro_byo.account_status import account_status_for_provider, account_statuses
from kiro_byo.credential_store import MemoryCredentialStore, ProviderSecrets, encode_provider_secrets


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._body


class AccountStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryCredentialStore()
        self.store.set(
            "provider:deepseek",
            encode_provider_secrets(ProviderSecrets(api_key="secret-key")),
        )
        self.provider = {
            "id": "deepseek",
            "name": "DeepSeek",
            "preset_id": "deepseek",
            "base_url": "https://api.deepseek.com",
            "credential": {
                "configured": True,
                "required": True,
                "ref": "provider:deepseek",
            },
        }

    def test_deepseek_balance_uses_official_endpoint_without_exposing_key(self) -> None:
        payload = {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "CNY",
                    "total_balance": "110.00",
                    "granted_balance": "10.00",
                    "topped_up_balance": "100.00",
                }
            ],
        }
        with patch("kiro_byo.account_status.urllib.request.urlopen", return_value=_Response(payload)) as urlopen:
            row = account_status_for_provider(self.provider, self.store)
        self.assertEqual(row["state"], "ok")
        self.assertTrue(row["available"])
        self.assertEqual(row["balances"][0]["total"], "110.00")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/user/balance")
        self.assertIn("Bearer secret-key", request.headers.values())
        self.assertNotIn("secret-key", json.dumps(row, ensure_ascii=False))

    def test_custom_deepseek_base_never_sends_key_to_official_account_endpoint(self) -> None:
        provider = dict(self.provider)
        provider["base_url"] = "https://relay.example/v1"
        with patch("kiro_byo.account_status.urllib.request.urlopen") as urlopen:
            row = account_status_for_provider(provider, self.store)
        self.assertEqual(row["state"], "unsupported")
        self.assertEqual(row["query_capability"], "unsupported")
        urlopen.assert_not_called()


    def test_account_list_omits_providers_without_supported_account_features(self) -> None:
        openai = {
            "id": "openai",
            "name": "OpenAI",
            "preset_id": "openai",
            "base_url": "https://api.openai.com/v1",
            "credential": {"configured": True, "required": True, "ref": "provider:openai"},
        }
        payload = {"is_available": True, "balance_infos": []}
        with patch("kiro_byo.account_status.urllib.request.urlopen", return_value=_Response(payload)):
            rows = account_statuses([openai, self.provider], self.store)
        self.assertEqual([row["provider_id"] for row in rows], ["deepseek"])

    def test_unsupported_provider_stays_explicitly_unsupported(self) -> None:
        provider = {
            "id": "openai",
            "name": "OpenAI",
            "preset_id": "openai",
            "base_url": "https://api.openai.com/v1",
            "credential": {"configured": True, "required": True, "ref": "provider:openai"},
        }
        row = account_status_for_provider(provider, self.store)
        self.assertEqual(row["state"], "unsupported")
        self.assertEqual(row["balances"], [])


if __name__ == "__main__":
    unittest.main()
