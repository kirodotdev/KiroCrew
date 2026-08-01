/**
 * Behaviour tests for the locale-aware formatting seam.
 *
 * ## Two kinds of assertion here, on purpose
 *
 * **Derived** assertions compute the expected string from the same `Intl` the
 * module uses. They prove *wiring* — that the formatter ran with the app's
 * language rather than the host's — and they degrade to a vacuous pass on a
 * small-icu Node instead of hard-failing, which is the precedent
 * `formatter.test.ts` sets and the reason it is safe to assert cross-locale at
 * all. Official Node 20+ builds are full-icu (verified `icu_small=false`), so in
 * CI these are real comparisons.
 *
 * **Golden** assertions hardcode a literal, and are used only where the phase's
 * own acceptance gate names an exact string ("a zh-CN UI renders 2026年7月30日")
 * or where an English output must be pinned because ~4000 existing assertions
 * depend on it. Each one is marked.
 *
 * ## Timezone
 *
 * The vitest setup pins no `TZ`, so every date assertion passes an explicit
 * `timeZone`. Without that these tests pass in UTC CI and fail on a developer
 * machine in Asia/Shanghai — a class of flake this file must not introduce.
 *
 * ## Language restoration
 *
 * i18next is a module-level singleton shared by the whole suite, so a file that
 * switches language must switch back or it silently breaks every later file.
 * Same `afterEach` discipline as `formatter.test.ts`.
 */

import { describe, it, expect, afterEach } from 'vitest'

import { i18next } from './index'
import {
  activeLocale,
  collator,
  compareText,
  fmtCurrency,
  fmtDate,
  fmtDateFields,
  fmtDateTime,
  fmtList,
  fmtNumber,
  fmtPercent,
  fmtRelative,
  fmtTime,
  fmtUnit,
  fmtWeekday,
  toDate,
  __resetFormatterCache,
} from './format'

/** 2026-07-30T15:04:05Z — a Thursday, mid-afternoon UTC. */
const INSTANT = new Date('2026-07-30T15:04:05Z')
const UTC = { timeZone: 'UTC' } as const

async function withLanguage(code: string, run: () => void | Promise<void>): Promise<void> {
  await i18next.changeLanguage(code)
  await run()
}

afterEach(async () => {
  await i18next.changeLanguage('en')
  __resetFormatterCache()
})

describe('activeLocale', () => {
  it('follows the app language, not the host', async () => {
    await withLanguage('zh-CN', () => {
      expect(activeLocale()).toBe('zh-CN')
    })
  })

  it('reports the RESOLVED language when an unsupported tag is requested', async () => {
    // `zz` is not in supportedLngs, so i18next falls back. Formatting must
    // follow the language actually in use, not the rejected request.
    await withLanguage('zz', () => {
      expect(activeLocale()).toBe('en')
    })
  })
})

describe('toDate', () => {
  it('accepts ISO strings, epoch seconds, epoch milliseconds and Date', () => {
    const ms = INSTANT.getTime()
    expect(toDate(INSTANT)?.getTime()).toBe(ms)
    expect(toDate('2026-07-30T15:04:05Z')?.getTime()).toBe(ms)
    expect(toDate(ms)?.getTime()).toBe(ms)
    expect(toDate(Math.floor(ms / 1000))?.getTime()).toBe(ms)
  })

  it('rejects the values that previously rendered as garbage ages', () => {
    // ts=0 rendered as ~20602d; NaN/undefined rendered "Invalid Date".
    for (const bad of [0, -1, NaN, Infinity, null, undefined, '', 'not a date']) {
      expect(toDate(bad as never)).toBeNull()
    }
  })
})

describe('fmtNumber', () => {
  it('groups per the active language', async () => {
    // Golden (en): the app's baseline rendering.
    expect(fmtNumber(1234567.891)).toBe('1,234,567.891')

    // Derived: de inverts separators, hi uses Indian grouping, bn uses Bengali
    // digits. Deriving keeps this honest on a small-icu runtime.
    for (const lng of ['de', 'hi', 'bn', 'ru']) {
      await withLanguage(lng, () => {
        expect(fmtNumber(1234567.891)).toBe(new Intl.NumberFormat(lng).format(1234567.891))
      })
    }
  })

  it('renders a non-finite input as an em dash rather than NaN', () => {
    expect(fmtNumber(NaN)).toBe('—')
    expect(fmtNumber(Infinity)).toBe('—')
  })
})

describe('fmtPercent', () => {
  it('takes a ratio and lets the locale place the sign', async () => {
    expect(fmtPercent(0.4567)).toBe('46%') // golden (en)
    await withLanguage('de', () => {
      // de separates the % with a non-breaking space; derived so the exact
      // space codepoint comes from CLDR rather than being guessed here.
      expect(fmtPercent(0.4567)).toBe(
        new Intl.NumberFormat('de', { style: 'percent', maximumFractionDigits: 0 }).format(0.4567),
      )
    })
  })
})

