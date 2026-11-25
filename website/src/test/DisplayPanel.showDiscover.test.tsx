// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks; the
// Display panel's View section renders one (Language), so the same lightweight
// mock DisplayPanel.language.test.tsx uses stands in for it.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

/**
 * The "Show Discover in the sidebar" Settings switch (Settings -> Display ->
 * View): it starts on, and turning it off puts Discover's nav id in the
 * sidebar hidden set the rails read (`mc-app-nav-hidden`); on takes it out
 * again and leaves the app pins in the set alone. The rail half (no Discover
 * row, the updates count on Library, /apps still opening) is in
 * App.discoverUpdatesBadge.test.tsx.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import React from 'react'

import { renderWithProviders } from './helpers'
import { APP_NAV_HIDDEN_KEY, readAppNavHidden } from '../lib/appNavHidden'

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

afterEach(() => {
  localStorage.removeItem(APP_NAV_HIDDEN_KEY)
})

describe('Settings → Display → View: Show Discover in the sidebar', () => {
  it('starts on and moves Discover in and out of the sidebar hidden set, keeping app pins', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify(['app-secretary']))
    renderWithProviders(<DisplayPanel />, { route: '/settings?tab=display&sub=view' })
    const toggle = screen.getByRole('switch', { name: 'Show Discover in the sidebar' })
    expect(toggle).toHaveAttribute('aria-checked', 'true')

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect([...readAppNavHidden()].sort()).toEqual(['app-secretary', 'apps'])

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect([...readAppNavHidden()]).toEqual(['app-secretary'])
  })
})
