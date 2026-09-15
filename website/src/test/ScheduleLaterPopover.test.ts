import { describe, it, expect } from 'vitest'
import { defaultFireLocal, toFireTime } from '../components/ScheduleLaterPopover'

describe('toFireTime', () => {
  const now = Date.parse('2026-03-05T12:00:00')

  it('reads the picker as local wall clock and returns epoch seconds', () => {
    // The input carries no zone, so parsing it as local is what makes the time
    // the user sees the time the job fires.
    const secs = toFireTime('2026-03-05T13:30', now)
    expect(secs).toBe(Math.floor(Date.parse('2026-03-05T13:30') / 1000))
  })

  it('refuses a past time, which would fire the moment it was saved', () => {
    expect(toFireTime('2026-03-05T11:59', now)).toBeNull()
  })

  it('refuses the current minute rather than racing the save', () => {
    expect(toFireTime('2026-03-05T12:00', now)).toBeNull()
  })

  it('refuses empty and unparseable values', () => {
    expect(toFireTime('', now)).toBeNull()
    expect(toFireTime('not a date', now)).toBeNull()
  })
})

describe('defaultFireLocal', () => {
  it('opens on a time that is already valid, not on now', () => {
    // A picker defaulting to "now" is invalid the instant it is read, so the
    // confirm button would start disabled for no reason the user can see.
    const now = Date.parse('2026-03-05T12:07:30')
    expect(toFireTime(defaultFireLocal(now), now)).not.toBeNull()
  })

  it('lands on a quarter hour', () => {
    const now = Date.parse('2026-03-05T12:07:30')
    expect(new Date(Date.parse(defaultFireLocal(now))).getMinutes() % 15).toBe(0)
  })

  it('moves to the next quarter when already on one', () => {
    const now = Date.parse('2026-03-05T12:15:00')
    expect(toFireTime(defaultFireLocal(now), now)).not.toBeNull()
  })

  it('is formatted for a datetime-local input', () => {
    expect(defaultFireLocal(Date.parse('2026-03-05T12:07:30'))).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/)
  })
})
