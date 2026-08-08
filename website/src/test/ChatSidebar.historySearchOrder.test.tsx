/**
 * Test: Older Sessions search results preserve the backend's relevance order.
 *
 * Regression for the ranking bug where `sortedHistory` re-sorted active
 * content-search results into the sidebar sort key (date-desc by default),
 * burying an exact title match (which the backend ranks first via the
 * `_TITLE_BOOST` field boost in `search_sessions`) below fresher sessions
 * that merely mention the query in their body content.
 *
 * The mocked `/api/sessions/search` response deliberately returns the
 * relevance-ranked list with the exact-title match FIRST but with the OLDEST
 * `modified` timestamp, so a date-desc re-sort would demote it to last —
 * exactly the observed defect. The assertions lock the rendered DOM order to
 * the backend order.
 *
 * Mock scaffolding mirrors ChatSidebar.offline.test.tsx (the file that owns
 * the component's mock setup).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Relevance-ranked backend response: exact title match first (oldest),
// content-only mentions after (newer). A date-desc re-sort would invert this.
// Single source of truth — the mock AND the fixture-validity guard both read
// this constant, so editing timestamps here cannot silently defuse the guard.
const { SEARCH_FIXTURE, sessionsSearchMock } = vi.hoisted(() => {
  const SEARCH_FIXTURE = [
    // Backend rank 1: title match (10x field boost) — oldest of the three.
    { key: 'cron_target', title: 'Cron job bug fixes investigation', modified: 1_000_000 },
    // Backend ranks 2-3: content-only matches — fresher timestamps.
    { key: 'dashboard_chat-9', title: 'Fresh unrelated title', modified: 3_000_000, snippet: 'we discussed cron job bug fixes here' },
    { key: 'dashboard_chat-8', title: 'Middle unrelated title', modified: 2_000_000, snippet: 'more cron job bug fixes chatter' },
  ]
  return {
    SEARCH_FIXTURE,
    sessionsSearchMock: vi.fn().mockResolvedValue({ sessions: SEARCH_FIXTURE }),
  }
})

// Mock API client (sidebar fires fetch calls on mount). importOriginal keeps
// non-`api` exports (e.g. SEARCH_MIN_CHARS used by the search debounce) real.
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
          'renameSlot', 'forkSession',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      chatFolders: vi.fn().mockResolvedValue([]),
      sessionsSearch: sessionsSearchMock,
    },
  }
})

// Browser API stubs
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

function renderSidebar() {
  const slots = [slot('s1', 'Session 1')]
  const history = [histItem('h1', 'Placeholder history row')]
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
  render(
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

describe('ChatSidebar – history search preserves backend relevance order', () => {
  beforeEach(() => {
    sessionsSearchMock.mockClear()
    localStorage.clear()
  })

  it('renders search results in backend order, not re-sorted by date', async () => {
    renderSidebar()

    // Expand the (collapsed-by-default) Older Sessions pane, then search.
    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    fireEvent.change(screen.getByPlaceholderText(/search older sessions/i), {
      target: { value: 'cron job bug fixes' },
    })

    // The search is debounced (250ms) before hitting the mocked endpoint.
    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith('cron job bug fixes'))
    const target = await screen.findByText('Cron job bug fixes investigation')
    const fresh = screen.getByText('Fresh unrelated title')
    const middle = screen.getByText('Middle unrelated title')

    // DOM order must match the backend's relevance ranking: title match first,
    // then the content-only matches in returned order. compareDocumentPosition
    // bit 4 (DOCUMENT_POSITION_FOLLOWING) means the argument comes AFTER `this`.
    expect(target.compareDocumentPosition(fresh) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(fresh.compareDocumentPosition(middle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('date-desc re-sort would have inverted this fixture (guards fixture validity)', () => {
    // Validate that the fixture actually exercises the regression: sorted by
    // modified desc, the title match would land LAST. Derived from the SAME
    // constant the mock serves — if someone edits SEARCH_FIXTURE such that
    // date order equals relevance order, the main assertion above would pass
    // vacuously and this guard fails instead.
    const byDateDesc = [...SEARCH_FIXTURE].sort((a, b) => b.modified - a.modified).map(s => s.key)
    expect(SEARCH_FIXTURE[0].key).toBe('cron_target')
    expect(byDateDesc[byDateDesc.length - 1]).toBe('cron_target')
    expect(byDateDesc).not.toEqual(SEARCH_FIXTURE.map(s => s.key))
  })
})
