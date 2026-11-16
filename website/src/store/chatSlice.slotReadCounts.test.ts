/** The dev-build slot read counts (`dev/slotReadCounts.ts`): each `refreshSlot` /
 *  `warmSlotCache` call that fetched is one read, and a read whose window walk
 *  fetched older pages is also counted as walked, with those pages and the reason
 *  it walked. A walk that fetched nothing is a bounded read. A production build
 *  records nothing and installs no devtools global.
 *
 *  The corpus and handler mock mirror `chatSlice.refreshSlotBound.test.ts`; the
 *  cases mirror its walk branches so each reason is pinned on the shape that
 *  actually produces it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

const TOTAL = 300
const SERVER_CLAMP = 500

type Row = { role: string; content: string; cls: string; ts?: string; meta?: { mid: string } }

const rows = (n: number, from = 0): Row[] =>
  Array.from({ length: n }, (_, i) => ({
    role: (from + i) % 2 === 0 ? 'user' : 'assistant',
    content: `m${from + i}`,
    cls: 'msg',
    ts: new Date(Date.UTC(2026, 0, 1, 0, 0, from + i)).toISOString(),
    meta: { mid: `mid-${from + i}` },
  }))

let HISTORY: Row[] = rows(TOTAL)

vi.mock('../api/client', () => ({
  api: {
    /** The handler: most-recent-N slice before `before`, `limit` clamped to 500. */
    chatSlotDetail: vi.fn((slot: string, limit?: number, before?: number) => {
      const total = HISTORY.length
      const end = before !== undefined ? Math.max(0, Math.min(before, total)) : total
      const eff = limit === undefined ? undefined : Math.min(limit, SERVER_CLAMP)
      const start = eff === undefined ? 0 : Math.max(0, end - eff)
      return Promise.resolve({
        key: slot,
        messages: HISTORY.slice(start, end),
        has_more: start > 0,
        next_before: start,
        total,
        running: false,
        queue: [],
      })
    }),
  },
}))

import chatReducer, { hydrateSlotMessages, refreshSlot, setActiveSlot, warmSlotCache } from './chatSlice'
import { api } from '../api/client'
import { resetSlotReadCounts, slotReadCounts } from '../dev/slotReadCounts'

const SLOT = 'slot-1'

function makeStore(extra: Record<string, unknown> = {}) {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...base, activeSlot: SLOT, ...extra } },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

/** A view paged back to rows `[oldest, oldest + view.length)` of the corpus. */
function viewOf(view: Row[], oldest: number) {
  return makeStore({ messages: view, slotHasMore: oldest > 0, slotOldestIndex: oldest, slotCursorKey: SLOT })
}
const pagedBack = (held: number) => viewOf(HISTORY.slice(TOTAL - held), TOTAL - held)

/** Older pages the handler served (requests that carried a `before` cursor). */
const olderPagesServed = () =>
  (api.chatSlotDetail as unknown as { mock: { calls: unknown[][] } }).mock.calls.filter(c => c[2] !== undefined).length

const devGlobal = () => (window as unknown as { __kcSlotReads?: () => unknown }).__kcSlotReads

describe('slot read counts', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    HISTORY = rows(TOTAL)
    resetSlotReadCounts()
  })
  afterEach(() => {
    vi.unstubAllEnvs()
    resetSlotReadCounts()
  })

  it('counts a count-matched refresh as one bounded read, readable from devtools', async () => {
    const store = pagedBack(180)
    expect(devGlobal()).toBeUndefined()

    await store.dispatch(refreshSlot(SLOT) as never)

    expect(olderPagesServed()).toBe(0)
    expect(slotReadCounts()).toEqual({ [SLOT]: { reads: 1, walked: 0, pages: 0, reasons: {} } })
    expect(devGlobal()?.()).toEqual(slotReadCounts())
  })

  it('counts a refresh whose page slid clear of the view as walked, disjoint', async () => {
    const held = 180
    const store = pagedBack(held)
    HISTORY = [...HISTORY, ...rows(held + 20, TOTAL)]

    await store.dispatch(refreshSlot(SLOT) as never)

    const served = olderPagesServed()
    expect(served).toBeGreaterThan(0)
    expect(slotReadCounts()[SLOT]).toEqual({ reads: 1, walked: 1, pages: served, reasons: { 'refresh:disjoint': 1 } })
  })

  it('names a duplicate anchor id ambiguous-mid', async () => {
    const dup = 'mid-shared'
    const held = 180
    const oldest = TOTAL - held
    const view = HISTORY.slice(oldest).map((r, i) => (i === 0 || i === held - 20 ? { ...r, meta: { mid: dup } } : r))
    HISTORY = HISTORY.map((r, i) => (i === oldest || i === oldest + held - 20 ? { ...r, meta: { mid: dup } } : r))
    const store = viewOf(view, oldest)

    await store.dispatch(refreshSlot(SLOT) as never)

    expect(slotReadCounts()[SLOT]).toMatchObject({ reads: 1, walked: 1, reasons: { 'refresh:ambiguous-mid': 1 } })
  })

  it('names a floor that over-requests into id-less durable rows unidentified-floor', async () => {
    const identified = 20
    HISTORY = rows(TOTAL).map((r, i) => {
      if (i >= TOTAL - identified) return r
      const { meta: _meta, ...rest } = r
      return rest
    })
    const store = viewOf(HISTORY.slice(), 0)

    await store.dispatch(refreshSlot(SLOT) as never)

    expect(slotReadCounts()[SLOT]).toMatchObject({ reads: 1, walked: 1, reasons: { 'refresh:unidentified-floor': 1 } })
  })

  it('counts a ts-less anchor the walk accepts without paging as bounded', async () => {
    const held = 180
    const oldest = TOTAL - held
    HISTORY = HISTORY.map(({ ts: _ts, ...rest }) => rest)
    const store = viewOf(HISTORY.slice(oldest), oldest)

    await store.dispatch(refreshSlot(SLOT) as never)

    expect(olderPagesServed()).toBe(0)
    expect(slotReadCounts()[SLOT]).toMatchObject({ reads: 1, walked: 0, pages: 0 })
  })

  it('names a warm over a cache holding a ts-less durable row no-ts', async () => {
    const store = makeStore({ activeSlot: 'other' })
    store.dispatch(setActiveSlot('other'))
    const cache = rows(600).map((r, i) => (i === 10 ? { ...r, ts: undefined } : r))
    store.dispatch(hydrateSlotMessages({
      slot: SLOT, messages: cache as never, hasMore: false, bounded: true, total: 600, running: false,
    }))
    HISTORY = rows(2000)

    await store.dispatch(warmSlotCache(SLOT) as never)

    const served = olderPagesServed()
    expect(served).toBeGreaterThan(0)
    expect(slotReadCounts()[SLOT]).toEqual({ reads: 1, walked: 1, pages: served, reasons: { 'warm:no-ts': 1 } })
  })

  it('records nothing and installs no global in a production build', async () => {
    vi.stubEnv('DEV', false)
    const store = pagedBack(180)
    HISTORY = [...HISTORY, ...rows(200, TOTAL)]

    await store.dispatch(refreshSlot(SLOT) as never)

    expect(olderPagesServed()).toBeGreaterThan(0)
    expect(slotReadCounts()).toEqual({})
    expect(devGlobal()).toBeUndefined()
  })
})
