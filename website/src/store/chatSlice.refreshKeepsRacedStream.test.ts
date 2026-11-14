/** A refresh of the open pane must not lose reply text that streamed in while
 *  its page was in flight. The page is read before those chunks land, so it
 *  either carries no streaming row yet or one folded to an older `seq` than the
 *  tab's replay floor; the floor stays put, so the lost chunks are never replayed
 *  and the text stays missing until the end-of-turn refresh. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

type Row = { role: string; content: string; cls: string; ts?: string; seq?: number; gen?: string; meta?: Record<string, unknown> }

let PAGE: Row[] = []
let RUNNING = true
let DURING_FETCH: (() => void) | null = null

vi.mock('../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn((slot: string) => {
      const messages = [...PAGE]
      if (DURING_FETCH) {
        const fire = DURING_FETCH
        DURING_FETCH = null
        fire()
      }
      return Promise.resolve({
        key: slot, messages, has_more: false, next_before: 0, total: messages.length, running: RUNNING, queue: [],
      })
    }),
  },
}))

import chatReducer, { appendMessage, refreshSlot, setActiveSlot, sseChatMessage } from './chatSlice'
import { streamFlush } from './streamFlush'

const SLOT = 'slot-raced-stream'
const GEN = 'g1'
const TS = '2026-11-13T17:00:00.000Z'
const USER: Row = { role: 'user', content: 'q', cls: 'msg msg-u', ts: TS, meta: { mid: 'u1' } }
const piece = (seq: number) => 'abcdefgh'[seq - 1]
const text = (upTo: number) => Array.from({ length: upTo }, (_, i) => piece(i + 1)).join('')
const pageStream = (seq: number): Row => ({ role: 'streaming', content: text(seq), cls: 'msg msg-a', seq, gen: GEN })

const makeStore = () => configureStore({
  reducer: { chat: chatReducer },
  middleware: getDefault => getDefault({ immutableCheck: false, serializableCheck: false }),
})
type Store = ReturnType<typeof makeStore>

const flush = (store: Store, from: number, to: number) => {
  const seqs = Array.from({ length: to - from + 1 }, (_, i) => from + i)
  store.dispatch(streamFlush({
    slots: [{ slot: SLOT, content: { text: seqs.map(piece).join(''), seq: to, gen: GEN, parts: seqs.map(seq => ({ seq, text: piece(seq) })) } }],
    subagents: [],
    recency: [],
  }))
}

const rows = (store: Store) => store.getState().chat.messages.map(m => `${m.role}:${m.content}`)

function streamingStore(upTo: number): Store {
  const store = makeStore()
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'q', ts: TS, meta: { mid: 'u1' } }))
  if (upTo > 0) flush(store, 1, upTo)
  return store
}

describe('refreshSlot.fulfilled keeps streamed text the page predates', () => {
  beforeEach(() => {
    PAGE = []
    RUNNING = true
    DURING_FETCH = null
  })

  it('keeps the live reply when the running page has no streaming row yet', async () => {
    const store = streamingStore(0)
    PAGE = [USER]
    DURING_FETCH = () => flush(store, 1, 3)

    await store.dispatch(refreshSlot(SLOT))

    expect(rows(store)).toEqual(['user:q', 'streaming:abc'])
    flush(store, 4, 4)
    expect(rows(store)).toEqual(['user:q', 'streaming:abcd'])
  })

  it('keeps the live reply when the page streaming row is behind the replay floor', async () => {
    const store = streamingStore(3)
    PAGE = [USER, pageStream(3)]
    DURING_FETCH = () => flush(store, 4, 5)

    await store.dispatch(refreshSlot(SLOT))

    expect(rows(store)).toEqual(['user:q', 'streaming:abcde'])
    flush(store, 6, 6)
    expect(rows(store)).toEqual(['user:q', 'streaming:abcdef'])
  })

  it('takes the page streaming row when it is at or past the replay floor', async () => {
    const store = streamingStore(3)
    PAGE = [USER, pageStream(5)]

    await store.dispatch(refreshSlot(SLOT))

    expect(rows(store)).toEqual(['user:q', 'streaming:abcde'])
  })

  it('keeps the live reply above a send that followed it on screen', async () => {
    const store = streamingStore(0)
    PAGE = [USER]
    DURING_FETCH = () => {
      flush(store, 1, 3)
      store.dispatch(appendMessage({ role: 'user', content: 'q2', cls: 'msg msg-u', ts: TS, meta: { optimistic: true, sendId: 's2' } }))
    }

    await store.dispatch(refreshSlot(SLOT))

    expect(rows(store)).toEqual(['user:q', 'streaming:abc', 'user:q2'])
  })

  it('does not keep the live copy when the running page already carries the flushed reply', async () => {
    const store = streamingStore(0)
    PAGE = [USER, { role: 'assistant', content: 'abc', cls: 'msg msg-a', ts: TS, meta: { mid: 'a1' } }]
    DURING_FETCH = () => flush(store, 1, 3)

    await store.dispatch(refreshSlot(SLOT))

    expect(rows(store)).toEqual(['user:q', 'assistant:abc'])
  })

  it('lets a finished page replace a stale partial', async () => {
    const store = streamingStore(3)
    PAGE = [USER, { role: 'assistant', content: 'abcdefgh', cls: 'msg msg-a', ts: TS, meta: { mid: 'a1' } }]
    RUNNING = false

    await store.dispatch(refreshSlot(SLOT))

    expect(rows(store)).toEqual(['user:q', 'assistant:abcdefgh'])
  })
})
