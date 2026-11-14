"""Provider-neutral account-status facade for the management UI.

Provider-owned endpoint and response-shape knowledge lives under
``kiro_byo.providers``.  This module only preserves the public API and supplies
the caller's HTTP opener so tests/embedders can control network I/O.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .credential_store import CredentialStore
from .providers.account_status import query_account_status


def _fetch_json(endpoint: str, api_key: str, *, timeout_seconds: float) -> tuple[dict[str, Any] | None, str]:
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        return None, f"账户查询失败：HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"账户查询失败：{exc}"
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "账户接口返回了无效 JSON。"
    if not isinstance(data, dict):
        return None, "账户接口返回了无法识别的数据结构。"
    return data, ""


def account_status_for_provider(
    provider: dict[str, Any],
    store: CredentialStore,
    *,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    return query_account_status(
        provider,
        store,
        timeout_seconds=timeout_seconds,
        fetch_json=_fetch_json,
    )


def account_statuses(
    providers: list[dict[str, Any]],
    store: CredentialStore,
    *,
    timeout_seconds: float = 8.0,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for provider in providers:
        row = account_status_for_provider(provider, store, timeout_seconds=timeout_seconds)
        if row.get("query_capability") == "unsupported":
            continue
        out.append(row)
    return out
