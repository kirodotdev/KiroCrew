/**
 * Feature: chat-virtualizer — prepend compensation for load-older history.
 *
 * Prepending older messages shifts every index up, so the row being read moves
 * down by the inserted height while scrollTop stays put and the transcript
 * lurches away. The hook snapshots the topmost visible row during render,
 * re-bases the window so it stays mounted, then corrects scrollTop by how far
 * that row actually travelled.
 *
 * jsdom has no layout, so this installs the same deterministic layout engine the
 * integration suite uses: getBoundingClientRect walks the scroller's children
 * summing heights minus scrollTop. That makes the jump reproducible — the rows
 * genuinely move — rather than asserting against source text.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render as rtlRender, act } from '@testing-library/react'
import { type RefObject } from 'react'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number, prefix = 'm'): Item[] =>
  Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i}` }))

// Every mounted row renders this tall, while the OffsetIndex credits unmeasured
// rows the flat estimate (80) — the same asymmetry the integration suite uses.
const REAL_H = 100
const CLIENT = 400
const SCROLL_HEIGHT = 3000

function rect(top: number, height: number): DOMRect {
  return {
    top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: top,
    toJSON() { return {} },
  } as DOMRect
}

function Harness({ items, scrollerRef }: {
  items: Item[]
  scrollerRef: RefObject<HTMLDivElement | null>
}) {
  const v = useVirtualChat<Item>({
    items, sessionId: 'prepend', getKey, overscan: 2, externalScrollerRef: scrollerRef,
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

/** Topmost row still visible (bottom edge below the viewport top), by virtual
 *  key — the identity that survives the index shift a prepend causes. */
function topVisible(el: HTMLElement): { key: string; idx: number; top: number } | null {
  const srTop = el.getBoundingClientRect().top
  let best: { key: string; idx: number; top: number } | null = null
  el.querySelectorAll('[data-key]').forEach((node) => {
    const r = (node as HTMLElement).getBoundingClientRect()
    if (r.bottom - srTop <= 0) return
    const idx = Number((node as HTMLElement).getAttribute('data-index'))
    if (!best || idx < best.idx) {
      best = { key: (node as HTMLElement).getAttribute('data-key')!, idx, top: r.top - srTop }
    }
  })
  return best
}

/** Visible rows in index order. [0] is the anchor the hook would pick; [1] is the
 *  next survivor it must fall forward to when [0]'s key is retired. */
function visibleByIndex(el: HTMLElement): { key: string; idx: number; top: number }[] {
  const srTop = el.getBoundingClientRect().top
  const out: { key: string; idx: number; top: number }[] = []
  el.querySelectorAll('[data-key]').forEach((node) => {
    const r = (node as HTMLElement).getBoundingClientRect()
    if (r.bottom - srTop <= 0) return
    out.push({
      key: (node as HTMLElement).getAttribute('data-key')!,
      idx: Number((node as HTMLElement).getAttribute('data-index')),
      top: r.top - srTop,
    })
  })
  return out.sort((a, b) => a.idx - b.idx)
}

function screenTopOf(el: HTMLElement, key: string): number | null {
  const node = el.querySelector(`[data-key="${key}"]`) as HTMLElement | null
  if (!node) return null
  return node.getBoundingClientRect().top - el.getBoundingClientRect().top
}

