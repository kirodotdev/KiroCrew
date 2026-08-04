/**
 * Natural-language reminder parsing — pure, so every phrasing below is unit-testable
 * without a clock, a network call or a DOM.
 *
 * This is deliberately a LOCAL parser rather than an LLM call. Typing "water every
 * 2 hours" should feel instant and work offline; a round-trip would put a spinner
 * and a failure mode in front of the single most common action in the app. The
 * grammar people actually use for reminders is small and regular, so a parser
 * covers it with predictable results.
 *
 * The tradeoff, stated plainly: this handles the common shapes ("every 2 hours",
 * "at 3pm", "in 20 minutes", "tomorrow at 9"), NOT arbitrary prose. When nothing is
 * recognised it does not guess — it reports `needsSchedule` so the UI can ask, which
 * is a better failure than silently inventing a time.
 *
 * The `fallbackText` argument is the reminder text used when parsing strips
 * everything away; the page passes a translated default so no English literal leaks
 * into a non-English UI.
 */

import type { Recurrence } from './types'
import { hasHan, parseZhParts, ZH_LEAD_FILLER } from './reminderParseZh'
import { foldForMatch, toUnits } from './reminderText'

export interface ParsedReminder {
  /** What to say when it fires, with the schedule words removed. */
  text: string
  /** When it first fires, ISO 8601. Null only when needsSchedule is true. */
  fireAt: string | null
  recurrence: Recurrence | null
  /**
   * True when no time or interval was found. The caller must ask rather than
   * inventing one — a reminder at a guessed time is worse than no reminder.
   */
  needsSchedule: boolean
}

/** Conventional hour for a bare "tomorrow" — morning, not midnight. */
const DEFAULT_HOUR = 9

const MIN = 1
const HOUR = 60
const DAY = 1440
const WEEK = 10080

/** Unit words → minutes. Ordered longest-first where prefixes overlap. */
const UNITS: ReadonlyArray<[RegExp, number]> = [
  [/^(minutes?|mins?|m)$/, MIN],
  [/^(hours?|hrs?|h)$/, HOUR],
  [/^(days?|d)$/, DAY],
  [/^(weeks?|wks?|w)$/, WEEK],
  // "every morning" is a daily repeat whose time of day comes from DAY_PARTS.
  [/^(mornings?|afternoons?|evenings?|nights?)$/, DAY],
]

/**
 * Default hour for a named part of the day.
 *
 * These are CONVENTIONS, not guesses in the sense the parser otherwise refuses:
 * the user did name a time, just coarsely. Inventing a time for "buy milk" (no time
 * words at all) is a different thing, and still refused.
 */
const DAY_PARTS: ReadonlyArray<[RegExp, number]> = [
  [/\b(mornings?)\b/, 9],
  [/\b(afternoons?)\b/, 15],
  [/\b(evenings?)\b/, 19],
  [/\b(tonight|nights?|nightly)\b/, 20],
]

function unitToMinutes(word: string): number | null {
  for (const [re, mins] of UNITS) if (re.test(word)) return mins
  return null
}

/** Words people use for a count instead of a digit. */
const WORD_NUMBERS: Record<string, number> = {
  a: 1, an: 1, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6,
  seven: 7, eight: 8, nine: 9, ten: 10, fifteen: 15, twenty: 20, thirty: 30,
  forty: 40, fortyfive: 45, sixty: 60, ninety: 90, half: 0.5,
}

function toCount(raw: string): number | null {
  const t = raw.trim().toLowerCase()
  if (/^\d+$/.test(t)) return parseInt(t, 10)
  return WORD_NUMBERS[t] ?? null
}

interface Span { start: number; end: number }

/** A recognised interval, plus where it sat in the input so it can be stripped. */
interface IntervalHit { everyMinutes: number; span: Span }

/**
 * "every 2 hours", "every hour", "hourly", "daily".
 *
 * Note `every other day` is NOT special-cased — it falls out of the general form
 * only if written "every 2 days", so it is left to needsSchedule rather than
 * silently parsed as daily.
 */
