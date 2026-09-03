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

/** Per-key rendered heights, keyed by the row's virtual key. Empty for every
 *  case but the equal-count SWAP, whose replacement row has to render TALLER
 *  than the row it replaces: an equal-height swap moves nothing on screen and
 *  would pass with no fix at all. Reset in beforeEach. */
let rowHeightByKey: Record<string, number> = {}

/** Rendered height of one row node — its override when it has one, else the
 *  flat REAL_H every other case in this file uses. */
function rowHeightOf(node: HTMLElement): number {
  const key = node.getAttribute('data-key')
  const override = key !== null ? rowHeightByKey[key] : undefined
  return override ?? REAL_H
}

function rect(top: number, height: number): DOMRect {
  return {
    top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: top,
    toJSON() { return {} },
  } as DOMRect
}

function Harness({ items, scrollerRef, estimatedHeight }: {
  items: Item[]
  scrollerRef: RefObject<HTMLDivElement | null>
  estimatedHeight?: number
}) {
  const v = useVirtualChat<Item>({
    items, sessionId: 'prepend', getKey, overscan: 2, externalScrollerRef: scrollerRef, estimatedHeight,
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
function PositionalHarness({ items, scrollerRef, estimatedHeight }: {
  items: Item[]
  scrollerRef: RefObject<HTMLDivElement | null>
  estimatedHeight?: number
}) {
  const keys = items.map((it) => it.id)
  const positionalGetKey = (_it: Item, i: number) => keys[i] ?? `oob-${i}`
  const v = useVirtualChat<Item>({
    items, sessionId: 'prepend-pos', getKey: positionalGetKey, overscan: 2, externalScrollerRef: scrollerRef, estimatedHeight,
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
      if ((child as HTMLElement).getAttribute('data-index') !== null) return rowHeightOf(child as HTMLElement)
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
        return this.getAttribute('data-index') !== null ? rowHeightOf(this) : 0
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
    rowHeightByKey = {}
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
  function mountScrolledUp(initial: Item[] = mkItems(30), H: typeof Harness = Harness, estimatedHeight?: number) {
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    let scrollTop = 0
    const view = rtlRender(<H items={initial} scrollerRef={scrollerRef} estimatedHeight={estimatedHeight} />)
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
        if (node.getAttribute('data-index') !== null) return h + rowHeightOf(node)
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

  it('anchors by stable id when the ONLY visible row is renamed (phone wLost case)', () => {
    // Field counters from a real phone: wLost=2 — two landings dropped their
    // compensation entirely. The viewport there shows ONE giant turn; a
    // landing regroups it and renames its display key; with key-based anchors
    // there is no survivor to fall forward to and the capture stands down.
    // Each such landing is a full-page-height lurch, with no native anchoring
    // on iOS to soften it. getStableId — the row's TAIL identity — survives
    // the regroup, so the SAME row anchors across the landing.
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    let scrollTop = 0
    // Every render renames every display key (generation counter) — the
    // extreme regroup. Stable ids never change.
    let generation = 0
    function StableHarness({ items }: { items: Item[] }) {
      const v = useVirtualChat<Item>({
        items, sessionId: 'prepend-stable', overscan: 2, externalScrollerRef: scrollerRef,
        getKey: (it: Item) => `${it.id}-g${generation}`,
        getStableId: (it: Item) => it.id,
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
    const view = rtlRender(<StableHarness items={mkItems(30)} />)
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

    const before = topVisible(el)
    expect(before).not.toBeNull()
    const beforeScrollTop = scrollTop

    // The landing: 10 rows prepended AND every display key renamed at once.
    generation = 1
    act(() => {
      view.rerender(<StableHarness items={[...mkItems(10, 'p'), ...mkItems(30)]} />)
    })

    // The anchored CONTENT (old index i, now i + 10) held its screen position:
    // resolved through the stable id, since no display key survived.
    const node = el.querySelector(`[data-index="${before!.idx + 10}"]`) as HTMLElement | null
    expect(node).not.toBeNull()
    const after = node!.getBoundingClientRect().top - el.getBoundingClientRect().top
    expect(Math.abs(after - before!.top)).toBeLessThanOrEqual(1)
    expect(scrollTop).toBeGreaterThan(beforeScrollTop)
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

  it('shows no blank band when a prepend retires EVERY visible key (positional anchor)', () => {
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp()

    const visible = visibleByIndex(el)
    expect(visible.length).toBeGreaterThan(0)
    const scrollBefore = readScrollTop()

    // A prepend whose commit ALSO retires every previously-visible key (a
    // wholesale refresh re-identifying the transcript). No identity survives
    // (this harness wires no getStableId, so ids fall back to the retired
    // keys), so the anchor is re-found by POSITION (old index + nearest
    // survivor displacement, net count when none) and the correction runs
    // through the same DOM-measured part-1/part-2 path as a surviving key --
    // which is what makes it inherently immune to double-compensation
    // against native CSS scroll anchoring. Before any fallback existed the
    // reader took the full block height as a lurch (the wLost full-page
    // fall recorded on a real phone).
    act(() => {
      view.rerender(
        <Harness items={[...mkItems(10, 'p'), ...mkItems(30, 'r')]} scrollerRef={scrollerRef} />,
      )
    })

    // Not vacuous: the old keys are genuinely gone (no key-based anchor could
    // bind), yet the correction moved the viewport over the inserted content.
    // The pinned invariant is VISUAL (the positional successor holds the
    // viewport top, asserted below), not an exact scrollTop: a wholesale
    // retirement leaves every row estimate-priced, so the spacer above the
    // anchor can be off by (estimate - real) per row until rows re-measure,
    // and the DOM-measured delta absorbs that skew by design.
    for (const v of visible) expect(screenTopOf(el, v.key)).toBeNull()
    expect(readScrollTop()).toBeGreaterThan(scrollBefore)

    // No blank band: a mounted row still covers the viewport top, and it is the
    // positional successor of the row that was there.
    const after = visibleByIndex(el)
    expect(after.length).toBeGreaterThan(0)
    expect(after[0].top).toBeLessThanOrEqual(1)
    expect(after[0].idx).toBe(visible[0].idx + 10)
  })

  it("subtracts the browser's native scroll-anchoring correction from the arithmetic fallback", () => {
    // Chromium ships CSS scroll anchoring: when rows are inserted above the
    // reader, the BROWSER adjusts scrollTop at layout time, before our layout
    // effects read it. The arithmetic fallback (anchor-miss path) must write
    // only the REMAINDER -- a blind full-height write on top of the native
    // correction doubles the compensation, and the reader leaps a page
    // ('skips a long stretch', low-probability field report). jsdom has no
    // native anchoring, so the sibling tests above pin the full-height write;
    // this test simulates the native correction with a scrollTop getter that
    // self-adjusts once the prepended rows are in the DOM.
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp()
    const visible = visibleByIndex(el)
    expect(visible.length).toBeGreaterThan(0)
    const scrollBefore = readScrollTop()
    const insertedPx = 10 * 100

    // Simulated native anchoring: once the regrouped rows are in the DOM
    // (the commit's mutation -- the capture site's render-phase read still
    // sees the old keys), the first forced-layout read of scrollTop returns
    // the browser-corrected value, exactly as Chromium's scroll anchoring
    // does. Writes go through normally.
    let base = scrollBefore
    let nativeApplied = false
    Object.defineProperty(el, 'scrollTop', {
      configurable: true,
      get: () => {
        if (!nativeApplied && el.querySelector('[data-key^="r"]') !== null) {
          nativeApplied = true
          base += insertedPx
        }
        return base
      },
      set: (v: number) => { base = v },
    })

    // Same wholesale-regroup prepend as the no-blank-band test: every visible
    // key retires, so no anchor survives and the fallback path is the one that
    // runs.
    act(() => {
      view.rerender(
        <Harness items={[...mkItems(10, 'p'), ...mkItems(30, 'r')]} scrollerRef={scrollerRef} />,
      )
    })

    // Native correction + our correction must land at no more than ONE
    // inserted height above the pre-prepend position -- never two. The
    // positional anchor feeds the DOM-measured part-2 path, which measures
    // the row's real post-layout offset, so whatever native anchoring
    // already moved is inherently included rather than added again (the
    // double-compensation this test exists to forbid). The small downward
    // slack covers estimate-vs-real spacer pricing after the wholesale
    // retirement; the hard bound is the upper one. Read through the
    // instrumented getter (readScrollTop closes over the harness's own
    // variable, which this test's defineProperty replaced).
    expect(el.scrollTop).toBeLessThanOrEqual(scrollBefore + insertedPx + 1)
    expect(el.scrollTop).toBeGreaterThan(scrollBefore + insertedPx - 300)
  })

  it('holds the reading position across a prepend when getKey is INDEX-ADDRESSED (ChatPage shape)', () => {
    // estimate == REAL_H for the same reason as the case above: pin identity
    // re-finding, not estimate-vs-real pricing drift.
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp(mkItems(30), PositionalHarness, REAL_H)

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

  it('holds a mid-transcript reader across a TURN-END rebuild (tail grows, tail key changes)', () => {
    // Turn end is not a prepend: the streaming row is replaced by its server
    // copy and the refresh may add a row, so the count grows AT THE TAIL while
    // index 0 is untouched. Nothing above the reader moved, so the reader must
    // not move either — the failure mode is crediting tail growth as front
    // growth and writing the height of the FIRST N rows, which displaces a
    // mid-transcript reader DOWN by a page or more ("I was in the upper part
    // and the moment your turn ended it jumped down a long way").
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp(mkItems(30), Harness, REAL_H)

    const before = visibleByIndex(el)
    expect(before.length).toBeGreaterThan(0)
    const beforeTop = readScrollTop()

    act(() => {
      // Same 30 rows, plus one appended, plus the LAST row re-identified (the
      // streaming bubble becoming its final server row).
      const rebuilt = [...mkItems(29), { id: 'm29-final' }, { id: 'm30' }]
      view.rerender(<Harness items={rebuilt} scrollerRef={scrollerRef} estimatedHeight={REAL_H} />)
    })

    const after = visibleByIndex(el)
    expect(after.length).toBeGreaterThan(0)
    // Same row, same index (nothing was inserted in front), same screen offset.
    expect(after[0].idx).toBe(before[0].idx)
    expect(Math.abs(after[0].top - before[0].top)).toBeLessThanOrEqual(1)
    expect(readScrollTop()).toBe(beforeTop)
  })

  it('holds the reader by POSITION when a front growth retires every visible key', () => {
    // estimate == REAL_H: a wholesale retirement reprices previously-measured
    // rows back to the estimate, so with a skewed estimate the mounted-row
    // set above the anchor prices differently across the rebuild and the row
    // sits a few estimate-vs-real quanta off (trigger-7 corrects that drift
    // separately -- see the heightSyncAnchor suite). This case pins the
    // IDENTITY contract (re-found by position, same screen offset), so the
    // pricing dimension is removed rather than slack-tolerated.
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp(mkItems(30), Harness, REAL_H)

    const before = visibleByIndex(el)
    expect(before.length).toBeGreaterThan(0)
    const beforeTop = readScrollTop()

    // A wholesale transcript rebuild: hundreds of older rows materialise in
    // front AND every row the reader can see is re-identified (a streamed row
    // replaced by its server copy under a different key). No key survives for
    // the anchor to follow, so the row is re-found by where it now sits.
    const inserted = 1000
    act(() => {
      view.rerender(
        <Harness items={[...mkItems(inserted, 'p'), ...mkItems(30, 'r')]} scrollerRef={scrollerRef} estimatedHeight={REAL_H} />,
      )
    })

    const after = visibleByIndex(el)
    expect(after.length).toBeGreaterThan(0)
    // The reader is still on the tail region, at the same offset from the end —
    // not on the first page of the transcript, which is where an uncorrected
    // window (old indices, old scrollTop) lands once `inserted` rows sit in front.
    expect(after[0].idx).toBe(before[0].idx + inserted)
    expect(after[0].key).toBe(`r${before[0].idx}`)
    expect(Math.abs(after[0].top - before[0].top)).toBeLessThanOrEqual(1)
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
  })

  it('holds the reader by POSITION across a total key retirement when getKey is INDEX-ADDRESSED', () => {
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp(mkItems(30), PositionalHarness)

    const before = visibleByIndex(el)
    expect(before.length).toBeGreaterThan(0)
    const beforeTop = readScrollTop()

    // The positional anchor names a row of the NEW list, so it must be priced by
    // the CURRENT render's getKey paired with the current items. Pricing it
    // through the getKey snapshotted with the PREVIOUS items (the pairing the
    // surviving-key path correctly uses) reads the old list at an index past
    // its end, yielding a key no mounted row carries: part 1 re-bases the window
    // but part 2 has nothing to correct against, and the viewport is left in
    // spacer. An identity getKey cannot tell the two pairings apart; this one can.
    const inserted = 1000
    act(() => {
      view.rerender(
        <PositionalHarness items={[...mkItems(inserted, 'p'), ...mkItems(30, 'r')]} scrollerRef={scrollerRef} estimatedHeight={REAL_H} />,
      )
    })

    const after = visibleByIndex(el)
    expect(after.length).toBeGreaterThan(0)
    expect(after[0].idx).toBe(before[0].idx + inserted)
    expect(after[0].key).toBe(`r${before[0].idx}`)
    // No blank band and no lost correction: the same screen offset, reached by
    // a scrollTop write over the inserted content.
    expect(Math.abs(after[0].top - before[0].top)).toBeLessThanOrEqual(1)
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
  })

  it('moves the positional anchor by the nearest SURVIVOR\'s displacement, not the net count growth', () => {
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp()

    const before = visibleByIndex(el)
    expect(before.length).toBeGreaterThan(0)
    const beforeTop = readScrollTop()
    const firstVisible = before[0].idx

    // Front growth of 10 AND tail growth of 5 in one commit (a reconnect refresh
    // catching up on missed rows), with every VISIBLE row re-identified while the
    // rows above the viewport keep their keys. The reader moved by 10 — the net
    // count grew by 15. Anchoring by the net count would put them 5 rows past
    // where they were reading.
    const front = 10
    const tail = 5
    const kept = mkItems(30).slice(0, firstVisible)
    const reidentified = mkItems(30, 'r').slice(firstVisible)
    act(() => {
      view.rerender(
        <Harness
          items={[...mkItems(front, 'p'), ...kept, ...reidentified, ...mkItems(tail, 't')]}
          scrollerRef={scrollerRef}
        />,
      )
    })

    const after = visibleByIndex(el)
    expect(after.length).toBeGreaterThan(0)
    expect(after[0].idx).toBe(firstVisible + front)
    expect(after[0].key).toBe(`r${firstVisible}`)
    expect(Math.abs(after[0].top - before[0].top)).toBeLessThanOrEqual(1)
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
  })

  it('re-bases by the anchored row\'s own displacement when a surviving key is found', () => {
    const { el, view, scrollerRef } = mountScrolledUp()

    const before = topVisible(el)
    expect(before).not.toBeNull()

    // Front growth of 3 with a tail growth of 50: the visible row's key survives
    // and it moved by 3. A re-base by the net count (53) would push the mounted
    // window 50 rows past the anchor, leaving part 2 nothing to measure.
    act(() => {
      view.rerender(
        <Harness items={[...mkItems(3, 'p'), ...mkItems(30), ...mkItems(50, 't')]} scrollerRef={scrollerRef} />,
      )
    })

    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
  })

  it('holds the reader when rows ABOVE them coalesce while the tail grows (negative displacement)', () => {
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp()

    const before = topVisible(el)
    expect(before).not.toBeNull()
    const beforeTop = readScrollTop()

    // Three rows above the reader collapse into one (index 0 is renamed, the
    // reader's row moves UP by two) while ten rows append at the tail, so the
    // count still GROWS by eight. The reader's displacement is -2, not +8: a
    // re-base by the net count would carry the mounted window eight rows past a
    // row that moved the other way, and standing down would leave the reader
    // two rows' height above where they were reading.
    act(() => {
      view.rerender(
        <Harness
          items={[{ id: 'c0' }, ...mkItems(30).slice(3), ...mkItems(10, 't')]}
          scrollerRef={scrollerRef}
        />,
      )
    })

    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
    // The hold is a scrollTop write, not a coincidence of the geometry.
    expect(readScrollTop()).not.toBe(beforeTop)
    // And the correction left no blank band: a mounted row covers the top.
    const visible = visibleByIndex(el)
    expect(visible.length).toBeGreaterThan(0)
    expect(visible[0].top).toBeLessThanOrEqual(1)
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

  it('holds the reading position when a row is SWAPPED at equal count above the reader', () => {
    const base = mkItems(30)
    const { el, view, scrollerRef } = mountScrolledUp(base)

    const before = topVisible(el)
    expect(before).not.toBeNull()
    const at = indexOf(base, before!.key)
    expect(at).toBeGreaterThan(0)

    // The ordinary streaming shape: React batches the placeholder LEAVING and its
    // replacement ARRIVING into one commit, so the net count never moves. The
    // replacement renders 3x taller than the row it replaces — an equal-height
    // swap displaces nothing and would pass without any fix.
    const replaced = base[at - 1].id
    rowHeightByKey = { out0: REAL_H * 3 }
    const swapped = base.map((it, i) => (i === at - 1 ? { id: 'out0' } : it))
    act(() => { view.rerender(<Harness items={swapped} scrollerRef={scrollerRef} />) })

    // Not vacuous: the placeholder really left and the taller replacement really
    // mounted in its place, at the same index.
    expect(screenTopOf(el, replaced)).toBeNull()
    expect(screenTopOf(el, 'out0')).not.toBeNull()
    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
  })

  it('holds the reading position across an equal-count SWAP when getKey is INDEX-ADDRESSED', () => {
    // Same contract as the splice cases: the swap anchor resolves the PREVIOUS
    // render's items at the mounted nodes' PREVIOUS indices, so it must price
    // them with the getKey captured WITH them. This render's closure reads the
    // post-swap key list, which names the replacement where the anchor expects
    // the row it replaced.
    const base = mkItems(30)
    const { el, view, scrollerRef } = mountScrolledUp(base, PositionalHarness)

    const before = topVisible(el)
    expect(before).not.toBeNull()
    const at = indexOf(base, before!.key)
    expect(at).toBeGreaterThan(0)

    const replaced = base[at - 1].id
    rowHeightByKey = { out0: REAL_H * 3 }
    const swapped = base.map((it, i) => (i === at - 1 ? { id: 'out0' } : it))
    act(() => { view.rerender(<PositionalHarness items={swapped} scrollerRef={scrollerRef} />) })

    expect(screenTopOf(el, replaced)).toBeNull()
    expect(screenTopOf(el, 'out0')).not.toBeNull()
    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
  })

  it('holds the reader by POSITION when a splice retires every visible key', () => {
    const base = mkItems(30)
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp(base)

    const before = visibleByIndex(el)
    expect(before.length).toBeGreaterThan(0)
    const at = before[0].idx
    expect(at).toBeGreaterThan(1)
    const beforeTop = readScrollTop()

    // A "thinking" row mounts above the viewport in the SAME commit that
    // re-identifies every visible row (each streamed row replaced by its server
    // copy under a different key). Index 0 keeps its key, so this is the splice
    // path, not a prepend — and no visible key survives for the anchor to
    // follow, so the row must be re-found by where it now sits.
    const rekeyed = base.map((it, i) =>
      i >= at && i < at + before.length ? { id: `r${i}` } : it,
    )
    const spliced = [...rekeyed.slice(0, at - 1), { id: 'ghost' }, ...rekeyed.slice(at - 1)]
    act(() => { view.rerender(<Harness items={spliced} scrollerRef={scrollerRef} />) })

    // Not vacuous: every previously visible key is genuinely gone, and the
    // ghost really mounted above the reader.
    for (const v of before) expect(screenTopOf(el, v.key)).toBeNull()
    expect(screenTopOf(el, 'ghost')).not.toBeNull()

    // The topmost visible row is the positional successor of the row that was
    // there, held at the same screen offset by a scrollTop write over the
    // inserted row — without the positional anchor it moves down by the
    // inserted row's height.
    const after = visibleByIndex(el)
    expect(after.length).toBeGreaterThan(0)
    expect(after[0].idx).toBe(at + 1)
    expect(after[0].key).toBe(`r${at}`)
    expect(Math.abs(after[0].top - before[0].top)).toBeLessThanOrEqual(1)
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
  })

  it('holds the reader by POSITION across a splice-with-total-retirement when getKey is INDEX-ADDRESSED', () => {
    // The positional anchor names a row of the NEW list, so it must be priced
    // by the CURRENT render's getKey paired with the current items — the same
    // pairing contract PR 8001 pinned for the prepend fallback. An identity
    // getKey cannot tell the two pairings apart; this one can.
    const base = mkItems(30)
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp(base, PositionalHarness)

    const before = visibleByIndex(el)
    expect(before.length).toBeGreaterThan(0)
    const at = before[0].idx
    expect(at).toBeGreaterThan(1)
    const beforeTop = readScrollTop()

    const rekeyed = base.map((it, i) =>
      i >= at && i < at + before.length ? { id: `r${i}` } : it,
    )
    const spliced = [...rekeyed.slice(0, at - 1), { id: 'ghost' }, ...rekeyed.slice(at - 1)]
    act(() => { view.rerender(<PositionalHarness items={spliced} scrollerRef={scrollerRef} />) })

    for (const v of before) expect(screenTopOf(el, v.key)).toBeNull()
    expect(screenTopOf(el, 'ghost')).not.toBeNull()

    const after = visibleByIndex(el)
    expect(after.length).toBeGreaterThan(0)
    expect(after[0].idx).toBe(at + 1)
    expect(after[0].key).toBe(`r${at}`)
    expect(Math.abs(after[0].top - before[0].top)).toBeLessThanOrEqual(1)
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
  })

  it('does not anchor the inserted row when the splice lands exactly at the viewport top', () => {
    const base = mkItems(30)
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp(base)

    const before = visibleByIndex(el)
    expect(before.length).toBeGreaterThan(0)
    const at = before[0].idx
    expect(at).toBeGreaterThan(1)
    const beforeTop = readScrollTop()

    // The ghost mounts AT the topmost visible row's own index while every
    // visible key retires. Rows above the viewport keep their keys but did NOT
    // move (they sit above the splice): borrowing their zero displacement
    // would resolve the anchor to the ghost itself and hold the wrong row.
    // The change boundary excludes them, so the displacement comes from the
    // survivors below (+1).
    const rekeyed = base.map((it, i) =>
      i >= at && i < at + before.length ? { id: `r${i}` } : it,
    )
    const spliced = [...rekeyed.slice(0, at), { id: 'ghost' }, ...rekeyed.slice(at)]
    act(() => { view.rerender(<Harness items={spliced} scrollerRef={scrollerRef} />) })

    // Not vacuous: every previously visible key is genuinely gone, and the
    // ghost really mounted.
    for (const v of before) expect(screenTopOf(el, v.key)).toBeNull()
    expect(screenTopOf(el, 'ghost')).not.toBeNull()

    // The reader's row (its re-keyed copy, now one index down) is held at its
    // screen offset; anchoring the ghost instead would leave it one row lower
    // with no scrollTop write.
    const held = screenTopOf(el, `r${at}`)
    expect(held).not.toBeNull()
    expect(Math.abs(held! - before[0].top)).toBeLessThanOrEqual(1)
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
  })

  it('does not anchor the inserted row at the viewport top when getKey is INDEX-ADDRESSED', () => {
    const base = mkItems(30)
    const { el, view, scrollerRef, readScrollTop } = mountScrolledUp(base, PositionalHarness)

    const before = visibleByIndex(el)
    expect(before.length).toBeGreaterThan(0)
    const at = before[0].idx
    expect(at).toBeGreaterThan(1)
    const beforeTop = readScrollTop()

    const rekeyed = base.map((it, i) =>
      i >= at && i < at + before.length ? { id: `r${i}` } : it,
    )
    const spliced = [...rekeyed.slice(0, at), { id: 'ghost' }, ...rekeyed.slice(at)]
    act(() => { view.rerender(<PositionalHarness items={spliced} scrollerRef={scrollerRef} />) })

    for (const v of before) expect(screenTopOf(el, v.key)).toBeNull()
    expect(screenTopOf(el, 'ghost')).not.toBeNull()

    const held = screenTopOf(el, `r${at}`)
    expect(held).not.toBeNull()
    expect(Math.abs(held! - before[0].top)).toBeLessThanOrEqual(1)
    expect(readScrollTop()).toBeGreaterThan(beforeTop)
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

  it('does not hold position for a PINNED reader across an equal-count SWAP', () => {
    const base = mkItems(30)
    const { el, view, scrollerRef, readScrollTop } = mountAtBottom(base)

    const beforeTop = readScrollTop()
    rowHeightByKey = { out0: REAL_H * 3 }
    const swapped = base.map((it, i) => (i === 10 ? { id: 'out0' } : it))
    act(() => { view.rerender(<Harness items={swapped} scrollerRef={scrollerRef} />) })
    act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })

    // The swap capture is gated on stick, so a pinned reader must never be pulled
    // BACK UP to where a row used to sit. (Following the taller replacement down
    // is the ResizeObserver's job, which this harness does not provide — the
    // assertion here is only that the anchor correction stays out of it.)
    expect(readScrollTop()).toBeGreaterThanOrEqual(beforeTop)

    // And stick survives: the next streamed message still lands at the bottom.
    act(() => {
      view.rerender(<Harness items={[...swapped, { id: 'z1' }]} scrollerRef={scrollerRef} />)
    })
    act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })
    expect(el.scrollTop).toBe(el.scrollHeight - CLIENT)
    const appended = screenTopOf(el, 'z1')
    expect(appended).not.toBeNull()
    expect(appended!).toBeGreaterThanOrEqual(0)
    expect(appended!).toBeLessThan(CLIENT)
  })

})
