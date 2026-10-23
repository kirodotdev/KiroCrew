"""``AcpSessionHandle`` keeps every member it had, and each is bound from the module that owns it.

The class's behaviour lives in the ``kiro_crew.acp.session_handle_*`` owner modules: each
moved member's body is a module-level function there whose first parameter is ``self``, and
the class binds it by name (``m = _owner.m``, or ``staticmethod(_owner.f)``). The instance
state stays on the instance, so tests, ``AcpSessionHandle.__new__`` builds and the runtime
see the same members they always did. This file pins the binding contract:

* the frozen member inventory -- every member, its kind, whether it is a coroutine or an
  async generator, its signature (the ``self`` annotation aside) and the module that owns it;
* each bound member IS its owner's function, named as a method of the class
  (``__qualname__``), and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade at run time, and an owner imports only owners listed before it;
* each owner's docstring names the instance fields its members write and read;
* owners log under the facade's logger, and the moved exception classes keep the facade
  as their module;
* a write through ``kiro_crew.acp.session_handle`` reaches every owner that reads the name,
  for every name the test trees patch there (the facade's mirror, ``_facade_mirror``), and
  the facade reads no name it serves as a bare global;
* the facade imports nothing beyond its frozen inventory, the check ``setup.cfg``'s F401
  exemption for it stands in for.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from facade_mirror_scan import needs_syntax_tree, patched_names, text_patched_names

from kiro_crew.acp import session_handle
from kiro_crew.acp.session_handle import AcpSessionHandle

pytestmark = pytest.mark.xdist_group(name="tree_scan_session_handle_bindings")

#: Member -> (kind, coroutine, signature digest, owning module). Frozen at the split: the
#: kind, coroutine flag and signature are BASE's; the owner is where the member lives now.
_MEMBERS: dict[str, tuple[str, object, str, str]] = {
    "__init__": ("function", False, "b84baaa24675", "session_handle_class"),
    "_accept_oauth_request": ("function", False, "668a15b0478d", "session_handle_mcp"),
    "_active_tool_calls": ("property", False, "d150141e37db", "session_handle_class"),
    "_advertised_model_ids": ("function", False, "6806e867af4e", "session_handle_model_selection"),
    "_answer_kas_hook_execute": ("function", True, "b922d8462af7", "session_handle_kas"),
    "_answer_kas_hooks_request": ("function", True, "87459833bc5c", "session_handle_kas"),
    "_apply_init_notification": ("function", False, "ac6a24522ec0", "session_handle_mcp"),
    "_apply_kas_context_pct": ("function", False, "386e4643466b", "session_handle_kas"),
    "_apply_kas_turn_completion": ("function", False, "7f31b87afeac", "session_handle_kas"),
    "_audit_elicitation_decision": (
        "function",
        False,
        "fa17e47d8779",
        "session_handle_elicitation",
    ),
    "_audit_handle_reject": ("function", False, "8a9e7399a659", "session_handle_permissions"),
    "_backfill_context_window": ("function", False, "f3e546765020", "session_handle_compaction"),
    "_build_child_tool_activity_prefix": (
        "function",
        False,
        "f3c17ef22460",
        "session_handle_native_children",
    ),
    "_build_permission_event": ("function", False, "e64128661dfa", "session_handle_permissions"),
    "_cancel_hook_tasks": ("function", False, "b60c5fea256e", "session_handle_kas"),
    "_classify": ("function", False, "b92a06811c94", "session_handle_dispatch"),
    "_cleanup_transcript": ("function", False, "b60c5fea256e", "session_handle_lifecycle"),
    "_close_abandoned_turn": ("function", True, "b60c5fea256e", "session_handle_turn"),
    "_codex_compaction_event": ("function", False, "f2eb5a4fbc3e", "session_handle_compaction"),
    "_compacts_inline": ("function", False, "a6632a57e602", "session_handle_compaction"),
    "_consult_oracle_offloaded": ("function", True, "a74b2cf7aa76", "session_handle_watchdog"),
    "_crew_agent_name": ("function", False, "9854751dbafb", "session_handle_model_selection"),
    "_deny_spec_disabled_tool": ("function", True, "2dfacffa6279", "session_handle_permissions"),
    "_died": ("function", False, "687ef4e00210", "session_handle_lifecycle"),
    "_dispatch_events": ("function", "agen", "883a50b3b1fa", "session_handle_dispatch"),
    "_drain_post_compaction_metadata": (
        "function",
        True,
        "0ca5ee4bb87d",
        "session_handle_compaction",
    ),
    "_emit_tool_interrupted_sel": ("function", False, "866fc4f75f7c", "session_handle_updates"),
    "_emit_watchdog_metric": ("function", False, "8cf581201a27", "session_handle_watchdog"),
    "_end_human_wait": ("function", False, "b60c5fea256e", "session_handle_watchdog"),
    "_end_stalled_tool": ("function", "agen", "1fcba2664f9e", "session_handle_watchdog"),
    "_handle_elicitation_request": ("function", True, "e64128661dfa", "session_handle_elicitation"),
    "_handle_kas_session_info": ("function", False, "f3c17ef22460", "session_handle_kas"),
    "_handle_kas_subagent": ("function", False, "5f796c64544c", "session_handle_native_children"),
    "_handle_kas_subagent_chunk": (
        "function",
        False,
        "5f796c64544c",
        "session_handle_native_children",
    ),
    "_handle_kas_update": ("function", False, "761fcd4d2d41", "session_handle_kas"),
    "_handle_update": ("function", False, "74878d3f1402", "session_handle_updates"),
    "_identity_unreadable_cache_for_wire": (
        "function",
        False,
        "931b07357dc9",
        "session_handle_updates",
    ),
    "_inflight_interactive": ("property", False, "55d13eb29157", "session_handle_class"),
    "_inflight_tool": ("property", False, "06d9a24bbca9", "session_handle_class"),
    "_inflight_tool_call_id": ("property", False, "bb418bb7ece8", "session_handle_class"),
    "_inline_turn_finished_cleanly": (
        "function",
        False,
        "a6632a57e602",
        "session_handle_compaction",
    ),
    "_input_wait_status": ("function", False, "7139eff8852c", "session_handle_watchdog"),
    "_is_kas_hooks_request": ("function", False, "50405edf7b6a", "session_handle_kas"),
    "_kas_compact_advertised": ("function", False, "a6632a57e602", "session_handle_compaction"),
    "_kas_compact_request": ("function", False, "b0a837a37097", "session_handle_compaction"),
    "_last_steer_monotonic": ("data", False, "", ""),
    "_log_working_deferral": ("function", False, "835e76052008", "session_handle_watchdog"),
    "_mark_available_models_captured": (
        "function",
        False,
        "b60c5fea256e",
        "session_handle_model_selection",
    ),
    "_no_tool_in_flight": ("function", False, "a6632a57e602", "session_handle_updates"),
    "_normalize_models": ("staticmethod", False, "0587318c8e24", "session_handle_model_selection"),
    "_note_mcp_sign_in_status": ("function", False, "d0d2b0689553", "session_handle_mcp"),
    "_note_native_child": ("function", False, "09a8133eda4c", "session_handle_native_children"),
    "_note_native_roster": ("function", False, "7a20326749ff", "session_handle_native_children"),
    "_note_status_rejected": ("function", False, "a9ff3d5a4447", "session_handle_updates"),
    "_note_steering_denial": ("function", False, "b60c5fea256e", "session_handle_steering"),
    "_offer_mcp_sign_in": ("function", False, "b60c5fea256e", "session_handle_mcp"),
    "_owns_mcp_frame": ("function", False, "50405edf7b6a", "session_handle_mcp"),
    "_push_model_config_option": (
        "function",
        True,
        "e60b4fb0cbfc",
        "session_handle_model_selection",
    ),
    "_reattempt_failed_mcp_servers": ("function", False, "b60c5fea256e", "session_handle_mcp"),
    "_refuse_unidentifiable_mcp_approval": (
        "function",
        True,
        "5e39ccd3f4ad",
        "session_handle_permissions",
    ),
    "_refuse_unreadable_mcp_identity": (
        "function",
        True,
        "2dfacffa6279",
        "session_handle_permissions",
    ),
    "_refuse_unretained_provenance": (
        "function",
        True,
        "5e39ccd3f4ad",
        "session_handle_permissions",
    ),
    "_release_proven_steers": ("function", False, "dbae1777abcc", "session_handle_steering"),
    "_release_settled_tool_call": ("function", False, "2362e63eea68", "session_handle_updates"),
    "_remote_flat_launches": ("function", False, "b809e9b1faeb", "session_handle_mcp"),
    "_report_evicted_attributions": ("function", False, "b60c5fea256e", "session_handle_updates"),
    "_restore_withdrawn": ("function", False, "491a23344d9c", "session_handle_permissions"),
    "_retire_liveness_state": ("function", False, "b60c5fea256e", "session_handle_watchdog"),
    "_run_turn": ("function", "agen", "b55fe99c131a", "session_handle_turn"),
    "_runtime_idle_secs": ("function", False, "59162e39f6f1", "session_handle_watchdog"),
    "_runtime_reattempts_mcp": ("function", False, "a6632a57e602", "session_handle_mcp"),
    "_send_awaited": ("function", True, "99990f4299d3", "session_handle_lifecycle"),
    "_send_hook_error": ("function", True, "38fa0936b024", "session_handle_kas"),
    "_served_default_after_set_mode": (
        "function",
        True,
        "b60c5fea256e",
        "session_handle_model_selection",
    ),
    "_settle_abandoned_steering": ("function", True, "b60c5fea256e", "session_handle_steering"),
    "_settle_codex_compaction": ("function", False, "01ddf3eeedf2", "session_handle_compaction"),
    "_settle_kas_compaction": ("function", False, "d0076098f390", "session_handle_compaction"),
    "_steer_via_steering_request": ("function", True, "e2662ac6ac7a", "session_handle_steering"),
    "_structured_status_event": ("function", False, "f8008b3ce042", "session_handle_updates"),
    "_sync_effort_levels": ("function", False, "b60c5fea256e", "session_handle_model_selection"),
    "_sync_mcp_reattempting": ("function", False, "b60c5fea256e", "session_handle_mcp"),
    "_take_injected_steers": ("function", False, "c42020aec8dd", "session_handle_steering"),
    "_tool_call_cache_overflow_for_wire": (
        "function",
        False,
        "9b594013b706",
        "session_handle_updates",
    ),
    "_tool_kind_cache_for_wire": ("function", False, "b7b9268c0112", "session_handle_updates"),
    "_track_metadata": ("function", False, "87459833bc5c", "session_handle_compaction"),
    "_track_prompt_usage": ("function", False, "c8d5c402278d", "session_handle_compaction"),
    "_tripwire_spec_disabled_tool": (
        "function",
        False,
        "8e767352900b",
        "session_handle_permissions",
    ),
    "_wait_for_response": ("function", True, "87f499e313b4", "session_handle_lifecycle"),
    "_working_tool_bound_reached": ("function", False, "c7051eec1093", "session_handle_watchdog"),
    "agent_version": ("property", False, "bb418bb7ece8", "session_handle_class"),
    "answer_elicitation": ("function", True, "2fcb1ac189fa", "session_handle_elicitation"),
    "apply_session_permission_routing": (
        "function",
        True,
        "b60c5fea256e",
        "session_handle_permissions",
    ),
    "approve_tool": ("function", True, "1241f87a8dc0", "session_handle_permissions"),
    "available_models": ("property", False, "92d6193dbe19", "session_handle_class"),
    "awaiting_permission": ("property", False, "a6632a57e602", "session_handle_class"),
    "bind_session_key": ("function", False, "57eca86d38c3", "session_handle_lifecycle"),
    "cancel": ("function", True, "4b75d9a5e0c7", "session_handle_lifecycle"),
    "cancel_pending_elicitations": ("function", True, "fa6cfecd5574", "session_handle_elicitation"),
    "cleanup_transcript_files": ("staticmethod", False, "a9cece317161", "session_handle_lifecycle"),
    "command_result": ("function", True, "1f650230e18e", "session_handle_lifecycle"),
    "compact": ("function", True, "416a3df991e6", "session_handle_compaction"),
    "config_options": ("property", False, "7eb96c2fda05", "session_handle_class"),
    "destroy": ("function", True, "b60c5fea256e", "session_handle_lifecycle"),
    "discard_unanswered_prompt": ("function", True, "a6632a57e602", "session_handle_kas"),
    "drain_init": ("function", True, "c9912f3fb68d", "session_handle_mcp"),
    "ensure_served_default": ("function", True, "4740fd0e8b58", "session_handle_model_selection"),
    "get_valid_effort_levels": (
        "function",
        False,
        "6806e867af4e",
        "session_handle_model_selection",
    ),
    "has_unfinished_turn": ("property", False, "a6632a57e602", "session_handle_class"),
    "inflight_interactive": ("property", False, "7949c1538a15", "session_handle_class"),
    "is_responsive": ("function", False, "4b2fc7c136b8", "session_handle_watchdog"),
    "is_turn_active": ("property", False, "a6632a57e602", "session_handle_class"),
    "last_steer_monotonic": ("property", False, "59162e39f6f1", "session_handle_class"),
    "maybe_refresh_available_models": (
        "function",
        True,
        "9de63ef05543",
        "session_handle_model_selection",
    ),
    "mcp_session_report": ("function", False, "ac691973b9b4", "session_handle_mcp"),
    "model": ("property", False, "bb418bb7ece8", "session_handle_class"),
    "native_child_overflow": ("property", False, "fa6cfecd5574", "session_handle_class"),
    "native_child_resume_refusal": (
        "function",
        False,
        "61b23cbd0173",
        "session_handle_native_children",
    ),
    "native_child_sessions": ("property", False, "602c14087aa8", "session_handle_class"),
    "note_keepalive": ("function", False, "b60c5fea256e", "session_handle_watchdog"),
    "parked_for_secs": ("function", False, "59162e39f6f1", "session_handle_watchdog"),
    "parked_since": ("property", False, "c2eec8a41739", "session_handle_class"),
    "pop_pending_oauth_requests": ("function", False, "92d6193dbe19", "session_handle_mcp"),
    "prompt": ("function", "agen", "ed7ccfb7b96f", "session_handle_turn"),
    "prompt_or_tool_seen": ("property", False, "a6632a57e602", "session_handle_class"),
    "prompt_outstanding_on_stall": ("property", False, "a6632a57e602", "session_handle_class"),
    "queued_frame_count": ("function", False, "fa6cfecd5574", "session_handle_mcp"),
    "rebind_watchdog": ("function", False, "c12bb41fb34c", "session_handle_watchdog"),
    "record_mcp_launches": ("function", False, "cf9b7e8daeb4", "session_handle_mcp"),
    "refresh_available_models": (
        "function",
        True,
        "de8486367b30",
        "session_handle_model_selection",
    ),
    "reject_tool": ("function", True, "84bed4474307", "session_handle_permissions"),
    "report_native_children": ("function", False, "ccfc57bdf135", "session_handle_native_children"),
    "send_command": ("function", True, "068aab573b43", "session_handle_lifecycle"),
    "served_model": ("property", False, "bb418bb7ece8", "session_handle_class"),
    "session_id": ("property", False, "bb418bb7ece8", "session_handle_class"),
    "set_config_option": ("function", True, "d17f94a912fd", "session_handle_model_selection"),
    "set_mode": ("function", True, "50a35c0990a8", "session_handle_model_selection"),
    "set_model": ("function", True, "bcd0cd3aa159", "session_handle_model_selection"),
    "status_rejections": ("property", False, "d8be45664668", "session_handle_class"),
    "steer": ("function", True, "b0a837a37097", "session_handle_steering"),
    "steer_needs_loss_recovery": ("property", False, "a6632a57e602", "session_handle_class"),
    "store_session_config": ("function", False, "83d029971996", "session_handle_model_selection"),
    "stream_command": ("function", "agen", "07d1ae72fdc6", "session_handle_turn"),
    "supports_config_option": ("function", False, "b86d69c93890", "session_handle_model_selection"),
    "supports_refusal_steer": ("property", False, "a6632a57e602", "session_handle_class"),
    "supports_steer": ("property", False, "a6632a57e602", "session_handle_class"),
    "wait_for_compaction": ("function", True, "0258399cca12", "session_handle_compaction"),
    "wait_for_declared_mcp": ("function", True, "921c3727b83c", "session_handle_mcp"),
    "wait_mcp_ready": ("function", True, "dc863b4461ff", "session_handle_mcp"),
    "wait_turn_done": ("function", True, "0ae71757d428", "session_handle_turn"),
}

#: Class-level annotations the split adds: fields first set by a member an owner defines,
#: declared in the class so type checkers know them. An annotation creates no attribute.
_DECLARED = frozenset({"_compaction_terminal_seen", "_kas_compact_turn"})

#: The owner modules, lowest layer first.
_OWNERS = (
    "kiro_crew.acp.session_handle_runtime_contract",
    "kiro_crew.acp.session_handle_watchdog",
    "kiro_crew.acp.session_handle_mcp",
    "kiro_crew.acp.session_handle_kas",
    "kiro_crew.acp.session_handle_native_children",
    "kiro_crew.acp.session_handle_model_selection",
    "kiro_crew.acp.session_handle_steering",
    "kiro_crew.acp.session_handle_lifecycle",
    "kiro_crew.acp.session_handle_compaction",
    "kiro_crew.acp.session_handle_permissions",
    "kiro_crew.acp.session_handle_elicitation",
    "kiro_crew.acp.session_handle_updates",
    "kiro_crew.acp.session_handle_turn",
    "kiro_crew.acp.session_handle_dispatch",
    "kiro_crew.acp.session_handle_class",
)

#: The module that holds the class statement and its class-resident members.
_CLASS_HOME = "session_handle_class"

_PACKAGE = "kiro_crew.acp"
_FACADE = session_handle.__name__

#: Every name the facade's import statements bound before the owners existed. They stay
#: bound here because callers and tests read and patch them as attributes of this module.
_BASE_IMPORTS = frozenset("""
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION
    ACP_BACKENDS_HOOKS_LIST ACP_BACKENDS_INLINE_COMPACTION
    ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL ACP_BACKENDS_STEER
    ACP_BACKENDS_STEERING_REQUEST ACP_BACKENDS_STRUCTURED_REFUSAL
    ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR ACP_BACKEND_KAS ACP_BACKEND_KIRO AcpClient
    AcpError AcpEvent AcpModelUnavailable AcpProcessDied AcpPromptStats AcpTimeoutError
    AcpToolGateUnroutable Any AsyncGenerator AsyncIterator Awaitable BUCKET_CAP
    CHILD_PERMISSION_DENIED COMPACT_WAIT_TIMEOUT_SECS Callable DEFAULT_MODEL
    DRAIN_YIELD_AFTER_S EVENT_AGENT_SWITCHED EVENT_CLEAR_STATUS EVENT_COMPACTION_STATUS
    EVENT_COMPLETE EVENT_MCP_OAUTH_REQUEST EVENT_MCP_SERVER_INITIALIZED
    EVENT_MCP_SERVER_INIT_FAILURE EVENT_STEER_CLEARED EVENT_STEER_CONSUMED
    EVENT_STEER_QUEUED EVENT_STRUCTURED_STATUS EVENT_SUBAGENT_ACTIVITY EVENT_SUBAGENT_LIST
    EVENT_TEXT_CHUNK EVENT_TODO_UPDATE EVENT_TOOL_CALL EVENT_TOOL_CALL_UPDATE
    EVENT_TOOL_RESULT EVIDENCE_ESTABLISHED_FLAT EVIDENCE_MCP_SUBTREE_ACTIVE
    EVIDENCE_PLATFORM_LIMITED EVIDENCE_REMOTE_FLAT EVIDENCE_SHELL_CHILD_ABSENT
    EntitlementRevalidating GrantDeadline GrantEnded INTERACTIVE_NARROWING_RISKS
    INTERACTIVE_NONE InFlightToolTracker InfraError InteractiveClassification
    JSONRPC_METHOD_NOT_FOUND JsonRpcMessage KasMcpReadiness LivenessOracle
    MAX_ACP_SESSION_ID_LEN MAX_ACTIVE_TOOL_CALLS MAX_STEERING_ANSWERS
    MAX_STEERING_TEXT_CHARS METHOD_CANCEL METHOD_COMMANDS_EXECUTE
    METHOD_KAS_CHECKPOINT_REVERT METHOD_KAS_MCP_STATUS METHOD_KAS_SESSION_COMPACT
    METHOD_MCP_OAUTH_REQUEST METHOD_PROMPT METHOD_REQUEST_PERMISSION METHOD_SESSION_STEERING
    METHOD_SET_CONFIG_OPTION METHOD_SET_MODE METHOD_SET_MODEL MODEL_CONFIG_ID Mapping
    McpSessionReport NAME_CAP OPTION_ALLOW_ALWAYS OPTION_ALLOW_ONCE OUTCOME_CANCELLED
    OUTCOME_SELECTED PROGRESS_SOURCE_PROCESS_EVIDENCE Protocol REATTEMPT_MAX
    STATUS_EXTENSION_KEY STATUS_ORIGIN_LIVENESS_ORACLE STATUS_PHASE_WAITING
    STEERING_ANSWER_WAIT_SECS STEERING_INJECTED STEERING_STARTED_NEW_TURN
    STOP_REASON_CANCELLED STOP_REASON_COMPACTION_FAILED STOP_REASON_CONTENT_FILTERED_WIRE
    STOP_REASON_END_TURN STOP_REASON_REFUSAL STOP_REASON_STALE_RECOVER
    STOP_REASON_TOOL_STALL SkillReadTracker StructuredStatus TERMINAL_TOOL_STATUSES
    TOOL_CALL_CACHE_MAX_ENTRIES TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS ToolCallState
    UPDATE_AGENT_MESSAGE_CHUNK UPDATE_AGENT_THOUGHT_CHUNK UPDATE_CURRENT_MODE
    UPDATE_SESSION_INFO VERDICT_DEAD VERDICT_STUCK_INPUT VERDICT_UNKNOWN VERDICT_WORKING
    WAIT_REASON_INPUT _COMPACTION_FAILED_TURN_BUDGET _consume_future_exception
    _effective_prompt_timeout_async _is_config_value_rejection _is_safe_oauth_url
    _is_tool_interrupted_marker _jsonrpc_error_code _loggable_request_id
    _push_model_via_effort_split _raise_acp_error acp_tool_gate advertised_model_ids asyncio
    boottime_now build_permission_event build_prompt_blocks capabilities_for
    catalog_row_would_drop classify_infra_error classify_interactive_command
    classify_notification compaction_failure_detail compaction_failure_is_transient
    consult_offloaded dataclass effort_config_option_id emit_counter
    error_is_refusal_terminal format_command_result grant_deadline_guard identified_mcp_call
    is_mcp_tool_approval is_recoverable_auth_failure is_unattributed_error_candidate
    kas_crew_agent_id kas_wire kiro_sessions_dir liveness_key logging model_registry
    parse_codex_compaction_update parse_metadata parse_prompt_token_usage parse_refusal
    parse_session_update parse_slash_command parse_text_chunk parse_usage_cost
    parse_usage_update permission_floor pick_served_default prompt_is_adapter_command
    prompt_timeout_for_ceiling redact_credentials redact_exfiltration_urls
    redact_log_via_context redact_text registration_rate_limited_error
    registration_throttle_line reject_option_id release_tool_call_provenance
    resolve_usable_model sanitize_sink_text scope_overflowed scoped_tool_cache_key sel
    set_mode_params set_model_params steady_now steering_outcome subprocess_executor
    summarize_prompt_structure time unattributed_terminal_error
    """.split())

#: The names the facade's own glue imports: the owner loop, the mirror, its class swap.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror", "_importlib", "_sys"})


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in session_handle._SESSION_HANDLE_OWNERS]


_ADDRESS = re.compile(r" at 0x[0-9A-Fa-f]+")


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    text = _ADDRESS.sub(" at 0x", str(sig.replace(parameters=params)))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def test_the_signature_digest_ignores_an_object_address_in_either_spelling() -> None:
    def f(x: object = None) -> None: ...

    lower = _ADDRESS.sub(" at 0x", "(x=<object object at 0x7f1a2b3c4d5e>)")
    upper = _ADDRESS.sub(" at 0x", "(x=<object object at 0x000001A9BFA701B0>)")
    assert lower == upper == "(x=<object object at 0x>)"
    assert len(_signature_digest(f)) == 12


def _member_function(name: str) -> Any:
    value = vars(AcpSessionHandle)[name]
    if isinstance(value, staticmethod):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _self_functions(
    tree: ast.Module, owner: str = ""
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The owner's functions the class binds: every ``self``-first function, plus the
    ones the inventory records as static methods of *owner*."""
    statics = {
        name
        for name, (kind, _, _, home) in _MEMBERS.items()
        if kind == "staticmethod" and home and f"{_PACKAGE}.{home}" == owner
    }
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and ((node.args.args and node.args.args[0].arg == "self") or node.name in statics)
    ]


