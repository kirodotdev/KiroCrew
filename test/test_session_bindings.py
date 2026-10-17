"""``SessionManager`` keeps every member it had, and each is bound from the module that owns it.

The manager's behaviour lives in the ``kiro_crew.session_parts`` owner modules: each
member's body is a module-level function there whose first parameter is ``self``, and the
class binds it by name (``m = _owner.m``, or ``staticmethod(_owner.f)``); each state view
is a ``StateProperty`` the owner builds from its getter and setter. The instance state
stays on the instance, so tests, ``SessionManager.__new__`` builds and every collaborator
see the same object they always did. This file pins the binding contract:

* the frozen member inventory -- every member, its kind, whether it is a coroutine, its
  signature (the ``self`` annotation aside) and the module that owns it;
* each bound member IS its owner's object, and no owner keeps an unbound ``self`` function;
* each state view is a real ``property`` whose docstring and accessor bodies are BASE's,
  and ``inspect.getattr_static`` / ``create_autospec`` read the class as they did;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade at run time, and each imports only owners listed before it;
* each owner's docstring names the instance fields its members write and read;
* every owner logs under ``kiro_crew.session``, and a moved class keeps that module name;
* ``kiro_crew._facade_mirror`` serves exactly the moved names the facade does not bind, and
  a write through ``kiro_crew.session`` reaches every owner that reads the name;
* ``from kiro_crew.session import *`` binds the names it bound before the split.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

import pytest

from kiro_crew import session as session_mod
from kiro_crew.session import SessionManager
from kiro_crew.session_parts.state_views import StateProperty

pytestmark = pytest.mark.xdist_group(name="tree_scan_session_bindings")

#: Member -> (kind, coroutine, signature digest, owning module). Frozen at the split: the
#: kind, coroutine flag and signature are BASE's; the owner is where the member lives now
#: (``session`` is the facade, any other name a ``kiro_crew.session_parts`` module).
_MEMBERS: dict[str, tuple[str, object, str, str]] = {
    "_CONFIG_SECTIONS": ("data", False, "", "config_apply"),
    "_FACTORY_CONFIG_PATHS": ("data", False, "", "config_apply"),
    "_POOL_DISCARD_TIMEOUT": ("data", False, "", "runtimes"),
    "_POOL_HEALTH_INTERVAL": ("data", False, "", "runtimes"),
    "__init__": ("function", False, "6e578750eb89", "session"),
    "_active_dashboard_slots": ("property", False, "9b594013b706", "state_views"),
    "_advance_session_generation": ("function", False, "028659ce42ac", "claims"),
    "_agent_model_cache": ("data", False, "", "session"),
    "_allocation_boundary": ("function", False, "4e46720466e2", "claims"),
    "_allocation_deps": ("function", False, "36ac72af9b5c", "session"),
    "_append_companion_runtime_rows": ("function", False, "d30b1c0d706e", "claims"),
    "_bg_backend_supports_runtime": ("function", False, "a6632a57e602", "runtimes"),
    "_bg_drain_reap_hook": ("function", True, "b60c5fea256e", "sweeps"),
    "_bg_runtime": ("property", False, "c3825669b663", "state_views"),
    "_bg_runtime_lock": ("property", False, "fd738ff11600", "state_views"),
    "_claim_from_pool": ("function", False, "3242151a2361", "runtimes"),
    "_cleanup_boundary": ("function", False, "20637b811905", "sweeps"),
    "_cleanup_deps": ("function", False, "d4cf5168171e", "sweeps"),
    "_cleanup_loop": ("function", True, "b60c5fea256e", "sweeps"),
    "_cleanup_state_boundary": ("function", False, "5aff8572f6bd", "state_views"),
    "_cleanup_task": ("property", False, "7c97074a9b09", "state_views"),
    "_closing": ("property", False, "a6632a57e602", "state_views"),
    "_compact_cooldown_until": ("property", False, "b00f42b34605", "state_views"),
    "_compact_in_place": ("function", True, "1c68c9ed8dbb", "turn_context"),
    "_compact_pending_verdict": ("property", False, "b00f42b34605", "state_views"),
    "_compact_session": ("function", True, "7aae01edaa22", "turn_context"),
    "_compacting": ("property", False, "67a56d539800", "state_views"),
    "_compaction_gate_decision": ("function", False, "5f19173cf83a", "turn_context"),
    "_companion_runtime_pids": ("function", False, "6e67e49bb5fd", "runtimes"),
    "_configured_bg_backend": ("function", False, "bb418bb7ece8", "runtimes"),
    "_configured_bg_backend_raw": ("function", False, "f21e39ec5ffb", "runtimes"),
    "_consume_persisted_replay_suppression": ("function", False, "bf6b61090441", "turn_context"),
    "_continuable_fallback": ("property", False, "bc1a086eaf85", "state_views"),
    "_continuable_keys": ("property", False, "67a56d539800", "state_views"),
    "_detach_bg_runtime_locked": ("function", True, "d2638a267916", "runtimes"),
    "_discard_pool_provider": ("function", True, "7ee0e56a00ce", "runtimes"),
    "_dispatch_hard_kill": ("staticmethod", False, "23ef798c6535", "runtimes"),
    "_displace_bg_runtime_locked": ("function", True, "b6976764a6bf", "runtimes"),
    "_drain_and_claim": ("function", True, "f7fc1af9bfcd", "runtimes"),
    "_draining_bg_runtimes": ("property", False, "eb482a8b1e3b", "state_views"),
    "_draining_subagent_runtimes": ("property", False, "eb482a8b1e3b", "state_views"),
    "_eager_respawn": ("function", True, "958babeb61c8", "teardown"),
    "_ensure_background": ("function", True, "ffb4efadb2a1", "runtimes"),
    "_ensure_cleanup_task": ("function", False, "b60c5fea256e", "sweeps"),
    "_evict_stale_session": ("function", True, "70690b0be258", "claims"),
    "_expire_idle": ("function", True, "0bb4579ef3e4", "sweeps"),
    "_expire_idle_hook": ("function", True, "b60c5fea256e", "sweeps"),
    "_fill_warm_pool": ("function", True, "b60c5fea256e", "runtimes"),
    "_fire_compact_callback": ("function", True, "75b404bd8c03", "turn_context"),
    "_fire_recycle_callback": ("function", True, "d9b2aabefef9", "teardown"),
    "_fold_key": ("function", False, "f1d40e624ae4", "claims"),
    "_get_or_bootstrap_run_runtime": ("function", True, "2837cd419786", "runtimes"),
    "_get_session_agent": ("function", False, "48f8c3736da9", "claims"),
    "_has_allocation_reservation": ("function", False, "1eda252b53bd", "claims"),
    "_has_attached_subagents": ("function", False, "9c6eda911dd8", "sweeps"),
    "_has_pending_injection": ("function", False, "1eda252b53bd", "sweeps"),
    "_identity_sweep_lock": ("property", False, "fd738ff11600", "state_views"),
    "_idle_sweep_enabled": ("property", False, "a6632a57e602", "state_views"),
    "_idle_timeout": ("property", False, "fa6cfecd5574", "state_views"),
    "_in_flight_pids": ("function", False, "6e67e49bb5fd", "runtimes"),
    "_is_continuable_key": ("function", False, "bcd6de7f056b", "claims"),
    "_judge_compact_effect": ("function", False, "59f4f14b45c9", "turn_context"),
    "_last_pycache_gc": ("property", False, "c2eec8a41739", "state_views"),
    "_lifecycle_boundary": ("function", False, "ee21e2b9c346", "teardown"),
    "_lifecycle_deps": ("function", False, "605abb6b7c5e", "session"),
    "_lifecycle_state_boundary": ("function", False, "4b08dece16e8", "state_views"),
    "_lock": ("property", False, "fd738ff11600", "state_views"),
    "_mark_identity_epoch": ("function", False, "b60c5fea256e", "runtimes"),
    "_on_compacted": ("property", False, "863b11bcd58c", "state_views"),
    "_on_config_change": ("function", True, "999e37524dc3", "config_apply"),
    "_on_recycled": ("property", False, "67238bb9d853", "state_views"),
    "_origin_links": ("property", False, "267307c65578", "state_views"),
    "_orphan_mcp_hook": ("function", True, "b60c5fea256e", "sweeps"),
    "_parent_runtime_kwargs": ("function", False, "bb45049dc0f8", "claims"),
    "_pool_agent": ("property", False, "bb418bb7ece8", "state_views"),
    "_pool_cwd": ("property", False, "bb418bb7ece8", "state_views"),
    "_pool_fill_lock": ("property", False, "fd738ff11600", "state_views"),
    "_pool_health_loop": ("function", True, "b60c5fea256e", "runtimes"),
    "_pool_health_task": ("property", False, "7c97074a9b09", "state_views"),
    "_pool_pids": ("function", False, "6e67e49bb5fd", "runtimes"),
    "_pool_size": ("property", False, "fa6cfecd5574", "state_views"),
    "_pool_started": ("property", False, "a6632a57e602", "state_views"),
    "_pool_sweep_pids": ("property", False, "6e67e49bb5fd", "state_views"),
    "_pool_ttl_secs": ("property", False, "fa6cfecd5574", "state_views"),
    "_provider_backed_bg_session": ("function", True, "dcb1875e1d16", "runtimes"),
    "_reacquire_and_validate": ("function", True, "1759a1167034", "claims"),
    "_reap_agent_scopes_hook": ("function", True, "b60c5fea256e", "sweeps"),
    "_reap_drained_bg_runtimes_locked": ("function", True, "b60c5fea256e", "runtimes"),
    "_reap_idle_stale_bg_runtime": ("function", True, "a6632a57e602", "runtimes"),
    "_rebind_live_watchdogs": ("function", True, "ade7df8164da", "session"),
    "_record_pool_decision": ("function", False, "3f2c2ecce79a", "runtimes"),
    "_recycle_held": ("function", True, "77006629a104", "turn_context"),
    "_recycle_unmanaged": ("function", True, "1c68c9ed8dbb", "turn_context"),
    "_recycling": ("property", False, "5c9a1a8aab23", "state_views"),
    "_registry_state": ("function", False, "21fce5dd2c88", "state_views"),
    "_reset_still_critical": ("function", True, "14f70d097513", "turn_context"),
    "_resolve_agent_model": ("staticmethod", False, "f2204edfe0d1", "session"),
    "_retire_kiro_bg_runtime": ("function", True, "b3a488b20d12", "teardown"),
    "_retire_kiro_subagent_runtimes": ("function", True, "b3a488b20d12", "teardown"),
    "_retire_kiro_warm_pool": ("function", True, "a6632a57e602", "runtimes"),
    "_retire_stale_backend_bg_runtime": ("function", True, "b60c5fea256e", "runtimes"),
    "_rss_max_mb": ("property", False, "fa6cfecd5574", "state_views"),
    "_rss_threshold_check": ("function", True, "b60c5fea256e", "sweeps"),
    "_safe_cleanup": ("function", True, "76a7c030508f", "claims"),
    "_schedule_replenish": ("function", False, "b60c5fea256e", "runtimes"),
    "_send_abort_for_session": ("function", True, "a6707a152144", "teardown"),
    "_sessions": ("property", False, "5c9a1a8aab23", "state_views"),
    "_settle_compact_cooldown": ("function", False, "782a54a66452", "turn_context"),
    "_spawn_in_flight": ("function", False, "4b576121a346", "claims"),
    "_start_sem": ("property", False, "1f1197566c49", "state_views"),
    "_starting_pids": ("property", False, "d657ba783a38", "state_views"),
    "_stuck_reported": ("property", False, "b00f42b34605", "state_views"),
    "_stuck_turn_check": ("function", True, "b60c5fea256e", "sweeps"),
    "_subagent_runtime_locks": ("property", False, "ac58df2ab69b", "state_views"),
    "_subagent_runtimes": ("property", False, "43d7bd6f4674", "state_views"),
    "_suppress_replay": ("property", False, "67a56d539800", "state_views"),
    "_sweep_warm_pool_once": ("function", True, "b60c5fea256e", "runtimes"),
    "_sync_autocompact_pct": ("function", False, "b60c5fea256e", "config_apply"),
    "_trigger_compaction": ("function", False, "ea34ffbc5059", "turn_context"),
    "_update_pause_owned": ("property", False, "a6632a57e602", "state_views"),
    "_warm_pool": ("property", False, "c1bd5834bcdd", "state_views"),
    "_watchdog": ("property", False, "524719d11e5f", "state_views"),
    "absorb_orphaned_release": ("function", False, "1eda252b53bd", "teardown"),
    "active_providers": ("function", False, "6d42d4fe7cc2", "claims"),
    "admission_closed": ("property", False, "a6632a57e602", "state_views"),
    "adopt_turn": ("function", False, "958babeb61c8", "teardown"),
    "aflush": ("function", True, "b60c5fea256e", "links"),
    "agent_autocompact_default": ("function", False, "486b646f770f", "turn_context"),
    "allocation_predecessor": ("function", False, "f1d40e624ae4", "claims"),
    "allocation_requested_model": ("function", False, "f1d40e624ae4", "claims"),
    "any_active_turn": ("function", False, "a6632a57e602", "claims"),
    "await_replay_gap": ("function", True, "958babeb61c8", "teardown"),
    "aws_profile_differs_from_parent": ("function", False, "76593e568eac", "claims"),
    "aws_profile_split_possible": ("function", False, "3780bd272b7a", "claims"),
    "batched_save": ("function", False, "08ba78a8a6e0", "links"),
    "begin_turn": ("function", False, "958babeb61c8", "claims"),
    "cancel_current": ("function", True, "9859e2116ea5", "teardown"),
    "cancel_owned_turn": ("function", True, "86bfc9daa5fd", "teardown"),
    "cancel_queued": ("function", False, "50fe52427d8c", "claims"),
    "capability_runtime_view": ("function", False, "de139dca2f5d", "claims"),
    "channel_key_for_stem": ("function", False, "99b8e84a3315", "links"),
    "check_context_usage": ("function", False, "52ae9aeaa52b", "turn_context"),
    "clear_mirror_link": ("function", False, "e17394b73cfb", "links"),
    "clear_mirror_link_if": ("function", False, "1dd34978d404", "links"),
    "clear_mirror_links_at": ("function", False, "daeb9edbe83d", "links"),
    "clear_queue": ("function", False, "1c7c5164c922", "claims"),
    "clear_slack_link": ("function", False, "1eda252b53bd", "links"),
    "clear_slack_link_if": ("function", False, "c9d53f91d7af", "links"),
    "close_all": ("function", True, "9679ccba1fac", "teardown"),
    "close_replay_gap": ("function", False, "958babeb61c8", "teardown"),
    "commit_provider_switch_replay_sid": ("function", False, "1eda252b53bd", "session"),
    "compact_if_needed": ("function", True, "f1d40e624ae4", "turn_context"),
    "compact_wait_budget_secs": ("function", False, "59162e39f6f1", "turn_context"),
    "compaction_due_after_turn": ("function", False, "9e38ffe0e60c", "turn_context"),
    "compaction_waits_for_turn": ("function", False, "1eda252b53bd", "turn_context"),
    "consume_first_turn_history_owed": ("function", False, "1eda252b53bd", "turn_context"),
    "consume_needs_reinjection": ("function", False, "1eda252b53bd", "turn_context"),
    "consume_provider_switch_replay": ("function", False, "1eda252b53bd", "turn_context"),
    "consume_replay_suppression": ("function", False, "1eda252b53bd", "turn_context"),
    "conversation_provider": ("function", False, "f1d40e624ae4", "claims"),
    "count": ("property", False, "fa6cfecd5574", "state_views"),
    "dequeue": ("function", False, "bf5dca345c3c", "claims"),
    "destroy": ("function", True, "958babeb61c8", "teardown"),
    "destroy_if": ("function", True, "b2e16ab8f2dd", "teardown"),
    "detach_queue": ("function", False, "197b82a5019f", "claims"),
    "discard_conversation": ("function", True, "b225e6bcbd95", "teardown"),
    "drain_active_turns": ("function", True, "5944c6af44b2", "teardown"),
    "drain_all_providers": ("function", True, "f5423acdeebc", "teardown"),
    "drain_warm_pool": ("function", True, "f5423acdeebc", "runtimes"),
    "effective_autocompact_pct": ("function", False, "c61b0946c307", "turn_context"),
    "end_children_for": ("function", True, "958babeb61c8", "teardown"),
    "ending_key": ("function", False, "e1e3936a7475", "claims"),
    "enqueue": ("function", False, "d3c6da093136", "claims"),
    "fence_update_restart": ("function", False, "a6632a57e602", "claims"),
    "final_drain_started": ("property", False, "a6632a57e602", "state_views"),
    "find_key_by_sid": ("function", False, "7f231b93851d", "links"),
    "find_mirror_sessions": ("function", False, "7b7ff4cef714", "links"),
    "first_turn_history_owed_pending": ("function", False, "1eda252b53bd", "turn_context"),
    "flag_identity_stamp_mismatches": ("function", True, "946212e80718", "teardown"),
    "forget_conversation": ("function", False, "4b576121a346", "claims"),
    "forget_conversation_if_sid": ("function", False, "70e3d61c5764", "claims"),
    "get_agent": ("function", False, "f1d40e624ae4", "claims"),
    "get_agent_selection": ("function", False, "b3496bff0391", "claims"),
    "get_approval_policy": ("function", False, "f1d40e624ae4", "claims"),
    "get_bg_session": ("function", True, "c8c03f90e44b", "runtimes"),
    "get_channel": ("function", False, "4b576121a346", "links"),
    "get_mirror_link": ("function", False, "cdc6a6d31d8b", "links"),
    "get_or_create": ("function", True, "c9826b33d6da", "claims"),
    "get_origin_link": ("function", False, "cdc6a6d31d8b", "links"),
    "get_pid": ("function", False, "1290edeed1b6", "claims"),
    "get_provider": ("function", False, "c66f77f7d253", "claims"),
    "get_session_for_thread": ("function", False, "d1420626ba0f", "links"),
    "get_slack_link": ("function", False, "1ff511281524", "links"),
    "get_subagent_runtime": ("function", True, "6bc33ba9df46", "runtimes"),
    "get_thread": ("function", False, "4b576121a346", "links"),
    "has_mirror_row": ("function", False, "1eda252b53bd", "links"),
    "has_session": ("function", False, "1eda252b53bd", "claims"),
    "identity_sweep_waiting_on": ("property", False, "9d5ab4ea1828", "state_views"),
    "inbound_callback_count": ("property", False, "fa6cfecd5574", "state_views"),
    "is_busy": ("function", False, "1eda252b53bd", "claims"),
    "is_cancelled": ("function", False, "50fe52427d8c", "claims"),
    "is_compacting": ("function", False, "1eda252b53bd", "turn_context"),
    "is_continuable": ("function", False, "1eda252b53bd", "claims"),
    "is_mirror_paused": ("function", False, "8f19901e86b9", "links"),
    "is_provider_alive": ("function", True, "5ccb54f56604", "claims"),
    "is_session_sharing_eligible": ("function", False, "3780bd272b7a", "claims"),
    "is_slack_paused": ("function", False, "1eda252b53bd", "links"),
    "mapped_session_keys": ("function", False, "602c14087aa8", "claims"),
    "mapped_sid": ("function", False, "f1d40e624ae4", "claims"),
    "mark_continuable": ("function", False, "958babeb61c8", "claims"),
    "mark_first_turn_history_owed": ("function", False, "1eda252b53bd", "session"),
    "mark_lifecycle_lease": ("function", False, "1eda252b53bd", "claims"),
    "mark_needs_reinjection": ("function", False, "958babeb61c8", "turn_context"),
    "mark_provider_switch_replay": ("function", False, "1eda252b53bd", "turn_context"),
    "max_generation": ("function", False, "507f0cfc2b1b", "links"),
    "mirror_accepts_inbound": ("function", False, "1eda252b53bd", "links"),
    "mirror_claim_blockers": ("function", False, "1d8651fc9ebe", "links"),
    "mirror_link_nonce": ("function", False, "f1d40e624ae4", "links"),
    "mirror_opt_out": ("function", False, "1eda252b53bd", "links"),
    "note_stop": ("function", False, "1eda252b53bd", "teardown"),
    "open_replay_gap": ("function", False, "958babeb61c8", "teardown"),
    "open_task_session": ("function", True, "79d5142a16be", "runtimes"),
    "parent_work_scratch_dir": ("function", False, "b0a47dac7d8f", "claims"),
    "pause_turn_admission_for_update": ("function", True, "a6632a57e602", "claims"),
    "pending_identity_sweep_fingerprint": ("property", False, "bb418bb7ece8", "state_views"),
    "project_aws_profile": ("function", False, "cb2074e9df23", "claims"),
    "provider_switch_replay_pending": ("function", False, "1eda252b53bd", "turn_context"),
    "record_failure": ("function", True, "1eda252b53bd", "claims"),
    "record_success": ("function", False, "958babeb61c8", "claims"),
    "recycle_background": ("function", True, "56a38eea24ac", "runtimes"),
    "recycle_heartbeat": ("function", True, "b60c5fea256e", "runtimes"),
    "refresh_defaults": ("function", True, "9ce79a530960", "config_apply"),
    "release": ("function", False, "b35cc323b390", "claims"),
    "release_subagent_runtime": ("function", True, "0f7ee42764e5", "runtimes"),
    "reload_provider_factory": ("function", True, "9ce79a530960", "config_apply"),
    "remove": ("function", True, "958babeb61c8", "teardown"),
    "remove_if_unclaimed": ("function", True, "1eda252b53bd", "teardown"),
    "replace_mirror_link_if": ("function", False, "7cf643151494", "links"),
    "reserve_generation": ("function", False, "57eca86d38c3", "links"),
    "reserve_inbound_callback": ("function", False, "b58e9cc18136", "claims"),
    "reset": ("function", True, "023cf44b43c3", "teardown"),
    "restore_queue": ("function", False, "e233ef2976f2", "claims"),
    "resumable_hint": ("function", False, "1eda252b53bd", "claims"),
    "resumable_sid": ("function", False, "4b576121a346", "claims"),
    "resume_turn_admission_after_update": ("function", True, "a6632a57e602", "claims"),
    "retire_effort_override": ("function", False, "958babeb61c8", "claims"),
    "retire_kiro_identity_sessions": ("function", True, "0929bcd2b0e5", "teardown"),
    "runtime_pids": ("function", False, "2468fe0fd6a3", "claims"),
    "runtime_reconciler": ("function", False, "4a493fe5dab9", "sweeps"),
    "seed_conversation": ("function", False, "7b9b418f95e7", "claims"),
    "session_generation": ("function", False, "028659ce42ac", "claims"),
    "session_keys": ("function", False, "602c14087aa8", "claims"),
    "set_active_dashboard_slots": ("function", False, "883f69448594", "sweeps"),
    "set_approval_policy": ("function", False, "33c822b68141", "claims"),
    "set_autocompact_pct": ("function", False, "960510a3bde3", "turn_context"),
    "set_bind_listener": ("staticmethod", False, "5106224b3fde", "session"),
    "set_channel": ("function", True, "29523aba714b", "links"),
    "set_child_teardown_handler": ("function", False, "9bf58b419a0c", "teardown"),
    "set_compact_callback": ("function", False, "6f1bae527bd9", "turn_context"),
    "set_compacting_callback": ("function", False, "2aed1f018edf", "turn_context"),
    "set_continuable_fallback": ("function", False, "9129b57ccbd2", "claims"),
    "set_injection_probe": ("function", False, "f6fd6f250ecd", "sweeps"),
    "set_lifecycle_turn_active": ("function", False, "3656499fe52f", "claims"),
    "set_mirror_link": ("function", False, "e65f668f550c", "links"),
    "set_mirror_opt_out": ("function", False, "5248122f7adc", "links"),
    "set_mirror_paused": ("function", False, "1293ad7be097", "links"),
    "set_origin_link": ("function", False, "97790037e57a", "links"),
    "set_recycle_callback": ("function", False, "eb879633e29d", "teardown"),
    "set_slack_link": ("function", False, "3d8214a87052", "links"),
    "set_slack_paused": ("function", False, "f83180910a99", "links"),
    "set_subagent_probe": ("function", False, "15a1fb4a5fb2", "sweeps"),
    "set_thread": ("function", True, "ca35fd6d7149", "links"),
    "set_unbind_listener": ("staticmethod", False, "de25b1a3a879", "session"),
    "slack_link_nonce": ("function", False, "f1d40e624ae4", "links"),
    "start_pool": ("function", True, "255a2f05f73c", "runtimes"),
    "stop_generation": ("function", False, "028659ce42ac", "teardown"),
    "stop_turn": ("function", True, "67ebfe2370a5", "teardown"),
    "suppress_replay_persistently": ("function", False, "958babeb61c8", "turn_context"),
    "teardown_scope": ("function", False, "30ab529c41c2", "teardown"),
    "tearing_down": ("function", False, "cc5b7dd151c6", "teardown"),
    "touch": ("function", False, "1eda252b53bd", "claims"),
    "try_acquire": ("function", True, "1eda252b53bd", "claims"),
    "unlanded_marker_pending": ("function", False, "1eda252b53bd", "turn_context"),
    "unmark_continuable": ("function", False, "958babeb61c8", "claims"),
    "update_restart_fenced": ("property", False, "a6632a57e602", "state_views"),
    "warm_providers": ("function", False, "6d42d4fe7cc2", "runtimes"),
    "widen_cold_start_queues": ("function", False, "b60c5fea256e", "config_apply"),
}

#: State view -> (has a setter, docstring digest, getter body digest, setter body digest),
#: each digest taken from the BASE ``@property`` the view replaced.
_PROPERTIES: dict[str, tuple[bool, str, str, str]] = {
    "_active_dashboard_slots": (True, "e3b0c44298fc", "27edb7230136", "1d193bab24ba"),
    "_bg_runtime": (True, "e3b0c44298fc", "cf45974d698d", "5a65d5c5dd79"),
    "_bg_runtime_lock": (True, "e3b0c44298fc", "21e44ccdfc1e", "9e456e2afbc9"),
    "_cleanup_task": (True, "e3b0c44298fc", "9d5a504bffcc", "648bd5f0e39d"),
    "_closing": (True, "e3b0c44298fc", "238e46fc3022", "8205b610981d"),
    "_compact_cooldown_until": (True, "e3b0c44298fc", "27cc7d580ebc", "d398cb7da756"),
    "_compact_pending_verdict": (True, "e3b0c44298fc", "a6af2ceff1d5", "c9f29567ab7d"),
    "_compacting": (True, "e3b0c44298fc", "3a324dd4a57b", "635176f11cd2"),
    "_continuable_fallback": (True, "e3b0c44298fc", "d7b551ac95ae", "778fe39f44f2"),
    "_continuable_keys": (True, "e3b0c44298fc", "538ec3a7ac9d", "12c2b975238b"),
    "_draining_bg_runtimes": (True, "e3b0c44298fc", "c87ab07fc85f", "082e345f52fd"),
    "_draining_subagent_runtimes": (True, "e3b0c44298fc", "583a3f4c9255", "4b307dbeb380"),
    "_identity_sweep_lock": (True, "e3b0c44298fc", "8bd66f1c4491", "0af91ae67016"),
    "_idle_sweep_enabled": (True, "e3b0c44298fc", "25f85a8a4cdb", "48b463014da3"),
    "_idle_timeout": (True, "e3b0c44298fc", "873fa6f16d78", "f9560cb03c9d"),
    "_last_pycache_gc": (True, "e3b0c44298fc", "970335b19f92", "4fef8d3205f1"),
    "_lock": (True, "e3b0c44298fc", "e9f3d1a227b4", "c1f597bad368"),
    "_on_compacted": (True, "e3b0c44298fc", "dea0fffe624b", "b53e08b7f644"),
    "_on_recycled": (True, "e3b0c44298fc", "260b104cce33", "6df70c3f865b"),
    "_origin_links": (True, "e3b0c44298fc", "e65383e2477f", "b5eedd65da11"),
    "_pool_agent": (True, "e3b0c44298fc", "156fc7dff5a6", "5d5db1fbefdb"),
    "_pool_cwd": (True, "e3b0c44298fc", "2b3402d4c4cf", "6b581e9cc46a"),
    "_pool_fill_lock": (True, "e3b0c44298fc", "f5cd3c0fa635", "b6e8c1544082"),
    "_pool_health_task": (True, "e3b0c44298fc", "e73e88a64520", "424a77f0d4b0"),
    "_pool_size": (True, "e3b0c44298fc", "0d6f7cd7674c", "529d1b871626"),
    "_pool_started": (True, "e3b0c44298fc", "a88c09beb60f", "9f19c57fb68d"),
    "_pool_sweep_pids": (True, "e3b0c44298fc", "f1361eab5aa6", "ad3f1a1a2e5a"),
    "_pool_ttl_secs": (True, "e3b0c44298fc", "3c3b61de44db", "f35ced988574"),
    "_recycling": (True, "e3b0c44298fc", "c56a4248b4e6", "50f5fb156a43"),
    "_rss_max_mb": (True, "e3b0c44298fc", "9a8b1e7aef92", "b14648e54b58"),
    "_sessions": (True, "e3b0c44298fc", "32a308a3cfb2", "09cd245a36cc"),
    "_start_sem": (True, "e3b0c44298fc", "8b7f1078f797", "307c26f2ffc9"),
    "_starting_pids": (True, "e3b0c44298fc", "05e0b78fc842", "83911b63bc15"),
    "_stuck_reported": (True, "e3b0c44298fc", "e29f1297be4c", "132a4623ee9c"),
    "_subagent_runtime_locks": (True, "e3b0c44298fc", "f1d8226bd7d2", "5810d1f81d21"),
    "_subagent_runtimes": (True, "e3b0c44298fc", "e6953e516a47", "af3593498280"),
    "_suppress_replay": (True, "e3b0c44298fc", "75e87571c017", "1704b68962f0"),
    "_update_pause_owned": (True, "e3b0c44298fc", "03360588fa18", "e313dec2722a"),
    "_warm_pool": (True, "e3b0c44298fc", "60d0c38e145d", "6a13740f55ce"),
    "_watchdog": (True, "e3b0c44298fc", "373a8ceb98d1", "22b9cebf49e6"),
    "admission_closed": (False, "ec25128af8a2", "2a56f99767a8", ""),
    "count": (False, "e3b0c44298fc", "920ce8a925e0", ""),
    "final_drain_started": (False, "4f8928884635", "071d6042a50f", ""),
    "identity_sweep_waiting_on": (False, "befbf32e7d8d", "ecd5f37b10c0", ""),
    "inbound_callback_count": (False, "014d66e4459a", "c4faa8600ae4", ""),
    "pending_identity_sweep_fingerprint": (False, "6e61bcba0729", "f01fb814b494", ""),
    "update_restart_fenced": (True, "fa3772b3fb27", "200d1a6737e6", "358ab92c9df2"),
}


#: Class-level annotations a binding does not carry: the two config tables are bound
#: from their owner, so the class body declares no annotation for them.
_DROPPED_ANNOTATIONS = frozenset({"_CONFIG_SECTIONS", "_FACTORY_CONFIG_PATHS"})

#: Moved module-level names the facade does not read itself, served from their owner.
_SERVED = frozenset(
    {
        "CONSOLIDATE_KEY",
        "StopOutcome",
        "_CHANNEL_PREFIX",
        "_COMPACT_RESULT_WAIT_FLOOR_SECS",
        "_CompactCallback",
        "_GEN_SUFFIX_RE",
        "_MAX_ORIGIN_LINKS",
        "_PERSISTENT_KEYS",
        "_RecycleCallback",
        "_SIDE_PREFIX",
        "_STUCK_TURN_REPORT_SECS",
        "_THREAD_PREFIX",
        "_WORKFLOW_AUTHOR_PREFIX",
        "_WORKFLOW_POOL_PREFIX",
        "_opt_out_key",
        "_provider_background_launch",
        "_resolve_compact_wait_secs",
    }
)

#: Classes that moved to an owner and must keep the facade's module name.
_MOVED_CLASSES = ("FirstTurnState", "_Session", "_CompactCallback", "_RecycleCallback")

_PACKAGE = "kiro_crew.session_parts"


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in session_mod._SESSION_OWNERS]


def _owner_module(short: str) -> ModuleType:
    return session_mod if short == "session" else sys.modules[f"{_PACKAGE}.{short}"]


def _bound_owners() -> list[str]:
    homes = {owner for kind, _c, _d, owner in _MEMBERS.values() if owner != "session"}
    return [name for name in session_mod._SESSION_OWNERS if name.rsplit(".", 1)[-1] in homes]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return _digest(str(sig.replace(parameters=params)))


def _member_function(name: str) -> Any:
    value = vars(SessionManager)[name]
    if isinstance(value, staticmethod):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _accessor(name: str, role: str) -> str:
    return f"_{role}{name}" if name.startswith("_") else f"_{role}_{name}"


def _self_functions(
    tree: ast.Module, owner: str = ""
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The owner's functions the class binds: every ``self``-first function, plus the
    ones the inventory records as static methods of *owner*."""
    statics = {
        name
        for name, (kind, _, _, home) in _MEMBERS.items()
        if kind == "staticmethod" and f"{_PACKAGE}.{home}" == owner
    }
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and ((node.args.args and node.args.args[0].arg == "self") or node.name in statics)
    ]


