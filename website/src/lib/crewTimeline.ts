/**
 * Where a Crew Mode timeline puts things: the time window, each bar's left and
 * width as a share of the track, and the axis ticks.
 *
 * Pure over `GET .../crew-timeline`'s reply, so the view and its tests agree on
 * every position. Nothing here invents a moment: a bar is placed from the times
 * the server read off transcript rows, and only its drawn width has a floor so a
 * zero-length bar (a tool whose end is not recorded) stays visible.
 */
import type { CrewTimeline } from '../types'

/** The window a timeline is drawn over, in epoch milliseconds. */
export interface TimelineWindow {
  start: number
  end: number
}

/** Room left past "now" while the run is live, so the playhead has somewhere to go. */
export const LIVE_HEADROOM = 0.25
/** The shortest live window, so a run a few seconds old is not drawn at 100x. */
export const MIN_LIVE_SPAN_MS = 30_000

export function parseTime(value: string | undefined | null): number | null {
  if (!value) return null
  const ms = Date.parse(value)
  return Number.isFinite(ms) ? ms : null
}

/**
 * The drawn window: from the first recorded row to the last one when the run is
 * done. While it is live the window runs past the server's `now` by at least
 * `LIVE_HEADROOM`, rounded up to an axis step, so the same window holds across
 * reads: the bars stay put and the playhead walks right until a read outgrows
 * it and the window widens to the next step.
 */
export function timelineWindow(data: CrewTimeline): TimelineWindow | null {
  const start = parseTime(data.started)
  if (start === null) return null
  if (data.live) {
    const now = parseTime(data.now) ?? start
    const target = Math.max(now - start, MIN_LIVE_SPAN_MS) * (1 + LIVE_HEADROOM)
    return { start, end: start + roundUpToStep(target) }
  }
  const end = parseTime(data.ended) ?? start
  return { start, end: Math.max(end, start + 1000) }
}

/** A time as a percentage of the window, clamped to the track. */
export function pct(window: TimelineWindow, ms: number): number {
  const span = window.end - window.start
  if (span <= 0) return 0
  return Math.min(100, Math.max(0, ((ms - window.start) / span) * 100))
}

/** A bar's left and width as percentages; null when its times do not parse. */
export function barBox(window: TimelineWindow, start: string, end: string): { left: number; width: number } | null {
  const from = parseTime(start)
  const to = parseTime(end)
  if (from === null || to === null) return null
  const left = pct(window, from)
  return { left, width: Math.max(0, pct(window, Math.max(to, from)) - left) }
}

const TICK_STEPS_MS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200].map(s => s * 1000)

/** *ms* rounded up to a multiple of the axis step that splits it into at most four. */
export function roundUpToStep(ms: number): number {
  const step = TICK_STEPS_MS.find(s => ms / s <= 4) ?? TICK_STEPS_MS[TICK_STEPS_MS.length - 1]
  return Math.ceil(ms / step) * step
}

/** Axis ticks as offsets from the window's start: about `target` evenly spaced, round steps. */
export function axisTicks(window: TimelineWindow, target = 4): number[] {
  const span = window.end - window.start
  if (span <= 0) return [0]
  const step = TICK_STEPS_MS.find(s => span / s <= target) ?? TICK_STEPS_MS[TICK_STEPS_MS.length - 1]
  const ticks: number[] = []
  for (let at = 0; at <= span; at += step) ticks.push(at)
  return ticks
}

/**
 * How far into the track a moment is drawn, given every moment the timeline
 * shows. Time runs true (linear) up to `LINEAR_MS` per stretch; past that a
 * stretch grows by half a minute per doubling, so one 30-minute wait cannot squash every
 * short turn and tool call into a sliver. The header still says the true elapsed
 * time, and `compressed` says the axis is not to scale so the view can say it.
 */
export interface TimeScale {
  /** A time as a percentage of the track, clamped to it. */
  toPct: (ms: number) => number
  /** Whether any stretch was drawn shorter than true. */
  compressed: boolean
}

/** The longest stretch drawn to true length. */
export const LINEAR_MS = 60_000

/** How much a doubling past `LINEAR_MS` adds, as a share of `LINEAR_MS`. */
const LOG_STEP = 0.5

function stretchWeight(ms: number): number {
  if (ms <= LINEAR_MS) return ms
  return LINEAR_MS * (1 + LOG_STEP * Math.log2(ms / LINEAR_MS))
}

/** The breakpoints of *data* inside *window*: every bar, hand-off and mark end. */
function breakpoints(data: CrewTimeline, window: TimelineWindow): number[] {
  const out = new Set<number>([window.start, window.end])
  const add = (value: string | undefined) => {
    const ms = parseTime(value)
    if (ms !== null && ms > window.start && ms < window.end) out.add(ms)
  }
  for (const lane of data.lanes) {
    for (const bar of lane.segments) { add(bar.start); add(bar.end) }
    for (const mark of lane.marks) add(mark.ts)
  }
  for (const link of data.handoffs) { add(link.start); add(link.end) }
  return [...out].sort((a, b) => a - b)
}

export function timelineScale(data: CrewTimeline, window: TimelineWindow): TimeScale {
  const points = breakpoints(data, window)
  const cumulative: number[] = [0]
  let compressed = false
  for (let i = 1; i < points.length; i += 1) {
    const span = points[i] - points[i - 1]
    if (span > LINEAR_MS) compressed = true
    cumulative.push(cumulative[i - 1] + stretchWeight(span))
  }
  const total = cumulative[cumulative.length - 1] || 1
  const toPct = (ms: number): number => {
    if (ms <= points[0]) return 0
    if (ms >= points[points.length - 1]) return 100
    let lo = 0
    let hi = points.length - 1
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1
      if (points[mid] <= ms) lo = mid
      else hi = mid
    }
    const span = points[hi] - points[lo]
    const within = span > 0 ? stretchWeight(ms - points[lo]) / stretchWeight(span) : 0
    return ((cumulative[lo] + within * (cumulative[hi] - cumulative[lo])) / total) * 100
  }
  return { toPct, compressed }
}

/** A bar's left and width on *scale*; null when its times do not parse. */
export function scaledBox(scale: TimeScale, start: string, end: string): { left: number; width: number } | null {
  const from = parseTime(start)
  const to = parseTime(end)
  if (from === null || to === null) return null
  const left = scale.toPct(from)
  return { left, width: Math.max(0, scale.toPct(Math.max(to, from)) - left) }
}

/** Axis ticks on *scale*: round offsets from the start whose labels sit at
 *  least `minGapPct` apart on the track, the start always among them. */
export function scaledTicks(window: TimelineWindow, scale: TimeScale, minGapPct = 14): { at: number; pct: number }[] {
  const out: { at: number; pct: number }[] = []
  for (const at of axisTicks(window, 12)) {
    const pct = scale.toPct(window.start + at)
    if (out.length === 0 || pct - out[out.length - 1].pct >= minGapPct) out.push({ at, pct })
  }
  return out
}