function findInterval(lower: string): IntervalHit | null {
  // "twice a day", "3 times a day" — a rate rather than an interval, so it divides.
  const rate = lower.match(/\b(twice|(\d+)\s*times?)\s+(?:a|per|each)\s+([a-z]+)\b/)
  if (rate) {
    const per = unitToMinutes(rate[3])
    const times = rate[2] ? parseInt(rate[2], 10) : 2
    if (per != null && times > 0) {
      return {
        everyMinutes: Math.max(1, Math.round(per / times)),
        span: { start: rate.index!, end: rate.index! + rate[0].length },
      }
    }
  }

  // Two explicit patterns rather than one with an optional count group: with the
  // optional form, "every day at 8am" greedily reads "day" as the count and "at"
  // as the unit, and the interval is missed entirely.
  // `each` is accepted alongside `every` — "each morning" is the same request.
  const withCount = lower.match(/\b(?:every|each)\s+(\d+|[a-z]+)\s+([a-z]+)\b/)
  if (withCount) {
    const mins = unitToMinutes(withCount[2])
    const count = toCount(withCount[1])
    if (mins != null && count != null && count > 0) {
      return {
        everyMinutes: Math.max(1, Math.round(count * mins)),
        span: { start: withCount.index!, end: withCount.index! + withCount[0].length },
      }
    }
  }

  // "every hour", "every morning", and the leading half of "every day at 8am".
  const bare = lower.match(/\b(?:every|each)\s+([a-z]+)\b/)
  if (bare) {
    const mins = unitToMinutes(bare[1])
    if (mins != null) {
      return { everyMinutes: mins, span: { start: bare.index!, end: bare.index! + bare[0].length } }
    }
  }

  const named = lower.match(/\b(hourly|daily|weekly|nightly)\b/)
  if (named) {
    const mins = named[1] === 'hourly' ? HOUR : named[1] === 'weekly' ? WEEK : DAY
    return { everyMinutes: mins, span: { start: named.index!, end: named.index! + named[0].length } }
  }
  return null
}

/**
 * A named part of the day ("tonight", "every morning") as a clock reading.
 *
 * Treated as an explicit meridiem: "morning" means 9am, and must never resolve to
 * 9pm via the bare-hour ambiguity rule.
 */
/**
 * A scheduling marker immediately BEFORE a day part makes it a time phrase:
 * "tomorrow morning", "this evening", "every night", "in the morning", "by tonight".
 */
const EN_DAY_MARKER = /\b(?:tomorrow|today|tonight|this|每|every|each|on|at|by|in\s+the|the\s+following|next)\s*$/

/** A trailing schedule fragment: a clock, a connector, or nothing at all. */
const EN_SCHEDULE_TAIL = /^(?:\s*$|[\s,;.]*(?:at|by|on|around|from)\b|\s*\d|\s*(?:tomorrow|today|tonight)\b)/

/**
 * Whether a day-part word at `start` is acting as a TIME rather than sitting inside
 * ordinary text the user wants to keep.
 *
 * The English mirror of `inSchedulePosition` in reminderParseZh.ts. Without it,
 * "morning" in "submit morning report" is read as 9am AND blanked out of the saved
 * text, so the reminder becomes "submit report". Every day-part word is also an
 * ordinary English noun or adjective -- "morning report", "afternoon tea",
 * "night shift", "evening news" -- so position, not presence, decides.
 *
 * Kept deliberately conservative: when it is unclear, the word stays in the user's
 * text and the reminder simply has no time, which `needsSchedule` then asks about.
 * Silently dropping a word the user typed is the worse failure.
 */
function inSchedulePositionEn(lower: string, start: number, word: string): boolean {
  if (EN_DAY_MARKER.test(lower.slice(0, start))) return true

  const after = lower.slice(start + word.length)
  return EN_SCHEDULE_TAIL.test(after)
}

