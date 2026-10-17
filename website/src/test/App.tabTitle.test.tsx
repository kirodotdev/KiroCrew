/**
 * The main window's browser tab title names where you are, detail first, the
 * same way the popout windows do (`{{label}} — {{productName}}`): the session
 * title, then the panel, then the product, with the attention count kept in
 * front so it survives tab truncation.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { addSlotOptimistic, markSlotUnread } from '../store/dashboardSlice'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import App from '../App'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatSlot } from '../types'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SchedulePage', () => ({ default: () => <div data-testid="schedule-page">SchedulePage</div> }))
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
      instances: instancesReducer,
    },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[path]}>
            <App />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  return store
}

const slot = (key: string, title: string) => ({ key, title, agent: 'kirocrew' }) as unknown as ChatSlot

describe('main window tab title', () => {
  it('names the active session, then Chat, then the product', async () => {
    const store = renderAt('/chat')
    act(() => {
      store.dispatch(addSlotOptimistic(slot('s1', 'Fix the login page')))
      store.dispatch(setActiveSlot('s1'))
    })
    await waitFor(() => expect(document.title).toBe('Fix the login page — Chat — Kiro Crew'))
  })

  it('names Chat alone when no session is selected', async () => {
    renderAt('/chat')
    await waitFor(() => expect(document.title).toBe('Chat — Kiro Crew'))
  })

  it('names the panel on any other page', async () => {
    renderAt('/schedule')
    await waitFor(() => expect(document.title).toBe('Schedule — Kiro Crew'))
  })

  it('keeps the attention count first', async () => {
    const store = renderAt('/chat')
    act(() => {
      store.dispatch(addSlotOptimistic(slot('s1', 'Fix the login page')))
      store.dispatch(addSlotOptimistic(slot('s2', 'Other work')))
      store.dispatch(setActiveSlot('s1'))
      store.dispatch(markSlotUnread('s2'))
    })
    await waitFor(() => expect(document.title).toMatch(/^\(\d+\) Fix the login page — Chat — Kiro Crew$/))
  })
})
