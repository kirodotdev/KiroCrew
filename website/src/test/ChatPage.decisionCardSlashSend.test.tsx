/**
 * Regression test: a pending-decision card answer is an ANSWER PAYLOAD, not a
 * typed command — an option that textually looks like a client slash command
 * ("/side …", "/btw …", "/onboarding") must reach the turn transport, not
 * open Side Chat and strand the unanswered card.
 *
 * Why this is pinned at the ChatPage layer: the carve-out lives on exactly one
 * line — send()'s slash branch gates on `!optionText` — and neither half is
 * visible to unit tests. PendingDecisionCard only calls its onSendDirect prop,
 * and interceptSlashCommand's own tests pass whether or not send() consults
 * optionText first. Drop the guard in a refactor and every other test stays
 * green while card answers shaped like commands silently divert.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))

const { mockSendTurn, mockSideOpen, mockSideTurn } = vi.hoisted(() => ({
  mockSendTurn: vi.fn().mockResolvedValue({ status: 'dispatched', body: {} }),
  mockSideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }),
  mockSideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
}))

vi.mock('../chat-core/transport/sendTurn', () => ({ sendTurn: mockSendTurn }))

vi.mock('../api/client', () => ({
  api: {
    sideOpen: mockSideOpen,
    sideTurn: mockSideTurn,
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    chatFolders: vi.fn().mockResolvedValue([]),
    chatTags: vi.fn().mockResolvedValue([]),
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

const DECISION = {
  options: ['/side investigate deeper', 'Stop'],
  excerpt: 'How should I proceed?',
  ts: '2026-09-11T10:00:00+00:00',
}

function makeStore(over: { needs_input?: boolean } = {}) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        connected: true,
        slots: [{
          key: 'slot-a', messages: 2, running: false, mode: '',
          pending_approval: false, waiting_for_input: false,
          last_activity_ts: undefined, pending_decision: DECISION,
          ...over,
        }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
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

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.clearAllMocks()
})

describe('ChatPage — decision-card answers bypass slash interception', { timeout: 15_000 }, () => {
  const renderPage = async (storeOver: { needs_input?: boolean } = {}) => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await act(async () => {
      render(
        <QueryClientProvider client={qc}>
          <Provider store={makeStore(storeOver)}>
            <ThemeProvider>
              <MemoryRouter><ChatPage /></MemoryRouter>
            </ThemeProvider>
          </Provider>
        </QueryClientProvider>,
      )
    })
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
  }

  it('sends a "/side …" option as turn content instead of opening Side Chat', async () => {
    await renderPage()
    await waitFor(() => expect(screen.getByTestId('pending-decision-card')).toBeTruthy())

    fireEvent.click(screen.getByText('/side investigate deeper'))
    await act(async () => {
      fireEvent.click(screen.getByTestId('pending-decision-send'))
    })

    // The answer travels the turn transport verbatim…
    await waitFor(() => expect(mockSendTurn).toHaveBeenCalled())
    const call = mockSendTurn.mock.calls[0][0] as { text?: string; content?: string }
    expect(JSON.stringify(call)).toContain('/side investigate deeper')
    // …and the slash command machinery never fires.
    expect(mockSideOpen).not.toHaveBeenCalled()
    expect(mockSideTurn).not.toHaveBeenCalled()
  })

  it('does not mount while the slot owes a question (needs_input), even before the questions map hydrates', async () => {
    // needs_input rides the SAME slot payload as pending_decision, so it is
    // the race-free precedence signal: the pendingQuestions map fills over an
    // async fetch, and during that window a mounted decision card could be
    // answered — appending a user row that retires the still-unhydrated
    // stateless question unanswered. The gate must hold on slot state alone.
    await renderPage({ needs_input: true })
    expect(screen.queryByTestId('pending-decision-card')).toBeNull()
  })
})
