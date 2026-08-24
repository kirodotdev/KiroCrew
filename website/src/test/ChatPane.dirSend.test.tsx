import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setQuestionCard } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* ChatPane sends must follow ChatPage's wire/bubble split for folder tokens
 * (issue #743 review finding): the API payload carries `[attached_dir N] path`
 * markers plus meta.dirs, while the optimistic bubble keeps the raw `@path/`
 * token for the chip. Without this, a split-pane send ships the display token
 * verbatim and history replay has no meta.dirs to resolve. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
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
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }, { name: 'reviewer' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

function makeStore(slotKey: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(slotKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey)
  return renderWithStore(store, qc, slotKey)
}

function renderWithStore(store: ReturnType<typeof makeStore>, qc: QueryClient, slotKey: string) {
  return Object.assign(render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  ), { store })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ChatPane send — folder token serialization', () => {
  it('sends [attached_dir N] wire text with meta.dirs; bubble keeps the raw token', async () => {
    renderPane('pane-1')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'please review @/home/user/design-assets/ thanks' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, slot, , , meta] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(slot).toBe('pane-1')
    expect(wireText).toBe('please review [attached_dir 1] /home/user/design-assets thanks')
    expect(meta).toEqual({ dirs: ['/home/user/design-assets'], sendId: expect.stringMatching(/^s-/) })
  })

  it('sends plain text untouched (sendId only) when there are no folder tokens', async () => {
    renderPane('pane-2')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'just words' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, , , , meta] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('just words')
    // sendId always rides meta (same contract as ChatPage) so the server echo
    // reconciles against the optimistic bubble even when wire text diverges.
    expect(meta).toEqual({ sendId: expect.stringMatching(/^s-/) })
  })
})

/* #4131: the pane's optimistic bubble is confirmed by the send's OWN response.
 * No `chat_message` user echo is coming — `DashboardState.append` suppresses it
 * for dashboard sends because the composer already rendered the bubble — so an
 * accepted response is the only thing that can retire the pending state at all.
 * The 30s wall-clock notice that used to read that state is gone precisely
 * because it fired on every dashboard send, delivered ones included. */
describe('ChatPane send — the response confirms the optimistic bubble', () => {
  const userRow = (store: ReturnType<typeof makeStore>, slot: string) =>
    store.getState().chat.slotMessages[slot]?.find(m => m.role === 'user')

  it('retires the pending-confirmation flags when the server accepts', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ ok: true, mid: 'm-server-confirmed' }),
    })
    const { store } = renderPane('pane-confirm')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'confirm me' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(userRow(store, 'pane-confirm')?.meta?.optimistic).toBeUndefined())
    // The correlation id stays so a late echo updates this row in place.
    expect(userRow(store, 'pane-confirm')?.meta?.sendId).toMatch(/^s-/)
    expect(userRow(store, 'pane-confirm')?.meta?.mid).toBe('m-server-confirmed')
  })

  it('leaves the bubble pending when the server rejects the send', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })
    const { store } = renderPane('pane-reject')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'refuse me' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    // A refusal is not a receipt, so the pending flag must survive it. What the
    // user is told is the error row the refusal path appends, not this flag.
    expect(userRow(store, 'pane-reject')?.meta?.optimistic).toBe(true)
  })
})

/* The split-view pane is the third dashboard caller of `chatSlotAgent`. It used
 * to swallow failures with `console.error`, so a switch that never happened
 * looked identical to one that did. It now feeds the same shared notice the
 * chat picker and the cycle shortcuts use. */
describe('ChatPane agent switch — failures reach the shared notice', () => {
  async function openAgentPicker() {
    const { store } = renderPane('pane-agent')
    const trigger = await screen.findByLabelText(/agent/i)
    fireEvent.click(trigger)
    return store
  }

  it('publishes the failure message instead of only logging it', async () => {
    const { ApiError } = await import('../api/client') as unknown as {
      ApiError: new (s: number, m: string, b?: string) => Error
    }
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(400, 'invalid agent name', JSON.stringify({ error: 'invalid agent name' })),
    )
    const store = await openAgentPicker()
    fireEvent.click(await screen.findByText('reviewer'))

    await waitFor(() => expect(api.chatSlotAgent).toHaveBeenCalledWith('pane-agent', 'reviewer'))
    await waitFor(() =>
      expect(store.getState().chat.agentSwitchNotice?.message).toBe('invalid agent name'),
    )
  })

  it('leaves no notice behind when the switch succeeds', async () => {
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockResolvedValueOnce(undefined)
    const store = await openAgentPicker()
    fireEvent.click(await screen.findByText('reviewer'))

    await waitFor(() => expect(api.chatSlotAgent).toHaveBeenCalledWith('pane-agent', 'reviewer'))
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })
})

