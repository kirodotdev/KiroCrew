import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, waitFor, fireEvent, act } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { PANE_HYDRATE_LIMIT, hydrateSlotMessages, appendSlotMessage, switchSlot } from '../store/chatSlice'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* The session grid mounts one ChatPane per session, and each pane hydrates its
 * own slot. An unbounded hydrate therefore costs one FULL history per visible
 * pane, concurrently. These tests pin the bound at the call site: the limit
 * argument must be present, and it must be a sane positive size the backend
 * accepts (it clamps to 1..500 and 400s on limit < 1). */

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
import { api } from '../api/client'

function makeStore(slotKey: string, activeSlot?: string, messages: unknown[] = [], running = false) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      ...(activeSlot
        ? { chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot, messages } as RootState['chat'] }
        : {}),
    } as Partial<RootState>,
  })
}

function renderPane(slotKey: string, opts: { onOpenFull?: (slot: string) => void; activeSlot?: string; messages?: unknown[]; running?: boolean } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey, opts.activeSlot, opts.messages, opts.running)
  const view = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} onOpenFull={opts.onOpenFull} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  return { ...view, store }
}

beforeEach(() => {
  vi.clearAllMocks()
  // Not `...Once`: one test issuing a different number of calls shifts the shared
  // FIFO queue, and a later test then silently receives an earlier one's payload.
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
    messages: [], running: false, has_more: false, total: 0,
  })
})

