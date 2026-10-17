/**
 * kirodotdev/KiroCrew#10810 -- reading the MIDDLE of a streaming reply, the text
 * slides UP out from under the reader by one token's height per tick.
 *
 * Reproduced on the device with the scroll inspector: every write was
 * `abovefold`, `+27/+54px` per tick, `Δtop == Δh`. The streaming row had been
 * scrolled into so far that its TOP was above the fold -- a STRADDLING row --
 * and the above-fold compensation credited its whole growth as "content above
 * the reader". But a streaming row grows by APPENDING at its bottom, below the
 * reader's eye line; nothing visible moved, and the browser's native anchor
 * was already holding them. The write was the drift. Parked at the message's
 * HEAD (row top inside the fold) the same reader held perfectly.
 *
 * This drives the real hook through its ResizeObserver, the way
 * `useVirtualChat.repriceSameFrame` does, so it fails if the call site ever
 * stops telling the predicate which row is the streaming one -- the unit test
 * on `repriceAboveFoldDelta` alone cannot see that wire.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'
import { setRailWidth, isRailSettling, __resetRailWidth } from '../hooks/useRailWidth'

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
  el.getBoundingClientRect = () =>
    ({ top: 0, bottom: 400, left: 0, right: 390, width: 390, height: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  return { el, state }
}

function makeRow(box: { top: number; h: number }) {
  const node = document.createElement('div')
  Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => box.h })
  node.getBoundingClientRect = () =>
    ({
      top: box.top, bottom: box.top + box.h, left: 0, right: 390,
      width: 390, height: box.h, x: 0, y: box.top, toJSON: () => ({}),
    }) as DOMRect
  return node
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))
const N = 30
const LAST = N - 1

describe('useVirtualChat: a released reader inside the streaming row is not walked by its growth (#10810)', () => {
  let origRaf: typeof requestAnimationFrame
  let origRO: typeof ResizeObserver | undefined
  let fire: ((entries: { target: Element }[]) => void) | undefined

  beforeEach(() => {
    localStorage.clear()
    __resetRailWidth()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(cb: ResizeObserverCallback) {
        fire = (entries) => cb(entries as unknown as ResizeObserverEntry[], this as unknown as ResizeObserver)
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    __resetRailWidth()
    globalThis.requestAnimationFrame = origRaf
    if (origRO) globalThis.ResizeObserver = origRO
    fire = undefined
  })

  /**
   * The last row is the streaming reply, tall enough that the reader can be
   * INSIDE it: its top is above the fold, its bottom far below. Follow is
   * released by an upward scroll off the bottom, as on the device.
   */
  function setup(streaming: boolean) {
    const { el, state } = makeScroller({ scrollTop: 4000, scrollHeight: 9000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(N)
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      {
        initialProps: {
          items, sessionId: 'mid-stream-reader', getKey, externalScrollerRef: ref, followOutput: true,
          streamingIndex: streaming ? LAST : undefined,
        },
      },
    )
    act(() => {
      state.scrollTop = 4000
      el.dispatchEvent(new Event('scroll'))
    })
    // The reply: top 3000px above the fold, 8000px tall => bottom 5000px below.
    const reply = { top: -3000, h: 8000 }
    const replyRow = makeRow(reply)
    act(() => { view.result.current.measureRef(LAST)(replyRow) })
    return { state, reply, replyRow }
  }

  /** One token lands: the row's bottom extends, its top does not move. */
  function appendToken(state: Geom, reply: { top: number; h: number }, px: number) {
    reply.h += px
    state.scrollHeight += px
  }

  it('holds scrollTop through a run of token appends on the straddling streaming row', () => {
    const { state, reply, replyRow } = setup(true)
    const startedAt = state.scrollTop
    // The per-tick sizes read off the inspector during the report.
    for (const px of [27, 27, 27, 54, 27, 50, 27, 54, 80, 80, 107]) {
      appendToken(state, reply, px)
      act(() => { fire?.([{ target: replyRow }]) })
    }
    // Pre-fix this walked +560 over the run (Δtop == Δh every tick).
    expect(state.scrollTop).toBe(startedAt)
  })

  it('stays held once the debounced height sync lands (no late correction in either direction)', () => {
    const { state, reply, replyRow } = setup(true)
    const startedAt = state.scrollTop
    appendToken(state, reply, 54)
    act(() => { fire?.([{ target: replyRow }]) })
    act(() => { vi.advanceTimersByTime(400) })
    expect(state.scrollTop).toBe(startedAt)
  })

  it('DISCRIMINATOR: the same straddling row NOT marked streaming is repriced and IS compensated', () => {
    // Identical geometry, no streamingIndex: this is a measurement replacing an
    // estimate on a row above the reader, and the reader must be held against
    // it (the walk-drift fix). Passing pre- and post-fix, it pins that the fix
    // is keyed on the streaming identity and did not blind the predicate.
    const { state, reply, replyRow } = setup(false)
    const startedAt = state.scrollTop
    appendToken(state, reply, 108)
    act(() => { fire?.([{ target: replyRow }]) })
    expect(state.scrollTop).toBe(startedAt + 108)
  })

  it('keeps compensating the straddling streaming row while the rail is collapsing (its height change is a re-wrap, not an append)', () => {
    // The rail's collapse animates the content column's width, so every
    // mounted row re-wraps -- the streaming row included. That height change
    // is distributed over the whole row, above the fold too, and is NOT a
    // bottom append; suppressing compensation for it would let the re-wrap
    // move a reader on a browser with no native scroll anchoring. The
    // streaming identity must yield to the settle window for its duration.
    const { state, reply, replyRow } = setup(true)
    const startedAt = state.scrollTop
    act(() => { setRailWidth(74) })
    expect(isRailSettling()).toBe(true)
    // Re-wrap: the same row, 60px taller, top unchanged.
    reply.h += 60
    state.scrollHeight += 60
    act(() => { fire?.([{ target: replyRow }]) })
    expect(state.scrollTop).toBe(startedAt + 60)
  })
})
