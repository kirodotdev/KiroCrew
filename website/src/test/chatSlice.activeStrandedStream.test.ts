/**
 * Turn-end recovery is fenced by the server's `{turn, turn_gen}` identity.
 *
 * The ordinary server order is idle `slots` row first, then `chat_done`. The
 * row finalizes the turn and records its identity; the matching `_done` is a
 * duplicate. A late duplicate therefore cannot finalize a successor turn,
 * even after a slot switch. Identity-less payloads retain older-gateway
 * behavior.
 */
import { describe, it, expect, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
// Before the store: the thunks below must load the mocked client.
import './mockApiClient'
import { api } from '../api/client'
import chatReducer, {
  setActiveSlot,
  appendMessage,
  appendQueuedMessage,
  appendSlotMessage,
  removeQueuedMessage,
  sseChatMessage,
  startLocalTurn,
  switchSlot,
  refreshSlot,
  warmSlotCache,
  syncSlotRunningFromServer,
  selectComposerBusy,
} from '../store/chatSlice'
import dashboardReducer, { sseSlots, fetchSlots } from '../store/dashboardSlice'

const SLOT = 'chat-1'
const OTHER_SLOT = 'chat-2'
const GEN = 'gateway-a'

function makeStore() {
  const store = configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer } })
  store.dispatch(setActiveSlot(SLOT))
  return store
}
type Store = ReturnType<typeof makeStore>
type Chat = ReturnType<typeof chatReducer>

const row = (
  running: boolean,
  turn = 1,
  turnGen = GEN,
  key = SLOT,
) => ({ key, messages: 1, running, stopping: false, mode: '', turn, turn_gen: turnGen }) as never
const liveFrame = (store: Store, running: boolean, turn = 1, turnGen = GEN, key = SLOT) =>
  store.dispatch(sseSlots([row(running, turn, turnGen, key)]))
const httpReply = (store: Store, running: boolean, turn = 1) =>
  store.dispatch(fetchSlots.fulfilled([row(running, turn)], 'req-1', undefined as never))
const done = (turn?: number, turnGen?: string, slot = SLOT) => sseChatMessage({
  slot,
  role: '_done',
  content: '',
  ...(turn !== undefined ? { turn } : {}),
  ...(turnGen !== undefined ? { turn_gen: turnGen } : {}),
})
const busy = (store: Store) => selectComposerBusy(store.getState() as never, SLOT)
const chat = (store: Store) => store.getState().chat
const replies = (s: Chat) => s.messages.filter(m => m.role === 'assistant' || m.role === 'streaming')
const turnFlags = (s: Chat) => ({
  slotState: s.slotState,
  slotRunning: s.slotRunning,
  slotStopping: s.slotStopping,
  lastChunkSeq: s.lastChunkSeq,
  pendingTurnSlot: s.pendingTurnSlot,
})

function switchTo(
  store: Store,
  slot: string,
  requestId: string,
  messages: Chat['messages'] = [],
  running = false,
  turn = 0,
  turnGen = GEN,
) {
  store.dispatch(switchSlot.pending(requestId, slot))
  store.dispatch(switchSlot.fulfilled({
    key: slot,
    running,
    turn,
    turn_gen: turnGen,
    hasMore: false,
    total: messages.length,
    queue: [],
    stopping: false,
    messages,
  } as never, requestId, slot))
}

/** A turn whose final row landed but whose `_done` did not. */
function strandedByLostDone(turn = 1) {
  const store = makeStore()
  liveFrame(store, true, turn)
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial', seq: 1 }))
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'final answer' }))
  store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: false, stopping: false }))
  return store
}

