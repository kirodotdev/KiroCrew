/** `warmSlotCache` must bound its fetch the same way a pane's own query does.
 *
 *  The pane renders the store's per-slot array, but the earlier-messages marker
 *  keys off a `staleTime: Infinity` bounded query. An unbounded warm on every
 *  background `chat_done` replaced that array with the FULL history while the
 *  query still reported `has_more`, so the pane showed a row claiming missing
 *  messages it was already displaying. Bounding both paths keeps `has_more`
 *  consistent with what the pane holds.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import { api } from '../api/client'
import chatReducer, { OLDER_PAGE_LIMIT, PANE_HYDRATE_LIMIT, appendSlotMessage, clearSlotCache, hydrateSlotMessages, refreshSlot, switchSlot, warmSlotCache } from '../store/chatSlice'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'

vi.mock('../api/client')

const detail = { messages: [], running: false, has_more: true, total: 500, queue: [] }

function makeStore(activeSlot: string, extra: Record<string, unknown> = {}) {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...base, activeSlot, ...extra } },
  })
}

const msg = (content: string, ts: string, mid?: string) =>
  ({ role: 'assistant', content, cls: '', ts, ...(mid ? { meta: { mid } } : {}) })

describe('warmSlotCache hydrate bound', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue(detail)
  })

  it('bounds the background warm to the pane limit', async () => {
    const store = makeStore('active-slot')
    await store.dispatch(warmSlotCache('background-slot') as never)
    expect(api.chatSlotDetail).toHaveBeenCalledWith('background-slot', PANE_HYDRATE_LIMIT)
    expect(PANE_HYDRATE_LIMIT).toBeGreaterThan(0)
    expect(PANE_HYDRATE_LIMIT).toBeLessThanOrEqual(500)
  })

  it('does not fetch at all when the slot is already active', async () => {
    const store = makeStore('same-slot')
    await store.dispatch(warmSlotCache('same-slot') as never)
    expect(api.chatSlotDetail).not.toHaveBeenCalled()
  })

  // Control: a refresh replaces the active transcript in place, so a bound
  // would shrink history the user already paged in. switchSlot resets the
  // pane's cursor, so IT pages: one bounded first page, older pages on demand.
  it('bounds the in-place refresh to the open page while the slot open pages', async () => {
    const store = makeStore('active-slot')
    await store.dispatch(switchSlot('active-slot') as never)
    expect(api.chatSlotDetail).toHaveBeenLastCalledWith('active-slot', OLDER_PAGE_LIMIT)
    // The PANE bound must not reach the active slot: distinct constants, so a
    // future edit collapsing them cannot pass this file unnoticed.
    expect(OLDER_PAGE_LIMIT).not.toBe(PANE_HYDRATE_LIMIT)
    // refreshSlot is COUNT-MATCHED (see chatSlice.refreshSlotBound.test.ts,
    // upstream #6947): a view whose rows the thunk cannot identify declines
    // the bound, which fetchSlotDetail spells as the one-arg call.
    await store.dispatch(refreshSlot('active-slot') as never)
    expect((api.chatSlotDetail as unknown as { mock: { calls: unknown[][] } }).mock.calls.at(-1)?.[0]).toBe('active-slot')
  })

  // The pane's query is staleTime:Infinity, so a pane that mounted under the bound
  // caches has_more:false and hides its recovery row after a truncating warm.
  it('records the warm has_more so a truncating warm cannot hide the recovery row', async () => {
    const store = makeStore('active-slot')
    await store.dispatch(warmSlotCache('background-slot') as never)
    expect(store.getState().chat.slotPaneHasMore['background-slot']).toBe(true)
  })

  // The third hydrate path: switchSlot.pending caches the OUTGOING slot's full
  // array, so a stale `true` marked that whole transcript as truncated.
  it('carries the active slot\'s own has_more when caching it on switch-away', async () => {
    const store = makeStore('slot-a', {
      messages: [msg('one', '2026-08-13T09:00:00Z')],
      slotHasMore: false,
      slotPaneHasMore: { 'slot-a': true },
    })
    await store.dispatch(switchSlot('slot-b') as never)
    // The cached copy is the full active view, and it held everything, so the
    // marker must be false -- not the warm's leftover true.
    expect(store.getState().chat.slotPaneHasMore['slot-a']).toBe(false)
  })

  // Both directions: an active slot that really does have unloaded older history
  // must keep its marker, or switching away hides a row the pane still needs.
  it('keeps the marker when the active slot itself had more to load', async () => {
    const store = makeStore('slot-a', {
      messages: [msg('one', '2026-08-13T09:00:00Z')],
      slotHasMore: true,
      slotPaneHasMore: {},
    })
    await store.dispatch(switchSlot('slot-b') as never)
    expect(store.getState().chat.slotPaneHasMore['slot-a']).toBe(true)
  })

  // A warm is bounded, so replacing the array wholesale deletes scrollback under
  // a reader who scrolled up in a background pane.
  it('keeps older messages a pane already holds instead of shrinking to the warm', async () => {
    const older = msg('older head', '2026-08-13T08:00:00Z', 'm-older')
    const overlap = msg('warm oldest', '2026-08-13T09:00:00Z', 'm-overlap')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [overlap], has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [older, overlap] },
      slotPaneHasMore: { 'bg-slot': false },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot']
    expect(held.map(m => m.content)).toContain('older head')
    // The array is no longer the bounded warm, so the warm's has_more must not
    // overwrite the flag that describes what the pane actually holds.
    expect(store.getState().chat.slotPaneHasMore['bg-slot']).toBe(false)
  })

  // Same-ts rows are real (coarse clock, channel replay) and a ts cut matches the
  // EARLIER one, so the slice drops the later row from the middle of the array.
  it('declines to merge on a ts collision when the rows carry no mid', async () => {
    const head = msg('head', '2026-08-13T07:00:00Z')
    const twinA = msg('twin A', '2026-08-13T09:00:00Z')
    const twinB = msg('twin B', '2026-08-13T09:00:00Z')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [twinB], has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [head, twinA, twinB] },
      slotPaneHasMore: { 'bg-slot': false },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    // A ts cut would have produced ['head', 'twin B'] -- twin A deleted, and the
    // array passed off as a retained head. With no identity the longer prior wins,
    // so every row survives.
    expect(held).toEqual(['head', 'twin A', 'twin B'])
    expect(held).not.toEqual(['head', 'twin B'])
    // Prior kept, so the array is not the bounded warm and its has_more must not
    // overwrite the marker that describes what the pane holds.
    expect(store.getState().chat.slotPaneHasMore['bg-slot']).toBe(false)
  })

  // Streaming is not an exception (#10005, the same retirement #7916 made for
  // switchSlot): the handler collapses chunk runs BEFORE computing total and
  // slicing, so a bound never cut a raw row, and this warm fires on every
  // background chat_done -- unbounded, it re-pulled a ~3000-row Crew Members
  // DM on every reply.
  it('warms a streaming slot bounded', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue(detail)
    const store = makeStore('active-slot', { slotRun: { 'bg-slot': { state: 'streaming' } } })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(api.chatSlotDetail).toHaveBeenCalledWith('bg-slot', PANE_HYDRATE_LIMIT)
    expect((api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.every(c => typeof c[1] === 'number')).toBe(true)
  })

  // A pane that already holds rows used to force the warm unbounded (#6839), on the
  // fear that a fixed window could miss a small cache once the server had grown.
  // Whether the page can be STITCHED onto the cache is now verified after the
  // response instead: the window is sized to the durable rows the pane holds (never
  // below the pane bound), and only a page that anchors into neither the cache's
  // head nor its oldest row takes the unbounded retry.
  describe('a pane holding rows warms through a count-matched window (#10005)', () => {
    // One row per minute from a fixed instant, so every ts parses and orders.
    const rows = (n: number, from = 0) => Array.from({ length: n }, (_, k) => {
      const i = from + k
      return msg(`row ${i}`, new Date(Date.UTC(2026, 7, 13, 0, i)).toISOString(), `m-${i}`)
    })

    it('asks for at least what the pane holds, never below the pane bound', async () => {
      const held = rows(3)
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail, messages: held, has_more: true, total: 400 })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
      expect(api.chatSlotDetail).toHaveBeenCalledWith('bg-slot', PANE_HYDRATE_LIMIT)
    })

    it('grows the window to a pane that holds more than the bound, so the warm cannot shrink it', async () => {
      const held = rows(PANE_HYDRATE_LIMIT + 7)
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail, messages: held, has_more: true, total: 400 })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
      const [, limit] = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls[0]
      expect(limit).toBe(PANE_HYDRATE_LIMIT + 7)
      expect(limit).toBeLessThanOrEqual(500)
    })

    it('sizes the window on durable rows only, so client-only rows do not ratchet it', async () => {
      const held = [
        ...rows(3),
        { role: 'thinking', content: 'reasoning', cls: '', ts: '2026-08-13T01:00:00Z' },
        { role: 'permission', content: 'approve?', cls: '', ts: '2026-08-13T01:01:00Z', meta: { approval_id: 'a-1' } },
      ]
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail, messages: rows(3), has_more: true, total: 400 })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      // 3 durable rows -> the floor; 5 raw rows would also give the floor here, so
      // pin the counting rule where it is observable: a pane past the bound.
      expect(api.chatSlotDetail).toHaveBeenCalledWith('bg-slot', PANE_HYDRATE_LIMIT)
      const big = [...rows(PANE_HYDRATE_LIMIT + 2), { role: 'thinking', content: 'reasoning', cls: '', ts: '2026-08-13T05:00:00Z' }]
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockClear()
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail, messages: rows(PANE_HYDRATE_LIMIT + 2), has_more: true, total: 400 })
      const store2 = makeStore('active-slot', { slotMessages: { 'bg-slot': big } })
      await store2.dispatch(warmSlotCache('bg-slot') as never)
      expect(api.chatSlotDetail).toHaveBeenCalledWith('bg-slot', PANE_HYDRATE_LIMIT + 2)
    })

    it('keeps a pane larger than the handler ceiling on one bounded read: the window anchors and the head is kept', async () => {
      // 3000 rows: the shape of the Crew Members DM this bound is for. The window
      // is the newest 500 server rows; its oldest row IS in the cache, so the
      // reducer keeps everything above it as head -- no retry, no shrink.
      const held = rows(3000)
      const window = held.slice(2500)
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail, messages: window, has_more: true, total: 3000 })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
      expect(api.chatSlotDetail).toHaveBeenCalledWith('bg-slot', 500)
      expect(store.getState().chat.slotMessages['bg-slot']).toHaveLength(3000)
    })

    it('does not retry when the server grew and the window merely pushed the cache\'s oldest rows above it', async () => {
      // The pane holds rows 0..59; two rows landed on the server that this pane never
      // saw. The newest-60 window is rows 2..61: its oldest row anchors into the
      // cache, so the reducer keeps rows 0..1 as head and the two new rows arrive.
      const held = rows(60)
      const window = [...held.slice(2), ...rows(2, 60)]
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail, messages: window, has_more: true, total: 400 })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
      const contents = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
      expect(contents[0]).toBe('row 0')
      expect(contents).toHaveLength(62)
      expect(contents.slice(-2)).toEqual(['row 60', 'row 61'])
    })

    it('does not retry over a live row appended while the window was in flight', async () => {
      // A row NEWER than the window is a tail the reducer rescues, not a hole.
      const held = rows(2)
      let resolveWarm: (v: unknown) => void = () => {}
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise(r => { resolveWarm = r }))
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      const warm = store.dispatch(warmSlotCache('bg-slot') as never)
      store.dispatch(appendSlotMessage({ slot: 'bg-slot', message: msg('live', '2026-08-13T11:00:00Z', 'm-live') as never }))
      resolveWarm({ ...detail, messages: held, has_more: true, total: 3 })
      await warm
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
      expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content)).toEqual(['row 0', 'row 1', 'live'])
    })

    it('retries unbounded only when the window anchors into neither the cache head nor its oldest row', async () => {
      const held = rows(3)
      // The server grew past the whole window: it holds none of the pane's rows, so
      // the reducer could only splice it on with a hole.
      const disjoint = rows(2, 100)
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>)
        .mockResolvedValueOnce({ ...detail, messages: disjoint, has_more: true, total: 400 })
        .mockResolvedValueOnce({ ...detail, messages: [...held, ...disjoint], has_more: false, total: 5 })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      const calls = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls
      expect(calls).toHaveLength(2)
      expect(calls[0]).toEqual(['bg-slot', PANE_HYDRATE_LIMIT])
      expect(calls[1]).toEqual(['bg-slot'])
      expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content))
        .toEqual(['row 0', 'row 1', 'row 2', 'row 100', 'row 101'])
    })

    it('discards a page when a /clear landed while the window was in flight', async () => {
      // The page describes the transcript the clear discarded; writing it back would
      // resurrect it into the just-cleared pane.
      const held = rows(3)
      let resolveWarm: (v: unknown) => void = () => {}
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise(r => { resolveWarm = r }))
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      const warm = store.dispatch(warmSlotCache('bg-slot') as never)
      store.dispatch(clearSlotCache('bg-slot'))
      // The backend's own confirmation row is what a later fetch would carry; this
      // stale page carries the pre-clear rows.
      resolveWarm({ ...detail, messages: held, has_more: true, total: 400 })
      await warm
      expect(store.getState().chat.slotMessages['bg-slot']).toEqual([])
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
    })

    it('discards the unbounded retry too when a /clear landed during it', async () => {
      const held = rows(3)
      let resolveWide: (v: unknown) => void = () => {}
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>)
        .mockResolvedValueOnce({ ...detail, messages: rows(2, 100), has_more: true, total: 400 })
        .mockImplementationOnce(() => new Promise(r => { resolveWide = r }))
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      const warm = store.dispatch(warmSlotCache('bg-slot') as never)
      // Let the bounded page land and the retry start before the clear.
      await new Promise(r => setTimeout(r, 0))
      await new Promise(r => setTimeout(r, 0))
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(2)
      store.dispatch(clearSlotCache('bg-slot'))
      resolveWide({ ...detail, messages: [...held, ...rows(2, 100)], has_more: false, total: 5 })
      await warm
      expect(store.getState().chat.slotMessages['bg-slot']).toEqual([])
    })

    it('discards a page for a slot the authoritative list no longer carries, even on a cold cache', async () => {
      // A delete evicts the cache AND the clear count, so a cold cache shows no
      // trace of it; the loaded slot list is the signal, and a late page must not
      // recreate the deleted transcript's cache.
      let resolveWarm: (v: unknown) => void = () => {}
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise(r => { resolveWarm = r }))
      const dashboardBase = dashboardReducer(undefined, { type: '@@INIT' })
      const store = configureStore({
        reducer: { chat: chatReducer, dashboard: dashboardReducer },
        preloadedState: {
          chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'active-slot' },
          dashboard: { ...dashboardBase, slotsLoaded: true, slots: [{ key: 'active-slot' }, { key: 'bg-slot' }] as never },
        },
      })
      const warm = store.dispatch(warmSlotCache('bg-slot') as never)
      // The slot is deleted while the page is in flight.
      store.dispatch(sseSlots([{ key: 'active-slot' }] as never))
      resolveWarm({ ...detail, messages: rows(3), has_more: false, total: 3 })
      await warm
      expect(store.getState().chat.slotMessages['bg-slot']).toBeUndefined()
    })

    it('discards a page for a slot deleted AND recreated under the same key while the window was in flight', async () => {
      // A cold cache before and after, and the key is back in the loaded list, so
      // neither the emptied-cache nor the slot-gone check can see it, and a cold
      // slot leaves no residue for the eviction to move the generation on. The
      // recreated record's `created` is what tells (deterministic app slot keys
      // make same-key recreation an ordinary flow).
      let resolveWarm: (v: unknown) => void = () => {}
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise(r => { resolveWarm = r }))
      const dashboardBase = dashboardReducer(undefined, { type: '@@INIT' })
      const store = configureStore({
        reducer: { chat: chatReducer, dashboard: dashboardReducer },
        preloadedState: {
          chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'active-slot' },
          dashboard: { ...dashboardBase, slotsLoaded: true, slots: [{ key: 'active-slot', created: '2026-08-13T00:00:00Z' }, { key: 'bg-slot', created: '2026-08-13T01:00:00Z' }] as never },
        },
      })
      const warm = store.dispatch(warmSlotCache('bg-slot') as never)
      store.dispatch(sseSlots([{ key: 'active-slot', created: '2026-08-13T00:00:00Z' }] as never))
      store.dispatch(sseSlots([{ key: 'active-slot', created: '2026-08-13T00:00:00Z' }, { key: 'bg-slot', created: '2026-08-13T02:00:00Z' }] as never))
      resolveWarm({ ...detail, messages: rows(3), has_more: false, total: 3 })
      await warm
      expect(store.getState().chat.slotMessages['bg-slot']).toBeUndefined()
    })

    it('discards a page when a slot WITH residue is deleted and recreated under the same key', async () => {
      // The eviction moves the key's generation, which survives the eviction.
      let resolveWarm: (v: unknown) => void = () => {}
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise(r => { resolveWarm = r }))
      const dashboardBase = dashboardReducer(undefined, { type: '@@INIT' })
      const store = configureStore({
        reducer: { chat: chatReducer, dashboard: dashboardReducer },
        preloadedState: {
          chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'active-slot', slotMessages: { 'bg-slot': rows(2) } },
          dashboard: { ...dashboardBase, slotsLoaded: true, slots: [{ key: 'active-slot' }, { key: 'bg-slot' }] as never },
        },
      })
      const warm = store.dispatch(warmSlotCache('bg-slot') as never)
      store.dispatch(sseSlots([{ key: 'active-slot' }] as never))
      // The eviction emptied the cache, so re-seed the recreated slot with a live
      // row: the emptied-cache check is then blind and the generation must tell.
      store.dispatch(sseSlots([{ key: 'active-slot' }, { key: 'bg-slot' }] as never))
      store.dispatch(appendSlotMessage({ slot: 'bg-slot', message: msg('fresh', '2026-08-13T11:00:00Z', 'm-fresh') as never }))
      resolveWarm({ ...detail, messages: rows(2), has_more: true, total: 400 })
      await warm
      expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content)).toEqual(['fresh'])
    })

    it('retains no baseline from a RUNNING unbounded retry (short by the in-flight row); the bounded count carries instead', async () => {
      const held = rows(3)
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>)
        .mockResolvedValueOnce({ ...detail, messages: rows(2, 100), has_more: true, total: 400, running: true })
        .mockResolvedValueOnce({ ...detail, messages: [...held, ...rows(2, 100), { role: 'streaming', content: 'partial', cls: '', ts: '' }], has_more: false, total: 900, running: true })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(2)
      // Bounded page: total 400 with no pending cards -> 400. The retry's own
      // durable count (5) would have been retained before and read a later
      // one-row rewind as no change.
      expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(400)
    })

    it('does not retry when the window already reaches the start of history', async () => {
      // A rewind removed rows the pane still holds; the window IS the whole
      // transcript, so a retry would return the same rows one round trip later.
      const held = rows(3)
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail, messages: [held[0]], has_more: false, total: 1 })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held }, slotServerTotal: { 'bg-slot': 3 } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
      expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content)).toEqual(['row 0'])
    })

    it('takes the unbounded retry for a pane whose rows carry no mid, since nothing can anchor', async () => {
      // Legacy rows predate meta.mid: the page's oldest row anchors nowhere and the
      // cache has no identified oldest row, so the stitch check declines.
      const legacy = [msg('legacy 1', '2026-08-13T05:00:00Z'), msg('legacy 2', '2026-08-13T06:00:00Z')]
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail, messages: legacy, has_more: true, total: 200 })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': legacy } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      const calls = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls
      expect(calls).toEqual([['bg-slot', PANE_HYDRATE_LIMIT], ['bg-slot']])
    })

    it('does not let a client-minted error bubble pin the slot to the unbounded read', async () => {
      // A send failure appends an `error` row locally: durable-role, no mid, and
      // rescued as tail on every warm, so it never leaves the cache. Declining on
      // it would send EVERY later warm unbounded -- the cost this bound removes.
      const held = [...rows(60), { role: 'error', content: 'send failed', cls: '', ts: '2026-08-13T05:00:00Z' }]
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail, messages: rows(60), has_more: true, total: 400 })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      await store.dispatch(warmSlotCache('bg-slot') as never)
      const calls = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls
      expect(calls).toHaveLength(2)
      expect(calls.every(c => c[1] === 61)).toBe(true)
      const contents = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
      expect(contents).toHaveLength(61)
      expect(contents.at(-1)).toBe('send failed')
    })

    it('carries the bounded count through the retry, so a raw unbounded total never becomes the baseline', async () => {
      // The unbounded handler counts raw rows (a `done` per finished turn among
      // them); the bounded one counts collapsed rows. Retaining the raw 900 would
      // make the next bounded warm's 402 read as a truncation and drop its tail.
      const held = rows(3)
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>)
        .mockResolvedValueOnce({ ...detail, messages: rows(2, 100), has_more: true, total: 400 })
        .mockResolvedValueOnce({ ...detail, messages: [...held, ...rows(2, 100)], has_more: true, total: 900 })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(2)
      expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(400)
    })

    it('retains a RUNNING count from a bounded warm in settled units: pending cards subtracted', async () => {
      // The bounded corpus keeps pending permission cards the settled one drops, so
      // a mid-turn `total` is settled + pending. Raw, the next settled warm would
      // read the cards' resolution as a truncation and drop the rescued tail;
      // refused, a slot that runs for most of its life would never get a baseline.
      const held = rows(3)
      const card = { role: 'permission', content: '', cls: '', ts: '2026-08-13T05:00:00Z', meta: { approval_id: 'a-1' } }
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail, messages: [...held, card], has_more: true, total: 401, running: true })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      expect(api.chatSlotDetail).toHaveBeenCalledWith('bg-slot', PANE_HYDRATE_LIMIT)
      expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(400)
    })

    it('carries the normalised bounded count through a retry from a mid-turn page, never the raw one', async () => {
      const held = rows(3)
      const card = { role: 'permission', content: '', cls: '', ts: '2026-08-13T05:00:00Z', meta: { approval_id: 'a-1' } }
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>)
        .mockResolvedValueOnce({ ...detail, messages: [...rows(2, 100), card], has_more: true, total: 401, running: true })
        .mockResolvedValueOnce({ ...detail, messages: [...held, ...rows(2, 100)], has_more: true, total: 900, running: false })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(2)
      expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(400)
    })

    it('declines a page that only SPANS a cache whose head carries no mid', async () => {
      // Legacy rows above the first identified one: the page contains that
      // identified row (spans) but its own oldest row is a legacy row the cache
      // cannot anchor, so the reducer would replace the head. Unbounded instead.
      const legacyHead = [msg('legacy 1', '2026-08-13T00:00:00Z'), msg('legacy 2', '2026-08-13T00:01:00Z')]
      const identified = rows(3, 10)
      const held = [...legacyHead, ...identified]
      // Server grew by one; the window (5 durable rows) reaches one legacy row further back.
      const window = [msg('legacy 2', '2026-08-13T00:01:00Z'), ...identified, ...rows(1, 20)]
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>)
        .mockResolvedValueOnce({ ...detail, messages: window, has_more: true, total: 400 })
        .mockResolvedValueOnce({ ...detail, messages: [...held, ...rows(1, 20)], has_more: false, total: 6 })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      const calls = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls
      expect(calls).toEqual([['bg-slot', PANE_HYDRATE_LIMIT], ['bg-slot']])
      expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content)[0]).toBe('legacy 1')
    })

    it('accepts a page that spans the cache when the only mid-less row is a tail bubble', async () => {
      // Contrast for the head guard: an `error` bubble BELOW the identified rows is
      // not a head, so `spans` is enough and the warm stays bounded.
      const held = [...rows(3), { role: 'error', content: 'send failed', cls: '', ts: '2026-08-13T05:00:00Z' }]
      const window = [msg('older', '2026-08-12T23:59:00Z', 'm-older'), ...rows(3)]
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail, messages: window, has_more: true, total: 400 })
      const store = makeStore('active-slot', { slotMessages: { 'bg-slot': held } })
      await store.dispatch(warmSlotCache('bg-slot') as never)
      expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
    })
  })

  // Legacy rows predate meta.mid, so no overlap can be found at all. Replacing
  // with the bounded warm would delete history the pane had already paged in.
  it('keeps a longer legacy array when no row identity is available', async () => {
    const legacy = [
      msg('legacy 1', '2026-08-13T05:00:00Z'),
      msg('legacy 2', '2026-08-13T06:00:00Z'),
      msg('legacy 3', '2026-08-13T07:00:00Z'),
    ]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [msg('warm only', '2026-08-13T09:00:00Z', 'm-w')], has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': legacy },
      slotPaneHasMore: { 'bg-slot': false },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    // No legacy row is deleted -- they survive in order at the front. The warm
    // row is appended, not discarded: prior ends before the page begins.
    expect(held).toEqual(['legacy 1', 'legacy 2', 'legacy 3', 'warm only'])
    expect(held.slice(0, 3)).toEqual(['legacy 1', 'legacy 2', 'legacy 3'])
    // Not the bounded warm, so the warm's has_more does not describe it.
    expect(store.getState().chat.slotPaneHasMore['bg-slot']).toBe(false)
  })

  // Rows the pane already held can carry queued bubbles the server has since
  // started, so returning them verbatim showed a running bubble as queued.
  it('collapses queued rows through the shared path even when it keeps prior', async () => {
    const legacy = [
      msg('legacy 1', '2026-08-13T05:00:00Z'),
      msg('legacy 2', '2026-08-13T06:00:00Z'),
      msg('legacy 3', '2026-08-13T07:00:00Z'),
      { role: 'queued', content: 'stale queued', cls: 'msg msg-queued', ts: '2026-08-13T08:00:00Z', meta: { queueId: 'q-old' } },
    ]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail,
      messages: [msg('warm only', '2026-08-13T09:00:00Z', 'm-w')],
      queue: [{ content: 'still queued', queueId: 'q-new', ts: '2026-08-13T10:00:00Z' }],
      has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': legacy },
      slotPaneHasMore: { 'bg-slot': false },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    // Every legacy row survives, the server queue replaced the stale bubble.
    expect(held).toEqual(['legacy 1', 'legacy 2', 'legacy 3', 'still queued'])
  })

  // A completed stream row can return with neither mid nor sendId, so anchoring the
  // rescue on the warm's NEWEST row found nothing and dropped an in-flight send.
  it('keeps a just-sent row when the warm newest row carries no identity', async () => {
    const history = msg('history', '2026-08-13T08:00:00Z', 'm-1')
    const optimistic = { role: 'user', content: 'just sent', cls: 'msg msg-u', ts: '2026-08-13T11:00:00Z', meta: { sendId: 's-1' } }
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail,
      messages: [
        history,
        msg('warm mid', '2026-08-13T09:00:00Z', 'm-2'),
        msg('completed stream', '2026-08-13T10:00:00Z'),
      ],
      has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [history, optimistic] },
      slotPaneHasMore: { 'bg-slot': false },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toContain('just sent')
  })

  // Counter-case: retaining prior whenever the warm's newest row is unlocatable
  // would strand stale scrollback, the very failure the warm path exists to fix.
  it('still replaces a stale short array when the warm newest row carries no identity', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail,
      messages: [
        msg('fresh 1', '2026-08-13T09:00:00Z', 'm-9'),
        msg('fresh 2', '2026-08-13T10:00:00Z'),
      ],
      has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [msg('stale only', '2026-08-13T01:00:00Z')] },
      slotPaneHasMore: { 'bg-slot': false },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toEqual(['fresh 1', 'fresh 2'])
    expect(held).not.toContain('stale only')
  })

  // A disconnect leaves prior entirely BEHIND the page, so no identity is shared
  // and the longer array wins on length -- discarding everything that arrived.
  it('keeps the messages a disconnect delivered when prior is longer and disjoint', async () => {
    const loaded = [
      msg('loaded 1', '2026-08-13T01:00:00Z', 'm-1'),
      msg('loaded 2', '2026-08-13T02:00:00Z', 'm-2'),
      msg('loaded 3', '2026-08-13T03:00:00Z', 'm-3'),
      msg('loaded 4', '2026-08-13T04:00:00Z', 'm-4'),
    ]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail,
      messages: [
        msg('arrived 1', '2026-08-13T08:00:00Z', 'm-8'),
        msg('arrived 2', '2026-08-13T09:00:00Z', 'm-9'),
        msg('arrived 3', '2026-08-13T10:00:00Z', 'm-10'),
      ],
      has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': loaded },
      slotPaneHasMore: { 'bg-slot': false },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    // Both directions in one assertion: the scrollback the pane had paged in
    // survives AND the rows the disconnect delivered are now present.
    expect(held).toEqual([
      'loaded 1', 'loaded 2', 'loaded 3', 'loaded 4',
      'arrived 1', 'arrived 2', 'arrived 3',
    ])
  })

  // Counter-case: prior's newest row POSTDATES the warm's oldest, so the arrays
  // interleave and appending would place an older row after a newer one.
  it('declines to append a warm page that is not wholly newer than prior', async () => {
    const prior = [
      msg('p 1', '2026-08-13T05:00:00Z'),
      msg('p 2', '2026-08-13T06:00:00Z'),
      msg('p 3', '2026-08-13T11:00:00Z'),
    ]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [msg('w 1', '2026-08-13T09:00:00Z', 'm-w')], has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': prior },
      slotPaneHasMore: { 'bg-slot': false },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toEqual(['p 1', 'p 2', 'p 3'])
  })

  // The rescued tail exists to recover prior rows the base DROPPED, so a base
  // that already carries all of prior must not append it a second time.
  it('does not duplicate the rescued tail when it appends a wholly newer warm page', async () => {
    const optimistic = { role: 'user', content: 'just sent', cls: 'msg msg-u', ts: '2026-08-13T04:00:00Z', meta: { sendId: 's-1' } }
    const trailing = msg('trailing local', '2026-08-13T04:30:00Z')
    const loaded = [
      msg('loaded 1', '2026-08-13T01:00:00Z', 'm-1'),
      msg('loaded 2', '2026-08-13T02:00:00Z', 'm-2'),
      optimistic,
      trailing,
    ]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail,
      messages: [
        { role: 'user', content: 'just sent', cls: 'msg msg-u', ts: '2026-08-13T08:00:00Z', meta: { sendId: 's-1' } },
        msg('arrived', '2026-08-13T09:00:00Z', 'm-9'),
      ],
      has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': loaded },
      slotPaneHasMore: { 'bg-slot': false },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held.filter(c => c === 'trailing local')).toHaveLength(1)
    expect(held.filter(c => c === 'just sent')).toHaveLength(1)
  })

  // Control for the pair below: one transcript, one ts format. Ordering is
  // unambiguous here, so the page must append both before and after the fix.
  it('appends a wholly newer page when every timestamp is in Z form', async () => {
    const prior = [
      msg('p1', '2026-08-13T05:00:00Z'),
      msg('p2', '2026-08-13T06:00:00Z'),
      msg('p3', '2026-08-13T07:00:00Z'),
      msg('p4', '2026-08-13T08:00:00Z'),
    ]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [msg('w1', '2026-08-13T12:00:00Z', 'm-w')], has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': prior },
      slotPaneHasMore: { 'bg-slot': false },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toEqual(['p1', 'p2', 'p3', 'p4', 'w1'])
  })

  // One transcript can carry both offset-aware and naive rows, so comparing the
  // raw strings orders them by TEXT: '17:00+09:00' reads later than '12:00Z'.
  it('appends a wholly newer page when prior carries an offset-aware timestamp', async () => {
    const prior = [
      msg('p1', '2026-08-13T14:00:00+09:00'),
      msg('p2', '2026-08-13T15:00:00+09:00'),
      msg('p3', '2026-08-13T16:00:00+09:00'),
      // Real instant 08:00Z, which genuinely precedes the page's 12:00Z.
      msg('p4', '2026-08-13T17:00:00+09:00'),
    ]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [msg('w1', '2026-08-13T12:00:00Z', 'm-w')], has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': prior },
      slotPaneHasMore: { 'bg-slot': false },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    // The whole server page was discarded while the pane kept rendering prior,
    // so nothing vanished on screen -- the arrived row simply never appeared.
    expect(held).toEqual(['p1', 'p2', 'p3', 'p4', 'w1'])
  })

  // An unreadable ts must decline the append, not guess an order -- the same
  // rule rowIdentities and tailNotInPage already follow for a missing identity.
  it('declines to append when a timestamp cannot be parsed', async () => {
    const prior = [
      msg('p1', '2026-08-13T05:00:00Z'),
      msg('p2', '2026-08-13T06:00:00Z'),
      msg('p3', 'not-a-timestamp'),
    ]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [msg('w1', '2026-08-13T12:00:00Z', 'm-w')], has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': prior },
      slotPaneHasMore: { 'bg-slot': false },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toEqual(['p1', 'p2', 'p3'])
  })

  // Omitting the bounded length DELETES the marker, and hydrateSlotMessages takes
  // its early return without one, so the pane could never widen to full history.
  it('restates the bounded marker when the warm is still the page prefix', async () => {
    const page = [msg('p1', '2026-08-13T05:00:00Z', 'm-1'), msg('p2', '2026-08-13T06:00:00Z', 'm-2')]
    const liveTail = msg('t1', '2026-08-13T07:00:00Z', 'm-3')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [...page], has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [...page, liveTail] },
      slotPaneHasMore: { 'bg-slot': true },
      slotPaneBounded: { 'bg-slot': 2 },
      slotHydrated: { 'bg-slot': true },
      slotRun: { 'bg-slot': { state: 'idle' } },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(store.getState().chat.slotPaneBounded['bg-slot']).toBe(2)
    // The one-shot widen must still land, keeping the live tail the page lacked.
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot',
      messages: [msg('older A', '2026-08-13T01:00:00Z', 'm-a'), ...page],
      hasMore: false,
      bounded: false,
    }))
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toEqual(['older A', 'p1', 'p2', 't1'])
  })

  // Counter-case: once older rows are prepended the warm is no longer the prefix,
  // so a restated length would describe rows the page never covered.
  it('claims no bounded page when the merge prepended older rows', async () => {
    const older = msg('older head', '2026-08-13T08:00:00Z', 'm-older')
    const overlap = msg('warm oldest', '2026-08-13T09:00:00Z', 'm-overlap')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [overlap], has_more: true,
    })
    const store = makeStore('active-slot', { slotMessages: { 'bg-slot': [older, overlap] } })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(store.getState().chat.slotPaneBounded['bg-slot']).toBeUndefined()
  })

  // Counter-case: keeping the longer prior array means the pane holds more than the
  // page, so a restated length would understate what it already shows.
  it('claims no bounded page when it kept a longer prior array', async () => {
    const legacy = [
      msg('legacy 1', '2026-08-13T05:00:00Z'),
      msg('legacy 2', '2026-08-13T06:00:00Z'),
      msg('legacy 3', '2026-08-13T07:00:00Z'),
    ]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [msg('warm only', '2026-08-13T09:00:00Z', 'm-w')], has_more: true,
    })
    const store = makeStore('active-slot', { slotMessages: { 'bg-slot': legacy } })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(store.getState().chat.slotPaneBounded['bg-slot']).toBeUndefined()
  })

  // switchSlot.fulfilled fills the same array. It wrote no marker at all, so a
  // stale `true` from an earlier bounded warm outlived the full transcript.
  it('writes the marker when switchSlot loads a slot, not just the array', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [msg('only row', '2026-08-13T10:00:00Z', 'm-1')], has_more: false,
    })
    const store = makeStore('other-slot', { slotPaneHasMore: { 'target-slot': true } })
    await store.dispatch(switchSlot('target-slot') as never)
    expect(store.getState().chat.slotPaneHasMore['target-slot']).toBe(false)
  })

  // Both directions: a slot that genuinely fits under the bound must NOT be
  // marked, or every pane grows a permanent row pointing at nothing.
  it('records false when the whole history fits under the bound', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, has_more: false, total: 3,
    })
    const store = makeStore('active-slot')
    await store.dispatch(warmSlotCache('small-slot') as never)
    expect(store.getState().chat.slotPaneHasMore['small-slot']).toBe(false)
  })
})

describe('hydrateSlotMessages leaves an already-loaded page alone', () => {
  // Prepending a bounded tail onto an already-loaded transcript put the newest
  // rows first and wrote a false earlier-messages marker over a full cache.
  it('keeps the loaded transcript and its marker instead of prepending a tail', () => {
    const full = [
      msg('old 1', '2026-08-13T01:00:00Z', 'm-1'),
      msg('old 2', '2026-08-13T02:00:00Z', 'm-2'),
      msg('newest', '2026-08-13T03:00:00Z', 'm-3'),
    ]
    const store = makeStore('other-slot', {
      slotMessages: { 'bg-slot': full },
      slotPaneHasMore: { 'bg-slot': false },
    })
    store.dispatch(hydrateSlotMessages({ slot: 'bg-slot', messages: [msg('newest', '2026-08-13T03:00:00Z', 'm-3')], hasMore: true }))
    const st = store.getState().chat
    expect(st.slotMessages['bg-slot'].map(m => m.content)).toEqual(['old 1', 'old 2', 'newest'])
    expect(st.slotPaneHasMore['bg-slot']).toBe(false)
  })

  // The prepend still exists for its real case: live frames seeded by
  // applyNonActiveFrame, which never records a marker.
  it('still prepends history in front of live frames that arrived first', () => {
    const store = makeStore('other-slot', {
      slotMessages: { 'bg-slot': [msg('live frame', '2026-08-13T09:00:00Z', 'm-live')] },
    })
    store.dispatch(hydrateSlotMessages({ slot: 'bg-slot', messages: [msg('history', '2026-08-13T01:00:00Z', 'm-h')], hasMore: true }))
    const st = store.getState().chat
    expect(st.slotMessages['bg-slot'].map(m => m.content)).toEqual(['history', 'live frame'])
    // The seeded frame is newer than the page, so the page's has-more still
    // describes what precedes it; leaving it unset hid the recovery row.
    expect(st.slotPaneHasMore['bg-slot']).toBe(true)
  })

  // A pane seeded by a live frame is the case that used to lose its marker
  // entirely, so a 50-row slice posed as the whole conversation.
  it('records the marker on a bounded hydrate that a live frame seeded', () => {
    const store = makeStore('other-slot', {
      slotMessages: { 'bg-slot': [msg('live frame', '2026-08-13T09:00:00Z', 'm-live')] },
    })
    store.dispatch(hydrateSlotMessages({ slot: 'bg-slot', messages: [msg('history', '2026-08-13T01:00:00Z', 'm-h')], hasMore: true, bounded: true }))
    const st = store.getState().chat
    expect(st.slotPaneHasMore['bg-slot']).toBe(true)
    expect(st.slotPaneBounded['bg-slot']).toBe(1)
  })
})

describe('switching away from a pane whose own fetch has not landed', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue(detail)
  })

  // `slotHasMore` belongs to the chat we left until this pane's switch resolves,
  // so writing it here marked a truncated pane as complete and dropped the row.
  it('preserves the bounded record and marker instead of writing a stale has_more', async () => {
    const store = makeStore('bg-slot', {
      messages: [msg('older', '2026-08-13T01:00:00Z'), msg('newer', '2026-08-13T02:00:00Z')],
      slotMessages: { 'bg-slot': [msg('older', '2026-08-13T01:00:00Z'), msg('newer', '2026-08-13T02:00:00Z')] },
      slotPaneBounded: { 'bg-slot': 2 },
      slotPaneHasMore: { 'bg-slot': true },
      // The pane's own switch is still in flight, so its has_more never landed.
      slotSwitchRequestId: 'rid-bg',
      slotSwitchTarget: 'bg-slot',
      slotHasMore: false,
    })
    await store.dispatch(switchSlot('other-slot') as never)
    const st = store.getState().chat
    expect(st.slotPaneHasMore['bg-slot']).toBe(true)
    expect(st.slotPaneBounded['bg-slot']).toBe(2)
  })

  // The settled case must keep its existing behaviour: the active view really is
  // the whole transcript, so its own has_more is the right marker to write.
  it('still writes the active slot has_more once its switch has landed', async () => {
    const store = makeStore('full-slot', {
      messages: [msg('one', '2026-08-13T09:00:00Z')],
      slotPaneHasMore: { 'full-slot': true },
      slotHasMore: false,
    })
    await store.dispatch(switchSlot('other-slot') as never)
    const st = store.getState().chat
    expect(st.slotPaneHasMore['full-slot']).toBe(false)
    expect(st.slotPaneBounded['full-slot']).toBeUndefined()
  })

  // Live frames seed the array with no marker, so a warm that appends a newer tail
  // is the FIRST writer -- withholding has_more leaves no marker at all.
  it('records has_more when the warm keeps a newer live tail', async () => {
    const history = msg('history', '2026-08-13T08:00:00Z', 'm-1')
    const live = msg('live frame', '2026-08-13T12:00:00Z', 'm-99')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail,
      messages: [history, msg('warm 2', '2026-08-13T09:00:00Z', 'm-2'), msg('warm 3', '2026-08-13T10:00:00Z', 'm-3')],
      has_more: true,
    })
    const store = makeStore('active-slot', { slotMessages: { 'bg-slot': [history, live] } })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const s = store.getState().chat
    expect(s.slotMessages['bg-slot'].map(m => m.content)).toEqual(['history', 'warm 2', 'warm 3', 'live frame'])
    expect(s.slotPaneHasMore['bg-slot']).toBe(true)
  })

  // The marker is also the sentinel that stops a late hydrate prepending onto a
  // loaded transcript, so an unrecorded has_more duplicates the overlapping rows.
  it('does not duplicate rows when the pane hydrate lands after such a warm', async () => {
    const history = msg('history', '2026-08-13T08:00:00Z', 'm-1')
    const live = msg('live frame', '2026-08-13T12:00:00Z', 'm-99')
    const warm2 = msg('warm 2', '2026-08-13T09:00:00Z', 'm-2')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [history, warm2, msg('warm 3', '2026-08-13T10:00:00Z', 'm-3')], has_more: true,
    })
    const store = makeStore('active-slot', { slotMessages: { 'bg-slot': [history, live] } })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    store.dispatch(hydrateSlotMessages({ slot: 'bg-slot', messages: [history, warm2], hasMore: true, bounded: true }))
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held.filter(c => c === 'history')).toHaveLength(1)
    expect(held.filter(c => c === 'warm 2')).toHaveLength(1)
    expect(held).toEqual(['history', 'warm 2', 'warm 3', 'live frame'])
  })

  // A rewind on another client SHORTENS the server's history, so the rows it
  // discarded are still sitting in this pane's array after the anchor. Appending
  // them puts deleted turns back on screen. The server's own count is what
  // separates that from the live-tail case below: a rewind DROPS it.
  it('does not resurrect forward turns a rewind discarded', async () => {
    const anchor = msg('anchor', '2026-08-13T08:00:00Z', 'm-1')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [anchor], has_more: false, total: 1,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [anchor, msg('forward 1', '2026-08-13T09:00:00Z', 'm-2'), msg('forward 2', '2026-08-13T10:00:00Z', 'm-3')] },
      slotServerTotal: { 'bg-slot': 3 },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toEqual(['anchor'])
  })

  // OPPOSITE DIRECTION, and the reason the rescue cannot simply be deleted: an
  // SSE row the page was built too early to include is one the server DOES hold,
  // so its count has not dropped. That tail must still be rescued.
  it('still rescues a live tail when the server count has not dropped', async () => {
    const history = msg('history', '2026-08-13T08:00:00Z', 'm-1')
    const live = msg('live frame', '2026-08-13T12:00:00Z', 'm-99')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail,
      messages: [history, msg('warm 2', '2026-08-13T09:00:00Z', 'm-2'), msg('warm 3', '2026-08-13T10:00:00Z', 'm-3')],
      has_more: true, total: 4,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [history, live] },
      slotServerTotal: { 'bg-slot': 4 },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toEqual(['history', 'warm 2', 'warm 3', 'live frame'])
  })

  // Decline, not guess: with no retained count there is nothing to compare, so
  // the rescue stands rather than dropping rows on a hunch. The principle is
  // unchanged; the fixture is narrowed to a pane that genuinely has no count.
  // A pane seeded ONLY by live frames never fetched a page, so nothing ever told
  // it what the server holds -- see the hydrate-seeded cases below, which do.
  it('keeps the rescue when no server count has been retained yet', async () => {
    const anchor = msg('anchor', '2026-08-13T08:00:00Z', 'm-1')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [anchor], has_more: false, total: 1,
    })
    // Seeded by live frames alone -- no hydrate, no warm, so no count exists.
    const store = makeStore('active-slot')
    store.dispatch(appendSlotMessage({ slot: 'bg-slot', message: anchor as never }))
    store.dispatch(appendSlotMessage({ slot: 'bg-slot', message: msg('forward 1', '2026-08-13T09:00:00Z', 'm-2') as never }))
    expect(store.getState().chat.slotServerTotal['bg-slot']).toBeUndefined()
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toEqual(['anchor', 'forward 1'])
  })

  // The comparison is only as good as the value it compares against, so the warm
  // must leave the count it just saw behind for the next one.
  it('retains the server count so a later warm can compare against it', async () => {
    const anchor = msg('anchor', '2026-08-13T08:00:00Z', 'm-1')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [anchor], has_more: false, total: 7,
    })
    const store = makeStore('active-slot', { slotMessages: { 'bg-slot': [anchor] } })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(7)
  })

  // A background pane's FIRST write is its own bounded hydrate, and until that
  // hydrate carried the count there was nothing for the warm to compare against.
  // Measured below: the other two retainers sit behind an active-slot guard, so
  // for a background slot the hydrate is the only thing that can seed it.
  it('does not resurrect forward turns after a rewind on a hydrate-seeded pane', async () => {
    const anchor = msg('anchor', '2026-08-13T08:00:00Z', 'm-1')
    // Remote rewind: the server now holds one row where the pane holds three.
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [anchor], has_more: false, total: 1,
    })
    const store = makeStore('active-slot')
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot',
      messages: [anchor, msg('forward 1', '2026-08-13T09:00:00Z', 'm-2'), msg('forward 2', '2026-08-13T10:00:00Z', 'm-3')] as never,
      hasMore: false,
      bounded: true,
      total: 3,
    }))
    expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(3)
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toEqual(['anchor'])
  })

  // NEGATIVE CONTROL for the case above. Suppressing the rescue wrongly DROPS a
  // live row the server does hold, so widening the set of panes where the count
  // can fire has to be pinned against that. A live frame arrives after the
  // hydrate and the server count has NOT dropped -- the tail must survive.
  it('still rescues a live tail on a hydrate-seeded pane when the count has not dropped', async () => {
    const history = msg('history', '2026-08-13T08:00:00Z', 'm-1')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail,
      messages: [history, msg('warm 2', '2026-08-13T09:00:00Z', 'm-2'), msg('warm 3', '2026-08-13T10:00:00Z', 'm-3')],
      has_more: true,
      total: 4,
    })
    const store = makeStore('active-slot')
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot', messages: [history] as never, hasMore: true, bounded: true, total: 4,
    }))
    store.dispatch(appendSlotMessage({ slot: 'bg-slot', message: msg('live frame', '2026-08-13T12:00:00Z', 'm-99') as never }))
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toEqual(['history', 'warm 2', 'warm 3', 'live frame'])
  })

  // Settles by EXECUTION what code-reading alone could not: whether any path
  // other than the hydrate seeds a BACKGROUND pane's count before its first
  // warm. Both other retainers sit after `state.activeSlot !== key` returns.
  it('does not seed a background slot count from the active-slot fetch paths', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [msg('row', '2026-08-13T08:00:00Z', 'm-1')], has_more: false, total: 9,
    })
    // A painted active view, so the refresh is the count-matched BOUNDED read: an
    // unbounded refresh (empty view) leaves no baseline, its raw count being in
    // the wrong units (see retainServerTotal).
    const store = makeStore('active-slot', { messages: [msg('row', '2026-08-13T08:00:00Z', 'm-1')] })
    await store.dispatch(refreshSlot('bg-slot') as never)
    expect(store.getState().chat.slotServerTotal['bg-slot']).toBeUndefined()
    // Positive control: the same fetch DOES seed the slot that is active.
    await store.dispatch(refreshSlot('active-slot') as never)
    expect(store.getState().chat.slotServerTotal['active-slot']).toBe(9)
  })

  // A count taken while the turn is RUNNING is not comparable with a settled
  // one: an unbounded read counts raw rows, so a streaming response is inflated by
  // rows that collapse at turn end. Retaining it makes the next warm read that
  // normal collapse as a truncation, and the suppression then DROPS a live row --
  // the opposite direction to the re-append this baseline exists to prevent.
  it('does not read the resolution of pending cards a running hydrate counted as a rewind', async () => {
    const anchor = msg('anchor', '2026-08-13T08:00:00Z', 'm-1')
    const card = (id: string) => ({ role: 'permission', content: '', cls: '', ts: '2026-08-13T08:30:00Z', meta: { approval_id: id } })
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail,
      messages: [anchor, msg('warm 2', '2026-08-13T09:00:00Z', 'm-2')],
      has_more: false, total: 2, running: false,
    })
    const store = makeStore('active-slot')
    // Hydrate lands mid-turn with two pending cards: the bounded corpus counts
    // them (total 3 = 1 settled row + 2 cards); the baseline must not.
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot', messages: [anchor, card('a-1'), card('a-2')] as never, hasMore: false, bounded: true, total: 3, running: true,
    }))
    expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(1)
    store.dispatch(appendSlotMessage({ slot: 'bg-slot', message: msg('live frame', '2026-08-13T12:00:00Z', 'm-99') as never }))
    await store.dispatch(warmSlotCache('bg-slot') as never)
    // The settled warm reports 2 against a baseline of 1: growth, not a rewind, so
    // the live tail is rescued (the still-pending cards ride along as tail too).
    const held = store.getState().chat.slotMessages['bg-slot'].filter(m => m.role !== 'permission').map(m => m.content)
    expect(held).toEqual(['anchor', 'warm 2', 'live frame'])
  })

  // Boundary of the guard above, so it cannot quietly widen into "never retain":
  // a SETTLED hydrate must still seed the count, which is what keeps the genuine
  // rewind detectable on a pane whose only write was its own hydrate.
  it('still retains a count from a settled hydrate', async () => {
    const anchor = msg('anchor', '2026-08-13T08:00:00Z', 'm-1')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [anchor], has_more: false, total: 1, running: false,
    })
    const store = makeStore('active-slot')
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot', messages: [anchor] as never, hasMore: false, bounded: true, total: 4, running: false,
    }))
    expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(4)
  })

  // #4578 (main): reasoning is broadcast-only and never persisted, so a warm
  // rebuilt from server history holds no thinking row and would drop every block
  // of a slot the user switched away from mid-turn. This shape is the one the
  // reconciliation cannot save on its own -- the block sits AFTER the warm's
  // first row, so no older head covers it and no rescue tail reaches it. The two
  // must compose: the block comes back, anchored on its tool call, exactly once.
  it('keeps a preserved reasoning block the warm cannot cover on its own', async () => {
    const tool1 = { role: 'tool', content: 'first tool', cls: '', ts: '2026-08-13T08:00:00Z', meta: { mid: 'm-1', tool_call_id: 'tc-1' } }
    const tool2 = { role: 'tool', content: 'second tool', cls: '', ts: '2026-08-13T09:00:00Z', meta: { mid: 'm-2', tool_call_id: 'tc-2' } }
    const answer = msg('answer', '2026-08-13T10:00:00Z', 'm-3')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [tool1, tool2, answer], has_more: false, total: 3, running: false,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [tool1, { role: 'thinking', content: 'reasoning', cls: '', ts: '2026-08-13T08:30:00Z' }, tool2, answer] },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot']
    expect(held.filter(m => m.role === 'thinking')).toHaveLength(1)
    expect(held.map(m => m.role)).toEqual(['tool', 'thinking', 'tool', 'assistant'])
  })

  // The bounded and unbounded fetches are separate in-flight requests, so the
  // bounded one can resolve last, be refused here, and still move the baseline.
  it('does not lower the retained count from a hydrate it declines', async () => {
    const anchor = msg('anchor', '2026-08-13T08:00:00Z', 'm-1')
    const store = makeStore('active-slot')
    // Accepted first hydrate settles the baseline at 9.
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot', messages: [anchor] as never, hasMore: false, bounded: true, total: 9, running: false,
    }))
    expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(9)
    // Stale bounded page lands after it and is DECLINED (already hydrated).
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot', messages: [anchor] as never, hasMore: false, bounded: true, total: 3, running: false,
    }))
    expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(9)
  })

  // The warm's total (5) sits BETWEEN the stale 3 and the true 9, so it is a
  // shrink only against the true baseline -- that is what makes this discriminate.
  it('keeps a rewind-discarded turn deleted after a declined hydrate', async () => {
    const anchor = msg('anchor', '2026-08-13T08:00:00Z', 'm-1')
    const forward = msg('forward turn', '2026-08-13T09:00:00Z', 'm-2')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [anchor], has_more: false, total: 5, running: false,
    })
    const store = makeStore('active-slot')
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot', messages: [anchor, forward] as never, hasMore: false, bounded: true, total: 9, running: false,
    }))
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot', messages: [anchor] as never, hasMore: false, bounded: true, total: 3, running: false,
    }))
    // A genuine remote rewind: the server now holds fewer rows than it did at 9.
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).not.toContain('forward turn')
    expect(held).toEqual(['anchor'])
  })

  // An inflated baseline fires the shrink check spuriously and drops a live row,
  // so the rule is "declined is not evidence", not merely "must not lower".
  it('does not raise the retained count from a hydrate it declines', async () => {
    const anchor = msg('anchor', '2026-08-13T08:00:00Z', 'm-1')
    const store = makeStore('active-slot')
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot', messages: [anchor] as never, hasMore: false, bounded: true, total: 9, running: false,
    }))
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot', messages: [anchor] as never, hasMore: false, bounded: true, total: 99, running: false,
    }))
    expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(9)
  })

  // A warm re-derives the marker against the REVIVED array (:4010), so a revived
  // block sits INSIDE the bounded region that this upgrade replaces.
  it('keeps a revived reasoning block when a bounded pane upgrades to the full page', () => {
    const tool1 = { role: 'tool', content: 'first tool', cls: '', ts: '2026-08-13T08:00:00Z', meta: { mid: 'm-1', tool_call_id: 'tc-1' } }
    const thinking = { role: 'thinking', content: 'reasoning', cls: '', ts: '2026-08-13T08:30:00Z' }
    const tool2 = { role: 'tool', content: 'second tool', cls: '', ts: '2026-08-13T09:00:00Z', meta: { mid: 'm-2', tool_call_id: 'tc-2' } }
    const answer = msg('answer', '2026-08-13T10:00:00Z', 'm-3')
    const older = msg('older history', '2026-08-13T07:00:00Z', 'm-0')
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [tool1, thinking, tool2, answer] },
      slotHydrated: { 'bg-slot': true },
      slotPaneBounded: { 'bg-slot': 4 },
      slotPaneHasMore: { 'bg-slot': true },
    })
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot', messages: [older, tool1, tool2, answer] as never, hasMore: false, bounded: false, total: 4, running: false,
    }))
    const held = store.getState().chat.slotMessages['bg-slot']
    expect(held.filter(m => m.role === 'thinking')).toHaveLength(1)
    expect(held.map(m => m.content)).toEqual(['older history', 'first tool', 'reasoning', 'second tool', 'answer'])
  })

  // Boundary that stops the fix widening into "preserve all of prior": the live
  // tail keeps its OWN blocks via `tail`, so merging prior wholesale duplicates them.
  it('does not duplicate a live-tail reasoning block on that upgrade', () => {
    const tool1 = { role: 'tool', content: 'first tool', cls: '', ts: '2026-08-13T08:00:00Z', meta: { mid: 'm-1', tool_call_id: 'tc-1' } }
    const answer = msg('answer', '2026-08-13T09:00:00Z', 'm-2')
    const tailThinking = { role: 'thinking', content: 'live reasoning', cls: '', ts: '2026-08-13T10:00:00Z' }
    const older = msg('older history', '2026-08-13T07:00:00Z', 'm-0')
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [tool1, answer, tailThinking] },
      slotHydrated: { 'bg-slot': true },
      slotPaneBounded: { 'bg-slot': 2 },
      slotPaneHasMore: { 'bg-slot': true },
    })
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot', messages: [older, tool1, answer] as never, hasMore: false, bounded: false, total: 3, running: false,
    }))
    const held = store.getState().chat.slotMessages['bg-slot']
    expect(held.filter(m => m.role === 'thinking')).toHaveLength(1)
    expect(held.map(m => m.content)).toEqual(['older history', 'first tool', 'answer', 'live reasoning'])
  })

  // The block's scan hit a turn-boundary user row — its turn is over — and the
  // unbounded upgrade page covers that row, so the server's full account of the
  // finished turn holds no position for it: dropped (#5815), not tail-appended
  // where it would strand below newer turns. #4218's re-append loop cannot
  // form either way: the upgrade clears the marker, so a later hydrate returns
  // at boundedLen undefined.
  it('drops a stopped-turn orphan from the replaced region once its boundary is covered (#5815)', () => {
    const thinking = { role: 'thinking', content: 'orphan reasoning', cls: '', ts: '2026-08-13T08:00:00Z' }
    const turn = { role: 'user', content: 'do it', cls: '', ts: '2026-08-13T08:30:00Z', meta: { mid: 'm-u' } }
    const answer = msg('answer', '2026-08-13T09:00:00Z', 'm-2')
    const older = msg('older history', '2026-08-13T07:00:00Z', 'm-0')
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [thinking, turn, answer] },
      slotHydrated: { 'bg-slot': true },
      slotPaneBounded: { 'bg-slot': 3 },
      slotPaneHasMore: { 'bg-slot': true },
    })
    store.dispatch(hydrateSlotMessages({
      slot: 'bg-slot', messages: [older, turn, answer] as never, hasMore: false, bounded: false, total: 3, running: false,
    }))
    const held = store.getState().chat.slotMessages['bg-slot']
    expect(held.filter(m => m.role === 'thinking')).toHaveLength(0)
    expect(held.map(m => m.content)).toEqual(['older history', 'do it', 'answer'])
    expect(store.getState().chat.slotPaneBounded['bg-slot']).toBeUndefined()
  })

  // A queued row carries neither mid nor sendId, so the rescue kept it while the
  // warm's own hydrate had already re-added it from the same server queue.
  it('does not duplicate a queued row when it rescues a live tail', async () => {
    const history = msg('history', '2026-08-13T08:00:00Z', 'm-1')
    const live = msg('live frame', '2026-08-13T12:00:00Z', 'm-99')
    const queued = { role: 'queued', content: 'queued A', cls: 'msg msg-queued', ts: '2026-08-13T13:00:00Z', meta: { queueId: 'q-1' } }
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail,
      messages: [history, msg('warm 2', '2026-08-13T09:00:00Z', 'm-2'), msg('warm 3', '2026-08-13T10:00:00Z', 'm-3')],
      queue: [{ content: 'queued A', id: 'q-1' }],
      has_more: true, total: 4,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [history, live, queued] },
      slotServerTotal: { 'bg-slot': 4 },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held.filter(c => c === 'queued A')).toHaveLength(1)
  })

  // OPPOSITE DIRECTION: excluding a queued row from the rescue must not cost the
  // live tail, which is the row the rescue exists to recover.
  it('still rescues a live tail when a queued row is present', async () => {
    const history = msg('history', '2026-08-13T08:00:00Z', 'm-1')
    const live = msg('live frame', '2026-08-13T12:00:00Z', 'm-99')
    const queued = { role: 'queued', content: 'queued A', cls: 'msg msg-queued', ts: '2026-08-13T13:00:00Z', meta: { queueId: 'q-1' } }
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail,
      messages: [history, msg('warm 2', '2026-08-13T09:00:00Z', 'm-2'), msg('warm 3', '2026-08-13T10:00:00Z', 'm-3')],
      queue: [{ content: 'queued A', id: 'q-1' }],
      has_more: true, total: 4,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [history, live, queued] },
      slotServerTotal: { 'bg-slot': 4 },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toContain('live frame')
  })

  // The shared hydrate appends queued rows last, so a rescued live row must not
  // land after them -- that renders the queue above the answer it waits on.
  it('keeps queued rows last when it rescues a live tail', async () => {
    const history = msg('history', '2026-08-13T08:00:00Z', 'm-1')
    const live = msg('live frame', '2026-08-13T12:00:00Z', 'm-99')
    const queued = { role: 'queued', content: 'queued A', cls: 'msg msg-queued', ts: '2026-08-13T13:00:00Z', meta: { queueId: 'q-1' } }
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail,
      messages: [history, msg('warm 2', '2026-08-13T09:00:00Z', 'm-2'), msg('warm 3', '2026-08-13T10:00:00Z', 'm-3')],
      queue: [{ content: 'queued A', id: 'q-1' }],
      has_more: true, total: 4,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [history, live, queued] },
      slotServerTotal: { 'bg-slot': 4 },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toEqual(['history', 'warm 2', 'warm 3', 'live frame', 'queued A'])
  })

  // Two background turns warm concurrently and resolve OUT OF ORDER. The late
  // response's `total` predates the newer turn, so it is not a truncation.
  it('keeps a newer completed turn when an earlier warm resolves last with a lower total', async () => {
    const history = msg('history', '2026-08-13T08:00:00Z', 'm-1')
    const turn2 = msg('turn 2 answer', '2026-08-13T12:00:00Z', 'm-9')
    let resolveFirst: (v: unknown) => void = () => {}
    let resolveSecond: (v: unknown) => void = () => {}
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>)
      .mockImplementationOnce(() => new Promise(r => { resolveFirst = r }))
      .mockImplementationOnce(() => new Promise(r => { resolveSecond = r }))
    const store = makeStore('active-slot', { slotMessages: { 'bg-slot': [history] } })
    const firstWarm = store.dispatch(warmSlotCache('bg-slot') as never)
    const secondWarm = store.dispatch(warmSlotCache('bg-slot') as never)
    // The SECOND turn lands first and is the newest view: total rises to 6.
    resolveSecond({ ...detail, messages: [history, turn2], total: 6, has_more: true })
    await secondWarm
    expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content)).toContain('turn 2 answer')
    // The FIRST turn's response arrives afterwards carrying its older total.
    resolveFirst({ ...detail, messages: [history], total: 5, has_more: true })
    await firstWarm
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).toContain('turn 2 answer')
    // The stale response must not lower the baseline either, or the next warm
    // compares against a count that was never the newest view.
    expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(6)
  })

  // OPPOSITE DIRECTION, and the reason the suppression cannot simply be deleted:
  // a genuine truncation must NOT have its deleted rows resurrected.
  it('still drops rows a genuine truncation removed', async () => {
    const anchor = msg('anchor', '2026-08-13T08:00:00Z', 'm-1')
    const discarded = msg('rewound turn', '2026-08-13T12:00:00Z', 'm-9')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [anchor], total: 1, has_more: false,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [anchor, discarded] },
      slotServerTotal: { 'bg-slot': 2 },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    const held = store.getState().chat.slotMessages['bg-slot'].map(m => m.content)
    expect(held).not.toContain('rewound turn')
  })
  // Only `warmSlotCache` supplies a sequence, so a writer that landed between two
  // warms must not destroy the field the staleness check needs to spare a late
  // EARLIER warm.
  it('keeps the recorded order when an unbounded hydrate lands between two warms', async () => {
    const history = msg('history', '2026-08-13T08:00:00Z', 'm-1')
    const turn2 = msg('turn 2 answer', '2026-08-13T12:00:00Z', 'm-9')
    let resolveFirst: (v: unknown) => void = () => {}
    let resolveSecond: (v: unknown) => void = () => {}
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>)
      .mockImplementationOnce(() => new Promise(r => { resolveFirst = r }))
      .mockImplementationOnce(() => new Promise(r => { resolveSecond = r }))
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [history] },
      slotHydrated: { 'bg-slot': true },
    })
    const firstWarm = store.dispatch(warmSlotCache('bg-slot') as never)
    const secondWarm = store.dispatch(warmSlotCache('bg-slot') as never)
    // The SECOND warm lands first and is the newest view, recording its order.
    resolveSecond({ ...detail, messages: [history, turn2], total: 6, has_more: true })
    await secondWarm
    expect(typeof store.getState().chat.slotServerTotalSeq['bg-slot']).toBe('number')
    // An unbounded pane hydrate for the same slot carries NO ordering token.
    store.dispatch(hydrateSlotMessages({ slot: 'bg-slot', messages: [history, turn2], hasMore: false, total: 7 }))
    expect(typeof store.getState().chat.slotServerTotalSeq['bg-slot']).toBe('number')
    // OPPOSITE DIRECTION: keeping the order must not freeze the count. An unordered
    // response is still the newest view of it and has to land -- in settled units:
    // a complete unbounded read's count is its durable prepared rows (2), never
    // its raw `total` (7, a `done` per finished turn among them).
    expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(2)
    resolveFirst({ ...detail, messages: [history], total: 5, has_more: true })
    await firstWarm
    expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content)).toContain('turn 2 answer')
  })

  // A background `/clear` empties only the ACTIVE slot, so this pane's cache survives
  // and the turn-end warm is the first thing to confirm the shorter history.
  it('drops the pane cache when a confirmed shrink lands on a disjoint newer page', async () => {
    const older = msg('before clear', '2026-08-13T08:00:00Z', 'm-1')
    const cleared = msg('cleared turn', '2026-08-13T09:00:00Z', 'm-2')
    const confirm = msg('conversation cleared', '2026-08-13T12:00:00Z', 'm-9')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [confirm], total: 1, has_more: false,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [older, cleared] },
      slotServerTotal: { 'bg-slot': 2 },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content))
      .toEqual(['conversation cleared'])
  })

  // Same removal through the OTHER branch: rows with no readable order cannot be
  // shown to end before the page, so the merge keeps the longer array instead.
  it('drops the pane cache on a confirmed shrink even when the rows carry no order', async () => {
    const legacyA = { role: 'assistant', content: 'legacy A', cls: '', ts: '' }
    const legacyB = { role: 'assistant', content: 'legacy B', cls: '', ts: '' }
    const confirm = msg('conversation cleared', '2026-08-13T12:00:00Z', 'm-9')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [confirm], total: 1, has_more: false,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [legacyA, legacyB] },
      slotServerTotal: { 'bg-slot': 2 },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content))
      .toEqual(['conversation cleared'])
  })
  // OPPOSITE DIRECTION: the retained head is anchored by identity, so a rewind that
  // removed only NEWER rows must not take its contiguous scrollback with it.
  it('keeps contiguous older scrollback when a shrink removes only newer rows', async () => {
    const x = msg('older x', '2026-08-13T06:00:00Z', 'm-x')
    const y = msg('older y', '2026-08-13T07:00:00Z', 'm-y')
    const a = msg('page a', '2026-08-13T08:00:00Z', 'm-a')
    const b = msg('page b', '2026-08-13T09:00:00Z', 'm-b')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [a, b], total: 4, has_more: true,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [x, y, a, b] },
      // A rewind removed newer rows, so the server's own count fell.
      slotServerTotal: { 'bg-slot': 6 },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content))
      .toEqual(['older x', 'older y', 'page a', 'page b'])
  })
  // OPPOSITE DIRECTION for the order fix: keeping the recorded order must not let a
  // stale one outlive its use, so a LATER warm confirming a real shrink still wins.
  it('still recognises a genuine truncation after an order-free hydrate', async () => {
    const history = msg('history', '2026-08-13T08:00:00Z', 'm-1')
    const turn = msg('removed turn', '2026-08-13T09:00:00Z', 'm-2')
    const confirm = msg('conversation cleared', '2026-08-13T12:00:00Z', 'm-9')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ ...detail, messages: [history, turn], total: 6, has_more: true })
      .mockResolvedValueOnce({ ...detail, messages: [confirm], total: 1, has_more: false })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [history] },
      slotHydrated: { 'bg-slot': true },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    // An order-free writer lands between the two warms.
    store.dispatch(hydrateSlotMessages({ slot: 'bg-slot', messages: [history, turn], hasMore: true, total: 6 }))
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content))
      .toEqual(['conversation cleared'])
  })
  // The recorded order must stay the last ORDERED position, not a high-water guess:
  // a warm dispatched before a later one is not thereby older than an unordered reply.
  it('still recognises a truncation reported by a warm that is not the newest dispatched', async () => {
    const history = msg('history', '2026-08-13T08:00:00Z', 'm-1')
    const keep = msg('kept turn', '2026-08-13T09:00:00Z', 'm-2')
    const confirm = msg('conversation cleared', '2026-08-13T12:00:00Z', 'm-9')
    let resolveA: (v: unknown) => void = () => {}
    let resolveB: (v: unknown) => void = () => {}
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>)
      .mockImplementationOnce(() => new Promise(r => { resolveA = r }))
      .mockImplementationOnce(() => new Promise(r => { resolveB = r }))
      .mockImplementationOnce(() => new Promise(() => {}))
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [history, keep] },
      slotHydrated: { 'bg-slot': true },
    })
    const a = store.dispatch(warmSlotCache('bg-slot') as never)
    const b = store.dispatch(warmSlotCache('bg-slot') as never)
    store.dispatch(warmSlotCache('bg-slot') as never)   // dispatched, never resolves
    resolveA({ ...detail, messages: [history, keep], total: 10, has_more: true })
    await a
    // An order-free writer lands while a later warm is still in flight.
    store.dispatch(hydrateSlotMessages({ slot: 'bg-slot', messages: [history, keep], hasMore: true, total: 10 }))
    // B is not the newest DISPATCHED warm, but its shorter history is real.
    resolveB({ ...detail, messages: [confirm], total: 4, has_more: false })
    await b
    expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content))
      .toEqual(['conversation cleared'])
  })
  // A remote regenerate REPLACES the reply, so the server's count never moves.
  // The superseded row is absent from the warm page, which reads as "newer".
  it('does not reappend a superseded reply when a rewrite leaves the count unchanged', async () => {
    const question = msg('question', '2026-08-13T08:00:00Z', 'm-1')
    const oldReply = msg('old reply', '2026-08-13T09:00:00Z', 'm-2')
    const newReply = msg('new reply', '2026-08-13T09:30:00Z', 'm-3')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [question, newReply], total: 2, has_more: false,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [question, oldReply] },
      slotServerTotal: { 'bg-slot': 2 },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content))
      .toEqual(['question', 'new reply'])
  })

  // OPPOSITE DIRECTION: an unchanged count is ALSO what a locally-sent row shows
  // before it persists, and that tail is genuinely newer -- it must survive.
  it('keeps a genuinely newer row when the count is unchanged', async () => {
    const question = msg('question', '2026-08-13T08:00:00Z', 'm-1')
    const reply = msg('reply', '2026-08-13T09:00:00Z', 'm-2')
    const justSent = msg('just sent', '2026-08-13T10:00:00Z', 'm-9')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...detail, messages: [question, reply], total: 2, has_more: false,
    })
    const store = makeStore('active-slot', {
      slotMessages: { 'bg-slot': [question, reply, justSent] },
      slotServerTotal: { 'bg-slot': 2 },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(store.getState().chat.slotMessages['bg-slot'].map(m => m.content))
      .toEqual(['question', 'reply', 'just sent'])
  })
})
