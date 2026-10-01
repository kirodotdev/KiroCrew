"""Contracts the config package's module composition must keep (owners behind facades).

Section DTOs, their builders and the loader's document, migration, cache and
agent-resolution rules live in owner modules inside ``kiro_crew.config``, while
``config.sections`` and ``config.loader`` stay the facades every caller imports.
What a relocation can silently break is pinned here: the facade namespaces and
the identity of what they hand out, the logger each owner writes under, and the
seams whose behaviour must not move with the code (the monitoring runtime
ceiling, the MCP ``autoApprove`` default and the read-only workspace root).
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import logging
import os
import pkgutil
from pathlib import Path

import pytest

import kiro_crew.config as config_pkg
from kiro_crew.config import loader, migration, section_builders, sections
from kiro_crew.monitoring.limits import DEFAULT_RUNTIME_CEILING_SECS, MAX_RUNTIME_CEILING_SECS

# Every name each facade module held before the section/loader owners were split
# out. A facade may gain names; it may not lose one, because tests and callers
# reach private seams through these two modules by attribute.
_HISTORICAL_NAMES = {
    "kiro_crew.config.sections": """
ACTIVATION_ALWAYS ACTIVATION_MENTION ACTIVATION_OBSERVE ACTIVATION_OFF ACTIVATION_REVIEW
APPROVAL_TURN_MARGIN_SECS AUTOCOMPACT_PCT_MAX AUTOCOMPACT_PCT_MIN AgentConfig
BACKGROUND_WORKER_AGENTS CHAT_ENTRY_CACHE_BYTES_DEFAULT CHAT_ENTRY_CACHE_BYTES_MAX
CHAT_ENTRY_CACHE_BYTES_MIN CHAT_ENTRY_CACHE_ENTRIES_DEFAULT CHAT_ENTRY_CACHE_ENTRIES_MAX
CHAT_ENTRY_CACHE_ENTRIES_MIN CHAT_TURN_TIMEOUT_MAX CHAT_TURN_TIMEOUT_MIN
COMPLETION_KEEP_CHARS_MAX COMPLETION_KEEP_CHARS_MIN CONTEXT_WARN_MARGIN_PCT ChannelConfig
ComputerUseConfig CronHistoryConfig DECISION_BUCKET_MAX DECISION_BUCKET_MIN
DECISION_HISTORY_BUDGET_DEFAULT DECISION_MODEL_ROUTE_DEFAULT DECISION_MODEL_ROUTE_TIERS
DECISION_PROVIDER_ENDPOINT_DEFAULT DECISION_PROVIDER_MODEL_DEFAULT DEDUP_EVERY_N_SWEEPS_MAX
DEFAULT_AUTOCOMPACT_PCT DEFAULT_AUTO_INGEST_ARTIFACT_KINDS DEFAULT_CWD_ALLOWED_ROOTS
DEFAULT_MAX_PARALLEL_STEPS DEFAULT_MODEL DEFAULT_POOL_SIZE
DEFAULT_RUNTIME_CEILING_SECS DEFAULT_SESSION_TIMEOUT DEFAULT_WATCHDOG_RSS_MAX_MB
DEGRADED_TAILSCALE DashboardConfig DecisionProviderConfig DecisionsConfig DiscordConfig
EFFORT_LEVELS EMBED_RATE_LIMIT_MAX EMPTY_RESPONSE_MAX_CONTINUES_MAX
EMPTY_RESPONSE_MAX_CONTINUES_MIN EXTRACTION_POOL_SIZE_MAX EXTRACTION_POOL_SIZE_MIN
ExternalRegistryConfig FOLDER_INGEST_CHUNK_BUDGET_MAX FOLDER_SORT_DEFAULT FOLDER_SORT_MODES
FORWARD_DECLARED_ENV_DEFAULT FeishuConfig HeartbeatConfig IMESSAGE_SERVICES
IMPORT_CHUNK_BUDGET_MAX IMessageConfig InstancesConfig JAIL_MODE_AUTO JAIL_MODE_OFF JAIL_MODE_ON
JUDGE_PROVIDERS JUDGE_PROVIDER_AUTO JUDGE_PROVIDER_JEV JUDGE_PROVIDER_LLM JiraAuthEntry
KiroCrewAgentConfig KnowledgeConfig LINK_PATTERNS_MAX LINK_PATTERN_PATTERN_MAX_LEN
LINK_PATTERN_URL_MAX_LEN LOOP_STALL_EXIT_AFTER_DEFAULT LOOP_STALL_EXIT_AFTER_MANAGED_DEFAULT
LOOP_STALL_EXIT_AFTER_MAX LOOP_STALL_EXIT_AFTER_MIN LinkPatternRule MAX_RUNTIME_CEILING_SECS
MAX_SUBAGENTS_FIXED_FLOOR MCP_PROBE_TIMEOUT_MAX MCP_PROBE_TIMEOUT_MIN McpConfig McpGatewayConfig
MemoryConfig MemoryStoreConfig MessagingConfig MonitoringConfig NudgeWakeConfig
POOL_SIZE_MAX POOL_TTL_SECS_MAX POOL_TTL_SECS_MIN Path PublishConfig
RECENT_TINT_COUNT_MAX RECENT_TINT_COUNT_MIN ROLE_MODEL_KEYS ResolvedBindings
ResourceLimitsConfig SECRET_URI_PREFIX SESSION_FOLDER_NAME_MAX SESSION_START_TIMEOUT_MAX
SESSION_START_TIMEOUT_MIN SESSION_TIMEOUT_MAX SESSION_TIMEOUT_MIN SOFT_STOP_BUDGET_MAX
SOFT_STOP_BUDGET_MIN STT_LANGUAGE_AUTO STT_LANGUAGE_FALLBACK STT_PROVIDER_LOCAL STT_PROVIDER_OFF
SUBAGENT_AUTO_MAX_CEILING SUBAGENT_MAX_TURNS_CEILING SWEEP_CHUNK_BUDGET_MAX SessionConfig
SessionSummaryConfig SkillsConfig SlackConfig SttConfig TELEGRAM_ACTIVATIONS THRESHOLD_PCT_MAX
THRESHOLD_PCT_MIN TOOL_APPROVAL_TIMEOUT_MAX TOOL_APPROVAL_TIMEOUT_MIN TailscaleConfig
TaskRunnerConfig TeamsConfig TelegramAccountConfig TelegramConfig TelemetryConfig TunnelConfig
WakaTimeConfig WatchdogConfig WeComConfig WebexConfig WeixinConfig WhatsAppConfig
WorkspaceConfig YOLO_UNTIL_SHUTDOWN _AVATAR_EXPRESSION_AXES _AVATAR_FILE_PIN_RE
_AVATAR_GHOST_BOOL_TRAITS _AVATAR_GHOST_STR_TRAITS _AVATAR_IMAGE_EXTS _AVATAR_MOTIONS
_AVATAR_SOUNDS _AVATAR_STATES _AVATAR_TRAIT_MAX_LEN _BOT_NAME_MAX _BOT_NAME_RE _COLOR_HEX_RE
_CONNECT_TIMEOUT_CEILING _CU_DEFAULT_ATTACH_SCREENSHOT _CU_DEFAULT_MAX_TREE_DEPTH
_CU_DEFAULT_MAX_TREE_NODES _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY _CU_DEFAULT_SCREENSHOT_MAX_PX
_CU_DEFAULT_TEXT_LIMIT _DEFAULT_BACKOFF_MAX _DEFAULT_BEACON_ENDPOINT
_DEFAULT_CHAT_TURN_TIMEOUT_SECS _DEFAULT_MAX_RECOVERY _DEFAULT_PROBE_FAILS
_DEFAULT_SSH_COMPRESSION _DEFAULT_SUBAGENT_MAX_TURNS _DEFAULT_TUNNEL_BASE_PORT
_DEFAULT_WARM_SET_CAP _GITLAB_HOST_NAME_RE _MANAGED_SERVICE_ENV _MAX_RECOVERY_CEILING
_MINT_TIMEOUT_CEILING _MINT_TIMEOUT_FLOOR _OBSERVED_DEGRADED_SECTIONS _RECOVER_BACKOFF_CEILING
_RETIRED_STT_PROVIDERS _STT_CATALOG _STT_DEFAULT_IDLE_EVICT_SECS _STT_DEFAULT_MODEL
_STT_DEFAULT_PARTIAL_INTERVAL_MS _STT_DEFAULT_SILENCE_MS _SUBAGENT_TIMEOUT_MAX
_SUBAGENT_TIMEOUT_MIN _SUBAGENT_TIMEOUT_SECS _VALID_ACTIVATIONS _VALID_CHANNEL_PREFIXES
_VALID_COMPLETION_KEEP _VALID_JAIL_MODES _VALID_STT_MODELS _VALID_STT_PROVIDERS
_WARM_SET_CAP_AUTO _WARNED_RESOURCE_LIMIT_KEYS _WARNED_STT_PROVIDERS
_WHATSAPP_GROUP_COOLDOWN_DEFAULT _WHATSAPP_GROUP_MODES _YOLO_DURATION_DEFAULT
_YOLO_DURATION_SECS _archive_retention_days _clamp_pct _coerce_embedding_provider
_coerce_gitlab_hosts _coerce_int _coerce_int_ids _coerce_jira_hosts _coerce_link_patterns
_coerce_opaque_str_ids _coerce_session_folder _coerce_str_ids _coerce_whatsapp_groups _limit_int
_meta _migrate_workspaces _normalize_acp_backend _normalize_jail _normalize_threshold_pair
_normalize_yolo_duration _parse_telegram_accounts _port_or_unset _re _read_auto_add_documents
_read_skip_permissions _resolve_stt_model _resolve_stub_overrides _resolve_stub_roster
_resolve_stub_servers _safe_avatar _safe_bool _safe_color _safe_dict _safe_expressions
_safe_float _safe_int _safe_list _safe_motions _safe_nonnegative_int _safe_pack_id _safe_sounds
_sanitize_bot_name _tailscale_config_from _threshold_pct _urlsplit _validate_activation
_validate_telegram_activation _validate_tracking_channels _validated_completion_keep
_validated_stt_model _validated_stt_provider annotations coerce_deepseek_env coerce_effort
coerce_fallback_model coerce_model_route coerce_refusal_fallback_model coerce_role_efforts
coerce_role_models dataclass deepseek_env_plaintext_keys field is_valid_effort
link_pattern_url_ok logger logging math model_registry normalize_agent_model
resolve_memory_store_config resolve_selected_backend stt_provider_is_coerced
stt_provider_resolution yolo_duration_to_secs
""".split(),
    "kiro_crew.config.loader": """
ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION ACTIVATION_ALWAYS ACTIVATION_MENTION
ACTIVATION_OBSERVE ACTIVATION_OFF ACTIVATION_REVIEW APPROVAL_TURN_MARGIN_SECS
AUTOCOMPACT_PCT_MAX AUTOCOMPACT_PCT_MIN AgentConfig BACKGROUND_WORKER_AGENTS
CHAT_ENTRY_CACHE_BYTES_DEFAULT CHAT_ENTRY_CACHE_BYTES_MAX CHAT_ENTRY_CACHE_BYTES_MIN
CHAT_ENTRY_CACHE_ENTRIES_DEFAULT CHAT_ENTRY_CACHE_ENTRIES_MAX CHAT_ENTRY_CACHE_ENTRIES_MIN
CHAT_TURN_TIMEOUT_MAX CHAT_TURN_TIMEOUT_MIN COMPLETION_KEEP_CHARS_MAX COMPLETION_KEEP_CHARS_MIN
CONFIG_DIR_NAME CONFIG_RESERVED_TOP_KEYS CONNECTIONS_UI_MIGRATION_MARKER CONTEXT_WARN_MARGIN_PCT
CREDENTIAL_KEYS CRED_AZURE_DEVOPS_EXT_PAT CRED_BITBUCKET_API_TOKEN CRED_BITBUCKET_EMAIL
CRED_DISCORD_BOT_TOKEN CRED_FEISHU_APP_ID CRED_FEISHU_APP_SECRET CRED_JIRA_API_TOKEN
CRED_KIRO_API_KEY CRED_MICROSOFT_APP_ID CRED_MICROSOFT_APP_PASSWORD CRED_MICROSOFT_APP_TENANT_ID
CRED_OWNER_ID CRED_SLACK_APP_TOKEN CRED_SLACK_BOT_TOKEN CRED_TELEGRAM_BOT_TOKEN
CRED_WAKATIME_API_KEY CRED_WEBEX_BOT_TOKEN CRED_WECOM_BOT_ID CRED_WECOM_SECRET CRED_WEIXIN_TOKEN
Callable ChannelConfig ComputerUseConfig ConfigReadError ConfigWriteRefused CronHistoryConfig
DASHBOARD_PORT DEDUP_EVERY_N_SWEEPS_MAX DEFAULT_AUTOCOMPACT_PCT
DEFAULT_AUTO_INGEST_ARTIFACT_KINDS DEFAULT_CWD_ALLOWED_ROOTS DEFAULT_MAX_PARALLEL_STEPS
DEFAULT_MEMORY_STORE DEFAULT_MODEL DEFAULT_POOL_SIZE DEFAULT_SESSION_TIMEOUT
DEFAULT_SUBAGENT_MAX_TURNS DEGRADED_TAILSCALE DEGRADED_WHOLE_CONFIG DashboardConfig
DecisionsConfig DiscordConfig EFFORT_LEVELS EMBED_RATE_LIMIT_MAX EXTRACTION_POOL_SIZE_MAX
EXTRACTION_POOL_SIZE_MIN ExternalRegistryConfig FOLDER_INGEST_CHUNK_BUDGET_MAX
FORWARD_DECLARED_ENV_DEFAULT FeishuConfig HeartbeatConfig IMESSAGE_SERVICES
IMPORT_CHUNK_BUDGET_MAX IMessageConfig InstancesConfig Iterable Iterator JAIL_MODE_AUTO
JAIL_MODE_OFF JAIL_MODE_ON JiraAuthEntry KiroCrewAgentConfig KiroCrewConfig KnowledgeConfig
LOOP_STALL_EXIT_AFTER_DEFAULT LOOP_STALL_EXIT_AFTER_MANAGED_DEFAULT LOOP_STALL_EXIT_AFTER_MAX
LOOP_STALL_EXIT_AFTER_MIN Literal MANAGED_VAULT_FIXED_CONSUMERS MAX_SUBAGENTS_FIXED_FLOOR
MCP_PROBE_TIMEOUT_MAX MCP_PROBE_TIMEOUT_MIN MIGRATE_AGENTS MIGRATE_CONNECTIONS_UI
MIGRATE_DEFAULT_AGENT MIGRATE_SUPERSEDED_DEFAULTS MIGRATE_WORKSPACES MISSING MODEL_NAMESPACE_ACP
Mapping McpConfig McpGatewayConfig MemoryConfig MemoryStoreConfig MessagingConfig
MonitoringConfig MutableMapping OUTBOX_DIR_NAME POOL_SIZE_MAX
POOL_TTL_SECS_MAX POOL_TTL_SECS_MIN Path PublishConfig RECENT_TINT_COUNT_MAX
RECENT_TINT_COUNT_MIN ROLE_MODEL_KEYS ResolvedBindings ResourceLimitsConfig
SESSION_FOLDER_NAME_MAX SESSION_START_TIMEOUT_MAX SESSION_START_TIMEOUT_MIN SESSION_TIMEOUT_MAX
SESSION_TIMEOUT_MIN SOFT_STOP_BUDGET_MAX SOFT_STOP_BUDGET_MIN STT_PROVIDER_LOCAL
SUBAGENT_AUTO_MAX_CEILING SUBAGENT_MAX_TURNS_CEILING SUBAGENT_TIMEOUT_MAX SUBAGENT_TIMEOUT_MIN
SUBAGENT_TIMEOUT_SECS SWEEP_CHUNK_BUDGET_MAX SessionConfig SessionSummaryConfig SkillsConfig
SlackConfig SttConfig SupersededDefault TELEGRAM_ACTIVATIONS THRESHOLD_PCT_MAX THRESHOLD_PCT_MIN
TOOL_APPROVAL_TIMEOUT_MAX TOOL_APPROVAL_TIMEOUT_MIN TailscaleConfig TaskRunnerConfig TeamsConfig
TelegramAccountConfig TelegramConfig TelemetryConfig TunnelConfig WakaTimeConfig WatchdogConfig
WeComConfig WebexConfig WeixinConfig WhatsAppConfig WorkspaceConfig WorkspaceDirUnusable
YOLO_UNTIL_SHUTDOWN _BOT_NAME_MAX _BOT_NAME_RE _COLOR_HEX_RE _CONFIG_AGENT_ALIAS_SNAPSHOT
_CONFIG_AUTOCOMPACT_ISSUED _CONFIG_AUTOCOMPACT_LOCK _CONFIG_AUTOCOMPACT_PCT
_CONFIG_AUTOCOMPACT_TICKET _CONFIG_CACHE _CONFIG_CACHE_LOCK _CONFIG_TIMEZONE
_CONFIG_TIMEZONE_LOCK _CONFIG_TIMEZONE_TICKET _CONNECT_TIMEOUT_CEILING
_CU_DEFAULT_ATTACH_SCREENSHOT _CU_DEFAULT_MAX_TREE_DEPTH _CU_DEFAULT_MAX_TREE_NODES
_CU_DEFAULT_SCREENSHOT_JPEG_QUALITY _CU_DEFAULT_SCREENSHOT_MAX_PX _CU_DEFAULT_TEXT_LIMIT
_CU_MAX_SCREENSHOT_MAX_PX _CU_MAX_TEXT_LIMIT _CU_MAX_TREE_DEPTH _CU_MAX_TREE_NODES
_CU_MIN_SCREENSHOT_MAX_PX _DEFAULT_BACKOFF_MAX _DEFAULT_BEACON_ENDPOINT
_DEFAULT_CHAT_TURN_TIMEOUT_SECS _DEFAULT_CONNECT_TIMEOUT _DEFAULT_MAX_RECOVERY
_DEFAULT_MEMORY_MODES _DEFAULT_MINT_TIMEOUT _DEFAULT_PORT _DEFAULT_PROBE_FAILS
_DEFAULT_SSH_COMPRESSION _DEFAULT_TUNNEL_BASE_PORT _DEFAULT_WARM_SET_CAP _GITLAB_HOST_NAME_RE
_HAS_JSONSCHEMA _JIRA_TOKEN_RE _JSON_TYPE_LABELS _KIRO_API_KEY_ONLY _KNOWN_CONFIG_SECTIONS
_MANAGED_SERVICE_ENV _MATERIALIZED_AGENTS _MATERIALIZED_AGENTS_GENERATION
_MATERIALIZED_AGENTS_LOCK _MATERIALIZED_AGENTS_READY _MATERIALIZED_REFRESH_APPLIED
_MATERIALIZED_REFRESH_ISSUED _MAX_RECOVERY_CEILING _MINT_TIMEOUT_CEILING _MINT_TIMEOUT_FLOOR
_OBSERVED_DEGRADED_SECTIONS _PinnedCreateRefusal _RECOVER_BACKOFF_CEILING
_REPORTED_SUPERSEDED_KEYS _RETIRED_STT_PROVIDERS _SECURITY_BOUNDED_FIELDS _SIDECAR_BASE_SHADOW
_STT_CATALOG _STT_DEFAULT_IDLE_EVICT_SECS _STT_DEFAULT_MODEL _STT_DEFAULT_PARTIAL_INTERVAL_MS
_STT_DEFAULT_SILENCE_MS _STT_DEFAULT_TIMEOUT_SECS _STT_IDLE_EVICT_SECS_MAX
_STT_IDLE_EVICT_SECS_MIN _STT_INTERVAL_MS_MAX _STT_MAX_TIMEOUT_SECS _STT_MIN_PARTIAL_INTERVAL_MS
_STT_MIN_SILENCE_MS _STT_MIN_TIMEOUT_SECS _VALID_ACTIVATIONS _VALID_CHANNEL_PREFIXES
_VALID_COMPLETION_KEEP _VALID_JAIL_MODES _VALID_STT_MODELS _VALID_STT_PROVIDERS
_WARM_SET_CAP_AUTO _WARNED_RESOURCE_LIMIT_KEYS _WARNED_STT_PROVIDERS
_WHATSAPP_GROUP_COOLDOWN_DEFAULT _WHATSAPP_GROUP_MODES _WORKSPACE_DIR_NAME
_YOLO_DURATION_DEFAULT _YOLO_DURATION_SECS _actual_type_name _adopt_in_memory
_apply_document_migrations _apply_field_default _archive_retention_days _build_agent_config
_build_computer_use_config _build_cron_history_config _build_dashboard_config
_build_discord_config _build_feishu_config _build_imessage_config _build_instances_config
_build_knowledge_config _build_mcp_config _build_mcp_gateway_config _build_memory_config
_build_messaging_config _build_monitoring_config
_build_publish_config _build_session_config _build_session_summary_config _build_skills_config
_build_slack_config _build_stt_config _build_taskrunner_config _build_teams_config
_build_telegram_config _build_telemetry_config _build_tunnel_config _build_wakatime_config
_build_watchdog_config _build_webex_config _build_wecom_config _build_weixin_config
_build_whatsapp_config _cached_validated_data _clamp_pct _clamp_security_bounds
_coerce_embedding_provider _coerce_gitlab_hosts _coerce_int _coerce_int_ids _coerce_jira_hosts
_coerce_opaque_str_ids _coerce_session_folder _coerce_str_ids _coerce_whatsapp_groups
_coerced_section _config_fingerprint _config_write_lock _deep_merge _deepseek_env_on_disk
_default_memory_mode_from _default_workspace_base _dot_path_from_json_path
_fail_closed_project_skills_config _folder_sort_from _get_help_text _inside_data_home
_invalidate_config_cache _is_deprecated_path _is_sensitive_path _limit_int _lock_target
_log_config_clamp_event _lookup_schema_node _mark_file_degraded _mask_value
_materialized_kiro_agent _meta _migrate_workspaces _normalize_acp_backend _normalize_jail
_normalize_threshold_pair _normalize_yolo_duration _notify_live_watch _overlay_kiro_agent
_overlay_supplies _parse_telegram_accounts _persist_config_migration _port_or_unset
_project_declares_agent _project_scope_excludes _raw_config _re _read_auto_add_documents
_read_hardened_agent_spec _read_skip_permissions _refuse_unpublishable
_report_superseded_defaults _resolution _resolve_agent_selection _resolve_stt_model
_resolve_stub_overrides _resolve_stub_roster _resolve_stub_servers _resolve_workspace_root
_safe_bool _safe_color _safe_dict _safe_dir_name _safe_float _safe_int _safe_list
_safe_nonnegative_int _sanitize_bot_name _scan_materialized_agents _sections _session_work_dir
_shadowed_base_sections _stat _store_validated_data _subagent_timeout_from _subtract_overlay
_tailscale_config_from _threshold_pct _urlsplit _validate_activation _validate_config_data
_validate_telegram_activation _validate_tracking_channels _validated_completion_keep
_validated_stt_model _validated_stt_provider _warned_env_keys _workspace_dir_file
_workspace_name_for_dir _write_migration_backup agent_alias_snapshot annotations asdict asyncio
atomic_write auto_adoptable aws_consent_path build_provider_factory capabilities_for
coerce_dict_section coerce_effort coerce_fallback_model coerce_role_efforts coerce_role_models
coerce_runtime_ceiling computer_use_state_path config_dir config_local_path config_package_dir
config_path consume_managed_service_launch_environment contextlib copy credential_redaction_path
data_home dataclass datetime decisions_consent_path default_overlay_dir default_project_dir
default_socket_path degraded_config_files denied_commands_path drift_summary drop_drifted_keys
ensure_data_home env_path field file_delivery_consent_path inject_kiro_cli_api_key
is_valid_effort iter_agent_spec_files jira_global_token_applicable jira_host_token_name json
kiro_agents_dir load_loop_stall_exit_after logger logging materialize_workspace_dir math
memory_store_name_defect model_registry model_scope model_supports_effort
next_config_load_ticket normalize_agent_model normalize_jira_host normalize_workspace_path
oauth_endpoints_path on_event_loop os outbox_dir parse_agent_spec_text pinned_fs platform_compat
publish_agent_alias_snapshot publish_autocompact_pct publish_config_timezone
publish_materialized_agents published_autocompact_pct published_config_timezone
read_config_for_update read_env_file_credential read_local_secret record_adoptions
refresh_config_meta_stamp refresh_materialized_agents reset_degraded_observations
resolve_agent_bindings resolve_agent_config_path resolve_agent_identity
resolve_cc_permission_mode resolve_crew_identity resolve_effective_agent resolve_effective_model
resolve_loop_stall_exit_after resolve_memory_store_config resolve_selected_backend
schedule_materialized_agents_refresh shutil ssh_auth_sock_consent_path stamp_config_meta
stored_value_or_none strip_kiro_cli_api_key superseded_default_drift sys
tailnet_effective_allowed_logins tailnet_identity_unknown threading timezone
unsandboxed_exec_declared unsandboxed_exec_platform_default update_config_locked uuid
validate_kiro_agent_references windows_acl workspace_dir_for workspace_dir_from_entry
workspace_root write_config_atomically yolo_duration_to_secs
""".split(),
}

# Modules that were never split out of the loader keep their own module-named
# logger; every other config module logs as the loader did, so a relocated
# warning keeps the record name operators filter on and ``caplog`` tests select.
_OWN_LOGGER_MODULES = {
    "kiro_crew.config.live",
    "kiro_crew.config.paths",
    "kiro_crew.config.superseded_defaults",
}

_MONITORING_HELP = (
    "Finite wall-clock ceiling for new and updated monitors. Accepts up to "
    "2592000 seconds (30 days). Raising this limit never extends an existing deadline."
)


def _config_modules() -> list[str]:
    return sorted(
        info.name
        for info in pkgutil.iter_modules(config_pkg.__path__, prefix="kiro_crew.config.")
        if not info.ispkg
    )


class TestFacadeNamespaces:
    @pytest.mark.parametrize("module_name", sorted(_HISTORICAL_NAMES))
    def test_facade_keeps_every_historical_name(self, module_name: str) -> None:
        module = importlib.import_module(module_name)
        missing = [name for name in _HISTORICAL_NAMES[module_name] if not hasattr(module, name)]
        assert missing == []

    def test_names_on_both_facades_are_the_same_object(self) -> None:
        shared = set(_HISTORICAL_NAMES["kiro_crew.config.sections"]) & set(
            _HISTORICAL_NAMES["kiro_crew.config.loader"]
        )
        assert len(shared) > 200
        split = sorted(
            name for name in shared if getattr(loader, name) is not getattr(sections, name)
        )
        assert split == []

    def test_package_surface_reads_the_loader(self) -> None:
        for name in config_pkg.__all__:
            assert getattr(config_pkg, name) is getattr(loader, name)


class TestOwnerLoggers:
    def test_every_config_module_logs_under_its_historical_name(self) -> None:
        names = {}
        for module_name in _config_modules():
            module = importlib.import_module(module_name)
            log = getattr(module, "logger", None)
            if isinstance(log, logging.Logger):
                names[module_name] = log.name
        expected = {
            module_name: (module_name if module_name in _OWN_LOGGER_MODULES else loader.__name__)
            for module_name in names
        }
        assert names == expected
        assert names[loader.__name__] == loader.__name__


class TestMonitoringRuntimeCeiling:
    def test_field_order_default_and_bounds(self) -> None:
        fields = dataclasses.fields(sections.MonitoringConfig)
        assert [f.name for f in fields] == ["max_runtime_secs", "prefer_structured_arming"]
        ceiling = fields[0]
        assert ceiling.type == "int"
        assert ceiling.default == DEFAULT_RUNTIME_CEILING_SECS == 604800
        assert MAX_RUNTIME_CEILING_SECS == 2592000
        assert ceiling.metadata["label"] == "Maximum monitoring runtime (seconds)"
        assert ceiling.metadata["help"] == _MONITORING_HELP
        assert ceiling.metadata["min"] == 1
        assert ceiling.metadata["max"] == MAX_RUNTIME_CEILING_SECS
        assert fields[1].default is False

    def test_serialized_section_keeps_its_key_order(self) -> None:
        section = loader.KiroCrewConfig().to_dict()["monitoring"]
        assert list(section) == ["max_runtime_secs", "prefer_structured_arming"]
        assert section == {"max_runtime_secs": 604800, "prefer_structured_arming": False}

    def test_unset_value_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            built = loader._build_monitoring_config({}, True)
        assert built == sections.MonitoringConfig(
            max_runtime_secs=DEFAULT_RUNTIME_CEILING_SECS, prefer_structured_arming=True
        )
        assert caplog.records == []

    @pytest.mark.parametrize("raw", [0, -1, MAX_RUNTIME_CEILING_SECS + 1, True, "3600", 3600.0])
    def test_rejected_value_warns_with_the_value_and_the_fallback(
        self, raw: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.monitoring.limits"):
            built = loader._build_monitoring_config({"max_runtime_secs": raw}, False)
        assert built.max_runtime_secs == DEFAULT_RUNTIME_CEILING_SECS
        [record] = [r for r in caplog.records if r.name == "kiro_crew.monitoring.limits"]
        assert repr(raw) in record.getMessage()
        assert str(DEFAULT_RUNTIME_CEILING_SECS) in record.getMessage()

    def test_accepted_value_reaches_a_loaded_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _load_document(
            tmp_path, monkeypatch, {"monitoring": {"max_runtime_secs": MAX_RUNTIME_CEILING_SECS}}
        )
        assert cfg.monitoring.max_runtime_secs == MAX_RUNTIME_CEILING_SECS
        assert cfg.monitoring.prefer_structured_arming is False


class TestMcpAutoApproveDefault:
    """The builder always sets the field, so its default and the dataclass's must agree."""

    def test_builder_and_dataclass_defaults_agree(self) -> None:
        assert sections.McpConfig().honour_auto_approve is True
        assert loader._build_mcp_config({}).honour_auto_approve is True
        assert loader.KiroCrewConfig().to_dict()["mcp"]["honour_auto_approve"] is True

    @pytest.mark.parametrize(("raw", "expected"), [(True, True), (False, False), ("false", False)])
    def test_only_a_real_boolean_true_is_read_as_true(self, raw: object, expected: bool) -> None:
        assert (
            loader._build_mcp_config({"honour_auto_approve": raw}).honour_auto_approve is expected
        )


