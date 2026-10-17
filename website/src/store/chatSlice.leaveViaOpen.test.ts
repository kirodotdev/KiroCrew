import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

/* Leaving a chat by OPENING another one (New Chat, a history resume) must cache
 * the transcript the reader had, the way a sidebar switch does. Those two exits
 * used to skip the cache write, so closing the new chat (which lands back on the
 * old one through `switchSlot`) painted whatever `slotMessages` still held from
 * the LAST switch -- the first page of an earlier visit -- and everything paged
 * in or streamed since was gone from the view. */

type Row = { role: string; content: string; ts: string; meta: { mid: string } }
const HIST: Record<string, Row[]> = {}
let tick = 0
function grow(slot: string, n: number) {
  const h = (HIST[slot] ??= [])
  for (let i = 0; i < n; i++) {
    tick++
    h.push({ role: h.length % 2 ? 'assistant' : 'user', content: `${slot}-${h.length}`, ts: new Date(Date.UTC(2026, 0, 1) + tick * 1000).toISOString(), meta: { mid: `${slot}-${h.length}` } })
  }
}

vi.mock('../lib/scrollQuiet', () => ({ whenScrollQuiet: () => Promise.resolve() }))
vi.mock('../api/client', () => ({
  api: {
    // The handler's most-recent-N slice, clamped at its 500-row ceiling.
    chatSlotDetail: vi.fn((slot: string, limit?: number, before?: number) => {
      const h = HIST[slot] ?? []
      const total = h.length
      const end = before !== undefined ? Math.max(0, Math.min(before, total)) : total
      const lim = limit === undefined ? undefined : Math.min(limit, 500)
      const start = lim === undefined ? 0 : Math.max(0, end - lim)
      return Promise.resolve({ messages: h.slice(start, end).map(r => ({ ...r, meta: { ...r.meta } })), has_more: start > 0, total, next_before: start, running: false })
    }),
    createChatSlot: vi.fn(() => Promise.resolve({ key: 'NEW', title: 'New' })),
    deleteChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
    resumeChatSlot: vi.fn((key: string) => {
      const h = HIST[key] ?? []
      return Promise.resolve({ ok: true, key, messages: h.slice(-200), total: h.length, has_more: h.length > 200, next_before: Math.max(0, h.length - 200), mode: '', surface: '' })
    }),
  },
}))

import dashboardReducer, { addSlotOptimistic } from './dashboardSlice'
import chatReducer, { setActiveSlot, switchSlot, refreshSlot, loadOlderMessages, createSlot, deleteSlot, resumeFromHistory } from './chatSlice'

const makeStore = () => {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer },
    middleware: g => g({ serializableCheck: false, immutableCheck: false }),
  })
  for (const key of ['X', 'Y']) store.dispatch(addSlotOptimistic({ key, title: key, messages: 0, running: false }))
  return store
}
type Store = ReturnType<typeof makeStore>

const shown = (s: Store) => s.getState().chat.messages.map(m => m.meta?.mid)
const tailOf = (slot: string, n: number) => HIST[slot].slice(-n).map(r => r.meta.mid)

/** Open X and page all of its history in, as a reader scrolling up does. */
async function openAndPageAll(store: Store) {
  store.dispatch(setActiveSlot(null))
  await store.dispatch(switchSlot('X'))
  while (store.getState().chat.slotHasMore) await store.dispatch(loadOlderMessages())
  expect(shown(store)).toEqual(tailOf('X', HIST.X.length))
}

/** New Chat as ChatPage does it: clear the selection, then create. */
async function newChat(store: Store) {
  store.dispatch(setActiveSlot(null))
  await store.dispatch(createSlot())
  expect(store.getState().chat.activeSlot).toBe('NEW')
}

describe('leaving a chat by opening another one keeps its loaded transcript', () => {
  beforeEach(() => { for (const k of Object.keys(HIST)) delete HIST[k]; grow('X', 600); grow('Y', 30) })

  it('closing a New Chat lands back on the full transcript, not the first page', async () => {
    const store = makeStore()
    await openAndPageAll(store)
    await newChat(store)
    // deleteSlot moves the selection synchronously, then fetches in the background.
    const close = store.dispatch(deleteSlot('NEW'))
    expect(store.getState().chat.activeSlot).toBe('X')
    const painted = shown(store)
    await close
    await new Promise(r => setTimeout(r, 0))
    expect({ painted: painted.length, settled: shown(store).length }).toEqual({ painted: 600, settled: 600 })
    expect(shown(store)).toEqual(tailOf('X', 600))
  })

  it('does not repaint a stale earlier visit after the chat grew', async () => {
    const store = makeStore()
    await openAndPageAll(store)
    await store.dispatch(switchSlot('Y'))
    await store.dispatch(switchSlot('X'))
    // The chat moves on while open: live turns, each settled by a refresh.
    grow('X', 300)
    await store.dispatch(refreshSlot('X'))
    const before = shown(store)
    expect(before).toEqual(tailOf('X', 900))
    await newChat(store)
    store.dispatch(setActiveSlot(null))
    const back = store.dispatch(switchSlot('X'))
    expect(shown(store)).toEqual(before)
    await back
    expect(shown(store)).toEqual(before)
  })

  it('returning from a resumed history chat keeps the full transcript', async () => {
    const store = makeStore()
    await openAndPageAll(store)
    await store.dispatch(resumeFromHistory({ key: 'Y', title: 'Y' }))
    expect(store.getState().chat.activeSlot).toBe('Y')
    const back = store.dispatch(switchSlot('X'))
    expect(shown(store)).toEqual(tailOf('X', 600))
    await back
    expect(shown(store)).toEqual(tailOf('X', 600))
  })
})
