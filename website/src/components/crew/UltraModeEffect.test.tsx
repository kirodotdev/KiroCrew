import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import UltraModeEffect, { ULTRA_EFFECT_FADE_MS, ULTRA_EFFECT_MS } from './UltraModeEffect'
import { CrewModeChip } from '../CrewModeChip'
import { fireUltraEffect } from '../../lib/ultraModeEffect'

const realMatchMedia = window.matchMedia

function setReducedMotion(reduce: boolean) {
  window.matchMedia = vi.fn((query: string) => ({
    matches: reduce && query.includes('prefers-reduced-motion: reduce'),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia
}

afterEach(() => {
  window.matchMedia = realMatchMedia
  vi.useRealTimers()
})

describe('UltraModeEffect', () => {
  it('draws nothing on mount, nor on a remount such as a chat switch', () => {
    setReducedMotion(false)
    const { unmount } = render(<UltraModeEffect />)
    expect(screen.queryByTestId('ultra-effect')).toBeNull()
    unmount()
    render(<UltraModeEffect />)
    expect(screen.queryByTestId('ultra-effect')).toBeNull()
  })

  it('plays the full effect once when Ultra Mode turns on, then takes its layer away', () => {
    vi.useFakeTimers()
    setReducedMotion(false)
    render(<UltraModeEffect />)
    act(() => { fireUltraEffect(new DOMRect(10, 20, 100, 30)) })
    const layer = screen.getByTestId('ultra-effect')
    expect(layer).toHaveAttribute('data-variant', 'full')
    expect(layer.className).toContain('pointer-events-none')
    expect(layer).toHaveAttribute('aria-hidden', 'true')
    expect(screen.getByTestId('ultra-effect-sweep')).toBeInTheDocument()
    expect(screen.getByTestId('ultra-effect-flash')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(ULTRA_EFFECT_MS) })
    expect(screen.queryByTestId('ultra-effect')).toBeNull()
  })

  it('glows the Board tab once it is there', () => {
    setReducedMotion(false)
    const tab = document.createElement('button')
    tab.setAttribute('data-tab-chip-id', 'workers')
    tab.getBoundingClientRect = () => new DOMRect(500, 10, 60, 24)
    document.body.appendChild(tab)
    try {
      render(<UltraModeEffect />)
      act(() => { fireUltraEffect(null) })
      expect(screen.getByTestId('ultra-effect-board')).toBeInTheDocument()
    } finally {
      tab.remove()
    }
  })

  it('under reduced motion is only a quick fade', () => {
    vi.useFakeTimers()
    setReducedMotion(true)
    render(<UltraModeEffect />)
    act(() => { fireUltraEffect(null) })
    expect(screen.getByTestId('ultra-effect')).toHaveAttribute('data-variant', 'fade')
    expect(screen.queryByTestId('ultra-effect-sweep')).toBeNull()
    expect(screen.queryByTestId('ultra-effect-flash')).toBeNull()
    act(() => { vi.advanceTimersByTime(ULTRA_EFFECT_FADE_MS) })
    expect(screen.queryByTestId('ultra-effect')).toBeNull()
  })

  it('the chip fires it on turning on only, never on turning off', () => {
    setReducedMotion(false)
    const seen = vi.fn()
    window.addEventListener('mc-ultra-mode-effect', seen)
    try {
      const { rerender } = render(<CrewModeChip onTurnOn={vi.fn()} onTurnOff={vi.fn()} />)
      expect(seen).not.toHaveBeenCalled()
      fireEvent.click(screen.getByTestId('crew-mode-chip'))
      expect(seen).toHaveBeenCalledTimes(1)
      rerender(<CrewModeChip onTurnOn={vi.fn()} onTurnOff={vi.fn()} pressed />)
      fireEvent.click(screen.getByTestId('crew-mode-chip'))
      expect(seen).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('mc-ultra-mode-effect', seen)
    }
  })
})
