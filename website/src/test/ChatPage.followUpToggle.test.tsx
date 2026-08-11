/**
 * Rapid clicks on a follow-up option must toggle, not append twice.
 *
 * The handler's add/remove predicate used to read the picked-set from the render
 * closure, so two clicks landing before a commit both took the append branch.
 *
 * These render the real ChatPage and click the real chip, so the shipped handler
 * runs — a suite that re-implements the handler locally passes with the fix
 * reverted. The starved-render window comes from advancing the chip's 220ms
 * debounce for both clicks inside one act(), so React cannot commit between them.
 * That precondition is asserted below, not assumed.
 *
 * Negative control: point the predicate back at `followUpPicked` and four of
 * these fail with "Deploy, Deploy".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/** The per-chip single-click debounce in FollowUpBar; one click = one onSelect. */
const CHIP_DEBOUNCE_MS = 220

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([{ key: 'chat-1', messages: 1, running: false, mode: '', project: '/repo' }]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
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
    dashboardConfig: vi.fn().mockResolvedValue({ quick_send: false }),
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
import { api } from '../api/client'

/** The marker has to close its own line for OPTION_MARKER_RE to match. */
const ASSISTANT_WITH_OPTIONS = 'Ready to proceed.\n\n[OPTIONS: Deploy | Roll back | Retry]'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: 'chat-1', messages: 1, running: false, mode: '', project: '/repo', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'chat-1', messages: [{ role: 'assistant', content: ASSISTANT_WITH_OPTIONS, cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
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

/** Render with real timers so the queries settle, then hand back to the caller. */
async function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={makeStore()}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Deploy' })).toBeTruthy())
}

const composer = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
/** Exact-name match: the send-now segment is a sibling button named "Send now: <option>". */
const chip = (option: string) => screen.getByRole('button', { name: option })

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.clearAllMocks()
  ;(api.dashboardConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ quick_send: false })
})
afterEach(() => { vi.useRealTimers() })

/** Fire one debounced chip click and let its onSelect run, without committing. */
function clickOption(option: string, opts: { shiftKey?: boolean } = {}) {
  fireEvent.click(chip(option), opts)
  vi.advanceTimersByTime(CHIP_DEBOUNCE_MS + 10)
}

describe('ChatPage follow-up option toggle', () => {
  it('appends the option text on a single click', async () => {
    await renderPage()
    vi.useFakeTimers()
    await act(async () => { clickOption('Deploy') })
    expect(composer().value).toBe('Deploy')
  })

  it('toggles off when the second click lands before React commits the first', async () => {
    // The reported defect: this appended "Deploy, Deploy" because the add/remove
    // predicate re-read the same uncommitted render state on both clicks.
    await renderPage()
    vi.useFakeTimers()
    await act(async () => {
      clickOption('Deploy')
      clickOption('Deploy')
    })
    expect(composer().value).toBe('')
  })

  it('does not commit between those two clicks (control for the case above)', async () => {
    // Without this, the case above would also pass on the unfixed handler if a
    // render happened to land between the clicks — the very thing that hid the bug.
    await renderPage()
    vi.useFakeTimers()
    let betweenClicks = 'unobserved'
    await act(async () => {
      clickOption('Deploy')
      betweenClicks = composer().value
      clickOption('Deploy')
    })
    // The single-click case proves a committed first click reads "Deploy", so an
    // empty value here can only mean no commit had landed when click two ran.
    expect(betweenClicks).toBe('')
    expect(composer().value).toBe('')
  })

  it('still toggles off when a render does land between the clicks', async () => {
    await renderPage()
    vi.useFakeTimers()
    await act(async () => { clickOption('Deploy') })
    expect(composer().value).toBe('Deploy')
    await act(async () => { clickOption('Deploy') })
    expect(composer().value).toBe('')
  })

  it('accumulates distinct options clicked in one uncommitted window', async () => {
    await renderPage()
    vi.useFakeTimers()
    await act(async () => {
      clickOption('Deploy')
      clickOption('Roll back')
      clickOption('Retry')
    })
    expect(composer().value).toBe('Deploy, Roll back, Retry')
  })

  it('removes a middle option without corrupting its neighbours', async () => {
    // Exercises the production splice, which tries the leading ", opt" before the
    // trailing "opt, " so a repeated label cannot splice the wrong occurrence.
    await renderPage()
    vi.useFakeTimers()
    await act(async () => {
      clickOption('Deploy')
      clickOption('Roll back')
      clickOption('Retry')
    })
    await act(async () => { clickOption('Roll back') })
    expect(composer().value).toBe('Deploy, Retry')
  })

  it('re-adds the option on a third click', async () => {
    await renderPage()
    vi.useFakeTimers()
    await act(async () => {
      clickOption('Deploy')
      clickOption('Deploy')
      clickOption('Deploy')
    })
    expect(composer().value).toBe('Deploy')
  })

  it('does not quick-send a second option while a selection is already open', async () => {
    // The one-click send path read the same stale set to decide "already in
    // multi-select", so it sent instead of extending the uncommitted selection.
    ;(api.dashboardConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ quick_send: true })
    await renderPage()
    vi.useFakeTimers()
    await act(async () => {
      clickOption('Deploy', { shiftKey: true })
      clickOption('Roll back')
    })
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(composer().value).toBe('Deploy, Roll back')
  })
})
