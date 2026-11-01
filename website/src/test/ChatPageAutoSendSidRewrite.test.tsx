/**
 * An in-window hand-off that names a session and asks for a send.
 *
 * The Command Bar and the milestone starters stage the prompt, switch to the
 * session, then navigate to `/chat?autoSend=1`. The router commits that
 * navigation in a transition, so a mounted ChatPage sees the staged text a
 * render before the query, and its URL sync then rewrites the address to
 * `?sid=` without `autoSend`. The prompt must still be sent exactly once, in the
 * named session, whether the steps land in one tick or are split across an
 * await.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { store as appStore } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation, useNavigate, type NavigateFunction } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { stagePendingSend, switchSlot } from '../store/chatSlice'
import dashboardReducer, { sseConnected, sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { resetSendRaceForTests } from '../api/sendRace'
import { resetSendRevisionSeedForTests } from '../hooks/sharedCrew/sendRevisionSeed'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const sendChat = vi.fn()
vi.mock('../api/client', async () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: (...a: unknown[]) => sendChat(...a),
    sharedCrewSendState: vi.fn().mockResolvedValue(null),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    knowledgeSources: vi.fn().mockResolvedValue({ sources: [] }),
    createChatSlot: vi.fn(),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

const slot = (key: string) => ({ key, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })

function makeStore({ connected = true }: { connected?: boolean } = {}) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected, slotsLoaded: true,
        slots: [slot('slot-a'), slot('slot-b')],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: '',
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

const nav: { navigate: NavigateFunction | null; search: string } = { navigate: null, search: '' }
function NavProbe() {
  nav.navigate = useNavigate()
  nav.search = useLocation().search
  return null
}

async function renderPage(store: ReturnType<typeof makeStore>) {
  vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={['/chat?sid=slot-a']}><NavProbe /><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

const OK = { ok: true, status: 200, json: () => Promise.resolve({ ok: true }) }

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  sendChat.mockReset()
  sendChat.mockResolvedValue(OK)
  resetSendRaceForTests()
  resetSendRevisionSeedForTests()
})
afterEach(() => vi.restoreAllMocks())

async function expectOneSendInSlotB(store: ReturnType<typeof makeStore>) {
  await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
  // Once the URL rewrite has committed, every render the hand-off causes has run.
  await waitFor(() => expect(nav.search).toContain('sid=slot-b'))
  expect(sendChat).toHaveBeenCalledTimes(1)
  expect(sendChat.mock.calls[0][0]).toBe('run the report')
  expect(sendChat.mock.calls[0][1]).toBe('slot-b')
  expect(nav.search).not.toContain('autoSend')
  expect(store.getState().chat.activeSlot).toBe('slot-b')
}

describe('an in-window auto-send hand-off to a named session', { timeout: 20_000 }, () => {
  it('sends once when stage, switch and navigate land in one tick', async () => {
    const store = makeStore()
    await renderPage(store)
    await act(async () => {
      store.dispatch(stagePendingSend('run the report'))
      void store.dispatch(switchSlot({ key: 'slot-b' }))
      nav.navigate!('/chat?autoSend=1')
    })
    await expectOneSendInSlotB(store)
  })

  it('sends once when the switch is awaited before stage and navigate', async () => {
    const store = makeStore()
    await renderPage(store)
    await act(async () => {
      await store.dispatch(switchSlot({ key: 'slot-b', keepTargetOnMissing: true })).unwrap().catch(() => {})
    })
    await act(async () => {
      store.dispatch(stagePendingSend('run the report'))
      nav.navigate!('/chat?autoSend=1')
    })
    await expectOneSendInSlotB(store)
  })

  it('sends once when stage and navigate are split across an await', async () => {
    const store = makeStore()
    await renderPage(store)
    await act(async () => {
      store.dispatch(stagePendingSend('run the report'))
      void store.dispatch(switchSlot({ key: 'slot-b' }))
      await Promise.resolve()
      await Promise.resolve()
      nav.navigate!('/chat?autoSend=1')
    })
    await expectOneSendInSlotB(store)
  })


  it('seeds the staged text instead of holding a send while disconnected', async () => {
    const store = makeStore({ connected: false })
    await renderPage(store)
    await act(async () => {
      store.dispatch(stagePendingSend('run the report'))
      void store.dispatch(switchSlot({ key: 'slot-b' }))
      nav.navigate!('/chat?autoSend=1')
    })
    await waitFor(() => expect(nav.search).toContain('sid=slot-b'))
    await waitFor(() => expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('run the report'))
    // Opening another session and reconnecting sends nothing on its own.
    await act(async () => { await store.dispatch(switchSlot({ key: 'slot-a' })) })
    await act(async () => {
      store.dispatch(sseConnected())
      store.dispatch(sseSlots([slot('slot-a'), slot('slot-b')]))
    })
    await waitFor(() => expect(store.getState().dashboard.connected).toBe(true))
    expect(sendChat).not.toHaveBeenCalled()
  })
})
