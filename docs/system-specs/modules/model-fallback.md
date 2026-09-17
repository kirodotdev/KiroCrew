# Model fallback

`agent.fallback_model` controls an optional retry after an active model exhausts
its transient-error budget before activity begins. The implementation is
Kiro Crew-side: `llm_helpers.advance_fallback_candidate` changes the model through
the substitute `set_model` seam, and `test/test_llm_helpers.py` pins that a
successful swap is observable before it is recorded.

## Configuration

`AgentConfig.fallback_model` is normalized by
`config/loader.py:coerce_fallback_model`. `llm_helpers.configured_fallback_chain`
is the shared derivation used by the fallback callers:

- The automatic-routing sentinel is the field default; its configured value
  produces a chain containing that sentinel.
- A concrete model value produces a chain that tries the configured value before
  the automatic-routing sentinel.
- An empty value produces an empty chain, so the fallback branch remains inert
  and the existing terminal error path handles the failure.

`TestFallbackModelLoad` in `test/test_config_loader.py` and
`TestCoerceFallbackModel` in `test/test_role_models.py` pin the default,
normalization, concrete-value ordering, and empty-value opt-out. The dashboard
PATCH schema in `dashboard/handlers/core.py` applies the model-id grammar and
`_validate_role_model`; that validator allows the automatic and empty values,
rejects a known unusable concrete value, and permits a concrete value when no
advertised-model set is available.

## Trigger and candidate walk

The fallback path is eligible only after the normal transient retry budget is
spent on a transient error with no qualifying prior activity. Each surface
tracks that condition in its own stream loop: `llm_helpers.stream_and_collect`
requires no result text or tool activity, `dashboard/chat_runner.py` requires
no emitted or thought activity, and `subagent._stream_with_transient_retry`
requires no activity. Post-activity recovery does not enter the model-fallback
walk.

`llm_helpers.advance_fallback_candidate` owns candidate selection for all three
paths. It preserves the original primary from an existing provider marker when
available, otherwise from the active model. It then skips the primary, the
currently active model, and candidates absent from a known advertised-model
set. Membership and both skips are judged through `resolve_pin_spelling`, so a
persisted chain entry carrying a stale `<namespace>::<bare-id>` qualifier still
matches a backend that advertises the bare id; an entry absent under both
spellings stays skipped. An unavailable advertised set does not reject a
candidate; this is load-bearing because entitlement cannot be determined
without that set.

The helper calls the substitute `set_model` path and verifies that the serving
model changed before it records the candidate, publishes `TURN_FALLBACK_ATTR`,
or logs the swap. This witness prevents a non-raising no-op model selection from
being announced as a fallback. The wire call, the witness, the marker, and the
walked/active records all carry the advertised spelling of the candidate
(`_fallback_wire_spelling`); the chain's own spelling drives only the walk
bookkeeping. `TestSetModelWitness`,
`TestAdvanceFallbackCandidateAutoPrimary`,
`TestAdvanceFallbackCandidateNamespacedChain`, and `TestFallbackState` in
`test/test_llm_helpers.py` cover no-op handling, marker-seeded primary
selection, advertised-model filtering, spelling folds, and chain progress.

`FallbackState.should_retry_active` owns the per-candidate retry allowance, and
`fallback_rewound_transient_budget` derives the dashboard counter from the same
allowance. The pinning tests in `test/test_llm_helpers.py` and
`TestRunChatModelFallback` in `test/test_dashboard_chat.py` prevent the three
surfaces from granting different candidate budgets.

When no candidate can advance, `FallbackState.exhaustion_story` describes the
candidates actually tried. `stream_and_collect` and the sub-agent attach that
story to the terminal exception for their delivery paths; the dashboard retains
its slot-local walked candidates to render its terminal error. `fallback_story_of`
redacts and bounds the attached story centrally, and `append_fallback_story`
uses it for cron alerts, sub-agent errors, and heartbeat failure logs. Central
handling is load-bearing because fallback model values are configuration input
and each error surface must receive the same safe text.

## Surfaces

