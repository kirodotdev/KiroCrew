/**
 * Sidebar Discover updates badge (PR2 App Store split), pinned at the App
 * level — real nav rail, real `countUpdatables` derivation, mocked pages.
 *
 * The badge must show THE SAME count as the Discover Updates sub-tab, so it
 * derives from the same two react-query caches the store pages own
 * (`['registry']` + `['apps']`) via the shared `countUpdatables`:
 *
 * - a gateway-lifecycle installed app with a pending registry update counts;
 *   a self-managed (`app`-lifecycle) one does NOT — the store cannot update
 *   it, so the badge must not promise work the Updates page won't list.
 * - zero pending updates → NO badge element at all (BadgeIndicator renders
 *   null), not a "0".
 * - a cold cache (store pages never visited) reads as no known updates.
 * - the count is LIVE: a cache write after mount (the mc:apps-changed refetch
 *   path) updates the badge without a remount.
 *
 * Same isolation shape as App.appsSplitNav.test.tsx: routed pages are mocked
 * so App mounts without real page trees.
 */
import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest'
import { render, screen, waitFor, within, act } from '@testing-library/react'
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
// asserts the NAV RAIL's badge, not page content.
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

// The cache shapes the badge derivation reads (see updatables.ts): only the
// server-emitted fields — the ['apps'] cache can hold RAW rows.
type RegistryRow = { name: string; version: string; updateAvailable: boolean }
type InstalledRow = {
  name: string; lifecycle: string; origin: string; enabled: boolean
  manifest?: Record<string, unknown>
}

/**
 * Seed the two payloads the badge derives from. The `['registry']` cache is
 * seeded directly (the shell registers no observer or queryFn for it, so the
 * write survives); the installed rows go through the `listApps` MOCK, because
 * App's own refreshAppNav fetches it on mount and writes the `['apps']` cache
 * — a direct seed would just be overwritten by that fetch.
 */
function seedAppsData(qc: QueryClient, registry: RegistryRow[], installed: InstalledRow[]) {
  qc.setQueryData(['registry'], { apps: registry })
  vi.mocked(api.listApps).mockResolvedValue(installed)
}

/** Renders App at /chat with the QueryClient handed back for cache seeding. */
function renderApp(seed?: (qc: QueryClient) => void) {
  const store = configureStore({
    reducer: {
      dashboard: dashboardReducer,
      chat: chatReducer,
      notifications: notificationsReducer,
      instances: instancesReducer,
    },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  seed?.(qc)
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

/** The Discover nav row. Regex name: with a badge present, the badge span's
 *  aria-label joins the row's accessible name ("Discover 1 updates"). */
const discoverRow = () => screen.getByRole('button', { name: /Discover/ })

const secretaryRegistry: RegistryRow = { name: 'secretary', version: '1.1.0', updateAvailable: true }
const radarRegistry: RegistryRow = { name: 'radar', version: '2.1.0', updateAvailable: true }
const secretaryInstalled: InstalledRow = { name: 'secretary', lifecycle: 'gateway', origin: 'registry', enabled: true }
// Self-managed lifecycle: the store cannot update it, so it must not count.
const radarInstalled: InstalledRow = { name: 'radar', lifecycle: 'app', origin: 'registry', enabled: true }

describe('Sidebar Discover badge — pending app updates', () => {
  beforeEach(() => {
    // Each test owns its installed rows; without a reset the previous test's
    // listApps payload would leak into the cold-cache case.
    vi.mocked(api.listApps).mockResolvedValue([])
  })

  it('shows the updatable count, filtered to gateway-lifecycle apps', async () => {
    renderApp(qc => seedAppsData(qc, [secretaryRegistry, radarRegistry], [secretaryInstalled, radarInstalled]))
    await waitFor(() => expect(discoverRow()).toBeInTheDocument())
    // 1, not 2: radar's pending update is self-managed and the Updates page
    // would not list it — the badge and the page must agree. findBy: the
    // installed rows arrive via App's own listApps fetch.
    expect(await within(discoverRow()).findByText('1')).toBeInTheDocument()
  })

  it('renders NO badge when nothing is updatable (hidden at zero, not "0")', async () => {
    renderApp(qc => seedAppsData(
      qc,
      [{ ...secretaryRegistry, updateAvailable: false }],
      [secretaryInstalled],
    ))
    await waitFor(() => expect(discoverRow()).toBeInTheDocument())
    // Wait for the listApps fetch to have landed, so this pins "computed 0 →
    // hidden" rather than "not computed yet".
    await waitFor(() => expect(api.listApps).toHaveBeenCalled())
    expect(within(discoverRow()).queryByText(/^\d+$/)).not.toBeInTheDocument()
  })

  it('a fresh session fetches the registry itself and shows the badge (no store page visit)', async () => {
    // The badge must not depend on a store page having populated the cache:
    // the shell registers its own observer on the shared registryQueryFn
    // boundary. listApps carries the installed row; the registry payload
    // comes from the shell's own fetch.
    ;(api.listRegistry as Mock).mockResolvedValue({
      apps: [secretaryRegistry], categoryOrder: [], editorialSections: [],
    })
    ;(api.listApps as Mock).mockResolvedValue([secretaryInstalled])
    renderApp()
    await waitFor(() => expect(discoverRow()).toBeInTheDocument())
    await waitFor(() => expect(api.listRegistry).toHaveBeenCalled())
    await waitFor(() => expect(within(discoverRow()).getByText('1')).toBeInTheDocument())
  })

  it('an empty registry answer keeps the badge hidden', async () => {
    renderApp()
    await waitFor(() => expect(discoverRow()).toBeInTheDocument())
    await waitFor(() => expect(api.listRegistry).toHaveBeenCalled())
    expect(within(discoverRow()).queryByText(/^\d+$/)).not.toBeInTheDocument()
  })

  it('a cache write after mount updates the badge live (no remount)', async () => {
    const qc = renderApp()
    await waitFor(() => expect(discoverRow()).toBeInTheDocument())
    expect(within(discoverRow()).queryByText(/^\d+$/)).not.toBeInTheDocument()

    // The mc:apps-changed refetch path lands new payloads in the caches; the
    // badge subscribes to the query cache, so it must pick this up in place.
    act(() => {
      qc.setQueryData(['registry'], { apps: [secretaryRegistry, radarRegistry] })
      qc.setQueryData(['apps'], [secretaryInstalled, { ...radarInstalled, lifecycle: 'gateway' }])
    })
    await waitFor(() => expect(within(discoverRow()).getByText('2')).toBeInTheDocument())
  })
})
