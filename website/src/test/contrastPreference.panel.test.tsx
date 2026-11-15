/**
 * The Theme page names the cause while the Contrast setting paints High Contrast
 * over the picked theme, and its button opens the row that controls it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router-dom'
import { renderWithProviders } from './helpers'

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
      kirocrewConfig: vi.fn(() => Promise.resolve({})),
      patchConfig: vi.fn(() => Promise.resolve({})),
      installTheme: vi.fn(() => Promise.resolve({ ok: true })),
    },
    ApiError,
  }
})

vi.mock('../hooks/ZoomProvider', () => ({
  useZoomCtx: () => ({
    zoom: 100, zoomSupported: true, zoomIn: vi.fn(), zoomOut: vi.fn(), reset: vi.fn(),
    family: 'sans', setFontFamily: vi.fn(), cycleFamily: vi.fn(),
  }),
}))

const themePick = vi.hoisted(() => ({ value: 'default' }))

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({
    preference: 'dark',
    setTheme: vi.fn(),
    colorTheme: themePick.value,
    setColorTheme: vi.fn(),
    allThemes: [
      { value: 'default', label: 'Default', custom: false },
      { value: 'highcontrast', label: 'High Contrast', custom: false },
    ],
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
import { CONTRAST_STORAGE_KEY } from '../utils/contrastPreference'
import { useSettingHighlight } from '../hooks/useSettingHighlight'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { settingsRoute } from '../components/commandPalette/settingsRoute'

const THEME_ROUTE = '/settings?tab=display&sub=theme'
const NOTICE = 'High Contrast is on'
const ALWAYS_HIGH_BODY = 'Contrast: Always high.'
const FOLLOW_SYSTEM_BODY = 'Contrast: Follow system.'
const MODE_NOTE = 'Mode still picks the light or dark version of High Contrast.'

function Location() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname + loc.search}</div>
}

/** The two halves of the settings page the notice's button depends on: the
 *  highlight hook and the Display panel mounted under the /settings path. */
function SettingsHost() {
  useSettingHighlight()
  return <DisplayPanel basePath="/settings" />
}

function renderSettingsHost() {
  return renderWithProviders(
    <>
      <Routes>
        <Route path="/settings/*" element={<SettingsHost />} />
      </Routes>
      <Location />
    </>,
    { route: '/settings/display/theme' },
  )
}

function stubOsContrast(matches: boolean): void {
  window.matchMedia = vi.fn().mockReturnValue({
    matches,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }) as unknown as typeof window.matchMedia
}

describe('Theme page while High Contrast is painted', () => {
  let originalMatchMedia: typeof window.matchMedia
  let originalScrollIntoView: typeof Element.prototype.scrollIntoView
  beforeEach(() => {
    themePick.value = 'default'
    localStorage.clear()
    originalMatchMedia = window.matchMedia
    originalScrollIntoView = Element.prototype.scrollIntoView
    stubOsContrast(false)
  })
  afterEach(() => {
    window.matchMedia = originalMatchMedia
    Element.prototype.scrollIntoView = originalScrollIntoView
    localStorage.clear()
  })

  it('shows no notice and no kept mark under Normal', async () => {
    renderWithProviders(<DisplayPanel />, { route: THEME_ROUTE })
    await screen.findByRole('button', { name: 'Install' })
    expect(screen.queryByText(NOTICE)).toBeNull()
    expect(screen.queryByText(/paused for High Contrast/)).toBeNull()
  })

  it('names Always high and gives only the way that option ends', async () => {
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'more')
    renderWithProviders(<DisplayPanel />, { route: THEME_ROUTE })
    expect(await screen.findByText(NOTICE)).toBeInTheDocument()
    const body = screen.getByText(new RegExp(`^${ALWAYS_HIGH_BODY}`))
    expect(body.textContent).toContain('until Contrast is set to Normal')
    expect(body.textContent).toContain(MODE_NOTE)
    expect(body.textContent).not.toContain('operating system')
    expect(screen.queryByText(new RegExp(FOLLOW_SYSTEM_BODY))).toBeNull()
  })

  it('names Follow system and its two ways out while the OS asks for more contrast', async () => {
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'auto')
    stubOsContrast(true)
    renderWithProviders(<DisplayPanel />, { route: THEME_ROUTE })
    expect(await screen.findByText(NOTICE)).toBeInTheDocument()
    const body = screen.getByText(new RegExp(`^${FOLLOW_SYSTEM_BODY}`))
    expect(body.textContent).toContain('your system stops asking')
    expect(body.textContent).toContain('Contrast is set to Normal')
    expect(body.textContent).toContain(MODE_NOTE)
    expect(screen.queryByText(new RegExp(ALWAYS_HIGH_BODY))).toBeNull()
  })

  it('marks the theme picker value as paused while High Contrast is painted', async () => {
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'more')
    renderWithProviders(<DisplayPanel />, { route: THEME_ROUTE })
    expect(await screen.findByText('Default (paused for High Contrast)')).toBeInTheDocument()
  })

  it('leaves the picker label and the notice off when the stored pick is already High Contrast', async () => {
    themePick.value = 'highcontrast'
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'more')
    renderWithProviders(<DisplayPanel />, { route: THEME_ROUTE })
    await screen.findByRole('button', { name: 'Install' })
    expect(screen.getByText('High Contrast')).toBeInTheDocument()
    expect(screen.queryByText(/paused for High Contrast/)).toBeNull()
    expect(screen.queryByText(NOTICE)).toBeNull()
  })

  it('leaves them off under Follow system when the OS asks and the pick is High Contrast', async () => {
    themePick.value = 'highcontrast'
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'auto')
    stubOsContrast(true)
    renderWithProviders(<DisplayPanel />, { route: THEME_ROUTE })
    await screen.findByRole('button', { name: 'Install' })
    expect(screen.queryByText(/paused for High Contrast/)).toBeNull()
    expect(screen.queryByText(NOTICE)).toBeNull()
  })

  it('sizes the Open the Contrast setting button to its text instead of the notice width', async () => {
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'more')
    renderSettingsHost()
    expect(await screen.findByText(NOTICE)).toBeInTheDocument()
    const button = screen.getByRole('button', { name: 'Open the Contrast setting' })
    expect(button.classList.contains('self-start')).toBe(true)
  })

  it('opens the Contrast row through the settings link, which rings it and scrolls it into view', async () => {
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'more')
    const scrolled = vi.fn()
    Element.prototype.scrollIntoView = scrolled
    renderSettingsHost()
    expect(await screen.findByText(NOTICE)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open the Contrast setting' }))
    const entry = SETTINGS_REGISTRY.find(e => e.id === 'display.contrast')
    expect(entry).toBeDefined()
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe(settingsRoute(entry!)))
    expect(screen.getByTestId('loc').textContent).toContain('highlight=display.contrast')
    const chosen = await screen.findByRole('radio', { name: 'Always high' })
    const row = chosen.closest<HTMLElement>('[data-setting-label]')
    expect(row?.getAttribute('data-setting-label')).toBe('Contrast')
    await waitFor(() => expect(row?.style.outlineOffset).toBe('4px'))
    expect(scrolled.mock.contexts).toContain(row)
  })

  it('shows the notice without the button on the embedded settings page, which has no settings route', async () => {
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'more')
    renderWithProviders(<DisplayPanel />, { route: THEME_ROUTE })
    expect(await screen.findByText(NOTICE)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Open the Contrast setting' })).toBeNull()
  })
})
