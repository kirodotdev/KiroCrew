"""Configuration loader for KiroCrew.

Config location: ~/.kiro/crew/config.json (overridden by KIROCREW_HOME)
Credentials:    ~/.kiro/crew/.env (overridden by KIROCREW_HOME)

KiroCrew is KiroACP-only: the sole provider is the ACP adapter driving the
kiro-cli backend. This module handles session timeouts, hook rules, and the
dashboard URL via the config file. (The dashboard *port* is set with the
``KIROCREW_PORT`` env var, not a config key.)
"""

from __future__ import annotations

# Every import below stays bound here: callers and tests import and patch these
# names through this facade, and its owners read most of them, not its own code
# (hence ``noqa: F401``).
import asyncio  # noqa: F401
import codecs as _codecs  # noqa: F401
import contextlib  # noqa: F401
import copy  # noqa: F401
import hashlib as _hashlib  # noqa: F401
import importlib as _importlib
import json  # noqa: F401
import locale as _locale  # noqa: F401
import logging
import math  # noqa: F401 - historical loader namespace compatibility
import os  # noqa: F401
import re as _re  # noqa: F401
import shutil  # noqa: F401
import stat as _stat  # noqa: F401
import sys
import sys as _sys
import threading  # noqa: F401
import uuid  # noqa: F401
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping  # noqa: F401
from dataclasses import MISSING, asdict, dataclass, field  # noqa: F401 - loader namespace
from datetime import datetime, timezone  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import TYPE_CHECKING, Any, Literal  # noqa: F401
from urllib.parse import urlsplit as _urlsplit  # noqa: F401 - compatibility facade

# Module alias for post-split helpers. The `from ... import` list below is a
# FROZEN pre-split alias snapshot (test_loader_reexports_historical_snapshot_by_identity),
# so new resolution helpers are reached through the module, not re-exported.
import kiro_crew.config.resolution as _resolution  # noqa: F401
from kiro_crew import (  # noqa: F401
    __version__,
    _facade_mirror,
    model_registry,
    model_scope,
    pinned_fs,
    platform_compat,
    windows_acl,
)
from kiro_crew.agent_sdk.backends import (  # noqa: F401
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    resolve_cc_permission_mode,
)
from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_ACP, capabilities_for  # noqa: F401
from kiro_crew.agent_spec_format import iter_agent_spec_files, parse_agent_spec_text  # noqa: F401

# Leaf module (stdlib + platform_compat only) — no import cycle with config.
from kiro_crew.atomic_write import atomic_write, on_event_loop  # noqa: F401
from kiro_crew.aws_project_profiles import (  # noqa: F401
    PROJECT_PROFILES_KEY,
    coerce_project_profiles,
    project_profile_refusals,
)

# Post-split section internals are reached through the module: the name-level
# `from kiro_crew.config.sections import (...)` block below is a FROZEN
# pre-split snapshot (test_config_module_boundaries pins it), so a coercer added
# after the split must not join it.
from kiro_crew.config import migration as _migration  # noqa: F401
from kiro_crew.config import sections as _sections  # noqa: F401

# Section DTOs and their field-level coercion live in a one-way sibling module.
# Re-export every historical loader name so existing imports keep working;
# KiroCrewConfig, in ``config.loader_config``, owns read/merge/save.
from kiro_crew.config.fields import _coerce_bool, _safe_str  # noqa: F401

# Historical loader names no builder reads any more (a section's defaults are its
# DTO fields'), kept bound from their owners for the facade.
from kiro_crew.config.integration_sections import (  # noqa: F401
    _CU_DEFAULT_ATTACH_SCREENSHOT,
    _CU_DEFAULT_MAX_TREE_DEPTH,
    _CU_DEFAULT_MAX_TREE_NODES,
    _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
    _CU_DEFAULT_SCREENSHOT_MAX_PX,
    _CU_DEFAULT_TEXT_LIMIT,
    _DEFAULT_BACKOFF_MAX,
    _DEFAULT_MAX_RECOVERY,
    _DEFAULT_PROBE_FAILS,
    _DEFAULT_SSH_COMPRESSION,
    _DEFAULT_TUNNEL_BASE_PORT,
    _DEFAULT_WARM_SET_CAP,
    AppSettingsConfig,
    AwsConfig,
)
from kiro_crew.config.loader_agents import resolve_agent_identity
from kiro_crew.config.loader_config import KiroCrewConfig
from kiro_crew.config.migration import (  # noqa: F401
    _REPORTED_SUPERSEDED_KEYS,
    CONNECTIONS_UI_MIGRATION_MARKER,
    MIGRATE_AGENTS,
    MIGRATE_CONNECTIONS_UI,
    MIGRATE_DEFAULT_AGENT,
    MIGRATE_STAR_DEFAULT_TEMPLATE,
    MIGRATE_SUPERSEDED_DEFAULTS,
    MIGRATE_WORKSPACES,
    _adopt_in_memory,
    _overlay_supplies,
    _report_superseded_defaults,
)

