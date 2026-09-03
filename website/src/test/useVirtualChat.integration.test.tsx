// Feature: chat-virtualizer — useVirtualChat composing-hook integration tests.
//
// The pure pieces (FollowController, WindowCalculator, HeightCache) are unit-
// tested in isolation. This suite covers the WIRING that those unit tests
// can't reach — the effects/refs that drive follow/yank behavior: append-pin
// while followed, a user scroll-up releasing follow so a later append does NOT
// yank, and a slot switch force-pinning to the bottom.
//
// jsdom has no layout engine, so scrollTop/scrollHeight/clientHeight are faked
// on a controlled detached scroller element passed via `externalScrollerRef`.
// The follow logic reads `scrollerRef.current` + live geometry synchronously
// inside layout effects, so these assertions are deterministic — they do not
// depend on rAF, ResizeObserver, or IntersectionObserver timing. (ResizeObserver
// is intentionally undefined in the test env, so the RO auto-pin never fires and
// can't perturb the result.)

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { renderHook, render as rtlRender, act } from '@testing-library/react'
import { type RefObject } from 'react'
import { readFileSync } from 'node:fs'

// Resolved from the vitest cwd (website/), used by the chokepoint source guard.
const HOOK_SRC = 'src/hooks/virtualizer/useVirtualChat.ts'
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

