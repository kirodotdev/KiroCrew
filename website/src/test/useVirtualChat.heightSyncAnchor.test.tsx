/**
 * Height-sync anchor compensation.
 *
 * A debounced height sync re-prices the offset spacers (estimates replaced by
 * real measurements). Rows ABOVE the viewport re-pricing moves everything
 * below by the delta; Chrome's native scroll anchoring absorbs that shift but
 * iOS Safari has none, so a reader mid-transcript sees the content slide
 * (measured 13-25px right after a far jump on the pod, and a nondeterministic
 * 170-190px lurch when the anchor was consumed early by the window effect —
 * see heightAnchorPendingRef's doc in useVirtualChat.ts).
 *
 * Pins: syncHeightsNow captures the top visible row before bumping
 * heightVersion, and the dedicated heightVersion-keyed layout effect corrects
 * scrollTop by the row's screen-position delta after the commit.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

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
  el.getBoundingClientRect = () => ({ top: 0, bottom: 400, left: 0, right: 390, width: 390, height: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  return { el, state }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

describe('useVirtualChat: height-sync spacer repricing keeps the top visible row anchored', () => {
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

  it('compensates scrollTop by the anchor row shift when a debounced sync commits (reader mid-transcript)', () => {
    const { el, state } = makeScroller({ scrollTop: 1000, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(30)
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items, sessionId: 'anchor-sync', getKey, externalScrollerRef: ref, followOutput: false } },
    )

    // Mount a visible row whose screen position SHIFTS between the sync's
    // anchor capture (first rect read → top 100) and the compensation
    // effect's re-read after the commit (subsequent reads → top 130),
    // simulating the spacer above it re-pricing by +30px. jsdom has no real
    // layout, so the shift is expressed through the self-mutating mock.
    const node = document.createElement('div')
    Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 250 })
    // Read #1 is the seed measurement itself (fractional heights read the
    // rect), #2 is the sync's anchor capture, #3+ the compensation effect.
    let reads = 0
    node.getBoundingClientRect = () => {
      reads += 1
      const top = reads <= 2 ? 100 : 130
      return { top, bottom: top + 250, left: 0, right: 390, width: 390, height: 250, x: 0, y: top, toJSON: () => ({}) } as DOMRect
    }
    act(() => { view.result.current.measureRef(5)(node) })

    // The seed above scheduled a debounced sync. Flush it: syncHeightsNow
    // captures {key:'m5', top:100}, commits the repricing, and the dedicated
    // layout effect re-reads top=130 → delta +30 → scrollTop corrected.
    const before = state.scrollTop
    act(() => { vi.advanceTimersByTime(120) })
    expect(state.scrollTop - before).toBe(30)
  })

  it('does not touch scrollTop when the anchor row does not move across the commit', () => {
    const { el, state } = makeScroller({ scrollTop: 1000, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(30)
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items, sessionId: 'anchor-sync-stable', getKey, externalScrollerRef: ref, followOutput: false } },
    )
    const node = document.createElement('div')
    Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 250 })
    node.getBoundingClientRect = () => ({ top: 100, bottom: 350, left: 0, right: 390, width: 390, height: 250, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect
    act(() => { view.result.current.measureRef(5)(node) })
    const before = state.scrollTop
    act(() => { vi.advanceTimersByTime(120) })
    expect(state.scrollTop).toBe(before)
  })
})
