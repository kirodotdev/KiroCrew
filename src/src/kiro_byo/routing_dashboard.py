"""Read-only observability model for Tiga adaptive routing.

This module powers the Modes page. It never mutates routing configuration and
never exposes sliders for internal weights: users can inspect what Tiga has
observed and why it chose a model, while policy packs remain the authority for
routing intent.
"""
from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from .adaptive_routing import blend_speed, runtime_failure_rows, snapshot_from_rows, task_profile_label
from .config import KiroByoConfig
from .mode_router import _model_priors, model_tiers
from .policy_packs import get_policy_pack
from .protocol import parse_type_selection
from .runtime_trace import RuntimeTraceStore
from .telemetry import TurnTelemetryStore


def _median(values: Sequence[float]) -> float | None:
    clean = [float(v) for v in values if v is not None and float(v) >= 0]
    return statistics.median(clean) if clean else None


def _rate(rows: Sequence[Mapping[str, Any]]) -> float | None:
    if not rows:
        return None
    return sum(1 for row in rows if str(row.get("outcome") or "") == "success") / len(rows)


def _confidence_zh(value: str) -> str:
    return {"high": "高", "medium": "中", "low": "低", "none": "暂无"}.get(str(value or ""), "暂无")


def _model_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return str(row.get("provider_id") or ""), str(row.get("served_model") or "")


def _telemetry_policy(row: Mapping[str, Any]) -> tuple[str, str]:
    pack = str(row.get("mode_pack") or "").strip()
    policy = str(row.get("mode_type") or "").strip()
    if pack and policy and policy != "concrete":
        return pack, policy
    selected = parse_type_selection(str(row.get("requested_model") or ""))
    if selected is not None:
        return selected.mode_id, selected.type_id
    return "", ""


def _recent_policy_usage(
    rows: Sequence[Mapping[str, Any]],
    *,
    pack_id: str,
    policy_id: str,
    model_names: Mapping[tuple[str, str], str],
    now: float,
    window_hours: int = 60,
) -> dict[str, Any]:
    cutoff = now - max(1, int(window_hours)) * 3600
    scoped = [
        row for row in rows
        if float(row.get("timestamp") or 0.0) >= cutoff
        and _telemetry_policy(row) == (pack_id, policy_id)
    ]
    total = len(scoped)
    success = sum(1 for row in scoped if str(row.get("outcome") or "") == "success")
    failed = sum(1 for row in scoped if str(row.get("outcome") or "") in {"error", "failed"})
    other = max(0, total - success - failed)
    counts: Counter[tuple[str, str]] = Counter(
        (str(row.get("provider_id") or ""), str(row.get("served_model") or ""))
        for row in scoped
        if str(row.get("served_model") or "")
    )
    models = [
        {
            "provider_id": provider_id,
            "model": model_id,
            "name": model_names.get((provider_id, model_id), model_id),
            "turns": count,
            "share": (count / total) if total else 0.0,
        }
        for (provider_id, model_id), count in counts.most_common(8)
    ]
    recent_calls = []
    for row in reversed(scoped):
        model_id = str(row.get("served_model") or "")
        provider_id = str(row.get("provider_id") or "")
        if not model_id:
            continue
        recent_calls.append({
            "timestamp": float(row.get("timestamp") or 0.0),
            "provider_id": provider_id,
            "model": model_id,
            "name": model_names.get((provider_id, model_id), model_id),
            "outcome": str(row.get("outcome") or ""),
            "total_ms": float(row.get("total_ms") or 0.0),
        })
        if len(recent_calls) >= 5:
            break
    return {
        "window_hours": int(window_hours),
        "turns": total,
        "success": success,
        "failed": failed,
        "other": other,
        "models": models,
        "recent_calls": recent_calls,
        "median_total_ms": _median([
            float(row.get("total_ms") or 0.0) for row in scoped
            if str(row.get("outcome") or "") == "success" and float(row.get("total_ms") or 0.0) > 0
        ]),
    }


