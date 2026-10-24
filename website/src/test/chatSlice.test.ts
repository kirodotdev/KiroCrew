/**
 * chatSlice: the core reducers, switching slots, and how a chat message frame
 * and a mid-turn steer land in the transcript, pipelined sends included. Send
 * receipts, segments, patches, context usage, side results, model reasoning and
 * batched-chunk coalescing are in chatSlice.messages.test.ts; subagent,
 * tool-activity and approval cases in chatSlice.activity.test.ts; forking, the
 * session history stack, cache warming, creating a slot, and a slot's queue and
 * composer state in chatSlice.slots.test.ts.
 */
import { describe, it, expect } from 'vitest'
import type { ChatMessage } from '../types'
import reducer, {
  setActiveSlot,
  setPendingInput,
  stageToMainComposer,
  appendMessage,
  appendSlotMessage,
  updateStreamingMessage,
  finalizeAssistant,
  removeThinking,
  setSlotRunning,
  setSlotStopping,
  settleStopNotRunning,
  startLocalTurn,
  endLocalTurn,
  syncSlotRunningFromServer,
  setSlotState,
  setSlotStatusDetail,
  clearMessages,
  sseChatMessage,
} from '../store/chatSlice'
import './mockApiClient'

describe('chatSlice reducers', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('has correct initial state', () => {
    expect(initial.activeSlot).toBeNull()
    expect(initial.messages).toEqual([])
    expect(initial.slotRunning).toBe(false)
    expect(initial.slotState).toBe('idle')
    expect(initial.pendingInput).toBeNull()
  })

  it('setActiveSlot', () => {
    expect(reducer(initial, setActiveSlot('chat-1')).activeSlot).toBe('chat-1')
  })

  it('setPendingInput', () => {
    expect(reducer(initial, setPendingInput('hello')).pendingInput).toBe('hello')
  })

  it('stageToMainComposer', () => {
    const staged = reducer(initial, stageToMainComposer({ slot: 's1', text: 'do X in main' }))
    expect(staged.mainComposerAppend).toEqual({ slot: 's1', text: 'do X in main' })
    expect(reducer(staged, stageToMainComposer(null)).mainComposerAppend).toBeNull()
  })

  it('appendMessage', () => {
    const state = reducer(initial, appendMessage({ role: 'user', content: 'hi', cls: '' }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('hi')
  })

  it('updateStreamingMessage creates streaming msg if none exists', () => {
    const state = reducer(initial, updateStreamingMessage('chunk1'))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].role).toBe('streaming')
    expect(state.messages[0].content).toBe('chunk1')
  })

  it('updateStreamingMessage appends to existing streaming msg', () => {
    let state = reducer(initial, updateStreamingMessage('chunk1'))
    state = reducer(state, updateStreamingMessage('chunk1chunk2'))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('chunk1chunk2')
  })

  it('finalizeAssistant converts streaming to assistant', () => {
    let state = reducer(initial, updateStreamingMessage('partial'))
    state = reducer(state, finalizeAssistant('final content'))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[0].content).toBe('final content')
  })

  it('finalizeAssistant with object payload', () => {
    let state = reducer(initial, updateStreamingMessage('partial'))
    state = reducer(state, finalizeAssistant({ content: 'done', ts: '2025-01-01' }))
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[0].ts).toBe('2025-01-01')
  })

  it('removeThinking filters thinking messages', () => {
    let state = reducer(initial, appendMessage({ role: 'thinking', content: '', cls: '' }))
    state = reducer(state, appendMessage({ role: 'user', content: 'hi', cls: '' }))
    state = reducer(state, removeThinking())
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].role).toBe('user')
  })

  it('setSlotRunning / setSlotStopping / setSlotState', () => {
    let state = reducer(initial, setSlotRunning(true))
    expect(state.slotRunning).toBe(true)
    state = reducer(state, setSlotStopping(true))
    expect(state.slotStopping).toBe(true)
    state = reducer(state, setSlotState('tool_running'))
    expect(state.slotState).toBe('tool_running')
  })

  describe('local turn / server running reconciliation (session resurrection)', () => {
    const active = (slot: string) => reducer(initial, setActiveSlot(slot))

    it('startLocalTurn sets running and pendingTurnSlot for the active slot', () => {
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-1'))
      expect(state.slotRunning).toBe(true)
      expect(state.pendingTurnSlot).toBe('chat-1')
    })

    it('startLocalTurn for a non-active (targeted) slot does not spin the active footer', () => {
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-2'))
      expect(state.slotRunning).toBe(false)
      expect(state.pendingTurnSlot).toBe('chat-2')
    })

    it('endLocalTurn is the slot-keyed inverse: a failed send for the active slot clears its footer and pending mark', () => {
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-1'))
      state = reducer(state, endLocalTurn('chat-1'))
      expect(state.slotRunning).toBe(false)
      expect(state.pendingTurnSlot).toBeNull()
    })

    it('endLocalTurn for a slot the user has LEFT does not clear the new active session\'s running state', () => {
      // Send from chat-1, switch to chat-2 (running), then chat-1's send fails.
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-1'))
      state = reducer(state, setActiveSlot('chat-2'))
      state = reducer(state, setSlotRunning(true))
      state = reducer(state, endLocalTurn('chat-1'))
      expect(state.slotRunning).toBe(true)
      // chat-1's pending mark is gone; nothing else changed.
      expect(state.pendingTurnSlot).toBeNull()
    })

    it('endLocalTurn leaves another slot\'s pending mark alone', () => {
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-2'))
      state = reducer(state, endLocalTurn('chat-1'))
      expect(state.pendingTurnSlot).toBe('chat-2')
    })

    it('a stale slots snapshot (running=false) does NOT clobber an optimistic local turn', () => {
      // Regression: after send() the server may broadcast a slots list that
      // predates the send (running=false). It must not hide the thinking footer.
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-1'))
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-1', running: false, stopping: false }))
      expect(state.slotRunning).toBe(true)
      expect(state.pendingTurnSlot).toBe('chat-1')
    })

    it('a stale snapshot cannot leak stopping=true onto a pending turn', () => {
      // While the guard blocks running=false it must also ignore stopping, else
      // a leftover stopping=true from a prior turn falsely shows "stopping".
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-1'))
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-1', running: false, stopping: true }))
      expect(state.slotRunning).toBe(true)
      expect(state.slotStopping).toBe(false)
      expect(state.pendingTurnSlot).toBe('chat-1')
    })

    it('server confirming running=true clears the pending guard', () => {
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-1'))
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-1', running: true, stopping: false }))
      expect(state.slotRunning).toBe(true)
      expect(state.pendingTurnSlot).toBeNull()
      // Once confirmed, a later running=false (genuine turn end) is honoured.
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-1', running: false, stopping: false }))
      expect(state.slotRunning).toBe(false)
    })

    it('_done authoritatively ends the turn and clears the pending guard', () => {
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-1'))
      state = reducer(state, sseChatMessage({ slot: 'chat-1', role: '_done', content: '' }))
      expect(state.slotRunning).toBe(false)
      expect(state.pendingTurnSlot).toBeNull()
      // A trailing stale slots snapshot after _done is now honoured (no clobber risk).
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-1', running: false, stopping: false }))
      expect(state.slotRunning).toBe(false)
    })

    it('setSlotRunning(false) clears the pending guard (send failure path)', () => {
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-1'))
      state = reducer(state, setSlotRunning(false))
      expect(state.slotRunning).toBe(false)
      expect(state.pendingTurnSlot).toBeNull()
    })

    it('switching active slot clears a stale pending guard', () => {
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-1'))
      state = reducer(state, setActiveSlot('chat-2'))
      expect(state.pendingTurnSlot).toBeNull()
    })

    it('syncSlotRunningFromServer leaves the active mirror alone for a non-active slot', () => {
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-1'))
      const before = state.slotRunning
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-2', running: false, stopping: true }))
      expect(state.slotRunning).toBe(before)
      expect(state.slotStopping).toBe(false)
    })

    /* A background slot (a member DM thread, a split pane) reads its run state
     * from `slotRun`, which only live frames wrote. A `_done` that never
     * reached the tab left it busy for good and its Stop button dead (#9547):
     * the server's snapshot now supplies the idle direction — and ONLY that
     * direction, since a snapshot racing the ordered live frames must never
     * promote a pane to busy. */
    it('syncSlotRunningFromServer idles a background slot the server reports not running', () => {
      let state = active('chat-1')
      state = reducer(state, sseChatMessage({ slot: 'chat-2', role: 'chunk', content: 'x', seq: 1 }))
      expect(state.slotRun['chat-2']?.state).toBe('streaming')
      expect(state.slotMessages['chat-2']?.at(-1)?.role).toBe('streaming')
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-2', running: false, stopping: false }))
      expect(state.slotRun['chat-2']?.state).toBe('idle')
      expect(state.slotRun['chat-2']?.lastChunkSeq).toBeUndefined()
      // The settlement stands in for the lost `_done`, so the trailing reply
      // is finalized as that frame would have finalized it.
      expect(state.slotMessages['chat-2']?.at(-1)?.role).toBe('assistant')
      expect(state.slotMessages['chat-2']?.at(-1)?.content).toBe('x')
    })

    it('syncSlotRunningFromServer idles a background slot even when the cancel flag is still set', () => {
      let state = active('chat-1')
      state = reducer(state, sseChatMessage({ slot: 'chat-2', role: 'tool', content: '🔧 ls', ts: '2026-01-01T00:00:00Z' }))
      expect(state.slotRun['chat-2']?.state).toBe('tool_running')
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-2', running: false, stopping: true }))
      expect(state.slotRun['chat-2']?.state).toBe('idle')
    })

    it('syncSlotRunningFromServer never promotes a background slot to running', () => {
      let state = active('chat-1')
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-2', running: true, stopping: false }))
      expect(state.slotRun['chat-2']).toBeUndefined()
      state = reducer(state, sseChatMessage({ slot: 'chat-2', role: 'chunk', content: 'x', seq: 1 }))
      state = reducer(state, sseChatMessage({ slot: 'chat-2', role: '_done', content: '' }))
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-2', running: true, stopping: false }))
      expect(state.slotRun['chat-2']?.state).toBe('idle')
    })

    it('syncSlotRunningFromServer settles only the turn it was captured against', () => {
      // Turn A streams and ends; the snapshot that reports A idle lags, and
      // turn B has already started by the time it is applied. B's first chunk
      // bumped the epoch, so a settlement carrying A's epoch must not idle B —
      // it would finalize B's streaming row mid-reply and split it.
      let state = active('chat-1')
      state = reducer(state, sseChatMessage({ slot: 'chat-2', role: 'chunk', content: 'a', seq: 1 }))
      const epochA = state.runEpoch['chat-2']
      state = reducer(state, sseChatMessage({ slot: 'chat-2', role: '_done', content: '' }))
      state = reducer(state, sseChatMessage({ slot: 'chat-2', role: 'chunk', content: 'b', seq: 2 }))
      expect(state.runEpoch['chat-2']).toBe(epochA + 1)
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-2', running: false, stopping: false, epoch: epochA }))
      expect(state.slotRun['chat-2']?.state).toBe('streaming')
      expect(state.slotMessages['chat-2']?.at(-1)?.role).toBe('streaming')
      // The same answer captured against B's own epoch settles B.
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-2', running: false, stopping: false, epoch: epochA + 1 }))
      expect(state.slotRun['chat-2']?.state).toBe('idle')
      expect(state.slotMessages['chat-2']?.at(-1)?.role).toBe('assistant')
    })

    it('a passive /note inject row does not count as a turn start, on either frame path', () => {
      // A note landing inside a Stop press's round-trip must not make the
      // settlement captured before it read as stale (the pane would stay
      // falsely busy). A cron / continue inject row DOES start a turn.
      let state = active('chat-1')
      state = reducer(state, sseChatMessage({ slot: 'chat-2', role: 'chunk', content: 'a', seq: 1 }))
      const bg = state.runEpoch['chat-2']
      state = reducer(state, sseChatMessage({ slot: 'chat-2', role: 'inject', content: 'note', cls: 'reconcile-note' }))
      state = reducer(state, sseChatMessage({ slot: 'chat-2', role: 'inject', content: 'note', meta: { noteSession: 's1' } }))
      expect(state.runEpoch['chat-2']).toBe(bg)
      state = reducer(state, sseChatMessage({ slot: 'chat-2', role: 'inject', content: 'cron tick' }))
      expect(state.runEpoch['chat-2']).toBe(bg + 1)
      // Active-slot path.
      const fg = state.runEpoch['chat-1'] ?? 0
      state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'inject', content: 'note', cls: 'reconcile-note' }))
      expect(state.runEpoch['chat-1'] ?? 0).toBe(fg)
      state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'inject', content: 'cron tick' }))
      expect(state.runEpoch['chat-1']).toBe(fg + 1)
    })

    it('settleStopNotRunning idles whichever path holds the slot', () => {
      let state = active('chat-1')
      state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'x', seq: 1 }))
      state = reducer(state, sseChatMessage({ slot: 'chat-2', role: 'chunk', content: 'x', seq: 1 }))
      state = reducer(state, settleStopNotRunning({ slot: 'chat-2' }))
      expect(state.slotRun['chat-2']?.state).toBe('idle')
      expect(state.slotMessages['chat-2']?.at(-1)?.role).toBe('assistant')
      expect(state.slotState).toBe('streaming')
      expect(state.messages.at(-1)?.role).toBe('streaming')
      state = reducer(state, settleStopNotRunning({ slot: 'chat-1' }))
      expect(state.slotState).toBe('idle')
      expect(state.slotRunning).toBe(false)
      expect(state.messages.at(-1)?.role).toBe('assistant')
    })

    it('settleStopNotRunning does not settle the active slot while a local turn is pending', () => {
      // Send → Stop before the backend registered the turn → "not running".
      // Settling here would reopen the composer mid-send and invite a
      // duplicate turn, so the pending mark wins until the first live frame.
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-1'))
      state = reducer(state, settleStopNotRunning({ slot: 'chat-1' }))
      expect(state.pendingTurnSlot).toBe('chat-1')
      expect(state.slotRunning).toBe(true)
      // Once the pending mark is gone the same answer settles it.
      state = reducer(state, endLocalTurn('chat-1'))
      state = reducer(state, setSlotRunning(true))
      state = reducer(state, settleStopNotRunning({ slot: 'chat-1' }))
      expect(state.slotRunning).toBe(false)
      expect(state.slotState).toBe('idle')
    })
  })

  it('setSlotStatusDetail updates kind, and keeps a server status label', () => {
    const state = reducer(initial, setSlotStatusDetail({ slot: 'test-slot', kind: 'thinking' }))
    expect(state.slotStatusDetail['test-slot'].kind).toBe('thinking')
    // A fixed phase stores no copy: the label is resolved from `kind` at render time.
    expect(state.slotStatusDetail['test-slot']).toEqual({ kind: 'thinking' })
    // Tool name optional
    const state2 = reducer(state, setSlotStatusDetail({ slot: 'test-slot', kind: 'tool', purpose: 'Tool: read', toolName: 'read' }))
    expect(state2.slotStatusDetail['test-slot'].toolName).toBe('read')
    // Idle clears
    const state3 = reducer(state2, setSlotStatusDetail({ slot: 'test-slot', kind: 'idle' }))
    expect(state3.slotStatusDetail['test-slot'].kind).toBe('idle')
    // A server-supplied status is the one non-tool phase that carries a label.
    const state4 = reducer(state3, setSlotStatusDetail({ slot: 'test-slot', kind: 'thinking', label: 'Compacting…' }))
    expect(state4.slotStatusDetail['test-slot']).toEqual({ kind: 'thinking', label: 'Compacting…' })
  })

  it('clearMessages resets messages and pagination', () => {
    let state = reducer(initial, appendMessage({ role: 'user', content: 'hi', cls: '' }))
    state = reducer(state, clearMessages())
    expect(state.messages).toEqual([])
    expect(state.slotHasMore).toBe(false)
    expect(state.slotOldestIndex).toBe(0)
  })
})

