/**
 * Regression test: the nav rail's Terminal row stays lit while the docked
 * bottom terminal panel is open.
 *
 * The row is the only NavItem that TOGGLES a surface instead of navigating, so
 * it has no route to derive `active` from. It was shipped with a hardcoded
 * `active={false}`, which meant the only feedback was the hover style — move
 * the pointer away and nothing indicated the panel below was open. `active`
 * now tracks useBottomTerminalOpen(), and `aria-pressed` carries the same
 * state for screen readers.
 *
 * Pinned here because the failure mode is invisible to every other test: the
 * panel itself renders correctly either way, so only an assertion on the ROW
 * catches a regression back to a constant.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { renderWithProviders } from './helpers'
import App from '../App'
import { setTerminalEnabledFlag } from '../utils/terminalRegistry'
import { __resetBottomTerminal, openBottomTerminal } from '../hooks/useBottomTerminal'

// Same isolation as App.terminalEnabledFlag.test.tsx: stub the routed pages and
// the api client so App mounts without real network, and additionally stub
// CliPanel — the docked panel mounts a real xterm instance, which jsdom has no
// canvas/WebGL for. The test only cares about the NAV ROW, not the shell.
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/AgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => <div data-testid="cli-panel" />,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => vi.fn(),
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
// Typed via `unknown` rather than `any`: CI lints with a --max-warnings ceiling,
// so a new no-explicit-any warning here spends budget for nothing.
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

/** The nav rail's Terminal row. Named by its text label when the rail is
 *  expanded and by aria-label when collapsed, so role+name finds it either way. */
const terminalRow = () => screen.getByRole('button', { name: 'Terminal' })

describe('App nav rail — Terminal row reflects the docked panel state', () => {
  beforeEach(() => {
    setTerminalEnabledFlag(true)
    __resetBottomTerminal()
    server.use(
      http.get('/api/terminal/sessions', () => HttpResponse.json({ enabled: true, sessions: [] })),
    )
  })
  afterEach(() => {
    __resetBottomTerminal()
    setTerminalEnabledFlag(false)
  })

  it('is unlit and aria-pressed=false while the panel is closed', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(terminalRow()).toBeInTheDocument())
    expect(terminalRow()).toHaveAttribute('aria-pressed', 'false')
    expect(terminalRow().className).not.toContain('nav-active')
  })

  it('lights up and stays lit after a click opens the panel', async () => {
    const user = userEvent.setup()
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(terminalRow()).toBeInTheDocument())

    await user.click(terminalRow())

    // The lit state is driven by store subscription, not by :hover — so it must
    // survive the pointer leaving the row entirely.
    await user.unhover(terminalRow())
    await waitFor(() => expect(terminalRow()).toHaveAttribute('aria-pressed', 'true'))
    expect(terminalRow().className).toContain('nav-active')
  })

  it('goes dark again when the panel is toggled shut', async () => {
    const user = userEvent.setup()
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(terminalRow()).toBeInTheDocument())

    await user.click(terminalRow())
    await waitFor(() => expect(terminalRow()).toHaveAttribute('aria-pressed', 'true'))

    await user.click(terminalRow())
    await waitFor(() => expect(terminalRow()).toHaveAttribute('aria-pressed', 'false'))
    expect(terminalRow().className).not.toContain('nav-active')
  })

  it('lights up when the panel is opened from OUTSIDE the nav row', async () => {
    // "Run in terminal" on a code block and the keyboard shortcut both call
    // openBottomTerminal() directly. The row must follow the store, not just
    // its own click handler.
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(terminalRow()).toBeInTheDocument())
    expect(terminalRow()).toHaveAttribute('aria-pressed', 'false')

    act(() => { openBottomTerminal() })

    await waitFor(() => expect(terminalRow()).toHaveAttribute('aria-pressed', 'true'))
  })
})
