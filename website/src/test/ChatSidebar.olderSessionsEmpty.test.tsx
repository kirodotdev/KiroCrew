/**
 * Test: the Older Sessions pane states its own emptiness.
 *
 * The pane lists the sessions that are NOT open as a tab, so an empty list is
 * an ordinary state — every session on disk is already open, which is the common
 * case for a light user. Rendering only the search box over blank space reads as
 * a broken pane. A filtered-to-nothing list is a different statement and reuses
 * the wording the two sibling panes already use.
 *
 * Mock scaffolding mirrors ChatSidebar.historySearchOrder.test.tsx (the file
 * that owns this pane's mock setup).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
          'renameSlot', 'forkSession', 'sessionsSearch',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      chatFolders: vi.fn().mockResolvedValue([]),
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

function renderSidebar(history: ChatHistoryItem[]) {
  const slots = [slot('s1', 'Session 1')]
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

describe('ChatSidebar – Older Sessions empty state', () => {
  beforeEach(() => { localStorage.clear() })

  it('says the pane is empty instead of rendering a bare search box', () => {
    renderSidebar([])
    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    expect(screen.getByText(/no older sessions/i)).toBeInTheDocument()
  })

  it('says no match when a filter emptied a non-empty list', () => {
    renderSidebar([histItem('dashboard_chat-9', 'Some closed session')])
    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    // One character stays below the content-search threshold, so this filters
    // the already-fetched rows client-side rather than hitting the endpoint.
    fireEvent.change(screen.getByPlaceholderText(/search older sessions/i), {
      target: { value: 'z' },
    })
    expect(screen.getByText(/no sessions match/i)).toBeInTheDocument()
    expect(screen.queryByText(/no older sessions/i)).not.toBeInTheDocument()
  })
})
