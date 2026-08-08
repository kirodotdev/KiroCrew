import { describe, it, expect } from 'vitest'
import type { ChatMessage, SubagentActivity, ToolActivity } from '../types'
import reducer, {
  setActiveSlot,
  setPendingInput,
  appendMessage,
  appendSlotMessage,
  updateStreamingMessage,
  finalizeAssistant,
  removeThinking,
  setSlotRunning,
  setSlotStopping,
  startLocalTurn,
  syncSlotRunningFromServer,
  setSlotState,
  setSlotStatusDetail,
  clearMessages,
  sseChatMessage,
  sseThinkingChunk,
  refreshSlot,
  warmSlotCache,
  sseSubagentPending,
  sseSubagentSpawn,
  sseSubagentChunk,
  sseSubagentTool,
  sseSubagentDone,
  sseToolActivity,
  sseToolResult,
  sseActivityEvent,
  sseChatMessageUpdate,
  sseContextUsage,
  toggleActivity,
  openActivityPanel,
  openActivityToTab,
  switchSlot,
  resolveByApprovalId,
  sseSideResult,
  sideClose,
  appendQueuedMessage,
  editQueuedMessage,
  cancelQueuedMessage,
  selectSlotSubagentsActive,
  selectSlotPendingSpawnApprovals,
  selectSlotPendingApproval,
  selectComposerBusy,
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

    it('syncSlotRunningFromServer ignores updates for non-active slots', () => {
      let state = active('chat-1')
      state = reducer(state, startLocalTurn('chat-1'))
      const before = state.slotRunning
      state = reducer(state, syncSlotRunningFromServer({ slot: 'chat-2', running: false, stopping: true }))
      expect(state.slotRunning).toBe(before)
      expect(state.slotStopping).toBe(false)
    })
  })

  it('setSlotStatusDetail updates kind, text, and ts', () => {
    const now = Date.now()
    const state = reducer(initial, setSlotStatusDetail({ slot: 'test-slot', kind: 'thinking', text: 'Thinking…', ts: now }))
    expect(state.slotStatusDetail['test-slot'].kind).toBe('thinking')
    expect(state.slotStatusDetail['test-slot'].text).toBe('Thinking…')
    expect(state.slotStatusDetail['test-slot'].ts).toBe(now)
    // Tool name optional
    const state2 = reducer(state, setSlotStatusDetail({ slot: 'test-slot', kind: 'tool', text: 'Tool: read', toolName: 'read', ts: now }))
    expect(state2.slotStatusDetail['test-slot'].toolName).toBe('read')
    // Idle clears
    const state3 = reducer(state2, setSlotStatusDetail({ slot: 'test-slot', kind: 'idle', text: 'Ready', ts: now }))
    expect(state3.slotStatusDetail['test-slot'].kind).toBe('idle')
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
    let state = { ...initial, activeSlot: 'A', messages: [] as any[] }
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
    let state = { ...initial, activeSlot: 'A', messages: [] as any[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'raw with secret AKIA123', cls: 'msg msg-u', ts: 't1', meta: { steer: true, optimistic: true } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'streaming', content: 'working…', cls: 'msg msg-a' } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'raw with secret [REDACTED]', cls: 'msg msg-u', ts: 't2', meta: { steer: true } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].content).toBe('raw with secret [REDACTED]')
    expect(users[0].meta?.optimistic).toBeUndefined()
  })

  it('matches the correct bubble for rapid back-to-back steers', () => {
    let state = { ...initial, activeSlot: 'A', messages: [] as any[] }
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

  it('does not reconcile into an unrelated non-steer optimistic user message', () => {
    // A plain queued/optimistic user message (no meta.steer) with different
    // content must NOT swallow a steer echo — the echo appends instead.
    let state = { ...initial, activeSlot: 'A', messages: [] as any[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'normal message', cls: 'msg msg-u', ts: 't1', meta: { optimistic: true } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'a steer', cls: 'msg msg-u', ts: 't2', meta: { steer: true } } }))
    expect(state.messages.filter(m => m.role === 'user')).toHaveLength(2)
  })

  it('does not consume a non-steer optimistic message even when content matches the echo exactly', () => {
    // The exact-content-match path must also require meta.steer — a plain
    // optimistic user message that happens to have identical text to the steer
    // echo is a different message and must keep its own bubble.
    let state = { ...initial, activeSlot: 'A', messages: [] as any[] }
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
    let state = { ...initial, activeSlot: 'A', messages: [] as any[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 'client-ts', meta: { steer: true, optimistic: true } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 'server-ts', meta: { steer: true } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].ts).toBe('server-ts')
    expect(users[0].meta?.clientTs).toBe('client-ts')
    expect(users[0].meta?.optimistic).toBeUndefined()
  })

  it('does not stash clientTs when the echo carries the same ts (key already stable)', () => {
    let state = { ...initial, activeSlot: 'A', messages: [] as any[] }
    state = reducer(state, appendMessage({ role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 'same-ts', meta: { steer: true, optimistic: true } }))
    state = reducer(state, appendSlotMessage({ slot: 'A', message: { role: 'user', content: 'steered text', cls: 'msg msg-u', ts: 'same-ts', meta: { steer: true } } }))
    const users = state.messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].meta?.clientTs).toBeUndefined()
  })

  it('does not stash clientTs when the echo has no ts (optimistic ts kept as-is)', () => {
    let state = { ...initial, activeSlot: 'A', messages: [] as any[] }
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
})

describe('sseChatMessage — _segment handling', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }

  it('_segment converts streaming → assistant, preserves content and rawText', () => {
    // Req 2.1, 5.2: streaming message finalized to assistant with rawText preserved
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'analysis text' }))
    expect(state.messages[0].role).toBe('streaming')

    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_segment', content: '' }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[0].content).toBe('analysis text')
    expect(state.messages[0].rawText).toBe('analysis text')
  })

  it('_segment with no streaming message is a no-op', () => {
    // Req 2.2: no streaming message → no change
    const state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: '_segment', content: '' }))
    expect(state.messages).toHaveLength(0)
  })

  it('_segment does not reset lastChunkSeq', () => {
    // Req 7.2: lastChunkSeq preserved across segment boundaries
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'text', seq: 5 }))
    expect(state.lastChunkSeq).toBe(5)

    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_segment', content: '' }))
    expect(state.lastChunkSeq).toBe(5)
    // slotState and slotRunning also unchanged
    expect(state.slotState).toBe('streaming')
  })

  it('chunk after _segment creates new streaming message', () => {
    // Req 2.3: new streaming message after segment boundary
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'before tool' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_segment', content: '' }))
    expect(state.messages[0].role).toBe('assistant')

    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'after tool' }))
    expect(state.messages).toHaveLength(2)
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[0].content).toBe('before tool')
    expect(state.messages[1].role).toBe('streaming')
    expect(state.messages[1].content).toBe('after tool')
  })

  it('tool insertion after _segment places tool after finalized assistant', () => {
    // Req 3.1: tool card inserted after the finalized assistant message
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'reasoning' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_segment', content: '' }))
    // Now: [assistant]
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'tool', content: '🔧 read_file' }))
    // Now: [assistant, tool]
    expect(state.messages).toHaveLength(2)
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[1].role).toBe('tool')
    expect(state.messages[1].content).toBe('🔧 read_file')
  })

  it('_done after segmented stream converts final streaming → assistant', () => {
    // Req 4.1: final streaming message finalized on _done after a segment
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'part 1' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_segment', content: '' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'tool', content: '🔧 bash' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'part 2' }))
    // Now: [assistant, tool, streaming]
    expect(state.messages).toHaveLength(3)
    expect(state.messages[2].role).toBe('streaming')

    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_done', content: '' }))
    // Now: [assistant, tool, assistant]
    expect(state.messages).toHaveLength(3)
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[0].content).toBe('part 1')
    expect(state.messages[1].role).toBe('tool')
    expect(state.messages[2].role).toBe('assistant')
    expect(state.messages[2].content).toBe('part 2')
    expect(state.slotRunning).toBe(false)
    expect(state.slotState).toBe('idle')
  })

  it('tool-free stream produces single assistant message (regression)', () => {
    // Req 8.2: no segments → single assistant message, identical to pre-feature behavior
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'hello ' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'world' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_done', content: '' }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[0].content).toBe('hello world')
  })
})

