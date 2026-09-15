/**
 * The decision vocabulary a resolved approval can carry, and the ONE predicate
 * that answers "was this a user rejection?".
 *
 * WHY A SHARED PREDICATE: `meta.resolved` is written by the BACKEND
 * (`_mark_permission_resolved`) with the raw decision token, so every consumer
 * sees `rejected_once` as well as `rejected`. Three sites used to equality-match
 * `'rejected'` alone, and an equality match fails one-sidedly when the
 * vocabulary widens: the row stops reading as a user rejection and falls through
 * to the auto-deny branch, which paints a deliberate human rejection as a
 * security-policy block. Adding the next token must be a one-line change here,
 * not a hunt for equality matches.
 */
const REJECTED_DECISIONS = ['rejected', 'rejected_once']

/** True for every decision token that means "the user refused this call". */
export function isRejectedDecision(decision: unknown): boolean {
  return typeof decision === 'string' && REJECTED_DECISIONS.includes(decision)
}

/** What the one-shot endpoint is able to honor. Not exported: it is only the
 *  declared return of `toApiDecision`, and a caller that needs the union can
 *  reach it through `ReturnType<typeof toApiDecision>`. */
type OneShotAction = 'approve' | 'reject' | 'reject_once'

/**
 * The ONE mapping from a UI decision token onto the one-shot approval endpoint.
 *
 * `POST /api/approvals/{id}/{action}` honors exactly `approve`, `reject` and
 * `reject_once` (`dashboard/handlers/sessions.py`), and records NO standing
 * grant — the next identical call prompts again. A trust verb (`trust`,
 * `trust_reads`, `trust_command`, `trust_base`) therefore has no representation
 * here at all, and belongs on a grant-recording endpoint instead
 * (`/api/chat/slots/{slot}/approve`, or the channel route).
 *
 * WHY THIS IS FAIL-CLOSED, and why the default is `reject` rather than
 * `approve`: mapping an unrecognized verb to `approve` runs the tool once while
 * the UI reports the standing grant the user asked for, so the backend records
 * nothing and the user's next identical action prompts again — which reads as
 * the grant having been FORGOTTEN rather than never made. Three surfaces
 * shipped that independently (#5400 spawn-approval card, #5434 collapsed tool
 * row, #5486 ChatInput), which is the signal that the rule was not written
 * anywhere a fourth author would have to read. This is that place.
 *
 * WHY A SHARED MAPPING AND NOT A LOCAL TERNARY: the backend rejects a trust
 * verb SENT to the one-shot endpoint with a 400, and `api.resolveApproval` is
 * typed to the three honored actions — but neither check can fire, because a
 * local `action === 'rejected' ? 'reject' : 'approve'` converts the verb into
 * `approve` BEFORE the call is made. By then the evidence that standing trust
 * was requested is destroyed, so no runtime guard downstream can recover it.
 * The mapping is the only layer that still sees the verb as itself, so it has
 * to be single. `eslint-rules/approval-one-shot-decision.js` enforces that every
 * `resolveApproval` argument comes from here rather than from a fresh ternary.
 */
export function toApiDecision(decision: string): OneShotAction {
  if (decision === 'approved') return 'approve'
  if (decision === 'rejected_once') return 'reject_once'
  return 'reject'
}
