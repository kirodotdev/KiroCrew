import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { store as appStore } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { appendSlotMessage, selectSlotMessages, setActiveSlot, setQuestionCard, sseChatMessage, sseSubagentPending } from '../store/chatSlice'
import dashboardReducer, { updateSlot } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'
import type { ChatMessage } from '../types'
import * as transport from '../chat-core/transport/sendTurn'

// The real pane/composer/serializer/reducers; only network and unrelated rich
// content are mocked. A deferred receipt lets each test observe the send itself.
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn(),
    sendChat: vi.fn(),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn(),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
    stopChat: vi.fn(),
    interruptSlot: vi.fn(),
    resolveApproval: vi.fn().mockResolvedValue({ ok: true }),
    answerQuestion: vi.fn().mockResolvedValue({ ok: true }),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {},
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../pages/chat/SubagentProgressBar', () => ({ default: () => <div data-testid="subagent-progress-probe" /> }))

import ChatPane from '../components/ChatPane'
import CrewComposer from '../pages/members/CrewComposer'
import { api } from '../api/client'

const SLOT = 'member-radar'
const FIXED_TIME = new Date('2026-10-14T07:00:00Z')
let settleSend: (response: Response) => void
const response = (body: object, status = 200) => ({ ok: status < 400, status, json: async () => body }) as Response

function renderCrewmate(opts: { running?: boolean; delegated?: boolean; ordinary?: boolean; stopping?: boolean; messages?: ChatMessage[]; width?: number } = {}) {
  const { running = true, delegated = false, ordinary = false, stopping = false, messages = [], width = 1024 } = opts
  vi.stubGlobal('innerWidth', width)
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: query.includes('max-width') && width < 768,
    media: query, onchange: null, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  })))
  vi.mocked(api.chatSlotDetail).mockResolvedValue({ messages, running, has_more: false, total: messages.length } as never)
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: messages.length, running, stopping, stop_state: stopping ? 'soft_pending' : 'idle', subagents_running: delegated, mode: 'member', pending_approval: false, waiting_for_input: false }],
        slotsLoaded: true, unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  store.dispatch(setActiveSlot('front'))
  vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <Provider store={store}><QueryClientProvider client={qc}><ThemeProvider><MemoryRouter>
      <ChatPane slotKey={SLOT} crewmate={ordinary ? undefined : { name: 'Radar' }} composerInput={ordinary ? undefined : CrewComposer} />
    </MemoryRouter></ThemeProvider></QueryClientProvider></Provider>,
  )
  return { ...view, store }
}

const userRows = (store: ReturnType<typeof renderCrewmate>['store']) =>
  selectSlotMessages(store.getState() as RootState, SLOT).filter(m => m.role === 'user')

