/**
 * Chinese natural-language reminder parsing — pure and separately testable.
 *
 * A separate module rather than more branches in reminderParse.ts, because almost
 * nothing transfers. The English patterns lean on `\b` word boundaries, and `\b`
 * does not match between a Han character and a digit or another Han character, so
 * reusing them would fail in ways that look like "no schedule found" rather than
 * like a bug. Chinese also puts the unit before the marker (2小时后, not "after 2
 * hours") and folds the meridiem into a day-part word (下午3点 = 15:00).
 *
 * Scope is the same as the English side: the common shapes, and an honest refusal
 * otherwise. It does NOT handle weekday names (每周一) or dates (8月5日), because
 * the Recurrence model is a single interval and cannot express them.
 */

import { toUnits } from './reminderText'

const MIN = 1
const HOUR = 60
const DAY = 1440
const WEEK = 10080

/** Does this look like Chinese input at all? */
export function hasHan(s: string): boolean {
  return /[\u4e00-\u9fff]/.test(s)
}

const CN_DIGITS: Record<string, number> = {
  零: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9,
}

/**
 * A Chinese or Arabic number.
 *
 * Handles the 十 forms people actually type for durations (十, 十五, 二十, 二十五,
 * 三十), plus 半 for "half". Larger constructions (一百二十) are out of scope — a
 * reminder interval that long is written with digits in practice.
 */
export function cnNumber(raw: string): number | null {
  const s = raw.trim()
  if (!s) return null
  if (/^\d+$/.test(s)) return parseInt(s, 10)
  if (s === '半') return 0.5

  if (s.includes('十')) {
    const [tensPart, onesPart] = s.split('十')
    const tens = tensPart === '' ? 1 : CN_DIGITS[tensPart]
    const ones = onesPart === '' || onesPart === undefined ? 0 : CN_DIGITS[onesPart]
    if (tens == null || ones == null) return null
    return tens * 10 + ones
  }

  if (s.length === 1) return CN_DIGITS[s] ?? null
  return null
}

const NUM = '(\\d+|[零一二两三四五六七八九十]+|半)'

/** Unit word → minutes. `个` is optional filler (2个小时). */
function unitMinutes(word: string): number | null {
  if (/^(分钟|分)$/.test(word)) return MIN
  if (/^(个?小时|个?钟头|个?小時)$/.test(word)) return HOUR
  if (/^(天|日)$/.test(word)) return DAY
  if (/^(周|星期|礼拜)$/.test(word)) return WEEK
  return null
}

const UNIT = '(分钟|分|个?小时|个?钟头|天|日|周|星期|礼拜)'

/**
 * Default hour for a named part of the day.
 *
 * Chinese folds the meridiem into these words, so 下午3点 is unambiguous in a way
 * that bare "at 3" is not — the hour is shifted rather than guessed.
 */
const NIGHT_HOUR = 20

/**
 * INVARIANT: every entry is a COMPLETE schedule word, never a bare character.
 *
 * These words are matched anywhere in the reminder and the match is then BLANKED
 * OUT of the saved text, so a single character here silently eats the user's own
 * words: a bare '晚' turned 明天提醒我交晚班报告 ("hand in the night-shift
 * report") into 交班报告 at 20:00. Any entry short enough to occur inside an
 * ordinary word (晚班, 早餐, 中午饭, 夜宵) corrupts text rather than mis-times it.
 *
 * 今晚/明晚 are spelled out because they are what a bare '晚' was really serving.
 * 每晚 does not need an entry: findInterval anchors it behind a required 每.
 */
const DAY_PARTS: ReadonlyArray<[string, number, number]> = [
  // [word, default hour when no clock given, 12-hour shift base]
  ['凌晨', 5, 0],
  ['早上', 9, 0],
  ['早晨', 9, 0],
  ['上午', 9, 0],
  ['中午', 12, 12],
  ['下午', 15, 12],
  ['傍晚', 18, 12],
  ['晚上', 20, 12],
  ['今晚', NIGHT_HOUR, 12],
  ['明晚', NIGHT_HOUR, 12],
  ['夜里', 22, 12],
]

/**
 * Default hour for a day-part word, including the bare '晚' that only
 * findInterval sees (behind a required 每, so it cannot match inside a word).
 */
function dayPartHour(word: string): number | undefined {
  const found = DAY_PARTS.find(([w]) => w === word)
  if (found) return found[1]
  return word === '晚' ? NIGHT_HOUR : undefined
}

/** Verbs that open the reminder body: after one of these, we are in the ACTION. */
const ZH_SCHEDULE_VERB = /^(?:提醒|叫我|记得|让我|通知)/
/** A day/frequency marker directly before a day part makes it a time phrase. */
const ZH_DAY_MARKER = /(?:今|明|后|每)$/

