/**
 * ChatPage's sends outside the composer honour the slot's busy-send mode, like
 * Send does: the side panel's comment Submit and the voice endpointer's
 * auto-submit.
 *
 * Both used to call the plain send. While only background sub-agents ran that
 * send carried no steer flag, so the server parked it behind them even in Steer
 * mode; while a turn ran it queued instead of steering. The comment Submit also
 * keeps its delivery verdict honest: ArtifactPanel marks a batch sent only on
 * `true`, so a refused or unconfirmed steer must resolve `false`.
 *
 * The real ChatPage is rendered. The side panel is stubbed to capture the
 * `onSubmitComments` it is handed, and the voice hook to capture the endpointer
 * verdict. The steer flag is `sendChat`'s 6th argument.
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
import dashboardReducer, { sseDisconnected } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { __resetComposerSendHoldsForTests, holdComposerSend } from '../utils/composerSendHolds'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))

const ASSISTANT = { role: 'assistant', content: 'Ready.', cls: '' }

const sendChat = vi.fn()
const cfg = { decisions: false }
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
    dashboardConfig: vi.fn().mockImplementation(() => Promise.resolve({ decisions_enabled: cfg.decisions })),
    getDecisionsConsent: vi.fn().mockImplementation(() => Promise.resolve({ permits: cfg.decisions })),
    chatFolders: vi.fn().mockResolvedValue([]),
    tagColumns: vi.fn().mockResolvedValue([]),
    sttConfig: vi.fn().mockResolvedValue({ enabled: true, streaming: true, dictation_panel: true, provider: 'transcribe', available: true }),
  },
  SEARCH_MIN_CHARS: 2,
}))
/** The streaming capture `useVoiceInput` drives: its partials and endpointer
 *  verdict, as the voice atom hands them over. */
const voice = vi.hoisted(() => ({
  recording: false,
  onPartial: null as ((t: string) => void) | null,
  onEndpoint: null as (() => void) | null,
}))
vi.mock('../hooks/useVoiceInput', () => ({
  useVoiceInput: (_onText: unknown, opts?: { onPartial?: (t: string) => void; onEndpoint?: () => void }) => {
    voice.onPartial = opts?.onPartial ?? null
    voice.onEndpoint = opts?.onEndpoint ?? null
    const on = () => { voice.recording = true }
    const off = () => { voice.recording = false }
    return {
      recording: voice.recording, transcribing: false, sessionOwner: null, streamEnabled: true,
      toggle: () => { voice.recording = !voice.recording }, start: on, stop: off, cancel: off, prewarm: vi.fn(),
      error: null, level: 0, deviceLabel: '', clearError: vi.fn(), partial: '',
      sampleRef: { current: { level: 0, centroid: 0.5, onset: 0 } },
    }
  },
  voiceInputSupported: true,
}))
/** The comment Submit the side panel is handed (ArtifactPanel / MarkdownPanel). */
const panel = vi.hoisted(() => ({ submit: null as ((message: string) => unknown) | null }))
vi.mock('../pages/chat/SidePanel', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../pages/chat/SidePanel')>()),
  default: (p: { onSubmitComments?: (message: string) => unknown }) => { panel.submit = p.onSubmitComments ?? null; return null },
}))
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
        // The side panel is open, so ChatPage mounts it with its Submit.
        subagents: {}, toolLog: [], activityOpen: true, activityTab: 'tools',
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
  // The send handlers read the singleton directly; selectors read Provider.
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
  const input = await screen.findByLabelText('Message input') as HTMLTextAreaElement
  await waitFor(() => expect(panel.submit).toBeTruthy())
  return { store, input, qc }
}

/** Auto mode reads Jev's consent; wait until the composer shows it. */
async function awaitAutoMode(input: HTMLTextAreaElement) {
  fireEvent.change(input, { target: { value: 'draft' } })
  await waitFor(() => expect(screen.getByTestId('busy-send-button').getAttribute('data-mode')).toBe('auto'))
}

async function submitComments(message = 'Review notes') {
  let verdict: unknown
  await act(async () => { verdict = await panel.submit?.(message) })
  return verdict
}

const steerArgOf = (call: unknown[]) => call[5]
const setMode = (mode: string) => localStorage.setItem('mc-busy-send-mode', mode)
const steered = () => ({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) })
const queued = () => ({ ok: true, json: () => Promise.resolve({ ok: true, queued: true, queue_id: 'q1' }) })

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  sendChat.mockReset()
  sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
  cfg.decisions = false
  panel.submit = null
  voice.onEndpoint = null
  voice.onPartial = null
  voice.recording = false
})

afterEach(() => { vi.restoreAllMocks(); __resetComposerSendHoldsForTests() })

