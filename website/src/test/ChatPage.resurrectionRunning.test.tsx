/**
 * Regression test for the session-resurrection thinking-indicator wiring.
 *
 * Behaviour guarded: after sending the first message to a resurrected (history)
 * session, the agent processes the turn and the chat-area "thinking" footer
 * must appear (not only the sidebar).
 *
 * The hazard: send() optimistically sets slotRunning=true, but the slots-sync
 * useEffect in ChatPage must not mirror dashboard slots[].running
 * unconditionally. A WS 'slots' broadcast that predates the send (server hasn't
 * started the agent yet, so running=false) would otherwise clobber the
 * optimistic true and hide ChatFooter.
 *
 * So send() dispatches startLocalTurn(slot) (records pendingTurnSlot) and the
 * effect dispatches syncSlotRunningFromServer, which ignores running=false while
 * a turn is pending confirmation for the active slot.
 *
 * These tests pin the *wiring*: reverting the effect to setSlotRunning(s.running)
 * makes them fail even though the reducer-level unit tests in chatSlice.test.ts
 * still pass.
 */
import type { ReactNode } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer, { startLocalTurn } from '../store/chatSlice'
import dashboardReducer, { updateSlot } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (i: number, d: unknown) => ReactNode }) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'user', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
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

/** Seed an idle, settled resurrected slot. The optimistic turn is simulated
 *  AFTER mount (via startLocalTurn) so the mount-time switchSlot fetch has
 *  already run, matching the real flow where the user sends after the page
 *  has settled. The dashboard slots snapshot reports running=false throughout
 *  (the server hasn't started the agent yet). */
function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', messages: 1, running: false, stopping: false, stop_state: 'idle', mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as ReturnType<typeof dashboardReducer>,
      chat: {
        activeSlot: 'slot-a', messages: [{ role: 'user', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle', pendingTurnSlot: null,
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'files',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as ReturnType<typeof chatReducer>,
      notifications: { items: [] } as unknown as ReturnType<typeof notificationsReducer>,
    },
  })
}

async function renderWithStore(store: ReturnType<typeof makeStore>) {
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
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
})

describe('ChatPage — session-resurrection thinking indicator', { timeout: 15_000 }, () => {
  it('a stale slots snapshot (running=false) does not clobber the optimistic turn', async () => {
    const store = makeStore()
    await renderWithStore(store)
    // Let the mount-time switchSlot fetch settle (it clears any guard + syncs
    // running from the server detail, which is running=false → idle).
    await waitFor(() => expect(store.getState().chat.pendingTurnSlot).toBeNull())
    // Idle: ChatFooter renders nothing (returns null when not running).
    expect(screen.queryByTestId('chat-footer')).toBeNull()

    // User sends the first message: send() optimistically starts the turn.
    await act(async () => { store.dispatch(startLocalTurn('slot-a')) })
    expect(store.getState().chat.slotRunning).toBe(true)
    // Thinking indicator now visible.
    await waitFor(() => expect(screen.queryByTestId('chat-footer')).not.toBeNull())

    // A WS 'slots' broadcast that predates the send arrives (running still
    // false — agent not started). Force a slots-array change to fire the
    // slots-sync effect while running stays false.
    await act(async () => { store.dispatch(updateSlot({ key: 'slot-a', messages: 2 })) })

    // Guard holds: footer stays. Pre-fix the indicator vanished here (the bug).
    expect(store.getState().chat.slotRunning).toBe(true)
    expect(store.getState().chat.pendingTurnSlot).toBe('slot-a')
    expect(screen.queryByTestId('chat-footer')).not.toBeNull()
  })

  it('once the server confirms running=true, a later running=false is honoured', async () => {
    const store = makeStore()
    await renderWithStore(store)
    await waitFor(() => expect(store.getState().chat.pendingTurnSlot).toBeNull())

    await act(async () => { store.dispatch(startLocalTurn('slot-a')) })
    expect(store.getState().chat.slotRunning).toBe(true)

    // Server starts the agent — slots broadcast reports running=true.
    await act(async () => { store.dispatch(updateSlot({ key: 'slot-a', running: true })) })
    expect(store.getState().chat.slotRunning).toBe(true)
    expect(store.getState().chat.pendingTurnSlot).toBeNull()  // guard cleared
    expect(screen.queryByTestId('chat-footer')).not.toBeNull()

    // Turn ends — slots broadcast reports running=false. Now honoured and the
    // indicator disappears.
    await act(async () => { store.dispatch(updateSlot({ key: 'slot-a', running: false })) })
    expect(store.getState().chat.slotRunning).toBe(false)
    await waitFor(() => expect(screen.queryByTestId('chat-footer')).toBeNull())
  })
})
