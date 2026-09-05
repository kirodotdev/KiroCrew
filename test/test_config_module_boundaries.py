"""Compatibility and dependency guards for the split config loader."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from kiro_crew.config import loader, resolution, sections

_PRE_SPLIT_REEXPORT_NAMES = {
    "kiro_crew.config.sections": tuple("""
ACTIVATION_ALWAYS
ACTIVATION_MENTION
ACTIVATION_OBSERVE
ACTIVATION_OFF
ACTIVATION_REVIEW
APPROVAL_TURN_MARGIN_SECS
AUTOCOMPACT_PCT_MAX
AUTOCOMPACT_PCT_MIN
AgentConfig
BACKGROUND_WORKER_AGENTS
CHAT_ENTRY_CACHE_BYTES_DEFAULT
CHAT_ENTRY_CACHE_BYTES_MAX
CHAT_ENTRY_CACHE_BYTES_MIN
CHAT_ENTRY_CACHE_ENTRIES_DEFAULT
CHAT_ENTRY_CACHE_ENTRIES_MAX
CHAT_ENTRY_CACHE_ENTRIES_MIN
CHAT_TURN_TIMEOUT_MAX
CHAT_TURN_TIMEOUT_MIN
COMPLETION_KEEP_CHARS_MAX
COMPLETION_KEEP_CHARS_MIN
CONTEXT_WARN_MARGIN_PCT
ChannelConfig
ComputerUseConfig
CronHistoryConfig
DEDUP_EVERY_N_SWEEPS_MAX
DEFAULT_AUTOCOMPACT_PCT
DEFAULT_AUTO_INGEST_ARTIFACT_KINDS
DEFAULT_CWD_ALLOWED_ROOTS
DEFAULT_MAX_PARALLEL_STEPS
DEFAULT_MODEL
DEFAULT_POOL_SIZE
DEFAULT_SESSION_TIMEOUT
DashboardConfig
DiscordConfig
EFFORT_LEVELS
EMBED_RATE_LIMIT_MAX
EXTRACTION_POOL_SIZE_MAX
EXTRACTION_POOL_SIZE_MIN
ExternalRegistryConfig
FOLDER_INGEST_CHUNK_BUDGET_MAX
FORWARD_DECLARED_ENV_DEFAULT
FeishuConfig
HeartbeatConfig
IMESSAGE_SERVICES
IMessageConfig
InstancesConfig
JAIL_MODE_AUTO
JAIL_MODE_OFF
JAIL_MODE_ON
JiraAuthEntry
KiroCrewAgentConfig
KnowledgeConfig
LOOP_STALL_EXIT_AFTER_DEFAULT
LOOP_STALL_EXIT_AFTER_MANAGED_DEFAULT
LOOP_STALL_EXIT_AFTER_MAX
LOOP_STALL_EXIT_AFTER_MIN
MAX_SUBAGENTS_FIXED_FLOOR
MCP_PROBE_TIMEOUT_MAX
MCP_PROBE_TIMEOUT_MIN
McpConfig
McpGatewayConfig
MemoryConfig
MemoryStoreConfig
MessagingConfig
OrchestratorConfig
POOL_SIZE_MAX
POOL_TTL_SECS_MAX
POOL_TTL_SECS_MIN
PublishConfig
RECENT_TINT_COUNT_MAX
RECENT_TINT_COUNT_MIN
ROLE_MODEL_KEYS
ResolvedBindings
ResourceLimitsConfig
SESSION_FOLDER_NAME_MAX
SESSION_START_TIMEOUT_MAX
SESSION_START_TIMEOUT_MIN
SESSION_TIMEOUT_MAX
SESSION_TIMEOUT_MIN
SOFT_STOP_BUDGET_MAX
SOFT_STOP_BUDGET_MIN
STT_PROVIDER_LOCAL
SUBAGENT_AUTO_MAX_CEILING
SUBAGENT_MAX_TURNS_CEILING
SWEEP_CHUNK_BUDGET_MAX
SessionConfig
SessionSummaryConfig
SkillsConfig
SlackConfig
SttConfig
TELEGRAM_ACTIVATIONS
THRESHOLD_PCT_MAX
THRESHOLD_PCT_MIN
TOOL_APPROVAL_TIMEOUT_MAX
TOOL_APPROVAL_TIMEOUT_MIN
TailscaleConfig
TaskRunnerConfig
TeamsConfig
TelegramAccountConfig
TelegramConfig
TelemetryConfig
TunnelConfig
WakaTimeConfig
WatchdogConfig
WeComConfig
WebexConfig
WeixinConfig
WhatsAppConfig
WorkspaceConfig
YOLO_UNTIL_SHUTDOWN
_BOT_NAME_MAX
_BOT_NAME_RE
_COLOR_HEX_RE
_CONNECT_TIMEOUT_CEILING
_DEFAULT_BEACON_ENDPOINT
_DEFAULT_CHAT_TURN_TIMEOUT_SECS
_GITLAB_HOST_NAME_RE
_MANAGED_SERVICE_ENV
_MAX_RECOVERY_CEILING
_MINT_TIMEOUT_CEILING
_MINT_TIMEOUT_FLOOR
_RECOVER_BACKOFF_CEILING
_RETIRED_STT_PROVIDERS
_STT_CATALOG
_VALID_ACTIVATIONS
_VALID_CHANNEL_PREFIXES
_VALID_COMPLETION_KEEP
_VALID_JAIL_MODES
_VALID_STT_MODELS
_VALID_STT_PROVIDERS
_WARM_SET_CAP_AUTO
_WARNED_RESOURCE_LIMIT_KEYS
_WARNED_STT_PROVIDERS
_WHATSAPP_GROUP_COOLDOWN_DEFAULT
_WHATSAPP_GROUP_MODES
_YOLO_DURATION_DEFAULT
_YOLO_DURATION_SECS
_archive_retention_days
_clamp_pct
_coerce_embedding_provider
_coerce_gitlab_hosts
_coerce_int
_coerce_int_ids
_coerce_jira_hosts
_coerce_opaque_str_ids
_coerce_session_folder
_coerce_str_ids
_coerce_whatsapp_groups
_limit_int
_meta
_migrate_workspaces
_normalize_acp_backend
_normalize_jail
_normalize_threshold_pair
_normalize_yolo_duration
_parse_telegram_accounts
_port_or_unset
_read_auto_add_documents
_read_skip_permissions
_resolve_stt_model
_resolve_stub_overrides
_resolve_stub_roster
_resolve_stub_servers
_safe_bool
_safe_color
_safe_dict
_safe_float
_safe_int
_safe_list
_safe_nonnegative_int
_sanitize_bot_name
_tailscale_config_from
_threshold_pct
_validate_activation
_validate_telegram_activation
_validate_tracking_channels
_validated_completion_keep
_validated_stt_model
_validated_stt_provider
coerce_effort
coerce_fallback_model
coerce_role_efforts
coerce_role_models
normalize_agent_model
resolve_memory_store_config
resolve_selected_backend
yolo_duration_to_secs
""".split()),
    "kiro_crew.config.resolution": tuple("""
