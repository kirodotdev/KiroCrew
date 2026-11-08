/**
 * Test: the Older Sessions search says when the backend's scan window left older
 * sessions unsearched (#22233).
 *
 * `/api/sessions/search` reads only the `session.search_scan_window` most recent
 * transcripts and, when that leaves sessions out, adds `scan: {searched, total}`.
 * The pane must render that under the results and under the empty state, so "no
 * matches" does not read as "not in history"; with no `scan` it says nothing.
 *
 * Mock scaffolding mirrors ChatSidebar.historySearchOrder.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const { sessionsSearchMock } = vi.hoisted(() => ({ sessionsSearchMock: vi.fn() }))

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
import { sessionSearchScan } from '../pages/chat-sidebar/search'
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

async function search(query: string) {
  fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
  fireEvent.change(screen.getByPlaceholderText(/search older sessions/i), { target: { value: query } })
  await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith(query))
}

describe('ChatSidebar – history search scan-window note', () => {
  beforeEach(() => {
    sessionsSearchMock.mockReset()
    localStorage.clear()
  })

  it('says how many sessions were searched under the results', async () => {
    sessionsSearchMock.mockResolvedValue({
      sessions: [{ key: 'dashboard_chat-9', title: 'Platypus plan', modified: 3_000_000 }],
      scan: { searched: 2000, total: 2150 },
    })
    renderSidebar()
    await search('platypus')
    await screen.findByText('Platypus plan')
    expect(await screen.findByTestId('history-search-scan-note')).toHaveTextContent(
      'Searched the 2,000 most recent of 2,150 sessions; older sessions were not searched.',
    )
  })

  it('says it under the empty state too', async () => {
    sessionsSearchMock.mockResolvedValue({ sessions: [], scan: { searched: 2000, total: 2150 } })
    renderSidebar()
    await search('platypus')
    expect(await screen.findByText(/no sessions match/i)).toBeInTheDocument()
    expect(await screen.findByTestId('history-search-scan-note')).toBeInTheDocument()
  })

  it('says nothing when the window reached every session', async () => {
    sessionsSearchMock.mockResolvedValue({
      sessions: [{ key: 'dashboard_chat-9', title: 'Platypus plan', modified: 3_000_000 }],
    })
    renderSidebar()
    await search('platypus')
    await screen.findByText('Platypus plan')
    expect(screen.queryByTestId('history-search-scan-note')).toBeNull()
  })

  it('ignores a malformed scan', async () => {
    sessionsSearchMock.mockResolvedValue({
      sessions: [{ key: 'dashboard_chat-9', title: 'Platypus plan', modified: 3_000_000 }],
      scan: { searched: 'all', total: 2150 },
    })
    renderSidebar()
    await search('platypus')
    await screen.findByText('Platypus plan')
    expect(screen.queryByTestId('history-search-scan-note')).toBeNull()
  })

  it('names this machine when the federated search reports a local count', async () => {
    sessionsSearchMock.mockResolvedValue({
      sessions: [{ key: 'dashboard_chat-9', title: 'Platypus plan', modified: 3_000_000 }],
      scan: { searched: 2000, total: 2150, scope: 'local' },
    })
    renderSidebar()
    await search('platypus')
    const note = await screen.findByTestId('history-search-scan-note')
    expect(note).toHaveAttribute('role', 'status')
    expect(note).toHaveTextContent(
      'Searched the 2,000 most recent of 2,150 sessions on this machine; older sessions here were not searched.',
    )
  })
})

describe('sessionSearchScan', () => {
  it.each([
    [{ scan: { searched: -1, total: 5 } }],
    [{ scan: { searched: 5, total: 5 } }],
    [{ scan: { searched: 1, total: Number.MAX_SAFE_INTEGER + 2 } }],
    [{ scan: { searched: 1.5, total: 5 } }],
    [{ scan: null }],
    [null],
  ])('rejects %j', (d) => {
    expect(sessionSearchScan(d)).toBeNull()
  })

  it('reads the counts and the local scope', () => {
    expect(sessionSearchScan({ scan: { searched: 0, total: 3 } })).toEqual({ searched: 0, total: 3, local: false })
    expect(sessionSearchScan({ scan: { searched: 2, total: 3, scope: 'local' } })).toEqual({ searched: 2, total: 3, local: true })
  })
})
