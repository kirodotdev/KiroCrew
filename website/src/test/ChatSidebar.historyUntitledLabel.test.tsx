/**
 * Test: an untitled Older Sessions row reads "Untitled session", never its key
 * (#20876).
 *
 * A history row used to show `title || key`, so a session with no title (or a
 * title equal to its key) showed a machine id such as `chat-9-1785440411` in
 * both its text and its hover tooltip. The row now reads the shared
 * `sessionDisplayTitle` label. Resume still sends the session key, and the
 * resume payload keeps its old `title || key` value: only the shown label
 * changes.
 *
 * Mock scaffolding mirrors ChatSidebar.olderSessionsTip.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const { sessionsMock, resumeMock } = vi.hoisted(() => ({
  sessionsMock: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
  resumeMock: vi.fn().mockResolvedValue({}),
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'deleteSession', 'agentDetail', 'spawnList',
          'renameSlot', 'forkSession', 'sessionsSearch',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      chatFolders: vi.fn().mockResolvedValue([]),
      resumeChatSlot: resumeMock,
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

const slot = (key: string, title: string): ChatSlot => ({
  key, title, messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
} as ChatSlot)

const histItem = (key: string, title: string, lastTs: string): ChatHistoryItem => ({
  key, title, last_ts: lastTs, modified: lastTs,
} as unknown as ChatHistoryItem)

const UNTITLED = 'chat-9-1785440411'
const KEY_TITLED = 'chat-8-1785440400'
const HISTORY = [
  histItem(UNTITLED, '', '2026-01-03T00:00:00Z'),
  histItem(KEY_TITLED, KEY_TITLED, '2026-01-02T00:00:00Z'),
  histItem('h-named', 'Named work', '2026-01-01T00:00:00Z'),
]

function renderSidebar() {
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
      history: HISTORY, historyHasMore: false, historyOffset: HISTORY.length,
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
              history={HISTORY}
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

/** Every history row in the open pane: each is a role=button carrying the
 *  session's hover tooltip. */
const historyRows = () => Array.from(document.querySelectorAll<HTMLElement>('#history-pane [role="button"][title]'))

describe('ChatSidebar — untitled Older Sessions rows', () => {
  beforeEach(() => {
    sessionsMock.mockReset().mockResolvedValue({ sessions: HISTORY, has_more: false })
    resumeMock.mockClear()
    localStorage.clear()
    window.history.replaceState({}, '', '/chat')
  })
  afterEach(() => { window.history.replaceState({}, '', '/') })

  it('labels an untitled row "Untitled session" in text and tooltip, and resumes its key', async () => {
    renderSidebar()
    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    await waitFor(() => expect(historyRows()).toHaveLength(3))

    const rows = historyRows()
    const titles = rows.map(r => r.getAttribute('title'))
    expect(titles).toEqual(['Untitled session', 'Untitled session', 'Named work'])
    for (const row of rows.slice(0, 2)) {
      expect(row.textContent).toContain('Untitled session')
      expect(row.textContent).not.toContain(UNTITLED)
      expect(row.textContent).not.toContain(KEY_TITLED)
    }
    expect(rows[2].textContent).toContain('Named work')

    // Resume still targets the key; the payload title is unchanged.
    fireEvent.keyDown(rows[0], { key: 'Enter' })
    await waitFor(() => expect(resumeMock).toHaveBeenCalledWith(UNTITLED, UNTITLED))
  })
})
