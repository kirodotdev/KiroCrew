import { describe, it, expect, vi, beforeEach } from 'vitest'
import { i18nT } from '../i18n/t'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { loadStagedSend, loadPaneRecovery } from '../utils/chatPaneRecovery'
import { PANE_RECOVERY_KEY, setPaneRecoveryFor, loadPaneRecoveryById, __resetPaneRecoveryForTests } from '../utils/chatPaneRecovery'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setQuestionCard, sseChatMessage, selectComposerBusy, appendSlotMessage } from '../store/chatSlice'
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

function makeStore(slotKey: string, busy = false) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        // Both busy signals: upstream seeds `subagents_running`, and `orchestrating` is what
        // selectComposerBusy reads for a durable busy slot. Seeding one only would leave the
        // other side's tests reading a slot that is not busy at all.
        slots: [{ key: slotKey, messages: 0, running: false, subagents_running: busy, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...(busy ? { orchestrating: true } : {}) }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(slotKey: string, opts: { busy?: boolean } | boolean = {}) {
  // Both call shapes: upstream's tests pass a positional boolean, this branch's pass an options
  // object. Normalising here keeps every existing call site meaning what it says.
  const busy = typeof opts === 'boolean' ? opts : opts.busy
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey, busy)
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
  localStorage.clear()
  __resetPaneRecoveryForTests()
})

