/**
 * The English reminder parser (`parseReminder`).
 *
 * This reads a plain-language phrase the user typed and turns it into a concrete
 * fire time, a repeat, or an honest "I couldn't find a time" — so a misparse is
 * never cosmetic: it fires the reminder at the wrong moment, or silently invents
 * one the user never asked for. These tests exercise the real parser (it is pure)
 * against the behaviour a person would expect from each phrase, at a fixed `now`.
 *
 * `now` is built with the numeric Date constructor (LOCAL time, 10:00), not an
 * ISO-Z string: the parser resolves clocks with `setHours`/`getHours` in local
 * time, so a UTC anchor would make every hour assertion depend on the runner's
 * timezone. Sibling files that assert hours use the same local-time convention.
 */
import { describe, it, expect } from 'vitest'
import { parseReminder } from '../apps/crew-companion/reminderParse'

// Mon 2026-06-15, 10:00 local. Numeric ctor => local time, timezone-independent.
const NOW = new Date(2026, 5, 15, 10, 0, 0, 0)
const p = (s: string) => parseReminder(s, NOW, 'Reminder')

/** A local clock reading relative to NOW, mirroring the parser's atClock+rollDay. */
const at = (h: number, m: number, addDays = 0): Date => {
  const d = new Date(NOW)
  d.setHours(h, m, 0, 0)
  d.setDate(d.getDate() + addDays)
  return d
}
/** now + N minutes, as the delay / interval paths compute it. */
const inMin = (mins: number): Date => new Date(NOW.getTime() + mins * 60_000)

describe('relative delays ("in ...")', () => {
  const CASES: Array<[string, string, number]> = [
    ['drink water in 20 minutes', 'drink water', 20],
    ['stand up in 5 mins', 'stand up', 5],
    ['call back in an hour', 'call back', 60],
    ['stretch in 2h', 'stretch', 120],
    ['break in half hour', 'break', 30], // WORD_NUMBERS.half = 0.5 * 60
  ]
  for (const [input, text, mins] of CASES) {
    it(`${JSON.stringify(input)} → +${mins}m`, () => {
      const r = p(input)
      expect(r.needsSchedule).toBe(false)
      expect(r.recurrence).toBeNull()
      expect(r.text).toBe(text)
      expect(r.fireAt).toBe(inMin(mins).toISOString())
    })
  }
})

describe('absolute clock times', () => {
  it('reads "at 3pm" as 15:00 today (it is still ahead of now)', () => {
    const r = p('meeting at 3pm')
    expect(r.text).toBe('meeting')
    expect(r.fireAt).toBe(at(15, 0).toISOString())
    expect(r.recurrence).toBeNull()
  })

  it('reads a bare "3pm" without the "at"', () => {
    expect(p('standup 3pm').fireAt).toBe(at(15, 0).toISOString())
  })

  it('reads a 24-hour "15:00"', () => {
    const r = p('gym 15:00')
    expect(r.text).toBe('gym')
    expect(r.fireAt).toBe(at(15, 0).toISOString())
  })

  it('reads "noon" as 12:00', () => {
    expect(p('lunch at noon').fireAt).toBe(at(12, 0).toISOString())
  })

  it('reads "midday" as 12:00', () => {
    expect(p('call at midday').fireAt).toBe(at(12, 0).toISOString())
  })

  it('rolls "midnight" forward to tomorrow 00:00 (already past today)', () => {
    const r = p('sleep at midnight')
    const fire = new Date(r.fireAt!)
    expect(fire.getHours()).toBe(0)
    expect(fire.getDate()).toBe(at(0, 0, 1).getDate())
  })

  it('an explicit am hour already past today rolls the DAY, not the meridiem', () => {
    // 8am < now(10:00) and it is meridiem-explicit, so only the day advances —
    // it must NOT silently become 8pm.
    const r = p('workout at 8am')
    expect(r.fireAt).toBe(at(8, 0, 1).toISOString())
  })

  it('a bare hour past today resolves to its PM reading, same day', () => {
    // "at 9" at 10:00 is ambiguous; 09:00 is past, so the next occurrence is
    // 21:00 today — the documented bare-hour rule.
    const r = p('call at 9')
    expect(r.text).toBe('call')
    expect(r.fireAt).toBe(at(21, 0).toISOString())
  })

  it('a bare hour still ahead today stays AM', () => {
    expect(p('call at 11').fireAt).toBe(at(11, 0).toISOString())
  })
})

