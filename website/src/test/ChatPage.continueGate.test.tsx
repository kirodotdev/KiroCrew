/**
 * Regression test: the composer's Continue (▶) button appears ONLY when the
 * transcript shows the last turn was interrupted — never on a slot that simply
 * holds a conversation.
 *
 * Why this is pinned at the ChatPage layer: `ChatInput` is prop-driven and
 * cannot tell the two situations apart, and `selectContinuable` /
 * `selectTurnInterrupted` are deliberately independent (availability vs copy).
 * The composition of the two happens exactly once, where ChatPage passes
 * `continuable={continuable && interrupted}` — so unit tests of either half
 * pass whether or not the gate exists. Without this file the gate can be
 * dropped in a refactor and every other test stays green.
 *
 * The behaviour being defended: an accent-filled circular button in the send
 * slot reads as "this is your next move". On a chat that finished cleanly there
 * is no next move, so the button advertised pending work that did not exist and
 * the only thing separating it from Send was a hover tooltip — invisible on
 * first read and absent on touch. Idle-with-a-conversation is the overwhelmingly
 * common state, so the old gate showed it almost always.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
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

/**
 * ChatPage refetches the active slot on mount and the response REPLACES
 * `chat.messages`, so a static empty-transcript mock would silently blank the
 * preloaded state and make `selectContinuable` false — every assertion below
 * would then pass for the wrong reason (no Continue because no conversation,
 * rather than because nothing was interrupted). The holder keeps the fetch and
 * the preloaded store telling the same story.
 */
const detail = vi.hoisted(() => ({ messages: [] as { role: string; content: string; cls?: string; kind?: string; meta?: Record<string, unknown> }[] }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn(async () => ({ messages: detail.messages, running: false, has_more: false, total: detail.messages.length })),
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

type Msg = { role: string; content: string; cls?: string; kind?: string; meta?: Record<string, unknown> }

function makeStore(messages: Msg[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', messages: messages.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages,
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

async function renderWith(messages: Msg[]) {
  detail.messages = messages
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={makeStore(messages)}>
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

describe('ChatPage — Continue appears only on an interrupted turn', { timeout: 15_000 }, () => {
  it('shows the ordinary Send button after a clean completion, not Continue', async () => {
    // The assistant answered and handed the floor back: the common resting
    // state of every chat the user returns to.
    await renderWith([
      { role: 'user', content: 'hi', cls: '' },
      { role: 'assistant', content: 'hello', cls: '' },
    ])

    expect(screen.queryByTestId('composer-continue')).toBeNull()
    // Send survives (disabled while the composer is empty) — the slot is not
    // left with a control that has no meaning.
    expect(screen.getByLabelText('Send')).toBeTruthy()
  })

  it('offers Continue when nothing came back at all (user row last)', async () => {
    // A gateway restart mid-turn leaves exactly this shape.
    await renderWith([{ role: 'user', content: 'do the thing', cls: '' }])

    expect(screen.getByTestId('composer-continue')).toBeTruthy()
  })

  it('offers Continue when the turn streamed partway then died (error trails the reply)', async () => {
    await renderWith([
      { role: 'user', content: 'do the thing', cls: '' },
      { role: 'assistant', content: 'starting on it', cls: '' },
      { role: 'error', content: 'connection lost', cls: '' },
    ])

    expect(screen.getByTestId('composer-continue')).toBeTruthy()
  })

  it('does not resurrect Continue for a superseded error (a later turn completed cleanly)', async () => {
    // `[user, error, user, assistant]` — the failure is history, the newest turn
    // finished. Availability alone would still light the composer here.
    await renderWith([
      { role: 'user', content: 'first ask', cls: '' },
      { role: 'error', content: 'connection lost', cls: '' },
      { role: 'user', content: 'second ask', cls: '' },
      { role: 'assistant', content: 'done', cls: '' },
    ])

    expect(screen.queryByTestId('composer-continue')).toBeNull()
    expect(screen.getByLabelText('Send')).toBeTruthy()
  })

  // ---- Pressing Stop is an ending, not an interruption -------------------
  // The pair below is the point: BOTH are the user pressing Stop, and they must
  // agree. They differ only in whether a reply segment had flushed first, which
  // is invisible timing the user cannot predict — before this was pinned, the
  // early-stop shape offered Resume and the late-stop shape did not.

  it('offers no Resume after a stop that landed before any reply text', async () => {
    // `[user, stop_event]` — tail-identical to a crash before first output.
    await renderWith([
      { role: 'user', content: 'do the thing', cls: '' },
      { role: 'system', content: 'Stopped', cls: '', meta: { kind: 'stop_event', state: 'stopped' } },
    ])

    expect(screen.queryByTestId('composer-continue')).toBeNull()
    expect(screen.getByLabelText('Send')).toBeTruthy()
  })

  it('offers no Resume after a stop that landed mid-reply', async () => {
    await renderWith([
      { role: 'user', content: 'do the thing', cls: '' },
      { role: 'assistant', content: 'starting on it', cls: '' },
      { role: 'system', content: 'Stopped', cls: '', meta: { kind: 'stop_event', state: 'stopped' } },
    ])

    expect(screen.queryByTestId('composer-continue')).toBeNull()
    expect(screen.getByLabelText('Send')).toBeTruthy()
  })

  it('recognises a stop card carrying only the top-level kind field', async () => {
    // The websocket path sets both `kind` and `meta.kind`; a disk-rehydrated row
    // can arrive with just one. Checking half the predicate would regress the
    // fix for exactly one of the two delivery paths.
    await renderWith([
      { role: 'user', content: 'do the thing', cls: '' },
      { role: 'system', content: 'Stopped', cls: '', kind: 'stop_event' },
    ])

    expect(screen.queryByTestId('composer-continue')).toBeNull()
  })

  it('still offers Resume for a real failure that follows an older stop', async () => {
    // An older stop card must not suppress a genuine later interruption.
    await renderWith([
      { role: 'user', content: 'first ask', cls: '' },
      { role: 'system', content: 'Stopped', cls: '', meta: { kind: 'stop_event', state: 'stopped' } },
      { role: 'user', content: 'second ask', cls: '' },
    ])

    expect(screen.getByTestId('composer-continue')).toBeTruthy()
  })
})
