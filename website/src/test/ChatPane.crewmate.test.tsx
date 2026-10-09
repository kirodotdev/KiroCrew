import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, waitFor, fireEvent } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setSlotStatusDetail, sseChatMessage, sseToolActivity, sseToolResult, syncSlotRunningFromServer } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* A crewmate's chat (ChatPane with the `crewmate` prop, as the Members page
 * mounts it): the empty state a machinery-only history filters down to must say
 * who has not spoken and hand the reader a LIVE way to its Work log; the
 * composer must address the crewmate by name. Rendering-level pins for the two
 * UX Watch items on #12923 — the pure filter and run rules are pinned in
 * components/chat/crewmateBubbles.test.ts. */

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
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Kiro Crew', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const SLOT = 'member-radar'
const radar = { name: 'Radar' }

/** A patroller's history: wakes, a shell call and a say-nothing reply — no speech. */
const MACHINERY = [
  { role: 'nudge', content: '[auto-nudge cycle 1]\nPatrol.', cls: '', ts: '2026-09-22T06:00:00Z' },
  { role: 'tool', content: '🔧 gh issue list --label needs-triage', cls: '', ts: '2026-09-22T06:00:05Z' },
  { role: 'assistant', content: '\u200b', cls: '', ts: '2026-09-22T06:00:09Z' },
]

function makeStore(running = false) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: MACHINERY.length, running, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(opts: { onOpenCrewWorkLog?: () => void; crewmate?: boolean; running?: boolean; identity?: { name: string; mate?: boolean } } = { crewmate: true }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={makeStore(opts.running)}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} crewmate={opts.crewmate === false ? undefined : (opts.identity ?? radar)} onOpenCrewWorkLog={opts.onOpenCrewWorkLog} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
    messages: MACHINERY, running: false, has_more: false, total: MACHINERY.length,
  })
})

