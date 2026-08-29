/**
 * A slot refresh must not swallow the reply that is still streaming.
 *
 * `switchSlot.fulfilled` re-attaches a trailing `assistant`/`streaming` row the
 * fetched page does not carry; `refreshSlot.fulfilled` rebuilds `messages` from
 * the page and re-injects only `permission` cards (and, since #6825, unconfirmed
 * `user` rows), so a `streaming` tail was dropped outright. The two reducers must
 * not diverge here.
 *
 * Pinned here: a `streaming` row is a CLIENT-ONLY role the server never persists
 * and never emits, so a page can never legitimately contain it and its absence
 * can only mean the page predates the in-flight text. The converse is pinned too
 * (fourth test): once the turn is over the page IS authoritative, so a stale
 * partial is still dropped rather than re-attached below the answer.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({ api: { chatSlotDetail: vi.fn() } }))

import chatReducer, { switchSlot, refreshSlot, setActiveSlot, replaceMessages } from './chatSlice'
import { api } from '../api/client'
import type { ChatMessage } from '../types'

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    middleware: (getDefault) => getDefault({ immutableCheck: false, serializableCheck: false }),
  })
}

const detail = vi.mocked(api.chatSlotDetail)

const SLOT = 'chat-1-1788026016'

/** A slot-detail response. `messages` is what the server would hand back. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural stand-in for the api client's response type
const page = (messages: unknown[], running: boolean): any => ({
  messages, running, stopping: false, has_more: false, total: messages.length, next_before: 0, queue: [],
})

const askRow = { role: 'user', content: 'summarise this', ts: '2026-08-29T18:00:00.000Z', meta: { mid: 'm-1' } }
/** An earlier segment the server already flushed, so the page carries it. */
const flushedSegment = { role: 'assistant', content: 'First segment. ', ts: '2026-08-29T18:00:01.000Z', meta: { mid: 'm-2' } }
/** The same reply once the turn ended and the server flushed the whole thing. */
const finishedReply = { role: 'assistant', content: 'First segment. Second half.', ts: '2026-08-29T18:00:02.000Z', meta: { mid: 'm-3' } }

/** Seed the active slot with `rows` verbatim, the state a live stream leaves. */
function storeWith(rows: ChatMessage[]) {
  const store = makeStore()
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(replaceMessages(rows))
  expect(store.getState().chat.messages).toHaveLength(rows.length)
  return store
}

const streamingRow = (content: string): ChatMessage =>
  ({ role: 'streaming', content, cls: 'msg msg-a', meta: { clientTs: 'c-1' } }) as ChatMessage

const rowsOf = (store: ReturnType<typeof makeStore>) =>
  store.getState().chat.messages.map(m => `${m.role}:${m.content}`)

describe('refreshSlot.fulfilled keeps the in-flight reply', () => {
  beforeEach(() => vi.clearAllMocks())

  it('does not drop a streaming row while the turn is still running', async () => {
    // Reachable from the WS-reconnect dispatch, which carries no `running` check.
    const store = storeWith([askRow as ChatMessage, streamingRow('Second half so far')])
    detail.mockResolvedValue(page([askRow], true))

    await store.dispatch(refreshSlot(SLOT))

    expect(rowsOf(store)).toEqual(['user:summarise this', 'streaming:Second half so far'])
  })

  it('keeps the streaming row BELOW a segment the server already flushed', async () => {
    const store = storeWith([askRow as ChatMessage, { ...flushedSegment } as ChatMessage, streamingRow('Second half so far')])
    detail.mockResolvedValue(page([askRow, flushedSegment], true))

    await store.dispatch(refreshSlot(SLOT))

    expect(rowsOf(store)).toEqual([
      'user:summarise this',
      'assistant:First segment. ',
      'streaming:Second half so far',
    ])
  })

  it('leaves the streaming role alone rather than finalizing it mid-turn', async () => {
    // Coercing to `assistant` while running makes the resuming chunk handler push
    // a NEW row, splitting one reply across two bubbles until chat_done heals it.
    const store = storeWith([askRow as ChatMessage, streamingRow('partial')])
    detail.mockResolvedValue(page([askRow], true))

    await store.dispatch(refreshSlot(SLOT))

    const tail = store.getState().chat.messages.at(-1)
    expect(tail?.role).toBe('streaming')
    expect(tail?.meta?.clientTs).toBe('c-1')
  })
})

describe('refreshSlot.fulfilled still defers to a finished page', () => {
  beforeEach(() => vi.clearAllMocks())

  it('drops a stale partial once the turn is over', async () => {
    // Negative control: a missed chat_done strands a partial, and re-attaching it
    // would print it a second time below the answer the page now carries in full.
    const store = storeWith([askRow as ChatMessage, streamingRow('First segment. Second ha')])
    detail.mockResolvedValue(page([askRow, finishedReply], false))

    await store.dispatch(refreshSlot(SLOT))

    expect(rowsOf(store)).toEqual(['user:summarise this', 'assistant:First segment. Second half.'])
  })

  it('applies the page verbatim when there is no in-flight row to keep', async () => {
    const store = storeWith([askRow as ChatMessage])
    detail.mockResolvedValue(page([askRow, finishedReply], false))

    await store.dispatch(refreshSlot(SLOT))

    expect(rowsOf(store)).toEqual(['user:summarise this', 'assistant:First segment. Second half.'])
  })
})

describe('switchSlot.fulfilled parity is untouched', () => {
  beforeEach(() => vi.clearAllMocks())

  it('still keeps a mid-turn streaming row as streaming', async () => {
    const store = storeWith([askRow as ChatMessage, streamingRow('partial')])
    detail.mockResolvedValue(page([askRow], true))

    await store.dispatch(switchSlot(SLOT))

    const tail = store.getState().chat.messages.at(-1)
    expect(tail?.role).toBe('streaming')
    expect(tail?.content).toBe('partial')
  })
})