- `llm_helpers.stream_and_collect` accepts a caller-supplied fallback chain.
  The Slack gateway passes `configured_fallback_chain()` for its cron and
  heartbeat work, then `annotate_model_fallback` prefixes a delivered result
  while the provider marker remains active.
- `dashboard/chat_runner.py` swaps through `_fallback_swap_for_turn` after its
  pre-activity transient branch, persists a notice, and requeues the same
  message as a synthetic recovery item. Its fallback condition excludes nested
  prompts. On exhaustion it renders the slot-local walk in the terminal error.
- `subagent._stream_with_transient_retry` follows the zero-activity branch,
  uses the shared candidate helper, and applies `annotate_model_fallback` to
  the completed result.

`annotate_model_fallback` redacts model values before rendering and leaves the
marker in place. That persistence is load-bearing: subsequent successful turns
on the fallback remain visibly identified until restoration succeeds.

## Sticky restore

A successful swap is sticky on the provider through `TURN_FALLBACK_ATTR`.
`probe_fallback_restore` makes one start-of-turn attempt to restore the primary
for unattended callers. It keeps the fallback on a transient restore failure;
if the session no longer serves the recorded fallback, it clears stale state
without selecting a model.

The dashboard adapter,
`chat_runner._probe_fallback_restore_for_slot_locked`, applies the same probe to
slot-held state. It snapshots `slot.model` and `_model_pick_gen` when fallback
activates. Explicit single-slot and bulk model picks, plus a provider switch,
increment the generation; a rejected single-slot pick restores its prior
value. A changed generation makes the fallback record stale, so restore cannot
override an explicit user choice. When automatic provider backfill changes
`slot.model` without changing the generation, the restore hook reinstates the
activation snapshot before clearing fallback state. This prevents an automatic
fallback selection from becoming a persistent user pin.

`TestRunChatModelFallback` and `TestSlotProbeWrapsSharedRestoreBody` in
`test/test_dashboard_chat.py`, together with the restore tests in
`test/test_llm_helpers.py`, pin the stale-record, explicit-pick, and backfill
invariants.

## Refusal fallback (`agent.refusal_fallback_model`)

A second, independent fallback with a different trigger: the model's CONTENT
FILTER declined the turn (`STOP_REASON_REFUSAL`), which the transient ladder
never enters because a refusal is deterministic for the model that issued it.
The premise of this feature is that refusal is NOT deterministic across model
families — a different model routinely accepts what another's filter declined
— so the dashboard retries the declined message once on a configured model.

- `AgentConfig.refusal_fallback_model` is normalized by
  `config/sections.py:coerce_refusal_fallback_model`: the empty default
  disables the feature (junk input collapses to disabled, never to enabled);
  the automatic sentinel defers to the `recommended_model` the provider's
  refusal envelope names, resolving to disabled when it names none; a concrete
  id is used directly. The dashboard PATCH schema applies the same grammar and
  validator as `agent.fallback_model`.
- `chat_runner._refusal_fallback_retry` (nested in `_run_chat`) gates the
  retry: interactive prompts only (no nested depth, and only a turn whose
  ledger actor is `user` — a cron, autonudge, or sub-agent wake keeps the
  terminal refusal, since nobody attends its announced swap), not while a
  Stop is suppressing requeues, never after the turn dispatched a tool
  (`_turn_tool_calls > 0` — replaying the whole message would run the side
  effect twice; streamed text alone stays retryable), never while a USER
  follow-up is already queued (`_has_user_queued_followup` — user speech in
  the queue is the user's next
  intent, often a correction of the refused message, and the replay's
  index-0 insert would jump it; a queued cron notification or sub-agent
  completion is orchestration and does not suppress the retry), and at most
  once per user message
  (`slot._refusal_fallback_attempted`) so a refusal from the fallback too is
  terminal and renders the ordinary card plus a fallback-also-declined note.
  The Stop and queue gates are re-checked after the swap lands: a stop or a
  follow-up arriving during the `set_model` await unwinds the swap through
  the witnessed restore helper and surfaces the refusal instead. Both gates
  read the monotonic `_stop_pressed()` counters alongside the stop-state
  check, so a Stop that pressed and already resolved back to idle during
  the turn (or during the swap await itself) still suppresses the retry —
  the state check alone would miss it, and the enqueue-time stop-generation
  snapshots would bake the moved counter in, blinding the drain purge.
