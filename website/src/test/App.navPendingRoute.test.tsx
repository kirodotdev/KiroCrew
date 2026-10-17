/**
 * Regression test: the rail's chat row acts on where the app is GOING, not on
 * the route it still renders.
 *
 * react-router wraps every location update in `React.startTransition`. A click
 * on another row therefore moves history at once, while the tree -- the rail
 * included -- keeps rendering the old route until that transition commits.
 * Inside that window the chat row still reads as "the page on screen", and its
 * click, which there means "come back to chat", was a sidebar pin toggle that
 * navigated nowhere: the address stayed on the other page and the persisted pin
 * preference flipped. The E2E round trip through Settings (`active-slot-
 * persistence.spec.ts`) hit this on a loaded runner; this pins the mechanism.
 *
 * A real `<BrowserRouter>`, because the subject is history moving ahead of the
 * rendered route, which a memory router cannot show through `window.location`.
 * Both clicks run inside ONE `act()`: the first schedules the transition and
 * the second fires before it is flushed, which is the window under test, with
 * no update landing outside a barrier.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { BrowserRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import App from '../App'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'

// Same isolation as the other App nav tests: stub the routed pages and the api
// client so App mounts without real network. Only the two pages the round trip
// visits render anything, and only enough to tell them apart.
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SettingsPage', () => ({ default: () => <div data-testid="settings-page">SettingsPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../pages/CapabilitiesPage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 0, credits_covered: 0, credits_plan: 10000, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04' } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

/** The shell as main.tsx mounts it: guard provider outside, a real browser router inside. */
function renderDashboard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <ThemeProvider>
          <NavigationLeaveGuardProvider>
            <BrowserRouter><App /></BrowserRouter>
          </NavigationLeaveGuardProvider>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const navRow = (name: RegExp) => screen.getByRole('button', { name })
const chatOnScreen = () => waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())

describe('rail chat row while a navigation is pending', () => {
  const pinToggles = vi.fn()
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    window.history.replaceState(null, '', '/chat')
    pinToggles.mockClear()
    window.addEventListener('toggle-pin-chat-sidebar', pinToggles)
  })
  afterEach(() => {
    window.removeEventListener('toggle-pin-chat-sidebar', pinToggles)
    vi.restoreAllMocks()
    cleanup()
  })

  it('toggles the sessions sidebar when chat is the page on screen AND the address', async () => {
    renderDashboard()
    await chatOnScreen()
    fireEvent.click(navRow(/^Sessions$/))
    expect(pinToggles).toHaveBeenCalledTimes(1)
    expect(window.location.pathname).toBe('/chat')
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
  })

  it('navigates back to chat when the address has already left it', async () => {
    renderDashboard()
    await chatOnScreen()
    const settings = navRow(/^Settings$/)
    const sessions = navRow(/^Sessions$/)
    act(() => {
      settings.click()
      // History has moved; the rail has not re-rendered yet. This is the
      // window a second click lands in on a loaded machine.
      expect(window.location.pathname).toBe('/settings')
      expect(screen.getByTestId('chat-page')).toBeInTheDocument()
      sessions.click()
    })
    // The click meant "come back". The address says so, the pin preference is
    // untouched, and the chat page is what ends up on screen.
    expect(pinToggles).not.toHaveBeenCalled()
    expect(window.location.pathname).toBe('/chat')
    await chatOnScreen()
    expect(screen.queryByTestId('settings-page')).toBeNull()
  })
})
