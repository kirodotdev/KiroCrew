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
  refreshSlot,
  switchSlot,
} from './chatSlice'
import { api } from '../api/client'

// Pin the NARROW walk page size (100): loadOlderMessages sizes its page by
// viewport (narrow 100 / desktop 300), and these fixtures hold only 300 rows
// of history -- the desktop size would drain them in one page and leave
// nothing for the contiguity assertions.
window.matchMedia = ((q: string) => ({
  matches: q.includes('max-width'), media: q, onchange: null,
  addListener: () => {}, removeListener: () => {},
  addEventListener: () => {}, removeEventListener: () => {},
  dispatchEvent: () => false,
})) as unknown as typeof window.matchMedia


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

describe('a superseded switch must not overwrite a NEWER paging cursor', () => {
  beforeEach(() => vi.clearAllMocks())

  /** Two same-slot bounded switches, resolved NEWEST-FIRST. History grew between
   *  them, so the stale response carries an older `next_before`. */
  async function newestFirst() {
    const store = makeStore()
    store.dispatch(setActiveSlot('grow'))
    const mock = api.chatSlotDetail as unknown as ReturnType<typeof vi.fn>

    let releaseStale: (v: unknown) => void = () => {}
    const withheld = new Promise(res => { releaseStale = res })
    mock.mockReturnValueOnce(withheld as never)
    const stale = store.dispatch(switchSlot('grow'))

    // The newer switch sees the grown history: a HIGHER next_before.
    mock.mockResolvedValueOnce({ messages: HISTORY.slice(300, 400), has_more: true, total: 400, next_before: 300 })
    await store.dispatch(switchSlot('grow'))
    const newer = { hasMore: store.getState().chat.slotHasMore, oldest: store.getState().chat.slotOldestIndex }

    releaseStale({ messages: HISTORY.slice(100, 200), has_more: true, total: 200, next_before: 100 })
    await stale
    return { store, newer }
  }

  it('keeps the newer cursor when the stale response lands second', async () => {
    const { store, newer } = await newestFirst()

    const s = store.getState().chat
    expect(s.slotOldestIndex).toBe(newer.oldest)
    expect(s.slotHasMore).toBe(newer.hasMore)
  })

  it('installs NO cursor from a superseded switch, even with none landed', async () => {
    // Asserted the opposite until the fallback was ruled a defect: the newer refresh paged
    // to next_before 0 while the older switch carries 100, so landing 100 skips rows.
    const store = makeStore()
    store.dispatch(setActiveSlot('gap'))
    const mock = api.chatSlotDetail as unknown as ReturnType<typeof vi.fn>

    let releaseSwitch: (v: unknown) => void = () => {}
    const withheld = new Promise(res => { releaseSwitch = res })
    mock.mockReturnValueOnce(withheld as never)
    const switching = store.dispatch(switchSlot('gap'))

    mock.mockResolvedValueOnce({ messages: HISTORY.slice(0, 50), has_more: true, total: 50, next_before: 0 })
    await store.dispatch(refreshSlot('gap'))
    releaseSwitch({ messages: HISTORY.slice(100, 200), has_more: true, total: 200, next_before: 100 })
    await switching

    const s = store.getState().chat
    expect(s.slotOldestIndex).not.toBe(100)
  })
})

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

