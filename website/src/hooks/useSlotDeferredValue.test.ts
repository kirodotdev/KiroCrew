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

let deferredOverride: unknown = undefined
vi.mock('react', async importOriginal => {
  const actual = await importOriginal<typeof import('react')>()
  return {
    ...actual,
    useDeferredValue: <T,>(value: T): T => (deferredOverride === undefined ? value : (deferredOverride as T)),
  }
})

import { useSlotDeferredValue } from './useSlotDeferredValue'

afterEach(() => {
  deferredOverride = undefined
})

const starter = [{ id: 'starter-turn' }]
const fresh = [{ id: 'fresh-turn' }]

describe('useSlotDeferredValue', () => {
  it('returns the deferred value while it belongs to the same slot', () => {
    // React is still showing the last committed frame of THIS slot: keep it.
    deferredOverride = { slot: 'chat-1', value: starter }
    const { result } = renderHook(() => useSlotDeferredValue('chat-1', fresh))
    expect(result.current).toBe(starter)
  })

  it('renders the current value at once when the deferred frame is another slot', () => {
    // The lagging frame is the OUTGOING session's transcript (the #8526 ghost
    // rows). It must not paint under the new slot; the current list wins.
    deferredOverride = { slot: 'starter', value: starter }
    const { result } = renderHook(() => useSlotDeferredValue('chat-1', fresh))
    expect(result.current).toBe(fresh)
  })

  it('treats a missing slot as its own key', () => {
    deferredOverride = { slot: 'chat-1', value: starter }
    const { result } = renderHook(() => useSlotDeferredValue(null, fresh))
    expect(result.current).toBe(fresh)
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
