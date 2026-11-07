import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * Settings → Display → Remote editor: the editor picker and the SSH host the
 * side panel ⋯ menu's "Open in <editor>" link is built from. Both are saved on the
 * gateway (dashboard.remote_editor.*), which serves them to that menu through
 * branding, so a save refetches branding as well.
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

const ROUTE = '/settings?tab=display&sub=remote-editor'

describe('DisplayPanel → Remote editor', () => {
  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ dashboard: { remote_editor: { editor: '', host: '' } } }))
  })

  it('saves the picked editor and refetches branding for the menus', async () => {
    const { queryClient } = renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const trigger = await screen.findByRole('combobox', { name: 'Editor' })
    await waitFor(() => expect(trigger).not.toBeDisabled())
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'VS Code' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('dashboard.remote_editor.editor', 'vscode'))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['branding'] }))
  })

  it('saves the trimmed SSH host on blur and says it saved', async () => {
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const input = (await screen.findByLabelText('SSH host')) as HTMLInputElement
    await waitFor(() => expect(input).not.toBeDisabled())
    // The empty field shows what kind of value goes in it.
    expect(input.placeholder).toBe('e.g. dev-box')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    fireEvent.change(input, { target: { value: ' dev-box ' } })
    fireEvent.blur(input)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('dashboard.remote_editor.host', 'dev-box'))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    // Typing again is a new unsaved draft, so the line goes away.
    fireEvent.change(input, { target: { value: 'dev-box-2' } })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('keeps a refused host in the field and says what the gateway takes', async () => {
    const { ApiError } = await import('../api/client')
    patchConfigMock.mockImplementation(() => Promise.reject(new ApiError(400, 'invalid value')))
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const input = (await screen.findByLabelText('SSH host')) as HTMLInputElement
    await waitFor(() => expect(input).not.toBeDisabled())
    fireEvent.change(input, { target: { value: 'dev-box:22' } })
    fireEvent.blur(input)
    await waitFor(() => expect(screen.getByText(/with no port or path/)).toBeInTheDocument())
    expect(input.value).toBe('dev-box:22')
    expect(screen.queryByText('Saved')).not.toBeInTheDocument()
  })
})
