/**
 * #11139: a non-active slot's cached transcript (`slotMessages`) must stay
 * bounded while no pane has hydrated it. A background turn appends rows each
 * cycle, so without a bound a monitor loop grows the Redux store forever. These
 * tests pin the bound on every way such a slot is fed (live frames only, a warm
 * after each background `chat_done`, a switch-away park), that a trim never
 * writes a cursor it cannot place, that a pane on screen is left whole, that a
 * pane which unmounts releases its slot to the bound, and that
 * no writer can replace a slot's array behind the bound.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { configureStore } from '@reduxjs/toolkit'

type Row = { role: string; content: string; cls: string; ts: string; meta: { mid: string } }

/** Persisted history per slot. The mock answers like the handler: `before` is
 *  an ABSOLUTE row index into the session, `next_before` is the page's start. */
let CORPUS: Record<string, Row[]> = {}

vi.mock('../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn((slot: string, limit?: number, before?: number) => {
      const rows = CORPUS[slot] ?? []
      const total = rows.length
      const end = before !== undefined ? Math.max(0, Math.min(before, total)) : total
      const start = limit === undefined ? 0 : Math.max(0, end - limit)
      return Promise.resolve({
        messages: rows.slice(start, end), has_more: start > 0, total, next_before: start, running: false,
      })
    }),
  },
}))

import chatReducer, {
  setActiveSlot, sseChatMessage, sseThinkingChunk, loadOlderMessages, loadOlderSlotMessages, hydrateSlotMessages, switchSlot, warmSlotCache,
  clearSlotHydrated, holdHydratedSlotPane, replaceMessages,
} from './chatSlice'
import dashboardReducer from './dashboardSlice'
import { SLOT_RETAIN_LIMIT } from './chat/slotCache'
import { api } from '../api/client'

const ACTIVE = 'chat-active'
const BG = 'chat-bg'
const GEN = 'gateway-a'

function makeStore() {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
  store.dispatch(setActiveSlot(ACTIVE))
  return store
}
type Store = ReturnType<typeof makeStore>
type Chat = ReturnType<typeof chatReducer>
const chat = (store: Store) => store.getState().chat
const rows = (s: Chat, slot = BG) => s.slotMessages[slot] ?? []
const mids = (s: Chat, slot = BG) => rows(s, slot).map(m => m.meta?.mid)

const row = (role: string, content: string, mid: string, i: number): Row => ({
  role, content, cls: role === 'user' ? 'msg msg-u' : 'msg msg-a',
  ts: new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString(), meta: { mid },
})

/** Seed `n` rows of history the server already holds for `slot`. */
function seedHistory(slot: string, n: number) {
  CORPUS[slot] = Array.from({ length: n }, (_, i) =>
    row(i % 2 === 0 ? 'user' : 'assistant', `h${i}`, `h-${slot}-${i}`, i))
}

/** Drive one completed background turn: a user row, an assistant answer and the
 *  identified `_done`, each persisted server-side as it would be. Distinct
 *  `mid`/`turn` per turn so the redelivery guard never collapses them. */
function backgroundTurn(store: Store, n: number, slot = BG, big = false) {
  const corpus = (CORPUS[slot] ??= [])
  const u = row('user', `q${n}`, `u-${slot}-${n}`, 10_000 + 2 * n)
  const a = row('assistant', big ? 'x'.repeat(4096) : `a${n}`, `a-${slot}-${n}`, 10_001 + 2 * n)
  corpus.push(u, a)
  store.dispatch(sseChatMessage({ slot, role: 'user', content: u.content, meta: { ...u.meta } }))
  store.dispatch(sseChatMessage({ slot, role: 'assistant', content: a.content, meta: { ...a.meta } }))
  store.dispatch(sseChatMessage({ slot, role: '_done', content: '', turn: n, turn_gen: GEN }))
}

/** The held rows must be ONE contiguous run of the server's history, starting
 *  at the cursor when the pane has one: no gap, no reorder, no duplicate. */
