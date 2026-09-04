import { describe, it, expect, vi } from 'vitest'
import type { ReactNode } from 'react'
import { render } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* ChatPane hosts the same working indicator the full chat page shows (the
 * ghost-pose carousel in ChatFooter). Before this wiring, a running turn in a
 * pane — a member DM thread, a split pane — showed NOTHING between tool steps.
 * These tests pin the wiring, not ChatFooter's own visibility algebra (its own
 * suite covers that): the footer must exist exactly when the pane's per-slot
 * stream state says a turn is running and text is not actively arriving. */

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
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

const SLOT = 'chat-1-loader'

type Msg = { role: string; content: string; ts: string }

function makeStore(slotState: string, messages: Msg[], slotRunning?: boolean) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: messages.length, running: slotRunning ?? slotState !== 'idle', mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      // The pane's slot is the ACTIVE one, so selectSlotStreamState reads
      // chat.slotState — the same signal the header dot already uses.
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: SLOT,
        slotState,
        messages,
      } as unknown as RootState['chat'],
    } as Partial<RootState>,
  })
}

function mount(slotState: string, messages: Msg[], slotRunning?: boolean) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={makeStore(slotState, messages, slotRunning)}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ChatPane slotKey={SLOT} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

const USER_MSG: Msg = { role: 'user', content: 'hi', ts: '2026-09-01T00:00:00Z' }

describe('ChatPane working indicator (ChatFooter wiring)', () => {
  it('shows the loader carousel while a tool step runs', () => {
    const { queryByTestId } = mount('tool_running', [USER_MSG])
    expect(queryByTestId('chat-footer')).not.toBeNull()
    expect(queryByTestId('loader-carousel')).not.toBeNull()
  })

  it('shows the footer before the first stream frame (server says running, stream still idle)', () => {
    // The gap the stream-state alone cannot cover: right after a send (or a
    // Slack/cron-initiated turn) the slots broadcast flips slot.running before
    // any WS chat frame moves slotState off 'idle' — the same union the full
    // page gets from startLocalTurn + syncSlotRunningFromServer.
    const { queryByTestId } = mount('idle', [USER_MSG], true)
    expect(queryByTestId('chat-footer')).not.toBeNull()
  })

  it('renders no footer when the slot is idle', () => {
    const { queryByTestId } = mount('idle', [USER_MSG])
    expect(queryByTestId('chat-footer')).toBeNull()
  })

  it('yields to the inline caret while text is actively streaming', () => {
    // lastRole 'streaming' + state 'streaming' with a fresh tick: the caret owns
    // the signal, the footer stays hidden (until the stream goes quiet — that
    // takeover timing belongs to ChatFooter's own tests).
    const { queryByTestId } = mount('streaming', [
      USER_MSG,
      { role: 'streaming', content: 'partial an', ts: '2026-09-01T00:00:01Z' },
    ])
    expect(queryByTestId('chat-footer')).toBeNull()
  })
})