/* Producer side of the split-view focus contract: `queryComposer()` scopes its
 * lookup to the `[data-chat-pane]` ancestor of the focused element, falling
 * back to the pane marked `data-chat-pane="focused"` when focus sits in a
 * portal (the pane's own pickers render under document.body). The REAL pane
 * wrapper must carry the attribute — with value "focused" exactly when the
 * grid marks the pane focused — and contain the pane's composer. Losing
 * either would not fail any focus test that mounts fake panes; it would only
 * silently degrade split-view shortcuts back to first-pane-wins in
 * production. */
describe('ChatPane pane boundary — data-chat-pane contract', () => {
  it('the pane wrapper carries data-chat-pane and contains the pane composer', async () => {
    const { container } = renderPane('pane-focus')
    const pane = container.querySelector('[data-chat-pane]')
    expect(pane).not.toBeNull()
    const composer = await screen.findAllByRole('textbox')
    expect(pane!.contains(composer[0])).toBe(true)
    expect(pane!.querySelector('textarea[data-composer-input]')).not.toBeNull()
  })

  it('the wrapper marks the grid-focused pane with the "focused" value', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-marked')
    const { container } = render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey="pane-marked" focused />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
    expect(container.querySelector('[data-chat-pane="focused"]')).not.toBeNull()
  })

  it('keyboard focus into the pane claims grid focus, not just mousedown', async () => {
    // Tab into a pane (no mousedown) must move the grid's focused marker,
    // or the "focused" fallback would name a pane the user already left and
    // route Alt+Enter from a portaled picker to the wrong session.
    const onFocus = vi.fn()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-kbd')
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey="pane-kbd" onFocus={onFocus} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
    const box = (await screen.findAllByRole('textbox'))[0]
    box.focus()
    expect(onFocus).toHaveBeenCalled()
  })
})

/* A pane send that fails used to report NOTHING: the composer cleared on the way
 * out, the optimistic bubble stayed on screen, and the rejected fetch was
 * swallowed by `.catch(() => undefined)`, so an undelivered message looked sent
 * forever. The only signal it ever had was a 30s wall-clock "may not have been
 * delivered" notice bolted onto every optimistic row — which fired on delivered
 * messages too and offered no action. These pin the real signal that replaced
 * it: assert the failure where the message was typed, and hand the text back. */
