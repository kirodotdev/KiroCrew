import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { crewWorkCounts, nextCrewWorkDeadline, useCrewWorkClock } from './crewWork'
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
    expect(crewWorkCounts(facts([item()]), idle, NOW)).toEqual({ person: 0, moving: 1, stalled: 0 })
  })

  it('an item past its stale moment is stalled', () => {
    expect(crewWorkCounts(facts([item({ stale_at_ms: NOW - 1 })]), idle, NOW))
      .toEqual({ person: 0, moving: 0, stalled: 1 })
  })

  it('an item the ledger never flags is moving', () => {
    expect(crewWorkCounts(facts([item({ stale_at_ms: 0 })]), idle, NOW)).toEqual({ person: 0, moving: 1, stalled: 0 })
  })

  it('a running worker is never stalled, however long it is quiet', () => {
    expect(crewWorkCounts(facts([item({ stale_at_ms: 1 })]), [{ key: 'w', running: true }], NOW))
      .toEqual({ person: 0, moving: 1, stalled: 0 })
  })

  it('a remote worker (no key) follows its stale moment alone', () => {
    expect(crewWorkCounts(facts([item({ worker: '', stale_at_ms: NOW - 1 })]), idle, NOW))
      .toEqual({ person: 0, moving: 0, stalled: 1 })
  })

  it('items on a person are counted apart', () => {
    expect(crewWorkCounts(facts([], 2), idle, NOW)).toEqual({ person: 2, moving: 0, stalled: 0 })
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
