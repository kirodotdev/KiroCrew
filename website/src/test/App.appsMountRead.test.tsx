/**
 * The shell's mount read of `GET /api/apps` shares the request an ['apps']
 * observer mounted in the same commit already started, instead of sending a
 * second identical one; a refresh after `mc:apps-changed` still reads the
 * server again. Same isolation shape as App.appNavHiddenFilter.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
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
import { api } from '../api/client'

// Mock the routed pages so App mounts without real page trees — the test
// asserts the NAV RAIL's rows, not page content.
// The chat page stands in for every ['apps'] observer the real page mounts in
// the same commit as the shell (the composer's session controls, the panel-tab
// registry): it reads the list through the shared key, as they do.
vi.mock('../pages/ChatPage', async () => {
  const { useQuery } = await import('@tanstack/react-query')
  const { api: mocked } = await import('../api/client')
  function ChatPageAppsObserver() {
    const { data } = useQuery({ queryKey: ['apps'], queryFn: () => mocked.listApps() })
    return <div data-testid="chat-page">{Array.isArray(data) ? data.length : 'none'}</div>
  }
  return { default: ChatPageAppsObserver }
})
vi.mock('../pages/apps/DiscoverPage', () => ({ default: () => <div data-testid="discover-page" /> }))
vi.mock('../pages/apps/LibraryPage', () => ({ default: () => <div data-testid="library-page" /> }))
vi.mock('../pages/AppPage', () => ({ default: () => <div data-testid="app-page" /> }))
vi.mock('../pages/AppDetailPage', () => ({ default: () => <div data-testid="app-detail-page" /> }))
vi.mock('../pages/MigrationPage', () => ({ default: () => <div data-testid="migration-page" /> }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/SettingsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../pages/HooksPage', () => ({ default: () => null }))
vi.mock('../pages/CapabilitiesPage', () => ({ default: () => null }))
vi.mock('../pages/KnowledgePage', () => ({ default: () => null }))
vi.mock('../pages/DeveloperPage', () => ({ default: () => null }))
vi.mock('../pages/ArtifactsPage', () => ({ default: () => null }))
vi.mock('../pages/ArtifactDetailPage', () => ({ default: () => null }))
vi.mock('../pages/ArtifactDeployPage', () => ({ default: () => null }))
vi.mock('../pages/EmbedSettingsPage', () => ({ default: () => null }))
vi.mock('../pages/PopoutFrame', () => ({ default: () => null }))
vi.mock('../pages/ArtifactPopoutFrame', () => ({ default: () => null }))
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
    listRegistry: vi.fn().mockResolvedValue({ apps: [], categoryOrder: [], editorialSections: [] }),
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
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

/** An installed app with a UI page, so it gets a sidebar row. */
const keeperApp = {
  name: 'keeper-demo',
  displayName: 'Keeper Demo',
  enabled: true,
  origin: 'registry',
  lifecycle: 'gateway',
  manifest: { ui: { entry: 'index.mjs', pages: [{ route: '/', label: 'Keeper Demo' }] } },
}

const refreshedApp = {
  ...keeperApp,
  name: 'refreshed-demo',
  displayName: 'Refreshed Demo',
  manifest: { ui: { entry: 'index.mjs', pages: [{ route: '/', label: 'Refreshed Demo' }] } },
}

/** Renders App at /chat (nav rail visible, no store page mounted). */
function renderApp() {
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
          <MemoryRouter initialEntries={['/chat']}>
            <App />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  return qc
}

const listAppsCalls = () => vi.mocked(api.listApps).mock.calls.length

describe('App apps-nav mount read', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.mocked(api.listApps).mockClear()
    vi.mocked(api.listApps).mockResolvedValue([keeperApp])
  })

  it('issues one GET /api/apps when an ["apps"] observer mounts with the shell', async () => {
    renderApp()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Keeper Demo' })).toBeInTheDocument())
    await waitFor(() => expect(screen.getByTestId('chat-page')).toHaveTextContent('1'))
    expect(listAppsCalls()).toBe(1)
  })

  it('still refetches after mc:apps-changed', async () => {
    renderApp()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Keeper Demo' })).toBeInTheDocument())
    const before = listAppsCalls()
    act(() => { window.dispatchEvent(new Event('mc:apps-changed')) })
    await waitFor(() => expect(listAppsCalls()).toBe(before + 1))
  })

  it('keeps the changed apps list when the slower boot GET resolves last', async () => {
    let resolveBoot!: (apps: typeof keeperApp[]) => void
    const boot = new Promise<typeof keeperApp[]>(resolve => { resolveBoot = resolve })
    vi.mocked(api.listApps)
      .mockReset()
      .mockImplementationOnce(() => boot)
      .mockResolvedValueOnce([refreshedApp])

    const qc = renderApp()
    await waitFor(() => expect(listAppsCalls()).toBe(1))

    act(() => { window.dispatchEvent(new Event('mc:apps-changed')) })
    await waitFor(() => expect(listAppsCalls()).toBe(2))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refreshed Demo' })).toBeInTheDocument())

    await act(async () => { resolveBoot([keeperApp]); await boot })

    expect(qc.getQueryData(['apps'])).toEqual([refreshedApp])
    expect(screen.queryByRole('button', { name: 'Keeper Demo' })).not.toBeInTheDocument()
  })
})
