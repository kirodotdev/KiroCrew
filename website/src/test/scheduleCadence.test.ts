import { describe, it, expect, afterEach } from 'vitest'

import { i18next } from '../i18n/all'
import { SCHEDULE_PRESETS } from '../utils/schedulePresets'
import { formatCadence } from '../utils/scheduleCadence'

// The cadence label used to be a stored English string per preset, which froze an
// en-US clock ("6:00am") and weekday ("Mondays") into every locale. It is now
// derived from the schedule through the i18n formatting seam. These tests pin the
// derivation for each schedule shape and, critically, that EVERY shipped preset
// still produces a label -- a preset whose schedule this helper cannot model would
// otherwise silently render an empty cadence on its card.

describe('formatCadence', () => {
  it('renders an interval schedule with a locale-formatted unit', () => {
    const out = formatCadence({ name: 'x', message: 'y', schedMode: 'interval', intVal: 30, intUnit: 'minutes' })
    expect(out).toMatch(/30/)
    expect(out.trim()).not.toBe('')
  })

  it('renders a daily cron with a formatted clock time', () => {
    const out = formatCadence({ name: 'x', message: 'y', schedMode: 'cron', cronExpr: '0 8 * * *' })
    expect(out.trim()).not.toBe('')
    expect(out).not.toContain('0 8 * * *')
  })

  it('distinguishes a weekday-range cron from a daily one', () => {
    const weekdays = formatCadence({ name: 'x', message: 'y', schedMode: 'cron', cronExpr: '0 17 * * 1-5' })
    const daily = formatCadence({ name: 'x', message: 'y', schedMode: 'cron', cronExpr: '0 17 * * *' })
    expect(weekdays).not.toBe(daily)
    expect(weekdays.trim()).not.toBe('')
  })

  it('renders a weekly schedule with a weekday name, not an index', () => {
    const out = formatCadence({
      name: 'x', message: 'y', schedMode: 'weekly', weekDays: [1], weekTime: '06:00',
    })
    expect(out.trim()).not.toBe('')
    // The ISO index itself must not leak into the label.
    expect(out).not.toMatch(/\b1\b(?!\d)/)
  })

  it('falls back to the raw expression for a cron shape it does not model', () => {
    const odd = '*/7 3 1 * *'
    expect(formatCadence({ name: 'x', message: 'y', schedMode: 'cron', cronExpr: odd })).toBe(odd)
  })

  it('produces a non-empty label for every shipped preset', () => {
    for (const p of SCHEDULE_PRESETS) {
      expect(formatCadence(p.prefill).trim(), `${p.id} renders no cadence`).not.toBe('')
    }
  })
})

describe('formatCadence interval unit words (#32066)', () => {
  afterEach(async () => {
    await i18next.changeLanguage('en')
  })

  const sixHours = { name: 'x', message: 'y', schedMode: 'interval', intVal: 6, intUnit: 'hours' } as const

  it('says the unit in Japanese words, never the narrow 6h', async () => {
    await i18next.changeLanguage('ja')
    expect(formatCadence(sixHours)).toBe('6\u00A0時間ごと')
  })

  it('keeps a readable unit in English', () => {
    expect(formatCadence(sixHours)).toBe('Every 6\u00A0hr')
  })
})

describe('formatCadence weekday list (#34103)', () => {
  afterEach(async () => {
    await i18next.changeLanguage('en')
  })

  const monWed = { name: 'x', message: 'y', schedMode: 'weekly', weekDays: [1, 3], weekTime: '06:00' } as const

  it('separates the days the way Japanese does', async () => {
    await i18next.changeLanguage('ja')
    expect(formatCadence(monWed)).toMatch(/^毎週 · 月、水 /)
  })

  it('keeps the English comma', () => {
    expect(formatCadence(monWed)).toMatch(/^Weekly · Mon, Wed /)
  })
})
