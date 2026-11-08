import { describe, it, expect } from 'vitest'
import { nextFireAt, scheduleOf, type Schedule } from '../apps/crew-companion/reminderSchedule'

/** Run `fn` with the process timezone set to `tz`, restoring it afterwards. */
function inZone<T>(tz: string, fn: () => T): T {
  const prev = process.env.TZ
  process.env.TZ = tz
  try {
    return fn()
  } finally {
    process.env.TZ = prev
  }
}

// vitest pins TZ=UTC, so these Z instants are local times. 2026-11-04 is a Wednesday.
const now = new Date('2026-11-04T10:32:00Z')
const at = (s: Partial<Schedule>, day?: Date) => nextFireAt({ repeat: 'once', hour: 0, minute: 0, weekday: 1, ...s }, now, day)

describe('nextFireAt', () => {
  it('aligns an hourly reminder to the clock: :00 from 10:32 is 11:00, not 11:32', () => {
    expect(at({ repeat: 'hourly', minute: 0 })).toBe('2026-11-04T11:00:00.000Z')
    expect(at({ repeat: 'hourly', minute: 45 })).toBe('2026-11-04T10:45:00.000Z')
  })

  it('puts a daily or one-time 07:30 on the next 07:30', () => {
    expect(at({ repeat: 'daily', hour: 7, minute: 30 })).toBe('2026-11-05T07:30:00.000Z')
    expect(at({ repeat: 'once', hour: 18, minute: 0 })).toBe('2026-11-04T18:00:00.000Z')
  })

  it('lands strictly after now, so a time equal to now moves on', () => {
    expect(at({ repeat: 'daily', hour: 10, minute: 32 })).toBe('2026-11-05T10:32:00.000Z')
    expect(at({ repeat: 'hourly', minute: 32 })).toBe('2026-11-04T11:32:00.000Z')
  })

  it('keeps a one-time reminder on its own date unless that date has passed', () => {
    expect(at({ repeat: 'once', hour: 10, minute: 0 }, new Date('2026-11-13T09:00:00Z'))).toBe('2026-11-13T10:00:00.000Z')
    expect(at({ repeat: 'once', hour: 10, minute: 0 }, new Date('2026-11-03T09:00:00Z'))).toBe('2026-11-05T10:00:00.000Z')
  })

  it('puts a weekly reminder on the chosen weekday', () => {
    expect(at({ repeat: 'weekly', weekday: 1, hour: 9, minute: 0 })).toBe('2026-11-09T09:00:00.000Z')
    expect(at({ repeat: 'weekly', weekday: 3, hour: 9, minute: 0 })).toBe('2026-11-11T09:00:00.000Z')
    expect(at({ repeat: 'weekly', weekday: 3, hour: 12, minute: 0 })).toBe('2026-11-04T12:00:00.000Z')
  })

  it('puts the first fire on the local clock time across a daylight-saving change', () => {
    // New York, Sat Mar 7 2026 20:00 EST: the next 07:30 is Sunday 07:30 EDT.
    const fire = inZone('America/New_York', () =>
      nextFireAt({ repeat: 'daily', hour: 7, minute: 30, weekday: 1 }, new Date('2026-03-08T01:00:00Z')))
    expect(fire).toBe('2026-03-08T11:30:00.000Z')
  })

  it('does not carry a spring-forward gap’s shifted time to the next day', () => {
    // New York, Sun Mar 8 2026 04:00 EDT: 02:30 does not exist today, so the next 02:30 is Monday 02:30 EDT.
    const fire = inZone('America/New_York', () =>
      nextFireAt({ repeat: 'daily', hour: 2, minute: 30, weekday: 1 }, new Date('2026-03-08T08:00:00Z')))
    expect(fire).toBe('2026-03-09T06:30:00.000Z')
  })

  it('takes the second pass of a fall-back hour while it is still ahead', () => {
    // New York, Sun Nov 1 2026 01:15 EST: 01:30 EDT has passed, 01:30 EST is fifteen minutes away.
    const fire = inZone('America/New_York', () =>
      nextFireAt({ repeat: 'daily', hour: 1, minute: 30, weekday: 1 }, new Date('2026-11-01T06:15:00Z')))
    expect(fire).toBe('2026-11-01T06:30:00.000Z')
  })

  it('keeps a custom-interval reminder on its own date', () => {
    expect(at({ repeat: 'custom', hour: 10, minute: 0 }, new Date('2026-11-13T09:00:00Z'))).toBe('2026-11-13T10:00:00.000Z')
  })

  it('counts the repeated hour of a fall-back change as an hour', () => {
    // New York, Sun Nov 1 2026: from 01:32 EDT the next :00 is 01:00 EST, and from 01:32 EST it is 02:00 EST.
    const next = (iso: string) => inZone('America/New_York', () =>
      nextFireAt({ repeat: 'hourly', hour: 0, minute: 0, weekday: 1 }, new Date(iso)))
    expect(next('2026-11-01T05:32:00Z')).toBe('2026-11-01T06:00:00.000Z')
    expect(next('2026-11-01T06:32:00Z')).toBe('2026-11-01T07:00:00.000Z')
  })
})

describe('scheduleOf', () => {
  const base = { id: 'r', text: 'draw', fireAt: '2026-11-09T07:30:00.000Z' }

  it('opens on the reminder’s own repeat, time and weekday', () => {
    expect(scheduleOf({ ...base, recurrence: { everyMinutes: 1440 } }))
      .toEqual({ repeat: 'daily', hour: 7, minute: 30, weekday: 1 })
    expect(scheduleOf({ ...base, recurrence: { everyMinutes: 60 } }).repeat).toBe('hourly')
    expect(scheduleOf({ ...base, recurrence: { everyMinutes: 10080 } }).repeat).toBe('weekly')
    expect(scheduleOf({ ...base, recurrence: null }).repeat).toBe('once')
  })

  it('opens on its own interval, not Once, for an interval no preset matches', () => {
    expect(scheduleOf({ ...base, recurrence: { everyMinutes: 120 } }).repeat).toBe('custom')
  })
})