describe('ChatPane hydrate is bounded', () => {
  it('passes a message limit when hydrating the pane slot', async () => {
    renderPane('pane-bound-1')
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalled())
    const [slot, limit] = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(slot).toBe('pane-bound-1')
    // The pre-fix call site passed the slot alone, so the limit arrived undefined
    // and the server returned the whole history.
    expect(limit).toBeTypeOf('number')
    expect(limit).toBeGreaterThan(0)
    expect(limit).toBeLessThanOrEqual(500)
  })

  it('hydrates a slot that is already mid-turn unbounded, so the tail is not all it shows', async () => {
    // A background slot's stream state reads idle until an SSE frame arrives, so
    // the bound cut raw chunk rows and the pane showed only the response tail.
    renderPane('pane-running-1', { running: true })
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalled())
    const [slot, limit] = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(slot).toBe('pane-running-1')
    expect(limit).toBeUndefined()
  })

  it('hydrates each pane once, so the bound is what caps a multi-pane grid', async () => {
    renderPane('pane-bound-2')
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalledTimes(1))
    const limits = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.map(c => c[1])
    expect(limits.every(l => typeof l === 'number' && l > 0)).toBe(true)
  })

  it('marks the cut when older messages exist, so the top is not a false beginning', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: [], running: false, has_more: true, total: 120,
    })
    const view = renderPane('pane-bound-3', { onOpenFull: vi.fn() })
    expect(await view.findByText(/earlier messages/i)).toBeTruthy()
  })

  // The pane's own hydrate is the only thing that can tell a BACKGROUND slot how
  // many rows the server holds, and the warm merge needs that baseline to tell a
  // remote rewind from a page it was simply built too early to carry. A bounded
  // page still reports the full-history total, so this does not widen the fetch.
  it('seeds the server row count from the pane hydrate, so a later warm has a baseline', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: [], running: false, has_more: true, total: 120,
    })
    const view = renderPane('pane-bound-total', { onOpenFull: vi.fn() })
    expect(await view.findByText(/earlier messages/i)).toBeTruthy()
    expect(view.store.getState().chat.slotServerTotal['pane-bound-total']).toBe(120)
  })

  it('shows no marker when the pane already holds the whole conversation', async () => {
  const hydrated = [{ role: 'assistant', content: 'hydrated sentinel', ts: '2026-08-13T09:00:00Z', meta: { mid: 'h-1' } }]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: hydrated, running: false, has_more: false, total: 1,
    })
    const view = renderPane('pane-bound-4', { onOpenFull: vi.fn() })
    // Anchor on rendered content first: waiting only for the CALL proves the fetch
    // started, so an absence asserted there passes before the row could appear.
    await view.findByText('hydrated sentinel')
    expect(view.queryByText(/earlier messages/i)).toBeNull()
  })

  it('leaves split view through the caller rather than navigating inside it', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: [], running: false, has_more: true, total: 120,
    })
    const onOpenFull = vi.fn()
    const view = renderPane('pane-bound-5', { onOpenFull })
    fireEvent.click(await view.findByText(/earlier messages/i))
    // The grid stays mounted on /chat, so the caller owns the exit -- assert the
    // handover. No anchor: an empty pane has no oldest message to land on.
    expect(onOpenFull).toHaveBeenCalledWith('pane-bound-5', undefined, undefined)
  })

  it('hands the full session the pane\'s oldest ts AND its mid, so it lands near the cut', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: [
        { role: 'user', content: 'oldest held', ts: '2026-08-13T09:00:00Z', meta: { mid: 'oldest-1' } },
        { role: 'assistant', content: 'newer', ts: '2026-08-13T09:05:00Z' },
      ],
      running: false, has_more: true, total: 120,
    })
    const onOpenFull = vi.fn()
    const view = renderPane('pane-bound-anchor', { onOpenFull })
    fireEvent.click(await view.findByText(/earlier messages/i))
    // The row promises EARLIER messages, so the destination must be the cut, not
    // the newest turn. Two rows can share a ts, so carry the mid as the identity.
    expect(onOpenFull).toHaveBeenCalledWith('pane-bound-anchor', '2026-08-13T09:00:00Z', 'oldest-1')
  })

  it('hides the marker on the active slot, whose pane renders the full history', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: [{ role: 'assistant', content: 'hydrated sentinel', ts: '2026-08-13T09:00:00Z', meta: { mid: 'h-1' } }],
      running: false, has_more: true, total: 120,
    })
    // A contrast is the only deterministic shape: `hydrateSlotMessages` returns early
    // for the active slot, so that pane's fetch has no observable effect to wait on.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-active', 'pane-active', [
      { role: 'assistant', content: 'store history', ts: '2026-08-13T09:00:00Z', meta: { mid: 's-1' } },
    ])
    const view = render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey="pane-active" onOpenFull={vi.fn()} />
              <ChatPane slotKey="pane-background" onOpenFull={vi.fn()} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalledTimes(2))
    await view.findByText(/earlier messages/i)
    // Not `waitFor`: it passes on the first poll while the count is transiently 1 and
    // can never see it reach 2, so it held with the guard removed. Settle, then assert.
    await new Promise((r) => setTimeout(r, 300))
    expect(view.queryAllByText(/earlier messages/i)).toHaveLength(1)
  })

  it('hides the marker when no caller can act on it', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: [{ role: 'assistant', content: 'hydrated sentinel', ts: '2026-08-13T09:00:00Z', meta: { mid: 'h-1' } }],
      running: false, has_more: true, total: 120,
    })
    const view = renderPane('pane-bound-7')
    await view.findByText('hydrated sentinel')
    expect(view.queryByText(/earlier messages/i)).toBeNull()
  })
})

/* A pane can mount against an IDLE slot and have the user start a turn before the
 * bounded fetch is served. The server slices RAW rows, so that page would be the tail
 * of the in-flight response -- the limit must still be upgradable at that point. */
