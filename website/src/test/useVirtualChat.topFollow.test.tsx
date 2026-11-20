/**
 * Feature: chat-virtualizer — `followEdge: 'top'`, the newest-first transcript.
 *
 * The newest turn is row 0, so the live edge is scrollTop 0 and history loads
 * at the END. Follow keeps its meaning ("the reader is at the live edge") with
 * the geometry flipped: a reader at the top sees new turns land at the head; a
 * reader who scrolled down into history is held still while rows arrive above
 * them; older pages land below and must never pin anyone to the bottom.
 *
 * jsdom has no layout, so this installs the deterministic layout engine the
 * prepend suite uses: getBoundingClientRect walks the scroller's children
 * summing heights minus scrollTop, and scrollHeight is the children's sum. The
 * rows genuinely move, so a hold is asserted as the reader's row keeping its
 * screen position rather than against source text.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render as rtlRender, act } from '@testing-library/react'
import { type RefObject } from 'react'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatReturn } from '../hooks/virtualizer/types'
import TranscriptScrollShell from '../pages/chat/TranscriptScrollShell'

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number, prefix = 'm'): Item[] =>
  Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i}` }))

const REAL_H = 100
const CLIENT = 400

let rowHeightByKey: Record<string, number> = {}
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

interface HarnessProps {
  items: Item[]
  scrollerRef: RefObject<HTMLDivElement | null>
  apiRef: { current: UseVirtualChatReturn<Item> | null }
  sessionId?: string
  heightScopeKey?: string
  followEdge?: 'top' | 'bottom'
  overscan?: number
  onTopReached?: () => void
  prefetchStartIndex?: number
  streamingIndex?: number
  /** Render the leading-chrome wrapper TranscriptScrollShell puts before the
   *  rows in top-down (the footer, preparing caption and survey). Its height
   *  is set on the node by the test. */
  leading?: boolean
  /** Render the real top-down TranscriptScrollShell around the rows, with
   *  this dock clearance as its top padding (the host's `scrollerStyle`). */
  shellPaddingTop?: number
}

function Harness({
  items,
  scrollerRef,
  apiRef,
  sessionId = 'top-follow',
  heightScopeKey,
  followEdge = 'top',
  overscan = 2,
  onTopReached,
  prefetchStartIndex,
  streamingIndex,
  leading,
  shellPaddingTop,
}: HarnessProps) {
  const v = useVirtualChat<Item>({
    items, sessionId, heightScopeKey, getKey, overscan, externalScrollerRef: scrollerRef,
    followEdge, onTopReached, prefetchStartIndex, streamingIndex,
  })
  apiRef.current = v
  const rows = v.virtualItems.map((it) => (
    <div key={it.key} data-index={it.index} data-key={it.key} ref={v.measureRef(it.index)} />
  ))
  if (shellPaddingTop !== undefined) {
    return (
      <TranscriptScrollShell
        scrollerRef={scrollerRef}
        onScroll={() => {}}
        virt={v}
        loadingOlder={false}
        liveEdgeAtTop
        headerSpacer={false}
        scrollerStyle={{ paddingTop: shellPaddingTop }}
      >
        {rows}
      </TranscriptScrollShell>
    )
  }
  return (
    <div ref={scrollerRef as RefObject<HTMLDivElement>} data-scroller>
      {leading && <div ref={v.leadingRef} data-leading="" />}
      <div ref={v.topSentinelRef} data-sentinel="top" />
      <div data-spacer="before" style={{ height: v.offsetBefore }} />
      {rows}
      <div data-spacer="after" style={{ height: v.offsetAfter }} />
      <div ref={v.bottomSentinelRef} data-sentinel="bottom" />
    </div>
  )
}

function screenTopOf(el: HTMLElement, key: string): number | null {
  const node = el.querySelector(`[data-key="${key}"]`) as HTMLElement | null
  if (!node) return null
  return node.getBoundingClientRect().top - el.getBoundingClientRect().top
}

