import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setQuestionCard, clearQuestionCard, appendSlotMessage } from '../store/chatSlice'
import dashboardReducer, { updateSlot } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { FOLLOWUP_CHIP_DEBOUNCE_MS } from '../components/FollowUpBar'

/* A grid pane must surface the agent's follow-up [OPTIONS:] choices
 * (issue #5870): ChatMessageList strips the marker from the transcript, so a
 * ChatPane that never passes followUpOptions to ChatInput silently drops the
 * choices — the user has to retype them by hand. These tests pin the ChatPage
 * wiring mirrored into ChatPane: pills render from the last assistant message,
 * are suppressed while the pane is busy or a question card is up, and a pick
 * routes through the pane's own send path. */


vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
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
    dashboardConfig: vi.fn().mockResolvedValue({ quick_send: false }),
    planAction: vi.fn().mockResolvedValue({ ok: true }),
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
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api, ApiError } from '../api/client'

/** The marker has to close its own line for OPTION_MARKER_RE to match. */
const ASSISTANT_WITH_OPTIONS = 'Ready to proceed.\n\n[OPTIONS: Alpha | Beta]'

/** A plan needs BOTH the header and a stage line for parseOptions to set isPlan.
 *  The footer mirrors the plan pipeline's template exactly: every plan that
 *  reaches a transcript is normalized to `[OPTION: Go | Go All | Cancel]`, and
 *  those are also the only actions the plan endpoint accepts. */
const ASSISTANT_WITH_PLAN = '📋 Plan for: ship it\n\nStage 1: build the thing\n\n[OPTION: Go | Go All | Cancel]'

/** Plan-SHAPED (header + stage line) but carrying non-protocol labels — e.g. an
 *  agent quoting a plan while offering its own choices. Must keep the composer path. */
const ASSISTANT_PLAN_SHAPED_CUSTOM = '📋 Plan for: ship it\n\nStage 1: build the thing\n\n[OPTIONS: Approve it | Revise stage 2]'

const PANE_MESSAGES = [
  { role: 'user', content: 'hi', ts: '2026-08-25T00:00:00Z' },
  { role: 'assistant', content: ASSISTANT_WITH_OPTIONS, ts: '2026-08-25T00:00:01Z' },
]

const PLAN_MESSAGES = [
  { role: 'user', content: 'plan it', ts: '2026-08-25T00:00:00Z' },
  { role: 'assistant', content: ASSISTANT_WITH_PLAN, ts: '2026-08-25T00:00:01Z' },
]

function makeStore(slotKey: string, slotExtra: Record<string, unknown> = {}) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...slotExtra }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

async function renderPane(slotKey: string, slotExtra: Record<string, unknown> = {}, messages = PANE_MESSAGES) {
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages, running: false, has_more: false, total: messages.length })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey, slotExtra)
  await act(async () => {
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={slotKey} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
  })
  // Hydration is settled once the transcript shows the assistant's prose.
  const settled = messages.some(m => m.content.includes('Plan for')) ? /Plan for: ship it/ : /Ready to proceed/
  await waitFor(() => expect(screen.getByText(settled)).toBeTruthy())
  return store
}

const composer = () => (screen.getAllByRole('textbox')[0]) as HTMLTextAreaElement
const chip = (option: string) => screen.getByRole('button', { name: option })

/** Fire one debounced chip click and let its onSelect run (fake timers active). */
function clickOption(option: string, opts: { shiftKey?: boolean } = {}) {
  fireEvent.click(chip(option), opts)
  vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 10)
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.clearAllMocks()
})
afterEach(() => { vi.useRealTimers() })