function render(geom: Geom, items: Item[], sessionId: string, extra?: Partial<UseVirtualChatOptions<Item>>) {
  const { el, state } = makeScroller(geom)
  const ref: RefObject<HTMLDivElement | null> = { current: el }
  const initialProps: UseVirtualChatOptions<Item> = {
    items,
    sessionId,
    getKey,
    externalScrollerRef: ref,
    ...extra,
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

  it('streaming growth across many ticks issues INSTANT pins landing exactly on bottomTarget', () => {
    // T3/#4: while following, each streamed chunk grows the bottom target. The
    // pin must be INSTANT (behavior:'auto') and land EXACTLY on the new bottom
    // — a smooth pin would be cancelled/restarted by the next chunk and chase a
    // moving target, never converging on a tall transcript.
    const { el, state, view } = render(
      { scrollTop: 0, scrollHeight: 400, clientHeight: 400 },
      mkItems(3),
      'stream-instant',
    )
    expect(el.scrollTop).toBe(0)

    // Record how the scroller is driven from here.
    const behaviors: (string | undefined)[] = []
    ;(el as unknown as { scrollTo: (o: { top: number; behavior?: string }) => void }).scrollTo = (o) => {
      behaviors.push(o.behavior)
      state.scrollTop = o.top
    }

    let count = 3
    for (let h = 700; h <= 4000; h += 300) {
      count += 1
      act(() => {
        state.scrollHeight = h
        view.rerender({ items: mkItems(count), sessionId: 'stream-instant', getKey, externalScrollerRef: { current: el } })
      })
      // Landed EXACTLY on bottomTarget (scrollHeight - clientHeight).
      expect(el.scrollTop).toBe(h - 400)
    }

    // Every streaming pin was instant — no smooth animation to chase the target.
    expect(behaviors.length).toBeGreaterThan(0)
    expect(behaviors.every((b) => b === 'auto')).toBe(true)
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


// ---------------------------------------------------------------------------
// T4/#5 — scroll-anchor preservation across an upward window shift.
//
// jsdom has no layout, so this suite installs a tiny deterministic "layout
// engine": getBoundingClientRect walks the scroller's children summing their
// heights (spacers use their inline style.height; rows use a per-index real
// height) minus scrollTop, and offsetHeight reports the same real height so
// measureRef seeds the cache. The virtualizer's offset spacer is sized from
// the OffsetIndex, which cold-starts at the flat estimate (80) — so when an
// upward scroll mounts rows that actually render taller (100), the content
// above the viewport grows and the row the user is looking at would JUMP down.
// The anchor-preservation layout effect must cancel that jump by correcting
// scrollTop, keeping the visible top row's screen position stable.
// ---------------------------------------------------------------------------
describe('useVirtualChat: every scroll write goes through the one chokepoint', () => {
  // Follow-release correctness requires every programmatic scrollTop write to
  // record itself in lastWriteTopRef. That invariant is structural rather than
  // comment-enforced: all writes funnel through `writeScrollTop`, whose
  // `accounting` argument is REQUIRED, so tsc rejects a write that does not
  // state how the follow guard should treat it.
  //
  // This is a SOURCE guard rather than a behavioural one, deliberately. The
  // failure it prevents is a future contributor adding a raw `el.scrollTop = x`
  // that skips the bookkeeping; that is a property of the code, and attempts to
  // express it behaviourally in jsdom did not discriminate (the mocked geometry
  // cannot reproduce the browser's scroll-event ordering that makes the guard
  // load-bearing). Asserting it at the source level does discriminate: adding a
  // raw write fails this test immediately.
  it('has no raw scrollTop / scrollTo writes outside writeScrollTop', () => {
    const src = readFileSync(HOOK_SRC, 'utf8')
    // Isolate the chokepoint body — the one place raw writes are allowed.
    const chokeStart = src.indexOf('const writeScrollTop = useCallback(')
    expect(chokeStart).toBeGreaterThan(-1)
    const chokeEnd = src.indexOf('\n  )', chokeStart)
    const outside = src.slice(0, chokeStart) + src.slice(chokeEnd)

    const rawWrites = outside
      .split('\n')
      .map((line, i) => ({ line: line.trim(), n: i + 1 }))
      .filter(({ line }) => !line.startsWith('//'))
      .filter(({ line }) => /\.scrollTop\s*(=|\+=|-=)/.test(line) || /\.scrollTo\(\{/.test(line))

    expect(
      rawWrites.map((r) => r.line),
      'raw scroll writes must go through writeScrollTop(el, top, behavior, accounting)',
    ).toEqual([])
  })

  it('states an accounting disposition at every call site', () => {
    const src = readFileSync(HOOK_SRC, 'utf8')
    const calls = [...src.matchAll(/writeScrollTop\(\s*el[^)]*\)/g)].map((m) => m[0])
    // Every call site (excluding the declaration) passes 'pin' or 'release'.
    expect(calls.length).toBeGreaterThanOrEqual(5)
    for (const c of calls) {
      expect(c, `call site missing accounting: ${c}`).toMatch(/'(pin|release)'/)
    }
  })
})

describe('useVirtualChat: smooth-pin guard (GPT MEDIUM round 4)', () => {
  // scrollToBottom defers its write into a rAF; this suite otherwise avoids rAF,
  // so run it synchronously to keep these assertions deterministic.
  let realRaf: typeof globalThis.requestAnimationFrame
  beforeEach(() => {
    realRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      cb(0)
      return 0
    }) as typeof globalThis.requestAnimationFrame
  })
  afterEach(() => { globalThis.requestAnimationFrame = realRaf })

  // NOTE: the positive case (a smooth pin's intermediate animation frames must
  // not be read as user input) is verified in a REAL BROWSER, not here. jsdom has
  // no scroll animation, so reproducing it requires hand-faking the frame
  // sequence AND the append-pin path, which does not discriminate the guard
  // reliably. The behaviour is measured with the Chromium harness instead. What
  // IS asserted here is the inverse, which jsdom models faithfully: an instant
  // pin must not leave the guard armed and swallow real user input.
  it('an INSTANT pin does not arm the smooth guard', () => {
    const { el, state } = makeScroller({ scrollTop: 2500, scrollHeight: 3000, clientHeight: 500 })
    const ref = { current: el } as RefObject<HTMLDivElement>
    const { result } = renderHook(() =>
      useVirtualChat<Item>({
        items: mkItems(30),
        getKey,
        sessionId: 'instant-pin-guard',
        externalScrollerRef: ref,
      } as UseVirtualChatOptions<Item>),
    )
    act(() => { result.current.scrollToBottom('auto') })
    // A genuine user scroll-up right after an instant pin must still release
    // follow — the guard must not be left armed and swallowing real input.
    act(() => {
      state.scrollTop = 800
      el.dispatchEvent(new Event('scroll'))
    })
    expect(result.current.isAtBottom).toBe(false)
  })

  // The NOTE above still holds for the *animation* itself. What the following
  // tests cover is the pure bookkeeping around it — which scroll positions arm,
  // hold, and disarm the guard — and jsdom models that faithfully, because it is
  // only scrollTop values and scroll events. The observable signal for "guard
  // still armed" is the abort's side effect: a wheel while armed re-issues an
  // INSTANT scrollTo to freeze the glide (releasing stick alone would let the
  // native animation finish). If the guard is already disarmed, a wheel does
  // nothing. That distinction is what these assert.
  function smoothPinHarness(sessionId: string) {
    // bottomTarget = scrollHeight - clientHeight = 2500.
    const { el, state } = makeScroller({ scrollTop: 1000, scrollHeight: 3000, clientHeight: 500 })
    const behaviors: (string | undefined)[] = []
    ;(el as unknown as { scrollTo: (o: { top: number; behavior?: string }) => void }).scrollTo =
      (o) => {
        behaviors.push(o.behavior)
        // A SMOOTH write does not teleport scrollTop — that is what makes it a
        // glide. The animation is then simulated by the explicit scrollTo()
        // steps below, which is exactly the frame sequence the guard reads.
        if (o.behavior !== 'smooth') state.scrollTop = o.top
      }
    const ref = { current: el } as RefObject<HTMLDivElement>
    const { result } = renderHook(() =>
      useVirtualChat<Item>({
        items: mkItems(30),
        getKey,
        sessionId,
        externalScrollerRef: ref,
      } as UseVirtualChatOptions<Item>),
    )
    const scrollTo = (top: number) => {
      act(() => {
        state.scrollTop = top
        el.dispatchEvent(new Event('scroll'))
      })
    }
    // Real scenario: mount auto-pins to the bottom, the user scrolls UP, then
    // presses jump-to-latest — so the glide starts from where they are, not from
    // the bottom. Without this the mount pin leaves scrollTop at the target and
    // every simulated glide frame reads as backward (user) motion instead.
    scrollTo(1000)
    act(() => { result.current.scrollToBottom('smooth') })
    // Returns true if the wheel was still able to abort, i.e. the guard was armed.
    const wheelAborts = () => {
      const before = behaviors.length
      act(() => { el.dispatchEvent(new Event('wheel')) })
      return behaviors.length > before && behaviors[behaviors.length - 1] === 'auto'
    }
    return { el, state, behaviors, result, scrollTo, wheelAborts }
  }

  it('holds the guard through intermediate glide frames, and they do not release follow', () => {
    const h = smoothPinHarness('smooth-intermediate')
    // Forward progress toward the target: these events are OURS, not the user's.
    h.scrollTo(1400)
    h.scrollTo(1900)
    expect(h.result.current.isAtBottom).toBe(false) // still short of the target
    expect(h.wheelAborts()).toBe(true)
  })

  // The arrival condition is the value actually written, NOT `atBottom` (the
  // 100px bottomThreshold): if it disarmed on entering that band while the
  // native animation still had up to 100px to run, a user grabbing the page
  // inside the band would be carried to the bottom anyway.
  it('does NOT disarm merely because the glide entered the 100px bottom band', () => {
    const h = smoothPinHarness('smooth-band')
    // 3000 - (2450 + 500) = 50px from the bottom → inside the 100px UI band,
    // but 50px short of the value we actually wrote (2500).
    h.scrollTo(2450)
    expect(h.wheelAborts()).toBe(true)
  })

  it('disarms once the glide reaches the value actually written', () => {
    const h = smoothPinHarness('smooth-arrived')
    h.scrollTo(2500) // exactly lastWriteTop
    // Arrived → listeners dropped, so a later unrelated wheel must be inert.
    expect(h.wheelAborts()).toBe(false)
    expect(h.result.current.isAtBottom).toBe(true)
  })

  it('disarms when the scroller is bottom-anchored even if the write was clamped', () => {
    const h = smoothPinHarness('smooth-clamped')
    // Content shrank mid-glide: the written target 2500 is now unreachable, but
    // we ARE at the bottom. Without the bottom-anchored fallback the guard (and
    // its listeners) would stay armed forever.
    act(() => {
      h.state.scrollHeight = 2400
      h.state.scrollTop = 1900 // 2400 - 500 = 1900 → exactly bottom
      h.el.dispatchEvent(new Event('scroll'))
    })
    expect(h.wheelAborts()).toBe(false)
  })

  it('a backward scroll mid-glide releases follow and disarms', () => {
    const h = smoothPinHarness('smooth-backward')
    h.scrollTo(1400)
    h.scrollTo(900) // user grabbed the page: scrollTop moved backward
    expect(h.result.current.isAtBottom).toBe(false)
    expect(h.wheelAborts()).toBe(false) // already disarmed by the backward move
  })

  it('a wheel abort freezes the glide in place instead of only releasing stick', () => {
    const h = smoothPinHarness('smooth-freeze')
    h.scrollTo(1400)
    act(() => { h.el.dispatchEvent(new Event('wheel')) })
    // The instant re-issue is what cancels the browser's native animation.
    expect(h.behaviors[h.behaviors.length - 1]).toBe('auto')
    expect(h.state.scrollTop).toBe(1400)
    expect(h.result.current.isAtBottom).toBe(false)
  })

  // The abort must also catch a scrollbar drag or keyboard scroll during the
  // glide, not just `wheel`/`touchmove` — otherwise the continuing animation
  // overrides them. attachUserScrollIntent is the shared input set.
  it('a scrollbar drag (pointerdown) aborts the glide', () => {
    const h = smoothPinHarness('smooth-pointer')
    h.scrollTo(1400)
    act(() => { h.el.dispatchEvent(new Event('pointerdown')) })
    expect(h.behaviors[h.behaviors.length - 1]).toBe('auto')
    expect(h.state.scrollTop).toBe(1400)
    expect(h.result.current.isAtBottom).toBe(false)
  })

  it('a scrolling keypress aborts the glide, but ordinary typing does not', () => {
    const h = smoothPinHarness('smooth-keys')
    h.scrollTo(1400)
    // Typing in the search box while a glide runs is not scroll intent.
    act(() => { h.el.dispatchEvent(new KeyboardEvent('keydown', { key: 'a' })) })
    expect(h.behaviors[h.behaviors.length - 1]).toBe('smooth')
    act(() => { h.el.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp' })) })
    expect(h.behaviors[h.behaviors.length - 1]).toBe('auto')
    expect(h.result.current.isAtBottom).toBe(false)
  })

  // pinAuto (RO tick / append layout effect) must not read the glide's own
  // mid-flight scrollTop as a user scroll-up — scrollTop sits below the recorded
  // target AND meaningfully away from the bottom, which is exactly
  // evaluateAutoPin's release signature. Streaming output resizes constantly, so
  // an append during an explicit jump-to-latest would otherwise kill follow for
  // the rest of the response.
  function appendDuringGlide(sessionId: string) {
    const { el, state } = makeScroller({ scrollTop: 1000, scrollHeight: 3000, clientHeight: 500 })
    const behaviors: (string | undefined)[] = []
    ;(el as unknown as { scrollTo: (o: { top: number; behavior?: string }) => void }).scrollTo =
      (o) => {
        behaviors.push(o.behavior)
        if (o.behavior !== 'smooth') state.scrollTop = o.top
      }
    const ref = { current: el } as RefObject<HTMLDivElement>
    const base = {
      items: mkItems(30),
      getKey,
      sessionId,
      externalScrollerRef: ref,
    } as UseVirtualChatOptions<Item>
    const view = renderHook((props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props), {
      initialProps: base,
    })
    // User scrolled up, then pressed jump-to-latest: the glide starts from where
    // they are and targets the CURRENT bottom (3000 - 500 = 2500).
    act(() => { state.scrollTop = 1000; el.dispatchEvent(new Event('scroll')) })
    act(() => { view.result.current.scrollToBottom('smooth') })
    const writesBeforeAppend = behaviors.length
    // Streaming appends mid-glide: the bottom moves to 4000 - 500 = 3500.
    act(() => {
      state.scrollHeight = 4000
      view.rerender({ ...base, items: mkItems(31) })
    })
    return { el, state, behaviors, view, writesBeforeAppend }
  }

  it('a mid-glide append does not release follow, and lands on the NEW bottom on arrival', () => {
    const h = appendDuringGlide('smooth-append-follow')
    // Simulate the animation reaching the value we wrote (2500) — short of the
    // new bottom, because the content grew after the write.
    act(() => { h.state.scrollTop = 2500; h.el.dispatchEvent(new Event('scroll')) })
    // Arrival disarms the guard and re-targets instantly to the new bottom.
    expect(h.state.scrollTop).toBe(3500)
    // The re-target is itself a scroll write; the browser emits a scroll event
    // for it, which is what refreshes the exposed isAtBottom state (it was
    // computed from the geometry read BEFORE the write).
    act(() => { h.el.dispatchEvent(new Event('scroll')) })
    expect(h.view.result.current.isAtBottom).toBe(true)
  })

  it('does not re-issue a smooth write for a mid-glide append (no animation restart)', () => {
    const h = appendDuringGlide('smooth-append-norestart')
    // Re-targeting mid-animation would cancel and restart the glide every resize
    // tick.
    expect(h.behaviors.length).toBe(h.writesBeforeAppend)
  })
})

describe('useVirtualChat: adaptive height estimate is wired into the offsets (GPT HIGH round 13)', () => {
  // HeightCache.averageHeight() is unit-tested in isolation, but that passes
  // even if the hook's `getH` uses the flat `estimatedHeight` — which sizes a
  // long transcript's unmeasured rows by a fixed 80px guess, so total height and
  // the spacer offsets are wildly short and scrolling up lands in the wrong
  // place. This asserts the WIRING — that measured heights feed the estimate for
  // UNMEASURED rows.
  beforeEach(() => localStorage.clear())

  const seed = (sid: string, keys: string[], h: number) => {
    const blob: Record<string, number> = {}
    for (const k of keys) blob[k] = h
    localStorage.setItem(`vc_heights_${sid}`, JSON.stringify(blob))
  }

  it('sizes unmeasured rows from the measured mean, not the flat estimate', () => {
    const sid = 'estimate-wiring'
    const N = 200
    // Ten rows measured at 500px; the other 190 have never been measured.
    seed(sid, Array.from({ length: 10 }, (_, i) => `m${i}`), 500)
    const { view } = render({ scrollTop: 0, scrollHeight: 1000, clientHeight: 400 }, mkItems(N), sid)

    // Flat 80px estimate would give 10*500 + 190*80 = 20,200. The adaptive mean
    // (500) gives 200*500 = 100,000.
    expect(view.result.current.totalHeight).toBe(N * 500)
    expect(view.result.current.totalHeight).toBeGreaterThan(20_200 * 2)
  })

  it('the spacer offsets follow the adaptive estimate too', () => {
    const sid = 'estimate-wiring-spacers'
    seed(sid, Array.from({ length: 10 }, (_, i) => `m${i}`), 500)
    // A NON-following reader: while follow is armed the window is tail-anchored
    // by design, so a top-pinned window (the scenario this test needs) only
    // exists for a reader who scrolled up / released follow.
    const { view } = render({ scrollTop: 0, scrollHeight: 1000, clientHeight: 400 }, mkItems(200), sid, { followOutput: false })
    // offsetBefore + rendered window + offsetAfter must reconstruct the total,
    // so an under-estimate anywhere would show up as a mismatch.
    const v = view.result.current
    expect(v.offsetBefore + v.offsetAfter).toBeLessThanOrEqual(v.totalHeight)
    // With the window pinned at the top, everything below it is estimated —
    // the flat guess would make offsetAfter an order of magnitude smaller.
    expect(v.offsetAfter).toBeGreaterThan(20_000)
  })

  it('falls back to the configured estimate only when nothing is measured', () => {
    const { view } = render(
      { scrollTop: 0, scrollHeight: 1000, clientHeight: 400 },
      mkItems(50),
      'estimate-wiring-empty',
    )
    expect(view.result.current.totalHeight).toBe(50 * 80)
  })
})

describe('useVirtualChat: OffsetIndex is rebuilt on session switch (GPT MEDIUM)', () => {
  // The offsetIndex memo must rebuild on a session switch even when the item
  // count is unchanged: getH's identity is stable across a session change
  // (deps: [estimatedHeight]), so keying only on [itemCount, getH] would leave
  // the Fenwick tree serving the previous transcript's heights and rendering
  // wrong spacers until a measurement tick corrected it.
  const seedHeights = (sessionId: string, n: number, h: number) => {
    const blob: Record<string, number> = {}
    for (let i = 0; i < n; i++) blob[`m${i}`] = h
    window.localStorage.setItem(`vc_heights_${sessionId}`, JSON.stringify(blob))
  }

  it('reports the NEW session\'s offsets when both sessions have equal item counts', () => {
    const N = 40
    // Two sessions, same row count, very different per-row heights.
    seedHeights('offidx-a', N, 100)
    seedHeights('offidx-b', N, 500)
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 1000, clientHeight: 500 })
    const ref = { current: el } as RefObject<HTMLDivElement>
    const { result, rerender } = renderHook(
      ({ sid }: { sid: string }) =>
        useVirtualChat<Item>({
          items: mkItems(N),
          getKey,
          sessionId: sid,
          externalScrollerRef: ref,
        } as UseVirtualChatOptions<Item>),
      { initialProps: { sid: 'offidx-a' } },
    )
    const totalA = result.current.totalHeight
    expect(totalA).toBe(N * 100)

    // Same item count, different session — the tree MUST be rebuilt.
    rerender({ sid: 'offidx-b' })
    expect(result.current.totalHeight).toBe(N * 500)
    expect(result.current.totalHeight).not.toBe(totalA)

    window.localStorage.removeItem('vc_heights_offidx-a')
    window.localStorage.removeItem('vc_heights_offidx-b')
  })
})

describe('useVirtualChat: height-cache eviction cap is wired to the row count', () => {
  // The hook must construct HeightCache WITH rowCount: HeightCache.test.ts
  // proves the cap arithmetic, but if the hook builds `new HeightCache(sessionId)`
  // without supplying rowCount, the cap stays pinned at the 2000 floor and long
  // sessions evict their oldest heights while every unit test still passes.
  // These assertions cover the WIRING, not the arithmetic.
  //
  // Public surface used: HeightCache enforces the cap when it LOADS a persisted
  // blob, so a pre-seeded localStorage entry of n > 2000 heights survives mount
  // only if the hook told the cache the session is n rows long.
  const CAP_FLOOR = 2000
  const SIDS = ['cap-wiring-seed', 'cap-wiring-short', 'cap-wiring-ceiling']
  // These tests deliberately write persisted height blobs. Clear them around
  // every case so they can't leak into suites that enumerate localStorage
  // (an order-dependent flake source).
  const clearSeeds = () => {
    for (const sid of SIDS) window.localStorage.removeItem(`vc_heights_${sid}`)
  }
  beforeEach(clearSeeds)
  afterEach(clearSeeds)
  const seed = (sessionId: string, n: number) => {
    const blob: Record<string, number> = {}
    for (let i = 0; i < n; i++) blob[`m${i}`] = 40 + (i % 5)
    window.localStorage.setItem(`vc_heights_${sessionId}`, JSON.stringify(blob))
  }
  const persistedCount = (sessionId: string) => {
    const raw = window.localStorage.getItem(`vc_heights_${sessionId}`)
    return raw ? Object.keys(JSON.parse(raw) as Record<string, number>).length : 0
  }
  // Mount, then push ONE real measurement through the hook's own measure path.
  // That matters: flush() skips when the cache isn't dirty, so without a write
  // the stale seeded blob would be read straight back and the assertion would
  // pass even with the cap left at the floor (a vacuous test).
  const mountMeasureUnmount = (sessionId: string, n: number) => {
    const { el } = makeScroller({ scrollTop: 0, scrollHeight: 1000, clientHeight: 500 })
    const ref = { current: el } as RefObject<HTMLDivElement>
    const { result, unmount } = renderHook(() =>
      useVirtualChat<Item>({
        items: mkItems(n),
        getKey,
        sessionId,
        externalScrollerRef: ref,
      } as UseVirtualChatOptions<Item>),
    )
    act(() => {
      const node = document.createElement('div')
      Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 44 })
      result.current.measureRef(0)(node)
    })
    unmount() // flushes the (now dirty) cache to localStorage
  }

  it('keeps more than the 2000 floor for a 3000-row session (cap seeded from row count)', () => {
    const sid = 'cap-wiring-seed'
    seed(sid, 3000)
    mountMeasureUnmount(sid, 3000)
    // With the cap left at the floor this trims to exactly 2000 and m0 is gone.
    expect(persistedCount(sid)).toBeGreaterThan(CAP_FLOOR)
    const blob = JSON.parse(window.localStorage.getItem(`vc_heights_${sid}`)!)
    expect(blob.m0).toBeDefined() // oldest entry survived
  })

  it('still trims a stale oversized blob down to the floor for a short session', () => {
    const sid = 'cap-wiring-short'
    seed(sid, 2600) // stale oversized blob, but the session is only 100 rows
    mountMeasureUnmount(sid, 100)
    expect(persistedCount(sid)).toBe(CAP_FLOOR)
  })

  it('honours the hard ceiling rather than tracking row count without bound', () => {
    const sid = 'cap-wiring-ceiling'
    seed(sid, 300)
    mountMeasureUnmount(sid, 1_000_000)
    // Cap is min(rowCount, 20000); the seeded 300 all survive and nothing blows up.
    expect(persistedCount(sid)).toBeGreaterThanOrEqual(300)
    expect(persistedCount(sid)).toBeLessThanOrEqual(20000)
  })
})

describe('useVirtualChat: scroll-anchor preservation (T4/#5)', () => {
  const REAL_H = 100 // every mounted row renders this tall…
  // …while the OffsetIndex spacer cold-starts at estimatedHeight (80), so each
  // newly-mounted row above the viewport adds REAL_H - 80 = 20px of drift.

  let restore: (() => void) | null = null

  function rect(top: number, height: number): DOMRect {
    return {
      top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: top,
      toJSON() { return {} },
    } as DOMRect
  }

  function installFakeLayout(scroller: HTMLElement, clientHeight: number) {
    const proto = HTMLElement.prototype
    const origRect = proto.getBoundingClientRect
    const origOffsetH = Object.getOwnPropertyDescriptor(proto, 'offsetHeight')

    const childHeight = (child: Element): number => {
      const di = (child as HTMLElement).getAttribute('data-index')
      if (di !== null) return REAL_H
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

  afterEach(() => {
    restore?.()
    restore = null
  })

  function AnchorHarness({ items, scrollerRef }: {
    items: Item[]
    scrollerRef: RefObject<HTMLDivElement | null>
  }) {
    const v = useVirtualChat<Item>({
      items, sessionId: 'anchor', getKey, overscan: 2, externalScrollerRef: scrollerRef,
    })
    return (
      <div ref={scrollerRef as RefObject<HTMLDivElement>} data-scroller>
        <div ref={v.topSentinelRef} data-sentinel="top" />
        <div data-spacer="before" style={{ height: v.offsetBefore }} />
        {v.virtualItems.map((it) => (
          <div key={it.key} data-index={it.index} ref={v.measureRef(it.index)} />
        ))}
        <div data-spacer="after" style={{ height: v.offsetAfter }} />
        <div ref={v.bottomSentinelRef} data-sentinel="bottom" />
      </div>
    )
  }

  it('holds the top visible row steady when an upward shift mounts rows above it', () => {
    // Deterministic rAF: capture frames so we control exactly when the
    // scroll-driven window recompute runs.
    const frames: FrameRequestCallback[] = []
    const origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      frames.push(cb)
      return frames.length
    }) as typeof requestAnimationFrame

    // Fake IntersectionObserver: capture instances so the test can fire the
    // top-sentinel intersection deterministically (jsdom has none).
    interface FakeIOInst { cb: IntersectionObserverCallback }
    const ioInstances: FakeIOInst[] = []
    class FakeIO {
      cb: IntersectionObserverCallback
      constructor(cb: IntersectionObserverCallback) { this.cb = cb; ioInstances.push(this) }
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() { return [] }
      root: Element | null = null
      rootMargin = ''
      thresholds: number[] = []
    }
    const origIO = globalThis.IntersectionObserver
    globalThis.IntersectionObserver = FakeIO as unknown as typeof IntersectionObserver

    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    try {
      const N = 30
      let scrollTop = 0
      const CLIENT = 400
      const SCROLL_HEIGHT = 3000

      rtlRender(<AnchorHarness items={mkItems(N)} scrollerRef={scrollerRef} />)
      const el = scrollerRef.current!
      Object.defineProperty(el, 'scrollTop', {
        configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v },
      })
      Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => CLIENT })
      Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => SCROLL_HEIGHT })
      installFakeLayout(el, CLIENT)

      // Drain the mount pins (slot-entry / bulk forcePin rAFs) at the real
      // geometry so they don't fire mid-test, then discard their frames.
      act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })

      // Scroll so the OffsetIndex (80px/row) places the window near the bottom:
      // indexAt(2160)=27 → window {25..30}. The scroll releases stick and
      // schedules a recompute; flush that one frame.
      act(() => { scrollTop = 2160; el.dispatchEvent(new Event('scroll')) })
      act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })

      // Topmost visible mounted row + its screen top, computed the same way the
      // hook's captureTopAnchor does.
      const topVisible = () => {
        const srTop = el.getBoundingClientRect().top
        let best: { idx: number; top: number } | null = null
        el.querySelectorAll('[data-index]').forEach((node) => {
          const r = (node as HTMLElement).getBoundingClientRect()
          if (r.bottom - srTop <= 0) return
          const idx = Number((node as HTMLElement).getAttribute('data-index'))
          if (!best || idx < best.idx) best = { idx, top: r.top - srTop }
        })
        return best
      }
      const before = topVisible()!
      expect(before).not.toBeNull()

      // Fire the top-sentinel intersection: expandWindowUp shifts start down by
      // overscan (25 → 23), mounting rows 23,24 which render at REAL_H (100)
      // while the shrunk offsetBefore spacer only credits them 80 each — a
      // per-row drift that would push `before` down without compensation.
      const topSentinel = el.querySelector('[data-sentinel="top"]') as HTMLElement
      act(() => {
        ioInstances[0].cb(
          [{ isIntersecting: true, target: topSentinel } as unknown as IntersectionObserverEntry],
          ioInstances[0] as unknown as IntersectionObserver,
        )
      })

      // The same row's screen position must be preserved (compensation moved
      // scrollTop by the drift), keeping the transcript visually stable.
      const afterNode = el.querySelector(`[data-index="${before.idx}"]`) as HTMLElement | null
      expect(afterNode).not.toBeNull()
      const afterTop = afterNode!.getBoundingClientRect().top - el.getBoundingClientRect().top
      expect(Math.abs(afterTop - before.top)).toBeLessThanOrEqual(1)
      // Compensation held the anchor by pushing scrollTop DOWN by the drift the
      // newly-mounted (taller-than-estimated) rows introduced above it.
      expect(scrollTop).toBeGreaterThan(2160)
    } finally {
      globalThis.requestAnimationFrame = origRaf
      globalThis.IntersectionObserver = origIO
    }
  })

  // At start === 0 expandWindowUp is a NO-OP, so a top anchor captured anyway is
  // never consumed upward — it sits pending until the next unrelated commit,
  // which then "corrects" scrollTop back to where the anchored row was, yanking
  // the user toward the top.
  it('does not capture a top anchor when the window is already at index 0', () => {
    const frames: FrameRequestCallback[] = []
    const origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      frames.push(cb)
      return frames.length
    }) as typeof requestAnimationFrame

    interface FakeIOInst { cb: IntersectionObserverCallback }
    const ioInstances: FakeIOInst[] = []
    class FakeIO {
      cb: IntersectionObserverCallback
      constructor(cb: IntersectionObserverCallback) { this.cb = cb; ioInstances.push(this) }
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() { return [] }
      root: Element | null = null
      rootMargin = ''
      thresholds: number[] = []
    }
    const origIO = globalThis.IntersectionObserver
    globalThis.IntersectionObserver = FakeIO as unknown as typeof IntersectionObserver

    const scrollerRef: RefObject<HTMLDivElement | null> = { current: null }
    try {
      let scrollTop = 0
      const CLIENT = 400
      rtlRender(<AnchorHarness items={mkItems(30)} scrollerRef={scrollerRef} />)
      const el = scrollerRef.current!
      Object.defineProperty(el, 'scrollTop', {
        configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v },
      })
      Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => CLIENT })
      Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 3000 })
      installFakeLayout(el, CLIENT)
      act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })

      // Near the very top: the window's start is already 0, and the scroll
      // releases stick (the condition the capture was gated on).
      act(() => { scrollTop = 50; el.dispatchEvent(new Event('scroll')) })
      act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })

      // Top sentinel fires (it is permanently in view at the top of the list).
      const topSentinel = el.querySelector('[data-sentinel="top"]') as HTMLElement
      act(() => {
        ioInstances[0].cb(
          [{ isIntersecting: true, target: topSentinel } as unknown as IntersectionObserverEntry],
          ioInstances[0] as unknown as IntersectionObserver,
        )
      })

      // The user now scrolls DOWN a little, then any commit lands (here the
      // bottom sentinel extending the window). A pending anchor from the no-op
      // upward expansion would be applied on that commit and drag scrollTop back
      // to 50.
      act(() => { scrollTop = 150; el.dispatchEvent(new Event('scroll')) })
      act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })
      const bottomSentinel = el.querySelector('[data-sentinel="bottom"]') as HTMLElement
      act(() => {
        ioInstances[0].cb(
          [{ isIntersecting: true, target: bottomSentinel } as unknown as IntersectionObserverEntry],
          ioInstances[0] as unknown as IntersectionObserver,
        )
      })
      act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })

      expect(scrollTop).toBe(150)
    } finally {
      globalThis.requestAnimationFrame = origRaf
      globalThis.IntersectionObserver = origIO
    }
  })
})

