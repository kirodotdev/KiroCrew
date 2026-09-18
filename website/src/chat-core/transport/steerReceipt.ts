import type { SendReceipt } from './sendTurn'

/**
 * The steer receipt policy, owned once (issue #9457).
 *
 * A `sendTurn({ steer: true })` receipt has to become transcript rows, composer
 * restores and queue-card stash entries under one fixed set of rulings. Those
 * rulings lived in THREE hand-synced copies after #8852 -- `ChatPage.steerMutation`,
 * `ChatPane.doSteer`, and `ChatPane.doSend`'s steer-flagged branch -- each pinned
 * by its own tests, so they could drift apart with CI green. That is the exact
 * failure mode #2240 found for the queue-card recipe before it was owned once in
 * `useQueuedMessageActions`; this is the same shape for the steer receipt.
 *
 * The rulings, identical across every host (read from #9457, not reconstructed
 * from one copy):
 *
 * - `refused` / `transport-error` -- the server said no, or the fetch rejected
 *   without a response. Drop the optimistic bubble (left standing it is a false
 *   third copy of the text next to the error row and the refilled composer),
 *   report the reason, and restore the raw text + files to the SENDING slot.
 * - `response-late` -- the abort deadline fired; delivery is indeterminate.
 *   Unless an echo carrying this send's `sendId` already reconciled the bubble,
 *   drop the bubble, restore, and warn (a duplicate is visible and deletable, a
 *   lost steer is not).
 * - `unknown` -- a 2xx whose body would not parse. Confirms nothing; leave the
 *   bubble as is.
 * - `steered` (`body.steered`) -- the server injected it; the `steer_push` echo
 *   owns the row. Leave the bubble as is.
 * - `queued` -- the slot was busy and the server queued the message. Bind
 *   `{ raw, files, sent }` to `queue_id` so the card's cancel restores typed
 *   text, then demote the bubble.
 * - `dispatched && !steered` -- a demotion that landed on a fresh turn. Its
 *   correlated user echo owns the row: demote the optimistic steer bubble to a
 *   plain user row via `resolveBubble('turn')`.
 *
 * The `sendId`-echo short-circuit (`(response-late | transport-error) &&
 * echoReconciled` -> do nothing) runs FIRST, before any ruling, because a
 * confirmed echo is stronger evidence than a missing HTTP response -- including
 * when a steer raced onto a new turn and lost its steer flag.
 *
 * How to REACT stays host policy, injected through {@link SteerReceiptAdapter}:
 * each host owns its own composer restore, its own error/notice rows, its own
 * bubble reducer, and (for the partial doSend copy) its own dispatched-row and
 * demoted-stash bindings. The helper decides WHICH ruling applies; the adapter
 * decides HOW the host expresses it.
 */
