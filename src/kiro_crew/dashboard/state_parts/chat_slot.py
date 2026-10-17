"""The ``_ChatSlot`` class: its storage, constructor and member bindings."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.dashboard.recovery_replays import RecoveryReplays
from kiro_crew.dashboard.side_state import SideState
from kiro_crew.dashboard.slot_buffers import SlotBufferCoordinator
from kiro_crew.dashboard.slot_projection import SlotProjection
from kiro_crew.dashboard.slot_queue_repository import (
    SlotQueueRepository,
    durable_queue_entries,
    durable_queue_view,
    queue_persist_signature,
)
from kiro_crew.dashboard.state_parts import own_methods
from kiro_crew.dashboard.state_parts import slot_ladder as _slot_ladder
from kiro_crew.dashboard.state_parts import slot_session as _slot_session
from kiro_crew.dashboard.state_parts import slot_sidebar as _slot_sidebar
from kiro_crew.dashboard.state_parts import slot_todo as _slot_todo
from kiro_crew.dashboard.state_parts import slot_transcript as _slot_transcript
from kiro_crew.dashboard.state_parts import slot_turns as _slot_turns
from kiro_crew.dashboard.state_parts import source_links as _source_links
from kiro_crew.dashboard.state_parts.slot_session import (
    _SLOT_KEY_TITLE_RE,
    NEW_SESSION_TITLE,
    VALID_MEMORY_MODES,
)
from kiro_crew.dashboard.state_parts.slot_turns import _log_task_exception
from kiro_crew.security.credential_sources import CredentialEvidence

if TYPE_CHECKING:
    from kiro_crew.dashboard import DashboardState
    from kiro_crew.subagent import SubagentDelivery
    from kiro_crew.trust_patterns import GrantDeadline


class _ChatSlot:
    """Independent chat session that runs server-side."""

    __slots__ = (
        "_buffers",
        "_projection",
        "_queue_repository",
        "_source_links_cache",
        "_source_links_revision",
        "_dismissed_source_links",
        "_dismissed_hydrated",
        "_dismissed_txn_depth",
        "_dismissed_txn_pending",
        "_closing",
        "credential_evidence",
        "segment_raw_text",
        "segment_shown_sha",
        "segment_removed",
        "segment_message_id",
        "key",
        "title",
        "agent",
        "agent_kind",
        "app_agent_owner",
        "model",
        "jev_route",
        "_model_withheld",
        "_model_withheld_for",
        "served_model",
        "_session_requested_model",
        "_crew_log_previous_sid",
        "_crew_log_previous_undecided",
        "_crew_log_previous_from_mapping",
        "_crew_log_opened_sid",
        "_crew_log_pending_channel",
        "_reasoning_effort",
        "_effort_pick_gen",
        "autocompact_pct",
        "turn_timeout_secs",
        "_turn_timeout_hydrated",
        "mode",
        "workspace",
        "memory_store",
        "_memory_assignment_from_history",
        "project",
        "created_at",
        "_messages",
        "_lazy_window",
        "_lazy_tail",
        "_window_loader_thread",
        "total_messages",
        "_task",
        "_turn_admission_reserved",
        "_turn_generation",
        "_wake_starting_task",
        "_chunk_seq",
        "event",
        "_pending",
        "_pending_consumers",
        "_pending_release_deferred",
        "_queue",
        "_queue_persisted_sig",
        "_queue_persist_inflight",
        "_queue_persist_owed",
        "_last_enqueue_ts",
        "_approval_futures",
        "_approval_instances",
        "_approval_stopped",
        "_trust",
        "_trust_scope",
        "_trust_reads",
        "_member_approval_seeded",
        "_trusted_patterns",
        "_trusted_pattern_deadlines",
        "_titled",
        "_title_origin",
        "_title_epoch",
        "_title_refresh_mark",
        "_title_low_signal",
        "_auto_tagged",
        "_title_in_flight",
        "_title_retry_pending",
        "_title_task",
        "_summary_in_flight",
        "_summary_turn_mark",
        "_detail_render_lock",
        "_last_stop_reason",
        "_created_by",
        "_created_by_sid",
        "_lineage_minted",
        "_revived_by",
        "_artifact",
        "_channel_folder_filed",
        "_resumed_count",
        "_hook_continuation_depth",
        "_todo",
        "_todo_overrides",
        "_todo_sync_rendered",
        "_todo_recovery_pending",
        "_todo_rebuild_expected",
        "_mcp_report",
        "_mcp_report_session_id",
        "_on_message",
        "_on_row",
        "_on_card_event",
        "_dashboard_card_identity",
        "_dashboard_card_exempt",
        "_on_question_retired",
        "_coordinator_approvals",
        "_has_reader_flag",
        "_compacting",
        "_stop_declined_at",
        "_stop_state_raw",
        "_stop_generation",
        "_stop_event_id",
        "_stop_escalated_card_id",
        "_pending_reset_history_key",
        "_pending_discard_conversation_key",
        "_pending_discard_from_wake",
        "_pending_model_pick",
        "_eager_spawn_task",
        "_eager_spawn_failures",
        "_eager_spawn_retry_at",
        "_prefetch_ttl_task",
        "_dirty_flag",
        "_dirty_gen",
        "_metadata_persist_inflight",
        "_guarded_history_writes",
        "recovery_retrigger_count",
        "_last_turn_auth_required",
        "_cycle_reached_provider",
        "_recovery_chat_triggered",
        "_slack_linked",
        "_slack_channel",
        "_slack_thread_ts",
        "channel_origin",
        "_channel_runtime_origin",
        "folder_id",
        "_folder_changed",
        "_folder_suggested",
        "pinned",
        "mutes_opened",
        "muted",
        "tags",
        "tags_revision",
        "_pending_subagent_failures",
        "_pending_synthesis",
        "_synthesis_inflight",
        "_synthesis_recheck",
        "_synthesis_rechecks",
        "_synthesis_completion_turns",
        "_subagent_deliveries_inflight",
        "_subagents_inline_collected",
        "_subagent_delivery_pending",
        "_prompt_busy_retries",
        "_acp_pipe_death_retries",
        "_stale_recovery_retries",
        "_stale_recovery_exhausted_emitted",
        "_tool_stall_retries",
        "_tool_stall_exhausted_emitted",
        "_transient_5xx_retries",
        "_transient_ladder_started",
        "_infra_retries",
        "_fallback_candidate_idx",
        "_fallback_walked",
        "_active_fallback_model",
        "_fallback_primary_model",
        "_fallback_slot_model",
        "_model_pick_gen",
        "_model_runtime_pick_gen",
        "_fallback_pick_gen",
        "_fallback_client_pick_epoch",
        "_refusal_fallback_primary",
        "_refusal_fallback_candidate",
        "_refusal_fallback_session_key",
        "_refusal_fallback_attempted",
        "_refusal_pick_gen",
        "_refusal_client_pick_epoch",
        "replays",
        "_model_access_fallback_used",
        "_posttoken_retry_used",
        "_last_turn_structural_terminal",
        "_last_turn_structural_terminal_loop_id",
        "_last_turn_structural_terminal_loop_gen",
        "_last_turn_model_refused_loop_id",
        "_last_turn_model_refused_loop_gen",
        "_prestream_exhausted_cycles",
        "_poisoned_reset_used",
        "_session_not_found_retry_used",
        "_empty_response_retries",
        "_empty_episode_productive",
        "_carried_ttft_clock",
        "_promise_only_retries",
        "_compaction_continue_retries",
        "_batch_rejected",
        "_batch_rejected_cause",
        "_compaction_failed_retries",
        "_compaction_fail_streak",
        "_compaction_fail_cooldown_until",
        "color_index",
        "color_hex",
        "color_theme",
        "theme_consent",
        "theme_consent_sha",
        "memory_mode",
        "_pending_memory_mode",
        "_ephemeral",
        "_pending_context",
        "_deferred_notes",
        "_dropped_note_ids",
        "_app",
        "_human_seen",
        "_origin",
        "_pending_variants",
        "_lock",
        "forked_from",
        "_fork_lock",
        "_model_pick_lock",
        "_tab_id",
        "_channel_window_mtime",
        "_disk_older_count",
        "_disk_older_durable_count",
        "_unknown_row_fields_bytes",
        "_disk_window_len",
        "_disk_meta_created_at",
        "_disk_meta_observed",
        "_disk_tail_ts",
        "_frozen_prefix_cache",
        "_foreign_reported",
        "_pending_rewrite",
        "_file_changes",
        "_turn_reply_mids",
        "linked_session_key",
        # Relay-archive marker: an older build ran this slot's turns on a peer
        # crew. See ``dashboard/relay_archive.py``.
        "executor",
        "instance_id",
        "remote_slot",
        "_turn_in_flight_generation",
        "_turn_in_flight_prompt",
        "_active_turn_session_key",
        "_responsive",
        "_side",
        "_acp_client",
        "_last_turn_awaiting_permission",
        "_last_turn_children_announced",
        "_steer_segment_cut",
        "_native_subagent_tracker",
        "_native_subagent_output",
        "_steer_confirmed",
        "_pending_steers",
        "_steer_delivery_ids",
        "_steer_send_ids",
        "_steer_user_origin",
        "_steer_channel_origin",
        "_turn_channel_narrowed",
        "_turn_channel_origin",
        "_turn_user_sent",
        "_steer_admissions",
        "_steer_decision_strips",
        "_steer_possibly_delivered",
        "_steer_rpc_in_flight",
        "_steer_audience_fences",
        "_steer_audience_fence_holders",
        "_steer_attachment_meta",
        "_steer_act_now",
        "_wait_state",
        "_end_wait_request",
        "_end_wait_by",
        "_wait_last_ping",
        "_wait_steer_baseline",
        "_wait_contested",
        "_question_pending",
        "_welcomed_agent",
    )

    # Initialised by state_parts/source_links.py.
    _projection: SlotProjection
    _source_links_revision: int
    _source_links_cache: tuple[tuple[int, int, int], list[dict]] | None
    _dismissed_source_links: set[str]
    _dismissed_hydrated: bool
    _dismissed_txn_depth: int
    _dismissed_txn_pending: set[str]

    # Initialised by state_parts/slot_sidebar.py.
    _artifact: str
    _channel_folder_filed: bool
    folder_id: str
    _folder_changed: bool
    _folder_suggested: bool
    pinned: bool
    mutes_opened: bool
    muted: bool
    tags: list[str]
    tags_revision: str
    color_index: int | None
    color_hex: str | None
    color_theme: str
    theme_consent: bool
    theme_consent_sha: str | None

    # Initialised by state_parts/slot_todo.py.
    _todo: dict[str, Any] | None
    _todo_overrides: dict[str, dict[str, Any]]
    _todo_sync_rendered: tuple[tuple[str, str, bool], ...]
    _todo_recovery_pending: bool
    _todo_rebuild_expected: bool

    # Initialised by state_parts/slot_session.py.
    agent_kind: str
    app_agent_owner: str
    _welcomed_agent: str
    jev_route: bool
    _model_withheld: bool
    _model_withheld_for: str
    _session_requested_model: str | None
    _crew_log_previous_sid: str
    _crew_log_previous_undecided: bool | None
    _crew_log_previous_from_mapping: bool
    _crew_log_opened_sid: str
    _crew_log_pending_channel: bool
    served_model: str
    _effort_pick_gen: int
    _reasoning_effort: str
    autocompact_pct: float | None
    turn_timeout_secs: int | None
    _turn_timeout_hydrated: bool
    memory_store: str
    _memory_assignment_from_history: bool
    project: str
    executor: str
    instance_id: str
    remote_slot: str
    created_at: str
    _titled: bool
    _title_origin: str
    _title_epoch: int
    _title_refresh_mark: int
    _title_low_signal: bool
    _auto_tagged: bool
    _title_in_flight: bool
    _title_task: asyncio.Task[None] | None
    _title_retry_pending: bool
    _summary_in_flight: bool
    _detail_render_lock: asyncio.Lock
    _summary_turn_mark: int
    _last_stop_reason: str
    _created_by: str
    _created_by_sid: str
    _lineage_minted: bool
    _revived_by: str
    _mcp_report: dict[str, Any] | None
    _mcp_report_session_id: str
    _pending_reset_history_key: str | None
    _pending_discard_conversation_key: str | None
    _pending_discard_from_wake: bool
    _pending_model_pick: Any
    _eager_spawn_task: asyncio.Task[None] | None
    _eager_spawn_failures: int
    _eager_spawn_retry_at: float
    _prefetch_ttl_task: asyncio.Task[None] | None
    _recovery_chat_triggered: bool
    recovery_retrigger_count: int
    _slack_linked: bool
    _slack_channel: str
    _slack_thread_ts: str
    _model_pick_gen: int
    _model_runtime_pick_gen: int | None
    _pending_memory_mode: str | None
    _app: str
    _human_seen: bool
    _origin: str
    forked_from: str | None
    _fork_lock: asyncio.Lock
    _model_pick_lock: asyncio.Lock
    _tab_id: str
    channel_origin: bool
    _channel_runtime_origin: bool

    # Initialised by state_parts/slot_turns.py.
    _turn_in_flight_generation: int
    _turn_in_flight_prompt: dict[str, Any] | None
    _buffers: SlotBufferCoordinator
    _queue_repository: SlotQueueRepository
    _closing: int
    credential_evidence: CredentialEvidence
    segment_raw_text: str | None
    segment_shown_sha: Any  # a ``hashlib.sha256()`` object
    segment_removed: dict[str, str]
    segment_message_id: str
    _task: asyncio.Task[Any] | None
    _turn_admission_reserved: bool
    _turn_generation: int
    _wake_starting_task: asyncio.Task[Any] | None
    _chunk_seq: int
    event: asyncio.Event
    _pending: list[dict[str, str]]
    _pending_consumers: int
    _pending_release_deferred: bool
    _queue: list[dict[str, Any]]
    _queue_persisted_sig: str
    _queue_persist_inflight: bool
    _queue_persist_owed: bool
    _last_enqueue_ts: str
    _approval_futures: dict[str, asyncio.Future[str]]
    _approval_instances: dict[str, tuple[asyncio.Future[str], str]]
    _approval_stopped: set[str]
    _trust: bool
    _trust_scope: str
    _trust_reads: bool
    _member_approval_seeded: bool
    _trusted_patterns: set[str]
    _trusted_pattern_deadlines: dict[str, GrantDeadline]
    _hook_continuation_depth: int
    _on_message: object | None
    _on_row: object | None
    _on_card_event: object | None
    _dashboard_card_identity: str
    _dashboard_card_exempt: bool
    _on_question_retired: object | None
    _coordinator_approvals: Callable[[str], list[dict]] | None
    _has_reader_flag: bool
    _compacting: bool
    _stop_declined_at: float
    _stop_state_raw: str
    _stop_generation: int
    _stop_event_id: str | None
    _stop_escalated_card_id: str | None
    _last_turn_auth_required: bool
    _cycle_reached_provider: bool
    _subagent_delivery_pending: dict[str, list[SubagentDelivery]]
    _pending_context: list[dict[str, Any]]
    _deferred_notes: list[dict[str, Any]]
    _dropped_note_ids: set[str]
    _pending_variants: list[dict]
    _lock: asyncio.Lock
    _file_changes: list[dict[str, Any]]
    _turn_reply_mids: list[str]
    linked_session_key: str
    _active_turn_session_key: str
    _responsive: Any
    _side: SideState | None
    _acp_client: None
    _last_turn_awaiting_permission: bool
    _last_turn_children_announced: bool
    _question_pending: dict[str, dict]

    # Initialised by state_parts/slot_ladder.py.
    _pending_subagent_failures: list[str]
    _pending_synthesis: bool
    _synthesis_inflight: bool
    _synthesis_recheck: asyncio.TimerHandle | None
    _synthesis_rechecks: int
    _synthesis_completion_turns: int
    _subagent_deliveries_inflight: int
    _subagents_inline_collected: set[str]
    _prompt_busy_retries: int
    _acp_pipe_death_retries: int
    _stale_recovery_retries: int
    _tool_stall_retries: int
    _stale_recovery_exhausted_emitted: bool
    _tool_stall_exhausted_emitted: bool
    _transient_5xx_retries: int
    _transient_ladder_started: float
    _infra_retries: int
    _fallback_candidate_idx: int
    _fallback_walked: list[str]
    _active_fallback_model: str
    _fallback_primary_model: str
    _fallback_slot_model: str
    _fallback_pick_gen: int
    _fallback_client_pick_epoch: int
    _refusal_fallback_primary: str
    _refusal_fallback_candidate: str
    _refusal_fallback_session_key: str
    _refusal_fallback_attempted: bool
    _refusal_pick_gen: int
    _refusal_client_pick_epoch: int
    replays: RecoveryReplays
    _model_access_fallback_used: bool
    _posttoken_retry_used: bool
    _last_turn_structural_terminal: bool
    _last_turn_structural_terminal_loop_id: str
    _last_turn_structural_terminal_loop_gen: int
    _last_turn_model_refused_loop_id: str
    _last_turn_model_refused_loop_gen: int
    _prestream_exhausted_cycles: int
    _poisoned_reset_used: bool
    _session_not_found_retry_used: bool
    _empty_response_retries: int
    _empty_episode_productive: bool
    _carried_ttft_clock: Any
    _promise_only_retries: int
    _compaction_continue_retries: int
    _batch_rejected: bool
    _batch_rejected_cause: str
    _compaction_failed_retries: int
    _compaction_fail_streak: int
    _compaction_fail_cooldown_until: float
    _steer_segment_cut: Callable[[], None] | None
    _native_subagent_tracker: dict[str, dict[str, Any]]
    _native_subagent_output: dict[str, list[str]]
    _steer_confirmed: set[str]
    _pending_steers: list[str]
    _steer_delivery_ids: dict[str, str]
    _steer_send_ids: dict[str, str]
    _steer_user_origin: dict[str, bool]
    _steer_channel_origin: dict[str, bool]
    _turn_channel_narrowed: bool
    _turn_channel_origin: bool
    _turn_user_sent: bool
    _steer_admissions: dict[str, dict]
    _steer_decision_strips: dict[str, dict]
    _steer_possibly_delivered: set[str]
    _steer_rpc_in_flight: set[str]
    _steer_audience_fences: dict[str, dict]
    _steer_audience_fence_holders: dict[str, int]
    _steer_attachment_meta: dict[str, dict[str, Any]]
    _steer_act_now: set[str]
    _wait_state: dict | None
    _end_wait_request: str | None
    _end_wait_by: str
    _wait_last_ping: float
    _wait_steer_baseline: float
    _wait_contested: bool

    # Initialised by state_parts/slot_transcript.py.
    _messages: list[dict[str, Any]]
    _lazy_window: Callable[[_ChatSlot], None] | None
    _lazy_tail: list[dict[str, Any]] | None
    _window_loader_thread: int | None
    total_messages: int
    _resumed_count: int
    _dirty_flag: bool
    _dirty_gen: int
    _metadata_persist_inflight: int
    _guarded_history_writes: set[Any]
    _channel_window_mtime: float
    _disk_older_count: int
    _unknown_row_fields_bytes: int
    _disk_older_durable_count: int
    _disk_window_len: int
    _disk_tail_ts: str | None
    _frozen_prefix_cache: tuple[float, int, int, str, list[str]] | None
    _foreign_reported: frozenset[int]
    _pending_rewrite: bool

    def __init__(
        self,
        key: str,
        title: str = "",
        agent: str = "",
        workspace: str = "default",
        model: str = "",
        mode: str = "",
        memory_mode: str = "persistent",
        ephemeral: bool = False,
    ) -> None:
        self.key = key
        self.title = title or key
        self.agent = agent
        _slot_session.init_session_fields(self)
        self.model = model
        # "" = default chat; app workers may carry their own mode (design-critique)
        self.mode = mode
        self.workspace = workspace
        _slot_turns.init_turns_fields(self)
        _slot_transcript.init_transcript_fields(self)
        _source_links.init_source_links_fields(self)
        _slot_sidebar.init_sidebar_fields(self)
        _slot_todo.init_todo_fields(self)
        _slot_ladder.init_ladder_fields(self)
        if memory_mode not in VALID_MEMORY_MODES:
            raise ValueError(
                f"invalid memory_mode {memory_mode!r}, must be one of {VALID_MEMORY_MODES}"
            )
        self.memory_mode: str = memory_mode
        self._ephemeral: bool = ephemeral  # Incognito mode: no memory writes
        # The ``created_at`` of the on-disk metadata line this slot LAST
        # OBSERVED (at restore or at its own save). This is the session file's
        # identity: ``delete_session`` removes the file and a later writer
        # (e.g. a channel/cron ``append_off_loop``) creates a FRESH one with a
        # new ``created_at``, so a pending save comparing the current on-disk
        # value against this field can tell "the same file I knew" from "a
        # different file born after my session was permanently deleted" — the
        # delete-won guard in ``_save_slot_to_history`` refuses to merge the
        # deleted window into the latter. Empty means "never observed a disk
        # identity" (fresh slot), which the guard treats as no evidence.
        self._disk_meta_created_at: str = ""
        # Whether this slot has OBSERVED its transcript's metadata on disk at
        # all — set wherever ``_disk_meta_created_at`` is recorded (hydrate
        # sites and committed saves). Legacy metadata carries no
        # ``created_at``, which leaves the identity string above EMPTY even
        # though the file was genuinely observed; without this bit the
        # delete-won guard would read that empty identity as "fresh slot, no
        # evidence" and let a save racing a permanent delete recreate the
        # deleted transcript. The bit supplies the missing-file witness for
        # legacy sessions; the identity COMPARISON still requires a non-empty
        # ``created_at`` on both sides.
        self._disk_meta_observed: bool = False

    @property
    def reasoning_effort(self) -> str:
        """The slot's reasoning effort: "" = provider default, else a level."""
        return self._reasoning_effort

    @reasoning_effort.setter
    def reasoning_effort(self, value: str) -> None:
        # Bumped on every write that changes the level, rollbacks included: a
        # rollback that lands while a session_set_model effort is pending drops
        # that pick rather than letting it overwrite a level the user touched.
        # A same-value write is not a new choice (the dropdown route re-commits
        # the level it already wrote after awaiting its live push), so it must
        # not drop a pick queued during that await; the route's explicit
        # same-level fast path bumps the generation itself.
        if value != self._reasoning_effort:
            self._effort_pick_gen += 1
        self._reasoning_effort = value

    @property
    def is_closing(self) -> bool:
        """Whether slot teardown currently fences new monitor admission."""
        return self._closing > 0

    @property
    def _dirty(self) -> bool:
        """True while this slot holds state not yet confirmed on disk.

        Deliberately a property so that ``_dirty_gen`` is bumped centrally by the
        ~20 existing ``slot._dirty = True`` sites without editing any of them.

        Two independent readers depend on this staying True for the WHOLE
        duration of a save, not just until the save starts:

        * ``chat_fork`` treats it as "unpersisted in-memory state exists". A False
          read makes it skip both the in-memory tail append and the durable
          pre-fork save, so it forks from stale disk and the new session silently
          omits the newest messages.
        * ``_save_slot_to_history``'s resumed-slot no-op guard skips when
          ``_resumed_count > 0 and len(window) <= _resumed_count and not _dirty``;
          its comment states the assumption directly — "a dirty slot whose length
          merely equals the resumed count still falls through ... otherwise an
          in-place edit after resume would never reach disk."

        So the periodic flush must NOT clear this early to protect itself against
        clobbering a concurrent mark. It compares ``_dirty_gen`` instead.
        """
        return self._dirty_flag

    @_dirty.setter
    def _dirty(self, value: bool) -> None:
        self._dirty_flag = value
        if value:
            # Monotonic: only ever advances, so a wrapped-around compare is
            # impossible and a missed bump can only cause an extra (harmless)
            # flush, never a skipped one.
            self._dirty_gen += 1

    @property
    def _stop_state(self) -> str:
        return self._stop_state_raw

    @_stop_state.setter
    def _stop_state(self, value: str) -> None:
        # Count stop INITIATIONS (idle → active edge) in a monotonic
        # generation that teardown never rewinds — see __init__ comment.
        # Escalations (soft_pending → killing) and resets (→ idle) are not
        # new initiations and do not bump it.
        if value != "idle" and self._stop_state_raw == "idle":
            self._stop_generation += 1
        self._stop_state_raw = value

    @property
    def _stopping(self) -> bool:
        return self._stop_state != "idle"

    @_stopping.setter
    def _stopping(self, value: bool) -> None:
        self._stop_state = "soft_pending" if value else "idle"

    @property
    def todo_sync_rendered(self) -> tuple[tuple[str, str, bool], ...]:
        """The ``(id, text, completed)`` rows the last :meth:`todo_sync_prompt` rendered."""
        return getattr(self, "_todo_sync_rendered", ())

    @property
    def todo_recovery_pending(self) -> bool:
        """True when a built recovery block has not yet been confirmed delivered."""
        return getattr(self, "_todo_recovery_pending", False)

    @property
    def pending_has_consumer(self) -> bool:
        """True while something can still deliver rows out of the pending queue."""
        return self._buffers.pending_has_consumer(self)

    @property
    def _has_reader(self) -> bool:
        """True while an HTTP SSE stream is draining this slot."""
        return self._has_reader_flag

    @_has_reader.setter
    def _has_reader(self, value: bool) -> None:
        was = self._has_reader_flag
        self._has_reader_flag = bool(value)
        if was and not self._has_reader_flag:
            self._retry_deferred_release()

    def durable_queue_entries(self) -> list[dict[str, Any]]:
        """The queued user prompts a metadata writer may persist right now."""
        return durable_queue_entries(self._queue)

    def durable_queue_view(self) -> tuple[list[dict[str, Any]], int]:
        """Persistable queued prompts and the candidate count, from one read.

        Used where the two are SUBTRACTED (the save's over-cap report), so the
        difference describes one observation of the queue rather than two.
        """
        return durable_queue_view(self._queue)

    @property
    def queue_persist_pending(self) -> bool:
        """True while a queued user prompt differs from what is on disk.

        A queued prompt is the user's own words with NO other copy: the
        transcript row for it is written by the drain, not by the enqueue, so
        until a save carries the queue itself the only record is this process's
        memory. The periodic flush reads this beside ``_dirty`` so an enqueue
        (or any in-place queue mutation) reaches disk on the next pass.
        """
        return queue_persist_signature(self.durable_queue_entries()) != self._queue_persisted_sig

    @property
    def task(self) -> asyncio.Task[Any] | None:
        return self._task

    @task.setter
    def task(self, value: asyncio.Task[Any] | None) -> None:
        if value is not None and value is not self._task:
            self._turn_generation += 1
        self._task = value

    @property
    def turn_running(self) -> bool:
        """Whether an active model turn still owns the slot."""
        task = self.task
        return bool(task is not None and not task.done())

    @property
    def running(self) -> bool:
        """Admission/reservation predicate; use ``turn_running`` for execution.

        See ``docs/system-specs/modules/session.md``.
        """
        return bool(self.turn_running or self._turn_admission_reserved)

    @property
    def queue_depth(self) -> int:
        """Number of prompts currently queued behind the active turn."""
        return len(self._queue)

    @property
    def model_withheld(self) -> bool | None:
        """Whether the live session withholds this slot's pinned model.

        Tri-state, and the third state carries as much information as the other
        two: ``True`` the account cannot run the pin (the spawn withheld it and
        the session is on the backend default), ``False`` it can, ``None``
        **unknown** -- no session has advertised a comparable list for this pin
        yet. The frontend must fail open on ``None`` (see ``displayModel``);
        unknown is not denied.

        Recorded per model id, not per slot: the verdict answers a question
        about ONE pin, so it is reported only while the slot still carries the
        model it was computed for. That makes every writer of ``slot.model``
        (the picker, the bulk pick, the rollback, the restore paths, the
        canonical backfill) invalidate it for free -- a stale verdict outliving
        a re-pin would be a display that names the wrong model, and there are
        too many writers to keep an explicit reset at each one.

        DISPLAY only. This is never a write source: the pin is deliberately
        KEPT when withheld (it is inert and self-heals on re-upgrade), and the
        pin-to-agent row writes ``slot.model`` itself.
        """
        if not self.model or self._model_withheld_for != self.model:
            return None
        return self._model_withheld

    @property
    def is_restricted(self) -> bool:
        """True when memory writes (consolidation, lessons) are blocked."""
        return self.memory_mode != "persistent"

    @property
    def blocks_reads(self) -> bool:
        """True when memory-context injection into this session is blocked."""
        return self.memory_mode == "temporary"

    @property
    def unattended(self) -> bool:
        """True when no human is driving this session's turns.

        FIX 1 + FIX 2 share this predicate: it decides which slots get the
        deny-fast approval window (:meth:`DashboardState.approval_timeout_for`)
        and which turns are charged against the background concurrency cap
        (:meth:`DashboardState.run_background_turn`).

        ``_app`` is the whole test, plus the ``_human_seen`` escape hatch. Why
        app-ownership and not ``_trust``:

        * ``_trust`` is False *by construction* wherever this predicate is
          consulted. The runner auto-approves and ``continue``s while trust
          holds, so a tool only reaches the interactive wait once trust is
          absent — and trust is in-memory, so a gateway restart clears it on
          every app worker. A ``_trust``-based detector reads False in exactly
          the situation it exists to detect.
        * ``_app`` is set only by an app creating the slot (App Kit §5.2), is
          persisted in the session metadata, and is already the ownership axis
          every other isolation decision in these files keys on. A session a
          person created has ``_app == ""`` and is therefore never affected —
          which is what keeps interactive behaviour byte-identical.

        Both halves are persisted, and they have to be: ``_app`` surviving a
        restart while ``_human_seen`` did not is what made an attended app tab
        silently revert to the deny-fast window after every upgrade.
        """
        return bool(self._app) and not self._human_seen

    def enqueue_or_run_prompt(
        self,
        prompt: str,
        run_chat_coro: Callable[[DashboardState, _ChatSlot, str], Coroutine[Any, Any, None]],
        state: DashboardState,
        *,
        extra_meta: dict[str, Any] | None = None,
    ) -> bool:
        """Queue *prompt* if busy, otherwise start an agent turn.

        Encapsulates the queue-vs-run decision so callers don't need to
        touch ``_queue``, ``task``, or ``_background_tasks`` directly.
        Always registers :func:`_log_task_exception` to prevent silent failures.

        Returns ``True`` if the prompt started an agent turn, ``False`` if
        it was queued. Lets callers gate UI-visible side-effects (notifications,
        SSE pushes) on whether the prompt actually ran.

        *extra_meta* is merged onto the queued entry's ``meta`` beside the
        containment stamp, for a producer that must record something about the
        ADMISSION for the drain to read later -- ``session_control.send_to_target``
        stamps the sending session there (``send_origin_meta``) so a drop can be
        reported back to it. Ignored on the run arm: a prompt that starts its turn
        immediately has no queue entry and no later drain to tell anything to. The
        containment keys win a collision, since the drain's own authorization
        decision must not be overwritable by a caller's extra fields.

        Concurrency: the check (``self.running``) and mutation (``self.task = ...``)
        run synchronously on the asyncio event loop with no ``await`` between them,
        so two concurrent callers targeting the same slot cannot both observe
        ``running == False`` within a single loop iteration.
        """
        if self.running:
            # circular import: session_control imports this module at module level.
            from kiro_crew.dashboard.chat_delivery import start_queue_persist
            from kiro_crew.dashboard.session_control import containment_meta

            # Stamp the containment constraints holding at ADMISSION, so the
            # queue drain can re-assert them at delivery: a target
            # that gains a channel/mirror link while this prompt waits must not
            # execute it under the weaker constraints that admitted it.
            #
            # *extra_meta* rides alongside, applied FIRST so the containment keys
            # win a collision: a caller's extra fields are descriptive, and the
            # drain's authorization input must not be replaceable from here.
            _meta: dict[str, Any] = dict(extra_meta or {})
            _meta.update(containment_meta(state, self))
            self.queue_append(prompt, meta=_meta)
            # Returning False IS the receipt that the prompt was accepted onto the
            # queue, and until the drain writes its transcript row the queue is the
            # prompt's only record -- so a restart inside the periodic flush
            # interval loses a prompt the caller was told had landed.
            # ``start_queue_persist``'s own contract is that every place a prompt is
            # accepted onto a slot queue starts the write that makes it durable,
            # "a receipt from one path and a write from only the other" being the
            # asymmetry it exists to prevent. This admission point is one of those
            # places. Started, not awaited, and self-limiting: it is skipped unless
            # the slot is dirty or its queue drifted from disk, and it is
            # single-flight per slot, so a burst of queued prompts is not a burst of
            # transcript rewrites.
            start_queue_persist(state, self)
            return False
        self.append("user", prompt, "msg msg-u")
        task = asyncio.create_task(run_chat_coro(state, self, prompt))
        self.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
        task.add_done_callback(_log_task_exception)
        return True

    @property
    def display_title(self) -> str:
        """Title for UI display. Shows ``NEW_SESSION_TITLE`` while the slot is
        still on its untouched default key (untitled) — covering brand-new
        empty sessions and the window before the LLM title lands — otherwise
        the real title. Slots with a meaningful non-key title (plan, cron,
        fork, slack) are unaffected since their title != key.
        """
        if not self._titled and (
            not self.title or self.title == self.key or _SLOT_KEY_TITLE_RE.match(self.title)
        ):
            return NEW_SESSION_TITLE
        return self.title

    @property
    def messages(self) -> list[dict[str, Any]]:
        """The in-memory transcript window, loaded on first read for a lazy row.

        A sidebar row restored at startup carries only its metadata until
        something needs its rows. Every reader goes through here, so the first
        one -- opening the tab, a turn, an injected row, a fork -- loads the
        window before it sees a single row, and no reader can act on the empty
        list a stub holds. Readers that only need the sidebar summary use
        :meth:`sidebar_messages`, and the save path uses :meth:`loaded_messages`,
        so neither forces a load.
        """
        if getattr(self, "_lazy_window", None) is not None:
            self.load_window()
        return self._messages

    @messages.setter
    def messages(self, value: list[dict[str, Any]]) -> None:
        # A wholesale replacement is a decision about the window, so it ends a
        # pending lazy load rather than letting the load land on top of it.
        if getattr(self, "_lazy_window", None) is not None:
            self._lazy_window = None
            self._lazy_tail = None
        self._messages = value

    @property
    def window_pending(self) -> bool:
        """Whether this slot is a restored row whose window is not loaded yet."""
        return getattr(self, "_lazy_window", None) is not None

    # state_parts/source_links.py
    invalidate_source_links = _source_links.invalidate_source_links
    dismiss_source_link = _source_links.dismiss_source_link
    mentions_source_identity = _source_links.mentions_source_identity
    _pr_source_links = _source_links._pr_source_links
    source_links_payload = _source_links.source_links_payload
    _summary_source_links = _source_links._summary_source_links
    source_links_view = _source_links.source_links_view

    # state_parts/slot_sidebar.py
    bump_tags_revision = _slot_sidebar.bump_tags_revision

    # state_parts/slot_todo.py
    set_todo = _slot_todo.set_todo
    _todo_override_row_matches = _slot_todo._todo_override_row_matches
    todo_payload = _slot_todo.todo_payload
    todo_task_text = _slot_todo.todo_task_text
    set_todo_task_completed = _slot_todo.set_todo_task_completed
    todo_sync_prompt = _slot_todo.todo_sync_prompt
    mark_todo_edits_stated = _slot_todo.mark_todo_edits_stated
    _todo_text_line = staticmethod(_slot_todo._todo_text_line)
    pin_completed_todo_rows = _slot_todo.pin_completed_todo_rows
    mark_todo_recovery_pending = _slot_todo.mark_todo_recovery_pending
    clear_todo_recovery_pending = _slot_todo.clear_todo_recovery_pending
    todo_recovery_prompt = _slot_todo.todo_recovery_prompt

    # state_parts/slot_session.py
    set_mcp_report = _slot_session.set_mcp_report
    clear_mcp_report = _slot_session.clear_mcp_report
    mcp_report_payload = _slot_session.mcp_report_payload
    record_model_withheld = _slot_session.record_model_withheld
    record_served_model = _slot_session.record_served_model
    latch_crew_log_previous = _slot_session.latch_crew_log_previous
    take_crew_log_previous = _slot_session.take_crew_log_previous
    forget_session_model_state = _slot_session.forget_session_model_state

    # state_parts/slot_turns.py
    begin_close = _slot_turns.begin_close
    cancel_close = _slot_turns.cancel_close
    push_wire_frame = _slot_turns.push_wire_frame
    drain = _slot_turns.drain
    _retry_deferred_release = _slot_turns._retry_deferred_release
    pending_consumer = _slot_turns.pending_consumer
    release_pending_chunks = _slot_turns.release_pending_chunks
    purge_chunks = _slot_turns.purge_chunks
    has_pending_context_seat = _slot_turns.has_pending_context_seat
    append_pending_context = _slot_turns.append_pending_context
    drop_foreign_authorized_notes = _slot_turns.drop_foreign_authorized_notes
    deferred_context_count = _slot_turns.deferred_context_count
    flush_deferred_notes = _slot_turns.flush_deferred_notes
    register_approval = _slot_turns.register_approval
    approval_instance = _slot_turns.approval_instance
    unregister_approval = _slot_turns.unregister_approval
    mark_permission_resolved = _slot_turns.mark_permission_resolved
    queue_append = _slot_turns.queue_append
    _note_enqueue = _slot_turns._note_enqueue
    queue_insert = _slot_turns.queue_insert
    queue_pop = _slot_turns.queue_pop
    note_pending_subagent_delivery = _slot_turns.note_pending_subagent_delivery
    owes_subagent_delivery = _slot_turns.owes_subagent_delivery
    take_pending_subagent_deliveries = _slot_turns.take_pending_subagent_deliveries
    queue_remove_by_id = _slot_turns.queue_remove_by_id
    queue_edit_by_id = _slot_turns.queue_edit_by_id
    queue_promote_by_id = _slot_turns.queue_promote_by_id

    # state_parts/slot_transcript.py
    note_disk_tail = _slot_transcript.note_disk_tail
    append = _slot_transcript.append
    update_message = _slot_transcript.update_message
    load_window = _slot_transcript.load_window
    loaded_messages = _slot_transcript.loaded_messages
    sidebar_messages = _slot_transcript.sidebar_messages
    to_dict = _slot_transcript.to_dict


own_methods(_ChatSlot)
