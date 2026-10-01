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
/** When set, the newest-window read (no cursor) reports a turn in flight and
 *  every older page reports it finished -- a turn ending mid-walk. */
let TURN_ENDS_MID_WALK = false
/** Newest-window reads answered so far; the turn is over after the first. */
let NEWEST_READS = 0
/** Fired once, on the first OLDER page request -- the moment the walk is in flight. */
let ON_OLDER: (() => void) | null = null

vi.mock('../api/client', () => ({
  api: {
    /** The handler: `limit` clamped to 500, `before` an index into the collapsed
     *  corpus, the slice `[before - limit, before)`, `next_before` its start. */
    chatSlotDetail: vi.fn((_slot: string, limit?: number, before?: number) => {
      if (before !== undefined && ON_OLDER) {
        const fire = ON_OLDER
        ON_OLDER = null
        fire()
      }
      const corpus = HISTORY
      const total = corpus.length
      const end = before !== undefined ? Math.max(0, Math.min(before, total)) : total
      const eff = limit === undefined ? undefined : Math.min(limit, SERVER_CLAMP)
      const start = eff === undefined ? 0 : Math.max(0, end - eff)
      const newestRead = before === undefined ? ++NEWEST_READS : 0
      return Promise.resolve({
        messages: corpus.slice(start, end),
        has_more: start > 0,
        total,
        next_before: STALL_CURSOR && before !== undefined ? before : start,
        running: TURN_ENDS_MID_WALK && newestRead === 1,
      })
    }),
    resumeChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  },
}))

import chatReducer, {
  PANE_HYDRATE_LIMIT,
  WINDOW_WALK_MAX_PAGES,
  appendSlotMessage,
  hydrateSlotMessages,
  refreshSlot,
  setActiveSlot,
  sseChatMessage,
  switchSlot,
  warmSlotCache,
} from './chatSlice'
import { api } from '../api/client'

const SLOT = 'slot-1'