describe('ChatPane busy send echo', () => {
  it.each([false, true])('keeps one user row before the reply with an early receipt: %s', async (earlyReceipt) => {
    let deliverReceipt!: (value: unknown) => void
    vi.mocked(api.sendChat).mockImplementationOnce(() => new Promise(resolve => { deliverReceipt = resolve }))
    const { store } = renderPane('pane-busy', true)
    const box = (await screen.findAllByRole('textbox'))[0]
    await waitFor(() => expect(selectComposerBusy(store.getState(), 'pane-busy')).toBe(true))
    await act(async () => {
      fireEvent.change(box, { target: { value: 'inspect @/tmp/design/ please' } })
      fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [content, , , , meta] = vi.mocked(api.sendChat).mock.calls[0]
    const receipt = { ok: true, json: async () => ({ ok: true, mid: 'm-pane-user' }) }
    if (earlyReceipt) await act(async () => deliverReceipt(receipt))
    const echo = { slot: 'pane-busy', role: 'user', content, meta: { ...meta, mid: 'm-pane-user' } }
    act(() => {
      store.dispatch(sseChatMessage(echo))
      store.dispatch(sseChatMessage({ slot: 'pane-busy', role: 'chunk', content: 'reading the folder' }))
    })
    if (!earlyReceipt) await act(async () => deliverReceipt(receipt))
    act(() => store.dispatch(sseChatMessage(echo)))
    const rows = store.getState().chat.slotMessages['pane-busy']
    expect(rows.map(m => m.role)).toEqual(['user', 'streaming'])
    expect(rows[0].content).toBe(content)
    expect(rows[0].meta?.dirs).toEqual(['/tmp/design'])
    expect(rows[0].meta?.mid).toBe('m-pane-user')
    expect(rows[0].meta?.optimistic).toBeUndefined()
  })
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
 * Dashboard sends now also emit a correlated user echo; an accepted response
 * still retires the pending state when that echo is missed.
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

  it('KEEPS retention when the transport error surfaces while offline', async () => {
    // `navigator.onLine` is read when the exception SURFACES, so it can be false on a
    // connection that dropped after the bytes went out — no proof the POST never left.
    const onLine = vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false)
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { store } = renderPane('pane-offline')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'may have left' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(userRow(store, 'pane-offline')).toBeTruthy())
    expect(userRow(store, 'pane-offline')?.meta?.pendingServerRow).toBe(true)
    onLine.mockRestore()
  })

  it('marks the retained bubble UNCONFIRMED so it stops reading as delivered', async () => {
    // Retention stays, so without this the transcript vouches for a delivery the
    // code itself calls unknown -- while the adjacent error row says it failed.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { store } = renderPane('pane-unknown')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'unknown delivery' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(userRow(store, 'pane-unknown')?.meta?.deliveryUnknown).toBe(true))
    // Retention is untouched: the marker describes the row, it does not retire it.
    expect(userRow(store, 'pane-unknown')?.meta?.pendingServerRow).toBe(true)
  })

  it('does NOT mark a bubble unconfirmed when the server explicitly refused', async () => {
    // Negative control: an explicit refusal is a known outcome, not an unknown one.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })
    const { store } = renderPane('pane-known')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'refuse me' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(userRow(store, 'pane-known')?.meta?.deliveryUnknown).toBeUndefined()
  })

  it('KEEPS retention when the response is merely lost while online', async () => {
    // Negative control for the arm above: the POST may have been accepted, and two
    // lanes signed off on preserving it, so this must NOT clear.
    const onLine = vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(true)
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { store } = renderPane('pane-lost')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'may have landed' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(userRow(store, 'pane-lost')?.meta?.pendingServerRow).toBe(true)
    onLine.mockRestore()
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
  const userRowIn = (store: ReturnType<typeof makeStore>, slot: string) =>
    (store.getState().chat.slotMessages[slot] || []).find(m => m.role === 'user')

  it('reports a rejected send and keeps the text recoverable in the bubble', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('offline'))
    const { store } = renderPane('pane-reject')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'this one never left' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(errorsIn(store, 'pane-reject')).toHaveLength(1))
    // Asserted as a non-empty error row rather than by copy: the string comes
    // from the shared catalog entry, and pinning its wording here would fail on
    // any locale and on the test env's fallback.
    expect(errorsIn(store, 'pane-reject')[0].content.trim().length).toBeGreaterThan(0)
    // The payload must survive a RELOAD, and the bubble is store-only, so it comes
    // back in the composer as well as staying on screen.
    await waitFor(() => expect(userRowIn(store, 'pane-reject')?.content).toBe('this one never left'))
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
    // The server's own reason survives — FRAMED with what happened and where
    // the text went, never bare: "check your connection" would be wrong AND
    // unactionable for a 409 the caller can actually do something about, and
    // a bare "slot is stopping" reads as the agent erroring, not as a send
    // that never went out.
    expect(errorsIn(store, 'pane-refused')[0].content).toBe("Couldn't send this message: slot is stopping. Your text is back in the composer.")
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

  it('states the cause when the transport rejects: the shared connection copy, not a bare "Send failed"', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('offline'))
    const { store } = renderPane('pane-generic')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'no body to read' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    // A rejected fetch does not prove the bytes never left, so the row states the RECEIPT fact
    // rather than urging a retry the delivery caption warns may duplicate the turn.
    await waitFor(() => expect(errorsIn(store, 'pane-generic')).toHaveLength(1))
    expect(errorsIn(store, 'pane-generic')[0].content).toBe(i18nT('pages.chatPage.send_no_response'))
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
    expect(errorsIn(store, 'pane-ask')[0].content).toBe("Couldn't send this message: slot is stopping. Your text is back in the composer.")
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
    // The text IS handed back though: abort is the weakest delivery state, and the
    // bubble is store-only, so clearing would leave a reload with no copy at all.
    expect((box as HTMLTextAreaElement).value).toBe('slow to answer')
  })

  it('does NOT retire a pane resend because an EARLIER identical send was confirmed', async () => {
    // Same defect as the page: a confirmed row with matching CONTENT is not the
    // send that was staged back, so identity is the only safe comparison.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new DOMException('The operation was aborted.', 'AbortError'),
    )
    const { store } = renderPane('pane-idsend')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'continue' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('continue'))

    await act(async () => {
      store.dispatch(appendSlotMessage({
        slot: 'pane-idsend',
        message: {
          role: 'user', content: 'continue',
          meta: { deliveryConfirmed: true, confirmedSendId: 'earlier-send' },
        },
      } as unknown as Parameters<typeof appendSlotMessage>[0]))
    })

    expect((box as HTMLTextAreaElement).value).toBe('continue')
  })

  it('KEEPS the restored ATTACHMENTS staged once a receipt confirms the send', async () => {
    // The text retires on confirmation, but a file is a bare path: retiring by path
    // would delete an attachment the user re-picked, so the chip must survive.
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: ['/tmp/report.pdf'] })
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }),
    })
    const { store, container } = renderPane('pane-refile')
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [new File(['x'], 'report.pdf', { type: 'application/pdf' })] })
    fireEvent.change(fileInput)
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())

    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'with a file' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    // Scoped OUTSIDE the transcript: the retained bubble renders the same filename, so a
    // document-wide query matches twice and asserts nothing about the chip this test is about.
    const chips = () => screen.queryAllByText(/report\.pdf/).filter(el => !el.closest('.chat-container'))
    await waitFor(() => expect(chips()).toHaveLength(1))

    const meta = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0][4] as { sendId: string }
    expect(typeof meta.sendId).toBe('string')
    act(() => {
      store.dispatch(appendSlotMessage({
        slot: 'pane-refile', message: { role: 'user', content: 'with a file', cls: '', ts: '2026-08-31T10:00:00.000Z',
          meta: { deliveryConfirmed: true, confirmedSendId: meta.sendId } },
      } as unknown as Parameters<typeof appendSlotMessage>[0]))
    })

    // A REFUSED send arms no caption: nothing was sent, so a retry is safe and claiming
    // a duplicate risk would discourage it. The chip still survives.
    expect(screen.queryByText(/send it twice|would deliver a duplicate/i)).toBeNull()
    expect(chips()).toHaveLength(1)
  })

  it('reports an attachment-only send the backend refuses', async () => {
    // The pane must surface a refusal of a file-only send: nothing else
    // carries the attachment once the composer clears. The wire text is no
    // longer empty for such a send (it carries the `[attached_file N]` marker,
    // as ChatPage's always has), so the refusal here stands in for any server
    // rejection rather than the old `message_required` for an empty text.
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: ['/tmp/report.pdf'] })
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false, status: 400, json: () => Promise.resolve({ error: 'refused', code: 'refused' }),
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
    // A file-only send carries its marker on the wire (ChatPage parity), so
    // the agent can resolve the path and the server has a message to accept.
    expect((api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0][0]).toBe('[attached_file 1] /tmp/report.pdf')
    await waitFor(() => expect(errorsIn(store, 'pane-dropped')).toHaveLength(1))
  })

  it('does NOT report a queued send that carried wire text', async () => {
    // A real queued message owns its own `queue_push` card, so the ordinary
    // busy-slot path must stay silent rather than reporting a drop.
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
    // Driven by an explicit REFUSAL, the arm that still hands text back. A transport
    // error no longer restores: the retained bubble holds that text (see below).
    let resolve: (v: unknown) => void = () => {}
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockReturnValueOnce(
      new Promise((res) => { resolve = res }),
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
    resolve({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })

    await waitFor(() =>
      expect((box as HTMLTextAreaElement).value).toBe('newer work\n\nthe failing one'),
    )
  })

  it('does not duplicate the failed text when the composer already holds it', async () => {
    let resolve: (v: unknown) => void = () => {}
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockReturnValueOnce(
      new Promise((res) => { resolve = res }),
    )
    renderPane('pane-dup')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'same text' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    // Retyping the same message while the first attempt is in flight is the
    // common recovery reflex; it must not come back doubled.
    fireEvent.change(box, { target: { value: 'same text' } })
    resolve({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })

    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('same text'))
  })

  it('hands a BUSY send back to the composer, since no bubble retained it', async () => {
    // A busy send appends NO optimistic bubble, so markDeliveryUnknown marks nothing
    // and the composer is the only surviving copy. Suppressing the restore lost it.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { store } = renderPane('pane-busy', { busy: true })
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'typed while busy' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    // Precondition: the busy gate really did suppress the bubble, so this cannot
    // pass by the retained-bubble route instead.
    expect(userRowIn(store, 'pane-busy')).toBeUndefined()
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('typed while busy'))
  })

  it('marks a timed-out send unconfirmed instead of leaving it looking delivered', async () => {
    // The abort fires before any receipt, so a row left unmarked reads as delivered
    // and a refetch preserves it as a phantom prompt indefinitely.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new DOMException('The operation was aborted.', 'AbortError'),
    )
    const { store } = renderPane('pane-abort')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'stalled send' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(userRowIn(store, 'pane-abort')?.meta?.deliveryUnknown).toBe(true))
    // Retention must survive: an abort is no proof the POST never left.
    expect(userRowIn(store, 'pane-abort')?.meta?.pendingServerRow).toBe(true)
  })

  it('hands a BUSY send back to the composer when the POST ABORTS', async () => {
    // The blocking case: busy means no bubble, so markDeliveryUnknown marks nothing
    // and the composer -- cleared before the POST -- held the only copy.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new DOMException('The operation was aborted.', 'AbortError'),
    )
    const { store } = renderPane('pane-busy-abort', { busy: true })
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'lost on abort' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    // Precondition: the busy gate really suppressed the bubble, so this cannot pass
    // by the retained-bubble route instead.
    expect(userRowIn(store, 'pane-busy-abort')).toBeUndefined()
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('lost on abort'))
  })

  it('does not print the delivery caption text in the error row as well', async () => {
    // The caption owns the delivery state; a row repeating it reads as the same
    // warning twice and keeps asserting it after a receipt retires the caption.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { store } = renderPane('pane-nodup')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'only once please' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(errorsIn(store, 'pane-nodup')).toHaveLength(1))
    const caption = i18nT('pages.chatPage.delivery_unconfirmed_resend') as string
    expect(errorsIn(store, 'pane-nodup')[0].content).not.toBe(caption)
    // Still says something, so the failure is not silent.
    expect(errorsIn(store, 'pane-nodup')[0].content.trim().length).toBeGreaterThan(0)
  })

  it('RESTORES the composer on an unknown delivery, so a reload cannot lose it', async () => {
    // The bubble is store-only and this send never reached the server, so the
    // persisted draft is the only copy that can outlive a reload.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { store } = renderPane('pane-nofill')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'unknown delivery text' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(userRowIn(store, 'pane-nofill')?.meta?.deliveryUnknown).toBe(true))
    // Both halves: still shown on screen AND recoverable after a reload.
    expect(userRowIn(store, 'pane-nofill')?.content).toBe('unknown delivery text')
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('unknown delivery text'))
  })

  it('STILL restores the composer when the server explicitly refused', async () => {
    // The refusal arm, which restores for a different reason: nothing was sent, so
    // the row is un-retained and a resend cannot duplicate a delivered turn.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })
    const { store } = renderPane('pane-refill')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'never left the client' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('never left the client'))
    expect(userRowIn(store, 'pane-refill')?.meta?.pendingServerRow).toBe(false)
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

describe('a pane confirmation retires the recovered payload ONLY', () => {
  it('keeps text typed WHILE the send was in flight', async () => {
    // The restore MERGES, so the staged copy is not purely the failed payload --
    // clearing it whole took the message the user wrote while the POST was open.
    let settle: (v: unknown) => void = () => {}
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise(res => { settle = res }))
    const { store } = renderPane('pane-merge')
    const box = (await screen.findAllByRole('textbox'))[0] as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'the payload that failed' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))

    // The POST is still open; the user writes a fresh message in that window.
    fireEvent.change(box, { target: { value: 'work typed during the post' } })
    await act(async () => {
      settle({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })
      await Promise.resolve()
    })

    // Premise: both are in the composer now.
    await waitFor(() => expect(box.value).toContain('the payload that failed'))
    expect(box.value).toContain('work typed during the post')

    const meta = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0][4] as { sendId: string }
    act(() => {
      store.dispatch(appendSlotMessage({
        slot: 'pane-merge', message: { role: 'user', content: 'the payload that failed', cls: '', ts: '2026-08-31T10:00:00.000Z',
          meta: { deliveryConfirmed: true, confirmedSendId: meta.sendId } },
      } as unknown as Parameters<typeof appendSlotMessage>[0]))
    })

    // Refused, so no duplicate-resend caption -- and nothing was cleared either.
    expect(screen.queryByText(/send it twice|would deliver a duplicate/i)).toBeNull()
    expect(box.value).toContain('the payload that failed')
    expect(box.value).toContain('work typed during the post')
  })
})

