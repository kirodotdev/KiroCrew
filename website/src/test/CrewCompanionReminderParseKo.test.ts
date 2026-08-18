/**
 * The Korean reminder parser.
 *
 * This reads what the user typed in their own words and turns it into a time, so a
 * misparse is not a cosmetic bug — it sets the reminder for the wrong moment, or
 * silently finds no time at all and leaves the user believing one was set.
 *
 * The parser is pure, so these tests exercise the real thing. They are written
 * against behaviour a Korean speaker would expect from each phrase, not against the
 * implementation's internals.
 */
import { describe, it, expect } from 'vitest'
import {
  hasHangul,
  koNumber,
  parseKoParts,
  KO_LEAD_FILLER,
  KO_TRAIL_FILLER,
} from '../apps/crew-companion/reminderParseKo'
import { parseReminder } from '../apps/crew-companion/reminderParse'

describe('hasHangul tells Korean input from the rest', () => {
  it.each(['물 마시기', '20분 뒤에 회의', 'buy milk 하기'])('sees Hangul in %s', (s) =>
    expect(hasHangul(s)).toBe(true))

  it.each(['drink water', '', '20 min', '123', '喝水'])('sees none in %s', (s) =>
    expect(hasHangul(s)).toBe(false))
})

describe('koNumber reads both counting systems', () => {
  it.each([['한', 1], ['두', 2], ['세', 3], ['네', 4], ['열', 10], ['스물', 20], ['서른', 30]])(
    'reads the native counter %s as %i',
    (raw, n) => expect(koNumber(raw as string)).toBe(n),
  )

  it.each([['일', 1], ['삼', 3], ['십', 10], ['십오', 15], ['이십', 20], ['사십오', 45]])(
    'reads the sino form %s as %i',
    (raw, n) => expect(koNumber(raw as string)).toBe(n),
  )

  it('reads 반 as a half', () => expect(koNumber('반')).toBe(0.5))
  it('reads digits', () => expect(koNumber('45')).toBe(45))

  it.each(['', 'abc', '아무'])('returns null for %s', (raw) =>
    expect(koNumber(raw)).toBeNull())
})

describe('a relative delay', () => {
  it.each([
    ['20분 뒤에 물 마시기', 20],
    ['20분후 물 마시기', 20],
    ['5분 뒤 일어서기', 5],
    ['반 시간 후 휴식', 30],
    ['한 시간 뒤에 회의', 60],
    ['두 시간 후 약 먹기', 120],
    ['30분 있다가 전화하기', 30],
    ['이틀 뒤 보고서', 2 * 1440],
  ])('%s -> %i minutes from now', (input, minutes) => {
    const r = parseKoParts(input as string)
    expect(r.hasSignal).toBe(true)
    expect(r.delayMinutes).toBe(minutes)
    expect(r.everyMinutes).toBeNull()
  })
})

describe('a repeat', () => {
  it.each([
    ['30분마다 물 마시기', 30],
    ['한 시간마다 일어서기', 60],
    ['2시간 간격으로 스트레칭', 120],
    ['매 30분 물 마시기', 30],
    ['매시간 일어서기', 60],
    ['매일 약 먹기', 1440],
    ['매주 주간보고 쓰기', 10080],
  ])('%s repeats every %i minutes', (input, minutes) => {
    expect(parseKoParts(input as string).everyMinutes).toBe(minutes)
  })

  it('carries the day-part hour for 매일 저녁', () => {
    const r = parseKoParts('매일 저녁 산책하기')
    expect(r.everyMinutes).toBe(1440)
    expect(r.clock).toEqual({ hour: 19, minute: 0, explicit: true })
  })

  it('lets an explicit clock win over the day part in 매일 아침 7시', () => {
    const r = parseKoParts('매일 아침 7시에 약 먹기')
    expect(r.everyMinutes).toBe(1440)
    expect(r.clock).toEqual({ hour: 7, minute: 0, explicit: true })
  })
})

describe('a rate ("N times a day") becomes an interval', () => {
  it.each([
    ['하루에 세 번 약 먹기', 480], // 1440 / 3
    ['하루 두 번 물 주기', 720], //   1440 / 2
    ['한 시간에 두 번 확인하기', 30], // 60 / 2
  ])('%s repeats every %i minutes', (input, minutes) => {
    expect(parseKoParts(input as string).everyMinutes).toBe(minutes)
  })
})

