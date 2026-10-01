"""What the ``kiro_crew.autonudge`` surface and its store bytes must keep.

The behaviour pins below were captured from the module before its responsibilities moved
into ``kiro_crew/autonudge_service/`` and pass on both sides of that move; the composition
and layering pins at the end describe the split itself:

* every name the module defined, and every imported name callers and tests read off it,
  still resolves on ``kiro_crew.autonudge``;
* every ``AutoNudgeService`` and maintenance-view member keeps its kind (method,
  static, class, coroutine, async context manager) and its signature;
* ``NudgeLoop`` keeps its fields, their order and their defaults;
* ``autonudge.json`` and ``autonudge.quarantine.json`` are written byte for byte as
  before: key order, indentation, the disk-only cycle-claim markers, an unparsed
  row carried verbatim at the end, a held-aside row kept only in the sidecar;
* a patch on a module-level name the moved code reads still reaches it;
* a patch on a service member still reaches every internal caller, because internal
  calls go through the instance -- the row serializer the store uses included.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import dataclasses
import hashlib
import inspect
import json
import logging
import re
import subprocess
import sys
import time
import typing
from pathlib import Path
from types import ModuleType

import pytest

import kiro_crew.autonudge as autonudge
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop, _AutoNudgeMaintenanceView
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorCreationSurface,
    monitor_state_to_dict,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT

#: Every function, class and constant the pre-split ``autonudge.py`` defined.
_BASE_DEFINED = frozenset(
    {
        "ADDRESSING_FIELDS",
        "APPROVAL_STALL_REASON",
        "AUTONUDGE_STOP_REASON",
        "AutoNudgeService",
        "AutoNudgeStaleBaseline",
        "AutoNudgeStoreUnvetted",
        "MANUAL_STOP_REASON",
        "MONITOR_TERMINAL_REASON",
        "MonitorUpdateConflict",
        "NUDGE_RENEW_DUE_SHARE",
        "NudgeAdmissionRefused",
        "NudgeLoop",
        "SENTINEL_DROPPED_REASON",
        "SESSION_START_FAILURE_REASON",
        "STRUCTURAL_TERMINAL_REASON",
        "_AutoNudgeMaintenanceView",
        "_CHANNEL_KEY_PREFIXES",
        "_INSTANCE",
        "_JUDGE_MAX_CRITERION_CHARS",
        "_JUDGE_MAX_CURSORS",
        "_JUDGE_MAX_OUTCOME_CHARS",
        "_JUDGE_MAX_TARGETS",
        "_JUDGE_MAX_TARGET_CHARS",
        "_JUDGE_PR_SEEN_DIGEST_CHARS",
        "_JUDGE_PR_SEEN_REMARKS",
        "_JUDGE_QUIET_STREAK_FLOOR_DEFAULT",
        "_MAINTENANCE_LOCKS",
        "_MAX_IDLE_SECS",
        "_MAX_QUIET_STREAK",
        "_MIN_IDLE_SECS",
        "_MONITOR_RETRY_BACKOFF_SECS",
        "_MONITOR_RETRY_MAX_BACKOFF_SECS",
        "_MUTATION_LOCK_OWNERS",
        "_NUDGES_FILE",
        "_OVERDUE_REARM_SECS",
        "_PR_FACTS_TICK_KEYS",
        "_QUARANTINE_FILE",
        "_REARM_BACKOFF_MAX_SHIFT",
        "_REARM_BACKOFF_SECS",
        "_REARM_MAX_BACKOFF_SECS",
        "_RECONCILE_INTERVAL_SECS",
        "_REPLACEABLE_LOOP_STOP_REASONS",
        "_START_FAILURE_BACKOFF_AFTER",
        "_START_FAILURE_STANDDOWN_AFTER",
        "_STORE_VERSION",
        "_TERMINAL_BOUND_REASONS",
        "_WAKE_FOLLOWUP_TICKS",
        "_addressing_value_unsafe_why",
        "_assert_mutation_lock_owned",
        "_bounded_judge_cursors",
        "_bounded_judge_pr_seen",
        "_bounded_judge_recent",
        "_bounded_judge_spec",
        "_bounded_judge_verdict",
        "_cancel_and_drain_tasks",
        "_claim_mutation_lock",
        "_current_task_or_none",
        "_is_torn_deactivation",
        "_judge_pr_targets",
        "_locked_file",
        "_maintenance_lock",
        "_positive_number",
        "_pr_facts_digest",
        "_pr_observation_of",
        "_quarantine_row_key",
        "_release_mutation_lock",
        "_repair_number",
        "_resolve_beat",
        "_rows_or_empty",
        "_stopped_row_is_replaceable",
        "_unclaim_mutation_lock",
        "binding_key_for",
        "enabled",
        "get_instance",
        "infer_monitor",
        "infer_subject",
        "is_channel_key",
        "is_structured_monitor_loop",
        "logger",
        "loop_subject",
        "new_goal_token",
        "nudge_cycle_header",
        "redact_store_value",
        "repair_sentinel_path",
        "runtime_budget_exceeded",
        "scrub_loop_text",
        "scrubbed_judge_spec",
        "structured_monitor_binding_key_for",
        "terminal_notification_delivery_matches",
    }
)

#: The names it imported that callers and tests read off it (``autonudge.irq.poll``,
#: ``autonudge.time.time``, ``autonudge.MAX_BANNER_CHARS`` ...), found by scanning the
#: tree for every ``kiro_crew.autonudge.<name>`` access.
_BASE_REACHED_IMPORTS = frozenset(
    {
        "MAX_BANNER_CHARS",
        "MonitorState",
        "asyncio",
        "config_dir",
        "fsync_dir",
        "irq",
        "is_sensitive_path",
        "replace_with_retry",
        "time",
    }
)

#: ``name -> (kind, signature digest)`` for every member ``AutoNudgeService`` defined: the
#: kind is method / static / class plus ``+async`` / ``+acm``, and the digest is the first
#: twelve hex digits of the SHA-256 of ``_signature_text`` -- every parameter, ``self``/``cls``
#: included, with its kind, default and annotation, and the return annotation.
_BASE_SERVICE_MEMBERS: dict[str, tuple[str, str]] = {
    "__init__": ("method", "38ac57f125dd"),
    "_acquire_mutation_lock": ("method+async", "e26080a5a8bc"),
    "_add_locked": ("method+async", "923a1f34e18b"),
    "_add_monitor_locked": ("method+async", "55179b26aeff"),
    "_add_unserialized": ("method+async", "923a1f34e18b"),
    "_append_judge_labels": ("method", "05f9e694d3d7"),
    "_apply_monitor_budget_stop": ("method", "c05b26fec956"),
    "_apply_monitor_user_stop": ("method", "0d02c6c3b88e"),
    "_apply_staged_monitor": ("method", "1eecbc416bc8"),
    "_arm_from_deadline": ("method", "fa9fb3019298"),
    "_arm_timer": ("method", "e290ef84f151"),
    "_begin_maintenance_quiesce": ("method", "f7956b1e6531"),
    "_cancel_timer": ("method", "bbb2f1624984"),
    "_compact_quarantine_sidecar": ("method", "35d06f40fb45"),
    "_compact_quarantine_sidecar_locked": ("method", "35d06f40fb45"),
    "_commit_judge_pr_seen": ("method+async", "441c236a15de"),
    "_confirm_judge_delivery": ("method", "fa9fb3019298"),
    "_deactivate_and_wait_unserialized": ("method+async", "4aa7e82c7c52"),
    "_deactivate_unwired_monitor": ("method+async", "f7956b1e6531"),
    "_drop_quarantine_sidecar": ("method", "35d06f40fb45"),
    "_emit": ("method", "2a11698d553e"),
    "_end_maintenance_quiesce": ("method", "f7956b1e6531"),
    "_find_by_slot": ("method", "1a9a46af4d72"),
    "_judge_quiet_streak_floor": ("method", "a149538e830d"),
    "_judge_tick_is_quiet": ("method+async", "13350afdc048"),
    "_label_judge_delivery_locked": ("method+async", "ebb80b654e73"),
    "_load": ("method", "35d06f40fb45"),
    "_mint_loop_id": ("method", "c54e236e2328"),
    "_monitor_snapshot_with_replacement": ("method", "889dae1a56cf"),
    "_monitor_tick_is_quiet": ("method+async", "3a33d55b099b"),
    "_move_aside_locked": ("method", "8bdc12b93966"),
    "_move_aside_unreadable_sidecar": ("method", "35d06f40fb45"),
    "_persist_judge_state": ("method+async", "3a33d55b099b"),
    "_persist_locked": ("method+async", "35d06f40fb45"),
    "_persist_soon": ("method", "35d06f40fb45"),
    "_persist_staged_monitor_locked": ("method+async", "1eecbc416bc8"),
    "_provider_credentials_authorized": ("static+async", "1009d6af5d26"),
    "_publish_pr_observation": ("method+async", "c0572291a250"),
    "_quarantine_rows_on_disk": ("method", "f21967637259"),
    "_read_quarantine_sidecar": ("method", "83d3950d4e97"),
    "_reconcile_forever": ("method+async", "35d06f40fb45"),
    "_reconcile_once": ("method", "35d06f40fb45"),
    "_record_judge_verdict": ("method", "6b6aa33c7755"),
    "_record_stops_committed": ("method", "b02c548f23ec"),
    "_refuse_writes_and_preserve_sidecar": ("method", "35d06f40fb45"),
    "_remove_unserialized": ("method+async", "f25b8e2642a7"),
    "_restore_provider_credentials": ("static+async", "c0e5298bfbb4"),
    "_retain_accepted_terminal_completion": ("method", "0d02c6c3b88e"),
    "_revoke_provider_credentials_before_removal": ("static+async", "631ee4d210f4"),
    "_revoke_self_arm": ("static", "631ee4d210f4"),
    "_revoke_self_arm_for": ("method", "fa9fb3019298"),
    "_run_fire_cycle": ("method+async", "fa9fb3019298"),
    "_save": ("method", "35d06f40fb45"),
    "_serialize_loop": ("static", "25c5a9ba35d5"),
    "_serialize_state": ("method", "070d0bf328d9"),
    "_serialized_loops": ("method", "297c6249aa5f"),
    "_set_monitor_deadline": ("method", "a9f111186976"),
    "_sidecar_transaction": ("method", "140eaa502228"),
    "_sync_terminal_completion_timer": ("method", "fa9fb3019298"),
    "_terminal_still_holds": ("method+async", "75f382f079fb"),
    "_timer": ("method+async", "e290ef84f151"),
    "_update_locked": ("method+async", "b7b62a00f723"),
    "_update_unserialized": ("method+async", "b7b62a00f723"),
    "_waits_for_terminal_completion": ("method", "3a33d55b099b"),
    "_withdraw_judge_suppression": ("method", "fa9fb3019298"),
    "_worker_running": ("method", "490393185551"),
    # The close side of the same liveness question, injected the same way.
    "_worker_closed": ("method", "490393185551"),
    # Reads one loop's monitor kind: the startup resume treats a work-ledger
    # watch differently, because its news arrives by a push a restart loses.
    "_observes_work_ledger": ("method", "3a33d55b099b"),
    "_write_monitor_snapshot_locked": ("method+async", "048a7479cdcf"),
    "_write_quarantine_rows": ("method", "54f84a64e0fe"),
    "_write_quarantine_sidecar": ("method", "35d06f40fb45"),
    "_write_quarantine_sidecar_locked": ("method", "35d06f40fb45"),
    "_write_state": ("method", "b02c548f23ec"),
    "add": ("method+async", "d723f7c062d9"),
    "add_monitor": ("method+async", "b07c1299b741"),
    "apply_monitor_probe": ("method+async", "7e318e77ec64"),
    "clear_terminal_monitor": ("method+async", "82db71923663"),
    "commit_monitor_replacement": ("method", "f7956b1e6531"),
    "deactivate_and_wait": ("method+async", "61dc2e194fa8"),
    "fire_now": ("method+async", "082992b0b249"),
    "get_by_id": ("method", "c7cdaf3c2920"),
    "get_by_slot": ("method", "1a9a46af4d72"),
    "list_all": ("method", "6672df12a725"),
    "load_for_maintenance": ("class+async", "63a85476ed4b"),
    "maintenance_service": ("class+acm", "f67271c49616"),
    "mark_monitor_action_in_flight": ("method+async", "c576214c1411"),
    "mark_monitor_turn_accepted": ("method", "ec9e933a59fb"),
    "mark_terminal_notification_delivered": ("method+async", "32d2569e33bd"),
    "monitor_dispatch_is_authorized": ("method+async", "909258b72a58"),
    "notify_approval_stalled": ("method", "2a34976b11b0"),
    "notify_cycle_landed": ("method", "2a34976b11b0"),
    "notify_cycle_start_failed": ("method", "2a34976b11b0"),
    "notify_turn_complete": ("method", "295ffab180e4"),
    "notify_user_input": ("method", "2a34976b11b0"),
    "record_monitor_completion_evidence_unavailable": ("method+async", "400845dc24be"),
    "record_monitor_dispatch_busy": ("method+async", "400845dc24be"),
    "record_monitor_dispatch_failure": ("method+async", "716192392c13"),
    "record_monitor_dispatched": ("method+async", "400845dc24be"),
    "record_monitor_turn_completion": ("method+async", "fb6d81452a33"),
    "remove": ("method+async", "e208f07be797"),
    "remove_by_slot": ("method+async", "1a9a46af4d72"),
    "remove_sync": ("method", "e06f7146f393"),
    "restore_monitor_after_failed_session_close": ("method+async", "76282e020abf"),
    "retire_monitor_for_session_close": ("method+async", "d67247ca6bd0"),
    "rollback_monitor_replacement": ("method+async", "61dc2e194fa8"),
    "rollback_monitor_update": ("method+async", "d86807019e86"),
    "start": ("method+async", "35d06f40fb45"),
    "stop": ("method", "35d06f40fb45"),
    "stop_monitor": ("method+async", "bddc1c278f12"),
    "stop_monitor_if_budget_exhausted": ("method+async", "7ab50d99dd32"),
    "subscribe": ("method", "7a1b8e1c52f1"),
    "update": ("method+async", "b7b62a00f723"),
    "update_monitor": ("method+async", "15a27e52eef4"),
}

#: The same, for the maintenance view.
_BASE_VIEW_MEMBERS: dict[str, tuple[str, str]] = {
    "__init__": ("method", "6cdc5e12c381"),
    "_release": ("method", "35d06f40fb45"),
    "deactivate_and_wait": ("method+async", "4aa7e82c7c52"),
    "get_by_slot": ("method", "1a9a46af4d72"),
    "list_all": ("method", "6672df12a725"),
    "remove": ("method+async", "f7956b1e6531"),
}

#: ``(field, default)`` for every ``NudgeLoop`` field, in declaration order -- which is
#: also the key order of a stored row, because a row is ``asdict(loop)``.
_BASE_NUDGELOOP_FIELDS: list[tuple[str, str | None]] = [
    ("id", None),
    ("slot_key", None),
    ("message", None),
    ("idle_secs", "60"),
    ("max_cycles", "0"),
    ("cycle_count", "0"),
    ("active", "True"),
    ("last_fire_ts", "0.0"),
    ("created_ts", "0.0"),
    ("stop_sentinel_path", "''"),
    ("goal_token", "''"),
    ("max_runtime_secs", "0"),
    ("gate", "False"),
    ("judge", "factory:dict"),
    ("judge_cursors", "factory:dict"),
    ("judge_pr_seen", "factory:dict"),
    ("judge_quiet_streak", "0"),
    ("judge_wake_pending", "False"),
    ("judge_last_verdict", "factory:dict"),
    ("judge_recent_verdicts", "factory:list"),
    ("stopped_reason", "''"),
    ("approval_stalled", "False"),
    ("consecutive_start_failures", "0"),
    ("next_due_ts", "0.0"),
    ("monitor", "None"),
    ("terminal_notification_outcome", "''"),
    ("terminal_notification_stopped_at", "0.0"),
    ("banner", "''"),
    ("self_armed", "False"),
    ("config_generation", "0"),
]


_FIXED_NOW = 1_790_000_000.0
#: AWS's documented example key: credential-shaped, so the loader holds its row aside.
_HELD_ID = "AKIAIOSFODNN7EXAMPLE"

#: ``autonudge.json`` as the golden scenario writes it, each ``monitor`` object folded
#: to ``<MONITOR>``: those bytes belong to ``monitoring.models.monitor_state_to_dict``,
#: and the test compares them to that function's output instead.
_GOLDEN_STORE = """\
{
  "version": 1,
  "loops": [
    {
      "id": "keepme01",
      "slot_key": "chat-1-100",
      "message": "keep watching",
      "idle_secs": 600,
      "max_cycles": 5,
      "cycle_count": 2,
      "active": true,
      "last_fire_ts": 1789999950.0,
      "created_ts": 1789999000.0,
      "stop_sentinel_path": "",
      "max_runtime_secs": 0,
      "gate": false,
      "judge": {},
      "judge_cursors": {},
      "judge_pr_seen": {},
      "judge_quiet_streak": 0,
      "judge_wake_pending": false,
      "judge_last_verdict": {},
      "judge_recent_verdicts": [],
      "stopped_reason": "",
      "approval_stalled": false,
      "consecutive_start_failures": 0,
      "next_due_ts": 1790000300.0,
      "terminal_notification_outcome": "",
      "terminal_notification_stopped_at": 0.0,
      "banner": "",
      "self_armed": false,
      "config_generation": 0
    },
    {
      "id": "claim001",
      "slot_key": "chat-2-200",
      "message": "was mid fire",
      "idle_secs": 60,
      "max_cycles": 0,
      "cycle_count": 3,
      "active": false,
      "last_fire_ts": 0.0,
      "created_ts": 1789999500.0,
      "stop_sentinel_path": "",
      "max_runtime_secs": 0,
      "gate": false,
      "judge": {},
      "judge_cursors": {},
      "judge_pr_seen": {},
      "judge_quiet_streak": 0,
      "judge_wake_pending": false,
      "judge_last_verdict": {},
      "judge_recent_verdicts": [],
      "stopped_reason": "interrupted_cycle",
      "approval_stalled": false,
      "consecutive_start_failures": 0,
      "next_due_ts": 0.0,
      "terminal_notification_outcome": "",
      "terminal_notification_stopped_at": 0.0,
      "banner": "",
      "self_armed": false,
      "config_generation": 0,
      "inflight_cycle": 4,
      "inflight_undelivered": true
    },
    {
      "id": "gated001",
      "slot_key": "chat-5-500",
      "message": "babysit https://github.com/octo/repo/pull/42 until merged",
      "idle_secs": 300,
      "max_cycles": 24,
      "cycle_count": 0,
      "active": true,
      "last_fire_ts": 0.0,
      "created_ts": 1790000000.0,
      "stop_sentinel_path": "",
      "max_runtime_secs": 3600,
      "gate": true,
      "judge": {
        "wake_when": "a reviewer asks for changes",
        "targets": [
          "https://github.com/octo/repo/pull/42"
        ]
      },
      "judge_cursors": {},
      "judge_pr_seen": {},
      "judge_quiet_streak": 0,
      "judge_wake_pending": false,
      "judge_last_verdict": {},
      "judge_recent_verdicts": [],
      "stopped_reason": "",
      "approval_stalled": false,
      "consecutive_start_failures": 0,
      "next_due_ts": 1790000300.0,
      "monitor": <MONITOR>,
      "terminal_notification_outcome": "",
      "terminal_notification_stopped_at": 0.0,
      "banner": "patrol",
      "self_armed": true,
      "config_generation": 0
    },
    {
      "id": "struct01",
      "slot_key": "chat-6-600",
      "message": "",
      "idle_secs": 900,
      "max_cycles": 0,
      "cycle_count": 0,
      "active": true,
      "last_fire_ts": 0.0,
      "created_ts": 1790000000.0,
      "stop_sentinel_path": "",
      "max_runtime_secs": 0,
      "gate": false,
      "judge": {},
      "judge_cursors": {},
      "judge_pr_seen": {},
      "judge_quiet_streak": 0,
      "judge_wake_pending": false,
      "judge_last_verdict": {},
      "judge_recent_verdicts": [],
      "stopped_reason": "",
      "approval_stalled": false,
      "consecutive_start_failures": 0,
      "next_due_ts": 1790000900.0,
      "monitor": <MONITOR>,
      "terminal_notification_outcome": "",
      "terminal_notification_stopped_at": 0.0,
      "banner": "",
      "self_armed": false,
      "config_generation": 0
    },
    {
      "id": "broken01",
      "slot_key": "chat-4-400"
    }
  ]
}"""

_GOLDEN_SIDECAR = """\
{
  "version": 1,
  "quarantined": [
    {
      "id": "AKIAIOSFODNN7EXAMPLE",
      "slot_key": "chat-3-300",
      "message": "held aside"
    }
  ]
}"""

_MONITOR_OBJECT = re.compile(r'(?ms)^      "monitor": \{\n.*?^      \},\n')


def _signature_text(owner: type, name: str) -> tuple[str, str]:
    """``(kind, rendered signature)`` for one member, in the manifest's notation."""
    raw = inspect.getattr_static(owner, name)
    if isinstance(raw, staticmethod):
        kind, fn = "static", raw.__func__
    elif isinstance(raw, classmethod):
        kind, fn = "class", raw.__func__
    else:
        kind, fn = "method", raw
    if inspect.iscoroutinefunction(fn):
        kind += "+async"
    if inspect.isasyncgenfunction(getattr(fn, "__wrapped__", None)):
        kind += "+acm"
    signature = inspect.signature(fn)
    rendered = []
    for param in signature.parameters.values():
        text = f"{param.name}:{str(param.kind)[0:3]}"
        if param.default is not inspect.Parameter.empty:
            text += f"={param.default!r}"
        annotation = param.annotation
        shown = annotation if isinstance(annotation, str) else repr(annotation)
        rendered.append(f"{text}:{shown}")
    returned = signature.return_annotation
    shown_return = returned if isinstance(returned, str) else repr(returned)
    return kind, f"{', '.join(rendered)} -> {shown_return}"


