// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks; the
// Display panel's View section renders one (Language), so the same lightweight
// mock DisplayPanel.language.test.tsx uses stands in for it.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

/**
 * The "Reverse trackpad swipe" Settings switch (Settings -> Display -> View,
 * #38623): off by default, written as `kc-reverse-trackpad-swipe` through
 * usePersistedBool, and read back by lib/scrollTouch `naturalScrolling` as
 * "Natural scrolling is off" only when the desktop shell does not report the
 * setting. Hidden where the shell does report it. The swipe itself is covered
 * in hooks/useTrackpadSwipe.test.ts.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import React from 'react'

import { renderWithProviders } from './helpers'
import { REVERSE_TRACKPAD_SWIPE_KEY, naturalScrolling } from '../lib/scrollTouch'

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

const LABEL = 'Reverse trackpad swipe'

function renderPanel() {
  renderWithProviders(<DisplayPanel />, { route: '/settings?tab=display&sub=view' })
}

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.removeItem(REVERSE_TRACKPAD_SWIPE_KEY)
  delete (window as Window & { electronAPI?: unknown }).electronAPI
})

describe('Settings → Display → Reverse trackpad swipe', () => {
  it('starts off in a browser tab and turns the unknown Natural scrolling into off', () => {
    renderPanel()
    const toggle = screen.getByRole('switch', { name: LABEL })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(naturalScrolling()).toBeNull()

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(localStorage.getItem(REVERSE_TRACKPAD_SWIPE_KEY)).toBe('1')
    expect(naturalScrolling()).toBe(false)

    fireEvent.click(toggle)
    expect(localStorage.getItem(REVERSE_TRACKPAD_SWIPE_KEY)).toBe('0')
    expect(naturalScrolling()).toBeNull()
  })

  it('is hidden where the desktop shell reports Natural scrolling, and the shell wins over a stored switch', () => {
    localStorage.setItem(REVERSE_TRACKPAD_SWIPE_KEY, '1')
    ;(window as Window & { electronAPI?: unknown }).electronAPI = { getNaturalScrolling: () => true }
    renderPanel()
    expect(screen.queryByRole('switch', { name: LABEL })).toBeNull()
    expect(naturalScrolling()).toBe(true)
  })
})
