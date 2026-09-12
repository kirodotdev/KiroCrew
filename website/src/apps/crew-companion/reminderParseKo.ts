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
 *
 * 반 ("half") is dropped from the hour alternatives: it is the minute word after
 * 시 (9시 반) and a half for durations (반 시간), but Korean has no half o'clock —
 * reading it as an hour turned 반시 주문하기 into a reminder near midnight.
 */
const NATIVE_HOUR_ALT = Object.keys(KO_NATIVE)
  .filter((w) => w !== '반')
  .sort((a, b) => b.length - a.length)
  .join('|')
const HOUR_NUM = `(${DIGITS}|${NATIVE_HOUR_ALT})`

/**
 * A count for durations and rates, narrower than `NUM` the same way `HOUR_NUM` is.
 *
 * Digits, native counters, and 십-compounds only — a BARE sino digit is
 * deliberately absent, because sino digits open ordinary words: 구분 ("section")
 * read as 9분 and turned 구분마다 into a nine-minute repeat. Clock MINUTES keep the
 * full `NUM`: after 시 a sino reading (9시 삼십분) is unambiguous.
 */
const COUNT_NUM = `(${DIGITS}|${NATIVE_ALT}|(?:${SINO_ALT})?십(?:${SINO_ALT})?)`

/**
 * Interval, delay and rate readings must also START at a word boundary — the
 * other half of the same defect: 매일 sits inside 구매일 ("purchase date") and 일
 * inside 휴일 ("holiday"), so an unanchored unit pattern reads a repeat out of an
 * ordinary noun.
 */
const WORD_START = '(?<![가-힣])'

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
  // And 밤 1시–5시 are the small hours PAST midnight (밤 1시 = 01:00) — adding 12
  // to every hour below 12 turned a late-night reminder into 13:00.
  if (meridiem === 'night') {
    if (hour === 12) return 0
    // 밤 0시 is an explicit midnight — the default add-12 turned it into noon.
    if (hour === 0) return 0
    if (hour >= 1 && hour <= 5) return hour
    return hour < 12 ? hour + 12 : hour
  }
  if (meridiem === 'pm' && hour < 12) return hour + 12
  if (meridiem === 'am' && hour === 12) return 0
  return hour
}

const DAY_PART_ALT = DAY_PARTS.map(([w]) => w).join('|')

/** A day or frequency marker directly before a day part makes it a time phrase. */
const KO_DAY_MARKER = /(?:오늘|내일|낼|모레|글피|매일|매|이번|다음|다음\s*주|주말)\s*$/

/**
 * The SINGLE definition of the two token classes that end a Korean time phrase:
 * approximators (9시쯤) and time particles (저녁에, 내일부터). Every tail rule below
 * derives from these two — the 군밤/저녁에 class of bug was four hand-copied
 * variants of this list drifting apart.
 */
const KO_APPROX = '(?:쯤|경|정도)'
// A time particle may carry a topic/additive ending (에는, 에도, 부터는, 까지는):
// absorbing only the bare particle stranded the ending in the saved text, so
// 20분 뒤에는 물 마시기 came back named 는 물 마시기. 엔 is the fused 에는.
const KO_PARTICLE = '(?:에|엔|부터|까지)(?:는|도)?'

/**
 * Particles that end a Korean time phrase, and therefore belong INSIDE the span.
 *
 * Blanking only the time word would strand its particle in the saved text: 저녁에
 * 약 먹기 would come back as "에 약 먹기". Absorbing the particle is what keeps the
 * reminder reading like the user's own sentence.
 */
const KO_TIME_TAIL = new RegExp(`^${KO_APPROX}?(?:${KO_PARTICLE})?`)

/** A REQUIRED particle (optionally approximated), not welded into a longer word. */
const KO_PARTICLE_AFTER = new RegExp(`^${KO_APPROX}?${KO_PARTICLE}(?![가-힣])`)

/**
 * Whether a schedule token starting at `idx` sits INSIDE an ordinary Hangul word.
 *
 * The SINGLE definition of "mid-word" for every reader in this module: a token
 * whose first character is directly preceded by Hangul is part of that word
 * (제한시, 군밤, 낮잠) — UNLESS the Hangul before it ends in a day marker, because
 * 오늘밤 9시 is one fused time phrase and must keep reading.
 */
