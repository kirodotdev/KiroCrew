/**
 * Height-sync anchor compensation.
 *
 * A debounced height sync re-prices the offset spacers (estimates replaced by
 * real measurements). Rows ABOVE the viewport re-pricing moves everything
 * below by the delta; Chrome's native scroll anchoring absorbs that shift but
 * iOS Safari has none, so a reader mid-transcript sees the content slide
 * (measured 13-25px right after a far jump on the pod, and a nondeterministic
 * 170-190px lurch when the anchor was consumed early by the window effect —
 * see heightAnchorPendingRef's doc in useVirtualChat.ts).
 *
 * Pins: syncHeightsNow captures the top visible row before bumping
 * heightVersion, and the dedicated heightVersion-keyed layout effect corrects
 * scrollTop by the row's screen-position delta after the commit.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state: Geom = { ...initial }
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { state.scrollTop = v },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { state.scrollTop = o.top }
  el.getBoundingClientRect = () => ({ top: 0, bottom: 400, left: 0, right: 390, width: 390, height: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  return { el, state }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

describe('useVirtualChat: height-sync spacer repricing keeps the top visible row anchored', () => {
  let origRaf: typeof requestAnimationFrame
  beforeEach(() => {
    localStorage.clear()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    globalThis.requestAnimationFrame = origRaf
  })

  it('compensates scrollTop by the anchor row shift when a debounced sync commits (reader mid-transcript)', () => {
    const { el, state } = makeScroller({ scrollTop: 1000, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(30)
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items, sessionId: 'anchor-sync', getKey, externalScrollerRef: ref, followOutput: false } },
    )

    // Mount a visible row whose screen position SHIFTS between the sync's
    // anchor capture (first rect read → top 100) and the compensation
    // effect's re-read after the commit (subsequent reads → top 130),
    // simulating the spacer above it re-pricing by +30px. jsdom has no real
    // layout, so the shift is expressed through the self-mutating mock.
    const node = document.createElement('div')
    Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 250 })
    // Read #1 is the seed measurement itself (fractional heights read the
    // rect), #2 is captureTopAnchorFrom inside the sync's anchor capture, #3
    // the candidate-collection pass (the capture stores a LIST of visible
    // fallback anchors, one more rect read per row), #4+ the compensation
    // effect.
    let reads = 0
    node.getBoundingClientRect = () => {
      reads += 1
      const top = reads <= 3 ? 100 : 130
      return { top, bottom: top + 250, left: 0, right: 390, width: 390, height: 250, x: 0, y: top, toJSON: () => ({}) } as DOMRect
    }
    act(() => { view.result.current.measureRef(5)(node) })

    // The seed above scheduled a debounced sync. Flush it: syncHeightsNow
    // captures {key:'m5', top:100}, commits the repricing, and the dedicated
    // layout effect re-reads top=130 → delta +30 → scrollTop corrected.
    const before = state.scrollTop
    act(() => { vi.advanceTimersByTime(120) })
    expect(state.scrollTop - before).toBe(30)
  })

  it('re-pins a FOLLOWED reader to the new bottom pre-paint when repricing grows the content', () => {
    // The measure farm reprices estimate-priced rows in background batches
    // (deep-idle ~5s after a session opens), which grows scrollHeight under a
    // reader parked at the bottom. The pre-paint re-pin in the height-anchor
    // consumer is what makes that invisible; without it the correction falls
    // through to the POST-paint pinAuto and the reader sees one displaced
    // frame per landing wave — "opens, then starts jumping a moment later".
    const { el, state } = makeScroller({ scrollTop: 4600, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(30)
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items, sessionId: 'anchor-sync-stick', getKey, externalScrollerRef: ref, followOutput: true } },
    )

    // Seed one measurement, which schedules the debounced sync; the repricing
    // it commits grows the content (farm-measured truth replacing estimates).
    const node = document.createElement('div')
    Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 250 })
    node.getBoundingClientRect = () =>
      ({ top: 100, bottom: 350, left: 0, right: 390, width: 390, height: 250, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect
    act(() => { view.result.current.measureRef(5)(node) })
    state.scrollHeight = 9000 // the reprice: content is now much taller

    act(() => { vi.advanceTimersByTime(120) })
    // Followed ⇒ the bottom is the position: scrollTop must sit at the NEW
    // bottom, written in the same commit that repriced (pre-paint).
    expect(state.scrollTop).toBe(9000 - 400)
  })

  it('does NOT re-pin a followed reader whose upward gesture is still in flight', () => {
    // A reader who has just started scrolling up is still `stick` for the few
    // ms until the scroll handler's rAF evaluates and releases follow. A
    // repricing batch committing inside that gap must not force them to the
    // bottom mid-gesture — reported as "I was reading mid-transcript and it
    // suddenly jumped to the end", which then mass-remounted the rows and
    // re-staged every diff (the "chips flash again" follow-on).
    const { el, state } = makeScroller({ scrollTop: 2000, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(30)
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items, sessionId: 'anchor-sync-gesture', getKey, externalScrollerRef: ref, followOutput: true } },
    )

    const node = document.createElement('div')
    Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 250 })
    node.getBoundingClientRect = () =>
      ({ top: 100, bottom: 350, left: 0, right: 390, width: 390, height: 250, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect
    act(() => { view.result.current.measureRef(5)(node) })
    // Real hardware input NOW: the gesture is in flight.
    act(() => { el.dispatchEvent(new Event('wheel')) })
    state.scrollHeight = 9000
    const before = state.scrollTop

    act(() => { vi.advanceTimersByTime(120) }) // inside SCROLL_SETTLE_MS
    expect(state.scrollTop).toBe(before)
  })

  it('honours a LATE anchor when the viewport never moved (turn-end reprice)', () => {
    // A turn ending is the busiest the main thread gets, so this effect can run
    // long after the capture. A wall-clock staleness gate dropped the anchor
    // there and a still reader paid the whole reprice as one displacement. The
    // discriminator is scrollTop: unchanged ⇒ the delta is entirely the
    // reprice's, so correct it however late it lands.
    const { el, state } = makeScroller({ scrollTop: 1000, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(30)
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items, sessionId: 'anchor-late', getKey, externalScrollerRef: ref, followOutput: false } },
    )

    const node = document.createElement('div')
    Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 250 })
    let reads = 0
    node.getBoundingClientRect = () => {
      reads += 1
      const top = reads <= 3 ? 100 : 130
      return { top, bottom: top + 250, left: 0, right: 390, width: 390, height: 250, x: 0, y: top, toJSON: () => ({}) } as DOMRect
    }
    act(() => { view.result.current.measureRef(5)(node) })

    const before = state.scrollTop
    // Flush the debounced sync LATE — far beyond any wall-clock age gate.
    act(() => { vi.advanceTimersByTime(5000) })
    expect(state.scrollTop - before).toBe(30)
  })

  it('does not touch scrollTop when the anchor row does not move across the commit', () => {
    const { el, state } = makeScroller({ scrollTop: 1000, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(30)
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items, sessionId: 'anchor-sync-stable', getKey, externalScrollerRef: ref, followOutput: false } },
    )
    const node = document.createElement('div')
    Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 250 })
    node.getBoundingClientRect = () => ({ top: 100, bottom: 350, left: 0, right: 390, width: 390, height: 250, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect
    act(() => { view.result.current.measureRef(5)(node) })
    const before = state.scrollTop
    act(() => { vi.advanceTimersByTime(120) })
    expect(state.scrollTop).toBe(before)
  })

  // ---- Transient-row repricing (issue #6076) ----
  //
  // `getHeight` prices an UNMEASURED row from the running mean of the measured
  // ones, so a measurement is not local to its own row: it re-prices every row
  // that has never been measured. A "thinking" placeholder mounts, is measured
  // at a height nothing like a real message, and then leaves the list — and its
  // measurement keeps pricing the transcript after the row itself is gone, so
  // the height credited above the reader stays wrong for the rest of the
  // session. Compensating the commit cannot fix that; the measurement has to
  // stop counting when the row it belongs to leaves.

  /** A node reporting a fixed height through both measurement paths. */
  function nodeOf(height: number): HTMLElement {
    const node = document.createElement('div')
    Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => height })
    node.getBoundingClientRect = () => ({
      top: 0, bottom: height, left: 0, right: 390, width: 390, height, x: 0, y: 0, toJSON: () => ({}),
    }) as DOMRect
    return node
  }

  it('stops pricing unmeasured rows from a transient row once it leaves the list (pinned reader)', () => {
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      // followOutput: the reader is PINNED to the bottom, which is where a
      // streaming placeholder is actually watched from.
      { initialProps: { items: mkItems(30), sessionId: 'ghost-reprice', getKey, externalScrollerRef: ref, followOutput: true } },
    )

    // Two real messages measured at 300 → every unmeasured row is priced 300.
    act(() => { view.result.current.measureRef(0)(nodeOf(300)) })
    act(() => { view.result.current.measureRef(1)(nodeOf(300)) })
    act(() => { vi.advanceTimersByTime(200) })
    const settled = view.result.current.totalHeight
    expect(settled).toBeCloseTo(30 * 300, 1)

    // The ghost row arrives at the tail and is measured at nothing like a
    // message height.
    const withGhost = [...mkItems(30), { id: 'thinking' }]
    act(() => { view.rerender({ items: withGhost, sessionId: 'ghost-reprice', getKey, externalScrollerRef: ref, followOutput: true }) })
    act(() => { view.result.current.measureRef(30)(nodeOf(20)) })
    act(() => { vi.advanceTimersByTime(200) })
    // Not vacuous: its measurement really did drag the mean, and with it every
    // unmeasured row in the transcript.
    expect(view.result.current.totalHeight).toBeLessThan(settled - 1000)

    // The ghost leaves. Its 20px is no longer a fact about this transcript, so
    // the rows it was pricing must return to what the real measurements say.
    act(() => { view.rerender({ items: mkItems(30), sessionId: 'ghost-reprice', getKey, externalScrollerRef: ref, followOutput: true }) })
    act(() => { vi.advanceTimersByTime(200) })
    expect(view.result.current.totalHeight).toBeCloseTo(settled, 1)
  })

  // ---- Optimistic truncation that gets ROLLED BACK (issue #6076) -----------
  //
  // `handleRegenerate` and `handleEditResend` both snapshot the transcript,
  // truncate it, and dispatch the snapshot back when the server refuses the
  // press. The rows therefore leave the list and come back. Retiring their
  // measurements (out of the mean, still in the cache) is what makes the restore
  // exact; deleting them would price the restored rows from the mean, which is a
  // wrong spacer and a viewport jump for rows still off-window.

  it('restores a rolled-back row at its own measured height, not the mean', () => {
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const full = mkItems(30)
    const props = (items: Item[]): UseVirtualChatOptions<Item> => ({
      items, sessionId: 'rollback', getKey, externalScrollerRef: ref, followOutput: false,
    })
    const view = renderHook(
      (p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p),
      { initialProps: props(full) },
    )

    // Two ordinary messages set the mean, and one row far from them is measured
    // much taller -- that row is what the rollback has to bring back intact.
    act(() => { view.result.current.measureRef(0)(nodeOf(300)) })
    act(() => { view.result.current.measureRef(1)(nodeOf(300)) })
    act(() => { view.result.current.measureRef(25)(nodeOf(900)) })
    act(() => { vi.advanceTimersByTime(200) })

    const heightOf = (key: string): number | undefined => {
      act(() => { view.result.current.mountIndex(full.findIndex((i) => i.id === key)) })
      return view.result.current.virtualItems.find((v) => v.key === key)?.height
    }
    expect(heightOf('m25')).toBe(900)

    // The refused press: truncate to 20 rows...
    act(() => { view.rerender(props(full.slice(0, 20))) })
    act(() => { vi.advanceTimersByTime(200) })

    // ...and while the row is gone it does not price the rows that stayed. The
    // mean is the two real messages (300), so the 18 unmeasured rows are 300
    // each; with the departed row still counted it would be 500 each.
    expect(view.result.current.totalHeight).toBeCloseTo(300 + 300 + 18 * 300, 1)

    // Now the rollback puts the snapshot back.
    act(() => { view.rerender(props(full)) })
    act(() => { vi.advanceTimersByTime(200) })

    // Its own measurement survived the round trip -- the restored row is placed
    // at 900, not re-priced from the mean.
    expect(heightOf('m25')).toBe(900)
  })

  it('retires a swapped-out row even though the count never moved', () => {
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const props = (items: Item[]): UseVirtualChatOptions<Item> => ({
      items, sessionId: 'swap-retire', getKey, externalScrollerRef: ref, followOutput: false,
    })
    const withGhost = [...mkItems(29), { id: 'thinking' }]
    const view = renderHook(
      (p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p),
      { initialProps: props(withGhost) },
    )

    act(() => { view.result.current.measureRef(0)(nodeOf(300)) })
    act(() => { view.result.current.measureRef(1)(nodeOf(300)) })
    act(() => { view.result.current.measureRef(29)(nodeOf(20)) })
    act(() => { vi.advanceTimersByTime(200) })
    // The ghost is dragging the mean down, so the 27 unmeasured rows are priced
    // below what the real messages say.
    expect(view.result.current.totalHeight).toBeLessThan(300 + 300 + 20 + 27 * 300)

    // React batches the removal and the replacement into ONE commit, so the
    // count is unchanged: a net-count detector sees nothing while the ghost's
    // measurement is still pricing the transcript.
    act(() => { view.rerender(props([...mkItems(29), { id: 'output' }])) })
    act(() => { vi.advanceTimersByTime(200) })

    // The retirement lands in the offset tree in THAT SAME commit. It cannot be
    // left to the `offsetIndex` memo: an equal-count swap moves none of its
    // dependencies, so the memo body does not run and the spacers would keep the
    // prices the retirement just invalidated. No extra measurement, no later
    // sync -- the geometry is correct as soon as the swap renders.
    //
    // The mean is the two real messages, so every unmeasured row -- the 27 plus
    // the unmeasured replacement -- is priced at 300. With the ghost still
    // counted the mean would be 206.67 and the total ~6386.
    expect(view.result.current.totalHeight).toBeCloseTo(2 * 300 + 28 * 300, 1)
  })

  // ---- An INTERIOR replacement is a departure too (issue #6076) -------------
  //
  // The boundary is not where a replacement has to happen. An artifact card
  // refreshing in place, or a row re-keyed mid-transcript, replaces a row the
  // reader is looking PAST -- so a detector that only reads the last
  // pre-existing index sees nothing while the replaced row's measurement stays
  // in the mean that prices every unmeasured row.

  /** Measures rows 0, 1 at 300 and `tallIdx` at 900, then settles. */
  function seedMean(
    view: { result: { current: { measureRef: (i: number) => (el: HTMLElement) => void } } },
    tallIdx: number,
  ): void {
    act(() => { view.result.current.measureRef(0)(nodeOf(300)) })
    act(() => { view.result.current.measureRef(1)(nodeOf(300)) })
    act(() => { view.result.current.measureRef(tallIdx)(nodeOf(900)) })
    act(() => { vi.advanceTimersByTime(200) })
  }

  it('retires an INTERIOR row replaced at equal count', () => {
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const props = (items: Item[]): UseVirtualChatOptions<Item> => ({
      items, sessionId: 'interior-swap', getKey, externalScrollerRef: ref, followOutput: false,
    })
    const base = mkItems(30)
    const view = renderHook(
      (o: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(o),
      { initialProps: props(base) },
    )
    seedMean(view, 15)

    // Row 15 is replaced in place. The count is unchanged AND the last
    // pre-existing index still answers to its own key, so nothing at the
    // boundary moved -- only an interior position changed hands.
    const swapped = base.map((it, i) => (i === 15 ? { id: 'refreshed' } : it))
    act(() => { view.rerender(props(swapped)) })
    act(() => { vi.advanceTimersByTime(200) })
    // The replacement is what re-syncs the tree; the retirement rides that sync.
    act(() => { view.result.current.measureRef(2)(nodeOf(300)) })
    act(() => { vi.advanceTimersByTime(200) })

    // The mean is the three real messages, so the 27 unmeasured rows are 300
    // each. With the replaced row's 900 still counted the mean would be 450.
    expect(view.result.current.totalHeight).toBeCloseTo(3 * 300 + 27 * 300, 1)
  })

  it('retires an INTERIOR row replaced while another is appended', () => {
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const props = (items: Item[]): UseVirtualChatOptions<Item> => ({
      items, sessionId: 'interior-swap-grow', getKey, externalScrollerRef: ref, followOutput: false,
    })
    const base = mkItems(30)
    const view = renderHook(
      (o: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(o),
      { initialProps: props(base) },
    )
    seedMean(view, 15)

    // The count GROWS here, and the last pre-existing index still holds its key,
    // so the commit reads as a plain tail append -- while an interior row was
    // replaced underneath it.
    const swapped = base.map((it, i) => (i === 15 ? { id: 'refreshed' } : it))
    act(() => { view.rerender(props([...swapped, { id: 'appended' }])) })
    act(() => { vi.advanceTimersByTime(200) })

    // 31 rows: 2 measured at 300, 29 unmeasured at the restored mean of 300.
    expect(view.result.current.totalHeight).toBeCloseTo(2 * 300 + 29 * 300, 1)
  })

  it('retires nothing when a commit only rewrites one row in place', () => {
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const props = (items: Item[]): UseVirtualChatOptions<Item> => ({
      items, sessionId: 'token-append', getKey, externalScrollerRef: ref, followOutput: false,
    })
    const base = mkItems(30)
    const view = renderHook(
      (o: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(o),
      { initialProps: props(base) },
    )
    seedMean(view, 15)
    const settled = view.result.current.totalHeight

    // The shape of a streaming token: a new array, every element object reused
    // except the streaming row's, and its KEY unchanged. No key departed, so no
    // measurement may be retired -- the mean must not move.
    const streamed = [...base]
    streamed[29] = { id: base[29].id }
    act(() => { view.rerender(props(streamed)) })
    act(() => { vi.advanceTimersByTime(200) })

    expect(view.result.current.totalHeight).toBeCloseTo(settled, 1)
  })

  // ---- Departure detection under an INDEX-ADDRESSED getKey (#7207) ----------
  //
  // ChatPage's getKey resolves a per-render deduped key LIST by position, so it
  // only prices an item correctly when paired with the items of its OWN render.
  // The departure scan reads the PREVIOUS render's items, and an interior
  // removal shifts every key below it up by one -- so pricing those items with
  // THIS render's getKey reads the removed row's old index as the key of the row
  // that moved into it, which still survives. The departure is invisible and the
  // ghost keeps pricing the transcript.

  it('retires an interior removal when getKey is INDEX-ADDRESSED (ChatPage shape)', () => {
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    // The harness contract: the function ignores its item argument and closes
    // over the key list of the render it was created in.
    const props = (items: Item[]): UseVirtualChatOptions<Item> => {
      const keys = items.map((it) => it.id)
      return {
        items,
        sessionId: 'positional-departure',
        getKey: (_it: Item, i: number) => keys[i] ?? `oob-${i}`,
        externalScrollerRef: ref,
        followOutput: false,
      }
    }
    const base = mkItems(30)
    const view = renderHook(
      (o: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(o),
      { initialProps: props(base) },
    )
    seedMean(view, 5)

    // Row 5 leaves from the MIDDLE, so indices 5..28 are all re-keyed one step
    // up while index 0 keeps its key (head paging, which renames index 0, stays
    // excluded).
    act(() => { view.rerender(props(base.filter((_it, i) => i !== 5))) })
    act(() => { vi.advanceTimersByTime(200) })

    // The mean is the two real messages, so the 27 unmeasured rows are 300 each.
    // With the departed row's 900 still counted the mean is 500 and the total
    // 14100 -- which is what the current render's getKey produces here.
    expect(view.result.current.totalHeight).toBeCloseTo(2 * 300 + 27 * 300, 1)
  })

  // ---- The gate is DEPARTURE, not a count shape (#6076) ---------------------
  //
  // Retirement used to ride on the anchor's triggers, which require index 0 to
  // keep its key. Emptying the transcript renames index 0 exactly as paging out
  // the head does, so a wipe read as a page-out and every measurement stayed in
  // the mean -- pricing the rows of the NEXT conversation from the heights of
  // the one that was cleared. The two are separated by whether anything
  // survived, not by what happened to index 0.

  it('retires every height when the transcript is CLEARED in the same session', () => {
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const props = (items: Item[]): UseVirtualChatOptions<Item> => ({
      items, sessionId: 'cleared', getKey, externalScrollerRef: ref, followOutput: false,
    })
    const view = renderHook(
      (o: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(o),
      { initialProps: props(mkItems(30)) },
    )
    seedMean(view, 5)

    // `/clear` empties the list while the session stays put.
    act(() => { view.rerender(props([])) })
    act(() => { vi.advanceTimersByTime(200) })
    // Then the next conversation starts. Its row has never been measured, so it
    // is priced from whatever samples are still standing.
    act(() => { view.rerender(props([{ id: 'fresh0' }])) })
    act(() => { vi.advanceTimersByTime(200) })

    // Every sample belonged to the cleared transcript, so none of them prices
    // this row: it falls back to the flat estimate. With the wipe read as a
    // page-out the mean is 500 and this row is priced at that.
    expect(view.result.current.totalHeight).toBeCloseTo(80, 1)
  })

  it('retires a row that a PREPEND regroups away, which grows the count', () => {
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const props = (items: Item[]): UseVirtualChatOptions<Item> => ({
      items, sessionId: 'prepend-regroup', getKey, externalScrollerRef: ref, followOutput: false,
    })
    const base = mkItems(30)
    const view = renderHook(
      (o: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(o),
      { initialProps: props(base) },
    )
    // The mean is set by two rows that SURVIVE, and the head row -- the one the
    // regroup takes away -- is the measured outlier, so the mean moves only if
    // its height is actually retired.
    act(() => { view.result.current.measureRef(5)(nodeOf(300)) })
    act(() => { view.result.current.measureRef(6)(nodeOf(300)) })
    act(() => { view.result.current.measureRef(0)(nodeOf(900)) })
    act(() => { vi.advanceTimersByTime(200) })

    // Loading older history prepends rows AND regroups the top turn: index 0's
    // row joins the turn above under a new lead key, so it departs while the
    // count GROWS. It drops a contiguous prefix and leaves survivors standing --
    // head paging's other two properties -- but it is not coming back.
    const older = mkItems(10).map((it) => ({ id: `older-${it.id}` }))
    act(() => { view.rerender(props([...older, { id: 'regrouped-lead' }, ...base.slice(1)])) })
    act(() => { vi.advanceTimersByTime(200) })

    // 40 rows: the 2 surviving measurements at 300, and 38 unmeasured priced from
    // their mean. With the regrouped row's 900 still counted the mean is 500 and
    // the total 19600.
    expect(view.result.current.totalHeight).toBeCloseTo(2 * 300 + 38 * 300, 1)
  })

  it('retires a filtered-out prefix when the consumer cannot page at all', () => {
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    // The artifacts gallery's shape: no `onTopReached`, so nothing pages, and the
    // item list is a SEARCH RESULT. Narrowing the box drops a leading run of
    // cards and keeps later ones -- head paging's every shape property -- but
    // those cards are gone, not scrolled past.
    const props = (items: Item[]): UseVirtualChatOptions<Item> => ({
      items, sessionId: 'gallery-filter', getKey, externalScrollerRef: ref, followOutput: false,
    })
    const base = mkItems(30)
    const view = renderHook(
      (o: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(o),
      { initialProps: props(base) },
    )
    // The measured cards are all in the run the filter removes, so their heights
    // are the only thing that can still be pricing the survivors afterwards.
    seedMean(view, 5)

    act(() => { view.rerender(props(base.slice(10))) })
    act(() => { vi.advanceTimersByTime(200) })

    // Every sample belonged to a filtered-out card, so the 20 survivors fall back
    // to the flat estimate. Treated as a page-out the mean stays 500 (10000).
    expect(view.result.current.totalHeight).toBeCloseTo(20 * 80, 1)
  })

  it('keeps measurements when the head is PAGED OUT, which is not a departure', () => {
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    // `onTopReached` is what makes this consumer a paging one, and the exemption
    // is gated on it -- a consumer that cannot page has no page-out to exempt.
    const props = (items: Item[]): UseVirtualChatOptions<Item> => ({
      items, sessionId: 'head-paged', getKey, externalScrollerRef: ref, followOutput: false,
      onTopReached: () => {},
    })
    const base = mkItems(30)
    const view = renderHook(
      (o: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(o),
      { initialProps: props(base) },
    )
    // The measured rows are all in the prefix that gets trimmed -- so if the
    // trim retired them, nothing would be left to price the rows that stayed.
    seedMean(view, 5)

    // Trimming the head drops a contiguous prefix and leaves the rest standing.
    // Those rows are coming back when the reader scrolls up, so their heights
    // must keep pricing the region above.
    act(() => { view.rerender(props(base.slice(10))) })
    act(() => { vi.advanceTimersByTime(200) })

    // 20 unmeasured rows at the retained mean of 500. Retiring the trimmed
    // prefix would drop every sample and price them at the flat estimate (1600).
    expect(view.result.current.totalHeight).toBeCloseTo(20 * 500, 1)
  })
})