# --------------------------------------------------------------------------- #
# The inventory
# --------------------------------------------------------------------------- #


def test_the_member_set_is_the_frozen_inventory() -> None:
    present = {
        name
        for name in vars(SessionManager)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    assert sorted(present ^ set(_MEMBERS)) == []
    annotations = set(SessionManager.__dict__.get("__annotations__", {}))
    assert annotations == {"_agent_model_cache"}
    assert not _DROPPED_ANNOTATIONS & annotations


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    value = vars(SessionManager)[name]
    if kind == "data":
        if owner != "session":
            assert value is vars(_owner_module(owner))[name]
        return
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "property": isinstance(value, property),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    shape = inspect.iscoroutinefunction(fn) or (inspect.isasyncgenfunction(fn) and "agen")
    assert (shape or False) == coroutine
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    module = _owner_module(owner)
    assert fn.__module__ == module.__name__
    if owner == "session":
        return
    if kind == "property":
        assert vars(module)[name] is value
        assert fn is vars(module)[_accessor(name, "get")]
    else:
        assert vars(module)[name] is fn


def test_every_owner_holding_members_is_listed_in_the_facade() -> None:
    homes = {owner for _kind, _c, _d, owner in _MEMBERS.values() if owner != "session"}
    listed = {name.rsplit(".", 1)[-1] for name in session_mod._SESSION_OWNERS}
    assert homes <= listed


@pytest.mark.parametrize("owner", session_mod._SESSION_OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    accessors = {_accessor(name, role): name for name in _PROPERTIES for role in ("get", "set")}
    for node in _self_functions(_tree(module), owner):
        if node.name in accessors:
            view = vars(SessionManager)[accessors[node.name]]
            assert vars(module)[node.name] in (view.fget, view.fset)
            continue
        assert node.name in _MEMBERS, f"{owner}.{node.name} is bound on no class"
        assert _member_function(node.name) is vars(module)[node.name]


# --------------------------------------------------------------------------- #
# The state views stay BASE's properties
# --------------------------------------------------------------------------- #


def _accessor_body_digest(module: ModuleType, function: str) -> str:
    (node,) = [
        n
        for n in _tree(module).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function
    ]
    # The docstring is compared on its own, cleaned: moving a function out of the class
    # body changes its raw indentation, not its text.
    body = node.body[1:] if ast.get_docstring(node, clean=False) is not None else node.body
    # Python 3.13 stopped printing empty fields by default; the frozen digests are 3.12's.
    keep_empty = {"show_empty": True} if sys.version_info >= (3, 13) else {}
    return _digest(ast.dump(ast.Module(body=body, type_ignores=[]), **keep_empty))


@pytest.mark.parametrize("name", sorted(_PROPERTIES))
def test_each_state_view_is_the_base_property(name: str) -> None:
    has_setter, doc, getter_body, setter_body = _PROPERTIES[name]
    view = vars(SessionManager)[name]
    assert isinstance(view, property) and isinstance(view, StateProperty)
    assert view.fdel is None
    assert (view.fset is not None) is has_setter
    assert _digest(inspect.cleandoc(view.__doc__ or "")) == doc
    module = sys.modules[view.fget.__module__]
    assert view.fget.__name__ == _accessor(name, "get")
    assert _accessor_body_digest(module, view.fget.__name__) == getter_body
    if has_setter:
        assert view.fset.__name__ == _accessor(name, "set")
        assert _accessor_body_digest(module, view.fset.__name__) == setter_body


def test_the_inventory_covers_every_property_and_nothing_else() -> None:
    assert set(_PROPERTIES) == {n for n, row in _MEMBERS.items() if row[0] == "property"}


def test_getattr_static_reads_each_member_as_its_base_kind() -> None:
    """``test/turn_harness.py`` tells properties from methods by ``getattr_static``."""
    kinds = {
        "property": property,
        "staticmethod": staticmethod,
    }
    for name, (kind, _co, _d, _owner) in _MEMBERS.items():
        static = inspect.getattr_static(SessionManager, name)
        if kind in kinds:
            assert isinstance(static, kinds[kind]), name
        elif kind == "function":
            assert inspect.isfunction(static), name


def test_create_autospec_reads_the_class_as_it_did() -> None:
    """A coroutine member autospecs to an awaitable mock, every other method to a
    synchronous one, and a property to a plain attribute, as at BASE."""
    spec = mock.create_autospec(SessionManager, instance=True)
    for name, (kind, coroutine, _d, _owner) in _MEMBERS.items():
        if kind not in ("function", "staticmethod") or name == "__init__":
            continue
        child = getattr(spec, name)
        assert callable(child), name
        assert isinstance(child, mock.AsyncMock) is (coroutine is True), name
    for name in _PROPERTIES:
        assert not isinstance(getattr(spec, name), mock.AsyncMock), name


# --------------------------------------------------------------------------- #
# What a bound function may not use, and what an owner may not import
# --------------------------------------------------------------------------- #


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


@pytest.mark.parametrize("owner", session_mod._SESSION_OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    tree = _tree(sys.modules[owner])
    assert {fn.name: _class_only_constructs(fn) for fn in _self_functions(tree, owner)} == {
        fn.name: [] for fn in _self_functions(tree, owner)
    }


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    """Lines that import the facade (``kiro_crew.session``) outside a ``TYPE_CHECKING`` block."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            guarded.update(id(inner) for inner in ast.walk(node))
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == "kiro_crew.session" or (
                node.module == "kiro_crew" and any(a.name == "session" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == "kiro_crew.session" for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.session import SessionManager\n"
        "def f():\n    from kiro_crew.session import x\n"
        "import kiro_crew.session\n"
        "from kiro_crew import session\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize("owner", session_mod._SESSION_OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(sys.modules[owner])) == []


def _owner_imports(tree: ast.Module) -> set[str]:
    """The session owner modules *tree* imports, by dotted name."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(f"{_PACKAGE}."):
                found.add(node.module)
            elif node.module == _PACKAGE:
                found.update(f"{_PACKAGE}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith(f"{_PACKAGE}."))
    return found


def test_the_owner_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from kiro_crew.session_parts.policy import X\n"
        "from kiro_crew.session_parts import record as _r\n"
        "import kiro_crew.session_parts.claims\n"
    )
    assert _owner_imports(tree) == {
        f"{_PACKAGE}.policy",
        f"{_PACKAGE}.record",
        f"{_PACKAGE}.claims",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_SESSION_OWNERS`` lists the owners lowest layer first: an owner imports only
    owners earlier in the tuple, so the order is the import order."""
    owners = session_mod._SESSION_OWNERS
    later = {
        owner: sorted(_owner_imports(_tree(sys.modules[owner])) - set(owners[:i]))
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


# --------------------------------------------------------------------------- #
# What each owner says about itself
# --------------------------------------------------------------------------- #

#: Calls on ``self.<field>`` that change the field's value in place.
_MUTATORS = frozenset({"add", "discard", "append", "pop", "update", "clear", "setdefault"})


def _self_field(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "self" and node.attr != "__dict__":
            return node.attr
    return None


def _is_self_dict(node: ast.AST) -> bool:
    """Whether *node* is ``self.__dict__``, whose string keys name fields."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__dict__"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _str_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _fields(fns: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> tuple[set[str], set[str]]:
    """The ``self`` fields *fns* write and read. Writes: a store or delete of the
    attribute or of a subscript on it, a mutator call on it and ``setattr(self, "<f>",
    ...)``. Reads: every other load and ``getattr``/``hasattr(self, "<f>")``. A
    ``self.__dict__["<f>"]`` subscript or ``self.__dict__.<method>("<f>", ...)`` call
    names the field ``<f>``, written by a store, delete or mutator, read otherwise."""
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
            if isinstance(node, ast.Subscript) and _is_self_dict(node.value):
                key = _str_const(node.slice)
                if key is not None:
                    (written if isinstance(node.ctx, (ast.Store, ast.Del)) else read).add(key)
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and _is_self_dict(func.value) and node.args:
                key = _str_const(node.args[0])
                if key is not None:
                    (written if func.attr in _MUTATORS else read).add(key)
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


def test_the_field_scan_reads_self_dict_keys_as_fields() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    self.__dict__['a'] = 1\n    del self.__dict__['b']\n"
        "    self.__dict__.pop('c', None)\n    self.__dict__.setdefault('d', 1)\n"
        "    return self.__dict__['e'], self.__dict__.get('f'), self.__dict__.get('a')\n"
    )
    assert _fields([tree.body[0]]) == ({"a", "b", "c", "d"}, {"e", "f"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\n\n|\n?$)", doc, re.S)
    return set(re.findall(r"``(\w+)``", match.group(1))) if match else set()


@pytest.mark.parametrize("owner", _bound_owners())
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module), owner))
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


@pytest.mark.parametrize("owner", session_mod._SESSION_OWNERS)
def test_owners_log_under_the_session_logger(owner: str) -> None:
    module = sys.modules[owner]
    assert getattr(module, "logger", session_mod.logger) is session_mod.logger
    assert session_mod.logger.name == "kiro_crew.session"


@pytest.mark.parametrize("name", _MOVED_CLASSES)
def test_a_moved_class_keeps_the_facade_as_its_module(name: str) -> None:
    """A class renders as ``module.qualname`` in reprs, pickles and tracebacks; one that
    moved to an owner keeps naming the path callers import it from."""
    assert getattr(session_mod, name).__module__ == session_mod.__name__


def test_every_class_an_owner_defines_is_a_moved_one_or_the_property_glue() -> None:
    defined = {
        node.name
        for owner in _owners()
        for node in ast.walk(_tree(owner))
        if isinstance(node, ast.ClassDef)
    }
    assert defined == {*_MOVED_CLASSES, "StateProperty"}


# --------------------------------------------------------------------------- #
# The mirror: what the facade serves, and a write through it
# --------------------------------------------------------------------------- #


def test_the_mirror_serves_exactly_the_moved_names_the_facade_does_not_read() -> None:
    index = session_mod._MIRROR
    assert index.excluded == ()
    assert index.divergent() == ()
    assert dict(index.forwards) == {}
    assert set(index.exports) == _SERVED == set(session_mod._EXPORTS)
    assert not _SERVED & set(vars(session_mod))


def test_a_bound_member_function_is_not_a_module_attribute() -> None:
    """The surface keeps owner member functions off the facade module: only the class
    carries them, as before the split."""
    leaked = sorted(
        name
        for name, (kind, _c, _d, owner) in _MEMBERS.items()
        if owner != "session" and kind != "data" and hasattr(session_mod, name)
    )
    assert leaked == []
    assert not hasattr(session_mod, "compact_wait_budget_secs")


@pytest.mark.parametrize(
    "name,reader",
    [
        ("_build_child_map", f"{_PACKAGE}.sweeps"),
        ("cleanup_stale_sandbox_profiles", f"{_PACKAGE}.sweeps"),
        ("_provider_has_active_turn", f"{_PACKAGE}.provider_probes"),
        ("_STATELESS_PREFIXES", f"{_PACKAGE}.policy"),
    ],
)
def test_a_patch_through_the_facade_lands_on_the_owner_and_is_undone(
    monkeypatch: pytest.MonkeyPatch, name: str, reader: str
) -> None:
    owner = sys.modules[reader]
    before = vars(owner)[name]
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(session_mod, name, sentinel)
        assert vars(owner)[name] is sentinel
        assert getattr(session_mod, name) is sentinel
    assert vars(owner)[name] is before
    assert getattr(session_mod, name) is before


def test_a_patch_of_a_served_name_reaches_every_owner_reading_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = sys.modules[f"{_PACKAGE}.policy"]
    links = sys.modules[f"{_PACKAGE}.links"]
    before = policy._MAX_ORIGIN_LINKS
    with monkeypatch.context() as patch:
        patch.setattr(session_mod, "_MAX_ORIGIN_LINKS", 3)
        assert policy._MAX_ORIGIN_LINKS == 3
        assert links._MAX_ORIGIN_LINKS == 3
        assert "_MAX_ORIGIN_LINKS" not in vars(session_mod)
    assert policy._MAX_ORIGIN_LINKS is before
    assert links._MAX_ORIGIN_LINKS is before


#: What ``from kiro_crew.session import *`` bound before the split (the unsplit module had
#: no ``__all__``, so every public name it bound): the facade's derived ``__all__`` keeps it.
_STAR_EXPORTS = frozenset("""
    ACP_BACKENDS_ACP_RUNTIME ACP_BACKENDS_SESSION_EVICTION ACP_BACKEND_KIRO
    AUTOCOMPACT_PCT_MAX AUTOCOMPACT_PCT_MIN AbstractContextManager AllocationConstants
    AllocationDeps Any Awaitable BACKGROUND_AGENT BACKGROUND_KEY BackgroundRuntimeDeps
    BackgroundSessionRuntime BindListener COMPACT_OUTCOME_COMPACTED
    COMPACT_WAIT_TIMEOUT_SECS CONSOLIDATE_KEY CONTEXT_WARN_MARGIN_PCT Callable CancelOutcome
    ChannelLink ClaudeCodeProvider CleanupDeps CleanupHook CleanupState
    CompactionCoordinator CompactionDeps CompactionState ConfigChange Enum FirstTurnState
    HEARTBEAT_KEY InboundCallbackReservation Iterator KiroCrewConfig LLMProvider Literal
    MIRROR_OPT_OUT_FLAG POOL_DECISIONS POOL_SIZE_MAX PROVIDER_LABEL_CLAUDE
    PROVIDER_LABEL_DEFAULT PYCACHE_GC_INTERVAL_SECS Path PidRefcount PrioritySemaphore
    Protocol ProviderFactory SESSION_IDLE_EXPIRED SUPPRESS_REPLAY_FLAG
    SessionAllocationService SessionBusyError SessionCleanup SessionClosingError
    SessionEndingError SessionLifecycleConstants SessionLifecycleDeps
    SessionLifecycleService SessionLifecycleState SessionManager SessionMap
    SessionRegistryState SessionWatchdog SpeculativeResumeRefused StartPriority Stats
    StopOutcome TYPE_CHECKING TornDown UNBIND_REASON_SESSION_DESTROYED
    UNBIND_REASON_UNSPECIFIED UnbindListener WarmPoolDeps WarmSessionPool
    advertised_model_ids annotations asyncio auto build_provider_factory
    cached_cold_start_concurrency canonical_key cast child_process_helpers
    cleanup_orphaned_session_roots cleanup_orphaned_sessions cleanup_stale_sandbox_profiles
    config_dir configure_runtime_spawn_width contextmanager crew_autocompact_pct dataclass
    default_project_dir deque detect_provider_switch emit_counter field
    find_orphan_mcp_candidates get_recorder get_session_rss_mb inspect
    is_claude_backend_name iter_agent_spec_files kill_orphan_mcps kiro_agents_dir_path
    legacy_key live logger logging maintenance_executor model_is_unusable model_registry
    model_registry_namespace model_scope new_cold_start_semaphore normalize_agent_model os
    platform_compat process_shutdown_requested prune_legacy_member_pid_bindings
    prune_pycache published_autocompact_pct re reap_abandoned_agent_scopes
    resolve_pin_spelling_on rotate_shell_audit_log schedule_abort_for sel
    selectable_backends set_bind_listener set_unbind_listener shutdown_event spec_model
    subprocess_executor telemetry_channel_of time unlink_queued_temp_paths
    widen_cold_start_semaphore
    """.split())


def test_the_star_import_binds_what_it_bound_before_the_split() -> None:
    assert set(session_mod.__all__) == _STAR_EXPORTS
    assert sorted(session_mod.__all__) == list(session_mod.__all__)
    unresolved = [name for name in session_mod.__all__ if not hasattr(session_mod, name)]
    assert unresolved == []
