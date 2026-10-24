"""Folds over ONE session's crew log -- the side panel's five views, and the ledger.

The panel reads five, and those five are what the growth push carries. ``class`` is a
sixth registered fold that is deliberately NOT advertised: its reader is
a session deciding whether it may read ANOTHER unit's log, which is why it is held at
the most restrictive value the log ever recorded rather than at the current one.

A projection folds one crew log and carries that log's ``seq`` as its version
(RFC FR-5), so a reader that holds a projection at seq N and reads the entries
after N reaches the same value a reader folding the whole file from scratch
does. That equality is the module's contract and the property its tests pin.

The INCREMENTAL form is the primitive here and the whole-file form wraps it. A
fold is three pure pieces -- a starting state, one step per entry, and a render
-- and :func:`fold` is those pieces run over every entry. A separate batch
implementation would be a second codepath that can disagree with the resumed
one about the same bytes, and nothing in the file would say which is right.

State is JSON-serializable and is the CHECKPOINT: a caller may store it, hand it
back later with the seq it was taken at, and continue. It is deliberately not
the rendered value. A fold keeps bookkeeping a reader has no use for (the open
tool calls it is matching by ``call_id``, the attempt an open turn is on), and
:func:`Checkpoint.state` holding exactly what the fold needs to continue is what
lets the render stay the surface the dashboard reads.
:mod:`kiro_crew.crew_log.checkpoint` writes that state beside the log, so a read
resumes where the last one stopped; this module owns no path and every failure
over there is answered by folding from seq 1 again.

Absent is never read as zero. ``turn/completed`` carries ``credits`` and
``tokens`` only when the provider reported them -- a synthesized closer omits
both, a measured one omits whichever went unreported -- and a total that counted
those turns as costing nothing would state a measurement nobody made. Each total
therefore rides beside the count of turns that contributed to it, and a caller
comparing the two learns what the total covers.

Nothing here synthesizes history. An interrupted turn and an unmatched tool call
are reported as OPEN, never closed with an invented outcome: closing them is the
store's ``repair=True``, which appends real deterministic closers under write
ownership, and a reader inventing the same fact in memory would make two readers
of one file disagree.

This module reads its own unit's file and nothing else (FR-4: no fold reads more
than its own crew log) -- with ONE stated exception, and it is stated because a
reader has to know which kind of fold it is holding. The ``ledger`` fold is keyed
by SLOT, and a slot owns one ACP session id at a time rather than for its whole
life, so the record it answers for is spread over a unit per id the slot ran
under. It therefore joins those units (:func:`fold_slot`), which is a wider read
than the six panel folds make and is why it is not one of them: the growth push
and the side panel address a session, and a slot-wide value pushed under one
session's id would report a partial answer as the whole one. The units it joins
are still exactly one slot's own, so nothing here reads across slots.

Resolving a ``ref`` is the PAGE path's work, in the routes that serve a person a
citation to follow.
"""

