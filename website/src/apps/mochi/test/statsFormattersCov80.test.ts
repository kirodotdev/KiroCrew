/**
 * statsFormatters — the duration/date arithmetic the panel and the dashboard
 * SHARE, so a wrong branch here is wrong on both surfaces at once.
 *
 * The three-way duration split and the invalid-date guard are the interesting
 * parts: each boundary (59s / 60s / 3600s, and an hour with no leftover
 * minutes) picks a different unit, and `calcCompanionDays` must answer 0 rather
 * than NaN for a corrupted `firstLaunch`, which is the value a stats file that
 * lost the field ends up holding.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'

import {
  calcCompanionDays,
  formatCompanionTime,
  formatDate,
  formatThinkingTime,
  getTopMoods,
  shouldShowStat,
} from '../src/shared/statsFormatters'

afterEach(() => {
  vi.useRealTimers()
})

describe('formatThinkingTime', () => {
  it('stays in seconds below a minute', () => {
    // Intl narrow units, not hand-built copy: the assertion is that the SECONDS
    // branch was taken, so the digits and the unit both have to be there.
    const out = formatThinkingTime(45)
    expect(out).toContain('45')
    expect(out).toMatch(/45\s*s/)
  })

  it('floors to whole minutes between a minute and an hour', () => {
    expect(formatThinkingTime(3599)).toMatch(/59\s*m/)
    expect(formatThinkingTime(90)).toMatch(/1\s*m/)
  })

  it('shows hours and the leftover minutes past an hour', () => {
    const out = formatThinkingTime(3600 + 15 * 60)
    expect(out).toMatch(/1\s*h/)
    expect(out).toMatch(/15\s*m/)
  })

  it('omits the minute part on a whole number of hours', () => {
    const out = formatThinkingTime(7200)
    expect(out).toMatch(/2\s*h/)
    expect(out).not.toMatch(/\d+\s*m/)
  })
})

describe('formatCompanionTime', () => {
  it('never reports zero minutes — a launch counts as one', () => {
    expect(formatCompanionTime(0)).toMatch(/1\s*m/)
    expect(formatCompanionTime(20)).toMatch(/1\s*m/)
  })

  it('reports whole minutes under an hour', () => {
    expect(formatCompanionTime(600)).toMatch(/10\s*m/)
  })

  it('reports hours, with the minutes only when there are some', () => {
    expect(formatCompanionTime(3600)).toMatch(/1\s*h/)
    expect(formatCompanionTime(3600)).not.toMatch(/\d+\s*m/)
    const mixed = formatCompanionTime(3600 * 3 + 60 * 5)
    expect(mixed).toMatch(/3\s*h/)
    expect(mixed).toMatch(/5\s*m/)
  })
})

describe('calcCompanionDays', () => {
  it('counts the first day as day one', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-10T12:00:00Z'))
    expect(calcCompanionDays('2026-08-10T09:00:00Z')).toBe(1)
    expect(calcCompanionDays('2026-08-08T12:00:00Z')).toBe(3)
  })

  it('answers 0 for an unparseable firstLaunch instead of NaN', () => {
    expect(calcCompanionDays('not-a-date')).toBe(0)
  })

  it('never goes negative for a firstLaunch in the future', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-10T12:00:00Z'))
    expect(calcCompanionDays('2027-01-01T00:00:00Z')).toBe(1)
  })
})

describe('getTopMoods', () => {
  it('sorts by count, caps at the limit, and percentages over the kept total', () => {
    const rows = getTopMoods({ curious: 6, scared: 2, happy: 12 }, 2)
    expect(rows.map((r) => r.mood)).toEqual(['happy', 'curious'])
    // 12 + 6 + 2 = 20 — the percentage is over EVERY non-zero mood, not just
    // the two that survived the slice.
    expect(rows[0].percent).toBe(60)
    expect(rows[1].percent).toBe(30)
  })

  it('drops zero-count moods and returns nothing when all are zero', () => {
    expect(getTopMoods({ happy: 0, sad: 0 })).toEqual([])
    expect(getTopMoods({}).length).toBe(0)
    expect(getTopMoods({ happy: 1, sad: 0 }).map((r) => r.mood)).toEqual(['happy'])
  })
})

describe('shouldShowStat', () => {
  it('hides a zero number and an empty string, keeps everything else', () => {
    expect(shouldShowStat(0)).toBe(false)
    expect(shouldShowStat(-1)).toBe(false)
    expect(shouldShowStat(1)).toBe(true)
    expect(shouldShowStat('')).toBe(false)
    expect(shouldShowStat('02:10')).toBe(true)
  })
})

describe('formatDate', () => {
  it('passes an unparseable value straight through rather than inventing a date', () => {
    expect(formatDate('')).toBe('')
    expect(formatDate('2026-08')).toBe('2026-08')
    expect(formatDate('20xx-aa-bb')).toBe('20xx-aa-bb')
  })

  it('renders month and day, ordered by the locale', () => {
    // en → 8/13; the point is that both components survive and the year does not.
    const out = formatDate('2026-08-13')
    expect(out).toContain('8')
    expect(out).toContain('13')
    expect(out).not.toContain('2026')
  })
})
