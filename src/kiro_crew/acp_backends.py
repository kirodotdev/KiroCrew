"""Re-export shim: the ACP backend registry lives behind the agent-SDK boundary.

The definitions live in :mod:`kiro_crew.agent_sdk.backends`, which
consolidates the capability mechanism inside ``kiro_crew.agent_sdk``. Read that
module for what each name means and why; this file exists so the ~30 existing
``from kiro_crew.acp_backends import ...`` call sites keep working, and so a
future one does too.

**Re-export, never a copy.** ``register_selectable_backend`` and
``apply_selectable_denials`` mutate module state that lives in
``agent_sdk.backends``: the names below bind the SAME function objects, so the
registry has one ``_baseline``/``_selectable`` pair whichever path imported it. The
private pair is deliberately NOT re-exported — a second binding to a mutable set
is how two views of one registry start disagreeing.

**Prefer the new path in new code.** This shim is not deprecated and nothing warns;
importing from here is correct. What it must not become is the path a NEW consumer
finds first, so ``test_agent_sdk_capabilities`` pins that the file stays a shim with
no definitions of its own.

Importing this module executes ``agent_sdk/__init__``, and that chain stays
import-light on purpose — no ``kiro_crew.config``, ``kiro_crew.platform`` or
``kiro_crew.acp`` at module scope — because ``config.loader`` reaches
``resolve_selected_backend`` from inside ``KiroCrewConfig.load()`` and a config
import here would re-enter that load. ``test_acp_capability_sets_leaf`` pins it in
a subprocess.
"""

from __future__ import annotations

from kiro_crew.agent_sdk.backends import (  # noqa: F401 - re-exported for existing importers
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PERMISSION_CONFIG,
    ACP_BACKEND_PERMISSION_SETTING,
    ACP_BACKEND_ROUTING,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_HOST_AUTH_CALLBACK,
    ACP_BACKENDS_INLINE_COMPACTION,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KIRO_SLASH_COMMANDS,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_LOAD_WITHOUT_MODES,
    ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    ACP_BACKENDS_POD_HOME_REMAP,
    ACP_BACKENDS_PRIVATE_MEMORY_MCP,
    ACP_BACKENDS_SEED_LOCAL_SETTINGS,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_SIDE_READONLY,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STRUCTURED_REFUSAL,
    BASELINE_SELECTABLE_BACKENDS,
    GOVERNANCE_FLOOR_BACKEND,
    POLICY_ID_BY_BACKEND,
    POLICY_ID_KIRO,
    Routing,
    apply_selectable_denials,
    model_registry_namespace,
    permission_config_for,
    permission_setting_for,
    register_selectable_backend,
    registered_backends,
    resolve_selected_backend,
    routing_for,
    selectable_backend_values,
    selectable_backends,
)

# Declared per harness rather than listed as a capability set: whether a
# ``kiro-cli logout`` retires a running child is a fact about how that harness signs
# in, and it is projected from the same declaration that gives the credential floor
# the leaf it fences. See ``agent_sdk.host_auth``.
from kiro_crew.agent_sdk.host_auth import (  # noqa: E402,F401 - re-exported for importers
    backends_retired_by_host_logout,
)

__all__ = [
    "ACP_BACKENDS_ACP_RUNTIME",
    "ACP_BACKENDS_ADVERTISED_MODEL_SELECTION",
    "ACP_BACKENDS_COMPACT",
    "ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION",
    "ACP_BACKENDS_HARNESS_OWNED_SESSIONS",
    "ACP_BACKENDS_HOST_AUTH_CALLBACK",
    "ACP_BACKENDS_INLINE_COMPACTION",
    "ACP_BACKENDS_INTERNAL_SANDBOX",
    "ACP_BACKENDS_KIRO_SLASH_COMMANDS",
    "ACP_BACKENDS_KNOWN",
    "ACP_BACKENDS_LOAD_WITHOUT_MODES",
    "ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD",
    "ACP_BACKENDS_MEMBER_DISPATCH",
    "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION",
    "ACP_BACKENDS_POD_HOME_REMAP",
    "ACP_BACKENDS_PRIVATE_MEMORY_MCP",
    "ACP_BACKENDS_SEED_LOCAL_SETTINGS",
    "ACP_BACKENDS_SESSION_MCP_ARRAY",
    "ACP_BACKENDS_SESSION_SHARING",
    "ACP_BACKENDS_SIDE_READONLY",
    "ACP_BACKENDS_STEER",
    "ACP_BACKENDS_STRUCTURED_REFUSAL",
    "ACP_BACKEND_CLAUDE",
    "ACP_BACKEND_CODEX",
    "ACP_BACKEND_KAS",
    "ACP_BACKEND_KIRO",
    "ACP_BACKEND_OPENCODE",
    "ACP_BACKEND_PERMISSION_CONFIG",
    "ACP_BACKEND_PERMISSION_SETTING",
    "ACP_BACKEND_ROUTING",
    "BASELINE_SELECTABLE_BACKENDS",
    "GOVERNANCE_FLOOR_BACKEND",
    "POLICY_ID_BY_BACKEND",
    "POLICY_ID_KIRO",
    "Routing",
    "apply_selectable_denials",
    "backends_retired_by_host_logout",
    "model_registry_namespace",
    "permission_config_for",
    "permission_setting_for",
    "register_selectable_backend",
    "registered_backends",
    "resolve_selected_backend",
    "routing_for",
    "selectable_backend_values",
    "selectable_backends",
]
