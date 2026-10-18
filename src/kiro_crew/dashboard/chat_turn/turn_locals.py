"""The state one dashboard chat turn shares across its phases: :class:`_ChatTurn`, and
:class:`_Flow`, how a phase tells ``_run_chat`` to go on after it.

``kiro_crew.dashboard.chat_runner._run_chat`` builds one ``_ChatTurn`` per call, as its first
statement, and every value of the turn that a function it calls with the turn writes, or reads
after the turn rebinds it, lives on it rather than in a local of one function: such an input of
the turn, and such a value the turn computes. A function that writes such a value (the steer cut
writes the reply text from the steer handler's task) writes the attribute; the turn reads the
attribute; so every reader and writer shares one storage cell.

An attribute is declared on the class and set where the turn first binds the value, never
earlier, so reading one before the turn bound it raises ``UnboundLocalError``, with the message
reading the unbound local raised. A read of a closure cell nothing had bound raised
``NameError``; ``UnboundLocalError`` is a ``NameError``, so a handler of either still catches it.
The declarations are strings: the class body evaluates nothing, so a test that rebinds a name on
the runner still reaches every reader.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from asyncio import Task
    from asyncio.locks import Lock
    from collections.abc import Awaitable, Callable
    from typing import Any

    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        MonitorCompletionHook,
        RefusalInfo,
        ReplayFamily,
        StopClass,
        StructuredStatus,
        _ChatSlot,
        _TurnExit,
    )
    from kiro_crew.dashboard.chat_turn.turn_stats import _FirstVisibleClock
    from kiro_crew.dashboard.poisoned_escalation import PoisonedConversationEscalation
    from kiro_crew.dashboard.responsive_chat import ResponsiveTurn
    from kiro_crew.execution_context import ExecutionContext
    from kiro_crew.repeat_loop import RepeatLoopTracker
    from kiro_crew.security import StreamRedactor
    from kiro_crew.start_priority import StartPriority


class _Flow(enum.Enum):
    """How a phase cut from ``_run_chat`` tells the turn to go on: the statement it ran."""

    #: ``return``: ``_run_chat`` returns at the call; a ``finally`` around the call still runs.
    END = "end"
    #: ``break``: the stream loop stops.
    BREAK = "break"
    #: ``continue``: the stream loop takes the next event.
    CONTINUE = "continue"


class _ChatTurn:
    """One turn's inputs and the state its phases share; see the module docstring."""

    # The inputs of ``_run_chat`` the functions it calls with the turn read off it.
    state: "DashboardState"
    slot: "_ChatSlot"
    turn_exit: "_TurnExit"
    message: "str"
    _prompt_depth: "int"
    _attachments: "tuple[str, ...] | list[str]"
    _attachment_meta: "dict[str, list[str]] | None"
    _synthetic_payload: "bool"
    _replay: "frozenset[ReplayFamily]"
    _synthetic_recovery_turn: "bool"
    _replays_completion: "bool"
    _steer_possibly_delivered: "bool"
    _responsive_human: "bool"
    _responsive_protected: "bool"
    _directive_user_origin: "bool"
    _turn_provenance_restored: "bool"
    _directive_self_wake: "bool"
    _directive_loop_id: "str"
    _directive_loop_gen: "int"
    _directive_channel_origin: "bool"
    _commands_off: "bool"
    _turn_actor: "str"
    regenerate_hint: "str"
    _on_consumed: "Callable[[bool], None] | None"
    _on_irreversibly_consumed: "Callable[[], Awaitable[None] | None] | None"
    monitor_completion: "MonitorCompletionHook | None"
    _current_message: "dict | None"

    # State those functions write, or read after the turn rebinds it.
    _acquired: "bool"
    _activity_after_terminal: "int"
    _allocation_kwargs: "dict[str, Any]"
    _auth_required: "bool"
    _batch_cascade_steered: "bool"
    _builtin_identity_trusted: "bool"
    _child_lf_warning: "str"
    _compact_after_turn_noticed: "bool"
    _compaction_completed: "bool"
    _compaction_dropped_leak: "bool"
    _compaction_notice_chunks: "list[str]"
    _compaction_started: "bool"
    _consumed_reported: "bool"
    _context_is_new: "bool"
    _crew_log_actor: "str"
    _crew_log_error: "str"
    _crew_log_sid: "str"
    _crew_log_step: "int"
    _crew_log_step_t0: "float"
    _crew_log_terminal: "dict[Any, Any] | None"
    _crew_log_turn_no: "int"
    _crew_log_turn_open: "bool"
    _current_replay_message: "dict[Any, Any] | None"
    _declined_grant_notices: "dict[str, str]"
    _deferred_cut_text: "str"
    _deferred_options_expiry: "bool"
    _dir_consumed_out: "dict[str, str]"
    _dispatch_lock: "Lock"
    _dispatch_lock_held: "bool"
    _escalation: "PoisonedConversationEscalation"
    _failed_write_calls: "set[str]"
    _first_turn_history_assembled: "bool"
    _had_empty_response_verdict: "bool"
    _held: "dict[str, Any]"
    _incoming_message: "str"
    _interrupted_turn_preamble: "str"
    _irreversible_consumption_reported: "bool"
    _is_monitor_wake: "bool"
    _is_quick_prompt: "bool"
    _is_refusal_retry_turn: "bool"
    _is_synthetic: "bool"
    _jev_route_text: "str"
    _l1_escalated: "bool"
    _l1_interrupted: "bool"
    _leak_card_posted: "bool"
    _local_turn_marker_generation: "int"
    _member_session_start_pending: "bool"
    _memory_preparation_admitted: "bool"
    _mirror_active_task: "str"
    _mirror_active_task_title: "str"
    _mirror_chan: "str | None"
    _mirror_stream_ts: "str"
    _mirror_task_counter: "int"
    _mirror_thread: "str | None"
    _native_card_output: "dict[str, list[str]]"
    _native_card_output_len: "dict[str, int]"
    _native_result_seen: "set[str]"
    _native_tc_card: "dict[str, str]"
    _native_tracker: "dict[str, dict[Any, Any]]"
    _needs_reinjection: "bool"
    _noticed_leak: "bool"
    _open_construct: "str"
    _owner_backend: "str | None"
    _owner_host_allowed: "bool"
    _owner_project: "Any | None"
    _parsed_input: "Any | None"
    _pending_dir_for_digest: "dict[str, str]"
    _pending_dir_tool: "dict[str, str]"
    _pending_input_digest: "dict[str, str]"
    _pending_tools: "dict[str, str]"
    _person_steered_turn: "bool"
    _produced_visible_output: "bool"
    _provider_has_history: "bool"
    _recovering_compaction: "bool"
    _recovering_infra: "bool"
    _recovering_promise: "bool"
    _recovery_turn_id: "str"
    _refusal_notices: "list[str]"
    _refusal_notices_settled: "int"
    _refusal_reasons: "list[tuple[str, str]]"
    _repeat_loop: "RepeatLoopTracker"
    _replay_accepted_this_turn: "bool"
    _replay_pending: "bool"
    _reply_flushed: "bool"
    _request_prefix_context: "str"
    _responsive_owner: "Task[Any] | None"
    _responsive_recovery: "list[dict[str, str]]"
    _responsive_turn: "ResponsiveTurn"
    _retrying_empty: "bool"
    _row: "dict[str, Any]"
    _rows_before_turn: "frozenset[str]"
    _saw_terminal_event: "bool"
    _saw_text_chunk: "bool"
    _seen_tool_identity: "dict[str, tuple[str, str]]"
    _session_stop_gen_at_entry: "int"
    _shell_tool_calls: "set[str]"
    _span_base_len: "int"
    _span_probe: "str"
    _spec_hooks: "list[Any]"
    _spec_hooks_cwd: "str | None"
    _spec_hooks_unreadable: "bool"
    _stall_command: "str"
    _stall_evidence: "str"
    _stall_status: "StructuredStatus | None"
    _stall_tool_title: "str"
    _status: "str"
    _stop_class: "StopClass"
    _stop_gen_at_entry: "int"
    _stop_gen_turn_start: "Any | int"
    _stop_reason: "str"
    _tcid: "str"
    _tcid_collapsed: "set[str]"
    _tcid_first_source: "dict[str, str]"
    _terminal_directive_applied: "bool"
    _terminal_directive_kind: "str"
    _terminal_directive_tcids: "set[str]"
    _terminal_synthetic: "bool"
    _thinkred: "StreamRedactor"
    _this_turn_is_clear: "bool"
    _todo_recovery_carried: "bool"
    _todo_sync_rendered: "tuple[tuple[str, str, bool], ...]"
    _trusted_prompt_tail: "str | None"
    _ttft_quiet_text: "bool"
    _ttft_t0: "float | None"
    _ttft_visible: "_FirstVisibleClock"
    _turn_billed: "bool"
    _turn_boundary_set: "bool"
    _turn_cost_usd: "float"
    _turn_credits: "float"
    _turn_elapsed_ms: "int"
    _turn_emitted: "bool"
    _turn_flushed_visible_text: "bool"
    _turn_is_completion: "bool"
    _turn_landed: "bool"
    _turn_model: "str"
    _turn_msg_boundary: "int"
    _turn_priority: "StartPriority"
    _turn_project: "str"
    _turn_refusal: "RefusalInfo | None"
    _turn_start_mid: "str | None"
    _turn_started: "float"
    _turn_stats0: "object"
    _turn_stats_captured: "bool"
    _turn_successful_tool_call_ids: "set[str]"
    _turn_t0: "float"
    _turn_thought: "bool"
    _turn_tool_calls: "int"
    _turn_tool_identities: "list[tuple[str, str, str, bool]]"
    _turn_usage_persisted: "bool"
    _user_msg_for_mirror: "str"
    _user_prepend_offset: "int"
    _user_span: "list[int]"
    _wsred: "StreamRedactor"
    _wt_coded_this_turn: "bool"
    _wt_deciding_coding: "bool"
    _wt_key: "str"
    _wt_last_cadence: "float"
    _wt_pending_coding: "set[str]"
    _wt_pending_dropped: "int"
    agent_model: "str"
    approval_sent: "Any"
    assistant_text: "str"
    chunk_seq: "int"
    client: "Any"
    cmd: "str"
    execution_context: "ExecutionContext | None"
    first_word: "str"
    full_message: "str"
    in_tool_group: "bool"
    is_new: "bool"
    is_slash: "bool"
    kiro_agent: "str | None"
    label: "str"
    last_heartbeat: "float"
    m: "dict[str, Any]"
    memory_store: "str | None"
    needs_conversation_discard: "bool"
    needs_session_reset: "bool"
    owner_agent: "str"
    owner_app: "str"
    pre_hook_results: "list[str]"
    private_member: "str"
    prompt_expanded: "bool"
    provider_name: "str"
    registered_slot: "_ChatSlot | None"
    resumed: "bool"
    saw_compaction: "bool"
    selected_binding: "tuple[Any, ...]"
    session_key: "str"
    sessions: "Any | None"
    slot_ctx_blocks: "dict[str, int]"
    slot_ctx_omitted: "dict[str, dict[str, Any]]"
    slot_ctx_phase: "str"
    user_typed_len: "int"
    user_typed_message: "str"
    validated_tool: "str"

    def __init__(
        self,
        *,
        state: "DashboardState",
        slot: "_ChatSlot",
        turn_exit: "_TurnExit",
        message: "str",
        _prompt_depth: "int",
        _attachments: "tuple[str, ...] | list[str]",
        _attachment_meta: "dict[str, list[str]] | None",
        _synthetic_payload: "bool",
        _replay: "frozenset[ReplayFamily]",
        _synthetic_recovery_turn: "bool",
        _replays_completion: "bool",
        _steer_possibly_delivered: "bool",
        _responsive_human: "bool",
        _responsive_protected: "bool",
        _directive_user_origin: "bool",
        _turn_provenance_restored: "bool",
        _directive_self_wake: "bool",
        _directive_loop_id: "str",
        _directive_loop_gen: "int",
        _directive_channel_origin: "bool",
        _commands_off: "bool",
        _turn_actor: "str",
        regenerate_hint: "str",
        _on_consumed: "Callable[[bool], None] | None",
        _on_irreversibly_consumed: "Callable[[], Awaitable[None] | None] | None",
        monitor_completion: "MonitorCompletionHook | None",
        _current_message: "dict | None",
    ) -> None:
        self.state = state
        self.slot = slot
        self.turn_exit = turn_exit
        self.message = message
        self._prompt_depth = _prompt_depth
        self._attachments = _attachments
        self._attachment_meta = _attachment_meta
        self._synthetic_payload = _synthetic_payload
        self._replay = _replay
        self._synthetic_recovery_turn = _synthetic_recovery_turn
        self._replays_completion = _replays_completion
        self._steer_possibly_delivered = _steer_possibly_delivered
        self._responsive_human = _responsive_human
        self._responsive_protected = _responsive_protected
        self._directive_user_origin = _directive_user_origin
        self._turn_provenance_restored = _turn_provenance_restored
        self._directive_self_wake = _directive_self_wake
        self._directive_loop_id = _directive_loop_id
        self._directive_loop_gen = _directive_loop_gen
        self._directive_channel_origin = _directive_channel_origin
        self._commands_off = _commands_off
        self._turn_actor = _turn_actor
        self.regenerate_hint = regenerate_hint
        self._on_consumed = _on_consumed
        self._on_irreversibly_consumed = _on_irreversibly_consumed
        self.monitor_completion = monitor_completion
        self._current_message = _current_message

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> object:
            # Only reached for an attribute that was never set. A declared one is a
            # value the turn has not bound yet: raise what reading that local raised.
            if name in _ChatTurn.__annotations__:
                raise UnboundLocalError(
                    f"cannot access local variable {name!r} where it is not associated with a value"
                )
            raise AttributeError(name)
