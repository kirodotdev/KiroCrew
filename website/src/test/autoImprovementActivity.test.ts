import { describe, it, expect } from 'vitest'

import { activityLine } from '../apps/auto-improvement/SetupPanel'

// The shapes the backend actually emits, captured from a live run. Handing any of
// these to React as a raw child is what produced the minified React #31 crash
// ("object with keys {t, agent}") that made the page fail to load — the component
// had typed the feed as string[] when every item is an object.
describe('activityLine (run activity feed)', () => {
  it('renders a plain note', () => {
    const out = activityLine({ t: 1785469717.28, note: 'run run-1785469705 starting' })
    expect(out).toContain('run run-1785469705 starting')
  })

  it('renders a stage transition with its cycle', () => {
    const out = activityLine({ t: 1785469717.28, cycle: 1, stage: 'profile' })
    expect(out).toContain('cycle 1')
    expect(out).toContain('profile')
  })

  it('renders discovery counts when the stage carries them', () => {
    const out = activityLine({ t: 1, cycle: 1, stage: 'propose', discovered: 2, fresh: 2 })
    expect(out).toContain('discovered 2')
    expect(out).toContain('fresh 2')
  })

  it('flattens a nested agent event into tool + detail', () => {
    const out = activityLine({
      t: 1785469723.84,
      agent: { kind: 'tool', tool: 'read', detail: 'Reading board.py:1' },
    })
    expect(out).toContain('tool:read')
    expect(out).toContain('Reading board.py:1')
  })

  it('surfaces an error item as an error', () => {
    expect(activityLine({ t: 1, error: 'boom' })).toContain('error: boom')
  })

  it('never returns a non-string, for ANY shape including unknown ones', () => {
    // The whole point: an unrecognized future shape must degrade to text rather
    // than reaching React as an object and crashing the page.
    const shapes: unknown[] = [
      {},
      { t: 1 },
      { t: 1, somethingNew: { nested: true } },
      { t: 1, proposers: { fanned: 2, survived_gate: 0 } },
      { t: 1, budget: { hours_used: 0.15 } },
    ]
    for (const s of shapes) {
      const out = activityLine(s as Parameters<typeof activityLine>[0])
      expect(typeof out).toBe('string')
      expect(out.length).toBeGreaterThan(0)
    }
  })
})
