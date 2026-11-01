"""The Slack gateway stays one namespace while its owners live in ``gateway_runtime``.

``kiro_crew.slack.gateway`` keeps its import path and its patch surface; the modules of
``kiro_crew.slack.gateway_runtime`` hold the rest, one responsibility each, and
``_facade_compose.compose`` runs every function they define on the facade's
globals. These tests pin what that composition promises:

* each owner function runs on the facade's globals, and every global it reads
  resolves there, so a patch of ``kiro_crew.slack.gateway.<name>`` reaches it;
* each moved name keeps the kind and signature it had in the one-module file,
  and is ONE object whichever module a caller reads it from;
* a function's ``__module__`` / ``__qualname__`` still name the facade and the
  orchestrator, and an owner's classes keep their own module;
* nothing but the facade imports an owner, and no owner imports another at
  runtime, so the facade is the only edge;
* the guards that enumerate constructs by path keep seeing every construct they
  enumerate, because each stays in exactly the files its guards read.
"""

from __future__ import annotations

import ast
import builtins
import dis
import importlib
import importlib.util
import inspect
import pkgutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

import kiro_crew.slack.gateway as gateway
from kiro_crew._facade_compose import compose as facade_compose
from kiro_crew.slack import gateway_runtime
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE = gateway.__name__
_SRC = Path(gateway.__file__).resolve().parents[2]
_RUNTIME_DIR = Path(gateway_runtime.__file__).resolve().parent
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"})


def _owners() -> list[types.ModuleType]:
    """Every owner module, imported through the package the facade composes."""
    return [
        importlib.import_module(f"{gateway_runtime.__name__}.{info.name}")
        for info in pkgutil.iter_modules([str(_RUNTIME_DIR)])
    ]