describe('subagent reducers', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }

  it('sseSubagentPending creates pending entry', () => {
    const state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: 'do stuff', approval_id: 'spawn:a1' }))
    expect(state.subagents['a1']).toBeDefined()
    expect(state.subagents['a1'].status).toBe('pending')
    expect(state.subagents['a1'].approval_id).toBe('spawn:a1')
  })

  it('sseSubagentPending ignores wrong slot', () => {
    const state = reducer(withSlot, sseSubagentPending({ slot: 'other', id: 'a1', task: 'do stuff', approval_id: 'spawn:a1' }))
    expect(state.subagents['a1']).toBeUndefined()
  })

  it('sseSubagentSpawn creates running entry', () => {
    const state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'search code', agent: 'amzn-builder' }))
    expect(state.subagents['a1'].status).toBe('running')
    expect(state.subagents['a1'].agent).toBe('amzn-builder')
    expect(state.subagents['a1'].task).toBe('search code')
  })

  it('sseSubagentSpawn preserves existing streaming text from pending', () => {
    let state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: 'task', approval_id: 'spawn:a1' }))
    state = reducer(state, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: 'kirocrew' }))
    expect(state.subagents['a1'].status).toBe('running')
    expect(state.subagents['a1'].startedAt).toBeDefined()
  })

  it('sseSubagentSpawn ignores wrong slot', () => {
    const state = reducer(withSlot, sseSubagentSpawn({ slot: 'other', id: 'a1', task: 'task', agent: '' }))
    expect(state.subagents['a1']).toBeUndefined()
  })

  it('sseSubagentChunk appends streaming text', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: '' }))
    state = reducer(state, sseSubagentChunk({ slot: 'slot-1', id: 'a1', text: 'hello ' }))
    state = reducer(state, sseSubagentChunk({ slot: 'slot-1', id: 'a1', text: 'world' }))
    expect(state.subagents['a1'].streaming).toBe('hello world')
  })

  it('sseSubagentChunk ignores unknown agent', () => {
    const state = reducer(withSlot, sseSubagentChunk({ slot: 'slot-1', id: 'unknown', text: 'data' }))
    expect(state.subagents['unknown']).toBeUndefined()
  })

  it('sseSubagentTool updates lastTool and status', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: '' }))
    state = reducer(state, sseSubagentTool({ slot: 'slot-1', id: 'a1', tool: 'grep' }))
    expect(state.subagents['a1'].lastTool).toBe('grep')
    expect(state.subagents['a1'].status).toBe('tool')
  })

  it('sseSubagentDone marks as done', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: '' }))
    state = reducer(state, sseSubagentDone({ slot: 'slot-1', id: 'a1', elapsed: 5.2 }))
    expect(state.subagents['a1'].status).toBe('done')
    expect(state.subagents['a1'].elapsed).toBe(5.2)
  })

  it('sseSubagentDone marks as error when error present', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: '' }))
    state = reducer(state, sseSubagentDone({ slot: 'slot-1', id: 'a1', elapsed: 10, error: 'timeout' }))
    expect(state.subagents['a1'].status).toBe('error')
    expect(state.subagents['a1'].error).toBe('timeout')
  })

  it('sseSubagentDone creates entry retroactively if spawn was missed', () => {
    const state = reducer(withSlot, sseSubagentDone({ slot: 'slot-1', id: 'late1', elapsed: 3, task: 'late task', agent: 'kiro-cli' }))
    expect(state.subagents['late1']).toBeDefined()
    expect(state.subagents['late1'].status).toBe('done')
    expect(state.subagents['late1'].task).toBe('late task')
  })

  it('sseSubagentSpawn copies task onto pending card (empty approval-derived task)', () => {
    // Pending card created from a spawn-approval event whose title carried no
    // task text — the later spawn event holds the authoritative task.
    let state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: '', approval_id: 'spawn:a1' }))
    state = reducer(state, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'scan package X', agent: 'kirocrew' }))
    expect(state.subagents['a1'].status).toBe('running')
    expect(state.subagents['a1'].task).toBe('scan package X')
  })

  it('sseSubagentDone resolves card by id across slots (mis-bucketed card)', () => {
    // Card lives under activeSlot but the done event arrives with a different
    // slot key (e.g. parent session key changed after reset) — the card must
    // still transition to done instead of staying stuck "running" forever.
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: '' }))
    state = reducer(state, sseSubagentDone({ slot: 'other-slot', id: 'a1', elapsed: 7 }))
    expect(state.subagents['a1'].status).toBe('done')
    expect(state.subagents['a1'].elapsed).toBe(7)
  })

  it('sseSubagentDone backfills empty task from done payload', () => {
    let state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: '', approval_id: 'spawn:a1' }))
    state = reducer(state, sseSubagentDone({ slot: 'slot-1', id: 'a1', elapsed: 4, task: 'the real task' }))
    expect(state.subagents['a1'].status).toBe('done')
    expect(state.subagents['a1'].task).toBe('the real task')
  })

  it('switchSlot saves and restores subagents per slot', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: '' }))
    // Switch away
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'slot-2', requestId: 'r1', requestStatus: 'pending' } })
    expect(state.subagents).toEqual({})
    // Switch back
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'slot-1', requestId: 'r2', requestStatus: 'pending' } })
    expect(state.subagents['a1']).toBeDefined()
    expect(state.subagents['a1'].status).toBe('running')
  })
})

describe('activity viewer reducers', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }

  it('toggleActivity flips activityOpen', () => {
    expect(initial.activityOpen).toBe(false)
    const state = reducer(initial, toggleActivity())
    expect(state.activityOpen).toBe(true)
    expect(reducer(state, toggleActivity()).activityOpen).toBe(false)
  })

  it('counts a view request only when one is actually made', () => {
    // The counter is what tells the side panel's tab strip "focus this view".
    // A chat switch restores the incoming chat's cached activityTab, and that
    // restore must NOT read as a request — otherwise reopening a chat drags
    // focus off the tab the user left it on.
    expect(initial.activityTabRequest).toBe(0)
    const requested = reducer(withSlot, openActivityToTab('subagents'))
    expect(requested.activityTabRequest).toBe(1)
    // Same view asked for twice is two requests: the user may have clicked away
    // in the strip in between, and the second ask must still pull focus back.
    expect(reducer(requested, openActivityToTab('subagents')).activityTabRequest).toBe(2)

    const switched = reducer(requested, switchSlot.pending('req-1', 'slot-2'))
    expect(switched.activityTab).toBe('files')
    expect(switched.activityTabRequest).toBe(1)
    // Opening the panel without naming a view is not a request either.
    expect(reducer(switched, openActivityPanel()).activityTabRequest).toBe(1)
  })

  it('sseToolActivity adds to toolLog', () => {
    const state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'grep', kind: 'read', purpose: 'search', input_preview: 'pattern' }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].text).toBe('grep')
    expect(state.toolLog[0].purpose).toBe('search')
  })

  it('sseToolActivity ignores wrong slot', () => {
    const state = reducer(withSlot, sseToolActivity({ slot: 'other', tool: 'grep', kind: 'read', purpose: '', input_preview: '' }))
    expect(state.toolLog).toHaveLength(0)
  })

  it('sseToolResult attaches output to last tool entry', () => {
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'grep', kind: 'read', purpose: 'search', input_preview: 'pattern' }))
    state = reducer(state, sseToolResult({ slot: 'slot-1', output: 'found 3 matches' }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].output).toBe('found 3 matches')
  })

  it('sseToolResult is noop without prior tool entry', () => {
    const state = reducer(withSlot, sseToolResult({ slot: 'slot-1', output: 'orphan' }))
    expect(state.toolLog).toHaveLength(0)
  })

  it('sseActivityEvent adds system event to toolLog', () => {
    const state = reducer(withSlot, sseActivityEvent({ slot: 'slot-1', kind: 'context', text: 'Injected 5000 chars' }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].type).toBe('context')
  })

  it('sseActivityEvent ignores wrong slot', () => {
    const state = reducer(withSlot, sseActivityEvent({ slot: 'other', kind: 'context', text: 'data' }))
    expect(state.toolLog).toHaveLength(0)
  })
})

