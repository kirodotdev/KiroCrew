import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, cleanup } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { store as globalStore } from '../store'
import { api } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'
import { MC_NOTIFICATION_EVENT, type McNotificationDetail } from '../hooks/notificationEvent'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: true, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: true, monitors: [] }),
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
  },
}))

const sockets: MockWebSocket[] = []
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
  constructor() { sockets.push(this) }
  open() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }
  frame(type: string, data: unknown) {
    act(() => this.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type, data }) })))
  }
}

const SLOT = 'chat-sound'
const LOOP = { id: 'loop', slot_key: SLOT, message: 'continue', idle_secs: 300, max_cycles: 20, cycle_count: 1, active: true, last_fire_ts: 0 }
const QUESTIONS = [{ question: 'Which approach?', options: [{ label: 'Use A' }] }]

describe('conversation attention sounds', () => {
  let testStore: ReturnType<typeof createTestStore>
  let queryClient: QueryClient
  let kinds: (string | undefined)[]
  const onSound = (event: Event) => kinds.push((event as CustomEvent<McNotificationDetail>).detail.kind)

  beforeEach(() => {
    vi.clearAllMocks()
    sockets.length = 0
    kinds = []
    testStore = createTestStore()
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.spyOn(globalStore, 'getState').mockImplementation(testStore.getState)
    vi.spyOn(globalStore, 'subscribe').mockImplementation(testStore.subscribe)
    vi.stubGlobal('WebSocket', MockWebSocket)
    window.addEventListener(MC_NOTIFICATION_EVENT, onSound)
  })

  afterEach(async () => {
    await act(async () => cleanup())
    queryClient.clear()
    window.removeEventListener(MC_NOTIFICATION_EVENT, onSound)
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  async function connect() {
    const wrapper = ({ children }: { children: React.ReactNode }) => createElement(
      Provider, { store: testStore },
      createElement(QueryClientProvider, { client: queryClient }, children),
    )
    renderHook(() => useWebSocket(), { wrapper })
    await act(async () => sockets[0].open())
    return sockets[0]
  }

  it('chimes once at the final parent reply, not at intermediate turns', async () => {
    const ws = await connect()
    ws.frame('chat_done', { slot: SLOT, continuing: true })
    ws.frame('chat_done', { slot: SLOT, continuing: true })
    expect(kinds).toEqual([])
    ws.frame('chat_done', { slot: SLOT, continuing: false })
    expect(kinds).toEqual(['turn'])
  })

  it('the fresh frame overrides stale running child and plan snapshots', async () => {
    const ws = await connect()
    ws.frame('slots', [{ key: SLOT, messages: 1, running: true, subagents_running: true, orchestrating: true }])
    ws.frame('subagent_spawn', { slot: SLOT, id: 'child', task: 'work', agent: 'worker' })
    ws.frame('workflow_run_event', { run_id: 'stale-workflow', session_key: `dashboard:${SLOT}`, type: 'run_started' })
    ws.frame('chat_done', { slot: SLOT, continuing: false })
    expect(kinds).toEqual(['turn'])
  })

  it.each(['subagents_running', 'orchestrating'])('honors %s on older frames without an activity hint', async field => {
    const ws = await connect()
    ws.frame('slots', [{ key: SLOT, messages: 1, running: false, [field]: true }])
    ws.frame('chat_done', { slot: SLOT })
    expect(kinds).toEqual([])
  })

  it('honors queued turns on older frames', async () => {
    const ws = await connect()
    ws.frame('slots', [{ key: SLOT, messages: 1, running: false, queue_depth: 1 }])
    ws.frame('chat_done', { slot: SLOT })
    expect(kinds).toEqual([])
  })

  it('honors live child events even before a slot snapshot arrives', async () => {
    const ws = await connect()
    ws.frame('subagent_spawn', { slot: SLOT, id: 'child', task: 'work', agent: 'worker' })
    ws.frame('chat_done', { slot: SLOT })
    expect(kinds).toEqual([])
  })

  it('honors accepted but queued children', async () => {
    const ws = await connect()
    ws.frame('subagent_queued', { slot: SLOT, queued: 2 })
    ws.frame('chat_done', { slot: SLOT })
    expect(kinds).toEqual([])
  })

  it('keeps monitor cycles silent and chimes on the final reply after stop', async () => {
    const ws = await connect()
    ws.frame('autonudge_state', { event: 'added', slot: SLOT, loop: LOOP })
    ws.frame('chat_done', { slot: SLOT })
    expect(kinds).toEqual([])
    ws.frame('autonudge_state', { event: 'updated', slot: SLOT, loop: { ...LOOP, active: false } })
    ws.frame('chat_done', { slot: SLOT, continuing: false })
    expect(kinds).toEqual(['turn'])
  })

  it('waits for a workflow and its parent synthesis, without chiming for each phase', async () => {
    const ws = await connect()
    ws.frame('workflow_run_event', { run_id: 'workflow', session_key: `dashboard:${SLOT}`, type: 'run_started', data: { name: 'review' } })
    ws.frame('chat_done', { slot: SLOT })
    ws.frame('workflow_run_event', { run_id: 'workflow', type: 'phase_started', data: { title: 'verify' } })
    expect(kinds).toEqual([])
    ws.frame('workflow_run_event', { run_id: 'workflow', type: 'run_finished' })
    expect(kinds).toEqual([])
    ws.frame('chat_done', { slot: SLOT, continuing: false })
    expect(kinds).toEqual(['turn'])
  })

  it('also matches a workflow through the linked session key', async () => {
    const ws = await connect()
    ws.frame('slots', [{ key: SLOT, messages: 1, running: false, linked_session_key: 'slack:123.456' }])
    ws.frame('workflow_run_event', { run_id: 'workflow', session_key: 'slack:123.456', type: 'run_started' })
    ws.frame('chat_done', { slot: SLOT })
    expect(kinds).toEqual([])
  })

  it('unrelated sessions and UI-launched workflows do not silence a finished chat', async () => {
    const ws = await connect()
    ws.frame('subagent_spawn', { slot: 'other', id: 'child', task: 'work', agent: 'worker' })
    ws.frame('autonudge_state', { event: 'added', slot: 'other', loop: { ...LOOP, slot_key: 'other' } })
    ws.frame('workflow_run_event', { run_id: 'other-workflow', session_key: 'dashboard:other', type: 'run_started' })
    ws.frame('workflow_run_event', { run_id: 'ui-workflow', type: 'run_started' })
    ws.frame('chat_done', { slot: SLOT, continuing: false })
    expect(kinds).toEqual(['turn'])
  })

  it('alerts immediately for a question and not again when its turn ends', async () => {
    const ws = await connect()
    ws.frame('question_card', { slot: SLOT, card_id: 'ask-1', questions: QUESTIONS })
    expect(kinds).toEqual(['approval'])
    ws.frame('chat_done', { slot: SLOT, continuing: false, needs_input: true })
    expect(kinds).toEqual(['approval'])
  })

  it('a blocking question is audible while other work remains', async () => {
    const ws = await connect()
    ws.frame('autonudge_state', { event: 'added', slot: SLOT, loop: LOOP })
    ws.frame('question_card', { slot: SLOT, ask_id: 'blocking-ask', questions: QUESTIONS })
    expect(kinds).toEqual(['approval'])
  })

  it('deduplicates a repeated question frame but not a genuinely new question', async () => {
    const ws = await connect()
    const card = { slot: SLOT, card_id: 'ask-1', questions: QUESTIONS }
    ws.frame('question_card', card)
    ws.frame('question_card', card)
    ws.frame('question_card', { ...card, card_id: 'ask-2' })
    expect(kinds).toEqual(['approval', 'approval'])
  })

  it('chimes for the final reply after the question is answered', async () => {
    const ws = await connect()
    ws.frame('question_card', { slot: SLOT, card_id: 'ask-1', questions: QUESTIONS })
    ws.frame('chat_done', { slot: SLOT, continuing: false, needs_input: true })
    ws.frame('question_card_resolved', { slot: SLOT, card_id: 'ask-1' })
    ws.frame('chat_done', { slot: SLOT, continuing: false, needs_input: false })
    expect(kinds).toEqual(['approval', 'turn'])
  })

  it('an authoritative input request is audible even if its card frame was not seen', async () => {
    const ws = await connect()
    ws.frame('chat_done', { slot: SLOT, continuing: true, needs_input: true })
    expect(kinds).toEqual(['turn'])
  })

  it('preserves approval and unrelated notification sounds', async () => {
    const ws = await connect()
    ws.frame('chat_done', { slot: SLOT, continuing: true })
    ws.frame('approval', { id: 'approval-1', tool: 'shell', source: 'agent', slot: SLOT })
    ws.frame('notification', { kind: 'cron', ts: '1', title: 'Reminder' })
    ws.frame('notification', { kind: 'agent', ts: '2', title: 'Passive', priority: 'passive' })
    expect(kinds).toEqual(['approval', 'cron'])
  })

  it('keeps completion and question replay silent until reconnect catch-up settles', async () => {
    const ws = await connect()
    let finishSync!: () => void
    vi.mocked(api.chatSlots).mockImplementationOnce(() => new Promise(resolve => {
      finishSync = () => resolve([])
    }))
    act(() => ws.open())
    ws.frame('chat_done', { slot: SLOT, continuing: false })
    ws.frame('question_card', { slot: SLOT, card_id: 'replayed', questions: QUESTIONS })
    expect(kinds).toEqual([])
    await act(async () => finishSync())
    ws.frame('question_card_resolved', { slot: SLOT, card_id: 'replayed' })
    ws.frame('chat_done', { slot: SLOT, continuing: false })
    expect(kinds).toEqual(['turn'])
  })

  it('rejects slot-less and invalid question frames', async () => {
    const ws = await connect()
    ws.frame('chat_done', {})
    ws.frame('chat_done', { slot: '' })
    ws.frame('question_card', { slot: '__proto__', card_id: 'bad', questions: QUESTIONS })
    expect(kinds).toEqual([])
  })
})
