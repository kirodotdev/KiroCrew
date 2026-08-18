/**
 * Regression test: a server-refused regenerate or switch-variant press renders
 * the refusal above the composer instead of dying in console.warn.
 *
 * Why this is pinned at the ChatPage layer: the two presses live in different
 * shapes — `handleRegenerate` is a memoized callback, the switch-variant handler
 * is built inline inside the message renderer — but both must land their
 * rejection on the ONE shared refused-press surface. A unit test of either
 * handler passes whether or not the surface exists; only the mounted page
 * proves the refusal becomes pixels.
 *
 * The behaviour being defended: these endpoints re-check under the slot lock
 * and refuse for ordinary, user-actionable reasons (a turn already running, a
 * stop in progress, a pending approval, a readiness probe that timed out). The
 * old catch logged to the console, so the button flicked to disabled and
 * straight back with nothing on screen — the user's only theory was "the click
 * did nothing", and the next move was pressing it again.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer, { setSlotRunning } from '../store/chatSlice'
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
 * `chat.messages`, so the fetch and the preloaded store must tell the same
 * story or the transcript this test clicks on would be silently blanked.
 */
type Msg = { role: string; content: string; variants?: { content: string; ts?: string }[]; variant_idx?: number; meta?: Record<string, unknown> }
const detail = vi.hoisted(() => ({ messages: [] as Msg[] }))
const regenerateSlot = vi.hoisted(() => vi.fn())
const switchVariant = vi.hoisted(() => vi.fn())
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
    regenerateSlot: (...a: unknown[]) => regenerateSlot(...a),
    switchVariant: (...a: unknown[]) => switchVariant(...a),
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
  const store = makeStore(messages)
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
  return store
}

const plainTurn: Msg[] = [
  { role: 'user', content: 'hello' },
  { role: 'assistant', content: 'first answer' },
]

const variantTurn: Msg[] = [
  { role: 'user', content: 'hello' },
  {
    role: 'assistant', content: 'take two',
    variants: [{ content: 'take one' }, { content: 'take two' }],
    variant_idx: 1,
  },
]

beforeEach(() => {
  regenerateSlot.mockReset()
  switchVariant.mockReset()
})

describe('refused presses render the notice above the composer', () => {
  it('a refused regenerate shows the server reason with the per-action title', async () => {
    regenerateSlot.mockRejectedValue(new Error('A turn is already running'))
    await renderWith(plainTurn)

    fireEvent.click(screen.getByRole('button', { name: 'Regenerate response' }))

    const notice = await screen.findByTestId('refused-press-error')
    expect(notice.textContent).toContain("Couldn't regenerate")
    expect(notice.textContent).toContain('A turn is already running')
  })

  it('a refused switch-variant shows the server reason, and dismiss clears it', async () => {
    switchVariant.mockRejectedValue(new Error('stop in progress'))
    await renderWith(variantTurn)

    fireEvent.click(screen.getByRole('button', { name: 'Previous version' }))

    const notice = await screen.findByTestId('refused-press-error')
    expect(notice.textContent).toContain("Couldn't switch version")
    expect(notice.textContent).toContain('stop in progress')
    expect(switchVariant).toHaveBeenCalledWith('slot-a', 0)

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.queryByTestId('refused-press-error')).toBeNull())
  })

  it('a turn taking over retires the refusal', async () => {
    regenerateSlot.mockRejectedValue(new Error('pending approval'))
    const store = await renderWith(plainTurn)

    fireEvent.click(screen.getByRole('button', { name: 'Regenerate response' }))
    await screen.findByTestId('refused-press-error')

    // The turn starting is the success signal for whatever the slot was busy
    // with — the old reason would now describe a state that passed.
    await act(async () => { store.dispatch(setSlotRunning(true)) })
    await waitFor(() => expect(screen.queryByTestId('refused-press-error')).toBeNull())
  })
})
