/**
 * A plain send whose POST hits the transport deadline before a receipt arrives
 * (`response-late`) must SAY so. Delivery is indeterminate at that point: the
 * request may have reached the gateway late or never left the browser, so the
 * composer keeps the optimistic bubble rather than handing the text back (a
 * duplicate is worse than a visible pending row) -- but a bubble that looks
 * delivered, with no notice under it, is read as a finished turn.
 *
 * Asserted on store state, as the steer-receipt tests are: the notice row and
 * the bubble's `meta.optimistic` / `meta.deliveryUnconfirmed` flags are the
 * inputs the transcript and the composer derive their rendering from, so
 * reading them stays on the production dispatch path. The footer's running
 * indicator is the one rendered surface read directly: whether it shows is the
 * question, not what it says.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import type { RootState } from '../store'
import { store as appStore } from '../store'
import type { ChatMessage } from '../types'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { confirmOptimisticSend, selectTurnInterrupted, sseChatMessage } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { i18nT } from '../i18n/t'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))

const sendChat = vi.fn()
// The slot's running state for the case under test. Idle by default, so Enter
// on the composer is a plain send, not a steer; the busy case flips it and the
// mount-time refetch must agree with the preloaded store or the slot would
// read idle again before Enter.
let fixtureRunning = false
const slotRow = () => ({
  key: 'slot-a', messages: 1, running: fixtureRunning, mode: '',
  pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
  subagents_running: false,
})
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockImplementation(() => Promise.resolve([slotRow()])),
    chatSlotDetail: vi.fn().mockImplementation(() => Promise.resolve({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: fixtureRunning, has_more: false, total: 1 })),
    sendChat: (...a: unknown[]) => sendChat(...a),
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

const SENT_TEXT = 'did this arrive?'

function makeStore({ running = false } = {}) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [slotRow()],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: running, slotStopping: false, slotState: 'idle',
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
  // Receipt handlers read the singleton; rendered selectors read Provider.
  // They must see the same transcript, as they do in the running app.
  vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
  return store
}

/** Drive the real path: mount, type, press Enter (a plain send), and let the
 *  transport's deadline abort the POST. With `echo`, a correlated user echo
 *  lands before the abort, the way a channel-linked slot delivers one. With
 *  `busy`, the slot is running and the split button is set to Queue, so Enter
 *  is still a plain send (not a steer) but mints no bubble. With `confirmed`,
 *  the POST answers an ordinary `ok` receipt instead: the control case whose
 *  footer must look exactly as it always did. */
async function sendPastTheDeadline({ echo = false, busy = false, confirmed = false } = {}) {
  fixtureRunning = busy
  if (busy) localStorage.setItem('mc-busy-send-mode:slot-a', 'queue')
  const store = makeStore({ running: busy })
  sendChat.mockImplementation(async (_text, slot, _signal, _files, meta) => {
    if (echo) {
      store.dispatch(sseChatMessage({
        slot, role: 'user', content: SENT_TEXT, cls: 'msg msg-u', ts: '2026-09-28T00:00:00Z',
        meta: { ...meta, mid: 'm-delivered' },
      }))
    }
    if (confirmed) return { ok: true, json: () => Promise.resolve({ ok: true, mid: 'm-ok' }) }
    // `sendTurn` maps an AbortError to `response-late`: the deadline fired.
    throw new DOMException('aborted', 'AbortError')
  })
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
  fireEvent.change(input, { target: { value: SENT_TEXT } })
  await act(async () => {
    fireEvent.keyDown(input, { key: 'Enter' })
    await Promise.resolve()
  })
  await waitFor(() => expect(sendChat).toHaveBeenCalled())
  // A plain send, not a steer: the steer flag is the 6th positional argument.
  expect(sendChat.mock.calls[0][5]).toBeFalsy()
  // Let send()'s receipt handling run to completion.
  await act(async () => { for (let i = 0; i < 8; i++) await Promise.resolve() })
  const messages = () => store.getState().chat.messages as ChatMessage[]
  // The client-minted correlation id the bubble and the POST share: the two
  // confirmation doors (`confirmOptimisticSend`, a correlated echo) address the
  // row by it, exactly as the receipt and the WS frame do in the running app.
  const sendId = (sendChat.mock.calls[0][4] as { sendId: string }).sendId
  return { store, input, messages, sendId }
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  sendChat.mockReset()
  fixtureRunning = false
})
afterEach(() => vi.restoreAllMocks())

