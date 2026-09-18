/**
 * Behavioural coverage for the multiplexed dashboard socket.
 *
 * `useWebSocket` is the single fan-out point for every server push: it owns the
 * exported question-card reconcile helpers, the ~70-arm frame router, the voice
 * playback queue, and the reconnect / subscription lifecycle. The existing
 * useWebSocket specs pin a handful of specific regressions; this file walks the
 * arms and lifecycle paths those specs leave untouched.
 *
 * Harness note: the hook DISPATCHES through the Provider store but READS
 * (`activeSlot`, `messages`, `slotStatusDetail`) off the singleton store
 * imported from `../store`. Read-dependent paths therefore prime the singleton
 * explicitly and reset it afterwards, matching useWebSocket.approvalRouting.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import {
  useWebSocket,
  askIdsOf,
  resolvedSince,
  staleAskIds,
  reconcileQuestions,
} from '../hooks/useWebSocket'
import { api } from '../api/client'
import { store as globalStore } from '../store'
import chatReducer, { setActiveSlot, clearMessages, sseChatMessage, sseActivityEvent, setQuestionCard, resolveQuestionCard, sseAutomation, resolveByApprovalId } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import { addNotification, removeNotificationByTs } from '../store/notificationsSlice'
import type { ChatSlot } from '../types'
import { recentErrors } from '../utils/errorReport'
import { structuredMonitorLoop } from './monitorFixtures'
import { normalizeAutomationRecord } from '../monitoring/automation'
import { AUTONUDGE_LOOPS_QUERY_KEY } from '../components/autoNudgeLoop'
import { i18nT } from '../i18n/t'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: false, monitors: [] }),
    pendingQuestions: vi.fn().mockResolvedValue([]),
    voiceSynthesize: vi.fn().mockResolvedValue({ ok: true }),
    voiceCancel: vi.fn().mockResolvedValue({ ok: true }),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
  },
}))

const ACTIVE = 'slot-a'
const BACKGROUND = 'slot-b'

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
  send = vi.fn()
  close = vi.fn(() => { this.readyState = MockWebSocket.CLOSED })

  constructor(public url: string) { WS_INSTANCES.push(this) }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }

  /** Raw frame delivery, for the malformed-payload guard. */
  simulateRaw(raw: string) {
    this.onmessage?.(new MessageEvent('message', { data: raw }))
  }
}

/** Minimal audio double: jsdom's HTMLAudioElement cannot actually play. */
class MockAudio {
  static instances: MockAudio[] = []
  static playResult: () => Promise<void> = () => Promise.resolve()
  onended: (() => void) | null = null
  onerror: (() => void) | null = null
  pause = vi.fn()
  play = vi.fn(() => MockAudio.playResult())
  constructor(public src: string) { MockAudio.instances.push(this) }
}

describe('useWebSocket exported reconcile helpers', () => {
  it('collects only ask_ids, skipping legacy cards and an absent map', () => {
    expect(askIdsOf(undefined)).toEqual([])
    expect(askIdsOf({
      a: { ask_id: 'ask-1' },
      b: {},              // legacy card: no server-side record
      c: undefined,
      d: { ask_id: 'ask-2' },
    })).toEqual(['ask-1', 'ask-2'])
  })

  it('reports only resolutions recorded after the watermark', () => {
    const log = new Map([['ask-old', 1], ['ask-mid', 2], ['ask-new', 3]])
    expect(resolvedSince(log, 0)).toEqual(['ask-old', 'ask-mid', 'ask-new'])
    expect(resolvedSince(log, 2)).toEqual(['ask-new'])
    expect(resolvedSince(log, 3)).toEqual([])
  })

  it('reports a locally-held card the server no longer lists as pending', () => {
    const current = { a: { ask_id: 'ask-live' }, b: { ask_id: 'ask-dead' }, c: {} }
    expect(staleAskIds(current, [{ ask_id: 'ask-live' }])).toEqual(['ask-dead'])
    // Legacy card (no ask_id) is never reported stale.
    expect(staleAskIds(current, [])).toEqual(['ask-live', 'ask-dead'])
    expect(staleAskIds(undefined, [])).toEqual([])
  })

  it('drops stale cards and adds the server pending set', () => {
    const before = { x: { ask_id: 'ask-stale' } }
    const { drop, add } = reconcileQuestions(before, before, [
      { ask_id: 'ask-fresh', slot: ACTIVE, questions: [{ question: 'Which?' }] },
    ])
    expect(drop).toEqual(['ask-stale'])
    expect(add.map(q => q.ask_id)).toEqual(['ask-fresh'])
  })

  it('will not re-add a card the server still lists but this client already dropped', () => {
    // `ask-gone` was local before the fetch and vanished during it (a WS
    // resolution): re-adding it would resurrect a card whose submit can only 404.
    const before = { x: { ask_id: 'ask-gone' } }
    const { drop, add } = reconcileQuestions(before, {}, [
      { ask_id: 'ask-gone', slot: ACTIVE, questions: [{ question: 'Dead?' }] },
    ])
    // Still listed as pending, so it is not reported stale — but it is dead.
    expect(drop).toEqual([])
    expect(add).toEqual([])
  })

  it('skips a resolution observed for a card this client never held', () => {
    const { add } = reconcileQuestions({}, {}, [
      { ask_id: 'ask-never-held', slot: ACTIVE, questions: [{ question: 'Q' }] },
    ], ['ask-never-held'])
    expect(add).toEqual([])
  })

  it('refuses a pending entry with no slot or no questions', () => {
    const { add } = reconcileQuestions({}, {}, [
      { ask_id: 'ask-no-slot', questions: [{ question: 'Q' }] },
      { ask_id: 'ask-no-questions', slot: ACTIVE, questions: [] },
    ])
    expect(add).toEqual([])
  })
})

