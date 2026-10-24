"""``AcpClient`` keeps every member it had, and each is bound from the module that owns it.

The class's behaviour lives in the ``kiro_crew.acp.client_*`` owner modules: each member's
body is a module-level function there whose first parameter is ``self``, and the class
binds it by name (``m = _owner.m``, or ``staticmethod(_owner.f)``). The instance state
stays on the instance, so tests, ``AcpClient.__new__`` builds and the ``ProcessSession``
port see the same object they always did. This file pins the binding contract:

* the frozen member inventory -- every member, its kind, whether it is a coroutine or an
  async generator, its signature (the ``self`` annotation aside) and the module that owns it;
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade or the class's module at run time;
* each owner's docstring names the instance fields its members write and read;
* a write through ``kiro_crew.acp.client`` reaches every owner that reads the name, for
  every name the test trees patch there (the facade's mirror, ``_facade_mirror``);
* the class statement lives in ``client_class`` and still names the facade as its module,
  for reprs, pickles and type hints;
* the facade fills the session start's ``_resolve_spawn_env``, which stays defined there.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import AcpClient

pytestmark = pytest.mark.xdist_group(name="tree_scan_client_bindings")

#: Member -> (kind, coroutine, signature digest, owning module). Frozen at the split: the
#: kind, coroutine flag and signature are BASE's; the owner is where the member lives now.
_MEMBERS: dict[str, tuple[str, object, str, str]] = {
    "__init__": ("function", False, "0da0e673600b", "client_class"),
    "_add_orphan_steers": ("function", False, "b3c41cdccc27", "client_turn_control"),
    "_advertised_model_ids": ("function", False, "6806e867af4e", "client_model_selection"),
    "_agent_env_for_spawn": ("function", False, "8cab7a4db01b", "client_spawn_env"),
    "_append_member_dispatch_server": ("function", False, "94c1c94e7642", "client_mcp_array"),
    "_append_member_panel_server": ("function", False, "94c1c94e7642", "client_mcp_array"),
    "_apply_session_identity_env": ("function", False, "1b5cd29daa82", "client_spawn_env"),
    "_apply_session_permission_routing": ("function", True, "b60c5fea256e", "client_permissions"),
    "_apply_startup_model": ("function", True, "b60c5fea256e", "client_model_selection"),
    "_audit_cancelled_approvals": ("function", True, "92ba6192cafa", "client_turn_control"),
    "_audit_elicitation_decision": ("function", True, "fa17e47d8779", "client_elicitation"),
    "_audit_spec_restriction": ("function", False, "76fead145772", "client_permissions"),
    "_await_steering_answer": ("function", True, "388d43738a7e", "client_turn_control"),
    "_backfill_context_window": ("function", False, "f3e546765020", "client_usage"),
    "_begin_session_report": ("function", False, "a9c1df12798e", "client_mcp_array"),
    "_build_permission_event": ("function", False, "e64128661dfa", "client_permissions"),
    "_capture_available_models": ("function", False, "8e047a255b27", "client_model_selection"),
    "_claim_pathname_if_ours": ("staticmethod", False, "af30c5c7e262", "client_class"),
    "_classify_failed_start": ("function", True, "05bb8f207290", "client_lifecycle"),
    "_claude_adapter_below_setting_sources_floor": (
        "function",
        False,
        "a6632a57e602",
        "client_claude_session",
    ),
    "_claude_adapter_disk_version": ("annotation", False, "", ""),
    "_claude_compaction_event": ("function", False, "0d6b4eecc1f2", "client_usage"),
    "_claude_local_settings_path": ("function", False, "b08d8e6a45bd", "client_seed_ownership"),
    "_claude_session_excludes_local": ("function", False, "a6632a57e602", "client_claude_session"),
    "_claude_session_mcp_servers": ("function", False, "f5423acdeebc", "client_mcp_array"),
    "_claude_session_meta": ("function", False, "16d9fe32838b", "client_claude_session"),
    "_claude_settings_is_still_ours": ("function", False, "a6632a57e602", "client_seed_ownership"),
    "_cleanup_failed_live_spawn": ("function", True, "b60c5fea256e", "client_lifecycle"),
    "_close_steer_turn": ("function", False, "f604c3478f16", "client_turn_control"),
    "_codex_compaction_event": ("function", False, "e64128661dfa", "client_usage"),
    "_confirm_member_projection": ("function", False, "0e165137f085", "client_class"),
    "_consult_liveness_model_wait": ("function", True, "0329a680bb66", "client_turn"),
    "_copy_aside_back_without_clobber": (
        "staticmethod",
        False,
        "460afeb00773",
        "client_seed_ownership",
    ),
    "_decline_elicitation": ("function", True, "87459833bc5c", "client_elicitation"),
    "_deepseek_session_mcp_servers": ("function", False, "f5423acdeebc", "client_mcp_array"),
    "_defer_opencode_shell_permission": ("function", True, "7c70a056d5ef", "client_turn"),
    "_deny_spec_disabled_tool": ("function", True, "2dfacffa6279", "client_permissions"),
    "_delegate_launch_failure": ("function", True, "35f56320d7d1", "client_lifecycle"),
    "_deny_zero_tools": ("function", True, "2dfacffa6279", "client_permissions"),
    "_discard_bound_workspace": ("function", True, "b60c5fea256e", "client_lifecycle"),
    "_discard_claude_settings_seed": ("function", True, "b60c5fea256e", "client_seed_ownership"),
    "_discard_pi_mcp_handoff": ("function", True, "b60c5fea256e", "client_mcp_array"),
    "_discard_sandbox_cleanup": ("function", False, "b60c5fea256e", "client_lifecycle"),
    "_dispatch_events": ("function", "agen", "f222467c1516", "client_turn"),
    "_dispatch_prompt_events": ("function", "agen", "69109bbc58c2", "client_turn_control"),
    "_drain_notifications": ("function", True, "23967cf3fb9c", "client_transport"),
    "_drain_post_compaction_metadata": ("function", True, "65fe4b4d1210", "client_usage"),
    "_drain_stderr": ("function", True, "550410b8da31", "client_lifecycle"),
    "_emit_tool_interrupted_sel": ("function", False, "866fc4f75f7c", "client_tool_events"),
    "_enforces_zero_tool_ban": ("property", False, "a6632a57e602", "client_class"),
    "_ensure_served_default": ("function", True, "b60c5fea256e", "client_model_selection"),
    "_entitlement_probe_answer": ("function", True, "1ca8e8c3a438", "client_model_selection"),
    "_entitlement_probe_client": ("function", False, "4d5584e1d14b", "client_model_selection"),
    "_exclude_foreign_local_settings": ("function", False, "0b50a902117a", "client_claude_session"),
    "_expected_settings_fingerprint": ("function", False, "77be1e224b87", "client_seed_ownership"),
    "_extract_text_chunk": ("function", False, "0d1d31659e5f", "client_tool_events"),
    "_extract_tool_call_refinement": ("function", False, "e64128661dfa", "client_tool_events"),
    "_extract_tool_call_update": ("function", False, "e64128661dfa", "client_tool_events"),
    "_extract_tool_event": ("function", False, "e64128661dfa", "client_tool_events"),
    "_foreign_mcp_identity": ("function", False, "2dfacffa6279", "client_permissions"),
    "_foreign_settings_is_crew_seed": ("function", False, "a6fc7539bdf3", "client_seed_ownership"),
    "_foreign_settings_owner": ("function", False, "0d96e8ab9b17", "client_seed_ownership"),
    "_fork_session": ("function", True, "bca802976336", "client_start"),
    "_goose_session_mcp_servers": ("function", False, "f5423acdeebc", "client_mcp_array"),
    "_guard_unresolved_mcp_refs": ("function", False, "779b51571c73", "client_mcp_array"),
    "_hand_back_unrevoked_claim": ("function", False, "d12bff1e2bd1", "client_seed_ownership"),
    "_handle_compaction_status": ("function", False, "87459833bc5c", "client_usage"),
    "_handle_config_option_update": ("function", False, "87459833bc5c", "client_model_selection"),
    "_handle_elicitation_request": ("function", True, "e64128661dfa", "client_elicitation"),
    "_handle_permission": ("function", True, "87459833bc5c", "client_permissions"),
    "_harness_mcp_server_names": ("function", False, "9d5ab4ea1828", "client_mcp_array"),
    "_initialize_params": ("function", False, "16d9fe32838b", "client_start"),
    "_initialize_session": ("function", True, "b60c5fea256e", "client_start"),
    "_invalidate_session_mcp_projection": ("function", False, "b60c5fea256e", "client_mcp_array"),
    "_is_claude": ("property", False, "a6632a57e602", "client_class"),
    "_is_deepseek": ("property", False, "a6632a57e602", "client_class"),
    "_is_goose": ("property", False, "a6632a57e602", "client_class"),
    "_is_kiro": ("property", False, "a6632a57e602", "client_class"),
    "_is_opencode": ("property", False, "a6632a57e602", "client_class"),
    "_is_pi": ("property", False, "a6632a57e602", "client_class"),
    "_is_process_alive": ("function", False, "a6632a57e602", "client_lifecycle"),
    "_jsonl_tool_results": ("staticmethod", False, "db2ae4946f1c", "client_tool_events"),
    "_judge_activated_model": ("function", True, "b60c5fea256e", "client_model_selection"),
    "_judges_permission_requests": ("property", False, "a6632a57e602", "client_class"),
    "_kill_process": ("function", True, "352add48ee4e", "client_lifecycle"),
    "_last_steer_monotonic": ("data", False, "", ""),
    "_log_claude_adapter_below_floor": ("function", False, "0453b971e505", "client_claude_session"),
    "_log_declined_share": ("function", False, "276d416344a7", "client_claude_seed"),
    "_maybe_audit_tool_call": ("function", True, "7518800ca3bd", "client_tool_events"),
    "_maybe_fire_post_tool_hooks": ("function", True, "2197dd9c2867", "client_tool_events"),
    "_maybe_fire_pre_tool_hooks": ("function", True, "7518800ca3bd", "client_tool_events"),
    "_mcp_timeout_progress": ("function", False, "0295d0f4c71a", "client_mcp_array"),
    "_member_mount_withheld": ("function", False, "78c5bf8e0fe2", "client_mcp_array"),
    "_mirrorless_member_mounts": ("function", False, "286e411ff644", "client_mcp_array"),
    "_model_config_candidates": ("staticmethod", False, "f9fa998bdaf1", "client_model_selection"),
    "_model_is_unusable": ("function", False, "fa34fb34dc41", "client_model_selection"),
    "_model_registry_namespace": ("property", False, "bb418bb7ece8", "client_class"),
    "_new_session_following_substitution": ("function", True, "44dc9acb7120", "client_start"),
    "_next_req_id": ("function", False, "fa6cfecd5574", "client_transport"),
    "_note_active_run": ("function", False, "87459833bc5c", "client_turn_control"),
    "_note_agent_switched": ("function", False, "d552841f483c", "client_model_selection"),
    "_note_background_launch": ("function", False, "87459833bc5c", "client_turn_control"),
    "_note_goose_version": ("function", False, "b60c5fea256e", "client_permissions"),
    "_note_pi_adapter_version": ("function", False, "b60c5fea256e", "client_permissions"),
    "_note_pi_gate_asked": ("function", False, "87459833bc5c", "client_permissions"),
    "_note_pi_gate_denied": ("function", False, "84bed4474307", "client_permissions"),
    "_opencode_session_mcp_servers": ("function", False, "f5423acdeebc", "client_mcp_array"),
    "_parked_on_permission": ("data", False, "", ""),
    "_permission_surface_governed": ("property", False, "a6632a57e602", "client_class"),
    "_persist_advertised_models_if_changed": (
        "function",
        True,
        "b60c5fea256e",
        "client_model_selection",
    ),
    "_pi_session_mcp_servers": ("function", False, "f5423acdeebc", "client_mcp_array"),
    "_pin_claude_starting_mode": ("function", True, "4d0ed6879afd", "client_claude_session"),
    "_placed_mcp_server_names": ("function", False, "9d5ab4ea1828", "client_mcp_array"),
    "_pooled_broker_stubs": ("function", False, "7eb96c2fda05", "client_mcp_array"),
    "_pooled_mcp_servers": ("function", False, "7eb96c2fda05", "client_mcp_array"),
    "_prepare_claude_side_read_denies": ("function", True, "b60c5fea256e", "client_claude_session"),
    "_prepare_session_mcp": ("function", True, "b60c5fea256e", "client_mcp_array"),
    "_prepare_spawn_workspace": ("function", False, "b60c5fea256e", "client_spawn_env"),
    "_probe_advertised_models": ("function", True, "1ca8e8c3a438", "client_model_selection"),
    "_process_message": ("function", False, "0a2dbbba4c1b", "client_turn"),
    "_prompt_loop": ("function", "agen", "f7811e44af15", "client_turn"),
    "_prune_unstartable_mcp": ("function", True, "4dfb79f9f73b", "client_mcp_array"),
    "_push_model_config_option": ("function", True, "e60b4fb0cbfc", "client_model_selection"),
    "_read_mcp_ref_spec": ("function", False, "e7ed4605a35e", "client_mcp_array"),
    "_read_message": ("function", True, "4b94f6a71f32", "client_transport"),
    "_read_new_tool_results": ("function", True, "58ee5ea89856", "client_tool_events"),
    "_read_new_tool_results_sync": ("function", False, "58ee5ea89856", "client_tool_events"),
    "_read_prompt_response": ("function", True, "b9663bcac669", "client_turn"),
    "_read_steer_answers": ("function", True, "68f218dcd574", "client_turn_control"),
    "_reassert_adapter_resolved_model": (
        "function",
        True,
        "b60c5fea256e",
        "client_model_selection",
    ),
    "_refuse_identity_drift": ("function", True, "2dfacffa6279", "client_permissions"),
    "_registration_throttle_line": ("function", True, "f21e39ec5ffb", "client_lifecycle"),
    "_reject_unknown_server_request": ("function", True, "87459833bc5c", "client_permissions"),
    "_release_accepted_steers": ("function", False, "9a0e503e4e3e", "client_turn_control"),
    "_rename_aside_noreplace": ("staticmethod", False, "58d10dda6fc2", "client_seed_ownership"),
    "_render_claude_settings_payload": ("function", False, "bb418bb7ece8", "client_claude_seed"),
    "_republish_pi_mcp_handoff": ("function", True, "b60c5fea256e", "client_mcp_array"),
    "_reseed_after_capture": ("function", True, "b60c5fea256e", "client_claude_seed"),
    "_reset_state": ("function", False, "b60c5fea256e", "client_lifecycle"),
    "_resolve_session_mcp_servers": ("function", False, "7eb96c2fda05", "client_mcp_array"),
    "_resolve_spawn_agent_argv": ("function", False, "bb418bb7ece8", "client_spawn_env"),
    "_restore_aside_without_clobber": ("staticmethod", False, "276fb30edb8b", "client_class"),
    "_retire_liveness_state": ("function", False, "b60c5fea256e", "client_lifecycle"),
    "_retract_reseed": ("function", False, "a76c82664804", "client_claude_seed"),
    "_sandbox_init_failure": ("function", True, "cc2578c6512d", "client_lifecycle"),
    "_scrub_observed": ("staticmethod", False, "b1a850b0b873", "client_permissions"),
    "_seed_session_settings": ("function", True, "b60c5fea256e", "client_claude_seed"),
    "_seeds_local_settings": ("property", False, "a6632a57e602", "client_class"),
    "_send_error": ("function", True, "f8eeeb44db9e", "client_transport"),
    "_send_prompt": ("function", True, "bed51a75b209", "client_turn"),
    "_send_request": ("function", True, "c05412d4481e", "client_transport"),
    "_send_response": ("function", True, "79c6f4daabf0", "client_transport"),
    "_session_mcp_servers": ("function", False, "7eb96c2fda05", "client_mcp_array"),
    "_session_work_dir": ("function", True, "bb418bb7ece8", "client_lifecycle"),
    "_settings_path_bytes": ("staticmethod", False, "faf5a3c79c9e", "client_seed_ownership"),
    "_settings_path_holds": ("staticmethod", False, "d9ca34c8f528", "client_class"),
    "_settle_claude_compaction": ("function", False, "01ddf3eeedf2", "client_usage"),
    "_settle_claude_settings_seed": ("function", False, "cf5eff128c4c", "client_seed_ownership"),
    "_settle_claude_settings_seed_locked": (
        "function",
        False,
        "cf5eff128c4c",
        "client_seed_ownership",
    ),
    "_settle_codex_compaction": ("function", False, "01ddf3eeedf2", "client_usage"),
    "_settle_concurrent_steers": ("function", True, "91bb89060371", "client_turn_control"),
    "_settle_orphan_steers": ("function", True, "b60c5fea256e", "client_turn_control"),
    "_settle_stderr": ("function", True, "8fe4e298f96b", "client_lifecycle"),
    "_settle_steer_answer": ("function", False, "50405edf7b6a", "client_turn_control"),
    "_settle_steering_answer": ("function", False, "50405edf7b6a", "client_turn_control"),
    "_share_settings_seed_if_identical": ("function", False, "0b50a902117a", "client_claude_seed"),
    "_share_sibling_settings_seed": ("function", False, "0b50a902117a", "client_claude_seed"),
    "_snapshot_process_tree": ("function", True, "b60c5fea256e", "client_lifecycle"),
    "_spawn": ("function", True, "b60c5fea256e", "client_start"),
    "_start_mount_check": ("function", False, "b60c5fea256e", "client_mcp_array"),
    "_speaks_steering_request": ("property", False, "a6632a57e602", "client_class"),
    "_stdin_write_lock": ("function", False, "fd738ff11600", "client_transport"),
    "_steer_echo_event": ("function", False, "e64128661dfa", "client_turn_control"),
    "_steer_via_concurrent_prompt": ("function", True, "e2662ac6ac7a", "client_turn_control"),
    "_steer_via_request": ("function", True, "89e0aff1cade", "client_turn_control"),
    "_steer_via_steering_request": ("function", True, "e2662ac6ac7a", "client_turn_control"),
    "_steering_advertised": ("data", False, "", ""),
    "_steering_request_ready": ("function", False, "a6632a57e602", "client_turn_control"),
    "_steering_turn_req": ("data", False, "", ""),
    "_steering_waiters": ("function", False, "425ee0793b31", "client_turn_control"),
    "_steers_accepted": ("annotation", False, "", ""),
    "_store_session_config": ("function", False, "4d0ed6879afd", "client_model_selection"),
    "_sync_effort_levels": ("function", False, "b60c5fea256e", "client_model_selection"),
    "_to_thread_guarding_sandbox": ("function", True, "8dbe076fd22d", "client_lifecycle"),
    "_track_metadata": ("function", False, "c20f6a9efea3", "client_usage"),
    "_track_prompt_usage": ("function", False, "c8d5c402278d", "client_usage"),
    "_track_tool_call": ("function", False, "87459833bc5c", "client_tool_events"),
    "_track_usage_update": ("function", False, "87459833bc5c", "client_usage"),
    "_tripwire_goose_mode": ("function", True, "87459833bc5c", "client_permissions"),
    "_tripwire_pi_gate": ("function", True, "87459833bc5c", "client_permissions"),
    "_tripwire_spec_disabled_tool": ("function", False, "42c6e584f488", "client_permissions"),
    "_unclassified_tool_call": ("function", False, "2dfacffa6279", "client_permissions"),
    "_uses_advertised_model_selection": ("property", False, "a6632a57e602", "client_class"),
    "_verify_claude_project_denies_unchanged": (
        "function",
        True,
        "b60c5fea256e",
        "client_claude_session",
    ),
    "_verify_goose_routing": ("function", False, "4d0ed6879afd", "client_permissions"),
    "_wait_for_response": ("function", True, "9d13604697d8", "client_transport"),
    "_withdraw_shared_reader_lease": ("function", False, "af8a01ae1d8e", "client_seed_ownership"),
    "_withhold_is_the_only_deny_channel": ("function", False, "a6632a57e602", "client_mcp_array"),
    "_write_claude_local_settings": ("function", False, "b60c5fea256e", "client_claude_seed"),
    "_write_request_bounded": ("function", True, "4ea88874f686", "client_transport"),
    "_write_response_bounded": ("function", True, "1aa0c9bf750c", "client_transport"),
    "_write_steer_cancel": ("function", True, "b60c5fea256e", "client_turn_control"),
    "acp_config_options": ("property", False, "3b4687b0c546", "client_class"),
    "agent_version": ("property", False, "bb418bb7ece8", "client_class"),
    "answer_elicitation": ("function", True, "2fcb1ac189fa", "client_elicitation"),
    "approve_tool": ("function", True, "d9f85d56922f", "client_permissions"),
    "authored_spec_declares_zero_tools": ("function", False, "a6632a57e602", "client_mcp_array"),
    "available_models": ("function", False, "92d6193dbe19", "client_model_selection"),
    "backend": ("property", False, "bb418bb7ece8", "client_class"),
    "background_launch": ("function", False, "d85112f1c4e4", "client_turn_control"),
    "cancel_pending_elicitations": ("function", True, "fa6cfecd5574", "client_elicitation"),
    "cancel_session": ("function", True, "0e6ce85f9f06", "client_turn_control"),
    "capability_projection_gaps": ("property", False, "9d5ab4ea1828", "client_class"),
    "command_result": ("function", True, "ef1b0634d9c5", "client_turn_control"),
    "confirm_member_projection": ("function", True, "b60c5fea256e", "client_class"),
    "effective_spec_declares_zero_tools": ("function", False, "b70ef8096bd5", "client_mcp_array"),
    "ensure_ready": ("function", True, "b60c5fea256e", "client_start"),
    "exit_code": ("property", False, "8dc15be1dfda", "client_class"),
    "get_valid_effort_levels": ("function", False, "6806e867af4e", "client_model_selection"),
    "has_active_turn": ("function", False, "a6632a57e602", "client_turn_control"),
    "has_unfinished_turn": ("function", False, "a6632a57e602", "client_turn_control"),
    "is_process_alive": ("function", False, "a6632a57e602", "client_class"),
    "is_ready": ("property", False, "a6632a57e602", "client_class"),
    "is_responsive": ("function", False, "4b2fc7c136b8", "client_lifecycle"),
    "last_steer_monotonic": ("property", False, "59162e39f6f1", "client_class"),
    "loaded_capability_template": ("property", False, "bb418bb7ece8", "client_class"),
    "mcp_session_report": ("function", False, "ac691973b9b4", "client_mcp_array"),
    "member_dispatch_mounted": ("property", False, "16180c63d5e6", "client_class"),
    "pop_pending_oauth_requests": ("function", False, "92d6193dbe19", "client_mcp_array"),
    "process_instance": ("property", False, "bb418bb7ece8", "client_class"),
    "process_tree_confirmed_dead": ("property", False, "a6632a57e602", "client_class"),
    "reclaim": ("function", False, "b60c5fea256e", "client_lifecycle"),
    "refresh_available_models": ("function", True, "92d6193dbe19", "client_model_selection"),
    "refresh_process_tree": ("function", True, "b60c5fea256e", "client_lifecycle"),
    "reject_tool": ("function", True, "84bed4474307", "client_permissions"),
    "rekey": ("function", False, "d58f70d0bd6a", "client_lifecycle"),
    "release_settings_seed_claim": ("function", False, "b60c5fea256e", "client_seed_ownership"),
    "resumed": ("property", False, "a6632a57e602", "client_class"),
    "send_command": ("function", True, "b66667c61d53", "client_turn_control"),
    "send_message": ("function", True, "c63d2b398c21", "client_turn"),
    "send_message_stream": ("function", "agen", "dbc2a1962f65", "client_turn"),
    "session_identity_token": ("property", False, "bb418bb7ece8", "client_class"),
    "set_config_option": ("function", True, "d17f94a912fd", "client_model_selection"),
    "set_fork_request": ("function", False, "09f226933c39", "client_lifecycle"),
    "set_model": ("function", True, "bcd0cd3aa159", "client_model_selection"),
    "set_resume_session_id": ("function", False, "df97aaea4e38", "client_lifecycle"),
    "settings_withheld_cause": ("function", False, "bb418bb7ece8", "client_seed_ownership"),
    "shutdown": ("function", True, "b60c5fea256e", "client_lifecycle"),
    "spec_zero_tools": ("property", False, "a6632a57e602", "client_class"),
    "steer": ("function", True, "b0a837a37097", "client_turn_control"),
    "steer_needs_loss_recovery": ("property", False, "a6632a57e602", "client_class"),
    "stream_command": ("function", "agen", "07d1ae72fdc6", "client_turn_control"),
    "stream_events": ("function", "agen", "ed7ccfb7b96f", "client_turn"),
    "supports_config_option": ("function", False, "b86d69c93890", "client_model_selection"),
    "supports_refusal_steer": ("property", False, "a6632a57e602", "client_class"),
    "supports_steer": ("property", False, "a6632a57e602", "client_class"),
    "touch_activity": ("function", False, "b60c5fea256e", "client_lifecycle"),
    "turn_finished_cleanly": ("function", False, "a6632a57e602", "client_turn_control"),
    "wait_for_compaction": ("function", True, "1d8ef804d52e", "client_usage"),
    "wait_for_declared_mcp": ("function", True, "921c3727b83c", "client_mcp_array"),
    "wait_turn_done": ("function", True, "48da118c69ff", "client_turn_control"),
    "work_scratch_dir": ("property", False, "d921fd8d443d", "client_class"),
}

#: Class-level annotations the split adds: fields first set by a member an owner defines,
#: declared in the class so type checkers know them. An annotation creates no attribute.
_DECLARED = frozenset(
    {
        "_claude_shipped_restrictions",
        "_entitlement_probe_inflight",
        "_member_dispatch_mounted",
        "_session_mcp_excludes_local",
        "_session_mcp_inline_settings",
        "_stdin_lock",
        "_steering_waiter_map",
        "_windows_tree_cleanup_failed",
    }
)

_PACKAGE = "kiro_crew.acp"


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in acp_client._CLIENT_OWNERS]


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return hashlib.sha256(str(sig.replace(parameters=params)).encode()).hexdigest()[:12]


def _member_function(name: str) -> Any:
    value = vars(AcpClient)[name]
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
        for name in vars(AcpClient)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    present |= set(AcpClient.__dict__.get("__annotations__", {}))
    assert sorted(present ^ (set(_MEMBERS) | _DECLARED)) == []
    assert not _DECLARED & set(vars(AcpClient))


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    if kind in ("data", "annotation"):
        assert (name in vars(AcpClient)) is (kind == "data")
        return
    value = vars(AcpClient)[name]
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
    if fn.__qualname__ != f"AcpClient.{name}":  # bound from an owner, not class-resident
        assert vars(sys.modules[f"{_PACKAGE}.{owner}"])[name] is fn


def test_every_owner_lists_in_the_facade_and_holds_its_members() -> None:
    homes = {owner for _kind, _c, _d, owner in _MEMBERS.values() if owner and owner != "client"}
    listed = {name.rsplit(".", 1)[-1] for name in acp_client._CLIENT_OWNERS}
    assert homes <= listed


@pytest.mark.parametrize("owner", acp_client._CLIENT_OWNERS)
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


@pytest.mark.parametrize("owner", acp_client._CLIENT_OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    tree = _tree(sys.modules[owner])
    assert {fn.name: _class_only_constructs(fn) for fn in _self_functions(tree, owner)} == {
        fn.name: [] for fn in _self_functions(tree, owner)
    }


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    """Lines that import the facade (``kiro_crew.acp.client``) outside a ``TYPE_CHECKING`` block."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            guarded.update(id(inner) for inner in ast.walk(node))
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == f"{_PACKAGE}.client" or (
                node.module == _PACKAGE and any(a.name == "client" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == f"{_PACKAGE}.client" for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.acp.client import AcpClient\n"
        "def f():\n    from kiro_crew.acp.client import x\n"
        "import kiro_crew.acp.client\n"
        "from kiro_crew.acp import client\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize("owner", acp_client._CLIENT_OWNERS)
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
    return written - set(_MEMBERS), read - written - set(_MEMBERS)


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


@pytest.mark.parametrize("owner", acp_client._CLIENT_OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module), owner))
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


