/**
 * The span-alignment invariant, pinned as a RULE rather than as its symptoms.
 *
 * Both parsers measure schedule spans with `RegExp.index` (UTF-16 code units) and apply
 * them to the original typed text. Any derived string that spans are measured against
 * must therefore agree with the original about what index N means.
 *
 * This has now broken four separate times, each time in a different place:
 *   1. `cleanText` used `[...original]` (code points) — an emoji shifted every span.
 *   2. `masked = [...lower]` in the English path — same bug, sibling variable.
 *   3. `masked = [...s]` in the Chinese path — same bug again.
 *   4. `lower = original.toLowerCase()` — `İ` lowercases to TWO code units.
 *
 * So the tests below assert the *invariant* over a battery of hostile inputs, not the
 * one string a reviewer happened to report. Every case is a reminder someone could
 * plausibly type.
 */
import { describe, it, expect } from 'vitest'
import { parseReminder } from '../apps/crew-companion/reminderParse'
import { toUnits, foldForMatch } from '../apps/crew-companion/reminderText'

const NOW = new Date('2026-03-10T09:00:00')

describe('foldForMatch keeps code-unit length', () => {
  // The guarantee the parsers depend on. `İ` is the known length-changer; the rest
  // cover scripts a user might realistically mix into a reminder.
  const CASES = ['İlaç', 'ILAÇ', 'Σigma', 'ǄOK', '💊 pill', 'Æther', 'ß', 'ẛ', '喝水']

  for (const s of CASES) {
    it(`preserves length for ${JSON.stringify(s)}`, () => {
      expect(foldForMatch(s).length).toBe(s.length)
    })
  }

  it('differs from naive toLowerCase exactly where that would change length', () => {
    // Proof the hazard is real and that the helper is not a no-op wrapper.
    expect('İlaç'.toLowerCase().length).toBe(5)
    expect('İlaç'.length).toBe(4)
    expect(foldForMatch('İlaç').length).toBe(4)
  })

  it('still folds ordinary ASCII, so matching keeps working', () => {
    expect(foldForMatch('EVERY 2 Hours AT 9PM')).toBe('every 2 hours at 9pm')
  })
})

describe('toUnits counts code units, not code points', () => {
  it('splits an astral character into its surrogate halves', () => {
    // This is what makes indices line up with RegExp.index.
    expect(toUnits('💊').length).toBe(2)
    expect([...'💊'].length).toBe(1)
  })

  it('round-trips losslessly through join', () => {
    const s = '💊 water 喝水 İlaç'
    expect(toUnits(s).join('')).toBe(s)
  })
})

describe('the saved text is never corrupted by a span shift', () => {
  // Each input pairs a real schedule with text that used to shift the spans.
  const CASES: Array<[string, string]> = [
    ['İlaç at 3pm', 'İlaç'],                       // the reported case: was "İlaç a"
    ['İÇMEK SU at 8am', 'İÇMEK SU'],               // two length-changers
    ['💊 take pill at 9am', '💊 take pill'],        // astral, English path
    ['drink 💧 water every 2 hours', 'drink 💧 water'],
    ['Ǆungla at 7am', 'Ǆungla'],                   // titlecase digraph
    ['ΣIGMA review at 4pm', 'ΣIGMA review'],       // Greek
  ]

  for (const [input, expected] of CASES) {
    it(`keeps ${JSON.stringify(expected)} from ${JSON.stringify(input)}`, () => {
      const parsed = parseReminder(input, NOW, 'Reminder')
      expect(parsed.text).toBe(expected)
      // The schedule must still have been recognised — a parser that simply stopped
      // matching would pass the text assertion above while breaking the feature.
      expect(parsed.needsSchedule).toBe(false)
      expect(parsed.fireAt).toBeTruthy()
    })
  }

  it('keeps Chinese text intact when an emoji precedes the schedule', () => {
    // The zh path had the same `[...s]` defect; Han is BMP so only an astral
    // character exposes it.
    const parsed = parseReminder('💊 喝水 每2小时', NOW, '提醒')
    expect(parsed.text).toBe('💊 喝水')
    expect(parsed.recurrence?.everyMinutes).toBe(120)
  })

  it('strips a daily clock in the zh path when an emoji shifts the mask', () => {
    // The one input found to make the `masked = [...s]` defect observable, by
    // differential testing across ~18 candidates. With code-point masking the
    // interval span blanks the wrong characters, so `9点` survives into the saved
    // text AND is re-read as the clock, moving the fire time.
    // 每天9点 ("every day at 9") is ordinary phrasing, so this is a real user path.
    const parsed = parseReminder('💊 每天9点', NOW, '提醒')
    expect(parsed.text).toBe('💊')
    expect(parsed.recurrence?.everyMinutes).toBe(1440)
  })
})