describe('a pane draft that merely LOOKS like the recovered payload is still the user\'s', () => {
  it('keeps text the user retyped to the same words while the send was open', async () => {
    let rejectSend: (e: unknown) => void = () => {}
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }),
    )
    const { store } = renderPane('pane-retype')
    const box = () => screen.getAllByRole('textbox')[0] as HTMLTextAreaElement
    fireEvent.change(box(), { target: { value: 'say it twice' } })
    fireEvent.keyDown(box(), { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))

    // The send emptied the composer; the user then types their own fresh draft,
    // which happens to read exactly like the payload still in flight.
    await waitFor(() => expect(box().value).toBe(''))
    await act(async () => { fireEvent.change(box(), { target: { value: 'say it twice' } }) })
    await act(async () => { rejectSend(new TypeError('Failed to fetch')); await Promise.resolve() })
    await waitFor(() => expect(box().value).toBe('say it twice'))

    const meta = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0][4] as { sendId: string }
    expect(typeof meta.sendId).toBe('string')
    act(() => {
      store.dispatch(appendSlotMessage({
        slot: 'pane-retype',
        message: { role: 'user', content: 'say it twice', cls: '', ts: '2026-09-01T10:00:00.000Z',
          meta: { deliveryConfirmed: true, confirmedSendId: meta.sendId } },
      } as unknown as Parameters<typeof appendSlotMessage>[0]))
    })
    await act(async () => { await Promise.resolve() })

    expect(box().value).toBe('say it twice')
  })
})

describe('a pane edit UNDONE back to the same words is still the user\'s own draft', () => {
  it('keeps a draft edited and undone before any confirmation arrived', async () => {
    let rejectSend: (e: unknown) => void = () => {}
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }),
    )
    const { store } = renderPane('pane-undo')
    const box = () => screen.getAllByRole('textbox')[0] as HTMLTextAreaElement
    fireEvent.change(box(), { target: { value: 'deliberately kept' } })
    fireEvent.keyDown(box(), { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    await act(async () => { rejectSend(new TypeError('Failed to fetch')); await Promise.resolve() })
    await waitFor(() => expect(box().value).toBe('deliberately kept'))
    const staged = box().value

    // Edit then undo, with no store update in between for the effect to react to.
    await act(async () => { fireEvent.change(box(), { target: { value: 'deliberately kept plus more' } }) })
    await act(async () => { fireEvent.change(box(), { target: { value: staged } }) })

    const meta = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0][4] as { sendId: string }
    act(() => {
      store.dispatch(appendSlotMessage({
        slot: 'pane-undo',
        message: { role: 'user', content: 'deliberately kept', cls: '', ts: '2026-09-01T13:00:00.000Z',
          meta: { deliveryConfirmed: true, confirmedSendId: meta.sendId } },
      } as unknown as Parameters<typeof appendSlotMessage>[0]))
    })
    await act(async () => { await Promise.resolve() })

    expect(box().value).toBe(staged)
  })
})

describe('releasing the duplicate warning outlives a reload', () => {
  afterEach(() => { localStorage.clear() })

  it('persists mayDuplicate false when an edit releases the warning', async () => {
    const SLOT = 'pane-release-on-edit'
    localStorage.clear()
    setPaneRecoveryFor(SLOT, {
      text: 'the stranded prompt', files: [], sendId: 's-rel', sent: 'the stranded prompt',
      mayDuplicate: true,
    })
    expect(loadPaneRecoveryById(SLOT, 's-rel')?.mayDuplicate,
      'premise: the record parked with the warning ARMED').toBe(true)

    renderPane(SLOT)
    const box = () => screen.getAllByRole('textbox')[0] as HTMLTextAreaElement
    await waitFor(() => expect(box().value).toContain('the stranded prompt'))

    // The user edits the restored text away, which releases the warning in the live pane.
    fireEvent.change(box(), { target: { value: 'a completely different prompt' } })

    await waitFor(() => {
      expect(loadPaneRecoveryById(SLOT, 's-rel')?.mayDuplicate,
        'a reload re-arms on `mayDuplicate !== false`, so only an explicit false releases it')
        .toBe(false)
    })
    expect(loadPaneRecoveryById(SLOT, 's-rel')?.text,
      'releasing the warning must not discard the prompt itself').toBeTruthy()
  })
})

describe('an off-screen recovery keeps the duplicate-send state, not just the words', () => {
  it('persists sendId and mayDuplicate when the pane is gone before the send resolves', async () => {
    let rejectSend: (e: unknown) => void = () => {}
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }),
    )
    const pane = renderPane('pane-offscreen')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'may already have been sent' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const meta = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0][4] as { sendId: string }

    // The pane is no longer showing this slot, so the recovery takes the off-screen guard.
    pane.unmount()
    // A response that never arrived: the send may or may not have reached the server.
    await act(async () => { rejectSend(new TypeError('Failed to fetch')) })

    const held = loadPaneRecoveryById('pane-offscreen', meta.sendId)
    expect(held?.text,
      'the off-screen guard must persist the send, not merely merge a draft')
      .toContain('may already have been sent')
    // Without this the pane comes back with the words and NO warning, one Enter from a duplicate turn.
    expect(held?.mayDuplicate,
      'a recovery that drops mayDuplicate restores an unwarned duplicate send')
      .toBe(true)
  })
})

describe('an echo that confirms BEFORE the abort restores still hands the payload back', () => {
  it('restores the payload intact rather than dropping it as already-delivered', async () => {
    let rejectSend: (e: unknown) => void = () => {}
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }),
    )
    const { store } = renderPane('pane-echofirst')
    const box = () => screen.getAllByRole('textbox')[0] as HTMLTextAreaElement
    fireEvent.change(box(), { target: { value: 'delivered already' } })
    fireEvent.keyDown(box(), { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const meta = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0][4] as { sendId: string }

    // Seed the slot FIRST: appendSlotMessage no-ops on a slot the store has never seen,
    // so without this the echo below never reaches the pane and the test is vacuous.
    act(() => {
      store.dispatch(appendSlotMessage({
        slot: 'pane-echofirst',
        message: { role: 'assistant', content: 'earlier turn', cls: '', ts: '2026-09-01T15:00:00.000Z' },
      } as unknown as Parameters<typeof appendSlotMessage>[0]))
    })
    // The server echo lands FIRST, while nothing is staged yet: the retire effect
    // runs here and finds no staged send, so this is its only allMessages change.
    act(() => {
      store.dispatch(appendSlotMessage({
        slot: 'pane-echofirst',
        message: { role: 'user', content: 'delivered already', cls: '', ts: '2026-09-01T16:00:00.000Z',
          meta: { deliveryConfirmed: true, confirmedSendId: meta.sendId } },
      } as unknown as Parameters<typeof appendSlotMessage>[0]))
    })
    // Only THEN does the transport failure restore the payload into the composer.
    await act(async () => { rejectSend(new DOMException('The operation was aborted.', 'AbortError')); await Promise.resolve() })
    await act(async () => { await Promise.resolve() })

    // The new contract: nothing is auto-cleared, so the payload is handed back intact even
    // though the echo already confirmed. Warning RELEASE is covered by the receipt tests.
    expect(box().value).toContain('delivered already')
  })
})