describe('switchSlot.pending', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('immediately switches activeSlot, caches old messages, sets loading for uncached slot', () => {
    const withStreaming = { ...initial, activeSlot: 'old', slotRunning: true, slotState: 'streaming' as const,
      messages: [{ role: 'streaming' as const, content: 'partial', cls: 'msg msg-a' }] }
    const state = reducer(withStreaming, { type: 'chat/switchSlot/pending', meta: { arg: 'new', requestId: 'r1', requestStatus: 'pending' } })
    expect(state.activeSlot).toBe('new')
    // Old messages cached, new slot has empty messages + loading
    expect(state.slotMessages['old']).toHaveLength(1)
    expect(state.messages).toEqual([])
    expect(state.slotLoading).toBe(true)
  })

  it('gates sseChatMessage from old slot after pending', () => {
    let state = { ...initial, activeSlot: 'old', slotRunning: true, slotState: 'streaming' as const,
      messages: [{ role: 'streaming' as const, content: 'partial', cls: 'msg msg-a' }] }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'new', requestId: 'r1', requestStatus: 'pending' } })
    // Messages cleared on pending (cached in slotMessages), chunks for old slot ignored
    state = reducer(state, sseChatMessage({ slot: 'old', role: 'chunk', content: ' more' }))
    expect(state.messages).toHaveLength(0)
  })

  it('rejected clears stale messages after pending set activeSlot', () => {
    let state = { ...initial, activeSlot: 'old', slotRunning: true,
      messages: [{ role: 'user' as const, content: 'old msg', cls: '' }] }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'new', requestId: 'r1', requestStatus: 'pending' } })
    expect(state.activeSlot).toBe('new')
    state = reducer(state, { type: 'chat/switchSlot/rejected', meta: { arg: 'new', requestId: 'r1', requestStatus: 'rejected' }, error: { message: 'fail' } })
    expect(state.messages).toEqual([])
    expect(state.slotRunning).toBe(false)
  })

  it('rejected skips clear if user already switched to another slot', () => {
    let state = { ...initial, activeSlot: 'old' }
    // First switch pending
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'A', requestId: 'r1', requestStatus: 'pending' } })
    // User switches again before first resolves
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'B', requestId: 'r2', requestStatus: 'pending' } })
    // Second switch fulfilled with messages
    state = { ...state, messages: [{ role: 'user' as const, content: 'B msg', cls: '' }] }
    // First switch rejects — should NOT wipe B's messages
    state = reducer(state, { type: 'chat/switchSlot/rejected', meta: { arg: 'A', requestId: 'r1', requestStatus: 'rejected' }, error: { message: 'fail' } })
    expect(state.activeSlot).toBe('B')
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('B msg')
  })

  it('fulfilled skips overwrite if user already switched to another slot', () => {
    let state = { ...initial, activeSlot: 'old' }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'A', requestId: 'r1', requestStatus: 'pending' } })
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'B', requestId: 'r2', requestStatus: 'pending' } })
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'B', requestId: 'r2', requestStatus: 'fulfilled' },
      payload: { key: 'B', messages: [{ role: 'user', content: 'B msg', cls: '' }], running: false, stopping: false, hasMore: false, total: 1, queue: [] },
    })
    // A fulfills late — should NOT overwrite B's state
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'A', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'A', messages: [{ role: 'user', content: 'A msg', cls: '' }], running: true, stopping: false, hasMore: false, total: 1, queue: [] },
    })
    expect(state.activeSlot).toBe('B')
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('B msg')
    expect(state.slotRunning).toBe(false)
  })

  it('fulfilled replaces empty messages with new slot data and updates cache', () => {
    let state = { ...initial, activeSlot: 'old',
      messages: [{ role: 'user' as const, content: 'old msg', cls: '' }] }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'new', requestId: 'r1', requestStatus: 'pending' } })
    // Messages cleared on pending (no stale flash)
    expect(state.messages).toEqual([])
    expect(state.slotLoading).toBe(true)
    // Fulfilled swaps in new slot's messages and updates cache
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'new', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'new', messages: [{ role: 'user', content: 'new msg', cls: '' }], running: false, stopping: false, hasMore: false, total: 1, queue: [] },
    })
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('new msg')
    expect(state.slotMessages['new']).toHaveLength(1)
    expect(state.slotLoading).toBe(false)
  })

  it('fulfilled preserves WS streaming chunks that arrived during fetch', () => {
    let state = { ...initial, activeSlot: 'old',
      messages: [{ role: 'user' as const, content: 'old msg', cls: '' }] }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'new', requestId: 'r1', requestStatus: 'pending' } })
    // WS chunk arrives for new slot during fetch — appended to stale messages
    state = reducer(state, sseChatMessage({ slot: 'new', role: 'chunk', content: 'streaming text' }))
    expect(state.messages[state.messages.length - 1].role).toBe('streaming')
    // Fulfilled merges: fetched history + local streaming
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'new', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'new', messages: [{ role: 'user', content: 'new msg', cls: '' }], running: true, stopping: false, hasMore: false, total: 1, queue: [] },
    })
    expect(state.messages).toHaveLength(2)
    expect(state.messages[0].content).toBe('new msg')
    expect(state.messages[1].role).toBe('streaming')
    expect(state.messages[1].content).toBe('streaming text')
  })

  it('fulfilled sets slotRunning from server response', () => {
    let state = { ...initial, activeSlot: 'old', slotRunning: true }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'new', requestId: 'r1', requestStatus: 'pending' } })
    // slotRunning not cleared by pending — still true from old slot
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'new', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'new', messages: [], running: false, stopping: false, hasMore: false, total: 0, queue: [] },
    })
    expect(state.slotRunning).toBe(false)
    expect(state.slotState).toBe('idle')
  })

  it('fulfilled discards stale streaming from old slot when no WS chunks arrived for new slot', () => {
    // Old slot is actively streaming
    let state = { ...initial, activeSlot: 'old', slotRunning: true, slotState: 'streaming' as const,
      messages: [{ role: 'streaming' as const, content: 'partial from old', cls: 'msg msg-a' }] }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'new', requestId: 'r1', requestStatus: 'pending' } })
    // Old streaming cached, new slot starts empty
    expect(state.messages).toEqual([])
    // No WS chunks arrive for new slot during fetch
    // Fulfilled should have clean new slot messages
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'new', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'new', messages: [{ role: 'user', content: 'new msg', cls: '' }], running: false, stopping: false, hasMore: false, total: 1, queue: [] },
    })
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('new msg')
    // No stale streaming message from old slot
    expect(state.messages.some(m => m.role === 'streaming')).toBe(false)
  })

  it('fulfilled preserves a locally-finalized latest reply when the server fetch is stale (switch-away-and-back regression)', () => {
    // Slot B finished streaming while backgrounded: its cache holds the
    // finalized assistant reply (via applyNonActiveFrame). User switches back to B.
    const bCache = [
      { role: 'user' as const, content: 'question', cls: '' },
      { role: 'assistant' as const, content: 'the latest reply', cls: 'msg msg-a' },
    ]
    let state = { ...initial, activeSlot: 'A',
      messages: [{ role: 'user' as const, content: 'A msg', cls: '' }],
      slotMessages: { 'B': bCache } }
    // Switch back to B — pending restores the cache instantly.
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'B', requestId: 'r1', requestStatus: 'pending' } })
    expect(state.messages).toHaveLength(2)
    // The HTTP fetch resolves with a STALE history that predates the reply.
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'B', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'B', messages: [{ role: 'user', content: 'question', cls: '' }], running: false, stopping: false, hasMore: false, total: 1, queue: [] },
    })
    // The latest reply must NOT be dropped — server history + re-attached reply.
    expect(state.messages.some(m => m.role === 'assistant' && m.content === 'the latest reply')).toBe(true)
    expect(state.messages[state.messages.length - 1].content).toBe('the latest reply')
    expect(state.slotMessages['B'].some(m => m.content === 'the latest reply')).toBe(true)
  })

  it('fulfilled does not duplicate the reply when the server fetch already includes it', () => {
    const bCache = [
      { role: 'user' as const, content: 'question', cls: '' },
      { role: 'assistant' as const, content: 'the latest reply', cls: 'msg msg-a' },
    ]
    let state = { ...initial, activeSlot: 'A',
      messages: [{ role: 'user' as const, content: 'A msg', cls: '' }],
      slotMessages: { 'B': bCache } }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'B', requestId: 'r1', requestStatus: 'pending' } })
    // Server IS up to date — it already returns the reply.
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'B', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'B', messages: [
        { role: 'user', content: 'question', cls: '' },
        { role: 'assistant', content: 'the latest reply', cls: 'msg msg-a' },
      ], running: false, stopping: false, hasMore: false, total: 2, queue: [] },
    })
    expect(state.messages.filter(m => m.role === 'assistant' && m.content === 'the latest reply')).toHaveLength(1)
    expect(state.messages).toHaveLength(2)
  })
  it('fulfilled keeps a still-streaming partial as streaming when the slot is still running (no frozen split bubble)', () => {
    // Switch back to slot B while B is STILL streaming its reply: its cache
    // holds a role:'streaming' partial (from applyNonActiveFrame), and the HTTP
    // fetch returns running:true with history that predates the partial.
    const bCache = [
      { role: 'user' as const, content: 'question', cls: '' },
      { role: 'streaming' as const, content: 'partial repl', cls: 'msg msg-a' },
    ]
    let state = { ...initial, activeSlot: 'A',
      messages: [{ role: 'user' as const, content: 'A msg', cls: '' }],
      slotMessages: { 'B': bCache } }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'B', requestId: 'r1', requestStatus: 'pending' } })
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'B', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'B', messages: [{ role: 'user', content: 'question', cls: '' }], running: true, stopping: false, hasMore: false, total: 1, queue: [] },
    })
    // The partial must stay 'streaming' (NOT frozen to 'assistant'), so the
    // resuming chunk handler continues into the SAME bubble instead of pushing
    // a second one. Pre-fix it was coerced to 'assistant' regardless of running.
    const tail = state.messages[state.messages.length - 1]
    expect(tail.role).toBe('streaming')
    expect(tail.content).toBe('partial repl')
    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(1)
  })
  it('fulfilled keeps the existing array when the fetched history is identical', () => {
    const bCache: ChatMessage[] = [
      { role: 'user', content: 'question', cls: '' },
      { role: 'assistant', content: 'the latest reply', cls: 'msg msg-a' },
    ]
    let state = { ...initial, activeSlot: 'A',
      messages: [{ role: 'user' as const, content: 'A msg', cls: '' }],
      slotMessages: { 'B': bCache } }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'B', requestId: 'r1', requestStatus: 'pending' } })
    // Positive control: pending restores the cached array BY REFERENCE, so the
    // assertion below can tell a preserved array from a re-created one.
    expect(state.messages).toBe(bCache)
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'B', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'B', messages: [
        { role: 'user', content: 'question', cls: '' },
        { role: 'assistant', content: 'the latest reply', cls: 'msg msg-a' },
      ], running: false, stopping: false, hasMore: false, total: 2, queue: [] },
    })
    expect(state.messages).toBe(bCache)
    expect(state.slotLoading).toBe(false)
  })

  it('fulfilled replaces the array when the fetched history appends a message', () => {
    const bCache: ChatMessage[] = [
      { role: 'user', content: 'question', cls: '' },
      { role: 'assistant', content: 'the latest reply', cls: 'msg msg-a' },
    ]
    let state = { ...initial, activeSlot: 'A',
      messages: [{ role: 'user' as const, content: 'A msg', cls: '' }],
      slotMessages: { 'B': bCache } }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'B', requestId: 'r1', requestStatus: 'pending' } })
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'B', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'B', messages: [
        { role: 'user', content: 'question', cls: '' },
        { role: 'assistant', content: 'the latest reply', cls: 'msg msg-a' },
        { role: 'user', content: 'follow-up', cls: '' },
      ], running: false, stopping: false, hasMore: false, total: 3, queue: [] },
    })
    expect(state.messages).not.toBe(bCache)
    expect(state.messages).toHaveLength(3)
    expect(state.messages[2].content).toBe('follow-up')
  })

  it('fulfilled replaces the array when a message body changed', () => {
    const bCache: ChatMessage[] = [
      { role: 'user', content: 'question', cls: '' },
      { role: 'assistant', content: 'the latest reply', cls: 'msg msg-a' },
    ]
    let state = { ...initial, activeSlot: 'A',
      messages: [{ role: 'user' as const, content: 'A msg', cls: '' }],
      slotMessages: { 'B': bCache } }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'B', requestId: 'r1', requestStatus: 'pending' } })
    // Same shape, one differing body — the guard must not mask a real change.
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'B', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'B', messages: [
        { role: 'user', content: 'question (edited)', cls: '' },
        { role: 'assistant', content: 'the latest reply', cls: 'msg msg-a' },
      ], running: false, stopping: false, hasMore: false, total: 2, queue: [] },
    })
    expect(state.messages).not.toBe(bCache)
    expect(state.messages).toHaveLength(2)
    expect(state.messages[0].content).toBe('question (edited)')
  })

  it('fulfilled populates messages when the slot had no cache', () => {
    let state = { ...initial, activeSlot: 'A',
      messages: [{ role: 'user' as const, content: 'A msg', cls: '' }] }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'B', requestId: 'r1', requestStatus: 'pending' } })
    expect(state.messages).toEqual([])
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'B', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'B', messages: [{ role: 'user', content: 'B msg', cls: '' }], running: false, stopping: false, hasMore: false, total: 1, queue: [] },
    })
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('B msg')
  })

  it('fulfilled still appends the in-flight streaming tail past the guard', () => {
    const bCache: ChatMessage[] = [{ role: 'user', content: 'question', cls: '' }]
    let state = { ...initial, activeSlot: 'A',
      messages: [{ role: 'user' as const, content: 'A msg', cls: '' }],
      slotMessages: { 'B': bCache } }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'B', requestId: 'r1', requestStatus: 'pending' } })
    state = reducer(state, sseChatMessage({ slot: 'B', role: 'chunk', content: 'live text' }))
    expect(state.messages[state.messages.length - 1].role).toBe('streaming')
    // Server history is behind the live stream: the WS branch must still win.
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'B', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'B', messages: [
        { role: 'user', content: 'question', cls: '' },
        { role: 'assistant', content: 'older reply', cls: 'msg msg-a' },
      ], running: true, stopping: false, hasMore: false, total: 2, queue: [] },
    })
    expect(state.messages).toHaveLength(3)
    expect(state.messages[state.messages.length - 1].role).toBe('streaming')
    expect(state.messages[state.messages.length - 1].content).toBe('live text')
    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(1)
  })

  it('fulfilled still adopts the stale-permission sweep past the guard', () => {
    const bCache: ChatMessage[] = [{ role: 'permission', content: 'approve?', cls: '', meta: { tool: 'x' } }]
    let state = { ...initial, activeSlot: 'A',
      messages: [{ role: 'user' as const, content: 'A msg', cls: '' }],
      slotMessages: { 'B': bCache } }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'B', requestId: 'r1', requestStatus: 'pending' } })
    expect(state.messages[0].meta?.resolved).toBeUndefined()
    // The sweep marks the fetched row resolved, so the guard must see a
    // difference in meta and adopt the fetched history rather than keep ours.
    state = reducer(state, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: 'B', requestId: 'r1', requestStatus: 'fulfilled' },
      payload: { key: 'B', messages: [{ role: 'permission', content: 'approve?', cls: '', meta: { tool: 'x' } }], running: false, stopping: false, hasMore: false, total: 1, queue: [] },
    })
    expect(state.messages).not.toBe(bCache)
    expect(state.messages[0].meta?.resolved).toBe('stale')
  })

  it('pending restores cached messages instantly without loading', () => {
    let state = { ...initial, activeSlot: 'A',
      messages: [{ role: 'user' as const, content: 'A msg', cls: '' }],
      slotMessages: { 'B': [{ role: 'user' as const, content: 'B msg', cls: '' }] } }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'B', requestId: 'r1', requestStatus: 'pending' } })
    // Cached messages restored instantly
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('B msg')
    expect(state.slotLoading).toBe(false)
    // Old slot's messages cached
    expect(state.slotMessages['A']).toHaveLength(1)
    expect(state.slotMessages['A'][0].content).toBe('A msg')
  })

  it('rejected clears slotLoading', () => {
    let state = { ...initial, activeSlot: 'old' }
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'new', requestId: 'r1', requestStatus: 'pending' } })
    expect(state.slotLoading).toBe(true)
    state = reducer(state, { type: 'chat/switchSlot/rejected', meta: { arg: 'new', requestId: 'r1', requestStatus: 'rejected' }, error: { message: 'fail' } })
    expect(state.slotLoading).toBe(false)
  })

  it('deleteSlot cleans up slotMessages cache', () => {
    let state = { ...initial, slotMessages: { 'A': [{ role: 'user' as const, content: 'hi', cls: '' }] } }
    state = reducer(state, { type: 'chat/deleteSlot/fulfilled', meta: { arg: 'A', requestId: 'r1', requestStatus: 'fulfilled' }, payload: 'A' })
    expect(state.slotMessages['A']).toBeUndefined()
  })
})

