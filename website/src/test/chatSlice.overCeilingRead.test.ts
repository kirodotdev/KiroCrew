/** A pane or view holding more rows than the page ceiling must not refetch the whole
 *  transcript.
 *
 *  A long chat paged back through "load earlier" holds more rows than the count-
 *  matched bound's ceiling, so `countMatchedFetchLimit` declines -- and both the
 *  background warm (`warmSlotCache`) and the open pane's refresh (`refreshSlot`) took
 *  the unbounded read. For a long-running session that is the entire multi-MB
 *  transcript downloaded and parsed into whatever tab is open, on every turn end.
 *  Both now ask for the newest ceiling-many rows and keep the rest as a head, falling
 *  back to the unbounded read only when that page cannot be merged.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import { api } from '../api/client'
import * as slotReadRelay from '../lib/slotReadRelay'
import chatReducer, { REFRESH_LIMIT_CEILING, SLOT_DETAIL_MAX_LIMIT, hydrateSlotMessages, markLoadedRowsChanged, noteRedactionHostsGen, noteSlotVariantSeqs, noteVariantSwitch, refreshSlot, slotVariantSeqSeen, renewUntilCurrent, sseChatMessage, sseChatMessagePatchByTs, switchSlot, warmSlotCache } from '../store/chatSlice'

vi.mock('../api/client')

type Row = { role: string; content: string; cls: string; ts: string; meta: Record<string, unknown> }

const BASE = Date.UTC(2026, 8, 1)
const row = (i: number): Row => ({
  role: i % 2 ? 'assistant' : 'user',
  content: `row ${i}`,
  cls: '',
  ts: new Date(BASE + i * 1000).toISOString(),
  meta: { mid: `m-${i}` },
})
const range = (from: number, to: number) =>
  Array.from({ length: to - from }, (_, k) => row(from + k))
const mids = (rows: Array<{ meta?: Record<string, unknown> }>) => rows.map(m => m.meta?.mid)

function makeStore(chat: Record<string, unknown>) {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...base, ...chat } as typeof base },
  })
}
const warmStore = (cache: Row[], extra: Record<string, unknown> = {}) =>
  makeStore({ activeSlot: 'active-slot', slotMessages: { 'bg-slot': cache }, ...extra })
const refreshStore = (view: Row[]) => makeStore({ activeSlot: 'open-slot', messages: view })

const detail = (messages: Row[], total: number) =>
  ({ messages, running: false, has_more: true, next_before: total - messages.length, total, queue: [] })

/** The server holds `rows`; a bounded read returns the newest `limit` of them. */
function serve(rows: Row[], total = rows.length) {
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(
    async (_key: string, limit?: number) => limit === undefined
      ? detail(rows, total)
      : detail(rows.slice(Math.max(0, rows.length - limit)), total),
  )
}

const calls = () => (api.chatSlotDetail as unknown as { mock: { calls: unknown[][] } }).mock.calls

/** Like `serve`, but a bounded read honours `before` as the walk sends it: the
 *  `limit` rows ending just above that index, with the cursor below them. */
