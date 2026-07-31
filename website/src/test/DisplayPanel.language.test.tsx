// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks — the
// same lightweight mock the SettingsSelect unit tests use, so options are real
// role="option" nodes we can read without opening a portal.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import React from 'react'

import { renderWithProviders } from './helpers'
import { LANG_STORAGE_KEY } from '../i18n/detect'

// DisplayPanel pulls in the zoom / theme / UI-mode / palette contexts; none of
// them matter here, so they are stubbed to their quiet defaults. Kept separate
// from DisplayPanel.test.tsx because the Radix mock above is file-scoped.
vi.mock('../hooks/ZoomProvider', () => ({
  useZoomCtx: () => ({
    zoom: 100,
    zoomSupported: true,
    zoomIn: vi.fn(),
    zoomOut: vi.fn(),
    reset: vi.fn(),
    family: 'sans',
    setFontFamily: vi.fn(),
    cycleFamily: vi.fn(),
  }),
}))

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({
    preference: 'dark',
    setTheme: vi.fn(),
    colorTheme: 'default',
    setColorTheme: vi.fn(),
    allThemes: [{ value: 'default', label: 'Default', custom: false }],
    theme: 'dark',
    themeVersion: 0,
    themeSwitching: false,
    addCustomTheme: vi.fn(),
    deleteCustomTheme: vi.fn(),
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
    paletteColors: ['#ff0000', '#00ff00', '#0000ff'],
    colorMode: 'tint' as const,
    paletteName: 'trailhead',
    intensity: 'clear',
    boost: { activePct: [60, 60, 60], idlePct: [30, 30, 30] },
  }),
}))

import { DisplayPanel } from '../pages/settings/DisplayPanel'

/** Open the Language dropdown and return the Auto row's text,
 *  e.g. "Auto — 简体中文". */
function autoOptionText(): string {
  fireEvent.click(screen.getByRole('combobox', { name: 'Language' }))
  const texts = screen.getAllByRole('option').map(o => o.textContent ?? '')
  const auto = texts.find(t => /Auto|自动/.test(t))
  expect(auto, `no Auto row among ${JSON.stringify(texts)}`).toBeTruthy()
  return auto as string
}

describe('DisplayPanel — language picker Auto row', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('names the BROWSER language, not the selected one', () => {
    // Explicit English on a Chinese browser: the annotation answers "what does
    // Auto give me?", so it must say 简体中文. Reading the ACTIVE language here
    // instead made it echo the selection ("— English") on every browser, which
    // is both uninformative and wrong.
    localStorage.setItem(LANG_STORAGE_KEY, 'en')
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['zh-CN', 'en'])

    renderWithProviders(<DisplayPanel />)

    expect(autoOptionText()).toContain('简体中文')
  })

  it('falls back to the default language when the browser matches nothing', () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'zh-CN')
    // `ja-JP` is deliberately a language we do NOT ship — a shippable tag makes
    // this assert the opposite of its name once that language lands (it was
    // `fr-FR`, which broke when French shipped).
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['ja-JP'])

    renderWithProviders(<DisplayPanel />)

    const text = autoOptionText()
    expect(text).toContain('English')
    expect(text).not.toContain('简体中文')
  })
})
