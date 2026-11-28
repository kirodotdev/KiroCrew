/**
 * connectBackoff — how long auto-connect leaves a crew alone after its connects
 * keep failing.
 *
 * Auto-connect (hooks/useAutoConnectInstances.ts) fans out on every poll of the
 * ['instances'] list, once a minute. A crew whose connect fails for a reason that
 * outlives a minute (its host refuses the SSH key, its remote gateway is down)
 * would otherwise get a fresh SSH session and token mint on every poll, all day.
 * After each failed auto-connect the crew waits twice as long as the last time,
 * from one poll up to `CONNECT_BACKOFF_MAX_MS`, so a short drop still comes back
 * on the next poll and a lasting one costs a few attempts an hour.
 *
 * Only the background connects are held back: auto-connect of an SSH crew, keyed
 * by its id, and the relay Crew prewarm (hooks/useCrewWarmKeeper.ts), keyed by
 * `relayCrewKey`. A person's own connect (a tab click, the pane's Retry, Connect
 * in Settings, bringing a relay Crew to the front) always goes through and starts
 * the count again, and any connect that warms the crew clears it
 * (lib/connectInstance.ts). Returning to the window is not a person's connect: a
 * refused key does not change on focus, so the wait holds there too. The record
 * is per window and in memory, so a reload starts it again too.
 */

/** The wait after the first failed auto-connect: one poll of the viewport's list. */
export const CONNECT_BACKOFF_BASE_MS = 60_000
/** The longest wait between two auto-connects of one crew. */
export const CONNECT_BACKOFF_MAX_MS = 30 * 60_000

interface Backoff {
  failures: number
  until: number
}

const backoff = new Map<string, Backoff>()

/** The wait after `failures` failed auto-connects in a row. */
export function connectBackoffDelay(failures: number): number {
  return Math.min(CONNECT_BACKOFF_MAX_MS, CONNECT_BACKOFF_BASE_MS * 2 ** Math.max(0, failures - 1))
}

/** How long from `now` until auto-connect may try crew `id` again; 0 when it may now. */
export function autoConnectWaitMs(id: string, now: number): number {
  return Math.max(0, (backoff.get(id)?.until ?? 0) - now)
}

/** Whether auto-connect may try crew `id` at `now`. */
export function autoConnectDue(id: string, now: number): boolean {
  return autoConnectWaitMs(id, now) === 0
}

/** Record a failed auto-connect of crew `id` that ended at `now`. */
export function noteAutoConnectFailed(id: string, now: number): void {
  const failures = (backoff.get(id)?.failures ?? 0) + 1
  backoff.set(id, { failures, until: now + connectBackoffDelay(failures) })
}

/** Forget crew `id`'s failures: a person asked for it, or it connected. */
export function clearConnectBackoff(id: string): void {
  backoff.delete(id)
}

/** Test seam: the record is module state. */
export function __resetConnectBackoffForTests(): void {
  backoff.clear()
}