export interface SteerReceiptAdapter {
  /** Whether an echo carrying this send's `sendId` has already reconciled the
   *  optimistic bubble. When true, a `response-late` / `transport-error`
   *  receipt is ignored: the steer provably landed. A host that mints no
   *  reconcilable bubble (or no `sendId`) returns `false`. */
  echoReconciled: () => boolean
  /** Restore the raw typed text and files to the SENDING slot's composer.
   *  Called on `refused`, `transport-error` and `response-late`, with the
   *  triggering status passed in so a host can gate differently per ruling.
   *  The partial doSend copy needs exactly that: it restores a `refused`
   *  regardless of whether a bubble was minted (the send never ran), but
   *  restores a `response-late` only when NO bubble was minted (a minted bubble
   *  stays pending to avoid a duplicate). A host whose send did not consume the
   *  composer (an option-chip send) may make this a no-op.
   *
   *  This is the ONLY restore the helper triggers: {@link reportFailure} reports
   *  and does not restore, so `refused` restores exactly once. */
  restore: (status: 'refused' | 'transport-error' | 'response-late') => void
  /** Announce, in the transcript that owns the message, that the send failed
   *  (`refused` / `transport-error`) -- the server's `reason` when there is one.
   *  Reports ONLY: the composer restore is {@link restore}'s job, so a host
   *  must not also hand the payload back here or a `refused` restores twice.
   *  Distinct from {@link warnUnconfirmed}: a refusal is a known failure, an
   *  unconfirmed delivery is indeterminate. */
  reportFailure: (reason: string | undefined, status: 'refused' | 'transport-error') => void
  /** Post the WARN-tone `delivery_unconfirmed` notice for a `response-late`
   *  whose delivery could not be confirmed. */
  warnUnconfirmed: () => void
  /** Resolve the optimistic steer bubble.
   *  - `'drop'` removes it (`refused` / `transport-error` / `response-late` /
   *    `queued` demotion): the server-side row, if any, is the representation
   *    and a standing bubble would assert a delivery that did not happen.
   *  - `'turn'` demotes it to a plain user row (`dispatched && !steered`): the
   *    steer fell onto a fresh turn and its correlated echo owns the row.
   *  A host that reconciles the dispatched row a different way maps its own
   *  bookkeeping onto the `'turn'` arm: the doSend copy calls
   *  `confirmOptimisticSend` there (and makes the `'drop'` arm a no-op, since it
   *  mints no optimistic steer bubble). The firing condition for `'turn'` is
   *  exactly `dispatched && !steered`, so there is no need for a separate
   *  dispatched-row hook -- this arm already IS it. */
  resolveBubble: (outcome: 'drop' | 'turn') => void
  /** Bind the pre-send composer state to the queue entry the send became, so
   *  cancelling that card restores the typed text and re-stages the files
   *  (#560). `queueId` is `receipt.body.queue_id`, already validated present. */
  stashDemoted: (queueId: string) => void
}

/**
 * Apply the steer receipt policy for one `sendTurn({ steer: true })` outcome.
 *
 * Pure control flow over {@link SteerReceiptAdapter}: it reads only the receipt
 * and calls the adapter. No store, no i18n, no DOM -- every side effect is the
 * host's, so the one set of rulings is testable in isolation and every host
 * that adopts it can no longer drift from the others.
 */
export function applySteerReceipt(receipt: SendReceipt, adapter: SteerReceiptAdapter): void {
  // A confirmed echo is stronger evidence than a missing HTTP response,
  // including when a steer raced onto a new turn and lost its steer flag. This
  // runs before every ruling so an indeterminate transport outcome never
  // clobbers a bubble the server already owns.
  if ((receipt.status === 'response-late' || receipt.status === 'transport-error')
    && adapter.echoReconciled()) return

  if (receipt.status === 'refused' || receipt.status === 'transport-error') {
    adapter.resolveBubble('drop')
    adapter.reportFailure(receipt.reason, receipt.status)
    adapter.restore(receipt.status)
    return
  }

  if (receipt.status === 'response-late') {
    adapter.resolveBubble('drop')
    adapter.restore('response-late')
    adapter.warnUnconfirmed()
    return
  }

  // A 2xx whose body would not parse: accepted, but `steered` is the one shape
  // the bubble's claim is true for and an unreadable body confirms nothing.
  if (receipt.status === 'unknown') return

  // The server injected it: the steer_push echo owns the row.
  if ((receipt.body as { steered?: boolean }).steered) return

  // A queued demotion: bind the pre-send composer state to the card before
  // demoting the bubble, so cancelling the card is lossless (#560).
  if (receipt.status === 'queued'
    && typeof receipt.body.queue_id === 'string'
    && receipt.body.queue_id) {
    adapter.stashDemoted(receipt.body.queue_id)
  }

  // `dispatched && !steered` landed on a fresh turn. Demote the optimistic
  // steer bubble to a plain user row via the `'turn'` arm and let the
  // correlated echo reconcile it. A host that confirms its dispatched row a
  // different way (doSend's confirmOptimisticSend) maps that onto its own
  // `resolveBubble('turn')`; the firing condition is identical, so no separate
  // hook is needed.
  adapter.resolveBubble(receipt.status === 'queued' ? 'drop' : 'turn')
}