beforeEach(() => {
  vi.clearAllMocks()
  vi.setSystemTime(FIXED_TIME)
  __resetPaneDraftsForTests()
  vi.mocked(api.chatSlotDetail).mockReset()
  vi.mocked(api.uploadFiles).mockReset().mockResolvedValue({ paths: ['/uploads/report.pdf'] } as never)
  vi.mocked(api.sendChat).mockReset().mockImplementation(() => new Promise<Response>(resolve => { settleSend = resolve }))
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

async function ready() {
  const box = await screen.findByPlaceholderText(/Message (Radar|Test)/)
  await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalled())
  return box
}
async function send(text: string, click = false) {
  const box = await ready()
  fireEvent.change(box, { target: { value: text } })
  if (click) fireEvent.click(screen.getByRole('button', { name: 'Send', exact: true }))
  else fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
  await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
  return box
}
async function settle(body: object = { ok: true }, status = 200) {
  await act(async () => { settleSend(response(body, status)) })
}

describe('mate composer placeholder', () => {
  it('names the mate without the sigil hint, while a plain chat keeps it', async () => {
    renderCrewmate({ running: false })
    const box = await ready()
    expect(box).toHaveAttribute('placeholder', 'Message Radar…')
    cleanup()
    renderCrewmate({ running: false, ordinary: true })
    const plain = await ready()
    expect(plain).toHaveAttribute('placeholder', expect.stringMatching(/^Message Test… \(\/command · @file · \$skill/))
  })
})

describe('mate responsive Send', () => {
  it.each([
    { running: false, width: 320 },
    { running: true, width: 390 },
    { running: false, delegated: true, width: 320 },
    { running: true, stopping: true, width: 390 },
  ])('keeps an ordinary Send while $running/$delegated/$stopping at $width px', async opts => {
    const { store, container } = renderCrewmate(opts)
    const empty = await ready()
    // A live stream is what makes the composer busy (the slot's `running`
    // flag alone does not), so start one for the running cases.
    if (opts.running) act(() => { store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Working', seq: 1 })) })
    if (opts.running || opts.delegated) {
      // A busy crewmate with nothing typed keeps main's Stop control.
      expect(await screen.findByTestId(opts.stopping ? 'stop-button-pulsing' : 'stop-button-armed')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Send', exact: true })).toBeNull()
    } else {
      expect(screen.getByRole('button', { name: 'Send', exact: true })).toBeDisabled()
      expect(screen.queryByRole('button', { name: /stop|interrupt/i })).toBeNull()
    }
    fireEvent.change(empty, { target: { value: 'typing' } })
    if (!opts.stopping) {
      // Once the person types, the control is the plain Send, never steer.
      const button = screen.getByRole('button', { name: 'Send', exact: true })
      expect(button.querySelector('.lucide-arrow-up')).not.toBeNull()
      expect(screen.queryByRole('button', { name: /stop|interrupt|steer/i })).toBeNull()
    }
    expect(screen.queryByTestId('busy-send-split')).toBeNull()
    fireEvent.change(empty, { target: { value: '' } })
    if (opts.stopping) return
    const box = await send('also check the logs', true)
    expect(userRows(store)).toHaveLength(1)
    expect(userRows(store)[0].meta).toMatchObject({ optimistic: true, responsiveSend: true })
    expect(screen.getAllByText('also check the logs')).toHaveLength(1)
    expect(box).toHaveValue('')
    expect(vi.mocked(api.sendChat).mock.calls[0][5]).toBeUndefined()
    expect(vi.mocked(api.sendChat).mock.calls[0][6]).toBe(true)
    expect(api.stopChat).not.toHaveBeenCalled()
    expect(api.interruptSlot).not.toHaveBeenCalled()
    // DOM contract only: happy-dom cannot measure pixel overflow.
    expect(container.querySelector('[data-testid="composer-sizer"]')).not.toBeNull()
    expect(screen.getByTestId('composer-dock-root')).toHaveClass('absolute', 'left-0', 'bottom-0')
    await settle()
    expect(userRows(store)[0].meta?.optimistic).toBeUndefined()
  })

  it.each(['ordinary', 'steer'])('the %s echo settles the same bubble, even before a lost receipt', async echo => {
    const { store } = renderCrewmate()
    const box = await send('check once')
    const sendId = vi.mocked(api.sendChat).mock.calls[0][4]!.sendId
    act(() => {
      const meta = { sendId, mid: 'mid-fixed', ...(echo === 'steer' ? { steer: true, steerState: 'consumed' } : {}) }
      if (echo === 'steer') store.dispatch(appendSlotMessage({ slot: SLOT, message: { role: 'user', content: 'check once', cls: 'msg msg-u', ts: '2026-10-14T07:00:01Z', meta } }))
      else store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'check once', ts: '2026-10-14T07:00:01Z', meta }))
    })
    expect(userRows(store)).toHaveLength(1)
    expect(userRows(store)[0].meta?.optimistic).toBeUndefined()
    expect(screen.queryByText(/Steered|Steering|runs as its own/)).toBeNull()
    await settle({ ok: true, steered: echo === 'steer' })
    expect(box).toHaveValue('')
    expect(userRows(store)).toHaveLength(1)
  })

  it('separates streaming text around the send without sending an interrupt', async () => {
    const { store } = renderCrewmate()
    await ready()
    act(() => { store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Before', seq: 1 })) })
    await send('new request')
    act(() => { store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'After', seq: 2 })) })
    expect(selectSlotMessages(store.getState() as RootState, SLOT).map(m => [m.role, m.content])).toEqual([
      ['assistant', 'Before'], ['user', 'new request'], ['streaming', 'After'],
    ])
    await settle()
  })

  it('restores attachments and the typed draft on a definite refusal', async () => {
    const { store, container } = renderCrewmate()
    const box = await ready()
    const upload = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(upload, { target: { files: [new File(['report'], 'report.pdf', { type: 'application/pdf' })] } })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())
    await screen.findByText('report.pdf')
    await send('read this')
    expect(vi.mocked(api.sendChat).mock.calls[0][0]).toContain('[attached_file 1] /uploads/report.pdf')
    expect(vi.mocked(api.sendChat).mock.calls[0][4]).toMatchObject({ files: ['/uploads/report.pdf'] })
    await settle({ ok: false, error: 'request refused' }, 409)
    await waitFor(() => expect(box).toHaveValue('read this'))
    expect(userRows(store)).toHaveLength(0)
    expect(screen.getByText('report.pdf')).toBeInTheDocument()
    expect(screen.getByText(/request refused/)).toBeInTheDocument()
  })

  it.each(['receipt first', 'push first'])('a real queued receipt keeps the one bubble in place (%s), with no queue card or interrupt', async (order) => {
    const { appendQueuedMessage } = await import('../store/chatSlice')
    const { store } = renderCrewmate()
    await send('queued safely')
    const sendId = vi.mocked(api.sendChat).mock.calls[0][4]!.sendId as string
    const bubble = screen.getByText('queued safely')
    const push = () => act(() => {
      store.dispatch(appendQueuedMessage({ slot: SLOT, content: 'queued safely', ts: '2026-10-14T07:00:02Z', queue_id: 'queue-fixed', meta: { sendId } }))
    })
    if (order === 'push first') push()
    expect(screen.getByText('queued safely')).toBe(bubble)
    expect(screen.queryByTestId('queue-card')).toBeNull()
    await settle({ ok: true, queued: true, queue_id: 'queue-fixed' })
    // Between the receipt and the push the bubble used to be spliced out and
    // redrawn from the queue entry: a visible flash at the bottom.
    expect(screen.getByText('queued safely')).toBe(bubble)
    if (order === 'receipt first') push()
    expect(screen.getAllByText('queued safely')).toHaveLength(1)
    expect(screen.getByText('queued safely')).toBe(bubble)
    expect(userRows(store)).toHaveLength(0)
    expect(screen.queryByTestId('queue-card')).toBeNull()
    expect(screen.queryByRole('button', { name: /interrupt/i })).toBeNull()
  })

  it('an uncertain receipt keeps its bubble with delivery-pending text, without restoring a duplicate', async () => {
    const { store } = renderCrewmate()
    await ready()
    vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'response-late', body: {} })
    const box = await screen.findByPlaceholderText(/Message (Radar|Test)/)
    fireEvent.change(box, { target: { value: 'keep pending' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(userRows(store)[0]?.meta?.deliveryUnconfirmed).toBe(true))
    expect(box).toHaveValue('')
    expect(screen.getByText(/Delivery pending/)).toBeInTheDocument()
  })

  it('keeps pending questions while hiding engineering rows and historical steer badges', async () => {
    const messages: ChatMessage[] = [
      { role: 'user', content: 'earlier request', cls: '', meta: { steer: true, steerState: 'requeued' } },
      { role: 'assistant', content: 'visible reply', cls: '' },
      { role: 'notice', content: 'runtime notice', cls: '' },
      { role: 'tool', content: '🔧 hidden shell command', cls: '' },
      { role: 'subagent', content: '[Subagent completion event]\nWorker complete', cls: '' },
      { role: 'system', content: 'hidden stop detail', cls: '', kind: 'stop_event', meta: { kind: 'stop_event' } },
      { role: 'error', content: 'actionable failure', cls: '' },
      { role: 'permission', content: 'approve required action', cls: '', meta: { approval_id: 'approval-fixed', tool: 'shell', description: 'approve required action' } },
    ]
    const { store } = renderCrewmate({ messages })
    await screen.findByText('visible reply')
    act(() => { store.dispatch(setQuestionCard({ slot: SLOT, ask_id: 'ask-fixed', questions: [{ question: 'Which report?', options: [{ label: 'Summary' }, { label: 'Full' }] }] })) })
    expect(await screen.findByText('Which report?')).toBeInTheDocument()
    expect(screen.getByText(/actionable failure/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeInTheDocument()
    await send('also check this')
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeInTheDocument()
    await settle()
    expect(screen.queryByText(/runtime notice|hidden shell|hidden stop|Worker complete|runs as its own/)).toBeNull()
    expect(screen.queryByTestId('subagent-progress-probe')).toBeNull()
    expect(screen.getByText('earlier request')).toBeInTheDocument()
    expect(selectSlotMessages(store.getState() as RootState, SLOT).some(m => m.role === 'permission' && !m.meta?.resolved)).toBe(true)
    expect(selectSlotMessages(store.getState() as RootState, SLOT).some(m => m.kind === 'stop_event')).toBe(true)
  })

  it('keeps IME and Shift+Enter local, then sends normally on a busy DM', async () => {
    renderCrewmate({ width: 320 })
    const box = await ready()
    fireEvent.change(box, { target: { value: '你好' } })
    fireEvent.compositionStart(box)
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter', isComposing: true })
    expect(api.sendChat).not.toHaveBeenCalled()
    fireEvent.compositionEnd(box)
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter', shiftKey: true })
    expect(api.sendChat).not.toHaveBeenCalled()
    // A fresh keydown after the composition commit uses the normal Send path.
    fireEvent.keyUp(box, { key: 'Enter', code: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: 'Send', exact: true }))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.sendChat).mock.calls[0][6]).toBe(true)
    await settle()
  })

  it('retains actionable spawn approvals even though routine subagent progress is hidden', async () => {
    const { store } = renderCrewmate({ width: 320 })
    await ready()
    act(() => { store.dispatch(sseSubagentPending({ slot: SLOT, id: 'worker-fixed', task: 'Review report', approval_id: 'spawn-fixed' })) })
    const card = await screen.findByTestId('spawn-approval-card')
    expect(card).toBeInTheDocument()
    expect(screen.queryByTestId('subagent-progress-probe')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Approve', exact: true }))
    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledWith('spawn-fixed', 'approve'))
  })

  it('an ordinary Sessions pane retains its engineering affordances and sends no responsive flag', async () => {
    const { store } = renderCrewmate({ ordinary: true, messages: [{ role: 'notice', content: 'runtime notice', cls: '' }] })
    await screen.findByText('runtime notice')
    act(() => { store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'working', seq: 1 })) })
    expect(screen.getByTestId('subagent-progress-probe')).toBeInTheDocument()
    await send('normal steer')
    expect(vi.mocked(api.sendChat).mock.calls[0][5]).toBe(true)
    expect(vi.mocked(api.sendChat).mock.calls[0]).toHaveLength(6)
    await settle({ ok: true, steered: true })
  })
})


