/**
 * `switchSlot.fulfilled` for a slot whose turn ended while this tab heard
 * nothing: the socket dropped while the chat was off screen, so no chunk and
 * no `chat_done` reached it, and the cached copy still ends in that turn's
 * partial `streaming` row. The switch page holds the finished reply. The view
 * must show the page's reply, not the cached partial.
 */
import { describe, it, expect, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({ api: { chatSlotDetail: vi.fn() } }))

import chatReducer, { setActiveSlot, sseChatMessage, switchSlot } from './chatSlice'
import dashboardReducer, { sseSlots } from './dashboardSlice'

const SLOT = 'chat-1'
const OTHER_SLOT = 'chat-2'
const GEN = 'gateway-a'
const TURN = 7

function makeStore() {
  const store = configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer } })
  store.dispatch(setActiveSlot(SLOT))
  return store
}
type Store = ReturnType<typeof makeStore>

const chat = (store: Store) => store.getState().chat

function switchPage(store: Store, slot: string, requestId: string, messages: unknown[], running: boolean) {
  store.dispatch(switchSlot.pending(requestId, slot))
  store.dispatch(switchSlot.fulfilled({
    key: slot, running, turn: TURN, turn_gen: GEN, hasMore: false, total: messages.length,
    queue: [], stopping: false, messages,
  } as never, requestId, slot))
}

describe('switching back to a slot whose turn ended while the socket was down', () => {
  it('shows the finished reply from the page, not the cached partial', () => {
    const store = makeStore()
    store.dispatch(sseSlots([{ key: SLOT, messages: 1, running: true, stopping: false, mode: '', turn: TURN, turn_gen: GEN }] as never))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'request', meta: { mid: 'u-7' } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'chunk 0 ', seq: 1, gen: GEN }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'chunk 1 ', seq: 2, gen: GEN }))
    expect(chat(store).messages.map(m => [m.role, m.content])).toEqual([
      ['user', 'request'], ['streaming', 'chunk 0 chunk 1 '],
    ])
    // Leave mid-turn. The socket then drops: nothing more reaches this tab.
    switchPage(store, OTHER_SLOT, 'leave-mid-turn', [], false)

    // Back after the turn ended on the server.
    switchPage(store, SLOT, 'back-after-end', [
      { role: 'user', content: 'request', cls: 'msg msg-u', meta: { mid: 'u-7' } },
      { role: 'assistant', content: 'chunk 0 chunk 1 chunk 2 chunk 3', cls: 'msg msg-a', meta: { mid: 'a-7' } },
    ], false)

    expect(chat(store).messages.map(m => [m.role, m.content])).toEqual([
      ['user', 'request'], ['assistant', 'chunk 0 chunk 1 chunk 2 chunk 3'],
    ])
    expect(chat(store)).toMatchObject({ slotState: 'idle', slotRunning: false })
  })

  it('drops the cached partial a slots snapshot already finalized', () => {
    const store = makeStore()
    store.dispatch(sseSlots([{ key: SLOT, messages: 1, running: true, stopping: false, mode: '', turn: TURN, turn_gen: GEN }] as never))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'request', meta: { mid: 'u-7' } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'chunk 0 ', seq: 1, gen: GEN }))
    switchPage(store, OTHER_SLOT, 'leave-mid-turn', [], false)
    // The reconnect's slots snapshot says the slot is idle.
    store.dispatch(sseSlots([
      { key: SLOT, messages: 2, running: false, stopping: false, mode: '', turn: TURN, turn_gen: GEN },
      { key: OTHER_SLOT, messages: 0, running: false, stopping: false, mode: '', turn: 1, turn_gen: GEN },
    ] as never))

    switchPage(store, SLOT, 'back-after-end', [
      { role: 'user', content: 'request', cls: 'msg msg-u', meta: { mid: 'u-7' } },
      { role: 'assistant', content: 'chunk 0 chunk 1', cls: 'msg msg-a', meta: { mid: 'a-7' } },
    ], false)

    expect(chat(store).messages.map(m => [m.role, m.content])).toEqual([
      ['user', 'request'], ['assistant', 'chunk 0 chunk 1'],
    ])
  })

  it('keeps the cached partial when the page does not answer its prompt yet', () => {
    const store = makeStore()
    store.dispatch(sseSlots([{ key: SLOT, messages: 1, running: true, stopping: false, mode: '', turn: TURN, turn_gen: GEN }] as never))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'first', meta: { mid: 'u-6' } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'first reply', meta: { mid: 'a-6' } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'second', meta: { mid: 'u-7' } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial ', seq: 1, gen: GEN }))
    switchPage(store, OTHER_SLOT, 'leave-mid-turn', [], false)

    // The page answers the first prompt only: it does not supersede the partial.
    switchPage(store, SLOT, 'back-older-page', [
      { role: 'user', content: 'first', cls: 'msg msg-u', meta: { mid: 'u-6' } },
      { role: 'assistant', content: 'first reply', cls: 'msg msg-a', meta: { mid: 'a-6' } },
      { role: 'user', content: 'second', cls: 'msg msg-u', meta: { mid: 'u-7' } },
    ], false)

    expect(chat(store).messages.map(m => m.content)).toEqual(['first', 'first reply', 'second', 'partial '])
  })
})
