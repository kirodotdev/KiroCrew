/**
 * `last_activity_ts` ordering.
 *
 * Every case here is chosen so STRING order and INSTANT order DISAGREE — a case
 * where they agree cannot fail, and the whole defect is that the two were
 * conflated. The disagreement is also host-timezone independent wherever it is
 * asserted: both values carry an offset, so `new Date()` resolves them to the
 * same two instants on any runner (the vitest setup pins no `TZ`).
 */
import { describe, it, expect } from 'vitest'

import { slotActivityMs, byRecentActivity } from './slotRecency'

/** Same instant pair throughout: A = 01:00Z, B = 02:30Z, so B is LATER. */
const A = '2026-09-14T09:00:00+08:00'
const B = '2026-09-14T02:30:00+00:00'

describe('slotRecency', () => {
  it('the fixtures really do disagree, or nothing below could fail', () => {
    // Guard the guard: text order says A is the later one, instants say B is.
    expect(A.localeCompare(B)).toBeGreaterThan(0)
    expect(A > B).toBe(true)
    expect(Date.parse(A)).toBeLessThan(Date.parse(B))
  })

  it('orders two aware timestamps written under different offsets by instant', () => {
    const sorted = [{ last_activity_ts: A }, { last_activity_ts: B }].sort(byRecentActivity)
    expect(sorted.map(s => s.last_activity_ts)).toEqual([B, A])
  })

  it('is stable regardless of which order the inputs arrive in', () => {
    const sorted = [{ last_activity_ts: B }, { last_activity_ts: A }].sort(byRecentActivity)
    expect(sorted.map(s => s.last_activity_ts)).toEqual([B, A])
  })

  it('sorts a missing or unparseable stamp last, as the old `|| \'\'` fallback did', () => {
    expect(slotActivityMs({ last_activity_ts: undefined })).toBe(0)
    expect(slotActivityMs({ last_activity_ts: '' })).toBe(0)
    expect(slotActivityMs({ last_activity_ts: 'not a timestamp' })).toBe(0)
    const sorted = [
      { last_activity_ts: undefined },
      { last_activity_ts: B },
      { last_activity_ts: 'not a timestamp' },
    ].sort(byRecentActivity)
    expect(sorted[0].last_activity_ts).toBe(B)
  })

  it('reads a naive stamp as local time, the way the writer meant it', () => {
    // The backend records naive rows from older builds and interprets them as
    // local (`history.transcript_sort_key`: "Naive values are interpreted as
    // local time, matching the writer that produced them"). Asserting the
    // ORDERING of a naive value against an aware one would be host-timezone
    // dependent, so this asserts only the parse rule, which is not.
    const naive = '2026-09-14T10:00:00'
    expect(slotActivityMs({ last_activity_ts: naive }))
      .toBe(new Date(2026, 8, 14, 10, 0, 0).getTime())
  })
})
