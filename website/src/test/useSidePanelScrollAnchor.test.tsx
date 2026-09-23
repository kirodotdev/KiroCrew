// The artifact/activity side panel is a layout sibling of the transcript, so
// opening/closing it changes the scroller width, flips the height-cache bucket,
// and reprices the virtualizer from estimates. A reader scrolled up into history
// (follow released) is otherwise stranded — measured ~780px drift, scrollTop → 0
// closing a panel on a long session. useSidePanelScrollAnchor captures the top
// visible row before the reflow and restores it after the reprice settles.
//
// These tests model that sequence directly: capture at the old width, then over
// animation frames change the width AND the rows' rendered heights (the reprice),
// and assert scrollTop is corrected so the anchor row returns to its captured
// screen offset. They also pin what must NOT happen: no correction for a
// FOLLOWING reader (the bottom pin owns that case), no correction after a real
// scroll gesture (the reader chose a new spot), no correction after a session
// switch (the anchor names a different conversation), and the primary assertion
// is non-vacuous (an uncorrected landing is far from the captured offset).

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render as rtlRender, act } from '@testing-library/react'
import { useRef } from 'react'
import { useSidePanelScrollAnchor } from '../hooks/useSidePanelScrollAnchor'

const CLIENT_H = 400
const NARROW_H = 120 // row height while the panel is open (narrow, taller wraps)
const WIDE_H = 90 // row height after the panel closes (wide, shorter wraps)
const N = 30

// Per-index rendered height, mutated when the "reprice" happens.
let rowHeight = NARROW_H
// Live scroller width, mutated across the transition.
let scrollerWidth = 238

function rect(top: number, height: number): DOMRect {
  return {
    top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: top,
    toJSON() { return {} },
  } as DOMRect
}

function Harness({
  wantsMount, following, sessionKey = 's1',
}: {
  wantsMount: boolean
  following: boolean
  sessionKey?: string
}) {
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  useSidePanelScrollAnchor({
    wantsMount,
    scrollerRef,
    isFollowing: () => following,
    sessionKey,
  })
  return (
    <div ref={scrollerRef} data-scroller>
      {Array.from({ length: N }, (_, i) => (
        <div key={i} data-display-index={String(i)} />
      ))}
    </div>
  )
}