# Pure path primitives live in the leaf module ``config.paths`` (stdlib-only,
# no ``kiro_crew`` imports) so the modules that only need ``config_dir()`` can
# import them from there without transitively pulling in the full loader (DTOs,
# schema validation, the process-global cache, and the provider factory).
# Re-exported here for backward compatibility — existing callers keep importing
# these from ``kiro_crew.config.loader``.
#
# The *dir-derived* helpers (config_path, workspace_root, workspace_dir_for, …)
# live in the loader owners, not in the leaf: a patch of
# ``kiro_crew.config.loader.config_dir`` reaches their ``config_dir()`` calls
# through this facade's mirror (used across the suite).
from kiro_crew.config.paths import (  # noqa: F401, kiro_agents_dir
    _WORKSPACE_DIR_NAME,
    CONFIG_DIR_NAME,
    OUTBOX_DIR_NAME,
    SCRATCH_ROOT_ENV,
    _default_workspace_base,
    _safe_dir_name,
    config_dir,
    config_package_dir,
    data_home,
    demote_scratch_root_override,
    ensure_data_home,
    kiro_agents_dir,
    mate_agents_dir,
    project_agents_dir,
    scratch_root_owner_refusal,
    scratch_root_writer_refusal,
    valid_scratch_root_override,
)
from kiro_crew.config.resolution import (  # noqa: F401
    _KNOWN_CONFIG_SECTIONS,
    _OBSERVED_DEGRADED_SECTIONS,
    CONFIG_RESERVED_TOP_KEYS,
    DEGRADED_TAILSCALE,
    DEGRADED_WHOLE_CONFIG,
    _coerced_section,
    _deep_merge,
    _fail_closed_project_skills_config,
    _mark_file_degraded,
    _subtract_overlay,
    degraded_config_files,
    reset_degraded_observations,
    tailnet_effective_allowed_logins,
    tailnet_identity_unknown,
)
from kiro_crew.config.section_builders import (  # noqa: F401
    _CU_MAX_SCREENSHOT_MAX_PX,
    _CU_MAX_TEXT_LIMIT,
    _CU_MAX_TREE_DEPTH,
    _CU_MAX_TREE_NODES,
    _CU_MIN_SCREENSHOT_MAX_PX,
    _DEFAULT_CONNECT_TIMEOUT,
    _DEFAULT_MINT_TIMEOUT,
    _STT_IDLE_EVICT_SECS_MAX,
    _STT_IDLE_EVICT_SECS_MIN,
    _STT_INTERVAL_MS_MAX,
    _STT_MAX_TIMEOUT_SECS,
    _STT_MIN_PARTIAL_INTERVAL_MS,
    _STT_MIN_SILENCE_MS,
    _STT_MIN_TIMEOUT_SECS,
    _build_computer_use_config,
    _build_cron_history_config,
    _build_discord_config,
    _build_feishu_config,
    _build_imessage_config,
    _build_instances_config,
    _build_knowledge_config,
    _build_mcp_config,
    _build_mcp_gateway_config,
    _build_memory_config,
    _build_messaging_config,
    _build_monitoring_config,
    _build_power_config,
    _build_publish_config,
    _build_session_summary_config,
    _build_skills_config,
    _build_slack_config,
    _build_stt_config,
    _build_taskrunner_config,
    _build_teams_config,
    _build_telegram_config,
    _build_tunnel_config,
    _build_wakatime_config,
    _build_watchdog_config,
    _build_webex_config,
    _build_wecom_config,
    _build_weixin_config,
    _build_whatsapp_config,
    _build_work_ledger_config,
    coerce_runtime_ceiling,
)
from kiro_crew.config.sections import (  # noqa: F401
    _BOT_NAME_MAX,
    _COLOR_HEX_RE,
    _CONNECT_TIMEOUT_CEILING,
    _DEFAULT_BEACON_ENDPOINT,
    _DEFAULT_CHAT_TURN_TIMEOUT_SECS,
    _GITLAB_HOST_NAME_RE,
    _MANAGED_SERVICE_ENV,
    _MAX_RECOVERY_CEILING,
    _MINT_TIMEOUT_CEILING,
    _MINT_TIMEOUT_FLOOR,
    _RECOVER_BACKOFF_CEILING,
    _RETIRED_STT_PROVIDERS,
    _STT_CATALOG,
    _VALID_ACTIVATIONS,
    _VALID_CHANNEL_PREFIXES,
    _VALID_COMPLETION_KEEP,
    _VALID_JAIL_MODES,
    _VALID_STT_MODELS,
    _VALID_STT_PROVIDERS,
    _WARM_SET_CAP_AUTO,
    _WARNED_RESOURCE_LIMIT_KEYS,
    _WARNED_STT_PROVIDERS,
    _WHATSAPP_GROUP_COOLDOWN_DEFAULT,
    _WHATSAPP_GROUP_MODES,
    _YOLO_DURATION_DEFAULT,
    _YOLO_DURATION_SECS,
    ACTIVATION_ALWAYS,
    ACTIVATION_MENTION,
    ACTIVATION_OBSERVE,
    ACTIVATION_OFF,
    ACTIVATION_REVIEW,
    APPROVAL_TURN_MARGIN_SECS,
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    BACKGROUND_WORKER_AGENTS,
    CHAT_ENTRY_CACHE_BYTES_DEFAULT,
    CHAT_ENTRY_CACHE_BYTES_MAX,
    CHAT_ENTRY_CACHE_BYTES_MIN,
    CHAT_ENTRY_CACHE_ENTRIES_DEFAULT,
    CHAT_ENTRY_CACHE_ENTRIES_MAX,
    CHAT_ENTRY_CACHE_ENTRIES_MIN,
    CHAT_TURN_TIMEOUT_MAX,
    CHAT_TURN_TIMEOUT_MIN,
    COMPLETION_KEEP_CHARS_MAX,
    COMPLETION_KEEP_CHARS_MIN,
    CONTEXT_WARN_MARGIN_PCT,
    DEDUP_EVERY_N_SWEEPS_MAX,
    DEFAULT_AUTO_INGEST_ARTIFACT_KINDS,
    DEFAULT_AUTOCOMPACT_PCT,
    DEFAULT_CWD_ALLOWED_ROOTS,
    DEFAULT_MAX_PARALLEL_STEPS,
    DEFAULT_MODEL,
    DEFAULT_POOL_SIZE,
    DEFAULT_SESSION_TIMEOUT,
    EFFORT_LEVELS,
    EMBED_RATE_LIMIT_MAX,
    EXTRACTION_POOL_SIZE_MAX,
    EXTRACTION_POOL_SIZE_MIN,
    FOLDER_INGEST_CHUNK_BUDGET_MAX,
    FORWARD_DECLARED_ENV_DEFAULT,
    IMESSAGE_SERVICES,
    IMPORT_CHUNK_BUDGET_MAX,
    JAIL_MODE_AUTO,
    JAIL_MODE_OFF,
    JAIL_MODE_ON,
    LOOP_STALL_EXIT_AFTER_DEFAULT,
    LOOP_STALL_EXIT_AFTER_MANAGED_DEFAULT,
    LOOP_STALL_EXIT_AFTER_MAX,
    LOOP_STALL_EXIT_AFTER_MIN,
    MAX_SUBAGENTS_FIXED_FLOOR,
    MCP_PROBE_TIMEOUT_MAX,
    MCP_PROBE_TIMEOUT_MIN,
    POOL_SIZE_MAX,
    POOL_TTL_SECS_MAX,
    POOL_TTL_SECS_MIN,
    RECENT_TINT_COUNT_MAX,
    RECENT_TINT_COUNT_MIN,
    ROLE_MODEL_KEYS,
    SESSION_FOLDER_NAME_MAX,
    SESSION_START_TIMEOUT_MAX,
    SESSION_START_TIMEOUT_MIN,
    SESSION_TIMEOUT_MAX,
    SESSION_TIMEOUT_MIN,
    SOFT_STOP_BUDGET_MAX,
    SOFT_STOP_BUDGET_MIN,
    STT_PROVIDER_LOCAL,
    SUBAGENT_AUTO_MAX_CEILING,
    SUBAGENT_MAX_TURNS_CEILING,
    SWEEP_CHUNK_BUDGET_MAX,
    TELEGRAM_ACTIVATIONS,
    THRESHOLD_PCT_MAX,
    THRESHOLD_PCT_MIN,
    TOOL_APPROVAL_TIMEOUT_MAX,
    TOOL_APPROVAL_TIMEOUT_MIN,
    YOLO_UNTIL_SHUTDOWN,
    AgentConfig,
    ChannelConfig,
    ComputerUseConfig,
    CronHistoryConfig,
    DashboardConfig,
    DecisionsConfig,
    DiscordConfig,
    ExternalRegistryConfig,
    FeishuConfig,
    HeartbeatConfig,
    IMessageConfig,
    InstancesConfig,
    JiraAuthEntry,
    KiroCrewAgentConfig,
    KnowledgeConfig,
    McpConfig,
    McpGatewayConfig,
    MemoryConfig,
    MemoryStoreConfig,
    MessagingConfig,
    MonitoringConfig,
    PublishConfig,
    ResolvedBindings,
    ResourceLimitsConfig,
    SessionConfig,
    SessionSummaryConfig,
    SkillsConfig,
    SlackConfig,
    SttConfig,
    TailscaleConfig,
    TaskRunnerConfig,
    TeamsConfig,
    TelegramAccountConfig,
    TelegramConfig,
    TelemetryConfig,
    TunnelConfig,
    WakaTimeConfig,
    WatchdogConfig,
    WebexConfig,
    WeComConfig,
    WeixinConfig,
    WhatsAppConfig,
    WorkspaceConfig,
    _archive_retention_days,
    _clamp_pct,
    _coerce_embedding_provider,
    _coerce_gitlab_hosts,
    _coerce_int,
    _coerce_int_ids,
    _coerce_jira_hosts,
    _coerce_opaque_str_ids,
    _coerce_session_folder,
    _coerce_str_ids,
    _coerce_whatsapp_groups,
    _limit_int,
    _meta,
    _migrate_workspaces,
    _normalize_acp_backend,
    _normalize_jail,
    _normalize_threshold_pair,
    _normalize_yolo_duration,
    _parse_telegram_accounts,
    _port_or_unset,
    _read_auto_add_documents,
    _read_skip_permissions,
    _resolve_stt_model,
    _resolve_stub_overrides,
    _resolve_stub_roster,
    _resolve_stub_servers,
    _safe_bool,
    _safe_color,
    _safe_dict,
    _safe_float,
    _safe_int,
    _safe_list,
    _safe_nonnegative_int,
    _sanitize_bot_name,
    _tailscale_config_from,
    _threshold_pct,
    _validate_activation,
    _validate_telegram_activation,
    _validate_tracking_channels,
    _validated_completion_keep,
    _validated_stt_model,
    _validated_stt_provider,
    coerce_effort,
    coerce_fallback_model,
    coerce_role_efforts,
    coerce_role_models,
    normalize_agent_model,
    resolve_memory_store_config,
    resolve_selected_backend,
    yolo_duration_to_secs,
)

