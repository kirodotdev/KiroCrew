// Feature: chat-virtualizer — viewport-box resize re-pin.
//
// The row ResizeObserver tracks CONTENT heights; the viewport observer under
// test here tracks the SCROLLER's own box. Chrome around the transcript
// (composer autosize on draft restore, attachment strips, banners, a window
// resize) shrinks the scroller with no scroll event and no row resize; while
// pinned to the bottom that used to strand the view slightly ABOVE the new
// bottom target ("switching sessions doesn't land at the bottom"). These tests
// pin the re-pin, its follow-guard (a reading user is never yanked), and the
// rail-collapse deferral (no per-frame scrollTop writes during the shell grid
// animation).

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatOptions } from '../hooks/virtualizer/types'
import { setRailWidth, railWidthFor, RAIL_SETTLE_MS, __resetRailWidth } from '../hooks/useRailWidth'
import { __resetComposerResizeMark } from '../utils/composerResize'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state: Geom = { ...initial }
  const writes = { n: 0 }
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { writes.n++; state.scrollTop = v },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { writes.n++; state.scrollTop = o.top }
  return { el, state, writes }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  cb: ResizeObserverCallback
  observed = new Set<Element>()
  constructor(cb: ResizeObserverCallback) { this.cb = cb; FakeResizeObserver.instances.push(this) }
  observe(el: Element) { this.observed.add(el) }
  unobserve(el: Element) { this.observed.delete(el) }
  disconnect() { this.observed.clear() }
  fire(entries: Partial<ResizeObserverEntry>[] = []) {
    this.cb(entries as ResizeObserverEntry[], this as unknown as ResizeObserver)
  }
}

/**
 * DIRECTION ASYMMETRY — only ONE of the two viewport directions needs a write.
 *
 * A SHRINK raises the maximum scrollTop (`scrollHeight - clientHeight` grows), and
 * no engine ever pushes a reader DOWN, so a bottom-flush follower is stranded
 * above the new bottom until something writes. That is the defect this file's
 * other cases pin.
 *
 * A GROWTH lowers the maximum, so the engine's own clamp brings a flush reader
 * back to flush with no write at all — and for a reader parked ABOVE the bottom
 * that same clamp is what drags them to the end (the deleting-a-draft report). A
 * pin there is therefore redundant at best and the yank itself at worst.
 */
