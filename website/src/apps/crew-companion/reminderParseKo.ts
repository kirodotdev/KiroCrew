/**
 * Korean natural-language reminder parsing — pure and separately testable.
 *
 * A separate module rather than more branches in reminderParse.ts, for the same
 * reason the Chinese rules live apart: the English patterns lean on `\b`, and `\b`
 * does not match between a Hangul syllable and a digit or another syllable, so
 * reusing them fails as "no schedule found" rather than as a visible bug. Korean
 * also puts the marker AFTER the quantity (20분 뒤, not "after 20 minutes"), marks
 * the repeat with a suffix (30분마다), and folds the meridiem into a separate word
 * (오후 3시 = 15:00).
 *
 * Scope matches the other two languages: the common shapes, and an honest refusal
 * otherwise. Weekday repeats (매주 월요일) and calendar dates (8월 5일) are out —
 * the Recurrence model is a single interval and cannot express either, so they are
 * refused rather than mis-scheduled.
 */

import { assembleParts } from './reminderParseParts'
import type { ClockHit, DayOffsetHit, DelayHit, IntervalHit, ScheduleParts, Span } from './reminderParseParts'

const MIN = 1
const HOUR = 60
const DAY = 1440
const WEEK = 10080

/**
 * Does this look like Korean input at all?
 *
 * Covers precomposed syllables plus the compatibility and conjoining Jamo blocks,
 * so a half-composed IME string still routes here instead of falling through to the
 * English parser, which would find nothing.
 */
export function hasHangul(s: string): boolean {
  return /[\uac00-\ud7a3\u3130-\u318f\u1100-\u11ff]/.test(s)
}

/** Native Korean counters, which is what people use with 시간 / 시 / 번. */
const KO_NATIVE: Record<string, number> = {
  하나: 1, 한: 1, 둘: 2, 두: 2, 셋: 3, 세: 3, 넷: 4, 네: 4,
  다섯: 5, 여섯: 6, 일곱: 7, 여덟: 8, 아홉: 9, 열: 10, 열다섯: 15,
  스무: 20, 스물: 20, 서른: 30, 마흔: 40, 쉰: 50, 예순: 60, 반: 0.5,
}

/** Sino-Korean digits, which is what people use with 분 and with a bare 시. */
const KO_SINO: Record<string, number> = {
  영: 0, 공: 0, 일: 1, 이: 2, 삼: 3, 사: 4, 오: 5, 육: 6, 칠: 7, 팔: 8, 구: 9,
}

/**
 * A Korean or Arabic number.
 *
 * Handles the two counting systems people mix freely in reminders, plus the 십
 * compounds that cover every interval anyone types in words (십오, 삼십, 사십오).
 * Bigger constructions (백이십) are out of scope: an interval that long is written
 * with digits in practice.
 */
export function koNumber(raw: string): number | null {
  const s = raw.trim()
  if (!s) return null
  if (/^\d+$/.test(s)) return parseInt(s, 10)
  if (s in KO_NATIVE) return KO_NATIVE[s]

  if (s.includes('십')) {
    const [tensPart, onesPart] = s.split('십')
    const tens = tensPart === '' ? 1 : KO_SINO[tensPart]
    const ones = onesPart === '' || onesPart === undefined ? 0 : KO_SINO[onesPart]
    if (tens == null || ones == null) return null
    return tens * 10 + ones
  }

  if (s.length === 1) return KO_SINO[s] ?? null
  return null
}

/**
 * Number alternation, built FROM the tables above rather than written out again, so a
 * counter cannot be added to one and forgotten in the other.
 *
 * Native forms are sorted longest-first because the regex engine takes the first
 * alternative that matches, and a shorter prefix listed earlier truncates the count
 * (열 ahead of 열다섯 reads 열다섯 as 10). The 십 compound precedes a bare sino digit
 * for the same reason.
 *
 * Every pattern that uses this anchors it against a following unit or counter word,
 * which is what keeps a sino digit from matching the first syllable of an ordinary
 * word: the 일 in 일어서기 is only read as a number when a unit follows it.
 */