describe('comment Submit follows the busy-send mode', { timeout: 20_000 }, () => {
  it('sends plainly while the session is idle', async () => {
    await renderChat({ subagentsRunning: false, turnRunning: false })
    expect(await submitComments()).toBe(true)
    expect(sendChat).toHaveBeenCalledTimes(1)
    expect(sendChat.mock.calls[0][0]).toBe('Review notes')
    expect(steerArgOf(sendChat.mock.calls[0])).toBeFalsy()
  })

  it('steers past the sub-agent hold in Steer mode', async () => {
    await renderChat({ subagentsRunning: true, turnRunning: false })
    expect(await submitComments()).toBe(true)
    expect(steerArgOf(sendChat.mock.calls[0])).toBe(true)
  })

  it('steers a running turn in Steer mode, and the steered receipt is a delivery', async () => {
    sendChat.mockResolvedValue(steered())
    await renderChat({ subagentsRunning: false, turnRunning: true })
    expect(await submitComments()).toBe(true)
    expect(sendChat.mock.calls[0][0]).toBe('Review notes')
    expect(steerArgOf(sendChat.mock.calls[0])).toBe(true)
  })

  it('queues in Queue mode', async () => {
    setMode('queue')
    sendChat.mockResolvedValue(queued())
    await renderChat({ subagentsRunning: false, turnRunning: true })
    expect(await submitComments()).toBe(true)
    expect(steerArgOf(sendChat.mock.calls[0])).toBeFalsy()
  })

  it('carries steer=auto in Auto mode', async () => {
    setMode('auto')
    cfg.decisions = true
    const { input } = await renderChat({ subagentsRunning: false, turnRunning: true })
    await awaitAutoMode(input)
    expect(await submitComments()).toBe(true)
    expect(steerArgOf(sendChat.mock.calls[0])).toBe('auto')
    // The composer draft is not the batch's payload.
    expect(input.value).toBe('draft')
  })

  it('keeps the batch pending when the steer is refused', async () => {
    sendChat.mockResolvedValue({ ok: false, json: () => Promise.resolve({ ok: false, error: 'slot agent mismatch' }) })
    await renderChat({ subagentsRunning: false, turnRunning: true })
    expect(await submitComments()).toBe(false)
    expect(steerArgOf(sendChat.mock.calls[0])).toBe(true)
  })

  it('keeps the batch pending when the steer is unconfirmed', async () => {
    // The abort deadline fired: the steer may or may not have landed, and the
    // steer receipt policy hands such a steer back rather than assume it.
    sendChat.mockRejectedValue(new DOMException('aborted', 'AbortError'))
    await renderChat({ subagentsRunning: false, turnRunning: true })
    expect(await submitComments()).toBe(false)
    expect(steerArgOf(sendChat.mock.calls[0])).toBe(true)
  })
})

describe('voice auto-submit is an Enter press', { timeout: 20_000 }, () => {
  async function dictateAndEndpoint(opts: Busy, text = 'dictated words') {
    const r = await renderChat(opts)
    if (localStorage.getItem('mc-busy-send-mode') === 'auto') {
      await awaitAutoMode(r.input)
      fireEvent.change(r.input, { target: { value: '' } })
    }
    // Start a streaming capture, let its partial land in the composer, then the
    // endpointer judges the utterance complete.
    await waitFor(() => expect(screen.getByRole('button', { name: /voice input/i })).toBeTruthy())
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /voice input/i })) })
    await act(async () => { voice.onPartial?.(text) })
    expect(r.input.value).toBe(text)
    await act(async () => { voice.onEndpoint?.(); await Promise.resolve() })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    return { ...r, call: sendChat.mock.calls[0] }
  }

  it('sends plainly while the session is idle', async () => {
    const { call } = await dictateAndEndpoint({ subagentsRunning: false, turnRunning: false })
    expect(call[0]).toBe('dictated words')
    expect(steerArgOf(call)).toBeFalsy()
  })

  it('steers past the sub-agent hold in Steer mode', async () => {
    const { call } = await dictateAndEndpoint({ subagentsRunning: true, turnRunning: false })
    expect(call[0]).toBe('dictated words')
    expect(steerArgOf(call)).toBe(true)
  })

  it('steers the dictation into a running turn in Steer mode', async () => {
    sendChat.mockResolvedValue(steered())
    const { call, input } = await dictateAndEndpoint({ subagentsRunning: false, turnRunning: true })
    expect(call[0]).toBe('dictated words')
    expect(steerArgOf(call)).toBe(true)
    // The steer path consumed the composer, as Enter does.
    expect(input.value).toBe('')
  })

  it('keeps the dictation when disconnected in Steer mode', async () => {
    const { input, store } = await renderChat({ subagentsRunning: false, turnRunning: true })
    await waitFor(() => expect(screen.getByRole('button', { name: /voice input/i })).toBeTruthy())
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /voice input/i })) })
    await act(async () => { voice.onPartial?.('dictated words') })
    expect(input.value).toBe('dictated words')
    await act(async () => { store.dispatch(sseDisconnected()) })
    expect(voice.onEndpoint).toBeTypeOf('function')
    await act(async () => { voice.onEndpoint?.() })
    expect(sendChat).not.toHaveBeenCalled()
    expect(input.value).toBe('dictated words')
  })

  it('queues in Queue mode', async () => {
    setMode('queue')
    sendChat.mockResolvedValue(queued())
    const { call } = await dictateAndEndpoint({ subagentsRunning: false, turnRunning: true })
    expect(steerArgOf(call)).toBeFalsy()
  })

  it('carries steer=auto in Auto mode', async () => {
    setMode('auto')
    cfg.decisions = true
    const { call } = await dictateAndEndpoint({ subagentsRunning: false, turnRunning: true })
    expect(call[0]).toBe('dictated words')
    expect(steerArgOf(call)).toBe('auto')
  })

  it('sends nothing while an upload holds the composer, even where it would steer', async () => {
    sendChat.mockResolvedValue(steered())
    const { input } = await renderChat({ subagentsRunning: false, turnRunning: true })
    await waitFor(() => expect(screen.getByRole('button', { name: /voice input/i })).toBeTruthy())
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /voice input/i })) })
    await act(async () => { voice.onPartial?.('dictated words') })
    await act(async () => { holdComposerSend('slot-a') })
    expect(voice.onEndpoint).toBeTypeOf('function')
    await act(async () => { voice.onEndpoint?.() })
    // The steer path would have consumed the composer and posted the text
    // without the file still on its way.
    expect(sendChat).not.toHaveBeenCalled()
    expect(input.value).toBe('dictated words')
  })
})
