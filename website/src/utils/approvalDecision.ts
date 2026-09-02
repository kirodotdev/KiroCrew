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
