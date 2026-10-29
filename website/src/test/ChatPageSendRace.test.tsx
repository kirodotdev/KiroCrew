/**
 * First submit wins, in the single-chat composer.
 *
 * A send carries the session revision this tab last saw. When the host answers
 * `send_race` (a collaborator's send was accepted first) the draft comes back
 * to the composer untouched, no bubble or error row is left behind, and a
 * notice names who sent first with Send now. Send now is a new send at the
 * current revision that never steers. An overload refusal reads differently:
 * an error row with the limit, the draft back, and no race notice.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ComponentProps, ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { store as appStore } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot, setPendingInput, sseChatMessage } from '../store/chatSlice'
import { composerTypingSignal } from '../pages/chat/shared-crew/typingSignal'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { baseRevisionFor, noteSendRevision, resetSendRaceForTests } from '../api/sendRace'
import { resetSendRevisionSeedForTests } from '../hooks/sharedCrew/sendRevisionSeed'
import { queryClient as appQueryClient } from '../api/queryClient'
import { applySharedCrewFrame, sharedCrewKeys } from '../hooks/sharedCrew/sharedCrewSocket'
import { createChatEndpoints } from '../api/client/chat'
import { api } from '../api/client'

const composerValues = vi.hoisted(() => [] as Array<string | undefined>)
vi.mock('../components/ChatInput', async importOriginal => {
  const actual = await importOriginal<typeof import('../components/ChatInput')>()
  return { ...actual, default: (props: ComponentProps<typeof actual.default>) => {
    composerValues.push(props.value)
    return <actual.default {...props} />
  } }
})

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const sendChat = vi.fn()
const sendState = vi.fn()
vi.mock('../api/client', async () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: (...a: unknown[]) => sendChat(...a),
    sharedCrewSendState: (...a: unknown[]) => sendState(...a),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    knowledgeSources: vi.fn().mockResolvedValue({ sources: [] }),
    createChatSlot: vi.fn(),
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
const voiceInput = vi.hoisted(() => ({ recording: false, transcribing: false, toggle: vi.fn() }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => voiceInput, voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

function makeStore({ running = false }: { running?: boolean } = {}) {
  // The slot detail read agrees with the preloaded state, so a running turn stays running.
  vi.mocked(api.chatSlotDetail).mockResolvedValue({ messages: [], running, has_more: false, total: 0 } as unknown as Awaited<ReturnType<typeof api.chatSlotDetail>>)
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [{ key: 'slot-a', messages: 0, running, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: running, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: '',
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderPage(store: ReturnType<typeof makeStore>, qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }), route = '/chat') {
  vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={[route]}><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

async function sendText(text: string) {
  const box = screen.getByLabelText('Message input')
  fireEvent.change(box, { target: { value: text } })
  await act(async () => { fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' }) })
}

const input = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
const userRow = (store: ReturnType<typeof makeStore>) => store.getState().chat.messages.find(m => m.role === 'user')

const WINNER = {
  revision: 4,
  principal: { id: 'p-alice', kind: 'human', display: { name: 'alice', login: 'alice@example.test', captured_at: null } },
  message_id: 'm-alice',
  accepted_at: 1,
}
const RACE = {
  ok: false,
  status: 409,
  json: () => Promise.resolve({ error: 'someone else sent first', code: 'send_race', session_key: 'slot-a', current_revision: 4, winner: WINNER }),
}
type RaceArg = { baseRevision?: number; sendNow?: boolean; idempotencyKey?: string }

beforeEach(() => {
  composerValues.length = 0
  sessionStorage.clear()
  localStorage.clear()
  sendChat.mockReset()
  vi.mocked(api.createChatSlot).mockReset()
  sendState.mockReset()
  // A gateway without the send-state route by default: the read answers nothing.
  sendState.mockResolvedValue(null)
  resetSendRaceForTests()
  resetSendRevisionSeedForTests()
})
afterEach(() => vi.restoreAllMocks())

describe('a send that loses a send race', { timeout: 20_000 }, () => {
  it('keeps the draft, names who sent first, and leaves nothing sent', async () => {
    noteSendRevision('slot-a', 3)
    sendChat.mockResolvedValue(RACE)
    const store = makeStore()
    await renderPage(store)
    await sendText('my draft')
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    const race = sendChat.mock.calls[0][7] as RaceArg
    expect(race.baseRevision).toBe(3)
    expect(race.sendNow).toBe(false)
    expect(race.idempotencyKey).toMatch(/^send-s-/)
    await waitFor(() => expect(screen.getByTestId('send-race-notice')).toBeTruthy())
    expect(screen.getByTestId('send-race-notice').textContent).toContain('alice sent first')
    expect(screen.getByTestId('send-race-notice').getAttribute('data-winner-message')).toBe('m-alice')
    expect(input().value).toBe('my draft')
    expect(userRow(store)).toBeUndefined()
    expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(false)
    expect(store.getState().chat.slotRunning).toBe(false)
    expect(baseRevisionFor('slot-a')).toBe(4)
    // Nothing is sent again on its own.
    expect(sendChat).toHaveBeenCalledTimes(1)
  })

  it.each(['send_race', 'composer_locked'])('keeps the server-confirmed turn running after a late %s refusal', async code => {
    let refuse!: (value: unknown) => void
    sendChat.mockReturnValue(new Promise(resolve => { refuse = resolve }))
    const store = makeStore()
    await renderPage(store)
    await sendText('my late draft')
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(store.getState().chat.pendingTurnSlot).toBe('slot-a')
    act(() => {
      store.dispatch(sseSlots([{ ...store.getState().dashboard.slots[0], running: true }]))
    })
    await waitFor(() => expect(store.getState().chat.pendingTurnSlot).toBeNull())
    expect(store.getState().chat.slotRunning).toBe(true)
    await act(async () => {
      refuse({ ok: false, status: 409, json: () => Promise.resolve({
        error: 'not accepted', code, session_key: 'slot-a', current_revision: 4, winner: WINNER,
        holder: WINNER.principal, lease: { session_key: 'slot-a', holder: WINNER.principal, holder_ids: ['p-alice'] },
      }) })
    })
    await waitFor(() => expect(input().value).toBe('my late draft'))
    expect(userRow(store)).toBeUndefined()
    expect(store.getState().chat.pendingTurnSlot).toBeNull()
    expect(store.getState().chat.slotRunning).toBe(true)
    expect(screen.getByTestId('busy-send-button')).toHaveAttribute('data-mode', 'steer')
  })

  it.each(['send_race', 'composer_locked'])('restores the draft after a collaborator finishes before a late %s refusal', async code => {
    let refuse!: (value: unknown) => void
    sendChat.mockReturnValue(new Promise(resolve => { refuse = resolve }))
    const store = makeStore()
    await renderPage(store)
    await sendText('B: my own unsent note')
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(input().value).toBe('')
    act(() => {
      store.dispatch(sseChatMessage({ slot: 'slot-a', role: 'user', content: 'A sent first', meta: { mid: 'm-alice', sendId: 'send-alice' } }))
      store.dispatch(sseSlots([{ ...store.getState().dashboard.slots[0], running: true }]))
    })
    act(() => {
      store.dispatch(sseChatMessage({ slot: 'slot-a', role: 'assistant', content: 'A reply' }))
      store.dispatch(sseChatMessage({ slot: 'slot-a', role: '_done', content: '' }))
      store.dispatch(sseSlots([{ ...store.getState().dashboard.slots[0], running: false }]))
    })
    await act(async () => {
      refuse({ ok: false, status: 409, json: () => Promise.resolve({
        error: 'not accepted', code, session_key: 'slot-a', current_revision: 4, winner: WINNER,
        holder: WINNER.principal, lease: { session_key: 'slot-a', holder: WINNER.principal, holder_ids: ['p-alice'] },
      }) })
    })
    await waitFor(() => expect(input().value).toBe('B: my own unsent note'))
    // The memoized editor receives a changed text prop, independently of
    // unrelated voice, query or parent updates that can also redraw it.
    expect(composerValues.at(-1)).toBe('B: my own unsent note')
    expect(store.getState().chat.messages.filter(m => m.role === 'user').map(m => m.content)).toEqual(['A sent first'])
    expect(sendChat).toHaveBeenCalledTimes(1)
  })

  it('Send now sends the kept draft at the current revision and never steers', async () => {
    sendChat.mockResolvedValueOnce(RACE)
    const store = makeStore()
    await renderPage(store)
    await sendText('my draft')
    await waitFor(() => expect(screen.getByTestId('send-race-send-now')).toBeTruthy())
    sendChat.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true, queued: true, queue_id: 'q-1' }) })
    await act(async () => { fireEvent.click(screen.getByTestId('send-race-send-now')) })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(2))
    const [message, slot, , , , steer, , race] = sendChat.mock.calls[1]
    expect(message).toBe('my draft')
    expect(slot).toBe('slot-a')
    expect(steer).toBeUndefined()
    expect(race).toMatchObject({ baseRevision: 4, sendNow: true })
    expect((race as RaceArg).idempotencyKey).not.toBe((sendChat.mock.calls[0][7] as RaceArg).idempotencyKey)
    await waitFor(() => expect(screen.queryByTestId('send-race-notice')).toBeNull())
    expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(false)
  })

  it('Dismiss hides the notice and keeps the draft', async () => {
    sendChat.mockResolvedValue(RACE)
    const store = makeStore()
    await renderPage(store)
    await sendText('keep me')
    await waitFor(() => expect(screen.getByTestId('send-race-dismiss')).toBeTruthy())
    await act(async () => { fireEvent.click(screen.getByTestId('send-race-dismiss')) })
    expect(screen.queryByTestId('send-race-notice')).toBeNull()
    expect(input().value).toBe('keep me')
    expect(sendChat).toHaveBeenCalledTimes(1)
    expect(userRow(store)).toBeUndefined()
  })

  it('an overload refusal reads as a limit, not a race', async () => {
    sendChat.mockResolvedValue({ ok: false, status: 429, json: () => Promise.resolve({ error: 'limit', code: 'principal_active_limit' }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('over the limit')
    await waitFor(() => expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(true))
    expect(screen.queryByTestId('send-race-notice')).toBeNull()
    expect(input().value).toBe('over the limit')
  })
})

describe('the send revision a send is based on', { timeout: 20_000 }, () => {
  it('is read for an open session on a Crew reporting no presence or attribution', async () => {
    sendState.mockResolvedValue({ schema_version: 1, session_key: 'slot-a', revision: 5, last: null })
    const store = makeStore()
    await renderPage(store)
    await waitFor(() => expect(baseRevisionFor('slot-a')).toBe(5))
    expect(sendState).toHaveBeenCalledWith('slot-a')
  })

  it('a first send waits for the read and carries the revision it answers', async () => {
    let answer: (v: unknown) => void = () => {}
    sendState.mockReturnValue(new Promise(resolve => { answer = resolve }))
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('first')
    expect(sendChat).not.toHaveBeenCalled()
    await act(async () => { answer({ schema_version: 1, session_key: 'slot-a', revision: 2, last: null }) })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect((sendChat.mock.calls[0][7] as RaceArg).baseRevision).toBe(2)
  })

  it('a failed read lets the send go without a base', async () => {
    sendState.mockRejectedValue(new Error('offline'))
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('still sends')
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect((sendChat.mock.calls[0][7] as RaceArg).baseRevision).toBeUndefined()
  })
})

describe('a send the host refuses for another reason', { timeout: 20_000 }, () => {
  it('keeps its pending bubble while the draft comes back', async () => {
    sendChat.mockResolvedValue({ ok: false, status: 503, json: () => Promise.resolve({ error: 'cannot run new work safely', code: 'containment_unavailable' }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('send under hard limits')
    await waitFor(() => expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(true))
    expect(input().value).toBe('send under hard limits')
    expect(userRow(store)?.meta?.optimistic).toBe(true)
  })

  it('keeps its pending bubble on a transport error while the draft comes back', async () => {
    sendChat.mockRejectedValue(new TypeError('Failed to fetch'))
    const store = makeStore()
    await renderPage(store)
    await sendText('never left')
    await waitFor(() => expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(true))
    expect(input().value).toBe('never left')
    expect(userRow(store)?.meta?.optimistic).toBe(true)
  })
})

/** `api.sendChat` as the app builds it, over a fetch that records each POST body
 *  and answers with *answer*, so a test reads what reaches the wire. */
