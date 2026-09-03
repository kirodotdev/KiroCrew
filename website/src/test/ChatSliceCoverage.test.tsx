/**
 * Behaviour coverage for `src/store/chatSlice.ts` aimed at the paths a rendered
 * component cannot reach: the fail-closed prototype-pollution guards on every
 * wire-keyed reducer, the bounded-retention caps, the background-slot frame
 * applier (`applyNonActiveFrame`), the staleness guards on the question /
 * follow-up / folder cards, the side-conversation queue, and the thunk failure
 * branches.
 *
 * A real store is used throughout and every assertion reads observable state
 * back out — no reducer is invoked directly and no internal is reached into.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  appendMessage,
  appendQueuedMessage,
  appendSlotMessage,
  captureStatelessCard,
  capturePendingAskId,
  shouldResolveAskOnSend,
  clearFolderSuggestion,
  clearFollowupCard,
  clearQuestionCard,
  clearTerminalSubagents,
  clearWorkflowRun,
  createSlot,
  deleteSlot,
  dismissFollowupItem,
  editQueuedMessage,
  fetchHistory,
  hydrateSlotMessages,
  loadOlderMessages,
  markSubagentApproving,
  mcpAppKey,
  missedChunkMarker,
  pendingQuestionFor,
  queueEditBroadcastAt,
  refreshSlot,
  reorderQueuedMessages,
  requestStop,
  resolveQuestionCard,
  resumeFromHistory,
  retireStatelessQuestion,
  selectComposerBusy,
  selectContinuable,
  selectSlotPendingApproval,
  selectSlotPendingSpawnApprovals,
  selectSlotSubagentsActive,
  selectSubagentActivityCount,
  selectTurnInterrupted,
  setActiveSlot,
  setFolderSuggestion,
  setFollowupCard,
  setAutomations,
  setQuestionCard,
  setQuestionDraft,
  setSlotStatusDetail,
  setStopPressedAt,
  sideClose,
  sideOptimisticAppend,
  sideOptimisticRollback,
  sideReleaseConsumed,
  sseActivityEvent,
  sseChatMessage,
  sseContextUsage,
  sseAutomation,
  sseMcpAppRender,
  sseSideQueue,
  sseSubagentBatchChunks,
  sseSubagentBatchUpdate,
  sseSubagentDone,
  sseSubagentPending,
  sseSubagentQueued,
  sseSubagentSnapshot,
  sseSubagentSpawn,
  sseToolActivity,
  switchSlot,
  warmSlotCache,
} from '../store/chatSlice'
import { isStopEvent } from '../lib/stopEvent'
import type { LegacyGoalLoop } from '../monitoring/automation'

const goalLoop = (
  slotKey: string,
  { active = true, cycleCount = 1, maxCycles = 5 }: Partial<LegacyGoalLoop> = {},
): LegacyGoalLoop => ({
  kind: 'legacy_goal_loop', id: `loop-${slotKey}`, slotKey, message: '', idleSecs: 60,
  maxCycles, cycleCount, active, lastFireAt: 0, stoppedReason: '',
})
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import type { ChatMessage, ChatSlot } from '../types'
import type { RootState } from '../store'

const apiMock = vi.hoisted(() => ({
  chatSlotDetail: vi.fn(),
  chatSlots: vi.fn(),
  chatMode: vi.fn(),
  chatSlotProject: vi.fn(),
  createChatSlot: vi.fn(),
  deleteChatSlot: vi.fn(),
  deleteSession: vi.fn(),
  forkChatSlot: vi.fn(),
  resumeChatSlot: vi.fn(),
  sessions: vi.fn(),
  setSlotColor: vi.fn(),
  stopChatSlot: vi.fn(),
  stopChatSlotForce: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: apiMock }))

function makeStore() {
  return configureStore({
    reducer: {
      chat: chatReducer,
      dashboard: dashboardReducer,
      notifications: notificationsReducer,
      instances: instancesReducer,
    },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

type Store = ReturnType<typeof makeStore>
const root = (store: Store): RootState => store.getState() as unknown as RootState
const chat = (store: Store) => store.getState().chat

/** Minimal slot record for the dashboard slice's authoritative slots list. */
const slotRow = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key,
  title: key,
  messages: 0,
  running: false,
  ...extra,
} as ChatSlot)

const POISON = ['__proto__', 'constructor', 'prototype'] as const

beforeEach(() => {
  for (const fn of Object.values(apiMock)) fn.mockReset()
  apiMock.chatSlots.mockResolvedValue([])
  apiMock.setSlotColor.mockResolvedValue({})
  apiMock.stopChatSlot.mockResolvedValue({})
  apiMock.stopChatSlotForce.mockResolvedValue({})
})

describe('chatSlice exported helpers', () => {
  it('namespaces an MCP App payload by session and tool call', () => {
    const key = mcpAppKey('dashboard:1', 'call-9')
    expect(key.startsWith('dashboard:1')).toBe(true)
    expect(key.endsWith('call-9')).toBe(true)
    expect(key).not.toBe(mcpAppKey('dashboard:2', 'call-9'))
  })

  it('reports a chunk gap only when the sequence numbers are not adjacent', () => {
    expect(missedChunkMarker(4, 5)).toBe('')
    expect(missedChunkMarker(9, 4)).toBe('')
    expect(missedChunkMarker(4, 8)).toContain('3')
  })

  it('reads a pending question card fail-closed', () => {
    const map = { real: { slot: 'real', questions: [], cardId: 'card-1' } }
    expect(pendingQuestionFor(map, 'real')?.cardId).toBe('card-1')
    expect(pendingQuestionFor(map, 'absent')).toBeNull()
    expect(pendingQuestionFor(undefined, 'real')).toBeNull()
    expect(pendingQuestionFor(map, null)).toBeNull()
    for (const bad of POISON) expect(pendingQuestionFor(map, bad)).toBeNull()
  })

  it('captures a stateless card identity but never a server-owned one', () => {
    const stateless = { s: { slot: 's', questions: [], cardId: 'card-7' } }
    expect(captureStatelessCard(stateless, 's')).toBe('card-7')
    const serverOwned = { s: { slot: 's', ask_id: 'ask-1', questions: [], cardId: 'card-7' } }
    expect(captureStatelessCard(serverOwned, 's')).toBeNull()
    expect(captureStatelessCard(stateless, 'absent')).toBeNull()
    const unminted = { s: { slot: 's', questions: [] } }
    expect(captureStatelessCard(unminted, 's')).toBeNull()
  })

  // The mirror capture for blocking cards, which the send path resolves over the
  // network because an agent is parked on the request.
  it('captures a blocking card ask_id but never a stateless one', () => {
    const blocking = { s: { slot: 's', ask_id: 'ask-1', questions: [], cardId: 'card-7' } }
    expect(capturePendingAskId(blocking, 's')).toBe('ask-1')
    const stateless = { s: { slot: 's', questions: [], cardId: 'card-7' } }
    expect(capturePendingAskId(stateless, 's')).toBeNull()
    expect(capturePendingAskId(blocking, 'absent')).toBeNull()
    expect(capturePendingAskId(undefined, 's')).toBeNull()
    expect(capturePendingAskId(blocking, null)).toBeNull()
    for (const bad of POISON) expect(capturePendingAskId(blocking, bad)).toBeNull()
  })

  // Resolving the card unmounts it, and a typed custom answer or a pending option
  // selection lives only in the component — the same work-in-progress invariant
  // the stateless path keeps.
  it('declines to capture a blocking card that holds an answer in progress', () => {
    const drafting = { s: { slot: 's', ask_id: 'ask-1', questions: [], cardId: 'card-7', draftActive: true } }
    expect(capturePendingAskId(drafting, 's')).toBeNull()
    const settled = { s: { slot: 's', ask_id: 'ask-1', questions: [], cardId: 'card-7', draftActive: false } }
    expect(capturePendingAskId(settled, 's')).toBe('ask-1')
  })

  // A queued acceptance MUST resolve the blocking card: the queue cannot pop
  // until the turn ends, and the turn cannot end while the agent is blocked on
  // the card, so waiting for queue_pop would hold both for the whole window.
  it('resolves a blocking card on an accepted send, queued included', () => {
    expect(shouldResolveAskOnSend({ ok: true }, 'ask-1')).toBe(true)
    expect(shouldResolveAskOnSend({ ok: true, queued: true }, 'ask-1')).toBe(true)
    expect(shouldResolveAskOnSend({ queued: true }, 'ask-1')).toBe(true)
  })

  it('leaves the card alone when the send was rejected or no card was pending', () => {
    // Rejected send: the card is the user's only way to answer, and the session
    // never moved on.
    expect(shouldResolveAskOnSend({ ok: false }, 'ask-1')).toBe(false)
    expect(shouldResolveAskOnSend({}, 'ask-1')).toBe(false)
    expect(shouldResolveAskOnSend(null, 'ask-1')).toBe(false)
    expect(shouldResolveAskOnSend(undefined, 'ask-1')).toBe(false)
    expect(shouldResolveAskOnSend({ ok: true }, null)).toBe(false)
  })

  it('reports no observed queue-edit broadcast for an untouched card', () => {
    expect(queueEditBroadcastAt('never-seen', 'q-1')).toBe(0)
  })

  it('recognises a stop card by either the top-level kind or the meta kind', () => {
    expect(isStopEvent({ role: 'system', content: '', kind: 'stop_event' } as ChatMessage)).toBe(true)
    expect(isStopEvent({ role: 'system', content: '', meta: { kind: 'stop_event' } } as ChatMessage)).toBe(true)
    expect(isStopEvent({ role: 'assistant', content: 'hi' } as ChatMessage)).toBe(false)
  })
})