# Superseded-default registry. Leaf module: stdlib only, so importing it
# here creates no cycle. The same module owns the one-shot adoption of the
# entries that opt into it; ``config.migration`` applies the drift readers, and
# the loader keeps them bound, with ``record_adoptions`` as the ledger writer
# its write-back passes on.
from kiro_crew.config.superseded_defaults import (  # noqa: F401
    SupersededDefault,
    auto_adoptable,
    drift_summary,
    drop_drifted_keys,
    record_adoptions,
    stored_value_or_none,
    superseded_default_drift,
)

# Schema validation + the validated-data cache live in ``config.validation``.
# Re-exported here for backward compatibility — callers and tests still
# reference these as ``kiro_crew.config.loader.X`` (e.g. the cache tests patch
# ``kiro_crew.config.loader._validate_config_data``). ``validate_config_data``
# is aliased to the historical private name ``_validate_config_data``. The cache
# fingerprint (``_config_fingerprint``) lives in ``config.loader_document``.
from kiro_crew.config.validation import (  # noqa: F401
    _CONFIG_CACHE,
    _CONFIG_CACHE_LOCK,
    _HAS_JSONSCHEMA,
    _actual_type_name,
    _apply_field_default,
    _dot_path_from_json_path,
    _get_help_text,
    _is_deprecated_path,
    _is_sensitive_path,
    _lookup_schema_node,
    _mask_value,
)
from kiro_crew.config.validation import validate_config_data as _validate_config_data  # noqa: F401
from kiro_crew.constants import (  # noqa: F401 - loader namespace compatibility
    DEFAULT_SPAWN_MIN_MEMORY_GB,
    DEFAULT_SUBAGENT_COST_GB,
    DEFAULT_SUBAGENT_MAX_TURNS,
    DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS,
    SUBAGENT_TIMEOUT_MAX,
    SUBAGENT_TIMEOUT_MIN,
    SUBAGENT_TIMEOUT_SECS,
)
from kiro_crew.effort import is_valid_effort, model_supports_effort  # noqa: F401
from kiro_crew.mcp_gateway.rewriter import default_overlay_dir, default_socket_path  # noqa: F401