const SINO_ALT = Object.keys(KO_SINO).join('|')
const NATIVE_ALT = Object.keys(KO_NATIVE).sort((a, b) => b.length - a.length).join('|')
// The digit branch comes from a regex literal's own source rather than a quoted
// '\\d+', so the pattern needs no escaped-backslash string to read past.
const DIGITS = /\d+/.source
const NUM = `(${DIGITS}|${NATIVE_ALT}|(?:${SINO_ALT})?십(?:${SINO_ALT})?|${SINO_ALT})`

/**
 * The hour of a clock reading, narrower than `NUM` on purpose.
 *
 * Korean says an hour with a native counter (한시, 세 시) or digits, never with a
 * bare sino digit — and every sino digit opens ordinary words, so allowing them
 * read 일시 중단 해제 ("lift the suspension") as 01:00 and saved it as 중단 해제.
 * Minutes keep the full `NUM`, because minutes ARE said in sino (삼십분).
 */
const HOUR_NUM = `(${DIGITS}|${NATIVE_ALT})`

/** Duration units. 시 is deliberately absent: it reads a clock, not a length. */
const UNIT = '(분|시간|일|주일|주)'

function unitMinutes(word: string): number | null {
  if (word === '분') return MIN
  if (word === '시간') return HOUR
  if (word === '일') return DAY
  if (word === '주' || word === '주일') return WEEK
  return null
}

/**
 * Longest schedule this model can carry, in minutes — ten years, matching the
 * ceiling the backend applies to a stored recurrence.
 *
 * Not cosmetic: a typed 20-digit count otherwise reaches `new Date`, produces an
 * Invalid Date, and `toISOString()` THROWS out of the submit handler, so the add
 * crashes rather than merely landing on a wrong time. Refusing the reading turns it
 * back into a "when?" prompt.
 */
const MAX_SCHEDULE_MINUTES = 10 * 366 * 24 * 60

/** A duration in minutes, or null when it is unusable or beyond what can be scheduled. */
function scaledMinutes(count: number, unitMins: number): number | null {
  const total = Math.round(count * unitMins)
  if (!Number.isFinite(total) || total <= 0 || total > MAX_SCHEDULE_MINUTES) return null
  return Math.max(1, total)
}

/**
 * Default hour for a named part of the day, and which meridiem it states.
 *
 * Korean states the meridiem as its own word, so 오후 3시 is unambiguous in a way a
 * bare 3시 is not — the hour is shifted rather than guessed. `noon` is neither: 점심
 * 12시 and 정오 are already 12, and shifting them either way would be wrong.
 *
 * INVARIANT: every entry is a complete word a user would write as a time. A bare
 * syllable here would be blanked out of the middle of the user's own words: 밤 is
 * also "chestnut" and 낮 opens 낮잠, which is why the standalone reading of any of
 * these has to pass `inSchedulePosition` first.
 */
const DAY_PARTS: ReadonlyArray<[string, number, Meridiem]> = [
  // [word, default hour when no clock is given, meridiem it states]
  ['새벽', 5, 'am'],
  ['아침', 9, 'am'],
  ['오전', 9, 'am'],
  ['점심', 12, 'noon'],
  ['정오', 12, 'noon'],
  ['낮', 13, 'pm'],
  ['오후', 15, 'pm'],
  ['저녁', 19, 'pm'],
  ['밤', 20, 'night'],
  ['자정', 0, 'am'],
]

/**
 * Apply a day part's meridiem to a 1–12 clock reading.
 *
 * 오후 3시 → 15:00, 오후 12시 stays 12, 오전 12시 is midnight, and 점심 12시 stays 12.
 */
type Meridiem = 'am' | 'pm' | 'noon' | 'night'

