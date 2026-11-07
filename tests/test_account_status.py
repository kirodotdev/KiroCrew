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


    def test_moonshot_balance_uses_same_model_key_and_platform_currency(self) -> None:
        self.store.set(
            "provider:moonshot",
            encode_provider_secrets(ProviderSecrets(api_key="moonshot-secret")),
        )
        provider = {
            "id": "moonshot", "name": "Moonshot / Kimi", "preset_id": "moonshot",
            "base_url": "https://api.moonshot.cn/v1",
            "credential": {"configured": True, "required": True, "ref": "provider:moonshot"},
        }
        payload = {
            "code": 0, "status": True, "data": {
                "available_balance": 49.58894, "voucher_balance": 46.58893, "cash_balance": 3.00001,
            },
        }
        with patch("kiro_byo.account_status.urllib.request.urlopen", return_value=_Response(payload)) as urlopen:
            row = account_status_for_provider(provider, self.store)
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["balances"][0]["currency"], "CNY")
        self.assertEqual(row["balances"][0]["total_label"], "可用余额")
        self.assertEqual(row["balances"][0]["components"][0]["label"], "现金余额")
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://api.moonshot.cn/v1/users/me/balance")
        self.assertNotIn("moonshot-secret", json.dumps(row, ensure_ascii=False))

    def test_moonshot_custom_relay_never_receives_account_query(self) -> None:
        self.store.set(
            "provider:moonshot",
            encode_provider_secrets(ProviderSecrets(api_key="moonshot-secret")),
        )
        provider = {
            "id": "moonshot", "name": "Moonshot / Kimi", "preset_id": "moonshot",
            "base_url": "https://relay.example/v1",
            "credential": {"configured": True, "required": True, "ref": "provider:moonshot"},
        }
        with patch("kiro_byo.account_status.urllib.request.urlopen") as urlopen:
            row = account_status_for_provider(provider, self.store)
        self.assertEqual(row["query_capability"], "unsupported")
        urlopen.assert_not_called()

    def test_minimax_token_plan_reports_provider_remaining_percentages(self) -> None:
        self.store.set(
            "provider:minimax",
            encode_provider_secrets(ProviderSecrets(api_key="sk-cp-example")),
        )
        provider = {
            "id": "minimax", "name": "MiniMax Token Plan", "preset_id": "minimax",
            "product_id": "token-plan", "profile_id": "global",
            "base_url": "https://api.minimax.io/anthropic/v1",
            "credential": {"configured": True, "required": True, "ref": "provider:minimax"},
        }
        payload = {
            "base_resp": {"status_code": 0},
            "model_remains": [{
                "model_name": "MiniMax-M3",
                "current_interval_status": 1,
                "current_interval_total_count": 1000,
                "current_interval_usage_count": 320,
                "current_interval_remaining_percent": 68,
                "current_weekly_status": 1,
                "current_weekly_total_count": 5000,
                "current_weekly_usage_count": 2950,
                "current_weekly_remaining_percent": 41,
                "remains_time": "2h14m",
            }],
        }
        with patch("kiro_byo.account_status.urllib.request.urlopen", return_value=_Response(payload)) as urlopen:
            row = account_status_for_provider(provider, self.store)
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["query_capability"], "quota")
        quota = row["quota_models"][0]
        self.assertEqual(quota["interval"]["remaining_percent"], 68.0)
        self.assertEqual(quota["weekly"]["remaining_percent"], 41.0)
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://www.minimax.io/v1/token_plan/remains")

    def test_minimax_payg_key_never_queries_token_plan_endpoint(self) -> None:
        self.store.set(
            "provider:minimax",
            encode_provider_secrets(ProviderSecrets(api_key="sk-payg-example")),
        )
        provider = {
            "id": "minimax", "name": "MiniMax", "preset_id": "minimax",
            "product_id": "api", "base_url": "https://api.minimax.io/anthropic/v1",
            "credential": {"configured": True, "required": True, "ref": "provider:minimax"},
        }
        with patch("kiro_byo.account_status.urllib.request.urlopen") as urlopen:
            row = account_status_for_provider(provider, self.store)
        self.assertEqual(row["query_capability"], "unsupported")
        urlopen.assert_not_called()

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
