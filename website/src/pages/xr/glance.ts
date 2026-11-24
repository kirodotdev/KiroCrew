/**
 * The glance: what one Bot's dashboard says in the few lines a person can read
 * in a second. It is built from typed data only (goals, key results, the
 * briefing, the Bot's asks). The Bot's own page is free-form HTML, so it never
 * reaches the XR scene: the glance names the page's state and links to it.
 *
 * Every fact carries the time it was read at and how its read went, so an old
 * number never looks new and a failed read never looks like "nothing".
 */
import type { MemberGoals } from '../../api/client'
import { firstLine, type XrBot, type XrFeedState, type XrNeed } from './model'

export type XrTrend = 'up' | 'down' | 'flat' | 'none'
export type XrPageState = 'live' | 'stale' | 'empty' | 'error' | 'unknown'

export interface XrNumber {
  label: string
  value: string
  target: string
  trend: XrTrend
  /** Epoch seconds of this number's own latest reading; 0 when never read. */
  at: number
}

export interface XrGlance {
  /** One line: how the Bot's work stands. */
  verdict: string
  /** Epoch seconds the verdict was written; 0 when it is only the Bot's last line. */
  verdictAt: number
  /** Every key result. A small card draws the first few and counts the rest. */
  numbers: XrNumber[]
  needs: XrNeed[]
  /** What the Bot is on now; '' when nothing runs. */
  next: string
  page: XrPageState
  goals: string[]
  reports: { title: string; at: number }[]
  /** How the goals read and the briefing read went. */
  goalsFeed: XrFeedState
  briefFeed: XrFeedState
}

/** How many numbers and asks a small card draws before it counts the rest. */
export const XR_GLANCE_NUMBERS = 3
export const XR_GLANCE_NEEDS = 2
/** A reading older than this is marked as old next to the number. */
export const XR_NUMBER_OLD_S = 24 * 3600

/**
 * The signs that count as a space round a plain number, written out: the gateway's reader of a Bot's 3D dashboard
 * (`dashboard_xr_layout.py`, `PLAIN_SPACES`) holds the same list, and the two languages do not agree on what `\s`
 * is. These are the signs this page's `\s` took, so nothing that was drawn as a number is drawn otherwise.
 */
export const PLAIN_SPACES: readonly number[] = [0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x20, 0xa0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000, 0xfeff]
const SPACE = new Set(PLAIN_SPACES)
/** The number itself, with no space in it: digits, and groups of a point or a comma and digits. */
const PLAIN_CORE = /^-?\d+(?:[.,]\d+)*$/
/** A number written with thousands commas: one to three digits, groups of a comma and exactly three digits, at most one point and digits. */
const GROUPED = /^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$/

/**
 * A reading that is a plain number (a percent sign and thousands commas are allowed), or null.
 *
 * Spaces before it and after it, one percent sign after it, and spaces round that sign. The spaces are cut off by
 * walking in from each end, once: the time is the text's length, whatever the text holds (a rule that tried each
 * way to split a run of spaces round the sign took the square of it, M175 and M182).
 *
 * Commas: a text whose commas all stand before groups of exactly three digits, after a first group of one to three,
 * is a number with thousands commas ("12,345,678.9" is 12345678.9). In any other text "1,23" is 1.23, as before.
 */
export function plainNumber(text: string): number | null {
  let start = 0
  let end = text.length
  while (start < end && SPACE.has(text.charCodeAt(start))) start += 1
  while (end > start && SPACE.has(text.charCodeAt(end - 1))) end -= 1
  if (end > start && text.charCodeAt(end - 1) === 0x25) {
    end -= 1
    while (end > start && SPACE.has(text.charCodeAt(end - 1))) end -= 1
  }
  const core = text.slice(start, end)
  if (!PLAIN_CORE.test(core)) return null
  // "1,234,567" is 1234567: all its commas stand before groups of three, so all go out. In any other text a comma
  // before three digits goes out (matches do not overlap: of two in a row only the first) and the first one left is the point.
  const bare = GROUPED.test(core) ? core.replace(/,/g, '') : core.replace(/(\d),(\d{3})/g, '$1$2').replace(',', '.')
  const n = Number(bare)
  return Number.isFinite(n) ? n : null
}

/** Compare the last two readings, when both are plain numbers. Free text has no trend. */
export function trendOf(readings: readonly { value: string }[] | undefined): XrTrend {
  const last = (readings ?? []).slice(-2).map(r => plainNumber(r.value))
  if (last.length < 2 || last[0] === null || last[1] === null) return 'none'
  return last[1] > last[0] ? 'up' : last[1] < last[0] ? 'down' : 'flat'
}

export interface XrBriefing { text?: string; updated_ts?: number }

export function buildGlance(
  bot: XrBot,
  goals: Pick<MemberGoals, 'goals' | 'key_results' | 'reports'> | null,
  briefing: XrBriefing | null,
  page: XrPageState,
  feeds: { goals: XrFeedState; briefing: XrFeedState } = { goals: 'ok', briefing: 'ok' },
): XrGlance {
  const krs = goals?.key_results ?? []
  // No progress bar: the data does not say whether more or less is better, so a
  // bar would call "44 tickets against a target of 40" a success.
  const numbers: XrNumber[] = krs.map(k => ({
    label: firstLine(k.text, 60),
    value: k.current || '—',
    target: k.target,
    trend: trendOf(k.readings),
    at: k.updated_at || 0,
  }))
  const working = bot.things.find(t => t.state === 'working' && !t.direct) ?? bot.things.find(t => t.state === 'working')
  const brief = firstLine(briefing?.text, 110)
  return {
    verdict: brief || bot.line,
    verdictAt: brief ? briefing?.updated_ts ?? 0 : bot.lastTs,
    numbers,
    needs: bot.needs,
    next: working ? working.topic || working.line : '',
    page,
    goals: (goals?.goals ?? []).map(g => firstLine(g.text, 120)).filter(Boolean),
    reports: (goals?.reports ?? []).map(r => ({ title: firstLine(r.title, 60), at: r.at })),
    goalsFeed: feeds.goals,
    briefFeed: feeds.briefing,
  }
}
