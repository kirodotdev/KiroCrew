/**
 * THE INVARIANT: whatever is loading, the reader's eye line does not move.
 *
 * Growth above them extends upward, growth below extends downward, and the row
 * they are looking at stays where it is. On iOS Safari there is no native scroll
 * anchoring, so the only way to make a mid-gesture reprice invisible is a
 * `scrollTop` write — and a write issued while a finger or momentum owns the
 * scroller either fights the gesture or lands a frame late. Measured on the
 * device: one +108 CSS px step, an exact −108 step ~100ms later.
 *
 * So a released reader's geometry commit WAITS. Nothing above them changes while
 * they move; the whole reprice lands in one compensated commit once they stop.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, SCROLL_SETTLE_MS, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'
import { geometryCommitDeferred } from '../hooks/virtualizer/FollowController'

describe('geometryCommitDeferred', () => {
  const base = { now: 10_000, lastHardInputAt: 0, lastUserScrollAt: 0, settleMs: SCROLL_SETTLE_MS }

  it('defers while a finger is on the glass', () => {
    expect(geometryCommitDeferred({ ...base, stick: false, lastHardInputAt: 9_950 })).toBe(true)
  })

  it('defers through momentum, which produces scrolls with no further input', () => {
    // The case a hard-input timestamp alone cannot see: iOS keeps scrolling long
    // after the finger is gone, and a commit landing then is just as visible.
    expect(geometryCommitDeferred({ ...base, stick: false, lastUserScrollAt: 9_900 })).toBe(true)
  })

  it('commits once the reader has been still for the settle window', () => {
    expect(
      geometryCommitDeferred({
        ...base,
        stick: false,
        lastHardInputAt: 10_000 - SCROLL_SETTLE_MS - 1,
        lastUserScrollAt: 10_000 - SCROLL_SETTLE_MS - 1,
      }),
    ).toBe(false)
  })

  it('never defers a FOLLOWED reader', () => {
    // The bottom pin owns their position, and stalling the streaming row's
    // growth re-creates the spacer lurch its eager sync path exists to prevent.
    expect(geometryCommitDeferred({ ...base, stick: true, lastHardInputAt: 9_999 })).toBe(false)
  })
})

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

function mkRowNode(h: number): HTMLDivElement {
  const node = document.createElement('div')
  Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => h })
  return node
}

function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state: Geom = { ...initial }
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { state.scrollTop = v },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { state.scrollTop = o.top }
  el.getBoundingClientRect = () =>
    ({ top: 0, bottom: 400, left: 0, right: 390, width: 390, height: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  return { el, state }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

describe('useVirtualChat: geometry waits for a moving reader', () => {
  let origRaf: typeof requestAnimationFrame
  beforeEach(() => {
    localStorage.clear()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    globalThis.requestAnimationFrame = origRaf
  })

  it('never defers the FIRST commit of a mount, even mid-scroll', () => {
    // A scroll event fires while the transcript takes its initial position, which
    // stamps the motion timestamp — so a deferral with no first-commit exemption
    // pushes every mount's seed geometry a debounce round later, leaving rows
    // priced at estimates while the reader is already looking at them. Nothing is
    // protected by that: the deferral exists to hold a SETTLED picture still.
    const { el, state } = makeScroller({ scrollTop: 2000, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items: mkItems(200), sessionId: 'defer-first', getKey, externalScrollerRef: ref, followOutput: true } },
    )
    act(() => {
      state.scrollTop = 2000
      el.dispatchEvent(new Event('scroll'))
    })
    const atMount = view.result.current.totalHeight
    act(() => { expect(view.result.current.farmRecord(20, 'm20', 900)).toBe(true) })
    // One debounce window, WITHOUT waiting out the settle gate.
    act(() => { vi.advanceTimersByTime(130) })
    expect(view.result.current.totalHeight).not.toBe(atMount)
  })

  it('holds the total height frozen while the reader scrolls, then lands it once they stop', () => {
    const { el, state } = makeScroller({ scrollTop: 2000, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items: mkItems(200), sessionId: 'defer-commit', getKey, externalScrollerRef: ref, followOutput: true } },
    )
    // Release follow by moving up from the bottom mount placement.
    act(() => {
      state.scrollTop = 2000
      el.dispatchEvent(new Event('scroll'))
    })
    // Seed EVERY row with a real measurement and let it commit. Two confounds go
    // away with it: an unmeasured transcript's total is driven by the estimate, so
    // it moves at render time with no commit for the deferral to hold; and the
    // mount's own first commit is exempt by design (nothing settled to protect
    // yet), so a fixture that leans on it is not testing the deferral at all.
    act(() => {
      for (let i = 0; i < 200; i++) view.result.current.measureRef(i)(mkRowNode(80))
    })
    act(() => { vi.advanceTimersByTime(SCROLL_SETTLE_MS + 200) })

    const before = view.result.current.totalHeight
    expect(before).toBeGreaterThan(0)

    // The gesture is already under way when an off-screen row's new height
    // arrives (the farm's path: silent cache write plus a debounced sync
    // request). Each scroll re-arms the deferral, so the spacers must not move.
    act(() => {
      state.scrollTop -= 120
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { expect(view.result.current.farmRecord(150, 'm150', 900)).toBe(true) })
    for (let i = 0; i < 5; i++) {
      act(() => {
        state.scrollTop -= 120
        el.dispatchEvent(new Event('scroll'))
      })
      act(() => { vi.advanceTimersByTime(130) })
      expect(view.result.current.totalHeight).toBe(before)
    }

    // The reader stops. The whole reprice lands in one commit.
    act(() => { vi.advanceTimersByTime(SCROLL_SETTLE_MS + 200) })
    expect(view.result.current.totalHeight).not.toBe(before)
  })
})
