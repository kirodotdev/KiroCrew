/**
 * The shell's rail track beside a reported fold (App.tsx, the grid's
 * `transition`). A 768px window folded into two 384px segments holds the rail
 * compact (`foldHoldsRailCompact`), and the sessions list is sized to the
 * compact rail at once, so the track snaps: an animated 236px-to-74px track
 * would carry the list across the fold while it ran. With no fold the track
 * keeps its 150ms animation for the person's own toggle.
 *
 * Harness as App.appNavHiddenFilter.test.tsx: the real shell and nav rail,
 * routed pages mocked.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { useLayoutEffect, useSyncExternalStore } from 'react'
import { setSessionsListOnScreen, __resetSessionsListOnScreen } from '../hooks/useSessionsListOnScreen'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
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

const listMock = vi.hoisted(() => ({ open: true, subs: new Set<() => void>() }))
const segmentsMock = vi.hoisted(() => ({ value: null as null | { x: number; width: number }[] }))
vi.mock('../hooks/useViewportSegments', async (orig) => ({
  ...(await orig<typeof import('../hooks/useViewportSegments')>()),
  useViewportSegments: () => segmentsMock.value,
}))
vi.mock('../hooks/useIsMobile', async (orig) => ({
  ...(await orig<typeof import('../hooks/useIsMobile')>()),
  useIsMobile: () => false,
}))

// Mock the routed pages so App mounts without real page trees — the test
// asserts the NAV RAIL's rows, not page content.
vi.mock('../pages/ChatPage', () => ({ default: function ChatPageWithList() {
  // Stands in for the sessions drawer's publisher (sessionsDrawer.ts): the list
  // is on screen while the chat page shows it, and goes when the page goes.
  const open = useSyncExternalStore(cb => { listMock.subs.add(cb); return () => { listMock.subs.delete(cb) } }, () => listMock.open)
  useLayoutEffect(() => { setSessionsListOnScreen(open) }, [open])
  useLayoutEffect(() => () => setSessionsListOnScreen(false), [])
  return <div data-testid="chat-page" data-list={String(open)} />
} }))
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

afterEach(() => { segmentsMock.value = null; listMock.open = true; cleanup(); __resetSessionsListOnScreen(); localStorage.clear() })

/** Renders App and waits for its first reads and the nav rail to land. */
async function renderSettled() {
  // The person's rail is expanded (`mc-nav` 0), so only the fold can take it compact.
  localStorage.setItem('mc-nav', '0')
  renderApp()
  await waitFor(() => expect(vi.mocked(api.listApps)).toHaveBeenCalled())
  await waitFor(() => expect(screen.getByRole('button', { name: /sidebar/i })).toBeInTheDocument())
  return document.querySelector<HTMLElement>('[style*="grid-template-areas"]')!
}

describe('the shell rail track beside a fold', () => {
  it('snaps to the compact rail when a 384px leading segment holds it', async () => {
    segmentsMock.value = [{ x: 0, width: 384 }, { x: 384, width: 384 }]
    const grid = await renderSettled()
    expect(grid.style.gridTemplateColumns.startsWith('74px')).toBe(true)
    expect(grid.style.transition).toBe('none')
  })
  it('snaps a rail the fold leaves free, so the person\'s own toggle never carries the list across', async () => {
    segmentsMock.value = [{ x: 0, width: 416 }, { x: 416, width: 416 }]
    const grid = await renderSettled()
    expect(grid.style.gridTemplateColumns.startsWith('236px')).toBe(true)
    expect(grid.style.transition).toBe('none')
  })
  it('keeps its animation with no fold', async () => {
    const grid = await renderSettled()
    expect(grid.style.gridTemplateColumns.startsWith('236px')).toBe(true)
    expect(grid.style.transition).toContain('grid-template-columns')
  })
})
