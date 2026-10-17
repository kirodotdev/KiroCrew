import { describe, expect, it } from 'vitest'

import { forecastCreditRunOut, weekdayWeights, type CreditForecastInput } from './creditForecast'

/** Local-time date helper (the forecast works in local calendar days). */
function at(y: number, m: number, d: number, h = 0): Date {
  return new Date(y, m - 1, d, h)
}

function key(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** 28 days of history ending the day before `today`, credits chosen per weekday. */
function history(today: Date, perWeekday: number[]): Record<string, number> {
  const out: Record<string, number> = {}
  for (let i = 1; i <= 28; i++) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - i)
    out[key(d)] = perWeekday[d.getDay()]
  }
  return out
}

// Wed 2026-10-14 at midnight: cycle Oct 1 -> Nov 1, 13 days elapsed.
const NOW = at(2026, 10, 14)
const base: CreditForecastInput = {
  used: 1300,
  limit: 3000,
  resets: '2026-11-01',
  bonusRemaining: 0,
  dailyCredits: undefined,
  now: NOW,
}

describe('weekdayWeights', () => {
  it('is flat with no history, too little history, or no spend', () => {
    expect(weekdayWeights(undefined, NOW)).toEqual([1, 1, 1, 1, 1, 1, 1])
    const short: Record<string, number> = {}
    for (let i = 1; i <= 13; i++) short[key(at(2026, 10, 14 - i))] = i
    expect(weekdayWeights(short, NOW)).toEqual([1, 1, 1, 1, 1, 1, 1])
    expect(weekdayWeights(history(NOW, [0, 0, 0, 0, 0, 0, 0]), NOW)).toEqual([1, 1, 1, 1, 1, 1, 1])
  })

  it('weights heavy weekdays up and the weekend down, mean 1', () => {
    // Sun..Sat: weekend idle, Tuesday heavy.
    const w = weekdayWeights(history(NOW, [0, 100, 300, 100, 100, 100, 0]), NOW)
    expect(w[0]).toBe(0)
    expect(w[6]).toBe(0)
    expect(w[2]).toBeCloseTo(300 / (700 / 7))
    expect(w.reduce((a, b) => a + b, 0) / 7).toBeCloseTo(1)
  })

  it('counts days before the first row as unknown, not zero', () => {
    // 14 days of history only: a missing earlier fortnight must not halve the weights.
    const d: Record<string, number> = {}
    for (let i = 1; i <= 14; i++) d[key(at(2026, 10, 14 - i))] = 10
    expect(weekdayWeights(d, NOW)).toEqual([1, 1, 1, 1, 1, 1, 1])
  })
})

describe('forecastCreditRunOut', () => {
  it('projects a run-out date before the reset at a flat pace', () => {
    // 100/day pace, 1700 left -> lasts 17 days from Oct 14 -> runs out Oct 30.
    const f = forecastCreditRunOut(base)
    expect(f).toEqual({ kind: 'runs-out', date: at(2026, 10, 30) })
  })

  it('says the plan lasts when the pace gets it to the reset', () => {
    expect(forecastCreditRunOut({ ...base, limit: 5000 })).toEqual({ kind: 'lasts' })
  })

  it('moves the date when the weekday shape says the remaining days are lighter', () => {
    // Same pace, but all spend lands on Tuesdays: only two Tuesdays remain
    // before the reset, so the plan lasts past where the flat projection ran out.
    const weekdaysOnly = history(NOW, [0, 0, 700, 0, 0, 0, 0])
    const flat = forecastCreditRunOut(base)
    const shaped = forecastCreditRunOut({ ...base, dailyCredits: weekdaysOnly })
    expect(flat).toEqual({ kind: 'runs-out', date: at(2026, 10, 30) })
    expect(shaped).toEqual({ kind: 'lasts' })
  })

  it('counts only the rest of today', () => {
    // Late evening: today contributes almost nothing, so the date slips a day.
    // `used` keeps the pace at exactly 100/day over the 13 23/24 days elapsed.
    const used = 100 * (13 + 23 / 24)
    const evening = forecastCreditRunOut({ ...base, used, limit: used + 1700, now: at(2026, 10, 14, 23) })
    expect(evening).toEqual({ kind: 'runs-out', date: at(2026, 10, 31) })
  })

  it('says nothing when there is nothing honest to project', () => {
    expect(forecastCreditRunOut({ ...base, resets: undefined })).toBeNull()
    expect(forecastCreditRunOut({ ...base, resets: 'soon' })).toBeNull()
    expect(forecastCreditRunOut({ ...base, used: 3000 })).toBeNull() // plan used up
    expect(forecastCreditRunOut({ ...base, bonusRemaining: 50 })).toBeNull()
    expect(forecastCreditRunOut({ ...base, now: at(2026, 10, 1, 12) })).toBeNull() // < 1 day in
    expect(forecastCreditRunOut({ ...base, resets: '2026-10-14' })).toBeNull() // reset passed
  })

  it('lasts when nothing has been used yet', () => {
    expect(forecastCreditRunOut({ ...base, used: 0 })).toEqual({ kind: 'lasts' })
  })

  it('clamps a 31st reset whose previous month is shorter', () => {
    // Reset Mar 31 -> cycle start Feb 28 (2027 is not a leap year), not Mar 3.
    const f = forecastCreditRunOut({
      ...base, used: 100, limit: 3000, resets: '2027-03-31', now: at(2027, 3, 2),
    })
    // 100 used over 2 days = 50/day; 2900 left lasts past Mar 31.
    expect(f).toEqual({ kind: 'lasts' })
  })
})
