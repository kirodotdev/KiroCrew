import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { sseChatMessage, setActiveSlot } from '../store/chatSlice'
import type { ChatMessage } from '../store/chatSlice'
import './mockApiClient'

/**
 * The chunk replay floor must outlive `_done`.
 *
 * WS delivery is at-least-once, and chunk seqs never restart within a gateway
 * generation. A chunk redelivered after the turn's `chat_done` therefore sits
 * at or below the floor. When `_done` cleared the floor, that chunk met no
 * guard, found no `streaming` row (the reply had just been finalized) and
 * pushed a second bubble repeating the reply's text. The floor is now cleared
 * only when a new turn starts (a non-steer `user` echo or an `inject` row).
 *
 * The server's `assistant` frame is the authoritative body of a segment; the
 * reconciled row adopts it as `rawText` too, so no consumer keeps reading the
 * client's accumulated copy.
 */

const ACTIVE = 'chat-active'
const BG = 'chat-bg'
const GEN = 'g1'

function makeStore() {
  const store = configureStore({ reducer: { chat: chatReducer } })
  store.dispatch(setActiveSlot(ACTIVE))
  return store
}

type Store = ReturnType<typeof makeStore>

const replies = (msgs: ChatMessage[]) => msgs.filter(m => m.role === 'streaming' || m.role === 'assistant')
const activeReplies = (s: Store) => replies(s.getState().chat.messages)
const bgReplies = (s: Store) => replies(s.getState().chat.slotMessages[BG] ?? [])

function streamTurn(store: Store, slot: string) {
  store.dispatch(sseChatMessage({ slot, role: 'chunk', content: 'Hello ', seq: 1, gen: GEN }))
  store.dispatch(sseChatMessage({ slot, role: 'chunk', content: 'world', seq: 2, gen: GEN }))
  store.dispatch(sseChatMessage({ slot, role: '_done', content: '' }))
}

describe('active slot: _done keeps the replay floor', () => {
  it('drops a chunk redelivered after _done instead of opening a second bubble', () => {
    const store = makeStore()
    streamTurn(store, ACTIVE)
    expect(store.getState().chat.lastChunkSeq).toBe(2)

    store.dispatch(sseChatMessage({ slot: ACTIVE, role: 'chunk', content: 'Hello ', seq: 1, gen: GEN }))
    store.dispatch(sseChatMessage({ slot: ACTIVE, role: 'chunk', content: 'world', seq: 2, gen: GEN }))

    const rows = activeReplies(store)
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ role: 'assistant', content: 'Hello world' })
  })

  it('a new turn clears the floor so a restarted counter is not swallowed', () => {
    const store = makeStore()
    streamTurn(store, ACTIVE)
    store.dispatch(sseChatMessage({ slot: ACTIVE, role: 'user', content: 'again', meta: { mid: 'u2' } }))
    expect(store.getState().chat.lastChunkSeq).toBeUndefined()

    store.dispatch(sseChatMessage({ slot: ACTIVE, role: 'chunk', content: 'Fresh', seq: 1, gen: GEN }))
    expect(activeReplies(store).map(m => m.content)).toEqual(['Hello world', 'Fresh'])
  })

  it('a steer does not clear the floor', () => {
    const store = makeStore()
    store.dispatch(sseChatMessage({ slot: ACTIVE, role: 'chunk', content: 'Hello', seq: 1, gen: GEN }))
    store.dispatch(sseChatMessage({ slot: ACTIVE, role: 'user', content: 'also', meta: { mid: 'u3', steer: true } }))
    expect(store.getState().chat.lastChunkSeq).toBe(1)
  })

  it('the reconciled assistant row adopts the server body as rawText', () => {
    const store = makeStore()
    store.dispatch(sseChatMessage({ slot: ACTIVE, role: 'chunk', content: 'dup dup dup', seq: 1, gen: GEN }))
    store.dispatch(sseChatMessage({ slot: ACTIVE, role: 'assistant', content: 'dup', meta: { mid: 'a1' } }))
    const [row] = activeReplies(store)
    expect(row).toMatchObject({ role: 'assistant', content: 'dup', rawText: 'dup' })
  })
})

describe('background slot: _done keeps the replay floor', () => {
  it('drops a chunk redelivered after _done instead of opening a second bubble', () => {
    const store = makeStore()
    streamTurn(store, BG)
    expect(store.getState().chat.slotRun[BG]?.lastChunkSeq).toBe(2)

    store.dispatch(sseChatMessage({ slot: BG, role: 'chunk', content: 'world', seq: 2, gen: GEN }))

    const rows = bgReplies(store)
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ role: 'assistant', content: 'Hello world' })
  })

  it('an inject row starting a new turn clears the floor', () => {
    const store = makeStore()
    streamTurn(store, BG)
    store.dispatch(sseChatMessage({ slot: BG, role: 'inject', content: '[Cron notification] tick', meta: { mid: 'i1' } }))
    expect(store.getState().chat.slotRun[BG]?.lastChunkSeq).toBeUndefined()
  })

  it('the reconciled assistant row adopts the server body as rawText', () => {
    const store = makeStore()
    store.dispatch(sseChatMessage({ slot: BG, role: 'chunk', content: 'dup dup', seq: 1, gen: GEN }))
    store.dispatch(sseChatMessage({ slot: BG, role: 'assistant', content: 'dup', meta: { mid: 'a2' } }))
    const [row] = bgReplies(store)
    expect(row).toMatchObject({ role: 'assistant', content: 'dup', rawText: 'dup' })
  })
})
