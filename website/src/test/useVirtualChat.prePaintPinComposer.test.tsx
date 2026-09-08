import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'

/**
 * REGRESSION GUARD — the PRE-PAINT bottom pin must not chase the composer.
 *
 * A height-sync commit re-targets the bottom inside the same commit that
 * repriced the tree, so a large reprice is invisible to a reader parked at the
 * bottom. That path had its own hand-rolled gate — `stick` plus a
 * hardware-input suppression window — and neither one sees typing: the intent
 * listeners are attached to the SCROLLER, and a keystroke in the composer never
 * reaches them.
 *
 * So typing grew the composer, the viewport shrank, the bottom moved DOWN by the
 * shrink with nobody scrolling, and this pin dragged the reader to it — reported
 * as the transcript springing back to the bottom while typing a short way above
 * it. The same defect had already been fixed in the ResizeObserver path and in
 * ChatPage's band observers; this was the third copy of the decision.
 *
 * The fix routes it through `evaluateAutoPin` — the predicate the post-paint pin
 * already uses — and freezes it against a viewport SHRINK. A private copy of a
 * decision is how the idle rule (`runActive`) came to cover one half of it only.
 */

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

function mkRowNode(h: number): HTMLDivElement {
  const node = document.createElement('div')
  Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => h })
  return node
}

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state = { ...initial }
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
  el.getBoundingClientRect = (() => ({ top: 0, bottom: state.clientHeight, height: state.clientHeight })) as unknown as typeof el.getBoundingClientRect
  return { el, state, writes }
}

class FakeRO {
  static instances: FakeRO[] = []
  constructor(readonly cb: ResizeObserverCallback) { FakeRO.instances.push(this) }
  observe() {}
  unobserve() {}
  disconnect() {}
  fire(entries: Partial<ResizeObserverEntry>[]) {
    this.cb(entries as ResizeObserverEntry[], this as unknown as ResizeObserver)
  }
}

const origRO = globalThis.ResizeObserver
const origRaf = globalThis.requestAnimationFrame

beforeEach(() => {
  vi.useFakeTimers()
  FakeRO.instances = []
  globalThis.ResizeObserver = FakeRO as unknown as typeof ResizeObserver
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
})

afterEach(() => {
  vi.useRealTimers()
  globalThis.ResizeObserver = origRO as typeof ResizeObserver
  globalThis.requestAnimationFrame = origRaf
})

/** Mounts glued to the bottom of a settled transcript. */
function mountAtBottom(runActive: boolean, key: string) {
  const { el, state, writes } = makeScroller({ scrollTop: 5000 - 700, scrollHeight: 5000, clientHeight: 700 })
  const ref: RefObject<HTMLDivElement | null> = { current: el }
  const view = renderHook(
    (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
    {
      initialProps: {
        items: mkItems(60),
        sessionId: `prepaint-${key}`,
        getKey,
        externalScrollerRef: ref,
        followOutput: true,
        runActive,
      },
    },
  )
  act(() => { vi.advanceTimersByTime(300) })
  writes.length = 0
  return { view, el, state, writes, bottom: () => state.scrollHeight - state.clientHeight }
}

/** A height commit: rows measure, then the debounced sync announces it. */
function landHeightCommit(view: ReturnType<typeof mountAtBottom>['view'], rows = 60) {
  act(() => {
    for (let i = 0; i < rows; i++) view.result.current.measureRef(i)(mkRowNode(95))
  })
  act(() => { vi.advanceTimersByTime(400) })
}

describe('pre-paint bottom pin and the composer', () => {
  it('does not chase a SHRINKING viewport even mid-turn (typing while steering)', () => {
    // Isolates the shrink freeze: a turn IS running, so the idle rule cannot be
    // what holds the reader here. Composer grows, same content, less viewport —
    // the bottom moves 40px further down than where they sit.
    const { view, state, writes } = mountAtBottom(true, 'typing-midturn')
    const parked = state.scrollTop
    state.clientHeight -= 40
    landHeightCommit(view)

    expect(state.scrollTop).toBe(parked)
    expect(writes.filter((w) => w > parked)).toEqual([])
  })

  it('carries a STILL reader back when content grows under them with nothing running', () => {
    // Viewport unchanged, no turn, no input: content grew below the fold with
    // nobody scrolling. Follow is still armed and the reader is no longer at the
    // bottom -- but every pixel of that gap is ours, so the idle rule must not
    // read it as the reader having left. This is the entry case on WebKit, which
    // has no native scroll anchoring to absorb a post-pin reprice: releasing here
    // left the transcript open a viewport above the end with nothing to bring it
    // back.
    const { view, state, writes, bottom } = mountAtBottom(false, 'idle-still')
    state.scrollHeight += 300
    landHeightCommit(view)

    expect(state.scrollTop).toBe(bottom())
    expect(writes).toContain(bottom())
  })

  it('does not re-target the bottom when nothing is running and the reader has moved', () => {
    // Same growth, but the reader touched the scroller since we last placed
    // them. With nothing running there is no output to follow, so the gap is
    // theirs to keep: the idle rule releases rather than yanking them down.
    const { view, el, state, writes } = mountAtBottom(false, 'idle-moved')
    const parked = state.scrollTop
    act(() => { el.dispatchEvent(new Event('wheel')) })
    state.scrollHeight += 300
    landHeightCommit(view)

    expect(state.scrollTop).toBe(parked)
    expect(writes.filter((w) => w > parked)).toEqual([])
  })

  it('still re-targets the bottom for a genuine reprice mid-turn', () => {
    // The path exists so a large reprice is invisible to a bottom-pinned reader;
    // neither guard may cost that. Viewport unchanged, a turn running.
    const { view, state } = mountAtBottom(true, 'reprice')
    const parked = state.scrollTop
    state.scrollHeight += 300
    landHeightCommit(view)

    expect(state.scrollTop).toBeGreaterThan(parked)
  })
})