function expectContiguousFromCursor(s: Chat, slot = BG) {
  const held = mids(s, slot)
  const corpus = (CORPUS[slot] ?? []).map(r => r.meta.mid)
  const start = corpus.indexOf(held[0] as string)
  expect(start).toBeGreaterThanOrEqual(0)
  expect(held).toEqual(corpus.slice(start, start + held.length))
  const hasMore = s.slotPaneHasMore?.[slot]
  if (hasMore === true) expect(s.slotPaneNextBefore?.[slot]).toBe(start)
  // A pane that says it holds the whole history must start at row 0.
  if (hasMore === false) expect(start).toBe(0)
}

beforeEach(() => {
  vi.clearAllMocks()
  CORPUS = {}
})

describe('#11139 slotMessages retention bound', () => {
  it('a never-hydrated background slot is bounded and its head stays undescribed', () => {
    const store = makeStore()
    const turns = SLOT_RETAIN_LIMIT + 10
    for (let n = 1; n <= turns; n++) backgroundTurn(store, n)

    const held = rows(chat(store))
    expect(held.length).toBeLessThanOrEqual(SLOT_RETAIN_LIMIT)
    expect(held.at(-1)?.content).toBe(`a${turns}`)
    // Nothing here knows where these rows start in the session, so the trim
    // writes no marker and no cursor for a page-back to misplace.
    expect(chat(store).slotPaneHasMore?.[BG]).toBeUndefined()
    expect(chat(store).slotPaneNextBefore?.[BG]).toBeUndefined()
    expect(chat(store).slotPaneBounded?.[BG]).toBeUndefined()
  })

  it('stays bounded when the background chat_done warm runs between turns', async () => {
    seedHistory(BG, 40)
    const store = makeStore()
    const turns = SLOT_RETAIN_LIMIT + 10
    for (let n = 1; n <= turns; n++) {
      backgroundTurn(store, n)
      // turnCompletion.ts warms every background slot on its chat_done.
      if (n % 7 === 0) await store.dispatch(warmSlotCache(BG))
    }
    await store.dispatch(warmSlotCache(BG))
    backgroundTurn(store, turns + 1)

    expect(api.chatSlotDetail).toHaveBeenCalled()
    expect(rows(chat(store)).length).toBeLessThanOrEqual(SLOT_RETAIN_LIMIT)
    // The warm placed the head with a server cursor; each later trim moves that
    // cursor past exactly the rows it dropped.
    expectContiguousFromCursor(chat(store))
  })

  it('stays bounded after the reader viewed the slot and switched away (park)', async () => {
    seedHistory(BG, 300)
    const store = makeStore()
    await store.dispatch(switchSlot(BG))
    await store.dispatch(switchSlot(ACTIVE))
    expect(chat(store).slotPaneHasMore?.[BG]).toBe(true)

    for (let n = 1; n <= SLOT_RETAIN_LIMIT + 10; n++) backgroundTurn(store, n)
    expect(rows(chat(store)).length).toBeLessThanOrEqual(SLOT_RETAIN_LIMIT)
    expectContiguousFromCursor(chat(store))

    // Paging back fetches exactly the rows above the kept head.
    const before = chat(store).slotPaneNextBefore?.[BG] ?? 0
    await store.dispatch(loadOlderSlotMessages(BG) as never)
    expect(api.chatSlotDetail).toHaveBeenLastCalledWith(BG, expect.any(Number), before)
    expectContiguousFromCursor(chat(store))
  })

  it('a failed switch away restores the origin cursor the trim moved, so paging back skips no row', async () => {
    seedHistory(BG, 300)
    const store = makeStore()
    await store.dispatch(switchSlot(BG))
    expect(chat(store).slotHasMore).toBe(true)

    // The reader leaves BG for a slot that turns out to be gone. `pending`
    // snapshots BG's cursor as the switch origin and parks BG with a described
    // head; the fetch never lands, so BG's background turns end in its window.
    const GONE = 'chat-gone'
    store.dispatch(switchSlot.pending('req-gone', GONE))
    expect(chat(store).slotSwitchOrigin?.key).toBe(BG)
    for (let n = 1; n <= 10; n++) backgroundTurn(store, n)
    expect(rows(chat(store)).length).toBeLessThanOrEqual(SLOT_RETAIN_LIMIT)
    expectContiguousFromCursor(chat(store))

    // The target 404s: the rejected reducer puts BG back with its snapshot cursor.
    store.dispatch(switchSlot.rejected(null, 'req-gone', GONE, { status: 404, message: 'slot unavailable' }))
    const s = chat(store)
    expect(s.activeSlot).toBe(BG)
    const start = CORPUS[BG].findIndex(r => r.meta.mid === s.messages[0]?.meta?.mid)
    expect(start).toBeGreaterThan(0)
    // The restored cursor addresses the first dropped row, not the pre-trim head.
    expect(s.slotHasMore).toBe(true)
    expect(s.slotOldestIndex).toBe(start)
    expect(s.slotCursorKey).toBe(BG)

    await store.dispatch(loadOlderMessages() as never)
    expect(api.chatSlotDetail).toHaveBeenLastCalledWith(BG, expect.any(Number), start, expect.anything())
    const held = chat(store).messages.map(m => m.meta?.mid)
    const corpus = CORPUS[BG].map(r => r.meta.mid)
    const first = corpus.indexOf(held[0] as string)
    expect(held).toEqual(corpus.slice(first, first + held.length))
  })

  it('a trimmed live-only slot opened in a pane takes the server page with no gap and no duplicate', () => {
    seedHistory(BG, 40)
    const store = makeStore()
    const turns = SLOT_RETAIN_LIMIT + 10
    for (let n = 1; n <= turns; n++) backgroundTurn(store, n)
    // One more reply streams in after the server snapshot below was taken.
    const snapshot = CORPUS[BG].slice()
    backgroundTurn(store, turns + 1)
    expect(rows(chat(store)).length).toBeLessThanOrEqual(SLOT_RETAIN_LIMIT)

    // The pane's hydrate still runs (the trim left the head undescribed) and
    // prepends the authoritative history, restoring every dropped row.
    store.dispatch(hydrateSlotMessages({
      slot: BG, messages: snapshot, hasMore: false, bounded: false, total: snapshot.length, running: false,
    }))
    const held = mids(chat(store))
    expect(new Set(held).size).toBe(held.length)
    expect(held).toEqual(CORPUS[BG].map(r => r.meta.mid))
    expect(chat(store).slotPaneHasMore?.[BG]).toBe(false)
  })

  it('leaves a hydrated pane whole across background turns', () => {
    seedHistory(BG, SLOT_RETAIN_LIMIT + 250)
    const store = makeStore()
    store.dispatch(hydrateSlotMessages({
      slot: BG, messages: CORPUS[BG].slice(), hasMore: false, bounded: false,
      total: CORPUS[BG].length, running: false,
    }))
    expect(chat(store).slotHydrated?.[BG]).toBe(true)

    for (let n = 1; n <= 40; n++) backgroundTurn(store, n)
    expect(mids(chat(store))).toEqual(CORPUS[BG].map(r => r.meta.mid))
  })

  it('keeps aggregate content.length across slotMessages under budget with 8 open slots each receiving 200 large frames', () => {
    const store = makeStore()
    for (let s = 0; s < 8; s++) {
      const slot = `bg-${s}`
      for (let n = 1; n <= 200; n++) backgroundTurn(store, n, slot, /* big */ true)
    }
    const st = chat(store)
    let aggregate = 0
    let maxRows = 0
    for (const slot of Object.keys(st.slotMessages)) {
      const arr = st.slotMessages[slot] ?? []
      maxRows = Math.max(maxRows, arr.length)
      for (const m of arr) aggregate += (m.content ?? '').length
    }
    // Each slot is capped at the window, so the aggregate is at most
    // 8 slots * N rows * 4096 bytes -- bounded, not 8 * 200 * 4096.
    expect(maxRows).toBeLessThanOrEqual(SLOT_RETAIN_LIMIT)
    expect(aggregate).toBeLessThanOrEqual(8 * SLOT_RETAIN_LIMIT * 4096)
  })
})

