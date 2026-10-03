/**
 * The `/api/ws` frame router and socket lifecycle as ONE ordered trace per
 * scenario.
 *
 * Each scenario mounts the hook, feeds it frames, and records every effect the
 * hook performs in the order it performs them: Redux actions (through a
 * recording middleware, so a thunk shows up as its synchronous `pending`
 * action), React Query cache operations, window CustomEvents, outbound socket
 * frames and page reloads, with animation frames drained at a marked point.
 * The per-arm specs pin individual regressions; this file pins the whole
 * routing table and the open / reconnect / unmount sequences, so moving a
 * handler between owners cannot drop, add or reorder an effect unnoticed.
 *
 * Volatile values are scrubbed: numbers that look like clock readings, and the
 * request ids React Query and Redux Toolkit mint. Payloads are otherwise
 * recorded verbatim.
 *
 * Harness note: the hook DISPATCHES through the Provider store but READS
 * (`activeSlot`, `slots`, `slotStatusDetail`, ...) and subscribes off the
 * singleton store. Production passes the singleton as the Provider store, so
 * the singleton's `getState` / `subscribe` are redirected to the recording
 * store and every read sees what the hook itself dispatched.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement, StrictMode } from 'react'
import { Provider } from 'react-redux'
import { configureStore, type Middleware } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import { store as globalStore } from '../store'
import { useWebSocket } from '../hooks/useWebSocket'
import { memberProjectionStore } from '../state/memberProjectionStore'
import { threadLiveStore } from '../state/threadLiveStore'
import { _resetSlotReadRelayForTest } from '../lib/slotReadRelay'
import { holdStreamingFlushes, releaseStreamingFlushes } from '../lib/streamHold'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    ackNotification: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: false, monitors: [] }),
    pendingQuestions: vi.fn().mockResolvedValue([]),
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
    credentialRedaction: vi.fn().mockResolvedValue({ enabled: true, changed_at: '' }),
    voiceSynthesize: vi.fn().mockResolvedValue({ ok: true }),
    voiceCancel: vi.fn().mockResolvedValue({ ok: true }),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
  },
}))

const ACTIVE = 'slot-a'
const BACKGROUND = 'slot-b'
const TS = '2026-09-01T00:00:00.000Z'

const SLOTS = [
  { key: ACTIVE, title: 'Active', last_ts: TS },
  { key: BACKGROUND, title: 'Background', last_ts: TS },
] as unknown as ChatSlot[]

let trace: string[] = []

/** Clock readings (ms or s since the epoch) and minted ids are the only
 *  values that differ between runs; everything else is recorded as sent. */
function scrub(value: unknown): string {
  return JSON.stringify(value, (key, v) => {
    if (typeof v === 'number' && v > 1e9) return '<clock>'
    if (key === 'requestId' || key === 'queueId') return '<id>'
    // An error-journal entry carries its own minted id and clock.
    if (key === 'report') return '<report>'
    if (typeof v === 'function') return '<fn>'
    return v
  }) ?? 'undefined'
}

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn((payload: string) => { trace.push(`send ${payload}`) })
  close = vi.fn(() => { trace.push('close'); this.readyState = MockWebSocket.CLOSED })

  constructor(public url: string) {
    const parsed = new URL(url)
    trace.push(`connect ${parsed.protocol}${parsed.pathname}${parsed.search}`)
    WS_INSTANCES.push(this)
  }

  open() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  frame(frame: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(frame) }))
  }

  raw(data: string) {
    this.onmessage?.(new MessageEvent('message', { data }))
  }

  drop() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.(new CloseEvent('close'))
  }
}

let rafQueue: Array<{ id: number; cb: FrameRequestCallback }> = []
let nextFrameId = 1

function drainFrames() {
  const pending = rafQueue
  rafQueue = []
  if (pending.length) trace.push('frame')
  for (const { cb } of pending) cb(0)
}

const recorder: Middleware = () => next => action => {
  const a = action as { type: string; payload?: unknown }
  if (/\/(pending|fulfilled|rejected)$/.test(a.type)) trace.push(`action ${a.type}`)
  else trace.push(`action ${a.type} ${scrub(a.payload)}`)
  return next(action)
}

function makeStore() {
  return configureStore({
    reducer: {
      dashboard: dashboardReducer,
      chat: chatReducer,
      notifications: notificationsReducer,
      instances: instancesReducer,
    },
    middleware: getDefault => getDefault({ serializableCheck: false, immutableCheck: false }).concat(recorder),
  })
}

/** Records the hook's own cache calls, not the ones React Query makes
 *  internally while serving them (an invalidate refetches through
 *  `refetchQueries`). */
function recordQueries(qc: QueryClient) {
  let depth = 0
  const ops = [
    'invalidateQueries', 'resetQueries', 'setQueryData', 'setQueriesData', 'cancelQueries',
    'refetchQueries', 'fetchQuery', 'removeQueries',
  ] as const
  for (const op of ops) {
    const original = (qc[op] as (...args: unknown[]) => unknown).bind(qc)
    vi.spyOn(qc, op as 'invalidateQueries').mockImplementation(((...args: unknown[]) => {
      if (depth > 0) return original(...args)
      const [first, second] = args as [Record<string, unknown> | unknown[], Record<string, unknown> | undefined]
      if (Array.isArray(first)) {
        trace.push(`query ${op} ${scrub(first)}`)
      } else {
        const { queryKey, queryFn: _fn, predicate: _p, ...rest } = first ?? {}
        void _fn; void _p
        const opts = Object.keys(rest).length ? ` ${scrub(rest)}` : ''
        const extra = op === 'invalidateQueries' || op === 'refetchQueries' || op === 'resetQueries'
          ? (second ? ` ${scrub(second)}` : '')
          : ''
        trace.push(`query ${op} ${scrub(queryKey)}${opts}${extra}`)
      }
      depth += 1
      try {
        return original(...args)
      } finally {
        depth -= 1
      }
    }) as never)
  }
}

let testStore: ReturnType<typeof makeStore>
let qc: QueryClient
let originalReload: typeof window.location.reload
const originalDispatchEvent = window.dispatchEvent.bind(window)

beforeEach(async () => {
  vi.clearAllMocks()
  // Scenarios may override these two; every scenario starts from the defaults.
  const { api } = await import('../api/client')
  vi.mocked(api.voiceConfig).mockResolvedValue({ autoSpeak: false } as never)
  vi.mocked(api.voiceSynthesize).mockResolvedValue({ ok: true } as never)
  WS_INSTANCES.length = 0
  trace = []
  rafQueue = []
  nextFrameId = 1
  sessionStorage.clear()
  localStorage.clear()
  memberProjectionStore.clear()
  threadLiveStore.reset()
  _resetSlotReadRelayForTest()
  testStore = makeStore()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  recordQueries(qc)
  vi.stubGlobal('WebSocket', MockWebSocket)
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    const id = nextFrameId++
    rafQueue.push({ id, cb })
    return id
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => {
    trace.push(`cancel-frame ${id}`)
    rafQueue = rafQueue.filter(entry => entry.id !== id)
  })
  vi.spyOn(window, 'dispatchEvent').mockImplementation((event: Event) => {
    const detail = (event as CustomEvent).detail
    trace.push(`event ${event.type}${detail === undefined || detail === null ? '' : ` ${scrub(detail)}`}`)
    return originalDispatchEvent(event)
  })
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  vi.spyOn(globalStore, 'getState').mockImplementation(() => testStore.getState())
  vi.spyOn(globalStore, 'subscribe').mockImplementation(listener => testStore.subscribe(listener))
  originalReload = window.location.reload
  Object.defineProperty(window.location, 'reload', {
    configurable: true, value: vi.fn(() => { trace.push('reload') }),
  })
})

afterEach(() => {
  Object.defineProperty(window.location, 'reload', { configurable: true, value: originalReload })
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

/** Mount, prime the Provider store like the singleton, open, and settle the
 *  boot reads so a scenario's trace starts from a quiet socket. */
async function mountOpen() {
  testStore.dispatch(sseSlots(SLOTS))
  testStore.dispatch(setActiveSlot(ACTIVE))
  const view = renderHook(() => useWebSocket(), {
    wrapper: ({ children }) => createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children)),
  })
  const ws = WS_INSTANCES[0]
  await act(async () => { ws.open() })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  act(() => { drainFrames() })
  trace = []
  return { ws, view }
}

type Frame = { type: string; data?: unknown; [extra: string]: unknown }

/** Frames delivered before the trace starts, then the frames under test. */
async function run(setup: Frame[], frames: Frame[]): Promise<string[]> {
  const { ws } = await mountOpen()
  act(() => { for (const f of setup) ws.frame(f) })
  act(() => { drainFrames() })
  trace = []
  act(() => { for (const f of frames) ws.frame(f) })
  act(() => { drainFrames() })
  return trace
}

const approval = { id: 'ap-1', slot: ACTIVE, tool: 'shell', source: 'agent', tool_input: 'ls', ts: '1790000000' }