- `chat_runner._refusal_fallback_swap` performs one explicit hop through the
  substitute `set_model` seam with the same silent-no-op witness as the chain
  walk, records `(primary, candidate)` on the SLOT (deliberately not
  `TURN_FALLBACK_ATTR`, whose start-of-turn probe would restore the primary
  before the retry ran), snapshots `_model_pick_gen` so a later explicit pick
  is detectable at restore, and refuses a candidate equal to the refusing
  model. When the session is unpinned (the automatic-routing sentinel, so the
  active model reads as unknown), the swap runs only when the sentinel is
  advertised as a switch target: the restore leg would have to
  `set_model("auto")`, and a partition that does not serve the sentinel
  refuses it, which would strand the session on the candidate for every
  later turn. Unprovable return leg → the refusal surfaces unswapped. A
  CHAINED swap — a second refusal arriving while a kept record still
  names the true primary (the prior restore failed or silently no-oped) —
  preserves that recorded primary instead of overwriting it with the stale
  fallback the session is stranded on, so the eventual restore returns to the
  user's real model rather than to fallback A. While the record is live,
  both `slot.model` backfill sites treat the
  session like an active throttle fallback: the provider's resolved model is
  the temporary candidate and must never be persisted into the durable pin,
  and usage rows attribute to the model that actually served the turn.
- The declined message is requeued verbatim at queue index 0 as a synthetic
  recovery item, carrying the refused turn's attachment lists in their
  original typed form (`files` and `dirs` stay distinct meta keys, so a
  folder attachment retries as a folder). Because the replay is the user's
  own words, it cannot be
  recognized by the fixed synthetic-recovery texts — and not by text at
  all: the drain redacts credentials and exfiltration URLs after the swap
  records the raw message, so string equality breaks for exactly the
  content most likely to draw a refusal. Recognition is the drained
  entry's queue id, matched by the drain (the entry must drain alone; a
  merge folding user input into the dispatch is a correction, not the
  retry) and passed into the runner as a parameter. The consuming turn
  re-validates before honoring it — recorded binding, slot and session
  stop generations, pending steers, queued user follow-ups — because the
  drain's own checks run before the turn task is spawned and a Stop,
  correction, or rebind can land in that window; a tripped check aborts
  the replay with the same cancellation notice the drain purge uses, the
  allowance stays spent, and the model swap is unwound by the next genuine
  turn's restore probe. A genuine new message drops a stale record (a Stop
  can purge the queued replay) and re-arms the one-retry allowance —
  GENUINE meaning user-origin: a kind-tagged recovery requeue of the
  user's own words (a pre-output process failure re-queueing the turn)
  never re-arms, and a recovery requeue of the replay itself — whichever
  recovery family issued it — carries the
  retry identity onto the fresh entry with refreshed stop snapshots, so
  the re-run is still the episode's one retry rather than a fresh episode
  (a crashing fallback replay cannot re-arm and cycle), and the drain's
  refusal-specific validation covers the requeue-to-dispatch window a
  family's own requeue-time guards cannot see.