def _signature_row(owner: type, name: str) -> tuple[str, str]:
    kind, text = _signature_text(owner, name)
    return kind, hashlib.sha256(text.encode()).hexdigest()[:12]


#: Service members that moved onto the composed ``LoopStore`` with the durable-store
#: state they work on, ``service name -> store name``. Everything else stays on the
#: service, most of it as a bound owner function (see ``_BINDINGS``).
_MOVED_TO_STORE = {
    "_compact_quarantine_sidecar": "_compact_quarantine_sidecar",
    "_compact_quarantine_sidecar_locked": "_compact_quarantine_sidecar_locked",
    "_drop_quarantine_sidecar": "_drop_quarantine_sidecar",
    "_move_aside_locked": "_move_aside_locked",
    "_move_aside_unreadable_sidecar": "_move_aside_unreadable_sidecar",
    "_quarantine_rows_on_disk": "_quarantine_rows_on_disk",
    "_read_quarantine_sidecar": "read_quarantine_sidecar",
    "_record_stops_committed": "_record_stops_committed",
    "_refuse_writes_and_preserve_sidecar": "_refuse_writes_and_preserve_sidecar",
    "_sidecar_transaction": "_sidecar_transaction",
    "_write_quarantine_rows": "_write_quarantine_rows",
    "_write_quarantine_sidecar": "_write_quarantine_sidecar",
    "_write_quarantine_sidecar_locked": "_write_quarantine_sidecar_locked",
}