# This module is the facade of the crew-log folds and every name is reached through
# it as ``kiro_crew.crew_log.projection.<name>``: a read is served by the owner that
# binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches
# every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this
# module. New work goes to the owner of its responsibility:
#
#   ``projection_catalogue`` -- every registered fold name by key kind, ``_Fold``, ``Projection``
#   ``projection_session_log`` -- opening one session's log and stating its identity
#   ``projection_values`` -- the bounded coercions every fold applies to entry data
#   ``projection_side_panel`` -- the ``status``, ``timeline``, ``tools`` and ``approvals`` folds
#   ``projection_usage`` -- the ``usage`` fold and the credit table it bills by
#   ``projection_usage_measured`` -- the agent engine's per-turn measurement in ``usage``
#   ``projection_subagents`` -- the ``subagents`` fold
#   ``projection_ledger`` -- the ``ledger`` slot fold (a session's work ledger)
#   ``projection_internal`` -- the ``class`` and ``outline`` folds, which no panel draws
#   ``projection_radar`` -- the ``radar`` slot fold (the Issue Radar crew ledger)
#   ``projection_dashboard`` -- the ``panel``, ``agentic`` and ``mistakes`` slot folds
#   ``projection_work`` -- the ``work`` slot fold and the units a board spans
#   ``projection_workstreams`` -- the ``workstreams`` slot fold and its board closure
#   ``projection_worktree`` -- the ``worktree`` tree fold
#   ``projection_registry`` -- ``_FOLDS`` and what each fold touches, copies and retains
#   ``projection_kernel`` -- ``Checkpoint`` and the pure fold API over the registry
#   ``projection_budget`` -- the warm memo's byte budget: measured row costs and the ceiling
#   ``projection_session`` -- folding one session's log on the projection kernel, with its savepoint
#   ``projection_slot_memo`` -- a slot's folds on the projection kernel, kept warm
#   ``projection_readers`` -- reading a slot's and a tree's folds
#   ``projection_session_memo`` -- a session's folds, kept warm, and the session read

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.crew_log.projection_catalogue",
    "kiro_crew.crew_log.projection_session_log",
    "kiro_crew.crew_log.projection_values",
    "kiro_crew.crew_log.projection_otel_usage",
    "kiro_crew.crew_log.projection_otel_session_tree",
    "kiro_crew.crew_log.projection_side_panel",
    "kiro_crew.crew_log.projection_usage_measured",
    "kiro_crew.crew_log.projection_usage",
    "kiro_crew.crew_log.projection_subagents",
    "kiro_crew.crew_log.projection_ledger",
    "kiro_crew.crew_log.projection_internal",
    "kiro_crew.crew_log.projection_radar",
    "kiro_crew.crew_log.projection_dashboard",
    "kiro_crew.crew_log.projection_crewmate",
    "kiro_crew.crew_log.projection_work",
    "kiro_crew.crew_log.projection_workers",
    "kiro_crew.crew_log.projection_workstreams",
    "kiro_crew.crew_log.projection_worktree",
    "kiro_crew.crew_log.projection_registry",
    "kiro_crew.crew_log.projection_kernel",
    "kiro_crew.crew_log.projection_budget",
    "kiro_crew.crew_log.projection_session",
    "kiro_crew.crew_log.projection_slot_memo",
    "kiro_crew.crew_log.projection_readers",
    "kiro_crew.crew_log.projection_session_memo",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.crew_log.projection_budget import (  # noqa: F401
        _SLOT_FOLD_ROW_BYTES,
        _TREE_FOLD_ROW_BYTES,
        _UNMEASURED_ROW_BYTES,
        _UNMEASURED_TREE_FOLDS,
        DEFAULT_SLOT_FOLD_CACHE_BYTES,
        SLOT_FOLD_CACHE_BYTES_ENV,
        slot_fold_cache_bytes,
        slot_fold_cell_bytes,
        slot_fold_row_bytes,
    )
    from kiro_crew.crew_log.projection_catalogue import (  # noqa: F401
        _FOLD_STATE_VERSION_BASE,
        FOLD_NAMES,
        INTERNAL_PROJECTION_NAMES,
        INTERNAL_SLOT_PROJECTION_NAMES,
        KNOWN_TYPES,
        OWNER_SERVED_SLOT_PROJECTION,
        PROJECTION_NAMES,
        SESSION_FOLD_NAMES,
        SLOT_PROJECTION_NAMES,
        TREE_PROJECTION_NAMES,
        WORKER_FOLD_NAME,
        WORKERS_FOLD_NAME,
        WORKSTREAMS_FOLD_NAME,
        WORKTREE_FOLD_NAME,
        Projection,
        _Fold,
        logger,
    )
    from kiro_crew.crew_log.projection_crewmate import (  # noqa: F401
        _MILESTONE_TEXT_KEYS,
        _WAITING_TEXT_KEYS,
        _goal_render,
        _goal_rows,
        _goal_start,
        _goal_step,
        _list,
        _options_total,
        _rows,
        _text,
        _waiting_render,
        _waiting_rows,
        _waiting_start,
        _waiting_step,
    )
    from kiro_crew.crew_log.projection_dashboard import (  # noqa: F401
        PANEL_SCHEMA_VERSION,
        _agentic_render,
        _agentic_rows,
        _agentic_start,
        _agentic_step,
        _dashboard_type_holds,
        _dashboard_value_fits,
        _mistake_key,
        _mistakes_render,
        _mistakes_rows,
        _mistakes_start,
        _mistakes_step,
        _panel_iso,
        _panel_owner_record,
        _panel_owner_start,
        _panel_render,
        _panel_start,
        _panel_step,
        _panel_text,
    )
    from kiro_crew.crew_log.projection_internal import (  # noqa: F401
        OUTLINE_PROMPT_CHARS,
        OUTLINE_REPLY_CHARS,
        OUTLINE_TURN_LIMIT,
        OUTLINE_TYPES,
        _class_absorb,
        _class_read,
        _class_render,
        _class_start,
        _class_step,
        _outline_copy,
        _outline_preview,
        _outline_render,
        _outline_row,
        _outline_start,
        _outline_step,
    )
    from kiro_crew.crew_log.projection_kernel import (  # noqa: F401
        Checkpoint,
        _state_matches_fold,
        advance,
        fold,
        fold_approvals,
        fold_state_version,
        fold_status,
        fold_timeline,
        fold_tools,
        fold_usage,
        initial,
        projection_of,
        require_name,
        state_is_serializable,
    )
    from kiro_crew.crew_log.projection_ledger import (  # noqa: F401
        _ledger_field,
        _ledger_iso,
        _ledger_render,
        _ledger_start,
        _ledger_step,
    )
    from kiro_crew.crew_log.projection_otel_session_tree import (  # noqa: F401
        _TREE_AGENT_KIND_UNSET,
        OTEL_SESSION_TREE_FOLD_NAME,
        OTEL_SESSION_TREE_TYPES,
        SESSION_SPAWNED_TYPE,
        _otel_session_tree_copy,
        _otel_session_tree_render,
        _otel_session_tree_start,
        _otel_session_tree_step,
        _tree_count,
    )
    from kiro_crew.crew_log.projection_otel_usage import (  # noqa: F401
        _AGENT_KIND_BY_STEM,
        _AGENT_KIND_UNSET,
        OTEL_USAGE_FOLD_NAME,
        OTEL_USAGE_TYPES,
        SESSION_OPENED_TYPE,
        TURN_STARTED_TYPE,
        _agent_kind,
        _agent_kind_by_name,
        _check_agent_kind_table_current,
        _otel_usage_copy,
        _otel_usage_render,
        _otel_usage_start,
        _otel_usage_step,
    )
    from kiro_crew.crew_log.projection_radar import (  # noqa: F401
        RADAR_EVENT_LIMIT,
        RADAR_ITEM_LIMIT,
        RADAR_PHASE_LINE_LIMIT,
        RADAR_SCHEMA_VERSION,
        RADAR_SKIP_LIMIT,
        RADAR_TEXT_LIMIT,
        RADAR_TRIED_LIMIT,
        _radar_bound_items,
        _radar_event_id,
        _radar_int,
        _radar_iso,
        _radar_new_item,
        _radar_number,
        _radar_payload_digest,
        _radar_record_skip,
        _radar_render,
        _radar_start,
        _radar_step,
        _radar_text,
        radar_ci_state,
    )
    from kiro_crew.crew_log.projection_readers import (  # noqa: F401
        _WORKERS_READ_LOCKS,
        _WORKERS_REVISIONS,
        _WORKERS_REVISIONS_LOCK,
        _WORKERS_REVISIONS_MAX,
        _read_workers,
        _read_workers_locked,
        _slot_units_for_fold,
        _supplemental_units,
        fold_slot,
        fold_slot_checkpoint,
        fold_tree_checkpoint,
        read_slot_projection,
        read_tree_projection,
        tree_fold_units,
        units_in_succession,
    )
    from kiro_crew.crew_log.projection_registry import (  # noqa: F401
        _FOLDS,
        _MISKEYED_FOLDS,
        _ORPHAN_EAGER,
        _UNCOUNTED_SLOT_FOLDS,
        APPROVAL_TYPES,
        EAGER_FOLD_NAMES,
        EAGER_SESSION_FOLD_NAMES,
        EAGER_SLOT_FOLD_NAMES,
        LAZY_FOLD_REASONS,
        TOOL_TYPES,
        USAGE_TYPES,
        WORKSTREAMS_TYPES,
        _approvals_copy,
        _flat_copy,
        _ledger_rows,
        _panel_rows,
        _radar_rows,
        _subagents_copy,
        _timeline_copy,
        _tools_copy,
        _usage_copy,
        _work_opaque,
        _work_rows,
    )
    from kiro_crew.crew_log.projection_session import (  # noqa: F401
        SessionProjections,
        _cells_as_checkpoints,
        _drive_session,
        _fold_attempt,
        _HeldCheckpoints,
        _note_session_fold_origin,
        _persisted,
        _resume_from_disk,
        _session_registry,
        _SessionFold,
        _tail_reader,
        empty_session,
        fold_session,
    )
    from kiro_crew.crew_log.projection_session_log import (  # noqa: F401
        _log_identity,
        log_origin,
        open_session_log,
    )
    from kiro_crew.crew_log.projection_session_memo import (  # noqa: F401
        _SESSION_LOCK_TAG,
        DEFAULT_SESSION_FOLD_CACHE_BYTES,
        SESSION_FOLD_CACHE_BYTES_ENV,
        WarmSession,
        _fold_session_warm_locked,
        _remember_session_fold,
        _session_memo_guard,
        _session_memos,
        _SessionMemo,
        fold_session_warm,
        forget_session_folds,
        read_projection,
        session_fold_cache_bytes,
        session_fold_memo_bytes,
        session_fold_state_bytes,
        slot_of_session,
    )
    from kiro_crew.crew_log.projection_side_panel import (  # noqa: F401
        OPEN_LIST_LIMIT,
        OPEN_RETAIN_LIMIT,
        SERVERS_PER_TOOL_LIMIT,
        TIMELINE_DETAIL_KEYS,
        TIMELINE_LIMIT,
        TIMELINE_MOMENT_KEYS,
        TIMELINE_TYPES,
        TOOL_NAME_LIMIT,
        _approvals_render,
        _approvals_start,
        _approvals_step,
        _status_render,
        _status_start,
        _status_step,
        _timeline_render,
        _timeline_start,
        _timeline_step,
        _tool_row,
        _tools_render,
        _tools_start,
        _tools_step,
    )
    from kiro_crew.crew_log.projection_slot_memo import (  # noqa: F401
        _ALL_RECORDS,
        _CUT,
        _UNCUT,
        _UNPROVEN,
        OVERSIZE_SLOT_CELL_LIMIT,
        _acquire_fold_lock,
        _cell_weight,
        _continuable,
        _cut_verdict,
        _digest_to_carry,
        _fold_slot_warm_locked,
        _folded_marks,
        _folded_prefix_holds,
        _FoldLock,
        _identity_marks,
        _minted_revision,
        _next_slot_revision,
        _note_slot_fold_idle,
        _note_slot_fold_origin,
        _Ordinal,
        _ordinal_seq,
        _oversize_slot_cells,
        _persist_slot_fold,
        _prefix_holds,
        _PrefixSeen,
        _release_fold_lock,
        _remember_slot_fold,
        _resume_from,
        _resume_slot_fold,
        _slot_cell,
        _slot_checkpoint,
        _slot_fold_locks,
        _slot_memo_bytes,
        _slot_memo_guard,
        _slot_memos,
        _slot_revision,
        _slot_witness,
        _SlotFold,
        _SlotMemo,
        _SlotStream,
        _unit_mark,
        _unit_marks,
        _unit_prefix,
        _UnitMark,
        _vouched,
        fold_slot_warm,
        fold_slot_warm_revised,
        forget_slot_folds,
        slot_fold_over_ceiling,
    )
    from kiro_crew.crew_log.projection_subagents import (  # noqa: F401
        _SUBAGENT_OUTCOMES,
        SUBAGENT_ROW_KEYS,
        SUBAGENT_TYPES,
        _subagent_credits,
        _subagents_render,
        _subagents_start,
        _subagents_step,
    )
    from kiro_crew.crew_log.projection_usage import (  # noqa: F401
        _CREDIT_SOURCE_OF,
        CONTEXT_ORDINAL_MEMO_LIMIT,
        CONTEXT_SOURCES_PER_TURN_LIMIT,
        CONTEXT_TURNS_LIMIT,
        CREDIT_SOURCES,
        MODEL_LIMIT,
        TOKEN_DIMENSIONS,
        _bill_credits,
        _count_turn_ordinal,
        _credit_charge,
        _usage_render,
        _usage_start,
        _usage_step,
    )
    from kiro_crew.crew_log.projection_usage_measured import (  # noqa: F401
        _HISTORY_KEYS,
        _INDEX,
        _NORMAL_STOPS,
        _SLOTS,
        _TTFT_KEYS,
        FLAG_BREAKDOWN,
        FLAG_COMPACTED,
        FLAG_RERUN,
        FLAG_RESET,
        FLAG_SCHEMA_UNKNOWN,
        MEASURED_FIELDS,
        MEASURED_FORMAT,
        MEASURED_UNKNOWN_LIMIT,
        _decode_values,
        _encode,
        _note_unknown,
        decode_measured,
        measured_start,
        measured_step,
        note_closed,
        note_composed,
        note_unit_start,
    )
    from kiro_crew.crew_log.projection_values import (  # noqa: F401
        ID_LIMIT,
        TEXT_LIMIT,
        _as_id,
        _as_int,
        _as_str,
        _as_text_or_none,
        _keyable,
        _note_omitted,
        _rows_in,
    )
    from kiro_crew.crew_log.projection_work import (  # noqa: F401
        _WORK_BASELINE_FIELDS,
        _WORK_CLOSED_STATES,
        _WORK_CONDUCTOR_FIELDS,
        _WORK_TEXT_LIMITS,
        _WORK_WORKER_FIELDS,
        WORK_EVENT_LIMIT,
        WORK_EVENT_TEXT_LIMIT,
        WORK_ITEM_LIMIT,
        WORK_PARKED_LIMIT,
        _work_add_event,
        _work_apply,
        _work_apply_header,
        _work_bind_slot,
        _work_field,
        _work_forget,
        _work_is_progress,
        _work_iso,
        _work_new_item,
        _work_reason,
        _work_render,
        _work_reset_board,
        _work_set_round,
        _work_settle,
        _work_stamp,
        _work_start,
        _work_step,
        _work_text,
        _work_units,
        work_slots_naming_board,
    )
    from kiro_crew.crew_log.projection_workers import (  # noqa: F401
        _IDLE_OUTCOMES,
        _PREAMBLE_PREFIX,
        _RELAY_PREFIX,
        _TASK_LABELS,
        STATE_BLOCKED,
        STATE_CLOSED,
        STATE_DONE,
        STATE_FAILED,
        STATE_IDLE,
        STATE_RELEASED,
        STATE_RUNNING,
        STATE_STARTING,
        WORKER_ROW_KEYS,
        WORKER_STATES,
        WORKER_TYPES,
        WORKERS_LIMIT,
        WORKERS_TEXT_LIMIT,
        WORKERS_TYPES,
        WORKERS_UNITS_PER_CHILD,
        _blank_row,
        _clip,
        _drop,
        _first_line,
        _labelled_task,
        _last_line,
        _let_go,
        _row,
        _turn_end_state,
        _worker_bind_slot,
        _worker_copy,
        _worker_render,
        _worker_rows,
        _worker_start,
        _worker_step,
        _workers_bind_slot,
        _workers_copy,
        _workers_render,
        _workers_rows,
        _workers_start,
        _workers_step,
        merge_render,
        tree_seeded,
        worker_units,
        workers_children,
        workers_units,
    )
    from kiro_crew.crew_log.projection_workstreams import (  # noqa: F401
        _WORKSTREAMS_DROPPED_SLOTS,
        _WORKSTREAMS_UNITS_DROPPED,
        WORKSTREAMS_BOARD_LIMIT,
        WORKSTREAMS_DECISION_LIMIT,
        WORKSTREAMS_EVENT_BUDGET,
        WORKSTREAMS_EVENT_LIMIT,
        WORKSTREAMS_SERIES_LIMIT,
        WORKSTREAMS_SPENDER_LIMIT,
        WORKSTREAMS_TASK_LIMIT,
        WORKSTREAMS_UNIT_LIMIT,
        _board_closure_units,
        _workstreams_advanced,
        _workstreams_bind_slot,
        _workstreams_binds_in,
        _workstreams_board_of,
        _workstreams_bound_tasks,
        _workstreams_bump,
        _workstreams_copy,
        _workstreams_event,
        _workstreams_hour,
        _workstreams_item_state,
        _workstreams_merge,
        _workstreams_new_board,
        _workstreams_new_item,
        _workstreams_render,
        _workstreams_rows,
        _workstreams_series,
        _workstreams_span_closed,
        _workstreams_span_opened,
        _workstreams_spend,
        _workstreams_start,
        _workstreams_step,
        _workstreams_tails,
        _workstreams_units,
        _workstreams_units_dropped,
        _workstreams_work,
    )
    from kiro_crew.crew_log.projection_worktree import (  # noqa: F401
        WORKTREE_BOARD_LIMIT,
        WORKTREE_GOAL_CHARS,
        WORKTREE_OWNER_LIMIT,
        _worktree_bind_slot,
        _worktree_copy,
        _worktree_new_board,
        _worktree_render,
        _worktree_rows,
        _worktree_start,
        _worktree_step,
    )

