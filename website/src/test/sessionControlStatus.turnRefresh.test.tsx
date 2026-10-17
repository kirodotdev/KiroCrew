/**
 * A finished turn re-asks the app-contributed control statuses of the session
 * it ran in (#10909). Before this, a chip was re-asked only when its popover
 * closed, so a status the agent changed during a turn stayed stale on screen.
 *
 * Driven through a real `chat_done` frame on the socket facade. The client
 * mirrors production's `staleTime: Infinity`, so nothing but an invalidation
 * can make a probe run again.
 */
import { renderHook, waitFor, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { setActiveSlot } from '../store/chatSlice'
import { store as globalStore } from '../store'
import { useWebSocket } from '../hooks/useWebSocket'
import {
  dashboardSessionKey,
  useSessionControlStatuses,
  type ResolvedSessionControl,
} from '../hooks/useSessionControls'
import { api } from '../api/client'

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark', colorTheme: 'default', themeVersion: 0 }) }))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    appSessionStatus: vi.fn(),
  },
}))

const mockStatus = vi.mocked(api.appSessionStatus)

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

const control: ResolvedSessionControl = {
  key: 'test-app:scope',
  appName: 'test-app',
  appDisplayName: 'Test App',
  appVersion: '0.1.0',
  id: 'scope',
  entryPoint: 'dist/session-control.mjs',
  label: 'Scope',
  icon: 'Tag',
  allowedApi: [],
  allowedEvents: [],
  statusPath: 'session-status',
  processBacked: false,
}

describe('session control status after a finished turn', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    mockStatus.mockReset()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    // The composer shows the active chat, so chat-1 is the one on screen.
    // Turn completion reads the active slot from the app's global store.
    globalStore.dispatch(setActiveSlot('chat-1'))
    qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => { qc.clear(); vi.unstubAllGlobals(); globalStore.dispatch(setActiveSlot(null)) })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  /** The composer's chip state for `chat-1`, beside a live socket. */
  async function mountChat1() {
    mockStatus.mockResolvedValueOnce({ state: 'ok', tooltip: 'free' })
    const { result } = renderHook(() => {
      useWebSocket()
      return useSessionControlStatuses([control], 'dashboard:chat-1').statuses
    }, { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    await waitFor(() => expect(result.current['test-app:scope']?.state).toBe('ok'))
    expect(mockStatus).toHaveBeenCalledTimes(1)
    return { result, ws }
  }

  it("re-asks the status when that session's turn finishes", async () => {
    const { result, ws } = await mountChat1()

    // The agent claimed the resource during the turn; the app now reports warn.
    mockStatus.mockResolvedValueOnce({ state: 'warn', tooltip: 'claimed' })
    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'chat-1' } }) })

    await waitFor(() => expect(result.current['test-app:scope']?.state).toBe('warn'))
    expect(mockStatus).toHaveBeenCalledTimes(2)
  })

  it.each(['chat-2', 'chat-10'])("leaves the status alone when %s's turn finishes", async (other) => {
    const { result, ws } = await mountChat1()
    mockStatus.mockResolvedValue({ state: 'warn', tooltip: 'claimed' })

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: other } }) })
    // Barrier: chat-1's own frame, sent after the foreign one, must refetch.
    // Seeing exactly that one refetch proves the foreign frame was processed
    // and asked nothing (a wrongly matched refetch would make it two).
    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'chat-1' } }) })

    await waitFor(() => expect(result.current['test-app:scope']?.state).toBe('warn'))
    expect(mockStatus).toHaveBeenCalledTimes(2)
  })

  it('marks a background chat\'s status stale without asking now', async () => {
    const { result, ws } = await mountChat1()
    act(() => { globalStore.dispatch(setActiveSlot('chat-2')) })
    mockStatus.mockResolvedValue({ state: 'warn', tooltip: 'claimed' })

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'chat-1' } }) })

    // Synchronous: invalidateQueries marks the cache before it returns.
    const probe = qc.getQueryCache().findAll({ queryKey: ['session-control-status', 'dashboard:chat-1'] })
    expect(probe).toHaveLength(1)
    expect(probe[0].state.isInvalidated).toBe(true)
    expect(probe[0].state.fetchStatus).toBe('idle')
    expect(mockStatus).toHaveBeenCalledTimes(1)
    expect(result.current['test-app:scope']?.state).toBe('ok')
  })
})

describe('dashboardSessionKey', () => {
  it('prefixes the slot the way session-scoped state is stored', () => {
    expect(dashboardSessionKey('chat-1')).toBe('dashboard:chat-1')
  })

  it('is empty for no slot, so no probe and no refresh is keyed on it', () => {
    expect(dashboardSessionKey(null)).toBe('')
    expect(dashboardSessionKey('')).toBe('')
  })
})
