import { describe, expect, it } from 'vitest'
import { axisTicks, barBox, LINEAR_MS, pct, roundUpToStep, scaledBox, scaledTicks, timelineScale, timelineWindow } from './crewTimeline'
import type { CrewTimeline } from '../types'

const LANE = { key: 'l', title: '', agent: '', model: '', model_source: 'default' as const, role: 'worker' as const, status: 'idle' as const, running: false, waiting: false, segments: [], marks: [], credits: 0, first_ts: '', last_ts: '', dropped_segments: 0 }
const at = (s: number) => new Date(Date.UTC(2026, 9, 21, 3, 0, s)).toISOString()
const base = (extra: Partial<CrewTimeline>): CrewTimeline => ({
  root: 'r', now: at(100), live: false, started: at(0), ended: at(60), elapsed_ms: 60_000,
  credits: 0, lanes: [], handoffs: [], gaps: [], ...extra,
})

describe('crewTimeline layout', () => {
  it('a finished run spans its first row to its last', () => {
    const w = timelineWindow(base({}))!
    expect([w.start, w.end]).toEqual([Date.parse(at(0)), Date.parse(at(60))])
  })

  it('a live run spans past now to a round step, and never shorter than the floor', () => {
    // 100s * 1.25 = 125s -> 180s (60s steps).
    const live = timelineWindow(base({ live: true, now: at(100) }))!
    expect(live.end - live.start).toBe(180_000)
    // A young run: the 30s floor * 1.25 = 37.5s -> 40s (10s steps).
    const young = timelineWindow(base({ live: true, now: at(2) }))!
    expect(young.end - young.start).toBe(40_000)
    expect(roundUpToStep(60_000)).toBe(60_000)
  })

  it('no recorded start means nothing to draw', () => {
    expect(timelineWindow(base({ started: '' }))).toBeNull()
  })

  it('places bars as shares of the window and clamps to the track', () => {
    const w = { start: 0, end: 1000 }
    expect(pct(w, 250)).toBe(25)
    expect(pct(w, -5)).toBe(0)
    expect(pct(w, 5000)).toBe(100)
    const box = barBox({ start: Date.parse(at(0)), end: Date.parse(at(60)) }, at(15), at(30))!
    expect(box.left).toBeCloseTo(25)
    expect(box.width).toBeCloseTo(25)
    expect(barBox(w, 'x', at(1))).toBeNull()
  })

  it('a bar whose end is before its start has no width', () => {
    const box = barBox({ start: Date.parse(at(0)), end: Date.parse(at(60)) }, at(30), at(10))!
    expect(box.width).toBe(0)
  })

  it('ticks on round steps', () => {
    expect(axisTicks({ start: 0, end: 60_000 })).toEqual([0, 15_000, 30_000, 45_000, 60_000])
    expect(axisTicks({ start: 0, end: 0 })).toEqual([0])
  })

  it('a run of short stretches keeps true time', () => {
    const data = base({ ended: at(60), lanes: [{ ...LANE, segments: [{ kind: 'tool', start: at(15), end: at(30), end_bound: 'recorded', label: '' }] }] })
    const scale = timelineScale(data, timelineWindow(data)!)
    expect(scale.compressed).toBe(false)
    expect(scale.toPct(Date.parse(at(15)))).toBeCloseTo(25)
  })

  it('a long stretch is drawn shorter, so short bars keep their room, and order holds', () => {
    const end = Date.parse(at(0)) + 30 * 60_000
    const data = base({ ended: new Date(end).toISOString(), lanes: [{ ...LANE, segments: [
      { kind: 'tool', start: at(0), end: at(10), end_bound: 'recorded', label: '' },
      { kind: 'waiting', start: at(10), end: new Date(end).toISOString(), end_bound: 'recorded', label: '' },
    ] }] })
    const window = timelineWindow(data)!
    const scale = timelineScale(data, window)
    expect(scale.compressed).toBe(true)
    const linear = barBox(window, at(0), at(10))!.width
    const scaled = scaledBox(scale, at(0), at(10))!.width
    expect(scaled).toBeGreaterThan(linear * 5)
    const xs = [at(0), at(5), at(10), at(500), new Date(end).toISOString()].map(v => scale.toPct(Date.parse(v)))
    expect([...xs].sort((a, b) => a - b)).toEqual(xs)
    expect(LINEAR_MS).toBe(60_000)
  })

  it('axis labels on a bent scale never crowd', () => {
    const end = Date.parse(at(0)) + 30 * 60_000
    const data = base({ ended: new Date(end).toISOString() })
    const window = timelineWindow(data)!
    const ticks = scaledTicks(window, timelineScale(data, window))
    for (let i = 1; i < ticks.length; i += 1) expect(ticks[i].pct - ticks[i - 1].pct).toBeGreaterThanOrEqual(14)
    expect(ticks[0].at).toBe(0)
  })
})
