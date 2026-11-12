/**
 * "Ask the agent" inside an app pop-out window, through the REAL shell: App
 * mounted in a document at `/popout/app/<route>`, the app page rendered by the
 * real frame, and the hand-off driven from a real `ErrorNotice` (harness from
 * navRailAppPopoutAction.test.tsx).
 *
 * The notice's control is the soft one (`sendErrorToChat` without `hard`). In
 * this window it must reach the main window's chat with its prompt and leave
 * the app on screen as it was: no `/chat` committed here, so no remount and no
 * history entry a Forward could replay.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { lazy, useEffect } from 'react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import App from '../App'
import ErrorNotice from '../components/ErrorNotice'
import { ThemeProvider } from '../hooks/useTheme'
import { registerBuiltinComponents } from '../apps/builtinRegistry'
import { consumeChatHandoff } from '../utils/errorReport'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page" /> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {}, subscribeSubagents: () => {}, forceReconnect: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../hooks/useDashboardHealthProbe', () => ({ useDashboardHealthProbe: () => {} }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
const popout = vi.hoisted(() => ({ forwardToMain: vi.fn() }))
vi.mock('../utils/appPopout', () => ({
  subscribe: () => () => {},
  // One stable instance: a fresh Set per call would re-render forever.
  getSnapshot: ((empty: ReadonlySet<string>) => () => empty)(new Set<string>()),
  openPopout: vi.fn(),
  focusPopout: vi.fn(),
  returnSelfToMain: vi.fn(),
  registerPopout: vi.fn(() => () => {}),
  forwardToMain: popout.forwardToMain,
  setNavIntentHandler: vi.fn(() => () => {}),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { available: false } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
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

const ROUTE = '/ask-agent-app'
const FAILURE = '503 from the board endpoint'
const mounts = { count: 0 }
/** The page is registered as a `React.lazy` component, which `BuiltinAppRoute`
 *  renders under `Suspense`: it appears only once that lazy boundary resolves. */
const BOARD_READY = { timeout: 5000 }

/** An app page with a load failure on screen and its soft "Ask the agent". */
function FailingBoard() {
  const { pathname, key } = useLocation()
  useEffect(() => { mounts.count += 1 }, [])
  return (
    <div data-testid="board" data-path={pathname} data-key={key}>
      <ErrorNotice title="Could not load the board" message={FAILURE} askAgent />
    </div>
  )
}
registerBuiltinComponents({
  [ROUTE]: { component: lazy(() => Promise.resolve({ default: FailingBoard })), appId: 'ask-agent-app' },
})

function renderPopoutWindow() {
  const store = configureStore({
    reducer: {
      dashboard: dashboardReducer,
      chat: chatReducer,
      notifications: notificationsReducer,
      instances: instancesReducer,
    },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // The window's router is mounted at `/popout/app`, so it reads the app's own path.
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[ROUTE]}>
            <App />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('Ask the agent in an app pop-out window', () => {
  beforeEach(() => {
    popout.forwardToMain.mockClear()
    mounts.count = 0
    sessionStorage.clear()
    // The shell reads which window it is from the document's own address.
    window.history.replaceState(null, '', `/popout/app${ROUTE}`)
  })

  afterEach(() => {
    window.history.replaceState(null, '', '/')
  })

  it('hands the prompt to the main window and leaves the app mounted where it was', async () => {
    renderPopoutWindow()
    const board = await screen.findByTestId('board', undefined, BOARD_READY)
    const keyBefore = board.getAttribute('data-key')
    fireEvent.click(screen.getByRole('button', { name: 'Ask the agent' }))
    expect(popout.forwardToMain).toHaveBeenCalledTimes(1)
    expect(popout.forwardToMain.mock.calls[0][0]).toMatchObject({
      path: '/chat',
      errorHandoffs: [expect.stringContaining(FAILURE)],
    })
    // Spent here: the main window re-stages it.
    expect(consumeChatHandoff()).toBeNull()
    // Nothing committed in this window: the same app instance on the same entry.
    expect(mounts.count).toBe(1)
    expect(screen.getByTestId('board')).toHaveAttribute('data-path', ROUTE)
    expect(screen.getByTestId('board')).toHaveAttribute('data-key', keyBefore)
  })
})