/**
 * How many characters of marker sit directly before a day part, so the span can
 * swallow them.
 *
 * Blanking only the day part would leave its introducer stranded in the saved
 * text: "this evening call dad" would become "this call dad", and "take pills in
 * the morning" would become "take pills in the". The marker is part of the time
 * phrase, so it belongs inside the span. Harmless when it overlaps another span --
 * `cleanText` blanks ranges, so overlapping with e.g. the "tomorrow" span is
 * idempotent.
 */
function markerWidthBefore(lower: string, start: number): number {
  const m = lower.slice(0, start).match(EN_DAY_MARKER)
  return m ? m[0].length : 0
}

function findDayPart(lower: string): ClockHit | null {
  for (const [re, hour] of DAY_PARTS) {
    // Scan EVERY occurrence, not just the first: in "morning report tomorrow morning"
    // the first "morning" is part of the text and the second is the schedule.
    const global = new RegExp(re.source, 'g')
    let m: RegExpExecArray | null
    while ((m = global.exec(lower)) !== null) {
      if (!inSchedulePositionEn(lower, m.index, m[0])) continue
      // Swallow the introducing marker too, so "this evening" does not leave a
      // stranded "this" in the text the user gets back.
      const start = m.index - markerWidthBefore(lower, m.index)
      return {
        hour, minute: 0, explicitMeridiem: true,
        span: { start, end: m.index + m[0].length },
      }
    }
  }
  return null
}

interface DelayHit { minutes: number; span: Span }

/** "in 20 minutes", "in an hour", "in 2h". */
function findDelay(lower: string): DelayHit | null {
  const m = lower.match(/\bin\s+(\d+|[a-z]+)\s*([a-z]+)\b/)
  if (!m) return null
  const mins = unitToMinutes(m[2])
  const count = toCount(m[1])
  if (mins == null || count == null || count <= 0) return null
  return {
    minutes: Math.max(1, Math.round(count * mins)),
    span: { start: m.index!, end: m.index! + m[0].length },
  }
}

interface ClockHit { hour: number; minute: number; explicitMeridiem: boolean; span: Span }