describe("a crewmate's chat", () => {
  it('draws none of the machinery and says who has not spoken, not "Session ready"', async () => {
    const view = renderPane()
    const hint = await view.findByTestId('crewmate-quiet-hint')
    expect(hint).toHaveTextContent("Radar hasn't said anything to you yet.")
    expect(view.queryByText(/Session ready/)).toBeNull()
    expect(view.queryByText(/gh issue list/)).toBeNull()
    expect(view.queryByText(/auto-nudge/)).toBeNull()
  })

  // The live status line reads the slot's live status record (`slotStatusDetail`)
  // and tool log — the DM header pill's own seam — so these tests seed the
  // store the way the WebSocket layer does rather than the transcript.
  const liveStore = (status: Record<string, unknown> | null, toolLog: Record<string, unknown>[] = []) => {
    const store = makeStore(true)
    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false }))
    if (status) store.dispatch(setSlotStatusDetail({ slot: SLOT, ts: Date.now(), ...status } as Parameters<typeof setSlotStatusDetail>[0]))
    for (const e of toolLog) {
      store.dispatch(sseToolActivity({ slot: SLOT, tool: String(e.tool), kind: 'other', purpose: '', input_preview: '', tool_call_id: String(e.tool_call_id), tool_name: e.tool_name as string | undefined, mcp_server: e.mcp_server as string | undefined }))
      if (e.output !== undefined) store.dispatch(sseToolResult({ slot: SLOT, output: String(e.output), tool_call_id: String(e.tool_call_id) }))
    }
    return store
  }
  const renderLive = (store: ReturnType<typeof makeStore>) => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={SLOT} crewmate={radar} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
  }

  it("while it works, the current step is the status line above the indicator; the patrol wake does not draw", async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: MACHINERY, running: true, has_more: false, total: MACHINERY.length,
    })
    const view = renderLive(liveStore({ kind: 'tool', purpose: 'Checking the triage queue', toolName: 'gh issue list', toolCallId: 'tc-1' }))
    await view.findByText('Checking the triage queue')
    const line = view.getByTestId('crewmate-live-activity')
    expect(line.dataset.activity).toBe('tool')
    expect(view.getAllByTestId('crewmate-live-activity')).toHaveLength(1)
    expect(view.queryByText(/auto-nudge/)).toBeNull()
    expect(view.queryByText(/gh issue list/)).toBeNull()
    expect(view.queryByTestId('crewmate-quiet-hint')).toBeNull()
  })

  it('once the call has returned (its output is in the tool log) the line reads the plain "Thinking…"', async () => {
    const view = renderLive(liveStore(
      { kind: 'tool', purpose: 'Checking the triage queue', toolName: 'gh issue list', toolCallId: 'tc-1' },
      [{ tool: 'gh issue list', tool_call_id: 'tc-1', output: 'no issues' }],
    ))
    const line = await view.findByTestId('crewmate-live-activity')
    await view.findByText('Thinking…', { selector: '[data-testid="crewmate-live-activity"] span' })
    expect(line.dataset.activity).toBe('thinking')
    expect(view.queryByText('Checking the triage queue')).toBeNull()
  })

  it('a nothing_to_do call in flight never names itself: the line reads "Thinking…", same height', async () => {
    const view = renderLive(liveStore(
      { kind: 'tool', purpose: 'Nothing to report', toolName: '@kirocrew-core/nothing_to_do', toolCallId: 'tc-q' },
      [{ tool: '@kirocrew-core/nothing_to_do', tool_call_id: 'tc-q', tool_name: 'nothing_to_do', mcp_server: 'kirocrew-core' }],
    ))
    const line = await view.findByTestId('crewmate-live-activity')
    expect(line.dataset.activity).toBe('thinking')
    expect(line).toHaveTextContent('Thinking…')
    expect(view.queryByText(/Nothing to report/)).toBeNull()
    expect(view.queryByTestId('crewmate-quiet-hint')).toBeNull()
  })

  it('the line carries words only — the ghost under it is the motion', async () => {
    const view = renderLive(liveStore({ kind: 'thinking' }))
    const line = await view.findByTestId('crewmate-live-activity')
    expect(line.querySelector('svg')).toBeNull()
  })

  it('while compacting, the line goes text-free (the footer under it already says "Compacting…") but keeps its height', async () => {
    const store = liveStore({ kind: 'thinking' })
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'compacting', content: '' } as Parameters<typeof sseChatMessage>[0]))
    const view = renderLive(store)
    const line = await view.findByTestId('crewmate-live-activity')
    expect(line.dataset.activity).toBe('compacting')
    expect(line).toHaveTextContent('')
    expect(view.getAllByText(/Compacting/)).toHaveLength(1)
  })

  it('a long step shows its FULL label (the pill clamps; this column has the room) and carries it as title', async () => {
    const long = 'Attempt an authenticated read of the SIM-T ticket through the alternate endpoint'
    const view = renderLive(liveStore({ kind: 'tool', purpose: long, toolName: 'curl', toolCallId: 'tc-1' }))
    await view.findByText(long)
    expect(view.getByTestId('crewmate-live-activity').querySelector('[role="status"]')).toHaveAttribute('title', long)
  })

  it('a BOUNDED window with no speech in it is not proof: the pane reads the whole history first', async () => {
    // Older speech behind fifty newer machinery rows: the bounded first read
    // (has_more) shows none of it. The pane must not say "hasn't said anything"
    // off that window — it upgrades to the unbounded read and draws the speech.
    const SPEECH = { role: 'assistant', content: 'Found the cause: the label was renamed.', cls: '', ts: '2026-09-21T09:00:00Z' }
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation((_key: string, limit?: number) =>
      Promise.resolve(limit === undefined
        ? { messages: [SPEECH, ...MACHINERY], running: false, has_more: false, total: MACHINERY.length + 1 }
        : { messages: MACHINERY, running: false, has_more: true, total: MACHINERY.length + 1 }))
    const view = renderPane()
    await waitFor(() => expect(view.getByText(/Found the cause/)).toBeInTheDocument())
    expect(view.queryByTestId('crewmate-quiet-hint')).toBeNull()
    const limits = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[1])
    expect(limits).toContain(undefined)
  })

  it('…and says who has not spoken only once the WHOLE history is machinery', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation((_key: string, limit?: number) =>
      Promise.resolve(limit === undefined
        ? { messages: MACHINERY, running: false, has_more: false, total: MACHINERY.length }
        : { messages: MACHINERY, running: false, has_more: true, total: MACHINERY.length + 40 }))
    const view = renderPane()
    const hint = await view.findByTestId('crewmate-quiet-hint')
    expect(hint).toHaveTextContent("Radar hasn't said anything to you yet.")
    const limits = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[1])
    expect(limits[limits.length - 1]).toBeUndefined()
  })

  it('a complete first read (no more behind it) is proof enough — no second read', async () => {
    const view = renderPane()
    await view.findByTestId('crewmate-quiet-hint')
    const limits = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[1])
    expect(limits.every((l) => l !== undefined)).toBe(true)
  })

  it('the "where the work went" line is a live link to the sessions it drives when the host can open it', async () => {
    const onOpenCrewWorkLog = vi.fn()
    const view = renderPane({ onOpenCrewWorkLog })
    const where = await view.findByTestId('crewmate-quiet-where')
    expect(where.tagName).toBe('BUTTON')
    expect(where).toHaveTextContent('See the sessions it is driving.')
    expect(where.className).toMatch(/underline/)
    fireEvent.click(where)
    expect(onOpenCrewWorkLog).toHaveBeenCalledTimes(1)
  })

  it('…and plain text — not a dead button — when it cannot', async () => {
    const view = renderPane({})
    const where = await view.findByTestId('crewmate-quiet-where')
    expect(where.tagName).toBe('DIV')
    expect(where.className).not.toMatch(/underline/)
  })

  it('the composer addresses the crewmate by name, not the product', async () => {
    const view = renderPane()
    await waitFor(() => expect(view.getByPlaceholderText(/Message Radar/)).toBeInTheDocument())
    expect(view.queryByPlaceholderText(/Message Kiro Crew/)).toBeNull()
  })

  it('an ordinary chat keeps the product placeholder', async () => {
    const view = renderPane({ crewmate: false })
    await waitFor(() => expect(view.getByPlaceholderText(/Message Kiro Crew/)).toBeInTheDocument())
  })
})

