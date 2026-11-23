"""KiroCrew platform contract — the Composed Platform Providers (CPP) seam.

Public API for booting and reading the platform context.  Core code imports
from ``kiro_crew.platform`` only; an enterprise companion imports
``build_default_context`` + the interfaces and supplies its own adapters.

See ``docs/system-specs/modules/platform-context.md``.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# Lazy attribute access (PEP 562). Importing a leaf such as
# ``kiro_crew.platform.update_governance`` runs this file first, and the names
# below pull the governance, security and agent_sdk graphs. Every MCP stub for one
# of Kiro Crew's own servers reaches ``update_governance`` through
# ``code_fingerprint``, so an eager import here cost each of those stubs about
# 10 MB. A name loads its owner on first read and is then bound here, so reads and
# monkeypatching behave as they did when the imports were eager.
_LAZY: dict[str, str] = {
    "AdmissionDecision": "admission",
    "AdmissionPolicy": "admission",
    "PluginManifest": "admission",
    "evaluate_admission": "admission",
    "load_admission_policy": "admission",
    "boot_platform": "bootstrap",
    "bootstrap_context": "bootstrap",
    "build_default_context": "bootstrap",
    "CONTRACT_VERSION": "context",
    "PROFILE_ENTERPRISE": "context",
    "PROFILE_STANDALONE": "context",
    "RESERVED_METHODS": "context",
    "RESERVED_SLOTS": "context",
    "PlatformCompositionError": "context",
    "PlatformContext": "context",
    "async_safe_context_call": "context",
    "binary_content_is_flagged": "context",
    "current_context": "context",
    "redact_log_via_context": "context",
    "redact_via_context": "context",
    "reset_context": "context",
    "safe_context_call": "context",
    "set_context": "context",
    "wide_content_is_flagged": "context",
    "PLUGIN_GROUP": "discovery",
    "PluginAdmissionError": "discovery",
    "resolve_profile": "profile",
    "BASELINE_DENY": "security_authority",
    "PolicyAuthority": "security_authority",
    "SecurityOverlay": "security_authority",
    "assert_security_floor": "security_authority",
}


def __getattr__(name: str) -> Any:
    """Load a re-exported name from the submodule that defines it (:pep:`562`)."""
    owner = _LAZY.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{owner}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


if TYPE_CHECKING:  # names for static type checkers only
    from kiro_crew.platform.admission import (
        AdmissionDecision,
        AdmissionPolicy,
        PluginManifest,
        evaluate_admission,
        load_admission_policy,
    )
    from kiro_crew.platform.bootstrap import boot_platform, bootstrap_context, build_default_context
    from kiro_crew.platform.context import (
        CONTRACT_VERSION,
        PROFILE_ENTERPRISE,
        PROFILE_STANDALONE,
        RESERVED_METHODS,
        RESERVED_SLOTS,
        PlatformCompositionError,
        PlatformContext,
        async_safe_context_call,
        binary_content_is_flagged,
        current_context,
        redact_log_via_context,
        redact_via_context,
        reset_context,
        safe_context_call,
        set_context,
        wide_content_is_flagged,
    )
    from kiro_crew.platform.discovery import PLUGIN_GROUP, PluginAdmissionError
    from kiro_crew.platform.profile import resolve_profile
    from kiro_crew.platform.security_authority import (
        BASELINE_DENY,
        PolicyAuthority,
        SecurityOverlay,
        assert_security_floor,
    )

__all__ = [
    "CONTRACT_VERSION",
    "PROFILE_ENTERPRISE",
    "PROFILE_STANDALONE",
    "PLUGIN_GROUP",
    "PlatformContext",
    "PlatformCompositionError",
    # Declared-inert contract surface (see context.py)
    "RESERVED_SLOTS",
    "RESERVED_METHODS",
    "boot_platform",
    "bootstrap_context",
    "build_default_context",
    "current_context",
    "redact_via_context",
    "redact_log_via_context",
    "binary_content_is_flagged",
    "wide_content_is_flagged",
    "safe_context_call",
    "async_safe_context_call",
    "set_context",
    "reset_context",
    "resolve_profile",
    "PolicyAuthority",
    "SecurityOverlay",
    "BASELINE_DENY",
    "assert_security_floor",
    # Plugin admission control
    "AdmissionPolicy",
    "AdmissionDecision",
    "PluginManifest",
    "PluginAdmissionError",
    "evaluate_admission",
    "load_admission_policy",
]
