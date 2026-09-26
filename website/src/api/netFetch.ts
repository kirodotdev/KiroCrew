/**
 * Network-failure-aware `fetch` for the dashboard transport.
 *
 * ## The gap this closes
 *
 * All of `api/client.ts`'s error machinery — `recordError`, auth recovery, the
 * ApiError shape — lives in `j`/`jNullable`, which only run once a `Response`
 * exists. When `fetch()` itself REJECTS (gateway restart, SSH tunnel drop,
 * machine waking from sleep), there is no Response: the bare
 * `TypeError: Failed to fetch` bypassed the whole chokepoint and landed raw in
 * each call site's `catch (e) { setError(e.message) }`. Two consequences:
 *
 * 1. **No journal entry.** `findReport` had nothing to recover, so the
 *    "ask the agent" hand-off carried six words — no endpoint, no route, no
 *    source — for exactly the failure class where the endpoint matters most.
 * 2. **No retry.** A single dropped packet during an SSH re-key became a
 *    user-visible error card, even though the very next attempt would succeed.
 *
 * ## What it does
 *
 * - Records every network-level failure once, after the final attempt, with
 *   `source: 'network'` and the request path — the fields the old path threw
 *   away. The thrown error keeps the browser's own message (`Failed to fetch`)
 *   so the ~80 `setError(e.message)` call sites render exactly what they
 *   rendered before, and `findReport`'s exact-message match now recovers the
 *   full report behind it.
 * - Retries **idempotent requests only** (GET), twice, with short backoff.
 *   Mutations are never replayed: a POST whose fetch rejected may still have
 *   reached the server (the failure can occur after the request was sent), and
 *   replaying it could double-apply a write.
 *
 * ## What it deliberately does not touch
 *
 * - **HTTP error responses.** A 4xx/5xx resolves the fetch promise; `j` owns
 *   that path, unchanged.
 * - **Aborts.** `AbortError` is a `DOMException`, not a `TypeError`: a caller
 *   cancelling its own request is not a network failure, so it is rethrown
 *   untouched — no retry, no journal entry.
 *
 * The `TypeError` check is the load-bearing discriminator: it is what every
 * engine throws for a network-level fetch failure (Chrome/Firefox
 * `Failed to fetch` / `NetworkError…`, Safari `Load failed`), and it is what
 * keeps this wrapper inert for aborts and for test doubles that reject with a
 * plain `Error`.
 */

import { recordError, requestPath } from '../utils/errorReport'

/** Delay before each retry attempt (attempt 2 waits RETRY_DELAYS_MS[0], …). */
export const RETRY_DELAYS_MS: readonly number[] = [250, 1000]

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

/** True for the rejection `fetch` uses to signal a network-level failure. */
const isNetworkFailure = (err: unknown): err is TypeError => err instanceof TypeError

/**
 * `fetch` with network-failure journaling, and bounded retries when `retry` is
 * set. Callers pass `retry: true` only for idempotent requests.
 *
 * A rejection mid-outage is recorded ONCE (after the last attempt), not per
 * attempt — a burst of per-attempt reports would push real errors out of the
 * bounded journal.
 */
export async function netFetch(
  url: string,
  init: RequestInit | undefined,
  opts: { retry?: boolean } = {},
): Promise<Response> {
  const attempts = opts.retry ? 1 + RETRY_DELAYS_MS.length : 1
  let lastErr: unknown
  let attemptsMade = 0
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (attempt > 0) {
      await sleep(RETRY_DELAYS_MS[attempt - 1])
      // The outage may outlive the caller's interest: an abort during backoff
      // stops retrying and reports the failure already in hand.
      if (init?.signal?.aborted) break
    }
    attemptsMade += 1
    try {
      return await fetch(url, init)
    } catch (err) {
      if (!isNetworkFailure(err)) throw err // abort or a non-network throw: not ours
      lastErr = err
    }
  }
  const failure = lastErr as TypeError
  recordError({
    source: 'network',
    message: failure.message,
    endpoint: requestPath(url),
    detail:
      `fetch rejected before a response after ${attemptsMade} attempt(s) — `
      + 'the gateway is unreachable (gateway down, SSH tunnel drop, or machine offline). '
      + 'No HTTP status exists: the request never completed.',
  })
  throw failure
}
