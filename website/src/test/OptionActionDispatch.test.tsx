/**
 * Zero-turn option actions — the DISPATCH layer, in both hosts.
 *
 * An `[OPTION-ACTIONS:]` chip runs a local action instead of sending its label as
 * chat text, so clicking one must start NO LLM turn. The action exists to leave a
 * record of the click, which makes the ORDER load-bearing: durable breadcrumb
 * first, close second, and the close gated on the breadcrumb actually landing.
 *
 * The gate is measured, not theoretical. A note POSTed while a turn is in flight
 * answers `appended:false, visibleDeferred:true` and is held IN MEMORY, not yet
 * durably recorded, and the close answers 200 either way so there is no error to
 * catch afterwards. `close_slot` DOES now flush held notes — this PR adds that — so
 * a close no longer destroys one; the gate stands because a note the backend has not
 * committed is still not the record the action exists to leave. Server-side
 * counterpart: the `TestDeferredNoteLostOnClose` suite.
 *
 * ## What these render, and why
 *
 * The REAL hosts, the REAL `ChatInput`, and the REAL `FollowUpBar` action chip —
 * so the shipped handler runs. A suite that re-implements the handler locally
 * passes with the sequencing reverted.
 *
 * `ChatInput` is wrapped rather than replaced: the wrapper renders the real
 * component and adds two test-only buttons that invoke `onFollowUpSelect` and
 * `onFollowUpSend` directly. Those two CONTENT seams cannot be reached through the
 * shipped action chip at all — `ActionChip` closes over neither callback, and both
 * are passed only to `Chip`, which renders solely from the content `options` array.
 * The buttons exist to drive the CONTENT path directly and prove an action row does
 * not disturb it; the action path itself is exercised through the real chip in the
 * same file.
 *
 * Negative controls, verified by hand while writing these:
 *  - make the dispatch close unconditionally → `refuses to close on a deferred
 *    note` fails in both hosts;
 *  - swap `close(slot)` for a bare `deleteSlot` dispatch → `honours the
 *    confirm-close preference` fails.
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

/** Label the test-only seam buttons hand to the content callbacks. Mutable so a
 *  test can aim a leak at a specific label without re-mocking the module. */
let seamLabel = ''

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))