describe('approval_resolved and toolLog mutations', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }

  it('approval_resolved marks matching approval entry as resolved', () => {
    let state = reducer(withSlot, sseActivityEvent({ slot: 'slot-1', kind: 'approval', text: 'spawn_run', approval_id: 'spawn:a1', approval_type: 'spawn' }))
    expect(state.toolLog).toHaveLength(1)
    state = reducer(state, sseActivityEvent({ slot: 'slot-1', kind: 'approval_resolved', text: '', approval_id: 'spawn:a1' }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].type).toBe('approval_resolved')
  })

  it('approval_resolved only affects matching approval entries', () => {
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'grep', kind: 'read', purpose: 'search', input_preview: 'pattern' }))
    state = reducer(state, sseActivityEvent({ slot: 'slot-1', kind: 'approval', text: 'spawn_run', approval_id: 'spawn:a1', approval_type: 'spawn' }))
    expect(state.toolLog).toHaveLength(2)
    state = reducer(state, sseActivityEvent({ slot: 'slot-1', kind: 'approval_resolved', text: '', approval_id: 'spawn:a1' }))
    expect(state.toolLog).toHaveLength(2)
    expect(state.toolLog[0].type).toBe('tool')
    expect(state.toolLog[1].type).toBe('approval_resolved')
  })

  it('toolLog clears on new user message', () => {
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'grep', kind: 'read', purpose: '', input_preview: '' }))
    state = reducer(state, sseToolActivity({ slot: 'slot-1', tool: 'read', kind: 'read', purpose: '', input_preview: '' }))
    expect(state.toolLog).toHaveLength(2)
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'user', content: 'hello' }))
    expect(state.toolLog).toHaveLength(0)
  })

  it('sseToolResult matches by tool_call_id', () => {
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'grep', kind: 'read', purpose: '', input_preview: '', tool_call_id: 'tc1' }))
    state = reducer(state, sseToolActivity({ slot: 'slot-1', tool: 'read', kind: 'read', purpose: '', input_preview: '', tool_call_id: 'tc2' }))
    state = reducer(state, sseToolResult({ slot: 'slot-1', output: 'grep output', tool_call_id: 'tc1' }))
    expect(state.toolLog[0].output).toBe('grep output')
    expect(state.toolLog[1].output).toBeUndefined()
  })

  it('sseToolActivity is_update merges into existing entry by tool_call_id', () => {
    // Simulate the claude-agent-acp two-phase flow: first a stub tool_call,
    // then a tool_call_update with the refined title/input.
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'Terminal', kind: 'execute', purpose: '', input_preview: '', tool_call_id: 'tc-bash-1' }))
    expect(state.toolLog).toHaveLength(1)
    state = reducer(state, sseToolActivity({ slot: 'slot-1', tool: 'List KiroCrew modules', kind: 'execute', purpose: '', input_preview: '{"command":"ls"}', tool_call_id: 'tc-bash-1', is_update: true }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].text).toBe('List KiroCrew modules')
    expect(state.toolLog[0].input).toBe('{"command":"ls"}')
  })

  it('sseToolActivity without is_update does not merge — replayed initial event appends', () => {
    // A duplicate initial tool_call (e.g. WebSocket reconnect/replay) should
    // NOT silently merge into the previous entry. Only is_update events do.
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'Terminal', kind: 'execute', purpose: '', input_preview: '', tool_call_id: 'tc-bash-1' }))
    state = reducer(state, sseToolActivity({ slot: 'slot-1', tool: 'Terminal', kind: 'execute', purpose: '', input_preview: '', tool_call_id: 'tc-bash-1' }))
    expect(state.toolLog).toHaveLength(2)
  })

  it('sseToolActivity is_update with no existing entry falls through to append', () => {
    // If the update arrives before its initial tool_call (out of order, or
    // initial dropped), don't drop it on the floor — append a new row.
    const state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'ls /tmp', kind: 'execute', purpose: '', input_preview: '', tool_call_id: 'tc-orphan', is_update: true }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].text).toBe('ls /tmp')
  })

  it('sseChatMessageUpdate patches matching tool message content+meta', () => {
    let state = reducer(withSlot, appendMessage({ role: 'tool', content: '🔧 Terminal', cls: '', meta: { tool_call_id: 'tc-bash-1' } }))
    state = reducer(state, sseChatMessageUpdate({ slot: 'slot-1', tool_call_id: 'tc-bash-1', content: '🔧 ls /tmp', meta: { input: '{"command":"ls /tmp"}' } }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('🔧 ls /tmp')
    expect(state.messages[0].meta?.input).toBe('{"command":"ls /tmp"}')
    expect(state.messages[0].meta?.tool_call_id).toBe('tc-bash-1')
  })

  it('sseChatMessageUpdate walks reverse and stops at most-recent match', () => {
    // Two tool messages can share a tool_call_id (auto-approved tools emit
    // 🔧 + ✅). The reducer should patch the most recent (the ✅).
    let state = reducer(withSlot, appendMessage({ role: 'tool', content: '🔧 Terminal', cls: '', meta: { tool_call_id: 'tc-bash-1' } }))
    state = reducer(state, appendMessage({ role: 'tool', content: '✅ Terminal', cls: '', meta: { tool_call_id: 'tc-bash-1' } }))
    state = reducer(state, sseChatMessageUpdate({ slot: 'slot-1', tool_call_id: 'tc-bash-1', content: '✅ ls /tmp' }))
    expect(state.messages[0].content).toBe('🔧 Terminal')
    expect(state.messages[1].content).toBe('✅ ls /tmp')
  })

  it('sseChatMessageUpdate is no-op when slot mismatches active', () => {
    let state = reducer(withSlot, appendMessage({ role: 'tool', content: '🔧 Terminal', cls: '', meta: { tool_call_id: 'tc-bash-1' } }))
    state = reducer(state, sseChatMessageUpdate({ slot: 'other', tool_call_id: 'tc-bash-1', content: '🔧 ls /tmp' }))
    expect(state.messages[0].content).toBe('🔧 Terminal')
  })

  it('sseChatMessageUpdate is no-op when tool_call_id is missing', () => {
    let state = reducer(withSlot, appendMessage({ role: 'tool', content: '🔧 Terminal', cls: '', meta: { tool_call_id: 'tc-bash-1' } }))
    state = reducer(state, sseChatMessageUpdate({ slot: 'slot-1', tool_call_id: '', content: '🔧 changed' }))
    expect(state.messages[0].content).toBe('🔧 Terminal')
  })
})

describe('permission cls parsing and approval resolution', () => {
  const slot = 'test-slot'
  const mkState = () => {
    let s = reducer(undefined, { type: '@@INIT' })
    s = reducer(s, setActiveSlot(slot))
    return s
  }

  it('parses cls JSON into meta.approval_id on permission messages', () => {
    const cls = JSON.stringify({ request_id: 'req-42', tool_input: 'echo hi', is_read_only: 'true' })
    const state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls }))
    const msg = state.messages[0]
    expect(msg.role).toBe('permission')
    expect(msg.meta?.approval_id).toBe('req-42')
    expect(msg.meta?.tool_input).toBe('echo hi')
    expect(msg.meta?.is_read_only).toBe('true')
  })

  it('ignores non-JSON cls gracefully', () => {
    const state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls: 'not-json' }))
    expect(state.messages[0].meta?.approval_id).toBeUndefined()
  })

  it('preserves existing meta when cls has no request_id', () => {
    const cls = JSON.stringify({ description: 'some tool' })
    const state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls, meta: { custom: 'val' } }))
    expect(state.messages[0].meta?.custom).toBe('val')
    expect(state.messages[0].meta?.approval_id).toBeUndefined()
  })

  it('skips cls parsing when meta already has approval_id', () => {
    const cls = JSON.stringify({ request_id: 'req-new' })
    const state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls, meta: { approval_id: 'req-existing' } }))
    expect(state.messages[0].meta?.approval_id).toBe('req-existing')
  })

  it('resolveByApprovalId marks permission as approved', () => {
    const cls = JSON.stringify({ request_id: 'req-1' })
    let state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls }))
    state = reducer(state, resolveByApprovalId({ id: 'req-1', decision: 'approved' }))
    expect(state.messages[0].meta?.resolved).toBe('approved')
  })

  it('resolveByApprovalId marks permission as rejected', () => {
    const cls = JSON.stringify({ request_id: 'req-2' })
    let state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls }))
    state = reducer(state, resolveByApprovalId({ id: 'req-2', decision: 'rejected' }))
    expect(state.messages[0].meta?.resolved).toBe('rejected')
  })

  it('resolveByApprovalId is no-op for unknown id', () => {
    const cls = JSON.stringify({ request_id: 'req-3' })
    let state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls }))
    state = reducer(state, resolveByApprovalId({ id: 'req-unknown' }))
    expect(state.messages[0].meta?.resolved).toBeUndefined()
  })

  it('resolved permission is filterable by meta.resolved', () => {
    const cls1 = JSON.stringify({ request_id: 'req-a' })
    const cls2 = JSON.stringify({ request_id: 'req-b' })
    let state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'tool1', cls: cls1 }))
    state = reducer(state, sseChatMessage({ slot, role: 'permission', content: 'tool2', cls: cls2 }))
    state = reducer(state, resolveByApprovalId({ id: 'req-a', decision: 'approved' }))
    const pending = state.messages.filter(m => m.role === 'permission' && !m.meta?.resolved)
    expect(pending).toHaveLength(1)
    expect(pending[0].meta?.approval_id).toBe('req-b')
  })
})


describe('forkSlot thunk', () => {
  it('calls api.forkChatSlot and dispatches addSlotOptimistic on ok response', async () => {
    const { server } = await import('../../integration/mocks/server')
    const { http, HttpResponse } = await import('msw')
    server.use(
      http.post('/api/chat/slots/:slot/fork', () => HttpResponse.json({
        ok: true, key: 'chat-2-123', title: 'Fork of Parent', messages: 3, prompt: '',
      })),
    )

    const { configureStore } = await import('@reduxjs/toolkit')
    const chatSlice = await import('../store/chatSlice')
    const dashboardReducer = (await import('../store/dashboardSlice')).default
    const store = configureStore({ reducer: { chat: chatSlice.default, dashboard: dashboardReducer } })
    const result = await store.dispatch(chatSlice.forkSlot({ slot: 'chat-1-100', atIndex: 2 })).unwrap()
    expect(result).toMatchObject({ ok: true, key: 'chat-2-123' })

    const slots = store.getState().dashboard.slots
    expect(slots).toContainEqual(expect.objectContaining({ key: 'chat-2-123', title: 'Fork of Parent' }))
  })

  it('skips addSlotOptimistic when response.ok is false', async () => {
    const { server } = await import('../../integration/mocks/server')
    const { http, HttpResponse } = await import('msw')
    server.use(
      http.post('/api/chat/slots/:slot/fork', () => HttpResponse.json({ ok: false, error: 'nope' })),
    )

    const { configureStore } = await import('@reduxjs/toolkit')
    const chatSlice = await import('../store/chatSlice')
    const dashboardReducer = (await import('../store/dashboardSlice')).default
    const store = configureStore({ reducer: { chat: chatSlice.default, dashboard: dashboardReducer } })
    const slotsBefore = store.getState().dashboard.slots.length
    await store.dispatch(chatSlice.forkSlot({ slot: 'chat-1-100' }))

    expect(store.getState().dashboard.slots.length).toBe(slotsBefore)
  })
})

