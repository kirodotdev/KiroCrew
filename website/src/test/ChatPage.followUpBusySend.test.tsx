/**
 * A follow-up chip's send honours the slot's busy-send mode, like Send does.
 *
 * Both chip paths used to call the plain send: the quick-send click and the
 * ↑ segment (double-click alike). While only background sub-agents ran, that
 * send carried no steer flag, so the server parked it behind the wave even in
 * Steer mode; while a turn ran, the ↑ segment queued instead of steering.
 *
 * The real ChatPage is rendered and the real chip clicked, so the shipped
 * wiring runs. The steer flag is `sendChat`'s 6th argument.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import type { RootState } from '../store'
import { store as appStore } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))

/** The marker has to close its own line for OPTION_MARKER_RE to match. */
const ASSISTANT = { role: 'assistant', content: 'Ready to proceed.\n\n[OPTIONS: Deploy | Roll back]', cls: '' }

const sendChat = vi.fn()
const cfg = { quickSend: false, decisions: false }
const detail = { running: false }
const slotRow = (over: Record<string, unknown> = {}) => ({
  key: 'slot-a', messages: 1, running: false, mode: '',
  pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
  subagents_running: false, ...over,
})
const slotsFixture: { rows: Record<string, unknown>[] } = { rows: [slotRow()] }
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockImplementation(() => Promise.resolve(slotsFixture.rows)),
    chatSlotDetail: vi.fn().mockImplementation(() => Promise.resolve({ messages: [ASSISTANT], running: detail.running, has_more: false, total: 1 })),
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
    dashboardConfig: vi.fn().mockImplementation(() => Promise.resolve({ quick_send: cfg.quickSend, decisions_enabled: cfg.decisions })),
    getDecisionsConsent: vi.fn().mockImplementation(() => Promise.resolve({ permits: cfg.decisions })),
    chatFolders: vi.fn().mockResolvedValue([]),
    tagColumns: vi.fn().mockResolvedValue([]),
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

type Busy = { subagentsRunning: boolean; turnRunning: boolean }

function makeStore(opts: Busy) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: slotsFixture.rows,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [ASSISTANT],
        slotRunning: opts.turnRunning, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
        followups: {},
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderChat(opts: Busy) {
  detail.running = opts.turnRunning
  slotsFixture.rows = [slotRow({ running: opts.turnRunning, subagents_running: opts.subagentsRunning })]
  const store = makeStore(opts)
  // The send handler reads the singleton directly; selectors read Provider.
  vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
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
  await waitFor(() => expect(screen.getByRole('button', { name: 'Deploy' })).toBeTruthy())
  return { store, input: screen.getByLabelText('Message input') as HTMLTextAreaElement }
}

/** The quick-send state: one click on an unpicked chip sends it. */
async function quickSendClick(option: string) {
  // The chip waits for the dashboard config before it switches to quick send.
  await waitFor(() => expect(screen.queryByRole('button', { name: `Send now: ${option}` })).toBeNull())
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: option })); await Promise.resolve() })
}

/** The ↑ segment, shown when quick send is off. */
async function sendNowClick(option: string) {
  const arrow = await screen.findByRole('button', { name: `Send now: ${option}` })
  await act(async () => { fireEvent.click(arrow); await Promise.resolve() })
}

const steerArgOf = (call: unknown[]) => call[5]
const setMode = (mode: string) => localStorage.setItem('mc-busy-send-mode', mode)

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  sendChat.mockReset()
  sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
  cfg.quickSend = false
  cfg.decisions = false
})

afterEach(() => vi.restoreAllMocks())

describe('chip sends while only sub-agents run', { timeout: 20_000 }, () => {
  it('a quick-send click carries the steer flag in Steer mode', async () => {
    cfg.quickSend = true
    await renderChat({ subagentsRunning: true, turnRunning: false })
    await quickSendClick('Deploy')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(sendChat.mock.calls[0][0]).toBe('Deploy')
    expect(steerArgOf(sendChat.mock.calls[0])).toBe(true)
  })

  it('the ↑ segment carries the steer flag in Steer mode', async () => {
    await renderChat({ subagentsRunning: true, turnRunning: false })
    await sendNowClick('Deploy')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(sendChat.mock.calls[0][0]).toBe('Deploy')
    expect(steerArgOf(sendChat.mock.calls[0])).toBe(true)
  })

  it('a quick-send click stays unflagged in Queue mode', async () => {
    setMode('queue')
    cfg.quickSend = true
    await renderChat({ subagentsRunning: true, turnRunning: false })
    await quickSendClick('Deploy')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(steerArgOf(sendChat.mock.calls[0])).toBeFalsy()
  })
})

describe('chip sends while a turn runs', { timeout: 20_000 }, () => {
  it('the ↑ segment steers the chip text into the turn, leaving the draft alone', async () => {
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) })
    const { store, input } = await renderChat({ subagentsRunning: false, turnRunning: true })
    fireEvent.change(input, { target: { value: 'my own draft' } })
    await sendNowClick('Deploy')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    const call = sendChat.mock.calls[0]
    expect(call[0]).toBe('Deploy')
    expect(steerArgOf(call)).toBe(true)
    // The receipt-aware steer path: a reconciliation sendId and no theme.
    expect(call[2]).toBeUndefined()
    expect((call[4] as { sendId?: string }).sendId).toBeTruthy()
    const bubble = store.getState().chat.messages.find(m => m.role === 'user' && m.content === 'Deploy')
    expect(bubble?.meta?.steer).toBe(true)
    expect(input.value).toBe('my own draft')
  })

  it('the ↑ segment carries steer=auto in Auto mode', async () => {
    setMode('auto')
    cfg.decisions = true
    const { input } = await renderChat({ subagentsRunning: false, turnRunning: true })
    // A draft shows the split button, whose mode reads Auto once consent answers.
    fireEvent.change(input, { target: { value: 'draft' } })
    await waitFor(() => expect(screen.getByTestId('busy-send-button').getAttribute('data-mode')).toBe('auto'))
    await sendNowClick('Deploy')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(sendChat.mock.calls[0][0]).toBe('Deploy')
    expect(steerArgOf(sendChat.mock.calls[0])).toBe('auto')
  })

  it('the ↑ segment queues in Queue mode', async () => {
    setMode('queue')
    await renderChat({ subagentsRunning: false, turnRunning: true })
    await sendNowClick('Deploy')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(sendChat.mock.calls[0][0]).toBe('Deploy')
    expect(steerArgOf(sendChat.mock.calls[0])).toBeFalsy()
  })

  it('a quick-send click still toggles the chip instead of sending', async () => {
    cfg.quickSend = true
    const { input } = await renderChat({ subagentsRunning: false, turnRunning: true })
    await quickSendClick('Deploy')

    await waitFor(() => expect(input.value).toContain('Deploy'))
    expect(sendChat).not.toHaveBeenCalled()
  })
})

describe('chip sends on an idle slot', { timeout: 20_000 }, () => {
  it('a quick-send click is an ordinary unflagged send', async () => {
    cfg.quickSend = true
    await renderChat({ subagentsRunning: false, turnRunning: false })
    await quickSendClick('Deploy')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(sendChat.mock.calls[0][0]).toBe('Deploy')
    expect(steerArgOf(sendChat.mock.calls[0])).toBeFalsy()
  })
})
