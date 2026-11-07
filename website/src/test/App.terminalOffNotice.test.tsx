/**
 * The reporting host of #31652: a restored terminal tab, a probe that cannot
 * rule or says the terminal is off. On an explicit false the dock stays hidden
 * and the off strip takes its slot; on a refused dial the registry's one look
 * writes the explicit false into the shared query cache, the mirror hides the
 * rail, and the strip explains — after one dial, not ten.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, waitFor, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import '../App'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
// happy-dom has no canvas for xterm: the tab stub only dials, through whichever
// registry instance App booted with (dynamic import, so it follows resetModules).
vi.mock('../components/CliPanel', async () => {
  const { useEffect } = await import('react')
  const fakeTerm = { reset: () => {}, write: () => {}, cols: 80, rows: 24, onData: () => {}, onResize: () => {} }
  function CliPanelStub({ sessionId }: { sessionId: string }) {
    useEffect(() => {
      void import('../utils/terminalRegistry').then(m =>
        m.ensureTerminalConnection(sessionId, fakeTerm as never, { fit: () => {} } as never))
    }, [sessionId])
    return <div data-testid="cli-panel" />
  }
  return {
    default: CliPanelStub,
    disposeTerminalSession: vi.fn(),
    useDeleteTerminalSession: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
  }
})
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
    // The registry's look after a refused dial, served by the msw handler of each case.
    terminalSessions: (signal?: AbortSignal) => fetch('/api/terminal/sessions', { signal }),
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

const STORAGE_KEY = 'mc-bottom-terminal'
const OPEN_KEY = 'mc-bottom-terminal-open'

const WS_INSTANCES: RefusingWebSocket[] = []
/** Every dial is refused: the upgrade errors and closes without ever opening. */
class RefusingWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = RefusingWebSocket.CONNECTING
  binaryType = 'blob'
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn(() => { this.readyState = RefusingWebSocket.CLOSED; this.onclose?.(new CloseEvent('close')) })
  constructor(public url: string) {
    WS_INSTANCES.push(this)
    queueMicrotask(() => this.onerror?.(new Event('error')))
  }
}

let store: typeof import('../hooks/useBottomTerminal') | null = null
let registry: typeof import('../utils/terminalRegistry') | null = null

/** Persist one docked tab, then boot the store and App fresh so module init restores it. */
async function bootApp() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ tabs: [{ id: 'restored' }], activeId: 'restored' }))
  sessionStorage.setItem(OPEN_KEY, '1')
  vi.resetModules()
  store = await import('../hooks/useBottomTerminal')
  registry = await import('../utils/terminalRegistry')
  // The registry's look writes the SHARED client (the one main.tsx provides), so
  // App must read from that same instance for the mirror to see the write.
  const { queryClient } = await import('../api/queryClient')
  const { QueryClientProvider } = await import('@tanstack/react-query')
  const { Provider } = await import('react-redux')
  const { MemoryRouter } = await import('react-router-dom')
  const { ThemeProvider } = await import('../hooks/useTheme')
  const { createTestStore } = await import('./helpers')
  const { render } = await import('@testing-library/react')
  const { default: App } = await import('../App')
  queryClient.clear()
  const redux = createTestStore()
  await act(async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <Provider store={redux}>
          <ThemeProvider>
            <MemoryRouter initialEntries={['/chat']}><App /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  return { registry }
}

afterEach(() => {
  act(() => {
    store?.__resetBottomTerminal()
    registry?.setTerminalEnabledFlag(false)
  })
  store = null
  registry = null
  WS_INSTANCES.length = 0
  localStorage.clear()
  sessionStorage.clear()
  vi.unstubAllGlobals()
})

describe('App — the terminal-off strip', () => {
  it('shows the strip, not the dock, when the probe says off and a tab was restored', async () => {
    server.use(http.get('/api/terminal/sessions', () => HttpResponse.json({ enabled: false, sessions: [] })))
    const { registry } = await bootApp()
    await waitFor(() => expect(registry.isTerminalEnabled()).toBe(false))
    await waitFor(() => expect(screen.getByTestId('terminal-off-notice')).toBeInTheDocument())
    expect(screen.queryByTestId('bottom-terminal-panel')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Terminal' })).toBeNull()
  })

  it('end to end: a refused dial looks once, hides the rail and explains', async () => {
    // The probe cannot rule (a 5xx), so the restored tab mounts and dials; the
    // dial is refused, and the look that follows is answered by the same route.
    let calls = 0
    server.use(http.get('/api/terminal/sessions', () => {
      calls += 1
      return calls === 1
        ? new HttpResponse(null, { status: 500 })
        : HttpResponse.json({ enabled: false, sessions: [] })
    }))
    vi.stubGlobal('WebSocket', RefusingWebSocket)
    const { registry } = await bootApp()
    // The failed probe reads enabled, the tab mounts and dials, the refusal's
    // look answers false: the end state is what the user sees.
    await waitFor(() => expect(screen.getByTestId('terminal-off-notice')).toBeInTheDocument(), { timeout: 6_000 })
    expect(calls).toBe(2)
    expect(registry.isTerminalEnabled()).toBe(false)
    expect(screen.queryByTestId('bottom-terminal-panel')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Terminal' })).toBeNull()
    expect(WS_INSTANCES).toHaveLength(1)
  })
})
