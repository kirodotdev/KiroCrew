/**
 * One frame of stream traffic lands as ONE `stream/flush` action, however many
 * sessions stream (hooks/websocket/streamBuffers.ts, store/streamFlush.ts).
 *
 * The flush also writes the `streaming` / `thinking` status a chunk implies, so
 * a chunk costs no status dispatch of its own. That write is deferred to the
 * frame, while `chat_status` and `tool_call` write their status at arrival, so
 * these specs also pin that a status newer than the slot's last chunk survives
 * the flush: the final status is the one the arrival order implies.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearSlotState } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockReturnValue(new Promise(() => {})),  // never resolves
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
  },
}))

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()

  constructor() {
    WS_INSTANCES.push(this)
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

const OPEN = 'slot-open'
const BG = ['slot-bg-1', 'slot-bg-2', 'slot-bg-3']
const row = (key: string): ChatSlot => ({ key, title: key, messages: 0, running: true, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })

const chunk = (slot: string, content: string, seq: number) => ({ type: 'chat_chunk', data: { slot, content, seq } })
const thought = (slot: string, content: string) => ({ type: 'chat_thinking', data: { slot, content } })
const chatStatus = (slot: string, status: string) => ({ type: 'chat_status', data: { slot, status } })
const toolCall = (slot: string, id: string) => ({
  type: 'tool_call',
  data: { slot, tool: 'Running make test', kind: 'execute', purpose: 'Run tests', input_preview: 'make test', tool_call_id: id, is_shell: true },
})
const status = (slot: string) => globalStore.getState().chat.slotStatusDetail[slot]

describe('useWebSocket lands each frame as one stream flush', () => {
  let queryClient: QueryClient
  let rafQueue: FrameRequestCallback[]

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    rafQueue = []
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // Frames are hand-run by runFrames(), so a burst can be observed before
    // and after its flush.
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafQueue.push(cb); return rafQueue.length })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => { rafQueue[id - 1] = () => {} })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(null))
    globalStore.dispatch(sseSlots([]))
  })

  /** Deliberately the SINGLETON store: the hook reads the status it records
   *  off the imported store, so a separate Provider store would diverge. The
   *  dispatch spy goes on before the mount, which captures `dispatch`. */
  function mount() {
    const spy = vi.spyOn(globalStore, 'dispatch')
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, {
        store: globalStore,
        children: createElement(QueryClientProvider, { client: queryClient }, children),
      })
    }
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(OPEN))
    globalStore.dispatch(sseSlots([row(OPEN), ...BG.map(row)]))
    spy.mockClear()
    return { ws, spy }
  }

  const runFrames = () => {
    act(() => {
      while (rafQueue.length) rafQueue.splice(0).forEach(cb => cb(performance.now()))
    })
  }

  const dispatchedTypes = (spy: { mock: { calls: unknown[][] } }) =>
    spy.mock.calls.map(([a]) => (a as { type?: string })?.type)

  it('lands chunks, reasoning, sub-agent text and recency for every slot in one dispatch', () => {
    const { ws, spy } = mount()
    act(() => {
      for (const [i, slot] of [OPEN, ...BG].entries()) {
        ws.simulateMessage(thought(slot, `plan ${i} `))
        ws.simulateMessage(chunk(slot, `text ${i}`, 1))
        ws.simulateMessage(chunk(slot, ' more', 2))
      }
      ws.simulateMessage({ type: 'subagent_chunk', data: { slot: BG[0], id: 'agent-1', text: 'sub text' } })
      ws.simulateMessage({ type: 'steer_push', data: { slot: BG[1], content: 'steer', ts: '2026-08-10T10:00:00Z' } })
    })
    // The steer row flushed the chat buffer ahead of itself; the frame lands the rest.
    spy.mockClear()
    act(() => { ws.simulateMessage(chunk(BG[2], ' tail', 3)) })
    expect(spy).not.toHaveBeenCalled()
    runFrames()
    expect(dispatchedTypes(spy)).toEqual(['stream/flush'])
    const payload = (spy.mock.calls[0][0] as { payload: { slots: { slot: string }[]; subagents: unknown[]; recency: { key: string }[] } }).payload
    expect(payload.slots.map(s => s.slot)).toEqual([BG[2]])
    expect(payload.subagents).toHaveLength(1)
    expect(payload.recency.map(r => r.key)).toEqual([BG[1]])
    for (const slot of [OPEN, ...BG]) expect(status(slot)?.kind).toBe('streaming')
    expect(globalStore.getState().chat.messages.find(m => m.role === 'streaming')?.content).toBe('text 0 more')
  })

  it('writes no status dispatch for a chunk: the flush carries the transition', () => {
    const { ws, spy } = mount()
    act(() => { for (const slot of BG) ws.simulateMessage(chunk(slot, 'x', 1)) })
    runFrames()
    expect(dispatchedTypes(spy)).toEqual(['stream/flush'])
    for (const slot of BG) expect(status(slot)?.kind).toBe('streaming')
  })

  it('keeps a chat_status label that arrived after the chunk in the same frame', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage(chunk(BG[0], 'step', 1))
      ws.simulateMessage(chatStatus(BG[0], 'Working step 5'))
    })
    runFrames()
    expect(status(BG[0])).toMatchObject({ kind: 'thinking', label: 'Working step 5' })
  })

  it('buffers a chat_status label into the frame and lets a later chunk move past it', () => {
    const { ws, spy } = mount()
    act(() => {
      ws.simulateMessage(chatStatus(BG[0], 'Working step 5'))
      ws.simulateMessage(chunk(BG[0], 'step', 1))
      ws.simulateMessage(chatStatus(BG[1], 'Working step 6'))
    })
    expect(spy).not.toHaveBeenCalled()
    runFrames()
    expect(dispatchedTypes(spy)).toEqual(['stream/flush'])
    expect(status(BG[0])?.kind).toBe('streaming')
    expect(status(BG[1])).toMatchObject({ kind: 'thinking', label: 'Working step 6' })
  })

  it('does not merge a tool refinement into a line a buffered label replaced', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage(toolCall(BG[0], 'call-1')) })
    act(() => {
      ws.simulateMessage(chatStatus(BG[0], 'Compacting'))
      ws.simulateMessage({ type: 'tool_call', data: { slot: BG[0], tool: 'make test', kind: 'execute', tool_call_id: 'call-1', is_update: true } })
    })
    runFrames()
    // The refinement's own fields only: the purpose of the replaced line is gone.
    expect(status(BG[0])).toMatchObject({ kind: 'tool', toolName: 'make test', purpose: '' })
  })

  it('keeps a tool refinement equal to the stored line over reasoning buffered before it', () => {
    const { ws } = mount()
    const refine = { type: 'tool_call', data: { slot: BG[0], tool: 'Running make test', kind: 'execute', input_preview: 'make test', tool_call_id: 'call-1', is_update: true, is_shell: true } }
    act(() => { ws.simulateMessage({ ...refine, data: { ...refine.data, is_update: false } }) })
    const toolLine = status(BG[0])
    act(() => {
      ws.simulateMessage(thought(BG[0], 'hmm'))
      // Same fields as the stored line, so the store keeps its identity.
      ws.simulateMessage(refine)
    })
    expect(status(BG[0])).toBe(toolLine)
    runFrames()
    expect(status(BG[0])?.kind).toBe('tool')
  })

  it('keeps a tool status that arrived after the chunk in the same frame', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage(chunk(BG[0], 'about to test', 1))
      ws.simulateMessage(toolCall(BG[0], 'call-1'))
    })
    runFrames()
    expect(status(BG[0])?.kind).toBe('tool')
  })

  it('moves to streaming when a chunk follows the newer status in the same frame', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage(chunk(BG[0], 'a', 1))
      ws.simulateMessage(toolCall(BG[0], 'call-1'))
      ws.simulateMessage(chunk(BG[0], 'b', 2))
    })
    runFrames()
    expect(status(BG[0])?.kind).toBe('streaming')
  })

  it('lets visible output outrank reasoning in either arrival order', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage(thought(BG[0], 'hmm'))
      ws.simulateMessage(chunk(BG[0], 'answer', 1))
      ws.simulateMessage(chunk(BG[1], 'answer', 1))
      ws.simulateMessage(thought(BG[1], 'hmm'))
    })
    runFrames()
    expect(status(BG[0])?.kind).toBe('streaming')
    expect(status(BG[1])?.kind).toBe('streaming')
  })

  it('moves a tool status to thinking when reasoning follows it', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage(toolCall(BG[0], 'call-1')) })
    act(() => { ws.simulateMessage(thought(BG[0], 'next')) })
    runFrames()
    expect(status(BG[0])?.kind).toBe('thinking')
  })

  it('a refinement that follows its call\'s final result does not put the finished call back', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage(toolCall(OPEN, 'call-1')) })
    act(() => { ws.simulateMessage({ type: 'tool_result', data: { slot: OPEN, output: 'ok', tool_call_id: 'call-1' } }) })
    runFrames()
    expect(status(OPEN)?.kind).toBe('thinking')
    act(() => {
      ws.simulateMessage({ type: 'tool_call', data: { slot: OPEN, tool: 'make test', kind: 'execute', input_preview: 'make test', tool_call_id: 'call-1', is_update: true, is_shell: true } })
    })
    runFrames()
    expect(status(OPEN)?.kind).toBe('thinking')
  })

  it('a refinement while its call still runs keeps updating the line', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage(toolCall(OPEN, 'call-1')) })
    act(() => { ws.simulateMessage({ type: 'tool_result', data: { slot: OPEN, output: 'partial', tool_call_id: 'call-1', final: false } }) })
    act(() => {
      ws.simulateMessage({ type: 'tool_call', data: { slot: OPEN, tool: 'make test', kind: 'execute', input_preview: 'make test', tool_call_id: 'call-1', is_update: true, is_shell: true } })
    })
    runFrames()
    expect(status(OPEN)).toMatchObject({ kind: 'tool', toolCallId: 'call-1' })
  })
})
