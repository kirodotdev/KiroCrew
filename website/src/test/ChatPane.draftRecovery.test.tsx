import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { selectSlotMessages, setActiveSlot, sseChatMessage } from '../store/chatSlice'
import type { SendReceipt, SendTurnOptions } from '../chat-core/transport/sendTurn'
import { queuedSendStash } from '../hooks/useQueuedMessageActions'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* Draft / recovery hardening for ChatPane, surfaced by review of the
 * steer-only DM composer (#8852). A pane can be rebound to another slot
 * without remounting (the Members page switches `slotKey` on one instance)
 * and can unmount with sends and uploads still in flight. Every late result
 * — a refused or unconfirmed send, a finished or failed upload — must reach
 * the slot it belongs to (its live composer, its parked draft, or its
 * transcript), never the composer now on screen and never nowhere. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: true, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
    editQueuedMessage: vi.fn().mockResolvedValue({ ok: true }),
    cancelQueuedMessage: vi.fn().mockResolvedValue({ ok: true }),
    interruptSlot: vi.fn().mockResolvedValue({ ok: true }),
    reorderQueuedMessages: vi.fn().mockResolvedValue({ ok: true }),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

/** Pass-through on the chat-core transport with ONE override hook: a test that
 *  needs an outcome the mocked fetch cannot produce deterministically (the
 *  abort deadline -> `response-late`) forces the receipt here instead of
 *  driving fake timers through react-query. Every other test hits the real
 *  `sendTurn` and asserts on `api.sendChat`'s arguments. */
let forcedReceipt: SendReceipt | null = null
let lastSendTurnOpts: SendTurnOptions | null = null
vi.mock('../chat-core/transport/sendTurn', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../chat-core/transport/sendTurn')>()
  return {
    ...actual,
    sendTurn: (opts: SendTurnOptions) => {
      lastSendTurnOpts = opts
      return forcedReceipt ? Promise.resolve(forcedReceipt) : actual.sendTurn(opts)
    },
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

type BusyMode = 'split' | 'steer-only'

function makeStore(slotKey: string, running: boolean, subagentsOnly = false) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        // `subagents_running` is the slots-stream snapshot flag selectComposerBusy
        // reads: busy WITHOUT a running main turn (spawn_run is fire-and-forget).
        slots: [{ key: slotKey, messages: 0, running, subagents_running: subagentsOnly, mode: 'member', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  // The pane is a BACKGROUND slot (the Members page never makes the DM the
  // active chat slot). A streamed chunk on it is what makes the pane's main
  // turn "running" in the store — the same frame the live WS delivers.
  store.dispatch(setActiveSlot('front'))
  if (running) store.dispatch(sseChatMessage({ slot: slotKey, role: 'chunk', content: 'working…', seq: 1 }))
  return store
}

function renderPane(slotKey: string, opts: { running: boolean; busyMode?: BusyMode; subagentsOnly?: boolean }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey, opts.running, opts.subagentsOnly)
  const ui = (key: string) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={key} {...(opts.busyMode ? { busyMode: opts.busyMode } : {})} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
  const r = render(ui(slotKey))
  // Rebind the SAME pane instance to another slot — what the Members page does
  // when the user clicks another member (no `key`, so no remount).
  const rebind = (key: string) => r.rerender(ui(key))
  return Object.assign(r, { store, rebind })
}

const composer = async () => (await screen.findAllByRole('textbox'))[0]

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  // The pane parks its composer on unmount (RTL cleanup) into a module-level
  // mirror that storage.clear() cannot reach; without this the previous
  // test's text rides into the next test's rebind of the same slot.
  __resetPaneDraftsForTests()
  forcedReceipt = null
  lastSendTurnOpts = null
  vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) } as unknown as Response)
  // clearAllMocks keeps implementations; a test that seeds slot-detail rows
  // must not leak them into the next one.
  vi.mocked(api.chatSlotDetail).mockResolvedValue({ messages: [], running: true, has_more: false, total: 0 } as never)
})