describe('an aborted send never restores into a different member composer', () => {
  it('leaves the new member composer untouched and keeps the marker for the sending slot', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-member-a')
    const view = render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey="pane-member-a" />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )

    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'typed for member A' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())

    // The pane switches to a DIFFERENT member while A's send is still in flight.
    view.rerender(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey="pane-member-b" />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })

    // B's composer must not receive A's payload.
    expect(box().value).not.toContain('typed for member A')
    // And the pane marker must not have been persisted: this pane's composer text is NOT
    // persisted, so a stored marker would caption an empty composer after a reload.
    expect(loadStagedSend('pane-member-a')).toBeUndefined()
  })

  it('does not carry a payload restored ON SCREEN into the next member composer', async () => {
    // The sibling above aborts AFTER the switch, which the early return already covers. Here
    // the abort lands while A is shown, so the restore SUCCEEDS and must not outlive the slot.
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-hold-a')
    const tree = (slot: string) => (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider><MemoryRouter><ChatPane slotKey={slot} /></MemoryRouter></ThemeProvider>
        </QueryClientProvider>
      </Provider>
    )
    const view = render(tree('pane-hold-a'))
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'only for member A' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())

    // Abort while A is STILL on screen, so the payload lands back in A's own composer.
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(box().value).toContain('only for member A'))

    // The pane now shows a different member: A's payload must not be addressable to B.
    view.rerender(tree('pane-hold-b'))
    await waitFor(() => expect(box().value).not.toContain('only for member A'))

    // Parked rather than destroyed -- returning to the sending member hands it back.
    view.rerender(tree('pane-hold-a'))
    await waitFor(() => expect(box().value).toContain('only for member A'))
  })

  it('leaves an UNSENT draft in place across a member switch', async () => {
    // Negative control: the guard must key on a RESTORED payload, so a switch that fires it
    // unconditionally would destroy an ordinary draft the user is still writing.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-draft-a')
    const tree = (slot: string) => (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider><MemoryRouter><ChatPane slotKey={slot} /></MemoryRouter></ThemeProvider>
        </QueryClientProvider>
      </Provider>
    )
    const view = render(tree('pane-draft-a'))
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'still writing this' } }) })

    view.rerender(tree('pane-draft-b'))
    // Drafts are PER MEMBER, so B's composer is its own and must not show A's text. The negative
    // control still holds the same way it always did -- the draft was not DESTROYED, it was parked
    // for A -- which is asserted by switching back rather than by reading it out of B's box.
    expect(box().value).not.toContain('still writing this')

    view.rerender(tree('pane-draft-a'))
    await waitFor(() => expect(box().value).toContain('still writing this'))
  })

  it('releases the caption when the restored payload is resent', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }))
    const view = renderPane('pane-resend')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    const caption = () => Array.from(view.container.querySelectorAll('[role="status"]'))
      .filter(el => /resending/i.test(el.textContent ?? ''))
    await act(async () => { fireEvent.change(box(), { target: { value: 'resend me' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    await act(async () => {
      rejectSend?.(new DOMException('aborted', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(caption().length).toBe(1))

    // Resend the restored payload WITHOUT editing: `doSend` clears the composer directly,
    // bypassing `onComposerInput`, so only an explicit release retires the caption.
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    expect(box().value).toBe('')
    await waitFor(() => expect(caption().length).toBe(0))
  })

  // MembersPage reuses ONE unkeyed pane across members, so state survives a `slotKey`
  // change and a caption keyed only on `stagedSend` renders on the wrong member.
  it('does not show one member the caption staged by another', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-A')
    const tree = (slot: string) => (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={slot} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>
    )
    const view = render(tree('pane-A'))
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    const caption = () => Array.from(view.container.querySelectorAll('[role="status"]'))
      .filter(el => /send it twice|would deliver a duplicate/i.test(el.textContent ?? ''))

    await act(async () => { fireEvent.change(box(), { target: { value: 'member A text' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    // The failure resolves while STILL on member A, so the ownership gate does not fire.
    await act(async () => {
      rejectSend?.(new DOMException('aborted', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(caption().length).toBe(1))

    // Same pane instance, different member.
    view.rerender(tree('pane-B'))
    expect(caption().length).toBe(0)
  })

  it('never persists the pane marker for a restore it DID own', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }))
    const view = renderPane('pane-owned')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'keep me' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })

    // The payload IS restored (the pane still owns the composer)...
    await waitFor(() => expect(box().value).toContain('keep me'))
    // ...but nothing is written to the shared store, which outlives that payload.
    expect(loadStagedSend('pane-owned')).toBeUndefined()
  })
})

/* A BUSY send appends no optimistic bubble (`appendedBubble = !busy && ...`), so the
 * composer holds the only copy of the text. Switching panes while that send is in flight
 * used to drop the recovery outright: `restoreIntoComposer` is a useCallback closed over
 * the SENDING slot, so its ownership gate compared a stale `slotKey` against the live
 * `slotKeyRef.current`, took the early return, and the payload existed nowhere. */
describe('ChatPane send — a busy send that fails after a slot switch', () => {
  function twoSlotStore(busySlot: string, otherSlot: string) {
    return configureStore({
      reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
      preloadedState: {
        dashboard: {
          status: null, connected: true,
          slots: [
            { key: busySlot, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, orchestrating: true },
            { key: otherSlot, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
          ],
          unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
          subagentRunning: {}, subagentDetails: {}, subagentText: {},
        } as unknown as RootState['dashboard'],
      } as Partial<RootState>,
    })
  }

  it('hands the stranded payload back when the sending slot is re-entered', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = twoSlotStore('pane-send', 'pane-other')
    let failSend: () => void = () => {}
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise((_resolve, reject) => { failSend = () => reject(new Error('network down')) }),
    )
    const tree = (slot: string) => (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider><MemoryRouter><ChatPane slotKey={slot} /></MemoryRouter></ThemeProvider>
        </QueryClientProvider>
      </Provider>
    )
    const view = render(tree('pane-send'))
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'stranded busy payload' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))

    // Premise: busy, so NO optimistic bubble carries the text. If this ever appends one the
    // test stops covering the loss it exists for, so it is asserted rather than assumed.
    expect(store.getState().chat.slotMessages['pane-send']?.some(
      m => m.role === 'user' && (m.content || '').includes('stranded busy payload'))).toBeFalsy()

    // The user leaves the pane while the POST is still in flight, then it fails.
    view.rerender(tree('pane-other'))
    await act(async () => { failSend(); await Promise.resolve(); await Promise.resolve() })

    // Coming back must hand the payload to the composer that owns it.
    view.rerender(tree('pane-send'))
    const back = (await screen.findAllByRole('textbox'))[0]
    await waitFor(() => expect((back as HTMLTextAreaElement).value).toContain('stranded busy payload'))
  })

  it('does not leak the stranded payload into a different slot', async () => {
    // Negative control: a stash keyed on the wrong slot, or applied unconditionally, would
    // paste one pane's failed send into another pane's composer.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = twoSlotStore('pane-owner', 'pane-bystander')
    let failSend: () => void = () => {}
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise((_resolve, reject) => { failSend = () => reject(new Error('network down')) }),
    )
    const tree = (slot: string) => (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider><MemoryRouter><ChatPane slotKey={slot} /></MemoryRouter></ThemeProvider>
        </QueryClientProvider>
      </Provider>
    )
    const view = render(tree('pane-owner'))
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'owner only text' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))

    view.rerender(tree('pane-bystander'))
    await act(async () => { failSend(); await Promise.resolve(); await Promise.resolve() })

    const bystander = (await screen.findAllByRole('textbox'))[0]
    expect((bystander as HTMLTextAreaElement).value).not.toContain('owner only text')
  })
})

/* A POST that times out before reaching the gateway leaves the recovered payload as the
 * ONLY copy of what the user typed: the composer was cleared at send time and the
 * optimistic bubble is store-only. It used to live in a component ref, so a reload
 * destroyed it. These remount a fresh pane, which is that reload for component state. */
describe('ChatPane send — a timed-out send survives a remount', () => {
  it('hands the payload back to a freshly mounted pane', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }))
    const view = renderPane('pane-reload')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'the only copy' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(box().value).toContain('the only copy'))
    // The persisted store is what a reload reads, so it must already hold the payload.
    await waitFor(() => expect(loadPaneRecovery('pane-reload')?.text).toContain('the only copy'))

    view.unmount()
    const fresh = renderPane('pane-reload')
    // Hydration must READ the record, not consume it. Checked before the mirror can rewrite it,
    // because until then the store is still the only copy and a reload moments later finds none.
    expect(loadPaneRecovery('pane-reload'),
      'hydration must not consume the persisted record').toBeTruthy()
    const freshBox = () => fresh.container.querySelector('textarea') as HTMLTextAreaElement
    await waitFor(() => expect(freshBox().value).toContain('the only copy'))

    // And a SECOND reload still finds it, so the first is not the last that can produce the text.
    fresh.unmount()
    const third = renderPane('pane-reload')
    const thirdBox = () => third.container.querySelector('textarea') as HTMLTextAreaElement
    await waitFor(() => expect(thirdBox().value, 'a second reload must still find the copy')
      .toContain('the only copy'))
  })

  it('rewrites the persisted copy when the user reworks it', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }))
    const view = renderPane('pane-edited')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'first words' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(loadPaneRecovery('pane-edited')).toBeTruthy())

    // An edit UPDATES the copy rather than dropping it: deleting on a keystroke would make the
    // store's only record disappear while the composer is still the sole place the text lives.
    await act(async () => { fireEvent.change(box(), { target: { value: 'reworked' } }) })
    await waitFor(() => expect(loadPaneRecovery('pane-edited')?.text).toBe('reworked'))
  })
})