describe('chatSlice prototype-pollution guards', () => {
  afterEach(() => {
    // Any leak would show up here as an inherited property on a bare object.
    const probe = {} as Record<string, unknown>
    for (const field of ['questions', 'items', 'folderId', 'toolLog', 'subagents', 'messages', 'pct', 'ts', 'status']) {
      expect(probe[field]).toBeUndefined()
    }
  })

  it('drops slot-keyed frames carrying a poisoned slot id', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('real'))
    for (const bad of POISON) {
      store.dispatch(setStopPressedAt({ slotId: bad, ts: 1 }))
      store.dispatch(setSlotStatusDetail({ slot: bad, kind: 'tool', text: 't', ts: 1 }))
      store.dispatch(sseContextUsage({ slot: bad, pct: 50, window_tokens: 100 }))
      store.dispatch(hydrateSlotMessages({ slot: bad, messages: [{ role: 'user', content: 'x' } as ChatMessage] }))
      store.dispatch(appendSlotMessage({ slot: bad, message: { role: 'user', content: 'x' } as ChatMessage }))
      store.dispatch(sseSubagentQueued({ slot: bad, queued: 3 }))
      store.dispatch(sseAutomation(goalLoop(bad)))
      store.dispatch(setAutomations({
        records: [goalLoop(bad)], legacyComplete: true, structuredComplete: true,
      }))
      store.dispatch(sseToolActivity({ slot: bad, tool: 't', kind: 'tool', purpose: '', input_preview: '' }))
      store.dispatch(sseActivityEvent({ slot: bad, kind: 'note', text: 'n' }))
      store.dispatch(clearTerminalSubagents({ slot: bad }))
      store.dispatch(editQueuedMessage({ slot: bad, queue_id: 'q', content: 'c' }))
      store.dispatch(reorderQueuedMessages({ slot: bad, order: ['q'] }))
      store.dispatch(sseSideQueue({ slot: bad, action: 'push', queue_id: 'q', content: 'c' }))
      store.dispatch(sideOptimisticAppend({ slot: bad, message: { role: 'user', content: 'c', ts: '1' } }))
      store.dispatch(sideClose(bad))
      store.dispatch(sseChatMessage({ slot: bad, role: 'user', content: 'x' }))
    }
    const s = chat(store)
    expect(Object.keys(s.stopPressedAt)).toEqual([])
    expect(Object.keys(s.slotStatusDetail)).toEqual([])
    expect(Object.keys(s.slotContextPct)).toEqual([])
    expect(Object.keys(s.slotMessages)).toEqual([])
    expect(Object.keys(s.subagentQueued)).toEqual([])
    expect(Object.keys(s.automations)).toEqual([])
    expect(Object.keys(s.slotSide)).toEqual([])
    expect(s.messages).toEqual([])
    expect(s.toolLog).toEqual([])
  })

  it('drops card frames carrying a poisoned slot id', () => {
    const store = makeStore()
    for (const bad of POISON) {
      store.dispatch(setQuestionCard({ slot: bad, questions: [{ question: 'q', options: [] }] }))
      store.dispatch(setQuestionDraft({ slot: bad, active: true }))
      store.dispatch(retireStatelessQuestion({ slot: bad, expected: 'card-1' }))
      store.dispatch(clearQuestionCard({ slot: bad }))
      store.dispatch(setFollowupCard({ slot: bad, items: [{ title: 't', description: 'd', prompt: 'p' }] }))
      store.dispatch(clearFollowupCard({ slot: bad }))
      store.dispatch(dismissFollowupItem({ slot: bad, index: 0 }))
      store.dispatch(setFolderSuggestion({ slot: bad, folderId: 'f', folderName: 'F', breadcrumb: 'F' }))
      store.dispatch(clearFolderSuggestion({ slot: bad }))
    }
    const s = chat(store)
    expect(Object.keys(s.pendingQuestions)).toEqual([])
    expect(Object.keys(s.followups)).toEqual([])
    expect(Object.keys(s.folderSuggestions)).toEqual([])
  })

  it('drops sub-agent frames carrying a poisoned slot or agent id', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('real'))
    for (const bad of POISON) {
      store.dispatch(sseSubagentPending({ slot: bad, id: 'a', task: 't', approval_id: 'ap' }))
      store.dispatch(sseSubagentPending({ slot: 'real', id: bad, task: 't', approval_id: 'ap' }))
      store.dispatch(sseSubagentSpawn({ slot: 'real', id: bad, task: 't', agent: 'kirocrew' }))
      store.dispatch(sseSubagentDone({ slot: 'real', id: bad, elapsed: 1 }))
      store.dispatch(sseSubagentSnapshot({ id: bad, slot: 'real', task: 't', agent: 'a', streaming: '', last_tool: '', started: 1 }))
      store.dispatch(markSubagentApproving({ id: bad, approving: true }))
      store.dispatch(sseSubagentBatchUpdate({ updates: [{ id: bad, slot: 'real', tool: 'grep' }] }))
      store.dispatch(sseSubagentBatchChunks({ chunks: [{ id: bad, slot: 'real', text: 'x' }] }))
    }
    expect(Object.keys(chat(store).subagents)).toEqual([])
    expect(Object.keys(chat(store).slotActivity)).toEqual([])
  })

  it('drops an MCP App render payload with a poisoned session or tool call id', () => {
    const store = makeStore()
    for (const bad of POISON) {
      store.dispatch(sseMcpAppRender({ session_key: bad, tool_call_id: 'call-1', html: '<p>x</p>' } as never))
      store.dispatch(sseMcpAppRender({ session_key: 'real', tool_call_id: bad, html: '<p>x</p>' } as never))
    }
    store.dispatch(sseMcpAppRender({ session_key: 'real', tool_call_id: '', html: '<p>x</p>' } as never))
    expect(Object.keys(chat(store).mcpApps)).toEqual([])
  })
})

