/**
 * The composer must idle once the live slots frame says the active turn ended.
 *
 * A turn whose `_done` the tab never applied left `slotState` busy. Two ways
 * in: the frame is lost, or a history fetch taken mid-turn resolves after the
 * `_done` and writes its stale `running: true` back. A later slots snapshot
 * cleared `slotRunning`, so the transcript and footer read idle, but
 * `selectComposerBusy` also reads the stream state, and the composer kept
 * offering Steer with no agent running.
 *
 * The live `sseSlots` frame now settles it. An HTTP `fetchSlots` reply does
 * not: it can predate a turn whose frames already streamed in.
 */
import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  setActiveSlot,
  sseChatMessage,
  startLocalTurn,
  switchSlot,
  syncSlotRunningFromServer,
  selectComposerBusy,
} from '../store/chatSlice'
import dashboardReducer, { sseSlots, fetchSlots } from '../store/dashboardSlice'
import './mockApiClient'

const SLOT = 'chat-1'

function makeStore() {
  const store = configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer } })
  store.dispatch(setActiveSlot(SLOT))
  return store
}
type Store = ReturnType<typeof makeStore>

const row = (running: boolean) => ({ key: SLOT, messages: 1, running, stopping: false, mode: '' }) as never
const liveFrame = (store: Store, running: boolean) => store.dispatch(sseSlots([row(running)]))
const httpReply = (store: Store, running: boolean) => store.dispatch(fetchSlots.fulfilled([row(running)], 'req-1', undefined as never))
const busy = (store: Store) => selectComposerBusy(store.getState() as never, SLOT)
const chat = (store: Store) => store.getState().chat

/** A turn that streamed, was confirmed running, and whose final reply landed
 *  without its `_done`; then the page's snapshot sync idled `slotRunning`. */
function strandedByLostDone() {
  const store = makeStore()
  liveFrame(store, true)
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial', seq: 1 }))
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'final answer' }))
  store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: false, stopping: false }))
  return store
}

describe('settleEndedActiveTurn (live slots frame)', () => {
  it('leaves the composer busy after a lost _done until a live frame reports the turn ended', () => {
    const store = strandedByLostDone()
    expect(chat(store).slotRunning).toBe(false)
    expect(chat(store).slotState).toBe('streaming')
    expect(busy(store)).toBe(true)

    liveFrame(store, false)
    expect(chat(store).slotState).toBe('idle')
    expect(chat(store).lastChunkSeq).toBeUndefined()
    expect(busy(store)).toBe(false)
    expect(chat(store).messages.at(-1)).toMatchObject({ role: 'assistant', content: 'final answer' })
  })

  it('finalizes a reply whose final frame was lost along with its _done', () => {
    const store = makeStore()
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'streamed text', seq: 1 }))
    liveFrame(store, false)
    expect(chat(store).slotState).toBe('idle')
    expect(chat(store).messages.at(-1)).toMatchObject({ role: 'assistant', content: 'streamed text' })
  })

  it('heals a history fetch that resolved after the _done and restored running', () => {
    const store = makeStore()
    store.dispatch(switchSlot.pending('s1', SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'answer', seq: 1 }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    // The fetch was served while the turn still ran.
    store.dispatch(switchSlot.fulfilled({
      key: SLOT, running: true, hasMore: false, total: 2, queue: [], stopping: false,
      messages: [{ role: 'user', content: 'hi', cls: '' }, { role: 'assistant', content: 'answer', cls: '' }],
    } as never, 's1', SLOT))
    expect(chat(store).slotState).toBe('streaming')
    expect(busy(store)).toBe(true)

    liveFrame(store, false)
    expect(chat(store).slotState).toBe('idle')
    expect(chat(store).slotRunning).toBe(false)
    expect(busy(store)).toBe(false)
  })

  it('never settles on an HTTP reply, which can predate a live turn', () => {
    const store = makeStore()
    liveFrame(store, true)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'still going', seq: 1 }))
    httpReply(store, false)
    expect(chat(store).slotState).toBe('streaming')
    expect(chat(store).messages.at(-1)).toMatchObject({ role: 'streaming', content: 'still going' })
    // The live stream continues into the same row.
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' on', seq: 2 }))
    expect(chat(store).messages.filter(m => m.role === 'streaming' || m.role === 'assistant').map(m => m.content)).toEqual(['still going on'])
  })

  it('does not settle while the live frame reports the slot running', () => {
    const store = makeStore()
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'x', seq: 1 }))
    liveFrame(store, true)
    expect(chat(store).slotState).toBe('streaming')
  })

  it('does not settle over a local send awaiting confirmation', () => {
    const store = makeStore()
    store.dispatch(startLocalTurn(SLOT))
    liveFrame(store, false)
    expect(chat(store).slotRunning).toBe(true)
    expect(chat(store).pendingTurnSlot).toBe(SLOT)
  })

  it('lets the next turn stream normally after a settlement', () => {
    const store = strandedByLostDone()
    liveFrame(store, false)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next turn', seq: 1 }))
    expect(chat(store).slotState).toBe('streaming')
    expect(chat(store).messages.at(-1)).toMatchObject({ role: 'streaming', content: 'next turn' })
    expect(chat(store).messages.at(-2)).toMatchObject({ role: 'assistant', content: 'final answer' })
  })

  it('ignores another slot reporting not running', () => {
    const store = makeStore()
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'x', seq: 1 }))
    store.dispatch(sseSlots([row(true), { key: 'chat-2', messages: 1, running: false, stopping: false, mode: '' } as never]))
    expect(chat(store).slotState).toBe('streaming')
  })
})
