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
import chatReducer, { appendMessage } from '../store/chatSlice'
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
    // The sidebar's folder and board-column reads now report a failure through
    // an ErrorNotice (with a Retry button); an absent mock reads as a failure,
    // so answer them so the notice does not compete with the assertions below.
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
import { api } from '../api/client'

/** The marker has to close its own line for OPTION_MARKER_RE to match. */
const ASSISTANT_WITH_OPTIONS = 'Ready to proceed.\n\n[OPTIONS: Deploy | Roll back | Retry]'

/** Plan-shaped text (header, stage line, Go/Go All/Cancel footer): an ordinary options message. */
const ASSISTANT_WITH_PLAN = '📋 Plan for: ship it\n\nStage 1: build the thing\n\n[OPTION: Go | Go All | Cancel]'

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

/** Render with real timers so the queries settle, then hand back to the caller. */
async function renderPage(content = ASSISTANT_WITH_OPTIONS, mode = '', settleChip = 'Deploy', slot = 'chat-1') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  ;(api.chatSlots as ReturnType<typeof vi.fn>).mockResolvedValue([{ key: slot, messages: 1, running: false, mode, project: '/repo' }])
  const store = makeStore(content, mode, slot)
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
  await waitFor(() => expect(screen.getByRole('button', { name: settleChip })).toBeTruthy())
  return store
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

  it('un-toggle removes only the chip-owned suffix, not user text inserted mid-draft (#7616)', async () => {
    // Removal keys on WHAT THE CHIP APPENDED, not the picked-set content. A
    // user edit between two appends re-baselines ownership: un-toggling Stay
    // strips only the owned ", Stay" and keeps "Go and more". The pre-fix
    // content search removed the wrong span.
    await renderPage('Ready to proceed.\n\n[OPTIONS: Go | Stay]', '', 'Go')
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(composer().value).toBe('Go')
    fireEvent.change(composer(), { target: { value: 'Go and more' } })
    await act(async () => { clickOption('Stay') })
    expect(composer().value).toBe('Go and more, Stay')
    await act(async () => { clickOption('Stay') })
    expect(composer().value).toBe('Go and more')
  })

  it('un-toggle leaves a user-rewritten tail equal to the suffix untouched (#7616)', async () => {
    // The endsWith() bug: after the chip appends ", Go", the user rewrites the
    // whole draft to different text still ENDING with ", Go". The pre-fix
    // endsWith(', Go') would splice it to 'other'; ownership preserves it.
    await renderPage('Ready to proceed.\n\n[OPTIONS: Go | Stay]', '', 'Go')
    fireEvent.change(composer(), { target: { value: 'note' } })
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(composer().value).toBe('note, Go')
    fireEvent.change(composer(), { target: { value: 'other, Go' } })
    await act(async () => { clickOption('Go') })
    expect(composer().value).toBe('other, Go')
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

  it('replaces the previous pick on a single-select [OPTION:] offer', async () => {
    await renderPage('Ready to proceed.\n\n[OPTION: Deploy | Roll back | Retry]')
    vi.useFakeTimers()
    await act(async () => {
      clickOption('Deploy')
      clickOption('Roll back')
    })
    expect(composer().value).toBe('Roll back')
    await act(async () => { clickOption('Retry') })
    expect(composer().value).toBe('Retry')
    // Un-picking the live choice still clears it.
    await act(async () => { clickOption('Retry') })
    expect(composer().value).toBe('')
  })

  it('a newer [OPTION:] reply with the same labels keeps the earlier offer\'s unsent picks', async () => {
    // The picks belong to the older row; the fresh offer must not replace them.
    const store = await renderPage()
    vi.useFakeTimers()
    await act(async () => {
      clickOption('Deploy', { shiftKey: true })
      clickOption('Roll back')
    })
    expect(composer().value).toBe('Deploy, Roll back')
    await act(async () => {
      store.dispatch(appendMessage({ role: 'assistant', content: 'One more thing.\n\n[OPTION: Deploy | Roll back | Retry]', cls: '', ts: '2026-01-01T00:00:05Z' }))
    })
    await act(async () => { clickOption('Retry') })
    expect(composer().value).toBe('Deploy, Roll back, Retry')
  })

  it('a newer [OPTIONS:] reply with the same labels keeps the multi-select picks, as on base', async () => {
    // Only a single-select offer resets on a new source row; a multi-select row keeps
    // its picks, so clicking a still-picked chip removes it.
    const store = await renderPage()
    vi.useFakeTimers()
    await act(async () => {
      clickOption('Deploy', { shiftKey: true })
      clickOption('Roll back')
    })
    expect(composer().value).toBe('Deploy, Roll back')
    await act(async () => {
      store.dispatch(appendMessage({ role: 'assistant', content: 'One more thing.\n\n[OPTIONS: Deploy | Roll back | Retry]', cls: '', ts: '2026-01-01T00:00:05Z' }))
    })
    await act(async () => { clickOption('Deploy') })
    expect(composer().value).toBe('Roll back')
  })
})

describe('ChatPage plan-shaped follow-ups', () => {
  // A Go / Go All / Cancel footer is an ordinary options row: the chip edits the
  // composer or sends its text, whatever the slot's (legacy) mode says.
  it('a single click appends the chip text to the composer, even on a legacy orchestrator-mode slot', async () => {
    await renderPage(ASSISTANT_WITH_PLAN, 'orchestrator', 'Go')
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(composer().value).toBe('Go')
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('double-click sends the chip text as a chat message', async () => {
    await renderPage(ASSISTANT_WITH_PLAN, '', 'Go')
    fireEvent.doubleClick(chip('Go'))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(JSON.stringify((api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0])).toContain('"Go"')
  })

  it('Send now sends the chip text as a chat message', async () => {
    await renderPage(ASSISTANT_WITH_PLAN, '', 'Go')
    fireEvent.click(screen.getByRole('button', { name: 'Send now: Go All' }))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(JSON.stringify((api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0])).toContain('"Go All"')
  })
})
