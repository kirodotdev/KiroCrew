/**
 * The optimistic steer bubble must agree with the steer POST's receipt.
 *
 * `steer()` appends it with `meta.steer` on Enter, which draws the "Steered into
 * the running turn" badge. The mutation never read the answer, so two accepted
 * shapes kept that claim: `{ok, queued}`, where the text went to the queue and
 * is ALSO drawn as a queue card, and `{ok, slot, mid}`, where the POST raced
 * `chat_done` and started a new turn instead.
 *
 * Asserted on store state, not on the badge: the flag is the input the badge is
 * derived from, so reading it stays on the production dispatch path without
 * mounting the renderer.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))

const sendChat = vi.fn()
const steerChat = vi.fn()
const slotRow = () => ({
  key: 'slot-a', messages: 1, running: true, mode: '',
  pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
  subagents_running: false,
})
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockImplementation(() => Promise.resolve([slotRow()])),
    chatSlotDetail: vi.fn().mockImplementation(() => Promise.resolve({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: true, has_more: false, total: 1 })),
    sendChat: (...a: unknown[]) => sendChat(...a),
    steerChat: (...a: unknown[]) => steerChat(...a),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
    suggestions: vi.fn().mockResolvedValue({ suggestions: [] }),
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

const STEERED_TEXT = 'change course now'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [slotRow()],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: true, slotStopping: false, slotState: 'idle',
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

/** Drive the real path: mount, type mid-turn, press Enter (Steer is the default
 *  busy action), and wait for the mocked receipt to have been applied. */
async function steerWithReceipt(receipt: Record<string, unknown>) {
  steerChat.mockResolvedValue(receipt)
  const store = makeStore()
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
  const input = await waitFor(() => screen.getByLabelText('Message input') as HTMLTextAreaElement)
  fireEvent.change(input, { target: { value: STEERED_TEXT } })
  await act(async () => {
    fireEvent.keyDown(input, { key: 'Enter' })
    await Promise.resolve()
  })
  await waitFor(() => expect(steerChat).toHaveBeenCalled())
  // The receipt is applied in the mutation's onSuccess, one microtask past the
  // resolved promise, so settle the queue before reading the store.
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
  const rows = (store.getState().chat.messages as ChatMessage[]).filter(m => m.role === 'user' && m.content === STEERED_TEXT)
  return rows
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  sendChat.mockReset()
  sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
  steerChat.mockReset()
})

describe('optimistic steer bubble vs the steer receipt', { timeout: 20_000 }, () => {
  it('drops the bubble when the server queued the text instead of injecting it', async () => {
    const rows = await steerWithReceipt({ ok: true, queued: true })
    // Every arm answering `queued` has already broadcast a `queue_push`, so that
    // card is the server-owned representation and the bubble is a duplicate.
    expect(rows).toHaveLength(0)
  })

  it('demotes the bubble to a plain user message when the steer started a new turn', async () => {
    const rows = await steerWithReceipt({ ok: true, slot: 'slot-a', mid: 'm-1' })
    // The text ran, so the row stays — but it was not steered into anything.
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.steer).toBeFalsy()
  })

  it('leaves a genuine steer alone', async () => {
    const rows = await steerWithReceipt({ ok: true, steered: true })
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.steer).toBe(true)
  })
})
