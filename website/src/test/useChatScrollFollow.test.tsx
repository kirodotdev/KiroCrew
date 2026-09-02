// Feature: shared chat scroll follow for plain (non-virtualized) scrollers.
//
// Pins the behaviours ChatPane (and the Crew Members thread it hosts) relies
// on after replacing its hand-rolled sentinel/message-hash scroll with the
// shared FollowController-backed hook:
//   1. a content-growth RO tick while following pins to the new bottom —
//      including growth on EARLIER rows, which the old message-hash missed,
//   2. a content SHRINK while following (turn collapse) re-pins instead of
//      stranding the viewport ("transcript suddenly got shorter"),
//   3. a genuine user scroll up releases follow, growth then never yanks,
//   4. returning to the bottom re-engages; scrollToBottom() force re-arms,
//   5. isAtBottom drives the jump pill: false once scrolled up, true again
//      after scrollToBottom().
//
// jsdom has no layout engine: geometry is faked via property descriptors on a
// real div (the followDisengage suite's technique), and RO ticks are driven
// through a stubbed ResizeObserver.

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import React, { useState } from 'react'
import { render, act, screen } from '@testing-library/react'

import { useChatScrollFollow } from '../app-sdk/useChatScrollFollow'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

function fakeGeom(el: HTMLElement, initial: Geom) {
  const state: Geom = { ...initial }
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { state.scrollTop = v },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  return state
}

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  cb: ResizeObserverCallback
  observed = new Set<Element>()
  constructor(cb: ResizeObserverCallback) { this.cb = cb; FakeResizeObserver.instances.push(this) }
  observe(el: Element) { this.observed.add(el) }
  unobserve(el: Element) { this.observed.delete(el) }
  disconnect() { this.observed.clear() }
  static fireAll() {
    for (const inst of FakeResizeObserver.instances) inst.cb([], inst as unknown as ResizeObserver)
  }
}

const CH = 400
const SH = 1000
const BOTTOM = SH - CH // 600

function Host({ resetKey, enabled }: { resetKey: string; enabled?: boolean }) {
  const follow = useChatScrollFollow({ resetKey, enabled })
  const [, force] = useState(0)
  return (
    <div>
      <div data-testid="scroller" ref={follow.scrollerRef} onScroll={follow.onScroll}>
        <div data-testid="content" ref={follow.contentRef} />
      </div>
      <span data-testid="at-bottom">{String(follow.isAtBottom)}</span>
      <button data-testid="jump" aria-label="jump" onClick={() => { follow.scrollToBottom(); force(n => n + 1) }} />
    </div>
  )
}

let origRO: typeof ResizeObserver | undefined

beforeEach(() => {
  FakeResizeObserver.instances = []
  origRO = globalThis.ResizeObserver
  globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
})

afterEach(() => {
  if (origRO) globalThis.ResizeObserver = origRO
})

function mount() {
  const view = render(<Host resetKey="slot-a" />)
  const scroller = screen.getByTestId('scroller')
  const state = fakeGeom(scroller, { scrollTop: 0, scrollHeight: SH, clientHeight: CH })
  return { view, scroller, state }
}

