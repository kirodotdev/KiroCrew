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
    planAction: vi.fn().mockResolvedValue({ ok: true }),
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

/** A plan needs BOTH the header and a stage line for parseOptions to set isPlan;
 *  the footer mirrors the plan pipeline's normalized template exactly. */
const ASSISTANT_WITH_PLAN = '📋 Plan for: ship it\n\nStage 1: build the thing\n\n[OPTION: Go | Go All | Cancel]'

/** Plan-SHAPED but carrying non-protocol labels — must keep the composer path. */
const ASSISTANT_PLAN_SHAPED_CUSTOM = '📋 Plan for: ship it\n\nStage 1: build the thing\n\n[OPTIONS: Approve it | Revise stage 2]'

function makeStore(content = ASSISTANT_WITH_OPTIONS, mode = '', slot = 'chat-1') {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slot, messages: 1, running: false, mode, project: '/repo', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: slot, messages: [{ role: 'assistant', content, cls: '' }],
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

/** Render with real timers so the queries settle, then hand back to the caller.
 *  `slot` is the active slot key: a test that DISPATCHES a plan action must pass
 *  a key no other test in this file dispatches on, because the hook's per-slot
 *  latches are module-level and a successful dispatch stays latched until the
 *  transcript acknowledges — which these static-store fixtures never simulate. */
async function renderPage(content = ASSISTANT_WITH_OPTIONS, mode = '', settleChip = 'Deploy', slot = 'chat-1') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  ;(api.chatSlots as ReturnType<typeof vi.fn>).mockResolvedValue([{ key: slot, messages: 1, running: false, mode, project: '/repo' }])
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={makeStore(content, mode, slot)}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByRole('button', { name: settleChip })).toBeTruthy())
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

  it('unselecting removes the appended option, not a matching substring in the draft', async () => {
    // Regression: `indexOf(', ' + option)` matched the ", Go" inside
    // "Please, Google" before the option the handler appended at the end.
    await renderPage('Ready to proceed.\n\n[OPTIONS: Go | Stay]', '', 'Go')
    fireEvent.change(composer(), { target: { value: 'Please, Google' } })
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(composer().value).toBe('Please, Google, Go')
    await act(async () => { clickOption('Go') })
    expect(composer().value).toBe('Please, Google')
  })

  it('leaves earlier draft text alone when the user already deleted the appended option', async () => {
    await renderPage('Ready to proceed.\n\n[OPTIONS: Go | Stay]', '', 'Go')
    fireEvent.change(composer(), { target: { value: 'Discuss, Go home' } })
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(composer().value).toBe('Discuss, Go home, Go')

    // The chip remains selected, but the user removes its generated tail by hand.
    // Unselecting must not fall back to the earlier ", Go" inside their draft.
    fireEvent.change(composer(), { target: { value: 'Discuss, Go home' } })
    await act(async () => { clickOption('Go') })
    expect(composer().value).toBe('Discuss, Go home')
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

describe('ChatPage plan follow-ups (issue #5893 parity)', () => {
  // Each test that DISPATCHES uses its OWN slot key. The hook's per-slot
  // latches are module-level and survive vi.clearAllMocks(), and a SUCCESSFUL
  // dispatch stays latched until the transcript acknowledges — which these
  // static-store fixtures never simulate. Unique keys make the collision
  // structurally impossible, so no reset hook (and no production-facing
  // release escape hatch on the hook) is needed.

  it('a plan chip in orchestrator mode dispatches the plan action and never touches the composer', async () => {
    await renderPage(ASSISTANT_WITH_PLAN, 'orchestrator', 'Go', 'chat-plan-dispatch')
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(api.planAction).toHaveBeenCalledTimes(1)
    expect(api.planAction).toHaveBeenCalledWith('chat-plan-dispatch', 'Go')
    expect(composer().value).toBe('')
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a plan-shaped message with NON-protocol labels keeps the composer path (allowlist gate)', async () => {
    // The endpoint accepts only go / go all / cancel; a plan-shaped message
    // quoting a plan while offering its own choices must compose text, not
    // fire a dispatch the server would 400 (which also skips the append —
    // a dead chip). This pins the allowlist ON THE MAIN SURFACE, so a future
    // server-side action added without updating isPlanAction fails a test
    // here instead of silently degrading to composer text.
    await renderPage(ASSISTANT_PLAN_SHAPED_CUSTOM, 'orchestrator', 'Approve it', 'chat-plan-allowlist')
    vi.useFakeTimers()
    await act(async () => { clickOption('Approve it') })
    expect(composer().value).toBe('Approve it')
    expect(api.planAction).not.toHaveBeenCalled()
  })

  it('double-click on a plan chip dispatches the plan action, never sendChat (issue #6240)', async () => {
    await renderPage(ASSISTANT_WITH_PLAN, 'orchestrator', 'Go', 'chat-plan-dbl')
    fireEvent.doubleClick(chip('Go'))
    await waitFor(() => expect(api.planAction).toHaveBeenCalledTimes(1))
    expect(api.planAction).toHaveBeenCalledWith('chat-plan-dbl', 'Go')
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(composer().value).toBe('')
  })

  it('Send now on a plan chip dispatches the plan action, never sendChat (issue #6240)', async () => {
    await renderPage(ASSISTANT_WITH_PLAN, 'orchestrator', 'Go', 'chat-plan-sendnow')
    fireEvent.click(screen.getByRole('button', { name: 'Send now: Go All' }))
    await waitFor(() => expect(api.planAction).toHaveBeenCalledTimes(1))
    expect(api.planAction).toHaveBeenCalledWith('chat-plan-sendnow', 'Go All')
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(composer().value).toBe('')
  })
})