describe('#11139 a pane that mounted and then left is bounded again', () => {
  /** What a mounting ChatPane does: flag the held slot, then hydrate the
   *  bounded page the server answers NOW (the pane drops its cached page on
   *  unmount, so a remount reads the server again). */
  async function mountPane(store: Store) {
    store.dispatch(holdHydratedSlotPane(BG))
    const page = await api.chatSlotDetail(BG, SLOT_RETAIN_LIMIT)
    store.dispatch(hydrateSlotMessages({
      slot: BG, messages: page.messages, hasMore: page.has_more, bounded: true,
      total: page.total, running: false, nextBefore: page.next_before,
    }))
  }

  it('clears the flag on unmount, bounds the backgrounded slot, and a remount pages back to the whole history', async () => {
    seedHistory(BG, 120)
    const store = makeStore()
    await mountPane(store)
    expect(chat(store).slotHydrated?.[BG]).toBe(true)
    // While the pane is mounted nothing is trimmed.
    for (let n = 1; n <= 10; n++) backgroundTurn(store, n)
    expect(rows(chat(store)).length).toBe(SLOT_RETAIN_LIMIT + 20)

    // (i) Unmount releases the slot.
    store.dispatch(clearSlotHydrated(BG))
    expect(chat(store).slotHydrated?.[BG]).toBeFalsy()

    // (ii) Background turns past the bound now trim it: the monitor-pane leak.
    for (let n = 11; n <= SLOT_RETAIN_LIMIT + 30; n++) backgroundTurn(store, n)
    expect(rows(chat(store)).length).toBeLessThanOrEqual(SLOT_RETAIN_LIMIT)
    expect(chat(store).slotPaneHasMore?.[BG]).toBe(true)
    expectContiguousFromCursor(chat(store))

    // (iii) Remount: the fresh page is newest-N, so the pane must keep saying
    // older rows exist, at a cursor that addresses the trimmed head exactly.
    await mountPane(store)
    expect(chat(store).slotHydrated?.[BG]).toBe(true)
    expect(chat(store).slotPaneHasMore?.[BG]).toBe(true)
    expectContiguousFromCursor(chat(store))
    for (let i = 0; i < 20 && chat(store).slotPaneHasMore?.[BG]; i++) {
      await store.dispatch(loadOlderSlotMessages(BG) as never)
    }
    expect(chat(store).slotPaneHasMore?.[BG]).toBe(false)
    expect(mids(chat(store))).toEqual(CORPUS[BG].map(r => r.meta.mid))
  })

  it('flags a remounted pane before its hydrate lands, so a turn ending meanwhile trims nothing on screen', () => {
    seedHistory(BG, 120)
    const store = makeStore()
    store.dispatch(hydrateSlotMessages({
      slot: BG, messages: CORPUS[BG].slice(), hasMore: false, bounded: false,
      total: CORPUS[BG].length, running: false,
    }))
    store.dispatch(clearSlotHydrated(BG))
    // Remounted; its hydrate fetch has not answered yet.
    store.dispatch(holdHydratedSlotPane(BG))
    expect(chat(store).slotHydrated?.[BG]).toBe(true)
    for (let n = 1; n <= 5; n++) backgroundTurn(store, n)
    expect(mids(chat(store))).toEqual(CORPUS[BG].map(r => r.meta.mid))
  })

  it('leaves a never-described slot to its hydrate, which still places the server page', () => {
    seedHistory(BG, 40)
    const store = makeStore()
    store.dispatch(holdHydratedSlotPane(BG))
    expect(chat(store).slotHydrated?.[BG]).toBeFalsy()
    store.dispatch(hydrateSlotMessages({
      slot: BG, messages: CORPUS[BG].slice(), hasMore: false, bounded: false, total: 40, running: false,
    }))
    expect(mids(chat(store))).toEqual(CORPUS[BG].map(r => r.meta.mid))
  })

  it('holds a split pane on the active slot once the reader switches away with the grid mounted', () => {
    // A split pane shows ACTIVE while it is the active slot: the hold is refused
    // there (the active mirror is live), so nothing flags the slot yet.
    seedHistory(ACTIVE, SLOT_RETAIN_LIMIT + 20)
    const store = makeStore()
    store.dispatch(replaceMessages(CORPUS[ACTIVE].slice()))
    store.dispatch(holdHydratedSlotPane(ACTIVE))
    expect(chat(store).slotHydrated?.[ACTIVE]).toBeFalsy()

    // The reader switches to BG; ACTIVE is parked and its pane is still on
    // screen, so the pane's hold runs again now that the slot is background.
    store.dispatch(setActiveSlot(BG))
    store.dispatch(holdHydratedSlotPane(ACTIVE))
    expect(chat(store).slotHydrated?.[ACTIVE]).toBe(true)

    backgroundTurn(store, 1, ACTIVE)
    expect(mids(chat(store), ACTIVE)).toEqual(CORPUS[ACTIVE].map(r => r.meta.mid))
  })

  it('a remount whose whole-history read follows a trim places the full history and keeps the live tail', () => {
    seedHistory(BG, 120)
    const store = makeStore()
    store.dispatch(hydrateSlotMessages({
      slot: BG, messages: CORPUS[BG].slice(), hasMore: false, bounded: false,
      total: CORPUS[BG].length, running: false,
    }))
    store.dispatch(clearSlotHydrated(BG))
    // Patrol turns push every earlier row out of the kept window.
    const turns = SLOT_RETAIN_LIMIT + 10
    for (let n = 1; n <= turns; n++) backgroundTurn(store, n)
    expect(rows(chat(store)).length).toBeLessThanOrEqual(SLOT_RETAIN_LIMIT)
    expect(chat(store).slotPaneBounded?.[BG]).toBeUndefined()
    expect(chat(store).slotPaneHasMore?.[BG]).toBe(true)

    // The pane remounts and reads the whole transcript (the crewmate pane's
    // unbounded read). One more reply streams in after that snapshot.
    store.dispatch(holdHydratedSlotPane(BG))
    expect(chat(store).slotHydrated?.[BG]).toBe(true)
    const snapshot = CORPUS[BG].slice()
    backgroundTurn(store, turns + 1)
    store.dispatch(hydrateSlotMessages({
      slot: BG, messages: snapshot, hasMore: false, bounded: false,
      total: snapshot.length, running: false, nextBefore: 0,
    }))

    const held = mids(chat(store))
    expect(new Set(held).size).toBe(held.length)
    expect(held).toEqual(CORPUS[BG].map(r => r.meta.mid))
    expect(held.at(-1)).toBe(`a-${BG}-${turns + 1}`)
    expect(chat(store).slotPaneHasMore?.[BG]).toBe(false)
    expect(chat(store).slotPaneBounded?.[BG]).toBeUndefined()
  })
})

