/**
 * The transcript skeleton on a pane's first load (a bot's chat, a bot's
 * session in the side panel). It shows only when the host already knows the
 * chat has history; a chat that never talked, or one the host cannot vouch
 * for, must never draw it, not even for one frame.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'
import { api } from '../api/client'
import ChatPane from './ChatPane'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn(),
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
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

type Detail = Awaited<ReturnType<typeof api.chatSlotDetail>>
const SKELETON = 'chat-pane-skeleton'

afterEach(() => { cleanup(); vi.mocked(api.chatSlotDetail).mockReset() })

/** The slot's first page, held until the test lets it land. */
function holdFirstPage() {
  let land!: (d: Detail) => void
  vi.mocked(api.chatSlotDetail).mockReturnValue(new Promise<Detail>((resolve) => { land = resolve }))
  return (messages: unknown[]) => act(async () => {
    land({ messages, running: false, has_more: false, total: messages.length } as unknown as Detail)
  })
}

/** Records whether the skeleton was EVER in the document, from before the
 *  first commit on: a mutation observer sees a node that one render added and
 *  the next removed, which a later query would miss. */
function watchSkeleton() {
  let seen = false
  const selector = `[data-testid="${SKELETON}"]`
  const isSkeleton = (n: Node) => n instanceof Element && (n.matches(selector) || !!n.querySelector(selector))
  // The records' own nodes, not the live DOM: a skeleton that came and went
  // before this callback runs is an added node, or (when it came inside a
  // larger subtree) a removed one.
  const touched = (records: MutationRecord[]) =>
    records.some(r => [...Array.from(r.addedNodes), ...Array.from(r.removedNodes)].some(isSkeleton))
  const observer = new MutationObserver((records) => { if (touched(records)) seen = true })
  observer.observe(document.body, { childList: true, subtree: true })
  return {
    seen: () => {
      if (touched(observer.takeRecords())) seen = true
      if (document.querySelector(selector)) seen = true
      return seen
    },
    stop: () => observer.disconnect(),
  }
}

function renderPane(slot: string, historyKnown: boolean | undefined, qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slot, messages: 3, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const pane = (known: boolean | undefined) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slot} frameless crewmate={{ name: 'bot' }} historyKnown={known} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
  const view = render(pane(historyKnown))
  return { ...view, qc, rerenderPane: (known: boolean | undefined) => view.rerender(pane(known)) }
}

const reply = { role: 'assistant', content: 'hello again', cls: '', ts: '2026-11-20T10:00:00Z' }

describe('ChatPane first-load skeleton', () => {
  it('shows while a chat with history loads its first page, and goes once it lands', async () => {
    const land = holdFirstPage()
    const view = renderPane('member-has-history', true)
    expect(screen.getByTestId(SKELETON)).toBeInTheDocument()
    expect(view.container.querySelector('[data-chat-pane]')).toHaveAttribute('aria-busy', 'true')
    await land([reply])
    expect(screen.queryByTestId(SKELETON)).toBeNull()
    expect(view.container.querySelector('[data-chat-pane]')).not.toHaveAttribute('aria-busy')
  })

  it('goes away over a chat that loads with no rows', async () => {
    const land = holdFirstPage()
    renderPane('member-loads-empty', true)
    expect(screen.getByTestId(SKELETON)).toBeInTheDocument()
    await land([])
    expect(screen.queryByTestId(SKELETON)).toBeNull()
  })

  it('never shows on a later visit whose page is already cached', async () => {
    const land = holdFirstPage()
    const first = renderPane('member-revisit', true)
    await land([reply])
    first.unmount()
    const watch = watchSkeleton()
    renderPane('member-revisit', true, first.qc)
    expect(watch.seen()).toBe(false)
    watch.stop()
  })

  it.each([
    ['no history', false],
    ['unknown history', undefined],
  ])('never draws it, not even on the first render, for %s', async (_case, known) => {
    const land = holdFirstPage()
    const watch = watchSkeleton()
    renderPane(`member-${String(known)}`, known)
    expect(watch.seen()).toBe(false)
    await land([])
    expect(watch.seen()).toBe(false)
    watch.stop()
  })

  it('never draws it when the signal arrives later and says no history', async () => {
    const land = holdFirstPage()
    const watch = watchSkeleton()
    const view = renderPane('member-late-no', undefined)
    view.rerenderPane(false)
    expect(watch.seen()).toBe(false)
    await land([])
    expect(watch.seen()).toBe(false)
    watch.stop()
  })

  it('shows once a late signal says history while the page is still loading', async () => {
    const land = holdFirstPage()
    const view = renderPane('member-late-yes', undefined)
    expect(screen.queryByTestId(SKELETON)).toBeNull()
    view.rerenderPane(true)
    expect(screen.getByTestId(SKELETON)).toBeInTheDocument()
    await land([reply])
    expect(screen.queryByTestId(SKELETON)).toBeNull()
  })
})
