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
  return { ...view, store, qc }
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

  it('hydrates a slot that is already mid-turn with the same bound (#12907)', async () => {
    // An unbounded read of a long, running session froze the renderer, and the
    // crash recovery reopened it into the same freeze. The handler collapses chunk
    // runs before slicing, so the bound is safe mid-stream.
    renderPane('pane-running-1', { running: true })
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalled())
    const calls = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls
    expect(calls[0][0]).toBe('pane-running-1')
    expect(calls.every((c) => c[1] === PANE_HYDRATE_LIMIT)).toBe(true)
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
 * bounded fetch is served, so the limit must still be upgradable at that point. The
 * handler collapses chunk runs before slicing, so a bound is not a raw-row hazard. */
describe('a turn that starts while the bounded fetch is in flight keeps the limit', () => {
  it('keeps the bound when an idle slot starts running mid-hydrate', async () => {
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

    // The turn starts. The pane must not re-ask for the whole transcript.
    act(() => {
      store.dispatch(sseSlots([{ key: 'pane-midturn', messages: 0, running: true, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }] as unknown as Parameters<typeof sseSlots>[0]))
    })
    await act(async () => {})
    expect((api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.some((c) => c[1] === undefined)).toBe(false)
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

/* A pane whose first read was served under an allow-list value this document has
 * since moved past (an allow or revoke landed mid-read) must not install it. */
describe('ChatPane hydrate under an outdated allow-list value', () => {
  const row = (id: string, content: string) => ({ role: 'assistant', content, cls: '', ts: '2026-09-01T00:00:00Z', meta: { mid: id } })

  it('re-reads instead of installing rows served under an outdated value', async () => {
    let n = 0
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      n += 1
      return n === 1
        ? { messages: [row('m-1', 'stale link')], running: false, has_more: false, total: 1, redaction_gen: 'g-old' }
        : { messages: [row('m-1', 'chip')], running: false, has_more: false, total: 1, redaction_gen: 'g-new' }
    })
    const slot = 'pane-gen-1'
    const { store } = renderPane(slot, { activeSlot: 'other-slot' })
    act(() => { store.dispatch({ type: 'chat/redactionHostsGenAdopted', payload: 'g-new' }) })
    await waitFor(() => expect(store.getState().chat.slotMessages[slot]?.[0]?.content).toBe('chip'))
    expect(n).toBe(2)
  })

  it('writes the renewed answer back to the query cache, so a remount does not re-read it', async () => {
    let n = 0
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      n += 1
      return n === 1
        ? { messages: [row('m-1', 'stale link')], running: false, has_more: false, total: 1, redaction_gen: 'g-old' }
        : { messages: [row('m-1', 'chip')], running: false, has_more: false, total: 1, redaction_gen: 'g-new' }
    })
    const slot = 'pane-gen-5'
    const { store, qc } = renderPane(slot, { activeSlot: 'other-slot' })
    act(() => { store.dispatch({ type: 'chat/redactionHostsGenAdopted', payload: 'g-new' }) })
    await waitFor(() => expect(store.getState().chat.slotMessages[slot]?.[0]?.content).toBe('chip'))
    const cached = qc.getQueryCache().getAll().map(q => (q.state.data as { messages?: { content: string }[] } | undefined)?.messages?.[0]?.content)
    expect(cached).toContain('chip')
    expect(cached).not.toContain('stale link')
    // The re-read itself ran under its own key, scoped to the value it re-read for,
    // so another pane re-reading this slot for the same change shares the request.
    expect(qc.getQueryCache().getAll().some(q => q.queryKey.includes('renew') && q.queryKey.includes('g-new'))).toBe(true)
  })

  it('holds the re-read to the same check when a revoke lands during it', async () => {
    let n = 0
    let adopt: (gen: string) => void = () => {}
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      n += 1
      if (n === 1) return { messages: [row('m-1', 'stale link')], running: false, has_more: false, total: 1, redaction_gen: 'g-old' }
      if (n === 2) {
        // A revoke lands while this re-read is in flight: its rows predate it.
        adopt('g-newer')
        return { messages: [row('m-1', 'pre-revoke link')], running: false, has_more: false, total: 1, redaction_gen: 'g-new' }
      }
      return { messages: [row('m-1', 'chip')], running: false, has_more: false, total: 1, redaction_gen: 'g-newer' }
    })
    const slot = 'pane-gen-3'
    const { store } = renderPane(slot, { activeSlot: 'other-slot' })
    adopt = gen => { store.dispatch({ type: 'chat/redactionHostsGenAdopted', payload: gen }) }
    act(() => { adopt('g-new') })
    await waitFor(() => expect(store.getState().chat.slotMessages[slot]?.[0]?.content).toBe('chip'))
    expect(n).toBe(3)
  })

  it('shows the load error, not a blank pane, when the re-read fails, and Retry recovers', async () => {
    let n = 0
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      n += 1
      if (n === 1) return { messages: [row('m-1', 'stale link')], running: false, has_more: false, total: 1, redaction_gen: 'g-old' }
      if (n === 2) throw new Error('network')
      await retryLanded
      return { messages: [row('m-1', 'chip')], running: false, has_more: false, total: 1, redaction_gen: 'g-new' }
    })
    let land: () => void = () => {}
    const retryLanded = new Promise<void>(r => { land = r })
    const slot = 'pane-gen-4'
    const view = renderPane(slot, { activeSlot: 'other-slot' })
    act(() => { view.store.dispatch({ type: 'chat/redactionHostsGenAdopted', payload: 'g-new' }) })
    await waitFor(() => expect(view.getByTestId('chat-pane-hydrate-error')).toBeTruthy())
    // The outdated rows were never installed.
    expect(view.store.getState().chat.slotMessages[slot]?.[0]?.content).not.toBe('stale link')
    fireEvent.click(view.getByText(/retry/i))
    // The notice stays while the retry is in flight: nothing is healed yet.
    await waitFor(() => expect(n).toBe(3))
    expect(view.getByTestId('chat-pane-hydrate-error')).toBeTruthy()
    // ...and its Retry says so, rather than reading as a dead click.
    const busy = view.getByText(/retrying/i).closest('button')!
    expect(busy.getAttribute('aria-disabled')).toBe('true')
    land()
    await waitFor(() => expect(view.store.getState().chat.slotMessages[slot]?.[0]?.content).toBe('chip'))
    expect(view.queryByTestId('chat-pane-hydrate-error')).toBeNull()
  })

  it('clears a failed re-read once the cached read is current, rather than showing the error over it', async () => {
    let n = 0
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      n += 1
      if (n === 1) return { messages: [row('m-1', 'served')], running: false, has_more: false, total: 1, redaction_gen: 'g-old' }
      throw new Error('network')
    })
    const slot = 'pane-gen-6'
    const view = renderPane(slot, { activeSlot: 'other-slot' })
    act(() => { view.store.dispatch({ type: 'chat/redactionHostsGenAdopted', payload: 'g-new' }) })
    await waitFor(() => expect(view.getByTestId('chat-pane-hydrate-error')).toBeTruthy())
    // The change is undone: the cached read now matches, and installs without a re-read.
    act(() => { view.store.dispatch({ type: 'chat/redactionHostsGenAdopted', payload: 'g-old' }) })
    fireEvent.click(view.getByText(/retry/i))
    await waitFor(() => expect(view.store.getState().chat.slotMessages[slot]?.[0]?.content).toBe('served'))
    expect(view.queryByTestId('chat-pane-hydrate-error')).toBeNull()
  })

  it('installs a re-read that answers with the same value again, rather than looping', async () => {
    let n = 0
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      n += 1
      return { messages: [row('m-1', 'current')], running: false, has_more: false, total: 1, redaction_gen: 'g-server' }
    })
    const slot = 'pane-gen-2'
    const { store } = renderPane(slot, { activeSlot: 'other-slot' })
    act(() => { store.dispatch({ type: 'chat/redactionHostsGenAdopted', payload: 'g-before-restart' }) })
    await waitFor(() => expect(store.getState().chat.slotMessages[slot]?.[0]?.content).toBe('current'))
    expect(n).toBe(2)
  })
})

