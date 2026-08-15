import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

// Older-history paging had two defects, one per test below.
//
// The cursor (slotOldestIndex) is a RAW index into the server's history, but the
// client stores a FILTERED list (filterMessages drops 'chunk'/'done') and the
// reducer also mints client-only rows ('thinking'). A cursor derived from the
// client array length is therefore in the wrong units.
const TOTAL = 500
const PAGE = 200 // api_chat_slot_resume returns messages[-200:]

type FakeMsg = { role: string; content: string; ts: string }

/** Deterministic history: index N has ts 2026-01-01T00:00:00.000Z + N seconds,
 *  and content is unique so a row's identity in assertions is unambiguous.
 *
 *  Indices 99 and 100 deliberately share a ts AND a role and carry no meta.mid:
 *  a coarse clock stamps two rows appended in the same tick identically, and a
 *  channel replay legitimately produces such a pair. They also STRADDLE a page
 *  boundary, so one is already resident when the other arrives -- the only
 *  arrangement in which a ts-keyed dedupe can discard one of them. */
const PAIR = 99
const HISTORY: FakeMsg[] = Array.from({ length: TOTAL }, (_, i) => ({
  role: i === PAIR || i === PAIR + 1 ? 'user' : i % 2 === 0 ? 'user' : 'assistant',
  content: `m${i}`,
  ts: new Date(Date.UTC(2026, 0, 1, 0, 0, i === PAIR ? PAIR + 1 : i)).toISOString(),
}))

vi.mock('../api/client', () => ({
  api: {
    // Mirrors the handler's pagination branch: end = min(before, total),
    // start = max(0, end - limit), has_more = start > 0.
    chatSlotDetail: vi.fn((_slot: string, limit?: number, before?: number) => {
      const lim = limit ?? 200
      const end = before !== undefined ? Math.max(0, Math.min(before, TOTAL)) : TOTAL
      const start = Math.max(0, end - lim)
      return Promise.resolve({ messages: HISTORY.slice(start, end), has_more: start > 0, total: TOTAL, next_before: start })
    }),
    resumeChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  },
}))

import chatReducer, {
  setActiveSlot,
  sseChatMessage,
  resumeFromHistory,
  loadOlderMessages,
} from './chatSlice'
import { api } from '../api/client'

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

/** Arms the cursor the way resume does: last PAGE of TOTAL, has_more true. */
function resumed(store: ReturnType<typeof makeStore>) {
  store.dispatch(setActiveSlot('active'))
  const recent = HISTORY.slice(TOTAL - PAGE)
  store.dispatch(
    resumeFromHistory.fulfilled(
      { ok: true, key: 'active', nextBefore: TOTAL - PAGE, messages: recent, hasMore: true, total: TOTAL },
      'req-resume',
      { key: 'active', title: 'active' },
    ),
  )
}

const detail = () => api.chatSlotDetail as unknown as { mock: { calls: unknown[][] } }