function serveWindowed(rows: Row[]) {
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(
    async (_key: string, limit?: number, before?: number) => {
      if (limit === undefined) return { ...detail(rows, rows.length), has_more: false, next_before: 0 }
      const end = before === undefined ? rows.length : before
      const start = Math.max(0, end - limit)
      return { messages: rows.slice(start, end), running: false, has_more: start > 0, next_before: start, total: rows.length, queue: [] }
    },
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('warmSlotCache over the ceiling', () => {
  for (const held of [SLOT_DETAIL_MAX_LIMIT + 1, 600, 2000]) {
    it(`bounds the warm of a pane holding ${held} rows and keeps every row`, async () => {
      // The server holds the pane's rows plus two new ones from the turn that ended.
      serve(range(0, held + 2))
      const store = warmStore(range(0, held))
      await store.dispatch(warmSlotCache('bg-slot') as never)

      // One bounded read, never the whole transcript.
      expect(calls()).toEqual([['bg-slot', SLOT_DETAIL_MAX_LIMIT]])
      // The rows above the page survive as a head, and the new rows are appended.
      expect(mids(store.getState().chat.slotMessages['bg-slot'])).toEqual(mids(range(0, held + 2)))
    })
  }

  it('walks older, never reading whole, when the page moved past the whole cache', async () => {
    // More than a ceiling of new rows landed: the newest page shares nothing with the
    // pane, so the warm walks older until it anchors (`walkWindowBackTo`).
    const held = 600
    const serverRows = range(0, held + SLOT_DETAIL_MAX_LIMIT + 10)
    serveWindowed(serverRows)
    const store = warmStore(range(0, held))
    await store.dispatch(warmSlotCache('bg-slot') as never)

    expect(calls().every(c => c[1] !== undefined)).toBe(true)
    expect(mids(store.getState().chat.slotMessages['bg-slot'])).toEqual(mids(serverRows))
  })

  it('settles a hole below the anchor with bounded reads only', async () => {
    // A hole: the page anchors, but a row the pane holds after the anchor is gone
    // from it. One fresh read of the same bounded page settles it.
    const held = 600
    serve(range(0, held).filter(m => m.meta.mid !== 'm-550'))
    const store = warmStore(range(0, held))
    await store.dispatch(warmSlotCache('bg-slot') as never)

    expect(calls().length).toBeGreaterThan(1)
    expect(calls().every(c => c[1] !== undefined)).toBe(true)
  })

  it('leaves a pane at the ceiling on the count-matched read', async () => {
    const held = SLOT_DETAIL_MAX_LIMIT
    serve(range(0, held))
    const store = warmStore(range(0, held))
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(calls()).toEqual([['bg-slot', held]])
  })

  /* The first bounded warm of a pane that used to take unbounded reads meets the
   * RAW count those reads left behind. Raw counts every per-turn `done` row the
   * bounded page collapses away, so the smaller collapsed count must not read as a
   * server shrink and drop the pane's live identity-less tail. */
  const streamingTail = { role: 'streaming', content: 'half a reply', cls: '', ts: new Date(BASE + 601_000).toISOString(), meta: {} }

  it('keeps the live tail when the retained baseline came from an unbounded read', async () => {
    serve(range(0, 600))
    const store = warmStore([...range(0, 600), streamingTail], {
      slotServerTotal: { 'bg-slot': 650 },
      slotServerTotalRaw: { 'bg-slot': true },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)

    expect(calls()).toEqual([['bg-slot', SLOT_DETAIL_MAX_LIMIT]])
    const kept = store.getState().chat.slotMessages['bg-slot']
    expect(kept.map(m => m.role).slice(-1)).toEqual(['streaming'])
    // The collapsed count replaces the raw one as the baseline.
    expect(store.getState().chat.slotServerTotal['bg-slot']).toBe(600)
    expect(store.getState().chat.slotServerTotalRaw['bg-slot']).toBeUndefined()
  })

  it('still reads a remote clear as a shrink when the baseline is raw and the retry has a raw count', async () => {
    // Another client cleared the slot: the server now holds three fresh rows. The
    // bounded probe anchors nothing, so the warm retries unbounded -- and that
    // retry's own raw `total` is in the raw baseline's units, so the fall is real
    // and the cleared rows must not survive in the pane.
    serve(range(700, 703))
    const store = warmStore([...range(0, 600), streamingTail], {
      slotServerTotal: { 'bg-slot': 650 },
      slotServerTotalRaw: { 'bg-slot': true },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)

    expect(calls()).toEqual([['bg-slot', SLOT_DETAIL_MAX_LIMIT], ['bg-slot']])
    expect(mids(store.getState().chat.slotMessages['bg-slot'])).toEqual(mids(range(700, 703)))
  })

  it('records a background hydrate read as raw only when it was unbounded', () => {
    const store = warmStore([])
    store.dispatch(hydrateSlotMessages({ slot: 'bg-a', messages: range(0, 5), hasMore: false, bounded: false, total: 7, running: false }))
    store.dispatch(hydrateSlotMessages({ slot: 'bg-b', messages: range(0, 5), hasMore: true, bounded: true, total: 7, running: false }))
    expect(store.getState().chat.slotServerTotalRaw['bg-a']).toBe(true)
    expect(store.getState().chat.slotServerTotalRaw['bg-b']).toBeUndefined()
  })

  it('still reads a fall against a bounded baseline as a shrink', async () => {
    // Control: the units gate must not switch shrink detection off.
    serve(range(0, 600))
    const store = warmStore([...range(0, 600), streamingTail], {
      slotServerTotal: { 'bg-slot': 650 },
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)

    const kept = store.getState().chat.slotMessages['bg-slot']
    expect(kept.some(m => m.role === 'streaming')).toBe(false)
  })
})

describe('refreshSlot over the ceiling', () => {
  for (const held of [REFRESH_LIMIT_CEILING + 1, 600, 2000]) {
    it(`bounds the refresh of a view holding ${held} rows and keeps every row`, async () => {
      serve(range(0, held + 2))
      const store = refreshStore(range(0, held))
      await store.dispatch(refreshSlot('open-slot') as never)

      expect(calls()).toEqual([['open-slot', REFRESH_LIMIT_CEILING]])
      expect(mids(store.getState().chat.messages)).toEqual(mids(range(0, held + 2)))
    })
  }

  it('leaves a view at the ceiling on the count-matched read', async () => {
    serve(range(0, REFRESH_LIMIT_CEILING))
    const store = refreshStore(range(0, REFRESH_LIMIT_CEILING))
    await store.dispatch(refreshSlot('open-slot') as never)
    expect(calls()).toEqual([['open-slot', REFRESH_LIMIT_CEILING]])
  })

  /* A mutation-driven refresh (a redaction host allowed, a variant switched) exists
   * to re-serve a row the view ALREADY holds, and that row can sit above any bound.
   * A newest-rows page would keep the view's stale copy of it as the head. */
  it('re-reads the whole transcript of a marked view', async () => {
    const held = 600
    const changed = range(0, held).map(m => m.meta.mid === 'm-10' ? { ...m, content: 'link restored' } : m)
    serve(changed)
    const store = refreshStore(range(0, held))
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(refreshSlot('open-slot') as never)

    expect(calls()).toEqual([['open-slot']])
    const older = store.getState().chat.messages.find(m => m.meta?.mid === 'm-10')
    expect(older?.content).toBe('link restored')
  })

  it('installs a marked whole read a live chunk landed inside, carrying the streamed text over', async () => {
    // A whole read takes as long as the transcript is big; its rows predate a chunk
    // that reduced while it was in flight. Installing them as-is would erase that
    // text, and dropping them would leave a revoked link clickable until turn end.
    const held = 600
    const changed = range(0, held).map(m => m.meta.mid === 'm-10' ? { ...m, content: 'link restored' } : m)
    const store = refreshStore(range(0, held))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(
      async (_key: string, limit?: number) => {
        if (limit === undefined) {
          store.dispatch(sseChatMessage({ slot: 'open-slot', role: 'chunk', content: 'streamed-live' } as never))
        }
        return detail(limit === undefined ? changed : changed.slice(held - limit), held)
      },
    )
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(refreshSlot('open-slot') as never)

    expect(calls()).toEqual([['open-slot']])
    const after = store.getState().chat.messages
    expect(after.at(-1)?.content).toBe('streamed-live')
    expect(after.filter(m => m.content === 'streamed-live')).toHaveLength(1)
    expect(after.find(m => m.meta?.mid === 'm-10')?.content).toBe('link restored')
    expect(store.getState().chat.slotHeadUnverified['open-slot']).toBeUndefined()
  })

  it('replays a row patch that landed inside a marked whole read onto the rows it installs', async () => {
    // A sign-in banner settling patches a row the view holds without moving the
    // live-frame count; the read's copy predates it and must not put it back.
    const held = 600
    const store = refreshStore(range(0, held))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(
      async (_key: string, limit?: number) => {
        if (limit === undefined) {
          store.dispatch(sseChatMessagePatchByTs({ slot: 'open-slot', ts: '', mid: 'm-10', meta: { oauth_state: 'authenticated' }, content: 'signed in' }))
        }
        return detail(limit === undefined ? range(0, held) : range(held - limit, held), held)
      },
    )
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(refreshSlot('open-slot') as never)

    expect(calls()).toEqual([['open-slot']])
    const row = store.getState().chat.messages.find(m => m.meta?.mid === 'm-10')
    expect(row?.content).toBe('signed in')
    expect(row?.meta?.oauth_state).toBe('authenticated')
    expect(store.getState().chat.slotHeadUnverified['open-slot']).toBeUndefined()
  })

  it('fails a marked whole read that outran the patch log, raising the Retry notice', async () => {
    // Some patches can no longer be replayed onto the read: installing it would
    // undo them, and dropping it silently would leave a revoked link with no notice.
    const held = 600
    const PATCHES = 1000
    const store = refreshStore(range(0, held))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(
      async (_key: string, limit?: number) => {
        if (limit === undefined) {
          // Far more than the log keeps (its cap is pinned in chatRowPatch.test.ts).
          for (let i = 0; i < PATCHES; i++) {
            store.dispatch(sseChatMessagePatchByTs({ slot: 'open-slot', ts: '', mid: 'm-10', content: `patch ${i}` }))
          }
        }
        return detail(limit === undefined ? range(0, held) : range(held - limit, held), held)
      },
    )
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(refreshSlot('open-slot') as never)

    expect(store.getState().chat.slotHealFailed['open-slot']).toBe(true)
    expect(store.getState().chat.slotHeadUnverified['open-slot']).toBeDefined()
    // The live view keeps the newest patch, not the read's older copy.
    expect(store.getState().chat.messages.find(m => m.meta?.mid === 'm-10')?.content).toBe(`patch ${PATCHES - 1}`)
  })

  it('takes the view\'s streaming row over the older copy a raced whole read carries', async () => {
    // The read snapshot the reply mid-stream; the view streamed further while it
    // was in flight. One streaming row survives, the view's (live frames only
    // extend it), never both.
    const held = 600
    const store = refreshStore(range(0, held))
    const stale = { role: 'streaming', content: 'half', cls: '', ts: new Date(BASE + held * 1000).toISOString(), meta: {} }
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(
      async (_key: string, limit?: number) => {
        if (limit === undefined) {
          store.dispatch(sseChatMessage({ slot: 'open-slot', role: 'chunk', content: 'half and more' } as never))
        }
        return { ...detail([...range(0, held), stale] as Row[], held + 1), running: true }
      },
    )
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(refreshSlot('open-slot') as never)

    const streams = store.getState().chat.messages.filter(m => m.role === 'streaming')
    expect(streams.map(m => m.content)).toEqual(['half and more'])
  })

  it('keeps a marked small view on the count-matched read when the page spans it', async () => {
    // Under the ceiling the count-matched page reaches every row the view holds,
    // so it already re-serves the changed row; the whole transcript is not needed.
    const held = 100
    serve(range(0, 300).map(m => m.meta.mid === 'm-210' ? { ...m, content: 'link restored' } : m))
    const store = refreshStore(range(200, 300))
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(refreshSlot('open-slot') as never)

    expect(calls()).toEqual([['open-slot', held]])
    const older = store.getState().chat.messages.find(m => m.meta?.mid === 'm-210')
    expect(older?.content).toBe('link restored')
  })

  it('retries a marked view unbounded when its page only overlaps it', async () => {
    // New rows landed, so the count-matched page no longer reaches the view's
    // oldest row. The ordinary refresh would keep that row as a head; a mutation
    // refresh must not, since that head is exactly the stale copy.
    serve(range(0, 310).map(m => m.meta.mid === 'm-200' ? { ...m, content: 'link restored' } : m))
    const store = refreshStore(range(200, 300))
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(refreshSlot('open-slot') as never)

    expect(calls()).toEqual([['open-slot', 100], ['open-slot']])
    const older = store.getState().chat.messages.find(m => m.meta?.mid === 'm-200')
    expect(older?.content).toBe('link restored')
  })

  it('installs the unbounded retry of a marked view a live chunk landed inside, carrying the streamed text over', async () => {
    const serverRows = range(0, 310).map(m => m.meta.mid === 'm-200' ? { ...m, content: 'link restored' } : m)
    const store = refreshStore(range(200, 300))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(
      async (_key: string, limit?: number) => {
        if (limit === undefined) {
          store.dispatch(sseChatMessage({ slot: 'open-slot', role: 'chunk', content: 'streamed-live' } as never))
        }
        return limit === undefined ? detail(serverRows, 310) : detail(serverRows.slice(310 - limit), 310)
      },
    )
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(refreshSlot('open-slot') as never)

    expect(calls()).toEqual([['open-slot', 100], ['open-slot']])
    expect(store.getState().chat.messages.at(-1)?.content).toBe('streamed-live')
    expect(store.getState().chat.messages.find(m => m.meta?.mid === 'm-200')?.content).toBe('link restored')
    expect(store.getState().chat.slotHeadUnverified['open-slot']).toBeUndefined()
  })

  it('keeps an unmarked refresh bounded, which is why a change must mark', async () => {
    const held = 600
    serve(range(0, held).map(m => m.meta.mid === 'm-10' ? { ...m, content: 'link restored' } : m))
    const store = refreshStore(range(0, held))
    await store.dispatch(refreshSlot('open-slot') as never)

    expect(calls()).toEqual([['open-slot', REFRESH_LIMIT_CEILING]])
    const older = store.getState().chat.messages.find(m => m.meta?.mid === 'm-10')
    expect(older?.content).toBe('row 10')
  })
})

describe('switchSlot into a cache over the ceiling', () => {
  const switchStore = (cache: Row[]) =>
    makeStore({ activeSlot: 'other-slot', messages: [], slotMessages: { 'long-slot': cache } })

  for (const held of [SLOT_DETAIL_MAX_LIMIT + 1, 600, 2000]) {
    it(`switches into a cache of ${held} rows with one bounded read and keeps every row`, async () => {
      serve(range(0, held + 2))
      const store = switchStore(range(0, held))
      await store.dispatch(switchSlot('long-slot') as never)

      expect(calls()).toEqual([['long-slot', SLOT_DETAIL_MAX_LIMIT]])
      expect(mids(store.getState().chat.messages)).toEqual(mids(range(0, held + 2)))
    })
  }
})

/* A workspace-wide change (a redaction host allowed) re-serves rows every loaded
 * session already holds. The over-ceiling reads keep rows above their page as a
 * head WITHOUT comparing it to the server, so a marked cache must not keep one. */
/* A sign-in link the server would withdraw once its child exited is only
 * withdrawn as the server serves the row, so a head kept above a bounded page
 * must not hold one: every path reads such a transcript whole. */
describe('a kept head holding an open sign-in link', () => {
  const held = 600
  const withBanner = (rows: Row[], meta: Record<string, unknown>) =>
    rows.map((r, i) => i === 3 ? { ...r, role: 'mcp_oauth', meta: { ...r.meta, server_name: 's', ...meta } } : r)
  const open = { oauth_url: 'https://idp.example/authorize' }

  it('makes a warm read the whole transcript', async () => {
    serve(withBanner(range(0, held + 2), { ...open, expired: true }))
    const store = warmStore(withBanner(range(0, held), open))
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(calls()).toEqual([['bg-slot', SLOT_DETAIL_MAX_LIMIT], ['bg-slot']])
    expect(store.getState().chat.slotMessages['bg-slot'][3].meta?.expired).toBe(true)
  })

  it('makes a refresh read the whole transcript', async () => {
    serve(withBanner(range(0, held + 2), { ...open, expired: true }))
    const store = refreshStore(withBanner(range(0, held), open))
    await store.dispatch(refreshSlot('open-slot') as never)
    expect(calls()).toEqual([['open-slot', REFRESH_LIMIT_CEILING], ['open-slot']])
    expect(store.getState().chat.messages[3].meta?.expired).toBe(true)
  })

  it('makes a switch read the whole transcript', async () => {
    serve(withBanner(range(0, held + 2), { ...open, expired: true }))
    const store = makeStore({ activeSlot: 'other-slot', messages: [], slotMessages: { 'long-slot': withBanner(range(0, held), open) } })
    await store.dispatch(switchSlot('long-slot') as never)
    expect(calls()).toEqual([['long-slot', SLOT_DETAIL_MAX_LIMIT], ['long-slot']])
    expect(store.getState().chat.messages[3].meta?.expired).toBe(true)
  })

  it('keeps the bounded read when the link in the head is already settled', async () => {
    serve(withBanner(range(0, held + 2), { completed: true }))
    const store = refreshStore(withBanner(range(0, held), { completed: true }))
    await store.dispatch(refreshSlot('open-slot') as never)
    expect(calls()).toEqual([['open-slot', REFRESH_LIMIT_CEILING]])
  })

  /* More new rows than a page holds: the first page misses the view, and the walk
   * (`walkWindowBackTo`) extends it older until it anchors. The rows above that
   * anchor are kept verbatim just the same, so an open link there reads whole. */
  const gained = REFRESH_LIMIT_CEILING + 10
  it('makes a refresh whose window was walked read the whole transcript', async () => {
    serveWindowed(withBanner(range(0, held + gained), { ...open, expired: true }))
    const store = refreshStore(withBanner(range(0, held), open))
    await store.dispatch(refreshSlot('open-slot') as never)
    expect(calls().at(-1)).toEqual(['open-slot'])
    expect(store.getState().chat.messages[3].meta?.expired).toBe(true)
  })

  it('makes a switch whose window was walked read the whole transcript', async () => {
    serveWindowed(withBanner(range(0, held + gained), { ...open, expired: true }))
    const store = makeStore({ activeSlot: 'other-slot', messages: [], slotMessages: { 'long-slot': withBanner(range(0, held), open) } })
    await store.dispatch(switchSlot('long-slot') as never)
    expect(calls().at(-1)).toEqual(['long-slot'])
    expect(store.getState().chat.messages[3].meta?.expired).toBe(true)
  })

  it('walks, and keeps the bounded reads, when the walked head holds no open link', async () => {
    serveWindowed(range(0, held + gained))
    const store = refreshStore(range(0, held))
    await store.dispatch(refreshSlot('open-slot') as never)
    expect(calls().every(c => c.length > 1)).toBe(true)
    expect(store.getState().chat.messages).toHaveLength(held + gained)
  })
})

/* A marked cache keeps no head: a switch whose window misses it re-reads every row
 * rather than walking, since the walk keeps the rows above its anchor as they were. */
describe('switchSlot into a marked cache the window misses', () => {
  it('reads the whole transcript instead of walking older', async () => {
    const held = 600
    const changed = range(0, held + SLOT_DETAIL_MAX_LIMIT + 10).map(m => m.meta.mid === 'm-10' ? { ...m, content: 'link restored' } : m)
    serveWindowed(changed)
    const store = makeStore({ activeSlot: 'other-slot', messages: [], slotMessages: { 'long-slot': range(0, held) } })
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(switchSlot('long-slot') as never)
    expect(calls()).toEqual([['long-slot', SLOT_DETAIL_MAX_LIMIT], ['long-slot']])
    expect(store.getState().chat.messages.find(m => m.meta?.mid === 'm-10')?.content).toBe('link restored')
  })
})

/* A reply variant switch changes one slot's rows. A tab that saw the switch frame
 * adopts its count; one whose socket was down sees the slot list's count move on
 * reconnect, and re-serves that slot alone, never every loaded session. */
describe('a live variant switch frame', () => {
  const withVariants = (rows: Row[], mid: string) =>
    rows.map(m => m.meta.mid === mid ? { ...m, variants: [{ content: 'first' }, { content: 'second' }], variant_idx: 1 } : m)

  it('applies the frame in place to the row it rewrote, keeping the open chat\'s re-read bounded', async () => {
    const held = 600
    const view = withVariants(range(0, held), 'm-599')
    serve(view as Row[])
    const store = refreshStore(view as Row[])
    const marked = noteVariantSwitch(store.dispatch as never, store.getState, { slot: 'open-slot', index: 0, content: 'first', seq: 's-2' })

    expect(marked).toBe(false)
    expect(store.getState().chat.slotHeadUnverified['open-slot']).toBeUndefined()
    const row = store.getState().chat.messages.find(m => m.meta?.mid === 'm-599')
    expect(row?.content).toBe('first')
    expect(row?.variant_idx).toBe(0)
    await vi.waitFor(() => expect(calls().length).toBeGreaterThan(0))
    expect(calls().every(c => c[1] !== undefined)).toBe(true)
  })

  it('marks the slot when it holds no row the switch could have rewritten', () => {
    const store = refreshStore(range(0, 3))
    serve(range(0, 3))
    expect(noteVariantSwitch(store.dispatch as never, store.getState, { slot: 'open-slot', index: 0, content: 'x', seq: 's-2' })).toBe(true)
    expect(store.getState().chat.slotHeadUnverified['open-slot']).toBeDefined()
  })
})

describe('a reply variant switch this tab missed', () => {
  const rows = (seqs: Record<string, number>) => Object.entries(seqs).map(([key, variant_seq]) => ({ key, variant_seq }))

  it('takes the first count as a baseline, then marks only the slot whose count moved', () => {
    // No rows loaded yet: the first count is a baseline.
    const store = makeStore({ activeSlot: 'other-slot', slotMessages: {} })
    const note = () => noteSlotVariantSeqs(store.dispatch as never, store.getState, rows({ a: 0, b: 2 }))
    expect(noteSlotVariantSeqs(store.dispatch as never, store.getState, rows({ a: 0, b: 1 }))).toEqual([])
    expect(store.getState().chat.slotHeadUnverified).toEqual({})
    store.dispatch(hydrateSlotMessages({ slot: 'a', messages: range(0, 3), hasMore: false }))
    store.dispatch(hydrateSlotMessages({ slot: 'b', messages: range(0, 3), hasMore: false }))
    expect(note()).toEqual(['b'])
    expect(Object.keys(store.getState().chat.slotHeadUnverified)).toEqual(['b'])
    // The same count again is not a second switch.
    expect(note()).toEqual([])
  })

  it('treats a first count as a change for a slot whose rows loaded before any count', () => {
    // Nothing vouches rows loaded before the first count predate no switch.
    const store = makeStore({ activeSlot: 'other-slot', slotMessages: { a: range(0, 3) } })
    expect(noteSlotVariantSeqs(store.dispatch as never, store.getState, rows({ a: 0, b: 1 }))).toEqual(['a'])
    expect(Object.keys(store.getState().chat.slotHeadUnverified)).toEqual(['a'])
  })

  it('re-reads the open chat when its count moved, and the read clears the mark', async () => {
    serve(range(0, 3))
    const store = makeStore({ activeSlot: 'open-slot', messages: range(0, 3), slotVariantSeq: { 'open-slot': 4 } })
    noteSlotVariantSeqs(store.dispatch as never, store.getState, rows({ 'open-slot': 4 }))
    expect(calls()).toEqual([])
    noteSlotVariantSeqs(store.dispatch as never, store.getState, rows({ 'open-slot': 5 }))
    // A view this small is spanned by its page, which re-serves every row it holds.
    await vi.waitFor(() => expect(calls().map(c => c[0])).toEqual(['open-slot']))
    await vi.waitFor(() => expect(store.getState().chat.slotHeadUnverified['open-slot']).toBeUndefined())
  })

  it('reads a gateway restart as a change even when the restarted count matches the held one', () => {
    // The server prefixes the count per process: after a restart and one missed
    // switch the count is 1 again, but the revision is not the one this tab holds.
    const store = makeStore({ activeSlot: 'other-slot', slotMessages: { a: range(0, 3) } })
    noteSlotVariantSeqs(store.dispatch as never, store.getState, [{ key: 'a', variant_seq: 'boot1:1' }])
    expect(noteSlotVariantSeqs(store.dispatch as never, store.getState, [{ key: 'a', variant_seq: 'boot2:1' }])).toEqual(['a'])
  })

  it('does not re-serve a slot whose switch this tab saw live', () => {
    const store = makeStore({ activeSlot: 'other-slot', slotMessages: { a: range(0, 3) } })
    noteSlotVariantSeqs(store.dispatch as never, store.getState, rows({ a: 1 }))
    store.dispatch(slotVariantSeqSeen({ slot: 'a', seq: 2 }))
    expect(noteSlotVariantSeqs(store.dispatch as never, store.getState, rows({ a: 2 }))).toEqual([])
  })
})

describe('a change to how loaded rows are served', () => {
  const changedAt = (rows: Row[], mid: string) =>
    rows.map(m => m.meta.mid === mid ? { ...m, content: 'link restored' } : m)

  it('marks every loaded slot, the open one included, or only the named ones', () => {
    const store = makeStore({ activeSlot: 'open-slot', slotMessages: { 'bg-a': range(0, 3), 'bg-b': range(0, 3) } })
    store.dispatch(markLoadedRowsChanged())
    expect(Object.keys(store.getState().chat.slotHeadUnverified).sort()).toEqual(['bg-a', 'bg-b', 'open-slot'])
    const named = makeStore({ activeSlot: 'open-slot', slotMessages: { 'bg-a': range(0, 3), 'bg-b': range(0, 3) } })
    named.dispatch(markLoadedRowsChanged({ slot: 'bg-b' }))
    expect(Object.keys(named.getState().chat.slotHeadUnverified)).toEqual(['bg-b'])
  })

  it('clears the open view\'s mark once its refresh re-served every loaded row', async () => {
    serve(range(0, 600))
    const store = refreshStore(range(0, 600))
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(refreshSlot('open-slot') as never)
    expect(calls()).toEqual([['open-slot']])
    expect(store.getState().chat.slotHeadUnverified['open-slot']).toBeUndefined()
  })

  it('keeps the open view marked when a switch away drops its re-read, so the switch back re-serves it', async () => {
    // The re-read the change asked for is in flight when the reader switches away:
    // its answer is dropped and the view it would have replaced is cached. That
    // cache still holds the stale rows, so the mark must survive for the switch back.
    const held = 600
    let release: (() => void) | undefined
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise(res => { release = () => res(detail(range(0, held), held)) }),
    )
    const store = makeStore({ activeSlot: 'open-slot', messages: range(0, held), slotMessages: {} })
    store.dispatch(markLoadedRowsChanged())
    const reread = store.dispatch(refreshSlot('open-slot') as never)
    serve(changedAt(range(0, held), 'm-10'))
    await store.dispatch(switchSlot('other-slot') as never)
    release?.()
    await reread
    expect(store.getState().chat.slotHeadUnverified['open-slot']).toBeDefined()

    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockClear()
    await store.dispatch(switchSlot('open-slot') as never)
    expect(calls()).toEqual([['open-slot', SLOT_DETAIL_MAX_LIMIT], ['open-slot']])
    expect(store.getState().chat.messages.find(m => m.meta?.mid === 'm-10')?.content).toBe('link restored')
    expect(store.getState().chat.slotHeadUnverified['open-slot']).toBeUndefined()
  })

  it('makes the next switch into a marked long chat re-serve every loaded row', async () => {
    const held = 600
    serve(changedAt(range(0, held), 'm-10'))
    const store = makeStore({ activeSlot: 'other-slot', messages: [], slotMessages: { 'long-slot': range(0, held) } })
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(switchSlot('long-slot') as never)

    expect(calls()).toEqual([['long-slot', SLOT_DETAIL_MAX_LIMIT], ['long-slot']])
    expect(store.getState().chat.messages.find(m => m.meta?.mid === 'm-10')?.content).toBe('link restored')
    expect(store.getState().chat.slotHeadUnverified['long-slot']).toBeUndefined()
  })

  it('re-reads a switch whose slot was marked while it was in flight', async () => {
    let n = 0
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      n += 1
      if (n === 1) {
        // A variant switch lands while the switch's read is in flight.
        store.dispatch(markLoadedRowsChanged({ slot: 'long-slot' }))
        return detail(range(0, 3).map((r, i) => (i === 2 ? { ...r, content: 'old variant' } : r)), 3)
      }
      return detail(range(0, 3).map((r, i) => (i === 2 ? { ...r, content: 'new variant' } : r)), 3)
    })
    const store = makeStore({ activeSlot: 'other-slot', messages: [], slotMessages: { 'long-slot': range(0, 3) } })
    await store.dispatch(switchSlot('long-slot') as never)
    expect(calls()[1]).toEqual(['long-slot'])
    expect(store.getState().chat.messages[2].content).toBe('new variant')
    // The whole re-read verified the mark it started under.
    expect(store.getState().chat.slotHeadUnverified['long-slot']).toBeUndefined()
  })

  it('flags a failed re-read of a marked slot, so the pane can offer a Retry', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network'))
    const store = refreshStore(range(0, 3))
    store.dispatch(markLoadedRowsChanged({ slot: 'open-slot' }))
    await store.dispatch(refreshSlot('open-slot') as never)
    expect(store.getState().chat.slotHealFailed['open-slot']).toBe(true)
    // A retry starting keeps it on screen (the rows are still outdated); one that
    // lands clears it with the mark.
    serve(range(0, 3))
    const retry = store.dispatch(refreshSlot('open-slot') as never) as unknown as Promise<unknown>
    expect(store.getState().chat.slotHealFailed['open-slot']).toBe(true)
    await retry
    expect(store.getState().chat.slotHeadUnverified['open-slot']).toBeUndefined()
    expect(store.getState().chat.slotHealFailed['open-slot']).toBeUndefined()
  })

  it('keeps the open chat\'s failed-heal notice when a warm for it starts (a warm never reads the open chat)', () => {
    const store = refreshStore(range(0, 3))
    store.dispatch(markLoadedRowsChanged({ slot: 'open-slot' }))
    store.dispatch({ type: 'chat/refreshSlot/rejected', meta: { arg: 'open-slot' }, error: { name: 'Error', message: 'x' } })
    store.dispatch({ type: 'chat/warmSlotCache/pending', meta: { arg: 'open-slot' } })
    expect(store.getState().chat.slotHealFailed['open-slot']).toBe(true)
  })

  it('does not flag a failed read of an unmarked slot, nor a warm refused as outdated', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network'))
    const store = refreshStore(range(0, 3))
    await store.dispatch(refreshSlot('open-slot') as never)
    expect(store.getState().chat.slotHealFailed['open-slot']).toBeUndefined()
    const warm = warmStore(range(0, 3))
    warm.dispatch(markLoadedRowsChanged({ slot: 'bg-slot' }))
    warm.dispatch({ type: 'chat/warmSlotCache/rejected', meta: { arg: 'bg-slot' }, error: { name: 'ObsoleteReadError', message: 'x' } })
    expect(warm.getState().chat.slotHealFailed['bg-slot']).toBeUndefined()
  })

  it('drops a refresh that predates a variant switch, so the switched reply stays', async () => {
    const view = range(0, 3)
    const releases: Array<() => void> = []
    let n = 0
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(() => {
      n += 1
      const content = n === 1 ? 'old variant' : 'new variant'
      const rows = range(0, 3).map((r, i) => (i === 2 ? { ...r, content } : r))
      return new Promise(res => { releases.push(() => res(detail(rows, 3))) })
    })
    const store = refreshStore(view)
    const first = store.dispatch(refreshSlot('open-slot') as never) as unknown as Promise<unknown>
    await new Promise(r => setTimeout(r, 0))
    // The variant switch: mark the slot, then re-read it.
    store.dispatch(markLoadedRowsChanged({ slot: 'open-slot' }))
    const second = store.dispatch(refreshSlot('open-slot') as never) as unknown as Promise<unknown>
    await new Promise(r => setTimeout(r, 0))
    releases[1]()
    await second
    expect(store.getState().chat.messages[2].content).toBe('new variant')
    // The earlier read lands last, with the reply as it was before the switch.
    releases[0]()
    await first
    expect(store.getState().chat.messages[2].content).toBe('new variant')
  })

  it('does not re-read a warm the change it predates has marked: the reducer drops it anyway', async () => {
    let n = 0
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async (key: string) => {
      const call = ++n
      if (call === 1) noteRedactionHostsGen(store.dispatch as (a: unknown) => unknown, store.getState, 'g-new', 'status')
      return { ...detail(range(0, 3), 3), has_more: false, redaction_gen: key === 'bg-slot' && call === 1 ? 'g-old' : 'g-new' }
    })
    const store = warmStore(range(0, 3), { redactionHostsGen: 'g-old' })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    await new Promise(r => setTimeout(r, 0))
    expect(calls().filter(c => c[0] === 'bg-slot').length).toBe(1)
    expect(store.getState().chat.slotHeadUnverified['bg-slot']).toBeDefined()
  })

  it('drops a warm that predates a change marking its pane', async () => {
    const releases: Array<() => void> = []
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(() => {
      const rows = range(0, 3).map((r, i) => (i === 2 ? { ...r, content: 'old variant' } : r))
      return new Promise(res => { releases.push(() => res(detail(rows, 3))) })
    })
    const fresh = range(0, 3).map((r, i) => (i === 2 ? { ...r, content: 'new variant' } : r))
    const store = warmStore(fresh)
    const warm = store.dispatch(warmSlotCache('bg-slot') as never) as unknown as Promise<unknown>
    await new Promise(r => setTimeout(r, 0))
    store.dispatch(markLoadedRowsChanged({ slot: 'bg-slot' }))
    releases[0]()
    await warm
    expect(store.getState().chat.slotMessages['bg-slot'][2].content).toBe('new variant')
    // Still marked: the next read re-serves every row.
    expect(store.getState().chat.slotHeadUnverified['bg-slot']).toBeDefined()
  })

  it('takes the whole read for a marked cache holding a row it cannot place', async () => {
    // 501 rows: the window (newest 500) covers every row but the oldest, which has
    // no readable ts -- a row streamed this session -- so coverage skips it and
    // would keep it as a head served under the old list once the mark is cleared.
    const held = SLOT_DETAIL_MAX_LIMIT + 1
    const cache = range(0, held).map((r, i) => (i === 0 ? { ...r, ts: '', content: 'revoked link' } : r))
    const server = range(0, held).map((r, i) => (i === 0 ? { ...r, content: 'chip' } : r))
    serve(server)
    const store = makeStore({ activeSlot: 'other-slot', messages: [], slotMessages: { 'long-slot': cache } })
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(switchSlot('long-slot') as never)

    expect(calls()).toEqual([['long-slot', SLOT_DETAIL_MAX_LIMIT], ['long-slot']])
    expect(store.getState().chat.messages.find(m => m.meta?.mid === 'm-0')?.content).toBe('chip')
  })

  it('makes the next warm of a marked long pane re-serve every loaded row', async () => {
    const held = 600
    serve(changedAt(range(0, held), 'm-10'))
    const store = warmStore(range(0, held))
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(warmSlotCache('bg-slot') as never)

    // The bounded page misses the head it would keep as-is, so the warm reads whole.
    expect(calls()).toEqual([['bg-slot', SLOT_DETAIL_MAX_LIMIT], ['bg-slot']])
    expect(store.getState().chat.slotMessages['bg-slot'].find(m => m.meta?.mid === 'm-10')?.content).toBe('link restored')
    expect(store.getState().chat.slotHeadUnverified['bg-slot']).toBeUndefined()
  })

  it('keeps a mark set while an older read was in flight', async () => {
    const held = 600
    let released = false
    const pending: Array<() => void> = []
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(
      (_key: string, limit?: number) => {
        const answer = () => limit === undefined ? detail(range(0, held), held) : detail(range(held - limit, held), held)
        return released ? Promise.resolve(answer()) : new Promise(res => pending.push(() => res(answer())))
      },
    )
    const release = () => { released = true; for (const go of pending.splice(0)) go() }
    const store = warmStore(range(0, held))
    store.dispatch(markLoadedRowsChanged())
    const warm = store.dispatch(warmSlotCache('bg-slot') as never)
    // A second change lands while the read the first one asked for is in flight.
    store.dispatch(markLoadedRowsChanged())
    release()
    await warm

    // The read predates the second change, so it cannot vouch for those rows.
    expect(store.getState().chat.loadedRowsEpoch).toBe(2)
    expect(store.getState().chat.slotHeadUnverified['bg-slot']).toBe(2)
  })
})

