/**
 * Second behaviour-coverage pass over `src/store/chatSlice.ts`, aimed at the
 * paths `ChatSliceCoverage.test.tsx` leaves untouched:
 *
 *  - the transcript-equality helpers behind switchSlot's reference-stability
 *    skip (`sameTranscript` / `sameMessage` / `jsonEqual`),
 *  - the two slot-detail merge passes (`mergePreservedThinking`,
 *    `mergePreservedClientTs`) reached through `refreshSlot.fulfilled`,
 *  - the remaining background-frame reconcile branches in
 *    `applyNonActiveFrame` (sendId scan, tail fallback, stale permissions,
 *    the background tool-log cap),
 *  - the active-slot frame branches: stop-card replacement, compacting,
 *    thinking dedup, a permission arriving for an already-rejected tool,
 *    the steer break and the tail reconcile fallback,
 *  - the approval / permission reducers (`removeByApprovalId`,
 *    `resolveByApprovalId` cross-slot lookup, `clearPendingPermissions`),
 *  - the per-slot activity seeding read from real localStorage,
 *  - and the thunks whose bodies were never entered: `resumeFromHistory`,
 *    `deleteHistorySession`, `loadOlderMessages`, the delete-navigates-to-a-peer
 *    path, and the create-slot colour / project carry.
 *
 * A real store is used for everything except the deliberately-partial-state
 * cases, and every assertion reads observable state back out.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import chatReducer, {
  appendSlotMessage,
  clearFocusToolCallId,
  clearPendingPermissions,
  createSlot,
  deleteHistorySession,
  deleteSlot,
  fetchHistory,
  finalizeAssistant,
  hydrateSlotMessages,
  markSubagentApproving,
  openActivityToTool,
  removeByApprovalId,
  removeQueuedMessage,
  cancelQueuedMessage,
  replaceMessages,
  resolveByApprovalId,
  resumeFromHistory,
  selectSubagentActivityCount,
  setActiveSlot,
  setFolderSuggestion,
  setFollowupCard,
  sseActivityEvent,
  sseChatMessage,
  sseChatMessageUpdate,
  sseSideResult,
  sseSubagentBatchChunks,
  sseSubagentChunk,
  sseSubagentDone,
  sseSubagentPending,
  sseSubagentSpawn,
  sseToolActivity,
  sseToolResult,
  switchSlot,
  truncateAfterIndex,
} from '../store/chatSlice'
import dashboardReducer, { addSlotOptimistic } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import { SPAWN_LAUNCH_MARKER } from '../pages/chat/types'
import type { ChatMessage, ChatSlot } from '../types'
import type { RootState } from '../store'

const apiMock = vi.hoisted(() => ({
  chatSlotDetail: vi.fn(),
  chatSlots: vi.fn(),
  chatSlotProject: vi.fn(),
  createChatSlot: vi.fn(),
  deleteChatSlot: vi.fn(),
  deleteSession: vi.fn(),
  resumeChatSlot: vi.fn(),
  sessions: vi.fn(),
  setSlotColor: vi.fn(),
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
const chat = (store: Store) => store.getState().chat

const slotRow = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key,
  title: key,
  messages: 0,
  running: false,
  ...extra,
} as ChatSlot)

const msg = (over: Partial<ChatMessage> & { role: string; content: string }): ChatMessage =>
  ({ cls: '', ...over }) as ChatMessage

/** A slot-detail payload as `fetchSlotDetail` normalizes it, dispatched
 *  directly so a reducer branch can be reached without a network round trip. */
function detail(key: string, messages: ChatMessage[], over: Record<string, unknown> = {}) {
  return {
    key,
    messages,
    running: false,
    stopping: false,
    hasMore: false,
    total: messages.length,
    queue: [],
    context: undefined,
    ...over,
  }
}

const lifecycle = (type: string, arg: string, payload: unknown) => ({
  type,
  meta: { arg, requestId: 'req-1', requestStatus: type.endsWith('fulfilled') ? 'fulfilled' : 'pending' },
  payload,
})

/** The pristine slice state, for the deliberately-partial-state cases below. */
const initial = chatReducer(undefined, { type: '@@INIT' })

beforeEach(() => {
  for (const fn of Object.values(apiMock)) fn.mockReset()
  apiMock.chatSlots.mockResolvedValue([])
  apiMock.setSlotColor.mockResolvedValue({})
  apiMock.chatSlotProject.mockResolvedValue({})
  apiMock.deleteChatSlot.mockResolvedValue({})
})

describe('chatSlice transcript equality on a repeat slot fetch', () => {
  // Switching back to an already-loaded slot re-fetches a history that is
  // usually identical. The skip is what keeps every consumer's reference (and
  // the virtualizer's measured row heights) intact, so it has to survive
  // messages whose renderable fields are arrays and nested objects.
  const withVariants = (): ChatMessage[] => [
    msg({ role: 'user', content: 'hi', ts: '1', meta: { mid: 'u1' } }),
    msg({
      role: 'assistant',
      content: 'there',
      ts: '2',
      meta: { mid: 'a1', tags: ['x'] },
      variants: [{ content: 'v1', ts: '2' }, { content: 'v2' }],
      variant_idx: 1,
    }),
  ]

  it('keeps the existing array when the refetched transcript renders identically', async () => {
    apiMock.chatSlotDetail.mockImplementation(async () => ({ messages: withVariants(), running: false, total: 2 }))
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    const first = chat(store).messages
    await store.dispatch(switchSlot('B'))
    await store.dispatch(switchSlot('A'))
    expect(chat(store).messages).toBe(first)
  })

  it('replaces the array when a variant list differs in shape or length', async () => {
    apiMock.chatSlotDetail.mockImplementation(async () => ({ messages: withVariants(), running: false, total: 2 }))
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    const first = chat(store).messages

    // One fewer variant: same message count, so the skip must be decided by the
    // per-field comparison rather than by array length.
    apiMock.chatSlotDetail.mockImplementation(async () => {
      const m = withVariants()
      m[1].variants = [{ content: 'v1', ts: '2' }]
      return { messages: m, running: false, total: 2 }
    })
    await store.dispatch(switchSlot('B'))
    await store.dispatch(switchSlot('A'))
    expect(chat(store).messages).not.toBe(first)
    expect(chat(store).messages[1].variants).toHaveLength(1)
  })

  it('replaces the array when a meta value stops being an array', async () => {
    apiMock.chatSlotDetail.mockImplementation(async () => ({ messages: withVariants(), running: false, total: 2 }))
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    const first = chat(store).messages

    apiMock.chatSlotDetail.mockImplementation(async () => {
      const m = withVariants()
      m[1].meta = { mid: 'a1', tags: { x: true } }
      return { messages: m, running: false, total: 2 }
    })
    await store.dispatch(switchSlot('B'))
    await store.dispatch(switchSlot('A'))
    expect(chat(store).messages).not.toBe(first)
    expect(chat(store).messages[1].meta?.tags).toEqual({ x: true })
  })

  it('replaces the array when a meta value becomes a bare string', async () => {
    apiMock.chatSlotDetail.mockImplementation(async () => ({ messages: withVariants(), running: false, total: 2 }))
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    const first = chat(store).messages

    apiMock.chatSlotDetail.mockImplementation(async () => {
      const m = withVariants()
      m[1].meta = { mid: 'a1', tags: 'x' }
      return { messages: m, running: false, total: 2 }
    })
    await store.dispatch(switchSlot('B'))
    await store.dispatch(switchSlot('A'))
    expect(chat(store).messages).not.toBe(first)
    expect(chat(store).messages[1].meta?.tags).toBe('x')
  })

  it('replaces the array when a meta object gains a key', async () => {
    apiMock.chatSlotDetail.mockImplementation(async () => ({ messages: withVariants(), running: false, total: 2 }))
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    const first = chat(store).messages

    apiMock.chatSlotDetail.mockImplementation(async () => {
      const m = withVariants()
      m[1].meta = { mid: 'a1', tags: ['x'], pinned: true }
      return { messages: m, running: false, total: 2 }
    })
    await store.dispatch(switchSlot('B'))
    await store.dispatch(switchSlot('A'))
    expect(chat(store).messages).not.toBe(first)
    expect(chat(store).messages[1].meta?.pinned).toBe(true)
  })
})

