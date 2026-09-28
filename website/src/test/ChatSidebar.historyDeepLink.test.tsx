/**
 * Test: `/chat?history=1` lands the user ON the Older Sessions pane.
 *
 * WHY THIS EXISTS
 *
 * Issue Radar's detail header can only ever POINT at a concluded item's
 * transcript. The session was closed by the user, and rehydrating a user-closed
 * session is deliberately refused (`adopt_closed` gates it and that app never
 * passes it) — but closing archives the session first, so the transcript is still
 * readable under Older Sessions. Before this, the notice NAMED that pane and
 * offered nothing that went there, so the one next step the copy described was the
 * one step the user could not take. The param is what makes the name a
 * destination.
 *
 * The pane is collapsed by default and its disclosure is this component's own
 * state, so the arrival intent has to be read here. Two properties matter and
 * both are asserted: the pane is OPEN, and it has FETCHED — the toggle fetches
 * when it opens the pane, so a pane that starts open would otherwise render its
 * empty state over real history.
 *
 * Mock scaffolding mirrors ChatSidebar.historySearchOrder.test.tsx (which mirrors
 * ChatSidebar.offline.test.tsx, the file that owns this component's mock setup).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const { sessionsMock } = vi.hoisted(() => ({
  sessionsMock: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList',
          'renameSlot', 'forkSession', 'sessionsSearch',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      chatFolders: vi.fn().mockResolvedValue([]),
      // What `fetchHistory` actually calls (chatSlice) — the observable signal
      // that the pane loaded, same one ChatPage.lazyHistoryFetch asserts on.
      sessions: sessionsMock,
    },
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar from '../pages/ChatSidebar'
import type { ChatSlot, ChatHistoryItem } from '../types'
import type { RootState } from '../store'

const slot = (key: string, title?: string): ChatSlot => ({
  key, title: title ?? key, messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
} as ChatSlot)

const histItem = (key: string, title: string): ChatHistoryItem => ({
  key, title, last_ts: '2026-01-01T00:00:00Z',
} as unknown as ChatHistoryItem)

const ARCHIVED_TITLE = '#6270 · a declined re-investigate'

function renderSidebar() {
  const slots = [slot('s1', 'Session 1')]
  const history = [histItem('h1', ARCHIVED_TITLE)]
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: true,
      slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 's1',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined,
      history, historyHasMore: false, historyOffset: history.length,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots}
              activeSlot={'s1'}
              unreadSlots={[]}
              history={history}
              historyHasMore={false}
              defaultAgent={'default'}
              installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The disclosure row carries `aria-expanded`, so the pane's state is readable
 *  without depending on which rows happen to be rendered inside it. */
const disclosure = () => screen.getByRole('button', { name: /^older sessions$/i })

describe('ChatSidebar — arriving with ?history=1', () => {
  beforeEach(() => {
    sessionsMock.mockClear()
    localStorage.clear()
    window.history.replaceState({}, '', '/chat')
  })
  afterEach(() => { window.history.replaceState({}, '', '/') })

  it('opens the Older Sessions pane and loads it', async () => {
    window.history.replaceState({}, '', '/chat?history=1')
    renderSidebar()

    expect(disclosure().getAttribute('aria-expanded')).toBe('true')
    // Open is not enough: the toggle is what normally fetches, so a pane opened
    // by the URL has to fetch on its own or it shows "no older sessions" over a
    // history that exists.
    await waitFor(() => expect(sessionsMock).toHaveBeenCalled())
    expect(await screen.findByText(ARCHIVED_TITLE)).toBeTruthy()
  })

  it('stays collapsed and fetches nothing without the param', async () => {
    renderSidebar()
    expect(disclosure().getAttribute('aria-expanded')).toBe('false')
    // Mirrors ChatPage.lazyHistoryFetch: history is lazy, and this change must
    // not turn it into an unconditional fetch on every sidebar mount.
    expect(sessionsMock).not.toHaveBeenCalled()
    expect(screen.queryByText(ARCHIVED_TITLE)).toBeNull()
  })

  it('ignores a value that is not the opt-in', async () => {
    window.history.replaceState({}, '', '/chat?history=0&sid=chat-3')
    renderSidebar()
    expect(disclosure().getAttribute('aria-expanded')).toBe('false')
    expect(sessionsMock).not.toHaveBeenCalled()
  })
})
