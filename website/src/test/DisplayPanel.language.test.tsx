// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks — the
// same lightweight mock the SettingsSelect unit tests use, so options are real
// role="option" nodes we can read without opening a portal.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import React from 'react'
import i18next from 'i18next'

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
    // instead would echo the selection ("— English") on every browser, which is
    // both uninformative and wrong.
    localStorage.setItem(LANG_STORAGE_KEY, 'en')
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['zh-CN', 'en'])

    renderWithProviders(<DisplayPanel />)

    expect(autoOptionText()).toContain('简体中文')
  })

  it('falls back to the default language when the browser matches nothing', () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'zh-CN')
    // Klingon is deliberately not a product locale. A plausible future language
    // would make this assert the opposite of its name when its catalog lands.
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['tlh-US'])

    renderWithProviders(<DisplayPanel />)

    const text = autoOptionText()
    expect(text).toContain('English')
    expect(text).not.toContain('简体中文')
  })

  // The endonym, not the English name: a user looking for Korean scans for
  // 한국어. `languages.ts` is the only place these strings live, so a language
  // registered there and missing from the picker fails here.
  it.each([['Japanese', '日本語'], ['Korean', '한국어']])(
    'offers %s as a display language',
    (_language, endonym) => {
      renderWithProviders(<DisplayPanel />)

      fireEvent.click(screen.getByRole('combobox', { name: 'Language' }))
      expect(screen.getByRole('option', { name: endonym })).toBeInTheDocument()
    },
  )
})

describe('DisplayPanel — zoom level description', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(async () => {
    vi.restoreAllMocks()
    await i18next.changeLanguage('en')
  })

  // The description used to be a template literal interpolating `modKey`, so it
  // stayed English in every locale while the label beside it translated — the
  // one untranslated sentence in an otherwise localized card. A template
  // literal is invisible to the extraction codemod, so only a rendered
  // assertion catches this class.
  it('is translated, and still names the platform modifier key', async () => {
    await i18next.changeLanguage('zh-CN')

    renderWithProviders(<DisplayPanel />)

    const description = screen.getByText(/原生窗口缩放/)
    expect(screen.queryByText(/Native window zoom/)).toBeNull()
    // `{{mod}}` must survive interpolation — a missing value renders the raw
    // placeholder, which reads as broken copy rather than as a keyboard hint.
    expect(description.textContent).not.toContain('{{mod}}')
  })
})