describe('slotHistory — session navigation stack', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const switchPending = (arg: string, requestId = 'r1') => ({
    type: 'chat/switchSlot/pending' as const,
    meta: { arg, requestId, requestStatus: 'pending' as const },
  })

  it('initializes slotHistory as empty array', () => {
    expect(initial.slotHistory).toEqual([])
  })

  it('switchSlot.pending pushes current activeSlot onto history', () => {
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, switchPending('B'))
    expect(state.slotHistory).toEqual(['A'])
    expect(state.activeSlot).toBe('B')
  })

  it('builds A→B→C navigation stack', () => {
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, switchPending('B', 'r1'))
    state = reducer(state, switchPending('C', 'r2'))
    expect(state.slotHistory).toEqual(['A', 'B'])
  })

  it('deduplicates: switching back to A removes A from history before pushing current', () => {
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, switchPending('B', 'r1'))
    state = reducer(state, switchPending('A', 'r2'))
    expect(state.slotHistory).toEqual(['B'])
    expect(state.activeSlot).toBe('A')
  })

  it('does not push when activeSlot is null', () => {
    const state = reducer(initial, switchPending('A'))
    expect(state.slotHistory).toEqual([])
  })

  it('does not push when switching to same slot', () => {
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, switchPending('A'))
    expect(state.slotHistory).toEqual([])
  })

  it('createSlot.fulfilled pushes current activeSlot onto history', () => {
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, {
      type: 'chat/createSlot/fulfilled',
      // originActiveSlot === activeSlot ('A'): the create resolved while the
      // user was still on A (fast create / didn't switch away), so the new slot
      // activates normally. only guards the switched-away case.
      meta: { arg: undefined, requestId: 'r1', requestStatus: 'fulfilled' as const, originActiveSlot: 'A' },
      payload: { key: 'new-slot' },
    })
    expect(state.slotHistory).toEqual(['A'])
    expect(state.activeSlot).toBe('new-slot')
  })

  it('deleteSlot.fulfilled cleans deleted key from history', () => {
    let state = { ...initial, activeSlot: 'C', slotHistory: ['A', 'B'] }
    state = reducer(state, {
      type: 'chat/deleteSlot/fulfilled',
      meta: { arg: 'B', requestId: 'r1', requestStatus: 'fulfilled' as const },
      payload: 'B',
    })
    expect(state.slotHistory).toEqual(['A'])
  })

  it('resumeFromHistory.fulfilled pushes activeSlot onto history', () => {
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, {
      type: 'chat/resumeFromHistory/fulfilled',
      meta: { arg: { key: 'H', title: 'old' }, requestId: 'r1', requestStatus: 'fulfilled' as const },
      payload: { ok: true, key: 'H', messages: [], hasMore: false, total: 0 },
    })
    expect(state.activeSlot).toBe('H')
    expect(state.slotHistory).toEqual(['A'])
  })

  it('caps slotHistory at 50 entries', () => {
    let state = { ...initial, activeSlot: 'slot-0' }
    for (let i = 1; i <= 60; i++) {
      state = reducer(state, switchPending(`slot-${i}`, `r${i}`))
    }
    expect(state.slotHistory.length).toBe(50)
  })

  it('full A→B→C→close(C) reducer flow: setActiveSlot(null) then switchSlot(B)', () => {
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, switchPending('B', 'r1'))
    state = reducer(state, switchPending('C', 'r2'))
    expect(state.slotHistory).toEqual(['A', 'B'])

    state = reducer(state, setActiveSlot(null))
    state = reducer(state, switchPending('B', 'r3'))
    expect(state.activeSlot).toBe('B')
    expect(state.slotHistory).not.toContain('C')
    expect(state.slotHistory).not.toContain(null)
    expect(state.slotHistory).not.toContain('B') // invariant: activeSlot ∉ slotHistory

    state = reducer(state, {
      type: 'chat/deleteSlot/fulfilled',
      meta: { arg: 'C', requestId: 'r4', requestStatus: 'fulfilled' as const },
      payload: 'C',
    })
    expect(state.activeSlot).toBe('B')
  })

  it('delete-then-switch-back does not create duplicates', () => {
    let state = { ...initial, activeSlot: 'A' }
    state = reducer(state, switchPending('B', 'r1'))
    state = reducer(state, switchPending('C', 'r2'))
    state = reducer(state, setActiveSlot(null))
    state = reducer(state, switchPending('B', 'r3'))
    state = reducer(state, switchPending('A', 'r4'))
    const bCount = state.slotHistory.filter(k => k === 'B').length
    expect(bCount).toBe(1)
  })

  it('resumeFromHistory removes resumed key from history (invariant: activeSlot ∉ slotHistory)', () => {
    let state = { ...initial, activeSlot: 'A', slotHistory: ['H', 'B'] }
    state = reducer(state, {
      type: 'chat/resumeFromHistory/fulfilled',
      meta: { arg: { key: 'H', title: 'old' }, requestId: 'r1', requestStatus: 'fulfilled' as const },
      payload: { ok: true, key: 'H', messages: [], hasMore: false, total: 0 },
    })
    expect(state.activeSlot).toBe('H')
    expect(state.slotHistory).not.toContain('H')
    expect(state.slotHistory).toContain('A')
  })

  it('clearSlotState resets all slot-related fields to initial values', () => {
    let state = {
      ...initial,
      activeSlot: 'A',
      messages: [{ role: 'user', content: 'hi', cls: '' }] as ChatMessage[],
      toolLog: [{ id: '1' }] as unknown as ToolActivity[],
      subagents: { s1: {} } as unknown as Record<string, SubagentActivity>,
      slotRunning: true,
      slotStopping: true,
      slotState: 'streaming' as const,
      slotHasMore: true,
      slotOldestIndex: 42,
      loadingOlder: true,
      lastChunkSeq: 99,
      _wsChunkedDuringFetch: true,
      slotStatusDetail: { x: { kind: 'tool', text: 'hi', ts: 1 } },
      voicePlaying: true,
      voiceAudio: 'base64data',
    }
    state = reducer(state, { type: 'chat/clearSlotState' })
    expect(state.messages).toEqual([])
    expect(state.toolLog).toEqual([])
    expect(state.subagents).toEqual({})
    expect(state.slotRunning).toBe(false)
    expect(state.slotStopping).toBe(false)
    expect(state.slotState).toBe('idle')
    expect(state.slotHasMore).toBe(false)
    expect(state.slotOldestIndex).toBe(0)
    expect(state.loadingOlder).toBe(false)
    expect(state.lastChunkSeq).toBeUndefined()
    expect(state._wsChunkedDuringFetch).toBe(false)
    expect(state.slotStatusDetail).toEqual({})
    expect(state.voicePlaying).toBe(false)
    expect(state.voiceAudio).toBeNull()
    expect(state.activeSlot).toBe('A')
  })

  it('no same-mode sessions: clearSlotState dispatched instead of switchSlot', () => {
    const slotHistory = ['autopilotA']
    const deletedMode = 'Chat'
    const dashboardSlots = [
      { key: 'chatC', mode: 'Chat' },
      { key: 'autopilotA', mode: 'Autopilot' },
    ]
    const sameMode = new Set(dashboardSlots.filter(s => (s.mode || '') === deletedMode).map(s => s.key))
    const prev = slotHistory.filter(k => k !== 'chatC' && sameMode.has(k)).pop()
      || dashboardSlots.filter(s => s.key !== 'chatC' && sameMode.has(s.key)).map(s => s.key)[0]
    expect(prev).toBeUndefined()

    let state = {
      ...initial,
      activeSlot: null as string | null,
      messages: [{ role: 'user', content: 'stale', cls: '' }] as ChatMessage[],
      toolLog: [{ id: '1' }] as unknown as ToolActivity[],
      slotRunning: true,
    }
    state = reducer(state, { type: 'chat/clearSlotState' })
    expect(state.messages).toEqual([])
    expect(state.toolLog).toEqual([])
    expect(state.slotRunning).toBe(false)
  })

  it('deleteSlot mode isolation: skips cross-mode history entries', () => {
    const slotHistory = ['chatA', 'autopilotB']
    const deletedMode = 'Chat'
    const dashboardSlots = [
      { key: 'chatA', mode: 'Chat' },
      { key: 'autopilotB', mode: 'Autopilot' },
    ]
    const sameMode = new Set(dashboardSlots.filter(s => (s.mode || '') === deletedMode).map(s => s.key))
    const prev = slotHistory.filter(k => k !== 'chatC' && sameMode.has(k)).pop()
    expect(prev).toBe('chatA')

    let state = { ...initial, activeSlot: 'chatC', slotHistory: ['chatA', 'autopilotB'] }
    state = reducer(state, setActiveSlot(null))
    state = reducer(state, switchPending('chatA', 'r1'))
    expect(state.activeSlot).toBe('chatA')
    expect(state.slotHistory).not.toContain('chatA')
  })
})

