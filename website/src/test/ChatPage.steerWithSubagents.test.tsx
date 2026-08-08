/**
 * Steer is the default action for the whole busy window, including the stretch
 * where the parent turn has ended but background sub-agents are still running.
 *
 * `spawn_run` is fire-and-forget, so during a wave the slot reads NOT running
 * while the composer still reads busy (`selectComposerBusy` folds in
 * `subagents_running`). Gating the split Steer/Queue button on the narrow
 * "turn running" signal left that stretch on the plain send path, which posts
 * no steer intent — and the server then parks the message behind the wave, so a
 * user who never chose Queue got queued anyway.
 *
 * Both halves are asserted here: the button is offered, and Enter carries the
 * steer flag on the `ws=1` send (a fresh turn needs the streaming endpoint —
 * there is no live turn to inject into). Choosing Queue must still queue, and
 * an ordinary idle send must not acquire the flag.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import type { RootState } from '../store'
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

const sendChat = vi.fn()
const steerChat = vi.fn()
/** ChatPage re-reads both the slot list and the slot detail after mount, so the
 *  fixtures have to agree with the store seed or the refresh erases the state
 *  under test (the wave flag, or `running`). */
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
    chatSlotDetail: vi.fn().mockImplementation(() => Promise.resolve({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: detail.running, has_more: false, total: 1 })),
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

/** `subagentsRunning` is the server's per-slot wave flag; `turnRunning` is the
 *  parent turn. The regression lives at subagentsRunning=true, turnRunning=false. */
function makeStore(opts: { subagentsRunning: boolean; turnRunning: boolean }) {
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
        activeSlot: 'slot-a', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: opts.turnRunning, slotStopping: false, slotState: 'idle',
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

async function renderChat(opts: { subagentsRunning: boolean; turnRunning: boolean }) {
  detail.running = opts.turnRunning
  slotsFixture.rows = [slotRow({ running: opts.turnRunning, subagents_running: opts.subagentsRunning })]
  const store = makeStore(opts)
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
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
  return { input: screen.getByLabelText('Message input') as HTMLTextAreaElement }
}

async function typeAndSubmit(input: HTMLTextAreaElement, text: string) {
  fireEvent.change(input, { target: { value: text } })
  await act(async () => {
    fireEvent.keyDown(input, { key: 'Enter' })
    await Promise.resolve()
  })
}

/** The steer flag is `sendChat`'s 6th argument. */
const steerArgOf = (call: unknown[]) => call[5]

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  sendChat.mockReset()
  sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
  steerChat.mockReset()
  steerChat.mockResolvedValue({ ok: true, steered: true })
})

describe('steer default while sub-agents run', { timeout: 20_000 }, () => {
  it('offers the split Steer button when only sub-agents are running', async () => {
    const { input } = await renderChat({ subagentsRunning: true, turnRunning: false })
    fireEvent.change(input, { target: { value: 'act on this now' } })

    const button = await waitFor(() => screen.getByTestId('busy-send-button'))
    // Label reflects the mode, so it doubles as the assertion that the default
    // is Steer rather than Queue.
    expect(button.getAttribute('aria-label')).toBe('Steer')
    expect(screen.getByTestId('busy-send-caret')).toBeInTheDocument()
  })

  it('sends with the steer flag instead of letting the server park the message', async () => {
    const { input } = await renderChat({ subagentsRunning: true, turnRunning: false })
    await typeAndSubmit(input, 'act on this now')

    await waitFor(() => expect(sendChat).toHaveBeenCalled())
    expect(steerArgOf(sendChat.mock.calls[0])).toBe(true)
    // No live turn exists, so the mid-turn injection endpoint must NOT be used:
    // it posts without `ws=1` and the fresh turn's output would go unread.
    expect(steerChat).not.toHaveBeenCalled()
  })

  it('honours an explicit Queue choice and leaves the flag off', async () => {
    localStorage.setItem('mc-busy-send-mode', 'queue')
    const { input } = await renderChat({ subagentsRunning: true, turnRunning: false })
    await typeAndSubmit(input, 'run this after the wave')

    await waitFor(() => expect(sendChat).toHaveBeenCalled())
    expect(steerArgOf(sendChat.mock.calls[0])).toBeFalsy()
  })

  it('still injects mid-turn when a real turn is running', async () => {
    const { input } = await renderChat({ subagentsRunning: true, turnRunning: true })
    await typeAndSubmit(input, 'change course')

    await waitFor(() => expect(steerChat).toHaveBeenCalled())
    expect(sendChat).not.toHaveBeenCalled()
  })

  it('leaves an ordinary idle send unflagged', async () => {
    const { input } = await renderChat({ subagentsRunning: false, turnRunning: false })
    await typeAndSubmit(input, 'plain message')

    await waitFor(() => expect(sendChat).toHaveBeenCalled())
    expect(steerArgOf(sendChat.mock.calls[0])).toBeFalsy()
  })
})
