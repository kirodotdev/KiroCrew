/**
 * ChatPage-level surface for the folder-suggestion card's option ORDER.
 *
 * The card lists the same tree the sidebar draws, in the sidebar's folder order
 * (`dashboard.folder_sort`, read through the shared settings query). What is only
 * reachable from the page is what happens when that read fails: the card is drawn
 * in the stored order -- a different list than the one the person chose -- and the
 * page adds NO notice of its own beside it: the sidebar in the same tree says the
 * failure once, over the tree it draws, and one screen names a failure once. The
 * card's own ordering is covered in FolderSuggestionCard.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, waitFor, within } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([
      { key: 'chat-1', messages: 1, running: false, mode: '', project: '/repo' },
    ]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    // Two folders whose stored positions and names disagree, so the drawn order
    // says which mode the card followed.
    chatFolders: vi.fn().mockResolvedValue([
      { id: 'work', name: 'Work', order: 0 },
      { id: 'home', name: 'Home', order: 1 },
    ]),
    tagColumns: vi.fn().mockResolvedValue([]),
    // The settings read behind the folder order. Each case sets it.
    kirocrewConfig: vi.fn(),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { api } from '../api/client'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: 'chat-1', messages: 1, running: false, mode: '', project: '/repo', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'chat-1', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
        followups: {},
        folderSuggestions: { 'chat-1': { folderId: 'home', folderName: 'Home', breadcrumb: 'Home', ts: 100, turns: 0 } },
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderPage(store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByTestId('folder-suggestion-card')).toBeTruthy())
}

const optionOrder = () =>
  Array.from((screen.getByTestId('folder-suggestion-select') as HTMLSelectElement).options).map(o => o.textContent)

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.clearAllMocks()
})

describe('ChatPage folder-suggestion card: the folder order behind its options', () => {
  it('lists the card in the sidebar order and says nothing while the read works', async () => {
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ dashboard: { folder_sort: 'name' } })
    await renderPage(makeStore())
    await waitFor(() => expect(optionOrder()).toEqual(['Home', 'Work']))
    expect(screen.queryByTestId('folder-order-unavailable')).toBeNull()
  })

  it('draws the card in the stored order when the read failed, and leaves the saying of it to the sidebar', async () => {
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('gateway restarting'))
    await renderPage(makeStore())
    // Stored positions are the fallback: Work (0) before Home (1).
    await waitFor(() => expect(optionOrder()).toEqual(['Work', 'Home']))
    // ONE notice per screen. The sidebar in this same tree says the failure over
    // the tree it draws; a second copy above the card, a few hundred pixels away,
    // would name the same failure twice for one reader.
    const notices = await screen.findAllByTestId('folder-order-unavailable')
    expect(notices).toHaveLength(1)
    expect(notices[0]).toHaveTextContent('gateway restarting')
    expect(screen.queryByTestId('folder-suggestion-order-unavailable')).toBeNull()
  })

  it('with the sidebar collapsed, says the failed read above the card itself -- the banner is not on this screen', async () => {
    // Desktop with the panel collapsed (mobile with the drawer closed, and embed
    // chat, take the same branch): the sidebar and its banner are unmounted, so
    // the card would otherwise draw the stored order with nothing on screen to
    // say why. No hand-off: the card's dropdown holds an unsaved pick.
    localStorage.setItem('mc-sidebar-pinned', 'false')
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('gateway restarting'))
    await renderPage(makeStore())
    await waitFor(() => expect(optionOrder()).toEqual(['Work', 'Home']))
    const notice = await screen.findByTestId('folder-suggestion-order-unavailable')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Folder order could not be read')
    expect(notice).toHaveTextContent('gateway restarting')
    // A picker's line with the pointer to the hand-off this notice has none of.
    expect(notice).toHaveTextContent('All folders are shown, in your Custom order; retries automatically. Open the chat sidebar to ask the agent about it.')
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    // Still one notice on the screen: the sidebar's is not mounted.
    expect(screen.queryByTestId('folder-order-unavailable')).toBeNull()
    const card = screen.getByTestId('folder-suggestion-card')
    expect(notice.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