describe('responsive question and queue repairs', () => {
  it.each([
    { running: true, native: true, steer: true },
    { running: false, native: true, steer: false },
    { running: false, native: false, steer: false },
  ])('answers a card through the correct route: %j', async ({ running, native, steer }) => {
    const { store } = renderCrewmate({ running })
    await ready()
    act(() => { store.dispatch(setQuestionCard({ slot: SLOT, card_id: 'native-card', native, questions: [{ question: 'Which?', options: [{ label: 'First' }] }] })) })
    fireEvent.click(await screen.findByText('First'))
    fireEvent.click(screen.getByText('Submit'))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const args = vi.mocked(api.sendChat).mock.calls[0]
    expect(!!args[5]).toBe(steer)
    expect(args[6]).toBe(true)
    await settle(steer ? { ok: true, steered: true } : { ok: true })
    expect(userRows(store)).toHaveLength(1)
  })

  it('answers a blocking MCP card through its answer endpoint, not a chat turn', async () => {
    const { store } = renderCrewmate()
    await ready()
    act(() => { store.dispatch(setQuestionCard({ slot: SLOT, ask_id: 'blocking-ask', questions: [{ question: 'Which?', options: [{ label: 'First' }] }] })) })
    fireEvent.click(await screen.findByText('First'))
    fireEvent.click(screen.getByText('Submit'))
    await waitFor(() => expect(api.answerQuestion).toHaveBeenCalledWith('blocking-ask', { 'Which?': 'First' }))
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it.each([
    { drained: false, status: 'response-late' as const },
    { drained: true, status: 'response-late' as const },
    { drained: false, status: 'refused' as const },
    { drained: true, status: 'refused' as const },
    { drained: false, status: 'transport-error' as const },
    { drained: true, status: 'transport-error' as const },
  ])('queue identity reconciles before a receipt: %j', async ({ drained, status }) => {
    const { appendQueuedMessage, removeQueuedMessage } = await import('../store/chatSlice')
    const { store } = renderCrewmate()
    await ready()
    let finish!: (receipt: transport.SendReceipt) => void
    vi.spyOn(transport, 'sendTurn').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const box = screen.getByPlaceholderText(/Message (Radar|Test)/)
    fireEvent.change(box, { target: { value: 'exact queue message' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(transport.sendTurn).toHaveBeenCalledTimes(1))
    const sendId = userRows(store)[0].meta!.sendId as string
    act(() => {
      store.dispatch(appendQueuedMessage({ slot: SLOT, content: 'exact queue message', ts: FIXED_TIME.toISOString(), queue_id: 'q-exact', meta: { sendId } }))
      if (drained) store.dispatch(removeQueuedMessage({ slot: SLOT, content: 'exact queue message', queue_id: 'q-exact', meta: { sendId } }))
    })
    expect(selectSlotMessages(store.getState() as RootState, SLOT).filter(m => m.content === 'exact queue message')).toHaveLength(1)
    await act(async () => { finish({ status, body: {} }) })
    expect(screen.queryByText(/Delivery pending/)).toBeNull()
    expect(box).toHaveValue('')
    expect(screen.getAllByText('exact queue message')).toHaveLength(1)
    expect(screen.queryByTestId('queue-card')).toBeNull()
  })

  it('a steered send keeps one bubble when its unconsumed steer is requeued for the next turn', async () => {
    const { appendQueuedMessage, removeQueuedMessage } = await import('../store/chatSlice')
    const { store } = renderCrewmate()
    await send('answer me first')
    const sendId = vi.mocked(api.sendChat).mock.calls[0][4]!.sendId as string
    act(() => {
      store.dispatch(appendSlotMessage({ slot: SLOT, message: { role: 'user', content: 'answer me first', cls: 'msg msg-u', ts: '2026-10-14T07:00:01Z', meta: { sendId, mid: 'mid-steer', steer: true, steerState: 'written', steer_delivery_id: 'd-1' } } }))
    })
    await settle({ ok: true, steered: true, responsive: 'steered' })
    expect(userRows(store)).toHaveLength(1)
    expect(userRows(store)[0].meta?.optimistic).toBeUndefined()
    // The grace expired: the cancelled turn's teardown requeues the same send.
    act(() => {
      store.dispatch(appendQueuedMessage({ slot: SLOT, content: 'answer me first', ts: FIXED_TIME.toISOString(), queue_id: 'q-steer', meta: { sendId } }))
    })
    expect(screen.getAllByText('answer me first')).toHaveLength(1)
    expect(screen.queryByTestId('queue-card')).toBeNull()
    expect(screen.queryByText(/Steered|Steering|runs as its own/)).toBeNull()
    act(() => {
      store.dispatch(removeQueuedMessage({ slot: SLOT, content: 'answer me first', queue_id: 'q-steer', drain_writes_row: true, meta: { sendId } }))
    })
    expect(screen.getAllByText('answer me first')).toHaveLength(1)
    expect(userRows(store)).toHaveLength(1)
  })
})

describe('mate chat order and typing while a send waits', () => {
  const before = (a: HTMLElement, b: HTMLElement) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)

  it('a queued message sits where it was sent: a later reply draws below it, and the drain keeps the order and the node', async () => {
    const { appendQueuedMessage, removeQueuedMessage } = await import('../store/chatSlice')
    const { store } = renderCrewmate({ messages: [{ role: 'user', content: 'first ask', cls: 'msg msg-u', ts: '2026-10-14T06:59:00Z' }] })
    await screen.findByText('first ask')
    await send('second ask')
    const sendId = vi.mocked(api.sendChat).mock.calls[0][4]!.sendId as string
    act(() => {
      store.dispatch(appendQueuedMessage({ slot: SLOT, content: 'second ask', ts: '2026-10-14T07:00:02Z', queue_id: 'q-order', meta: { sendId } }))
    })
    await settle({ ok: true, queued: true, queue_id: 'q-order' })
    const queued = screen.getByText('second ask')
    act(() => {
      store.dispatch(appendSlotMessage({ slot: SLOT, message: { role: 'assistant', content: 'reply to the first', cls: 'msg msg-a', ts: '2026-10-14T07:00:03Z' } }))
    })
    const reply = await screen.findByText('reply to the first')
    expect(before(screen.getByText('first ask'), queued)).toBe(true)
    expect(before(queued, reply)).toBe(true)
    act(() => {
      store.dispatch(removeQueuedMessage({ slot: SLOT, content: 'second ask', queue_id: 'q-order', meta: { sendId } }))
    })
    expect(screen.getAllByText('second ask')).toHaveLength(1)
    expect(screen.getByText('second ask')).toBe(queued)
    expect(before(screen.getByText('second ask'), screen.getByText('reply to the first'))).toBe(true)
    const rows = selectSlotMessages(store.getState() as RootState, SLOT)
    expect(rows.map(m => [m.role, m.content])).toEqual([
      ['user', 'first ask'], ['user', 'second ask'], ['assistant', 'reply to the first'],
    ])
    expect(rows[1].meta?.optimistic).toBeUndefined()
  })

  it('shows the typing bubble the moment an idle DM is sent to, and drops it when the reply lands', async () => {
    const { store } = renderCrewmate({ running: false })
    await ready()
    expect(screen.queryByTestId('crewmate-live-activity')).toBeNull()
    await send('are you there')
    expect(screen.getByTestId('crewmate-live-activity')).toBeInTheDocument()
    expect(screen.getByTestId('crewmate-typing-bubble')).toHaveAttribute('data-shown', 'true')
    expect(screen.getByTestId('crewmate-live-activity')).toHaveAttribute('data-activity', 'working')
    await settle({ ok: true, dispatched: true })
    expect(screen.getByTestId('crewmate-live-activity')).toBeInTheDocument()
    act(() => {
      store.dispatch(appendSlotMessage({ slot: SLOT, message: { role: 'assistant', content: 'here', cls: 'msg msg-a', ts: '2026-10-14T07:00:04Z' } }))
    })
    await screen.findByText('here')
    expect(screen.queryByTestId('crewmate-live-activity')).toBeNull()
  })

  it('keeps typing through the end of the earlier turn while the send is queued, and stops when its own turn ends silently', async () => {
    const { appendQueuedMessage, removeQueuedMessage } = await import('../store/chatSlice')
    const { store } = renderCrewmate({ running: true })
    await send('after this one')
    const sendId = vi.mocked(api.sendChat).mock.calls[0][4]!.sendId as string
    act(() => {
      store.dispatch(appendQueuedMessage({ slot: SLOT, content: 'after this one', ts: '2026-10-14T07:00:02Z', queue_id: 'q-wait', meta: { sendId } }))
    })
    await settle({ ok: true, queued: true, queue_id: 'q-wait' })
    // The earlier turn ends; this send is still parked for the next one.
    act(() => { store.dispatch(updateSlot({ key: SLOT, running: false })) })
    expect(screen.getByTestId('crewmate-live-activity')).toHaveAttribute('data-activity', 'working')
    act(() => {
      store.dispatch(removeQueuedMessage({ slot: SLOT, content: 'after this one', queue_id: 'q-wait', meta: { sendId } }))
      store.dispatch(updateSlot({ key: SLOT, running: true }))
    })
    expect(screen.getByTestId('crewmate-live-activity')).toBeInTheDocument()
    // Its own turn ends without writing a word: nothing is coming.
    act(() => { store.dispatch(updateSlot({ key: SLOT, running: false })) })
    expect(screen.queryByTestId('crewmate-live-activity')).toBeNull()
  })

  it('a send that waits behind a streaming reply keeps its place through the row written at send, the reply\'s end and the turn-end warm', async () => {
    const { appendQueuedMessage, removeQueuedMessage, warmSlotCache } = await import('../store/chatSlice')
    const first: ChatMessage = { role: 'user', content: 'first ask', cls: 'msg msg-u', ts: '2026-10-14T06:59:00Z', meta: { mid: 'u-first' } }
    const { store } = renderCrewmate({ messages: [first] })
    await screen.findByText('first ask')
    act(() => { store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'part one', seq: 1 })) })
    await send('second ask')
    // The virtual row wrapper is keyed by the row's key; it surviving means the
    // message never left its place to be redrawn.
    const rowOf = () => screen.getByText('second ask').closest('[data-display-index]')
    const row = rowOf()
    expect(row).not.toBeNull()
    const sendId = vi.mocked(api.sendChat).mock.calls[0][4]!.sendId as string
    const order = () => [...document.querySelectorAll('[data-role="user"], [data-role="assistant"]')]
      .map(e => `${e.getAttribute('data-role')}:${e.querySelector('.message-bubble')?.textContent}`)
    // The gateway's frames for a send that waits (responsive_chat.accept):
    // the queue entry, then the user row it writes at send time.
    act(() => {
      store.dispatch(appendQueuedMessage({ slot: SLOT, content: 'second ask', ts: '2026-10-14T07:00:02Z', queue_id: 'q-wait', meta: { sendId } }))
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'second ask', ts: '2026-10-14T07:00:02.5Z', meta: { sendId, mid: 'u-second' } }))
    })
    await settle({ ok: true, queued: true, queue_id: 'q-wait', responsive: 'queued' })
    // The reply goes on below the message; the segment the gateway cut at the
    // send holds only what came after it.
    act(() => {
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'part two', seq: 2 }))
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'part two', ts: '2026-10-14T07:00:05Z', meta: { mid: 'a-two' } }))
    })
    const live = ['user:first ask', 'assistant:part one', 'user:second ask', 'assistant:part two']
    expect(order()).toEqual(live)
    // The row written at send took the queued bubble's place and key.
    expect(rowOf()).toBe(row)
    // Turn end: the pane warms from the gateway's history, which now holds the
    // rows in the order they were sent, with the entry still queued.
    const history: ChatMessage[] = [
      first,
      { role: 'assistant', content: 'part one', cls: 'msg msg-a', ts: '2026-10-14T07:00:01Z', meta: { mid: 'a-one' } },
      { role: 'user', content: 'second ask', cls: 'msg msg-u', ts: '2026-10-14T07:00:02.5Z', meta: { mid: 'u-second', sendId } },
      { role: 'assistant', content: 'part two', cls: 'msg msg-a', ts: '2026-10-14T07:00:05Z', meta: { mid: 'a-two' } },
    ]
    vi.mocked(api.chatSlotDetail).mockResolvedValue({ messages: history, running: false, has_more: false, total: history.length, queue: [{ content: 'second ask', queueId: 'q-wait', ts: '2026-10-14T07:00:02Z', meta: { sendId } }] } as never)
    act(() => { store.dispatch(updateSlot({ key: SLOT, running: false })) })
    await act(async () => { await store.dispatch(warmSlotCache(SLOT)) })
    expect(order()).toEqual(live)
    // The drain reuses that row (drain_writes_row), so nothing moves.
    act(() => { store.dispatch(removeQueuedMessage({ slot: SLOT, content: 'second ask', queue_id: 'q-wait', drain_writes_row: true, meta: { sendId } })) })
    expect(order()).toEqual(live)
    expect(screen.getAllByText('second ask')).toHaveLength(1)
  })

  it('a refused send leaves no typing bubble behind', async () => {
    renderCrewmate({ running: false })
    await send('will be refused')
    expect(screen.getByTestId('crewmate-live-activity')).toBeInTheDocument()
    await settle({ ok: false, error: 'request refused' }, 409)
    await screen.findByText(/request refused/)
    expect(screen.queryByTestId('crewmate-live-activity')).toBeNull()
  })
})