# The memory-store shape rule, applied to store NAMES at load. Leaf module:
# stdlib only at module scope (it imports this one lazily), so no cycle.
from kiro_crew.memory_stores import (  # noqa: F401
    DEFAULT_MEMORY_STORE,
    memory_store_name_defect,
)
from kiro_crew.owner_only_files import GROUP_OTHER_BITS, is_owner_only_target  # noqa: F401
from kiro_crew.session_start_sizing import AUTO as _SESSION_START_AUTO  # noqa: F401
from kiro_crew.session_start_sizing import is_auto as _session_start_is_auto  # noqa: F401
from kiro_crew.stt.limits import (  # noqa: F401
    DEFAULT_IDLE_EVICT_SECS as _STT_DEFAULT_IDLE_EVICT_SECS,
)
from kiro_crew.stt.limits import (  # noqa: F401
    DEFAULT_PARTIAL_INTERVAL_MS as _STT_DEFAULT_PARTIAL_INTERVAL_MS,
)
from kiro_crew.stt.limits import DEFAULT_SILENCE_MS as _STT_DEFAULT_SILENCE_MS  # noqa: F401
from kiro_crew.stt.limits import DEFAULT_TIMEOUT_SECS as _STT_DEFAULT_TIMEOUT_SECS  # noqa: F401
from kiro_crew.stt.models import DEFAULT_MODEL as _STT_DEFAULT_MODEL  # noqa: F401
from kiro_crew.user_json import strip_utf8_bom  # noqa: F401

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Effective-model resolution: the model a session, an app or a crew agent runs.
# ---------------------------------------------------------------------------
def app_model_pin(config: KiroCrewConfig, app_name: str | None) -> str:
    """The user's model for the app *app_name* (``apps.<name>.model``), or ``""``.

    ``""`` and ``"auto"`` both INHERIT, as for every other model tier. Total: a
    config without the table, an unknown app, or a malformed entry reads ``""``.
    """
    if not app_name:
        return ""
    apps = getattr(config, "apps", None)
    entry = apps.get(app_name) if isinstance(apps, dict) else None
    return normalize_agent_model(getattr(entry, "model", ""))


