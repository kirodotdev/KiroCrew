/**
 * A day-part word inside the user's own words is never a time.
 *
 * Asserts the invariant across the WHOLE table. Example failure shapes:
 *
 *   bare 晚 inside 晚班    -> 交晚班报告 saved as 交班报告
 *   下午 inside 下午茶     -> 提醒我买下午茶 saved as 买茶 at 15:00
 *
 * Matched day parts are BLANKED out of the saved text, so a match inside a compound
 * does not mis-schedule the reminder — it rewrites what the user typed. The guard is
 * `inSchedulePosition`: a day part with no clock counts only when the next character
 * ends the time phrase, or a day/frequency marker precedes it.
 */
import { describe, it, expect } from 'vitest'
import { parseReminder } from '../apps/crew-companion/reminderParse'

const NOW = new Date('2026-08-01T10:00:00+08:00')
const parse = (s: string) => parseReminder(s, NOW, '提醒')

/** Every day-part word currently in the table, paired with a real compound. */
const COMPOUNDS: ReadonlyArray<[string, string]> = [
  ['下午茶', '买下午茶'],       // afternoon tea
  ['中午饭', '订中午饭'],       // lunch
  ['晚上好', '写晚上好的卡片'], // "good evening"
  ['早上好', '录早上好的语音'],
  ['夜里人', '统计夜里人流'],
  ['凌晨场', '订凌晨场的票'],
  ['上午场', '订上午场的票'],
  ['傍晚风', '写傍晚风的诗'],
  ['早晨跑', '记早晨跑的距离'],
]

describe('day parts inside ordinary words are left alone', () => {
  for (const [compound, body] of COMPOUNDS) {
    it(`keeps "${compound}" whole in 提醒我${body}`, () => {
      const r = parse(`提醒我${body}`)
      expect(r).not.toBeNull()
      // The text must survive verbatim — that is the whole point.
      expect(r!.text).toContain(compound)
      expect(r!.text).toBe(body)
    })
  }

  it('the reported case: 提醒我买下午茶 keeps its 下午', () => {
    const r = parse('提醒我买下午茶')
    expect(r!.text).toBe('买下午茶')
    // No time was requested, so the parser must ask rather than pick 15:00.
    expect(r!.needsSchedule).toBe(true)
  })
})

describe('real time phrases still resolve', () => {
  it.each([
    ['今晚提醒我锁门', '锁门', 20],
    ['明天下午提醒我开会', '开会', 15],
    ['下午提醒我打电话', '打电话', 15],
    ['明天早上提醒我交表', '交表', 9],
  ])('%s', (input, text, hour) => {
    const r = parse(input)
    expect(r!.text).toBe(text)
    expect(r!.needsSchedule).toBe(false)
    expect(new Date(r!.fireAt!).getHours()).toBe(hour)
  })

  it('a day part with an explicit clock is unaffected by the guard', () => {
    // These go through the clock scans, which always require a clock — pinned
    // so the guard cannot regress them.
    for (const [input, hour] of [['下午三点提醒我吃药', 15], ['晚上八点提醒我吃药', 20]] as const) {
      const r = parse(input)
      expect(r!.text).toBe('吃药')
      expect(new Date(r!.fireAt!).getHours()).toBe(hour)
    }
  })

  it('每晚 still recurs daily at its own hour', () => {
    const r = parse('每晚提醒我拉伸')
    expect(r!.text).toBe('拉伸')
    expect(r!.recurrence?.everyMinutes).toBe(1440)
  })
})

describe('the deliberate imperfection is documented, not accidental', () => {
  it('a day part after the verb loses its hour but never its text', () => {
    // 提醒我明天下午开会 — 下午 sits after the verb and before a non-verb action, so
    // it cannot be told apart from 下午茶 without part-of-speech knowledge. We
    // choose the conservative failure: the text is kept intact and the day falls
    // back to the conventional hour rather than 15:00.
    const r = parse('提醒我明天下午开会')
    expect(r!.text).toContain('下午开会')     // text preserved — the thing that matters
    expect(r!.needsSchedule).toBe(false)      // 明天 still gives it a day
    expect(new Date(r!.fireAt!).getHours()).toBe(9)
  })
})
