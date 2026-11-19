/**
 * The shell publishes the rail's width before the page paints (shell/appShell.tsx,
 * the `setRailWidth` layout effect). The sessions list sizes itself from that
 * published width, beside a fold (`foldSidebarWidth`) and without one, so a
 * width published after paint would draw the first frame against the previous
 * rail: at a 669px fold, one frame 74px across the fold. A later root sibling's
 * layout effect records whether the width had been published by the end of the
 * commit's layout phase, the last point before the browser paints.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { useLayoutEffect } from 'react'
import { flushSync } from 'react-dom'
import { createRoot, type Root } from 'react-dom/client'
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
import { useSidebarResize } from '../pages/chat-sidebar/resize'

const segmentsMock = vi.hoisted(() => ({ value: null as null | { x: number; width: number }[] }))
vi.mock('../hooks/useViewportSegments', async (orig) => ({
  ...(await orig<typeof import('../hooks/useViewportSegments')>()),
  useViewportSegments: () => segmentsMock.value,
}))
vi.mock('../hooks/useIsMobile', async (orig) => ({
  ...(await orig<typeof import('../hooks/useIsMobile')>()),
  useIsMobile: () => false,
}))
// Every published width, and whether it came before the end of a layout phase.
const published = vi.hoisted(() => ({ widths: [] as number[], layoutEnds: 0, atLayoutEnd: [] as number[] }))
vi.mock('../hooks/useRailWidth', async (orig) => {
  const real = await orig<typeof import('../hooks/useRailWidth')>()
  return { ...real, setRailWidth: (w: number) => { published.widths.push(w); real.setRailWidth(w) } }
})

// Mock the routed pages so App mounts without real page trees — the test
// asserts the NAV RAIL's rows, not page content.
// The chat page is a probe: the sessions list's sizing hook and the width it paints.
vi.mock('../pages/ChatPage', () => ({ default: function ListProbe() {
  // Read at render time, after the imports below have run.
  const { paintedSidebarWidth } = useSidebarResize({ onWidthChange: undefined, onDragChange: undefined, boardActive: false })
  return <div data-testid="list" data-width={String(paintedSidebarWidth)} />
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

import { setRailWidth as setRailWidthReal, __resetRailWidth } from '../hooks/useRailWidth'

function LayoutEnd() {
  useLayoutEffect(() => { published.atLayoutEnd.push(published.widths.length) })
  return null
}

let root: Root | null = null
let host: HTMLElement | null = null
const actEnv = globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT?: boolean }
const savedActEnv = actEnv.IS_REACT_ACT_ENVIRONMENT

afterEach(() => {
  if (root) flushSync(() => root!.unmount())
  host?.remove()
  root = null
  host = null
  segmentsMock.value = null
  localStorage.clear()
  __resetRailWidth()
  actEnv.IS_REACT_ACT_ENVIRONMENT = savedActEnv
})

/** Renders App at /chat in one synchronous commit, the rail stored compact,
 *  after a phone form that published no rail (0px). */
function renderChat() {
  actEnv.IS_REACT_ACT_ENVIRONMENT = false
  localStorage.setItem('mc-nav', '1')
  __resetRailWidth()
  setRailWidthReal(0)
  published.widths = []
  published.atLayoutEnd = []
  const store = configureStore({ reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer } })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  flushSync(() => root!.render(
    <>
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter initialEntries={['/chat']}>
              <App />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>
      <LayoutEnd />
    </>,
  ))
  return host.querySelector<HTMLElement>('[data-testid="list"]')
}

describe('the rail width the shell publishes', () => {
  it('is current before the first paint at a 669px fold, so the list ends at the fold', () => {
    segmentsMock.value = [{ x: 0, width: 334.5 }, { x: 334.5, width: 334.5 }]
    const list = renderChat()
    expect(published.atLayoutEnd[0]).toBeGreaterThan(0)
    expect(published.widths[0]).toBe(74)
    expect(list).not.toBeNull()
    expect(Number(list!.dataset.width)).toBe(Math.max(180, 334.5 - 74))
  })

  it('is current before the first paint with no fold too', () => {
    const list = renderChat()
    expect(published.atLayoutEnd[0]).toBeGreaterThan(0)
    expect(published.widths[0]).toBe(74)
    expect(list).not.toBeNull()
  })
})
