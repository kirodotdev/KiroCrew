// Radix Select needs pointer APIs jsdom lacks; the View section renders one.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

/**
 * Settings -> Display -> View: "Reopen where I left off" (#29497 decision).
 * Shown only in the installed app, off by default, stored per device under
 * STANDALONE_REOPEN_KEY ('1' / '0'), the key lib/standaloneLastRoute.ts reads.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import React from 'react'

import { renderWithProviders } from './helpers'
import { STANDALONE_REOPEN_KEY } from '../lib/standaloneLastRoute'

vi.mock('../hooks/ZoomProvider', () => ({
  useZoomCtx: () => ({
    zoom: 100, zoomSupported: true, zoomIn: vi.fn(), zoomOut: vi.fn(), reset: vi.fn(),
    family: 'sans', setFontFamily: vi.fn(), cycleFamily: vi.fn(),
  }),
}))
vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({
    preference: 'dark', setTheme: vi.fn(), colorTheme: 'default', setColorTheme: vi.fn(),
    allThemes: [{ value: 'default', label: 'Default', custom: false }], theme: 'dark',
    themeVersion: 0, themeSwitching: false, addCustomTheme: vi.fn(), deleteCustomTheme: vi.fn(),
    loadCustomThemes: vi.fn(),
  }),
  ThemeProvider: ({ children }: { children: React.ReactNode }) => children,
  CUSTOM_THEMES_CHANGED_EVENT: 'custom-themes-changed',
}))
vi.mock('../hooks/useUIMode', () => ({
  useUIMode: () => ({ uiMode: 'chat', setUIMode: vi.fn(), toggleUIMode: vi.fn() }),
  UIModeProvider: ({ children }: { children: React.ReactNode }) => children,
}))
vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({
    paletteColors: ['#ff0000', '#00ff00', '#0000ff'], colorMode: 'tint' as const,
    paletteName: 'trailhead', intensity: 'clear',
    boost: { activePct: [60, 60, 60], idlePct: [30, 30, 30] },
  }),
}))

import { DisplayPanel } from '../pages/settings/DisplayPanel'

const NAME = 'Reopen where I left off'

function setStandalone(on: boolean): void {
  const real = window.matchMedia
  vi.spyOn(window, 'matchMedia').mockImplementation((query: string) =>
    query === '(display-mode: standalone)'
      ? ({ matches: on, media: query, addEventListener: () => {}, removeEventListener: () => {} } as unknown as MediaQueryList)
      : real.call(window, query),
  )
}

function renderView() {
  renderWithProviders(<DisplayPanel />, { route: '/settings?tab=display&sub=view' })
}

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.removeItem(STANDALONE_REOPEN_KEY)
})

describe('Settings → Display → Reopen where I left off', () => {
  it('is not offered in a browser tab', () => {
    setStandalone(false)
    renderView()
    expect(screen.queryByRole('switch', { name: NAME })).toBeNull()
  })

  it('is offered in the installed app, starts off, and stores the choice per device', () => {
    setStandalone(true)
    renderView()
    const toggle = screen.getByRole('switch', { name: NAME })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(localStorage.getItem(STANDALONE_REOPEN_KEY)).toBe('1')
    fireEvent.click(toggle)
    expect(localStorage.getItem(STANDALONE_REOPEN_KEY)).toBe('0')
  })
})
