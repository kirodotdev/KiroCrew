/**
 * App reads about fifty things (queries, store fields, its own state) and
 * renders again when any of them changes. The routed page must not render
 * again with it: react-router hands each page a new route context whenever
 * `<Routes>` renders, so every page that calls a router hook (the chat page,
 * the sidebar inside it) used to be drawn again for a change only the shell
 * cares about. This pins that App's own render stops at the routes, through
 * App's real route table, and that a route change still reaches the page.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useNavigate, useParams } from 'react-router-dom'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { sseConnected, sseDisconnected } from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import App from '../App'
import { ThemeProvider } from '../hooks/useTheme'

const page = vi.hoisted(() => ({ renders: 0, go: (_to: string) => {} }))

// The page stands in for ChatPage: like it, it reads the route (`useParams`
// reads react-router's route context), and it counts how often it renders.
vi.mock('../pages/ChatPage', () => {
  function PageProbe() {
    const { slug } = useParams()
    page.go = useNavigate()
    page.renders += 1
    return <div data-testid="chat-page">{slug ?? 'no-slug'}</div>
  }
  return { default: PageProbe }
})
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
  render(
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
  return store
}

/** Wait for the page, then let App's own mount reads and effects finish. */
async function settled() {
  await screen.findByTestId('chat-page')
  await act(async () => {})
  await act(async () => {})
}

describe('App renders the routed page once per route change, not once per App render', () => {
  it('a change only the shell reads does not render the page again', async () => {
    const store = renderAt('/chat/alpha')
    await settled()
    const connectedBefore = store.getState().dashboard.connected
    const before = page.renders

    // `dashboard.connected` is read by App (the top bar's offline state) and not
    // by the page above. Flip it both ways so App renders at least once.
    await act(async () => { store.dispatch(connectedBefore ? sseDisconnected() : sseConnected()) })
    await act(async () => { store.dispatch(connectedBefore ? sseConnected() : sseDisconnected()) })
    expect(store.getState().dashboard.connected).toBe(connectedBefore)

    expect(page.renders).toBe(before)
  })

  it('a route change still reaches the page', async () => {
    renderAt('/chat/alpha')
    await settled()
    expect(screen.getByTestId('chat-page').textContent).toBe('alpha')

    await act(async () => { page.go('/chat/beta') })

    await waitFor(() => expect(screen.getByTestId('chat-page').textContent).toBe('beta'))
  })
})
