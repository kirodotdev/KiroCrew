/**
 * Feature: chat-virtualizer — a retained range follows its rows when a
 * load-older prepend renumbers them.
 *
 * The selection owner retains the rows under a native text selection by their
 * display indices, and re-retains only on the next selectionchange. A landed
 * older page shifts every index by the page size, so until the reader moves a
 * handle the retained numbers name the oldest rows of that page. The window is
 * widened by the retained range on every write, so the stale range kept those
 * rows mounted, one more page of them per landing while the selection was held,
 * and under a displacement that moves rows UP it let the rows under the
 * selection unmount, which re-roots the selection. The hook now moves the
 * retained range by the same displacement it re-bases the window with, in each
 * of the three owners that re-base it: the anchored landing (part 1), the
 * anchor-miss arithmetic fallback, and follow's bulk prepend under a reader
 * pinned to the bottom.
 *
 * Layout is the same deterministic engine the prependAnchor suite uses.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render as rtlRender, act } from '@testing-library/react'
import { type RefObject } from 'react'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number, prefix = 'm'): Item[] =>
  Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i}` }))

const REAL_H = 100
const CLIENT = 400
const OLDER = 10

function rect(top: number, height: number): DOMRect {
  return {
    top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: top,
    toJSON() { return {} },
  } as DOMRect
}

type RetainRange = ReturnType<typeof useVirtualChat<Item>>['retainRange']

function Harness({ items, scrollerRef, retainRef }: {
  items: Item[]
  scrollerRef: RefObject<HTMLDivElement | null>
  retainRef: { current: RetainRange | null }
}) {
  const v = useVirtualChat<Item>({
    items, sessionId: 'retained-prepend', getKey, overscan: 2,
    externalScrollerRef: scrollerRef, estimatedHeight: REAL_H,
  })
  retainRef.current = v.retainRange
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

/** Keys of the mounted rows, in index order. */
function mountedKeys(el: HTMLElement): string[] {
  return Array.from(el.querySelectorAll<HTMLElement>('[data-key]'))
    .sort((a, b) => Number(a.getAttribute('data-index')) - Number(b.getAttribute('data-index')))
    .map((n) => n.getAttribute('data-key')!)
}

describe('useVirtualChat: a retained range follows its rows across a prepend', () => {
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

  /** 30 rows in a scroller whose height follows its children, so a follow pin
   *  has a real bottom to land on. The caller places the reader. */
  function mount() {
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    const retainRef: { current: RetainRange | null } = { current: null }
    let scrollTop = 0
    const view = rtlRender(<Harness items={mkItems(30)} scrollerRef={scrollerRef} retainRef={retainRef} />)
    const el = scrollerRef.current!
    Object.defineProperty(el, 'scrollTop', {
      configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v },
    })
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => CLIENT })
    Object.defineProperty(el, 'scrollHeight', {
      configurable: true,
      get: () => Array.from(el.children).reduce((h, c) => {
        const node = c as HTMLElement
        if (node.getAttribute('data-index') !== null) return h + REAL_H
        return h + (parseFloat(node.style?.height || '0') || 0)
      }, 0),
    })
    installFakeLayout(el)
    flushFrames()
    const land = (items: Item[]) => {
      act(() => { view.rerender(<Harness items={items} scrollerRef={scrollerRef} retainRef={retainRef} />) })
      flushFrames()
    }
    const setScrollTop = (v: number) => { scrollTop = v }
    return { el, retain: (start: number, end: number) => act(() => { retainRef.current!({ start, end }) }), land, setScrollTop }
  }

  it('keeps the selected rows mounted, not the older rows that took their numbers (anchored landing)', () => {
    const { el, retain, land, setScrollTop } = mount()
    // Reading mid-transcript, stick released: rows 2 and 3 are well above the
    // window, where a selection's start sits once its handle scrolled away.
    act(() => { setScrollTop(2160); el.dispatchEvent(new Event('scroll')) })
    flushFrames()
    expect(mountedKeys(el)).not.toContain('m2')
    retain(2, 4)
    expect(mountedKeys(el)).toEqual(expect.arrayContaining(['m2', 'm3']))

    // An older page lands at the front: m2 and m3 are rows 12 and 13 now, and
    // p2 and p3 carry their old numbers.
    land([...mkItems(OLDER, 'p'), ...mkItems(30)])
    // The next window recompute is the first write that reads the retained
    // range on its own, with the reader's rows no longer carried by the re-base.
    act(() => { el.dispatchEvent(new Event('scroll')) })
    flushFrames()

    const mounted = mountedKeys(el)
    expect(mounted).toEqual(expect.arrayContaining(['m2', 'm3']))
    expect(mounted).not.toContain('p2')
    expect(mounted).not.toContain('p3')
    // Nothing above the selection's start is held: the span starts at m2.
    expect(mounted[0]).toBe('m2')
  })

  it('moves the range by the net count when no row can anchor the landing (arithmetic fallback)', () => {
    const { el, retain, land, setScrollTop } = mount()
    act(() => { setScrollTop(2160); el.dispatchEvent(new Event('scroll')) })
    flushFrames()
    retain(2, 4)
    // Every mounted row sits above the viewport when the page lands (the
    // scroller moved with no scroll event, as a programmatic write does), so
    // no row anchors the landing and part 1 falls back to the net count.
    setScrollTop(5000)
    land([...mkItems(OLDER, 'p'), ...mkItems(30)])
    act(() => { el.dispatchEvent(new Event('scroll')) })
    flushFrames()

    const mounted = mountedKeys(el)
    expect(mounted).toEqual(expect.arrayContaining(['m2', 'm3']))
    expect(mounted).not.toContain('p2')
    expect(mounted).not.toContain('p3')
    expect(mounted[0]).toBe('m2')
  })

  it('moves the range with a bulk prepend under a reader pinned to the bottom (follow re-base)', () => {
    const { el, retain, land } = mount()
    // Following the tail: rows 28 and 29 are the last two, on screen.
    expect(mountedKeys(el)).toEqual(expect.arrayContaining(['m28', 'm29']))
    expect(mountedKeys(el)).not.toContain('m2')
    // The retained rows sit far above the tail window, so the count the range
    // moves by is read off the first mounted row; a range at the tail would
    // hide a wrong count inside the rows the tail window mounts anyway.
    retain(2, 4)
    expect(mountedKeys(el)[0]).toBe('m2')

    // A bulk prepend keeps the tail, so follow re-bases the numeric window
    // itself; m2 and m3 are rows 12 and 13 now, p2 and p3 took 2 and 3.
    land([...mkItems(OLDER, 'p'), ...mkItems(30)])
    act(() => { el.dispatchEvent(new Event('scroll')) })
    flushFrames()

    const mounted = mountedKeys(el)
    expect(mounted).toEqual(expect.arrayContaining(['m2', 'm3', 'm28', 'm29']))
    expect(mounted).not.toContain('p2')
    expect(mounted).not.toContain('p3')
    expect(mounted[0]).toBe('m2')
  })
})