const FRAME_CASES: Array<[string, Frame[], Frame[]]> = [
  ['dashboard status', [], [{ type: 'dashboard', data: { version: '1.0', bundle_id: 'b1' } }]],
  ['dashboard version change reloads', [{ type: 'dashboard', data: { version: '1.0' } }], [{ type: 'dashboard', data: { version: '2.0' } }]],
  ['dashboard bundle change reloads', [{ type: 'dashboard', data: { version: '1.0', bundle_id: 'b1' } }], [{ type: 'dashboard', data: { version: '1.0', bundle_id: 'b2' } }]],
  ['slots first frame of a connection', [], [{
    type: 'slots', data: SLOTS, yolo: true, channelTrusted: false,
    folders: [{ id: 'f1', name: 'F' }], foldersGeneration: 3, gitlabHostsGeneration: 4, governanceGeneration: 5,
  }]],
  ['slots repeated frame is skipped', [{ type: 'slots', data: SLOTS, foldersGeneration: 3 }], [{ type: 'slots', data: SLOTS, foldersGeneration: 3 }]],
  ['slots unchanged generations', [{ type: 'slots', data: SLOTS, foldersGeneration: 3, gitlabHostsGeneration: 4 }], [{ type: 'slots', data: [...SLOTS].reverse(), foldersGeneration: 3, gitlabHostsGeneration: 4 }]],
  ['credential_redaction_changed', [], [{ type: 'credential_redaction_changed', data: { enabled: true, changed_at: TS } }]],
  ['credential_redaction_changed without a boolean', [], [{ type: 'credential_redaction_changed', data: { enabled: 'yes', changed_at: 3 } }]],
  ['skills.pending_changed', [], [{ type: 'skills.pending_changed', data: {} }]],
  ['todo_update', [], [{ type: 'todo_update', data: { slot: ACTIVE, todo: { items: [] } } }, { type: 'todo_update', data: { todo: null } }]],
  ['mcp_report_update', [], [{ type: 'mcp_report_update', data: { slot: ACTIVE, mcp_report: null } }, { type: 'mcp_report_update', data: { mcp_report: {} } }]],
  ['slot_title', [], [{ type: 'slot_title', data: { key: ACTIVE, title: 'Renamed' } }]],
  ['slot_patch with an unknown row', [], [{ type: 'slot_patch', data: { slots: [{ key: 'slot-new', title: 'New' }], removed: [] } }]],
  ['slot_patch removing a row', [], [{ type: 'slot_patch', data: { slots: [{ key: 'slot-gone', title: 'Gone' }], removed: ['slot-gone'] } }]],
  ['dashboard_card update, removal and no slot', [], [
    { type: 'dashboard_card', data: { slot: ACTIVE } },
    { type: 'dashboard_card', data: { slot: BACKGROUND, removed: true } },
    { type: 'dashboard_card', data: {} },
  ]],
  ['slot_projection for a worker and its creator', [{ type: 'slots', data: [
    { key: ACTIVE, title: 'Active', last_ts: TS },
    { key: BACKGROUND, title: 'Background', last_ts: TS, created_by: ACTIVE },
  ] }], [
    { type: 'slot_projection', data: { slot: `dashboard:${BACKGROUND}` } },
    { type: 'slot_projection', data: {} },
  ]],
  ['session_summary', [], [{ type: 'session_summary', data: { key: ACTIVE } }, { type: 'session_summary', data: {} }]],
  ['pins_changed', [], [{ type: 'pins_changed', data: { slot_key: ACTIVE } }, { type: 'pins_changed', data: {} }]],
  ['artifact_update', [], [{ type: 'artifact_update', data: { slug: 'doc' } }]],
  ['artifact_update deleted', [], [{ type: 'artifact_update', data: { slug: 'doc', deleted: true } }, { type: 'artifact_update', data: {} }]],
  ['notification', [], [{ type: 'notification', data: { kind: 'info', title: 'Hello', ts: '1790000001' } }]],
  ['notification silenced and passive', [], [
    { type: 'notification', data: { kind: 'info', title: 'Muted', ts: '1790000002', silenced: true } },
    { type: 'notification', data: { kind: 'info', title: 'Quiet', ts: '1790000003', priority: 'passive' } },
  ]],
  ['panel_published', [], [{ type: 'panel_published', data: { slug: 'crew' } }, { type: 'panel_published', data: { slug: '' } }]],
  ['notification ack, unack and clear', [], [
    { type: 'notification_ack', data: { ts: '1' } },
    { type: 'notification_unack', data: { ts: '1' } },
    { type: 'notifications_clear', data: {} },
  ]],
  ['approval in the owning slot', [], [{ type: 'approval', data: { ...approval, tool_call_id: 'tc-1', tool_purpose: 'List files' } }]],
  ['approval for a spawn', [], [{ type: 'approval', data: { id: 'spawn:agent-1', slot: ACTIVE, tool: 'spawn_run(write docs)', source: 'agent', ts: '1790000004' } }]],
  ['approval from a subagent', [], [{ type: 'approval', data: { id: 'ap-sub', slot: ACTIVE, tool: 'shell', source: 'subagent', ts: '1790000005' } }]],
  ['approval with no slot', [], [{ type: 'approval', data: { id: 'ap-free', tool: 'shell', source: 'cron', ts: '1790000006' } }]],
  ['approval_resolved for a coordinator approval', [{ type: 'approval', data: approval }], [{ type: 'approval_resolved', data: { id: 'ap-1', slot: ACTIVE, approved: true } }]],
  ['approval_resolved expired spawn', [{ type: 'approval', data: { id: 'spawn:agent-2', slot: ACTIVE, tool: 'spawn_run(x)', source: 'agent', ts: '1790000007' } }], [{ type: 'approval_resolved', data: { id: 'spawn:agent-2', slot: ACTIVE, approved: false, decision: 'expired' } }]],
  ['approval_resolved without a slot', [{ type: 'approval', data: approval }], [{ type: 'approval_resolved', data: { id: 'ap-1', approved: false } }]],
  ['refresh with history', [], [{ type: 'refresh', data: { kinds: ['history'] } }]],
  ['slot_clear active and background', [], [{ type: 'slot_clear', data: { slot: ACTIVE } }, { type: 'slot_clear', data: { slot: BACKGROUND } }]],
  ['slot_agent_switch', [], [{ type: 'slot_agent_switch', data: { slot: ACTIVE } }]],
  ['member_projection', [], [
    { type: 'member_projection', data: { slug: 'ada', key: 'roster', seq: 2, value: { starred: true } } },
    { type: 'member_projection', data: { slug: 'ada', key: 'roster' } },
  ]],
  ['members_subscribed truncating a torn tail', [{ type: 'member_projection', data: { slug: 'ada', key: 'roster', seq: 9, value: 1 } }], [{ type: 'members_subscribed', data: { lastSeqs: { ada: 3 } } }]],
  ['members_subscribed with nothing to drop', [], [{ type: 'members_subscribed', data: { lastSeqs: { ada: 3 } } }]],
  ['chat_message user row in the active slot', [], [{ type: 'chat_message', data: { slot: ACTIVE, role: 'user', content: 'hi', ts: TS } }]],
  ['chat_message assistant row in a background slot', [], [{ type: 'chat_message', data: { slot: BACKGROUND, role: 'assistant', content: 'done', ts: TS } }]],
  ['chat_message permission row in a background slot', [], [{ type: 'chat_message', data: { slot: BACKGROUND, role: 'permission', content: '[agent] shell', ts: TS } }]],
  ['chat_message permission row', [], [{ type: 'chat_message', data: { slot: ACTIVE, role: 'permission', content: '[agent] shell', ts: TS, meta: { approval_id: 'p-1' } } }]],
  ['chat_message resolved permission row', [], [{ type: 'chat_message', data: { slot: ACTIVE, role: 'permission', content: '[agent] shell', ts: TS, meta: { approval_id: 'p-2', resolved: true } } }]],
  ['chat_message subagent and tool rows', [], [
    { type: 'chat_message', data: { slot: ACTIVE, role: 'subagent', content: 'child', ts: TS } },
    { type: 'chat_message', data: { slot: ACTIVE, role: 'tool_result', content: 'out', ts: TS } },
  ]],
  ['chat_message passive note', [], [{ type: 'chat_message', data: { slot: ACTIVE, role: 'inject', cls: 'msg msg-note', content: 'note', ts: TS } }]],
  ['chat_message_update by tool call and by row', [], [
    { type: 'chat_message_update', data: { slot: ACTIVE, tool_call_id: 'tc-1', content: 'x' } },
    { type: 'chat_message_update', data: { slot: ACTIVE, ts: TS, mid: 'm-1', meta: { k: 1 } } },
  ]],
  ['queue family', [], [
    { type: 'queue_push', data: { slot: ACTIVE, content: 'later', queue_id: 'q-1', ts: TS } },
    { type: 'queue_edit', data: { slot: ACTIVE, queue_id: 'q-1', content: 'edited', meta: { files: [{ name: 'a' }] } } },
    { type: 'queue_reorder', data: { slot: ACTIVE, order: ['q-1'] } },
    { type: 'queue_cancel', data: { slot: ACTIVE, queue_id: 'q-1' } },
    { type: 'queue_pop', data: { slot: ACTIVE, queue_id: 'q-1' } },
  ]],
  ['steer_push', [{ type: 'chat_chunk', data: { slot: ACTIVE, seq: 1, content: 'pre' } }], [{
    type: 'steer_push', data: { slot: ACTIVE, content: 'steer', ts: TS, sendId: 's-1', steerState: 'written', mid: 'm-9', meta: { files: ['f'], dirs: ['d'] } },
  }]],
  ['steer_push without a slot', [], [{ type: 'steer_push', data: { content: 'steer' } }]],
  ['chat_chunk', [], [
    { type: 'chat_chunk', data: { slot: ACTIVE, seq: 1, content: 'Hello', gen: 'g-1' } },
    { type: 'chat_chunk', data: { slot: ACTIVE, seq: 1, content: 'Hello' } },
    { type: 'chat_chunk', data: { slot: ACTIVE, seq: 2, content: ' world' } },
  ]],
  ['tool_call and refinement', [], [
    { type: 'tool_call', data: { slot: ACTIVE, tool: 'Terminal', kind: 'execute', purpose: 'List files', input_preview: 'ls -la', is_shell: true, tool_call_id: 'tc-1' } },
    { type: 'tool_call', data: { slot: ACTIVE, tool: 'ls -la', kind: 'execute', input_preview: 'ls -la', is_shell: true, tool_call_id: 'tc-1', is_update: true } },
  ]],
  ['tool_result and mcp_app_render', [], [
    { type: 'tool_result', data: { slot: ACTIVE, output: 'ok', tool_call_id: 'tc-1' } },
    { type: 'mcp_app_render', data: { slot: ACTIVE, tool_call_id: 'tc-1', resource_uri: 'ui://x' } },
  ]],
  ['question_card new and repeated', [], [
    { type: 'question_card', data: { slot: ACTIVE, ask_id: 'ask-1', questions: [{ question: 'Which?' }] } },
    { type: 'question_card', data: { slot: ACTIVE, ask_id: 'ask-1', questions: [{ question: 'Which?' }] } },
  ]],
  ['question_card in a background slot', [], [{ type: 'question_card', data: { slot: BACKGROUND, card_id: 'card-1', questions: [{ question: 'Which?' }] } }]],
  ['question_card_resolved', [{ type: 'question_card', data: { slot: ACTIVE, ask_id: 'ask-1', questions: [{ question: 'Which?' }] } }], [
    { type: 'question_card_resolved', data: { ask_id: 'ask-1' } },
    { type: 'question_card_resolved', data: { card_id: 'never-held' } },
  ]],
  ['followup_card', [], [
    { type: 'followup_card', data: { slot: ACTIVE, ts: 5, items: [{ title: 'Next', prompt: 'do it', description: 3, branch: 'b' }, { title: 1 }] } },
    { type: 'followup_card', data: { slot: ACTIVE, items: [] } },
  ]],
  ['slot_read', [], [{ type: 'slot_read', data: { slot: ACTIVE, read_ts: TS } }, { type: 'slot_read', data: { slot: ACTIVE, read_ts: '' } }, { type: 'slot_read', data: {} }]],
  ['slot_folder_suggestion', [], [
    { type: 'slot_folder_suggestion', data: { slot: ACTIVE, folder_id: 'f1', folder_name: 'Work', breadcrumb: 'A / Work', ts: 7 } },
    { type: 'slot_folder_suggestion', data: { slot: ACTIVE, folder_id: 'f1' } },
  ]],
  ['activity_event', [], [
    { type: 'activity_event', data: { slot: ACTIVE, kind: 'session', text: 'spawned', spawned: true } },
    { type: 'activity_event', data: { slot: ACTIVE, kind: 'session', text: 'warm' } },
  ]],
  ['subagent lifecycle', [], [
    { type: 'subagent_spawn', data: { slot: ACTIVE, id: 'sa-1', task: 't', agent: 'a' } },
    { type: 'subagent_queued', data: { slot: ACTIVE, queued: 2, reason: 'memory' } },
    { type: 'subagent_chunk', data: { slot: ACTIVE, id: 'sa-1', text: 'partial' } },
    { type: 'subagent_tool', data: { slot: ACTIVE, id: 'sa-1', tool: 'read', turns: 1 } },
    { type: 'subagent_stalled', data: { slot: ACTIVE, id: 'sa-1', stalled: true } },
    { type: 'subagent_chunk', data: { slot: ACTIVE, id: 'sa-1', text: 'more' } },
    { type: 'subagent_retrying', data: { slot: ACTIVE, id: 'sa-1', attempt: 2 } },
    { type: 'subagent_chunk', data: { slot: ACTIVE, id: 'sa-1', text: 'again' } },
    { type: 'subagent_recovering', data: { slot: ACTIVE, id: 'sa-1' } },
    { type: 'subagent_chunk', data: { slot: ACTIVE, id: 'sa-1', text: 'last' } },
    { type: 'subagent_done', data: { slot: ACTIVE, id: 'sa-1', elapsed: 3, credits: 1.5 } },
  ]],
  ['subagent snapshot and batches', [{ type: 'subagent_chunk', data: { slot: ACTIVE, id: 'sa-2', text: 'buffered' } }], [
    { type: 'subagent_snapshot', data: { slot: ACTIVE, id: 'sa-2', task: 't', agent: 'a', streaming: 'all', last_tool: '', started: 1 } },
    { type: 'subagent_chunk', data: { slot: ACTIVE, id: 'sa-3', text: 'x' } },
    { type: 'subagent_batch_update', data: { updates: [{ slot: ACTIVE, id: 'sa-3', attempt: 2 }, { slot: ACTIVE, id: 'sa-4', tool: 'grep' }] } },
    { type: 'subagent_chunk', data: { slot: ACTIVE, id: 'sa-5', text: 'y' } },
    { type: 'subagent_batch_chunks', data: { chunks: [{ slot: ACTIVE, id: 'sa-5', text: 'z' }] } },
    { type: 'subagent_chunk', data: { slot: ACTIVE, id: 'sa-6', text: 'w' } },
    { type: 'subagent_snapshot_batch', data: { items: [
      { type: 'subagent_snapshot', data: { slot: ACTIVE, id: 'sa-7', task: 't', agent: 'a', streaming: '', last_tool: '', started: 1 } },
      { type: 'subagent_done', data: { slot: ACTIVE, id: 'sa-6', elapsed: 1 } },
      { type: 'other', data: {} },
    ] } },
  ]],
  ['subagent status and text', [], [
    { type: 'subagent_status', data: { slot: ACTIVE, running: 1 } },
    { type: 'subagent_status', data: { running: 1 } },
    { type: 'subagent_text', data: { slot: ACTIVE, id: 'sa-1', text: 't' } },
    { type: 'subagent_text', data: { slot: ACTIVE, text: 't' } },
  ]],
  ['app_reload', [], [{ type: 'app_reload', data: { app: 'notes' } }]],
  ['wave markers and heartbeat', [], [
    { type: 'spawn_batch_started', data: {} },
    { type: 'batch_finished', data: {} },
    { type: 'heartbeat', data: {} },
  ]],
  ['workflow_run_event progress and a terminal status', [], [
    { type: 'workflow_run_event', data: { run_id: 'r-2', seq: 1, type: 'step_started', data: {} } },
    { type: 'workflow_run_event', data: { run_id: 'r-2', seq: 2, type: 'run_failed', data: {} } },
  ]],
  ['workflow_run_event and side_result', [], [
    { type: 'workflow_run_event', data: { run_id: 'r-1', seq: 1, type: 'run_started', data: { session_key: ACTIVE } } },
    { type: 'chat.side_result', data: { slot: ACTIVE, run_id: 'side-1', role: 'assistant', content: 'aside', final: true } },
  ]],
  ['chat.thread_reply', [], [
    { type: 'chat.thread_reply', data: { slot: ACTIVE, mid: 'm-1', role: 'assistant', delta: 'part' } },
    { type: 'chat.thread_reply', data: { slot: ACTIVE, mid: 'm-1', role: 'assistant', final: true, content: 'all' } },
    { type: 'chat.thread_reply', data: { slot: ACTIVE, mid: 'm-1', role: 'user', content: 'reply' } },
    { type: 'chat.thread_reply', data: { slot: ACTIVE, role: 'user' } },
  ]],
  ['chat.side_queue', [], [
    { type: 'chat.side_queue', data: { slot: ACTIVE, action: 'push', queue_id: 'sq-1', content: 'aside', raw: true } },
    { type: 'chat.side_queue', data: { slot: ACTIVE, action: 'cancel', queue_id: 'sq-1', origin_client: 'another-tab' } },
  ]],
  ['context_usage', [], [{ type: 'context_usage', data: { slot: ACTIVE, pct: 40 } }]],
  ['chat_thinking', [], [
    { type: 'chat_thinking', data: { slot: ACTIVE, content: 'mull' } },
    { type: 'chat_thinking', data: { slot: ACTIVE, content: 'more' } },
  ]],
  ['chat_segment', [{ type: 'chat_chunk', data: { slot: ACTIVE, seq: 1, content: 'a' } }], [{ type: 'chat_segment', data: { slot: ACTIVE } }]],
  ['chat_status and variant switch', [], [
    { type: 'chat_status', data: { slot: ACTIVE, status: 'Compacting' } },
    { type: 'chat_status', data: { slot: ACTIVE, status: '' } },
    { type: 'chat_variant_switch', data: { slot: ACTIVE } },
  ]],
  ['chat_done in the active slot', [{ type: 'chat_chunk', data: { slot: ACTIVE, seq: 1, content: 'answer' } }], [{ type: 'chat_done', data: { slot: ACTIVE, ts: TS } }]],
  ['chat_done in a background slot', [], [{ type: 'chat_done', data: { slot: BACKGROUND, ts: TS } }]],
  ['chat_done needing input', [], [{ type: 'chat_done', data: { slot: BACKGROUND, ts: TS, needs_input: true, continuing: false } }]],
  ['chat_done still continuing', [], [{ type: 'chat_done', data: { slot: BACKGROUND, ts: TS, continuing: true } }]],
  ['autonudge_state update and removal', [], [
    { type: 'autonudge_state', data: { slot: ACTIVE, event: 'updated', loop: { goal: 'ship', interval_secs: 60, cycles: 1, max_cycles: 3, active: true } } },
    { type: 'autonudge_state', data: { slot: ACTIVE, event: 'removed' } },
    { type: 'autonudge_state', data: { event: 'updated' } },
  ]],
  ['voice frames without a request', [], [
    { type: 'voice_chunk', data: { slot: ACTIVE, audio: 'AAAA', request_id: 'nope' } },
    { type: 'voice_complete', data: { slot: ACTIVE, request_id: 'nope' } },
    { type: 'voice_error', data: { slot: ACTIVE, request_id: 'nope', code: 'x' } },
  ]],
  ['sessions_restarting and refine', [], [
    { type: 'sessions_restarting', data: { status: 'restarting' } },
    { type: 'refine', data: {} },
  ]],
  ['update_progress', [], [
    { type: 'update_progress', data: { step: 'downloading', detail: '1/2' } },
    { type: 'update_progress', data: { step: 'restarting', detail: '' } },
    { type: 'update_progress', data: { step: 'failed', detail: 'boom' } },
    { type: 'update_progress', data: { step: 'done', detail: '' } },
  ]],
  ['channel frames', [], [
    { type: 'channel_message', data: { id: 1 } },
    { type: 'channel_agent_status', data: { id: 2 } },
    { type: 'channel_created', data: { id: 3 } },
    { type: 'channel_closed', data: { id: 4 } },
    { type: 'channel_agent_joined', data: { id: 5 } },
    { type: 'channel_agent_left', data: { id: 6 } },
  ]],
  ['cron_history', [], [{ type: 'cron_history', data: { job: 'nightly' } }]],
  ['source_status', [], [
    { type: 'source_status', data: { url: 'https://example.test/pr/1', state: 'merged', ci: 'passed', origin: 'chip' } },
    { type: 'source_status', data: { state: 'merged' } },
  ]],
  ['computer_use_frame', [], [{ type: 'computer_use_frame', data: { jpeg: 'AAAA', w: 10, h: 10 } }]],
  ['tool_call with a null payload', [], [{ type: 'tool_call', data: null }]],
  ['approval without an id', [], [{ type: 'approval', data: { slot: ACTIVE, tool: 'shell', source: 'agent', ts: '1790000008' } }]],
  ['unknown and prototype-named types', [], [
    { type: 'no_such_frame', data: {} },
    { type: 'constructor', data: {} },
    { type: '__proto__', data: {} },
    { type: 'toString', data: {} },
  ]],
]

