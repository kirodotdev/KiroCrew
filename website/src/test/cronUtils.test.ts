import { describe, it, expect, afterEach } from 'vitest'
// The INITIALIZED instance: the bare `i18next` package has no catalogs, so
// `changeLanguage` resolves nothing and every assertion below passes vacuously.
import { i18next } from '../i18n/all'
import { expandDow, fmtCron, scheduleLabel, scheduleMinutes } from '../utils/cronUtils'
import { __resetFormatterCache } from '../i18n/format'
import type { CronJob } from '../types'

describe('expandDow', () => {
  it('expands a range', () => {
    expect(expandDow('1-5')).toEqual([1, 2, 3, 4, 5])
  })
  it('passes through comma-separated values', () => {
    expect(expandDow('0,6')).toEqual([0, 6])
  })
  it('handles mixed range and values', () => {
    expect(expandDow('1-3,5')).toEqual([1, 2, 3, 5])
  })
  it('handles single value', () => {
    expect(expandDow('3')).toEqual([3])
  })
  it('handles reversed range (wrap-around)', () => {
    expect(expandDow('5-1')).toEqual([5, 6, 0, 1])
  })
  it('handles named day (MON)', () => {
    expect(expandDow('MON')).toEqual([1])
  })
  it('handles named range (MON-FRI)', () => {
    expect(expandDow('MON-FRI')).toEqual([1, 2, 3, 4, 5])
  })
  it('handles named comma-separated (MON,WED,FRI)', () => {
    expect(expandDow('MON,WED,FRI')).toEqual([1, 3, 5])
  })
  it('handles named wrap-around (FRI-MON)', () => {
    expect(expandDow('FRI-MON')).toEqual([5, 6, 0, 1])
  })
  it('handles lowercase named days', () => {
    expect(expandDow('mon-fri')).toEqual([1, 2, 3, 4, 5])
  })
  it('handles mixed named and numeric', () => {
    expect(expandDow('MON,3,FRI')).toEqual([1, 3, 5])
  })
  it('returns empty for invalid named input', () => {
    expect(expandDow('INVALID')).toEqual([])
  })
  it('returns empty for empty string', () => {
    expect(expandDow('')).toEqual([])
  })
  it('clamps out-of-bounds range to 0-6 wrap', () => {
    expect(expandDow('5-2')).toEqual([5, 6, 0, 1, 2])
  })
  it('normalizes dow 7 to 0 (Sunday)', () => {
    expect(expandDow('7')).toEqual([0])
  })
  it('normalizes range with 7 endpoint', () => {
    expect(expandDow('5-7')).toEqual([5, 6, 0])
  })
  it('deduplicates after normalization', () => {
    expect(expandDow('0,7')).toEqual([0])
  })
  it('expands 0-7 as every day', () => {
    expect(expandDow('0-7')).toEqual([0, 1, 2, 3, 4, 5, 6])
  })
  it('returns empty for step expressions', () => {
    expect(expandDow('*/2')).toEqual([])
    expect(expandDow('1-5/2')).toEqual([])
  })
})

