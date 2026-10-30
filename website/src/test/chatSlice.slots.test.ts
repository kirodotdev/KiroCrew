/**
 * chatSlice: a slot's own lifecycle -- forking, the session history stack,
 * background cache warming, creating a slot and which create activated it, and
 * the queue and composer state a slot carries. Switching slots is in
 * chatSlice.test.ts.
 */
import { describe, it, expect } from 'vitest'
import type { ChatMessage, SubagentActivity, ToolActivity } from '../types'
import { reduceResume } from './resumeLanded'
import reducer, {
  setActiveSlot,
  appendMessage,
  appendSlotMessage,
  warmSlotCache,
  sseSubagentSpawn,
  sseSubagentDone,
  appendQueuedMessage,
  editQueuedMessage,
  reorderQueuedMessages,
  cancelQueuedMessage,
  selectComposerBusy,
} from '../store/chatSlice'
import './mockApiClient'

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

  it('a fork the server reports pinned lands in the pinned group before the slots refresh', async () => {
    const { server } = await import('../../integration/mocks/server')
    const { http, HttpResponse } = await import('msw')
    server.use(
      http.post('/api/chat/slots/:slot/fork', () => HttpResponse.json({
        ok: true, key: 'chat-3-123', title: 'Fork of Pinned', messages: 2, prompt: '', pinned: true,
      })),
    )

    const { configureStore } = await import('@reduxjs/toolkit')
    const chatSlice = await import('../store/chatSlice')
    const dashboardReducer = (await import('../store/dashboardSlice')).default
    const store = configureStore({ reducer: { chat: chatSlice.default, dashboard: dashboardReducer } })
    await store.dispatch(chatSlice.forkSlot({ slot: 'chat-1-100' })).unwrap()

    const row = store.getState().dashboard.slots.find(s => s.key === 'chat-3-123')
    expect(row?.pinned).toBe(true)
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

  it('createSlot.fulfilled starts the new chat with the side panel CLOSED', () => {
    // The panel is open on the chat being left. A brand-new slot has no cached
    // activity bucket, so it must land closed — the same `?? false` every other
    // slot-entry path applies. Leaking the flag also left it unpersisted under
    // the new slot's key, so a reload silently closed the panel again.
    let state = { ...initial, activeSlot: 'A', activityOpen: true }
    state = reducer(state, {
      type: 'chat/createSlot/fulfilled',
      meta: { arg: undefined, requestId: 'r1', requestStatus: 'fulfilled' as const, originActiveSlot: 'A' },
      payload: { key: 'new-slot' },
    })
    expect(state.activityOpen).toBe(false)
    // The chat being left keeps its own open state in its bucket.
    expect(state.slotActivity['A']?.activityOpen).toBe(true)
  })

  it('createSlot.fulfilled leaves the panel alone when the user switched away', () => {
    // Switched-away guard: the create must not touch the view at all, panel
    // state included.
    let state = { ...initial, activeSlot: 'B', activityOpen: true }
    state = reducer(state, {
      type: 'chat/createSlot/fulfilled',
      meta: { arg: undefined, requestId: 'r1', requestStatus: 'fulfilled' as const, originActiveSlot: 'A' },
      payload: { key: 'new-slot' },
    })
    expect(state.activeSlot).toBe('B')
    expect(state.activityOpen).toBe(true)
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

  it('resumeFromHistory.fulfilled with a non-chat surface keeps the history row and the active slot (#3624)', () => {
    // The wire resume succeeded, but ChatPage cannot display the surface.
    // Consuming the row while the sidebar's notice says "can't be opened"
    // reads as data loss, and switching activeSlot to an undisplayable slot
    // is the silent bounce itself -- the reducer must not mutate at all.
    const before = { ...initial, activeSlot: 'A', history: [{ key: 'dash-1', title: 'Ops', messages: 3 }], historyOffset: 1, slotHistory: ['Z'] }
    const after = reducer(before, {
      type: 'chat/resumeFromHistory/fulfilled',
      meta: { arg: { key: 'dash-1', title: 'Ops' }, requestId: 'r1', requestStatus: 'fulfilled' as const },
      payload: { ok: true, key: 'dash-1', surface: 'dashboard', messages: [], hasMore: false, total: 0 },
    })
    expect(after.history).toEqual(before.history)
    expect(after.activeSlot).toBe('A')
    expect(after.historyOffset).toBe(1)
    expect(after.slotHistory).toEqual(['Z'])
  })

  it('resumeFromHistory.fulfilled with a chat-page surface still consumes the row and switches', () => {
    let state = { ...initial, activeSlot: 'A', history: [{ key: 'H', title: 'old', messages: 1 }] }
    state = reduceResume(reducer, state, { ok: true, key: 'H', surface: 'orchestrator', messages: [], hasMore: false, total: 0 }, 'r1', { key: 'H', title: 'old' })
    expect(state.history).toEqual([])
    expect(state.activeSlot).toBe('H')
  })

  it('resumeFromHistory.fulfilled pushes activeSlot onto history', () => {
    let state = { ...initial, activeSlot: 'A' }
    state = reduceResume(reducer, state, { ok: true, key: 'H', messages: [], hasMore: false, total: 0 }, 'r1', { key: 'H', title: 'old' })
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
    state = reduceResume(reducer, state, { ok: true, key: 'H', messages: [], hasMore: false, total: 0 }, 'r1', { key: 'H', title: 'old' })
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
      slotStatusDetail: { x: { kind: 'tool', purpose: 'hi', ts: 1 } },
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

describe('queue reorder reducer', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withQueued3 = () => {
    let state = reducer(initial, setActiveSlot('chat-1'))
    state = reducer(state, appendQueuedMessage({ slot: 'chat-1', content: 'first', ts: 't1', queue_id: 'q1' }))
    state = reducer(state, appendQueuedMessage({ slot: 'chat-1', content: 'second', ts: 't2', queue_id: 'q2' }))
    state = reducer(state, appendQueuedMessage({ slot: 'chat-1', content: 'third', ts: 't3', queue_id: 'q3' }))
    return state
  }
  const queuedIds = (state: ReturnType<typeof reducer>) =>
    state.messages.filter(m => m.role === 'queued').map(m => m.meta?.queueId)

  it('reorders queued messages to the given id sequence', () => {
    let state = withQueued3()
    state = reducer(state, reorderQueuedMessages({ slot: 'chat-1', order: ['q3', 'q1', 'q2'] }))
    expect(queuedIds(state)).toEqual(['q3', 'q1', 'q2'])
    expect(state.messages.filter(m => m.role === 'queued').map(m => m.content)).toEqual(['third', 'first', 'second'])
  })

  it('keeps ids missing from the order after the ordered ones (backend semantics)', () => {
    let state = withQueued3()
    state = reducer(state, reorderQueuedMessages({ slot: 'chat-1', order: ['q2'] }))
    expect(queuedIds(state)).toEqual(['q2', 'q1', 'q3'])
  })

  it('ignores unknown ids in the order', () => {
    let state = withQueued3()
    state = reducer(state, reorderQueuedMessages({ slot: 'chat-1', order: ['ghost', 'q2', 'q1', 'q3'] }))
    expect(queuedIds(state)).toEqual(['q2', 'q1', 'q3'])
  })

  it('does not move non-queued messages: queued slots are re-filled in place', () => {
    let state = reducer(initial, setActiveSlot('chat-1'))
    state = reducer(state, appendMessage({ role: 'user', content: 'hello', cls: 'msg msg-u' }))
    state = reducer(state, appendQueuedMessage({ slot: 'chat-1', content: 'first', ts: 't1', queue_id: 'q1' }))
    state = reducer(state, appendQueuedMessage({ slot: 'chat-1', content: 'second', ts: 't2', queue_id: 'q2' }))
    const before = state.messages.map(m => m.role)
    state = reducer(state, reorderQueuedMessages({ slot: 'chat-1', order: ['q2', 'q1'] }))
    expect(state.messages.map(m => m.role)).toEqual(before)
    expect(queuedIds(state)).toEqual(['q2', 'q1'])
    expect(state.messages[0].content).toBe('hello')
  })

  it('is a no-op with fewer than two queued messages', () => {
    let state = reducer(initial, setActiveSlot('chat-1'))
    state = reducer(state, appendQueuedMessage({ slot: 'chat-1', content: 'only', ts: 't1', queue_id: 'q1' }))
    const before = state.messages
    state = reducer(state, reorderQueuedMessages({ slot: 'chat-1', order: ['q1'] }))
    expect(state.messages).toEqual(before)
  })

  it('targets slotMessages for a non-active slot', () => {
    let state = reducer(initial, setActiveSlot('chat-1'))
    state = reducer(state, appendQueuedMessage({ slot: 'chat-2', content: 'a', ts: 't1', queue_id: 'qa' }))
    state = reducer(state, appendQueuedMessage({ slot: 'chat-2', content: 'b', ts: 't2', queue_id: 'qb' }))
    state = reducer(state, reorderQueuedMessages({ slot: 'chat-2', order: ['qb', 'qa'] }))
    expect((state.slotMessages['chat-2'] || []).filter(m => m.role === 'queued').map(m => m.meta?.queueId)).toEqual(['qb', 'qa'])
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

describe('create activation tracking — which create activated which slot', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const pending = (requestId: string, arg?: { activate?: boolean }) => ({ type: 'chat/createSlot/pending', meta: { arg, requestId, requestStatus: 'pending' as const } })
  const fulfilled = (requestId: string, origin: string | null, key: string, activate = true) => ({
    type: 'chat/createSlot/fulfilled',
    meta: { arg: undefined, requestId, requestStatus: 'fulfilled' as const, originActiveSlot: origin, activate },
    payload: { key },
  })
  const rejected = (requestId: string) => ({ type: 'chat/createSlot/rejected', meta: { arg: undefined, requestId, requestStatus: 'rejected' as const }, error: {} })

  it('records the activated slot with the requestId of the create that activated it', () => {
    let state = reducer({ ...initial, activeSlot: 'A' }, pending('r1'))
    expect(state.foregroundCreateId).toBe('r1')
    state = reducer(state, fulfilled('r1', 'A', 'new-slot'))
    expect(state.activeSlot).toBe('new-slot')
    expect(state.lastCreatedActivation).toEqual({ slot: 'new-slot', requestId: 'r1', seq: expect.any(Number), burst: expect.any(Number) })
    expect(state.foregroundCreateId).toBeNull()
  })

  it('records no activation when the user switched away during the create', () => {
    const state = reducer(reducer({ ...initial, activeSlot: 'B' }, pending('r1')), fulfilled('r1', 'A', 'new-slot'))
    expect(state.activeSlot).toBe('B')
    expect(state.lastCreatedActivation).toBeNull()
  })

  it('a background create neither arms a foreground create nor records an activation', () => {
    let state = reducer({ ...initial, activeSlot: 'A' }, pending('bg', { activate: false }))
    expect(state.foregroundCreateId).toBeNull()
    state = reducer(state, fulfilled('bg', 'A', 'bg-slot', false))
    expect(state.activeSlot).toBe('A')
    expect(state.lastCreatedActivation).toBeNull()
  })

  it('a background create resolving does not clear a foreground create still in flight', () => {
    let state = reducer({ ...initial, activeSlot: 'A' }, pending('bg', { activate: false }))
    state = reducer(state, pending('fg'))
    state = reducer(state, fulfilled('bg', 'A', 'bg-slot', false))
    expect(state.foregroundCreateId).toBe('fg')
    state = reducer(state, fulfilled('fg', 'A', 'fg-slot'))
    expect(state.lastCreatedActivation).toEqual({ slot: 'fg-slot', requestId: 'fg', seq: expect.any(Number), burst: expect.any(Number) })
  })

  it('a second foreground create takes over the pending id, and the first one activating carries its own id', () => {
    let state = reducer({ ...initial, activeSlot: 'A' }, pending('r1'))
    state = reducer(state, pending('r2'))
    expect(state.foregroundCreateId).toBe('r2')
    state = reducer(state, fulfilled('r1', 'A', 'slot-1'))
    expect(state.lastCreatedActivation).toEqual({ slot: 'slot-1', requestId: 'r1', seq: expect.any(Number), burst: expect.any(Number) })
    expect(state.foregroundCreateId).toBe('r2')
  })

  it('a rejected create clears its own pending id only', () => {
    let state = reducer({ ...initial, activeSlot: 'A' }, pending('r1'))
    state = reducer(state, rejected('r1'))
    expect(state.foregroundCreateId).toBeNull()
    state = reducer(reducer(state, pending('r2')), rejected('r1'))
    expect(state.foregroundCreateId).toBe('r2')
  })

  describe('default-folder reveal', () => {
    const filed = (origin: string | null, opts: { defaultFolder?: boolean; activate?: boolean; folder_id?: string } = {}) => ({
      type: 'chat/createSlot/fulfilled',
      meta: { arg: { defaultFolder: opts.defaultFolder ?? true }, requestId: 'r1', requestStatus: 'fulfilled' as const, originActiveSlot: origin, activate: opts.activate ?? true },
      payload: { key: 'new-slot', folder_id: 'folder_id' in opts ? opts.folder_id : 'f-default' },
    })

    it('asks the sidebar to reveal an activated create filed into the default folder', () => {
      const state = reducer({ ...initial, activeSlot: 'A' }, filed('A'))
      expect(state.revealRequest).toEqual({ kind: 'session', target: 'new-slot', nonce: initial.revealNonce + 1, openFoldersOnly: true, folderId: 'f-default' })
      expect(state.revealNonce).toBe(initial.revealNonce + 1)
    })

    it('asks for no reveal when the user switched away, the create ran in the background, or no folder was applied', () => {
      expect(reducer({ ...initial, activeSlot: 'B' }, filed('A')).revealRequest).toBeNull()
      expect(reducer({ ...initial, activeSlot: 'A' }, filed('A', { activate: false })).revealRequest).toBeNull()
      expect(reducer({ ...initial, activeSlot: 'A' }, filed('A', { folder_id: undefined })).revealRequest).toBeNull()
      expect(reducer({ ...initial, activeSlot: 'A' }, filed('A', { defaultFolder: false })).revealRequest).toBeNull()
    })
  })

  it('the next foreground create clears the previous activation', () => {
    const state = reducer({ ...initial, activeSlot: 'A', lastCreatedActivation: { slot: 'old', requestId: 'r0' } }, pending('r1'))
    expect(state.lastCreatedActivation).toBeNull()
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
  const wrap = (chat: ReturnType<typeof reducer>, slots: Array<{ key: string; subagents_running?: boolean }> = []) =>
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
