// Guards for the off-screen measure farm: the idle sweep that converts
// estimate-height rows into measured rows so no height correction ever lands
// under the reader's finger (measured: ~180 content-anchored jumps in a 12s
// 390px back-scroll on an unmeasured transcript, 0 once measured).

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, renderHook, act } from '@testing-library/react'
import React from 'react'

import { MeasureFarm, FARM_TICK_MS } from '../hooks/virtualizer/MeasureFarm'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import { PierreFarmHoldContext, useStagedMount, __resetStagingForTests, __stagedWaitingCount, EAGER_ROWS } from '../components/pierreStaging'

describe('MeasureFarm', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  const scroller = () => null

  /** Runs one farm cycle: tick (pick) + double rAF (measure). */
  const runCycle = () => {
    act(() => { vi.advanceTimersByTime(FARM_TICK_MS + 1) })
    act(() => { vi.advanceTimersByTime(32) }) // two rAF frames under fake timers
  }

  it('sweeps upward from the origin, measures a batch, and records real heights', () => {
    const measured = new Set<number>()
    const record = vi.fn((index: number, _key: string, px: number) => {
      expect(px).toBeGreaterThan(0)
      measured.add(index)
      return true
    })
    render(
      <MeasureFarm
        enabled
        count={6}
        originIndex={5}
        isMeasured={(i) => measured.has(i)}
        keyAt={(i) => `k${i}`}
        record={record}
        renderItem={(i) => <span>row {i}</span>}
        scrollerEl={scroller}
        measureEl={(el) => 100 + Number(el.dataset.farmIndex)}
      />,
    )
    runCycle()
    // No scroll has ever happened (deep idle from mount), so the sweep takes
    // the deep batch and covers all six rows in one pass — origin-first,
    // walking UP: above the reader is where unmeasured rows hurt.
    expect(record.mock.calls.map((c) => c[0])).toEqual([5, 4, 3, 2, 1, 0])
    expect(record.mock.calls.map((c) => c[2])).toEqual([105, 104, 103, 102, 101, 100])
    runCycle()
    // Everything measured: no re-measure churn.
    expect(record.mock.calls).toHaveLength(6)
  })

  it('does not pick a batch while paused, and never records px<=0', () => {
    const record = vi.fn(() => true)
    const { rerender } = render(
      <MeasureFarm
        enabled
        paused
        count={3}
        originIndex={2}
        isMeasured={() => false}
        keyAt={(i) => `k${i}`}
        record={record}
        renderItem={(i) => <span>{i}</span>}
        scrollerEl={scroller}
        measureEl={() => 0}
      />,
    )
    runCycle()
    expect(record).not.toHaveBeenCalled()
    // Unpause, but the measurer answers 0 (jsdom-like): still no record — a
    // zero is not a measurement, and caching it would be worse than estimating.
    rerender(
      <MeasureFarm
        enabled
        paused={false}
        count={3}
        originIndex={2}
        isMeasured={() => false}
        keyAt={(i) => `k${i}`}
        record={record}
        renderItem={(i) => <span>{i}</span>}
        scrollerEl={scroller}
        measureEl={() => 0}
      />,
    )
    runCycle()
    expect(record).not.toHaveBeenCalled()
  })
})

describe('farmRecord identity revalidation (useVirtualChat)', () => {
  it('drops a write whose key no longer matches the item at that index', () => {
    // 60 rows: the chat window mounts the TAIL, so rows 0/1 sit outside
    // the mounted window and exercise the farm's own identity gate rather
    // than the mounted-row exclusion (pinned separately below).
    const items = Array.from({ length: 60 }, (_, i) => ({ id: `m${i}` }))
    const { result } = renderHook(() =>
      useVirtualChat({
        items,
        getKey: (it: { id: string }) => it.id,
        sessionId: `farm-test-${Math.random()}`,
      }),
    )
    expect(result.current.farmRowMounted(0)).toBe(false)
    // Stale key (indices shifted between pick and measure): dropped.
    expect(result.current.farmRecord(0, 'm1', 120)).toBe(false)
    expect(result.current.farmIsMeasured(0)).toBe(false)
    // Matching identity: recorded.
    expect(result.current.farmRecord(0, 'm0', 120)).toBe(true)
    expect(result.current.farmIsMeasured(0)).toBe(true)
    // Zero is never a measurement.
    expect(result.current.farmRecord(1, 'm1', 0)).toBe(false)
  })

  it('drops a write for a row the live window has mounted (the RO owns it)', () => {
    // The farm renders a row in its DEFAULT disclosure state, which can
    // differ from the live row's by thousands of px; two writers on one
    // cache slot oscillated (bottom rig: persistent ±2666px against a
    // parked reader). A mounted row's height comes from the RO only.
    const items = Array.from({ length: 60 }, (_, i) => ({ id: `m${i}` }))
    const { result } = renderHook(() =>
      useVirtualChat({
        items,
        getKey: (it: { id: string }) => it.id,
        sessionId: `farm-test-${Math.random()}`,
      }),
    )
    const tail = 59
    expect(result.current.farmRowMounted(tail)).toBe(true)
    expect(result.current.farmRecord(tail, 'm59', 240)).toBe(false)
    expect(result.current.farmIsMeasured(tail)).toBe(false)
  })
})

describe('PierreFarmHoldContext', () => {
  beforeEach(() => { vi.useFakeTimers(); __resetStagingForTests() })
  afterEach(() => { vi.useRealTimers() })

  it('a farm render never queues, never latches, and never reports ready', () => {
    const farmWrapper = ({ children }: { children: React.ReactNode }) => (
      <PierreFarmHoldContext.Provider value={true}>{children}</PierreFarmHoldContext.Provider>
    )
    const { result } = renderHook(() => useStagedMount(false, 'farm-key'), { wrapper: farmWrapper })
    expect(result.current).toBe(false)
    expect(__stagedWaitingCount()).toBe(0)
    // The REAL mount later must still pay the normal admission path: the farm
    // must not have latched the key. Exhaust the eager budget first so a
    // latched key would be distinguishable (latched = ready immediately).
    for (let i = 0; i < EAGER_ROWS; i++) renderHook(() => useStagedMount(false, `warm-${i}`))
    const { result: real } = renderHook(() => useStagedMount(false, 'farm-key'))
    expect(real.current).toBe(false)
    expect(__stagedWaitingCount()).toBe(1)
  })
})

describe('MeasureFarm with an incomplete input', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('does nothing instead of throwing out of its timer', () => {
    const record = vi.fn(() => true)
    // A caller that omits `isMeasured`. The prop type declares it required, so
    // production cannot reach this -- a partial test double can, and the throw
    // lands in a setInterval callback where no render catches it: every
    // assertion passes and the run still fails on an unhandled rejection.
    const partial = { isMeasured: undefined } as unknown as { isMeasured: (i: number) => boolean }
    expect(() => {
      render(
        <MeasureFarm
          enabled
          count={6}
          originIndex={5}
          isMeasured={partial.isMeasured}
          keyAt={(i) => `k${i}`}
          record={record}
          renderItem={(i) => <span>row {i}</span>}
          scrollerEl={() => null}
          measureEl={() => 100}
        />,
      )
      act(() => { vi.advanceTimersByTime(FARM_TICK_MS + 1) })
      act(() => { vi.advanceTimersByTime(32) })
    }).not.toThrow()
    // And it must not fall back to "nothing is measured" -- that would walk the
    // farm over the whole list, the most expensive response to missing input.
    expect(record).not.toHaveBeenCalled()
  })
})