function startsMidWord(s: string, idx: number): boolean {
  return idx > 0 && /[가-힣]/.test(s[idx - 1]) && !KO_DAY_MARKER.test(s.slice(0, idx))
}

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
 * it, or when it starts a word AND a time particle follows it or it ends the input.
 * The mid-word rejection applies to EVERY branch: 군밤 ends the input and 군밤에
 * carries a particle, yet neither 밤 is a time — the noun it ends decides, not
 * what follows.
 *
 * Deliberately conservative: when it is unclear the word stays in the user's text
 * and the reminder simply has no time, which `needsSchedule` then asks about.
 * Silently dropping a word the user typed is the worse failure.
 */
function inSchedulePosition(s: string, start: number, word: string): boolean {
  if (KO_DAY_MARKER.test(s.slice(0, start))) return true
  if (startsMidWord(s, start)) return false

  const after = s.slice(start + word.length)
  if (after === '') return true
  return KO_PARTICLE_AFTER.test(after)
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
  // 말 is bounded because it also opens ordinary words: 매주 말하기 연습 ("weekly
  // speaking practice") is a plain weekly repeat, not a weekend, and refusing it
  // took away a shape this parser supports. A bare 말 only means 주말 here when it
  // stands alone (매주말), which the bound still reads.
  return /^\s*(?:[월화수목금토일]요일|[월화수목금토일]욜|말(?![가-힣]))/.test(s.slice(afterIndex))
}

/** 하루에 세 번 / 한 시간에 두 번 — a rate rather than an interval, so it divides. */
function findRate(s: string): IntervalHit | null {
  const m = s.match(new RegExp(`${WORD_START}(하루|일주일|한\\s*주|1\\s*주|한\\s*시간|1\\s*시간)\\s*에?\\s*${COUNT_NUM}\\s*번`))
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
  // The span absorbs an optional repeat suffix exactly like the prefixed form —
  // 매일 아침마다 matched only 매일 아침, stranding 마다 at the front of the saved text.
  const dayPart = s.match(
    new RegExp(`${WORD_START}매\\s*일?\\s*(${DAY_PART_ALT})(?:\\s*(?:마다|씩)(?![가-힣]))?`),
  )
  if (dayPart) {
    const rest = s.slice(dayPart.index! + dayPart[0].length)
    const followedByClock = new RegExp(`^\\s*(?:${HOUR_NUM}\\s*시(?!간)|\\d{1,2}:\\d{2})`).test(rest)
    if (!followedByClock) {
      const entry = DAY_PARTS.find(([w]) => w === dayPart[1])
      // The span takes its particle like every other time phrase: 매일 저녁에 blanked
      // without the 에 persisted a daily reminder NAMED 에.
      return { everyMinutes: DAY, span: withTail(s, spanOf(dayPart)), hour: entry?.[1] }
    }
  }

  // 매 30분 / 매일 / 매시간 / 매주. The span takes an optional repeat suffix —
  // 매 30분마다 matched only 매 30분, stranding 마다 at the front of the saved text.
  // The suffix is Hangul-bounded so it never bites the first syllable of a
  // following word (매 30분 씩씩하게 걷기 keeps 씩씩하게 whole).
  const prefixed = s.match(
    new RegExp(`${WORD_START}매\\s*${COUNT_NUM}?\\s*${UNIT}(?:\\s*(?:마다|씩|간격으로|간격)(?![가-힣]))?`),
  )
  if (prefixed) {
    const mins = unitMinutes(prefixed[2])
    const count = prefixed[1] ? koNumber(prefixed[1]) : 1
    const weekday = /^주/.test(prefixed[2]) && namesAWeekday(s, prefixed.index! + prefixed[0].length)
    const every = mins != null && count != null ? scaledMinutes(count, mins) : null
    if (every != null && !weekday) {
      return { everyMinutes: every, span: spanOf(prefixed) }
    }
  }

  // 30분마다 / 2시간 간격으로 / 이틀마다. An optional leading 매 joins the span —
  // 매 이틀마다 matched from 이틀, stranding 매 at the front of the saved text.
  const fused = s.match(new RegExp(`${WORD_START}(?:매\\s*)?(${FUSED_DAY_ALT})\\s*(?:마다|간격으로|간격)`))
  if (fused) {
    return { everyMinutes: FUSED_DAYS[fused[1]] * DAY, span: spanOf(fused) }
  }

  const suffixed = s.match(new RegExp(`${WORD_START}${COUNT_NUM}?\\s*${UNIT}\\s*(?:마다|간격으로|간격)`))
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

  const fused = s.match(new RegExp(`${WORD_START}(${FUSED_DAY_ALT})\\s*${AFTER}`))
  if (fused) {
    return { minutes: FUSED_DAYS[fused[1]] * DAY, span: withTail(s, spanOf(fused)) }
  }

  const m = s.match(new RegExp(`${WORD_START}${COUNT_NUM}\\s*${UNIT}\\s*${AFTER}`))
  if (!m) return null
  const mins = unitMinutes(m[2])
  const count = koNumber(m[1])
  if (mins == null || count == null) return null
  const minutes = scaledMinutes(count, mins)
  if (minutes == null) return null
  return { minutes, span: withTail(s, spanOf(m)) }
}