describe('live idle slots settlement', () => {
  it('heals a lost _done and records the ended server turn', () => {
    const store = strandedByLostDone(4)
    expect(chat(store).slotState).toBe('streaming')
    expect(busy(store)).toBe(true)

    liveFrame(store, false, 4)

    expect(chat(store).slotState).toBe('idle')
    // The replay floor survives a turn end so at-least-once chunk delivery
    // cannot open a duplicate bubble; the next turn clears it.
    expect(chat(store).lastChunkSeq).toBe(1)
    expect(chat(store).endedTurn[SLOT]).toEqual({ gen: GEN, turn: 4 })
    expect(busy(store)).toBe(false)
    expect(chat(store).messages.at(-1)).toMatchObject({ role: 'assistant', content: 'final answer' })
  })

  it('finalizes a reply whose final frame was lost with its _done', () => {
    const store = makeStore()
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'streamed text', seq: 1 }))
    liveFrame(store, false, 1)
    expect(chat(store).messages.at(-1)).toMatchObject({ role: 'assistant', content: 'streamed text' })
  })

  it('never settles from an HTTP slot-list reply, which can predate the live turn', () => {
    const store = makeStore()
    liveFrame(store, true, 2)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'still going', seq: 1 }))
    httpReply(store, false, 2)
    expect(chat(store).slotState).toBe('streaming')
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' on', seq: 2 }))
    expect(replies(chat(store)).map(m => m.content)).toEqual(['still going on'])
  })

  it('does not settle a pending send from a row already known ended', () => {
    const store = makeStore()
    liveFrame(store, false, 5)
    store.dispatch(startLocalTurn(SLOT))

    liveFrame(store, false, 5)

    expect(chat(store).pendingTurnSlot).toBe(SLOT)
    expect(chat(store).slotRunning).toBe(true)
    expect(busy(store)).toBe(true)
  })

  it('settles a pending send when the idle row reports its newer turn', () => {
    const store = makeStore()
    liveFrame(store, false, 5)
    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'fast answer', seq: 1 }))

    liveFrame(store, false, 6)

    expect(replies(chat(store)).at(-1)).toMatchObject({ role: 'assistant', content: 'fast answer' })
    expect(chat(store).endedTurn[SLOT]).toEqual({ gen: GEN, turn: 6 })
    expect(chat(store).pendingTurnSlot).toBeNull()
    expect(busy(store)).toBe(false)
  })

  it('r6: a new chat whose first turn ends inside one slots window is not left busy', () => {
    // No ended turn is known yet, so the only row the coalesced push delivers
    // names turn 1, followed by that turn's own `_done`.
    const store = makeStore()
    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'instant failure', seq: 1 }))

    liveFrame(store, false, 1)
    expect(chat(store).pendingTurnSlot).toBeNull()
    expect(busy(store)).toBe(false)

    store.dispatch(done(1, GEN))
    expect(busy(store)).toBe(false)
    expect(replies(chat(store)).map(m => m.content)).toEqual(['instant failure'])
  })

  it('r6: with no turn known, a row naming turn 0 (no turn has run) keeps the pending send', () => {
    const store = makeStore()
    store.dispatch(startLocalTurn(SLOT))

    liveFrame(store, false, 0)

    expect(chat(store).pendingTurnSlot).toBe(SLOT)
    expect(busy(store)).toBe(true)
    store.dispatch(done(1, GEN))
    expect(busy(store)).toBe(false)
  })

  it('r6: after a gateway restart the first new turn settles its pending send', () => {
    const store = makeStore()
    liveFrame(store, false, 9, 'gateway-old')
    store.dispatch(startLocalTurn(SLOT))

    liveFrame(store, false, 1, 'gateway-new')

    expect(chat(store).pendingTurnSlot).toBeNull()
    expect(busy(store)).toBe(false)
  })

  it('records idle rows for non-active slots without settling the active turn', () => {
    const store = makeStore()
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'active', seq: 1 }))
    store.dispatch(sseSlots([
      row(true, 1),
      row(false, 9, GEN, OTHER_SLOT),
    ]))
    expect(chat(store).slotState).toBe('streaming')
    expect(chat(store).endedTurn[OTHER_SLOT]).toEqual({ gen: GEN, turn: 9 })
  })

  it('settles a background turn before recording its idle row', () => {
    const store = makeStore()
    switchTo(store, OTHER_SLOT, 'activate-background-peer')
    store.dispatch(sseChatMessage({
      slot: SLOT,
      role: 'chunk',
      content: 'background answer',
      seq: 1,
    }))
    expect(chat(store).slotRun[SLOT]?.state).toBe('streaming')

    store.dispatch(sseSlots([
      row(false, 41, GEN, SLOT),
      row(false, 1, GEN, OTHER_SLOT),
    ]))

    expect(chat(store).slotRun[SLOT]?.state).toBe('idle')
    expect(chat(store).slotMessages[SLOT].at(-1)).toMatchObject({
      role: 'assistant', content: 'background answer', rawText: 'background answer',
    })
    expect(chat(store).endedTurn[SLOT]).toEqual({ gen: GEN, turn: 41 })

    store.dispatch(switchSlot.pending('open-settled-background', SLOT))
    expect(chat(store)).toMatchObject({
      activeSlot: SLOT, slotState: 'idle', slotRunning: false,
    })
    expect(chat(store).messages.at(-1)?.role).toBe('assistant')

    store.dispatch(switchSlot.rejected(
      new Error('detail unavailable'), 'open-settled-background', SLOT,
    ))
    expect(chat(store)).toMatchObject({
      activeSlot: SLOT, slotState: 'idle', slotRunning: false,
    })
    expect(busy(store)).toBe(false)
  })
})

