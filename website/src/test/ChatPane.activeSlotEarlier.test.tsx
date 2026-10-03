import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* The Members page mounts ChatPane on the ACTIVE slot (a crewmate DM). That
 * slot shows the store's main list, which holds only the newest page, so the
 * pane must offer the same older-history walk the full chat page does --
 * without it the reader cannot scroll past the last few turns. */

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

const SLOT = 'member-conductor'
const NEWEST = [
  { role: 'user', content: 'status?', cls: '', ts: '2026-10-03T16:00:00Z' },
  { role: 'assistant', content: 'newest reply', cls: '', ts: '2026-10-03T16:00:05Z' },
]

function makeStore(chat: Partial<RootState['chat']>) {
  const base = chatReducer(undefined, { type: '@@init' })
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      chat: { ...base, activeSlot: SLOT, messages: NEWEST, ...chat } as RootState['chat'],
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 120, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(chat: Partial<RootState['chat']>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={makeStore(chat)}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} crewmate={{ name: 'Conductor' }} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => { vi.clearAllMocks() })

describe('a pane on the active slot', () => {
  it('offers the older-history walk and pages from the oldest loaded index', async () => {
    const detail = api.chatSlotDetail as ReturnType<typeof vi.fn>
    const view = renderPane({ slotHasMore: true, slotOldestIndex: 70, slotCursorKey: SLOT })
    const bar = await view.findByTestId('load-earlier-messages')
    detail.mockResolvedValueOnce({ messages: [], running: false, has_more: false, total: 120, next_before: 0 })
    fireEvent.click(bar)
    await waitFor(() => expect(detail).toHaveBeenCalledWith(SLOT, expect.any(Number), 70, expect.anything()))
  })

  it('draws no bar when nothing older exists', async () => {
    const view = renderPane({ slotHasMore: false, slotOldestIndex: 0, slotCursorKey: SLOT })
    await view.findByText('newest reply')
    expect(view.queryByTestId('load-earlier-messages')).toBeNull()
  })

  it('draws no bar while the cursor still describes another chat', async () => {
    const view = renderPane({ slotHasMore: true, slotOldestIndex: 70, slotCursorKey: 'some-other-slot' })
    await view.findByText('newest reply')
    expect(view.queryByTestId('load-earlier-messages')).toBeNull()
  })
})

/* The Members page never calls `switchSlot`, so a crewmate DM is usually NOT
 * the active slot: its pane is a warm background pane holding one bounded
 * page. It must page back from its own cursor, in place. */
describe('a pane on a background slot', () => {
  const OLDER = [
    { role: 'user', content: 'older question', cls: '', ts: '2026-10-03T15:00:00Z', meta: { mid: 'm-old-1' } },
    { role: 'assistant', content: 'older reply', cls: '', ts: '2026-10-03T15:00:05Z', meta: { mid: 'm-old-2' } },
  ]

  it('offers the bar from the hydrate cursor and prepends the older page', async () => {
    const detail = api.chatSlotDetail as ReturnType<typeof vi.fn>
    detail.mockResolvedValueOnce({ messages: NEWEST, running: false, has_more: true, total: 120, next_before: 50 })
    const view = renderPane({ activeSlot: null, messages: [] })
    const bar = await view.findByTestId('load-earlier-messages')
    detail.mockResolvedValueOnce({ messages: OLDER, running: false, has_more: false, total: 120, next_before: 0 })
    fireEvent.click(bar)
    await waitFor(() => expect(detail).toHaveBeenCalledWith(SLOT, expect.any(Number), 50))
    await view.findByText('older reply')
    expect(view.getByText('newest reply')).toBeTruthy()
    // That page reached the start, so the bar goes away.
    await waitFor(() => expect(view.queryByTestId('load-earlier-messages')).toBeNull())
  })

  it('draws no bar when the hydrate page is the whole history', async () => {
    const detail = api.chatSlotDetail as ReturnType<typeof vi.fn>
    detail.mockResolvedValueOnce({ messages: NEWEST, running: false, has_more: false, total: 2, next_before: 0 })
    const view = renderPane({ activeSlot: null, messages: [] })
    await view.findByText('newest reply')
    expect(view.queryByTestId('load-earlier-messages')).toBeNull()
  })
})

describe('loadOlderSlotMessages', () => {
  const hydrated = async () => {
    const { hydrateSlotMessages } = await import('../store/chatSlice')
    const store = makeStore({ activeSlot: null, messages: [] })
    store.dispatch(hydrateSlotMessages({ slot: SLOT, messages: NEWEST as never, hasMore: true, bounded: true, nextBefore: 50 }))
    return store
  }

  it('refuses a second fetch while one is in flight', async () => {
    const { loadOlderSlotMessages } = await import('../store/chatSlice')
    const store = await hydrated()
    let resolve!: (v: unknown) => void
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise(r => { resolve = r }))
    const run = store.dispatch(loadOlderSlotMessages(SLOT))
    store.dispatch(loadOlderSlotMessages(SLOT))
    expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
    expect(store.getState().chat.slotPaneLoadingOlder?.[SLOT]).toBe(true)
    resolve({ messages: [{ role: 'assistant', content: 'older', ts: '2026-10-03T14:00:00Z' }], has_more: true, next_before: 10 })
    await run
    const chat = store.getState().chat
    expect(chat.slotMessages[SLOT].map(m => m.content)).toEqual(['older', 'status?', 'newest reply'])
    expect(chat.slotPaneNextBefore?.[SLOT]).toBe(10)
    expect(chat.slotPaneLoadingOlder?.[SLOT]).toBeUndefined()
  })

  it('drops a page whose cursor moved while it flew', async () => {
    const { loadOlderSlotMessages } = await import('../store/chatSlice')
    const store = await hydrated()
    store.dispatch(loadOlderSlotMessages.fulfilled(
      { slot: SLOT, before: 70, nextBefore: 20, messages: [{ role: 'assistant', content: 'stale', ts: '2026-10-03T14:00:00Z' }] as never, hasMore: true },
      'req', SLOT,
    ))
    expect(store.getState().chat.slotMessages[SLOT].map(m => m.content)).toEqual(['status?', 'newest reply'])
    expect(store.getState().chat.slotPaneNextBefore?.[SLOT]).toBe(50)
  })

  it('marks a failed fetch for its own slot only', async () => {
    const { loadOlderSlotMessages } = await import('../store/chatSlice')
    const store = await hydrated()
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('boom'))
    await store.dispatch(loadOlderSlotMessages(SLOT))
    expect(store.getState().chat.slotPaneOlderError?.[SLOT]).toBe(true)
    expect(store.getState().chat.slotOlderError).toBe(false)
  })
})
