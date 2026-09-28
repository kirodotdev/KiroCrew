/**
 * PR1 App Store split — route resolution + sidebar active states, pinned at
 * the App level (real <Routes> block, real nav rail, mocked pages).
 *
 * Behavioral half of the route-order pin (appsSplitRouteOrder.test.ts is the
 * structural half): `/apps/library` must resolve to LibraryPage, never fall
 * through to the `/apps/:name` installed-app catch-all.
 *
 * Sidebar mapping (documented in App.tsx above libraryNavActive):
 *  - 库 (Library) lights on /apps/library and everything under it.
 *  - 发现 (Discover) lights on /apps plus the /apps/detail/* and
 *    /apps/migrate/* storefront flows.
 *  - Installed-app pages (/apps/:name) light NEITHER entry — each installed
 *    app has its own rail row, so exactly one row lights at a time.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
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

// Mock the routed pages so App mounts without real page trees — the test
// asserts ROUTING and the NAV RAIL, not page content. Same isolation shape as
// App.deployRoute.test.tsx.
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page" /> }))
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
// Typed via `unknown` rather than `any`: CI lints with a --max-warnings
// ceiling, so a new no-explicit-any warning here spends budget for nothing.
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

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
    </Provider>,
  )
}

/** The nav rail's Discover / Library rows. role+name finds them whether the
 *  rail is expanded (text label) or collapsed (aria-label). */
const navRow = (name: string) => screen.getByRole('button', { name })

const isActive = (el: HTMLElement) => el.className.includes('nav-active')

describe('App Store split — route resolution', () => {
  it('/apps renders DiscoverPage', async () => {
    renderAt('/apps')
    await waitFor(() => expect(screen.getByTestId('discover-page')).toBeInTheDocument())
  })

  it('/apps/library renders LibraryPage, NOT the /apps/:name catch-all', async () => {
    renderAt('/apps/library')
    await waitFor(() => expect(screen.getByTestId('library-page')).toBeInTheDocument())
    expect(screen.queryByTestId('app-page')).not.toBeInTheDocument()
  })

  it('/apps/detail/:name and /apps/migrate/:name keep working unchanged', async () => {
    const { unmount } = renderAt('/apps/detail/code-review-sage')
    await waitFor(() => expect(screen.getByTestId('app-detail-page')).toBeInTheDocument())
    unmount()
    renderAt('/apps/migrate/code-review-sage')
    await waitFor(() => expect(screen.getByTestId('migration-page')).toBeInTheDocument())
  })

  it('/apps/:name still reaches the installed-app page for non-reserved names', async () => {
    renderAt('/apps/oncall-radar')
    await waitFor(() => expect(screen.getByTestId('app-page')).toBeInTheDocument())
  })
})

describe('App Store split — sidebar active states', () => {
  it('/apps lights Discover, not Library', async () => {
    renderAt('/apps')
    await waitFor(() => expect(navRow('Discover')).toBeInTheDocument())
    expect(isActive(navRow('Discover'))).toBe(true)
    expect(isActive(navRow('Library'))).toBe(false)
  })

  it('/apps/library lights Library, not Discover', async () => {
    renderAt('/apps/library')
    await waitFor(() => expect(navRow('Library')).toBeInTheDocument())
    expect(isActive(navRow('Library'))).toBe(true)
    expect(isActive(navRow('Discover'))).toBe(false)
  })

  it('/apps/detail/* and /apps/migrate/* light Discover (storefront flows)', async () => {
    const { unmount } = renderAt('/apps/detail/code-review-sage')
    await waitFor(() => expect(navRow('Discover')).toBeInTheDocument())
    expect(isActive(navRow('Discover'))).toBe(true)
    expect(isActive(navRow('Library'))).toBe(false)
    unmount()
    renderAt('/apps/migrate/code-review-sage')
    await waitFor(() => expect(navRow('Discover')).toBeInTheDocument())
    expect(isActive(navRow('Discover'))).toBe(true)
  })

  it('/apps/:name (installed-app page) lights NEITHER entry', async () => {
    renderAt('/apps/oncall-radar')
    await waitFor(() => expect(navRow('Discover')).toBeInTheDocument())
    expect(isActive(navRow('Discover'))).toBe(false)
    expect(isActive(navRow('Library'))).toBe(false)
  })
})
