/**
 * Feature: chat-virtualizer — the first painted frame of an opened session
 * covers the viewport, even when its rows are shorter than the estimate.
 *
 * Opening a session lands its page in one bulk growth while the view follows
 * the tail, and the bulk pin mounts a fixed count of tail rows. In a chat of
 * one-line rows those rows end below the viewport top. The recompute that
 * widens the window is a passive effect whose update React renders in a later
 * task, so the frame that showed the opened rows painted spacer above them.
 *
 * "Painted" is modelled as the DOM once React's synchronous work for the open
 * is done: the open is committed with `flushSync` and outside `act()`, so the
 * work React leaves to a later task (where the passive recompute's update
 * lands) has not run when the assertions read the DOM.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { type RefObject } from 'react'
import { act } from '@testing-library/react'
import { flushSync } from 'react-dom'
import { createRoot, type Root } from 'react-dom/client'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

const ESTIMATE = 100
const OVERSCAN = 6
const CLIENT = 400
const ROWS = 40

function rect(top: number, height: number): DOMRect {
  return {
    top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: top,
    toJSON() { return {} },
  } as DOMRect
}

function Harness({ items, scrollerRef }: { items: Item[]; scrollerRef: RefObject<HTMLDivElement | null> }) {
  const v = useVirtualChat<Item>({
    items, sessionId: 'open-coverage', getKey, overscan: OVERSCAN,
    externalScrollerRef: scrollerRef, estimatedHeight: ESTIMATE, eagerFirstMeasure: true,
  })
  return (
    <div ref={scrollerRef as RefObject<HTMLDivElement>} data-scroller>
      <div ref={v.topSentinelRef} data-sentinel="top" />
      <div data-spacer="before" style={{ height: v.offsetBefore }} />
      {v.virtualItems.map((it) => (
        <div key={it.key} data-index={it.index} ref={v.measureRef(it.index)} />
      ))}
      <div data-spacer="after" style={{ height: v.offsetAfter }} />
      <div ref={v.bottomSentinelRef} data-sentinel="bottom" />
    </div>
  )
}

const mountedIndexes = (el: HTMLElement): number[] =>
  Array.from(el.querySelectorAll('[data-index]')).map((n) => Number((n as HTMLElement).getAttribute('data-index')))

describe('useVirtualChat: the first painted frame of an open covers the viewport', () => {
  const act_ = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
  let prevActEnv: boolean | undefined
  let restoreLayout: (() => void) | null = null
  let origRaf: typeof requestAnimationFrame
  let origCaf: typeof cancelAnimationFrame
  let origIO: typeof IntersectionObserver
  let origRO: typeof ResizeObserver
  let root: Root | null = null
  let host: HTMLDivElement | null = null
  let rowH = 0

  const childHeight = (child: Element): number => {
    if ((child as HTMLElement).getAttribute('data-index') !== null) return rowH
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
      get(this: HTMLElement) { return this.getAttribute('data-index') !== null ? rowH : 0 },
    })
    restoreLayout = () => {
      proto.getBoundingClientRect = origRect
      if (origOffsetH) Object.defineProperty(proto, 'offsetHeight', origOffsetH)
      else delete (proto as unknown as Record<string, unknown>).offsetHeight
    }
  }

  beforeEach(() => {
    localStorage.clear()
    prevActEnv = act_.IS_REACT_ACT_ENVIRONMENT
    origRaf = globalThis.requestAnimationFrame
    origCaf = globalThis.cancelAnimationFrame
    let nextId = 1
    // Frames never run: every assertion reads the open's own task.
    globalThis.requestAnimationFrame = (() => nextId++) as typeof requestAnimationFrame
    globalThis.cancelAnimationFrame = (() => {}) as typeof cancelAnimationFrame
    class Inert {
      constructor(readonly cb: unknown) {}
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() { return [] }
      root: Element | null = null
      rootMargin = ''
      thresholds: number[] = []
    }
    origIO = globalThis.IntersectionObserver
    globalThis.IntersectionObserver = Inert as unknown as typeof IntersectionObserver
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = Inert as unknown as typeof ResizeObserver
  })

  afterEach(() => {
    act_.IS_REACT_ACT_ENVIRONMENT = true
    act(() => { root?.unmount() })
    root = null
    host?.remove()
    host = null
    act_.IS_REACT_ACT_ENVIRONMENT = prevActEnv
    restoreLayout?.()
    restoreLayout = null
    globalThis.requestAnimationFrame = origRaf
    globalThis.cancelAnimationFrame = origCaf
    globalThis.IntersectionObserver = origIO
    globalThis.ResizeObserver = origRO
  })

  /** Mount empty (the session's page still loading), then land its page in a
   *  commit whose later-task work is left pending. */
  function openSession(height: number): HTMLElement {
    rowH = height
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    host = document.createElement('div')
    document.body.appendChild(host)
    act_.IS_REACT_ACT_ENVIRONMENT = true
    root = createRoot(host)
    act(() => { root!.render(<Harness items={[]} scrollerRef={scrollerRef} />) })
    const el = scrollerRef.current!
    let scrollTop = 0
    // A browser clamps scrollTop to the content as it is laid out now.
    const maxTop = () => Math.max(0, contentHeight(el) - CLIENT)
    Object.defineProperty(el, 'scrollTop', {
      configurable: true,
      get: () => Math.min(scrollTop, maxTop()),
      set: (v: number) => { scrollTop = Math.max(0, Math.min(v, maxTop())) },
    })
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => CLIENT })
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => contentHeight(el) })
    installFakeLayout(el)
    act_.IS_REACT_ACT_ENVIRONMENT = false
    flushSync(() => { root!.render(<Harness items={mkItems(ROWS)} scrollerRef={scrollerRef} />) })
    return el
  }

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
  const lastRowBottom = (el: HTMLElement): number | null => {
    const last = el.querySelector(`[data-index="${ROWS - 1}"]`) as HTMLElement | null
    return last ? last.getBoundingClientRect().bottom : null
  }

  it('one-line rows: no viewport px is left uncovered, and the last row is on screen', () => {
    const el = openSession(40)
    // The fixed tail window of overscan + 1 rows covers 280 of 400 px.
    expect(blankPx(el), `mounted ${mountedIndexes(el).join(',')}`).toBe(0)
    const bottom = lastRowBottom(el)
    expect(bottom).not.toBeNull()
    expect(bottom!).toBeGreaterThan(0)
    expect(bottom!).toBeLessThanOrEqual(CLIENT)
  })

  it('rows taller than the estimate: the open mounts the same tail window as before', () => {
    const el = openSession(160)
    expect(blankPx(el)).toBe(0)
    expect(mountedIndexes(el)).toEqual(Array.from({ length: OVERSCAN + 1 }, (_, i) => ROWS - OVERSCAN - 1 + i))
  })
})
