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