describe('useWebSocket frame router', () => {
  let testStore: ReturnType<typeof createTestStore>
  let testQueryClient: QueryClient
  let rafCbs: FrameRequestCallback[]
  let originalCreateObjectUrl: typeof URL.createObjectURL
  let originalRevokeObjectUrl: typeof URL.revokeObjectURL

  const slotFixture = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
    key, title: key, agent: 'kirocrew', ...extra,
  } as ChatSlot)

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    MockAudio.instances.length = 0
    MockAudio.playResult = () => Promise.resolve()
    rafCbs = []
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE },
    })
    // The production tree dispatches and reads through the same singleton
    // store. This harness intentionally dispatches through a Provider test
    // store, so mirror only production-marked coordinator rows into the
    // singleton read side; unmarked runner rows remain explicit per test.
    const mirroredCoordinatorRows = new Set<string>()
    testStore.subscribe(() => {
      const state = testStore.getState().chat
      const bySlot: Array<[string | null, typeof state.messages]> = [
        [state.activeSlot, state.messages],
        ...Object.entries(state.slotMessages),
      ]
      for (const [slot, messages] of bySlot) {
        if (!slot) continue
        for (const message of messages ?? []) {
          const id = message.meta?.approval_id
          if (message.role !== 'permission'
            || message.meta?.registry !== 'coordinator'
            || typeof id !== 'string') continue
          const key = `${slot}\u0000${id}\u0000${message.ts ?? ''}`
          if (mirroredCoordinatorRows.has(key)) continue
          mirroredCoordinatorRows.add(key)
          globalStore.dispatch(sseChatMessage({
            slot,
            role: message.role,
            content: message.content,
            ts: message.ts,
            cls: message.cls,
            meta: message.meta,
          }))
        }
      }
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafCbs.push(cb); return rafCbs.length })
    originalCreateObjectUrl = URL.createObjectURL
    originalRevokeObjectUrl = URL.revokeObjectURL
    let blobSeq = 0
    URL.createObjectURL = vi.fn(() => `blob:voice-${++blobSeq}`)
    URL.revokeObjectURL = vi.fn()
    // Read paths resolve against the singleton store, not the Provider store.
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    URL.createObjectURL = originalCreateObjectUrl
    URL.revokeObjectURL = originalRevokeObjectUrl
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    testQueryClient = qc
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  function mount() {
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => {
      ws.simulateOpen()
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot: ACTIVE, request_id: 'test-voice' } }))
    })
    return { ...hook, ws }
  }

  const chat = () => testStore.getState().chat
  const dash = () => testStore.getState().dashboard

  it('subscribes to subagent events over the freshly-opened socket', () => {
    const { ws } = mount()
    expect(ws.url).toContain('/api/ws')
    expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: 'subscribe_subagents' }))
    expect(dash().connected).toBe(true)
  })

  it('ignores a frame that is not valid JSON', () => {
    const { ws } = mount()
    act(() => { ws.simulateRaw('{not json') })
    expect(dash().connected).toBe(true)
  })

  it('stores the first server version and reloads only when it changes', () => {
    const { ws } = mount()
    const originalReload = window.location.reload
    const reloadSpy = vi.fn()
    Object.defineProperty(window.location, 'reload', { configurable: true, value: reloadSpy })
    try {
      act(() => { ws.simulateMessage({ type: 'dashboard', data: { version: '1.0.0', running: 1 } }) })
      expect(reloadSpy).not.toHaveBeenCalled()
      expect(dash().status?.version).toBe('1.0.0')

      // Same version again: still no navigation.
      act(() => { ws.simulateMessage({ type: 'dashboard', data: { version: '1.0.0' } }) })
      expect(reloadSpy).not.toHaveBeenCalled()

      act(() => { ws.simulateMessage({ type: 'dashboard', data: { version: '1.1.0' } }) })
      expect(reloadSpy).toHaveBeenCalledTimes(1)
      // The status dispatch is skipped on the reload path.
      expect(dash().status?.version).toBe('1.0.0')
    } finally {
      Object.defineProperty(window.location, 'reload', { configurable: true, value: originalReload })
    }
  })

  it('applies slots-frame side channels without discarding dashboard status', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'dashboard',
        data: { version: '1.0.0', yolo: false, yolo_duration: 'until_shutdown' },
      })
      ws.simulateMessage({
        type: 'slots',
        data: [slotFixture(ACTIVE)],
        yolo: true,
        channelTrusted: true,
      })
    })
    expect(dash().slots.map(s => s.key)).toEqual([ACTIVE])
    expect(dash().approvalMode).toBe('yolo')
    expect(dash().status).toMatchObject({ yolo: true, yolo_duration: 'until_shutdown' })
    expect(dash().channelTrusted).toBe(true)
  })

  it('patches a live TODO list and a renamed title onto the known slot', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'slots', data: [slotFixture(ACTIVE)] }) })

    const todo = { items: [{ text: 'ship it', status: 'pending' }] }
    act(() => {
      ws.simulateMessage({ type: 'todo_update', data: { slot: ACTIVE, todo } })
      ws.simulateMessage({ type: 'slot_title', data: { key: ACTIVE, title: 'Renamed session' } })
    })
    expect(dash().slots[0].todo).toEqual(todo)
    expect(dash().slots[0].title).toBe('Renamed session')

    // A slot-less TODO delta is dropped rather than dispatched.
    act(() => { ws.simulateMessage({ type: 'todo_update', data: { todo: null } }) })
    expect(dash().slots[0].todo).toEqual(todo)
  })

  it('refreshes the pending-skill queues when a candidate is staged', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'skills.pending_changed', data: {} }) })
    // Nothing to assert on the store: the arm exists to invalidate react-query
    // caches, and reaching it without throwing is the contract.
    expect(dash().connected).toBe(true)
  })

  it('acknowledges and un-acknowledges a delivered notification by timestamp', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'notification', data: { kind: 'info', title: 'Build green', ts: '100' } })
    })
    expect(testStore.getState().notifications.items[0].title).toBe('Build green')

    act(() => { ws.simulateMessage({ type: 'notification_ack', data: { ts: '100' } }) })
    expect(testStore.getState().notifications.items[0].acked).toBe(true)

    act(() => { ws.simulateMessage({ type: 'notification_unack', data: { ts: '100' } }) })
    expect(testStore.getState().notifications.items[0].acked).toBe(false)
  })

  it('keeps a silenced or passive notification out of the sound channel', () => {
    const { ws } = mount()
    const heard: string[] = []
    const listener = (e: Event) => { heard.push((e as CustomEvent).detail?.kind) }
    window.addEventListener('mc-notification', listener)
    try {
      act(() => {
        ws.simulateMessage({ type: 'notification', data: { kind: 'info', title: 'Muted', ts: '1', silenced: true } })
        ws.simulateMessage({ type: 'notification', data: { kind: 'info', title: 'Passive', ts: '2', priority: 'passive' } })
      })
      expect(heard).toEqual([])

      act(() => {
        ws.simulateMessage({ type: 'notification', data: { kind: 'info', title: 'Audible', ts: '3' } })
      })
      expect(heard).toEqual(['info'])
      // All three still reached the feed.
      expect(testStore.getState().notifications.items).toHaveLength(3)
    } finally {
      window.removeEventListener('mc-notification', listener)
    }
  })

  it('raises a desktop notification for an approval while the tab is hidden', () => {
    class MockNotification {
      static permission = 'granted'
      static instances: { title: string }[] = []
      constructor(public title: string) { MockNotification.instances.push({ title }) }
    }
    vi.stubGlobal('Notification', MockNotification)
    const hiddenSpy = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    try {
      const { ws } = mount()
      act(() => {
        ws.simulateMessage({
          type: 'approval',
          data: { id: 'ap-1', slot: ACTIVE, source: 'agent', tool: 'execute_bash', tool_input: '{}', ts: 5 },
        })
      })
      expect(MockNotification.instances).toHaveLength(1)
      const card = chat().messages.find(m => m.role === 'permission')
      expect(card?.meta?.approval_id).toBe('ap-1')
      expect(chat().toolLog.some(e => e.approval_id === 'ap-1')).toBe(true)
    } finally {
      hiddenSpy.mockRestore()
    }
  })

  it('turns a spawn approval into a pending subagent card instead of a tool row', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'spawn:agent-7', slot: ACTIVE, source: 'agent', tool: 'spawn_run(write the tests)', ts: 6 },
      })
    })
    const pending = chat().subagents['agent-7']
    expect(pending?.status).toBe('pending')
    expect(pending?.task).toBe('write the tests')
    expect(pending?.approval_id).toBe('spawn:agent-7')
    // A spawn approval must not also land as a generic approval activity row.
    expect(chat().toolLog.some(e => e.approval_id === 'spawn:agent-7' && e.type === 'approval')).toBe(false)
  })

  it('omits the activity row for an approval raised by a subagent', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-sub', slot: ACTIVE, source: 'subagent', tool: 'fs_read', ts: 7 },
      })
    })
    expect(chat().messages.some(m => m.meta?.approval_id === 'ap-sub')).toBe(true)
    expect(chat().toolLog.some(e => e.approval_id === 'ap-sub')).toBe(false)
  })

  it('forwards the active slot while retiring its approval card and feed entry', () => {
    const dispatchSpy = vi.spyOn(testStore, 'dispatch')
    const { ws } = mount()
    // The resolve path looks the raised card up in the SINGLETON store, so the
    // feed row and the activity row have to exist there for it to find them.
    globalStore.dispatch(addNotification({
      kind: 'approval', title: 'Tool approval', body: '', ts: '8', approval_id: 'ap-2',
    } as Parameters<typeof addNotification>[0]))
    globalStore.dispatch(sseActivityEvent({
      slot: ACTIVE, kind: 'approval', text: 'execute_bash', approval_id: 'ap-2', approval_type: 'chat',
    }))
    try {
      act(() => {
        ws.simulateMessage({
          type: 'approval',
          data: { id: 'ap-2', slot: ACTIVE, source: 'agent', tool: 'execute_bash', ts: 8 },
        })
      })
      expect(testStore.getState().notifications.items).toHaveLength(1)

      act(() => {
        ws.simulateMessage({ type: 'approval_resolved', data: { id: 'ap-2', slot: ACTIVE, approved: true } })
      })
      expect(dispatchSpy).toHaveBeenCalledWith(expect.objectContaining({
        type: 'chat/resolveByApprovalId',
        payload: { id: 'ap-2', decision: 'approved', slot: ACTIVE, registry: 'coordinator' },
      }))
      expect(chat().messages.find(m => m.meta?.approval_id === 'ap-2')?.meta?.resolved).toBe('approved')
      expect(testStore.getState().notifications.items).toHaveLength(0)
      expect(chat().toolLog.some(e => e.approval_id === 'ap-2' && e.type === 'approval_resolved')).toBe(true)
    } finally {
      globalStore.dispatch(removeNotificationByTs('8'))
    }
  })

  it('resolves only the named slot when approval ids collide', () => {
    testStore.dispatch(setActiveSlot(BACKGROUND))
    globalStore.dispatch(setActiveSlot(BACKGROUND))
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-shared', slot: ACTIVE, source: 'agent', tool: 'slot_a_tool', ts: 9 },
      })
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-shared', slot: BACKGROUND, source: 'agent', tool: 'slot_b_tool', ts: 10 },
      })
    })

    act(() => {
      ws.simulateMessage({ type: 'approval_resolved', data: { id: 'ap-shared', slot: ACTIVE, approved: false } })
    })

    expect(chat().messages.find(m => m.meta?.approval_id === 'ap-shared')?.meta?.resolved).toBeUndefined()
    expect(chat().slotMessages[ACTIVE]?.find(m => m.meta?.approval_id === 'ap-shared')?.meta?.resolved).toBe('rejected')
  })


  it('leaves coordinator provenance intact for a same-slot runner collision, then reconciles it', async () => {
    const id = 'ap-same-slot'
    const notification = {
      kind: 'approval', title: 'Coordinator approval', body: '', ts: '17', approval_id: id,
    } as Parameters<typeof addNotification>[0]
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      const runner = {
        slot: ACTIVE,
        role: 'permission',
        content: 'runner tool',
        ts: '16',
        meta: { approval_id: id },
      }
      testStore.dispatch(sseChatMessage(runner))
      globalStore.dispatch(sseChatMessage(runner))
      ws.simulateMessage({
        type: 'approval',
        data: { id, slot: ACTIVE, source: 'agent', tool: 'coordinator_tool', ts: 17 },
      })
      globalStore.dispatch(addNotification(notification))
    })

    const rows = () => chat().messages.filter(m => m.meta?.approval_id === id)
    const runnerRow = () => rows().find(m => m.meta?.registry == null)
    const coordinatorRow = () => rows().find(m => m.meta?.registry === 'coordinator')
    try {
      act(() => {
        ws.simulateMessage({
          type: 'approval_resolved',
          data: { id, slot: ACTIVE, approved: false },
        })
      })

      expect(rows()).toHaveLength(2)
      expect(runnerRow()?.meta?.resolved).toBe('rejected')
      expect(coordinatorRow()?.meta?.resolved).toBeUndefined()
      expect(testStore.getState().notifications.items.some(n => n.approval_id === id)).toBe(true)

      // Mirror the Provider dispatch in the singleton read store used by this
      // harness. The resolved runner is no longer an ambiguous pending row.
      globalStore.dispatch(resolveByApprovalId({ id, slot: ACTIVE, decision: 'rejected' }))
      ;(api.notifications as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        notifications: [notification], unread: 1,
      })
      ;(api.approvals as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
      vi.useFakeTimers()
      act(() => { ws.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      vi.useRealTimers()
      await act(async () => {
        WS_INSTANCES[1].simulateOpen()
        await Promise.resolve()
        await Promise.resolve()
      })

      expect(coordinatorRow()?.meta?.resolved).toBe('stale')
      expect(testStore.getState().notifications.items.some(n => n.approval_id === id)).toBe(false)
    } finally {
      vi.useRealTimers()
      globalStore.dispatch(removeNotificationByTs('17'))
    }
  })

  it('does not duplicate a surviving coordinator row after its notification is cleared', async () => {
    const approval = {
      id: 'ap-surviving-row', slot: ACTIVE, source: 'agent', tool: 'surviving_tool', ts: 22,
    }
    ;(api.approvals as ReturnType<typeof vi.fn>).mockResolvedValueOnce([approval])
    const dispatchSpy = vi.spyOn(testStore, 'dispatch')
    const { ws } = mount()
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    const row = chat().messages.find(m => m.meta?.approval_id === approval.id)
    expect(row?.meta?.registry).toBe('coordinator')
    act(() => { ws.simulateMessage({ type: 'notifications_clear', data: {} }) })
    expect(testStore.getState().notifications.items.some(n => n.approval_id === approval.id)).toBe(false)

    ;(api.notifications as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ notifications: [], unread: 0 })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockResolvedValueOnce([approval])
    vi.useFakeTimers()
    act(() => { ws.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(1000) })
    vi.useRealTimers()
    try {
      await act(async () => {
        WS_INSTANCES[1].simulateOpen()
        await Promise.resolve()
        await Promise.resolve()
      })
      expect(chat().messages.filter(m => m.meta?.approval_id === approval.id)).toHaveLength(1)
      expect(dispatchSpy.mock.calls.filter(([action]) =>
        action.type === 'chat/sseChatMessage'
        && action.payload?.meta?.approval_id === approval.id)).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('records a same-slot ambiguous resolution while an authority snapshot is in flight', async () => {
    type ApprovalSnapshot = Awaited<ReturnType<typeof api.approvals>>
    let release: (value: ApprovalSnapshot) => void = () => {}
    const pending = new Promise<ApprovalSnapshot>((resolve) => { release = resolve })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    const dispatchSpy = vi.spyOn(testStore, 'dispatch')
    const { ws } = mount()
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    const id = 'ap-ambiguous-in-flight'
    act(() => {
      const runner = {
        slot: ACTIVE,
        role: 'permission',
        content: 'runner tool',
        ts: '23',
        meta: { approval_id: id },
      }
      testStore.dispatch(sseChatMessage(runner))
      globalStore.dispatch(sseChatMessage(runner))
      ws.simulateMessage({
        type: 'approval',
        data: { id, slot: ACTIVE, source: 'agent', tool: 'coordinator_tool', ts: 24 },
      })
      ws.simulateMessage({
        type: 'approval_resolved',
        data: { id, slot: ACTIVE, approved: false },
      })
      testStore.dispatch(clearMessages())
      globalStore.dispatch(clearMessages())
    })

    await act(async () => {
      release([{ id, slot: ACTIVE, source: 'agent', tool: 'coordinator_tool', ts: 24 }])
      await pending
      await Promise.resolve()
    })

    expect(chat().messages.some(m => m.meta?.approval_id === id)).toBe(false)
    expect(dispatchSpy.mock.calls.filter(([action]) =>
      action.type === 'chat/sseChatMessage'
      && action.payload?.meta?.approval_id === id)).toHaveLength(2)
  })
  it('preserves coordinator provenance when a colliding runner resolution names another slot', async () => {
    const notification = {
      kind: 'approval', title: 'Coordinator approval', body: '', ts: '18', approval_id: 'ap-collision',
    } as Parameters<typeof addNotification>[0]
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-collision', slot: ACTIVE, source: 'agent', tool: 'coordinator_tool', ts: 18 },
      })
      testStore.dispatch(sseChatMessage({
        slot: BACKGROUND,
        role: 'permission',
        content: 'runner tool',
        ts: '19',
        meta: { approval_id: 'ap-collision' },
      }))
      globalStore.dispatch(addNotification(notification))
    })

    try {
      act(() => {
        ws.simulateMessage({
          type: 'approval_resolved',
          data: { id: 'ap-collision', slot: BACKGROUND, approved: false },
        })
      })

      expect(chat().messages.find(m => m.meta?.approval_id === 'ap-collision')?.meta?.resolved).toBeUndefined()
      expect(chat().slotMessages[BACKGROUND]?.find(m => m.meta?.approval_id === 'ap-collision')?.meta?.resolved).toBe('rejected')
      expect(testStore.getState().notifications.items.some(n => n.approval_id === 'ap-collision')).toBe(true)

      ;(api.notifications as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        notifications: [notification], unread: 1,
      })
      ;(api.approvals as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
      vi.useFakeTimers()
      act(() => { ws.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      vi.useRealTimers()
      const reconnected = WS_INSTANCES[1]
      await act(async () => {
        reconnected.simulateOpen()
        await Promise.resolve()
        await Promise.resolve()
      })

      expect(chat().messages.find(m => m.meta?.approval_id === 'ap-collision')?.meta?.resolved).toBe('stale')
      expect(testStore.getState().notifications.items.some(n => n.approval_id === 'ap-collision')).toBe(false)
    } finally {
      vi.useRealTimers()
      globalStore.dispatch(removeNotificationByTs('18'))
    }
  })

  it('retires matching coordinator provenance once after ignoring a colliding slot', async () => {
    const notification = {
      kind: 'approval', title: 'Matching approval', body: '', ts: '20', approval_id: 'ap-matching',
    } as Parameters<typeof addNotification>[0]
    const dispatchSpy = vi.spyOn(testStore, 'dispatch')
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-matching', slot: ACTIVE, source: 'agent', tool: 'matching_tool', ts: 20 },
      })
      testStore.dispatch(sseChatMessage({
        slot: BACKGROUND,
        role: 'permission',
        content: 'runner tool',
        ts: '21',
        meta: { approval_id: 'ap-matching' },
      }))
      globalStore.dispatch(addNotification(notification))
    })

    try {
      act(() => {
        ws.simulateMessage({
          type: 'approval_resolved',
          data: { id: 'ap-matching', slot: BACKGROUND, approved: false },
        })
      })
      expect(testStore.getState().notifications.items.some(n => n.approval_id === 'ap-matching')).toBe(true)

      act(() => {
        ws.simulateMessage({
          type: 'approval_resolved',
          data: { id: 'ap-matching', slot: ACTIVE, approved: true },
        })
      })
      expect(chat().messages.find(m => m.meta?.approval_id === 'ap-matching')?.meta?.resolved).toBe('approved')
      expect(testStore.getState().notifications.items.some(n => n.approval_id === 'ap-matching')).toBe(false)

      const retirementCount = () => dispatchSpy.mock.calls.filter(([action]) =>
        action.type === 'chat/resolveByApprovalId' && action.payload?.id === 'ap-matching').length
      expect(retirementCount()).toBe(2)

      ;(api.notifications as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ notifications: [], unread: 0 })
      ;(api.approvals as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
      vi.useFakeTimers()
      act(() => { ws.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      vi.useRealTimers()
      await act(async () => {
        WS_INSTANCES[1].simulateOpen()
        await Promise.resolve()
        await Promise.resolve()
      })

      expect(retirementCount()).toBe(2)
    } finally {
      vi.useRealTimers()
      globalStore.dispatch(removeNotificationByTs('20'))
    }
  })

  it('uses coordinator provenance to retire a mapped row from a slotless resolution', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-state', slot: ACTIVE, source: 'agent', tool: 'active_tool', ts: 11 },
      })
    })
    globalStore.dispatch(addNotification({
      kind: 'approval', title: 'State approval', body: '', ts: '11', approval_id: 'ap-state',
    } as Parameters<typeof addNotification>[0]))
    const invalidateSpy = vi.spyOn(testQueryClient, 'invalidateQueries')
    try {
      act(() => {
        ws.simulateMessage({ type: 'approval_resolved', data: { id: 'ap-state', approved: false } })
      })

      expect(chat().messages.find(m => m.meta?.approval_id === 'ap-state')?.meta?.resolved).toBe('rejected')
      expect(testStore.getState().notifications.items.find(n => n.approval_id === 'ap-state')).toBeUndefined()
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['global-approvals'] })
    } finally {
      globalStore.dispatch(removeNotificationByTs('11'))
    }
  })

  it('retires a coordinator row omitted by the reconnect snapshot and clears its provenance', async () => {
    ;(api.approvals as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })

    const notification = {
      kind: 'approval', title: 'Gap approval', body: '', ts: '12', approval_id: 'ap-gap',
    } as Parameters<typeof addNotification>[0]
    try {
      act(() => {
        ws.simulateMessage({
          type: 'approval',
          data: { id: 'ap-gap', slot: ACTIVE, source: 'agent', tool: 'gap_tool', ts: 12 },
        })
        globalStore.dispatch(addNotification(notification))
      })

      ;(api.notifications as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        notifications: [notification], unread: 1,
      })
      ;(api.approvals as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
      vi.useFakeTimers()
      act(() => { ws.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      vi.useRealTimers()
      const reconnected = WS_INSTANCES[1]
      await act(async () => {
        reconnected.simulateOpen()
        await Promise.resolve()
        await Promise.resolve()
      })

      expect(chat().messages.find(m => m.meta?.approval_id === 'ap-gap')?.meta?.resolved).toBe('stale')
      expect(testStore.getState().notifications.items.some(n => n.approval_id === 'ap-gap')).toBe(false)

      // Retirement removes provenance, so a later chat-runner row with the same
      // per-connection id is not claimed by a slotless coordinator frame.
      act(() => {
        testStore.dispatch(clearMessages())
        testStore.dispatch(sseChatMessage({
          slot: ACTIVE,
          role: 'permission',
          content: 'runner tool',
          ts: '13',
          meta: { approval_id: 'ap-gap' },
        }))
        reconnected.simulateMessage({
          type: 'approval_resolved', data: { id: 'ap-gap', approved: true },
        })
      })
      expect(chat().messages.find(m => m.meta?.approval_id === 'ap-gap')?.meta?.resolved).toBeUndefined()
    } finally {
      vi.useRealTimers()
      globalStore.dispatch(removeNotificationByTs('12'))
    }
  })

  it.each([
    { id: 'appr-ghost', decision: undefined },
    { id: 'spawn:ghost', decision: 'expired' },
  ])('does not resurrect unseen $id resolved during the pending snapshot', async ({ id, decision }) => {
    type ApprovalSnapshot = Awaited<ReturnType<typeof api.approvals>>
    let release: (value: ApprovalSnapshot) => void = () => {}
    const pending = new Promise<ApprovalSnapshot>((resolve) => { release = resolve })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)

    const { ws } = mount()
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(api.approvals).toHaveBeenCalledTimes(1)
    act(() => {
      ws.simulateMessage({
        type: 'approval_resolved',
        data: { id, slot: ACTIVE, approved: false, ...(decision ? { decision } : {}) },
      })
    })

    await act(async () => {
      release([{ id, slot: ACTIVE, source: 'agent', tool: 'ghost_tool', ts: 13 }])
      await pending
      await Promise.resolve()
    })

    expect(testStore.getState().notifications.items.some(n => n.approval_id === id)).toBe(false)
    expect(chat().messages.some(m => m.role === 'permission' && m.meta?.approval_id === id)).toBe(false)
  })

  it('does not resurrect an approval resolved during the pending snapshot and still adds live approvals', async () => {
    type ApprovalSnapshot = Awaited<ReturnType<typeof api.approvals>>
    let release: (value: ApprovalSnapshot) => void = () => {}
    const pending = new Promise<ApprovalSnapshot>((resolve) => { release = resolve })
    const staleNotification = {
      kind: 'approval', title: 'Stale approval', body: '', ts: '14', approval_id: 'ap-resolved-during-sync',
    } as Parameters<typeof addNotification>[0]
    ;(api.notifications as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      notifications: [staleNotification], unread: 1,
    })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    const dispatchSpy = vi.spyOn(testStore, 'dispatch')
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-resolved-during-sync', slot: ACTIVE, source: 'agent', tool: 'stale_tool', ts: 14 },
      })
      globalStore.dispatch(addNotification(staleNotification))
    })

    try {
      await act(async () => {
        await Promise.resolve()
        await Promise.resolve()
      })
      expect(api.approvals).toHaveBeenCalledTimes(1)

      act(() => {
        ws.simulateMessage({
          type: 'approval_resolved',
          data: { id: 'ap-resolved-during-sync', slot: ACTIVE, approved: true },
        })
        globalStore.dispatch(removeNotificationByTs('14'))
      })
      expect(testStore.getState().notifications.items.some(n => n.approval_id === 'ap-resolved-during-sync')).toBe(false)

      await act(async () => {
        release([
          { id: 'ap-resolved-during-sync', slot: ACTIVE, source: 'agent', tool: 'stale_tool', ts: 14 },
          { id: 'ap-still-pending', slot: ACTIVE, source: 'agent', tool: 'live_tool', ts: 15 },
        ])
        await pending
        await Promise.resolve()
      })

      const approvalRowDispatches = (id: string) => dispatchSpy.mock.calls.filter(([action]) =>
        action.type === 'chat/sseChatMessage' && action.payload?.meta?.approval_id === id).length
      expect(testStore.getState().notifications.items.find(n => n.approval_id === 'ap-resolved-during-sync')).toBeUndefined()
      expect(chat().messages.filter(m => m.meta?.approval_id === 'ap-resolved-during-sync')).toHaveLength(1)
      expect(chat().messages.find(m => m.meta?.approval_id === 'ap-resolved-during-sync')?.meta?.resolved).toBe('approved')
      expect(approvalRowDispatches('ap-resolved-during-sync')).toBe(1)
      expect(testStore.getState().notifications.items.some(n => n.approval_id === 'ap-still-pending')).toBe(true)
      expect(chat().messages.find(m => m.meta?.approval_id === 'ap-still-pending')?.meta?.resolved).toBeUndefined()
      expect(approvalRowDispatches('ap-still-pending')).toBe(1)
    } finally {
      globalStore.dispatch(removeNotificationByTs('14'))
    }
  })

  it('does not resurrect an approval injected and resolved during the pending snapshot', async () => {
    type ApprovalSnapshot = Awaited<ReturnType<typeof api.approvals>>
    let release: (value: ApprovalSnapshot) => void = () => {}
    const pending = new Promise<ApprovalSnapshot>((resolve) => { release = resolve })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    const dispatchSpy = vi.spyOn(testStore, 'dispatch')
    const { ws } = mount()
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(api.approvals).toHaveBeenCalledTimes(1)

    const notification = {
      kind: 'approval', title: 'Injected approval', body: '', ts: '16', approval_id: 'ap-injected-during-sync',
    } as Parameters<typeof addNotification>[0]
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-injected-during-sync', slot: ACTIVE, source: 'agent', tool: 'injected_tool', ts: 16 },
      })
      globalStore.dispatch(addNotification(notification))
      ws.simulateMessage({
        type: 'approval_resolved',
        data: { id: 'ap-injected-during-sync', slot: ACTIVE, approved: false },
      })
      globalStore.dispatch(removeNotificationByTs('16'))
    })

    try {
      await act(async () => {
        release([{ id: 'ap-injected-during-sync', slot: ACTIVE, source: 'agent', tool: 'injected_tool', ts: 16 }])
        await pending
        await Promise.resolve()
      })

      const approvalRowDispatches = dispatchSpy.mock.calls.filter(([action]) =>
        action.type === 'chat/sseChatMessage'
        && action.payload?.meta?.approval_id === 'ap-injected-during-sync').length
      expect(testStore.getState().notifications.items.find(n => n.approval_id === 'ap-injected-during-sync')).toBeUndefined()
      expect(chat().messages.filter(m => m.meta?.approval_id === 'ap-injected-during-sync')).toHaveLength(1)
      expect(chat().messages.find(m => m.meta?.approval_id === 'ap-injected-during-sync')?.meta?.resolved).toBe('rejected')
      expect(approvalRowDispatches).toBe(1)
    } finally {
      globalStore.dispatch(removeNotificationByTs('16'))
    }
  })

  it('retires only coordinator ids held before an in-flight snapshot', async () => {
    let release: (value: unknown) => void = () => {}
    const pending = new Promise((resolve) => { release = resolve })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-before', slot: ACTIVE, source: 'agent', tool: 'before_tool', ts: 14 },
      })
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(api.approvals).toHaveBeenCalledTimes(1)

    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-during', slot: ACTIVE, source: 'agent', tool: 'during_tool', ts: 15 },
      })
    })
    await act(async () => {
      release([])
      await pending
      await Promise.resolve()
    })

    expect(chat().messages.find(m => m.meta?.approval_id === 'ap-before')?.meta?.resolved).toBe('stale')
    expect(chat().messages.find(m => m.meta?.approval_id === 'ap-during')?.meta?.resolved).toBeUndefined()
  })

  it('keeps a decision delivered during an in-flight snapshot that omits the id', async () => {
    // The retire loop walks the PRE-fetch provenance map. An approval decided
    // by a live frame while the read was in flight is absent from the snapshot
    // (the server already popped it) and still present in that map, so the
    // loop retires it again as 'stale'. The decided card must not be
    // downgraded by that second retirement.
    let release: (value: unknown) => void = () => {}
    const pending = new Promise((resolve) => { release = resolve })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-decided', slot: ACTIVE, source: 'agent', tool: 'decided_tool', ts: 18 },
      })
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(api.approvals).toHaveBeenCalledTimes(1)

    act(() => {
      ws.simulateMessage({
        type: 'approval_resolved',
        data: { id: 'ap-decided', slot: ACTIVE, approved: true },
      })
    })
    expect(chat().messages.find(m => m.meta?.approval_id === 'ap-decided')?.meta?.resolved).toBe('approved')

    await act(async () => {
      release([])
      await pending
      await Promise.resolve()
    })

    expect(chat().messages.find(m => m.meta?.approval_id === 'ap-decided')?.meta?.resolved).toBe('approved')
  })

  it('keeps a decision this tab made itself when the snapshot omits the id', async () => {
    // ChatInput's Allow/Reject and ChatPage's dismiss dispatch
    // resolveByApprovalId directly — no frame, no retireApproval, so the
    // retired-id log never sees them and provenance stays intact. A reconcile
    // snapshot that no longer lists the id then retires the row as 'stale'
    // through the very same loop; the local decision must survive it.
    let release: (value: unknown) => void = () => {}
    const pending = new Promise((resolve) => { release = resolve })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-local', slot: ACTIVE, source: 'agent', tool: 'local_tool', ts: 19 },
      })
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(api.approvals).toHaveBeenCalledTimes(1)

    act(() => {
      testStore.dispatch(resolveByApprovalId({ id: 'ap-local', slot: ACTIVE, decision: 'approved' }))
    })
    expect(chat().messages.find(m => m.meta?.approval_id === 'ap-local')?.meta?.resolved).toBe('approved')

    await act(async () => {
      release([])
      await pending
      await Promise.resolve()
    })

    expect(chat().messages.find(m => m.meta?.approval_id === 'ap-local')?.meta?.resolved).toBe('approved')
  })

  it('reconciles mapped coordinator rows without dropping an untracked runner prompt', async () => {
    let release: (value: unknown) => void = () => {}
    const pending = new Promise((resolve) => { release = resolve })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    const { ws } = mount()
    act(() => {
      testStore.dispatch(sseChatMessage({
        slot: ACTIVE,
        role: 'permission',
        content: 'runner permission',
        ts: '16',
        meta: { approval_id: 'runner-only' },
      }))
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'coordinator-only', slot: ACTIVE, source: 'agent', tool: 'coordinator_tool', ts: 17 },
      })
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
      release([])
      await pending
      await Promise.resolve()
    })

    expect(chat().messages.find(m => m.meta?.approval_id === 'coordinator-only')?.meta?.resolved).toBe('stale')
    expect(chat().messages.find(m => m.meta?.approval_id === 'runner-only')?.meta?.resolved).toBeUndefined()
  })

  it('reads a background slot activity log that was never opened', () => {
    const { ws } = mount()
    // No cached toolLog for the background slot: the lookup must fall back to an
    // empty list rather than throwing, and a chat-type resolution with no raised
    // card writes no activity row.
    act(() => {
      ws.simulateMessage({ type: 'approval_resolved', data: { id: 'ap-bg', slot: BACKGROUND, approved: false } })
    })
    expect(chat().slotActivity[BACKGROUND]?.toolLog ?? []).toEqual([])
  })

  it('promotes an approved spawn to a running card and a rejected one to done', () => {
    const dispatchSpy = vi.spyOn(testStore, 'dispatch')
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'approval_resolved', data: { id: 'spawn:ok', slot: ACTIVE, approved: true } })
      ws.simulateMessage({ type: 'approval_resolved', data: { id: 'spawn:no', slot: ACTIVE, approved: false } })
    })
    const spawned = dispatchSpy.mock.calls.filter(([action]) =>
      action.type === 'chat/sseSubagentSpawn' && action.payload?.id === 'ok')
    expect(chat().subagents['ok']?.status).toBe('running')
    expect(spawned).toHaveLength(1)
    expect(chat().subagents['no']?.status).toBe('error')
    // Same verbatim ErrorNotice slot as the expiry path: a rejection must land
    // as the catalog sentence, never as the raw `rejected` decision token.
    const rejectedCopy = i18nT('hooks.useWebSocket.approval_rejected')
    expect(rejectedCopy).toBe('The approval was rejected, so the request was denied.')
    expect(chat().subagents['no']?.error).toBe(rejectedCopy)
    expect(chat().subagents['no']?.error).not.toBe('rejected')
  })

  it('retires an expired approval with the stale vocabulary, not as a rejection', () => {
    const dispatchSpy = vi.spyOn(testStore, 'dispatch')
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-expired', slot: ACTIVE, source: 'agent', tool: 'execute_bash', ts: 30 },
      })
    })

    act(() => {
      ws.simulateMessage({
        type: 'approval_resolved',
        data: { id: 'ap-expired', slot: ACTIVE, approved: false, decision: 'expired' },
      })
    })

    expect(dispatchSpy).toHaveBeenCalledWith(expect.objectContaining({
      type: 'chat/resolveByApprovalId',
      payload: { id: 'ap-expired', decision: 'stale', slot: ACTIVE, registry: 'coordinator' },
    }))
    expect(chat().messages.find(m => m.meta?.approval_id === 'ap-expired')?.meta?.resolved).toBe('stale')
  })

  it('terminates an explicitly expired spawn while keeping stale chat vocabulary', async () => {
    const dispatchSpy = vi.spyOn(testStore, 'dispatch')
    const doneFor = (agentId: string) => dispatchSpy.mock.calls.filter(([action]) =>
      action.type === 'chat/sseSubagentDone' && action.payload?.id === agentId)
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'spawn:agent-x', slot: ACTIVE, source: 'agent', tool: 'spawn_run(agent-x)', ts: 31 },
      })
    })
    expect(chat().subagents['agent-x']?.status).toBe('pending')

    act(() => {
      ws.simulateMessage({
        type: 'approval_resolved', data: {
          id: 'spawn:agent-x', slot: ACTIVE, approved: false, decision: 'expired',
        },
      })
    })
    expect(dispatchSpy).toHaveBeenCalledWith(expect.objectContaining({
      type: 'chat/resolveByApprovalId',
      payload: { id: 'spawn:agent-x', decision: 'stale', slot: ACTIVE, registry: 'coordinator' },
    }))
    expect(chat().messages.find(m => m.meta?.approval_id === 'spawn:agent-x')?.meta?.resolved).toBe('stale')
    expect(doneFor('agent-x')).toHaveLength(1)
    // The card renders this value verbatim under its error label, so the wire
    // token must arrive as the catalog sentence, not as the machine word.
    const expiredCopy = i18nT('hooks.useWebSocket.approval_wait_expired')
    expect(expiredCopy).toBe('The approval wait expired, so the request was denied.')
    expect(doneFor('agent-x')[0][0].payload.error).toBe(expiredCopy)
    expect(doneFor('agent-x')[0][0].payload.error).not.toBe('expired')
    expect(chat().subagents['agent-x']?.status).toBe('error')
    expect(chat().subagents['agent-x']?.error).toBe(expiredCopy)
  })

  it('emits no subagent lifecycle outcome for a reconnect-stale spawn', async () => {
    const dispatchSpy = vi.spyOn(testStore, 'dispatch')
    const lifecycleFor = (agentId: string) => dispatchSpy.mock.calls.filter(([action]) =>
      ['chat/sseSubagentSpawn', 'chat/sseSubagentDone'].includes(action.type)
        && action.payload?.id === agentId)
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      for (const [id, ts] of [['spawn:gone', 32], ['spawn:no', 33]] as const) {
        ws.simulateMessage({
          type: 'approval',
          data: { id, slot: ACTIVE, source: 'agent', tool: `spawn_run(${id})`, ts },
        })
      }
    })
    expect(chat().subagents['gone']?.status).toBe('pending')
    expect(chat().subagents['no']?.status).toBe('pending')

    // Reconnect reconcile: the server no longer lists spawn:gone, and this
    // client never saw which way it went.
    ;(api.notifications as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ notifications: [], unread: 0 })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockResolvedValueOnce([{ id: 'spawn:no', slot: ACTIVE }])
    vi.useFakeTimers()
    act(() => { ws.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(1000) })
    vi.useRealTimers()
    const reconnected = WS_INSTANCES[1]
    try {
      await act(async () => {
        reconnected.simulateOpen()
        await Promise.resolve()
        await Promise.resolve()
      })
      expect(chat().messages.find(m => m.meta?.approval_id === 'spawn:gone')?.meta?.resolved).toBe('stale')
      expect(lifecycleFor('gone')).toHaveLength(0)
      expect(chat().subagents['gone']?.status).toBe('pending')

      // A decided rejection still terminates its card after reconnect.
      act(() => {
        reconnected.simulateMessage({
          type: 'approval_resolved',
          data: { id: 'spawn:no', slot: ACTIVE, approved: false },
        })
      })
      expect(lifecycleFor('no')).toHaveLength(1)
      const rejectedCopy = i18nT('hooks.useWebSocket.approval_rejected')
      expect(lifecycleFor('no')[0][0].payload.error).toBe(rejectedCopy)
      expect(lifecycleFor('no')[0][0].payload.error).not.toBe('rejected')
      expect(chat().subagents['no']?.status).toBe('error')
    } finally {
      vi.useRealTimers()
    }
  })

  it('ignores an approval resolution that names no owning slot', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'approval_resolved', data: { id: 'spawn:orphan', approved: true } })
    })
    expect(chat().subagents['orphan']).toBeUndefined()
  })

  it('re-fetches the session list when a refresh names history', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'refresh', data: { kinds: ['history'] } }) })
    expect(api.sessions).toHaveBeenCalled()
    expect(dash().refreshTrigger).toBeGreaterThan(0)

    ;(api.sessions as ReturnType<typeof vi.fn>).mockClear()
    act(() => { ws.simulateMessage({ type: 'refresh', data: {} }) })
    expect(api.sessions).not.toHaveBeenCalled()
  })

  it('clears the transcript only for the viewed slot on a slash-clear', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'hi', ts: '1' } }) })
    expect(chat().messages).toHaveLength(1)

    act(() => { ws.simulateMessage({ type: 'slot_clear', data: { slot: BACKGROUND } }) })
    expect(chat().messages).toHaveLength(1)

    act(() => { ws.simulateMessage({ type: 'slot_clear', data: { slot: ACTIVE } }) })
    expect(chat().messages).toHaveLength(0)
  })

  it('re-reads slot metadata after a slash-agent switch', () => {
    const { ws } = mount()
    ;(api.chatSlots as ReturnType<typeof vi.fn>).mockClear()
    act(() => { ws.simulateMessage({ type: 'slot_agent_switch', data: { slot: ACTIVE } }) })
    expect(api.chatSlots).toHaveBeenCalled()
  })

  it('marks a thinking status and re-ranks recency when a user message arrives', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'slots', data: [slotFixture(ACTIVE)] }) })
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'user', content: 'go', ts: '2024-01-01T00:00:00Z' } })
    })
    expect(chat().slotStatusDetail[ACTIVE]?.kind).toBe('thinking')
    // The recency bump is buffered per slot and flushed once per animation frame,
    // so it is not observable until a frame runs. This harness hands rAF a queue
    // that nothing drains on its own — driving it is what makes the assertion read
    // the flushed value rather than the pre-flush undefined.
    act(() => { const pending = rafCbs; rafCbs = []; pending.forEach(cb => cb(0)) })
    expect(dash().slots[0].last_ts).toBe('2024-01-01T00:00:00Z')
  })

  it('routes a chat_message_update by tool_call_id and a patch by timestamp', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'chat_message',
        data: { slot: ACTIVE, role: 'tool', content: 'reading', ts: '10', meta: { tool_call_id: 'tc-1' } },
      })
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'banner', ts: '11' } })
    })

    act(() => {
      ws.simulateMessage({ type: 'chat_message_update', data: { slot: ACTIVE, tool_call_id: 'tc-1', content: 'read 40 lines' } })
      ws.simulateMessage({ type: 'chat_message_update', data: { slot: ACTIVE, ts: '11', meta: { authorized: true } } })
    })
    expect(chat().messages.find(m => m.meta?.tool_call_id === 'tc-1')?.content).toBe('read 40 lines')
    expect(chat().messages.find(m => m.ts === '11')?.meta?.authorized).toBe(true)
  })

  it('walks a queued message through push, edit, reorder, cancel and pop', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'queue_push', data: { slot: ACTIVE, content: 'first', ts: '1', queue_id: 'q1' } })
      ws.simulateMessage({ type: 'queue_push', data: { slot: ACTIVE, content: 'second', ts: '2', queue_id: 'q2' } })
    })
    expect(chat().messages.filter(m => m.role === 'queued').map(m => m.content)).toEqual(['first', 'second'])

    act(() => { ws.simulateMessage({ type: 'queue_edit', data: { slot: ACTIVE, queue_id: 'q1', content: 'first (edited)' } }) })
    expect(chat().messages.find(m => m.meta?.queueId === 'q1')?.content).toBe('first (edited)')

    act(() => { ws.simulateMessage({ type: 'queue_reorder', data: { slot: ACTIVE, order: ['q2', 'q1'] } }) })
    expect(chat().messages.filter(m => m.role === 'queued').map(m => m.meta?.queueId)).toEqual(['q2', 'q1'])

    act(() => { ws.simulateMessage({ type: 'queue_cancel', data: { slot: ACTIVE, queue_id: 'q2' } }) })
    expect(chat().messages.filter(m => m.role === 'queued')).toHaveLength(1)

    act(() => { ws.simulateMessage({ type: 'queue_pop', data: { slot: ACTIVE, queue_id: 'q1' } }) })
    expect(chat().messages.filter(m => m.role === 'queued')).toHaveLength(0)
  })

  it('echoes a mid-turn steer into the target transcript', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'steer_push', data: { slot: ACTIVE, content: 'actually use pnpm', ts: '30' } })
    })
    const steer = chat().messages.find(m => m.meta?.steer === true)
    expect(steer?.content).toBe('actually use pnpm')
    expect(steer?.role).toBe('user')
  })

  it('records a tool call and its result against the slot', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'tool_call',
        data: { slot: ACTIVE, tool: 'fs_read', kind: 'read', purpose: 'Read the config', input_preview: 'config.json', tool_call_id: 'tc-9' },
      })
    })
    expect(chat().slotStatusDetail[ACTIVE]?.kind).toBe('tool')
    expect(chat().slotStatusDetail[ACTIVE]?.toolName).toBe('fs_read')

    act(() => {
      ws.simulateMessage({ type: 'tool_result', data: { slot: ACTIVE, output: '42 lines', tool_call_id: 'tc-9' } })
    })
    expect(chat().toolLog.some(e => e.output?.includes('42 lines'))).toBe(true)
  })

  it('stores an MCP app render payload keyed by its session and tool call', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'mcp_app_render',
        data: { session_key: ACTIVE, tool_call_id: 'tc-app', html: '<p>hi</p>', app: 'demo' },
      })
      // A payload missing its session key is refused before it can be stored.
      ws.simulateMessage({ type: 'mcp_app_render', data: { tool_call_id: 'tc-orphan', html: '' } })
    })
    const stored = Object.values(chat().mcpApps)
    expect(stored).toHaveLength(1)
    expect(stored[0].tool_call_id).toBe('tc-app')
  })

  it('marks a live question card fresh and clears it on resolution', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'question_card',
        data: { slot: ACTIVE, ask_id: 'ask-live', questions: [{ question: 'Ship?', options: [{ label: 'Yes' }] }] },
      })
    })
    expect(chat().pendingQuestions[ACTIVE]?.ask_id).toBe('ask-live')
    expect(chat().pendingQuestions[ACTIVE]?.cardId).toBeTruthy()

    act(() => { ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: 'ask-live' } }) })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
  })

  it('records a resolution for a card it never held, so a later reconcile cannot resurrect it', async () => {
    let releaseSnapshot!: (v: unknown) => void
    const snapshot = new Promise(res => { releaseSnapshot = res })
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockReturnValueOnce(snapshot)

    const { ws } = mount()
    // The reconnect snapshot is already in flight; the resolution lands mid-flight
    // for an ask this client has no local trace of.
    act(() => { ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: 'ask-ghost' } }) })

    await act(async () => {
      releaseSnapshot([{ ask_id: 'ask-ghost', slot: ACTIVE, questions: [{ question: 'Dead?', options: [{ label: 'x' }] }] }])
      await snapshot
    })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
  })

  it('keeps only the renderable fields of a follow-up card', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'followup_card',
        data: {
          slot: ACTIVE,
          ts: 99,
          items: [
            { title: 'Add tests', prompt: 'write tests', description: 'raise coverage', branch: 'test/cov', extra: 'dropped' },
            { title: 'no prompt' },
            null,
          ],
        },
      })
    })
    const card = chat().followups[ACTIVE]
    expect(card?.ts).toBe(99)
    expect(card?.items).toEqual([
      { title: 'Add tests', description: 'raise coverage', prompt: 'write tests', branch: 'test/cov' },
    ])
  })

  it('drops a follow-up frame with no renderable item', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'followup_card', data: { slot: ACTIVE, items: 'not an array' } }) })
    expect(chat().followups[ACTIVE]).toBeUndefined()
  })

  it('accepts a complete folder suggestion and refuses a half-filled one', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'slot_folder_suggestion',
        data: { slot: ACTIVE, folder_id: 'f1', folder_name: 'Reviews', breadcrumb: 'Work / Reviews', ts: 12 },
      })
    })
    expect(chat().folderSuggestions[ACTIVE]).toEqual({
      folderId: 'f1', folderName: 'Reviews', breadcrumb: 'Work / Reviews', ts: 12, turns: 0,
    })

    act(() => {
      ws.simulateMessage({ type: 'slot_folder_suggestion', data: { slot: BACKGROUND, folder_id: 'f2' } })
    })
    expect(chat().folderSuggestions[BACKGROUND]).toBeUndefined()
  })

  it('records a plain activity event without disturbing the model catalog', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'activity_event', data: { slot: ACTIVE, kind: 'session', text: 'warm turn' } })
    })
    expect(chat().toolLog.some(e => e.type === 'session')).toBe(true)
  })

  it('threads the whole subagent lifecycle into one card', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'subagent_spawn', data: { slot: ACTIVE, id: 'ag-1', task: 'Read specs', agent: 'kirocrew' } })
      ws.simulateMessage({ type: 'subagent_queued', data: { slot: ACTIVE, queued: 3 } })
    })
    expect(chat().subagents['ag-1']?.status).toBe('running')
    expect(chat().subagentQueued[ACTIVE]).toBe(3)

    act(() => {
      ws.simulateMessage({ type: 'subagent_chunk', data: { slot: ACTIVE, id: 'ag-1', text: 'partial ' } })
      ws.simulateMessage({ type: 'subagent_tool', data: { slot: ACTIVE, id: 'ag-1', tool: 'fs_read', tool_count: 2 } })
    })
    // Subagent chunks are now buffered and flushed per animation frame (PR #5945).
    act(() => { const pending = rafCbs; rafCbs = []; pending.forEach(cb => cb(0)) })
    expect(chat().subagents['ag-1']?.streaming).toBe('partial ')
    expect(chat().subagents['ag-1']?.lastTool).toBe('fs_read')

    act(() => {
      ws.simulateMessage({ type: 'subagent_stalled', data: { slot: ACTIVE, id: 'ag-1', stalled: true, idle_secs: 90 } })
    })
    expect(chat().subagents['ag-1']?.stalled).toBe(true)

    act(() => {
      ws.simulateMessage({ type: 'subagent_retrying', data: { slot: ACTIVE, id: 'ag-1', attempt: 2 } })
      // The legacy alias routes into the same reducer.
      ws.simulateMessage({ type: 'subagent_recovering', data: { slot: ACTIVE, id: 'ag-1', attempt: 3 } })
    })
    expect(chat().subagents['ag-1']?.retrying).toBe(true)
    expect(chat().subagents['ag-1']?.stalled).toBe(false)

    act(() => {
      ws.simulateMessage({ type: 'subagent_done', data: { slot: ACTIVE, id: 'ag-1', elapsed: 12, outcome: 'completed' } })
    })
    expect(chat().subagents['ag-1']?.status).toBe('done')
    expect(chat().subagents['ag-1']?.elapsed).toBe(12)
  })

  it('fans a coalesced batch of updates and chunks into the per-agent cards', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'subagent_spawn', data: { slot: ACTIVE, id: 'ag-a', task: 'A', agent: 'w' } })
      ws.simulateMessage({ type: 'subagent_spawn', data: { slot: ACTIVE, id: 'ag-b', task: 'B', agent: 'w' } })
      ws.simulateMessage({
        type: 'subagent_batch_update',
        data: { updates: [{ id: 'ag-a', slot: ACTIVE, tool: 'grep', tool_count: 4 }, { id: 'ag-b', slot: ACTIVE, stalled: true }] },
      })
      ws.simulateMessage({
        type: 'subagent_batch_chunks',
        data: { chunks: [{ id: 'ag-a', slot: ACTIVE, text: 'aa' }, { id: 'ag-b', slot: ACTIVE, text: 'bb' }] },
      })
    })
    expect(chat().subagents['ag-a']?.lastTool).toBe('grep')
    expect(chat().subagents['ag-a']?.streaming).toBe('aa')
    expect(chat().subagents['ag-b']?.stalled).toBe(true)
    expect(chat().subagents['ag-b']?.streaming).toBe('bb')
  })

  it('replays a collapsed reconnect snapshot batch through both reducers', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'subagent_snapshot_batch',
        data: {
          items: [
            { type: 'subagent_snapshot', data: { id: 'ag-live', slot: ACTIVE, task: 'T', agent: 'w', streaming: '', last_tool: '', started: 1 } },
            { type: 'subagent_done', data: { slot: ACTIVE, id: 'ag-fin', elapsed: 4, task: 'T2', agent: 'w' } },
            { type: 'unknown_kind', data: {} },
          ],
        },
      })
    })
    expect(chat().subagents['ag-live']?.status).toBe('running')
    expect(chat().subagents['ag-fin']?.status).toBe('done')
  })

  it('accepts the wave lifecycle markers and the heartbeat as no-ops', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'spawn_batch_started', data: { slot: ACTIVE, size: 4 } })
      ws.simulateMessage({ type: 'batch_finished', data: { slot: ACTIVE } })
      ws.simulateMessage({ type: 'heartbeat', data: {} })
      ws.simulateMessage({ type: 'a_type_this_client_has_never_heard_of', data: {} })
    })
    expect(chat().subagents).toEqual({})
    expect(dash().connected).toBe(true)
  })

  it('re-imports a dev-mode app bundle on an app_reload frame', () => {
    const { ws } = mount()
    const seen: string[] = []
    const listener = (e: Event) => { seen.push((e as CustomEvent).detail?.app) }
    window.addEventListener('mc:app-reload', listener)
    try {
      act(() => { ws.simulateMessage({ type: 'app_reload', data: { app: 'dev-fleet' } }) })
      expect(seen).toEqual(['dev-fleet'])
    } finally {
      window.removeEventListener('mc:app-reload', listener)
    }
  })

  it('tracks a dynamic-workflow run and a side-conversation result', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'workflow_run_event',
        data: { run_id: 'run-1', seq: 1, type: 'phase', data: { name: 'discover', phase: 'discover' } },
      })
      ws.simulateMessage({
        type: 'chat.side_result',
        data: { slot: ACTIVE, run_id: 'run-1', role: 'assistant', content: 'side answer', final: true },
      })
    })
    expect(chat().workflowRuns['run-1']).toBeDefined()
    expect(chat().slotSide[ACTIVE]?.messages.some(m => m.content === 'side answer')).toBe(true)
  })

  it('records context usage and a plain status line', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'context_usage', data: { slot: ACTIVE, pct: 42, used_tokens: 4200, window_tokens: 10000 } })
      ws.simulateMessage({ type: 'chat_status', data: { slot: ACTIVE, status: 'Compacting…' } })
    })
    expect(chat().slotContextPct[ACTIVE]).toBe(42)
    expect(chat().slotStatusDetail[ACTIVE]?.text).toBe('Compacting…')

    // A status frame with no text is ignored rather than clearing the detail.
    act(() => { ws.simulateMessage({ type: 'chat_status', data: { slot: ACTIVE } }) })
    expect(chat().slotStatusDetail[ACTIVE]?.text).toBe('Compacting…')
  })

  it('re-reads the transcript when a variant switch names a slot', () => {
    const { ws } = mount()
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockClear()
    act(() => { ws.simulateMessage({ type: 'chat_variant_switch', data: { slot: ACTIVE } }) })
    expect(api.chatSlotDetail).toHaveBeenCalledWith(ACTIVE)
  })

  it('chimes once when a turn completes', () => {
    const { ws } = mount()
    const heard: string[] = []
    const listener = (e: Event) => { heard.push((e as CustomEvent).detail?.kind) }
    window.addEventListener('mc-notification', listener)
    try {
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
      expect(heard).toEqual(['turn'])
      expect(chat().slotStatusDetail[ACTIVE]?.kind).toBe('idle')
    } finally {
      window.removeEventListener('mc-notification', listener)
    }
  })

  it('normalizes an autonudge frame into the authoritative automation map', () => {
    const { ws } = mount()
    const loop = {
      id: 'loop-1', slot_key: ACTIVE, message: 'go', idle_secs: 60,
      active: true, cycle_count: 4, max_cycles: 24, last_fire_ts: 0,
    }
    act(() => {
      ws.simulateMessage({
        type: 'autonudge_state',
        data: { event: 'fired', slot: ACTIVE, loop },
      })
    })
    expect(chat().automations[ACTIVE]).toMatchObject({
      kind: 'legacy_goal_loop', cycleCount: 4, maxCycles: 24,
    })

    act(() => {
      ws.simulateMessage({
        type: 'autonudge_state',
        data: { event: 'removed', slot: ACTIVE, loop },
      })
    })
    expect(chat().automations[ACTIVE]).toBeUndefined()
  })

  it('refreshes the full loop registry when a removal frame arrives', () => {
    const { ws } = mount()
    const invalidate = vi.spyOn(testQueryClient, 'invalidateQueries')
    invalidate.mockClear()

    act(() => {
      ws.simulateMessage({
        type: 'autonudge_state',
        data: { event: 'removed', slot: ACTIVE },
      })
    })

    expect(invalidate).toHaveBeenCalledWith({ queryKey: AUTONUDGE_LOOPS_QUERY_KEY })
  })

  it('removes a structured monitor when its websocket tombstone arrives', () => {
    const { ws } = mount()
    const loop = { ...structuredMonitorLoop(), slot_key: ACTIVE }
    act(() => {
      ws.simulateMessage({
        type: 'autonudge_state',
        data: { event: 'updated', slot: ACTIVE, loop },
      })
    })
    expect(chat().automations[ACTIVE]).toMatchObject({ kind: 'structured_monitor' })
    expect(testQueryClient.getQueryData(['session-automation', ACTIVE]))
      .toMatchObject({ kind: 'structured_monitor' })

    act(() => {
      ws.simulateMessage({
        type: 'autonudge_state',
        data: { event: 'removed', slot: ACTIVE, loop },
      })
    })
    expect(chat().automations[ACTIVE]).toBeUndefined()
  })

  it('tombstones the cached automation before a removal refetch can fail', () => {
    const { ws } = mount()
    const loop = { ...structuredMonitorLoop(), slot_key: ACTIVE }
    testQueryClient.setQueryData(['session-automation', ACTIVE], loop)

    act(() => {
      ws.simulateMessage({
        type: 'autonudge_state',
        data: { event: 'removed', slot: ACTIVE, loop },
      })
    })

    expect(testQueryClient.getQueryData(['session-automation', ACTIVE])).toBeNull()
    expect(chat().automations[ACTIVE]).toBeUndefined()
  })

  it('tombstones a cached automation omitted by a complete reconnect seed', async () => {
    const stale = normalizeAutomationRecord({
      ...structuredMonitorLoop(),
      slot_key: ACTIVE,
    })!
    testStore.dispatch(sseAutomation(stale))
    renderHook(() => useWebSocket(), { wrapper })
    testQueryClient.setQueryData(['session-automation', ACTIVE], stale)

    await act(async () => {
      WS_INSTANCES[0].simulateOpen()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(chat().automations[ACTIVE]).toBeUndefined()
    expect(testQueryClient.getQueryData(['session-automation', ACTIVE])).toBeNull()
  })

  it('ignores an autonudge frame with no slot', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'autonudge_state', data: { event: 'fired' } }) })
    expect(chat().automations).toEqual({})
  })

  it('shows update progress and clears it on the done step', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'update_progress', data: { step: 'download', detail: '40%' } }) })
    expect(dash().updateProgress).toEqual({ step: 'download', detail: '40%' })

    act(() => { ws.simulateMessage({ type: 'update_progress', data: { step: 'done', detail: '' } }) })
    expect(dash().updateProgress).toBeNull()
  })

  it('bumps the refresh trigger for a session restart and a refine frame', () => {
    const { ws } = mount()
    const before = dash().refreshTrigger
    act(() => {
      ws.simulateMessage({ type: 'sessions_restarting', data: { status: 'restarting' } })
      ws.simulateMessage({ type: 'refine', data: {} })
    })
    expect(dash().refreshTrigger).toBe(before + 2)
  })

  it('tracks aggregate subagent status and per-agent text', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'subagent_status', data: { slot: ACTIVE, running: 2, agents: [] } })
      ws.simulateMessage({ type: 'subagent_text', data: { slot: ACTIVE, id: 'ag-1', text: 'progress' } })
      // Neither arm dispatches without the identifying fields.
      ws.simulateMessage({ type: 'subagent_status', data: { running: 5 } })
      ws.simulateMessage({ type: 'subagent_text', data: { slot: ACTIVE, text: 'no id' } })
    })
    expect(dash().subagentRunning[ACTIVE]).toBe(2)
    expect(dash().subagentText[ACTIVE]?.['ag-1']).toBe('progress')
  })

  it('re-broadcasts every channel frame on one window event', () => {
    const { ws } = mount()
    const kinds: string[] = []
    const listener = (e: Event) => { kinds.push((e as CustomEvent).detail?.type) }
    window.addEventListener('kirocrew-channel', listener)
    try {
      act(() => {
        for (const type of [
          'channel_message', 'channel_agent_status', 'channel_created',
          'channel_closed', 'channel_agent_joined', 'channel_agent_left',
        ]) ws.simulateMessage({ type, data: { channel: 'c1' } })
      })
      expect(kinds).toEqual([
        'channel_message', 'channel_agent_status', 'channel_created',
        'channel_closed', 'channel_agent_joined', 'channel_agent_left',
      ])
    } finally {
      window.removeEventListener('kirocrew-channel', listener)
    }
  })

  it('re-broadcasts tool_call so the Browser panel can open on a browse', () => {
    // ChatPage listens for this to auto-open the panel when a shell call turns out
    // to be `playwright-cli`. Without the re-broadcast the listener is live code
    // that can never fire, which is exactly how the previous frame-based trigger
    // broke when its producer was removed.
    const { ws } = mount()
    const seen: unknown[] = []
    const onTool = (e: Event) => { seen.push((e as CustomEvent).detail) }
    window.addEventListener('kirocrew-tool-call', onTool)
    try {
      act(() => {
        ws.simulateMessage({
          type: 'tool_call',
          data: {
            slot: ACTIVE, tool: 'execute_bash', kind: 'tool', purpose: 'open a page',
            input_preview: 'playwright-cli open https://example.com', is_shell: true,
          },
        })
      })
      expect(seen).toHaveLength(1)
      expect((seen[0] as { input_preview: string }).input_preview).toContain('playwright-cli')
    } finally {
      window.removeEventListener('kirocrew-tool-call', onTool)
    }
  })

  it('re-broadcasts cron history and the computer-use mirror frame', () => {
    const { ws } = mount()
    const seen: string[] = []
    const push = (name: string) => () => { seen.push(name) }
    const onCron = push('cron')
    const onBrowser = push('browser')
    const onComputer = push('computer')
    window.addEventListener('cron_history', onCron)
    window.addEventListener('kirocrew-browser-frame', onBrowser)
    window.addEventListener('kirocrew-computer-use-frame', onComputer)
    try {
      act(() => {
        ws.simulateMessage({ type: 'cron_history', data: { job: 'j1' } })
        // No producer sends browser frames: the Browser panel frames the
        // Playwright CLI's own dashboard, so a frame arriving here would be a
        // message from a component that no longer exists.
        ws.simulateMessage({ type: 'browser_frame', data: { image: 'x' } })
        ws.simulateMessage({ type: 'computer_use_frame', data: { image: 'y' } })
      })
      expect(seen).toEqual(['cron', 'computer'])
    } finally {
      window.removeEventListener('cron_history', onCron)
      window.removeEventListener('kirocrew-browser-frame', onBrowser)
      window.removeEventListener('kirocrew-computer-use-frame', onComputer)
    }
  })

  it('patches the sidebar chip and the cached batch from a source_status delta', () => {
    const { ws } = mount()
    const url = 'https://github.com/o/r/pull/1'
    act(() => {
      ws.simulateMessage({
        type: 'slots',
        data: [slotFixture(ACTIVE, { source_links: [{ url, state: 'open', ci: 'running' }] } as Partial<ChatSlot>)],
      })
    })
    act(() => {
      ws.simulateMessage({ type: 'source_status', data: { url, state: 'merged', ci: 'passed' } })
    })
    expect(dash().slots[0].source_links?.[0].state).toBe('merged')
    expect(dash().slots[0].source_links?.[0].ci).toBe('passed')

    // A delta that carries no usable url is dropped before any cache work.
    act(() => { ws.simulateMessage({ type: 'source_status', data: {} }) })
    expect(dash().slots[0].source_links?.[0].state).toBe('merged')
  })

  it('queues, plays and drains voice chunks for the viewed slot', async () => {
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()
    const audio = () => (URL.createObjectURL as ReturnType<typeof vi.fn>).mock.calls.length

    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('first') } })
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('second') } })
    })
    expect(audio()).toBe(2)
    expect(chat().voicePlaying).toBe(true)
    // Only the head plays; the tail waits for `onended`.
    expect(MockAudio.instances).toHaveLength(1)

    act(() => { MockAudio.instances[0].onended?.() })
    expect(MockAudio.instances).toHaveLength(2)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:voice-1')

    // Draining the last chunk stops the playing indicator.
    act(() => { MockAudio.instances[1].onended?.() })
    expect(chat().voicePlaying).toBe(false)
  })

  it('uses the WAV MIME type supplied with a local voice chunk', async () => {
    const blobs: Blob[] = []
    URL.createObjectURL = vi.fn((blob: Blob) => {
      blobs.push(blob)
      return 'blob:voice-wav'
    })
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()

    await act(async () => {
      ws.simulateMessage({
        type: 'voice_chunk',
        data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('wav'), audioMime: 'audio/wav' },
      })
    })

    expect(blobs).toHaveLength(1)
    expect(blobs[0].type).toBe('audio/wav')
  })

  it.each(['media-error', 'autoplay-blocked'])('cancels the entire voice queue after %s', async failure => {
    let rejectPlayback!: (reason: Error) => void
    MockAudio.playResult = () => new Promise((_, reject) => { rejectPlayback = reject })
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()
    const errors = vi.fn()
    window.addEventListener('voice-error', errors)
    try {
      await act(async () => {
        ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('a') } })
        ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('b') } })
      })
      await act(async () => {
        if (failure === 'media-error') MockAudio.instances[0].onerror?.()
        else rejectPlayback(new DOMException('autoplay blocked', 'NotAllowedError'))
      })
      await act(async () => {
        ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('late') } })
      })
      expect(errors).toHaveBeenCalledOnce()
      expect((errors.mock.calls[0][0] as CustomEvent).detail.code).toBe(
        failure === 'media-error' ? 'voice_playback_failed' : 'voice_playback_blocked',
      )
      expect(MockAudio.instances).toHaveLength(1)
      expect(MockAudio.instances[0].pause).toHaveBeenCalledOnce()
      expect(api.voiceCancel).toHaveBeenCalledWith(ACTIVE, 'test-voice')
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:voice-1')
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:voice-2')
      expect(chat().voicePlaying).toBe(false)
    } finally {
      window.removeEventListener('voice-error', errors)
    }
  })

  it('drops voice audio for a background slot and a malformed payload', async () => {
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()
    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: BACKGROUND, audio: btoa('x') } })
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: 'not-base64-@@@' } })
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE } })
    })
    expect(MockAudio.instances).toHaveLength(0)
    expect(chat().voicePlaying).toBe(false)
  })

  it('stores the stitched replay audio from voice_complete', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'voice_complete', data: { slot: ACTIVE, request_id: 'test-voice', audio: 'BASE64MP3' } }) })
    expect(chat().voiceAudio).toBe('BASE64MP3')

    act(() => { ws.simulateMessage({ type: 'voice_complete', data: {} }) })
    expect(chat().voiceAudio).toBe('BASE64MP3')
  })

  it('journals an asynchronous synthesis failure with no ChatPage listener mounted', () => {
    const { ws } = mount()
    const before = recentErrors().map(report => report.id)
    act(() => {
      ws.simulateMessage({ type: 'voice_error', data: {
        slot: ACTIVE, request_id: 'test-voice', code: 'voice_synthesis_failed',
      } })
      // A repeated terminal frame belongs to the already-consumed request.
      ws.simulateMessage({ type: 'voice_error', data: {
        slot: ACTIVE, request_id: 'test-voice', code: 'voice_synthesis_failed',
      } })
    })
    const added = recentErrors().filter(report => !before.includes(report.id))
    expect(added).toHaveLength(1)
    expect(added[0]).toMatchObject({
      source: 'system', code: 'voice_synthesis_failed', endpoint: '/api/voice/synthesize',
    })
  })

  it('interrupts playback and empties the queue on a voice-stop event', async () => {
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()
    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('a') } })
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('b') } })
    })
    expect(chat().voicePlaying).toBe(true)

    act(() => { window.dispatchEvent(new Event('voice-stop')) })
    expect(MockAudio.instances[0].pause).toHaveBeenCalled()
    expect(chat().voicePlaying).toBe(false)
    // The un-played tail url was released, not leaked.
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:voice-2')

    // Muted afterwards: a further chunk is ignored.
    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('c') } })
    })
    expect(chat().voicePlaying).toBe(false)
  })

  it('interrupts playback when the user speaks over the agent', async () => {
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()
    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('a') } })
    })
    expect(chat().voicePlaying).toBe(true)

    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'user', content: 'stop', ts: '9' } })
    })
    expect(chat().voicePlaying).toBe(false)
  })

  it('speaks completed sentences once per flush when auto-speak is on', async () => {
    ;(api.voiceConfig as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ autoSpeak: true })
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    // The auto-speak reader looks at the SINGLETON store's streaming message.
    act(() => {
      globalStore.dispatch(sseChatMessage({
        slot: ACTIVE, role: 'chunk', content: 'This sentence is long enough. ', batched: true,
      }))
    })

    await act(async () => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: ACTIVE, content: 'This sentence is long enough. ', seq: 1 } })
    })
    await act(async () => { rafCbs[0](0) })

    expect(api.voiceSynthesize).toHaveBeenCalledWith(ACTIVE, 'This sentence is long enough.', expect.objectContaining({ request_id: expect.any(String) }))
  })

  it('does not re-speak a sentence it already sent, nor a fragment', async () => {
    ;(api.voiceConfig as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ autoSpeak: true })
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      globalStore.dispatch(sseChatMessage({
        slot: ACTIVE, role: 'chunk', content: 'Short. and then an unterminated tail', batched: true,
      }))
    })
    await act(async () => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: ACTIVE, content: 'x', seq: 1 } })
    })
    await act(async () => { rafCbs[0](0) })
    // A short sentence is still speakable; the unfinished tail waits.
    expect(api.voiceSynthesize).toHaveBeenCalledWith(ACTIVE, 'Short.', expect.objectContaining({ request_id: expect.any(String) }))
  })

  it('speaks the unspoken tail when the turn finishes', async () => {
    ;(api.voiceConfig as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ autoSpeak: true })
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      globalStore.dispatch(sseChatMessage({
        slot: ACTIVE, role: 'assistant', content: 'A complete final answer.', ts: '1',
      }))
    })

    await act(async () => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
    expect(api.voiceSynthesize).toHaveBeenCalledWith(ACTIVE, 'A complete final answer.', expect.objectContaining({ request_id: expect.any(String) }))
  })

  it('re-reads the auto-speak preference when the finished turn had it off', async () => {
    const { ws } = mount()
    ;(api.voiceConfig as ReturnType<typeof vi.fn>).mockClear()
    await act(async () => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
    expect(api.voiceConfig).toHaveBeenCalled()
  })

  it('follows the auto-speak preference when the settings pane changes it', async () => {
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })  // settle the on-open config read
    act(() => {
      window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: { autoSpeak: true } }))
      globalStore.dispatch(sseChatMessage({
        slot: ACTIVE, role: 'assistant', content: 'Spoken because the pane turned it on.', ts: '1',
      }))
    })
    await act(async () => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
    expect(api.voiceSynthesize).toHaveBeenCalledWith(ACTIVE, 'Spoken because the pane turned it on.', expect.objectContaining({ request_id: expect.any(String) }))

    // ...and switching it back off silences the next turn.
    ;(api.voiceSynthesize as ReturnType<typeof vi.fn>).mockClear()
    act(() => {
      window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: { autoSpeak: false } }))
    })
    await act(async () => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
    expect(api.voiceSynthesize).not.toHaveBeenCalled()
  })

  it('survives a notification listener that throws, on every arm that fires one', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const passThrough = window.dispatchEvent.bind(window)
    const dispatchSpy = vi.spyOn(window, 'dispatchEvent').mockImplementation((event: Event) => {
      if (event.type === 'mc-notification') throw new Error('listener exploded')
      return passThrough(event)
    })
    try {
      const { ws } = mount()
      act(() => {
        ws.simulateMessage({ type: 'notification', data: { kind: 'info', title: 'Boom', ts: '1' } })
        ws.simulateMessage({ type: 'approval', data: { id: 'ap-boom', slot: ACTIVE, tool: 'execute_bash', ts: 2 } })
        ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } })
      })
      // Each arm swallowed the listener failure and completed its real work.
      expect(warn).toHaveBeenCalledTimes(3)
      expect(testStore.getState().notifications.items).toHaveLength(2)
      expect(chat().slotStatusDetail[ACTIVE]?.kind).toBe('idle')
    } finally {
      dispatchSpy.mockRestore()
      warn.mockRestore()
    }
  })

  it('cancels the scheduled frame when a finalizing frame flushes synchronously', () => {
    const cancelSpy = vi.fn()
    vi.stubGlobal('cancelAnimationFrame', cancelSpy)
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'chat_chunk', data: { slot: ACTIVE, content: 'buffered', seq: 1 } }) })
    expect(rafCbs).toHaveLength(1)

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
    expect(cancelSpy).toHaveBeenCalledWith(1)
    expect(chat().messages.find(m => m.role === 'assistant')?.content).toBe('buffered')
  })

  it('falls back to a timer when the environment has no animation frames', () => {
    vi.stubGlobal('requestAnimationFrame', undefined)
    vi.stubGlobal('cancelAnimationFrame', undefined)
    vi.useFakeTimers()
    try {
      const { ws } = mount()
      act(() => { ws.simulateMessage({ type: 'chat_chunk', data: { slot: ACTIVE, content: 'timed', seq: 1 } }) })
      expect(chat().messages.find(m => m.role === 'streaming')).toBeUndefined()
      act(() => { vi.advanceTimersByTime(16) })
      expect(chat().messages.find(m => m.role === 'streaming')?.content).toBe('timed')
    } finally {
      vi.useRealTimers()
    }
  })

  it('ignores a resolution with no ask_id and keeps the newest ones after trimming', async () => {
    let releaseSnapshot!: (v: unknown) => void
    const snapshot = new Promise(res => { releaseSnapshot = res })
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockReturnValueOnce(snapshot)

    const { ws } = mount()
    // An empty id is not recordable, so it must be dropped outright.
    act(() => { ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: '' } }) })

    // Overflow the 200-entry bound while the rehydration snapshot is in flight.
    // The oldest ids are trimmed, but the newest must still be recognised as
    // dead so the snapshot cannot resurrect a card this client never held.
    act(() => {
      for (let i = 0; i < 204; i++) {
        ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: `ask-${i}` } })
      }
      ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: 'ask-target' } })
    })

    await act(async () => {
      releaseSnapshot([{ ask_id: 'ask-target', slot: ACTIVE, questions: [{ question: 'Q', options: [{ label: 'x' }] }] }])
      await snapshot
    })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
  })

  it('adopts a still-pending card the snapshot reports when nothing resolved it', async () => {
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { ask_id: 'ask-alive', slot: ACTIVE, questions: [{ question: 'Q', options: [{ label: 'x' }] }] },
    ])
    mount()
    await act(async () => { await Promise.resolve() })
    expect(chat().pendingQuestions[ACTIVE]?.ask_id).toBe('ask-alive')
  })

  it('rehydrates a STATELESS card (card_id, no ask_id) into an empty slot', async () => {
    // A card is a one-shot broadcast with no transcript row, so after a reload
    // the slot's needs-input status would name a question with nothing on screen
    // to answer or dismiss. The server-held record is the only way back.
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { card_id: 'card-alive', slot: ACTIVE, questions: [{ question: 'Which region?', options: [{ label: 'us-east-1' }] }] },
    ])
    mount()
    await act(async () => { await Promise.resolve() })
    const card = chat().pendingQuestions[ACTIVE]
    expect(card?.ask_id).toBeUndefined()
    expect(card?.serverCardId).toBe('card-alive')
    expect(card?.questions[0].question).toBe('Which region?')
  })

  it('drops a stateless card when the server announces its retirement', async () => {
    // Another window answered or dismissed it. The card must leave this window
    // too, or submitting it appends a duplicate turn.
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'question_card',
        data: { slot: ACTIVE, card_id: 'card-live', questions: [{ question: 'Q', options: [{ label: 'x' }] }] },
      })
    })
    expect(chat().pendingQuestions[ACTIVE]?.serverCardId).toBe('card-live')
    // A retirement for a DIFFERENT card must not touch it: a stale announcement
    // for an already-replaced question would otherwise clear the live card.
    act(() => {
      ws.simulateMessage({ type: 'question_card_resolved', data: { card_id: 'card-other', slot: ACTIVE } })
    })
    expect(chat().pendingQuestions[ACTIVE]?.serverCardId).toBe('card-live')
    act(() => {
      ws.simulateMessage({ type: 'question_card_resolved', data: { card_id: 'card-live', slot: ACTIVE } })
    })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
  })

  it('does not rehydrate a stateless card retired while the request was in flight', async () => {
    // The response describes the server as it was when the request was served, so
    // it races the retirement. Re-rendering the card would resurrect a dead ask.
    let release: (v: unknown) => void = () => {}
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockReturnValueOnce(
      new Promise((res) => { release = res }),
    )
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'question_card_resolved', data: { card_id: 'card-ghost', slot: ACTIVE } })
    })
    await act(async () => {
      release([{ card_id: 'card-ghost', slot: ACTIVE, questions: [{ question: 'Q', options: [{ label: 'x' }] }] }])
      await Promise.resolve()
    })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
  })

  it('drops a stateless card the server no longer lists (retired while disconnected)', async () => {
    // A tab that missed the retirement broadcast holds card A while the server
    // has moved on. Keeping it lets a submit answer a question the agent is past;
    // the snapshot's silence about A is the evidence, exactly as for a blocking
    // ask — which is why both kinds go through one reconcile.
    const held = setQuestionCard({
      slot: ACTIVE,
      card_id: 'card-gone',
      questions: [{ question: 'Stale', options: [{ label: 'x' }] }],
      fresh: true,
    })
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
    act(() => {
      globalStore.dispatch(held)
      testStore.dispatch(held as never)
    })
    mount()
    await act(async () => { await Promise.resolve() })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
  })

  it('replaces a held stateless card with the one the server now lists', async () => {
    const held = setQuestionCard({
      slot: ACTIVE,
      card_id: 'card-old',
      questions: [{ question: 'Old', options: [{ label: 'x' }] }],
      fresh: true,
    })
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { card_id: 'card-new', slot: ACTIVE, questions: [{ question: 'New', options: [{ label: 'y' }] }] },
    ])
    act(() => {
      globalStore.dispatch(held)
      testStore.dispatch(held as never)
    })
    mount()
    await act(async () => { await Promise.resolve() })
    expect(chat().pendingQuestions[ACTIVE]?.serverCardId).toBe('card-new')
    expect(chat().pendingQuestions[ACTIVE]?.questions[0].question).toBe('New')
  })

  it('keeps a held stateless card the server still lists', async () => {
    // Reload with the same card pending: the snapshot confirms it, so the drop
    // side must leave it alone. A live set that only collected ask_ids would
    // report every stateless card stale and wipe a live question.
    const held = setQuestionCard({
      slot: ACTIVE,
      card_id: 'card-live',
      questions: [{ question: 'Still asking', options: [{ label: 'x' }] }],
      fresh: true,
    })
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { card_id: 'card-live', slot: ACTIVE, questions: [{ question: 'Still asking', options: [{ label: 'x' }] }] },
    ])
    act(() => {
      globalStore.dispatch(held)
      testStore.dispatch(held as never)
    })
    const deliveryId = chat().pendingQuestions[ACTIVE]?.cardId
    expect(deliveryId).toBeTruthy()
    mount()
    await act(async () => { await Promise.resolve() })
    expect(chat().pendingQuestions[ACTIVE]?.serverCardId).toBe('card-live')
    // The SAME entry, not a drop-and-re-add: a fresh per-delivery id would mean
    // the component remounted, discarding a half-typed answer on every reconnect.
    expect(chat().pendingQuestions[ACTIVE]?.cardId).toBe(deliveryId)
  })

  it('restores a stateless card when a queued answer is cancelled', async () => {
    // The card is cleared optimistically on submit. If the answer was QUEUED and
    // the user then cancels it, nothing ever lands — so without this the slot
    // keeps reporting needs_input with nothing on screen to answer or dismiss.
    // The server still holds the record, so the question comes back.
    // Two Onces, not mockResolvedValue: a persistent mock would leak this
    // snapshot into every later test in the file. The first serves the mount's
    // sync, the second the cancel's.
    const snapshot = [
      { card_id: 'card-unanswered', slot: ACTIVE, questions: [{ question: 'Which region?', options: [{ label: 'us-east-1' }] }] },
    ]
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(snapshot)
      .mockResolvedValueOnce(snapshot)
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    // Simulate the answered-then-cleared state this window would be in.
    act(() => {
      globalStore.dispatch(resolveQuestionCard({ card_id: 'card-unanswered' }))
      testStore.dispatch(resolveQuestionCard({ card_id: 'card-unanswered' }) as never)
    })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()

    await act(async () => {
      ws.simulateMessage({ type: 'queue_cancel', data: { slot: ACTIVE, queue_id: 'q-1' } })
      await Promise.resolve()
    })
    expect(chat().pendingQuestions[ACTIVE]?.serverCardId).toBe('card-unanswered')
  })

  it('never overwrites a card that arrived while the request was in flight', async () => {
    // The response describes the server as it was when the request was served, so
    // one card per slot means adding its row would replace a NEWER live card. The
    // deferred promise is load-bearing: the dispatch has to land strictly between
    // the fetch starting and its resolution, or the test passes on ordering luck
    // instead of on the guard.
    let release: (v: unknown) => void = () => {}
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockReturnValueOnce(
      new Promise((res) => { release = res }),
    )
    const live = setQuestionCard({
      slot: ACTIVE,
      card_id: 'card-new',
      questions: [{ question: 'Live', options: [{ label: 'y' }] }],
      fresh: true,
    })
    mount()
    // Both stores: the hook reads the module store for its snapshots (like the
    // rest of this file's setup) and dispatches into the Provider's test store.
    act(() => {
      globalStore.dispatch(live)
      testStore.dispatch(live as never)
    })
    await act(async () => {
      release([{ card_id: 'card-old', slot: ACTIVE, questions: [{ question: 'Stale', options: [{ label: 'x' }] }] }])
      await Promise.resolve()
    })
    expect(chat().pendingQuestions[ACTIVE]?.serverCardId).toBe('card-new')
    expect(chat().pendingQuestions[ACTIVE]?.questions[0].question).toBe('Live')
  })
})

