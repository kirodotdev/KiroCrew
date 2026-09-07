/**
 * The optimistic steer bubble must agree with the steer POST's receipt.
 *
 * `steer()` appends it with `meta.steer` on Enter, which draws the "Steered into
 * the running turn" badge. The mutation never read the answer, so two accepted
 * shapes kept that claim: `{ok, queued}`, where the text went to the queue and
 * is ALSO drawn as a queue card, and `{ok, slot, mid}`, where the POST raced
 * `chat_done` and started a new turn instead.
 *
 * Asserted on store state, not on the badge: the flag is the input the badge is
 * derived from, so reading it stays on the production dispatch path without
 * mounting the renderer.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { i18nT } from '../i18n/t'
import { DRAFTS_KEY } from '../utils/chatDrafts'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))

const sendChat = vi.fn()
const steerChat = vi.fn()
const slotRow = () => ({
  key: 'slot-a', messages: 1, running: true, mode: '',
  pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
  subagents_running: false,
})
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockImplementation(() => Promise.resolve([slotRow()])),
    chatSlotDetail: vi.fn().mockImplementation(() => Promise.resolve({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: true, has_more: false, total: 1 })),
    sendChat: (...a: unknown[]) => sendChat(...a),
    steerChat: (...a: unknown[]) => steerChat(...a),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
    suggestions: vi.fn().mockResolvedValue({ suggestions: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
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

const STEERED_TEXT = 'change course now'

function makeStore(extraSlots: Array<ReturnType<typeof slotRow>> = []) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [slotRow(), ...extraSlots],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: true, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
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

/** Drive the real path: mount, type mid-turn, press Enter (Steer is the default
 *  busy action), and wait for the mocked receipt to have been applied. */
async function steerWithReceipt(receipt: Record<string, unknown> | { reject: unknown } | { httpStatus: number; body: Record<string, unknown> }) {
  // A mid-turn steer is the same `/api/chat` POST as a send, flagged `steer`,
  // through the same transport -- so it is `sendChat` that answers, with a
  // Response-shaped value the receipt reader parses (or a rejection, for a
  // request that never left / the transport's deadline).
  if ('reject' in receipt) sendChat.mockRejectedValue(receipt.reject)
  else if ('httpStatus' in receipt) sendChat.mockResolvedValue({ ok: false, status: receipt.httpStatus, json: () => Promise.resolve(receipt.body) })
  else sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve(receipt) })
  const store = makeStore()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  const input = await waitFor(() => screen.getByLabelText('Message input') as HTMLTextAreaElement)
  fireEvent.change(input, { target: { value: STEERED_TEXT } })
  await act(async () => {
    fireEvent.keyDown(input, { key: 'Enter' })
    await Promise.resolve()
  })
  await waitFor(() => expect(sendChat).toHaveBeenCalled())
  // The steer flag is the 6th positional argument of api.sendChat.
  expect(sendChat.mock.calls[0][5]).toBe(true)
  // The receipt is applied in the mutation's onSuccess, a few microtasks past
  // the resolved promise, so settle the queue before reading the store.
  await act(async () => { for (let i = 0; i < 6; i++) await Promise.resolve() })
  const rows = (store.getState().chat.messages as ChatMessage[]).filter(m => m.role === 'user' && m.content === STEERED_TEXT)
  return Object.assign(rows, { store, input })
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  sendChat.mockReset()
  sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
  steerChat.mockReset()
})