describe('appendSlotMessage steer reconcile', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('reconciles the steer echo into the optimistic bubble instead of duplicating (active slot)', () => {
    let state = { ...initial, activeSlot: 'A', messages: [{ role: 'streaming' as const, content: 'partial', cls: 'msg msg-a' }] }
    // steer() optimistically appends the user bubble (meta.optimistic).
    state = reducer(state, appendMessage({ role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 't1', meta: { steer: true, optimistic: true } }))
    expect(state.messages.filter(m => m.role === 'user')).toHaveLength(1)
    // Backend echoes via steer_push (meta.steer, NO optimistic) — must reconcile.
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 't2', meta: { steer: true } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].ts).toBe('t2')
    expect(users[0].meta?.optimistic).toBeUndefined()
    expect(users[0].meta?.steer).toBe(true)
  })

  it('reconciles the steer echo for a backgrounded (non-active) slot', () => {
    let state = { ...initial, activeSlot: 'A',
      slotMessages: { 'B': [{ role: 'user' as const, content: 'steered', cls: 'msg msg-u', meta: { steer: true, optimistic: true } }] } }
    state = reducer(state, appendSlotMessage({ slot: 'B', message: { role: 'user', content: 'steered', cls: 'msg msg-u', ts: 't2', meta: { steer: true } } }))
    expect(state.slotMessages['B'].filter(m => m.role === 'user')).toHaveLength(1)
    expect(state.slotMessages['B'][0].meta?.optimistic).toBeUndefined()
  })

  it('still pushes a normal (non-steer) slot message', () => {
    let state = { ...initial, activeSlot: 'A', messages: [{ role: 'user' as const, content: 'hi', cls: '' }] }
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'assistant', content: 'reply', cls: 'msg msg-a' } }))
    expect(state.messages).toHaveLength(2)
  })

  it('reconciles even when streaming/thinking messages landed after the optimistic bubble (mid-turn race)', () => {
    // Real-world duplicate: steer is by definition sent mid-turn, so chunks
    // keep streaming in. The optimistic bubble is NOT the last message when
    // the echo arrives — a tail-only check rendered two steer cards.
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'u should rebase from remote beta', cls: 'msg msg-u', ts: 't1', meta: { steer: true, optimistic: true } }))
    // Streaming content lands between optimistic append and steer_push echo.
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'thinking', content: '', cls: '' } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'streaming', content: 'checking builds…', cls: 'msg msg-a' } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'u should rebase from remote beta', cls: 'msg msg-u', ts: 't2', meta: { steer: true } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].ts).toBe('t2')
    expect(users[0].meta?.optimistic).toBeUndefined()
    expect(users[0].meta?.steer).toBe(true)
  })

  it('reconciles by most-recent optimistic steer bubble when redaction altered the echoed content', () => {
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'raw with secret AKIA123', cls: 'msg msg-u', ts: 't1', meta: { steer: true, optimistic: true } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'streaming', content: 'working…', cls: 'msg msg-a' } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'raw with secret [REDACTED]', cls: 'msg msg-u', ts: 't2', meta: { steer: true } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].content).toBe('raw with secret [REDACTED]')
    expect(users[0].meta?.optimistic).toBeUndefined()
  })

  it('matches the correct bubble for rapid back-to-back steers', () => {
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'first steer', cls: 'msg msg-u', ts: 'o1', meta: { steer: true, optimistic: true } }))
    state = reducer(state, appendMessage({ role: 'user', content: 'second steer', cls: 'msg msg-u', ts: 'o2', meta: { steer: true, optimistic: true } }))
    // Echo for the FIRST steer arrives after both optimistic bubbles exist.
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'first steer', cls: 'msg msg-u', ts: 'e1', meta: { steer: true } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(2)
    expect(users[0].ts).toBe('e1')
    expect(users[0].meta?.optimistic).toBeUndefined()
    // Second bubble untouched, still optimistic pending its own echo.
    expect(users[1].meta?.optimistic).toBe(true)
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'second steer', cls: 'msg msg-u', ts: 'e2', meta: { steer: true } } }))
    expect(state.messages.filter(m => m.role === 'user')).toHaveLength(2)
    expect(state.messages.filter(m => m.role === 'user')[1].meta?.optimistic).toBeUndefined()
  })

  it('reconciles by sendId when both echo and bubble carry one, over any content drift (#6075)', () => {
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'raw with secret AKIA123', cls: 'msg msg-u', ts: 't1', meta: { steer: true, optimistic: true, sendId: 'sid-1' } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'raw with secret [REDACTED]', cls: 'msg msg-u', ts: 't2', meta: { steer: true, sendId: 'sid-1' } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].meta?.optimistic).toBeUndefined()
    expect(users[0].content).toBe('raw with secret [REDACTED]')
  })

  it('an id-carrying echo never consumes a bubble with a DIFFERENT sendId (#6075)', () => {
    // Another tab steered too: its echo must not eat this tab's pending
    // bubble, even when the texts coincide. The foreign echo inserts its own
    // row; this tab's bubble stays optimistic until ITS echo arrives.
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'same text', cls: 'msg msg-u', ts: 't1', meta: { steer: true, optimistic: true, sendId: 'sid-mine' } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'same text', cls: 'msg msg-u', ts: 't2', meta: { steer: true, sendId: 'sid-theirs' } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(2)
    expect(users[0].meta?.optimistic).toBe(true)
    expect(users[0].meta?.sendId).toBe('sid-mine')
  })

  it('an id-carrying echo never content-consumes an ID-LESS bubble (#6075)', () => {
    // A pre-upgrade tab left an id-less optimistic steer bubble; a NEW tab
    // then steers byte-identical text with a sendId. The id-bearing echo
    // belongs to the new send: consuming the old bubble on the text
    // coincidence would overwrite it AND omit the new steer's card. Id-bearing
    // echoes match by id only — no match means insert (over-insert is the
    // recoverable direction; the old bubble keeps waiting for its own echo).
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'same text', cls: 'msg msg-u', ts: 't1', meta: { steer: true, optimistic: true } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'same text', cls: 'msg msg-u', ts: 't2', meta: { steer: true, sendId: 'sid-new-tab' } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(2)
    expect(users[0].meta?.optimistic).toBe(true)
    expect(users[0].meta?.sendId).toBeUndefined()
    expect(users[1].meta?.sendId).toBe('sid-new-tab')
  })

  it('a delayed echo after the refresh already installed the persisted row inserts nothing (#6075)', () => {
    // chat_done fires a transcript refresh that can replace the optimistic
    // bubble with the persisted steer row BEFORE the steer_push echo is
    // processed. The id-matched non-optimistic row proves the echo is a
    // redelivery: inserting would render a duplicate steer card (and
    // finalize-on-steer could freeze an unrelated live stream below it).
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'check X', cls: 'msg msg-u', ts: 't3', meta: { steer: true, sendId: 'sid-dup', mid: 'm-1' } } }))
    state = reducer(state, updateStreamingMessage('post-steer stream'))
    const before = state.messages.length
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'check X', cls: 'msg msg-u', ts: 't3', meta: { steer: true, sendId: 'sid-dup' } } }))
    expect(state.messages).toHaveLength(before)
    expect(state.messages.filter(m => m.role === 'user')).toHaveLength(1)
    // The live post-steer stream was not frozen by the redelivered echo.
    expect(state.messages.some(m => m.role === 'streaming')).toBe(true)
  })

  it('an ID-LESS echo never consumes an id-bearing bubble (#6075)', () => {
    // The gateway serves this SPA bundle, so an id-less echo is not version
    // skew — it is a DIFFERENT send that carried no id (a scene-interaction
    // steer). Consuming this tab's id-bearing bubble on the text coincidence
    // would adopt the foreign echo's identity AND suppress the bubble's own
    // later exact-id echo via the redelivery guard. The id-less echo inserts
    // its own row; the id-bearing bubble keeps waiting for its echo.
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 't1', meta: { steer: true, optimistic: true, sendId: 'sid-this-tab' } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 't2', meta: { steer: true } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(2)
    expect(users[0].meta?.optimistic).toBe(true)
    expect(users[0].meta?.sendId).toBe('sid-this-tab')
    expect(users[1].meta?.sendId).toBeUndefined()
  })

  it('does not reconcile into an unrelated non-steer optimistic user message', () => {
    // A plain queued/optimistic user message (no meta.steer) with different
    // content must NOT swallow a steer echo — the echo appends instead.
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'normal message', cls: 'msg msg-u', ts: 't1', meta: { optimistic: true } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'a steer', cls: 'msg msg-u', ts: 't2', meta: { steer: true } } }))
    expect(state.messages.filter(m => m.role === 'user')).toHaveLength(2)
  })

  it('does not consume a non-steer optimistic message even when content matches the echo exactly', () => {
    // The exact-content-match path must also require meta.steer — a plain
    // optimistic user message that happens to have identical text to the steer
    // echo is a different message and must keep its own bubble.
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'same text', cls: 'msg msg-u', ts: 't1', meta: { optimistic: true } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'same text', cls: 'msg msg-u', ts: 't2', meta: { steer: true } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(2)
    // The original optimistic bubble is untouched.
    expect(users[0].meta?.optimistic).toBe(true)
    expect(users[0].meta?.steer).toBeUndefined()
  })

  it('stashes the optimistic client ts as meta.clientTs when the echo swaps in the server ts', () => {
    // Remount-replay regression: the renderer keys rows by clientTs ?? ts.
    // Overwriting ts without stashing the client ts changed the React key,
    // remounting the bubble and replaying the steer entrance animation.
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 'client-ts', meta: { steer: true, optimistic: true } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 'server-ts', meta: { steer: true } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].ts).toBe('server-ts')
    expect(users[0].meta?.clientTs).toBe('client-ts')
    expect(users[0].meta?.optimistic).toBeUndefined()
  })

  it('does not stash clientTs when the echo carries the same ts (key already stable)', () => {
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 'same-ts', meta: { steer: true, optimistic: true } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 'same-ts', meta: { steer: true } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].meta?.clientTs).toBeUndefined()
  })

  it('does not stash clientTs when the echo has no ts (optimistic ts kept as-is)', () => {
    let state = { ...initial, activeSlot: 'A', messages: [] as ChatMessage[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 'client-ts', meta: { steer: true, optimistic: true } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'steered text', cls: 'msg msg-u', meta: { steer: true } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].ts).toBe('client-ts')
    expect(users[0].meta?.clientTs).toBeUndefined()
  })
})