describe('a clock time', () => {
  it('reads a bare 24-hour time as explicit', () => {
    expect(parseKoParts('14:30 주간보고 제출').clock).toEqual({
      hour: 14, minute: 30, explicit: true,
    })
  })

  it('leaves a bare 1–12 colon time AMBIGUOUS so it can resolve forward', () => {
    // Marking it explicit would roll 3:51 typed at 15:50 to 03:51 TOMORROW.
    expect(parseKoParts('3:51 퇴근').clock).toEqual({ hour: 3, minute: 51, explicit: false })
  })

  it('shifts 오후 into the afternoon', () => {
    expect(parseKoParts('오후 3시에 물 마시기').clock?.hour).toBe(15)
  })

  it('shifts 저녁 into the evening', () => {
    expect(parseKoParts('저녁 7시에 산책').clock?.hour).toBe(19)
  })

  it('leaves 오전 in the morning', () => {
    expect(parseKoParts('오전 9시 회의').clock?.hour).toBe(9)
  })

  it('keeps 오후 12시 at noon rather than shifting it to 24', () => {
    expect(parseKoParts('오후 12시에 점심').clock?.hour).toBe(12)
  })

  it('reads 오전 12시 as midnight', () => {
    expect(parseKoParts('오전 12시에 알람').clock?.hour).toBe(0)
  })

  it('reads 시 반 as the half hour', () => {
    const r = parseKoParts('8시 반에 아침 먹기')
    expect(r.clock?.hour).toBe(8)
    expect(r.clock?.minute).toBe(30)
  })

  it('reads an explicit minute count', () => {
    const r = parseKoParts('9시 30분에 회의')
    expect(r.clock?.hour).toBe(9)
    expect(r.clock?.minute).toBe(30)
  })

  it('reads a native counter as the hour', () => {
    expect(parseKoParts('오후 세 시에 커피').clock?.hour).toBe(15)
  })

  it('leaves a bare hour AMBIGUOUS so it resolves to the next occurrence', () => {
    expect(parseKoParts('9시에 회의').clock).toEqual({ hour: 9, minute: 0, explicit: false })
  })
})

describe('a day offset', () => {
  it.each([
    ['내일 9시에 회의', 1],
    ['모레 오후 2시 검진', 2],
    ['글피 서류 제출', 3],
  ])('%s -> day +%i', (input, offset) => {
    expect(parseKoParts(input as string).dayOffset).toBe(offset)
  })

  it('treats an unqualified time as today', () => {
    expect(parseKoParts('9시에 회의').dayOffset).toBe(0)
  })
})

describe('when there is no time in the sentence', () => {
  it.each(['물 마시기', '우유 사기', '엄마한테 전화하기'])('reports no signal for %s', (input) => {
    const r = parseKoParts(input as string)
    expect(r.hasSignal).toBe(false)
    // and nothing invented — the whole point is not to guess a time the user never
    // gave, which is the rule the backend enforces too.
    expect(r.delayMinutes).toBeNull()
    expect(r.clock).toBeNull()
    expect(r.everyMinutes).toBeNull()
  })
})

describe('a day-part word inside the user’s own words is NOT a time', () => {
  it.each([
    '아침 회의록 정리하기',
    '저녁 메뉴 정하기',
    '오후 발표 자료 만들기',
  ])('leaves %s alone', (input) => {
    const r = parseKoParts(input as string)
    expect(r.clock).toBeNull()
    expect(r.hasSignal).toBe(false)
  })

  it.each([
    ['저녁에 약 먹기', 19],
    ['내일 아침 운동하기', 9],
    ['오늘 밤 스트레칭', 20],
  ])('but reads %s as a time', (input, hour) => {
    expect(parseKoParts(input as string).clock?.hour).toBe(hour)
  })
})

describe('the spans it reports for stripping', () => {
  it('marks every schedule word as a real slice of the input', () => {
    const input = '20분 뒤에 물 마시기'
    const r = parseKoParts(input)
    expect(r.spans.length).toBeGreaterThan(0)
    for (const s of r.spans) {
      expect(s.start).toBeGreaterThanOrEqual(0)
      expect(s.end).toBeLessThanOrEqual(input.length)
      expect(s.end).toBeGreaterThan(s.start)
    }
  })
})