describe('chatSlice slot-detail refresh merges', () => {
  it('re-inserts a reasoning block above its answer and keeps an unanchored one', () => {
    // Reasoning is never persisted server-side, so the refresh fired on
    // chat_done would drop it without this merge. The second block's scan hits
    // a `user` row before any answer, so it has no anchor and must still
    // survive rather than being silently dropped.
    let s = chatReducer(initial, setActiveSlot('A'))
    s = chatReducer(s, replaceMessages([
      msg({ role: 'thinking', content: 'because…' }),
      msg({ role: 'assistant', content: 'answer one', ts: '2' }),
      msg({ role: 'thinking', content: 'orphan reasoning' }),
      msg({ role: 'user', content: 'next question', ts: '3' }),
    ]))
    s = chatReducer(s, lifecycle('chat/refreshSlot/fulfilled', 'A', detail('A', [
      msg({ role: 'assistant', content: 'answer one', ts: '2' }),
      msg({ role: 'user', content: 'next question', ts: '3' }),
    ])))
    const roles = s.messages.map(m => m.role)
    expect(roles).toEqual(['thinking', 'assistant', 'user', 'thinking'])
    expect(s.messages[0].content).toBe('because…')
    expect(s.messages[3].content).toBe('orphan reasoning')
  })

  it('carries a durable client identity onto the reloaded copy by exact ts', () => {
    // Pass 1: a stamp that already has a server ts matches its incoming copy by
    // that ts. The rows around it must be skipped, not mis-paired: one already
    // carries a clientTs, one has no ts at all, and one has a ts the transcript
    // never stamped.
    let s = chatReducer(initial, setActiveSlot('A'))
    s = chatReducer(s, replaceMessages([
      msg({ role: 'assistant', content: 'reloaded once', ts: '20', meta: { clientTs: 'msg-durable' } }),
    ]))
    s = chatReducer(s, lifecycle('chat/refreshSlot/fulfilled', 'A', detail('A', [
      msg({ role: 'user', content: 'already stamped', ts: '10', meta: { clientTs: 'msg-kept' } }),
      msg({ role: 'tool', content: 'no ts at all' }),
      msg({ role: 'assistant', content: 'different row', ts: '99' }),
      msg({ role: 'assistant', content: 'reloaded once', ts: '20' }),
    ])))
    const byContent = Object.fromEntries(s.messages.map(m => [m.content, m.meta?.clientTs]))
    expect(byContent['reloaded once']).toBe('msg-durable')
    expect(byContent['already stamped']).toBe('msg-kept')
    expect(byContent['different row']).toBeUndefined()
    expect(byContent['no ts at all']).toBeUndefined()
  })

  it('pairs a freshly-streamed identity newest-first and skips a stamped incoming row', () => {
    // Pass 2: a ts-less stamp (born this session) has nothing to match on, so
    // it pairs against the newest unused row of the same role. An incoming row
    // that already carries a clientTs is never a candidate.
    let s = chatReducer(initial, setActiveSlot('A'))
    s = chatReducer(s, replaceMessages([
      msg({ role: 'assistant', content: 'streamed now', meta: { clientTs: 'msg-fresh' } }),
    ]))
    s = chatReducer(s, lifecycle('chat/refreshSlot/fulfilled', 'A', detail('A', [
      msg({ role: 'assistant', content: 'streamed now', ts: '5', meta: { clientTs: 'msg-other' } }),
      msg({ role: 'assistant', content: 'streamed now', ts: '6' }),
    ])))
    expect(s.messages.map(m => m.meta?.clientTs)).toEqual(['msg-other', 'msg-fresh'])
  })

  it('declines to hand a fresh identity to a row that already carries one', () => {
    // The only content match in the incoming history was already stamped by an
    // earlier reload, so the fresh identity has nowhere to land and the
    // reloaded row must keep the stamp it already has.
    let s = chatReducer(initial, setActiveSlot('A'))
    s = chatReducer(s, replaceMessages([
      msg({ role: 'assistant', content: 'streamed now', meta: { clientTs: 'msg-fresh' } }),
    ]))
    s = chatReducer(s, lifecycle('chat/refreshSlot/fulfilled', 'A', detail('A', [
      msg({ role: 'assistant', content: 'streamed now', ts: '5', meta: { clientTs: 'msg-other' } }),
      msg({ role: 'assistant', content: 'a different answer', ts: '6' }),
    ])))
    expect(s.messages.map(m => m.meta?.clientTs)).toEqual(['msg-other', undefined])
  })

  it('discards a refresh that resolved for a slot the user already left', () => {
    let s = chatReducer(initial, setActiveSlot('A'))
    s = chatReducer(s, replaceMessages([msg({ role: 'user', content: 'mine' })]))
    s = chatReducer(s, lifecycle('chat/refreshSlot/fulfilled', 'B', detail('B', [
      msg({ role: 'user', content: 'someone else\u2019s history' }),
    ])))
    expect(s.messages.map(m => m.content)).toEqual(['mine'])
  })

  it('merges a locally-resolved permission back over the refreshed history and orders mixed stamps', () => {
    // The client's resolved flag is the only record that the approval was
    // answered, so it wins over the server copy — and re-injecting it forces a
    // sort across ISO, epoch, and missing timestamps.
    let s = chatReducer(initial, setActiveSlot('A'))
    s = chatReducer(s, replaceMessages([
      msg({ role: 'permission', content: 'run tests?', ts: '1700000002', meta: { approval_id: 'ap-1', resolved: 'approved' } }),
    ]))
    s = chatReducer(s, lifecycle('chat/refreshSlot/fulfilled', 'A', detail('A', [
      msg({ role: 'user', content: 'oldest', ts: '2023-11-14T22:13:20.000Z' }),
      msg({ role: 'permission', content: 'run tests?', ts: '1700000002', meta: { approval_id: 'ap-1' } }),
      msg({ role: 'permission', content: 'write file?', ts: '1700000003', meta: { approval_id: 'ap-2' } }),
      msg({ role: 'assistant', content: 'no stamp' }),
    ])))
    const perms = s.messages.filter(m => m.role === 'permission')
    expect(perms.find(m => m.meta?.approval_id === 'ap-1')?.meta?.resolved).toBe('approved')
    // The server-only approval is adopted, not dropped.
    expect(perms.find(m => m.meta?.approval_id === 'ap-2')).toBeDefined()
    // A row with no ts sorts to the front (epoch 0); the ISO row parses to the
    // same second as ap-1 rather than to a millisecond value.
    expect(s.messages[0].content).toBe('no stamp')
    expect(s.messages.map(m => m.content)).toContain('oldest')
  })

  it('keeps a background pane\u2019s locally-resolved approval when its cache is warmed', () => {
    let s = chatReducer(initial, setActiveSlot('A'))
    s = chatReducer(s, hydrateSlotMessages({
      slot: 'B',
      messages: [msg({ role: 'permission', content: 'run?', meta: { approval_id: 'ap-9', resolved: 'rejected' } })],
    }))
    s = chatReducer(s, lifecycle('chat/warmSlotCache/fulfilled', 'B', detail('B', [
      msg({ role: 'permission', content: 'run?', meta: { approval_id: 'ap-9' } }),
    ])))
    expect(s.slotMessages.B[0].meta?.resolved).toBe('rejected')
    expect(s.slotRun.B.state).toBe('idle')
  })

  it('drops a slot-detail payload whose key would poison the prototype', () => {
    let s = chatReducer(initial, setActiveSlot('__proto__'))
    const poisoned = detail('__proto__', [msg({ role: 'user', content: 'hostile' })])
    for (const type of ['chat/switchSlot/fulfilled', 'chat/refreshSlot/fulfilled', 'chat/warmSlotCache/fulfilled']) {
      s = chatReducer(s, lifecycle(type, '__proto__', poisoned))
    }
    expect(s.messages).toEqual([])
    expect(Object.keys(s.slotMessages)).toEqual([])
    expect(({} as Record<string, unknown>).hostile).toBeUndefined()
  })
})