- Restore is single-message: at the start of the first turn that is neither
  the replay nor a synthetic recovery continuation (a runner-authored
  continuation of the fallback turn must finish on the model that produced
  it), `chat_runner._restore_refusal_fallback` moves the session back to the
  recorded primary — a definite restore, not a probe, because the primary is
  not throttled. The record is applied only under the binding the swap
  recorded: when the slot has since rebound to a different session, the
  client on hand serves the rebound session, and applying the record
  through it would move that session's model to a primary it never chose —
  the restore drops the record with a warning instead (the recorded
  session is unreachable through the slot). It runs immediately
  after session acquisition, before the
  `slot.model` backfill and any model-window-dependent prompt construction,
  so the genuine turn is assembled against the primary's context window. The
  restore is witnessed like the swap: a non-raising `set_model` that did not
  move the model keeps the record for the next turn instead of stranding the
  session on the fallback. A pick-generation that moved since the swap, or a
  session already off the candidate by other means (explicit pick, reset),
  clears the record without selecting a model; a failed restore keeps the
  record for the next turn. The explicit-pick guard is two-layered because
  two slots can drive one wire session (a channel-born slot and its
  dashboard alias share `effective_session_key`): the slot-local pick
  generation covers picks on this slot, and a CLIENT-scoped explicit-pick
  epoch — stamped by the live-switch path on the shared client object and
  snapshotted at swap time — covers a pick made through any alias. The
  stamp lands immediately after `set_model` succeeds, before the effort
  reapply: the wire session serves the pick from that moment, so a reapply
  failure that falls back to reset must not leave the landed pick
  unstamped for a sibling restore to overwrite. A pick
  that took the session-reset path replaces the client entirely; the
  moved-off check catches it except when the alias picked exactly the
  candidate, an accepted compound-corner residual. Both the swap and the
  restore run under the same session-scoped switch lock the pick handlers
  take (`llm_helpers.slot_switch_session_lock`), acquired before the slot's
  pick lock. The lock keys off the TURN's binding, captured once at turn
  start and threaded into the swap, which stamps it on the slot; the
  restore locks on that recorded key rather than a live re-derivation. A
  slot's binding is mutable mid-turn (a cron result binds an unbound slot,
  a relink moves a bound one), and deriving at each seam would put swap and
  restore in disjoint lock domains while the replay drained onto a session
  the refused turn never ran on — the drain purges the replay when the
  live binding differs from the recorded one (the unbound-to-bound case is
  already dropped by the admission sweep's fail-closed booleans; the
  recorded-key check catches the bound-to-bound relink those booleans
  cannot see, and clears the episode's dispatch-gate record either way).
  The restore reads the epoch snapshot once before its
  `set_model` await, so without that serialization a pick landing inside
  the await window would be applied first and silently overwritten when the
  restore's write completed last; the swap writes the snapshot AFTER its
  own `set_model` await, so an unserialized pick landing inside that await
  would be folded into the snapshot and blind the restore's guard. Under
  the lock the switches are strictly
  ordered — a pick either completes first (the epoch check drops the
  record) or starts after the restore finishes and wins by ordering. Two seams cooperate with that rule. The
  same-value pick short-circuit in `chat_handlers` (the "nothing to switch"
  early return) is suppressed while either fallback record is live, so a user
  re-picking the displayed primary actually moves the wire model back — the
  pick-generation bump then makes the restore a deliberate no-op rather than
  the only path off the fallback. And when the moved-off model is explained
  by an ACTIVE throttle walk whose recorded restore target is the refusal
  candidate (the walk advanced off it mid-retry), the restore hands over
  instead of dropping: it rewrites the walk's restore target to the refusal
  primary, so the walk's own restore returns the session there rather than to
  the intermediate candidate.

The refusal retry is dashboard-only. Unattended surfaces (cron, heartbeat,
sub-agents) keep the terminal refusal behavior.

## Non-goals

- Swapping models after activity has begun on the transient/throttle path;
  the existing continuation recovery handles that path. (The refusal fallback
  above is a distinct trigger and replays the whole message instead of
  continuing mid-turn.)
- Per-crew, per-cron, or per-role fallback chains.
- A dedicated per-turn model field on the ACP wire.
- kiro-cli changes.

## Tests

`test/test_llm_helpers.py` covers chain derivation, candidate selection, retry
state, story handling, and unattended restore. `test/test_dashboard_chat.py`
covers dashboard fallback and slot restore. `test/test_subagent_turn_resilience.py`
and `test/test_cron_gateway_integration.py` cover sub-agent and gateway wiring.
`test/test_config_loader.py`, `test/test_role_models.py`, and
`test/test_dashboard_handlers_core_coverage.py` cover configuration loading and
PATCH validation.
