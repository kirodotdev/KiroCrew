"""Provider account-status queries exposed to Kiro BYO management UIs.

This module is deliberately conservative: it only calls documented provider
account endpoints that can be authenticated with the same API credential Kiro
API already stores.  Unsupported providers are reported as unsupported instead
of scraping web consoles or inventing a balance from local token telemetry.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from .credential_store import CredentialStore, CredentialStoreError, decode_provider_secrets
from .provider_catalog import provider_preset


_DEEPSEEK_HOSTS = {"api.deepseek.com"}


def _base_result(provider: dict[str, Any]) -> dict[str, Any]:
    credential = provider.get("credential") if isinstance(provider.get("credential"), dict) else {}
    return {
        "provider_id": str(provider.get("id") or ""),
        "provider_name": str(provider.get("name") or provider.get("id") or ""),
        "preset_id": str(provider.get("preset_id") or "custom"),
        "base_url": str(provider.get("base_url") or ""),
        "credential_configured": bool(credential.get("configured")),
        "query_capability": "unsupported",
        "features": [],
        "state": "unsupported",
        "available": None,
        "balances": [],
        "updated_at": 0.0,
        "source": "provider",
        "note": "当前适配未发现可由此 API Key 安全读取的官方账户余额接口。",
    }


def _deepseek_balance(provider: dict[str, Any], store: CredentialStore, *, timeout_seconds: float) -> dict[str, Any]:
    result = _base_result(provider)
    result["query_capability"] = "balance"
    result["features"] = ["balance"]
    result["note"] = ""
    credential = provider.get("credential") if isinstance(provider.get("credential"), dict) else {}
    ref = str(credential.get("ref") or "").strip()
    if not ref:
        result["state"] = "credential_missing"
        result["note"] = "尚未配置 API Key。"
        return result
    try:
        secrets = decode_provider_secrets(store.get(ref))
    except CredentialStoreError as exc:
        result["state"] = "error"
        result["note"] = f"读取系统凭据失败：{exc}"
        return result
    if not secrets.api_key:
        result["state"] = "credential_missing"
        result["note"] = "尚未配置 API Key。"
        return result

    base_url = str(provider.get("base_url") or "").strip().rstrip("/")
    host = (urlparse(base_url).hostname or "").lower()
    if host not in _DEEPSEEK_HOSTS:
        result["state"] = "unsupported"
        result["query_capability"] = "unsupported"
        result["features"] = []
        result["note"] = "此 DeepSeek preset 使用了自定义 API 地址，因此不会向官方账户接口发送凭据。"
        return result

    endpoint = "https://api.deepseek.com/user/balance"
    request = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {secrets.api_key}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            if raw:
                detail = raw[:300]
        except Exception:
            pass
        result["state"] = "error"
        result["note"] = f"账户查询失败：HTTP {exc.code}" + (f" · {detail}" if detail else "")
        result["updated_at"] = time.time()
        return result
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result["state"] = "error"
        result["note"] = f"账户查询失败：{exc}"
        result["updated_at"] = time.time()
        return result

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        result["state"] = "error"
        result["note"] = "账户接口返回了无效 JSON。"
        result["updated_at"] = time.time()
        return result

    if not isinstance(data, dict) or not isinstance(data.get("balance_infos"), list):
        result["state"] = "error"
        result["note"] = "账户接口返回了无法识别的数据结构。"
        result["updated_at"] = time.time()
        return result

    balances: list[dict[str, str]] = []
    for row in data.get("balance_infos", []):
        if not isinstance(row, dict):
            continue
        currency = str(row.get("currency") or "").strip()
        total = str(row.get("total_balance") or "").strip()
        if not currency or not total:
            continue
        balances.append({
            "currency": currency,
            "total": total,
            "granted": str(row.get("granted_balance") or "").strip(),
            "topped_up": str(row.get("topped_up_balance") or "").strip(),
        })

    result.update({
        "state": "ok",
        "available": bool(data.get("is_available")),
        "balances": balances,
        "updated_at": time.time(),
        "note": "官方账户接口",
    })
    return result


def account_status_for_provider(
    provider: dict[str, Any],
    store: CredentialStore,
    *,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """Return only account facts Kiro BYO can obtain from a documented endpoint.

    A provider/model feature being absent is not an error. Unsupported account
    capabilities stay absent rather than being synthesized from local telemetry.
    """
    preset_id = str(provider.get("preset_id") or "custom").strip().lower()
    preset = provider_preset(preset_id)
    capabilities = set(preset.account_capabilities if preset is not None else ())
    if "balance" in capabilities and preset_id == "deepseek":
        return _deepseek_balance(provider, store, timeout_seconds=timeout_seconds)
    result = _base_result(provider)
    if not result["credential_configured"] and bool((provider.get("credential") or {}).get("required")):
        result["state"] = "credential_missing"
        result["note"] = "尚未配置 API Key。"
    return result


def account_statuses(
    providers: list[dict[str, Any]],
    store: CredentialStore,
    *,
    timeout_seconds: float = 8.0,
) -> list[dict[str, Any]]:
    """Return rows only for providers with a usable account capability.

    The account page is capability-driven: an OpenAI-compatible provider with no
    safely queryable billing endpoint simply does not grow an artificial
    "unavailable balance" card.
    """
    out: list[dict[str, Any]] = []
    for provider in providers:
        row = account_status_for_provider(provider, store, timeout_seconds=timeout_seconds)
        if row.get("query_capability") == "unsupported":
            continue
        out.append(row)
    return out