// The real ChatInput plus two probes onto the CONTENT seams. Both hosts import
// this same module id, so one mock covers ChatPage and ChatPane.
vi.mock('../components/ChatInput', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../components/ChatInput')>()
  const Real = mod.default
  type Probed = {
    followUpSourceKey?: string | null
    onFollowUpSelect?: (option: string, event: React.MouseEvent, sourceKeyAtClick?: string | null) => void
    onFollowUpSend?: (text?: string) => void
  }
  return {
    ...mod,
    default: (props: Probed & Record<string, unknown>) => (
      <>
        <Real {...(props as never)} />
        <button
          type="button"
          data-testid="seam-select"
          onClick={(e) => props.onFollowUpSelect?.(seamLabel, e, props.followUpSourceKey)}
        >
          seam-select
        </button>
        <button
          type="button"
          data-testid="seam-send"
          onClick={() => props.onFollowUpSend?.(seamLabel)}
        >
          seam-send
        </button>
      </>
    ),
  }
})

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatSlotNote: vi.fn(),
    deleteChatSlot: vi.fn().mockResolvedValue({ ok: true }),
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
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
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
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
// `CliPanel` side-imports `@xterm/xterm/css/xterm.css`, and the CSS pipeline pulls
// in `tailwind.config.js` → `tailwindcss-animate`. A factory mock means the real
// module is never loaded, so its stylesheet is never transformed — which is what
// makes a ChatPage render possible here at all. Nothing in this file asserts on
// the terminal.
vi.mock('../components/CliPanel', () => ({ default: () => null }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

/** The action label the canonical marker offers. Free text on purpose — it carries
 *  a comma and spaces, the shape a `,`-splitting parser would tear in half. */
const ACTION_LABEL = 'Nothing else, close this tab'
/** A marker has to close its own line for the protocol regexes to match. */
const ACTION_ONLY = `All done.\n\n[OPTION-ACTIONS: close=${ACTION_LABEL}]`
const OPTIONS_AND_ACTION = `Pick one.\n\n[OPTIONS: Keep going | Show the diff]\n\n[OPTION-ACTIONS: close=${ACTION_LABEL}]`
/** Content and action offering the SAME label — the tiebreak case. */
const COLLIDING_LABEL = 'Close it'
const COLLIDING = `Pick one.\n\n[OPTIONS: ${COLLIDING_LABEL}]\n\n[OPTION-ACTIONS: close=${COLLIDING_LABEL}]`
/** Prose that TALKS ABOUT the markers without carrying one. Must offer no action. */
const PROSE_ABOUT_MARKERS = 'You can end a chat with an OPTION-ACTIONS close marker, or list choices with an OPTIONS marker.\n\n[OPTIONS: Got it | Tell me more]'

const APPENDED = { ok: true as const, appended: true, visibleDeferred: false, deliveryConditional: false, contextSkipped: true, pending: 0 }
const DEFERRED = { ok: true as const, appended: false, visibleDeferred: true, deliveryConditional: false, contextSkipped: true, pending: 0 }

const mock = (fn: unknown) => fn as ReturnType<typeof vi.fn>

const dashboardState = (slot: string) => ({
  status: null, connected: true,
  slots: [{ key: slot, messages: 1, running: false, mode: '', project: '/repo', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
  unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
} as unknown as RootState['dashboard'])

/** ChatPage reads its transcript from `chat.messages` + `chat.activeSlot`, so it needs
 *  the full preloaded chat shape (mirrors ChatPage.followUpToggle.test.tsx). */
function makePageStore(slot: string, content: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: dashboardState(slot),
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

/** ChatPane reads `slotMessages[slotKey]`, hydrated from `chatSlotDetail`, and touches
 *  slices a partial preload would omit (`pendingQuestions`, per-slot maps). So it takes
 *  the reducer's own defaults and preloads only `dashboard` — mirrors
 *  ChatPane.followUpOptions.test.tsx, whose harness this file deliberately matches. */
function makePaneStore(slot: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: { dashboard: dashboardState(slot) } as Partial<RootState>,
  })
}

/** The two hosts, rendered through one signature so every dispatch assertion runs
 *  against BOTH. ChatPage reads its slot from the store's `activeSlot`; ChatPane is
 *  bound to `slotKey` and hydrates its transcript from `chatSlotDetail`. */
const HOSTS = [
  {
    name: 'ChatPage',
    async render(slot: string, content: string) {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      mock(api.chatSlots).mockResolvedValue([{ key: slot, messages: 1, running: false, mode: '', project: '/repo' }])
      const store = makePageStore(slot, content)
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
      return store
    },
  },
  {
    name: 'ChatPane',
    async render(slot: string, content: string) {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      const messages = [{ role: 'assistant', content, ts: '2026-08-29T00:00:01Z' }]
      mock(api.chatSlotDetail).mockResolvedValue({ messages, running: false, has_more: false, total: 1 })
      const store = makePaneStore(slot)
      await act(async () => {
        render(
          <Provider store={store}>
            <QueryClientProvider client={qc}>
              <ThemeProvider>
                <MemoryRouter><ChatPane slotKey={slot} /></MemoryRouter>
              </ThemeProvider>
            </QueryClientProvider>
          </Provider>,
        )
      })
      return store
    },
  },
] as const

/** The shipped action chip, addressed by the attribute the component sets rather
 *  than by its label: the tiebreak fixture gives a content chip the SAME visible
 *  text, so an accessible-name lookup would be ambiguous there. */
const actionChip = () => {
  const el = document.querySelector('[data-option-action="close"]')
  if (!el) throw new Error('no action chip rendered')
  return el as HTMLButtonElement
}
const seam = (which: 'select' | 'send') => screen.getAllByTestId(`seam-${which}`)[0]
const errorRows = (store: ReturnType<typeof makePageStore>, slot: string) => {
  const s = store.getState()
  const rows = s.chat.activeSlot === slot && s.chat.messages.length ? s.chat.messages : (s.chat.slotMessages?.[slot] ?? [])
  return rows.filter(m => m.role === 'error')
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  seamLabel = ''
  vi.clearAllMocks()
  mock(api.chatSlotNote).mockResolvedValue(APPENDED)
  mock(api.deleteChatSlot).mockResolvedValue({ ok: true })
  mock(api.dashboardConfig).mockResolvedValue({ quick_send: false })
  // jsdom's confirm() is unimplemented and throws. The dispatch must go through
  // `useSessionActions().close`, which CALLS it whenever the preference is on, so
  // a stub is required rather than incidental.
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe.each(HOSTS)('$name — zero-turn option action dispatch', (host) => {
  it('clicking an action chip starts NO LLM turn', async () => {
    const slot = `${host.name}-noturn`
    await host.render(slot, ACTION_ONLY)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(actionChip()) })
    // The whole premise: no message goes to the agent, and no plan dispatch either.
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(api.planAction).not.toHaveBeenCalled()
  })

  it('writes the breadcrumb with visibleOnly and the picked label, BEFORE closing', async () => {
    const slot = `${host.name}-breadcrumb`
    await host.render(slot, ACTION_ONLY)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(actionChip()) })
    await waitFor(() => expect(api.chatSlotNote).toHaveBeenCalledTimes(1))
    expect(api.chatSlotNote).toHaveBeenCalledWith(
      slot,
      // A SENTENCE naming the outcome, WITH the label inside it. The row is
      // permanent and renders verbatim in the transcript, so the bare label read as
      // something the USER had said — it named neither actor nor outcome, which is
      // the opposite of the audit job the row exists to do. Provenance still lives
      // in `source`.
      expect.stringContaining(ACTION_LABEL),
      { source: 'option-action', visibleOnly: true },
    )
    const [, noteContent] = mock(api.chatSlotNote).mock.calls[0]
    // The row records the REQUEST, not a completed close, and that is forced by the
    // ordering rather than chosen for tone: the write must precede the staleness and
    // composer rechecks (they exist to catch state that moved DURING this POST), so a
    // recheck can abort the close when the row is already durable. A row reading
    // "Session closed" would then be a permanent false statement in the transcript.
    expect(noteContent).toMatch(/request/i)
    expect(noteContent).not.toMatch(/session closed/i)
    // Still no MACHINE-shaped prefix: a `[option-action:close] ` tag had zero
    // readers and `parseOptions` strips only `[OPTIONS:`/`[OPTION-ACTIONS:`, so the
    // singular lowercase form rendered raw in the bubble. `stringContaining` above
    // is exactly what let that ride along unnoticed, so these stay.
    expect(noteContent).not.toContain('option-action')
    expect(noteContent).not.toMatch(/^\[/)
    // `visibleOnly` is not cosmetic: `_pending_context` is never serialized and the
    // close pops the slot, so a context half would die with the frame.
    const [, , opts] = mock(api.chatSlotNote).mock.calls[0]
    expect(opts.visibleOnly).toBe(true)
    // Ordering, from the mock invocation record rather than from the source: the
    // close must not be able to race the write it is gated on.
    await waitFor(() => expect(api.deleteChatSlot).toHaveBeenCalledTimes(1))
    expect(mock(api.chatSlotNote).mock.invocationCallOrder[0])
      .toBeLessThan(mock(api.deleteChatSlot).mock.invocationCallOrder[0])
  })

  it('closes exactly once when the breadcrumb landed', async () => {
    const slot = `${host.name}-close-once`
    await host.render(slot, ACTION_ONLY)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(actionChip()) })
    await waitFor(() => expect(api.deleteChatSlot).toHaveBeenCalledTimes(1))
    expect(api.deleteChatSlot).toHaveBeenCalledWith(slot)
    expect(api.chatSlotNote).toHaveBeenCalledTimes(1)
  })

  it('refuses to close on a DEFERRED note, and says why', async () => {
    // THE measured case. A deferred note is held in memory only and is destroyed
    // by the close, which still answers 200 — so a dispatch that trusted the 200
    // would lose the record with no error anywhere.
    const slot = `${host.name}-deferred`
    mock(api.chatSlotNote).mockResolvedValue(DEFERRED)
    const store = await host.render(slot, ACTION_ONLY)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(actionChip()) })
    await waitFor(() => expect(api.chatSlotNote).toHaveBeenCalledTimes(1))
    expect(api.deleteChatSlot).not.toHaveBeenCalled()
    // The tab stays, so the reason has to reach the transcript.
    await waitFor(() => expect(errorRows(store, slot).length).toBe(1))
    expect(errorRows(store, slot)[0].content).toMatch(/turn is running/i)
    // And still no turn.
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('refuses to close when the breadcrumb POST fails outright', async () => {
    const slot = `${host.name}-note-reject`
    mock(api.chatSlotNote).mockRejectedValue(new Error('network'))
    const store = await host.render(slot, ACTION_ONLY)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(actionChip()) })
    await waitFor(() => expect(api.chatSlotNote).toHaveBeenCalledTimes(1))
    expect(api.deleteChatSlot).not.toHaveBeenCalled()
    await waitFor(() => expect(errorRows(store, slot).length).toBe(1))
  })

  it('a REFUSED close writes no second breadcrumb and claims nothing', async () => {
    // The close path answers 500 on a teardown-hook failure and deliberately
    // refuses rather than half-closing. The breadcrumb is already durable, so the
    // only requirements are: do not write again, and do not report success.
    const slot = `${host.name}-close-refused`
    mock(api.deleteChatSlot).mockRejectedValue(new Error('teardown hook failed'))
    const store = await host.render(slot, ACTION_ONLY)
    const slotsReadsBefore = mock(api.chatSlots).mock.calls.length
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(actionChip()) })
    await waitFor(() => expect(api.deleteChatSlot).toHaveBeenCalledTimes(1))
    // No retry write, ever — the record is already durable.
    expect(api.chatSlotNote).toHaveBeenCalledTimes(1)
    // Nothing claims the tab closed: the dispatch appends no row of its own on
    // this path, so the only rows are whatever the close's own recovery produced.
    expect(errorRows(store, slot).some(m => /clos/i.test(m.content ?? ''))).toBe(false)
    // The refusal self-heals through the close's own re-fetch rather than being
    // papered over — proof the rejection was absorbed, not swallowed into a lie.
    await waitFor(() => expect(mock(api.chatSlots).mock.calls.length).toBeGreaterThan(slotsReadsBefore))
  })

  it('honours the confirm-close preference instead of re-implementing it', async () => {
    // Proof the dispatch goes through `useSessionActions().close` rather than
    // dispatching `deleteSlot` directly: only the hook consults this preference.
    const slot = `${host.name}-confirm`
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: true }))
    mock(window.confirm).mockReturnValue(false)
    await host.render(slot, ACTION_ONLY)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(actionChip()) })
    await waitFor(() => expect(window.confirm).toHaveBeenCalled())
    // Declined — so no close.
    expect(api.deleteChatSlot).not.toHaveBeenCalled()
  })

  it('writes NO breadcrumb when the confirm is declined', async () => {
    // The breadcrumb is a PERMANENT transcript row, so writing it before the user
    // can cancel leaves the transcript asserting "Nothing else, close this tab"
    // for a close that never happened — a durable record contradicting what
    // occurred, on the one row this feature exists to make readable.
    //
    // So the confirm has to come FIRST. It cannot simply move after the write,
    // because the `appended === true` refusal gate must still hold: the close is
    // refused unless the breadcrumb landed. Order is therefore
    // confirm -> note -> close, which keeps both properties.
    const slot = `${host.name}-confirm-no-note`
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: true }))
    mock(window.confirm).mockReturnValue(false)
    const store = await host.render(slot, ACTION_ONLY)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(actionChip()) })
    await waitFor(() => expect(window.confirm).toHaveBeenCalled())
    expect(api.chatSlotNote).not.toHaveBeenCalled()
    expect(api.deleteChatSlot).not.toHaveBeenCalled()
    // And the slot survives: cancelling means nothing happened at all.
    expect(store.getState().dashboard.slots.some((s: { key: string }) => s.key === slot)).toBe(true)
  })

  it('still writes the breadcrumb when the confirm is ACCEPTED', async () => {
    // Negative control for the test above: if the fix simply stopped writing the
    // note, this would fail. The note must still precede the close.
    const slot = `${host.name}-confirm-accepted`
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: true }))
    mock(window.confirm).mockReturnValue(true)
    await host.render(slot, ACTION_ONLY)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(actionChip()) })
    await waitFor(() => expect(api.deleteChatSlot).toHaveBeenCalledTimes(1))
    expect(api.chatSlotNote).toHaveBeenCalledTimes(1)
    expect(mock(api.chatSlotNote).mock.invocationCallOrder[0])
      .toBeLessThan(mock(api.deleteChatSlot).mock.invocationCallOrder[0])
  })

  it('leaves ordinary [OPTIONS:] behaviour alone on a row that also offers an action', async () => {
    const slot = `${host.name}-content-untouched`
    seamLabel = 'Keep going'
    await host.render(slot, OPTIONS_AND_ACTION)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(seam('select')) })
    // The content seam composes text; it does not write or close. Nothing
    // intercepts here at all — the action path is reachable only through the
    // action chip, which closes over neither of these callbacks.
    expect(api.chatSlotNote).not.toHaveBeenCalled()
    expect(api.deleteChatSlot).not.toHaveBeenCalled()
  })

  it('lets a CONTENT option win at the content seam when it shares a label with an action', async () => {
    // The content seams belong to content options, so a same-named content option
    // keeps its exact behaviour — that is what makes "ordinary [OPTIONS:] is
    // completely unchanged" true even for a label that reads like an action.
    const slot = `${host.name}-collision-content`
    seamLabel = COLLIDING_LABEL
    await host.render(slot, COLLIDING)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(seam('send')) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(api.chatSlotNote).not.toHaveBeenCalled()
    expect(api.deleteChatSlot).not.toHaveBeenCalled()
  })

  it('still dispatches the shared-label ACTION through its own chip', async () => {
    // Separate render on purpose: the send above appends a `user` row, which ends
    // the turn and clears every chip — so a second assertion in that same render
    // would be measuring the cleared bar, not the tiebreak.
    const slot = `${host.name}-collision-action`
    await host.render(slot, COLLIDING)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(actionChip()) })
    await waitFor(() => expect(api.chatSlotNote).toHaveBeenCalledTimes(1))
    expect(api.chatSlotNote).toHaveBeenCalledWith(
      slot,
      expect.stringContaining(COLLIDING_LABEL),
      { source: 'option-action', visibleOnly: true },
    )
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('confirms even when the close-confirm preference is OFF', async () => {
    // The affordance is MODEL-authored: the label is arbitrary prose and the chip is
    // one click, while `confirmCloseSession` defaults to false — so without a forced
    // confirm the session went away with neither a dialog nor a stated consequence.
    // A caller that put the affordance there itself (the session menu, a keyboard
    // shortcut) does not force it.
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: false }))
    const confirmSpy = mock(window.confirm).mockReturnValue(true)
    confirmSpy.mockClear()
    const slot = `${host.name}-forced-confirm`
    await host.render(slot, ACTION_ONLY)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(actionChip()) })
    await waitFor(() => expect(api.deleteChatSlot).toHaveBeenCalledTimes(1))
    expect(confirmSpy).toHaveBeenCalled()
  })

  it('a DECLINED forced confirm writes nothing and keeps the session', async () => {
    // The negative control for the test above: it must be able to fail because the
    // confirm ran and was answered, not because a dialog appeared and was ignored.
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: false }))
    mock(window.confirm).mockReturnValue(false)
    const slot = `${host.name}-forced-confirm-declined`
    await host.render(slot, ACTION_ONLY)
    await waitFor(() => expect(actionChip()).toBeTruthy())
    await act(async () => { fireEvent.click(actionChip()) })
    expect(api.chatSlotNote).not.toHaveBeenCalled()
    expect(api.deleteChatSlot).not.toHaveBeenCalled()
  })

  it('offers no action for prose that merely discusses the marker syntax', async () => {
    const slot = `${host.name}-prose`
    seamLabel = 'Got it'
    await host.render(slot, PROSE_ABOUT_MARKERS)
    // Content chips are there; no action chip is, so no dispatch surface exists.
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Got it' }).length).toBeGreaterThan(0))
    expect(document.querySelector('[data-option-action]')).toBeNull()
    await act(async () => { fireEvent.click(seam('select')) })
    expect(api.chatSlotNote).not.toHaveBeenCalled()
    expect(api.deleteChatSlot).not.toHaveBeenCalled()
  })
})
