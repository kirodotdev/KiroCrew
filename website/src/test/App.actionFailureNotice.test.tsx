/**
 * The sink for `reportActionFailure` must be mounted where a page cannot take it
 * away.
 *
 * `reportActionFailure` is a module-level store precisely so a write reported
 * from a menu subtree that unmounts still lands somewhere — but the sink itself
 * lived in `ChatPage`, which is inside `<Routes>`. A session write issued from
 * the global command palette on a non-chat route (Settings + "Pin current
 * session") therefore rolled back and reported into an unmounted page: silent,
 * the dead end `errors-use-error-notice` forbids.
 *
 * This renders the REAL App route table at a non-chat path with `ChatPage`
 * mocked away, so a sink that depends on the chat page cannot satisfy it.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import App from '../App'
import { ThemeProvider } from '../hooks/useTheme'
import { reportActionFailure, __resetActionFailureForTests } from '../utils/actionFailure'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/CapabilitiesPage', () => ({ default: () => <div data-testid="capabilities-page">Capabilities</div> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {}, subscribeSubagents: () => {}, forceReconnect: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../hooks/useDashboardHealthProbe', () => ({ useDashboardHealthProbe: () => {} }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
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
    approvals: vi.fn().mockResolvedValue([]),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class extends Error { status: number; constructor(s: number, m: string) { super(m); this.status = s } },
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation(query => ({
    matches: false, media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
})

function renderAt(path: string) {
  const store = configureStore({
    reducer: {
      dashboard: dashboardReducer,
      chat: chatReducer,
      notifications: notificationsReducer,
      instances: instancesReducer,
    },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[path]}>
            <App />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
}

describe('a rejected session write reaches a surface off the chat route', () => {
  it('renders the notice on a non-chat route', async () => {
    renderAt('/capabilities')
    await waitFor(() => { expect(screen.getByTestId('capabilities-page')).toBeInTheDocument() })
    expect(screen.queryByTestId('chat-page')).toBeNull()
    __resetActionFailureForTests()
    act(() => { reportActionFailure("Couldn't change this session's pin — the change was undone.", 'Research notes') })
    const notice = await screen.findByTestId('session-action-error')
    expect(notice.textContent).toContain('Research notes')
  })

  it('mounts the sink outside the route table, so it is not a page surface', async () => {
    renderAt('/capabilities')
    await waitFor(() => { expect(screen.getByTestId('capabilities-page')).toBeInTheDocument() })
    __resetActionFailureForTests()
    act(() => { reportActionFailure('Session reload failed.') })
    const notice = await screen.findByTestId('session-action-error')
    expect(notice.contains(screen.getByTestId('capabilities-page'))).toBe(false)
    expect(screen.getByTestId('capabilities-page').contains(notice)).toBe(false)
  })
})