describe('#11139 a head trim never loses client-only reasoning', () => {
  /** Two turns the reader watched live on BG, each `[user, thinking, assistant]`,
   *  then parked by switching away. Six rows, so with two rows per background
   *  turn the trims cut `(uA, TA)`, `(aA, uB)`, `(TB, aB)`: the first trim drops
   *  TA while its anchor aA SURVIVES, the third drops TB together with aB. */
  function watchedThenParked(): Store {
    const store = makeStore()
    store.dispatch(setActiveSlot(BG))
    for (const t of ['A', 'B']) {
      const i = t === 'A' ? 0 : 2
      const u = row('user', `q${t}`, `u-${t}`, i)
      const a = row('assistant', `answer ${t}`, `a-${t}`, i + 1)
      CORPUS[BG] = [...(CORPUS[BG] ?? []), u, a]
      store.dispatch(sseChatMessage({ slot: BG, ...u, meta: { ...u.meta } }))
      store.dispatch(sseThinkingChunk({ slot: BG, content: `reasoning ${t}` }))
      store.dispatch(sseChatMessage({ slot: BG, role: 'chunk', content: a.content }))
      store.dispatch(sseChatMessage({ slot: BG, ...a, meta: { ...a.meta } }))
    }
    store.dispatch(setActiveSlot(ACTIVE))
    expect(rows(chat(store)).map(m => m.role)).toEqual(['user', 'thinking', 'assistant', 'user', 'thinking', 'assistant'])
    expect(chat(store).slotHydrated?.[BG]).toBeFalsy()
    return store
  }
  const runTurns = (store: Store, from: number, to: number) => {
    for (let n = from; n <= to; n++) backgroundTurn(store, n)
  }
  /** Every copy of a reasoning block the slot holds: in its window or parked. */
  const copies = (s: Chat, content: string) =>
    rows(s).filter(m => m.role === 'thinking' && m.content === content).length
    + (s.thinkingOrphans?.[BG] ?? []).filter(p => p.msg.content === content).length

  /** The reasoning sits directly above its answer, exactly once, in `list`. */
  const expectSeatedAbove = (list: Chat['messages'], content: string, mid: string) => {
    const at = list.findIndex(m => m.meta?.mid === mid)
    expect(at).toBeGreaterThan(0)
    expect(list[at - 1]).toMatchObject({ role: 'thinking', content })
    expect(list.filter(m => m.content === content)).toHaveLength(1)
  }

  it('keeps a dropped block whose anchor survives the trim inline, above its anchor, not parked', async () => {
    const store = watchedThenParked()
    runTurns(store, 1, 48)
    const held = rows(chat(store))
    // The cut fell between TA and its anchor: aA is kept, so TA stays in the
    // window directly above it rather than being parked.
    expect(held.map(m => m.meta?.mid)).toContain('a-A')
    expectSeatedAbove(held, 'reasoning A', 'a-A')
    expect(copies(chat(store), 'reasoning A')).toBe(1)
    expect(chat(store).thinkingOrphans?.[BG]?.some(p => p.msg.content === 'reasoning A') ?? false).toBe(false)

    // Switching in shows it once, still above its anchor.
    await store.dispatch(switchSlot(BG))
    expectSeatedAbove(chat(store).messages, 'reasoning A', 'a-A')
    expect(chat(store).thinkingOrphans?.[BG]?.some(p => p.msg.content === 'reasoning A') ?? false).toBe(false)
  })

  it('a grid pane mounting the trimmed slot shows the kept anchor WITH its reasoning', () => {
    // Opus finding on PR #22640: a parked block re-seats only on the active
    // paths, and a grid pane's hydrate returns early on the defined marker, so
    // a parked kept-anchor block left that pane showing the answer bare.
    const store = watchedThenParked()
    runTurns(store, 1, 48)
    expect(chat(store).slotPaneHasMore?.[BG]).toBeDefined()
    store.dispatch(hydrateSlotMessages({
      slot: BG, messages: CORPUS[BG].slice(-SLOT_RETAIN_LIMIT), hasMore: true, bounded: true,
      total: CORPUS[BG].length, running: false, nextBefore: CORPUS[BG].length - SLOT_RETAIN_LIMIT,
    }))
    expect(chat(store).activeSlot).toBe(ACTIVE)
    expectSeatedAbove(rows(chat(store)), 'reasoning A', 'a-A')
  })

  it('parks the inline block once its anchor is dropped too, then re-seats it on switch-in', async () => {
    const store = watchedThenParked()
    runTurns(store, 1, 49)
    const s = chat(store)
    expect(rows(s).map(m => m.meta?.mid)).not.toContain('a-A')
    expect(copies(s, 'reasoning A')).toBe(1)
    const rec = (s.thinkingOrphans?.[BG] ?? []).find(p => p.msg.content === 'reasoning A')
    expect(rec?.anchor.mid).toBe('a-A')

    // Switching in shows the newest page; paging back loads aA again, and the
    // parked block goes back directly above it and leaves the parking map.
    await store.dispatch(switchSlot(BG))
    await store.dispatch(loadOlderMessages() as never)
    expectSeatedAbove(chat(store).messages, 'reasoning A', 'a-A')
    expect(chat(store).thinkingOrphans?.[BG]?.some(p => p.msg.content === 'reasoning A') ?? false).toBe(false)
  })

  it('a split pane paging back to a dropped anchor re-seats its reasoning without activating the slot', async () => {
    // GPT finding on PR #22640: the background-pane walk prepended the older
    // page but never re-seated parked blocks, so a split pane showed the
    // restored answer bare until the slot was made active.
    const store = watchedThenParked()
    runTurns(store, 1, 49)
    expect(rows(chat(store)).map(m => m.meta?.mid)).not.toContain('a-A')
    expect((chat(store).thinkingOrphans?.[BG] ?? []).some(p => p.msg.content === 'reasoning A')).toBe(true)

    // A split pane mounts the slot: its hydrate returns early on the defined
    // marker, then the pane pages back to the rows the trim dropped.
    store.dispatch(hydrateSlotMessages({
      slot: BG, messages: CORPUS[BG].slice(-SLOT_RETAIN_LIMIT), hasMore: true, bounded: true,
      total: CORPUS[BG].length, running: false, nextBefore: CORPUS[BG].length - SLOT_RETAIN_LIMIT,
    }))
    await store.dispatch(loadOlderSlotMessages(BG) as never)

    const s = chat(store)
    expect(s.activeSlot).toBe(ACTIVE)
    expect(s.slotPaneHasMore?.[BG]).toBe(false)
    expectSeatedAbove(rows(s), 'reasoning A', 'a-A')
    expect(copies(s, 'reasoning A')).toBe(1)
    expect(s.thinkingOrphans?.[BG] ?? []).toEqual([])
    // Re-seating inserts only the client-only row: the durable rows stay one
    // contiguous run of the server's history.
    expect(rows(s).filter(m => m.role !== 'thinking').map(m => m.meta?.mid))
      .toEqual(CORPUS[BG].map(r => r.meta.mid))
  })

  it('a split pane paging back over a BOUNDED page counts a re-seated block inside the bounded head', async () => {
    // The bounded length is an index into the pane's array; a block re-seated
    // inside that leading region must extend it, or the later unbounded upgrade
    // splits the array too early.
    //
    // The parked blocks are made while the slot's head is UNDESCRIBED, so the
    // pane's bounded hydrate still runs and records `slotPaneBounded`. That
    // takes a provisional park: the reader opened BG, the turns streamed while
    // its switch fetch was in flight, and the reader left before it landed. The
    // switch's `pending` is dispatched by hand so the fetch never lands.
    const store = makeStore()
    store.dispatch(switchSlot.pending('req-bg', BG))
    for (const t of ['A', 'B']) {
      const i = t === 'A' ? 0 : 2
      const u = row('user', `q${t}`, `u-${t}`, i)
      const a = row('assistant', `answer ${t}`, `a-${t}`, i + 1)
      CORPUS[BG] = [...(CORPUS[BG] ?? []), u, a]
      store.dispatch(sseChatMessage({ slot: BG, ...u, meta: { ...u.meta } }))
      store.dispatch(sseThinkingChunk({ slot: BG, content: `reasoning ${t}` }))
      store.dispatch(sseChatMessage({ slot: BG, role: 'chunk', content: a.content }))
      store.dispatch(sseChatMessage({ slot: BG, ...a, meta: { ...a.meta } }))
    }
    store.dispatch(setActiveSlot(ACTIVE))
    expect(rows(chat(store)).map(m => m.role)).toEqual(['user', 'thinking', 'assistant', 'user', 'thinking', 'assistant'])
    expect(chat(store).slotPaneHasMore?.[BG]).toBeUndefined()

    // Trims park both blocks and leave the head undescribed.
    runTurns(store, 1, 50)
    expect(chat(store).slotPaneHasMore?.[BG]).toBeUndefined()
    expect(chat(store).slotPaneBounded?.[BG]).toBeUndefined()
    expect((chat(store).thinkingOrphans?.[BG] ?? []).map(p => p.anchor.mid).sort()).toEqual(['a-A', 'a-B'])

    // The pane's bounded page predates the newest turn, whose two rows stay as
    // the live tail after the page.
    const total = CORPUS[BG].length
    const snapshot = CORPUS[BG].slice(0, total - 2)
    const page = snapshot.slice(-SLOT_RETAIN_LIMIT)
    const pageStart = snapshot.length - SLOT_RETAIN_LIMIT
    store.dispatch(hydrateSlotMessages({
      slot: BG, messages: page, hasMore: true, bounded: true,
      total: snapshot.length, running: false, nextBefore: pageStart,
    }))
    expect(chat(store).slotPaneBounded?.[BG]).toBe(SLOT_RETAIN_LIMIT)
    expect(copies(chat(store), 'reasoning B')).toBe(1)
    expect((chat(store).thinkingOrphans?.[BG] ?? []).some(p => p.anchor.mid === 'a-B')).toBe(true)

    // The older page is the head of the session: it brings back a-A, the
    // final page seats both blocks, and both land inside the bounded head
    // (a-B was already in the bounded page).
    await store.dispatch(loadOlderSlotMessages(BG) as never)
    const s = chat(store)
    expect(s.activeSlot).toBe(ACTIVE)
    expect(s.slotPaneHasMore?.[BG]).toBe(false)
    const list = rows(s)
    // (a) each block seated once, directly above its anchor.
    for (const t of ['A', 'B']) {
      expectSeatedAbove(list, `reasoning ${t}`, `a-${t}`)
      expect(copies(s, `reasoning ${t}`)).toBe(1)
    }
    // (b) both left the parking map.
    expect(s.thinkingOrphans?.[BG] ?? []).toEqual([])
    // (c) the head counts the prepended rows AND both seated rows: it ends on
    // the bounded page's last row and the live tail starts right after it.
    const bounded = s.slotPaneBounded?.[BG] as number
    expect(bounded).toBe(pageStart + page.length + 2)
    expect(list[bounded - 1]?.meta?.mid).toBe(page.at(-1)?.meta.mid)
    expect(list[bounded]?.meta?.mid).toBe(CORPUS[BG][total - 2].meta.mid)
    expect(list.slice(bounded).map(m => m.meta?.mid)).toEqual(CORPUS[BG].slice(total - 2).map(r => r.meta.mid))

    // (d) the unbounded upgrade replaces the head and keeps the live tail,
    // with no row dropped or duplicated and each block still above its anchor.
    store.dispatch(hydrateSlotMessages({
      slot: BG, messages: snapshot.slice(), hasMore: false, bounded: false,
      total: snapshot.length, running: false,
    }))
    const after = rows(chat(store))
    const durable = after.filter(m => m.role !== 'thinking').map(m => m.meta?.mid)
    expect(new Set(durable).size).toBe(durable.length)
    expect(durable).toEqual(CORPUS[BG].map(r => r.meta.mid))
    for (const t of ['A', 'B']) expectSeatedAbove(after, `reasoning ${t}`, `a-${t}`)
    expect(after.length).toBe(CORPUS[BG].length + 2)
    expect(chat(store).slotPaneBounded?.[BG]).toBeUndefined()
  })

  it('parks a dropped block whose anchor is dropped with it, once per block', () => {
    const store = watchedThenParked()
    runTurns(store, 1, 50)
    const s = chat(store)
    expect(rows(s).length).toBeLessThanOrEqual(SLOT_RETAIN_LIMIT)
    expect(rows(s).map(m => m.meta?.mid)).not.toContain('a-B')
    for (const t of ['A', 'B']) {
      expect(copies(s, `reasoning ${t}`)).toBe(1)
      const rec = (s.thinkingOrphans?.[BG] ?? []).find(p => p.msg.content === `reasoning ${t}`)
      expect(rec?.anchor.mid).toBe(`a-${t}`)
    }
  })
})

