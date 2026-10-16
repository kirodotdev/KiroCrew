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

import asyncio
import functools
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
import tempfile
import time
import uuid
from collections import deque
from contextlib import aclosing, suppress
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
from kiro_crew.acp import client_claude_seed as _client_claude_seed
from kiro_crew.acp import client_claude_session as _client_claude_session
from kiro_crew.acp import client_lifecycle as _client_lifecycle
from kiro_crew.acp import client_mcp_array as _client_mcp_array
from kiro_crew.acp import client_model_selection as _client_model_selection
from kiro_crew.acp import client_permissions as _client_permissions
from kiro_crew.acp import client_seed_ownership as _client_seed_ownership
from kiro_crew.acp import client_spawn_env as _client_spawn_env
from kiro_crew.acp import client_tool_events as _client_tool_events
from kiro_crew.acp import client_transport as _client_transport
from kiro_crew.acp import client_turn_control as _client_turn_control
from kiro_crew.acp import client_usage as _client_usage
from kiro_crew.acp import launch as launch_mod  # noqa: F401
from kiro_crew.acp import managed_agent_shadow as managed_shadow_mod
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
from kiro_crew.acp.client_turn_control import _CLIENT_STEERING_REQUEST_METHODS  # noqa: F401
from kiro_crew.acp.client_turn_control import _GOOSE_SESSION_STEER  # noqa: F401
from kiro_crew.acp.client_turn_control import _ConcurrentSteerTurn  # noqa: F401
from kiro_crew.acp.client_turn_control import format_command_result  # noqa: F401
from kiro_crew.acp.client_turn_control import parse_slash_command  # noqa: F401
from kiro_crew.acp.client_turn_control import steering_advertised  # noqa: F401
from kiro_crew.acp.harness import claude as claude_mod  # noqa: F401
from kiro_crew.acp.harness import pi as pi_mod  # noqa: F401
from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.acp.harness.base import SpawnContext, SpawnPlan
from kiro_crew.acp.harness_tool_names import CLAUDE_READ_ONLY_BUILTINS  # noqa: F401
from kiro_crew.acp.launch import LaunchRequest, LaunchTools, launch  # noqa: F401
from kiro_crew.acp.liveness import (
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
from kiro_crew.acp.prompt_blocks import build_prompt_blocks
from kiro_crew.acp.runtime_models import (  # noqa: F401
    DEFAULT_MODEL,
    _extract_advisory_detail,
    _is_model_substitution_advisory,
    _substitute_model_from_advisory,
    model_is_unusable,
    pick_served_default,
)
from kiro_crew.acp.runtime_process_tree import ChildRecord
from kiro_crew.acp.session_mcp import agent_spec_snapshot, session_mcp_deny_rules  # noqa: F401
from kiro_crew.acp.skill_reads import SkillReadTracker
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
from kiro_crew.agent import (
    DerivedSpecSnapshot,
    DerivedSpecStale,
    ForkGovernanceUnresolved,
    ensure_agent_materialized,
    require_fork_governance,
    require_fresh_derived_spec,
    require_unchanged_derived_spec,
)
from kiro_crew.agent_sdk import host_auth
from kiro_crew.agent_sdk.backends import (  # noqa: F401
    ACP_BACKEND_LAUNCH,
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
    ACP_BACKEND_PROCESS_NAMES,
    ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL,
    NODE_ADAPTER_ENTRY_SEGMENTS,
    launch_for,
    model_refusal_phrase,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.atomic_write import atomic_write  # noqa: F401
from kiro_crew.browser_cli.launch import browser_session_env, browser_socket_env  # noqa: F401
from kiro_crew.config.paths import config_dir, kiro_sessions_dir
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS  # noqa: F401
from kiro_crew.dashboard.side_readonly_spec import unavailable_mode_explanation
from kiro_crew.env import (  # noqa: F401
    augmented_path,
    describe_search_path,
    resolve_krb5_ccname,
)
from kiro_crew.executors import subprocess_executor
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
from kiro_crew.owner_only_files import ensure_directory
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

#: The name sent as ``clientInfo.name``. It IS the kiro-cli client-application
#: tag, so one filter string finds Crew on both channels and the two cannot drift.
CLIENT_NAME = KIRO_CLI_CLIENT_APPLICATION
#: The version reported to the agent host in ``initialize``'s ``clientInfo``, and
#: from there into the host's own telemetry (``acp_client_version``). It is the
#: PACKAGE version, never a literal of its own: a hand-maintained literal is a
#: second version number nobody bumps, and this one was not -- it sat at
#: ``"0.1.2"`` from the first commit that named it while the product shipped
#: 0.2.0 through 0.8.0, so every Crew-driven session of every release reported
#: one indistinguishable version and no version split of Crew traffic was
#: possible. Reading ``__version__`` also inherits what that attribute already
#: resolves at import: a release lane's rewritten literal AND a repackager's
#: ``BUILD_VERSION`` stamp.
CLIENT_VERSION = __version__
# kiro-cli uses a date-stamped protocol; claude-agent-acp follows the
# upstream ACP SDK (numeric integer, currently 1).  See acp.types.
PROTOCOL_VERSION = "2025-08-22"
PROTOCOL_VERSION_CLAUDE = 1
# codex-acp has no literal here, and its absence is the point: this core drives
# only the harnesses in the table below, and codex is not one of them. Its
# handshake dialect is declared by ``acp.harness.codex``, on the core that does
# drive it. One harness, one declaration -- a second copy on a core that never
# performs the handshake is a copy nothing can keep honest.
# OpenCode answers ``initialize`` with an integer ``protocolVersion`` of 1, so it
# speaks the SPEC dialect rather than kiro-cli's date-stamped one. Verified off its
# own wire, and its own literal for the same reason codex has one (harness-parity
# H10): a divergence should be a one-line edit here, not a silent downgrade of
# whichever harness moved first.
PROTOCOL_VERSION_OPENCODE = launch_for(ACP_BACKEND_OPENCODE).protocol_version
# pi-acp answers ``initialize`` with an integer ``protocolVersion`` of 1 as well,
# verified off its own wire; its own literal for the same H10 reason.
PROTOCOL_VERSION_PI = 1
# goose answers ``initialize`` with an integer ``protocolVersion`` of 1, captured off
# goose 1.50.1's own wire (``test/fixtures/acp_frames/goose/handshake-live.jsonl``);
# its own literal for the same H10 reason.
PROTOCOL_VERSION_GOOSE = launch_for(ACP_BACKEND_GOOSE).protocol_version
# DeepSeek Harness answers ``initialize`` with an integer ``protocolVersion`` of 1,
# so it speaks the SPEC dialect too. Verified off its own wire, and its own literal
# for the same reason the two above have one (harness-parity H10).
PROTOCOL_VERSION_DEEPSEEK = launch_for(ACP_BACKEND_DEEPSEEK).protocol_version
#: Handshake dialect per harness. A TABLE, not an if-chain: the handshake runs on
#: the construction path kiro-cli shares with every adapter, and harness-parity H13
#: keeps that path free of conditionals added in service of one. A harness added
#: later is one row here; an id with no row speaks kiro-cli's date-stamped dialect.
_PROTOCOL_VERSION_BY_BACKEND: dict[str, int | str] = {
    ACP_BACKEND_CLAUDE: PROTOCOL_VERSION_CLAUDE,
    ACP_BACKEND_PI: PROTOCOL_VERSION_PI,
    # Every harness whose own binary serves ACP declares its dialect in its
    # ``ACP_BACKEND_LAUNCH`` row, so those rows are read rather than restated here.
    # The two adapters above keep explicit rows: each is a separate package with
    # its own release cadence, and none has a launch record to read.
    **{backend: record.protocol_version for backend, record in sorted(ACP_BACKEND_LAUNCH.items())},
}


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


# Emitted by kiro-cli as a plain agent_message_chunk when its built-in, non-overridable
# security filter cancels every tool use in an assistant turn (e.g. shell commands
# containing "credentials").  After this text kiro-cli returns to an idle state waiting
# for the next user prompt and NEVER sends a ``complete`` response for the in-flight
# ``session/prompt`` — so without special handling Kiro Crew waits the full prompt timeout.
# Treating this chunk as end-of-turn unblocks the caller; the text itself is still
# yielded so the user/agent sees what happened.  We use an exact (stripped) match so
# the detection does not fire if the model merely quotes the marker string in prose.
_TOOL_INTERRUPTED_MARKER = "Tool uses were interrupted, waiting for the next user prompt"


def _is_tool_interrupted_marker(chunk: str) -> bool:
    """Exact match against the kiro-cli security-filter interrupt marker."""
    return chunk.strip() == _TOOL_INTERRUPTED_MARKER


class AcpClient:
    """JSON-RPC 2.0 client over stdio with kiro-cli acp."""

    # Written by the claude adapter at launch, read back by the settings writer
    # through ``getattr`` with an empty default: never set on any other host.
    _claude_adapter_disk_version: str
    # Fields first set by a member an owner module defines, declared so the class names them.
    _claude_shipped_restrictions: frozenset[tuple[str, str]]
    _entitlement_probe_inflight: asyncio.Future[tuple[list[dict[str, str]], float]] | None
    _member_dispatch_mounted: bool
    _session_mcp_excludes_local: bool
    _session_mcp_inline_settings: dict[str, Any] | None
    _stdin_lock: asyncio.Lock
    _steering_waiter_map: dict[int, asyncio.Future[Any]]
    _windows_tree_cleanup_failed: bool
    # The scrub this session applies to what a harness reports back, handed to the
    # host adapters that quote such a value (``ProcessSession._scrub_observed``).
    _scrub_observed = staticmethod(_scrub_observed)

    def __init__(
        self,
        work_dir: str | Path | None = None,
        model: str | None = None,
        agent: str = CLIENT_NAME,
        sandbox_mode: str = "auto",
        session_key: str | None = None,
        channel_id: str | None = None,
        extra_env: dict[str, str] | None = None,
        acp_backend: str = "",
        audit_source: str | None = None,
        mcp_gateway_overlay: str | Path | None = None,
        mcp_gateway_socket: str | Path | None = None,
        permission_mode: str | None = None,
        shared_scratch: Path | None = None,
        side_read_only: bool = False,
    ):
        if work_dir:
            self._work_dir = Path(work_dir)
        else:
            self._work_dir = config_dir() / "workspace"
        # Once-per-instance guard for the ensure_ready work-dir check: True
        # after the first (off-loop) mkdir, so the per-prompt warm path pays
        # no filesystem syscall at all.
        self._work_dir_ready = False
        self._model = model or DEFAULT_MODEL
        self._agent = agent
        self._sandbox_mode = sandbox_mode
        self.memory_mode = "persistent"
        self._acp_backend = acp_backend
        # Claude backend permission mode (Auto-mode / permission-UI parity).
        # Inert on the kiro-cli path. None = the backend's own default
        # ("default", i.e. every tool decision is forwarded to the host), which is
        # what _write_claude_local_settings leaves in place when nothing asked
        # for a mode.
        self._permission_mode = permission_mode
        # A Side Chat session that may run read-only tools. On claude it starts
        # with only the read built-ins and no MCP server, and its mode is pinned
        # to ``default`` (_claude_session_meta, _pin_claude_starting_mode). Inert
        # on the kiro-cli path, where the derived ``<agent>--readonly`` spec
        # confines the session instead.
        self._side_read_only = side_read_only
        # That session's sensitive-path deny rules, built off the loop before its
        # launch (_prepare_claude_side_read_denies). Empty until then, and a side
        # session whose envelope would carry none is not started.
        self._claude_side_read_denies: tuple[str, ...] = ()
        # The session tree's work directory when this client is a DEDICATED
        # subagent process spawned on a parent's behalf: re-validated at spawn
        # (``agent_scratch.shared_scratch_window``) and mounted as a second
        # private window beside this process's own scratch, which keeps the
        # temp triple and the kiro-cli log. ``None`` for a session that starts
        # its own tree. Once live, the client joins the tree's owner marker
        # beside its parent (``agent_scratch.adopt_owner``), so a parent that
        # dies first leaves no dead-owner marker over its running children.
        self._shared_scratch: Path | None = Path(shared_scratch) if shared_scratch else None
        self._scratch_dir: Path | None = None
        # True once this session has CREATED <work_dir>/.claude/settings.local.json
        # itself. Only then does reset remove it, and only then does a re-seed
        # overwrite it. The writer refuses a path that already holds a file it did
        # not author, so Crew never owns the undo for someone else's project
        # settings -- see _write_claude_local_settings.
        self._claude_settings_authored = False
        # True while the project owns settings.local.json and this session leaves
        # that file out of its setting sources, carrying Crew's settings inline in
        # ``_claude_inline_settings`` instead -- see _exclude_foreign_local_settings.
        self._claude_local_settings_excluded = False
        self._claude_inline_settings: dict[str, Any] | None = None
        # The exact bytes this session last wrote to that path, held as the str
        # whose utf-8 encoding IS those bytes -- the writer emits binary so no
        # newline translation can come between the two on any platform. Creating
        # the file is not sufficient ownership on its own: a user can replace it
        # atomically (write-temp + rename) after the create, and the replacement is
        # theirs. So the flag says "Crew created it" and this says "and it is still
        # Crew's content" -- the re-seed and the reset unlink both require BOTH.
        self._claude_settings_written: str | None = None
        # True while a durable OWNER holder of this client's is still on disk for a
        # file whose bytes are not Crew's: a user replaced the seed and the hand-back
        # (``forget``, then ``release``) could not reach the sidecar. Authorship
        # drops the moment the bytes are observed foreign -- governance is about the
        # file, not the record -- so this carries the one thing teardown still owes:
        # another attempt at the hand-back. It feeds NO governance decision.
        self._claude_settings_claim_unrevoked = False
        # True while this session holds a durable reader lease on a sibling
        # Crew client's seed. The lease pins that file's lifecycle even after a
        # later byte re-validation fails, so it is deliberately distinct from
        # whether the current bytes still govern this session's permission
        # surface. A sharer never removes the owner's file or live claim.
        self._claude_settings_shared = False
        # True only while the sibling seed's CURRENT on-disk bytes have passed
        # byte-for-byte validation against this session's rendered permission
        # surface. Unlike the retained reader lease above, every re-validation
        # clears this flag on entry and only full success re-earns it; the MCP
        # array is withheld after every non-success exit.
        self._permission_surface_share_validated = False
        # Identity this client claims its seed under, in the durable record. Two
        # keyless clients share the default work_dir, so a token is what keeps
        # "Crew wrote it" from collapsing into "any Crew client may take it": the
        # durable record is for adopting an ORPHAN, and a sibling still running in
        # this process does not have one. Per instance and never reset -- a client
        # that re-spawns is the same owner across spawns.
        self._seed_owner = uuid.uuid4().hex
        # This session's translated ``mcpServers`` array, resolved once per spawn.
        # Held here so the shared session-params call site is a pure in-memory
        # read: the translation touches disk, and doing that AT the call site
        # would put an executor hop on every backend's construction path
        # (harness-parity H13). None = not resolved yet; cleared on reset so the
        # next spawn re-reads the spec. See _session_mcp_servers.
        self._session_mcp_cache: list[dict[str, Any]] | None = None
        # The ``DerivedSpecSnapshot`` that array was built from, or None when this
        # session's agent mirrors nothing. For an array-backed host the array IS the
        # spec the host consumes -- it reads no spec of its own -- so the bracket the
        # kiro-cli path closes at ``initialize`` closes HERE at the ``session/new`` /
        # ``session/load`` response instead, against this snapshot. Resolved and
        # cleared together with the array, for the same per-spawn freshness reason.
        self._session_mcp_snapshot: DerivedSpecSnapshot | None = None
        # This session's agent spec, snapshotted once per spawn for the
        # unresolved-ref guard alone (see _guard_unresolved_mcp_refs). Held for
        # the same reason as the array above and read at the same kind of site:
        # the guard runs where the wire array is composed, which is shared with
        # kiro-cli, so the read cannot happen there. None = not snapshotted (or
        # unreadable), which makes the guard a no-op rather than a disk read on
        # the loop. Cleared on reset so the next spawn re-reads the spec.
        self._mcp_ref_spec: dict[str, Any] | None = None
        # What the pre-spawn freshness gate verified for THIS child, captured in
        # _spawn and re-verified in _initialize_session once the child has read its
        # spec. _spawn is the only writer and it runs before any handshake, so the
        # value the post-load half reads always belongs to the live process.
        # Deliberately NOT cleared on reset: a retained snapshot can only ever be
        # over-strict, while a None substituted there would short-circuit the
        # re-check on any path that reached it. None = no spawn yet.
        self._derived_spec_snapshot: DerivedSpecSnapshot | None = None
        # The mirror's CLIENT OBLIGATION from the same spec parse as the array
        # (``SessionProjection.denied_tools``): ``(server, tool)`` pairs the spec
        # switched off that the backend cannot refuse on the wire, so this client
        # refuses them when the backend asks permission (``_deny_spec_disabled_tool``).
        # A backend that honours the restriction natively (kiro-cli) or through a
        # file Crew writes (claude's ``permissions.deny``) leaves it empty, and an
        # empty set makes the refusal a no-op. Cleared on reset with the array.
        self._spec_denied_tools: frozenset[tuple[str, str]] = frozenset()
        # The mirror's other CLIENT OBLIGATION from the same parse
        # (``SessionProjection.zero_tools``): the spec declares ``"tools": []``, a
        # total ban the array and ``denied_tools`` above have no way to express --
        # they answer "which MCP servers mount", not "may this agent's own native
        # tools (the harness's built-in Bash/Read/Write) run at all". kiro-cli/KAS
        # honour the ban natively; every mirrored backend does not, so this client
        # refuses EVERY permission request when it is set (``_handle_permission``).
        # Cleared on reset with the array.
        self._spec_zero_tools: bool = False
        # Narrowed servers the projection withheld because THIS session cannot put
        # their per-tool rule in force (``SessionProjection.unhonoured_servers``). No
        # member mount may re-add one, whatever the backend's PerToolDeny says.
        self._session_mcp_unhonoured: frozenset[str] = frozenset()
        # Tool ids the projection asks the harness itself to deny
        # (``SessionProjection.harness_deny_rules``), seeded by the opencode routing.
        self._session_harness_deny_rules: tuple[str, ...] = ()
        # The subset of those rules the opencode read-back found IN FORCE, handed back
        # to every later projection so it withholds a server whose rule is not. None
        # until this spawn has seeded any.
        self._opencode_denies_in_force: frozenset[str] | None = None
        # The seeded rules the last read-back found outranked by another rule.
        self._opencode_denies_unenforced: frozenset[str] = frozenset()
        # Servers opencode mounts from its own config, from this spawn's read-back.
        self._opencode_native_mounts: frozenset[str] = frozenset()
        # The MCP server names opencode's routing read-back found in the harness's
        # own resolved config, so a fused tool title from one of them splits back
        # exactly. Recorded by its launch (``acp.harness.opencode``).
        self._opencode_config_mcp_servers: tuple[str, ...] = ()
        # The per-session nonce a gate extension echoes in every dialog it raises;
        # minted by the pi or DeepSeek Harness launch, placed in the child's
        # environment there, and the only key under which a permission frame is read
        # as an envelope.
        self._pi_gate_nonce = ""
        # The pi MCP handoff file the launch published and its bytes, kept so it can
        # be republished before each request that starts a pi process and removed
        # once the session is established (``acp.harness.pi``).
        self._pi_mcp_handoff: tuple[str, bytes] = ("", b"")
        # Every server name that handoff carried (the member mounts AND the pooled
        # stubs), which is what a pi MCP tool title is spelled after.
        self._pi_mcp_server_names: tuple[str, ...] = ()
        self._deepseek_gate_nonce = ""
        # toolCallIds the gate extension asked about in this session, read off the
        # envelopes; a completed tool call not in it is a call the gate never saw.
        self._pi_gate_asked_ids: set[str] = set()
        # request id -> toolCallId for every envelope the gate asked about, so a
        # reject answered by request id can name the call it denied; and the set
        # of denied calls, which must never reach ``completed``.
        self._pi_gate_request_tool: dict[str, str] = {}
        self._pi_gate_denied_ids: set[str] = set()
        self._session_key = session_key
        # When set, this client emits a per-tool-call SEL audit from the ACP
        # dispatch loop. Used by app/worker-pool clients (e.g. code-review-sage,
        # knowledge llm_pool) that have no external audit loop. Left None for
        # chat / subagent clients, which already audit via chat_runner /
        # SubagentManager, so they never double-log.
        self._audit_source = audit_source
        self._channel_id = channel_id
        self._extra_env = extra_env or {}
        # ``agent.env`` for the spawn in progress; see _agent_env_for_spawn.
        self._agent_env_snapshot: dict[str, str] | None = None
        # MCP gateway overlay: when set, the broker stubs in its rewritten specs
        # are injected into this session at ACP session/new, where they outrank
        # the same-named entries in the agent spec. Nothing is written to the
        # user's project or to ~/.kiro/agents. None = pooling off.
        # Broker requests carry the owning session through ordinary transport
        # authentication; member memory uses that session's execution record.
        self._mcp_gateway_overlay = str(mcp_gateway_overlay) if mcp_gateway_overlay else None
        self._mcp_gateway_socket = str(mcp_gateway_socket) if mcp_gateway_socket else None
        # Token this client's injected broker-stub entries carry, so gatewayd can
        # tell this session's stub connections from those of another session on
        # the same runtime PID (``mcp_gateway.claim.mint_stub_session_token``).
        # Minted once per client: one client drives one child process serving one
        # session at a time, and a warm-pool ``rekey()`` re-binds the SAME token
        # to the claiming session rather than re-minting — the stub processes
        # outlive a single chat, so a fresh token would leave the live ones
        # carrying a name no claim will ever mention again. Never logged.
        self._stub_session_token = mint_stub_session_token()
        self._sandbox_cleanup: str | None = None
        self._bound_workspace_fd: int | None = None
        self._spawn_work_dir = str(self._work_dir)
        self._process: asyncio.subprocess.Process | None = None
        self._pid: int | None = None
        self._kill_sent: tuple[int, str] | None = None  # (signal, reason) we last sent
        # The root's process-start identity, read once at spawn and handed to
        # both the session-file tracker and the identity-bound retirement in
        # _reset_state, so the line written and the line later compared are one
        # read of the identity rather than two (see session_pid._pid_start_token).
        self._spawn_start_token: str | None = None
        # False until shutdown confirms both the root's exit and every tracked
        # descendant's exit. A work-directory reclaim reads this fail-closed
        # verdict after shutdown.
        self._process_tree_confirmed_dead = False
        self._start_time: str | None = None  # start identity for PID-recycle detection
        # Names THIS spawn of the child, not the session it serves: a resume
        # re-uses the session id on a brand-new process (see ensure_ready's
        # session/load path), so the session id cannot distinguish the process
        # that minted a resource from a successor that cannot honor it. Fresh
        # per spawn, cleared with the process. See ``process_instance``.
        self._process_instance: str = ""
        self._session_id: str | None = None
        self._next_id = 1
        self._buffer: deque[JsonRpcMessage] = deque(maxlen=100)
        self._mcp_notifications: list[JsonRpcMessage] = []
        # What THIS session's MCP servers reported at init. The frames arrive
        # during _drain_notifications; reducing them to one log line and dropping
        # them would leave a session that started without a server unable to say
        # so. Read via mcp_session_report().
        self._mcp_report = McpSessionReport()
        #: Index into ``_mcp_notifications`` below which frames belong to a PRIOR
        #: session attempt and must not reach the report. See
        #: ``_begin_session_report``.
        self._mcp_report_frame_floor = 0
        #: Elements the last array build left out as unstartable, recorded on the
        #: report by ``_begin_session_report``. See ``_prune_unstartable_mcp``.
        self._mcp_pruned: tuple[PrunedServer, ...] = ()
        #: Names of the servers this attempt pruned, set by ``_begin_session_report``.
        #: The ref guard counts them as configured: each is already on the report as
        #: failed, and calling it unconfigured too would point at the projection.
        self._mcp_pruned_names: tuple[str, ...] = ()
        #: How many pruned servers the last array build had past
        #: ``MCP_REPORT_BUCKET_CAP``; counted on the report, never listed.
        self._mcp_pruned_omitted = 0
        # MCP OAuth requests collected during session init from
        # `_kiro.dev/mcp/oauth_request` notifications. Drained by callers via
        # `pop_pending_oauth_requests()` after `ensure_ready()` so the UI can
        # surface an Authorize button to the user. Each entry: {"serverName", "oauthUrl"}.
        self._pending_oauth_requests: list[dict[str, str]] = []
        # Server names already surfaced to the UI in this ACP session — kiro-cli
        # may emit `_kiro.dev/mcp/oauth_request` multiple times per server (e.g.
        # once per probe attempt). Dedupe so the user sees one banner per server.
        self._oauth_emitted_servers: set[str] = set()
        self._cancelled = False
        self._cancel_ts: float = 0.0
        # Cooperative-cancel read-grace for the CURRENT cancel. Defaults to the
        # module floor but is raised to the caller's ack budget by
        # cancel_session() so a configured soft_stop_budget_secs > 10 actually
        # extends the window instead of being silently capped (the read loop
        # would otherwise abort the turn at 10s while the soft waiter blocked
        # the full budget, then hard-kill — losing the session).
        self._cancel_grace_secs: float = _CANCEL_GRACE_SECS
        self._resume_session_id: str | None = None
        # (source session id, fork-point message id) for the next ensure_ready().
        self._fork_request: tuple[str, str] | None = None
        self._resumed = False
        self._can_load_session = False
        # agentInfo.version from the initialize response — the version the
        # spawned process runs, not the file on disk. "" until the handshake.
        self._agent_version = ""
        # Whether ``_agent_version`` has been read off a handshake at all, so an
        # adapter that reported NO version is told apart from one not yet asked.
        self._agent_version_read = False
        # Models advertised by the backend in the session/new (or session/load)
        # response. claude-agent-acp returns the real versioned Claude list
        # (Opus 4.8/4.7, Sonnet 4.6, …); kiro-cli returns its own. Captured so
        # the dashboard model dropdown reflects what the backend actually
        # offers rather than a hardcoded guess. Each entry: {modelId, name,
        # description}.
        self._available_models: list[dict[str, str]] = []
        # When that snapshot was captured (monotonic) and whether a live re-probe
        # confirmed it. A ``session/new`` answer is one unconfirmed reading taken
        # inside the startup race where an entitlement lookup can answer the
        # free-tier default; only :meth:`refresh_available_models` confirms it.
        self._available_models_captured_at: float = 0.0
        self._available_models_probe_confirmed: bool = False
        # Set by _capture_available_models (claude only) when the discovered ids
        # changed the cross-session provider-model cache, signalling the async
        # init path to offload a disk persist. Reset to False after each persist.
        self._advertised_models_changed: bool = False
        # Mode ids the backend advertised at session init (session/new|load
        # `modes.availableModes`). Empty when the backend omits `modes` (older
        # kiro-cli / offline fake) — the set_mode guard treats empty as "attempt"
        # for backward compatibility. Populated by _store_session_config.
        self._available_mode_ids: list[str] = []
        # Whether the backend advertised a `modes` list at all (even an empty
        # one). Distinguishes "unknown, attempt for backward compat" (False)
        # from "advertised zero/some modes, honor the list" (True) so an
        # explicitly-empty availableModes fails closed rather than attempting.
        self._modes_advertised: bool = False
        # Model kiro-cli/claude-agent-acp actually resolved to (may differ
        # from self._model when that's the "auto" sentinel). Used to look up
        # the context window when usage_update isn't sent (see _track_metadata).
        self._resolved_model_id: str | None = None
        # The model a ``_kiro.dev/agent/switched`` frame says the session now
        # runs, waiting to be judged by ``_judge_activated_model`` before the next
        # prompt. ``""`` = nothing to judge. See ``_note_agent_switched``.
        self._activated_model: str = ""
        # True from the start path's ``session/set_mode`` until the first
        # agent/switched frame after it, and whether a ``session/set_model``
        # went out in between (that later write is what the session runs).
        self._mode_frame_awaited: bool = False
        self._mode_frame_superseded: bool = False
        # Model the backend last substituted to via the -32603 admin-tier policy
        # advisory ("Using X instead"). Set by _wait_for_response when it sees the
        # advisory; consumed by the session/new path to re-issue creation on the
        # model the gateway will actually serve (the advisory carries no
        # sessionId, so the first attempt creates nothing). None = no substitution.
        self._last_substitution_model: str | None = None
        # The pinned model a startup config-option push was refused on, or a
        # startup pin kiro withheld because the account does not serve it, or
        # ``""``. Neither raises: the session stays on the backend default.
        # Callers that bill or label a turn by the pin read this to learn the
        # pin never ran.
        self.model_pin_refused: str = ""
        # The bare model a ``<model>[<effort>]`` pin landed as when its effort
        # half was refused, or ``""``. Set by ``_push_model_via_effort_split``.
        self.model_pin_partial: str = ""
        self._child_pids: dict[int, ChildRecord] = {}  # pid → (start_time, basename)
        self.last_prompt_stats = AcpPromptStats()
        self._tool_call_inputs: dict[str, str] = {}
        # Same-key provenance for the redacted display cache.  No removed bytes
        # are retained here; approval surfaces only need to know that the value
        # they received was not the complete command the provider requested.
        self._tool_call_input_redacted: dict[str, bool] = {}
        # Map toolCallId → is_shell, cached from the tool_call notification so
        # the later permission_request event (which carries no kind) can inherit
        # the canonical shell signal. Mirrors _tool_call_inputs lifecycle.
        self._tool_call_is_shell: dict[str, bool] = {}
        # Map toolCallId → "no channel classified this call". A tool_call frame
        # answers the question "is this a command?" through an ACP ``kind`` or
        # through a harness ``_meta`` channel; a frame carrying NEITHER leaves the
        # command gates with nothing to check, and the permission request that
        # follows carries no kind of its own. Remembered here so that request can
        # be refused rather than answered blind. Mirrors _tool_call_is_shell's
        # lifecycle.
        self._tool_call_unclassified: dict[str, bool] = {}
        # Skill bodies this session's tool calls read directly, credited to the
        # usage ledger once each read completes. Shared with AcpSessionHandle so
        # the credit does not depend on the transport. Mirrors
        # _tool_call_is_shell's lifecycle.
        self._skill_reads = SkillReadTracker()
        # Map toolCallId → trusted MCP server name (_meta.kiro.mcpServerName),
        # cached from the tool_call notification so the later permission_request
        # event (which carries no _meta) can inherit it — the signal the
        # app-own-server auto-approve keys on. Mirrors _tool_call_is_shell.
        self._tool_call_mcp_server: dict[str, str] = {}
        # Map toolCallId → trusted tool name (_meta.kiro.toolName), cached like
        # _tool_call_mcp_server so the permission_request event can rebuild the
        # canonical mcp__<server>__<tool> for per-tool governance in the
        # app-own-server auto-approve.
        self._tool_call_tool_name: dict[str, str] = {}
        # toolCallId -> the tool's own name its tool_call frame stated, for the
        # permission event's harness_tool_id (see _dispatch.harness_tool_name).
        self._tool_call_harness_tool_name: dict[str, str] = {}
        # toolCallId -> True when the tool_call frame PRESENTED an MCP identity half
        # the name bound refused (``mcp_identity_unreadable``); the permission
        # request is then refused in ``_handle_permission`` rather than judged
        # under the empty name. Mirrors AcpSessionHandle._tool_call_identity_unreadable.
        self._tool_call_identity_unreadable: dict[str, bool] = {}
        # Structured raw tool params (rawInput dict) keyed by toolCallId, cached
        # from the ToolCall notification so the later request_permission event —
        # which carries only a truncated title — can recover the real path/url
        # the governance gate needs (filesystem.write / network.egress scopes).
        self._tool_call_params: dict[str, dict] = {}
        # toolCallId -> path named by the tool_call's diff content block, so the
        # permission event can carry diff_path for the edit gate when the params
        # themselves carry no path key. Mirrors AcpSessionHandle's cache.
        self._tool_call_diff_path: dict[str, str] = {}
        # Map JSON-RPC request id → {"once": optionId, "always": optionId} so
        # the host can echo back the exact optionIds the agent advertised.
        # kiro-cli uses "allow_once"/"allow_always"; claude-agent-acp uses
        # "allow"/"allow_always". Falling back to OPTION_ALLOW_ONCE causes
        # claude-agent-acp to reject the response.
        self._permission_options: dict[str | int, dict[str, str]] = {}
        # Request id -> the permission event built for it, so approve_tool can
        # put the request through the security floor (``permission_floor``)
        # whichever consumer answers it.
        self._permission_gate_events: dict[str | int, AcpEvent] = {}
        self._stderr_lines: deque[str] = deque(maxlen=20)
        # Latched on this process's FIRST non-thinking text chunk, tool call or
        # tool result, cleared with the rest of the process state on respawn:
        # the registration-throttle death classification is refused once work
        # has been observed, so the transient verdict it hands the retry
        # ladders can only ever license replaying a turn that provably did
        # nothing. Per PROCESS (this client owns exactly one), matching the
        # ring the evidence is read from.
        self._prompt_or_tool_seen = False
        # Set by ``_spawn`` from the wrapped argv; only meaningful once a child
        # has been spawned. False before that, which is also the safe default for
        # the classifier: a spawn that never reached the wrap cannot have been
        # refused by it.
        self._sandbox_wrapped_by_crew: bool = False
        # The mask set this spawn asked for, so a trusted corroboration run can
        # exercise the same mounts rather than a weaker profile.
        self._sandbox_hidden_dirs: tuple[str, ...] = ()
        self._jsonl_pos: int = 0  # track read position in session JSONL for tool results
        self._stderr_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._last_activity: float = time.monotonic()
        # Set when a request write stalls (``_write_request_bounded``): the child
        # stopped reading stdin and may still read a buffered frame. The client
        # then reads as neither responsive nor mid-turn, and the next
        # ``ensure_ready`` kills it before spawning a replacement, so no new
        # prompt is ever appended behind the stalled one.
        self._stdin_stalled = False
        # Idle == done. An idle client (spawned, no prompt sent yet) must read
        # as NOT in a turn: has_active_turn() is the 409 turn_in_flight gate
        # on set-model / set-agent, so an unset Event on a live warm process
        # blocks the user until a real turn's finally runs. Every prompt
        # entry clear()s this before sending, so a real turn still reads active.
        self._turn_done: asyncio.Event = asyncio.Event()
        self._turn_done.set()
        # goose steer bookkeeping (``_steer_via_request``): the running prompt's
        # announced run id, steer requests awaiting their answer by request id,
        # and accepted steers awaiting their echo by goose ``messageId``.
        self._active_run_id: str | None = None
        self._steer_answers: dict[int, tuple[asyncio.Future[str], str]] = {}
        self._steers_awaiting_echo: dict[str, str] = {}
        # Steers sent as a second ``session/prompt``
        # (``ACP_BACKENDS_STEER_CONCURRENT_PROMPT``): the running prompt turn's own
        # record, and the earlier steers on this process never answered, which the
        # next prompt cancels before it is written (``_settle_orphan_steers``).
        self._steer_turn: _ConcurrentSteerTurn | None = None
        self._orphan_steers: set[int] = set()
        self._orphan_steers_process: Any = None
        self._turn_permission_denied = False
        # Serializes whole read turns on this client's single stdout StreamReader.
        # An asyncio StreamReader permits exactly ONE waiting reader; the shared
        # `_bg` session is streamed by ~8 callers and the per-session Semaphore(1)
        # does not cover abnormal-exit overlap (a turn dying mid-readline leaves a
        # parked read the next caller collides with -> "readuntil() called while
        # another coroutine is already waiting"). Acquired at the top of
        # _prompt_loop, released in its finally.
        #
        # Finalization caveat: a consumer that `return`s on "complete" without
        # exhausting _prompt_loop leaves the async-gen SUSPENDED; CPython runs
        # its finally via a *deferred* scheduled athrow (next loop tick), not at
        # the consumer's return. So the lock releases promptly on a live loop but
        # NOT synchronously. send_message_stream wraps the loop in `aclosing(...)`
        # to force deterministic release on its hot path; the other consumers
        # rely on the next-tick finalization.
        #
        # Coverage caveat: this lock only covers reads inside _prompt_loop.
        # _read_message also has callers OUTSIDE the loop (_wait_for_response
        # during init, wait_for_compaction). Those run in distinct lifecycle
        # phases that do not overlap a streaming _bg turn, so they are not
        # serialized here; if that ever changes, the readuntil race could recur.
        self._turn_lock: asyncio.Lock = asyncio.Lock()
        self._stale_eligible: bool = False  # set by _dispatch_events after text chunks
        # Set when a tool_call is yielded, cleared when the tool resolves
        # (tool_call_update result) or the turn starts/completes.  NOT cleared
        # on arbitrary inbound frames — that would disarm the watchdog after a
        # single progress frame.  Gates the _TOOL_STALL_TIMEOUT watchdog so a
        # dispatched-but-never-resolved tool can't hang the whole turn.
        self._tool_dispatched: bool = False
        # The open calls' ids, bounded like the session handle's attribution
        # map (``_dispatch.InFlightIdSet``: the shared key and count bound, the
        # oldest evicted first, evictions counted). After an eviction the set
        # answers ``possibly_open`` until the turn resets, so the tool-stall
        # clock (``_TOOL_STALL_TIMEOUT``) stays armed and the stale clock
        # disarmed for a call whose row was dropped but may still be running.
        self._active_tool_calls = InFlightIdSet()
        # Armed by _handle_compaction_status on a `failed` status and cleared at
        # turn start: gates the _COMPACTION_FAILED_TURN_BUDGET check in
        # _prompt_loop. _compaction_failed_turn records that the check fired, so
        # _dispatch_events ends the turn with the compaction stop reason instead
        # of the generic timeout error.
        self._compaction_failed_at: float | None = None
        # Retryability of the LAST failed compaction, read by the dashboard's
        # STOP_REASON_COMPACTION_FAILED branch to decide between re-queuing the
        # abandoned message and giving up. Public (no leading underscore)
        # because that consumer reaches it through getattr on whichever of the
        # two client classes is serving the slot. Only the VERDICT is carried:
        # the reason text reaches the chat row through the compaction-status
        # event title and the server log through the WARNING each arming site
        # already emits, so forwarding it as well would widen the provider
        # contract for no reader.
        self.last_compaction_transient: bool = False
        self._compaction_failed_turn: bool = False
        # Set when the claude backend's "Compacting..." notice is seen inside a
        # turn and cleared by its terminal notice. Only a MANUAL /compact gets a
        # terminal from the adapter (see parse_claude_compaction_notice), so an
        # automatic mid-turn compaction leaves this armed and the dispatch loop
        # settles it with a synthetic `completed` at turn end. Without that, a
        # consumer showing a compacting state would never leave it.
        self._claude_compaction_pending: bool = False
        # The codex twin of the flag above. It guards TWO directions, and the
        # second is why it is read on the way in as well as at turn end. A loaded
        # session replays a past compaction as a ``tool_call`` that is already
        # ``completed``, so only a terminal following a ``started`` seen in THIS
        # turn describes work this turn did. And a compaction that ERRORS reports
        # nothing at all -- codex-acp's ``runCompact`` never resolves, so the
        # ``session/prompt`` request goes unanswered -- which leaves this armed
        # for ``_settle_codex_compaction`` to close out at the turn's terminal.
        self._codex_compaction_pending: bool = False
        # When this session's harness last launched work that outlives the
        # prompt (a backgrounded command, a Workflow). Never reset per turn: a
        # turn ending says nothing about whether that work has finished, and
        # this client reads its pipe only while a call is waiting, so the
        # harness's eventual report of it is not seen until the next prompt.
        self._background_launches = BackgroundLaunchRecord()
        # Liveness oracle for the stale-turn gate: before ending a silent turn
        # at _STALE_TURN_TIMEOUT, consult /proc evidence so a backend that is
        # provably working (CPU/IO movement in the subprocess subtree) is not
        # reaped. Mirrors the kiro shared-runtime path (AcpSessionHandle), which
        # already defers on a WORKING verdict instead of a blunt wall-clock.
        self._liveness_oracle = LivenessOracle()
        # Keep the executor future, not an await-scoped flag: wait_for can time
        # out while the underlying thread continues its /proc walk. A pending
        # future prevents silent-read polling from stacking blocked workers.
        self._consult_future: asyncio.Future[tuple[str, str]] | None = None
        # Record every observed tool_call (id -> (title, kind)) so the
        # PostToolUse hook fire can recover the tool_name from the result's
        # tool_call_id — the RESULT event carries no title. See
        # _maybe_fire_post_tool_hooks.
        self._observed_tool_calls: dict[str, tuple[str, str]] = {}
        self._last_stop_reason: str = ""
        # Dynamic config from ACP session/new response and config_option_update notifications.
        # Only the effort configOptions are consumed (model lists come from
        # _capture_available_models, which parses the real dict-shaped `models`).
        self._acp_config_options: list[dict] = []

    @property
    def backend(self) -> str:
        """ACP backend identifier (e.g. ACP_BACKEND_CLAUDE for claude-agent-acp)."""
        return getattr(self, "_acp_backend", "")

    @property
    def agent_version(self) -> str:
        """``agentInfo.version`` reported at ``initialize`` (``""`` until then).

        The version this process RUNS — see :attr:`AcpRuntime.agent_version`.
        """
        return getattr(self, "_agent_version", "")

    @property
    def spec_zero_tools(self) -> bool:
        """True once this session's CONSUMED mirror projection confirms the spec's ``"tools": []``.

        Set from :attr:`_spec_zero_tools`, which only becomes True after the session's
        projection has been parsed and reports the agent spec actually in force declares
        an empty tool list, so :meth:`_deny_zero_tools` refuses every permission request
        the session raises. Stays False for the lifetime of a session whose spec is
        missing, malformed, or shadowed by another one, and always False on a backend
        with no mirror at all, where the harness itself reads the spec and enforces the
        ban natively rather than through this client.
        """
        return self._spec_zero_tools

    # Compose an ``AcpClient`` session's ``mcpServers`` array: ``kiro_crew.acp.client_mcp_array``.
    authored_spec_declares_zero_tools = _client_mcp_array.authored_spec_declares_zero_tools
    effective_spec_declares_zero_tools = _client_mcp_array.effective_spec_declares_zero_tools
    _pooled_mcp_servers = _client_mcp_array._pooled_mcp_servers
    _kiro_session_servers = _client_mcp_array._kiro_session_servers
    _resolve_session_mcp_servers = _client_mcp_array._resolve_session_mcp_servers
    _mirrorless_member_mounts = _client_mcp_array._mirrorless_member_mounts
    _pooled_broker_stubs = _client_mcp_array._pooled_broker_stubs
    _append_member_dispatch_server = _client_mcp_array._append_member_dispatch_server
    _member_mount_withheld = _client_mcp_array._member_mount_withheld
    _append_member_panel_server = _client_mcp_array._append_member_panel_server
    _withhold_is_the_only_deny_channel = _client_mcp_array._withhold_is_the_only_deny_channel
    _read_mcp_ref_spec = _client_mcp_array._read_mcp_ref_spec
    _guard_unresolved_mcp_refs = _client_mcp_array._guard_unresolved_mcp_refs
    _session_mcp_servers = _client_mcp_array._session_mcp_servers
    _claude_session_mcp_servers = _client_mcp_array._claude_session_mcp_servers
    _opencode_session_mcp_servers = _client_mcp_array._opencode_session_mcp_servers
    _deepseek_session_mcp_servers = _client_mcp_array._deepseek_session_mcp_servers
    _pi_session_mcp_servers = _client_mcp_array._pi_session_mcp_servers
    _republish_pi_mcp_handoff = _client_mcp_array._republish_pi_mcp_handoff
    _discard_pi_mcp_handoff = _client_mcp_array._discard_pi_mcp_handoff
    _goose_session_mcp_servers = _client_mcp_array._goose_session_mcp_servers
    _invalidate_session_mcp_projection = _client_mcp_array._invalidate_session_mcp_projection
    _prepare_session_mcp = _client_mcp_array._prepare_session_mcp
    _mcp_timeout_progress = _client_mcp_array._mcp_timeout_progress
    pop_pending_oauth_requests = _client_mcp_array.pop_pending_oauth_requests
    _begin_session_report = _client_mcp_array._begin_session_report
    _prune_unstartable_mcp = _client_mcp_array._prune_unstartable_mcp
    mcp_session_report = _client_mcp_array.mcp_session_report
    _placed_mcp_server_names = _client_mcp_array._placed_mcp_server_names
    _harness_mcp_server_names = _client_mcp_array._harness_mcp_server_names

    @property
    def _enforces_zero_tool_ban(self) -> bool:
        """True when the spec's ``"tools": []`` is refused at the permission request.

        Needs both facts: the session's spec declares the empty list, and the backend
        honours it as a ban on native tools too. A backend that does not honour it
        leaves the refusal unreached on some paths, so it is not applied at the sites
        that do run.
        """
        return self._spec_zero_tools and capabilities_for(self.backend).honors_zero_tool_ban

    @property
    def _judges_permission_requests(self) -> bool:
        """True when a permission request on this session is JUDGED before it is answered.

        Two facts make an answering site build the full event and consult the provenance
        caches instead of approving on the frame alone: a spec deny set (a switched-off
        tool must be refused wherever it asks), and membership in
        ``ACP_BACKENDS_META_IDENTITY`` (the harness publishes a tool identity on every
        frame, and a frame that carries none, or one naming a server Crew never mounted,
        is refused). The second is a fact about the channel, not about the deny set: the
        goose projection carries an empty deny set by ruling, so a refusal that waited
        for one would never run on a goose session at all. Everywhere else the sites keep
        their prior behaviour byte for byte.

        A third fact judges too: :attr:`_enforces_zero_tool_ban`, the agent's own
        ``"tools": []`` on a backend that honours it. It is not folded into the
        deny-set check above because it answers a broader question -- every request,
        MCP or native -- and a spec with no MCP servers to deny at all (the ordinary
        case for a zero-tool agent) would otherwise leave ``_spec_denied_tools`` empty
        and this property False, which is exactly the auto-approve path the zero-tool
        ban exists to close.
        """
        return (
            bool(self._spec_denied_tools)
            or self._enforces_zero_tool_ban
            or self.backend in ACP_BACKENDS_META_IDENTITY
        )

    @property
    def _is_claude(self) -> bool:
        return self.backend == ACP_BACKEND_CLAUDE

    @property
    def _is_opencode(self) -> bool:
        return self.backend == ACP_BACKEND_OPENCODE

    @property
    def _is_pi(self) -> bool:
        return self.backend == ACP_BACKEND_PI

    @property
    def _is_goose(self) -> bool:
        return self.backend == ACP_BACKEND_GOOSE

    @property
    def _is_deepseek(self) -> bool:
        return self.backend == ACP_BACKEND_DEEPSEEK

    @property
    def _model_registry_namespace(self) -> str:
        """The model_registry namespace key for this backend (``claude_code`` /
        ``acp``). A registry index selector, NOT a provider-identity check — see
        agent_sdk.provider_identity note 3. Used to fold the wire model id and
        seed the allowlist against the right index for whichever backend is a
        member of ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION``."""
        return model_registry_namespace(self.backend)

    @property
    def _uses_advertised_model_selection(self) -> bool:
        """True when this backend sources its wire model id / seed from the
        provider's advertised list (see ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION``)."""
        return self.backend in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION

    @property
    def _seeds_local_settings(self) -> bool:
        """True when this backend seeds (and must re-seed) a per-session settings
        file (see ``ACP_BACKENDS_SEED_LOCAL_SETTINGS``)."""
        return self.backend in ACP_BACKENDS_SEED_LOCAL_SETTINGS

    @property
    def _is_kiro(self) -> bool:
        """True when this client drives kiro-cli (the AcpClient default).

        AcpClient serves kiro-cli, claude-agent-acp and the dormant codex seam, so
        this is the positive spelling of the sites that used to read
        ``not self._is_claude`` (harness-parity H5). KAS runs on AcpRuntime, not
        AcpClient, so it never reaches this property.
        """
        return self.backend == ACP_BACKEND_KIRO

    @property
    def member_dispatch_mounted(self) -> bool | None:
        """Whether this session's array carries the member session-control entry.

        ``None`` until :meth:`_append_member_dispatch_server` has composed an
        array for this client (no evidence yet); after that, the answer that
        composition gave -- False on every withhold, on a non-member session and
        on a backend outside ``ACP_BACKENDS_MEMBER_DISPATCH``.
        """
        return getattr(self, "_member_dispatch_mounted", None)

    # Prove and hand back Crew's ownership of a claude settings seed on disk: ``kiro_crew.acp.client_seed_ownership``.
    _claude_local_settings_path = _client_seed_ownership._claude_local_settings_path
    _claude_settings_is_still_ours = _client_seed_ownership._claude_settings_is_still_ours
    _settings_path_bytes = staticmethod(_client_seed_ownership._settings_path_bytes)
    _expected_settings_fingerprint = _client_seed_ownership._expected_settings_fingerprint
    _withdraw_shared_reader_lease = _client_seed_ownership._withdraw_shared_reader_lease
    _rename_aside_noreplace = staticmethod(_client_seed_ownership._rename_aside_noreplace)
    _copy_aside_back_without_clobber = staticmethod(
        _client_seed_ownership._copy_aside_back_without_clobber
    )
    _discard_claude_settings_seed = _client_seed_ownership._discard_claude_settings_seed
    _hand_back_unrevoked_claim = _client_seed_ownership._hand_back_unrevoked_claim
    _settle_claude_settings_seed = _client_seed_ownership._settle_claude_settings_seed
    release_settings_seed_claim = _client_seed_ownership.release_settings_seed_claim
    _foreign_settings_owner = _client_seed_ownership._foreign_settings_owner
    _settle_claude_settings_seed_locked = _client_seed_ownership._settle_claude_settings_seed_locked

    @staticmethod
    def _settings_path_holds(path: Path, expectation: tuple[int, str] | None) -> bool:
        """Whether *path* still holds exactly the ``(size, sha256)`` in *expectation*.

        Split out from :meth:`_claude_settings_is_still_ours` so the teardown
        transaction can be a pure function of arguments captured before it starts.
        It runs in a thread while the client clears its instance flags, so a version
        that re-read ``self`` could decide ownership against state that changed
        underneath it. ``None`` means Crew has no claim to check, which reads as
        "not ours".
        """
        return AcpClient._settings_path_bytes(path, expectation) is not None

    @staticmethod
    def _claim_pathname_if_ours(
        path: Path, expectation: tuple[int, str] | None
    ) -> tuple[Path, tuple[int, int]] | None:
        """Atomically move *path* aside into a fresh name; return it IFF it is Crew's.

        Closes the window between an ownership check and the delete or overwrite that
        acts on it. ``_settings_path_holds`` verifies bytes by PATHNAME, but the delete
        or re-seed then mutates that same pathname a moment later -- and a user who
        atomically replaced the file in between would have their settings deleted or
        clobbered. ``os.replace`` captures whatever is at the pathname in ONE atomic
        step, so verifying the MOVED inode cannot then race: its bytes are fixed. A
        match is Crew's own seed (the caller deletes or replaces the moved file); a
        mismatch is a replacement that raced in, and it is put back untouched.

        The capture destination is a FRESH name created by ``mkstemp`` (``O_EXCL``) in
        the same directory, so it is a pathname this process just created and provably
        did NOT pre-exist. A fixed sibling name (e.g. ``<name>.crew-gc``) could name a
        file the project already owns, and ``os.replace`` onto it would clobber that
        file atomically -- relocating the very data loss this helper prevents one
        pathname over. ``os.replace`` onto our own fresh temp destroys only the empty
        temp we just made.

        ``(aside, identity)`` -- the moved file and ``(st_dev, st_ino)`` captured at
        the move, which every later restore and cleanup pins against. ``None``
        (nothing for the caller to mutate) when the path is gone, cannot be moved, or
        holds a file that is not Crew's. A crash between the move and the caller's
        follow-up leaves at most one such ``.crew-gc`` temp beside the target, which
        the next session's fresh seed ignores.
        """
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".crew-gc"
            )
        except OSError:
            return None
        tmp_st = os.fstat(fd)
        os.close(fd)
        aside = Path(tmp_name)
        try:
            os.replace(path, aside)
        except OSError:
            # Nothing to capture (path gone, or it cannot be moved): drop the empty
            # temp so a refusal never litters a stray file beside the target.
            with suppress(OSError):
                pinned_fs.unlink_verified_by_name(
                    path.parent, aside.name, (tmp_st.st_dev, tmp_st.st_ino)
                )
            return None
        try:
            moved = os.lstat(aside)
            moved_ident = (moved.st_dev, moved.st_ino)
        except OSError:
            # The moved entry vanished under us (a racing unlink): nothing left
            # to validate or restore. The empty temp name is gone with it.
            return None
        if AcpClient._settings_path_holds(aside, expectation):
            return aside, moved_ident
        # Not Crew's after all: a replacement raced in, or the user's own file --
        # a different regular file, a symlink the verifying open refuses to
        # follow, a file of any size. Put the moved entry back AS IT IS, without
        # clobbering whatever else may have taken the vacated name in the
        # meantime. No-clobber preserves the NEWER occupant and keeps the moved
        # entry recoverable beside it, where a replace-semantics restore would
        # silently destroy the newer one.
        AcpClient._restore_aside_without_clobber(aside, path, moved_ident)
        return None

    @property
    def _permission_surface_governed(self) -> bool:
        """Whether Crew controls this session's native permission surface.

        The precondition for delivering the ``mcpServers`` array, in either of
        its two shapes: this client authored ``settings.local.json`` itself
        (``_claude_settings_authored``), or the file's CURRENT on-disk bytes
        passed the sibling-seed validation
        (``_permission_surface_share_validated``). The durable reader lease is
        tracked separately in ``_claude_settings_shared`` because it survives a
        failed re-validation to keep the file pinned for a later re-earn, and the
        owner's un-revoked durable claim (``_claude_settings_claim_unrevoked``) is
        deliberately not read here either: it says a hand-back is still owed for
        a file already observed to be someone else's, never that Crew governs it.
        ``getattr`` on both governed flags because tests build clients without
        ``__init__``.

        A third shape governs without the file: the project owns it, and this
        session leaves it out of its setting sources
        (``_claude_local_settings_excluded``, see
        :meth:`_exclude_foreign_local_settings`). Its ``permissions.allow`` then
        never loads, so it cannot pre-approve a tool either.
        """
        return (
            getattr(self, "_claude_settings_authored", False)
            or getattr(self, "_permission_surface_share_validated", False)
            or getattr(self, "_claude_local_settings_excluded", False)
        )

    # Author an ``AcpClient`` session's claude ``settings.local.json`` seed: ``kiro_crew.acp.client_claude_seed``.
    _share_settings_seed_if_identical = _client_claude_seed._share_settings_seed_if_identical
    _share_sibling_settings_seed = _client_claude_seed._share_sibling_settings_seed
    _retract_reseed = _client_claude_seed._retract_reseed
    _log_declined_share = _client_claude_seed._log_declined_share
    _render_claude_settings_payload = _client_claude_seed._render_claude_settings_payload
    _write_claude_local_settings = _client_claude_seed._write_claude_local_settings
    _reseed_after_capture = _client_claude_seed._reseed_after_capture
    _seed_session_settings = _client_claude_seed._seed_session_settings

    @staticmethod
    def _restore_aside_without_clobber(aside: Path, path: Path, expect: tuple[int, int]) -> bool:
        """Put a moved-aside entry back under *path* without replacing a new occupant.

        A same-directory no-clobber rename (:meth:`_rename_aside_noreplace`), so a
        symlink, a directory or a file of any size the user placed at the settings
        path is restored exactly as it was captured -- no copy, no size cap, no read
        of its bytes -- and the aside name is consumed by the move. An occupant that
        arrived at the pathname while the entry was out is newer, so the rename
        refuses it and the moved entry stays recoverable beside it as ``.crew-gc``
        litter. Where no no-clobber rename primitive exists the restore falls back
        to the validated byte copy :func:`pinned_fs.put_back_no_clobber`, pinned to
        the moved inode *expect*; that path restores only a regular file.
        """
        try:
            if AcpClient._rename_aside_noreplace(aside, path):
                return True
        except FileExistsError:
            back: str | None = pinned_fs.PUT_BACK_NAME_TAKEN
        except OSError:
            logger.warning("could not restore %s; it is at %s", path, aside.name)
            return False
        else:
            back = AcpClient._copy_aside_back_without_clobber(aside, path, expect)
            if back is None:
                return True
        if back == pinned_fs.PUT_BACK_NAME_TAKEN:
            logger.info(
                "%s was recreated while Crew's seed was moved aside; leaving the new "
                "file in place (the moved seed remains at %s).",
                path,
                aside.name,
            )
            return False
        logger.warning("could not restore %s; it is at %s", path, aside.name)
        return False

    # The claude-agent-acp session contract an ``AcpClient`` keeps: ``kiro_crew.acp.client_claude_session``.
    _exclude_foreign_local_settings = _client_claude_session._exclude_foreign_local_settings
    _claude_adapter_below_setting_sources_floor = (
        _client_claude_session._claude_adapter_below_setting_sources_floor
    )
    _log_claude_adapter_below_floor = _client_claude_session._log_claude_adapter_below_floor
    _prepare_claude_side_read_denies = _client_claude_session._prepare_claude_side_read_denies
    _claude_session_meta = _client_claude_session._claude_session_meta
    _claude_session_excludes_local = _client_claude_session._claude_session_excludes_local
    _verify_claude_project_denies_unchanged = (
        _client_claude_session._verify_claude_project_denies_unchanged
    )
    _pin_claude_starting_mode = _client_claude_session._pin_claude_starting_mode

    @property
    def is_ready(self) -> bool:
        return self._process is not None and self._session_id is not None

    # Own an ``AcpClient`` child process after it is spawned: ``kiro_crew.acp.client_lifecycle``.
    _is_process_alive = _client_lifecycle._is_process_alive
    is_responsive = _client_lifecycle.is_responsive
    touch_activity = _client_lifecycle.touch_activity
    set_resume_session_id = _client_lifecycle.set_resume_session_id
    set_fork_request = _client_lifecycle.set_fork_request
    rekey = _client_lifecycle.rekey
    reclaim = _client_lifecycle.reclaim
    _discard_sandbox_cleanup = _client_lifecycle._discard_sandbox_cleanup
    _discard_bound_workspace = _client_lifecycle._discard_bound_workspace
    _session_work_dir = _client_lifecycle._session_work_dir
    _cleanup_failed_live_spawn = _client_lifecycle._cleanup_failed_live_spawn
    _to_thread_guarding_sandbox = _client_lifecycle._to_thread_guarding_sandbox
    _drain_stderr = _client_lifecycle._drain_stderr
    _snapshot_process_tree = _client_lifecycle._snapshot_process_tree
    refresh_process_tree = _client_lifecycle.refresh_process_tree
    _kill_process = _client_lifecycle._kill_process
    _reset_state = _client_lifecycle._reset_state
    _classify_failed_start = _client_lifecycle._classify_failed_start
    _settle_stderr = _client_lifecycle._settle_stderr
    _sandbox_init_failure = _client_lifecycle._sandbox_init_failure
    _registration_throttle_line = _client_lifecycle._registration_throttle_line
    shutdown = _client_lifecycle.shutdown

    @property
    def work_scratch_dir(self) -> Path | None:
        """The session tree's work directory this process exposes as ``$KIROCREW_SCRATCH``.

        The inherited directory when this client was spawned into an existing
        tree, else its own allocation; ``None`` before spawn or when allocation
        failed. Handed as ``shared_scratch`` to every spawn made on behalf of
        this session (see ``session_allocation._collect_parent_runtime_kwargs``).
        """
        return self._shared_scratch or self._scratch_dir

    def is_process_alive(self) -> bool:
        """True if the underlying process exists and has not exited."""
        return self._is_process_alive()

    @property
    def process_tree_confirmed_dead(self) -> bool:
        """Whether shutdown confirmed the root and every tracked child exited."""
        return self._process_tree_confirmed_dead is True

    @property
    def process_instance(self) -> str:
        """Identity of the CURRENT child process instance (``""`` when none).

        A fresh random id per spawn. This — not the ACP session id — is what a
        resource minted by the child must be compared against later: a resume
        carries the SAME session id onto a NEW process (``ensure_ready``'s
        session/load path re-sets ``_session_id`` from ``resume_sid``), so a
        session-id comparison fails open exactly when the minting process is
        gone. Liveness is a separate question: this names which spawn, while
        :meth:`is_process_alive` answers whether it still runs.
        """
        return self._process_instance if self._process is not None else ""

    @property
    def exit_code(self) -> int | None:
        """Return the process exit code, or None if still running / never started."""
        return self._process.returncode if self._process else None

    @property
    def resumed(self) -> bool:
        """True if the last session was restored via session/load."""
        return self._resumed

    @property
    def session_identity_token(self) -> str:
        """This session's per-session identity token, or ``""``.

        The uniform name the shared per-turn publisher reads
        (``messaging.identity._publish_session_token``), for the same reason
        :meth:`reclaim` is uniform: the publisher must stay backend-agnostic, and
        the two ACP providers keep the token in different places — this one on the
        client, the shared-runtime one on the session handle. A provider without
        this attribute is simply not asked.
        """
        return self._stub_session_token

    # Select, verify and apply an ``AcpClient`` session's model: ``kiro_crew.acp.client_model_selection``.
    set_model = _client_model_selection.set_model
    _capture_available_models = _client_model_selection._capture_available_models
    _persist_advertised_models_if_changed = (
        _client_model_selection._persist_advertised_models_if_changed
    )
    available_models = _client_model_selection.available_models
    _advertised_model_ids = _client_model_selection._advertised_model_ids
    _model_is_unusable = _client_model_selection._model_is_unusable
    refresh_available_models = _client_model_selection.refresh_available_models
    _entitlement_probe_client = _client_model_selection._entitlement_probe_client
    _probe_advertised_models = _client_model_selection._probe_advertised_models
    _entitlement_probe_answer = _client_model_selection._entitlement_probe_answer
    _model_config_candidates = staticmethod(_client_model_selection._model_config_candidates)
    _push_model_config_option = _client_model_selection._push_model_config_option
    _ensure_served_default = _client_model_selection._ensure_served_default
    _note_agent_switched = _client_model_selection._note_agent_switched
    _judge_activated_model = _client_model_selection._judge_activated_model
    _reassert_adapter_resolved_model = _client_model_selection._reassert_adapter_resolved_model
    _apply_startup_model = _client_model_selection._apply_startup_model
    set_config_option = _client_model_selection.set_config_option
    _store_session_config = _client_model_selection._store_session_config
    _handle_config_option_update = _client_model_selection._handle_config_option_update
    _sync_effort_levels = _client_model_selection._sync_effort_levels
    supports_config_option = _client_model_selection.supports_config_option
    get_valid_effort_levels = _client_model_selection.get_valid_effort_levels

    # Answer an ``AcpClient`` session's permission requests and enforce its tool restrictions: ``kiro_crew.acp.client_permissions``.
    _apply_session_permission_routing = _client_permissions._apply_session_permission_routing
    _verify_goose_routing = _client_permissions._verify_goose_routing
    approve_tool = _client_permissions.approve_tool
    _note_pi_gate_denied = _client_permissions._note_pi_gate_denied
    reject_tool = _client_permissions.reject_tool
    _handle_permission = _client_permissions._handle_permission
    _refuse_identity_drift = _client_permissions._refuse_identity_drift
    _foreign_mcp_identity = _client_permissions._foreign_mcp_identity
    _unclassified_tool_call = _client_permissions._unclassified_tool_call
    _tripwire_spec_disabled_tool = _client_permissions._tripwire_spec_disabled_tool
    _note_pi_adapter_version = _client_permissions._note_pi_adapter_version
    _note_goose_version = _client_permissions._note_goose_version
    _tripwire_goose_mode = _client_permissions._tripwire_goose_mode
    _tripwire_pi_gate = _client_permissions._tripwire_pi_gate
    _audit_spec_restriction = _client_permissions._audit_spec_restriction
    _deny_zero_tools = _client_permissions._deny_zero_tools
    _deny_spec_disabled_tool = _client_permissions._deny_spec_disabled_tool
    _reject_unknown_server_request = _client_permissions._reject_unknown_server_request
    _note_pi_gate_asked = _client_permissions._note_pi_gate_asked
    _build_permission_event = _client_permissions._build_permission_event

    @property
    def acp_config_options(self) -> list[dict]:
        """Config options reported by ACP (effort, model, mode selectors)."""
        return self._acp_config_options

    # JSON-RPC over an ``AcpClient`` child's stdio: ``kiro_crew.acp.client_transport``.
    _next_req_id = _client_transport._next_req_id
    _send_request = _client_transport._send_request
    _write_request_bounded = _client_transport._write_request_bounded
    _stdin_write_lock = _client_transport._stdin_write_lock
    _write_response_bounded = _client_transport._write_response_bounded
    _send_response = _client_transport._send_response
    _send_error = _client_transport._send_error
    _read_message = _client_transport._read_message
    _wait_for_response = _client_transport._wait_for_response
    _drain_notifications = _client_transport._drain_notifications

    # Prepare an ``AcpClient`` spawn before the process exists: ``kiro_crew.acp.client_spawn_env``.
    _prepare_spawn_workspace = _client_spawn_env._prepare_spawn_workspace
    _agent_env_for_spawn = _client_spawn_env._agent_env_for_spawn
    _apply_session_identity_env = _client_spawn_env._apply_session_identity_env
    _resolve_spawn_agent_argv = _client_spawn_env._resolve_spawn_agent_argv

    async def _spawn(self) -> None:
        """Start the ACP backend subprocess with stdio pipes, suspended, then track it.

        A host this client starts one process per session for -- claude-agent-acp,
        OpenCode, goose, pi, the DeepSeek Harness -- resolves its plan in its own
        adapter (:func:`kiro_crew.acp.harness.process_adapter_for`); kiro-cli keeps its
        own arm below. Either plan goes through the launch tail both drivers share
        (:func:`kiro_crew.acp.launch.launch`). What happens once the process exists --
        the resume, its identity, its pids, its stderr -- stays here.
        """
        # Off-loop: mkdir is a blocking syscall and the parent dirs may live on
        # slow storage; the loop must never wait on the kernel here. The
        # unresolved-ref snapshot rides IN this hop rather than in one of its own:
        # the detector runs for every harness (the defect it catches has shipped on
        # three), and a second await would be a new suspension point on kiro-cli's
        # construction path in service of a diagnostic -- so the count of awaits
        # here is deliberately unchanged (harness-parity H13).
        await asyncio.to_thread(self._prepare_spawn_workspace)
        # A fresh spawn reads ``agent.env`` afresh, once (_agent_env_for_spawn).
        self._agent_env_snapshot = None

        # Kiro's internal macOS sandbox replaces (rather than nests inside)
        # Kiro Crew's Seatbelt profile. Refuse a delegated agent workspace that
        # can reach the protected named decoder snapshots; otherwise a same-UID
        # agent could replace verified bytes before the decoder spawn opens them.
        if self.backend in ACP_BACKENDS_INTERNAL_SANDBOX:
            await asyncio.to_thread(assert_voice_runtime_outside_agent_workspace, self._work_dir)

        adapter = process_adapter_for(self.backend)
        if adapter is not None:
            # The host's own launch knowledge -- its binary, argv, labels, credential
            # mask and gate -- in its own file. The context names this session, which
            # the host may need to prepare before it starts (its MCP array, a seed)
            # and which keeps the facts its turn-time code reads back. The
            # environment and home fields are the shared context's; no per-process
            # host reads them.
            plan = await adapter.resolve_spawn(
                SpawnContext(
                    agent=self._agent,
                    work_dir=self._work_dir,
                    model=self._model,
                    environ=dict(os.environ),
                    home=Path(os.path.expanduser("~")),
                    sandbox_mode=self._sandbox_mode,
                    session=self,
                )
            )
        else:
            # Pin ONE reading of the environment for both the search and the
            # message that reports it. The previous code resolved against the live
            # ``os.environ`` and then, on failure, recomputed the directory set
            # from a FRESH read -- so a PATH change landing in that window (a
            # concurrent installer, a self-update, anything editing the gateway's
            # environment) would produce a "not found" message naming directories
            # that were never searched, while omitting ones that were. Caching the
            # search path WITH the resolution result is what avoids that, and is
            # the same guarantee the Claude adapter carries.
            spawn_environ = dict(os.environ)
            spawn_home = Path.home()
            try:
                kiro_bin = await _resolve_kiro_bin_for_spawn(environ=spawn_environ, home=spawn_home)
            except _KiroExecutableTrustError as exc:
                raise AcpError(str(exc)) from exc
            if not kiro_bin:
                # Pure function of the arguments, so this reproduces exactly the
                # set the resolution above walked. Still off-loop: it expands the
                # inherited PATH.
                raise AcpError(
                    await asyncio.to_thread(
                        kiro_cli_not_found_message,
                        environ=spawn_environ,
                        home=spawn_home,
                    )
                )
            # Self-heal (B): ensure the managed default agent file exists before
            # this --agent spawn, so kiro-cli registers the mode and step 4's
            # set_mode succeeds instead of faulting "Mode not found". Best-effort,
            # off the loop; non-managed agents fall through to the step-4 guard.
            try:
                await asyncio.to_thread(ensure_agent_materialized, self._agent)
            except Exception:
                logger.warning("pre-spawn agent materialization failed", exc_info=True)
            # ALSO not best-effort, and for the same reason as the fork gate below: a
            # DERIVED spec (kirocrew-worker, kirocrew-heartbeat) that predates the default
            # agent's still mounts and auto-approves a server the default does not have, so
            # proceeding would run ungoverned grants. Repairs first and refuses only
            # when it cannot -- a refused dispatch is recoverable and reportable,
            # which a worker running on a revoked server is not.
            try:
                derived_snapshot = await asyncio.to_thread(
                    require_fresh_derived_spec, self._agent, self._work_dir
                )
            except DerivedSpecStale as exc:
                raise AcpError(str(exc)) from exc
            # Captured for the post-load half of the bracket, which is NOT here: this
            # method creates the child and returns after bookkeeping, so at this point
            # the subprocess has not provably read its spec and closing the bracket
            # would prove nothing. ``_initialize_session`` closes it against this
            # object, at the ``initialize`` response -- the earliest signal that the
            # read happened. Same pairing as the AcpRuntime path.
            self._derived_spec_snapshot = derived_snapshot
            # NOT best-effort: a fork-backed agent may not spawn until fork
            # governance is re-projected, and a failed or timed-out refresh
            # ABORTS the spawn — its on-disk allowedTools/autoApprove bypass
            # the PreToolUse gate, so proceeding would run ungoverned grants.
            # The work dir is the cwd kiro-cli resolves --agent against first,
            # so the gate also refuses a fork shadowed by a project-local spec.
            try:
                await asyncio.to_thread(require_fork_governance, self._agent, self._work_dir)
            except ForkGovernanceUnresolved as exc:
                raise AcpError(str(exc)) from exc
            # The same cwd-first resolution makes a checkout that declares a MANAGED
            # agent name the spec that runs, whatever Kiro Crew installed under it.
            shadowed = await asyncio.to_thread(
                managed_shadow_mod.managed_agent_shadow_refusal, self._agent, self._work_dir
            )
            if shadowed:
                raise AcpError(shadowed)
            # The agents-tree seal is a launcher rule, and a spawn delegated to
            # kiro-cli's internal sandbox never sees the launcher — so on those
            # paths a workspace overlapping the agents directory is the one way
            # left to rewrite a spec. Refused before the spawn; off-loop because
            # the delegation predicate reads the kiro settings file.
            overlap = await asyncio.to_thread(
                delegated_workspace_exposes_sealed_target, self._work_dir
            )
            if overlap:
                raise AcpError(overlap)
            from kiro_crew.acp.skill_projection import prepare_native_skill_projection

            # One process, one session: the identity rides the process environment
            # (``_apply_session_identity_env``) and kiro-cli mounts ``kirocrew-core``
            # natively from the view, restrictions included, so no per-session
            # element replaces the declaration here -- the shared runtime's
            # element-withholding question does not arise, and a ``disabledTools``
            # naming other tools must not refuse this agent's view.
            self._native_skill_projection = await asyncio.to_thread(
                prepare_native_skill_projection, self._work_dir, per_session_element=False
            )
            if self._native_skill_projection is not None:
                # The agent this process is launched as: its own FIRST ``set_mode``
                # activation is tolerated even with no prepared view (see
                # NativeSkillProjection.request), which consumes the exemption --
                # so a later switch, including back to this same agent once its
                # view has vanished, takes the strict resolver and fails closed.
                self._native_skill_projection.spawn_agent_name = self._agent
                # Advertised independently of the request() exemption above: the
                # start must find the launch agent's mode in availableModes even
                # with no prepared view (see NativeSkillProjection.frame).
                self._native_skill_projection.advertised_launch_name = self._agent
            argv = [
                kiro_bin,
                KIRO_CLI_SUBCMD,
                "--agent",
                (
                    self._resolve_spawn_agent_argv()
                    if self._native_skill_projection is not None
                    else self._agent
                ),
            ]
            spawn_label = f"{KIRO_CLI_BIN} {KIRO_CLI_SUBCMD}"
            stderr_label = KIRO_CLI_BIN
            plan = SpawnPlan(argv=argv, spawn_label=spawn_label, stderr_label=stderr_label)

        async def _env_before_scrub(
            env: dict[str, str], spawned_binary: str | None
        ) -> dict[str, str]:
            # The host's own variables first, then this session's identity, so a
            # host value can never stand in for the identity the session carries.
            if adapter is not None:
                await adapter.prepare_spawn_env(env, to_thread=self._to_thread_guarding_sandbox)
            self._apply_session_identity_env(env)
            if self._channel_id:
                env["KIROCREW_CHANNEL_ID"] = self._channel_id
            else:
                env.pop("KIROCREW_CHANNEL_ID", None)
            # Resolve SSH_AUTH_SOCK dynamically — the gateway's env may be stale
            # after an ssh-agent restart — and KRB5CCNAME to a FILE: ccache (the
            # kernel keyring, the default on some Linux distros, is invisible to
            # this child, so Kerberos-gated MCP servers fail without it). Covers
            # the session agent and all ACP-provider subagents, which spawn through
            # this same path. The same hop settles the CLI's own KIRO_API_KEY:
            # re-injected from .env for the kiro-cli backend (post-scrub Docker),
            # actively stripped for a foreign backend, which must never receive it
            # (see config.loader.inject/strip_kiro_cli_api_key). All of this
            # glob/stat/reads under /tmp and the data home, so it runs off-loop in
            # ONE thread hop. Guarded: the sandbox temp file is live, so a
            # cancellation here must not orphan it. The launch tail scrubs after
            # it, and KIRO_API_KEY stays available only to the positively
            # identified Kiro backend.
            return await self._to_thread_guarding_sandbox(
                functools.partial(_resolve_spawn_env, kiro_api_key=self._is_kiro), env
            )

        def _env_after_scrub(env: dict[str, str]) -> None:
            # The auxiliary kiro-cli children never reach a harness's apply_spawn_env,
            # so they are named here.
            if self._is_kiro:
                from kiro_crew.acp.harness._common import apply_client_application_env

                apply_client_application_env(env)

        launched = await launch(
            self,
            LaunchRequest(
                argv=plan.argv,
                backend=self.backend,
                sandbox_mode=self._sandbox_mode,
                extra_env=self._extra_env,
                scratch_label=self._session_key or "session",
                env_before_scrub=_env_before_scrub,
                env_after_scrub=_env_after_scrub,
                agent_env=self._agent_env_snapshot,
                extra_hidden_dirs=plan.extra_hidden_dirs,
                extra_expose_files=plan.extra_expose_files,
                internal_sandbox=self.backend in ACP_BACKENDS_INTERNAL_SANDBOX,
                pod_home_remap=self.backend in ACP_BACKENDS_POD_HOME_REMAP,
            ),
            _launch_tools(),
        )
        self._process = launched.process
        self._pid = self._process.pid
        self._kill_sent = None
        self._process_tree_confirmed_dead = False
        # Minted with the process it names, random rather than pid-derived: a
        # pid can be reused by the OS, and the start-time disambiguator is not
        # readable on every platform, so equality on a fresh random id is the
        # comparison that cannot false-match across spawns.
        self._process_instance = uuid.uuid4().hex[:16]
        _spawn_label = plan.spawn_label
        # Everything from here to the end of _spawn runs with a LIVE subprocess
        # that nothing has recorded yet, so every step must be guarded. Without
        # this, any exception in the window — finish_suspended_spawn, the
        # start-time read, the two PID-file appends, the descendant scan — unwinds
        # out of _spawn leaving that process running and absent from both PID
        # files. It is then unreachable by every agent-runtime reaper (they all
        # read those files, and the /proc orphan scan declines managed agent
        # runtimes on purpose), so it leaks until the host reboots.
        #
        # ensure_ready()'s retry loop cannot substitute for this: it only catches
        # AcpTimeoutError / AcpError, and nothing raised in this window is either
        # of those — an OSError from the executor or a wedged file lock sails
        # straight past it and its `finally` records metrics only.
        #
        # BaseException so a CancelledError mid-window cleans up too. Mirrors the
        # twin guard in acp/runtime.py around reader startup + handshake.
        try:
            # Windows resource ceiling, applied while the child is still SUSPENDED,
            # then resumed. No-op on POSIX (CREATE_SUSPENDED is 0 there). OFFLOADED
            # because the Windows path reads the config file and walks the process
            # and thread tables (see the note on finish_suspended_spawn); the child
            # is frozen until it returns, so this is the one await the spawn cannot
            # skip.
            await platform_compat.finish_windows_cleanup_owned_spawn(
                lambda: asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    functools.partial(
                        finish_suspended_spawn, self._process, self._pid, label=_spawn_label
                    ),
                )
            )
            # Identity for the recycle guards that later decide whether this pid
            # may be signalled. ``get_process_start_id`` is in-process on every
            # platform (its own docstring pins that), so unlike the ``ps``-forking
            # reader it replaces there is nothing here to offload.
            self._start_time = platform_compat.get_process_start_id(self._pid)
            if self._scratch_dir is not None:
                # Liveness anchor for the scratch sweeps -- see acp/runtime.py's
                # twin block. Off-loop, and fail-open on a write that could not
                # happen: an unowned dir is covered by the grace-window rule.
                owner_outcome = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    functools.partial(agent_scratch.record_owner, self._scratch_dir, self._pid),
                )
                if owner_outcome == "refused":
                    # A LINK where this child's own marker belongs: the child
                    # owns that directory and has pointed it somewhere else, so
                    # it is steering an UNSANDBOXED gateway write at a path of
                    # its choosing. Raising hands it to the guard below, which
                    # reaps the process -- the spawn must not carry on as if the
                    # owner had been recorded.
                    raise agent_scratch.ScratchBoundaryError(
                        "the spawned agent replaced its scratch owner marker with a link"
                    )
                if owner_outcome == "stale":
                    # Not an attack: the update failed and the marker it left
                    # could not be cleared, so this dir still names the GATEWAY.
                    # That pid dies with the gateway while this child lives on,
                    # which is the state a later sweep reads as a dead owner
                    # before deleting a live agent's temp dir. Reaping now is
                    # recoverable; that deletion is not.
                    raise agent_scratch.ScratchBoundaryError(
                        "the scratch owner marker still names the gateway after a failed update"
                    )
            if self._shared_scratch is not None:
                # Twin of acp/runtime.py: join the tree's owner marker beside
                # the parent, so the sweep keeps the tree while either lives.
                adopt_outcome = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    functools.partial(agent_scratch.adopt_owner, self._shared_scratch, self._pid),
                )
                if adopt_outcome in ("refused", "stale"):
                    raise agent_scratch.SharedScratchJoinError(
                        "the inherited scratch owner marker could not be joined"
                        if adopt_outcome == "stale"
                        else "the inherited scratch owner marker was replaced with a link"
                    )
                if adopt_outcome != "recorded":
                    logger.warning(
                        "agent-scratch: could not join the owner marker of %r (%s); the tree's "
                        "work directory is left unowned and will not be swept",
                        self._shared_scratch.name,
                        adopt_outcome,
                    )
            logger.info("Spawned %s (PID %d)", _spawn_label, self._pid)
            # Track root PID and do an early descendant scan.  kiro-cli forks
            # child processes quickly after launch.  Recording them here means
            # _kill_process() can clean up even if _initialize_session() fails.
            from kiro_crew.session import (  # circular: session -> config.loader -> providers.acp -> acp.client
                _track_child_pids,
                _track_pid,
                _track_session_pid,
            )
            from kiro_crew.session_pid import _pid_start_token

            # Read BEFORE the appends: the identity of the process that holds the
            # number NOW, kept for the identity-bound retirement in _reset_state
            # and handed to the tracker so it records this same token.
            self._spawn_start_token = _pid_start_token(self._pid)

            # The PID-file trackers each take an exclusive file lock and do a
            # read-modify-append under it — blocking syscalls that must not run
            # on the event loop: ensure_ready() awaits _spawn() from the loop on
            # every cold start, so a contended or wedged lock holder here would
            # stall every task including the liveness heartbeat. Ride the same
            # executor as the descendant scans below.
            _loop = asyncio.get_running_loop()
            await _loop.run_in_executor(subprocess_executor(), _track_pid, self._pid)
            # Separate file for startup cleanup.
            await _loop.run_in_executor(
                subprocess_executor(), _track_session_pid, self._pid, self._spawn_start_token
            )
            await asyncio.sleep(0.3)
            early_descendants = await _loop.run_in_executor(
                subprocess_executor(), runtime_process_tree._get_child_pids, self._pid
            )
            if early_descendants:
                self._child_pids = await _loop.run_in_executor(
                    subprocess_executor(),
                    runtime_process_tree._capture_child_records,
                    early_descendants,
                )
                await _loop.run_in_executor(
                    subprocess_executor(), _track_child_pids, self._child_pids, self._pid or 0
                )
                logger.info(
                    "Early tracking %d descendants of PID %d", len(self._child_pids), self._pid
                )

            if self._process.stderr:
                self._stderr_task = asyncio.ensure_future(
                    self._drain_stderr(self._process.stderr, label=plan.stderr_label)
                )
        except BaseException:
            logger.error(
                "Spawn of %s (PID %s) failed after the process was live; killing it so it "
                "cannot leak untracked",
                _spawn_label,
                self._pid,
                exc_info=True,
            )
            try:
                await self._cleanup_failed_live_spawn()
            except Exception:
                logger.warning(
                    "Cleanup kill after a failed spawn did not complete for PID %s",
                    self._pid,
                    exc_info=True,
                )
            raise

    def _retire_liveness_state(self) -> None:
        """Release the tracked consult and swap in a fresh, configured oracle.

        Both boundaries that drop a movement baseline — turn start in
        ``_prompt_loop`` under ``_turn_lock``, and process reset — retire rather
        than ``reset()``,
        and both must retire the future TOGETHER with the oracle. Their lifetimes
        cannot diverge: if only the oracle were replaced, a walk wedged during the
        previous turn would answer every later poll with "prior consult still in
        flight", so the new turn never samples its own process and the 90s cutoff
        completes it early — the truncation this gate exists to prevent.

        Releasing the future costs at most one abandoned worker per boundary
        instead of per silent read, which is the bound this gate is actually for.
        A walk submitted through ``_consult_liveness_model_wait`` already carries a
        retrieval callback from submission time, so its eventual exception is
        consumed even though nobody awaits it any more; the consume/attach here
        additionally covers a future that did not come from that path.

        ``fresh()`` carries the configuration over: a default-constructed
        replacement would silently repoint a caller-supplied /proc root, clock or
        sampling interval. ``getattr`` because the low-level PID lifecycle tests
        build clients with ``__new__``.
        """
        prior_consult = getattr(self, "_consult_future", None)
        self._consult_future = None
        if prior_consult is not None:
            if prior_consult.done():
                _consume_future_exception(prior_consult)
            else:
                prior_consult.add_done_callback(_consume_future_exception)
        retiring = getattr(self, "_liveness_oracle", None)
        self._liveness_oracle = retiring.fresh() if retiring is not None else LivenessOracle()

    async def _new_session_following_substitution(self) -> dict:
        """Issue ``session/new``; if the gateway substitutes the model, adopt it
        and re-issue once so a real session is actually created.

        The admin-tier policy advisory ("Model X is restricted ... Using Y
        instead.") comes back as a ``-32603`` *error frame with no sessionId* --
        the first attempt creates nothing. ``_wait_for_response`` records the
        substitute model in ``self._last_substitution_model``; here we pin
        ``self._model`` to it, re-seed ``settings.local.json`` (the claude
        backend builds a fresh SettingsManager per session/new, so the new model
        takes effect), and re-issue. Idempotent and bounded to ONE retry -- if the
        gateway substitutes again to the same/another restricted id we stop
        rather than loop. Returns the session/new response dict (possibly still
        without a sessionId, which the caller treats as a hard failure).

        The substitution retry is a claude-only path (kiro-cli never emits this
        advisory), and so is the re-seed: the kiro-cli branch writes no
        ``settings.local.json`` at all.
        """
        new_params: dict = {
            "cwd": await self._session_work_dir(),
            # kiro-cli loads servers from --agent; a harness in
            # ACP_BACKENDS_SESSION_MCP_ARRAY must be told here -- it reads no
            # agent spec of its own, so this array is the only channel Crew has
            # onto the session's MCP surface (translated from that same spec,
            # see acp/session_mcp.py). Empty on the kiro-cli path.
            # Pooled broker stubs are appended for kiro-cli: a session-injected
            # server outranks the same-named entry in the agent spec, which is
            # how pooling takes effect without writing a spec anywhere.
            # Each per-harness hook is spliced only for ITS OWN backend. An
            # edition overriding both would otherwise hand a claude session
            # codex's entries and vice versa -- and one entry whose transport the
            # adapter does not advertise fails the whole session/new, not just
            # that server. Both hooks are in-memory reads of a cache the spawn
            # path warmed, NOT executor hops: this call site is shared with
            # kiro-cli, and adapter work must not add a scheduling or failure
            # point to that backend's construction path (harness-parity H13).
            # The pooled read stays off the loop, as it already was.
            # A claude Side Chat session mounts no server: READ_ONLY refuses every
            # MCP call, and a tool that is not mounted cannot be pre-approved by
            # a settings allow rule. Checked at the splice, not in the seam, so
            # an edition's override of the seam cannot mount servers there.
            "mcpServers": [
                *(
                    self._claude_session_mcp_servers()
                    if self._is_claude and not getattr(self, "_side_read_only", False)
                    else []
                ),
                *(self._opencode_session_mcp_servers() if self._is_opencode else []),
                *(self._goose_session_mcp_servers() if self._is_goose else []),
                *(self._deepseek_session_mcp_servers() if self._is_deepseek else []),
                *(self._pi_session_mcp_servers() if self._is_pi else []),
                *(await asyncio.to_thread(self._pooled_mcp_servers)),
            ],
        }
        if self.backend in ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP:
            new_params["mcpServers"] = await self._prune_unstartable_mcp(new_params["mcpServers"])
        if self._is_claude:
            new_params["_meta"] = self._claude_session_meta()

        # The roster this session put ON THE WIRE. Distinct from the agent spec
        # on disk: the backend starts the spec's own servers too, so the frames
        # that come back are a superset of this list.
        self._begin_session_report(new_params.get("mcpServers"))
        # AFTER begin_session_report, which clears the report: the guard writes a
        # row on it, and writing before the clear would erase the finding.
        self._guard_unresolved_mcp_refs(new_params.get("mcpServers"))

        self._last_substitution_model = None
        if self._is_pi:
            await self._republish_pi_mcp_handoff()
        session_id = await self._send_request(METHOD_SESSION_NEW, new_params)
        session_resp = await self._wait_for_response(
            session_id,
            timeout=_INIT_TIMEOUT,
            method=METHOD_SESSION_NEW,
            expected_mcp=new_params.get("mcpServers"),
        )

        # Happy path: a real session came back.
        if session_resp.get("sessionId"):
            return session_resp

        # Substitution path: the gateway named a model it WILL serve. Adopt it,
        # re-seed settings so the backend resolves to it, and re-issue once.
        substitute = self._last_substitution_model
        if self._is_claude and substitute and substitute != self._model:
            # Redact the substitute name before logging. It is parsed straight
            # from the untrusted ACP backend advisory (data.details "Using X
            # instead") and the gateway log fans out to the dashboard activity
            # feed and Slack -- mirror the dual-redaction discipline applied
            # to the source advisory in _wait_for_response.
            _sub_log, _ = redact_exfiltration_urls(str(substitute))
            _sub_log, _ = redact_credentials(_sub_log)
            logger.warning(
                "ACP session/new returned a substitution advisory with no session; "
                "adopting gateway-served model %r and retrying session creation.",
                _sub_log,
            )
            self._model = substitute
            # Re-seed settings.local.json so the fresh SettingsManager the adapter
            # builds for the retry resolves the substitute model (it merges
            # settings sources each session/new).
            try:
                await asyncio.to_thread(self._write_claude_local_settings)
            except (OSError, ValueError, TypeError):
                # Narrow to realistic re-seed failure modes: OSError covers
                # disk / permission errors on the atomic write; ValueError
                # and TypeError cover registry / json shape surprises.
                # Never let re-seed failure mask the retry -- worst case, the
                # adapter resolves to whatever it had cached and we still
                # retry session/new on the substitute path.
                logger.warning("re-seed of settings.local.json failed", exc_info=True)
            # The writer owns every transition in the native permission surface
            # and invalidates the frozen MCP projection when that state changes.
            # Re-resolve off the event loop, then rebuild the exact array the retry
            # will send so a failed byte re-validation withholds rather than
            # re-delivering the first attempt's stale roster.
            resolved_mcp = await asyncio.to_thread(self._resolve_session_mcp_servers)
            self._session_mcp_cache = resolved_mcp
            new_params["mcpServers"] = [
                *(
                    self._claude_session_mcp_servers()
                    if self._is_claude and not getattr(self, "_side_read_only", False)
                    else []
                ),
                *(self._opencode_session_mcp_servers() if self._is_opencode else []),
                *(self._goose_session_mcp_servers() if self._is_goose else []),
                *(self._deepseek_session_mcp_servers() if self._is_deepseek else []),
                *(self._pi_session_mcp_servers() if self._is_pi else []),
                *(await asyncio.to_thread(self._pooled_mcp_servers)),
            ]
            if self.backend in ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP:
                new_params["mcpServers"] = await self._prune_unstartable_mcp(
                    new_params["mcpServers"]
                )
            # Rebuilt AFTER the array, from the same re-seed: the envelope carries
            # the setting sources the array was authorized under and the inline
            # settings naming the substitute model.
            new_params["_meta"] = self._claude_session_meta()
            self._begin_session_report(new_params.get("mcpServers"))
            self._guard_unresolved_mcp_refs(new_params.get("mcpServers"))
            self._last_substitution_model = None
            retry_id = await self._send_request(METHOD_SESSION_NEW, new_params)
            session_resp = await self._wait_for_response(
                retry_id,
                timeout=_INIT_TIMEOUT,
                method=METHOD_SESSION_NEW,
                expected_mcp=new_params.get("mcpServers"),
            )

        return session_resp

    def _initialize_params(self) -> dict[str, Any]:
        """The ``initialize`` request params, one spelling for every handshake.

        Read by :meth:`_initialize_session` and by the entitlement probe's own
        handshake (:meth:`_entitlement_probe_answer`), so the probe process is
        introduced to kiro-cli exactly as the session it probes for was.
        """
        protocol_version: int | str = _PROTOCOL_VERSION_BY_BACKEND.get(
            self.backend, PROTOCOL_VERSION
        )
        return {
            "protocolVersion": protocol_version,
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            "clientCapabilities": ACP_CLIENT_CAPABILITIES,
        }

    async def _fork_session(
        self, source_sid: str, message_id: str, capabilities: dict
    ) -> str | None:
        """Fork *source_sid* up to and including *message_id*: the new sid, or None.

        Only a harness in ``ACP_BACKENDS_FORK_AT_MESSAGE``, at a version that reads
        the ``_meta`` fork point sent here: a fork that ignored it would copy the
        WHOLE session, including everything after *message_id* that the caller is
        discarding.
        """
        session_capabilities = capabilities.get("sessionCapabilities")
        if not (
            self.backend in ACP_BACKENDS_FORK_AT_MESSAGE
            and _adapter_version_at_least(self._agent_version, CLAUDE_ACP_FORK_POINT_MIN_VERSION)
            and isinstance(session_capabilities, dict)
            and isinstance(session_capabilities.get("fork"), dict)
        ):
            return None
        params = {
            "sessionId": source_sid,
            "cwd": await self._session_work_dir(),
            "mcpServers": [],
            "_meta": {"jetbrains": {"air": {"fork": {"version": 1, "messageId": message_id}}}},
        }
        try:
            fork_id = await self._send_request(METHOD_SESSION_FORK, params)
            fork_resp = await self._wait_for_response(fork_id, timeout=_INIT_TIMEOUT)
        except (AcpError, AcpTimeoutError):
            logger.info(
                "session/fork of %s at %s failed, falling back to session/new",
                source_sid,
                message_id,
            )
            return None
        forked_sid = fork_resp.get("sessionId")
        if not isinstance(forked_sid, str) or not forked_sid:
            return None
        logger.info("ACP session forked: %s -> %s at %s", source_sid, forked_sid, message_id)
        return forked_sid

    async def _initialize_session(self) -> None:
        """Handshake: initialize → session/load or session/new → set_mode → set_model."""
        # 1. Initialize
        init_id = await self._send_request(METHOD_INITIALIZE, self._initialize_params())
        init_resp = await self._wait_for_response(init_id, timeout=_INIT_TIMEOUT)
        logger.info("ACP initialized (protocol=%s)", init_resp.get("protocolVersion"))

        # Whether this harness can restore a session at all, asked of whichever
        # capability the harness advertises it under. Both spellings are standard ACP
        # v1: ``loadSession`` is the flag for ``session/load``, and
        # ``sessionCapabilities.resume`` is the object -- an empty one means supported
        # -- for ``session/resume``. Keyed on the membership set rather than probing
        # both, so a harness cannot be read as restorable through a capability it
        # never advertised and then sent a verb it does not serve.
        capabilities = init_resp.get("agentCapabilities") or {}
        if self.backend in ACP_BACKENDS_RESUME_WITHOUT_LOAD:
            session_capabilities = capabilities.get("sessionCapabilities")
            self._can_load_session = isinstance(session_capabilities, dict) and isinstance(
                session_capabilities.get("resume"), dict
            )
        else:
            self._can_load_session = bool(capabilities.get("loadSession", False))
        # No harness this core drives narrows the session MCP array against the
        # advertised ``mcpCapabilities``: every one of them either reads no array
        # or accepts the shapes Crew already sends, opencode being the measured
        # case of the latter (stdio, http and sse alike -- see
        # ``_opencode_session_mcp_servers``). The harness that DOES narrow reads
        # the handshake on the core that drives it, in
        # ``acp.harness.codex.session_mcp_servers``, so nothing is held here.
        self._agent_version = agent_version_from_init(init_resp)
        self._agent_version_read = True
        self._steering_advertised = steering_advertised(init_resp)
        self._note_pi_adapter_version()
        self._note_goose_version()

        # The subprocess has now read its agent spec, which closes the window the
        # pre-spawn snapshot opened: a write landing before this point is caught here,
        # and one landing after cannot change what kiro-cli already loaded. BEFORE
        # session/load or session/new, so a session is never created on a spec nobody
        # verified. Off-loop: one stat, and a hash only when the stat differs.
        #
        # Converted to ``AcpError`` for the SAME reason the pre-spawn gate above is:
        # that is the type ``ensure_ready`` handles, and its handler is what ends the
        # child and drops the half-registered session state --
        # ``_cleanup_failed_live_spawn`` kills the process, releases the workspace
        # binding and discards the settings seed, and ``_reset_state`` closes the
        # pipes and clears the PIDs, the session id and the per-spawn spec caches.
        # A raw ``DerivedSpecStale`` (a ``RuntimeError``) matches none of those
        # handlers and would leave a live child running on an unverified spec. The one
        # retry that handler allows is not a way past this: it respawns, so it goes
        # back through the pre-spawn gate and either re-derives from the default as it
        # now stands or refuses again. Nothing continues on this child.
        try:
            await asyncio.to_thread(require_unchanged_derived_spec, self._derived_spec_snapshot)
        except DerivedSpecStale as exc:
            raise AcpError(str(exc)) from exc

        # 2. Try session/load if we have a resume ID and kiro-cli supports it
        self._resumed = False
        resume_sid = self._resume_session_id
        self._resume_session_id = None  # consume — no retry loop
        fork_request = self._fork_request
        self._fork_request = None
        if fork_request and not resume_sid and self._can_load_session:
            # A successful fork is then restored like any resume.
            resume_sid = await self._fork_session(*fork_request, capabilities)
        sent_snapshot: DerivedSpecSnapshot | None = None

        if resume_sid and self._can_load_session:
            # Only attempt session/load when the prior session transcript
            # actually exists on disk. Without this guard a stale persisted SID
            # (e.g. a slot reopened for a brand-new conversation) triggers a
            # session/load that REPLAYS the old transcript on top of the fresh
            # system prompt + memory injection — inflating base context to
            # ~38% on turn 1. kiro-cli stores transcripts at ~/.kiro/sessions/
            # cli/<sid>.json; a missing transcript falls back to session/new
            # (a genuinely fresh start).
            if self.backend in ACP_BACKENDS_HARNESS_OWNED_SESSIONS:
                # The harness keeps its own session records and resolves them from
                # the sessionId, so there is no Crew-side file to name. Gating on
                # the kiro transcript here would make file_ok always False -- such
                # a session could never resume, it would silently start fresh every
                # time -- which is why the _meta block below gives it no
                # session_file either. For claude the SDK transcript-path resolver
                # is the internal companion's; the public core simply attempts the
                # load.
                session_file = ""
                file_ok = True
            else:
                session_file = str(kiro_sessions_dir() / f"{resume_sid}.json")
                file_ok = Path(session_file).exists()
            if file_ok:
                # WHICH restore verb, from the same membership set that chose the
                # capability above. The two calls share a contract --
                # ``ResumeSessionRequest`` carries the same fields as
                # ``LoadSessionRequest``, and their responses carry the same fields --
                # so the params built below and every check after them are unchanged,
                # and only the method name differs. That is why this is a set and a
                # constant rather than a restore path per harness (harness-parity
                # H13).
                #
                # Resolved BEFORE the try, not inside it: the failure log in the
                # handler names this method, and a params-building failure would
                # otherwise reach that log with the name unbound.
                restore_method = (
                    METHOD_SESSION_RESUME
                    if self.backend in ACP_BACKENDS_RESUME_WITHOUT_LOAD
                    else METHOD_SESSION_LOAD
                )
                try:
                    load_params: dict = {
                        "sessionId": resume_sid,
                        "cwd": await self._session_work_dir(),
                        # kiro-cli gets its servers via --agent; a session-array
                        # backend must receive them here as well -- a resumed
                        # session re-declares everything Crew mounts on it or comes
                        # back with no tools (see session/new above). Pooled stubs are
                        # re-declared so a resumed session keeps talking to the
                        # broker. Gated per backend, and in-memory here vs
                        # off-loop there, for the same reasons as session/new.
                        "mcpServers": [
                            *(
                                self._claude_session_mcp_servers()
                                if self._is_claude and not getattr(self, "_side_read_only", False)
                                else []
                            ),
                            *(self._opencode_session_mcp_servers() if self._is_opencode else []),
                            *(self._goose_session_mcp_servers() if self._is_goose else []),
                            *(self._deepseek_session_mcp_servers() if self._is_deepseek else []),
                            *(self._pi_session_mcp_servers() if self._is_pi else []),
                            *(await asyncio.to_thread(self._pooled_mcp_servers)),
                        ],
                    }
                    if self.backend in ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP:
                        load_params["mcpServers"] = await self._prune_unstartable_mcp(
                            load_params["mcpServers"]
                        )
                    if self._is_claude:
                        load_params["_meta"] = self._claude_session_meta()
                    elif self.backend not in ACP_BACKENDS_HARNESS_OWNED_SESSIONS:
                        # The kiro family reads its transcript path from _meta. A
                        # harness that owns its sessions reads no _meta of ours,
                        # so it gets neither key rather than a kiro session_file
                        # it would not know what to do with.
                        load_params["_meta"] = {"_kiro.dev/session_file": session_file}
                    self._begin_session_report(load_params.get("mcpServers"))
                    # A resumed session re-declares everything Crew mounts on it, so it
                    # can be short of a referenced server exactly as a fresh one can.
                    self._guard_unresolved_mcp_refs(load_params.get("mcpServers"))
                    if self._is_pi:
                        await self._republish_pi_mcp_handoff()
                    load_id = await self._send_request(restore_method, load_params)
                    load_resp = await self._wait_for_response(
                        load_id,
                        timeout=_INIT_TIMEOUT,
                        method=restore_method,
                        expected_mcp=load_params.get("mcpServers"),
                    )
                    # A ``modes`` block is the shape kiro-cli and claude return on a
                    # successful load, and its absence on THOSE harnesses means the
                    # load did not really take. A member of
                    # ACP_BACKENDS_LOAD_WITHOUT_MODES never returns one, so for it a
                    # response that is not an error IS the successful load -- gating
                    # it on ``modes`` would fall through to session/new and discard
                    # the conversation the harness had just restored.
                    if "modes" in load_resp or self.backend in ACP_BACKENDS_LOAD_WITHOUT_MODES:
                        self._session_id = resume_sid
                        self._resumed = True
                        self._capture_available_models(load_resp)
                        sent_snapshot = self._session_mcp_snapshot
                        if self._uses_advertised_model_selection:
                            await self._persist_advertised_models_if_changed()
                            await self._reseed_after_capture()
                        self._store_session_config(load_resp)
                        logger.info("ACP session resumed: %s", resume_sid)
                except (AcpError, AcpTimeoutError):
                    logger.info(
                        "%s failed for %s, falling back to session/new",
                        restore_method,
                        resume_sid,
                    )
                else:
                    # The ``else`` of the try, so this runs ONLY on a load that did not
                    # raise -- and OUTSIDE it, so the refusal below cannot be caught as
                    # a failed load. Gated here rather than inside the method so the
                    # first-class kiro path gains no call and no failure point in
                    # service of an adapter (harness-parity H13).
                    #
                    # A RESTORED session carries the mode it was left in, which the
                    # environment seed does not re-apply, so this is what keeps a
                    # resumed session from coming back permissive. Conditioned on
                    # ``_resumed`` because a load that returned no ``modes`` block did
                    # not take: that path falls through to session/new, which does its
                    # own read-back on the response that replaces this one.
                    if self._is_goose and self._resumed:
                        self._verify_goose_routing(load_resp)
                    if self._is_claude and self._resumed:
                        await self._pin_claude_starting_mode(load_resp)
            else:
                logger.info("Session file missing for %s, skipping load", resume_sid)

        # 3. Create new session if load didn't succeed. On the claude backend a
        # model-substitution advisory comes back as an error frame with NO
        # sessionId; _new_session_following_substitution adopts the substitute
        # model and re-issues once so a real session is actually created.
        if not self._session_id:
            # Capture the requested model before the helper runs. The helper resets
            # self._last_substitution_model = None on every return path, so we can't
            # use it to detect whether substitution happened. Comparing self._model
            # before vs after is the reliable signal.
            model_before = self._model
            session_resp = await self._new_session_following_substitution()
            sent_snapshot = self._session_mcp_snapshot
            self._session_id = session_resp.get("sessionId")
            self._capture_available_models(session_resp)
            if self._uses_advertised_model_selection:
                await self._persist_advertised_models_if_changed()
                await self._reseed_after_capture()
            self._store_session_config(session_resp)
            # Before the first prompt, and before the session id is even checked: a
            # session that cannot establish the asking posture is one where none of
            # Crew's tool controls execute. Gated HERE rather than inside the method so
            # the first-class kiro path gains no call and no failure point in service of
            # an adapter (harness-parity H13).
            if self._is_goose:
                self._verify_goose_routing(session_resp)
            if not self._session_id:
                # Both the initial attempt and the substitution retry failed to
                # yield a session. Raise a clear, actionable error instead of
                # letting the next step die on the opaque "Cannot set config
                # option before session is initialized" guard.
                # self._model can be backend-derived (the substitute adopted
                # by _new_session_following_substitution from the ACP advisory),
                # and AcpError chains through to the dashboard activity feed
                # and Slack. Dual-redact before interpolating, same discipline
                # as the logger paths.
                # Standardize redacted-local naming across this file: the
                # convention is _<source>_log so the reader sees what was redacted.
                _model_for_error_log, _ = redact_exfiltration_urls(str(self._model))
                _model_for_error_log, _ = redact_credentials(_model_for_error_log)
                raise AcpError(
                    "session/new returned no sessionId"
                    + (
                        f" even after adopting substitute model {_model_for_error_log!r}"
                        if self._model != model_before
                        else ""
                    )
                    + "; the backend did not create a session."
                )
            # self._model can be backend-derived if _new_session_following_substitution
            # adopted the gateway substitute. Redact it consistently with the
            # warning-log path so any URL / credential-shaped substitute id never
            # reaches the dashboard activity feed or Slack unredacted.
            _model_log, _ = redact_exfiltration_urls(str(self._model))
            _model_log, _ = redact_credentials(_model_log)
            logger.info("ACP session created: %s (model=%s)", self._session_id, _model_log)
            if self._is_claude:
                await self._pin_claude_starting_mode(session_resp)
        self._last_activity = time.monotonic()
        # The session exists, so the pi process that read the MCP handoff file has
        # started; the file has no further reader and goes now.
        if self._is_pi:
            await self._discard_pi_mcp_handoff()

        # Seek to end of JSONL so we only read new tool results.
        # claude-agent-acp stores sessions via its own SDK, not ~/.kiro/ — skip.
        if self._session_id and self._is_kiro:
            _jpath = kiro_sessions_dir() / f"{self._session_id}.jsonl"
            try:
                self._jsonl_pos = _jpath.stat().st_size if _jpath.exists() else 0
            except OSError:
                self._jsonl_pos = 0

        # The host has now consumed the ``mcpServers`` array, by whichever route created
        # the session -- ``session/load``, ``session/new``, or the substitution retry --
        # and every route sends the same cached array. For an array-backed host that
        # array IS the derived spec: the child reads no spec of its own, so the
        # ``initialize`` check above (which judges the kiro-cli ``--agent`` read) does
        # not cover it. This is the other end of THAT bracket: a write landing between
        # the array's build and this point is caught, and one landing after cannot
        # change what the host already registered. ``None`` for every agent that
        # mirrors nothing, and for the kiro-cli path, which sends no array. Converted
        # to ``AcpError`` for the reason the two checks above are: that is the type
        # ``ensure_ready`` handles, and its handler is what ends the child and drops
        # the half-registered session state.
        try:
            await asyncio.to_thread(require_unchanged_derived_spec, sent_snapshot)
        except DerivedSpecStale as exc:
            raise AcpError(str(exc)) from exc

        # 4. Activate agent via set_mode -- kiro only. claude-agent-acp does handle
        #    session/set_mode, but its modeIds are permission modes (default,
        #    acceptEdits, plan, ...), never an agent name, so there is no agent to
        #    activate there. _pin_claude_starting_mode uses it for the permission mode.
        #    Guard (A): fire only when the backend advertised this agent, or
        #    advertised no modes at all (older kiro-cli / fake → attempt,
        #    backward-compatible). If modes ARE advertised but this agent is
        #    absent, its ~/.kiro/agents/<agent>.json didn't load — FAIL CLOSED
        #    with an actionable error rather than silently running kiro-cli's
        #    default (broader) mode, which for a restricted agent is a privilege
        #    escalation. Self-heal (B, in _spawn) regenerates the managed default
        #    so the common case never reaches this branch.
        if self._is_kiro:
            # The advertised ids were rewritten to each agent's DECLARED name by
            # the projection's reverse map (frame()). The launch identity is
            # ``self._agent`` -- kiro-cli resolves ``--agent`` by the declared
            # name, so that is the id the start compares against -- while the
            # modeId sent is the same launch identity, which request() tolerates
            # for the agent's own first activation.
            if not self._modes_advertised or self._agent in self._available_mode_ids:
                # The answer is not awaited, so the model this activation puts the
                # session on is judged from its agent/switched frame wherever that
                # frame is read (_note_agent_switched).
                self._activated_model = ""
                self._mode_frame_awaited = True
                self._mode_frame_superseded = False
                await self._send_request(
                    METHOD_SET_MODE,
                    {"sessionId": self._session_id, "modeId": self._agent},
                )
                logger.info("ACP agent activated: %s", self._agent)
            else:
                cause, remedy = await asyncio.to_thread(unavailable_mode_explanation, self._agent)
                raise AcpError(
                    f"Agent mode {self._agent!r} is not available on this session "
                    f"(advertised modes: {self._available_mode_ids or 'none'}); {cause} "
                    f"Refusing to run the backend default mode in its place. {remedy}"
                )

        # 5. Set model — override if KiroCrew config specifies non-default.
        await self._apply_startup_model()

        # 6. Arm permission routing for harnesses whose asking is a session
        #    config option. AFTER the model apply (both write config options, and
        #    the permission one must land last) and before any prompt can run:
        #    _initialize_session is entirely pre-prompt, which is what makes this
        #    placement the guarantee rather than a best effort.
        #    Gated HERE rather than inside the method, so the first-class Kiro path
        #    gains no call, no await and no failure point in service of an adapter
        #    (harness-parity H13). A positive membership test, never "not claude".
        if acp_tool_gate.routing_for(self.backend) is acp_tool_gate.Routing.SESSION_CONFIG:
            await self._apply_session_permission_routing()

        # (settings.local.json is re-seeded up in step 2/3, beside the model-cache
        #  persist, rather than as a step of its own down here: see
        #  _reseed_after_capture on why it rides an EXISTING adapter-only branch.)

        # Drain MCP server init notifications
        await self._drain_notifications()

        # The drain usually reads step 4's agent/switched frame; judge the model
        # it named before the first prompt can run on it.
        await self._judge_activated_model()

    async def ensure_ready(self) -> None:
        """Ensure process is spawned and session is initialized.

        Runs before EVERY prompt, so the warm path must stay syscall-free:
        the work dir is created once per instance (off-loop) and remembered —
        a per-prompt mkdir would tax every prompt with a blocking syscall
        whose latency scales with the parent directory's entry count.
        Re-creating the directory later could not repair a live child anyway:
        a process's cwd is bound to the inode, not the path.
        """
        if (
            platform_compat.IS_WINDOWS
            and self._process
            and (
                self._process.returncode is not None
                or getattr(self, "_windows_tree_cleanup_failed", False) is True
            )
        ) or getattr(self, "_stdin_stalled", False):
            # A stalled child stopped reading stdin with a frame possibly
            # buffered: it is killed BEFORE a replacement starts, so it cannot
            # act on that frame while the new turn runs.
            await self._kill_process(force=True)
            if (
                getattr(self, "_stdin_stalled", False)
                and self._process is not None
                and self._process.returncode is None
            ):
                # Not confirmed dead: stay stalled, so the next ensure_ready
                # retries the kill rather than spawning beside the old child.
                raise AcpProcessDied(
                    "the stalled ACP child could not be confirmed dead; "
                    "not starting a replacement beside it"
                )
            try:
                await self._discard_claude_settings_seed()
            finally:
                self._reset_state()
        if not self._work_dir_ready:
            await asyncio.to_thread(ensure_directory, self._work_dir)
            self._work_dir_ready = True
        if self._process and self._process.returncode is None and self._session_id:
            if getattr(self, "_activated_model", ""):
                # An agent/switched frame read after the start drain (inside a
                # prompt) is judged here, before the next prompt goes out.
                await self._judge_activated_model()
            return

        # Telemetry (kirocrew.session.startup.duration): time the cold-start work
        # below (spawn + session init) and emit in the finally so every exit path
        # — success, auth-required, error — is measured. The warm fast-path above
        # is intentionally NOT measured (no startup work). Best-effort: a
        # telemetry failure must never affect session startup.
        _startup_t0 = time.monotonic()
        _startup_spawned = False
        # Default "error": any exit that is NOT the explicit success path below
        # — including an unexpected non-Acp exception propagating through the
        # finally — is recorded as a failure, never a false "ready".
        _startup_outcome = "error"
        try:
            # Retry once — kiro-cli first launch can be slow (MCP server init),
            # and transient failures (MCP crash, bad config read) are recoverable.
            for attempt in range(2):
                try:
                    if self._process and self._process.returncode is not None:
                        await self._discard_bound_workspace()
                        # `finally`, because the discard is an await and this runs on
                        # cancellable paths: the in-memory reset must land even when
                        # the cancellation arrives during the seed's settle. The
                        # settle itself is shielded, so it completes regardless.
                        try:
                            await self._discard_claude_settings_seed()
                        finally:
                            self._reset_state()

                    if not self._process:
                        await self._spawn()
                        _startup_spawned = True

                    await self._initialize_session()
                    try:
                        await self._snapshot_process_tree()
                    except Exception:
                        logger.warning("Failed to snapshot process tree", exc_info=True)

                    _startup_outcome = "ready"
                    return
                except AcpToolGateUnroutable:
                    # Non-retryable BY CONSTRUCTION (see the class docstring): the
                    # refusal is a configuration fact, so a respawn re-reads the same
                    # answer and refuses again -- a wasted spawn plus teardown that
                    # also spends the reconnect budget this DISTINCT type exists to
                    # protect. Must sit BEFORE the generic transport handler, because
                    # it subclasses AcpError and would otherwise be retried by it,
                    # which is the shape that made the distinct type decorative.
                    _startup_outcome = "tool_gate_unroutable"
                    await self._cleanup_failed_live_spawn()
                    self._reset_state()
                    raise
                except (AcpTimeoutError, AcpError, OSError) as exc:
                    # A sandbox that refused to initialize is DETERMINISTIC, so
                    # the second attempt below would rebuild the same profile on
                    # the same host and be refused identically -- paying a spawn
                    # and a teardown to report the same thing later, with the one
                    # line that names the fix buried in a generic exit card. Fail
                    # fast with the layer named instead, exactly as the
                    # ``AcpToolGateUnroutable`` arm above does for a
                    # configuration refusal. Placed inside the generic handler
                    # rather than as its own ``except`` clause because the
                    # signature is on the CHILD's stderr, not on the exception:
                    # the same refusal surfaces as an EOF ``AcpError``, as an
                    # ``AcpTimeoutError`` when the child dies before answering
                    # ``initialize``, and as an ``OSError`` on the write that
                    # follows it.
                    sandbox_failure = await self._classify_failed_start(self._sandbox_init_failure)
                    if sandbox_failure is not None:
                        _startup_outcome = "sandbox_init_failed"
                        await self._cleanup_failed_live_spawn()
                        self._reset_state()
                        raise sandbox_failure from exc
                    # The harness answered with its OWN "no provider / not signed
                    # in" words (declared in ``host_auth``). Deterministic like the
                    # sandbox refusal above: a fresh process reads the same missing
                    # configuration, so fail fast with the message that names the
                    # fix instead of a retry and then a raw JSON-RPC frame. A plain
                    # non-transient ``AcpError`` rather than ``AcpAuthRequired``: the
                    # dashboard reads that type as "Kiro is signed out" and would
                    # mark a valid kiro-cli login as not ready.
                    if isinstance(exc, AcpError) and host_auth.reports_signed_out(
                        self.backend, str(exc)
                    ):
                        _startup_outcome = "auth_required"
                        await self._cleanup_failed_live_spawn()
                        self._reset_state()
                        raise AcpError(
                            host_auth.signed_out_message(self.backend),
                            transient=False,
                            user_worded=True,
                        ) from exc
                    if attempt == 0:
                        logger.warning("ACP init failed (%s), retrying with fresh process...", exc)
                        await self._cleanup_failed_live_spawn()
                        self._reset_state()
                        if isinstance(exc, OSError):
                            await asyncio.sleep(_ACP_RESPAWN_BACKOFF_S)
                    else:
                        # Read the ring BEFORE the cleanup below clears it: a
                        # startup that died with a throttled registration on its
                        # stderr is pre-prompt by construction, so the typed
                        # transient subclass is the accurate verdict here too.
                        _throttled = await self._classify_failed_start(
                            self._registration_throttle_line
                        )
                        # AcpAuthRequired subclasses AcpError; label it distinctly
                        # so a not-logged-in exit is never counted as a generic
                        # startup error. (Only a harness's declared signed-out
                        # phrase fails fast, above; other auth answers keep the retry.)
                        if isinstance(exc, AcpAuthRequired):
                            _startup_outcome = "auth_required"
                        elif _throttled is not None:
                            _startup_outcome = "registration_rate_limited"
                        else:
                            _startup_outcome = "error"
                        await self._cleanup_failed_live_spawn()
                        self._reset_state()
                        if _throttled is not None and not isinstance(exc, AcpAuthRequired):
                            raise registration_rate_limited_error(
                                "ACP session startup failed", _throttled
                            ) from exc
                        raise
        finally:
            try:
                # circular import: importing get_recorder at module top would
                # form config.loader -> acp.types -> acp.client -> metrics.provider
                # -> config.loader (provider reads KiroCrewConfig). Keep it lazy so
                # provider is never loaded during config.loader's import chain.
                from kiro_crew.metrics.provider import get_recorder

                get_recorder().histogram(
                    "kirocrew.session.startup.duration",
                    (time.monotonic() - _startup_t0) * 1000.0,
                    unit="ms",
                    attrs={"outcome": _startup_outcome, "spawned": _startup_spawned},
                )
            except Exception:  # never let telemetry break session startup
                logger.debug("session startup metric emit failed", exc_info=True)

    # ── Prompt Loop Helpers ──

    def _process_message(self, msg: JsonRpcMessage, req_id: int) -> str:
        """Classify a message into an action string.

        Actions: "complete", "error", "steer_answer", "permission", "update",
        "metadata", "server_request_unknown", "skip".
        """
        if msg.is_response_for(req_id):
            return "error" if msg.error else "complete"

        steer_turn = getattr(self, "_steer_turn", None)
        if msg.method is None and steer_turn is not None and msg.id in steer_turn.steers:
            return "steer_answer"

        if msg.is_method(METHOD_REQUEST_PERMISSION):
            return "permission"

        if msg.is_method(METHOD_SESSION_UPDATE):
            self._note_active_run(msg)
            return "update"

        if msg.method is None and msg.id is not None and self._settle_steer_answer(msg):
            return "skip"

        if msg.is_method(METHOD_METADATA):
            return "metadata"

        if msg.is_method(METHOD_COMPACTION_STATUS):
            return "compaction"

        if msg.is_method(METHOD_CLEAR_STATUS):
            return "clear"

        if msg.is_method(METHOD_AGENT_SWITCHED):
            return "agent_switched"

        if msg.is_method(METHOD_MCP_OAUTH_REQUEST):
            return "mcp_oauth_request"

        if msg.is_method(METHOD_MCP_SERVER_INITIALIZED):
            return "mcp_server_initialized"

        if msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE):
            return "mcp_server_init_failure"

        if msg.is_method(METHOD_SUBAGENT_LIST_UPDATE):
            return "subagent_list"

        if msg.is_method(METHOD_KIRO_SESSION_UPDATE):
            return "subagent_activity"

        # Unknown server→client REQUEST (has both method AND id). Per JSON-RPC
        # the agent blocks until it gets a response, so it must be answered
        # (with -32601 by the dispatch sites) rather than silently skipped —
        # otherwise the agent hangs forever. Known requests (request_permission)
        # are handled above; only genuinely unrecognized requests reach here.
        if msg.method is not None and msg.id is not None:
            return "server_request_unknown"

        return "skip"

    async def _defer_opencode_shell_permission(self, msg: JsonRpcMessage, remaining: float) -> bool:
        """Queue a matching command update before its OpenCode shell permission."""
        params = msg.params if isinstance(msg.params, dict) else {}
        tool_call = params.get("toolCall")
        tool_call = tool_call if isinstance(tool_call, dict) else {}
        tool_call_id = tool_call.get("toolCallId")
        if (
            not isinstance(tool_call_id, str)
            or self._tool_call_is_shell.get(tool_call_id) is not True
        ):
            return False
        cached = self._tool_call_params.get(tool_call_id, {})
        if isinstance(cached, dict) and cached.get("command"):
            return False

        # OpenCode can ask permission before its command-bearing update. Read
        # only the next frame to preserve unrelated event order. The request's
        # own rawInput must never fill the trusted security cache.
        following = await self._read_message(
            timeout=min(_OPENCODE_PERMISSION_REFINEMENT_TIMEOUT, remaining)
        )
        if following is None:
            return False

        follow_params = following.params if isinstance(following.params, dict) else {}
        update = follow_params.get("update")
        update = update if isinstance(update, dict) else {}
        raw_input = update.get("rawInput")
        buffer_capacity = self._buffer.maxlen
        if not (
            following.is_method(METHOD_SESSION_UPDATE)
            and isinstance(params.get("sessionId"), str)
            and follow_params.get("sessionId") == params.get("sessionId")
            and update.get("sessionUpdate") == "tool_call_update"
            and update.get("toolCallId") == tool_call_id
            and update.get("status") == "in_progress"
            and isinstance(raw_input, dict)
            and isinstance(raw_input.get("command"), str)
            and raw_input["command"]
            and not update.get("content")
            and not update.get("rawOutput")
            and (buffer_capacity is None or len(self._buffer) <= buffer_capacity - 2)
        ):
            self._buffer.appendleft(following)
            return False

        # The normal dispatcher caches the command before building permission.
        self._buffer.appendleft(msg)
        self._buffer.appendleft(following)
        return True

    async def _prompt_loop(
        self,
        req_id: int,
        timeout: float,
    ) -> AsyncGenerator[tuple[str, JsonRpcMessage], None]:
        """Core prompt read loop. Yields (action, msg) pairs.

        Always releases ``_turn_done`` on exit — including abnormal exits
        (process death, cancel-grace exceeded, or a caller that raises on an
        ``error`` action and closes this generator). Without the ``finally``,
        those paths bypass the callers' trailing ``_turn_done.set()`` and a
        ``wait_turn_done`` waiter (the cooperative-stop ack) blocks for its
        full budget before escalating to a session-losing hard kill.
        """
        # L1 turn-lock: serialize the whole read turn on this client's single
        # stdout StreamReader so two _bg streaming turns can't both park on
        # readline() and trip "readuntil() called while another coroutine is
        # already waiting". Acquired here (every streaming consumer funnels
        # through _prompt_loop), released in the finally — see the __init__
        # comment for the finalization + coverage caveats.
        await self._turn_lock.acquire()
        try:
            # Retire the liveness state HERE, under the lock, because this is the
            # one point every prompt path funnels through: send_message (via
            # _read_prompt_response), send_message_stream, and _dispatch_events.
            # Retiring in a caller's prologue instead would (a) miss the direct
            # prompt APIs, leaving their next turn gated by the previous turn's
            # wedged walk, and (b) run BEFORE this acquire, letting a queued turn
            # clear the active turn's tracked consult and so allow a second walk
            # while the first is still pending.
            self._retire_liveness_state()
            # A goose run id and the steers awaiting their echo belong to one
            # turn: the new prompt's run id is announced by a frame this loop
            # has not read yet, and a steer the last turn never echoed was
            # discarded with that run and already queued by its caller.
            self._active_run_id = None
            self._steers_awaiting_echo = {}
            self._compaction_failed_at = None
            self._compaction_failed_turn = False
            self._claude_compaction_pending = False
            self._codex_compaction_pending = False
            deadline = time.monotonic() + timeout
            consecutive_empty = 0
            last_data_ts = time.monotonic()
            # Consumer park accounting (mirrors AcpSessionHandle._dispatch_events):
            # the interval between this generator's yield and its resume is
            # CONSUMER time — a human approval prompt parks the whole generator
            # chain at that yield — so the post-compaction-failure idle clock
            # below must subtract it or a long approval wait reads as backend
            # silence and the budget reaps a live turn. `parked_at_data`
            # snapshots the accumulator when `last_data_ts` is taken, so only
            # park time accrued SINCE the last frame is excluded.
            parked_total = 0.0
            parked_at_data = 0.0
            _last_yield = time.monotonic()

            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                # Post-compaction-failure budget: a `failed` status arrived and the
                # backend has since gone silent past the budget, so no prompt
                # response or end_turn is coming. Stop reading so _dispatch_events
                # ends the turn explicitly and the runner releases the slot,
                # instead of draining to `deadline` (hours).
                #
                # SUSPENDED while a tool is in flight: kiro-cli can recover from a
                # failed compaction and dispatch a tool, and a legitimately silent
                # long tool (a build, a spawned subagent) would then be reaped at
                # 60s — killing valid work that the tool-stall watchdog already
                # governs on its own, much longer, liveness-gated budget. A tool
                # dispatch is positive evidence the turn is alive, so this budget
                # hands off to _TOOL_STALL_TIMEOUT and re-arms when the tool
                # resolves (_tool_dispatched cleared in _dispatch_events).
                #
                # Clock asymmetry with AcpSessionHandle's twin is INTENTIONAL: this
                # path owns a dedicated process, so every frame is its own and the
                # park accounting below is what it needs; the shared-runtime twin
                # instead needs session-attributable frames (a co-tenant's fanout
                # must not defer the reap). Keep both when touching either.
                if self._compaction_failed_at is not None and not self._tool_dispatched:
                    _compact_idle = max(
                        0.0,
                        (time.monotonic() - max(self._compaction_failed_at, last_data_ts))
                        - max(0.0, parked_total - parked_at_data),
                    )
                    if _compact_idle > _COMPACTION_FAILED_TURN_BUDGET:
                        logger.warning(
                            "Compaction failed for req %d and no prompt response "
                            "arrived for %.0fs — ending the turn.",
                            req_id,
                            _compact_idle,
                        )
                        self._compaction_failed_turn = True
                        return

                # Cooperative yield, placed where the previous frame is fully
                # handled and the next one is not yet read: no frame is held in a
                # local here, so a cancellation landing on this yield drops
                # nothing. `_read_message` is NOT a suspension point when input is
                # already buffered -- it returns `self._buffer.popleft()` outright,
                # and `StreamReader.readline` returns without awaiting when the
                # line is already in its buffer -- so without this a burst drains
                # inside one task step. Twin of
                # AcpSessionHandle._dispatch_events; both read the same budget.
                _now = time.monotonic()
                if _now - _last_yield >= DRAIN_YIELD_AFTER_S:
                    await asyncio.sleep(0)
                    _last_yield = time.monotonic()

                msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
                if msg is None:
                    consecutive_empty += 1
                    if consecutive_empty >= _MAX_CONSECUTIVE_EMPTY and not self._is_process_alive():
                        rc = self._process.returncode if self._process else "?"
                        # A death whose retained stderr shows a throttled
                        # registration is endpoint throttling, not a crash:
                        # raise the typed transient subclass so the retry
                        # ladders recover it (a respawn through ensure_ready
                        # genuinely retries registration on this transport).
                        _throttled = await self._registration_throttle_line()
                        if _throttled is not None:
                            raise registration_rate_limited_error(
                                f"Process exited during prompt (exit code {rc})", _throttled
                            )
                        raise AcpProcessDied(f"Process exited during prompt (exit code {rc})")
                    # Staleness check: if caller set _stale_eligible (text was
                    # streamed) and kiro-cli has gone silent, exit early.
                    # Fold in _last_activity (refreshed by the stderr drain) so a
                    # turn that is still streaming thinking_tokens on stderr —
                    # between its final text chunk and its next tool call — is not
                    # mistaken for silence. Using only the stdout clock
                    # (last_data_ts) trips a false stale-turn on every multi-turn
                    # reasoning step, burning ~_STALE_TURN_TIMEOUT s per turn and
                    # reaping long subagent runs at the timeout.
                    last_seen = max(last_data_ts, self._last_activity)
                    if self._stale_eligible:
                        # Consult the liveness oracle on EVERY silent read once
                        # text has streamed — not only at the timeout. The oracle
                        # needs a prior sample to compute a movement delta (its
                        # first call after a boundary retirement always reads
                        # UNKNOWN/"sampling"),
                        # so a single consult at the 90s mark would always reap.
                        # Sampling each silent read primes the baseline and keeps
                        # the movement window recent, mirroring the kiro path's
                        # per-tick consult. The consult is /proc-based and
                        # offloaded so it cannot block the read loop, and degrades
                        # to a non-WORKING verdict on any error (fail toward
                        # reaping). In production, silent reads recur at the
                        # ~_READ_TIMEOUT cadence, giving well-spaced samples.
                        #
                        # Snapshot the idle window BEFORE awaiting the consult.
                        # The walk is OUR OWN probe and a cold one costs tens of
                        # milliseconds (executor warm-up, then the subtree walk);
                        # reading the clock AFTER it charges that latency to the
                        # BACKEND's silence budget, so on a loaded host the first
                        # silent read could cross the cutoff carrying the only
                        # verdict the oracle can give before it has a baseline,
                        # and reap a turn that was streaming a moment earlier.
                        _idle_secs = time.monotonic() - last_seen
                        verdict, evidence = await self._consult_liveness_model_wait()
                        if _idle_secs > _STALE_TURN_TIMEOUT:
                            # Past the cutoff: a backend still doing work (CPU/IO
                            # movement in its subtree — a long model generation or
                            # a spawned build) reads WORKING and we keep waiting.
                            # Only WORKING extends; any other verdict
                            # (DEAD/UNKNOWN/STUCK_INPUT) preserves the conservative
                            # end-the-turn behavior, so hang recovery is never
                            # weakened (still bounded by _DEFAULT_PROMPT_TIMEOUT).
                            if verdict == VERDICT_WORKING:
                                logger.debug(
                                    "Stale-turn deferral for req %d — idle %.0fs but "
                                    "backend WORKING (%s)",
                                    req_id,
                                    _idle_secs,
                                    evidence,
                                )
                                continue
                            if evidence == EVIDENCE_SAMPLING:
                                # The oracle stored a BASELINE and has nothing to
                                # compare it against yet, so this verdict is
                                # structurally incapable of reading WORKING: it
                                # means "ask me again", not "idle". Reaping on it
                                # ends a live turn on no evidence at all — the
                                # per-silent-read consulting above exists to make
                                # that unlikely, and deferring here makes it
                                # impossible rather than merely improbable. The
                                # next silent read has a baseline and answers for
                                # real, so recovery is delayed by one read, not
                                # weakened.
                                logger.debug(
                                    "Stale-turn deferral for req %d — idle %.0fs but the "
                                    "liveness baseline is still priming (%s)",
                                    req_id,
                                    _idle_secs,
                                    evidence,
                                )
                                continue
                            if self._tool_dispatched:
                                # An OPEN TOOL CALL is positive evidence the turn
                                # is alive, so it defers this cutoff whatever the
                                # verdict says. Only kiro-cli streams
                                # tool_call_update progress frames while a tool
                                # runs; a backend that runs the tool to completion
                                # and only then reports the result is silent for
                                # the whole tool, and with text streamed before
                                # the dispatch this cutoff would tear the turn down
                                # MID-TOOL — reporting truncated work as complete.
                                # Deliberately NOT a `continue`:
                                # falling through hands the silence to the
                                # tool-stall watchdog below, which governs exactly
                                # this case on its own much longer budget and
                                # RECOVERS the slot instead of completing the
                                # turn. Mirrors the compaction-failed budget's
                                # suspension above; _tool_dispatched is cleared
                                # when the tool resolves, so the cutoff re-arms
                                # for the model-wait silence it is actually for.
                                logger.debug(
                                    "Stale-turn deferral for req %d — idle %.0fs with a tool "
                                    "call still open (liveness=%s: %s); tool-stall watchdog "
                                    "governs",
                                    req_id,
                                    _idle_secs,
                                    verdict,
                                    evidence,
                                )
                            else:
                                logger.warning(
                                    "Stale turn detected for req %d — no data for %.0fs after text was streamed "
                                    "(liveness=%s: %s). Treating as complete.",
                                    req_id,
                                    _idle_secs,
                                    verdict,
                                    evidence,
                                )
                                return
                    # Tool-stall watchdog: a tool was dispatched but NOTHING has
                    # come back (no result, no progress, no permission) for the
                    # stall window.  This is the silent-hang case where
                    # _stale_eligible is False (cleared on tool_call) so the
                    # check above never fires.
                    #
                    # Recovery (not just detection): the original bare ``return``
                    # abandoned the turn but left the kiro-cli child ALIVE
                    # mid-prompt, so the slot wedged and every later prompt hit
                    # "Prompt already in progress" until the whole backend was
                    # killed by hand.  Instead, kill the wedged child so the next
                    # prompt cold-starts, and raise AcpProcessDied so the existing
                    # recovery path takes over: the dashboard resets the session
                    # and re-queues the message (bounded by _acp_pipe_death_retries
                    # → "Session stuck" after 3), and cron/other callers surface a
                    # clean error instead of a hung turn.  _kill_process only
                    # touches the subprocess/pipes (never _turn_lock), so it is
                    # safe to call here — the finally still releases the lock.
                    #
                    # Use max(last_data_ts, _last_activity) so that MCP tools
                    # which ping /api/session-keepalive (wait, spawn_sub_agents)
                    # keep the watchdog satisfied even though no JSON-RPC frames
                    # arrive on stdout during their execution.
                    _tool_last_seen = max(last_data_ts, self._last_activity)
                    if (
                        self._tool_dispatched
                        and (time.monotonic() - _tool_last_seen) > _TOOL_STALL_TIMEOUT
                    ):
                        _stall_idle = time.monotonic() - _tool_last_seen
                        logger.warning(
                            "Tool stall detected for req %d — tool dispatched but no data for %.0fs. "
                            "Killing agent to recover the slot.",
                            req_id,
                            _stall_idle,
                        )
                        await self._kill_process(force=True)
                        raise AcpProcessDied(
                            f"tool stalled — no data for {_stall_idle:.0f}s; agent killed to recover"
                        )
                    continue

                consecutive_empty = 0
                last_data_ts = time.monotonic()
                parked_at_data = parked_total
                # NB: do NOT clear _tool_dispatched here.  The last_data_ts reset
                # above already prevents false positives for tools that stream
                # progress frames (each frame restarts the _TOOL_STALL_TIMEOUT
                # countdown).  Clearing the flag on every inbound frame would
                # disarm the watchdog after a single progress frame, so a tool
                # that emits one frame then silently stalls would hang anyway —
                # exactly the bug this watchdog targets.  The flag is cleared
                # only when a tool actually resolves or the turn completes
                # (see _dispatch_events).
                self.last_prompt_stats.event_count += 1

                # The answer to a steer sent into this turn is read here, by
                # the one consumer of stdout, and handed to the waiting steer.
                if self._settle_steering_answer(msg):
                    continue

                action = self._process_message(msg, req_id)
                if (
                    action == "permission"
                    and self.backend == ACP_BACKEND_OPENCODE
                    and await self._defer_opencode_shell_permission(msg, remaining)
                ):
                    self.last_prompt_stats.event_count -= 1
                    continue
                # Single yield chokepoint: everything downstream (dispatch,
                # chat runner, a human answering an approval card) runs while
                # this generator is suspended here, so the whole gap is
                # consumer time. `finally` so an abandoned generator's
                # GeneratorExit still closes the park.
                _parked_since = time.monotonic()
                try:
                    yield action, msg
                finally:
                    parked_total += max(0.0, time.monotonic() - _parked_since)
        finally:
            self._turn_lock.release()
            # Nothing reads a steer's answer once this loop is gone, so the turn
            # stops taking steers on every exit.
            if self._steering_turn_req == req_id:
                self._steering_turn_req = None
            # Release any cooperative-stop waiter regardless of how the loop
            # ends. The callers set the precise stop reason on the clean
            # "complete" path before this runs (idempotent); on abnormal exit
            # the reason stays "" → provider.cancel reports "timeout" →
            # escalates to hard kill, the correct outcome for a dead turn.
            if not self._turn_done.is_set():
                self._turn_done.set()

    async def _consult_liveness_model_wait(self) -> tuple[str, str]:
        """Liveness verdict for the stale-turn gate, offloaded off the loop.

        The oracle walks ``/proc`` for the backend subprocess subtree — a
        synchronous filesystem walk that can block on a wedged fd — so it runs
        on ``subprocess_executor()`` under a bounded timeout, the same treatment
        the runtime's other /proc probes get. Any failure or timeout degrades to
        a non-WORKING verdict so the caller falls through to ending the turn
        (fail toward reaping, never toward hanging).

        Scope, mirroring the shared-runtime oracle this converges onto:
        - Linux-only evidence. On a host without ``/proc`` the verdict is
          UNKNOWN → the turn is reaped at the cutoff exactly as before this
          gate existed, so the change is behavior-preserving off Linux and a
          strict improvement on the gateway's Linux deploy target.
        - Subtree-aggregate movement. A busy *unrelated* descendant (e.g. an
          MCP child polling) can read WORKING even if the model turn itself is
          wedged with a lost completion frame, extending that turn to the
          ``_DEFAULT_PROMPT_TIMEOUT`` backstop rather than reaping at 90s. This
          is an inherent property of the shared ``LivenessOracle`` (the kiro
          path has it too); tighter per-branch attribution belongs in
          ``liveness.py``, shared by both callers, not here.

        A timed-out await does not stop its executor thread. The one-outstanding-
        walk bound, the refused-submission-reads-UNKNOWN contract, and exception
        retrieval all live in the shared :func:`consult_offloaded` guard.
        ``_prompt_loop`` retires the tracked future at turn start under
        ``_turn_lock``, so a walk abandoned by one turn never gates the next (at
        the cost of one abandoned worker per turn, versus one per silent read
        before this guard existed).
        """
        return await consult_offloaded(
            self,
            self._liveness_oracle.check_model_wait,
            (getattr(self, "_pid", None),),
            executor_factory=subprocess_executor,
            log_label="liveness consult",
        )

    # ── Public API ──

    async def send_message(self, message: str, timeout: float | None = None) -> str:
        """Send a prompt and return the full response text."""
        timeout = await _effective_prompt_timeout_async(timeout)
        self._cancelled = False
        self._turn_done.clear()
        await self.ensure_ready()

        req_id = await self._send_prompt(message)
        return await self._read_prompt_response(req_id, timeout)

    async def send_message_stream(
        self, message: str, timeout: float | None = None
    ) -> AsyncIterator[str]:
        """Send a prompt and yield text chunks as they arrive."""
        timeout = await _effective_prompt_timeout_async(timeout)
        # NOTE: PreToolUse/PostToolUse hooks are intentionally NOT fired on this
        # streaming path today. No audit_source (worker-pool) consumer uses
        # send_message_stream — hook instrumentation lives on the _read_prompt_response
        # path (_maybe_fire_pre_tool_hooks / _maybe_fire_post_tool_hooks). If a future
        # streaming subagent adopts this method, mirror that Pre/Post instrumentation here.
        self._cancelled = False
        self._turn_done.clear()
        await self.ensure_ready()

        req_id = await self._send_prompt(message)
        self.last_prompt_stats = self.last_prompt_stats.carry_over()

        # aclosing(): _prompt_loop holds _turn_lock and releases it in its
        # finally. Consumers below `return` on "complete" without exhausting the
        # loop, which leaves the async-generator SUSPENDED — and CPython
        # finalizes async-gens via a *deferred* scheduled athrow, not at the
        # return point, so the lock would stay held past the turn (next _bg
        # caller blocks = the freeze). aclosing() runs aclose() deterministically
        # on block exit, firing the finally and releasing the lock immediately.
        async with aclosing(self._prompt_loop(req_id, timeout)) as _loop:
            async for action, msg in _loop:
                if action == "complete":
                    reason = ""
                    result = msg.result or {}
                    if isinstance(result, dict):
                        reason = result.get("stopReason", "") or ""
                    self._track_prompt_usage(result)
                    # Close out an automatic claude compaction. The returned
                    # event is discarded — this API yields str — but the context
                    # counts it drops are what the meter reads next turn.
                    self._settle_claude_compaction(reason)
                    self._settle_codex_compaction(reason)
                    reason, _ = self.last_prompt_stats.terminal_refusal(reason)
                    self._last_stop_reason = reason
                    self._turn_done.set()
                    return
                if action == "error":
                    if error_is_refusal_terminal(msg.error, self.last_prompt_stats.refusal):
                        # The refusal's own -32603 terminal (see
                        # _dispatch_events). This API yields str, so the fold
                        # lands on _last_stop_reason and the turn ends cleanly
                        # rather than raising a deterministic decline.
                        reason, _ = self.last_prompt_stats.terminal_refusal("")
                        self._last_stop_reason = reason
                        self._turn_done.set()
                        return
                    _raise_acp_error(msg.error, self._advertised_model_ids(), backend=self.backend)
                if action == "permission":
                    await self._handle_permission(msg)
                elif action == "server_request_unknown":
                    await self._reject_unknown_server_request(msg)
                elif action == "update":
                    self._track_usage_update(msg)
                    self._note_background_launch(msg)
                    # Apply the codex compaction state change; this API yields
                    # str so the event has nowhere to go, but the context counts
                    # it drops are what the meter reads next turn.
                    self._codex_compaction_event(msg)
                    # The gate tripwire holds on every reader that answers
                    # permission frames, this text-only one included.
                    await self._tripwire_pi_gate(msg)
                    await self._tripwire_goose_mode(msg)
                    chunk, is_thinking = self._extract_text_chunk(msg)
                    if chunk and not is_thinking:
                        # A claude compaction notice is a control frame wearing
                        # assistant text. This API yields str, so the event has
                        # nowhere to go — but the state change still applies and
                        # the chunk is still forwarded. Recognizing a notice is a
                        # guess about bare prose, so dropping it here would let
                        # one wrong guess erase a real answer with nothing left
                        # to recover it from.
                        self._claude_compaction_event(chunk)
                        self.last_prompt_stats.text_chunks += 1
                        yield chunk
                        if _is_tool_interrupted_marker(chunk):
                            self._emit_tool_interrupted_sel("send_message_stream")
                            # send_message_stream yields only text chunks (str),
                            # not AcpEvent objects. Tool-result events are a
                            # different shape and cannot be yielded here; callers
                            # of this API do not consume them. Unlike
                            # _dispatch_events (which yields AcpEvent and must
                            # drain tool results before EVENT_COMPLETE), we just
                            # return — no further text will arrive from kiro-cli.
                            return
                    # On a session with a spec deny set, the full extractor rather
                    # than the stats-only tracker: this loop answers permission
                    # requests through _handle_permission, and the refusal there
                    # identifies a call ONLY from the provenance the extractor caches
                    # (raw params by toolCallId) -- stats alone would leave every
                    # request unidentified, and the auto-approve site then refuses
                    # them all rather than run a switched-off tool. The same extractor
                    # writes the classification cache the identity-drift refusals read,
                    # so a harness in ACP_BACKENDS_META_IDENTITY runs it too. On a
                    # session that judges nothing, the tracker's byte-identical
                    # behaviour on every other backend is kept.
                    if self._judges_permission_requests:
                        self._extract_tool_event(msg)
                    else:
                        self._track_tool_call(msg)
                elif action == "metadata":
                    self._track_metadata(msg)
                elif action == "compaction":
                    self._handle_compaction_status(msg)

        # Loop ended without "complete" — timeout or process death.
        self._last_stop_reason = ""
        self._turn_done.set()

    async def stream_events(
        self,
        message: str,
        timeout: float | None = None,
        *,
        allow_image: bool = True,
    ) -> AsyncIterator[AcpEvent]:
        """Send a prompt and yield AcpEvent objects (text, tool_call, permission, complete).

        ``allow_image=False`` sends *message* as text only: no path in it is
        read or inlined.
        """
        timeout = await _effective_prompt_timeout_async(timeout)
        self._cancelled = False
        self._turn_done.clear()
        await self.ensure_ready()
        req_id = await self._send_prompt(message, allow_image=allow_image)
        # aclosing(): a consumer that closes this stream early must close the
        # dispatcher too, so its finally records the turn's unanswered steers
        # as orphans before the next _send_prompt settles them.
        async with aclosing(self._dispatch_prompt_events(req_id, timeout)) as events:
            async for event in events:
                yield event

    async def _dispatch_events(
        self,
        req_id: int,
        timeout: float,
        *,
        extract_agent_from_result: bool = False,
    ) -> AsyncGenerator[AcpEvent, None]:
        """Shared event dispatch loop for prompts and commands."""
        self.last_prompt_stats = self.last_prompt_stats.carry_over()
        self._tool_call_inputs.clear()
        self._tool_call_input_redacted.clear()
        self._tool_call_is_shell.clear()
        self._tool_call_unclassified.clear()
        self._skill_reads.clear()
        self._tool_call_mcp_server.clear()
        self._tool_call_tool_name.clear()
        self._tool_call_harness_tool_name.clear()
        self._tool_call_identity_unreadable.clear()
        self._tool_call_params.clear()
        self._tool_call_diff_path.clear()
        # Reset the per-turn observed-tool-call bookkeeping (see __init__).
        self._observed_tool_calls.clear()
        # Clear stale permission options so an aborted/cancelled request from
        # a prior turn cannot leak into this one (memory + correctness).
        self._permission_options.clear()
        getattr(self, "_permission_gate_events", {}).clear()
        self._stale_eligible = False
        self._tool_dispatched = False
        if self._active_tool_calls.evicted_count:
            # The previous turn's overflow, counted and said once (the same
            # sentence the session handle says for its map).
            logger.warning(
                "tool-call open set evicted %d id(s) at its bound of %d last turn; the "
                "tool-stall clock stayed armed for that turn",
                self._active_tool_calls.evicted_count,
                MAX_ACTIVE_TOOL_CALLS,
            )
        self._active_tool_calls.clear()
        got_complete = False
        saw_agent_switch = False
        # A prompt turn takes ``_session/steering`` steers; a command turn does
        # not. Steers accepted into an earlier turn were settled or dropped there.
        self._steers_accepted = []
        self._steering_turn_req = None if extract_agent_from_result else req_id

        async for action, msg in self._prompt_loop(req_id, timeout):
            if action != "update":
                logger.debug(
                    "ACP event: method=%s id=%s action=%s",
                    _loggable_request_id(msg.method),
                    _loggable_request_id(msg.id),
                    action,
                )

            # Reset staleness only on events that indicate active work.
            # Passive updates (usage_update, tool_call_update after completion,
            # available_commands) must NOT reset it — they can arrive after the
            # final text chunk when kiro-cli has finished but hasn't sent the
            # complete response yet.
            if action != "update":
                self._stale_eligible = False

            if action == "complete":
                got_complete = True
                result = msg.result or {}
                reason = ""
                if isinstance(result, dict):
                    reason = result.get("stopReason", "") or ""
                self._track_prompt_usage(result)
                if extract_agent_from_result and isinstance(result, dict):
                    # commands/execute returns output in result fields,
                    # not via session/update chunks — yield as text.
                    text = format_command_result(result)
                    if text:
                        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=text)
                    if not saw_agent_switch:
                        data = result.get("data", {})
                        if isinstance(data, dict) and data.get("agent"):
                            agent_info = data["agent"]
                            name = (
                                agent_info.get("name", "") if isinstance(agent_info, dict) else ""
                            )
                            if name:
                                yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=name)
                # Flush any remaining tool results before completing
                for tr_event in await self._read_new_tool_results():
                    yield tr_event
                # An automatic claude compaction never sends its own terminal —
                # close it out here, BEFORE EVENT_COMPLETE, so a consumer that
                # reads the terminal to leave its compacting state sees it
                # inside the turn rather than after the turn it belongs to.
                _compaction_settle = self._settle_claude_compaction(reason)
                if _compaction_settle is not None:
                    yield _compaction_settle
                # The codex reading of the same hole: a compaction that errored
                # sent no terminal, so the turn ending is where it is closed out.
                _codex_settle = self._settle_codex_compaction(reason)
                if _codex_settle is not None:
                    yield _codex_settle
                reason, _refusal = self.last_prompt_stats.terminal_refusal(reason)
                # Turn is over — disarm the stall watchdog.
                self._tool_dispatched = False
                self._last_stop_reason = reason
                _consumed = self._release_accepted_steers(req_id, reason, _refusal)
                self._turn_done.set()
                for _injected in _consumed:
                    yield AcpEvent(kind=EVENT_STEER_CONSUMED, text=_injected)
                yield AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=reason,
                    refusal=_refusal,
                    usage=self.last_prompt_stats.to_turn_usage(),
                )
                return
            if action == "error":
                if error_is_refusal_terminal(msg.error, self.last_prompt_stats.refusal):
                    # See AcpSessionHandle: a content-filter refusal can
                    # terminate as a bare -32603. The reason is already on the
                    # stats; surface it as the refusal terminal, never raise.
                    #
                    # Flush pending tool results first, exactly as the
                    # `complete` branch does: a tool that finished just before
                    # the filtered inference would otherwise have its result
                    # dropped, or emitted into the NEXT turn.
                    reason, _refusal = self.last_prompt_stats.terminal_refusal("")
                    for tr_event in await self._read_new_tool_results():
                        yield tr_event
                    self._tool_dispatched = False
                    self._last_stop_reason = reason
                    self._turn_done.set()
                    yield AcpEvent(
                        kind=EVENT_COMPLETE,
                        stop_reason=reason,
                        refusal=_refusal,
                        usage=self.last_prompt_stats.to_turn_usage(),
                    )
                    return
                _raise_acp_error(msg.error, self._advertised_model_ids(), backend=self.backend)
            if action == "permission":
                permission_event = self._build_permission_event(msg)
                if permission_event is None:
                    if msg.id is not None:
                        await self._send_error(msg.id, -32600, "invalid request id")
                    continue
                # Three refusals before the consumer's gate sees the request: a
                # zero-tool spec, a switched-off tool, and a harness identity that is
                # absent or names an unmounted server. The third matters HERE as much
                # as on the auto-approve site -- a frame nothing classified reaches
                # the consumer as a non-shell tool, so the command-deny tier never
                # sees the command it carries.
                if not (
                    await self._deny_zero_tools(permission_event)
                    or await self._deny_spec_disabled_tool(permission_event)
                    or await self._refuse_identity_drift(permission_event)
                ):
                    # The loop is parked here until the approval is answered, and
                    # nothing reads stdout meanwhile, so no steer is sent then.
                    self._parked_on_permission = True
                    try:
                        yield permission_event
                    finally:
                        self._parked_on_permission = False
            elif action == "server_request_unknown":
                await self._reject_unknown_server_request(msg)
            elif action == "steer_answer" and self._steer_turn is not None:
                # Answered before the turn's own terminal: kept for the settle.
                self._steer_turn.answers[msg.id] = (msg, False)
            elif action == "update":
                self._track_usage_update(msg)
                self._note_background_launch(msg)
                _steer_echo = self._steer_echo_event(msg)
                if _steer_echo is not None:
                    yield _steer_echo
                # codex reports compaction as a marked tool_call pair rather than
                # as text, so it is read off the FRAME here instead of off a
                # chunk below. Yielded and then fallen through: the frame is
                # still a tool call this turn made.
                _codex_compaction = self._codex_compaction_event(msg)
                if _codex_compaction is not None:
                    yield _codex_compaction
                chunk, is_thinking = self._extract_text_chunk(msg)
                _notice_chunk = False
                if chunk and not is_thinking:
                    # The claude backend reports compaction as plain assistant
                    # text. Emit the compaction status event every consumer
                    # already handles, then fall through and yield the chunk too:
                    # the event is a side effect, not a replacement for the text.
                    # The chunk carries ``control_notice`` so a consumer can show
                    # it without counting it as the turn's answer — the ACP layer
                    # owns the classification, and nothing downstream re-parses.
                    _compaction_event = self._claude_compaction_event(chunk)
                    if _compaction_event is not None:
                        yield _compaction_event
                        _notice_chunk = True
                if chunk:
                    # Before yielding text, check for tool results from JSONL
                    for tr_event in await self._read_new_tool_results():
                        yield tr_event
                    kind = EVENT_THINKING_CHUNK if is_thinking else EVENT_TEXT_CHUNK
                    if not is_thinking:
                        self.last_prompt_stats.text_chunks += 1
                        self._stale_eligible = not self._active_tool_calls.possibly_open
                        self._prompt_or_tool_seen = True
                    yield AcpEvent(
                        kind=kind,
                        text=chunk,
                        control_notice=_notice_chunk,
                        message_id=parse_message_id((msg.params or {}).get("update")),
                    )
                    if not is_thinking and _is_tool_interrupted_marker(chunk):
                        # kiro-cli's built-in security filter cancelled the turn's tools.
                        # It will not send a ``complete`` response — synthesize one so the
                        # caller exits instead of waiting out the prompt timeout.
                        # (_emit_tool_interrupted_sel logs + audits the cancellation.)
                        self._emit_tool_interrupted_sel("_dispatch_events")
                        got_complete = True
                        for tr_event in await self._read_new_tool_results():
                            yield tr_event
                        yield AcpEvent(
                            kind=EVENT_COMPLETE,
                            usage=self.last_prompt_stats.to_turn_usage(),
                        )
                        return
                tool_event = self._extract_tool_event(msg)
                if tool_event:
                    self._stale_eligible = False
                    self._prompt_or_tool_seen = True
                    # Arm the tool-stall watchdog: if no further data arrives
                    # within _TOOL_STALL_TIMEOUT, _prompt_loop treats the turn
                    # as dead instead of hanging to the full prompt timeout.
                    self._active_tool_calls.add(tool_event.tool_call_id or "")
                    self._tool_dispatched = True
                    # Record every observed tool_call so PostToolUse can recover
                    # tool_name from _observed_tool_calls (see
                    # _maybe_fire_post_tool_hooks).
                    if tool_event.tool_call_id:
                        self._observed_tool_calls[tool_event.tool_call_id] = (
                            tool_event.title or "unknown",
                            tool_event.tool_kind or "",
                        )
                    # Check for results from previous tool before yielding new tool_call
                    for tr_event in await self._read_new_tool_results():
                        yield tr_event
                    # ACP-layer tool audit for clients with no external audit
                    # loop (e.g. app worker pools). No-op unless audit_source is set.
                    await self._maybe_audit_tool_call(tool_event)
                    # Co-located with the SEL audit Pre-side: fire the PreToolUse
                    # HOOK ENGINE so app/worker-pool subagents reach hook parity
                    # with the main agent / SubagentManager. PostToolUse fires
                    # separately on the tool_result branch below (fire_tool_hooks
                    # is Pre-only). No-op unless audit_source is set.
                    await self._skill_reads.note(tool_event)
                    await self._maybe_fire_pre_tool_hooks(tool_event)
                    yield tool_event
                # Real-time tool result from `tool_call_update` session updates.
                # kiro-cli emits these the moment a tool completes — fires before
                # the JSONL flush, so the inline pill gets its output the instant
                # the tool finishes instead of waiting for the next tool call or
                # message end.  See `_extract_tool_call_update` for the dual-path
                # (content blocks vs rawOutput) details.
                await self._tripwire_pi_gate(msg)
                await self._tripwire_goose_mode(msg)
                tool_result_event = self._extract_tool_call_update(msg)
                if tool_result_event:
                    if tool_result_event.tool_status in TERMINAL_TOOL_STATUSES:
                        self._active_tool_calls.discard(tool_result_event.tool_call_id or "")
                        self._tool_dispatched = self._active_tool_calls.possibly_open
                        self._stale_eligible = not self._active_tool_calls.possibly_open
                    # Fire the PostToolUse HOOK ENGINE now that the tool RESULT
                    # (and its output) exists — the Pre-vs-Post split is required
                    # because fire_tool_hooks above is PreToolUse-only. No-op
                    # unless audit_source is set.
                    self._skill_reads.credit(tool_result_event)
                    await self._maybe_fire_post_tool_hooks(tool_result_event)
                    self._tripwire_spec_disabled_tool(tool_result_event)
                    yield tool_result_event
                # claude-agent-acp emits a separate `tool_call_update` carrying
                # the refined title / kind / rawInput once `chunk.input` finishes
                # streaming (the initial `tool_call` arrives with empty input and
                # generic title like "Terminal"/"grep").  Yield as a refinement
                # event so the dashboard pill + persisted message can be
                # patched in place — see `EVENT_TOOL_CALL_UPDATE` in chat_runner.
                tool_refine_event = self._extract_tool_call_refinement(msg)
                if tool_refine_event:
                    await self._skill_reads.note(tool_refine_event)
                    yield tool_refine_event
            elif action == "metadata":
                self._track_metadata(msg)
            elif action == "compaction":
                self._handle_compaction_status(msg)
                params = msg.params or {}
                status = params.get("status", {})
                status_type = status.get("type", "") if isinstance(status, dict) else str(status)
                summary = params.get("summary", "")
                if status_type == "failed":
                    # The notice reads AcpEvent.title, and `summary` is empty on
                    # failure — carry the notification's own reason so the row
                    # stops collapsing to "unknown error".
                    summary = compaction_failure_detail(params)
                yield AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=summary)
            elif action == "clear":
                yield AcpEvent(kind=EVENT_CLEAR_STATUS)
            elif action == "subagent_list":
                params = msg.params or {}
                _subs = params.get("subagents")
                logger.debug(
                    "ACP subagent_list received: %s entries",
                    len(_subs) if isinstance(_subs, list) else "n/a",
                )
                if isinstance(_subs, list):
                    # A roster means children exist: a spawned child can mutate
                    # state before its first activity frame is observed, so the
                    # roster itself closes the registration-throttle window.
                    if _subs:
                        self._prompt_or_tool_seen = True
                    # No runtime_global marking here: AcpClient owns a dedicated
                    # process with a single session, so an ownerless frame from
                    # it is this session's own roster, never a co-tenant's.
                    yield AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=_subs)
            elif action == "subagent_activity":
                # _kiro.dev/session/update: a sub-agent session's own update,
                # tagged with its sessionId. Carries either:
                # - tool_call_chunk (inner tool starting) with toolCallId/title
                # - agent_message_chunk with text (sub-agent's streamed output)
                params = msg.params or {}
                _ssid = str(params.get("sessionId") or "")
                _upd_raw = params.get("update")
                _upd = _upd_raw if isinstance(_upd_raw, dict) else {}
                _su_kind = str(_upd.get("sessionUpdate") or "")
                _tcid = str(_upd.get("toolCallId") or "")
                # Prefer the nested ``content.text`` shape kiro-cli 2.10.0 emits
                # (via the shared chunk extractor); fall back to the flat
                # top-level ``text`` for older payloads. Fall back on a falsy
                # chunk (None OR empty string) so an empty nested content.text
                # does not shadow a populated flat ``text``.
                _su_chunk, _su_thinking = self._extract_text_chunk(msg)
                _su_text = str(_su_chunk or (_upd.get("text") or ""))
                # A frame naming THIS session is this turn's own stream, not a
                # child's. kiro-cli sends the extension spelling for the parent's
                # own tool-call chunk as well as for a child's update, and the
                # sessionId is what separates them; treating the parent's as a
                # child would put a sub-agent card on the session the user is
                # already looking at.
                if _ssid and _ssid == (self._session_id or ""):
                    continue
                if _ssid and _tcid:
                    # A child's tool call is this process's side effect for
                    # replay purposes — the parent prompt spawned it, so a
                    # replay would re-run it. Close the registration-throttle
                    # window, exactly as the shared-runtime handle does for its
                    # fanned-out child tool calls.
                    self._prompt_or_tool_seen = True
                    # Sub-agent output is LLM-influenced — redact the title before
                    # it reaches the dashboard/persisted message.
                    _su_title, _ = redact_exfiltration_urls(str(_upd.get("title") or ""))
                    _su_title, _ = redact_credentials(_su_title)
                    yield AcpEvent(
                        kind=EVENT_SUBAGENT_ACTIVITY,
                        sub_session_id=_ssid,
                        tool_call_id=_tcid,
                        title=_su_title,
                    )
                elif _ssid and _su_text and _su_kind == "agent_message_chunk" and not _su_thinking:
                    # Sub-agent's streamed text output — the real content; redact
                    # exfil URLs + credentials the sub-agent may have emitted.
                    # Skip reasoning/thinking blocks (is_thinking) — those are the
                    # sub-agent's internal reasoning, not user-visible output, and
                    # the flat pre-port read never surfaced them.
                    # Observed child output also closes the registration-throttle
                    # window: a child whose tool frame was lost or differently
                    # spelled must not read as "did nothing".
                    self._prompt_or_tool_seen = True
                    _su_text, _ = redact_exfiltration_urls(_su_text)
                    _su_text, _ = redact_credentials(_su_text)
                    yield AcpEvent(
                        kind=EVENT_SUBAGENT_ACTIVITY,
                        sub_session_id=_ssid,
                        text=_su_text,
                    )
            elif action == "agent_switched":
                saw_agent_switch = True
                params = msg.params or {}
                agent_name = params.get("agentName", "")
                self._note_agent_switched(params if isinstance(params, dict) else None)
                yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=agent_name)
            elif action == "mcp_oauth_request":
                params = msg.params or {}
                server_name = str(params.get("serverName") or params.get("name") or "")
                oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
                # Reject unsafe-scheme URLs *before* recording dedupe so a later
                # safe retry for the same server still gets through.
                if not _is_safe_oauth_url(oauth_url):
                    if oauth_url:
                        logger.warning(
                            "ACP: refusing unsafe mid-session MCP OAuth URL for %s",
                            server_name or "(unknown)",
                        )
                    continue
                # Without a server_name we can't correlate this banner with the
                # later server_initialized/server_init_failure notification (the
                # discard path keys on server_name only).
                if not server_name:
                    logger.warning(
                        "ACP: dropping mid-session MCP OAuth request with empty serverName"
                    )
                    continue
                if server_name in self._oauth_emitted_servers:
                    logger.debug(
                        "ACP: dropping duplicate mid-session MCP OAuth request for %s",
                        server_name,
                    )
                    continue
                self._oauth_emitted_servers.add(server_name)
                logger.info("ACP: MCP OAuth request mid-session for %s", server_name)
                yield AcpEvent(
                    kind=EVENT_MCP_OAUTH_REQUEST,
                    server_name=server_name,
                    oauth_url=oauth_url,
                )
            elif action == "mcp_server_initialized":
                params = msg.params or {}
                server_name = str(params.get("serverName") or params.get("name") or "")
                if server_name:
                    logger.info("ACP: MCP server initialized: %s", server_name)
                    # Allow re-emission of oauth_request if this server's token expires later.
                    self._oauth_emitted_servers.discard(server_name)
                    yield AcpEvent(
                        kind=EVENT_MCP_SERVER_INITIALIZED,
                        server_name=server_name,
                    )
            elif action == "mcp_server_init_failure":
                params = msg.params or {}
                server_name = str(params.get("serverName") or params.get("name") or "")
                err = str(params.get("error") or "")
                if server_name:
                    logger.warning("ACP: MCP server init failure: %s — %s", server_name, err)
                    # The current banner is in a closed (failed) state — clear
                    # the dedupe entry so kiro-cli's next oauth_request retry
                    # for this server surfaces a new banner instead of being
                    # silently dropped.
                    self._oauth_emitted_servers.discard(server_name)
                    yield AcpEvent(
                        kind=EVENT_MCP_SERVER_INIT_FAILURE,
                        server_name=server_name,
                        text=err,
                    )

        if not got_complete:
            self._last_stop_reason = ""
            self._turn_done.set()
            if self._compaction_failed_turn:
                # Compaction failed and the turn was abandoned by the backend.
                # Terminate explicitly — checked BEFORE the stale-turn branch so
                # a turn that had streamed text does not report a normal
                # end_turn, and before AcpTimeoutError so callers get the real
                # cause. The user-facing notice is already appended by the
                # compaction-status path; this only ends the turn.
                self._compaction_failed_turn = False
                self._last_stop_reason = STOP_REASON_COMPACTION_FAILED
                yield AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=STOP_REASON_COMPACTION_FAILED,
                    usage=self.last_prompt_stats.to_turn_usage(),
                )
                return
            # If text was streamed, this is a stale turn (kiro-cli finished
            # but never sent `result`).  Yield a synthetic complete so callers
            # finalize normally instead of showing a timeout error.
            if self._stale_eligible:
                logger.info(
                    "Synthesizing EVENT_COMPLETE after stale turn (chunks=%d)",
                    self.last_prompt_stats.text_chunks,
                )
                yield AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=STOP_REASON_END_TURN,
                    synthetic_completion=True,
                    usage=self.last_prompt_stats.to_turn_usage(),
                )
                return
            raise AcpTimeoutError()

    # Control an ``AcpClient`` turn from outside it: ``kiro_crew.acp.client_turn_control``.
    command_result = _client_turn_control.command_result
    send_command = _client_turn_control.send_command
    stream_command = _client_turn_control.stream_command
    cancel_session = _client_turn_control.cancel_session
    _audit_cancelled_approvals = _client_turn_control._audit_cancelled_approvals
    steer = _client_turn_control.steer
    _steering_request_ready = _client_turn_control._steering_request_ready
    _steering_waiters = _client_turn_control._steering_waiters
    _settle_steering_answer = _client_turn_control._settle_steering_answer
    _steer_via_steering_request = _client_turn_control._steer_via_steering_request
    _await_steering_answer = _client_turn_control._await_steering_answer
    _release_accepted_steers = _client_turn_control._release_accepted_steers
    _steer_via_request = _client_turn_control._steer_via_request
    _steer_via_concurrent_prompt = _client_turn_control._steer_via_concurrent_prompt
    _dispatch_prompt_events = _client_turn_control._dispatch_prompt_events
    _close_steer_turn = _client_turn_control._close_steer_turn
    _add_orphan_steers = _client_turn_control._add_orphan_steers
    _settle_concurrent_steers = _client_turn_control._settle_concurrent_steers
    _read_steer_answers = _client_turn_control._read_steer_answers
    _write_steer_cancel = _client_turn_control._write_steer_cancel
    _settle_orphan_steers = _client_turn_control._settle_orphan_steers
    _note_active_run = _client_turn_control._note_active_run
    _settle_steer_answer = _client_turn_control._settle_steer_answer
    _steer_echo_event = _client_turn_control._steer_echo_event
    turn_finished_cleanly = _client_turn_control.turn_finished_cleanly
    wait_turn_done = _client_turn_control.wait_turn_done
    _note_background_launch = _client_turn_control._note_background_launch
    background_launch = _client_turn_control.background_launch
    has_active_turn = _client_turn_control.has_active_turn
    has_unfinished_turn = _client_turn_control.has_unfinished_turn

    # The prompt request whose read loop takes ``_session/steering`` steers right
    # now, or None: set when a prompt turn's dispatch starts, cleared when its
    # loop ends or its terminal is handled.
    _steering_turn_req: int | None = None
    # Whether the adapter advertised ``_meta.steering.supported`` at ``initialize``.
    _steering_advertised: bool = False
    # True while the turn's dispatch is parked on a permission event its consumer
    # has not answered: the answer to a steer sent then would not be read until
    # the approval is, past the steer's own wait.
    _parked_on_permission: bool = False
    # (turn request id, ``<user_message>``-wrapped text) for each steer accepted
    # into the running turn and not yet released by its terminal.
    _steers_accepted: list[tuple[int, str]]

    # Monotonic stamp of the last steer handed to the backend, 0.0 when never
    # steered. Mirrors AcpSessionHandle.last_steer_monotonic — the dashboard's
    # keepalive route reads whichever of the two backs the live session.
    _last_steer_monotonic: float = 0.0

    @property
    def last_steer_monotonic(self) -> float:
        """Monotonic time of the last steer written to the backend (0.0 if none)."""
        return self._last_steer_monotonic

    @property
    def supports_steer(self) -> bool:
        """True when the backend takes a user's mid-turn message.

        Membership in ``ACP_BACKENDS_STEER`` (kiro-cli's ``_session/steer``), in
        ``ACP_BACKENDS_STEERING_REQUEST``, or in ``ACP_BACKENDS_STEER_CONCURRENT_PROMPT``
        (a second ``session/prompt``), harness-parity H6, so a harness added later
        does not inherit any of them from ``not _is_claude``. A member of
        ``ACP_BACKENDS_STEERING_REQUEST`` takes its own request where this client
        speaks one (``_CLIENT_STEERING_REQUEST_METHODS``: goose); otherwise it also
        needs its adapter to have advertised ``_session/steering`` at the handshake
        and to be at or above its floor, and before the handshake it reads False.
        """
        if (
            self.backend in ACP_BACKENDS_STEER
            or self.backend in ACP_BACKENDS_STEER_CONCURRENT_PROMPT
            or self._speaks_steering_request
        ):
            return True
        return self.backend in ACP_BACKENDS_STEERING_REQUEST and self._steering_request_ready()

    @property
    def supports_refusal_steer(self) -> bool:
        """True when a deny notice steered into the refused turn reaches the model.

        Only ``ACP_BACKENDS_STEER``: a steering-request member's steer is reported
        consumed only at a clean end (``_session/steering``) or on its echo
        (goose), so it is not a deny-notice channel, and a harness that takes a
        steer as a second ``session/prompt`` ends its turn on the denial the
        notice is about.
        """
        return self.backend in ACP_BACKENDS_STEER

    @property
    def steer_needs_loss_recovery(self) -> bool:
        """True when an accepted steer can still be dropped with its turn.

        A ``_session/steering`` steer (``ACP_BACKENDS_STEERING_REQUEST``) and a
        concurrent-prompt steer (``ACP_BACKENDS_STEER_CONCURRENT_PROMPT``) are
        reported consumed only when their turn ends cleanly, so a cancelled or
        failed turn leaves them for the caller to queue. goose discards a steer
        still queued when its run ends, so its accepted steer is reported
        consumed only on goose's echo, and the caller requeues it otherwise.
        Only the dashboard composer keeps such an entry; kiro-cli's
        ``_session/steer`` keeps every steer delivered into the turn.
        """
        return (
            self.backend in ACP_BACKENDS_STEERING_REQUEST
            or self.backend in ACP_BACKENDS_STEER_CONCURRENT_PROMPT
        )

    @property
    def _speaks_steering_request(self) -> bool:
        """Whether this client speaks the member's own steer request
        (``_CLIENT_STEERING_REQUEST_METHODS``) rather than ``_session/steering``."""
        return (
            self.backend in ACP_BACKENDS_STEERING_REQUEST
            and self.backend in _CLIENT_STEERING_REQUEST_METHODS
        )

    # ── Private Helpers ──

    async def _send_prompt(self, message: str, *, allow_image: bool = True) -> int:
        await self._settle_orphan_steers()
        # Shared with AcpSessionHandle.prompt via prompt_blocks so the two paths
        # cannot drift.
        return await self._send_request(
            METHOD_PROMPT,
            {
                "sessionId": self._session_id,
                # Offloaded: see the note in session_handle.prompt -- image
                # reads and base64 encoding must not block the event loop.
                "prompt": await asyncio.to_thread(
                    build_prompt_blocks, message, allow_image=allow_image
                ),
            },
        )

    async def _read_prompt_response(self, req_id: int, timeout: float) -> str:
        output: list[str] = []
        self.last_prompt_stats = self.last_prompt_stats.carry_over()

        async for action, msg in self._prompt_loop(req_id, timeout):
            if action == "complete":
                reason = ""
                result = msg.result or {}
                if isinstance(result, dict):
                    reason = result.get("stopReason", "") or ""
                self._track_prompt_usage(result)
                # See send_message_stream: settle for the context counts, drop
                # the event this API cannot yield.
                self._settle_claude_compaction(reason)
                self._settle_codex_compaction(reason)
                # Fold a metadata refusal onto the terminal, as the streaming
                # paths do, so a caller reading ``last_stop_reason`` sees the
                # refusal and does not retry a deterministic decline.
                reason, _ = self.last_prompt_stats.terminal_refusal(reason)
                self._last_stop_reason = reason
                self._turn_done.set()
                return "".join(output)
            if action == "error":
                if error_is_refusal_terminal(msg.error, self.last_prompt_stats.refusal):
                    # A content-filter refusal terminating as a bare -32603 (see
                    # _dispatch_events). This API returns the turn's text, so
                    # return what streamed (the canned explanation) under the
                    # refusal stop reason rather than raising: an AcpError here
                    # would discard the reason, feed the retry ladder a
                    # deterministic decline, and retire a healthy worker.
                    reason, _ = self.last_prompt_stats.terminal_refusal("")
                    self._last_stop_reason = reason
                    self._turn_done.set()
                    return "".join(output)
                _raise_acp_error(msg.error, self._advertised_model_ids(), backend=self.backend)
            if action == "permission":
                await self._handle_permission(msg)
            elif action == "server_request_unknown":
                await self._reject_unknown_server_request(msg)
            elif action == "update":
                self._track_usage_update(msg)
                self._note_background_launch(msg)
                # See send_message_stream: settle the codex compaction for the
                # context counts, drop the event this API cannot return.
                self._codex_compaction_event(msg)
                chunk, is_thinking = self._extract_text_chunk(msg)
                if chunk and not is_thinking:
                    # Apply the claude compaction state change, then KEEP the
                    # chunk. This path returns one string callers treat as the
                    # agent's answer, and a caller that wants the notice out of
                    # that string subtracts it with
                    # strip_claude_compaction_notices; dropping it here would
                    # silently truncate a real answer on a misclassification.
                    self._claude_compaction_event(chunk)
                    output.append(chunk)
                    self.last_prompt_stats.text_chunks += 1
                    if _is_tool_interrupted_marker(chunk):
                        self._emit_tool_interrupted_sel("_read_prompt_response")
                        return "".join(output)  # see _dispatch_events for rationale
                self._track_tool_call(msg)
                # Mirror _dispatch_events for the send_message (worker-pool)
                # dispatch path: send_message drives tools through here, not
                # through the stream _dispatch_events, so without this
                # app/worker-pool subagents never reached hook/SEL parity. All
                # gating stays inside the _maybe_* methods (self._audit_source),
                # so main-chat send_message callers (audit_source=None) are no-op.
                tool_event = self._extract_tool_event(msg)
                if tool_event:
                    # Record every observed tool_call so PostToolUse can recover
                    # tool_name from _observed_tool_calls (see _maybe_fire_post_tool_hooks).
                    if tool_event.tool_call_id:
                        self._observed_tool_calls[tool_event.tool_call_id] = (
                            tool_event.title or "unknown",
                            tool_event.tool_kind or "",
                        )
                    await self._maybe_audit_tool_call(tool_event)
                    await self._skill_reads.note(tool_event)
                    await self._maybe_fire_pre_tool_hooks(tool_event)
                await self._tripwire_pi_gate(msg)
                await self._tripwire_goose_mode(msg)
                tool_result_event = self._extract_tool_call_update(msg)
                if tool_result_event:
                    self._skill_reads.credit(tool_result_event)
                    await self._maybe_fire_post_tool_hooks(tool_result_event)
                    self._tripwire_spec_disabled_tool(tool_result_event)
            elif action == "metadata":
                self._track_metadata(msg)
            elif action == "compaction":
                self._handle_compaction_status(msg)

        self._last_stop_reason = ""
        self._turn_done.set()
        raise AcpTimeoutError(partial_output="".join(output))

    # Turn an ``AcpClient`` session's tool-call frames into events, and audit them: ``kiro_crew.acp.client_tool_events``.
    _extract_text_chunk = _client_tool_events._extract_text_chunk
    _maybe_audit_tool_call = _client_tool_events._maybe_audit_tool_call
    _maybe_fire_pre_tool_hooks = _client_tool_events._maybe_fire_pre_tool_hooks
    _maybe_fire_post_tool_hooks = _client_tool_events._maybe_fire_post_tool_hooks
    _emit_tool_interrupted_sel = _client_tool_events._emit_tool_interrupted_sel
    _track_tool_call = _client_tool_events._track_tool_call
    _extract_tool_event = _client_tool_events._extract_tool_event
    _extract_tool_call_update = _client_tool_events._extract_tool_call_update
    _extract_tool_call_refinement = _client_tool_events._extract_tool_call_refinement
    _read_new_tool_results = _client_tool_events._read_new_tool_results
    _read_new_tool_results_sync = _client_tool_events._read_new_tool_results_sync
    _jsonl_tool_results = staticmethod(_client_tool_events._jsonl_tool_results)
    # Fold an ``AcpClient`` session's usage, metadata and compaction frames into its turn stats: ``kiro_crew.acp.client_usage``.
    _track_usage_update = _client_usage._track_usage_update
    _track_prompt_usage = _client_usage._track_prompt_usage
    _backfill_context_window = _client_usage._backfill_context_window
    _track_metadata = _client_usage._track_metadata
    _handle_compaction_status = _client_usage._handle_compaction_status
    _claude_compaction_event = _client_usage._claude_compaction_event
    _codex_compaction_event = _client_usage._codex_compaction_event
    _settle_codex_compaction = _client_usage._settle_codex_compaction
    _settle_claude_compaction = _client_usage._settle_claude_compaction
    wait_for_compaction = _client_usage.wait_for_compaction
    _drain_post_compaction_metadata = _client_usage._drain_post_compaction_metadata


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