describe('sseChatMessagePatchByTs', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  // Build a slot state with one mcp_oauth banner already appended.
  function withMcpOauthBanner(activeSlot: string | null = 'slot-1') {
    const ts = '2026-05-28T01:00:00.000Z'
    const banner = {
      role: 'mcp_oauth',
      content: '🔐 linear requires authentication.',
      cls: 'msg msg-info',
      ts,
      meta: { server_name: 'linear', oauth_url: 'https://mcp.linear.app/authorize' },
    }
    return {
      state: {
        ...initial,
        activeSlot,
        messages: activeSlot === 'slot-1' ? [banner] as ChatMessage[] : [],
        slotMessages: { 'slot-1': [banner] as ChatMessage[] },
      },
      ts,
    }
  }

  it('patches the active slot messages array (success transition)', () => {
    const { state, ts } = withMcpOauthBanner('slot-1')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: {
        slot: 'slot-1',
        ts,
        meta: { server_name: 'linear', completed: true },
        content: '🔓 linear authenticated.',
      },
    })
    expect(out.messages[0].content).toBe('🔓 linear authenticated.')
    expect(out.messages[0].meta).toMatchObject({ completed: true, server_name: 'linear' })
    // slotMessages cache also updated.
    expect(out.slotMessages['slot-1'][0].meta).toMatchObject({ completed: true })
  })

  it('patches a slot the user is NOT currently viewing (slotMessages cache only)', () => {
    // The active slot is "other", but the update is for "slot-1". We still
    // patch slotMessages so the user sees the right state after switching back.
    const { state, ts } = withMcpOauthBanner('other')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: {
        slot: 'slot-1',
        ts,
        meta: { server_name: 'linear', completed: true },
        content: '🔓 linear authenticated.',
      },
    })
    // Active messages array (= 'other') is untouched.
    expect(out.messages).toEqual([])
    // Cached messages for slot-1 reflect the patched state.
    expect(out.slotMessages['slot-1'][0].meta).toMatchObject({ completed: true })
    expect(out.slotMessages['slot-1'][0].content).toBe('🔓 linear authenticated.')
  })

  it('merges meta — keeps existing keys, adds new ones', () => {
    const { state, ts } = withMcpOauthBanner('slot-1')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: {
        slot: 'slot-1',
        ts,
        meta: { failed: true, error: 'dns failed' },
        content: '🚫 linear authentication failed.',
      },
    })
    // server_name preserved; failed + error added.
    expect(out.messages[0].meta).toMatchObject({
      server_name: 'linear',
      failed: true,
      error: 'dns failed',
    })
  })

  it('no-op when ts does not match any message', () => {
    const { state } = withMcpOauthBanner('slot-1')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: {
        slot: 'slot-1',
        ts: '2099-01-01T00:00:00.000Z',
        meta: { completed: true },
      },
    })
    // Original banner unchanged.
    expect(out.messages[0].meta).toEqual({
      server_name: 'linear',
      oauth_url: 'https://mcp.linear.app/authorize',
    })
  })

  it('no-op when ts is empty', () => {
    const { state } = withMcpOauthBanner('slot-1')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: { slot: 'slot-1', ts: '', meta: { completed: true } },
    })
    expect(out.messages[0].meta?.completed).toBeUndefined()
  })

  it('no-op when slot is empty', () => {
    const { state, ts } = withMcpOauthBanner('slot-1')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: { slot: '', ts, meta: { completed: true } },
    })
    expect(out.messages[0].meta?.completed).toBeUndefined()
  })

  it('content-only update leaves meta untouched', () => {
    const { state, ts } = withMcpOauthBanner('slot-1')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: { slot: 'slot-1', ts, content: 'changed' },
    })
    expect(out.messages[0].content).toBe('changed')
    // meta preserved as-is.
    expect(out.messages[0].meta).toEqual({
      server_name: 'linear',
      oauth_url: 'https://mcp.linear.app/authorize',
    })
  })
})

describe('sseContextUsage reducer', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('stores pct and token counts when window is known', () => {
    const state = reducer(initial, sseContextUsage({ slot: 's1', pct: 44, used_tokens: 88000, window_tokens: 200000 }))
    expect(state.slotContextPct['s1']).toBe(44)
    expect(state.slotContextTokens['s1']).toEqual({ used: 88000, window: 200000 })
  })

  it('stores pct only and leaves tokens untouched when window is 0/absent', () => {
    const state = reducer(initial, sseContextUsage({ slot: 's1', pct: 9 }))
    expect(state.slotContextPct['s1']).toBe(9)
    expect(state.slotContextTokens['s1']).toBeUndefined()
    const zero = reducer(initial, sseContextUsage({ slot: 's1', pct: 9, used_tokens: 5, window_tokens: 0 }))
    expect(zero.slotContextTokens['s1']).toBeUndefined()
  })

  it('falls back to used:0 when used_tokens omitted but window present', () => {
    const state = reducer(initial, sseContextUsage({ slot: 's1', pct: 10, window_tokens: 200000 }))
    expect(state.slotContextTokens['s1']).toEqual({ used: 0, window: 200000 })
  })

  it('reset with a window replaces the stored entry (live model switch)', () => {
    const seeded = reducer(initial, sseContextUsage({ slot: 's1', pct: 10, used_tokens: 100000, window_tokens: 1000000 }))
    const state = reducer(seeded, sseContextUsage({ slot: 's1', pct: 36.8, used_tokens: 100000, window_tokens: 272000, reset: true }))
    expect(state.slotContextTokens['s1']).toEqual({ used: 100000, window: 272000 })
    expect(state.slotContextPct['s1']).toBe(36.8)
  })

  it('reset without a window deletes the stored entry (session reset / compaction)', () => {
    // Deleting re-enables the model-derived fallback for the slot's NEW model;
    // without reset the stale old-model entry short-circuits it until the next turn.
    //
    // #1645: this is the frontend half of the "225K used / 0%" bug. After
    // /compact the backend zeroes `used` but keeps the window, so it emits a
    // reset frame here. Honouring it deletes the pre-compaction token entry so
    // the ring can never show a stale count beside the freshly-reset 0%.
    const seeded = reducer(initial, sseContextUsage({ slot: 's1', pct: 10, used_tokens: 225000, window_tokens: 1000000 }))
    const state = reducer(seeded, sseContextUsage({ slot: 's1', pct: 0, reset: true }))
    expect(state.slotContextTokens['s1']).toBeUndefined()
    expect(state.slotContextPct['s1']).toBe(0)
  })

  it('pct-only event WITHOUT reset still leaves stored tokens untouched', () => {
    const seeded = reducer(initial, sseContextUsage({ slot: 's1', pct: 10, used_tokens: 100000, window_tokens: 1000000 }))
    const state = reducer(seeded, sseContextUsage({ slot: 's1', pct: 12 }))
    expect(state.slotContextTokens['s1']).toEqual({ used: 100000, window: 1000000 })
  })
})

describe('sseSideResult — side conversation reducer', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('assistant chunks accumulate as deltas under same run_id', () => {
    let state = reducer(initial, sseSideResult({ slot: 'slot-1', run_id: 'r1', role: 'user', content: 'hi' }))
    state = reducer(state, sseSideResult({ slot: 'slot-1', run_id: 'r1', role: 'assistant', content: 'Hello' }))
    state = reducer(state, sseSideResult({ slot: 'slot-1', run_id: 'r1', role: 'assistant', content: ' world' }))
    expect(state.slotSide['slot-1'].messages).toHaveLength(2)
    expect(state.slotSide['slot-1'].messages[1].content).toBe('Hello world')
    expect(state.slotSide['slot-1'].lastRunId).toBe('r1')
  })

  it('new run_id starts a fresh assistant message', () => {
    let state = reducer(initial, sseSideResult({ slot: 'slot-1', run_id: 'r1', role: 'assistant', content: 'first' }))
    state = reducer(state, sseSideResult({ slot: 'slot-1', run_id: 'r2', role: 'user', content: 'q2' }))
    state = reducer(state, sseSideResult({ slot: 'slot-1', run_id: 'r2', role: 'assistant', content: 'second' }))
    expect(state.slotSide['slot-1'].messages).toHaveLength(3)
    expect(state.slotSide['slot-1'].lastRunId).toBe('r2')
  })

  it('sideClose drops per-slot side state', () => {
    let state = reducer(initial, sseSideResult({ slot: 'slot-1', run_id: 'r1', role: 'user', content: 'q' }))
    state = reducer(state, sseSideResult({ slot: 'slot-2', run_id: 'r2', role: 'user', content: 'q2' }))
    state = reducer(state, sideClose('slot-1'))
    expect(state.slotSide['slot-1']).toBeUndefined()
    expect(state.slotSide['slot-2']).toBeDefined()
  })
})

