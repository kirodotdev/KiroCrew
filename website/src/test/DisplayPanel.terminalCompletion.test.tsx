import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * Settings → Display → Terminal: the Command completion toggle.
 *
 * Its own file because it needs the api client mocked (the shared
 * DisplayPanel.test.tsx runs against the real client). The toggle is
 * server-persisted (dashboard.terminal.completion.enabled): the gate lives in
 * the gateway's completion route, which answers an empty listing while it is
 * off, so the popup never opens. Default on; only a literal `false` reads as
 * off — the same rule the backend applies.
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

// Same provider doubles as DisplayPanel.terminalShell.test.tsx — the panel
// reads all of these on render and none is under test here.
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

const KEY = 'dashboard.terminal.completion.enabled'

function seed(completion: Record<string, unknown> | undefined) {
  kirocrewConfigMock.mockImplementation(() =>
    Promise.resolve({ dashboard: { terminal: completion === undefined ? {} : { completion } } }),
  )
}

const toggle = () => screen.findByRole('switch', { name: 'Command completion' })

describe('DisplayPanel → Terminal command completion', () => {
  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    seed(undefined)
  })

  it('reads as ON when the key is absent (the backend default)', async () => {
    renderWithProviders(<DisplayPanel />)
    expect(await toggle()).toHaveAttribute('aria-checked', 'true')
  })

  it('reads as OFF only for a literal false', async () => {
    seed({ enabled: false })
    renderWithProviders(<DisplayPanel />)
    await waitFor(async () => expect(await toggle()).toHaveAttribute('aria-checked', 'false'))
  })

  it('does not read a hand-edited "false" string as off — the backend does not either', async () => {
    seed({ enabled: 'false' })
    renderWithProviders(<DisplayPanel />)
    expect(await toggle()).toHaveAttribute('aria-checked', 'true')
  })

  it('PATCHes the nested key with a boolean on click, flipping optimistically', async () => {
    // A PATCH that never settles: the flip below can only come from the
    // per-path overlay, not from a refetch of the (stateless) config mock.
    patchConfigMock.mockImplementation(() => new Promise(() => {}))
    renderWithProviders(<DisplayPanel />)
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(KEY, false))
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
  })

  it('surfaces a catalog message and rolls back when the save fails', async () => {
    patchConfigMock.mockImplementation(() => Promise.reject(new Error('boom')))
    renderWithProviders(<DisplayPanel />)
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() =>
      expect(screen.getByText(/Could not save the command completion setting/)).toBeInTheDocument(),
    )
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
  })

  it('keeps showing the saved value after a successful commit (no blink-back)', async () => {
    let stored: unknown = undefined
    patchConfigMock.mockImplementation(((_path: string, value: unknown) => {
      stored = value
      return Promise.resolve({})
    }) as never)
    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({
        dashboard: { terminal: stored === undefined ? {} : { completion: { enabled: stored } } },
      }),
    )
    renderWithProviders(<DisplayPanel />)
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(KEY, false))
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
    // A refetch round-trip later it still reads OFF.
    await new Promise(r => setTimeout(r, 20))
    expect(sw).toHaveAttribute('aria-checked', 'false')
  })
})