describe('useVirtualChat: prepend compensation (load older history)', () => {
  let restore: (() => void) | null = null
  let origRaf: typeof requestAnimationFrame
  let origIO: typeof IntersectionObserver
  let frames: FrameRequestCallback[] = []

  function installFakeLayout(scroller: HTMLElement, clientHeight: number) {
    const proto = HTMLElement.prototype
    const origRect = proto.getBoundingClientRect
    const origOffsetH = Object.getOwnPropertyDescriptor(proto, 'offsetHeight')

    const childHeight = (child: Element): number => {
      if ((child as HTMLElement).getAttribute('data-index') !== null) return REAL_H
      const h = (child as HTMLElement).style?.height
      return h ? parseFloat(h) : 0
    }

    proto.getBoundingClientRect = function (this: HTMLElement): DOMRect {
      if (this === scroller) return rect(0, clientHeight)
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
      get(this: HTMLElement) {
        return this.getAttribute('data-index') !== null ? REAL_H : 0
      },
    })

    restore = () => {
      proto.getBoundingClientRect = origRect
      if (origOffsetH) Object.defineProperty(proto, 'offsetHeight', origOffsetH)
      else delete (proto as unknown as Record<string, unknown>).offsetHeight
    }
  }

  // Deterministic rAF (the mount pins schedule frames) and a no-op
  // IntersectionObserver, which jsdom does not provide at all.
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

  /** Mounts 30 rows, then scrolls up so stick is released and the window sits
   *  mid-transcript — the state a user reading history is in. */
  function mountScrolledUp() {
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    let scrollTop = 0
    const view = rtlRender(<Harness items={mkItems(30)} scrollerRef={scrollerRef} />)
    const el = scrollerRef.current!
    Object.defineProperty(el, 'scrollTop', {
      configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v },
    })
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => CLIENT })
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => SCROLL_HEIGHT })
    installFakeLayout(el, CLIENT)
    act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })
    act(() => { scrollTop = 2160; el.dispatchEvent(new Event('scroll')) })
    act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })
    return { el, view, scrollerRef, readScrollTop: () => scrollTop }
  }

  it('holds the reading position when older history is prepended', () => {
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp()

    const before = topVisible(el)
    expect(before).not.toBeNull()
    const beforeTop = readScrollTop()

    // Ten older messages at the FRONT shift every index by 10 — wider than the
    // mounted window, so without the re-base the anchor unmounts unmeasured.
    act(() => {
      view.rerender(
        <Harness items={[...mkItems(10, 'p'), ...mkItems(30)]} scrollerRef={scrollerRef} />,
      )
    })

    const afterTop = screenTopOf(el, before!.key)
    expect(afterTop).not.toBeNull()
    expect(Math.abs(afterTop! - before!.top)).toBeLessThanOrEqual(1)
    // The compensation is a scrollTop write: the same row is held in place by
    // moving the viewport down over the newly inserted content.
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
  })

  it('falls forward to a surviving row when regrouping retires the anchor key', () => {
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp()

    const visible = visibleByIndex(el)
    expect(visible.length).toBeGreaterThan(1)
    const retired = visible[0]
    const survivor = visible[1]
    const beforeTop = readScrollTop()

    // Models the real hazard: a turn takes its LEAD item's key, so an older page
    // joining the top turn RENAMES that row while it stays on screen.
    const renamed = mkItems(30).map((it) =>
      it.id === retired.key ? { id: `${it.id}-regrouped` } : it,
    )
    act(() => {
      view.rerender(
        <Harness items={[...mkItems(10, 'p'), ...renamed]} scrollerRef={scrollerRef} />,
      )
    })

    // The retired key is genuinely gone -- otherwise this test proves nothing.
    expect(screenTopOf(el, retired.key)).toBeNull()
    // Compensation still ran, anchored on the next surviving row.
    const survivorAfter = screenTopOf(el, survivor.key)
    expect(survivorAfter).not.toBeNull()
    expect(Math.abs(survivorAfter! - survivor.top)).toBeLessThanOrEqual(1)
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
  })

  it('does not compensate when items are APPENDED, only prepended', () => {
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp()

    const before = topVisible(el)
    expect(before).not.toBeNull()
    const beforeTop = readScrollTop()

    // Growth alone must not trigger it. Index 0 is unchanged, so nothing was
    // inserted above the reader and a scroll correction here would be the bug.
    act(() => {
      view.rerender(
        <Harness items={[...mkItems(30), ...mkItems(10, 'z')]} scrollerRef={scrollerRef} />,
      )
    })

    expect(readScrollTop()).toBe(beforeTop)
    // Row position is NOT asserted: an append drifts it identically on an
    // unmodified hook (verified), so that drift is not this path's to fix.
    expect(screenTopOf(el, before!.key)).not.toBeNull()
  })
})