describe('chatSlice background-slot reconcile', () => {
  it('caps the background pane tool log when reasoning lands on a full log', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    for (let i = 0; i < 100; i++) {
      store.dispatch(sseToolActivity({
        slot: 'bg', tool: `t${i}`, kind: 'read', purpose: '', input_preview: '', tool_call_id: `tc-${i}`,
      }))
    }
    expect(chat(store).slotActivity.bg.toolLog).toHaveLength(100)
    store.dispatch(sseChatMessage({ slot: 'bg', role: 'chunk', content: 'thinking out loud' }))
    const log = chat(store).slotActivity.bg.toolLog
    expect(log).toHaveLength(100)
    expect(log[log.length - 1].type).toBe('reasoning')
    // The oldest tool entry is the one that was evicted.
    expect(log[0].text).toBe('t1')
  })

  it('rejects a background pane\u2019s unanswered approvals when a new turn starts', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(hydrateSlotMessages({
      slot: 'bg',
      messages: [
        msg({ role: 'permission', content: 'with meta', meta: { approval_id: 'ap-1' } }),
        msg({ role: 'permission', content: 'no meta at all' }),
        msg({ role: 'permission', content: 'already answered', meta: { approval_id: 'ap-3', resolved: 'approved' } }),
      ],
    }))
    store.dispatch(sseChatMessage({ slot: 'bg', role: 'user', content: 'next turn' }))
    const perms = chat(store).slotMessages.bg.filter(m => m.role === 'permission')
    expect(perms.map(m => m.meta?.resolved)).toEqual(['rejected', 'rejected', 'approved'])
  })

  it('reconciles a background echo by sendId even after streaming pushed rows on top', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendSlotMessage({
      slot: 'bg',
      message: msg({ role: 'user', content: 'do it', meta: { sendId: 'send-7' } }),
    }))
    store.dispatch(sseChatMessage({ slot: 'bg', role: 'chunk', content: 'working' }))
    store.dispatch(sseChatMessage({
      slot: 'bg', role: 'user', content: 'do it', ts: '42', meta: { sendId: 'send-7', mid: 'mid-7' },
    }))
    const users = chat(store).slotMessages.bg.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].ts).toBe('42')
    expect(users[0].meta?.mid).toBe('mid-7')
    expect(users[0].meta?.optimistic).toBeUndefined()
  })

  it('stops the background sendId scan at a steer bubble instead of adopting it', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendSlotMessage({
      slot: 'bg',
      message: msg({ role: 'user', content: 'steered', meta: { steer: true, optimistic: true } }),
    }))
    store.dispatch(sseChatMessage({
      slot: 'bg', role: 'user', content: 'a different send', meta: { sendId: 'send-9', mid: 'mid-9' },
    }))
    const users = chat(store).slotMessages.bg.filter(m => m.role === 'user')
    expect(users).toHaveLength(2)
    expect(users[0].meta?.mid).toBeUndefined()
  })

  it('stops the background sendId scan at the first user row that is not the match', () => {
    // The scan looks at the newest user row and stops there: an echo for a send
    // this pane never made must not adopt somebody else's bubble.
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendSlotMessage({
      slot: 'bg',
      message: msg({ role: 'user', content: 'mine', meta: { sendId: 'send-1' } }),
    }))
    store.dispatch(sseChatMessage({
      slot: 'bg', role: 'user', content: 'not mine', meta: { sendId: 'send-2', mid: 'mid-2' },
    }))
    const users = chat(store).slotMessages.bg.filter(m => m.role === 'user')
    expect(users.map(m => m.content)).toEqual(['mine', 'not mine'])
    expect(users[0].meta?.optimistic).toBe(true)
  })

  it('reconciles a background echo with no sendId by matching the tail content', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('front'))
    store.dispatch(appendSlotMessage({ slot: 'bg', message: msg({ role: 'user', content: 'queued then popped' }) }))
    store.dispatch(sseChatMessage({
      slot: 'bg', role: 'user', content: 'queued then popped', ts: '77', meta: { mid: 'mid-tail' },
    }))
    const users = chat(store).slotMessages.bg.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].ts).toBe('77')
    expect(users[0].meta?.mid).toBe('mid-tail')
  })
})

