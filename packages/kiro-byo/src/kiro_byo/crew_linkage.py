"""Read-only Crew ACP backend linkage facts and safe member-sync preconditions.

Host-owned configuration is the authority. A configured backend is not the
same as a running session's backend; schedules and workflows have their own
resolution rules. This module contains no platform paths or persistence.
"""
from __future__ import annotations

from typing import Any, Mapping


def backend_id(value: Any) -> str:
    return str(value or "").strip() or "kiro"


def role_pin(agent: Any, role: str) -> str:
    roles = getattr(agent, "role_models", None)
    if isinstance(roles, Mapping):
        return str(roles.get(role) or "auto").strip() or "auto"
    return "auto"


def role_resolution(agent: Any, role: str) -> dict[str, str]:
    configured = role_pin(agent, role)
    try:
        resolved = str(agent.resolve_model(role) or "auto").strip() or "auto"
    except (AttributeError, TypeError, ValueError):
        resolved = "unknown"
    return {
        "configured_model": configured,
        "resolved_model": resolved,
        "selection": "inherited" if configured == "auto" else "pinned",
    }


def linkage_snapshot(agent: Any, *, member_declared: bool | None = None) -> dict[str, Any]:
    """Describe Crew defaults without pretending active sessions were migrated.

    Agent-backed schedules, background roles and subagents use the main ACP
    backend unless a session/job supplies an explicit override. Script/command
    cron entries have no ACP backend and are handled separately by the task UI.
    """
    main = backend_id(getattr(agent, "acp_backend", ""))
    # Crew owns selection of member backends, including fallback if unset.
    member_value = getattr(agent, "member_acp_backend", None)
    member = backend_id(member_value) if member_value is not None else "unknown"
    return {
        "main": {"configured_backend": main, "scope": "new_sessions"},
        "members": {
            "configured_backend": member,
            "scope": "member_default",
            "follows_main": member == main,
            "declared": member_declared,
        },
        "background": {**role_resolution(agent, "background"), "runtime_backend": main},
        "subagent": {**role_resolution(agent, "subagent"), "runtime_backend": main},
        "cron": {"runtime_backend": main, "scope": "agent_jobs_default"},
        "workflow": {"runtime_backend": main, "scope": "default_unless_step_override"},
        "heartbeat": {"runtime_backend": main, "scope": "host_owned"},
        "active_sessions": {"runtime_backend": "not_inspected", "scope": "unchanged"},
        "follow_policy": "not_installed",
    }


def check_member_sync(expected_main: str, expected_member: str, actual_main: str, actual_member: str) -> str:
    """Optimistic check prevents a stale UI overwriting a newer host choice."""
    if not expected_main or not expected_member:
        raise ValueError("both expected backend values are required")
    if backend_id(expected_main) != backend_id(actual_main):
        raise ValueError("main backend changed; refresh before syncing")
    if backend_id(expected_member) != backend_id(actual_member):
        raise ValueError("member backend changed; refresh before syncing")
    if actual_main == "unknown" or actual_member == "unknown":
        raise ValueError("cannot sync an unknown Crew backend")
    return backend_id(actual_main)


def model_pin_allowed(*, selected_backend: str, model: str, byo_models: set[str]) -> bool:
    """Clearing an old pin is always safe; setting a BYO pin needs BYO host."""
    if model in ("", "auto"):
        return True
    return backend_id(selected_backend) == "kiro-byo" and model in byo_models


def implicit_member_backend(main_backend: str) -> str:
    """Crew 0.11 member default when no explicit member backend is stored.

    The BYO adapter makes an implicit member backend follow BYO.  Every other
    main backend keeps Crew 0.11's shipped KAS member default.
    """
    return "kiro-byo" if backend_id(main_backend) == "kiro-byo" else "kas"