describe('fmtCron', () => {
  it('names a THREE-day gappy list, the picker toggles Mon/Wed/Fri produce', () => {
    // The toggle buttons emit a gappy list, so a 2-name cap showed raw cron for
    // one of the commonest weekly schedules. Measured 147px in a 164px box.
    expect(fmtCron('0 9 * * 1,3,5')).toBe('9:00 AM · Mon,Wed,Fri')
    expect(fmtCron('0 0 * * 0,3,6')).toBe('12:00 AM · Sun,Wed,Sat')
  })

  it('reads an all-seven-day set as the bare clock, exactly like `*`', () => {
    // Every day ticked IS daily; `Sun-Sat` would be a second spelling of it in
    // one column. Held for every syntax that covers the week.
    const daily = fmtCron('0 9 * * *')
    expect(daily).toBe('9:00 AM')
    expect(fmtCron('0 9 * * 0,1,2,3,4,5,6')).toBe(daily)
    expect(fmtCron('0 9 * * 0-6')).toBe(daily)
    expect(fmtCron('0 9 * * 1-7')).toBe(daily)
  })

  it('still declines a gappy list past three names', () => {
    // Four gappy names outgrow the column, and the raw expression is shorter.
    expect(fmtCron('0 0 * * 1,2,4,6')).toBe('0 0 * * 1,2,4,6')
  })

  it('collapses a CONTIGUOUS comma list to endpoints, as the picker emits it', () => {
    // `JobForm`'s weekly mode joins checked days with commas, so five ticked
    // weekdays arrive as a LIST, not a range, and used to show raw cron.
    expect(fmtCron('0 9 * * 1,2,3,4,5')).toBe('9:00 AM · Mon-Fri')
    expect(fmtCron('0 9 * * 1,2,3')).toBe('9:00 AM · Mon-Wed')
  })

  it('does NOT collapse a gappy list into a range it never asked for', () => {
    // `Mon-Fri` would assert Tue/Thu, which `1,3,5` excludes. Up to three names it
    // lists them out instead; past that the raw expression stands.
    expect(fmtCron('0 0 * * 1,3,5')).toBe('12:00 AM · Mon,Wed,Fri')
    expect(fmtCron('0 0 * * 1,2,4,6')).toBe('0 0 * * 1,2,4,6')
    // Two names still render as a list.
    expect(fmtCron('30 15 * * 2,4')).toBe('3:30 PM · Tue,Thu')
  })

  it('keeps a literal range on its authored endpoints, not the sorted ones', () => {
    // `5-1` is Fri..Mon; sorting would read Sun-Sat and silently widen it.
    expect(fmtCron('0 9 * * 5-1')).toBe('9:00 AM · Fri-Mon')
  })

  it('renders a weekday range by its ENDPOINTS, not five names', () => {
    // The label lives in a narrow column, so a range shows Mon-Fri rather than
    // expanding to every day it covers.
    expect(fmtCron('0 9 * * 1-5')).toBe('9:00 AM · Mon-Fri')
  })
  it('renders a two-day list', () => {
    expect(fmtCron('30 15 * * 2,4')).toBe('3:30 PM · Tue,Thu')
  })
  it('renders a single weekday', () => {
    expect(fmtCron('5 6 * * 1')).toBe('6:05 AM · Mon')
  })
  it('renders an unrestricted expression as the clock alone', () => {
    expect(fmtCron('0 8 * * *')).toBe('8:00 AM')
  })
  it('renders a month and day-of-month pair', () => {
    // February 30 does not exist as a Date; the pair must still render.
    expect(fmtCron('0 0 30 2 *')).toBe('12:00 AM · Feb 30')
  })
  it('renders a month alone', () => {
    expect(fmtCron('15 6 * 7 *')).toBe('6:15 AM · Jul')
  })
  it('handles the 12-hour clock at noon and midnight', () => {
    expect(fmtCron('0 0 * * *')).toBe('12:00 AM')
    expect(fmtCron('0 12 * * *')).toBe('12:00 PM')
  })
  it('names Sunday whether it is spelled 0 or 7', () => {
    expect(fmtCron('0 12 * * 0')).toBe('12:00 PM · Sun')
    expect(fmtCron('0 12 * * 7')).toBe('12:00 PM · Sun')
  })
  it('resolves a wrap-around range to its endpoints', () => {
    expect(fmtCron('0 9 * * 5-1')).toBe('9:00 AM · Fri-Mon')
  })
  it('resolves named day tokens', () => {
    expect(fmtCron('0 13 * * MON-FRI')).toBe('1:00 PM · Mon-Fri')
    expect(fmtCron('30 9 * * MON,WED')).toBe('9:30 AM · Mon,Wed')
  })

  describe('falls back to the raw expression rather than to prose', () => {
    it('does NOT combine a restricted day-of-month with a day-of-week', () => {
      // Cron ORs the two fields: this fires on the 1st AND every Monday. The old
      // spelling rendered `Mon 00:00 (days 1)`, asserting an AND that is not there.
      expect(fmtCron('0 0 1 * 1')).toBe('0 0 1 * 1')
      expect(fmtCron('0 9 1-3 * 1-5')).toBe('0 9 1-3 * 1-5')
    })
    it('declines a bare day-of-month, which has no Intl rendering', () => {
      expect(fmtCron('0 0 1 * *')).toBe('0 0 1 * *')
    })
    it('declines a FOUR-item gappy day list as too wide', () => {
      expect(fmtCron('0 0 * * 1,2,4,6')).toBe('0 0 * * 1,2,4,6')
    })
    it('declines step and range forms in minute, hour or day-of-week', () => {
      expect(fmtCron('0 9 * * */2')).toBe('0 9 * * */2')
      expect(fmtCron('0,30 * * * *')).toBe('0,30 * * * *')
      expect(fmtCron('*/5 9-16 * * 1-5')).toBe('*/5 9-16 * * 1-5')
    })
    it('declines out-of-range clock and calendar fields', () => {
      expect(fmtCron('0 99 * * *')).toBe('0 99 * * *')
      expect(fmtCron('0 0 32 1 *')).toBe('0 0 32 1 *')
      expect(fmtCron('0 0 * 13 *')).toBe('0 0 * 13 *')
    })
    it('returns anything that is not five fields untouched', () => {
      expect(fmtCron('bogus')).toBe('bogus')
      expect(fmtCron('0 0 * *')).toBe('0 0 * *')
      expect(fmtCron('0 0 * * * *')).toBe('0 0 * * * *')
    })
  })

  it('is shorter than the verbose backend prose it replaces', () => {
    // The whole point: it has to fit where cron_descriptor's 53 characters
    // could not.
    expect(fmtCron('0 0 30 2 *').length).toBeLessThanOrEqual(20)
  })
})