describe("Mate's narration before a tool call", () => {
  const TURN = [
    { role: 'user', content: 'Where is chat history?', cls: 'msg msg-u', ts: '2026-10-06T09:00:00Z' },
    { role: 'assistant', content: "I'll look up where chat history lives in the dashboard.", cls: '', ts: '2026-10-06T09:00:02Z' },
    { role: 'tool', content: '🔧 find_ui', cls: '', ts: '2026-10-06T09:00:03Z' },
    { role: 'assistant', content: 'Your older sessions are under Older Sessions.', cls: '', ts: '2026-10-06T09:00:09Z' },
  ]
  beforeEach(() => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: TURN, running: false, has_more: false, total: TURN.length })
  })

  it('is not drawn in Mate\'s chat once the answer follows it', async () => {
    const view = renderPane({ crewmate: true, identity: { name: 'mate', mate: true } })
    await view.findByText('Your older sessions are under Older Sessions.')
    expect(view.queryByText(/I'll look up where chat history lives/)).toBeNull()
  })

  it("stays a bubble in any other crewmate's chat", async () => {
    const view = renderPane({ crewmate: true })
    await view.findByText('Your older sessions are under Older Sessions.')
    expect(view.getByText(/I'll look up where chat history lives/)).toBeInTheDocument()
  })
})