describe('a plain send whose POST hits the deadline (response-late)', { timeout: 20_000 }, () => {
  it('posts the WARN notice under the bubble and keeps the bubble pending', async () => {
    const { messages, input } = await sendPastTheDeadline()
    const notice = await waitFor(() => {
      const row = messages().find(m => m.role === 'notice')
      expect(row).toBeDefined()
      return row!
    })
    // The notice says the bubble is still pending -- NOT that the text is back
    // in the composer, which is the steer path's copy for the no-bubble case.
    expect(notice.content).toBe('\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_unconfirmed_pending'))
    expect(notice.content).toMatch(/^\u26A0\uFE0F Delivery not confirmed/)
    // The bubble stays, still marked unconfirmed: the standing rule for a minted
    // bubble on `response-late` is to keep it pending rather than restore the
    // text and risk a duplicate of a turn that did run. The deadline mark is
    // what draws its pending line.
    const bubbles = messages().filter(m => m.role === 'user' && m.content === SENT_TEXT)
    expect(bubbles).toHaveLength(1)
    expect(bubbles[0].meta?.optimistic).toBe(true)
    expect(bubbles[0].meta?.deliveryUnconfirmed).toBe(true)
    // The notice lands directly after the bubble it describes.
    expect(messages().indexOf(notice)).toBe(messages().indexOf(bubbles[0]) + 1)
    // No restore and no error row: nothing proved the send failed.
    expect(input.value).toBe('')
    expect(messages().some(m => m.role === 'error')).toBe(false)
  })

  it('does not read the pending bubble as an interrupted turn, so Resume is not offered for it', async () => {
    const { store, messages } = await sendPastTheDeadline()
    await waitFor(() => expect(messages().some(m => m.role === 'notice')).toBe(true))
    expect(selectTurnInterrupted(store.getState())).toBe(false)
  })

  it('stays silent when a correlated echo already proved delivery', async () => {
    const { messages, input } = await sendPastTheDeadline({ echo: true })
    const bubbles = messages().filter(m => m.role === 'user' && m.content === SENT_TEXT)
    expect(bubbles).toHaveLength(1)
    // The echo reconciled the bubble in place: it is the server's row now.
    expect(bubbles[0].meta?.mid).toBe('m-delivered')
    expect(bubbles[0].meta?.optimistic).toBeUndefined()
    expect(bubbles[0].meta?.deliveryUnconfirmed).toBeUndefined()
    expect(messages().some(m => m.role === 'notice' || m.role === 'error')).toBe(false)
    expect(input.value).toBe('')
  })

  it('posts no notice for a busy-slot Queue send, which minted no bubble to describe', async () => {
    // Enter on a running slot with the split button on Queue is a plain send
    // that draws no bubble (the server's queue card would represent it). A
    // notice pointing at "the message above" would point at nothing, so this
    // shape keeps the bare pending verdict. What becomes of its text is the
    // restore decision this change does not take.
    const { messages, input } = await sendPastTheDeadline({ busy: true })
    expect(messages().some(m => m.role === 'user' && m.content === SENT_TEXT)).toBe(false)
    expect(messages().some(m => m.role === 'notice' || m.role === 'error')).toBe(false)
    expect(input.value).toBe('')
  })
})

/* The footer's running indicator ("Thinking…") under a bubble that says
 * "Delivery pending…" over a notice that says "Delivery not confirmed" is three
 * signals for two states: the UI claims the agent is working on a message
 * nothing proves it received. The indicator yields while the trailing send is
 * unconfirmed and returns by itself through either confirmation door; a send
 * whose receipt came back is untouched. */
describe('the footer running indicator while the trailing send is unconfirmed', { timeout: 20_000 }, () => {
  const thinking = () => screen.queryByRole('status', { name: i18nT('pages.chat.chatFooter.thinking') })

  it('hides the indicator while the trailing bubble is unconfirmed', async () => {
    const { store, messages } = await sendPastTheDeadline()
    await waitFor(() => expect(messages().some(m => m.role === 'notice')).toBe(true))
    // The local turn is still pending -- the send did start one -- so only the
    // unconfirmed mark can be what hides the indicator.
    expect(store.getState().chat.slotRunning).toBe(true)
    expect(screen.getByTestId('send-pending')).toBeInTheDocument()
    expect(thinking()).toBeNull()
  })

  it('brings the indicator back once the receipt confirms the send after all', async () => {
    const { store, messages, sendId } = await sendPastTheDeadline()
    await waitFor(() => expect(messages().some(m => m.role === 'notice')).toBe(true))
    expect(thinking()).toBeNull()
    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId, mid: 'm-late-receipt' })) })
    await waitFor(() => expect(thinking()).toBeInTheDocument())
    expect(screen.queryByTestId('send-pending')).toBeNull()
  })

  it('brings the indicator back once a correlated echo clears the mark', async () => {
    const { store, messages, sendId } = await sendPastTheDeadline()
    await waitFor(() => expect(messages().some(m => m.role === 'notice')).toBe(true))
    expect(thinking()).toBeNull()
    await act(async () => {
      store.dispatch(sseChatMessage({
        slot: 'slot-a', role: 'user', content: SENT_TEXT, cls: 'msg msg-u', ts: '2026-09-28T00:00:05Z',
        meta: { sendId, mid: 'm-late-echo' },
      }))
    })
    await waitFor(() => expect(thinking()).toBeInTheDocument())
    expect(screen.queryByTestId('send-pending')).toBeNull()
    // The echo reconciled the bubble in place rather than adding a second row.
    expect(messages().filter(m => m.role === 'user' && m.content === SENT_TEXT)).toHaveLength(1)
  })

  it('leaves the indicator alone for a send whose receipt came back', async () => {
    const { store, messages } = await sendPastTheDeadline({ confirmed: true })
    const bubbles = messages().filter(m => m.role === 'user' && m.content === SENT_TEXT)
    expect(bubbles).toHaveLength(1)
    expect(bubbles[0].meta?.optimistic).toBeUndefined()
    expect(bubbles[0].meta?.deliveryUnconfirmed).toBeUndefined()
    expect(store.getState().chat.slotRunning).toBe(true)
    await waitFor(() => expect(thinking()).toBeInTheDocument())
    expect(screen.queryByTestId('send-pending')).toBeNull()
    expect(messages().some(m => m.role === 'notice' || m.role === 'error')).toBe(false)
  })
})