function shiftMeridiem(hour: number, meridiem: Meridiem): number {
  // 밤 12시 is MIDNIGHT while 오후 12시 is noon, so night cannot share the pm rule:
  // pm leaves 12 alone, which turned 내일 밤 12시 into a lunchtime reminder.
  if (meridiem === 'night') return hour === 12 ? 0 : hour < 12 ? hour + 12 : hour
  if (meridiem === 'pm' && hour < 12) return hour + 12
  if (meridiem === 'am' && hour === 12) return 0
  return hour
}

const DAY_PART_ALT = DAY_PARTS.map(([w]) => w).join('|')

/** A day or frequency marker directly before a day part makes it a time phrase. */
const KO_DAY_MARKER = /(?:오늘|내일|낼|모레|글피|매일|매|이번|다음|다음\s*주|주말)\s*$/

/**
 * Particles that end a Korean time phrase, and therefore belong INSIDE the span.
 *
 * Blanking only the time word would strand its particle in the saved text: 저녁에
 * 약 먹기 would come back as "에 약 먹기". Absorbing the particle is what keeps the
 * reminder reading like the user's own sentence.
 */
const KO_TIME_TAIL = /^(?:쯤|경|정도)?(?:에|부터|까지|엔)?/

/**
 * Whether a day-part word at `start` is acting as a TIME rather than sitting inside
 * ordinary text the user wants to keep.
 *
 * The Korean mirror of `inSchedulePosition` in reminderParseZh.ts, and the guard the
 * whole DAY_PARTS class of bug comes down to: the word is matched anywhere and then
 * BLANKED from the saved text, so a match inside a noun compound does not mis-time
 * the reminder, it rewrites what the user typed. Without it, 아침 회의록 정리
 * ("write up the morning meeting notes") is scheduled for 9:00 AND saved as
 * "회의록 정리", and 밤 사 오기 ("buy chestnuts") becomes 사 오기 at 20:00.
 *
 * A day part counts as a time when a day or frequency marker sits directly before
 * it, or when a time particle follows it, or when it ends the input. Anything else
 * (아침 followed by an ordinary noun) is part of a phrase.
 *
 * Deliberately conservative: when it is unclear the word stays in the user's text
 * and the reminder simply has no time, which `needsSchedule` then asks about.
 * Silently dropping a word the user typed is the worse failure.
 */
function inSchedulePosition(s: string, start: number, word: string): boolean {
  if (KO_DAY_MARKER.test(s.slice(0, start))) return true

  const after = s.slice(start + word.length)
  if (after === '') return true
  return /^(?:쯤|경|정도)?(?:에|엔|부터|까지)(?![가-힣])/.test(after)
}

/** How many characters of marker sit directly before a day part. */
function markerWidthBefore(s: string, start: number): number {
  const m = s.slice(0, start).match(KO_DAY_MARKER)
  return m ? m[0].length : 0
}

/** How many characters of trailing particle belong to a time phrase ending at `end`. */
function tailWidthAfter(s: string, end: number): number {
  const m = s.slice(end).match(KO_TIME_TAIL)
  return m ? m[0].length : 0
}

/** The Korean parser's result, whose shape is shared with every other language. */
export type KoParse = ScheduleParts

const spanOf = (m: RegExpMatchArray): Span => ({ start: m.index!, end: m.index! + m[0].length })

/** Widen a span to swallow the time particle that follows it. */
function withTail(s: string, span: Span): Span {
  return { start: span.start, end: span.end + tailWidthAfter(s, span.end) }
}

/**
 * A weekday or 주말 directly after 주 means the user named a DAY, not a week.
 *
 * The Recurrence model is a single interval and cannot express a weekday, so 매주
 * 월요일 must fall through and ASK — treating it as weekly would fire on the wrong
 * day, every week.
 */
function namesAWeekday(s: string, afterIndex: number): boolean {
  return /^\s*(?:[월화수목금토일]요일|[월화수목금토일]욜|말)/.test(s.slice(afterIndex))
}

