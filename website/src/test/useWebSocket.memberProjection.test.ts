/**
 * Verifies the two member event-log frames feed the projection store:
 *   - `member_projection` applies one (slug, key, value, seq) via the store's
 *     higher-seq-wins rule, and a malformed frame (missing slug/key/seq) is a
 *     no-op.
 *   - `members_subscribed` truncates held rows whose seq ran ahead of the
 *     server's authoritative lastSeq (a torn tail after a restart).
 *
 * The fields ride at the TOP LEVEL of the frame (not under `data`), per
 * MemberProjectionFrame / MembersSubscribedFrame.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { memberProjectionStore } from '../state/memberProjectionStore'

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

describe('useWebSocket member projection frames', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    memberProjectionStore.clear()
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

  function open() {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return ws
  }

  it('applies a member_projection frame to the store', () => {
    const ws = open()
    act(() => {
      ws.simulateMessage({ type: 'member_projection', data: { slug: 'oncall', key: 'roster', value: { model: 'x' }, seq: 3 } })
    })
    expect(memberProjectionStore.get('oncall', 'roster')).toEqual({ model: 'x' })
  })

  it('drops a member_projection frame missing slug/key/seq', () => {
    const ws = open()
    act(() => {
      ws.simulateMessage({ type: 'member_projection', data: { key: 'roster', value: { model: 'x' }, seq: 3 } })
      ws.simulateMessage({ type: 'member_projection', data: { slug: 'oncall', value: { model: 'x' }, seq: 3 } })
      ws.simulateMessage({ type: 'member_projection', data: { slug: 'oncall', key: 'roster', value: { model: 'x' } } })
    })
    expect(memberProjectionStore.has('oncall')).toBe(false)
  })

  it('members_subscribed truncates a torn tail past the server lastSeq', () => {
    const ws = open()
    act(() => {
      ws.simulateMessage({ type: 'member_projection', data: { slug: 'oncall', key: 'roster', value: { model: 'ahead' }, seq: 9 } })
      ws.simulateMessage({ type: 'members_subscribed', data: { lastSeqs: { oncall: 5 } } })
    })
    expect(memberProjectionStore.get('oncall', 'roster')).toBeUndefined()
  })
})