describe('#11139 no writer replaces a slot array outside the bounded helper', () => {
  it('only writeSlotPage and the documented empty-queue seed assign slotMessages[...] a value', () => {
    // A ratchet: a NEW path that replaces a slot's transcript array behind the
    // bound re-opens the unbounded-retention bug. Seeds (`??= []`) and reads
    // are allowed; only a replacement assignment is policed, and it must live
    // in writeSlotPage (the single sanctioned array writer) or be the
    // documented empty-array seed in queue.ts.
    const here = dirname(fileURLToPath(import.meta.url))
    const files = ['chatSlice.ts', 'chat/slotCache.ts', 'chat/messages.ts', 'chat/activity.ts', 'chat/queue.ts', 'chat/runState.ts', 'chat/slotRefresh.ts', 'chat/slotSwitch.ts']
    // Matches `slotMessages[...] =` but not `??=`, `===`, `==`, `<=`, `>=`.
    const assign = /slotMessages\[[^\]]*\]\s*=(?![=])/
    const offenders: string[] = []
    for (const rel of files) {
      const text = readFileSync(join(here, rel), 'utf8')
      text.split('\n').forEach((line, i) => {
        if (line.includes('??=')) return
        if (!assign.test(line)) return
        const sanctioned =
          (rel === 'chat/slotCache.ts') || // writeSlotPage
          (rel === 'chat/queue.ts' && line.includes('= []')) // documented empty-queue seed
        if (!sanctioned) offenders.push(`${rel}:${i + 1}: ${line.trim()}`)
      })
    }
    expect(offenders).toEqual([])
  })
})
