import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, renderHook } from '@testing-library/react'
import { createElement, Suspense, useLayoutEffect } from 'react'

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
/** While set, the stand-in returns the input, the way React's background pass does. */
let passThrough = false
let frozen: { set: boolean; value: unknown } = { set: false, value: undefined }
/** Every input the hook hands to useDeferredValue, in render order. */
const inputs: unknown[] = []
vi.mock('react', async importOriginal => {
  const actual = await importOriginal<typeof import('react')>()
  return {
    ...actual,
    useDeferredValue: <T,>(value: T): T => {
      inputs.push(value)
      if (passThrough) return value
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
  inputs.length = 0
  passThrough = false
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

  describe('sameShape', () => {
    type V = { tail: string; rows: number }
    const grewInPlace = (a: V, b: V) => a.rows === b.rows
    const mountShaped = (value: V) =>
      renderHook(({ slot, value }) => useSlotDeferredValue(slot, value, grewInPlace), { initialProps: { slot: 'chat-a', value } })

    it('renders an in-place change at once and hands the deferral nothing new', () => {
      freeze = true
      const first = { tail: 'a', rows: 2 }
      const { result, rerender } = mountShaped(first)
      for (const tail of ['ab', 'abc', 'abcd']) {
        const next = { tail, rows: 2 }
        rerender({ slot: 'chat-a', value: next })
        expect(result.current).toBe(next)
      }
      // One input for the whole stream: React has no background pass to run.
      expect(new Set(inputs).size).toBe(1)
    })

    it('still defers a change of shape, and keeps showing the old value while it is pending', () => {
      freeze = true
      const first = { tail: 'a', rows: 2 }
      const { result, rerender } = mountShaped(first)
      rerender({ slot: 'chat-a', value: { tail: 'a', rows: 3 } })
      expect(result.current).toBe(first)
      // A tail update on top of the pending change does not jump ahead of it.
      rerender({ slot: 'chat-a', value: { tail: 'ab', rows: 3 } })
      expect(result.current).toBe(first)
    })

    it('keeps the last live value, not the frame the stream started from, while a later change of shape is pending', () => {
      freeze = true
      const { result, rerender } = mountShaped({ tail: 'a', rows: 2 })
      const grown = { tail: 'abc', rows: 2 }
      rerender({ slot: 'chat-a', value: { tail: 'ab', rows: 2 } })
      rerender({ slot: 'chat-a', value: grown })
      // A new row arrives; its deferred render has not committed yet.
      rerender({ slot: 'chat-a', value: { tail: 'abc', rows: 3 } })
      expect(result.current).toBe(grown)
      // Tail growth on top of the pending row still does not roll the reply back.
      rerender({ slot: 'chat-a', value: { tail: 'abcd', rows: 3 } })
      expect(result.current).toBe(grown)
    })

    it('compares against the last value handed to the deferral, not the last one rendered', () => {
      const first = { tail: 'a', rows: 2 }
      const { result, rerender } = mountShaped(first)
      rerender({ slot: 'chat-a', value: { tail: 'ab', rows: 2 } })
      const grown = { tail: 'ab', rows: 3 }
      rerender({ slot: 'chat-a', value: grown })
      expect(result.current).toBe(grown)
      expect(inputs.at(-1)).not.toBe(inputs[0])
    })
  })

  it('ignores a background render that React throws away before it commits', async () => {
    type V = { tail: string; rows: number }
    const grewInPlace = (a: V, b: V) => a.rows === b.rows
    const shown: V[] = []
    let suspend: Promise<void> | null = null
    function Host({ value }: { value: V }) {
      const out = useSlotDeferredValue('chat-a', value, grewInPlace)
      useLayoutEffect(() => { shown.push(out) })
      return null
    }
    function MaybeSuspend() {
      if (suspend) throw suspend
      return null
    }
    const view = (value: V) => createElement(Suspense, { fallback: null }, createElement(Host, { value }), createElement(MaybeSuspend))
    freeze = true
    const { rerender } = render(view({ tail: 'a', rows: 2 }))
    const live = { tail: 'abc', rows: 2 }
    rerender(view({ tail: 'ab', rows: 2 }))
    rerender(view(live))
    const grown = { tail: 'abc', rows: 3 }
    rerender(view(grown))
    expect(shown.at(-1)).toBe(live)
    // The background pass for the new row renders, then is discarded: a sibling
    // suspends in the same render, so nothing from it commits.
    passThrough = true
    suspend = new Promise(() => {})
    rerender(view(grown))
    passThrough = false
    suspend = null
    // The next chunk lands on top of the still-pending row.
    rerender(view({ tail: 'abcd', rows: 3 }))
    expect(shown.at(-1)).toBe(live)
  })
})