describe('chatSlice active-slot frame branches', () => {
  it('replaces an active stop card in place instead of stacking a second one', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseChatMessage({ slot: 'A', role: 'system', content: 'Stopping…', kind: 'stop_event', meta: { id: 'stop-1' } }))
    store.dispatch(sseChatMessage({ slot: 'A', role: 'system', content: 'Stopped', kind: 'stop_event', meta: { id: 'stop-1' } }))
    const cards = chat(store).messages.filter(m => m.kind === 'stop_event')
    expect(cards).toHaveLength(1)
    expect(cards[0].content).toBe('Stopped')
  })

  it('blocks the composer while a compaction runs on the active slot', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseChatMessage({ slot: 'A', role: 'compacting', content: '' }))
    expect(chat(store).slotState).toBe('compacting')
    expect(chat(store).slotRunning).toBe(true)
    expect(chat(store).messages).toEqual([])
  })

  it('keeps a single thinking placeholder for the active turn', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseChatMessage({ slot: 'A', role: 'thinking', content: '' }))
    store.dispatch(sseChatMessage({ slot: 'A', role: 'thinking', content: '' }))
    expect(chat(store).messages.filter(m => m.role === 'thinking')).toHaveLength(1)
  })

  it('pre-resolves a permission whose tool was already rejected', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseToolActivity({
      slot: 'A', tool: 'bash', kind: 'shell', purpose: '', input_preview: 'rm -rf', tool_call_id: 'tc-9',
    }))
    store.dispatch(replaceMessages([
      msg({ role: 'permission', content: 'first ask', meta: { approval_id: 'ap-1', tool_call_id: 'tc-9' } }),
    ]))
    store.dispatch(resolveByApprovalId({ id: 'ap-1', decision: 'rejected' }))
    // A re-broadcast of the same tool's permission must not reopen the bar.
    store.dispatch(sseChatMessage({
      slot: 'A', role: 'permission', content: 'second ask',
      cls: JSON.stringify({ request_id: 'ap-2', tool_input: 'rm -rf', tool_call_id: 'tc-9' }),
    }))
    const latest = chat(store).messages[chat(store).messages.length - 1]
    expect(latest.meta?.approval_id).toBe('ap-2')
    expect(latest.meta?.resolved).toBe('rejected')
  })

  it('rejects an active unanswered approval that carries no meta on a new turn', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([msg({ role: 'permission', content: 'metaless' })]))
    store.dispatch(sseChatMessage({ slot: 'A', role: 'user', content: 'new turn' }))
    expect(chat(store).messages[0].meta?.resolved).toBe('rejected')
  })

  it('stops the active sendId scan at a steer bubble', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([msg({ role: 'user', content: 'steered', meta: { steer: true } })]))
    store.dispatch(sseChatMessage({
      slot: 'A', role: 'user', content: 'another send', meta: { sendId: 'send-3', mid: 'mid-3' },
    }))
    expect(chat(store).messages.filter(m => m.role === 'user')).toHaveLength(2)
  })

  it('reconciles an active echo with no sendId against the tail bubble', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([msg({ role: 'user', content: 'promoted from queue' })]))
    store.dispatch(sseChatMessage({
      slot: 'A', role: 'user', content: 'promoted from queue', ts: '88', meta: { mid: 'mid-88' },
    }))
    const users = chat(store).messages.filter(m => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].meta?.mid).toBe('mid-88')
    expect(users[0].ts).toBe('88')
  })
})

