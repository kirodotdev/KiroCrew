/**
 * Regression test: App syncs the terminal-enabled registry flag from the
 * /api/terminal/sessions config.
 *
 * The "Run in Terminal" button on shell code blocks is gated in
 * EditableCodeBlock behind `useTerminalEnabled()`, which reads a module-level
 * flag in terminalRegistry (`_enabled`, initialised to false). App is the ONE
 * place that flips that flag on, via:
 *
 *   const terminalEnabled = terminalConfig?.enabled !== false
 *   useEffect(() => { setTerminalEnabledFlag(terminalEnabled) }, [terminalEnabled])
 *
 * This wiring is easy to drop silently in an App.tsx rewrite: existing
 * EditableCodeBlock / terminalRegistry tests exercise the gate GIVEN the flag --
 * none assert that App actually sets it, so such a regression slips through
 * green tests. These tests pin the App-to-registry contract directly so a
 * future App.tsx refactor can't silently drop it:
 *   1. server reports enabled -> isTerminalEnabled() becomes true
 *   2. server reports enabled:false (explicit opt-out) -> App drives the flag
 *      to false, even if a prior enabled session had left it true
 *   3. the probe FAILS (non-OK / error) -> App keeps the flag ON (default-on):
 *      a transient failure must not hide an enabled terminal
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { renderWithProviders } from './helpers'
import App from '../App'
import { isTerminalEnabled, setTerminalEnabledFlag } from '../utils/terminalRegistry'

// Match the App.test.tsx mock setup: isolate routing + stub the api client so
// App mounts without hitting real network for everything except the MSW-served
// /api/terminal/sessions endpoint this test cares about.
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
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 0, credits_covered: 3044, credits_plan: 10000, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04' } }),
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
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

describe('App terminal-enabled registry flag sync', () => {
  beforeEach(() => {
    // Reset the module-level flag to its initial (off) state so each test
    // starts from the same baseline regardless of ordering.
    setTerminalEnabledFlag(false)
  })
  afterEach(() => {
    setTerminalEnabledFlag(false)
  })

  it('turns the flag ON when /api/terminal/sessions reports enabled', async () => {
    server.use(
      http.get('/api/terminal/sessions', () => HttpResponse.json({ enabled: true, sessions: [] })),
    )
    expect(isTerminalEnabled()).toBe(false)
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(isTerminalEnabled()).toBe(true))
  })

  it('keeps the flag ON when the probe fails (default-on)', async () => {
    // A transient / auth-timing failure of the /api/terminal/sessions probe
    // must NOT hide an enabled terminal. The queryFn falls back to
    // {enabled:true} on a non-OK response, so App drives the flag ON.
    server.use(
      http.get('/api/terminal/sessions', () => new HttpResponse(null, { status: 500 })),
    )
    expect(isTerminalEnabled()).toBe(false)
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(isTerminalEnabled()).toBe(true))
  })

  it('drives the flag OFF only on an explicit enabled:false (opt-out honored)', async () => {
    // The backend opt-out (dashboard.terminal.enabled=false) is still honored:
    // an explicit false hides the terminal. Simulate a stale "on" flag left
    // over from an earlier enabled session — the effect must actively clear it.
    setTerminalEnabledFlag(true)
    server.use(
      http.get('/api/terminal/sessions', () => HttpResponse.json({ enabled: false, sessions: [] })),
    )
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(isTerminalEnabled()).toBe(false))
    // A second settle window guards against a late async re-flip.
    await new Promise(r => setTimeout(r, 20))
    expect(isTerminalEnabled()).toBe(false)
  })
})
