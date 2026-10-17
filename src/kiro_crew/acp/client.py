"""ACP client — JSON-RPC 2.0 over stdio with `kiro-cli acp` or `claude-agent-acp`.

Protocol (ACP JSON-RPC 2.0):
  initialize → session/new → session/set_mode → session/set_model → session/prompt
  (claude backend: no agent activation via set_mode -- its modes are permission
  modes, not agents -- and session/set_config_option for model)

Agent selection: ``session/set_mode`` with ``modeId`` activates the agent
config (prompt, tools, resources).  MCP servers are passed explicitly
in ``session/new`` via the ``mcpServers`` parameter.

Permission flow:
  ← session/request_permission (server→client REQUEST with uuid id)
  → {result: {outcome: {outcome: "selected", optionId: "allow_once"}}}
"""

from __future__ import annotations

import asyncio  # noqa: F401
import functools  # noqa: F401
import glob
import hashlib  # noqa: F401
import importlib
import json  # noqa: F401
import logging
import os
import re  # noqa: F401
import shutil
import signal  # noqa: F401
import stat
import subprocess as subprocess_mod
import sys
import tempfile  # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
from collections import deque  # noqa: F401
from contextlib import aclosing, suppress  # noqa: F401
from pathlib import Path, PureWindowsPath  # noqa: F401
from types import ModuleType
from typing import (  # noqa: F401
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Mapping,
    TypeVar,
)

