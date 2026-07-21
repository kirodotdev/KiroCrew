// Feature: chat-virtualizer — useVirtualChat composing-hook integration tests.
//
// The pure pieces (FollowController, WindowCalculator, HeightCache) are unit-
// tested in isolation. This suite covers the WIRING that those unit tests
// can't reach — the effects/refs that historically caused the follow/yank
// regressions: append-pin while followed, a user scroll-up releasing follow so
// a later append does NOT yank, and a slot switch force-pinning to the bottom.
//
// jsdom has no layout engine, so scrollTop/scrollHeight/clientHeight are faked
// on a controlled detached scroller element passed via `externalScrollerRef`.
// The follow logic reads `scrollerRef.current` + live geometry synchronously
// inside layout effects, so these assertions are deterministic — they do not
// depend on rAF, ResizeObserver, or IntersectionObserver timing. (ResizeObserver
// is intentionally undefined in the test env, so the RO auto-pin never fires and
// can't perturb the result.)

import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatOptions } from '../hooks/virtualizer/types'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

/** A detached div with controllable, mutable scroll geometry. */
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
  // forcePin/pinAuto write `el.scrollTop` directly; scrollToBottom may use
  // scrollTo — map it onto the same backing state for completeness.
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { state.scrollTop = o.top }
  return { el, state }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

