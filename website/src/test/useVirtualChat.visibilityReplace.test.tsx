// Feature: chat-virtualizer — re-place the transcript on a DISTURBED mobile tab
// return, and leave an undisturbed return alone.
//
// The slot-entry placement (restore the saved anchor, else force-pin to the
// bottom) ran ONLY on a session change or first mount. A mobile visibility
// return keeps the SAME mounted sessionId, so none of it re-ran — yet while the
// tab was hidden the scroller got a zero-height layout, whose huge
// distance-from-bottom released `stick`, and the WebSocket heal path then
// rebuilt the rows under the still-mounted scroller. WebKit has no scroll
// anchoring to absorb that, so the transcript landed far back instead of at the
// live end.
//
// The fix has two halves. On HIDE the hook flushes any pending debounced anchor
// save and snapshots `stick`, the geometry and (for a released reader) the top
// visible row. On RETURN it compares the live scroller against that snapshot
// and re-places ONLY when the hidden interval moved something: follow released,
// a follower off the live end, or scrollTop / clientHeight changed. An
// undisturbed return — the desktop tab switch — changes nothing. A collapsed
// (zero-height) box is also refused by BOTH automatic-pin sites, so the release
// cannot happen in the first place.
//
// Harness matches useVirtualChat.anchorRestore.test.tsx: a detached scroller
// with controllable geometry, synchronous rAF, fake timers, layout-effect-driven
// assertions.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'

import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'
import {
  HEIGHT_SCHEMA_VERSION,
  SCHEMA_VERSION_KEY,
} from '../hooks/virtualizer/HeightCache'
import { loadScrollAnchor, saveScrollAnchor } from '../hooks/virtualizer/ScrollAnchorCache'

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
  const scrollTo = vi.fn((o: { top: number }) => { state.scrollTop = o.top })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = scrollTo
  return { el, state, scrollTo }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

/** Pre-measure every row at `h` px via the persisted HeightCache blob, so the
 *  restore's offset math is exact (100px * index) rather than estimate-driven. */
function seedHeights(sessionId: string, n: number, h: number) {
  const blob: Record<string, number | string> = { [SCHEMA_VERSION_KEY]: HEIGHT_SCHEMA_VERSION }
  for (let i = 0; i < n; i++) blob[`m${i}`] = h
  localStorage.setItem(`vc_heights_${sessionId}`, JSON.stringify(blob))
}

/** Flip jsdom's document.hidden and fire the visibilitychange the hook listens
 *  for — same mechanism as slotReadRelay.test.tsx. */
function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { value: hidden, configurable: true })
  document.dispatchEvent(new Event('visibilitychange'))
}

type View = ReturnType<typeof renderHook<ReturnType<typeof useVirtualChat<Item>>, UseVirtualChatOptions<Item>>>

function mount(sessionId: string, geom: Geom, items: Item[], extra: Partial<UseVirtualChatOptions<Item>> = {}) {
  const { el, state, scrollTo } = makeScroller(geom)
  const ref: RefObject<HTMLDivElement | null> = { current: el }
  const view: View = renderHook(
    (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
    { initialProps: { items, sessionId, getKey, externalScrollerRef: ref, ...extra } },
  )
  return { el, state, view, ref, scrollTo }
}

/** Register a DOM node for each row whose viewport rect is derived from the
 *  live scrollTop, mimicking real layout: rowTop(i) = i*100 - scrollTop. The
 *  hide snapshot's anchor capture reads these rects. */
function attachRows(view: View, state: Geom, indices: number[]) {
  for (const i of indices) {
    const node = document.createElement('div')
    Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 100 })
    node.getBoundingClientRect = () =>
      ({ top: i * 100 - state.scrollTop, bottom: i * 100 - state.scrollTop + 100, height: 100 } as DOMRect)
    act(() => { view.result.current.measureRef(i)(node) })
  }
}

function useHarness() {
  let origRaf: typeof requestAnimationFrame
  beforeEach(() => {
    localStorage.clear()
    // Synchronous rAF: the slot-entry settle frames run inline (they self-disable
    // on the detached scroller — isConnected is false — so the offset-math /
    // force-pin write is what the assertions see).
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    vi.useFakeTimers()
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
  })
  afterEach(() => {
    vi.useRealTimers()
    globalThis.requestAnimationFrame = origRaf
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
  })
}

