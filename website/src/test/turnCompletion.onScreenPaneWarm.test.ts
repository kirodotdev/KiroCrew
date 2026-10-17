/** A turn that ends in a pane the user is watching but that is not the Redux
 * active slot -- the Crew Members DM thread -- must reconcile that pane to the
 * server transcript. Neither the off-screen warm nor `refreshSlot` (active
 * slot only) reaches it, so a reply row that never reduced live stayed missing
 * until a page reload.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { api } from '../api/client'
import { setActiveSlot, clearSlotState } from '../store/chatSlice'
import { setViewedThreadSlot, _resetViewedThreadForTests } from '../lib/viewedThread'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockReturnValue(new Promise(() => {})),
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
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateMessage(data: object) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) })) }
}

const MEMBER = 'member-probe'
const ACTIVE = 'chat-elsewhere'
const detailCalls = (slot: string) =>
  (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.filter(c => c[0] === slot).length

describe('turn end in an on-screen pane that is not the active slot', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('requestAnimationFrame', () => 1)
    vi.stubGlobal('cancelAnimationFrame', () => {})
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    _resetViewedThreadForTests()
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(null))
    queryClient.clear()
  })

  function mount() {
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
    globalStore.dispatch(setActiveSlot(ACTIVE))
    vi.clearAllMocks()
    return ws
  }

  it('re-reads the watched member thread when its turn ends', async () => {
    const ws = mount()
    setViewedThreadSlot(MEMBER)
    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: MEMBER } }) })
    await act(async () => { await Promise.resolve() })
    expect(detailCalls(MEMBER)).toBe(1)
  })

  it('does not re-read the active slot through the warm path', async () => {
    const ws = mount()
    setViewedThreadSlot(MEMBER)
    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
    await act(async () => { await Promise.resolve() })
    // `refreshSlot` owns the active slot; the warm declines it.
    expect(detailCalls(ACTIVE)).toBeLessThanOrEqual(1)
  })
})