describe('the filler patterns', () => {
  it.each(['제발 물 마시기', '좀 쉬기', '리마인더: 물 마시기'])(
    'strips the opener in %s',
    (input) => expect((input as string).replace(KO_LEAD_FILLER, '').length)
      .toBeLessThan((input as string).length),
  )

  it.each(['물 마시기 알려 줘', '약 먹으라고 알려줘', '스트레칭 잊지 마', '운동 해줘'])(
    'strips the closing request in %s',
    (input) => expect((input as string).replace(KO_TRAIL_FILLER, '').length)
      .toBeLessThan((input as string).length),
  )

  it('leaves a sentence that is only the task alone', () => {
    expect('물 마시기'.replace(KO_TRAIL_FILLER, '').replace(KO_LEAD_FILLER, '')).toBe('물 마시기')
  })
})

describe('known limitation: a weekday repeat is refused, not mis-scheduled', () => {
  it('reports no interval for 매주 월요일', () => {
    // The Recurrence model is a single interval and cannot express a weekday, so
    // findInterval bails rather than firing weekly on the wrong day. Asserted as-is
    // so a future weekday feature updates this test on purpose.
    const r = parseKoParts('매주 월요일 팀 회의')
    expect(r.everyMinutes).toBeNull()
  })

  it('but a plain 매주 IS a weekly repeat', () => {
    expect(parseKoParts('매주 주간보고 쓰기').everyMinutes).toBe(10080)
  })
})

/**
 * The end-to-end path, including the reminder text that is actually saved.
 *
 * These are the phrasings the Korean UI itself offers as examples, so a regression
 * here means the app is once again teaching users a syntax it cannot read.
 */
