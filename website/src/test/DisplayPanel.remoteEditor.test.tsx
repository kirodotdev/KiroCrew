// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks — the
// same lightweight mock the other DisplayPanel select tests use, so options are
// real role="option" nodes we can click without opening a portal.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import React from 'react'
import { renderWithProviders } from './helpers'

/**
 * Settings → Display → Remote editor: its own rail pane (issue #11338).
 *
 * Pins the pane's two controls, the server write for the editor pick, and the
 * "set a host" hint: an editor with no host builds no link, so without the hint
 * the menu row silently never appears.
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
// Rendered with editor 'vscode' (REMOTE_EDITORS.vscode.label === 'VS Code').
const HINT = 'Set an SSH host to show “Open in VS Code” in file menus.'

function seed(remote: Record<string, unknown> | undefined) {
  kirocrewConfigMock.mockImplementation(() =>
    Promise.resolve({ dashboard: remote === undefined ? {} : { remote_editor: remote } }),
  )
}

describe('DisplayPanel → Remote editor pane', () => {
  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    seed(undefined)
  })

  it('renders the editor picker and the SSH host field on its own pane', async () => {
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    expect(await screen.findByRole('combobox', { name: 'Editor' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'SSH host' })).toBeInTheDocument()
  })

  it('normalizes non-string server values before rendering the controls', async () => {
    seed({ editor: { name: 'vscode' }, host: 42 })
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    expect(await screen.findByRole('combobox', { name: 'Editor' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'SSH host' })).toHaveValue('')
  })

  it('shows Off for an unknown editor id', async () => {
    seed({ editor: 'cursor', host: '' })
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const select = await screen.findByRole('combobox', { name: 'Editor' })
    await waitFor(() => expect(select).not.toBeDisabled())
    fireEvent.click(select)
    expect(screen.getByRole('option', { name: 'Off' })).toHaveAttribute('aria-selected', 'true')
  })

  it('PATCHes the nested editor key when an editor is picked', async () => {
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const select = await screen.findByRole('combobox', { name: 'Editor' })
    await waitFor(() => expect(select).not.toBeDisabled())
    fireEvent.click(select)
    fireEvent.click(screen.getByRole('option', { name: 'VS Code' }))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('dashboard.remote_editor.editor', 'vscode'),
    )
  })

  it('serializes rapid editor picks so the last pick is the last write', async () => {
    // Hold each PATCH open so a second pick lands while the first is in flight.
    const pending: Array<() => void> = []
    patchConfigMock.mockImplementation(
      () => new Promise(resolve => { pending.push(() => resolve({})) }),
    )
    const { queryClient } = renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const select = await screen.findByRole('combobox', { name: 'Editor' })
    await waitFor(() => expect(select).not.toBeDisabled())
    fireEvent.click(select)
    fireEvent.click(screen.getByRole('option', { name: 'VS Code' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledTimes(1))
    fireEvent.click(select)
    fireEvent.click(screen.getByRole('option', { name: 'Kiro' }))
    // Scoped mutations queue: the second PATCH must not start until the first
    // settles, or a delayed earlier write could land last and revert the pick.
    // Wait for the positive signal that it is queued -- the second editor
    // mutation sits paused in the cache -- before asserting it has not run.
    // Without the scope it starts at once and is never paused.
    await waitFor(() => {
      const editorMuts = queryClient.getMutationCache().getAll()
        .filter(m => m.state.variables === 'kiro')
      expect(editorMuts).toHaveLength(1)
      expect(editorMuts[0].state.isPaused).toBe(true)
    })
    expect(patchConfigMock).toHaveBeenCalledTimes(1)
    pending[0]()
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledTimes(2))
    expect(patchConfigMock.mock.calls.map(c => c[1])).toEqual(['vscode', 'kiro'])
    pending[1]()
  })

  it('says a host is needed when an editor is picked but no host is set', async () => {
    seed({ editor: 'vscode', host: '' })
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    expect(await screen.findByText(HINT)).toBeInTheDocument()
  })

  it('shows no hint when the feature is off or fully configured', async () => {
    seed({ editor: '', host: '' })
    const { unmount } = renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await screen.findByRole('combobox', { name: 'Editor' })
    expect(screen.queryByText(HINT)).not.toBeInTheDocument()
    unmount()

    seed({ editor: 'kiro', host: 'dev-host.example.com' })
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'SSH host' })).toHaveValue('dev-host.example.com'),
    )
    expect(screen.queryByText(HINT)).not.toBeInTheDocument()
  })

  it.each([
    [400, /^Couldn’t save the SSH host\. Use a hostname/],
    [500, /^Couldn’t save the SSH host\.$/],
  ])('names the format rule only when the gate refuses the host (HTTP %i)', async (status, text) => {
    const { ApiError } = await import('../api/client')
    patchConfigMock.mockImplementation(() => Promise.reject(new ApiError(status, 'refused')))
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const field = await screen.findByRole('textbox', { name: 'SSH host' })
    fireEvent.change(field, { target: { value: 'bad:host' } })
    fireEvent.blur(field)
    expect(await screen.findByText(text)).toBeInTheDocument()
  })
})
