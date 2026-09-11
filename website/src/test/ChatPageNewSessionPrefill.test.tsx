/**
 * `/chat?new=1&prefill=<text>` — the cold-URL deep link an external launcher (a
 * Slack card, a browser bookmarklet, a CLI `--open`) can build to hand the
 * dashboard a pre-drafted prompt.
 *
 * Before this, `?new=1` created a blank session and `?prefill=` was honoured only
 * alongside an existing `?sid=`, so a launcher had no way in: the only cold path
 * that could both create a session AND carry a prompt was the signed `?token=`
 * channel flow, which an external tool cannot mint.
 *
 * Two properties are load-bearing and both are pinned here.
 *  1. It SEEDS ONLY. Nothing is sent — the human still presses Enter — so the
 *     link adds no way to spend a model turn.
 *  2. It requires the explicit `new=1` marker. A bare `?prefill=<v>` is an in-app
 *     SENTINEL in this dashboard (the file explorer's "Chat about this file" uses
 *     `?prefill=1`, a project idea's "Edit in chat" uses `?prefill=plan`, both
 *     with the real text riding Redux `pendingInput`), so honouring one would
 *     spawn a spurious empty session and type the marker into it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, act, waitFor, screen } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { PREFILL_STORAGE_KEY } from '../utils/navIntent'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const createChatSlot = vi.fn()
const sendChat = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: (...a: unknown[]) => sendChat(...a),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    createChatSlot: (...a: unknown[]) => createChatSlot(...a),
    deleteChatSlot: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotContext: vi.fn().mockResolvedValue({ ok: true }),
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
// The composer's sketch dialog pulls in the excalidraw bundle, which this test
// never opens and which costs seconds to transform.
vi.mock('../components/SketchDialog', () => ({ default: () => null }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

const PROMPT = 'Draft the release note for the ACP retry backoff'
const NEW_SLOT = 'chat-new-1'

/** `activeSlot: null` with one known session — the state a cold `/chat?new=1`
 *  load lands in, and the one where a spurious create would be visible. */
function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [
          { key: 'chat-old', messages: 3, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
        ],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: null, messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false, followups: {},
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderAt(route: string) {
  const store = makeStore()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={[route]}><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  return store
}

const composer = () => screen.getByLabelText('Message input') as HTMLTextAreaElement

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  createChatSlot.mockReset()
  sendChat.mockReset()
  sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
  createChatSlot.mockResolvedValue({ key: NEW_SLOT, title: NEW_SLOT, messages: 0, running: false })
})

describe('ChatPage — /chat?new=1&prefill= seeds a fresh session', { timeout: 20_000 }, () => {
  it('creates the session and seeds its composer with the linked prompt', async () => {
    const store = await renderAt(`/chat?new=1&prefill=${encodeURIComponent(PROMPT)}`)

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
    await waitFor(() => expect(composer().value).toBe(PROMPT))
    // The staged value is consumed, not left behind for the next slot switch.
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
  })

  it('sends nothing — the prompt waits for a human Enter', async () => {
    await renderAt(`/chat?new=1&prefill=${encodeURIComponent(PROMPT)}`)
    await waitFor(() => expect(composer().value).toBe(PROMPT))
    expect(sendChat).not.toHaveBeenCalled()
  })

  it('still creates a plain blank session when ?prefill= is empty', async () => {
    const store = await renderAt('/chat?new=1&prefill=')

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
    expect(composer().value).toBe('')
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
  })

  it('ignores a bare ?prefill=1 with no new=1, because that is an in-app sentinel', async () => {
    // The file explorer's "Chat about this file" navigates to /chat?prefill=1 and
    // passes the real text through Redux `pendingInput`. Creating a session here
    // and typing "1" into it is the failure this guard exists for.
    await renderAt('/chat?prefill=1')

    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    expect(createChatSlot).not.toHaveBeenCalled()
    expect(composer().value).toBe('')
  })

  it('ignores a bare ?prefill=<text> with no new=1, so the launcher URL must say what it wants', async () => {
    await renderAt(`/chat?prefill=${encodeURIComponent(PROMPT)}`)

    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    expect(createChatSlot).not.toHaveBeenCalled()
    expect(composer().value).toBe('')
  })
})
