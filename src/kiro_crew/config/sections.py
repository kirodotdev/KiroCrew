"""Configuration section DTOs, defaults, and field-level coercion.

This module is the import path and the patch seam for the section DTOs other specs
and tests anchor here; their code lives in the ``sections_*`` owner modules beside
it, one responsibility each (``docs/system-specs/modules/config.md`` lists them).
It also re-exports every name of the domain owners it composes: ``config.fields``
(field metadata and primitive coercion), ``config.memory_sections``,
``config.integration_sections`` and ``config.service_sections``.

The loader imports and re-exports this module's names as its compatibility
facade.  Keep this module one-way: it must not import the loader, schema, or
validation modules, and the owners it re-exports never import it.
"""

from __future__ import annotations

import importlib as _importlib
import logging
import math
import os as _os
import re as _re
import sys as _sys
import unicodedata as _unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, overload
from urllib.parse import urlsplit as _urlsplit

from kiro_crew import _facade_mirror, model_registry

# Leaf module (stdlib only) owning "which ACP backend can this build serve": the
# registry an edition extends at boot. Importable at module scope precisely because
# it does NOT reach ``kiro_crew.acp`` — the package init (client + runtime) imports
# config models, which is the cycle the old ``acp.types`` import had to defer for.
#
# The one gate stays inside ``_normalize_acp_backend`` on the way out of
# config.json. Only what it reads changed: the registry, instead of a frozen
# literal.
from kiro_crew.acp_backends import DEFAULT_ACP_BACKEND, resolve_selected_backend
from kiro_crew.appearance_packs import safe_pack_id as _safe_pack_id
from kiro_crew.config.fields import (  # noqa: F401
    _COLOR_HEX_RE,
    _coerce_int,
    _declared_fields,
    _meta,
    _port_or_unset,
    _safe_bool,
    _safe_color,
    _safe_dict,
    _safe_float,
    _safe_int,
    _safe_list,
    _safe_nonnegative_int,
    field_default,
)
from kiro_crew.config.integration_sections import (  # noqa: F401
    _CONNECT_TIMEOUT_CEILING,
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
    _MAX_RECOVERY_CEILING,
    _MINT_TIMEOUT_CEILING,
    _MINT_TIMEOUT_FLOOR,
    _RECOVER_BACKOFF_CEILING,
    _WARM_SET_CAP_AUTO,
    FORWARD_DECLARED_ENV_DEFAULT,
    MANAGED_STUB_FOLLOWERS,
    AppSettingsConfig,
    AwsConfig,
    ComputerUseConfig,
    ExternalRegistryConfig,
    InstancesConfig,
    McpConfig,
    McpGatewayConfig,
    PublishConfig,
    TunnelConfig,
    _resolve_stub_base,
    _resolve_stub_overrides,
    _resolve_stub_roster,
    _resolve_stub_servers,
)
from kiro_crew.config.memory_sections import (  # noqa: F401
    DEFAULT_AUTO_INGEST_ARTIFACT_KINDS,
    KnowledgeConfig,
    MemoryConfig,
    MemoryStoreConfig,
    SessionSummaryConfig,
    SkillsConfig,
    _coerce_embedding_provider,
    _read_auto_add_documents,
    resolve_memory_store_config,
)
from kiro_crew.config.resolution import _OBSERVED_DEGRADED_SECTIONS, DEGRADED_TAILSCALE
from kiro_crew.config.sections_agent import (  # noqa: F401
    _YOLO_DURATION_DEFAULT,
    AgentConfig,
    _read_skip_permissions,
    coerce_fallback_model,
    yolo_duration_to_secs,
)
from kiro_crew.config.sections_agent_env import (  # noqa: F401
    AGENT_ENV_CREDENTIAL_CLASS,
    AGENT_ENV_DATA_ONLY_NAMES,
    AGENT_ENV_DENIED_NAMES,
    AGENT_ENV_DENIED_PREFIXES,
    AGENT_ENV_DENIED_SUFFIXES,
    AGENT_ENV_NAME_GRAMMAR,
    CHILD_ENV_DEFAULT_MAX,
    CHILD_ENV_DEFAULT_MIN,
    CHILD_ENV_DEFAULT_NAMES,
    _agent_env_denied,
    _agent_env_entry_refusal,
    _agent_env_home_override_names,
    agent_env_refusal,
    agent_env_scrub_removes,
    agent_env_write_refusals,
    coerce_agent_env,
    coerce_child_env_defaults,
    coerce_deepseek_env,
    deepseek_env_plaintext_keys,
)
from kiro_crew.config.sections_agent_fields import (  # noqa: F401
    _AVATAR_EXPRESSION_AXES,
    _AVATAR_FILE_PIN_RE,
    _AVATAR_GHOST_BOOL_TRAITS,
    _AVATAR_GHOST_STR_TRAITS,
    _AVATAR_IMAGE_EXTS,
    _AVATAR_MOTIONS,
    _AVATAR_POSE_RE,
    _AVATAR_SOUNDS,
    _AVATAR_STATES,
    _AVATAR_TRAIT_MAX_LEN,
    _BOT_NAME_CATEGORIES,
    _BOT_NAME_MAX,
    _BOT_NAME_SEPARATORS,
    _VALID_COMPLETION_KEEP,
    _VALID_JAIL_MODES,
    _YOLO_DURATION_SECS,
    BACKGROUND_WORKER_AGENTS,
    DEFAULT_CWD_ALLOWED_ROOTS,
    JAIL_MODE_AUTO,
    JAIL_MODE_OFF,
    JAIL_MODE_ON,
    KAS_BUNDLE_DEFAULT_URL,
    MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE,
    MEMBER_APPROVAL_MODES,
    MODEL_CONTEXT_TOKENS_MAX,
    MODEL_CONTEXT_TOKENS_MAX_ENTRIES,
    MODEL_CONTEXT_TOKENS_MIN,
    ROLE_MODEL_KEYS,
    YOLO_UNTIL_SHUTDOWN,
    _bot_name_char,
    _normalize_acp_backend,
    _normalize_jail,
    _normalize_yolo_duration,
    _safe_avatar,
    _safe_expressions,
    _safe_motions,
    _safe_sounds,
    _sanitize_bot_name,
    _validated_completion_keep,
    coerce_effort,
    coerce_kas_bundle_url,
    coerce_member_approval_mode,
    coerce_model_context_tokens,
    coerce_refusal_fallback_model,
    coerce_role_efforts,
    coerce_role_models,
    normalize_agent_model,
    safe_imported,
    worker_session_defaults,
)
from kiro_crew.config.sections_channels import (  # noqa: F401
    _WHATSAPP_GROUP_COOLDOWN_DEFAULT,
    _WHATSAPP_GROUP_MODES,
    IMESSAGE_SERVICES,
    SESSION_FOLDER_NAME_MAX,
    THRESHOLD_PCT_MAX,
    THRESHOLD_PCT_MIN,
    DiscordConfig,
    FeishuConfig,
    IMessageConfig,
    TeamsConfig,
    WebexConfig,
    WeComConfig,
    WeixinConfig,
    WhatsAppConfig,
    _clamp_pct,
    _coerce_int_ids,
    _coerce_opaque_str_ids,
    _coerce_session_folder,
    _coerce_str_ids,
    _coerce_whatsapp_groups,
    _normalize_threshold_pair,
    _threshold_pct,
)
from kiro_crew.config.sections_dashboard import (  # noqa: F401
    _MANAGED_SERVICE_ENV,
    CHAT_ENTRY_CACHE_BYTES_DEFAULT,
    CHAT_ENTRY_CACHE_BYTES_MAX,
    CHAT_ENTRY_CACHE_BYTES_MIN,
    CHAT_ENTRY_CACHE_ENTRIES_DEFAULT,
    CHAT_ENTRY_CACHE_ENTRIES_MAX,
    CHAT_ENTRY_CACHE_ENTRIES_MIN,
    EXTERNAL_TERMINAL_APPS,
    LEGACY_VERBOSITY_ALIASES,
    LINK_PATTERN_PATTERN_MAX_LEN,
    LINK_PATTERN_URL_MAX_LEN,
    LINK_PATTERNS_MAX,
    LOOP_STALL_EXIT_AFTER_DEFAULT,
    LOOP_STALL_EXIT_AFTER_MANAGED_DEFAULT,
    LOOP_STALL_EXIT_AFTER_MAX,
    LOOP_STALL_EXIT_AFTER_MIN,
    VERBOSITY_LEVELS,
    DashboardConfig,
    JiraAuthEntry,
    LinkPatternRule,
    normalize_verbosity,
)
from kiro_crew.config.sections_dashboard_hosts import (  # noqa: F401
    _BITBUCKET_CLOUD_HOSTS,
    _BROWSER_LOCAL_HOSTS,
    _GITHUB_PUBLIC_HOSTS,
    _GITLAB_HOST_NAME_RE,
    _coerce_bitbucket_server_hosts,
    _coerce_gerrit_hosts,
    _coerce_github_hosts,
    _coerce_gitlab_hosts,
    _coerce_jira_hosts,
    _coerce_link_patterns,
    coerce_browser_local_origins,
    link_pattern_url_ok,
)
from kiro_crew.config.sections_decisions import (  # noqa: F401
    DECISION_BUCKET_MAX,
    DECISION_BUCKET_MIN,
    DECISION_HISTORY_BUDGET_DEFAULT,
    DECISION_MODEL_ROUTE_DEFAULT,
    DECISION_MODEL_ROUTE_TIERS,
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DECISION_PROVIDER_MODEL_DEFAULT,
    JUDGE_PROVIDER_AUTO,
    JUDGE_PROVIDER_JEV,
    JUDGE_PROVIDER_LLM,
    JUDGE_PROVIDERS,
    DecisionProviderConfig,
    DecisionsConfig,
    NudgeWakeConfig,
    coerce_model_route,
)
from kiro_crew.config.sections_defaults import (  # noqa: F401
    _ABSENT,
    _T,
    CONTEXT_WARN_MARGIN_PCT,
    DEFAULT_AUTOCOMPACT_PCT,
    DEFAULT_MODEL,
    DEFAULT_POOL_SIZE,
    DEFAULT_RECONCILE_MAX_KILLS,
    DEFAULT_SESSION_TIMEOUT,
    DEFAULT_WATCHDOG_RSS_MAX_MB,
    SectionReader,
)
from kiro_crew.config.sections_limits import (  # noqa: F401
    _DEFAULT_CHAT_TURN_TIMEOUT_SECS,
    _WARNED_RESOURCE_LIMIT_KEYS,
    APPROVAL_TURN_MARGIN_SECS,
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    CHAT_TURN_TIMEOUT_MAX,
    CHAT_TURN_TIMEOUT_MIN,
    COMPACT_WAIT_SECS_MAX,
    COMPACT_WAIT_SECS_MIN,
    COMPLETION_KEEP_CHARS_MAX,
    COMPLETION_KEEP_CHARS_MIN,
    DEDUP_EVERY_N_SWEEPS_MAX,
    EMBED_RATE_LIMIT_MAX,
    EMPTY_RESPONSE_MAX_CONTINUES_MAX,
    EMPTY_RESPONSE_MAX_CONTINUES_MIN,
    EXTRACTION_POOL_SIZE_MAX,
    EXTRACTION_POOL_SIZE_MIN,
    FILE_CHANGES_KEEP_TURNS_MIN,
    FOLDER_INGEST_CHUNK_BUDGET_MAX,
    FOLDER_SORT_DEFAULT,
    FOLDER_SORT_MODES,
    IDLE_EXEMPT_KEY_MAX_CHARS,
    IDLE_EXEMPT_KEYS_MAX,
    IMPORT_CHUNK_BUDGET_MAX,
    MAX_SUBAGENTS_FIXED_FLOOR,
    MCP_PROBE_TIMEOUT_MAX,
    MCP_PROBE_TIMEOUT_MIN,
    POOL_SIZE_MAX,
    POOL_TTL_SECS_MAX,
    POOL_TTL_SECS_MIN,
    RECENT_TINT_COUNT_MAX,
    RECENT_TINT_COUNT_MIN,
    RECONCILE_MAX_KILLS_MAX,
    SESSION_SORT_MODES,
    SESSION_START_TIMEOUT_MAX,
    SESSION_START_TIMEOUT_MIN,
    SESSION_TIMEOUT_MAX,
    SESSION_TIMEOUT_MIN,
    SOFT_STOP_BUDGET_MAX,
    SOFT_STOP_BUDGET_MIN,
    SUBAGENT_AUTO_MAX_CEILING,
    SUBAGENT_MAX_TURNS_CEILING,
    SWEEP_CHUNK_BUDGET_MAX,
    TITLE_REFRESH_EVERY_TURNS_MAX,
    TITLE_REFRESH_EVERY_TURNS_MIN,
    TOOL_APPROVAL_TIMEOUT_MAX,
    TOOL_APPROVAL_TIMEOUT_MIN,
    UPLOAD_MAX_MB_MAX,
    UPLOAD_MAX_MB_MIN,
    AutocompactPctError,
    ResourceLimitsConfig,
    _limit_int,
    parse_autocompact_pct,
)
from kiro_crew.config.sections_session import (  # noqa: F401
    DEFAULT_KIRO_TEMPLATE,
    KiroCrewAgentConfig,
    ResolvedBindings,
    SessionConfig,
    SessionEnvironmentProfileConfig,
    SessionEnvironmentsConfig,
    WorkspaceConfig,
    _archive_retention_days,
    _migrate_workspaces,
)
from kiro_crew.config.sections_slack import (  # noqa: F401
    _VALID_ACTIVATIONS,
    _VALID_CHANNEL_PREFIXES,
    ACTIVATION_ALWAYS,
    ACTIVATION_MENTION,
    ACTIVATION_OBSERVE,
    ACTIVATION_OFF,
    ACTIVATION_REVIEW,
    ChannelConfig,
    SlackConfig,
    _validate_activation,
    _validate_tracking_channels,
)
from kiro_crew.config.sections_stt import (  # noqa: F401
    _DEFERRED_STT_PROVIDERS,
    _RETIRED_STT_PROVIDERS,
    _TRANSCRIBE_LANGUAGE_RE,
    _TRANSCRIBE_VOCABULARY_NAME_RE,
    _TRANSCRIBE_VOCABULARY_NOTICE_VALUE_MAX,
    _VALID_STT_MODELS,
    _VALID_STT_PROVIDERS,
    _WARNED_STT_PROVIDERS,
    STT_LANGUAGE_AUTO,
    STT_LANGUAGE_FALLBACK,
    STT_PROVIDER_LOCAL,
    STT_PROVIDER_OFF,
    TRANSCRIBE_LANGUAGES_MAX,
    TRANSCRIBE_LANGUAGES_MIN,
    TRANSCRIBE_VOCABULARY_NAME_MAX,
    SttConfig,
    _validated_stt_custom_path,
    _validated_stt_custom_sha256,
    _validated_stt_model,
    _validated_stt_provider,
    _validated_stt_threads,
    _validated_transcribe_languages,
    _validated_transcribe_vocabulary,
    default_stt_provider,
    stt_provider_is_coerced,
    stt_provider_resolution,
    transcribe_language_options,
    transcribe_vocabulary_name,
    valid_stt_providers,
    warn_deferred_stt_providers,
)
from kiro_crew.config.sections_tailnet import (  # noqa: F401
    TailscaleConfig,
    _tailscale_config_from,
    effective_pin_scope,
)
from kiro_crew.config.sections_telegram import (  # noqa: F401
    TELEGRAM_ACTIVATIONS,
    TelegramAccountConfig,
    TelegramConfig,
    _parse_telegram_accounts,
    _validate_telegram_activation,
)
from kiro_crew.config.sections_telemetry import (  # noqa: F401
    _DEFAULT_BEACON_ENDPOINT,
    TelemetryConfig,
    WakaTimeConfig,
)
from kiro_crew.config.service_sections import (  # noqa: F401
    DEFAULT_MAX_PARALLEL_STEPS,
    DEFAULT_RUNTIME_CEILING_SECS,
    MAX_RUNTIME_CEILING_SECS,
    CronHistoryConfig,
    HeartbeatConfig,
    MessagingConfig,
    MonitoringConfig,
    TaskRunnerConfig,
    WatchdogConfig,
)
from kiro_crew.constants import DEFAULT_SPAWN_MIN_MEMORY_GB as _DEFAULT_SPAWN_MIN_MEMORY_GB
from kiro_crew.constants import DEFAULT_SUBAGENT_COST_GB as _DEFAULT_SUBAGENT_COST_GB
from kiro_crew.constants import DEFAULT_SUBAGENT_MAX_TURNS as _DEFAULT_SUBAGENT_MAX_TURNS
from kiro_crew.constants import (
    DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS as _DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS,
)
from kiro_crew.constants import SUBAGENT_TIMEOUT_MAX as _SUBAGENT_TIMEOUT_MAX
from kiro_crew.constants import SUBAGENT_TIMEOUT_MIN as _SUBAGENT_TIMEOUT_MIN
from kiro_crew.constants import SUBAGENT_TIMEOUT_SECS as _SUBAGENT_TIMEOUT_SECS
from kiro_crew.effort import EFFORT_LEVELS, is_valid_effort
from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX
from kiro_crew.stt.limits import DEFAULT_IDLE_EVICT_SECS as _STT_DEFAULT_IDLE_EVICT_SECS
from kiro_crew.stt.limits import DEFAULT_PARTIAL_INTERVAL_MS as _STT_DEFAULT_PARTIAL_INTERVAL_MS
from kiro_crew.stt.limits import DEFAULT_SILENCE_MS as _STT_DEFAULT_SILENCE_MS
from kiro_crew.stt.limits import MAX_THREADS as _STT_MAX_THREADS
from kiro_crew.stt.limits import THREADS_AUTO as _STT_THREADS_AUTO
from kiro_crew.stt.limits import parse_threads as _parse_stt_threads
from kiro_crew.stt.models import CATALOG as _STT_CATALOG
from kiro_crew.stt.models import CUSTOM_MODEL as _STT_CUSTOM_MODEL
from kiro_crew.stt.models import DEFAULT_MODEL as _STT_DEFAULT_MODEL
from kiro_crew.stt.models import resolve as _resolve_stt_model
from kiro_crew.stt.models import valid_custom_path as _valid_stt_custom_path
from kiro_crew.stt.models import valid_custom_sha256 as _valid_stt_custom_sha256

