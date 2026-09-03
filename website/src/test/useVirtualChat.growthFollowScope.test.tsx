/**
 * Which row's growth a bottom-parked reader is carried along by.
 *
 * Streaming and widget-load growth happen at the TAIL, and following them is
 * what keeps a reader at the bottom. A disclosure the user just OPENED
 * mid-transcript ("Worked through N steps", a tool's error output) grows an
 * OLDER row by hundreds to thousands of px. Following that growth pins to the
 * bottom, which shoves the content they just opened up past the viewport top —
 * once per ResizeObserver fire as the revealed lines render, which is felt as
 * bounce.
 *
 * The observer callback previously reduced every entry to one boolean
 * (`genuineResize`), discarding WHICH row grew, so the two cases were
 * indistinguishable. Pin: only a tail row's growth drives the follow pin.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, pinSuppressedNow, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'

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

/** A row node whose reported border-box height is mutable, so a resize can be
 *  expressed the way the observer actually reads it. */
function makeRow(h: { v: number }) {
  const node = document.createElement('div')
  Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => h.v })
  node.getBoundingClientRect = () =>
    ({ top: 0, bottom: h.v, left: 0, right: 390, width: 390, height: h.v, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  return node
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

describe('useVirtualChat: only TAIL growth carries a bottom-parked reader', () => {
  let origRaf: typeof requestAnimationFrame
  let origRO: typeof ResizeObserver | undefined
  let fire: ((entries: { target: Element }[]) => void) | undefined

  beforeEach(() => {
    localStorage.clear()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    origRO = globalThis.ResizeObserver
    // Capturing stub: the hook's callback is the thing under test, so the test
    // needs to deliver entries to it directly.
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
    globalThis.requestAnimationFrame = origRaf
    if (origRO) globalThis.ResizeObserver = origRO
    fire = undefined
  })

  /** Park at the bottom (stick armed), seed two measured rows, return both. */
  function setup() {
    const { el, state } = makeScroller({ scrollTop: 4600, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(30)
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items, sessionId: 'growth-scope', getKey, externalScrollerRef: ref, followOutput: true } },
    )
    const oldH = { v: 250 }
    const tailH = { v: 250 }
    const oldRow = makeRow(oldH)
    const tailRow = makeRow(tailH)
    // Seed a real measurement for each, so a later resize is a GENUINE resize
    // (prevH defined) rather than a first mount.
    act(() => {
      view.result.current.measureRef(3)(oldRow)
      view.result.current.measureRef(items.length - 1)(tailRow)
    })
    return { el, state, view, oldRow, tailRow, oldH, tailH }
  }

  it('does NOT pin when a long expansion cascade outlives the input settle window', () => {
    const { el, state, tailRow, tailH } = setup()
    state.scrollTop = 4600
    // The user clicks the disclosure. pointerdown is already in the intent set,
    // so the settle window is armed here.
    el.dispatchEvent(new Event('pointerdown'))
    // Many error lines render and re-measure in a cascade. Each fire lands
    // AFTER the 150ms window would have expired on its own — which is exactly
    // the case that used to escape the gate and pin.
    for (let i = 0; i < 6; i++) {
      tailH.v += 300
      state.scrollHeight += 300
      act(() => { vi.advanceTimersByTime(120) })
      act(() => { fire?.([{ target: tailRow }]) })
    }
    expect(state.scrollTop).toBe(4600)
  })

  it('DOES pin to the bottom when the tail row grows with no input in flight', () => {
    const { state, tailRow, tailH } = setup()
    state.scrollTop = 4600
    tailH.v = 1450
    state.scrollHeight = 6200
    act(() => { fire?.([{ target: tailRow }]) })
    expect(state.scrollTop).toBeGreaterThan(4600)
  })
})

describe('pinSuppressedNow', () => {
  it('suppresses inside the input window and while a cascade deadline is live', () => {
    expect(pinSuppressedNow(1000, 900, 0, 150)).toBe(true)
    expect(pinSuppressedNow(1000, 500, 1200, 150)).toBe(true)
  })
  it('releases once both the input window and the cascade deadline have passed', () => {
    expect(pinSuppressedNow(1000, 500, 0, 150)).toBe(false)
    expect(pinSuppressedNow(1000, 500, 999, 150)).toBe(false)
  })
})