describe('a turn that starts while the bounded fetch is in flight upgrades the limit', () => {
  it('refetches unbounded when an idle slot starts running mid-hydrate', async () => {
    // Never resolves: pins the pane in the window where the bounded fetch is in flight.
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(() => new Promise(() => {}))
    const store = makeStore('pane-midturn', undefined, [], false)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey="pane-midturn" onOpenFull={vi.fn()} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalled())
    expect((api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls[0][1]).toBe(PANE_HYDRATE_LIMIT)

    // The turn starts. Pre-fix the limit was already latched bounded and never upgraded,
    // so the pane committed the tail of the streaming response with no marker.
    act(() => {
      store.dispatch(sseSlots([{ key: 'pane-midturn', messages: 0, running: true, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }] as unknown as Parameters<typeof sseSlots>[0]))
    })
    await waitFor(() =>
      expect((api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.some((c) => c[1] === undefined)).toBe(true))
  })
})

/* Reducer-level pins for the two ways a bounded pane page can strand a transcript.
 * Both are store mechanics, so they are exercised against the reducer directly
 * rather than through a pane render. */
function reducerStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
}
function row(mid: string, content = mid) {
  return { role: 'assistant', content, ts: '2026-08-13T09:00:00Z', meta: { mid } }
}

describe('a bounded pane page is superseded once by the unbounded refetch', () => {
  it('replaces the bounded page, keeps the live tail, and updates the marker', () => {
    const store = reducerStore()
    const slot = 'pane-upgrade'
    // Pane mounts idle: bounded page of 2 rows, server has older history.
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('b-1'), row('b-2')], hasMore: true, bounded: true }))
    expect(store.getState().chat.slotMessages[slot].map((m) => m.meta?.mid)).toEqual(['b-1', 'b-2'])
    // A frame lands while the unbounded refetch is in flight.
    store.dispatch(appendSlotMessage({ slot, message: row('live-1') as never }))
    // The turn's unbounded refetch resolves with the FULL history.
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('a-0'), row('b-1'), row('b-2')], hasMore: false, bounded: false }))
    expect(store.getState().chat.slotMessages[slot].map((m) => m.meta?.mid)).toEqual(['a-0', 'b-1', 'b-2', 'live-1'])
    expect(store.getState().chat.slotPaneHasMore[slot]).toBe(false)
  })

  it('refuses a second upgrade and refuses a bounded page over an unbounded one', () => {
    const store = reducerStore()
    const slot = 'pane-once'
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('b-1')], hasMore: true, bounded: true }))
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('a-0'), row('b-1')], hasMore: false, bounded: false }))
    const afterUpgrade = store.getState().chat.slotMessages[slot].map((m) => m.meta?.mid)
    // A later unbounded page must not re-upgrade, and a bounded one must not win.
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('x-9')], hasMore: false, bounded: false }))
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('y-9')], hasMore: true, bounded: true }))
    expect(store.getState().chat.slotMessages[slot].map((m) => m.meta?.mid)).toEqual(afterUpgrade)
  })
})

describe('a pruned slot leaves no marker behind to suppress a later hydrate', () => {
  it('hydrates a recreated slot after sseSlots pruned the original', () => {
    const store = reducerStore()
    const slot = 'pane-reused'
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('old-1')], hasMore: true, bounded: true }))
    expect(store.getState().chat.slotPaneHasMore[slot]).toBe(true)
    // Slot removed remotely: the push carries a different live slot.
    store.dispatch(sseSlots([{ key: 'other', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }] as unknown as Parameters<typeof sseSlots>[0]))
    expect(store.getState().chat.slotPaneHasMore[slot]).toBeUndefined()
    expect(store.getState().chat.slotPaneBounded[slot]).toBeUndefined()
    // Same key recreated: its pane must be able to hydrate again.
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('new-1')], hasMore: false, bounded: true }))
    expect(store.getState().chat.slotMessages[slot].map((m) => m.meta?.mid)).toEqual(['new-1'])
  })
})

/* The bounded-length record indexes INTO the pane array, so any writer that
 * replaces that array must invalidate it or the next upgrade slices at the wrong
 * offset and re-appends rows the new page already carries. */
