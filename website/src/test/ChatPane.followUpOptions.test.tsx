import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { StrictMode } from 'react'
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
import { composerRoot, composerValue, setComposerValue, awaitComposer } from './helpers'

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
  await awaitComposer()
  return store
}

const composer = () => composerRoot()
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
    expect(composerValue(composer())).toBe('Alpha')
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
    await setComposerValue('my unsent draft', composer())
    fireEvent.doubleClick(chip('Alpha'))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('Alpha')
    expect(composerValue(composer())).toBe('my unsent draft')
  })

  it('unselecting an option splices its own appended text, never a matching substring of the draft', async () => {
    // Regression: `indexOf(', ' + option)` can match INSIDE the draft — draft
    // "Please, Alphabet" + option "Alpha" would splice mid-word on unselect.
    // The handler appends at the END, so it must remove the LAST occurrence.
    await renderPane('pane-7')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    await setComposerValue('Please, Alphabet', composer())
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composerValue(composer())).toBe('Please, Alphabet, Alpha')
    await act(async () => { clickOption('Alpha') })
    expect(composerValue(composer())).toBe('Please, Alphabet')
  })

  it('leaves earlier draft text alone when the user already deleted the appended option', async () => {
    // ChatPage removes only the COMPLETE generated suffix (#6092 review). The
    // pane must hold the same invariant: once the user has deleted the
    // appended ", Alpha" by hand, unselecting the still-lit chip has nothing of
    // its own left to remove, and a last-occurrence search would fall back onto
    // the ", Alpha" the user typed — silently editing their draft.
    await renderPane('pane-8')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    await setComposerValue('Discuss, Alpha home', composer())
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composerValue(composer())).toBe('Discuss, Alpha home, Alpha')

    // The chip stays lit; the user removes its generated tail themselves.
    await setComposerValue('Discuss, Alpha home', composer())
    await act(async () => { clickOption('Alpha') })
    expect(composerValue(composer())).toBe('Discuss, Alpha home')
  })

  it('un-toggle removes only the chip-owned suffix, not user text inserted mid-draft (#7616)', async () => {
    // The fenced GPT bug generalized: the removal must key on WHAT THE CHIP
    // APPENDED, not on the picked-set's content. Here the user edits the draft
    // between two appends, so the owned suffix ("Beta") no longer equals the
    // picked-set string ("Alpha, Beta"). Un-toggling Beta must strip only the
    // owned ", Beta" and keep the user's "Alpha and more" — the pre-fix
    // content search removed the wrong span.
    await renderPane('pane-7616-owned')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composerValue(composer())).toBe('Alpha')
    // The user edits the draft after the first append — this re-baselines
    // ownership so the chip no longer owns the whole "Alpha, Beta" content.
    await setComposerValue('Alpha and more', composer())
    await act(async () => { clickOption('Beta') })
    expect(composerValue(composer())).toBe('Alpha and more, Beta')
    await act(async () => { clickOption('Beta') })
    expect(composerValue(composer())).toBe('Alpha and more')
  })

  it('un-toggle leaves the draft untouched once the user edited the chip-owned tail (#7616)', async () => {
    // Even when the picked-set string still equals a suffix of the draft, an
    // edit to the owned span means the chip no longer owns it: the pre-fix
    // endsWith()/=== checks would still splice, eating the user's own words.
    // Ownership makes the un-toggle a no-op on the text, only un-highlighting.
    await renderPane('pane-7616-edited')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    await setComposerValue('note', composer())
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composerValue(composer())).toBe('note, Alpha')
    // The user rewrites the whole draft to different text that still ENDS with
    // ", Alpha" — a content endsWith() match, but NOT the chip's own append.
    await setComposerValue('other, Alpha', composer())
    await act(async () => { clickOption('Alpha') })
    // Ownership no longer matches the tail → the user's text is preserved.
    // The pre-fix endsWith(', Alpha') would have sliced it to 'other'.
    expect(composerValue(composer())).toBe('other, Alpha')
  })

  it('un-toggle removes a comma-bearing label as one unit, never mis-split (#7616 F1)', async () => {
    // The option list is "|"-separated, so a label may legally contain ", "
    // ([OPTIONS: bar | foo, bar]). Selecting both then un-toggling "bar" must
    // remove only that label; a split-on-", " removal would corrupt "foo, bar".
    const msgs = [
      { role: 'user', content: 'hi', ts: '2026-08-25T00:00:00Z' },
      { role: 'assistant', content: 'Ready.\n\n[OPTIONS: bar | foo, bar]', ts: '2026-08-25T00:00:01Z' },
    ]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: msgs, running: false, has_more: false, total: msgs.length })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-7616-comma')
    await act(async () => {
      render(
        <Provider store={store}>
          <QueryClientProvider client={qc}>
            <ThemeProvider><MemoryRouter><ChatPane slotKey="pane-7616-comma" /></MemoryRouter></ThemeProvider>
          </QueryClientProvider>
        </Provider>,
      )
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'bar', exact: true })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('bar') })
    await act(async () => { clickOption('foo, bar') })
    expect(composerValue(composer())).toBe('bar, foo, bar')
    await act(async () => { clickOption('bar') })
    // Only the "bar" label is removed; the comma-bearing "foo, bar" survives whole.
    expect(composerValue(composer())).toBe('foo, bar')
  })

  it('un-toggle survives StrictMode double-invocation without reclassifying text (#7616 F2)', async () => {
    // The app mounts under <StrictMode> (main.tsx). A toggle transform that
    // wrote ownership as a side effect inside a functional state updater would
    // be double-invoked and rebase on stale state, leaving the un-picked option
    // in the composer. select Alpha, select Beta, un-select Alpha → "Beta".
    const store = makeStore('pane-7616-strict')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: PANE_MESSAGES, running: false, has_more: false, total: PANE_MESSAGES.length })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await act(async () => {
      render(
        <StrictMode>
          <Provider store={store}>
            <QueryClientProvider client={qc}>
              <ThemeProvider><MemoryRouter><ChatPane slotKey="pane-7616-strict" /></MemoryRouter></ThemeProvider>
            </QueryClientProvider>
          </Provider>
        </StrictMode>,
      )
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    await act(async () => { clickOption('Beta') })
    expect(composerValue(composer())).toBe('Alpha, Beta')
    await act(async () => { clickOption('Alpha') })
    // Ownership was not rebased by the double-invoked handler: Alpha is removed.
    expect(composerValue(composer())).toBe('Beta')
  })

  it('un-toggle is a no-op after the user edited and restored the draft byte-for-byte (#7616 F3)', async () => {
    // Ownership must not survive a user edit: select Alpha (composer "Alpha"),
    // the user edits the draft and then restores it to exactly "Alpha" by hand,
    // then un-toggles. A span that revalidated on the restored text would delete
    // the user's restored word; clearing ownership on any edit makes un-toggle
    // leave the text and only un-highlight.
    await renderPane('pane-7616-restore')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composerValue(composer())).toBe('Alpha')
    // User edits away and then restores the identical text.
    await setComposerValue('Alpha draft', composer())
    await setComposerValue('Alpha', composer())
    await act(async () => { clickOption('Alpha') })
    // The edit invalidated ownership, so the user's restored "Alpha" survives.
    expect(composerValue(composer())).toBe('Alpha')
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
    expect(composerValue(composer())).toBe('')
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a NON-plan chip in an orchestrator pane still appends to the composer (plain follow-ups unaffected)', async () => {
    await renderPane('pane-plan-2', { mode: 'orchestrator' }, PANE_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composerValue(composer())).toBe('Alpha')
    expect(api.planAction).not.toHaveBeenCalled()
  })

  it('a plan-shaped chip outside orchestrator mode falls through to the composer (same mode gate as ChatPage)', async () => {
    await renderPane('pane-plan-3', { mode: '' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(composerValue(composer())).toBe('Go')
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
    expect(composerValue(composer())).toBe('Approve it')
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
    expect(composerValue(composer())).toBe('')
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

  /* #6056: the dispatch has to be visible. Until this landed a click on a plan
   * chip produced no pending state and no message on failure — `onError` only
   * reached the console — so a slow dispatch and a dead button looked the same,
   * which is what drove users to remount the pane. */
  it('a failed dispatch shows the error row under the chips, and the next click clears it', async () => {
    ;(api.planAction as ReturnType<typeof vi.fn>)
      // A DEFINITIVE 4xx, so the latch releases and the retry below really runs.
      .mockImplementationOnce(() => Promise.reject(new ApiError(400, 'unknown action')))
      // The retry never settles, which is what makes the cleared row observable:
      // a second rejection would repaint it before the assertion could run.
      .mockImplementation(() => new Promise(() => {}))
    await renderPane('pane-plan-6056a', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    expect(screen.queryByRole('alert')).toBeNull()

    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { await Promise.resolve() })
    vi.useRealTimers()
    await waitFor(() => {
      const row = screen.getByRole('alert')
      expect(row).toHaveTextContent('Could not send that plan action.')
      expect(row).toHaveTextContent('unknown action')
    })
    // The chip is idle again, so the retry the released latch permits is reachable.
    expect(screen.getByRole('button', { name: 'Go' })).not.toHaveAttribute('aria-disabled')

    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    vi.useRealTimers()
    expect(api.planAction).toHaveBeenCalledTimes(2)
    // No timer and no dismiss button retired it — the next click did.
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('Go and Cancel outstanding together spin BOTH chips, because the classes latch independently', async () => {
    // Cancel is deliberately never blocked by a pending Go, so a slot can hold
    // two latches at once. The chips have to say so: a single busy label would
    // let the second dispatch un-spin the first chip, and an idle-looking chip
    // over a held latch is the dead button the affordance exists to remove.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => new Promise(() => {}))
    await renderPane('pane-plan-6056d', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())

    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { clickOption('Cancel') })
    vi.useRealTimers()
    expect(api.planAction).toHaveBeenCalledTimes(2)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toHaveAttribute('aria-disabled', 'true'))
    expect(screen.getByRole('button', { name: 'Go' }).querySelector('.animate-spin')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByRole('button', { name: 'Cancel' }).querySelector('.animate-spin')).toBeTruthy()
    // Go All shares Go's latch but was never dispatched, so it only dims. The
    // pane's chips carry a Send-now segment, so the dim lands on the split
    // wrapper (the flex item) rather than on the label button.
    // Refused by Go's shared latch: dimmed and announced unavailable, never disabled.
    expect(screen.getByRole('button', { name: 'Go All' })).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByRole('button', { name: 'Go All' })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: 'Go All' }).parentElement?.className).toContain('opacity-70')
  })

  it('a REMOUNTED pane on a latched slot still spins the chip, because the latch is shared not copied', async () => {
    // The grid unmounts and remounts a pane on split/close, and the unified view
    // holds its own instance of the same hook on the same slot. The latch lives
    // in a module map they all share, so a fresh instance that started from its
    // own empty copy would draw an idle chip while the map still refused the
    // click — the exact dead button, reintroduced in a narrower window.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve({ ok: true }))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: PLAN_MESSAGES, running: false, has_more: false, total: PLAN_MESSAGES.length })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-plan-6056e', { mode: 'orchestrator' })
    const ui = (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey="pane-plan-6056e" />
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
    vi.useRealTimers()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toHaveAttribute('aria-disabled', 'true'))
    first.unmount()

    // Same query client and store: warm cache, the same stale row, so the latch
    // is still held and the remounted pane must still show it held.
    render(ui)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toHaveAttribute('aria-disabled', 'true'))
    expect(screen.getByRole('button', { name: 'Go' }).querySelector('.animate-spin')).toBeTruthy()
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    vi.useRealTimers()
    expect(api.planAction).toHaveBeenCalledTimes(1)  // still latched — dropped
  })

  it('a stalled dispatch rejecting late never overwrites a NEWER row\'s failure', async () => {
    // `onError` is options-level and fires for a superseded mutation. Gating only
    // the DISPLAY is not enough: an unconditional write lets the stale rejection
    // clobber the current row's record, and the read gate then hides the
    // replacement too, so a real error the user needed silently disappears.
    let stall: ((e: unknown) => void) | null = null
    ;(api.planAction as ReturnType<typeof vi.fn>)
      .mockImplementationOnce(() => new Promise((_r, rej) => { stall = rej }))
      .mockImplementation(() => Promise.reject(new ApiError(400, 'second row failed')))
    const store = await renderPane('pane-plan-6056h', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())

    // R1: dispatch and stall.
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    vi.useRealTimers()

    // R2 arrives, freeing the latch, and its own dispatch fails for real.
    await act(async () => {
      store.dispatch(appendSlotMessage({
        slot: 'pane-plan-6056h',
        message: { role: 'assistant', content: ASSISTANT_WITH_PLAN, ts: '2026-08-25T00:00:15Z' },
      }))
    })
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { await Promise.resolve() })
    vi.useRealTimers()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('second row failed'))

    // Now R1 finally rejects. R2's message must survive it.
    await act(async () => { stall?.(new ApiError(400, 'first row failed')); await Promise.resolve() })
    expect(screen.getByRole('alert')).toHaveTextContent('second row failed')
  })

  it('a LATE rejection landing after the row advanced never shows, even though its slot matches', async () => {
    // The reversed ordering of the superseded-row case, and the one scoping the
    // slot alone does not cover: the acknowledgement clear has already run, so a
    // rejection that resolves AFTER it writes its record unopposed. Only gating
    // the exposed value on the ROW as well as the slot keeps it off screen.
    let reject: ((e: unknown) => void) | null = null
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise((_res, rej) => { reject = rej }),
    )
    const store = await renderPane('pane-plan-6056g', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())

    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    vi.useRealTimers()
    expect(api.planAction).toHaveBeenCalledTimes(1)

    // The next plan row arrives FIRST, so the acknowledgement clear runs now.
    await act(async () => {
      store.dispatch(appendSlotMessage({
        slot: 'pane-plan-6056g',
        message: { role: 'assistant', content: ASSISTANT_WITH_PLAN, ts: '2026-08-25T00:00:13Z' },
      }))
    })
    // ...and only THEN does the old dispatch reject. Same slot, superseded row.
    await act(async () => { reject?.(new ApiError(400, 'unknown action')); await Promise.resolve() })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('a failure is retired when the row it belongs to is superseded, not left under the next stage', async () => {
    // An ambiguous 5xx keeps the latch, so the plan can still advance server-side.
    // When the acknowledgement row arrives the latch frees and the chips un-spin —
    // and the error row has to go with them, or it sits under the NEXT stage's
    // chips claiming that stage failed.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.reject(new ApiError(500, 'internal error')))
    const store = await renderPane('pane-plan-6056f', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())

    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { await Promise.resolve() })
    vi.useRealTimers()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('internal error'))

    // The plan advances: a different options-bearing row arrives.
    await act(async () => {
      store.dispatch(appendSlotMessage({
        slot: 'pane-plan-6056f',
        message: { role: 'assistant', content: ASSISTANT_WITH_PLAN, ts: '2026-08-25T00:00:11Z' },
      }))
    })
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })

  it('a REFUSED click keeps the error row, because nothing replaced the explanation', async () => {
    // The 5xx keeps the latch, so the next click on the same class is dropped:
    // no request goes out and the failure is still the only account of why the
    // chip is doing nothing. Retiring the row on the way IN left a wedged latch
    // with no message at all.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.reject(new ApiError(500, 'internal error')))
    await renderPane('pane-plan-6056c', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())

    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { await Promise.resolve() })
    vi.useRealTimers()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('internal error'))

    // Go All shares the Go latch, so this click is refused outright.
    vi.useFakeTimers()
    await act(async () => { clickOption('Go All') })
    vi.useRealTimers()
    expect(api.planAction).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('alert')).toHaveTextContent('internal error')
  })

  it('a chip stays spinning through the silent window after a SUCCESSFUL dispatch, until the transcript acknowledges', async () => {
    // The state the request alone cannot express: the response has landed and
    // the latch still holds, so a re-click is being refused. `isPending` would
    // have gone false here and left the chip looking idle while it was not.
    ;(api.planAction as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve({ ok: true }))
    const store = await renderPane('pane-plan-6056b', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())

    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    await act(async () => { await Promise.resolve() })
    vi.useRealTimers()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toHaveAttribute('aria-disabled', 'true'))
    expect(screen.getByRole('button', { name: 'Go' }).querySelector('.animate-spin')).toBeTruthy()
    // A sibling is dimmed but still live, so Cancel is reachable all through it.
    expect(screen.getByRole('button', { name: 'Cancel' })).not.toHaveAttribute('aria-disabled')

    // The acknowledgement: a different options-bearing row arrives.
    await act(async () => {
      store.dispatch(appendSlotMessage({
        slot: 'pane-plan-6056b',
        message: { role: 'assistant', content: ASSISTANT_WITH_PLAN, ts: '2026-08-25T00:00:09Z' },
      }))
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).not.toHaveAttribute('aria-disabled'))
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
    expect(composerValue(composer())).toBe('')
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
    expect(composerValue(composer())).toBe('')
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
    expect(composerValue(composer())).toBe('')
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
    expect(composerValue(composer())).toBe('')
    // And the refusal did not wedge the new row: a fresh click on it goes.
    await act(async () => { clickOption('Go') })
    expect(api.planAction).toHaveBeenCalledTimes(1)
  })
})
