/**
 * The parser must never invent a time, and must not lose one to an earlier number.
 *
 * Two failures with the same root: looseness about what counts as a schedule.
 *
 *  - `findClock` tested only the FIRST number-shaped match, so a reminder opening
 *    with a quantity ("take 2 pills … at 3pm") had its real clock hidden behind the
 *    bare `2` and lost its time completely.
 *  - `parseZh` accepted "a day marker was found" as a schedule, so 今天 (today, no
 *    time) fell through to an invented one-hour default — while the English path
 *    correctly asked the user. The two languages disagreed on one rule.
 *
 * `needsSchedule`'s contract is that the caller ASKS rather than the parser guessing,
 * so these assert that contract on both sides rather than the two symptoms.
 */
import { describe, it, expect } from 'vitest'
import { parseReminder } from '../apps/crew-companion/reminderParse'

const NOW = new Date('2026-08-01T10:00:00+08:00')
const parse = (s: string) => parseReminder(s, NOW, 'Reminder')
const hourOf = (iso: string) => new Date(iso).getHours()

describe('a leading quantity never hides the clock', () => {
  it('finds 3pm in "take 2 pills every day at 3pm"', () => {
    const r = parse('take 2 pills every day at 3pm')
    expect(r).not.toBeNull()
    // Previously: findClock matched the bare `2`, failed its own shape test, and
    // returned null — so this was saved as "one interval from now" (24h).
    expect(r!.needsSchedule).toBe(false)
    expect(hourOf(r!.fireAt!)).toBe(15)
    expect(r!.recurrence?.everyMinutes).toBe(1440)
  })

  it.each([
    ['buy 3 apples at 5pm', 17],
    ['walk 2 dogs at 7am', 7],
    ['read 10 pages at 9:30pm', 21],
  ])('finds the clock in "%s"', (input, hour) => {
    const r = parse(input)
    expect(r!.needsSchedule).toBe(false)
    expect(hourOf(r!.fireAt!)).toBe(hour)
  })

  it('still refuses to read a bare interval number as a clock', () => {
    // The guard this fix must NOT weaken: "every 2 hours" has no clock at all.
    const r = parse('stretch every 2 hours')
    expect(r!.recurrence?.everyMinutes).toBe(120)
  })
})

describe('the parser never invents a time', () => {
  it('asks when Chinese gives a day but no time', () => {
    // 今天 is a signal but carries NO time; this was silently scheduled at now + 1h.
    const r = parse('今天提醒我买牛奶')
    expect(r!.needsSchedule).toBe(true)
    expect(r!.fireAt).toBeNull()
    expect(r!.text).toBe('买牛奶')
  })

  it('asks when English gives no time either — the same rule', () => {
    const r = parse('remind me to buy milk')
    expect(r!.needsSchedule).toBe(true)
    expect(r!.fireAt).toBeNull()
  })

  it('both languages agree on what counts as a schedule', () => {
    // The asymmetry WAS the bug, so pin the pairs rather than each side alone.
    const pairs: ReadonlyArray<[string, string, boolean]> = [
      ['remind me to buy milk', '提醒我买牛奶', true],          // no time -> ask
      ['today remind me to buy milk', '今天提醒我买牛奶', true], // day, no time -> ask
      ['remind me to buy milk at 3pm', '下午三点提醒我买牛奶', false],
      ['buy milk every 2 hours', '每2小时提醒我买牛奶', false],
      ['buy milk in 20 minutes', '20分钟后提醒我买牛奶', false],
    ]
    for (const [en, zh, shouldAsk] of pairs) {
      expect(parse(en)!.needsSchedule, `en: ${en}`).toBe(shouldAsk)
      expect(parse(zh)!.needsSchedule, `zh: ${zh}`).toBe(shouldAsk)
    }
  })

  it('a Chinese recurrence with no clock still fires one interval out', () => {
    // The behaviour the removed `?? 60` was meant to serve must survive.
    const r = parse('每2小时提醒我喝水')
    expect(r!.needsSchedule).toBe(false)
    expect(r!.recurrence?.everyMinutes).toBe(120)
    const delta = new Date(r!.fireAt!).getTime() - NOW.getTime()
    expect(Math.round(delta / 60_000)).toBe(120)
  })

  it('明天 with no time still gets the conventional morning hour', () => {
    // dayOffset > 0 IS a schedule — this must not regress into asking.
    const r = parse('明天提醒我交报告')
    expect(r!.needsSchedule).toBe(false)
    expect(hourOf(r!.fireAt!)).toBe(9)
  })
})