/** 하루에 세 번 / 한 시간에 두 번 — a rate rather than an interval, so it divides. */
function findRate(s: string): IntervalHit | null {
  const m = s.match(new RegExp(`(하루|일주일|한\\s*주|1\\s*주|한\\s*시간|1\\s*시간)\\s*에?\\s*${NUM}\\s*번`))
  if (!m) return null
  const per = /주/.test(m[1]) ? WEEK : /시간/.test(m[1]) ? HOUR : DAY
  const times = koNumber(m[2])
  if (times == null || times <= 0) return null
  const every = scaledMinutes(1 / times, per)
  if (every == null) return null
  return { everyMinutes: every, span: spanOf(m) }
}

/**
 * 30분마다 / 매 2시간 / 매일 / 매시간 / 매일 아침.
 *
 * `hour` is carried out for the day-part form, because the span masks the day-part
 * word before the clock scan runs — without it 매일 저녁 would lose its 19:00 and
 * fall back to "one interval from now".
 */
function findInterval(s: string): IntervalHit | null {
  const rate = findRate(s)
  if (rate) return rate

  // 매일 아침 / 매 저녁 — a day part repeats daily. Checked before the unit forms so
  // the day part is not left stranded behind a 매일 match, but SKIPPED when a clock
  // follows it: 매일 아침 9시 must leave 아침 9시 for the clock scan, which reads the
  // meridiem from it, rather than swallowing 아침 and seeing an ambiguous bare 9시.
  const dayPart = s.match(new RegExp(`매\\s*일?\\s*(${DAY_PART_ALT})`))
  if (dayPart) {
    const rest = s.slice(dayPart.index! + dayPart[0].length)
    const followedByClock = new RegExp(`^\\s*(?:${HOUR_NUM}\\s*시(?!간)|\\d{1,2}:\\d{2})`).test(rest)
    if (!followedByClock) {
      const entry = DAY_PARTS.find(([w]) => w === dayPart[1])
      return { everyMinutes: DAY, span: spanOf(dayPart), hour: entry?.[1] }
    }
  }

  // 매 30분 / 매일 / 매시간 / 매주.
  const prefixed = s.match(new RegExp(`매\\s*${NUM}?\\s*${UNIT}`))
  if (prefixed) {
    const mins = unitMinutes(prefixed[2])
    const count = prefixed[1] ? koNumber(prefixed[1]) : 1
    const weekday = /^주/.test(prefixed[2]) && namesAWeekday(s, prefixed.index! + prefixed[0].length)
    const every = mins != null && count != null ? scaledMinutes(count, mins) : null
    if (every != null && !weekday) {
      return { everyMinutes: every, span: spanOf(prefixed) }
    }
  }

  // 30분마다 / 2시간 간격으로 / 이틀마다.
  const fused = s.match(new RegExp(`(${FUSED_DAY_ALT})\\s*(?:마다|간격으로|간격)`))
  if (fused) {
    return { everyMinutes: FUSED_DAYS[fused[1]] * DAY, span: spanOf(fused) }
  }

  const suffixed = s.match(new RegExp(`${NUM}?\\s*${UNIT}\\s*(?:마다|간격으로|간격)`))
  if (suffixed) {
    const mins = unitMinutes(suffixed[2])
    const count = suffixed[1] ? koNumber(suffixed[1]) : 1
    const every = mins != null && count != null ? scaledMinutes(count, mins) : null
    if (every != null) {
      return { everyMinutes: every, span: spanOf(suffixed) }
    }
  }

  return null
}

/**
 * Day counts Korean writes as one fused word rather than a number plus a unit.
 *
 * These cannot fall out of the `NUM` + `UNIT` shape at all — 이틀 is not "2" followed
 * by "day" — so without them the most ordinary way to say "in two days" reads as no
 * schedule.
 */
const FUSED_DAYS: Record<string, number> = {
  하루: 1, 이틀: 2, 사흘: 3, 나흘: 4, 닷새: 5, 엿새: 6, 이레: 7, 열흘: 10,
}

const FUSED_DAY_ALT = Object.keys(FUSED_DAYS).join('|')

