/**
 * Sidebar Apps-group filtering by the Library launchpad's hidden set
 * (`mc-app-nav-hidden`, PR3), pinned at the App level — real nav rail, real
 * `readAppNavHidden` derivation, mocked pages.
 *
 * The Library grid's pin badge decides which installed apps appear in the
 * sidebar. The contract this file pins:
 *
 * - an app id present in `mc-app-nav-hidden` has NO sidebar row, while a
 *   sibling app absent from the set keeps its row — membership filters
 *   per-row, not the whole group.
 * - the filter is LIVE: a localStorage write followed by the
 *   `mc:app-nav-hidden-changed` window event (the same-tab path
 *   `lib/appNavHidden.ts` dispatches on every persisted write) hides and
 *   re-shows the row in place, without a remount.
 * - the Discover and Library BUILT-IN rows are never filtered, regardless of
 *   what the storage array contains — they are dedicated section rows
 *   (navIds `apps` / `apps-library`), not launchpad-managed app rows.
 *
 * Same isolation shape as App.discoverUpdatesBadge.test.tsx: routed pages are
 * mocked so App mounts without real page trees.
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
import { APP_NAV_HIDDEN_KEY, APP_NAV_HIDDEN_CHANGED_EVENT } from '../lib/appNavHidden'

// Mock the routed pages so App mounts without real page trees — the test
// asserts the NAV RAIL's rows, not page content.
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

/**
 * Two installed (non-builtin) apps with UI pages. `appNavTarget` routes both
 * through AppHost, so their sidebar nav ids are `app-<name>` — the same ids
 * the Library grid writes into the hidden set.
 */
const pinnerApp = {
  name: 'pinner-demo',
  displayName: 'Pinner Demo',
  enabled: true,
  origin: 'registry',
  lifecycle: 'gateway',
  manifest: { ui: { entry: 'index.mjs', pages: [{ route: '/', label: 'Pinner Demo' }] } },
}
const keeperApp = {
  name: 'keeper-demo',
  displayName: 'Keeper Demo',
  enabled: true,
  origin: 'registry',
  lifecycle: 'gateway',
  manifest: { ui: { entry: 'index.mjs', pages: [{ route: '/', label: 'Keeper Demo' }] } },
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

/** Persist a hidden set and fire the same-tab change event, as one action. */
function writeHiddenAndNotify(ids: string[]) {
  act(() => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify(ids))
    window.dispatchEvent(new Event(APP_NAV_HIDDEN_CHANGED_EVENT))
  })
}

const pinnerRow = () => screen.queryByRole('button', { name: 'Pinner Demo' })
const keeperRow = () => screen.queryByRole('button', { name: 'Keeper Demo' })

describe('Sidebar Apps-group filtering — mc-app-nav-hidden', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.mocked(api.listApps).mockResolvedValue([pinnerApp, keeperApp])
  })

  it('an app id in the stored hidden set has no sidebar row; a sibling keeps its row', async () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify(['app-pinner-demo']))
    renderApp()
    // The control row proves the apps fetch landed — only then is the
    // missing row a FILTERED row rather than a not-yet-fetched one.
    await waitFor(() => expect(keeperRow()).toBeInTheDocument())
    expect(pinnerRow()).not.toBeInTheDocument()
  })

  it('a storage write + mc:app-nav-hidden-changed hides and re-shows the row live, without remount', async () => {
    renderApp()
    await waitFor(() => expect(pinnerRow()).toBeInTheDocument())

    // Unpin: the LibraryPage tile path is exactly this — persist, then
    // dispatch the same-tab event (same-tab writes never fire `storage`).
    writeHiddenAndNotify(['app-pinner-demo'])
    await waitFor(() => expect(pinnerRow()).not.toBeInTheDocument())
    expect(keeperRow()).toBeInTheDocument()

    // Re-pin: the row must come back on the same mount.
    writeHiddenAndNotify([])
    await waitFor(() => expect(pinnerRow()).toBeInTheDocument())
  })

  it('Discover and Library built-in rows are never filtered, whatever the storage holds', async () => {
    // Every id shape that could plausibly point at the two built-ins,
    // including their real navIds — none of them may remove the rows,
    // because Discover/Library are dedicated section rows, not
    // launchpad-managed app rows.
    localStorage.setItem(
      APP_NAV_HIDDEN_KEY,
      JSON.stringify(['apps', 'apps-library', 'discover', 'library', 'app-apps', 'app-apps-library']),
    )
    renderApp()
    await waitFor(() => expect(screen.getByRole('button', { name: /Discover/ })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Library/ })).toBeInTheDocument()

    // And the same holds for a LIVE write after mount.
    writeHiddenAndNotify(['apps', 'apps-library'])
    await waitFor(() => expect(api.listApps).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: /Discover/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Library/ })).toBeInTheDocument()
  })

  it('hiding an app materializes the full order into mc-app-nav-order', async () => {
    // The GPT-review sequence this pins: unpin B (no reorder ever ran, so
    // mc-app-nav-order is empty) -> DISABLE B (its nav row disappears, so
    // nothing remembers its slot) -> reorder others -> re-pin B lands last.
    // The fix: the moment an id is hidden, App.tsx persists the effective
    // full order, so the persisted array is authoritative for hidden ids
    // BEFORE any later event can erase the implicit source.
    renderApp()
    await waitFor(() => expect(pinnerRow()).toBeInTheDocument())
    expect(localStorage.getItem('mc-app-nav-order')).toBeNull()

    writeHiddenAndNotify(['app-pinner-demo'])
    await waitFor(() => expect(pinnerRow()).toBeNull())
    await waitFor(() => {
      const order = JSON.parse(localStorage.getItem('mc-app-nav-order') || '[]')
      expect(order).toContain('app-pinner-demo')
      expect(order).toContain('app-keeper-demo')
    })
  })

  it('a failed order write does not loop the materialize effect', async () => {
    // Denied/full storage: setItem throws, so the persisted order never
    // gains the hidden id and the effect's fresh-read guard stays
    // unsatisfied. The state mirror must be gated on write success —
    // setting it anyway feeds a new array reference back into the effect's
    // own deps every render, an unbounded update loop that LIVELOCKS the
    // worker (it starves even the test-timeout timer). So the pin is a
    // bounded discriminator, never a wait-for-the-loop: the spy fails the
    // first 3 order writes then succeeds, which caps a regressed loop at 4
    // passes. The guarded effect stops after write #1 (no state change, no
    // re-run until deps change); the unguarded one retries ≥2 times.
    renderApp()
    await waitFor(() => expect(pinnerRow()).toBeInTheDocument())

    const setItem = Storage.prototype.setItem
    let orderWrites = 0
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(
      function (this: Storage, key: string, value: string) {
        if (key === 'mc-app-nav-order' && ++orderWrites <= 3) {
          throw new DOMException('denied', 'SecurityError')
        }
        return setItem.call(this, key, value)
      })
    try {
      writeHiddenAndNotify(['app-pinner-demo'])
      // The row still hides (the hidden set is React state, not order-gated)…
      await waitFor(() => expect(pinnerRow()).toBeNull())
      // …and the effect made exactly one (failed) attempt: the failure must
      // not feed back into its own deps.
      expect(orderWrites).toBe(1)
      expect(localStorage.getItem('mc-app-nav-order')).toBeNull()
    } finally {
      spy.mockRestore()
    }
  })
})
