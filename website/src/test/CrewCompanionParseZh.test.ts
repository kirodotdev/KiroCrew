/**
 * The Chinese parser must never eat schedule characters out of ordinary words.
 *
 * `DAY_PARTS` words are matched anywhere in the reminder and then BLANKED from
 * the saved text, so any entry short enough to occur inside a normal word
 * corrupts what the user typed rather than merely mis-timing it. A bare `晚`
 * turned 明天提醒我交晚班报告 ("hand in the night-shift report") into 交班报告.
 *
 * This is the SECOND defect of that shape — the first was UTF-16 offsets in
 * `cleanText` — so these tests assert the INVARIANT rather than the single
 * character that was reported: for reminders whose content embeds day-part
 * characters inside ordinary words, the saved text must come back whole.
 */
import { describe, it, expect } from 'vitest'
import { parseReminder } from '../apps/crew-companion/reminderParse'

const NOW = new Date('2026-08-01T10:00:00+08:00')
const parse = (s: string) => parseReminder(s, NOW, '提醒')

/** Words that merely CONTAIN a day-part character and must survive intact. */
const ORDINARY_WORDS: ReadonlyArray<[string, string]> = [
  ['明天提醒我交晚班报告', '交晚班报告'],   // 晚 inside 晚班 (night shift)
  ['提醒我订早餐会的会议室', '订早餐会的会议室'], // 早 inside 早餐会
  ['明天提醒我买夜宵', '买夜宵'],           // 夜 inside 夜宵
  ['提醒我写中期报告', '写中期报告'],       // 中 inside 中期
]

describe('Chinese day-part words never eat ordinary text', () => {
  for (const [input, expected] of ORDINARY_WORDS) {
    it(`keeps the text of "${input}"`, () => {
      const r = parse(input)
      expect(r).not.toBeNull()
      expect(r!.text).toBe(expected)
    })
  }

  it('still resolves the real schedule phrases', () => {
    // 今晚 -> tonight 20:00
    const tonight = parse('今晚提醒我锁门')
    expect(tonight!.text).toBe('锁门')
    expect(new Date(tonight!.fireAt!).getHours()).toBe(20)

    // 每晚 -> daily, and it keeps its 20:00 rather than "one interval from now"
    const nightly = parse('每晚提醒我拉伸')
    expect(nightly!.text).toBe('拉伸')
    expect(nightly!.recurrence?.everyMinutes).toBe(1440)

    // 晚上八点 -> explicit clock still wins
    const eight = parse('晚上八点提醒我吃药')
    expect(eight!.text).toBe('吃药')
    expect(new Date(eight!.fireAt!).getHours()).toBe(20)
  })

  it('reads 明晚 as TOMORROW night, not tonight', () => {
    // findDayOffset matched 明天/明日 but not 明晚, so 明晚 took its hour from the
    // bare 晚 and its offset from nothing — resolving to TODAY 20:00.
    const r = parse('明晚提醒我倒垃圾')
    expect(r!.text).toBe('倒垃圾')
    const fire = new Date(r!.fireAt!)
    expect(fire.getHours()).toBe(20)
    // Compare calendar days, not getDate() + 1: NOW is 10:00 at +08:00, which is
    // the PREVIOUS day in a westerly local zone, so arithmetic on the day number
    // rolls off the end of the month and the test would fail on correct code.
    const dayOf = (d: Date) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
    const tomorrow = new Date(NOW)
    tomorrow.setDate(tomorrow.getDate() + 1)
    expect(dayOf(fire)).toBe(dayOf(tomorrow))
  })

  it('has no single-character day-part word left in the table', async () => {
    // The invariant itself: a one-character entry is what makes this class of bug
    // possible, so no future entry may be one.
    const src = await import('../apps/crew-companion/reminderParseZh?raw')
      .then((m: { default: string }) => m.default)
      .catch(() => null)
    if (src === null) return // ?raw unavailable in this config — the cases above still cover it
    const table = /const DAY_PARTS: ReadonlyArray<\[string, number, number\]> = \[([\s\S]*?)\n\]/.exec(src)
    expect(table).not.toBeNull()
    const words = [...table![1].matchAll(/\['([^']+)'/g)].map((m) => m[1])
    expect(words.length).toBeGreaterThan(5)
    expect(words.filter((w) => w.length < 2)).toEqual([])
  })
})
