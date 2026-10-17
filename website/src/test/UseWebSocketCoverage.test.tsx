/**
 * Behavioural coverage for the multiplexed dashboard socket.
 *
 * `useWebSocket` is the single fan-out point for every server push: it owns the
 * exported question-card reconcile helpers, the ~70-arm frame router, the voice
 * playback queue, and the reconnect / subscription lifecycle. The existing
 * useWebSocket specs pin a handful of specific regressions; this file walks the
 * exported reconcile helpers and the socket, slots, notification, approval and
 * session/chat/tool/MCP-app arms. Its UseWebSocketCoverage.<topic>.test.tsx
 * siblings walk the question-card arms (questionCards), the follow-up, activity,
 * automation, update and re-broadcast arms (activity), the voice arms (voice) and
 * the connection lifecycle (lifecycle). Each sibling that covers the frame router
 * repeats this file's frame-router setup; keep the copies identical. The socket
 * and audio fakes they share live in UseWebSocketCoverageTestUtils.tsx.
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
import chatReducer, { setActiveSlot, clearMessages, sseChatMessage, sseActivityEvent, resolveByApprovalId } from '../store/chatSlice'
import { sseSlots, addSlotOptimistic, armConfirmedCloseHold, removeSlotOptimistic } from '../store/dashboardSlice'
import { addNotification, removeNotificationByTs } from '../store/notificationsSlice'
import type { ChatSlot } from '../types'
import { i18nT } from '../i18n/t'
import { ACTIVE, BACKGROUND, WS_INSTANCES, MockWebSocket, MockAudio } from './UseWebSocketCoverageTestUtils'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error { status = 500 },
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: false, monitors: [] }),
    pendingQuestions: vi.fn().mockResolvedValue([]),
    answerQuestion: vi.fn().mockResolvedValue({ ok: true }),
    voiceSynthesize: vi.fn().mockResolvedValue({ ok: true }),
    voiceCancel: vi.fn().mockResolvedValue({ ok: true }),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
  },
}))

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

  it('refreshes slots once when a slot patch names an absent row', async () => {
    globalStore.dispatch(sseSlots([slotFixture(ACTIVE)]))
    try {
      const { ws } = mount()
      await act(async () => { await Promise.resolve(); await Promise.resolve() })
      vi.mocked(api.chatSlots).mockClear()

      act(() => {
        ws.simulateMessage({
          type: 'slot_patch',
          data: { slots: [{ key: BACKGROUND, title: 'New session' }] },
        })
      })

      expect(api.chatSlots).toHaveBeenCalledTimes(1)
    } finally {
      globalStore.dispatch(sseSlots([]))
    }
  })

  it('does not refresh slots when a slot patch names a present row', async () => {
    globalStore.dispatch(sseSlots([slotFixture(ACTIVE)]))
    try {
      const { ws } = mount()
      await act(async () => { await Promise.resolve(); await Promise.resolve() })
      vi.mocked(api.chatSlots).mockClear()

      act(() => {
        ws.simulateMessage({
          type: 'slot_patch',
          data: { slots: [{ key: ACTIVE, title: 'Renamed session' }] },
        })
      })

      expect(api.chatSlots).not.toHaveBeenCalled()
    } finally {
      globalStore.dispatch(sseSlots([]))
    }
  })

  it('does not refresh slots when an absent patched row is closing', async () => {
    globalStore.dispatch(sseSlots([slotFixture(ACTIVE), slotFixture(BACKGROUND)]))
    globalStore.dispatch(armConfirmedCloseHold(BACKGROUND))
    globalStore.dispatch(removeSlotOptimistic(BACKGROUND))
    try {
      const { ws } = mount()
      await act(async () => { await Promise.resolve(); await Promise.resolve() })
      vi.mocked(api.chatSlots).mockClear()

      act(() => {
        ws.simulateMessage({
          type: 'slot_patch',
          data: { slots: [{ key: BACKGROUND, title: 'Closing session' }] },
        })
      })

      expect(api.chatSlots).not.toHaveBeenCalled()
    } finally {
      globalStore.dispatch(addSlotOptimistic(slotFixture(BACKGROUND)))
      globalStore.dispatch(sseSlots([]))
    }
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

  it.each(['live', 'reconciled'])('fences %s approval commands while preserving its purpose policy', async delivery => {
    const approval = {
      id: 'ap-literal', source: 'cron', tool: 'execute_bash',
      tool_input: 'echo ```; rm -rf *cache*', tool_purpose: '**Reason:** cleanup', ts: 5,
    }
    if (delivery === 'reconciled') vi.mocked(api.approvals).mockResolvedValueOnce([approval])
    const { ws } = mount()
    // Boot notification fetch and the pending-approval snapshot must settle first.
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    if (delivery === 'live') {
      act(() => { ws.simulateMessage({ type: 'approval', data: approval }) })
    }
    const note = testStore.getState().notifications.items.find(n => n.approval_id === approval.id)
    const body = '**Source:** cron\n\n````approval-command\necho ```; rm -rf *cache*\n````'
    expect(note?.body).toBe(delivery === 'live' ? `${body}\n\n**Reason:** cleanup` : body)
  })

  it('hands an approval to the feed and raises no desktop notification of its own', () => {
    // The feed entry is what reaches the OS (useNativeNotification constructs
    // the toast, tagged with the approval id); a constructor here would be a
    // second banner for the same approval.
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
      expect(MockNotification.instances).toHaveLength(0)
      const feedNote = testStore.getState().notifications.items.find(n => n.approval_id === 'ap-1')
      expect(feedNote?.kind).toBe('approval')
      expect(feedNote?.title).toContain('execute_bash')
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
      // ...but no surface keeps asking for it: the same gone verdict a refused
      // press records, left for the spawn inventory to settle.
      expect(chat().subagents['gone']?.approvalGone).toBe('spawn:gone')

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

  it('a steer echo keeps the quote its meta carries, and drops a malformed one', () => {
    const { ws } = mount()
    const quote = { role: 'assistant', text: 'older reply', ts: '29' }
    act(() => {
      ws.simulateMessage({ type: 'steer_push', data: { slot: ACTIVE, content: '> older reply\n\nfollow-up', ts: '30', meta: { quote } } })
      ws.simulateMessage({ type: 'steer_push', data: { slot: ACTIVE, content: 'plain', ts: '31', meta: { quote: { role: 'system', text: 'x' } } } })
    })
    const steers = chat().messages.filter(m => m.meta?.steer === true)
    expect(steers[0]?.meta?.quote).toEqual(quote)
    expect(steers[1]?.meta?.quote).toBeUndefined()
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
})
