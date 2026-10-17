/**
 * Behavioural coverage for the multiplexed dashboard socket: the connection
 * lifecycle (open, subscribe, reconnect, catch-up) and the slots reconcile.
 * The frame router is covered in UseWebSocketCoverage.test.tsx and its
 * siblings; the fakes are in UseWebSocketCoverageTestUtils.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import { store as globalStore } from '../store'
import chatReducer, { PANE_HYDRATE_LIMIT, setActiveSlot, sseChatMessage, setQuestionCard, resolveQuestionCard, sseAutomation } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import { addNotification, removeNotificationByTs } from '../store/notificationsSlice'
import type { ChatSlot } from '../types'
import { ACTIVE, BACKGROUND, WS_INSTANCES, MockWebSocket } from './UseWebSocketCoverageTestUtils'

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
      expect(api.chatSlotDetail).toHaveBeenCalledWith(ACTIVE, PANE_HYDRATE_LIMIT)
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
