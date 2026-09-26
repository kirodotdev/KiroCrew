/**
 * Regression test: the composer's inherited effort follows a Settings change
 * without a reload.
 *
 * A slot with no effort override runs at Settings > Chat > Default reasoning
 * effort, and the model chip names that level. The chip used to read it from
 * its own query key, which nothing refreshed: queries never go stale here
 * (staleTime: Infinity), and the Settings save and the server's refresh
 * broadcast both update the shared ['kirocrewConfig'] entry instead. So the
 * chip kept the old level until a hard refresh.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, within } from '@testing-library/react'
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
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([
      { model_name: 'auto', description: 'Models chosen by task' },
      { model_name: 'claude-opus-5', description: 'Claude Opus 5' },
    ]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({ model: 'claude-opus-5' }),
    agentResolvedModel: vi.fn().mockResolvedValue({ model: 'claude-opus-5' }),
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { reasoning_effort: 'high' } }),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' }) }))
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

function makeStore(slotFields: Record<string, unknown> = {}) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        // No reasoning_effort by default: the slot inherits the configured default.
        slots: [{ key: 'slot-a', messages: 0, running: false, mode: '', agent: 'kirocrew', model: 'claude-opus-5', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...slotFields }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
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

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.mocked(api.kirocrewConfig).mockReset()
  vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { reasoning_effort: 'high' } })
})

describe('ChatPage — inherited default effort', { timeout: 15_000 }, () => {
  it('follows a Settings change to the default without a reload', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    await act(async () => {
      render(
        <QueryClientProvider client={qc}>
          <Provider store={makeStore()}>
            <ThemeProvider>
              <MemoryRouter><ChatPage /></MemoryRouter>
            </ThemeProvider>
          </Provider>
        </QueryClientProvider>,
      )
    })
    // The effort sits in its own control beside the model chip.
    const chip = await waitFor(() => screen.getByTitle(/^Reasoning effort: /))
    await waitFor(() => expect(chip.textContent).toContain('High'))

    // What the Settings save does on success: write the shared config entry.
    await act(async () => {
      qc.setQueryData(['kirocrewConfig'], { agent: { reasoning_effort: 'low' } })
    })
    await waitFor(() => expect(screen.getByTitle(/^Reasoning effort: /).textContent).toContain('Low'))
  })

  it('shows a config-read error while keeping the effort chip usable', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValueOnce(new Error('config store unavailable'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })

    await act(async () => {
      render(
        <QueryClientProvider client={qc}>
          <Provider store={makeStore()}>
            <ThemeProvider>
              <MemoryRouter><ChatPage /></MemoryRouter>
            </ThemeProvider>
          </Provider>
        </QueryClientProvider>,
      )
    })

    const notice = await screen.findByTestId('default-effort-config-error')
    expect(notice).toHaveTextContent(
      "Couldn't load your Settings, so the effort control next to the model picker shows Default. You can still send messages.",
    )
    expect(notice).not.toHaveTextContent('config store unavailable')
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTitle(/^Reasoning effort: /)).toHaveTextContent('Default'))
  })

  it('shows no config-read notice when the slot has its own effort', async () => {
    // The notice says the effort control shows Default. A slot pinned to Low
    // keeps showing Low whatever the Settings read did, so the notice would
    // describe a control that is not on screen.
    vi.mocked(api.kirocrewConfig).mockRejectedValueOnce(new Error('config store unavailable'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })

    await act(async () => {
      render(
        <QueryClientProvider client={qc}>
          <Provider store={makeStore({ reasoning_effort: 'low' })}>
            <ThemeProvider>
              <MemoryRouter><ChatPage /></MemoryRouter>
            </ThemeProvider>
          </Provider>
        </QueryClientProvider>,
      )
    })

    // Settle on the failed read first, so the absence is a verdict on it.
    await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.status).toBe('error'))
    await waitFor(() => expect(screen.getByTitle(/^Reasoning effort: /)).toHaveTextContent('Low'))
    expect(screen.queryByTestId('default-effort-config-error')).toBeNull()
  })
})