describe('finalize-on-steer (stuck streaming marker fix)', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('optimistic steer bubble freezes the live streaming message; next chunk opens a NEW one below', () => {
    // Repro of the bug: mid-text-stream steer. Without the freeze, the chunk
    // reducer (backwards scan for role==='streaming') kept streaming the rest
    // of the segment into the message ABOVE the bubble.
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, sseChatMessage({ slot: 'A', role: 'chunk', content: 'pre-steer text' }))
    state = reducer(state, appendMessage({ role: 'user', content: 'go left', cls: 'msg msg-u', ts: 't1', meta: { steer: true, optimistic: true } }))
    // Pre-steer text is frozen as assistant ABOVE the bubble.
    expect(state.messages.map(m => m.role)).toEqual(['assistant', 'user'])
    expect(state.messages[0].content).toBe('pre-steer text')
    expect(state.messages[0].rawText).toBe('pre-steer text')
    // Post-steer chunks open a fresh streaming message BELOW the bubble.
    state = reducer(state, sseChatMessage({ slot: 'A', role: 'chunk', content: 'post-steer text' }))
    expect(state.messages.map(m => m.role)).toEqual(['assistant', 'user', 'streaming'])
    expect(state.messages[2].content).toBe('post-steer text')
  })

  it('drops a placeholder-only streaming message instead of freezing it', () => {
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, sseChatMessage({ slot: 'A', role: 'chunk', content: '…' }))
    state = reducer(state, appendMessage({ role: 'user', content: 'go left', cls: 'msg msg-u', ts: 't1', meta: { steer: true, optimistic: true } }))
    expect(state.messages.map(m => m.role)).toEqual(['user'])
  })

  it('steer echo with no optimistic bubble (other-tab view) freezes before inserting', () => {
    // This tab did not initiate the steer — no optimistic bubble to reconcile,
    // so appendSlotMessage inserts the echo. It must freeze first.
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, sseChatMessage({ slot: 'A', role: 'chunk', content: 'pre-steer text' }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'go left', cls: 'msg msg-u', ts: 't2', meta: { steer: true } } }))
    expect(state.messages.map(m => m.role)).toEqual(['assistant', 'user'])
    state = reducer(state, sseChatMessage({ slot: 'A', role: 'chunk', content: 'post' }))
    expect(state.messages.map(m => m.role)).toEqual(['assistant', 'user', 'streaming'])
  })

  it('steer echo freeze also applies to a backgrounded slot array', () => {
    let state = { ...initial, activeSlot: 'A',
      slotMessages: { 'B': [{ role: 'streaming' as const, content: 'bg partial', cls: 'msg msg-a' }] } }
    state = reducer(state, appendSlotMessage({ slot: 'B', message: { role: 'user', content: 'go left', cls: 'msg msg-u', ts: 't2', meta: { steer: true } } }))
    expect(state.slotMessages['B'].map(m => m.role)).toEqual(['assistant', 'user'])
  })

  it('echo reconcile does NOT freeze a live post-steer streaming message', () => {
    // Freeze happened at optimistic-push time; by the time the echo arrives a
    // NEW post-steer streaming message can be live below the bubble. The
    // reconcile path must leave it streaming.
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, sseChatMessage({ slot: 'A', role: 'chunk', content: 'pre' }))
    state = reducer(state, appendMessage({ role: 'user', content: 'go left', cls: 'msg msg-u', ts: 't1', meta: { steer: true, optimistic: true } }))
    state = reducer(state, sseChatMessage({ slot: 'A', role: 'chunk', content: 'post' }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'go left', cls: 'msg msg-u', ts: 't2', meta: { steer: true } } }))
    expect(state.messages.map(m => m.role)).toEqual(['assistant', 'user', 'streaming'])
    expect(state.messages[1].ts).toBe('t2')
    expect(state.messages[2].content).toBe('post')
  })

  it('non-steer appendMessage does not touch a live streaming message', () => {
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, sseChatMessage({ slot: 'A', role: 'chunk', content: 'streaming on' }))
    state = reducer(state, appendMessage({ role: 'user', content: 'plain message', cls: 'msg msg-u', ts: 't1' }))
    expect(state.messages.map(m => m.role)).toEqual(['streaming', 'user'])
  })
})