describe('useWebSocket frame routing trace', () => {
  it.each(FRAME_CASES)('%s', async (name, setup, frames) => {
    const got = await run(setup, frames)
    expect(got).toEqual(EXPECTED_FRAMES[name])
  })

  it('swallows a malformed frame and keeps routing the next one', async () => {
    const { ws } = await mountOpen()
    act(() => {
      ws.raw('not json')
      ws.raw('null')
      ws.raw('{"type":"slot_title"}')
      ws.frame({ type: 'slot_title', data: { key: ACTIVE, title: 'After' } })
    })
    // Unparseable JSON and a `null` envelope never reach a handler; a frame
    // whose reducer throws on a missing payload is swallowed after dispatch.
    expect(trace).toEqual([
      'action dashboard/sseSlotTitle undefined',
      'action dashboard/sseSlotTitle {"key":"slot-a","title":"After"}',
    ])
  })
})

describe('useWebSocket lifecycle trace', () => {
  it('first connect', async () => {
    testStore.dispatch(sseSlots(SLOTS))
    testStore.dispatch(setActiveSlot(ACTIVE))
    trace = []
    const view = renderHook(() => useWebSocket(), {
      wrapper: ({ children }) => createElement(Provider, { store: testStore },
        createElement(QueryClientProvider, { client: qc }, children)),
    })
    act(() => { view.result.current.subscribeLogs(() => {}) })
    await act(async () => { WS_INSTANCES[0].open() })
    expect(trace).toEqual(EXPECTED_LIFECYCLE['first connect'])
  })

  it('log and subagent subscriptions follow the callers, and log frames reach the callback', async () => {
    const { ws, view } = await mountOpen()
    act(() => { view.result.current.subscribeLogs(d => { trace.push(`log ${scrub(d)}`) }) })
    act(() => { ws.frame({ type: 'log', data: { level: 'info', msg: 'one' } }) })
    act(() => { view.result.current.subscribeLogs(null) })
    act(() => { ws.frame({ type: 'log', data: { level: 'info', msg: 'two' } }) })
    act(() => { view.result.current.subscribeSubagents(false) })
    act(() => { view.result.current.subscribeSubagents(true) })
    expect(trace).toEqual([
      'send {"type":"subscribe_logs"}',
      'log {"level":"info","msg":"one"}',
      'send {"type":"unsubscribe_logs"}',
      'send {"type":"unsubscribe_subagents"}',
      'send {"type":"subscribe_subagents"}',
    ])
  })

  it('reconnect catch-up', async () => {
    const { ws } = await mountOpen()
    act(() => {
      ws.frame({ type: 'chat_thinking', data: { slot: ACTIVE, content: 'kept' } })
      ws.frame({ type: 'chat_chunk', data: { slot: ACTIVE, seq: 1, content: 'dropped' } })
      ws.frame({ type: 'subagent_chunk', data: { slot: ACTIVE, id: 'sa-1', text: 'dropped' } })
      ws.frame({ type: 'queue_push', data: { slot: ACTIVE, content: 'q', queue_id: 'q-9', ts: TS } })
    })
    trace = []
    vi.useFakeTimers()
    try {
      act(() => { ws.drop() })
      act(() => { vi.advanceTimersByTime(1000) })
    } finally {
      vi.useRealTimers()
    }
    await act(async () => { WS_INSTANCES[1].open() })
    act(() => { drainFrames() })
    expect(trace).toEqual(EXPECTED_LIFECYCLE['reconnect catch-up'])
  })

  it('reconnect after an update restart reloads', async () => {
    const { ws } = await mountOpen()
    act(() => { ws.frame({ type: 'update_progress', data: { step: 'restarting', detail: '' } }) })
    trace = []
    vi.useFakeTimers()
    try {
      act(() => { ws.drop() })
      act(() => { vi.advanceTimersByTime(1000) })
    } finally {
      vi.useRealTimers()
    }
    await act(async () => { WS_INSTANCES[1].open() })
    expect(trace).toEqual(EXPECTED_LIFECYCLE['reconnect after an update restart reloads'])
  })

  it('backoff doubles to a 10s cap and an open resets it', async () => {
    const { ws } = await mountOpen()
    /** Advance in 500 ms steps until a new socket is constructed, and return
     *  how long that took. Bounded well past the 10 s cap, so a regression
     *  fails here instead of spinning the worker. */
    const waitForNextSocket = () => {
      const before = WS_INSTANCES.length
      for (let waited = 500; waited <= 30_000; waited += 500) {
        act(() => { vi.advanceTimersByTime(500) })
        if (WS_INSTANCES.length > before) return waited
      }
      throw new Error('no reconnect within 30s')
    }
    vi.useFakeTimers()
    try {
      act(() => { ws.drop() })
      const delays: number[] = []
      for (let i = 0; i < 6; i += 1) {
        delays.push(waitForNextSocket())
        act(() => { WS_INSTANCES[WS_INSTANCES.length - 1].drop() })
      }
      expect(delays).toEqual([1000, 2000, 4000, 8000, 10000, 10000])
      waitForNextSocket()
      act(() => { WS_INSTANCES[WS_INSTANCES.length - 1].open() })
      act(() => { WS_INSTANCES[WS_INSTANCES.length - 1].drop() })
      expect(waitForNextSocket()).toBe(1000)
    } finally {
      vi.useRealTimers()
    }
  })

  it('uses a secure socket on https and keeps the socket across a re-render', async () => {
    const original = window.location
    const { ws, view } = await mountOpen()
    expect(ws.url).toMatch(/^ws:\/\/[^/]+\/api\/ws\?caps=slot_patch$/)
    // A re-render keeps the socket: no owner identity changes, so neither
    // the mount effect nor the connect it runs fires again.
    act(() => { view.rerender() })
    expect(WS_INSTANCES).toHaveLength(1)
    view.unmount()
    vi.stubGlobal('location', { ...original, protocol: 'https:', host: 'crew.test' })
    WS_INSTANCES.length = 0
    renderHook(() => useWebSocket(), {
      wrapper: ({ children }) => createElement(Provider, { store: testStore },
        createElement(QueryClientProvider, { client: qc }, children)),
    })
    expect(WS_INSTANCES[0].url).toBe('wss://crew.test/api/ws?caps=slot_patch')
  })

  it('unmount', async () => {
    const { ws, view } = await mountOpen()
    act(() => {
      ws.frame({ type: 'chat_thinking', data: { slot: ACTIVE, content: 'salvaged' } })
      ws.frame({ type: 'chat_chunk', data: { slot: ACTIVE, seq: 1, content: 'kept in buffer' } })
      ws.frame({ type: 'subagent_chunk', data: { slot: ACTIVE, id: 'sa-1', text: 'flushed' } })
      ws.frame({ type: 'queue_push', data: { slot: ACTIVE, content: 'q', queue_id: 'q-8', ts: TS } })
    })
    trace = []
    view.unmount()
    expect(trace).toEqual(EXPECTED_LIFECYCLE['unmount'])
    trace = []
    vi.useFakeTimers()
    try {
      act(() => { vi.advanceTimersByTime(20_000) })
    } finally {
      vi.useRealTimers()
    }
    expect(WS_INSTANCES).toHaveLength(1)
  })

  it('forceReconnect', async () => {
    const { ws, view } = await mountOpen()
    trace = []
    vi.useFakeTimers()
    try {
      act(() => { view.result.current.forceReconnect() })
      expect(ws.onclose).toBeNull()
      expect(ws.onerror).toBeNull()
      act(() => { vi.advanceTimersByTime(0) })
    } finally {
      vi.useRealTimers()
    }
    await act(async () => { WS_INSTANCES[1].open() })
    expect(trace).toEqual(EXPECTED_LIFECYCLE['forceReconnect'])
  })

  it('StrictMode mounts twice and connects once', async () => {
    testStore.dispatch(sseSlots(SLOTS))
    testStore.dispatch(setActiveSlot(ACTIVE))
    trace = []
    renderHook(() => useWebSocket(), {
      wrapper: ({ children }) => createElement(StrictMode, null,
        createElement(Provider, { store: testStore },
          createElement(QueryClientProvider, { client: qc }, children))),
    })
    const first = WS_INSTANCES[0]
    act(() => { first.drop() })
    await act(async () => { WS_INSTANCES[WS_INSTANCES.length - 1].open() })
    expect(trace).toEqual(EXPECTED_LIFECYCLE['StrictMode mounts twice and connects once'])
  })

  it('a stream hold defers all three pipelines to one timer each', async () => {
    const { ws } = await mountOpen()
    vi.useFakeTimers()
    try {
      holdStreamingFlushes(400)
      act(() => {
        ws.frame({ type: 'chat_chunk', data: { slot: ACTIVE, seq: 1, content: 'held' } })
        ws.frame({ type: 'subagent_chunk', data: { slot: ACTIVE, id: 'sa-1', text: 'held' } })
        ws.frame({ type: 'queue_push', data: { slot: ACTIVE, content: 'q', queue_id: 'q-7', ts: TS } })
      })
      trace.push('before hold ends')
      act(() => { vi.advanceTimersByTime(415) })
      trace.push('hold ended')
      act(() => { vi.advanceTimersByTime(1) })
    } finally {
      releaseStreamingFlushes()
      vi.useRealTimers()
    }
    expect(rafQueue).toHaveLength(0)
    expect(trace).toEqual(EXPECTED_LIFECYCLE['a stream hold defers all three pipelines to one timer each'])
  })

  it('without requestAnimationFrame every pipeline flushes on a 16ms timer', async () => {
    const { ws } = await mountOpen()
    vi.stubGlobal('requestAnimationFrame', undefined)
    vi.stubGlobal('cancelAnimationFrame', undefined)
    vi.useFakeTimers()
    try {
      act(() => {
        ws.frame({ type: 'chat_chunk', data: { slot: ACTIVE, seq: 1, content: 'timed' } })
        ws.frame({ type: 'subagent_chunk', data: { slot: ACTIVE, id: 'sa-1', text: 'timed' } })
        ws.frame({ type: 'queue_push', data: { slot: ACTIVE, content: 'q', queue_id: 'q-6', ts: TS } })
      })
      trace.push('before 16ms')
      act(() => { vi.advanceTimersByTime(15) })
      trace.push('15ms')
      act(() => { vi.advanceTimersByTime(1) })
    } finally {
      vi.useRealTimers()
    }
    expect(trace).toEqual(EXPECTED_LIFECYCLE['without requestAnimationFrame every pipeline flushes on a 16ms timer'])
  })

  it('voice frames for a registered request', async () => {
    const { ws } = await mountOpen()
    vi.stubGlobal('Audio', class {
      onended: (() => void) | null = null
      onerror: (() => void) | null = null
      constructor(public src: string) { trace.push(`audio ${src}`) }
      pause() { trace.push('audio pause') }
      play() { trace.push('audio play'); return Promise.resolve() }
    })
    const created = vi.fn(() => 'blob:voice')
    const revoked = vi.fn((url: string) => { trace.push(`revoke ${url}`) })
    vi.stubGlobal('URL', Object.assign(Object.create(URL), { createObjectURL: created, revokeObjectURL: revoked }))
    trace = []
    act(() => {
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot: ACTIVE, request_id: 'r-1' } }))
      ws.frame({ type: 'voice_chunk', data: { slot: ACTIVE, audio: btoa('abc'), audioMime: 'audio/mpeg', request_id: 'r-1' } })
      ws.frame({ type: 'voice_chunk', data: { slot: BACKGROUND, audio: btoa('abc'), request_id: 'r-1' } })
      ws.frame({ type: 'voice_complete', data: { slot: ACTIVE, audio: 'QQ==', request_id: 'r-1' } })
      ws.frame({ type: 'voice_complete', data: { slot: ACTIVE, audio: 'QQ==', request_id: 'r-1' } })
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot: ACTIVE, request_id: 'r-2' } }))
      ws.frame({ type: 'voice_error', data: { slot: ACTIVE, request_id: 'r-2', code: 'voice_cancelled' } })
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot: ACTIVE, request_id: 'r-3' } }))
      ws.frame({ type: 'voice_error', data: { slot: ACTIVE, request_id: 'r-3', code: 'voice_synthesis_failed' } })
      window.dispatchEvent(new CustomEvent('voice-stop'))
    })
    expect(trace).toEqual(EXPECTED_LIFECYCLE['voice frames for a registered request'])
  })

  it('with the unread-on-attention opt-in, an off-screen approval and question chime before they mark the slot unread', async () => {
    localStorage.setItem('mc-unread-on-attention', '1')
    const { ws } = await mountOpen()
    act(() => {
      ws.frame({ type: 'approval', data: { id: 'ap-bg', slot: BACKGROUND, tool: 'shell', source: 'agent', ts: '1790000008' } })
      ws.frame({ type: 'question_card', data: { slot: BACKGROUND, card_id: 'card-bg', questions: [{ question: 'Which?' }] } })
    })
    // Every other scenario runs with the opt-in off (localStorage is cleared),
    // so this is the one that pins the chime-then-badge order in both arms.
    expect(trace.filter(line => line.startsWith('event mc-notification') || line.includes('markSlotUnread'))).toEqual([
      'event mc-notification {"kind":"approval"}',
      'action dashboard/markSlotUnread {"slot":"slot-b"}',
      'event mc-notification {"kind":"approval"}',
      'action dashboard/markSlotUnread {"slot":"slot-b"}',
    ])
  })

  it('frames inside the reconnect catch-up window', async () => {
    const { ws } = await mountOpen()
    vi.useFakeTimers()
    try {
      act(() => { ws.drop() })
      act(() => { vi.advanceTimersByTime(1000) })
    } finally {
      vi.useRealTimers()
    }
    trace = []
    act(() => {
      WS_INSTANCES[1].open()
      trace.push('opened')
      WS_INSTANCES[1].frame({ type: 'notification', data: { kind: 'info', title: 'Replayed', ts: '1790000009' } })
      WS_INSTANCES[1].frame({ type: 'approval', data: { ...approval, id: 'ap-replay' } })
      WS_INSTANCES[1].frame({ type: 'chat_message', data: { slot: BACKGROUND, role: 'assistant', content: 'old', ts: TS } })
      WS_INSTANCES[1].frame({ type: 'question_card', data: { slot: ACTIVE, ask_id: 'ask-9', questions: [{ question: 'Which?' }] } })
      WS_INSTANCES[1].frame({ type: 'chat_done', data: { slot: BACKGROUND, ts: TS } })
    })
    trace = trace.slice(trace.indexOf('opened') + 1)
    expect(trace).toEqual(EXPECTED_LIFECYCLE['frames inside the reconnect catch-up window'])
  })

  it('an overflowing chunk lands its status before the flush; reasoning after it', async () => {
    const { ws } = await mountOpen()
    trace = []
    act(() => {
      ws.frame({ type: 'chat_chunk', data: { slot: ACTIVE, seq: 1, content: 'x'.repeat(50_001) } })
      ws.frame({ type: 'chat_thinking', data: { slot: BACKGROUND, content: 'y'.repeat(50_001) } })
    })
    expect(trace.map(line => line.slice(0, 120))).toEqual(EXPECTED_LIFECYCLE['an overflowing chunk lands its status before the flush; reasoning after it'])
  })

  it('turn boundaries speak the tail and keep the delivery floor across a segment', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.voiceConfig).mockResolvedValue({ autoSpeak: true } as never)
    vi.mocked(api.voiceSynthesize).mockImplementation(((slot: string, text: string) => {
      trace.push(`synthesize ${slot} ${JSON.stringify(text)}`)
      return Promise.resolve({ ok: true })
    }) as never)
    const { ws } = await mountOpen()
    trace = []
    await act(async () => {
      ws.frame({ type: 'chat_chunk', data: { slot: ACTIVE, seq: 1, content: 'First sentence. Second' } })
      drainFrames()
      ws.frame({ type: 'chat_segment', data: { slot: ACTIVE } })
      ws.frame({ type: 'chat_chunk', data: { slot: ACTIVE, seq: 1, content: 'replayed' } })
      ws.frame({ type: 'chat_chunk', data: { slot: ACTIVE, seq: 2, content: 'After tool. Tail' } })
      drainFrames()
      ws.frame({ type: 'chat_done', data: { slot: ACTIVE, ts: TS } })
      await new Promise(r => setTimeout(r, 0))
    })
    expect(trace.filter(line => !line.startsWith('query ') && !line.includes('/pending') && !line.includes('/fulfilled')))
      .toEqual(EXPECTED_LIFECYCLE['turn boundaries speak the tail and keep the delivery floor across a segment'])
  })

  it('unmount during backoff still lands the buffered recency bump', async () => {
    const { ws, view } = await mountOpen()
    vi.useFakeTimers()
    try {
      act(() => {
        ws.frame({ type: 'queue_push', data: { slot: ACTIVE, content: 'q', queue_id: 'q-5', ts: TS } })
        ws.drop()
      })
      trace = []
      view.unmount()
      act(() => { vi.advanceTimersByTime(20_000) })
    } finally {
      vi.useRealTimers()
    }
    expect(trace).toEqual(EXPECTED_LIFECYCLE['unmount during backoff still lands the buffered recency bump'])
    expect(WS_INSTANCES).toHaveLength(1)
  })

  it('focus changes, visibility and the pane-focus emitter share one sender', async () => {
    const { emitSlotFocused } = await import('../hooks/useWebSocket')
    const { view } = await mountOpen()
    act(() => { testStore.dispatch(setActiveSlot(BACKGROUND)) })
    act(() => { emitSlotFocused('pane-slot') })
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    hidden.mockReturnValue(false)
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    act(() => { window.dispatchEvent(new Event('focus')) })
    view.unmount()
    act(() => { emitSlotFocused('after-unmount') })
    expect(trace.filter(line => line.startsWith('send ') || line.startsWith('close'))).toEqual(EXPECTED_LIFECYCLE['focus sender'])
  })
})

