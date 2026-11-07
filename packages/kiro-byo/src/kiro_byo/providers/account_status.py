"""Provider account facts exposed to the Kiro BYO management UI.

Account queries are deliberately narrower than model support.  Kiro BYO only
calls a documented provider-owned account endpoint when the *same credential*
already configured for inference is accepted by that endpoint.  It never
scrapes consoles and never asks for a higher-privilege admin credential just to
fill the Accounts page.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from ..credential_store import CredentialStore, CredentialStoreError, decode_provider_secrets
from ..provider_catalog import account_status_handler


_DEEPSEEK_HOSTS = {"api.deepseek.com"}
_MOONSHOT_ENDPOINTS = {
    "api.moonshot.cn": ("https://api.moonshot.cn/v1/users/me/balance", "CNY"),
    "api.moonshot.ai": ("https://api.moonshot.ai/v1/users/me/balance", "USD"),
}
_MINIMAX_QUOTA_ENDPOINTS = {
    "api.minimax.io": "https://www.minimax.io/v1/token_plan/remains",
    "api.minimax.cn": "https://www.minimaxi.com/v1/token_plan/remains",
    "api.minimaxi.com": "https://www.minimaxi.com/v1/token_plan/remains",
}


def _base_result(provider: dict[str, Any]) -> dict[str, Any]:
    credential = provider.get("credential") if isinstance(provider.get("credential"), dict) else {}
    return {
        "provider_id": str(provider.get("id") or ""),
        "provider_name": str(provider.get("name") or provider.get("id") or ""),
        "preset_id": str(provider.get("preset_id") or "custom"),
        "platform_id": str(provider.get("platform_id") or ""),
        "product_id": str(provider.get("product_id") or ""),
        "profile_id": str(provider.get("profile_id") or ""),
        "base_url": str(provider.get("base_url") or ""),
        "credential_configured": bool(credential.get("configured")),
        "query_capability": "unsupported",
        "features": [],
        "state": "unsupported",
        "available": None,
        "balances": [],
        "quota_models": [],
        "updated_at": 0.0,
        "source": "provider",
        "note": "当前适配未发现可由此 API Key 安全读取的官方账户数据接口。",
    }


def _api_key(provider: dict[str, Any], store: CredentialStore, result: dict[str, Any]) -> str:
    credential = provider.get("credential") if isinstance(provider.get("credential"), dict) else {}
    ref = str(credential.get("ref") or "").strip()
    if not ref:
        result["state"] = "credential_missing"
        result["note"] = "尚未配置 API Key。"
        return ""
    try:
        secrets = decode_provider_secrets(store.get(ref))
    except CredentialStoreError as exc:
        result["state"] = "error"
        result["note"] = f"读取系统凭据失败：{exc}"
        return ""
    if not secrets.api_key:
        result["state"] = "credential_missing"
        result["note"] = "尚未配置 API Key。"
        return ""
    return secrets.api_key


def _balance_row(
    *, currency: str, total: Any, total_label: str, components: list[tuple[str, Any]],
    topped_up: Any = "", granted: Any = "",
) -> dict[str, Any]:
    return {
        "currency": str(currency or "").strip(),
        "total": str(total).strip() if total is not None else "",
        "total_label": total_label,
        "components": [
            {"label": label, "value": str(value).strip() if value is not None else ""}
            for label, value in components
        ],
        # Backwards-compatible fields used by older 0.2.0 UIs/tests.
        "topped_up": str(topped_up).strip() if topped_up is not None else "",
        "granted": str(granted).strip() if granted is not None else "",
    }


def _deepseek_balance(provider: dict[str, Any], store: CredentialStore, *, timeout_seconds: float, fetch_json: Callable[..., tuple[dict[str, Any] | None, str]]) -> dict[str, Any]:
    result = _base_result(provider)
    result.update({"query_capability": "balance", "features": ["balance"], "note": ""})
    api_key = _api_key(provider, store, result)
    if not api_key:
        return result
    host = (urlparse(str(provider.get("base_url") or "")).hostname or "").lower()
    if host not in _DEEPSEEK_HOSTS:
        result.update({
            "state": "unsupported", "query_capability": "unsupported", "features": [],
            "note": "此 DeepSeek preset 使用了自定义 API 地址，因此不会向官方账户接口发送凭据。",
        })
        return result
    data, error = fetch_json("https://api.deepseek.com/user/balance", api_key, timeout_seconds=timeout_seconds)
    result["updated_at"] = time.time()
    if data is None:
        result.update({"state": "error", "note": error})
        return result
    if not isinstance(data.get("balance_infos"), list):
        result.update({"state": "error", "note": "账户接口返回了无法识别的数据结构。"})
        return result
    balances: list[dict[str, Any]] = []
    for row in data.get("balance_infos", []):
        if not isinstance(row, dict):
            continue
        currency = str(row.get("currency") or "").strip()
        total = row.get("total_balance")
        if not currency or total is None:
            continue
        balances.append(_balance_row(
            currency=currency,
            total=total,
            total_label="当前余额",
            components=[("充值余额", row.get("topped_up_balance")), ("赠送余额", row.get("granted_balance"))],
            topped_up=row.get("topped_up_balance"),
            granted=row.get("granted_balance"),
        ))
    result.update({
        "state": "ok", "available": bool(data.get("is_available")), "balances": balances,
        "note": "DeepSeek 官方账户接口",
    })
    return result


def _moonshot_balance(provider: dict[str, Any], store: CredentialStore, *, timeout_seconds: float, fetch_json: Callable[..., tuple[dict[str, Any] | None, str]]) -> dict[str, Any]:
    result = _base_result(provider)
    result.update({"query_capability": "balance", "features": ["balance"], "note": ""})
    api_key = _api_key(provider, store, result)
    if not api_key:
        return result
    host = (urlparse(str(provider.get("base_url") or "")).hostname or "").lower()
    account_endpoint = _MOONSHOT_ENDPOINTS.get(host)
    if account_endpoint is None:
        result.update({
            "state": "unsupported", "query_capability": "unsupported", "features": [],
            "note": "此 Moonshot / Kimi preset 使用了自定义 API 地址，因此不会向官方账户接口发送凭据。",
        })
        return result
    endpoint, currency = account_endpoint
    data, error = fetch_json(endpoint, api_key, timeout_seconds=timeout_seconds)
    result["updated_at"] = time.time()
    if data is None:
        result.update({"state": "error", "note": error})
        return result
    body = data.get("data") if isinstance(data.get("data"), dict) else None
    code = data.get("code")
    status = data.get("status")
    if body is None or (code is not None and code != 0) or status is False:
        result.update({"state": "error", "note": "账户接口返回了失败状态或无法识别的数据结构。"})
        return result
    available = body.get("available_balance")
    if available is None:
        result.update({"state": "error", "note": "账户接口没有返回 available_balance。"})
        return result
    cash = body.get("cash_balance")
    voucher = body.get("voucher_balance")
    result.update({
        "state": "ok",
        "available": True if status is None else bool(status),
        "balances": [_balance_row(
            currency=currency,
            total=available,
            total_label="可用余额",
            components=[("现金余额", cash), ("代金券余额", voucher)],
            topped_up=cash,
            granted=voucher,
        )],
        "note": "Kimi 官方账户接口",
    })
    return result


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _quota_window(row: dict[str, Any], *, weekly: bool) -> dict[str, Any] | None:
    prefix = "current_weekly" if weekly else "current_interval"
    remaining = _number(row.get(f"{prefix}_remaining_percent"))
    total = _int_or_none(row.get(f"{prefix}_total_count"))
    used = _int_or_none(row.get(f"{prefix}_usage_count"))
    status = row.get(f"{prefix}_status")
    # Observed MiniMax response status=3 can describe a model without an active
    # quota bucket. Do not turn an empty/100% placeholder into a claimed plan.
    status_number = _int_or_none(status)
    if status_number not in (None, 1) and not (total and total > 0):
        return None
    if remaining is None and total is not None and total > 0 and used is not None:
        remaining = max(0.0, min(100.0, (total - used) / total * 100.0))
    if remaining is None and total is None and used is None:
        return None
    out: dict[str, Any] = {
        "label": "周额度" if weekly else "5 小时额度",
        "remaining_percent": None if remaining is None else max(0.0, min(100.0, remaining)),
        "total_count": total,
        "used_count": used,
    }
    # The API has used both absolute end timestamps and human-readable remains
    # fields over time. Preserve only provider-returned values; the UI can show
    # whichever is present without inventing a reset time.
    for target, keys in (
        ("reset_at", ("weekly_end_time", "current_weekly_end_time") if weekly else ("end_time", "current_interval_end_time")),
        ("remaining_time", ("weekly_remains_time",) if weekly else ("remains_time", "current_interval_remains_time")),
    ):
        for key in keys:
            if row.get(key) not in (None, ""):
                out[target] = row.get(key)
                break
    return out


def _minimax_token_plan(provider: dict[str, Any], store: CredentialStore, *, timeout_seconds: float, fetch_json: Callable[..., tuple[dict[str, Any] | None, str]]) -> dict[str, Any]:
    result = _base_result(provider)
    result.update({"query_capability": "quota", "features": ["quota"], "note": "", "account_label": "MiniMax Token Plan"})
    api_key = _api_key(provider, store, result)
    if not api_key:
        return result
    # Subscription and PAYG credentials are separate products. Fail closed even
    # if a stale config accidentally labels a normal PAYG key as Token Plan.
    if not api_key.strip().lower().startswith("sk-cp"):
        result.update({
            "state": "unsupported", "query_capability": "unsupported", "features": [],
            "note": "当前凭据不是 MiniMax Token Plan Subscription Key。",
        })
        return result
    host = (urlparse(str(provider.get("base_url") or "")).hostname or "").lower()
    endpoint = _MINIMAX_QUOTA_ENDPOINTS.get(host)
    if endpoint is None:
        result.update({
            "state": "unsupported", "query_capability": "unsupported", "features": [],
            "note": "此 MiniMax preset 使用了自定义 API 地址，因此不会向官方套餐接口发送凭据。",
        })
        return result
    data, error = fetch_json(endpoint, api_key, timeout_seconds=timeout_seconds)
    result["updated_at"] = time.time()
    if data is None:
        result.update({"state": "error", "note": error})
        return result
    base_resp = data.get("base_resp") if isinstance(data.get("base_resp"), dict) else {}
    status_code = _int_or_none(base_resp.get("status_code"))
    if status_code not in (None, 0):
        result.update({"state": "error", "note": str(base_resp.get("status_msg") or f"套餐接口返回状态 {status_code}")})
        return result
    rows = data.get("model_remains")
    if not isinstance(rows, list) and isinstance(data.get("data"), dict):
        rows = data["data"].get("model_remains")
    if not isinstance(rows, list):
        result.update({"state": "error", "note": "套餐接口返回了无法识别的数据结构。"})
        return result
    quota_models: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("model_name") or raw.get("name") or "").strip()
        interval = _quota_window(raw, weekly=False)
        weekly = _quota_window(raw, weekly=True)
        if not name or (interval is None and weekly is None):
            continue
        quota_models.append({"model_name": name, "interval": interval, "weekly": weekly})
    if not quota_models:
        result.update({"state": "error", "note": "套餐接口没有返回可确认的有效额度窗口。"})
        return result
    result.update({
        "state": "ok", "available": True, "quota_models": quota_models,
        "note": "MiniMax 官方 Token Plan 接口",
    })
    return result


_ACCOUNT_HANDLERS = {
    "deepseek-balance": _deepseek_balance,
    "moonshot-balance": _moonshot_balance,
    "minimax-token-plan": _minimax_token_plan,
}


def _credential_value(provider: dict[str, Any], store: CredentialStore) -> str:
    credential = provider.get("credential") if isinstance(provider.get("credential"), dict) else {}
    ref = str(credential.get("ref") or "").strip()
    if not ref:
        return ""
    try:
        return decode_provider_secrets(store.get(ref)).api_key
    except CredentialStoreError:
        return ""


def query_account_status(
    provider: dict[str, Any],
    store: CredentialStore,
    *,
    timeout_seconds: float = 8.0,
    fetch_json: Callable[..., tuple[dict[str, Any] | None, str]],
) -> dict[str, Any]:
    """Dispatch provider-owned account queries through product capabilities."""

    preset_id = str(provider.get("preset_id") or "custom").strip().lower()
    product_id = str(provider.get("product_id") or "").strip().lower()
    base_url = str(provider.get("base_url") or "")
    credential = _credential_value(provider, store)
    handler_name = account_status_handler(
        preset_id, base_url, product_id=product_id, credential=credential,
    )
    handler = _ACCOUNT_HANDLERS.get(handler_name)
    if handler is not None:
        return handler(provider, store, timeout_seconds=timeout_seconds, fetch_json=fetch_json)

    result = _base_result(provider)
    if not result["credential_configured"] and bool((provider.get("credential") or {}).get("required")):
        result["state"] = "credential_missing"
        result["note"] = "尚未配置 API Key。"
    return result