describe('ChatPane send — a failed send is reported on the pane', () => {
  const errorsIn = (store: ReturnType<typeof makeStore>, slot: string) =>
    (store.getState().chat.slotMessages[slot] || []).filter(m => m.role === 'error')

  it('reports a rejected send and hands the text back to the composer', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('offline'))
    const { store } = renderPane('pane-reject')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'this one never left' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(errorsIn(store, 'pane-reject')).toHaveLength(1))
    // Asserted as a non-empty error row rather than by copy: the string comes
    // from the shared `pages.chatPage.send_failed` catalog entry, and pinning
    // its wording here would fail on any locale and on the test env's fallback.
    expect(errorsIn(store, 'pane-reject')[0].content.trim().length).toBeGreaterThan(0)
    // The payload is recoverable rather than lost, which is the action the
    // removed notice never offered.
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('this one never left'))
  })

  it('reports a body the server accepted as neither ok nor queued', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true, json: () => Promise.resolve({ error: 'slot is stopping' }),
    })
    const { store } = renderPane('pane-refused')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'refused at the guard' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(errorsIn(store, 'pane-refused')).toHaveLength(1))
    // The server's own reason survives. "check your connection" would be wrong
    // AND unactionable for a 409 the caller can actually do something about.
    expect(errorsIn(store, 'pane-refused')[0].content).toBe('slot is stopping')
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('refused at the guard'))
  })

  it('says nothing when a 2xx receipt will not parse, and keeps the composer clear (#4217)', async () => {
    // A truncated or proxy-mangled body on an ACCEPTED post is not a refusal:
    // the request got through and the turn may be streaming. The pane treats it
    // exactly as it treats the 10s abort below — no error row, and the payload
    // stays out of the composer so a retry cannot duplicate a delivered turn.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true, json: () => Promise.reject(new Error('unexpected end of JSON input')),
    })
    const { store } = renderPane('pane-unreadable')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'maybe it landed' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(errorsIn(store, 'pane-unreadable')).toHaveLength(0)
    expect((box as HTMLTextAreaElement).value).toBe('')
  })

  it('falls back to the generic string when the transport rejects', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('offline'))
    const { store } = renderPane('pane-generic')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'no body to read' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    // No response means no server reason, so the connectivity copy is correct here.
    await waitFor(() => expect(errorsIn(store, 'pane-generic')).toHaveLength(1))
    expect(errorsIn(store, 'pane-generic')[0].content.trim().length).toBeGreaterThan(0)
  })

  it('reports a REFUSED question-card answer instead of losing it (#4217)', async () => {
    // The card clears the instant the user submits, so this is the one send in
    // the pane whose payload nothing else carries. A 200 answering `{ok:false}`
    // used to pass a status-only check as a success: the answer vanished and the
    // agent kept waiting, with nothing on screen saying either.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true, json: () => Promise.resolve({ ok: false, error: 'slot is stopping' }),
    })
    const { store } = renderPane('pane-ask')
    act(() => {
      store.dispatch(setQuestionCard({
        slot: 'pane-ask',
        card_id: 'delivery-1',
        questions: [{ question: 'Pick a trust model', options: [{ label: 'Public only' }] }],
      }))
    })
    fireEvent.click(await screen.findByText('Public only'))
    fireEvent.click(screen.getByText('Submit'))

    await waitFor(() => expect(errorsIn(store, 'pane-ask')).toHaveLength(1))
    expect(errorsIn(store, 'pane-ask')[0].content).toBe('slot is stopping')
    // ...and the answer comes back so it can be sent again.
    const box = (await screen.findAllByRole('textbox'))[0] as HTMLTextAreaElement
    await waitFor(() => expect(box.value).toBe('Public only'))
  })

  it('recovers a cleared question-card answer when its receipt is late', async () => {
    // The transport normalizes AbortError to response-late. Unlike the normal
    // composer path, the card has already removed the only visible copy of the
    // answer, so this caller deliberately restores it for the user to inspect.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new DOMException('The operation was aborted.', 'AbortError'),
    )
    const { store } = renderPane('pane-ask-late')
    act(() => {
      store.dispatch(setQuestionCard({
        slot: 'pane-ask-late',
        card_id: 'delivery-late',
        questions: [{ question: 'Pick a trust model', options: [{ label: 'Public only' }] }],
      }))
    })
    fireEvent.click(await screen.findByText('Public only'))
    fireEvent.click(screen.getByText('Submit'))

    await waitFor(() => expect(errorsIn(store, 'pane-ask-late')).toHaveLength(1))
    const box = (await screen.findAllByRole('textbox'))[0] as HTMLTextAreaElement
    await waitFor(() => expect(box.value).toBe('Public only'))
  })

  it('passes an abort signal so a hung send cannot sit silent', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true, json: () => Promise.resolve({ ok: true }),
    })
    renderPane('pane-abort')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'might hang' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    // A hung POST settles neither way, so without a bound the message sits on
    // screen looking sent until the browser's own network timeout. `ChatPage`
    // has always passed one; the pane now does too.
    const signal = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0][3]
    expect(signal).toBeInstanceOf(AbortSignal)
  })

  it('does NOT report an abort — the request was received, only the reply is late', async () => {
    // The 10s bound stops waiting on the response; it does not mean the send
    // failed. Reporting it would hand the payload back and invite a retry that
    // duplicates a turn already running, with its side effects. `ChatPage`
    // records the same rule at its own timeout.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new DOMException('The operation was aborted.', 'AbortError'),
    )
    const { store } = renderPane('pane-aborted')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'slow to answer' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(errorsIn(store, 'pane-aborted')).toHaveLength(0)
    // The composer stays clear: the message is on its way, not recoverable work.
    expect((box as HTMLTextAreaElement).value).toBe('')
  })

  it('reports an attachment-only send the backend refuses for its empty wire text', async () => {
    // The server refuses an empty wire text above every dispatch branch, so a
    // file-only send comes back 400 `message_required`. The pane must surface
    // that refusal: nothing else carries the attachment once the composer clears.
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: ['/tmp/report.pdf'] })
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false, status: 400, json: () => Promise.resolve({ error: 'message is required', code: 'message_required' }),
    })
    const { store, container } = renderPane('pane-dropped')
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['x'], 'report.pdf', { type: 'application/pdf' })
    Object.defineProperty(fileInput, 'files', { value: [file] })
    fireEvent.change(fileInput)
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())

    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    // Wire text is empty for a file-only send, which is exactly what the server
    // refuses.
    expect((api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0][0]).toBe('')
    await waitFor(() => expect(errorsIn(store, 'pane-dropped')).toHaveLength(1))
  })

  it('does NOT report a queued send that carried wire text', async () => {
    // The control for the guard above: a real queued message owns its own
    // `queue_push` card, so treating every `queued` as a drop would cry wolf on
    // the ordinary busy-slot path.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true, json: () => Promise.resolve({ ok: true, queued: true }),
    })
    const { store } = renderPane('pane-queued')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'wait your turn' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(errorsIn(store, 'pane-queued')).toHaveLength(0)
    expect((box as HTMLTextAreaElement).value).toBe('')
  })

  it('reports nothing when the server accepts the send', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true, json: () => Promise.resolve({ ok: true }),
    })
    const { store } = renderPane('pane-ok')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'this one landed' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(errorsIn(store, 'pane-ok')).toHaveLength(0)
    expect((box as HTMLTextAreaElement).value).toBe('')
  })

  it('appends the failed payload below a message typed while the send was in flight', async () => {
    let reject: (e: Error) => void = () => {}
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockReturnValueOnce(
      new Promise((_res, rej) => { reject = rej }),
    )
    renderPane('pane-merge')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'the failing one' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    // The user starts a fresh message before the POST settles. NEITHER payload
    // may win: preferring the newer one silently discards the message the error
    // row is telling the user to try again, and preferring the older one loses
    // work they just did.
    fireEvent.change(box, { target: { value: 'newer work' } })
    reject(new Error('offline'))

    await waitFor(() =>
      expect((box as HTMLTextAreaElement).value).toBe('newer work\n\nthe failing one'),
    )
  })

  it('does not duplicate the failed text when the composer already holds it', async () => {
    let reject: (e: Error) => void = () => {}
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockReturnValueOnce(
      new Promise((_res, rej) => { reject = rej }),
    )
    renderPane('pane-dup')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'same text' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    // Retyping the same message while the first attempt is in flight is the
    // common recovery reflex; it must not come back doubled.
    fireEvent.change(box, { target: { value: 'same text' } })
    reject(new Error('offline'))

    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('same text'))
  })
})

describe('ChatPane file drop', () => {
  it('shows the pane overlay and uploads a dropped file exactly once', async () => {
    renderPane('pane-drop')
    const box = (await screen.findAllByRole('textbox'))[0]
    const file = new File(['hello'], 'hello.txt', { type: 'text/plain' })
    const dataTransfer = {
      types: ['Files'],
      items: [{
        kind: 'file',
        type: file.type,
        getAsFile: () => file,
        webkitGetAsEntry: () => ({ isDirectory: false }),
      }],
      files: [file],
      dropEffect: 'none',
    } as unknown as DataTransfer

    fireEvent.dragEnter(box, { dataTransfer })
    expect(screen.getByTestId('chat-drop-overlay')).toBeInTheDocument()

    fireEvent.drop(box, { dataTransfer })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
    await waitFor(() => {
      expect(screen.queryByTestId('chat-drop-overlay')).not.toBeInTheDocument()
    })
  })
})