CONFIG_RESERVED_TOP_KEYS
DEGRADED_TAILSCALE
DEGRADED_WHOLE_CONFIG
_KNOWN_CONFIG_SECTIONS
_OBSERVED_DEGRADED_SECTIONS
_coerced_section
_deep_merge
_fail_closed_project_skills_config
_mark_file_degraded
_subtract_overlay
degraded_config_files
reset_degraded_observations
tailnet_effective_allowed_logins
tailnet_identity_unknown
""".split()),
}


def _loader_reexports(module_name: str) -> set[str]:
    """Names explicitly imported from *module_name* by the compatibility facade."""
    tree = ast.parse(Path(loader.__file__).read_text(encoding="utf-8"))
    return {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == module_name
        for alias in node.names
    }


def test_loader_reexports_historical_snapshot_by_identity() -> None:
    """Pre-split aliases stay frozen without exporting future module internals."""
    reexports = {
        module.__name__: _loader_reexports(module.__name__) for module in (sections, resolution)
    }
    assert {name: tuple(sorted(names)) for name, names in reexports.items()} == (
        _PRE_SPLIT_REEXPORT_NAMES
    )

    mismatches = [
        f"{module.__name__}.{name}"
        for module in (sections, resolution)
        for name in sorted(reexports[module.__name__])
        if getattr(loader, name, None) is not getattr(module, name)
    ]
    assert mismatches == []


def test_extracted_modules_do_not_import_the_loader() -> None:
    """The facade owns orchestration; extracted modules cannot depend back on it."""
    code = (
        "import sys\n"
        "import kiro_crew.config.sections\n"
        "import kiro_crew.config.resolution\n"
        "forbidden = (\n"
        "    'kiro_crew.config.loader',\n"
        "    'kiro_crew.config.schema',\n"
        "    'kiro_crew.config.validation',\n"
        ")\n"
        "print(','.join(name for name in forbidden if name in sys.modules))\n"
    )
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env=env,
    )
    assert result.stdout.strip() == ""
