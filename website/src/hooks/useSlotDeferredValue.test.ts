import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'

/**
 * Regression coverage for #8526: a deferred transcript must never outlive the
 * session that produced it.
 *
 * `useDeferredValue` is replaced with a controllable stand-in so the test can
 * hold the deferred frame back the way React does under urgent churn -- inside
 * `act()` the real hook flushes both renders at once, which is exactly the
 * window the bug lives in and the one the test has to be able to freeze.
 */

// `freeze` holds the deferred value at the FIRST frame the hook passes in,
// the way React keeps returning the last committed deferred value while every
// background render is interrupted by urgent work (a streaming session's
// per-frame flushes). The frame's shape is private to the hook, so the test
// captures it rather than spelling it.
let freeze = false
let frozen: { set: boolean; value: unknown } = { set: false, value: undefined }
vi.mock('react', async importOriginal => {
  const actual = await importOriginal<typeof import('react')>()
  return {
    ...actual,
    useDeferredValue: <T,>(value: T): T => {
      if (freeze) {
        if (!frozen.set) frozen = { set: true, value }
        return frozen.value as T
      }
      return value
    },
  }
})

import { useSlotDeferredValue } from './useSlotDeferredValue'

afterEach(() => {
  freeze = false
  frozen = { set: false, value: undefined }
})

const starter = [{ id: 'starter-turn' }]
const fresh = [{ id: 'fresh-turn' }]

describe('useSlotDeferredValue', () => {
  const mount = (slot: string | null, value: unknown) =>
    renderHook(({ slot, value }) => useSlotDeferredValue(slot, value), { initialProps: { slot, value } })

  it('returns the deferred value while it belongs to the same slot', () => {
    // React is still showing the last committed frame of THIS slot: keep it.
    freeze = true
    const { result, rerender } = mount('chat-1', starter)
    rerender({ slot: 'chat-1', value: fresh })
    expect(result.current).toBe(starter)
  })

  it('renders the current value at once when the deferred frame is another slot', () => {
    // The lagging frame is the OUTGOING session's transcript (the #8526 ghost
    // rows). It must not paint under the new slot; the current list wins.
    freeze = true
    const { result, rerender } = mount('starter', starter)
    rerender({ slot: 'chat-1', value: fresh })
    expect(result.current).toBe(fresh)
  })

  it('treats a missing slot as its own key', () => {
    freeze = true
    const { result, rerender } = mount('chat-1', starter)
    rerender({ slot: null, value: fresh })
    expect(result.current).toBe(fresh)
  })

  it('does not paint a frame left over from an earlier visit to the same slot', () => {
    // Send in A, peek at B, come back to A. If React never committed B's
    // deferred render, the deferred frame is still the one A held when the
    // user LEFT it: same slot key, but the transcript from before the send.
    // It must not come back as A's current transcript.
    freeze = true
    const { result, rerender } = renderHook(({ slot, value }) => useSlotDeferredValue(slot, value), {
      initialProps: { slot: 'chat-a', value: starter },
    })
    expect(result.current).toBe(starter)
    const other = [{ id: 'b-turn' }]
    rerender({ slot: 'chat-b', value: other })
    expect(result.current).toBe(other)
    rerender({ slot: 'chat-a', value: fresh })
    expect(result.current).toBe(fresh)
  })

  it('keeps deferring within one visit to a slot', () => {
    // The streaming case the deferral exists for: same slot, no switch, so
    // the last committed frame stays up while the new one renders.
    freeze = true
    const { result, rerender } = renderHook(({ slot, value }) => useSlotDeferredValue(slot, value), {
      initialProps: { slot: 'chat-a', value: starter },
    })
    rerender({ slot: 'chat-a', value: fresh })
    expect(result.current).toBe(starter)
  })

  it('passes the value straight through once the deferred frame has caught up', () => {
    const { result, rerender } = renderHook(({ slot, value }) => useSlotDeferredValue(slot, value), {
      initialProps: { slot: 'chat-1', value: fresh },
    })
    expect(result.current).toBe(fresh)
    const next = [{ id: 'next-turn' }]
    rerender({ slot: 'chat-1', value: next })
    expect(result.current).toBe(next)
  })
})
