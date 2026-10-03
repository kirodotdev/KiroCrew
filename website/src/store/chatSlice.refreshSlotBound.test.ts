/** `refreshSlot` must bound its fetch WITHOUT shrinking the open transcript.
 *
 *  The recurring refresh (WS reconnect, `chat_done`, a variant switch) used to
 *  pass no `limit` at all, so every one of them pulled the whole chained history
 *  — a cost that grows with the transcript and is paid again at the end of every
 *  turn (#4690). A FIXED bound is not available to it: unlike a pane warm, this
 *  thunk REPLACES `messages` in place, so a 50-row page would delete scrollback
 *  the user had paged back through.
 *
 *  The bound is therefore COUNT-MATCHED — at least as many rows as the view
 *  already holds, clamped to the handler's ceiling — and a window that does not
 *  reach the view is extended OLDER one clamp-sized page at a time
 *  (`walkWindowBackTo`) rather than re-read whole. These tests assert the
 *  `limit` argument that reaches `api.chatSlotDetail`, not merely the resulting
 *  state: the argument is the fix, and a state assertion alone would still pass
 *  if the bound were dropped. `undefined` -- the handler's read-everything
 *  shape -- must never appear in that list.
 *
 *  The `warmSlotCache` half of #4690 was already bounded by #3240 and is not
 *  touched here; `chatSlice.warmSlotCacheBound.test.ts` owns it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

const TOTAL = 300
/** The slot-detail handler's own clamp (`min(int(limit), 500)`), mirrored so a
 *  request above it comes back SHORT here exactly as it would in production. */
const SERVER_CLAMP = 500

type Row = {
  role: string
  content: string
  cls: string
  ts: string
  meta?: { mid: string; kind?: string; notice?: string }
  seq?: number
  gen?: string
}

const rows = (n: number, from = 0): Row[] =>
  Array.from({ length: n }, (_, i) => ({
    role: (from + i) % 2 === 0 ? 'user' : 'assistant',
    content: `m${from + i}`,
    cls: 'msg',
    ts: new Date(Date.UTC(2026, 0, 1, 0, 0, from + i)).toISOString(),
    meta: { mid: `mid-${from + i}` },
  }))

let HISTORY: Row[] = rows(TOTAL)
let RUNNING = false
/** Fired once, inside the next `chatSlotDetail` call. */
let DURING_FETCH: (() => void) | null = null
/** Fired once after the mock has frozen the response snapshot. */
let DURING_FETCH_AFTER_SNAPSHOT: (() => void) | null = null

vi.mock('../api/client', () => ({
  api: {
    /** Mirrors the handler: the corpus is collapsed to one row per displayed
     *  message BEFORE `total` and the slice, the slice is the most-recent-N, and
     *  `limit` is clamped to 500. */
    chatSlotDetail: vi.fn((_slot: string, limit?: number, before?: number) => {
      // Lets a test land a concurrent store write INSIDE the thunk's await, which is
      // the only way to reproduce a `loadOlderMessages` resolving mid-refresh.
      if (DURING_FETCH) {
        const fire = DURING_FETCH
        DURING_FETCH = null
        fire()
      }
      const corpus = [...HISTORY]
      if (DURING_FETCH_AFTER_SNAPSHOT) {
        const fire = DURING_FETCH_AFTER_SNAPSHOT
        DURING_FETCH_AFTER_SNAPSHOT = null
        fire()
      }
      const total = corpus.length
      const end = before !== undefined ? Math.max(0, Math.min(before, total)) : total
      const eff = limit === undefined ? undefined : Math.min(limit, SERVER_CLAMP)
      const start = eff === undefined ? 0 : Math.max(0, end - eff)
      return Promise.resolve({
        key: _slot,
        messages: corpus.slice(start, end),
        has_more: start > 0,
        next_before: start,
        total,
        running: RUNNING,
        queue: [],
      })
    }),
  },
}))

