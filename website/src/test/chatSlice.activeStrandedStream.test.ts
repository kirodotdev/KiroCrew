/**
 * Turn-end recovery is fenced by the server's `{turn, turn_gen}` identity.
 *
 * The ordinary server order is idle `slots` row first, then `chat_done`. The
 * row finalizes the turn and records its identity; the matching `_done` is a
 * duplicate. A late duplicate therefore cannot finalize a successor turn,
 * even after a slot switch. Identity-less payloads retain older-gateway
 * behavior.
 */
import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  setActiveSlot,
  sseChatMessage,
  startLocalTurn,
  switchSlot,
  refreshSlot,
  syncSlotRunningFromServer,
  selectComposerBusy,
} from '../store/chatSlice'
import dashboardReducer, { sseSlots, fetchSlots } from '../store/dashboardSlice'
import './mockApiClient'

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
    expect(chat(store).lastChunkSeq).toBeUndefined()
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

    expect(chat(store).slotState).toBe('idle')
    expect(chat(store).slotRunning).toBe(false)
    expect(busy(store)).toBe(false)
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