if TYPE_CHECKING:  # names it serves from outside the fold family, from their homes
    from kiro_crew import (  # noqa: F401
        session_ledger,
    )
    from kiro_crew.config.paths import (  # noqa: F401
        data_home,
    )
    from kiro_crew.context_blocks import (  # noqa: F401
        PHASE_SESSION_START,
    )
    from kiro_crew.crew_log.entry_types import (  # noqa: F401
        CREWMATE_OPTION_LIMIT,
        CREWMATE_ROW_LIMIT,
        CREWMATE_TEXT_LIMIT,
        CREWMATE_WAITING_LIMIT,
        DASHBOARD_AGENTIC_ENTRY_TYPE,
        DASHBOARD_CODE_LIMIT,
        DASHBOARD_FIELD_LIMIT,
        DASHBOARD_FOLD_NAME,
        DASHBOARD_REASON_LIMIT,
        DASHBOARD_REFUSED_ENTRY_TYPE,
        DASHBOARD_TYPE_LIMIT,
        DASHBOARD_VALUE_BYTES,
        DASHBOARD_VALUE_LIMIT,
        GOAL_ENTRY_TYPE,
        GOAL_FOLD_NAME,
        MISTAKE_CORRECTED_CODE,
        MISTAKE_CORRECTION_LIMIT,
        MISTAKE_GROUP_LIMIT,
        MISTAKES_FOLD_NAME,
        PANEL_CREW_KEY_LIMIT,
        PANEL_ENTRY_TYPE,
        PANEL_FOLD_NAME,
        PANEL_HISTORY_LIMIT,
        PANEL_OWNER_LIMIT,
        PANEL_TEMPLATE_LIMIT,
        PANEL_TITLE_LIMIT,
        RADAR_CI_BOUNDS,
        RADAR_CI_KEYS,
        RADAR_CLEARABLE_FIELDS,
        RADAR_CREW_LEVEL_EVENT_KIND,
        RADAR_DEFAULT_SKIP_SCOPE,
        RADAR_EDITING_PHASES,
        RADAR_ENTRY_TYPE,
        RADAR_EVENT_KINDS,
        RADAR_LABELS_LIMIT,
        RADAR_NUMBER_BOUNDS,
        RADAR_PHASES,
        RADAR_SKIP_SCOPES,
        RADAR_TERMINAL_PHASES,
        SESSION_ENTRY_TYPES,
        WAITING_ENTRY_TYPE,
        WAITING_FOLD_NAME,
        WORK_ENTRY_TYPE,
    )
    from kiro_crew.crew_log.errors import (  # noqa: F401
        CODE_BAD_DATA,
        CrewLogError,
    )
    from kiro_crew.crew_log.otel_session_tree_fold import (  # noqa: F401
        depth_observation,
    )
    from kiro_crew.crew_log.schema import (  # noqa: F401
        KIND_SESSION,
        MAX_ENTRY_BYTES,
        Entry,
    )
    from kiro_crew.crew_log.session_tree import (  # noqa: F401
        OpenedRecord,
        fold_citations,
        log_rank_of,
    )
    from kiro_crew.crew_log.store import (  # noqa: F401
        CrewLog,
        ReadCursor,
        ResumePoint,
        log_exception_text,
        segment_paths,
        session_units_by_slot,
        session_units_for_slot,
        unit_header_created_at,
        unit_opened_previous,
    )
    from kiro_crew.engine_context import (  # noqa: F401
        BREAKDOWN_SCHEMA,
        HISTORY_PARTS,
        KNOWN_CATEGORIES,
        MAX_COUNT,
        count,
    )
    from kiro_crew.projection import (  # noqa: F401
        ProjectionRegistry,
        Savepoint,
        attribute_seq,
    )
    from kiro_crew.session_ledger import _FOLD_NAME as LEDGER_FOLD_NAME  # noqa: F401
    from kiro_crew.session_ledger import (  # noqa: F401
        _MAX_ARTIFACT_KEY as LEDGER_ARTIFACT_KEY_LIMIT,
    )
    from kiro_crew.session_ledger import _MAX_ARTIFACTS as LEDGER_ARTIFACT_LIMIT  # noqa: F401
    from kiro_crew.session_ledger import _MAX_EVENTS as LEDGER_EVENT_LIMIT  # noqa: F401
    from kiro_crew.session_ledger import _MAX_PHASE as LEDGER_PHASE_LIMIT  # noqa: F401
    from kiro_crew.session_ledger import _MAX_TEXT as LEDGER_TEXT_LIMIT  # noqa: F401
    from kiro_crew.session_ledger import _MAX_TRIED as LEDGER_TRIED_LIMIT  # noqa: F401
    from kiro_crew.session_ledger import EVENT_KINDS as LEDGER_EVENT_KINDS  # noqa: F401
    from kiro_crew.session_ledger import (  # noqa: F401
        LEDGER_ENTRY_TYPE,
    )
    from kiro_crew.session_ledger import SCHEMA_VERSION as LEDGER_SCHEMA_VERSION  # noqa: F401
    from kiro_crew.session_ledger import TERMINAL_PHASES as LEDGER_TERMINAL_PHASES  # noqa: F401
    from kiro_crew.work_vocab import (  # noqa: F401
        WORK_BLOCKED_REASONS,
        WORK_CLOSED_WINDOW,
        WORK_CONDUCTOR_FIELDS,
        WORK_STORED_ITEM_LIMIT,
        WORK_TOMBSTONE_LIMIT,
        WorkBoardForgotten,
        WorkBoardItem,
        WorkBoardView,
    )