describe('identified chat_done races', () => {
  it('ordinary idle row then matching _done leaves one finished reply and an idle composer', () => {
    const store = makeStore()
    liveFrame(store, true, 8)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Hello', seq: 1 }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' world', seq: 2 }))

    liveFrame(store, false, 8)
    const settled = chat(store)
    store.dispatch(done(8, GEN))
    const after = chat(store)

    expect(replies(after)).toEqual([
      expect.objectContaining({ role: 'assistant', content: 'Hello world', rawText: 'Hello world' }),
    ])
    expect(after.endedTurn[SLOT]).toEqual({ gen: GEN, turn: 8 })
    expect(turnFlags(after)).toEqual(turnFlags(settled))
    expect(busy(store)).toBe(false)
  })

  it('r3: a late done cannot finalize a locally-started successor', () => {
    const store = makeStore()
    liveFrame(store, true, 10)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old reply', seq: 1 }))
    liveFrame(store, false, 10)

    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'new reply', seq: 2 }))
    store.dispatch(done(10, GEN))

    expect(replies(chat(store))).toEqual([
      expect.objectContaining({ role: 'assistant', content: 'old reply' }),
      expect.objectContaining({ role: 'streaming', content: 'new reply' }),
    ])
    expect(chat(store).pendingTurnSlot).toBe(SLOT)
    expect(busy(store)).toBe(true)
  })

  it('r4: a late done cannot finalize a newer background turn after switching away', () => {
    const store = makeStore()
    liveFrame(store, true, 12)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old reply', seq: 1 }))
    liveFrame(store, false, 12)

    switchTo(store, OTHER_SLOT, 'away')
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'new reply', seq: 2 }))
    store.dispatch(done(12, GEN))

    const background = chat(store)
    expect(background.slotMessages[SLOT].filter(m => m.role === 'assistant' || m.role === 'streaming')).toEqual([
      expect.objectContaining({ role: 'assistant', content: 'old reply' }),
      expect.objectContaining({ role: 'streaming', content: 'new reply' }),
    ])
    expect(background.slotRun[SLOT]?.state).toBe('streaming')
  })

  it('r4: the guard survives switching away and back', () => {
    const store = makeStore()
    liveFrame(store, true, 14)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old reply', seq: 1 }))
    liveFrame(store, false, 14)

    switchTo(store, OTHER_SLOT, 'away')
    switchTo(store, SLOT, 'back', [{ role: 'assistant', content: 'old reply', cls: '' }], false, 14)
    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'new reply', seq: 2 }))
    store.dispatch(done(14, GEN))

    expect(replies(chat(store))).toEqual([
      expect.objectContaining({ role: 'assistant', content: 'old reply' }),
      expect.objectContaining({ role: 'streaming', content: 'new reply' }),
    ])
    expect(chat(store).pendingTurnSlot).toBe(SLOT)
    expect(busy(store)).toBe(true)
  })

  it('r5: a successor done finalizes before any running row arrives', () => {
    const store = makeStore()
    liveFrame(store, true, 20)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old reply', seq: 1 }))
    liveFrame(store, false, 20)

    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'fast successor', seq: 2 }))
    store.dispatch(done(21, GEN))

    expect(replies(chat(store)).at(-1)).toMatchObject({ role: 'assistant', content: 'fast successor' })
    expect(chat(store).endedTurn[SLOT]).toEqual({ gen: GEN, turn: 21 })
    expect(chat(store).pendingTurnSlot).toBeNull()
    expect(busy(store)).toBe(false)
  })

  it('a known turn from another gateway generation does not suppress done', () => {
    const store = makeStore()
    liveFrame(store, false, 50, 'old-gateway')
    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'new gateway reply', seq: 1 }))

    store.dispatch(done(1, 'new-gateway'))

    expect(replies(chat(store)).at(-1)).toMatchObject({ role: 'assistant', content: 'new gateway reply' })
    expect(chat(store).endedTurn[SLOT]).toEqual({ gen: 'new-gateway', turn: 1 })
    expect(busy(store)).toBe(false)
  })

  it('an older gateway done without identity finalizes as before', () => {
    const store = makeStore()
    liveFrame(store, false, 3)
    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'legacy reply', seq: 1 }))

    store.dispatch(done())

    expect(replies(chat(store)).at(-1)).toMatchObject({ role: 'assistant', content: 'legacy reply' })
    expect(chat(store).pendingTurnSlot).toBeNull()
    expect(busy(store)).toBe(false)
  })
})