/** 20분 뒤에 / 한 시간 후 / 30분 있다가 / 이틀 뒤. */
function findDelay(s: string): DelayHit | null {
  const AFTER = '(?:뒤|후|이따가?|있다가|지나(?:서|면))'

  const fused = s.match(new RegExp(`(${FUSED_DAY_ALT})\\s*${AFTER}`))
  if (fused) {
    return { minutes: FUSED_DAYS[fused[1]] * DAY, span: withTail(s, spanOf(fused)) }
  }

  const m = s.match(new RegExp(`${NUM}\\s*${UNIT}\\s*${AFTER}`))
  if (!m) return null
  const mins = unitMinutes(m[2])
  const count = koNumber(m[1])
  if (mins == null || count == null) return null
  const minutes = scaledMinutes(count, mins)
  if (minutes == null) return null
  return { minutes, span: withTail(s, spanOf(m)) }
}

/** 오후 3시 30분 / 아침 9시 / 9시 반 / 15:00 / 정오. */
function findClock(s: string): ClockHit | null {
  // A colon time, optionally prefixed by a day part (오후 3:51).
  const colon = s.match(new RegExp(`(${DAY_PART_ALT})?\\s*(\\d{1,2}):(\\d{2})`))
  if (colon) {
    let hour = parseInt(colon[2], 10)
    const minute = parseInt(colon[3], 10)
    if (hour <= 23 && minute <= 59) {
      const entry = colon[1] ? DAY_PARTS.find(([w]) => w === colon[1]) : undefined
      let explicit = false
      if (entry) {
        hour = shiftMeridiem(hour, entry[2])
        explicit = true
      } else if (hour >= 13 || hour === 0) {
        // 15:51 states the meridiem by being 24-hour; 3:51 does not. A bare 1–12
        // colon time must stay AMBIGUOUS so the next-occurrence rule can pick this
        // afternoon rather than rolling an explicit 03:51 to tomorrow.
        explicit = true
      }
      return { hour, minute, explicit, span: withTail(s, spanOf(colon)) }
    }
  }

  // 오후 세 시 반 / 9시 30분 / 아침 7시.
  // `시(?!간)`: the clock marker 시 is a PREFIX of the hour word 시간, so an unanchored
  // 시 reads the 시 that opens 시간 — 1시간 운동 would schedule 01:00 and save "간 운동".
  // Chinese (点 vs 小时) and the English `\b` paths have no such collision.
  //
  // The trailing lookahead is the same defect from the other side: 시 also opens
  // ordinary words, so a reading has to END at a token boundary — whitespace,
  // punctuation, a digit, end of input, or one of the particles a time phrase takes.
  // Without it 한시적으로 알림 끄기 schedules 01:00 and saves "적으로 알림 끄기".
  const clockTail = '(?=$|[^가-힣]|에|엔|쯤|경|정도|부터|까지)'
  const m = s.match(
    new RegExp(`(${DAY_PART_ALT})?\\s*${HOUR_NUM}\\s*시(?!간)\\s*(반|${NUM}\\s*분?)?${clockTail}`),
  )
  if (m) {
    const read = koNumber(m[2])
    if (read != null && read <= 23) {
      let hour = read
      let minute = 0
      if (m[3] === '반') minute = 30
      else if (m[3]) {
        const mm = koNumber(m[3].replace(/분/g, '').trim())
        // An explicit minute that is not a minute means this is not a clock reading:
        // 9시 90분 has to ASK rather than quietly become 09:00.
        if (mm == null || mm > 59) return null
        minute = mm
      }

      const entry = m[1] ? DAY_PARTS.find(([w]) => w === m[1]) : undefined
      let explicit = false
      if (entry) {
        hour = shiftMeridiem(hour, entry[2])
        explicit = true
      }
      return { hour, minute, explicit, span: withTail(s, spanOf(m)) }
    }
  }

  // A day part with no clock at all: 내일 아침 / 저녁에. Must be in scheduling
  // position — see inSchedulePosition and the DAY_PARTS invariant.
  for (const [word, hour] of DAY_PARTS) {
    const global = new RegExp(word, 'g')
    let dm: RegExpExecArray | null
    while ((dm = global.exec(s)) !== null) {
      if (!inSchedulePosition(s, dm.index, word)) continue
      // Swallow the introducing marker too, so 이번 저녁 does not leave a stranded
      // 이번 in the text the user gets back.
      const start = dm.index - markerWidthBefore(s, dm.index)
      return {
        hour,
        minute: 0,
        explicit: true,
        span: withTail(s, { start, end: dm.index + word.length }),
      }
    }
  }
  return null
}

