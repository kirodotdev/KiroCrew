/**
 * App hands the boot-time `GET /api/terminal/sessions` answer to the dock
 * terminal store, so persisted tabs whose shells are gone do not come back after
 * a reload (#10977).
 *
 * Pins the wiring, not the store logic (useBottomTerminal.hydrateReconcile
 * covers that): the hosts draw no terminal while the restored set is pending, so
 * an App that never drove the two looks would leave the panel blank forever.
 * Four answers are pinned — omitted on both looks (dropped), omitted on the
 * first look but listed live on the uncached second (kept: the shell another
 * window was opening), a failing request (every tab kept), and a confirm look
 * that never answers (every tab kept once the probe deadline passes, so a hung
 * socket cannot hold the panel blank). Each case boots
 * App and the store fresh from seeded storage — and the render helpers with
 * them, so App and its providers share one context module instance.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { http, HttpResponse, delay } from 'msw'
import { server } from '../../integration/mocks/server'
// Warm-up only: transforming App's import graph is the slow part, and paying
// it here (at collection) keeps the per-test fresh boot below inside the test
// budget. `vi.resetModules` clears module instances, not the transform cache.
import '../App'

// Same isolation as App.terminalNavActive.test.tsx: stub the routed pages and
// the api client so App mounts without real network, and stub CliPanel (jsdom
// has no canvas for xterm). Only the store's tab list is asserted on.
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => <div data-testid="cli-panel" />,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
}))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 0, credits_covered: 0, credits_plan: 10000, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04' } }),
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
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
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
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

const STORAGE_KEY = 'mc-bottom-terminal'

let store: typeof import('../hooks/useBottomTerminal') | null = null
let registry: typeof import('../utils/terminalRegistry') | null = null

/** Persist two tabs, then boot the store and App fresh so module init restores them. */
async function bootApp() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    open: true, tabs: [{ id: 'live' }, { id: 'gone' }], activeId: 'live',
  }))
  vi.resetModules()
  store = await import('../hooks/useBottomTerminal')
  registry = await import('../utils/terminalRegistry')
  const { renderWithProviders } = await import('./helpers')
  const { default: App } = await import('../App')
  expect(store.isTerminalHydratePending()).toBe(true)
  registry.setTerminalEnabledFlag(true)
  renderWithProviders(<App />, { route: '/chat' })
  return store
}

afterEach(() => {
  store?.__resetBottomTerminal()
  registry?.setTerminalEnabledFlag(false)
  store = null
  registry = null
  localStorage.clear()
})

// The confirm look waits out the opening grace (2 s) before asking again, so
// the settle assertions allow for it.
const SETTLE_TIMEOUT_MS = 6_000

describe('App — restored dock terminal tabs are reconciled with the backend', () => {
  it('drops the restored tab both looks omit and keeps the live one', async () => {
    let probes = 0
    server.use(
      http.get('/api/terminal/sessions', () => {
        probes += 1
        return HttpResponse.json({ enabled: true, sessions: [{ session_id: 'live', alive: true }] })
      }),
    )

    const s = await bootApp()

    await waitFor(() => expect(s.isTerminalHydratePending()).toBe(false), { timeout: SETTLE_TIMEOUT_MS })
    expect(probes).toBeGreaterThanOrEqual(2)
    expect(s.hasTab('live')).toBe(true)
    expect(s.hasTab('gone')).toBe(false)
  })

  it('keeps a tab the first look omitted once the second look lists it live', async () => {
    let probes = 0
    server.use(
      http.get('/api/terminal/sessions', () => {
        probes += 1
        // First answer: `gone` is still a reservation placeholder the route
        // skips. Second answer: its shell is up.
        const sessions = probes === 1
          ? [{ session_id: 'live', alive: true }]
          : [{ session_id: 'live', alive: true }, { session_id: 'gone', alive: true }]
        return HttpResponse.json({ enabled: true, sessions })
      }),
    )

    const s = await bootApp()

    await waitFor(() => expect(s.isTerminalHydratePending()).toBe(false), { timeout: SETTLE_TIMEOUT_MS })
    expect(s.hasTab('live')).toBe(true)
    expect(s.hasTab('gone')).toBe(true)
  })

  it('settles by keeping every suspect when the confirm look hangs past its deadline', async () => {
    let probes = 0
    server.use(
      http.get('/api/terminal/sessions', async () => {
        probes += 1
        if (probes === 1) return HttpResponse.json({ enabled: true, sessions: [] })
        // The confirm look: a socket that never answers.
        await delay('infinite')
        return HttpResponse.error()
      }),
    )

    const s = await bootApp()

    // Opening grace (2 s) + probe deadline (10 s), then the catch settles with null.
    await waitFor(() => expect(s.isTerminalHydratePending()).toBe(false), { timeout: 14_000 })
    expect(probes).toBe(2)
    expect(s.hasTab('live')).toBe(true)
    expect(s.hasTab('gone')).toBe(true)
  }, 20_000)

  it('settles by keeping every restored tab when the probe request fails', async () => {
    server.use(http.get('/api/terminal/sessions', () => HttpResponse.error()))

    const s = await bootApp()

    await waitFor(() => expect(s.isTerminalHydratePending()).toBe(false))
    expect(s.hasTab('live')).toBe(true)
    expect(s.hasTab('gone')).toBe(true)
  })
})
