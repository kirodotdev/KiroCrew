/**
 * English day parts: position decides, not presence.
 *
 * Every English day-part word is also an ordinary noun or adjective — "morning report",
 * "afternoon tea", "night shift", "evening news". Matching on presence alone reads them
 * as the schedule AND blanks them out, so "submit morning report tomorrow" would be saved
 * as "submit report": a word the user typed, silently deleted.
 *
 * 晚班 and 下午茶 are the Chinese originals, guarded by `inSchedulePosition` in
 * reminderParseZh.ts — and `night shift` / `afternoon tea` are literally the same two
 * phrases in English. These tests pin BOTH languages together, so a change that guards
 * one parser and forgets the other fails here.
 */
import { describe, it, expect } from 'vitest'
import { parseReminder } from '../apps/crew-companion/reminderParse'

const NOW = new Date('2026-03-10T09:00:00')
const p = (s: string) => parseReminder(s, NOW, 'Reminder')

describe('a day-part word inside ordinary text is kept', () => {
  // The core case, plus the English twins of the two Chinese bugs.
  const KEEP: Array<[string, string]> = [
    ['submit morning report tomorrow', 'submit morning report'],
    ['buy afternoon tea tomorrow', 'buy afternoon tea'],          // twin of 下午茶
    ['send the night shift roster tomorrow', 'send the night shift roster'], // twin of 晚班
    ['review the evening news at 8pm', 'review the evening news'],
    ['morning report tomorrow', 'morning report'],
    ['morning yoga tomorrow', 'morning yoga'],
    ['the morning meeting notes tomorrow', 'the morning meeting notes'],
  ]

  for (const [input, text] of KEEP) {
    it(`keeps ${JSON.stringify(text)}`, () => {
      const r = p(input)
      expect(r.text).toBe(text)
      // The schedule still had to be found from the REST of the phrase, otherwise
      // this would pass simply by the parser giving up.
      expect(r.needsSchedule).toBe(false)
      expect(r.fireAt).toBeTruthy()
    })
  }
})

describe('a day-part word acting as the schedule is consumed', () => {
  // Introduced by a marker, or standing where a time belongs.
  const CONSUME: Array<[string, string]> = [
    ['call mom tomorrow morning', 'call mom'],
    ['this evening call dad', 'call dad'],
    ['take pills in the morning', 'take pills'],
    ['stretch every night', 'stretch'],
  ]

  for (const [input, text] of CONSUME) {
    it(`${JSON.stringify(input)} → ${JSON.stringify(text)}`, () => {
      const r = p(input)
      expect(r.text).toBe(text)
      expect(r.fireAt).toBeTruthy()
    })
  }

  it('consumes the introducing marker, not just the day part', () => {
    // Blanking only "evening" would leave "this" stranded in the saved text.
    expect(p('this evening call dad').text).toBe('call dad')
    expect(p('take pills in the morning').text).toBe('take pills')
  })

  it('still resolves the day part to its hour', () => {
    const hour = (s: string) => new Date(p(s).fireAt!).getHours()
    expect(hour('call mom tomorrow morning')).toBe(9)
    expect(hour('this evening call dad')).toBe(19)
    expect(hour('take pills in the morning')).toBe(9)
  })

  it('an explicit clock still wins over a day part', () => {
    // Pinned because findDayPart scans every occurrence; the clock must not lose.
    const r = p('every morning at 7am')
    expect(new Date(r.fireAt!).getHours()).toBe(7)
  })

  it('scans past a text occurrence to a later schedule one', () => {
    // "morning" appears twice: the first belongs to the text, the second is the time.
    const r = p('morning report tomorrow morning')
    expect(r.text).toBe('morning report')
    expect(new Date(r.fireAt!).getHours()).toBe(9)
  })
})

describe('the Chinese guard still holds (the pair must not drift)', () => {
  // Restated here so both languages are pinned in one place. If someone changes
  // one parser's rule, this file fails.
  it('keeps 下午茶 intact', () => {
    expect(parseReminder('明天买下午茶', NOW, '提醒').text).toBe('买下午茶')
  })

  it('keeps 晚班 intact', () => {
    expect(parseReminder('明天交晚班表', NOW, '提醒').text).toBe('交晚班表')
  })
})