describe('chatSlice background-slot frames (applyNonActiveFrame)', () => {
  it('streams chunks into one bubble and idles the pane on done', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'thinking', content: '' }))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'chunk', content: 'Hel', seq: 1 }))
    expect(chat(store).slotRun.back.state).toBe('streaming')
    store.dispatch(sseChatMessage({ slot: 'back', role: 'chunk', content: 'lo', seq: 2 }))
    expect(chat(store).slotMessages.back.map(m => m.role)).toEqual(['streaming'])
    expect(chat(store).slotMessages.back[0].content).toBe('Hello')
    store.dispatch(sseChatMessage({ slot: 'back', role: '_done', content: '' }))
    expect(chat(store).slotMessages.back[0].role).toBe('assistant')
    expect(chat(store).slotRun.back.state).toBe('idle')
  })

  it('marks a chunk gap on the background pane but not on a batched frame', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'chunk', content: 'a', seq: 1 }))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'chunk', content: 'b', seq: 5 }))
    expect(chat(store).slotMessages.back[0].content).toContain('3')

    const batched = makeStore()
    batched.dispatch(setActiveSlot('front'))
    batched.dispatch(sseChatMessage({ slot: 'back', role: 'chunk', content: 'a', seq: 1, batched: true }))
    batched.dispatch(sseChatMessage({ slot: 'back', role: 'chunk', content: 'b', seq: 5, batched: true }))
    expect(chat(batched).slotMessages.back[0].content).toBe('ab')
  })

  it('replaces a background stop card in place by id', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'system', content: 'stopping', meta: { kind: 'stop_event', id: 'stop-1' } }))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'system', content: 'stopped', meta: { kind: 'stop_event', id: 'stop-1' } }))
    expect(chat(store).slotMessages.back).toHaveLength(1)
    expect(chat(store).slotMessages.back[0].content).toBe('stopped')
  })

  it('drops a placeholder segment and freezes a real one', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'chunk', content: '...' }))
    store.dispatch(sseChatMessage({ slot: 'back', role: '_segment', content: '' }))
    expect(chat(store).slotMessages.back).toHaveLength(0)

    store.dispatch(sseChatMessage({ slot: 'back', role: 'chunk', content: 'real answer' }))
    store.dispatch(sseChatMessage({ slot: 'back', role: '_segment', content: '' }))
    expect(chat(store).slotMessages.back.map(m => m.role)).toEqual(['assistant'])
  })

  it('inserts a background tool row above the live stream and flags the pane', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'chunk', content: 'thinking out loud' }))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'tool', content: 'grep', meta: { tool_call_id: 'c1' } }))
    expect(chat(store).slotMessages.back.map(m => m.role)).toEqual(['tool', 'streaming'])
    expect(chat(store).slotRun.back.state).toBe('tool_running')
    store.dispatch(sseChatMessage({ slot: 'back', role: 'compacting', content: '' }))
    expect(chat(store).slotRun.back.state).toBe('compacting')
  })

  it('lifts a background permission row identity out of the JSON cls and tolerates a non-JSON one', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseChatMessage({
      slot: 'back',
      role: 'permission',
      content: 'allow?',
      cls: JSON.stringify({ request_id: 'req-1', tool_input: 'ls', is_read_only: true, tool_call_id: 'c9' }),
    }))
    const lifted = chat(store).slotMessages.back[0]
    expect(lifted.meta?.approval_id).toBe('req-1')
    expect(lifted.meta?.tool_call_id).toBe('c9')

    store.dispatch(sseChatMessage({ slot: 'back', role: 'permission', content: 'allow?', cls: 'msg msg-permission' }))
    expect(chat(store).slotMessages.back).toHaveLength(2)
    expect(chat(store).slotMessages.back[1].meta?.approval_id).toBeUndefined()
  })

  it('counts a redelivered background frame instead of rendering it twice', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    const frame = { slot: 'back', role: 'assistant', content: 'once', meta: { mid: 'row-1' } }
    store.dispatch(sseChatMessage(frame))
    store.dispatch(sseChatMessage(frame))
    expect(chat(store).slotMessages.back).toHaveLength(1)
    expect(chat(store)._redeliveredFramesDropped).toBe(1)
  })

  it('reconciles a background user echo rather than duplicating the optimistic bubble', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendSlotMessage({ slot: 'back', message: { role: 'user', content: 'ping', meta: { sendId: 's-bg-1' } } as ChatMessage }))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'user', content: 'ping', ts: '2026-01-01T00:00:00Z', meta: { mid: 'm-echo-1', sendId: 's-bg-1' } }))
    expect(chat(store).slotMessages.back).toHaveLength(1)
    expect(chat(store).slotMessages.back[0].ts).toBe('2026-01-01T00:00:00Z')
  })

  it('rejects unresolved background permissions on a new turn but not on a steer', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'permission', content: 'allow?', meta: { approval_id: 'a1' } }))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'user', content: 'steered', meta: { steer: true } }))
    expect(chat(store).slotMessages.back[0].meta?.resolved).toBeUndefined()
    store.dispatch(sseChatMessage({ slot: 'back', role: 'user', content: 'a brand new turn' }))
    expect(chat(store).slotMessages.back[0].meta?.resolved).toBe('rejected')
  })

  it('overwrites the background stream with the final assistant text and its row id', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'chunk', content: 'partial' }))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'assistant', content: 'final', ts: '2026-02-02T00:00:00Z', meta: { mid: 'row-9' } }))
    const [msg] = chat(store).slotMessages.back
    expect(msg.role).toBe('assistant')
    expect(msg.content).toBe('final')
    expect(msg.meta?.mid).toBe('row-9')
  })
})

describe('chatSlice bounded retention', () => {
  it('caps the active tool log at 100 entries', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    for (let i = 0; i < 130; i++) {
      store.dispatch(sseToolActivity({ slot: 'front', tool: `tool-${i}`, kind: 'tool', purpose: '', input_preview: '' }))
    }
    const log = chat(store).toolLog
    expect(log).toHaveLength(100)
    expect(log[0].text).toBe('tool-30')
  })

  it('evicts the oldest MCP App payloads for a slot past the retention cap', () => {
    const store = makeStore()
    for (let i = 0; i < 30; i++) {
      store.dispatch(sseMcpAppRender({ session_key: 'front', tool_call_id: `call-${i}`, html: `<p>${i}</p>` } as never))
    }
    const keys = Object.keys(chat(store).mcpApps)
    expect(keys).toHaveLength(24)
    expect(keys).not.toContain(mcpAppKey('front', 'call-0'))
    expect(keys).toContain(mcpAppKey('front', 'call-29'))
  })

  it('bounds the retired side-queue id list', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    for (let i = 0; i < 60; i++) {
      store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: `q-${i}`, content: `c${i}` }))
      store.dispatch(sseSideQueue({ slot: 'front', action: 'drain', queue_id: `q-${i}` }))
    }
    const retired = chat(store).slotSide.front.removedQueueIds ?? []
    expect(retired).toHaveLength(50)
    expect(retired).not.toContain('q-0')
    expect(retired).toContain('q-59')
  })

  it('bounds the recently-visited slot history', async () => {
    apiMock.chatSlotDetail.mockResolvedValue({ messages: [], running: false })
    const store = makeStore()
    for (let i = 0; i < 60; i++) {
      await store.dispatch(switchSlot(`slot-${i}`))
    }
    const history = chat(store).slotHistory
    expect(history).toHaveLength(50)
    expect(history).not.toContain('slot-0')
    expect(history[history.length - 1]).toBe('slot-58')
  })
})

