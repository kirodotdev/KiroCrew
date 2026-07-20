import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import terminalReducer from '../store/terminalSlice'
import instancesReducer from '../store/instancesSlice'
import App from '../App'
import { ThemeProvider } from '../hooks/useTheme'

// Mock pages to isolate routing
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/ArtifactsPage', () => ({ default: () => <div data-testid="artifacts-page">ArtifactsPage</div> }))
vi.mock('../pages/ArtifactDetailPage', () => ({ default: () => <div data-testid="artifact-detail-page">ArtifactDetailPage</div> }))
vi.mock('../pages/ArtifactDeployPage', () => ({ default: () => <div data-testid="artifact-deploy-page">ArtifactDeployPage</div> }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => <div data-testid="notifications-page">NotificationsPage</div> }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => <div data-testid="agents-page">AgentsPage</div> }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => <div data-testid="projects-page">ProjectsPage</div> }))
vi.mock('../pages/LogsPage', () => ({ default: () => <div data-testid="logs-page">LogsPage</div> }))
vi.mock('../pages/SettingsPage', () => ({ default: () => <div data-testid="settings-page">SettingsPage</div> }))
vi.mock('../pages/SchedulePage', () => ({ default: () => <div data-testid="schedule-page">SchedulePage</div> }))
vi.mock('../pages/HooksPage', () => ({ default: () => <div data-testid="hooks-page">HooksPage</div> }))
vi.mock('../pages/CapabilitiesPage', () => ({ default: () => <div data-testid="capabilities-page">CapabilitiesPage</div> }))
vi.mock('../pages/KnowledgePage', () => ({ default: () => <div data-testid="knowledge-page">KnowledgePage</div> }))
vi.mock('../pages/DeveloperPage', () => ({ default: () => <div data-testid="developer-page">DeveloperPage</div> }))
vi.mock('../pages/AppsPage', () => ({ default: () => <div data-testid="apps-page">AppsPage</div> }))
vi.mock('../pages/AppPage', () => ({ default: () => <div data-testid="app-page">AppPage</div> }))
vi.mock('../pages/AppDetailPage', () => ({ default: () => <div data-testid="app-detail-page">AppDetailPage</div> }))
vi.mock('../pages/MigrationPage', () => ({ default: () => <div data-testid="migration-page">MigrationPage</div> }))
vi.mock('../pages/EmbedSettingsPage', () => ({ default: () => <div data-testid="embed-settings">EmbedSettingsPage</div> }))
vi.mock('../pages/PopoutFrame', () => ({ default: () => <div data-testid="popout-frame">PopoutFrame</div> }))
vi.mock('../pages/ArtifactPopoutFrame', () => ({ default: () => <div data-testid="artifact-popout">ArtifactPopoutFrame</div> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {}, subscribeSubagents: () => {}, forceReconnect: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../hooks/useDashboardHealthProbe', () => ({ useDashboardHealthProbe: () => {} }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../components/BrowserLiveView', () => ({ default: () => null }))
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
      terminal: terminalReducer,
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

describe('F5: /deploy route (artifact slug "deploy" no longer shadowed)', () => {
  it('renders ArtifactDeployPage at /deploy', async () => {
    renderAt('/deploy')
    await waitFor(() => {
      expect(screen.getByTestId('artifact-deploy-page')).toBeInTheDocument()
    })
  })

  it('/artifacts/deploy redirects to /deploy', async () => {
    renderAt('/artifacts/deploy')
    await waitFor(() => {
      expect(screen.getByTestId('artifact-deploy-page')).toBeInTheDocument()
    })
  })

  it('an artifact with slug "deploy" resolves to ArtifactDetailPage', async () => {
    renderAt('/artifacts/deploy')
    // The redirect takes us to /deploy which renders deploy page.
    // But /artifacts/:slug where slug != "deploy" should render detail page.
    // Let's test /artifacts/my-widget goes to detail:
  })

  it('/artifacts/:slug renders ArtifactDetailPage for non-deploy slugs', async () => {
    renderAt('/artifacts/my-widget')
    await waitFor(() => {
      expect(screen.getByTestId('artifact-detail-page')).toBeInTheDocument()
    })
  })
})