function wireSendChat(answer: (body: Record<string, unknown>) => { status: number; body: unknown }) {
  const posts: Record<string, unknown>[] = []
  const endpoints = createChatEndpoints({
    jfetch: (_url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>
      posts.push(body)
      const a = answer(body)
      return Promise.resolve(new Response(JSON.stringify(a.body), { status: a.status, headers: { 'Content-Type': 'application/json' } }))
    },
    sessionKeyHeader: {},
    sendResponseAuthRecovery: (r: Response) => r,
  } as unknown as Parameters<typeof createChatEndpoints>[0])
  sendChat.mockImplementation((...a: unknown[]) => (endpoints.send.sendChat as (...b: unknown[]) => Promise<Response>)(...a))
  return posts
}

const RACE_BODY = { error: 'someone else sent first', code: 'send_race', session_key: 'slot-a', current_revision: 1, winner: { ...WINNER, revision: 1 } }

describe('what a send into an open session puts on the wire', { timeout: 20_000 }, () => {
  it.each([false, true])('keeps a restart conflict resolved after leaving and returning (running=%s)', async running => {
    let revision = 10
    sendState.mockImplementation((sessionKey: string) => Promise.resolve({ schema_version: 1, session_key: sessionKey, revision, last: null }))
    const posts = wireSendChat(body => body.base_revision !== revision
      ? { status: 409, body: RACE_BODY }
      : { status: 200, body: { ok: true, queued: true, queue_id: 'q-restart' } })
    const store = makeStore({ running })
    await renderPage(store, appQueryClient)
    await waitFor(() => expect(baseRevisionFor('slot-a')).toBe(10))
    revision = 1
    await sendText('my draft across restart')
    await waitFor(() => expect(screen.getByTestId('send-race-send-now')).toBeInTheDocument())
    expect(posts[0].base_revision).toBe(10)
    await act(async () => { fireEvent.click(screen.getByTestId('send-race-send-now')) })
    await waitFor(() => expect(screen.queryByTestId('send-race-notice')).toBeNull())
    expect(posts[1].base_revision).toBe(1)
    revision = 2
    act(() => {
      applySharedCrewFrame(appQueryClient, { type: 'shared_crew.send_state', schema_version: 1, session_key: 'slot-a', revision, last: null })
      store.dispatch(sseSlots([
        ...store.getState().dashboard.slots,
        { ...store.getState().dashboard.slots[0], key: 'slot-b' },
      ]))
      store.dispatch(setActiveSlot('slot-b'))
    })
    await waitFor(() => expect(sendState).toHaveBeenCalledWith('slot-b'))
    await waitFor(() => expect(baseRevisionFor('slot-b')).toBe(2))
    await act(async () => { store.dispatch(setActiveSlot('slot-a')) })
    await sendText('another draft after returning')
    await waitFor(() => expect(posts).toHaveLength(3))
    expect(posts[2].base_revision).toBe(2)
    expect(appQueryClient.getQueryData(sharedCrewKeys.sendState('slot-a'))).toMatchObject({ revision: 2 })
    await waitFor(() => expect(input().value).toBe(''))
    expect(screen.queryByTestId('send-race-notice')).toBeNull()
  })

  it('an idle session with no revision learned yet: the send carries the revision the read answers', async () => {
    sendState.mockResolvedValue({ schema_version: 1, session_key: 'slot-a', revision: 0, last: null })
    const posts = wireSendChat(() => ({ status: 200, body: { ok: true } }))
    const store = makeStore()
    await renderPage(store)
    await sendText('first from B')
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].base_revision).toBe(0)
  })

  it('a running session with no revision learned yet: the steer carries the revision too', async () => {
    sendState.mockResolvedValue({ schema_version: 1, session_key: 'slot-a', revision: 0, last: null })
    const posts = wireSendChat(() => ({ status: 200, body: { ok: true, steered: true } }))
    const store = makeStore({ running: true })
    await renderPage(store)
    await sendText('first from B')
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].steer).toBe(true)
    expect(posts[0].base_revision).toBe(0)
  })

  it('a steer that consumes the draft ends its typing lease', async () => {
    sendState.mockResolvedValue({ schema_version: 1, session_key: 'slot-a', revision: 0, last: null })
    wireSendChat(() => ({ status: 200, body: { ok: true, steered: true } }))
    const heard = vi.fn()
    const off = composerTypingSignal.subscribeSent(heard)
    try {
      await renderPage(makeStore({ running: true }))
      fireEvent.change(input(), { target: { value: 'steer from B' } })
      await act(async () => { fireEvent.click(screen.getByTestId('busy-send-button')) })
      await waitFor(() => expect(input().value).toBe(''))
      expect(heard.mock.calls).toEqual([['slot-a']])
    } finally {
      off()
    }
  })

  it('a frame for a collaborator\'s send landing after the press does not raise the base', async () => {
    noteSendRevision('slot-a', 0)
    let answerRead: (v: unknown) => void = () => {}
    // Hold the POST's start behind the send's own wait: the tab knows revision 0,
    // the person presses, then the collaborator's frame (revision 1) lands.
    const posts = wireSendChat(() => ({ status: 409, body: RACE_BODY }))
    sendState.mockReturnValue(new Promise(resolve => { answerRead = resolve }))
    const store = makeStore()
    await renderPage(store)
    const box = screen.getByLabelText('Message input')
    fireEvent.change(box, { target: { value: 'first from B' } })
    await act(async () => {
      fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
      noteSendRevision('slot-a', 1)
    })
    answerRead({ schema_version: 1, session_key: 'slot-a', revision: 1, last: null })
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].base_revision).toBe(0)
    await waitFor(() => expect(screen.getByTestId('send-race-notice')).toBeTruthy())
  })

  it.each([false, true])('keeps base_revision when a winning frame overtakes the snapshot (running=%s)', async running => {
    let answer!: (value: unknown) => void
    sendState.mockReturnValue(new Promise(resolve => { answer = resolve }))
    const posts = wireSendChat(body => body.base_revision === 0
      ? { status: 409, body: RACE_BODY }
      : { status: 200, body: { ok: true, steered: running } })
    const store = makeStore({ running })
    await renderPage(store, appQueryClient)
    await sendText('pending from B')
    expect(posts).toHaveLength(0)
    await act(async () => {
      applySharedCrewFrame(appQueryClient, { type: 'shared_crew.send_state', schema_version: 1, session_key: 'slot-a', revision: 1, last: RACE_BODY.winner })
      answer({ schema_version: 1, session_key: 'slot-a', revision: 0, last: null })
    })
    await waitFor(() => expect(screen.getByTestId('send-race-notice')).toBeInTheDocument())
    expect(posts).toHaveLength(1)
    expect(posts[0].base_revision).toBe(0)
    expect(posts[0].steer).toBe(running ? true : undefined)
    expect(input().value).toBe('pending from B')
    expect(userRow(store)).toBeUndefined()
    expect(appQueryClient.getQueryData(sharedCrewKeys.sendState('slot-a'))).toMatchObject({ revision: 1 })
  })

  it('a steer that loses the race keeps its text, drops its bubble and names who sent first', async () => {
    noteSendRevision('slot-a', 0)
    const posts = wireSendChat(() => ({ status: 409, body: RACE_BODY }))
    const store = makeStore({ running: true })
    await renderPage(store)
    await sendText('steer from B')
    await waitFor(() => expect(screen.getByTestId('send-race-notice')).toBeTruthy())
    expect(posts[0].steer).toBe(true)
    expect(posts[0].base_revision).toBe(0)
    expect(screen.getByTestId('send-race-notice').textContent).toContain('alice sent first')
    expect(input().value).toBe('steer from B')
    expect(userRow(store)).toBeUndefined()
    expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(false)
  })
})