/** The recorded contract: any difference from these traces is a behaviour change. */
const EXPECTED_FRAMES: Record<string, string[]> = {
  "dashboard status": [
    'action dashboard/sseStatus {"version":"1.0","bundle_id":"b1"}',
  ],
  "dashboard version change reloads": [
    'reload',
  ],
  "dashboard bundle change reloads": [
    'reload',
  ],
  "slots first frame of a connection": [
    'action dashboard/sseSlots [{"key":"slot-a","title":"Active","last_ts":"2026-09-01T00:00:00.000Z"},{"key":"slot-b","title":"Background","last_ts":"2026-09-01T00:00:00.000Z"}]',
    'action dashboard/sseYolo true',
    'action dashboard/setChannelTrusted false',
    'query setQueryData ["chat-folders"]',
    'query invalidateQueries ["chat-folders"]',
    'query invalidateQueries ["dashboardConfig"]',
    'event mc:app:slots',
  ],
  "slots repeated frame is skipped": [],
  "slots unchanged generations": [
    'action dashboard/sseSlots [{"key":"slot-b","title":"Background","last_ts":"2026-09-01T00:00:00.000Z"},{"key":"slot-a","title":"Active","last_ts":"2026-09-01T00:00:00.000Z"}]',
    'event mc:app:slots',
  ],
  "credential_redaction_changed": [
    'query setQueryData ["credential-redaction"]',
    'query invalidateQueries ["credential-redaction"]',
    'query resetQueries ["file-read"]',
    'query resetQueries ["file-diff"]',
  ],
  "credential_redaction_changed without a boolean": [
    'query invalidateQueries ["credential-redaction"]',
    'query resetQueries ["file-read"]',
    'query resetQueries ["file-diff"]',
  ],
  "skills.pending_changed": [
    'query invalidateQueries ["skills-pending"]',
    'query invalidateQueries ["skills"]',
  ],
  "todo_update": [
    'action dashboard/sseTodoUpdate {"slot":"slot-a","todo":{"items":[]}}',
  ],
  "mcp_report_update": [
    'action dashboard/sseMcpReportUpdate {"slot":"slot-a","mcp_report":null}',
  ],
  "slot_title": [
    'action dashboard/sseSlotTitle {"key":"slot-a","title":"Renamed"}',
  ],
  "slot_patch with an unknown row": [
    'action dashboard/fetchSlots/pending',
    'action dashboard/sseSlotPatch {"slots":[{"key":"slot-new","title":"New"}],"removed":[]}',
    'event mc:app:slots',
  ],
  "slot_patch removing a row": [
    'query resetQueries ["dashboard-card","slot-gone"]',
    'action dashboard/sseSlotPatch {"slots":[{"key":"slot-gone","title":"Gone"}],"removed":["slot-gone"]}',
    'event mc:app:slots',
  ],
  "dashboard_card update, removal and no slot": [
    'query invalidateQueries ["dashboard-card","slot-a"]',
    'query resetQueries ["dashboard-card","slot-b"]',
  ],
  "slot_projection for a worker and its creator": [
    'query invalidateQueries ["command-center","slot-b","work"] {"exact":true} {"cancelRefetch":false}',
    'query invalidateQueries ["command-center","slot-a","work"] {"exact":true} {"cancelRefetch":false}',
  ],
  "session_summary": [
    'query invalidateQueries ["session-summary","slot-a"]',
  ],
  "pins_changed": [
    'query invalidateQueries ["chat-pins","slot-a"]',
  ],
  "artifact_update": [
    'query invalidateQueries ["artifact","doc"]',
    'query invalidateQueries ["artifact-versions","doc"]',
    'query invalidateQueries ["artifact-events","doc"]',
    'query invalidateQueries ["artifact-comments","doc"]',
    'query invalidateQueries ["artifacts"]',
    'query invalidateQueries ["command-center","artifacts"]',
  ],
  "artifact_update deleted": [
    'event kirocrew:artifact-deleted {"slug":"doc"}',
    'query invalidateQueries ["artifacts"]',
    'query invalidateQueries ["command-center","artifacts"]',
  ],
  "notification": [
    'action notifications/addNotification {"kind":"info","title":"Hello","ts":"1790000001"}',
    'event mc-notification {"kind":"info"}',
    'event mc-live-notification {"note":{"kind":"info","title":"Hello","ts":"1790000001"}}',
  ],
  "notification silenced and passive": [
    'action notifications/addNotification {"kind":"info","title":"Muted","ts":"1790000002","silenced":true}',
    'event mc-live-notification {"note":{"kind":"info","title":"Muted","ts":"1790000002","silenced":true}}',
    'action notifications/addNotification {"kind":"info","title":"Quiet","ts":"1790000003","priority":"passive"}',
    'event mc-live-notification {"note":{"kind":"info","title":"Quiet","ts":"1790000003","priority":"passive"}}',
  ],
  "panel_published": [
    'query invalidateQueries ["member-panel","crew"]',
  ],
  "notification ack, unack and clear": [
    'action notifications/ackNotificationByTs "1"',
    'action notifications/unackNotificationByTs "1"',
    'action notifications/clearAllNotifications undefined',
  ],
  "approval in the owning slot": [
    'query invalidateQueries ["global-approvals"]',
    'event mc-notification {"kind":"approval"}',
    "action notifications/addNotification {\"kind\":\"approval\",\"title\":\"Tool approval: shell\",\"body\":\"**Source:** agent\\n\\n```approval-command\\nls\\n```\\n\\nList files\",\"ts\":\"1790000000\",\"approval_id\":\"ap-1\",\"slot\":\"slot-a\"}",
    "event mc-live-notification {\"note\":{\"kind\":\"approval\",\"title\":\"Tool approval: shell\",\"body\":\"**Source:** agent\\n\\n```approval-command\\nls\\n```\\n\\nList files\",\"ts\":\"1790000000\",\"approval_id\":\"ap-1\",\"slot\":\"slot-a\"}}",
    'action chat/sseChatMessage {"slot":"slot-a","role":"permission","content":"[agent] shell","ts":"1790000000","meta":{"tool_input":"ls","approval_id":"ap-1","source":"agent","registry":"coordinator","tool_call_id":"tc-1"}}',
    'action chat/sseActivityEvent {"slot":"slot-a","kind":"approval","text":"shell","approval_id":"ap-1","approval_type":"chat"}',
  ],
  "approval for a spawn": [
    'query invalidateQueries ["global-approvals"]',
    'event mc-notification {"kind":"approval"}',
    'action notifications/addNotification {"kind":"approval","title":"Tool approval: spawn_run(write docs)","body":"**Source:** agent","ts":"1790000004","approval_id":"spawn:agent-1","slot":"slot-a"}',
    'event mc-live-notification {"note":{"kind":"approval","title":"Tool approval: spawn_run(write docs)","body":"**Source:** agent","ts":"1790000004","approval_id":"spawn:agent-1","slot":"slot-a"}}',
    'action chat/sseChatMessage {"slot":"slot-a","role":"permission","content":"[agent] spawn_run(write docs)","ts":"1790000004","meta":{"tool_input":"","approval_id":"spawn:agent-1","source":"agent","registry":"coordinator"}}',
    'action chat/sseSubagentPending {"slot":"slot-a","id":"agent-1","task":"write docs","approval_id":"spawn:agent-1"}',
  ],
  "approval from a subagent": [
    'query invalidateQueries ["global-approvals"]',
    'event mc-notification {"kind":"approval"}',
    'action notifications/addNotification {"kind":"approval","title":"Tool approval: shell","body":"**Source:** subagent","ts":"1790000005","approval_id":"ap-sub","slot":"slot-a"}',
    'event mc-live-notification {"note":{"kind":"approval","title":"Tool approval: shell","body":"**Source:** subagent","ts":"1790000005","approval_id":"ap-sub","slot":"slot-a"}}',
    'action chat/sseChatMessage {"slot":"slot-a","role":"permission","content":"[subagent] shell","ts":"1790000005","meta":{"tool_input":"","approval_id":"ap-sub","source":"subagent","registry":"coordinator"}}',
  ],
  "approval with no slot": [
    'query invalidateQueries ["global-approvals"]',
    'event mc-notification {"kind":"approval"}',
    'action notifications/addNotification {"kind":"approval","title":"Tool approval: shell","body":"**Source:** cron","ts":"1790000006","approval_id":"ap-free"}',
    'event mc-live-notification {"note":{"kind":"approval","title":"Tool approval: shell","body":"**Source:** cron","ts":"1790000006","approval_id":"ap-free"}}',
  ],
  "approval_resolved for a coordinator approval": [
    'query invalidateQueries ["global-approvals"]',
    'action notifications/retireApprovalNote {"ts":"1790000000","why":"approve"}',
    'action notifications/ack/pending',
    'action chat/resolveByApprovalId {"id":"ap-1","slot":"slot-a","decision":"approved","registry":"coordinator"}',
    'action chat/sseActivityEvent {"slot":"slot-a","kind":"approval_resolved","text":"","approval_id":"ap-1","approval_type":"chat"}',
  ],
  "approval_resolved expired spawn": [
    'query invalidateQueries ["global-approvals"]',
    'action notifications/retireApprovalNote {"ts":"1790000007","why":"gone"}',
    'action notifications/ack/pending',
    'action chat/resolveByApprovalId {"id":"spawn:agent-2","slot":"slot-a","decision":"stale","registry":"coordinator"}',
    'action chat/sseActivityEvent {"slot":"slot-a","kind":"approval_resolved","text":"","approval_id":"spawn:agent-2","approval_type":"spawn"}',
    'action chat/sseSubagentDone {"slot":"slot-a","id":"agent-2","elapsed":0,"error":"The approval wait expired, so the request was denied."}',
  ],
  "approval_resolved without a slot": [
    'query invalidateQueries ["global-approvals"]',
    'action notifications/retireApprovalNote {"ts":"1790000000","why":"reject"}',
    'action notifications/ack/pending',
    'action chat/resolveByApprovalId {"id":"ap-1","slot":"slot-a","decision":"rejected","registry":"coordinator"}',
    'action chat/sseActivityEvent {"slot":"slot-a","kind":"approval_resolved","text":"","approval_id":"ap-1","approval_type":"chat"}',
  ],
  "refresh with history": [
    'action dashboard/triggerRefresh undefined',
    'query invalidateQueries ["cron-jobs"]',
    'query invalidateQueries ["crons"]',
    'query invalidateQueries ["cron-history-all"]',
    'query invalidateQueries ["spawn-list"]',
    'query invalidateQueries ["sessions-context"]',
    'query invalidateQueries ["sessions-usage"]',
    'query invalidateQueries ["agents-installed"]',
    'query invalidateQueries ["mcp-tools"]',
    'query invalidateQueries ["kirocrew-agents"]',
    'query invalidateQueries ["default-agent"]',
    'query invalidateQueries ["workspaces"]',
    'query invalidateQueries ["kirocrewConfig"]',
    'query invalidateQueries ["resolved-model"]',
    'query invalidateQueries ["agent-resolved-model"]',
    'query invalidateQueries ["artifacts"]',
    'query invalidateQueries ["artifact-folders"]',
    'action chat/fetchHistory/pending',
  ],
  "slot_clear active and background": [
    'action chat/clearMessages undefined',
    'action chat/clearSlotCache "slot-b"',
  ],
  "slot_agent_switch": [
    'action dashboard/fetchSlots/pending',
  ],
  "member_projection": [],
  "members_subscribed truncating a torn tail": [
    'query resetQueries ["kirocrew-agents","members-roster"]',
    'query resetQueries ["kirocrew-agents","member-projections"]',
  ],
  "members_subscribed with nothing to drop": [],
  "chat_message user row in the active slot": [
    'action chat/sseChatMessage {"slot":"slot-a","role":"user","content":"hi","ts":"2026-09-01T00:00:00.000Z"}',
    'send {"type":"slot_read","slot":"slot-a","read_ts":"2026-09-01T00:00:00.000Z"}',
    'action chat/setVoicePlaying false',
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"thinking","ts":"<clock>"}',
    'frame',
    'cancel-frame 1',
    'action dashboard/touchSlotActivity {"key":"slot-a","ts":"2026-09-01T00:00:00.000Z","settled":true}',
  ],
  "chat_message assistant row in a background slot": [
    'action chat/sseChatMessage {"slot":"slot-b","role":"assistant","content":"done","ts":"2026-09-01T00:00:00.000Z"}',
    'action dashboard/markSlotUnread {"slot":"slot-b","ts":"2026-09-01T00:00:00.000Z","localTs":"2026-09-01T00:00:00.000Z"}',
    'event mc-theme-sound {"trigger":"message-received"}',
    'frame',
    'cancel-frame 1',
    'action dashboard/touchSlotActivity {"key":"slot-b","ts":"2026-09-01T00:00:00.000Z","settled":false}',
  ],
  "chat_message permission row in a background slot": [
    'action chat/sseChatMessage {"slot":"slot-b","role":"permission","content":"[agent] shell","ts":"2026-09-01T00:00:00.000Z"}',
    'event mc-notification {"kind":"approval"}',
    'action dashboard/markSlotUnread {"slot":"slot-b","localTs":"2026-09-01T00:00:00.000Z"}',
  ],
  "chat_message permission row": [
    'action chat/sseChatMessage {"slot":"slot-a","role":"permission","content":"[agent] shell","ts":"2026-09-01T00:00:00.000Z","meta":{"approval_id":"p-1"}}',
    'event mc-notification {"kind":"approval"}',
    'send {"type":"slot_read","slot":"slot-a","read_ts":"2026-09-01T00:00:00.000Z"}',
  ],
  "chat_message resolved permission row": [
    'action chat/sseChatMessage {"slot":"slot-a","role":"permission","content":"[agent] shell","ts":"2026-09-01T00:00:00.000Z","meta":{"approval_id":"p-2","resolved":true}}',
    'send {"type":"slot_read","slot":"slot-a","read_ts":"2026-09-01T00:00:00.000Z"}',
  ],
  "chat_message subagent and tool rows": [
    'action chat/sseChatMessage {"slot":"slot-a","role":"subagent","content":"child","ts":"2026-09-01T00:00:00.000Z"}',
    'send {"type":"slot_read","slot":"slot-a","read_ts":"2026-09-01T00:00:00.000Z"}',
    'action chat/setVoicePlaying false',
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"thinking","ts":"<clock>"}',
    'action chat/sseChatMessage {"slot":"slot-a","role":"tool_result","content":"out","ts":"2026-09-01T00:00:00.000Z"}',
    'frame',
    'cancel-frame 1',
    'action dashboard/touchSlotActivity {"key":"slot-a","ts":"2026-09-01T00:00:00.000Z","settled":false}',
  ],
  "chat_message passive note": [
    'action chat/sseChatMessage {"slot":"slot-a","role":"inject","cls":"msg msg-note","content":"note","ts":"2026-09-01T00:00:00.000Z"}',
    'send {"type":"slot_read","slot":"slot-a","read_ts":"2026-09-01T00:00:00.000Z"}',
    'action chat/setVoicePlaying false',
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"thinking","ts":"<clock>"}',
    'frame',
    'cancel-frame 1',
    'action dashboard/touchSlotActivity {"key":"slot-a","ts":"2026-09-01T00:00:00.000Z","settled":true}',
  ],
  "chat_message_update by tool call and by row": [
    'action chat/sseChatMessageUpdate {"slot":"slot-a","tool_call_id":"tc-1","content":"x"}',
    'action chat/sseChatMessagePatchByTs {"slot":"slot-a","ts":"2026-09-01T00:00:00.000Z","mid":"m-1","meta":{"k":1}}',
  ],
  "queue family": [
    'action chat/appendQueuedMessage {"slot":"slot-a","content":"later","queue_id":"q-1","ts":"2026-09-01T00:00:00.000Z","queueId":"<id>"}',
    'action chat/editQueuedMessage {"slot":"slot-a","queue_id":"q-1","content":"edited","meta":{"files":[{"name":"a"}]},"attachments":{}}',
    'action chat/reorderQueuedMessages {"slot":"slot-a","order":["q-1"]}',
    'action chat/cancelQueuedMessage {"slot":"slot-a","queue_id":"q-1"}',
    'action chat/removeQueuedMessage {"slot":"slot-a","queue_id":"q-1"}',
    'frame',
    'cancel-frame 1',
    'action dashboard/touchSlotActivity {"key":"slot-a","ts":"2026-09-01T00:00:00.000Z","settled":true}',
  ],
  "steer_push": [
    'action chat/appendSlotMessage {"slot":"slot-a","message":{"role":"user","content":"steer","cls":"msg msg-u","meta":{"steer":true,"sendId":"s-1","steerState":"written","mid":"m-9","files":["f"],"dirs":["d"]},"ts":"2026-09-01T00:00:00.000Z"}}',
    'frame',
    'cancel-frame 2',
    'action dashboard/touchSlotActivity {"key":"slot-a","ts":"2026-09-01T00:00:00.000Z","settled":true}',
  ],
  "steer_push without a slot": [
    'action chat/appendSlotMessage {"slot":"slot-a","message":{"role":"user","content":"steer","cls":"msg msg-u","meta":{"steer":true}}}',
  ],
  "chat_chunk": [
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"streaming","ts":"<clock>"}',
    'frame',
    'cancel-frame 1',
    'action chat/sseChatMessage {"slot":"slot-a","role":"chunk","content":"Hello world","seq":2,"gen":"g-1","batched":true,"parts":[{"seq":1,"text":"Hello"},{"seq":2,"text":" world"}]}',
  ],
  "tool_call and refinement": [
    'event kirocrew-tool-call {"slot":"slot-a","tool":"Terminal","kind":"execute","purpose":"List files","input_preview":"ls -la","is_shell":true,"tool_call_id":"tc-1"}',
    'action chat/sseToolActivity {"slot":"slot-a","tool":"Terminal","kind":"execute","purpose":"List files","input_preview":"ls -la","is_shell":true,"tool_call_id":"tc-1","auto":false,"is_update":false}',
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"tool","purpose":"List files","toolName":"Terminal","derivedTitle":"","toolCallId":"tc-1","ts":"<clock>"}',
    'event kirocrew-tool-call {"slot":"slot-a","tool":"ls -la","kind":"execute","input_preview":"ls -la","is_shell":true,"tool_call_id":"tc-1","is_update":true}',
    'action chat/sseToolActivity {"slot":"slot-a","tool":"ls -la","kind":"execute","input_preview":"ls -la","is_shell":true,"tool_call_id":"tc-1","is_update":true,"auto":false}',
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"tool","purpose":"List files","toolName":"ls -la","derivedTitle":"","derivedAction":{"type":"list_files"},"derivedMore":0,"toolCallId":"tc-1","ts":"<clock>"}',
  ],
  "tool_result and mcp_app_render": [
    'action chat/sseToolResult {"slot":"slot-a","output":"ok","tool_call_id":"tc-1"}',
    'action chat/sseMcpAppRender {"slot":"slot-a","tool_call_id":"tc-1","resource_uri":"ui://x"}',
  ],
  "question_card new and repeated": [
    'query invalidateQueries ["command-center","questions"]',
    'action chat/setQuestionCard {"slot":"slot-a","ask_id":"ask-1","questions":[{"question":"Which?"}]}',
    'event mc-notification {"kind":"approval"}',
    'query invalidateQueries ["command-center","questions"]',
    'action chat/setQuestionCard {"slot":"slot-a","ask_id":"ask-1","questions":[{"question":"Which?"}]}',
  ],
  "question_card in a background slot": [
    'query invalidateQueries ["command-center","questions"]',
    'action chat/setQuestionCard {"slot":"slot-b","card_id":"card-1","questions":[{"question":"Which?"}]}',
    'event mc-notification {"kind":"approval"}',
  ],
  "question_card_resolved": [
    'query invalidateQueries ["command-center","questions"]',
    'action chat/resolveQuestionCard {"ask_id":"ask-1"}',
    'query invalidateQueries ["command-center","questions"]',
    'action chat/resolveQuestionCard {"card_id":"never-held"}',
  ],
  "followup_card": [
    'action chat/setFollowupCard {"slot":"slot-a","items":[{"title":"Next","description":"","prompt":"do it","branch":"b"}],"ts":5}',
  ],
  "slot_read": [
    'action dashboard/remoteSlotRead {"slot":"slot-a","readTs":"2026-09-01T00:00:00.000Z"}',
    'action dashboard/remoteSlotRead {"slot":"slot-a"}',
  ],
  "slot_folder_suggestion": [
    'action chat/setFolderSuggestion {"slot":"slot-a","folderId":"f1","folderName":"Work","breadcrumb":"A / Work","ts":7}',
  ],
  "activity_event": [
    'query invalidateQueries ["available-models"]',
    'action chat/sseActivityEvent {"slot":"slot-a","kind":"session","text":"spawned","spawned":true}',
    'action chat/sseActivityEvent {"slot":"slot-a","kind":"session","text":"warm"}',
  ],
  "subagent lifecycle": [
    'action chat/sseSubagentSpawn {"slot":"slot-a","id":"sa-1","task":"t","agent":"a"}',
    'action chat/sseSubagentQueued {"slot":"slot-a","queued":2,"reason":"memory"}',
    'action chat/sseSubagentTool {"slot":"slot-a","id":"sa-1","tool":"read","turns":1}',
    'action chat/sseSubagentStalled {"slot":"slot-a","id":"sa-1","stalled":true}',
    'cancel-frame 1',
    'action chat/sseSubagentBatchChunks {"chunks":[{"id":"sa-1","slot":"slot-a","text":"partialmore"}]}',
    'action chat/sseSubagentRetrying {"slot":"slot-a","id":"sa-1","attempt":2}',
    'cancel-frame 2',
    'action chat/sseSubagentBatchChunks {"chunks":[{"id":"sa-1","slot":"slot-a","text":"again"}]}',
    'action chat/sseSubagentRetrying {"slot":"slot-a","id":"sa-1"}',
    'cancel-frame 3',
    'action chat/sseSubagentBatchChunks {"chunks":[{"id":"sa-1","slot":"slot-a","text":"last"}]}',
    'action chat/sseSubagentDone {"slot":"slot-a","id":"sa-1","elapsed":3,"credits":1.5}',
  ],
  "subagent snapshot and batches": [
    'action chat/sseSubagentSnapshot {"slot":"slot-a","id":"sa-2","task":"t","agent":"a","streaming":"all","last_tool":"","started":1}',
    'action chat/sseSubagentBatchChunks {"chunks":[{"id":"sa-3","slot":"slot-a","text":"x"}]}',
    'action chat/sseSubagentBatchUpdate {"updates":[{"slot":"slot-a","id":"sa-3","attempt":2},{"slot":"slot-a","id":"sa-4","tool":"grep"}]}',
    'cancel-frame 2',
    'action chat/sseSubagentBatchChunks {"chunks":[{"id":"sa-5","slot":"slot-a","text":"y"}]}',
    'action chat/sseSubagentBatchChunks {"chunks":[{"slot":"slot-a","id":"sa-5","text":"z"}]}',
    'action chat/sseSubagentSnapshot {"slot":"slot-a","id":"sa-7","task":"t","agent":"a","streaming":"","last_tool":"","started":1}',
    'action chat/sseSubagentBatchChunks {"chunks":[{"id":"sa-6","slot":"slot-a","text":"w"}]}',
    'action chat/sseSubagentDone {"slot":"slot-a","id":"sa-6","elapsed":1}',
    'frame',
    'cancel-frame 3',
  ],
  "subagent status and text": [
    'action dashboard/sseSubagentStatus {"slot":"slot-a","running":1}',
    'action dashboard/sseSubagentText {"slot":"slot-a","id":"sa-1","text":"t"}',
  ],
  "app_reload": [
    'event mc:app-reload {"app":"notes"}',
  ],
  "wave markers and heartbeat": [],
  "workflow_run_event progress and a terminal status": [
    'action chat/sseWorkflowEvent {"run_id":"r-2","seq":1,"type":"step_started","data":{}}',
    'action chat/sseWorkflowEvent {"run_id":"r-2","seq":2,"type":"run_failed","data":{}}',
    'query invalidateQueries ["command-center","workflows"]',
  ],
  "workflow_run_event and side_result": [
    'action chat/sseWorkflowEvent {"run_id":"r-1","seq":1,"type":"run_started","data":{"session_key":"slot-a"}}',
    'action chat/sseSideResult {"slot":"slot-a","run_id":"side-1","role":"assistant","content":"aside","final":true}',
  ],
  "chat.thread_reply": [
    'query invalidateQueries ["chat-thread","slot-a","m-1"]',
    'query invalidateQueries ["chat-threads","slot-a"]',
    'query invalidateQueries ["chat-thread","slot-a","m-1"]',
    'query invalidateQueries ["chat-threads","slot-a"]',
  ],
  "chat.side_queue": [
    'action chat/sseSideQueue {"slot":"slot-a","action":"push","queue_id":"sq-1","content":"aside"}',
    'action chat/sseSideQueue {"slot":"slot-a","action":"cancel","queue_id":"sq-1","origin_client":"another-tab","suppressRelease":true}',
  ],
  "context_usage": [
    'action chat/sseContextUsage {"slot":"slot-a","pct":40}',
  ],
  "chat_thinking": [
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"thinking","ts":"<clock>"}',
    'frame',
    'cancel-frame 1',
    'action chat/sseThinkingChunk {"slot":"slot-a","content":"mullmore"}',
  ],
  "chat_segment": [
    'action chat/sseChatMessage {"slot":"slot-a","role":"_segment"}',
  ],
  "chat_status and variant switch": [
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"thinking","label":"Compacting","ts":"<clock>"}',
    'action chat/refreshSlot/pending',
  ],
  "chat_done in the active slot": [
    'action chat/sseChatMessage {"slot":"slot-a","ts":"2026-09-01T00:00:00.000Z","role":"_done"}',
    'event mc-notification {"kind":"turn"}',
    'send {"type":"slot_read","slot":"slot-a","read_ts":"2026-09-01T00:00:00.000Z"}',
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"idle","ts":"<clock>"}',
    'action chat/refreshSlot/pending',
    'query refetchQueries ["pull-request-source"] {"type":"active"}',
    'query invalidateQueries ["pull-request-statuses"] {"refetchType":"active"}',
  ],
  "chat_done in a background slot": [
    'action chat/sseChatMessage {"slot":"slot-b","ts":"2026-09-01T00:00:00.000Z","role":"_done"}',
    'event mc-notification {"kind":"turn"}',
    'action dashboard/markSlotUnread {"slot":"slot-b","ts":"2026-09-01T00:00:00.000Z"}',
    'action chat/warmSlotCache/pending',
    'action chat/setSlotStatusDetail {"slot":"slot-b","kind":"idle","ts":"<clock>"}',
    'action chat/refreshSlot/pending',
    'query invalidateQueries ["pull-request-statuses"] {"refetchType":"none"}',
  ],
  "chat_done needing input": [
    'action chat/sseChatMessage {"slot":"slot-b","ts":"2026-09-01T00:00:00.000Z","needs_input":true,"continuing":false,"role":"_done"}',
    'event mc-notification {"kind":"turn"}',
    'action dashboard/markSlotUnread {"slot":"slot-b","ts":"2026-09-01T00:00:00.000Z"}',
    'action chat/warmSlotCache/pending',
    'action chat/setSlotStatusDetail {"slot":"slot-b","kind":"idle","ts":"<clock>"}',
    'action chat/refreshSlot/pending',
    'query invalidateQueries ["pull-request-statuses"] {"refetchType":"none"}',
  ],
  "chat_done still continuing": [
    'action chat/sseChatMessage {"slot":"slot-b","ts":"2026-09-01T00:00:00.000Z","continuing":true,"role":"_done"}',
    'action dashboard/markSlotUnread {"slot":"slot-b","ts":"2026-09-01T00:00:00.000Z"}',
    'action chat/warmSlotCache/pending',
    'action chat/setSlotStatusDetail {"slot":"slot-b","kind":"idle","ts":"<clock>"}',
    'action chat/refreshSlot/pending',
    'query invalidateQueries ["pull-request-statuses"] {"refetchType":"none"}',
  ],
  "autonudge_state update and removal": [
    'query invalidateQueries ["autonudge-loops"]',
    'query setQueryData ["session-automation","slot-a"]',
    'query invalidateQueries ["session-automation","slot-a"]',
    'action chat/removeAutomation "slot-a"',
    'query invalidateQueries ["autonudge-loops"]',
    'query invalidateQueries ["autonudge-loops"]',
  ],
  "voice frames without a request": [],
  "sessions_restarting and refine": [
    'action dashboard/triggerRefresh undefined',
    'query invalidateQueries ["cron-jobs"]',
    'query invalidateQueries ["crons"]',
    'query invalidateQueries ["cron-history-all"]',
    'query invalidateQueries ["spawn-list"]',
    'query invalidateQueries ["sessions-context"]',
    'query invalidateQueries ["sessions-usage"]',
    'query invalidateQueries ["agents-installed"]',
    'query invalidateQueries ["mcp-tools"]',
    'query invalidateQueries ["kirocrew-agents"]',
    'query invalidateQueries ["default-agent"]',
    'query invalidateQueries ["workspaces"]',
    'query invalidateQueries ["kirocrewConfig"]',
    'query invalidateQueries ["resolved-model"]',
    'query invalidateQueries ["agent-resolved-model"]',
    'query invalidateQueries ["artifacts"]',
    'query invalidateQueries ["artifact-folders"]',
    'action dashboard/triggerRefresh undefined',
    'query invalidateQueries ["cron-jobs"]',
    'query invalidateQueries ["crons"]',
    'query invalidateQueries ["cron-history-all"]',
    'query invalidateQueries ["spawn-list"]',
    'query invalidateQueries ["sessions-context"]',
    'query invalidateQueries ["sessions-usage"]',
    'query invalidateQueries ["agents-installed"]',
    'query invalidateQueries ["mcp-tools"]',
    'query invalidateQueries ["kirocrew-agents"]',
    'query invalidateQueries ["default-agent"]',
    'query invalidateQueries ["workspaces"]',
    'query invalidateQueries ["kirocrewConfig"]',
    'query invalidateQueries ["resolved-model"]',
    'query invalidateQueries ["agent-resolved-model"]',
    'query invalidateQueries ["artifacts"]',
    'query invalidateQueries ["artifact-folders"]',
  ],
  "update_progress": [
    'action dashboard/setUpdateProgress {"step":"downloading","detail":"1/2"}',
    'action dashboard/setUpdateProgress {"step":"restarting","detail":""}',
    'action dashboard/setUpdateProgress {"step":"failed","detail":"boom"}',
    'action dashboard/setUpdateProgress null',
  ],
  "channel frames": [
    'event kirocrew-channel {"type":"channel_message","data":{"id":1}}',
    'event kirocrew-channel {"type":"channel_agent_status","data":{"id":2}}',
    'event kirocrew-channel {"type":"channel_created","data":{"id":3}}',
    'event kirocrew-channel {"type":"channel_closed","data":{"id":4}}',
    'event kirocrew-channel {"type":"channel_agent_joined","data":{"id":5}}',
    'event kirocrew-channel {"type":"channel_agent_left","data":{"id":6}}',
  ],
  "cron_history": [
    'event cron_history {"job":"nightly"}',
    'query invalidateQueries ["cron-history"]',
    'query invalidateQueries ["cron-history-all"]',
  ],
  "source_status": [
    'query cancelQueries ["pull-request-statuses"]',
    'query setQueriesData ["pull-request-statuses"]',
    'action dashboard/patchSlotSourceLinks {"url":"https://example.test/pr/1","state":"merged","ci":"passed"}',
    'query invalidateQueries ["pull-request-source","https://example.test/pr/1"]',
    'query invalidateQueries ["pull-request-checks","https://example.test/pr/1"]',
  ],
  "computer_use_frame": [
    'event kirocrew-computer-use-frame {"jpeg":"AAAA","w":10,"h":10}',
  ],
  "tool_call with a null payload": [
    'event kirocrew-tool-call',
  ],
  "approval without an id": [
    'query invalidateQueries ["global-approvals"]',
    'event mc-notification {"kind":"approval"}',
    'action notifications/addNotification {"kind":"approval","title":"Tool approval: shell","body":"**Source:** agent","ts":"1790000008","slot":"slot-a"}',
    'event mc-live-notification {"note":{"kind":"approval","title":"Tool approval: shell","body":"**Source:** agent","ts":"1790000008","slot":"slot-a"}}',
    'action chat/sseChatMessage {"slot":"slot-a","role":"permission","content":"[agent] shell","ts":"1790000008","meta":{"tool_input":"","source":"agent","registry":"coordinator"}}',
    'action chat/sseActivityEvent {"slot":"slot-a","kind":"approval","text":"shell","approval_type":"chat"}',
  ],
  "unknown and prototype-named types": [],
}