describe('useSidePanelScrollAnchor', () => {
  let restore: (() => void) | null = null
  let origRaf: typeof requestAnimationFrame
  let origCancel: typeof cancelAnimationFrame
  let frames: FrameRequestCallback[]
  const scrollTopRef = { current: 0 }
  let fakeNow = 0
  let origPerfNow: typeof performance.now

  function installFakeLayout(scroller: HTMLElement) {
    const proto = HTMLElement.prototype
    const origRect = proto.getBoundingClientRect
    proto.getBoundingClientRect = function (this: HTMLElement): DOMRect {
      if (this === scroller) return rect(0, CLIENT_H)
      if (this.parentElement === scroller && this.getAttribute('data-display-index') !== null) {
        const idx = Number(this.getAttribute('data-display-index'))
        return rect(idx * rowHeight - scrollTopRef.current, rowHeight)
      }
      return origRect.call(this)
    }
    Object.defineProperty(scroller, 'clientWidth', { configurable: true, get: () => scrollerWidth })
    Object.defineProperty(scroller, 'scrollTop', {
      configurable: true, get: () => scrollTopRef.current, set: (v: number) => { scrollTopRef.current = v },
    })
    restore = () => { proto.getBoundingClientRect = origRect }
  }

  function flushFrames(max = 60) {
    let n = 0
    while (frames.length && n < max) {
      const batch = frames
      frames = []
      fakeNow += 16 // advance one 60fps frame so the time-based settle gate elapses
      act(() => { batch.forEach((cb) => cb(0)) })
      n += 1
    }
  }

  beforeEach(() => {
    frames = []
    rowHeight = NARROW_H
    scrollerWidth = 238
    scrollTopRef.current = 0
    fakeNow = 1000
    origPerfNow = performance.now.bind(performance)
    performance.now = () => fakeNow
    origRaf = globalThis.requestAnimationFrame
    origCancel = globalThis.cancelAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { frames.push(cb); return frames.length }) as typeof requestAnimationFrame
    globalThis.cancelAnimationFrame = (() => {}) as typeof cancelAnimationFrame
  })

  afterEach(() => {
    restore?.()
    restore = null
    performance.now = origPerfNow
    globalThis.requestAnimationFrame = origRaf
    globalThis.cancelAnimationFrame = origCancel
  })

  /** Mount scrolled up, panel open (narrow). Returns the scroller and view.
   *  Reader parked mid-history at an exact row boundary: scrollTop = 12*120, so
   *  row 12's top sits at 0 (the unambiguous topmost visible row) and row 11's
   *  bottom is exactly at 0 (excluded by the > +2 test). Anchor === row 12 @ 0. */
  function mountScrolledUpOpen(following: boolean, sessionKey = 's1') {
    scrollTopRef.current = 12 * NARROW_H
    const view = rtlRender(
      <Harness wantsMount following={following} sessionKey={sessionKey} />,
    )
    const el = view.container.querySelector('[data-scroller]') as HTMLElement
    installFakeLayout(el)
    return { el, view }
  }

  function offsetOfRow(el: HTMLElement, index: string): number {
    const scRect = el.getBoundingClientRect()
    const row = el.querySelector(`[data-display-index="${index}"]`) as HTMLElement
    return row.getBoundingClientRect().top - scRect.top
  }

  it('restores the anchor row to its captured offset after the panel closes (widen + reprice)', () => {
    const { el, view } = mountScrolledUpOpen(false)
    // Close the panel: wantsMount -> false. The layout effect captures the top
    // row synchronously against the CURRENT (narrow) geometry (row 12 @ 0).
    act(() => {
      view.rerender(<Harness wantsMount={false} following={false} />)
    })
    // Now the width widens and rows reprice shorter over the next frames.
    scrollerWidth = 698
    rowHeight = WIDE_H
    // Without correction, row 12 would now sit at 12*90 - scrollTop(1440) = -360.
    flushFrames()
    // After correction, row 12 is back at ~0 (within a px).
    expect(Math.abs(offsetOfRow(el, '12') - 0)).toBeLessThan(2)
  })

  it('does NOT correct before the reprice-debounce window elapses (the timing gate)', () => {
    // The height-index rebuild is debounced 200ms behind the last width change,
    // so the correction must WAIT past that window — not fire ~20ms after the
    // width stops moving, when the offset looks stable only because the reprice
    // has not run yet. This pins the two reviewers' finding.
    const { el, view } = mountScrolledUpOpen(false)
    act(() => {
      view.rerender(<Harness wantsMount={false} following={false} />)
    })
    scrollerWidth = 698
    rowHeight = WIDE_H
    const before = scrollTopRef.current
    // Flush only ~5 frames (≈80ms elapsed) — inside the 200ms+slack window.
    flushFrames(5)
    // The correction has NOT fired yet: scrollTop untouched.
    expect(scrollTopRef.current).toBe(before)
    // Now let the debounce window fully elapse.
    flushFrames()
    // Correction has landed: row 12 back at its captured offset.
    expect(Math.abs(offsetOfRow(el, '12') - 0)).toBeLessThan(2)
    expect(scrollTopRef.current).not.toBe(before)
  })

  it('is non-vacuous: WITHOUT the correction the anchor row drifts far away', () => {
    // Same sequence but the reader is FOLLOWING, so the hook must NOT correct —
    // proving the assertion above distinguishes a corrected landing from a
    // no-op, and pinning that a follower's position is left to the bottom pin.
    const { el, view } = mountScrolledUpOpen(true)
    act(() => {
      view.rerender(<Harness wantsMount={false} following />)
    })
    scrollerWidth = 698
    rowHeight = WIDE_H
    flushFrames()
    // Uncorrected: 12*90 - 1440 = -360. Far from the captured 0.
    expect(offsetOfRow(el, '12')).toBe(-360)
    expect(Math.abs(offsetOfRow(el, '12') - 0)).toBeGreaterThan(300)
  })

  it('does not move scrollTop when the width never changes', () => {
    // A wantsMount flip that does not actually reflow the scroller (width
    // stable) must leave the reader exactly where they are.
    const { view } = mountScrolledUpOpen(false)
    const before = scrollTopRef.current
    act(() => {
      view.rerender(<Harness wantsMount={false} following={false} />)
    })
    flushFrames()
    expect(scrollTopRef.current).toBe(before)
  })

  it('abandons the correction when the reader scrolls during the settle window', () => {
    // A reader who wheels/keys/drags mid-transition chose a new position;
    // correcting would yank them back. A gesture must cancel the pending fix.
    const { el, view } = mountScrolledUpOpen(false)
    act(() => {
      view.rerender(<Harness wantsMount={false} following={false} />)
    })
    scrollerWidth = 698
    rowHeight = WIDE_H
    // Reader takes over before the reprice settles.
    act(() => { el.dispatchEvent(new Event('wheel')) })
    const afterGesture = scrollTopRef.current
    flushFrames()
    // scrollTop is left wherever the reader's gesture put it — untouched by us.
    expect(scrollTopRef.current).toBe(afterGesture)
    // And the uncorrected anchor row is NOT snapped back to 0.
    expect(offsetOfRow(el, '12')).toBe(-360)
  })

  it('abandons the correction when the session changes during the settle window', () => {
    // A session switch mid-settle means the transcript is now a different
    // conversation; the captured display-index names an unrelated row, so the
    // pending correction must be dropped rather than jumping the new session.
    const { view } = mountScrolledUpOpen(false, 's1')
    act(() => {
      view.rerender(<Harness wantsMount={false} following={false} sessionKey="s1" />)
    })
    scrollerWidth = 698
    rowHeight = WIDE_H
    const before = scrollTopRef.current
    // The active session flips to a different key before the reprice settles.
    act(() => {
      view.rerender(<Harness wantsMount={false} following={false} sessionKey="s2" />)
    })
    flushFrames()
    expect(scrollTopRef.current).toBe(before)
  })
})
