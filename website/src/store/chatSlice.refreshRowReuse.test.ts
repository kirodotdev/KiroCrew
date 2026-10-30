/** A slot-detail refetch that repeats rows the store already holds must hand
 *  back the stored row objects, not the fetched copies.
 *
 *  `refreshSlot` runs on every turn end, reconnect and row-delivery watchdog
 *  pass, and `warmSlotCache` on every background or pane turn end. Both rebuild
 *  the transcript from the fetched page. The turn grouper and the memoised row
 *  components compare rows by reference, so a fresh object for an unchanged row
 *  re-renders its whole turn. These tests count the turns the grouper re-mints,
 *  which is the number of TurnBlock bodies that re-run.
 *
 *  The fake server returns a deep copy on every read, as a JSON response does:
 *  a mock that hands back the same row objects would keep identity on its own
 *  and prove nothing. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import type { ChatMessage } from '../types'

type Row = { role: string; content: string; cls: string; ts: string; seq?: number; meta?: Record<string, unknown> }

/** 20 user/assistant turns, each row carrying a server mid. */
const makeHistory = (turns: number): Row[] =>
  Array.from({ length: turns * 2 }, (_, i) => ({
    role: i % 2 === 0 ? 'user' : 'assistant',
    content: i % 2 === 0 ? `question ${i}` : `answer ${i}`,
    cls: 'msg',
    ts: new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString(),
    meta: { mid: `mid-${i}` },
  }))

let HISTORY: Row[] = []

vi.mock('../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn((slot: string, limit?: number, before?: number) => {
      const total = HISTORY.length
      const end = before !== undefined ? Math.max(0, Math.min(before, total)) : total
      const start = limit === undefined ? 0 : Math.max(0, end - limit)
      return Promise.resolve({
        key: slot,
        messages: structuredClone(HISTORY.slice(start, end)),
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
import { createTurnGrouper } from '../pages/chat/groupDisplayItems'

const SLOT = 'slot-1'
const TURNS = 20

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}
type Store = ReturnType<typeof makeStore>

/** Display items of `before` that one mounted grouper re-mints for `after`.
 *  Items `after` appends are not counted: they mount, whatever the reducer does. */
function remintedTurns(before: ChatMessage[], after: ChatMessage[]): number {
  const group = createTurnGrouper()
  const prev = group(before).turns
  const next = group(after).turns
  return prev.filter((t, i) => next[i] !== t).length
}

async function openWithRefresh(store: Store): Promise<ChatMessage[]> {
  store.dispatch(setActiveSlot(SLOT))
  await store.dispatch(refreshSlot(SLOT))
  const rows = store.getState().chat.messages
  expect(rows.length).toBe(TURNS * 2)
  return rows
}

beforeEach(() => {
  HISTORY = makeHistory(TURNS)
})

describe('refreshSlot keeps unchanged rows', () => {
  it('keeps the transcript array when the page repeats it', async () => {
    const store = makeStore()
    const before = await openWithRefresh(store)
    await store.dispatch(refreshSlot(SLOT))
    expect(store.getState().chat.messages).toBe(before)
    expect(remintedTurns(before, store.getState().chat.messages)).toBe(0)
  })

  it('re-mints no prior turn when a turn end appends one', async () => {
    const store = makeStore()
    const before = await openWithRefresh(store)
    HISTORY.push(
      { role: 'user', content: 'new question', cls: 'msg', ts: '2026-01-01T00:01:00.000Z', meta: { mid: 'mid-new-u' } },
      { role: 'assistant', content: 'new answer', cls: 'msg', ts: '2026-01-01T00:01:01.000Z', meta: { mid: 'mid-new-a' } },
    )
    await store.dispatch(refreshSlot(SLOT))
    const after = store.getState().chat.messages
    expect(after.length).toBe(before.length + 2)
    before.forEach((row, i) => expect(after[i]).toBe(row))
    expect(after.at(-1)?.content).toBe('new answer')
    expect(remintedTurns(before, after)).toBe(0)
  })

  it('takes the fetched row where any field differs, including ones sameMessage does not list', async () => {
    const store = makeStore()
    const before = await openWithRefresh(store)
    HISTORY[HISTORY.length - 1] = { ...HISTORY[HISTORY.length - 1], content: 'edited answer' }
    HISTORY[1] = { ...HISTORY[1], seq: 7 }
    await store.dispatch(refreshSlot(SLOT))
    const after = store.getState().chat.messages
    expect(after.at(-1)).not.toBe(before.at(-1))
    expect(after.at(-1)?.content).toBe('edited answer')
    expect(after[1]).not.toBe(before[1])
    expect(after[1].seq).toBe(7)
    expect(after[2]).toBe(before[2])
    expect(remintedTurns(before, after)).toBe(2)
  })
})

describe('warmSlotCache keeps unchanged cached rows', () => {
  async function warmedCache(store: Store): Promise<ChatMessage[]> {
    store.dispatch(setActiveSlot('other'))
    store.dispatch(hydrateSlotMessages({
      slot: 'bg', messages: structuredClone(HISTORY), hasMore: false,
      bounded: false, total: HISTORY.length, running: false,
    }))
    return store.getState().chat.slotMessages.bg
  }

  it('keeps the cache array when the warm repeats it', async () => {
    const store = makeStore()
    const before = await warmedCache(store)
    await store.dispatch(warmSlotCache('bg'))
    expect(store.getState().chat.slotMessages.bg).toBe(before)
  })

  it('re-mints no prior turn when a background turn end appends one', async () => {
    const store = makeStore()
    const before = await warmedCache(store)
    HISTORY.push(
      { role: 'user', content: 'bg question', cls: 'msg', ts: '2026-01-01T00:01:00.000Z', meta: { mid: 'mid-bg-u' } },
      { role: 'assistant', content: 'bg answer', cls: 'msg', ts: '2026-01-01T00:01:01.000Z', meta: { mid: 'mid-bg-a' } },
    )
    await store.dispatch(warmSlotCache('bg'))
    const after = store.getState().chat.slotMessages.bg
    expect(after.at(-1)?.content).toBe('bg answer')
    const kept = before.filter(row => after.includes(row)).length
    expect(kept).toBe(before.length)
    expect(remintedTurns(before, after)).toBe(0)
  })
})