describe('sseThinkingChunk (model reasoning)', () => {
  const base = reducer(undefined, { type: '@@INIT' })
  const active = reducer(base, setActiveSlot('chat-1'))

  it('creates a content-bearing thinking message', () => {
    const state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'Let me think' }))
    const thinking = state.messages.filter(m => m.role === 'thinking')
    expect(thinking).toHaveLength(1)
    expect(thinking[0].content).toBe('Let me think')
  })

  it('accumulates into a single thinking message within a turn', () => {
    let state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'Step 1. ' }))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'Step 2.' }))
    const thinking = state.messages.filter(m => m.role === 'thinking')
    expect(thinking).toHaveLength(1)
    expect(thinking[0].content).toBe('Step 1. Step 2.')
  })

  it('ignores chunks for a non-active slot', () => {
    const state = reducer(active, sseThinkingChunk({ slot: 'other', content: 'nope' }))
    expect(state.messages).toHaveLength(0)
  })

  it('ignores empty content', () => {
    const state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: '' }))
    expect(state.messages).toHaveLength(0)
  })

  it('starts a fresh reasoning block in a new turn (after a user message)', () => {
    let state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'first turn reasoning' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'user', content: 'follow up', ts: '' }))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'second turn reasoning' }))
    const thinking = state.messages.filter(m => m.role === 'thinking')
    expect(thinking).toHaveLength(2)
    expect(thinking[1].content).toBe('second turn reasoning')
  })

  it('chat_chunk preserves a content-bearing reasoning block', () => {
    let state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'reasoning text' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'answer', seq: 0 }))
    const thinking = state.messages.filter(m => m.role === 'thinking')
    const streaming = state.messages.filter(m => m.role === 'streaming')
    expect(thinking).toHaveLength(1)
    expect(thinking[0].content).toBe('reasoning text')
    expect(streaming).toHaveLength(1)
  })

  it('chat_chunk still drops an empty thinking placeholder', () => {
    let state = reducer(active, appendMessage({ role: 'thinking', content: '', cls: '' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'answer', seq: 0 }))
    expect(state.messages.filter(m => m.role === 'thinking')).toHaveLength(0)
  })
})

describe('thinking survives refreshSlot (client-only reasoning)', () => {
  const base = reducer(undefined, { type: '@@INIT' })

  const refreshPayload = (key: string, messages: { role: string; content: string; cls?: string; ts?: string }[]) => ({
    key, messages, running: false, hasMore: false, total: messages.length, stopping: false,
  })

  it('re-inserts the reasoning block before its assistant after refresh', () => {
    let state = reducer(base, setActiveSlot('chat-1'))
    // model reasons, then answers
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'because X then Y' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'The answer', seq: 0 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: '_done', content: '' }))
    expect(state.messages.filter(m => m.role === 'thinking')).toHaveLength(1)

    // server refresh carries only the persisted user/assistant (no thinking)
    state = reducer(state, refreshSlot.fulfilled(
      refreshPayload('chat-1', [{ role: 'assistant', content: 'The answer', cls: 'msg msg-a' }]),
      'req', 'chat-1',
    ))

    const thinking = state.messages.filter(m => m.role === 'thinking')
    expect(thinking).toHaveLength(1)
    expect(thinking[0].content).toBe('because X then Y')
    // anchored directly before its assistant
    const ti = state.messages.findIndex(m => m.role === 'thinking')
    const ai = state.messages.findIndex(m => m.role === 'assistant')
    expect(ti).toBeGreaterThanOrEqual(0)
    expect(ti).toBe(ai - 1)
  })

  it('does not duplicate the reasoning block across successive refreshes', () => {
    let state = reducer(base, setActiveSlot('chat-1'))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'reasoning' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'Answer', seq: 0 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: '_done', content: '' }))
    const payload = refreshPayload('chat-1', [{ role: 'assistant', content: 'Answer', cls: 'msg msg-a' }])
    state = reducer(state, refreshSlot.fulfilled(payload, 'r1', 'chat-1'))
    state = reducer(state, refreshSlot.fulfilled(payload, 'r2', 'chat-1'))
    expect(state.messages.filter(m => m.role === 'thinking')).toHaveLength(1)
  })
})

describe('streaming chunk coalescing (batched flag)', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const active = reducer(initial, setActiveSlot('chat-1'))

  it('batched chunk appends content without inserting a missed-chunk marker on a seq jump', () => {
    // The useWebSocket flush buffer owns gap detection across the chunks it
    // merges, so the reducer must NOT re-derive a gap from the batch seq.
    let state = reducer(active, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'Hello ', seq: 1, batched: true }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'world', seq: 5, batched: true }))
    const streaming = state.messages.find(m => m.role === 'streaming')
    expect(streaming?.content).toBe('Hello world')
    expect(streaming?.content).not.toContain('chunk(s) missed')
    expect(state.lastChunkSeq).toBe(5)
  })

  it('non-batched chunk still inserts a missed-chunk marker on a seq jump (behavior unchanged)', () => {
    let state = reducer(active, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'Hello ', seq: 1 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'world', seq: 5 }))
    const streaming = state.messages.find(m => m.role === 'streaming')
    expect(streaming?.content).toContain('chunk(s) missed')
  })
})

describe('warmSlotCache (background cache warm)', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const warmPayload = (key: string, messages: unknown[]) => ({
    key, messages, running: false, stopping: false, hasMore: false, total: messages.length, queue: [],
  })

  it('writes only slotMessages[key] for a background slot and leaves the active view untouched', () => {
    const state0 = reducer(initial, setActiveSlot('chat-1'))
    const msgs = [{ role: 'assistant', content: 'background answer', cls: 'msg msg-a' }]
    const state = reducer(state0, warmSlotCache.fulfilled(warmPayload('chat-2', msgs), 'w1', 'chat-2'))
    expect(state.slotMessages['chat-2']).toEqual(msgs)
    expect(state.messages).toEqual(state0.messages)
  })

  it('skips the cache write if the slot became active before fulfilment', () => {
    const state0 = reducer(initial, setActiveSlot('chat-2'))
    const state = reducer(state0, warmSlotCache.fulfilled(warmPayload('chat-2', [{ role: 'assistant', content: 'x', cls: '' }]), 'w1', 'chat-2'))
    expect(state.slotMessages['chat-2']).toBeUndefined()
  })

  it('ignores a null payload (slot was already active at dispatch time)', () => {
    const state0 = reducer(initial, setActiveSlot('chat-1'))
    const state = reducer(state0, warmSlotCache.fulfilled(null, 'w1', 'chat-1'))
    expect(state.slotMessages).toEqual(state0.slotMessages)
  })
})

describe('queue edit reducers', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withQueued = () => {
    let state = reducer(initial, setActiveSlot('chat-1'))
    state = reducer(state, appendQueuedMessage({ slot: 'chat-1', content: 'first', ts: 't1', queue_id: 'q1' }))
    state = reducer(state, appendQueuedMessage({ slot: 'chat-1', content: 'second', ts: 't2', queue_id: 'q2' }))
    return state
  }

  it('editQueuedMessage updates the matching queued message in place', () => {
    let state = withQueued()
    state = reducer(state, editQueuedMessage({ slot: 'chat-1', queue_id: 'q2', content: 'second edited' }))
    const queued = state.messages.filter(m => m.role === 'queued')
    expect(queued.map(m => m.content)).toEqual(['first', 'second edited'])
    // Order and ids preserved
    expect(queued.map(m => m.meta?.queueId)).toEqual(['q1', 'q2'])
  })

  it('editQueuedMessage is a no-op for an unknown queue_id', () => {
    let state = withQueued()
    state = reducer(state, editQueuedMessage({ slot: 'chat-1', queue_id: 'nope', content: 'x' }))
    expect(state.messages.filter(m => m.role === 'queued').map(m => m.content)).toEqual(['first', 'second'])
  })

  it('editQueuedMessage ignores events for a non-active slot', () => {
    let state = withQueued()
    state = reducer(state, editQueuedMessage({ slot: 'other-slot', queue_id: 'q1', content: 'hijack' }))
    expect(state.messages.filter(m => m.role === 'queued').map(m => m.content)).toEqual(['first', 'second'])
  })

  it('editQueuedMessage does not touch a cancelled message', () => {
    let state = withQueued()
    state = reducer(state, cancelQueuedMessage({ slot: 'chat-1', queue_id: 'q1' }))
    state = reducer(state, editQueuedMessage({ slot: 'chat-1', queue_id: 'q1', content: 'ghost' }))
    expect(state.messages.filter(m => m.role === 'queued').map(m => m.content)).toEqual(['second'])
  })
})

