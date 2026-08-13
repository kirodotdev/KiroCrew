/**
 * `activity_event` with `kind: "session"` -> refetch of the model list.
 *
 * A session's `session/new` response is the ONLY place the backend learns what
 * models this account is entitled to run, and `/api/models` narrows its catalog
 * to that set. A cold gateway therefore answers the FIRST model fetch from the
 * unnarrowed catalog — and because that is a live 200, the self-heal poll stops,
 * so without this the picker would keep offering models no turn can use for the
 * rest of the page's life.
 *
 * It is deliberately event-driven rather than "mark the cold response degraded":
 * degraded means poll every 8s, and `/api/models` spawns
 * `kiro chat --list-models`, so an idle dashboard with no session would spawn a
 * subprocess every 8 seconds indefinitely.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'

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

describe('useWebSocket activity_event → model list refetch', () => {
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

  function send(data: object) {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => { ws.simulateMessage({ type: 'activity_event', data }) })
  }

  const keysOf = (spy: { mock: { calls: unknown[][] } }) =>
    spy.mock.calls.map(c => JSON.stringify((c[0] as { queryKey?: unknown })?.queryKey))

  it('refetches the model list when a session is created', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    send({ slot: 's1', kind: 'session', spawned: true, text: 'Session created · kirocrew · auto' })
    expect(keysOf(spy)).toContain(JSON.stringify(['available-models']))
  })

  it('refetches on a resumed session too', () => {
    // Resume also re-runs session/new, so the advertised set is re-learned.
    const spy = vi.spyOn(qc, 'invalidateQueries')
    send({ slot: 's1', kind: 'session', spawned: true, text: 'Session resumed · kirocrew · auto' })
    expect(keysOf(spy)).toContain(JSON.stringify(['available-models']))
  })

  it('does NOT refetch on a warm turn that spawned nothing', () => {
    // The session frame is emitted on every turn, not only at a spawn. Nothing
    // was respawned here, so the advertised list cannot have changed — and
    // /api/models spawns `kiro chat --list-models`, so refetching would run a
    // subprocess per prompt.
    const spy = vi.spyOn(qc, 'invalidateQueries')
    send({ slot: 's1', kind: 'session', spawned: false, text: 'Session created · kirocrew · auto' })
    expect(keysOf(spy)).not.toContain(JSON.stringify(['available-models']))
  })

  it('does NOT refetch when the flag is absent', () => {
    // Fail closed: an emitter that omits the flag must not reintroduce the
    // per-prompt subprocess.
    const spy = vi.spyOn(qc, 'invalidateQueries')
    send({ slot: 's1', kind: 'session', text: 'Session created · kirocrew · auto' })
    expect(keysOf(spy)).not.toContain(JSON.stringify(['available-models']))
  })

  it('does NOT refetch on ordinary status activity', () => {
    // Status lines arrive many times per turn ("Thinking…", tool titles). Each
    // one triggering a refetch would spawn `kiro chat --list-models` repeatedly.
    const spy = vi.spyOn(qc, 'invalidateQueries')
    send({ slot: 's1', kind: 'status', text: 'Thinking…' })
    expect(keysOf(spy)).not.toContain(JSON.stringify(['available-models']))
  })

  it('still records the activity item in the store', () => {
    // The refetch is additive — it must not swallow the event's normal routing.
    // A non-active slot lands in slotActivity[slot].toolLog.
    send({ slot: 's1', kind: 'session', text: 'Session created · kirocrew · auto' })
    const state = testStore.getState() as {
      chat: { slotActivity: Record<string, { toolLog: { type: string; text: string }[] }>; toolLog: { type: string; text: string }[] }
    }
    const log = state.chat.slotActivity?.s1?.toolLog ?? state.chat.toolLog
    expect(log.some(e => e.type === 'session' && e.text.includes('Session created'))).toBe(true)
  })
})
