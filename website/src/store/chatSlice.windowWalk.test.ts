/** `walkWindowBackTo`: a most-recent window that misses the rows a tab holds is
 *  extended OLDER one clamp-sized page at a time, and STOPS the moment the
 *  replacing reducer could keep everything -- it does not read to the start.
 *
 *  The sibling files (`chatSlice.refreshSlotBound.test.ts`,
 *  `chatSlice.boundedRefetchShrink.test.ts`) use a 300-row corpus, where one
 *  walked page always reaches row 0, so they cannot tell "walked until anchored"
 *  from "read everything in pages". This file uses a corpus several pages deep
 *  so the distinction is observable: the rows fetched are the ones the server
 *  GAINED plus one overlapping page, and row 0 is never requested.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

const SERVER_CLAMP = 500

type Row = { role: string; content: string; cls: string; ts: string; meta?: { mid: string } }

const rows = (n: number, from = 0): Row[] =>
  Array.from({ length: n }, (_, i) => ({
    role: (from + i) % 2 === 0 ? 'user' : 'assistant',
    content: `m${from + i}`,
    cls: 'msg',
    ts: new Date(Date.UTC(2026, 0, 1, 0, 0, from + i)).toISOString(),
    meta: { mid: `mid-${from + i}` },
  }))

let HISTORY: Row[] = []
/** When set, the handler answers every older page with the SAME cursor it was
 *  asked for -- a server that never moves older while still saying `has_more`. */
let STALL_CURSOR = false

vi.mock('../api/client', () => ({
  api: {
    /** The handler: `limit` clamped to 500, `before` an index into the collapsed
     *  corpus, the slice `[before - limit, before)`, `next_before` its start. */
    chatSlotDetail: vi.fn((_slot: string, limit?: number, before?: number) => {
      const corpus = HISTORY
      const total = corpus.length
      const end = before !== undefined ? Math.max(0, Math.min(before, total)) : total
      const eff = limit === undefined ? undefined : Math.min(limit, SERVER_CLAMP)
      const start = eff === undefined ? 0 : Math.max(0, end - eff)
      return Promise.resolve({
        messages: corpus.slice(start, end),
        has_more: start > 0,
        total,
        next_before: STALL_CURSOR && before !== undefined ? before : start,
        running: false,
      })
    }),
    resumeChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  },
}))

import chatReducer, {
  PANE_HYDRATE_LIMIT,
  WINDOW_WALK_MAX_PAGES,
  hydrateSlotMessages,
  refreshSlot,
  setActiveSlot,
  switchSlot,
} from './chatSlice'
import { api } from '../api/client'

const SLOT = 'slot-1'

function makeStore(extra: Record<string, unknown> = {}) {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...base, activeSlot: SLOT, ...extra } },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

/** `[limit, before]` of every request, in order. */
const requests = () =>
  (api.chatSlotDetail as unknown as { mock: { calls: unknown[][] } }).mock.calls.map(c => [c[1], c[2]])
const limits = () => requests().map(r => r[0])