describe('the typing signal belongs to the consumed composer draft', { timeout: 20_000 }, () => {
  it('signals the composer slot before the revision read finishes', async () => {
    let answer!: (value: unknown) => void
    sendState.mockReturnValue(new Promise(resolve => { answer = resolve }))
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
    const heard = vi.fn()
    const off = composerTypingSignal.subscribeSent(heard)
    try {
      await renderPage(makeStore())
      await sendText('my draft')
      expect(input().value).toBe('')
      expect(sendChat).not.toHaveBeenCalled()
      expect(heard.mock.calls).toEqual([['slot-a']])
      await act(async () => { answer({ schema_version: 1, session_key: 'slot-a', revision: 0, last: null }) })
      await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
      expect(heard).toHaveBeenCalledTimes(1)
    } finally {
      off()
    }
  })

  it('a delayed new-session launch does not end typing in the session opened meanwhile', async () => {
    let created!: (slot: Awaited<ReturnType<typeof api.createChatSlot>>) => void
    vi.mocked(api.createChatSlot).mockReturnValue(new Promise(resolve => { created = resolve }))
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
    const store = makeStore()
    store.dispatch(setPendingInput('launch in a new session'))
    localStorage.setItem('mc-chat-drafts', JSON.stringify({ 'slot-b': 'saved B draft' }))
    const heard = vi.fn()
    const off = composerTypingSignal.subscribeSent(heard)
    try {
      await renderPage(store, undefined, '/chat?autoSend=1&newSession=1')
      await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))
      act(() => {
        store.dispatch(sseSlots([
          ...store.getState().dashboard.slots,
          { ...store.getState().dashboard.slots[0], key: 'slot-b' },
        ]))
        store.dispatch(setActiveSlot('slot-b'))
      })
      await waitFor(() => expect(input().value).toBe('saved B draft'))
      fireEvent.change(input(), { target: { value: 'B still has a draft' } })
      await waitFor(() => expect(input().value).toBe('B still has a draft'))
      await act(async () => { created({ key: 'slot-created', messages: 0, running: false }) })
      await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
      expect(sendChat.mock.calls[0].slice(0, 2)).toEqual(['launch in a new session', 'slot-created'])
      expect(store.getState().chat.activeSlot).toBe('slot-b')
      expect(input().value).toBe('B still has a draft')
      expect(heard).not.toHaveBeenCalled()
    } finally {
      off()
    }
  })
})