class TestSurface:
    def test_every_name_the_module_defined_still_resolves(self) -> None:
        assert sorted(name for name in _BASE_DEFINED if not hasattr(autonudge, name)) == []

    def test_every_import_callers_reach_through_it_still_resolves(self) -> None:
        assert sorted(n for n in _BASE_REACHED_IMPORTS if not hasattr(autonudge, n)) == []

    @pytest.mark.parametrize("name", sorted(set(_BASE_SERVICE_MEMBERS) - set(_MOVED_TO_STORE)))
    def test_a_service_member_keeps_its_kind_and_signature(self, name: str) -> None:
        assert (
            _signature_row(AutoNudgeService, name) == _BASE_SERVICE_MEMBERS[name]
        ), _signature_text(AutoNudgeService, name)

    @pytest.mark.parametrize("name", sorted(_MOVED_TO_STORE))
    def test_a_store_member_moved_with_its_kind_and_signature(self, name: str) -> None:
        from kiro_crew.autonudge_service.store import LoopStore

        assert not hasattr(AutoNudgeService, name)
        assert _signature_row(LoopStore, _MOVED_TO_STORE[name]) == _BASE_SERVICE_MEMBERS[name]

    @pytest.mark.parametrize("name", sorted(_BASE_SERVICE_MEMBERS))
    def test_a_service_member_s_annotations_still_resolve(self, name: str) -> None:
        """Every annotation resolved on the pre-split class, so ``typing.get_type_hints``
        still resolves it wherever the member lives now."""
        from kiro_crew.autonudge_service.store import LoopStore

        owner, member = (
            (LoopStore, _MOVED_TO_STORE[name])
            if name in _MOVED_TO_STORE
            else (AutoNudgeService, name)
        )
        raw = inspect.getattr_static(owner, member)
        function = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
        typing.get_type_hints(function)

    def test_the_service_has_no_member_the_manifest_does_not_name(self) -> None:
        members = {
            name
            for name, value in vars(AutoNudgeService).items()
            if (not name.startswith("__") or name == "__init__")
            and (callable(value) or isinstance(value, (staticmethod, classmethod)))
        }
        assert members == set(_BASE_SERVICE_MEMBERS) - set(_MOVED_TO_STORE)

    @pytest.mark.parametrize("name", sorted(_BASE_VIEW_MEMBERS))
    def test_a_maintenance_view_member_keeps_its_kind_and_signature(self, name: str) -> None:
        assert _signature_row(_AutoNudgeMaintenanceView, name) == _BASE_VIEW_MEMBERS[name]

    def test_nudge_loop_keeps_its_fields_order_and_defaults(self) -> None:
        def _default(item: dataclasses.Field) -> str | None:
            if item.default is not dataclasses.MISSING:
                return repr(item.default)
            if item.default_factory is not dataclasses.MISSING:
                return "factory:" + item.default_factory.__name__
            return None

        fields = [(item.name, _default(item)) for item in dataclasses.fields(NudgeLoop)]
        assert fields == _BASE_NUDGELOOP_FIELDS

    def test_importing_the_facade_keeps_the_heavy_graphs_lazy(self) -> None:
        """``mcp_tools/control.py`` and ``workflows/service.py`` import the facade at the
        top, so everything it pulls in is paid by every MCP server process."""
        code = (
            "import sys, kiro_crew.autonudge\n"
            "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('kiro_crew'))))"
        )
        loaded = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, check=True, **UTF8_TEXT
        ).stdout.split()
        for lazy in (
            "kiro_crew.autonudge_judge",
            "kiro_crew.autonudge_authz",
            "kiro_crew.autonudge_selfarm",
            "kiro_crew.autonudge_provider_trust",
            "kiro_crew.decisions",
            "kiro_crew.config.live",
            "kiro_crew.dashboard.state",
            "kiro_crew.messaging",
        ):
            assert lazy not in loaded, f"importing the facade loaded {lazy}"