/* An allow-list change reaches a document two ways -- the status frame every tab
 * gets, and the answer to this tab's own write -- and must be handled once. */
describe('noteRedactionHostsGen', () => {
  const note = (store: ReturnType<typeof makeStore>, gen: unknown, source: 'status' | 'write') =>
    noteRedactionHostsGen(store.dispatch as (a: unknown) => unknown, store.getState, gen, source)
  const bgStore = () => makeStore({ activeSlot: 'open-slot', messages: [], slotMessages: { 'bg-slot': range(0, 3) } })

  it('takes a status frame\'s first generation as a baseline, and handles it moving once', () => {
    serve(range(0, 3))
    // A document holding no rows yet: the first value is a baseline.
    const store = makeStore({ activeSlot: 'open-slot', messages: [], slotMessages: {} })
    expect(note(store, 'g-1', 'status')).toBe(false)
    expect(store.getState().chat.slotHeadUnverified).toEqual({})
    store.dispatch(hydrateSlotMessages({ slot: 'bg-slot', messages: range(0, 3), hasMore: false }))
    expect(note(store, 'g-2', 'status')).toBe(true)
    expect(Object.keys(store.getState().chat.slotHeadUnverified).sort()).toEqual(['bg-slot', 'open-slot'])
    expect(calls().map(c => c[0])).toEqual(['open-slot'])
    expect(note(store, 'g-2', 'status')).toBe(false)
  })

  it('handles a first generation as a change when rows were loaded before any value', () => {
    // Rows a history resume installed carry no value, so nothing vouches they were
    // served under the first one this document hears: a revoke landing between the
    // two would otherwise become the baseline and leave its link clickable.
    serve(range(0, 3))
    const store = bgStore()
    expect(note(store, 'g-1', 'status')).toBe(true)
    expect(Object.keys(store.getState().chat.slotHeadUnverified).sort()).toEqual(['bg-slot', 'open-slot'])
    expect(note(store, 'g-1', 'status')).toBe(false)
  })

  it('handles this tab\'s own write once, so the status frame carrying it is not a second change', () => {
    serve(range(0, 3))
    const store = bgStore()
    note(store, 'g-1', 'status')
    expect(note(store, 'g-2', 'write')).toBe(true)
    const epoch = store.getState().chat.loadedRowsEpoch
    expect(note(store, 'g-2', 'status')).toBe(false)
    expect(store.getState().chat.loadedRowsEpoch).toBe(epoch)
  })

  it('handles a write the status frame already carried as nothing new', () => {
    serve(range(0, 3))
    const store = bgStore()
    note(store, 'g-1', 'status')
    expect(note(store, 'g-2', 'status')).toBe(true)
    expect(note(store, 'g-2', 'write')).toBe(false)
  })

  it('treats an empty generation as unknown, but still handles a write that carries none', () => {
    serve(range(0, 3))
    const store = bgStore()
    note(store, 'g-1', 'status')
    expect(note(store, '', 'status')).toBe(false)
    expect(note(store, undefined, 'status')).toBe(false)
    expect(store.getState().chat.redactionHostsGen).toBe('g-1')
    expect(note(store, undefined, 'write')).toBe(true)
  })
})