describe('walkWindowBackTo', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    STALL_CURSOR = false
  })

  describe('refreshSlot', () => {
    it('stops the walk at the first page that reaches the view, not at the start', async () => {
      // The tab holds the newest 40 rows of a 1300-row transcript; the server then
      // gains 700. The count-matched page (floor 50) is clear of the view, so the
      // walk starts: page 1 (rows 1450..1949) still misses it, page 2 (950..1449)
      // spans it. Two older pages, then stop -- rows 0..949 are never asked for.
      const held = 40
      HISTORY = rows(1300)
      const store = makeStore({
        messages: HISTORY.slice(1300 - held),
        slotHasMore: true,
        slotOldestIndex: 1300 - held,
        slotCursorKey: SLOT,
      })
      HISTORY = rows(2000)

      await store.dispatch(refreshSlot(SLOT) as never)

      expect(requests()).toEqual([
        [PANE_HYDRATE_LIMIT, undefined],
        [SERVER_CLAMP, 1950],
        [SERVER_CLAMP, 1450],
      ])
      const after = store.getState().chat
      const contents = after.messages.map(m => m.content)
      expect(after.messages).toHaveLength(PANE_HYDRATE_LIMIT + 2 * SERVER_CLAMP)
      // Everything the view held survived, and the gap between it and the
      // newest row is filled -- no hole was spliced in.
      expect(contents).toContain(`m${1300 - held}`)
      expect(contents).toContain('m1299')
      expect(contents).toContain('m1999')
      expect(contents[0]).toBe('m950')
      expect(contents).not.toContain('m0')
      // The cursor describes exactly what is loaded.
      expect({ hasMore: after.slotHasMore, oldest: after.slotOldestIndex })
        .toEqual({ hasMore: true, oldest: 950 })
    })

    it('spends at most WINDOW_WALK_MAX_PAGES older pages, then hands the reducer what it has', async () => {
      // The gap is wider than the cap covers: the view held rows 1260..1299 and the
      // server is now 12,000 rows deep. The walk takes exactly the cap and stops,
      // and the reducer -- finding no anchor -- keeps no head: the 40 held rows
      // leave the view, one page-back away. That is the documented cost taken
      // instead of a 12,000-row read.
      const held = 40
      HISTORY = rows(1300)
      const store = makeStore({
        messages: HISTORY.slice(1300 - held),
        slotHasMore: true,
        slotOldestIndex: 1300 - held,
        slotCursorKey: SLOT,
      })
      HISTORY = rows(12_000)

      await store.dispatch(refreshSlot(SLOT) as never)

      const sent = limits()
      expect(sent).toHaveLength(1 + WINDOW_WALK_MAX_PAGES)
      expect(sent).not.toContain(undefined)
      expect(Math.max(...(sent as number[]))).toBeLessThanOrEqual(SERVER_CLAMP)
      const after = store.getState().chat
      const loaded = PANE_HYDRATE_LIMIT + WINDOW_WALK_MAX_PAGES * SERVER_CLAMP
      expect(after.messages).toHaveLength(loaded)
      expect(after.messages[0].content).toBe(`m${12_000 - loaded}`)
      expect(after.messages.map(m => m.content)).not.toContain('m1299')
      expect({ hasMore: after.slotHasMore, oldest: after.slotOldestIndex })
        .toEqual({ hasMore: true, oldest: 12_000 - loaded })
    })

    it('stops after one older page when the server cursor does not move, well short of the cap', async () => {
      // A handler that keeps answering the cursor it was asked for, while still
      // saying has_more, must not be paid for again: the walk reads that nothing
      // moved older and stops, keeping `hasMore` as the server reported it.
      const held = 40
      HISTORY = rows(1300)
      const store = makeStore({
        messages: HISTORY.slice(1300 - held),
        slotHasMore: true,
        slotOldestIndex: 1300 - held,
        slotCursorKey: SLOT,
      })
      HISTORY = rows(12_000)
      STALL_CURSOR = true

      await store.dispatch(refreshSlot(SLOT) as never)

      expect(limits()).toHaveLength(2)
      expect(store.getState().chat.slotHasMore).toBe(true)
    })

    it('does not walk at all when the count-matched page already reaches the view', async () => {
      // Negative control: the walk must not fire where the old design did not
      // refetch either. The view holds the newest 120 rows, nothing was gained, the
      // page IS the view.
      HISTORY = rows(2000)
      const store = makeStore({
        messages: HISTORY.slice(2000 - 120),
        slotHasMore: true,
        slotOldestIndex: 2000 - 120,
        slotCursorKey: SLOT,
      })
      await store.dispatch(refreshSlot(SLOT) as never)
      expect(requests()).toEqual([[120, undefined]])
      expect(store.getState().chat.messages).toHaveLength(120)
    })
  })

  describe('switchSlot', () => {
    it('closes an observed coverage hole by walking, and stops where the cache begins', async () => {
      // A background cache of rows 300..399 (a reader who had paged back), then the
      // server grows to 900. The switch asks for the cache's count (floored to 100):
      // the window 800..899 is clear of the cache, the shortfall check OBSERVES the
      // hole, and one older page (300..799) anchors the cache's oldest row. Stop:
      // rows 0..299 are never asked for.
      HISTORY = rows(900)
      const store = makeStore({ activeSlot: 'other' })
      store.dispatch(setActiveSlot('other'))
      store.dispatch(hydrateSlotMessages({
        slot: SLOT, messages: rows(100, 300), hasMore: true,
        bounded: true, total: 400, running: false,
      }))

      await store.dispatch(switchSlot(SLOT) as never)

      expect(requests()).toEqual([
        [100, undefined],
        [SERVER_CLAMP, 800],
      ])
      const after = store.getState().chat
      const contents = after.messages.map(m => m.content)
      expect(after.messages).toHaveLength(600)
      expect(contents[0]).toBe('m300')
      expect(contents).toContain('m399')
      expect(contents.at(-1)).toBe('m899')
      expect(contents).not.toContain('m0')
      expect({ hasMore: after.slotHasMore, oldest: after.slotOldestIndex })
        .toEqual({ hasMore: true, oldest: 300 })
    })

    it('reaches the start when the cache sits deeper than one page, every request bounded', async () => {
      // The cache holds rows 100..399 and the server grows to 1300. Window 1000..1299
      // misses it; one older page (500..999) still misses it; the next (0..499)
      // reaches the start. The reducer then anchors the page's rows inside the
      // cache and keeps nothing above (the page spans the cache), so the view is the
      // whole corpus -- and every request was bounded.
      HISTORY = rows(1300)
      const store = makeStore({ activeSlot: 'other' })
      store.dispatch(setActiveSlot('other'))
      store.dispatch(hydrateSlotMessages({
        slot: SLOT, messages: rows(300, 100), hasMore: true,
        bounded: true, total: 400, running: false,
      }))

      await store.dispatch(switchSlot(SLOT) as never)

      expect(limits()).toEqual([300, SERVER_CLAMP, SERVER_CLAMP])
      const after = store.getState().chat
      expect(after.messages).toHaveLength(1300)
      expect(after.messages[0].content).toBe('m0')
      expect(after.slotHasMore).toBe(false)
    })
  })
})
