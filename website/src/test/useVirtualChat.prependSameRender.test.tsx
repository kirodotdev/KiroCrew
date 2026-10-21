/**
 * Feature: chat-virtualizer — a load-older prepend re-bases the window in the
 * SAME render that sees the longer list.
 *
 * A prepend shifts every index up by the page size. If the render that first
 * sees the new list commits with the previous window range, that range now
 * names the oldest rows of the page that just landed: every row the reader is
 * looking at unmounts, a throwaway set mounts, and the next commit swaps them
 * back as brand-new nodes. On a phone that teardown is one 120-170 ms long task
 * in the middle of a momentum scroll.
 *
 * These cases pin the user-visible half of the fix in jsdom: the reader's rows
 * keep their DOM nodes across the prepend, and no commit ever shows the stale
 * window. A third pins that a retained selection range, which widens every
 * window write, cannot keep the same-render re-base from converging. Layout is
 * the same deterministic engine the prependAnchor suite uses.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render as rtlRender, act } from '@testing-library/react'
import { useLayoutEffect, type RefObject } from 'react'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number, prefix = 'm'): Item[] =>
  Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i}` }))

const REAL_H = 100
const CLIENT = 400
const SCROLL_HEIGHT = 3000

function rect(top: number, height: number): DOMRect {
  return {
    top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: top,
    toJSON() { return {} },
  } as DOMRect
}

/** Mount and unmount log per row key, plus the row keys of every commit. */
interface Log { mounts: string[]; unmounts: string[]; commits: string[][] }

function Row({ rowKey, index, measure, log }: {
  rowKey: string
  index: number
  measure: (el: HTMLElement | null) => void
  log: Log
}) {
  useLayoutEffect(() => {
    log.mounts.push(rowKey)
    return () => { log.unmounts.push(rowKey) }
  }, [rowKey, log])
  return <div data-index={index} data-key={rowKey} ref={measure} />
}

type RetainRange = ReturnType<typeof useVirtualChat<Item>>['retainRange']

function Harness({ items, scrollerRef, log, retainRef }: {
  items: Item[]
  scrollerRef: RefObject<HTMLDivElement | null>
  log: Log
  retainRef?: { current: RetainRange | null }
}) {
  const v = useVirtualChat<Item>({
    items, sessionId: 'prepend-same-render', getKey, overscan: 2,
    externalScrollerRef: scrollerRef, estimatedHeight: REAL_H,
  })
  if (retainRef) retainRef.current = v.retainRange
  const keys = v.virtualItems.map((it) => String(it.key))
  // No deps: runs once per COMMIT, so it records exactly what reached the DOM.
  useLayoutEffect(() => { log.commits.push(keys) })
  return (
    <div ref={scrollerRef as RefObject<HTMLDivElement>} data-scroller>
      <div ref={v.topSentinelRef} data-sentinel="top" />
      <div data-spacer="before" style={{ height: v.offsetBefore }} />
      {v.virtualItems.map((it) => (
        <Row key={it.key} rowKey={String(it.key)} index={it.index} measure={v.measureRef(it.index)} log={log} />
      ))}
      <div data-spacer="after" style={{ height: v.offsetAfter }} />
      <div ref={v.bottomSentinelRef} data-sentinel="bottom" />
    </div>
  )
}

/** Keys of the rows on screen (any part inside the viewport). */
function visibleKeys(el: HTMLElement): string[] {
  const srTop = el.getBoundingClientRect().top
  const out: string[] = []
  el.querySelectorAll('[data-key]').forEach((node) => {
    const r = (node as HTMLElement).getBoundingClientRect()
    if (r.bottom - srTop <= 0 || r.top - srTop >= CLIENT) return
    out.push((node as HTMLElement).getAttribute('data-key')!)
  })
  return out
}