/* Two ways the persisted recovery could still lose the only copy: storing the restored
 * FRAGMENT while the composer holds it merged with text typed mid-flight, and clearing the
 * record at send DISPATCH so a resend that never lands leaves nothing behind. */
describe('ChatPane send — the persisted recovery is the whole copy, and outlives a resend', () => {
  it('persists the composer text MERGED with what was typed mid-flight', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }))
    const view = renderPane('pane-merged')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'went out' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())

    // Typed WHILE the POST is pending, so the restore merges rather than replaces.
    await act(async () => { fireEvent.change(box(), { target: { value: 'typed during' } }) })
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })

    await waitFor(() => expect(loadPaneRecovery('pane-merged')?.text).toContain('went out'))
    // The half a fragment-only record would drop.
    expect(loadPaneRecovery('pane-merged')?.text, 'the mid-flight text must survive too')
      .toContain('typed during')
  })

  it('keeps the record while a resend is still in flight', async () => {
    let rejectFirst: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectFirst = rej }))
    const view = renderPane('pane-resend')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'try once' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    await act(async () => {
      rejectFirst?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(loadPaneRecovery('pane-resend')).toBeTruthy())

    // Resend, and leave the POST hanging: the composer is cleared at dispatch, so the persisted
    // record is again the only copy and must NOT have been dropped with it.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise(() => {}))
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))
    expect(loadPaneRecovery('pane-resend')?.text,
      'a resend still in flight must not have discarded the recovery').toContain('try once')
  })
})


/* Three blocking losses, plus the two UX exits: a receipt clearing a draft
 * typed after it, an emptied composer resurrecting on reload, and Discard leaving the bubble. */
