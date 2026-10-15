import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, fireEvent, waitFor, act, screen } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot, sseChatMessage } from '../store/chatSlice'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'

/* A crewmate's chat hands the newest error row its Resume (`continuable`,
 * `interrupted`, `continuing`, `onContinue`), and those four values are part of
 * the row set's identity. The pane's settled rows are reused only while that
 * identity holds, so the Resume values must stay put while a reply streams and
 * while the composer is typed into: neither may rebuild the row set or redraw a
 * settled row. Runs the real Resume hook (`useChatPaneContinue`), not a mock. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const continueSlot = vi.hoisted(() => vi.fn())
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
    continueSlot: (...a: unknown[]) => continueSlot(...a),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Kiro Crew', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

// Count how often the pane builds its row set, and which rows each set draws.
const built = vi.hoisted(() => ({ sets: 0, drawn: [] as string[] }))
vi.mock('../pages/chat/transcriptRenderers', async (importOriginal) => {
  const real = await importOriginal<typeof import('../pages/chat/transcriptRenderers')>()
  return {
    ...real,
    createTranscriptRenderers: (...args: Parameters<typeof real.createTranscriptRenderers>) => {
      built.sets += 1
      return real.createTranscriptRenderers(...args).map((r) => ({
        ...r,
        render: (m: Parameters<typeof r.render>[0], ctx: Parameters<typeof r.render>[1]) => {
          built.drawn.push(m.content)
          return r.render(m, ctx)
        },
      }))
    },
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import CrewComposer from '../pages/members/CrewComposer'
import { api } from '../api/client'

let SLOT = 'member-radar-reuse-0'
let slotSeq = 0
const detail = api.chatSlotDetail as ReturnType<typeof vi.fn>

const THROTTLED = 'Bedrock is throttling requests. Try: (1) wait a few seconds and retry, or (2) switch to a different model in the picker (e.g. sonnet).'
const row = (n: number, role: string, content: string, extra: Record<string, unknown> = {}) => ({
  role, content, ts: `2026-10-14T09:00:${String(n).padStart(2, '0')}Z`, cls: '', meta: { mid: `m${n}` }, ...extra,
})
const SETTLED = 'On it.'
const HISTORY = [
  row(0, 'user', 'Check the nightly build'),
  row(1, 'assistant', SETTLED),
  row(2, 'user', 'Status?'),
  row(3, 'error', THROTTLED, { cls: 'msg msg-err' }),
]

function renderPane() {
  detail.mockResolvedValue({ messages: HISTORY, running: false, has_more: false, total: HISTORY.length })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: HISTORY.length, running: false, mode: 'member', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  // The Members page never makes the DM the active chat slot.
  store.dispatch(setActiveSlot('front'))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} frameless agentLocked composerInput={CrewComposer} crewmate={{ name: 'Radar' }} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  return store
}

const chunk = (store: ReturnType<typeof renderPane>, content: string) =>
  act(() => { store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content } as Parameters<typeof sseChatMessage>[0])) })
const drawsOf = (content: string) => built.drawn.filter((c) => c === content).length
const type = (text: string) => fireEvent.change(screen.getByRole('textbox'), { target: { value: text } })

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  localStorage.setItem('mc-chat-config', JSON.stringify({ streamMode: 'immediate' }))
  __resetPaneDraftsForTests()
  continueSlot.mockReset()
  continueSlot.mockResolvedValue({ ok: true, slot: 'x' })
  SLOT = `member-radar-reuse-${++slotSeq}`
  built.sets = 0
  built.drawn.length = 0
})

describe("a mate's chat with Resume wired into its rows", () => {
  it('keeps the row set and the settled rows while the composer is typed into under an offered Resume', async () => {
    renderPane()
    // The turn ended on a model error: Resume is offered on the error row.
    await screen.findByTestId('error-card-continue')
    const sets = built.sets
    const settled = drawsOf(SETTLED)
    const errorDraws = drawsOf(THROTTLED)
    expect(settled).toBeGreaterThan(0)

    type('h')
    type('he')
    type('hey')

    expect(built.sets).toBe(sets)
    expect(drawsOf(SETTLED)).toBe(settled)
    expect(drawsOf(THROTTLED)).toBe(errorDraws)
    expect(screen.getByTestId('error-card-continue')).toBeInTheDocument()
  })

  it('keeps the row set and the settled rows while the resumed reply streams and the composer is typed into', async () => {
    const store = renderPane()
    fireEvent.click(await screen.findByTestId('error-card-continue'))
    await waitFor(() => expect(continueSlot).toHaveBeenCalledWith(SLOT))
    // The press redraws the error row: its Resume now waits for the turn.
    expect(screen.getByTestId('error-card-continue')).toBeDisabled()
    // The press took: the slots frame reports the crewmate running again.
    act(() => { store.dispatch(sseSlots([{ key: SLOT, title: 'Radar', messages: HISTORY.length, running: true, mode: 'member', agent: 'radar' }] as never)) })
    await waitFor(() => expect(screen.queryByTestId('error-card-continue')).toBeNull())
    // The first chunk joins the transcript; measure from there.
    chunk(store, 'Retrying')
    await screen.findByText('Retrying')
    const sets = built.sets
    const settled = drawsOf(SETTLED)
    const errorDraws = drawsOf(THROTTLED)

    chunk(store, ' the build')
    chunk(store, ' check now.')
    type('thanks')
    chunk(store, ' Still running.')

    expect(screen.getByText('Retrying the build check now. Still running.')).toBeInTheDocument()
    expect(built.sets).toBe(sets)
    expect(drawsOf(SETTLED)).toBe(settled)
    expect(drawsOf(THROTTLED)).toBe(errorDraws)
  })
})