const EXPECTED_LIFECYCLE: Record<string, string[]> = {
  "first connect": [
    'connect ws:/api/ws?caps=slot_patch',
    'query invalidateQueries ["crew-log-projections"] {"cancelRefetch":false}',
    'action dashboard/sseConnected undefined',
    'query fetchQuery ["automation-seed","legacy"] {"staleTime":0,"retry":false}',
    'query fetchQuery ["automation-seed","structured"] {"staleTime":0,"retry":false}',
    'action notifications/fetch/pending',
    'query fetchQuery ["workflow-runs-reconcile"] {"staleTime":0,"gcTime":30000,"retry":false}',
    'action chat/clearSubagentsForSnapshot undefined',
    'send {"type":"subscribe_subagents"}',
    'send {"type":"subscribe_logs"}',
    'send {"type":"slot_focused","slot":"slot-a"}',
    'action chat/reconcileWorkflowRuns []',
    'query setQueryData ["command-center","workflows"]',
    'action chat/setAutomations {"records":[],"legacyComplete":true,"structuredComplete":true,"protectedSlots":[]}',
    'action notifications/fetch/fulfilled',
  ],
  "reconnect catch-up": [
    'action dashboard/sseDisconnected undefined',
    'connect ws:/api/ws?caps=slot_patch',
    'query invalidateQueries ["crew-log-projections"] {"cancelRefetch":false}',
    'cancel-frame 1',
    'action chat/sseThinkingChunk {"slot":"slot-a","content":"kept"}',
    'cancel-frame 2',
    'cancel-frame 3',
    'action dashboard/sseConnected undefined',
    'action dashboard/fetchSlots/pending',
    'query invalidateQueries ["session-summary"]',
    'query resetQueries ["dashboard-card"]',
    'query invalidateQueries ["command-center"]',
    'query invalidateQueries ["global-approvals"]',
    'query invalidateQueries ["artifacts"]',
    'query invalidateQueries ["artifact-folders"]',
    'query fetchQuery ["credential-redaction"] {"staleTime":0}',
    'query invalidateQueries ["chat-thread"]',
    'query invalidateQueries ["chat-threads"]',
    'query removeQueries ["member-thread"]',
    'query fetchQuery ["automation-seed","legacy"] {"staleTime":0,"retry":false}',
    'query fetchQuery ["automation-seed","structured"] {"staleTime":0,"retry":false}',
    'action notifications/fetch/pending',
    'query fetchQuery ["workflow-runs-reconcile"] {"staleTime":0,"gcTime":30000,"retry":false}',
    'action chat/refreshSlot/pending',
    'action chat/clearSubagentsForSnapshot undefined',
    'send {"type":"subscribe_subagents"}',
    'send {"type":"slot_focused","slot":"slot-a"}',
    'action dashboard/fetchSlots/fulfilled',
    'action chat/reconcileWorkflowRuns []',
    'query setQueryData ["command-center","workflows"]',
    'action chat/setAutomations {"records":[],"legacyComplete":true,"structuredComplete":true,"protectedSlots":[]}',
    'action notifications/fetch/fulfilled',
    'action chat/refreshSlot/fulfilled',
  ],
  "reconnect after an update restart reloads": [
    'action dashboard/sseDisconnected undefined',
    'connect ws:/api/ws?caps=slot_patch',
    'query invalidateQueries ["crew-log-projections"] {"cancelRefetch":false}',
    'reload',
  ],
  "unmount": [
    'cancel-frame 1',
    'cancel-frame 2',
    'cancel-frame 3',
    'action dashboard/touchSlotActivity {"key":"slot-a","ts":"2026-09-01T00:00:00.000Z","settled":true}',
    'cancel-frame 2',
    'action chat/sseSubagentBatchChunks {"chunks":[{"id":"sa-1","slot":"slot-a","text":"flushed"}]}',
    'action chat/sseThinkingChunk {"slot":"slot-a","content":"salvaged"}',
    'close',
    'action chat/setVoicePlaying false',
  ],
  "forceReconnect": [
    'close',
    'connect ws:/api/ws?caps=slot_patch',
    'query invalidateQueries ["crew-log-projections"] {"cancelRefetch":false}',
    'action dashboard/sseConnected undefined',
    'action dashboard/fetchSlots/pending',
    'query invalidateQueries ["session-summary"]',
    'query resetQueries ["dashboard-card"]',
    'query invalidateQueries ["command-center"]',
    'query invalidateQueries ["global-approvals"]',
    'query invalidateQueries ["artifacts"]',
    'query invalidateQueries ["artifact-folders"]',
    'query fetchQuery ["credential-redaction"] {"staleTime":0}',
    'query invalidateQueries ["chat-thread"]',
    'query invalidateQueries ["chat-threads"]',
    'query removeQueries ["member-thread"]',
    'query fetchQuery ["automation-seed","legacy"] {"staleTime":0,"retry":false}',
    'query fetchQuery ["automation-seed","structured"] {"staleTime":0,"retry":false}',
    'action notifications/fetch/pending',
    'query fetchQuery ["workflow-runs-reconcile"] {"staleTime":0,"gcTime":30000,"retry":false}',
    'action chat/refreshSlot/pending',
    'action chat/clearSubagentsForSnapshot undefined',
    'send {"type":"subscribe_subagents"}',
    'send {"type":"slot_focused","slot":"slot-a"}',
    'action dashboard/fetchSlots/fulfilled',
    'action chat/reconcileWorkflowRuns []',
    'query setQueryData ["command-center","workflows"]',
    'action chat/setAutomations {"records":[],"legacyComplete":true,"structuredComplete":true,"protectedSlots":[]}',
    'action notifications/fetch/fulfilled',
    'action chat/refreshSlot/fulfilled',
  ],
  "StrictMode mounts twice and connects once": [
    'connect ws:/api/ws?caps=slot_patch',
    'close',
    'action chat/setVoicePlaying false',
    'connect ws:/api/ws?caps=slot_patch',
    'query invalidateQueries ["crew-log-projections"] {"cancelRefetch":false}',
    'action dashboard/sseConnected undefined',
    'query fetchQuery ["automation-seed","legacy"] {"staleTime":0,"retry":false}',
    'query fetchQuery ["automation-seed","structured"] {"staleTime":0,"retry":false}',
    'action notifications/fetch/pending',
    'query fetchQuery ["workflow-runs-reconcile"] {"staleTime":0,"gcTime":30000,"retry":false}',
    'action chat/clearSubagentsForSnapshot undefined',
    'send {"type":"subscribe_subagents"}',
    'send {"type":"slot_focused","slot":"slot-a"}',
    'action chat/reconcileWorkflowRuns []',
    'query setQueryData ["command-center","workflows"]',
    'action chat/setAutomations {"records":[],"legacyComplete":true,"structuredComplete":true,"protectedSlots":[]}',
    'action notifications/fetch/fulfilled',
  ],
  "a stream hold defers all three pipelines to one timer each": [
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"streaming","ts":"<clock>"}',
    'action chat/appendQueuedMessage {"slot":"slot-a","content":"q","queue_id":"q-7","ts":"2026-09-01T00:00:00.000Z","queueId":"<id>"}',
    'before hold ends',
    'hold ended',
    'action chat/sseChatMessage {"slot":"slot-a","role":"chunk","content":"held","seq":1,"batched":true,"parts":[{"seq":1,"text":"held"}]}',
    'action chat/sseSubagentBatchChunks {"chunks":[{"id":"sa-1","slot":"slot-a","text":"held"}]}',
    'action dashboard/touchSlotActivity {"key":"slot-a","ts":"2026-09-01T00:00:00.000Z","settled":true}',
  ],
  "without requestAnimationFrame every pipeline flushes on a 16ms timer": [
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"streaming","ts":"<clock>"}',
    'action chat/appendQueuedMessage {"slot":"slot-a","content":"q","queue_id":"q-6","ts":"2026-09-01T00:00:00.000Z","queueId":"<id>"}',
    'before 16ms',
    '15ms',
    'action chat/sseChatMessage {"slot":"slot-a","role":"chunk","content":"timed","seq":1,"batched":true,"parts":[{"seq":1,"text":"timed"}]}',
    'action chat/sseSubagentBatchChunks {"chunks":[{"id":"sa-1","slot":"slot-a","text":"timed"}]}',
    'action dashboard/touchSlotActivity {"key":"slot-a","ts":"2026-09-01T00:00:00.000Z","settled":true}',
  ],
  "voice frames for a registered request": [
    'event voice-synthesis-start {"slot":"slot-a","request_id":"r-1"}',
    'action chat/setVoicePlaying true',
    'audio blob:voice',
    'audio play',
    'action chat/setVoiceAudio "QQ=="',
    'event voice-synthesis-start {"slot":"slot-a","request_id":"r-2"}',
    'event voice-synthesis-start {"slot":"slot-a","request_id":"r-3"}',
    'event voice-error {"slot":"slot-a","request_id":"r-3","code":"voice_synthesis_failed","report":"<report>"}',
    'event voice-stop',
    'audio pause',
    'revoke blob:voice',
    'action chat/setVoicePlaying false',
  ],
  "frames inside the reconnect catch-up window": [
    'action notifications/addNotification {"kind":"info","title":"Replayed","ts":"1790000009"}',
    'event mc-notification {"kind":"info"}',
    'query invalidateQueries ["global-approvals"]',
    "action notifications/addNotification {\"kind\":\"approval\",\"title\":\"Tool approval: shell\",\"body\":\"**Source:** agent\\n\\n```approval-command\\nls\\n```\",\"ts\":\"1790000000\",\"approval_id\":\"ap-replay\",\"slot\":\"slot-a\"}",
    'action chat/sseChatMessage {"slot":"slot-a","role":"permission","content":"[agent] shell","ts":"1790000000","meta":{"tool_input":"ls","approval_id":"ap-replay","source":"agent","registry":"coordinator"}}',
    'action chat/sseActivityEvent {"slot":"slot-a","kind":"approval","text":"shell","approval_id":"ap-replay","approval_type":"chat"}',
    'action chat/sseChatMessage {"slot":"slot-b","role":"assistant","content":"old","ts":"2026-09-01T00:00:00.000Z"}',
    'event mc-theme-sound {"trigger":"message-received"}',
    'query invalidateQueries ["command-center","questions"]',
    'action chat/setQuestionCard {"slot":"slot-a","ask_id":"ask-9","questions":[{"question":"Which?"}]}',
    'action chat/sseChatMessage {"slot":"slot-b","ts":"2026-09-01T00:00:00.000Z","role":"_done"}',
    'action chat/setSlotStatusDetail {"slot":"slot-b","kind":"idle","ts":"<clock>"}',
    'action chat/refreshSlot/pending',
    'query invalidateQueries ["pull-request-statuses"] {"refetchType":"none"}',
  ],
  "an overflowing chunk lands its status before the flush; reasoning after it": [
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"streaming","ts":"<clock>"}',
    'action chat/sseChatMessage {"slot":"slot-a","role":"chunk","content":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
    'action chat/sseThinkingChunk {"slot":"slot-b","content":"yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy',
    'action chat/setSlotStatusDetail {"slot":"slot-b","kind":"thinking","ts":"<clock>"}',
  ],
  "turn boundaries speak the tail and keep the delivery floor across a segment": [
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"streaming","ts":"<clock>"}',
    'frame',
    'cancel-frame 1',
    'action chat/sseChatMessage {"slot":"slot-a","role":"chunk","content":"First sentence. Second","seq":1,"batched":true,"parts":[{"seq":1,"text":"First sentence. Second"}]}',
    'action chat/sseChatMessage {"slot":"slot-a","role":"_segment"}',
    'frame',
    'cancel-frame 2',
    'action chat/sseChatMessage {"slot":"slot-a","role":"chunk","content":"After tool. Tail","seq":2,"batched":true,"parts":[{"seq":2,"text":"After tool. Tail"}]}',
    'action chat/sseChatMessage {"slot":"slot-a","ts":"2026-09-01T00:00:00.000Z","role":"_done"}',
    'event mc-notification {"kind":"turn"}',
    'send {"type":"slot_read","slot":"slot-a","read_ts":"2026-09-01T00:00:00.000Z"}',
    'action chat/setSlotStatusDetail {"slot":"slot-a","kind":"idle","ts":"<clock>"}',
    "synthesize slot-a \"First sentence.\\nSecond\\nAfter tool.\\nTail\"",
  ],
  "unmount during backoff still lands the buffered recency bump": [
    'action dashboard/touchSlotActivity {"key":"slot-a","ts":"2026-09-01T00:00:00.000Z","settled":true}',
    'action chat/setVoicePlaying false',
  ],
  "focus sender": [
    'send {"type":"slot_focused","slot":"slot-b"}',
    'send {"type":"slot_focused","slot":"pane-slot"}',
    'send {"type":"slot_focused","slot":null}',
    'send {"type":"slot_focused","slot":"slot-b"}',
    'send {"type":"slot_read","slot":"slot-b","read_ts":"2026-09-01T00:00:00.000Z"}',
    'send {"type":"slot_focused","slot":"slot-b"}',
    'close',
  ],
}
