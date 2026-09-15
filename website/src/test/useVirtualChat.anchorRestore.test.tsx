// Feature: chat-virtualizer — reading-position save/restore (issue #2774).
//
// Switching away from a session and back used to always pin to the bottom,
// losing the user's place in a long transcript. The fix persists a scroll
// ANCHOR (topmost visible row's key + viewport offset — never a raw
// scrollTop, which is meaningless before rows are measured) on scroll-settle,
// and restores it on slot entry instead of the unconditional bottom pin.
//
// Harness matches useVirtualChat.integration.test.tsx: a detached scroller
// with controllable geometry, layout-effect-driven assertions. The restore's
// initial write is offset math (synchronous, pre-paint), so it is
// deterministic in jsdom; the DOM settle frames guard on degenerate rects and
// a disconnected scroller, so they self-disable here.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatOptions } from '../hooks/virtualizer/types'
import {
  ANCHOR_KEY_PREFIX,
  saveScrollAnchor,
  loadScrollAnchor,
} from '../hooks/virtualizer/ScrollAnchorCache'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

/** Comfortably past the hook's RESTORE_HYDRATE_WAIT_MS. Real time rather than
 *  fake timers: the deadline is read from `performance.now()`, which the fake
 *  clock does not necessarily own, and a test that silently never advances the
 *  clock it thinks it advanced is worse than one that takes a second. */
const RESTORE_WAIT_PROBE_MS = 1350

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
  return { el, state }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

/** ChatPage's real `getKey` prices a row against the deduped `rowKeys` of ONE
 *  render, so the same message answers to a different key once its neighbours
 *  change. This models that: the key carries the row's index. */
const positionalGetKey = (it: Item, i: number) => `${it.id}#${i}`
/** ChatPage's `getStableId` -- the row's tail message, index-free. */
const stableId = (it: Item) => `a-${it.id}`

/** Pre-measure every row at `h` px via the persisted HeightCache blob, so the
 *  restore's offset math is exact (100px * index) rather than estimate-driven. */
function seedHeights(sessionId: string, n: number, h: number) {
  const blob: Record<string, number> = {}
  for (let i = 0; i < n; i++) blob[`m${i}`] = h
  localStorage.setItem(`vc_heights_${sessionId}`, JSON.stringify(blob))
}

