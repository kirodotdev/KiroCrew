import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, act } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { selectComposerBusy } from '../store/chatSlice'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* The gateway can prepare an assistant message for a slot outside any turn
 * (the warm task brief's opener, then a background model call). The slots
 * frame says so with `preparing`, and the pane shows the SAME working
 * indicator a turn shows until a frame drops it. It is not a turn: the
 * composer stays idle so a send is never queued behind it. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Kiro Crew', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const SLOT = 'member-radar'
// An earlier, ordinary row: the catch-up has no separate opener any more.
const OPENER = { role: 'assistant', content: 'The fix is in review.', cls: 'msg msg-a', ts: '2026-10-21T10:00:00Z' }

const row = (preparing: boolean) => ({ key: SLOT, messages: 1, running: false, preparing, mode: 'member', pending_approval: false, waiting_for_input: false })

function makeStore(preparing: boolean) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [row(preparing)],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(store: ReturnType<typeof makeStore>, crewmate: boolean) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} crewmate={crewmate ? { name: 'Radar' } : undefined} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
    messages: [OPENER], running: false, has_more: false, total: 1,
  })
})

describe('a message prepared outside a turn', () => {
  it("shows the mate's typing bubble while preparing, and drops it with the frame that clears it", async () => {
    const store = makeStore(true)
    const view = renderPane(store, true)
    const line = await view.findByTestId('crewmate-live-activity')
    expect(line.dataset.activity).toBe('working')
    // The caption names what is coming; the finished message replaces it.
    expect(line).toHaveTextContent('Preparing your catch-up…')
    expect(view.getByTestId('crewmate-typing-bubble').dataset.shown).toBe('true')
    // Not a turn: a send now goes straight through, never queued behind it.
    expect(selectComposerBusy(store.getState(), SLOT)).toBe(false)

    act(() => { store.dispatch(sseSlots([row(false)] as Parameters<typeof sseSlots>[0])) })
    expect(view.queryByTestId('crewmate-live-activity')).toBeNull()
  })

  it('an ordinary pane shows the chat footer while preparing', async () => {
    const store = makeStore(true)
    const view = renderPane(store, false)
    expect(await view.findByTestId('chat-footer')).not.toBeNull()
    expect(view.getByTestId('chat-pane-preparing-catch-up')).toHaveTextContent('Preparing your catch-up…')
    act(() => { store.dispatch(sseSlots([row(false)] as Parameters<typeof sseSlots>[0])) })
    expect(view.queryByTestId('chat-footer')).toBeNull()
    expect(view.queryByTestId('chat-pane-preparing-catch-up')).toBeNull()
  })

  it('a pane opened on a slot that is not preparing shows no indicator', async () => {
    // A reload or reconnect reads the current frame: no stuck spinner from
    // work that already ended.
    const view = renderPane(makeStore(false), true)
    await view.findByText(OPENER.content)
    expect(view.queryByTestId('crewmate-live-activity')).toBeNull()
  })
})