describe('fmtCurrency', () => {
  it('places the symbol per locale', async () => {
    expect(fmtCurrency(12.5)).toBe('$12.50') // golden (en)
    await withLanguage('de', () => {
      expect(fmtCurrency(12.5)).toBe(
        new Intl.NumberFormat('de', { style: 'currency', currency: 'USD' }).format(12.5),
      )
    })
  })
})

describe('fmtUnit', () => {
  it('formats durations and sizes without Intl.DurationFormat', () => {
    // DurationFormat is undefined on the Node 20 baseline; this is the
    // replacement path, and this assertion is what would catch a future
    // refactor reaching for the unavailable API.
    expect(typeof (Intl as { DurationFormat?: unknown }).DurationFormat).toBe('undefined')
    expect(fmtUnit(1.5, 'second', { maximumFractionDigits: 1 })).toBe('1.5s') // golden (en)
    expect(fmtUnit(90, 'minute')).toBe('90m') // golden (en)
    expect(fmtUnit(512, 'megabyte')).toBe('512MB') // golden (en)
  })

  it('translates the unit itself', async () => {
    await withLanguage('de', () => {
      expect(fmtUnit(90, 'minute')).toBe(
        new Intl.NumberFormat('de', { style: 'unit', unit: 'minute', unitDisplay: 'narrow' }).format(90),
      )
    })
  })
})

describe('fmtDate / fmtTime / fmtDateTime', () => {
  it('renders the phase gate\'s named example for zh-CN', async () => {
    // Golden, and the literal the Phase 4 acceptance gate names: "a zh-CN UI on
    // an en-US browser renders 2026年7月30日".
    await withLanguage('zh-CN', () => {
      expect(fmtDate(INSTANT, UTC)).toBe('2026年7月30日')
    })
  })

  it('renders the English baseline', () => {
    expect(fmtDate(INSTANT, UTC)).toBe('Jul 30, 2026') // golden (en)
    expect(fmtTime(INSTANT, UTC)).toBe('3:04 PM') // golden (en)
  })

  it('switches 12h/24h per locale rather than per browser', async () => {
    // The defect being fixed: a Chinese UI on an en-US host showed "3:04 PM".
    await withLanguage('zh-CN', () => {
      expect(fmtTime(INSTANT, UTC)).toBe(
        new Intl.DateTimeFormat('zh-CN', { timeStyle: 'short', timeZone: 'UTC' }).format(INSTANT),
      )
    })
  })

  it('combines date and time', () => {
    expect(fmtDateTime(INSTANT, UTC)).toContain('Jul 30, 2026')
    expect(fmtDateTime(INSTANT, UTC)).toContain('3:04')
  })

  it('renders a missing date as an em dash', () => {
    expect(fmtDate(null)).toBe('—')
    expect(fmtTime(undefined)).toBe('—')
  })

  it('builds a time from explicit components without hitting the style conflict', () => {
    // Regression: `fmtTime(d, { hour, minute })` threw TypeError, because
    // `fmtTime` injects `timeStyle` and ECMA-402 CreateDateTimeFormat step 37
    // forbids combining a style with a component. It shipped in the command
    // palette's recents provider and emptied the palette for every session with
    // a timestamp. The option types now make that spelling a compile error;
    // this asserts the correct entry point works at runtime.
    expect(fmtDateFields(INSTANT, { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }))
      .toMatch(/15|3/)
    expect(() =>
      fmtDateFields(INSTANT, { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }),
    ).not.toThrow()
  })

  it('proves the conflict this split avoids is real', () => {
    // The raw Intl call the old code produced. If a future refactor merges the
    // two option types back together, this documents what breaks.
    expect(() =>
      new Intl.DateTimeFormat('en', { timeStyle: 'short', hour: '2-digit' }).format(INSTANT),
    ).toThrow(TypeError)
  })
})

describe('fmtWeekday', () => {
  it('maps ISO 1..7 onto Monday..Sunday', () => {
    // Golden (en). The index is the cron contract; only the label is localized.
    expect([1, 2, 3, 4, 5, 6, 7].map((d) => fmtWeekday(d))).toEqual([
      'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun',
    ])
  })

  it('keeps the index → weekday mapping stable regardless of language', async () => {
    await withLanguage('de', () => {
      expect(fmtWeekday(1)).toBe(
        new Intl.DateTimeFormat('de', { weekday: 'short', timeZone: 'UTC' })
          .format(new Date(Date.UTC(2024, 0, 1))),
      )
    })
  })

  it('supports long and narrow styles', () => {
    expect(fmtWeekday(1, 'long')).toBe('Monday')
    expect(fmtWeekday(1, 'narrow')).toBe('M')
  })

  it('rejects an out-of-range index instead of inventing a day', () => {
    expect(fmtWeekday(0)).toBe('—')
    expect(fmtWeekday(8)).toBe('—')
  })
})

