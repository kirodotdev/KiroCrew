/**
 * Two panels, one bound element — the composition ChatPage actually renders.
 *
 * The mobile chat pane binds `useDrawerSwipe` twice on the same container: once
 * for the sessions drawer on the left, once for the side panel on the right.
 * Nothing about a single instance can pin that arrangement, and it has exactly
 * one failure mode worth a test: BOTH instances see every touch, so whatever
 * separates them has to hold for every combination of state and direction.
 *
 * Two rules do the separating, and they cover different cases:
 *  - while both panels are CLOSED, DIRECTION does it — each instance rejects
 *    the direction that would open the other one;
 *  - once a panel is OPEN, direction can no longer tell them apart, because the
 *    drag that closes the open panel is the very drag that opens the other. The
 *    consumer resolves that by disabling the far instance while a panel is on
 *    screen, and this file pins the consequence rather than the wiring: with
 *    that gate in place, closing one panel must never open the other.
 */
import { renderHook, act, waitFor } from '@testing-library/react'
import { motionValue } from 'framer-motion'
import * as React from 'react'
import { useDrawerSwipe } from '../hooks/useDrawerSwipe'

function touch(type: string, clientX: number, clientY = 0, timeStamp = 0): TouchEvent {
  const t = { clientX, clientY } as Touch
  const init: TouchEventInit = { bubbles: true }
  if (type === 'touchstart' || type === 'touchmove') init.touches = [t]
  if (type === 'touchend' || type === 'touchcancel') init.changedTouches = [t]
  const e = new TouchEvent(type, init)
  Object.defineProperty(e, 'timeStamp', { value: timeStamp })
  return e
}