def _owner_functions() -> list[tuple[str, types.FunctionType]]:
    """``(label, function)`` for every function an owner's file defines at top level
    or as a member of a class the owner defines."""
    found: list[tuple[str, types.FunctionType]] = []
    for owner in _owners():
        for name, value in vars(owner).items():
            members = [(name, value)]
            if isinstance(value, type) and value.__module__ == owner.__name__:
                members = [(f"{name}.{k}", v) for k, v in vars(value).items()]
            for label, member in members:
                fn = getattr(member, "__func__", member)
                if isinstance(member, property):
                    fn = member.fget
                if isinstance(fn, types.FunctionType) and fn.__code__.co_filename == owner.__file__:
                    found.append((f"{owner.__name__.rsplit('.', 1)[-1]}.{label}", fn))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and its nested code objects read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in _GLOBAL_OPS:
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _owner_trees() -> list[tuple[str, ast.Module]]:
    return [
        (path.name, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(_RUNTIME_DIR.glob("*.py"))
        if path.name != "__init__.py"
    ]


#: Each moved name, the owner that holds it, and its kind and signature in the
#: one-module gateway (captured from it before the split).
_BASE_SURFACE: dict[str, tuple[tuple[str, str, str], ...]] = {
    "admission": (
        ("_start_subagent_dispatch_after_memory_ready", "async method", "(self) -> 'None'"),
        ("_register_child_liveness", "method", "(self) -> 'None'"),
        (
            "_start_adaptive_controller",
            "method",
            "(self, cfg: 'KiroCrewConfig | None' = None) -> 'None'",
        ),
        ("_wire_overload_health", "method", "(self, controller: 'AdaptiveController') -> 'None'"),
        ("_subagent_dependency_coordinator", "method", "(self) -> 'Any'"),
        ("_ensure_subagent_coordinator", "async method", "(self) -> 'None'"),
        ("_unwire_overload_health", "method", "(self) -> 'None'"),
        ("_wire_runner_admission", "method", "(self) -> 'None'"),
        (
            "_subscribe_runner_admission",
            "staticmethod",
            "(coordinator: 'Any', admission: 'Any') -> 'None'",
        ),
        ("_attach_runner_admission_consumers", "method", "(self, admission: 'Any') -> 'None'"),
        ("_runner_admission_store_ready", "async method", "(self) -> 'None'"),
        ("_unwire_runner_admission", "method", "(self) -> 'None'"),
        ("_start_dashboard_workers_after_memory_ready", "method", "(self) -> 'None'"),
    ),
    "channel_lifecycle": (
        ("_channel_transport_permitted", "function", "(member: 'str') -> 'bool'"),
        (
            "_push_observe_limits",
            "function",
            "(history: 'ChannelHistory', max_entries: 'int', ttl_secs: 'int') -> 'None'",
        ),
        ("_connect_slack", "async method", "(self) -> 'bool'"),
        ("_on_channel_config_change", "async method", "(self, change: 'ConfigChange') -> 'None'"),
        (
            "_restart_changed_channels",
            "async method",
            "(self, to_restart: 'list[tuple[str, frozenset[str]]]') -> 'None'",
        ),
        ("channel_restarts_settled", "async method", "(self) -> 'None'"),
        (
            "restart_channel",
            "async method",
            "(self, channel_type: 'str', *, cfg: 'KiroCrewConfig | None' = None) -> 'object | None'",
        ),
        (
            "_forget_superseded_start",
            "method",
            "(self, channel_type: 'str', client: 'object') -> 'None'",
        ),
        ("_publish_teams_webhook", "method", "(self, handles: 'dict') -> 'None'"),
        ("_close_channel_locked", "async method", "(self, channel_type: 'str') -> 'None'"),
        ("_on_slack_config_change", "async method", "(self, change: 'ConfigChange') -> 'None'"),
        ("_on_default_agent_config_change", "method", "(self, change: 'ConfigChange') -> 'None'"),
        (
            "_adopt_channel_sections_from_watcher",
            "async method",
            "(self, boot: \"'tuple[ChannelDescriptor, ...]'\") -> 'dict[str, str]'",
        ),
        (
            "_adopt_channel_section_from_watcher",
            "method",
            "(self, desc: \"'ChannelDescriptor'\", creds: 'dict[str, str]') -> 'None'",
        ),
        ("_schedule_inbound_replay", "method", "(self) -> 'None'"),
        (
            "_replay_spooled_inbound",
            "async method",
            "(self, *, spool: 'Path | None' = None) -> 'None'",
        ),
        (
            "_badge_unready_channels",
            "method",
            "(self, bootable: \"'tuple[ChannelDescriptor, ...]'\") -> 'None'",
        ),
    ),
    "cron_dispatch": (
        ("_CRON_TRANSIENT_RETRIES", "value", "'int'"),
        ("_CRON_POSTTOKEN_CONTINUE_MSG", "value", "'str'"),
        ("_defer_cron_before_dispatch", "function", "(job: 'CronJob', reason: 'str') -> 'None'"),
        (
            "_await_cron_fire_time_gate",
            "async function",
            "(job: 'CronJob', *, tool_name: 'str', tool_kind: 'str') -> 'tuple[str | None, bool]'",
        ),
        ("_CRON_RESERVED_ENV_KEYS", "value", "'frozenset'"),
        (
            "cron_job_env_without_reserved",
            "function",
            "(job_env: 'dict[str, str] | None') -> 'dict[str, str]'",
        ),
        (
            "_pre_create_cron_slot",
            "async function",
            "(dashboard_state: \"'DashboardState'\", job: 'CronJob') -> 'None'",
        ),
        ("CronClaimTimeDenied", "class", ""),
        ("CronClaimAbandoned", "class", ""),
        ("_ClaimHandoff", "class", ""),
        ("CronVetOverran", "class", ""),
        ("claim_vet_bound", "function", "(job: 'CronJob') -> 'float'"),
        ("_claim_backstop", "function", "(job: 'CronJob', subprocess_bound: 'int') -> 'float'"),
        (
            "_vet_at_claim_then",
            "function",
            "(handoff: '_ClaimHandoff', job: 'CronJob', fn: 'Callable[..., Any]', *args: 'Any') -> 'Any'",
        ),
        ("_annotate_model_fallback", "function", "(text: 'str', provider: 'Any') -> 'str'"),
        (
            "_cron_stream_with_posttoken_resume",
            "async function",
            "(client: 'Any', message: 'str', *, job_name: 'str', **stream_kwargs: 'Any') -> 'tuple[str, float | None]'",
        ),
    ),
    "cron_verdict": (
        ("_VOLATILE_RE", "value", "'Pattern'"),
        ("_EPOCH_RE", "value", "'Pattern'"),
        ("_EPOCH_WINDOW_SECS", "value", "'int'"),
        ("_SUCCESS_REMINDER_SECS", "value", "'int'"),
        ("_FAILURE_REMINDER_SECS", "value", "'int'"),
        ("_CRON_FAILURE_DETAIL_CAP", "value", "'int'"),
        ("_NO_RESPONSE", "value", "'str'"),
        ("_GateTally", "class", ""),
        (
            "_annotate_partial_block",
            "function",
            "(result_text: 'str', tally: '_GateTally') -> 'str'",
        ),
        ("_result_hash", "function", "(text: 'str') -> 'str'"),
    ),
    "delivery": (
        (
            "_open_dm_with_retry",
            "async method",
            "(self, user_id: 'str', job_name: 'str', max_attempts: 'int' = 3) -> 'str | None'",
        ),
        ("_record_cron_delivery", "method", "(self, job: 'CronJob', result_hash: 'str') -> 'None'"),
        (
            "_remember_options",
            "method",
            "(self, session_key: 'str', channel: 'str', ts: 'str', choices: 'list[str]', blocks: 'list[dict]', text: 'str') -> 'None'",
        ),
        (
            "_channel_reply_link",
            "method",
            "(self, parent_key: 'str') -> 'tuple[ChannelLink, bool] | None'",
        ),
        ("_cron_origin_key", "method", "(self, parent_key: 'str') -> 'str'"),
        (
            "_deliver_cron_to_channel",
            "async method",
            "(self, origin_key: 'str', text: 'str', *, actor_key: 'str') -> 'bool'",
        ),
        ("_cron_job_is_silent", "method", "(self, parent_key: 'str') -> 'bool'"),
    ),
    "mcp_broker": (
        ("_init_mcp_discovery", "method", "(self) -> 'None'"),
        (
            "_schedule_mcp_launch_approval_persist",
            "method",
            "(self, approvals: 'LaunchApprovals') -> 'None'",
        ),
        (
            "_init_mcp_gateway",
            "async method",
            "(self, stub_servers: 'frozenset[str] | None' = None) -> 'None'",
        ),
        ("_mcp_resolve_refresh_secs", "method", "(self) -> 'float'"),
        (
            "_mcp_resolve_prefetch_loop",
            "async method",
            "(self, target_env: 'dict[str, str]') -> 'None'",
        ),
        (
            "_prefetch_mcp_resolutions",
            "async method",
            "(self, target_env: 'dict[str, str]', *, force: 'bool' = False) -> 'dict[str, str]'",
        ),
        ("_refresh_mcp_resolutions", "async method", "(self) -> 'dict'"),
        ("_stop_mcp_broker", "async method", "(self) -> 'None'"),
        (
            "_refresh_mcp_overlays",
            "async method",
            "(self, register: 'Callable[[], Awaitable[bool]] | None' = None) -> 'dict'",
        ),
        ("_apply_mcp_gateway_enabled", "async method", "(self, enabled: 'bool') -> 'dict'"),
        ("_apply_mcp_stub", "async method", "(self) -> 'dict'"),
        ("_wire_mcp_gateway_dashboard", "method", "(self) -> 'None'"),
    ),
    "memory_lifecycle": (
        ("_initialize_memory_worker", "method", "(self) -> 'bool'"),
        ("_stop_memory_startup", "method", "(self) -> 'None'"),
        ("_start_memory_after_ready", "method", "(self) -> 'None'"),
        ("_schedule_memory_preparation", "method", "(self) -> \"'asyncio.Task[None] | None'\""),
        ("_wait_for_memory_preparation", "async method", "(self) -> 'bool'"),
        ("_repair_member_memory_once", "method", "(self) -> 'None'"),
        ("_repair_member_memory", "async method", "(self) -> 'None'"),
        ("_start_embeddings", "async method", "(self) -> 'None'"),
        ("_auto_migrate_memory", "async method", "(self) -> 'None'"),
        ("_set_memory_migrated", "async method", "(self, value: 'bool') -> 'None'"),
    ),
    "tool_policy": (
        ("_BACKGROUND_APPROVAL_SOURCES", "value", "'frozenset'"),
        ("_READ_ONLY_TOOL_PREFIXES", "value", "'tuple'"),
        ("_WRITE_INDICATORS", "value", "'tuple'"),
        ("_is_read_only_tool", "function", "(event_title: 'str') -> 'bool'"),
        ("_HEARTBEAT_STATUS_PREFIXES", "value", "'tuple'"),
        ("_is_heartbeat_safe_tool", "function", "(event: 'LLMEvent') -> 'bool'"),
        ("_build_heartbeat_hooks", "function", "(user_hooks: 'HookManager') -> 'HookManager'"),
        ("_bare_tool_name", "function", "(title: 'str') -> 'str'"),
    ),
    "install_repair": (
        ("_KIRO_CLI_RESOLVE_TIMEOUT_SECS", "value", "'float'"),
        ("_pinned_kiro_cli", "async function", "(purpose: 'str') -> 'str | None'"),
        (
            "_kill_startup_child",
            "async staticmethod",
            "(proc: \"'asyncio.subprocess.Process'\") -> 'None'",
        ),
        (
            "_reap_startup_child",
            "async classmethod",
            "(cls, proc: \"'asyncio.subprocess.Process'\") -> 'None'",
        ),
        ("_check_missing_deps", "async method", "(self) -> 'None'"),
        ("_check_console_script", "async method", "(self) -> 'None'"),
        ("_schedule_console_script_repair", "method", "(self) -> 'asyncio.Task[None]'"),
        ("_warn_if_kiro_cli_outdated", "async method", "(self) -> 'None'"),
    ),
    "update_schedule": (
        ("_UPDATE_STOP_GRACE_SECS", "value", "'float'"),
        ("_in_flight_work_counts", "method", "(self) -> 'tuple[int, int]'"),
        ("_count_in_flight_work", "method", "(self) -> 'int'"),
        ("_live_update_handler_tasks", "method", "(self) -> 'list[asyncio.Task[Any]]'"),
        (
            "_drain_update_callback_work",
            "async method",
            "(self, *, timeout: 'float | None') -> 'bool'",
        ),
        (
            "_prepare_auto_update_apply",
            "async method",
            "(self, *, mandatory: 'bool', mandatory_key: 'str' = '') -> 'bool'",
        ),
        ("_finish_auto_update_apply", "async method", "(self) -> 'None'"),
        ("_wheel_apply_module", "staticmethod", "() -> 'Any'"),
        ("_dashboard_policy_applies", "staticmethod", "() -> \"list['asyncio.Task[Any]']\""),
        (
            "_settle_update_work",
            "async method",
            "(self, update_task: \"'asyncio.Task[None] | None'\") -> 'None'",
        ),
        ("_run_update_checks", "async method", "(self) -> 'None'"),
        ("_retry_pending_update_restart", "async method", "(self) -> 'bool'"),
        ("_check_for_updates", "async method", "(self) -> 'None'"),
        (
            "_auto_update_effect",
            "async method",
            "(self, provider: 'object | None') -> 'AutoUpdateEffect'",
        ),
        ("_publish_provider_update_state", "method", "(self, result: 'object') -> 'None'"),
        ("_check_for_updates_via_provider", "async method", "(self, provider: 'object') -> 'None'"),
        (
            "_restart_after_update_claimed",
            "async method",
            "(self, launcher: 'str | None', exe: 'str | None') -> 'None'",
        ),
        ("_check_for_updates_legacy", "async method", "(self) -> 'None'"),
        (
            "_restart_after_update_body",
            "async method",
            "(self, respawn: 'Callable[[], str]') -> 'None'",
        ),
    ),
    "update_apply": (
        ("_auto_apply_update", "async method", "(self, *, mandatory: 'bool' = False) -> 'None'"),
        (
            "_auto_apply_wheel_update",
            "async method",
            "(self, channel: 'str', version: 'str', *, mandatory: 'bool' = False, mandatory_key: 'str' = '') -> 'None'",
        ),
        (
            "_notice_wheel_update_once",
            "method",
            "(self, key: 'str', title: 'str', body: 'str') -> 'None'",
        ),
    ),
    "nudge_wiring": (
        ("_init_autonudge", "async method", "(self) -> 'None'"),
        ("_notify_nudge_held", "method", "(self, loop: 'NudgeLoop') -> 'bool'"),
        ("_notify_nudge_expired", "method", "(self, loop: 'NudgeLoop') -> 'bool'"),
    ),
    "nudge_dashboard_fire": (
        (
            "_fire_dashboard_nudge",
            "async method",
            "(self, loop: 'NudgeLoop', wake_message: 'str | None' = None) -> 'bool | MonitorDispatchResult'",
        ),
        (
            "_work_ledger_wake_loop",
            "method",
            "(self, loop: 'NudgeLoop', wake_message: 'str | None') -> 'bool'",
        ),
        ("_work_ledger_wake_text", "async method", "(self, loop: 'NudgeLoop') -> 'str'"),
        (
            "_steer_work_ledger_wake",
            "async method",
            "(self, loop: 'NudgeLoop', slot: 'Any', text: 'str') -> 'bool'",
        ),
        (
            "_dashboard_mode_admits",
            "async staticmethod",
            "(loop: 'NudgeLoop', slot: 'Any') -> 'bool'",
        ),
        ("_audit_fire_refused", "async staticmethod", "(loop: 'NudgeLoop', slot: 'Any') -> 'None'"),
    ),
    "nudge_channel_fire": (
        ("_NUDGE_TURN_TIMEOUT", "value", "'float'"),
        (
            "_delivery_result",
            "function",
            "(wake_message: 'str | None', result: 'MonitorDispatchResult') -> 'bool | MonitorDispatchResult'",
        ),
        ("_DmDispatchAdapter", "class", ""),
        (
            "_fire_slack_nudge",
            "async method",
            "(self, loop: 'NudgeLoop', wake_message: 'str | None' = None) -> 'bool | MonitorDispatchResult'",
        ),
        (
            "_bookkeeping_section",
            "method",
            "(self) -> 'contextlib.AbstractContextManager[None]'",
        ),
        (
            "_fire_slack_inner_nudge",
            "async method",
            "(self, loop: 'NudgeLoop', wake_message: 'str | None', bookkeeping: 'contextlib.ExitStack') -> 'bool | MonitorDispatchResult'",
        ),
        (
            "_fire_dm_nudge",
            "async method",
            "(self, loop: 'NudgeLoop', adapter: '_DmDispatchAdapter', wake_message: 'str | None' = None) -> 'bool | MonitorDispatchResult'",
        ),
        (
            "_fire_discord_nudge",
            "async method",
            "(self, loop: 'NudgeLoop', wake_message: 'str | None' = None) -> 'bool | MonitorDispatchResult'",
        ),
        ("_fire_webex_nudge", "async method", "(self, loop: 'NudgeLoop') -> 'bool'"),
        (
            "_stop_message_loop_if_structural_terminal",
            "async method",
            "(self, loop: 'NudgeLoop', exc: 'BaseException', wake_message: 'str | None', fired_generation: 'int') -> 'bool'",
        ),
        (
            "_monitor_completion_hook",
            "method",
            "(self, loop: 'NudgeLoop') -> 'MonitorCompletionHook | None'",
        ),
        (
            "_report_monitor_completion",
            "async method",
            "(self, loop: 'NudgeLoop', disposition: 'MonitorActionDisposition', usage: 'AgentTurnUsage | None', *, hook: 'MonitorCompletionHook | None' = None) -> 'None'",
        ),
    ),
    "turn_approvals": (
        ("_dashboard_client_attached", "method", "(self) -> 'bool'"),
        (
            "_interactive_approval",
            "method",
            "(self, source: 'str', slot_resolver: 'Callable[[str], str] | None' = None, nudge_key: 'str' = '', *, raise_when_unreachable: 'bool' = False) -> 'ToolApprovalCallback'",
        ),
        (
            "_heartbeat_approval",
            "async method",
            "(self, event: 'LLMEvent', _parent_session_key: 'str' = '') -> 'bool'",
        ),
    ),
    "service_boot": (
        ("_auto_open_dashboard", "async method", "(self, dashboard_url: 'str') -> 'None'"),
        ("_init_services", "async method", "(self) -> 'None'"),
        (
            "_notify_consolidation_abandoned",
            "method",
            "(self, key: 'str', message_count: 'int', reason: 'str', char_count: 'int' = 0, last_slice: 'bool' = False, retired: 'bool' = False) -> 'None'",
        ),
        ("_schedule_session_start_sizing", "method", "(self) -> 'None'"),
        ("_log_session_start_sizing", "async method", "(self) -> 'None'"),
        ("_init_task_runner", "method", "(self) -> 'None'"),
        ("_init_dashboard", "async method", "(self) -> 'None'"),
        ("_init_api_server", "async method", "(self) -> 'None'"),
        ("run", "async method", "(self) -> 'None'"),
        ("_PROFILE_PROJECTION_POLL_S", "value", "'float'"),
    ),
    "heartbeat_turns": (
        ("_init_heartbeat", "async method", "(self) -> 'None'"),
        (
            "_persist_turn_row",
            "async function",
            "(client: 'Any', session_key: 'str', *, provider: 'str', surface: 'str', agent_fallback: 'Callable[[], str]', t0: 'float', usage: 'AgentTurnUsage | None' = None) -> 'None'",
        ),
        ("_HEARTBEAT_KEEP_INJECTION", "value", "'str'"),
    ),
    "result_egress": (
        (
            "_deliver_cron_response",
            "async method",
            "(self, parent_key: 'str', text: 'str', *, silent: 'bool' = False) -> 'bool'",
        ),
        (
            "_deliver_channel_reply",
            "async method",
            "(self, parent_key: 'str', text: 'str', *, resolved_link: 'tuple[ChannelLink, bool] | None' = None, caller: 'str' = 'subagent') -> 'bool'",
        ),
        ("_cron_outbound_thread", "method", "(self, parent_key: 'str') -> 'str | None'"),
        ("_persist_slot_title", "async method", "(self, slot: \"'_ChatSlot'\") -> 'None'"),
        ("_resolve_loaded_slot", "async method", "(self, slot_name: 'str') -> 'Any'"),
        (
            "_deliver_result",
            "async method",
            "(self, title: 'str', task_summary: 'str', result_text: 'str', deliver: 'str') -> 'None'",
        ),
        ("_heartbeat_slack_parts", "function", "(title: 'str', result_text: 'str') -> 'list[str]'"),
    ),
    "exit_path": (
        ("_shutdown", "async method", "(self) -> 'None'"),
        (
            "_stop_services_and_sessions",
            "async method",
            "(self, *, eager_spawns: 'list[asyncio.Task[None]] | None' = None) -> 'None'",
        ),
        ("_write_marker_worker", "method", "(self, run_marker: 'Any', port: 'int') -> 'None'"),
        ("_install_shutdown_signal_handlers", "method", "(self) -> 'None'"),
        (
            "_shutdown_and_exit",
            "async method",
            "(self, watchdog: 'asyncio.Future | None' = None) -> 'None'",
        ),
        ("_MARKER_WRITE_WAIT_SECS", "value", "'float'"),
    ),
    "process_entry": (
        ("_live_session_work_dirs", "function", "(sessions: 'Any') -> 'list[str]'"),
        ("_live_session_agents", "function", "(sessions: 'Any') -> 'set[str]'"),
        (
            "_sweep_predecessor_session_work_dirs",
            "function",
            "(live_work_dirs: 'list[str]') -> 'int'",
        ),
        (
            "_run_signing_key_preflight_bounded",
            "async function",
            "(timeout: 'float | None' = None) -> 'None'",
        ),
        ("signing_key_preflight", "function", "() -> 'None'"),
        ("_start_kas_cli_floor", "function", "(cfg: 'KiroCrewConfig') -> \"'asyncio.Task[None]'\""),
        (
            "run_gateway",
            "async function",
            "(cfg: 'KiroCrewConfig', *, no_dashboard: 'bool' = False, no_crons: 'bool' = False, no_tunnel: 'bool' = False, no_open: 'bool' = False, port_override: 'str | None' = None, json_ready: 'bool' = False, approval_mode: 'str | None' = None, test_mode: 'bool' = False) -> 'None'",
        ),
        ("BOOT_RESTART_HINT", "value", "'str'"),
        ("_SIGNING_KEY_PREFLIGHT_TIMEOUT_SECS", "value", "'float'"),
    ),
    # The cron decomposition: _init_cron and the four failure-alert helpers keep their
    # BASE shapes; the other twelve are the phases cut out of _init_cron's closure.
    "cron_fire": (
        ("_init_cron", "async method", "(self, *, arm: 'bool' = True) -> 'None'"),
        ("_cron_capture", "async method", "(self, job: 'CronJob') -> '_CronCapture'"),
        ("_record_bound_script_failure", "async method", "(self, job: 'CronJob') -> 'None'"),
        (
            "_alert_cron_timeout",
            "async method",
            "(self, job: 'CronJob', detail: 'str') -> 'None'",
        ),
        (
            "_resolve_cron_agent",
            "method",
            "(self, cron_execution: 'ExecutionContext', alias: 'str | None', *, template_namespace: 'bool' = False, sequence_step: 'bool' = False) -> \"'tuple[str | None, str | None, str | None]'\"",
        ),
        (
            "_acquire_with_model_fallback",
            "async method",
            "(self, job: 'CronJob', cron_execution: 'ExecutionContext', key: 'str', agent_id: 'str | None', cwd: 'str | None' = None, crew_agent: 'str | None' = None, *, execution: 'ExecutionContext | None' = None, replace_execution: 'bool' = False) -> \"'tuple[LLMProvider, bool, bool, bool]'\"",
        ),
        (
            "_retained_session_agent_mismatch",
            "method",
            "(self, job: 'CronJob', key: 'str', agent: 'str | None') -> 'str | None'",
        ),
        ("_announce_cron_quarantine", "method", "(self) -> 'None'"),
        ("_start_cron_after_memory_ready", "async method", "(self) -> 'None'"),
    ),
    "cron_exec": (
        ("_cron_run_command", "async method", "(self, job: 'CronJob') -> 'str | None'"),
        (
            "_deliver_script_result",
            "async method",
            "(self, job: 'CronJob', message: 'str', *, remove: 'bool' = False) -> 'None'",
        ),
        ("_cron_run_script", "async method", "(self, job: 'CronJob') -> 'str | None'"),
        ("_live_internal_secret", "method", "(self) -> 'str'"),
    ),
    "cron_turn": (
        (
            "_cron_run_sequence",
            "async method",
            "(self, job: 'CronJob', cap: '_CronCapture') -> 'str | None'",
        ),
        (
            "_cron_run_single",
            "async method",
            "(self, job: 'CronJob', cap: '_CronCapture', *, retry: 'Callable[[CronJob], Awaitable[str | None]]') -> 'str | None'",
        ),
    ),
    "cron_delivery": (
        (
            "_deliver_cron_agent_result",
            "async method",
            "(self, job: 'CronJob', session_key: 'str', client: 'LLMProvider', result_text: 'str', rh: 'str', _ctx_reading: 'Any', _turn_stats: 'dict[str, Any] | None') -> 'str'",
        ),
        ("_CRON_MSG_LIMIT", "value", "'int'"),
    ),
    "cron_failure": (
        (
            "_failure_alert_due",
            "function",
            "(job: 'CronJob', streak: 'int', *, denied: 'bool' = False) -> 'bool'",
        ),
        ("_log_held_alert", "function", "(job: 'CronJob') -> 'None'"),
        (
            "_failure_alert_is_duplicate",
            "method",
            "(self, job: 'CronJob', failure_hash: 'str') -> 'bool'",
        ),
        ("_slack_safe_fenced", "method", "(self, text: 'str') -> 'str'"),
        (
            "_deliver_failure_alert",
            "async method",
            "(self, job: 'CronJob', *, mrkdwn: 'str', plain: 'str', actor_key: 'str', silent: 'bool' = False) -> 'tuple[bool, bool, bool]'",
        ),
        (
            "_advance_failure_dedup",
            "method",
            "(self, job: 'CronJob', failure_hash: 'str', *, channel_delivered: 'bool', slack_failed: 'bool') -> 'None'",
        ),
        (
            "_alert_cron_failure",
            "async method",
            "(self, job: 'CronJob', detail: 'str', *, denied: 'bool' = False) -> 'None'",
        ),
        (
            "_cron_single_failure",
            "async method",
            "(self, job: 'CronJob', exc: 'Exception', session_key: 'str', _gate_counted: 'bool', _job_owns_failure: 'bool', _hand_over_streak: 'bool') -> 'None'",
        ),
    ),
    "orchestrator_init": (
        (
            "__init__",
            "method",
            "(self, cfg: 'KiroCrewConfig', *, no_dashboard: 'bool' = False, no_crons: 'bool' = False, no_open: 'bool' = False, port_override: 'str | None' = None, json_ready: 'bool' = False, approval_mode: 'str | None' = None, test_mode: 'bool' = False, credentials: 'Mapping[str, str] | None' = None) -> 'None'",
        ),
    ),
    "subagent_wiring": (
        (
            "_subagent_work_pending",
            "async function",
            "(manager: 'Any', parent_session_key: 'str') -> 'bool'",
        ),
        (
            "_subagent_queued_count",
            "async function",
            "(manager: 'Any', parent_session_key: 'str') -> 'int'",
        ),
        (
            "_subagent_batch_pending",
            "async function",
            "(manager: 'Any', batch_id: 'str') -> 'bool'",
        ),
        ("_WORKFLOW_STEP_PREFIXES", "value", "'tuple'"),
        ("_spawn_parent_slot", "function", "(parent_key: 'str', workflow_service: 'Any') -> 'str'"),
        ("_subagent_coalescer", "method", "(self) -> \"'SubagentEventCoalescer'\""),
        ("_init_subagents", "method", "(self) -> 'None'"),
    ),
    "subagent_report": (
        ("_digest_chunk_size", "function", "() -> 'int'"),
        ("SUBAGENT_DIGEST_CHUNK_SIZE", "value", "'int'"),
        ("_SubagentReport", "class", ""),
        ("_WaveDigest", "class", ""),
        (
            "_compose_subagent_completion",
            "function",
            "(info: 'SubagentInfo', *, _flush_only: 'bool') -> '_SubagentReport'",
        ),
        (
            "_subagent_wave_digest",
            "method",
            "(self, info: 'SubagentInfo', *, report: '_SubagentReport', _flush_only: 'bool', _batch_id: 'str', _batch_total: 'int', _no_batch_pending: 'bool', _injection_slot_name: 'str') -> '_WaveDigest | None'",
        ),
    ),
    "subagent_injection": (
        ("_MAX_INJECT_ATTEMPTS", "value", "'int'"),
        ("_injection_slot_busy", "function", "(slot: 'Any') -> 'bool'"),
        (
            "_defer_queued_delivery",
            "staticmethod",
            "(slot: 'Any', announce: 'str', info: 'SubagentInfo', *, flush_only: 'bool') -> 'None'",
        ),
        (
            "_route_subagent_to_tab",
            "async method",
            "(self, info: 'SubagentInfo', *, announce: 'str', body: 'str', title: 'str', sub_meta: 'dict[str, Any]', _flush_only: 'bool', _injection_slot: 'Any', _injection_slot_name: 'str', parent_key: 'str') -> 'None'",
        ),
        (
            "_inject_subagent_into_channel_parent",
            "async method",
            "(self, info: 'SubagentInfo', *, announce: 'str', body: 'str', title: 'str', parent_key: 'str') -> 'None'",
        ),
        (
            "_finish_subagent_cron_or_notify",
            "async method",
            "(self, info: 'SubagentInfo', *, announce: 'str', body: 'str', title: 'str', parent_key: 'str') -> 'None'",
        ),
    ),
    "runtime_charge": (
        ("_apply_gate_verdict", "function", "(job: 'CronJob', tally: '_GateTally') -> 'bool'"),
        (
            "_cron_failure_attribution",
            "function",
            "(job: 'CronJob', _run_provider: 'object | None') -> 'tuple[bool, bool]'",
        ),
        ("_charge_failure", "function", "(job: 'CronJob', _hand_over_streak: 'bool') -> 'None'"),
        (
            "_streak_after_charge",
            "function",
            "(job: 'CronJob', charges: 'bool', _hand_over_streak: 'bool') -> 'int'",
        ),
        (
            "_inject_with_retry",
            "async method",
            "(self, info: 'SubagentInfo', client, msg: 'str', parent_key: 'str', label: 'str') -> 'str | None'",
        ),
    ),
    "channel_boot": (
        (
            "_hoist_wecom",
            "method",
            "(self, cfg: 'KiroCrewConfig', creds: 'Mapping[str, str]') -> 'None'",
        ),
        (
            "_hoist_telegram",
            "method",
            "(self, cfg: 'KiroCrewConfig', creds: 'Mapping[str, str]') -> 'None'",
        ),
        (
            "_hoist_weixin",
            "method",
            "(self, cfg: 'KiroCrewConfig', creds: 'Mapping[str, str]') -> 'None'",
        ),
        (
            "_hoist_whatsapp",
            "method",
            "(self, cfg: 'KiroCrewConfig', creds: 'Mapping[str, str]') -> 'None'",
        ),
        (
            "_hoist_feishu",
            "method",
            "(self, cfg: 'KiroCrewConfig', creds: 'Mapping[str, str]') -> 'None'",
        ),
        (
            "_hoist_discord",
            "method",
            "(self, cfg: 'KiroCrewConfig', creds: 'Mapping[str, str]') -> 'None'",
        ),
        (
            "_hoist_webex",
            "method",
            "(self, cfg: 'KiroCrewConfig', creds: 'Mapping[str, str]') -> 'None'",
        ),
        (
            "_hoist_imessage",
            "method",
            "(self, cfg: 'KiroCrewConfig', creds: 'Mapping[str, str]') -> 'None'",
        ),
        (
            "_hoist_teams",
            "method",
            "(self, cfg: 'KiroCrewConfig', creds: 'Mapping[str, str]') -> 'None'",
        ),
        (
            "_on_session_environment_config_change",
            "method",
            "(self, change: 'ConfigChange') -> 'None'",
        ),
        ("_register_config_appliers", "method", "(self) -> 'None'"),
        (
            "_start_channel_transports",
            "async method",
            "(self, descriptors: \"'tuple[ChannelDescriptor, ...] | None'\" = None) -> 'None'",
        ),
    ),
}


# ── one namespace ─────────────────────────────────────────────────────────────


def test_the_owners_define_functions_for_the_sweeps_to_check() -> None:
    """Every sweep below proves nothing unless it has functions to sweep."""
    labels = {label for label, _ in _owner_functions()}
    callables = sum(
        kind.endswith(("function", "method"))
        for rows in _BASE_SURFACE.values()
        for _, kind, _ in rows
    )
    assert len(labels) >= callables
    assert "channel_lifecycle._channel_transport_permitted" in labels
    assert "cron_verdict._GateTally.note" in labels


def test_every_owner_function_runs_on_the_facade_globals() -> None:
    """A patch of ``kiro_crew.slack.gateway.<name>`` reaches an owner function only
    because the function reads the facade's globals, not its own module's."""
    strays = [
        label
        for label, fn in _owner_functions()
        if fn.__globals__ is not vars(gateway) or fn.__module__ != _FACADE
    ]
    assert strays == []


def test_the_sweep_reports_a_global_the_facade_does_not_bind() -> None:
    """The name sweep can fail, nested bodies included."""

    def _probe() -> object:
        def _inner() -> object:
            return _absent_from_the_gateway_namespace  # noqa: F821

        return _inner

    assert "_absent_from_the_gateway_namespace" in set(_global_names(_probe.__code__))


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    """An owner's own imports are inert for its functions, so a name missing from the
    facade surfaces only when its line runs -- often inside an ``except`` that turns
    the NameError into a refusal. The sweep makes it a test failure instead."""
    namespace = vars(gateway)
    unresolved = sorted(
        (label, name)
        for label, fn in _owner_functions()
        for name in set(_global_names(fn.__code__))
        if name not in namespace and not hasattr(builtins, name)
    )
    assert unresolved == []


def test_a_patch_of_the_facade_reaches_an_owner_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract the rebinding exists for, exercised end to end on two owners."""
    denied = types.SimpleNamespace(permitted=False, reason="policy", rule="r", layer="policy")
    calls: list[str] = []

    def _deny(scope: str, member: str, **_kw: object) -> object:
        calls.append(member)
        return denied

    monkeypatch.setattr(gateway, "governance_permits", _deny)
    monkeypatch.setattr(
        gateway, "sel", lambda: types.SimpleNamespace(log_governance_decision=lambda **_: None)
    )
    assert gateway._channel_transport_permitted("telegram") is False
    assert calls == ["telegram"]

    monkeypatch.setattr(gateway, "vet_job_at_fire_time", lambda job: "denied at claim")
    job = types.SimpleNamespace(name="j", id="j1")
    with pytest.raises(gateway.CronClaimTimeDenied, match="denied at claim"):
        gateway._vet_at_claim_then(gateway._ClaimHandoff(), job, lambda: None)


# ── the moved surface ─────────────────────────────────────────────────────────


def _facade_member(name: str) -> tuple[object, str]:
    """``(object, kind)`` for a moved name, read the way callers read it."""
    raw = vars(gateway.GatewayOrchestrator).get(name)
    if raw is not None:
        wrap = type(raw).__name__ if isinstance(raw, (staticmethod, classmethod)) else ""
        fn = raw.__func__ if wrap else raw
        return fn, ("async " if inspect.iscoroutinefunction(fn) else "") + (wrap or "method")
    obj = getattr(gateway, name)
    if inspect.isclass(obj):
        return obj, "class"
    if inspect.isfunction(obj):
        return obj, ("async " if inspect.iscoroutinefunction(obj) else "") + "function"
    return obj, "value"


def _signature(fn: object) -> str:
    """The signature with the first parameter's annotation dropped: an owner spells
    ``self: GatewayOrchestrator`` where the class body said ``self``."""
    sig = inspect.signature(fn)  # type: ignore[arg-type]
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return str(sig.replace(parameters=params))


@pytest.mark.parametrize(
    ("owner", "name", "kind", "signature"),
    [(owner, *row) for owner, rows in _BASE_SURFACE.items() for row in rows],
    ids=[f"{owner}:{row[0]}" for owner, rows in _BASE_SURFACE.items() for row in rows],
)
def test_a_moved_name_keeps_its_base_shape_and_owner(
    owner: str, name: str, kind: str, signature: str
) -> None:
    """Every name that moved keeps the kind and signature it had in the one-module
    gateway, lives in the owner its responsibility names, and is ONE object: the
    facade attribute, the class attribute and the owner's are the same."""
    obj, found_kind = _facade_member(name)
    assert found_kind == kind
    if kind == "value":
        assert type(obj).__name__ == signature.strip("'")
    elif kind != "class":
        assert _signature(obj) == signature
    module = importlib.import_module(f"{gateway_runtime.__name__}.{owner}")
    assert getattr(module, name) is obj


def test_module_and_qualname_still_name_the_facade() -> None:
    """Reprs, pickling by reference and nested functions' names read as they did
    before the split: every owner function resolves back through its own
    ``__module__`` and ``__qualname__``."""
    wrong = []
    for label, fn in _owner_functions():
        target: object = sys.modules[fn.__module__]
        for part in fn.__qualname__.split("."):
            target = (
                vars(target).get(part) if isinstance(target, type) else getattr(target, part, None)
            )
            target = getattr(target, "fget", target)
            target = getattr(target, "__func__", target)
        if target is not fn:
            wrong.append(label)
    assert wrong == []
    nested = gateway.GatewayOrchestrator._schedule_inbound_replay.__code__
    inner = [c.co_qualname for c in nested.co_consts if isinstance(c, types.CodeType)]
    assert inner and all(
        q.startswith("GatewayOrchestrator._schedule_inbound_replay.") for q in inner
    )


def test_a_star_import_carries_the_moved_public_names(tmp_path: Path) -> None:
    """``from kiro_crew.slack.gateway import *`` exports what the one-module file did."""
    probe = tmp_path / "gateway_star_probe.py"
    probe.write_text("from kiro_crew.slack.gateway import *  # noqa: F401,F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("gateway_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in (
        "CronClaimTimeDenied",
        "claim_vet_bound",
        "cron_job_env_without_reserved",
    ):
        assert getattr(module, name) is getattr(gateway, name)


# ── one edge ──────────────────────────────────────────────────────────────────

_RUNTIME_PACKAGE = gateway_runtime.__name__


def _package_of(path: Path) -> str:
    """The package a relative import in a file under ``src/`` resolves against."""
    parts = list(path.resolve().relative_to(_SRC).with_suffix("").parts)
    return ".".join(parts[:-1])


def _import_targets(tree: ast.Module, package: str) -> list[tuple[ast.AST, str]]:
    """``(node, dotted module)`` for every module a tree imports, spelled any way.

    Covers ``import a.b``, ``from a import b`` (which may name module ``a.b``),
    relative ``from . import x`` / ``from ..x import y`` resolved against *package*,
    and a string-literal ``importlib.import_module(...)`` / ``__import__(...)`` call.
    """
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, package)
            found.append((node, base))
            found.extend((node, f"{base}.{alias.name}") for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            name = node.args[0].value
            if name.startswith("."):
                anchor = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(anchor, ast.Constant) and isinstance(anchor.value, str)):
                    continue
                name = importlib.util.resolve_name(name, anchor.value)
            found.append((node, name))
    return found


def _is_owner(target: str) -> bool:
    return target == _RUNTIME_PACKAGE or target.startswith(f"{_RUNTIME_PACKAGE}.")


def _is_facade_or_owner(target: str) -> bool:
    return target == _FACADE or target.startswith(f"{_FACADE}.") or _is_owner(target)


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    return {
        id(stmt)
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for stmt in ast.walk(node)
    }


def _owner_importers(source: str, package: str) -> list[int]:
    """Lines where a module other than the facade imports an owner, in any spelling
    and under ``TYPE_CHECKING`` too (a type-only import still makes an owner part of
    another module's surface)."""
    tree = ast.parse(source)
    return sorted(
        {node.lineno for node, target in _import_targets(tree, package) if _is_owner(target)}
    )


def _owner_runtime_edges(source: str, package: str) -> list[int]:
    """Lines where an owner imports the facade or an owner outside ``TYPE_CHECKING``."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    return sorted(
        {
            node.lineno
            for node, target in _import_targets(tree, package)
            if _is_facade_or_owner(target) and id(node) not in guarded
        }
    )


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from . import channel_lifecycle\n", True),
        ("from .channel_lifecycle import restart_channel\n", True),
        ("from .. import gateway\n", True),
        ("from ..gateway import sel\n", True),
        ("from kiro_crew.slack import gateway\n", True),
        ("from kiro_crew.slack import gateway_runtime\n", True),
        ("import kiro_crew.slack.gateway\n", True),
        ("import kiro_crew.slack.gateway as facade\n", True),
        ("from kiro_crew.slack.gateway import sel\n", True),
        ("def f():\n    from kiro_crew.slack.gateway import sel\n", True),
        ("import importlib\nimportlib.import_module('kiro_crew.slack.gateway')\n", True),
        ("__import__('kiro_crew.slack.gateway_runtime.admission')\n", True),
        (
            "import importlib\nimportlib.import_module('.admission', 'kiro_crew.slack.gateway_runtime')\n",
            True,
        ),
        ("if TYPE_CHECKING:\n    from kiro_crew.slack.gateway import sel\n", False),
        ("if TYPE_CHECKING:\n    from .. import gateway\n", False),
        ("from kiro_crew.slack import handler\n", False),
        ("import kiro_crew.slack.gateway_helpers_elsewhere\n", False),
        ("from kiro_crew.llm_helpers import annotate_model_fallback\n", False),
    ],
)
def test_the_owner_edge_check_sees_every_spelling(source: str, flagged: bool) -> None:
    """The runtime-edge check below is only as good as the spellings it resolves."""
    assert bool(_owner_runtime_edges(source, _RUNTIME_PACKAGE)) is flagged


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from .gateway_runtime import delivery\n", True),
        ("from . import gateway_runtime\n", True),
        ("from .gateway_runtime.delivery import _cron_origin_key\n", True),
        ("import kiro_crew.slack.gateway_runtime.delivery\n", True),
        ("from kiro_crew.slack.gateway_runtime import delivery as d\n", True),
        ("if TYPE_CHECKING:\n    from .gateway_runtime import delivery\n", True),
        (
            "import importlib\nimportlib.import_module('kiro_crew.slack.gateway_runtime.delivery')\n",
            True,
        ),
        ("from . import gateway\n", False),
        ("from .gateway import GatewayOrchestrator\n", False),
        ("from kiro_crew.slack import gateway\n", False),
        ("import importlib\nimportlib.import_module(name)\n", False),
    ],
)
def test_the_importer_check_sees_every_spelling(source: str, flagged: bool) -> None:
    """The one-import-path check below is only as good as the spellings it resolves."""
    assert bool(_owner_importers(source, "kiro_crew.slack")) is flagged


def test_nothing_but_the_facade_imports_an_owner() -> None:
    """The facade is the only import path, so there is one patch surface."""
    importers = []
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        if (
            _RUNTIME_DIR in path.resolve().parents
            or path.resolve() == Path(gateway.__file__).resolve()
            or "_vendor" in path.parts
        ):
            continue
        text = path.read_text(encoding="utf-8")
        if "gateway_runtime" not in text:
            continue
        importers.extend(
            f"{path.relative_to(_SRC)}:{line}" for line in _owner_importers(text, _package_of(path))
        )
    assert importers == []


def test_an_owner_imports_the_facade_and_its_siblings_only_for_type_checking() -> None:
    """No owner imports the facade or another owner at runtime: the facade imports
    the owners and nothing points back, so there is no import cycle to order."""
    offenders = [
        f"{name}:{line}"
        for name, _ in _owner_trees()
        for line in _owner_runtime_edges(
            (_RUNTIME_DIR / name).read_text(encoding="utf-8"), _RUNTIME_PACKAGE
        )
    ]
    assert offenders == []


def test_every_owner_with_a_coroutine_is_in_the_config_dir_guard() -> None:
    """``test_no_config_dir_in_async`` scans the files it lists, so an owner that
    gains an ``async def`` must be listed there, or its coroutines escape the guard."""
    guard = _SRC.parent / "test" / "test_no_config_dir_in_async.py"
    listed: set[str] = set()
    for node in ast.parse(guard.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_ASYNC_CHECKED_FILES" for t in node.targets
        ):
            listed = {elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)}
    assert listed, "_ASYNC_CHECKED_FILES not found; this check went stale"
    with_coroutines = {
        f"slack/gateway_runtime/{name}"
        for name, tree in _owner_trees()
        if any(isinstance(n, ast.AsyncFunctionDef) for n in ast.walk(tree))
    }
    assert with_coroutines, "no owner defines a coroutine; this check went stale"
    assert sorted(with_coroutines - listed) == []


def test_a_second_facade_import_recomposes_the_owners_onto_it(tmp_path: Path) -> None:
    """A test that purges the facade and imports it again gets owners that run on
    the NEW namespace, and the orchestrator's qualnames are not prefixed twice."""
    script = textwrap.dedent("""
        import sys
        import kiro_crew.slack.gateway as first
        del sys.modules["kiro_crew.slack.gateway"]
        import kiro_crew.slack.gateway as second
        from kiro_crew.slack.gateway_runtime import delivery
        fn = second.GatewayOrchestrator._deliver_cron_to_channel
        assert second is not first
        assert fn.__globals__ is vars(second)
        assert delivery._deliver_cron_to_channel is fn
        assert fn.__qualname__ == "GatewayOrchestrator._deliver_cron_to_channel"
        assert fn.__code__.co_qualname == "GatewayOrchestrator._deliver_cron_to_channel"
        assert second._result_hash.__globals__ is vars(second)
        print("ok")
        """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        timeout=120,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def test_a_fresh_facade_import_loads_every_owner(tmp_path: Path) -> None:
    """Importing the facade imports every owner with it: none loads lazily on a
    later call, so the import order stays the one the one-module file had."""
    names = sorted(info.name for info in pkgutil.iter_modules([str(_RUNTIME_DIR)]))
    assert len(names) >= 8, names
    script = textwrap.dedent("""
        import sys
        import kiro_crew.slack.gateway
        missing = [n for n in sys.argv[1:] if f"kiro_crew.slack.gateway_runtime.{n}" not in sys.modules]
        assert missing == [], missing
        print("ok")
        """)
    result = subprocess.run(
        [sys.executable, "-c", script, *names],
        capture_output=True,
        timeout=120,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


# ── the path-keyed guards keep their reach ────────────────────────────────────

#: Constructs repository guards enumerate by path (usage-row persists, runtime-death
#: attribution and streaks, spawn and exit sites, chat-turn dispatches, the nudge
#: composer, the memory-store and transcript seams, the boot-order anchors, the
#: event-log publish), each mapped to the files that hold it -- the facade
#: (``gateway``) or an owner -- which are the files those guards read. A holder that
#: grew one, or lost one, would move it out of such a guard's sight without failing
#: it, so each set is exact; a set changes in the commit that points the token's
#: guards at its new home (``slack-gateway.md``, Composition).
_GUARDED_TOKEN_HOMES: dict[str, frozenset[str]] = {
    "persist_token_record": frozenset({"cron_turn", "gateway", "heartbeat_turns"}),
    ".read_messages": frozenset({"cron_delivery", "nudge_wiring"}),
    "begin_turn": frozenset({"nudge_channel_fire"}),
    "os._exit": frozenset({"exit_path"}),
    "save_conversation_turn_off_loop": frozenset(
        {"gateway", "nudge_channel_fire", "subagent_injection"}
    ),
    "session_store_for_turn": frozenset(
        {"gateway", "heartbeat_turns", "nudge_channel_fire", "subagent_injection"}
    ),
    "create_subprocess_": frozenset({"gateway", "install_repair", "update_apply"}),
    "isolated_python_argv": frozenset({"install_repair", "update_apply"}),
    "respawn_executable": frozenset({"update_apply", "update_schedule"}),
    "reexec_python_module": frozenset({"update_schedule"}),
    "_running_script_ids.add": frozenset({"cron_exec"}),
    "caused_by_this_session": frozenset({"runtime_charge"}),
    "note_shared_death": frozenset({"runtime_charge"}),
    "clear_marker": frozenset({"exit_path"}),
    "init_socket_mode": frozenset({"service_boot"}),
    "build_message": frozenset(
        {"cron_fire", "cron_turn", "heartbeat_turns", "nudge_channel_fire", "subagent_injection"}
    ),
    "VectorMemoryStore": frozenset({"gateway", "service_boot"}),
    "configure_default_ladder": frozenset({"subagent_wiring"}),
    "eventlog_hooks": frozenset({"exit_path", "nudge_wiring", "service_boot"}),
    "autonudge_state": frozenset({"nudge_wiring"}),
    "_run_chat": frozenset(
        {
            "cron_fire",
            "gateway",
            "nudge_dashboard_fire",
            "result_egress",
            "subagent_injection",
        }
    ),
    "spawn_guarded_turn": frozenset({"gateway", "nudge_dashboard_fire"}),
    "compose_nudge_body": frozenset({"gateway", "nudge_channel_fire", "nudge_dashboard_fire"}),
    "clear_shared_deaths": frozenset({"cron_exec", "runtime_charge"}),
    "run_in_cron_pool(": frozenset({"cron_exec"}),
    "consecutive_failures": frozenset({"cron_exec", "cron_failure", "cron_turn", "runtime_charge"}),
    "_delivery_queued": frozenset({"subagent_injection"}),
    "_digest_held": frozenset({"subagent_report"}),
    "_deliver_failure_alert": frozenset({"cron_failure", "gateway"}),
    "render_for_slack(": frozenset(
        {"cron_delivery", "nudge_channel_fire", "result_egress", "subagent_injection"}
    ),
}


def _holder_sources() -> dict[str, str]:
    """The facade's and every owner's source, keyed by stem."""
    return {
        "gateway": Path(gateway.__file__).read_text(encoding="utf-8"),
        **{
            name.removesuffix(".py"): (_RUNTIME_DIR / name).read_text(encoding="utf-8")
            for name, _ in _owner_trees()
        },
    }


@pytest.mark.parametrize("token", sorted(_GUARDED_TOKEN_HOMES))
def test_a_construct_a_guard_reads_stays_where_the_guard_reads_it(token: str) -> None:
    holders = {stem for stem, source in _holder_sources().items() if token in source}
    assert holders == _GUARDED_TOKEN_HOMES[token]


def test_no_owner_reads_a_hoisted_channel_attribute() -> None:
    """``test_channel_boot_hot_reload`` checks every ``_<channel>_*`` attribute read in
    ``slack/gateway.py``, ``orchestrator_init.py`` and ``channel_boot.py`` against that
    channel's hoist; the reads stay there, so no other owner holds one."""
    read_by_the_parity_test = {"orchestrator_init.py", "channel_boot.py"}
    channels = (
        "wecom",
        "telegram",
        "weixin",
        "whatsapp",
        "feishu",
        "discord",
        "webex",
        "imessage",
        "teams",
    )
    offenders = [
        f"{name}:{node.lineno}:{node.attr}"
        for name, tree in _owner_trees()
        if name not in read_by_the_parity_test
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and any(node.attr.startswith(f"_{c}_") for c in channels)
    ]
    assert offenders == []


def test_the_facade_and_its_owners_reach_autonudge_only_through_its_service() -> None:
    """AutoNudge keeps its shutdown-drain invariant: a Slack nudge's post-reply
    bookkeeping opens its drained section with ``AutoNudgeService.drain_section``, and
    neither the facade nor an owner imports an ``autonudge_service`` module."""

    def _names(node: ast.AST) -> list[str]:
        if isinstance(node, ast.ImportFrom):
            return [node.module or ""]
        if isinstance(node, ast.Import):
            return [alias.name for alias in node.names]
        return []

    offenders = [
        f"{stem}:{node.lineno}"
        for stem, source in _holder_sources().items()
        for node in ast.walk(ast.parse(source))
        if any(name.startswith("kiro_crew.autonudge_service") for name in _names(node))
    ]
    assert offenders == []


# ── compose, on its own ───────────────────────────────────────────────────────


def _write_module(tmp_path: Path, name: str, source: str) -> types.ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_rebinds_functions_methods_and_members(tmp_path: Path) -> None:
    """On synthetic modules: a module function, a bound method, a static method and a
    property of an owner class all read the host namespace afterwards, the method and
    its nested function carry the host class's qualname, and the owner class keeps its
    own module (``inspect`` finds a class's source through it)."""
    owner = _write_module(
        tmp_path,
        "compose_owner_probe",
        """
        def helper():
            return VALUE

        def method(self):
            def inner():
                return VALUE
            return inner

        def static():
            return VALUE

        class Tally:
            @property
            def value(self):
                return VALUE
        """,
    )
    namespace = {"__name__": "compose_host_probe", "VALUE": "host"}

    class Host:
        pass

    Host.method = owner.method  # type: ignore[attr-defined]
    Host.static = staticmethod(owner.static)  # type: ignore[attr-defined]
    namespace["helper"] = owner.helper
    facade_compose(namespace, (owner,), host=Host)

    assert namespace["helper"]() == "host" and owner.helper is namespace["helper"]
    assert Host().method()() == "host"  # type: ignore[attr-defined]
    assert Host.static() == "host"  # type: ignore[attr-defined]
    assert owner.Tally().value == "host"
    assert Host.method.__qualname__ == f"{Host.__qualname__}.method"  # type: ignore[attr-defined]
    inner = Host().method()  # type: ignore[attr-defined]
    assert inner.__qualname__ == f"{Host.__qualname__}.method.<locals>.inner"
    assert owner.Tally.__module__ == "compose_owner_probe"
    namespace["VALUE"] = "patched"
    assert namespace["helper"]() == "patched"