/**
 * Whether a day-part word at `start` is acting as a TIME rather than sitting inside
 * one of the user's words.
 *
 * This is the guard that the whole DAY_PARTS class of bug comes down to. The word is
 * matched anywhere in the string and then BLANKED from the saved text, so matching it
 * inside a noun compound does not mis-schedule the reminder — it rewrites what the
 * user typed. 提醒我买下午茶 became 买茶 at 15:00; before that, 交晚班报告 became
 * 交班报告.
 *
 * A day part counts as a time when the character AFTER it ends the time phrase — a
 * schedule verb, a clock, punctuation, whitespace, or end of input — or when a
 * day/frequency marker sits directly BEFORE it. Anything else (下午 followed by 茶)
 * is part of a word.
 *
 * Deliberately conservative: 提醒我明天下午开会 fails this test, so it falls back to
 * the conventional hour rather than 15:00. Telling 下午开会 from 下午茶 needs
 * part-of-speech knowledge this parser does not have, and a slightly wrong time is a
 * far better failure than silently altering the reminder's words.
 */
function inSchedulePosition(s: string, start: number, word: string): boolean {
  const before = s.slice(0, start)
  if (ZH_DAY_MARKER.test(before)) return true

  const after = s.slice(start + word.length)
  if (after === '') return true
  if (ZH_SCHEDULE_VERB.test(after)) return true
  // A clock, a counter, or ordinary separators all end the time phrase.
  return /^[\s，,、。：:0-9０-９一二三四五六七八九十半点分]/.test(after)
}

export interface Span { start: number; end: number }

export interface ZhParse {
  /** Repeat interval in minutes, or null for one-time. */
  everyMinutes: number | null
  /** Relative delay in minutes ("20分钟后"). */
  delayMinutes: number | null
  /** Clock time, already meridiem-resolved. */
  clock: { hour: number; minute: number; explicit: boolean } | null
  /** 0 = today, 1 = 明天, 2 = 后天. */
  dayOffset: number
  /** Ranges to strip when building the reminder text. */
  spans: Span[]
  /** False when nothing schedule-like was found. */
  hasSignal: boolean
}

const span = (m: RegExpMatchArray): Span => ({ start: m.index!, end: m.index! + m[0].length })

/** 一天两次 / 每天三次 → an interval derived from a rate. */
function findRate(s: string): { everyMinutes: number; span: Span } | null {
  const m = s.match(new RegExp(`(?:每|一)(天|日|小时|周)${NUM}次`))
  if (!m) return null
  const per = unitMinutes(m[1])
  const times = cnNumber(m[2])
  if (per == null || times == null || times <= 0) return null
  return { everyMinutes: Math.max(1, Math.round(per / times)), span: span(m) }
}

/** 每2小时 / 每天 / 每隔30分钟 / 每晚. */
function findInterval(s: string): { everyMinutes: number; span: Span; hour?: number } | null {
  const rate = findRate(s)
  if (rate) return rate

  // 每 or 每隔, an optional count, then a unit.
  const withUnit = s.match(new RegExp(`每(?:隔)?${NUM}?\\s*${UNIT}`))
  if (withUnit) {
    // 每周一 is "every Monday", NOT "every week". The Recurrence model is a single
    // interval and cannot express a weekday, so this must fall through and ASK —
    // silently treating it as weekly would fire on the wrong day, every week.
    if (/^(周|星期|礼拜)$/.test(withUnit[2])) {
      const after = s.charAt(withUnit.index! + withUnit[0].length)
      if (/[一二三四五六日]/.test(after)) return null
    }
    const mins = unitMinutes(withUnit[2])
    if (mins != null) {
      const count = withUnit[1] ? cnNumber(withUnit[1]) : 1
      if (count != null && count > 0) {
        return { everyMinutes: Math.max(1, Math.round(count * mins)), span: span(withUnit) }
      }
    }
  }

  // 每晚 / 每天早上 — a day part repeats daily.
  const dayPart = s.match(/每(?:隔)?(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|晚)/)
  if (dayPart) {
    // Carry the hour: the span masks the day-part word before the clock scan, so
    // without this 每晚 would lose its 20:00 and fall back to "one interval from
    // now". Via dayPartHour() because bare 晚 is deliberately not in DAY_PARTS.
    return { everyMinutes: DAY, span: span(dayPart), hour: dayPartHour(dayPart[1]) }
  }

  return null
}

/** 20分钟后 / 半小时后 / 过两小时. */
function findDelay(s: string): { minutes: number; span: Span } | null {
  const after = s.match(new RegExp(`${NUM}\\s*${UNIT}(?:之)?后`))
  if (after) {
    const mins = unitMinutes(after[2])
    const count = cnNumber(after[1])
    if (mins != null && count != null && count > 0) {
      return { minutes: Math.max(1, Math.round(count * mins)), span: span(after) }
    }
  }
  const guo = s.match(new RegExp(`过${NUM}\\s*${UNIT}`))
  if (guo) {
    const mins = unitMinutes(guo[2])
    const count = cnNumber(guo[1])
    if (mins != null && count != null && count > 0) {
      return { minutes: Math.max(1, Math.round(count * mins)), span: span(guo) }
    }
  }
  return null
}

