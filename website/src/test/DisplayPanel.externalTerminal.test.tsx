import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * Settings → Display → Terminal: the "External terminal app" picker.
 *
 * Desktop app only, against a gateway on this machine, and it lists only the
 * allowlisted apps the desktop shell finds installed. Unset by default (None);
 * a pick is saved to dashboard.terminal.external_app, which the gateway's
 * launch gate reads.
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

const brandingEnv = vi.hoisted(() => ({ directLocal: true }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal }),
}))

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

const KEY = 'dashboard.terminal.external_app'
const LABEL = 'External terminal app'
const ROUTE = '/settings?tab=display&sub=terminal'

/** Bound for every wait in this suite, so none relies on the 1,000 ms
 *  default. The picker renders only once the desktop shell's detection
 *  (`externalTerminalAPI.detect`) answers, and shows the pick only once the
 *  config query (`api.kirocrewConfig`) beside it answers too; a pick then
 *  waits on the config write, and a Retry on a second detection. The default
 *  is a bet on runner speed; this ceiling turns a lost run into a named
 *  failure. */
const EXTERNAL_ROW_WAIT = { timeout: 5000 }

let detect: ReturnType<typeof vi.fn>

function installBridge(apps: Array<{ id: string; label: string }>) {
  detect = vi.fn().mockResolvedValue({ apps, capable: true })
  ;(window as { externalTerminalAPI?: unknown }).externalTerminalAPI = { detect, open: vi.fn() }
}

function seed(app: unknown) {
  kirocrewConfigMock.mockImplementation(() =>
    Promise.resolve({ dashboard: { terminal: app === undefined ? {} : { external_app: app } } }),
  )
}

const picker = () => screen.findByRole('combobox', { name: LABEL }, EXTERNAL_ROW_WAIT)