describe('ChatPane recovery — generation, emptying, and the Discard exit', () => {
  it('spares a draft typed while the send was still open', async () => {
    let rejectFirst: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_r, rej) => { rejectFirst = rej }))
    let resolveSend: ((v: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((res) => { resolveSend = res }))
    const view = renderPane('pane-gen')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement

    // Arm a recovery: send, have it abort, so the payload is restored and persisted.
    await act(async () => { fireEvent.change(box(), { target: { value: 'first attempt' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    await act(async () => {
      rejectFirst?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(loadPaneRecovery('pane-gen')?.text).toContain('first attempt'))

    // Resend it, then type something NEW while that POST is still open.
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))
    await act(async () => { fireEvent.change(box(), { target: { value: 'a newer draft' } }) })
    await waitFor(() => expect(loadPaneRecovery('pane-gen')?.text).toContain('a newer draft'))

    // The receipt lands for the OLD payload and must not take the newer draft with it.
    await act(async () => {
      resolveSend?.({ ok: true, json: () => Promise.resolve({ ok: true, mid: 'm-gen' }) })
      await Promise.resolve()
    })
    await waitFor(() => expect(loadPaneRecovery('pane-gen')?.text,
      'a receipt must not clear a draft written after it').toContain('a newer draft'))
  })

  it('retires the copy when the user empties the composer', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_r, rej) => { rejectSend = rej }))
    const view = renderPane('pane-emptied')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'unwanted text' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(loadPaneRecovery('pane-emptied')?.text).toContain('unwanted text'))

    // Emptying it by hand is the user rejecting the payload, so a reload must not resurrect it.
    await act(async () => { fireEvent.change(box(), { target: { value: '' } }) })
    await waitFor(() => expect(loadPaneRecovery('pane-emptied'),
      'an emptied composer must not resurrect on reload').toBeUndefined())
  })

  it('retires the unconfirmed bubble when the recovery is discarded', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_r, rej) => { rejectSend = rej }))
    const view = renderPane('pane-discard-row')
    const store = view.store
    const row = () => (store.getState().chat.slotMessages['pane-discard-row'] || [])
      .find(m => m.role === 'user')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'doubtful send' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(row()?.meta?.pendingServerRow).toBe(true))

    const discard = await screen.findByTestId('delivery-exit-discard')
    await act(async () => { fireEvent.click(discard) })
    // Retention must go with the text, or the dimmed bubble outlives the user saying no to it.
    await waitFor(() => expect(row()?.meta?.pendingServerRow,
      'discard must release the retained bubble').not.toBe(true))
  })

  /* both no-id callers of `clearPaneRecoveryFor` wiped
   * EVERY record for the slot, so one tab's discard deleted another tab's only durable copy. */
  it('leaves another tabs parked send for the same slot alone on discard', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_r, rej) => { rejectSend = rej }))
    const view = renderPane('pane-two-tabs')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'this tabs send' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(loadPaneRecovery('pane-two-tabs')).toBeTruthy())
    // A SECOND tab parks its own failed send for the same slot -- the state `setPaneRecoveryFor`
    // deliberately re-reads the store to preserve, so this is a designed shape, not an exotic one.
    setPaneRecoveryFor('pane-two-tabs', { text: 'the other tabs prompt', files: [], sendId: 's-other-tab', gen: 99 })

    const discard = await screen.findByTestId('delivery-exit-discard')
    await act(async () => { fireEvent.click(discard) })

    const survivors = [loadPaneRecoveryById('pane-two-tabs', 's-other-tab')?.text]
    expect(survivors,
      'discarding one send must not delete the other tabs only durable copy of its prompt')
      .toContain('the other tabs prompt')
  })

  /* `consumed` was the NEWEST record for the slot, so a
   * sibling tab that parked BEFORE this send became the record this receipt retired. */
  it('does not retire a sibling record parked before this send started', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      { ok: true, json: () => Promise.resolve({ ok: true }) })
    const view = renderPane('pane-preparked')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'a brand new send' } }) })
    // AFTER mount, so this pane never restored it and never armed it: another tab parks its own
    // failed send while this composer holds only freshly typed text.
    setPaneRecoveryFor('pane-preparked', { text: 'the other tabs prompt', files: [], sendId: 's-sibling', gen: 99 })

    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    await act(async () => { await Promise.resolve(); await Promise.resolve() })

    expect(loadPaneRecoveryById('pane-preparked', 's-sibling')?.text,
      'this composer restored nothing, so it consumed nothing and must retire no one')
      .toBe('the other tabs prompt')
  })

  /* a refusal restores with NO sendId, so its record landed
   * under the bare slot key, the retry's settlement matched nothing, and a reload resurrected a
   * prompt the server had by then accepted. */
  // A park on slot-leave keeps the payload but drops `sent`, which used to leave the containment
  // check no basis at all, so the first keystroke retired the warning over a live payload.
  it('keeps the delivery warning while an edited composer still holds a park with no fragment', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { store } = renderPane('pane-basis')
    const userRowIn = (st: typeof store, slot: string) =>
      (st.getState().chat.slotMessages[slot] || []).find(m => m.role === 'user')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'the unknown delivery text' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(userRowIn(store, 'pane-basis')?.meta?.pendingServerRow).toBe(true))
    const sendId = loadPaneRecovery('pane-basis')?.sendId
    expect(sendId, 'premise: the failed send parked an identified record').toBeTruthy()

    // The slot-leave park: the payload survives, the `sent` fragment does not.
    setPaneRecoveryFor('pane-basis', { text: 'the unknown delivery text', files: [], sendId: sendId! })
    expect(loadPaneRecoveryById('pane-basis', sendId!)?.sent,
      'premise: this is the no-fragment shape the finding names').toBeUndefined()

    // A fresh pane adopts that record and the user edits AROUND the payload. The SAME store: a
    // second `renderPane` builds its own, so the retire dispatch would land where nothing sees it.
    const second = renderWithStore(store, new QueryClient({ defaultOptions: { queries: { retry: false } } }), 'pane-basis')
    const box2 = second.container.querySelector('textarea') as HTMLTextAreaElement
    await waitFor(() => expect(box2.value).toContain('the unknown delivery text'))
    await act(async () => {
      fireEvent.change(box2, { target: { value: 'the unknown delivery text and one more clause' } })
    })

    expect(userRowIn(store, 'pane-basis')?.meta?.pendingServerRow,
      'the composer still holds the payload, so the warning must stand')
      .toBe(true)
  })

  // Editing one of two same-slot recoveries read the NEWEST-by-gen record for the basis, so the
  // sibling's `sent` was carried into a write keyed to this send and became the wrong Discard gate.
  it('carries THIS sends discard basis when a sibling recovery is newer', async () => {
    const SLOT = 'pane-basis-sibling'
    // This composer's OWN failed send arms first: a record parked before mount would be adopted,
    // which would make the arm the sibling's and measure the wrong record entirely.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const view = renderPane(SLOT)
    const box = view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box, { target: { value: 'mine as parked' } }) })
    await act(async () => { fireEvent.keyDown(box, { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    const armedId = await waitFor(() => {
      const id = loadPaneRecovery(SLOT)?.sendId
      expect(id).toBeTruthy()
      return id as string
    })

    // A SECOND tab now parks its own failed send for the same slot, with a higher `gen` so it wins
    // newest-by-gen -- the shape this pane's own two-tabs test already exercises.
    setPaneRecoveryFor(SLOT, {
      text: 'the siblings text', files: [], sendId: 's-sibling', gen: 9,
      sent: 'SIBLING the fragment', sentFiles: ['sibling.png'],
    })

    // Now EDIT the composer, which mirrors the armed record back to storage on the debounce.
    await act(async () => { fireEvent.change(box, { target: { value: 'mine as parked plus an edit' } }) })
    await act(async () => { await new Promise(r => setTimeout(r, DRAFT_SAVE_DEBOUNCE_MS + 120)) })

    const mine = loadPaneRecoveryById(SLOT, armedId)
    expect(mine?.sent,
      "the Discard basis must be this send's own fragment, never the newer sibling's")
      .not.toBe('SIBLING the fragment')
    expect(loadPaneRecoveryById(SLOT, 's-sibling')?.sent,
      'premise: the sibling record is present and does carry the other fragment')
      .toBe('SIBLING the fragment')
  })

  // Two tabs on one slot both refused: the id was withheld on a refusal, so both wrote the bare
  // `pane:<slot>` key and the later write overwrote the earlier tab's prompt with no copy left.
  // The slot change is a PROP change on one mounted pane, as production drives it: a remount would
  // build a fresh `strandedSends` ref, so the twin could not survive to be resurrected.
  it('does not resurrect a discarded prompt discarded under a refused persist', async () => {
    const SLOT = 'pane-discard-twin'
    const store = makeStore(SLOT)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (slotKey: string) => (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={slotKey} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>
    )
    const view = render(tree(SLOT))
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement

    // Quota exhausted with nothing to reclaim: every recovery write is REFUSED, so the only copy
    // of the payload is the pane's in-memory twin.
    const realSet = localStorage.setItem.bind(localStorage)
    const spy = vi.spyOn(localStorage, 'setItem').mockImplementation((k: string, v: string) => {
      if (k.startsWith(`${PANE_RECOVERY_KEY}:`)) {
        throw new DOMException('exceeded the quota', 'QuotaExceededError')
      }
      realSet(k, v)
    })
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await act(async () => { fireEvent.change(box(), { target: { value: 'the discarded prompt' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await act(async () => { await new Promise(r => setTimeout(r, 150)) })

    const discard = [...view.container.querySelectorAll('button')]
      .find(b => /discard/i.test(b.textContent || ''))
    expect(discard, 'premise: the discard affordance is rendered').toBeTruthy()
    await act(async () => { fireEvent.click(discard as Element) })
    spy.mockRestore()

    // Leave the slot and come back on the SAME pane instance.
    await act(async () => { view.rerender(tree('pane-elsewhere')) })
    await act(async () => { await new Promise(r => setTimeout(r, 60)) })
    await act(async () => { view.rerender(tree(SLOT)) })
    await act(async () => { await new Promise(r => setTimeout(r, 150)) })

    expect((view.container.querySelector('textarea') as HTMLTextAreaElement).value,
      'the user discarded this prompt -- returning to the slot must not restore it, re-sendable')
      .toBe('')
  })

  // An OFF-SCREEN park persisted without registering in `ownParked`, so the resend-success chain
  // clear could not reach it. The slot change is a PROP change, as production drives it.
  it('registers an off-screen park so a later delivered resend retires it', async () => {
    const SLOT = 'pane-offscreen'
    const OTHER = 'pane-offscreen-other'
    const keys = () => {
      const out: string[] = []
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i)
        if (k && k.startsWith(`${PANE_RECOVERY_KEY}:pane:${SLOT}`)) out.push(k)
      }
      return out.sort()
    }
    const store = makeStore(SLOT)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (slotKey: string) => (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={slotKey} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>
    )
    const view = render(tree(SLOT))
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement

    // A send fails with delivery UNKNOWN, so the payload is retained and restored.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await act(async () => { fireEvent.change(box(), { target: { value: 'the offscreen prompt' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await act(async () => { await new Promise(r => setTimeout(r, 150)) })

    // Leave the slot: the composer's payload parks under the slot it was typed in, OFF-SCREEN.
    await act(async () => { view.rerender(tree(OTHER)) })
    await act(async () => { await new Promise(r => setTimeout(r, 80)) })
    await act(async () => { view.rerender(tree(SLOT)) })
    await act(async () => { await new Promise(r => setTimeout(r, 150)) })
    expect(keys().length, 'premise: the off-screen park persisted a record').toBeGreaterThan(0)

    const afterFirst = keys()
    expect(afterFirst.length, 'premise: one off-screen record so far').toBe(1)

    // A SECOND failure parked off-screen under its own send id. The lane's mechanism needs two:
    // the revisit re-arms on the newest, so the consumed path alone retires only that one.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await act(async () => { fireEvent.change(box(), { target: { value: 'the offscreen prompt two' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await act(async () => { await new Promise(r => setTimeout(r, 150)) })
    await act(async () => { view.rerender(tree(OTHER)) })
    await act(async () => { await new Promise(r => setTimeout(r, 80)) })
    await act(async () => { view.rerender(tree(SLOT)) })
    await act(async () => { await new Promise(r => setTimeout(r, 150)) })

    // The resend SUCCEEDS, so every record this pane parked for that payload is superseded.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      { ok: true, json: () => Promise.resolve({ ok: true, mid: 'm-off' }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await act(async () => { await new Promise(r => setTimeout(r, 200)) })

    expect(keys(),
      'a delivered payload must leave no off-screen record to resurrect on reload')
      .toEqual([])
  })

  it('drops the in-memory twin once a persist LANDS, so a stale one cannot merge forward', async () => {
    // A quota refusal parks a twin the success path then left in place, and the slot-leave park
    // merges whatever the twin holds forward -- so text the user had REMOVED came back.
    const SLOT = 'pane-stale-twin'
    const OTHER = 'pane-stale-twin-other'
    const KEPT = 'keep this half'
    const REMOVED = 'DELETE-THIS-HALF'
    const store = makeStore(SLOT)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (slotKey: string) => (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={slotKey} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>
    )

    // A parked send the revisit arms, so the composer mirror is live for this slot.
    setPaneRecoveryFor(SLOT, { text: `${KEPT} ${REMOVED}`, files: [], sent: `${KEPT} ${REMOVED}`, sendId: 's-twin' })
    const view = render(tree(SLOT))
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await waitFor(() => expect(box().value).toContain(REMOVED))

    // The store REFUSES the next write, which is what parks an in-memory twin.
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    await act(async () => { fireEvent.change(box(), { target: { value: `${KEPT} ${REMOVED} edited` } }) })
    await act(async () => { await new Promise(r => setTimeout(r, DRAFT_SAVE_DEBOUNCE_MS + 120)) })
    spy.mockRestore()

    // The user now REMOVES half the text, and this write lands. The twin above is stale from here.
    await act(async () => { fireEvent.change(box(), { target: { value: KEPT } }) })
    await act(async () => { await new Promise(r => setTimeout(r, DRAFT_SAVE_DEBOUNCE_MS + 120)) })
    expect(loadPaneRecoveryById(SLOT, 's-twin')?.text, 'premise: the landed write holds only the kept half').toBe(KEPT)

    // Leaving the slot parks the composer, merging whatever `strandedSends` still holds.
    await act(async () => { view.rerender(tree(OTHER)) })

    const parked = loadPaneRecoveryById(SLOT, 's-twin')
    expect(parked?.text, 'a stale twin merged forward puts the removed text back').not.toContain(REMOVED)
  })

  it('does not re-arm the duplicate-risk warning after reloading an explicitly REFUSED send', async () => {
    // A refusal proves nothing was accepted, so resending CANNOT duplicate. The live path already
    // withholds the warning; only the persisted record could not say so, and a reload re-armed it.
    const SLOT = 'pane-refused-rearm'
    const store = makeStore(SLOT)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = () => (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={SLOT} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>
    )
    const caption = i18nT('pages.chatPage.delivery_unconfirmed_resend') as string
    const PROMPT = 'the refused prompt'

    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false, json: () => Promise.resolve({ error: 'refused by the server' }),
    })
    const view = render(tree())
    const box = (v: ReturnType<typeof render>) => v.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(view), { target: { value: PROMPT } }) })
    await act(async () => { fireEvent.keyDown(box(view), { key: 'Enter', code: 'Enter' }) })
    await act(async () => { await new Promise(r => setTimeout(r, 200)) })

    expect(view.container.textContent ?? '',
      'premise: the LIVE refusal path already withholds the warning').not.toContain(caption)
    expect(loadPaneRecovery(SLOT)?.text,
      'premise: the refusal parked the payload, so the reload has something to restore').toContain(PROMPT)

    // The RELOAD: a fresh component has empty refs, so the persisted record is the only copy.
    view.unmount()
    const reloaded = render(tree())
    await waitFor(() => expect(box(reloaded).value).toContain(PROMPT))

    expect(reloaded.container.textContent ?? '',
      'a refused send cannot duplicate, so no reload may re-arm the warning').not.toContain(caption)
  })

  it('shows the dismiss consequence as VISIBLE copy, never hover-only', async () => {
    // A `title` renders on neither touch nor keyboard focus, so a DESTRUCTIVE consequence parked
    // there is clicked blind -- and it is also what names WHAT the bare label removes.
    const SLOT = 'pane-dismiss-visible'
    // No `sent` fragment, so there is nothing to Discard and the DISMISS arm is the one rendered.
    setPaneRecoveryFor(SLOT, { text: 'a parked prompt', files: [], sendId: 's-visible' })
    const view = renderPane(SLOT)
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await waitFor(() => expect(box().value).toContain('a parked prompt'))

    const note = i18nT('pages.chatPage.delivery_unconfirmed_dismiss_note') as string
    expect(view.container.querySelector('[role="status"]'),
      'premise: the warning strip is armed').not.toBeNull()
    expect(view.container.textContent ?? '',
      'the consequence must be readable without hovering').toContain(note)

    const titled = [...view.container.querySelectorAll('button')]
      .find(b => b.getAttribute('title') === note)
    expect(titled, 'and must not be hover-ONLY on any control').toBeUndefined()

    // This arm DROPS the unconfirmed send, so its label must name that outcome: a bare "Dismiss"
    // reads as clearing a nag and gets swatted, taking the user's only copy of the text.
    const dropping = [...view.container.querySelectorAll('button')]
      .find(b => b.textContent?.trim() === (i18nT('pages.chatPage.delivery_remove') as string))
    expect(dropping, 'the dropping arm names what it removes').toBeTruthy()
    expect([...view.container.querySelectorAll('button')].map(b => b.textContent?.trim()),
      'and never offers the generic verb while it drops').not.toContain(i18nT('app.dismiss') as string)
  })

  it('gives each pane its OWN dismiss-note id, since the grid mounts one per leaf', async () => {
    // Two split panes each holding an unconfirmed non-discardable staged send both emitted the same
    // literal id, so the duplicate cross-wired the second pane's control to the first pane's node.
    setPaneRecoveryFor('pane-dup-a', { text: 'parked in A', files: [], sendId: 's-dup-a' })
    setPaneRecoveryFor('pane-dup-b', { text: 'parked in B', files: [], sendId: 's-dup-b' })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-dup-a')
    const view = render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey="pane-dup-a" />
              <ChatPane slotKey="pane-dup-b" />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )

    await waitFor(() => expect(
      view.container.querySelectorAll('[id^="pane-delivery-dismiss-note"]').length,
      'premise: both panes armed their warning strip').toBe(2))

    const ids = [...view.container.querySelectorAll('[id^="pane-delivery-dismiss-note"]')]
      .map(n => n.getAttribute('id'))
    expect(new Set(ids).size, 'a duplicate DOM id cross-wires the second pane to the first node')
      .toBe(ids.length)

    const described = [...view.container.querySelectorAll('[aria-describedby^="pane-delivery-dismiss-note"]')]
      .map(n => n.getAttribute('aria-describedby'))
    expect(new Set(described).size, 'and each control must point at its own note').toBe(described.length)
  })

  it('keeps both prompts when two tabs are refused on the same slot', async () => {
    // Keyed on IDENTITY, not on text: the second pane ADOPTS whatever is parked for the slot, so
    // its merged copy can contain the first prompt while the first tab's own record is gone.
    const recordKeys = () => {
      const out: string[] = []
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i)
        if (k && k.startsWith(`${PANE_RECOVERY_KEY}:pane:pane-two-refused`)) out.push(k)
      }
      return out.sort()
    }

    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      { ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })
    const first = renderPane('pane-two-refused')
    const box1 = first.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box1, { target: { value: 'the first tabs prompt' } }) })
    await act(async () => { fireEvent.keyDown(box1, { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(recordKeys().length).toBe(1))
    const firstKey = recordKeys()[0]
    expect(firstKey, 'premise: the refusal must name its send, not write the bare slot key')
      .toMatch(/\|/)

    // A SECOND tab on the same slot, refused in turn.
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      { ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })
    const second = renderPane('pane-two-refused')
    const box2 = second.container.querySelectorAll('textarea')[0] as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box2, { target: { value: 'the second tabs prompt' } }) })
    await act(async () => { fireEvent.keyDown(box2, { key: 'Enter' }) })
    await waitFor(() => expect(recordKeys().length).toBeGreaterThan(1))

    expect(recordKeys(),
      "the first tab's record must still exist: it is the only copy of that prompt")
      .toContain(firstKey)
  })

  it('retires the refusal record once the retry succeeds', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      { ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })
    const view = renderPane('pane-refuse-retry')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'refused then resent' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    // The refusal path put the payload back and persisted it WITHOUT a send id.
    await waitFor(() => expect(loadPaneRecovery('pane-refuse-retry')?.text).toContain('refused then resent'))

    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      { ok: true, json: () => Promise.resolve({ ok: true, mid: 'm-retry' }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))
    await act(async () => { await new Promise(r => setTimeout(r, 80)) })

    expect(loadPaneRecovery('pane-refuse-retry'),
      'the retry delivered it, so a reload must not resurrect the prompt as unsent')
      .toBeUndefined()
  })

  it('offers the Discard exit again after a reload', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_r, rej) => { rejectSend = rej }))
    const view = renderPane('pane-reload-exit')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'stranded words' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(loadPaneRecovery('pane-reload-exit')).toBeTruthy())

    view.unmount()
    const fresh = renderPane('pane-reload-exit')
    const freshBox = () => fresh.container.querySelector('textarea') as HTMLTextAreaElement
    await waitFor(() => expect(freshBox().value).toContain('stranded words'))
    // The caption comes back on reload, so its one-click way out has to come back with it.
    await waitFor(() => expect(fresh.container.querySelector('[data-testid="delivery-exit-discard"]'),
      'the reloaded caption needs its Discard exit').not.toBeNull())
  })
})