from kiro_crew import (  # noqa: F401
    __version__,
    _facade_mirror,
    acp_tool_gate,
    agent_scratch,
    model_registry,
    model_scope,
    permission_floor,
    pinned_fs,
    platform_compat,
)
from kiro_crew import sel as sel_module  # noqa: F401
from kiro_crew.acp import client_claude_seed as _client_claude_seed  # noqa: F401
from kiro_crew.acp import client_claude_session as _client_claude_session  # noqa: F401
from kiro_crew.acp import client_lifecycle as _client_lifecycle  # noqa: F401
from kiro_crew.acp import client_mcp_array as _client_mcp_array  # noqa: F401
from kiro_crew.acp import client_model_selection as _client_model_selection  # noqa: F401
from kiro_crew.acp import client_permissions as _client_permissions  # noqa: F401
from kiro_crew.acp import client_seed_ownership as _client_seed_ownership  # noqa: F401
from kiro_crew.acp import client_spawn_env as _client_spawn_env  # noqa: F401
from kiro_crew.acp import client_start as _client_start
from kiro_crew.acp import client_tool_events as _client_tool_events  # noqa: F401
from kiro_crew.acp import client_transport as _client_transport  # noqa: F401
from kiro_crew.acp import client_turn as _client_turn  # noqa: F401
from kiro_crew.acp import client_turn_control as _client_turn_control  # noqa: F401
from kiro_crew.acp import client_usage as _client_usage  # noqa: F401
from kiro_crew.acp import launch as launch_mod  # noqa: F401
from kiro_crew.acp import managed_agent_shadow as managed_shadow_mod  # noqa: F401
from kiro_crew.acp import (  # noqa: F401
    runtime_models,
    runtime_process_tree,
    seed_provenance,
    transport_framing,
)
from kiro_crew.acp._dispatch import (  # noqa: F401
    ACP_BACKENDS_META_IDENTITY,
    DRAIN_YIELD_AFTER_S,
    MAX_ACTIVE_TOOL_CALLS,
    BackgroundLaunchRecord,
    InFlightIdSet,
    _dumps_degraded,
    _loggable_request_id,
    _measure_tool_output,
    agent_version_from_init,
    build_permission_event,
    build_session_new_params,
    classify_tool_call,
    derive_edit_diff,
    error_is_refusal_terminal,
    extract_tool_purpose,
    gate_envelope,
    harness_tool_name,
)
from kiro_crew.acp._dispatch import identified_mcp_call as _identified_mcp_call  # noqa: F401
from kiro_crew.acp._dispatch import is_mcp_tool_approval as _is_mcp_tool_approval  # noqa: F401
from kiro_crew.acp._dispatch import (  # noqa: F401
    log_unrenderable_content,
    make_unified_diff,
    meta_builtin_server_names,
    parse_claude_compaction_notice,
    parse_codex_compaction_update,
    parse_message_id,
    parse_prompt_token_usage,
    parse_refusal,
    parse_session_modes,
    parse_usage_cost,
    parse_usage_update,
    redact_text,
)
from kiro_crew.acp._dispatch import select_tool_title as _select_tool_title  # noqa: F401
from kiro_crew.acp._dispatch import (  # noqa: F401
    tool_call_content_text,
)
from kiro_crew.acp._frame_record import record_frame  # noqa: F401
from kiro_crew.acp.agent_env import agent_env_overlay  # noqa: F401
from kiro_crew.acp.child_env_defaults import apply_child_env_defaults
from kiro_crew.acp.client_class import AcpClient  # noqa: F401
from kiro_crew.acp.client_claude_seed import _claude_settings_usable  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_CARRIED_PERMISSION_KEYS  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_CREW_GOVERNED_SETTING_SOURCES  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_GATE_ESCAPING_MODES  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_PINNED_MODE  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_PROJECT_SETTINGS_FILES  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_PROJECT_SETTINGS_MAX_BYTES  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_PROJECT_UNCARRIED_GUARD_KEYS  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_SIDE_READ_ONLY_ASK_RULES  # noqa: F401
from kiro_crew.acp.client_claude_session import _GITIGNORE_PATTERN_CHARS  # noqa: F401
from kiro_crew.acp.client_claude_session import CLAUDE_ACP_FORK_POINT_MIN_VERSION  # noqa: F401
from kiro_crew.acp.client_claude_session import CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION  # noqa: F401
from kiro_crew.acp.client_claude_session import CLAUDE_ACP_STEERING_MIN_VERSION  # noqa: F401
from kiro_crew.acp.client_claude_session import _adapter_version_at_least  # noqa: F401
from kiro_crew.acp.client_claude_session import _claude_root_pattern  # noqa: F401
from kiro_crew.acp.client_claude_session import _claude_side_read_deny_rules  # noqa: F401
from kiro_crew.acp.client_claude_session import _project_settings_restrictions  # noqa: F401
from kiro_crew.acp.client_claude_session import (  # noqa: F401
    _claude_adapter_honours_setting_sources,
)
from kiro_crew.acp.client_lifecycle import _SUPPRESSED_STDERR_MARKERS  # noqa: F401
from kiro_crew.acp.client_lifecycle import _T  # noqa: F401
from kiro_crew.acp.client_model_selection import _JSONRPC_INVALID_PARAMS  # noqa: F401
from kiro_crew.acp.client_model_selection import _is_config_value_rejection  # noqa: F401
from kiro_crew.acp.client_model_selection import _push_model_via_effort_split  # noqa: F401
from kiro_crew.acp.client_permissions import GOOSE_VERIFIED_VERSION_PREFIX  # noqa: F401
from kiro_crew.acp.client_permissions import PI_ACP_VERIFIED_VERSION  # noqa: F401
from kiro_crew.acp.client_permissions import _goose_versions_noted  # noqa: F401
from kiro_crew.acp.client_permissions import _pi_adapter_versions_noted  # noqa: F401
from kiro_crew.acp.client_permissions import _scrub_observed  # noqa: F401
from kiro_crew.acp.client_spawn_env import _CODEX_ACP_DEP_MARKER  # noqa: F401
from kiro_crew.acp.client_spawn_env import _CODEX_ACP_PKG_ENTRY  # noqa: F401
from kiro_crew.acp.client_spawn_env import _ENV_CODEX_ACP_BIN  # noqa: F401
from kiro_crew.acp.client_spawn_env import _KIRO_CLI_BUNDLE_EXECUTABLE  # noqa: F401
from kiro_crew.acp.client_spawn_env import _MACOS_APP_BUNDLE_SUFFIX  # noqa: F401
from kiro_crew.acp.client_spawn_env import _MACOS_APP_BUNDLE_TAIL  # noqa: F401
from kiro_crew.acp.client_spawn_env import CODEX_ACP_BIN  # noqa: F401
from kiro_crew.acp.client_spawn_env import CODEX_ACP_NPM_PKG  # noqa: F401
from kiro_crew.acp.client_spawn_env import CREDENTIAL_POINTER_ENV_VARS  # noqa: F401
from kiro_crew.acp.client_spawn_env import IDENTITY_STORE_ROOT_ENV_VARS  # noqa: F401
from kiro_crew.acp.client_spawn_env import KIRO_CLI_BIN  # noqa: F401
from kiro_crew.acp.client_spawn_env import KIRO_CLI_SUBCMD  # noqa: F401
from kiro_crew.acp.client_spawn_env import _apply_pod_home_remap  # noqa: F401
from kiro_crew.acp.client_spawn_env import _codex_acp_argv_cache  # noqa: F401
from kiro_crew.acp.client_spawn_env import _exit_origin  # noqa: F401
from kiro_crew.acp.client_spawn_env import _kiro_cli_app_bundle_target  # noqa: F401
from kiro_crew.acp.client_spawn_env import _kiro_cli_bundle_binary  # noqa: F401
from kiro_crew.acp.client_spawn_env import _KiroExecutableTrustError  # noqa: F401
from kiro_crew.acp.client_spawn_env import _launch_tools  # noqa: F401
from kiro_crew.acp.client_spawn_env import _resolve_codex_acp_bin  # noqa: F401
from kiro_crew.acp.client_spawn_env import _resolve_kiro_bin  # noqa: F401
from kiro_crew.acp.client_spawn_env import _resolve_kiro_bin_for_spawn  # noqa: F401
from kiro_crew.acp.client_spawn_env import apply_pod_bundle_spawn  # noqa: F401
from kiro_crew.acp.client_spawn_env import codex_acp_not_found_message  # noqa: F401
from kiro_crew.acp.client_spawn_env import finish_suspended_spawn  # noqa: F401
from kiro_crew.acp.client_spawn_env import kiro_cli_not_found_message  # noqa: F401
from kiro_crew.acp.client_start import _PROTOCOL_VERSION_BY_BACKEND  # noqa: F401
from kiro_crew.acp.client_start import CLIENT_NAME  # noqa: F401
from kiro_crew.acp.client_start import CLIENT_VERSION  # noqa: F401
from kiro_crew.acp.client_start import PROTOCOL_VERSION  # noqa: F401
from kiro_crew.acp.client_start import PROTOCOL_VERSION_CLAUDE  # noqa: F401
from kiro_crew.acp.client_start import PROTOCOL_VERSION_DEEPSEEK  # noqa: F401
from kiro_crew.acp.client_start import PROTOCOL_VERSION_GOOSE  # noqa: F401
from kiro_crew.acp.client_start import PROTOCOL_VERSION_OPENCODE  # noqa: F401
from kiro_crew.acp.client_start import PROTOCOL_VERSION_PI  # noqa: F401
from kiro_crew.acp.client_timeouts import _ACP_RESPAWN_BACKOFF_S  # noqa: F401
from kiro_crew.acp.client_timeouts import _CANCEL_GRACE_SECS  # noqa: F401
from kiro_crew.acp.client_timeouts import _COMPACTION_FAILED_TURN_BUDGET  # noqa: F401
from kiro_crew.acp.client_timeouts import _CONCURRENT_STEER_CANCEL_SECS  # noqa: F401
from kiro_crew.acp.client_timeouts import _CONCURRENT_STEER_GRACE_SECS  # noqa: F401
from kiro_crew.acp.client_timeouts import _DEFAULT_PROMPT_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _DRAIN_DURATION  # noqa: F401
from kiro_crew.acp.client_timeouts import _DRAIN_IDLE_EXIT  # noqa: F401
from kiro_crew.acp.client_timeouts import _INIT_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _MAX_CONCURRENT_STEER_CHARS  # noqa: F401
from kiro_crew.acp.client_timeouts import _MAX_CONCURRENT_STEERS  # noqa: F401
from kiro_crew.acp.client_timeouts import _MAX_CONSECUTIVE_EMPTY  # noqa: F401
from kiro_crew.acp.client_timeouts import _OPENCODE_PERMISSION_REFINEMENT_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _POST_COMPACTION_METADATA_GRACE_SECS  # noqa: F401
from kiro_crew.acp.client_timeouts import _PROMPT_TIMEOUT_MARGIN_SECS  # noqa: F401
from kiro_crew.acp.client_timeouts import _READ_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _SEL_AUDIT_TIMEOUT_SECONDS  # noqa: F401
from kiro_crew.acp.client_timeouts import _STALE_TURN_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _STEERING_WAIT_SLICE_SECS  # noqa: F401
from kiro_crew.acp.client_timeouts import _SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS  # noqa: F401
from kiro_crew.acp.client_timeouts import _TOOL_STALL_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _WAIT_RESPONSE_MAX_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _effective_prompt_timeout  # noqa: F401
from kiro_crew.acp.client_timeouts import _effective_prompt_timeout_async  # noqa: F401
from kiro_crew.acp.client_timeouts import prompt_timeout_for_ceiling  # noqa: F401
from kiro_crew.acp.client_timeouts import resolve_prompt_timeout  # noqa: F401
from kiro_crew.acp.client_timeouts import resolve_prompt_timeout_for_deadline  # noqa: F401
from kiro_crew.acp.client_tool_events import _ACP_SHELL_KIND  # noqa: F401
from kiro_crew.acp.client_tool_events import _MAX_CACHED_TOOL_PARAMS  # noqa: F401
from kiro_crew.acp.client_tool_events import _is_shell_kind  # noqa: F401
from kiro_crew.acp.client_tool_events import _make_unified_diff  # noqa: F401
from kiro_crew.acp.client_transport import _is_safe_oauth_url  # noqa: F401
from kiro_crew.acp.client_transport import _jsonrpc_error_code  # noqa: F401
from kiro_crew.acp.client_turn import _TOOL_INTERRUPTED_MARKER  # noqa: F401
from kiro_crew.acp.client_turn import _is_tool_interrupted_marker  # noqa: F401
from kiro_crew.acp.client_turn_control import _CLIENT_STEERING_REQUEST_METHODS  # noqa: F401
from kiro_crew.acp.client_turn_control import _GOOSE_SESSION_STEER  # noqa: F401
from kiro_crew.acp.client_turn_control import _ConcurrentSteerTurn  # noqa: F401
from kiro_crew.acp.client_turn_control import format_command_result  # noqa: F401
from kiro_crew.acp.client_turn_control import parse_slash_command  # noqa: F401
from kiro_crew.acp.client_turn_control import steering_advertised  # noqa: F401
from kiro_crew.acp.harness import claude as claude_mod  # noqa: F401
from kiro_crew.acp.harness import pi as pi_mod  # noqa: F401
from kiro_crew.acp.harness import process_adapter_for  # noqa: F401
from kiro_crew.acp.harness.base import SpawnContext, SpawnPlan  # noqa: F401
from kiro_crew.acp.harness_tool_names import CLAUDE_READ_ONLY_BUILTINS  # noqa: F401
from kiro_crew.acp.launch import LaunchRequest, LaunchTools, launch  # noqa: F401
from kiro_crew.acp.liveness import (  # noqa: F401
    EVIDENCE_SAMPLING,
    VERDICT_WORKING,
    LivenessOracle,
    _consume_future_exception,
    consult_offloaded,
)
from kiro_crew.acp.mcp_ref_guard import (  # noqa: F401
    warn_remote_header_secret_refs,
    warn_unresolved_server_refs,
)
from kiro_crew.acp.mcp_session_report import BUCKET_CAP as MCP_REPORT_BUCKET_CAP  # noqa: F401
from kiro_crew.acp.mcp_session_report import ERROR_CAP as MCP_REPORT_ERROR_CAP  # noqa: F401
from kiro_crew.acp.mcp_session_report import NAME_CAP as MCP_REPORT_NAME_CAP  # noqa: F401
from kiro_crew.acp.mcp_session_report import McpSessionReport, sanitize_sink_text  # noqa: F401
from kiro_crew.acp.mcp_startable import PrunedServer, prune_unstartable  # noqa: F401
from kiro_crew.acp.prompt_blocks import build_prompt_blocks  # noqa: F401
from kiro_crew.acp.runtime_models import (  # noqa: F401
    DEFAULT_MODEL,
    _extract_advisory_detail,
    _is_model_substitution_advisory,
    _substitute_model_from_advisory,
    model_is_unusable,
    pick_served_default,
)
from kiro_crew.acp.runtime_process_tree import ChildRecord  # noqa: F401
from kiro_crew.acp.session_mcp import agent_spec_snapshot, session_mcp_deny_rules  # noqa: F401
from kiro_crew.acp.skill_reads import SkillReadTracker  # noqa: F401
from kiro_crew.acp.transport_errors import (  # noqa: F401
    AcpAuthRequired,
    AcpError,
    AcpModelUnavailable,
    AcpProcessDied,
    AcpSandboxInitFailed,
    AcpTimeoutError,
    AcpToolGateUnroutable,
    _raise_acp_error,
    compaction_failure_detail,
    compaction_failure_is_transient,
    is_sandbox_init_failure_output,
    registration_rate_limited_error,
    registration_throttle_line,
    sandbox_init_failure,
)
from kiro_crew.acp.transport_framing import (  # noqa: F401
    RequestWriteResult,
    _stall_window_phrase,
    response_write_window_secs,
    settle_drain,
    write_notification_best_effort,
    write_request_frame_bounded,
    write_response_frame_bounded,
)
from kiro_crew.acp.types import (  # noqa: F401
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    ACP_BACKENDS_FORK_AT_MESSAGE,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_INLINE_COMPACTION,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_LOAD_WITHOUT_MODES,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_MEMBER_PANEL,
    ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    ACP_BACKENDS_POD_HOME_REMAP,
    ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP,
    ACP_BACKENDS_RESUME_WITHOUT_LOAD,
    ACP_BACKENDS_SEED_LOCAL_SETTINGS,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEER_CONCURRENT_PROMPT,
    ACP_BACKENDS_STEERING_REQUEST,
    ACP_BACKENDS_STRUCTURED_REFUSAL,
    ACP_CLIENT_CAPABILITIES,
    EVENT_AGENT_SWITCHED,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_STEER_CONSUMED,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    JSONRPC_METHOD_NOT_FOUND,
    KIRO_CLI_CLIENT_APPLICATION,
    KNOWN_SESSION_UPDATES,
    MAX_STEERING_ANSWERS,
    MAX_STEERING_TEXT_CHARS,
    MCP_ROSTER_COMPLETE_NOTE,
    METHOD_AGENT_SWITCHED,
    METHOD_CANCEL,
    METHOD_CLEAR_STATUS,
    METHOD_COMMANDS_EXECUTE,
    METHOD_COMPACTION_STATUS,
    METHOD_INITIALIZE,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_METADATA,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_FORK,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_RESUME,
    METHOD_SESSION_STEERING,
    METHOD_SESSION_UPDATE,
    METHOD_SET_MODE,
    METHOD_SET_MODEL,
    METHOD_SUBAGENT_LIST_UPDATE,
    MODEL_CONFIG_ID,
    OPTION_ALLOW_ALWAYS,
    OPTION_ALLOW_ONCE,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    STEERING_ANSWER_WAIT_SECS,
    STEERING_INJECTED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_END_TURN,
    TERMINAL_TOOL_STATUSES,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_CONFIG_OPTION,
    UPDATE_CURRENT_MODE,
    UPDATE_SESSION_INFO,
    UPDATE_TOOL_CALL,
    UPDATE_USAGE,
    UPDATE_USER_MESSAGE_CHUNK,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
    JsonRpcRequest,
    effort_config_option_id,
    effort_config_option_value,
    model_registry_namespace,
    overlay_project_scope,
    steering_outcome,
)
from kiro_crew.agent import (  # noqa: F401
    DerivedSpecSnapshot,
    DerivedSpecStale,
    ForkGovernanceUnresolved,
    ensure_agent_materialized,
    require_fork_governance,
    require_fresh_derived_spec,
    require_unchanged_derived_spec,
)
from kiro_crew.agent_sdk import host_auth  # noqa: F401
from kiro_crew.agent_sdk.backends import (  # noqa: F401
    ACP_BACKEND_LAUNCH,
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
    ACP_BACKEND_PROCESS_NAMES,
    ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL,
    NODE_ADAPTER_ENTRY_SEGMENTS,
    launch_for,
    model_refusal_phrase,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for  # noqa: F401
from kiro_crew.atomic_write import atomic_write  # noqa: F401
from kiro_crew.browser_cli.launch import browser_session_env, browser_socket_env  # noqa: F401
from kiro_crew.config.paths import config_dir, kiro_sessions_dir  # noqa: F401
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS  # noqa: F401
from kiro_crew.dashboard.side_readonly_spec import unavailable_mode_explanation  # noqa: F401
from kiro_crew.env import (  # noqa: F401
    augmented_path,
    describe_search_path,
    resolve_krb5_ccname,
)
from kiro_crew.executors import subprocess_executor  # noqa: F401
from kiro_crew.hooks import (  # noqa: F401
    HOOK_EVENT_POST_TOOL_USE,
    fire_tool_hooks,
    get_global_hook_store,
)
from kiro_crew.identity_stores import IDENTITY_STORE_ROOTS  # noqa: F401
from kiro_crew.json_line import parse_json_object_line  # noqa: F401
from kiro_crew.jsonl_util import bounded_raw_records_with_offsets  # noqa: F401
from kiro_crew.kiro_cli import known_kiro_cli_dirs, resolve_kiro_cli  # noqa: F401
from kiro_crew.mcp_gateway.claim import (  # noqa: F401
    STUB_SESSION_TOKEN_ENV,
    mint_stub_session_token,
    schedule_claim,
)
from kiro_crew.mcp_gateway.session_servers import (  # noqa: F401
    attach_stub_session_token,
    injection_server_names,
    pooled_session_servers,
)
from kiro_crew.metrics.tool_calls import (  # noqa: F401
    note_tool_call_started,
    record_tool_call_finished,
)
from kiro_crew.owner_only_files import ensure_directory  # noqa: F401
from kiro_crew.platform.context import redact_log_via_context  # noqa: F401
from kiro_crew.providers.mirrors import MIRRORS, mirror_for  # noqa: F401
from kiro_crew.recovery.ladder import L3_ACP_RUNTIME as _L3_ACP_RUNTIME  # noqa: F401
from kiro_crew.recovery.ladder import LADDER as _LADDER  # noqa: F401
from kiro_crew.resource_status import inject_xdist_auto_cap  # noqa: F401
from kiro_crew.sandbox import (  # noqa: F401
    BoundWorkspaceMismatch,
    _forward_ssh_auth_sock,
    apply_windows_resource_ceiling,
    assert_voice_runtime_outside_agent_workspace,
    bind_voice_safe_agent_workspace_async,
    cgroup_scope_argv,
    create_subprocess_limited,
    delegated_workspace_exposes_sealed_target,
    release_bound_agent_workspace,
    resolve_bound_session_workspace,
    scrub_agent_subprocess_env,
    wrap_argv,
    wrap_argv_async,
    wrapped_by_crew_sandbox,
)
from kiro_crew.security import (  # noqa: F401
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
    sandbox_credential_targets,
)
from kiro_crew.security.credential_sources import tool_output_fingerprints  # noqa: F401
from kiro_crew.sel import sel  # noqa: F401
from kiro_crew.session_pid import _replace_child_pids  # noqa: F401
from kiro_crew.session_token_sig import schedule_session_token_publish  # noqa: F401
from kiro_crew.skill_usage import get_global_skill_read_observer  # noqa: F401  (facade surface)
from kiro_crew.trust_patterns import GrantDeadline, GrantEnded, grant_deadline_guard  # noqa: F401
from kiro_crew.turn_limit import current_turn_limit_secs  # noqa: F401
from kiro_crew.user_json import loads_user_json  # noqa: F401

logger = logging.getLogger(__name__)


def _mise_which(tool: str) -> str | None:
    """Ask mise for the resolved path of *tool*.

    Respects MISE_DATA_DIR, global config, and .mise.toml — works
    regardless of how the user configured their mise installation.
    Returns None if mise isn't installed or the tool isn't registered.
    """
    mise_bin = shutil.which("mise")
    if not mise_bin:
        return None
    try:
        result = subprocess_mod.run(
            [mise_bin, "which", tool],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            path = result.stdout.strip()
            if Path(path).is_file():
                return path
    except (subprocess_mod.TimeoutExpired, OSError):
        pass
    return None


def _resolve_ssh_auth_sock(env: dict[str, str]) -> None:
    """Ensure SSH_AUTH_SOCK points to a live agent socket.

    The gateway's inherited value may be stale after an ssh-agent restart.
    Re-discovers the current agent socket without spawning a login shell.

    - macOS: launchd listener path changes on reboot
    - Linux: ssh-agent sockets live under /tmp/ssh-*/agent.*
    - Windows: no-op — there is no ``SSH_AUTH_SOCK`` (Win32 OpenSSH agent uses a
      named pipe, which needs no repair). Bare ``os.getuid()`` below would also
      ``AttributeError`` on win32, so return early; this function runs in the
      spawn prelude for BOTH ACP backends.
    """
    if platform_compat.IS_WINDOWS:
        return

    current = env.get("SSH_AUTH_SOCK", "")
    if current and os.path.exists(current):
        return  # already valid

    if sys.platform == "darwin":
        patterns = [
            "/tmp/com.apple.launchd.*/Listeners",
            "/var/run/com.apple.launchd.*/Listeners",
        ]
    else:
        uid = os.getuid()
        patterns = [
            "/tmp/ssh-*/agent.*",
            f"/run/user/{uid}/ssh-agent.socket",
            f"/run/user/{uid}/keyring/ssh",
        ]

    for pattern in patterns:
        candidates = [p for p in glob.glob(pattern) if stat.S_ISSOCK(os.stat(p).st_mode)]
        if candidates:
            best = max(candidates, key=lambda p: os.path.getmtime(p))
            env["SSH_AUTH_SOCK"] = best
            logger.debug("Resolved SSH_AUTH_SOCK → %s", best)
            return


def _resolve_spawn_env(env: dict[str, str], *, kiro_api_key: bool = False) -> dict[str, str]:
    """Repair stale credential pointers in *env* before an agent spawn.

    Bundles :func:`_resolve_ssh_auth_sock` (glob + stat over ``/tmp``) and
    :func:`resolve_krb5_ccname` (lstat/stat of ``/tmp/krb5cc_<uid>``) so the
    spawn path pays ONE thread hop for both. Both resolvers issue synchronous
    filesystem syscalls whose latency scales with the ``/tmp`` entry count, so
    they must never run on the event loop — call this via
    ``asyncio.to_thread``. Mutates *env* in place and returns it for
    convenience.

    With ``kiro_api_key=True`` (the kiro-cli backend), also re-injects the
    CLI's own model credential from the data home's ``.env`` when the Docker
    entrypoint scrubbed it out of the parent environ — the child authenticates
    from its environment, so without this an API-key container loses model
    auth. With ``kiro_api_key=False`` (a foreign backend) the credential is
    actively STRIPPED instead: it is kiro-cli's alone, and the deny scrub
    deliberately exempts it, so an inherited copy would otherwise ride into a
    foreign agent process. The file read is IO, which is why both branches
    ride this same off-loop hop.

    A kiro-cli child (``kiro_api_key=True``) also gets
    ``agent.child_env_defaults`` for keys *env* does not already carry -- a
    config read, so it rides this hop too. The ``AcpRuntime`` spawn applies the
    same helper through the kiro-family harness hook instead.
    """
    _resolve_ssh_auth_sock(env)
    resolve_krb5_ccname(env)
    # Deferred import: this module keeps config.loader off its import graph
    # (in-file convention; see the _prompt_timeout lazy-import note).
    from kiro_crew.config.loader import inject_kiro_cli_api_key, strip_kiro_cli_api_key

    if kiro_api_key:
        inject_kiro_cli_api_key(env)
        apply_child_env_defaults(env)
    else:
        strip_kiro_cli_api_key(env)
    return env


# The session-start owner calls this at spawn time and cannot import this module.
_client_start._resolve_spawn_env = _resolve_spawn_env


# --------------------------------------------------------------------------- #
# Compatibility facade. The module-level code for the stdio framing, the error
# taxonomy and its classifiers, the model catalog and the process-tree helpers lives
# in the owner modules imported above, and every name that moved stays readable as
# ``kiro_crew.acp.client.<name>``:
#
# * A moved name this module's own code reads, and that nothing patches through
#   this module, is an ordinary import above: the owner's object, bound here.
# * Every other moved name is FORWARDED. ``__getattr__`` reads it from its owner,
#   and ``_facade_mirror`` sends a write or delete there, so a patch of
#   ``kiro_crew.acp.client.<name>`` reaches the owner's own callers. A forwarded name
#   is absent from this module's namespace on purpose -- a binding here would
#   shadow the owner for every later read -- and this module's code reads it as
#   ``<owner>.<name>``. A public package import only moved code reads is forwarded to
#   the owner that reads it, for the same reason, so a test that reads or patches it
#   through this module reaches that reader; a standard-library or typing import only
#   moved code reads is not re-exported.
# * The modules the moved process-tree helpers probe with (``platform_compat``,
#   ``sys``, ``Path``, ``subprocess_mod``) stay bound here. Each helper listed in the
#   facade test's ``_SEAM_IMPORTS`` imports them from this module when it runs, so a
#   test that rebinds one here reaches the moved helper as it did before the move.
#
# ``test/test_acp_refactor_facade.py`` pins both halves: a name a test patches
# through this module is forwarded, and a forwarded name is never bound here.
# --------------------------------------------------------------------------- #
#: Owner module -> every name this module forwards to it.
_EXPORTS_BY_OWNER: dict[str, tuple[str, ...]] = {
    "kiro_crew.acp.transport_framing": (
        "_STDOUT_BUFFER_LIMIT",
        "_OVERSIZE_DRAIN_MAX_BYTES",
        "OversizeLineUnrecoverable",
        "_drain_oversize_line",
        "_RESPONSE_WRITE_BOUND_SECS",
        "_RESPONSE_WRITE_MIN_PROGRESS_BYTES",
        "_RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS",
        "_is_proactor_loop",
        "_level_is_progress_signal",
        "_pending_write_bytes",
        "await_under_no_progress_bound",
        "_release_if_acquired",
    ),
    "kiro_crew.acp.transport_errors": (
        "_COMPACTION_DETAIL_MAX_CHARS",
        "_COMPACTION_DETAIL_KEYS",
        "_COMPACTION_DETAIL_PLACEHOLDERS",
        "_COMPACTION_TRANSIENT_MARKERS",
        "_COMPACTION_WALK_MAX_DEPTH",
        "_walk_compaction_payload",
        "AcpConversationBindingMismatch",
        "AcpPermissionNeeded",
        "AcpRegistrationRateLimited",
        "AcpPromptBusy",
        "PiGateExtensionTampered",
        "_RE_MODEL_UNAVAILABLE",
        "_RE_MODEL_TEMP_UNAVAILABLE",
        "_RE_INVALID_MODEL_ID",
        "_RE_THROTTLE_NAMED",
        "_RE_THROTTLE_GENERIC",
        "_RE_AUTH",
        "_5XX_SEP",
        "_RE_5XX_NAMED",
        "_RE_5XX_STATUS",
        "_RE_CONNECTION",
        "_RE_5XX_HINT",
        "_RE_AUTH_STATUS",
        "_RE_SESSION_EXPIRED",
        "_RE_INVALID_BEARER",
        "_is_session_expired",
        "is_auth_failure_output",
        "_RE_SANDBOX_INIT_FAILURE",
        "sandbox_init_failure_for_runtime",
        "_RE_REGISTRATION_FAILED",
        "_RE_REGISTRATION_THROTTLE_MARK",
        "is_registration_throttle_output",
        "_RE_USAGE_LIMIT",
        "_RE_GENERATE_FAILED",
        "_RE_PROCESS_FAILED",
        "_RE_MALFORMED_REQUEST",
        "_RE_CONTEXT_WINDOW_OVERFLOW",
        "_RE_IMAGE_FORMAT_UNSUPPORTED",
        "_PROMPT_BUSY_RE",
        "_RE_STREAM_ENVELOPE",
        "_RE_TRAILING_REQ_ID",
        "_provider_detail",
        "_model_is_unentitled",
        "_is_transient_raw_error",
        "PROVIDER_ERROR_USAGE_LIMIT",
        "PROVIDER_ERROR_MALFORMED_REQUEST",
        "PROVIDER_ERROR_MODEL_UNAVAILABLE",
        "PROVIDER_ERROR_THROTTLE",
        "PROVIDER_ERROR_CREDENTIAL_PROPAGATION",
        "PROVIDER_ERROR_AUTH",
        "PROVIDER_ERROR_SESSION_EXPIRED",
        "PROVIDER_ERROR_CONNECTION",
        "PROVIDER_ERROR_HTTP_5XX",
        "PROVIDER_ERROR_UNKNOWN",
        "ProviderErrorClass",
        "classify_provider_error",
        "_auto_remedy",
        "_format_acp_error",
        "_rejected_model_from_error",
        "corroborate_launcher_refusal",
        "is_credential_propagation_delay",
        "ACP_BACKENDS_HOST_AUTH_CALLBACK",
        "LAUNCHER_EXIT_PREFIXES",
        "SANDBOX_LAYER_CREW",
        "SANDBOX_LAYER_HARNESS",
        "launcher_refusal",
        "sandbox_init_remediation",
    ),
    "kiro_crew.acp.runtime_models": (
        "advertised_model_ids",
        "resolve_pin_spelling",
        "resolve_pin_spelling_on",
        "catalog_row_would_drop",
        "resolve_usable_model",
        "_MODEL_SUBSTITUTION_ADVISORY_RE",
        "_MODEL_SUBSTITUTE_RE",
    ),
    "kiro_crew.acp.runtime_process_tree": (
        "_get_child_pids",
        "_direct_children",
        "_get_start_time",
        "_read_basename",
        "_capture_child_records",
        "_is_our_child",
        "_kill_escaped_children",
    ),
    "kiro_crew.acp.launch": (
        "_ADAPTER_INTERPRETERS",
        "_is_adapter_package_entry",
        "_named_by_override",
        "_adapter_spawn_label",
        "_normalize_exe_casing",
        "_mise_node_installs_dir",
        "_resolve_node_for_script",
        "_UNRESOLVED",
        "_vendored_acp_roots",
        "_resolve_node_adapter_argv",
        "_node_module_search_dirs",
        "_vendored_adapter_entry",
        "_self_served_bin_caches",
        "_resolution_generation",
        "_resolution_epoch",
        "bump_resolution_generation",
        "_resolve_self_served_bin",
        "_SANDBOX_PREFLIGHT_TIMEOUT",
        "_sandbox_preflight",
        "_run_preflight_bounded",
        "_unlink_readback_launcher",
        "_READBACK_STDERR_SCAN_CHARS",
        "_READBACK_FAULT_MAX_SHAPES",
        "_READBACK_FAULT_SHAPES",
        "_readback_stderr_diagnosis",
        "_readback_detail_with_diagnosis",
        "mise_data_dir",
        "RLIMIT_PROFILE_SESSION_HOST",
        "KIROCREW_SPAWNED_ENV",
        "KIROCREW_SPAWNED_VALUE",
    ),
    "kiro_crew.acp.harness.claude": (
        "CLAUDE_ACP_BIN",
        "CLAUDE_CODE_BIN",
        "CLAUDE_ACP_NPM_PKG",
        "_CLAUDE_ACP_PKG_ENTRY",
        "_CLAUDE_ACP_DEP_MARKER",
        "_claude_acp_argv_cache",
        "_resolve_vendored_claude_acp",
        "_resolve_claude_acp_bin",
        "_resolve_claude_code_executable",
        "_claude_adapter_installed_version",
    ),
    "kiro_crew.acp.harness.opencode": (
        "_OPENCODE_LAUNCH",
        "OPENCODE_BIN",
        "_ENV_OPENCODE_CONFIG_CONTENT",
        "OPENCODE_INSTALL_COMMAND",
        "_OPENCODE_CONFIG_READBACK_ARGS",
        "_OPENCODE_READBACK_TIMEOUT_S",
        "_opencode_readback_remedy",
        "_opencode_uniform_permission",
        "_opencode_rules_deny",
        "_opencode_config_mcp_server_names",
        "_opencode_config_mcp_servers_remedy",
        "_opencode_agent_permissions",
        "MAX_HARNESS_CONFIG_MCP_SERVERS",
        "MAX_HARNESS_TOOL_NAME_LEN",
        "opencode_rewrites_name",
    ),
    "kiro_crew.acp.harness.goose": (
        "_ENV_GOOSE_MODE",
        "_GOOSE_BUILTIN_ARG",
        "_GOOSE_BUILTIN_DEVELOPER",
    ),
    "kiro_crew.acp.harness.pi": (
        "PI_ACP_BIN",
        "PI_ACP_NPM_PKG",
        "_PI_ACP_PKG_ENTRY",
        "_PI_ACP_DEP_MARKER",
        "_ENV_PI_ACP_BIN",
        "PI_BIN",
        "PI_NPM_PKG",
        "_ENV_PI_ACP_PI_COMMAND",
        "PI_INSTALL_COMMAND",
        "_PI_RPC_ARGS",
        "_PI_EXTENSION_FLAG",
        "_PI_READBACK_REQUEST",
        "_PI_READBACK_TIMEOUT_S",
        "PI_MIN_VERSION",
        "_PI_NPM_PACKAGE_NAMES",
        "_PI_MANIFEST_SEARCH_DEPTH",
        "_ENV_PI_GATE_SESSION",
        "PI_GATE_EXTENSION_SHA256",
        "_pi_acp_argv_cache",
        "_pi_bin_cache",
        "_pi_gate_launcher_cache",
        "_resolve_pi_acp_bin",
        "_resolve_pi_bin",
        "_pi_installed_version",
        "_pi_version_issue",
        "pi_gate_extension_path",
        "_pi_gate_artifact_dir",
        "_pi_gate_extension_bytes",
        "_seal_pi_gate_extension",
        "_seal_gate_extension",
        "_publish_gate_artifact",
        "_pi_gate_launcher_body",
        "_ensure_pi_gate_launcher",
        "_pi_readback_remedy",
        "_same_file_spelling",
        "_same_file_spelling_all",
        "_pi_commands_from_readback",
        "agent_sdk",
    ),
    "kiro_crew.acp.harness.deepseek": (
        "_DSH_PATCH_FLAG",
        "_ENV_DSH_GATE_SESSION",
        "_ENV_DSH_GATE_MARKER",
        "_ENV_DSH_GATE_SCRUB_NAMES",
        "_DSH_GATE_SCRUB_CANARY_PREFIX",
        "_DSH_GATE_READBACK_TIMEOUT_S",
        "_DSH_GATE_MARKER_POLL_S",
        "_DSH_GATE_PROBE_EXIT_S",
        "_DSH_GATE_MARKER_MAX_BYTES",
        "DEEPSEEK_GATE_EXTENSION_SHA256",
        "_ENV_DEEPSEEK_PERMISSION_MODE",
        "DEEPSEEK_PERMISSION_MODE",
        "_DEEPSEEK_ENV_CHILD_SCRUB_CLASS",
        "_DEEPSEEK_ENV_NAME_GRAMMAR",
        "_DEEPSEEK_ENV_RESERVED_PREFIXES",
        "_DEEPSEEK_ENV_CREW_OWNED_NAMES",
        "deepseek_gate_extension_path",
        "_seal_deepseek_gate_extension",
        "_write_deepseek_gate_patch",
        "_validate_deepseek_env_mapping",
        "_deepseek_vault_env_names",
        "_deepseek_vault_env",
        "SECRET_URI_PREFIX",
        "resolve_secret_uris",
        "agent_env_scrub_prefixes",
    ),
}


def _index_exports() -> dict[str, str]:
    """Invert the owner table into forwarded name -> owner."""
    return {name: owner for owner, names in _EXPORTS_BY_OWNER.items() for name in names}


#: Forwarded name -> the dotted NAME of its owner, never the module object: the owner
#: is read from :data:`sys.modules` on each use, so a module purged and imported again
#: is seen at once instead of this table forwarding to the old copy. Bound empty
#: before the install below, which fills it with every name the mirror serves.
_EXPORTS: dict[str, str] = {}


def _owner(name: str) -> ModuleType:
    """Return the module that owns forwarded *name*, resolved on each access.

    ``importlib.import_module`` answers from :data:`sys.modules`, the one place a
    module is stored, so a purged or replaced owner is seen at once; and it waits on
    that module's import lock while its body is still running, where a bare
    ``sys.modules`` read would hand a second thread a half-built owner.
    """
    return importlib.import_module(_EXPORTS[name])


# Hidden from type checkers: mypy types every unknown attribute of a module that
# defines ``__getattr__`` as ``Any``, so a mistyped ``client.<name>`` would type-check.
# mypy sees the forwarded names through the ``TYPE_CHECKING`` imports below instead.
if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Read a name added to ``_EXPORTS`` after the install from its owner (:pep:`562`).

        ``_facade_mirror.MirroredModule`` answers every name the mirror serves, so this
        declines each of them: ``install`` refuses a resolver that answers one.
        """
        if name not in _EXPORTS or name in _MIRROR.served:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(_owner(name), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


# The ``client_*`` owner modules hold code that was this module's own: the
# ``AcpClient`` members the class binds, and the module-level names re-imported above,
# which this module keeps binding. ``_facade_mirror`` keeps the whole patch surface: a
# name in ``_EXPORTS`` is forwarded to its one owner exactly as before (read, write and
# delete there; ``dir()`` lists it), and a write of any other name this module binds
# reaches every client owner binding the same object -- the readers one global of this
# module had before the split. ``surface`` is this module's own namespace, so an
# owner's private helper is never part of the facade. ``test/test_client_bindings.py``
# pins the reach.
#: The client's owner modules, lowest layer first.
_CLIENT_OWNERS: tuple[str, ...] = (
    "kiro_crew.acp.client_timeouts",
    "kiro_crew.acp.client_spawn_env",
    "kiro_crew.acp.client_permissions",
    "kiro_crew.acp.client_claude_session",
    "kiro_crew.acp.client_claude_seed",
    "kiro_crew.acp.client_seed_ownership",
    "kiro_crew.acp.client_model_selection",
    "kiro_crew.acp.client_mcp_array",
    "kiro_crew.acp.client_tool_events",
    "kiro_crew.acp.client_usage",
    "kiro_crew.acp.client_transport",
    "kiro_crew.acp.client_lifecycle",
    "kiro_crew.acp.client_turn_control",
    "kiro_crew.acp.client_start",
    "kiro_crew.acp.client_turn",
    "kiro_crew.acp.client_class",
)
_MIRROR = _facade_mirror.install(
    sys.modules[__name__],
    _CLIENT_OWNERS,
    excluded=(),
    forwards=_index_exports(),
    forward_reads="import_module",
    surface=frozenset(globals()),
)
_EXPORTS.update(_MIRROR.served)

# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

# ``from kiro_crew.acp.client import *`` binds only the names this list holds: nothing
# can enumerate what ``__getattr__`` would serve, so a forwarded name reaches a star
# importer only by being listed here. The list is DERIVED from what this module binds
# plus the forwarding table, so it is not a third list of names to keep in step.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))

if TYPE_CHECKING:  # the forwarded names, visible to type checkers and IDEs
    from kiro_crew.acp.harness.claude import (  # noqa: F401
        _CLAUDE_ACP_DEP_MARKER,
        _CLAUDE_ACP_PKG_ENTRY,
        CLAUDE_ACP_BIN,
        CLAUDE_ACP_NPM_PKG,
        CLAUDE_CODE_BIN,
        _claude_acp_argv_cache,
        _claude_adapter_installed_version,
        _resolve_claude_acp_bin,
        _resolve_claude_code_executable,
        _resolve_vendored_claude_acp,
    )
    from kiro_crew.acp.harness.deepseek import (  # noqa: F401
        _DEEPSEEK_ENV_CHILD_SCRUB_CLASS,
        _DEEPSEEK_ENV_CREW_OWNED_NAMES,
        _DEEPSEEK_ENV_NAME_GRAMMAR,
        _DEEPSEEK_ENV_RESERVED_PREFIXES,
        _DSH_GATE_MARKER_MAX_BYTES,
        _DSH_GATE_MARKER_POLL_S,
        _DSH_GATE_PROBE_EXIT_S,
        _DSH_GATE_READBACK_TIMEOUT_S,
        _DSH_GATE_SCRUB_CANARY_PREFIX,
        _DSH_PATCH_FLAG,
        _ENV_DEEPSEEK_PERMISSION_MODE,
        _ENV_DSH_GATE_MARKER,
        _ENV_DSH_GATE_SCRUB_NAMES,
        _ENV_DSH_GATE_SESSION,
        DEEPSEEK_GATE_EXTENSION_SHA256,
        DEEPSEEK_PERMISSION_MODE,
        SECRET_URI_PREFIX,
        _deepseek_vault_env,
        _deepseek_vault_env_names,
        _seal_deepseek_gate_extension,
        _validate_deepseek_env_mapping,
        _write_deepseek_gate_patch,
        agent_env_scrub_prefixes,
        deepseek_gate_extension_path,
        resolve_secret_uris,
    )
    from kiro_crew.acp.harness.goose import (  # noqa: F401
        _ENV_GOOSE_MODE,
        _GOOSE_BUILTIN_ARG,
        _GOOSE_BUILTIN_DEVELOPER,
    )
    from kiro_crew.acp.harness.opencode import (  # noqa: F401
        _ENV_OPENCODE_CONFIG_CONTENT,
        _OPENCODE_CONFIG_READBACK_ARGS,
        _OPENCODE_LAUNCH,
        _OPENCODE_READBACK_TIMEOUT_S,
        MAX_HARNESS_CONFIG_MCP_SERVERS,
        MAX_HARNESS_TOOL_NAME_LEN,
        OPENCODE_BIN,
        OPENCODE_INSTALL_COMMAND,
        _opencode_agent_permissions,
        _opencode_config_mcp_server_names,
        _opencode_config_mcp_servers_remedy,
        _opencode_readback_remedy,
        _opencode_rules_deny,
        _opencode_uniform_permission,
        opencode_rewrites_name,
    )
    from kiro_crew.acp.harness.pi import (  # noqa: F401
        _ENV_PI_ACP_BIN,
        _ENV_PI_ACP_PI_COMMAND,
        _ENV_PI_GATE_SESSION,
        _PI_ACP_DEP_MARKER,
        _PI_ACP_PKG_ENTRY,
        _PI_EXTENSION_FLAG,
        _PI_MANIFEST_SEARCH_DEPTH,
        _PI_NPM_PACKAGE_NAMES,
        _PI_READBACK_REQUEST,
        _PI_READBACK_TIMEOUT_S,
        _PI_RPC_ARGS,
        PI_ACP_BIN,
        PI_ACP_NPM_PKG,
        PI_BIN,
        PI_GATE_EXTENSION_SHA256,
        PI_INSTALL_COMMAND,
        PI_MIN_VERSION,
        PI_NPM_PKG,
        _ensure_pi_gate_launcher,
        _pi_acp_argv_cache,
        _pi_bin_cache,
        _pi_commands_from_readback,
        _pi_gate_artifact_dir,
        _pi_gate_extension_bytes,
        _pi_gate_launcher_body,
        _pi_gate_launcher_cache,
        _pi_installed_version,
        _pi_readback_remedy,
        _pi_version_issue,
        _publish_gate_artifact,
        _resolve_pi_acp_bin,
        _resolve_pi_bin,
        _same_file_spelling,
        _same_file_spelling_all,
        _seal_gate_extension,
        _seal_pi_gate_extension,
        agent_sdk,
        pi_gate_extension_path,
    )
    from kiro_crew.acp.launch import (  # noqa: F401
        _ADAPTER_INTERPRETERS,
        _READBACK_FAULT_MAX_SHAPES,
        _READBACK_FAULT_SHAPES,
        _READBACK_STDERR_SCAN_CHARS,
        _SANDBOX_PREFLIGHT_TIMEOUT,
        _UNRESOLVED,
        KIROCREW_SPAWNED_ENV,
        KIROCREW_SPAWNED_VALUE,
        RLIMIT_PROFILE_SESSION_HOST,
        _adapter_spawn_label,
        _is_adapter_package_entry,
        _mise_node_installs_dir,
        _named_by_override,
        _node_module_search_dirs,
        _normalize_exe_casing,
        _readback_detail_with_diagnosis,
        _readback_stderr_diagnosis,
        _resolution_epoch,
        _resolution_generation,
        _resolve_node_adapter_argv,
        _resolve_node_for_script,
        _resolve_self_served_bin,
        _run_preflight_bounded,
        _sandbox_preflight,
        _self_served_bin_caches,
        _unlink_readback_launcher,
        _vendored_acp_roots,
        _vendored_adapter_entry,
        bump_resolution_generation,
        mise_data_dir,
    )
    from kiro_crew.acp.runtime_models import (  # noqa: F401
        _MODEL_SUBSTITUTE_RE,
        _MODEL_SUBSTITUTION_ADVISORY_RE,
        advertised_model_ids,
        catalog_row_would_drop,
        resolve_pin_spelling,
        resolve_pin_spelling_on,
        resolve_usable_model,
    )
    from kiro_crew.acp.runtime_process_tree import (  # noqa: F401
        _capture_child_records,
        _direct_children,
        _get_child_pids,
        _get_start_time,
        _is_our_child,
        _kill_escaped_children,
        _read_basename,
    )
    from kiro_crew.acp.transport_errors import (  # noqa: F401
        _5XX_SEP,
        _COMPACTION_DETAIL_KEYS,
        _COMPACTION_DETAIL_MAX_CHARS,
        _COMPACTION_DETAIL_PLACEHOLDERS,
        _COMPACTION_TRANSIENT_MARKERS,
        _COMPACTION_WALK_MAX_DEPTH,
        _PROMPT_BUSY_RE,
        _RE_5XX_HINT,
        _RE_5XX_NAMED,
        _RE_5XX_STATUS,
        _RE_AUTH,
        _RE_AUTH_STATUS,
        _RE_CONNECTION,
        _RE_CONTEXT_WINDOW_OVERFLOW,
        _RE_GENERATE_FAILED,
        _RE_IMAGE_FORMAT_UNSUPPORTED,
        _RE_INVALID_BEARER,
        _RE_INVALID_MODEL_ID,
        _RE_MALFORMED_REQUEST,
        _RE_MODEL_TEMP_UNAVAILABLE,
        _RE_MODEL_UNAVAILABLE,
        _RE_PROCESS_FAILED,
        _RE_REGISTRATION_FAILED,
        _RE_REGISTRATION_THROTTLE_MARK,
        _RE_SANDBOX_INIT_FAILURE,
        _RE_SESSION_EXPIRED,
        _RE_STREAM_ENVELOPE,
        _RE_THROTTLE_GENERIC,
        _RE_THROTTLE_NAMED,
        _RE_TRAILING_REQ_ID,
        _RE_USAGE_LIMIT,
        ACP_BACKENDS_HOST_AUTH_CALLBACK,
        LAUNCHER_EXIT_PREFIXES,
        PROVIDER_ERROR_AUTH,
        PROVIDER_ERROR_CONNECTION,
        PROVIDER_ERROR_CREDENTIAL_PROPAGATION,
        PROVIDER_ERROR_HTTP_5XX,
        PROVIDER_ERROR_MALFORMED_REQUEST,
        PROVIDER_ERROR_MODEL_UNAVAILABLE,
        PROVIDER_ERROR_SESSION_EXPIRED,
        PROVIDER_ERROR_THROTTLE,
        PROVIDER_ERROR_UNKNOWN,
        PROVIDER_ERROR_USAGE_LIMIT,
        SANDBOX_LAYER_CREW,
        SANDBOX_LAYER_HARNESS,
        AcpConversationBindingMismatch,
        AcpPermissionNeeded,
        AcpPromptBusy,
        AcpRegistrationRateLimited,
        PiGateExtensionTampered,
        ProviderErrorClass,
        _auto_remedy,
        _format_acp_error,
        _is_session_expired,
        _is_transient_raw_error,
        _model_is_unentitled,
        _provider_detail,
        _rejected_model_from_error,
        _walk_compaction_payload,
        classify_provider_error,
        corroborate_launcher_refusal,
        is_auth_failure_output,
        is_credential_propagation_delay,
        is_registration_throttle_output,
        launcher_refusal,
        sandbox_init_failure_for_runtime,
        sandbox_init_remediation,
    )
    from kiro_crew.acp.transport_framing import (  # noqa: F401
        _OVERSIZE_DRAIN_MAX_BYTES,
        _RESPONSE_WRITE_BOUND_SECS,
        _RESPONSE_WRITE_MIN_PROGRESS_BYTES,
        _RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS,
        _STDOUT_BUFFER_LIMIT,
        OversizeLineUnrecoverable,
        _drain_oversize_line,
        _is_proactor_loop,
        _level_is_progress_signal,
        _pending_write_bytes,
        _release_if_acquired,
        await_under_no_progress_bound,
    )