// Feature: chat-virtualizer — initialPlacement: 'top' (the list/gallery contract).
//
// The default is the chat contract: tail window + a slot-entry force-pin to the
// bottom. A gallery consuming this hook opens at the HEAD instead — and beyond
// the landing position this is the flicker fix: at the tail every unmeasured
// row is ABOVE the viewport, so each measurement forces a scrollTop
// compensation write; at the head they are all below, and corrections land in
// the bottom spacer invisibly.
describe('initialPlacement: top', () => {
  const geom = { scrollTop: 0, scrollHeight: 4000, clientHeight: 800 }

  function renderTop(items: Item[], extra?: Partial<UseVirtualChatOptions<Item>>) {
    const { el, state } = makeScroller({ ...geom, ...((extra as { geom?: Geom })?.geom ?? {}) })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const view = renderHook((props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props), {
      initialProps: {
        items,
        sessionId: 'gallery-top',
        getKey,
        externalScrollerRef: ref,
        followOutput: false,
        initialPlacement: 'top',
        ...extra,
      } as UseVirtualChatOptions<Item>,
    })
    return { view, el, state }
  }

  it('mounts the HEAD window, not the tail', () => {
    const { view } = renderTop(mkItems(40))
    const mounted = view.result.current.virtualItems.filter((v) => v.mounted).map((v) => v.index)
    expect(mounted).toContain(0)
    expect(mounted).not.toContain(39)
    // Nothing above the first mounted row — measurements can only grow the
    // bottom spacer, which is what makes mount quiet.
    expect(view.result.current.offsetBefore).toBe(0)
  })

  it('slot entry lands at scrollTop 0 even on an inherited scroller, and does not bottom-pin', () => {
    // The page-column scroller outlives the gallery view, so it can carry
    // leftover scrollTop from whatever it showed before.
    const { state } = renderTop(mkItems(40), { geom: { scrollTop: 500, scrollHeight: 4000, clientHeight: 800 } } as never)
    expect(state.scrollTop).toBe(0)
  })

  it('default placement still takes the chat contract (tail window)', () => {
    const { el, state } = makeScroller(geom)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const { result } = renderHook(() =>
      useVirtualChat<Item>({ items: mkItems(40), sessionId: 'chat-default', getKey, externalScrollerRef: ref }),
    )
    const mounted = result.current.virtualItems.filter((v) => v.mounted).map((v) => v.index)
    expect(mounted).toContain(39)
    expect(mounted).not.toContain(0)
    // Slot entry force-pinned to the bottom.
    expect(state.scrollTop).toBe(geom.scrollHeight - geom.clientHeight)
  })

  it('measures sub-pixel row heights instead of rounding to integers', () => {
    // offsetHeight rounds; content scaled to width is fractional (an image at
    // a 696:204 ratio in a 342px column is 100.24px tall). The rounding error
    // accumulates across the list into a few-pixel drift that cashes out at
    // window boundaries on engines without scroll anchoring (iOS Safari).
    const { view } = renderTop(mkItems(10), { eagerFirstMeasure: true } as never)
    const before = view.result.current.totalHeight
    const node = document.createElement('div')
    Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 405 })
    node.getBoundingClientRect = () =>
      ({ height: 404.688, width: 342, top: 0, left: 0, bottom: 404.688, right: 342, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
    act(() => { view.result.current.measureRef(0)(node) })
    // Unmeasured rows re-estimate from the measured average, so pin the
    // DISCRIMINATING property rather than the arithmetic: the fraction
    // survives into the total (404.688 → quarter-px 404.75). Rounding through
    // offsetHeight instead yields an integer total.
    expect(before % 1).toBe(0)
    expect(view.result.current.totalHeight % 1).not.toBe(0)
  })

  it('eagerFirstMeasure lands a first measurement in the offset math immediately, even under a mounting streak', () => {
    // The downward-scroll bounce: scrolling down mounts a new card every few
    // dozen ms; each seed measurement used to (re-)arm the 120ms debounce, so
    // the offset tree stayed frozen at estimates for the whole gesture, and
    // every row the window front handed from real DOM to the before-spacer
    // shrank the content above the viewport by (real − estimate).
    const { view } = renderTop(mkItems(10), { eagerFirstMeasure: true } as never)
    const before = view.result.current.totalHeight
    const measure = (i: number, h: number) => {
      const node = document.createElement('div')
      Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => h })
      view.result.current.measureRef(i)(node)
    }
    // Two seeds in quick succession — no timers advanced in between, exactly
    // the streak that used to starve the debounced sync. Directional assert:
    // unmeasured rows re-estimate from the measured average, so the exact
    // total is the cache's business; what matters is it moved NOW.
    act(() => { measure(0, 560) })
    act(() => { measure(1, 560) })
    expect(view.result.current.totalHeight).toBeGreaterThan(before)
  })

  it('without the option, first measurements stay debounced (the chat contract)', () => {
    // Pins the default: the upward-anchor compensation's commit ordering
    // depends on seeds riding the debounce, so eager sync must be opt-in.
    const { view } = renderTop(mkItems(10))
    const before = view.result.current.totalHeight
    const node = document.createElement('div')
    Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 560 })
    act(() => { view.result.current.measureRef(0)(node) })
    expect(view.result.current.totalHeight).toBe(before)
  })

  it('maps scrollTop through the leading offset when content sits above the list', () => {
    // A page column carrying header/toolbar content ABOVE the list: raw
    // scrollTop is NOT a list offset. With 1600px of leading content and the
    // scroller at scrollTop 1600, the viewport is exactly at the list's first
    // row — a raw conversion would instead compute row ~20 (1600px / 80px
    // estimate) and swap the window away from the rows actually on screen,
    // at the same scroll positions every time (the fixed-position bounce).
    const rect = (top: number) =>
      ({ top, left: 0, bottom: top + 800, right: 390, width: 390, height: 800, x: 0, y: top, toJSON: () => ({}) }) as DOMRect
    const { el, state } = makeScroller({ scrollTop: 0, scrollHeight: 4800, clientHeight: 800 })
    ;(el as unknown as { getBoundingClientRect: () => DOMRect }).getBoundingClientRect = () => rect(0)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const frames: FrameRequestCallback[] = []
    const origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { frames.push(cb); return frames.length }) as typeof requestAnimationFrame
    try {
      const { result } = renderHook(() =>
        useVirtualChat<Item>({
          items: mkItems(40),
          sessionId: 'lead-offset',
          getKey,
          externalScrollerRef: ref,
          followOutput: false,
          initialPlacement: 'top',
        }),
      )
      // The list's own top sentinel: at scrollTop 1600 it sits exactly at the
      // viewport top, i.e. the leading content is 1600px tall.
      const sentinel = document.createElement('div')
      ;(sentinel as unknown as { getBoundingClientRect: () => DOMRect }).getBoundingClientRect = () => rect(0)
      ;(result.current.topSentinelRef as { current: HTMLDivElement | null }).current = sentinel
      act(() => { state.scrollTop = 1600; el.dispatchEvent(new Event('scroll')) })
      act(() => { frames.forEach((cb) => cb(0)); frames.length = 0 })
      const indices = result.current.virtualItems.map((v) => v.index)
      // The viewport is at the FIRST row: the window must still cover it.
      expect(Math.min(...indices)).toBe(0)
    } finally {
      globalThis.requestAnimationFrame = origRaf
    }
  })
})