/* An allow, revoke or variant switch marks every loaded slot; only a pane that is
 * on screen re-reads itself, so one change never re-reads caches nobody sees. */
describe('ChatPane re-reads itself when its slot is marked', () => {
  const row = (id: string, content: string) => ({ role: 'assistant', content, cls: '', ts: '2026-09-01T00:00:00Z', meta: { mid: id } })

  it('warms a hydrated background pane once its slot is marked', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: [row('m-1', 'link')], running: false, has_more: false, total: 1 })
    const slot = 'pane-mark-1'
    const { store } = renderPane(slot, { activeSlot: 'other-slot' })
    await waitFor(() => expect(store.getState().chat.slotHydrated[slot]).toBe(true))
    const before = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.length
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: [row('m-1', 'chip')], running: false, has_more: false, total: 1 })
    act(() => { store.dispatch({ type: 'chat/markLoadedRowsChanged', payload: undefined }) })
    await waitFor(() => expect(store.getState().chat.slotMessages[slot]?.[0]?.content).toBe('chip'))
    expect((api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.length).toBe(before + 1)
    // The warm that re-served it cleared the mark, so nothing re-fires.
    expect(store.getState().chat.slotHeadUnverified[slot]).toBeUndefined()
  })

  it('shows the load error with a Retry when its marked re-read fails, and Retry re-reads', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: [row('m-1', 'link')], running: false, has_more: false, total: 1 })
    const slot = 'pane-mark-3'
    const view = renderPane(slot, { activeSlot: 'other-slot' })
    await waitFor(() => expect(view.store.getState().chat.slotHydrated[slot]).toBe(true))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network'))
    act(() => { view.store.dispatch({ type: 'chat/markLoadedRowsChanged', payload: undefined }) })
    await waitFor(() => expect(view.getByTestId('chat-pane-hydrate-error')).toBeTruthy())
    let land: () => void = () => {}
    const landed = new Promise<void>(r => { land = r })
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => { await landed; return { messages: [row('m-1', 'chip')], running: false, has_more: false, total: 1 } })
    fireEvent.click(view.getByText(/retry/i))
    // In flight: the notice stays and its Retry is busy, not dead.
    await waitFor(() => expect(view.getByText(/retrying/i).closest('button')!.getAttribute('aria-disabled')).toBe('true'))
    // Still focusable while busy, so a keyboard user keeps their place.
    expect((view.getByText(/retrying/i).closest('button') as HTMLButtonElement).disabled).toBe(false)
    land()
    await waitFor(() => expect(view.store.getState().chat.slotMessages[slot]?.[0]?.content).toBe('chip'))
    expect(view.queryByTestId('chat-pane-hydrate-error')).toBeNull()
  })

  it('announces the pane notice again when its heal Retry fails again', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: [row('m-1', 'link')], running: false, has_more: false, total: 1 })
    const slot = 'pane-mark-5'
    const view = renderPane(slot, { activeSlot: 'other-slot' })
    await waitFor(() => expect(view.store.getState().chat.slotHydrated[slot]).toBe(true))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network'))
    act(() => { view.store.dispatch({ type: 'chat/markLoadedRowsChanged', payload: undefined }) })
    await waitFor(() => expect(view.getByTestId('chat-pane-hydrate-error')).toBeTruthy())
    const first = view.getByTestId('chat-pane-hydrate-error')
    fireEvent.click(view.getByText(/retry/i))
    await waitFor(() => expect(view.getByTestId('chat-pane-hydrate-error')).not.toBe(first))
    expect(view.getByText(/^retry$/i)).toBeTruthy()
    // Said in words, not only by the re-mount's motion (reduced motion shows none).
    expect(view.getByText(/still couldn't refresh this session/i)).toBeTruthy()
  })

  it('re-reads the OPEN chat with its refresh when the pane showing it retries a failed heal', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: [row('m-1', 'link')], running: false, has_more: false, total: 1 })
    const slot = 'pane-mark-4'
    const view = renderPane(slot, { activeSlot: slot })
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalled())
    act(() => {
      view.store.dispatch({ type: 'chat/markLoadedRowsChanged', payload: { slot } })
      view.store.dispatch({ type: 'chat/refreshSlot/rejected', meta: { arg: slot }, error: { name: 'Error', message: 'network' } })
    })
    await waitFor(() => expect(view.getByTestId('chat-pane-hydrate-error')).toBeTruthy())
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockClear()
    fireEvent.click(view.getByText(/retry/i))
    // A warm exits for the open chat without reading; the refresh re-reads it.
    await waitFor(() => expect((api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.some(c => c[0] === slot)).toBe(true))
  })

  it('leaves the open chat to its own refresh', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: [row('m-1', 'link')], running: false, has_more: false, total: 1 })
    const slot = 'pane-mark-2'
    const { store } = renderPane(slot, { activeSlot: slot })
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalled())
    const before = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.length
    act(() => { store.dispatch({ type: 'chat/markLoadedRowsChanged', payload: undefined }) })
    await new Promise(r => setTimeout(r, 20))
    expect((api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.length).toBe(before)
  })
})
