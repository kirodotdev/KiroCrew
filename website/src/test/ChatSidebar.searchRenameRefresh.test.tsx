/**
 * Test: renaming a session surfaces it in the active-session search results.
 *
 * Regression for the bug where the slot filter deferred entirely to the backend
 * result set once the query reached `SEARCH_MIN_CHARS`, so a session whose TITLE
 * matches the live query stayed hidden until the query string itself was edited —
 * the backend search only re-fired on a query change, and a rename changes
 * neither the query nor the already-returned backend key set.
 *
 * Two assertions, one per half of the fix: the filter now ORs a local title match
 * with the backend keys, so a locally-matching session renders against an empty
 * backend result set; and the search hook takes a revalidate signal derived from
 * session titles, so a rename re-runs `sessionsSearch` for the unchanged query.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// The backend returns NO hits: this models the window between a rename and the
// content index catching up, where the pre-fix filter hid the row.
const { sessionsSearchMock } = vi.hoisted(() => ({
  sessionsSearchMock: vi.fn().mockResolvedValue({ sessions: [] }),
}))

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

const QUERY = 'quarterly review notes'

const slot = (key: string, title: string): ChatSlot => ({
  key, title, messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z', pinned: false,
} as ChatSlot)

const RENAMED = slot('chat-renamed', QUERY)
const DECOY = slot('chat-decoy', 'Unrelated title')

function renderSidebar(slots: ChatSlot[]) {
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
      activeSlot: 'chat-decoy',
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
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots}
              activeSlot={'chat-decoy'}
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
  // Re-render with a new `slots` array to model a rename landing from the store.
  const rerender = (next: ChatSlot[]) => view.rerender(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={next}
              activeSlot={'chat-decoy'}
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
  return { rerender }
}

describe('ChatSidebar – search results honour a locally-renamed title', () => {
  beforeEach(() => {
    sessionsSearchMock.mockClear()
    localStorage.clear()
  })

  it('shows a title-matching session while the backend result set is empty', async () => {
    renderSidebar([RENAMED, DECOY])

    fireEvent.change(screen.getByPlaceholderText(/search sessions/i), { target: { value: QUERY } })

    // Wait for the empty backend response to land, so the assertion runs in the
    // state that used to hide the row rather than before the search resolved.
    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith(QUERY))
    await waitFor(() => expect(screen.getByText(QUERY)).toBeTruthy())
    expect(screen.queryByText('Unrelated title')).toBeNull()
  })

  it('re-runs the backend search when a title changes, without re-typing', async () => {
    const { rerender } = renderSidebar([slot('chat-renamed', 'Old title'), DECOY])

    fireEvent.change(screen.getByPlaceholderText(/search sessions/i), { target: { value: QUERY } })
    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith(QUERY))
    const callsBeforeRename = sessionsSearchMock.mock.calls.length

    rerender([RENAMED, DECOY])

    // Same query, so the debounced effect cannot fire again — only the revalidate
    // signal can. The throttle window is 100ms.
    await waitFor(() => expect(sessionsSearchMock.mock.calls.length).toBeGreaterThan(callsBeforeRename))
    expect(sessionsSearchMock).toHaveBeenLastCalledWith(QUERY)
  })

  it('does not append a session that matches only on agent, not title', async () => {
    const agentOnly = { ...slot('chat-agent-only', 'Agent only row'), agent: QUERY } as ChatSlot
    renderSidebar([agentOnly, DECOY])

    fireEvent.change(screen.getByPlaceholderText(/search sessions/i), { target: { value: QUERY } })

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith(QUERY))
    // The backend excluded it, and a rename cannot have produced it, so the local
    // OR must not resurrect it as an unranked tail row.
    expect(screen.queryByText('Agent only row')).toBeNull()
  })
})
