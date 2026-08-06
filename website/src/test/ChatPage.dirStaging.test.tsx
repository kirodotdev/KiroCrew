import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
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
    createChatSlot: vi.fn().mockResolvedValue({ key: 'new-slot', title: 'new-slot', messages: 0, running: false }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
    fileSearch: vi.fn().mockResolvedValue({
      root: '/repo',
      results: [{ path: '/repo/src/widgets', name: 'widgets', size: 0, mtime: Math.floor(Date.now() / 1000) - 60, kind: 'dir' }],
    }),
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

function makeStore(activeSlot: string, slots: { key: string }[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slots: slots.map(s => ({ key: s.key, messages: 1, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
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
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

/** Type an @-token, wait for the picker's folder row, click it. Returns the textarea. */
async function stageFolder() {
  const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
  fireEvent.change(ta, { target: { value: '@wid' } })
  // 200ms debounce before the search fires; findByText waits it out.
  const row = await screen.findByText('widgets/', undefined, { timeout: 3000 })
  fireEvent.mouseDown(row)
  // Chip render is the staging signal (remove control carries the aria-label).
  await screen.findByLabelText('Remove folder')
  return ta
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
})

describe('ChatPage staged folder references', { timeout: 15_000 }, () => {
  it('clears staged folders on slot switch (no cross-slot leak)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderPage(store)

    await stageFolder()

    act(() => { store.dispatch(setActiveSlot('slot-b')) })

    // Folders are per-message only (no per-slot draft store), so the incoming
    // slot must not show the outgoing slot's chip.
    await waitFor(() => expect(screen.queryByLabelText('Remove folder')).not.toBeInTheDocument())
  })

  it('removing the folder chip also strips its @-token from the composer', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderPage(store)

    const ta = await stageFolder()
    expect(ta.value).toContain('@src/widgets/')

    fireEvent.click(screen.getByLabelText('Remove folder'))

    await waitFor(() => expect(screen.queryByLabelText('Remove folder')).not.toBeInTheDocument())
    // The remove control's promise: the agent no longer receives the folder.
    expect(ta.value).not.toContain('@src/widgets/')
  })

  it('token strip is exact: a longer sibling token survives the remove', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderPage(store)

    const ta = await stageFolder()
    // User keeps typing after the pick, including a hand-typed longer token
    // that shares the staged token as a prefix.
    fireEvent.change(ta, { target: { value: ta.value + 'and @src/widgets/sub/ please' } })

    fireEvent.click(screen.getByLabelText('Remove folder'))

    await waitFor(() => expect(ta.value).not.toMatch(/(^|\s)@src\/widgets\/(\s|$)/))
    expect(ta.value).toContain('@src/widgets/sub/')
  })

  it('hand-editing the token out of the composer drops the orphaned chip', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderPage(store)

    const ta = await stageFolder()
    expect(ta.value).toContain('@src/widgets/')

    // The composer token is the only payload the agent receives, so a chip
    // whose token was deleted by hand must not keep claiming the folder.
    fireEvent.change(ta, { target: { value: 'no folder here anymore' } })

    await waitFor(() => expect(screen.queryByLabelText('Remove folder')).not.toBeInTheDocument())
  })

  it('a longer token that merely shares the prefix does not keep the chip alive', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderPage(store)

    const ta = await stageFolder()

    // "@src/widgets/sub/" contains the staged "@src/widgets/" as a substring,
    // but the boundary-checked match must not treat it as the staged token.
    fireEvent.change(ta, { target: { value: 'look at @src/widgets/sub/ instead' } })

    await waitFor(() => expect(screen.queryByLabelText('Remove folder')).not.toBeInTheDocument())
  })
})