describe('history turn identity', () => {
  it('trusts admission-only running history without a numbered turn identity', () => {
    const store = makeStore()
    liveFrame(store, false, 29)

    store.dispatch(refreshSlot.fulfilled({
      key: SLOT,
      running: true,
      hasMore: false,
      nextBefore: 0,
      total: 0,
      queue: [],
      stopping: false,
      messages: [],
    } as never, 'admission-reserved', SLOT))

    expect(chat(store).slotRunning).toBe(true)
    expect(busy(store)).toBe(true)
  })

  it('does not restore busy from a stale running reply for an ended turn', () => {
    const store = makeStore()
    liveFrame(store, true, 30)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'answer', seq: 1 }))
    store.dispatch(done(30, GEN))

    store.dispatch(switchSlot.pending('stale-history', SLOT))
    store.dispatch(switchSlot.fulfilled({
      key: SLOT,
      running: true,
      turn: 30,
      turn_gen: GEN,
      hasMore: false,
      total: 1,
      queue: [],
      stopping: false,
      messages: [{ role: 'assistant', content: 'answer', cls: '' }],
    } as never, 'stale-history', SLOT))

    expect(chat(store).slotState).toBe('idle')
    expect(chat(store).slotRunning).toBe(false)
    expect(busy(store)).toBe(false)
  })

  it('does not restore busy from a stale refresh for an ended turn', () => {
    const store = makeStore()
    liveFrame(store, true, 31)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'answer', seq: 1 }))
    store.dispatch(done(31, GEN))
    const before = chat(store).messages

    store.dispatch(refreshSlot.fulfilled({
      key: SLOT,
      running: true,
      turn: 31,
      turn_gen: GEN,
      hasMore: false,
      nextBefore: 0,
      total: 1,
      queue: [],
      stopping: false,
      messages: [{ role: 'assistant', content: 'answer', cls: '' }],
    } as never, 'stale-refresh', SLOT))

    expect(chat(store).messages).toBe(before)
    expect(chat(store).slotState).toBe('idle')
    expect(chat(store).slotRunning).toBe(false)
    expect(busy(store)).toBe(false)
  })

  it('leaves the pane as it was when an ended-turn running refresh lands before the successor', () => {
    const store = makeStore()
    liveFrame(store, true, 36)
    liveFrame(store, false, 36)
    const before = chat(store).messages

    store.dispatch(refreshSlot.fulfilled({
      key: SLOT,
      running: true,
      turn: 36,
      turn_gen: GEN,
      hasMore: false,
      nextBefore: 0,
      total: 2,
      queue: [],
      stopping: false,
      messages: [
        { role: 'user', content: 'first request', cls: 'msg msg-u', meta: { mid: 'u-36' } },
        { role: 'streaming', content: 'first answer', cls: 'msg msg-a', seq: 1, gen: GEN, meta: { mid: 'a-36' } },
      ],
    } as never, 'ended-turn-refresh', SLOT))

    expect(chat(store).messages).toBe(before)
    expect(chat(store)).toMatchObject({ slotState: 'idle', slotRunning: false })
    expect(busy(store)).toBe(false)

    store.dispatch(done(36, GEN))
    expect(busy(store)).toBe(false)

    store.dispatch(appendSlotMessage({
      slot: SLOT,
      message: { role: 'user', content: 'next request', cls: 'msg msg-u', meta: { sendId: 'send-37' } },
    }))
    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next answer', seq: 2, gen: GEN }))

    expect(chat(store).messages).toEqual([
      expect.objectContaining({ role: 'user', content: 'next request' }),
      expect.objectContaining({ role: 'streaming', content: 'next answer' }),
    ])
  })

  it('finalizes an ended-turn running warm before the successor streams', () => {
    const store = makeStore()
    liveFrame(store, true, 37)
    liveFrame(store, false, 37)
    switchTo(store, OTHER_SLOT, 'show-other-slot')

    store.dispatch(warmSlotCache.fulfilled({
      key: SLOT,
      running: true,
      turn: 37,
      turn_gen: GEN,
      hasMore: false,
      nextBefore: 0,
      total: 2,
      queue: [],
      stopping: false,
      messages: [
        { role: 'user', content: 'background request', cls: 'msg msg-u', meta: { mid: 'u-37' } },
        { role: 'streaming', content: 'background answer', cls: 'msg msg-a', seq: 1, gen: GEN, meta: { mid: 'a-37' } },
      ],
      warmSeq: 1,
      runTickAtDispatch: chat(store).slotRun[SLOT]?.tick ?? 0,
    } as never, 'ended-turn-warm', SLOT))

    expect(chat(store).slotRun[SLOT]?.state).toBe('idle')
    expect(busy(store)).toBe(false)

    store.dispatch(done(37, GEN, SLOT))
    expect(busy(store)).toBe(false)

    store.dispatch(appendSlotMessage({
      slot: SLOT,
      message: { role: 'user', content: 'next background request', cls: 'msg msg-u', meta: { sendId: 'send-38' } },
    }))
    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next background answer', seq: 2, gen: GEN }))

    expect(chat(store).slotMessages[SLOT]).toEqual([
      expect.objectContaining({ role: 'user', content: 'background request' }),
      expect.objectContaining({ role: 'assistant', content: 'background answer' }),
      expect.objectContaining({ role: 'user', content: 'next background request' }),
      expect.objectContaining({ role: 'streaming', content: 'next background answer' }),
    ])
  })

  it('stale ended-turn page does not idle a live successor', () => {
    const store = makeStore()
    liveFrame(store, true, 60)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'first request', meta: { mid: 'u-60' } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'first answer', seq: 1, gen: GEN }))
    liveFrame(store, false, 60)

    store.dispatch(appendSlotMessage({
      slot: SLOT,
      message: { role: 'user', content: 'next request', cls: 'msg msg-u', meta: { sendId: 'send-61' } },
    }))
    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false, turn: 61, turn_gen: GEN }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next ', seq: 2, gen: GEN }))
    const before = chat(store).messages

    store.dispatch(refreshSlot.fulfilled({
      key: SLOT,
      running: true,
      turn: 60,
      turn_gen: GEN,
      hasMore: false,
      nextBefore: 0,
      total: 2,
      queue: [],
      stopping: false,
      messages: [
        { role: 'user', content: 'first request', cls: 'msg msg-u', meta: { mid: 'u-60' } },
        { role: 'streaming', content: 'first ', cls: 'msg msg-a', seq: 1, gen: GEN },
      ],
    } as never, 'stale-over-live-successor', SLOT))

    expect(chat(store)).toMatchObject({
      slotRunning: true,
      slotState: 'streaming',
      lastChunkSeq: 2,
    })
    expect(busy(store)).toBe(true)
    expect(chat(store).slotServerTotal?.[SLOT]).toBeUndefined()
    expect(chat(store).messages).toBe(before)
    expect(chat(store).messages).toEqual([
      expect.objectContaining({ role: 'user', content: 'first request' }),
      expect.objectContaining({ role: 'assistant', content: 'first answer' }),
      expect.objectContaining({ role: 'user', content: 'next request' }),
      expect.objectContaining({ role: 'streaming', content: 'next ' }),
    ])

    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'replayed', seq: 2, gen: GEN }))
    expect(chat(store).messages.at(-1)).toMatchObject({ role: 'streaming', content: 'next ' })
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'answer', seq: 3, gen: GEN }))
    expect(chat(store).messages.at(-1)).toMatchObject({ role: 'streaming', content: 'next answer' })
  })

  it("stale ended-turn page before the successor's first chunk keeps the composer locked", () => {
    const store = makeStore()
    liveFrame(store, true, 62)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'first request', meta: { mid: 'u-62' } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'first answer', seq: 1, gen: GEN }))
    liveFrame(store, false, 62)

    store.dispatch(appendSlotMessage({
      slot: SLOT,
      message: { role: 'user', content: 'next request', cls: 'msg msg-u', meta: { sendId: 'send-63' } },
    }))
    store.dispatch(startLocalTurn(SLOT))
    expect(chat(store).pendingTurnSlot).toBe(SLOT)
    const before = chat(store).messages

    store.dispatch(refreshSlot.fulfilled({
      key: SLOT,
      running: true,
      turn: 62,
      turn_gen: GEN,
      hasMore: false,
      nextBefore: 0,
      total: 2,
      queue: [],
      stopping: true,
      messages: [
        { role: 'user', content: 'first request', cls: 'msg msg-u', meta: { mid: 'u-62' } },
        { role: 'streaming', content: 'first ', cls: 'msg msg-a', seq: 1, gen: GEN },
      ],
    } as never, 'stale-before-first-successor-chunk', SLOT))

    expect(chat(store).messages).toBe(before)
    expect(chat(store).pendingTurnSlot).toBe(SLOT)
    expect(chat(store).slotRunning).toBe(true)
    expect(chat(store).slotStopping).toBe(false)
    expect(busy(store)).toBe(true)
    expect(chat(store).slotServerTotal?.[SLOT]).toBeUndefined()

    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next answer', seq: 2, gen: GEN }))
    expect(chat(store).messages.slice(-2)).toEqual([
      expect.objectContaining({ role: 'user', content: 'next request' }),
      expect.objectContaining({ role: 'streaming', content: 'next answer' }),
    ])
  })

  it('finalizes stale switch history after a non-text turn ending', () => {
    const store = makeStore()
    liveFrame(store, true, 64)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'first request', meta: { mid: 'u-64' } }))
    switchTo(store, OTHER_SLOT, 'leave-streaming-turn')
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial ', seq: 1, gen: GEN }))

    store.dispatch(switchSlot.pending('stale-switch-history', SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'answer', seq: 2, gen: GEN }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'partial answer', meta: { mid: 'a-64' } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'tool', content: 'nothing_to_do', meta: { mid: 't-64' } }))
    liveFrame(store, false, 64)

    store.dispatch(switchSlot.fulfilled({
      key: SLOT,
      running: true,
      turn: 64,
      turn_gen: GEN,
      hasMore: false,
      total: 2,
      queue: [],
      stopping: false,
      messages: [
        { role: 'user', content: 'first request', cls: 'msg msg-u', meta: { mid: 'u-64' } },
        { role: 'streaming', content: 'partial ', cls: 'msg msg-a', seq: 1, gen: GEN },
      ],
    } as never, 'stale-switch-history', SLOT))

    expect(chat(store)).toMatchObject({ slotRunning: false, slotState: 'idle' })
    expect(busy(store)).toBe(false)
    expect(chat(store).messages.some(message => message.role === 'streaming')).toBe(false)
    store.dispatch(done(64, GEN))

    store.dispatch(appendSlotMessage({
      slot: SLOT,
      message: { role: 'user', content: 'next request', cls: 'msg msg-u', meta: { sendId: 'send-65' } },
    }))
    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next answer', seq: 3, gen: GEN }))
    expect(chat(store).messages.slice(-2)).toEqual([
      expect.objectContaining({ role: 'user', content: 'next request' }),
      expect.objectContaining({ role: 'streaming', content: 'next answer' }),
    ])
  })

  it('finalizes every streaming row in stale warm and switch pages and applies no stale refresh', () => {
    const splitPage = (turn: number) => [
      { role: 'user', content: 'split request', cls: 'msg msg-u', meta: { mid: `u-${turn}` } },
      { role: 'streaming', content: 'before stop ', cls: 'msg msg-a', seq: 1, gen: GEN },
      { role: 'system', content: 'stopped', cls: '', meta: { kind: 'stop_event', mid: `stop-${turn}` } },
      { role: 'streaming', content: 'after stop', cls: 'msg msg-a', seq: 2, gen: GEN },
    ]
    const expectSeparateSuccessor = (messages: Chat['messages']) => {
      expect(messages.filter(message => message.role === 'assistant').map(message => message.content)).toEqual([
        'before stop ',
        'after stop',
      ])
      expect(messages.slice(-2)).toEqual([
        expect.objectContaining({ role: 'user', content: 'next request' }),
        expect.objectContaining({ role: 'streaming', content: 'next answer' }),
      ])
    }

    const active = makeStore()
    liveFrame(active, true, 70)
    liveFrame(active, false, 70)
    const activeBefore = chat(active).messages
    active.dispatch(refreshSlot.fulfilled({
      key: SLOT, running: true, turn: 70, turn_gen: GEN, hasMore: false,
      nextBefore: 0, total: 4, queue: [], stopping: true, messages: splitPage(70),
    } as never, 'split-refresh', SLOT))
    expect(chat(active).messages).toBe(activeBefore)
    active.dispatch(appendSlotMessage({
      slot: SLOT,
      message: { role: 'user', content: 'next request', cls: '', meta: { sendId: 'send-71' } },
    }))
    active.dispatch(startLocalTurn(SLOT))
    active.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next answer', seq: 3, gen: GEN }))
    expect(chat(active).messages).toEqual([
      expect.objectContaining({ role: 'user', content: 'next request' }),
      expect.objectContaining({ role: 'streaming', content: 'next answer' }),
    ])

    const background = makeStore()
    liveFrame(background, true, 72)
    liveFrame(background, false, 72)
    switchTo(background, OTHER_SLOT, 'show-other-for-split-warm')
    background.dispatch(warmSlotCache.fulfilled({
      key: SLOT, running: true, turn: 72, turn_gen: GEN, hasMore: false,
      nextBefore: 0, total: 4, queue: [], stopping: true, messages: splitPage(72),
      warmSeq: 1, runTickAtDispatch: chat(background).slotRun[SLOT]?.tick ?? 0,
    } as never, 'split-warm', SLOT))
    background.dispatch(appendSlotMessage({
      slot: SLOT,
      message: { role: 'user', content: 'next request', cls: '', meta: { sendId: 'send-73' } },
    }))
    background.dispatch(startLocalTurn(SLOT))
    background.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next answer', seq: 3, gen: GEN }))
    expectSeparateSuccessor(chat(background).slotMessages[SLOT])

    const switched = makeStore()
    liveFrame(switched, true, 74)
    liveFrame(switched, false, 74)
    switchTo(switched, OTHER_SLOT, 'show-other-for-split-switch')
    switchTo(switched, SLOT, 'split-switch', splitPage(74) as Chat['messages'], true, 74)
    switched.dispatch(appendSlotMessage({
      slot: SLOT,
      message: { role: 'user', content: 'next request', cls: '', meta: { sendId: 'send-75' } },
    }))
    switched.dispatch(startLocalTurn(SLOT))
    switched.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next answer', seq: 3, gen: GEN }))
    expectSeparateSuccessor(chat(switched).messages)
  })

  it('keeps a longer identity-less cached reply when stale warm history is partial', () => {
    const store = makeStore()
    liveFrame(store, true, 80)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'background request', meta: { mid: 'u-80' } }))
    switchTo(store, OTHER_SLOT, 'show-other-for-partial-warm')
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'background ', seq: 1, gen: GEN }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'answer', seq: 2, gen: GEN }))
    liveFrame(store, false, 80)
    store.dispatch(done(80, GEN, SLOT))

    expect(chat(store).slotMessages[SLOT].at(-1)).toMatchObject({
      role: 'assistant', content: 'background answer',
    })
    store.dispatch(warmSlotCache.fulfilled({
      key: SLOT,
      running: true,
      turn: 80,
      turn_gen: GEN,
      hasMore: false,
      nextBefore: 0,
      total: 2,
      queue: [],
      stopping: false,
      messages: [
        { role: 'user', content: 'background request', cls: '', meta: { mid: 'u-80' } },
        { role: 'streaming', content: 'background ', cls: 'msg msg-a', seq: 1, gen: GEN },
      ],
      warmSeq: 1,
      runTickAtDispatch: chat(store).slotRun[SLOT]?.tick ?? 0,
    } as never, 'partial-stale-warm', SLOT))

    expect(chat(store).slotMessages[SLOT]).toContainEqual(
      expect.objectContaining({ role: 'assistant', content: 'background answer' }),
    )
    expect(chat(store).slotServerTotal?.[SLOT]).toBeUndefined()
  })

  it('records an identified idle history reply', () => {
    const store = makeStore()
    switchTo(store, SLOT, 'idle-history', [], false, 31)
    expect(chat(store).endedTurn[SLOT]).toEqual({ gen: GEN, turn: 31 })
  })

  it('does not restore busy from a stale session-list row for an ended turn', () => {
    const store = makeStore()
    liveFrame(store, true, 32)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'answer', seq: 1 }))
    store.dispatch(done(32, GEN))

    // ChatPage feeds `dashboard.slots`, which a late HTTP fetchSlots also writes.
    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false, turn: 32, turn_gen: GEN }))

    expect(chat(store).slotRunning).toBe(false)
    expect(busy(store)).toBe(false)
  })

  it('lets a session-list row for a newer turn mark the slot running', () => {
    const store = makeStore()
    liveFrame(store, false, 33)

    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false, turn: 34, turn_gen: GEN }))

    expect(chat(store).slotRunning).toBe(true)
  })

  it('a mid-turn _done without identity does not fence the live turn', () => {
    // The deferred /compact acknowledgement: the server omits the identity
    // because the same turn keeps streaming after it.
    const store = makeStore()
    liveFrame(store, true, 35)
    store.dispatch(done())
    expect(chat(store).endedTurn[SLOT]).toBeUndefined()

    store.dispatch(refreshSlot.fulfilled({
      key: SLOT,
      running: true,
      turn: 35,
      turn_gen: GEN,
      hasMore: false,
      nextBefore: 0,
      total: 0,
      queue: [],
      stopping: false,
      messages: [],
    } as never, 'mid-turn-refresh', SLOT))

    expect(chat(store).slotRunning).toBe(true)
  })
})