describe('chatSlice question cards', () => {
  it('keeps one card per re-delivered ask but re-mints identity for a fresh one', () => {
    const store = makeStore()
    const questions = [{ question: 'Ship it?', options: [{ label: 'Yes' }] }]
    store.dispatch(setQuestionCard({ slot: 'front', ask_id: 'ask-1', questions }))
    const first = chat(store).pendingQuestions.front.cardId
    store.dispatch(setQuestionCard({ slot: 'front', ask_id: 'ask-1', questions }))
    expect(chat(store).pendingQuestions.front.cardId).toBe(first)
    store.dispatch(setQuestionCard({ slot: 'front', ask_id: 'ask-1', questions, fresh: true }))
    expect(chat(store).pendingQuestions.front.cardId).not.toBe(first)
  })

  it('retires a stateless card only for the identity the send captured', () => {
    const store = makeStore()
    store.dispatch(setQuestionCard({ slot: 'front', questions: [{ question: 'q', options: [] }] }))
    const captured = captureStatelessCard(chat(store).pendingQuestions, 'front')
    store.dispatch(retireStatelessQuestion({ slot: 'front', expected: 'card-stale' }))
    expect(chat(store).pendingQuestions.front).toBeDefined()
    store.dispatch(retireStatelessQuestion({ slot: 'front', expected: captured as string }))
    expect(chat(store).pendingQuestions.front).toBeUndefined()
    // A retire against an empty slot is a no-op rather than a throw.
    store.dispatch(retireStatelessQuestion({ slot: 'front', expected: 'card-1' }))
    expect(chat(store).pendingQuestions.front).toBeUndefined()
  })

  it('never retires a server-owned card through the stateless path', () => {
    const store = makeStore()
    store.dispatch(setQuestionCard({ slot: 'front', ask_id: 'ask-1', questions: [{ question: 'q', options: [] }] }))
    const cardId = chat(store).pendingQuestions.front.cardId as string
    store.dispatch(retireStatelessQuestion({ slot: 'front', expected: cardId }))
    expect(chat(store).pendingQuestions.front).toBeDefined()
  })

  it('clears a server-owned card by ask id across slots', () => {
    const store = makeStore()
    store.dispatch(setQuestionCard({ slot: 'a', ask_id: 'ask-1', questions: [{ question: 'q', options: [] }] }))
    store.dispatch(setQuestionCard({ slot: 'b', ask_id: 'ask-2', questions: [{ question: 'q', options: [] }] }))
    store.dispatch(resolveQuestionCard({ ask_id: 'ask-1' }))
    expect(chat(store).pendingQuestions.a).toBeUndefined()
    expect(chat(store).pendingQuestions.b).toBeDefined()
    store.dispatch(resolveQuestionCard({ ask_id: 'ask-unknown' }))
    expect(chat(store).pendingQuestions.b).toBeDefined()
  })

  it('spares a half-typed stateless answer when the server retires the record', () => {
    // A nudge on a monitored session retires the record while the user is still
    // typing. The typed text lives only in the card's component state, so
    // unmounting it here would discard the answer — the same invariant the frame
    // applier keeps. A blocking ask is different: its future is already settled.
    const store = makeStore()
    store.dispatch(setQuestionCard({ slot: 'front', card_id: 'card-live', questions: [{ question: 'q', options: [] }] }))
    store.dispatch(setQuestionDraft({ slot: 'front', active: true }))
    store.dispatch(resolveQuestionCard({ card_id: 'card-live' }))
    expect(chat(store).pendingQuestions.front).toBeDefined()

    // Once the draft is gone the same retirement clears it.
    store.dispatch(setQuestionDraft({ slot: 'front', active: false }))
    store.dispatch(resolveQuestionCard({ card_id: 'card-live' }))
    expect(chat(store).pendingQuestions.front).toBeUndefined()
  })

  it('clears a blocking card even mid-draft, since its ask is already settled', () => {
    const store = makeStore()
    store.dispatch(setQuestionCard({ slot: 'front', ask_id: 'ask-9', questions: [{ question: 'q', options: [] }] }))
    store.dispatch(setQuestionDraft({ slot: 'front', active: true }))
    store.dispatch(resolveQuestionCard({ ask_id: 'ask-9' }))
    expect(chat(store).pendingQuestions.front).toBeUndefined()
  })

  it('retires a stale stateless card on the next turn but spares a half-typed answer', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(setQuestionCard({ slot: 'front', questions: [{ question: 'q', options: [] }] }))
    store.dispatch(setQuestionDraft({ slot: 'front', active: true }))
    store.dispatch(sseChatMessage({ slot: 'front', role: 'nudge', content: 'keep going' }))
    expect(chat(store).pendingQuestions.front).toBeDefined()

    store.dispatch(setQuestionDraft({ slot: 'front', active: false }))
    store.dispatch(sseChatMessage({ slot: 'front', role: 'subagent', content: 'agent finished' }))
    expect(chat(store).pendingQuestions.front).toBeDefined()
    store.dispatch(sseChatMessage({ slot: 'front', role: 'user', content: 'answer' }))
    expect(chat(store).pendingQuestions.front).toBeUndefined()
  })

  it('retires a stale background card too, and a late draft flip is a no-op', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(setQuestionCard({ slot: 'back', questions: [{ question: 'q', options: [] }] }))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'user', content: 'answered elsewhere' }))
    expect(chat(store).pendingQuestions.back).toBeUndefined()
    store.dispatch(setQuestionDraft({ slot: 'back', active: true }))
    expect(chat(store).pendingQuestions.back).toBeUndefined()
  })
})

describe('chatSlice follow-up and folder cards', () => {
  it('ignores an empty follow-up payload and keeps a newer card on a stale clear', () => {
    const store = makeStore()
    store.dispatch(setFollowupCard({ slot: 'front', items: [] }))
    expect(chat(store).followups.front).toBeUndefined()

    store.dispatch(setFollowupCard({ slot: 'front', items: [{ title: 'A', description: 'd', prompt: 'p' }], ts: 100 }))
    store.dispatch(clearFollowupCard({ slot: 'front', ts: 99 }))
    expect(chat(store).followups.front).toBeDefined()
    store.dispatch(clearFollowupCard({ slot: 'front', ts: 100 }))
    expect(chat(store).followups.front).toBeUndefined()
    store.dispatch(clearFollowupCard({ slot: 'front' }))
    expect(chat(store).followups.front).toBeUndefined()
  })

  it('skips one suggestion and drops the card once the last one is gone', () => {
    const store = makeStore()
    store.dispatch(setFollowupCard({
      slot: 'front',
      ts: 5,
      items: [
        { title: 'A', description: 'd', prompt: 'p' },
        { title: 'B', description: 'd', prompt: 'p' },
      ],
    }))
    store.dispatch(dismissFollowupItem({ slot: 'front', index: 0, ts: 4 }))
    expect(chat(store).followups.front.items).toHaveLength(2)
    store.dispatch(dismissFollowupItem({ slot: 'front', index: 0, ts: 5 }))
    expect(chat(store).followups.front.items.map(i => i.title)).toEqual(['B'])
    store.dispatch(dismissFollowupItem({ slot: 'front', index: 0 }))
    expect(chat(store).followups.front).toBeUndefined()
    store.dispatch(dismissFollowupItem({ slot: 'front', index: 0 }))
    expect(chat(store).followups.front).toBeUndefined()
  })

  it('requires a complete folder offer and honours the staleness guard on clear', () => {
    const store = makeStore()
    store.dispatch(setFolderSuggestion({ slot: 'front', folderId: '', folderName: 'F', breadcrumb: 'F' }))
    expect(chat(store).folderSuggestions.front).toBeUndefined()

    store.dispatch(setFolderSuggestion({ slot: 'front', folderId: 'f1', folderName: 'Docs', breadcrumb: 'Root / Docs', ts: 7 }))
    expect(chat(store).folderSuggestions.front.folderName).toBe('Docs')
    store.dispatch(clearFolderSuggestion({ slot: 'front', ts: 6 }))
    expect(chat(store).folderSuggestions.front).toBeDefined()
    store.dispatch(clearFolderSuggestion({ slot: 'front', ts: 7 }))
    expect(chat(store).folderSuggestions.front).toBeUndefined()
    store.dispatch(clearFolderSuggestion({ slot: 'front' }))
    expect(chat(store).folderSuggestions.front).toBeUndefined()
  })
})

