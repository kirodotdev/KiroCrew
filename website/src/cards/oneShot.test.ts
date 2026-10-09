/**
 * One-shot schedule cards: what marks a card as running once, and how its one
 * run time reads in the schedule's own zone.
 */
import { describe, it, expect } from 'vitest'
import { fmtOneShot, isOneShot, oneShotRunAt, oneShotWhen, wallTimeIn } from './oneShot'
import { cardChanges } from './cardRegistry'

const NOW = Date.parse('2026-10-03T15:05:00Z') // Sat Oct 3, 8:05 AM PDT
const LA = 'America/Los_Angeles'

describe('isOneShot', () => {
  const sched = (params: Record<string, unknown>, once?: boolean) => ({ kind: 'schedule.create', params, once })

  it('reads the gateway flag on the card or in its params', () => {
    expect(isOneShot(sched({}, true))).toBe(true)
    expect(isOneShot(sched({ once: true }))).toBe(true)
  })

  it('reads an `at` with no cron as one-shot, and a cron as recurring', () => {
    expect(isOneShot(sched({ at: '2026-10-04T09:00' }))).toBe(true)
    expect(isOneShot(sched({ at: 1_791_000_000 }))).toBe(true)
    expect(isOneShot(sched({ at: '2026-10-04T09:00', cron_expr: '0 9 * * *' }))).toBe(false)
    expect(isOneShot(sched({ cron_expr: '0 9 * * *' }))).toBe(false)
    expect(isOneShot(sched({ at: '  ' }))).toBe(false)
  })

  it('never marks a non-schedule kind', () => {
    expect(isOneShot({ kind: 'crewmate.create', params: { at: 'x', once: true }, once: true })).toBe(false)
  })
})

describe('fmtOneShot', () => {
  it('reads tomorrow relatively, in the schedule zone, with the zone named', () => {
    expect(fmtOneShot(Date.parse('2026-10-04T16:00:00Z'), LA, { now: NOW })).toBe('Tomorrow, Oct 4 · 9:00 AM PDT')
  })

  it('reads today relatively, and lowercases inside a sentence', () => {
    expect(fmtOneShot(Date.parse('2026-10-03T23:30:00Z'), LA, { now: NOW, capitalize: false })).toBe('today, Oct 3 · 4:30 PM PDT')
  })

  it('names the weekday for a later day, and the year only when it differs', () => {
    expect(fmtOneShot(Date.parse('2026-10-10T16:00:00Z'), LA, { now: NOW })).toBe('Sat, Oct 10 · 9:00 AM PDT')
    expect(fmtOneShot(Date.parse('2027-01-05T17:00:00Z'), LA, { now: NOW })).toBe('Tue, Jan 5, 2027 · 9:00 AM PST')
  })

  it('decides "tomorrow" by the schedule zone, not the reader\'s', () => {
    // 06:00 UTC on Oct 4 is still Oct 3 in Los Angeles.
    expect(fmtOneShot(Date.parse('2026-10-04T06:00:00Z'), LA, { now: NOW })).toBe('Today, Oct 3 · 11:00 PM PDT')
  })

  it('gives null for a value that is not a time', () => {
    expect(fmtOneShot('tomorrow 9am', LA, { now: NOW })).toBeNull()
    expect(fmtOneShot(undefined, LA)).toBeNull()
  })
})

describe('oneShotWhen', () => {
  it('prefers the resolved next run over the raw `at`', () => {
    const card = { kind: 'schedule.create', params: { at: 'tomorrow 9am', timezone: LA }, next_run_at: Date.parse('2026-10-04T16:00:00Z') / 1000 }
    expect(oneShotWhen(card, { now: NOW })).toBe('Tomorrow, Oct 4 · 9:00 AM PDT')
  })
})

// The `at` param is a wall time with no offset, read by the gateway in the
// card's zone. The test run pins TZ=UTC, so a parse in the reader's zone shows
// 2:00 AM PDT for a 9:00 AM Los Angeles reminder -- the bug this pins.
describe('a bare wall time is read in the card zone, not the reader\'s', () => {
  it('wallTimeIn names the instant the gateway resolves', () => {
    expect(wallTimeIn('2026-10-04T09:00', LA)).toBe(Date.parse('2026-10-04T16:00:00Z'))
    expect(wallTimeIn('2027-01-05T09:00', LA)).toBe(Date.parse('2027-01-05T17:00:00Z'))
    expect(wallTimeIn('2026-10-04T09:00', 'Asia/Tokyo')).toBe(Date.parse('2026-10-04T00:00:00Z'))
    expect(wallTimeIn('tomorrow 9am', LA)).toBeNull()
    expect(wallTimeIn('2026-10-04T09:00', 'Not/AZone')).toBeNull()
  })

  it('fmtOneShot shows the wall time itself', () => {
    expect(fmtOneShot('2026-10-04T09:00', LA, { now: NOW })).toBe('Tomorrow, Oct 4 · 9:00 AM PDT')
  })

  it('the card\'s Runs once row and its run time agree with the stored time', () => {
    const card = {
      kind: 'schedule.create', once: true, timezone: LA,
      params: { name: 'Review open PRs', message: 'Review open PRs', at: '2026-10-04T09:00', timezone: LA },
      changes: [{ field: 'at', label: 'When', after: '2026-10-04T09:00' }],
    }
    const row = cardChanges(card).find(c => c.label === 'Runs once')
    expect(String(row?.after)).toContain('9:00 AM PDT')
    expect(oneShotRunAt(card)).toBe(Date.parse('2026-10-04T16:00:00Z'))
  })
})

