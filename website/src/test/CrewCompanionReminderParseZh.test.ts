/**
 * The Chinese reminder parser.
 *
 * This reads what the user typed in their own words and turns it into a time, so a
 * misparse is not a cosmetic bug — it sets the reminder for the wrong moment, or
 * silently finds no time at all and leaves the user believing one was set.
 *
 * The parser is pure, so these tests exercise the real thing. They are written
 * against behaviour a Chinese speaker would expect from each phrase, not against
 * the implementation's internals.
 */
import { describe, it, expect } from 'vitest'
import {
  hasHan,
  cnNumber,
  parseZhParts,
  ZH_LEAD_FILLER,
} from '../apps/crew-companion/reminderParseZh'

describe('hasHan tells Chinese input from Latin', () => {
  it.each(['喝水', '20分钟后开会', '记得buy milk'])('sees Han in %s', (s) =>
    expect(hasHan(s)).toBe(true))

  it.each(['drink water', '', '20 min', '123', '!!!'])('sees none in %s', (s) =>
    expect(hasHan(s)).toBe(false))
})

describe('cnNumber reads Chinese numerals', () => {
  it.each([
    ['一', 1], ['二', 2], ['三', 3], ['五', 5], ['九', 9], ['十', 10],
  ])('reads %s as %i', (raw, n) => expect(cnNumber(raw as string)).toBe(n))

  it.each([
    ['十一', 11], ['十五', 15], ['二十', 20], ['二十五', 25], ['三十', 30],
  ])('reads the compound %s as %i', (raw, n) => expect(cnNumber(raw as string)).toBe(n))

  it.each(['', 'abc', '啊'])('returns null for %s', (raw) =>
    expect(cnNumber(raw)).toBeNull())
})

describe('a relative delay', () => {
  it.each([
    ['20分钟后提醒我喝水', 20],
    ['五分钟后站起来', 5],
    ['半小时后喝水', 30],
    ['一小时后休息', 60],
    ['两小时后开会', 120],
  ])('%s -> %i minutes from now', (input, minutes) => {
    const r = parseZhParts(input as string)
    expect(r.hasSignal).toBe(true)
    expect(r.delayMinutes).toBe(minutes)
  })
})

describe('a clock time', () => {
  it('reads a bare 24-hour time', () => {
    const r = parseZhParts('14:30提醒我交周报')
    expect(r.clock).toEqual({ hour: 14, minute: 30, explicit: true })
  })

  it('resolves 下午 to the afternoon', () => {
    const r = parseZhParts('下午三点提醒我喝水')
    expect(r.clock?.hour).toBe(15)
  })

  it('leaves 上午 in the morning', () => {
    const r = parseZhParts('上午九点开会')
    expect(r.clock?.hour).toBe(9)
  })

  it('reads 点半 as the half hour', () => {
    const r = parseZhParts('八点半吃早饭')
    expect(r.clock?.hour).toBe(8)
    expect(r.clock?.minute).toBe(30)
  })
})

describe('a day offset', () => {
  it.each([
    ['明天九点开会', 1],
    ['后天下午两点', 2],
  ])('%s -> day +%i', (input, offset) => {
    expect(parseZhParts(input as string).dayOffset).toBe(offset)
  })

  it('treats an unqualified time as today', () => {
    expect(parseZhParts('九点开会').dayOffset).toBe(0)
  })
})

describe('a repeat', () => {
  it.each([
    ['每30分钟喝水', 30],
    ['每小时站起来', 60],
    ['每两小时休息一下', 120],
  ])('%s repeats every %i minutes', (input, minutes) => {
    expect(parseZhParts(input as string).everyMinutes).toBe(minutes)
  })

  it('does not read a one-off delay as a repeat', () => {
    const r = parseZhParts('20分钟后喝水')
    expect(r.everyMinutes).toBeNull()
    expect(r.delayMinutes).toBe(20)
  })
})

describe('when there is no time in the sentence', () => {
  it.each(['喝水', '记得买牛奶', '给妈妈打电话'])('reports no signal for %s', (input) => {
    const r = parseZhParts(input as string)
    expect(r.hasSignal).toBe(false)
    // and nothing invented — the whole point is not to guess a time the user
    // never gave, which is the rule the backend enforces too.
    expect(r.delayMinutes).toBeNull()
    expect(r.clock).toBeNull()
    expect(r.everyMinutes).toBeNull()
  })
})

