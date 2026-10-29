import { describe, it, expect } from 'vitest'
import {
  KIRO_USAGE_STEADY_POLL_MS,
  KIRO_USAGE_WARMING_MAX_READS,
  KIRO_USAGE_WARMING_POLL_MS,
  kiroUsageRefetchInterval,
  parseKiroUsagePayload,
  type KiroCreditUsage,
} from '../api/kiroUsage'

/**
 * How fast the credit pill re-polls `/api/sessions/usage`.
 *
 * The gateway serves the reading from a cache the GET warms in the background,
 * so a reopen sees an empty cache (parsed to `null`) and, before kiro-cli
 * readiness is verified, a 503 the query holds as an error with no data. The
 * helper must poll fast in both of those unsettled states so the pill resolves
 * in seconds, and drop to the steady cadence once a reading or a terminal
 * sentinel lands, so a resolved pill is not polled every few seconds forever.
 *
 * Tested as a pure function: the alternative is two vite builds and a browser
 * with a faked clock to watch an interval tick, which is why the cadence logic
 * is a standalone helper rather than an inline arrow in the query options.
 */
describe('kiroUsageRefetchInterval', () => {
  const reading: KiroCreditUsage = {
    used: 100,
    limit: 10_000,
    overage: 0,
    bonusCredits: [],
    stale: false,
  }

  it('polls fast while the cache is warming (null reading)', () => {
    expect(kiroUsageRefetchInterval(null, false, 0)).toBe(KIRO_USAGE_WARMING_POLL_MS)
  })

  it('polls fast while the read errors, even though errored data is also null', () => {
    // The pre-readiness 503: the query holds no data and an error. Both the
    // null branch and the error branch resolve to the fast cadence, and the
    // error flag is asserted on its own so a future change to the data branch
    // cannot silently stop accelerating the boot 503.
    expect(kiroUsageRefetchInterval(null, true, 0)).toBe(KIRO_USAGE_WARMING_POLL_MS)
  })

  it('drops to the steady cadence once a real reading lands', () => {
    expect(kiroUsageRefetchInterval(reading, false, 0)).toBe(KIRO_USAGE_STEADY_POLL_MS)
  })

  it('drops to the steady cadence on each terminal sentinel', () => {
    for (const terminal of ['none', 'api-key', 'signin-required'] as const) {
      expect(kiroUsageRefetchInterval(terminal, false, 0)).toBe(KIRO_USAGE_STEADY_POLL_MS)
    }
  })

  it('keeps polling fast just below the cap', () => {
    const below = KIRO_USAGE_WARMING_MAX_READS - 1
    expect(kiroUsageRefetchInterval(null, true, below)).toBe(KIRO_USAGE_WARMING_POLL_MS)
    expect(kiroUsageRefetchInterval(null, false, below)).toBe(KIRO_USAGE_WARMING_POLL_MS)
  })

  it('ends a persistent error at the steady cadence', () => {
    // A signed-out kiro-cli or a down gateway keeps the query in error forever:
    // past the cap it must poll no faster than before this change.
    expect(kiroUsageRefetchInterval(null, true, KIRO_USAGE_WARMING_MAX_READS)).toBe(KIRO_USAGE_STEADY_POLL_MS)
    expect(kiroUsageRefetchInterval(null, true, 500)).toBe(KIRO_USAGE_STEADY_POLL_MS)
  })

  it('ends a cache that never warms at the steady cadence', () => {
    expect(kiroUsageRefetchInterval(null, false, KIRO_USAGE_WARMING_MAX_READS)).toBe(KIRO_USAGE_STEADY_POLL_MS)
  })

  it('does not speed up a pill that had a reading and then starts failing', () => {
    // Errors after a reading keep the old data; many reads have already settled.
    expect(kiroUsageRefetchInterval(reading, true, 40)).toBe(KIRO_USAGE_STEADY_POLL_MS)
  })
})

/**
 * The parser distinctions the cadence helper rests on: an empty envelope is the
 * warming `null` that must poll fast, while an explicit no-reading answer is a
 * terminal sentinel that must settle to the steady cadence.
 */
describe('parseKiroUsagePayload warming vs settled', () => {
  it('maps an empty envelope to the warming null', () => {
    expect(parseKiroUsagePayload({ usage: {} })).toBeNull()
    expect(parseKiroUsagePayload(undefined)).toBeNull()
  })

  it('maps an explicit unavailable answer to a terminal sentinel', () => {
    expect(parseKiroUsagePayload({ usage: { available: false } })).toBe('none')
    expect(parseKiroUsagePayload({ usage: { available: false, reason: 'api_key_auth' } })).toBe('api-key')
    expect(parseKiroUsagePayload({ usage: { available: false, reason: 'signin_required' } })).toBe('signin-required')
  })
})