def _agent_tier_model(
    config: KiroCrewConfig,
    agent_name: str | None,
    in_scope: Callable[[str], str],
    *,
    selection_kind: str = "",
    template: str | None = None,
) -> str:
    """The model the agent itself pins: its crew's ``model``, else its template's.

    Tiers 1 and 2 of :func:`resolve_effective_model`. ``""`` when neither pins a
    model the active harness can claim. A *template* the caller names is the
    template tier itself, with no crew above it.
    """
    if template is not None:
        kiro_agent, model_pin = template, ""
    else:
        _, kiro_agent, model_pin = resolve_agent_identity(
            config, agent_name, selection_kind=selection_kind
        )
    if in_scope(model_pin):
        return model_pin
    if kiro_agent and kiro_agent != "kirocrew":
        pinned = normalize_agent_model(config._resolve_named_agent_model(kiro_agent))
        if in_scope(pinned):
            return pinned
    return ""


def _model_scope_filter(config: KiroCrewConfig, backend: str | None = None) -> Callable[[str], str]:
    namespace = capabilities_for(
        config.agent.acp_backend if backend is None else backend
    ).model_id_namespace

    def _in_scope(pin: str) -> str:
        return model_scope.scoped_pin(
            pin, namespace, source="resolve_effective_model", log_level=logging.DEBUG
        )

    return _in_scope


def resolve_app_spawn_model(config: KiroCrewConfig, app_name: str, agent_name: str) -> str:
    """The model an app's background spawn of *agent_name* runs on, or ``""``.

    The app's own ``apps.<name>.model`` when the user set one, unless the spawned
    template itself pins a model, which wins as it does for an app session. That
    pin is the spawn's whole agent tier: an app spawn is a template spawn, which
    allocates with ``crew_agent=""``, so its session (``session._session_model``)
    never reads a crew's pin, not even one of a crew that shares the template's
    name. ``""`` leaves the spawn exactly as it is without the setting.
    """
    in_scope = _model_scope_filter(config)
    app_pin = in_scope(app_model_pin(config, app_name))
    if not app_pin:
        return ""
    if agent_name and agent_name != "kirocrew":
        if in_scope(normalize_agent_model(config._resolve_named_agent_model(agent_name))):
            return ""
    return app_pin


