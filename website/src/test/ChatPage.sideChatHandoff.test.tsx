import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot, stageToMainComposer } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { DRAFTS_KEY } from '../utils/chatDrafts'

/* Side Chat → main composer hand-off, as ChatPage's single-session composer
 * consumes it (`chat.mainComposerAppend`). The hand-off APPENDS to the slot it
 * names and never to any other slot's text — including in the one commit where
 * the active slot is switching and the composer still belongs to the slot the
 * user is leaving. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
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
    createChatSlot: vi.fn().mockResolvedValue({ key: 'new-slot', title: 'new-slot', messages: 0, running: false }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
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

function makeStore(activeSlot: string, slots: { key: string }[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slots: slots.map(s => ({ key: s.key, messages: 1, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null, mainComposerAppend: null,
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

async function renderPage(store: ReturnType<typeof makeStore>) {
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

const input = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
const savedDrafts = () => JSON.parse(localStorage.getItem(DRAFTS_KEY) ?? '{}') as Record<string, string>

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ChatPage Side Chat hand-off consumer', { timeout: 15_000 }, () => {
  it('appends the answer to the active slot\'s composer draft and clears the staged hand-off', async () => {
    localStorage.setItem(DRAFTS_KEY, JSON.stringify({ 'slot-a': 'half-typed question' }))
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderPage(store)
    await waitFor(() => expect(input().value).toBe('half-typed question'))

    act(() => { store.dispatch(stageToMainComposer({ slot: 'slot-a', text: 'Use the narrow fix.' })) })

    await waitFor(() => expect(input().value).toBe('half-typed question\n\nUse the narrow fix.'))
    expect(store.getState().chat.mainComposerAppend).toBeNull()
    // The seeded-composer hint lifts the box so the appended answer is readable.
    expect(screen.getByText(/Prompt pre-filled/)).toBeInTheDocument()
    await waitFor(() => expect(savedDrafts()['slot-a']).toBe('half-typed question\n\nUse the narrow fix.'))
  })

  it('a hand-off landing in the same commit as a switch into its slot merges into THAT slot\'s stored draft, not the outgoing composer text', async () => {
    // The composer still belongs to slot-a (composerSlotRef trails the switch
    // by two effects) while `activeSlot` already reads slot-b. Reading the live
    // composer here would write slot-a's unsent text under slot-b and the
    // slot-change restore would then show it, erasing slot-b's own draft.
    localStorage.setItem(DRAFTS_KEY, JSON.stringify({ 'slot-b': 'B was here' }))
    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderPage(store)
    fireEvent.change(input(), { target: { value: 'A unsent text' } })
    await waitFor(() => expect(savedDrafts()['slot-a']).toBe('A unsent text'))

    act(() => {
      store.dispatch(setActiveSlot('slot-b'))
      store.dispatch(stageToMainComposer({ slot: 'slot-b', text: 'Answer for B.' }))
    })

    expect(input().value).toBe('B was here\n\nAnswer for B.')
    expect(store.getState().chat.mainComposerAppend).toBeNull()
    expect(savedDrafts()['slot-a']).toBe('A unsent text')
    await waitFor(() => expect(savedDrafts()['slot-b']).toBe('B was here\n\nAnswer for B.'))

    // Slot-a comes back exactly as it was left.
    act(() => { store.dispatch(setActiveSlot('slot-a')) })
    expect(input().value).toBe('A unsent text')
  })
})