describe('ChatPane draft & recovery hardening (steer-only DM thread)', () => {
  it('a steer the server demoted to the queue binds the typed text to its queue card, so cancelling restores it', async () => {
    // A backend without a steer channel parks the message instead: the queue
    // card then owns it, and its cancel must hand back what was TYPED (and
    // re-stage files), which is the same stash doSend keeps for its own queued
    // sends. The optimistic steer bubble is withdrawn — the card represents it.
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, queued: true, queue_id: 'q-demoted' }) } as unknown as Response)
    const { store } = renderPane('member-demoted', { running: true, busyMode: 'steer-only' })
    const box = await composer()
    fireEvent.change(box, { target: { value: 'park me if you must' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(queuedSendStash.get('q-demoted')).toEqual({ raw: 'park me if you must', files: [], sent: 'park me if you must' }))
    await waitFor(() => expect(selectSlotMessages(store.getState() as RootState, 'member-demoted').some(m => m.role === 'user' && m.meta?.steer)).toBe(false))
    queuedSendStash.delete('q-demoted')
  })

  it('sub-agents-only busy: a send whose receipt never came hands the draft back and warns instead of losing it', async () => {
    // Busy WITHOUT a running main turn: only sub-agents are active, so the
    // steer takes the send-with-steer-flag path, which mints no optimistic
    // bubble. The transport's deadline then fires (response-late) — forced
    // through the transport hook, since nothing on screen represents the text.
    forcedReceipt = { status: 'response-late', body: {} }
    const { store } = renderPane('member-subagents', { running: false, busyMode: 'steer-only', subagentsOnly: true })
    const box = await composer()
    fireEvent.change(box, { target: { value: 'while you wait, look at #77' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(lastSendTurnOpts).not.toBeNull())
    expect(lastSendTurnOpts?.steer).toBe(true)
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('while you wait, look at #77'))
    const rows = selectSlotMessages(store.getState() as RootState, 'member-subagents')
    expect(rows.some(m => m.role === 'notice')).toBe(true)
    expect(rows.some(m => m.role === 'user')).toBe(false)
  })

  it('sub-agents-only busy: a send with an attachment inlines it into the wire text, so a demotion to the queue cannot drop it', async () => {
    // The steer-flagged send may be demoted to the queue, which keeps only the
    // wire text (no `meta`). An attachment riding `meta.files` alone would
    // then execute as a turn without it — so the send inlines the file as
    // `[attached_file N] path` (ChatPage's and doSteer's wire shape, the
    // pane's since #9433) and keeps the RAW file list for the cancel-restore
    // stash. Pinned here for the steer-flagged path specifically.
    vi.mocked(api.uploadFiles).mockResolvedValue({ paths: ['/tmp/uploads/report.pdf'] })
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, queued: true, queue_id: 'q-inlined' }) } as unknown as Response)
    const { container } = renderPane('member-inline', { running: false, busyMode: 'steer-only', subagentsOnly: true })
    const box = await composer()
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [new File(['x'], 'report.pdf', { type: 'application/pdf' })] })
    fireEvent.change(fileInput)
    expect(await screen.findByText('report.pdf')).toBeInTheDocument()
    fireEvent.change(box, { target: { value: 'read this' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(lastSendTurnOpts).not.toBeNull())
    expect(lastSendTurnOpts!.steer).toBe(true)
    expect(lastSendTurnOpts!.message).toBe('read this\n[attached_file 1] /tmp/uploads/report.pdf')
    expect((lastSendTurnOpts!.meta as { files?: string[] }).files).toEqual(['/tmp/uploads/report.pdf'])
    // The queue card's cancel restores the typed text and re-stages the chip.
    await waitFor(() => expect(queuedSendStash.get('q-inlined')).toEqual({ raw: 'read this', files: ['/tmp/uploads/report.pdf'], sent: 'read this\n[attached_file 1] /tmp/uploads/report.pdf' }))
    queuedSendStash.delete('q-inlined')
  })

  it('an identical queue entry does not stand in for the unconfirmed send — the draft still comes back', async () => {
    // "ok" is already parked on the slot's queue. The user sends "ok" again;
    // the receipt never arrives. Queue cards carry no sendId, so NO card may be
    // read as this send's echo — only an id-bearing user echo can — or the
    // new draft is lost behind a card that may belong to anyone.
    vi.mocked(api.chatSlotDetail).mockResolvedValue({
      messages: [{ role: 'queued', content: 'ok', cls: 'msg msg-queued', ts: '2026-09-06T00:00:01Z', meta: { queueId: 'q-old' } }],
      running: false, has_more: false, total: 1,
    } as never)
    forcedReceipt = { status: 'response-late', body: {} }
    const { store } = renderPane('member-dup-queue', { running: false, busyMode: 'steer-only', subagentsOnly: true })
    const box = await composer()
    await screen.findByRole('button', { name: 'Cancel queued message' })
    fireEvent.change(box, { target: { value: 'ok' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(lastSendTurnOpts).not.toBeNull())
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('ok'))
    const rows = selectSlotMessages(store.getState() as RootState, 'member-dup-queue')
    expect(rows.some(m => m.role === 'notice')).toBe(true)
    // The pre-existing card is untouched.
    expect(rows.filter(m => m.role === 'queued')).toHaveLength(1)
  })

  it('rebinding the pane to another member parks the draft — it does not ride into the other composer', async () => {
    const { rebind } = renderPane('member-a', { running: false, busyMode: 'steer-only' })
    const box = await composer()
    fireEvent.change(box, { target: { value: 'half-typed note for A' } })
    rebind('member-b')
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(''))
    fireEvent.change(box, { target: { value: 'something for B' } })
    rebind('member-a')
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('half-typed note for A'))
    rebind('member-b')
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('something for B'))
  })

  it('a refusal that lands after the user switched members restores into the SENDING member, not the one on screen', async () => {
    let refuse!: () => void
    vi.mocked(api.sendChat).mockReturnValue(new Promise((resolve) => {
      refuse = () => resolve({ ok: false, status: 409, json: () => Promise.resolve({ error: 'slot agent mismatch' }) } as unknown as Response)
    }) as ReturnType<typeof api.sendChat>)
    const { store, rebind } = renderPane('member-a', { running: true, busyMode: 'steer-only' })
    const box = await composer()
    fireEvent.change(box, { target: { value: 'meant for A' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    // The user moves on to B while A's steer is still in flight...
    rebind('member-b')
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(''))
    // ...and A's send is refused now.
    await act(async () => { refuse() })
    await waitFor(() => expect(selectSlotMessages(store.getState() as RootState, 'member-a').some(m => m.role === 'error')).toBe(true))
    // B's composer is untouched; the error row went to A's transcript.
    expect((box as HTMLTextAreaElement).value).toBe('')
    expect(selectSlotMessages(store.getState() as RootState, 'member-b').some(m => m.role === 'error')).toBe(false)
    // Back on A, the text is waiting.
    rebind('member-a')
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('meant for A'))
  })

  it('an upload that finishes after the user switched members is staged for the member it was picked in', async () => {
    let finish!: () => void
    vi.mocked(api.uploadFiles).mockReturnValue(new Promise((resolve) => {
      finish = () => resolve({ paths: ['/tmp/uploads/report.pdf'] })
    }) as ReturnType<typeof api.uploadFiles>)
    const { container, rebind } = renderPane('member-a', { running: false, busyMode: 'steer-only' })
    await composer()
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [new File(['x'], 'report.pdf', { type: 'application/pdf' })] })
    fireEvent.change(fileInput)
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
    // Switch to B while A's upload is still in flight, then let it finish.
    rebind('member-b')
    await act(async () => { finish() })
    // B did not receive A's attachment…
    expect(screen.queryByText('report.pdf')).not.toBeInTheDocument()
    // …A has it waiting.
    rebind('member-a')
    expect(await screen.findByText('report.pdf')).toBeInTheDocument()
  })

  it('an upload that FAILS after the user switched members is reported in that member\'s transcript, not over the other thread', async () => {
    let fail!: () => void
    vi.mocked(api.uploadFiles).mockReturnValue(new Promise((resolve) => {
      fail = () => resolve({ paths: [], error: 'Unsupported file type' })
    }) as ReturnType<typeof api.uploadFiles>)
    const { container, store, rebind } = renderPane('member-a', { running: false, busyMode: 'steer-only' })
    await composer()
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [new File(['x'], 'evil.exe')] })
    fireEvent.change(fileInput)
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
    rebind('member-b')
    await act(async () => { fail() })
    // Not a banner over B…
    expect(screen.queryByTestId('chat-pane-upload-error')).not.toBeInTheDocument()
    // …but an error row in A's conversation, which survives rebinds and page exits alike.
    expect(selectSlotMessages(store.getState() as RootState, 'member-a').find(m => m.role === 'error')?.content).toContain('Unsupported file type')
    expect(selectSlotMessages(store.getState() as RootState, 'member-b').some(m => m.role === 'error')).toBe(false)
  })

  it('an upload failure shown on screen renders through the shared error surface and is dismissible', async () => {
    vi.mocked(api.uploadFiles).mockResolvedValue({ paths: [], error: 'Unsupported file type' } as never)
    const { container } = renderPane('member-a', { running: false, busyMode: 'steer-only' })
    await composer()
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [new File(['x'], 'evil.exe')] })
    fireEvent.change(fileInput)
    const notice = await screen.findByTestId('chat-pane-upload-error')
    expect(notice).toHaveTextContent('Unsupported file type')
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    await waitFor(() => expect(screen.queryByTestId('chat-pane-upload-error')).not.toBeInTheDocument())
  })

  it('an upload that fails after the pane unmounted lands in that slot\'s transcript as an error row', async () => {
    let fail!: () => void
    vi.mocked(api.uploadFiles).mockReturnValue(new Promise((resolve) => {
      fail = () => resolve({ paths: [], error: 'Unsupported file type' })
    }) as ReturnType<typeof api.uploadFiles>)
    const first = renderPane('member-a', { running: false, busyMode: 'steer-only' })
    await composer()
    const fileInput = first.container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [new File(['x'], 'evil.exe')] })
    fireEvent.change(fileInput)
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
    first.unmount()
    await act(async () => { fail() })
    const rows = selectSlotMessages(first.store.getState() as RootState, 'member-a')
    expect(rows.find(m => m.role === 'error')?.content).toContain('Unsupported file type')
  })

  it('a refusal that lands after the member was closed and REOPENED goes into the new pane\'s composer and survives its next park', async () => {
    let refuse!: () => void
    vi.mocked(api.sendChat).mockReturnValue(new Promise((resolve) => {
      refuse = () => resolve({ ok: false, status: 409, json: () => Promise.resolve({ error: 'slot agent mismatch' }) } as unknown as Response)
    }) as ReturnType<typeof api.sendChat>)
    const first = renderPane('member-a', { running: true, busyMode: 'steer-only' })
    const box1 = await composer()
    fireEvent.change(box1, { target: { value: 'sent from the first pane' } })
    fireEvent.keyDown(box1, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    first.unmount()
    // Reopen the member: a NEW pane shows A before the old send has resolved.
    const second = renderPane('member-a', { running: false, busyMode: 'steer-only' })
    const box2 = await composer()
    expect((box2 as HTMLTextAreaElement).value).toBe('')
    await act(async () => { refuse() })
    // The late recovery reaches the pane that is showing A right now…
    await waitFor(() => expect((box2 as HTMLTextAreaElement).value).toBe('sent from the first pane'))
    // …and is not lost when that pane parks and a third one comes back.
    second.rebind('member-b')
    await waitFor(() => expect((box2 as HTMLTextAreaElement).value).toBe(''))
    second.rebind('member-a')
    await waitFor(() => expect((box2 as HTMLTextAreaElement).value).toBe('sent from the first pane'))
  })

  it('a draft survives the pane unmounting (leaving the page) and a refusal that lands while it is gone', async () => {
    let refuse!: () => void
    vi.mocked(api.sendChat).mockReturnValue(new Promise((resolve) => {
      refuse = () => resolve({ ok: false, status: 409, json: () => Promise.resolve({ error: 'slot agent mismatch' }) } as unknown as Response)
    }) as ReturnType<typeof api.sendChat>)
    const first = renderPane('member-a', { running: true, busyMode: 'steer-only' })
    const box = await composer()
    fireEvent.change(box, { target: { value: 'sent, then I left' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    fireEvent.change(box, { target: { value: 'still typing' } })
    // Leave the Members page: the pane unmounts with the send in flight…
    first.unmount()
    // …and the send is refused with nobody on screen to hand the text to.
    await act(async () => { refuse() })
    // Coming back, both the unsent typing and the refused text are waiting.
    renderPane('member-a', { running: false, busyMode: 'steer-only' })
    const again = await composer()
    await waitFor(() => expect((again as HTMLTextAreaElement).value).toContain('still typing'))
    expect((again as HTMLTextAreaElement).value).toContain('sent, then I left')
  })
})
