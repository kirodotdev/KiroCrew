/**
 * Feature: chat-virtualizer — opening a session mounts the rows above the
 * viewport a few per frame.
 *
 * Opening a session lands its page in one bulk growth while the view follows
 * the tail. The window then widened by the whole overscan above the viewport in
 * the task right after the page painted, and that render (rows nobody can see
 * yet) held up the next frame. The rows above are staged in over the next
 * frames (LANDING_LEADING_STEPS), like a landed older page. The view must stay
 * at the bottom, every row on screen must stay mounted, and the whole overscan
 * must be mounted once the stages end.
 *
 * Layout is the deterministic engine the landing-stages suite uses: rows render
 * TALLER than the estimate.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render as rtlRender, act } from '@testing-library/react'
import { type RefObject } from 'react'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import { LANDING_LEADING_STEPS } from '../hooks/virtualizer/windowRange'

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

const REAL_H = 100
const ESTIMATE = 80
const OVERSCAN = 5
const CLIENT = 400
const ROWS = 30

function rect(top: number, height: number): DOMRect {
  return {
    top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: top,
    toJSON() { return {} },
  } as DOMRect
}

function Harness({ items, scrollerRef }: { items: Item[]; scrollerRef: RefObject<HTMLDivElement | null> }) {
  const v = useVirtualChat<Item>({
    items, sessionId: 'entry-stages', getKey, overscan: OVERSCAN,
    externalScrollerRef: scrollerRef, estimatedHeight: ESTIMATE,
  })
  return (
    <div ref={scrollerRef as RefObject<HTMLDivElement>} data-scroller>
      <div ref={v.topSentinelRef} data-sentinel="top" />
      <div data-spacer="before" style={{ height: v.offsetBefore }} />
      {v.virtualItems.map((it) => (
        <div key={it.key} data-index={it.index} data-key={it.key} ref={v.measureRef(it.index)} />
      ))}
      <div data-spacer="after" style={{ height: v.offsetAfter }} />
      <div ref={v.bottomSentinelRef} data-sentinel="bottom" />
    </div>
  )
}

const mountedIndexes = (el: HTMLElement): number[] =>
  Array.from(el.querySelectorAll('[data-index]')).map((n) => Number((n as HTMLElement).getAttribute('data-index')))

describe('useVirtualChat: opening a session stages the rows above the viewport', () => {
  let restore: (() => void) | null = null
  let origRaf: typeof requestAnimationFrame
  let origCaf: typeof cancelAnimationFrame
  let origIO: typeof IntersectionObserver
  let frames = new Map<number, FrameRequestCallback>()
  let nextId = 1

  const childHeight = (child: Element): number => {
    if ((child as HTMLElement).getAttribute('data-index') !== null) return REAL_H
    const h = (child as HTMLElement).style?.height
    return h ? parseFloat(h) : 0
  }
  const contentHeight = (scroller: HTMLElement) =>
    Array.from(scroller.children).reduce((s, c) => s + childHeight(c), 0)

  function installFakeLayout(scroller: HTMLElement) {
    const proto = HTMLElement.prototype
    const origRect = proto.getBoundingClientRect
    const origOffsetH = Object.getOwnPropertyDescriptor(proto, 'offsetHeight')
    proto.getBoundingClientRect = function (this: HTMLElement): DOMRect {
      if (this === scroller) return rect(0, CLIENT)
      if (this.parentElement === scroller) {
        let y = 0
        for (const sib of Array.from(scroller.children)) {
          if (sib === this) break
          y += childHeight(sib)
        }
        return rect(y - scroller.scrollTop, childHeight(this))
      }
      return origRect.call(this)
    }
    Object.defineProperty(proto, 'offsetHeight', {
      configurable: true,
      get(this: HTMLElement) { return this.getAttribute('data-index') !== null ? REAL_H : 0 },
    })
    restore = () => {
      proto.getBoundingClientRect = origRect
      if (origOffsetH) Object.defineProperty(proto, 'offsetHeight', origOffsetH)
      else delete (proto as unknown as Record<string, unknown>).offsetHeight
    }
  }

  beforeEach(() => {
    localStorage.clear()
    frames = new Map()
    nextId = 1
    origRaf = globalThis.requestAnimationFrame
    origCaf = globalThis.cancelAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      const id = nextId++
      frames.set(id, cb)
      return id
    }) as typeof requestAnimationFrame
    globalThis.cancelAnimationFrame = ((id: number) => { frames.delete(id) }) as typeof cancelAnimationFrame
    class FakeIO {
      constructor(readonly cb: IntersectionObserverCallback) {}
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() { return [] }
      root: Element | null = null
      rootMargin = ''
      thresholds: number[] = []
    }
    origIO = globalThis.IntersectionObserver
    globalThis.IntersectionObserver = FakeIO as unknown as typeof IntersectionObserver
    FakeRO.instances = []
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = FakeRO as unknown as typeof ResizeObserver
  })

  afterEach(() => {
    restore?.()
    restore = null
    globalThis.requestAnimationFrame = origRaf
    globalThis.cancelAnimationFrame = origCaf
    globalThis.IntersectionObserver = origIO
    globalThis.ResizeObserver = origRO
  })

  /** A browser reports a newly observed element's size once, after the
   *  frame's callbacks and layout and before paint: that is how a row mounted
   *  above the viewport reaches the bottom pin before anyone sees it. */
  class FakeRO {
    static instances: FakeRO[] = []
    pending = new Set<Element>()
    constructor(readonly cb: ResizeObserverCallback) { FakeRO.instances.push(this) }
    observe(t: Element) { this.pending.add(t) }
    unobserve(t: Element) { this.pending.delete(t) }
    disconnect() { this.pending.clear() }
    deliver() {
      const targets = [...this.pending].filter((t) => t.isConnected)
      this.pending.clear()
      if (targets.length === 0) return
      const entries = targets.map((target) => {
        const h = (target as HTMLElement).offsetHeight
        const box = [{ blockSize: h, inlineSize: 0 }]
        return { target, contentRect: rect(0, h), borderBoxSize: box, contentBoxSize: box } as unknown as ResizeObserverEntry
      })
      this.cb(entries, this as unknown as ResizeObserver)
    }
  }
  let origRO: typeof ResizeObserver

  /** Resize observations the last commit left, delivered as a browser does
   *  before paint: rounds until none are pending. */
  const deliverResizes = () => act(() => {
    for (let round = 0; round < 5 && FakeRO.instances.some((ro) => ro.pending.size > 0); round++) {
      for (const ro of FakeRO.instances) ro.deliver()
    }
  })
  /** One frame: its callbacks, then the resize observations, then "paint"
   *  (the assertions that follow). */
  const flushOneFrame = () => {
    act(() => {
      const due = [...frames.values()]
      frames.clear()
      for (const cb of due) cb(0)
    })
    deliverResizes()
  }

  /** Mount empty (a session whose page is still loading), then land its page. */
  function openSession() {
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    let scrollTop = 0
    const view = rtlRender(<Harness items={[]} scrollerRef={scrollerRef} />)
    const el = scrollerRef.current!
    Object.defineProperty(el, 'scrollTop', {
      configurable: true,
      get: () => scrollTop,
      set: (v: number) => { scrollTop = Math.max(0, Math.min(v, contentHeight(el) - CLIENT)) },
    })
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => CLIENT })
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => contentHeight(el) })
    installFakeLayout(el)
    act(() => { view.rerender(<Harness items={mkItems(ROWS)} scrollerRef={scrollerRef} />) })
    return el
  }

  /** Indexes of the rows with any part inside the viewport, by layout. */
  const onScreen = (el: HTMLElement): number[] => {
    const out: number[] = []
    el.querySelectorAll('[data-index]').forEach((n) => {
      const r = (n as HTMLElement).getBoundingClientRect()
      if (r.bottom > 0 && r.top < CLIENT) out.push(Number((n as HTMLElement).getAttribute('data-index')))
    })
    return out
  }
  /** Distance from the viewport bottom to the last row's bottom. */
  const tailGap = (el: HTMLElement): number => {
    const last = el.querySelector(`[data-index="${ROWS - 1}"]`) as HTMLElement | null
    return last ? Math.abs(last.getBoundingClientRect().bottom - CLIENT) : Infinity
  }
  const visibleRows = Math.ceil(CLIENT / REAL_H)
  /** Viewport pixels not covered by any mounted row. */
  const blankPx = (el: HTMLElement): number => {
    const spans = Array.from(el.querySelectorAll('[data-index]'))
      .map((n) => (n as HTMLElement).getBoundingClientRect())
      .map((r) => [Math.max(0, r.top), Math.min(CLIENT, r.bottom)] as const)
      .filter(([a, b]) => b > a)
      .sort((x, y) => x[0] - y[0])
    let covered = 0
    let at = 0
    for (const [a, b] of spans) {
      if (b <= at) continue
      covered += b - Math.max(a, at)
      at = b
    }
    return Math.round(CLIENT - covered)
  }

  it('mounts at most the first step above the viewport in the open task', () => {
    const el = openSession()
    const firstShown = Math.min(...onScreen(el))
    const above = mountedIndexes(el).filter((i) => i < firstShown)
    // Without staging the open mounts the whole overscan above the viewport.
    expect(above.length).toBeLessThanOrEqual(LANDING_LEADING_STEPS[0])
    expect(mountedIndexes(el)).toContain(ROWS - 1)
  })

  it('stays at the bottom with no blank rows on screen through every stage, then mounts the whole overscan', () => {
    const el = openSession()
    const counts: number[] = [mountedIndexes(el).length]
    const gaps: number[] = [tailGap(el)]
    const blank: number[] = []
    // The first frame places the reader at the bottom; from then on nothing moves.
    flushOneFrame()
    gaps.push(tailGap(el)); blank.push(blankPx(el)); counts.push(mountedIndexes(el).length)
    for (let i = 0; i < 50 && frames.size > 0; i++) {
      flushOneFrame()
      gaps.push(tailGap(el)); blank.push(blankPx(el)); counts.push(mountedIndexes(el).length)
    }
    // No part of the viewport shows spacer instead of a row, and the last row
    // stays on screen. (Whether the bottom pin holds to the pixel depends on
    // the browser's resize timing, which this layout fake does not model; the
    // PR checks it frame by frame in Chromium.)
    blank.forEach((b, i) => expect(b, `blank px after frame ${i + 1}`).toBe(0))
    gaps.slice(1).forEach((g, i) => expect(g, `last row on screen after frame ${i + 1}`).toBeLessThan(REAL_H))
    // Ends with the full overscan above the viewport mounted.
    const first = Math.min(...mountedIndexes(el))
    expect(first).toBeLessThanOrEqual(ROWS - visibleRows - OVERSCAN)
    // And it got there over more than one frame.
    expect(counts.filter((c, i) => i > 0 && c > counts[i - 1]).length).toBeGreaterThan(1)
  })

  it('shows rows, not spacer, when the reader scrolls up while the stages run', () => {
    const el = openSession()
    flushOneFrame()
    // Up by most of a viewport before the stages have finished.
    act(() => { el.scrollTop = el.scrollTop - 300; el.dispatchEvent(new Event('scroll')) })
    deliverResizes()
    for (let i = 0; i < 50 && frames.size > 0; i++) {
      flushOneFrame()
      expect(blankPx(el), `blank px after frame ${i + 1}`).toBe(0)
    }
    expect(blankPx(el)).toBe(0)
  })
})
