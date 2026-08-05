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
})
