/**
 * Settings → Display → Terminal: the owner-only Terminal panel switch
 * (`dashboard.terminal.enabled`). Off asks first when the gateway lists live
 * shells, writes the accepted value into the `terminal-enabled` query cache
 * and issues NO refetch: an immediate refetch would read the gateway's pre-flip
 * 30 s cache as a fresh answer and undo the write for a minute.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { isTerminalEnabled, setTerminalEnabledFlag } from '../utils/terminalRegistry'
import { useTerminalRestoreProbe } from '../shell/boot/terminalRestore'

/**
 * Settings → Display → Sidebar: the "Show unread dot on session cards" toggle.
 *
 * Server-persisted (dashboard.show_unread_dot) and read by ChatSidebar. Default
 * ON, so today's dot stays; only a literal `false` reads as off, the same rule
 * the sidebar applies.
 */

const { patchConfigMock, kirocrewConfigMock, terminalSessionsMock, peopleMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn((_path: string, _value: unknown) => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({})),
  terminalSessionsMock: vi.fn((_signal?: AbortSignal) => Promise.resolve(new Response(JSON.stringify({ enabled: true, sessions: [] }), { status: 200 }))),
  peopleMock: vi.fn(() => Promise.resolve({ people: [{ principal: { id: 'me' }, sources: [], is_viewer: true, is_owner: true }] })),
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
      terminalSessions: terminalSessionsMock,
      sharedCrewPeople: peopleMock,
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

// The restored-tab reconcile is not under test: only the mirror of the query cache is.
vi.mock('../hooks/useBottomTerminal', () => ({ confirmRestoredTabs: vi.fn(), reconcileRestoredTabs: vi.fn(() => []) }))
vi.mock('../hooks/usePanelTabs', () => ({ confirmRestoredPanelTerminals: vi.fn(), reconcileRestoredPanelTerminals: vi.fn(() => []) }))
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

const KEY = 'dashboard.terminal.enabled'
const ROUTE = '/settings?tab=display&sub=terminal'
const ROW = '[data-setting-key="dashboard.terminal.enabled"]'

const list = (status: number, body?: unknown) =>
  terminalSessionsMock.mockImplementation(() => Promise.resolve(new Response(body === undefined ? null : JSON.stringify(body), { status })))
const people = (rows: object[] | number) =>
  peopleMock.mockImplementation(() => typeof rows === 'number'
    ? Promise.reject(Object.assign(new Error('refused'), { status: rows }))
    : Promise.resolve({ people: rows }))
const OWNER = [{ principal: { id: 'me' }, sources: [], is_viewer: true, is_owner: true }]

/** The shell's mirror beside the panel, so the cache write reaches the registry flag. */
function Mirror() { useTerminalRestoreProbe(); return null }

const toggle = () => screen.findByRole('switch', { name: 'Terminal panel' })

describe('DisplayPanel → Terminal panel switch', () => {
  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ dashboard: { terminal: {} } }))
    terminalSessionsMock.mockClear()
    peopleMock.mockClear()
    list(200, { enabled: true, sessions: [] })
    people(OWNER)
    setTerminalEnabledFlag(true)
  })
  afterEach(() => { act(() => { setTerminalEnabledFlag(false) }) })

  it('T7: reads as on for the owner, asks before ending one live shell, writes the cache and never refetches', async () => {
    list(200, { enabled: true, sessions: [{ session_id: 'a' }] })
    const { queryClient } = renderWithProviders(<><Mirror /><DisplayPanel /></>, { route: ROUTE })
    queryClient.setQueryData(['terminal-enabled'], { enabled: true, sessions: [] })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const refetch = vi.spyOn(queryClient, 'refetchQueries')
    const sw = await toggle()
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))

    fireEvent.click(sw)
    expect(await screen.findByRole('dialog')).toHaveTextContent('1 terminal shell is running')
    expect(patchConfigMock).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Turn off and end shells' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledTimes(1))
    expect(patchConfigMock).toHaveBeenCalledWith(KEY, false)
    await waitFor(() => expect(queryClient.getQueryData(['terminal-enabled'])).toEqual({ enabled: false, sessions: [] }))
    await waitFor(() => expect(isTerminalEnabled()).toBe(false))
    const terminalEnabledCalls = (spy: ReturnType<typeof vi.spyOn>) =>
      spy.mock.calls.filter(c => JSON.stringify((c[0] as { queryKey?: unknown })?.queryKey) === JSON.stringify(['terminal-enabled']))
    expect(terminalEnabledCalls(invalidate)).toHaveLength(0)
    expect(terminalEnabledCalls(refetch)).toHaveLength(0)
    // The list route still answers the gateway's pre-flip cache; nothing reads it.
    vi.useFakeTimers()
    try { await act(async () => { await vi.advanceTimersByTimeAsync(5_000) }) } finally { vi.useRealTimers() }
    expect(isTerminalEnabled()).toBe(false)
    expect(queryClient.getQueryData(['terminal-enabled'])).toEqual({ enabled: false, sessions: [] })
  })

  it('T7: cancel leaves the setting alone', async () => {
    list(200, { enabled: true, sessions: [{ session_id: 'a' }] })
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('T7: no live shell means no dialog', async () => {
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(KEY, false))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('T7: a refused list asks in the unknown form; several shells use the plural', async () => {
    list(403)
    const { unmount } = renderWithProviders(<DisplayPanel />, { route: ROUTE })
    let sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    expect(await screen.findByRole('dialog')).toHaveTextContent('ends every running terminal shell')
    unmount()

    list(200, { enabled: true, sessions: [{ session_id: 'a' }, { session_id: 'b' }] })
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    expect(await screen.findByRole('dialog')).toHaveTextContent('2 terminal shells are running')
  })

  it('T7: turning on needs no confirm', async () => {
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ dashboard: { terminal: { enabled: false } } }))
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const sw = await toggle()
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(KEY, true))
    expect(terminalSessionsMock).not.toHaveBeenCalled()
  })

  it('T7: a non-owner gets no row and no list look; a refused people read reads as not owner', async () => {
    people([{ principal: { id: 'me' }, sources: [], is_viewer: true, is_owner: false }])
    const { container, unmount } = renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await screen.findByRole('switch', { name: 'Command completion' })
    expect(container.querySelector(ROW)).toBeNull()
    expect(terminalSessionsMock).not.toHaveBeenCalled()
    unmount()

    people(403)
    const second = renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await screen.findByRole('switch', { name: 'Command completion' })
    expect(second.container.querySelector(ROW)).toBeNull()
  })

  it('T7: the row appears after a failed people read recovers, without a remount', async () => {
    let reads = 0
    peopleMock.mockImplementation(() => {
      reads += 1
      return reads === 1 ? Promise.reject(new Error('500')) : Promise.resolve({ people: OWNER })
    })
    const { container } = renderWithProviders(<DisplayPanel />, { route: ROUTE, queryDefaults: { retry: 1, retryDelay: 0 } })
    await waitFor(() => expect(container.querySelector(ROW)).not.toBeNull())
    expect(reads).toBe(2)
  })

  it('T7: the embed Settings page carries no row', async () => {
    const { container } = renderWithProviders(<DisplayPanel embed />, { route: ROUTE })
    await screen.findByRole('switch', { name: 'Command completion' })
    expect(container.querySelector(ROW)).toBeNull()
  })

  it('T8: an overlay-pinned key reports why and leaves the cache alone', async () => {
    const { ApiError } = await import('../api/client')
    patchConfigMock.mockImplementation(() => Promise.reject(new ApiError(409, 'overlay', JSON.stringify({ code: 'config_overlay_owned' }))))
    const { queryClient } = renderWithProviders(<DisplayPanel />, { route: ROUTE })
    queryClient.setQueryData(['terminal-enabled'], { enabled: true, sessions: [] })
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await screen.findByText(/config\.local\.json, which overrides this switch/)
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    expect(queryClient.getQueryData(['terminal-enabled'])).toEqual({ enabled: true, sessions: [] })
  })
})