/**
 * The SINGLE clock scan behind both `findClock` and `hasBrokenClock`.
 *
 * Six review rounds of this parser came down to the reader and the guard being two
 * parallel definitions of "a valid clock reading" that disagreed on new input
 * shapes: the reader refused 9시 90 but the guard only checked minutes wearing 분,
 * so the refused clock silently fell through to the 09:00 day default. Here every
 * clock-shaped candidate is enumerated ONCE and given one verdict:
 *
 *  - well-formed and readable -> it may become the reading (`hit`),
 *  - clock-shaped but malformed (hour > 23, minute > 59, unbounded digit run) ->
 *    it poisons the parse (`broken`), because the user plainly TRIED to state a
 *    clock and the honest outcome is to ask,
 *  - shaped like a clock but sitting inside an ordinary word (제한시) -> neither:
 *    it is the word the user typed, skipped for reading and not poison.
 *
 * The reader and the guard cannot disagree again because there is nothing left to
 * disagree: both consume this one scan.
 */
function scanClocks(s: string): { hit: ClockHit | null; broken: boolean; mentions: number } {
  let hit: ClockHit | null = null
  let broken = false

  // The match's optional leading day part + \s* can put the match start before the
  // token itself, so mid-word checks anchor at the first non-space character.
  const tokenStart = (c: RegExpMatchArray): number =>
    c.index! + (c[0].length - c[0].trimStart().length)

  // Every digit-colon run is either a well-formed, digit-bounded time or poison:
  // 012:30 and 25:00 must ASK rather than quietly become a nearby time or fall
  // through to a named day's 09:00 default.
  for (const m of s.matchAll(/\d+:\d+/g)) {
    const before = s[m.index! - 1]
    const after = s[m.index! + m[0].length]
    const unbounded =
      (before !== undefined && /\d/.test(before)) || (after !== undefined && /\d/.test(after))
    if (unbounded || !/^(?:[01]?\d|2[0-3]):[0-5]\d$/.test(m[0])) broken = true
  }

  // A colon time, optionally prefixed by a day part (오후 3:51). Read only when
  // well-formed — the raw scan above already poisoned everything else. Every
  // well-formed mention is COUNTED even after the first is taken: two stated
  // clocks are two reminders, not one (see the mention check below).
  const colonRe = new RegExp(`(${DAY_PART_ALT})?\\s*(?<!\\d)(\\d{1,2}):(\\d{2})(?!\\d)`, 'g')
  let clockMentions = 0
  for (const c of s.matchAll(colonRe)) {
    if (startsMidWord(s, tokenStart(c))) continue
    let hour = parseInt(c[2], 10)
    const minute = parseInt(c[3], 10)
    if (hour > 23 || minute > 59) continue
    clockMentions++
    if (hit) continue
    const entry = c[1] ? DAY_PARTS.find(([w]) => w === c[1]) : undefined
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
    hit = { hour, minute, explicit, span: withTail(s, spanOf(c)) }
  }

  // 오후 세 시 반 / 9시 30분 / 아침 7시 / 9시 30.
  // `시(?!간)`: the clock marker 시 is a PREFIX of the hour word 시간, so an unanchored
  // 시 reads the 시 that opens 시간 — 1시간 운동 would schedule 01:00 and save "간 운동".
  // The minute is a NUM with or without 분, digit-bounded so a longer digit run
  // cannot backtrack into a shorter "valid" minute (9시 90 must poison as 90, not
  // read as 9). No structural tail here: the tail decides READABILITY below, not
  // whether a malformed candidate poisons — 9시 90분이라고 states a broken clock
  // no matter what follows the 분.
  const clockRe = new RegExp(
    `(${DAY_PART_ALT})?\\s*${HOUR_NUM}\\s*시(?!간)(?:\\s*(반|${NUM}(?!\\d)\\s*분?))?`,
    'g',
  )
  // A reading must END at a token boundary — whitespace, punctuation, a digit, end
  // of input, or one of the particles a time phrase takes. 시 opens ordinary words,
  // so without this 한시적으로 알림 끄기 schedules 01:00 and saves "적으로 알림 끄기".
  const clockTail = new RegExp(`^(?:$|[^가-힣]|${KO_APPROX}|${KO_PARTICLE})`)
  for (const c of s.matchAll(clockRe)) {
    const hour = koNumber(c[2])
    let minute = 0
    let malformed = hour == null || hour > 23
    if (c[3] === '반') minute = 30
    else if (c[3]) {
      const mm = koNumber(c[3].replace(/분/g, '').trim())
      // An explicit minute that is not a minute means this is not a clock reading:
      // 9시 90분 — and the suffixless 9시 90 — have to ASK rather than quietly
      // become 09:00.
      if (mm == null || mm > 59) malformed = true
      else minute = mm
    }
    if (malformed) {
      broken = true
      continue
    }
    if (startsMidWord(s, tokenStart(c))) continue
    // A well-formed candidate is a clock MENTION whether or not its tail lets it
    // be read: 내일 3시와 5시에 회의 skipped 3시와 on its tail and read 5시에 alone,
    // persisting 05:00 with the corrupted text 3시와 회의.
    clockMentions++
    if (hit) continue
    if (!clockTail.test(s.slice(c.index! + c[0].length))) continue
    const entry = c[1] ? DAY_PARTS.find(([w]) => w === c[1]) : undefined
    let shifted = hour!
    let explicit = false
    if (entry) {
      shifted = shiftMeridiem(shifted, entry[2])
      explicit = true
    } else if (shifted >= 13 || shifted === 0) {
      // 14시 and 0시 state their meridiem by being 24-hour, exactly like the colon
      // forms — 0시 left ambiguous resolved as NOON after midnight.
      explicit = true
    }
    hit = { hour: shifted, minute, explicit, span: withTail(s, spanOf(c)) }
  }

  // Two stated clocks cannot be one reminder. Reading the first (or the readable
  // one) silently drops a time the user named, so the whole parse asks instead.
  if (clockMentions > 1) broken = true

  return { hit, broken, mentions: clockMentions }
}

