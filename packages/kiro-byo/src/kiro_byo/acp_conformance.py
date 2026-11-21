"""Stable ACP v1 conformance facts and profile checks.

The profiles are intentionally protocol-level, not claims that a particular
third-party product version was physically tested in this build environment.
They let the same harness exercise generic ACP behaviour while separately
checking KiroCrew extensions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .protocol import PROTOCOL_VERSION, initialize_result


STANDARD_REQUEST_METHODS = frozenset({
    "initialize", "session/new", "session/load", "session/list", "session/resume",
    "session/delete", "session/close", "session/prompt", "session/set_mode",
    "session/set_config_option",
})
STANDARD_NOTIFICATION_METHODS = frozenset({"session/cancel"})
# Supported only for old Crew clients; newer clients use set_config_option.
# Keep this separate from current-standard method claims.
LEGACY_COMPAT_REQUEST_METHODS = frozenset({"session/set_model"})
KIRO_EXTENSION_METHODS = frozenset({
    "_session/steer", "_kiro/session/compact", "_kiro/session/delete",
    "_kiro.dev/session/terminate", "_kiro/agent/list", "_kiro/agent/send",
    "_kiro/agent/cancel", "_kiro/agent/resume", "_kiro/run/list", "_kiro/run/get",
    "_kiro/run/resume", "_kiro/run/retry", "_kiro/run/replan", "_kiro/team/list",
    "_kiro/team/get", "_kiro/team/send", "_kiro/team/spawn", "_kiro/team/board", "_kiro/team/recycle",
    "_kiro/workspace/list", "_kiro/workspace/get", "_kiro/workspace/diff",
    "_kiro/workspace/check", "_kiro/workspace/compare", "_kiro/workspace/apply", "_kiro/workspace/remove", "_kiro/acp_agents/list", "_kiro/acp/conformance",
})


@dataclass(frozen=True)
class ConformanceProfile:
    profile_id: str
    label: str
    generic: bool = True
    accepts_kiro_extensions: bool = False


PROFILES = (
    ConformanceProfile("generic", "Generic ACP v1"),
    ConformanceProfile("kirocrew", "KiroCrew ACP v1", accepts_kiro_extensions=True),
    ConformanceProfile("zed", "Zed ACP-compatible client"),
    ConformanceProfile("jetbrains", "JetBrains ACP-compatible client"),
    ConformanceProfile("qwen-code", "Qwen Code ACP-compatible client"),
)


def validate_initialize(payload: dict[str, Any] | None = None) -> tuple[str, ...]:
    data = payload or initialize_result()
    issues: list[str] = []
    if int(data.get("protocolVersion") or -1) != PROTOCOL_VERSION:
        issues.append("initialize.protocolVersion is not stable ACP v1")
    caps = data.get("agentCapabilities")
    if not isinstance(caps, dict):
        return tuple(issues + ["initialize.agentCapabilities missing"])
    session_caps = caps.get("sessionCapabilities")
    if not isinstance(session_caps, dict):
        issues.append("sessionCapabilities missing")
    else:
        for required in ("list", "delete", "resume", "close", "additionalDirectories"):
            if required not in session_caps:
                issues.append(f"session capability missing: {required}")
    prompt = caps.get("promptCapabilities")
    if not isinstance(prompt, dict) or "image" not in prompt:
        issues.append("promptCapabilities.image missing")
    if not isinstance(data.get("agentInfo"), dict):
        issues.append("agentInfo missing")
    return tuple(issues)


def conformance_report() -> dict[str, Any]:
    issues = validate_initialize()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "initialize_valid": not issues,
        "issues": list(issues),
        "standard_request_methods": sorted(STANDARD_REQUEST_METHODS),
        "standard_notification_methods": sorted(STANDARD_NOTIFICATION_METHODS),
        "legacy_compat_request_methods": sorted(LEGACY_COMPAT_REQUEST_METHODS),
        "kiro_extension_methods": sorted(KIRO_EXTENSION_METHODS),
        "profiles": [profile.__dict__ for profile in PROFILES],
    }
