/**
 * `chat_thinking` must not re-dispatch its status detail on every thought frame.
 *
 * The guard tested `slotStatusDetail[slot]?.kind !== 'streaming'` and then wrote
 * `kind: 'thinking'` — which is itself `!== 'streaming'`. So it never
 * self-limited: every reasoning frame dispatched `setSlotStatusDetail` again
 * with a fresh `ts`. That reducer replaces `slotStatusDetail[slot]` wholesale,
 * so the map identity changed per frame and every whole-map subscriber
 * (ChatSidebar, CommandPalette) re-rendered for the entire duration of the
 * model's reasoning — a ~2,600-line sidebar, per frame, to redraw the same
 * "Thinking…" string.
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
    // whole-map subscriber re-render. This is the assertion that fails on the
    // pre-fix code (each frame replaced the detail with a fresh `ts`).
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

    // ...and the next thought frame must restore it. The idempotence fix must
    // not degrade into "only ever dispatch once".
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

    // Pre-existing behaviour, pinned so the fix cannot regress it: visible
    // output outranks reasoning in the status line.
    expect(store.getState().chat.slotStatusDetail['slot-1']).toBe(streamingDetail)
  })
})
