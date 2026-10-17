/**
 * Lifetimes for a timed command-scoped trust grant.
 *
 * The approval card offers two timed choices beside the chat-long grant: an
 * hour, and the rest of the user's own day. The client turns the choice into
 * whole seconds because only the browser knows where the user's day ends; the
 * server re-checks the number and refuses anything outside 1..MAX, so this
 * module shapes a request and never widens one.
 */

/** The server's ceiling on a timed grant, in seconds (one day). */
export const MAX_TIMED_GRANT_SECONDS = 24 * 60 * 60

/** The timed choices the trust menu offers, in menu order. */
export type GrantDuration = 'hour' | 'today'

export const GRANT_DURATIONS: readonly GrantDuration[] = ['hour', 'today']

const HOUR_SECONDS = 60 * 60

/** Whole seconds from `now` until the next local midnight, within 1..MAX.
 *
 *  Rounded up, so a grant made mid-second still lasts until midnight and the
 *  end time the menu shows is midnight itself. */
export function secondsUntilEndOfDay(now: Date): number {
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1)
  const seconds = Math.ceil((midnight.getTime() - now.getTime()) / 1000)
  return Math.min(MAX_TIMED_GRANT_SECONDS, Math.max(1, seconds))
}

/** The `ttl_seconds` a timed grant sends for `duration`, measured from `now`. */
export function grantTtlSeconds(duration: GrantDuration, now: Date): number {
  return duration === 'hour' ? HOUR_SECONDS : secondsUntilEndOfDay(now)
}

/** When a timed grant for `duration` chosen at `now` ends, as epoch milliseconds. */
export function grantEndsAt(duration: GrantDuration, now: Date): number {
  return now.getTime() + grantTtlSeconds(duration, now) * 1000
}
