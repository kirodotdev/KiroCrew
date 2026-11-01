"""Stable management-status snapshot for the KiroCrew App UI.

The UI should render facts, not re-implement backend health policy.  This module
turns provider, integration, runtime-capability and telemetry data into one
small, versioned status model that can be consumed by the sidebar, overview
page, diagnostics and future host surfaces.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping


def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _provider_model_count(providers: Iterable[Any]) -> int:
    total = 0
    for provider in providers:
        row = _dict(provider)
        total += len(_list(row.get("models")))
    return total


def build_overview_snapshot(
    *,
    version: str,
    app_home: str,
    credential_backend: str,
    providers: list[dict[str, Any]],
    runtime_capabilities: Mapping[str, Any],
    native_tools: list[str],
    integration: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the management UI's authoritative summary model.

    No secrets or prompt/tool content belong here.  The result intentionally
    contains only facts already safe for the existing status/telemetry APIs.
    """

    integ = _dict(integration)
    diag = _dict(diagnostics)
    session_caps = _list(_dict(runtime_capabilities).get("session_capabilities"))

    provider_count = len(providers)
    model_count = _provider_model_count(providers)
    supported = bool(integ.get("supported"))
    installed = bool(integ.get("installed"))
    active = bool(integ.get("active", installed))
    restart_required = bool(integ.get("restart_required"))
    repairable = bool(integ.get("repairable"))
    failed_turns = int(diag.get("failed") or 0)

    alerts: list[dict[str, str]] = []
    if not supported:
        state = "incompatible"
        label = "Crew 不兼容"
        tone = "error"
        alerts.append({
            "id": "integration-incompatible",
            "tone": "error",
            "title": "当前 KiroCrew 版本无法安全集成",
            "detail": str(integ.get("error") or "未匹配到受支持的 0.9 semantic integration profile。"),
            "action": "integration",
        })
    elif not installed:
        state = "needs_integration"
        label = "需要集成"
        tone = "warning"
        alerts.append({
            "id": "integration-required",
            "tone": "warning",
            "title": "集成需要安装或更新",
            "detail": "安装或修复 Integration 并完全重启 KiroCrew 后，Kiro BYO 才能作为 Agent Harness 使用。",
            "action": "integration",
        })
    elif restart_required or not active:
        state = "restart_pending"
        label = "等待重启"
        tone = "warning"
        alerts.append({
            "id": "integration-restart",
            "tone": "warning",
            "title": "Integration 已写入，但当前 KiroCrew 进程仍在使用旧代码",
            "detail": "完全退出并重新打开 KiroCrew 后，新的 BYO 模型菜单与 Type 才会进入当前 Gateway/renderer。",
            "action": "integration",
        })
    elif repairable:
        state = "repairable"
        label = "需要修复"
        tone = "warning"
        alerts.append({
            "id": "integration-repairable",
            "tone": "warning",
            "title": "检测到不完整的 Kiro BYO 集成",
            "detail": "当前 seam 仍可安全识别，可以从设置中的 KiroCrew 集成执行事务修复。",
            "action": "integration",
        })
    elif provider_count == 0:
        state = "needs_provider"
        label = "需要 Provider"
        tone = "warning"
        alerts.append({
            "id": "provider-required",
            "tone": "warning",
            "title": "尚未配置模型供应商",
            "detail": "添加 Provider 并发现模型后即可从 KiroCrew 选择具体模型。",
            "action": "providers",
        })
    else:
        state = "ready"
        label = "运行正常"
        tone = "success"

    if installed and active and provider_count > 0 and not alerts:
        alerts.append({
            "id": "ready",
            "tone": "success",
            "title": "Kiro BYO 已就绪",
            "detail": "Integration 与 Provider 均已配置，模型能力由实际发现结果驱动。",
            "action": "overview",
        })

    return {
        "schema_version": 1,
        # Runtime/integration facts drive global health. A failed or cancelled
        # turn is a request outcome and must never downgrade the app shell.
        "health": {"state": state, "label": label, "tone": tone},
        "runtime_health": {"state": state, "label": label, "tone": tone},
        "request_outcome": {
            "turns": int(diag.get("turns") or 0),
            "success": int(diag.get("success") or 0),
            "failed": failed_turns,
            "cancelled": int(diag.get("cancelled") or 0),
            "interrupted": int(diag.get("interrupted") or 0),
            "error_categories": dict(_dict(diag.get("error_categories"))),
        },
        "version": version,
        "crew_version": str(integ.get("crew_version") or "unknown"),
        "provider_count": provider_count,
        "model_count": model_count,
        "credential_backend": credential_backend,
        "app_home": app_home,
        "session_capability_count": len(session_caps),
        "native_tools": list(native_tools),
        "native_tool_count": len(native_tools),
        "integration": {
            "installed": installed,
            "active": active,
            "restart_required": restart_required,
            "supported": supported,
            "repairable": repairable,
            "baseline": str(integ.get("baseline") or ""),
            "strategy": str(integ.get("strategy") or ""),
            "runtime_version": str(integ.get("runtime_version") or ""),
            "app_home_bound": bool(integ.get("app_home_bound")),
        },
        "diagnostics": {
            "turns": int(diag.get("turns") or 0),
            "success": int(diag.get("success") or 0),
            "failed": failed_turns,
            "median_first_visible_ms": float(diag.get("median_first_visible_ms") or 0),
            "median_total_ms": float(diag.get("median_total_ms") or 0),
            "input_tokens": int(diag.get("input_tokens") or 0),
            "output_tokens": int(diag.get("output_tokens") or 0),
            "cached_tokens": int(diag.get("cached_tokens") or 0),
            "cache_write_tokens": int(diag.get("cache_write_tokens") or 0),
            "reasoning_tokens": int(diag.get("reasoning_tokens") or 0),
            "cache_usage_reported_turns": int(diag.get("cache_usage_reported_turns") or 0),
            "reasoning_usage_reported_turns": int(diag.get("reasoning_usage_reported_turns") or 0),
            "cache_hit_rate": diag.get("cache_hit_rate") if isinstance(diag.get("cache_hit_rate"), (int, float)) else None,
        },
        "alerts": alerts,
        "capability_policy": (
            "Provider preset 只负责连接模板；reasoning、tools、modalities 等能力属于具体模型，"
            "由运行时发现结果决定。"
        ),
    }