describe('ChatPane follow-up options (issue #5870)', () => {
  it('renders the last assistant message\'s [OPTIONS:] choices as pills', async () => {
    await renderPane('pane-1')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Beta' })).toBeTruthy()
  })

  it('clicking a pill fills the composer, and Enter sends through the pane\'s send path', async () => {
    await renderPane('pane-2')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composer().value).toBe('Alpha')
    vi.useRealTimers()
    fireEvent.keyDown(composer(), { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, slot] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('Alpha')
    expect(slot).toBe('pane-2')
  })

  it('double-click sends the option label directly through the pane\'s send path', async () => {
    await renderPane('pane-3')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Beta' })).toBeTruthy())
    fireEvent.doubleClick(chip('Beta'))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, slot] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('Beta')
    expect(slot).toBe('pane-3')
  })

  it('an option send never consumes the composer draft (clear-without-send guard)', async () => {
    // ChatPage.send gates its clear cluster on `if (!optionText)` — the pane
    // must hold the same invariant: a direct-send of an option label supplies
    // its own text, so the user's typed draft stays in the composer instead of
    // being wiped by a message they never composed.
    await renderPane('pane-6')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    fireEvent.change(composer(), { target: { value: 'my unsent draft' } })
    fireEvent.doubleClick(chip('Alpha'))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('Alpha')
    expect(composer().value).toBe('my unsent draft')
  })

  it('unselecting an option splices its own appended text, never a matching substring of the draft', async () => {
    // Regression: `indexOf(', ' + option)` can match INSIDE the draft — draft
    // "Please, Alphabet" + option "Alpha" would splice mid-word on unselect.
    // The handler appends at the END, so it must remove the LAST occurrence.
    await renderPane('pane-7')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    fireEvent.change(composer(), { target: { value: 'Please, Alphabet' } })
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composer().value).toBe('Please, Alphabet, Alpha')
    await act(async () => { clickOption('Alpha') })
    expect(composer().value).toBe('Please, Alphabet')
  })

  it('offers no pills while the pane is busy, and offers them once busy clears', async () => {
    // selectComposerBusy reads the dashboard slot's subagents_running flag —
    // the same composer-busy rule that queues sends — so the derive gate must
    // suppress the pills for the whole busy window, mirroring ChatPage's
    // isStreaming argument to deriveFollowUpOptions.
    const store = await renderPane('pane-4', { subagents_running: true })
    expect(screen.queryByRole('button', { name: 'Alpha' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Beta' })).toBeNull()
    // Positive control in the same test: flipping busy off makes the pills
    // appear, so the nulls above prove the gate rather than a render break.
    await act(async () => { store.dispatch(updateSlot({ key: 'pane-4', subagents_running: false })) })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
  })

  it('suppresses pills while a pending question card is up, and restores them when it clears', async () => {
    const store = await renderPane('pane-5')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    await act(async () => {
      store.dispatch(setQuestionCard({ slot: 'pane-5', questions: [{ question: 'Which one?', options: [{ label: 'Card-X' }] }] }))
    })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Alpha' })).toBeNull())
    // Positive control in the same test: the pills return once the card is
    // gone, so the null above proves the gate, not an unrelated render break.
    await act(async () => { store.dispatch(clearQuestionCard({ slot: 'pane-5' })) })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
  })
})

describe('ChatPane plan follow-ups dispatch (issue #5893)', () => {
  // LOAD-BEARING: every test in this block uses a UNIQUE slot key. The hook's
  // single-flight latches are module-level and vi.clearAllMocks() does not
  // reset them, so a never-resolving-mock test latches its slot for the rest
  // of the file — reusing a key would silently drop that test's dispatch.
  it('a plan chip in an orchestrator pane dispatches the plan action and never touches the composer', async () => {
    await renderPane('pane-plan-1', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(api.planAction).toHaveBeenCalledTimes(1)
    expect(api.planAction).toHaveBeenCalledWith('pane-plan-1', 'Go')
    // The label must NOT fall through to the composer-append path: before the
    // fix the click typed the literal label into the composer, one Enter away
    // from being sent to the agent as an ordinary chat message.
    expect(composer().value).toBe('')
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a NON-plan chip in an orchestrator pane still appends to the composer (plain follow-ups unaffected)', async () => {
    await renderPane('pane-plan-2', { mode: 'orchestrator' }, PANE_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composer().value).toBe('Alpha')
    expect(api.planAction).not.toHaveBeenCalled()
  })

  it('a plan-shaped chip outside orchestrator mode falls through to the composer (same mode gate as ChatPage)', async () => {
    await renderPane('pane-plan-3', { mode: '' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(composer().value).toBe('Go')
    expect(api.planAction).not.toHaveBeenCalled()
  })

  it('a plan-shaped message with NON-protocol labels keeps the composer path (allowlist gate)', async () => {
    // The endpoint accepts only go / go all / cancel; dispatching anything
    // else would 400 server-side while the append path was already skipped —
    // a dead chip. Such a message is reachable: an agent quoting a plan while
    // offering its own choices trips the plan-shape detector.
    const custom = [
      { role: 'user', content: 'plan it', ts: '2026-08-25T00:00:00Z' },
      { role: 'assistant', content: ASSISTANT_PLAN_SHAPED_CUSTOM, ts: '2026-08-25T00:00:01Z' },
    ]
    await renderPane('pane-plan-6', { mode: 'orchestrator' }, custom)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve it' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Approve it') })
    expect(composer().value).toBe('Approve it')
    expect(api.planAction).not.toHaveBeenCalled()
  })

  it('a plan chip is a NO-OP while the slot record is unresolved (never appends an approval label)', async () => {
    // On a reload with a restored grid the pane hydrates its transcript from
    // the detail fetch before the first WS slots snapshot lands, so paneSlot
    // can be undefined while the chips are already clickable. The mode is
    // unknown in that window: dispatching is unsafe (the slot may not be an
    // orchestrator) and appending re-creates the reported bug — so the click
    // must do nothing at all.
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: PLAN_MESSAGES, running: false, has_more: false, total: PLAN_MESSAGES.length })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = configureStore({
      reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
      preloadedState: {
        dashboard: {
          status: null, connected: true,
          slots: [], // first slots snapshot not yet delivered
          unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
          subagentRunning: {}, subagentDetails: {}, subagentText: {},
        } as unknown as RootState['dashboard'],
      } as Partial<RootState>,
    })
    await act(async () => {
      render(
        <Provider store={store}>
          <QueryClientProvider client={qc}>
            <ThemeProvider>
              <MemoryRouter>
                <ChatPane slotKey="pane-plan-7" />
              </MemoryRouter>
            </ThemeProvider>
          </QueryClientProvider>
        </Provider>,
      )
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(api.planAction).not.toHaveBeenCalled()
    expect(composer().value).toBe('')
  })

  it('a second click while the dispatch is pending does not fire twice (re-entrancy across renders)', async () => {
    // Never-resolving promise keeps the mutation pending across both clicks.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => new Promise(() => {}))
    await renderPane('pane-plan-4', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { clickOption('Go') })
    expect(api.planAction).toHaveBeenCalledTimes(1)
  })

  it('two stage-advancing chips landing in the SAME tick dispatch once (synchronous latch)', async () => {
    // `mutation.isPending` is a render snapshot: two onSelect callbacks firing
    // before the next render both read false. Without a synchronous latch a
    // rapid Go followed by Go All submits two stage-advancing actions and the
    // plan advances an extra stage. Both debounce timers are advanced inside
    // ONE act, so no render happens between the two dispatches — only the
    // hook's per-slot in-flight latch can stop the second.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => new Promise(() => {}))
    await renderPane('pane-plan-5', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => {
      fireEvent.click(chip('Go'))
      fireEvent.click(chip('Go All'))
      vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 10)
    })
    expect(api.planAction).toHaveBeenCalledTimes(1)
    expect(api.planAction).toHaveBeenCalledWith('pane-plan-5', 'Go')
  })

  it('Cancel goes through while a Go is still in flight (the stop control is never swallowed)', async () => {
    // The Go latch guards the stage-advancing actions only. A user who clicks
    // Go and immediately realises the plan is wrong must be able to Cancel
    // inside the request window — dropping it would advance a stage they
    // tried to stop.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => new Promise(() => {}))
    await renderPane('pane-plan-8', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { clickOption('Cancel') })
    expect(api.planAction).toHaveBeenCalledTimes(2)
    expect(api.planAction).toHaveBeenLastCalledWith('pane-plan-8', 'Cancel')
  })

  it('a second Cancel while one is in flight is dropped (double-Cancel dedupe)', async () => {
    // Cancel bypasses the GO latch but dedupes against itself: the server
    // only guards tracker.stop() — the "Plan cancelled" transcript append and
    // its broadcasts run on every POST, so an unlatched double-Cancel writes
    // duplicate transcript rows.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => new Promise(() => {}))
    await renderPane('pane-plan-10', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Cancel') })
    await act(async () => { clickOption('Cancel') })
    expect(api.planAction).toHaveBeenCalledTimes(1)
  })

  it('a DEFINITIVE 4xx rejection releases its own class for retry — and only its own', async () => {
    // A 4xx the server produced before touching the plan is the ONLY
    // HTTP-driven release (success consumes until transcript ack, and an
    // ambiguous failure keeps the latch — see the three tests below). Per
    // class: a definitively-rejected Cancel must free retry-Cancel while a
    // still-pending Go stays latched.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(
      (_slot: string, action: string) => action === 'Cancel' ? Promise.reject(new ApiError(400, 'unknown action')) : new Promise(() => {}),
    )
    await renderPane('pane-plan-11', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })      // latches the Go set, never settles
    await act(async () => { clickOption('Cancel') })  // 400 → releases the CANCEL set only
    await act(async () => { await Promise.resolve() })
    await act(async () => { clickOption('Cancel') })  // retry allowed after the definitive rejection
    expect(api.planAction).toHaveBeenCalledTimes(3)
    expect(api.planAction).toHaveBeenLastCalledWith('pane-plan-11', 'Cancel')
    await act(async () => { clickOption('Go All') })  // the pending Go's latch was untouched
    expect(api.planAction).toHaveBeenCalledTimes(3)
  })

  it('a 5xx failure KEEPS the latch — the server may have committed and lost the response', async () => {
    // The transport-ambiguity gap: the plan-action handler can raise after
    // `queue_append` has already landed, so a 500 does not prove the action
    // did not run. Releasing here turns one user click into two server-side
    // Go turns (or two "🛑 Plan cancelled." transcript rows). A latch stuck
    // until the next plan row is the strictly safer failure.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.reject(new ApiError(500, 'internal error')))
    await renderPane('pane-plan-16', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Cancel') })
    await act(async () => { await Promise.resolve() })
    await act(async () => { clickOption('Cancel') })  // must be DROPPED — still latched
    expect(api.planAction).toHaveBeenCalledTimes(1)
  })

  it('a TRANSPORT rejection with no response KEEPS the latch', async () => {
    // `fetch` rejecting (offline tab, TCP reset, DNS) never reaches `j()`, so
    // there is no ApiError and no status — the request may have been fully
    // served with only the response lost. Indistinguishable from "never
    // arrived", therefore treated as ambiguous and not released.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.reject(new TypeError('Failed to fetch')))
    await renderPane('pane-plan-17', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { await Promise.resolve() })
    await act(async () => { clickOption('Go') })      // must be DROPPED — still latched
    expect(api.planAction).toHaveBeenCalledTimes(1)
  })

  it.each([408, 429])('a retryable %i KEEPS the latch even though it is a 4xx', async (status) => {
    // 4xx status, ambiguous meaning: 408 is an edge giving up on a request it
    // may already have forwarded, and 429 is the tunnel throttle the
    // QueryClient itself treats as retryable. A retryable rejection is by
    // definition not proof the plan was left untouched, so the definitive-4xx
    // release must exclude both rather than keying on the 4xx range alone.
    const slot = `pane-plan-retryable-${status}`
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.reject(new ApiError(status, `HTTP ${status}`)))
    await renderPane(slot, { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { await Promise.resolve() })
    await act(async () => { clickOption('Go') })      // must be DROPPED — still latched
    expect(api.planAction).toHaveBeenCalledTimes(1)
  })

  it('a Go superseded by a Cancel on the same hook instance still runs its error release', async () => {
    // React Query builds a fresh Mutation per mutate() and only detaches the
    // observer from the old one — the superseded Go keeps running and fires
    // its own options-level onError. If a query-core upgrade ever changed
    // that, a failed Go would latch the slot forever with every other test
    // green, so the property is pinned rather than assumed from internals.
    // The Go is rejected with a DEFINITIVE 4xx so the release is the only
    // thing under test here: an ambiguous failure would retain the latch by
    // design and this test could no longer see whether onError ran at all.
    let rejectGo!: (e: unknown) => void
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(
      (_slot: string, action: string) => action === 'Go'
        ? new Promise((_r, rej) => { rejectGo = rej })
        : Promise.resolve({ ok: true }),
    )
    await renderPane('pane-plan-12', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })      // latches, deferred
    await act(async () => { clickOption('Cancel') })  // supersedes the observer's mutation
    await act(async () => { rejectGo(new ApiError(409, 'slot not planning')); await Promise.resolve() })
    await act(async () => { clickOption('Go') })      // the failed Go must have freed the slot
    expect(api.planAction).toHaveBeenCalledTimes(3)
    expect(api.planAction).toHaveBeenLastCalledWith('pane-plan-12', 'Go')
  })

  it('an IDENTICAL next-stage footer in a single hydration write still releases (row identity, not labels)', async () => {
    // Reconnect recovery hydrates the whole transcript in one write: if stage
    // 2's footer has the same `Go | Go All | Cancel` labels, an options-LABEL
    // key never changes and a label-keyed latch would drop the legitimate
    // stage-2 click forever. The latch is keyed on the options-bearing ROW
    // (its ts/mid), which differs between the two footers.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve({ ok: true }))
    const store = await renderPane('pane-plan-13', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { await Promise.resolve() })
    await act(async () => { clickOption('Go All') })  // stale window: dropped
    expect(api.planAction).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
    // One write, no empty-options interlude: a NEW footer row, same labels.
    await act(async () => {
      store.dispatch(appendSlotMessage({ slot: 'pane-plan-13', message: { role: 'assistant', content: ASSISTANT_WITH_PLAN, ts: '2026-08-25T00:03:00Z' } as never }))
    })
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Go' }).length).toBeGreaterThan(0))
    vi.useFakeTimers()
    await act(async () => { clickOption('Go All') })
    expect(api.planAction).toHaveBeenCalledTimes(2)
    expect(api.planAction).toHaveBeenLastCalledWith('pane-plan-13', 'Go All')
  })

  it('a pane REMOUNT on a latched slot with a warm cache does not release (same stale row)', async () => {
    // The grid swaps element types on split/close, unmounting and remounting
    // a pane; with staleTime:Infinity the remount is served the cached,
    // pre-dispatch transcript with no network call. The acknowledgement is a
    // function of the observed ROW — the remounted pane re-derives the SAME
    // stale row, so the latch must survive and the re-click must be dropped.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve({ ok: true }))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: PLAN_MESSAGES, running: false, has_more: false, total: PLAN_MESSAGES.length })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-plan-14', { mode: 'orchestrator' })
    const ui = (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey="pane-plan-14" />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>
    )
    const first = render(ui)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { await Promise.resolve() })
    expect(api.planAction).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
    first.unmount()
    // Remount with the SAME query client and store: warm cache, no refetch,
    // the same stale chips render immediately.
    render(ui)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(api.planAction).toHaveBeenCalledTimes(1)  // still latched — dropped
  })

  it('a LATE failure from an old dispatch does not free a newer dispatch\'s latch', async () => {
    // Sequence: Go on footer A never got its response; the transcript moves
    // to footer B (ack frees the slot); the user dispatches Go on footer B;
    // THEN footer A's request finally fails. The stale onError must not
    // delete footer B's latch — that would re-open a duplicate submit.
    // Footer A fails with a DEFINITIVE 4xx, i.e. an error that IS eligible
    // for release: otherwise the ambiguity classifier would hold the latch on
    // its own and this test would pass without exercising the source-key
    // guard it exists to pin.
    let rejectFirstGo!: (e: unknown) => void
    let call = 0
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => {
      call += 1
      return call === 1 ? new Promise((_r, rej) => { rejectFirstGo = rej }) : new Promise(() => {})
    })
    const store = await renderPane('pane-plan-15', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })              // dispatch 1, hangs
    expect(api.planAction).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
    await act(async () => {
      store.dispatch(appendSlotMessage({ slot: 'pane-plan-15', message: { role: 'assistant', content: ASSISTANT_WITH_PLAN, ts: '2026-08-25T00:05:00Z' } as never }))
    })
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Go' }).length).toBeGreaterThan(0))
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })              // dispatch 2 on footer B (ack freed the slot)
    expect(api.planAction).toHaveBeenCalledTimes(2)
    await act(async () => { rejectFirstGo(new ApiError(409, 'stale plan row')); await Promise.resolve() })
    await act(async () => { clickOption('Go All') })          // must STAY blocked by dispatch 2's latch
    expect(api.planAction).toHaveBeenCalledTimes(2)
  })

  it('a SUCCESSFUL dispatch stays latched until the transcript acknowledges, then frees', async () => {
    // The core of the held-until-ack contract: an HTTP 200 is not proof the
    // user saw anything. With the WS down the pane keeps rendering the STALE
    // chips of a plan that already advanced — a re-click must be dropped
    // (releasing on success would queue_append an unintended extra Go turn).
    // The release signal is the derived options CHANGING: chips clear while
    // the stage runs, then the next footer re-offers them.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve({ ok: true }))
    const store = await renderPane('pane-plan-9', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { await Promise.resolve() })   // let the 200 land
    // Stale-chip window (WS down): the chips have not moved, so a re-click
    // must be swallowed even though the request succeeded.
    await act(async () => { clickOption('Go All') })
    expect(api.planAction).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
    // The stream comes back: the stage's assistant text clears the chips…
    await act(async () => {
      store.dispatch(appendSlotMessage({ slot: 'pane-plan-9', message: { role: 'assistant', content: 'Running stage 1…', ts: '2026-08-25T00:01:00Z' } as never }))
    })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Go' })).toBeNull())
    // …and the next stage's footer re-offers them. The ack released the latch.
    await act(async () => {
      store.dispatch(appendSlotMessage({ slot: 'pane-plan-9', message: { role: 'assistant', content: ASSISTANT_WITH_PLAN, ts: '2026-08-25T00:02:00Z' } as never }))
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go All' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go All') })
    expect(api.planAction).toHaveBeenCalledTimes(2)
    expect(api.planAction).toHaveBeenLastCalledWith('pane-plan-9', 'Go All')
  })

  it('a pane dispatches against its OWN slot, not another pane\'s (slot isolation)', async () => {
    // Two live panes, plan chips in both; the dispatch from pane B must carry
    // pane B's slot key — the regression most likely to slip through a copy
    // of ChatPage's handler, which uses the page-global active slot.
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: PLAN_MESSAGES, running: false, has_more: false, total: PLAN_MESSAGES.length })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = configureStore({
      reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
      preloadedState: {
        dashboard: {
          status: null, connected: true,
          slots: [
            { key: 'pane-fg', messages: 0, running: false, mode: 'orchestrator', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
            { key: 'pane-bg', messages: 0, running: false, mode: 'orchestrator', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
          ],
          unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
          subagentRunning: {}, subagentDetails: {}, subagentText: {},
        } as unknown as RootState['dashboard'],
      } as Partial<RootState>,
    })
    let container!: HTMLElement
    await act(async () => {
      ;({ container } = render(
        <Provider store={store}>
          <QueryClientProvider client={qc}>
            <ThemeProvider>
              <MemoryRouter>
                <div>
                  <ChatPane slotKey="pane-fg" />
                  <ChatPane slotKey="pane-bg" />
                </div>
              </MemoryRouter>
            </ThemeProvider>
          </QueryClientProvider>
        </Provider>,
      ))
    })
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Go' })).toHaveLength(2))
    const panes = container.querySelectorAll('[data-chat-pane]')
    expect(panes).toHaveLength(2)
    const bgChip = within(panes[1] as HTMLElement).getByRole('button', { name: 'Go' })
    vi.useFakeTimers()
    await act(async () => {
      fireEvent.click(bgChip)
      vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 10)
    })
    expect(api.planAction).toHaveBeenCalledTimes(1)
    expect(api.planAction).toHaveBeenCalledWith('pane-bg', 'Go')
  })

  it('double-click on a plan chip dispatches the plan action, never sendChat (issue #6240)', async () => {
    // Single-click is the #5893 / #6040 path. Double-click used to call
    // onSend(label) and type "Go" / "Cancel" as ordinary chat.
    await renderPane('pane-plan-dbl', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    fireEvent.doubleClick(chip('Go'))
    await waitFor(() => expect(api.planAction).toHaveBeenCalledTimes(1))
    expect(api.planAction).toHaveBeenCalledWith('pane-plan-dbl', 'Go')
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(composer().value).toBe('')
  })

  it('Send now on a plan chip dispatches the plan action, never sendChat (issue #6240)', async () => {
    // The visible split-button segment is the discoverable form of the same
    // onSend bypass. Cancel is the sharp edge: a typed Cancel never stops the
    // plan.
    await renderPane('pane-plan-sendnow', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Send now: Cancel' }))
    await waitFor(() => expect(api.planAction).toHaveBeenCalledTimes(1))
    expect(api.planAction).toHaveBeenCalledWith('pane-plan-sendnow', 'Cancel')
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(composer().value).toBe('')
  })

  it('a double-click whose footer is REPLACED between clicks never dispatches (issue #6240 race)', async () => {
    // First click of a double-click arms the chip with row-1. A byte-identical
    // replacement footer reuses the chip; the second click must hand the
    // FIRST-click key to onSend so the hook refuses the replacement stage.
    const store = await renderPane('pane-plan-dbl-stale', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { fireEvent.click(chip('Go'), { detail: 1 }) })
    await act(async () => {
      store.dispatch(appendSlotMessage({
        slot: 'pane-plan-dbl-stale',
        message: { role: 'assistant', content: ASSISTANT_WITH_PLAN, ts: '2026-08-25T00:06:00Z' } as never,
      }))
    })
    await act(async () => {
      fireEvent.click(chip('Go'), { detail: 2 })
      fireEvent.doubleClick(chip('Go'))
    })
    expect(api.planAction).not.toHaveBeenCalled()
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(composer().value).toBe('')
  })

  it('Send now on a NON-plan chip still sends the label through the pane\'s send path', async () => {
    await renderPane('pane-plan-sendnow-plain', { mode: 'orchestrator' }, PANE_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Send now: Alpha' }))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, slot] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('Alpha')
    expect(slot).toBe('pane-plan-sendnow-plain')
    expect(api.planAction).not.toHaveBeenCalled()
  })

  it('a click whose footer is REPLACED during the 220ms debounce never dispatches (host forwards the click-time row)', async () => {
    // End-to-end proof of the wiring, not just the hook: FollowUpBar snapshots
    // its sourceKey at click time, ChatInput forwards it, and the pane hands it
    // to mutate as clickedSourceKey. Without every link the pending timer fires
    // against the NEW row (the acknowledgement effect already freed the latch
    // for it) and approves a stage the user never saw.
    //
    // Fake timers stay on across the store dispatch on purpose: the pane
    // derives its options synchronously from redux, so `act` alone flushes the
    // new row — and switching to real timers here would discard the pending
    // debounce this test exists to fire.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve({ ok: true }))
    const store = await renderPane('pane-plan-18', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    // Click, but do NOT let the debounce elapse yet.
    await act(async () => { fireEvent.click(chip('Go')) })
    expect(api.planAction).not.toHaveBeenCalled()
    // A NEW footer row with byte-identical labels lands: same chips, no
    // remount, so the pending timer is still armed on the OLD row.
    await act(async () => {
      store.dispatch(appendSlotMessage({ slot: 'pane-plan-18', message: { role: 'assistant', content: ASSISTANT_WITH_PLAN, ts: '2026-08-25T00:04:00Z' } as never }))
    })
    await act(async () => { vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 10) })
    expect(api.planAction).not.toHaveBeenCalled()
    // The composer is untouched too — a refused plan click must not fall
    // through to the append path (that is #5893 itself).
    expect(composer().value).toBe('')
    // And the refusal did not wedge the new row: a fresh click on it goes.
    await act(async () => { clickOption('Go') })
    expect(api.planAction).toHaveBeenCalledTimes(1)
  })
})
