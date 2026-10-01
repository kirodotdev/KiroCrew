/**
 * A released reader is held through a WIDTH TRANSITION -- and held exactly once.
 *
 * Dragging the sidebar (or collapsing the rail) re-wraps every mounted row and
 * fires the ResizeObserver for each, while `canMeasure()` is false for the
 * whole drag plus the 200ms width settle: the persisted cache still belongs to
 * the OLD width, so no measurement may land in it. Two invariants are pinned
 * here. On an engine with no native scroll anchoring (WebKit, which this
 * scroller double simulates: nothing adjusts scrollTop but the hook) every
 * re-wrap above the reader must still be compensated, gate or no gate. And the
 * compensation ADDS the raw delta to scrollTop, so each fire must be priced
 * against the height the DOM showed at the previous fire, never against the
 * refused old-width cache: 100 -> 120 -> 140 is credited 20 + 20, not 20 + 40,
 * and a repeated 140 is credited 0, not 40. The old width's heights stay
 * untouched throughout.
 *
 * Drives the real hook through its ResizeObserver and ref seeds, the way
 * `useVirtualChat.repriceSameFrame` does; nothing about heights, the index or
 * the window is mocked.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'
import { HeightCache } from '../hooks/virtualizer/HeightCache'
import { HeightIndex } from '../hooks/virtualizer/HeightIndex'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

/** Every row box made by makeRow while a scroller is live: a scroll moves them
 *  all, as layout does, so a row's viewport top and scrollTop stay one model. */
let liveBoxes: Array<{ top: number; h: number }> = []

/** Scrolling moves every mounted row by the same amount; a write is the hook's
 *  own unless a test moves `scrollTop` itself (`nudge`) to play the reader's
 *  hand or the engine's native anchoring. No scroll EVENT is dispatched by a
 *  write (the browser's arrives a frame later); tests dispatch one when they
 *  model that frame. */
function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state: Geom = { ...initial }
  const scrollTo = (v: number) => {
    const d = v - state.scrollTop
    state.scrollTop = v
    for (const box of liveBoxes) box.top -= d
  }
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { scrollTo(v) },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { scrollTo(o.top) }
  el.getBoundingClientRect = () =>
    ({ top: 0, bottom: 400, left: 0, right: 390, width: 390, height: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  /** Move scrollTop by `d` from OUTSIDE the hook: the reader, or the engine. */
  const nudge = (d: number) => scrollTo(state.scrollTop + d)
  return { el, state, nudge }
}

/** A row whose height and viewport position both move; a negative `top` is above the fold. */
function makeRow(box: { top: number; h: number }) {
  liveBoxes.push(box)
  const node = document.createElement('div')
  Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => box.h })
  node.getBoundingClientRect = () =>
    ({
      top: box.top, bottom: box.top + box.h, left: 0, right: 390,
      width: 390, height: box.h, x: 0, y: box.top, toJSON: () => ({}),
    }) as DOMRect
  return node
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))
const N = 30
const OLD_SCOPE = 'width-transition:w1216'
const NEW_SCOPE = 'width-transition:w1024'

