/**
 * Test: active-session search results preserve the backend's relevance order.
 *
 * Regression for the ranking bug where `filteredSlots` collapsed the
 * `/api/sessions/search` response into a membership Set and re-sorted the
 * matches with `comparePinnedThenSort` (pinned first, then date-desc by
 * default), burying a title match (which the backend ranks first via the
 * `_TITLE_BOOST` field boost in `search_sessions`) below fresher — or merely
 * pinned — sessions that only mention the query in their body content.
 *
 * The mocked `/api/sessions/search` response deliberately returns the
 * relevance-ranked list with the title match FIRST but with the OLDEST
 * `last_ts`, while one content-only decoy is PINNED and the other is the
 * freshest — so the old pin+date re-sort would demote the title match to
 * last. The assertions lock the rendered DOM order to the backend order.
 *
 * The companion lane (Older Sessions) has the same contract in
 * ChatSidebar.historySearchOrder.test.tsx; mock scaffolding mirrors that file.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Relevance-ranked backend response: title match first (oldest slot), then the
// content-only mentions (pinned / fresher). Backend keys carry the
// `dashboard_` prefix that the sidebar strips to map them onto slot keys.
// Single source of truth — the mock AND the fixture-validity guard both read
// this constant, so editing it cannot silently defuse the guard.
const { SEARCH_FIXTURE, sessionsSearchMock } = vi.hoisted(() => {
  const SEARCH_FIXTURE = [
    // Backend rank 1: title match (10x field boost) — oldest of the three.
    { key: 'dashboard_chat-target', title: 'Managing session overload strategies', modified: 1_000_000 },
    // Backend ranks 2-3: content-only matches — pinned and/or fresher.
    { key: 'dashboard_chat-pinned', title: 'Pinned unrelated title', modified: 3_000_000, snippet: 'we discussed session overload here' },
    { key: 'dashboard_chat-fresh', title: 'Fresh unrelated title', modified: 2_000_000, snippet: 'more session overload chatter' },
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
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

const slot = (key: string, title: string, lastTs: string, pinned = false): ChatSlot => ({
  key, title, messages: 1, running: false, mode: '', created: '', last_ts: lastTs, pinned,
} as ChatSlot)

// Slot-side mirror of SEARCH_FIXTURE: the title match is the OLDEST and
// unpinned; both decoys would outrank it under pin-first + date-desc.
const SLOTS = [
  slot('chat-target', 'Managing session overload strategies', '2026-01-01T00:00:00Z'),
  slot('chat-pinned', 'Pinned unrelated title', '2026-03-01T00:00:00Z', true),
  slot('chat-fresh', 'Fresh unrelated title', '2026-02-01T00:00:00Z'),
]

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: true,
      slots: SLOTS,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 'chat-target',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined,
      history: [], historyHasMore: false, historyOffset: 0,
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
              slots={SLOTS}
              activeSlot={'chat-target'}
              unreadSlots={[]}
              history={[]}
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

describe('ChatSidebar – active-session search preserves backend relevance order', () => {
  beforeEach(() => {
    sessionsSearchMock.mockClear()
    localStorage.clear()
  })

  it('renders search results in backend order, not re-sorted by pin/date', async () => {
    renderSidebar()

    fireEvent.change(screen.getByPlaceholderText(/search sessions/i), {
      target: { value: 'session overload' },
    })

    // The search is debounced (250ms) before hitting the mocked endpoint.
    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith('session overload'))

    // Once the ranked response lands, all three matches render — in backend
    // order. compareDocumentPosition bit 4 (DOCUMENT_POSITION_FOLLOWING) means
    // the argument comes AFTER `this`.
    await waitFor(() => {
      const target = screen.getByText('Managing session overload strategies')
      const pinnedDecoy = screen.getByText('Pinned unrelated title')
      const fresh = screen.getByText('Fresh unrelated title')
      expect(target.compareDocumentPosition(pinnedDecoy) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(pinnedDecoy.compareDocumentPosition(fresh) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })
  })

  it('pin-first + date-desc re-sort would have inverted this fixture (guards fixture validity)', () => {
    // Validate that the fixture actually exercises the regression: under the
    // old ordering (pinned first, then last_ts desc) the title match lands
    // LAST. Derived from the SAME constants the mock serves — if someone edits
    // the fixture such that pin/date order equals relevance order, the main
    // assertion above would pass vacuously and this guard fails instead.
    const oldOrder = [...SLOTS]
      .sort((a, b) => {
        const pa = a.pinned ? 0 : 1
        const pb = b.pinned ? 0 : 1
        if (pa !== pb) return pa - pb
        return new Date(b.last_ts!).getTime() - new Date(a.last_ts!).getTime()
      })
      .map(s => s.key)
    const backendOrder = SEARCH_FIXTURE.map(s => s.key.replace(/^dashboard_/, ''))
    expect(backendOrder[0]).toBe('chat-target')
    expect(oldOrder[oldOrder.length - 1]).toBe('chat-target')
    expect(oldOrder).not.toEqual(backendOrder)
  })
})
