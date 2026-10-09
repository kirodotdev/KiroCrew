import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, fireEvent } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* A "New conversation" discards the model's context and deliberately deletes
 * nothing: the transcript stays on disk. So the pane holds rows the model no
 * longer remembers, and drawing them as current context is the lie this
 * boundary exists to stop. The line comes from the member log's projection, not
 * from the pane, which is what makes it survive a reload -- the pane's job is
 * only to draw it and to let the reader past it. */

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

const SLOT = 'member-conductor'
/** The reset moment every case below is written against. */
const BOUNDARY = Date.parse('2026-10-03T16:00:00Z')

const DISCARDED = [
  { role: 'user', content: 'what the model forgot', cls: '', ts: '2026-10-03T15:00:00Z' },
  { role: 'assistant', content: 'the forgotten reply', cls: '', ts: '2026-10-03T15:00:05Z' },
]
const CURRENT = [
  { role: 'user', content: 'the fresh question', cls: '', ts: '2026-10-03T17:00:00Z' },
  { role: 'assistant', content: 'the fresh reply', cls: '', ts: '2026-10-03T17:00:05Z' },
]

function makeStore(messages: unknown[], chat: Partial<RootState['chat']> = {}) {
  const base = chatReducer(undefined, { type: '@@init' })
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      chat: { ...base, activeSlot: SLOT, messages, slotHasMore: false, slotOldestIndex: 0, slotCursorKey: SLOT, ...chat } as RootState['chat'],
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: messages.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(messages: unknown[], conversationStartTs?: number, chat: Partial<RootState['chat']> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={makeStore(messages, chat)}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} crewmate={{ name: 'Conductor' }} conversationStartTs={conversationStartTs} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => { vi.clearAllMocks() })

describe('the conversation boundary', () => {
  it('hides what the model forgot and keeps what it remembers', async () => {
    const view = renderPane([...DISCARDED, ...CURRENT], BOUNDARY)

    await view.findByText('the fresh reply')
    expect(view.queryByText('the forgotten reply')).toBeNull()
    expect(view.getByTestId('chat-pane-show-earlier')).toBeTruthy()
  })

  it('draws the earlier rows once the reader asks for them', async () => {
    const view = renderPane([...DISCARDED, ...CURRENT], BOUNDARY)

    fireEvent.click(await view.findByTestId('chat-pane-show-earlier'))

    await view.findByText('the forgotten reply')
    expect(view.getByText('the fresh reply')).toBeTruthy()
    expect(view.queryByTestId('chat-pane-show-earlier')).toBeNull()
  })

  it('keeps the boundary marked once the history is on screen', async () => {
    /* The one thing the marker exists for is telling the two halves apart, so
     * revealing the history must not erase the line between them. */
    const view = renderPane([...DISCARDED, ...CURRENT], BOUNDARY)

    fireEvent.click(await view.findByTestId('chat-pane-show-earlier'))

    await view.findByText('the forgotten reply')
    expect(view.getByTestId('conversation-boundary-row')).toBeTruthy()
    expect(view.getByTestId('chat-pane-hide-earlier')).toBeTruthy()
    expect(view.getByTestId('conversation-boundary-time')).toBeTruthy()
  })

  it('collapses the history again from the same row', async () => {
    const view = renderPane([...DISCARDED, ...CURRENT], BOUNDARY)

    fireEvent.click(await view.findByTestId('chat-pane-show-earlier'))
    await view.findByText('the forgotten reply')
    fireEvent.click(view.getByTestId('chat-pane-hide-earlier'))

    await view.findByTestId('chat-pane-show-earlier')
    expect(view.queryByText('the forgotten reply')).toBeNull()
  })

  it('draws every row and no control when no reset is on record', async () => {
    const view = renderPane([...DISCARDED, ...CURRENT], undefined)

    await view.findByText('the forgotten reply')
    expect(view.getByText('the fresh reply')).toBeTruthy()
    expect(view.queryByTestId('chat-pane-show-earlier')).toBeNull()
  })

  it('hides a whole transcript that predates the boundary', async () => {
    const view = renderPane(DISCARDED, BOUNDARY)

    const control = await view.findByTestId('chat-pane-show-earlier')
    expect(control).toBeTruthy()
    expect(view.queryByText('the forgotten reply')).toBeNull()
  })

  it('says the thread is fresh rather than that the crewmate has been quiet', async () => {
    /* A crewmate whose conversation was just discarded has rows in the
     * transcript and none drawn -- the exact shape "has been quiet" tests for.
     * It has not been quiet; it has been reset, and the two lines send the
     * reader to different places (the Work log, versus the composer). */
    const view = renderPane(DISCARDED, BOUNDARY)

    await view.findByTestId('chat-pane-show-earlier')
    expect(view.queryByTestId('crewmate-quiet-hint')).toBeNull()
  })

  it('does not pull the whole transcript to re-answer what the boundary answered', async () => {
    /* A DM longer than one hydrate page whose conversation was just reset has
     * rows in the window and none drawn — the exact shape "this bounded window
     * proved nothing about whether the crewmate ever spoke" tests for. Latching
     * the unbounded read there costs a whole-transcript fetch on every reset and
     * every reopen, and it finds nothing new: the boundary still withholds those
     * rows. */
    const detail = (await import('../api/client')).api.chatSlotDetail as ReturnType<typeof vi.fn>
    const view = renderPane(DISCARDED, BOUNDARY, { slotPaneHasMore: { [SLOT]: true } })

    await view.findByTestId('chat-pane-show-earlier')
    // Every read stayed bounded: none asked for the whole history.
    for (const call of detail.mock.calls) expect(call[1]).not.toBeUndefined()
  })

  it('keeps a row whose timestamp it cannot read, and stops there', async () => {
    /* Fails OPEN. Showing a message that may be older is a smaller error than
     * hiding one that is not, and a prefix walk means a single unreadable row
     * cannot take the rest of the conversation down with it. */
    const view = renderPane(
      [{ role: 'assistant', content: 'no timestamp at all', cls: '', ts: undefined }, ...CURRENT],
      BOUNDARY,
    )

    await view.findByText('no timestamp at all')
    expect(view.getByText('the fresh reply')).toBeTruthy()
    // Nothing is withheld, so there is no line to draw either.
    expect(view.queryByTestId('conversation-boundary-row')).toBeNull()
  })
})