describe('chatSlice side conversation queue', () => {
  it('refuses to resurrect a closed side conversation from a queue mutation', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sideClose('front'))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-1', content: 'c' }))
    expect(chat(store).slotSide.front).toBeUndefined()
    store.dispatch(sseSideQueue({ slot: 'other', action: 'edit', queue_id: 'q-1', content: 'c' }))
    expect(chat(store).slotSide.other).toBeUndefined()
  })

  it('head-inserts a requeued steer and ignores a replayed push', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendMessage({ role: 'user', content: 'parent turn' } as ChatMessage))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-1', content: 'first', ts: 1000 }))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-2', content: 'jump', front: true, steer_id: 's-1' }))
    expect(chat(store).slotSide.front.queue?.map(e => e.id)).toEqual(['q-2', 'q-1'])
    expect(chat(store).slotSide.front.queue?.[0].steerId).toBe('s-1')
    expect(chat(store).slotSide.front.openedAtTurnCount).toBe(1)

    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-1', content: '[REDACTED]' }))
    expect(chat(store).slotSide.front.queue).toHaveLength(2)
    expect(chat(store).slotSide.front.queue?.[1].content).toBe('first')
  })

  it('treats raw content as a one-way ratchet and records a swallowed broadcast edit', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-1', content: 'plain' }))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'edit', queue_id: 'q-1', content: 'scrubbed' }))
    expect(chat(store).slotSide.front.queue?.[0].content).toBe('scrubbed')

    store.dispatch(sseSideQueue({ slot: 'front', action: 'edit', queue_id: 'q-1', content: 'the real secret', raw: true }))
    expect(chat(store).slotSide.front.queue?.[0].content).toBe('the real secret')

    expect(queueEditBroadcastAt('front', 'q-1')).toBe(0)
    store.dispatch(sseSideQueue({ slot: 'front', action: 'edit', queue_id: 'q-1', content: '[REDACTED]' }))
    expect(chat(store).slotSide.front.queue?.[0].content).toBe('the real secret')
    expect(queueEditBroadcastAt('front', 'q-1')).toBeGreaterThan(0)
  })

  it('releases a cancelled question into the composer buffer and accumulates two', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-1', content: 'first question' }))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-2', content: 'second question' }))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'cancel', queue_id: 'q-1' }))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'cancel', queue_id: 'q-2' }))
    const released = chat(store).slotSide.front.releasedText ?? ''
    expect(released).toContain('first question')
    expect(released).toContain('second question')
    expect(chat(store).slotSide.front.queue).toHaveLength(0)
  })

  it('stays quiet on another tab cancel unless this tab owns the unredacted copy', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-1', content: 'scrubbed' }))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'cancel', queue_id: 'q-1', suppressRelease: true }))
    expect(chat(store).slotSide.front.releasedText).toBeUndefined()

    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-2', content: 'mine', raw: true }))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'cancel', queue_id: 'q-2', suppressRelease: true }))
    expect(chat(store).slotSide.front.releasedText).toContain('mine')
  })

  it('ignores a push that lost the race to its own retirement', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-1', content: 'c' }))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'drain', queue_id: 'q-1' }))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-1', content: 'c' }))
    expect(chat(store).slotSide.front.queue).toHaveLength(0)
  })

  it('drains the released buffer by compare-and-clear, keeping later text', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-1', content: 'alpha' }))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'cancel', queue_id: 'q-1' }))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'push', queue_id: 'q-2', content: 'beta' }))
    store.dispatch(sseSideQueue({ slot: 'front', action: 'cancel', queue_id: 'q-2' }))
    store.dispatch(sideReleaseConsumed({ slot: 'front', consumed: 'alpha' }))
    expect(chat(store).slotSide.front.releasedText).toBe('beta')
    store.dispatch(sideReleaseConsumed({ slot: 'front', consumed: 'beta' }))
    expect(chat(store).slotSide.front.releasedText).toBeUndefined()
    store.dispatch(sideReleaseConsumed({ slot: 'absent', consumed: 'x' }))
    expect(chat(store).slotSide.absent).toBeUndefined()
  })

  it('rolls back the optimistic side bubble by marker, not by position', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sideClose('front'))
    store.dispatch(sideOptimisticAppend({ slot: 'front', message: { role: 'user', content: 'ask', ts: '2026-01-01T00:00:00Z' } }))
    expect(chat(store).slotSideClosed.front).toBeUndefined()
    expect(chat(store).slotSide.front.pending).toBe(true)
    store.dispatch(sideOptimisticRollback('front'))
    expect(chat(store).slotSide.front.messages).toHaveLength(0)
    expect(chat(store).slotSide.front.pending).toBe(false)
    store.dispatch(sideOptimisticRollback('absent'))
    expect(chat(store).slotSide.absent).toBeUndefined()
  })
})

describe('chatSlice workflow runs', () => {
  it('folds a run lifecycle into one progress entry and clears it', () => {
    const store = makeStore()
    store.dispatch({ type: 'chat/sseWorkflowEvent', payload: { run_id: 'r1', session_key: 'front', type: 'run_started', data: { name: 'Nightly' } } })
    store.dispatch({ type: 'chat/sseWorkflowEvent', payload: { run_id: 'r1', type: 'phase_started', data: { title: 'Discover' } } })
    store.dispatch({ type: 'chat/sseWorkflowEvent', payload: { run_id: 'r1', type: 'log', data: { message: 'step 1 done' } } })
    store.dispatch({ type: 'chat/sseWorkflowEvent', payload: { run_id: 'r1', type: 'log', data: {} } })
    store.dispatch({ type: 'chat/sseWorkflowEvent', payload: { run_id: 'r1', type: 'unknown_event' } })
    let run = chat(store).workflowRuns.r1
    expect(run.name).toBe('Nightly')
    expect(run.sessionKey).toBe('front')
    expect(run.phase).toBe('Discover')
    expect(run.lastLog).toBe('step 1 done')
    expect(run.status).toBe('running')

    store.dispatch({ type: 'chat/sseWorkflowEvent', payload: { run_id: 'r1', type: 'run_finished' } })
    expect(chat(store).workflowRuns.r1.status).toBe('finished')
    store.dispatch({ type: 'chat/sseWorkflowEvent', payload: { run_id: 'r2', type: 'run_failed', data: { error: 'boom' } } })
    run = chat(store).workflowRuns.r2
    expect(run.status).toBe('failed')
    expect(run.error).toBe('boom')
    expect(run.name).toBe('')
    store.dispatch({ type: 'chat/sseWorkflowEvent', payload: { run_id: 'r3', type: 'run_cancelled' } })
    expect(chat(store).workflowRuns.r3.status).toBe('cancelled')

    store.dispatch(clearWorkflowRun('r1'))
    expect(chat(store).workflowRuns.r1).toBeUndefined()
  })

  it('drops a workflow event with a missing or poisoned run id', () => {
    const store = makeStore()
    store.dispatch({ type: 'chat/sseWorkflowEvent', payload: { run_id: '', type: 'run_started' } })
    for (const bad of POISON) {
      store.dispatch({ type: 'chat/sseWorkflowEvent', payload: { run_id: bad, type: 'run_started' } })
    }
    expect(Object.keys(chat(store).workflowRuns)).toEqual([])
  })
})

describe('chatSlice automations and queued sub-agent counts', () => {
  it('keeps only active legacy loops', () => {
    const store = makeStore()
    store.dispatch(setAutomations({
      records: [
      goalLoop('a', { cycleCount: 3, maxCycles: 24 }),
      goalLoop('b', { active: false, cycleCount: 9, maxCycles: 24 }),
      ],
      legacyComplete: true,
      structuredComplete: true,
    }))
    expect(chat(store).automations.a).toEqual(goalLoop('a', { cycleCount: 3, maxCycles: 24 }))
    expect(chat(store).automations.b).toBeUndefined()

    store.dispatch(sseAutomation(goalLoop('a', { cycleCount: 4, maxCycles: 24 })))
    expect((chat(store).automations.a as LegacyGoalLoop).cycleCount).toBe(4)
    store.dispatch(sseAutomation(goalLoop('a', { active: false, cycleCount: 5, maxCycles: 24 })))
    expect(chat(store).automations.a).toBeUndefined()
  })

  it('drops a zero queued count instead of showing an empty waiting badge', () => {
    const store = makeStore()
    store.dispatch(sseSubagentQueued({ slot: 'front', queued: 4 }))
    expect(chat(store).subagentQueued.front).toBe(4)
    store.dispatch(sseSubagentQueued({ slot: 'front', queued: -2 }))
    expect(chat(store).subagentQueued.front).toBeUndefined()
  })
})

describe('chatSlice queued message bubbles', () => {
  it('does not duplicate a queued bubble a hydration already produced', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendQueuedMessage({ slot: 'front', content: 'later', ts: '1', queue_id: 'q-1' }))
    store.dispatch(appendQueuedMessage({ slot: 'front', content: 'later', ts: '1', queue_id: 'q-1' }))
    expect(chat(store).messages.filter(m => m.role === 'queued')).toHaveLength(1)
  })

  it('edits a queued bubble in place, including on a background slot', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendQueuedMessage({ slot: 'back', content: 'old', ts: '1', queue_id: 'q-1' }))
    store.dispatch(editQueuedMessage({ slot: 'back', queue_id: 'q-1', content: 'new' }))
    expect(chat(store).slotMessages.back[0].content).toBe('new')
    store.dispatch(editQueuedMessage({ slot: 'never-seen', queue_id: 'q-1', content: 'x' }))
    store.dispatch(editQueuedMessage({ slot: 'back', queue_id: 'absent', content: 'x' }))
    expect(chat(store).slotMessages.back[0].content).toBe('new')
  })

  it('re-slots queued bubbles into the given order, trailing the unlisted ones', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendQueuedMessage({ slot: 'front', content: 'one', ts: '1', queue_id: 'q-1' }))
    store.dispatch(appendQueuedMessage({ slot: 'front', content: 'two', ts: '2', queue_id: 'q-2' }))
    store.dispatch(appendQueuedMessage({ slot: 'front', content: 'three', ts: '3', queue_id: 'q-3' }))
    store.dispatch(reorderQueuedMessages({ slot: 'front', order: ['q-3', 'q-1'] }))
    expect(chat(store).messages.map(m => m.content)).toEqual(['three', 'one', 'two'])
  })

  it('leaves a single queued bubble and an unknown slot untouched', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendQueuedMessage({ slot: 'front', content: 'only', ts: '1', queue_id: 'q-1' }))
    store.dispatch(reorderQueuedMessages({ slot: 'front', order: ['q-1'] }))
    store.dispatch(reorderQueuedMessages({ slot: 'never-seen', order: ['q-1'] }))
    expect(chat(store).messages.map(m => m.content)).toEqual(['only'])
  })
})

