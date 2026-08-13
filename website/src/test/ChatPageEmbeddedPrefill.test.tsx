/**
 * Regression: artifact companion chat opens with an EMPTY composer the first
 * time a comment is sent to the agent (correct only on the second open).
 *
 * Root cause: unlike the follow-up worktree flow (which does
 * `writePrefill(slot)` then `await switchSlot(slot)` back-to-back while ChatPage
 * is already mounted), the artifact flow writes the prefill in
 * `createBoundSession` but activates the slot LATER and ELSEWHERE — in
 * `ArtifactChatPanel`'s `useEffect(() => dispatch(switchSlot(slotKey)))`. The
 * embedded `<ChatPage>` only mounts once the panel receives a non-null slotKey,
 * so ChatPage MOUNTS and the slot SWITCH happen in the same beat. The per-slot
 * draft-restore effect (which consumes PREFILL_STORAGE_KEY) is a child effect,
 * so it runs on mount BEFORE the panel's parent switch effect — at that point
 * `activeSlot` is still the previous slot, the prefill's slotKey doesn't match,
 * and the composer is seeded from the (empty) incoming draft instead.
 *
 * This harness reproduces exactly that timing with the REAL ChatPage.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { StrictMode, useEffect, useRef, useState } from 'react'
import { render, screen, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { switchSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { useAppDispatch } from '../store'
import { writePrefill } from '../utils/navIntent'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([
      { key: 'chat-1', messages: 1, running: false, mode: '', project: '/repo' },
      { key: 'chat-2', messages: 0, running: false, mode: '', project: '/repo' },
    ]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    createChatSlot: vi.fn().mockResolvedValue({ key: 'chat-2', title: 'chat-2', messages: 0, running: false }),
    deleteChatSlot: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotContext: vi.fn().mockResolvedValue({ ok: true }),
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

const PROMPT = 'Please review and address the 2 open comments on this artifact.'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [
          { key: 'chat-1', messages: 1, running: false, mode: '', project: '/repo', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
          { key: 'chat-2', messages: 0, running: false, mode: '', project: '/repo', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
        ],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'chat-1', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
        followups: {},
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

/** Mirror of ArtifactChatPanel: mounts the embedded ChatPage only once a bound
 *  slotKey exists, and activates that slot from a useEffect. */
function PanelHarness({ slotKey }: { slotKey: string | null }) {
  const dispatch = useAppDispatch()
  const prev = useRef<string | null>(null)
  useEffect(() => {
    if (slotKey && slotKey !== prev.current) {
      prev.current = slotKey
      dispatch(switchSlot(slotKey))
    }
  }, [slotKey, dispatch])
  if (!slotKey) return <div>no session</div>
  return <ChatPage embedded embedMode="chat" noUrlSync />
}

function Wrapper({ store }: { store: ReturnType<typeof makeStore> }) {
  const [slotKey, setSlotKey] = useState<string | null>(null)
  ;(globalThis as unknown as { __openCompanion: () => void }).__openCompanion = () => {
    // Mirror the FIXED createBoundSession ordering (ArtifactDetailPage.tsx):
    // writePrefill + switchSlot back-to-back BEFORE the bound slot surfaces to
    // the panel, so the embedded ChatPage mounts with the target already active
    // — the same contract as ChatPage's follow-up worktree handler. Without the
    // switchSlot here, this assertion fails under React.StrictMode (the slot is
    // never activated on first open → empty composer).
    writePrefill('chat-2', PROMPT)
    store.dispatch(switchSlot('chat-2'))
    setSlotKey('chat-2')
  }
  return <PanelHarness slotKey={slotKey} />
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.clearAllMocks()
})

describe('embedded ChatPage prefill on first activation (artifact companion)', () => {
  const composer = () => screen.getByLabelText('Message input') as HTMLTextAreaElement

  it('seeds the composer with the staged prompt the FIRST time the panel opens', async () => {
    const store = makeStore()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await act(async () => {
      render(
        <QueryClientProvider client={qc}>
          <Provider store={store}>
            <ThemeProvider>
              <StrictMode><MemoryRouter><Wrapper store={store} /></MemoryRouter></StrictMode>
            </ThemeProvider>
          </Provider>
        </QueryClientProvider>,
      )
    })
    // Open the companion panel (writes prefill + mounts ChatPage + switches slot).
    await act(async () => {
      ;(globalThis as unknown as { __openCompanion: () => void }).__openCompanion()
    })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2'))
    await waitFor(() => expect(composer().value).toBe(PROMPT))
    expect(sessionStorage.getItem('kirocrew_prefill')).toBeNull()
  })
})