/* restoreIntoComposer snapshotted the MERGE as `recoveredPayload`, so the composer equalled
 * it and Discard offered to delete text the user typed while the send was still open. */
describe('ChatPane Discard — never offered over work the send did not carry', () => {
  it('hides Discard when the composer holds more than the restored send', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_r, rej) => { rejectSend = rej }))
    const view = renderPane('pane-discard-gate')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'the sent words' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())

    // Typed while the POST was still open: this text is the user's, not the send's.
    await act(async () => { fireEvent.change(box(), { target: { value: 'my own new words' } }) })
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    // The restore merges both into the composer, which is correct -- nothing is lost.
    await waitFor(() => expect(box().value).toContain('the sent words'))
    expect(box().value).toContain('my own new words')

    // But Discard must NOT be offered, because clicking it would erase the mid-flight text too.
    expect(view.container.querySelector('[data-testid="delivery-exit-discard"]'),
      'Discard must not be offered over text the send never carried').toBeNull()
  })

  it('offers Discard when the composer holds exactly the restored send', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_r, rej) => { rejectSend = rej }))
    const view = renderPane('pane-discard-ok')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'only the send' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(box().value).toContain('only the send'))
    // Nothing beyond the send is in there, so the exit is safe and must still be offered.
    await waitFor(() => expect(view.container.querySelector('[data-testid="delivery-exit-discard"]'),
      'the exit is still owed when nothing else is at stake').not.toBeNull())
  })
})


