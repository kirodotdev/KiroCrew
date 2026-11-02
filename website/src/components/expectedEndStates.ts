/**
 * The closed list of EXPECTED end states: outcomes that arrive through a
 * refused request or a stopped loop, yet mean nothing on the user's side went
 * wrong. Each renders as status (`StatusNotice`, `role="status"`), never as an
 * `ErrorNotice`. Everything NOT on this list stays an error notice, as the
 * `errors-use-error-notice` rule says (`website/AUTOSDE.yaml`), which names this
 * list as its one exception.
 *
 * - `approval_gone` — a press on an approval that is already expired, decided
 *   elsewhere or replaced (the gateway answers 404, or 400 "no pending
 *   approval"). The request it would have answered no longer exists. #18203.
 * - `interrupted_cycle` — a loop cycle cut off by a restart mid-fire. The cycle
 *   is owed, not spent, and resumes on its own. #21637.
 *
 * The list is fixed: adding an entry is a product decision, recorded with the
 * rule, not something a surface does to quiet its own failure. This file is
 * self-contained (no imports) so it stays byte-identical wherever it is carried.
 */
export const EXPECTED_END_STATES = Object.freeze(['approval_gone', 'interrupted_cycle'] as const)

/** One entry of `EXPECTED_END_STATES`. */
export type ExpectedEndState = (typeof EXPECTED_END_STATES)[number]

/** Is `state` one of the listed expected end states (status, not an error)? */
export function isExpectedEndState(state: unknown): state is ExpectedEndState {
  return typeof state === 'string' && (EXPECTED_END_STATES as readonly string[]).includes(state)
}
