"""``AcpRuntime`` keeps every member it had, and each is bound from the module that owns it.

The runtime's behaviour lives in the ``kiro_crew.acp.runtime_*`` owner modules listed in
``kiro_crew.acp.runtime._RUNTIME_OWNERS``: each moved member's body is a module-level function
there whose first parameter is ``self``, and the class binds it by name (``m = _owner.m``, or
``staticmethod(_owner.f)``). The instance state stays on the instance, so tests,
``AcpRuntime.__new__`` builds and every caller see the same members they always did. This file
pins the binding contract:

* the member inventory -- every member the class had stays, with its kind, whether it is a
  coroutine, its signature (the ``self`` annotation aside) and the module that owns it; a
  member added later needs no row here;
* each bound member IS its owner's function, named as a method of the class
  (``__qualname__``), and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade at run time, and an owner imports only owners listed before it;
* each owner's docstring names the instance fields its members write and read;
* owners log under the facade's logger, and the moved exception classes keep the facade
  as their module;
* a write through ``kiro_crew.acp.runtime`` reaches every owner that reads the name, for every
  name the test trees patch there (the facade's mirror, ``_facade_mirror``), and the facade's
  forward table is the mirror's;
* every import the facade binds is accounted for (a BASE name, the glue's, owner-defined, or
  read by the family's code), the check ``setup.cfg``'s F401 exemption for it stands in for.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib
import inspect
import re
import sys
import typing
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from facade_mirror_scan import needs_syntax_tree, patched_names, text_patched_names

from kiro_crew.acp import runtime
from kiro_crew.acp.runtime import AcpRuntime

pytestmark = pytest.mark.xdist_group(name="tree_scan_runtime_bindings")

#: Member -> (kind, coroutine, signature digest, owning module). Frozen at the split: the
#: kind, coroutine flag and signature are BASE's; the owner is where the member lives now.
_MEMBERS: dict[str, tuple[str, object, str, str]] = {
    "_DESCENDANT_RESCAN_DELAY": ("data", False, "", "runtime_class"),
    "_EXIT_DRAIN_GRACE": ("data", False, "", "runtime_class"),
    "_EXIT_DRAIN_MAX": ("data", False, "", "runtime_class"),
    "_EXIT_WATCH_POLL_INTERVAL": ("data", False, "", "runtime_class"),
    "_FAILED_START_REAP_WAIT": ("data", False, "", "runtime_class"),
    "_KILL_REAP_TIMEOUT": ("data", False, "", "runtime_class"),
    "_KILL_TERM_TIMEOUT": ("data", False, "", "runtime_class"),
    "_RC_NOT_REAPED": ("data", False, "", "runtime_class"),
    "_RC_NO_PROCESS": ("data", False, "", "runtime_class"),
    "_REVIVE_ANSWER_DRAIN_TIMEOUT": ("data", False, "", "runtime_class"),
    "_ROOT_UNREACHED_REASON_BY_VERDICT": ("data", False, "", "runtime_class"),
    # Takes ``launch_agent``: the app launch copy kiro-cli is asked for (``apps.agent_launch``).
    "__init__": ("function", False, "933ec8f4a25b", "runtime_class"),
    "_activate_mode_bracketed": ("function", True, "0b6220ddca25", "runtime_mode_activation"),
    "_activates_agent_by_mode": ("function", False, "a6632a57e602", "runtime_mode_activation"),
    "_adopt_skill_projection": ("function", False, "5c4748bee6c5", "runtime_mode_activation"),
    "_adopted_skill_projection_generation": (
        "function",
        False,
        "fa6cfecd5574",
        "runtime_mode_activation",
    ),
    "_answer_get_access_token": ("function", True, "8cfd1a859983", "runtime_host_requests"),
    "_answer_host_request": ("function", True, "a18377df09da", "runtime_host_requests"),
    "_answer_open_external_url": ("function", True, "1440835ff53b", "runtime_host_requests"),
    "_answer_ownerless_request": ("function", True, "a18377df09da", "runtime_host_requests"),
    "_answer_secret_request": ("function", True, "40866b9ae753", "runtime_host_requests"),
    "_answer_unroutable_permission": ("function", True, "3c88fb3b5f4d", "runtime_host_requests"),
    "_audit_denied_off_loop": ("function", False, "bc403500636d", "runtime_host_requests"),
    "_collect_late_start": ("function", False, "df0576b2759e", "runtime_session_create"),
    "_compose_death_summary": ("function", False, "d296f1f5d821", "runtime_liveness"),
    "_confirm_member_projection": ("function", True, "c694d7c95d1b", "runtime_session_setup"),
    "_copy_native_launch_sources": ("function", False, "b4bc0bbb71ed", "runtime_session_setup"),
    "_discard_bound_workspace": ("function", True, "b60c5fea256e", "runtime_tree_kill"),
    "_discard_sandbox_cleanup": ("function", False, "b60c5fea256e", "runtime_spawn_launch"),
    "_drain_stderr": ("function", True, "b60c5fea256e", "runtime_liveness"),
    "_end_roster_overflow_episode": ("function", False, "b60c5fea256e", "runtime_frame_accounting"),
    "_exit_reason": ("function", False, "5ea8f751c789", "runtime_liveness"),
    "_exit_watch_loop": ("function", True, "b60c5fea256e", "runtime_liveness"),
    "_failed_start_cleanup": ("function", True, "b60c5fea256e", "runtime_spawn_launch"),
    "_finish_create_session": ("function", True, "70f51c6f9496", "runtime_session_create"),
    "_finish_session_init": ("function", False, "eb69f6ac3aa6", "runtime_session_registry"),
    "_flush_dropped_frames": ("function", False, "bc10f150ea47", "runtime_frame_accounting"),
    "_flush_roster_overflow": ("function", False, "bc10f150ea47", "runtime_frame_accounting"),
    "_guard_unresolved_mcp_refs": ("function", False, "124371616ca9", "runtime_session_setup"),
    "_handshake_client_capabilities": ("function", True, "16d9fe32838b", "runtime_spawn_launch"),
    "_harness": ("property", False, "482ead03a978", "runtime_class"),
    "_initialize_handshake": ("function", True, "9a2155299a08", "runtime_spawn_launch"),
    "_is_stale": ("function", True, "f21e39ec5ffb", "runtime_liveness"),
    "_issue_skill_projection_generation": (
        "function",
        False,
        "fa6cfecd5574",
        "runtime_mode_activation",
    ),
    "_kas_custom_agents": ("function", True, "997f5903aa65", "runtime_session_setup"),
    "_kill_failed_start_escapees": ("function", True, "ccaec8d76f05", "runtime_spawn_launch"),
    "_kill_inner": ("function", True, "4f9ced5c4b86", "runtime_tree_kill"),
    "_mark_dead": ("function", False, "e8c36427bffd", "runtime_liveness"),
    "_mcp_init_progress": ("function", False, "67daef5be2a5", "runtime_session_registry"),
    "_member_projection_needs_confirmation": (
        "function",
        False,
        "ead9c31b8aca",
        "runtime_session_setup",
    ),
    "_mirrored_session_mcp": ("function", True, "2db14e85d828", "runtime_session_setup"),
    "_mirrored_spec_check_needed": ("function", False, "3cd7cb3cf81a", "runtime_session_setup"),
    "_mode_available": ("staticmethod", False, "08e0d5cbf178", "runtime_mode_activation"),
    "_mount_member_panel": ("function", True, "bcc6c7135aaa", "runtime_session_setup"),
    "_native_persona_text": ("function", False, "d155a7c55d32", "runtime_mode_activation"),
    "_note_dropped_frame": ("function", False, "65da3cb3ad9e", "runtime_frame_accounting"),
    "_note_reaped": ("function", False, "65a401984277", "runtime_liveness"),
    "_note_reaped_after_kill": ("function", False, "0d4bde9dbb0a", "runtime_liveness"),
    "_note_roster_overflow": ("function", False, "72f2430f414f", "runtime_frame_accounting"),
    "_note_switched_model": ("function", False, "f5f767aea752", "runtime_mode_activation"),
    "_note_unadopted_skill_projection": (
        "function",
        False,
        "7e7f79a5fa13",
        "runtime_mode_activation",
    ),
    "_notification_aliases": ("function", False, "513465b2035b", "runtime_reader_loop"),
    "_one_at_a_time_ahead": ("function", False, "1250d8670681", "runtime_wire"),
    "_own_stub_session": ("function", True, "a5917f2a34a7", "runtime_session_setup"),
    "_projected_spawn_spec": ("function", False, "e7ed4605a35e", "runtime_spawn_launch"),
    "_reader_has_buffered_input": ("function", False, "a6632a57e602", "runtime_liveness"),
    "_reader_loop": ("function", True, "b60c5fea256e", "runtime_reader_loop"),
    "_reap_after_cancelled_tracking": ("function", True, "ae883e772e9f", "runtime_liveness"),
    "_record_native_persona_snapshot": (
        "function",
        True,
        "91c8fe45987e",
        "runtime_mode_activation",
    ),
    "_record_tree_before_failed_start_kill": (
        "function",
        True,
        "5ceead74864e",
        "runtime_spawn_launch",
    ),
    "_refuse_if_loader_unreachable": ("function", False, "246551d1efff", "runtime_session_setup"),
    "_refuse_if_view_superseded": ("function", False, "eabe9032415b", "runtime_mode_activation"),
    "_refuse_if_view_unverified": ("function", False, "e68a5cae7047", "runtime_mode_activation"),
    "_refuse_write_if_dead": ("function", False, "b60c5fea256e", "runtime_wire"),
    "_require_unchanged_mirrored_spec": ("function", True, "e0977b234f08", "runtime_session_setup"),
    "_resolve_spawn_plan": ("function", True, "a6093c8b82cf", "runtime_spawn_launch"),
    "_resolve_start_alias": ("function", False, "12ab76a15b9b", "runtime_mode_activation"),
    "_retire_tracking_after_exit": ("function", True, "b60c5fea256e", "runtime_liveness"),
    "_returncode_label": ("function", False, "bb418bb7ece8", "runtime_liveness"),
    "_revive_after_failed_start": ("function", True, "a6632a57e602", "runtime_spawn_entry"),
    "_root_identity": ("function", False, "bb418bb7ece8", "runtime_tree_kill"),
    "_root_identity_holds": ("function", False, "a6632a57e602", "runtime_tree_kill"),
    "_run_mcp_reattempt": ("function", True, "2547915d03d1", "runtime_host_requests"),
    "_run_mcp_sign_in": ("function", True, "2547915d03d1", "runtime_host_requests"),
    "_send_and_await": ("function", True, "150855d82a3e", "runtime_wire"),
    "_served_default_after_mode": ("function", True, "60c03c7acdea", "runtime_mode_activation"),
    "_session_start_budget": ("function", True, "59162e39f6f1", "runtime_session_setup"),
    "_session_start_stalled": ("function", False, "e49f31db985c", "runtime_session_registry"),
    "_session_teardown_method": ("function", False, "bb418bb7ece8", "runtime_session_registry"),
    "_session_work_dir": ("function", True, "921e81b9866d", "runtime_session_setup"),
    "_signal_tree": ("function", True, "16bbef62a9e3", "runtime_tree_kill"),
    "_skill_projection_lock": ("function", False, "fd738ff11600", "runtime_mode_activation"),
    "_snapshot_descendants": ("function", True, "82c2010f8649", "runtime_tree_kill"),
    "_snapshot_subagent_sessions": ("function", False, "b5c612257fd5", "runtime_frame_accounting"),
    "_source_agent": ("staticmethod", True, "a50b19e96682", "runtime_spawn_launch"),
    "_spawn_admitted": ("function", True, "b60c5fea256e", "runtime_spawn_launch"),
    "_spawn_admitted_rederiving_once": ("function", True, "b60c5fea256e", "runtime_spawn_entry"),
    "_spawn_agent_not_loaded_reason": ("function", True, "46026981236a", "runtime_mode_activation"),
    "_spawn_answer_task": ("function", True, "3c88fb3b5f4d", "runtime_host_requests"),
    "_spawn_persona_text": ("function", False, "2e34b912ca8a", "runtime_mode_activation"),
    "_stage_init_frame": ("function", False, "87459833bc5c", "runtime_session_registry"),
    "_stale_by_age": ("function", False, "a6632a57e602", "runtime_liveness"),
    "_start_mount_check": ("function", False, "3e413c5306a5", "runtime_session_setup"),
    "_stdin_write_lock": ("function", False, "fd738ff11600", "runtime_wire"),
    "_superseding_alias": ("function", False, "12ab76a15b9b", "runtime_mode_activation"),
    "_switched_model_record": ("function", False, "8cab7a4db01b", "runtime_mode_activation"),
    "_teardown_late_session": ("function", True, "c284282f8a1d", "runtime_session_create"),
    "_to_thread_guarding_sandbox": ("function", True, "8dbe076fd22d", "runtime_spawn_launch"),
    "_unadopted_skill_projection_generation": (
        "function",
        False,
        "fa6cfecd5574",
        "runtime_mode_activation",
    ),
    "_unpooled_control_planes": ("function", True, "5255b093c0d2", "runtime_session_setup"),
    "_verify_spawn_agent_active": ("function", True, "3d143e45e940", "runtime_mode_activation"),
    "_wait_for_answer_capacity": ("function", True, "609119a8d63a", "runtime_host_requests"),
    "_wait_managed_mcp": ("function", True, "318828f7ab77", "runtime_session_load"),
    "_write_request_bounded": ("function", True, "4ea88874f686", "runtime_wire"),
    "_write_response_bounded": ("function", True, "325c14124282", "runtime_wire"),
    "acp_backend": ("property", False, "bb418bb7ece8", "runtime_class"),
    "agent_version": ("property", False, "bb418bb7ece8", "runtime_class"),
    "begin_mcp_reattempt": ("function", False, "dc7c165669b1", "runtime_host_requests"),
    "begin_mcp_sign_in": ("function", False, "dc7c165669b1", "runtime_host_requests"),
    "create_session": ("function", True, "155a6120083d", "runtime_session_create"),
    "death_summary": ("function", False, "f21e39ec5ffb", "runtime_liveness"),
    "entitlement_probe_result_at": ("property", False, "59162e39f6f1", "runtime_class"),
    "forget_request": ("function", False, "a0b46301fd2c", "runtime_wire"),
    "has_active_or_initializing_sessions": ("function", False, "a6632a57e602", "runtime_liveness"),
    "has_active_sessions": ("function", False, "a6632a57e602", "runtime_liveness"),
    "is_alive": ("function", False, "a6632a57e602", "runtime_liveness"),
    "kill": ("function", True, "4f9ced5c4b86", "runtime_tree_kill"),
    "load_session": ("function", True, "abdb4c5767fc", "runtime_session_load"),
    "mark_turn_active": ("function", False, "a3b3a732c817", "runtime_session_registry"),
    "mcp_sign_in_holds": ("function", False, "dc7c165669b1", "runtime_host_requests"),
    "pid": ("property", False, "8dc15be1dfda", "runtime_class"),
    "probe_advertised_models": ("function", True, "d21329fd3356", "runtime_session_create"),
    "process_instance": ("property", False, "bb418bb7ece8", "runtime_class"),
    "process_tree_confirmed_dead": ("property", False, "a6632a57e602", "runtime_class"),
    "reattempts_mcp_servers": ("property", False, "a6632a57e602", "runtime_class"),
    "redacted_stderr_tail": ("function", False, "bb418bb7ece8", "runtime_liveness"),
    "refresh_process_tree": ("function", True, "b60c5fea256e", "runtime_tree_kill"),
    "sandbox_hidden_dirs": ("property", False, "9d5ab4ea1828", "runtime_class"),
    "sandbox_mode": ("property", False, "bb418bb7ece8", "runtime_class"),
    "sandbox_wrapped_by_crew": ("property", False, "a6632a57e602", "runtime_class"),
    "saw_not_logged_in": ("function", False, "a6632a57e602", "runtime_liveness"),
    "saw_sandbox_init_failure": ("function", False, "a6632a57e602", "runtime_liveness"),
    "send_error": ("function", True, "f8eeeb44db9e", "runtime_wire"),
    "send_notification": ("function", True, "78b195a50312", "runtime_wire"),
    "send_request": ("function", True, "60d06c03f35b", "runtime_wire"),
    "send_request_for_answer": ("function", True, "f3fe127a3feb", "runtime_wire"),
    "send_response": ("function", True, "74f287d889d6", "runtime_wire"),
    "settle_stderr": ("function", True, "8fe4e298f96b", "runtime_liveness"),
    "spawn": ("function", True, "dbecd2a33a23", "runtime_spawn_entry"),
    "spawn_monotonic": ("property", False, "c2eec8a41739", "runtime_class"),
    "start_collectors": ("function", False, "d0635fa60364", "runtime_session_create"),
    "stdin_reader_may_live": ("function", False, "a6632a57e602", "runtime_liveness"),
    "stdin_stall_death": ("property", False, "a6632a57e602", "runtime_class"),
    "supports_image_prompt": ("property", False, "a6632a57e602", "runtime_class"),
    "take_switched_model": ("function", False, "99d948c35ed6", "runtime_mode_activation"),
    "terminate_session": ("function", True, "c284282f8a1d", "runtime_session_registry"),
    "turn_active_at_stall": ("function", False, "762cb291397b", "runtime_liveness"),
    "unregister_session": ("function", False, "c284282f8a1d", "runtime_session_registry"),
    "uses_kiro_identity_store": ("property", False, "a6632a57e602", "runtime_class"),
    "work_scratch_dir": ("property", False, "d921fd8d443d", "runtime_class"),
}


#: The owner modules, lowest layer first.
_OWNERS = (
    "kiro_crew.acp.runtime_tree_kill",
    "kiro_crew.acp.runtime_spawn_launch",
    "kiro_crew.acp.runtime_spawn_entry",
    "kiro_crew.acp.runtime_session_setup",
    "kiro_crew.acp.runtime_session_create",
    "kiro_crew.acp.runtime_session_load",
    "kiro_crew.acp.runtime_session_registry",
    "kiro_crew.acp.runtime_mode_activation",
    "kiro_crew.acp.runtime_wire",
    "kiro_crew.acp.runtime_liveness",
    "kiro_crew.acp.runtime_host_requests",
    "kiro_crew.acp.runtime_frame_accounting",
    "kiro_crew.acp.runtime_reader_loop",
    "kiro_crew.acp.runtime_remote_workspace",
    "kiro_crew.acp.runtime_class",
)

#: Sibling modules named ``runtime_*`` that are not owners of the class: module-level code
#: that moved out of the facade before the class split, forwarded through ``_EXPORTS``.
_NOT_OWNERS = frozenset(
    {
        "kiro_crew.acp.runtime_models",
        "kiro_crew.acp.runtime_process_tree",
        "kiro_crew.acp.runtime_start",
    }
)

#: The module that holds the class statement and its class-resident members.
_CLASS_HOME = "runtime_class"

_PACKAGE = "kiro_crew.acp"
_FACADE = runtime.__name__

#: Every name the facade's import statements bound before the owners existed. They stay
#: bound here because callers and tests read and patch them as attributes of this module.
_BASE_IMPORTS = frozenset("""
    ACP_BACKENDS_MARKDOWN_AGENT_SPECS ACP_BACKENDS_SERIAL_SESSION_STARTS
    ACP_BACKENDS_SESSION_MCP_ARRAY ACP_BACKEND_KAS ACP_BACKEND_KIRO AcpFrameTooLarge
    AcpModeNotFound AcpRequestTimeout AcpRuntimeDead AcpRuntimeError AcpRuntimeOverloaded
    AcpRuntimeProtocol AcpSessionHandle Any Awaitable BoundWorkspaceMismatch
    CHILD_PERMISSION_DENIED CHILD_PERMISSION_ROUTED CLIENT_NAME CLIENT_VERSION Callable
    ChildRecord DROPPED_FRAMES HarnessAdapter HostAuthCallbackError INITIALIZE_TIMEOUT_SECS
    JsonRpcMessage JsonRpcRequest KAS_AUTH_CALLBACK_ERROR_CODE KIROCREW_RUNTIME_AGENT_ENV
    KIROCREW_SPAWN_HOME_ENV KIROCREW_SPAWN_INSTANCE_ENV KIRO_CLI_BIN KIRO_CLI_SUBCMD
    LaunchRequest LaunchTools MAX_ACP_SESSION_ID_LEN MCP_ROSTER_COMPLETE_NOTE
    METHOD_AGENT_SWITCHED METHOD_KAS_AUTH_GET_ACCESS_TOKEN METHOD_KAS_MCP_RESET_SERVER
    METHOD_KAS_OPEN_EXTERNAL_URL METHOD_MCP_OAUTH_REQUEST METHOD_MCP_SERVER_INITIALIZED
    METHOD_MCP_SERVER_INIT_FAILURE METHOD_REQUEST_PERMISSION METHOD_SESSION_LOAD
    METHOD_SESSION_NEW METHOD_SET_MODE MODEL_ID_RE MemberEssentialContextError ModuleType
    NAME_CAP NATIVE_CHILD_ROSTER_CAP NATIVE_SKILL_ALIAS_PREFIX NamedTuple
    NotificationAliases OversizeLineUnrecoverable PROTOCOL_VERSION PROTOCOL_VERSION_KAS Path
    RequestWriteResult SKILL_VIEW_FALLBACKS START_QUEUE_ADMISSION START_QUEUE_SESSION_NEW
    SessionExtras SpawnContext SpawnPlan StartCollector StartPermit StartPriority
    TYPE_CHECKING ToolSearchSettings TypeVar _INIT_NOTIFICATION_BUFFER_LIMIT
    _KiroExecutableTrustError _RESPONSE_WRITE_BOUND_SECS _RESPONSE_WRITE_MIN_PROGRESS_BYTES
    _SESSION_NEW_TIMEOUT _START_COLLECT_TIMEOUT_DEFAULT _STDOUT_BUFFER_LIMIT
    _apply_pod_home_remap _capture_child_records _cold_start_counts _drain_oversize_line
    _forward_ssh_auth_sock _get_child_pids _is_safe_oauth_url _kill_escaped_children
    _load_watchdog_settings _loggable_request_id _pgroup_has_member_besides
    _pid_gone_or_unmanaged _pid_start_token _reject_option_id _replace_child_pids
    _resolve_start_collect_timeout _signal_orphaned_runtime_group _split_init_frames
    _stall_window_phrase _track_pid _track_session_pid _untrack_child_pids _untrack_pid
    _untrack_pid_if_dead _untrack_root_by_identity _untrack_session_pid acp_driver
    acp_tool_gate active_custom_agent advertised_models_from_session agent_env_overlay
    agent_scratch agent_spec_snapshot agent_version_from_init agents_slice_throttling
    apply_pod_bundle_spawn assert_voice_runtime_outside_agent_workspace asyncio
    attach_kas_custom_agents attach_stub_session_token augmented_path authorize_runtime_kill
    backends_retired_by_host_logout bind_voice_safe_agent_workspace_async
    browser_session_env browser_socket_env build_session_new_params cgroup_scope_argv
    create_subprocess_limited data_home deque emit_counter encode_frame
    ensure_agent_materialized ensure_directory finish_suspended_spawn functools
    group_vouching_available harness_for has_mirror hoist_managed_servers importlib
    inject_xdist_auto_cap injection_server_names is_auth_failure_output
    is_sandbox_init_failure_output kas_client_meta_settings kas_secret_storage
    kas_wire_agent_id kiro_agents_dir launch live logging markdown_spec_for_agent
    member_essential_context mint_stub_session_token mirror_for name_scope_unit
    notify_start_queue os outstanding_leases overlay_project_scope parse_json_object_line
    parse_session_modes platform_compat pooled_session_servers publish_session_token re
    record_frame redact_backend_text redact_credentials redact_exfiltration_urls redact_text
    register_protected_pid registration_throttle_line release_bound_agent_workspace
    required_managed_servers resolve_bound_session_workspace resolve_krb5_ccname
    response_write_window_secs roster_names runtime_death runtime_process_tree runtime_start
    sanitize_sink_text schedule_mount_check scope_unit_name scrub_agent_subprocess_env
    send_claim session_mcp_disabled_tools session_mcp_server_is_disabled set_mode_params
    settle_drain signal spec_grants_tool_search subprocess subprocess_executor sys time
    unavailable_mode_explanation unregister_protected_pid uuid
    warn_remote_header_secret_refs warn_unresolved_server_refs weakref
    with_client_meta_settings wrap_argv wrap_argv_async wrapped_by_crew_sandbox
    write_notification_best_effort write_request_frame_bounded write_response_frame_bounded
    """.split())

#: The names the facade's own glue imports: the mirror its class swap installs.
_GLUE_IMPORTS = frozenset({"_facade_mirror"})


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in runtime._RUNTIME_OWNERS]


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
    value = vars(AcpRuntime)[name]
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


def test_every_inventory_member_is_still_on_the_class() -> None:
    present = {
        name
        for name in vars(AcpRuntime)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    assert sorted(set(_MEMBERS) - present) == []


#: Fields a bound member assigns first, declared for type checkers in the class body.
_TYPE_CHECKING_DECLARED = frozenset(
    {
        "_native_skill_projection",
        "_skill_projection_generation",
        "_skill_projection_issued",
        "_skill_projection_lock_obj",
        "_skill_projection_unadopted",
        "_spawn_skill_projection",
        "_stdin_lock",
    }
)


def _type_checking_declarations() -> set[str]:
    """The fields the class declares inside ``if TYPE_CHECKING:``, read off its source."""
    home = sys.modules[AcpRuntime.__init__.__module__]
    cls = next(
        node
        for node in _tree(home).body
        if isinstance(node, ast.ClassDef) and node.name == "AcpRuntime"
    )
    return {
        stmt.target.id
        for block in cls.body
        if isinstance(block, ast.If) and ast.unparse(block.test) == "TYPE_CHECKING"
        for stmt in block.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }


def test_the_fields_a_member_assigns_first_are_declared_for_type_checkers_only() -> None:
    """A field a bound member assigns first is declared for mypy, never at run time: the
    class carries no annotations, as before the split, and a runtime built with
    ``__new__`` reads each one through the ``getattr`` default its readers pass."""
    declared = _type_checking_declarations()
    assert declared >= _TYPE_CHECKING_DECLARED
    assert not declared & set(vars(AcpRuntime))
    assert AcpRuntime.__dict__.get("__annotations__", {}) == {}


def test_type_hints_resolve_for_the_class_and_every_member() -> None:
    """``typing.get_type_hints`` evaluates each annotation in the module that holds it: the
    class has none, as before the split, and every member's hints resolve where its body
    now lives."""
    assert typing.get_type_hints(AcpRuntime) == {}
    for name, (kind, _coroutine, _digest, _owner) in _MEMBERS.items():
        if kind != "data":
            typing.get_type_hints(_member_function(name))


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    if kind == "data":
        assert name in vars(AcpRuntime)
        return
    value = vars(AcpRuntime)[name]
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
    assert fn.__qualname__ == f"AcpRuntime.{name}"
    assert "self" not in fn.__annotations__
    # The code object still names the owner's module-level function: the body did move.
    assert fn.__code__.co_qualname == name


def test_the_qualname_restore_reaches_exactly_the_owner_modules() -> None:
    """``_own_methods`` rewrites the functions of the modules it names and nothing else: the
    owners the class binds from, never a sibling like ``runtime_start``."""
    home = sys.modules[AcpRuntime.__init__.__module__]
    assert home._MEMBER_OWNERS == frozenset(_OWNERS) - {f"{_PACKAGE}.{_CLASS_HOME}"}
    assert not home._MEMBER_OWNERS & _NOT_OWNERS


def test_every_owner_lists_in_the_facade_and_holds_its_members() -> None:
    assert runtime._RUNTIME_OWNERS == _OWNERS
    homes = {owner for _kind, _c, _d, owner in _MEMBERS.values() if owner and owner != "runtime"}
    listed = {name.rsplit(".", 1)[-1] for name in _OWNERS}
    assert homes <= listed
    siblings = {
        f"{_PACKAGE}.{path.stem}"
        for path in Path(runtime.__file__ or "").parent.glob("runtime_*.py")
    }
    assert sorted(siblings - _NOT_OWNERS) == sorted(_OWNERS)


def _unbound_self_functions(owner: str, tree: ast.Module) -> list[str]:
    """The ``self``-first functions in *owner*'s *tree* that the class does not bind.

    Each must be an attribute of the class whose function IS the owner's: the class is the
    authority, not the inventory, so a member an owner gains later passes with no row here.
    """
    module = sys.modules[owner]
    return [
        node.name
        for node in _self_functions(tree, owner)
        if node.name not in vars(AcpRuntime)
        or _member_function(node.name) is not vars(module).get(node.name)
    ]


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    unbound = _unbound_self_functions(owner, _tree(sys.modules[owner]))
    assert unbound == [], f"{owner}: {unbound} bound on no class"


def test_the_unbound_scan_passes_a_later_bound_member_and_fails_an_unbound_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _OWNERS[0]
    module = sys.modules[owner]

    def _planted_bound(self: Any) -> None:
        return None

    monkeypatch.setattr(module, "_planted_bound", _planted_bound, raising=False)
    monkeypatch.setattr(AcpRuntime, "_planted_bound", _planted_bound, raising=False)
    tree = _tree(module)
    planted = "def _planted_bound(self):\n    pass\n\n\ndef _planted_unbound(self):\n    pass\n"
    tree.body.extend(ast.parse(planted).body)
    assert _unbound_self_functions(owner, tree) == ["_planted_unbound"]


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
    """Lines that import the facade (``kiro_crew.acp.runtime``) outside a ``TYPE_CHECKING``
    block."""
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
                node.module == _PACKAGE and any(a.name == "runtime" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == _FACADE for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.acp.runtime import AcpRuntime\n"
        "def f():\n    from kiro_crew.acp.runtime import x\n"
        "import kiro_crew.acp.runtime\n"
        "from kiro_crew.acp import runtime\n"
        "from kiro_crew.acp import runtime_start\n"
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
def test_owners_log_under_the_runtime_logger(owner: str) -> None:
    logger = vars(sys.modules[owner]).get("logger", runtime.logger)
    assert logger is runtime.logger
    assert runtime.logger.name == "kiro_crew.acp.runtime"


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
    """The runtime owner modules *tree* imports, by dotted name."""
    found: set[str] = set()
    owners = set(_OWNERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in owners:
                found.add(node.module)
            elif node.module == _PACKAGE:
                found.update(
                    f"{_PACKAGE}.{a.name}" for a in node.names if f"{_PACKAGE}.{a.name}" in owners
                )
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name in owners)
    return found


def test_the_owner_import_scan_can_fail() -> None:
    first, second, third = _OWNERS[:3]
    tree = ast.parse(
        f"from {first} import X\n"
        f"from {_PACKAGE} import {second.rsplit('.', 1)[-1]} as _w, kas_wire\n"
        f"import {third}\n"
        f"from {_PACKAGE} import runtime_start\n"
    )
    assert _owner_imports(tree) == {first, second, third}


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_RUNTIME_OWNERS`` lists the owners lowest layer first: an owner imports only owners
    earlier in the tuple, so the order is the import order."""
    owners = runtime._RUNTIME_OWNERS
    later = {
        owner: sorted(_owner_imports(_tree(sys.modules[owner])) - set(owners[:i]))
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


# --------------------------------------------------------------------------- #
# Moved classes keep the facade as their module.
# --------------------------------------------------------------------------- #

#: Exceptions an error chain renders as ``module.qualname``: the path callers import them
#: from is the one it names.
_FACADE_MODULE_CLASSES = (
    "AcpRuntimeStdinStalled",
    "AcpSessionStartTimeout",
    "AcpToolSurfaceBindingError",
    "AcpWorkspaceBindingError",
)

#: Moved classes no reader names by module: they keep the module that defines them.
_OWNER_MODULE_CLASSES = {
    "_MirroredSessionMcp": "kiro_crew.acp.runtime_session_setup",
    "_PendingRequests": "kiro_crew.acp.runtime_wire",
}


@pytest.mark.parametrize("name", _FACADE_MODULE_CLASSES)
def test_a_moved_exception_keeps_the_facade_as_its_module(name: str) -> None:
    cls = getattr(runtime, name)
    assert (cls.__module__, cls.__qualname__) == (_FACADE, name)
    assert vars(runtime)[name] is cls


def test_a_moved_exception_names_the_facade_in_an_error_chain() -> None:
    from kiro_crew.subagent import _describe_exception

    error = runtime.AcpWorkspaceBindingError
    try:
        try:
            raise ValueError("inner")
        except ValueError as cause:
            raise error("gone") from cause
    except error as exc:
        text = _describe_exception(exc)
    assert f"kiro_crew.acp.runtime.{error.__qualname__}" in text
    assert "kiro_crew.acp.runtime_" not in text


@pytest.mark.parametrize("name", sorted(_OWNER_MODULE_CLASSES))
def test_a_moved_class_without_a_module_reader_keeps_its_owner_module(name: str) -> None:
    cls = getattr(runtime, name)
    assert cls.__module__ == _OWNER_MODULE_CLASSES[name]
    assert vars(runtime)[name] is cls


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
    """Every name a test file patches through ``kiro_crew.acp.runtime``: the regular
    expressions read every file, and the syntax tree the files holding a patch shape the
    expressions cannot read."""
    names: set[str] = set()
    for path in sorted(p for root in _CORPUS_ROOTS for p in root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "runtime" not in text:
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
    return _tree_reads(_tree(module))


def _tree_reads(tree: ast.Module) -> set[str]:
    """Every name *tree*'s code reads as a bare global (a ``Name`` load off its import lines)."""
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
    # The scan saw names the owners read: a clock module, a handshake budget, the launch
    # tail's spawn factory and a session array helper.
    assert {
        "time",
        "_INITIALIZE_TIMEOUT",
        "create_subprocess_limited",
        "_pooled_session_servers_and_ref_spec",
    } <= patched
    assert _reach_gaps(patched, runtime._MIRROR) == []


def test_the_reach_scan_can_fail() -> None:
    index = runtime._MIRROR
    owner = "kiro_crew.acp.runtime_spawn_entry"
    holders = dict(index.holders)
    holders["time"] = tuple(h for h in holders["time"] if h != owner)
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps({"time"}, index) == []
    assert _reach_gaps({"time"}, blind) == [f"{owner}.time"]


def test_the_mirror_indexes_one_object_per_name_and_forwards_the_legacy_table() -> None:
    index = runtime._MIRROR
    assert index.owners == _OWNERS
    assert index.excluded == ()
    assert index.divergent() == ()
    # The facade serves exactly the names its forward table held before the owners existed,
    # each read through the module that owns it; every moved name stays bound here.
    assert dict(index.forwards) == runtime._index_exports()
    assert dict(index.exports) == {}
    assert runtime._EXPORTS == runtime._index_exports()
    assert index.forward_reads == "import_module"


def test_a_patch_through_the_facade_lands_on_every_owner_and_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_owner = sys.modules["kiro_crew.acp.runtime_spawn_launch"]
    before = runtime._INITIALIZE_TIMEOUT
    with monkeypatch.context() as patch:
        patch.setattr(runtime, "_INITIALIZE_TIMEOUT", 1.5)
        assert launch_owner._INITIALIZE_TIMEOUT == 1.5
        assert runtime._INITIALIZE_TIMEOUT == 1.5
    assert launch_owner._INITIALIZE_TIMEOUT is before
    assert runtime._INITIALIZE_TIMEOUT is before


# --------------------------------------------------------------------------- #
# Every import the facade binds is accounted for (F401's stand-in).
# --------------------------------------------------------------------------- #


def _is_type_checking(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"


def _defined_by(module_name: str) -> set[str]:
    """Names *module_name* defines at its top level (a def, class or assignment)."""
    from kiro_crew import _facade_mirror

    return _facade_mirror._defined_names(_tree(importlib.import_module(module_name)))


def unexpected_imports(
    source: str, owner_names: dict[str, set[str]], owner_reads: set[str]
) -> list[str]:
    """Names *source* binds by a runtime import that nothing accounts for.

    An import is accounted for when it is a BASE import, the glue's own, an owner module
    under its ``_rt_<role>`` alias, a name an owner module defines imported from that
    owner, or a name the family's own code reads (the facade's *source*, or any owner's
    code: *owner_reads*). A later import the facade's code uses therefore needs no edit
    here; an import nothing reads, and an import of a name from an owner that does not
    define it, are reported. Imports under ``if TYPE_CHECKING:`` bind nothing at run time
    and are the mirror guard's business, so they are skipped.
    """
    stems = {owner.rsplit(".", 1)[-1] for owner in owner_names}
    read = _tree_reads(ast.parse(source)) | owner_reads
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
            elif module == _PACKAGE and original in stems and name.startswith("_rt_"):
                continue
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS | read:
                found.append(name)
    return found


def _owner_names() -> dict[str, set[str]]:
    return {owner: _defined_by(owner) for owner in _OWNERS}


def _owner_reads() -> set[str]:
    return set().union(*(_reads(owner) for owner in _owners()))


def test_every_facade_import_is_accounted_for() -> None:
    source = Path(runtime.__file__ or "").read_text(encoding="utf-8")
    assert unexpected_imports(source, _owner_names(), _owner_reads()) == []


def test_the_import_check_reports_only_what_nothing_accounts_for() -> None:
    """The F401 stand-in must be able to fail, and must not freeze the facade: an import
    nothing reads, and an owner import of a name that owner does not define, are reported;
    an import the facade's code reads, and one an owner's code reads, are not."""
    source = Path(runtime.__file__ or "").read_text(encoding="utf-8")
    planted = source + (
        "\nimport zipapp\nimport csv\nimport shlex\n_PLANTED_READ = csv.writer\n"
        "from kiro_crew.acp.runtime_spawn_launch import AcpRuntimeDead\n"
    )
    assert "zipapp" not in _owner_reads()
    assert unexpected_imports(planted, _owner_names(), _owner_reads() | {"shlex"}) == [
        "zipapp",
        "kiro_crew.acp.runtime_spawn_launch.AcpRuntimeDead",
    ]


def test_every_base_import_stays_bound_on_the_facade() -> None:
    assert sorted(_BASE_IMPORTS - set(vars(runtime))) == []


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


#: ``typing.get_type_hints(AcpRuntime.__init__)`` before the class left ``runtime.py``.
_INIT_HINTS = {
    "acp_backend": "<class 'str'>",
    "agent": "<class 'str'>",
    "crew_agent": "<class 'str'>",
    "expect_mcp_reports": "<class 'bool'>",
    "extra_env": "dict[str, str] | None",
    "max_age_secs": "<class 'float'>",
    "max_rss_mb": "<class 'float'>",
    "mcp_gateway_overlay": "str | pathlib.Path | None",
    "mcp_gateway_socket": "str | pathlib.Path | None",
    "member_context": "<class 'bool'>",
    "memory_mode": "<class 'str'>",
    "model": "str | None",
    "sandbox_mode": "<class 'str'>",
    "shared_scratch": "pathlib.Path | None",
    "launch_agent": "<class 'str'>",
    "tool_search": "kiro_crew.agent_sdk.tool_search.ToolSearchSettings | None",
    "work_dir": "str | pathlib.Path | None",
}


def test_the_class_home_keeps_the_facade_as_the_class_module() -> None:
    """The class statement lives in its class home, and the facade is its import path: the
    class names the facade as its ``__module__`` (what reprs and error chains print), and the
    facade binds the class home's very object, never a copy or a subclass."""
    home = sys.modules[AcpRuntime.__init__.__module__]
    assert home.__name__ == f"{_PACKAGE}.{_CLASS_HOME}"
    assert (AcpRuntime.__module__, AcpRuntime.__qualname__) == (_FACADE, "AcpRuntime")
    assert vars(runtime)["AcpRuntime"] is vars(home)["AcpRuntime"]
    assert _FACADE + ".AcpRuntime object at" in repr(object.__new__(AcpRuntime))
    # A pickle stores a class as module + qualname and loads it back by that lookup.
    assert (
        getattr(importlib.import_module(AcpRuntime.__module__), AcpRuntime.__qualname__)
        is AcpRuntime
    )


def test_the_constructor_keeps_its_type_hints() -> None:
    hints = typing.get_type_hints(AcpRuntime.__init__)
    assert {name: repr(hint) for name, hint in hints.items()} == _INIT_HINTS


def test_a_facade_patch_reaches_the_class_resident_constructor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``__init__`` reads its globals in the class home; a patch through the facade still
    reaches it there, because the mirror writes every module binding the same object."""

    class StandIn(runtime._PendingRequests):
        pass

    monkeypatch.setattr(runtime, "_PendingRequests", StandIn)
    rt = AcpRuntime(work_dir=str(tmp_path))
    assert type(rt._pending_requests) is StandIn
