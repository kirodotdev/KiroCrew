/**
 * The split-view pane's voice auto-submit is an Enter press.
 *
 * The streaming endpointer's verdict used to call the pane's plain send, so a
 * busy pane queued the dictation (or parked it behind running sub-agents) even
 * in Steer mode, where Enter would have steered. It now takes the composer's
 * default busy decision with the pane's own composer inputs.
 *
 * The real ChatPane is rendered; the voice hook is stubbed to capture the
 * partials and the endpointer verdict. The steer flag is `sendChat`'s 6th
 * argument.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { store as appStore } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot, sseChatMessage } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'
import { __resetComposerSendHoldsForTests, holdComposerSend } from '../utils/composerSendHolds'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const cfg = vi.hoisted(() => ({ decisions: false }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn(),
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
    dashboardConfig: vi.fn().mockImplementation(() => Promise.resolve({ decisions_enabled: cfg.decisions })),
    getDecisionsConsent: vi.fn().mockImplementation(() => Promise.resolve({ permits: cfg.decisions })),
    sttConfig: vi.fn().mockResolvedValue({ enabled: true, streaming: true, dictation_panel: true, provider: 'transcribe', available: true }),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))
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
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const SLOT = 'pane-slot'

function makeStore(running: boolean, subagentsOnly: boolean) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running, subagents_running: subagentsOnly, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  // A background pane, as in split view; a streamed chunk is what makes its
  // main turn "running" in the store.
  store.dispatch(setActiveSlot('front'))
  if (running) store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'working…', seq: 1 }))
  return store
}

async function dictateAndEndpoint(opts: { running: boolean; subagentsOnly?: boolean; busyMode?: 'split' | 'steer-only' }, text = 'dictated words') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(opts.running, !!opts.subagentsOnly)
  vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} {...(opts.busyMode ? { busyMode: opts.busyMode } : {})} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  const input = (await screen.findAllByRole('textbox'))[0] as HTMLTextAreaElement
  const mic = await screen.findByRole('button', { name: /voice input/i })
  await act(async () => { fireEvent.click(mic) })
  await act(async () => { voice.onPartial?.(text) })
  expect(input.value).toBe(text)
  // Auto reads Jev's consent: wait until the pane's split button shows it, so
  // the pane has rendered with the consent the auto-submit reads.
  if (cfg.decisions) await waitFor(() => expect(screen.getByTestId('busy-send-button').getAttribute('data-mode')).toBe('auto'))
  await act(async () => { voice.onEndpoint?.(); await Promise.resolve() })
  await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
  return { input, call: vi.mocked(api.sendChat).mock.calls[0] }
}

const steerArgOf = (call: unknown[]) => call[5]
const setMode = (mode: string) => localStorage.setItem(`mc-busy-send-mode:${SLOT}`, mode)

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetPaneDraftsForTests()
  cfg.decisions = false
  voice.recording = false
  voice.onPartial = null
  voice.onEndpoint = null
  vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) } as unknown as Response)
})

afterEach(() => { vi.restoreAllMocks(); __resetComposerSendHoldsForTests() })

describe('pane voice auto-submit follows the busy-send mode', { timeout: 20_000 }, () => {
  it('sends plainly while the pane is idle', async () => {
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) } as unknown as Response)
    const { call } = await dictateAndEndpoint({ running: false })
    expect(call[0]).toBe('dictated words')
    expect(steerArgOf(call)).toBeFalsy()
  })

  it('steers past the sub-agent hold in Steer mode', async () => {
    const { call } = await dictateAndEndpoint({ running: false, subagentsOnly: true })
    expect(call[0]).toBe('dictated words')
    expect(steerArgOf(call)).toBe(true)
  })

  it('steers the dictation into a running turn in Steer mode', async () => {
    const { call, input } = await dictateAndEndpoint({ running: true })
    expect(call[0]).toBe('dictated words')
    expect(steerArgOf(call)).toBe(true)
    expect(input.value).toBe('')
  })

  it('queues in Queue mode', async () => {
    setMode('queue')
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, queued: true, queue_id: 'q1' }) } as unknown as Response)
    const { call } = await dictateAndEndpoint({ running: true })
    expect(steerArgOf(call)).toBeFalsy()
  })

  it('carries steer=auto in Auto mode', async () => {
    setMode('auto')
    cfg.decisions = true
    const { call } = await dictateAndEndpoint({ running: true })
    expect(steerArgOf(call)).toBe('auto')
  })

  it('steers on a steer-only pane whatever mode the slot stored', async () => {
    setMode('queue')
    const { call } = await dictateAndEndpoint({ running: true, busyMode: 'steer-only' })
    expect(steerArgOf(call)).toBe(true)
  })

  it('sends nothing while an upload holds the composer, even where it would steer', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore(true, false)
    vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={SLOT} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
    const input = (await screen.findAllByRole('textbox'))[0] as HTMLTextAreaElement
    const mic = await screen.findByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    await act(async () => { voice.onPartial?.('dictated words') })
    await act(async () => { holdComposerSend(SLOT) })
    expect(voice.onEndpoint).toBeTypeOf('function')
    await act(async () => { voice.onEndpoint?.() })
    // The steer path would have consumed the composer and posted the text
    // without the file still on its way.
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(input.value).toBe('dictated words')
  })
})
