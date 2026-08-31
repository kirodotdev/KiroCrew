/**
 * Feature: chat-virtualizer — prepend compensation for load-older history.
 *
 * Prepending older messages shifts every index up, so the row being read moves
 * down by the inserted height while scrollTop stays put and the transcript
 * lurches away. The hook snapshots the topmost visible row during render,
 * re-bases the window so it stays mounted, then corrects scrollTop by how far
 * that row actually travelled.
 *
 * A tail APPEND lands on the same path (issue #4352): it inserts nothing above
 * the reader, but it re-syncs the offset tree, which re-prices every unmeasured
 * row from the running mean and so changes the height credited above them.
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

/** Same transcript, but getKey is INDEX-ADDRESSED the way ChatPage's is: a
 *  per-render key LIST looked up by position (ChatPage builds a deduped
 *  `rowKeys` array and its getKey returns `rowKeys[i]`). The function ignores
 *  the item argument and changes identity every render — so it only prices an
 *  item correctly when paired with the items of its OWN render. The prepend
 *  capture resolves the PREVIOUS render's items and must therefore use the
 *  getKey snapshotted with them; this harness is what gives that contract a
 *  failing shape. */
function PositionalHarness({ items, scrollerRef }: {
  items: Item[]
  scrollerRef: RefObject<HTMLDivElement | null>
}) {
  const keys = items.map((it) => it.id)
  const positionalGetKey = (_it: Item, i: number) => keys[i] ?? `oob-${i}`
  const v = useVirtualChat<Item>({
    items, sessionId: 'prepend-pos', getKey: positionalGetKey, overscan: 2, externalScrollerRef: scrollerRef,
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
  function mountScrolledUp(initial: Item[] = mkItems(30), H: typeof Harness = Harness) {
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    let scrollTop = 0
    const view = rtlRender(<H items={initial} scrollerRef={scrollerRef} />)
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

  /** Mounts 30 rows and leaves the reader PINNED to the bottom — the primary
   *  reading mode, where an append must follow the new message down rather than
   *  hold the old position. `scrollHeight` is derived from the live children
   *  here (not the fixed constant) so growing the transcript really does move
   *  the bottom, which is what the follow pin is asserted against. */
  function mountAtBottom(initial: Item[] = mkItems(30)) {
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    let scrollTop = 0
    const view = rtlRender(<Harness items={initial} scrollerRef={scrollerRef} />)
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
    installFakeLayout(el, CLIENT)
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

  it('shows no blank band when a prepend retires EVERY visible key (no anchor to bind)', () => {
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp()

    const visible = visibleByIndex(el)
    expect(visible.length).toBeGreaterThan(0)
    const scrollBefore = readScrollTop()

    // A prepend whose commit ALSO retires every previously-visible key (a
    // wholesale refresh regrouping the transcript). No anchor survives, so the
    // capture stands down entirely: no stage is set, part 1 never shifts the
    // window, and the reading position is (acceptably) lost — but the window
    // must still resolve to a range that covers the viewport, not strand it in
    // spacer. Pins the deliberate no-anchor design so a future change to the
    // capture cannot introduce a shift-without-correction path unnoticed.
    act(() => {
      view.rerender(
        <Harness items={[...mkItems(10, 'p'), ...mkItems(30, 'r')]} scrollerRef={scrollerRef} />,
      )
    })

    // Not vacuous: the old keys are genuinely gone (the anchor had nothing to
    // bind to) and no correction moved the viewport.
    for (const v of visible) expect(screenTopOf(el, v.key)).toBeNull()
    expect(readScrollTop()).toBe(scrollBefore)

    // No blank band: a mounted row still covers the viewport top.
    const after = visibleByIndex(el)
    expect(after.length).toBeGreaterThan(0)
    expect(after[0].top).toBeLessThanOrEqual(1)
  })

  it('holds the reading position across a prepend when getKey is INDEX-ADDRESSED (ChatPage shape)', () => {
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp(mkItems(30), PositionalHarness)

    const before = topVisible(el)
    expect(before).not.toBeNull()
    const beforeTop = readScrollTop()

    // The capture resolves the PREVIOUS render's items at their OLD indices.
    // A positional getKey answers that correctly only through the snapshot
    // taken with those items — resolving them through the CURRENT render's
    // closure returns the new list's key at the old index (a row 10 positions
    // earlier), misnaming the anchor: the correction then either no-ops or
    // yanks the viewport to the wrong row.
    act(() => {
      view.rerender(
        <PositionalHarness items={[...mkItems(10, 'p'), ...mkItems(30)]} scrollerRef={scrollerRef} />,
      )
    })

    const afterTop = screenTopOf(el, before!.key)
    expect(afterTop).not.toBeNull()
    expect(Math.abs(afterTop! - before!.top)).toBeLessThanOrEqual(1)
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
  })

  it('holds the reading position when a message is APPENDED at the tail', () => {
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp()

    const before = topVisible(el)
    expect(before).not.toBeNull()
    const beforeTop = readScrollTop()

    // Nothing is inserted ABOVE the reader here, which is why this case looks
    // like it should need no correction. It does: growing the list re-syncs the
    // offset tree, and every row that has never been measured is re-priced from
    // the running mean of the measured ones — so the height credited above the
    // reader changes and the transcript slides under them anyway (the row went
    // from screen offset 0 to 500 before this trigger existed).
    act(() => {
      view.rerender(
        <Harness items={[...mkItems(30), ...mkItems(10, 'z')]} scrollerRef={scrollerRef} />,
      )
    })

    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
    // Held the same way the prepend trigger holds it: by moving the viewport
    // down over the re-priced content, not by touching the estimator.
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
  })

  it('holds the reading position when a SINGLE streaming message is appended', () => {
    const { el, view, scrollerRef } = mountScrolledUp()

    const before = topVisible(el)
    expect(before).not.toBeNull()

    // The shape a streaming agent actually produces: one row at a time.
    act(() => {
      view.rerender(
        <Harness items={[...mkItems(30), { id: 'z0' }]} scrollerRef={scrollerRef} />,
      )
    })

    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
  })

  it('still follows an append to the bottom while the reader is PINNED', () => {
    const { el, view, scrollerRef, readScrollTop } = mountAtBottom()

    const beforeTop = readScrollTop()

    // Stick is armed, so the append trigger must stand down: following the new
    // message is the primary reading mode, and holding position here would be
    // the regression.
    act(() => {
      view.rerender(
        <Harness items={[...mkItems(30), { id: 'z0' }]} scrollerRef={scrollerRef} />,
      )
    })
    act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })

    // Followed down and landed exactly on the new bottom...
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
    expect(readScrollTop()).toBe(el.scrollHeight - CLIENT)
    // ...so the appended message is what the reader is looking at.
    const appended = screenTopOf(el, 'z0')
    expect(appended).not.toBeNull()
    expect(appended!).toBeGreaterThanOrEqual(0)
    expect(appended!).toBeLessThan(CLIENT)
  })

  // ---- Reader-row immobility: ONE invariant, every compensation trigger ----
  //
  // A prepend, an upward window shift and a tail append are three ways the
  // height credited above the reader grows. The user-visible contract is
  // identical for all of them: the row being read does not move. These cases
  // pin that contract per TRIGGER, independent of which internal slot carries
  // the anchor, so collapsing the capture paths cannot silently drop one of
  // them.

  it('INVARIANT holds the reader row across the PREPEND trigger', () => {
    const { el, view, scrollerRef } = mountScrolledUp()

    const before = topVisible(el)
    expect(before).not.toBeNull()

    act(() => {
      view.rerender(
        <Harness items={[...mkItems(10, 'p'), ...mkItems(30)]} scrollerRef={scrollerRef} />,
      )
    })

    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
  })

  /** Lowest mounted virtual index — proves an upward shift actually happened,
   *  so the invariant case cannot pass vacuously on a window that never moved. */
  function lowestMountedIndex(el: HTMLElement): number {
    let min = Number.POSITIVE_INFINITY
    el.querySelectorAll('[data-index]').forEach((n) => {
      min = Math.min(min, Number((n as HTMLElement).getAttribute('data-index')))
    })
    return min
  }

  it('INVARIANT holds the reader row across the upward WINDOW-SHIFT trigger', () => {
    const { el } = mountScrolledUp()

    // A reading-scroll UP, not a far jump: the window shifts up by a couple of
    // rows while the row being read stays mounted. The scroll is the user's;
    // the shift it provokes is ours, so the reference position is read AFTER
    // the scroll lands and BEFORE the rAF that mounts rows above.
    const mountedBefore = lowestMountedIndex(el)
    act(() => {
      el.scrollTop = 1960
      el.dispatchEvent(new Event('scroll'))
    })
    const before = topVisible(el)
    expect(before).not.toBeNull()

    act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })

    // Not vacuous: rows really did mount above the reader.
    expect(lowestMountedIndex(el)).toBeLessThan(mountedBefore)
    // Those rows are REAL_H while the offset index had credited them the flat
    // estimate, so without compensation the reader's row is pushed down by the
    // difference. Assert the ROW, not scrollTop: holding the row IS the
    // contract, and it is held by moving the viewport under it.
    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
  })

  // ---- Mid-list SPLICE: a transient "thinking" row mounting and unmounting
  // between already-rendered output (issue #6076) ----
  //
  // Both directions grow/shrink the count while index 0 keeps its key, so
  // neither is a prepend and neither is a tail append: every index from the
  // splice point on MOVES. That is what separates them from TRIGGER 3 — the
  // mounted DOM nodes still carry the previous commit's indices, so resolving
  // them through the new `items` names the wrong row.

  /** Index `key` currently occupies in `list`. */
  function indexOf(list: Item[], key: string): number {
    return list.findIndex((it) => it.id === key)
  }

  it('holds the reading position when a row is SPLICED IN above the reader', () => {
    const base = mkItems(30)
    const { el, view, scrollerRef } = mountScrolledUp(base)

    const before = topVisible(el)
    expect(before).not.toBeNull()
    const at = indexOf(base, before!.key)
    expect(at).toBeGreaterThan(0)

    // A "thinking" placeholder appearing directly above the row being read.
    const spliced = [...base.slice(0, at), { id: 'ghost' }, ...base.slice(at)]
    act(() => { view.rerender(<Harness items={spliced} scrollerRef={scrollerRef} />) })

    // Not vacuous: the ghost really did mount between the rendered rows.
    expect(screenTopOf(el, 'ghost')).not.toBeNull()
    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
  })

  it('holds the reading position when a row is SPLICED IN and getKey is INDEX-ADDRESSED', () => {
    // The splice anchor resolves the PREVIOUS render's items at the mounted
    // nodes' PREVIOUS indices, so it prices them with the getKey captured WITH
    // them -- the same contract the prepend capture has. This render's closure
    // would read the post-splice key list at pre-splice indices and name the
    // anchor one row off, correcting the viewport by the wrong row's travel.
    const base = mkItems(30)
    const { el, view, scrollerRef } = mountScrolledUp(base, PositionalHarness)

    const before = topVisible(el)
    expect(before).not.toBeNull()
    const at = indexOf(base, before!.key)
    expect(at).toBeGreaterThan(0)

    const spliced = [...base.slice(0, at), { id: 'ghost' }, ...base.slice(at)]
    act(() => { view.rerender(<PositionalHarness items={spliced} scrollerRef={scrollerRef} />) })

    expect(screenTopOf(el, 'ghost')).not.toBeNull()
    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
  })

  it('holds the reading position when a row is REMOVED above the reader', () => {
    const base = mkItems(30)
    const { el, view, scrollerRef } = mountScrolledUp(base)

    const before = topVisible(el)
    expect(before).not.toBeNull()
    const at = indexOf(base, before!.key)
    expect(at).toBeGreaterThan(0)

    // The same ghost row unmounting: content ABOVE the reader disappears, so
    // the transcript is pulled UP under them — the symptom's other half, and
    // the case no trigger covered.
    const removed = base[at - 1].id
    const pruned = base.filter((it) => it.id !== removed)
    act(() => { view.rerender(<Harness items={pruned} scrollerRef={scrollerRef} />) })

    // Not vacuous: the row above the reader is genuinely gone.
    expect(screenTopOf(el, removed)).toBeNull()
    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
  })

  it('still follows to the bottom when a row is SPLICED IN while PINNED', () => {
    const base = mkItems(30)
    const { el, view, scrollerRef, readScrollTop } = mountAtBottom(base)

    const beforeTop = readScrollTop()
    const spliced = [...base.slice(0, 10), { id: 'ghost' }, ...base.slice(10)]
    act(() => { view.rerender(<Harness items={spliced} scrollerRef={scrollerRef} />) })
    act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })

    // Holding position here would be the regression: a pinned reader follows
    // the output down, mid-list splice or not.
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
    expect(readScrollTop()).toBe(el.scrollHeight - CLIENT)
  })

  it('keeps following after a row is REMOVED while PINNED', () => {
    const base = mkItems(30)
    const { el, view, scrollerRef } = mountAtBottom(base)

    const pruned = base.filter((it) => it.id !== 'm10')
    act(() => { view.rerender(<Harness items={pruned} scrollerRef={scrollerRef} />) })
    act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })

    // The removal must not steal stick: the next streamed message still lands
    // at the bottom.
    act(() => {
      view.rerender(<Harness items={[...pruned, { id: 'z0' }]} scrollerRef={scrollerRef} />)
    })
    act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })

    expect(el.scrollTop).toBe(el.scrollHeight - CLIENT)
    const appended = screenTopOf(el, 'z0')
    expect(appended).not.toBeNull()
    expect(appended!).toBeGreaterThanOrEqual(0)
    expect(appended!).toBeLessThan(CLIENT)
  })

})
