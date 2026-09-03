import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

// `switchSlot` (100) and `warmSlotCache` (50) bound their fetch while `refreshSlot`
// is unbounded, so the three write one slot's transcript at three sizes.
const TOTAL = 300
const BOUND = 100
const WARM = 50

type FakeMsg = { role: string; content: string; ts: string; meta?: Record<string, unknown> }

/** Persisted history. `chunk`/`done` are wire-only and never reach disk (measured: 0
 *  of 330789 rows), so the corpus a limit slices is already one row per message. */
function makeHistory(withMid: boolean): FakeMsg[] {
  return Array.from({ length: TOTAL }, (_, i) => ({
    role: i % 2 === 0 ? 'user' : 'assistant',
    content: `m${i}`,
    ts: new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString(),
    ...(withMid ? { meta: { mid: `mid-${i}` } } : {}),
  }))
}

let HISTORY: FakeMsg[] = makeHistory(true)
let RUNNING = false

vi.mock('../api/client', () => ({
  api: {
    // Mirrors the handler: `_collapse_wire_rows` folds the in-flight run and drops
    // `done` BEFORE `total` and the slice, so a limit takes collapsed rows.
    chatSlotDetail: vi.fn((_slot: string, limit?: number, before?: number) => {
      const collapsed = RUNNING
        ? [...HISTORY, { role: 'streaming', content: 'partial', ts: 'x' }]
        : [...HISTORY]
      const total = collapsed.length
      const end = before !== undefined ? Math.max(0, Math.min(before, total)) : total
      const start = limit === undefined ? 0 : Math.max(0, end - limit)
      return Promise.resolve({
        messages: collapsed.slice(start, end),
        has_more: start > 0,
        total,
        next_before: start,
        running: RUNNING,
      })
    }),
    resumeChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  },
}))

import chatReducer, {
  setActiveSlot, refreshSlot, switchSlot, warmSlotCache, hydrateSlotMessages,
} from './chatSlice'
import { api } from '../api/client'

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

const visible = (s: ReturnType<typeof makeStore>) => s.getState().chat.messages.length
/** The limits sent for a slot. The defect signal is the `limit` field, never a
 *  `returned` vs `total` comparison -- `total` is inflated while streaming. */
const limitsFor = (slot: string) =>
  (api.chatSlotDetail as unknown as { mock: { calls: unknown[][] } }).mock.calls
    .filter(c => c[0] === slot).map(c => c[1])