describe('the spans it reports for stripping', () => {
  it('marks the schedule words so the reminder keeps only what to do', () => {
    const input = '20分钟后提醒我喝水'
    const r = parseZhParts(input)
    expect(r.spans.length).toBeGreaterThan(0)
    // every span has to be a real slice of the input, or stripping corrupts the text
    for (const s of r.spans) {
      expect(s.start).toBeGreaterThanOrEqual(0)
      expect(s.end).toBeLessThanOrEqual(input.length)
      expect(s.end).toBeGreaterThan(s.start)
    }
  })
})

describe('the lead-in filler pattern', () => {
  it.each(['请提醒我喝水', '帮我记得喝水', '别忘了喝水', '麻烦提醒我喝水'])(
    'strips the polite opener in %s',
    (input) => {
      expect((input as string).replace(ZH_LEAD_FILLER, '').length)
        .toBeLessThan((input as string).length)
    },
  )

  it('leaves a sentence that opens with the task itself alone', () => {
    expect('喝水'.replace(ZH_LEAD_FILLER, '')).toBe('喝水')
  })
})

describe('cnNumber reads the remaining number forms', () => {
  it.each([['半', 0.5], ['两', 2], ['45', 45]] as Array<[string, number]>)(
    'reads %s as %s',
    (raw, n) => expect(cnNumber(raw)).toBe(n),
  )
})

describe('a rate ("N times a day") becomes an interval', () => {
  it.each([
    ['每天三次提醒我喝水', 480], // 1440 / 3
    ['一天两次吃药', 720], //       1440 / 2
    ['每小时两次看一下', 30], //     60 / 2
  ])('%s repeats every %i minutes', (input, minutes) => {
    const r = parseZhParts(input as string)
    expect(r.everyMinutes).toBe(minutes)
    expect(r.delayMinutes).toBeNull()
  })
})

describe('the "过 / 每隔" phrasings', () => {
  it('reads 过两小时 as a +120m delay', () => {
    const r = parseZhParts('过两小时提醒我')
    expect(r.delayMinutes).toBe(120)
    expect(r.everyMinutes).toBeNull()
  })

  it('reads 每隔30分钟 as a 30-minute repeat', () => {
    const r = parseZhParts('每隔30分钟喝水')
    expect(r.everyMinutes).toBe(30)
  })

  it('reads 30分钟之后 (with 之) as a delay', () => {
    expect(parseZhParts('30分钟之后休息').delayMinutes).toBe(30)
  })

  it('reads multi-day / multi-week delays', () => {
    expect(parseZhParts('两天后交报告').delayMinutes).toBe(2 * 1440)
    expect(parseZhParts('一周后复诊').delayMinutes).toBe(10080)
  })
})

describe('colon times with and without a day part', () => {
  it('shifts 下午3:51 into the afternoon and marks it explicit', () => {
    const r = parseZhParts('下午3:51提醒我下班')
    expect(r.clock).toEqual({ hour: 15, minute: 51, explicit: true })
  })

  it('leaves a bare 1–12 colon time AMBIGUOUS so it can resolve forward', () => {
    // The pinned fix for "我3:51下班" typed at 15:50: marking it explicit made it
    // roll to 03:51 TOMORROW. It must stay ambiguous (explicit=false).
    const r = parseZhParts('我3:51下班')
    expect(r.clock).toEqual({ hour: 3, minute: 51, explicit: false })
  })

  it('treats a 24-hour colon time (>=13) as explicit', () => {
    expect(parseZhParts('15:51提醒我').clock?.explicit).toBe(true)
  })
})

describe('点 with an explicit minute count', () => {
  it('reads 八点三十分 as 8:30', () => {
    const r = parseZhParts('八点三十分吃早饭')
    expect(r.clock?.hour).toBe(8)
    expect(r.clock?.minute).toBe(30)
  })
})

describe('the far day offset', () => {
  it('reads 大后天 as +3 days', () => {
    expect(parseZhParts('大后天提醒我体检').dayOffset).toBe(3)
  })
})

describe('known limitation: 每周一 (every Monday) is refused, not mis-scheduled', () => {
  it('documents CURRENT behaviour — a weekday repeat reports no signal', () => {
    // The Recurrence model is a single interval and cannot express a weekday, so
    // findInterval deliberately bails when 周 is followed by 一–日 rather than
    // silently firing weekly on the wrong day. The caller then asks. Asserted
    // as-is so a future weekday feature updates this test on purpose.
    const r = parseZhParts('每周一提醒我开会')
    expect(r.everyMinutes).toBeNull()
    expect(r.hasSignal).toBe(false)
  })

  it('but a plain 每周 (every week) IS a weekly repeat', () => {
    expect(parseZhParts('每周提醒我交周报').everyMinutes).toBe(10080)
  })
})