describe('chatSlice message patching', () => {
  it('patches a message by timestamp in both the live list and the slot cache', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([msg({ role: 'assistant', content: 'needs auth', ts: '11', meta: { kind: 'mcp_oauth' } })]))
    store.dispatch(hydrateSlotMessages({
      slot: 'B',
      messages: [msg({ role: 'assistant', content: 'needs auth', ts: '12', meta: { kind: 'mcp_oauth' } })],
    }))
    store.dispatch(sseChatMessageUpdate({ slot: 'A', ts: '11', content: 'authenticated', meta: { authed: true } }))
    store.dispatch(sseChatMessageUpdate({ slot: 'B', ts: '12', meta: { authed: true } }))
    store.dispatch(sseChatMessageUpdate({ slot: 'B', ts: 'no-such-ts', content: 'ignored' }))
    expect(chat(store).messages[0].content).toBe('authenticated')
    expect(chat(store).messages[0].meta?.authed).toBe(true)
    expect(chat(store).slotMessages.B[0].meta?.authed).toBe(true)
    expect(chat(store).slotMessages.B[0].content).toBe('needs auth')
  })

  it('patches a tool row by call id in the background slot cache as well', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([msg({ role: 'tool', content: 'active tool', meta: { tool_call_id: 'tc-1' } })]))
    store.dispatch(hydrateSlotMessages({
      slot: 'A',
      messages: [msg({ role: 'tool', content: 'ignored, A is active', meta: { tool_call_id: 'tc-1' } })],
    }))
    store.dispatch(hydrateSlotMessages({
      slot: 'B',
      messages: [
        msg({ role: 'assistant', content: 'not a tool row' }),
        msg({ role: 'tool', content: 'cached tool', meta: { tool_call_id: 'tc-1' } }),
      ],
    }))
    store.dispatch(sseChatMessageUpdate({ slot: 'A', tool_call_id: 'tc-1', content: 'patched active' }))
    store.dispatch(sseChatMessageUpdate({ slot: 'B', tool_call_id: 'tc-1', content: 'patched cache', meta: { output: 'ok' } }))
    expect(chat(store).messages[0].content).toBe('patched active')
    expect(chat(store).slotMessages.B[1].content).toBe('patched cache')
    expect(chat(store).slotMessages.B[1].meta?.output).toBe('ok')
    expect(chat(store).slotMessages.B[0].content).toBe('not a tool row')
  })

  it('ignores an update frame with no slot', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([msg({ role: 'assistant', content: 'untouched', ts: '11' })]))
    store.dispatch(sseChatMessageUpdate({ slot: '', ts: '11', content: 'clobbered' }))
    expect(chat(store).messages[0].content).toBe('untouched')
  })

  it('merges a tool_call_update into the existing pill rather than adding one', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseToolActivity({
      slot: 'A', tool: 'bash', kind: '', purpose: '', input_preview: '', tool_call_id: 'tc-1',
    }))
    store.dispatch(sseToolActivity({
      slot: 'A', tool: 'bash', kind: 'shell', purpose: 'list the worktree', input_preview: 'ls -la',
      tool_call_id: 'tc-1', is_update: true, is_shell: true,
    }))
    const log = chat(store).toolLog
    expect(log).toHaveLength(1)
    expect(log[0].purpose).toBe('list the worktree')
    expect(log[0].input).toBe('ls -la')
    expect(log[0].kind).toBe('shell')
    expect(log[0].is_shell).toBe(true)
  })

  it('marks the permission message resolved when its approval resolves', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([msg({ role: 'permission', content: 'run?', meta: { approval_id: 'ap-1' } })]))
    store.dispatch(sseActivityEvent({ slot: 'A', kind: 'approval', text: 'run?', approval_id: 'ap-1', approval_type: 'tool' }))
    store.dispatch(sseActivityEvent({ slot: 'A', kind: 'approval_resolved', text: '', approval_id: 'ap-1' }))
    expect(chat(store).toolLog[0].type).toBe('approval_resolved')
    expect(chat(store).messages[0].meta?.resolved).toBe('approved')
  })

  it('copies a spawn launch result onto every tool row sharing the call id', () => {
    // An auto-approved tool produces two rows for one call id and the server
    // patches both, so stopping at the newest would leave the pair disagreeing.
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([
      msg({ role: 'assistant', content: 'not a tool row' }),
      msg({ role: 'tool', content: 'no meta yet' }),
      msg({ role: 'tool', content: 'other call', meta: { tool_call_id: 'tc-other' } }),
      msg({ role: 'tool', content: 'pre-approval', meta: { tool_call_id: 'tc-1' } }),
      msg({ role: 'tool', content: 'post-approval', meta: { tool_call_id: 'tc-1' } }),
    ]))
    store.dispatch(sseToolResult({ slot: 'A', tool_call_id: 'tc-1', output: `Spawned 2 ${SPAWN_LAUNCH_MARKER}` }))
    const outs = chat(store).messages.map(m => m.meta?.output)
    expect(outs[3]).toContain('Spawned 2')
    expect(outs[4]).toContain('Spawned 2')
    expect(outs[2]).toBeUndefined()
    expect(outs[1]).toBeUndefined()
  })
})

describe('chatSlice approval and permission reducers', () => {
  it('drops the approval row once its request is withdrawn', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([
      msg({ role: 'permission', content: 'run?', meta: { approval_id: 'ap-1' } }),
      msg({ role: 'assistant', content: 'kept' }),
    ]))
    store.dispatch(removeByApprovalId('ap-1'))
    expect(chat(store).messages.map(m => m.content)).toEqual(['kept'])
  })

  it('resolves an approval that lives in a background slot cache and flags its tool pill', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseToolActivity({
      slot: 'A', tool: 'bash', kind: 'shell', purpose: '', input_preview: '', tool_call_id: 'tc-5',
    }))
    store.dispatch(hydrateSlotMessages({
      slot: 'B',
      messages: [msg({ role: 'permission', content: 'run?', meta: { approval_id: 'ap-5', tool_call_id: 'tc-5' } })],
    }))
    store.dispatch(resolveByApprovalId({ id: 'ap-5', decision: 'rejected' }))
    expect(chat(store).slotMessages.B[0].meta?.resolved).toBe('rejected')
    expect(chat(store).toolLog[0].rejected).toBe(true)
  })

  it('defaults an unspecified decision to approved', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([msg({ role: 'permission', content: 'run?', meta: { approval_id: 'ap-1' } })]))
    store.dispatch(resolveByApprovalId({ id: 'ap-1' }))
    expect(chat(store).messages[0].meta?.resolved).toBe('approved')
  })

  it('rejects every unanswered approval and unfinished tool when the turn is stopped', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseToolActivity({
      slot: 'A', tool: 'bash', kind: 'shell', purpose: '', input_preview: '', tool_call_id: 'tc-1',
    }))
    store.dispatch(sseToolActivity({
      slot: 'A', tool: 'read', kind: 'read', purpose: '', input_preview: '', tool_call_id: 'tc-2',
    }))
    store.dispatch(sseToolResult({ slot: 'A', tool_call_id: 'tc-2', output: 'done' }))
    store.dispatch(replaceMessages([
      msg({ role: 'permission', content: 'with meta', meta: { approval_id: 'ap-1' } }),
      msg({ role: 'permission', content: 'metaless' }),
      msg({ role: 'permission', content: 'answered', meta: { approval_id: 'ap-3', resolved: 'approved' } }),
    ]))
    store.dispatch(clearPendingPermissions())
    expect(chat(store).messages.map(m => m.meta?.resolved)).toEqual(['rejected', 'rejected', 'approved'])
    const log = chat(store).toolLog
    expect(log.find(e => e.tool_call_id === 'tc-1')?.rejected).toBe(true)
    // The finished tool keeps its output and is not marked rejected.
    expect(log.find(e => e.tool_call_id === 'tc-2')?.rejected).toBeUndefined()
  })
})