def resolve_effective_model(
    config: KiroCrewConfig,
    agent_name: str | None = None,
    *,
    selection_kind: str = "",
    app_name: str = "",
    backend: str | None = None,
    template: str | None = None,
) -> str:
    """Return the model a new session on *agent_name* would start with.

    Single source of truth for the default-model precedence, so the display
    path (the dashboard's model chip) and the execution path
    (``create_provider_factory._acp``) cannot drift apart. Tiers, highest first:

    1. the KiroCrew agent's own ``model``
    2. the bound kiro agent's pinned ``model`` (skipped for the built-in
       ``kirocrew`` agent, which tracks the global by design)
    3. for a session an app owns (*app_name*), the user's ``apps.<name>.model``
    4. the global ``agent.model`` default
    5. the installed ``kirocrew.json`` / bundled ``defaults.json`` model

    A per-session pick outranks all of these and is NOT considered here — the
    caller holds it. Returns ``""`` when every tier defers, meaning the backend
    picks (kiro-cli's own ``chat.defaultModel``).

    Every tier is scoped to the namespace of the backend that would run the
    session, the same gate ``create_provider_factory`` applies to the value it
    sends. Without that the chip and the wire disagree in the one case the scope
    exists for: after a backend switch the chip kept naming the previous
    harness's pin while every turn ran on the new harness's default. A tier whose
    pin the active harness cannot claim is skipped, so the next tier down answers
    -- exactly as if that tier were unset. *backend* names that backend when it
    is not the configured ``agent.acp_backend`` (a member DM routed to
    ``agent.member_acp_backend``, a sub-agent spawned with an explicit backend);
    ``None`` means the configured one.

    *template* is for a caller that already holds the template the session will
    launch and no crew (the session layer, which hands that name to the provider
    factory verbatim): it is the tier-2 agent, read directly rather than looked up
    from *agent_name*, so the answer is the template the backend really starts.
    ``""`` names no template, leaving tier 2 empty.
    """
    _in_scope = _model_scope_filter(config, backend)

    agent_pin = _agent_tier_model(
        config, agent_name, _in_scope, selection_kind=selection_kind, template=template
    )
    if agent_pin:
        return agent_pin
    app_pin = _in_scope(app_model_pin(config, app_name))
    if app_pin:
        return app_pin

    configured = normalize_agent_model(config.agent.model)
    if _in_scope(configured):
        return configured
    # agent.model is "auto"/unset: fall through to the installed agent file the
    # factory would read, so the chip shows what will actually be used.
    return _in_scope(normalize_agent_model(config._resolve_agent_model()))


def validate_kiro_agent_references(
    config: KiroCrewConfig,
    installed_agents: list[str],
) -> None:
    """Cross-reference kiro_agent values against installed agents.

    Logs warnings for unresolved references. Never raises.
    """
    installed_names = set(installed_agents)
    for mc_name, mc_agent in config.agents.items():
        if mc_agent.kiro_agent and mc_agent.kiro_agent not in installed_names:
            logger.warning(
                "KiroCrew agent '%s' references kiro agent '%s' " "which is not installed",
                mc_name,
                mc_agent.kiro_agent,
            )