class TestReadOnlyWorkspaceRoot:
    def test_signature_is_frozen(self) -> None:
        assert (
            str(inspect.signature(loader.workspace_root)) == "(*, create: 'bool' = True) -> 'Path'"
        )

    def test_create_false_resolves_without_creating_the_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KIROCREW_WORKSPACE", raising=False)
        monkeypatch.setattr(loader, "_default_workspace_base", lambda: tmp_path / "base")
        data_home = tmp_path / "home"
        data_home.mkdir()
        monkeypatch.setattr(loader, "config_dir", lambda: data_home)

        root = loader.workspace_root(create=False)

        assert root == Path(os.path.realpath(tmp_path / "base" / loader._WORKSPACE_DIR_NAME))
        assert not (tmp_path / "base").exists()
        assert list(data_home.iterdir()) == []

    def test_saved_root_is_resolved_but_not_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KIROCREW_WORKSPACE", raising=False)
        data_home = tmp_path / "home"
        data_home.mkdir()
        target = tmp_path / "saved" / "ws"
        (data_home / "workspace_dir").write_text(f"{target}\n", encoding="utf-8")
        monkeypatch.setattr(loader, "config_dir", lambda: data_home)

        assert loader.workspace_root(create=False) == Path(os.path.realpath(target))
        assert not (tmp_path / "saved").exists()
        assert sorted(p.name for p in data_home.iterdir()) == ["workspace_dir"]

    def test_default_still_creates_the_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "ws"))

        assert loader.workspace_root() == Path(os.path.realpath(tmp_path / "ws"))
        assert (tmp_path / "ws").is_dir()