describe('useChatScrollFollow', () => {
  it('pins to the bottom on a content-growth RO tick while following', () => {
    const { state } = mount()
    act(() => { FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(BOTTOM)

    // Growth anywhere in the list (the geometry is the whole scroller's, so
    // this covers earlier-row growth the old tail-hash never saw).
    act(() => { state.scrollHeight = SH + 300; FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(SH + 300 - CH)
  })

  it('re-pins on a content SHRINK while following (turn collapse)', () => {
    const { state } = mount()
    act(() => { FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(BOTTOM)

    // Turn completes and collapses: scrollHeight drops; the browser clamps
    // scrollTop to the new max on its own, but the follow must survive it and
    // the next growth must still pin.
    act(() => {
      state.scrollHeight = SH - 200
      state.scrollTop = BOTTOM - 200 // browser clamp to new bottom
      FakeResizeObserver.fireAll()
    })
    expect(state.scrollTop).toBe(SH - 200 - CH)
    act(() => { state.scrollHeight = SH; FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(BOTTOM)
  })

  it('a turn-collapse SHRINK in the same tick as a viewport shrink keeps following', () => {
    // The sibling of the virtualizer's queue-band defect, on the plain
    // scroller: this hook's single observer watches the scroller's own box as
    // well as the content wrapper, so one tick can carry both a turn-collapse
    // content shrink (which clamps scrollTop below our last write) and a
    // viewport shrink (a pane drag, a keyboard, chrome mounting below). Judged
    // against the just-shrunk box the pair reads as a user scroll-up, and this
    // hook has no clamp re-baseline to fall back on, so follow released for
    // good and later growth never pinned again.
    const { state } = mount()
    act(() => { FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(BOTTOM)

    act(() => {
      state.scrollHeight = SH - 200
      state.scrollTop = BOTTOM - 200 // the layout engine's clamp, NOT a user scroll
      state.clientHeight = CH - 29 // the box shrinks in the same tick
      FakeResizeObserver.fireAll()
    })
    expect(state.scrollTop).toBe(SH - 200 - (CH - 29))

    // Follow is still armed, so the next growth pins.
    act(() => { state.scrollHeight = SH; FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(SH - (CH - 29))
  })

  it('a real scroll-up inside a viewport shrink still releases follow', () => {
    // The boundary: the allowance forgives only the box's own pixels.
    const { scroller, state } = mount()
    act(() => { FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(BOTTOM)

    act(() => {
      state.clientHeight = CH - 29
      state.scrollTop = BOTTOM - 200
      FakeResizeObserver.fireAll()
    })
    expect(state.scrollTop).toBe(BOTTOM - 200)
    act(() => { scroller.dispatchEvent(new Event('scroll')) })
    act(() => { state.scrollHeight = SH + 500; FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(BOTTOM - 200)
  })

  it('releases on user scroll up: growth no longer moves the viewport, pill shows', () => {
    const { scroller, state } = mount()
    act(() => { FakeResizeObserver.fireAll() })

    act(() => { state.scrollTop = 200; scroller.dispatchEvent(new Event('scroll', { bubbles: false })) })
    expect(screen.getByTestId('at-bottom').textContent).toBe('false')

    act(() => { state.scrollHeight = SH + 500; FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(200)
  })

  it('re-engages when the user returns to the bottom', () => {
    const { scroller, state } = mount()
    act(() => { FakeResizeObserver.fireAll() })
    act(() => { state.scrollTop = 200; scroller.dispatchEvent(new Event('scroll')) })
    act(() => { state.scrollTop = BOTTOM; scroller.dispatchEvent(new Event('scroll')) })
    expect(screen.getByTestId('at-bottom').textContent).toBe('true')

    act(() => { state.scrollHeight = SH + 100; FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(SH + 100 - CH)
  })

  it('scrollToBottom() force re-arms follow after a release', () => {
    const { scroller, state } = mount()
    act(() => { FakeResizeObserver.fireAll() })
    act(() => { state.scrollTop = 100; scroller.dispatchEvent(new Event('scroll')) })
    expect(screen.getByTestId('at-bottom').textContent).toBe('false')

    act(() => { screen.getByTestId('jump').click() })
    expect(state.scrollTop).toBe(BOTTOM)
    expect(screen.getByTestId('at-bottom').textContent).toBe('true')

    act(() => { state.scrollHeight = SH + 50; FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(SH + 50 - CH)
  })

  it('self-scroll events from our own pin never release follow', () => {
    const { scroller, state } = mount()
    act(() => { FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(BOTTOM)

    // The pin's own scroll event arrives with scrollTop == lastWriteTop.
    act(() => { scroller.dispatchEvent(new Event('scroll')) })
    act(() => { state.scrollHeight = SH + 40; FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(SH + 40 - CH)
  })

  it('enabled=false is fully inert even with refs attached: no mount pin, no RO, resize never yanks', () => {
    // The half-wired regression shape: a host with a mode (ChatEmbed's
    // startAtBottom=false) keeps its refs attached but disables the hook. A
    // top-anchored reader must never be pinned to the bottom by a resize.
    const view = render(<Host resetKey="slot-a" enabled={false} />)
    const scroller = screen.getByTestId('scroller')
    const state = fakeGeom(scroller, { scrollTop: 0, scrollHeight: SH, clientHeight: CH })

    // Disabled: no ResizeObserver is attached at all.
    const observed = FakeResizeObserver.instances.reduce((n, i) => n + i.observed.size, 0)
    expect(observed).toBe(0)
    expect(state.scrollTop).toBe(0) // no mount pin

    // A stray RO tick (none should exist) and a scroll event both leave the
    // reader's position alone.
    act(() => { FakeResizeObserver.fireAll() })
    act(() => { state.scrollTop = 120; scroller.dispatchEvent(new Event('scroll')) })
    act(() => { state.scrollHeight = SH + 400; FakeResizeObserver.fireAll() })
    expect(state.scrollTop).toBe(120)
    expect(screen.getByTestId('at-bottom').textContent).toBe('true') // frozen, drives no pill
    view.unmount()
  })
})