describe('chatSlice selectors', () => {
  it('reports the composer busy for a running background pane or an orchestrating slot', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    expect(selectComposerBusy(root(store), null)).toBe(false)

    store.dispatch(sseChatMessage({ slot: 'back', role: 'chunk', content: 'x' }))
    expect(selectComposerBusy(root(store), 'back')).toBe(true)

    store.dispatch(sseSlots([slotRow('idlepane', { orchestrating: true })]))
    expect(selectComposerBusy(root(store), 'idlepane')).toBe(true)
    store.dispatch(sseSlots([slotRow('plainpane')]))
    expect(selectComposerBusy(root(store), 'plainpane')).toBe(false)
  })

  it('reports the composer busy while a background sub-agent runs', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseSubagentSpawn({ slot: 'back', id: 'a1', task: 't', agent: 'kirocrew' }))
    expect(selectSlotSubagentsActive(root(store), 'back')).toBe(true)
    expect(selectComposerBusy(root(store), 'back')).toBe(true)
    expect(selectSlotSubagentsActive(root(store), 'never-seen')).toBe(false)

    store.dispatch(sseSubagentDone({ slot: 'back', id: 'a1', elapsed: 2, outcome: 'completed' }))
    expect(selectSlotSubagentsActive(root(store), 'back')).toBe(false)
  })

  it('surfaces pending spawn approvals only when they carry an approval id', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    expect(selectSlotPendingSpawnApprovals(root(store), null)).toEqual([])
    expect(selectSlotPendingSpawnApprovals(root(store), 'never-seen')).toEqual([])

    store.dispatch(sseSubagentPending({ slot: 'front', id: 'a1', task: 'map it', approval_id: 'ap-1' }))
    const pending = selectSlotPendingSpawnApprovals(root(store), 'front')
    expect(pending.map(a => a.id)).toEqual(['a1'])

    store.dispatch(markSubagentApproving({ id: 'a1', approving: true }))
    expect(chat(store).subagents.a1.approving).toBe(true)

    store.dispatch(sseSubagentSpawn({ slot: 'front', id: 'a1', task: 'map it well', agent: 'kirocrew' }))
    expect(chat(store).subagents.a1.status).toBe('running')
    expect(chat(store).subagents.a1.task).toBe('map it well')
    expect(selectSlotPendingSpawnApprovals(root(store), 'front')).toEqual([])
  })

  it('counts in-flight sub-agents across every slot exactly once', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseSubagentSpawn({ slot: 'front', id: 'a1', task: 't', agent: 'kirocrew' }))
    store.dispatch(sseSubagentSpawn({ slot: 'back', id: 'b1', task: 't', agent: 'kirocrew' }))
    store.dispatch(sseSubagentQueued({ slot: 'back', queued: 2 }))
    expect(selectSubagentActivityCount(root(store))).toBe(4)

    store.dispatch(sseSubagentDone({ slot: 'front', id: 'a1', elapsed: 1, outcome: 'failed', error: 'boom' }))
    expect(chat(store).subagents.a1.status).toBe('error')
    expect(selectSubagentActivityCount(root(store))).toBe(3)
  })

  it('finds the pending approval after the last non-steer user message', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    expect(selectSlotPendingApproval(root(store), null)).toBeNull()

    store.dispatch(appendMessage({ role: 'user', content: 'do it' } as ChatMessage))
    store.dispatch(sseChatMessage({ slot: 'front', role: 'permission', content: 'allow?', meta: { approval_id: 'ap-1' } }))
    store.dispatch(appendMessage({ role: 'user', content: 'also this', meta: { steer: true } } as ChatMessage))
    expect(selectSlotPendingApproval(root(store), 'front')?.meta?.approval_id).toBe('ap-1')

    store.dispatch(appendMessage({ role: 'user', content: 'new turn' } as ChatMessage))
    expect(selectSlotPendingApproval(root(store), 'front')).toBeNull()
  })

  it('offers Continue only on an idle slot that holds a real conversation', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    expect(selectContinuable(root(store))).toBe(false)

    store.dispatch(appendMessage({ role: 'user', content: 'hello' } as ChatMessage))
    expect(selectContinuable(root(store))).toBe(true)

    store.dispatch(appendQueuedMessage({ slot: 'front', content: 'queued', ts: '1', queue_id: 'q-1' }))
    expect(selectContinuable(root(store))).toBe(false)
  })

  it('withholds Continue while a turn is live, stopping, or mid-plan', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendMessage({ role: 'user', content: 'hello' } as ChatMessage))
    store.dispatch({ type: 'chat/setSlotRunning', payload: true })
    expect(selectContinuable(root(store))).toBe(false)
    store.dispatch({ type: 'chat/setSlotRunning', payload: false })
    store.dispatch({ type: 'chat/setSlotStopping', payload: true })
    expect(selectContinuable(root(store))).toBe(false)
    store.dispatch({ type: 'chat/setSlotStopping', payload: false })

    store.dispatch(sseSlots([slotRow('front', { subagents_running: true })]))
    expect(selectContinuable(root(store))).toBe(false)
  })

  it('walks past interstitial rows and compaction notices to find the floor', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendMessage({ role: 'assistant', content: 'answered' } as ChatMessage))
    store.dispatch(appendMessage({ role: 'assistant', content: 'compacted', meta: { kind: 'compaction' } } as ChatMessage))
    store.dispatch(appendMessage({ role: 'inject', content: 'cron note' } as ChatMessage))
    expect(selectContinuable(root(store))).toBe(true)
    expect(selectTurnInterrupted(root(store))).toBe(false)
  })

  it('reads an interruption from a trailing user row or an error after the answer', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    expect(selectTurnInterrupted(root(store))).toBe(false)

    store.dispatch(appendMessage({ role: 'user', content: 'hello' } as ChatMessage))
    expect(selectTurnInterrupted(root(store))).toBe(true)

    store.dispatch(appendMessage({ role: 'assistant', content: 'partial' } as ChatMessage))
    expect(selectTurnInterrupted(root(store))).toBe(false)
    store.dispatch(appendMessage({ role: 'error', content: 'gateway died' } as ChatMessage))
    expect(selectTurnInterrupted(root(store))).toBe(true)
  })

  it('treats a deliberate stop as the end of a turn, not an interruption', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendMessage({ role: 'user', content: 'hello' } as ChatMessage))
    store.dispatch(sseChatMessage({ slot: 'front', role: 'system', content: 'stopped', meta: { kind: 'stop_event', id: 's-1' } }))
    expect(selectTurnInterrupted(root(store))).toBe(false)
  })
})

describe('chatSlice slot reconcile from the authoritative slots list', () => {
  it('evicts caches for sessions that vanished but never the active one', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseChatMessage({ slot: 'back', role: 'assistant', content: 'cached' }))
    store.dispatch(sseChatMessage({ slot: 'gone', role: 'assistant', content: 'doomed' }))
    store.dispatch(setFollowupCard({ slot: 'gone', items: [{ title: 'A', description: 'd', prompt: 'p' }] }))
    store.dispatch(sseContextUsage({ slot: 'gone', pct: 10, window_tokens: 200 }))

    // An empty frame is a reconnect artefact and must not wipe the caches.
    store.dispatch(sseSlots([]))
    expect(chat(store).slotMessages.gone).toBeDefined()

    store.dispatch(sseSlots([slotRow('back')]))
    expect(chat(store).slotMessages.back).toBeDefined()
    expect(chat(store).slotMessages.gone).toBeUndefined()
    expect(chat(store).followups.gone).toBeUndefined()
    expect(chat(store).slotContextPct.gone).toBeUndefined()
  })
})

