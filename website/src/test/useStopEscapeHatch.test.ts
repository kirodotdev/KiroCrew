import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useStopEscapeHatch, KILLING_ESCAPE_MS } from '../hooks/useStopEscapeHatch'

describe('useStopEscapeHatch', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('returns escaped=false initially when stopState is undefined', () => {
    const { result } = renderHook(() => useStopEscapeHatch(undefined))
    expect(result.current.escaped).toBe(false)
  })

  it('returns escaped=false initially when stopState is idle', () => {
    const { result } = renderHook(() => useStopEscapeHatch('idle'))
    expect(result.current.escaped).toBe(false)
  })

  it('returns escaped=false initially when stopState is killing', () => {
    const { result } = renderHook(() => useStopEscapeHatch('killing'))
    expect(result.current.escaped).toBe(false)
  })

  it('sets escaped=true after KILLING_ESCAPE_MS when in killing state', () => {
    const { result } = renderHook(() => useStopEscapeHatch('killing'))
    expect(result.current.escaped).toBe(false)

    act(() => { vi.advanceTimersByTime(KILLING_ESCAPE_MS - 1) })
    expect(result.current.escaped).toBe(false)

    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current.escaped).toBe(true)
  })

  it('resets escaped when stopState transitions away from killing', () => {
    const { result, rerender } = renderHook(
      ({ state }) => useStopEscapeHatch(state),
      { initialProps: { state: 'killing' as const } },
    )

    act(() => { vi.advanceTimersByTime(KILLING_ESCAPE_MS) })
    expect(result.current.escaped).toBe(true)

    rerender({ state: 'idle' as const })
    expect(result.current.escaped).toBe(false)
  })

  it('resets timer when stopState goes back to killing', () => {
    const { result, rerender } = renderHook(
      ({ state }) => useStopEscapeHatch(state),
      { initialProps: { state: 'killing' as const } },
    )

    // Advance partway
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(result.current.escaped).toBe(false)

    // Transition away and back
    rerender({ state: 'idle' as const })
    rerender({ state: 'killing' as const })

    // Timer restarted — still need full timeout
    act(() => { vi.advanceTimersByTime(KILLING_ESCAPE_MS - 1) })
    expect(result.current.escaped).toBe(false)

    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current.escaped).toBe(true)
  })

  it('does NOT start a timer for soft_pending state', () => {
    const { result } = renderHook(() => useStopEscapeHatch('soft_pending'))
    act(() => { vi.advanceTimersByTime(KILLING_ESCAPE_MS * 2) })
    expect(result.current.escaped).toBe(false)
  })

  it('respects custom timeout', () => {
    const { result } = renderHook(() => useStopEscapeHatch('killing', 5000))
    act(() => { vi.advanceTimersByTime(4999) })
    expect(result.current.escaped).toBe(false)
    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current.escaped).toBe(true)
  })

  it('exports the default timeout constant as 15000ms', () => {
    expect(KILLING_ESCAPE_MS).toBe(15_000)
  })
})