async def _write_golden_scenario(base: Path) -> AutoNudgeService:
    """Load a store carrying every row shape, then arm one gated and one structured loop."""
    (base / "autonudge.json").write_text(
        json.dumps(
            {
                "version": 1,
                "loops": [
                    {
                        "id": "keepme01",
                        "slot_key": "chat-1-100",
                        "message": "keep watching",
                        "idle_secs": 600,
                        "max_cycles": 5,
                        "cycle_count": 2,
                        "active": True,
                        "last_fire_ts": _FIXED_NOW - 50,
                        "created_ts": _FIXED_NOW - 1000,
                        "stop_sentinel_path": "",
                        "next_due_ts": _FIXED_NOW + 300,
                    },
                    {
                        "id": "claim001",
                        "slot_key": "chat-2-200",
                        "message": "was mid fire",
                        "idle_secs": 60,
                        "cycle_count": 3,
                        "active": True,
                        "created_ts": _FIXED_NOW - 500,
                        "next_due_ts": 0.0,
                        "inflight_cycle": 4,
                        "inflight_undelivered": True,
                    },
                    {"id": _HELD_ID, "slot_key": "chat-3-300", "message": "held aside"},
                    {"id": "broken01", "slot_key": "chat-4-400"},
                ],
            }
        ),
        encoding="utf-8",
    )
    svc = AutoNudgeService(base_dir=base)
    await asyncio.get_running_loop().run_in_executor(None, svc._load)
    await svc._persist_locked()
    await svc.add(
        "chat-5-500",
        "babysit https://github.com/octo/repo/pull/42 until merged",
        idle_secs=300,
        max_cycles=24,
        max_runtime_secs=3600,
        banner="patrol",
        gate=True,
        judge={
            "wake_when": "a reviewer asks for changes",
            "targets": ["https://github.com/octo/repo/pull/42"],
        },
        loop_id="gated001",
        self_armed=True,
    )
    await svc.add_monitor(
        slot_key="chat-6-600",
        kind="github_pull_request",
        target="octo/repo#7",
        objective="review_ready",
        cadence_secs=900,
        budgets=MonitorBudgets(
            max_runtime_secs=7200, max_agent_turns=4, max_tokens=100000, max_provider_errors=3
        ),
        wake_instructions="fix what is red",
        now=_FIXED_NOW,
        loop_id="struct01",
        creation_surface=MonitorCreationSurface.DASHBOARD,
    )
    return svc


