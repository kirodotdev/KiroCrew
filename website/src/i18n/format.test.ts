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
import { SUPPORTED_LANGUAGES } from './languages'
import {
  activeLocale,
  collator,
  compareText,
  fmtBytes,
  fmtCompact,
  fmtCurrency,
  fmtDate,
  fmtDateFields,
  fmtDateTime,
  fmtDateNumeric,
  fmtDateTimeNumeric,
  fmtTimeNumeric,
  fmtDuration,
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
    // DurationFormat may or may not exist depending on the Node version;
    // fmtUnit uses NumberFormat's `unit` style regardless, so the golden
    // outputs must hold either way.
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

describe('fmtDate / fmtTime / fmtDateTime / the numeric widths', () => {
  it('renders the phase gate\'s named example for zh-CN', async () => {
    // Golden — the exact literal a zh-CN UI on an en-US browser must render:
    // "2026年7月30日".
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

  it('reproduces every bare toLocale* default byte for byte, in every shipped locale', async () => {
    // The contract these three helpers hold: a call site that passes NO options
    // renders the locale's own all-numeric width, seconds included. These helpers
    // are that width, so using `fmtDateTimeNumeric(d)` in place of
    // `d.toLocaleString()` changes WHICH locale is read and nothing else —
    // English output is unchanged.
    //
    // Asserted against the platform call itself rather than a golden literal, so
    // a CLDR data change moves both sides together instead of turning this red.
    for (const code of SUPPORTED_LANGUAGES.filter(l => !l.devOnly).map(l => l.code)) {
      await withLanguage(code, () => {
        const tag = activeLocale()
        expect(fmtDateNumeric(INSTANT), `${code} date`)
          .toBe(INSTANT.toLocaleDateString(tag, UTC))
        expect(fmtTimeNumeric(INSTANT), `${code} time`)
          .toBe(INSTANT.toLocaleTimeString(tag, UTC))
        expect(fmtDateTimeNumeric(INSTANT), `${code} date+time`)
          .toBe(INSTANT.toLocaleString(tag, UTC))
      })
      __resetFormatterCache()
    }
  })

  it('keeps the second that the numeric widths carry and the style presets drop', () => {
    // `fmtTime`/`fmtDateTime` use `timeStyle: 'short'`, which has no second. Log
    // rows, cron execution lists and the next-run tooltip use the second to tell
    // two runs apart, so they use the numeric helpers rather than the presets.
    expect(fmtTimeNumeric(INSTANT)).toMatch(/:05/)
    expect(fmtDateTimeNumeric(INSTANT)).toMatch(/:05/)
    expect(fmtTime(INSTANT, UTC)).not.toMatch(/:05/)
    expect(fmtDateTime(INSTANT, UTC)).not.toMatch(/:05/)
  })

  it('renders a missing value as an em dash in the numeric widths too', () => {
    expect(fmtDateNumeric(null)).toBe('—')
    expect(fmtTimeNumeric(undefined)).toBe('—')
    expect(fmtDateTimeNumeric('')).toBe('—')
  })

  it('renders a missing date as an em dash', () => {
    expect(fmtDate(null)).toBe('—')
    expect(fmtTime(undefined)).toBe('—')
  })

  it('builds a time from explicit components without hitting the style conflict', () => {
    // Regression: `fmtTime(d, { hour, minute })` threw TypeError, because
    // `fmtTime` injects `timeStyle` and ECMA-402 CreateDateTimeFormat step 37
    // forbids combining a style with a component. The option types now make that
    // spelling a compile error; this asserts the correct entry point works at
    // runtime.
    expect(fmtDateFields(INSTANT, { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }))
      .toMatch(/15|3/)
    expect(() =>
      fmtDateFields(INSTANT, { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }),
    ).not.toThrow()
  })

  it('proves the conflict this split avoids is real', () => {
    // The raw Intl call that merging the two option types would produce. If a
    // future refactor merges them back together, this documents what breaks.
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
    expect(fmtRelative(at(0), { now })).toBe('now')
    expect(fmtRelative(at(86400), { now })).toBe('yesterday')
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

describe('fmtDuration', () => {
  it('preserves the compact English compound the app already rendered', () => {
    // Golden (en). These match the compact compounds the app renders —
    // `${m}m ${s}s` (useUptime), `${h}h ${m}m` (InstanceTabBar).
    expect(fmtDuration([[6, 'minute'], [38, 'second']])).toBe('6m 38s')
    expect(fmtDuration([[1, 'hour'], [2, 'minute'], [3, 'second']])).toBe('1h 2m 3s')
    expect(fmtDuration([[2, 'hour'], [15, 'minute']])).toBe('2h 15m')
  })

  it('joins with ListFormat, not a literal space', async () => {
    // The reason this matters: zh joins unit lists with NOTHING, so a hardcoded
    // space would leave a stray gap in `6分钟38秒`.
    await withLanguage('zh-CN', () => {
      expect(fmtDuration([[6, 'minute'], [38, 'second']])).toBe(
        new Intl.ListFormat('zh-CN', { type: 'unit', style: 'narrow' }).format([
          new Intl.NumberFormat('zh-CN', { style: 'unit', unit: 'minute', unitDisplay: 'narrow' }).format(6),
          new Intl.NumberFormat('zh-CN', { style: 'unit', unit: 'second', unitDisplay: 'narrow' }).format(38),
        ]),
      )
      expect(fmtDuration([[6, 'minute'], [38, 'second']])).not.toContain(' ')
    })
  })

  it('renders zero parts by default, because callers depend on it', () => {
    // `fmtTurnElapsed` rounds to whole seconds BEFORE splitting precisely so
    // 119.6s reads `2m 0s` and never the invalid `1m 60s`. Dropping the zero by
    // default would silently undo that fix, which is why it is opt-in.
    expect(fmtDuration([[2, 'minute'], [0, 'second']])).toBe('2m 0s')
    expect(fmtDuration([[0, 'minute'], [38, 'second']])).toBe('0m 38s')
  })

  it('drops zero parts on request, for callers that used to branch', () => {
    expect(fmtDuration([[0, 'hour'], [15, 'minute']], { dropZero: true })).toBe('15m')
  })

  it('keeps a unit when every part is zero, rather than rendering empty', () => {
    // `0s` is a real reading; '' would silently blank the surface.
    expect(fmtDuration([[0, 'minute'], [0, 'second']], { dropZero: true })).toBe('0s')
  })

  it('renders an em dash when no part is finite', () => {
    expect(fmtDuration([[NaN, 'second']])).toBe('—')
    expect(fmtDuration([])).toBe('—')
  })
})

describe('fmtCompact', () => {
  it('abbreviates per the language, not with a hardcoded K', async () => {
    expect(fmtCompact(1234)).toBe('1.2K') // golden (en)
    expect(fmtCompact(999)).toBe('999')

    // zh abbreviates on 万 (10^4), so 15300 is 1.5万 and 1234 does not
    // abbreviate at all — a `K` suffix is an English fact, not a numeric one.
    await withLanguage('zh-CN', () => {
      expect(fmtCompact(15300)).toBe(
        new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(15300),
      )
    })
  })
})

describe('fmtBytes', () => {
  it('formats each magnitude with one shared implementation', () => {
    // Golden (en). `kB` is the SI spelling CLDR uses.
    expect(fmtBytes(512)).toBe('512B')
    expect(fmtBytes(1500)).toBe('1.5kB')
    expect(fmtBytes(2_400_000)).toBe('2.4MB')
    expect(fmtBytes(3_100_000_000)).toBe('3.1GB')
  })

  it('divides by 1000 so the SI unit label is honest', () => {
    // `kB` is the DECIMAL SI unit, so dividing by 1000 keeps the label honest —
    // 1000 bytes is `1kB`, not 1024. Intl offers no binary (kibibyte) unit, so
    // the divisor is 1000.
    expect(fmtBytes(1000)).toBe('1kB')
  })

  it('localizes the unit and the separator', async () => {
    await withLanguage('ru', () => {
      expect(fmtBytes(1500)).toBe(
        new Intl.NumberFormat('ru', {
          style: 'unit', unit: 'kilobyte', unitDisplay: 'narrow', maximumFractionDigits: 1,
        }).format(1.5),
      )
    })
  })

  it('renders a non-finite size as an em dash', () => {
    expect(fmtBytes(NaN)).toBe('—')
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