describe('replacing the pane array invalidates the bounded-length record', () => {
  it('does not duplicate rows when a full transcript replaced the bounded page', () => {
    const slot = 'pane-stale'
    // State after: pane hydrated bounded (record = 2), then the user visited the
    // slot so the active view holds its FULL history.
    const store = configureStore({
      reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
      preloadedState: {
        chat: {
          ...chatReducer(undefined, { type: '@@INIT' }),
          activeSlot: slot,
          messages: [row('a-0'), row('b-1'), row('b-2')],
          slotMessages: { [slot]: [row('b-1'), row('b-2')] },
          slotPaneBounded: { [slot]: 2 },
          slotPaneHasMore: { [slot]: true },
          slotHydrated: { [slot]: true },
        } as unknown as RootState['chat'],
      } as Partial<RootState>,
    })
    // Switching away caches the full transcript over the bounded page.
    store.dispatch(switchSlot.pending('rid', 'other'))
    expect(store.getState().chat.slotMessages[slot].map((m) => m.meta?.mid)).toEqual(['a-0', 'b-1', 'b-2'])
    // The slot then starts a turn, so its pane refetches unbounded.
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('a-0'), row('b-1'), row('b-2')], hasMore: false, bounded: false }))
    const mids = store.getState().chat.slotMessages[slot].map((m) => m.meta?.mid)
    expect(new Set(mids).size).toBe(mids.length)
    expect(mids).toEqual(['a-0', 'b-1', 'b-2'])
  })

  it('clears the record even when the write carries no marker', () => {
    const store = reducerStore()
    const slot = 'pane-nomarker'
    // A caller that omits has_more leaves writeSlotPage nothing to record, so it
    // returns early -- the record must be handled before that return.
    store.dispatch(appendSlotMessage({ slot, message: row('live-1') as never }))
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('b-1')], bounded: true }))
    expect(store.getState().chat.slotPaneHasMore[slot]).toBeUndefined()
    expect(store.getState().chat.slotPaneBounded[slot]).toBe(1)
  })
})

/* Two copies of one row have to be recognised as one row. A row the user just
 * sent carries only its send id until the echo lands; the server stores that id
 * and stamps its own, so its snapshot copy carries both. */
function sent(sendId: string, content = sendId) {
  return { role: 'user', content, ts: '2026-08-13T09:10:00Z', meta: { sendId } }
}
function sentOnServer(sendId: string, mid: string, content = sendId) {
  return { role: 'user', content, ts: '2026-08-13T09:10:00Z', meta: { sendId, mid } }
}

describe('reconciling a live tail against a wider page', () => {
  it('does not duplicate a just-sent row the unbounded page already carries', () => {
    const store = reducerStore()
    const slot = 'pane-send'
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('b-1')], hasMore: true, bounded: true }))
    // The pane sends: optimistic row, echo not back yet, so no mid on it.
    store.dispatch(appendSlotMessage({ slot, message: sent('s-1') as never }))
    // The refetch resolves; the server persisted that row before acking the send.
    store.dispatch(hydrateSlotMessages({
      slot, messages: [row('a-0'), row('b-1'), sentOnServer('s-1', 'm-9')], hasMore: false, bounded: false,
    }))
    const contents = store.getState().chat.slotMessages[slot].map((m) => m.content)
    expect(contents).toEqual(['a-0', 'b-1', 's-1'])
  })

  it('matches on mid once the echo has reconciled the row', () => {
    const store = reducerStore()
    const slot = 'pane-echoed'
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('b-1')], hasMore: true, bounded: true }))
    // Post-echo shape: sendId stripped, server mid adopted.
    store.dispatch(appendSlotMessage({ slot, message: row('m-9', 'sent') as never }))
    store.dispatch(hydrateSlotMessages({
      slot, messages: [row('a-0'), row('b-1'), row('m-9', 'sent')], hasMore: false, bounded: false,
    }))
    expect(store.getState().chat.slotMessages[slot].map((m) => m.content)).toEqual(['a-0', 'b-1', 'sent'])
  })

  it('keeps a row with no identity rather than guessing it is a duplicate', () => {
    const store = reducerStore()
    const slot = 'pane-noid'
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('b-1')], hasMore: true, bounded: true }))
    store.dispatch(appendSlotMessage({ slot, message: { role: 'assistant', content: 'legacy', ts: '2026-08-13T09:20:00Z' } as never }))
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('a-0'), row('b-1')], hasMore: false, bounded: false }))
    expect(store.getState().chat.slotMessages[slot].map((m) => m.content)).toEqual(['a-0', 'b-1', 'legacy'])
  })
})

