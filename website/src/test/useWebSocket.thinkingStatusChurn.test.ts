/**
 * `chat_thinking` must not re-dispatch its status detail on every thought frame.
 *
 * `setSlotStatusDetail` replaces `slotStatusDetail[slot]` wholesale, so a
 * dispatch per reasoning frame changes the map identity every frame and every
 * whole-map subscriber (ChatSidebar, CommandPalette) re-renders for the entire
 * duration of the model's reasoning — a ~2,600-line sidebar, per frame, to
 * redraw the same "Thinking…" string. The guard must therefore stay idempotent
 * while `kind` remains 'thinking'.
 *
 * The sibling `chat_chunk` guard writes `kind: 'streaming'` and is therefore
 * naturally idempotent. These tests pin that same property onto the thinking
 * path, and pin the transitions that must STILL fire.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearSlotState, sseChatMessage } from '../store/chatSlice'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
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

function storeOnSlot1() {
  // Deliberately the SINGLETON store, not a fresh createTestStore(). useWebSocket
  // dispatches through useAppDispatch() (the Provider store) but reads the guard's
  // current state off the imported singleton (`hooks/useWebSocket.ts:5`). In
  // production those are the same object; a separate Provider store would make
  // reads and writes diverge, so the guard would never observe its own write and
  // these tests would pass against the buggy code too.
  globalStore.dispatch(clearSlotState())
  globalStore.dispatch(setActiveSlot('slot-1'))
  globalStore.dispatch(sseChatMessage({ slot: 'slot-1', role: 'user', content: 'explain this' }))
  return globalStore
}

describe('useWebSocket chat_thinking status-detail churn', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(null))
  })

  function mount(store: ReturnType<typeof storeOnSlot1>) {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { hook, ws }
  }

  const thinking = (content: string) => ({
    type: 'chat_thinking',
    data: { slot: 'slot-1', content },
  })

  it('keeps slotStatusDetail[slot] reference-stable across many thought frames', () => {
    const store = storeOnSlot1()
    const { ws } = mount(store)

    act(() => { ws.simulateMessage(thinking('Let me ')) })
    const afterFirst = store.getState().chat.slotStatusDetail['slot-1']
    expect(afterFirst).toBeDefined()
    expect(afterFirst.kind).toBe('thinking')

    // 25 more frames, as one reasoning block streams in.
    act(() => {
      for (let i = 0; i < 25; i++) ws.simulateMessage(thinking(`token${i} `))
    })

    // The SAME object — no re-dispatch, so no new map identity, so no
    // whole-map subscriber re-render. A non-idempotent guard would replace the
    // detail with a fresh `ts` on each frame.
    expect(store.getState().chat.slotStatusDetail['slot-1']).toBe(afterFirst)
  })

  it('keeps the slotStatusDetail map itself reference-stable across frames', () => {
    const store = storeOnSlot1()
    const { ws } = mount(store)

    act(() => { ws.simulateMessage(thinking('first ')) })
    const mapAfterFirst = store.getState().chat.slotStatusDetail

    act(() => {
      for (let i = 0; i < 10; i++) ws.simulateMessage(thinking('more '))
    })

    expect(store.getState().chat.slotStatusDetail).toBe(mapAfterFirst)
  })

  it('still accumulates the reasoning text on every frame', () => {
    const store = storeOnSlot1()
    const { ws } = mount(store)

    act(() => {
      ws.simulateMessage(thinking('alpha '))
      ws.simulateMessage(thinking('beta '))
      ws.simulateMessage(thinking('gamma'))
    })

    const think = store.getState().chat.messages.find(m => m.role === 'thinking')
    expect(think).toBeDefined()
    // Suppressing the redundant STATUS dispatch must not suppress the content.
    expect(think!.content).toBe('alpha beta gamma')
  })

  it('still sets the detail on a genuine transition back into thinking', () => {
    const store = storeOnSlot1()
    const { ws } = mount(store)

    act(() => { ws.simulateMessage(thinking('thinking first ')) })
    expect(store.getState().chat.slotStatusDetail['slot-1'].kind).toBe('thinking')

    // A tool call moves the detail off 'thinking'...
    act(() => {
      ws.simulateMessage({
        type: 'tool_call',
        data: { slot: 'slot-1', tool: 'fs_read', kind: 'tool', purpose: 'reading a file', input_preview: '' },
      })
    })
    expect(store.getState().chat.slotStatusDetail['slot-1'].kind).toBe('tool')
    const afterTool = store.getState().chat.slotStatusDetail['slot-1']

    // ...and the next thought frame must restore it. Idempotence must not
    // degrade into "only ever dispatch once".
    act(() => { ws.simulateMessage(thinking('back to thinking')) })
    const restored = store.getState().chat.slotStatusDetail['slot-1']
    expect(restored.kind).toBe('thinking')
    expect(restored).not.toBe(afterTool)
  })

  it('does not overwrite a streaming detail with thinking', () => {
    const store = storeOnSlot1()
    const { ws } = mount(store)

    act(() => { ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'slot-1', content: 'answer', seq: 1 } }) })
    expect(store.getState().chat.slotStatusDetail['slot-1'].kind).toBe('streaming')
    const streamingDetail = store.getState().chat.slotStatusDetail['slot-1']

    act(() => { ws.simulateMessage(thinking('late thought')) })

    // Visible output outranks reasoning in the status line.
    expect(store.getState().chat.slotStatusDetail['slot-1']).toBe(streamingDetail)
  })
})
