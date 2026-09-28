import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, waitFor, fireEvent } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
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

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: MACHINERY.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(opts: { onOpenCrewWorkLog?: () => void; crewmate?: boolean } = { crewmate: true }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={makeStore()}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} crewmate={opts.crewmate === false ? undefined : radar} onOpenCrewWorkLog={opts.onOpenCrewWorkLog} />
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

  it('the "where the work went" line is a live link to the Work log when the host can open it', async () => {
    const onOpenCrewWorkLog = vi.fn()
    const view = renderPane({ onOpenCrewWorkLog })
    const where = await view.findByTestId('crewmate-quiet-where')
    expect(where.tagName).toBe('BUTTON')
    expect(where).toHaveTextContent('Work log')
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
