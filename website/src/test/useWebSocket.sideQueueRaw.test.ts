/**
 * `chat.side_queue` frames cannot vouch for unredacted text.
 *
 * `raw` marks queue-card content the LOCAL client typed, and the reducer treats it as a
 * one-way ratchet: a scrubbed broadcast edit may not overwrite raw content. Broadcast
 * payloads are redacted by `ws.py` before they leave the server, so a frame claiming `raw`
 * could only ever vouch for scrubbed text — the dispatch adapter therefore strips the field
 * instead of relying on it being absent from the wire contract.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { TAB_ID } from '../api/tabId'

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

  constructor() { WS_INSTANCES.push(this) }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

const SLOT = 'chat-1'

describe('useWebSocket chat.side_queue raw claim', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => { vi.unstubAllGlobals() })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  function connect() {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return ws
  }

  it('a frame claiming raw cannot protect scrubbed text from a later edit', () => {
    const ws = connect()

    // A push arriving from the wire, dishonestly claiming to carry raw text.
    act(() => {
      ws.simulateMessage({
        type: 'chat.side_queue',
        data: { slot: SLOT, action: 'push', queue_id: 'q1', content: 'from [REDACTED: credential]', raw: true },
      })
    })

    // If the claim had been honored the entry would be ratcheted and this edit ignored.
    act(() => {
      ws.simulateMessage({
        type: 'chat.side_queue',
        data: { slot: SLOT, action: 'edit', queue_id: 'q1', content: 'corrected by the server' },
      })
    })

    const entry = testStore.getState().chat.slotSide?.[SLOT]?.queue?.[0]
    expect(entry?.content).toBe('corrected by the server')
    expect(entry?.raw).toBeUndefined()
  })

  it('a cancel echoed from ANOTHER tab drops the card without releasing the question', () => {
    const ws = connect()

    act(() => {
      ws.simulateMessage({
        type: 'chat.side_queue',
        data: { slot: SLOT, action: 'push', queue_id: 'q9', content: 'someone else queued this' },
      })
    })
    act(() => {
      ws.simulateMessage({
        type: 'chat.side_queue',
        data: {
          slot: SLOT, action: 'cancel', queue_id: 'q9', content: 'someone else queued this',
          origin_client: 'a-different-tab',
        },
      })
    })

    const side = testStore.getState().chat.slotSide?.[SLOT]
    expect(side?.queue?.length ?? 0).toBe(0)
    expect(side?.releasedText).toBeUndefined()
  })

  it('a cancel echoed back to THIS tab still releases the question', () => {
    const ws = connect()

    act(() => {
      ws.simulateMessage({
        type: 'chat.side_queue',
        data: { slot: SLOT, action: 'push', queue_id: 'q10', content: 'my own question' },
      })
    })
    act(() => {
      ws.simulateMessage({
        type: 'chat.side_queue',
        data: {
          slot: SLOT, action: 'cancel', queue_id: 'q10', content: 'my own question',
          origin_client: TAB_ID,
        },
      })
    })

    expect(testStore.getState().chat.slotSide?.[SLOT]?.releasedText).toContain('my own question')
  })
})