# ---------------------------------------------------------------------------
# Provider factory seam.
# ---------------------------------------------------------------------------
def build_provider_factory(cfg: "KiroCrewConfig") -> Callable:
    """Return the LLM-provider factory for *cfg*, via the platform seam.

    Routes through ``current_context().providers.create_factory(cfg)`` (the CPP
    ``ProviderRegistry`` extension point) instead of calling
    ``cfg.create_provider_factory()`` directly, so an edition can supply an
    alternate provider factory (e.g. registering an ACP backend the core does not
    ship, through the ``ACP_BACKEND_*`` seam).  The ``Default`` ProviderRegistry
    returns exactly ``cfg.create_provider_factory()``, so the public edition is
    behaviorally identical to calling it directly.

    Fail-closed: a :class:`PlatformCompositionError` (a non-standalone host that
    could not compose its companion) propagates.  Any other transient lookup
    failure degrades to ``cfg.create_provider_factory()`` so an unbooted /
    standalone call site never breaks — it just gets the public factory.

    The fallback is passed as ``fallback_factory`` (a lazy thunk), NOT eagerly:
    ``cfg.create_provider_factory()`` is built ONLY on the degrade path, so the
    standalone happy path builds the factory exactly once (the Default
    ``ProviderRegistry`` already returns ``cfg.create_provider_factory()``, so an
    eager fallback would build it a second time on every session/reload).  A
    failure INSIDE ``cfg.create_provider_factory()`` itself is handled by
    ``safe_context_call`` (which guards the factory call) rather than escaping
    uncaught; with no eager ``fallback`` here there is no usable factory, so a
    composition error propagates (fail-closed) and any other error re-raises —
    a corrupt-config failure surfaces at the factory site, it is not swallowed.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    return safe_context_call(
        lambda: current_context().providers.create_factory(cfg),
        fallback_factory=lambda: cfg.create_provider_factory(),
        log_message="providers.create_factory failed; using cfg.create_provider_factory()",
    )


# ---------------------------------------------------------------------------
# The facade over the loader_* owners
# ---------------------------------------------------------------------------
# ``kiro_crew.config.loader`` is the import path and the patch seam; the code it
# re-exports lives in the owner modules below, one responsibility each. The owners
# import each other's names, lowest layer first, so a name a test patches here is
# read in more than one module. ``_facade_mirror`` keeps that patch whole: a write or
# delete of a name through this module lands in every owner that binds it (and here,
# when this module binds it too), and a name this module does not bind is read from
# the first owner that does. No owner imports this module.
#: Owner modules, lowest layer first: the first one binding a name answers a read of it.
_OWNERS = (
    "kiro_crew.config.loader_credentials",
    "kiro_crew.config.loader_locations",
    "kiro_crew.config.loader_document",
    "kiro_crew.config.loader_builders",
    "kiro_crew.config.loader_snapshots",
    "kiro_crew.config.loader_agents",
    "kiro_crew.config.loader_writeback",
    "kiro_crew.config.loader_config",
    "kiro_crew.config.loader_pipeline",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)

#: Owner-bound name this module does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)

# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

# Derived, not listed: what ``from kiro_crew.config.loader import *`` binds is every
# public name of the family, as it was when the family was one module.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name this module forwards, for type checkers
    from kiro_crew.config.loader_agents import (  # noqa: F401
        _crew_autocompact_pct,
        _resolve_agent_selection,
        _workspace_name_for_dir,
        crew_autocompact_pct,
        resolve_agent_bindings,
        resolve_crew_identity,
    )
    from kiro_crew.config.loader_builders import (  # noqa: F401
        _DEFAULT_MEMORY_MODES,
        _HARNESS_CHOICE_KEYS,
        _JSON_TYPE_LABELS,
        _MEMBER_HARNESS_KEY,
        _SECURITY_BOUNDED_FIELDS,
        _build_agent_config,
        _build_dashboard_config,
        _build_session_config,
        _build_telemetry_config,
        _clamp_compact_wait_secs,
        _clamp_security_bounds,
        _crew_mode_agent_or_default,
        _default_memory_mode_from,
        _default_session_folder_or_empty,
        _folder_sort_from,
        _jira_auth_entry,
        _load_idle_exempt_keys,
        _log_config_clamp_event,
        _member_remap_logged,
        _note_member_remap,
        _read_backend_choice,
        _registry_entry,
        _restore_stored_harness_fields,
        _restore_stored_stt_provider,
        _session_folder_root_or_empty,
        _session_sort_from,
        _session_start_concurrency,
        _stored_agent_dict,
        _subagent_timeout_from,
        _title_refresh_every_turns,
        _translucent_panels_from,
        consume_managed_service_launch_environment,
        launched_as_managed_service,
        resolve_loop_stall_exit_after,
    )
    from kiro_crew.config.loader_credentials import (  # noqa: F401
        _DEFAULT_PORT,
        _JIRA_TOKEN_RE,
        _KIRO_API_KEY_ONLY,
        _WIDE_BOMS,
        CRED_AZURE_DEVOPS_EXT_PAT,
        CRED_BITBUCKET_API_TOKEN,
        CRED_BITBUCKET_EMAIL,
        CRED_DISCORD_BOT_TOKEN,
        CRED_FEISHU_APP_ID,
        CRED_FEISHU_APP_SECRET,
        CRED_JIRA_API_TOKEN,
        CRED_KIRO_API_KEY,
        CRED_MICROSOFT_APP_ID,
        CRED_MICROSOFT_APP_PASSWORD,
        CRED_MICROSOFT_APP_TENANT_ID,
        CRED_OWNER_ID,
        CRED_SLACK_APP_TOKEN,
        CRED_SLACK_BOT_TOKEN,
        CRED_TELEGRAM_BOT_TOKEN,
        CRED_WAKATIME_API_KEY,
        CRED_WEBEX_BOT_TOKEN,
        CRED_WECOM_BOT_ID,
        CRED_WECOM_SECRET,
        CRED_WEIXIN_TOKEN,
        CREDENTIAL_KEYS,
        DASHBOARD_PORT,
        MANAGED_VAULT_FIXED_CONSUMERS,
        VAULT_CHANNEL_CREDENTIAL_KEYS,
        EnvFileWideEncodingError,
        _is_env_credential_key,
        _shadow_checked_env_keys,
        _warn_unresolved_env_credentials,
        _warned_env_keys,
        _warned_undecodable_env,
        _warned_unresolved_env_refs,
        _warned_unresolved_env_refs_lock,
        _wide_bom,
        _wide_family,
        decode_env_bytes,
        env_bom_prefix,
        env_path,
        inject_kiro_cli_api_key,
        jira_global_token_applicable,
        jira_host_token_name,
        normalize_jira_host,
        read_env_file,
        read_env_file_credential,
        read_env_text,
        resolve_env_credential,
        resolve_env_credentials,
        strip_kiro_cli_api_key,
        undecodable_env_message,
        vault_channel_env_value,
        warn_undecodable_env,
    )
    from kiro_crew.config.loader_document import (  # noqa: F401
        _ABSENT_AGENT_KEY,
        _SIDECAR_BASE_SHADOW,
        _SIDECAR_BASE_UNREADABLE,
        _SIDECAR_OVERLAY_UNREADABLE,
        ConfigReadError,
        ConfigWriteRefused,
        _agent_key_declared,
        _agent_key_on_disk,
        _aws_project_profiles_on_disk,
        _backend_choice_declared,
        _bind_content_digest,
        _cached_validated_data,
        _config_fingerprint,
        _config_write_lock,
        _content_digest_of,
        _deepseek_env_on_disk,
        _invalidate_config_cache,
        _lock_target,
        _notify_live_watch,
        _raw_agent_section,
        _raw_config,
        _raw_stt_provider,
        _refuse_agent_env_entries,
        _refuse_aws_project_profiles,
        _refuse_deepseek_env_plaintext,
        _refuse_unpublishable,
        _shadowed_base_sections,
        _store_validated_data,
        _stored_agent_value,
        _type_names,
        acp_backend_declared,
        coerce_config_field,
        coerce_dict_section,
        config_content_stamp,
        member_acp_backend_declared,
        read_config_for_update,
        refresh_config_meta_stamp,
        stamp_config_meta,
        unsandboxed_exec_declared,
        unsandboxed_exec_platform_default,
        update_config_locked,
        write_config_atomically,
    )
    from kiro_crew.config.loader_locations import (  # noqa: F401
        WorkspaceDirUnusable,
        _first_non_directory_on_path,
        _PinnedCreateRefusal,
        _resolve_workspace_root,
        _session_work_dir,
        _workspace_blocker_message,
        _workspace_dir_file,
        aws_consent_path,
        computer_use_state_path,
        config_local_path,
        config_path,
        credential_redaction_path,
        decisions_consent_path,
        denied_commands_path,
        materialize_workspace_dir,
        normalize_workspace_path,
        oauth_endpoints_path,
        outbox_dir,
        overlay_pins,
        read_config_text,
        read_local_secret,
        registry_trust_path,
        resolve_agent_config_path,
        ssh_auth_sock_consent_path,
        workspace_dir_from_entry,
        workspace_root,
    )
    from kiro_crew.config.loader_pipeline import (  # noqa: F401
        _TOP_LEVEL_DEFAULTS,
        ConfigDocument,
        _parse_app_settings,
        build_config,
        default_project_dir,
        load_config_with_content_stamp,
        load_loop_stall_exit_after,
        persist_write_back,
        read_config_document,
        workspace_dir_for,
    )
    from kiro_crew.config.loader_snapshots import (  # noqa: F401
        _CONFIG_AGENT_ALIAS_SNAPSHOT,
        _CONFIG_AUTOCOMPACT_ISSUED,
        _CONFIG_AUTOCOMPACT_LOCK,
        _CONFIG_AUTOCOMPACT_PCT,
        _CONFIG_AUTOCOMPACT_TICKET,
        _CONFIG_TIMEZONE,
        _CONFIG_TIMEZONE_LOCK,
        _CONFIG_TIMEZONE_TICKET,
        _EDITION_LOOKUP,
        _MATERIALIZED_AGENTS,
        _MATERIALIZED_AGENTS_GENERATION,
        _MATERIALIZED_AGENTS_LOCK,
        _MATERIALIZED_AGENTS_READY,
        _MATERIALIZED_COMPLETE,
        _MATERIALIZED_LAST_DROPPED,
        _MATERIALIZED_MATE_ONLY,
        _MATERIALIZED_REFRESH_APPLIED,
        _MATERIALIZED_REFRESH_ISSUED,
        _MATERIALIZED_SEEN,
        _MATERIALIZED_SEEN_MAX,
        _MATERIALIZED_SEEN_NAME_MAX_CHARS,
        _MATERIALIZED_STEMS,
        _SCAN_STATE,
        DEFAULT_KIRO_TEMPLATE,
        KIRO_CLI_BUILTIN_AGENTS,
        _alias_workspace_dir,
        _dangling_template,
        _edition_agent_names,
        _locked_workspace_dir,
        _log_default_agent_reset,
        _materialized_kiro_agent,
        _project_declares_agent,
        _project_scope_excludes,
        _read_hardened_agent_spec,
        _remember_seen,
        _removal_evidence,
        _report_dropped,
        _scan_materialized_agents,
        _scan_materialized_index,
        _workspace_declares,
        agent_alias_snapshot,
        default_template,
        dispatch_kiro_agent,
        materialized_snapshot_complete,
        member_template_id,
        next_config_load_ticket,
        publish_agent_alias_snapshot,
        publish_autocompact_pct,
        publish_config_timezone,
        publish_mate_agents,
        publish_materialized_agents,
        published_autocompact_pct,
        published_config_timezone,
        refresh_materialized_agents,
        reset_dangling_default_agent,
        resolve_effective_agent,
        schedule_materialized_agents_refresh,
    )
    from kiro_crew.config.loader_writeback import (  # noqa: F401
        _apply_document_migrations,
        _inside_data_home,
        _overlay_kiro_agent,
        _persist_config_migration,
        _write_migration_backup,
    )
