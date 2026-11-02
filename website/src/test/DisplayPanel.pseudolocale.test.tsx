import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * Settings → Display button-group labels rendered under the `en-XA` pseudolocale.
 *
 * Interface (Chat), Mode (Auto / Light / Dark) and the sidebar colors' Display Mode
 * (Solid Tint / Gradient) used to be English literals, and Intensity (Soft … Intense)
 * capitalised the stored id, so they stayed English in every language. The pseudolocale brackets and accents every catalog value: a label
 * read from the catalog renders as its `en-XA` value, a literal renders as plain
 * English. Each case asserts both halves, and pins that a click still writes the
 * wire value, not the translated label.
 */

const { patchConfigMock, kirocrewConfigMock, setThemeMock, setUIModeMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn((_path: string, _value: unknown) => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({})),
  setThemeMock: vi.fn(),
  setUIModeMock: vi.fn(),
}))

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  }
  return {
    api: {
      kirocrewConfig: kirocrewConfigMock,
      patchConfig: patchConfigMock,
      installTheme: vi.fn(() => Promise.resolve({ ok: true })),
    },
    ApiError,
  }
})

const zoomCtx = {
  zoom: 100,
  zoomSupported: true,
  zoomIn: vi.fn(),
  zoomOut: vi.fn(),
  reset: vi.fn(),
  family: 'sans',
  setFontFamily: vi.fn(),
  cycleFamily: vi.fn(),
}
vi.mock('../hooks/ZoomProvider', () => ({
  useZoomCtx: () => zoomCtx,
}))

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({
    preference: 'dark',
    setTheme: setThemeMock,
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
  useUIMode: () => ({
    uiMode: 'cli',
    setUIMode: setUIModeMock,
    toggleUIMode: vi.fn(),
  }),
  UIModeProvider: ({ children }: { children: React.ReactNode }) => children,
}))

vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({
    paletteColors: ['#ff0000', '#00ff00', '#0000ff'],
    colorMode: 'tint' as const,
    paletteName: 'trailhead',
    intensity: 'clear',
    boost: {
      activePct: [60, 60, 60],
      idlePct: [30, 30, 30],
    },
  }),
}))

import { i18next } from '../i18n/all'
import { DisplayPanel } from '../pages/settings/DisplayPanel'

const XA = 'en-XA'
const xa = (key: string) => i18next.getFixedT(XA)(`pages.settings.displayPanel.${key}`)
const route = (sub: string) => `/settings?tab=display&sub=${sub}`

beforeEach(async () => {
  vi.clearAllMocks()
  await i18next.changeLanguage(XA)
})

afterEach(async () => {
  // Unmount first: a language switch re-renders every mounted `useTranslation`
  // consumer, and doing it after the test's last barrier is an act() warning.
  cleanup()
  await i18next.changeLanguage('en')
})

describe('DisplayPanel button-group labels under the pseudolocale', () => {
  it('translates the Interface option Chat and keeps writing the wire value', async () => {
    renderWithProviders(<DisplayPanel />, { route: route('view') })
    const label = xa('interface_chat')
    expect(label).toMatch(/^\[/)
    fireEvent.click(await screen.findByRole('button', { name: label }))
    expect(setUIModeMock).toHaveBeenCalledWith('chat')
    expect(screen.queryByRole('button', { name: 'Chat' })).not.toBeInTheDocument()
  })

  it('translates the Mode options Auto / Light / Dark and keeps writing the wire values', async () => {
    renderWithProviders(<DisplayPanel />, { route: route('theme') })
    for (const [key, english, wire] of [
      ['theme_mode_auto', 'Auto', 'system'],
      ['theme_mode_light', 'Light', 'light'],
      ['theme_mode_dark', 'Dark', 'dark'],
    ] as const) {
      const label = xa(key)
      expect(label).toMatch(/^\[/)
      fireEvent.click(await screen.findByRole('button', { name: label }))
      expect(setThemeMock).toHaveBeenLastCalledWith(wire)
      expect(screen.queryByRole('button', { name: english })).not.toBeInTheDocument()
    }
  })

  it('translates the session-color Display Mode options and keeps writing the wire values', async () => {
    const { store } = renderWithProviders(<DisplayPanel />, { route: route('sidebar') })
    const gradient = xa('color_mode_gradient')
    const tint = xa('color_mode_solid_tint')
    expect(gradient).toMatch(/^\[/)
    expect(tint).toMatch(/^\[/)
    fireEvent.click(await screen.findByRole('button', { name: gradient }))
    expect(store.getState().dashboard.sessionColorsMode).toBe('gradient')
    fireEvent.click(screen.getByRole('button', { name: tint }))
    expect(store.getState().dashboard.sessionColorsMode).toBe('tint')
    expect(screen.queryByRole('button', { name: 'Gradient' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Solid Tint' })).not.toBeInTheDocument()
  })

  it('translates the session-color Intensity options and keeps writing the wire values', async () => {
    const { store } = renderWithProviders(<DisplayPanel />, { route: route('sidebar') })
    for (const [wire, english] of [
      ['soft', 'Soft'], ['clear', 'Clear'], ['vivid', 'Vivid'], ['bold', 'Bold'], ['intense', 'Intense'],
    ] as const) {
      const label = xa(`intensity_${wire}`)
      expect(label).toMatch(/^\[/)
      fireEvent.click(await screen.findByRole('button', { name: label }))
      expect(store.getState().dashboard.sessionColorsIntensity).toBe(wire)
      expect(screen.queryByRole('button', { name: english })).not.toBeInTheDocument()
    }
  })
})