def _load_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: dict
) -> loader.KiroCrewConfig:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(loader, "config_path", lambda: path)
    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(loader, "config_local_path", lambda: tmp_path / "config.local.json")
    return loader.KiroCrewConfig.load()


class TestLoaderSeamsReachTheOwners:
    """A name read by code that stays in the loader is still a call-time seam there."""

    def test_patched_paths_steer_load_and_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _load_document(tmp_path, monkeypatch, {"session": {"timeout_secs": 1234}})
        assert cfg.session.timeout_secs == 1234
        cfg.session.timeout_secs = 4321
        cfg.save()
        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["session"]["timeout_secs"] == 4321

    def test_update_config_locked_writes_through_the_loader_writer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(loader, "config_path", lambda: path)
        monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
        real = loader.write_config_atomically
        written: list[Path] = []

        def spy(target, data, *args, **kwargs):
            written.append(Path(target))
            return real(target, data, *args, **kwargs)

        monkeypatch.setattr(loader, "write_config_atomically", spy)
        loader.update_config_locked(mutate=lambda doc: {**doc, "timezone": "UTC"})
        assert written == [path]
        assert json.loads(path.read_text(encoding="utf-8"))["timezone"] == "UTC"

    def test_patched_pool_size_fallback_reaches_the_session_builder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(loader, "DEFAULT_POOL_SIZE", 3)
        cfg = _load_document(tmp_path, monkeypatch, {"session": {}})
        assert cfg.session.pool_size == 3

    def test_patched_clamp_event_hears_a_load_time_clamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[tuple] = []
        monkeypatch.setattr(loader, "_log_config_clamp_event", lambda *a: events.append(a))
        cfg = _load_document(tmp_path, monkeypatch, {"agent": {"subagent_max_turns": 0}})
        assert cfg.agent.subagent_max_turns == 1
        assert [e[0] for e in events] == ["agent.subagent_max_turns"]

    def test_patched_ledger_writer_reaches_the_migration_transform(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}
        monkeypatch.setattr(loader, "record_adoptions", lambda values: seen.update(values))
        doc = {"agent": {"subagent_timeout_secs": 1800}}
        changed = loader._apply_document_migrations(
            doc,
            frozenset({loader.MIGRATE_SUPERSEDED_DEFAULTS}),
            overlay_kiro_agent=None,
            default_kiro_agent="kirocrew",
            adopt_keys=frozenset({"agent.subagent_timeout_secs"}),
            recorded_adoptions=[],
        )
        assert changed is True
        assert seen == {"agent.subagent_timeout_secs": 1800}
        assert doc == {"agent": {}}

    def test_patched_env_path_steers_the_credential_reader(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = tmp_path / ".env"
        env.write_text("WEBEX_BOT_TOKEN=fixture-token\n", encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)
        monkeypatch.delenv("WEBEX_BOT_TOKEN", raising=False)
        assert loader.read_env_file_credential("WEBEX_BOT_TOKEN") == "fixture-token"

    def test_moved_builders_are_the_loader_names(self) -> None:
        moved = sorted(n for n in vars(section_builders) if n.startswith("_build_"))
        assert len(moved) == 27
        assert [n for n in moved if getattr(loader, n) is not getattr(section_builders, n)] == []
        loader_owned = sorted(
            n
            for n, v in vars(loader).items()
            if n.startswith("_build_") and getattr(v, "__module__", "") == loader.__name__
        )
        assert loader_owned == [
            "_build_agent_config",
            "_build_dashboard_config",
            "_build_session_config",
            "_build_telemetry_config",
        ]


class TestRelocatedSeamsLiveOnTheirOwners:
    """A helper read inside a relocated builder or rule is patched on its owner module."""

    def test_stub_roster_reader_is_patched_on_section_builders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        roster = ["patched-roster"]
        monkeypatch.setattr(section_builders, "_resolve_stub_servers", lambda _data: roster)
        assert loader._build_mcp_gateway_config({}).stub_servers is roster

    def test_runtime_ceiling_coercer_is_patched_on_section_builders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(section_builders, "coerce_runtime_ceiling", lambda _value: 42)
        assert loader._build_monitoring_config({}, False).max_runtime_secs == 42

    def test_stt_provider_rule_is_patched_on_section_builders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(section_builders, "_validated_stt_provider", lambda _raw: "patched")
        assert loader._build_stt_config({"provider": "local"}).provider == "patched"

    def test_drift_reader_is_patched_on_migration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[dict] = []
        monkeypatch.setattr(
            migration, "superseded_default_drift", lambda data: seen.append(data) or []
        )
        document = {"agent": {}}
        loader._report_superseded_defaults(document)
        assert seen == [document]

    def test_warn_once_set_is_one_shared_object(self) -> None:
        assert loader._REPORTED_SUPERSEDED_KEYS is migration._REPORTED_SUPERSEDED_KEYS
        assert sections.MemoryConfig is loader.MemoryConfig


class TestMigrationContract:
    def test_migration_ids_and_marker_are_frozen(self) -> None:
        assert (
            migration.MIGRATE_WORKSPACES,
            migration.MIGRATE_AGENTS,
            migration.MIGRATE_DEFAULT_AGENT,
            migration.MIGRATE_CONNECTIONS_UI,
            migration.MIGRATE_SUPERSEDED_DEFAULTS,
            migration.CONNECTIONS_UI_MIGRATION_MARKER,
        ) == (
            "workspaces",
            "agents",
            "default_agent",
            "connections_ui",
            "superseded_defaults",
            "connections_ui_migrated.json",
        )
        for name in (
            "MIGRATE_WORKSPACES",
            "MIGRATE_AGENTS",
            "MIGRATE_DEFAULT_AGENT",
            "MIGRATE_CONNECTIONS_UI",
            "MIGRATE_SUPERSEDED_DEFAULTS",
            "CONNECTIONS_UI_MIGRATION_MARKER",
            "_REPORTED_SUPERSEDED_KEYS",
        ):
            assert getattr(loader, name) is getattr(migration, name)

    def test_pending_migrations_produce_this_document_and_are_idempotent(self) -> None:
        """Pins the migrated document, its key order, and that a second pass is a no-op."""
        doc = {
            "agent": {"default_agent": "kiro-base"},
            "workspaces": {"default": "~/w", "kept": {"dir": "~/k"}},
            "connections_ui": False,
            "default_agent": "missing",
        }
        pending = frozenset(
            {
                loader.MIGRATE_WORKSPACES,
                loader.MIGRATE_AGENTS,
                loader.MIGRATE_CONNECTIONS_UI,
                loader.MIGRATE_DEFAULT_AGENT,
            }
        )
        kwargs = {"overlay_kiro_agent": None, "default_kiro_agent": "kirocrew"}

        assert loader._apply_document_migrations(doc, pending, **kwargs) is True
        assert list(doc) == ["agent", "workspaces", "default_agent", "agents"]
        assert doc["workspaces"] == {"default": {"dir": "~/w"}, "kept": {"dir": "~/k"}}
        assert doc["agents"]["default"]["kiro_agent"] == "kiro-base"
        assert doc["agents"]["default"]["workspace"] == "default"
        assert doc["default_agent"] == "default"
        assert loader._apply_document_migrations(doc, pending, **kwargs) is False

    def test_the_agents_seed_runs_before_the_default_agent_repair(self) -> None:
        """The repair reads the seeded ``agents``, and the order decides the saved key order."""
        doc = {"agent": {"default_agent": "kiro-base"}}
        loader._apply_document_migrations(
            doc,
            frozenset({loader.MIGRATE_AGENTS, loader.MIGRATE_DEFAULT_AGENT}),
            overlay_kiro_agent=None,
            default_kiro_agent="kirocrew",
        )
        assert list(doc) == ["agent", "agents", "default_agent"]
        assert doc["default_agent"] == "default"
        assert doc["agents"]["default"]["kiro_agent"] == "kiro-base"

    def test_write_back_backs_up_the_exact_original_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = b'{"agent":   {"default_agent": "kiro-base"}}'
        (tmp_path / "config.json").write_bytes(original)
        monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(loader, "config_local_path", lambda: tmp_path / "config.local.json")

        loader.KiroCrewConfig.load()

        assert (tmp_path / "config.json.bak").read_bytes() == original
        migrated = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert migrated["agents"]["default"]["kiro_agent"] == "kiro-base"
        assert migrated["default_agent"] == "default"
