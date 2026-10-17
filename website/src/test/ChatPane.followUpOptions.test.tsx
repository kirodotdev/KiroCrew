import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { StrictMode } from 'react'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setQuestionCard, clearQuestionCard } from '../store/chatSlice'
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
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: {} }),
    agentResolvedModel: vi.fn().mockResolvedValue({ pinned: false }),
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
import { api } from '../api/client'

/** The marker has to close its own line for OPTION_MARKER_RE to match. */
const ASSISTANT_WITH_OPTIONS = 'Ready to proceed.\n\n[OPTIONS: Alpha | Beta]'

/** Plan-shaped text (header, stage line, Go/Go All/Cancel footer): an ordinary options message. */
const ASSISTANT_WITH_PLAN = '📋 Plan for: ship it\n\nStage 1: build the thing\n\n[OPTION: Go | Go All | Cancel]'

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

  it('leaves earlier draft text alone when the user already deleted the appended option', async () => {
    // ChatPage removes only the COMPLETE generated suffix (#6092 review). The
    // pane must hold the same invariant: once the user has deleted the
    // appended ", Alpha" by hand, unselecting the still-lit chip has nothing of
    // its own left to remove, and a last-occurrence search would fall back onto
    // the ", Alpha" the user typed — silently editing their draft.
    await renderPane('pane-8')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    fireEvent.change(composer(), { target: { value: 'Discuss, Alpha home' } })
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composer().value).toBe('Discuss, Alpha home, Alpha')

    // The chip stays lit; the user removes its generated tail themselves.
    fireEvent.change(composer(), { target: { value: 'Discuss, Alpha home' } })
    await act(async () => { clickOption('Alpha') })
    expect(composer().value).toBe('Discuss, Alpha home')
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
    expect(composer().value).toBe('Alpha')
    // The user edits the draft after the first append — this re-baselines
    // ownership so the chip no longer owns the whole "Alpha, Beta" content.
    fireEvent.change(composer(), { target: { value: 'Alpha and more' } })
    await act(async () => { clickOption('Beta') })
    expect(composer().value).toBe('Alpha and more, Beta')
    await act(async () => { clickOption('Beta') })
    expect(composer().value).toBe('Alpha and more')
  })

  it('un-toggle leaves the draft untouched once the user edited the chip-owned tail (#7616)', async () => {
    // Even when the picked-set string still equals a suffix of the draft, an
    // edit to the owned span means the chip no longer owns it: the pre-fix
    // endsWith()/=== checks would still splice, eating the user's own words.
    // Ownership makes the un-toggle a no-op on the text, only un-highlighting.
    await renderPane('pane-7616-edited')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    fireEvent.change(composer(), { target: { value: 'note' } })
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composer().value).toBe('note, Alpha')
    // The user rewrites the whole draft to different text that still ENDS with
    // ", Alpha" — a content endsWith() match, but NOT the chip's own append.
    fireEvent.change(composer(), { target: { value: 'other, Alpha' } })
    await act(async () => { clickOption('Alpha') })
    // Ownership no longer matches the tail → the user's text is preserved.
    // The pre-fix endsWith(', Alpha') would have sliced it to 'other'.
    expect(composer().value).toBe('other, Alpha')
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
    expect(composer().value).toBe('bar, foo, bar')
    await act(async () => { clickOption('bar') })
    // Only the "bar" label is removed; the comma-bearing "foo, bar" survives whole.
    expect(composer().value).toBe('foo, bar')
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
    expect(composer().value).toBe('Alpha, Beta')
    await act(async () => { clickOption('Alpha') })
    // Ownership was not rebased by the double-invoked handler: Alpha is removed.
    expect(composer().value).toBe('Beta')
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
    expect(composer().value).toBe('Alpha')
    // User edits away and then restores the identical text.
    fireEvent.change(composer(), { target: { value: 'Alpha draft' } })
    fireEvent.change(composer(), { target: { value: 'Alpha' } })
    await act(async () => { clickOption('Alpha') })
    // The edit invalidated ownership, so the user's restored "Alpha" survives.
    expect(composer().value).toBe('Alpha')
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

describe('ChatPane plan-shaped follow-ups', () => {
  // A Go / Go All / Cancel footer is an ordinary options row: the chip edits the
  // composer or sends its text through the pane's send path, whatever the
  // slot's (legacy) mode says.
  it('a single click appends the chip text to the composer, even in a legacy orchestrator-mode pane', async () => {
    await renderPane('pane-plan-1', { mode: 'orchestrator' }, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(composer().value).toBe('Go')
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a single-select [OPTION:] pick replaces the previous pick instead of joining it', async () => {
    await renderPane('pane-plan-single', {}, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Go') })
    expect(composer().value).toBe('Go')
    await act(async () => { clickOption('Cancel') })
    expect(composer().value).toBe('Cancel')
  })

  it('a multi-select [OPTIONS:] pick still joins the previous pick (control)', async () => {
    await renderPane('pane-multi-control')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    await act(async () => { clickOption('Beta') })
    expect(composer().value).toBe('Alpha, Beta')
  })

  it('double-click sends the chip text through the pane\'s send path', async () => {
    await renderPane('pane-plan-dbl', {}, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Go' })).toBeTruthy())
    fireEvent.doubleClick(chip('Go'))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, slot] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('Go')
    expect(slot).toBe('pane-plan-dbl')
  })

  it('Send now sends the chip text through the pane\'s send path', async () => {
    await renderPane('pane-plan-sendnow', {}, PLAN_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Send now: Cancel' }))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, slot] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('Cancel')
    expect(slot).toBe('pane-plan-sendnow')
  })

  it('Send now on a plain chip sends the label through the pane\'s send path', async () => {
    await renderPane('pane-sendnow-plain', {}, PANE_MESSAGES)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Send now: Alpha' }))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, slot] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('Alpha')
    expect(slot).toBe('pane-sendnow-plain')
  })
})
