/**
 * Feature: chat-virtualizer -- a row appended under a FOLLOWED reader mounts in
 * the SAME render that sees the longer list.
 *
 * The mounted window is only recomputed from a scroll event or a resize. A row
 * appended at the tail (the prompt the reader just sent, the reply that starts
 * streaming under it) therefore committed OUTSIDE the window: it was priced
 * into the bottom spacer at the running height estimate, and the append pin
 * carried the followed reader to the bottom of that estimate. A frame later the
 * pin's own scroll event mounted the row at its real height and the bottom
 * moved back. With a short prompt under a transcript of long replies the
 * estimate is a few hundred pixels too tall, so every send moved the
 * transcript up by the estimate and then back down by the difference.
 *
 * The fix extends the window over the new tail rows in the render that sees
 * them, while the reader follows the tail. These cases pin that, and that a
 * reader who scrolled up, a bulk growth and a collapsed box are not affected.
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
// The estimate a never-measured row is priced at, deliberately far from its
// real height: the gap is the jump a reader saw.
const ESTIMATE_H = 300
const CLIENT = 400
const OVERSCAN = 2

function rect(top: number, height: number): DOMRect {
  return {
    top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: top,
    toJSON() { return {} },
  } as DOMRect
}

/** The row keys and the bottom spacer height of every commit. */
interface Commit { keys: string[]; after: number }

function Row({ index, measure }: { index: number; measure: (el: HTMLElement | null) => void }) {
  return <div data-index={index} ref={measure} />
}

type RetainRange = ReturnType<typeof useVirtualChat<Item>>['retainRange']

function Harness({ items, scrollerRef, commits, retainRef }: {
  items: Item[]
  scrollerRef: RefObject<HTMLDivElement | null>
  commits: Commit[]
  retainRef: { current: RetainRange | null }
}) {
  const v = useVirtualChat<Item>({
    items, sessionId: 'tail-append-same-render', getKey, overscan: OVERSCAN,
    externalScrollerRef: scrollerRef, estimatedHeight: ESTIMATE_H,
  })
  retainRef.current = v.retainRange
  const keys = v.virtualItems.map((it) => String(it.key))
  // No deps: runs once per COMMIT, so it records exactly what reached the DOM.
  useLayoutEffect(() => { commits.push({ keys, after: v.offsetAfter }) })
  return (
    <div ref={scrollerRef as RefObject<HTMLDivElement>} data-scroller>
      <div ref={v.topSentinelRef} data-sentinel="top" />
      <div data-spacer="before" style={{ height: v.offsetBefore }} />
      {v.virtualItems.map((it) => (
        <Row key={it.key} index={it.index} measure={v.measureRef(it.index)} />
      ))}
      <div data-spacer="after" style={{ height: v.offsetAfter }} />
      <div ref={v.bottomSentinelRef} data-sentinel="bottom" />
    </div>
  )
}

describe('useVirtualChat: a tail append under a followed reader mounts in the same render', () => {
  let restore: (() => void) | null = null
  let origRaf: typeof requestAnimationFrame
  let origIO: typeof IntersectionObserver
  let frames: FrameRequestCallback[] = []

  /** Deterministic layout: rows are REAL_H tall, spacers their inline height. */
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
    return () => Array.from(scroller.children).reduce((sum, c) => sum + childHeight(c), 0)
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

  /** 30 rows opened at the bottom; `scrollUp` releases follow first. */
  function mountTranscript(opts: { scrollUp?: boolean } = {}) {
    const commits: Commit[] = []
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    const retainRef: { current: RetainRange | null } = { current: null }
    let scrollTop = 0
    let clientHeight = CLIENT
    let items = mkItems(30)
    const harness = (list: Item[]) => <Harness items={list} scrollerRef={scrollerRef} commits={commits} retainRef={retainRef} />
    const view = rtlRender(harness(items))
    const el = scrollerRef.current!
    const contentHeight = installFakeLayout(el)
    Object.defineProperty(el, 'scrollTop', {
      configurable: true,
      get: () => scrollTop,
      // The engine clamps a write to the scrollable range.
      set: (v: number) => { scrollTop = Math.max(0, Math.min(v, contentHeight() - clientHeight)) },
    })
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => clientHeight })
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => contentHeight() })
    // Re-render once so the rows measure against the fake layout, then let the
    // entry pin land.
    act(() => { view.rerender(harness([...items])) })
    flushFrames()
    act(() => { el.scrollTop = el.scrollHeight; el.dispatchEvent(new Event('scroll')) })
    flushFrames()
    if (opts.scrollUp) {
      act(() => {
        el.dispatchEvent(new WheelEvent('wheel', { deltaY: -600 }))
        el.scrollTop = el.scrollTop - 600
        el.dispatchEvent(new Event('scroll'))
      })
      flushFrames()
    }
    commits.length = 0
    const append = (rows: Item[]) => {
      items = [...items, ...rows]
      act(() => { view.rerender(harness(items)) })
    }
    // A backgrounded tab / display:none pane: the box has no height.
    const collapse = () => { clientHeight = 0 }
    return { el, commits, append, collapse, retainRef }
  }

  it('mounts the sent prompt in the first commit that holds it', () => {
    const { commits, append } = mountTranscript()
    append([{ id: 'sent' }])

    expect(commits.length).toBeGreaterThan(0)
    // Every commit that sees the new row renders it as a row, never as spacer.
    for (const c of commits) {
      expect(c.keys).toContain('sent')
      expect(c.after).toBe(0)
    }
  })

  it('mounts a prompt and the reply row appended together', () => {
    const { commits, append } = mountTranscript()
    append([{ id: 'sent' }, { id: 'reply' }])

    expect(commits.length).toBeGreaterThan(0)
    for (const c of commits) {
      expect(c.keys).toEqual(expect.arrayContaining(['sent', 'reply']))
      expect(c.after).toBe(0)
    }
  })

  it('does not move the window of a reader who scrolled up', () => {
    const { commits, append } = mountTranscript({ scrollUp: true })
    append([{ id: 'sent' }])

    expect(commits.length).toBeGreaterThan(0)
    // Released: the reader owns the window, and the new row stays off it.
    expect(commits[0].keys).not.toContain('sent')
  })

  it('leaves a bulk growth to the hydration force-pin', () => {
    const { commits, append } = mountTranscript()
    // One row past the cap: the follow owner's hydration path, which mounts
    // its own tail window in a layout effect. Mounting the whole growth in the
    // render first would be work thrown away a moment later -- and on a real
    // hydration, every row of the conversation.
    const bulk = mkItems(OVERSCAN + 2, 'b')
    append(bulk)

    expect(commits.length).toBeGreaterThan(0)
    expect(commits[0].keys).not.toContain('b0')
  })

  it('leaves a collapsed box alone, where no pin trims the window behind the rows', () => {
    const { commits, append, collapse } = mountTranscript()
    collapse()
    append([{ id: 'sent' }])

    expect(commits.length).toBeGreaterThan(0)
    expect(commits[0].keys).not.toContain('sent')
  })

  it('still mounts the sent prompt with a selection retained over the bottom rows', () => {
    const { commits, append, retainRef } = mountTranscript()
    // A native selection over the bottom rows: every window write is widened
    // by it, and the selected rows must stay mounted alongside the new one.
    act(() => { retainRef.current!({ start: 20, end: 30 }) })
    commits.length = 0
    append([{ id: 'sent' }])

    expect(commits.length).toBeGreaterThan(0)
    for (const c of commits) {
      expect(c.keys).toContain('sent')
      expect(c.keys).toContain('m20')
    }
  })
})
