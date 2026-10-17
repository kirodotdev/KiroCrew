/**
 * The retry ladder stops only where retrying cannot possibly work.
 *
 * The first version of this stopped on `authRequired`, which the gateway's OWN
 * `X-Auth-Required` 403 also sets. That silently cancelled the recovery a slept
 * laptop depends on: `attemptSilentRefresh` re-mints the cookie, but a successful
 * refresh invalidates only `['auth-me']` and the default `staleTime` is `Infinity`,
 * so the one retry ~1s later is the ONLY thing that refetches the query that 403'd.
 * The proxy case has no such recovery, because the request never reached the gateway.
 */
import { describe, it, expect } from 'vitest'
import { ApiError } from '../api/apiError'
import { refetchWhileFailed, retryPolicy, retryThroughRestart } from '../api/queryClient'

/** The gateway's own lapse: `authRequired`, but NOT a proxy challenge. */
const gatewayLapse = () => new ApiError(403, 'session expired', '', true, false)
/** An interposed proxy's sign-in page: both flags, since no retry reaches through. */
const proxyChallenge = () => new ApiError(403, 'proxy expired', '', true, true)
const throttled = () => new ApiError(429, 'slow down', '', false, false)

describe('retryPolicy', () => {
  it('keeps the retry a gateway lapse recovers through', () => {
    // The regression guard. Without it the silent refresh lands and nothing refetches.
    expect(retryPolicy(0, gatewayLapse())).toBe(true)
    expect(retryPolicy(1, gatewayLapse())).toBe(false)
  })

  it('does not retry an interposed proxy that never reached the gateway', () => {
    for (const attempt of [0, 1, 2, 3]) {
      expect(retryPolicy(attempt, proxyChallenge()), `attempt ${attempt}`).toBe(false)
    }
  })

  it('still climbs the ladder for a throttle', () => {
    // Without this the assertion above would pass on a policy that never retries.
    expect(retryPolicy(0, throttled())).toBe(true)
    expect(retryPolicy(3, throttled())).toBe(true)
    expect(retryPolicy(4, throttled())).toBe(false)
  })

  it('still allows one retry for an ordinary failure', () => {
    const boom = new ApiError(500, 'boom', 'boom', false, false)
    expect(retryPolicy(0, boom)).toBe(true)
    expect(retryPolicy(1, boom)).toBe(false)
  })
})

describe('retryThroughRestart', () => {
  it('rides out a dropped connection for three retries, where retryPolicy gives one', () => {
    const drop = new TypeError('Failed to fetch')
    expect(retryPolicy(1, drop)).toBe(false)
    for (const attempt of [0, 1, 2]) expect(retryThroughRestart(attempt, drop), `attempt ${attempt}`).toBe(true)
    expect(retryThroughRestart(3, drop)).toBe(false)
  })

  it('rides out a proxy 502/504 and the gateway\'s own 503 the same way', () => {
    for (const status of [502, 503, 504]) {
      const err = new ApiError(status, 'bad gateway', '', false, false)
      expect(retryPolicy(1, err), `retryPolicy ${status}`).toBe(false)
      expect(retryThroughRestart(2, err), `${status}`).toBe(true)
      expect(retryThroughRestart(3, err), `${status}`).toBe(false)
    }
  })

  it('is retryPolicy for every answered failure and for our own deadline', () => {
    const boom = new ApiError(500, 'boom', 'boom', false, false)
    expect(retryThroughRestart(0, boom)).toBe(true)
    expect(retryThroughRestart(1, boom)).toBe(false)
    expect(retryThroughRestart(0, proxyChallenge())).toBe(false)
    expect(retryThroughRestart(3, throttled())).toBe(true)
    const deadline = Object.assign(new Error('deadline'), { name: 'TimeoutError' })
    expect(retryThroughRestart(0, deadline)).toBe(false)
  })
})

describe('refetchWhileFailed', () => {
  it('re-reads every 30s only while the read is failed', () => {
    expect(refetchWhileFailed({ state: { status: 'error' } })).toBe(30_000)
    expect(refetchWhileFailed({ state: { status: 'success' } })).toBe(false)
    expect(refetchWhileFailed({ state: { status: 'pending' } })).toBe(false)
  })
})