def build_routing_dashboard(
    config: KiroByoConfig,
    *,
    telemetry: TurnTelemetryStore | None = None,
    trace: RuntimeTraceStore | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    telemetry = telemetry or TurnTelemetryStore(max_records=max(500, limit))
    trace = trace or RuntimeTraceStore()
    rows = telemetry.recent(limit)
    evidence_rows = list(rows) + runtime_failure_rows(trace, limit=limit)
    pack = get_policy_pack(config.mode_pack)
    selected_policy = pack.policy(config.mode_type) if pack is not None else None
    if selected_policy is None and pack is not None:
        selected_policy = pack.policy(pack.default_policy)
    adaptive_profile = (
        selected_policy.execution.routing.adaptive if selected_policy is not None else None
    )
    snapshot = snapshot_from_rows(evidence_rows, profile=adaptive_profile)
    now = time.time()
    model_names = {
        (provider.id, model.model_id): model.name
        for provider in config.providers
        for model in provider.models
    }

    model_profiles: list[dict[str, Any]] = []
    for provider in config.providers:
        for model in provider.models:
            quality, static_speed, economy, _reasons = _model_priors(model)
            evidence = snapshot.for_model(provider.id, model.model_id)
            if selected_policy is not None:
                routing = selected_policy.execution.routing
                base_alpha = routing.adaptive.speed_alpha_base
                speed_weight = float(routing.speed)
                max_alpha = max(
                    routing.adaptive.speed_alpha_min,
                    min(
                        routing.adaptive.speed_alpha_max,
                        base_alpha + routing.adaptive.speed_alpha_speed_weight * speed_weight,
                    ),
                )
            else:
                max_alpha = 0.52
            effective_speed = blend_speed(
                static_speed, evidence, max_alpha=max_alpha, profile=adaptive_profile
            )
            task_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for row in rows:
                if _model_key(row) == (provider.id, model.model_id):
                    task_rows[str(row.get("task_profile") or "unknown")].append(row)
            task_profiles = [
                {
                    "id": task,
                    "label": task_profile_label(task),
                    "samples": len(items),
                    "success_rate": _rate(items),
                }
                for task, items in sorted(task_rows.items(), key=lambda item: (-len(item[1]), item[0]))
                if task not in {"", "concrete", "unknown"}
            ]
            model_profiles.append({
                "provider_id": provider.id,
                "provider_name": provider.name,
                "model": model.model_id,
                "name": model.name,
                "tiers": list(model_tiers(model)),
                "samples": evidence.samples,
                "confidence": evidence.confidence,
                "confidence_label": _confidence_zh(evidence.confidence),
                "success_rate": evidence.success_rate,
                "reliability": evidence.reliability,
                "median_ttft_ms": evidence.median_ttft_ms,
                "median_total_ms": evidence.median_total_ms,
                "static": {
                    "quality": quality,
                    "speed": static_speed,
                    "economy": economy,
                },
                "runtime": {
                    "speed": evidence.runtime_speed,
                    "reliability": evidence.reliability if evidence.samples else None,
                },
                "effective": {
                    "quality": quality,
                    "speed": effective_speed,
                    "economy": economy,
                    "reliability": evidence.reliability,
                },
                "routing_source": str(getattr(model, "routing_source", "unknown") or "unknown"),
                "routing_confidence": str(getattr(model, "routing_confidence", "unknown") or "unknown"),
                "task_profiles": task_profiles[:6],
            })

    policy_cards: list[dict[str, Any]] = []
    # The user-visible "last task features" must describe a retained,
    # completed request, not a transient router event that may already have
    # rolled out of the trace window or survived a request-history clear.
    recent_routes: list[dict[str, Any]] = []
    for row in reversed(rows):
        raw_signals = row.get("route_signals")
        if not isinstance(raw_signals, dict) or not raw_signals:
            continue  # Old schema v4 turns have no durable feature vector.
        pack_id, policy_id = _telemetry_policy(row)
        if not pack_id or not policy_id:
            continue
        signals = {
            key: max(0.0, min(1.0, float(raw_signals[key])))
            for key in (
                "complexity", "breadth", "parallelizability", "risk",
                "tool_intensity", "write_intensity", "context_demand",
                "verification_value", "latency_sensitivity",
            ) if isinstance(raw_signals.get(key), (int, float))
        }
        if not signals:
            continue
        model_id = str(row.get("served_model") or "")
        provider_id = str(row.get("provider_id") or "")
        diagnostic = row.get("route_diagnostics")
        diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
        recent_routes.append({
            "timestamp": float(row.get("timestamp") or 0.0),
            "pack": pack_id, "policy": policy_id,
            "policy_label": policy_id.title() + " Type",
            "provider_id": provider_id, "model": model_id,
            "model_name": model_names.get((provider_id, model_id), model_id),
            "task_profile": str(row.get("task_profile") or "unknown"),
            "task_profile_label": task_profile_label(str(row.get("task_profile") or "unknown")),
            "complexity": signals.get("complexity"),
            "signals": signals,
            "score": float(row.get("routing_score") or 0.0),
            "runtime_confidence": str(row.get("routing_confidence") or "none"),
            "outcome": str(row.get("outcome") or ""),
            "reasons": list(diagnostic.get("reasons") or []),
            "candidates": list(diagnostic.get("candidates") or []),
            "diagnostics": diagnostic,
        })
        if len(recent_routes) >= 40:
            break
    if pack is not None:
        for policy in pack.policies:
            policy_rows = [row for row in rows if _telemetry_policy(row) == (pack.id, policy.id)]
            recent = next((row for row in recent_routes if row.get("pack") == pack.id and row.get("policy") == policy.id), None)
            model_counts = Counter(str(row.get("served_model") or "") for row in policy_rows if str(row.get("served_model") or ""))
            routing = policy.execution.routing
            policy_cards.append({
                "id": policy.id,
                "display_name": policy.display_name,
                "localized_name": policy.localized_name,
                "short_description": policy.short_description,
                "description": policy.description,
                "turns": len(policy_rows),
                "success_rate": _rate(policy_rows),
                "median_ttft_ms": _median([float(row.get("first_visible_ms") or -1) for row in policy_rows]),
                "median_total_ms": _median([float(row.get("total_ms") or -1) for row in policy_rows]),
                "tier_pool": str(routing.primary_tier_pool or routing.tier_pool or "any"),
                "tier_fallback": str(routing.tier_fallback or "capability"),
                "weights": {
                    "quality": float(routing.quality),
                    "speed": float(routing.speed),
                    "economy": float(routing.economy),
                },
                "thinking_auto": str(policy.execution.auto_reasoning.thinking),
                "effort_auto": str(policy.execution.auto_reasoning.effort),
                "collaboration": str(policy.execution.collaboration),
                "sticky": bool(policy.execution.sticky),
                "max_agents": int(policy.execution.orchestration.max_agents),
                "max_parallel": int(policy.execution.orchestration.max_parallel),
                "worker_mode": str(policy.execution.orchestration.worker_mode),
                "worker_tier_pool": str(policy.execution.orchestration.worker_tier_pool),
                "single_writer": bool(policy.execution.orchestration.single_writer),
                "secondary_enabled": policy.execution.secondary is not None,
                "secondary_always": bool(policy.execution.secondary and policy.execution.secondary.trigger.always),
                "top_models": [{"model": model, "turns": count} for model, count in model_counts.most_common(4)],
                "recent_usage": _recent_policy_usage(
                    rows,
                    pack_id=pack.id,
                    policy_id=policy.id,
                    model_names=model_names,
                    now=now,
                    window_hours=60,
                ),
                "last_route": recent,
            })

    routed_rows = [row for row in rows if _telemetry_policy(row) != ("", "")]
    task_counts = Counter(str(row.get("task_profile") or "unknown") for row in routed_rows)
    task_profiles = []
    for task, count in task_counts.most_common():
        items = [row for row in routed_rows if str(row.get("task_profile") or "unknown") == task]
        task_profiles.append({
            "id": task,
            "label": task_profile_label(task),
            "turns": count,
            "success_rate": _rate(items),
        })

    oldest = min((float(row.get("timestamp") or now) for row in rows), default=now)
    window_days = min(30.0, max(0.0, (now - oldest) / 86400.0))
    confidence_counts = Counter(profile["confidence"] for profile in model_profiles if profile["samples"])
    state = "ready" if len(routed_rows) >= 30 and snapshot.observed_models >= 2 else ("learning" if routed_rows else "waiting")
    return {
        "schema_version": 1,
        "generated_at": now,
        "pack": pack.id if pack is not None else config.mode_pack,
        "pack_label": pack.short_name if pack is not None else config.mode_pack,
        "adaptive": {
            "enabled": True,
            "strategy": "telemetry_blend",
            "router_model": False,
            "state": state,
            "learned_turns": len(routed_rows),
            "observed_models": snapshot.observed_models,
            "telemetry_turns": len(rows),
            "window_days": round(window_days, 1),
            "confidence": dict(confidence_counts),
            "signals": ["recent_latency", "success_reliability", "task_profile"],
            "non_adaptive": ["quality_prior", "economy_prior"],
            "policy_driven": True,
            "active_policy": selected_policy.id if selected_policy is not None else "",
            "history_limit": adaptive_profile.history_limit if adaptive_profile is not None else limit,
            "half_life_days": adaptive_profile.half_life_days if adaptive_profile is not None else None,
        },
        "policies": policy_cards,
        "models": sorted(model_profiles, key=lambda row: (-int(row["samples"]), row["provider_name"], row["name"])),
        "task_profiles": task_profiles,
        "recent_routes": recent_routes[:20],
    }
