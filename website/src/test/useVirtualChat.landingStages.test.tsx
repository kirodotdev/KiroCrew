/**
 * Feature: chat-virtualizer — a landed older page mounts the rows above the
 * reader a few per frame.
 *
 * A reader near the top of the loaded history lands the next page: the window
 * re-bases onto the rows they are reading and then widens by the whole
 * overscan above them. Mounting those rows in the prepend's own task was the
 * single long task a phone fling showed (7 rows, 51-55 ms). The rows are
 * off-screen above the reader, so they are staged in over the next frames
 * (LANDING_LEADING_STEPS) and the reader must not move while they arrive.
 *
 * Layout is the deterministic engine the prependAnchor suite uses, with rows
 * rendering TALLER than the estimate, so every row that mounts above the
 * reader genuinely pushes them down unless it is compensated.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render as rtlRender, act } from '@testing-library/react'
import { type RefObject } from 'react'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import { LANDING_LEADING_STEPS } from '../hooks/virtualizer/windowRange'

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number, prefix = 'm'): Item[] =>
  Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i}` }))

const REAL_H = 100
const ESTIMATE = 80
const OVERSCAN = 5
const CLIENT = 400

function rect(top: number, height: number): DOMRect {
  return {
    top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: top,
    toJSON() { return {} },
  } as DOMRect
}

function Harness({ items, scrollerRef }: { items: Item[]; scrollerRef: RefObject<HTMLDivElement | null> }) {
  const v = useVirtualChat<Item>({
    items, sessionId: 'landing-stages', getKey, overscan: OVERSCAN,
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

const mountedKeys = (el: HTMLElement): string[] =>
  Array.from(el.querySelectorAll('[data-key]')).map((n) => (n as HTMLElement).getAttribute('data-key')!)

function screenTopOf(el: HTMLElement, key: string): number | null {
  const node = el.querySelector(`[data-key="${key}"]`) as HTMLElement | null
  return node ? node.getBoundingClientRect().top - el.getBoundingClientRect().top : null
}

/** Topmost row with any part on screen. */
function topVisibleKey(el: HTMLElement): string | null {
  let best: { key: string; idx: number } | null = null
  el.querySelectorAll('[data-key]').forEach((node) => {
    const r = (node as HTMLElement).getBoundingClientRect()
    if (r.bottom - el.getBoundingClientRect().top <= 0) return
    const idx = Number((node as HTMLElement).getAttribute('data-index'))
    if (!best || idx < best.idx) best = { key: (node as HTMLElement).getAttribute('data-key')!, idx }
  })
  return best ? (best as { key: string }).key : null
}

describe('useVirtualChat: a landed page stages the rows above the reader', () => {
  let restore: (() => void) | null = null
  let origRaf: typeof requestAnimationFrame
  let origCaf: typeof cancelAnimationFrame
  let origIO: typeof IntersectionObserver
  let frames = new Map<number, FrameRequestCallback>()
  let nextId = 1

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
  })

  afterEach(() => {
    restore?.()
    restore = null
    globalThis.requestAnimationFrame = origRaf
    globalThis.cancelAnimationFrame = origCaf
    globalThis.IntersectionObserver = origIO
  })

  /** One frame: the callbacks queued before it run; any they queue wait for
   *  the next one, as a browser's do. */
  const flushOneFrame = () => act(() => {
    const due = [...frames.values()]
    frames.clear()
    for (const cb of due) cb(0)
  })
  const flushAllFrames = () => {
    for (let i = 0; i < 50 && frames.size > 0; i++) flushOneFrame()
  }

  /** 30 rows with the reader near the TOP of them (the first rows mounted,
   *  stick released), then a 20-row older page lands at the front. */
  function landWhileNearTop() {
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    let scrollTop = 0
    const view = rtlRender(<Harness items={mkItems(30)} scrollerRef={scrollerRef} />)
    const el = scrollerRef.current!
    Object.defineProperty(el, 'scrollTop', {
      configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v },
    })
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => CLIENT })
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 5000 })
    installFakeLayout(el)
    flushAllFrames()
    act(() => { scrollTop = 2160; el.dispatchEvent(new Event('scroll')) })
    flushAllFrames()
    act(() => { scrollTop = 150; el.dispatchEvent(new Event('scroll')) })
    flushAllFrames()

    const anchor = topVisibleKey(el)!
    const anchorTop = screenTopOf(el, anchor)!
    const visibleBefore = mountedKeys(el).filter((k) => {
      const t = screenTopOf(el, k)!
      return t + REAL_H > 0 && t < CLIENT
    })
    const nodesBefore = new Map(visibleBefore.map((k) => [k, el.querySelector(`[data-key="${k}"]`)]))
    act(() => {
      view.rerender(<Harness items={[...mkItems(20, 'p'), ...mkItems(30)]} scrollerRef={scrollerRef} />)
    })
    return { el, anchor, anchorTop, visibleBefore, nodesBefore }
  }

  const landedRows = (el: HTMLElement) => mountedKeys(el).filter((k) => k.startsWith('p'))

  it('mounts at most the first step of the landed rows in the prepend task', () => {
    const { el, anchor, visibleBefore } = landWhileNearTop()

    // Not vacuous: the reader was on the first rows, so nothing above them was
    // mounted, and the full overscan would have been OVERSCAN landed rows.
    expect(anchor).toBe('m1')
    expect(visibleBefore.length).toBeGreaterThan(0)
    expect(landedRows(el).length).toBeLessThanOrEqual(LANDING_LEADING_STEPS[0])
    for (const k of visibleBefore) expect(mountedKeys(el)).toContain(k)
  })

  it('mounts the rest of the overscan over later frames, a step per frame', () => {
    const { el } = landWhileNearTop()

    const counts = [landedRows(el).length]
    for (let i = 0; i < 50 && frames.size > 0; i++) {
      flushOneFrame()
      counts.push(landedRows(el).length)
    }
    // Ends with the whole overscan above the reader mounted, as an unstaged
    // landing does in one task: the reader's top row is m1, and m0 was
    // already mounted above it, so the rest of the overscan is landed rows.
    expect(counts[counts.length - 1]).toBe(OVERSCAN - 1)
    expect(landedRows(el)).toEqual(['p16', 'p17', 'p18', 'p19'])
    // And no single frame mounted more than the largest step's increment or
    // the final top-up to the full overscan.
    const caps = [...LANDING_LEADING_STEPS, OVERSCAN]
    const maxStep = Math.max(...caps.map((c, i) => c - (i === 0 ? 0 : caps[i - 1])))
    for (let i = 1; i < counts.length; i++) expect(counts[i] - counts[i - 1]).toBeLessThanOrEqual(maxStep)
    expect(counts.filter((c, i) => i > 0 && c > counts[i - 1]).length).toBeGreaterThan(1)
  })

  it('holds the reader still through the prepend and every stage', () => {
    const { el, anchor, anchorTop, visibleBefore, nodesBefore } = landWhileNearTop()

    expect(Math.abs(screenTopOf(el, anchor)! - anchorTop)).toBeLessThanOrEqual(1)
    for (let i = 0; i < 50 && frames.size > 0; i++) {
      flushOneFrame()
      // Every staged row renders 20px taller than its estimate, so an
      // uncompensated stage moves the reader by at least that much.
      expect(Math.abs(screenTopOf(el, anchor)! - anchorTop), `after frame ${i + 1}`).toBeLessThanOrEqual(1)
    }
    // The rows on screen kept their DOM nodes the whole way.
    for (const k of visibleBefore) expect(el.querySelector(`[data-key="${k}"]`)).toBe(nodesBefore.get(k))
  })
})