@pytest.mark.parametrize("owner", acp_client._CLIENT_OWNERS)
def test_owners_log_under_the_client_logger(owner: str) -> None:
    module = sys.modules[owner]
    assert getattr(module, "logger", acp_client.logger) is acp_client.logger


@pytest.mark.parametrize("owner", acp_client._CLIENT_OWNERS)
def test_a_moved_class_keeps_the_facade_as_its_module(owner: str) -> None:
    """Error chains and tracebacks render a class as ``module.qualname``; a class that
    moved to an owner keeps naming the path callers import it from."""
    module = sys.modules[owner]
    defined = [n.name for n in _tree(module).body if isinstance(n, ast.ClassDef)]
    assert {name: vars(module)[name].__module__ for name in defined} == {
        name: acp_client.__name__ for name in defined
    }


def test_the_kiro_trust_error_names_the_facade_in_an_error_chain() -> None:
    from kiro_crew.subagent import _describe_exception

    try:
        try:
            raise acp_client._KiroExecutableTrustError("bytes not approved")
        except acp_client._KiroExecutableTrustError as exc:
            raise RuntimeError("spawn refused") from exc
    except RuntimeError as outer:
        text = _describe_exception(outer)
    assert text == (
        "RuntimeError: spawn refused <- caused by "
        "kiro_crew.acp.client._KiroExecutableTrustError: bytes not approved"
    )


