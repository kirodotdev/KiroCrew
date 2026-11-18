import { describe, expect, it } from 'vitest'
import { pointOn, spanOn, tickTimes, timelineWindow, turnSpans, turnsRange } from './homeTimeline'

const NOW = Date.parse('2026-11-17T12:00:00Z')
const H = 3_600_000
const win = timelineWindow(12, NOW)

describe('timelineWindow and ticks', () => {
  it('ends now and starts the chosen hours earlier', () => {
    expect(win).toEqual({ startMs: NOW - 12 * H, endMs: NOW })
  })
  it('spaces ticks evenly from the start, leaving the end for the caller', () => {
    expect(tickTimes(win, 4)).toEqual([NOW - 12 * H, NOW - 9 * H, NOW - 6 * H, NOW - 3 * H])
  })
})

describe('spanOn and pointOn', () => {
  it('places a span by percentage and clamps it to the window', () => {
    expect(spanOn(win, NOW - 6 * H, NOW - 3 * H)).toEqual({ leftPct: 50, widthPct: 25 })
    // Started before the window: begins at 0.
    expect(spanOn(win, NOW - 20 * H, NOW - 9 * H)).toEqual({ leftPct: 0, widthPct: 25 })
    // Still running: ends at the window's end.
    expect(spanOn(win, NOW - 3 * H, NOW + H)).toEqual({ leftPct: 75, widthPct: 25 })
  })
  it('keeps a brief turn visible as a sliver and drops one outside the window', () => {
    expect(spanOn(win, NOW - 6 * H, NOW - 6 * H + 1000)?.widthPct).toBe(0.4)
    expect(spanOn(win, NOW - 30 * H, NOW - 20 * H)).toBeNull()
    expect(spanOn(win, NOW + H, NOW + 2 * H)).toBeNull()
  })
  it('places an instant, or not at all', () => {
    expect(pointOn(win, NOW - 3 * H)).toBe(75)
    expect(pointOn(win, NOW)).toBe(100)
    expect(pointOn(win, NOW - 13 * H)).toBeNull()
  })
})

describe('turnSpans and turnsRange', () => {
  it('turns usage rows into spans and drops an undatable row', () => {
    const spans = turnSpans([
      { ts: new Date(NOW - 6 * H).toISOString(), duration_ms: 3 * H },
      { ts: 'not a time', duration_ms: 5 },
      { ts: new Date(NOW - H).toISOString() },
    ], win)
    expect(spans).toHaveLength(2)
    expect(spans[0]).toEqual({ leftPct: 50, widthPct: 25 })
    expect(spans[1].leftPct).toBeCloseTo(91.667, 2)
    expect(spans[1].widthPct).toBe(0.4)
  })
  it('spells the window as the usage route reads it', () => {
    expect(turnsRange(win)).toBe(`since=${(NOW - 12 * H) / 1000}&until=${NOW / 1000}`)
  })
})
