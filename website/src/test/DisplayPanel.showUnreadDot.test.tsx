import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * Settings → Display → Sidebar: the "Show unread dot on session cards" toggle.
 *
 * Server-persisted (dashboard.show_unread_dot) and read by ChatSidebar. Default
 * ON, so today's dot stays; only a literal `false` reads as off, the same rule
 * the sidebar applies.
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

const KEY = 'dashboard.show_unread_dot'
const ROUTE = '/settings?tab=display&sub=sidebar'

function seed(value: unknown) {
  kirocrewConfigMock.mockImplementation(() =>
    Promise.resolve({ dashboard: value === undefined ? {} : { show_unread_dot: value } }),
  )
}

const toggle = () => screen.findByRole('switch', { name: 'Show unread dot on session cards' })

describe('DisplayPanel → Sidebar show-unread-dot', () => {
  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    seed(undefined)
  })

  it('reads as ON when the key is absent (the default keeps the dot)', async () => {
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await waitFor(async () => expect(await toggle()).toHaveAttribute('aria-checked', 'true'))
  })

  it('reads as OFF only for a literal false', async () => {
    seed(false)
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await waitFor(async () => expect(await toggle()).toHaveAttribute('aria-checked', 'false'))
  })

  it('PATCHes false on click, flipping optimistically', async () => {
    patchConfigMock.mockImplementation(() => new Promise(() => {}))
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(KEY, false))
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
  })

  it('PATCHes true on click when it is off', async () => {
    seed(false)
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const sw = await toggle()
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(KEY, true))
  })

  it('surfaces a catalog message and rolls back when the save fails', async () => {
    patchConfigMock.mockImplementation(() => Promise.reject(new Error('boom')))
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() => expect(screen.getByText(/Could not save the unread dot setting/)).toBeInTheDocument())
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
  })
})
