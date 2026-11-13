// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks; the
// Display panel's View section renders one (Language), so the same lightweight
// mock DisplayPanel.language.test.tsx uses stands in for it.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

/**
 * The "Show search in the top bar" Settings switch (Settings -> Display ->
 * View): it starts on, writes `mc-topbar-search` through usePersistedBool
 * ('1' / '0'), and a same-tab reader of that key follows it live, even when the
 * store refuses the write. The probe below stands in for useAppShell, which
 * reads the same key; usePersistedBool.test.ts covers the hook itself. The
 * header half (the trigger leaving, the focus-mode toggle moving to the right
 * group, the grid modifier, the shortcuts-off fallback) is in
 * App.topbarAndShortcuts.test.tsx, which carries the full-shell mocks.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import React from 'react'

import { renderWithProviders } from './helpers'
import { usePersistedBool } from '../hooks/usePersistedBool'

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

const KEY = 'mc-topbar-search'

/** A second reader of the key, mounted beside the switch as the header is. */
function HeaderProbe() {
  const [shown] = usePersistedBool(KEY, true)
  return <output data-testid="header-probe">{shown ? 'shown' : 'hidden'}</output>
}

function renderSwitchAndProbe() {
  renderWithProviders(<><DisplayPanel /><HeaderProbe /></>, { route: '/settings?tab=display&sub=view' })
  return screen.getByRole('switch', { name: 'Show search in the top bar' })
}

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.removeItem(KEY)
})

describe('Settings → Display → View switch', () => {
  it('sits under Translucent panels, starts on, and writes the setting both ways', () => {
    const toggle = renderSwitchAndProbe()
    const switches = screen.getAllByRole('switch')
    const glass = switches.findIndex(s => s.getAttribute('aria-label') === 'Translucent panels')
    expect(glass).toBeGreaterThanOrEqual(0)
    expect(switches.indexOf(toggle)).toBe(glass + 1)
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('header-probe')).toHaveTextContent('shown')

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(localStorage.getItem(KEY)).toBe('0')
    expect(screen.getByTestId('header-probe')).toHaveTextContent('hidden')

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(localStorage.getItem(KEY)).toBe('1')
    expect(screen.getByTestId('header-probe')).toHaveTextContent('shown')
  })

  it('still applies the choice to the page when the store refuses the write', () => {
    const toggle = renderSwitchAndProbe()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError')
    })
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByTestId('header-probe')).toHaveTextContent('hidden')
    // The mount write stored the default; the refused write left it there.
    expect(localStorage.getItem(KEY)).toBe('1')
  })
})
