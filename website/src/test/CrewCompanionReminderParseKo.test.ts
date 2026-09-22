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

  it('does not read 반 as an hour: 반시 is not a clock', () => {
    // 반 is the minute word after 시 (9시 반) and a half for durations (반 시간),
    // but Korean has no half o'clock — reading 반 as an hour turned 반시 주문하기
    // into a reminder near midnight instead of leaving the text alone.
    const r = parseReminder('반시 주문하기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
    expect(r.text).toBe('반시 주문하기')
  })

  it('reads 밤 0시 as midnight, not noon', () => {
    // The night meridiem's default branch added 12 to every hour below 12, so
    // hour 0 — already an explicit midnight — became 12:00 and 밤 0시에 서버 점검
    // persisted a noon reminder.
    expect(parseKoParts('밤 0시에 서버 점검').clock).toMatchObject({ hour: 0, minute: 0 })
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

  it('keeps the small 밤 hours in the early morning', () => {
    // 밤 1시 is 1 AM (past midnight), not 13:00 — adding 12 to every hour below
    // 12 turned a late-night reminder into an afternoon one.
    expect(parseKoParts('밤 1시에 서버 확인').clock?.hour).toBe(1)
    expect(parseKoParts('밤 2시 반에 배치 확인').clock).toMatchObject({ hour: 2, minute: 30 })
    expect(parseKoParts('밤 5시에 알람').clock?.hour).toBe(5)
    // 6 and up are the evening reading.
    expect(parseKoParts('밤 6시에 저녁 준비').clock?.hour).toBe(18)
  })
})

describe('clock readings never start inside an ordinary Hangul word', () => {
  it('does not read 한시 out of 제한시', () => {
    const r = parseReminder('제한시에 알림 끄기', new Date(2026, 7, 26, 10, 0, 0))
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('제한시에 알림 끄기')
  })

  it('still reads a real clock later in the same sentence', () => {
    // The mid-word candidate must be skipped, not abort the whole scan.
    const r = parseKoParts('제한시간 끝나면 내일 3시에 회의')
    expect(r.clock?.hour).toBe(3)
  })

  it('still reads a day part fused to a day word', () => {
    expect(parseKoParts('오늘밤 11시 스트레칭').clock?.hour).toBe(23)
  })
})

describe('a bare out-of-range hour poisons the parse', () => {
  const at = new Date(2026, 7, 26, 10, 0, 0)

  it('asks for 내일 25시 회의 instead of defaulting to tomorrow 09:00', () => {
    // The user plainly tried to state a clock; silently scheduling the day-only
    // default is a time they never gave.
    const r = parseReminder('내일 25시 회의', at)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('내일 25시 회의')
  })

  it('catches a broken clock that is not the first 시 reading', () => {
    const r = parseReminder('오후 3시 아니고 25시에 점검', at)
    expect(r.needsSchedule).toBe(true)
  })

  it('still reads a valid bare hour', () => {
    expect(parseKoParts('23시에 백업').clock?.hour).toBe(23)
  })
})

describe('known limitation: calendar dates and one-time weekdays are refused, not mis-scheduled', () => {
  // Dropping only the date would leave the clock standing, so 12월 25일 오전 9시 was
  // persisted for the NEXT 09:00 instead of December 25 — a reminder on a day the
  // user never named. Refused wholesale, the same disposition 매주 월요일 has.
  it.each([
    '12월 25일 오전 9시 송년회',
    '1월 1일 새해 인사 보내기',
    '12/25 오전 9시 송년회',
    '12.25 오전 9시 송년회',
    '월요일 오전 9시 팀 회의',
    '다음 주 화요일 3시 병원 예약',
    '금욜 저녁 회식',
  ])('refuses %s wholesale', (input) => {
    const r = parseKoParts(input as string)
    expect(r.hasSignal).toBe(false)
    expect(r.everyMinutes).toBeNull()
    expect(r.clock).toBeNull()
  })

  it('keeps the full text so the ask shows the user their own words', () => {
    const now = new Date('2026-08-12T07:00:00Z')
    const r = parseReminder('12월 25일 오전 9시 송년회', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('12월 25일 오전 9시 송년회')
  })
})

describe('a malformed colon time asks instead of matching a substring', () => {
  it('does not read 12:30 out of 012:30', () => {
    // Unanchored, the clock regex matched 12:30 inside the digit run and persisted
    // a reminder at 12:30 with a stray 0 left in the saved text.
    const r = parseKoParts('012:30 회의')
    expect(r.clock).toBeNull()
    expect(r.hasSignal).toBe(false)
  })

  it('does not read 12:30 out of 12:305', () => {
    expect(parseKoParts('12:305 회의').clock).toBeNull()
  })

  it('still reads a clean colon time', () => {
    const r = parseKoParts('12:30 회의')
    expect(r.clock).toEqual({ hour: 12, minute: 30, explicit: false })
  })
})

describe('오늘 is a day only when it stands alone', () => {
  it('does not eat the 오늘 of 오늘날', () => {
    const now = new Date('2026-08-12T07:00:00Z')
    const r = parseReminder('오늘날 역사 공부하기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('오늘날 역사 공부하기')
  })

  it('still reads 오늘밤 written without a space', () => {
    expect(parseKoParts('오늘밤 9시 약 먹기').clock?.hour).toBe(21)
  })
})

describe('an explicit 오늘 whose clock has passed is refused, not rolled to tomorrow', () => {
  // Built from local hours so the assertion holds in any timezone the suite runs in.
  const at = (h: number) => { const d = new Date(); d.setHours(h, 0, 0, 0); return d }

  it('refuses 오늘 오후 3시 at five in the afternoon', () => {
    // The rollover rule is right for a bare 3시 and wrong for 오늘 3시: moving the
    // reminder to tomorrow contradicts the day the user explicitly named.
    const r = parseReminder('오늘 오후 3시 회의', at(17))
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
    // The ask echoes the user's own time words — stripping them would make the
    // "when?" question imply no time was given.
    expect(r.text).toBe('오늘 오후 3시 회의')
  })

  it('schedules 오늘 오후 3시 at ten in the morning for 15:00 today', () => {
    const r = parseReminder('오늘 오후 3시 회의', at(10))
    expect(r.needsSchedule).toBe(false)
    const d = new Date(r.fireAt!)
    expect(d.getHours()).toBe(15)
    expect(d.getDate()).toBe(at(10).getDate())
  })

  it('still tries the afternoon reading of an ambiguous 오늘 3시', () => {
    const r = parseReminder('오늘 3시 회의', at(10))
    expect(new Date(r.fireAt!).getHours()).toBe(15)
  })
})

describe('매주 followed by an ordinary 말- word is a plain weekly repeat', () => {
  it('reads 매주 말하기 연습 as weekly', () => {
    // The bare 말 alternative matched the 말 of 말하기 and refused a supported shape.
    expect(parseKoParts('매주 말하기 연습').everyMinutes).toBe(10080)
  })

  it('still refuses 매주말 as a weekend repeat', () => {
    expect(parseKoParts('매주말 대청소').everyMinutes).toBeNull()
  })
})

describe('the quotative 라고 stays welded to its verb stem', () => {
  it('keeps 약 먹으라고 whole', () => {
    // Bare 라고 matched inside 먹으라고 and truncated the saved text to 약 먹으.
    expect('약 먹으라고 알려줘'.replace(KO_TRAIL_FILLER, '')).toBe('약 먹으라고')
  })

  it('still strips a complete 하라고 request', () => {
    expect('운동하라고 알려줘'.replace(KO_TRAIL_FILLER, '')).toBe('운동')
  })
})

describe('day tokens are days only when they stand alone', () => {
  it('does not eat the 모레 of 아모레퍼시픽', () => {
    // The +2-day row was unbounded, so a company name was scheduled two days out
    // and saved with its middle syllables cut.
    const now = new Date('2026-08-12T07:00:00Z')
    const r = parseReminder('아모레퍼시픽 보고서 9시', now)
    expect(r.text).toBe('아모레퍼시픽 보고서')
    const p = parseKoParts('아모레퍼시픽 보고서 9시')
    expect(p.dayOffset).toBe(0)
    expect(p.dayExplicit).toBe(false)
  })

  it.each([
    ['내일모레 발표 준비', 2],
    ['모레쯤에 전화하기', 2],
    ['내일까지 보고서 내기', 1],
  ])('still reads %s with offset %i', (input, offset) => {
    const p = parseKoParts(input as string)
    expect(p.dayOffset).toBe(offset)
    expect(p.dayExplicit).toBe(true)
  })
})

describe('깨워 is a task verb unless the request ending makes it filler', () => {
  it('keeps 아기 깨워 whole', () => {
    // The bare 깨워 alternative stripped the action and saved only 아기.
    expect('아기 깨워'.replace(KO_TRAIL_FILLER, '')).toBe('아기 깨워')
  })

  it('still strips an explicit 깨워 줘 request', () => {
    expect('아침에 깨워 줘'.replace(KO_TRAIL_FILLER, '')).toBe('아침에')
  })
})

describe('interval readings never start inside ordinary words', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it.each([
    '구분마다 색상 확인하기',
    '구매일마다 재고 확인',
    '휴일마다 산책',
  ])('%s is not a repeat', (input) => {
    const r = parseReminder(input as string, now)
    expect(r.recurrence).toBeNull()
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe(input)
  })

  it('still reads a compound sino count', () => {
    expect(parseKoParts('십분마다 스트레칭').everyMinutes).toBe(10)
  })
})

describe('a schedule token never reads out of the HEAD of an ordinary word', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  // The right-boundary mirror of "interval readings never start inside ordinary
  // words". A token that ENDS in Hangul (매일, the delay markers 뒤/후, the repeat
  // suffixes) must match as a complete word: 매일 opens 매일경제 (a newspaper) and 후
  // opens 후원하기 ("to sponsor"). Four blocking review findings across three rounds
  // were all this one missing invariant, so this corpus stands guard over the whole
  // class rather than the reported words one at a time.
  it.each([
    '매일경제 구독하기', // 매일 (every day) heads 매일경제
    '매일신문 읽기', // same, other paper
    '한 시간 후원하기', // 후 (after) heads 후원하기
    '후원금 정리', // 후 heads 후원금
    '뒤풀이 준비하기', // 뒤 (after) heads 뒤풀이
    '뒤편 정리하기', // 뒤 heads 뒤편
    '두 시간 후배 만나기', // 후 heads 후배
  ])('%s is not a schedule', (input) => {
    const r = parseReminder(input as string, now)
    expect(r.recurrence).toBeNull()
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe(input)
  })

  // The neighbours the bound must NOT break: the same tokens ARE a schedule when
  // they end at a real word boundary — a space, or the time particle the phrase
  // takes (뒤에, 후에).
  it('still reads 매일 as a daily repeat before a space', () => {
    expect(parseKoParts('매일 물 마시기').everyMinutes).toBe(1440)
  })

  it.each([
    ['한 시간 후 물 마시기', 60],
    ['한 시간 후에 물 마시기', 60],
    ['20분 뒤에 물 마시기', 20],
    ['이틀 뒤 청소', 2 * 1440],
    ['한 시간 지나서 알림', 60],
  ])('still reads the delay in %s', (input, minutes) => {
    expect(parseKoParts(input as string).delayMinutes).toBe(minutes)
  })
})

describe('unsupported relative-day names are refused, not half-scheduled', () => {
  it.each(['주말 아침에 청소', '다음 주에 보고서 제출', '이번 주 회의 준비', '주말에 등산'])(
    'refuses %s wholesale',
    (input) => {
      const p = parseKoParts(input as string)
      expect(p.hasSignal).toBe(false)
      expect(p.clock).toBeNull()
    },
  )

  it('주말 inside a compound noun is not a weekend', () => {
    // 주말 opens ordinary compounds — 주말농장 ("weekend farm") is the NAME of the
    // place, not a date, and refusing it took away a plain next-day clock.
    expect(parseKoParts('주말농장 청소 내일 오후 3시').clock).toMatchObject({ hour: 15 })
  })
})

describe('a broken clock poisons the whole parse', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it.each(['내일 012:30 회의', '내일 9시 90분 회의', '내일 25:00 출발'])(
    '%s asks instead of defaulting the named day to 09:00',
    (input) => {
      const r = parseReminder(input as string, now)
      expect(r.needsSchedule).toBe(true)
      expect(r.fireAt).toBeNull()
      expect(r.text).toBe(input)
    },
  )

  it('still schedules a well-formed clock on a named day', () => {
    const r = parseReminder('내일 12:30 회의', now)
    expect(r.needsSchedule).toBe(false)
  })

  it('poisons a suffixless minute that is not a minute', () => {
    // The reader refuses 9시 90 (90 cannot be a minute), so without the guard
    // agreeing, 내일 stood alone and the reminder silently fired at the 09:00
    // day default — a time the user never gave.
    const r = parseReminder('내일 9시 90 회의', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
    expect(r.text).toBe('내일 9시 90 회의')
  })
})

describe('prefixed repeats absorb their suffix; multiple clocks refuse', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('매 30분마다 does not strand 마다 in the saved text', () => {
    const r = parseReminder('매 30분마다 물 마시기', now)
    expect(r.recurrence).toEqual({ everyMinutes: 30 })
    expect(r.text).toBe('물 마시기')
  })

  it('매일 아침마다 does not strand 마다 in the saved text', () => {
    // The day-part repeat's span stopped before a trailing 마다, so the daily
    // reminder was saved with 마다 welded to the front of the task.
    const r = parseReminder('매일 아침마다 약 먹기', now)
    expect(r.recurrence).toEqual({ everyMinutes: 24 * 60 })
    expect(r.text).toBe('약 먹기')
  })

  it('매 이틀마다 does not strand 매 in the saved text', () => {
    // The fused-day span started at 이틀, leaving a leading 매 in the saved text.
    const r = parseReminder('매 이틀마다 청소', now)
    expect(r.recurrence).toEqual({ everyMinutes: 2 * 24 * 60 })
    expect(r.text).toBe('청소')
  })

  it('매 30분 without the suffix still repeats', () => {
    const r = parseReminder('매 30분 스트레칭', now)
    expect(r.recurrence).toEqual({ everyMinutes: 30 })
    expect(r.text).toBe('스트레칭')
  })

  it('the suffix never bites the first syllable of a following word', () => {
    const r = parseReminder('매 30분 씩씩하게 걷기', now)
    expect(r.recurrence).toEqual({ everyMinutes: 30 })
    expect(r.text).toBe('씩씩하게 걷기')
  })

  it('two clock phrases refuse rather than half-schedule', () => {
    // 3시와 fails the clock tail and was SKIPPED, so 5시에 read alone: the
    // reminder persisted at 05:00 with the corrupted text 3시와 회의.
    const r = parseReminder('내일 3시와 5시에 회의', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
  })

  it('a single clock still reads', () => {
    const r = parseReminder('내일 오후 5시에 회의', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.text).toBe('회의')
  })
})

describe('multiple day-part mentions refuse rather than collapse', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('아침과 저녁에 refuses instead of persisting one 19:00 reminder', () => {
    // The day-part fallback kept only 저녁에: one 19:00 reminder named 아침과 약 먹기.
    const r = parseReminder('아침과 저녁에 약 먹기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
  })

  it('refuses with a day token present rather than falling to its default', () => {
    const r = parseReminder('내일 아침과 저녁에 약 먹기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
  })

  it('a single day part still reads', () => {
    const r = parseReminder('저녁에 약 먹기', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.text).toBe('약 먹기')
  })

  it('아침 as breakfast is not a time mention', () => {
    const r = parseReminder('아침 먹고 저녁에 약 먹기', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.text).toBe('아침 먹고 약 먹기')
  })

  it('a day part fused to its clock is one mention, not two', () => {
    const r = parseReminder('내일 아침 9시에 회의', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.text).toBe('회의')
  })
})

describe('a day part at the end of a noun is not a time', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('does not read the 밤 of 군밤 as tonight', () => {
    // 군밤 ("roasted chestnuts") ends the input, and the end-of-input branch of
    // the schedule-position rule did not check what sat BEFORE the day part —
    // so 밤 scheduled 20:00 and the saved text was corrupted to 군.
    const r = parseReminder('군밤', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
    expect(r.text).toBe('군밤')
  })

  it('does not read the 밤 of 군밤에 as tonight either', () => {
    // The particle branch had the same gap: 에 after the noun-final 밤 read as a
    // time particle, corrupting the text to 군 대해 검색하기.
    const r = parseReminder('군밤에 대해 검색하기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('군밤에 대해 검색하기')
  })

  it('still reads a fused day marker before the day part', () => {
    // The mid-word rejection must keep its one exception: 오늘밤 is one fused
    // time phrase, not a noun that happens to end in 밤.
    expect(parseKoParts('오늘밤 9시 약 먹기').clock?.hour).toBe(21)
    expect(parseKoParts('내일 밤에 스트레칭').clock?.hour).toBe(20)
  })
})

describe('an unambiguous 시 hour states its own meridiem', () => {
  it('reads 0시 as explicit midnight', () => {
    // 0시 left `explicit` false, so after midnight the ambiguous-hour resolution
    // tried twelve hours later and persisted the reminder for NOON.
    expect(parseKoParts('0시에 서버 점검').clock).toEqual({ hour: 0, minute: 0, explicit: true })
  })

  it('reads a 24-hour 시 reading as explicit', () => {
    // 14시 states its meridiem by being 24-hour, exactly like 14:00 — only the
    // colon branch knew that.
    expect(parseKoParts('14시에 회의').clock).toEqual({ hour: 14, minute: 0, explicit: true })
  })

  it('keeps a bare 1-12 hour ambiguous', () => {
    expect(parseKoParts('9시에 회의').clock?.explicit).toBe(false)
  })
})

describe('a daily day part swallows its trailing particle', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('does not leave 에 behind as the reminder text', () => {
    // The daily branch blanked only 매일 저녁, so the reminder was named 에 약 먹기.
    const r = parseReminder('매일 저녁에 약 먹기', now)
    expect(r.text).toBe('약 먹기')
    expect(r.recurrence?.everyMinutes).toBe(24 * 60)
  })
})

describe('compound time particles are absorbed whole', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('does not strand the 는 of 뒤에는 in the saved text', () => {
    // The particle rule matched only the bare particle, so absorbing 에 out of
    // 에는 left 는 welded to the front of the reminder: 는 물 마시기.
    const r = parseReminder('20분 뒤에는 물 마시기', now)
    expect(r.text).toBe('물 마시기')
    expect(new Date(r.fireAt!).getTime() - now.getTime()).toBe(20 * 60_000)
  })

  it('absorbs 에는 and 에도 after a day part', () => {
    const evening = parseReminder('저녁에는 약 먹기', now)
    expect(evening.text).toBe('약 먹기')
    expect(parseKoParts('저녁에도 스트레칭').clock?.hour).toBe(19)
  })

  it('absorbs 까지는 after a day token', () => {
    const r = parseReminder('내일까지는 보고서 내기', now)
    expect(r.text).toBe('보고서 내기')
  })
})

describe('weekday-group and year-qualified dates are refused, not half-scheduled', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('refuses 매주 평일 rather than persisting a plain weekly repeat', () => {
    // 평일 ("weekdays") names a day group the interval model cannot express, the
    // same way 주말 does — dropping it persisted a weekly reminder firing on the
    // wrong days.
    const r = parseReminder('매주 평일 오전 9시에 팀 회의', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.recurrence).toBeNull()
    expect(r.text).toBe('매주 평일 오전 9시에 팀 회의')
  })

  it('refuses 주중 the same way', () => {
    const r = parseReminder('주중 아침 운동', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('주중 아침 운동')
  })

  it('refuses a year-qualified date rather than scheduling the next 09:00', () => {
    // 2026-12-25 오전 9시 named a calendar day the model cannot express; only the
    // month/day and MM/DD spellings were refused, so the date was dropped and the
    // reminder persisted for the NEXT 09:00 instead of December 25.
    const r = parseReminder('2026-12-25 오전 9시 파티', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('2026-12-25 오전 9시 파티')
    const dotted = parseReminder('2026.12.25 오전 9시 파티', now)
    expect(dotted.needsSchedule).toBe(true)
  })

  it('still reads the shapes that neighbour these refusals', () => {
    expect(parseKoParts('매주 주간보고 쓰기').everyMinutes).toBe(10080)
    // A version-like digit run is not a date.
    expect(parseKoParts('1.2.3 릴리스 노트 쓰기 내일 9시').clock?.hour).toBe(9)
  })
})

describe('a leading recipient is task text unless the sentence is a request', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('keeps 저에게 when there is no request framing', () => {
    const r = parseReminder('내일 저에게 온 메일 확인하기', now)
    expect(r.text).toBe('저에게 온 메일 확인하기')
  })

  it('still strips 나에게 from a framed request', () => {
    const r = parseReminder('나에게 물 마시라고 알려줘', now)
    expect(r.text).toBe('물 마시라고')
  })
})

describe('request verbs are filler only with an explicit request ending', () => {
  it('keeps 팀에 결과 말해 whole', () => {
    expect('팀에 결과 말해'.replace(KO_TRAIL_FILLER, '')).toBe('팀에 결과 말해')
  })

  it.each(['결과 말해 줘', '일정 기억해 줘', '물 마시기 알려 주세요'])(
    'still strips the framed request in %s',
    (input) => expect((input as string).replace(KO_TRAIL_FILLER, '').length)
      .toBeLessThan((input as string).length),
  )
})

describe('a night reading in the small hours rolls to the next day', () => {
  // now = Wed 2026-08-12 07:00Z. 밤 12시 / 밤 1시–5시 map to 00:00–05:59, which
  // belong to the day AFTER the evening named — 내일 밤 12시 is the midnight that
  // ENDS tomorrow, not the one that starts it (which was one day early).
  const now = new Date('2026-08-12T07:00:00Z')

  it('reads 내일 밤 12시 as the midnight ending tomorrow', () => {
    const r = parseReminder('내일 밤 12시에 서버 점검', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-14T00:00:00.000Z')
    expect(r.text).toBe('서버 점검')
  })

  it('reads 내일 밤 2시 as the small hours after tomorrow night', () => {
    expect(parseReminder('내일 밤 2시 알람', now).fireAt).toBe('2026-08-14T02:00:00.000Z')
  })

  it('reads 오늘 밤 12시 as the coming midnight, not the passed one', () => {
    expect(parseReminder('오늘 밤 12시 점검', now).fireAt).toBe('2026-08-13T00:00:00.000Z')
  })

  it('does NOT roll an evening night hour', () => {
    // 밤 8시 = 20:00 is the evening of the named day, not the small hours.
    expect(parseReminder('내일 밤 8시 회의', now).fireAt).toBe('2026-08-13T20:00:00.000Z')
  })

  it('does NOT double-roll a bare 밤 12시', () => {
    // No day named: the next-occurrence rule already picks the coming midnight.
    expect(parseReminder('밤 12시 점검', now).fireAt).toBe('2026-08-13T00:00:00.000Z')
  })
})

describe('a repeat span absorbs its trailing particle and 씩', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('does not strand 씩 from a rate', () => {
    // 하루에 세 번씩 약 먹기 -> "씩 약 먹기" before the fix.
    const r = parseReminder('하루에 세 번씩 약 먹기', now)
    expect(r.recurrence?.everyMinutes).toBe(480) // 1440 / 3
    expect(r.text).toBe('약 먹기')
  })

  it('does not strand 씩 from a per-hour rate', () => {
    const r = parseReminder('한 시간에 두 번씩 스트레칭', now)
    expect(r.recurrence?.everyMinutes).toBe(30)
    expect(r.text).toBe('스트레칭')
  })

  it('does not strand the 에 particle from a prefixed interval', () => {
    // 약을 매 시간에 먹기 -> "약을 에 먹기" before the fix.
    const r = parseReminder('약을 매 시간에 먹기', now)
    expect(r.recurrence?.everyMinutes).toBe(60)
    expect(r.text).toBe('약을 먹기')
  })

  it('still reads a plain rate with no 씩', () => {
    expect(parseReminder('하루에 세 번 약 먹기', now).recurrence?.everyMinutes).toBe(480)
  })
})

describe('a clock survives a noun-final day-part prefix', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('reads 밤 10시 out of 내일 군밤 10시에 사기, keeping 군밤', () => {
    // The 밤 of 군밤 is mid-word so it is not a meridiem, but 10시 is still the
    // user's stated clock — before the fix scanClocks skipped 밤 10시 entirely and
    // only 내일 survived, persisting the 09:00 day default.
    const r = parseReminder('내일 군밤 10시에 사기', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-13T10:00:00.000Z')
    expect(r.text).toBe('군밤 사기')
  })

  it('reads 10시 out of a bare 군밤 10시에 사기', () => {
    const r = parseReminder('군밤 10시에 사기', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.text).toBe('군밤 사기')
  })

  it('still skips a clock whose hour itself is mid-word', () => {
    // 제한시 — the hour is glued to Hangul, so there is no clock to rescue.
    const r = parseReminder('제한시에 알림 끄기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('제한시에 알림 끄기')
  })

  it('still reads a fused day-marker prefix (오늘밤 9시)', () => {
    expect(parseReminder('오늘밤 9시에 회의', now).fireAt).toBe('2026-08-12T21:00:00.000Z')
  })
})

describe('a valid day-part prefix anchors a fused clock', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('reads 저녁8시 fused directly onto the day part', () => {
    // 저녁 is a whole word here, so it establishes the boundary and the 8 gluing
    // onto it is a real clock — before the fix the hour mid-word check saw 8 after
    // Hangul 녁 and dropped the whole candidate, falling back to the 19:00 default
    // and stranding "8시 회의".
    const r = parseReminder('내일 저녁8시 회의', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-13T20:00:00.000Z')
    expect(r.text).toBe('회의')
  })

  it('still reads the spaced form 저녁 8시', () => {
    const r = parseReminder('내일 저녁 8시 회의', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-13T20:00:00.000Z')
    expect(r.text).toBe('회의')
  })

  it('does not treat the 밤 of 군밤 as a boundary-establishing prefix', () => {
    // 군밤 10시 must still drop the mid-word 밤 and read 10시 alone, keeping 군밤.
    const r = parseReminder('내일 군밤 10시에 사기', now)
    expect(r.fireAt).toBe('2026-08-13T10:00:00.000Z')
    expect(r.text).toBe('군밤 사기')
  })
})

describe('an optional minute does not eat the trailing space', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('reads 9시 30 as 09:30 when 분 is absent', () => {
    // Before the fix the \s* before the optional 분 ate the trailing space, so the
    // match was "9시 30 "; clockTail then saw 회 and rejected the whole clock,
    // persisting the 09:00 day default with the corrupt text "9시 30 회의".
    const r = parseReminder('내일 9시 30 회의', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-13T09:30:00.000Z')
    expect(r.text).toBe('회의')
  })

  it('still reads the explicit 9시 30분', () => {
    const r = parseReminder('내일 9시 30분 회의', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-13T09:30:00.000Z')
    expect(r.text).toBe('회의')
  })

  it('refuses 9시 30초 rather than reading a bogus minute', () => {
    // 초 (seconds) is not a minute suffix; the clock must ASK, not become 09:30.
    const r = parseReminder('9시 30초 알림', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('9시 30초 알림')
  })
})

describe('a broken clock refuses even when a day is named', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('refuses 내일 9시 30초 알림 instead of falling back to the day default', () => {
    // 9시 30 is a well-formed clock MENTION, but the 초 tail makes it unreadable.
    // Before the fix scanClocks dropped it without marking it broken, so with a
    // named day (내일) the parse fell through to the 09:00 default and persisted the
    // corrupt text "9시 30초 알림". A counted mention that fails its tail is broken,
    // so the whole parse must ASK.
    const r = parseReminder('내일 9시 30초 알림', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
    expect(r.text).toBe('내일 9시 30초 알림')
  })

  it('still reads 내일 9시 30 회의 as 09:30', () => {
    const r = parseReminder('내일 9시 30 회의', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-13T09:30:00.000Z')
    expect(r.text).toBe('회의')
  })

  it('still reads 내일 9시 30분 회의 as 09:30', () => {
    const r = parseReminder('내일 9시 30분 회의', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-13T09:30:00.000Z')
    expect(r.text).toBe('회의')
  })
})

describe('점심 is an afternoon word and 정오 is strictly noon', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('reads 점심 1시 as 13:00, not 01:00', () => {
    // 점심 ("lunchtime") is pm: a 1–11 clock beside it is afternoon. Before the fix
    // 점심 carried the `noon` meridiem, which shiftMeridiem never shifted, so
    // 점심 1시 persisted 01:00 — a wrong time the phrase does not name.
    const r = parseReminder('점심 1시에 밥 먹기', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-12T13:00:00.000Z')
    expect(r.text).toBe('밥 먹기')
  })

  it('reads 점심 3시 as 15:00', () => {
    const r = parseReminder('점심 3시에 회의', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-12T15:00:00.000Z')
    expect(r.text).toBe('회의')
  })

  it('leaves a stated 점심 12시 at 12:00', () => {
    const r = parseReminder('점심 12시에 밥 먹기', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-12T12:00:00.000Z')
    expect(r.text).toBe('밥 먹기')
  })

  it('refuses 정오 1시 — a clock other than 12 contradicts strict noon', () => {
    // 정오 is exactly 12:00; 정오 1시 is a contradiction. Before the fix the raw
    // hour was silently kept (01:00). It must ASK instead of persisting a wrong time.
    const r = parseReminder('정오 1시에 밥 먹기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
    expect(r.text).toBe('정오 1시에 밥 먹기')
  })

  it('still reads a bare 정오 as 12:00', () => {
    const r = parseReminder('정오에 밥 먹기', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-12T12:00:00.000Z')
    expect(r.text).toBe('밥 먹기')
  })

  it('refuses 정오 3:00 in the colon form too', () => {
    const r = parseReminder('정오 3:00에 회의', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
  })
})

describe('a leading 나 is stripped only with its dative particle', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('still strips 나에게 when the input frames a request', () => {
    const r = parseReminder('나에게 물 마시라고 알려줘', now)
    expect(r.text).toBe('물 마시라고')
  })

  it('keeps a bare 나 that opens the task', () => {
    // 나 대신 ("in my place") is part of the task, not a recipient. Before the fix
    // KO_LEAD_RECIPIENT stripped a bare 나, saving the corrupt text "대신 회의 참석".
    const r = parseReminder('나 대신 내일 회의 참석하라고 알려줘', now)
    expect(r.text).toBe('나 대신 회의 참석')
  })
})

describe('a day marker does not read a day part out of the head of a noun', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('refuses 오늘 낮잠 자기 — 낮 is the head of 낮잠 (a nap), not a time', () => {
    // Before the fix inSchedulePosition accepted 낮 whenever a day marker preceded
    // it, without requiring the day part to END at a boundary: 오늘 낮잠 자기 read 낮
    // as 13:00 and saved the corrupt text "잠 자기".
    const r = parseReminder('오늘 낮잠 자기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.fireAt).toBeNull()
    expect(r.text).toBe('낮잠 자기')
  })

  it('refuses 오늘 아침밥 먹기 — 아침 is the head of 아침밥 (breakfast)', () => {
    const r = parseReminder('오늘 아침밥 먹기', now)
    expect(r.needsSchedule).toBe(true)
    expect(r.text).toBe('아침밥 먹기')
  })

  it('keeps 밤나무 whole under 내일 (no time, saved intact)', () => {
    // 밤 is the head of 밤나무 (a chestnut tree). The day part must not be read,
    // but 내일 still gives the named day's default with the full text preserved.
    const r = parseReminder('내일 밤나무 심기', now)
    expect(r.text).toBe('밤나무 심기')
    expect(r.fireAt).toBe('2026-08-13T09:00:00.000Z')
  })

  it('still reads 오늘 낮 산책 as 13:00', () => {
    const r = parseReminder('오늘 낮 산책', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-12T13:00:00.000Z')
    expect(r.text).toBe('산책')
  })

  it('still reads 오늘 낮에 산책 (particle) as 13:00', () => {
    const r = parseReminder('오늘 낮에 산책', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-12T13:00:00.000Z')
    expect(r.text).toBe('산책')
  })

  it('still reads 내일 밤 운동 as 20:00', () => {
    const r = parseReminder('내일 밤 운동', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-13T20:00:00.000Z')
    expect(r.text).toBe('운동')
  })
})

describe('a daily repeat does not read a day part out of the head of a noun', () => {
  const now = new Date('2026-08-12T07:00:00Z')

  it('keeps 매일 낮잠 자기 whole — 낮 is the head of 낮잠', () => {
    // The 매일+day-part interval branch matched 매일 낮 without a WORD_END boundary,
    // so 매일 낮잠 자기 became a 13:00 daily repeat named "잠 자기". The day part now
    // must end at a boundary or a repeat suffix.
    const r = parseReminder('매일 낮잠 자기', now)
    expect(r.text).toBe('낮잠 자기')
  })

  it('still reads 매일 낮 산책 as a 13:00 daily repeat', () => {
    const r = parseReminder('매일 낮 산책', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-12T13:00:00.000Z')
    expect(r.text).toBe('산책')
  })

  it('still reads 매일 저녁에 산책 (particle) as a 19:00 daily repeat', () => {
    const r = parseReminder('매일 저녁에 산책', now)
    expect(r.needsSchedule).toBe(false)
    expect(r.fireAt).toBe('2026-08-12T19:00:00.000Z')
    expect(r.text).toBe('산책')
  })
})
