/**
 * A reader who scrolled up to read mid-stream is taken to the end a moment
 * later, on a phone, with nothing but tokens arriving.
 *
 * THE MECHANISM. Scrolling up releases follow, which is correct and happens on
 * the first upward scroll event. But the rows the reader scrolled past are
 * virtualized and priced from estimates, and on a narrow phone column an
 * estimate is far under a real wrapped message. When the tail reprices, the
 * content below the reader collapses past where they sat, the maximum scrollTop
 * drops under their position, and the engine clamps them flush with the new
 * bottom -- no finger anywhere near the screen. That clamp arrives as an
 * ordinary scroll event at distance ~0, `resolveUserScrollStick`'s rule 1 read
 * it as the reader returning to the bottom, and follow re-armed. From there the
 * bottom pin owns them and every later token drags them along.
 *
 * Rule 3 already refuses the same thing one band further out (FOLLOW_REENGAGE_PX,
 * "the band arrives at a STILL reader"), and a clamp lands at distance ~0 rather
 * than inside that band, which is how it slipped through.
 *
 * This drives the real hook through its own scroll listener AND its
 * ResizeObserver, so it fails if the release is ever decided somewhere the
 * clamp can bypass -- the unit test on `resolveUserScrollStick` alone cannot
 * see that wire.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'
import { __resetRailWidth } from '../hooks/useRailWidth'

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

/** A row wholly BELOW the fold, so the above-fold reprice path contributes nothing. */
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

/** Past SCROLL_SETTLE_MS: the reader has stopped and is reading, so an
 *  automatic pin is no longer held off by the settle gate. Real timers, because
 *  the gate is measured with `performance.now()`. */
const SETTLED_MS = 220

describe('useVirtualChat: a content collapse that clamps a released reader flush must not re-arm follow', () => {
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
  })
  afterEach(() => {
    __resetRailWidth()
    globalThis.requestAnimationFrame = origRaf
    if (origRO) globalThis.ResizeObserver = origRO
    fire = undefined
  })

  /**
   * Phone shape: a 400px viewport on a 9000px transcript, a live turn, and the
   * tail row mounted below the fold so only the follow pin can move anyone.
   */
  function setup() {
    const { el, state } = makeScroller({ scrollTop: 8600, scrollHeight: 9000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      {
        initialProps: {
          items: mkItems(N), sessionId: 'collapse-clamp', getKey, externalScrollerRef: ref,
          followOutput: true, streamingIndex: LAST,
        },
      },
    )
    const tail = { top: 200, h: 180 }
    const tailRow = makeRow(tail)
    act(() => { view.result.current.measureRef(LAST)(tailRow) })
    return { view, state, el, tail, tailRow }
  }

  /** The reader drags up off the bottom: follow releases on this event. */
  function scrollUpToRead(el: HTMLElement, state: Geom, to: number) {
    act(() => {
      state.scrollTop = to
      el.dispatchEvent(new Event('scroll'))
    })
  }

  /**
   * The tail reprices under its estimates: `scrollHeight` collapses past the
   * reader and the engine clamps `scrollTop` to the new maximum, then dispatches
   * an ordinary scroll event for it.
   */
  function collapseTailAndClamp(el: HTMLElement, state: Geom, newScrollHeight: number) {
    act(() => {
      state.scrollHeight = newScrollHeight
      const max = Math.max(0, newScrollHeight - state.clientHeight)
      if (state.scrollTop > max) state.scrollTop = max
      el.dispatchEvent(new Event('scroll'))
    })
  }

  /** One token lands on the streaming tail row. */
  function appendToken(state: Geom, tail: { h: number }, tailRow: Element, px: number) {
    tail.h += px
    state.scrollHeight += px
    act(() => { fire?.([{ target: tailRow }]) })
  }

  const settle = async () => {
    await act(async () => { await new Promise((r) => setTimeout(r, SETTLED_MS)) })
  }

  it('leaves the clamped reader where the clamp put them while the turn keeps streaming', async () => {
    const { state, el, tail, tailRow } = setup()
    scrollUpToRead(el, state, 4000)
    expect(state.scrollTop).toBe(4000)
    // 4200 - 400 = 3800: the maximum drops 200px under the reader.
    collapseTailAndClamp(el, state, 4200)
    expect(state.scrollTop).toBe(3800)
    const clampedAt = state.scrollTop
    await settle()
    for (const px of [27, 54, 27, 80, 107]) appendToken(state, tail, tailRow, px)
    // Pre-fix the clamp re-armed follow and the first append pinned to the new
    // bottom, then every later one followed it -- +355px over this run and
    // climbing for the rest of the turn.
    expect(state.scrollTop).toBe(clampedAt)
  })

  it('DISCRIMINATOR: a reader still AT the bottom is carried across the same collapse', async () => {
    // Identical collapse, identical clamp -- the only difference is that this
    // reader never scrolled up, so follow is still armed and the clamp is the
    // engine carrying a follower. Releasing here would freeze streaming follow
    // for the rest of the turn, which is what rule 1 exists to prevent.
    const { state, el, tail, tailRow } = setup()
    collapseTailAndClamp(el, state, 4200)
    expect(state.scrollTop).toBe(3800)
    await settle()
    appendToken(state, tail, tailRow, 60)
    expect(state.scrollTop).toBe(4260 - 400)
  })

  it('DISCRIMINATOR: a reader who scrolls back DOWN to the bottom themselves re-engages', async () => {
    // The re-engagement the rule must keep: same arrival at the bottom, but this
    // reader moved toward it under their own hand.
    const { state, el, tail, tailRow } = setup()
    scrollUpToRead(el, state, 4000)
    act(() => {
      state.scrollTop = 8600
      el.dispatchEvent(new Event('scroll'))
    })
    await settle()
    appendToken(state, tail, tailRow, 60)
    expect(state.scrollTop).toBe(9060 - 400)
  })
})