/** 오늘 / 내일 / 모레 / 글피. */
function findDayOffset(s: string): DayOffsetHit | null {
  const table: ReadonlyArray<[RegExp, number]> = [
    [/글피/, 3],
    [/내일\s*모레|모레/, 2],
    // 낼 is bounded because it is also the tail of ordinary verbs — 보낼 이메일 정리하기
    // otherwise lost its 낼 and was scheduled for tomorrow as "보 이메일 정리하기".
    [/내일|(?<![가-힣])낼(?![가-힣])/, 1],
    [/오늘/, 0],
  ]
  for (const [re, offset] of table) {
    const m = s.match(re)
    if (m) return { offset, span: withTail(s, spanOf(m)) }
  }
  return null
}

/**
 * 매주 월요일 / 매주말 — a weekday or weekend repeat.
 *
 * Refused WHOLESALE, not partially. Dropping only the repeat leaves the clock
 * standing, so 매주 월요일 오전 9시 would save as a ONE-TIME 9am reminder — a weekly
 * request silently turned into a single one, which is worse than admitting the shape
 * is unsupported. With no schedule at all the caller asks instead.
 *
 * The Recurrence model is a single interval and cannot express a named day; the
 * Chinese path refuses 每周一 for the same reason.
 */
function namesWeekdayRepeat(s: string): boolean {
  const every = s.match(/매\s*주/)
  if (every && namesAWeekday(s, every.index! + every[0].length)) return true
  // 월요일마다 / 주말마다 name the same unsupported shape without 매주. Left to the
  // interval patterns, the 일 of 월요일 read as a DAY unit and 월요일마다 became a
  // daily reminder whose text was mangled to 월요.
  return /(?:[월화수목금토일]요일|[월화수목금토일]욜|주말)\s*마다/.test(s)
}

/**
 * Parse the schedule parts out of Korean input.
 *
 * Returns the pieces rather than a finished reminder so the caller applies the same
 * next-occurrence and rollover rules the other two languages use — those rules are
 * about time, not language, and duplicating them is how the paths would drift.
 */
export function parseKoParts(input: string): KoParse {
  if (namesWeekdayRepeat(input)) {
    return {
      everyMinutes: null,
      delayMinutes: null,
      clock: null,
      dayOffset: 0,
      spans: [],
      hasSignal: false,
    }
  }

  return assembleParts(
    input,
    findInterval(input),
    findDelay(input),
    findDayOffset(input),
    findClock,
  )
}

/** Politeness and framing that opens a sentence and carries no meaning. */
export const KO_LEAD_FILLER = /^(?:제발\s+|좀\s+|리마인더\s*[:：]\s*|나(?:에게|한테)?\s+|저(?:에게|한테)\s+)+/

/**
 * The Korean analogue of the English lead filler, at the other end of the sentence.
 *
 * Korean puts the request verb last (물 마시기 알려 줘), so stripping only a leading
 * opener would leave "알려 줘" welded onto every saved reminder.
 */
export const KO_TRAIL_FILLER =
  /(?:\s*(?:하라고|라고|한다고)?\s*(?:좀\s*)?(?:알려|말해|얘기해|리마인드\s*해?|기억해|깨워)\s*(?:주세요|줘요|줘|주라|달라)?|\s*잊지\s*(?:마(?:세요|요)?|말\s*(?:아?라|게|자|고)?)|\s*해\s*(?:주세요|줘))\s*[.!?~]*$/