describe('recurring intervals', () => {
  const CASES: Array<[string, string, number]> = [
    ['drink water every 2 hours', 'drink water', 120],
    ['stretch every 30 minutes', 'stretch', 30],
    ['stand up every hour', 'stand up', 60], // bare "every <unit>" (no count)
    ['water hourly', 'water', 60],
    ['standup daily', 'standup', 1440],
    ['review weekly', 'review', 10080],
    ['drink water twice a day', 'drink water', 720], // rate: 1440 / 2
    ['take pills 3 times a day', 'take pills', 480], // rate: 1440 / 3
  ]
  for (const [input, text, every] of CASES) {
    it(`${JSON.stringify(input)} repeats every ${every}m`, () => {
      const r = p(input)
      expect(r.needsSchedule).toBe(false)
      expect(r.recurrence).toEqual({ everyMinutes: every })
      expect(r.text).toBe(text)
    })
  }

  it('a recurring reminder with no stated time first fires ONE interval out', () => {
    // Not immediately: adding "every 2 hours" must not fire on Enter.
    const r = p('drink water every 2 hours')
    expect(r.fireAt).toBe(inMin(120).toISOString())
  })

  it('does not read the count in "every 2 hours" as a clock time', () => {
    const r = p('every 2 hours')
    expect(r.recurrence).toEqual({ everyMinutes: 120 })
    // If "2" leaked to findClock this would be a today-2-o'clock instant, not +2h.
    expect(r.fireAt).toBe(inMin(120).toISOString())
  })

  it('scans past a leading quantity to find the real clock', () => {
    // "take 2 pills every day at 3pm": the leading "2" is number-shaped but not a
    // clock; the parser must keep scanning and land on 3pm.
    const r = p('take 2 pills every day at 3pm')
    expect(r.recurrence).toEqual({ everyMinutes: 1440 })
    expect(r.text).toBe('take 2 pills')
    expect(r.fireAt).toBe(at(15, 0).toISOString())
  })

  it('an explicit clock wins over the interval\'s default first-fire', () => {
    // "every day at 3pm": first fire is 3pm today (ahead of now), not now+1day.
    const r = p('vitamins every day at 3pm')
    expect(r.fireAt).toBe(at(15, 0).toISOString())
    expect(r.recurrence).toEqual({ everyMinutes: 1440 })
  })
})

describe('"tomorrow"', () => {
  it('a bare "tomorrow" schedules the conventional 9am, not midnight', () => {
    const r = p('buy milk tomorrow')
    expect(r.text).toBe('buy milk')
    expect(r.needsSchedule).toBe(false)
    expect(r.recurrence).toBeNull()
    expect(r.fireAt).toBe(at(9, 0, 1).toISOString())
  })

  it('"tomorrow at <clock>" honours the stated hour on the next day', () => {
    const r = p('submit report tomorrow at 5pm')
    expect(r.text).toBe('submit report')
    expect(r.fireAt).toBe(at(17, 0, 1).toISOString())
  })

  it('accepts the "tmr" shorthand', () => {
    expect(p('pay rent tmr').fireAt).toBe(at(9, 0, 1).toISOString())
  })
})

describe('lead-in filler is stripped from the saved text', () => {
  const CASES: Array<[string, string]> = [
    ['remind me to drink water in 20 minutes', 'drink water'],
    ['please remind me to call mom tomorrow', 'call mom'],
    ['remember to stretch every hour', 'stretch'],
    ['nudge me to stand up in 5 minutes', 'stand up'],
  ]
  for (const [input, text] of CASES) {
    it(`${JSON.stringify(input)} → ${JSON.stringify(text)}`, () => {
      expect(p(input).text).toBe(text)
    })
  }
})

describe('when no schedule can be found', () => {
  it.each(['buy milk', 'call the dentist', 'water the plants'])(
    'reports needsSchedule for %s and invents nothing',
    (input) => {
      const r = p(input)
      expect(r.needsSchedule).toBe(true)
      expect(r.fireAt).toBeNull()
      expect(r.recurrence).toBeNull()
      expect(r.text).toBe(input) // the text is preserved verbatim
    },
  )

  it('falls back to the provided default text for a schedule-only phrase', () => {
    // Only "in 20 minutes" — nothing left to say once the schedule is stripped.
    const r = parseReminder('in 20 minutes', NOW, 'Reminder')
    expect(r.text).toBe('Reminder')
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe(inMin(20).toISOString())
  })

  it('empty input needs a schedule and uses the fallback text', () => {
    const r = parseReminder('', NOW, 'Reminder')
    expect(r.text).toBe('Reminder')
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
  })

  it('dispatches Han input to the Chinese path', () => {
    // "喝水" (drink water) with no time → the zh parser also reports needsSchedule.
    const r = parseReminder('喝水', NOW, '提醒')
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('喝水')
  })

  it('resolves a Chinese phrase end-to-end through parseReminder', () => {
    // Exercises the parseZh wrapper: delay + text stripping + ISO fire time.
    const r = parseReminder('20分钟后提醒我喝水', NOW, '提醒')
    expect(r.needsSchedule).toBe(false)
    expect(r.text).toBe('喝水')
    expect(r.fireAt).toBe(inMin(20).toISOString())
  })
})

describe('known limitation: "in half an hour" is not recognised', () => {
  it('documents CURRENT behaviour — "an" is consumed as the unit, so no time is found', () => {
    // The delay regex reads "half" as the count and "an" as the unit; "an" is not
    // a unit, so the whole delay match fails and the parser asks rather than
    // guessing. Failing to a needsSchedule prompt (not a wrong time) is the
    // module's intended failure mode, but this common phrasing arguably should
    // parse to +30m. Flagged in the report, asserted here as-is so a future fix
    // updates this test deliberately.
    const r = p('in half an hour')
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
  })

  it('but "in half hour" (no "an") does resolve to +30m', () => {
    expect(p('coffee in half hour').fireAt).toBe(inMin(30).toISOString())
  })
})