describe('fmtRelative', () => {
  const now = INSTANT.getTime()
  const at = (secondsAgo: number) => new Date(now - secondsAgo * 1000)

  it('preserves the compact English output the app already rendered', () => {
    // Golden (en) — these four are byte-identical to the hand-rolled ladder
    // that this replaces, which is what keeps the migration reviewable.
    expect(fmtRelative(at(45), { now })).toBe('45s ago')
    expect(fmtRelative(at(120), { now })).toBe('2m ago')
    expect(fmtRelative(at(7200), { now })).toBe('2h ago')
    expect(fmtRelative(at(5 * 86400), { now })).toBe('5d ago')
  })

  it('applies the two reviewed English deltas', () => {
    // Documented in format.ts: CLDR words these idiomatically instead of
    // mechanically, which is the reason to use the platform at all.
    expect(fmtRelative(at(0), { now })).toBe('now') // was 'just now'
    expect(fmtRelative(at(86400), { now })).toBe('yesterday') // was '1d ago'
  })

  it('words the same instant idiomatically per language', async () => {
    // Derived. zh says 昨天, de vorgestern for two days — output CLDR produces
    // and a template-literal ladder structurally cannot.
    for (const lng of ['zh-CN', 'de', 'ru', 'bn']) {
      await withLanguage(lng, () => {
        const rtf = new Intl.RelativeTimeFormat(lng, { numeric: 'auto', style: 'narrow' })
        expect(fmtRelative(at(86400), { now })).toBe(rtf.format(-1, 'day'))
        expect(fmtRelative(at(120), { now })).toBe(rtf.format(-2, 'minute'))
      })
    }
  })

  it('truncates toward zero so an age never rounds into the future', () => {
    // 119s elapsed is "1m ago"; rounding would claim 2m had passed.
    expect(fmtRelative(at(119), { now })).toBe('1m ago')
  })

  it('formats a future timestamp forwards instead of clamping it', () => {
    // Clock skew should be visible, not laundered into "now".
    expect(fmtRelative(new Date(now + 300_000), { now })).toBe('in 5m')
  })

  it('renders a missing timestamp as an em dash', () => {
    expect(fmtRelative(null)).toBe('—')
    expect(fmtRelative(0)).toBe('—')
  })

  it('pins the unit when asked, so a zero calendar-day delta reads "today"', async () => {
    // Regression: a caller that has already reduced its input to whole calendar
    // days got "now" for anything earlier the same day, because a zero delta
    // means "under one second" to the auto unit picker. Issue Radar's
    // `relativeDate` is that caller.
    expect(fmtRelative(INSTANT, { now, unit: 'day', style: 'long' })).toBe('today')
    expect(fmtRelative(at(86400), { now, unit: 'day', style: 'long' })).toBe('yesterday')
    expect(fmtRelative(at(5 * 86400), { now, unit: 'day', style: 'long' })).toBe('5 days ago')

    await withLanguage('zh-CN', () => {
      const rtf = new Intl.RelativeTimeFormat('zh-CN', { numeric: 'auto', style: 'long' })
      expect(fmtRelative(INSTANT, { now, unit: 'day', style: 'long' })).toBe(rtf.format(0, 'day'))
    })
  })
})

describe('fmtList', () => {
  it('uses the language\'s own separator and conjunction', async () => {
    expect(fmtList(['A', 'B', 'C'])).toBe('A, B, and C') // golden (en)

    // Golden (zh-CN): the ideographic comma is the specific defect a
    // `join(', ')` plus a translated " and " cannot express.
    await withLanguage('zh-CN', () => {
      expect(fmtList(['A', 'B', 'C'])).toBe('A、B和C')
    })
  })

  it('supports disjunction', () => {
    expect(fmtList(['A', 'B'], { type: 'disjunction' })).toBe('A or B')
  })

  it('drops empty entries so a filtered array cannot leave a dangling separator', () => {
    expect(fmtList(['A', '', 'B'])).toBe('A and B')
  })
})

describe('collator / compareText', () => {
  it('sorts digits naturally instead of by byte', () => {
    // The defect: byte order puts reviewer-10 before reviewer-2.
    expect(['reviewer-10', 'reviewer-2'].sort(compareText)).toEqual(['reviewer-2', 'reviewer-10'])
  })

  it('ignores case so one list has one ordering', () => {
    expect(compareText('apple', 'Apple')).toBe(0)
  })

  it('sorts per the active language', async () => {
    // Derived: de and sv disagree about ä; asserting against the language's own
    // collator proves the app language reached Intl.
    await withLanguage('de', () => {
      const words = ['zeta', 'ärger', 'apfel']
      expect([...words].sort(compareText)).toEqual(
        [...words].sort(new Intl.Collator('de', { numeric: true, sensitivity: 'base' }).compare),
      )
    })
  })

  it('exposes the raw collator for callers needing other options', () => {
    expect(collator({ sensitivity: 'variant' }).compare('apple', 'Apple')).not.toBe(0)
  })
})
