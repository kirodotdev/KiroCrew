/**
 * The rail beside a fold with the side panel open (shell/appShell.tsx,
 * `foldHoldsRailCompact` with `paneMin`). In book pose on a 951px tab an
 * expanded rail leaves the list room before the fold, so the rail is free. With
 * the companion layout showing, the transcript sits beside the rail instead and
 * needs the chat pane's minimum, which an expanded rail would take: the rail is
 * held compact and its toggle is locked. Dropping the `paneMin` argument at the
 * `foldHoldsRailCompact` call fails the first case.
 *
 * Harness as App.appNavHiddenFilter.test.tsx: the real shell and nav rail,
 * routed pages mocked.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, act, cleanup, waitFor } from '@testing-library/react'
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
import { setFoldCompanionShown } from '../lib/foldCompanion'
import { i18nT } from '../i18n/t'

// Book pose on the iPhone Duo's Safari tab, in the desktop form a fold gives it.
vi.mock('../hooks/useViewportSegments', async (orig) => ({
  ...(await orig<typeof import('../hooks/useViewportSegments')>()),
  useViewportSegments: () => [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }],
}))
vi.mock('../hooks/useIsMobile', async (orig) => ({
  ...(await orig<typeof import('../hooks/useIsMobile')>()),
  useIsMobile: () => false,
}))

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

afterEach(() => { act(() => setFoldCompanionShown(false)); cleanup(); localStorage.clear() })

const railToggle = () => screen.getByRole('button', { name: /sidebar/i })

/** Renders App and waits for its first reads (the apps list among them) to land. */
async function renderSettled() {
  renderApp()
  await waitFor(() => expect(vi.mocked(api.listApps)).toHaveBeenCalled())
  await waitFor(() => expect(railToggle()).toBeInTheDocument())
}

describe('the rail in book pose', () => {
  it('is held compact, its toggle locked, while the side panel holds the trailing segment', async () => {
    await renderSettled()
    act(() => setFoldCompanionShown(true))
    await waitFor(() => expect(railToggle()).toBeDisabled())
    expect(railToggle()).toHaveAccessibleName(i18nT('app.expand_sidebar'))
  })
  it('stays free with the panel closed: the list fits before the fold beside an expanded rail', async () => {
    await renderSettled()
    expect(railToggle()).not.toBeDisabled()
  })
})
