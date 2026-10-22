/**
 * steer_push frames carry the sender record onto the live row, so the
 * "From <chat>" label agrees with what a reload would show.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearSlotState } from '../store/chatSlice'
import { readSendProvenance, SEND_PROVENANCE_META_KEY } from '../pages/chat/sendProvenance'

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

/** Deliberately the SINGLETON store: useWebSocket dispatches via useAppDispatch()
 *  but reads state off the imported singleton, so a separate Provider store would
 *  let reads and writes diverge. */
function seedStore() {
  globalStore.dispatch(clearSlotState())
  globalStore.dispatch(setActiveSlot('slot-1'))
  return globalStore
}


const ENV = '[sent by session chat-1-1792410000 via session_send]\n\n'
const SENDER = { from: 'chat-1-1792410000', title: 'Release conductor', via: 'session_send', mode: 'steered' }

const steerRow = () => globalStore.getState().chat.messages.find(m => m.role === 'user' && m.meta?.steer)!

describe('useWebSocket: steer_push carries who wrote the steer', () => {
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

  function mount() {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store: globalStore },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return ws
  }

  it("a peer's steer keeps its sender, so the live row is labelled", () => {
    const ws = mount()
    seedStore()
    act(() => { ws.simulateMessage({ type: 'steer_push', data: { slot: 'slot-1', content: `${ENV}stop`, mid: 'm-1', meta: { [SEND_PROVENANCE_META_KEY]: SENDER } } }) })
    const row = steerRow()
    expect(readSendProvenance(row.content, row.meta)?.provenance.mode).toBe('steered')
  })
})
