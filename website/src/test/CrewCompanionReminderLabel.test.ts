import { describe, it, expect } from 'vitest'
import { labelFor } from '../apps/crew-companion/reminders'

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

// vitest pins TZ=UTC, so the Z instants below are local days unless a test
// moves the zone with inZone().
describe('labelFor beyond-today day count', () => {
  it('calls a reminder late tomorrow "tomorrow", not "2 days from now"', () => {
    // Mon 09:00 -> Tue 23:30 is 38.5h, but only one calendar day away.
    const { absLabel, relLabel } = labelFor('2026-01-06T23:30:00Z', new Date('2026-01-05T09:00:00Z'))
    expect(relLabel).toBe('tomorrow')
    expect(absLabel).not.toContain('Tue')
  })

  it('does not call a reminder two calendar days out "tomorrow"', () => {
    // Mon 23:00 -> Wed 08:00 is 33h, but two calendar days away.
    const { absLabel, relLabel } = labelFor('2026-01-07T08:00:00Z', new Date('2026-01-05T23:00:00Z'))
    expect(relLabel).toBe('2 days from now')
    expect(absLabel).toContain('Wed')
  })

  it('keeps the plain cases: early tomorrow and two days out at the same hour', () => {
    const now = new Date('2026-01-05T09:00:00Z')
    expect(labelFor('2026-01-06T08:00:00Z', now).relLabel).toBe('tomorrow')
    expect(labelFor('2026-01-07T10:00:00Z', now).relLabel).toBe('2 days from now')
  })

  it('counts calendar days across a 23h spring-forward day', () => {
    // New York, Sat Mar 7 22:00 EST -> Mon Mar 9 07:00 EDT: 32h elapsed,
    // and Sunday is only 23h long, yet the reminder is two days out.
    const { absLabel, relLabel } = inZone('America/New_York', () =>
      labelFor('2026-03-09T11:00:00Z', new Date('2026-03-08T03:00:00Z')))
    expect(relLabel).toBe('2 days from now')
    expect(absLabel).toContain('Mon')
  })

  it('counts calendar days across a 25h fall-back day', () => {
    // New York, Sat Oct 31 08:00 EDT -> Sun Nov 1 23:30 EST: 40.5h elapsed
    // over a 25h Sunday, yet the reminder fires tomorrow.
    const { relLabel } = inZone('America/New_York', () =>
      labelFor('2026-11-02T04:30:00Z', new Date('2026-10-31T12:00:00Z')))
    expect(relLabel).toBe('tomorrow')
  })
})
