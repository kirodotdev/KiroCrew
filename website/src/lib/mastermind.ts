/** Pure Mastermind facts shared by the store, the sidebar and Settings (see
 *  `hooks/useMastermind.ts` for the query). */

/** The one slot key the backend treats as the Mastermind session. */
export const MASTERMIND_SLOT_KEY = 'mastermind'

/** Is a failed request the backend's 409 `mastermind_protected` refusal (a close,
 *  archive or delete of the Mastermind session while it is enabled)? */
export function isMastermindProtected(err: unknown): boolean {
  const e = err as { status?: unknown; code?: unknown; body?: unknown } | null
  if (!e || typeof e !== 'object' || e.status !== 409) return false
  if (e.code === 'mastermind_protected') return true
  if (typeof e.body !== 'string') return false
  try {
    return (JSON.parse(e.body) as { code?: unknown } | null)?.code === 'mastermind_protected'
  } catch {
    return false
  }
}

/** Is `key` the Mastermind session while the store says it is enabled? The one
 *  test every client close path asks (the close thunk refuses it; the confirm
 *  prompts in front of it are skipped, since no close will follow). */
export function isMastermindLockedIn(state: { dashboard: { mastermindEnabled?: boolean } }, key: string | null | undefined): boolean {
  return key === MASTERMIND_SLOT_KEY && state.dashboard.mastermindEnabled === true
}
