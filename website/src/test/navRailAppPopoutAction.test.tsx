/**
 * The Apps-group rail row's pop-out action, pinned at the App level: real nav
 * rail, mocked pages (harness from App.appNavHiddenFilter.test.tsx).
 *
 * - on the expanded desktop rail, an installed app's row has the action as a
 *   SIBLING of its `role="button"` row element, never a descendant (a button
 *   inside a button is one control to assistive tech);
 * - the collapsed rail renders no action at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
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

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page" /> }))
vi.mock('../pages/apps/DiscoverPage', () => ({ default: () => null }))
vi.mock('../pages/apps/LibraryPage', () => ({ default: () => null }))
vi.mock('../pages/AppPage', () => ({ default: () => null }))
vi.mock('../pages/AppDetailPage', () => ({ default: () => null }))
vi.mock('../pages/MigrationPage', () => ({ default: () => null }))
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
vi.mock('../utils/appPopout', () => ({
  subscribe: () => () => {},
  // One stable instance: a fresh Set per call would re-render forever.
  getSnapshot: ((empty: ReadonlySet<string>) => () => empty)(new Set<string>()),
  openPopout: vi.fn(),
  focusPopout: vi.fn(),
  returnSelfToMain: vi.fn(),
  registerPopout: vi.fn(() => () => {}),
  forwardToMain: vi.fn(),
  setNavIntentHandler: vi.fn(() => () => {}),
}))
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

/** An installed app with a UI page: AppHost-routed, nav id `app-demo-tool`, route `/apps/demo-tool`. */
const demoApp = {
  name: 'demo-tool',
  displayName: 'Demo Tool',
  enabled: true,
  origin: 'registry',
  lifecycle: 'gateway',
  manifest: { ui: { entry: 'index.mjs', pages: [{ route: '/', label: 'Demo Tool' }] } },
}

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
}

const demoRow = () => document.querySelector<HTMLElement>('[data-onboarding-nav="app-demo-tool"]')
const actions = () => document.querySelectorAll('[data-testid="app-row-popout-action"]')

describe('the Apps-group rail row pop-out action', () => {
  beforeEach(() => {
    localStorage.clear()
    // The rail is collapsed by default; these cases are about the expanded rail.
    localStorage.setItem('mc-nav', '0')
    vi.mocked(api.listApps).mockResolvedValue([demoApp] as never)
  })

  it('sits beside the row element on the expanded rail, not inside it', async () => {
    renderApp()
    await waitFor(() => expect(demoRow()).not.toBeNull())
    const row = demoRow()!
    expect(row).toHaveAttribute('role', 'button')
    const action = row.parentElement!.querySelector('[data-testid="app-row-popout-action"]')
    expect(action).not.toBeNull()
    expect(action).toHaveAccessibleName('Pop out app to its own window')
    expect(row.contains(action)).toBe(false)
    expect(action!.parentElement).toBe(row.parentElement)
    expect(row.querySelector('button, [role="button"]')).toBeNull()
  })

  it('renders none on the collapsed rail', async () => {
    localStorage.setItem('mc-nav', '1')
    renderApp()
    await waitFor(() => expect(demoRow()).not.toBeNull())
    expect(actions()).toHaveLength(0)
  })
})