describe('creatingSlot — New Chat pending flag', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const pending = { type: 'chat/createSlot/pending', meta: { arg: undefined, requestId: 'r1', requestStatus: 'pending' as const } }
  const rejected = { type: 'chat/createSlot/rejected', meta: { arg: undefined, requestId: 'r1', requestStatus: 'rejected' as const }, error: { message: 'boom' } }
  const fulfilled = { type: 'chat/createSlot/fulfilled', meta: { arg: undefined, requestId: 'r1', requestStatus: 'fulfilled' as const }, payload: { key: 'new-slot' } }

  it('defaults to false', () => {
    expect(initial.creatingSlot).toBe(false)
  })

  it('createSlot.pending sets creatingSlot true', () => {
    expect(reducer(initial, pending).creatingSlot).toBe(true)
  })

  it('createSlot.fulfilled clears creatingSlot', () => {
    let state = reducer(initial, pending)
    expect(state.creatingSlot).toBe(true)
    state = reducer(state, fulfilled)
    expect(state.creatingSlot).toBe(false)
  })

  it('createSlot.rejected clears creatingSlot (button never stuck)', () => {
    let state = reducer(initial, pending)
    expect(state.creatingSlot).toBe(true)
    state = reducer(state, rejected)
    expect(state.creatingSlot).toBe(false)
  })
})

describe('selectSlotSubagentsActive', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }
  const wrap = (chat: ReturnType<typeof reducer>) => ({ chat }) as never

  it('is false with no subagents', () => {
    expect(selectSlotSubagentsActive(wrap(withSlot), 'slot-1')).toBe(false)
  })

  it('is true while a subagent runs on the active slot (spawn event)', () => {
    const state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 't', agent: '' }))
    expect(selectSlotSubagentsActive(wrap(state), 'slot-1')).toBe(true)
  })

  it('is true for a pending subagent (awaiting spawn approval)', () => {
    const state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: 't', approval_id: 'spawn:a1' }))
    expect(selectSlotSubagentsActive(wrap(state), 'slot-1')).toBe(true)
  })

  it('clears when the subagent finishes (done event — reaper self-heal path)', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 't', agent: '' }))
    state = reducer(state, sseSubagentDone({ slot: 'slot-1', id: 'a1', elapsed: 1 }))
    expect(selectSlotSubagentsActive(wrap(state), 'slot-1')).toBe(false)
  })

  it('reads background slots from slotActivity, not the active-slot map', () => {
    const state = reducer(withSlot, sseSubagentSpawn({ slot: 'bg-slot', id: 'b1', task: 't', agent: '' }))
    expect(selectSlotSubagentsActive(wrap(state), 'bg-slot')).toBe(true)
    expect(selectSlotSubagentsActive(wrap(state), 'slot-1')).toBe(false)
  })
})

// Single source of truth for the composer busy/queue rule — shared by ChatPage
// (main route) and ChatPane (split view). Busy = per-slot stream state OR the
// global active-slot running flag OR either sub-agent signal (live WS-derived
// OR slots-stream snapshot). Conservative OR of every input the two routes
// previously computed separately.
describe('selectComposerBusy', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }
  const wrap = (chat: ReturnType<typeof reducer>, slots: Array<{ key: string; subagents_running?: boolean; orchestrating?: boolean }> = []) =>
    ({ chat, dashboard: { slots } }) as never

  it('is idle when nothing runs', () => {
    expect(selectComposerBusy(wrap(withSlot), 'slot-1')).toBe(false)
  })

  it('is busy while the main turn streams (per-slot stream state)', () => {
    expect(selectComposerBusy(wrap({ ...withSlot, slotState: 'streaming' }), 'slot-1')).toBe(true)
  })

  it('is busy on the global running flag for the active slot', () => {
    expect(selectComposerBusy(wrap({ ...withSlot, slotRunning: true }), 'slot-1')).toBe(true)
  })

  it('is busy on the live WS sub-agent signal even when the main turn is idle', () => {
    // Main idle but sub-agents running must still queue.
    const state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 't', agent: '' }))
    expect(selectComposerBusy(wrap(state), 'slot-1')).toBe(true)
  })

  it('is busy on the snapshot field alone (first frames after reload)', () => {
    expect(selectComposerBusy(wrap(withSlot, [{ key: 'slot-1', subagents_running: true }]), 'slot-1')).toBe(true)
  })

  it('is busy while an autopilot plan is orchestrating (queues mid-plan messages)', () => {
    // slot.running reads False between stages, but a mid-plan message must still
    // queue as a chip rather than render an optimistic bubble.
    expect(selectComposerBusy(wrap(withSlot, [{ key: 'slot-1', orchestrating: true }]), 'slot-1')).toBe(true)
  })

  it('clears when the subagent finishes (done event — reaper self-heal path)', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 't', agent: '' }))
    state = reducer(state, sseSubagentDone({ slot: 'slot-1', id: 'a1', elapsed: 1 }))
    expect(selectComposerBusy(wrap(state, [{ key: 'slot-1', subagents_running: false }]), 'slot-1')).toBe(false)
  })

  it('falls back to the global running flag when slot is null', () => {
    expect(selectComposerBusy(wrap({ ...withSlot, slotRunning: true }), null)).toBe(true)
    expect(selectComposerBusy(wrap(withSlot), null)).toBe(false)
  })
})

// a slow createSlot (backend round-trip under memory pressure) must
// not hijack the view if the user switched to another session while it was
// pending. Mirrors the switched-away guard every other async thunk already has
// (switchSlot/refreshSlot/warmSlotCache). Without the guard, createSlot.fulfilled
// unconditionally reassigns activeSlot + clears messages, stealing the tab the
// user is now typing in ("New Chat copies my text into the new chat").
describe('createSlot.fulfilled switched-away guard', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  // origin is the activeSlot captured when the create was dispatched; the
  // reducer carries it in action.meta (fulfillWithValue), not the payload.
  const fulfilled = (key: string, origin: string | null) => ({
    type: 'chat/createSlot/fulfilled',
    meta: { arg: undefined, requestId: 'r1', requestStatus: 'fulfilled' as const, originActiveSlot: origin },
    payload: { key, title: key, messages: 0, running: false },
  })

  it('activates the new slot when the user has NOT switched away (normal case)', () => {
    // No slot is active yet (empty New Chat from the welcome screen): origin is
    // null and still matches activeSlot, so the fresh slot becomes active.
    const state = reducer(initial, fulfilled('new-slot', null))
    expect(state.activeSlot).toBe('new-slot')
    expect(state.messages).toEqual([])
  })

  it('does NOT steal activeSlot if the user switched to another slot while the create was pending', () => {
    // User is looking at (and typing into) slot-b when a slow New Chat resolves.
    // The create was dispatched from the welcome screen (origin null), so it no
    // longer matches the now-active slot-b.
    const busy = {
      ...initial,
      activeSlot: 'slot-b',
      messages: [{ role: 'user' as const, content: 'text I typed into slot-b', cls: '' }],
    }
    const state = reducer(busy, fulfilled('new-slot', null))
    // The view stays on slot-b; the just-created slot must not hijack it.
    expect(state.activeSlot).toBe('slot-b')
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('text I typed into slot-b')
  })

  it('does not clobber the active slot activity when a late create resolves', () => {
    const busy = {
      ...initial,
      activeSlot: 'slot-b',
      toolLog: [{ type: 'tool' as const, text: 'read', ts: 1 }],
    }
    const state = reducer(busy, fulfilled('new-slot', null))
    expect(state.activeSlot).toBe('slot-b')
    expect(state.toolLog).toHaveLength(1)
  })

  it('clears the creatingSlot pending flag even when it does not activate (switched away)', () => {
    // The "Creating…" spinner must not stay stuck on after a switched-away create.
    const busy = { ...initial, activeSlot: 'slot-b', creatingSlot: true }
    const state = reducer(busy, fulfilled('new-slot', null))
    expect(state.activeSlot).toBe('slot-b')
    expect(state.creatingSlot).toBe(false)
  })

  it('does not pollute Object.prototype when a slot key is __proto__ (isUnsafeKey guard)', () => {
    // A crafted WS payload carrying slot="__proto__" must NOT reach the shared
    // prototype through the per-slot state maps. The reducer's isUnsafeKey()
    // early-return drops the frame entirely, so nothing is written for a hostile
    // key and Object.prototype stays clean.
    const proto = Object.prototype as Record<string, unknown>
    const msg: ChatMessage = { role: 'user', content: 'pwned', cls: '' }
    const state = reducer(
      { ...initial, activeSlot: 'other' },
      appendSlotMessage({ slot: '__proto__', message: msg }),
    )
    // Object.prototype was not extended, and no fresh object inherits slot data.
    expect(({} as Record<string, unknown>).content).toBeUndefined()
    expect(proto.content).toBeUndefined()
    // The hostile frame was dropped: no own-property was created under either the
    // raw '__proto__' key or the sanitized fallback.
    expect(Object.prototype.hasOwnProperty.call(state.slotMessages, '__proto__')).toBe(false)
    expect(state.slotMessages['unsafe-key:__proto__']).toBeUndefined()
  })

  it('handles a real slot key normally (isUnsafeKey guard does not over-block)', () => {
    // Confidence check: a legitimate slot key still writes through — the guard
    // only trips on __proto__/constructor/prototype.
    const msg: ChatMessage = { role: 'user', content: 'hi', cls: '' }
    const state = reducer(
      { ...initial, activeSlot: 'other' },
      appendSlotMessage({ slot: 'chat-7', message: msg }),
    )
    expect(state.slotMessages['chat-7']).toBeDefined()
    expect(state.slotMessages['chat-7'].at(-1)?.content).toBe('hi')
  })
})