describe('chatSlice transcript editing reducers', () => {
  it('appends a finalized answer when no stream is open', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(finalizeAssistant({ content: 'from a channel replay', ts: '9' }))
    expect(chat(store).messages).toHaveLength(1)
    expect(chat(store).messages[0].role).toBe('assistant')
    expect(chat(store).messages[0].ts).toBe('9')
  })

  it('truncates the transcript at a fork point', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([
      msg({ role: 'user', content: 'one' }),
      msg({ role: 'assistant', content: 'two' }),
      msg({ role: 'user', content: 'three' }),
    ]))
    store.dispatch(truncateAfterIndex(1))
    expect(chat(store).messages.map(m => m.content)).toEqual(['one'])
  })

  it('hydrates a background slot exactly once and never the active one', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(hydrateSlotMessages({ slot: 'A', messages: [msg({ role: 'user', content: 'active' })] }))
    expect(chat(store).slotMessages.A).toBeUndefined()

    store.dispatch(sseChatMessage({ slot: 'B', role: 'assistant', content: 'live frame first' }))
    store.dispatch(hydrateSlotMessages({ slot: 'B', messages: [msg({ role: 'user', content: 'history' })] }))
    store.dispatch(hydrateSlotMessages({ slot: 'B', messages: [msg({ role: 'user', content: 'second attempt' })] }))
    expect(chat(store).slotMessages.B.map(m => m.content)).toEqual(['history', 'live frame first'])
  })

  it('focuses a tool call inline and clears the signal once consumed', () => {
    const store = makeStore()
    store.dispatch(openActivityToTool('tc-42'))
    expect(chat(store).focusToolCallId).toBe('tc-42')
    store.dispatch(clearFocusToolCallId())
    expect(chat(store).focusToolCallId).toBeNull()
  })

  it('ignores queued-bubble edits aimed at a slot with no transcript', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(removeQueuedMessage({ slot: 'unknown', content: 'x' }))
    store.dispatch(cancelQueuedMessage({ slot: 'unknown', queue_id: 'q-1' }))
    expect(chat(store).slotMessages.unknown).toBeUndefined()
  })

  it('promotes a queued bubble matched by content when no queue id is supplied', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([
      msg({ role: 'queued', content: 'run the suite', ts: '5', meta: { queueId: 'q-1' } }),
    ]))
    store.dispatch(removeQueuedMessage({ slot: 'A', content: 'run the suite' }))
    expect(chat(store).messages.map(m => m.role)).toEqual(['user'])
    expect(chat(store).messages[0].ts).toBe('5')
  })
})

describe('chatSlice sub-agent cards', () => {
  it('marks an approving sub-agent that lives under a background slot', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseSubagentSpawn({ slot: 'B', id: 'ag-1', task: 'read specs', agent: 'kirocrew' }))
    store.dispatch(markSubagentApproving({ id: 'ag-1', approving: true }))
    expect(chat(store).slotActivity.B.subagents['ag-1'].approving).toBe(true)
    // An id nobody holds is a silent no-op rather than a crash.
    store.dispatch(markSubagentApproving({ id: 'ag-missing', approving: true }))
    expect(chat(store).slotActivity.B.subagents['ag-missing']).toBeUndefined()
  })

  it('truncates a runaway sub-agent stream from the front', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseSubagentSpawn({ slot: 'A', id: 'ag-1', task: 't', agent: 'kirocrew' }))
    store.dispatch(sseSubagentChunk({ slot: 'A', id: 'ag-1', text: 'x'.repeat(50_001) }))
    const streamed = chat(store).subagents['ag-1'].streaming
    expect(streamed.length).toBeLessThan(50_001)
    expect(streamed.endsWith('x')).toBe(true)
    expect(streamed).toContain('\n')
  })

  it('truncates a runaway stream delivered as a coalesced batch too', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseSubagentSpawn({ slot: 'A', id: 'ag-1', task: 't', agent: 'kirocrew' }))
    store.dispatch(sseSubagentBatchChunks({ chunks: [{ id: 'ag-1', slot: 'A', text: 'y'.repeat(50_001) }] }))
    expect(chat(store).subagents['ag-1'].streaming.length).toBeLessThan(50_001)
  })

  it('finds a finishing card filed under a different slot key', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseSubagentSpawn({ slot: 'B', id: 'ag-7', task: 'read specs', agent: 'kirocrew' }))
    store.dispatch(sseSubagentDone({ slot: 'C', id: 'ag-7', elapsed: 12, outcome: 'completed' }))
    expect(chat(store).slotActivity.B.subagents['ag-7'].status).toBe('done')
    expect(chat(store).slotActivity.B.subagents['ag-7'].elapsed).toBe(12)
  })

  it('backfills a native card\u2019s task, agent, and inline result on completion', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseSubagentSpawn({ slot: 'A', id: 'native:1', task: '', agent: '' }))
    store.dispatch(sseSubagentDone({
      slot: 'A', id: 'native:1', elapsed: 3, outcome: 'completed',
      task: 'summarize the log', agent: 'explorer', result: 'all green',
    }))
    const card = chat(store).subagents['native:1']
    expect(card.task).toBe('summarize the log')
    expect(card.result).toBe('all green')
    // A spawn with no agent falls back to the default name, so `agent` is
    // already set and the done payload must not overwrite it.
    expect(card.agent).toBe('kirocrew')
  })

  it('names the agent on a card that finished straight from the approval queue', () => {
    // A pending spawn-approval card is minted with an empty agent (the approval
    // title carries no agent name), so a completion is the first frame that can
    // fill it in.
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseSubagentPending({ slot: 'A', id: 'ag-2', task: '', approval_id: 'ap-2' }))
    expect(chat(store).subagents['ag-2'].agent).toBe('')
    store.dispatch(sseSubagentDone({
      slot: 'A', id: 'ag-2', elapsed: 1, outcome: 'stopped', task: 'never ran', agent: 'explorer',
    }))
    const card = chat(store).subagents['ag-2']
    expect(card.agent).toBe('explorer')
    expect(card.task).toBe('never ran')
    expect(card.status).toBe('stopped')
    // A stopped card carries no error text, and a managed id keeps no inline result.
    expect(card.error).toBeUndefined()
    expect(card.result).toBeUndefined()
  })

  it('counts nothing for a slot whose activity bucket has no sub-agent map', () => {
    const state = {
      chat: { activeSlot: null, subagents: {}, slotActivity: { bg: {} }, subagentQueued: undefined },
    } as unknown as RootState
    expect(selectSubagentActivityCount(state)).toBe(0)
  })
})