function makeStore(extra: Record<string, unknown> = {}) {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...base, activeSlot: SLOT, ...extra } },
    // Match the production store, including the default development checks.
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
    TURN_ENDS_MID_WALK = false
    NEWEST_READS = 0
    ON_OLDER = null
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
        // One re-read of the newest edge, so the tail is as fresh as the status.
        [SERVER_CLAMP, undefined],
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

    it('reports the run state of the newest response it saw, not the first page', async () => {
      // The first page is read while a turn runs; the turn ends before the walk's
      // older page is answered. The walk's own latest read says idle, and that is
      // what must reach the reducer -- the first page's `running: true` would
      // re-arm the run state over the `_done` that already landed.
      HISTORY = rows(1300)
      const store = makeStore({
        messages: HISTORY.slice(1260),
        slotHasMore: true,
        slotOldestIndex: 1260,
        slotCursorKey: SLOT,
      })
      HISTORY = rows(2000)
      TURN_ENDS_MID_WALK = true

      const res = await store.dispatch(refreshSlot(SLOT) as never) as { payload: { running: boolean } }

      expect(requests().length).toBeGreaterThan(1)
      expect(res.payload.running).toBe(false)
      expect(store.getState().chat.slotRunning).toBe(false)
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
      expect(sent).toHaveLength(2 + WINDOW_WALK_MAX_PAGES)
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

      expect(limits()).toHaveLength(3)
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
        [SERVER_CLAMP, undefined],
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

    it('returns the newest edge as of the walk\'s end, not the first page\'s partial tail', async () => {
      // Same coverage hole as above. While the walk pages older, the turn finishes:
      // the in-flight reply's final text lands as a new newest row. Pairing the
      // latest idle status with the first page's tail would publish the reply
      // without that row, with no live frame left to correct it.
      HISTORY = rows(900)
      const store = makeStore({ activeSlot: 'other' })
      store.dispatch(setActiveSlot('other'))
      store.dispatch(hydrateSlotMessages({
        slot: SLOT, messages: rows(100, 300), hasMore: true,
        bounded: true, total: 400, running: false,
      }))
      ON_OLDER = () => { HISTORY = rows(902) }

      await store.dispatch(switchSlot(SLOT) as never)

      const contents = store.getState().chat.messages.map(m => m.content)
      expect(contents.at(-1)).toBe('m901')
      expect(contents).toContain('m300')
      // Contiguous: the splice left no hole and no duplicate.
      expect(new Set(contents).size).toBe(contents.length)
      expect(contents).toHaveLength(602)
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

      expect(limits()).toEqual([300, SERVER_CLAMP, SERVER_CLAMP, SERVER_CLAMP])
      const after = store.getState().chat
      expect(after.messages).toHaveLength(1300)
      expect(after.messages[0].content).toBe('m0')
      expect(after.slotHasMore).toBe(false)
    })
  })

  describe('warmSlotCache', () => {
    /** A background pane holding rows `[from, from + n)`, as a switch away caches it. */
    function backgroundPane(n: number, from: number, total: number) {
      const store = makeStore({ activeSlot: 'other' })
      store.dispatch(setActiveSlot('other'))
      store.dispatch(hydrateSlotMessages({
        slot: SLOT, messages: rows(n, from), hasMore: from > 0,
        bounded: true, total, running: false,
      }))
      return store
    }
    const cached = (store: ReturnType<typeof makeStore>) =>
      (store.getState().chat.slotMessages?.[SLOT] ?? []).map(m => m.content)

    it('warms a pane past the clamp with one bounded page, not the whole transcript', async () => {
      // The reported case: a background pane holds the newest 8,000 rows of an
      // 8,000-row session and its turn ends with two new rows. One clamp-sized
      // page overlaps the cache, so one request does it -- the cache is too wide
      // for a count-matched limit, which used to mean an unbounded read.
      HISTORY = rows(8000)
      const store = backgroundPane(8000, 0, 8000)
      HISTORY = rows(8002)

      await store.dispatch(warmSlotCache(SLOT) as never)

      expect(requests()).toEqual([[SERVER_CLAMP, undefined]])
      const contents = cached(store)
      // The held head above the page survived, and the new rows landed.
      expect(contents).toHaveLength(8002)
      expect(contents[0]).toBe('m0')
      expect(contents.at(-1)).toBe('m8001')
      expect(new Set(contents).size).toBe(contents.length)
    })

    it('walks a coverage gap older, bounded, and keeps the held head where it anchors', async () => {
      // The pane holds rows 0..599 and the server gained 1,400 while it was off
      // screen. The newest page (1500..1999) misses the cache; the walk reads
      // 1000..1499 and 500..999, which anchors cache row 500. Stop there.
      HISTORY = rows(600)
      const store = backgroundPane(600, 0, 600)
      HISTORY = rows(2000)

      await store.dispatch(warmSlotCache(SLOT) as never)

      expect(requests()).toEqual([
        [SERVER_CLAMP, undefined],
        [SERVER_CLAMP, 1500],
        [SERVER_CLAMP, 1000],
        [SERVER_CLAMP, undefined],
      ])
      const contents = cached(store)
      expect(contents).toHaveLength(2000)
      // Rows 0..499 sat above the walked window: the reducer kept them.
      expect(contents[0]).toBe('m0')
      expect(contents).toContain('m599')
      expect(contents.at(-1)).toBe('m1999')
      expect(new Set(contents).size).toBe(contents.length)
    })

    it('keeps a just-sent row when the walk stops on a window that contains the whole cache', async () => {
      // The pane holds rows 1100..1149 plus a send the server has not persisted yet,
      // and the server is now 2,000 rows deep. The walk's second older page
      // (949..1448) contains the cache's oldest row, so it stops on a SUPERSET whose
      // own oldest row is older than the cache. That window covers the cache: the
      // just-sent row must survive as newer than the page, not be dropped as if the
      // walk had run out of pages.
      HISTORY = rows(1150)
      const store = backgroundPane(50, 1100, 1150)
      store.dispatch(appendSlotMessage({
        slot: SLOT,
        message: { role: 'user', content: 'just sent', cls: 'msg msg-u', ts: new Date(Date.UTC(2026, 0, 2)).toISOString(), meta: { sendId: 's-1' } } as never,
      }))
      HISTORY = rows(2000)

      await store.dispatch(warmSlotCache(SLOT) as never)

      expect(limits()).not.toContain(undefined)
      const contents = cached(store)
      expect(contents).toContain('m1100')
      expect(contents).toContain('m1999')
      expect(contents.at(-1)).toBe('just sent')
    })

    it.each([50, 5000])('keeps the cache when a user row is appended during an unanchored warm of %i rows', async (held) => {
      HISTORY = rows(held)
      const store = backgroundPane(held, 0, held)
      const before = store.getState().chat
      let live = before
      HISTORY = rows(20_000)
      ON_OLDER = () => {
        store.dispatch(appendSlotMessage({
          slot: SLOT,
          message: { role: 'user', content: 'sent during walk', cls: 'msg msg-u', meta: { sendId: 'during-walk' } },
        }))
        live = store.getState().chat
      }

      const payload = await store.dispatch(warmSlotCache(SLOT)).unwrap()

      expect(payload?.walkUnanchored).toBe(true)
      expect(payload?.cacheAtDispatch).toBe(before.slotMessages[SLOT])
      expect(limits()).toHaveLength(1 + WINDOW_WALK_MAX_PAGES + 1)
      expect(limits()).not.toContain(undefined)
      // An optimistic send changes the cache without changing the run tick.
      expect(live.slotRun[SLOT]?.tick).toBe(before.slotRun[SLOT]?.tick)
      expect(live.slotMessages[SLOT]).not.toBe(before.slotMessages[SLOT])
      const after = store.getState().chat
      expect(after.slotMessages[SLOT]).toBe(live.slotMessages[SLOT])
      expect(cached(store)).toEqual([...rows(held).map(m => m.content), 'sent during walk'])
      expect(after.slotPaneHasMore?.[SLOT]).toBe(live.slotPaneHasMore?.[SLOT])
      expect(after.slotPaneBounded?.[SLOT]).toBe(live.slotPaneBounded?.[SLOT])
      expect(after.slotServerTotal?.[SLOT]).toBe(20_000)
    })

    it('keeps an in-place streaming chunk received during an unanchored warm', async () => {
      HISTORY = rows(50)
      const store = backgroundPane(50, 0, 50)
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'first', seq: 1 }))
      const before = store.getState().chat
      let live = before
      HISTORY = rows(20_000)
      ON_OLDER = () => {
        store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' second', seq: 2 }))
        live = store.getState().chat
      }

      const payload = await store.dispatch(warmSlotCache(SLOT)).unwrap()

      expect(payload?.walkUnanchored).toBe(true)
      expect(payload?.cacheAtDispatch).toBe(before.slotMessages[SLOT])
      expect(limits()).toHaveLength(1 + WINDOW_WALK_MAX_PAGES + 1)
      expect(limits()).not.toContain(undefined)
      // The real chunk reducer edits the row, not the array length; Immer must
      // still give that slot a new array identity without mutating the snapshot.
      expect(live.slotMessages[SLOT]).toHaveLength(before.slotMessages[SLOT].length)
      expect(live.slotMessages[SLOT]).not.toBe(before.slotMessages[SLOT])
      expect(before.slotMessages[SLOT].at(-1)?.content).toBe('first')
      const after = store.getState().chat
      expect(after.slotMessages[SLOT]).toBe(live.slotMessages[SLOT])
      expect(cached(store)).toEqual([...rows(50).map(m => m.content), 'first second'])
      expect(after.slotMessages[SLOT].at(-1)).toMatchObject({
        role: 'streaming', content: 'first second', rawText: 'first second',
      })
      expect(after.slotPaneHasMore?.[SLOT]).toBe(live.slotPaneHasMore?.[SLOT])
      expect(after.slotPaneBounded?.[SLOT]).toBe(live.slotPaneBounded?.[SLOT])
      expect(after.slotRun[SLOT]).toEqual(live.slotRun[SLOT])
      expect(after.slotServerTotal?.[SLOT]).toBe(20_000)
    })

    it('keeps the newer cache and paging markers when an older unanchored warm settles last', async () => {
      HISTORY = rows(50)
      const store = backgroundPane(50, 0, 50)
      const detail = vi.mocked(api.chatSlotDetail)
      const readDetail = detail.getMockImplementation()!
      let releaseOlder!: () => void
      detail.mockImplementationOnce((...args) => {
        const response = readDetail(...args)
        return new Promise(resolve => { releaseOlder = () => resolve(response) })
      })
      // Hold the old snapshot while a newer warm observes rewritten history.
      const cacheAtDispatch = store.getState().chat.slotMessages[SLOT]
      const staleHistory = rows(20_000)
      HISTORY = staleHistory
      const older = store.dispatch(warmSlotCache(SLOT))
      HISTORY = rows(20)
      await store.dispatch(warmSlotCache(SLOT)).unwrap()
      const newer = store.getState().chat
      const requestsBeforeRelease = limits().length
      expect(cached(store)).toEqual(HISTORY.map(m => m.content))
      expect(newer.slotPaneHasMore?.[SLOT]).toBe(false)
      expect(newer.slotPaneBounded?.[SLOT]).toBeUndefined()

      // The older request finishes its bounded walk against its old corpus.
      HISTORY = staleHistory
      releaseOlder()
      const payload = await older.unwrap()
      expect(payload?.walkUnanchored).toBe(true)
      expect(payload?.cacheAtDispatch).toBe(cacheAtDispatch)
      expect(newer.slotMessages[SLOT]).not.toBe(cacheAtDispatch)
      expect(limits()).toHaveLength(requestsBeforeRelease + WINDOW_WALK_MAX_PAGES + 1)
      expect(limits()).not.toContain(undefined)
      const after = store.getState().chat
      expect(after.slotMessages[SLOT]).toBe(newer.slotMessages[SLOT])
      expect(after.slotPaneHasMore?.[SLOT]).toBe(newer.slotPaneHasMore?.[SLOT])
      expect(after.slotPaneBounded?.[SLOT]).toBe(newer.slotPaneBounded?.[SLOT])
      expect(after.slotServerTotal?.[SLOT]).toBe(20)
      expect(after.slotServerTotalSeq?.[SLOT]).toBe(newer.slotServerTotalSeq?.[SLOT])
      expect(after.slotRun[SLOT]).toEqual(newer.slotRun[SLOT])
    })

    it('never reads unbounded, spending at most 1 + cap + 1 requests, and replaces rather than splices when the cap runs out', async () => {
      // The gap is wider than the cap covers: the pane holds rows 0..4999 -- more
      // than the walk's whole window -- and the server is 20,000 rows deep. The walk
      // takes exactly the cap and stops, where the unbounded read it replaces would
      // have moved all 20,000. The walked window is disjoint from the cache, so
      // merging would publish a hole between row 4999 and the window; the pane takes
      // the window instead, with a paging cursor that reaches the rest.
      HISTORY = rows(5000)
      const store = backgroundPane(5000, 0, 5000)
      const cacheAtDispatch = store.getState().chat.slotMessages[SLOT]
      HISTORY = rows(20_000)

      const payload = await store.dispatch(warmSlotCache(SLOT)).unwrap()

      expect(payload?.walkUnanchored).toBe(true)
      expect(payload?.cacheAtDispatch).toBe(cacheAtDispatch)
      expect(Object.isFrozen(payload?.cacheAtDispatch)).toBe(true)
      const sent = limits()
      expect(sent).not.toContain(undefined)
      expect(sent).toHaveLength(1 + WINDOW_WALK_MAX_PAGES + 1)
      expect(Math.max(...(sent as number[]))).toBeLessThanOrEqual(SERVER_CLAMP)
      const contents = cached(store)
      const walked = (1 + WINDOW_WALK_MAX_PAGES) * SERVER_CLAMP
      expect(contents).toHaveLength(walked)
      expect(contents[0]).toBe(`m${20_000 - walked}`)
      expect(contents.at(-1)).toBe('m19999')
      expect(contents).not.toContain('m4999')
      expect(contents).toEqual(HISTORY.slice(-walked).map(m => m.content))
      expect(store.getState().chat.slotMessages[SLOT]).not.toBe(cacheAtDispatch)
      expect(store.getState().chat.slotPaneHasMore?.[SLOT]).toBe(true)
      expect(store.getState().chat.slotPaneBounded?.[SLOT]).toBe(walked)
    })
  })
})