describe('chatSlice thunks', () => {
  it('appends a second page of history and tracks the offset', async () => {
    apiMock.sessions.mockResolvedValueOnce({ sessions: [{ key: 's1' }], has_more: true })
    const store = makeStore()
    await store.dispatch(fetchHistory(false))
    expect(chat(store).history).toHaveLength(1)
    expect(chat(store).historyHasMore).toBe(true)

    apiMock.sessions.mockResolvedValueOnce({ sessions: [{ key: 's2' }], has_more: false })
    await store.dispatch(fetchHistory(true))
    expect(chat(store).history.map(s => s.key)).toEqual(['s1', 's2'])
    expect(chat(store).historyOffset).toBe(2)
    expect(apiMock.sessions).toHaveBeenLastCalledWith(30, 1, false, true)
  })

  it('asks the server to exclude sessions already open as tabs', async () => {
    // Older sessions is the complement of the tab list above it. The exclusion
    // has to happen server-side: historyOffset advances by the row count
    // received, so dropping rows on the client desynchronises paging.
    apiMock.sessions.mockResolvedValueOnce({ sessions: [], has_more: false })
    const store = makeStore()
    await store.dispatch(fetchHistory(false))
    expect(apiMock.sessions).toHaveBeenLastCalledWith(30, 0, false, true)
  })

  it('drops the resumed row from history so the pane stops listing it', async () => {
    // Resuming turns the row into an open tab, so it leaves the complement.
    // Keyed on meta.arg.key (the transcript name history is indexed by), not on
    // payload.key (the slot key the resume returned).
    apiMock.sessions.mockResolvedValueOnce({
      sessions: [{ key: 'dashboard_chat-1' }, { key: 'dashboard_chat-2' }],
      has_more: false,
    })
    const store = makeStore()
    await store.dispatch(fetchHistory(false))
    expect(chat(store).history.map(s => s.key)).toEqual(['dashboard_chat-1', 'dashboard_chat-2'])

    store.dispatch({
      type: resumeFromHistory.fulfilled.type,
      payload: { ok: true, key: 'chat-1', messages: [], hasMore: false, total: 0 },
      meta: { arg: { key: 'dashboard_chat-1', title: 'Some session' } },
    })
    expect(chat(store).history.map(s => s.key)).toEqual(['dashboard_chat-2'])
    // historyOffset counts rows consumed from the SERVER's list, and the server
    // drops the resumed row too. Holding the old offset would ask for a window
    // one past the end of a list that just shrank, skipping an unseen row.
    expect(chat(store).historyOffset).toBe(1)
  })

  it('leaves the offset alone when the resumed row was not in the pane', async () => {
    // A resume from a search hit or the command palette filters nothing here, so
    // the server's list is unchanged from this client's point of view.
    apiMock.sessions.mockResolvedValueOnce({
      sessions: [{ key: 'dashboard_chat-1' }, { key: 'dashboard_chat-2' }],
      has_more: true,
    })
    const store = makeStore()
    await store.dispatch(fetchHistory(false))

    store.dispatch({
      type: resumeFromHistory.fulfilled.type,
      payload: { ok: true, key: 'chat-9', messages: [], hasMore: false, total: 0 },
      meta: { arg: { key: 'dashboard_chat-9', title: 'Never listed here' } },
    })
    expect(chat(store).history).toHaveLength(2)
    expect(chat(store).historyOffset).toBe(2)
  })

  it('ignores a refresh or a warm that raced an active-slot change', async () => {
    apiMock.chatSlotDetail.mockResolvedValue({ messages: [{ role: 'assistant', content: 'server' }], running: false })
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    await store.dispatch(refreshSlot('back'))
    expect(chat(store).messages).toEqual([])
    await store.dispatch(warmSlotCache('front'))
    expect(chat(store).slotMessages.front).toBeUndefined()

    await store.dispatch(warmSlotCache('back'))
    expect(chat(store).slotMessages.back.map(m => m.content)).toEqual(['server'])
    expect(chat(store).slotRun.back.state).toBe('idle')
  })

  it('hydrates queued bubbles and seeds the context meter from a slot fetch', async () => {
    apiMock.chatSlotDetail.mockResolvedValue({
      messages: [{ role: 'user', content: 'hi' }],
      running: false,
      queue: ['plain string entry', { content: 'object entry', id: 'q-9' }],
      context_pct: 42,
      context_used_tokens: 8400,
      context_window_tokens: 20000,
      total: 1,
    })
    const store = makeStore()
    await store.dispatch(switchSlot('front'))
    const queued = chat(store).messages.filter(m => m.role === 'queued')
    expect(queued.map(m => m.content)).toEqual(['plain string entry', 'object entry'])
    expect(chat(store).slotContextPct.front).toBe(42)
    expect(chat(store).slotContextTokens.front).toEqual({ used: 8400, window: 20000 })

    // Absent-only: a later fetch must not clobber a measured live reading.
    store.dispatch(sseContextUsage({ slot: 'front', pct: 55, used_tokens: 11000, window_tokens: 20000 }))
    await store.dispatch(refreshSlot('front'))
    expect(chat(store).slotContextPct.front).toBe(55)
  })

  it('marks stale permissions resolved when the fetched slot is idle', async () => {
    apiMock.chatSlotDetail.mockResolvedValue({
      messages: [{ role: 'permission', content: 'allow?', meta: { approval_id: 'ap-1' } }],
      running: false,
    })
    const store = makeStore()
    await store.dispatch(switchSlot('front'))
    expect(chat(store).messages[0].meta?.resolved).toBe('stale')
    expect(selectSlotPendingApproval(root(store), 'front')).toBeNull()
  })

  it('re-attaches a locally finalized reply the server history predates', async () => {
    apiMock.chatSlotDetail.mockResolvedValue({ messages: [{ role: 'user', content: 'hi' }], running: false })
    const store = makeStore()
    store.dispatch(setActiveSlot('other'))
    store.dispatch(sseChatMessage({ slot: 'front', role: 'chunk', content: 'streamed while backgrounded' }))
    await store.dispatch(switchSlot('front'))
    const contents = chat(store).messages.map(m => m.content)
    expect(contents).toEqual(['hi', 'streamed while backgrounded'])
    expect(chat(store).messages[1].role).toBe('assistant')
  })

  it('restores the pane activity a switch away had cached', async () => {
    apiMock.chatSlotDetail.mockResolvedValue({ messages: [], running: false })
    const store = makeStore()
    await store.dispatch(switchSlot('front'))
    store.dispatch({ type: 'chat/openActivityToTab', payload: 'subagents' })
    store.dispatch(sseToolActivity({ slot: 'front', tool: 'grep', kind: 'tool', purpose: '', input_preview: '' }))

    await store.dispatch(switchSlot('back'))
    expect(chat(store).activityTab).toBe('changes')
    expect(chat(store).activityOpen).toBe(false)
    expect(chat(store).toolLog).toEqual([])

    await store.dispatch(switchSlot('front'))
    expect(chat(store).activityTab).toBe('subagents')
    expect(chat(store).activityOpen).toBe(true)
    expect(chat(store).toolLog.map(e => e.text)).toEqual(['grep'])
  })

  it('refuses to load older messages without a page to load', async () => {
    apiMock.chatSlotDetail.mockResolvedValue({ messages: [], running: false })
    const store = makeStore()
    await store.dispatch(loadOlderMessages())
    expect(apiMock.chatSlotDetail).not.toHaveBeenCalled()

    await store.dispatch(switchSlot('front'))
    apiMock.chatSlotDetail.mockClear()
    await store.dispatch(loadOlderMessages())
    expect(apiMock.chatSlotDetail).not.toHaveBeenCalled()
    expect(chat(store).loadingOlder).toBe(false)
  })

  it('debounces a soft stop and clears the press stamp when the request fails', async () => {
    const store = makeStore()
    await store.dispatch(requestStop({ slotId: 'front', force: false }))
    expect(apiMock.stopChatSlot).toHaveBeenCalledTimes(1)
    const stamp = chat(store).stopPressedAt.front as number
    expect(stamp).toBeGreaterThan(0)

    await store.dispatch(requestStop({ slotId: 'front', force: false }))
    expect(apiMock.stopChatSlot).toHaveBeenCalledTimes(1)

    apiMock.stopChatSlotForce.mockRejectedValueOnce(new Error('offline'))
    await store.dispatch(requestStop({ slotId: 'front', force: true }))
    expect(apiMock.stopChatSlotForce).toHaveBeenCalledTimes(1)
    expect(chat(store).stopPressedAt.front).toBe(0)
  })

  it('keeps the user where they are when a create resolves after they moved', async () => {
    apiMock.createChatSlot.mockImplementation(async () => {
      // The user switches away while the POST is in flight.
      store.dispatch(setActiveSlot('elsewhere'))
      return { key: 'brand-new' }
    })
    const store = makeStore()
    store.dispatch(setActiveSlot('origin'))
    await store.dispatch(createSlot({ agent: 'kirocrew' }))
    expect(chat(store).creatingSlot).toBe(false)
    expect(chat(store).activeSlot).toBe('elsewhere')
  })

  it('carries a caller-supplied title on the create request', async () => {
    // The server pins a title given at create time, locking the background
    // auto-titler out, and the create broadcast already carries it. A later
    // rename would paint a generated title first and can fail silently.
    apiMock.createChatSlot.mockResolvedValue({ key: 'titled-slot' })
    const store = makeStore()
    await store.dispatch(createSlot({ folder_id: 'f1', title: '#4237 · a readable name' }))
    expect(apiMock.createChatSlot.mock.calls[0][5]).toBe('#4237 · a readable name')
  })

  it('registers a background create without stealing focus', async () => {
    apiMock.createChatSlot.mockResolvedValue({ key: 'bg-slot' })
    apiMock.chatSlotProject.mockResolvedValue({})
    const store = makeStore()
    store.dispatch(setActiveSlot('origin'))
    await store.dispatch(createSlot({ activate: false, project: '/tmp/wt' }))
    expect(apiMock.chatSlotProject).toHaveBeenCalledWith('bg-slot', '/tmp/wt')
    expect(chat(store).activeSlot).toBe('origin')
    expect(chat(store).creatingSlot).toBe(false)
  })

  it('deletes an unscoped background session rather than publishing it', async () => {
    apiMock.createChatSlot.mockResolvedValue({ key: 'bg-slot' })
    apiMock.chatSlotProject.mockRejectedValue(new Error('scope failed'))
    apiMock.deleteChatSlot.mockResolvedValue({})
    const store = makeStore()
    const result = await store.dispatch(createSlot({ activate: false, project: '/tmp/wt' }))
    expect(result.type).toBe('chat/createSlot/rejected')
    expect(apiMock.deleteChatSlot).toHaveBeenCalledWith('bg-slot')
    expect(chat(store).creatingSlot).toBe(false)
  })

  // An activated create must not publish the slot until the server has
  // recorded the project: anything observing the optimistic slot earlier
  // (a roster fetch keyed to it, a turn sent into it) would run against the
  // default checkout, and a roster cached under the optimistic (slot, project)
  // identity would never refetch.
  it('scopes an activated create before publishing the slot', async () => {
    apiMock.createChatSlot.mockResolvedValue({ key: 'fg-slot' })
    apiMock.deleteChatSlot.mockResolvedValue({})
    const store = makeStore()
    apiMock.chatSlotProject.mockImplementation(async () => {
      expect(root(store).dashboard.slots.map(s => s.key)).not.toContain('fg-slot')
      return {}
    })
    await store.dispatch(createSlot({ project: '/tmp/wt' }))
    expect(apiMock.chatSlotProject).toHaveBeenCalledWith('fg-slot', '/tmp/wt')
    expect(root(store).dashboard.slots.map(s => s.key)).toContain('fg-slot')
  })

  it('deletes an unscoped activated session rather than publishing it', async () => {
    apiMock.createChatSlot.mockResolvedValue({ key: 'fg-slot' })
    apiMock.chatSlotProject.mockRejectedValue(new Error('scope failed'))
    apiMock.deleteChatSlot.mockResolvedValue({})
    const store = makeStore()
    const result = await store.dispatch(createSlot({ project: '/tmp/wt' }))
    expect(result.type).toBe('chat/createSlot/rejected')
    expect(apiMock.deleteChatSlot).toHaveBeenCalledWith('fg-slot')
    expect(root(store).dashboard.slots.map(s => s.key)).not.toContain('fg-slot')
    expect(chat(store).creatingSlot).toBe(false)
  })

  it('resyncs the slots list when a delete fails on the server', async () => {
    apiMock.chatSlotDetail.mockResolvedValue({ messages: [], running: false })
    apiMock.deleteChatSlot.mockRejectedValue(new Error('500'))
    const store = makeStore()
    await store.dispatch(switchSlot('front'))
    const outcome = await store.dispatch(deleteSlot('front'))
    expect(outcome.type).toBe('chat/deleteSlot/rejected')
    expect(apiMock.chatSlots).toHaveBeenCalled()
    // The optimistic navigation still happened: no peer session to fall back to.
    expect(chat(store).activeSlot).toBeNull()
  })

  // The dismissed tab must not wait on an unrelated conversation's transcript.
  // The peer's history fetch is unbounded, so awaiting it before the removal
  // pins the close control for as long as that load takes; only the state
  // transitions are ordered, and `switchSlot.pending` completes those
  // synchronously.
  it('removes the dismissed slot before the peer history fetch resolves', async () => {
    let releasePeer: (v: unknown) => void = () => {}
    const peerFetch = new Promise(resolve => { releasePeer = resolve })
    apiMock.chatSlotDetail.mockReturnValue(peerFetch)
    apiMock.deleteChatSlot.mockResolvedValue({})
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    store.dispatch(setActiveSlot('doomed'))

    const pending = store.dispatch(deleteSlot('doomed'))
    // Let the thunk run up to its first real suspension point.
    await Promise.resolve()

    // The peer's transcript is still in flight...
    expect(apiMock.chatSlotDetail).toHaveBeenCalledWith('peer', expect.any(Number))
    // ...yet the tab is already gone and focus already moved.
    expect(root(store).dashboard.slots.map(s => s.key)).not.toContain('doomed')
    expect(chat(store).activeSlot).toBe('peer')

    releasePeer({ messages: [], running: false })
    await pending
    expect(chat(store).activeSlot).toBe('peer')
  })

  // The thunk still owns the navigation it started: a caller that awaits the
  // dismissal reads the store afterwards, so resolution must mean the peer
  // settled, not merely that the DELETE returned.
  it('does not resolve the dismissal until the peer navigation settles', async () => {
    let releasePeer: (v: unknown) => void = () => {}
    const peerFetch = new Promise(resolve => { releasePeer = resolve })
    apiMock.chatSlotDetail.mockReturnValue(peerFetch)
    apiMock.deleteChatSlot.mockResolvedValue({})
    const store = makeStore()
    store.dispatch(sseSlots([slotRow('doomed'), slotRow('peer')]))
    store.dispatch(setActiveSlot('doomed'))

    let settled = false
    const pending = store.dispatch(deleteSlot('doomed')).then(r => { settled = true; return r })
    await Promise.resolve()
    await Promise.resolve()
    expect(settled).toBe(false)

    releasePeer({ messages: [{ role: 'user', content: 'peer history' }], running: false })
    const outcome = await pending
    expect(settled).toBe(true)
    expect(outcome.type).toBe('chat/deleteSlot/fulfilled')
    expect(chat(store).slotLoading).toBe(false)
  })

  it('evicts every per-slot cache once a delete succeeds', async () => {
    apiMock.chatSlotDetail.mockResolvedValue({ messages: [{ role: 'user', content: 'hi' }], running: false })
    apiMock.deleteChatSlot.mockResolvedValue({})
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(sseChatMessage({ slot: 'doomed', role: 'assistant', content: 'cached' }))
    store.dispatch(setFollowupCard({ slot: 'doomed', items: [{ title: 'A', description: 'd', prompt: 'p' }] }))
    store.dispatch(setFolderSuggestion({ slot: 'doomed', folderId: 'f', folderName: 'F', breadcrumb: 'F' }))
    store.dispatch(sseMcpAppRender({ session_key: 'doomed', tool_call_id: 'call-1', html: '<p>x</p>' } as never))

    await store.dispatch(deleteSlot('doomed'))
    const s = chat(store)
    expect(s.slotMessages.doomed).toBeUndefined()
    expect(s.followups.doomed).toBeUndefined()
    expect(s.folderSuggestions.doomed).toBeUndefined()
    expect(Object.keys(s.mcpApps)).toEqual([])
    expect(s.activeSlot).toBe('front')
  })
})

describe('chatSlice per-slot activity seeding', () => {
  const ORIGINAL = window.localStorage

  afterEach(() => {
    Object.defineProperty(window, 'localStorage', { configurable: true, value: ORIGINAL })
    vi.resetModules()
  })

  it('starts with no seeded panels when storage refuses to be enumerated', async () => {
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        get length(): number { throw new Error('storage access denied') },
        key: () => null,
        getItem: () => null,
        setItem: () => undefined,
        removeItem: () => undefined,
      },
    })
    vi.resetModules()
    const fresh = await import('../store/chatSlice')
    const store = configureStore({ reducer: { chat: fresh.default } })
    expect(store.getState().chat.slotActivity).toEqual({})
  })
})