describe('useVirtualChat: above-fold compensation during a width transition', () => {
  let origRaf: typeof requestAnimationFrame
  let origRO: typeof ResizeObserver | undefined
  let fire: ((entries: { target: Element }[]) => void) | undefined
  const owners = new Set<HeightIndex>()

  beforeEach(() => {
    localStorage.clear()
    owners.clear()
    liveBoxes = []
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(cb: ResizeObserverCallback) {
        fire = (entries) => cb(entries as unknown as ResizeObserverEntry[], this as unknown as ResizeObserver)
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    // Every owner that ever took a write, so the persisted blobs can be flushed
    // and read back per scope without reaching into the hook.
    const setMeasured = HeightIndex.prototype.setMeasured
    vi.spyOn(HeightIndex.prototype, 'setMeasured').mockImplementation(function (this: HeightIndex, index, height) {
      owners.add(this)
      setMeasured.call(this, index, height)
    })
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    globalThis.requestAnimationFrame = origRaf
    // jsdom ships no ResizeObserver: put back the absence, not the double.
    if (origRO) globalThis.ResizeObserver = origRO
    else delete (globalThis as { ResizeObserver?: typeof ResizeObserver }).ResizeObserver
    fire = undefined
    localStorage.clear()
  })

  function persisted(scope: string, key: string) {
    for (const owner of owners) owner.flush()
    return new HeightCache(scope).peek(key)
  }

  /**
   * Park mid-transcript with follow RELEASED, one measured row above the fold
   * and one inside the viewport, both seeded while the width scope is settled.
   * `gate` is the live `canMeasure()` answer; flipping it is the drag starting.
   */
  function setup(opts: { streamingIndex?: number } = {}, following = false) {
    const parkedAt = following ? 4600 : 2000
    const { el, state, nudge } = makeScroller({ scrollTop: parkedAt, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(N)
    const gate = { open: true }
    const canMeasure = () => gate.open
    const props: UseVirtualChatOptions<Item> = {
      items, sessionId: 'width-transition', heightScopeKey: OLD_SCOPE, canMeasure,
      getKey, externalScrollerRef: ref, followOutput: true, ...opts,
    }
    const view = renderHook((p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p), { initialProps: props })
    act(() => {
      state.scrollTop = parkedAt
      el.dispatchEvent(new Event('scroll'))
    })
    const above = { top: -900, h: 250 }
    const visible = { top: 40, h: 250 }
    const aboveRow = makeRow(above)
    const visibleRow = makeRow(visible)
    act(() => {
      view.result.current.measureRef(3)(aboveRow)
      view.result.current.measureRef(12)(visibleRow)
    })
    expect(persisted(OLD_SCOPE, 'm3')).toBe(250)
    return { el, state, nudge, view, props, gate, above, aboveRow, visible, visibleRow }
  }

  /** The row above the reader re-wraps to `h`; with no native anchor, nothing else moves. */
  function rewrap(state: Geom, row: { top: number; h: number }, visible: { top: number; h: number }, h: number) {
    const delta = h - row.h
    row.h = h
    state.scrollHeight += delta
    visible.top += delta
  }

  it('holds the reader through several re-wraps of a row above the fold while the scope is gated, crediting each fire once', () => {
    const { state, gate, above, aboveRow, visible } = setup()
    const startedAt = state.scrollTop
    gate.open = false

    // 250 -> 270: one re-wrap, one hold.
    rewrap(state, above, visible, 270)
    act(() => { fire?.([{ target: aboveRow }]) })
    expect(state.scrollTop).toBe(startedAt + 20)

    // 270 -> 290: the second fire is priced against 270, not the refused 250.
    rewrap(state, above, visible, 290)
    act(() => { fire?.([{ target: aboveRow }]) })
    expect(state.scrollTop).toBe(startedAt + 40)

    // The observer re-reports an unchanged 290 (the debounce window is full of
    // these): nothing moved, so nothing may be credited.
    act(() => { fire?.([{ target: aboveRow }]) })
    act(() => { fire?.([{ target: aboveRow }]) })
    expect(state.scrollTop).toBe(startedAt + 40)

    // The old width's measurement is untouched by every one of those fires.
    expect(persisted(OLD_SCOPE, 'm3')).toBe(250)
    expect(persisted(NEW_SCOPE, 'm3')).toBeUndefined()
  })

  it('credits every row of one batch that sat above the fold BEFORE the reflow, even when siblings pushed it past the fold', () => {
    // Three rows re-wrap in ONE observer fire: two fully above the fold and the
    // straddling row under the reader's eye line. The observer reports each
    // row's POST-layout rect, in which the rows above have already pushed the
    // lower ones down by their growth -- so the second above-fold row and the
    // straddler both read as "top at or below the fold" and were dropped
    // (probe: four table rows +320 each, only two credited, reader row 320px
    // lower). Pre-reflow the classification is unambiguous, and the whole
    // batch is one displacement.
    const { state, gate, above, aboveRow, visible, view } = setup()
    const startedAt = state.scrollTop
    // A second row above the fold, close enough that the first row's growth
    // alone carries it past the fold, and the straddler at the fold.
    const mid = { top: -200, h: 150 }
    const midRow = makeRow(mid)
    const straddle = { top: -20, h: 250 }
    const straddleRow = makeRow(straddle)
    act(() => {
      view.result.current.measureRef(5)(midRow)
      view.result.current.measureRef(7)(straddleRow)
    })
    gate.open = false
    // Post-layout geometry of the batch: 3 grows 300, 5 grows 100, 7 grows 200.
    // Row 5 lands at -200 + 300 = +100 (past the fold), row 7 at -20 + 400.
    above.h = 550
    mid.top += 300; mid.h = 250
    straddle.top += 400; straddle.h = 450
    visible.top += 600
    state.scrollHeight += 600
    act(() => { fire?.([{ target: aboveRow }, { target: midRow }, { target: straddleRow }]) })
    // Whole batch credited once: 300 + 100 + 200 (the straddler's full change,
    // the existing rule for a re-wrapped straddling row).
    expect(state.scrollTop).toBe(startedAt + 600)
    // The write moved every row back up: the straddler's bottom is where it
    // was, its top 200 higher (the bottom-held rule).
    expect(straddle.top + straddle.h).toBe(230)
    // The same batch reversed, reported bottom-up: rows 3 and 5 are entirely
    // above the fold and the straddler still straddles, so the shrink is
    // paid back whole and the reader's row returns to its original box.
    above.h = 250; mid.top -= 300; mid.h = 150; straddle.top -= 400; straddle.h = 250
    visible.top -= 600; state.scrollHeight -= 600
    act(() => { fire?.([{ target: straddleRow }, { target: midRow }, { target: aboveRow }]) })
    expect(state.scrollTop).toBe(startedAt)
    expect(straddle.top).toBe(-20)
    expect(persisted(OLD_SCOPE, 'm3')).toBe(250)
  })

  /**
   * The engine's NATIVE scroll anchoring, modelled. Chromium adjusts scrollTop
   * for a reprice above the viewport DURING the layout that reprices it --
   * before any ResizeObserver callback, before its own scroll event -- holding
   * the TOP of the row it chose as anchor. So when the fire arrives, scrollTop
   * already carries that adjustment and every rect already reflects it. The
   * hook must pay only what native left undone (measured against where the
   * reader's row was last seen), never the batch's summed growth on top of
   * native's payment, and a row native pushed under the fold must not be read
   * as straddling.
   *
   * Layout (viewport 400): row 3 far above (-900), row 10 straddling the fold
   * (-20..230, the reader's row), row 12 visible below it (230..480), row 14
   * mounted below the viewport (480..730). Batch: 3 +300, 10 +320, 12 +320.
   */
  function nativeLayout() {
    const s = setup()
    const straddle = { top: -20, h: 250 }
    const under = { top: 230, h: 250 }
    const below = { top: 480, h: 250 }
    const straddleRow = makeRow(straddle)
    const underRow = makeRow(under)
    const belowRow = makeRow(below)
    act(() => {
      s.view.result.current.measureRef(10)(straddleRow)
      s.view.result.current.measureRef(12)(underRow)
      s.view.result.current.measureRef(14)(belowRow)
    })
    /** The batch's LAYOUT: 3 +300, 10 +320, 12 +320, each row pushed by the
     *  growth above it (scrollTop untouched -- what the fire sees on an engine
     *  with no anchoring; a native adjustment is a `nudge` on top). */
    const grow = () => {
      s.above.h = 550; straddle.h = 570; under.h = 570
      straddle.top += 300; under.top += 620; below.top += 940
      s.state.scrollHeight += 940
    }
    return { ...s, straddle, under, below, straddleRow, underRow, belowRow, grow }
  }

  it('pays only the residual when native anchoring already held the reader row (Chromium), and a row pushed under the fold is not counted', () => {
    const { state, nudge, gate, aboveRow, straddle, straddleRow, underRow, grow } = nativeLayout()
    const startedAt = state.scrollTop
    gate.open = false
    grow()
    // NATIVE, before the fire: the engine holds the anchor row's top by
    // scrolling by the growth above it (row 3's 300). Everything is where
    // native left it when the fire reads the rects.
    const native = 300
    nudge(native)
    expect(straddle.top).toBe(-20)
    act(() => { fire?.([{ target: aboveRow }, { target: straddleRow }, { target: underRow }]) })
    // The hook adds exactly the straddler's own growth (bottom held), not
    // 300 + 320 again, and not 320 more for row 12 (whose post-layout top,
    // minus the batch growth above it, would read as straddling).
    expect(state.scrollTop).toBe(startedAt + native + 320)
    expect(straddle.top).toBe(-340)
    expect(straddle.top + straddle.h).toBe(230)
    expect(persisted(OLD_SCOPE, 'm3')).toBe(250)
    expect(persisted(OLD_SCOPE, 'm10')).toBe(250)
    expect(persisted(NEW_SCOPE, 'm10')).toBeUndefined()
  })

  it('pays the whole batch when nothing native moved (WebKit), the same write as before', () => {
    const { state, gate, aboveRow, straddle, straddleRow, underRow, grow } = nativeLayout()
    const startedAt = state.scrollTop
    gate.open = false
    grow()
    act(() => { fire?.([{ target: aboveRow }, { target: straddleRow }, { target: underRow }]) })
    expect(state.scrollTop).toBe(startedAt + 300 + 320)
    expect(straddle.top + straddle.h).toBe(230)
  })

  it('writes nothing when native anchoring chose a row below the reader and already held the straddler bottom', () => {
    const { state, nudge, gate, aboveRow, straddle, under, straddleRow, underRow, grow } = nativeLayout()
    const startedAt = state.scrollTop
    gate.open = false
    grow()
    // Native held row 12's top: it absorbed 300 + 320, so the straddler's
    // top moved up by its own growth -- exactly the bottom-held rule.
    nudge(620)
    expect(under.top).toBe(230)
    act(() => { fire?.([{ target: aboveRow }, { target: straddleRow }, { target: underRow }]) })
    expect(state.scrollTop).toBe(startedAt + 620)
    expect(straddle.top + straddle.h).toBe(230)
  })

  it('a click that scrolls nothing leaves the record valid: a resize two seconds later is paid in full', () => {
    // pointerdown on the transcript stamps follow's hard-input clock (any
    // click, not only a scrollbar grab). The rows did not move, so the record
    // still describes the frame; a guard keyed on that stamp stood every later
    // fire down and the whole re-wrap went unpaid (probe: reader row 41's top
    // -6 -> +954 with anchoring off).
    const { el, state, gate, aboveRow, straddle, straddleRow, underRow, grow } = nativeLayout()
    const startedAt = state.scrollTop
    act(() => { el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })) })
    act(() => { vi.advanceTimersByTime(2000) })
    gate.open = false
    grow()
    act(() => { fire?.([{ target: aboveRow }, { target: straddleRow }, { target: underRow }]) })
    expect(state.scrollTop).toBe(startedAt + 300 + 320)
    expect(straddle.top + straddle.h).toBe(230)
  })

  it('a PageDown whose scroll has not landed yet does not stand the fire down, and its scroll is kept when it lands', () => {
    // keydown stamps the clock; the smooth scroll it starts reaches scrollTop
    // (and dispatches its event) a frame or more later. At the fire nothing
    // has moved the rows the record describes, so the re-wrap is paid now;
    // the user's scroll then lands on top and is never undone.
    const { el, state, nudge, gate, aboveRow, straddle, under, below, straddleRow, underRow, grow, above } = nativeLayout()
    const startedAt = state.scrollTop
    act(() => { el.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageDown', bubbles: true })) })
    gate.open = false
    grow()
    act(() => { fire?.([{ target: aboveRow }, { target: straddleRow }, { target: underRow }]) })
    expect(state.scrollTop).toBe(startedAt + 620)
    expect(straddle.top + straddle.h).toBe(230)
    // The browser's scroll: offset visible to the main thread and its event
    // dispatched in the same rendering update, before any later fire.
    nudge(566)
    act(() => { el.dispatchEvent(new Event('scroll')) })
    // A later re-wrap above the reader is measured from the scrolled frame.
    above.h = 650; straddle.top += 100; under.top += 100; below.top += 100; state.scrollHeight += 100
    act(() => { fire?.([{ target: aboveRow }]) })
    expect(state.scrollTop).toBe(startedAt + 620 + 566 + 100)
  })

  it('a wheel whose scroll landed before the fire is kept: the fire measures from the scrolled frame', () => {
    // The user's scroll reaches scrollTop at the start of a rendering update
    // and its scroll event runs before layout and the observer, so the record
    // is re-read before the fire reads any rect.
    const { el, state, nudge, gate, aboveRow, under, straddleRow, underRow, grow } = nativeLayout()
    const startedAt = state.scrollTop
    act(() => { el.dispatchEvent(new WheelEvent('wheel', { deltaY: 300, bubbles: true })) })
    nudge(300)
    act(() => { el.dispatchEvent(new Event('scroll')) })
    // Row 12 now straddles the fold (top -70); it is the reader's row.
    expect(under.top).toBe(-70)
    gate.open = false
    grow()
    act(() => { fire?.([{ target: aboveRow }, { target: straddleRow }, { target: underRow }]) })
    // Its bottom is held where the user left it; the 300 they scrolled stays.
    expect(under.top + under.h).toBe(180)
    expect(state.scrollTop).toBe(startedAt + 300 + 940)
  })

  it('a row that left the window is no anchor: the next reader row is measured instead', () => {
    const { view, state, nudge, gate, aboveRow, straddle, under, below, straddleRow, underRow, grow, above } = nativeLayout()
    const startedAt = state.scrollTop
    gate.open = false
    // The straddler unmounts (a window shift, a session switch); row 12 is
    // now the reader's row and its own position decides the write.
    act(() => { view.result.current.measureRef(10)(null) })
    grow()
    // Native held row 12 (the new anchor): nothing left for the hook.
    nudge(620)
    act(() => { fire?.([{ target: aboveRow }, { target: straddleRow }, { target: underRow }]) })
    expect(state.scrollTop).toBe(startedAt + 620)
    // No native this time: row 3 grows another 100 and pushes row 12 down by
    // it; row 12 is fully visible (top at or below the fold), so nothing of
    // its own is credited and the write is exactly its displacement.
    above.h = 650; straddle.top += 100; under.top += 100; below.top += 100; state.scrollHeight += 100
    act(() => { fire?.([{ target: aboveRow }]) })
    expect(state.scrollTop).toBe(startedAt + 620 + 100)
    expect(under.top).toBe(230)
  })

  it('a later callback of the same layout measures only what it adds: the record moves with the write, it is not re-read mid-frame', () => {
    // The observer delivers one layout over several callbacks (the scroller's
    // own box came alone, the rows an iteration later, in the width probe).
    // Every rect already shows the whole reflow at the first callback.
    const { el, state, gate, aboveRow, straddle, straddleRow, underRow, grow } = nativeLayout()
    const startedAt = state.scrollTop
    gate.open = false
    grow()
    // Callback 1: the scroller's box only. Nothing genuine, no write -- and
    // the record must NOT be refreshed from these already-reflowed rects.
    act(() => { fire?.([{ target: el }]) })
    expect(state.scrollTop).toBe(startedAt)
    // Callback 2: row 3 alone. Its 300 pushed the straddler; paid.
    act(() => { fire?.([{ target: aboveRow }]) })
    expect([state.scrollTop, straddle.top]).toEqual([startedAt + 300, -20])
    // Callback 3: the straddler and row 12. Only the straddler's own 320 is
    // still owed -- the record moved with callback 2's write.
    act(() => { fire?.([{ target: straddleRow }, { target: underRow }]) })
    expect(state.scrollTop).toBe(startedAt + 620)
    expect(straddle.top + straddle.h).toBe(230)
  })

  it('walks the reader back when the width returns, and a settled re-fire at the old height adds nothing', () => {
    const { state, gate, above, aboveRow, visible } = setup()
    const startedAt = state.scrollTop
    gate.open = false
    rewrap(state, above, visible, 290)
    act(() => { fire?.([{ target: aboveRow }]) })
    expect(state.scrollTop).toBe(startedAt + 40)

    // Back to the original width while still gated: the same delta, reversed.
    rewrap(state, above, visible, 250)
    act(() => { fire?.([{ target: aboveRow }]) })
    expect(state.scrollTop).toBe(startedAt)

    // The width settles on the ORIGINAL bucket; the observer re-reports 250.
    gate.open = true
    act(() => { fire?.([{ target: aboveRow }]) })
    act(() => { vi.advanceTimersByTime(400) })
    expect(state.scrollTop).toBe(startedAt)
    expect(persisted(OLD_SCOPE, 'm3')).toBe(250)
  })

  it('does not compensate again when the new scope reseeds after the settle, nor when its debounced sync lands', () => {
    const { state, view, props, gate, above, aboveRow, visible } = setup()
    const startedAt = state.scrollTop
    gate.open = false
    rewrap(state, above, visible, 270)
    act(() => { fire?.([{ target: aboveRow }]) })
    rewrap(state, above, visible, 290)
    act(() => { fire?.([{ target: aboveRow }]) })
    expect(state.scrollTop).toBe(startedAt + 40)

    // The bucket settles: a new scope, a new gate identity, and the facade's
    // reseed writes every mounted row's live height into the new owner.
    act(() => { view.rerender({ ...props, heightScopeKey: NEW_SCOPE, canMeasure: () => true }) })
    expect(persisted(NEW_SCOPE, 'm3')).toBe(290)
    expect(persisted(OLD_SCOPE, 'm3')).toBe(250)
    // The reseed is measurements only -- the reader was already held.
    expect(state.scrollTop).toBe(startedAt + 40)

    // A late duplicate fire and the debounced height sync both find the row
    // where it already is.
    act(() => { fire?.([{ target: aboveRow }]) })
    expect(state.scrollTop).toBe(startedAt + 40)
    act(() => { vi.advanceTimersByTime(400) })
    expect(state.scrollTop).toBe(startedAt + 40)
    expect(persisted(NEW_SCOPE, 'm3')).toBe(290)
    expect(persisted(OLD_SCOPE, 'm3')).toBe(250)
  })

  it('classifies a row mounted DURING the transition from the live height seeded at mount', () => {
    const { state, view, gate, visible } = setup()
    const startedAt = state.scrollTop
    gate.open = false

    // Scrolled into during the drag: the seed is refused by the old scope but
    // the node's live height is known from mount, so the observer's first
    // report -- arriving after a re-wrap -- is priced against it rather than
    // taken for a first mount.
    const late = { top: -400, h: 180 }
    const lateRow = makeRow(late)
    act(() => { view.result.current.measureRef(6)(lateRow) })
    expect(persisted(OLD_SCOPE, 'm6')).toBeUndefined()
    rewrap(state, late, visible, 200)
    act(() => { fire?.([{ target: lateRow }]) })
    expect(state.scrollTop).toBe(startedAt + 20)

    // Its next re-wrap above the fold is credited once.
    rewrap(state, late, visible, 230)
    act(() => { fire?.([{ target: lateRow }]) })
    act(() => { fire?.([{ target: lateRow }]) })
    expect(state.scrollTop).toBe(startedAt + 50)
    expect(persisted(OLD_SCOPE, 'm6')).toBeUndefined()

    // A node the pass has never seen at a real height (seeded under a hidden
    // ancestor) is a first mount, not a resize -- the fallback the settled
    // path always had.
    const hidden = { top: -600, h: 0 }
    const hiddenRow = makeRow(hidden)
    act(() => { view.result.current.measureRef(8)(hiddenRow) })
    hidden.h = 120
    act(() => { fire?.([{ target: hiddenRow }]) })
    expect(state.scrollTop).toBe(startedAt + 50)
  })

  it('leaves a released reader alone when the straddling streaming row appends during the transition', () => {
    // Reader inside the streaming reply: top above the fold, bottom far below.
    const { state, view, gate } = setup({ streamingIndex: N - 1 })
    const reply = { top: -3000, h: 8000 }
    const replyRow = makeRow(reply)
    act(() => { view.result.current.measureRef(N - 1)(replyRow) })
    const startedAt = state.scrollTop
    gate.open = false
    for (const px of [27, 54, 27]) {
      reply.h += px
      state.scrollHeight += px
      act(() => { fire?.([{ target: replyRow }]) })
    }
    // Appends move nothing above the reader; the gate must not turn them into
    // re-wraps.
    expect(state.scrollTop).toBe(startedAt)
    expect(persisted(OLD_SCOPE, `m${N - 1}`)).toBe(8000)
  })

  it.each([true, false])('preserves trailing-footer follow=%s while width-cache writes are gated', following => {
    const { state, view, gate } = setup({}, following)
    const wrapper = document.createElement('div')
    ;(view.result.current.trailingRef as { current: HTMLDivElement | null }).current = wrapper
    expect(view.result.current.getFollow()).toBe(following)
    const startedAt = state.scrollTop
    gate.open = false
    state.scrollHeight += 56

    // Only the footer changed: no row or viewport resize can mask a missing
    // trailing-chrome follow signal while row-cache writes stand down.
    act(() => { fire?.([{ target: wrapper }]) })
    expect(state.scrollTop).toBe(following ? state.scrollHeight - state.clientHeight : startedAt)
    expect(view.result.current.getFollow()).toBe(following)
    expect(persisted(OLD_SCOPE, 'm3')).toBe(250)
    expect(view.result.current.farmIsMeasured(0)).toBe(false)
  })

  it('forgets a detached node: a remount is seeded afresh and its first fire credits nothing', () => {
    const { state, view, gate, above, aboveRow, visible } = setup()
    const startedAt = state.scrollTop
    gate.open = false
    rewrap(state, above, visible, 290)
    act(() => { fire?.([{ target: aboveRow }]) })
    expect(state.scrollTop).toBe(startedAt + 40)

    // The row leaves the window and comes back as a NEW node at the re-wrapped
    // height (a slot switch, a window shift): no history, so no credit.
    act(() => { view.result.current.measureRef(3)(null) })
    const remounted = makeRow({ top: -900, h: 290 })
    act(() => { view.result.current.measureRef(3)(remounted) })
    act(() => { fire?.([{ target: remounted }]) })
    expect(state.scrollTop).toBe(startedAt + 40)

    // The ORIGINAL node re-attaching carries no history either: back under a
    // hidden ancestor (seed 0), its first real height is a first mount, not a
    // resize priced against what it showed before it detached.
    act(() => { view.result.current.measureRef(3)(null) })
    above.h = 0
    act(() => { view.result.current.measureRef(3)(aboveRow) })
    above.h = 250
    act(() => { fire?.([{ target: aboveRow }]) })
    expect(state.scrollTop).toBe(startedAt + 40)
    expect(persisted(OLD_SCOPE, 'm3')).toBe(250)
  })
})

/**
 * The WIDTH-SCOPE SWAP itself: the settled bucket hands the height owner to a
 * COLD index, and the before-spacer is re-priced from that index's estimates in
 * the render that constructs it, while the mounted rows keep their heights and
 * scrollTop stays put -- so everything under the reader moves up by
 * (old prefix - estimate prefix) in one commit. The fixed-DOM cases above never
 * model the spacer, which is why they stayed green over a reproduced blank
 * frame (probe: same window, spacerBefore 9353 -> 3500, scrollTop unchanged,
 * topmost row 41 -> null -> 111).
 *
 * This scroller models LIVE geometry: a mounted row's screen top is the hook's
 * own `offsetBefore` plus its distance into the window minus scrollTop, exactly
 * as the spacer div places it. Nothing but the hook moves scrollTop.
 */
describe('useVirtualChat: the reader is anchored across a cold width-scope swap', () => {
  const H = 250
  const EST = 80
  const ROWS = 60
  const WARM = 'cold-swap:tables1:w1216'
  const COLD = 'cold-swap:tables1:w832'
  let origRaf: typeof requestAnimationFrame
  let origRO: typeof ResizeObserver | undefined
  const owners = new Set<HeightIndex>()
  /** Extra px every measurement written into the COLD scope carries, so a case
   *  can model rows that re-wrap taller at the new width. */
  let coldExtra = 0

  beforeEach(() => {
    localStorage.clear()
    owners.clear()
    coldExtra = 0
    // Fake timers FIRST: they fake requestAnimationFrame too, and the scroll
    // listener's window recompute rides a frame, so the synchronous frame
    // below must be installed over the fake one.
    vi.useFakeTimers()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    const setMeasured = HeightIndex.prototype.setMeasured
    vi.spyOn(HeightIndex.prototype, 'setMeasured').mockImplementation(function (this: HeightIndex, index, height) {
      owners.add(this)
      setMeasured.call(this, index, this.sessionId === COLD ? height + coldExtra : height)
    })
    // The warm width has every row measured at H, persisted.
    const warm = new HeightCache(WARM, { rowCount: ROWS })
    for (let i = 0; i < ROWS; i++) warm.set(`m${i}`, H)
    warm.flush()
  })
  afterEach(() => {
    globalThis.requestAnimationFrame = origRaf
    vi.useRealTimers()
    vi.restoreAllMocks()
    if (origRO) globalThis.ResizeObserver = origRO
    else delete (globalThis as { ResizeObserver?: typeof ResizeObserver }).ResizeObserver
    localStorage.clear()
  })

  function persisted(scope: string, key: string) {
    for (const owner of owners) owner.flush()
    return new HeightCache(scope).peek(key)
  }

  /** How the scroller double behaves at LAYOUT, which a real engine forces at
   *  every scrollTop / scrollHeight / getBoundingClientRect read. The default
   *  (nothing set) is the frozen scroller the earlier cases were written
   *  against: scrollTop is whatever was last written, whatever the document's
   *  height. */
  interface RangeModel {
    /** The engine keeps scrollTop within [0, scrollHeight - clientHeight]: a
     *  document that commits shorter than the reader's position drags
     *  scrollTop up to the new ceiling, and it stays there when the document
     *  grows back (both engines, verify-firefox / cr-deep-trace). */
    clamp?: boolean
    /** Where the engine itself puts scrollTop when the cold document first
     *  lays out (native scroll anchoring moving the reader by the spacer
     *  delta, well below the ceiling: 14662 for the same capture). */
    coldDrop?: number
    /** An input that lands once the reseeded document is laid out (a wheel
     *  the engine processed between the two commits). */
    scrollAfterReseed?: number
    /** Reader row; 41 puts the reader below the cold document's ceiling, 15
     *  keeps them above it (a fresh owner prices the whole cold document at
     *  ROWS * EST until the reseed). */
    read?: number
  }

  /** Park the reader with row `READ` at screen top 40 on the warm width, every
   *  window row mounted with live geometry. */
  function setup(opts: Partial<UseVirtualChatOptions<Item>> = {}, model: RangeModel = {}) {
    const READ = model.read ?? 41
    const state = { scrollTop: 0, clientHeight: 400 }
    const hook: { current: { offsetBefore: number; totalHeight: number; virtualItems: { index: number }[] } | null } = { current: null }
    const range = { armed: false, clamps: 0, clampedTo: -1, parkedHeight: 0, dropApplied: false, reseedSeen: false, inputApplied: false }
    /** The engine's layout: applied at every layout-forcing read. */
    const layout = () => {
      const r = hook.current
      if (!r || !range.armed) return
      if (model.coldDrop !== undefined && !range.dropApplied && r.totalHeight < range.parkedHeight) {
        range.dropApplied = true
        state.scrollTop = model.coldDrop
      }
      if (model.clamp) {
        const ceiling = Math.max(0, r.totalHeight - state.clientHeight)
        if (state.scrollTop > ceiling) { state.scrollTop = ceiling; range.clamps++; range.clampedTo = ceiling }
        else if (range.clamps > 0 && !range.reseedSeen && ceiling > range.clampedTo) {
          range.reseedSeen = true
          if (model.scrollAfterReseed && !range.inputApplied) { range.inputApplied = true; state.scrollTop += model.scrollAfterReseed }
        }
      }
    }
    const el = document.createElement('div')
    Object.defineProperty(el, 'scrollTop', {
      configurable: true, get: () => { layout(); return state.scrollTop }, set: (v: number) => { state.scrollTop = v; layout() },
    })
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => { layout(); return hook.current?.totalHeight ?? ROWS * H } })
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
    const writes: number[] = []
    ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { writes.push(o.top); state.scrollTop = o.top; layout() }
    el.getBoundingClientRect = () =>
      ({ top: 0, bottom: 400, left: 0, right: 800, width: 800, height: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(ROWS)
    const gate = { open: true }
    const canMeasure = () => gate.open
    const props: UseVirtualChatOptions<Item> = {
      items, sessionId: 'cold-swap', heightScopeKey: WARM, canMeasure, estimatedHeight: EST,
      getKey, externalScrollerRef: ref, followOutput: true, ...opts,
    }
    const view = renderHook((p: UseVirtualChatOptions<Item>) => {
      const r = useVirtualChat<Item>(p)
      hook.current = r
      return r
    }, { initialProps: props })
    // Two scroll events: the first gives the fresh scroller its direction
    // baseline (at the bottom, where follow placed it), the second is the
    // reader's climb, which releases follow and re-derives the window.
    act(() => {
      state.scrollTop = hook.current!.totalHeight - state.clientHeight
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => {
      state.scrollTop = READ * H - 40
      el.dispatchEvent(new Event('scroll'))
    })
    expect(view.result.current.getFollow()).toBe(false)
    /** What the DOM shows for a mounted row; the cache may disagree while the
     *  measurement gate is closed. */
    const domH = new Map<number, number>()
    const dh = (i: number) => domH.get(i) ?? H
    /** Live top of mounted row `i`: the spacer the hook renders (priced by the
     *  tree), then the mounted rows at their DOM heights. */
    const rowTop = (i: number) => {
      layout()
      const r = hook.current!
      let top = r.offsetBefore - state.scrollTop
      for (let j = r.virtualItems[0].index; j < i; j++) top += dh(j)
      return top
    }
    const nodes = new Map<number, HTMLElement>()
    act(() => {
      for (const { index } of hook.current!.virtualItems) {
        const node = document.createElement('div')
        nodes.set(index, node)
        Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => dh(index) })
        node.getBoundingClientRect = () => {
          const top = rowTop(index)
          const h = dh(index)
          return { top, bottom: top + h, left: 0, right: 800, width: 800, height: h, x: 0, y: top, toJSON: () => ({}) } as DOMRect
        }
        view.result.current.measureRef(index)(node)
      }
    })
    const start = hook.current!.virtualItems[0].index
    expect(start).toBeLessThan(READ)
    expect(hook.current!.offsetBefore).toBe(start * H)
    expect(rowTop(READ)).toBe(40)
    writes.length = 0
    range.armed = true
    range.parkedHeight = hook.current!.totalHeight
    /** The ceiling the cold document commits with: a fresh owner prices EVERY
     *  row at the flat estimate until the reseed announces the mounted ones. */
    const coldCeiling = ROWS * EST - state.clientHeight
    return { view, props, state, hook, rowTop, start, READ, writes, nodes, gate, domH, el, range, coldCeiling }
  }

  it('a scroll recompute that unmounts re-wrapped rows above the reader while the tree still holds the old width is paid back', () => {
    // Mid-transition: three table rows above the reader re-wrapped +320 each
    // and the fire held the reader (scrollTop +960), but the gate keeps those
    // heights out of the tree. The user then wheels down 400: the scroll
    // recompute maps scrollTop through the STALE tree, walks the window start
    // down past the hysteresis and unmounts rows 35-40 -- replaced by a spacer
    // priced 960 short of the DOM they were. With no native anchoring nothing
    // else corrects that (probe: -960 after a settled wheel, and again after
    // a wheel during the resize). TRIGGER 2 now captures a downward start
    // move too and pays the reader's row back in the same commit.
    const { view, state, hook, rowTop, start, READ, gate, domH, el } = setup()
    gate.open = false
    for (const i of [35, 37, 39]) domH.set(i, H + 320)
    // The re-wrap's compensation already landed (not under test here); the
    // browser's scroll event for it re-read the record.
    state.scrollTop += 960
    act(() => { el.dispatchEvent(new Event('scroll')) })
    expect(rowTop(READ)).toBe(40)
    expect(hook.current!.virtualItems[0].index).toBe(start)
    // The user's wheel.
    state.scrollTop += 400
    const readerBefore = rowTop(READ + 1)
    expect(readerBefore).toBe(-110)
    act(() => { el.dispatchEvent(new Event('scroll')) })
    // The window start walked down through the stale tree and unmounted the
    // re-wrapped rows...
    const s2 = hook.current!.virtualItems[0].index
    expect(s2).toBeGreaterThan(start)
    expect(s2).toBeLessThanOrEqual(READ)
    expect(hook.current!.offsetBefore).toBe(s2 * H)
    // ...and the reader's row sits exactly where the wheel left it, the user's
    // 400 intact: on the unanchored code it is 960 higher.
    expect(rowTop(READ + 1)).toBe(readerBefore)
    expect(view.result.current.getFollow()).toBe(false)
    expect(persisted(WARM, 'm35')).toBe(H)
  })

  it('holds the reader row at the same top and identity across the swap, with the window unchanged', () => {
    const { view, props, state, hook, rowTop, start, READ, writes } = setup()
    const parkedAt = state.scrollTop

    act(() => { view.rerender({ ...props, heightScopeKey: COLD }) })

    // The cold owner's estimate-priced spacer never reaches paint: the reseed
    // announces the mounted rows' prices in the swap commit, so the committed
    // geometry prices the prefix from their mean, and the row under the reader
    // is the same row at the same place with the window where it was. On the
    // unanchored code the spacer commits at `start * EST` with scrollTop
    // unchanged, and the row sits `start * (H - EST)` px above where it was.
    expect(hook.current!.virtualItems[0].index).toBe(start)
    expect(hook.current!.offsetBefore).toBe(start * H)
    expect(rowTop(READ)).toBe(40)
    expect(state.scrollTop).toBe(parkedAt)
    // Nothing paid the swap against the cold spacer's coordinates.
    expect(writes).not.toContain(parkedAt - start * (H - EST))
    // The warm width is untouched; the cold one holds only the reseeded window.
    expect(persisted(WARM, `m${READ}`)).toBe(H)
    expect(persisted(WARM, 'm0')).toBe(H)
    expect(persisted(COLD, `m${READ}`)).toBe(H)
    expect(persisted(COLD, 'm0')).toBeUndefined()
  })

  it('pays a swap whose new-width prefix differs exactly once: the reader row keeps its top, the debounce adds nothing', () => {
    // A cold width where the mounted rows measure taller than the warm cache
    // says, so the reseeded prefix differs from the warm one and a real
    // scrollTop write is required.
    const { view, props, state, hook, rowTop, start, READ, writes } = setup()
    const parkedAt = state.scrollTop
    coldExtra = 20
    act(() => { view.rerender({ ...props, heightScopeKey: COLD }) })
    // Prefix priced from the new mean (the window may have grown upward, since
    // the taller prefix puts more rows within overscan); the reader row's top is
    // held by moving scrollTop by exactly the prefix delta, once.
    const s2 = hook.current!.virtualItems[0].index
    expect(s2).toBeLessThanOrEqual(start)
    expect(hook.current!.offsetBefore).toBe(s2 * (H + 20))
    expect(state.scrollTop).toBe(parkedAt + s2 * 20)
    expect(rowTop(READ)).toBe(40)
    // The swap was paid ONCE, against the window it committed with; any later
    // write is the upward window growth's own (TRIGGER 2) correction.
    expect(writes[0]).toBe(parkedAt + start * 20)
    const settled = writes.length
    act(() => { vi.advanceTimersByTime(400) })
    expect(writes.length).toBe(settled)
    expect(state.scrollTop).toBe(parkedAt + s2 * 20)
    expect(rowTop(READ)).toBe(40)
  })

  it('walks back to the warm width the same way', () => {
    const { view, props, state, hook, rowTop, start, READ } = setup()
    const parkedAt = state.scrollTop
    act(() => { view.rerender({ ...props, heightScopeKey: COLD }) })
    act(() => { view.rerender({ ...props, heightScopeKey: WARM }) })
    expect(hook.current!.offsetBefore).toBe(start * H)
    expect(state.scrollTop).toBe(parkedAt)
    expect(rowTop(READ)).toBe(40)
    act(() => { vi.advanceTimersByTime(400) })
    expect(state.scrollTop).toBe(parkedAt)
    expect(rowTop(READ)).toBe(40)
    expect(persisted(WARM, 'm0')).toBe(H)
  })

  it('returns to a WARM scope whose prefix differs while the mounted rows already equal its cache, and still pays the reader back', () => {
    // Cold rows measure +20; coming back to the warm width the mounted rows
    // equal the warm cache exactly, so the reseed WRITES nothing -- the
    // announcement must still come (a fresh owner has no announced baseline)
    // or the capture sits unconsumed and the reader lands start*20 off.
    const { view, props, state, hook, rowTop, start, READ, writes } = setup()
    const parkedAt = state.scrollTop
    coldExtra = 20
    act(() => { view.rerender({ ...props, heightScopeKey: COLD }) })
    expect(rowTop(READ)).toBe(40)
    const s2 = hook.current!.virtualItems[0].index
    expect(state.scrollTop).toBe(parkedAt + s2 * 20)
    writes.length = 0
    act(() => { view.rerender({ ...props, heightScopeKey: WARM }) })
    const s3 = hook.current!.virtualItems[0].index
    expect(hook.current!.offsetBefore).toBe(s3 * H)
    expect(rowTop(READ)).toBe(40)
    expect(writes.length).toBeGreaterThan(0)
    act(() => { vi.advanceTimersByTime(400) })
    expect(rowTop(READ)).toBe(40)
    expect(persisted(WARM, 'm0')).toBe(H)
    expect(persisted(WARM, `m${READ}`)).toBe(H)
    expect(persisted(COLD, `m${READ}`)).toBe(H + 20)
    expect(start).toBeGreaterThan(0)
  })

  it('with a scope key but no canMeasure there is no reseed, and the swap commits unanchored', () => {
    // Optional-API shape no shipped host uses (both pass canMeasure). Without
    // the gate the reseed effect stands down, so the swap commit is the
    // unanchored one: the cold spacer with scrollTop untouched, the reader's
    // row a whole `start * (H - EST)` above where it was. The swap capture is
    // not spent against that frame; recovery is the reseed's, which is why
    // the hosts pass the gate. What the later window recompute and sync do
    // with that frame is not a contract of this shape.
    const { view, props, state, hook, rowTop, start, READ } = setup({ canMeasure: undefined })
    const parkedAt = state.scrollTop
    act(() => { view.rerender({ ...props, canMeasure: undefined, heightScopeKey: COLD }) })
    expect(hook.current!.virtualItems[0].index).toBeGreaterThan(start)
    expect(state.scrollTop).toBe(parkedAt)
    expect(Math.abs(rowTop(READ) - (40 - start * (H - EST)))).toBeLessThan(EST)
    expect(persisted(WARM, 'm0')).toBe(H)
  })

  it('keeps a FOLLOWED reader pinned to the tail across the swap instead of anchoring a row', () => {
    const { view, props, state, hook } = setup()
    act(() => {
      state.scrollTop = hook.current!.totalHeight - state.clientHeight
      view.result.current.scrollerRef.current!.dispatchEvent(new Event('scroll'))
    })
    expect(view.result.current.getFollow()).toBe(true)
    act(() => { view.rerender({ ...props, heightScopeKey: COLD }) })
    expect(view.result.current.getFollow()).toBe(true)
    expect(state.scrollTop).toBe(hook.current!.totalHeight - state.clientHeight)
  })

  it('does not capture across a true session switch: the new transcript is placed by its entry, not by the old row', () => {
    const { view, props, state, start, writes } = setup()
    const parkedAt = state.scrollTop
    const other = Array.from({ length: ROWS }, (_, i) => ({ id: `s2-m${i}` }))
    act(() => { view.rerender({ ...props, items: other, sessionId: 'cold-swap-2', heightScopeKey: 'cold-swap-2:tables1:w832' }) })
    // A swap-style correction would have written the spacer delta against the
    // OLD reader row priced through the NEW items.
    expect(writes).not.toContain(parkedAt - start * (H - EST))
    expect(writes).not.toContain(parkedAt)
    expect(persisted(WARM, 'm0')).toBe(H)
    expect(persisted('cold-swap-2:tables1:w832', 's2-m0')).toBeUndefined()
  })

  // The engine's RANGE CLAMP. The swap commit's cold document prices every
  // unmounted row at the flat estimate, so it can be SHORTER than the reader's
  // scrollTop; the engine then drags scrollTop up to the new ceiling with no
  // application write anywhere (Firefox 153 and Chromium 145 at a matched
  // depth: captured 35365, cold ceiling 22083, scrollTop 22083). The reseed
  // grows the document back but scrollTop stays at the ceiling, so the
  // consumer's freshness guard compared the captured 35365 with 22083, took
  // the move for the reader's, and dropped the payment: one blank frame, then
  // a row 75 places away under the reader. The stand-down branch that runs in
  // the swap commit now re-bases the capture's scrollTop to the clamped value
  // -- and only to that value -- so the reseed pays the whole move.
  it('pays a swap whose cold document is too short for the reader: the engine clamps scrollTop to the cold ceiling and the reseed still pays the whole move', () => {
    const { view, props, state, hook, rowTop, start, READ, writes, range, coldCeiling, el } = setup({}, { clamp: true })
    const parkedAt = state.scrollTop
    // Precondition of the defect: the reader sits below the cold ceiling.
    const ceiling = coldCeiling
    expect(parkedAt).toBeGreaterThan(ceiling)

    act(() => { view.rerender({ ...props, heightScopeKey: COLD }) })

    // The engine clamped once, to the cold ceiling, in the swap commit...
    expect(range.clamps).toBe(1)
    expect(range.clampedTo).toBe(ceiling)
    // ...and the reseed paid the reader's whole move from that clamped frame:
    // scrollTop back where the reader parked, the same row at the same top.
    // The frozen consumer compared 35365 with 22083 and returned: scrollTop
    // stayed at the clamp, no write at all, and the window recompute mapped
    // the clamped value through the reseeded tree to a row far above.
    expect(writes).toEqual([parkedAt])
    expect(state.scrollTop).toBe(parkedAt)
    expect(rowTop(READ)).toBe(40)
    // The window recompute inside the swap mapped the clamped value through
    // the reseeded tree (rows far above the reader); the payment's own scroll
    // event, which every engine raises for the write, re-derives it.
    act(() => { el.dispatchEvent(new Event('scroll')) })
    expect(state.scrollTop).toBe(parkedAt)
    expect(hook.current!.offsetBefore).toBe(start * H)
    expect(hook.current!.virtualItems[0].index).toBe(start)
    expect(rowTop(READ)).toBe(40)
    expect(view.result.current.getFollow()).toBe(false)
    act(() => { vi.advanceTimersByTime(400) })
    expect(state.scrollTop).toBe(parkedAt)
    expect(rowTop(READ)).toBe(40)
    expect(persisted(WARM, 'm0')).toBe(H)
    expect(persisted(COLD, `m${READ}`)).toBe(H)
  })

  it('a scroll that lands after the clamp still wins: the reseed pays nothing against a moved viewport', () => {
    const { view, props, state, hook, rowTop, READ, writes, range } = setup({}, { clamp: true, scrollAfterReseed: -300 })
    const parkedAt = state.scrollTop
    act(() => { view.rerender({ ...props, heightScopeKey: COLD }) })
    expect(range.clamps).toBe(1)
    expect(range.inputApplied).toBe(true)
    // Re-basing to the clamp does not re-authorize a moved viewport: the live
    // scrollTop is the clamp plus the input, the guard drops the capture.
    expect(writes).toEqual([])
    expect(state.scrollTop).toBe(range.clampedTo - 300)
    expect(rowTop(READ)).not.toBe(40)
    expect(state.scrollTop).not.toBe(parkedAt)
    expect(hook.current!.offsetBefore).toBeGreaterThan(0)
  })

  it('a drop that is not the ceiling is not re-based: the engine moved the reader below it, the capture is dropped as before', () => {
    // Chromium with anchoring on moves scrollTop by the spacer delta at the
    // cold commit, which lands well below the ceiling (14662 against a 22083
    // ceiling for the same capture). That is not the range clamp, so the
    // stand-down leaves the capture alone and the freshness guard drops it,
    // exactly as on the frozen code: the engine that moved the viewport owns
    // the position and the consumer writes nothing.
    const drop = ROWS * EST - 400 - 50
    const { view, props, state, writes, range, coldCeiling } = setup({}, { clamp: true, coldDrop: drop })
    expect(drop).toBe(coldCeiling - 50)
    const parkedAt = state.scrollTop
    act(() => { view.rerender({ ...props, heightScopeKey: COLD }) })
    expect(range.dropApplied).toBe(true)
    expect(range.clamps).toBe(0)
    expect(writes).toEqual([])
    expect(state.scrollTop).toBe(drop)
    expect(state.scrollTop).not.toBe(parkedAt)
  })

  it('a shallow reader above the cold ceiling is paid exactly as before under the same engine', () => {
    const { view, props, state, hook, rowTop, start, READ, writes, range, coldCeiling } = setup({}, { clamp: true, read: 15 })
    const parkedAt = state.scrollTop
    expect(parkedAt).toBeLessThan(coldCeiling)
    act(() => { view.rerender({ ...props, heightScopeKey: COLD }) })
    expect(range.clamps).toBe(0)
    expect(hook.current!.offsetBefore).toBe(start * H)
    expect(rowTop(READ)).toBe(40)
    expect(state.scrollTop).toBe(parkedAt)
    expect(writes).toEqual([])
  })

  it('a FOLLOWED reader clamped by the cold document is re-pinned to the reseeded tail', () => {
    const { view, props, state, hook, range } = setup({}, { clamp: true })
    act(() => {
      state.scrollTop = hook.current!.totalHeight - state.clientHeight
      view.result.current.scrollerRef.current!.dispatchEvent(new Event('scroll'))
    })
    expect(view.result.current.getFollow()).toBe(true)
    act(() => { view.rerender({ ...props, heightScopeKey: COLD }) })
    expect(range.clamps).toBe(1)
    expect(view.result.current.getFollow()).toBe(true)
    expect(state.scrollTop).toBe(hook.current!.totalHeight - state.clientHeight)
  })

  it('a true session switch under the clamping engine still captures nothing', () => {
    const { view, props, state, start, writes, range } = setup({}, { clamp: true })
    const parkedAt = state.scrollTop
    const other = Array.from({ length: ROWS }, (_, i) => ({ id: `s2-m${i}` }))
    act(() => { view.rerender({ ...props, items: other, sessionId: 'cold-swap-2', heightScopeKey: 'cold-swap-2:tables1:w832' }) })
    expect(writes).not.toContain(parkedAt - start * (H - EST))
    expect(writes).not.toContain(parkedAt)
    expect(range.clamps).toBeGreaterThanOrEqual(0)
    expect(persisted('cold-swap-2:tables1:w832', 's2-m0')).toBeUndefined()
  })
})