/* A tab's first transcript read can land before its first status frame. The rows
 * it holds were served under the list the READ saw, so that value -- not the
 * first status frame's -- is the baseline: a revoke between the two must count. */
describe('seeding the allow-list baseline from a read', () => {
  const note = (store: ReturnType<typeof makeStore>, gen: unknown) =>
    noteRedactionHostsGen(store.dispatch as (a: unknown) => unknown, store.getState, gen, 'status')

  it('takes the first read\'s value, so a first status frame that differs is a change', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail(range(0, 3), 3), has_more: false, redaction_gen: 'g-A' })
    const store = makeStore({ activeSlot: 'other-slot', messages: [], slotMessages: {} })
    await store.dispatch(switchSlot('open-slot') as never)
    expect(store.getState().chat.redactionHostsGen).toBe('g-A')
    expect(note(store, 'g-B')).toBe(true)
    expect(store.getState().chat.slotHeadUnverified['open-slot']).toBeDefined()
  })

  it('never moves a baseline a read did not set first', async () => {
    const store = refreshStore(range(0, 3))
    note(store, 'g-A')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail(range(0, 3), 3), has_more: false, redaction_gen: 'g-C' })
    await store.dispatch(refreshSlot('open-slot') as never)
    expect(store.getState().chat.redactionHostsGen).toBe('g-A')
  })

  it('seeds from a background pane\'s first hydrate and from a warm', async () => {
    const store = warmStore([])
    store.dispatch(hydrateSlotMessages({ slot: 'bg-a', messages: range(0, 3), hasMore: false, redactionGen: 'g-H' }))
    expect(store.getState().chat.redactionHostsGen).toBe('g-H')
    const warm = warmStore(range(0, 3))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ ...detail(range(0, 3), 3), has_more: false, redaction_gen: 'g-W' })
    await warm.dispatch(warmSlotCache('bg-slot') as never)
    expect(warm.getState().chat.redactionHostsGen).toBe('g-W')
  })
})

