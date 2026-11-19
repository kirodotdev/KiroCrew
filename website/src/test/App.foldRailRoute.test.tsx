/**
 * Where the fold holds the rail compact (shell/appShell.tsx, `foldCompactRail`).
 * The hold exists for the sessions list, which ends at the fold, so it applies
 * on the chat route only. On every other route (Settings, Logs, an app page)
 * the rail and its toggle stay the person's, and a stored expanded rail is
 * honoured beside the fold as it is on main.
 *
 * Harness as App.foldRailSnap.test.tsx: the real shell and nav rail, routed
 * pages mocked.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { useLayoutEffect, useSyncExternalStore } from 'react'
import { setSessionsListOnScreen, __resetSessionsListOnScreen, useSessionsListOnScreen } from '../hooks/useSessionsListOnScreen'
import { render, screen, cleanup, waitFor, act } from '@testing-library/react'
import { MemoryRouter, useNavigate } from 'react-router-dom'
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
import { i18nT } from '../i18n/t'

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

/** Renders App at `path` with the person's rail expanded (`mc-nav` 0). */
async function renderAt(path: string) {
  localStorage.setItem('mc-nav', '0')
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const nav: { go: ((to: string) => void) | null } = { go: null }
  const NavHandle = () => { nav.go = useNavigate(); return null }
  const tree = () => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[path]}>
            <App />
            <NavHandle />
            <ListFlag />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
  const { rerender } = render(tree())
  await waitFor(() => expect(vi.mocked(api.listApps)).toHaveBeenCalled())
  await waitFor(() => expect(screen.getByRole('button', { name: /sidebar|folded/i })).toBeInTheDocument())
  return {
    grid: document.querySelector<HTMLElement>('[style*="grid-template-areas"]')!,
    toggle: screen.getByRole('button', { name: /sidebar|folded/i }),
    rerender: () => rerender(tree()),
    navigate: (to: string) => nav.go!(to),
  }
}

/** The published list flag, drawn so a test can read it. */
function ListFlag() {
  return <div data-testid="list-flag" data-on={String(useSessionsListOnScreen())} />
}
const useSessionsListOnScreenSnapshot = () => document.querySelector('[data-testid="list-flag"]')!.getAttribute('data-on') === 'true'

afterEach(() => { segmentsMock.value = null; listMock.open = true; cleanup(); __resetSessionsListOnScreen(); localStorage.clear() })

/** A 768px window folded into two 384px segments: an expanded rail (236px)
 *  plus the 180px list floor does not fit before the fold. */
const NARROW_FOLD = [{ x: 0, width: 384 }, { x: 384, width: 384 }]

describe('the fold rail hold, by route', () => {
  it('on Settings the rail stays the person\'s: expanded as stored, toggle enabled', async () => {
    segmentsMock.value = NARROW_FOLD
    const { grid, toggle } = await renderAt('/settings')
    expect(grid.style.gridTemplateColumns.startsWith('236px')).toBe(true)
    expect(toggle).not.toHaveAttribute('aria-disabled')
    expect(toggle).toHaveAccessibleName(i18nT('app.collapse_sidebar'))
  })

  it('on the chat route the same fold holds the rail compact and locks its toggle', async () => {
    segmentsMock.value = NARROW_FOLD
    const { grid, toggle } = await renderAt('/chat')
    expect(grid.style.gridTemplateColumns.startsWith('74px')).toBe(true)
    expect(toggle).toHaveAttribute('aria-disabled', 'true')
  })

  it('on the chat route a leading segment of 416px or more leaves the rail free', async () => {
    segmentsMock.value = [{ x: 0, width: 416 }, { x: 416, width: 416 }]
    const { grid, toggle } = await renderAt('/chat')
    expect(grid.style.gridTemplateColumns.startsWith('236px')).toBe(true)
    expect(toggle).not.toHaveAttribute('aria-disabled')
  })

  it('on the chat route with the sessions list closed the same fold leaves the rail free', async () => {
    segmentsMock.value = NARROW_FOLD
    listMock.open = false
    const { grid, toggle } = await renderAt('/chat')
    expect(grid.style.gridTemplateColumns.startsWith('236px')).toBe(true)
    expect(toggle).not.toHaveAttribute('aria-disabled')
  })
})

describe('the fold rail hold follows the sessions list', () => {
  it('closing the list while held frees the rail in the same commit', async () => {
    segmentsMock.value = NARROW_FOLD
    const view = await renderAt('/chat')
    expect(view.grid.style.gridTemplateColumns.startsWith('74px')).toBe(true)
    expect(view.toggle).toHaveAttribute('aria-disabled', 'true')
    // The person closes the list: the page re-renders with it gone.
    act(() => { listMock.open = false; for (const cb of [...listMock.subs]) cb() })
    expect(document.querySelector('[data-testid="chat-page"]')!.getAttribute('data-list')).toBe('false')
    expect(view.grid.style.gridTemplateColumns.startsWith('236px')).toBe(true)
    expect(screen.getByRole('button', { name: /sidebar|folded/i })).not.toHaveAttribute('aria-disabled')
  })

  it('leaving the chat page clears the published list, so the rail is free', async () => {
    segmentsMock.value = NARROW_FOLD
    const view = await renderAt('/chat')
    expect(view.toggle).toHaveAttribute('aria-disabled', 'true')
    act(() => { view.navigate('/settings') })
    await waitFor(() => expect(document.querySelector('[data-testid="chat-page"]')).toBeNull())
    expect(useSessionsListOnScreenSnapshot()).toBe(false)
    expect(view.grid.style.gridTemplateColumns.startsWith('236px')).toBe(true)
    expect(screen.getByRole('button', { name: /sidebar|folded/i })).not.toHaveAttribute('aria-disabled')
  })
})