# Stdlib-only leaf: the one table of code points that render as nothing, shared
# with the terminal and scanner paths so the bot-name filter does not keep a copy.
from kiro_crew.terminal_safe import _is_invisible

logger = logging.getLogger("kiro_crew.config.loader")


# The facade over the sections_* owners. ``kiro_crew.config.sections`` is the import
# path and the patch seam; the code it re-exports lives in the owner modules imported
# above, one responsibility each. The owners import each other's names, lowest layer
# first, so a name a test patches here is read in more than one module.
# ``_facade_mirror`` keeps that patch whole: a write or delete of a name through this
# module lands here and in every owner that binds it. This module binds every name its
# owners define, so an annotation evaluated in its namespace still resolves, except a
# global an owner rebinds at run time: that one is read from its owner, so a read here
# never returns a value the owner has replaced. No owner imports this module.
#: Owner modules, lowest layer first: the first one binding a name answers a read of it.
_OWNERS = (
    "kiro_crew.config.sections_defaults",
    "kiro_crew.config.sections_agent_env",
    "kiro_crew.config.sections_agent_fields",
    "kiro_crew.config.sections_agent",
    "kiro_crew.config.sections_session",
    "kiro_crew.config.sections_limits",
    "kiro_crew.config.sections_tailnet",
    "kiro_crew.config.sections_dashboard",
    "kiro_crew.config.sections_dashboard_hosts",
    "kiro_crew.config.sections_slack",
    "kiro_crew.config.sections_channels",
    "kiro_crew.config.sections_telegram",
    "kiro_crew.config.sections_stt",
    "kiro_crew.config.sections_decisions",
    "kiro_crew.config.sections_telemetry",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)

#: Owner-bound name this module does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)

# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

# Derived, not listed: what ``from kiro_crew.config.sections import *`` binds is every
# public name of the family, as it was when the family was one module.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))

if TYPE_CHECKING:  # the owner-defined names this module serves, for type checkers
    from kiro_crew.config.sections_stt import (  # noqa: F401
        _LAST_WARNED_TRANSCRIBE_LANGUAGES,
        _LAST_WARNED_TRANSCRIBE_VOCABULARY,
    )
