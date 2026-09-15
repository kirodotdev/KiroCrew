/**
 * Pure-math tests for the stale-session collapse split (pages/staleCollapse).
 * The component-level behavior (expander row, exemptions wiring, menu) is
 * covered in ChatSidebar.staleCollapse.test.tsx; these pin the predicate.
 */
import { describe, it, expect } from 'vitest'
import { DEFAULT_STALE_COLLAPSE_MS, STALE_COLLAPSE_PRESETS_MS, splitStaleSlots } from '../pages/staleCollapse'

interface Row { key: string; ts: number }

const NOW = 1_000_000_000_000
const DAY = 24 * 60 * 60 * 1000
const row = (key: string, ageMs: number): Row => ({ key, ts: NOW - ageMs })
const split = (list: Row[], thresholdMs: number, exempt: ReadonlySet<string> = new Set()) =>
  splitStaleSlots(list, thresholdMs, NOW, r => r.ts, r => exempt.has(r.key))

describe('splitStaleSlots', () => {
  it('partitions by age against the threshold, preserving order in both halves', () => {
    const list = [row('a', 1 * DAY), row('b', 3 * DAY), row('c', 0), row('d', 5 * DAY)]
    const { fresh, stale } = split(list, 2 * DAY)
    expect(fresh.map(r => r.key)).toEqual(['a', 'c'])
    expect(stale.map(r => r.key)).toEqual(['b', 'd'])
  })

  it('treats a row exactly at the threshold as fresh (strictly older collapses)', () => {
    const { fresh, stale } = split([row('edge', 2 * DAY)], 2 * DAY)
    expect(fresh.map(r => r.key)).toEqual(['edge'])
    expect(stale).toEqual([])
  })

  it('threshold 0 disables the split entirely — even ancient rows stay fresh', () => {
    const list = [row('old', 400 * DAY)]
    const { fresh, stale } = split(list, 0)
    expect(fresh.map(r => r.key)).toEqual(['old'])
    expect(stale).toEqual([])
  })

  it('never collapses a row it cannot date (lastActivityMs 0)', () => {
    const { fresh, stale } = split([{ key: 'undated', ts: 0 }], 2 * DAY)
    expect(fresh.map(r => r.key)).toEqual(['undated'])
    expect(stale).toEqual([])
  })

  it('an exempt row stays fresh regardless of age', () => {
    const list = [row('pinned-old', 30 * DAY), row('plain-old', 30 * DAY)]
    const { fresh, stale } = split(list, 2 * DAY, new Set(['pinned-old']))
    expect(fresh.map(r => r.key)).toEqual(['pinned-old'])
    expect(stale.map(r => r.key)).toEqual(['plain-old'])
  })

  it('presets include off (0), the default (7 days) and a 14-day option, ascending', () => {
    expect(STALE_COLLAPSE_PRESETS_MS[0]).toBe(0)
    expect(STALE_COLLAPSE_PRESETS_MS).toContain(DEFAULT_STALE_COLLAPSE_MS)
    expect(DEFAULT_STALE_COLLAPSE_MS).toBe(7 * DAY)
    expect(STALE_COLLAPSE_PRESETS_MS).toContain(14 * DAY)
    expect([...STALE_COLLAPSE_PRESETS_MS]).toEqual([...STALE_COLLAPSE_PRESETS_MS].sort((a, b) => a - b))
  })
})