describe('fmtCron localization', () => {
  /**
   * The reason this label is built here instead of on the server: every word in
   * it comes from `Intl`, so it follows the dashboard's language. A backend-minted
   * string could not, having no catalog path. These assert the label genuinely
   * CHANGES with the language rather than being English that merely passed
   * through `Intl` — so they compare against the `en` rendering, not just against
   * a fixed foreign string.
   */
  afterEach(async () => {
    await i18next.changeLanguage('en')
    __resetFormatterCache()
  })

  async function inLanguage(code: string, expr: string): Promise<string> {
    await i18next.changeLanguage(code)
    __resetFormatterCache()
    return fmtCron(expr)
  }

  it('translates the weekday name', async () => {
    const en = await inLanguage('en', '0 9 * * 1-5')
    const de = await inLanguage('de', '0 9 * * 1-5')
    const ja = await inLanguage('ja', '0 9 * * 1-5')
    expect(en).toBe('9:00 AM · Mon-Fri')
    expect(de).not.toBe(en)
    expect(ja).not.toBe(en)
    // Not merely different — actually the target language's weekday.
    expect(ja).toContain('月')
  })

  it('translates the month name and respects the locale field order', async () => {
    expect(await inLanguage('en', '0 0 30 2 *')).toBe('12:00 AM · Feb 30')
    // de and fr put the day first, with their own separators and abbreviation
    // periods — supplied by the formatter, not hand-assembled.
    expect(await inLanguage('de', '0 0 30 2 *')).toBe('0:00 · 30. Feb.')
    expect(await inLanguage('fr', '0 0 30 2 *')).toBe('0:00 · 30 févr.')
  })

  it('does not double the CJK month affix', async () => {
    // A CJK locale returns the month as a BARE NUMBER with `月` a separate literal,
    // so injecting a pre-formatted `2月` produced `2月月30日`. Only the DAY is replaced.
    expect(await inLanguage('ja', '0 0 30 2 *')).toBe('0:00 · 2月30日')
    expect(await inLanguage('zh-CN', '0 0 30 2 *')).toBe('0:00 · 2月30日')
  })

  it('renders February 30 rather than rolling it over to March', async () => {
    // `Date.UTC(2024, 1, 30)` is March 1, so a naive format names the WRONG month.
    // Asserted on the month NAME, since the day "30" contains a 3 of its own.
    expect(await inLanguage('en', '0 0 30 2 *')).not.toContain('Mar')
    expect(await inLanguage('ja', '0 0 30 2 *')).not.toContain('3月')
    expect(await inLanguage('de', '0 0 30 2 *')).not.toContain('Mär')
  })

  it('uses the locale 24-hour clock where that is the convention', async () => {
    // de is a 24-hour locale, so the AM/PM the en label carries must be absent.
    const de = await inLanguage('de', '0 13 * * *')
    expect(de).not.toContain('AM')
    expect(de).not.toContain('PM')
    expect(de).toContain('13')
  })

  it('leaves the raw-expression fallback untranslated, since it is not prose', async () => {
    // A cron expression is the user's own input and must round-trip verbatim in
    // every language.
    for (const code of ['en', 'de', 'ja', 'zh-CN']) {
      expect(await inLanguage(code, '*/5 9-16 * * 1-5')).toBe('*/5 9-16 * * 1-5')
    }
  })

  it('renders the day in the locale numbering system', async () => {
    // bn uses Bengali digits; substituting ASCII gave `30 ফেব` for `৩০ ফেব`.
    const bn = await inLanguage('bn', '0 0 30 2 *')
    expect(bn).toContain('৩০')
    expect(bn).not.toContain('30')
  })
})