describe('useVirtualChat: visibility return re-places a DISTURBED follower', () => {
  useHarness()

  it('force-pins when follow was released while hidden', () => {
    const { el, state, view } = mount(
      'sess-vis-released',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    expect(el.scrollTop).toBe(4600)
    expect(view.result.current.getFollow()).toBe(true)

    // Hide with follow engaged at the live end. While hidden, something
    // releases follow and leaves the position far back (the shape of the
    // zero-height release + heal-rebuild symptom, expressed here through the
    // scroll handler because both automatic-pin sites now refuse a collapsed
    // box).
    act(() => { setHidden(true) })
    act(() => {
      el.dispatchEvent(new Event('wheel'))
      state.scrollTop = 500
      el.dispatchEvent(new Event('scroll'))
    })
    expect(view.result.current.getFollow()).toBe(false)

    // The scroll-up also carried the mounted window into mid-history (in a
    // real engine the window follows the reader up; here the jump mounts it).
    act(() => { view.result.current.mountIndex(5) })
    const midWindow = view.result.current.virtualItems
    expect(midWindow[midWindow.length - 1].index).toBeLessThan(49)

    // The return sees follow released against a snapshot that had it engaged:
    // the live end is the position, so it re-pins and re-arms follow -- and it
    // remounts the TAIL window in the same commit, so the pin lands the live
    // turn rather than the bottom spacer with rows still to mount.
    act(() => { setHidden(false) })
    expect(el.scrollTop).toBe(4600)
    expect(view.result.current.getFollow()).toBe(true)
    const tail = view.result.current.virtualItems
    expect(tail[tail.length - 1].index).toBe(49)
  })

  it('force-pins when the position was clamped while hidden, ignoring a stale persisted anchor', () => {
    // Contract change: a reader FOLLOWING at hide returns to the live end even
    // when a persisted anchor exists — that anchor predates the position they
    // were actually at, and restoring it would yank them backwards.
    seedHeights('sess-vis-clamped', 50, 100)
    const { el, state } = mount(
      'sess-vis-clamped',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    expect(el.scrollTop).toBe(4600)
    saveScrollAnchor('sess-vis-clamped', { key: 'm20', top: -30 })

    act(() => { setHidden(true) })
    // The heal-path rebuild left the position far back; follow stayed armed.
    state.scrollTop = 1234
    act(() => { setHidden(false) })
    expect(el.scrollTop).toBe(4600)
  })

  it('force-pins a follower whose live end moved while the box was collapsed', () => {
    const { el, state, view } = mount(
      'sess-vis-grew',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    expect(el.scrollTop).toBe(4600)

    act(() => { setHidden(true) })
    // The browser collapses the scroller to zero height, then a streamed
    // append lands (its rerender fires pinAuto through the window recompute).
    // Without the collapsed-box guard, evaluateAutoPin's idle branch would read
    // the whole transcript as distance-from-bottom and release stick; with it,
    // pinAuto is a no-op and follow survives — but no pin was written either.
    act(() => {
      state.clientHeight = 0
      state.scrollHeight = 5400
      view.rerender({ items: mkItems(54), sessionId: 'sess-vis-grew', getKey, externalScrollerRef: { current: el } })
    })
    expect(view.result.current.getFollow()).toBe(true)
    expect(el.scrollTop).toBe(4600)

    // The box is back and the follower sits off the live end: re-pin.
    state.clientHeight = 400
    act(() => { setHidden(false) })
    expect(el.scrollTop).toBe(5000)
    expect(view.result.current.getFollow()).toBe(true)
  })
})

describe('useVirtualChat: an UNDISTURBED visibility return changes nothing', () => {
  useHarness()

  it('leaves a scrolled-up anchorless reader untouched (desktop tab switch)', () => {
    // A desktop reader who jumped to a search hit — a programmatic position the
    // intent gate deliberately never persists — and briefly switched tabs. No
    // anchor for THIS position exists; a stale one from an earlier visit does.
    // The return must not re-latch it, and must not pin.
    seedHeights('sess-vis-desktop', 50, 100)
    const { el, state, view, scrollTo } = mount(
      'sess-vis-desktop',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    attachRows(view, state, [4, 5, 6, 7, 8])
    act(() => {
      state.scrollTop = 590 // the jump: a scroll event with no hardware input
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) }) // the debounced save ran (and skipped: no intent)
    expect(view.result.current.getFollow()).toBe(false)
    saveScrollAnchor('sess-vis-desktop', { key: 'm20', top: -30 })
    const writesBefore = scrollTo.mock.calls.length

    act(() => { setHidden(true) })
    act(() => { setHidden(false) })
    expect(el.scrollTop).toBe(590)
    expect(scrollTo.mock.calls.length).toBe(writesBefore)
    expect(view.result.current.restoreGate).toBe(false)
    // The stale persisted anchor was not consumed either.
    expect(loadScrollAnchor('sess-vis-desktop')).toEqual({ key: 'm20', top: -30 })
  })

  it('leaves a follower parked at the live end untouched', () => {
    const { el, view, scrollTo } = mount(
      'sess-vis-parked',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    expect(el.scrollTop).toBe(4600)
    const writesBefore = scrollTo.mock.calls.length

    act(() => { setHidden(true) })
    act(() => { setHidden(false) })
    expect(el.scrollTop).toBe(4600)
    expect(scrollTo.mock.calls.length).toBe(writesBefore)
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('never applies one session\'s hide snapshot to another session shown on return', () => {
    // Follower on session A hides; while hidden the pane switches to session B
    // and B's own slot-entry placement puts a released reader mid-history with
    // a saved anchor of its own. On return the listener finds A's snapshot,
    // which says "follower at the live end". Applying it would pin B's reader
    // to B's end. The guard compares the snapshot's session against the LIVE
    // session, so the snapshot is discarded and B is left exactly as B's own
    // entry placement left it.
    seedHeights('sess-vis-b', 50, 100)
    saveScrollAnchor('sess-vis-b', { key: 'm20', top: -30 })
    const { el, state, view, scrollTo } = mount(
      'sess-vis-a',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    expect(el.scrollTop).toBe(4600)
    expect(view.result.current.getFollow()).toBe(true)

    act(() => { setHidden(true) })
    // Session switch while hidden: B restores its anchor (m20 at -30 -> 2030).
    attachRows(view, state, [18, 19, 20, 21, 22, 23])
    act(() => {
      view.rerender({ items: mkItems(50), sessionId: 'sess-vis-b', getKey, externalScrollerRef: { current: el } })
    })
    act(() => { vi.advanceTimersByTime(50) })
    const bPosition = el.scrollTop
    expect(bPosition).not.toBe(4600)
    expect(view.result.current.getFollow()).toBe(false)
    const writesBefore = scrollTo.mock.calls.length

    act(() => { setHidden(false) })
    expect(el.scrollTop).toBe(bPosition)
    expect(scrollTo.mock.calls.length).toBe(writesBefore)
    expect(view.result.current.getFollow()).toBe(false)
  })

  it('does nothing on a visibilitychange with no preceding hidden phase', () => {
    const { el, state } = mount(
      'sess-vis-noop',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    expect(el.scrollTop).toBe(4600)

    // The reader scrolled up and stayed there. A stray visibilitychange that is
    // NOT a hidden -> visible transition (the tab was already visible) must not
    // re-place them.
    act(() => {
      state.scrollTop = 500
      setHidden(false) // still visible; no hidden phase happened
    })
    expect(el.scrollTop).toBe(500)
  })
})

describe('useVirtualChat: the hide snapshot captures the live position', () => {
  useHarness()

  it('flushes a debounced anchor save synchronously when the tab goes hidden', () => {
    const { el, state, view } = mount(
      'sess-vis-flush',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    attachRows(view, state, [4, 5, 6, 7, 8])

    // The reader scrolls up (hardware intent), then the tab goes hidden BEFORE
    // the 200ms save timer fires. Mirroring the slot-switch LEAVE flush, the
    // hide writes what the timer was about to, against the still-intact box.
    act(() => {
      el.dispatchEvent(new Event('wheel'))
      state.scrollTop = 590
      el.dispatchEvent(new Event('scroll'))
    })
    expect(loadScrollAnchor('sess-vis-flush')).toBeNull()
    act(() => { setHidden(true) })
    expect(loadScrollAnchor('sess-vis-flush')).toEqual({ key: 'm5', top: -90 })
    // The cancelled timer does not fire later against a collapsed box.
    state.clientHeight = 0
    act(() => { vi.advanceTimersByTime(500) })
    expect(loadScrollAnchor('sess-vis-flush')).toEqual({ key: 'm5', top: -90 })
  })

  it('restores the HIDE-TIME position on a disturbed return, not a stale persisted anchor', () => {
    seedHeights('sess-vis-hidepos', 50, 100)
    const { el, state, view } = mount(
      'sess-vis-hidepos',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    attachRows(view, state, [4, 5, 6, 7, 8])
    // A stale anchor from an earlier visit (written after entry so the entry
    // latch did not consume it).
    saveScrollAnchor('sess-vis-hidepos', { key: 'm20', top: -30 })
    // The reader jumps to row 5 programmatically — a position the intent gate
    // never persists, so storage still says m20.
    act(() => {
      state.scrollTop = 590
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) })
    expect(loadScrollAnchor('sess-vis-hidepos')).toEqual({ key: 'm20', top: -30 })

    // Hide snapshots {m5, -90} from the live box. While hidden the rebuild
    // clamps the position; the return restores the hide-time row, not m20.
    act(() => { setHidden(true) })
    state.scrollTop = 100
    act(() => { setHidden(false) })
    // offsetOf(m5) = 500, row top 90px above the viewport top -> 590.
    expect(el.scrollTop).toBe(590)
    expect(view.result.current.getFollow()).toBe(false)
  })

  it('a disturbed return never raises restoreGate (no skeleton over an already-positioned transcript)', () => {
    // The caller hides the transcript behind a skeleton while `restoreGate` is
    // up. That is right for a slot ENTRY, whose rows are still assembling; on a
    // return the rows are mounted and positioned and only scrollTop moves, so
    // a raised gate would blank the reader's content until settle. Record the
    // gate on every render across the whole return.
    seedHeights('sess-vis-gate', 50, 100)
    const gates: boolean[] = []
    const { el, state, view } = mount(
      'sess-vis-gate',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    attachRows(view, state, [4, 5, 6, 7, 8])
    act(() => {
      state.scrollTop = 590
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) })
    expect(view.result.current.restoreGate).toBe(false)

    act(() => { setHidden(true) })
    state.scrollTop = 100 // clamped while hidden -> disturbed
    act(() => {
      setHidden(false)
      gates.push(view.result.current.restoreGate)
    })
    gates.push(view.result.current.restoreGate)
    act(() => { vi.advanceTimersByTime(700) })
    gates.push(view.result.current.restoreGate)

    expect(el.scrollTop).toBe(590)
    expect(gates).toEqual([false, false, false])
  })
})

describe('useVirtualChat: collapsed-box guard covers both automatic-pin sites', () => {
  useHarness()

  it('pre-paint height-sync re-pin refuses a zero-height box', () => {
    // A scroller mounted collapsed (a display:none pane). The slot-entry pin
    // already wrote against the collapsed box (bottomTarget = scrollHeight), so
    // the pre-paint path's viewport-shrink guard does not apply: without the
    // shared predicate, the reprice commit would re-target the "bottom" at
    // scrollHeight again — a write the browser clamps the moment the pane
    // shows, which the scroll handler then reads as a user scroll.
    const { el, state, view } = mount(
      'sess-collapsed-prepaint',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 0 },
      mkItems(30),
    )
    const after = el.scrollTop
    expect(view.result.current.getFollow()).toBe(true)

    // Seed one measurement, which schedules the debounced height sync; the
    // repricing it commits grows the content (farm-measured truth replacing
    // estimates) — the trigger of the pre-paint stick branch.
    const node = document.createElement('div')
    Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 250 })
    node.getBoundingClientRect = () =>
      ({ top: 100, bottom: 350, left: 0, right: 390, width: 390, height: 250, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect
    act(() => { view.result.current.measureRef(5)(node) })
    state.scrollHeight = 9000
    act(() => { vi.advanceTimersByTime(120) })

    expect(el.scrollTop).toBe(after)
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('post-paint pinAuto refuses a zero-height box without releasing follow', () => {
    const { el, state, view } = mount(
      'sess-collapsed-pinauto',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    expect(el.scrollTop).toBe(4600)

    // The box collapses and an append lands (its rerender fires pinAuto through
    // the window recompute). No release, no write.
    act(() => {
      state.clientHeight = 0
      state.scrollHeight = 5100
      view.rerender({ items: mkItems(51), sessionId: 'sess-collapsed-pinauto', getKey, externalScrollerRef: { current: el } })
    })
    expect(view.result.current.getFollow()).toBe(true)
    expect(el.scrollTop).toBe(4600)
  })
})