describe('optimistic steer bubble vs the steer receipt', { timeout: 20_000 }, () => {
  it('drops the bubble when the server queued the text instead of injecting it', async () => {
    const rows = await steerWithReceipt({ ok: true, queued: true })
    // Every arm answering `queued` has already broadcast a `queue_push`, so that
    // card is the server-owned representation and the bubble is a duplicate.
    expect(rows).toHaveLength(0)
  })

  it('demotes the bubble to a plain user message when the steer started a new turn', async () => {
    const rows = await steerWithReceipt({ ok: true, slot: 'slot-a', mid: 'm-1' })
    // The text ran, so the row stays — but it was not steered into anything.
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.steer).toBeFalsy()
  })

  it('leaves a genuine steer alone', async () => {
    const rows = await steerWithReceipt({ ok: true, steered: true })
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.steer).toBe(true)
    expect(rows.input.value).toBe('')
  })

  it('a refused steer hands the text back AND says why, framed as a send failure', async () => {
    // The composer was cleared at submit and the optimistic bubble is not
    // persisted, so a refusal that restored nothing would lose the text -- and
    // the server's reason ("no running turn") is the user's next step, not a
    // console line.
    const rows = await steerWithReceipt({ httpStatus: 409, body: { ok: false, error: 'no running turn' } })
    await waitFor(() => expect(rows.input.value).toBe(STEERED_TEXT))
    const err = (rows.store.getState().chat.messages as ChatMessage[]).find(m => m.role === 'error')
    expect(err?.content).toBe('Send failed: no running turn')
    expect((rows.store.getState().chat.messages as ChatMessage[]).some(m => m.role === 'notice')).toBe(false)
    // The optimistic bubble is dropped too: standing, it would be a third,
    // false representation of the same text next to the error row.
    await waitFor(() => expect(
      (rows.store.getState().chat.messages as ChatMessage[]).filter(m => m.role === 'user' && m.content === STEERED_TEXT),
    ).toHaveLength(0))
  })

  it('a deadline-aborted steer removes the unconfirmed bubble and hands the text back under a WARN notice', async () => {
    // The transport aborts a hung POST; the steer never had a deadline before,
    // so a stalled socket the abort kills is a new way for the text to be lost.
    // The bubble goes too: standing, it would read as delivered and make
    // "check the transcript" unanswerable.
    const rows = await steerWithReceipt({ reject: new DOMException('aborted', 'AbortError') })
    await waitFor(() => expect(rows.input.value).toBe(STEERED_TEXT))
    const notice = (rows.store.getState().chat.messages as ChatMessage[]).find(m => m.role === 'notice')
    expect(notice?.content).toMatch(/^\u26A0\uFE0F Delivery not confirmed/)
    await waitFor(() => expect(
      (rows.store.getState().chat.messages as ChatMessage[]).filter(m => m.role === 'user' && m.content === STEERED_TEXT),
    ).toHaveLength(0))
  })

  it('a never-left steer hands the text back with the connection copy', async () => {
    const rows = await steerWithReceipt({ reject: new TypeError('Failed to fetch') })
    await waitFor(() => expect(rows.input.value).toBe(STEERED_TEXT))
    const err = (rows.store.getState().chat.messages as ChatMessage[]).find(m => m.role === 'error')
    expect(err?.content).toBe(i18nT('pages.chatPage.send_failed_connection'))
    await waitFor(() => expect(
      (rows.store.getState().chat.messages as ChatMessage[]).filter(m => m.role === 'user' && m.content === STEERED_TEXT),
    ).toHaveLength(0))
  })

  it('a refusal that lands after the user switched sessions is handed back to the ORIGINATING slot', async () => {
    let rejectSend: (e: unknown) => void = () => {}
    sendChat.mockReturnValue(new Promise((_, rej) => { rejectSend = rej }))
    // Two live sessions, so the switch below is to a slot the page knows.
    const store = makeStore([{ ...slotRow(), key: 'slot-b', running: false }])
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await act(async () => {
      render(
        <QueryClientProvider client={qc}>
          <Provider store={store}>
            <ThemeProvider>
              <MemoryRouter><ChatPage /></MemoryRouter>
            </ThemeProvider>
          </Provider>
        </QueryClientProvider>,
      )
    })
    const input = await waitFor(() => screen.getByLabelText('Message input') as HTMLTextAreaElement)
    fireEvent.change(input, { target: { value: STEERED_TEXT } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }); await Promise.resolve() })
    await waitFor(() => expect(sendChat).toHaveBeenCalled())
    // Switch to another session and start typing there.
    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    const inputB = await waitFor(() => screen.getByLabelText('Message input') as HTMLTextAreaElement)
    expect(store.getState().chat.activeSlot).toBe('slot-b')
    fireEvent.change(inputB, { target: { value: 'typing in B' } })
    await act(async () => { rejectSend(new TypeError('Failed to fetch')); for (let i = 0; i < 6; i++) await Promise.resolve() })
    // B's composer is untouched; A's draft holds the steer text; the error row
    // is in A's transcript, not B's.
    expect(inputB.value).toBe('typing in B')
    expect(store.getState().chat.slotMessages['slot-a']?.some(m => m.role === 'error')).toBe(true)
    expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(false)
    expect(JSON.parse(localStorage.getItem(DRAFTS_KEY) ?? '{}')['slot-a']).toBe(STEERED_TEXT)
  })
})
