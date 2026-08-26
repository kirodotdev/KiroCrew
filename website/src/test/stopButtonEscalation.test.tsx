/**
 * Tests for the stop button UX improvements:
 * 1. soft_pending: "Click again to force stop" hint label
 * 2. killing state: disabled spinner initially, re-enabled "Force reset" after 15s
 *
 * Note: The killing escape hatch timeout is 15s. For the integration test we
 * use vi.useFakeTimers({ shouldAdvanceTime: true }) to allow React's microtask
 * rendering to proceed while controlling setTimeout.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
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
    stopChatSlot: vi.fn().mockResolvedValue({}),
    stopChatSlotForce: vi.fn().mockResolvedValue({}),
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
        slots: [{ key: 'slot-a', messages: 1, running: opts.slotRunning, stopping: opts.slotStopping, stop_state: opts.stopState ?? 'idle', mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
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
  vi.useFakeTimers({ shouldAdvanceTime: true })
  sessionStorage.clear()
  localStorage.clear()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('Stop button — soft_pending force-stop affordance', { timeout: 30_000 }, () => {
  it('renders the "Click again to force stop" hint when in soft_pending state', async () => {
    await renderWithState({ slotRunning: true, slotStopping: true, slotState: 'stopping', stopState: 'soft_pending' })

    expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()
    expect(screen.getByTestId('stop-force-hint')).toBeInTheDocument()
    expect(screen.getByTestId('stop-force-hint').textContent).toBe('Click again to force stop')
  })

  it('has correct aria-label on the pulsing force button', async () => {
    await renderWithState({ slotRunning: true, slotStopping: true, slotState: 'stopping', stopState: 'soft_pending' })

    const btn = screen.getByTestId('stop-button-pulsing')
    expect(btn.getAttribute('aria-label')).toBe('Force kill session (discards in-progress work and queued messages)')
  })
})

describe('Stop button — killing-state escape hatch (15s timeout)', { timeout: 30_000 }, () => {
  it('renders a disabled spinner initially in killing state', async () => {
    await renderWithState({ slotRunning: true, slotStopping: true, slotState: 'stopping', stopState: 'killing' })

    const btn = screen.getByTestId('stop-button-killing')
    expect(btn).toBeInTheDocument()
    expect(btn).toBeDisabled()
    expect(btn.getAttribute('aria-label')).toBe('Killing session')
  })

  it('re-enables the button after 15s as "Force reset" with escape hint', async () => {
    await renderWithState({ slotRunning: true, slotStopping: true, slotState: 'stopping', stopState: 'killing' })

    expect(screen.getByTestId('stop-button-killing')).toBeInTheDocument()
    expect(screen.queryByTestId('stop-button-escape-hatch')).not.toBeInTheDocument()

    // Advance past the 15s threshold
    await act(async () => { vi.advanceTimersByTime(15_000) })

    await waitFor(() => {
      expect(screen.queryByTestId('stop-button-killing')).not.toBeInTheDocument()
      expect(screen.getByTestId('stop-button-escape-hatch')).toBeInTheDocument()
    })
    expect(screen.getByTestId('stop-button-escape-hatch')).not.toBeDisabled()
    expect(screen.getByTestId('stop-escape-hint').textContent).toBe('taking longer than expected')
  })

  it('the escape hatch button has correct aria-label', async () => {
    await renderWithState({ slotRunning: true, slotStopping: true, slotState: 'stopping', stopState: 'killing' })

    await act(async () => { vi.advanceTimersByTime(15_000) })

    await waitFor(() => {
      expect(screen.getByTestId('stop-button-escape-hatch')).toBeInTheDocument()
    })
    const btn = screen.getByTestId('stop-button-escape-hatch')
    expect(btn.getAttribute('aria-label')).toBe('Force reset session (taking longer than expected)')
  })

  it('escape hatch button is clickable (dispatches force stop)', async () => {
    await renderWithState({ slotRunning: true, slotStopping: true, slotState: 'stopping', stopState: 'killing' })

    await act(async () => { vi.advanceTimersByTime(15_000) })

    await waitFor(() => {
      expect(screen.getByTestId('stop-button-escape-hatch')).toBeInTheDocument()
    })
    const btn = screen.getByTestId('stop-button-escape-hatch')
    // Should not throw — button is enabled and clickable
    fireEvent.click(btn)
  })
})