/** 下午3点半 / 早上9点 / 8点30分 / 15:00. */
function findClock(s: string): { hour: number; minute: number; explicit: boolean; span: Span } | null {
  const partsAlt0 = DAY_PARTS.map(([w]) => w).join('|')

  // A colon time, optionally prefixed by a day part (下午3:51).
  const colon = s.match(new RegExp(`(${partsAlt0})?\\s*(\\d{1,2}):(\\d{2})`))
  if (colon) {
    let h = parseInt(colon[2], 10)
    const mi = parseInt(colon[3], 10)
    if (h <= 23 && mi <= 59) {
      const part = colon[1]
      let explicit = false
      if (part) {
        const found = DAY_PARTS.find(([w]) => w === part)
        if (found) {
          if (found[2] === 12 && h < 12) h += 12
          if (found[2] === 0 && h === 12) h = 0
          explicit = true
        }
      } else if (h >= 13 || h === 0) {
        // 15:51 states the meridiem by being 24-hour; 3:51 does not.
        explicit = true
      }
      // Note what is NOT set explicit: a bare 1–12 o'clock like 3:51. Marking it
      // explicit made "我3:51下班" typed at 15:50 resolve to 03:51 TOMORROW, because
      // an explicit time only rolls the DAY. It has to stay ambiguous so the
      // next-occurrence rule can pick 15:51 today.
      return { hour: h, minute: mi, explicit, span: span(colon) }
    }
  }

  const partsAlt = partsAlt0
  const m = s.match(new RegExp(`(${partsAlt})?\\s*${NUM}\\s*点\\s*(半|${NUM}\\s*分?)?`))
  if (m) {
    const hour = cnNumber(m[2])
    if (hour != null && hour <= 23) {
      let h = hour
      let minute = 0
      if (m[3] === '半') minute = 30
      else if (m[3]) {
        const mm = cnNumber(m[3].replace(/分/g, '').trim())
        if (mm != null && mm <= 59) minute = mm
      }

      const part = m[1]
      let explicit = false
      if (part) {
        const found = DAY_PARTS.find(([w]) => w === part)
        if (found) {
          const shift = found[2]
          // 下午3点 → 15:00, but 下午12点 stays 12 and 中午12点 stays 12.
          if (shift === 12 && h < 12) h += 12
          if (shift === 0 && h === 12) h = 0
          explicit = true
        }
      }
      return { hour: h, minute, explicit, span: span(m) }
    }
  }

  // A day part with no clock at all: 今晚 / 明天早上. Must be in scheduling
  // position — see inSchedulePosition, and the DAY_PARTS invariant comment.
  for (const [w, hour] of DAY_PARTS) {
    const dm = s.match(new RegExp(w))
    if (dm && inSchedulePosition(s, dm.index!, w)) {
      return { hour, minute: 0, explicit: true, span: span(dm) }
    }
  }
  return null
}

/** 今天 / 明天 / 后天 / 大后天. */
function findDayOffset(s: string): { offset: number; span: Span } | null {
  const table: ReadonlyArray<[RegExp, number]> = [
    [/大后天/, 3],
    [/后天/, 2],
    [/明天|明日|明晚/, 1],
    [/今天|今晚|今日/, 0],
  ]
  for (const [re, offset] of table) {
    const m = s.match(re)
    if (m) return { offset, span: span(m) }
  }
  return null
}

/**
 * Parse the schedule parts out of Chinese input.
 *
 * Returns the pieces rather than a finished reminder so the caller can apply the
 * same next-occurrence and rollover rules used for English — those rules are about
 * time, not language, and should not be duplicated.
 */
export function parseZhParts(input: string): ZhParse {
  const s = input
  const interval = findInterval(s)
  const delay = findDelay(s)
  const dayOff = findDayOffset(s)

  // Mask what the interval and delay already consumed, so the 2 in 每2小时 is not
  // then read as 2点.
  const masked = toUnits(s)
  for (const sp of [interval?.span, delay?.span].filter(Boolean) as Span[]) {
    for (let i = sp.start; i < sp.end; i++) masked[i] = ' '
  }
  let clock = findClock(masked.join(''))
  if (!clock && interval?.hour != null) {
    clock = { hour: interval.hour, minute: 0, explicit: true, span: interval.span }
  }

  const spans: Span[] = []
  if (interval) spans.push(interval.span)
  if (delay) spans.push(delay.span)
  if (clock) spans.push(clock.span)
  if (dayOff) spans.push(dayOff.span)

  return {
    everyMinutes: interval?.everyMinutes ?? null,
    delayMinutes: delay?.minutes ?? null,
    clock: clock ? { hour: clock.hour, minute: clock.minute, explicit: clock.explicit } : null,
    dayOffset: dayOff?.offset ?? 0,
    spans,
    hasSignal: !!(interval || delay || clock || dayOff),
  }
}

/** Politeness and framing that carries no meaning once the schedule is extracted. */
export const ZH_LEAD_FILLER = /^(?:请|麻烦|帮我|记得|别忘了|不要忘了|提醒我(?:要|去)?|叫我)+/