describe('chatSlice side conversation frames', () => {
  it('drops a side frame whose slot id would poison the prototype', () => {
    const store = makeStore()
    store.dispatch(sseSideResult({ slot: '__proto__', run_id: 'r1', role: 'user', content: 'hostile' }))
    expect(Object.keys(chat(store).slotSide)).toEqual([])
  })

  it('blocks a late assistant chunk that arrives after the side was closed', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseSideResult({ slot: 'A', run_id: 'r1', role: 'user', content: 'question' }))
    store.dispatch({ type: 'chat/sideClose', payload: 'A' })
    store.dispatch(sseSideResult({ slot: 'A', run_id: 'r1', role: 'assistant', content: 'too late' }))
    expect(chat(store).slotSide.A).toBeUndefined()
  })

  it('keeps an errored side answer as its own row and stops the spinner', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseSideResult({ slot: 'A', run_id: 'r1', role: 'user', content: 'question' }))
    store.dispatch(sseSideResult({ slot: 'A', run_id: 'r1', role: 'assistant', content: 'partial ' }))
    store.dispatch(sseSideResult({ slot: 'A', run_id: 'r1', role: 'assistant', content: 'boom', is_error: true }))
    const side = chat(store).slotSide.A
    expect(side.messages.map(m => m.is_error)).toEqual([undefined, undefined, true])
    expect(side.pending).toBe(false)
  })

  it('ignores a redelivered side chunk with byte-identical content', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(sseSideResult({ slot: 'A', run_id: 'r1', role: 'user', content: 'question' }))
    store.dispatch(sseSideResult({ slot: 'A', run_id: 'r1', role: 'assistant', content: 'answer', ts: 1 }))
    store.dispatch(sseSideResult({ slot: 'A', run_id: 'r1', role: 'assistant', content: 'answer', ts: 2 }))
    const side = chat(store).slotSide.A
    expect(side.messages.filter(m => m.role === 'assistant')).toHaveLength(1)
    expect(side.messages[1].ts).toBe(new Date(1000).toISOString())
  })
})

describe('chatSlice partial preloaded state', () => {
  // Older persisted state and hand-built fixtures can arrive without a key the
  // slice added later; indexing an absent map throws and would drop the update.
  it('creates the follow-up and folder maps on demand', () => {
    const bare = { ...initial, followups: undefined, folderSuggestions: undefined } as unknown as typeof initial
    let s = chatReducer(bare, setFollowupCard({ slot: 'A', items: [{ title: 'T', description: 'd', prompt: 'p' }] }))
    s = chatReducer(s, setFolderSuggestion({ slot: 'A', folderId: 'f1', folderName: 'Work', breadcrumb: 'Work' }))
    expect(s.followups.A.items).toHaveLength(1)
    expect(s.folderSuggestions.A.folderName).toBe('Work')
  })

  it('creates the hydration guard map on demand', () => {
    const bare = { ...initial, activeSlot: 'A', slotHydrated: undefined } as unknown as typeof initial
    const s = chatReducer(bare, hydrateSlotMessages({ slot: 'B', messages: [msg({ role: 'user', content: 'x' })] }))
    expect(s.slotHydrated.B).toBe(true)
  })

  it('creates the slot message cache on demand when a warm lands', () => {
    const bare = { ...initial, activeSlot: 'A', slotMessages: undefined } as unknown as typeof initial
    const s = chatReducer(bare, lifecycle('chat/warmSlotCache/fulfilled', 'B', detail('B', [
      msg({ role: 'user', content: 'warmed' }),
    ])))
    expect(s.slotMessages.B.map(m => m.content)).toEqual(['warmed'])
  })
})

