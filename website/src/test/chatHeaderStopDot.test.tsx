import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, act } from '@testing-library/react'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* #20397: while a stop is under way the chat header status dot must NOT stay
 * green (the working colour). It turns danger, so the dot agrees with the
 * "Stopping…"/"Killing…" caption the footer and composer already show. Both
 * header-dot surfaces — the shared ChatPane header (split view and Crew Member
 * DM panes) and the app-sdk ChatEmbed — follow the one rule. */

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
    stopChatSlot: vi.fn().mockResolvedValue({ ok: true }),
    stopChatSlotForce: vi.fn().mockResolvedValue({ ok: true }),
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
import { ThemeProvider } from '../hooks/useTheme'

const SLOT = 'member-default'
const OTHER = 'chat-1-main'
const USER_MSG = { role: 'user', content: 'hi', ts: '2026-09-01T00:00:00Z' }

function slotRow(over: Partial<ChatSlot> = {}): ChatSlot {
  return { key: SLOT, messages: 1, running: true, mode: 'member', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...over } as unknown as ChatSlot
}

function makeStore(runState: string, slot: Partial<ChatSlot> = {}) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [slotRow(slot)],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: OTHER,
        slotMessages: { [SLOT]: [USER_MSG] },
        slotRun: { [SLOT]: { state: runState } },
      } as unknown as RootState['chat'],
    } as Partial<RootState>,
  })
}

function mountPane(store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  let utils!: ReturnType<typeof render>
  // Wrapped in act + a flush: ChatPane's mount effects (query, slot hydrate)
  // land state after the first paint, so the dot is read only once they settle.
  act(() => {
    utils = render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
          <MemoryRouter>
            {/* NOT frameless: the header row (and its status dot) renders. */}
            <ChatPane slotKey={SLOT} agentLocked />
          </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
  })
  return utils
}

describe('ChatPane header status dot — #20397', () => {
  it('is green (bg-ok) while the turn works normally', async () => {
    const { getByTestId, findByTestId } = mountPane(makeStore('streaming'))
    await findByTestId('chat-pane-status-dot')
    const dot = getByTestId('chat-pane-status-dot')
    expect(dot).toHaveClass('bg-ok')
    expect(dot).not.toHaveClass('bg-danger')
  })

  it('turns danger (not green) while the stream state is stopping', async () => {
    const { getByTestId, findByTestId } = mountPane(makeStore('stopping'))
    await findByTestId('chat-pane-status-dot')
    const dot = getByTestId('chat-pane-status-dot')
    expect(dot).toHaveClass('bg-danger')
    expect(dot).not.toHaveClass('bg-ok')
  })

  it('turns danger while the slots frame reports stopping, even mid-tool', async () => {
    const { getByTestId, findByTestId } = mountPane(makeStore('tool_running', { stop_state: 'soft_pending', stopping: true }))
    await findByTestId('chat-pane-status-dot')
    const dot = getByTestId('chat-pane-status-dot')
    expect(dot).toHaveClass('bg-danger')
    expect(dot).not.toHaveClass('bg-ok')
  })

  it('the header caption agrees with the dot: run-state while working, "Stopping…" while stopping', async () => {
    // The caption sits right beside the dot; it must not stay green with a
    // run-state token while the dot is red (the UX-review contradiction).
    const working = mountPane(makeStore('tool_running'))
    await working.findByTestId('chat-pane-status-caption')
    const workingCaption = working.getByTestId('chat-pane-status-caption')
    expect(workingCaption).toHaveClass('text-ok')
    expect(workingCaption.textContent).not.toMatch(/Stopping/)
    working.unmount()

    const stopping = mountPane(makeStore('tool_running', { stop_state: 'soft_pending', stopping: true }))
    await stopping.findByTestId('chat-pane-status-caption')
    const stoppingCaption = stopping.getByTestId('chat-pane-status-caption')
    expect(stoppingCaption).toHaveClass('text-danger')
    expect(stoppingCaption).not.toHaveClass('text-ok')
    expect(stoppingCaption.textContent).toMatch(/Stopping/)
    stopping.unmount()

    // Force-kill phase: the caption switches to "Killing…" in danger, in step
    // with the footer and the ChatEmbed caption.
    const killing = mountPane(makeStore('tool_running', { stop_state: 'killing', stopping: true }))
    await killing.findByTestId('chat-pane-status-caption')
    const killingCaption = killing.getByTestId('chat-pane-status-caption')
    expect(killingCaption).toHaveClass('text-danger')
    expect(killingCaption.textContent).toMatch(/Force stopping/)
  })

  it('an idle pane with a stale slots-frame stopping flag shows no red dot and no caption', async () => {
    // DESIGN edge case: `stopUnderWay` is gated on `running`, so a leftover
    // `stopping: true` on an idle pane cannot paint a red pulsing dot beside
    // an absent (running-gated) caption.
    const { getByTestId, findByTestId, queryByTestId } = mountPane(makeStore('idle', { running: false, stopping: true }))
    await findByTestId('chat-pane-status-dot')
    const dot = getByTestId('chat-pane-status-dot')
    expect(dot).not.toHaveClass('bg-danger')
    // Idle: the caption (running-gated) is not rendered at all.
    expect(queryByTestId('chat-pane-status-caption')).toBeNull()
  })
})

// ChatEmbed is driven by a poll, not the store, so it gets its own harness.
const mockGet = vi.fn()
const mockPost = vi.fn()
vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))
vi.mock('../app-sdk/ChatMessageList', async () => {
  const React = await import('react')
  return {
    default: React.forwardRef(function MockList(
      { transcript }: { transcript?: { belowRows?: ReactNode } },
      _ref: unknown,
    ) {
      return <div data-testid="chat-message-list">{transcript?.belowRows}</div>
    }),
  }
})

import ChatEmbed from '../app-sdk/ChatEmbed'

describe('ChatEmbed header status dot — #20397', () => {
  let queryClient: QueryClient
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers()
    mockPost.mockResolvedValue({ ok: true })
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })
  afterEach(() => { vi.useRealTimers() })

  async function mountEmbed() {
    let utils!: ReturnType<typeof render>
    await act(async () => {
      utils = render(
        <QueryClientProvider client={queryClient}>
          <ChatEmbed slotKey="slot-1" />
        </QueryClientProvider>,
      )
    })
    await act(async () => { vi.advanceTimersByTime(100) })
    return utils
  }

  it('is green (bg-ok) while the turn works normally', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '' })
    const { getByTestId } = await mountEmbed()
    const dot = getByTestId('chat-embed-status-dot')
    expect(dot).toHaveClass('bg-ok')
    expect(dot).not.toHaveClass('bg-danger')
  })

  it('turns danger (not green) while a stop is under way', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stopping: true, stop_state: 'soft_pending' })
    const { getByTestId } = await mountEmbed()
    const dot = getByTestId('chat-embed-status-dot')
    expect(dot).toHaveClass('bg-danger')
    expect(dot).not.toHaveClass('bg-ok')
  })
})
