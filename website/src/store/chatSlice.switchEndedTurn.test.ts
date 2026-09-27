/**
 * `switchSlot.fulfilled` with a page captured before its turn ended.
 *
 * The tab already finished the reply while the slot was in the background
 * (its `chat_done` applied there) and recorded the ended turn. The switch
 * page still carries that turn's partial as a `streaming` row and says
 * `running: true`. The merge re-attaches the finished local reply and drops
 * the page's streaming rows beside it, so the page's partial must still be
 * `streaming` when the merge runs: finalized first, it reads as a reply and
 * the transcript shows both "partial " and "partial answer".
 */
import { describe, it, expect, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({ api: { chatSlotDetail: vi.fn() } }))

import chatReducer, { setActiveSlot, selectComposerBusy, sseChatMessage, switchSlot } from './chatSlice'
import dashboardReducer, { sseSlots } from './dashboardSlice'

const SLOT = 'chat-1'
const OTHER_SLOT = 'chat-2'
const GEN = 'gateway-a'
const TURN = 90

function makeStore() {
  const store = configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer } })
  store.dispatch(setActiveSlot(SLOT))
  return store
}
type Store = ReturnType<typeof makeStore>
type Chat = ReturnType<typeof chatReducer>

const chat = (store: Store) => store.getState().chat
const replies = (s: Chat) => s.messages.filter(m => m.role === 'assistant' || m.role === 'streaming')

function switchPage(store: Store, slot: string, requestId: string, messages: unknown[], running: boolean) {
  store.dispatch(switchSlot.pending(requestId, slot))
  store.dispatch(switchSlot.fulfilled({
    key: slot, running, turn: TURN, turn_gen: GEN, hasMore: false, total: messages.length,
    queue: [], stopping: false, messages,
  } as never, requestId, slot))
}

describe('switching to a slot whose turn ended in the background', () => {
  it('a mid-turn switch page does not render the reply its background chat_done finished twice', () => {
    const store = makeStore()
    store.dispatch(sseSlots([{ key: SLOT, messages: 1, running: true, stopping: false, mode: '', turn: TURN, turn_gen: GEN }] as never))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'request', meta: { mid: 'u-90' } }))
    switchPage(store, OTHER_SLOT, 'leave-mid-turn', [], false)
    // In the background the tab receives the server's final reply and the
    // turn's identified `chat_done`, which finishes the reply and records the
    // ended turn. No chunk reached the tab, so the slot has no replay floor.
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'partial answer', meta: { mid: 'a-90' } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: '_done', content: '', turn: TURN, turn_gen: GEN }))
    expect(chat(store).endedTurn[SLOT]).toEqual({ gen: GEN, turn: TURN })

    switchPage(store, SLOT, 'back-to-ended-turn', [
      { role: 'user', content: 'request', cls: 'msg msg-u', meta: { mid: 'u-90' } },
      { role: 'streaming', content: 'partial ', cls: 'msg msg-a', seq: 1, gen: GEN },
    ], true)

    expect(replies(chat(store)).map(m => ({ role: m.role, content: m.content }))).toEqual([
      { role: 'assistant', content: 'partial answer' },
    ])
    expect(chat(store).messages.map(m => m.content)).toEqual(['request', 'partial answer'])
    expect(chat(store)).toMatchObject({ slotState: 'idle', slotRunning: false })
    expect(selectComposerBusy(store.getState() as never, SLOT)).toBe(false)
    // The replay floor is seeded from the page's streaming row, as `_done`
    // would have left it: a redelivered chunk of the ended turn is dropped
    // rather than reopening the reply and the busy composer.
    expect(chat(store)).toMatchObject({ lastChunkSeq: 1, lastChunkGen: GEN })
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial ', seq: 1, gen: GEN }))
    expect(chat(store).messages.map(m => m.content)).toEqual(['request', 'partial answer'])
    expect(selectComposerBusy(store.getState() as never, SLOT)).toBe(false)
  })
})