describe('the composer while a send waits for the turn that answers it', () => {
  // The last turn died on an error, so Resume is on offer before the send.
  const interrupted: ChatMessage[] = [
    { role: 'user', content: 'check the build', cls: 'msg msg-u', ts: '2026-10-14T06:59:00Z', meta: { mid: 'u-first' } },
    { role: 'error', content: 'Bedrock is throttling requests.', cls: 'msg msg-err', ts: '2026-10-14T06:59:01Z' },
  ]
  const resumeShown = () => !!screen.queryByTestId('composer-continue') || !!screen.queryByTestId('error-card-continue')
  // While Resume is on offer the placeholder names it instead of the crewmate.
  async function typeAndSend(text: string) {
    const box = await screen.findByPlaceholderText(/Message Radar|press Resume/)
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalled())
    fireEvent.change(box, { target: { value: text } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
  }

  // The gateway's frames for a send to an idle DM (responsive_chat.accept): it
  // writes the user row and starts the turn inside the POST, so the row and
  // the `queued` receipt land before any frame says the turn is running.
  async function sendToIdleDm(store: ReturnType<typeof renderCrewmate>['store'], text: string) {
    await typeAndSend(text)
    const sendId = vi.mocked(api.sendChat).mock.calls[0][4]!.sendId as string
    act(() => {
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: text, ts: '2026-10-14T07:00:01Z', meta: { sendId, mid: 'u-sent' } }))
    })
    await settle({ ok: true, queued: true, queue_id: 'q-idle', responsive: 'queued' })
  }

  it('reads busy from the Send and never offers Resume before the turn is seen', async () => {
    const { store } = renderCrewmate({ running: false, messages: interrupted })
    await screen.findByTestId('composer-continue')
    await sendToIdleDm(store, 'try again please')
    expect(userRows(store).at(-1)?.meta?.optimistic).toBeUndefined()
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
    expect(resumeShown()).toBe(false)
    // A slots frame serialized before the turn started still says idle.
    act(() => { store.dispatch(updateSlot({ key: SLOT, running: false })) })
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
    expect(resumeShown()).toBe(false)
    // The turn is live, then answers and ends.
    act(() => {
      store.dispatch(updateSlot({ key: SLOT, running: true }))
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'On it', seq: 1 }))
    })
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
    expect(resumeShown()).toBe(false)
    act(() => {
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'On it. The build is green.', ts: '2026-10-14T07:00:05Z', meta: { mid: 'a-reply' } }))
      store.dispatch(sseChatMessage({ slot: SLOT, role: '_done', content: '' } as Parameters<typeof sseChatMessage>[0]))
      store.dispatch(updateSlot({ key: SLOT, running: false }))
    })
    await screen.findByText('On it. The build is green.')
    expect(screen.queryByTestId('stop-button-armed')).toBeNull()
    expect(screen.getByRole('button', { name: 'Send', exact: true })).toBeDisabled()
    expect(resumeShown()).toBe(false)
  })

  it.each([
    { what: 'a turn that came and went inside one slots window', turn: 5, gen: 'gen-a' },
    { what: 'a gateway restart', turn: 0, gen: 'gen-b' },
  ])('gives Resume back after $what left the send unanswered', async ({ turn, gen }) => {
    const { store } = renderCrewmate({ running: false, messages: interrupted })
    await screen.findByTestId('composer-continue')
    act(() => { store.dispatch(updateSlot({ key: SLOT, turn: 4, turn_gen: 'gen-a' })) })
    await sendToIdleDm(store, 'try again please')
    expect(resumeShown()).toBe(false)
    act(() => { store.dispatch(updateSlot({ key: SLOT, running: false, turn, turn_gen: gen })) })
    expect(await screen.findByTestId('composer-continue')).toBeInTheDocument()
    expect(screen.getByTestId('error-card-continue')).toBeInTheDocument()
    expect(screen.queryByTestId('stop-button-armed')).toBeNull()
    expect(screen.queryByTestId('crewmate-live-activity')).toBeNull()
  })

  it('keeps waiting while the send is still queued behind the turn that just ended', async () => {
    const { appendQueuedMessage } = await import('../store/chatSlice')
    const { store } = renderCrewmate({ running: true, messages: interrupted })
    act(() => { store.dispatch(updateSlot({ key: SLOT, turn: 4, turn_gen: 'gen-a' })) })
    await typeAndSend('after this one')
    const sendId = vi.mocked(api.sendChat).mock.calls[0][4]!.sendId as string
    act(() => {
      store.dispatch(appendQueuedMessage({ slot: SLOT, content: 'after this one', ts: '2026-10-14T07:00:02Z', queue_id: 'q-wait', meta: { sendId } }))
    })
    await settle({ ok: true, queued: true, queue_id: 'q-wait' })
    act(() => { store.dispatch(updateSlot({ key: SLOT, running: false, turn: 5, turn_gen: 'gen-a' })) })
    expect(screen.getByTestId('crewmate-live-activity')).toBeInTheDocument()
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
    expect(resumeShown()).toBe(false)
  })
})