function render(geom: Geom, items: Item[], sessionId: string) {
  const { el, state } = makeScroller(geom)
  const ref: RefObject<HTMLDivElement | null> = { current: el }
  const initialProps: UseVirtualChatOptions<Item> = {
    items,
    sessionId,
    getKey,
    externalScrollerRef: ref,
  }
  const view = renderHook(
    (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
    { initialProps },
  )
  return { el, state, view }
}

describe('useVirtualChat integration: follow / pin wiring', () => {
  beforeEach(() => localStorage.clear())

  it('pins to the new bottom when items append while followed', () => {
    // Mount at the bottom (content == viewport). Slot-entry forcePin lands at 0.
    const { el, state, view } = render(
      { scrollTop: 0, scrollHeight: 400, clientHeight: 400 },
      mkItems(5),
      'append-followed',
    )
    expect(el.scrollTop).toBe(0)

    // A new message arrives: content grows and the item count increases.
    act(() => {
      state.scrollHeight = 900
      view.rerender({ items: mkItems(6), sessionId: 'append-followed', getKey, externalScrollerRef: { current: el } })
    })

    // The append layout effect pinned to the new bottom (900 - 400).
    expect(el.scrollTop).toBe(500)
  })

  it('does NOT yank the user back to the bottom after a scroll-up, on a later append', () => {
    // Tall content, mounted at the bottom: forcePin → 2000 - 400 = 1600.
    const { el, state, view } = render(
      { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 },
      mkItems(5),
      'scrollup-release',
    )
    expect(el.scrollTop).toBe(1600)

    // User scrolls up to read history (well away from the bottom).
    // Dispatch scroll event so the passive scroll handler detects the user
    // scroll and releases stick (stick is now released ONLY by the scroll handler).
    act(() => { state.scrollTop = 600; el.dispatchEvent(new Event('scroll')) })

    // A new message appends. The race-proof guard in pinAuto reads the live
    // scrollTop, sees the user moved up (distance from bottom >> epsilon), and
    // releases follow instead of pinning.
    act(() => {
      state.scrollHeight = 2200
      view.rerender({ items: mkItems(6), sessionId: 'scrollup-release', getKey, externalScrollerRef: { current: el } })
    })

    // Position preserved — no yank back to 1800.
    expect(el.scrollTop).toBe(600)
  })

  it('force-pins to the bottom on slot switch even if the previous slot was scrolled up', () => {
    const { el, state, view } = render(
      { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 },
      mkItems(5),
      'slot-a',
    )
    expect(el.scrollTop).toBe(1600)

    // User scrolled up in slot A…
    act(() => { state.scrollTop = 600 })

    // …then switches to slot B. Slot entry deterministically force-pins to the
    // true bottom (does not inherit the previous slot's scroll position).
    act(() => {
      view.rerender({ items: mkItems(5), sessionId: 'slot-b', getKey, externalScrollerRef: { current: el } })
    })

    expect(el.scrollTop).toBe(1600)
  })

  it('pins to the bottom when items first arrive after a slot switch (async fetch lands)', () => {
    // Mount with sessionId 'A' but NO items yet — the slot switched but the
    // messages fetch hasn't resolved. forcePin runs against empty content
    // (scrollHeight === 0, target === 0), so scrollTop stays at 0.
    const { el, state, view } = render(
      { scrollTop: 0, scrollHeight: 0, clientHeight: 400 },
      [],
      'async-fetch',
    )
    expect(el.scrollTop).toBe(0)

    // Now the HTTP fetch resolves: items first appear, and (in real DOM)
    // scrollHeight grows past clientHeight. The slot-entry effect must
    // re-fire because itemCount transitioned 0 → 8 for the same sessionId.
    act(() => {
      state.scrollHeight = 1200
      view.rerender({ items: mkItems(8), sessionId: 'async-fetch', getKey, externalScrollerRef: { current: el } })
    })

    // forcePin landed at the new bottom instantly (1200 - 400 = 800) — no
    // smooth-scroll animation, no land-short on late widget settle.
    expect(el.scrollTop).toBe(800)
  })

  it('does NOT re-pin on later appends after the first content-arrival pin (streaming follow stays smooth)', () => {
    // Mount empty (slot just switched), then content arrives (initial pin),
    // then more items append (streaming). The slot-entry layout effect MUST
    // NOT fire forcePin on every subsequent append — otherwise the user
    // would be yanked back to the bottom on every streamed token. Appends
    // are pinAuto's responsibility (smooth follow + scroll-up release).
    const { el, state, view } = render(
      { scrollTop: 0, scrollHeight: 0, clientHeight: 400 },
      [],
      'no-repin-on-stream',
    )

    // Initial content arrival: slot-entry effect re-fires once, pins to 800.
    // (Append-effect pinAuto also fires and sets smoothPinActiveRef=true.)
    act(() => {
      state.scrollHeight = 1200
      view.rerender({ items: mkItems(8), sessionId: 'no-repin-on-stream', getKey, externalScrollerRef: { current: el } })
    })
    expect(el.scrollTop).toBe(800)

    // Drain smoothPinActiveRef: dispatch a scroll event while we're at the
    // bottom (atBottom=true) so the smooth-pin branch in the scroll handler
    // transitions back to !smoothPinActive. In a real browser this happens
    // when the smooth-scroll animation finishes; jsdom's scrollTo stub
    // completes instantly but doesn't fire that final scroll event.
    act(() => { el.dispatchEvent(new Event('scroll')) })

    // Now the user scrolls up partway. With smoothPinActive cleared, the
    // scroll handler takes the normal-user-scroll path, sees the move is
    // not a self-scroll (400 ≠ lastWriteTop=800), and releases stick.
    act(() => { state.scrollTop = 400; el.dispatchEvent(new Event('scroll')) })
    expect(el.scrollTop).toBe(400)

    // Streaming-style append arrives. The append-effect's pinAuto checks
    // stickRef (released → no-op). The slot-entry effect's slotPinDoneRef
    // gate matches the current sessionId → no-op. Both paths preserve the
    // user's scroll position.
    act(() => {
      state.scrollHeight = 1400
      view.rerender({ items: mkItems(10), sessionId: 'no-repin-on-stream', getKey, externalScrollerRef: { current: el } })
    })

    expect(el.scrollTop).toBe(400)
  })

  it('jumps instantly (no smooth glide) when bulk history hydration replaces a thin list', () => {
    // The in-progress-conversation race: slot switches (sessionId flips), the
    // history fetch is in flight, and a live WS streaming chunk lands FIRST —
    // the list goes 0 → 1 and the slot-entry one-shot pin is consumed against
    // that lone streaming bubble.
    const { el, state, view } = render(
      { scrollTop: 0, scrollHeight: 0, clientHeight: 400 },
      [],
      'bulk-hydration',
    )
    act(() => {
      state.scrollHeight = 120
      view.rerender({ items: mkItems(1), sessionId: 'bulk-hydration', getKey, externalScrollerRef: { current: el } })
    })
    expect(el.scrollTop).toBe(0) // content shorter than viewport

    // Track HOW the scroller is driven from here: a smooth scrollTo is the
    // "awkward paging" bug; the fix must land via an instant write.
    let smoothCalls = 0
    ;(el as unknown as { scrollTo: (o: { top: number; behavior?: string }) => void }).scrollTo = (o) => {
      if (o.behavior === 'smooth') smoothCalls++
      state.scrollTop = o.top
    }

    // The fetch resolves: the full conversation replaces the thin list
    // (1 → 200 items, way past the overscan+1 bulk threshold).
    act(() => {
      state.scrollHeight = 24000
      view.rerender({ items: mkItems(200), sessionId: 'bulk-hydration', getKey, externalScrollerRef: { current: el } })
    })

    // Instant force-pin to the true bottom — not a smooth glide.
    expect(el.scrollTop).toBe(23600)
    expect(smoothCalls).toBe(0)
  })

  it('bulk growth does NOT yank a user who scrolled up while history loads', () => {
    const { el, state, view } = render(
      { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 },
      mkItems(8),
      'bulk-no-yank',
    )
    expect(el.scrollTop).toBe(1600)

    // User scrolls up to read — the scroll handler releases stick.
    act(() => { state.scrollTop = 300; el.dispatchEvent(new Event('scroll')) })

    // A bulk prepend lands (load-older page). Stick is released, so neither
    // the bulk force-pin nor pinAuto may move the viewport.
    act(() => {
      state.scrollHeight = 12000
      view.rerender({ items: mkItems(108), sessionId: 'bulk-no-yank', getKey, externalScrollerRef: { current: el } })
    })

    expect(el.scrollTop).toBe(300)
  })

  it('does NOT yank when the user scrolls up between the bulk pin and its settle frame', () => {
    // rAF is queued by the bulk path's settle pin — capture it so the test
    // controls exactly when the frame fires.
    const frames: FrameRequestCallback[] = []
    const origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      frames.push(cb)
      return frames.length
    }) as typeof requestAnimationFrame
    try {
      const { el, state, view } = render(
        { scrollTop: 0, scrollHeight: 0, clientHeight: 400 },
        [],
        'bulk-settle-scrollup',
      )
      // The settle frame guards on el.isConnected — attach the scroller so
      // the frame actually runs (other tests use a detached element because
      // they only exercise synchronous pins).
      document.body.appendChild(el)
      act(() => {
        state.scrollHeight = 120
        view.rerender({ items: mkItems(1), sessionId: 'bulk-settle-scrollup', getKey, externalScrollerRef: { current: el } })
      })
      frames.length = 0 // drop entry-pin frames; only the bulk settle matters below

      // Bulk hydration lands: synchronous force-pin to the bottom.
      act(() => {
        state.scrollHeight = 24000
        view.rerender({ items: mkItems(200), sessionId: 'bulk-settle-scrollup', getKey, externalScrollerRef: { current: el } })
      })
      expect(el.scrollTop).toBe(23600)

      // User scrolls up BEFORE the settle frame fires — stick is released.
      act(() => { state.scrollTop = 5000; el.dispatchEvent(new Event('scroll')) })

      // The settle frame must respect the released stick and not yank back.
      act(() => { frames.forEach(cb => cb(0)); frames.length = 0 })
      expect(el.scrollTop).toBe(5000)
      el.remove()
    } finally {
      globalThis.requestAnimationFrame = origRaf
    }
  })
})