import chatReducer, {
  PANE_HYDRATE_LIMIT,
  REFRESH_LIMIT_CEILING,
  refreshSlot,
  replaceMessages,
  sseChatMessage,
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

/** A view that has paged back to the newest `held` rows of `TOTAL`. */
function pagedBack(held: number) {
  const oldest = TOTAL - held
  return makeStore({
    messages: HISTORY.slice(oldest),
    slotHasMore: oldest > 0,
    slotOldestIndex: oldest > 0 ? oldest : 0,
    slotCursorKey: SLOT,
  })
}

/** Every `limit` argument the thunk sent, in order. */
const limits = () =>
  (api.chatSlotDetail as unknown as { mock: { calls: unknown[][] } }).mock.calls.map(c => c[1])

/** The shape of a window that had to WALK: one count-matched page, then only
 *  clamp-sized older pages. Never the handler's read-everything shape
 *  (`undefined`), which is the argument this file exists to keep out. */
function expectWalkedFrom(first: number) {
  const sent = limits()
  expect(sent[0]).toBe(first)
  expect(sent.length).toBeGreaterThan(1)
  expect(sent.slice(1)).toEqual(sent.slice(1).map(() => SERVER_CLAMP))
  expect(sent).not.toContain(undefined)
}

describe('refreshSlot count-matched bound', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    HISTORY = rows(TOTAL)
    RUNNING = false
    DURING_FETCH = null
    DURING_FETCH_AFTER_SNAPSHOT = null
  })

  it('bounds the recurring refresh instead of pulling the whole transcript', async () => {
    const store = pagedBack(120)
    await store.dispatch(refreshSlot(SLOT) as never)
    // The defect signal: a `limit` is present at all, and it is not the corpus.
    expect(limits()).toEqual([120])
    expect(120).toBeLessThan(TOTAL)
  })

  it('asks for at least PANE_HYDRATE_LIMIT on a slot holding only a few rows', async () => {
    HISTORY = rows(6)
    const store = makeStore({ messages: HISTORY.slice(0, 3), slotHasMore: false })
    await store.dispatch(refreshSlot(SLOT) as never)
    expect(limits()).toEqual([PANE_HYDRATE_LIMIT])
    // The floor is a floor, not a cap: it can only ask for MORE than the view
    // holds, so it can never be the thing that truncates.
    expect(PANE_HYDRATE_LIMIT).toBeGreaterThan(3)
    expect(store.getState().chat.messages).toHaveLength(6)
  })

  it('refreshes a paged-back view at ITS count, never at the floor', async () => {
    const held = 180
    const store = pagedBack(held)
    await store.dispatch(refreshSlot(SLOT) as never)
    expect(limits()).toEqual([held])
    expect(limits()).not.toEqual([PANE_HYDRATE_LIMIT])
    // History the user paged in survives the in-place replacement.
    const after = store.getState().chat.messages
    expect(after).toHaveLength(held)
    expect(after[0].content).toBe(`m${TOTAL - held}`)
    expect(after.at(-1)?.content).toBe(`m${TOTAL - 1}`)
  })

  it('leaves hasMore and the older cursor exactly where the paged-back view had them', async () => {
    const held = 180
    const store = pagedBack(held)
    const before = store.getState().chat
    await store.dispatch(refreshSlot(SLOT) as never)
    const after = store.getState().chat
    // A wrong `hasMore` either hides the "load older" affordance or spins on a
    // cursor that never advances, so both fields are pinned, not just the flag.
    expect({ hasMore: after.slotHasMore, oldest: after.slotOldestIndex })
      .toEqual({ hasMore: before.slotHasMore, oldest: before.slotOldestIndex })
    expect(after.slotHasMore).toBe(true)
  })

  it('reports no older page once the count-matched refresh covers the corpus', async () => {
    const store = pagedBack(TOTAL)
    await store.dispatch(refreshSlot(SLOT) as never)
    const after = store.getState().chat
    expect({ limit: limits()[0], hasMore: after.slotHasMore, oldest: after.slotOldestIndex })
      .toEqual({ limit: TOTAL, hasMore: false, oldest: 0 })
  })

  it('clamps to the handler ceiling and keeps the rows above the page', async () => {
    // The handler clamps `limit` to 500, so a count-matched request above it
    // would be answered SHORT of the view it matched. Ask for exactly the
    // ceiling instead: the page's oldest row is in the view, so the reducer
    // keeps the 120 rows above it and nothing shrinks -- one request, bounded.
    HISTORY = rows(REFRESH_LIMIT_CEILING + 120)
    const store = makeStore({
      messages: HISTORY.slice(),
      slotHasMore: false,
      slotCursorKey: SLOT,
    })
    await store.dispatch(refreshSlot(SLOT) as never)
    expect(limits()).toEqual([REFRESH_LIMIT_CEILING])
    expect(store.getState().chat.messages).toHaveLength(REFRESH_LIMIT_CEILING + 120)
    expect(store.getState().chat.messages[0].content).toBe('m0')
    expect(REFRESH_LIMIT_CEILING).toBe(SERVER_CLAMP)
  })

  it('asks for the floor when the view holds nothing to count-match against', async () => {
    // A refresh on an empty view (reconnect after clearMessages, a refresh
    // racing slot activation) has no scrollback to protect, so it takes one
    // page and hands `loadOlderMessages` the cursor for the rest.
    const store = makeStore({ messages: [] })
    await store.dispatch(refreshSlot(SLOT) as never)
    expect(limits()).toEqual([PANE_HYDRATE_LIMIT])
    const after = store.getState().chat
    expect(after.messages).toHaveLength(PANE_HYDRATE_LIMIT)
    expect(after.messages.at(-1)?.content).toBe(`m${TOTAL - 1}`)
    expect({ hasMore: after.slotHasMore, oldest: after.slotOldestIndex })
      .toEqual({ hasMore: true, oldest: TOTAL - PANE_HYDRATE_LIMIT })
  })

  it('count-matches a STREAMING view too, since the handler collapses before it slices', async () => {
    RUNNING = true
    const held = 140
    const store = pagedBack(held)
    await store.dispatch(refreshSlot(SLOT) as never)
    expect(limits()).toEqual([held])
    expect(store.getState().chat.messages).toHaveLength(held)
  })

  // The window a count-matched limit returns SLIDES when the server grew while
  // this client was away -- which is exactly the reconnect this refresh exists to
  // recover from. Matching the COUNT preserves the row count, not the row
  // identities, so the reducer has to keep the head that falls out.
  describe('a slid window must not drop the head', () => {
    it('keeps the loaded oldest rows when the server gained messages during the gap', async () => {
      const held = 180
      const store = pagedBack(held)
      const oldestBefore = store.getState().chat.messages[0].content
      // Five messages landed while the socket was down.
      HISTORY = [...HISTORY, ...rows(5, TOTAL)]

      await store.dispatch(refreshSlot(SLOT) as never)

      const after = store.getState().chat.messages
      // The page began 5 rows NEWER than the view's oldest row, so a wholesale
      // assignment would have deleted those 5.
      expect(after[0].content).toBe(oldestBefore)
      expect(after.map(m => m.content)).toContain(`m${TOTAL + 4}`)
      expect(after).toHaveLength(held + 5)
    })

    it('shifts the older cursor by the head it kept, so "load older" is not a dead click', async () => {
      const held = 180
      const store = pagedBack(held)
      const oldestIndexBefore = store.getState().chat.slotOldestIndex
      HISTORY = [...HISTORY, ...rows(5, TOTAL)]

      await store.dispatch(refreshSlot(SLOT) as never)

      const after = store.getState().chat
      // The page's own cursor was 125 (305 - 180); the 5 kept rows sit below it,
      // so the cursor must come back down to where the view actually starts.
      expect({ hasMore: after.slotHasMore, oldest: after.slotOldestIndex })
        .toEqual({ hasMore: true, oldest: oldestIndexBefore })
    })

    it('reports no older page when the kept head proves the window is complete', async () => {
      // The view holds the whole corpus, then 5 rows land. The page covers the
      // newest `held`, the head covers the rest — together, everything.
      const store = pagedBack(TOTAL)
      HISTORY = [...HISTORY, ...rows(5, TOTAL)]

      await store.dispatch(refreshSlot(SLOT) as never)

      const after = store.getState().chat
      expect(after.messages).toHaveLength(TOTAL + 5)
      expect({ hasMore: after.slotHasMore, oldest: after.slotOldestIndex })
        .toEqual({ hasMore: false, oldest: 0 })
    })

    it('walks older when the gap slid the page CLEAR of the view', async () => {
      // The server gained more rows than the view holds, so the most-recent-N page
      // and the view are fully disjoint: the page carries no row the reducer can
      // cut at, and a wholesale assignment would drop the entire loaded window.
      const held = 180
      const store = pagedBack(held)
      const oldestBefore = store.getState().chat.messages[0].content
      HISTORY = [...HISTORY, ...rows(held + 20, TOTAL)]

      await store.dispatch(refreshSlot(SLOT) as never)

      // First the count-matched request, then the walk older in clamp-sized pages.
      expectWalkedFrom(held)
      const after = store.getState().chat
      expect(after.messages).toHaveLength(TOTAL + held + 20)
      expect(after.messages[0].content).toBe('m0')
      // Nothing the view held was dropped, and no hole was spliced into it.
      expect(after.messages.map(m => m.content)).toContain(oldestBefore)
      expect(after.slotHasMore).toBe(false)
    })

    it('walks to the start when the view carries no server row identity', async () => {
      // `olderHeadAbovePage` cuts on `meta.mid` only (two rows can share a `ts`, so a
      // ts match can cut at the wrong row) and declines without one. A view of rows
      // carrying none has a server span of zero, so there is no count to match and
      // the bound is skipped outright — one fetch, not a bounded one thrown away.
      HISTORY = rows(TOTAL).map(({ meta: _meta, ...rest }) => rest) as typeof HISTORY
      const store = pagedBack(180)

      await store.dispatch(refreshSlot(SLOT) as never)

      expectWalkedFrom(PANE_HYDRATE_LIMIT)
      expect(store.getState().chat.messages).toHaveLength(TOTAL)
    })

    it('does NOT retry when the page already reaches the start of history', async () => {
      // The floor asks for 50 against a 30-row transcript, so the page IS the whole
      // history and `hasMore` is false. Nothing can sit above it, so no anchor is
      // needed and a retry would be a wasted round trip.
      HISTORY = rows(30)
      const store = makeStore({ messages: HISTORY.slice(), slotHasMore: false })

      await store.dispatch(refreshSlot(SLOT) as never)

      expect(limits()).toEqual([PANE_HYDRATE_LIMIT])
      expect(store.getState().chat.messages).toHaveLength(30)
    })
  })

  // The limit reaches a handler that applies it to SERVER rows, so the count has to
  // be the view's server span. `state.messages` also carries client-only rows, and
  // counting those over-requests: the page then starts BELOW the view's oldest row,
  // which is benign in itself but must not be mistaken for a slid window.
  describe('the count is the view\'s SERVER-row span', () => {
    /** A paged-back view with `extra` client-only rows interleaved — the shape any
     *  session with reasoning, an approval card or a queued bubble is in. */
    function withClientRows(held: number, extra: number) {
      const server = HISTORY.slice(TOTAL - held)
      const clientOnly = Array.from({ length: extra }, (_, i) => ({
        role: 'thinking', content: `reasoning ${i}`, cls: 'msg',
        ts: new Date(Date.UTC(2026, 0, 1, 0, 0, TOTAL - held + i)).toISOString(),
      }))
      return makeStore({
        messages: [...server.slice(0, 1), ...clientOnly, ...server.slice(1)],
        slotHasMore: true,
        slotOldestIndex: TOTAL - held,
        slotCursorKey: SLOT,
      })
    }

    it('does not count client-only rows toward the limit', async () => {
      const store = withClientRows(180, 12)
      expect(store.getState().chat.messages).toHaveLength(192)

      await store.dispatch(refreshSlot(SLOT) as never)

      // 180, the server span — not 192, the array length.
      expect(limits()).toEqual([180])
    })

    it('makes ONE request for a window holding reasoning, not a bounded one plus an unbounded retry', async () => {
      // The regression this pins: an inflated count drags the page below the view's
      // oldest row, the anchor check misses, and the thunk falls back to the
      // whole-transcript pull the bound exists to prevent — on every turn end of any
      // session carrying a reasoning block.
      const store = withClientRows(180, 12)

      await store.dispatch(refreshSlot(SLOT) as never)

      // No unbounded call in the list at all, and exactly one request total.
      expect(limits()).not.toContain(undefined)
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
    })

    it('keeps an id-less legacy prefix that sits below the oldest identified row', async () => {
      // MIXED history: the view holds durable rows carrying no `meta.mid` (written
      // before the backend stamped them) BELOW rows that do. `serverRows[0]` is then
      // the oldest IDENTIFIED row, not the view's oldest durable row, so the page can
      // legitimately begin above the legacy prefix. What preserves the prefix is the
      // head-keep: the page's oldest row IS in the view, so `olderHeadAbovePage` cuts
      // at it and everything above — the legacy rows included — is kept.
      const legacy = rows(20).map(({ meta: _meta, ...rest }) => rest) as typeof HISTORY
      const identified = rows(180, 20)
      HISTORY = [...legacy, ...identified]
      const store = makeStore({
        messages: [...legacy, ...identified],
        slotHasMore: false,
        slotCursorKey: SLOT,
      })

      await store.dispatch(refreshSlot(SLOT) as never)

      // Bounded to the 180 IDENTIFIED rows — the legacy rows are not counted.
      expect(limits()).toEqual([180])
      const after = store.getState().chat
      // Nothing was dropped: all 20 legacy rows plus all 180 identified ones.
      expect(after.messages).toHaveLength(200)
      expect(after.messages[0].content).toBe('m0')
      expect(after.messages[19].content).toBe('m19')
      expect(after.messages.at(-1)?.content).toBe('m199')
      // The kept head saturates the page's cursor, so nothing older is advertised.
      expect({ hasMore: after.slotHasMore, oldest: after.slotOldestIndex })
        .toEqual({ hasMore: false, oldest: 0 })
    })

    it('walks older when a mixed-history page slides clear of the identified rows', async () => {
      // The same mixed shape, but the server also gained more rows than the view's
      // identified span — so the page overlaps neither the legacy prefix nor the
      // identified rows, and only walking older can preserve the window.
      const legacy = rows(20).map(({ meta: _meta, ...rest }) => rest) as typeof HISTORY
      const identified = rows(180, 20)
      HISTORY = [...legacy, ...identified]
      const store = makeStore({
        messages: [...legacy, ...identified],
        slotHasMore: false,
        slotCursorKey: SLOT,
      })
      HISTORY = [...HISTORY, ...rows(200, 200)]

      await store.dispatch(refreshSlot(SLOT) as never)

      expectWalkedFrom(180)
      const after = store.getState().chat
      expect(after.messages).toHaveLength(400)
      expect(after.messages[0].content).toBe('m0')
    })

    it('refuses a DUPLICATE anchor id rather than cutting at the wrong occurrence', async () => {
      // `meta.mid` on an inbound message is caller-supplied, so a client can post the
      // same id twice. A membership test (`Set.has` / `Array.some`) then answers
      // "SOME row carries this id" and the page reads as spanning the view while the
      // matching occurrence is a NEWER row -- the reducer cuts there and deletes the
      // history above it. The anchor must name exactly ONE row on each side.
      const dup = 'mid-shared'
      const held = 180
      const oldest = TOTAL - held
      // The view's OLDEST row and a much newer row share an id.
      const viewRows = HISTORY.slice(oldest).map((r, i) =>
        i === 0 || i === held - 20 ? { ...r, meta: { mid: dup } } : r,
      )
      HISTORY = HISTORY.map((r, i) =>
        i === oldest || i === oldest + held - 20 ? { ...r, meta: { mid: dup } } : r,
      )
      const store = makeStore({
        messages: viewRows,
        slotHasMore: true,
        slotOldestIndex: oldest,
        slotCursorKey: SLOT,
      })

      await store.dispatch(refreshSlot(SLOT) as never)

      // Declined the ambiguous anchor and walked older instead of trusting it.
      expectWalkedFrom(held)
      const after = store.getState().chat
      expect(after.messages).toHaveLength(TOTAL)
      expect(after.messages[0].content).toBe('m0')
    })

    it('refuses an anchor whose two rows disagree on ts, even when the id is unique', async () => {
      // Unique on both sides is not sufficient: two DIFFERENT rows can carry the same
      // caller-supplied id once each. The ts agreement is what makes the id a
      // reference to one row rather than a coincidence.
      const held = 180
      const oldest = TOTAL - held
      const viewRows = HISTORY.slice(oldest).map((r, i) =>
        i === 0 ? { ...r, ts: '2020-01-01T00:00:00Z' } : r,
      )
      const store = makeStore({
        messages: viewRows,
        slotHasMore: true,
        slotOldestIndex: oldest,
        slotCursorKey: SLOT,
      })

      await store.dispatch(refreshSlot(SLOT) as never)

      expectWalkedFrom(held)
      expect(store.getState().chat.messages).toHaveLength(TOTAL)
    })

    it('does NOT retry when the floor over-requests into a SUPERSET of the view', async () => {
      // A near-empty view against a long transcript: the floor asks for 50 where the
      // view holds 3, so the page begins below the view's oldest row. Its oldest row
      // is therefore absent from the view — but the page SPANS the view, and a
      // superset cannot lose anything, so a retry would be a wasted round trip.
      const store = makeStore({
        messages: HISTORY.slice(TOTAL - 3),
        slotHasMore: true,
        slotOldestIndex: TOTAL - 3,
        slotCursorKey: SLOT,
      })

      await store.dispatch(refreshSlot(SLOT) as never)

      expect(limits()).toEqual([PANE_HYDRATE_LIMIT])
      const after = store.getState().chat
      expect(after.messages).toHaveLength(PANE_HYDRATE_LIMIT)
      expect(after.slotHasMore).toBe(true)
    })
  })

  // `olderHeadAbovePage` is shared by switchSlot.fulfilled, refreshSlot.fulfilled and
  // warmSlotCache.fulfilled, so the anchor invariant lives THERE rather than at each
  // caller. These pin the two directions of the shared cut through the refresh path.
  describe('the shared cut refuses an ambiguous anchor', () => {
    it('drops no history when the page-oldest id names two rows in the view', async () => {
      // A bare findIndex cut at the FIRST row carrying the id, which for a
      // caller-repeated `mid` is an OLDER row than the one meant -- so the head was
      // measured to the wrong place and the rows between were lost.
      const dup = 'mid-repeated'
      const held = 180
      const oldest = TOTAL - held
      // The page's oldest row's id also appears much later in the view.
      HISTORY = HISTORY.map((r, i) =>
        i === oldest || i === oldest + 40 ? { ...r, meta: { mid: dup } } : r,
      )
      const store = makeStore({
        messages: HISTORY.slice(oldest),
        slotHasMore: true,
        slotOldestIndex: oldest,
        slotCursorKey: SLOT,
      })

      await store.dispatch(refreshSlot(SLOT) as never)

      // Declined at the thunk, so the reducer never sees a page it cannot cut.
      expectWalkedFrom(held)
      const after = store.getState().chat
      expect(after.messages).toHaveLength(TOTAL)
      expect(after.messages[0].content).toBe('m0')
    })

    it('takes a ts-less anchor at the reducer\'s own rule, without a wider read', async () => {
      // The thunk's strict check declines a corpus with no `ts` anywhere, and the
      // walk then judges the SAME page by the rule the reducer will apply -- which
      // accepts two ts-less rows that do not contradict each other (pinned in
      // chatSlice.boundedRefetchShrink.test.ts). The page IS the view, so the
      // reducer's cut keeps everything and no older page is needed: one request,
      // nothing lost. Walking to the start here would re-read a legacy transcript
      // on every refresh, which is the cost this file exists to remove.
      const held = 180
      const oldest = TOTAL - held
      const stripTs = (r: Row) => {
        const { ts: _ts, ...rest } = r
        return rest as Row
      }
      HISTORY = HISTORY.map(stripTs)
      const store = makeStore({
        messages: HISTORY.slice(oldest).map(stripTs),
        slotHasMore: true,
        slotOldestIndex: oldest,
        slotCursorKey: SLOT,
      })

      await store.dispatch(refreshSlot(SLOT) as never)

      expect(limits()).toEqual([held])
      const after = store.getState().chat
      expect(after.messages).toHaveLength(held)
      expect(after.messages[0].content).toBe(`m${oldest}`)
      expect(after.messages.at(-1)?.content).toBe(`m${TOTAL - 1}`)
      expect({ hasMore: after.slotHasMore, oldestIndex: after.slotOldestIndex })
        .toEqual({ hasMore: true, oldestIndex: oldest })
    })
  })

  /** The FLOOR over-request meeting durable rows that carry no id.
   *
   *  At `want === held` the page cannot strand a durable row -- the span check only
   *  passes when a page of exactly `held` rows holds all `held` identified ones,
   *  leaving no room for an unidentified row to be its oldest. The floor breaks that
   *  arithmetic: it asks for MORE than the view's identified span, so the extra rows
   *  come from older history, and with legacy rows there the page's oldest row is one
   *  the `mid`-keyed cut cannot anchor. The reducer then keeps no head while the view
   *  holds rows above the page -- rows in no page and no head.
   *
   *  These pin the floor's decline. The count-matched cases above stay bounded. */
  describe('the floor meeting durable rows that carry no id', () => {
    /** `total` rows where only the newest `identified` carry a `meta.mid`. */
    const legacyTail = (total: number, identified: number): Row[] =>
      rows(total).map((r, i) => {
        if (i >= total - identified) return r
        const { meta: _meta, ...rest } = r
        return rest as Row
      })

    it('walks older when the floor would reach past the identified rows', async () => {
      // 20 identified rows, so the floor asks for 50 -- a page holding all 20 AND 30
      // older legacy rows. The span check passes on the oldest identified row while the
      // page's OWN oldest row carries no id, so the cut keeps nothing and a 300-row
      // transcript would collapse to the 50-row page.
      const identified = 20
      expect(identified).toBeLessThan(PANE_HYDRATE_LIMIT)
      HISTORY = legacyTail(TOTAL, identified)
      const store = makeStore({
        messages: HISTORY.slice(),
        slotHasMore: false,
        slotOldestIndex: 0,
        slotCursorKey: SLOT,
      })

      await store.dispatch(refreshSlot(SLOT) as never)

      expectWalkedFrom(PANE_HYDRATE_LIMIT)
      const after = store.getState().chat.messages
      expect(after).toHaveLength(TOTAL)
      expect(after[0].content).toBe('m0')
      expect(after.at(-1)?.content).toBe(`m${TOTAL - 1}`)
    })

    it('declines on a single unidentified durable row, not just a large legacy block', async () => {
      // The floor cannot retain ONE unanchorable row any more than hundreds, so a
      // threshold would leave exactly that row droppable. The view here is the newest
      // 20 identified rows plus the one legacy row directly below them.
      const identified = 20
      HISTORY = legacyTail(TOTAL, identified)
      const oldest = TOTAL - identified - 1
      const store = makeStore({
        messages: HISTORY.slice(oldest),
        slotHasMore: true,
        slotOldestIndex: oldest,
        slotCursorKey: SLOT,
      })

      await store.dispatch(refreshSlot(SLOT) as never)

      expectWalkedFrom(PANE_HYDRATE_LIMIT)
      expect(store.getState().chat.messages).toHaveLength(TOTAL)
    })

    it('still bounds when the floor is not in play, whatever the identities are', async () => {
      // `held >= PANE_HYDRATE_LIMIT`, so `want === held` and the arithmetic that makes
      // the count-matched request safe applies -- legacy rows do not disable it.
      const identified = 180
      HISTORY = legacyTail(TOTAL, identified)
      const store = makeStore({
        messages: HISTORY.slice(),
        slotHasMore: false,
        slotOldestIndex: 0,
        slotCursorKey: SLOT,
      })

      await store.dispatch(refreshSlot(SLOT) as never)

      expect(limits()).toEqual([identified])
      expect(store.getState().chat.messages).toHaveLength(TOTAL)
    })

    it('still bounds a view whose only id-less rows are client-only', async () => {
      // `thinking` / `queued` carry no `mid` either and are the common case -- if they
      // tripped the guard, #4690 would be unfixed in every live session. Not durable,
      // so they do not count. Held below the floor to put the floor genuinely in play.
      const held = 20
      const oldest = TOTAL - held
      const store = makeStore({
        messages: [
          ...HISTORY.slice(oldest),
          { role: 'thinking', content: 'thinking...', cls: 'msg' } as unknown as Row,
          { role: 'queued', content: 'next up', cls: 'msg' } as unknown as Row,
        ],
        slotHasMore: true,
        slotOldestIndex: oldest,
        slotCursorKey: SLOT,
      })

      await store.dispatch(refreshSlot(SLOT) as never)

      expect(limits()).toEqual([PANE_HYDRATE_LIMIT])
    })
  })

  /** A client-only row that happens to carry a `mid`.
   *
   *  `permission` cards are not persisted, so the handler's disk slice never contains
   *  one -- but they can still arrive carrying a `meta.mid`. Counting one inflates
   *  `held`, and the damage is not the over-request: it makes `want === held` true
   *  while the DURABLE span is smaller, which bypasses the floor guard whose argument
   *  is that a page of exactly `held` rows has no room to hide an unidentified row.
   *  With legacy rows present that is the scrollback loss again, reached through the
   *  count rather than the floor. */
  describe('client-only rows carrying an id', () => {
    it('does not count a permission card toward the bound', async () => {
      const held = 120
      const oldest = TOTAL - held
      const store = makeStore({
        messages: [
          ...HISTORY.slice(oldest),
          // Not persisted, but stamped.
          {
            role: 'permission',
            content: 'allow?',
            cls: 'msg',
            meta: { mid: 'mid-perm-1' },
          } as unknown as Row,
        ],
        slotHasMore: true,
        slotOldestIndex: oldest,
        slotCursorKey: SLOT,
      })

      await store.dispatch(refreshSlot(SLOT) as never)

      // 120, not 121: the card is not a row the disk slice can return.
      expect(limits()).toEqual([held])
    })

    it('does not let stamped permission cards inflate held past the floor guard', async () => {
      // 20 durable identified rows plus 30 stamped permission cards. Counting the
      // cards makes held 50, so `want === held` and the floor guard is skipped --
      // while the real durable span is 20 and the page of 50 reaches back into the
      // legacy rows, whose oldest the `mid`-keyed cut cannot anchor.
      const identified = 20
      const legacy = TOTAL - identified
      HISTORY = rows(TOTAL).map((r, i) => {
        if (i >= legacy) return r
        const { meta: _meta, ...rest } = r
        return rest as Row
      })
      const cards = Array.from({ length: 30 }, (_, i) => ({
        role: 'permission',
        content: `allow ${i}?`,
        cls: 'msg',
        meta: { mid: `mid-perm-${i}` },
      })) as unknown as Row[]
      const store = makeStore({
        messages: [...HISTORY.slice(), ...cards],
        slotHasMore: false,
        slotOldestIndex: 0,
        slotCursorKey: SLOT,
      })

      await store.dispatch(refreshSlot(SLOT) as never)

      // held is 20, so the floor genuinely over-requests and the guard fires.
      expectWalkedFrom(PANE_HYDRATE_LIMIT)
      const after = store.getState().chat.messages
      expect(after.filter(m => m.role !== 'permission')).toHaveLength(TOTAL)
    })
  })

  /** The view can change DURING the fetch.
   *
   *  `view` is read before the await so the limit can be sized from it, but
   *  `loadOlderMessages` can resolve inside that await. The rows it prepends are
   *  precisely the scrollback a wrong decision strands, and they can also turn an
   *  anchor that was unique in the old view into an AMBIGUOUS one. So the page is
   *  judged against the view as it is when the page arrives, not against the snapshot
   *  the limit came from. */
  describe('a view that changed while the fetch was in flight', () => {
    it('keeps rows that load-earlier prepended during the await', async () => {
      const held = 120
      const oldest = TOTAL - held
      const store = pagedBack(held)
      // Mid-fetch, "load earlier" lands: the view grows to the whole corpus.
      DURING_FETCH = () => {
        store.dispatch(replaceMessages(HISTORY.slice()))
      }

      await store.dispatch(refreshSlot(SLOT) as never)

      const after = store.getState().chat.messages
      // The page covers only the newest 120, so accepting it against the GROWN view
      // would drop the rows that just arrived.
      expect(after).toHaveLength(TOTAL)
      expect(after[0].content).toBe('m0')
      expect(after.at(-1)?.content).toBe(`m${TOTAL - 1}`)
      expect(oldest).toBeGreaterThan(0)
    })

    it('walks older when the arriving rows make the anchor ambiguous', async () => {
      // The page's own oldest row is the anchor. If the rows landing mid-fetch carry a
      // second copy of that id, the anchor no longer names one row and the cut cannot
      // be trusted -- judged on the OLD view it still looks unique.
      const held = 120
      const oldest = TOTAL - held
      const dup = HISTORY[oldest].meta?.mid
      const store = pagedBack(held)
      DURING_FETCH = () => {
        const grown = HISTORY.slice().map((r, i) =>
          i === oldest - 30 ? { ...r, meta: { mid: dup as string } } : r,
        )
        store.dispatch(replaceMessages(grown))
      }

      await store.dispatch(refreshSlot(SLOT) as never)

      expectWalkedFrom(held)
      expect(store.getState().chat.messages).toHaveLength(TOTAL)
    })

    it('still bounds normally when nothing lands during the await', async () => {
      // The re-read must not become a blanket decline: an undisturbed refresh keeps
      // the single bounded request, which is the whole point of #4690.
      const held = 120
      const store = pagedBack(held)

      await store.dispatch(refreshSlot(SLOT) as never)

      expect(limits()).toEqual([held])
      expect(store.getState().chat.messages).toHaveLength(held)
    })

    it('declines an acceptable first page when a live chunk lands during the fetch', async () => {
      // The page reaches the start of history, so it takes the one-page fast path.
      // A live chunk reduced after dispatch is newer than that snapshot and must win.
      HISTORY = rows(30)
      RUNNING = true
      const store = makeStore({
        messages: [
          ...HISTORY,
          { role: 'streaming', content: 'partial', cls: 'msg msg-a', rawText: 'partial' },
        ],
        slotHasMore: false,
        slotOldestIndex: 0,
        slotCursorKey: SLOT,
      })
      DURING_FETCH = () => {
        store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' more' } as never))
      }

      const result = await store.dispatch(refreshSlot(SLOT) as never) as { payload: unknown }

      expect(limits()).toEqual([PANE_HYDRATE_LIMIT, PANE_HYDRATE_LIMIT])
      expect(result.payload).toBeNull()
      expect(store.getState().chat.messages.find(m => m.role === 'streaming')?.content)
        .toBe('partial more')
    })

    it('keeps a row streamed in while the walk was paging older', async () => {
      // Disjoint page -> walk. A live chunk lands during the walk's SECOND request,
      // after the first page was read, so it is in the view but in no walked page.
      const held = 180
      const store = pagedBack(held)
      HISTORY = [...HISTORY, ...rows(held + 20, TOTAL)]
      DURING_FETCH = () => {
        DURING_FETCH = () => {
          store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'streamed-live' } as never))
        }
      }

      await store.dispatch(refreshSlot(SLOT) as never)

      expect(limits().length).toBeGreaterThan(1)
      const after = store.getState().chat.messages
      expect(after.at(-1)?.content).toBe('streamed-live')
      expect(after).toHaveLength(held + 1)
    })

    it('keeps a chunk that lands on a streaming row sitting ABOVE a queued bubble', async () => {
      // A message queued mid-turn renders below the streaming reply, so the reply is
      // not the tail. A chunk still lands on it in place; the walked payload holds
      // none of that text, so accepting it would erase what the user just read.
      const held = 180
      const base = pagedBack(held).getState().chat.messages
      const streaming = { role: 'streaming', content: 'partial', cls: 'msg msg-a', rawText: 'partial' }
      const queued = { role: 'user', content: 'queued-next', cls: 'msg', meta: { queued: true } }
      const store = makeStore({
        messages: [...base, streaming, queued],
        slotHasMore: true,
        slotOldestIndex: TOTAL - held,
        slotCursorKey: SLOT,
      })
      HISTORY = [...HISTORY, ...rows(held + 20, TOTAL)]
      DURING_FETCH = () => {
        DURING_FETCH = () => {
          store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' more' } as never))
        }
      }

      await store.dispatch(refreshSlot(SLOT) as never)

      const after = store.getState().chat.messages
      expect(after.find(m => m.role === 'streaming')?.content).toBe('partial more')
      expect(after.at(-1)?.content).toBe('queued-next')
    })

    it('retries a passive frame race and restores canonical history plus the frame', async () => {
      HISTORY = rows(2)
      const passive: Row = {
        role: 'assistant',
        content: 'The turn has been quiet for five minutes.',
        cls: 'msg msg-a',
        ts: '2026-01-01T00:00:03.000Z',
        meta: {
          mid: 'mid-passive-notice',
          kind: 'compaction',
          notice: 'stuck_turn',
        },
      }
      const store = makeStore({ messages: [HISTORY[0]], slotHasMore: false })
      DURING_FETCH_AFTER_SNAPSHOT = () => {
        HISTORY = [...HISTORY, passive]
        store.dispatch(sseChatMessage({
          slot: SLOT,
          ...passive,
          kind: 'compaction',
          meta: { ...passive.meta, kind: 'compaction', notice: 'stuck_turn' },
        } as never))
      }

      await store.dispatch(refreshSlot(SLOT) as never)

      expect(limits()).toEqual([PANE_HYDRATE_LIMIT, PANE_HYDRATE_LIMIT])
      expect(store.getState().chat.messages.map(message => message.content)).toEqual([
        'm0',
        'm1',
        passive.content,
      ])
      expect(store.getState().chat.messages.at(-1)?.meta).toEqual(expect.objectContaining({
        kind: 'compaction', notice: 'stuck_turn',
      }))
    })

    it('retries a live chunk race without duplicating or rewinding the canonical stream', async () => {
      RUNNING = true
      const anchor = rows(1)[0]
      const stream: Row = {
        role: 'streaming', content: 'server partial', cls: 'msg msg-a',
        ts: '2026-01-01T00:00:02.000Z',
      }
      HISTORY = [anchor, stream]
      const store = makeStore({ messages: HISTORY.slice(), slotHasMore: false })
      DURING_FETCH_AFTER_SNAPSHOT = () => {
        HISTORY = [anchor, {
          ...stream, content: 'server partial more', seq: 2, gen: 'g-live',
        } as Row]
        store.dispatch(sseChatMessage({
          slot: SLOT, role: 'chunk', content: ' more', seq: 2, gen: 'g-live',
        } as never))
      }

      await store.dispatch(refreshSlot(SLOT) as never)

      expect(limits()).toEqual([PANE_HYDRATE_LIMIT, PANE_HYDRATE_LIMIT])
      expect(store.getState().chat.messages.filter(message => message.role === 'streaming'))
        .toEqual([expect.objectContaining({ content: 'server partial more', seq: 2, gen: 'g-live' })])
    })

    it('declines identical open bytes from a different segment ordinal', async () => {
      RUNNING = true
      const anchor = rows(1)[0]
      const priorSegment: Row = {
        role: 'assistant', content: 'Earlier segment.', cls: 'msg msg-a',
        ts: '2026-01-01T00:00:01.000Z',
      }
      const open: Row = {
        role: 'streaming', content: 'identical bytes', cls: 'msg msg-a',
        ts: '2026-01-01T00:00:02.000Z', seq: 1,
      }
      HISTORY = [anchor, priorSegment, open]
      const store = makeStore({ messages: HISTORY.slice(), slotHasMore: false })
      DURING_FETCH_AFTER_SNAPSHOT = () => {
        HISTORY = [anchor, { ...open, seq: 2 }]
        store.dispatch(sseChatMessage({
          slot: SLOT, role: 'chunk', content: '', seq: 2,
        } as never))
      }

      const result = await store.dispatch(refreshSlot(SLOT) as never) as { payload: unknown }

      expect(result.payload).toBeNull()
      expect(store.getState().chat.messages.map(message => message.content)).toEqual([
        anchor.content, priorSegment.content, open.content,
      ])
    })

    it.each(['current view', 'fetched page'] as const)(
      'declines a mid duplicated in the %s',
      async (duplicateSide) => {
        const duplicateMid = 'mid-duplicate-retry'
        const first: Row = {
          role: 'assistant', content: 'first copy', cls: 'msg msg-a',
          ts: '2026-01-01T00:00:01.000Z', meta: { mid: duplicateMid },
        }
        const second: Row = {
          role: 'assistant', content: 'second copy', cls: 'msg msg-a',
          ts: '2026-01-01T00:00:02.000Z', meta: { mid: duplicateMid },
        }
        const notice: Row = {
          role: 'assistant', content: 'retry notice', cls: 'msg msg-a',
          ts: '2026-01-01T00:00:03.000Z',
          meta: { mid: 'mid-duplicate-race', kind: 'compaction' },
        }
        HISTORY = [first]
        const store = makeStore({
          messages: duplicateSide === 'current view' ? [first, second] : [first],
          slotHasMore: false,
        })
        DURING_FETCH_AFTER_SNAPSHOT = () => {
          HISTORY = duplicateSide === 'fetched page'
            ? [first, second, notice]
            : [first, notice]
          store.dispatch(sseChatMessage({ slot: SLOT, ...notice } as never))
        }

        const result = await store.dispatch(refreshSlot(SLOT) as never) as { payload: unknown }

        expect(result.payload).toBeNull()
      },
    )

    it('accepts byte-different redaction when one unique mid identifies the row', async () => {
      const anchor = rows(1)[0]
      const local: Row = {
        role: 'assistant', content: 'Token AKIA-LIVE was removed.', cls: 'msg msg-a',
        ts: '2026-01-01T00:00:01.000Z', meta: { mid: 'mid-redacted-retry' },
      }
      const redacted: Row = { ...local, content: 'Token [redacted] was removed.' }
      HISTORY = [anchor]
      const store = makeStore({ messages: [anchor], slotHasMore: false })
      DURING_FETCH_AFTER_SNAPSHOT = () => {
        HISTORY = [anchor, redacted]
        store.dispatch(sseChatMessage({ slot: SLOT, ...local } as never))
      }

      const result = await store.dispatch(refreshSlot(SLOT) as never) as { payload: unknown }

      expect(result.payload).not.toBeNull()
      expect(store.getState().chat.messages.at(-1)?.content).toBe(redacted.content)
    })

    it('declines newer same-text stream sequence from an incompatible generation', async () => {
      RUNNING = true
      const anchor = rows(1)[0]
      const open: Row = {
        role: 'streaming', content: 'same text', cls: 'msg msg-a',
        ts: '2026-01-01T00:00:01.000Z', seq: 1,
      }
      HISTORY = [anchor, open]
      const store = makeStore({ messages: HISTORY.slice(), slotHasMore: false })
      DURING_FETCH_AFTER_SNAPSHOT = () => {
        HISTORY = [anchor, { ...open, seq: 3, gen: 'g-other' }]
        store.dispatch(sseChatMessage({
          slot: SLOT, role: 'chunk', content: '', seq: 2,
        } as never))
      }

      const result = await store.dispatch(refreshSlot(SLOT) as never) as { payload: unknown }

      expect(result.payload).toBeNull()
      expect(store.getState().chat.messages.at(-1)).toEqual(expect.objectContaining({
        role: 'streaming', content: open.content,
      }))
      expect(store.getState().chat.lastChunkSeq).toBe(2)
      expect(store.getState().chat.lastChunkGen).toBeUndefined()
    })

    it('covers a mid-less finalized race only by the same shared anchor and ordinal', async () => {
      RUNNING = true
      const anchor = rows(1)[0]
      const open: Row = {
        role: 'streaming', content: 'local unredacted bytes', cls: 'msg msg-a',
        ts: '2026-01-01T00:00:01.000Z', seq: 1, gen: 'g-finalized',
      }
      const canonical: Row = {
        role: 'assistant', content: 'canonical redacted bytes', cls: 'msg msg-a',
        ts: '2026-01-01T00:00:02.000Z',
      }
      HISTORY = [anchor]
      const store = makeStore({ messages: [anchor, open], slotHasMore: false })
      DURING_FETCH_AFTER_SNAPSHOT = () => {
        HISTORY = [anchor, canonical]
        store.dispatch(sseChatMessage({
          slot: SLOT, role: '_segment', content: '', seq: 1, gen: 'g-finalized',
        } as never))
      }

      const result = await store.dispatch(refreshSlot(SLOT) as never) as { payload: unknown }

      expect(result.payload).not.toBeNull()
      expect(store.getState().chat.messages.at(-1)?.content).toBe(canonical.content)
    })

    it('does not reintroduce a placeholder removed by a rowless live boundary', async () => {
      RUNNING = true
      const anchor = rows(1)[0]
      const placeholder: Row = {
        role: 'streaming', content: '', cls: 'msg msg-a',
        ts: '2026-01-01T00:00:01.000Z',
      }
      HISTORY = [anchor, placeholder]
      const store = makeStore({ messages: HISTORY.slice(), slotHasMore: false })
      DURING_FETCH_AFTER_SNAPSHOT = () => {
        store.dispatch(sseChatMessage({ slot: SLOT, role: '_segment', content: '' } as never))
      }

      const result = await store.dispatch(refreshSlot(SLOT) as never) as { payload: unknown }

      expect(result.payload).toBeNull()
      expect(store.getState().chat.messages).toEqual([anchor])
    })

    it('stops after three changing reads and preserves the local live state', async () => {
      HISTORY = rows(1)
      const store = makeStore({ messages: HISTORY.slice(), slotHasMore: false })
      let frame = 0
      const churn = () => {
        frame += 1
        store.dispatch(sseChatMessage({
          slot: SLOT,
          role: 'assistant',
          content: `passive-${frame}`,
          meta: { mid: `mid-passive-${frame}`, kind: 'compaction' },
        } as never))
        DURING_FETCH_AFTER_SNAPSHOT = churn
      }
      DURING_FETCH_AFTER_SNAPSHOT = churn

      const result = await store.dispatch(refreshSlot(SLOT) as never) as { payload: unknown }

      expect(limits()).toEqual([
        PANE_HYDRATE_LIMIT,
        PANE_HYDRATE_LIMIT,
        PANE_HYDRATE_LIMIT,
      ])
      expect(limits()).not.toContain(undefined)
      expect(result.payload).toBeNull()
      expect(store.getState().chat.messages.map(message => message.content)).toEqual([
        'm0', 'passive-1', 'passive-2', 'passive-3',
      ])
    })

    it('declines when the active slot changes during a retry', async () => {
      HISTORY = rows(2)
      const store = makeStore({ messages: [HISTORY[0]], slotHasMore: false })
      DURING_FETCH_AFTER_SNAPSHOT = () => {
        store.dispatch(sseChatMessage({
          slot: SLOT,
          role: 'assistant',
          content: 'passive race',
          meta: { mid: 'mid-passive-race', kind: 'compaction' },
        } as never))
        DURING_FETCH_AFTER_SNAPSHOT = () => {
          store.dispatch({ type: 'chat/setActiveSlot', payload: 'other-slot' })
        }
      }

      const result = await store.dispatch(refreshSlot(SLOT) as never) as { payload: unknown }

      expect(limits()).toEqual([PANE_HYDRATE_LIMIT, PANE_HYDRATE_LIMIT])
      expect(result.payload).toBeNull()
      expect(store.getState().chat.activeSlot).toBe('other-slot')
    })

    it('still lands the walk when only a row ABOVE the tail changed during it', async () => {
      // An approval retiring mid-walk rewrites one earlier row: a new array, nothing
      // streamed. Declining here would drop every row the reconnect walked for.
      const held = 180
      const store = pagedBack(held)
      HISTORY = [...HISTORY, ...rows(held + 20, TOTAL)]
      DURING_FETCH = () => {
        DURING_FETCH = () => {
          const view = store.getState().chat.messages
          store.dispatch(replaceMessages(view.map((m, i) => (i === 3 ? { ...m } : m))))
        }
      }

      await store.dispatch(refreshSlot(SLOT) as never)

      expect(store.getState().chat.messages).toHaveLength(TOTAL + held + 20)
    })

    it('drops an older refresh payload that settles after a newer one applied', () => {
      // Two overlapping refreshes (a variant switch mid-walk dispatches the second)
      // can settle newest-first. The older payload carries rows read before the
      // switch and must not put the old variant back.
      const store = pagedBack(120)
      const payload = (seq: number, content: string) => ({
        key: SLOT, messages: [...HISTORY.slice(0, -1), { ...HISTORY[HISTORY.length - 1], content }],
        running: false, hasMore: false, nextBefore: 0, total: TOTAL, queue: [], stopping: false, refreshSeq: seq,
      })
      store.dispatch(refreshSlot.pending('r1', SLOT))
      store.dispatch(refreshSlot.pending('r2', SLOT))
      store.dispatch(refreshSlot.fulfilled(payload(2, 'switched-variant') as never, 'r2', SLOT))
      store.dispatch(refreshSlot.fulfilled(payload(1, 'old-variant') as never, 'r1', SLOT))

      expect(store.getState().chat.messages.at(-1)?.content).toBe('switched-variant')
    })

    it('fetches nothing more when the slot switched during the await', async () => {
      const store = pagedBack(120)
      DURING_FETCH = () => {
        store.dispatch({ type: 'chat/setActiveSlot', payload: 'other-slot' })
      }

      await store.dispatch(refreshSlot(SLOT) as never)

      // One bounded request went out before the switch; nothing is retried after it.
      expect(limits()).toEqual([120])
    })
  })

  it('still fetches nothing for a slot that is no longer active', async () => {
    const store = makeStore({ activeSlot: 'other', messages: HISTORY.slice() })
    await store.dispatch(refreshSlot(SLOT) as never)
    expect(api.chatSlotDetail).not.toHaveBeenCalled()
  })
})
