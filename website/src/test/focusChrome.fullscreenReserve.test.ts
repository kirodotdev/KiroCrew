/**
 * Focus mode's top bar closes on POSITION: only a mousemove below the header
 * band hides it. In macOS fullscreen the shell sits `topReservePx` down the
 * window, so the band must move down with the header; otherwise resting on the
 * header's lower half slides the bar away under the pointer.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useFocusChrome } from '../shell/focus/focusChrome'
import { setFocusModeEnabled, __resetFocusMode } from '../hooks/useFocusMode'

const moveTo = (y: number) => act(() => {
  document.dispatchEvent(new MouseEvent('mousemove', { clientY: y, clientX: 400, bubbles: true }))
})

describe('useFocusChrome — top peek band follows the fullscreen reserve', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    __resetFocusMode()
    setFocusModeEnabled(true, { echo: false })
  })
  afterEach(() => {
    __resetFocusMode()
    vi.useRealTimers()
  })

  const openTopPeek = (topReservePx: number) => {
    const { result } = renderHook(() => useFocusChrome({
      isMobile: false, navCollapsed: false, activeInstanceId: null, topReservePx,
    }))
    act(() => { result.current.topPeek.triggerProps.onMouseEnter() })
    act(() => { vi.advanceTimersByTime(200) })
    expect(result.current.topPeek.open).toBe(true)
    return result
  }

  it('keeps the bar open over the shifted header and closes below it', () => {
    const result = openTopPeek(24)
    // y=60 is inside the header shifted to 24..66 (+6 slack): never a departure.
    moveTo(60)
    act(() => { vi.advanceTimersByTime(2000) })
    expect(result.current.topPeek.open).toBe(true)

    moveTo(80)
    act(() => { vi.advanceTimersByTime(2000) })
    expect(result.current.topPeek.open).toBe(false)
  })

  it('keeps the 48px band when there is no reserve', () => {
    const result = openTopPeek(0)
    moveTo(60)
    act(() => { vi.advanceTimersByTime(2000) })
    expect(result.current.topPeek.open).toBe(false)
  })
})