/** First row whose bottom edge is below the viewport top. */
function topVisible(el: HTMLElement): { key: string; top: number } | null {
  let best: { key: string; idx: number; top: number } | null = null
  el.querySelectorAll('[data-key]').forEach((node) => {
    const r = (node as HTMLElement).getBoundingClientRect()
    if (r.bottom <= 0) return
    const idx = Number((node as HTMLElement).getAttribute('data-index'))
    if (!best || idx < best.idx) best = { key: (node as HTMLElement).getAttribute('data-key')!, idx, top: r.top }
  })
  return best
}

describe('useVirtualChat: followEdge top', () => {
  let restore: (() => void) | null = null
  let origRaf: typeof requestAnimationFrame
  let origIO: typeof IntersectionObserver
  let origRO: typeof ResizeObserver | undefined
  let frames: FrameRequestCallback[] = []
  let ios: { cb: IntersectionObserverCallback; targets: Element[] }[] = []
  let fireRO: ((targets: Element[]) => void) | undefined
  let roObserved: Set<Element> = new Set()

  function flush() {
    // Frames can schedule frames; bounded so a loop cannot hang the test.
    for (let i = 0; i < 10 && frames.length; i++) {
      const batch = frames
      frames = []
      act(() => { batch.forEach((cb) => cb(0)) })
    }
  }

  /** The scroller's inline top padding (the top-down shell's dock clearance):
   *  the first child starts below it and it counts in scrollHeight. */
  const paddingTopOf = (scroller: HTMLElement): number => parseFloat(scroller.style.paddingTop || '0') || 0

  function installFakeLayout(scroller: HTMLElement) {
    const proto = HTMLElement.prototype
    const origRect = proto.getBoundingClientRect
    const origOffsetH = Object.getOwnPropertyDescriptor(proto, 'offsetHeight')
    const childHeight = (child: Element): number => {
      if ((child as HTMLElement).getAttribute('data-index') !== null) return rowHeightOf(child as HTMLElement)
      const h = (child as HTMLElement).style?.height
      return h ? parseFloat(h) : 0
    }
    proto.getBoundingClientRect = function (this: HTMLElement): DOMRect {
      if (this === scroller) return rect(0, CLIENT)
      if (this.parentElement === scroller) {
        let y = paddingTopOf(scroller)
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

  beforeEach(() => {
    localStorage.clear()
    frames = []
    ios = []
    rowHeightByKey = {}
    roObserved = new Set()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      frames.push(cb)
      return frames.length
    }) as typeof requestAnimationFrame
    class FakeIO {
      targets: Element[] = []
      constructor(readonly cb: IntersectionObserverCallback) { ios.push(this) }
      observe(t: Element) { this.targets.push(t) }
      unobserve() {}
      disconnect() {}
      takeRecords() { return [] }
      root: Element | null = null
      rootMargin = ''
      thresholds: number[] = []
    }
    origIO = globalThis.IntersectionObserver
    globalThis.IntersectionObserver = FakeIO as unknown as typeof IntersectionObserver
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(cb: ResizeObserverCallback) {
        fireRO = (targets) => cb(targets.map((target) => ({ target })) as unknown as ResizeObserverEntry[], this as unknown as ResizeObserver)
      }
      observe(t: Element) { roObserved.add(t) }
      unobserve(t: Element) { roObserved.delete(t) }
      disconnect() {}
    } as unknown as typeof ResizeObserver
  })

  afterEach(() => {
    vi.useRealTimers()
    restore?.()
    restore = null
    globalThis.requestAnimationFrame = origRaf
    globalThis.IntersectionObserver = origIO
    if (origRO) globalThis.ResizeObserver = origRO
    fireRO = undefined
  })

  function mount(initial: Item[] = mkItems(30), extra: Partial<HarnessProps> = {}) {
    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    const apiRef: { current: UseVirtualChatReturn<Item> | null } = { current: null }
    let scrollTop = 0
    const writes: number[] = []
    let props: HarnessProps = { items: initial, scrollerRef, apiRef, ...extra }
    const view = rtlRender(<Harness {...props} />)
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
      }, paddingTopOf(el)),
    })
    ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => {
      writes.push(o.top)
      scrollTop = o.top
    }
    installFakeLayout(el)
    flush()
    const rerender = (next: Partial<HarnessProps>) => {
      props = { ...props, ...next }
      act(() => { view.rerender(<Harness {...props} />) })
    }
    /** A reader's scroll: move, dispatch, let the window recompute. */
    const scrollTo = (top: number) => {
      act(() => { scrollTop = top; el.dispatchEvent(new Event('scroll')) })
      flush()
    }
    return { el, api: () => apiRef.current!, rerender, scrollTo, readScrollTop: () => scrollTop, writes }
  }

  it('opens at the head: scrollTop 0, head rows mounted, at the live edge and following', () => {
    const { el, api, readScrollTop } = mount()
    expect(readScrollTop()).toBe(0)
    expect(screenTopOf(el, 'm0')).toBe(0)
    expect(el.querySelector('[data-key="m29"]')).toBeNull()
    expect(api().isAtBottom).toBe(true)
    expect(api().getFollow()).toBe(true)
  })

  it('opens reversed rows against a fresh top-down host height scope before timers run', () => {
    vi.useFakeTimers()
    const chronological = mkItems(30)
    rowHeightByKey = Object.fromEntries(chronological.map((item, index) => [item.id, index < 10 ? 400 : 40]))
    const bottomScope = 'chat-1-1:tables1:pane:w800'
    const topDownScope = 'chat-1-1:tables1:pane:topdown:w800'
    const { el, api, rerender } = mount(chronological, {
      sessionId: 'pane:chat-1-1',
      heightScopeKey: bottomScope,
      followEdge: 'bottom',
      overscan: 30,
    })

    act(() => { fireRO?.(Array.from(roObserved)) })
    flush()
    act(() => { vi.runOnlyPendingTimers() })
    flush()
    expect(api().totalHeight).toBe(4800)

    vi.clearAllTimers()
    frames = []
    rerender({
      items: [...chronological].reverse(),
      sessionId: 'pane:chat-1-1:topdown',
      followEdge: 'top',
      heightScopeKey: topDownScope,
      overscan: 6,
    })

    const mounted = Array.from(el.querySelectorAll<HTMLElement>('[data-index]'))
    expect(mounted[0]?.getAttribute('data-index')).toBe('0')
    expect(parseFloat((el.querySelector('[data-spacer="before"]') as HTMLElement).style.height || '0')).toBe(0)
    expect(mounted.reduce((height, row) => height + rowHeightOf(row), 0)).toBeGreaterThanOrEqual(CLIENT)
  })

  it('at the top, a new turn inserted at index 0 keeps scrollTop 0 and is shown at the top', () => {
    const { el, rerender, readScrollTop } = mount()
    rerender({ items: [...mkItems(2, 'n'), ...mkItems(30)] })
    flush()
    expect(readScrollTop()).toBe(0)
    expect(screenTopOf(el, 'n0')).toBe(0)
    expect(screenTopOf(el, 'm0')).toBe(2 * REAL_H)
  })

  it('a follower inside the re-engage band is written to exactly 0 when a turn lands at the head', () => {
    // Above 0 native anchoring would carry the reader past the new rows, so
    // the head insert must re-write 0 rather than rely on the position.
    const { el, api, rerender, scrollTo, readScrollTop } = mount()
    scrollTo(300)
    scrollTo(10)
    expect(api().getFollow()).toBe(true)
    rerender({ items: [...mkItems(2, 'n'), ...mkItems(30)] })
    flush()
    expect(readScrollTop()).toBe(0)
    expect(screenTopOf(el, 'n0')).toBe(0)
  })

  it('does not cancel a downward wheel scroll whose scroll event is still pending when a turn lands', () => {
    // The wheel opens a SCROLL_SETTLE_MS gesture window read from
    // performance.now(). Freeze that clock so the rerender lands inside the
    // window on a slow machine too, not only when it finishes within 150 ms.
    vi.useFakeTimers({ toFake: ['performance'] })
    const { el, rerender, readScrollTop, writes } = mount()
    act(() => {
      el.dispatchEvent(new WheelEvent('wheel', { deltaY: 80, deltaMode: 0 }))
      // Browsers update scrollTop before they dispatch the matching scroll event.
      el.scrollTop = 8
    })
    writes.length = 0
    rerender({ items: [...mkItems(2, 'n'), ...mkItems(30)] })
    flush()
    expect(readScrollTop()).toBe(8)
    expect(writes).toEqual([])
  })

  it('a reader scrolled down into history is held still when a new turn lands at the head', () => {
    const { el, rerender, scrollTo, readScrollTop } = mount()
    scrollTo(1250)
    const before = topVisible(el)
    expect(before).not.toBeNull()
    rerender({ items: [...mkItems(2, 'n'), ...mkItems(30)] })
    flush()
    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
    expect(readScrollTop()).toBeGreaterThan(1250)
  })

  it('streaming growth of row 0 above a scrolled-away reader keeps their visible row in place', () => {
    // Row 0 is the streaming turn and sits above the fold: every token it
    // grows by lands above the reader, the new regime this orientation brings.
    const { el, rerender, scrollTo, readScrollTop } = mount(mkItems(30), { streamingIndex: 0 })
    expect(readScrollTop()).toBe(0)
    scrollTo(250)
    rerender({})
    const before = topVisible(el)
    expect(before).not.toBeNull()
    expect(before!.key).not.toBe('m0')
    const row0 = el.querySelector('[data-key="m0"]')!
    for (const grow of [27, 54, 27]) {
      rowHeightByKey.m0 = (rowHeightByKey.m0 ?? REAL_H) + grow
      act(() => { fireRO?.([row0]) })
      flush()
    }
    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
    expect(readScrollTop()).toBe(250 + 108)
  })

  describe('leading chrome (the footer, caption and survey above the rows)', () => {
    // In top-down the shell renders the host's live chrome BEFORE the rows, in
    // the wrapper it hands to `leadingRef`. When a turn starts, ends or goes
    // quiet that chrome changes height above a reader in history. WebKit has
    // no scroll anchoring, so the measured residual is the only holder.
    function setup() {
      const view = mount(mkItems(30), { leading: true })
      const leading = view.el.querySelector('[data-leading]') as HTMLElement
      return { ...view, leading }
    }

    it('the resize observer watches the leading wrapper', () => {
      const { leading } = setup()
      expect(roObserved.has(leading)).toBe(true)
    })

    it('holds a released reader on their row when the chrome grows 40px and nothing else moved them', () => {
      const { el, api, scrollTo, readScrollTop, leading } = setup()
      scrollTo(1250)
      expect(api().getFollow()).toBe(false)
      const before = topVisible(el)
      expect(before).not.toBeNull()
      leading.style.height = '40px'
      // The rows moved down with it: what the reader read is 40px lower.
      expect(screenTopOf(el, before!.key)).toBe(before!.top + 40)
      act(() => { fireRO?.([leading]) })
      flush()
      expect(readScrollTop()).toBe(1250 + 40)
      expect(screenTopOf(el, before!.key)).toBe(before!.top)
    })

    it('writes nothing when native anchoring already held the row', () => {
      const { el, api, scrollTo, readScrollTop, writes, leading } = setup()
      scrollTo(1250)
      expect(api().getFollow()).toBe(false)
      const before = topVisible(el)
      writes.length = 0
      leading.style.height = '40px'
      // The engine adjusted scrollTop in layout; its scroll event is still
      // pending when the observer fires.
      el.scrollTop = 1290
      act(() => { fireRO?.([leading]) })
      flush()
      expect(writes).toEqual([])
      expect(readScrollTop()).toBe(1290)
      expect(screenTopOf(el, before!.key)).toBe(before!.top)
    })

    it('leaves a follower at scrollTop 0: the chrome grows below the live edge', () => {
      const { api, readScrollTop, writes, leading } = setup()
      expect(api().getFollow()).toBe(true)
      writes.length = 0
      leading.style.height = '40px'
      act(() => { fireRO?.([leading]) })
      flush()
      expect(writes.every((w) => w === 0)).toBe(true)
      expect(readScrollTop()).toBe(0)
    })
  })

  it('scrolling down releases follow; scrolling back to within 16px of the top re-engages it', () => {
    const { api, scrollTo } = mount()
    scrollTo(300)
    expect(api().getFollow()).toBe(false)
    expect(api().isAtBottom).toBe(false)
    scrollTo(40)
    expect(api().getFollow()).toBe(false)
    scrollTo(12)
    expect(api().getFollow()).toBe(true)
    expect(api().isAtBottom).toBe(true)
  })

  it('a dock change under a reader near the top leaves follow as it was', () => {
    // The real top-down shell: a dock change moves its top padding, and the
    // shell shifts scrollTop by the same amount so the reader's content stays
    // still. That shift is the virtualizer's own write. Its scroll event must
    // not arm follow for a released reader it lands inside the 16px band, or
    // release a follower it carries away from 0.
    // The follower half needs the dock change inside a wheel's settle window,
    // where the leading-chrome pin stands down. Freeze the clock so a slow
    // machine stays inside it too.
    vi.useFakeTimers({ toFake: ['performance'] })
    const { el, api, rerender, scrollTo, readScrollTop, writes } = mount(mkItems(30), { shellPaddingTop: 200 })
    expect(el.style.paddingTop).toBe('200px')
    scrollTo(300)
    scrollTo(36)
    expect(api().getFollow()).toBe(false)
    // The composer loses a line: the dock clearance shrinks by 24px.
    rerender({ shellPaddingTop: 176 })
    expect(readScrollTop()).toBe(12)
    act(() => { el.dispatchEvent(new Event('scroll')) })
    flush()
    expect(api().getFollow()).toBe(false)
    // A turn lands at the head: the released reader is held on their row,
    // not pinned to 0.
    rerender({ items: [...mkItems(2, 'n'), ...mkItems(30)] })
    flush()
    expect(readScrollTop()).toBeGreaterThan(12)
    // A reader wheels back into the band and follows again. The composer
    // gains a line before the wheel's settle window ends.
    scrollTo(300)
    act(() => { el.dispatchEvent(new WheelEvent('wheel', { deltaY: -290, deltaMode: 0 })) })
    scrollTo(10)
    expect(api().getFollow()).toBe(true)
    writes.length = 0
    rerender({ shellPaddingTop: 200 })
    expect(readScrollTop()).toBe(34)
    expect(writes).toEqual([34])
    act(() => { el.dispatchEvent(new Event('scroll')) })
    flush()
    expect(api().getFollow()).toBe(true)
    // Still a follower: the next turn lands in view, at scrollTop 0.
    act(() => { vi.advanceTimersByTime(1000) })
    rerender({ items: [...mkItems(3, 'p'), ...mkItems(2, 'n'), ...mkItems(30)] })
    flush()
    expect(readScrollTop()).toBe(0)
  })

  it('the history callback fires from the BOTTOM sentinel, never the top one', () => {
    let fired = 0
    const { el } = mount(mkItems(30), { onTopReached: () => { fired++ } })
    const top = el.querySelector('[data-sentinel="top"]')!
    const bottom = el.querySelector('[data-sentinel="bottom"]')!
    const ioFor = (t: Element) => ios.find((o) => o.targets.includes(t))!
    act(() => { ioFor(top).cb([{ isIntersecting: true, target: top } as unknown as IntersectionObserverEntry], {} as IntersectionObserver) })
    expect(fired).toBe(0)
    act(() => { ioFor(bottom).cb([{ isIntersecting: true, target: bottom } as unknown as IntersectionObserverEntry], {} as IntersectionObserver) })
    expect(fired).toBe(1)
  })

  it('the history prefetch fires when the window END crosses down past prefetchStartIndex', () => {
    let fired = 0
    const { api, scrollTo } = mount(mkItems(30), { onTopReached: () => { fired++ }, prefetchStartIndex: 24 })
    scrollTo(800)
    expect(fired).toBe(0)
    scrollTo(2200)
    expect(fired).toBe(1)
    expect(api().getFollow()).toBe(false)
  })

  it('an older page appended at the END pins nobody: neither a reader in history nor one at the top', () => {
    const atTop = mount()
    atTop.writes.length = 0
    atTop.rerender({ items: [...mkItems(30), ...mkItems(40, 'old')] })
    flush()
    expect(atTop.readScrollTop()).toBe(0)
    expect(atTop.writes.every((w) => w === 0)).toBe(true)
  })

  it('an older page appended at the END leaves a reader in history exactly where they are', () => {
    // scrollTop itself may move: appending re-prices the unmeasured rows above
    // the reader from the new running mean (the tail-append trigger compensates
    // that). What must not move is the row they are reading.
    const { el, api, rerender, scrollTo, readScrollTop } = mount()
    expect(readScrollTop()).toBe(0)
    scrollTo(1250)
    expect(api().getFollow()).toBe(false)
    const before = topVisible(el)
    rerender({ items: [...mkItems(30), ...mkItems(40, 'old')] })
    flush()
    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
  })

  it('an older page that splits the oldest turn holds a reader near the bottom on their row', () => {
    // The page boundary had cut the oldest turn: its loaded rows are the ones
    // from the reader's row to the end. The landing inserts that turn's
    // EARLIER rows (c0..c2) in front of them, in the middle of the list right
    // where the reader is, and appends the older turns (old0..old9) at the end.
    const { el, api, rerender, scrollTo } = mount()
    scrollTo(el.scrollHeight - CLIENT - 50)
    expect(api().getFollow()).toBe(false)
    const before = topVisible(el)
    expect(before).not.toBeNull()
    const k = Number(before!.key.slice(1))
    expect(k).toBeGreaterThan(20)
    const topBefore = el.scrollTop
    rerender({
      items: [...mkItems(k), ...mkItems(3, 'c'), ...mkItems(30).slice(k), ...mkItems(10, 'old')],
    })
    flush()
    const after = screenTopOf(el, before!.key)
    expect(after).not.toBeNull()
    expect(Math.abs(after! - before!.top)).toBeLessThanOrEqual(1)
    // The hold is a compensation write: the inserted rows sit above the reader.
    expect(el.scrollTop).toBeGreaterThan(topBefore)
  })

  it('scrollToBottom jumps to the live edge: writes 0 and re-arms follow', () => {
    const { el, api, scrollTo, readScrollTop, writes } = mount()
    scrollTo(1250)
    expect(api().getFollow()).toBe(false)
    writes.length = 0
    act(() => { api().scrollToBottom() })
    flush()
    expect(readScrollTop()).toBe(0)
    expect(writes.length).toBeGreaterThan(0)
    expect(writes.every((w) => w === 0)).toBe(true)
    expect(api().getFollow()).toBe(true)
    expect(screenTopOf(el, 'm0')).toBe(0)
  })

  it('bulk growth at the head with the TAIL unchanged is not read as a bottom bulk-prepend', () => {
    // A long new turn at index 0 leaves the tail key as it was: the bottom
    // rules call that a bulk prepend, shift the window by the growth and pin
    // to the bottom.
    const { el, rerender, readScrollTop, writes } = mount()
    writes.length = 0
    rerender({ items: [...mkItems(12, 'n'), ...mkItems(30)] })
    flush()
    expect(readScrollTop()).toBe(0)
    expect(writes.every((w) => w === 0)).toBe(true)
    expect(screenTopOf(el, 'n0')).toBe(0)
  })

  it('a hydration REPLACE in top mode remounts the head and stays at 0, never the bottom', () => {
    const { el, rerender, readScrollTop, writes } = mount([{ id: 'optimistic' }])
    writes.length = 0
    rerender({ items: mkItems(50, 'h') })
    flush()
    expect(readScrollTop()).toBe(0)
    expect(writes.every((w) => w === 0)).toBe(true)
    expect(screenTopOf(el, 'h0')).toBe(0)
    expect(el.querySelector('[data-key="h49"]')).toBeNull()
  })
})