describe('an explicit slot delete clears the bounded-length record', () => {
  it('leaves no record behind for a reused slot key', () => {
    const store = reducerStore()
    const slot = 'pane-deleted'
    store.dispatch(hydrateSlotMessages({ slot, messages: [row('b-1')], hasMore: true, bounded: true }))
    expect(store.getState().chat.slotPaneBounded[slot]).toBe(1)
    store.dispatch({ type: 'chat/deleteSlot/fulfilled', payload: slot })
    expect(store.getState().chat.slotPaneHasMore[slot]).toBeUndefined()
    expect(store.getState().chat.slotPaneBounded[slot]).toBeUndefined()
  })
})

describe('a warm snapshot does not drop a row sent while it was in flight', () => {
  it('preserves the newer prior tail past the warm page', () => {
    const store = reducerStore()
    const slot = 'pane-warm'
    store.dispatch(appendSlotMessage({ slot, message: row('m-1') as never }))
    store.dispatch(appendSlotMessage({ slot, message: row('m-2') as never }))
    // The user sends while the warm fetch is already out.
    store.dispatch(appendSlotMessage({ slot, message: sent('s-2') as never }))
    store.dispatch({
      type: 'chat/warmSlotCache/fulfilled',
      payload: { key: slot, messages: [row('m-1'), row('m-2')], hasMore: false },
    })
    expect(store.getState().chat.slotMessages[slot].map((m) => m.content)).toEqual(['m-1', 'm-2', 's-2'])
  })
})

/* The anchor jump has to know whether the active view is still the cached BOUNDED
 * page: switchSlot.pending seeds it and clears slotLoading, so slotLoading is
 * false in exactly the failing case. The bounded-length record is the signal. */
describe('the bounded-length record marks the active view as provisional', () => {
  it('is present after switchSlot.pending restores a bounded cache, absent after fulfilled', () => {
    const slot = 'pane-anchor'
    const store = configureStore({
      reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
      preloadedState: {
        chat: {
          ...chatReducer(undefined, { type: '@@INIT' }),
          slotMessages: { [slot]: [row('b-1'), row('b-2')] },
          slotPaneBounded: { [slot]: 2 },
          slotPaneHasMore: { [slot]: true },
          slotHydrated: { [slot]: true },
        } as unknown as RootState['chat'],
      } as Partial<RootState>,
    })
    store.dispatch(switchSlot.pending('rid', slot))
    // The active view is the 50-row-style bounded page, and slotLoading is FALSE --
    // which is why a slotLoading guard would be dead code here.
    expect(store.getState().chat.messages.map((m) => m.meta?.mid)).toEqual(['b-1', 'b-2'])
    expect(store.getState().chat.slotLoading).toBe(false)
    expect(store.getState().chat.slotPaneBounded[slot]).toBe(2)
    // The full transcript arrives and supersedes it; the record must go.
    store.dispatch({
      type: 'chat/switchSlot/fulfilled',
      payload: { key: slot, messages: [row('a-0'), row('b-1'), row('b-2')], running: false, hasMore: false, queue: [], nextBefore: 0 },
      meta: { arg: slot },
    })
    expect(store.getState().chat.slotPaneBounded[slot]).toBeUndefined()
  })
})