describe('chatSlice thunks', () => {
  it('resumes an archived session and files it in the sidebar', async () => {
    apiMock.resumeChatSlot.mockResolvedValue({
      ok: true, key: 'resumed', messages: [
        { role: 'user', content: 'old question', cls: '' },
        { role: 'chunk', content: 'dropped', cls: '' },
      ],
      has_more: true, total: 250, next_before: 50, mode: 'dashboard', surface: 'dashboard', memory_mode: 'full',
    })
    const store = makeStore()
    store.dispatch(setActiveSlot('origin'))
    await store.dispatch(resumeFromHistory({ key: 'resumed', title: 'Old chat' }))
    const s = chat(store)
    expect(s.activeSlot).toBe('resumed')
    // Streaming scaffolding roles never enter the transcript.
    expect(s.messages.map(m => m.role)).toEqual(['user'])
    expect(s.slotHasMore).toBe(true)
    // The cursor is the server's raw index: only one of the two arrived rows
    // survived filterMessages, and neither count bears on it.
    expect(s.slotOldestIndex).toBe(50)
    expect(store.getState().dashboard.slots.some(sl => sl.key === 'resumed')).toBe(true)
  })

  it('leaves state alone when a resume is refused', async () => {
    apiMock.resumeChatSlot.mockResolvedValue({ ok: false, key: 'resumed', messages: [] })
    const store = makeStore()
    store.dispatch(setActiveSlot('origin'))
    await store.dispatch(resumeFromHistory({ key: 'resumed', title: 'Old chat' }))
    expect(chat(store).activeSlot).toBe('origin')
  })

  it('removes a deleted session from the history list', async () => {
    apiMock.sessions.mockResolvedValue({ sessions: [{ key: 'h1' }, { key: 'h2' }], has_more: false })
    apiMock.deleteSession.mockResolvedValue({})
    const store = makeStore()
    await store.dispatch(fetchHistory(false))
    expect(chat(store).history).toHaveLength(2)
    await store.dispatch(deleteHistorySession('h1'))
    expect(apiMock.deleteSession).toHaveBeenCalledWith('h1')
    expect(chat(store).history.map(h => h.key)).toEqual(['h2'])
  })

  // The older-page REQUEST is exercised at the reducer boundary rather than
  // through `loadOlderMessages()`: the thunk dispatches `pending` (which sets
  // `loadingOlder`) before its payload creator reads that same flag as a
  // re-entrancy guard, so the creator always returns null and never fetches.
  // Driving the thunk here would only assert that defect.
  it('prepends an older page and tracks the remaining depth', () => {
    let s = chatReducer(initial, setActiveSlot('A'))
    // Arm the cursor the way a resume does: one resident message out of ten, so
    // nine remain above it. Each page then carries the next cursor itself.
    s = chatReducer(s, lifecycle('chat/resumeFromHistory/fulfilled', 'A', {
      ok: true,
      key: 'A',
      nextBefore: 9,
      messages: [msg({ role: 'user', content: 'newest', ts: '9' })],
      hasMore: true,
      total: 10,
    }))
    expect(s.slotOldestIndex).toBe(9)
    s = chatReducer(s, lifecycle('chat/loadOlder/pending', 'A', undefined))
    expect(s.loadingOlder).toBe(true)
    s = chatReducer(s, lifecycle('chat/loadOlder/fulfilled', 'A', {
      slot: 'A',
      nextBefore: 8,
      messages: [msg({ role: 'user', content: 'older', ts: '1' })],
      hasMore: true,
      total: 10,
    }))
    expect(s.messages.map(m => m.content)).toEqual(['older', 'newest'])
    expect(s.slotHasMore).toBe(true)
    // Two rows are now resident, so eight remain above them.
    expect(s.slotOldestIndex).toBe(8)
    expect(s.loadingOlder).toBe(false)
  })

  it('ignores an older page that resolved for a slot the user left', () => {
    let s = chatReducer(initial, setActiveSlot('A'))
    s = chatReducer(s, replaceMessages([msg({ role: 'user', content: 'newest' })]))
    s = chatReducer(s, lifecycle('chat/loadOlder/fulfilled', 'B', {
      slot: 'B', messages: [msg({ role: 'user', content: 'other slot' })], hasMore: false, total: 1,
    }))
    expect(s.messages.map(m => m.content)).toEqual(['newest'])
    // A null payload (nothing left to page) is also a no-op.
    s = chatReducer(s, lifecycle('chat/loadOlder/fulfilled', 'A', null))
    expect(s.messages.map(m => m.content)).toEqual(['newest'])
  })

  it('clears the older-page spinner when the fetch fails', () => {
    let s = chatReducer(initial, setActiveSlot('A'))
    s = chatReducer(s, lifecycle('chat/loadOlder/pending', 'A', undefined))
    s = chatReducer(s, { type: 'chat/loadOlder/rejected', meta: { arg: 'A', requestId: 'req-1', requestStatus: 'rejected' }, error: { message: 'offline' } })
    expect(s.loadingOlder).toBe(false)
  })

  it('navigates to a peer session on the same surface when the active one is deleted', async () => {
    apiMock.chatSlotDetail.mockResolvedValue({ messages: [], running: false })
    const store = makeStore()
    store.dispatch(addSlotOptimistic(slotRow('peer', { mode: 'dashboard', surface: 'dashboard' })))
    store.dispatch(addSlotOptimistic(slotRow('doomed', { mode: 'dashboard', surface: 'dashboard' })))
    store.dispatch(addSlotOptimistic(slotRow('other-surface', { mode: 'slack', surface: 'slack' })))
    await store.dispatch(switchSlot('peer'))
    await store.dispatch(switchSlot('doomed'))

    await store.dispatch(deleteSlot('doomed'))
    expect(chat(store).activeSlot).toBe('peer')
    expect(store.getState().dashboard.slots.some(sl => sl.key === 'doomed')).toBe(false)
  })

  it('falls back to a cleared view when navigating to the peer fails', async () => {
    apiMock.chatSlotDetail.mockResolvedValueOnce({ messages: [], running: false })
    const store = makeStore()
    store.dispatch(addSlotOptimistic(slotRow('peer', { mode: 'dashboard', surface: 'dashboard' })))
    store.dispatch(addSlotOptimistic(slotRow('doomed', { mode: 'dashboard', surface: 'dashboard' })))
    await store.dispatch(switchSlot('doomed'))
    store.dispatch(sseChatMessage({ slot: 'doomed', role: 'assistant', content: 'will be dropped' }))

    apiMock.chatSlotDetail.mockRejectedValue(new Error('gone'))
    await store.dispatch(deleteSlot('doomed'))
    expect(chat(store).messages).toEqual([])
    expect(chat(store).slotRunning).toBe(false)
  })

  it('carries an explicit colour and a project directory onto a new session', async () => {
    apiMock.createChatSlot.mockResolvedValue({ key: 'fresh' })
    const store = makeStore()
    await store.dispatch(createSlot({ color_index: 4, project: '/tmp/worktree' }))
    expect(apiMock.setSlotColor).toHaveBeenCalledWith('fresh', 4)
    expect(apiMock.chatSlotProject).toHaveBeenCalledWith('fresh', '/tmp/worktree')
    const created = store.getState().dashboard.slots.find(sl => sl.key === 'fresh')
    expect(created?.color_index).toBe(4)
    expect(created?.project).toBe('/tmp/worktree')
    expect(chat(store).activeSlot).toBe('fresh')
  })

  it('clears the active view when a delete resolves for the slot still on screen', () => {
    let s = chatReducer(initial, setActiveSlot('doomed'))
    s = chatReducer(s, replaceMessages([msg({ role: 'user', content: 'stale' })]))
    s = chatReducer(s, lifecycle('chat/deleteSlot/fulfilled', 'doomed', 'doomed'))
    expect(s.activeSlot).toBeNull()
    expect(s.messages).toEqual([])
    expect(s.toolLog).toEqual([])
  })
})

describe('chatSlice per-slot activity seeding from storage', () => {
  const KEYS = ['mc-activity-open:alpha', 'mc-activity-open:beta', 'mc-activity-open:', 'unrelated-key']

  afterEach(() => {
    for (const k of KEYS) window.localStorage.removeItem(k)
    vi.resetModules()
  })

  it('restores each chat\u2019s panel open state on a cold load', async () => {
    window.localStorage.setItem('mc-activity-open:alpha', 'true')
    window.localStorage.setItem('mc-activity-open:beta', 'false')
    // A bare prefix (no slot) and an unrelated key must both be skipped.
    window.localStorage.setItem('mc-activity-open:', 'true')
    window.localStorage.setItem('unrelated-key', 'true')
    vi.resetModules()
    const fresh = await import('../store/chatSlice')
    const store = configureStore({ reducer: { chat: fresh.default } })
    const seeded = store.getState().chat.slotActivity
    expect(seeded.alpha).toEqual({ toolLog: [], subagents: {}, activityOpen: true })
    expect(seeded.beta.activityOpen).toBe(false)
    expect(Object.keys(seeded).sort()).toEqual(['alpha', 'beta'])
  })
})