describe('useWebSocket connection lifecycle', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE },
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => { vi.unstubAllGlobals() })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  it('sends the log subscription only once the socket is open', () => {
    const { result } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]

    // Requested during the handshake: buffered, nothing on the wire yet.
    const cb = vi.fn()
    act(() => { result.current.subscribeLogs(cb) })
    expect(ws.send).not.toHaveBeenCalled()

    // The open handler flushes the buffered request.
    act(() => { ws.simulateOpen() })
    expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: 'subscribe_logs' }))

    act(() => { ws.simulateMessage({ type: 'log', data: { level: 'INFO', msg: 'gateway ready' } }) })
    expect(cb).toHaveBeenCalledWith({ level: 'INFO', msg: 'gateway ready' })

    act(() => { result.current.subscribeLogs(null) })
    expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: 'unsubscribe_logs' }))
  })

  it('toggles the subagent subscription on an open socket only', () => {
    const { result } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]

    act(() => { result.current.subscribeSubagents(false) })
    expect(ws.send).not.toHaveBeenCalled()

    act(() => { ws.simulateOpen() })
    ws.send.mockClear()
    act(() => { result.current.subscribeSubagents(false) })
    expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: 'unsubscribe_subagents' }))

    act(() => { result.current.subscribeSubagents(true) })
    expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: 'subscribe_subagents' }))
  })

  it('doubles the reconnect delay up to the ten-second ceiling', () => {
    vi.useFakeTimers()
    try {
      const { unmount } = renderHook(() => useWebSocket(), { wrapper })
      act(() => { WS_INSTANCES[0].simulateOpen() })
      let attempt = 0
      // Each attempt fails before opening, so the window keeps growing:
      // 1s, 2s, 4s, 8s, then pinned at the 10s ceiling.
      for (const delay of [1000, 2000, 4000, 8000, 10000]) {
        act(() => { WS_INSTANCES[attempt].onclose?.(new CloseEvent('close')) })
        expect(testStore.getState().dashboard.connected).toBe(false)
        // Nothing reconnects a millisecond early.
        act(() => { vi.advanceTimersByTime(delay - 1) })
        expect(WS_INSTANCES).toHaveLength(attempt + 1)
        act(() => { vi.advanceTimersByTime(1) })
        attempt += 1
        expect(WS_INSTANCES).toHaveLength(attempt + 1)
      }
      unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('ignores a close from a socket the hook already replaced', () => {
    vi.useFakeTimers()
    try {
      renderHook(() => useWebSocket(), { wrapper })
      const stale = WS_INSTANCES[0]
      act(() => { stale.simulateOpen() })
      act(() => { stale.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      const live = WS_INSTANCES[1]
      act(() => { live.simulateOpen() })
      expect(testStore.getState().dashboard.connected).toBe(true)

      // The stale socket closing again must not tear down the live connection.
      act(() => { stale.onclose?.(new CloseEvent('close')) })
      expect(testStore.getState().dashboard.connected).toBe(true)
      expect(WS_INSTANCES).toHaveLength(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not schedule a reconnect from the error handler', () => {
    vi.useFakeTimers()
    try {
      renderHook(() => useWebSocket(), { wrapper })
      const ws = WS_INSTANCES[0]
      act(() => { ws.simulateOpen() })
      act(() => { ws.onerror?.(new Event('error')) })
      act(() => { vi.advanceTimersByTime(20000) })
      expect(WS_INSTANCES).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('replaces the socket immediately on a forced reconnect', () => {
    vi.useFakeTimers()
    try {
      const { result } = renderHook(() => useWebSocket(), { wrapper })
      const first = WS_INSTANCES[0]
      act(() => { first.simulateOpen() })

      act(() => { result.current.forceReconnect() })
      // Handlers are detached before close, so no disconnect is dispatched and no
      // second reconnect is queued on top of the immediate one.
      expect(first.onclose).toBeNull()
      expect(first.close).toHaveBeenCalled()
      expect(testStore.getState().dashboard.connected).toBe(true)

      act(() => { vi.advanceTimersByTime(0) })
      expect(WS_INSTANCES).toHaveLength(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('forces a reconnect with no socket to close', () => {
    vi.useFakeTimers()
    try {
      const { result } = renderHook(() => useWebSocket(), { wrapper })
      const first = WS_INSTANCES[0]
      act(() => { first.simulateOpen() })
      act(() => { first.onclose?.(new CloseEvent('close')) })  // wsRef is now null

      act(() => { result.current.forceReconnect() })
      act(() => { vi.advanceTimersByTime(0) })
      expect(WS_INSTANCES).toHaveLength(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('refuses to reconnect after the hook has been torn down', () => {
    vi.useFakeTimers()
    try {
      const { result, unmount } = renderHook(() => useWebSocket(), { wrapper })
      const ws = WS_INSTANCES[0]
      act(() => { ws.simulateOpen() })
      const forceReconnect = result.current.forceReconnect

      act(() => { unmount() })
      expect(ws.close).toHaveBeenCalled()

      act(() => { forceReconnect() })
      act(() => { vi.advanceTimersByTime(20000) })
      expect(WS_INSTANCES).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('rehydrates approvals and question cards the socket missed', async () => {
    ;(api.approvals as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { id: 'ap-missed', slot: ACTIVE, source: 'cron', tool: 'execute_bash', tool_input: '{}', tool_call_id: 'tc-1', ts: 3 },
      // Already in the feed: skipped rather than delivered twice.
      { id: 'ap-known', slot: ACTIVE, source: 'cron', tool: 'execute_bash', ts: 4 },
      // No slot: feed only.
      { id: 'ap-global' },
    ])
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { ask_id: 'ask-missed', slot: ACTIVE, questions: [{ question: 'Resume?', options: [{ label: 'Yes' }] }] },
    ])
    // The dedupe check reads the SINGLETON feed.
    globalStore.dispatch(addNotification({
      kind: 'approval', title: 'known', body: '', ts: '4', approval_id: 'ap-known',
    } as Parameters<typeof addNotification>[0]))

    try {
      const { result } = renderHook(() => useWebSocket(), { wrapper })
      await act(async () => { WS_INSTANCES[0].simulateOpen() })
      await act(async () => { await Promise.resolve() })

      const notifs = testStore.getState().notifications.items
      expect(notifs.filter(n => n.approval_id === 'ap-missed')).toHaveLength(1)
      expect(notifs.some(n => n.approval_id === 'ap-known')).toBe(false)
      expect(notifs.some(n => n.approval_id === 'ap-global')).toBe(true)
      // Only the slot-owning approval got an inline card.
      expect(testStore.getState().chat.messages.filter(m => m.role === 'permission')).toHaveLength(1)
      expect(testStore.getState().chat.pendingQuestions[ACTIVE]?.ask_id).toBe('ask-missed')
      expect(result.current.forceReconnect).toBeTypeOf('function')
    } finally {
      globalStore.dispatch(removeNotificationByTs('4'))
    }
  })

  it('survives a rehydration whose endpoints reject', async () => {
    ;(api.approvals as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('gateway down'))
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('gateway down'))
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockImplementationOnce(() => { throw new Error('sync boom') })
    testStore.dispatch(sseAutomation({
      kind: 'legacy_goal_loop', id: 'legacy-live', slotKey: ACTIVE, message: 'Keep going',
      idleSecs: 60, maxCycles: 0, cycleCount: 2, active: true, lastFireAt: 0,
      stoppedReason: '',
    }))

    renderHook(() => useWebSocket(), { wrapper })
    await act(async () => { WS_INSTANCES[0].simulateOpen() })
    await act(async () => { await Promise.resolve() })

    // A cosmetic seed throwing synchronously must not strand the subscribe.
    expect(WS_INSTANCES[0].send).toHaveBeenCalledWith(JSON.stringify({ type: 'subscribe_subagents' }))
    expect(testStore.getState().dashboard.connected).toBe(true)
    // A successful structured snapshot must not turn a failed legacy read into
    // an authoritative empty legacy snapshot.
    expect(testStore.getState().chat.automations[ACTIVE]?.kind).toBe('legacy_goal_loop')
  })

  it('drops a stale question card the server no longer lists after a reconnect', async () => {
    vi.useFakeTimers()
    // The reconcile diffs against the SINGLETON pending map, so the card has to
    // be there as well as in the Provider store.
    globalStore.dispatch(setQuestionCard({
      slot: ACTIVE, ask_id: 'ask-stale', questions: [{ question: 'Still there?', options: [{ label: 'x' }] }],
    }))
    try {
      const { unmount } = renderHook(() => useWebSocket(), { wrapper })
      const first = WS_INSTANCES[0]
      act(() => { first.simulateOpen() })
      act(() => {
        first.simulateMessage({
          type: 'question_card',
          data: { slot: ACTIVE, ask_id: 'ask-stale', questions: [{ question: 'Still there?', options: [{ label: 'x' }] }] },
        })
      })
      expect(testStore.getState().chat.pendingQuestions[ACTIVE]?.ask_id).toBe('ask-stale')

      // It was answered elsewhere while this client was offline: the reconnect
      // snapshot lists nothing, so the card must go.
      act(() => { first.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      vi.useRealTimers()
      const second = WS_INSTANCES[1]
      await act(async () => { second.simulateOpen() })
      await act(async () => { await Promise.resolve() })

      expect(testStore.getState().chat.pendingQuestions[ACTIVE]).toBeUndefined()
      unmount()
    } finally {
      vi.useRealTimers()
      globalStore.dispatch(resolveQuestionCard({ ask_id: 'ask-stale' }))
    }
  })

  it('re-reads the viewed transcript and re-subscribes on reconnect', () => {
    vi.useFakeTimers()
    try {
      globalStore.dispatch(setActiveSlot(ACTIVE))
      const { unmount } = renderHook(() => useWebSocket(), { wrapper })
      const first = WS_INSTANCES[0]
      act(() => { first.simulateOpen() })
      act(() => { first.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockClear()

      const second = WS_INSTANCES[1]
      act(() => { second.simulateOpen() })
      expect(api.chatSlotDetail).toHaveBeenCalledWith(ACTIVE)
      expect(second.send).toHaveBeenCalledWith(JSON.stringify({ type: 'subscribe_subagents' }))
      unmount()
    } finally {
      vi.useRealTimers()
      globalStore.dispatch(setActiveSlot(null))
    }
  })

  it('re-sends a live log subscription across a reconnect', () => {
    vi.useFakeTimers()
    try {
      const { result, unmount } = renderHook(() => useWebSocket(), { wrapper })
      const first = WS_INSTANCES[0]
      act(() => { first.simulateOpen() })
      act(() => { result.current.subscribeLogs(vi.fn()) })

      act(() => { first.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      const second = WS_INSTANCES[1]
      act(() => { second.simulateOpen() })
      expect(second.send).toHaveBeenCalledWith(JSON.stringify({ type: 'subscribe_logs' }))
      unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('resets the backoff window after a successful reconnect', () => {
    vi.useFakeTimers()
    try {
      const { unmount } = renderHook(() => useWebSocket(), { wrapper })
      act(() => { WS_INSTANCES[0].simulateOpen() })
      act(() => { WS_INSTANCES[0].onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      // Second socket connects successfully, which resets the delay to 1s.
      act(() => { WS_INSTANCES[1].simulateOpen() })
      act(() => { WS_INSTANCES[1].onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(999) })
      expect(WS_INSTANCES).toHaveLength(2)
      act(() => { vi.advanceTimersByTime(1) })
      expect(WS_INSTANCES).toHaveLength(3)
      unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('detaches the voice listeners it registered when unmounted', async () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await act(async () => { WS_INSTANCES[0].simulateOpen() })
    act(() => { unmount() })

    // Firing after teardown must not touch the store.
    act(() => { window.dispatchEvent(new Event('voice-stop')) })
    act(() => { window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: { autoSpeak: true } })) })
    expect(testStore.getState().chat.voicePlaying).toBe(false)
  })
})

describe('useWebSocket slots reconcile', () => {
  it('prunes per-slot caches for sessions the authoritative list dropped', () => {
    const testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE },
    })
    testStore.dispatch(sseChatMessage({ slot: BACKGROUND, role: 'assistant', content: 'archived', ts: '1' }))
    expect(testStore.getState().chat.slotMessages[BACKGROUND]).toBeDefined()

    testStore.dispatch(sseSlots([{ key: ACTIVE, title: ACTIVE, agent: 'kirocrew' } as ChatSlot]))
    expect(testStore.getState().chat.slotMessages[BACKGROUND]).toBeUndefined()
  })

  it('treats an empty slots frame as a no-op rather than a purge', () => {
    const testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE },
    })
    testStore.dispatch(sseChatMessage({ slot: BACKGROUND, role: 'assistant', content: 'keep me', ts: '1' }))
    testStore.dispatch(sseSlots([]))
    expect(testStore.getState().chat.slotMessages[BACKGROUND]).toBeDefined()
  })
})