describe('sseChatMessage', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }

  it('ignores messages for other slots', () => {
    const state = reducer(withSlot, sseChatMessage({ slot: 'other', role: 'user', content: 'hi' }))
    expect(state.messages).toHaveLength(0)
  })

  it('accumulates chunks into streaming message', () => {
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'Hello' }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].role).toBe('streaming')
    expect(state.slotState).toBe('streaming')

    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: ' world' }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('Hello world')
  })

  it('detects missed chunks via sequence gap', () => {
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'a', seq: 1 }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'c', seq: 5 }))
    expect(state.messages[0].content).toContain('chunk(s) missed')
  })

  it('_done finalizes streaming to assistant', () => {
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'response' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_done', content: '' }))
    expect(state.messages[0].role).toBe('assistant')
    expect(state.slotRunning).toBe(false)
    expect(state.slotState).toBe('idle')
  })

  it('tool message sets tool_running state', () => {
    const state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'tool', content: '🔧 bash' }))
    expect(state.slotState).toBe('tool_running')
    expect(state.messages[0].role).toBe('tool')
  })

  it('tool message does NOT deduplicate consecutive same-tool calls', () => {
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'tool', content: '🔧 bash' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'tool', content: '🔧 bash' }))
    expect(state.messages).toHaveLength(2)
  })

  it('does NOT collapse non-consecutive same-tool calls [A, B, A]', () => {
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'tool', content: '🔧 bash' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'tool', content: '🔧 read' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'tool', content: '🔧 bash' }))
    expect(state.messages).toHaveLength(3)
    expect(state.messages[0].content).toBe('🔧 bash')
    expect(state.messages[2].content).toBe('🔧 bash')
  })

  it('appends regular messages', () => {
    const state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'permission', content: 'run bash?' }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].role).toBe('permission')
  })

  it('reconciles user echo (with mid) even when assistant frames arrived first (#2845)', () => {
    // Race condition: user sends a message, agent starts streaming before the
    // server echoes the user frame with its mid. The reconcile uses the
    // client-generated sendId to correlate the echo with its optimistic bubble.
    let state = withSlot
    // 1. Optimistic user bubble via appendMessage (like ChatPage send handler).
    state = reducer(state, appendMessage({ role: 'user', content: 'deploy to prod', cls: '', ts: '2026-08-11T09:59:59.000000+00:00', meta: { sendId: 's-test-123' } }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].meta?.optimistic).toBe(true)
    expect(state.messages[0].meta?.sendId).toBe('s-test-123')

    // 2. Agent starts streaming before the user echo arrives.
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'Starting deployment...' }))
    expect(state.messages).toHaveLength(2)
    expect(state.messages[1].role).toBe('streaming')

    // 3. Server echoes the user frame WITH mid AND the same sendId.
    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'deploy to prod',
      ts: '2026-08-11T10:00:00.000000+00:00', meta: { mid: 'm-user-1', sendId: 's-test-123' },
    }))

    // Should NOT duplicate the user message — still 2 messages total.
    expect(state.messages).toHaveLength(2)
    // The original user bubble now has the mid from the server echo.
    expect(state.messages[0].role).toBe('user')
    expect(state.messages[0].content).toBe('deploy to prod')
    expect(state.messages[0].meta?.mid).toBe('m-user-1')
    expect(state.messages[0].ts).toBe('2026-08-11T10:00:00.000000+00:00')
    // Optimistic marker cleared after reconcile.
    expect(state.messages[0].meta?.optimistic).toBeUndefined()
  })

  it('reconciles user echo even when tool frames intervene (#2845)', () => {
    let state = withSlot
    // 1. Optimistic user bubble via appendMessage.
    state = reducer(state, appendMessage({ role: 'user', content: 'fix the bug', cls: '', ts: '2026-08-11T10:00:59.000000+00:00', meta: { sendId: 's-test-456' } }))
    // 2. Tool frame arrives (agent called a tool before echo).
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'tool', content: '🔧 bash' }))
    // 3. Streaming starts.
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'Reading...' }))
    expect(state.messages).toHaveLength(3)

    // 4. Server echo with mid and sendId.
    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'fix the bug',
      ts: '2026-08-11T10:01:00.000000+00:00', meta: { mid: 'm-user-2', sendId: 's-test-456' },
    }))

    // No duplicate — still 3.
    expect(state.messages).toHaveLength(3)
    expect(state.messages[0].role).toBe('user')
    expect(state.messages[0].meta?.mid).toBe('m-user-2')
    expect(state.messages[0].meta?.optimistic).toBeUndefined()
  })

  it('does not reconcile a message with different sendId even if content matches (#2845)', () => {
    let state = withSlot
    // Optimistic bubble with one sendId.
    state = reducer(state, appendMessage({ role: 'user', content: 'yes', cls: '', ts: '2026-08-11T10:00:00.000000+00:00', meta: { sendId: 's-mine' } }))

    // A distinct channel message with same content but a DIFFERENT sendId
    // (or no sendId) — must NOT be consumed.
    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'yes',
      ts: '2026-08-11T10:00:01.000000+00:00', meta: { mid: 'm-channel', sendId: 's-other' },
    }))

    // Both messages kept — different sendIds mean different sends.
    expect(state.messages.filter(m => m.role === 'user')).toHaveLength(2)
    // Original optimistic bubble still has its sendId and is still optimistic.
    expect(state.messages[0].meta?.sendId).toBe('s-mine')
    expect(state.messages[0].meta?.optimistic).toBe(true)
  })

  it('does not reconcile channel messages that lack sendId', () => {
    let state = withSlot
    // Optimistic bubble.
    state = reducer(state, appendMessage({ role: 'user', content: 'hello', cls: '', ts: '2026-08-11T10:00:00.000000+00:00', meta: { sendId: 's-abc' } }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'working...' }))

    // Channel message with same content but no sendId.
    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'hello',
      ts: '2026-08-11T10:00:02.000000+00:00', meta: { mid: 'm-chan' },
    }))

    // Not reconciled — pushed as new.
    expect(state.messages.filter(m => m.role === 'user')).toHaveLength(2)
  })

  it('does not reconcile into a steered user message', () => {
    let state = withSlot
    // A steered user message (meta.steer = true) — these have their own
    // reconcile path and lifecycle; the regular echo must not touch them.
    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'steer instruction',
      meta: { steer: true, mid: 'm-steer-1' },
    }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'following steer...' }))
    expect(state.messages).toHaveLength(2)

    // A regular echo arrives with same content — should push as new, not
    // mutate the steered bubble.
    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'steer instruction',
      ts: '2026-08-11T09:04:00.000000+00:00', meta: { mid: 'm-new-user' },
    }))
    expect(state.messages).toHaveLength(3)
    // The steered bubble is untouched.
    expect(state.messages[0].meta?.steer).toBe(true)
    expect(state.messages[0].meta?.mid).toBe('m-steer-1')
  })
})