def linkage_change_plan(
    snapshot: Mapping[str, Any], *, target_main: str, member_mode: str,
    fixed_member: str = "", main_backends: set[str], member_backends: set[str],
    cron_models: list[Mapping[str, str]] | None = None, byo_models: set[str] | None = None,
) -> dict[str, Any]:
    """Plan a *one-time* native-Crew configuration transaction.

    No persistent watch is installed. Explicit per-task/session overrides and
    host-owned job data are never edited. Backends are obtained from Crew's
    advertised registries, not a BYO-specific hardcoded whitelist.
    """
    before_main = str(snapshot["main"]["configured_backend"])
    before_member = str(snapshot["members"]["configured_backend"])
    if before_main == "unknown" or before_member == "unknown":
        raise ValueError("Crew backend configuration is not fully known")
    if target_main not in main_backends or target_main in ("", "unknown"):
        raise ValueError("target main backend is not selectable by Crew")
    if member_mode not in {"follow", "keep", "fixed"}:
        raise ValueError("member mode must be follow, keep or fixed")
    declared = (snapshot.get("members") or {}).get("declared")
    if member_mode == "follow":
        target_member = implicit_member_backend(target_main)
        member_action = "inherit"
    elif member_mode == "keep":
        target_member = before_member
        member_action = "keep"
    else:
        target_member = fixed_member
        member_action = "fixed"
    if not target_member or target_member not in member_backends:
        raise ValueError("target member backend is not registered for dispatch")
    # Follow is represented by NO stored member override. This is what lets a
    # later native Crew main-harness switch restore the official KAS default
    # instead of leaving a hidden BYO pin behind.
    changes = {}
    if target_main != before_main:
        changes["agent.acp_backend"] = {"from": before_main, "to": target_main}
    if member_action == "inherit":
        if before_member != target_member or declared is True:
            changes["agent.member_acp_backend"] = {
                "from": before_member, "to": target_member, "action": "clear_override"
            }
    elif member_action == "fixed":
        if before_member != target_member or declared is not True:
            changes["agent.member_acp_backend"] = {
                "from": before_member, "to": target_member, "action": "set_override"
            }
    warnings = ["sessions_unchanged", "one_time_only"]
    catalog = byo_models or set()
    conflicting = []
    for row in cron_models or []:
        if row.get("execution_kind") != "agent":
            continue
        pin = str(row.get("model") or "").strip()
        if pin and ((target_main == "kiro-byo" and pin not in catalog) or
                    (target_main != "kiro-byo" and pin in catalog)):
            conflicting.append({"id": str(row.get("id") or ""), "model": pin})
    role_conflicts = []
    for role in ("background", "subagent"):
        pin = str((snapshot.get(role) or {}).get("configured_model") or "auto")
        if pin != "auto" and ((target_main == "kiro-byo" and pin not in catalog) or
                              (target_main != "kiro-byo" and pin in catalog)):
            role_conflicts.append({"role": role, "model": pin})
    if conflicting:
        warnings.append("cron_model_conflicts")
    if role_conflicts:
        warnings.append("role_model_conflicts")
    return {
        "before": {"main": before_main, "member": before_member},
        "before_member_declared": declared,
        "after": {"main": target_main, "member": target_member},
        "member_mode": member_mode,
        "member_action": member_action,
        "changes": changes,
        "cron_model_conflicts": conflicting,
        "role_model_conflicts": role_conflicts,
        "warnings": warnings,
        "scope": "future_default_sessions_only",
        "persistent_follow": False,
    }


def background_execution_snapshot(turns: list[Mapping[str, Any]], *, limit: int = 4) -> dict[str, Any]:
    """A *recorded* BYO background execution, not a guessed Crew backend.

    Only telemetry explicitly tagged by the Crew lightweight role (or the
    legacy BYO background Auto path) qualifies.
    A routed model on an errored turn is a selection, NOT a successful provider call.
    """
    accepted = {"background_auto", "background_auto_restore"}
    rows: list[dict[str, Any]] = []
    for turn in reversed(turns):
        if str(turn.get("selection_source") or "") not in accepted and turn.get("is_background_role") is not True:
            continue
        outcome = str(turn.get("outcome") or "unknown")
        rows.append({
            "timestamp": float(turn.get("timestamp") or 0),
            "outcome": outcome,
            "provider_id": str(turn.get("provider_id") or ""),
            "served_model": str(turn.get("served_model") or ""),
            "requested_model": str(turn.get("selection_requested") or turn.get("requested_model") or "auto"),
            "error_kind": str(turn.get("error_kind") or ""),
            "error_category": str(turn.get("error_category") or ""),
            "error_site": str(turn.get("error_site") or ""),
            "total_ms": float(turn.get("total_ms") or 0),
            "input_tokens": int(turn.get("input_tokens") or 0),
            "output_tokens": int(turn.get("output_tokens") or 0),
            "run_id": str(turn.get("run_id") or ""),
            "turn_id": str(turn.get("turn_id") or ""),
            "recorded_by": "kiro-byo",
            "upstream_verified": outcome == "success",  # successful turn, not a billing receipt
        })
        if len(rows) >= limit:
            break
    return {
        "recent": rows,
        "latest": rows[0] if rows else None,
        "failed_recent": sum(1 for item in rows if item["outcome"] in {"error", "failed"}),
        "scope": "recorded_byo_background_auto_only",
    }