describe('parseReminder resolves Korean end to end', () => {
  const now = new Date('2026-08-12T07:00:00Z') // 16:00 KST

  it('reads the first hint example', () => {
    const r = parseReminder('20분 뒤에 물 마시기', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.text).toBe('물 마시기')
    expect(r.recurrence).toBeNull()
    expect(new Date(r.fireAt!).getTime() - now.getTime()).toBe(20 * 60_000)
  })

  it('reads the second hint example', () => {
    const r = parseReminder('한 시간마다 일어서기', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.text).toBe('일어서기')
    expect(r.recurrence).toEqual({ everyMinutes: 60 })
  })

  it('keeps the task and drops the closing request verb', () => {
    const r = parseReminder('30분마다 물 마시기 알려 줘', now)
    expect(r.text).toBe('물 마시기')
    expect(r.recurrence).toEqual({ everyMinutes: 30 })
  })

  it('schedules a bare 내일 at the conventional morning hour', () => {
    const r = parseReminder('내일 우유 사기', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.text).toBe('우유 사기')
    expect(new Date(r.fireAt!).getHours()).toBe(9)
  })

  it('still asks when no time was given', () => {
    const r = parseReminder('우유 사기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
    expect(r.text).toBe('우유 사기')
  })

  it('does not let Korean input reach the Chinese rules', () => {
    // 시 is not Han, but a name or quotation can carry one; Hangul must win.
    const r = parseReminder('30분마다 漢字 공부', now)
    expect(r.recurrence).toEqual({ everyMinutes: 30 })
    expect(r.text).toBe('漢字 공부')
  })
})

/**
 * Inputs that must ASK rather than schedule something the user did not say.
 *
 * Each one previously produced a wrong reminder — a crash, a wrong time, or a saved
 * text with characters eaten out of it — which is the failure this parser's whole
 * refusal contract exists to avoid. Grouped together because they are one rule:
 * when a reading is not unambiguously a schedule, it is not a schedule.
 */
describe('ambiguous or unsupported input asks instead of guessing', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('refuses a duration too large to schedule instead of throwing', () => {
    // An out-of-range count reached `new Date` and `toISOString()` threw out of the
    // submit handler, so the add crashed rather than landing on a wrong time.
    const r = parseReminder('99999999999999999999분 뒤에 물 마시기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
    expect(r.text).toBe('99999999999999999999분 뒤에 물 마시기')
  })

  it.each(['1시간 운동', '3시간 공부', '2시간 산책'])(
    'does not read the 시 that opens 시간 in %s as an hour',
    (input) => {
      const r = parseReminder(input as string, now)
      expect(r.needsSchedule).toBe(true)
      expect(r.text).toBe(input)
    },
  )

  it('does not read 한시 inside 한시적으로 as one o’clock', () => {
    const r = parseReminder('한시적으로 알림 끄기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('한시적으로 알림 끄기')
  })

  it('refuses a weekday repeat outright rather than keeping just its clock', () => {
    // Dropping only the repeat left 09:00 standing, so a weekly request was saved as
    // a single one-time reminder.
    const r = parseReminder('매주 월요일 오전 9시에 팀 회의', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.recurrence).toBeNull()
    expect(r.text).toBe('매주 월요일 오전 9시에 팀 회의')
  })

  it('refuses a clock whose explicit minute is not a minute', () => {
    const r = parseReminder('9시 90분에 회의', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('9시 90분에 회의')
  })

  it('keeps the whole task when the delay marker is 이따가', () => {
    // The marker alternation matched only 이따, leaving 가 welded to the task.
    const r = parseReminder('30분 이따가 전화하기', now)
    expect(r.text).toBe('전화하기')
    expect(new Date(r.fireAt!).getTime() - now.getTime()).toBe(30 * 60_000)
  })

  it('still reads the phrasings that neighbour each refusal', () => {
    // The refusals above must not have cost the readings next to them.
    expect(parseKoParts('9시 30분에 회의').clock).toEqual({ hour: 9, minute: 30, explicit: false })
    expect(parseKoParts('8시 반에 아침 먹기').clock?.minute).toBe(30)
    expect(parseKoParts('2시간 간격으로 스트레칭').everyMinutes).toBe(120)
    expect(parseKoParts('매주 주간보고 쓰기').everyMinutes).toBe(10080)
    expect(parseKoParts('9시경에 회의').clock?.hour).toBe(9)
  })
})

/**
 * Schedule words that are also fragments of ordinary Korean words.
 *
 * Korean writes without inter-word boundaries a regex can lean on, so every token
 * this parser looks for — a day shorthand, a filler word, an hour, a repeat suffix —
 * also occurs INSIDE words the user typed. Each case below scheduled something and
 * ate characters out of the saved text.
 */
describe('a schedule word inside an ordinary word is not a schedule', () => {
  const now = new Date('2026-08-13T04:00:00Z')

  it('does not read the 낼 of 보낼 as tomorrow', () => {
    const r = parseReminder('보낼 이메일 정리하기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('보낼 이메일 정리하기')
  })

  it('still reads a standalone 낼', () => {
    const r = parseKoParts('낼 아침 회의')
    expect(r.dayOffset).toBe(1)
    expect(r.clock?.hour).toBe(9)
  })

  it('does not read the 일 of 월요일 as a day unit', () => {
    // 월요일마다 matched 일마다 and became a DAILY reminder with the text cut to 월요.
    const r = parseReminder('월요일마다 오전 9시에 팀 회의', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.recurrence).toBeNull()
    expect(r.text).toBe('월요일마다 오전 9시에 팀 회의')
  })

  it('does not strip the 좀 of 좀비', () => {
    const r = parseReminder('20분 뒤에 좀비 영화 보기', now)
    expect(r.text).toBe('좀비 영화 보기')
    expect(new Date(r.fireAt!).getTime() - now.getTime()).toBe(20 * 60_000)
  })

  it('does not read 일시 as one o’clock', () => {
    // An hour is written in digits or a native counter, never a bare sino digit.
    const r = parseReminder('일시 중단 해제', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('일시 중단 해제')
  })
})

describe('밤 12시 is midnight, not noon', () => {
  it('resolves 밤 12시 to hour 0', () => {
    expect(parseKoParts('내일 밤 12시에 약 먹기').clock?.hour).toBe(0)
  })

  it('leaves the other night hours in the evening', () => {
    expect(parseKoParts('밤 11시에 스트레칭').clock?.hour).toBe(23)
    expect(parseKoParts('밤 9시에 산책').clock?.hour).toBe(21)
  })

  it('keeps 오후 12시 at noon', () => {
    // The pm rule is right for 오후 and wrong for 밤, which is why they differ.
    expect(parseKoParts('오후 12시에 점심').clock?.hour).toBe(12)
  })
})
