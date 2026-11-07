import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { crewNeedsYou, crewWorkCounts, longestStalledWork, nextCrewWorkDeadline, stalledWorkKeys, useCrewWorkClock } from './crewWork'
import type { CrewWorkItemFacts } from '../types'

const NOW = 1_800_000_000_000
const item = (over: Partial<CrewWorkItemFacts> = {}): CrewWorkItemFacts => ({
  worker: 'w', stale_at_ms: NOW + 60_000, ...over,
})
const facts = (items: CrewWorkItemFacts[], person = 0) => ({ open: items.length + person, person, items })
const idle = [{ key: 'w', running: false }]

describe('crewWorkCounts — the ledger read against the live rows', () => {
  it('no summary is no reading', () => {
    expect(crewWorkCounts(undefined, idle, NOW)).toBeNull()
  })

  it('an item before its stale moment is moving', () => {
    expect(crewWorkCounts(facts([item()]), idle, NOW)).toEqual({ person: 0, moving: 1, looping: 0, stalled: 0 })
  })

  it('an item past its stale moment is stalled', () => {
    expect(crewWorkCounts(facts([item({ stale_at_ms: NOW - 1 })]), idle, NOW))
      .toEqual({ person: 0, moving: 0, looping: 0, stalled: 1 })
  })

  it('an item the ledger never flags is moving', () => {
    expect(crewWorkCounts(facts([item({ stale_at_ms: 0 })]), idle, NOW)).toEqual({ person: 0, moving: 1, looping: 0, stalled: 0 })
  })

  it('a running worker is never stalled, however long it is quiet', () => {
    expect(crewWorkCounts(facts([item({ stale_at_ms: 1 })]), [{ key: 'w', running: true }], NOW))
      .toEqual({ person: 0, moving: 1, looping: 0, stalled: 0 })
  })

  it('a stall the conductor\'s own live loop will pick up is not counted stalled', () => {
    const handled = { ...facts([item({ stale_at_ms: NOW - 1 })]), conductor_looping: true }
    expect(crewWorkCounts(handled, idle, NOW)).toEqual({ person: 0, moving: 1, looping: 0, stalled: 0 })
    expect(stalledWorkKeys(handled, idle, NOW)).toBe('')
  })

  it('a remote worker (no key) follows its stale moment alone', () => {
    expect(crewWorkCounts(facts([item({ worker: '', stale_at_ms: NOW - 1 })]), idle, NOW))
      .toEqual({ person: 0, moving: 0, looping: 0, stalled: 1 })
  })

  it('a moving item whose worker loops (waiting on CI) is counted looping too', () => {
    const f = facts([item({ looping: true }), item({ worker: 'x' })])
    expect(crewWorkCounts(f, idle, NOW)).toEqual({ person: 0, moving: 2, looping: 1, stalled: 0 })
  })

  it('items on a person are counted apart', () => {
    expect(crewWorkCounts(facts([], 2), idle, NOW)).toEqual({ person: 2, moving: 0, looping: 0, stalled: 0 })
  })
})

describe('stalledWorkKeys / longestStalledWork — who has been quiet longest', () => {
  const rows = [{ key: 'a', running: false }, { key: 'b', running: false }, { key: 'c', running: true }]
  // The gateway stamps every already-stale item with one moment.
  const f = facts([
    item({ worker: 'a', stale_at_ms: NOW - 1 }),
    item({ worker: 'b', stale_at_ms: NOW - 1 }),
    item({ worker: 'c', stale_at_ms: NOW - 1 }),
    item({ worker: 'd', stale_at_ms: NOW + 60_000 }),
  ])

  it('lists the stalled items by the counting rule, skipping moving and running ones', () => {
    expect(stalledWorkKeys(f, rows, NOW)).toBe(`a@${NOW - 1} b@${NOW - 1}`)
    expect(stalledWorkKeys(undefined, rows, NOW)).toBe('')
  })

  it('ranks by each worker\'s own quiet time, not by the shared stale stamp', () => {
    const quiet: Record<string, number> = { a: NOW - 5 * 60_000, b: NOW - 40 * 60_000 }
    expect(longestStalledWork(stalledWorkKeys(f, rows, NOW), w => quiet[w] ?? NaN)).toEqual({ worker: 'b', since: NOW - 40 * 60_000 })
  })

  it('crewNeedsYou counts a worker in both sources once and distinct ones apart', () => {
    const listed = [{ key: 'a', group: 'approval' }, { key: 'b', group: 'waiting' }]
    expect(crewNeedsYou(listed, ['a'], 1, ['a'])).toBe(1)
    expect(crewNeedsYou(listed, ['a'], 1, ['b'])).toBe(2)
    // No keys from the gateway (or an unbound worker): the item adds its own count.
    expect(crewNeedsYou(listed, ['a'], 1, undefined)).toBe(2)
    expect(crewNeedsYou(listed, [], 2, ['a', ''])).toBe(2)
    // Before the list answers, the live rows' approval keys stand, deduped against the ledger.
    expect(crewNeedsYou(undefined, ['b', 'c'], 0, [])).toBe(2)
    expect(crewNeedsYou(undefined, ['b'], 1, ['b'])).toBe(1)
    // A worker whose turn runs again moved on: its ledger ask is not counted.
    expect(crewNeedsYou([{ key: 'a', group: 'working' }], [], 1, ['a'])).toBe(0)
    // A list that still shows the worker as waiting while its live row asks: one.
    expect(crewNeedsYou([{ key: 'b', group: 'waiting' }], ['b'], 1, ['b'])).toBe(1)
  })

  it('falls back to the stale moment for a worker with no known activity, and is null for none', () => {
    expect(longestStalledWork(`@${NOW - 7}`, () => NaN)).toEqual({ worker: '', since: NOW - 7 })
    expect(longestStalledWork('', () => NaN)).toBeNull()
  })
})

describe('the crew-work clock — a quiet board still turns Waiting into Stalled', () => {
  it('names the earliest stale moment still ahead', () => {
    expect(nextCrewWorkDeadline([facts([item({ stale_at_ms: NOW + 5 }), item({ stale_at_ms: NOW + 2 })]), undefined], NOW)).toBe(NOW + 2)
    expect(nextCrewWorkDeadline([facts([item({ stale_at_ms: NOW - 1 }), item({ stale_at_ms: 0 })])], NOW)).toBeNull()
  })

  afterEach(() => { vi.useRealTimers() })

  it('steps once that moment passes', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    const f = [facts([item({ stale_at_ms: NOW + 1_000 })])]
    const { result } = renderHook(() => useCrewWorkClock(f))
    expect(result.current).toBe(0)
    act(() => { vi.advanceTimersByTime(1_100) })
    expect(result.current).toBe(1)
  })
})