describe('DisplayPanel → External terminal app', () => {
  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    brandingEnv.directLocal = true
    seed(undefined)
    delete (window as { externalTerminalAPI?: unknown }).externalTerminalAPI
  })

  afterEach(() => {
    delete (window as { externalTerminalAPI?: unknown }).externalTerminalAPI
  })

  it('reads as None when nothing is picked (the default)', async () => {
    installBridge([{ id: 'kitty', label: 'kitty' }])
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await waitFor(async () => expect(await picker()).toHaveTextContent('None'), EXTERNAL_ROW_WAIT)
  })

  it('lists only the apps the desktop shell detected, after None', async () => {
    installBridge([{ id: 'kitty', label: 'kitty' }, { id: 'wezterm', label: 'WezTerm' }])
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const trigger = await picker()
    await waitFor(() => expect(trigger).not.toBeDisabled(), EXTERNAL_ROW_WAIT)
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getByRole('option', { name: 'WezTerm' })).toBeInTheDocument(), EXTERNAL_ROW_WAIT)
    expect(screen.getAllByRole('option').map(o => o.textContent)).toEqual(['None', 'kitty', 'WezTerm'])
  })

  it('saves the picked app id', async () => {
    installBridge([{ id: 'kitty', label: 'kitty' }])
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const trigger = await picker()
    await waitFor(() => expect(trigger).not.toBeDisabled(), EXTERNAL_ROW_WAIT)
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'kitty' }, EXTERNAL_ROW_WAIT))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(KEY, 'kitty'), EXTERNAL_ROW_WAIT)
  })

  it('says so when saving the pick fails, and keeps the picker usable', async () => {
    installBridge([{ id: 'kitty', label: 'kitty' }])
    patchConfigMock.mockImplementation(() => Promise.reject(new Error('write refused')))
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const trigger = await picker()
    await waitFor(() => expect(trigger).not.toBeDisabled(), EXTERNAL_ROW_WAIT)
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'kitty' }, EXTERNAL_ROW_WAIT))
    expect(
      await screen.findByText('Could not save the external terminal app', undefined, EXTERNAL_ROW_WAIT),
    ).toBeInTheDocument()
    // The failed write leaves the picker enabled, so the user can pick again.
    const again = await picker()
    await waitFor(() => expect(again).not.toBeDisabled(), EXTERNAL_ROW_WAIT)
    fireEvent.click(again)
    fireEvent.click(await screen.findByRole('option', { name: 'kitty' }, EXTERNAL_ROW_WAIT))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledTimes(2), EXTERNAL_ROW_WAIT)
  })

  it('shows a stored pick that is no longer installed as None', async () => {
    installBridge([{ id: 'kitty', label: 'kitty' }])
    seed('ghostty')
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await waitFor(async () => expect(await picker()).toHaveTextContent('None'), EXTERNAL_ROW_WAIT)
  })

  it('says so when no supported app is installed', async () => {
    installBridge([])
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    expect(await screen.findByText('No supported terminal app was found on this computer.', undefined, EXTERNAL_ROW_WAIT)).toBeInTheDocument()
    await waitFor(async () => expect(await picker()).toBeDisabled(), EXTERNAL_ROW_WAIT)
  })

  it('reports a failed detection instead of claiming nothing is installed', async () => {
    installBridge([])
    detect.mockRejectedValue(new Error('probe failed'))
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    expect(await screen.findByText("Couldn't check which terminal apps are installed on this computer.", undefined, EXTERNAL_ROW_WAIT)).toBeInTheDocument()
    expect(screen.queryByText('No supported terminal app was found on this computer.')).toBeNull()
  })

  it('retries a failed detection from its notice and then lists the apps', async () => {
    installBridge([{ id: 'kitty', label: 'kitty' }])
    detect.mockRejectedValueOnce(new Error('probe failed'))
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const notice = await screen.findByTestId('settings-external-terminal-detect-error', undefined, EXTERNAL_ROW_WAIT)
    fireEvent.click(within(notice.parentElement as HTMLElement).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByTestId('settings-external-terminal-detect-error')).toBeNull(), EXTERNAL_ROW_WAIT)
    expect(detect).toHaveBeenCalledTimes(2)
    const trigger = await picker()
    await waitFor(() => expect(trigger).not.toBeDisabled(), EXTERNAL_ROW_WAIT)
    fireEvent.click(trigger)
    expect(await screen.findByRole('option', { name: 'kitty' }, EXTERNAL_ROW_WAIT)).toBeInTheDocument()
  })

  // A desktop window reaching a remote crew through an SSH tunnel reads as
  // direct-local, so the panel asks; the shell answers "not capable".
  it('is absent, with no error, for a window the desktop shell answers as not capable', async () => {
    installBridge([])
    detect.mockResolvedValue({ apps: [], capable: false })
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await screen.findByRole('switch', { name: 'Reuse the current terminal' }, EXTERNAL_ROW_WAIT)
    await waitFor(() => expect(detect).toHaveBeenCalled(), EXTERNAL_ROW_WAIT)
    expect(screen.queryByRole('combobox', { name: LABEL })).toBeNull()
    expect(screen.queryByText("Couldn't check which terminal apps are installed on this computer.")).toBeNull()
    expect(screen.queryByText('No supported terminal app was found on this computer.')).toBeNull()
  })

  it('is absent in the browser build, where there is no launch bridge', async () => {
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await screen.findByRole('switch', { name: 'Reuse the current terminal' }, EXTERNAL_ROW_WAIT)
    expect(screen.queryByRole('combobox', { name: LABEL })).toBeNull()
  })

  it('is absent for a gateway that is not on this machine', async () => {
    installBridge([{ id: 'kitty', label: 'kitty' }])
    brandingEnv.directLocal = false
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await screen.findByRole('switch', { name: 'Reuse the current terminal' }, EXTERNAL_ROW_WAIT)
    expect(screen.queryByRole('combobox', { name: LABEL })).toBeNull()
    expect(detect).not.toHaveBeenCalled()
  })
})