describe('sseChatMessage — pipelined sends reconcile (#3898)', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }

  it('reconciles echo for first message when second was sent before echo arrived', () => {
    let state = withSlot
    // User sends message A, then message B in quick succession (pipelined).
    state = reducer(state, appendMessage({ role: 'user', content: 'first', cls: '', ts: '2026-08-16T10:00:00.000Z', meta: { sendId: 's-first' } }))
    state = reducer(state, appendMessage({ role: 'user', content: 'second', cls: '', ts: '2026-08-16T10:00:01.000Z', meta: { sendId: 's-second' } }))
    expect(state.messages).toHaveLength(2)
    expect(state.messages[0].meta?.optimistic).toBe(true)
    expect(state.messages[1].meta?.optimistic).toBe(true)

    // Echo for message A arrives — must find it past message B.
    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'first',
      ts: '2026-08-16T10:00:00.100Z', meta: { mid: 'm-first', sendId: 's-first' },
    }))

    // Should NOT duplicate — still 2 messages.
    expect(state.messages).toHaveLength(2)
    expect(state.messages[0].meta?.mid).toBe('m-first')
    expect(state.messages[0].meta?.optimistic).toBeUndefined()
    // Keep correlation for the first send's still-pending HTTP request.
    expect(state.messages[0].meta?.sendId).toBe('s-first')
    // Second message still optimistic
    expect(state.messages[1].meta?.optimistic).toBe(true)
    expect(state.messages[1].meta?.sendId).toBe('s-second')
  })

  it('reconciles echo for second message after first was already reconciled', () => {
    let state = withSlot
    state = reducer(state, appendMessage({ role: 'user', content: 'first', cls: '', ts: '2026-08-16T10:00:00.000Z', meta: { sendId: 's-first' } }))
    state = reducer(state, appendMessage({ role: 'user', content: 'second', cls: '', ts: '2026-08-16T10:00:01.000Z', meta: { sendId: 's-second' } }))

    // Reconcile first
    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'first',
      ts: '2026-08-16T10:00:00.100Z', meta: { mid: 'm-first', sendId: 's-first' },
    }))
    // Reconcile second
    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'second',
      ts: '2026-08-16T10:00:01.100Z', meta: { mid: 'm-second', sendId: 's-second' },
    }))

    expect(state.messages).toHaveLength(2)
    expect(state.messages[0].meta?.mid).toBe('m-first')
    expect(state.messages[1].meta?.mid).toBe('m-second')
    expect(state.messages[0].meta?.optimistic).toBeUndefined()
    expect(state.messages[1].meta?.optimistic).toBeUndefined()
    expect(state.messages[0].meta?.sendId).toBe('s-first')
    expect(state.messages[1].meta?.sendId).toBe('s-second')
  })

  it('reconciles pipelined sends even with streaming frames interleaved', () => {
    let state = withSlot
    state = reducer(state, appendMessage({ role: 'user', content: 'msg-a', cls: '', ts: '2026-08-16T10:00:00.000Z', meta: { sendId: 's-a' } }))
    // Streaming from first turn starts
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'Thinking...' }))
    // User sends second message (new turn)
    state = reducer(state, appendMessage({ role: 'user', content: 'msg-b', cls: '', ts: '2026-08-16T10:00:02.000Z', meta: { sendId: 's-b' } }))
    expect(state.messages.filter(m => m.role === 'user')).toHaveLength(2)

    // Echo for msg-a arrives
    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'msg-a',
      ts: '2026-08-16T10:00:00.050Z', meta: { mid: 'm-a', sendId: 's-a' },
    }))

    // Should reconcile without duplication
    const userMsgs = state.messages.filter(m => m.role === 'user')
    expect(userMsgs).toHaveLength(2)
    expect(userMsgs[0].meta?.mid).toBe('m-a')
    expect(userMsgs[0].meta?.optimistic).toBeUndefined()
  })

  it('keeps sendId after reconciliation so a late HTTP failure can recognize delivery', () => {
    let state = withSlot
    state = reducer(state, appendMessage({ role: 'user', content: 'hello', cls: '', ts: '2026-08-16T10:00:00.000Z', meta: { sendId: 's-hello' } }))
    expect(state.messages[0].meta?.sendId).toBe('s-hello')

    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'hello',
      ts: '2026-08-16T10:00:00.100Z', meta: { mid: 'm-hello', sendId: 's-hello' },
    }))

    expect(state.messages[0].meta?.sendId).toBe('s-hello')
    expect(state.messages[0].meta?.mid).toBe('m-hello')
  })

  it('records no wall-clock timestamp on an optimistic bubble', () => {
    // The bubble carries `optimistic` (the reconcile scan's marker, load-bearing
    // for #3898) and nothing else. A per-send `optimisticTs` used to ride along
    // to drive a 30s "may not have been delivered" notice: the send path already
    // reports a real failure with its own error bubble and restores the
    // composer, so that notice could only appear once the server had ACCEPTED
    // the message, asserting as uncertain the one thing least likely to be true.
    // It also persisted into the durable transcript and was never cleaned up.
    let state = withSlot
    state = reducer(state, appendMessage({ role: 'user', content: 'pending', cls: '', ts: '2026-08-16T10:00:00.000Z', meta: { sendId: 's-x' } }))

    expect(state.messages[0].meta?.optimistic).toBe(true)
    expect(state.messages[0].meta?.optimisticTs).toBeUndefined()
    expect(state.messages[0].meta?.stale).toBeUndefined()
  })
})