describe('useVirtualChat: viewport-box resize re-pin', () => {
  let origRO: typeof ResizeObserver | undefined
  let origRaf: typeof requestAnimationFrame

  beforeEach(() => {
    localStorage.clear()
    __resetRailWidth()
    FakeResizeObserver.instances = []
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    globalThis.ResizeObserver = origRO as typeof ResizeObserver
    globalThis.requestAnimationFrame = origRaf
    __resetRailWidth()
  })

  /** The shared observer (it watches the scroller element alongside rows). */
  function viewportRO(el: HTMLElement): FakeResizeObserver {
    const inst = FakeResizeObserver.instances.find((i) => i.observed.has(el))
    expect(inst).toBeDefined()
    return inst!
  }

  /** Deliver a viewport-box resize: an entry whose target is the scroller. */
  function fireViewport(el: HTMLElement) {
    viewportRO(el).fire([{ target: el } as Partial<ResizeObserverEntry>])
  }

  function mount(sessionId: string, geom: Geom, items: Item[]) {
    const { el, state, writes } = makeScroller(geom)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const baseProps: UseVirtualChatOptions<Item> = { items, sessionId, getKey, externalScrollerRef: ref }
    const view = renderHook((p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p), { initialProps: baseProps })
    act(() => { vi.advanceTimersByTime(250) }) // settle mount timers
    return { el, state, view, writes }
  }

  it('re-pins to the new bottom when the viewport shrinks while followed', () => {
    // Pinned at the bottom: 2000 - 400 = 1600 (slot-entry forcePin).
    const { el, state } = mount('viewport-shrink', { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 }, mkItems(10))
    expect(el.scrollTop).toBe(1600)

    // The composer grows (draft restored / attachment strip mounts): the
    // scroller's box shrinks by 60px. No scroll event, no row resize — only
    // the viewport observer sees it. Old scrollTop is now 60px short.
    act(() => {
      state.clientHeight = 340
      fireViewport(el)
    })
    expect(el.scrollTop).toBe(2000 - 340)
  })

  it('does NOT move a user who scrolled up when the viewport shrinks', () => {
    const { el, state } = mount('viewport-noyank', { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 }, mkItems(10))
    expect(el.scrollTop).toBe(1600)

    // User scrolls up to read history — the scroll handler releases follow.
    act(() => { state.scrollTop = 600; el.dispatchEvent(new Event('scroll')) })

    act(() => {
      state.clientHeight = 340
      fireViewport(el)
    })
    expect(el.scrollTop).toBe(600)
  })

  it('re-pins through the shrink animation when a content clamp preceded it', () => {
    // The measured cause of the queue-band dip. A send that queues behind a
    // busy turn regroups the turn and remounts tail rows, so the content
    // shrinks and the browser clamps scrollTop; the queue band then mounts
    // below the transcript and spring-animates the scroller's box smaller over
    // the following frames. The clamp's scroll event used to be stamped as user
    // input, which armed the SCROLL_SETTLE_MS gate and suppressed EVERY
    // viewport re-pin of that animation.
    const { el, state } = mount('viewport-clamp-gate', { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 }, mkItems(10))
    expect(el.scrollTop).toBe(1600)

    // The remount: content shrinks 125px, the layout engine clamps scrollTop by
    // the same amount, and the resulting scroll event dispatches. Still exactly
    // at the bottom (1875 - 1475 - 400 === 0), so this is a clamp, not input.
    act(() => {
      state.scrollHeight = 1875
      state.scrollTop = 1475
      el.dispatchEvent(new Event('scroll'))
    })

    // First frame of the band's animation, well inside the settle window.
    act(() => {
      state.clientHeight = 371
      fireViewport(el)
    })
    expect(el.scrollTop).toBe(1875 - 371)
  })

  it('a genuine gesture still holds pins off for the settle window', () => {
    // The boundary the fix must not move: real input is stamped by the intent
    // listeners at wheel/touch/key time, and a viewport shrink inside that
    // window must not write scrollTop out from under the gesture.
    const { el, state, writes } = mount('viewport-gesture-gate', { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 }, mkItems(10))
    expect(el.scrollTop).toBe(1600)
    const before = writes.n

    act(() => { el.dispatchEvent(new Event('wheel')) })
    act(() => {
      state.clientHeight = 371
      fireViewport(el)
    })
    expect(writes.n).toBe(before)

    // Once the window expires, follow resumes. (SCROLL_SETTLE_MS is 150ms and
    // module-private; followDisengage's gate test uses the same literal.)
    act(() => { vi.advanceTimersByTime(151); fireViewport(el) })
    expect(el.scrollTop).toBe(2000 - 371)
  })

  it('re-pins when a tail-row remount clamps scrollTop in the same tick as the shrink', () => {
    const { el, state } = mount('viewport-clamp-shrink', { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 }, mkItems(10))
    expect(el.scrollTop).toBe(1600)

    // A send that queues behind a busy turn does two things in one commit
    // window: the queued row appends, which regroups the turn and REMOUNTS
    // tail rows (content transiently shrinks — here by 28px, so the browser
    // clamps scrollTop to the new maximum 1972 - 400 = 1572), and the queue
    // band mounts below the transcript and spring-animates the scroller's box
    // smaller (here by 29px). Scroll events dispatch asynchronously, so this
    // RO callback is the first code to see either change.
    act(() => {
      state.scrollHeight = 1972
      state.scrollTop = 1572 // the layout engine's clamp, NOT a user scroll
      state.clientHeight = 371
      fireViewport(el)
    })

    // The whole gap is ours — a clamp plus our own viewport shrink — so follow
    // must hold and the pin must land on the new bottom. Judged against the
    // just-applied box instead, the clamp (scrollTop below our last write) and
    // the shrink-inflated distance together carried a user-scroll-up
    // signature: follow released, no re-pin ran, and the content settled a
    // card-height low for the rest of the animation.
    expect(el.scrollTop).toBe(1972 - 371)
  })

  it('still releases follow when the user scrolls up during a viewport shrink', () => {
    const { el, state } = mount('viewport-shrink-userup', { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 }, mkItems(10))
    expect(el.scrollTop).toBe(1600)

    // Same tick, but 200px of the gap is a real drag. The allowance covers
    // only the box's own 29px, so the remainder still reads as user input.
    act(() => {
      state.clientHeight = 371
      state.scrollTop = 1400
      fireViewport(el)
    })
    expect(el.scrollTop).toBe(1400)
  })

  it('defers per-frame writes during the rail collapse and re-pins once at settle', () => {
    const { el, state, writes } = mount('viewport-rail', { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 }, mkItems(10))
    expect(el.scrollTop).toBe(1600)
    const before = writes.n

    // Rail collapse arms the settle window; the shell grid animation resizes
    // the scroller's box every frame. None of those frames may write scrollTop.
    act(() => { setRailWidth(railWidthFor({ isMobile: false, collapsed: true })) })
    act(() => {
      for (let i = 0; i < 8; i++) {
        state.clientHeight = 400 - i // width-driven reflow jitters the box
        fireViewport(el)
      }
    })
    expect(writes.n).toBe(before)

    // One re-pin when the settle window closes (we were following).
    act(() => { state.clientHeight = 340; vi.advanceTimersByTime(RAIL_SETTLE_MS + 1) })
    expect(el.scrollTop).toBe(2000 - 340)
  })
})

