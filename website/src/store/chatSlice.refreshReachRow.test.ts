/** `refreshSlot({ key, reachTs })`: a refresh asked to re-serve ONE held row --
 *  a reply whose link card just allowed or revoked a host -- must not keep that
 *  row verbatim in the head above the newest page (#17023). The plain refresh
 *  does keep it: the newest page overlaps the view, so the reducer keeps every
 *  row above the page's first row as the tab already had it. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

const SERVER_CLAMP = 500

type Row = { role: string; content: string; cls: string; ts: string; meta?: { mid: string } }

/** Row content carries a serving generation, so a re-served row is told apart
 *  from the copy the tab already held. */
let GEN = 'old'
const rowsAt = (n: number): Row[] =>
  Array.from({ length: n }, (_, i) => ({
    role: i % 2 === 0 ? 'user' : 'assistant',
    content: `m${i}-${GEN}`,
    cls: 'msg',
    ts: new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString(),
    meta: { mid: `mid-${i}` },
  }))

let TOTAL = 0

vi.mock('../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn((_slot: string, limit?: number, before?: number) => {
      const corpus = rowsAt(TOTAL)
      const end = before !== undefined ? Math.max(0, Math.min(before, TOTAL)) : TOTAL
      const eff = limit === undefined ? undefined : Math.min(limit, SERVER_CLAMP)
      const start = eff === undefined ? 0 : Math.max(0, end - eff)
      return Promise.resolve({
        messages: corpus.slice(start, end),
        has_more: start > 0,
        total: TOTAL,
        next_before: start,
        running: false,
      })
    }),
    resumeChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  },
}))

import chatReducer, { refreshSlot } from './chatSlice'
import { api } from '../api/client'

const SLOT = 'slot-1'

function makeStore(extra: Record<string, unknown> = {}) {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...base, activeSlot: SLOT, ...extra } },
  })
}

const befores = () =>
  (api.chatSlotDetail as unknown as { mock: { calls: unknown[][] } }).mock.calls.map(c => c[2])

/** A tab that paged back to row 1200 of a 2000-row chat: 800 rows held, so the
 *  refresh asks for one clamp-sized page (rows 1500..1999) and keeps 1200..1499
 *  as a verbatim head. */
function heldStore() {
  TOTAL = 2000
  GEN = 'old'
  const held = rowsAt(TOTAL).slice(1200)
  GEN = 'new'
  return makeStore({ messages: held, slotHasMore: true, slotOldestIndex: 1200, slotCursorKey: SLOT })
}

const contentOf = (store: ReturnType<typeof makeStore>, mid: string) =>
  store.getState().chat.messages.find(m => m.meta?.mid === mid)?.content

describe('refreshSlot reachTs (#17023)', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('plain refresh keeps a row above the newest page as the tab had it', async () => {
    const store = heldStore()
    await store.dispatch(refreshSlot(SLOT))
    expect(befores()).toEqual([undefined])
    expect(contentOf(store, 'mid-1300')).toBe('m1300-old')
    expect(contentOf(store, 'mid-1999')).toBe('m1999-new')
  })

  it('re-serves the named row by walking one page older, and keeps the rows above it', async () => {
    const store = heldStore()
    const ts = rowsAt(TOTAL)[1300].ts
    await store.dispatch(refreshSlot({ key: SLOT, reachTs: ts }))
    // One older page (1000..1499) reaches row 1300, then the newest re-read.
    expect(befores()).toEqual([undefined, 1500, undefined])
    expect(contentOf(store, 'mid-1300')).toBe('m1300-new')
    // No scrollback lost: the view still starts at the row the tab paged back to
    // or earlier, and holds every row once.
    const mids = store.getState().chat.messages.map(m => m.meta?.mid)
    expect(new Set(mids).size).toBe(mids.length)
    expect(mids).toContain('mid-1200')
    expect(mids[mids.length - 1]).toBe('mid-1999')
  })

  it('a row already inside the newest page needs no walk', async () => {
    const store = heldStore()
    await store.dispatch(refreshSlot({ key: SLOT, reachTs: rowsAt(TOTAL)[1800].ts }))
    expect(befores()).toEqual([undefined])
    expect(contentOf(store, 'mid-1800')).toBe('m1800-new')
  })

  it('an ambiguous ts falls back to the plain refresh', async () => {
    const store = heldStore()
    const ts = rowsAt(TOTAL)[1300].ts
    const messages = store.getState().chat.messages.map(m => m.meta?.mid === 'mid-1301' ? { ...m, ts } : m)
    const dup = makeStore({ messages, slotHasMore: true, slotOldestIndex: 1200, slotCursorKey: SLOT })
    await dup.dispatch(refreshSlot({ key: SLOT, reachTs: ts }))
    expect(befores()).toEqual([undefined])
    expect(contentOf(dup, 'mid-1300')).toBe('m1300-old')
  })
})