def test_the_bound_function_scans_include_every_static_method() -> None:
    statics = {
        name: f"{_PACKAGE}.{home}"
        for name, (kind, _, _, home) in _MEMBERS.items()
        if kind == "staticmethod"
        and _member_function(name).__qualname__ != f"AcpClient.{name}"  # bound, not resident
    }
    assert statics
    scanned = {
        fn.name
        for owner in set(statics.values())
        for fn in _self_functions(_tree(sys.modules[owner]), owner)
    }
    assert set(statics) <= scanned


def _owner_imports(tree: ast.Module) -> set[str]:
    """The client owner modules *tree* imports, by dotted name."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(f"{_PACKAGE}.client_"):
                found.add(node.module)
            elif node.module == _PACKAGE:
                found.update(
                    f"{_PACKAGE}.{a.name}" for a in node.names if a.name.startswith("client_")
                )
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith(f"{_PACKAGE}.client_"))
    return found


def test_the_owner_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from kiro_crew.acp.client_timeouts import X\n"
        "from kiro_crew.acp import client_spawn_env as _se, launch\n"
        "import kiro_crew.acp.client_permissions\n"
    )
    assert _owner_imports(tree) == {
        "kiro_crew.acp.client_timeouts",
        "kiro_crew.acp.client_spawn_env",
        "kiro_crew.acp.client_permissions",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_CLIENT_OWNERS`` lists the owners lowest layer first: an owner imports only
    owners earlier in the tuple, so the order is the import order."""
    owners = acp_client._CLIENT_OWNERS
    later = {
        owner: sorted(_owner_imports(_tree(sys.modules[owner])) - set(owners[:i]))
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


# --------------------------------------------------------------------------- #
# A write through the facade reaches every owner that reads the name.
# --------------------------------------------------------------------------- #

#: Names the launch capture patches through the facade from a table, which the patch scan
#: reads as ``<dynamic>`` (``test/acp_launch_capture.py``).
_TABLE_PATCHED = frozenset(
    {
        "_resolve_spawn_env",
        "_apply_pod_home_remap",
        "scrub_agent_subprocess_env",
        "cgroup_scope_argv",
        "apply_pod_bundle_spawn",
        "wrap_argv_async",
        "_codex_acp_argv_cache",
    }
)


def _patched_through_the_facade() -> set[str]:
    import test_acp_refactor_facade as facade_test

    return {
        name
        for _path, _line, _where, facade, name in facade_test._facade_patches()
        if facade == acp_client.__name__ and name != "<dynamic>"
    } | _TABLE_PATCHED


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
    assert {"logger", "_READ_TIMEOUT", "agent_spec_snapshot"} <= patched  # the scan saw them
    assert _reach_gaps(patched, acp_client._MIRROR) == []


def test_the_reach_scan_can_fail() -> None:
    index = acp_client._MIRROR
    owner = "kiro_crew.acp.client_permissions"
    holders = dict(index.holders)
    holders["logger"] = tuple(h for h in holders["logger"] if h != owner)
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps({"logger"}, acp_client._MIRROR) == []
    assert _reach_gaps({"logger"}, blind) == [f"{owner}.logger"]


def test_the_mirror_indexes_one_object_per_name_and_no_legacy_forward() -> None:
    index = acp_client._MIRROR
    assert index.excluded == ()
    assert index.divergent() == ()
    assert set(index.holders).isdisjoint(acp_client._EXPORTS)
    # The legacy table keeps single-owner forwarding, and the mirror serves nothing else:
    # every name the facade bound before is still bound on it.
    assert dict(index.forwards) == acp_client._EXPORTS
    assert dict(index.exports) == {}


def test_every_forwarded_read_asks_the_import_system_for_its_owner() -> None:
    import importlib
    from unittest import mock

    assert acp_client._MIRROR.forward_reads == "import_module"
    name = "resolve_usable_model"
    owner = acp_client._EXPORTS[name]
    calls: list[str] = []
    real_import = importlib.import_module

    def counting(target: str, package: str | None = None) -> ModuleType:
        calls.append(target)
        return real_import(target, package)

    with mock.patch.object(importlib, "import_module", counting):
        first = getattr(acp_client, name)
        second = getattr(acp_client, name)
    assert calls == [owner, owner]
    assert first is second is getattr(sys.modules[owner], name)


def test_the_facade_resolver_declines_every_name_the_mirror_serves() -> None:
    resolver = vars(acp_client)["__getattr__"]
    served = sorted(acp_client._MIRROR.served)
    assert served
    answered = []
    for name in served:
        try:
            resolver(name)
        except AttributeError:
            continue
        answered.append(name)
    assert answered == []


def test_the_facade_resolver_answers_a_name_added_after_the_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    monkeypatch.setitem(acp_client._EXPORTS, "dumps", json.__name__)
    assert vars(acp_client)["__getattr__"]("dumps") is json.dumps


def test_install_refuses_a_facade_resolver_that_answers_a_served_name() -> None:
    from kiro_crew import _facade_mirror

    def answering(name: str) -> object:
        return object()

    namespace = dict(vars(acp_client))
    namespace.pop(_facade_mirror.INDEX_ATTRIBUTE, None)
    namespace["__getattr__"] = answering
    stand_in = ModuleType(acp_client.__name__)
    stand_in.__dict__.update(namespace)
    with pytest.raises(RuntimeError, match=r"its own module-level __getattr__ answers"):
        _facade_mirror.install(
            stand_in,
            acp_client._CLIENT_OWNERS,
            excluded=(),
            forwards=acp_client._index_exports(),
            surface=frozenset(namespace),
        )
    assert _facade_mirror.INDEX_ATTRIBUTE not in vars(stand_in)


def test_a_patch_through_the_facade_lands_on_the_owner_and_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = sys.modules["kiro_crew.acp.client_timeouts"]
    before = owner._READ_TIMEOUT
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(acp_client, "_READ_TIMEOUT", sentinel)
        assert owner._READ_TIMEOUT is sentinel
        assert acp_client._READ_TIMEOUT is sentinel
    assert owner._READ_TIMEOUT is before
    assert acp_client._READ_TIMEOUT is before


# --------------------------------------------------------------------------- #
# The class lives in ``client_class``; callers, pickles and reprs still name the facade.
# --------------------------------------------------------------------------- #

#: ``typing.get_type_hints(AcpClient.__init__)`` before the class left ``client.py``.
_INIT_HINTS = {
    "acp_backend": "<class 'str'>",
    "agent": "<class 'str'>",
    "audit_source": "str | None",
    "channel_id": "str | None",
    "extra_env": "dict[str, str] | None",
    "mcp_gateway_overlay": "str | pathlib.Path | None",
    "mcp_gateway_socket": "str | pathlib.Path | None",
    "model": "str | None",
    "permission_mode": "str | None",
    "sandbox_mode": "<class 'str'>",
    "session_key": "str | None",
    "shared_scratch": "pathlib.Path | None",
    "side_read_only": "<class 'bool'>",
    "work_dir": "str | pathlib.Path | None",
}


def _class_resident() -> dict[str, Any]:
    """The members whose body is still in the class statement, by name."""
    found = {}
    for name in vars(AcpClient):
        fn = _member_function(name) if name in _MEMBERS else None
        if inspect.isfunction(fn) and fn.__qualname__ == f"AcpClient.{name}":
            found[name] = fn
    return found


def test_the_class_keeps_the_facade_as_its_module_name_and_pickle_path() -> None:
    import importlib

    from kiro_crew.acp import client_class

    assert AcpClient is client_class.AcpClient is acp_client.AcpClient
    assert (AcpClient.__module__, AcpClient.__qualname__) == (acp_client.__name__, "AcpClient")
    assert inspect.getdoc(AcpClient)
    # A pickle stores a class as module + qualname and loads it back by that lookup.
    assert (
        getattr(importlib.import_module(AcpClient.__module__), AcpClient.__qualname__) is AcpClient
    )


def test_every_class_resident_member_resolves_its_type_hints() -> None:
    import typing

    resident = _class_resident()
    assert {"__init__", "backend", "_restore_aside_without_clobber"} <= set(resident)
    for fn in resident.values():
        typing.get_type_hints(fn)
    init = typing.get_type_hints(AcpClient.__init__)
    assert {name: repr(hint) for name, hint in init.items()} == _INIT_HINTS
    assert typing.get_type_hints(vars(AcpClient)["backend"].fget) == {"return": str}


def test_a_facade_patch_of_the_class_reaches_its_class_resident_statics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The class's own statics call each other through the ``AcpClient`` global, which a
    patch of ``kiro_crew.acp.client.AcpClient`` still replaces for them."""
    calls: list[tuple[Path, Path]] = []

    class StandIn(AcpClient):
        @staticmethod
        def _rename_aside_noreplace(aside: Path, path: Path) -> bool:
            calls.append((aside, path))
            return True

    monkeypatch.setattr(acp_client, "AcpClient", StandIn)
    aside, path = tmp_path / "aside", tmp_path / "settings.json"
    assert AcpClient._restore_aside_without_clobber(aside, path, (0, 0)) is True
    assert calls == [(aside, path)]


# --------------------------------------------------------------------------- #
# The session start reads ``_resolve_spawn_env``, which the facade defines and fills in.
# --------------------------------------------------------------------------- #


def test_the_facade_fills_the_session_starts_spawn_env_resolver() -> None:
    from kiro_crew.acp import client_start

    assert client_start._resolve_spawn_env is vars(acp_client)["_resolve_spawn_env"]
    assert client_start._resolve_spawn_env.__module__ == acp_client.__name__
    spawn = inspect.unwrap(client_start._spawn)
    codes, names = [spawn.__code__], set()
    while codes:  # the read sits in a nested function of the spawn
        code = codes.pop()
        names.update(code.co_names)
        codes.extend(c for c in code.co_consts if inspect.iscode(c))
    assert "_resolve_spawn_env" in names
    assert spawn.__globals__ is vars(client_start)


def test_a_facade_patch_of_the_spawn_env_resolver_reaches_the_session_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.acp import client_start

    def stand_in(env: dict[str, str], *, kiro_api_key: bool = False) -> dict[str, str]:
        return env

    before = client_start._resolve_spawn_env
    with monkeypatch.context() as patch:
        patch.setattr(acp_client, "_resolve_spawn_env", stand_in)
        assert client_start._spawn.__globals__["_resolve_spawn_env"] is stand_in
    assert client_start._resolve_spawn_env is before


def _imports_the_session_start(tree: ast.Module) -> bool:
    return f"{_PACKAGE}.client_start" in _owner_imports(tree)


def test_only_the_facade_and_the_class_import_the_session_start() -> None:
    """Nothing reaches ``client_start`` before the facade has filled it: the package
    imports the facade first, and the class module is the only other importer."""
    src = Path(acp_client.__file__ or "").resolve().parents[1]
    importers = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "client_start" in path.read_text(encoding="utf-8")
        and _imports_the_session_start(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert importers == ["acp/client.py", "acp/client_class.py"]
    package = ast.parse((src / "acp" / "__init__.py").read_text(encoding="utf-8"))
    first = next(n for n in package.body if isinstance(n, (ast.Import, ast.ImportFrom)))
    assert isinstance(first, ast.ImportFrom) and first.module == acp_client.__name__


def test_the_session_start_import_scan_can_fail() -> None:
    assert _imports_the_session_start(ast.parse("from kiro_crew.acp import client_start\n"))
    assert _imports_the_session_start(ast.parse("from kiro_crew.acp.client_start import X\n"))
    assert not _imports_the_session_start(ast.parse("from kiro_crew.acp import client\n"))
