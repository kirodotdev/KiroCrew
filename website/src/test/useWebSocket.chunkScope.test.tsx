/**
 * The tab's half of `chunk_scope.1`: what it sends as its watch set, and what the
 * coarse frames for a slot it does not watch (`chat_phase`, `tool_status`) and the
 * `chunk_watch_ack` catch-up do to the store. The frame-router setup repeats
 * UseWebSocketCoverage.test.tsx's (keep the copies identical); the fakes are in
 * UseWebSocketCoverageTestUtils.tsx.
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
import chatReducer, { setActiveSlot, clearMessages, setSlotStatusDetail } from '../store/chatSlice'
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

describe('useWebSocket chunk scope', () => {
  let testStore: ReturnType<typeof createTestStore>
  let rafCbs: FrameRequestCallback[]

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    MockAudio.instances.length = 0
    rafCbs = []
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE },
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafCbs.push(cb); return rafCbs.length })
    // Read paths resolve against the singleton store, not the Provider store.
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
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
    act(() => { ws.simulateOpen() })
    return { ...hook, ws }
  }

  /** Run the frames a background slot's dispatches were batched into. */
  function flushFrames() {
    act(() => {
      const cbs = rafCbs.splice(0)
      for (const cb of cbs) cb(performance.now())
    })
  }

  const status = (slot: string) => testStore.getState().chat.slotStatusDetail[slot]
  const watchFrames = (ws: MockWebSocket) => ws.send.mock.calls
    .map(([raw]) => JSON.parse(raw as string))
    .filter((f: { type: string }) => f.type === 'chunk_watch')
  const detailReads = (slot: string) => vi.mocked(api.chatSlotDetail).mock.calls.filter(c => c[0] === slot).length

  it('declares the capability and sends its watch set on open', () => {
    const { ws } = mount()
    expect(ws.url).toContain('chunk_scope.1')
    expect(watchFrames(ws).at(-1)).toEqual({ type: 'chunk_watch', slots: [ACTIVE] })
  })

  it('keeps the active slot watched while the tab is hidden', () => {
    const { ws } = mount()
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    try {
      act(() => { document.dispatchEvent(new Event('visibilitychange')) })
      // The set did not change, so nothing new went out.
      expect(watchFrames(ws)).toEqual([{ type: 'chunk_watch', slots: [ACTIVE] }])
    } finally {
      hidden.mockRestore()
    }
  })

  it('an ack re-reads the active slot and warms a background slot', async () => {
    const { ws } = mount()
    const activeBefore = detailReads(ACTIVE)
    await act(async () => {
      ws.simulateMessage({ type: 'chunk_watch_ack', data: { slots: [ACTIVE, BACKGROUND], catchup: [ACTIVE, BACKGROUND] } })
      await Promise.resolve()
    })
    expect(detailReads(ACTIVE)).toBe(activeBefore + 1)
    expect(detailReads(BACKGROUND)).toBe(1)
  })

  it('a reconnect re-reads a live slot again on the ack, which follows the watch registration', async () => {
    vi.useFakeTimers()
    try {
      mount()
      act(() => { WS_INSTANCES[0].onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1000) })
      const ws = WS_INSTANCES[1]
      const before = detailReads(ACTIVE)
      act(() => { ws.simulateOpen() })
      const afterReconnect = detailReads(ACTIVE)
      expect(afterReconnect).toBe(before + 1)
      await act(async () => {
        ws.simulateMessage({ type: 'chunk_watch_ack', data: { slots: [ACTIVE], catchup: [ACTIVE] } })
        await Promise.resolve()
      })
      // The reconnect read can predate the gateway registering the watch set,
      // and chunks in between were held back: only the ack's read covers them.
      expect(detailReads(ACTIVE)).toBe(afterReconnect + 1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('chat_phase moves the line to streaming, and thinking does not override it', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'chat_phase', data: { slot: BACKGROUND, phase: 'streaming' } }) })
    flushFrames()
    expect(status(BACKGROUND)?.kind).toBe('streaming')
    // The handler reads the current line from the singleton store, which is
    // the Provider store in production; mirror the line there.
    act(() => { globalStore.dispatch(setSlotStatusDetail({ slot: BACKGROUND, kind: 'streaming' })) })
    act(() => { ws.simulateMessage({ type: 'chat_phase', data: { slot: BACKGROUND, phase: 'thinking' } }) })
    flushFrames()
    expect(status(BACKGROUND)?.kind).toBe('streaming')
  })

  it('tool_status sets the line and seeds the tool log without the shrunk input', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'tool_status', data: { slot: BACKGROUND, tool: 'Shell', kind: 'execute', purpose: 'List files', tool_call_id: 't1', input_preview: '{"command":"ls"}' } })
    })
    flushFrames()
    expect(status(BACKGROUND)).toMatchObject({ kind: 'tool', purpose: 'List files', toolCallId: 't1' })
    const entry = testStore.getState().chat.slotActivity[BACKGROUND]?.toolLog.find(e => e.tool_call_id === 't1')
    expect(entry?.purpose).toBe('List files')
    expect(entry?.input ?? '').toBe('')
  })

  it('tool_status with returned moves that call off the line to thinking', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'tool_status', data: { slot: BACKGROUND, tool: 'Shell', tool_call_id: 't1' } }) })
    act(() => { ws.simulateMessage({ type: 'tool_status', data: { slot: BACKGROUND, tool_call_id: 't1', returned: true } }) })
    flushFrames()
    expect(status(BACKGROUND)).toEqual({ kind: 'thinking' })
  })

  it('a return for an earlier call leaves a later parallel call on the line', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'tool_status', data: { slot: BACKGROUND, tool: 'Shell', tool_call_id: 't1' } })
      ws.simulateMessage({ type: 'tool_status', data: { slot: BACKGROUND, tool: 'Read', tool_call_id: 't2' } })
      ws.simulateMessage({ type: 'tool_status', data: { slot: BACKGROUND, tool_call_id: 't1', returned: true } })
    })
    flushFrames()
    expect(status(BACKGROUND)).toMatchObject({ kind: 'tool', toolCallId: 't2' })
  })

  it('clearing the active pane keeps another slot\'s status line', () => {
    mount()
    act(() => {
      testStore.dispatch(setSlotStatusDetail({ slot: BACKGROUND, kind: 'streaming' }))
      testStore.dispatch(setSlotStatusDetail({ slot: ACTIVE, kind: 'thinking' }))
      testStore.dispatch({ type: 'chat/clearSlotState' })
    })
    expect(status(BACKGROUND)?.kind).toBe('streaming')
    expect(status(ACTIVE)).toBeUndefined()
  })
})
