/**
 * `switchSlot` settlements of a read that a later switch superseded.
 *
 * The thunk never aborts an earlier read of the same slot. A delayed read R1
 * of slot A, captured mid-turn N, can land after the user went to B (R2) and
 * back to A (R3). By then N ended in the background and N+1 is streaming on
 * A. The tab recorded N as ended, so R1's page reads as an ended-turn page,
 * and merged it would finalize N+1's live row and idle the slot. The newest
 * request owns the pane: an older settlement applies nothing.
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
const NEXT_TURN = 91

function makeStore() {
  const store = configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer } })
  store.dispatch(setActiveSlot(SLOT))
  return store
}
type Store = ReturnType<typeof makeStore>
type Chat = ReturnType<typeof chatReducer>

const chat = (store: Store) => store.getState().chat
const busy = (store: Store) => selectComposerBusy(store.getState() as never, SLOT)
const replies = (s: Chat) => s.messages
  .filter(m => m.role === 'assistant' || m.role === 'streaming')
  .map(m => ({ role: m.role, content: m.content }))

function slots(running: boolean, turn: number) {
  return sseSlots([
    { key: SLOT, messages: 1, running, stopping: false, mode: '', turn, turn_gen: GEN },
    { key: OTHER_SLOT, messages: 0, running: false, stopping: false, mode: '' },
  ] as never)
}

function fulfil(slot: string, requestId: string, page: {
  running: boolean; turn?: number; hasMore: boolean; nextBefore: number; messages: unknown[]
}) {
  return switchSlot.fulfilled({
    key: slot, running: page.running, ...(page.turn === undefined ? {} : { turn: page.turn, turn_gen: GEN }),
    hasMore: page.hasMore, nextBefore: page.nextBefore, total: page.messages.length,
    queue: [], stopping: false, messages: page.messages,
  } as never, requestId, slot)
}

/** R1's page: captured while turn N was still streaming its first chunk. */
const r1Page = {
  running: true, turn: TURN, hasMore: true, nextBefore: 3,
  messages: [
    { role: 'user', content: 'first request', cls: 'msg msg-u', meta: { mid: 'u-90' } },
    { role: 'streaming', content: 'first ', cls: 'msg msg-a', seq: 1, gen: GEN },
  ],
}

/** R3's page: captured while turn N+1 streams. */
const r3Page = {
  running: true, turn: NEXT_TURN, hasMore: true, nextBefore: 40,
  messages: [
    { role: 'user', content: 'first request', cls: 'msg msg-u', meta: { mid: 'u-90' } },
    { role: 'assistant', content: 'first answer', cls: 'msg msg-a', meta: { mid: 'a-90' } },
    { role: 'user', content: 'second request', cls: 'msg msg-u', meta: { mid: 'u-91' } },
    { role: 'streaming', content: 'second ', cls: 'msg msg-a', seq: 3, gen: GEN },
  ],
}

/** Turn N streams on A; a read R1 of A is issued and delayed; the user goes to
 *  B (R2 settles); N ends and N+1 opens on A in the background; the user
 *  switches back to A (R3 pending, A's cache restored with N+1's live row). */
function supersedeR1(store: Store) {
  store.dispatch(slots(true, TURN))
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'first request', meta: { mid: 'u-90' } }))
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'first ', seq: 1, gen: GEN }))
  store.dispatch(switchSlot.pending('r1-a', SLOT))
  store.dispatch(switchSlot.pending('r2-b', OTHER_SLOT))
  store.dispatch(fulfil(OTHER_SLOT, 'r2-b', { running: false, hasMore: false, nextBefore: 0, messages: [] }))
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'answer', seq: 2, gen: GEN }))
  store.dispatch(slots(false, TURN))
  store.dispatch(sseChatMessage({ slot: SLOT, role: '_done', content: '', turn: TURN, turn_gen: GEN }))
  expect(chat(store).endedTurn[SLOT]).toEqual({ gen: GEN, turn: TURN })
  store.dispatch(slots(true, NEXT_TURN))
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'second request', meta: { mid: 'u-91' } }))
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'second ', seq: 3, gen: GEN }))
  store.dispatch(switchSlot.pending('r3-a', SLOT))
}

function expectLiveSuccessor(store: Store) {
  expect(replies(chat(store))).toEqual([
    { role: 'assistant', content: 'first answer' },
    { role: 'streaming', content: 'second ' },
  ])
  expect(chat(store)).toMatchObject({ slotRunning: true, slotState: 'streaming' })
  expect(busy(store)).toBe(true)
}

/** N+1's next chunk must extend the same bubble, not open a second one. */
function expectNextChunkAppends(store: Store) {
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'half', seq: 4, gen: GEN }))
  expect(replies(chat(store))).toEqual([
    { role: 'assistant', content: 'first answer' },
    { role: 'streaming', content: 'second half' },
  ])
  expect(busy(store)).toBe(true)
}

describe('switch settlements of a read a later switch superseded', () => {
  it('a superseded switch page cannot finalize the live turn that followed it', () => {
    const store = makeStore()
    supersedeR1(store)
    store.dispatch(fulfil(SLOT, 'r3-a', r3Page))
    expectLiveSuccessor(store)
    const settled = chat(store)

    store.dispatch(fulfil(SLOT, 'r1-a', r1Page))

    expectLiveSuccessor(store)
    expect(chat(store).messages).toBe(settled.messages)
    // The paging cursor and the loading flag are still R3's.
    expect(chat(store)).toMatchObject({
      slotHasMore: true, slotOldestIndex: 40, slotCursorKey: SLOT, slotLoading: false,
    })
    expectNextChunkAppends(store)
  })

  it('a superseded switch page landing while the newer switch is in flight applies nothing', () => {
    const store = makeStore()
    supersedeR1(store)
    const restored = chat(store)
    expect(restored.slotSwitchRequestId).toBe('r3-a')
    expectLiveSuccessor(store)

    store.dispatch(fulfil(SLOT, 'r1-a', r1Page))

    expect(chat(store)).toBe(restored)
    // R3 still owns the pane and its page lands as it would have alone.
    store.dispatch(fulfil(SLOT, 'r3-a', r3Page))
    expectLiveSuccessor(store)
    expect(chat(store)).toMatchObject({
      slotSwitchRequestId: null, slotHasMore: true, slotOldestIndex: 40, slotCursorKey: SLOT, slotLoading: false,
    })
    expectNextChunkAppends(store)
  })

  it('a superseded switch failure cannot empty the live turn that followed it', () => {
    const store = makeStore()
    supersedeR1(store)
    store.dispatch(fulfil(SLOT, 'r3-a', r3Page))
    const settled = chat(store)

    store.dispatch(switchSlot.rejected(new Error('Failed to fetch'), 'r1-a', SLOT))

    expect(chat(store)).toBe(settled)
    expectLiveSuccessor(store)
    expectNextChunkAppends(store)
  })
})
