import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import {
  useBottomTerminal,
  setBottomTerminalWidth,
  setBottomTerminalHeight,
  __resetBottomTerminal,
  MAX_VH,
  MAX_VW,
  MIN_WIDTH,
  MIN_HEIGHT,
  clampToViewport,
} from '../hooks/useBottomTerminal'

const setViewport = (w: number, h: number) => {
  Object.defineProperty(window, 'innerWidth', { value: w, configurable: true, writable: true })
  Object.defineProperty(window, 'innerHeight', { value: h, configurable: true, writable: true })
}

describe('useBottomTerminal viewport clamp', () => {
  const ORIG_W = window.innerWidth
  const ORIG_H = window.innerHeight

  beforeEach(() => {
    __resetBottomTerminal()
    localStorage.clear()
  })
  afterEach(() => {
    __resetBottomTerminal()
    setViewport(ORIG_W, ORIG_H)
  })

  it('clampToViewport limits width to MAX_VW fraction of viewport', () => {
    setViewport(1000, 800)
    expect(clampToViewport(2000, 'width')).toBe(Math.round(1000 * MAX_VW))
  })

  it('clampToViewport limits height to MAX_VH fraction of viewport', () => {
    setViewport(1000, 800)
    expect(clampToViewport(2000, 'height')).toBe(Math.round(800 * MAX_VH))
  })

  it('clampToViewport enforces minimum width', () => {
    setViewport(1000, 800)
    expect(clampToViewport(50, 'width')).toBe(MIN_WIDTH)
  })

  it('clampToViewport enforces minimum height', () => {
    setViewport(1000, 800)
    expect(clampToViewport(50, 'height')).toBe(MIN_HEIGHT)
  })

  it('first render returns clamped dimensions via fresh module load', async () => {
    // Persist oversized dimensions BEFORE the module loads
    localStorage.setItem('mc-bottom-terminal', JSON.stringify({
      open: true, height: 2000, width: 2000, position: 'right',
      tabs: [{ id: 'test-1' }], activeId: 'test-1',
    }))
    setViewport(1000, 800)

    // Reset the module registry so the next import re-runs module init
    vi.resetModules()
    const freshModule = await import('../hooks/useBottomTerminal')

    const { result } = renderHook(() => freshModule.useBottomTerminal())
    // Width clamped to 55% of 1000 = 550
    expect(result.current.width).toBe(Math.round(1000 * MAX_VW))
    // Height clamped to 72% of 800 = 576
    expect(result.current.height).toBe(Math.round(800 * MAX_VH))

    // Cleanup — reset the fresh module's state
    freshModule.__resetBottomTerminal()
  })

  it('re-clamps when the window is resized', () => {
    setViewport(2000, 1200)
    act(() => { setBottomTerminalWidth(1000) })
    act(() => { setBottomTerminalHeight(800) })

    const { result } = renderHook(() => useBottomTerminal())
    // On a 2000px viewport, 55% = 1100 — 1000 fits
    expect(result.current.width).toBe(1000)
    // On a 1200px viewport, 72% = 864 — 800 fits
    expect(result.current.height).toBe(800)

    // Shrink the viewport so the stored dimensions exceed the cap
    act(() => {
      setViewport(800, 600)
      window.dispatchEvent(new Event('resize'))
    })

    // Width should now be clamped to 55% of 800 = 440
    expect(result.current.width).toBe(Math.round(800 * MAX_VW))
    // Height should now be clamped to 72% of 600 = 432
    expect(result.current.height).toBe(Math.round(600 * MAX_VH))
  })

  it('resize reuses object reference when clamped values are unchanged', () => {
    setViewport(2000, 1200)
    act(() => { setBottomTerminalWidth(400) })
    act(() => { setBottomTerminalHeight(300) })

    const { result } = renderHook(() => useBottomTerminal())
    const first = result.current

    // Dispatch resize but viewport stays the same — clamped values unchanged
    act(() => { window.dispatchEvent(new Event('resize')) })

    // Same reference (no unnecessary re-render object allocation)
    expect(result.current).toBe(first)
  })

  it('does not clamp dimensions that already fit the viewport', () => {
    setViewport(2000, 1200)
    act(() => { setBottomTerminalWidth(400) })
    act(() => { setBottomTerminalHeight(300) })

    const { result } = renderHook(() => useBottomTerminal())
    expect(result.current.width).toBe(400)
    expect(result.current.height).toBe(300)
  })
})