// The iOS toolbar case. Safari's URL bar collapses under exactly the DOWNWARD
// drag that scrolls toward the bottom, so for the frames of that animation the
// scroller GROWS while the reader moves: the bottom comes up to meet them. The
// scroll handler's re-engage band used to be judged against the already-grown
// box, so a reader who nudged down a few px from well outside the band was read
// as arriving inside it and follow re-armed -- for someone who never reached the
// bottom. The next automatic pin (here: the toolbar re-showing, which shrinks the
// box and re-pins a follower) then carried them to the end: the reported yank.
// A viewport SHRINK on its own never moves a released reader -- every pin is
// follow-gated -- which "does NOT move a user who scrolled up" above already pins.
describe('useVirtualChat: iOS toolbar collapse under a downward drag', () => {
  let origRO: typeof ResizeObserver | undefined
  let origRaf: typeof requestAnimationFrame

  beforeEach(() => {
    localStorage.clear()
    __resetRailWidth()
    __resetComposerResizeMark()
    FakeResizeObserver.instances = []
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    globalThis.ResizeObserver = origRO as typeof ResizeObserver
    globalThis.requestAnimationFrame = origRaf
    __resetRailWidth()
    __resetComposerResizeMark()
  })

  function viewportRO(el: HTMLElement): FakeResizeObserver {
    const inst = FakeResizeObserver.instances.find((i) => i.observed.has(el))
    expect(inst).toBeDefined()
    return inst!
  }
  function fireViewport(el: HTMLElement) {
    viewportRO(el).fire([{ target: el } as Partial<ResizeObserverEntry>])
  }
  /** A live turn: the idle rule would otherwise release a wrongly re-armed
   *  follow before it could pin, hiding the re-arm behind a second guard. */
  function mount(sessionId: string, geom: Geom, items: Item[]) {
    const { el, state, writes } = makeScroller(geom)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const baseProps: UseVirtualChatOptions<Item> = { items, sessionId, getKey, externalScrollerRef: ref, runActive: true }
    const view = renderHook((p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p), { initialProps: baseProps })
    act(() => { vi.advanceTimersByTime(250) })
    return { el, state, view, writes, baseProps }
  }
  /** Reader scrolls up to read, then comes part-way back: released, parked
   *  `above` px above the bottom, settled. */
  function parkAbove(el: HTMLElement, state: Geom, above: number) {
    act(() => { state.scrollTop = 600; el.dispatchEvent(new Event('scroll')) })
    act(() => { vi.advanceTimersByTime(400) })
    act(() => { state.scrollTop = state.scrollHeight - state.clientHeight - above; el.dispatchEvent(new Event('scroll')) })
    act(() => { vi.advanceTimersByTime(400) })
  }

  it('a nudge down while the toolbar collapses does not re-arm follow, so the toolbar re-showing does not pin', () => {
    // Phone with the URL bar showing: 340px of transcript.
    const { el, state, view, writes } = mount('ios-bar-collapse', { scrollTop: 0, scrollHeight: 2000, clientHeight: 340 }, mkItems(10))
    parkAbove(el, state, 60)
    expect(view.result.current.getFollow()).toBe(false)
    expect(el.scrollTop).toBe(1600)
    const before = writes.n

    // One frame of the collapse: the reader's drag moves them 3px down and the
    // bar's animation grows the box 50px. Live distance 2000 - 1603 - 390 = 7px,
    // inside the re-engage band -- 50 of those 53px were the browser's.
    act(() => {
      state.clientHeight = 390
      state.scrollTop = 1603
      el.dispatchEvent(new Event('scroll'))
      fireViewport(el)
    })

    // The bar re-shows (the reader scrolls up a hair, or taps the top): the box
    // shrinks back. A follower is re-pinned here; this reader must not be.
    act(() => { vi.advanceTimersByTime(400) })
    act(() => {
      state.clientHeight = 340
      fireViewport(el)
    })
    expect(el.scrollTop).toBe(1603)
    expect(writes.n).toBe(before)
    expect(view.result.current.getFollow()).toBe(false)
  })

  it('a collapse animated over several frames is credited in full, not one frame at a time', () => {
    // Safari does not collapse the bar in one step: the box grows across the
    // frames of the drag, and each frame's scroll event carries only THAT
    // frame's growth. Reader parked 60px up; three frames of the collapse each
    // move them 3px while the box grows 38, 8, then 1px. After the third
    // frame the live distance is 2000 - 1609 - 387 = 4px. Judged on the frame
    // alone the third's 3px of travel against 1px of growth is the reader's
    // arrival, and follow re-arms on the tail of every real collapse; on the
    // gesture the reader moved 9px of the 56px approach against the box's
    // 47px, so the toolbar re-showing must leave them where they are.
    const { el, state, view, writes } = mount('ios-bar-collapse-frames', { scrollTop: 0, scrollHeight: 2000, clientHeight: 340 }, mkItems(10))
    parkAbove(el, state, 60)
    expect(view.result.current.getFollow()).toBe(false)
    expect(el.scrollTop).toBe(1600)
    const before = writes.n

    const frames: Array<[clientHeight: number, scrollTop: number]> = [[378, 1603], [386, 1606], [387, 1609]]
    for (const [clientHeight, scrollTop] of frames) {
      act(() => {
        state.clientHeight = clientHeight
        state.scrollTop = scrollTop
        el.dispatchEvent(new Event('scroll'))
        fireViewport(el)
        vi.advanceTimersByTime(16)
      })
      expect(view.result.current.getFollow()).toBe(false)
    }

    act(() => { vi.advanceTimersByTime(400) })
    act(() => {
      state.clientHeight = 340
      fireViewport(el)
    })
    expect(el.scrollTop).toBe(1609)
    expect(writes.n).toBe(before)
    expect(view.result.current.getFollow()).toBe(false)
  })

  it('growth from a collapse the reader sat through is not credited to a later, separate nudge', () => {
    // The window has to close, or the credit leaks: the bar collapses fully
    // under a STILL reader (neutral events, so nothing re-arms), they rest,
    // and well after the settle window they drag themselves down into the
    // band. That approach is entirely theirs and re-engages.
    const { el, state, view } = mount('ios-bar-collapse-lapsed', { scrollTop: 0, scrollHeight: 2000, clientHeight: 340 }, mkItems(10))
    parkAbove(el, state, 80)
    expect(view.result.current.getFollow()).toBe(false)
    expect(el.scrollTop).toBe(1580)

    for (const clientHeight of [360, 380, 390]) {
      act(() => {
        state.clientHeight = clientHeight
        el.dispatchEvent(new Event('scroll'))
        fireViewport(el)
        vi.advanceTimersByTime(16)
      })
    }
    // Distance is now 2000 - 1580 - 390 = 30px, follow still released.
    expect(view.result.current.getFollow()).toBe(false)

    act(() => { vi.advanceTimersByTime(400) })
    // A fresh downward drag of 20px lands 10px from the bottom: inside the
    // band by the reader's own hand.
    act(() => {
      state.scrollTop = 1600
      el.dispatchEvent(new Event('scroll'))
    })
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('growth that landed while the reader rested, with no scroll event of its own, is not charged to their next drag', () => {
    // Parked far above the bottom, the keyboard closes (or the window grows):
    // the box grows by 50px and NO scroll event fires, because nothing clamped.
    // The reader then drags down in two frames to within the band. That drag
    // is theirs, so it must re-engage -- the rest-period growth must not sit in
    // the gesture total and hold the band out of reach for the whole drag.
    const { el, state, view } = mount('ios-rest-growth', { scrollTop: 0, scrollHeight: 2000, clientHeight: 340 }, mkItems(10))
    parkAbove(el, state, 200)
    expect(view.result.current.getFollow()).toBe(false)
    expect(el.scrollTop).toBe(1460)

    // Rest growth: viewport branch only, no scroll event.
    act(() => {
      state.clientHeight = 390
      fireViewport(el)
      vi.advanceTimersByTime(400)
    })
    expect(view.result.current.getFollow()).toBe(false)

    // Fresh drag: 1460 -> 1560 -> 1600. Frame 2 lands 2000 - 1600 - 390 = 10px
    // from the bottom, inside the band, with no viewport change in the gesture.
    act(() => {
      state.scrollTop = 1560
      el.dispatchEvent(new Event('scroll'))
      vi.advanceTimersByTime(16)
    })
    expect(view.result.current.getFollow()).toBe(false)
    act(() => {
      state.scrollTop = 1600
      el.dispatchEvent(new Event('scroll'))
    })
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('a reader who drags all the way down while the toolbar collapses re-engages and is pinned through the re-show', () => {
    const { el, state, view } = mount('ios-bar-collapse-arrive', { scrollTop: 0, scrollHeight: 2000, clientHeight: 340 }, mkItems(10))
    parkAbove(el, state, 200)
    expect(view.result.current.getFollow()).toBe(false)
    expect(el.scrollTop).toBe(1460)

    // Safari's real 50px collapse, and the reader's own 148px drag is what
    // reaches the bottom: live distance 2000 - 1608 - 390 = 2px. Judged
    // against the pre-growth box that is 52px -- past the band, as is every
    // position the grown box lets them reach -- so "return to live" was refused
    // for the whole collapse. They closed 148 of the 198px approach themselves.
    act(() => {
      state.clientHeight = 390
      state.scrollTop = 1608
      el.dispatchEvent(new Event('scroll'))
      fireViewport(el)
    })
    expect(view.result.current.getFollow()).toBe(true)

    act(() => { vi.advanceTimersByTime(400) })
    act(() => {
      state.clientHeight = 340
      fireViewport(el)
    })
    expect(el.scrollTop).toBe(2000 - 340)
  })

  it('a released reader clamped flush by the growth stays released', () => {
    // The growth alone (no reader move in the frame) drops the maximum under a
    // reader parked 20px up; the engine clamps them flush. Arriving is not
    // asking, so the toolbar re-showing leaves them 60px above the new bottom.
    const { el, state, view, writes } = mount('ios-bar-collapse-clamp', { scrollTop: 0, scrollHeight: 2000, clientHeight: 340 }, mkItems(10))
    parkAbove(el, state, 20)
    expect(view.result.current.getFollow()).toBe(false)
    expect(el.scrollTop).toBe(1640)

    act(() => {
      state.clientHeight = 400
      state.scrollTop = 1600 // the layout engine's clamp, not a user scroll
      el.dispatchEvent(new Event('scroll'))
      fireViewport(el)
    })
    expect(view.result.current.getFollow()).toBe(false)
    const before = writes.n

    act(() => { vi.advanceTimersByTime(400) })
    act(() => {
      state.clientHeight = 340
      fireViewport(el)
    })
    expect(el.scrollTop).toBe(1600)
    expect(writes.n).toBe(before)
  })

  it('a nudge the collapse clamps FLUSH does not re-arm follow either', () => {
    // The nudge case above lands 7px up, inside the band, and rule 3 refuses
    // it. Parked 60px up the box's new maximum is only 10px past the reader,
    // so a nudge of 10px or more is clamped flush instead -- distance 0, a
    // downward move -- and reaches the bottom-epsilon branch. That branch must
    // apply the same travel-against-growth test, or most of the nudge range
    // re-arms follow and the toolbar re-showing takes them to the end.
    const { el, state, view, writes } = mount('ios-bar-collapse-flush-nudge', { scrollTop: 0, scrollHeight: 2000, clientHeight: 340 }, mkItems(10))
    parkAbove(el, state, 60)
    expect(view.result.current.getFollow()).toBe(false)
    expect(el.scrollTop).toBe(1600)
    const before = writes.n

    // 12px nudge, 50px collapse: the engine stops the scroller at the new
    // maximum 2000 - 390 = 1610. 10 of the 12 asked-for pixels moved.
    act(() => {
      state.clientHeight = 390
      state.scrollTop = 1610
      el.dispatchEvent(new Event('scroll'))
      fireViewport(el)
    })
    expect(view.result.current.getFollow()).toBe(false)

    act(() => { vi.advanceTimersByTime(400) })
    act(() => {
      state.clientHeight = 340
      fireViewport(el)
    })
    expect(el.scrollTop).toBe(1610)
    expect(writes.n).toBe(before)
    expect(view.result.current.getFollow()).toBe(false)
  })

  it('growth that lands under our own pins mid-turn is not charged to the reader\'s later gesture', () => {
    // A follower during a streaming turn: every tail-row resize pins, and each
    // pin fires a scroll event of OUR making well inside the settle window. The
    // keyboard closes mid-turn (the box grows 50px, no reader scroll), the
    // reader flicks up to check something, and reverses back down within the
    // band a frame later. That approach is entirely theirs and must
    // re-engage. When our pins counted as "a gesture in flight" the growth
    // sat in the gesture total for the rest of the turn and refused them:
    // follow stayed released and the output streamed past.
    const { el, state, view, baseProps } = mount('ios-midturn-growth', { scrollTop: 0, scrollHeight: 2000, clientHeight: 340 }, mkItems(10))
    expect(view.result.current.getFollow()).toBe(true)
    expect(el.scrollTop).toBe(1660)

    let items = mkItems(10)
    /** One streaming tick: a row appends, the bottom moves, pinAuto writes,
     *  and the write's own scroll event dispatches ~50ms after the last. */
    const streamTick = () => {
      act(() => {
        state.scrollHeight += 40
        items = mkItems(items.length + 1)
        view.rerender({ ...baseProps, items })
      })
      expect(el.scrollTop).toBe(state.scrollHeight - state.clientHeight)
      act(() => {
        el.dispatchEvent(new Event('scroll'))
        vi.advanceTimersByTime(50)
      })
    }
    for (let i = 0; i < 3; i++) streamTick()
    expect(view.result.current.getFollow()).toBe(true)

    // Keyboard closes between two pins: viewport branch only, no scroll event.
    act(() => {
      state.clientHeight = 390
      fireViewport(el)
    })
    for (let i = 0; i < 3; i++) streamTick()
    expect(view.result.current.getFollow()).toBe(true)
    const bottom = state.scrollHeight - state.clientHeight
    expect(el.scrollTop).toBe(bottom)

    // The reader flicks up 30px: released.
    act(() => {
      state.scrollTop = bottom - 30
      el.dispatchEvent(new Event('scroll'))
    })
    expect(view.result.current.getFollow()).toBe(false)

    // ...and reverses 20px down a frame later, landing 10px from the bottom.
    // No viewport change in THEIR gesture, so this is the reader's own arrival.
    act(() => {
      vi.advanceTimersByTime(40)
      state.scrollTop = bottom - 10
      el.dispatchEvent(new Event('scroll'))
    })
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('growth across one of OUR scroll events inside the reader\'s gesture is dropped, not charged to them', () => {
    // The reader is scrolling down and taps jump-to-latest mid-gesture. The
    // tap blurs the composer, so the keyboard dismisses in the same frames:
    // the box grows 50px while our instant pin fires its own scroll event. The
    // pin is not their gesture, so the growth that arrives across it must not
    // land in the gesture total -- a flick up and straight back down within
    // the band is still their own arrival.
    const { el, state, view } = mount('ios-pin-inside-gesture', { scrollTop: 0, scrollHeight: 2000, clientHeight: 340 }, mkItems(10))
    parkAbove(el, state, 300)
    expect(view.result.current.getFollow()).toBe(false)
    expect(el.scrollTop).toBe(1360)

    // Reader nudges 10px down: a gesture opens, with 10px of travel in it.
    act(() => {
      state.scrollTop = 1370
      el.dispatchEvent(new Event('scroll'))
      vi.advanceTimersByTime(30)
    })
    expect(view.result.current.getFollow()).toBe(false)
    // Keyboard dismisses (viewport branch, gesture in flight so no re-baseline)
    // and our jump-to-latest pin writes the new bottom next frame and fires
    // its own scroll event.
    act(() => {
      state.clientHeight = 390
      fireViewport(el)
      view.result.current.scrollToBottom()
      vi.advanceTimersByTime(20)
    })
    expect(el.scrollTop).toBe(2000 - 390)
    act(() => {
      el.dispatchEvent(new Event('scroll'))
      vi.advanceTimersByTime(30)
    })
    expect(view.result.current.getFollow()).toBe(true)

    // Still inside their gesture: flick 30px up (released), then 20px back
    // down to 10px from the bottom. Their gesture holds 30px of travel against
    // no growth of theirs, so this re-engages; charged the 50px the pin's
    // event saw, 30 < 50 would refuse them.
    act(() => {
      state.scrollTop = 1610 - 30
      el.dispatchEvent(new Event('scroll'))
      vi.advanceTimersByTime(30)
    })
    expect(view.result.current.getFollow()).toBe(false)
    act(() => {
      state.scrollTop = 1610 - 10
      el.dispatchEvent(new Event('scroll'))
    })
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('growth that lands right after the turn\'s LAST pin is re-baselined at rest, not held for the reader\'s next move', () => {
    // The turn ends on a pin, and within the settle window of that pin's own
    // scroll event the keyboard closes: the box grows 50px with no scroll event
    // of anyone's (a flush follower has nothing to clamp). The observer is the
    // only code that sees it. Keyed on ANY scroll event it read the pin as a
    // gesture still in flight and left the baseline alone, so the reader's
    // first move seconds later -- a flick up and back -- inherited the 50px as
    // its own growth and was refused its arrival.
    const { el, state, view, baseProps } = mount('ios-last-pin-growth', { scrollTop: 0, scrollHeight: 2000, clientHeight: 340 }, mkItems(10))
    let items = mkItems(10)
    for (let i = 0; i < 3; i++) {
      act(() => {
        state.scrollHeight += 40
        items = mkItems(items.length + 1)
        view.rerender({ ...baseProps, items })
      })
      act(() => {
        el.dispatchEvent(new Event('scroll'))
        vi.advanceTimersByTime(50)
      })
    }
    expect(view.result.current.getFollow()).toBe(true)
    const bottom = state.scrollHeight - 390
    // 50ms after the last pin's scroll event: the keyboard closes.
    act(() => {
      state.clientHeight = 390
      fireViewport(el)
      vi.advanceTimersByTime(400)
    })
    // The engine holds a flush follower flush; mirror that in the fake.
    state.scrollTop = bottom

    act(() => {
      state.scrollTop = bottom - 30
      el.dispatchEvent(new Event('scroll'))
    })
    expect(view.result.current.getFollow()).toBe(false)
    act(() => {
      vi.advanceTimersByTime(40)
      state.scrollTop = bottom - 10
      el.dispatchEvent(new Event('scroll'))
    })
    expect(view.result.current.getFollow()).toBe(true)
  })
})

describe('viewport GROWTH is left to the engine', () => {
  it('does not write when the scroller grows under a followed reader', () => {
    // The clamp already holds a flush reader flush; a write here would also fire
    // for a reader parked above the bottom, which is the deletion yank.
    const src = readFileSync(join(__dirname, '..', 'hooks', 'virtualizer', 'useVirtualChat.ts'), 'utf8')
    const branch = src.slice(src.indexOf('if (entry.target === el) {'))
    const head = branch.slice(0, branch.indexOf('viewportResized = true'))
    // The skipped direction is GROWTH (`>`), not shrink: reversing this comparison
    // is what made typing walk the transcript and deleting jump to the bottom.
    expect(head).toMatch(/if \(prevCh > 0 && el\.clientHeight > prevCh\) continue/)
    expect(head).not.toMatch(/el\.clientHeight < prevCh\) continue/)
  })
})

describe('a composer-caused shrink is not followed', () => {
  it('skips the pin when the composer explains the viewport change', () => {
    // The reported phone defect: typing grows the composer, which shrinks the
    // scroller, and following that walks the transcript up a line every few
    // characters. Chrome mounting below the transcript is the SAME geometry with a
    // different cause and must still re-pin — so the branch consults the cause.
    const src = readFileSync(join(__dirname, '..', 'hooks', 'virtualizer', 'useVirtualChat.ts'), 'utf8')
    const branch = src.slice(src.indexOf('if (entry.target === el) {'))
    const head = branch.slice(0, branch.indexOf('viewportResized = true'))
    expect(head).toMatch(/if \(composerExplainsViewportChange\(\)\) continue/)
  })

  it('the composer autosizer is what publishes that cause', () => {
    // Both ends must exist or the guard above is permanently false and the pin
    // simply never fires for anyone.
    const input = readFileSync(join(__dirname, '..', 'components', 'ChatInput.tsx'), 'utf8')
    expect(input).toMatch(/markComposerResize\(\)/)
    expect(input).toMatch(/from '\.\.\/utils\/composerResize'/)
  })
})
