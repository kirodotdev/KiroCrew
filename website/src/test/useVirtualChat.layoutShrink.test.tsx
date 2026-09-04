// Feature: chat-virtualizer — a layout-driven scrollTop drop must not disarm
// follow, while a scroll that lands away from the bottom still must.
//
// Reproduces a captured production sequence. Mid-turn the content shrank 227px
// and the browser clamped scrollTop by the same 227px, leaving the viewport
// still exactly at the bottom. That clamp dispatches a `scroll` event which is
// NOT a self-scroll (it lands 227px from the value we wrote), so the handler
// ran — but it left the self-scroll reference pointing at our last write. The
// next pin evaluation therefore read a 227px gap as a user scroll-up, released
// follow, and never pinned again: scrollTop sat frozen for 20s while content
// grew, and only a manual scroll back to the bottom re-armed it.
//
// jsdom has no layout engine, so scroll geometry is faked on a detached scroller
// passed via `externalScrollerRef`, and the pin is driven through the append
// layout effect (an itemCount increase) rather than a ResizeObserver — the same
// technique the integration suite uses. Neither case dispatches an input event:
// the discriminator is WHERE the scroll lands, not what caused it, which is what
// keeps keyboard scrolling and wheels over widget iframes working (their events
// never reach this element).

import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'

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
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { state.scrollTop = o.top }
  return { el, state }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

// The captured numbers: pinned at 10318 of an 11462 scrollHeight over a 1144
// viewport, then a 227px shrink with a matching clamp, then growth to 11441.
const CH = 1144
const START_SH = 11462
const BOTTOM = START_SH - CH        // 10318 — where the pin lands
const SHRUNK_SH = 11235
const CLAMPED = SHRUNK_SH - CH      // 10091 — still exactly at the bottom
const GROWN_SH = 11441
const GROWN_BOTTOM = GROWN_SH - CH  // 10297 — where follow should land
// GROWN_SH is below START_SH, so replaying the capture is a net 21px shrink.
// The in-band scroll-up case needs genuine growth instead, or positions within
// 21px of the old bottom sit past the new maximum and would be clamped anyway.
const GROWN_TRUE_SH = START_SH + 200

function mount(items: Item[], sessionId: string) {
  const { el, state } = makeScroller({ scrollTop: 0, scrollHeight: START_SH, clientHeight: CH })
  const ref: RefObject<HTMLDivElement | null> = { current: el }
  const initialProps: UseVirtualChatOptions<Item> = { items, sessionId, getKey, externalScrollerRef: ref }
  const view = renderHook((p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p), { initialProps })
  return { el, state, view, ref }
}

describe('useVirtualChat: a clamp at the bottom vs a scroll away from it', () => {
  beforeEach(() => localStorage.clear())

  it('keeps following when a content shrink clamps scrollTop at the bottom', () => {
    const { el, state, view, ref } = mount(mkItems(5), 'layout-clamp')
    expect(el.scrollTop).toBe(BOTTOM)

    // The layout engine's doing: content shrinks, the browser clamps scrollTop
    // by the same amount and dispatches a scroll. The viewport is still at the
    // bottom, so this must re-baseline rather than look like a scroll-up.
    act(() => {
      state.scrollHeight = SHRUNK_SH
      state.scrollTop = CLAMPED
      el.dispatchEvent(new Event('scroll'))
    })

    // Streaming resumes: a row appends and content grows past the clamp.
    act(() => {
      state.scrollHeight = GROWN_SH
      view.rerender({ items: mkItems(6), sessionId: 'layout-clamp', getKey, externalScrollerRef: ref })
    })

    expect(el.scrollTop).toBe(GROWN_BOTTOM)
  })

  it.each([3, 50, 100])('does not yank back a %ipx scroll-up inside the bottom band', (up) => {
    const { el, state, view, ref } = mount(mkItems(5), `small-nudge-${up}`)
    expect(el.scrollTop).toBe(BOTTOM)

    // Inside the 100px `atBottom` UI band. The upward move releases follow
    // (direction-aware decision), and evaluateAutoPin's scroll-up guard backs
    // it up — either way the position belongs to the user. Re-baselining on
    // that band instead of on the clamp would erase the guard's evidence and
    // the next append would pin to the bottom — the regression this locks out.
    // 3px is the smallest move that clears SELF_SCROLL_EPSILON and so is the
    // first that reaches this branch at all.
    act(() => { state.scrollTop = BOTTOM - up; el.dispatchEvent(new Event('scroll')) })

    act(() => {
      state.scrollHeight = GROWN_TRUE_SH
      view.rerender({ items: mkItems(6), sessionId: `small-nudge-${up}`, getKey, externalScrollerRef: ref })
    })

    expect(el.scrollTop).toBe(BOTTOM - up)
  })

  it('still releases follow for a scroll that lands away from the bottom', () => {
    const { el, state, view, ref } = mount(mkItems(5), 'scroll-away')
    expect(el.scrollTop).toBe(BOTTOM)

    // Away from the bottom by far more than bottomThreshold. No input event is
    // dispatched: a keyboard scroll or a wheel over a widget iframe produces
    // exactly this — a scroll event on the scroller and nothing else — and it
    // must still release follow.
    act(() => { state.scrollTop = 4000; el.dispatchEvent(new Event('scroll')) })

    act(() => {
      state.scrollHeight = GROWN_SH
      view.rerender({ items: mkItems(6), sessionId: 'scroll-away', getKey, externalScrollerRef: ref })
    })

    // Position preserved — no yank back to the bottom.
    expect(el.scrollTop).toBe(4000)
  })
})