/* An allow-list change must reach every pane on screen, and a read that predates
 * it must not write its outdated rows over the fresh read the change asked for. */
describe('after an allow-list change', () => {
  const note = (store: ReturnType<typeof makeStore>, gen: unknown) =>
    noteRedactionHostsGen(store.dispatch as (a: unknown) => unknown, store.getState, gen, 'status')

  it('marks a BACKGROUND card pane on this tab\'s own write and refreshes the open chat', async () => {
    serve(range(0, 3))
    const store = makeStore({ activeSlot: 'open-slot', messages: range(0, 3), slotMessages: { 'card-pane': range(0, 3) } })
    noteRedactionHostsGen(store.dispatch as (a: unknown) => unknown, store.getState, 'g-2', 'write')
    await new Promise(r => setTimeout(r, 0))
    // The card's pane re-reads itself once marked (ChatPane); the store only marks it.
    expect(store.getState().chat.slotHeadUnverified['card-pane']).toBeDefined()
    // The open chat's rows are outdated by the same change, and the status frame
    // will not report it: this write already adopted the value.
    expect(calls().map(c => c[0])).toEqual(['open-slot'])
  })

  it('re-reads again when the allow-list value moves while a re-read is in flight', async () => {
    let gen = 'g-1'
    let n = 0
    const reads: string[] = []
    const reRead = async () => { n += 1; const served = gen; if (n === 1) gen = 'g-3'; reads.push(served); return { redactionGen: served, n } }
    gen = 'g-2'
    const out = await renewUntilCurrent({ redactionGen: 'g-1', n: 0 }, reRead, () => gen)
    // The first re-read was served under g-2 and a revoke moved the value to g-3
    // mid-flight, so it re-read once more and kept the g-3 answer.
    expect(reads).toEqual(['g-2', 'g-3'])
    expect(out).toEqual({ redactionGen: 'g-3', n: 2 })
  })

  it('keeps a re-read that disagrees with an unchanged value instead of looping', async () => {
    const reRead = vi.fn(async () => ({ redactionGen: 'g-server' }))
    const out = await renewUntilCurrent({ redactionGen: 'g-server' }, reRead, () => 'g-before-restart')
    expect(reRead).toHaveBeenCalledTimes(1)
    expect(out).toEqual({ redactionGen: 'g-server' })
  })

  it('refuses a page still outdated once the cap is spent, rather than install it', async () => {
    let gen = 0
    const reRead = vi.fn(async () => { const served = `g-${gen}`; gen += 1; return { redactionGen: served } })
    // Bounded (three re-reads): a value that never settles cannot spin forever,
    // and the last page -- still outdated -- is refused, never handed back.
    await expect(renewUntilCurrent({ redactionGen: 'g-old' }, reRead, () => `g-${gen}`)).rejects.toMatchObject({ name: 'ObsoleteReadError' })
    expect(reRead).toHaveBeenCalledTimes(3)
  })

  it('does not wipe a switched-to chat when its read is refused as outdated', async () => {
    // Every read of long-slot is answered under a value that has moved on by the
    // time it lands (four changes inside the switch), so the switch is refused.
    let gen = 0
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      const served = `g-${gen}`
      gen += 1
      store.dispatch({ type: 'chat/redactionHostsGenAdopted', payload: `g-${gen}` })
      return { ...detail(range(0, 3), 3), redaction_gen: served }
    })
    const store = makeStore({ activeSlot: 'other-slot', messages: [], slotMessages: { 'long-slot': range(0, 3) }, redactionHostsGen: 'g-0' })
    const relayed = vi.spyOn(slotReadRelay, 'emitSlotRead')
    const res = await store.dispatch(switchSlot('long-slot') as never) as { type: string }
    expect(res.type).toBe('chat/switchSlot/rejected')
    expect(mids(store.getState().chat.messages)).toEqual(['m-0', 'm-1', 'm-2'])
    expect(store.getState().chat.slotLoading).toBe(false)
    // Nothing was installed, so other windows keep their unread badge for it.
    expect(relayed.mock.calls.filter(c => c[0] === 'long-slot')).toEqual([])
    relayed.mockRestore()
  })

  it('re-reads only the open chat, leaving hydrated background caches marked', async () => {
    serve(range(0, 3))
    const store = makeStore({ activeSlot: 'open-slot', messages: range(0, 3), slotMessages: {}, redactionHostsGen: 'g-1' })
    store.dispatch(hydrateSlotMessages({ slot: 'pane-a', messages: range(0, 3), hasMore: false }))
    note(store, 'g-2')
    await new Promise(r => setTimeout(r, 0))
    // A hydrated cache may no longer be on screen: re-reading every one would fan
    // one click out to whole-transcript reads of each. Its mark heals it lazily,
    // and a pane that IS on screen re-reads itself (ChatPane).
    expect(calls().map(c => c[0])).toEqual(['open-slot'])
    expect(store.getState().chat.slotHeadUnverified['pane-a']).toBeDefined()
  })

  const obsoleteThenFresh = (rows: Row[]) => {
    let n = 0
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      n += 1
      const gen = n === 1 ? 'g-old' : 'g-new'
      const served = n === 1 ? rows : rows.map(m => m.meta.mid === 'm-1' ? { ...m, content: 'fresh' } : m)
      return { ...detail(served, served.length), has_more: false, redaction_gen: gen }
    })
  }

  it('drops a refresh an allow-list change outdated mid-flight, and the change\'s own refresh heals', async () => {
    let n = 0
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      n += 1
      if (n === 1) {
        // The change lands while this refresh is in flight: noteRedactionHostsGen
        // marks every loaded slot and dispatches the open chat's own refresh.
        noteRedactionHostsGen(store.dispatch as (a: unknown) => unknown, store.getState, 'g-new', 'status')
        return { ...detail(range(0, 3), 3), has_more: false, redaction_gen: 'g-old' }
      }
      return { ...detail(range(0, 3).map(m => m.meta.mid === 'm-1' ? { ...m, content: 'fresh' } : m), 3), has_more: false, redaction_gen: 'g-new' }
    })
    const store = makeStore({ activeSlot: 'open-slot', messages: range(0, 3), redactionHostsGen: 'g-old' })
    await store.dispatch(refreshSlot('open-slot') as never)
    await new Promise(r => setTimeout(r, 0))
    // Two reads, not three: the outdated one is dropped rather than re-read.
    expect(calls().length).toBe(2)
    expect(store.getState().chat.messages.find(m => m.meta?.mid === 'm-1')?.content).toBe('fresh')
  })

  it('re-reads a warm whose rows were served under an outdated list', async () => {
    obsoleteThenFresh(range(0, 3))
    const store = warmStore(range(0, 3), { redactionHostsGen: 'g-new' })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(calls().length).toBe(2)
    expect(store.getState().chat.slotMessages['bg-slot'].find(m => m.meta?.mid === 'm-1')?.content).toBe('fresh')
  })

  it('replays a row patch that landed inside a bounded refresh onto the page it installs', async () => {
    // A small view's count-matched page spans it and is installed directly; its
    // copy of a row patched mid-flight predates the patch.
    const store = refreshStore(range(0, 3))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      store.dispatch(sseChatMessagePatchByTs({ slot: 'open-slot', ts: '', mid: 'm-1', content: 'retired' }))
      return { ...detail(range(0, 3), 3), has_more: false }
    })
    await store.dispatch(refreshSlot('open-slot') as never)
    expect(store.getState().chat.messages.find(m => m.meta?.mid === 'm-1')?.content).toBe('retired')
  })

  it('flags a marked background warm whose patches outran the log, so the pane offers Retry', async () => {
    const store = warmStore(range(0, 3))
    store.dispatch(markLoadedRowsChanged({ slot: 'bg-slot' }))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      for (let i = 0; i < 1000; i++) store.dispatch(sseChatMessagePatchByTs({ slot: 'bg-slot', ts: '', mid: 'm-1', content: `p${i}` }))
      return { ...detail(range(0, 3), 3), has_more: false }
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(store.getState().chat.slotHealFailed['bg-slot']).toBe(true)
  })

  it('replays a row patch that landed inside a background warm onto the cache it writes', async () => {
    const store = warmStore(range(0, 3))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      store.dispatch(sseChatMessagePatchByTs({ slot: 'bg-slot', ts: '', mid: 'm-1', content: 'retired' }))
      return { ...detail(range(0, 3), 3), has_more: false }
    })
    await store.dispatch(warmSlotCache('bg-slot') as never)
    expect(store.getState().chat.slotMessages['bg-slot'].find(m => m.meta?.mid === 'm-1')?.content).toBe('retired')
  })

  it('replays a row patch that landed inside a switch\'s read onto the rows it installs', async () => {
    // A sign-in banner retired while the switch's read was in flight: the read's
    // copy predates the retirement and must not put the live Authorize link back.
    const store = makeStore({ activeSlot: 'other-slot', messages: [], slotMessages: { 'open-slot': range(0, 3) } })
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      store.dispatch(sseChatMessagePatchByTs({ slot: 'open-slot', ts: '', mid: 'm-1', meta: { oauth_state: 'retired' }, content: 'retired' }))
      return { ...detail(range(0, 3), 3), has_more: false }
    })
    await store.dispatch(switchSlot('open-slot') as never)
    const row = store.getState().chat.messages.find(m => m.meta?.mid === 'm-1')
    expect(row?.content).toBe('retired')
    expect(row?.meta?.oauth_state).toBe('retired')
  })

  it('re-reads a switch whose rows were served under an outdated list', async () => {
    obsoleteThenFresh(range(0, 3))
    const store = makeStore({ activeSlot: 'other-slot', messages: [], slotMessages: {}, redactionHostsGen: 'g-new' })
    await store.dispatch(switchSlot('open-slot') as never)
    expect(calls().length).toBe(2)
    expect(store.getState().chat.messages.find(m => m.meta?.mid === 'm-1')?.content).toBe('fresh')
  })

  it('re-reads a marked view whole when id-less rows sit above its oldest identified row', async () => {
    // History read off disk without a `mid` sits above the page's anchor, and a
    // page "spanning" the view from its oldest identified row keeps it verbatim.
    const legacy = { role: 'assistant', content: 'old link', cls: '', ts: new Date(BASE - 5000).toISOString(), meta: {} }
    serve(range(0, 100))
    const store = refreshStore([legacy as Row, ...range(0, 100)])
    store.dispatch(markLoadedRowsChanged())
    await store.dispatch(refreshSlot('open-slot') as never)
    expect(calls()).toEqual([['open-slot', 100], ['open-slot']])
  })
})
