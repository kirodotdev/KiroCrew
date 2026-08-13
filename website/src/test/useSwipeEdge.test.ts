import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useSwipeEdge } from '../hooks/useSwipeEdge'

function createTouchEvent(type: string, clientX: number, clientY = 0): TouchEvent {
  const touch = { clientX, clientY } as Touch
  const init: TouchEventInit = { bubbles: true }
  if (type === 'touchstart') init.touches = [touch]
  if (type === 'touchend' || type === 'touchcancel') init.changedTouches = [touch]
  return new TouchEvent(type, init)
}

describe('useSwipeEdge', () => {
  let el: HTMLDivElement
  let ref: { current: HTMLDivElement }

  beforeEach(() => {
    el = document.createElement('div')
    document.body.appendChild(el)
    ref = { current: el }
    Object.defineProperty(window, 'innerWidth', { writable: true, value: 400 })
  })

  it('fires onSwipe when swiping right from left edge zone', () => {
    const onSwipe = vi.fn()
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'left', edgeZone: 30, threshold: 60, onSwipe }))

    el.dispatchEvent(createTouchEvent('touchstart', 20))
    el.dispatchEvent(createTouchEvent('touchend', 100))
    expect(onSwipe).toHaveBeenCalledTimes(1)
  })

  it('does not fire when touch starts outside edge zone', () => {
    const onSwipe = vi.fn()
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'left', edgeZone: 30, threshold: 60, onSwipe }))

    el.dispatchEvent(createTouchEvent('touchstart', 50))
    el.dispatchEvent(createTouchEvent('touchend', 120))
    expect(onSwipe).not.toHaveBeenCalled()
  })

  it('does not fire when swipe distance is below threshold', () => {
    const onSwipe = vi.fn()
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'left', edgeZone: 30, threshold: 60, onSwipe }))

    el.dispatchEvent(createTouchEvent('touchstart', 10))
    el.dispatchEvent(createTouchEvent('touchend', 40))
    expect(onSwipe).not.toHaveBeenCalled()
  })

  it('does not fire when vertical movement exceeds horizontal', () => {
    const onSwipe = vi.fn()
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'left', edgeZone: 30, threshold: 60, onSwipe }))

    el.dispatchEvent(createTouchEvent('touchstart', 10, 0))
    el.dispatchEvent(createTouchEvent('touchend', 80, 200))
    expect(onSwipe).not.toHaveBeenCalled()
  })

  it('supports fractional edgeZone as percentage of screen width', () => {
    const onSwipe = vi.fn()
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'left', edgeZone: 0.35, threshold: 60, onSwipe }))

    // 0.35 * 400 = 140px zone. Touch at 130 is inside.
    el.dispatchEvent(createTouchEvent('touchstart', 130))
    el.dispatchEvent(createTouchEvent('touchend', 200))
    expect(onSwipe).toHaveBeenCalledTimes(1)
  })

  it('fractional edgeZone rejects touches outside percentage', () => {
    const onSwipe = vi.fn()
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'left', edgeZone: 0.35, threshold: 60, onSwipe }))

    // 0.35 * 400 = 140px zone. Touch at 150 is outside.
    el.dispatchEvent(createTouchEvent('touchstart', 150))
    el.dispatchEvent(createTouchEvent('touchend', 220))
    expect(onSwipe).not.toHaveBeenCalled()
  })

  it('supports right edge swipe', () => {
    const onSwipe = vi.fn()
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'right', edgeZone: 9999, threshold: 50, onSwipe }))

    el.dispatchEvent(createTouchEvent('touchstart', 300))
    el.dispatchEvent(createTouchEvent('touchend', 200))
    expect(onSwipe).toHaveBeenCalledTimes(1)
  })

  it('does not fire when disabled', () => {
    const onSwipe = vi.fn()
    renderHook(() => useSwipeEdge(ref, { enabled: false, edge: 'left', edgeZone: 30, threshold: 60, onSwipe }))

    el.dispatchEvent(createTouchEvent('touchstart', 10))
    el.dispatchEvent(createTouchEvent('touchend', 100))
    expect(onSwipe).not.toHaveBeenCalled()
  })

  it('resets tracking on touchcancel', () => {
    const onSwipe = vi.fn()
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'left', edgeZone: 30, threshold: 60, onSwipe }))

    el.dispatchEvent(createTouchEvent('touchstart', 10))
    el.dispatchEvent(new TouchEvent('touchcancel', { bubbles: true }))
    el.dispatchEvent(createTouchEvent('touchend', 100))
    expect(onSwipe).not.toHaveBeenCalled()
  })

  /**
   * A horizontally scrollable child of the swipe root, mirroring the sidebar's
   * board-view column strip (`overflow-x-auto` with off-screen columns).
   */
  function appendScroller(scrollLeft: number, scrollWidth = 900, clientWidth = 300): HTMLDivElement {
    const sc = document.createElement('div')
    sc.style.overflowX = 'auto'
    Object.defineProperty(sc, 'scrollWidth', { configurable: true, value: scrollWidth })
    Object.defineProperty(sc, 'clientWidth', { configurable: true, value: clientWidth })
    Object.defineProperty(sc, 'scrollLeft', { configurable: true, writable: true, value: scrollLeft })
    el.appendChild(sc)
    return sc
  }

  it('does not close the pane when panning a horizontal scroller that can reveal more', () => {
    const onSwipe = vi.fn()
    const sc = appendScroller(0)
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'right', edgeZone: 9999, threshold: 50, onSwipe }))

    expect(sc.scrollWidth - sc.clientWidth).toBe(600)
    sc.dispatchEvent(createTouchEvent('touchstart', 200))
    sc.dispatchEvent(createTouchEvent('touchend', 100))
    expect(onSwipe).not.toHaveBeenCalled()
  })

  it('does not close the pane when the scroller consumed the gesture', () => {
    const onSwipe = vi.fn()
    const sc = appendScroller(600)
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'right', edgeZone: 9999, threshold: 50, onSwipe }))

    sc.dispatchEvent(createTouchEvent('touchstart', 200))
    sc.scrollLeft = 540
    sc.dispatchEvent(createTouchEvent('touchend', 100))
    expect(onSwipe).not.toHaveBeenCalled()
  })

  it('closes the pane when the scroller is already at its end and did not move', () => {
    const onSwipe = vi.fn()
    const sc = appendScroller(600)
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'right', edgeZone: 9999, threshold: 50, onSwipe }))

    expect(sc.scrollLeft).toBe(sc.scrollWidth - sc.clientWidth)
    sc.dispatchEvent(createTouchEvent('touchstart', 200))
    sc.dispatchEvent(createTouchEvent('touchend', 100))
    expect(onSwipe).toHaveBeenCalledTimes(1)
  })

  it('closes the pane from a child that does not scroll horizontally', () => {
    const onSwipe = vi.fn()
    const plain = document.createElement('div')
    el.appendChild(plain)
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'right', edgeZone: 9999, threshold: 50, onSwipe }))

    expect(plain.scrollWidth - plain.clientWidth).toBe(0)
    plain.dispatchEvent(createTouchEvent('touchstart', 200))
    plain.dispatchEvent(createTouchEvent('touchend', 100))
    expect(onSwipe).toHaveBeenCalledTimes(1)
  })

  it('opens the pane from the left edge over a scroller already at its start', () => {
    const onSwipe = vi.fn()
    const sc = appendScroller(0)
    renderHook(() => useSwipeEdge(ref, { enabled: true, edge: 'left', edgeZone: 0.35, threshold: 60, onSwipe }))

    expect(sc.scrollLeft).toBe(0)
    sc.dispatchEvent(createTouchEvent('touchstart', 20))
    sc.dispatchEvent(createTouchEvent('touchend', 140))
    expect(onSwipe).toHaveBeenCalledTimes(1)
  })
})
