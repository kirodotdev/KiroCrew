import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import { useLatchedRunning, RUNNING_LATCH_MS } from './useLatchedRunning'

/**
 * The latch holds a `false` back so a broadcast that catches the agent between
 * two tool calls does not collapse a live turn, and it must do that WITHOUT
 * outliving the session it was raised for.
 *
 * Leaving a running session used to keep the latch up while the next session's
 * transcript rendered, which stamped that session's trailing turn incomplete
 * and painted its steps with no fold for the whole window. Fake timers are what
 * make the window observable: the flash lives entirely inside it, so a test
 * that let it elapse could not see the difference.
 */

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

const latch = (slot: string | null, running: boolean) =>
  renderHook(({ slot, running }) => useLatchedRunning(slot, running), { initialProps: { slot, running } })

describe('useLatchedRunning', () => {
  it('holds a false for the flap window within one session', () => {
    const { result, rerender } = latch('chat-1', true)
    expect(result.current).toBe(true)

    // The gap between two tool calls of the SAME turn: the display layer must
    // not believe it, or the turn collapses under a reader parked at the bottom.
    rerender({ slot: 'chat-1', running: false })
    expect(result.current).toBe(true)

    act(() => { vi.advanceTimersByTime(RUNNING_LATCH_MS) })
    expect(result.current).toBe(false)
  })

  it('applies a true immediately, with no hold', () => {
    const { result, rerender } = latch('chat-1', false)
    expect(result.current).toBe(false)
    rerender({ slot: 'chat-1', running: true })
    expect(result.current).toBe(true)
  })

  it('reports the incoming session state at once when the slot changes', () => {
    const { result, rerender } = latch('chat-1', true)
    expect(result.current).toBe(true)

    // The switch itself, with the window untouched. A raised latch belongs to
    // the session being left; reporting it here is the expanded frame.
    rerender({ slot: 'chat-2', running: false })
    expect(result.current).toBe(false)
  })

  it('keeps the incoming session state for the whole window after a switch', () => {
    const { result, rerender } = latch('chat-1', true)
    rerender({ slot: 'chat-2', running: false })

    // Sampled across the window the old latch would have covered: no frame of
    // it may read as running, including the instant it would have expired.
    for (const step of [1, RUNNING_LATCH_MS / 2, RUNNING_LATCH_MS]) {
      act(() => { vi.advanceTimersByTime(step) })
      expect(result.current).toBe(false)
    }
  })

  it('switching INTO a running session reports running at once', () => {
    const { result, rerender } = latch('chat-1', false)
    rerender({ slot: 'chat-2', running: true })
    expect(result.current).toBe(true)
  })

  it('treats a missing slot as its own key', () => {
    const { result, rerender } = latch('chat-1', true)
    rerender({ slot: null, running: false })
    expect(result.current).toBe(false)
  })

  it('does not revive the previous session\'s latch when returning to it inside the window', () => {
    const { result, rerender } = latch('chat-1', true)
    // Leave A while it is running, then come back before the window has
    // elapsed, after A has stopped. The frame A raised must have been replaced
    // by the switch, not left waiting to be read again.
    rerender({ slot: 'chat-2', running: false })
    act(() => { vi.advanceTimersByTime(RUNNING_LATCH_MS / 4) })
    rerender({ slot: 'chat-1', running: false })
    expect(result.current).toBe(false)
    act(() => { vi.advanceTimersByTime(RUNNING_LATCH_MS) })
    expect(result.current).toBe(false)
  })

  it('re-latches for the new session once it settles', () => {
    const { result, rerender } = latch('chat-1', true)
    rerender({ slot: 'chat-2', running: true })
    act(() => { vi.advanceTimersByTime(RUNNING_LATCH_MS) })
    expect(result.current).toBe(true)

    // The new session now owns the latch, so ITS flap is held the same way.
    rerender({ slot: 'chat-2', running: false })
    expect(result.current).toBe(true)
    act(() => { vi.advanceTimersByTime(RUNNING_LATCH_MS) })
    expect(result.current).toBe(false)
  })
})
