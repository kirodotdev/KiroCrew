/**
 * Regression test: the Globe / Browse toggle must be per-session
 * (keyed by slot), not page-global.
 *
 * Browse mode is keyed by slot. ChatPage never remounts on slot switch (only
 * `activeSlot` changes), so a single page-global boolean would bleed across
 * every session: enabling Browse in session A would leave it on when switching
 * to session B. Keying by slot gives each session its own toggle, and new
 * sessions default to off.
 *
 * Browse mode is a per-session Toggle (role="switch", aria-checked) inside the
 * ChatInput "+" drop-up menu ("Let the agent use the browser"), which is what this test observes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode }) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
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

const MSG = [{ role: 'assistant', content: 'hi', cls: '' }]

function makeStore(activeSlot: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [
          { key: 'slot-a', messages: 1, running: false, stopping: false, stop_state: 'idle', mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
          { key: 'slot-b', messages: 1, running: false, stopping: false, stop_state: 'idle', mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
        ],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: MSG,
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
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

async function renderChat(activeSlot: string) {
  const store = makeStore(activeSlot)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
  return store
}

/** Browse mode lives in the ChatInput "+" drop-up as a Toggle ("Let the agent use the browser").
 *  Open the menu once per test; it stays open across slot switches (ChatInput
 *  doesn't remount), so the switch keeps reflecting the active slot's state. */
const openMenu = () => fireEvent.click(screen.getByTitle('Add files & options'))
const browseToggle = () => screen.getByRole('switch', { name: 'Let the agent use the browser' })
const isOn = () => browseToggle().getAttribute('aria-checked') === 'true'

async function switchSlot(store: ReturnType<typeof makeStore>, slot: string) {
  await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: slot }) })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
})

describe('ChatPage — per-session Browse toggle', { timeout: 15_000 }, () => {
  it('defaults to off and toggles on for the active session', async () => {
    await renderChat('slot-a')
    await act(async () => { openMenu() })
    expect(isOn()).toBe(false)
    await act(async () => { fireEvent.click(browseToggle()) })
    expect(isOn()).toBe(true)
  })

  it('does not bleed an enabled toggle into another session', async () => {
    const store = await renderChat('slot-a')
    await act(async () => { openMenu() })
    // Enable browse in slot-a.
    await act(async () => { fireEvent.click(browseToggle()) })
    expect(isOn()).toBe(true)
    // Switch to slot-b — it must be independent (off). Menu stays open.
    await switchSlot(store, 'slot-b')
    expect(isOn()).toBe(false)
  })

  it('preserves each session\'s toggle when switching back', async () => {
    const store = await renderChat('slot-a')
    await act(async () => { openMenu() })
    // slot-a ON.
    await act(async () => { fireEvent.click(browseToggle()) })
    expect(isOn()).toBe(true)
    // slot-b stays OFF, then turn it ON independently.
    await switchSlot(store, 'slot-b')
    expect(isOn()).toBe(false)
    await act(async () => { fireEvent.click(browseToggle()) })
    expect(isOn()).toBe(true)
    // Back to slot-a — still ON (its own state preserved).
    await switchSlot(store, 'slot-a')
    expect(isOn()).toBe(true)
    // Back to slot-b — still ON.
    await switchSlot(store, 'slot-b')
    expect(isOn()).toBe(true)
  })
})