// `loadOlderMessages.fulfilled` is deliberately NOT one of these reset sites:
// `pending` always runs first, so a reset there would be unreachable dead code.
describe('slotOlderError is scoped to the slot that failed', () => {
  beforeEach(() => vi.clearAllMocks())

  /** Store in the post-failure state: 'active' tried to page older history and
   *  the fetch rejected, so its bar is showing the red retry label. */
  function afterFailedOlderFetch() {
    const store = makeStore()
    resumed(store)
    store.dispatch(loadOlderMessages.rejected(null, 'req-fail', undefined, { slot: 'active' }))
    expect(store.getState().chat.slotOlderError).toBe(true)
    return store
  }

  it('clears the flag when switchSlot re-bases history for another slot', () => {
    const store = afterFailedOlderFetch()

    store.dispatch(setActiveSlot('other'))
    store.dispatch(
      switchSlot.fulfilled(
        {
          key: 'other',
          messages: HISTORY.slice(TOTAL - PAGE),
          running: false,
          hasMore: true,
          total: TOTAL,
          rawCount: PAGE,
          queue: [],
        } as never,
        'req-switch',
        'other',
      ),
    )

    // hasMore true is what makes ChatPage render the bar at all, so this is the
    // arrangement in which a stale flag is visible rather than merely present.
    expect(store.getState().chat.slotHasMore).toBe(true)
    expect(store.getState().chat.slotOlderError).toBe(false)
  })

  it('clears the flag when resumeFromHistory re-bases history for another slot', () => {
    const store = afterFailedOlderFetch()

    store.dispatch(
      resumeFromHistory.fulfilled(
        { ok: true, key: 'other', nextBefore: TOTAL - PAGE, messages: HISTORY.slice(TOTAL - PAGE), hasMore: true, total: TOTAL },
        'req-resume-other',
        { key: 'other', title: 'other' },
      ),
    )

    expect(store.getState().chat.slotHasMore).toBe(true)
    expect(store.getState().chat.slotOlderError).toBe(false)
  })

  // refreshSlot carries no reset of its own: the clear rides the cursor re-base,
  // so this pins the invariant at a caller that never mentions the flag.
  it('clears the flag when refreshSlot re-bases the active chat', () => {
    const store = afterFailedOlderFetch()

    store.dispatch(
      refreshSlot.fulfilled(
        { key: 'active', messages: HISTORY.slice(TOTAL - PAGE), running: false, hasMore: true, total: TOTAL, nextBefore: TOTAL - PAGE, queue: [] } as never,
        'req-refresh',
        'active',
      ),
    )

    expect(store.getState().chat.slotHasMore).toBe(true)
    expect(store.getState().chat.slotOlderError).toBe(false)
  })

  it('marks the chat failed when its OWN older fetch rejects', () => {
    const store = makeStore()
    resumed(store)
    store.dispatch(loadOlderMessages.pending('req-own', undefined))
    store.dispatch(loadOlderMessages.rejected(null, 'req-own', undefined, { slot: 'active' }))
    const s = store.getState().chat
    expect(s.slotOlderError).toBe(true)
    expect(s.loadingOlder).toBe(false)
  })

  // The mirror of the switch-then-fail case: here the fetch is already in flight
  // when the switch lands, so the rejection arrives against a chat it never ran on.
  it('does NOT mark the new chat failed when the old chat fetch rejects after a switch', () => {
    const store = makeStore()
    resumed(store)
    store.dispatch(loadOlderMessages.pending('req-cross', undefined))
    expect(store.getState().chat.loadingOlder).toBe(true)

    store.dispatch(switchSlot.pending('req-switch-cross', 'other'))
    store.dispatch(loadOlderMessages.rejected(null, 'req-cross', undefined, { slot: 'active' }))

    const s = store.getState().chat
    expect(s.activeSlot).toBe('other')
    expect(s.slotOlderError).toBe(false)
    expect(s.loadingOlder).toBe(false)
  })

  // `pending` is the instant-switch arm: it moves activeSlot and restores a cached
  // transcript, so the bar would paint before `fulfilled` re-bases the cursor.
  it('clears the flag when switchSlot only reaches pending', () => {
    const store = afterFailedOlderFetch()

    store.dispatch(switchSlot.pending('req-switch-pending', 'other'))

    const s = store.getState().chat
    expect(s.activeSlot).toBe('other')
    expect(s.slotOlderError).toBe(false)
    // The cursor still describes the OUTGOING chat, so a switch de-keys it rather than
    // zeroing it; the paging gate refuses while that key and the active slot disagree.
    expect(s.slotCursorKey).toBeNull()
  })

  // A cancelled fetch rethrows its AbortError rather than naming a slot, so the
  // reducer's slot check cannot match and the reader is shown no failure.
  it('shows no error when a switch cancelled the fetch', async () => {
    const store = makeStore()
    resumed(store)
    ;(api.chatSlotDetail as unknown as { mockRejectedValueOnce: (e: unknown) => void })
      .mockRejectedValueOnce(new DOMException('Aborted', 'AbortError'))
    const before = detail().mock.calls.length

    await store.dispatch(loadOlderMessages())

    // Guards against a vacuous pass: the gate must have let the fetch run.
    expect(detail().mock.calls.length).toBe(before + 1)
    expect(store.getState().chat.slotOlderError).toBe(false)
  })

  it('refuses a mid-switch older fetch instead of paging at the outgoing offset', async () => {
    const store = afterFailedOlderFetch()
    expect(store.getState().chat.slotOldestIndex).toBeGreaterThan(0)

    store.dispatch(switchSlot.pending('req-switch-cursor', 'other'))
    const before = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.length
    const result = await store.dispatch(loadOlderMessages())

    expect(result.meta.condition).toBe(true)
    expect((api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.length).toBe(before)
  })
})