def test_the_member_set_is_the_frozen_inventory() -> None:
    present = {
        name
        for name in vars(AcpSessionHandle)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    present |= set(AcpSessionHandle.__dict__.get("__annotations__", {}))
    assert sorted(present ^ (set(_MEMBERS) | _DECLARED)) == []
    assert not _DECLARED & set(vars(AcpSessionHandle))


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    if kind == "data":
        assert name in vars(AcpSessionHandle)
        return
    value = vars(AcpSessionHandle)[name]
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "property": isinstance(value, property),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    shape = inspect.iscoroutinefunction(fn) or (inspect.isasyncgenfunction(fn) and "agen")
    assert (shape or False) == coroutine
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    assert fn.__module__ == f"{_PACKAGE}.{owner}"
    if owner != _CLASS_HOME:
        assert vars(sys.modules[f"{_PACKAGE}.{owner}"])[name] is fn


@pytest.mark.parametrize(
    "name", sorted(n for n, row in _MEMBERS.items() if row[0] != "data" and row[3] != _CLASS_HOME)
)
def test_a_bound_member_reads_as_a_method_of_the_class(name: str) -> None:
    """``_own_methods`` names each bound function after its class attribute and drops the
    ``self`` annotation the owner carries for type checkers, so ``__qualname__`` (what a
    coroutine's and a task's repr print) and ``inspect.signature`` read as BASE's."""
    fn = _member_function(name)
    assert fn.__qualname__ == f"AcpSessionHandle.{name}"
    assert "self" not in fn.__annotations__
    # The code object still names the owner's module-level function: the body did move.
    assert fn.__code__.co_qualname == name


def test_every_owner_lists_in_the_facade_and_holds_its_members() -> None:
    assert session_handle._SESSION_HANDLE_OWNERS == _OWNERS
    homes = {owner for _kind, _c, _d, owner in _MEMBERS.values() if owner and owner != _CLASS_HOME}
    listed = {name.rsplit(".", 1)[-1] for name in _OWNERS} - {_CLASS_HOME}
    assert homes <= listed
    siblings = sorted(
        f"{_PACKAGE}.{path.stem}"
        for path in Path(session_handle.__file__ or "").parent.glob("session_handle_*.py")
    )
    assert siblings == sorted(_OWNERS)


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    for node in _self_functions(_tree(module), owner):
        assert node.name in _MEMBERS, f"{owner}.{node.name} is bound on no class"
        assert _member_function(node.name) is vars(module)[node.name]


def _class_only_constructs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """What breaks when a method body leaves its class: ``super()`` with no arguments, the
    ``__class__`` cell, and a name-mangled ``__attr`` (``__x`` without a trailing ``__``)."""
    found: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "super" and not node.args:
                found.append("super()")
        if isinstance(node, ast.Name) and node.id == "__class__":
            found.append("__class__")
        spelled = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if isinstance(spelled, str) and spelled.startswith("__") and not spelled.endswith("__"):
            found.append(spelled)
    return found


def test_the_class_only_scan_can_fail() -> None:
    tree = ast.parse("def f(self):\n    super().g()\n    return __class__, self.__hidden\n")
    assert sorted(_class_only_constructs(tree.body[0])) == ["__class__", "__hidden", "super()"]


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    tree = _tree(sys.modules[owner])
    assert {fn.name: _class_only_constructs(fn) for fn in _self_functions(tree, owner)} == {
        fn.name: [] for fn in _self_functions(tree, owner)
    }


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    """Lines that import the facade (``kiro_crew.acp.session_handle``) outside a
    ``TYPE_CHECKING`` block."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            guarded.update(id(inner) for inner in ast.walk(node))
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == _FACADE or (
                node.module == _PACKAGE and any(a.name == "session_handle" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == _FACADE for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.acp.session_handle import AcpSessionHandle\n"
        "def f():\n    from kiro_crew.acp.session_handle import x\n"
        "import kiro_crew.acp.session_handle\n"
        "from kiro_crew.acp import session_handle\n"
        "from kiro_crew.acp import session_handle_turn\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(sys.modules[owner])) == []


#: Calls on ``self.<field>`` that change the field's value in place.
_MUTATORS = frozenset({"add", "discard", "append", "pop", "update", "clear", "setdefault"})


def _self_field(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "self":
            return node.attr
    return None


def _fields(fns: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> tuple[set[str], set[str]]:
    """The ``self`` fields *fns* write and read. Writes: a store or delete of the
    attribute or of a subscript on it, a mutator call on it and ``setattr(self, "<f>",
    ...)``. Reads: every other load and ``getattr``/``hasattr(self, "<f>")``."""
    written: set[str] = set()
    read: set[str] = set()
    members = set(_MEMBERS)
    for fn in fns:
        for node in ast.walk(fn):
            name = _self_field(node)
            if name is not None:
                (written if isinstance(node.ctx, (ast.Store, ast.Del)) else read).add(name)
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                target = _self_field(node.value)
                if target is not None:
                    written.add(target)
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _MUTATORS:
                target = _self_field(func.value)
                if target is not None:
                    written.add(target)
            if (
                isinstance(func, ast.Name)
                and func.id in ("getattr", "hasattr", "setattr")
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "self"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                (written if func.id == "setattr" else read).add(node.args[1].value)
    return written - members, read - written - members


def test_the_field_scan_counts_every_read_and_write_shape() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    self.a = 1\n    del self.b\n    self.c[k] = v\n    del self.d[k]\n"
        "    self.e.add(1)\n    self.f.setdefault(k, v)\n    setattr(self, 'g', 1)\n"
        "    return self.h, getattr(self, 'i', None), hasattr(self, 'j'), self.e\n"
    )
    assert _fields([tree.body[0]]) == ({"a", "b", "c", "d", "e", "f", "g"}, {"h", "i", "j"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\n\n|\n?$)", doc, re.S)
    return set(re.findall(r"``(\w+)``", match.group(1))) if match else set()


@pytest.mark.parametrize("owner", _OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module), owner))
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


@pytest.mark.parametrize("owner", _OWNERS)
def test_owners_log_under_the_session_handle_logger(owner: str) -> None:
    logger = vars(sys.modules[owner]).get("logger", session_handle.logger)
    assert logger is session_handle.logger
    assert session_handle.logger.name == "kiro_crew.acp.session_handle"


def test_the_bound_function_scans_include_every_static_method() -> None:
    statics = {
        name: f"{_PACKAGE}.{home}"
        for name, (kind, _, _, home) in _MEMBERS.items()
        if kind == "staticmethod" and home != _CLASS_HOME
    }
    assert statics
    scanned = {
        fn.name
        for owner in set(statics.values())
        for fn in _self_functions(_tree(sys.modules[owner]), owner)
    }
    assert set(statics) <= scanned


def _owner_imports(tree: ast.Module) -> set[str]:
    """The session-handle owner modules *tree* imports, by dotted name."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(f"{_PACKAGE}.session_handle_"):
                found.add(node.module)
            elif node.module == _PACKAGE:
                found.update(
                    f"{_PACKAGE}.{a.name}"
                    for a in node.names
                    if a.name.startswith("session_handle_")
                )
        elif isinstance(node, ast.Import):
            found.update(
                a.name for a in node.names if a.name.startswith(f"{_PACKAGE}.session_handle_")
            )
    return found


def test_the_owner_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from kiro_crew.acp.session_handle_turn import X\n"
        "from kiro_crew.acp import session_handle_watchdog as _w, kas_wire\n"
        "import kiro_crew.acp.session_handle_dispatch\n"
    )
    assert _owner_imports(tree) == {
        "kiro_crew.acp.session_handle_turn",
        "kiro_crew.acp.session_handle_watchdog",
        "kiro_crew.acp.session_handle_dispatch",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_SESSION_HANDLE_OWNERS`` lists the owners lowest layer first: an owner imports only
    owners earlier in the tuple, so the order is the import order."""
    owners = session_handle._SESSION_HANDLE_OWNERS
    later = {
        owner: sorted(_owner_imports(_tree(sys.modules[owner])) - set(owners[:i]))
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


# --------------------------------------------------------------------------- #
# Moved classes keep the facade as their module.
# --------------------------------------------------------------------------- #

#: The runtime exception family: an error chain renders each raised class as
#: ``module.qualname``, so the path callers import them from is the one it names.
_FACADE_MODULE_CLASSES = (
    "AcpAgentUnavailable",
    "AcpFrameTooLarge",
    "AcpModeNotFound",
    "AcpRequestTimeout",
    "AcpRuntimeDead",
    "AcpRuntimeError",
    "AcpRuntimeOverloaded",
)

#: Moved classes no reader names by module: they keep the module that defines them.
_OWNER_MODULE_CLASSES = {
    "AcpRuntimeProtocol": "kiro_crew.acp.session_handle_runtime_contract",
    "WatchdogSettings": "kiro_crew.acp.session_handle_watchdog",
}


@pytest.mark.parametrize("name", _FACADE_MODULE_CLASSES)
def test_a_moved_exception_keeps_the_facade_as_its_module(name: str) -> None:
    cls = getattr(session_handle, name)
    assert (cls.__module__, cls.__qualname__) == (_FACADE, name)
    assert vars(session_handle)[name] is cls


def test_a_moved_exception_names_the_facade_in_an_error_chain() -> None:
    from kiro_crew.subagent import _describe_exception

    try:
        try:
            raise ValueError("inner")
        except ValueError as cause:
            raise session_handle.AcpRuntimeDead("gone") from cause
    except session_handle.AcpRuntimeDead as exc:
        text = _describe_exception(exc)
    assert "kiro_crew.acp.session_handle.AcpRuntimeDead" in text
    assert "session_handle_runtime_contract" not in text


@pytest.mark.parametrize("name", sorted(_OWNER_MODULE_CLASSES))
def test_a_moved_class_without_a_module_reader_keeps_its_owner_module(name: str) -> None:
    cls = getattr(session_handle, name)
    assert cls.__module__ == _OWNER_MODULE_CLASSES[name]
    assert vars(session_handle)[name] is cls


# --------------------------------------------------------------------------- #
# A write through the facade reaches every owner that reads the name.
# --------------------------------------------------------------------------- #

_TEST_TREE = Path(__file__).resolve().parents[1] / "test"
_SPELLINGS = {_FACADE: _FACADE}
#: The test trees: ``test/`` and the builtin apps' own test packages under ``src/``.
_CORPUS_ROOTS = (_TEST_TREE, _TEST_TREE.parent / "src" / "kiro_crew" / "apps")

#: Names a test patches through the facade in a shape the scan cannot read: an f-string
#: target over a loop of module names (``test_hang_resilience_metrics.py``).
_HAND_PATCHED = frozenset({"emit_counter"})


def _patched_through_the_facade() -> set[str]:
    """Every name a test file patches through ``kiro_crew.acp.session_handle``: the regular
    expressions read every file, and the syntax tree the files holding a patch shape the
    expressions cannot read."""
    names: set[str] = set()
    for path in sorted(p for root in _CORPUS_ROOTS for p in root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "session_handle" not in text:
            continue
        names |= text_patched_names(text, _SPELLINGS).get(_FACADE, set())
        if not needs_syntax_tree(text, _SPELLINGS):
            continue
        try:
            names |= patched_names(text, _SPELLINGS).get(_FACADE, set())
        except SyntaxError:
            continue
    return names | _HAND_PATCHED


def _reads(module: ModuleType) -> set[str]:
    tree = _tree(module)
    imports = {
        line
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.lineno not in imports
    }


def _reach_gaps(patched: set[str], index: Any) -> list[str]:
    gaps = []
    for module in _owners():
        for name in sorted(patched & _reads(module)):
            if name in vars(module) and module.__name__ not in index.holders.get(name, ()):
                gaps.append(f"{module.__name__}.{name}")
    return gaps


def test_every_name_patched_through_the_facade_reaches_the_owners_reading_it() -> None:
    patched = _patched_through_the_facade()
    # The scan saw names the owners read: a clock module, a drain bound, a budget, a path.
    assert {
        "time",
        "DRAIN_YIELD_AFTER_S",
        "_COMPACTION_FAILED_TURN_BUDGET",
        "kiro_sessions_dir",
        "_MCP_DRAIN_NO_REPORT_CEILING",
    } <= patched
    assert _reach_gaps(patched, session_handle._MIRROR) == []


def test_the_reach_scan_can_fail() -> None:
    index = session_handle._MIRROR
    owner = "kiro_crew.acp.session_handle_dispatch"
    holders = dict(index.holders)
    holders["time"] = tuple(h for h in holders["time"] if h != owner)
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps({"time"}, index) == []
    assert _reach_gaps({"time"}, blind) == [f"{owner}.time"]


def test_the_mirror_indexes_one_object_per_name_and_serves_only_moved_helpers() -> None:
    index = session_handle._MIRROR
    assert index.owners == _OWNERS
    assert index.excluded == ()
    assert index.divergent() == ()
    assert dict(index.forwards) == {}
    # What the facade serves without binding is exactly the moved module-level helpers
    # nothing outside the family and none of its own code reads, each from its owner.
    assert set(index.exports) == set(session_handle._SERVED)
    assert dict(index.exports) == session_handle._EXPORTS
    assert session_handle._SERVED
    for name, owner in index.exports.items():
        assert name not in vars(session_handle)
        assert getattr(session_handle, name) is vars(sys.modules[owner])[name]


def _bare_loads(tree: ast.Module, served: set[str]) -> list[tuple[int, str]]:
    """Every bare read of a served name outside an import statement."""
    import_lines = {
        line
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return sorted(
        (node.lineno, node.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in served
        and node.lineno not in import_lines
    )


def test_the_facade_reads_no_served_name_as_a_bare_global() -> None:
    """A function defined in the facade resolves a bare global through the facade's own
    namespace, which the mirror's ``__getattr__`` never sees, so such a read would need
    the facade to bind the name -- a second copy no patch of the owner reaches."""
    assert _bare_loads(_tree(session_handle), set(session_handle._EXPORTS)) == []


def test_the_bare_global_scan_can_fail() -> None:
    tree = ast.parse("from x import y\n\ndef f():\n    return _launch_argv\n")
    assert _bare_loads(tree, {"_launch_argv"}) == [(4, "_launch_argv")]
    assert _bare_loads(ast.parse("from x import (\n    _launch_argv,\n)\n"), {"_launch_argv"}) == []


def test_a_patch_through_the_facade_lands_on_every_owner_and_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = sys.modules["kiro_crew.acp.session_handle_mcp"]
    dispatch = sys.modules["kiro_crew.acp.session_handle_dispatch"]
    before = session_handle._MCP_DRAIN_NO_REPORT_CEILING
    yield_before = session_handle.DRAIN_YIELD_AFTER_S
    with monkeypatch.context() as patch:
        patch.setattr(session_handle, "_MCP_DRAIN_NO_REPORT_CEILING", 0.25)
        patch.setattr(session_handle, "DRAIN_YIELD_AFTER_S", 123.0)
        assert mcp._MCP_DRAIN_NO_REPORT_CEILING == 0.25
        assert dispatch.DRAIN_YIELD_AFTER_S == 123.0
        assert session_handle._MCP_DRAIN_NO_REPORT_CEILING == 0.25
    assert mcp._MCP_DRAIN_NO_REPORT_CEILING is before
    assert dispatch.DRAIN_YIELD_AFTER_S is yield_before
    assert session_handle._MCP_DRAIN_NO_REPORT_CEILING is before


def test_a_patch_of_a_served_helper_lands_on_its_reader_and_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn = sys.modules["kiro_crew.acp.session_handle_turn"]
    before = turn._DRAIN_CLOSED_STOP_REASONS
    sentinel = frozenset({"patched"})
    with monkeypatch.context() as patch:
        patch.setattr(session_handle, "_DRAIN_CLOSED_STOP_REASONS", sentinel)
        assert turn._DRAIN_CLOSED_STOP_REASONS is sentinel
        assert "_DRAIN_CLOSED_STOP_REASONS" not in vars(session_handle)
    assert turn._DRAIN_CLOSED_STOP_REASONS is before
    assert session_handle._DRAIN_CLOSED_STOP_REASONS is before


# --------------------------------------------------------------------------- #
# The class home: the class statement lives in session_handle_class.py.
# --------------------------------------------------------------------------- #


def test_the_class_keeps_the_facade_as_its_module_name_and_pickle_path() -> None:
    from kiro_crew.acp import runtime, session_handle_class

    assert AcpSessionHandle is session_handle_class.AcpSessionHandle is runtime.AcpSessionHandle
    assert (AcpSessionHandle.__module__, AcpSessionHandle.__qualname__) == (
        _FACADE,
        "AcpSessionHandle",
    )
    # What pickle and ``typing.get_type_hints`` do with a class: look it up by its module
    # and qualified name.
    assert getattr(sys.modules[AcpSessionHandle.__module__], "AcpSessionHandle") is AcpSessionHandle
    assert inspect.getdoc(AcpSessionHandle)


def test_every_class_resident_member_resolves_its_type_hints() -> None:
    import typing

    for name, (kind, _c, _d, owner) in _MEMBERS.items():
        if kind == "data" or owner != _CLASS_HOME:
            continue
        typing.get_type_hints(_member_function(name))
    assert set(typing.get_type_hints(AcpSessionHandle)) >= _DECLARED


def test_only_the_facade_imports_the_class_home() -> None:
    root = Path(session_handle.__file__ or "").resolve().parents[1]
    importers = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if _imports_the_class_home(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert importers == ["acp/session_handle.py"]


def _imports_the_class_home(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == f"{_PACKAGE}.{_CLASS_HOME}":
                return True
            if node.module == _PACKAGE and any(a.name == _CLASS_HOME for a in node.names):
                return True
        elif isinstance(node, ast.Import) and any(
            a.name == f"{_PACKAGE}.{_CLASS_HOME}" for a in node.names
        ):
            return True
    return False


def test_the_class_home_import_scan_can_fail() -> None:
    assert _imports_the_class_home(ast.parse("from kiro_crew.acp.session_handle_class import X\n"))
    assert _imports_the_class_home(ast.parse("from kiro_crew.acp import session_handle_class\n"))
    assert not _imports_the_class_home(ast.parse("from kiro_crew.acp import session_handle\n"))


def test_the_model_parsers_normalize_through_the_function_the_class_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``parse_advertised_models`` calls its owner's ``_normalize_models`` (it read the class
    attribute before the class moved), and the class binds that same function."""
    models = sys.modules["kiro_crew.acp.session_handle_model_selection"]
    assert vars(AcpSessionHandle)["_normalize_models"].__func__ is models._normalize_models
    normalized: list[dict[str, str]] = [{"modelId": "normalized"}]
    monkeypatch.setattr(models, "_normalize_models", lambda raw: normalized)
    wrapped = {"models": {"availableModels": [{"modelId": "raw"}]}}
    assert models.parse_advertised_models(wrapped) is normalized
    assert models.parse_advertised_models({"models": [{"modelId": "raw"}]}) is normalized


# --------------------------------------------------------------------------- #
# The facade imports nothing beyond its frozen inventory (F401's stand-in).
# --------------------------------------------------------------------------- #


def _is_type_checking(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"


def _defined_by(module_name: str) -> set[str]:
    """Names *module_name* defines at its top level (a def, class or assignment)."""
    from kiro_crew import _facade_mirror

    return _facade_mirror._defined_names(_tree(importlib.import_module(module_name)))


def unexpected_imports(source: str, owner_names: dict[str, set[str]]) -> list[str]:
    """Names *source* binds by a runtime import beyond the frozen inventory.

    The inventory is the BASE imports, the glue's own, each owner module under its
    ``_sh_<role>`` alias, and from each owner module only names that owner defines.
    Imports under ``if TYPE_CHECKING:`` bind nothing at run time and are the mirror
    guard's business, so they are skipped.
    """
    stems = {owner.rsplit(".", 1)[-1] for owner in owner_names}
    found: list[str] = []
    for node in ast.parse(source).body:
        if _is_type_checking(node):
            continue
        if isinstance(node, ast.Import):
            bound = [
                (alias.asname or alias.name.split(".")[0], None, alias.name) for alias in node.names
            ]
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            bound = [(alias.asname or alias.name, node.module, alias.name) for alias in node.names]
        else:
            continue
        for name, module, original in bound:
            if module in owner_names:
                if name not in owner_names[module]:
                    found.append(f"{module}.{name}")
            elif module == _PACKAGE and original in stems and name.startswith("_sh_"):
                continue
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS:
                found.append(name)
    return found


def _owner_names() -> dict[str, set[str]]:
    return {owner: _defined_by(owner) for owner in _OWNERS}


def test_the_facade_imports_nothing_beyond_its_frozen_inventory() -> None:
    source = Path(session_handle.__file__ or "").read_text(encoding="utf-8")
    assert unexpected_imports(source, _owner_names()) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: a stray import, and an owner import of a
    name that owner does not define, are both reported."""
    source = Path(session_handle.__file__ or "").read_text(encoding="utf-8")
    planted = source + (
        "\nimport json\nfrom kiro_crew.acp.session_handle_turn import AcpRuntimeDead\n"
    )
    assert unexpected_imports(planted, _owner_names()) == [
        "json",
        "kiro_crew.acp.session_handle_turn.AcpRuntimeDead",
    ]


def test_every_base_import_stays_bound_on_the_facade() -> None:
    assert sorted(_BASE_IMPORTS - set(vars(session_handle))) == []


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    import source_corpus

    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = Path(__file__).resolve()
    assert this_file.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)