describe('useVirtualChat: a prepend re-bases the window in the same render', () => {
  let restore: (() => void) | null = null
  let origRaf: typeof requestAnimationFrame
  let origIO: typeof IntersectionObserver
  let frames: FrameRequestCallback[] = []

  function installFakeLayout(scroller: HTMLElement) {
    const proto = HTMLElement.prototype
    const origRect = proto.getBoundingClientRect
    const origOffsetH = Object.getOwnPropertyDescriptor(proto, 'offsetHeight')
    const childHeight = (child: Element): number => {
      if ((child as HTMLElement).getAttribute('data-index') !== null) return REAL_H
      const h = (child as HTMLElement).style?.height
      return h ? parseFloat(h) : 0
    }
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
    frames = []
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      frames.push(cb)
      return frames.length
    }) as typeof requestAnimationFrame
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
  })

  afterEach(() => {
    restore?.()
    restore = null
    globalThis.requestAnimationFrame = origRaf
    globalThis.IntersectionObserver = origIO
  })

  const flushFrames = () => act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })

  /** 30 rows, scrolled up mid-transcript so stick is released, then a 10-row
   *  older page lands at the front. Returns the logs split at the prepend. */
  function prependWhileReading(opts: { retainVisible?: boolean } = {}) {
    const log: Log = { mounts: [], unmounts: [], commits: [] }
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    const retainRef: { current: RetainRange | null } = { current: null }
    let scrollTop = 0
    const view = rtlRender(<Harness items={mkItems(30)} scrollerRef={scrollerRef} log={log} retainRef={retainRef} />)
    const el = scrollerRef.current!
    Object.defineProperty(el, 'scrollTop', {
      configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v },
    })
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => CLIENT })
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => SCROLL_HEIGHT })
    installFakeLayout(el)
    flushFrames()
    act(() => { scrollTop = 1500; el.dispatchEvent(new Event('scroll')) })
    flushFrames()

    const visible = visibleKeys(el)
    const mountedBefore = Array.from(el.querySelectorAll('[data-key]')).map(
      (n) => (n as HTMLElement).getAttribute('data-key')!,
    )
    const nodesBefore = new Map(visible.map((k) => [k, el.querySelector(`[data-key="${k}"]`)]))
    if (opts.retainVisible) {
      // A native selection over the rows on screen, retained by its
      // pre-prepend indices: the selection owner only re-retains on the next
      // selectionchange, after the prepend has rendered.
      const indices = visible.map((k) => Number(k.slice(1)))
      act(() => { retainRef.current!({ start: Math.min(...indices), end: Math.max(...indices) + 1 }) })
    }
    log.mounts.length = 0
    log.unmounts.length = 0
    log.commits.length = 0

    act(() => {
      view.rerender(
        <Harness items={[...mkItems(10, 'p'), ...mkItems(30)]} scrollerRef={scrollerRef} log={log} retainRef={retainRef} />,
      )
    })
    flushFrames()
    return { el, log, visible, mountedBefore, nodesBefore }
  }

  it('keeps the DOM node of every visible row across a prepend', () => {
    const { el, log, visible, nodesBefore } = prependWhileReading()

    // Not vacuous: the reader had rows on screen and they are still there.
    expect(visible.length).toBeGreaterThan(0)
    for (const k of visible) {
      expect(log.unmounts).not.toContain(k)
      const node = el.querySelector(`[data-key="${k}"]`)
      expect(node).not.toBeNull()
      expect(node).toBe(nodesBefore.get(k))
    }
  })

  it('never commits the stale window range, so no throwaway row mounts', () => {
    const { el, log, visible, mountedBefore } = prependWhileReading()

    // Every commit after the prepend still holds every row the reader saw.
    expect(log.commits.length).toBeGreaterThan(0)
    for (const committed of log.commits) {
      for (const k of visible) expect(committed).toContain(k)
    }
    // A row that mounted only to be torn down again is the stale commit's
    // signature; the rows that stayed mounted are the same set as before.
    const mountedAfter = Array.from(el.querySelectorAll('[data-key]')).map(
      (n) => (n as HTMLElement).getAttribute('data-key')!,
    )
    for (const k of log.mounts) expect(log.unmounts).not.toContain(k)
    for (const k of mountedBefore) {
      if (mountedAfter.includes(k)) expect(log.unmounts).not.toContain(k)
    }
  })

  it('converges when a retained selection widens the re-based range', () => {
    // The retained range still names the pre-prepend indices, so every window
    // write is widened below the re-based target. Comparing the committed range
    // against the raw target never matched, and each re-run queued another
    // update until React threw "Too many re-renders".
    const { el, log, visible } = prependWhileReading({ retainVisible: true })

    expect(visible.length).toBeGreaterThan(0)
    expect(log.commits.length).toBeGreaterThan(0)
    for (const k of visible) {
      expect(log.unmounts).not.toContain(k)
      expect(el.querySelector(`[data-key="${k}"]`)).not.toBeNull()
    }
  })
})