describe('useDrawerSwipe — two panels sharing one element', () => {
  let el: HTMLDivElement
  let ref: { current: HTMLDivElement }
  let leftX: ReturnType<typeof motionValue<number>>
  let rightX: ReturnType<typeof motionValue<number>>
  let openLeft: ReturnType<typeof vi.fn>
  let openRight: ReturnType<typeof vi.fn>
  let settleLeft: ReturnType<typeof vi.fn>
  let settleRight: ReturnType<typeof vi.fn>

  const W = 400

  beforeEach(() => {
    el = document.createElement('div')
    document.body.appendChild(el)
    ref = { current: el }
    leftX = motionValue(-W)
    rightX = motionValue(W)
    openLeft = vi.fn()
    openRight = vi.fn()
    settleLeft = vi.fn()
    settleRight = vi.fn()
    Object.defineProperty(window, 'innerWidth', { writable: true, value: W })
  })

  /** Both instances, gated exactly as ChatPage gates them: each is bound only
   *  while the OTHER panel is off screen. */
  function mountBoth({ leftOpen = false, rightOpen = false } = {}) {
    return renderHook(() => {
      useDrawerSwipe(ref, {
        enabled: !rightOpen,
        side: 'left',
        open: leftOpen,
        x: leftX,
        onGestureOpen: openLeft,
        onSettle: settleLeft,
      })
      useDrawerSwipe(ref, {
        enabled: !leftOpen,
        side: 'right',
        open: rightOpen,
        x: rightX,
        onGestureOpen: openRight,
        onSettle: settleRight,
      })
    })
  }

  function fire(e: TouchEvent) {
    act(() => { el.dispatchEvent(e) })
  }

  it('a rightward drag mid-pane opens the LEFT panel and only that one', () => {
    mountBoth()
    fire(touch('touchstart', 200))
    fire(touch('touchmove', 260))
    expect(openLeft).toHaveBeenCalledTimes(1)
    expect(openRight).not.toHaveBeenCalled()
    expect(leftX.get()).toBe(-W + 60)
    expect(rightX.get()).toBe(W)      // untouched, still parked
  })

  it('a leftward drag mid-pane opens the RIGHT panel and only that one', () => {
    mountBoth()
    fire(touch('touchstart', 200))
    fire(touch('touchmove', 140))
    expect(openRight).toHaveBeenCalledTimes(1)
    expect(openLeft).not.toHaveBeenCalled()
    expect(rightX.get()).toBe(W - 60)
    expect(leftX.get()).toBe(-W)
  })

  it('closing the open LEFT panel does not open the right one behind it', async () => {
    // The drag that closes the left drawer is, in isolation, a perfectly good
    // right-panel opening drag. Only the enabled gate keeps it from being both.
    leftX.set(0)
    mountBoth({ leftOpen: true })
    fire(touch('touchstart', 380, 0, 0))
    fire(touch('touchmove', 60, 0, 200))
    fire(touch('touchend', 60, 0, 400))
    await waitFor(() => expect(settleLeft).toHaveBeenCalledWith(false))
    expect(openRight).not.toHaveBeenCalled()
    expect(rightX.get()).toBe(W)
  })

  it('closing the open RIGHT panel does not open the drawer behind it', async () => {
    rightX.set(0)
    mountBoth({ rightOpen: true })
    fire(touch('touchstart', 20, 0, 0))
    fire(touch('touchmove', 340, 0, 200))
    fire(touch('touchend', 340, 0, 400))
    await waitFor(() => expect(settleRight).toHaveBeenCalledWith(false))
    expect(openLeft).not.toHaveBeenCalled()
    expect(leftX.get()).toBe(-W)
  })

  it('a vertical scroll is claimed by neither', () => {
    mountBoth()
    fire(touch('touchstart', 200))
    fire(touch('touchmove', 206, 60))   // dy dominates
    expect(openLeft).not.toHaveBeenCalled()
    expect(openRight).not.toHaveBeenCalled()
  })

  // ── Handing over mid-settle ────────────────────────────────────────────────
  // The gate above is stated as "while a panel is on screen", but the consumer
  // spells it as a PHASE, and a phase that only reaches 'closed' when the slide
  // finishes keeps the far instance unbound for the whole ~300ms. So a swipe
  // that dismissed one panel could not be followed straight away by a swipe
  // revealing the other — the user had to wait out an animation.
  //
  // These two model the consumer's own state machine rather than a static
  // boolean: 'open' | 'closing' | 'closed', gated on `!== 'open'`, with the
  // release decision arriving via `onCommit` instead of `onSettle`.
  type Phase = 'open' | 'closing' | 'closed'

  function mountPhased(initial: { left: Phase; right: Phase }) {
    const seen = { left: initial.left, right: initial.right }
    const view = renderHook(() => {
      const [left, setLeft] = React.useState<Phase>(initial.left)
      const [right, setRight] = React.useState<Phase>(initial.right)
      seen.left = left
      seen.right = right
      useDrawerSwipe(ref, {
        enabled: right !== 'open',
        side: 'left',
        open: left === 'open',
        x: leftX,
        onGestureOpen: () => { setLeft('open'); openLeft() },
        onCommit: open => { if (!open) setLeft('closing') },
        onSettle: open => { if (!open) setLeft('closed'); settleLeft(open) },
      })
      useDrawerSwipe(ref, {
        enabled: left !== 'open',
        side: 'right',
        open: right === 'open',
        x: rightX,
        onGestureOpen: () => { setRight('open'); openRight() },
        onCommit: open => { if (!open) setRight('closing') },
        onSettle: open => { if (!open) setRight('closed'); settleRight(open) },
      })
    })
    return { view, seen }
  }

  it('the RIGHT panel arms immediately after the left is swiped shut, mid-settle', () => {
    leftX.set(0)
    const { seen } = mountPhased({ left: 'open', right: 'closed' })
    // Swipe the open left drawer shut and release.
    fire(touch('touchstart', 300, 0, 0))
    fire(touch('touchmove', 100, 0, 200))
    fire(touch('touchend', 100, 0, 400))
    // Still sliding out — NOT yet arrived.
    expect(seen.left).toBe('closing')
    expect(settleLeft).not.toHaveBeenCalled()
    // A new leftward drag, without waiting for the slide: the right panel opens.
    fire(touch('touchstart', 300, 0, 500))
    fire(touch('touchmove', 240, 0, 516))
    expect(openRight).toHaveBeenCalledTimes(1)
    expect(rightX.get()).toBeLessThan(W)
  })

  it('while a panel is genuinely OPEN the far instance stays unbound', () => {
    // The control: the exclusion still has to hold for the case it exists for —
    // an open panel's closing drag is the other's opening drag.
    leftX.set(0)
    mountPhased({ left: 'open', right: 'closed' })
    fire(touch('touchstart', 300, 0, 0))
    fire(touch('touchmove', 240, 0, 16))
    expect(openRight).not.toHaveBeenCalled()
    expect(rightX.get()).toBe(W)
  })
})