/** The same race through the real `refreshSlot` thunk: the fetch is held while
 * live frames end turn 10 (and, in most cases, open turn 11), then the page
 * captured mid-turn resolves. However the tab knows its own rows and whatever
 * opened the successor, that page leaves the pane exactly as the tab had it.
 * Re-hydrating is the job of the refresh the recorded end dispatches. */
describe('a stale ended-turn refresh through the thunk', () => {
  const TS = (s: number) => `2026-10-09T10:00:${String(s).padStart(2, '0')}Z`
  const rows = (s: Chat) => s.messages.map(m => [m.role, m.content])
  /** Hold the next slot-detail fetch; returns its resolver. */
  const holdDetail = () => {
    let resolve!: (body: unknown) => void
    const body = new Promise(r => { resolve = r })
    vi.mocked(api.chatSlotDetail).mockImplementationOnce(() => body as never)
    return resolve
  }
  /** A slot-detail body captured while turn 10 was running. */
  const detail = (messages: unknown[], extra: Record<string, unknown> = {}) => ({
    messages, running: true, turn: 10, turn_gen: GEN, stopping: false,
    has_more: false, total: messages.length, next_before: 0, queue: [], ...extra,
  })
  const turn9Rows = [
    { role: 'user', content: 'old request', cls: '', ts: TS(1), meta: { mid: 'u-9' } },
    { role: 'assistant', content: 'old answer', cls: 'msg msg-a', ts: TS(2), meta: { mid: 'a-9' } },
  ]
  const turn10Request = { role: 'user', content: 'first request', cls: '', ts: TS(3), meta: { mid: 'u-10', sendId: 's-10' } }
  const turn10Partial = { role: 'streaming', content: 'partial ', cls: 'msg msg-a', seq: 2, gen: GEN }

  /** Turn 9 already ran; its rows sit in the view and in every page. A turn
   * that handed the floor to a queued message ends with no idle row or `_done`. */
  function seedTurn9(store: Store, ended = true) {
    liveFrame(store, true, 9)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'old request', ts: TS(1), meta: { mid: 'u-9' } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old answer', seq: 1, gen: GEN }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'old answer', ts: TS(2), meta: { mid: 'a-9' } }))
    if (!ended) return
    liveFrame(store, false, 9)
    store.dispatch(done(9, GEN))
  }
  /** The user sends turn 10; its echo carries the server's `mid`. */
  function startTurn10(store: Store) {
    store.dispatch(appendMessage({ role: 'user', content: 'first request', cls: '', ts: TS(3), meta: { sendId: 's-10' } }))
    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false, turn: 10, turn_gen: GEN }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'first request', ts: TS(3), meta: { mid: 'u-10', sendId: 's-10' } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial ', seq: 2, gen: GEN }))
  }
  /** Turn 10 ends in the server's order: final text, idle row, `_done`. */
  function endTurn10(store: Store) {
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'answer', seq: 3, gen: GEN }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'partial answer', ts: TS(5), meta: { mid: 'a-10' } }))
    liveFrame(store, false, 10)
    store.dispatch(done(10, GEN))
  }
  /** The user sends turn 11 and its first chunk streams. */
  function sendTurn11(store: Store) {
    store.dispatch(appendSlotMessage({ slot: SLOT, message: { role: 'user', content: 'next request', cls: '', ts: TS(6), meta: { sendId: 's-11' } } }))
    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false, turn: 11, turn_gen: GEN }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next ', seq: 4, gen: GEN }))
  }
  /** Resolve the held page and require that it changed nothing the tab had. */
  async function landStale(store: Store, resolve: (body: unknown) => void, refresh: Promise<unknown>, body: unknown) {
    const before = chat(store).messages
    const flags = turnFlags(chat(store))
    resolve(body)
    const settled = await refresh
    // The page reached the reducer: the thunk fulfilled with it.
    expect(refreshSlot.fulfilled.match(settled) && settled.payload !== null).toBe(true)
    expect(chat(store).messages).toBe(before)
    expect(turnFlags(chat(store))).toEqual(flags)
  }
  /** The successor keeps streaming on its own row. */
  function expectSuccessorContinues(store: Store) {
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'answer', seq: 5, gen: GEN }))
    expect(chat(store).messages.at(-1)).toMatchObject({ role: 'streaming', content: 'next answer' })
    expect(busy(store)).toBe(true)
  }

  it('loses nothing when the first local row carries only its sendId', async () => {
    const store = makeStore()
    // The receipt and the echo that stamp `mid: u-10` on this row were lost.
    store.dispatch(appendMessage({ role: 'user', content: 'first request', cls: '', ts: TS(3), meta: { sendId: 's-10' } }))
    store.dispatch(startLocalTurn(SLOT))
    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false, turn: 10, turn_gen: GEN }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial ', seq: 2, gen: GEN }))
    const resolve = holdDetail()
    const refresh = store.dispatch(refreshSlot(SLOT))
    endTurn10(store)
    sendTurn11(store)

    await landStale(store, resolve, refresh, detail([turn10Request, turn10Partial]))
    expect(rows(chat(store))).toEqual([
      ['user', 'first request'], ['assistant', 'partial answer'],
      ['user', 'next request'], ['streaming', 'next '],
    ])
    expectSuccessorContinues(store)
  })

  it.each(['nudge', 'subagent'])('loses nothing when a %s row opened the successor', async role => {
    const store = makeStore()
    seedTurn9(store)
    startTurn10(store)
    const resolve = holdDetail()
    const refresh = store.dispatch(refreshSlot(SLOT))
    endTurn10(store)
    // A monitor cycle or a drained sub-agent result opens turn 11 server-side.
    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false, turn: 11, turn_gen: GEN }))
    store.dispatch(sseChatMessage({ slot: SLOT, role, content: 'turn 11 opener', ts: TS(6), meta: { mid: 'o-11' } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next ', seq: 4, gen: GEN }))

    await landStale(store, resolve, refresh, detail([...turn9Rows, turn10Request, turn10Partial]))
    expect(rows(chat(store)).slice(-4)).toEqual([
      ['user', 'first request'], ['assistant', 'partial answer'],
      [role, 'turn 11 opener'], ['streaming', 'next '],
    ])
    expectSuccessorContinues(store)
  })

  describe('for a turn the queue dispatched (its request is a local row with no identity)', () => {
    /** The follow-up queued behind turn 9 and the drain rebuilt it as a
     * `user` row: no echo follows a user row, and turn 9 hands off with no
     * idle row or `_done`. The server's copy of that row carries a `mid`. */
    function startDrainedTurn10(store: Store) {
      seedTurn9(store, false)
      store.dispatch(appendQueuedMessage({ slot: SLOT, content: 'follow up', ts: TS(3), queue_id: 'q-1' }))
      store.dispatch(removeQueuedMessage({ slot: SLOT, content: 'follow up', queue_id: 'q-1' }))
      store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false, turn: 10, turn_gen: GEN }))
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial ', seq: 2, gen: GEN }))
    }
    const drainedPage = () => detail([
      ...turn9Rows,
      { role: 'user', content: 'follow up', cls: 'msg msg-u', ts: TS(4), meta: { mid: 'u-10' } },
      turn10Partial,
    ])
    const turnsSeen = [
      ['user', 'old request'], ['assistant', 'old answer'],
      ['user', 'follow up'], ['assistant', 'partial answer'],
    ]

    it('renders its request and reply once', async () => {
      const store = makeStore()
      startDrainedTurn10(store)
      const resolve = holdDetail()
      const refresh = store.dispatch(refreshSlot(SLOT))
      endTurn10(store)

      await landStale(store, resolve, refresh, drainedPage())
      expect(rows(chat(store))).toEqual(turnsSeen)
    })

    it('renders its request and reply once under a live successor', async () => {
      const store = makeStore()
      startDrainedTurn10(store)
      const resolve = holdDetail()
      const refresh = store.dispatch(refreshSlot(SLOT))
      endTurn10(store)
      sendTurn11(store)

      await landStale(store, resolve, refresh, drainedPage())
      expect(rows(chat(store))).toEqual([...turnsSeen, ['user', 'next request'], ['streaming', 'next ']])
      expectSuccessorContinues(store)
    })

    it("does not re-queue a drained successor from the stale page's queue", async () => {
      const store = makeStore()
      seedTurn9(store)
      startTurn10(store)
      store.dispatch(appendQueuedMessage({ slot: SLOT, content: 'queued follow up', ts: TS(4), queue_id: 'q-2' }))
      const resolve = holdDetail()
      const refresh = store.dispatch(refreshSlot(SLOT))
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'answer', seq: 3, gen: GEN }))
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'partial answer', ts: TS(5), meta: { mid: 'a-10' } }))
      // Turn 10 hands the floor to the queued message: no idle row or `_done`.
      store.dispatch(removeQueuedMessage({ slot: SLOT, content: 'queued follow up', queue_id: 'q-2' }))
      store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false, turn: 11, turn_gen: GEN }))
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'second answer', seq: 4, gen: GEN }))
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'second answer', ts: TS(7), meta: { mid: 'a-11' } }))
      liveFrame(store, false, 11)
      store.dispatch(done(11, GEN))

      await landStale(store, resolve, refresh, detail(
        [...turn9Rows, turn10Request, turn10Partial],
        { queue: [{ content: 'queued follow up', id: 'q-2' }] },
      ))
      expect(rows(chat(store))).toEqual([
        ['user', 'old request'], ['assistant', 'old answer'],
        ['user', 'first request'], ['assistant', 'partial answer'],
        ['user', 'queued follow up'], ['assistant', 'second answer'],
      ])
    })
  })

  it('the completion refresh that follows lands the text the tab never received', async () => {
    const store = makeStore()
    seedTurn9(store)
    startTurn10(store)
    const resolveStale = holdDetail()
    const stale = store.dispatch(refreshSlot(SLOT))
    // The rest of turn 10's text never reached this tab. Its idle row did,
    // with no `_done`, and the socket hook dispatches the completion refresh.
    liveFrame(store, false, 10)
    const resolveCompletion = holdDetail()
    const completion = store.dispatch(refreshSlot(SLOT))

    await landStale(store, resolveStale, stale, detail([...turn9Rows, turn10Request, turn10Partial]))
    expect(rows(chat(store)).at(-1)).toEqual(['assistant', 'partial '])

    resolveCompletion(detail([
      ...turn9Rows,
      turn10Request,
      { role: 'assistant', content: 'partial answer, in full', cls: 'msg msg-a', ts: TS(5), meta: { mid: 'a-10' } },
    ], { running: false }))
    expect(refreshSlot.fulfilled.match(await completion)).toBe(true)
    expect(rows(chat(store))).toEqual([
      ['user', 'old request'], ['assistant', 'old answer'],
      ['user', 'first request'], ['assistant', 'partial answer, in full'],
    ])
    expect(busy(store)).toBe(false)
  })
})

describe('settlement then done equals done alone', () => {
  const finishBothWays = (base: Chat, turn: number) => ({
    viaSettle: chatReducer(chatReducer(base, sseSlots([row(false, turn)])), done(turn, GEN)),
    viaDone: chatReducer(base, done(turn, GEN)),
  })

  it.each([
    ['real text', 'streamed text'],
    ['a placeholder streaming row', '…'],
  ])('for a transcript ending in %s', (_label, content) => {
    const store = makeStore()
    liveFrame(store, true, 40)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content, seq: 1 }))
    const { viaSettle, viaDone } = finishBothWays(chat(store), 40)

    expect(viaDone.messages.at(-1)).toMatchObject({ role: 'assistant', content, rawText: content })
    expect(viaSettle.messages).toEqual(viaDone.messages)
    expect(turnFlags(viaSettle)).toEqual(turnFlags(viaDone))
    expect(viaSettle.endedTurn).toEqual(viaDone.endedTurn)
  })
})