/* `onComposerInput` dropped `restoredPayload.current`, and the slot-leave park
 * is gated on it -- so an EDITED recovered draft followed the composer to the next member. */
describe('ChatPane recovery ownership survives an edit', () => {
  it('clears the composer on a member switch after the draft was edited', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_r, rej) => { rejectSend = rej }))
    const store = makeStore('member-a')
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = renderWithStore(store, qc, 'member-a')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement

    await act(async () => { fireEvent.change(box(), { target: { value: 'for member a' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(box().value).toContain('for member a'))

    // EDIT it. Ownership must survive, because the slot-leave park is gated on it.
    await act(async () => { fireEvent.change(box(), { target: { value: 'for member a, edited' } }) })

    // Show a DIFFERENT member in the same pane. The park owns the handoff, and clearing the
    // composer is its observable half -- skip it and member A's words are addressable to B.
    await act(async () => {
      view.rerender(
        <Provider store={store}>
          <QueryClientProvider client={qc}>
            <ThemeProvider>
              <MemoryRouter>
                <ChatPane slotKey="member-b" />
              </MemoryRouter>
            </ThemeProvider>
          </QueryClientProvider>
        </Provider>,
      )
    })

    await waitFor(() => expect(box().value,
      "another member's composer must not hold the edited draft").not.toContain('for member a'))
    // And it is parked under the member it was sent to, not the bystander.
    expect(loadPaneRecovery('member-a')?.text).toContain('edited')
    expect(loadPaneRecovery('member-b'),
      'the bystander member must not acquire it').toBeUndefined()
  })
})


/* the receipt retires the arm by mutating a ref, which fires no re-render and
 * so no effect cleanup -- a debounce timer pending at clear time wrote the record back after it. */
describe('ChatPane recovery — a fast receipt is not undone by a pending debounce', () => {
  it('does not resurrect the record after the receipt cleared it', async () => {
    let rejectFirst: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_r, rej) => { rejectFirst = rej }))
    const view = renderPane('pane-race')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement

    // Arm a recovery: a send that aborts, so its payload is restored and persisted.
    await act(async () => { fireEvent.change(box(), { target: { value: 'the words' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    await act(async () => {
      rejectFirst?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(loadPaneRecovery('pane-race')).toBeTruthy())

    // Resend, holding the POST open so the receipt can land AFTER the next edit.
    let resolveSecond: ((v: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((res) => { resolveSecond = res }))
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))

    // Type while that POST is open: this SCHEDULES a debounce write and does not perform one, so
    // the stored generation still matches what the send consumed.
    await act(async () => { fireEvent.change(box(), { target: { value: 'typed mid-flight' } }) })

    // The receipt lands FAST -- inside the debounce window -- so it clears the record.
    await act(async () => {
      resolveSecond?.({ ok: true, json: () => Promise.resolve({ ok: true, mid: 'm-race' }) })
      await Promise.resolve()
    })
    await waitFor(() => expect(loadPaneRecovery('pane-race')).toBeUndefined())

    // Now let the pending timer fire. It must find its arm retired and write nothing.
    await act(async () => { await new Promise(r => setTimeout(r, 450)) })
    expect(loadPaneRecovery('pane-race'),
      'a pending debounce must not resurrect a record the receipt retired').toBeUndefined()
  })
})


/* the clear was gated on `(now?.gen ?? 0) === (consumedGen ?? 0)`, and the
 * FAILURE path persists no `gen`. So a send that consumed no draft compared 0 === 0 and retired a
 * SIBLING send's recovery — after which the terminal reload had no copy of that prompt at all. */
describe('a receipt retires only the recovery its own send consumed', () => {
  it('leaves a sibling failure-path recovery (which carries no gen) alone', async () => {
    let resolveSecond: ((v: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((res) => { resolveSecond = res }))
    const view = renderPane('pane-sibling')
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement

    // A send goes out from a pane holding NO recovery, so it consumes nothing.
    await act(async () => { fireEvent.change(box(), { target: { value: 'the second send' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))

    // While that POST is open, ANOTHER send's failure lands its recovery. The failure path writes
    // no `gen`, which is the normal shape rather than an edge case.
    await act(async () => {
      setPaneRecoveryFor('pane-sibling', { text: 'a sibling prompt', files: ['sib.png'], sent: 'a sibling prompt', sendId: 's-sibling' })
    })
    expect(loadPaneRecovery('pane-sibling')).toBeTruthy()

    // The in-flight send's receipt now arrives. It consumed nothing, so it must retire nothing.
    await act(async () => {
      resolveSecond?.({ ok: true, json: () => Promise.resolve({ ok: true, mid: 'm-sibling' }) })
      await Promise.resolve()
    })
    await act(async () => { await new Promise(r => setTimeout(r, 60)) })

    expect(loadPaneRecovery('pane-sibling'),
      "a receipt must not clear a recovery its own send never consumed").toBeTruthy()
    expect(loadPaneRecovery('pane-sibling')?.text).toBe('a sibling prompt')
  })

  it('keys the late failure of a BUSY send to the issuing slot, not the rebound one', async () => {
    // Four call sites passed a SLOT into the new third POSITIONAL parameter, where a send id was
    // expected -- disarming the warning and letting `forSlot` fall back to whatever slot is live.
    let rejectSend: ((e: unknown) => void) | undefined
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((_res, rej) => { rejectSend = rej }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // BUSY, so the send mints no optimistic bubble and the late arm must hand the payload back.
    const store = makeStore('pane-issued-a', true)
    const tree = (slot: string) => (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider><MemoryRouter><ChatPane slotKey={slot} /></MemoryRouter></ThemeProvider>
        </QueryClientProvider>
      </Provider>
    )
    const view = render(tree('pane-issued-a'))
    const box = () => view.container.querySelector('textarea') as HTMLTextAreaElement

    await act(async () => { fireEvent.change(box(), { target: { value: 'words meant for A' } }) })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())

    view.rerender(tree('pane-issued-b'))
    await act(async () => {
      rejectSend?.(new DOMException('aborted', 'AbortError'))
      await Promise.resolve()
    })

    expect(box().value,
      "B's composer belongs to another conversation; A's words must not surface in it")
      .not.toContain('words meant for A')
    // The position defect itself: a SLOT reaching the `sendId` parameter stamps the recovery record
    // with a fabricated identity, so it can never be matched to the send it belongs to.
    expect(loadPaneRecoveryById('pane-issued-a', 'pane-issued-a'),
      'a slot name must never be stored as a send id')
      .toBeUndefined()
    expect(loadPaneRecoveryById('pane-issued-b', 'pane-issued-b'),
      'and the rebound slot must not acquire one either')
      .toBeUndefined()
  })
})
