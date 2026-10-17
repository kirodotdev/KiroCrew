// Feature: chat-virtualizer — an explicit bottom pin keeps the rows on screen.
//
// `scrollToBottom` (a Send's force-pin, the jump-to-bottom button) mounts the
// tail before it pins. It used to REPLACE the window with the last
// `overscan + 1` rows; in a chat of short bubbles that is a fraction of the
// viewport, so the rows the reader was looking at unmounted and the spacer's
// placeholder bars (`vc-spacer-skeleton`) showed in their place for the frames
// until the pin's own scroll remounted them -- the skeleton that flashed right
// after a crewmate Send. The window it mounts must cover the viewport and keep
// what was already mounted next to the tail.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatOptions } from '../hooks/virtualizer/types'
import { bottomPinWindow } from '../hooks/virtualizer/WindowCalculator'

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

const ROW = 40
const VIEWPORT = 600

function makeScroller(count: number) {
  const el = document.createElement('div')
  let top = 0
  Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => top, set: (v: number) => { top = v } })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => count * ROW })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => VIEWPORT })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { top = o.top }
  return el
}

describe('bottomPinWindow', () => {
  const flat = () => ROW
  it('covers the viewport plus overscan, never less than the bare tail', () => {
    // 600px / 40px = 15 rows on screen, + 5 overscan.
    expect(bottomPinWindow(100, 5, flat, VIEWPORT)).toEqual({ start: 80, end: 100 })
    // Tall rows: the bare overscan tail is already more than a viewport.
    expect(bottomPinWindow(100, 5, () => 900, VIEWPORT)).toEqual({ start: 94, end: 100 })
    expect(bottomPinWindow(3, 5, flat, VIEWPORT)).toEqual({ start: 0, end: 3 })
    // No viewport yet (unlaid-out host): the bare tail, as before.
    expect(bottomPinWindow(100, 5, flat, 0)).toEqual({ start: 94, end: 100 })
  })
})

describe('useVirtualChat: scrollToBottom keeps the reader\'s rows mounted', () => {
  let origRaf: typeof requestAnimationFrame
  const frames: FrameRequestCallback[] = []
  beforeEach(() => {
    origRaf = globalThis.requestAnimationFrame
    // Frames are held so the test can read the window the pin commits BEFORE
    // its scroll write runs -- the frame a reader actually sees.
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { frames.push(cb); return frames.length }) as typeof requestAnimationFrame
    vi.useFakeTimers()
  })
  afterEach(() => {
    frames.length = 0
    vi.useRealTimers()
    globalThis.requestAnimationFrame = origRaf
  })

  it('mounts a viewport of rows on the pin, so no placeholder shows where text was', () => {
    const items = mkItems(60)
    const el = makeScroller(items.length)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const props: UseVirtualChatOptions<Item> = { items, sessionId: 'pin-window', getKey, externalScrollerRef: ref, estimatedHeight: ROW }
    const view = renderHook((p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p), { initialProps: props })
    act(() => { vi.advanceTimersByTime(250) })
    // A Send appends the bubble, then the pane force-pins.
    const sent = mkItems(61)
    view.rerender({ ...props, items: sent })
    act(() => { view.result.current.scrollToBottom() })
    const mounted = view.result.current.virtualItems
    const mountedHeight = mounted.length * ROW
    expect(mounted[mounted.length - 1].index).toBe(60)
    expect(mountedHeight).toBeGreaterThanOrEqual(VIEWPORT)
    // The rows above the mounted window are the only spacer; it ends above
    // the viewport's top edge, so none of its bars are on screen.
    expect(view.result.current.offsetBefore).toBeLessThanOrEqual(sent.length * ROW - VIEWPORT)
  })
})
