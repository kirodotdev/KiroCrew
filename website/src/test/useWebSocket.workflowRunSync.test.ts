/**
 * The delivery half of the workflow-status fix, in `useWebSocket.ts`.
 *
 * `workflow_run_event` is a one-shot broadcast with no replay, so the chat's
 * workflow row is only as correct as the frames this tab happened to be awake
 * for. These tests pin the three moments that consult the authority instead —
 * first connect (seed), reconnect (catch up), and a slow heal for a frame lost
 * while the socket stayed open — plus the rule that an authority which cannot be
 * read changes nothing.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { sseWorkflowEvent } from '../store/chatSlice'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
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
}

const mockWorkflowRuns = api.workflowRuns as unknown as ReturnType<typeof vi.fn>

describe('useWebSocket workflow run reconcile', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    mockWorkflowRuns.mockResolvedValue({ runs: [] })
  })

  afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  const workflowRuns = () => testStore.getState().chat.workflowRuns

  /** A live run in the store, put there the only way the app can: WS frames. */
  const liveRun = (run_id = 'wf_000025') => {
    testStore.dispatch(sseWorkflowEvent({ run_id, session_key: 'dashboard:chat-1', type: 'run_started', data: { name: 'perf audit' } }))
  }

  async function connect() {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    await act(async () => { ws.simulateOpen() })
    return ws
  }

  /** Record every action type dispatched from here on. */
  function spyDispatch(): string[] {
    const seen: string[] = []
    const real = testStore.dispatch.bind(testStore)
    vi.spyOn(testStore, 'dispatch').mockImplementation(((a: unknown) => {
      if (a && typeof a === 'object' && 'type' in a) seen.push(String((a as { type: unknown }).type))
      return real(a as Parameters<typeof real>[0])
    }) as typeof testStore.dispatch)
    return seen
  }

  const reconciles = (seen: string[]) => seen.filter(t => t === 'chat/reconcileWorkflowRuns').length

  it('seeds an in-flight run on first connect', async () => {
    // A reload mid-run: the stream carries nothing about a run already started,
    // so without this read the bar stays empty until the next phase event.
    mockWorkflowRuns.mockResolvedValue({
      runs: [{ run_id: 'wf_000031', name: 'Nightly audit', status: 'running', session_key: 'dashboard:chat-1' }],
    })

    await connect()

    expect(mockWorkflowRuns).toHaveBeenCalled()
    expect(workflowRuns()['wf_000031']).toMatchObject({ status: 'running', name: 'Nightly audit' })
  })

  it('catches up a run that ended while the socket was down', async () => {
    await connect()
    liveRun()
    expect(workflowRuns()['wf_000025'].status).toBe('running')

    mockWorkflowRuns.mockResolvedValue({ runs: [{ run_id: 'wf_000025', status: 'finished' }] })
    act(() => { WS_INSTANCES[0].onclose?.(new CloseEvent('close')) })
    const reconnected = WS_INSTANCES[WS_INSTANCES.length - 1]
    await act(async () => { reconnected.simulateOpen() })

    expect(workflowRuns()['wf_000025'].status).toBe('finished')
  })

  it('changes nothing when the authority cannot be read', async () => {
    // A 503 (workflows service unavailable) or a transport failure is not
    // evidence that no runs exist — clearing a spinner off it would be a guess.
    mockWorkflowRuns.mockRejectedValue(new Error('503'))
    await connect()
    liveRun()
    mockWorkflowRuns.mockRejectedValue(new Error('503'))
    const dispatched = spyDispatch()
    act(() => { WS_INSTANCES[0].onclose?.(new CloseEvent('close')) })
    await act(async () => { WS_INSTANCES[WS_INSTANCES.length - 1].simulateOpen() })

    expect(reconciles(dispatched)).toBe(0)
    expect(workflowRuns()['wf_000025'].status).toBe('running')
  })

  it('changes nothing when the response carries no run list', async () => {
    // Asserted on the DISPATCH, not just the resulting state: a 503 body has no
    // `runs`, and reconciling an absent list must not even be attempted.
    mockWorkflowRuns.mockResolvedValue({ error: 'workflows not available' })
    await connect()
    liveRun()
    const dispatched = spyDispatch()
    act(() => { WS_INSTANCES[0].onclose?.(new CloseEvent('close')) })
    await act(async () => { WS_INSTANCES[WS_INSTANCES.length - 1].simulateOpen() })

    expect(reconciles(dispatched)).toBe(0)
    expect(workflowRuns()['wf_000025'].status).toBe('running')
  })

  it('heals a row still showing as running without any reconnect', async () => {
    // A frame can be lost while the socket stays open, and then no other path
    // would ever correct the row: the spinner is driven purely by stored status.
    vi.useFakeTimers()
    renderHook(() => useWebSocket(), { wrapper })
    await act(async () => { WS_INSTANCES[0].simulateOpen() })
    liveRun()

    mockWorkflowRuns.mockResolvedValue({ runs: [{ run_id: 'wf_000025', status: 'finished' }] })
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })

    expect(workflowRuns()['wf_000025'].status).toBe('finished')
  })

  it('makes no request while nothing is running', async () => {
    // The backstop must be free when idle, or every open tab pays for it forever.
    vi.useFakeTimers()
    renderHook(() => useWebSocket(), { wrapper })
    await act(async () => { WS_INSTANCES[0].simulateOpen() })
    mockWorkflowRuns.mockClear()

    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })

    expect(mockWorkflowRuns).not.toHaveBeenCalled()
  })

  it('shares one request when two heals land together', async () => {
    // The visibility heal and the interval tick can fire in the same moment, and
    // two concurrent reads of the same authority are pure waste. They go through
    // one query key, so the later ones join the first's in-flight request.
    vi.useFakeTimers()
    let release = () => {}
    const gate = new Promise(resolve => {
      release = () => resolve({ runs: [{ run_id: 'wf_000025', status: 'finished' }] })
    })
    renderHook(() => useWebSocket(), { wrapper })
    await act(async () => { WS_INSTANCES[0].simulateOpen() })
    // Inside act so the running row's re-render lands and the heal effect has
    // ARMED its visibility listener before the events below are dispatched.
    await act(async () => { liveRun() })
    mockWorkflowRuns.mockClear()
    mockWorkflowRuns.mockReturnValue(gate)

    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
      document.dispatchEvent(new Event('visibilitychange'))
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(mockWorkflowRuns).toHaveBeenCalledTimes(1)

    // ...and the shared answer still lands.
    await act(async () => { release(); await vi.advanceTimersByTimeAsync(50) })
    expect(workflowRuns()['wf_000025'].status).toBe('finished')
  })

  it('waits for the tab to be looked at, then heals immediately', async () => {
    // A hidden tab's timers are throttled and its rows are not on screen, so the
    // tick skips it; regaining visibility is the moment the row matters again.
    vi.useFakeTimers()
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    renderHook(() => useWebSocket(), { wrapper })
    await act(async () => { WS_INSTANCES[0].simulateOpen() })
    liveRun()
    mockWorkflowRuns.mockClear()
    mockWorkflowRuns.mockResolvedValue({ runs: [{ run_id: 'wf_000025', status: 'finished' }] })

    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(mockWorkflowRuns).not.toHaveBeenCalled()
    expect(workflowRuns()['wf_000025'].status).toBe('running')

    hidden.mockReturnValue(false)
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })

    expect(workflowRuns()['wf_000025'].status).toBe('finished')
  })
})
