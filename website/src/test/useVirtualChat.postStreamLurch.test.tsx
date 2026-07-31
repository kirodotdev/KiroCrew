// REGRESSION GUARD (was a failing repro) — gap #3: DIFF (and code) blocks.
//
// PR #824 added `streamingIndex` so the streaming row's height changes bypass
// the 120ms HEIGHT_SYNC_DEBOUNCE_MS (see useVirtualChat.scheduleHeightSync).
// That bypass is scoped to `isStreaming` being true: ChatPage passes
// `streamingIndex = isStreaming && len>0 ? len-1 : undefined`, so it goes
// UNDEFINED the instant the turn closes.
//
// Diff and code blocks are wrapped in <SmoothResize enabled={!complete}>, which
// drives the row height toward the content height via a CSS `height .32s`
// transition. That means the row KEEPS RESIZING for up to ~320ms AFTER the last
// content byte streamed in — and the completion flip (enabled true→false) is a
// further one-shot height change. If the turn closes (streamingIndex → undefined)
// while that height-ease tail / completion snap is still resizing the row, those
// trailing changes fall back to the DEBOUNCED path — re-creating the exact
// frozen-then-jump spacer lurch #824 removed, just shifted to the moment a diff
// finishes at end of stream. For a scrolled-up user that is a visible flash.
//
// This test drives the hook's REAL outputs through a controllable fake
// ResizeObserver under fake timers (same harness as spacerLurch): it streams a
// row live (tracked), clears streamingIndex (turn closes), then fires the
// SmoothResize height-ease tail. The GAP test asserts the row that is still
// visibly resizing as the turn closes keeps tracking live; it passes once the
// hook keeps the just-ended streaming row on the immediate-sync path for a
// short settle grace. The CONTROL proves the smooth path (streamingIndex still
// set) is unaffected.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatOptions } from '../hooks/virtualizer/types'

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
  return { el, state }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  cb: ResizeObserverCallback
  observed = new Set<Element>()
  constructor(cb: ResizeObserverCallback) {
    this.cb = cb
    FakeResizeObserver.instances.push(this)
  }
  observe(el: Element) { this.observed.add(el) }
  unobserve(el: Element) { this.observed.delete(el) }
  disconnect() { this.observed.clear() }
  fire(entries: Partial<ResizeObserverEntry>[]) {
    this.cb(entries as ResizeObserverEntry[], this as unknown as ResizeObserver)
  }
}

function mkEntry(target: HTMLElement, height: number): Partial<ResizeObserverEntry> {
  Object.defineProperty(target, 'offsetHeight', { configurable: true, get: () => height })
  return { target }
}

describe('useVirtualChat: SmoothResize tail after stream end (diff/code gap #3)', () => {
  let origRO: typeof ResizeObserver | undefined
  let origRaf: typeof requestAnimationFrame

  beforeEach(() => {
    localStorage.clear()
    FakeResizeObserver.instances = []
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    globalThis.ResizeObserver = origRO as typeof ResizeObserver
    globalThis.requestAnimationFrame = origRaf
  })

  function mountStreaming(sessionId: string, geom: Geom, items: Item[]) {
    const { el, state } = makeScroller(geom)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const lastIdx = items.length - 1
    const baseProps: UseVirtualChatOptions<Item> = {
      items, sessionId, getKey, externalScrollerRef: ref, streamingIndex: lastIdx,
    }
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: baseProps },
    )
    const HISTORY_H = 100
    for (let i = 0; i < lastIdx; i++) {
      const node = document.createElement('div')
      Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => HISTORY_H })
      act(() => { view.result.current.measureRef(i)(node) })
    }
    const streamNode = document.createElement('div')
    Object.defineProperty(streamNode, 'offsetHeight', { configurable: true, get: () => 40 })
    act(() => { view.result.current.measureRef(lastIdx)(streamNode) })
    // Flush the debounced first-mount seed so the baseline is settled.
    act(() => { vi.advanceTimersByTime(120) })
    return { el, state, view, streamNode, baseProps, lastIdx }
  }

  it('GAP: the height-ease tail firing after the turn closes still tracks live (no post-stream lurch)', () => {
    const { view, streamNode, baseProps } = mountStreaming(
      'diff-smoothresize-tail',
      { scrollTop: 500, scrollHeight: 3000, clientHeight: 400 },
      mkItems(21),
    )
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]
    const baselineTotal = view.result.current.totalHeight

    // --- Phase 1: diff content streams in. streamingIndex is set, so each RO
    // tick is synced immediately (the #824 behavior we rely on). ---
    let h = 40
    let expected = baselineTotal
    for (let i = 0; i < 6; i++) {
      h += 10
      expected += 10
      act(() => { ro.fire([mkEntry(streamNode, h)]) })
      act(() => { vi.advanceTimersByTime(16) })
      expect(view.result.current.totalHeight).toBe(expected) // tracked live while streaming
    }

    // --- Phase 2: the turn closes. isStreaming flips false, so ChatPage stops
    // passing streamingIndex. The row is STILL resizing, though: SmoothResize's
    // `height .32s` transition is easing the wrapper toward the content height,
    // and the completion flip is one more height change. ---
    act(() => { view.rerender({ ...baseProps, streamingIndex: undefined }) })

    // --- Phase 3: the SmoothResize ease tail + completion snap — more genuine
    // resizes on the SAME row, each within a debounce window (as a 320ms CSS
    // transition steps frame by frame). ---
    for (let i = 0; i < 4; i++) {
      h += 12
      expected += 12
      act(() => { ro.fire([mkEntry(streamNode, h)]) })
      act(() => { vi.advanceTimersByTime(16) }) // << 120ms: still inside the debounce window
    }

    // Regression guard: because the row is still visibly resizing as the turn
    // closed, its height is reflected promptly via the post-stream settle grace
    // (STREAMING_SETTLE_GRACE_MS) — no frozen-then-jump. Before that grace
    // existed, clearing streamingIndex sent these Phase-3 ticks down the
    // debounced path, freezing totalHeight at the Phase-1 value (`expected - 48`).
    expect(view.result.current.totalHeight).toBe(expected)
  })

  it('CONTROL: the same ease tail is smooth when the turn has NOT yet closed (streamingIndex still set)', () => {
    // Proves the lurch is caused specifically by streamingIndex clearing at the
    // stream boundary, not by SmoothResize growth per se. Survives the fix.
    const { view, streamNode } = mountStreaming(
      'diff-smoothresize-tail-control',
      { scrollTop: 500, scrollHeight: 3000, clientHeight: 400 },
      mkItems(21),
    )
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]
    let h = 40
    let expected = view.result.current.totalHeight
    for (let i = 0; i < 8; i++) {
      h += 12
      expected += 12
      act(() => { ro.fire([mkEntry(streamNode, h)]) })
      act(() => { vi.advanceTimersByTime(16) })
      // streamingIndex is still set → immediate sync every tick.
      expect(view.result.current.totalHeight).toBe(expected)
    }
  })
})
