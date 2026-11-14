import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import ConductorModeEffect, { CONDUCTOR_MODE_EFFECT_FADE_MS, CONDUCTOR_MODE_EFFECT_MS } from './ConductorModeEffect'
import { CrewModeChip } from '../CrewModeChip'
import { fireConductorModeEffect } from '../../lib/conductorModeEffect'

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

describe('ConductorModeEffect', () => {
  it('draws nothing on mount, nor on a remount such as a chat switch', () => {
    setReducedMotion(false)
    const { unmount } = render(<ConductorModeEffect />)
    expect(screen.queryByTestId('conductor-mode-effect')).toBeNull()
    unmount()
    render(<ConductorModeEffect />)
    expect(screen.queryByTestId('conductor-mode-effect')).toBeNull()
  })

  it('plays the full effect once when conductor mode turns on, then takes its layer away', () => {
    vi.useFakeTimers()
    setReducedMotion(false)
    render(<ConductorModeEffect />)
    act(() => { fireConductorModeEffect(new DOMRect(10, 20, 100, 30)) })
    const layer = screen.getByTestId('conductor-mode-effect')
    expect(layer).toHaveAttribute('data-variant', 'full')
    expect(layer.className).toContain('pointer-events-none')
    expect(layer).toHaveAttribute('aria-hidden', 'true')
    expect(screen.getByTestId('conductor-mode-effect-sweep')).toBeInTheDocument()
    expect(screen.getByTestId('conductor-mode-effect-flash')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(CONDUCTOR_MODE_EFFECT_MS) })
    expect(screen.queryByTestId('conductor-mode-effect')).toBeNull()
  })

  it('glows the Board tab once it is there', () => {
    setReducedMotion(false)
    const tab = document.createElement('button')
    tab.setAttribute('data-tab-chip-id', 'workers')
    tab.getBoundingClientRect = () => new DOMRect(500, 10, 60, 24)
    document.body.appendChild(tab)
    try {
      render(<ConductorModeEffect />)
      act(() => { fireConductorModeEffect(null) })
      expect(screen.getByTestId('conductor-mode-effect-board')).toBeInTheDocument()
    } finally {
      tab.remove()
    }
  })

  it('under reduced motion is only a quick fade', () => {
    vi.useFakeTimers()
    setReducedMotion(true)
    render(<ConductorModeEffect />)
    act(() => { fireConductorModeEffect(null) })
    expect(screen.getByTestId('conductor-mode-effect')).toHaveAttribute('data-variant', 'fade')
    expect(screen.queryByTestId('conductor-mode-effect-sweep')).toBeNull()
    expect(screen.queryByTestId('conductor-mode-effect-flash')).toBeNull()
    act(() => { vi.advanceTimersByTime(CONDUCTOR_MODE_EFFECT_FADE_MS) })
    expect(screen.queryByTestId('conductor-mode-effect')).toBeNull()
  })

  it('the chip fires it on turning on only, never on turning off', () => {
    setReducedMotion(false)
    const seen = vi.fn()
    window.addEventListener('mc-conductor-mode-effect', seen)
    try {
      const { rerender } = render(<CrewModeChip onTurnOn={vi.fn()} onTurnOff={vi.fn()} />)
      expect(seen).not.toHaveBeenCalled()
      fireEvent.click(screen.getByTestId('crew-mode-chip'))
      expect(seen).toHaveBeenCalledTimes(1)
      rerender(<CrewModeChip onTurnOn={vi.fn()} onTurnOff={vi.fn()} pressed />)
      fireEvent.click(screen.getByTestId('crew-mode-chip'))
      expect(seen).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('mc-conductor-mode-effect', seen)
    }
  })
})
