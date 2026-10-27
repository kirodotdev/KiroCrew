import { describe, it, expect, vi, afterEach } from 'vitest'
import { calcCompanionDays } from '../apps/crew-companion/memories'

/** Run `fn` with the process timezone set to `tz`, restoring it afterwards.
 *  A real zone, so `Date` itself lands on that zone's local days. */
function inZone<T>(tz: string, fn: () => T): T {
  const prev = process.env.TZ
  process.env.TZ = tz
  try {
    return fn()
  } finally {
    process.env.TZ = prev
  }
}

afterEach(() => { vi.useRealTimers() })

// vitest pins TZ=UTC, so the Z instants below are local days unless a test
// moves the zone with inZone().
describe('calcCompanionDays: "N days together" counts calendar days', () => {
  it('counts the second calendar day as 2 when under 24h have elapsed', () => {
    // First launch Mon 23:00, looked at Tue 01:00: 2h elapsed, second day.
    vi.useFakeTimers({ now: new Date('2026-01-06T01:00:00Z') })
    expect(calcCompanionDays('2026-01-05T23:00:00Z')).toBe(2)
  })

  it('keeps the plain cases: same day is 1, same hour a week later is 8', () => {
    vi.useFakeTimers({ now: new Date('2026-01-05T10:00:00Z') })
    expect(calcCompanionDays('2026-01-05T08:00:00Z')).toBe(1)
    expect(calcCompanionDays('2025-12-29T10:00:00Z')).toBe(8)
  })

  it('counts calendar days across a 23h spring-forward day', () => {
    // New York, Sun Mar 8 10:00 EDT -> Mon Mar 9 09:00 EDT: 23h elapsed and a
    // 23h midnight-to-midnight gap, yet Monday is the second day.
    vi.useFakeTimers({ now: new Date('2026-03-09T13:00:00Z') })
    expect(inZone('America/New_York', () => calcCompanionDays('2026-03-08T14:00:00Z'))).toBe(2)
  })

  it('counts calendar days across a 25h fall-back day', () => {
    // New York, Sat Oct 31 00:30 EDT -> Sun Nov 1 23:30 EST: 48h elapsed over
    // a 25h Sunday, yet Sunday is only the second day.
    vi.useFakeTimers({ now: new Date('2026-11-02T04:30:00Z') })
    expect(inZone('America/New_York', () => calcCompanionDays('2026-10-31T04:30:00Z'))).toBe(2)
  })

  it('still answers 0 for an unparseable firstLaunch', () => {
    expect(calcCompanionDays('not-a-date')).toBe(0)
  })
})
