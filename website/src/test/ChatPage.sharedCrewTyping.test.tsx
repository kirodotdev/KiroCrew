/**
 * Shared Crew typing presence: a keystroke in the main composer fires the
 * content-free typing signal the collab strip's presence reporter listens to,
 * and opening a session does not.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: true, has_more: false, total: 1 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    sharedCrewCapabilities: vi.fn(),
    sharedCrewViewer: vi.fn(),
    sharedCrewPresence: vi.fn(),
    sharedCrewDecisions: vi.fn(),
    sharedCrewSendState: vi.fn(),
    sharedCrewTypingLease: vi.fn().mockResolvedValue(null),
    sharedCrewMode: vi.fn().mockResolvedValue(null),
    sharedCrewPeople: vi.fn().mockResolvedValue(null),
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
import { ALL_UNAVAILABLE } from '../api/sharedCrewUi'
import { SessionCollabStrip } from '../pages/chat/shared-crew/SessionCollabStrip'
import { composerTypingSignal } from '../pages/chat/shared-crew/typingSignal'

type SlotState = 'idle' | 'streaming' | 'tool_running' | 'stopping' | 'compacting'

function makeStore(opts: { slotRunning: boolean; slotStopping: boolean; slotState: SlotState }) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', messages: 1, running: opts.slotRunning, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: opts.slotRunning, slotStopping: opts.slotStopping, slotState: opts.slotState,
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

async function renderWithState(opts: { slotRunning: boolean; slotStopping: boolean; slotState: SlotState }) {
  const store = makeStore(opts)
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
  return store
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE })
  vi.mocked(api.sharedCrewViewer).mockResolvedValue(null)
  vi.mocked(api.sharedCrewPresence).mockResolvedValue({ schema_version: 1, entries: [] })
  vi.mocked(api.sharedCrewDecisions).mockResolvedValue({ schema_version: 1, items: [] })
  vi.mocked(api.sharedCrewSendState).mockResolvedValue({ schema_version: 1, session_key: 'slot-a', revision: 0, last: null })
})

describe('ChatPage: Shared Crew typing signal', { timeout: 15_000 }, () => {
  it('signals typing on the user\'s own keystrokes, carrying no text', async () => {
    const heard = vi.fn()
    const off = composerTypingSignal.subscribe((...args: unknown[]) => heard(...args))
    try {
      await renderWithState({ slotRunning: false, slotStopping: false, slotState: 'idle' })
      heard.mockClear()
      const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
      fireEvent.change(input, { target: { value: 'h' } })
      fireEvent.change(input, { target: { value: 'hi' } })
      // Wait for the edit's own follow-up render to land the value before asserting.
      await waitFor(() => expect(input.value).toBe('hi'))
      await waitFor(() => expect(heard).toHaveBeenCalledTimes(2))
      // The only thing a listener hears is whether the draft is now empty.
      for (const call of heard.mock.calls) expect(call).toEqual([false])
    } finally {
      off()
    }
  })

  it('does not signal typing when the page opens a session', async () => {
    const heard = vi.fn()
    const off = composerTypingSignal.subscribe(heard)
    try {
      await renderWithState({ slotRunning: false, slotStopping: false, slotState: 'idle' })
      expect(heard).not.toHaveBeenCalled()
    } finally {
      off()
    }
  })
})

describe('Shared Crew collaboration read errors', () => {
  it.each([
    'sharedCrewCapabilities', 'sharedCrewViewer', 'sharedCrewPresence', 'sharedCrewDecisions', 'sharedCrewSendState',
  ] as const)('shows %s failures with agent recovery', async which => {
    vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, presence: 'available', attribution: 'available' })
    vi.mocked(api[which]).mockRejectedValue(new Error(`${which} read failed`))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><SessionCollabStrip sessionKey="slot-a" /></QueryClientProvider>)
    expect(await screen.findByTestId('session-collab-error')).toHaveTextContent(`${which} read failed`)
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
  })

  it('renders no collaboration strip for a healthy personal Crew', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { container } = render(<QueryClientProvider client={qc}><SessionCollabStrip sessionKey="slot-a" /></QueryClientProvider>)
    await waitFor(() => {
      expect(qc.getQueryState(['shared-crew', 'capabilities'])?.status).toBe('success')
      expect(qc.getQueryState(['shared-crew', 'send-state', 'slot-a'])?.status).toBe('success')
    })
    expect(container).toBeEmptyDOMElement()
  })
})