/** "at 3pm", "3:30pm", "15:00", "at noon". */
function findClock(lower: string): ClockHit | null {
  const noon = lower.match(/\b(noon|midday|midnight)\b/)
  if (noon) {
    return {
      hour: noon[1] === 'midnight' ? 0 : 12, minute: 0, explicitMeridiem: true,
      span: { start: noon.index!, end: noon.index! + noon[0].length },
    }
  }

  /*
    `at` is optional so "3pm" works, but a bare number with neither `at` nor a
    meridiem nor minutes is NOT a clock — otherwise "every 2 hours" would read the
    2 as a time.

    Scan ALL candidates rather than testing only the first. Testing only the first
    stops at the earliest number-shaped thing, so any reminder that opens with a
    quantity loses its time entirely: "take 2 pills every day at 3pm" matches the
    bare `2`, fails the shape test, and never sees 3pm.
  */
  const re = /\b(?:(at)\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b/g
  for (const m of lower.matchAll(re)) {
    const hasAt = !!m[1]
    const meridiem = m[4]
    const hasMinutes = m[3] != null
    // Not clock-shaped — keep looking instead of abandoning the search.
    if (!hasAt && !meridiem && !hasMinutes) continue

    let hour = parseInt(m[2], 10)
    const minute = m[3] ? parseInt(m[3], 10) : 0
    if (hour > 23 || minute > 59) continue

    if (meridiem === 'pm' && hour < 12) hour += 12
    if (meridiem === 'am' && hour === 12) hour = 0

    return {
      hour, minute, explicitMeridiem: !!meridiem,
      span: { start: m.index!, end: m.index! + m[0].length },
    }
  }
  return null
}

function findTomorrow(lower: string): Span | null {
  const m = lower.match(/\b(tomorrow|tmr|tomorow)\b/)
  return m ? { start: m.index!, end: m.index! + m[0].length } : null
}

function atClock(base: Date, hour: number, minute: number): Date {
  const d = new Date(base)
  d.setHours(hour, minute, 0, 0)
  return d
}

/**
 * Resolve a clock reading to a concrete future instant.
 *
 * A bare hour ("at 9") is genuinely ambiguous, so it resolves to whichever of 9:00
 * / 21:00 comes first — predictable, and never in the past. With an explicit am/pm
 * the stated hour is honoured and only the DAY rolls forward.
 */
function resolveClock(now: Date, hit: ClockHit, tomorrow: boolean): Date {
  if (tomorrow) {
    const d = atClock(now, hit.hour, hit.minute)
    d.setDate(d.getDate() + 1)
    return d
  }

  const first = atClock(now, hit.hour, hit.minute)
  if (first > now) return first

  if (!hit.explicitMeridiem && hit.hour < 12) {
    const pm = atClock(now, hit.hour + 12, hit.minute)
    if (pm > now) return pm
  }

  const next = atClock(now, hit.hour, hit.minute)
  next.setDate(next.getDate() + 1)
  return next
}

/** Filler that carries no meaning once the schedule is extracted. */
const LEAD_FILLER = /^(?:please\s+)?(?:can\s+you\s+)?(?:remind(?:er)?\s+me\s+(?:to\s+|about\s+)?|remember\s+to\s+|tell\s+me\s+to\s+|nudge\s+me\s+(?:to\s+)?)/i

function cleanText(original: string, spans: Span[]): string {
  // Blank the matched ranges rather than string-replacing, so repeated words like
  // "water" in "water every 2 hours" cannot be removed by accident.
  // toUnits and NOT [...original]: spans come from regex .index, which counts
  // UTF-16 code units, while the spread iterates CODE POINTS. See reminderText.ts
  // for the full contract -- it is shared so this rule cannot be honoured in one
  // place and quietly broken in another.
  const chars = toUnits(original)
  for (const s of spans) for (let i = s.start; i < s.end && i < chars.length; i++) chars[i] = '\u0000'
  let out = chars.filter(c => c !== '\u0000').join('')

  out = out.replace(LEAD_FILLER, '')
  out = out.replace(/\s+/g, ' ').trim()
  out = out.replace(/[\s,]*\b(at|on|every|in|to)\b[\s,]*$/i, '').trim()
  out = out.replace(/^[\s,]*\b(at|on|to)\b\s*/i, '').trim()
  out = out.replace(/[.,;:]+$/, '').trim()
  return out
}

/**
 * Parse a reminder phrase.
 *
 * `now` is injected so the resolution rules are testable at a fixed instant.
 * `fallbackText` is the text used when nothing but schedule words was typed.
 */
export function parseReminder(input: string, now: Date, fallbackText = 'Reminder'): ParsedReminder {
  const original = input.trim()
  // foldForMatch, NOT toLowerCase(): `İ`.toLowerCase() is TWO code units, which
  // shifts every span measured afterwards and corrupts the saved text.
  const lower = foldForMatch(original)

  // Chinese input takes the CJK rules. Dispatching on script rather than on the UI
  // language deliberately: a Chinese UI user may still type English and vice versa,
  // and the text itself is the only reliable signal.
  if (hasHan(original)) return parseZh(original, now, fallbackText)

  const interval = findInterval(lower)
  const delay = findDelay(lower)
  const tomorrowSpan = findTomorrow(lower)

  // Look for a clock time only OUTSIDE the interval/delay match, so the "2" in
  // "every 2 hours" is never read as 2 o'clock.
  const consumed = [interval?.span, delay?.span].filter(Boolean) as Span[]
  const masked = toUnits(lower)
  for (const s of consumed) for (let i = s.start; i < s.end; i++) masked[i] = ' '

  // An explicit clock wins over a day part, so "every morning at 7am" is 7am.
  const dayPart = findDayPart(lower)
  const clock: ClockHit | null = findClock(masked.join('')) ?? dayPart

  const spans: Span[] = [...consumed]
  if (clock) spans.push(clock.span)
  if (dayPart) spans.push(dayPart.span)
  if (tomorrowSpan) spans.push(tomorrowSpan)

  const text = cleanText(original, spans) || fallbackText

  // "buy milk tomorrow" names a day but no time — a bare `tomorrow` is a one-time
  // reminder at a conventional morning hour rather than an unschedulable input.
  if (!interval && !delay && !clock && tomorrowSpan) {
    const d = atClock(now, DEFAULT_HOUR, 0)
    d.setDate(d.getDate() + 1)
    return { text, fireAt: d.toISOString(), recurrence: null, needsSchedule: false }
  }

  if (!interval && !delay && !clock) {
    return { text, fireAt: null, recurrence: null, needsSchedule: true }
  }

  let fireAt: Date
  if (clock) {
    fireAt = resolveClock(now, clock, !!tomorrowSpan)
  } else if (delay) {
    fireAt = new Date(now.getTime() + delay.minutes * 60_000)
  } else {
    // Recurring with no stated time — first fire is one interval away rather than
    // immediately, so adding "every 2 hours" does not fire the moment you press Enter.
    fireAt = new Date(now.getTime() + interval!.everyMinutes * 60_000)
  }

  return {
    text,
    fireAt: fireAt.toISOString(),
    recurrence: interval ? { everyMinutes: interval.everyMinutes } : null,
    needsSchedule: false,
  }
}

/**
 * Resolve Chinese input.
 *
 * Deliberately shares the DAY/next-occurrence rules with the English path via
 * `atClock` and `resolveClock` — those rules are about time, not language, and
 * duplicating them is how the two paths would silently drift apart.
 */
function parseZh(original: string, now: Date, fallbackText: string): ParsedReminder {
  const parts = parseZhParts(original)

  let text = cleanText(original, parts.spans)
  text = text.replace(ZH_LEAD_FILLER, '').trim()
  // Chinese has no inter-word spaces, so leftover connectives read as noise.
  text = text.replace(/^[，,、。\s]+|[，,、。\s]+$/g, '').trim()
  if (!text) text = fallbackText

  /*
    Same rule as the English path above: a clock, a delay or an interval is a
    schedule; anything else is not. `hasSignal` alone is too weak, because a
    zero-offset day marker (今天) is a signal that carries NO time — it would slip
    through and fall out of the branch chain below onto an invented default.

    needsSchedule's own contract is that the caller asks rather than the parser
    guessing, so both languages must agree on this condition or one of them
    silently schedules something the user never asked for.
  */
  const zhHasSchedule = parts.clock != null
    || parts.delayMinutes != null
    || parts.everyMinutes != null
    || parts.dayOffset > 0
  if (!parts.hasSignal || !zhHasSchedule) {
    return { text, fireAt: null, recurrence: null, needsSchedule: true }
  }

  let fireAt: Date
  if (parts.clock) {
    if (parts.dayOffset > 0) {
      fireAt = atClock(now, parts.clock.hour, parts.clock.minute)
      fireAt.setDate(fireAt.getDate() + parts.dayOffset)
    } else {
      fireAt = resolveClock(
        now,
        {
          hour: parts.clock.hour, minute: parts.clock.minute,
          explicitMeridiem: parts.clock.explicit, span: { start: 0, end: 0 },
        },
        false,
      )
    }
  } else if (parts.delayMinutes != null) {
    fireAt = new Date(now.getTime() + parts.delayMinutes * 60_000)
  } else if (parts.dayOffset > 0) {
    // 明天 with no time — the same conventional morning hour as the English side.
    fireAt = atClock(now, DEFAULT_HOUR, 0)
    fireAt.setDate(fireAt.getDate() + parts.dayOffset)
  } else {
    /*
      Recurring with no stated time: first fire one interval out, so adding
      每2小时 does not fire the moment you press Enter.

      everyMinutes is non-null here by the zhHasSchedule guard above — every other
      branch of that guard is handled by an earlier arm of this chain. There is
      deliberately no fallback default (an invented one-hour default would make
      今天提醒我买牛奶 schedule itself silently).
    */
    fireAt = new Date(now.getTime() + parts.everyMinutes! * 60_000)
  }

  return {
    text,
    fireAt: fireAt.toISOString(),
    recurrence: parts.everyMinutes ? { everyMinutes: parts.everyMinutes } : null,
    needsSchedule: false,
  }
}