describe('loadOlderMessages', () => {
  beforeEach(() => vi.clearAllMocks())

  it('fetches a page instead of self-blocking on its own pending flag', async () => {
    const store = makeStore()
    resumed(store)
    expect(store.getState().chat.slotOldestIndex).toBe(TOTAL - PAGE)

    await store.dispatch(loadOlderMessages())

    // `pending` sets loadingOlder before the payload creator runs, so a creator
    // that reads the same flag returns null and never calls the API at all.
    expect(detail().mock.calls.length).toBe(1)
    expect(store.getState().chat.messages.length).toBeGreaterThan(PAGE)
  })

  it('does not skip history when the client holds a client-only message', async () => {
    const store = makeStore()
    resumed(store)

    // A 'thinking' row is minted by the reducer and never comes from the server,
    // so it inflates the client array relative to the server's `total`.
    store.dispatch(sseChatMessage({ slot: 'active', role: 'thinking', content: 'reasoning' }))
    expect(store.getState().chat.messages.some((m) => m.role === 'thinking')).toBe(true)

    await store.dispatch(loadOlderMessages())
    await store.dispatch(loadOlderMessages())

    // Pages must be contiguous. A cursor in the wrong units steps past a
    // message, which no later page ever covers.
    expect(detail().mock.calls.map((c) => c[2])).toEqual([TOTAL - PAGE, TOTAL - PAGE - 100])

    const held = new Set(store.getState().chat.messages.map((m) => m.content))
    const oldestHeld = HISTORY.findIndex((m) => held.has(m.content))
    expect(oldestHeld).toBeGreaterThanOrEqual(0)
    const missing = HISTORY.slice(oldestHeld).filter((m) => !held.has(m.content)).map((m) => m.content)

    expect(missing).toEqual([])
  })

  it('keeps two distinct rows that share a ts and a role', async () => {
    const store = makeStore()
    resumed(store)

    // Page back to the start of history so indices 0 and 1 are loaded.
    for (let i = 0; i < 10 && store.getState().chat.slotHasMore; i++) {
      await store.dispatch(loadOlderMessages())
    }
    expect(store.getState().chat.slotHasMore).toBe(false)

    // Identity is meta.mid, which neither row has, so neither may be discarded.
    // A ts-and-role dedupe key drops the one that arrives second.
    const contents = store.getState().chat.messages.map((m) => m.content)
    expect(contents).toContain(`m${PAIR}`)
    expect(contents).toContain(`m${PAIR + 1}`)
  })

  it('does not overlap when the server collapses rows before returning them', async () => {
    // A page returns fewer rows than the raw span it consumed, so only the
    // server's cursor steps far enough to avoid re-delivering rows.
    const collapsed = (m: FakeMsg) => Number(m.content.slice(1)) % 5 === 0
    const detailMock = api.chatSlotDetail as unknown as {
      mockImplementation: (f: (s: string, l?: number, b?: number) => Promise<unknown>) => void
    }
    detailMock.mockImplementation((_slot: string, limit?: number, before?: number) => {
      const lim = limit ?? 200
      const end = before !== undefined ? Math.max(0, Math.min(before, TOTAL)) : TOTAL
      const start = Math.max(0, end - lim)
      return Promise.resolve({
        messages: HISTORY.slice(start, end).filter((m) => !collapsed(m)),
        has_more: start > 0,
        total: TOTAL,
        next_before: start,
      })
    })

    const store = makeStore()
    resumed(store)
    await store.dispatch(loadOlderMessages())
    await store.dispatch(loadOlderMessages())

    const contents = store.getState().chat.messages.map((m) => m.content)
    const dupes = [...new Set(contents.filter((c, i) => contents.indexOf(c) !== i))]
    expect(dupes).toEqual([])
  })
})

describe('resume cursor', () => {
  beforeEach(() => { vi.clearAllMocks() })

  // Both directions: without the positive case, a thrown thunk would leave
  // hasMore false and the guard assertion would pass for the wrong reason.
  it('advertises older history when the server sends a cursor', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    ;(api.resumeChatSlot as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true, key: 'active', messages: [], has_more: true, total: TOTAL, next_before: TOTAL - PAGE,
    })
    await store.dispatch(resumeFromHistory({ key: 'active', title: 'active' }) as never)
    expect(store.getState().chat.slotHasMore).toBe(true)
    expect(store.getState().chat.slotOldestIndex).toBe(TOTAL - PAGE)
  })

  it('does not advertise older history when the server omits the cursor', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    ;(api.resumeChatSlot as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true, key: 'active', messages: [], has_more: true, total: TOTAL,
    })
    await store.dispatch(resumeFromHistory({ key: 'active', title: 'active' }) as never)
    expect(store.getState().chat.slotHasMore).toBe(false)
    expect(store.getState().chat.slotOldestIndex).toBe(0)
  })
})
