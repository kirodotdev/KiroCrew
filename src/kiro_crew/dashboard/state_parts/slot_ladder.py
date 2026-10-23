"""Per-slot bookkeeping the chat runner keeps between turns.

Recovery-ladder budgets and their exhaustion latches, the model-fallback walk,
steer delivery and its audience fences, subagent synthesis and the wait tool.
The chat runner reads and writes these; this module declares their defaults.

Instance fields of ``_ChatSlot`` this module reads and writes:

Writes: ``_acp_pipe_death_retries``, ``_active_fallback_model``, ``_batch_rejected``,
``_batch_rejected_cause``, ``_carried_ttft_clock``, ``_compaction_continue_retries``,
``_compaction_fail_cooldown_until``, ``_compaction_fail_streak``,
``_compaction_failed_retries``, ``_empty_episode_productive``,
``_empty_response_retries``, ``_end_wait_by``, ``_end_wait_request``,
``_fallback_candidate_idx``, ``_fallback_client_pick_epoch``, ``_fallback_pick_gen``,
``_fallback_primary_model``, ``_fallback_slot_model``, ``_fallback_walked``,
``_infra_retries``, ``_last_turn_model_refused_loop_gen``,
``_last_turn_model_refused_loop_id``, ``_last_turn_structural_terminal``,
``_last_turn_structural_terminal_loop_gen``, ``_last_turn_structural_terminal_loop_id``,
``_model_access_fallback_used``, ``_native_subagent_output``,
``_native_subagent_tracker``, ``_pending_steers``, ``_pending_subagent_failures``,
``_pending_synthesis``, ``_poisoned_reset_used``, ``_posttoken_retry_used``,
``_prestream_exhausted_cycles``, ``_promise_only_retries``, ``_prompt_busy_retries``,
``_refusal_client_pick_epoch``, ``_refusal_fallback_attempted``,
``_refusal_fallback_candidate``, ``_refusal_fallback_primary``,
``_refusal_fallback_session_key``, ``_refusal_pick_gen``,
``_session_not_found_retry_used``, ``_stale_recovery_exhausted_emitted``,
``_stale_recovery_retries``, ``_steer_act_now``, ``_steer_admissions``,
``_steer_attachment_meta``, ``_steer_audience_fence_holders``,
``_steer_audience_fences``, ``_steer_automation``, ``_steer_channel_origin``,
``_steer_confirmed``,
``_steer_decision_strips``, ``_steer_delivery_ids``, ``_steer_possibly_delivered``,
``_steer_rpc_in_flight``, ``_steer_segment_cut``, ``_steer_send_ids``,
``_steer_user_origin``, ``_subagent_deliveries_inflight``,
``_subagents_inline_collected``, ``_synthesis_completion_turns``,
``_synthesis_inflight``, ``_synthesis_recheck``, ``_synthesis_rechecks``,
``_tool_stall_exhausted_emitted``, ``_tool_stall_retries``, ``_transient_5xx_retries``,
``_transient_ladder_started``, ``_turn_channel_narrowed``, ``_turn_channel_origin``,
``_turn_retries``, ``_turn_user_sent``, ``_wait_contested``, ``_wait_last_ping``,
``_wait_state``, ``_wait_steer_baseline``, ``replays``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.dashboard.recovery_replays import RecoveryReplays

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot


def init_ladder_fields(self: _ChatSlot) -> None:
    """Set this module's slot fields to their defaults (``_ChatSlot.__init__``)."""
    self._pending_subagent_failures = []
    # Fix 2 (B1): armed by gateway when the LAST sub-agent of a fan-out
    # completes; consumed once by chat_runner's drain/idle branch to fire a
    # single post-fan-out synthesis turn. Cleared if a user message drains
    # first (user takes over).
    self._pending_synthesis = False
    # True while chat_runner owns the one readiness-wait/synthesis task.
    # Kept separate from _pending_synthesis so readiness loss does not
    # consume the one-shot request or permit duplicate waiters.
    self._synthesis_inflight = False
    # The fire gate's outage re-check (chat_runner._arm_synthesis_recheck):
    # its pending timer, cancelled by begin_close, and how many it ran.
    self._synthesis_recheck = None
    self._synthesis_rechecks = 0
    # Sub-agent completion turns handed to this slot: one per completion
    # the gateway queues or launches (a wave digest is one). Zeroed when
    # the synthesis runs or is dropped, and when a user message drained at
    # a turn's end disarms it after the fire gate answered clear
    # (`_hand_off_queue` reads; the drain itself does not); other disarms
    # keep the count. Exactly 1 means one turn already reported every
    # result, so the turn-end fire gate drops the arm instead of running a
    # synthesis that restates it.
    self._synthesis_completion_turns = 0
    # Fix 2 (B1) race guard: number of sub-agent completion deliveries
    # currently in flight for this slot (incremented in gateway._subagent_done
    # from entry until the completion is queued/launched). The synthesis
    # fire-gate requires this to be 0 so a concurrently-finishing sibling
    # can't let an earlier turn fire synthesis before its result lands.
    self._subagent_deliveries_inflight = 0
    # IDs of sub-agents whose results were already delivered inline via the
    # blocking spawn_sub_agents MCP tool.  _subagent_done skips injection
    # for these to prevent a duplicate turn that clobbers [OPTIONS:] buttons.
    self._subagents_inline_collected = set()
    self._prompt_busy_retries = 0
    self._acp_pipe_death_retries = 0
    # Auto-recovery of a genuinely-wedged (stale) turn: bumped when the ACP
    # layer signals STOP_REASON_STALE_RECOVER; bounded (3) so a permanently
    # broken session surfaces "start a new chat" instead of looping. Reset on
    # a completed turn (alongside the other retry budgets).
    self._stale_recovery_retries = 0
    # Tool-stall recovery: bumped when the ACP layer ends a turn with
    # STOP_REASON_TOOL_STALL. A SEPARATE budget from pipe-death (the legacy
    # path charged stalls against _acp_pipe_death_retries and re-queued the
    # original message verbatim — one false positive burned the whole
    # session budget). Bounded (3); reset on a completed turn.
    self._tool_stall_retries = 0
    # Telemetry dedup for the exhausted outcome: set when outcome=exhausted
    # is emitted for the corresponding budget, cleared when the budget
    # resets on a completed turn. Keeps a repeatedly-stalling wedged slot
    # from re-emitting "exhausted" every stall, and keeps a later ok turn
    # from mis-emitting "recovered" for an already-exhausted cycle —
    # WITHOUT mutating the budget itself (a wedged slot stays terminal
    # until a turn actually completes; it never re-enters a fresh
    # recovery cycle just because the metric fired).
    self._stale_recovery_exhausted_emitted = False
    self._tool_stall_exhausted_emitted = False
    # Transient backend 5xx (InternalServerError / DispatchFailure /
    # ConnectionReset) retries on the interactive stream path. Distinct
    # budget from prompt-busy / pipe-death; reset on a completed turn.
    self._transient_5xx_retries = 0
    # time.monotonic() at the current transient ladder's first retry, the
    # start of the network-drop recovery window. Re-stamped whenever the
    # ladder starts from zero, so it needs no reset of its own.
    self._transient_ladder_started = 0.0
    # Re-sends of the current user turn after a pre-output transient error,
    # by mechanism (``throttle`` / ``transient_5xx``). A plain count, not a
    # budget: _transient_5xx_retries is rewound by a fallback swap, this is
    # not. Read by the landed turn's footer stats and recovery telemetry;
    # emptied when a top-level turn that is not a recovery starts.
    self._turn_retries = {}
    # L1 gateway-capacity retries: how many times THIS cycle waited out an
    # infrastructure refusal of a tool call (the ladder owns the budget; this
    # is the slot-visible count the health panel classifies as recovering).
    # Deliberately NOT _transient_5xx_retries: that one is a live budget read
    # by the re-prompt gate, the backoff seed and the model-fallback
    # threshold, so spending it here shortens the next real 5xx ladder and
    # brings the fallback swap closer over a wait the primary model had no
    # part in.
    self._infra_retries = 0
    # Throttle-exhaustion model-fallback walk state (agent.fallback_model).
    # _fallback_candidate_idx / _fallback_walked are PER-CYCLE (next chain
    # position to try + candidates already tried this logical turn, for the
    # chain-exhausted error story); both reset with the other retry budgets
    # on a landed turn. _active_fallback_model / _fallback_primary_model are
    # STICKY session state: set when a fallback swap lands, kept across
    # turns until the start-of-turn restore probe moves the session back to
    # the primary (deliberately NOT reset on turn completion).
    self._fallback_candidate_idx = 0
    self._fallback_walked = []
    self._active_fallback_model = ""
    self._fallback_primary_model = ""
    # Snapshot of slot.model taken when the fallback activated; it heals
    # slot.model if the automatic provider backfill wrote the fallback id
    # into an empty slot while the fallback was active (slot.model is
    # re-sent as a set_model override on resume, so leaving the fallback
    # there would re-pin it after the primary recovered).
    self._fallback_slot_model = ""
    self._fallback_pick_gen = 0
    # The shared client's explicit-pick epoch at fallback activation. The
    # slot-local _fallback_pick_gen is invisible to a pick made through a
    # session alias (a channel-born slot and its dashboard twin share one
    # wire session and one client object); the restore probe compares this
    # shared epoch so an alias's explicit pick is not silently overwritten,
    # mirroring _refusal_client_pick_epoch on the refusal path.
    self._fallback_client_pick_epoch = 0
    # Content-filter (refusal) fallback state (agent.refusal_fallback_model).
    # _refusal_fallback_primary/_refusal_fallback_candidate are the models
    # to restore/verify at the start of the NEXT genuine turn after a
    # refusal retry swapped the live session (single-message semantics —
    # unlike the throttle fallback above, this swap never sticks).
    # _refusal_fallback_attempted is the one-attempt-per-user-message
    # guard: a refusal from the fallback too is terminal.
    self._refusal_fallback_primary = ""
    self._refusal_fallback_candidate = ""
    # The session binding the refusal swap ran under, captured ONCE at
    # swap time. The restore locks on THIS key (not a re-derived one) so
    # both seams always share one lock domain, and the retry replay is
    # recorded under it, so the drain purges the replay when the live
    # binding differs — a cron result binding an unbound slot mid-turn must
    # not route the replay onto the newly bound session.
    self._refusal_fallback_session_key = ""
    self._refusal_fallback_attempted = False
    # _model_pick_gen snapshot taken at refusal-swap time: a gen that moved
    # means an explicit user pick landed after the swap, and the restore
    # must respect it instead of stomping it with the old primary (same
    # rule as _fallback_pick_gen on the throttle path).
    self._refusal_pick_gen = 0
    # Snapshot of the shared CLIENT's explicit-pick epoch at refusal-swap
    # time: a pick through a session alias moves the client epoch without
    # touching this slot's generation, and the restore must see it.
    self._refusal_client_pick_epoch = 0
    #: The recovery replays this slot queued for itself -- the model-access
    #: swap, the lost-session reconnect, the image-history recovery, the
    #: content-filter retry and the auto-continuations -- each with the queue
    #: entry it is, the Stop counts and the session binding it was queued
    #: under. The drain and the turn's consume seam re-check them; see
    #: ``dashboard/recovery_replays.py``.
    self.replays = RecoveryReplays()
    # One-shot guard for the reactive model-access-denial fallback: a new
    # conversation whose configured model (commonly the "auto" sentinel) is
    # refused for entitlement, not throttled, is re-prompted at most ONCE on
    # the first advertised model this account can run, rather than failing
    # the first reply. One attempt only, so an account entitled to nothing
    # falls through to the terminal entitlement error naming what was tried
    # instead of looping. Refreshed at the start of a genuine user turn but
    # NOT when the incoming turn is the swap's own replay (the drain claims
    # it by its queue id in ``replays``), so a still-unentitled candidate
    # cannot trigger a second swap.
    self._model_access_fallback_used = False
    # One-shot guard for the post-token (text-only) transient retry: a turn
    # that has already streamed answer tokens may be re-prompted at most
    # ONCE on a transient 5xx (and only when no tool call fired). Reset on a
    # completed turn alongside _transient_5xx_retries.
    self._posttoken_retry_used = False
    # True after the slot's LAST turn ended on a STRUCTURAL terminal error
    # (a malformed-request rejection: the backend refused the payload's
    # SHAPE, so re-sending the identical context reproduces it). Read by the
    # auto-nudge fire path to STOP a self-prompting loop instead of firing
    # the same doomed context every interval; cleared at the start of every
    # genuine new turn (see chat_runner) so a human /clear-then-message, or
    # any turn with different context, re-arms the loop. Not persisted: a
    # gateway restart re-derives it from the next turn's outcome, and a loop
    # reloaded active simply fires once and re-learns the verdict.
    self._last_turn_structural_terminal = False
    # The id of the loop whose delivered wake set the flag above, so the fire
    # guard scopes the verdict to that loop and cannot deactivate a DIFFERENT
    # loop armed later on the same slot. Empty when the flag is False.
    self._last_turn_structural_terminal_loop_id = ""
    # The loop's config generation the malformed turn fired under; the fire
    # guard passes it to AutoNudgeService.update(expected_generation=...) so
    # the stop is applied under an atomic (id, generation) fence.
    self._last_turn_structural_terminal_loop_gen = 0
    # The loop whose own delivered cycle ended on a content refusal that
    # nothing retried, and the config generation it fired under. Empty when
    # the last turn was not such a cycle. The auto-nudge fire guard reads the
    # pair and stops that loop with ``model_refused`` instead of firing the
    # same prompt into the same model again; cleared at the start of every
    # genuine new turn, like the structural verdict above. Not persisted, for
    # the same reason as that verdict.
    self._last_turn_model_refused_loop_id = ""
    self._last_turn_model_refused_loop_gen = 0
    # Poisoned-conversation escalation (cross-cycle). Both cells below are
    # written only through ``poisoned_escalation.PoisonedConversationEscalation``,
    # the recovery-replay ledger's refund included. A cycle that EXHAUSTS
    # the transient-5xx ladder with ZERO output counts one pre-stream
    # exhaustion; consecutive exhausted cycles indicate the backend is
    # deterministically rejecting this session's persisted conversation
    # (not a momentary blip — a fresh session on the same gateway works).
    # At the threshold, chat_runner discards the native conversation
    # (clearing the poisoned resume sid, keeping the session-map entry)
    # and re-queues once. Streak broken only by a LANDED turn or a
    # non-matching terminal error.
    self._prestream_exhausted_cycles = 0
    # One-shot guard for that discard+retry: consumed when a discard is
    # enqueued, re-armed only by a LANDED turn — so a genuine prolonged
    # outage gets at most one fresh-conversation attempt, never a
    # discard loop.
    self._poisoned_reset_used = False
    # One-shot guard for the lost-backend-session recovery: a live backend
    # answering 'Session not found' gets ONE fresh process that re-loads the
    # mapped id and one retry of the turn. Re-armed only by a LANDED turn,
    # so a backend that keeps losing the session ends on a clear error.
    self._session_not_found_retry_used = False
    self._empty_response_retries = 0
    # True once any turn of the CURRENT empty-turn episode was productive.
    self._empty_episode_productive = False
    # First-token clock of the last top-level turn, reused by its recovery
    # turns until one saves the row (``chat_runner._turn_clock``).
    self._carried_ttft_clock = None
    # One bounded synthetic continuation when a turn ended on a promise-only
    # final message (announced an immediate action, then yielded with no tool
    # call). Reset like the other per-turn retry budgets on a landed turn.
    self._promise_only_retries = 0
    # One bounded synthetic continuation when the BACKEND compacted the
    # conversation mid-turn and then ended the turn without finishing the
    # work (see COMPACTION_RECOVERY_PREFIX). Bounded separately from the
    # promise-only budget: the two failure modes are independent, and a turn
    # that hits one must not be denied recovery from the other. Reset like
    # the other per-turn retry budgets on a landed turn.
    self._compaction_continue_retries = 0
    self._batch_rejected = False
    # WHO/WHAT set ``_batch_rejected``: empty for an interactive user
    # decline, else a short host-authored sentence naming the auto-decline
    # (approval timeout, no turn budget, Slack delivery failure). The
    # cascade site reads it to decide attribution: a user's refusal makes
    # kiro-cli's "User denied tool execution" TRUE for the cascaded
    # remainder, while a host-caused decline makes it false and worth a
    # cause-specific in-band correction. Same lifetime as the flag itself —
    # set together, cleared together.
    self._batch_rejected_cause = ""
    # Per-turn compaction-status failure tracking. Distinct from
    # SessionManager._compact_cooldown_until, which only gates the
    # *proactive* session-level auto-compact trigger — this gates the
    # per-turn EVENT_COMPACTION_STATUS notice path in chat_runner, which
    # without a backoff appends one near-identical "Compaction failed:
    # unknown error" message per turn indefinitely.
    # Retry budget for a turn the backend abandoned after a TRANSIENT
    # compaction failure. Distinct from _compaction_fail_streak above,
    # which only paces the NOTICE: this one bounds how many times the
    # abandoned message is re-queued. Reset on a landed turn alongside the
    # other recovery budgets.
    self._compaction_failed_retries = 0
    self._compaction_fail_streak = 0
    self._compaction_fail_cooldown_until = 0.0
    # Sync callable published by _run_chat alongside _acp_client (both cleared in
    # _turn_release_handles): flushes the turn's accumulated text as a finalized
    # assistant segment NOW. The steer handler calls it right BEFORE
    # persisting the steer user message, so the transcript order is
    # [assistant(pre-steer), user(steer), assistant(post-steer)] — matching
    # what the client rendered live — instead of the whole segment landing
    # BELOW the steer bubble at end-of-turn (and stranding the pre-steer
    # chunk entries above it, which _flush_segment's trailing-run walk could
    # then never reclaim). None when idle.
    self._steer_segment_cut = None
    # Native kiro-cli subagents run inside the parent ACP turn. Keep their
    # live and terminal state on the slot so reconnects can hydrate cards.
    self._native_subagent_tracker = {}
    self._native_subagent_output = {}
    # Delivery ids whose steer a NON-EMPTY echo actually accounted for. An
    # entry can leave `_pending_steers` for reasons that look identical
    # afterwards, and only a matched, non-empty echo is evidence of
    # consumption. `chat_delivery` therefore infers `consumed` from
    # positive evidence rather than from the entry being gone, so no
    # remover -- present or added later -- can turn an evidence-free frame
    # into a confirmed injection. Keyed on the delivery id, not the text,
    # so a later identical steer cannot inherit an earlier one's evidence.
    self._steer_confirmed = set()
    # Mid-turn steers handed to the backend but not yet confirmed consumed
    # (no steering_consumed / EVENT_STEER_CONSUMED echo yet). Appended by
    # the dashboard steer handler BEFORE the steer RPC's await (so a turn
    # dying mid-write still sees it), settled by _on_steer_consumed (steer_queue)
    # when the consumed echo arrives (matched against the echo's snapshot text),
    # and — the point of the mechanism — REQUEUED as ordinary queue cards
    # by _run_chat's finally when the turn dies first (stall-cancel, user
    # STOP, error). Without this, a steer swallowed by a dying turn
    # vanished with no trace (see the requeue site).
    self._pending_steers = []
    # Opaque id per in-flight steer, keyed by its text (the one-per-text
    # rule in chat_delivery makes that key unique). The requeue moves the id
    # onto the queue entry and the drain unions entry meta onto the row it
    # writes, which is how a caller can tell a delivery the drain already
    # persisted from one the running turn consumed — a distinction the bare
    # text cannot make.
    self._steer_delivery_ids = {}
    # The client's `sendId` for an in-flight steer that supplied one, keyed by
    # the same message text as `_steer_delivery_ids`. Kept in LOCKSTEP with
    # that map -- every site that removes a delivery id removes this too -- so
    # "present here" always implies "present there" and no reader has to ask
    # which of the two a half-finished path left behind. Only the requeue reads
    # it: it moves the id onto the queue entry's meta so the drained row
    # carries `meta.sendId` like an accepted steer's row does. A steer
    # that persists its own row stamps the id directly and drops this entry.
    self._steer_send_ids = {}
    # Whether an in-flight steer was typed by the session's OWN human, keyed by
    # the same message text as the two maps above and kept in the same LOCKSTEP.
    # The requeue reads it to decide `directive_user_origin`, which exempts a
    # queue entry from the drain's LINKED drop. That exemption exists because
    # "the author typed into the session's own surface" -- true of the composer,
    # false of a `session_send` steer from a peer -- so the requeue cannot
    # derive it from the slot and has to be told. Absent means NOT the session's
    # own human: an unrecorded steer fails closed into the ordinary drop.
    self._steer_user_origin = {}
    # Whether an in-flight steer arrived through a MESSAGING CHANNEL rather than
    # this slot's own composer, keyed and kept in the same LOCKSTEP as the map
    # above. The requeue reads it for `directive_channel_origin`: a requeued
    # steer runs as its own turn, and channel authority is the narrower
    # credential boundary (a directive that turn issues is filed as
    # channel-created, as a queued channel message's is). A steer the running
    # turn consumes never reads it -- an injected steer runs under that turn's
    # provenance, narrowed by the flag below. Absent means "not through a
    # channel", the composer's case.
    self._steer_channel_origin = {}
    # Whether a CHANNEL steer has been admitted into the turn this slot is
    # running. Set by `steer_into_running_turn(channel_origin=True)` at
    # admission -- before its RPC, since the client can inject the text and the
    # model can act on it before the RPC returns -- and read by the turn wherever
    # it stamps a directive's producer (`apply_session_directive`'s
    # `producer_is_channel`), so every directive the model emits after channel
    # text reached it is filed as channel-created, the narrower authority a
    # channel-origin turn's directives carry. The turn's opener provenance
    # (`_directive_channel_origin`) is a per-turn argument and cannot change
    # mid-turn; this flag is the slot-level seam that can. Held for the rest of
    # the turn, a declined or requeued steer included (narrowing is the direction
    # that cannot be wrong); the turn resets it at its end and at the next start.
    self._turn_channel_narrowed = False
    # The running turn's OPENER provenance: True while a turn a channel message
    # opened (`_run_chat(_directive_channel_origin=True)`) is in flight. Set at
    # the turn's outermost start and cleared at its end, beside
    # `_turn_channel_narrowed`; read by gateway routes a tool call reaches
    # mid-turn (the guide routes refuse a channel turn), which have
    # no other way to learn where the turn came from.
    self._turn_channel_origin = False
    # Whether the running turn is one the person sent from the dashboard
    # (`_run_chat(_directive_user_origin=True)` and not a channel's): set at
    # the turn's outermost start and cleared at its end beside
    # `_turn_channel_origin`. A loop wake, a cron or app injection, a
    # `session_send` and a sub-agent completion never set it, and a steer
    # that is not the user's clears it (`chat_delivery`), so the guide routes
    # that act for the user refuse them.
    self._turn_user_sent = False
    # The containment that held when an in-flight steer was AUTHORIZED, keyed by
    # the same message text and kept in the same LOCKSTEP. The requeue stamps it
    # on the queue entry instead of reading the slot again: its own moment is the
    # turn's teardown, on the far side of the steer RPC's suspension, so a mirror
    # linked during that suspension would be folded into the baseline and then
    # read as "held at admission" by the drain -- which is exactly the audience
    # the authorization refused. Absent means the entry carries NO containment
    # key, which puts it on the drain's fail-closed floor: checked against every
    # currently held constraint rather than against a baseline built at teardown.
    self._steer_admissions = {}
    # The `message.steer` decision row that chose the STEER path for an
    # in-flight steer, keyed by the same message text as the maps above and
    # kept in the same LOCKSTEP. Only the requeue reads it: the receipt is
    # stamped on the persisted row by the steer path itself and on the queue
    # entry by `queue_for_next_turn`, but a steer the turn's teardown requeues
    # takes neither of those writers -- the teardown is a different coroutine
    # that never sees the caller's argument -- so without this map the one
    # outcome a decision did choose lands as a queue entry with no receipt.
    # Absent for a manual steer, which has none to carry.
    self._steer_decision_strips = {}
    # Steers whose RPC died ambiguously (``AcpProcessDied.ambiguous_delivery``):
    # the frame may already have reached the turn. Left pending, so the
    # turn's teardown requeues them, and that requeue says they may already
    # have been delivered instead of re-sending the text as fresh.
    self._steer_possibly_delivered = set()
    # Steers whose ``client.steer()`` RPC has not returned yet. A turn that
    # ends meanwhile requeues them as possibly delivered: the frame may
    # already be in the pipe, and the RPC's own verdict arrives too late to
    # mark an entry the drain may already have run.
    self._steer_rpc_in_flight = set()
    # Admission snapshots of the peer steers that influenced THIS turn, keyed by
    # an opaque token. The turn consults them before publishing its CROSS-SURFACE
    # reply leg and withholds it when a constraint newly holds:
    # the steer RPC suspends on `stdin.drain()`, `_deliver_cross_surface_reply`
    # resolves the mirror live at reply-delivery time, and a fast turn can deliver
    # before the sender's post-RPC check resumes -- so reacting after the fact
    # cannot stop a reply that is already sent. An entry is recorded BEFORE the
    # RPC, synchronously with the authorization that admitted the send.
    #
    # The entry is RETAINED for the whole turn rather than released once the
    # sender's own check passes: the reply publishes later still, and a mirror
    # bound between that check and the publication would be just as unauthorized.
    # Keeping the admission here lets the decision be made where it can be exact
    # -- synchronously at delivery, against the containment holding THEN -- which
    # is also why an ordinary steer costs the channel audience nothing.
    #
    # TURN-SCOPED: the turn's teardown empties it, so one turn's withheld reply
    # never silences the next, whose authorization is its own.
    #
    # A second writer records the same shape for the opposite direction:
    # ``session_control.record_audience_admission`` stamps the CALLER's own
    # containment here from the caller-side admission gates (every
    # session-control verb's, and the work ledger's), because what a verb
    # returns becomes part of this slot's reply and the same publisher resolves
    # the mirror live at delivery -- a mirror gained or retargeted between the
    # admission and the reply would publish a peer's transcript or a ledger to an
    # audience the admission never saw. One entry per distinct audience per turn,
    # keyed by the snapshot under ``AUDIENCE_ADMISSION_KEY_PREFIX`` so polling
    # does not grow it. One record, one publisher check, both directions.
    self._steer_audience_fences = {}
    # How many channel steers hold each AUDIENCE-keyed fence above. A channel
    # hand-off records one fence per distinct containment snapshot per turn
    # (`channel_handoff.audience_fence_key`), so several messages share one
    # record; each admission counts a holder, and a steer whose text does not
    # enter the running turn (declined, unavailable, requeued) releases its
    # hold. The record is popped only when no holder remains, so a fence a
    # landed sibling relies on survives a sibling's decline, while a fence
    # nothing holds does not withhold the reply of a turn that never received
    # the channel text. The peer path's per-token fences are not counted here.
    # Cleared with the fences at the turn's teardown.
    self._steer_audience_fence_holders = {}
    # Validated attachment lists for a pending steer. Requeue moves them
    # to the queue entry; a consumption echo releases them after an accepted
    # steer has stamped its own row.
    self._steer_attachment_meta = {}
    # Pending steers the composer's own human sent, held with the lists above
    # and released the same ways. Requeue stamps ``STEER_INTENT_META`` on the
    # entry, so the sub-agent hold lets it drain instead of parking it.
    self._steer_act_now = set()
    # Pending AUTOMATION steers (a work-ledger wake), each with the
    # ``meta.nudge`` its row carries. Requeue stamps it on the entry
    # (``AUTOMATION_STEER_META``), so the drained row is a nudge row too.
    self._steer_automation = {}
    # In-flight `wait` tool sleep, as reported by the tool's own keepalive
    # ping: {"wait_id": str, "seconds": int, "deadline_ts": float}. The
    # deadline is on the dashboard's clock (see api_session_keepalive) so
    # the browser can count down against it directly. None whenever no wait
    # is sleeping.
    self._wait_state = None
    # wait_id the user asked to end early, parked here until the sleeping
    # tool collects it on its next poll. Consumed exactly once.
    self._end_wait_request = None
    # Slot key of the session that parked ``_end_wait_request`` through
    # ``session_end_wait``; "" when the End-wait button parked it. Both
    # writers set it together with the request, so it is only ever read
    # alongside a request it describes and needs no clearing of its own.
    self._end_wait_by = ""
    # Wall clock of the tracked wait's last keepalive ping. Server-side only
    # (deliberately NOT in to_dict): it is the heartbeat that distinguishes a
    # sleep that ended from one that is still running, which is how
    # _service_wait_ping tells a legitimate hand-over from two concurrent
    # waits colliding on one session key.
    self._wait_last_ping = 0.0
    # The session's steer stamp as it read when the tracked wait was minted.
    # Server-side only (deliberately NOT in to_dict). A steer newer than
    # this baseline is what ends the sleep early; re-reading it per sleep is
    # what stops ONE unconsumed steer from ending every subsequent sleep in
    # the turn and handing the model a `wait` that returns instantly.
    self._wait_steer_baseline = 0.0
    # True once two sleeps have been seen sharing this slot: neither may be
    # tracked or ended, because there is no way to know which one the user is
    # looking at. Latched for the rest of the turn and cleared by the same
    # turn-end block that clears the two fields above -- an earlier revision
    # expired it on a timer, which let whichever sleep pinged first after
    # expiry re-publish its deadline onto the other's pill.
    self._wait_contested = False
