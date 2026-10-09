// REGRESSION GUARD -- kirodotdev/KiroCrew#18421.
//
// A reader scrolls UP while a reply streams and the transcript yanks them back
// to the bottom on the next streamed update, so a scrolled-up position cannot
// be held until the turn ends. Auto-follow must pin only a reader who is still
// at the bottom.
//
// The shape that matters is the first frame of the reader's input. A desktop
// wheel notch is one `wheel` event with a 100px delta that the engine ANIMATES:
// the first scroll event of that animation moves a fraction of a pixel to a
// couple of pixels, inside SELF_SCROLL_EPSILON, and a precision touchpad
// scrolled slowly reports 1-2px per event on its own. The scroll handler reads
// such a frame as our own pin landing -- it cannot tell the two apart -- and
// rightly leaves follow armed. It also RETIRED the pending-intent hold with it,
// so a row appended in that frame found a reader "resting on our write" and
// pinned them to the new bottom; the instant write cancelled the engine's
// animation too, and the notch was eaten whole.
//
// The fix leaves the upward intent pending through a frame inside the epsilon:
// the append's pin is HELD until the notch's next frame lands outside it and
// releases follow through the unchanged path, or the intent expires unanswered
// and the held pin retries against a reader who is still resting.
//
// Drives the real hook (intent listener, scroll handler, append effect and the
// ResizeObserver follow path) through a fake scroller, the way the other
// useVirtualChat suites do.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'
import { SCROLL_SETTLE_MS, SELF_SCROLL_EPSILON } from '../hooks/virtualizer/FollowController'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state: Geom = { ...initial }
  const writes: number[] = []
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { state.scrollTop = v; writes.push(v) },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => {
    state.scrollTop = o.top
    writes.push(o.top)
  }
  el.getBoundingClientRect = () =>
    ({ top: 0, bottom: CH, left: 0, right: 390, width: 390, height: CH, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  return { el, state, writes }
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

const CH = 400
const SH = 9000
const BOTTOM = SH - CH // 8600
const N = 30
const FRAME = 16

describe('useVirtualChat: a reader who wheels up while a reply streams is not re-pinned (#18421)', () => {
  let fire: ((entries: { target: Element }[]) => void) | undefined
  let origRaf: typeof requestAnimationFrame
  let origRO: typeof ResizeObserver | undefined
  let nowSpy: ReturnType<typeof vi.spyOn> | undefined
  let now = 100_000

  beforeEach(() => {
    localStorage.clear()
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
    now = 100_000
    nowSpy = vi.spyOn(performance, 'now').mockImplementation(() => now)
  })
  afterEach(() => {
    nowSpy?.mockRestore()
    vi.useRealTimers()
    globalThis.requestAnimationFrame = origRaf
    if (origRO) globalThis.ResizeObserver = origRO
    fire = undefined
  })

  /** The clock the settle windows and the held-pin retry read. */
  function tick(ms: number) {
    now += ms
    act(() => { vi.advanceTimersByTime(ms) })
  }

  /**
   * A live turn: follow armed at the bottom, the last row is the streaming
   * reply (300px tall, its bottom flush with the viewport's), a run active.
   */
  function mountStreaming(sessionId: string) {
    const { el, state, writes } = makeScroller({ scrollTop: 0, scrollHeight: SH, clientHeight: CH })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    let items = mkItems(N)
    const props = (): UseVirtualChatOptions<Item> => ({
      items, sessionId, getKey, externalScrollerRef: ref, followOutput: true,
      streamingIndex: items.length - 1, runActive: true,
    })
    const view = renderHook((p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p), { initialProps: props() })
    // Slot entry pinned the reader to the bottom and armed follow.
    expect(state.scrollTop).toBe(BOTTOM)
    expect(view.result.current.getFollow()).toBe(true)
    const reply = { top: CH - 300, h: 300 }
    const replyRow = makeRow(reply)
    act(() => { view.result.current.measureRef(N - 1)(replyRow) })
    // The reader has been parked for a while: no input or gesture in flight.
    tick(1000)
    writes.length = 0

    const getFollow = () => view.result.current.getFollow()
    /** Hardware input: one wheel event, pixel mode, negative = up. */
    const wheel = (deltaY: number) => {
      act(() => { el.dispatchEvent(new WheelEvent('wheel', { deltaY, deltaMode: 0 })) })
    }
    /** The engine answers an input: scrollTop lands at `top` and the scroll event dispatches. */
    const scrollLands = (top: number) => {
      act(() => { state.scrollTop = top; el.dispatchEvent(new Event('scroll')) })
    }
    /** A streamed chunk: the reply row grows at its bottom and the observer reports it. */
    const streamChunk = (px: number) => {
      reply.h += px
      state.scrollHeight += px
      act(() => { fire?.([{ target: replyRow }]) })
    }
    /** A new row lands at the tail (a tool call starting mid-turn): the items array grows by one. */
    const appendRow = (px: number) => {
      items = mkItems(items.length + 1)
      state.scrollHeight += px
      act(() => { view.rerender(props()) })
    }
    return { el, state, writes, getFollow, wheel, scrollLands, streamChunk, appendRow }
  }

  it('one large wheel step up releases follow; streamed chunks and a new row leave the reader where they stopped', () => {
    const { state, writes, getFollow, wheel, scrollLands, streamChunk, appendRow } = mountStreaming('18421-large-step')
    wheel(-100)
    tick(8)
    scrollLands(BOTTOM - 100)
    expect(getFollow()).toBe(false)
    streamChunk(40)
    expect(state.scrollTop).toBe(BOTTOM - 100)
    // Past the settle window, with the reader resting where they stopped.
    tick(SCROLL_SETTLE_MS + 50)
    streamChunk(40)
    appendRow(120)
    tick(FRAME)
    expect(state.scrollTop).toBe(BOTTOM - 100)
    expect(getFollow()).toBe(false)
    expect(writes).toEqual([])
  })

  it('a wheel notch whose first animation frame moves under SELF_SCROLL_EPSILON is not eaten by a new row landing in that frame', () => {
    const { state, writes, getFollow, wheel, scrollLands, streamChunk, appendRow } = mountStreaming('18421-smooth-notch')
    // One notch: the engine animates 100px up over several frames, the first
    // of which moves barely a pixel. That frame cannot be told from our own
    // pin landing, so follow stays armed through it -- but the upward intent
    // stays pending, and the row appended in the same frame must NOT pin.
    const firstFrame = BOTTOM - (SELF_SCROLL_EPSILON - 1)
    wheel(-100)
    tick(8)
    scrollLands(firstFrame)
    appendRow(120)
    expect(state.scrollTop).toBe(firstFrame)
    expect(writes).toEqual([])
    // The animation goes on (a pin would have cancelled it); the next frame
    // lands outside the epsilon and releases follow, and the reader keeps
    // every pixel of their notch through the chunks still streaming.
    tick(8)
    scrollLands(BOTTOM - 6)
    expect(getFollow()).toBe(false)
    streamChunk(30)
    tick(FRAME)
    scrollLands(BOTTOM - 21)
    streamChunk(30)
    tick(FRAME)
    scrollLands(BOTTOM - 50)
    // The held pin's retry fires at the intent's expiry and finds follow released.
    tick(SCROLL_SETTLE_MS + 50)
    streamChunk(30)
    expect(state.scrollTop).toBe(BOTTOM - 50)
    expect(getFollow()).toBe(false)
    expect(writes).toEqual([])
  })

  it('a slow touchpad (1px per event, each under SELF_SCROLL_EPSILON) keeps its ground through streamed chunks', () => {
    const { state, writes, getFollow, wheel, scrollLands, streamChunk } = mountStreaming('18421-touchpad-chunks')
    for (let k = 1; k <= 6; k++) {
      wheel(-1)
      tick(FRAME / 2)
      scrollLands(BOTTOM - k)
      tick(FRAME / 2)
      streamChunk(10)
    }
    expect(state.scrollTop).toBe(BOTTOM - 6)
    expect(getFollow()).toBe(false)
    expect(writes).toEqual([])
  })

  it('a slow touchpad is not re-pinned by a new row landing between two of its 1px steps', () => {
    const { state, writes, getFollow, wheel, scrollLands, appendRow } = mountStreaming('18421-touchpad-row')
    wheel(-1)
    tick(FRAME / 2)
    scrollLands(BOTTOM - 1)
    tick(FRAME / 2)
    appendRow(120)
    expect(state.scrollTop).toBe(BOTTOM - 1)
    wheel(-1)
    tick(FRAME / 2)
    scrollLands(BOTTOM - 2)
    tick(FRAME / 2)
    appendRow(120)
    expect(state.scrollTop).toBe(BOTTOM - 2)
    wheel(-1)
    tick(FRAME / 2)
    scrollLands(BOTTOM - 3)
    expect(getFollow()).toBe(false)
    // The held pins' retry fires at expiry and finds follow released.
    tick(SCROLL_SETTLE_MS + 50)
    expect(state.scrollTop).toBe(BOTTOM - 3)
    expect(writes).toEqual([])
  })

  it('a height commit landing between two scroll events of one upward gesture does not re-pin', () => {
    const { state, writes, getFollow, wheel, scrollLands, streamChunk } = mountStreaming('18421-commit-between')
    wheel(-100)
    tick(8)
    scrollLands(BOTTOM - 1)
    tick(4)
    // The streaming row commits a chunk between the gesture's first and second frames.
    streamChunk(30)
    tick(4)
    scrollLands(BOTTOM - 6)
    tick(8)
    streamChunk(30)
    expect(state.scrollTop).toBe(BOTTOM - 6)
    expect(getFollow()).toBe(false)
    expect(writes).toEqual([])
  })

  it('an upward input the transcript never answers past one sub-epsilon frame leaves the reader followed: the held pin retries at expiry', () => {
    // A single 1px touchpad event and nothing more: the frame is inside the
    // epsilon, so follow stays armed, the append's pin is held for the intent's
    // window, and at expiry the retry finds a reader still resting on our write
    // and carries them to the row that landed meanwhile.
    const { state, writes, getFollow, wheel, scrollLands, appendRow } = mountStreaming('18421-unanswered-intent')
    wheel(-1)
    tick(8)
    scrollLands(BOTTOM - 1)
    appendRow(120)
    expect(state.scrollTop).toBe(BOTTOM - 1)
    expect(writes).toEqual([])
    expect(getFollow()).toBe(true)
    tick(SCROLL_SETTLE_MS + 50)
    expect(state.scrollTop).toBe(state.scrollHeight - CH)
    expect(writes.length).toBe(1)
    expect(getFollow()).toBe(true)
  })

  it('a reader still at the bottom who did not scroll is carried by the same chunks (follow itself is intact)', () => {
    const { state, getFollow, streamChunk, appendRow } = mountStreaming('18421-still-follows')
    streamChunk(40)
    expect(state.scrollTop).toBe(state.scrollHeight - CH)
    tick(FRAME)
    appendRow(120)
    tick(FRAME)
    expect(state.scrollTop).toBe(state.scrollHeight - CH)
    expect(getFollow()).toBe(true)
  })

  it('a pin that lands a device pixel short of its target, under an upward input that moved nothing, keeps following', () => {
    // Fractional row heights put the true bottom between device pixels, so an
    // instant pin can land 0.25px short of the integer target it asked for. An
    // upward wheel the transcript never answered (consumed by a nested
    // scroller inside a row) stamps intent in the same frame. The pin's own
    // scroll event then shows a scrollTop inside the epsilon under a fresh
    // upward stamp: it must not release a reader who never moved.
    const { el, state, writes, getFollow, wheel, streamChunk } = mountStreaming('18421-short-landing')
    ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => {
      state.scrollTop = o.top - 0.25
      writes.push(o.top)
    }
    streamChunk(40)
    expect(writes.length).toBe(1)
    expect(state.scrollTop).toBe(state.scrollHeight - CH - 0.25)
    wheel(-1)
    act(() => { el.dispatchEvent(new Event('scroll')) })
    expect(getFollow()).toBe(true)
    tick(SCROLL_SETTLE_MS + 50)
    streamChunk(40)
    expect(state.scrollTop).toBe(state.scrollHeight - CH - 0.25)
    expect(getFollow()).toBe(true)
  })
})
