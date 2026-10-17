import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, within } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot, sseChatMessage } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { fmtMessageTimeFull } from '../pages/chat/messageTime'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'

/* A crewmate's chat (ChatPane with `crewmate`, as the Members page mounts it)
 * reuses its settled rows while a reply streams, and the rows that change in
 * place still change: each bubble's place in its run (its corners) and the
 * footer the run's last bubble carries. Frames reach the pane through the
 * store the way the WebSocket layer delivers them to a background slot. The
 * last case needs the unfiltered transcript: the patrol wake between two
 * turns is not drawn, and only it says the earlier reply ended its run. */

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
// A minute apart: the footer's full-date title is minute-precise, and the
// replies of one turn share a transcript row, so each must read differently.
const at = (min: number) => `2026-03-02T10:${String(min).padStart(2, '0')}:00Z`

const Q1 = { role: 'user', content: 'Is the build green?', cls: '', ts: at(1), meta: { mid: 'm1' } }
const A1 = { role: 'assistant', content: 'Checking the last run.', cls: '', ts: at(2), meta: { mid: 'm2' } }
const A2 = { role: 'assistant', content: 'It is green.', cls: '', ts: at(3), meta: { mid: 'm3' } }

function renderCrewmate() {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 3, running: false, mode: 'member', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
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
            <ChatPane slotKey={SLOT} crewmate={{ name: 'Radar' }} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  return store
}

/** One WebSocket frame for the DM's slot. */
const frame = (store: ReturnType<typeof renderCrewmate>, role: string, content: string, extra: Record<string, unknown> = {}) =>
  act(() => { store.dispatch(sseChatMessage({ slot: SLOT, role, content, ...extra } as Parameters<typeof sseChatMessage>[0])) })

/** The bubble that draws this text, and the transcript row around it. */
const bubbleOf = (text: string) => {
  const bubble = screen.getByText(text).closest('.message-bubble')
  if (!bubble) throw new Error(`no bubble draws ${JSON.stringify(text)}`)
  return bubble as HTMLElement
}
const rowOf = (text: string) => {
  const row = screen.getByText(text).closest('[data-display-index]')
  if (!row) throw new Error(`no transcript row holds ${JSON.stringify(text)}`)
  return row as HTMLElement
}
const footerOf = (text: string, ts: string) => within(rowOf(text)).queryByTitle(fmtMessageTimeFull(ts))

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  // Draw streamed text as it arrives (Settings → Chat → streaming), so a
  // chunk's words are on screen in the same render.
  localStorage.setItem('mc-chat-config', JSON.stringify({ streamMode: 'immediate' }))
  __resetPaneDraftsForTests()
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
    messages: [Q1, A1, A2], running: false, has_more: false, total: 3,
  })
})

describe("a crewmate's run while a reply streams", () => {
  it('regroups the settled bubbles and moves the footer as the reply joins, streams and settles', async () => {
    const store = renderCrewmate()
    await screen.findByText(A2.content)
    // Two replies of one turn: a1 opens the run, a2 ends it and carries the footer.
    expect(bubbleOf(A1.content)).toHaveClass('rounded-bl-md')
    expect(bubbleOf(A2.content)).toHaveClass('rounded-tl-md')
    expect(footerOf(A2.content, A2.ts)).not.toBeNull()

    // The crewmate keeps talking: the streaming reply continues the run.
    frame(store, 'chunk', 'And the deploy')
    expect(bubbleOf(A2.content)).toHaveClass('rounded-l-md')
    expect(bubbleOf(A2.content)).not.toHaveClass('rounded-tl-md')
    expect(footerOf(A2.content, A2.ts)).toBeNull()
    frame(store, 'chunk', ' finished too.')
    expect(screen.getByText('And the deploy finished too.')).toBeInTheDocument()
    expect(bubbleOf(A2.content)).toHaveClass('rounded-l-md')

    // The reply settles and the turn ends: the new reply ends the run.
    const settled = 'And the deploy finished too.'
    frame(store, 'assistant', settled, { ts: at(4), meta: { mid: 'm4' } })
    frame(store, '_done', '')
    expect(bubbleOf(settled)).toHaveClass('rounded-tl-md')
    expect(footerOf(settled, at(4))).not.toBeNull()
    expect(bubbleOf(A2.content)).toHaveClass('rounded-l-md')
    expect(footerOf(A2.content, A2.ts)).toBeNull()
  })

  it('keeps a run ended at a patrol wake that is not drawn, while the next turn streams below it', async () => {
    const store = renderCrewmate()
    await screen.findByText(A2.content)
    // A patrol wake opens the next turn. The chat does not draw it.
    frame(store, 'nudge', '[auto-nudge cycle 1]\nPatrol.', { ts: at(5), meta: { mid: 'm5' } })
    expect(screen.queryByText(/auto-nudge/)).toBeNull()
    frame(store, 'chunk', 'Nothing new')
    frame(store, 'chunk', ' since the last check.')
    // a2 still ends its own turn's run: corners and footer stay.
    expect(bubbleOf(A2.content)).toHaveClass('rounded-tl-md')
    expect(footerOf(A2.content, A2.ts)).not.toBeNull()
    // The new turn's reply opens a run of its own.
    expect(bubbleOf('Nothing new since the last check.')).not.toHaveClass('rounded-tl-md')
    expect(bubbleOf('Nothing new since the last check.')).not.toHaveClass('rounded-l-md')
  })
})
