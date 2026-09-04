/**
 * Tests for the electron-shell top-bar surfaces:
 * - readout capsule collapse/expand via the connection dot (persisted)
 * - macOS fullscreen: 'mac-fullscreen' class driven by the electron
 *   fullscreen-changed bridge (drops the 84px traffic-light inset via CSS)
 *
 * (The activity-panel open toggle lives in the ChatPage session header, not
 * the top bar — see ChatPage.responsivePanel.test.tsx for its coverage.)
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { safeSetItem } from '../utils/safeStorage'
import { setDesktopUpdateAvailable } from '../store/dashboardSlice'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/AgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
// isMacElectron is a module-level const (frozen at import) — force it true so
// the mac-fullscreen wiring is exercisable in jsdom.
vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  isMacElectron: true,
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { available: false } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

import App from '../App'

const setWindowWidth = (w: number) => {
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: w })
}

describe('App top bar — readout capsule collapse', () => {
  beforeEach(() => {
    localStorage.clear()
    setWindowWidth(1400)
  })

  it('clicking the connection dot collapses the readouts to just the dot and persists', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const dot = await screen.findByLabelText('Gateway connected')
    expect(dot.getAttribute('aria-expanded')).toBe('true')
    // metrics segment visible while expanded (the fork capsule has no
    // enterprise-SSO segment — that SSO flow is stubbed in this fork)
    expect(screen.getByLabelText('System metrics')).toBeTruthy()

    fireEvent.click(dot)
    expect(dot.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByLabelText('System metrics')).toBeNull()
    expect(localStorage.getItem('mc-topbar-capsule-collapsed')).toBe('1')

    // Click again: readouts return.
    fireEvent.click(dot)
    expect(dot.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByLabelText('System metrics')).toBeTruthy()
    expect(localStorage.getItem('mc-topbar-capsule-collapsed')).toBe('0')
  })

  it('starts collapsed when the persisted flag is set', async () => {
    safeSetItem('mc-topbar-capsule-collapsed', '1')
    renderWithProviders(<App />, { route: '/chat' })
    const dot = await screen.findByLabelText('Gateway connected')
    expect(dot.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByLabelText('System metrics')).toBeNull()
  })
})

describe('App top bar — update pill shifts the collapse-ladder budget', () => {
  beforeEach(() => {
    localStorage.clear()
    setWindowWidth(1400)
  })

  it('puts tb-has-update on the actions group exactly while an update exists', async () => {
    // The class is what arms the shifted @container rungs in index.css: the
    // update pill is a non-shrinking sibling of the ladder, so while it is
    // mounted the group's fixed content is wider by the pill's footprint and
    // every rung must fire earlier. Class and pill must move together — a class
    // without the pill evicts readouts for nothing, a pill without the class is
    // the two-line clipped-wrap defect this guards against — and the two
    // conditions are computed independently (App.tsx vs UpdatePill.tsx), so the
    // pill's own mount is asserted alongside the class at every step.
    const { container, store } = renderWithProviders(<App />, { route: '/chat' })
    await screen.findByLabelText('Gateway connected')
    const group = container.querySelector('.tb-right') as HTMLElement
    expect(group).toBeTruthy()
    expect(group.classList.contains('tb-has-update')).toBe(false)
    expect(screen.queryByTestId('update-pill')).toBeNull()

    act(() => { store.dispatch(setDesktopUpdateAvailable(true)) })
    expect(group.classList.contains('tb-has-update')).toBe(true)
    expect(await screen.findByTestId('update-pill')).toBeTruthy()

    act(() => { store.dispatch(setDesktopUpdateAvailable(false)) })
    expect(group.classList.contains('tb-has-update')).toBe(false)
    expect(screen.queryByTestId('update-pill')).toBeNull()
  })

  it('keeps the nowrap backstop on the capsule segments', async () => {
    // The other half of the fix: if the group is ever narrower than its
    // contents (a locale wider than the measured budget, the dev-only
    // pseudolocale), a squeezed built-in segment must clip at the group's
    // edge, never wrap into two lines that the capsule's fixed h-7 crops.
    // The class lives on the shared `seg` string, so one rendered segment
    // pins them all.
    renderWithProviders(<App />, { route: '/chat' })
    const metrics = await screen.findByLabelText('System metrics')
    expect(metrics.className).toContain('whitespace-nowrap')
  })
})

describe('App shell — macOS fullscreen class', () => {
  it('toggles mac-fullscreen on the root grid from the electron bridge', async () => {
    let fsCallback: ((fs: boolean) => void) | undefined
    ;(window as { electronAPI?: { onFullScreenChanged?: (cb: (fs: boolean) => void) => () => void } }).electronAPI = {
      onFullScreenChanged: (cb: (fs: boolean) => void) => { fsCallback = cb; return () => { fsCallback = undefined } },
    }
    const { container } = renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')

    const root = container.querySelector('.mac-electron') as HTMLElement
    expect(root).toBeTruthy()
    expect(root.classList.contains('mac-fullscreen')).toBe(false)

    act(() => fsCallback?.(true))
    expect(root.classList.contains('mac-fullscreen')).toBe(true)

    act(() => fsCallback?.(false))
    expect(root.classList.contains('mac-fullscreen')).toBe(false)
    delete (window as { electronAPI?: { onFullScreenChanged?: (cb: (fs: boolean) => void) => () => void } }).electronAPI
  })
})