class TestStoreBytes:
    @pytest.mark.asyncio
    async def test_the_store_and_sidecar_bytes_are_unchanged(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
        monkeypatch.setattr(time, "time", lambda: _FIXED_NOW)
        svc = await _write_golden_scenario(tmp_path)
        try:
            raw = (tmp_path / "autonudge.json").read_text(encoding="utf-8")
            assert _MONITOR_OBJECT.sub('      "monitor": <MONITOR>,\n', raw) == _GOLDEN_STORE
            rows = {row["id"]: row for row in json.loads(raw)["loops"]}
            for loop_id in ("gated001", "struct01"):
                loop = svc.get_by_id(loop_id)
                assert loop is not None and loop.monitor is not None
                assert rows[loop_id]["monitor"] == monitor_state_to_dict(loop.monitor)
            sidecar = (tmp_path / "autonudge.quarantine.json").read_text(encoding="utf-8")
            assert sidecar == _GOLDEN_SIDECAR
        finally:
            svc.stop()


def _legacy(loop_id: str = "loop0001", **changes: object) -> NudgeLoop:
    values: dict[str, object] = {
        "id": loop_id,
        "slot_key": f"chat-1-{loop_id}",
        "message": "watch",
        "idle_secs": 600,
        "created_ts": time.time(),
    }
    values.update(changes)
    return NudgeLoop(**values)  # type: ignore[arg-type]


class TestModuleSeamsReachTheCodeThatReadsThem:
    """A patch on ``kiro_crew.autonudge.<name>`` reaches each place that reads the name."""

    def test_the_overdue_beat(self, tmp_path, monkeypatch) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        delays: list[float | None] = []
        monkeypatch.setattr(svc, "_arm_timer", lambda loop, delay=None: delays.append(delay))
        monkeypatch.setattr(autonudge, "_OVERDUE_REARM_SECS", 7)
        svc._arm_from_deadline(_legacy(next_due_ts=time.time() - 5))
        assert delays == [7.0]

    @pytest.mark.asyncio
    async def test_the_reconcile_interval(self, tmp_path, monkeypatch) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        ev_loop = asyncio.get_running_loop()
        real_call_later = ev_loop.call_later
        waits: list[float] = []

        def _spy(delay, callback, *args):
            if callback is autonudge._resolve_beat:
                waits.append(delay)
                delay = 0
            return real_call_later(delay, callback, *args)

        monkeypatch.setattr(ev_loop, "call_later", _spy)
        monkeypatch.setattr(autonudge, "_RECONCILE_INTERVAL_SECS", 1234)
        passed = asyncio.Event()
        monkeypatch.setattr(svc, "_reconcile_once", passed.set)
        task = asyncio.create_task(svc._reconcile_forever())
        try:
            await asyncio.wait_for(passed.wait(), 10)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert waits[0] == 1234

    def test_the_store_write_helpers(self, tmp_path, monkeypatch) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        calls: list[tuple[str, str]] = []
        real_replace, real_fsync = autonudge.replace_with_retry, autonudge.fsync_dir

        def _replace(src, dst):
            calls.append(("replace", Path(dst).name))
            real_replace(src, dst)

        def _fsync(directory):
            calls.append(("fsync_dir", Path(directory).name))
            real_fsync(directory)

        monkeypatch.setattr(autonudge, "replace_with_retry", _replace)
        monkeypatch.setattr(autonudge, "fsync_dir", _fsync)
        svc._save()
        assert calls == [("replace", "autonudge.json"), ("fsync_dir", tmp_path.name)]

    @pytest.mark.asyncio
    async def test_the_judge_scrub(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            autonudge, "scrubbed_judge_spec", lambda spec: {"wake_when": "scrubbed"}
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            loop = await svc.add("chat-1-1", "watch", judge={"wake_when": "raw"})
            assert loop.judge == {"wake_when": "scrubbed"}
            loop.judge = {}
            await svc.update(loop.id, judge={"wake_when": "raw again"})
            assert loop.judge == {"wake_when": "scrubbed"}
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_published_instance(self, tmp_path, monkeypatch) -> None:
        live = AutoNudgeService(base_dir=tmp_path)
        monkeypatch.setattr(autonudge, "_INSTANCE", live)
        async with AutoNudgeService.maintenance_service(base_dir=tmp_path) as view:
            assert view._service is live
        monkeypatch.setattr(autonudge, "_INSTANCE", None)
        async with AutoNudgeService.maintenance_service(base_dir=tmp_path) as view:
            assert view._service is not live

    @pytest.mark.asyncio
    async def test_the_lock_registries(self, tmp_path, monkeypatch) -> None:
        locks: dict = {}
        owners: dict = {}
        monkeypatch.setattr(autonudge, "_MAINTENANCE_LOCKS", locks)
        monkeypatch.setattr(autonudge, "_MUTATION_LOCK_OWNERS", owners)
        lock = autonudge._maintenance_lock(tmp_path)
        assert list(locks.values()) == [lock]
        async with lock:
            autonudge._claim_mutation_lock(lock)
            assert owners == {lock: asyncio.current_task()}
            autonudge._release_mutation_lock(lock)
            await lock.acquire()
        assert owners == {}


class TestInstanceSeamsReachEveryInternalCaller:
    """Internal calls go through the instance, so a patch on it reaches each caller."""

    @pytest.mark.asyncio
    async def test_every_store_write_goes_through_write_state(self, tmp_path, monkeypatch) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        writes: list[list[str]] = []
        real = svc._write_state

        def _recording(payload):
            writes.append(sorted(row["id"] for row in payload["loops"]))
            real(payload)

        monkeypatch.setattr(svc, "_write_state", _recording)
        try:
            await svc.add("chat-1-1", "watch", loop_id="legacy01")
            await svc.update("legacy01", message="watch harder")
            await svc.add_monitor(
                slot_key="chat-2-2",
                kind="github_pull_request",
                target="octo/repo#1",
                objective="review_ready",
                cadence_secs=300,
                budgets=MonitorBudgets(),
                loop_id="struct01",
            )
            await svc.stop_monitor("struct01")
            await svc._persist_locked()
            await svc.remove("legacy01")
            svc.remove_sync("struct01")
        finally:
            svc.stop()
        both = ["legacy01", "struct01"]
        assert writes == [["legacy01"], ["legacy01"], both, both, both, ["struct01"], []]

    @pytest.mark.asyncio
    async def test_the_timer_reaches_the_gate_and_the_fire_cycle(
        self, tmp_path, monkeypatch
    ) -> None:
        fired: list[str] = []

        async def _on_fire(loop):
            fired.append(loop.id)
            return True

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=_on_fire)
        loop = _legacy("timer001")
        svc._loops[loop.id] = loop
        calls: list[str] = []

        async def _gate(target):
            calls.append("gate")
            return False

        real_cycle = svc._run_fire_cycle

        async def _cycle(target):
            calls.append("fire")
            await real_cycle(target)

        monkeypatch.setattr(svc, "_monitor_tick_is_quiet", _gate)
        monkeypatch.setattr(svc, "_run_fire_cycle", _cycle)
        try:
            await svc._timer(loop, 0)
        finally:
            svc.stop()
        assert (calls, fired, loop.cycle_count) == (["gate", "fire"], ["timer001"], 1)

    @pytest.mark.asyncio
    async def test_the_gate_reaches_the_judge(self, tmp_path, monkeypatch) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        asked: list[str] = []

        async def _judged(loop):
            asked.append(loop.id)
            return True

        monkeypatch.setattr(svc, "_judge_tick_is_quiet", _judged)
        loop = _legacy("judge001", gate=True)
        assert await svc._monitor_tick_is_quiet(loop) is True
        assert asked == ["judge001"]

    @pytest.mark.asyncio
    async def test_fire_now_and_the_deadline_arm_go_through_arm_timer(
        self, tmp_path, monkeypatch
    ) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        loop = _legacy("arm00001", next_due_ts=time.time() + 100)
        svc._loops[loop.id] = loop
        armed: list[tuple[str, float | None]] = []
        monkeypatch.setattr(
            svc, "_arm_timer", lambda target, delay=None: armed.append((target.id, delay))
        )
        assert (await svc.fire_now(loop.id))[2] == 200
        svc._arm_from_deadline(loop)
        assert armed[0] == ("arm00001", 0.0)
        assert armed[1][0] == "arm00001" and 0 < (armed[1][1] or 0) <= 100

    @pytest.mark.asyncio
    async def test_a_scheduled_persist_goes_through_persist_locked(
        self, tmp_path, monkeypatch
    ) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        persisted = asyncio.Event()

        async def _persist():
            persisted.set()

        monkeypatch.setattr(svc, "_persist_locked", _persist)
        svc._persist_soon()
        await asyncio.wait_for(persisted.wait(), 10)

    @pytest.mark.asyncio
    async def test_mutations_go_through_emit_cancel_and_the_mutation_lock(
        self, tmp_path, monkeypatch
    ) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        events: list[tuple[str, str | None]] = []
        cancelled: list[str] = []
        monkeypatch.setattr(
            svc, "_emit", lambda event, loop: events.append((event, loop and loop.id))
        )
        real_cancel = svc._cancel_timer

        def _cancel(loop_id, **kwargs):
            cancelled.append(loop_id)
            real_cancel(loop_id, **kwargs)

        monkeypatch.setattr(svc, "_cancel_timer", _cancel)
        try:
            loop = await svc.add("chat-1-1", "watch", loop_id="mut00001")
            svc.notify_user_input("chat-1-1")
            assert events == [("added", "mut00001")]
            assert cancelled[-1] == "mut00001"

            async def _claimed_by_maintenance(loop_id):
                return None

            monkeypatch.setattr(svc, "_acquire_mutation_lock", _claimed_by_maintenance)
            assert await svc.update(loop.id, message="changed") is None
            assert await svc.remove(loop.id) is False
            assert svc.get_by_id(loop.id) is loop and loop.message == "watch"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_structured_transitions_go_through_the_snapshot_writers(
        self, tmp_path, monkeypatch
    ) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        staged: list[str] = []
        written: list[int] = []
        real_staged = svc._persist_staged_monitor_locked
        real_snapshot = svc._write_monitor_snapshot_locked

        async def _staged(loop, replacement):
            staged.append(loop.id)
            await real_staged(loop, replacement)

        async def _snapshot(payload=None):
            written.append(len(payload["loops"]) if payload else -1)
            await real_snapshot(payload)

        monkeypatch.setattr(svc, "_persist_staged_monitor_locked", _staged)
        monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", _snapshot)
        await svc.add_monitor(
            slot_key="chat-2-2",
            kind="github_pull_request",
            target="octo/repo#1",
            objective="review_ready",
            cadence_secs=300,
            budgets=MonitorBudgets(),
            loop_id="struct01",
        )
        await svc.stop_monitor("struct01")
        assert staged == ["struct01"]
        assert written == [1, 1]

    @pytest.mark.asyncio
    async def test_start_goes_through_load_and_update_through_the_unserialized_body(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
        loaded: list[str] = []
        real_load = AutoNudgeService._load

        def _load(self):
            loaded.append("load")
            real_load(self)

        monkeypatch.setattr(AutoNudgeService, "_load", _load)
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        try:
            loop = await svc.add("chat-1-1", "watch", loop_id="upd00001")
            bodies: list[str] = []
            real_body = svc._update_unserialized

            async def _body(loop_id, **kwargs):
                bodies.append(loop_id)
                return await real_body(loop_id, **kwargs)

            monkeypatch.setattr(svc, "_update_unserialized", _body)
            await svc.update(loop.id, idle_secs=900)
            removed: list[str] = []
            real_remove = svc._remove_unserialized

            async def _remove(loop_id, **kwargs):
                removed.append(loop_id)
                return await real_remove(loop_id, **kwargs)

            monkeypatch.setattr(svc, "_remove_unserialized", _remove)
            await svc.remove(loop.id)
        finally:
            svc.stop()
        assert (loaded, bodies, removed) == (["load"], ["upd00001"], ["upd00001"])

    def test_a_persisting_removal_goes_through_save(self, tmp_path, monkeypatch) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._loops["save0001"] = _legacy("save0001")
        saved: list[str] = []
        monkeypatch.setattr(svc, "_save", lambda: saved.append("save"))
        svc.remove_sync("save0001")
        assert saved == ["save"]

    def test_every_stored_row_goes_through_serialize_loop(self, tmp_path, monkeypatch) -> None:
        """The store builds the payload, but each row with the SERVICE's serializer."""
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._loops["rows0001"] = _legacy("rows0001")
        monkeypatch.setattr(svc, "_serialize_loop", lambda loop: {"id": loop.id, "patched": True})
        assert svc._serialized_loops() == [{"id": "rows0001", "patched": True}]
        svc._save()
        stored = json.loads((tmp_path / "autonudge.json").read_text())["loops"]
        assert stored == [{"id": "rows0001", "patched": True}]

    @pytest.mark.asyncio
    async def test_a_persist_goes_through_serialize_state(self, tmp_path, monkeypatch) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        monkeypatch.setattr(svc, "_serialize_state", lambda: {"version": 1, "loops": ["marker"]})
        await svc._persist_locked()
        assert json.loads((tmp_path / "autonudge.json").read_text())["loops"] == ["marker"]


#: ``service member -> owner module`` for every owner function bound on ``AutoNudgeService``.
#: A member bound under another name inside its owner is spelled out in ``_BOUND_AS``.
_BINDINGS = {
    "_serialize_loop": "store",
    "maintenance_service": "maintenance",
    "_begin_maintenance_quiesce": "maintenance",
    "_end_maintenance_quiesce": "maintenance",
    "_acquire_mutation_lock": "maintenance",
    "deactivate_and_wait": "maintenance",
    "_deactivate_and_wait_unserialized": "maintenance",
    "notify_approval_stalled": "timers",
    "notify_cycle_start_failed": "timers",
    "notify_cycle_landed": "timers",
    "notify_turn_complete": "timers",
    "notify_user_input": "timers",
    "_cancel_timer": "timers",
    "_arm_timer": "timers",
    "_arm_from_deadline": "timers",
    "_reconcile_forever": "timers",
    "_reconcile_once": "timers",
    "_commit_judge_pr_seen": "gate",
    "_publish_pr_observation": "gate",
    "_monitor_tick_is_quiet": "gate",
    "_terminal_still_holds": "gate",
    "_judge_quiet_streak_floor": "judge_tick",
    "_judge_tick_is_quiet": "judge_tick",
    "_record_judge_verdict": "judge_tick",
    "_withdraw_judge_suppression": "judge_tick",
    "_confirm_judge_delivery": "judge_tick",
    "_label_judge_delivery_locked": "judge_tick",
    "_append_judge_labels": "judge_tick",
    "_persist_judge_state": "judge_tick",
    "_timer": "firing",
    "_run_fire_cycle": "firing",
    "fire_now": "firing",
    "add": "mutations",
    "_mint_loop_id": "mutations",
    "_add_locked": "mutations",
    "_add_unserialized": "mutations",
    "update": "mutations",
    "_update_locked": "mutations",
    "_update_unserialized": "mutations",
    "remove_sync": "mutations",
    "_revoke_self_arm_for": "mutations",
    "_revoke_self_arm": "mutations",
    "remove": "mutations",
    "remove_by_slot": "mutations",
    "clear_terminal_monitor": "mutations",
    "_remove_unserialized": "mutations",
    "_revoke_provider_credentials_before_removal": "mutations",
    "_provider_credentials_authorized": "mutations",
    "_restore_provider_credentials": "mutations",
    "add_monitor": "monitor_records",
    "_add_monitor_locked": "monitor_records",
    "commit_monitor_replacement": "monitor_records",
    "rollback_monitor_replacement": "monitor_records",
    "_monitor_snapshot_with_replacement": "monitor_records",
    "_apply_staged_monitor": "monitor_records",
    "_persist_staged_monitor_locked": "monitor_records",
    "apply_monitor_probe": "monitor_records",
    "stop_monitor_if_budget_exhausted": "monitor_records",
    "_set_monitor_deadline": "monitor_records",
    "stop_monitor": "monitor_records",
    "mark_terminal_notification_delivered": "monitor_records",
    "retire_monitor_for_session_close": "monitor_records",
    "restore_monitor_after_failed_session_close": "monitor_records",
    "update_monitor": "monitor_records",
    "rollback_monitor_update": "monitor_records",
    "mark_monitor_action_in_flight": "monitor_records",
    "record_monitor_turn_completion": "monitor_records",
    "_apply_monitor_budget_stop": "monitor_records",
    "_apply_monitor_user_stop": "monitor_records",
    "_retain_accepted_terminal_completion": "monitor_records",
    "_waits_for_terminal_completion": "monitor_records",
    "_sync_terminal_completion_timer": "monitor_records",
    "record_monitor_dispatch_failure": "monitor_records",
    "monitor_dispatch_is_authorized": "monitor_records",
    "mark_monitor_turn_accepted": "monitor_records",
    "record_monitor_dispatch_busy": "monitor_records",
    "record_monitor_dispatched": "monitor_records",
    "record_monitor_completion_evidence_unavailable": "monitor_records",
    "_deactivate_unwired_monitor": "monitor_records",
}
_BOUND_AS = {"_serialize_loop": "LoopStore.serialize_loop"}

#: The owner modules, in the order each may import the ones before it.
_OWNER_ORDER = (
    "model",
    "subject",
    "store",
    "maintenance",
    "timers",
    "gate",
    "judge_tick",
    "firing",
    "mutations",
    "monitor_records",
)

#: The names owner code reads through ``kiro_crew.autonudge`` at call time: the four
#: tests patch there, and the four that stay defined there (a redactor-calling scrub
#: helper, the singleton, and the two lock registries the boot smoke tracks by name).
_SEAMS = frozenset(
    {
        "_INSTANCE",
        "_MAINTENANCE_LOCKS",
        "_MUTATION_LOCK_OWNERS",
        "_OVERDUE_REARM_SECS",
        "_RECONCILE_INTERVAL_SECS",
        "fsync_dir",
        "replace_with_retry",
        "scrubbed_judge_spec",
    }
)

_OWNER_DIR = Path(autonudge.__file__).resolve().parent / "autonudge_service"


def _owner_modules() -> list:
    import importlib

    return [importlib.import_module(f"kiro_crew.autonudge_service.{name}") for name in _OWNER_ORDER]


def _owner_trees() -> dict[str, ast.Module]:
    return {
        name: ast.parse((_OWNER_DIR / f"{name}.py").read_text(encoding="utf-8"))
        for name in _OWNER_ORDER
    }


def _function_local_facade_imports(tree: ast.Module) -> list[tuple[ast.AST, str]]:
    """Every ``from kiro_crew import autonudge as <alias>`` inside a function."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and inner.module == "kiro_crew":
                for alias in inner.names:
                    if alias.name == "autonudge":
                        found.append((node, alias.asname or "autonudge"))
    return found


class TestTheComposition:
    def test_the_package_lists_exactly_these_owners(self) -> None:
        present = sorted(p.stem for p in _OWNER_DIR.glob("*.py") if p.stem != "__init__")
        assert present == sorted(_OWNER_ORDER)

    def test_a_moved_name_is_the_owner_s_own_object(self) -> None:
        """One object per name: the facade binds what the owner defines, never a copy."""
        owners: dict[str, object] = {}
        for module in _owner_modules():
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            for node in tree.body:
                targets = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    targets = [node.name]
                elif isinstance(node, ast.Assign):
                    targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    targets = [node.target.id]
                for name in targets:
                    owners[name] = getattr(module, name)
        moved = [name for name in _BASE_DEFINED if name in owners]
        assert len(moved) >= 60  # a vacuity guard: the split moved most of the module
        assert [name for name in moved if getattr(autonudge, name) is not owners[name]] == []

    @pytest.mark.parametrize("name", sorted(_BINDINGS))
    def test_a_bound_member_is_its_owner_s_function(self, name: str) -> None:
        import importlib

        owner: object = importlib.import_module(f"kiro_crew.autonudge_service.{_BINDINGS[name]}")
        for attribute in _BOUND_AS.get(name, name).split("."):
            owner = getattr(owner, attribute)
        raw = inspect.getattr_static(AutoNudgeService, name)
        function = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
        assert function is owner

    def test_every_service_function_an_owner_defines_is_bound(self) -> None:
        """A function written for the service but never bound would be dead, and a second
        definition of a bound one would be a second rule."""
        for name, tree in _owner_trees().items():
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                first = (node.args.posonlyargs + node.args.args)[:1]
                annotated = first and first[0].arg in ("self", "cls")
                if annotated:
                    assert _BINDINGS.get(node.name) == name, f"{name}.{node.name} is not bound"
        assert set(_BINDINGS.values()) <= set(_OWNER_ORDER)

    def test_every_bound_owner_function_types_its_self_for_mypy(self) -> None:
        """mypy checks an owner function's body through its ``self`` / ``cls``
        annotation, and the facade drops that annotation from the runtime object once
        the function is bound, so the method's signature reads as the class body's."""
        import importlib

        for name, tree in _owner_trees().items():
            module = importlib.import_module(f"kiro_crew.autonudge_service.{name}")
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                first = (node.args.posonlyargs + node.args.args)[:1]
                if not first or first[0].arg not in ("self", "cls"):
                    continue
                wanted = "AutoNudgeService" if first[0].arg == "self" else "type[AutoNudgeService]"
                written = first[0].annotation
                assert written is not None and ast.unparse(written) == wanted, node.name
                bound = getattr(module, node.name)
                for function in (bound, inspect.unwrap(bound)):
                    assert first[0].arg not in function.__annotations__, node.name

    def test_the_facade_is_a_plain_module(self) -> None:
        assert type(sys.modules["kiro_crew.autonudge"]) is ModuleType
        assert "__getattr__" not in vars(autonudge)

    def test_a_star_import_still_exposes_every_public_name(self, tmp_path: Path) -> None:
        import importlib.util

        probe = tmp_path / "autonudge_star_probe.py"
        probe.write_text("from kiro_crew.autonudge import *  # noqa: F401,F403\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("autonudge_star_probe", probe)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        public = [name for name in _BASE_DEFINED if not name.startswith("_")]
        assert [name for name in public if not hasattr(module, name)] == []

    def test_every_owner_logs_on_the_service_channel(self) -> None:
        """Operators filter, and tests ``caplog``, on ``kiro_crew.autonudge``."""
        for module in _owner_modules():
            logger = vars(module).get("logger")
            if logger is not None:
                assert logger is logging.getLogger("kiro_crew.autonudge"), module.__name__

    def test_no_owner_holds_a_mutable_module_global(self) -> None:
        """Runtime state lives on the service or its store; a module-level container
        would be process state the boot smoke does not know about."""
        for module in _owner_modules():
            for name, value in vars(module).items():
                if name.startswith("__"):
                    continue
                assert not isinstance(value, (dict, list, set)), f"{module.__name__}.{name}"

    def test_the_store_state_lives_on_the_composed_store(self, tmp_path: Path) -> None:
        from kiro_crew.autonudge_service.store import LoopStore

        svc = AutoNudgeService(base_dir=tmp_path)
        assert isinstance(svc._store, LoopStore)
        assert svc._path is svc._store.path
        for gone in ("_quarantined", "_load_refused", "_unparsed_rows", "_stop_notes"):
            assert not hasattr(svc, gone), gone


class TestTheOwnersReadTheSeamsThroughTheFacade:
    def test_no_owner_imports_the_facade_or_a_later_owner_at_import_time(self) -> None:
        for position, (name, tree) in enumerate(_owner_trees().items()):
            later = {f"kiro_crew.autonudge_service.{o}" for o in _OWNER_ORDER[position:]}
            for node in tree.body:
                if isinstance(node, ast.ImportFrom):
                    assert node.module != "kiro_crew.autonudge", name
                    assert not (
                        node.module == "kiro_crew"
                        and any(a.name == "autonudge" for a in node.names)
                    ), name
                    assert node.module not in later, f"{name} imports {node.module}"
                if isinstance(node, ast.Import):
                    assert all(a.name != "kiro_crew.autonudge" for a in node.names), name

    def test_each_owner_imports_alone_without_the_facade(self) -> None:
        code = (
            "import importlib, sys\n"
            f"for name in {list(_OWNER_ORDER)!r}:\n"
            "    importlib.import_module('kiro_crew.autonudge_service.' + name)\n"
            "print('kiro_crew.autonudge' in sys.modules)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, check=True, **UTF8_TEXT
        ).stdout.strip()
        assert out == "False"

    def test_only_the_declared_seams_are_read_through_it(self) -> None:
        read: set[str] = set()
        for tree in _owner_trees().values():
            for function, alias in _function_local_facade_imports(tree):
                for node in ast.walk(function):
                    if (
                        isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == alias
                    ):
                        read.add(node.attr)
        assert read == _SEAMS

    def test_no_owner_reads_a_seam_from_its_own_namespace(self) -> None:
        """A seam read as a bare name would bypass a patch on the facade."""
        for name, tree in _owner_trees().items():
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                bare = [
                    n.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in _SEAMS
                ]
                assert not bare, f"{name}:{node.name} reads {bare} without the facade"

    def test_every_facade_patch_in_the_tree_targets_a_name_the_moved_code_can_see(self) -> None:
        """A test that patches ``kiro_crew.autonudge.<name>`` for a name owner code reads
        as its OWN global reaches that owner only if the name is a declared seam, so the
        scan of every test tree must find no other such target -- except the patches
        listed in ``_FACADE_PATCHES_FOR_CONSUMERS``, which aim at a caller that imports
        the name from the facade on each call and has no owner on the path under test."""
        root = Path(__file__).resolve().parents[1]
        read_bare: set[str] = set()
        for tree in _owner_trees().values():
            read_bare |= {
                n.id
                for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }
        moved = {name for name in _BASE_DEFINED if any(name in vars(m) for m in _owner_modules())}
        found = set()
        for path in list((root / "test").rglob("*.py")) + list(
            (root / "src/kiro_crew").rglob("tests/**/*.py")
        ):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "autonudge" not in text:
                continue
            for target in _facade_patch_targets(text):
                if target in moved and target in read_bare and target not in _SEAMS:
                    found.add((path.relative_to(root).as_posix(), target))
        assert found == set(_FACADE_PATCHES_FOR_CONSUMERS)


#: ``(test file, name)`` facade patches aimed at a consumer outside the service. Each one
#: was read at its call site: the consumer imports the name from ``kiro_crew.autonudge``
#: inside the function under test, so the patch reaches it, and the service is a stub there.
_FACADE_PATCHES_FOR_CONSUMERS = {
    # dashboard/session_directive_apply.py imports is_structured_monitor_loop per call;
    # these tests hand it a SimpleNamespace service, so no owner code runs.
    ("test/test_autonudge_member_self_arm.py", "is_structured_monitor_loop"): "directive consumer",
}


def _facade_patch_targets(source: str) -> set[str]:
    """Names a test patches on ``kiro_crew.autonudge`` (setattr, mock.patch, patch.object)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    aliases = {"kiro_crew.autonudge"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "kiro_crew.autonudge":
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
            for alias in node.names:
                if alias.name == "autonudge":
                    aliases.add(alias.asname or "autonudge")
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called not in {"setattr", "patch", "object", "delattr"}:
            continue
        args = node.args
        if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
            text = args[0].value
            if text.startswith("kiro_crew.autonudge.") and text.count(".") >= 2:
                found.add(text.split(".")[2])
        elif (
            len(args) >= 2 and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str)
        ):
            owner = ast.unparse(args[0])
            if owner in aliases:
                found.add(args[1].value)
    return found


def test_the_patch_target_scan_answers_both_ways() -> None:
    sample = (
        "import kiro_crew.autonudge as an\n"
        "from kiro_crew import autonudge as _an\n"
        "def t(monkeypatch):\n"
        "    monkeypatch.setattr(an, '_MAX_QUIET_STREAK', 3)\n"
        "    monkeypatch.setattr('kiro_crew.autonudge._OVERDUE_REARM_SECS', 1)\n"
        "    mock.patch.object(_an, 'infer_subject')\n"
        "    monkeypatch.setattr(other, '_MAX_IDLE_SECS', 1)\n"
        "    monkeypatch.setattr('kiro_crew.autonudge_judge.owner_acted', f)\n"
    )
    assert _facade_patch_targets(sample) == {
        "_MAX_QUIET_STREAK",
        "_OVERDUE_REARM_SECS",
        "infer_subject",
    }
