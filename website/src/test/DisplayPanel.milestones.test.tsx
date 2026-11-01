import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * Settings → Display → View: the Explore toggle (RFC "Milestones for New-User
 * Feature Discovery" 4.9). Shown only when the gateway's config carries the
 * key, since an older gateway refuses the write; default ON.
 */

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn((_path: string, _value: unknown) => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({})),
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
  useUIMode: () => ({
    uiMode: 'chat',
    setUIMode: vi.fn(),
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

import { DisplayPanel } from '../pages/settings/DisplayPanel'

const ROUTE = '/settings?tab=display&sub=view'
const main = () => screen.queryByRole('switch', { name: 'Explore' })

describe('DisplayPanel → View Explore toggle', () => {
  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
  })

  it('hides the toggle on a gateway whose config has no milestones key', async () => {
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ dashboard: { show_unread_dot: true } }))
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    await screen.findByRole('switch', { name: 'Translucent panels' })
    expect(main()).toBeNull()
  })

  it('shows it, reads it, and PATCHes the switch', async () => {
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ dashboard: { milestones_enabled: true } }))
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await waitFor(() => expect(main()).toHaveAttribute('aria-checked', 'true'))
    await waitFor(() => expect(main()).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(main()!)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('dashboard.milestones_enabled', false))
  })
})