describe('a bounded refetch must not shrink what is already loaded', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    HISTORY = makeHistory(true)
    RUNNING = false
  })

  it('holds the full transcript across refreshSlot -> switchSlot -> refreshSlot', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))

    await store.dispatch(refreshSlot('active'))
    const full = visible(store)
    expect(full).toBe(TOTAL)

    await store.dispatch(switchSlot('active'))
    const afterSwitch = visible(store)

    await store.dispatch(refreshSlot('active'))
    expect({ afterSwitch, afterRefresh: visible(store) })
      .toEqual({ afterSwitch: full, afterRefresh: full })
  })

  it('holds the transcript when the page carries no row identity', async () => {
    HISTORY = makeHistory(false)
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))

    await store.dispatch(refreshSlot('active'))
    const full = visible(store)
    await store.dispatch(switchSlot('active'))
    expect(visible(store)).toBe(full)
  })

  // Correction 1: `slotRun` has only two writers and is never seeded from the slots
  // list, so a stream this client never witnessed reads `?? 'idle'` and bounds.
  it('does not bound a painted slot the client wrongly believes is idle', async () => {
    RUNNING = true
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))

    await store.dispatch(refreshSlot('active'))
    const full = visible(store)
    expect(store.getState().chat.slotRun?.active).toBeUndefined()

    await store.dispatch(switchSlot('active'))
    expect(limitsFor('active').at(-1)).toBeUndefined()
    expect(visible(store)).toBe(full)
  })

  // The decisive live capture: a background slot that had returned 111 rows unbounded
  // was refetched at limit=50. `switchSlot.pending` paints from this cache.
  it('does not let a warm shrink a background slot cache below what it holds', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('other'))
    store.dispatch(hydrateSlotMessages({
      slot: 'bg', messages: HISTORY.slice(0, 111), hasMore: false,
      bounded: false, total: TOTAL, running: false,
    }))
    expect(store.getState().chat.slotMessages.bg.length).toBe(111)

    await store.dispatch(warmSlotCache('bg'))
    expect(limitsFor('bg')).toEqual([undefined])
    expect(store.getState().chat.slotMessages.bg.length).toBeGreaterThanOrEqual(111)
  })

  // A cache at or below the limit still loses rows: unseen server growth moves the
  // window clear of it, and rows without `meta.mid` are replaced rather than merged.
  it('does not bound a small cache the server has grown past', async () => {
    HISTORY = makeHistory(false)
    const store = makeStore()
    // Seeded while backgrounded, then switched into -- `hydrateSlotMessages` refuses
    // the active slot, so seeding it directly would be a silent no-op.
    store.dispatch(setActiveSlot('other'))
    const painted = HISTORY.slice(0, 40)
    store.dispatch(hydrateSlotMessages({
      slot: 'active', messages: painted, hasMore: false,
      bounded: false, total: 40, running: false,
    }))
    expect(store.getState().chat.slotMessages.active?.length).toBe(40)

    await store.dispatch(switchSlot('active'))

    const shown = new Set(store.getState().chat.messages.map(m => m.content))
    const dropped = painted.filter(m => !shown.has(m.content)).map(m => m.content)
    expect({ limit: limitsFor('active').at(-1), dropped }).toEqual({ limit: undefined, dropped: [] })
  })

  it('does not bound a small background cache the server has grown past', async () => {
    HISTORY = makeHistory(false)
    const store = makeStore()
    store.dispatch(setActiveSlot('other'))
    const painted = HISTORY.slice(0, 30)
    store.dispatch(hydrateSlotMessages({
      slot: 'bg', messages: painted, hasMore: false,
      bounded: false, total: 30, running: false,
    }))

    await store.dispatch(warmSlotCache('bg'))

    const cache = new Set(store.getState().chat.slotMessages.bg.map(m => m.content))
    const dropped = painted.filter(m => !cache.has(m.content)).map(m => m.content)
    expect({ limit: limitsFor('bg').at(-1), dropped }).toEqual({ limit: undefined, dropped: [] })
  })

  // Negative control: the bound must SURVIVE where it was designed to help, so the
  // tests above passing cannot mean it was simply removed.
  it('still bounds a cold open and a cold warm', async () => {
    const store = makeStore()
    await store.dispatch(switchSlot('cold'))
    expect(limitsFor('cold')).toEqual([BOUND])

    store.dispatch(setActiveSlot('other'))
    await store.dispatch(warmSlotCache('coldwarm'))
    expect(limitsFor('coldwarm')).toEqual([WARM])
  })

  /** `olderHeadAbovePage` is the ONE cut all three reducers share, so the identity rule
   *  lives there. A bare `findIndex` cut at the FIRST row carrying the page-oldest id,
   *  and `meta.mid` is caller-supplied (minted only when absent), so a repeated id cut
   *  at the wrong occurrence. These two pin both directions of that rule on the warm
   *  path, which reaches the cut without the thunk-side strict check in front of it. */
  it('keeps the cache whole when the page-oldest id names two rows', async () => {
    const dup = 'mid-repeated'
    HISTORY = makeHistory(true).map((r, i) =>
      i === 0 || i === 60 ? { ...r, meta: { mid: dup } } : r,
    )
    const store = makeStore()
    store.dispatch(setActiveSlot('other'))
    store.dispatch(hydrateSlotMessages({
      slot: 'bg', messages: HISTORY.slice(0, 140), hasMore: false,
      bounded: false, total: TOTAL, running: false,
    }))

    await store.dispatch(warmSlotCache('bg'))

    // The ambiguous id declines the cut, and declining must not lose rows: the
    // reducer keeps the longer prior array instead of trusting a wrong boundary.
    const cache = store.getState().chat.slotMessages.bg
    const kept = new Set(cache.map(m => m.content))
    const dropped = HISTORY.slice(0, 140).filter(m => !kept.has(m.content)).map(m => m.content)
    expect(dropped).toEqual([])
  })

  it('still cuts on an id whose rows carry no ts', async () => {
    // Declining inside the shared cut costs the KEPT HEAD, not a round trip, so the
    // rule only requires that the two rows do not contradict each other. Demanding a
    // `ts` here would drop scrollback for legacy rows that have none.
    HISTORY = makeHistory(true).map(({ ts: _ts, ...rest }) => rest as never)
    const store = makeStore()
    store.dispatch(setActiveSlot('other'))
    store.dispatch(hydrateSlotMessages({
      slot: 'bg', messages: HISTORY.slice(0, 140), hasMore: false,
      bounded: false, total: TOTAL, running: false,
    }))

    await store.dispatch(warmSlotCache('bg'))

    const cache = store.getState().chat.slotMessages.bg
    const kept = new Set(cache.map(m => m.content))
    const dropped = HISTORY.slice(0, 140).filter(m => !kept.has(m.content)).map(m => m.content)
    expect(dropped).toEqual([])
  })

  it('leaves the cursor consistent with what is loaded', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    await store.dispatch(refreshSlot('active'))
    await store.dispatch(switchSlot('active'))

    const s = store.getState().chat
    expect({ hasMore: s.slotHasMore, paneHasMore: s.slotPaneHasMore?.active })
      .toEqual({ hasMore: false, paneHasMore: false })
  })
})
