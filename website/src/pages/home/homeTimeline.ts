/**
 * The agent board's time axis: a rolling window ending now, and the arithmetic
 * that places a span or an instant on it as percentages of the track's width.
 * Pure, so the board's drawing is testable without a DOM.
 */

/** The windows the board offers, in hours. */
export const TIMELINE_HOURS = [6, 12, 24] as const
export type TimelineHours = (typeof TIMELINE_HOURS)[number]

export interface TimelineWindow { startMs: number; endMs: number }

export function timelineWindow(hours: TimelineHours, nowMs: number): TimelineWindow {
  return { startMs: nowMs - hours * 3_600_000, endMs: nowMs }
}

/** A span's place on the track, clamped to the window; null when it lies wholly
 *  outside. `minPct` keeps a brief turn visible as a sliver rather than nothing. */
export interface TrackSpan { leftPct: number; widthPct: number }

export function spanOn(window: TimelineWindow, startMs: number, endMs: number, minPct = 0.4): TrackSpan | null {
  const total = window.endMs - window.startMs
  if (total <= 0) return null
  const start = Math.max(window.startMs, Math.min(startMs, endMs))
  const end = Math.min(window.endMs, Math.max(startMs, endMs))
  if (end < window.startMs || start > window.endMs) return null
  const leftPct = ((start - window.startMs) / total) * 100
  const widthPct = Math.max(minPct, ((end - start) / total) * 100)
  return { leftPct: Math.min(leftPct, 100 - minPct), widthPct: Math.min(widthPct, 100 - leftPct) }
}

/** An instant's place on the track, or null when outside the window. */
export function pointOn(window: TimelineWindow, atMs: number): number | null {
  if (atMs < window.startMs || atMs > window.endMs) return null
  return ((atMs - window.startMs) / (window.endMs - window.startMs)) * 100
}

/** Evenly spaced axis ticks, the window's start included and its end excluded
 *  (the end is labelled "now" by the caller). */
export function tickTimes(window: TimelineWindow, count: number): number[] {
  const step = (window.endMs - window.startMs) / count
  return Array.from({ length: count }, (_, i) => window.startMs + i * step)
}

/** One per-turn usage row as the gateway serves it. Only the timing fields matter here. */
export interface TurnRow { ts: string; duration_ms?: number }

/** Turn rows as spans, oldest first, each at least a sliver long. A row whose
 *  timestamp does not parse is dropped: an undatable turn cannot be placed. */
export function turnSpans(rows: readonly TurnRow[], window: TimelineWindow): TrackSpan[] {
  return rows
    .map(row => {
      const at = Date.parse(row.ts)
      if (Number.isNaN(at)) return null
      return spanOn(window, at, at + Math.max(0, row.duration_ms ?? 0))
    })
    .filter((s): s is TrackSpan => s !== null)
}

/** The query string `api.usageTurns` takes for this window. */
export function turnsRange(window: TimelineWindow): string {
  return `since=${Math.floor(window.startMs / 1000)}&until=${Math.ceil(window.endMs / 1000)}`
}
