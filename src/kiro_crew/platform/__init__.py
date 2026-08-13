"""KiroCrew platform contract — the Composed Platform Providers (CPP) seam.

Public API for booting and reading the platform context.  Core code imports
from ``kiro_crew.platform`` only; an enterprise companion imports
``build_default_context`` + the interfaces and supplies its own adapters.

See ``docs/system-specs/modules/platform-context.md``.
"""

from __future__ import annotations

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
    current_context,
    redact_via_context,
    reset_context,
    safe_context_call,
    set_context,
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