/** 오후 3시 30분 / 아침 9시 / 9시 반 / 15:00 / 정오. */
function findClock(s: string): ClockHit | null {
  const { hit } = scanClocks(s)
  if (hit) return hit

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
  /*
    Every day token is bounded the SAME way, as an invariant rather than per-token
    patches — 낼 (tail of 보낼), 오늘 (head of 오늘날) and 모레 (inside 아모레퍼시픽)
    were each reported separately, and any unbounded sibling is the same bug waiting.
    A token reads as a day only when it does not sit inside a Hangul word: nothing
    Hangul directly before it, and after it only a word boundary, a day part
    (오늘밤, 내일아침 — written without a space), or a time particle (내일까지,
    모레쯤에).
  */
  const dayTail = `(?=$|[^가-힣]|${DAY_PART_ALT}|${KO_APPROX}?${KO_PARTICLE})`
  const dayWord = (alt: string) => `(?<![가-힣])(?:${alt})${dayTail}`
  const table: ReadonlyArray<[RegExp, number]> = [
    [new RegExp(dayWord('글피')), 3],
    [new RegExp(dayWord('내일\\s*모레|모레')), 2],
    // 낼 keeps its stricter both-sides bound: unlike the full words it is a single
    // syllable that ends ordinary verbs, so even a day-part continuation (낼아침)
    // is more likely to be a verb fragment than a time.
    [new RegExp(`${dayWord('내일')}|(?<![가-힣])낼(?![가-힣])`), 1],
    [new RegExp(dayWord('오늘')), 0],
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
 * 12월 25일 오전 9시 / 월요일 아침 / 다음 주 화요일 — calendar shapes the model
 * cannot express, refused WHOLESALE for the same reason as the weekday repeat:
 * dropping only the date leaves the clock standing, so 12월 25일 오전 9시 was
 * persisted for the NEXT 09:00 instead of December 25 — a reminder on a day the
 * user never named. With no schedule at all the caller asks instead.
 */
function namesUnsupportedDate(s: string): boolean {
  // A month-and-day calendar date. Digit-only on purpose: dates are written with
  // digits in practice, matching the scope note at the top of the module.
  if (/(?<!\d)\d{1,2}\s*월\s*\d{1,2}\s*일/.test(s)) return true
  // The numeric spellings of the same date: 12/25 and 12.25. Range-checked so an
  // arbitrary decimal does not read as a date, and bounded against digit/dot runs
  // so 1.2.3-style versions stay out.
  if (/(?<![\d.])(?:1[0-2]|0?[1-9])\s*[./]\s*(?:3[01]|[12]\d|0?[1-9])(?![\d.])/.test(s)) return true
  // The year-qualified spellings: 2026-12-25 / 2026.12.25 / 2026/12/25. The dash
  // and dotted year-first forms match none of the patterns above (2026.12.25's
  // leading digit-dot run fails their bound), so the date was DROPPED and the
  // reminder persisted for the next 09:00 instead of the named day. Month and day
  // stay range-checked and the run digit/dot-bounded, so 1.2.3-style versions and
  // longer numeric ids stay out.
  if (
    /(?<![\d.])\d{4}\s*[-./]\s*(?:1[0-2]|0?[1-9])\s*[-./]\s*(?:3[01]|[12]\d|0?[1-9])(?![\d.])/.test(
      s,
    )
  ) {
    return true
  }
  // 주말 / 평일 / 주중 / 이번 주 / 다음 주 — relative day-group names the interval
  // model cannot express any more than a weekday. Left to the day-part scan, 주말
  // 아침 read the 아침 and persisted TOMORROW morning instead of the weekend, and
  // 매주 평일 오전 9시 persisted a plain weekly repeat firing on the wrong days.
  // The day-group words take the same right bound as the 주 forms beside them:
  // 주말 also opens ordinary compounds, and an unbounded match read 주말농장
  // ("weekend farm") as a weekend and refused a plain next-day clock.
  if (
    /(?<![가-힣])(?:주말|평일|주중)(?=$|[^가-힣]|에|엔|부터|까지)|(?:이번|다음)\s*주(?=$|[^가-힣]|에|엔|부터|까지|말)/.test(
      s,
    )
  ) {
    return true
  }
  // A named weekday in ANY position, not just the 매주/마다 repeat forms — a
  // one-time 월요일 오전 9시 mis-schedules exactly the same way. 욜 is bounded
  // because it can open other syllable runs; X요일 appears in no ordinary word.
  return /[월화수목금토일]요일|[월화수목금토일]욜(?![가-힣])/.test(s)
}

/**
 * A clock-shaped candidate the unified scan refuses.
 *
 * A rejected clock must poison the WHOLE parse rather than only itself: with a
 * named day in the input, 내일 012:30 회의 dropped the broken clock, kept 내일, and
 * persisted tomorrow at the 09:00 default — a time the user never gave. The user
 * plainly TRIED to state a clock, so the honest outcome is to ask. The verdict
 * comes from the SAME scan the reader consumes (see scanClocks), so a shape the
 * reader refuses is a shape this guard poisons — by construction, not by keeping
 * two regexes in step.
 */
/**
 * How many separate day-part TIME MENTIONS the input carries — the day-part
 * mirror of scanClocks' clock-mention count. A mention is a day-part word that
 * is not mid-word, is not fused to a following clock (내일 아침 9시 is ONE
 * mention, read by the clock scan), and is qualified as a time by a day marker
 * before it, a time particle after it, or a joining conjunction (아침과 저녁에) —
 * the conjunction form is exactly the shape the fallback skipped, leaving 아침과
 * stranded in the saved text while 저녁에 persisted alone. A bare day-part noun
 * (아침 먹고 = breakfast; 오후 12시에 점심 = lunch) matches no arm and is never
 * counted.
 */
function countDayPartMentions(s: string): number {
  const followedByClock = new RegExp(`^\\s*(?:${HOUR_NUM}\\s*시(?!간)|\\d{1,2}:\\d{2})`)
  const conjunction = /^(?:과|와|하고|이랑|랑|및)(?![가-힣])/
  let count = 0
  for (const [word] of DAY_PARTS) {
    const global = new RegExp(word, 'g')
    let dm: RegExpExecArray | null
    while ((dm = global.exec(s)) !== null) {
      if (startsMidWord(s, dm.index)) continue
      const after = s.slice(dm.index + word.length)
      if (followedByClock.test(after)) continue
      // Qualified by a day marker before, a particle after, or a conjunction —
      // NOT by merely ending the input: a trailing bare day part is routinely
      // the reminder content itself (오후 12시에 점심 = lunch, not a second noon).
      if (
        KO_DAY_MARKER.test(s.slice(0, dm.index)) ||
        KO_PARTICLE_AFTER.test(after) ||
        conjunction.test(after)
      ) {
        count++
      }
    }
  }
  return count
}

function hasBrokenClock(s: string): boolean {
  const { broken, mentions } = scanClocks(s)
  // Clock and day-part mentions together name the moments the user stated. More
  // than one cannot be a single reminder — 아침과 저녁에 약 먹기 collapsed into one
  // 19:00 reminder named 아침과 약 먹기 — so the whole parse asks instead.
  return broken || mentions + countDayPartMentions(s) > 1
}

/**
 * Parse the schedule parts out of Korean input.
 *
 * Returns the pieces rather than a finished reminder so the caller applies the same
 * next-occurrence and rollover rules the other two languages use — those rules are
 * about time, not language, and duplicating them is how the paths would drift.
 */
export function parseKoParts(input: string): ScheduleParts {
  if (namesWeekdayRepeat(input) || namesUnsupportedDate(input) || hasBrokenClock(input)) {
    return {
      everyMinutes: null,
      delayMinutes: null,
      clock: null,
      dayOffset: 0,
      dayExplicit: false,
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
export const KO_LEAD_FILLER = /^(?:제발\s+|좀\s+|리마인더\s*[:：]\s*)+/

/**
 * A leading "to me" (나에게 / 저한테), stripped ONLY when the input carries request
 * framing (a trailing 알려 줘-style verb): there it addresses the reminder itself.
 * Standing alone it is part of the task — 내일 저에게 온 메일 확인하기 ("check the
 * mail that came TO ME") lost its recipient once the day word was removed and the
 * old unconditional lead filler saw 저에게 at the front.
 */
export const KO_LEAD_RECIPIENT = /^(?:나(?:에게|한테)?\s+|저(?:에게|한테)\s+)+/

/**
 * The Korean analogue of the English lead filler, at the other end of the sentence.
 *
 * Korean puts the request verb last (물 마시기 알려 줘), so stripping only a leading
 * opener would leave "알려 줘" welded onto every saved reminder.
 *
 * The quotative branch carries only the COMPLETE verb forms 하라고 / 한다고. A bare
 * 라고 is always welded to the verb stem it quotes (먹으라고, 마시라고), so stripping
 * it truncated the stem — 약 먹으라고 알려줘 was saved as 약 먹으. Leaving the
 * quotative in place keeps the user's own words whole.
 *
 * EVERY request verb strips only with an explicit request ending (주세요 / 줘요 /
 * 줘 / 주라 / 달라) — one rule, no per-verb exceptions. Each of these verbs can be
 * the task itself, not a request about the reminder: 아기 깨워 ("wake the baby") and
 * 팀에 결과 말해 ("tell the team the results") both lost their action verb when a
 * bare final verb was treated as filler. The ending is what marks the verb as
 * addressed to the assistant; without it the verb stays in the user's text.
 */
export const KO_TRAIL_FILLER =
  /(?:\s*(?:하라고|한다고)?\s*(?:좀\s*)?(?:알려|말해|얘기해|리마인드\s*해?|기억해|깨워)\s*(?:주세요|줘요|줘|주라|달라)|\s*잊지\s*(?:마(?:세요|요)?|말\s*(?:아?라|게|자|고)?)|\s*해\s*(?:주세요|줘))\s*[.!?~]*$/
