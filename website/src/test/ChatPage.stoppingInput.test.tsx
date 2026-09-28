/**
 * Regression test: keep ChatInput enabled while a turn is being
 * stopped so users can type and queue a follow-up the same way they can during
 * streaming or compaction.
 *
 * The ChatInput is NOT gated by `disabled={slotStopping}`: that would blank the
 * textarea (pointer-events-none + "Stopping…" placeholder) for the up-to-10s
 * soft-cancel window and lose quick "stop and redirect" steering. The backend
 * queues POST /api/chat while slot.running is true (it never rejects on
 * stop_state) and stop runs with preserve_queue=True, so keeping the input live
 * is safe.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode }) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
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

type SlotState = 'idle' | 'streaming' | 'tool_running' | 'stopping' | 'compacting'
type StopState = 'idle' | 'soft_pending' | 'killing'

function makeStore(opts: { slotRunning: boolean; slotStopping: boolean; slotState: SlotState; stopState?: StopState }) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        // stop_state lives on the dashboard slot (ChatPage reads currentSlot.stop_state).
        slots: [{ key: 'slot-a', messages: 1, running: opts.slotRunning, stop_state: opts.stopState ?? 'idle', mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as ReturnType<typeof dashboardReducer>,
      chat: {
        activeSlot: 'slot-a', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: opts.slotRunning, slotStopping: opts.slotStopping, slotState: opts.slotState,
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as ReturnType<typeof chatReducer>,
      notifications: { items: [] } as unknown as ReturnType<typeof notificationsReducer>,
    },
  })
}

async function renderWithState(opts: { slotRunning: boolean; slotStopping: boolean; slotState: SlotState; stopState?: StopState }) {
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
})

describe('ChatPage — input while stopping', { timeout: 15_000 }, () => {
  it('keeps the textarea interactive while slotStopping is true', async () => {
    await renderWithState({ slotRunning: true, slotStopping: true, slotState: 'stopping' })

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    // pointer-events-none is applied via the `disabled` class branch in ChatInput
    expect(input.className).not.toMatch(/pointer-events-none/)
    // "Stopping…" is the disabled placeholder; while stopping we want the
    // normal placeholder so the user knows they can still type.
    expect(input.placeholder).not.toBe('Stopping…')
  })

  it('accepts typing while stopping (messages can be queued by send())', async () => {
    await renderWithState({ slotRunning: true, slotStopping: true, slotState: 'stopping' })

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: 'queued while stopping' } })
    expect(input.value).toBe('queued while stopping')
  })

  it('keeps the textarea interactive during a soft_pending cooperative stop', async () => {
    await renderWithState({ slotRunning: true, slotStopping: true, slotState: 'stopping', stopState: 'soft_pending' })

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(input.className).not.toMatch(/pointer-events-none/)
    expect(input.placeholder).not.toBe('Stopping…')
    // The force-stop affordance must still be present — unblocking the input
    // must not remove the user's ability to escalate the stop.
    expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()
  })
})
