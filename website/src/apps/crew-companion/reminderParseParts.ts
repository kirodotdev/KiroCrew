/**
 * The shape a non-English reminder parser hands back, and the one assembly step the
 * language modules share.
 *
 * Chinese and Korean differ in every pattern but agree on the plumbing: mask what the
 * interval and delay already consumed so their digits are not re-read as a clock,
 * scan the remainder for a clock, carry a day-part hour that the mask would otherwise
 * hide, and collect the spans the caller strips from the saved text. That sequence is
 * about how the pieces fit together rather than about either language, so it lives
 * here — one place to get the masking order right, instead of two that drift.
 */

import { toUnits } from './reminderText'

export interface Span { start: number; end: number }

/** A recognised repeat. `hour` carries a day part's time of day (매일 저녁 / 每晚). */
export interface IntervalHit { everyMinutes: number; span: Span; hour?: number }

/** A recognised relative delay. */
export interface DelayHit { minutes: number; span: Span }

/** A recognised clock reading, already meridiem-resolved. */
export interface ClockHit { hour: number; minute: number; explicit: boolean; span: Span }

/** A recognised day marker: 0 = today, 1 = tomorrow, and so on. */
export interface DayOffsetHit { offset: number; span: Span }

export interface ScheduleParts {
  /** Repeat interval in minutes, or null for one-time. */
  everyMinutes: number | null
  /** Relative delay in minutes. */
  delayMinutes: number | null
  /** Clock time, already meridiem-resolved. */
  clock: { hour: number; minute: number; explicit: boolean } | null
  /** 0 = today, 1 = tomorrow, and so on. */
  dayOffset: number
  /** Ranges to strip when building the reminder text. */
  spans: Span[]
  /** False when nothing schedule-like was found. */
  hasSignal: boolean
}

/**
 * Combine the per-language hits into the parts the caller resolves against a clock.
 *
 * `findClock` is called on a COPY of the input with the interval and delay ranges
 * blanked, which is what stops the 2 in 每2小时 / 2시간마다 being read as 2 o'clock.
 * It receives a string of the same length as the input, so the spans it reports stay
 * aligned with the original text.
 */
export function assembleParts(
  input: string,
  interval: IntervalHit | null,
  delay: DelayHit | null,
  dayOff: DayOffsetHit | null,
  findClock: (masked: string) => ClockHit | null,
): ScheduleParts {
  const masked = toUnits(input)
  for (const sp of [interval?.span, delay?.span].filter(Boolean) as Span[]) {
    for (let i = sp.start; i < sp.end; i++) masked[i] = ' '
  }

  let clock = findClock(masked.join(''))
  if (!clock && interval?.hour != null) {
    // The interval's span masked the day-part word before the clock scan, so without
    // this 每晚 / 매일 저녁 would lose its hour and fall back to "one interval away".
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