describe('scheduleMinutes', () => {
  const job = (o: Partial<CronJob>) => ({ id: 'j', name: 'n', message: 'm', enabled: true, schedule: '', ...o }) as CronJob

  it('returns minutes since midnight so the sort is chronological', () => {
    // The label sorts wrongly as text: `1:00 PM` collates before `9:00 AM`.
    expect(scheduleMinutes(job({ cron_expr: '0 9 * * *' }))).toBe(540)
    expect(scheduleMinutes(job({ cron_expr: '0 13 * * *' }))).toBe(780)
    expect(scheduleMinutes(job({ cron_expr: '30 0 * * *' }))).toBe(30)
  })

  it('is null for a row with no clock to compare', () => {
    // A declined shape's minute/hour still parse, which is why the check asks
    // whether `fmtCron` produced a label, not whether the digits look like a time.
    expect(scheduleMinutes(job({ schedule: 'every 600s' }))).toBeNull()
    expect(scheduleMinutes(job({ cron_expr: '0 0 1 * 1' }))).toBeNull()
    expect(scheduleMinutes(job({ cron_expr: '*/5 9-16 * * 1-5' }))).toBeNull()
    expect(scheduleMinutes(job({ cron_expr: 'bogus' }))).toBeNull()
  })

  it('orders 9 AM before 1 PM, which the label alone does not', () => {
    const nine = job({ cron_expr: '0 9 * * *' })
    const onePm = job({ cron_expr: '0 13 * * *' })
    expect(scheduleMinutes(nine)!).toBeLessThan(scheduleMinutes(onePm)!)
    // The defect this replaces: as text, the 1 PM label sorts first.
    expect(scheduleLabel(onePm).localeCompare(scheduleLabel(nine))).toBeLessThan(0)
  })
})

describe('scheduleLabel', () => {
  const job = (o: Partial<CronJob>) => ({ id: 'j', name: 'n', message: 'm', enabled: true, schedule: '', ...o }) as CronJob

  it('renders a cron job through fmtCron', () => {
    expect(scheduleLabel(job({ cron_expr: '0 0 30 2 *', schedule: 'At 12:00 AM, on day 30…' })))
      .toBe('12:00 AM · Feb 30')
  })
  it('keeps the backend string when there is no cron expression', () => {
    expect(scheduleLabel(job({ schedule: 'every 600s' }))).toBe('every 600s')
  })
  it('never returns undefined for a job with neither', () => {
    expect(scheduleLabel(job({}))).toBe('')
  })
})