function mountWith(
  sessionId: string,
  geom: Geom,
  items: Item[],
  extra: Partial<UseVirtualChatOptions<Item>>,
) {
  const { el, state } = makeScroller(geom)
  const ref: RefObject<HTMLDivElement | null> = { current: el }
  const view = renderHook(
    (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
    { initialProps: { items, sessionId, getKey, externalScrollerRef: ref, ...extra } },
  )
  return { el, state, view, ref }
}

function mount(sessionId: string, geom: Geom, items: Item[]) {
  const { el, state } = makeScroller(geom)
  const ref: RefObject<HTMLDivElement | null> = { current: el }
  const view = renderHook(
    (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
    { initialProps: { items, sessionId, getKey, externalScrollerRef: ref } },
  )
  return { el, state, view, ref }
}

describe('useVirtualChat: reading-position restore on slot entry', () => {
  let origRaf: typeof requestAnimationFrame
  beforeEach(() => {
    localStorage.clear()
    // Synchronous rAF: the bottom-pin settle and the restore settle frames run
    // inline (the latter self-disable on the detached scroller — isConnected
    // is false — so the offset-math write is what the assertions see).
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
  })
  afterEach(() => {
    globalThis.requestAnimationFrame = origRaf
  })

  it('restores the saved anchor instead of pinning to the bottom (first mount)', () => {
    seedHeights('sess-r', 50, 100)
    saveScrollAnchor('sess-r', { key: 'm10', top: 24 })
    const { el, view } = mount(
      'sess-r',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    // offsetOf(m10) = 10 * 100 = 1000; the row's top sits 24px below the
    // viewport top → scrollTop = 976. The bottom pin would be 4600.
    expect(el.scrollTop).toBe(976)
    // The mounted window is around the anchor, not the tail.
    const indices = view.result.current.virtualItems.map((v) => v.index)
    expect(indices).toContain(10)
    expect(indices).not.toContain(49)
  })

  it('restores on a slot SWITCH into a session with a saved anchor', () => {
    seedHeights('sess-b', 50, 100)
    saveScrollAnchor('sess-b', { key: 'm20', top: -30 })
    const { el, state, view } = mount(
      'sess-a',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    // Session A has no anchor: slot entry pinned to the bottom.
    expect(el.scrollTop).toBe(4600)

    act(() => {
      state.scrollTop = 4600
      view.rerender({ items: mkItems(50), sessionId: 'sess-b', getKey, externalScrollerRef: { current: el } })
    })
    // offsetOf(m20) = 2000; row top 30px ABOVE the viewport top → 2030.
    expect(el.scrollTop).toBe(2030)
  })

  it('a streaming append while restored does NOT yank to the bottom', () => {
    seedHeights('sess-y', 50, 100)
    saveScrollAnchor('sess-y', { key: 'm10', top: 24 })
    const { el, state, view } = mount(
      'sess-y',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    expect(el.scrollTop).toBe(976)

    act(() => {
      state.scrollHeight = 5100
      view.rerender({ items: mkItems(51), sessionId: 'sess-y', getKey, externalScrollerRef: { current: el } })
    })
    // Follow is released while restored mid-history: no pull to the bottom.
    expect(el.scrollTop).toBe(976)
    expect(view.result.current.isAtBottom).toBe(false)
  })

  it('falls back to the bottom pin when the anchored row no longer exists', async () => {
    seedHeights('sess-gone', 50, 100)
    saveScrollAnchor('sess-gone', { key: 'deleted-row', top: 0 })
    const { el, state, view } = mount(
      'sess-gone',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    // The fallback is now DEFERRED, not immediate: a transcript hydrates in
    // chunks, so "absent on this commit" no longer means "gone". The anchor is
    // held (and the caller covers the transcript) until the row lands or the
    // wait expires -- so nothing is placed yet here, and the gate is up.
    expect(el.scrollTop).toBe(0)
    expect(view.result.current.restoreGate).toBe(true)
    // Past the wait the row is genuinely not coming, and the reader must not be
    // stranded on a transcript nobody positioned. This is the guarantee the test
    // has always made; only its timing moved.
    await act(async () => { await new Promise((r) => setTimeout(r, RESTORE_WAIT_PROBE_MS)) })
    expect(view.result.current.restoreGate).toBe(false)
    expect(el.scrollTop).toBe(4600)
    // ...and follow was re-armed: an append pins to the new bottom.
    act(() => {
      state.scrollHeight = 5100
      view.rerender({ items: mkItems(51), sessionId: 'sess-gone', getKey, externalScrollerRef: { current: el } })
    })
    expect(el.scrollTop).toBe(4700)
  })

  it('resolves a persisted anchor by the stable id, not the per-render key', () => {
    // The defect this pins: the anchor was captured and resolved through
    // `getKey`, which is priced per render. Re-entering the slot with a
    // different window renames every row, so the lookup missed on all of them
    // and the restore silently degraded to the bottom pin -- reported from a
    // phone as "switching sessions always lands at the bottom", with the
    // position erased on the way (the ensuing at-bottom save clears it).
    seedHeights('sess-vocab', 50, 100)
    saveScrollAnchor('sess-vocab', { key: 'a-m20', top: -30 })
    const { el } = mountWith(
      'sess-vocab',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
      { getKey: positionalGetKey, getStableId: stableId },
    )
    // offsetOf(m20) = 2000, row top 30px above the viewport top -> 2030.
    // Resolving through positionalGetKey yields 'm20#20' for that row and can
    // never match, which would leave the bottom pin at 4600.
    expect(el.scrollTop).toBe(2030)
  })

  it('still resolves through getKey for a caller that supplies no stable id', () => {
    // The fallback is not decoration: a consumer outside ChatPage passes only
    // getKey, and it must keep anchoring rather than lose it.
    seedHeights('sess-nostable', 50, 100)
    saveScrollAnchor('sess-nostable', { key: 'm20', top: -30 })
    const { el } = mount(
      'sess-nostable',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    expect(el.scrollTop).toBe(2030)
  })

  it('holds the anchor while the transcript hydrates, then restores when the row lands', async () => {
    // THE defect this pins. Entry commits carrying a PARTIAL transcript (measured
    // on a phone: 6 rows, then 17 a moment later) used to consume the anchor on
    // the first of them, so the lookup missed every time and the miss cleared the
    // anchor on its way out -- "switching sessions always lands at the bottom",
    // with the position erased so it could never work on a later try either.
    seedHeights('sess-chunk', 50, 100)
    saveScrollAnchor('sess-chunk', { key: 'm20', top: -30 })
    const { el, state, view } = mount(
      'sess-chunk',
      { scrollTop: 0, scrollHeight: 600, clientHeight: 400 },
      mkItems(6), // first hydration chunk: m0..m5, the anchored row is NOT here
    )
    // Neither restored nor pinned: held, with the gate up for the caller's cover.
    expect(view.result.current.restoreGate).toBe(true)
    expect(el.scrollTop).toBe(0)

    // The rest of the transcript lands.
    act(() => {
      state.scrollHeight = 5000
      view.rerender({ items: mkItems(50), sessionId: 'sess-chunk', getKey, externalScrollerRef: { current: el } })
    })
    // offsetOf(m20) = 2000, row top 30px above the viewport top -> 2030.
    expect(el.scrollTop).toBe(2030)
    expect(view.result.current.restoreGate).toBe(false)
  })

  it('pins to the bottom as before when no anchor is saved', () => {
    const { el } = mount(
      'sess-none',
      { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 },
      mkItems(5),
    )
    expect(el.scrollTop).toBe(1600)
  })
})

describe('useVirtualChat: reading-position save on scroll settle', () => {
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

  /** Register a DOM node for row `i` whose viewport rect is derived from the
   *  live scrollTop, mimicking real layout: rowTop(i) = i*100 - scrollTop. */
  function attachRows(
    view: { result: { current: { measureRef: (i: number) => (el: HTMLElement | null) => void } } },
    state: Geom,
    indices: number[],
  ) {
    for (const i of indices) {
      const node = document.createElement('div')
      Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 100 })
      node.getBoundingClientRect = () =>
        ({ top: i * 100 - state.scrollTop, bottom: i * 100 - state.scrollTop + 100, height: 100 } as DOMRect)
      act(() => { view.result.current.measureRef(i)(node) })
    }
  }

  it('persists the topmost visible row + offset after a user scroll settles', () => {
    const { el, state, view } = mount(
      'sess-save',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    attachRows(view, state, [4, 5, 6, 7, 8])

    // User scrolls up to read history: row 5 (top = 500-590 = -90) is the
    // topmost row still intersecting the viewport; row 4 ends above it.
    act(() => {
      el.dispatchEvent(new Event('wheel'))
      state.scrollTop = 590
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) })

    expect(loadScrollAnchor('sess-save')).toEqual({ key: 'm5', top: -90 })
  })

  it('clears the anchor once the user returns to the bottom', () => {
    const { el, state, view } = mount(
      'sess-clear',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    attachRows(view, state, [4, 5, 6])
    act(() => {
      el.dispatchEvent(new Event('wheel'))
      state.scrollTop = 590
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) })
    expect(localStorage.getItem(`${ANCHOR_KEY_PREFIX}sess-clear`)).not.toBeNull()

    act(() => {
      state.scrollTop = 4600 // exactly at the bottom
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) })
    expect(localStorage.getItem(`${ANCHOR_KEY_PREFIX}sess-clear`)).toBeNull()
  })

  it('flushes a pending save when the slot switches inside the debounce window', () => {
    const { el, state, view } = mount(
      'sess-flush',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    attachRows(view, state, [4, 5, 6, 7, 8])

    // Scroll up, then switch sessions BEFORE the 200ms save timer fires.
    act(() => {
      state.scrollTop = 590
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => {
      view.rerender({ items: mkItems(10), sessionId: 'sess-other', getKey, externalScrollerRef: { current: el } })
    })

    // The switch flushed the outgoing session's position synchronously.
    expect(loadScrollAnchor('sess-flush')).toEqual({ key: 'm5', top: -90 })
    // The cancelled timer must not fire later against the new session.
    act(() => { vi.advanceTimersByTime(500) })
    expect(loadScrollAnchor('sess-flush')).toEqual({ key: 'm5', top: -90 })
  })

  it('flush at switch clears the outgoing anchor when leaving at the bottom', () => {
    const { el, state, view } = mount(
      'sess-flush-bottom',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    attachRows(view, state, [4, 5, 6])
    // A stale anchor exists from an earlier visit (written after mount so the
    // entry latch did not consume it).
    act(() => { saveScrollAnchor('sess-flush-bottom', { key: 'm5', top: -90 }) })

    // User scrolls (lands within the bottom threshold), then switches before
    // the timer fires: the flush must CLEAR the stale anchor, because the
    // user left this session at the bottom.
    act(() => {
      state.scrollTop = 4550
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => {
      view.rerender({ items: mkItems(10), sessionId: 'sess-other-2', getKey, externalScrollerRef: { current: el } })
    })
    expect(loadScrollAnchor('sess-flush-bottom')).toBeNull()
  })

  it('persists a save whose ONLY change is the alt identity', () => {
    // The two ends fail in opposite cases, which is why the anchor carries both. An
    // older-page prepend regroups a row and renames its LEAD while the tail id and the
    // offset stay put -- so a de-dupe keyed on tail+offset swallows that write, the
    // stored `alt` goes stale, and a later append renames the tail too. Then NEITHER
    // identity resolves and entry falls back to the bottom pin, defeating the point of
    // carrying two. Both de-dupe layers ask `anchorWriteChangesState`, which reads alt.
    let lead = 'l-a'
    const { el, state, view, ref } = mountWith(
      'sess-alt',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
      { getAltId: () => lead },
    )
    attachRows(view, state, [4, 5, 6, 7, 8])

    act(() => {
      el.dispatchEvent(new Event('wheel'))
      state.scrollTop = 590
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) })
    expect(loadScrollAnchor('sess-alt')).toEqual({ key: 'm5', top: -90, alt: 'l-a' })

    // Same row, same offset, new lead id: the write must still land.
    lead = 'l-b'
    act(() => {
      view.rerender({
        items: mkItems(50), sessionId: 'sess-alt', getKey,
        externalScrollerRef: ref, getAltId: () => lead,
      })
      el.dispatchEvent(new Event('wheel'))
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) })

    expect(loadScrollAnchor('sess-alt')).toEqual({ key: 'm5', top: -90, alt: 'l-b' })
  })
})


describe('useVirtualChat: anchor saves require hard input', () => {
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

  it('a self-scroll displacement (no hardware input) never becomes the saved anchor', () => {
    const { el, state, view } = mount(
      'sess-selfmove',
      { scrollTop: 4600, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    const attach = (indices: number[]) => {
      for (const i of indices) {
        const node = document.createElement('div')
        Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 100 })
        node.getBoundingClientRect = () =>
          ({ top: i * 100 - state.scrollTop, bottom: i * 100 - state.scrollTop + 100, height: 100 } as DOMRect)
        act(() => { view.result.current.measureRef(i)(node) })
      }
    }
    attach([4, 5, 6, 7, 8])
    // The shape of the live defect: scrollTop jumps mid-transcript and a
    // scroll event fires -- but NO wheel/touch/key/scrollbar input exists.
    act(() => {
      state.scrollTop = 590
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) })
    // The displaced position was NOT persisted: a reload still lands at the
    // bottom instead of "opening at the top of the chat".
    expect(loadScrollAnchor('sess-selfmove')).toBeNull()
  })

  it('clearing at the bottom stays unconditional (no input needed)', () => {
    saveScrollAnchor('sess-uncond', { key: 'm5', top: -90 })
    const { el, state } = mount(
      'sess-uncond',
      { scrollTop: 4600, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    // A self-scroll settles at the bottom: the stale anchor is cleared even
    // though no hardware input happened -- clearing is the safe direction.
    act(() => {
      state.scrollTop = 4600
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) })
    expect(loadScrollAnchor('sess-uncond')).toBeNull()
  })
})


describe('useVirtualChat: switch flush honors follow (stick) over transient geometry', () => {
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

  it('switching away mid-follow never persists the pin-lag transient as an anchor', () => {
    const { el, state, view, ref } = mount(
      'sess-follow-a',
      { scrollTop: 4600, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    // Mounted row nodes so the flush's anchor capture has something to bind
    // (without them the capture returns null and nothing is ever saved --
    // which would make this test pass vacuously in both directions).
    for (const i of [44, 45, 46, 47, 48, 49]) {
      const node = document.createElement('div')
      Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 100 })
      node.getBoundingClientRect = () =>
        ({ top: i * 100 - state.scrollTop, bottom: i * 100 - state.scrollTop + 100, height: 100 } as DOMRect)
      act(() => { view.result.current.measureRef(i)(node) })
    }
    // Follow engaged, reader at the bottom; a scroll event arms the debounced
    // save (the pin's own writes fire scroll events too).
    act(() => {
      el.dispatchEvent(new Event('scroll'))
    })
    // Streamed growth lands; the pin has not caught up yet, so INSTANTANEOUS
    // geometry reads as "not at bottom". No event fires for this drift -- it
    // is the transient the switch will observe.
    state.scrollHeight = 5400
    // Switch away inside the debounce window: the flush must trust follow
    // (stick), clear the outgoing anchor, and persist nothing.
    act(() => {
      view.rerender({ items: mkItems(50), sessionId: 'sess-follow-b', getKey, externalScrollerRef: ref })
    })
    expect(loadScrollAnchor('sess-follow-a')).toBeNull()
  })
})

/**
 * Both writers of the persisted anchor have to ask whether a RESTORE owns the position,
 * not merely whether one is still pending. `pendingRestoreRef` goes false the moment the
 * anchored row is located -- before the scroller is written, and long before the settle
 * stops correcting -- so a site gated on it reads our own landing as the reader's chosen
 * position. A restore that lands at or near the end then CLEARED the anchor it had just
 * restored, and the next entry, finding none, opened at the bottom.
 *
 * The gesture revocation restoreAnchor performs does not cover this: the at-bottom CLEAR
 * runs ABOVE the intent gate, by design (see 'clearing at the bottom stays unconditional').
 *
 * Asserted against the source because the gap is a frame-ordering one -- the settle
 * corrects across rAF callbacks jsdom does not run -- so a behavioural test here would
 * pass against the defect.
 */
describe('a restore owns the position while it lands (source guard)', () => {
  const region = (src: string, from: string, to: string) => {
    const a = src.indexOf(from)
    expect(a).toBeGreaterThan(-1)
    const b = src.indexOf(to, a)
    expect(b).toBeGreaterThan(a)
    return src.slice(a, b)
  }

  it('gates the debounced save and the leave flush on restoreOwnsPosition', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const src = fs.readFileSync(
      path.resolve(__dirname, '../hooks/virtualizer/useVirtualChat.ts'),
      'utf8',
    )

    const save = region(src, 'const scheduleAnchorSave = useCallback(', 'captureTopAnchor, restoreOwnsPosition])')
    const leave = region(src, "devLog('LEAVE',", 'lastScrollCtxRef.current = null')

    // Both regions are the ones that write storage -- if this stops holding, the slices
    // above are pointing somewhere else and the rest of this test means nothing.
    expect(save).toContain('clearScrollAnchor(')
    expect(leave).toContain('clearScrollAnchor(')

    expect(save).toContain('restoreOwnsPosition()')
    expect(leave).toContain('!restoreOwnsPosition()')

    // The narrower ref must not be what either one consults.
    expect(save).not.toMatch(/if\s*\(\s*pendingRestoreRef\.current\s*\)\s*return/)
    expect(leave).not.toContain('pendingRestoreRef.current')
  })
})
