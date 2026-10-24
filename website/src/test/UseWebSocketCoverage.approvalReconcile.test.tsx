/**
 * Behavioural coverage for the multiplexed dashboard socket: the approval
 * reconcile a reconnect runs against the authority's listing, and the live
 * frames that race it (out-of-order reads, ids reused during a read, decisions
 * and expiries that land while a snapshot is in flight). The rest of the frame
 * router is covered in UseWebSocketCoverage.test.tsx and its siblings. This file
 * repeats that file's frame-router setup; keep the copies identical. The socket
 * and audio fakes are in UseWebSocketCoverageTestUtils.tsx.
 *
 * Harness note: the hook DISPATCHES through the Provider store but READS
 * (`activeSlot`, `messages`, `slotStatusDetail`) off the singleton store
 * imported from `../store`. Read-dependent paths therefore prime the singleton
 * explicitly and reset it afterwards, matching useWebSocket.approvalRouting.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import { store as globalStore } from '../store'
import chatReducer, { setActiveSlot, clearMessages, sseChatMessage, sseActivityEvent, resolveApprovalRow } from '../store/chatSlice'
import { addNotification, deleteNotification } from '../store/notificationsSlice'
import { ACTIVE, WS_INSTANCES, MockWebSocket, MockAudio } from './UseWebSocketCoverageTestUtils'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error { status = 500 },
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    ackNotification: vi.fn().mockResolvedValue({ ok: true }),
    deleteNotification: vi.fn(() => new Promise(() => {})),
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

describe('useWebSocket frame router: approval reconcile', () => {
  let testStore: ReturnType<typeof createTestStore>
  let rafCbs: FrameRequestCallback[]
  let originalCreateObjectUrl: typeof URL.createObjectURL
  let originalRevokeObjectUrl: typeof URL.revokeObjectURL

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
    // A test that throws between its own useFakeTimers/useRealTimers pair
    // must not hand fake timers to the next test.
    vi.useRealTimers()
    vi.unstubAllGlobals()
    URL.createObjectURL = originalCreateObjectUrl
    URL.revokeObjectURL = originalRevokeObjectUrl
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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
  // An approval_resolved frame removes the request's feed row.
  const listed = (approvalId: string) =>
    testStore.getState().notifications.items.some(n => n.approval_id === approvalId)

  it('overlapping reconnect reads answering out of order keep the newer adoption live', async () => {
    type ApprovalSnapshot = Awaited<ReturnType<typeof api.approvals>>
    const id = 'ap-raised-while-away'
    const approvals = api.approvals as ReturnType<typeof vi.fn>
    approvals.mockResolvedValueOnce([])
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    // Two reconnects in quick succession. The first read stalls; the second
    // answers first and lists a request raised while the socket was down.
    let answerOlder: (v: ApprovalSnapshot) => void = () => {}
    let answerNewer: (v: ApprovalSnapshot) => void = () => {}
    approvals
      .mockImplementationOnce(() => new Promise<ApprovalSnapshot>(r => { answerOlder = r }))
      .mockImplementationOnce(() => new Promise<ApprovalSnapshot>(r => { answerNewer = r }))
    const reconnect = async (n: number) => {
      vi.useFakeTimers()
      act(() => { WS_INSTANCES[n - 1].onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      vi.useRealTimers()
      await act(async () => { WS_INSTANCES[n].simulateOpen(); await Promise.resolve(); await Promise.resolve() })
    }
    try {
      expect(WS_INSTANCES[0]).toBe(ws)
      await reconnect(1)
      await waitFor(() => expect(approvals).toHaveBeenCalledTimes(2))
      await reconnect(2)
      await waitFor(() => expect(approvals).toHaveBeenCalledTimes(3))
      await act(async () => {
        answerNewer([{ id, instance: 'inst-c', slot: ACTIVE, source: 'agent', tool: 'late_tool', ts: 90 }] as ApprovalSnapshot)
        await Promise.resolve(); await Promise.resolve()
      })
      await waitFor(() => {
        expect(testStore.getState().notifications.items.filter(n => n.approval_id === id).map(n => n.approval_instance)).toEqual(['inst-c'])
      })
      // The older read, taken before the request was raised, lands last.
      await act(async () => {
        answerOlder([] as ApprovalSnapshot)
        await Promise.resolve(); await Promise.resolve()
      })
      const rows = testStore.getState().notifications.items.filter(n => n.approval_id === id)
      expect(rows.map(n => [n.approval_instance, Object.hasOwn(testStore.getState().notifications.retiredApprovals ?? {}, n.ts)]))
        .toEqual([['inst-c', false]])
      const card = chat().messages.filter(m => m.role === 'permission' && m.meta?.approval_id === id)
      expect(card.map(m => m.meta?.resolved)).toEqual([undefined])
    } finally {
      vi.useRealTimers()
      testStore.dispatch(deleteNotification.fulfilled('90', '', '90'))
      globalStore.dispatch(deleteNotification.fulfilled('90', '', '90'))
    }
  })

  it('a decision made elsewhere and an expiry both remove their rows, with no DELETE', () => {
    const del = api.deleteNotification as unknown as ReturnType<typeof vi.fn>
    del.mockClear()
    const decided = { kind: 'approval', title: 'Decided', body: '', ts: '70', approval_id: 'ap-elsewhere', approval_instance: 'inst-e', slot: ACTIVE } as Parameters<typeof addNotification>[0]
    const expired = { ...decided, title: 'Expired', ts: '71', approval_id: 'ap-expiring', approval_instance: 'inst-x' }
    const { ws } = mount()
    for (const n of [decided, expired]) { testStore.dispatch(addNotification(n)); globalStore.dispatch(addNotification(n)) }
    try {
      act(() => {
        ws.simulateMessage({ type: 'approval_resolved', data: { id: 'ap-elsewhere', approved: true, instance: 'inst-e' } })
        ws.simulateMessage({ type: 'approval_resolved', data: { id: 'ap-expiring', approved: false, decision: 'expired', instance: 'inst-x' } })
      })
      // The rows are this tab's own copies: nothing stored to delete.
      expect(del).not.toHaveBeenCalled()
      expect(listed('ap-elsewhere')).toBe(false)
      expect(listed('ap-expiring')).toBe(false)
    } finally {
      for (const ts of ['70', '71']) globalStore.dispatch(deleteNotification.fulfilled(ts, '', ts))
    }
  })

  it("the frame for this tab's own decision removes the row", () => {
    // No view holds a lock over the decide: the frame settles the row as it
    // would any decision made elsewhere, and the view's own settle that
    // follows finds the row already gone.
    const del = api.deleteNotification as unknown as ReturnType<typeof vi.fn>
    del.mockClear()
    const row = { kind: 'approval', title: 'Mine', body: '', ts: '72', approval_id: 'ap-mine', approval_instance: 'inst-m', slot: ACTIVE } as Parameters<typeof addNotification>[0]
    const { ws } = mount()
    // The frame handler reads the singleton store (see the harness note above).
    for (const st of [testStore, globalStore]) st.dispatch(addNotification(row))
    try {
      act(() => {
        ws.simulateMessage({ type: 'approval_resolved', data: { id: 'ap-mine', approved: true, instance: 'inst-m' } })
      })
      expect(listed('ap-mine')).toBe(false)
      expect(del).not.toHaveBeenCalled()
    } finally {
      globalStore.dispatch(deleteNotification.fulfilled('72', '', '72'))
    }
  })

  it('a snapshot fetched before a live frame neither replaces nor drops the row that frame raised', async () => {
    type ApprovalSnapshot = Awaited<ReturnType<typeof api.approvals>>
    const id = 'ap-raised-during-read'
    let release: (value: ApprovalSnapshot) => void = () => {}
    const pending = new Promise<ApprovalSnapshot>((resolve) => { release = resolve })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    const { ws } = mount()
    try {
      await act(async () => { await Promise.resolve(); await Promise.resolve() })
      expect(api.approvals).toHaveBeenCalledTimes(1)
      // Request B arrives live while the read is in flight.
      act(() => {
        ws.simulateMessage({
          type: 'approval',
          data: { id, instance: 'inst-b', slot: ACTIVE, source: 'agent', tool: 'live_tool', ts: 67 },
        })
        // The registry reads the app store; mirror the frame's feed row there.
        globalStore.dispatch(addNotification({
          kind: 'approval', title: 'Live', body: '', ts: '67', approval_id: id, approval_instance: 'inst-b', slot: ACTIVE,
        } as Parameters<typeof addNotification>[0]))
      })
      // The read answers with request A, which B replaced under the same id.
      await act(async () => {
        release([{ id, instance: 'inst-a', slot: ACTIVE, source: 'agent', tool: 'stale_tool', ts: 66 }])
        await pending
        await Promise.resolve()
      })
      const rows = testStore.getState().notifications.items.filter(n => n.approval_id === id)
      expect(rows.map(n => [n.ts, n.approval_instance])).toEqual([['67', 'inst-b']])
    } finally {
      for (const ts of ['66', '67']) globalStore.dispatch(deleteNotification.fulfilled(ts, '', ts))
    }
  })

  it('a snapshot that omits an id re-raised live during the read does not retire it', async () => {
    type ApprovalSnapshot = Awaited<ReturnType<typeof api.approvals>>
    const id = 'ap-reraised-during-read'
    const note = {
      kind: 'approval', title: 'Live', body: '', ts: '68', approval_id: id, approval_instance: 'inst-1', slot: ACTIVE,
    } as Parameters<typeof addNotification>[0]
    const { ws } = mount()
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    act(() => {
      ws.simulateMessage({ type: 'approval', data: { id, instance: 'inst-1', slot: ACTIVE, source: 'agent', tool: 'first_tool', ts: 68 } })
      globalStore.dispatch(addNotification(note))
    })
    let release: (value: ApprovalSnapshot) => void = () => {}
    const pending = new Promise<ApprovalSnapshot>((resolve) => { release = resolve })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    try {
      // A reconnect read begins while the id is held, then the id is raised
      // again live; the answer predates that frame and omits the id.
      vi.useFakeTimers()
      act(() => { ws.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      vi.useRealTimers()
      const reconnected = WS_INSTANCES[1]
      await act(async () => { reconnected.simulateOpen(); await Promise.resolve() })
      act(() => {
        reconnected.simulateMessage({ type: 'approval', data: { id, instance: 'inst-1', slot: ACTIVE, source: 'agent', tool: 'first_tool', ts: 68 } })
      })
      await act(async () => { release([]); await pending; await Promise.resolve() })
      expect(testStore.getState().notifications.items.some(n => n.approval_id === id)).toBe(true)
      expect(chat().messages.find(m => m.meta?.approval_id === id)?.meta?.resolved).toBeUndefined()
    } finally {
      globalStore.dispatch(deleteNotification.fulfilled('68', '', '68'))
    }
  })

  it('removes a slotless cron approval on its expiry, which has no chat row to match', () => {
    const { ws } = mount()
    try {
      act(() => {
        ws.simulateMessage({
          type: 'approval',
          data: { id: 'ap-cron', instance: 'inst-cron', source: 'cron', tool: 'cron_tool', ts: 41 },
        })
      })
      globalStore.dispatch(addNotification({
        kind: 'approval', title: 'Cron approval', body: '', ts: '41', approval_id: 'ap-cron', approval_instance: 'inst-cron',
      } as Parameters<typeof addNotification>[0]))
      act(() => {
        ws.simulateMessage({
          type: 'approval_resolved',
          data: { id: 'ap-cron', approved: false, decision: 'expired', instance: 'inst-cron' },
        })
      })
      expect(listed('ap-cron')).toBe(false)
    } finally {
      globalStore.dispatch(deleteNotification.fulfilled('41', '', '41'))
    }
  })

  it('an instance-bearing resolution settles the coordinator row, never a colliding chat-runner row', () => {
    const id = 'ap-same-slot-collision'
    // A chat-runner permission row under the same id, in the same slot, first.
    testStore.dispatch(sseChatMessage({
      slot: ACTIVE, role: 'permission', content: '[agent] native_tool', ts: '70',
      meta: { approval_id: id, tool_input: '' },
    }))
    const { ws } = mount()
    try {
      act(() => {
        ws.simulateMessage({
          type: 'approval',
          data: { id, instance: 'inst-coord', slot: ACTIVE, source: 'agent', tool: 'coord_tool', ts: 71 },
        })
      })
      globalStore.dispatch(addNotification({
        kind: 'approval', title: 'Coordinator approval', body: '', ts: '71', approval_id: id, approval_instance: 'inst-coord', slot: ACTIVE,
      } as Parameters<typeof addNotification>[0]))
      act(() => {
        ws.simulateMessage({ type: 'approval_resolved', data: { id, slot: ACTIVE, approved: true, instance: 'inst-coord' } })
      })
      const rows = chat().messages.filter(m => m.role === 'permission' && m.meta?.approval_id === id)
      expect(rows).toHaveLength(2)
      expect(rows.find(m => m.meta?.registry == null)?.meta?.resolved).toBeUndefined()
      expect(rows.find(m => m.meta?.registry === 'coordinator')?.meta?.resolved).toBe('approved')
      expect(listed(id)).toBe(false)
    } finally {
      globalStore.dispatch(deleteNotification.fulfilled('71', '', '71'))
    }
  })

  it.each([
    ['names no row', {}],
    ['carries the row mid', { mid: 'm1' }],
  ])('a coordinator resolution with no instance settles no native row under its id (%s)', (_label, extra) => {
    // The server can send one: an audit broadcast with an empty instance, or a
    // takeover of a record that had none. It names no coordinator request, so
    // it must not fall through to the runner's path and settle a native row
    // that only shares the id.
    const id = 'ap-coord-no-instance'
    const native = { origin: 'native' as const, id, slot: ACTIVE, mid: 'm1' }
    for (const s of [testStore, globalStore]) {
      s.dispatch(sseChatMessage({
        slot: ACTIVE, role: 'permission', content: '[agent] native_tool', ts: '90',
        meta: { approval_id: id, mid: 'm1', tool_input: '' },
      }))
      s.dispatch(sseActivityEvent({
        slot: ACTIVE, kind: 'approval', text: 'native_tool', approval_id: id, approval_type: 'chat', approval_target: native,
      }))
    }
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'approval_resolved', data: { id, slot: ACTIVE, origin: 'coordinator', approved: true, ...extra } })
    })
    expect(chat().messages.find(m => m.meta?.approval_id === id)?.meta?.resolved).toBeUndefined()
    expect(chat().toolLog.some(e => e.approval_id === id && e.type === 'approval_resolved')).toBe(false)
  })

  it('a native resolution naming one row mid settles only that row, not a same-id row beside it', () => {
    // Request A's card is still in the slot when request B reuses its id. A
    // runner frame for B names B's row, so A's row is left exactly as it was.
    const id = 'ap-native-reused-id'
    for (const s of [testStore, globalStore]) {
      s.dispatch(sseChatMessage({
        slot: ACTIVE, role: 'permission', content: '[agent] tool A', ts: '95',
        meta: { approval_id: id, mid: 'm-old', tool_input: '' },
      }))
      s.dispatch(sseChatMessage({
        slot: ACTIVE, role: 'permission', content: '[agent] tool B', ts: '96',
        meta: { approval_id: id, mid: 'm-live', tool_input: '' },
      }))
    }
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'approval_resolved', data: { id, slot: ACTIVE, origin: 'native', mid: 'm-live', approved: true } })
    })
    const rows = chat().messages.filter(m => m.meta?.approval_id === id)
    expect(rows).toHaveLength(2)
    expect(rows.find(m => m.meta?.mid === 'm-live')?.meta?.resolved).toBe('approved')
    expect(rows.find(m => m.meta?.mid === 'm-old')?.meta?.resolved).toBeUndefined()
  })

  it('a tracked resolution with only a chat-runner row in its slot leaves that row pending', () => {
    const id = 'ap-slotless-vs-native'
    testStore.dispatch(sseChatMessage({
      slot: ACTIVE, role: 'permission', content: '[agent] native_tool', ts: '72',
      meta: { approval_id: id, tool_input: '' },
    }))
    const { ws } = mount()
    try {
      act(() => {
        ws.simulateMessage({
          type: 'approval',
          data: { id, instance: 'inst-cron2', source: 'cron', tool: 'cron_tool', ts: 73 },
        })
      })
      act(() => {
        ws.simulateMessage({ type: 'approval_resolved', data: { id, slot: ACTIVE, approved: true, instance: 'inst-cron2' } })
      })
      expect(chat().messages.find(m => m.meta?.approval_id === id)?.meta?.resolved).toBeUndefined()
    } finally {
      globalStore.dispatch(deleteNotification.fulfilled('73', '', '73'))
    }
  })

  it('a coordinator resolution this tab cannot place leaves a chat-runner row under the id pending', () => {
    // No coordinator request under the id is tracked here, so the resolution
    // did not end the chat runner's request that shares it.
    const id = 'ap-unplaced-takeover'
    testStore.dispatch(sseChatMessage({
      slot: ACTIVE, role: 'permission', content: '[agent] native_tool', ts: '74',
      meta: { approval_id: id, tool_input: '' },
    }))
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'approval_resolved',
        data: { id, slot: ACTIVE, approved: false, instance: 'inst-elsewhere' },
      })
    })
    expect(chat().messages.find(m => m.meta?.approval_id === id)?.meta?.resolved).toBeUndefined()
  })

  it('keeps a request that reused an id during the reconcile read, removing only the earlier one', async () => {
    type ApprovalSnapshot = Awaited<ReturnType<typeof api.approvals>>
    const id = 'ap-replaced-mid-read'
    const note = (ts: string, instance: string) => ({
      kind: 'approval', title: 'Approval', body: '', ts, approval_id: id, approval_instance: instance, slot: ACTIVE,
    } as Parameters<typeof addNotification>[0])
    ;(api.approvals as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    try {
      act(() => {
        ws.simulateMessage({
          type: 'approval',
          data: { id, instance: 'inst-a', slot: ACTIVE, source: 'agent', tool: 'first_tool', ts: 50 },
        })
      })
      globalStore.dispatch(addNotification(note('50', 'inst-a')))

      // The reconnect read is answered from a listing taken before request B.
      let release: (value: ApprovalSnapshot) => void = () => {}
      ;(api.approvals as ReturnType<typeof vi.fn>).mockReturnValueOnce(
        new Promise<ApprovalSnapshot>((resolve) => { release = resolve }))
      ;(api.notifications as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        notifications: [note('50', 'inst-a')], unread: 1,
      })
      vi.useFakeTimers()
      act(() => { ws.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      vi.useRealTimers()
      const reconnected = WS_INSTANCES[1]
      await act(async () => {
        reconnected.simulateOpen()
        await Promise.resolve()
      })
      // Request B reuses A's id while that read is in flight.
      act(() => {
        reconnected.simulateMessage({
          type: 'approval',
          data: { id, instance: 'inst-b', slot: ACTIVE, source: 'agent', tool: 'second_tool', ts: 51 },
        })
      })
      globalStore.dispatch(addNotification(note('51', 'inst-b')))
      await act(async () => {
        release([])
        await Promise.resolve()
        await Promise.resolve()
      })

      const rows = () => testStore.getState().notifications.items.filter(n => n.approval_id === id).map(n => n.ts)
      expect(rows()).toEqual(['51'])
      expect(testStore.getState().notifications.retiredApprovals?.['51']).toBeUndefined()
      // B's provenance survived, so its own decision still removes its row.
      act(() => {
        reconnected.simulateMessage({ type: 'approval_resolved', data: { id, approved: true, instance: 'inst-b' } })
      })
      expect(rows()).toEqual([])
    } finally {
      vi.useRealTimers()
      for (const ts of ['50', '51']) globalStore.dispatch(deleteNotification.fulfilled(ts, '', ts))
    }
  })

  it('retires a coordinator row omitted by the reconnect snapshot and clears its provenance', async () => {
    ;(api.approvals as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })

    const notification = {
      kind: 'approval', title: 'Gap approval', body: '', ts: '12', approval_id: 'ap-gap', approval_instance: 'inst-ap-gap',
    } as Parameters<typeof addNotification>[0]
    try {
      act(() => {
        ws.simulateMessage({
          type: 'approval',
          data: { id: 'ap-gap', instance: 'inst-ap-gap', slot: ACTIVE, source: 'agent', tool: 'gap_tool', ts: 12 },
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
          type: 'approval_resolved', data: { id: 'ap-gap', origin: 'coordinator', instance: 'inst-ap-gap', approved: true },
        })
      })
      expect(chat().messages.find(m => m.meta?.approval_id === 'ap-gap')?.meta?.resolved).toBeUndefined()
    } finally {
      vi.useRealTimers()
      globalStore.dispatch(deleteNotification.fulfilled('12', '', '12'))
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
      kind: 'approval', title: 'Stale approval', body: '', ts: '14', approval_id: 'ap-resolved-during-sync', approval_instance: 'inst-ap-resolved-during-sync',
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
        data: { id: 'ap-resolved-during-sync', instance: 'inst-ap-resolved-during-sync', slot: ACTIVE, source: 'agent', tool: 'stale_tool', ts: 14 },
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
          data: { id: 'ap-resolved-during-sync', origin: 'coordinator', instance: 'inst-ap-resolved-during-sync', slot: ACTIVE, approved: true },
        })
        globalStore.dispatch(deleteNotification.fulfilled('14', '', '14'))
      })
      expect(testStore.getState().notifications.items.some(n => n.approval_id === 'ap-resolved-during-sync')).toBe(false)

      await act(async () => {
        release([
          { id: 'ap-resolved-during-sync', instance: 'inst-ap-resolved-during-sync', slot: ACTIVE, source: 'agent', tool: 'stale_tool', ts: 14 },
          { id: 'ap-still-pending', instance: 'inst-ap-still-pending', slot: ACTIVE, source: 'agent', tool: 'live_tool', ts: 15 },
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
      globalStore.dispatch(deleteNotification.fulfilled('14', '', '14'))
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
      kind: 'approval', title: 'Injected approval', body: '', ts: '16', approval_id: 'ap-injected-during-sync', approval_instance: 'inst-ap-injected-during-sync',
    } as Parameters<typeof addNotification>[0]
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-injected-during-sync', instance: 'inst-ap-injected-during-sync', slot: ACTIVE, source: 'agent', tool: 'injected_tool', ts: 16 },
      })
      globalStore.dispatch(addNotification(notification))
      ws.simulateMessage({
        type: 'approval_resolved',
        data: { id: 'ap-injected-during-sync', origin: 'coordinator', instance: 'inst-ap-injected-during-sync', slot: ACTIVE, approved: false },
      })
      globalStore.dispatch(deleteNotification.fulfilled('16', '', '16'))
    })

    try {
      await act(async () => {
        release([{ id: 'ap-injected-during-sync', instance: 'inst-ap-injected-during-sync', slot: ACTIVE, source: 'agent', tool: 'injected_tool', ts: 16 }])
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
      globalStore.dispatch(deleteNotification.fulfilled('16', '', '16'))
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
        data: { id: 'ap-before', instance: 'inst-ap-before', slot: ACTIVE, source: 'agent', tool: 'before_tool', ts: 14 },
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
        data: { id: 'ap-during', instance: 'inst-ap-during', slot: ACTIVE, source: 'agent', tool: 'during_tool', ts: 15 },
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
        data: { id: 'ap-decided', instance: 'inst-ap-decided', slot: ACTIVE, source: 'agent', tool: 'decided_tool', ts: 18 },
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
        data: { id: 'ap-decided', origin: 'coordinator', instance: 'inst-ap-decided', slot: ACTIVE, approved: true },
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
    // ChatInput's Allow/Reject settles its row through resolveApprovalRow
    // directly — no frame, no retireApproval, so the retired-id log never
    // sees it and provenance stays intact. A reconcile
    // snapshot that no longer lists the id then retires the row as 'stale'
    // through the very same loop; the local decision must survive it.
    let release: (value: unknown) => void = () => {}
    const pending = new Promise((resolve) => { release = resolve })
    ;(api.approvals as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-local', instance: 'inst-ap-local', slot: ACTIVE, source: 'agent', tool: 'local_tool', ts: 19 },
      })
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(api.approvals).toHaveBeenCalledTimes(1)

    act(() => {
      testStore.dispatch(resolveApprovalRow({
        target: { origin: 'coordinator', id: 'ap-local', slot: ACTIVE, instance: 'inst-ap-local' }, decision: 'approved',
      }))
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
        data: { id: 'coordinator-only', instance: 'inst-coordinator-only', slot: ACTIVE, source: 'agent', tool: 'coordinator_tool', ts: 17 },
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

})