describe('selectSlotPendingSpawnApprovals', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }
  const wrap = (chat: typeof initial) => ({ chat }) as any

  it('returns pending spawn approvals for the active slot', () => {
    const state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: 'do stuff', approval_id: 'spawn:a1' }))
    const pending = selectSlotPendingSpawnApprovals(wrap(state), 'slot-1')
    expect(pending).toHaveLength(1)
    expect(pending[0].id).toBe('a1')
    expect(pending[0].approval_id).toBe('spawn:a1')
  })

  it('is empty (stable ref) when the slot has no pending spawns', () => {
    const a = selectSlotPendingSpawnApprovals(wrap(withSlot), 'slot-1')
    const b = selectSlotPendingSpawnApprovals(wrap(withSlot), 'slot-1')
    expect(a).toHaveLength(0)
    expect(a).toBe(b) // referentially stable so shallowEqual short-circuits re-renders
  })

  it('returns empty for a null slot', () => {
    expect(selectSlotPendingSpawnApprovals(wrap(withSlot), null)).toHaveLength(0)
  })

  it('drops the approval once the sub-agent starts running', () => {
    let state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: 't', approval_id: 'spawn:a1' }))
    expect(selectSlotPendingSpawnApprovals(wrap(state), 'slot-1')).toHaveLength(1)
    state = reducer(state, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 't', agent: 'kirocrew' }))
    expect(selectSlotPendingSpawnApprovals(wrap(state), 'slot-1')).toHaveLength(0)
  })

  it('surfaces pending spawns parked under a background (non-active) slot', () => {
    // Pending card for a slot the user is NOT currently viewing lands in
    // slotActivity[slot]; the selector must still find it when queried by slot.
    const state = reducer(withSlot, sseSubagentPending({ slot: 'bg-slot', id: 'a2', task: 't', approval_id: 'spawn:a2' }))
    const pending = selectSlotPendingSpawnApprovals(wrap(state), 'bg-slot')
    expect(pending).toHaveLength(1)
    expect(pending[0].id).toBe('a2')
  })
})


describe('steer does not deadlock pending approval (#1667)', () => {
  const slot = 'slot-1'
  const initial = reducer(undefined, { type: '@@INIT' })
  const wrap = (chat: ReturnType<typeof reducer>) => ({ chat }) as never

  // Builds a state with an active slot, a pending permission row, and a toolLog entry.
  const withPendingApproval = () => {
    let state = { ...initial, activeSlot: slot }
    // Inject a permission row (active-slot path via sseChatMessage)
    const cls = JSON.stringify({ request_id: 'req-1', tool_input: 'rm -rf /', is_read_only: '' })
    state = reducer(state, sseChatMessage({ slot, role: 'permission', content: 'approve rm?', cls }))
    // Add a tool activity entry so toolLog is non-empty
    state = reducer(state, sseToolActivity({ slot, tool: 'bash', kind: 'write', purpose: 'delete', input_preview: 'rm -rf' }))
    return state
  }

  describe('sseChatMessage (active-slot path)', () => {
    it('steered user message does NOT auto-resolve pending permission rows', () => {
      let state = withPendingApproval()
      expect(state.messages.find(m => m.role === 'permission')?.meta?.resolved).toBeUndefined()
      state = reducer(state, sseChatMessage({ slot, role: 'user', content: 'also check /tmp', meta: { steer: true } }))
      // Permission must remain unresolved
      expect(state.messages.find(m => m.role === 'permission')?.meta?.resolved).toBeUndefined()
    })

    it('steered user message does NOT clear the toolLog', () => {
      let state = withPendingApproval()
      expect(state.toolLog.length).toBeGreaterThan(0)
      state = reducer(state, sseChatMessage({ slot, role: 'user', content: 'also check /tmp', meta: { steer: true } }))
      expect(state.toolLog.length).toBeGreaterThan(0)
    })

    it('normal user message STILL auto-resolves pending permissions (existing behavior)', () => {
      let state = withPendingApproval()
      state = reducer(state, sseChatMessage({ slot, role: 'user', content: 'new turn' }))
      expect(state.messages.find(m => m.role === 'permission')?.meta?.resolved).toBe('rejected')
    })

    it('normal user message STILL clears the toolLog (existing behavior)', () => {
      let state = withPendingApproval()
      state = reducer(state, sseChatMessage({ slot, role: 'user', content: 'new turn' }))
      expect(state.toolLog).toHaveLength(0)
    })
  })

  describe('applyNonActiveFrame (background-slot path)', () => {
    const bgSlot = 'bg-slot'

    const withBgPendingApproval = () => {
      // Active slot is different from bgSlot so bgSlot hits applyNonActiveFrame
      let state = { ...initial, activeSlot: slot }
      const cls = JSON.stringify({ request_id: 'req-bg', tool_input: 'drop db', is_read_only: '' })
      state = reducer(state, sseChatMessage({ slot: bgSlot, role: 'permission', content: 'approve drop?', cls }))
      return state
    }

    it('steered user message does NOT auto-resolve permission rows in background slot', () => {
      let state = withBgPendingApproval()
      const bgMsgs = () => state.slotMessages[bgSlot] ?? []
      expect(bgMsgs().find(m => m.role === 'permission')?.meta?.resolved).toBeUndefined()
      state = reducer(state, sseChatMessage({ slot: bgSlot, role: 'user', content: 'steer correction', meta: { steer: true } }))
      expect(bgMsgs().find(m => m.role === 'permission')?.meta?.resolved).toBeUndefined()
    })

    it('steered user message does NOT clear the background slot toolLog', () => {
      let state = withBgPendingApproval()
      // Add a tool log entry in the background slot
      state = reducer(state, sseToolActivity({ slot: bgSlot, tool: 'grep', kind: 'read', purpose: '', input_preview: '' }))
      expect(state.slotActivity[bgSlot]?.toolLog.length).toBeGreaterThan(0)
      state = reducer(state, sseChatMessage({ slot: bgSlot, role: 'user', content: 'steer', meta: { steer: true } }))
      expect(state.slotActivity[bgSlot]?.toolLog.length).toBeGreaterThan(0)
    })

    it('normal user message STILL auto-resolves permissions in background slot', () => {
      let state = withBgPendingApproval()
      state = reducer(state, sseChatMessage({ slot: bgSlot, role: 'user', content: 'new turn' }))
      const bgMsgs = state.slotMessages[bgSlot] ?? []
      expect(bgMsgs.find(m => m.role === 'permission')?.meta?.resolved).toBe('rejected')
    })

    it('normal user message STILL clears the background slot toolLog', () => {
      let state = withBgPendingApproval()
      state = reducer(state, sseToolActivity({ slot: bgSlot, tool: 'grep', kind: 'read', purpose: '', input_preview: '' }))
      expect(state.slotActivity[bgSlot]?.toolLog.length).toBeGreaterThan(0)
      state = reducer(state, sseChatMessage({ slot: bgSlot, role: 'user', content: 'new turn' }))
      expect(state.slotActivity[bgSlot]?.toolLog).toHaveLength(0)
    })
  })

  describe('selectSlotPendingApproval ignores steered user messages', () => {
    it('returns the pending approval even after a steered user message is appended', () => {
      let state = withPendingApproval()
      // Selector should find the permission row
      expect(selectSlotPendingApproval(wrap(state), slot)).not.toBeNull()
      expect(selectSlotPendingApproval(wrap(state), slot)?.meta?.approval_id).toBe('req-1')
      // Append a steered user message
      state = reducer(state, sseChatMessage({ slot, role: 'user', content: 'also try X', meta: { steer: true } }))
      // Selector must STILL find the pending permission
      expect(selectSlotPendingApproval(wrap(state), slot)).not.toBeNull()
      expect(selectSlotPendingApproval(wrap(state), slot)?.meta?.approval_id).toBe('req-1')
    })

    it('a normal user message hides the pending approval (existing behavior)', () => {
      let state = withPendingApproval()
      expect(selectSlotPendingApproval(wrap(state), slot)).not.toBeNull()
      state = reducer(state, sseChatMessage({ slot, role: 'user', content: 'new turn' }))
      // The permission gets resolved AND is now before the last user msg
      expect(selectSlotPendingApproval(wrap(state), slot)).toBeNull()
    })

    it('works with appendSlotMessage (optimistic steer bubble)', () => {
      let state = withPendingApproval()
      expect(selectSlotPendingApproval(wrap(state), slot)).not.toBeNull()
      state = reducer(state, appendSlotMessage({ slot, message: { role: 'user', content: 'steer text', cls: 'msg msg-u', meta: { steer: true, optimistic: true } } }))
      // Approval must still be visible
      expect(selectSlotPendingApproval(wrap(state), slot)).not.toBeNull()
      expect(selectSlotPendingApproval(wrap(state), slot)?.meta?.approval_id).toBe('req-1')
    })
  })
})
