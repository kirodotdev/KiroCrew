/**
 * A split pane on another harness than the configured one lists that harness's models, from a list
 * with no last-good copy. When that list fails to load, the pane's picker says so instead of
 * offering only Auto as if Auto were the whole list.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, fireEvent, waitFor, within } from '@testing-library/react'
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
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatSlotSelectionCapabilities: vi.fn().mockResolvedValue({ known: false, models_backend: 'claude' }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    dashboardConfig: vi.fn().mockResolvedValue({}),
    models: vi.fn((backend?: string) => (backend === 'claude'
      ? Promise.reject(new Error('Service Unavailable'))
      : Promise.resolve(backend === 'codex'
        ? [{ model_name: 'auto' }, { model_name: 'openai.gpt-6.1-sol' }]
        : [{ model_name: 'auto' }, { model_name: 'claude-fable-5.1' }]))),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

/** The pane's list arrives through a chain: selection-capabilities names the session's harness, then
 *  that harness's model list loads and fails. */
const HARNESS_CHAIN = { timeout: 5000 }

function renderPane(slotKey: string, model = 'claude-opus-5-5') {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', agent: 'helper', model, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('ChatPane model picker — the session\'s own list', () => {
  it('says so when the list of the harness the pane runs on fails to load', async () => {
    renderPane('member-helper')
    const chip = await waitFor(() => screen.getByTitle(/^Model: claude-opus-5-5(?: ·|$)/), HARNESS_CHAIN)

    await act(async () => { fireEvent.click(chip) })
    const menu = await waitFor(() => screen.getByRole('dialog', { name: 'Model list' }))

    expect(await within(menu).findByRole('alert', {}, HARNESS_CHAIN)).toHaveTextContent('Couldn\'t load Claude Code\'s models, so only “auto” is offered.')
  })
})

describe('ChatPane effort control — a crewmate thread before its first turn', () => {
  const caps = vi.mocked(api.chatSlotSelectionCapabilities)
  afterEach(() => { caps.mockResolvedValue({ known: false, models_backend: 'claude' } as never) })

  it('keeps it where no session on the thread\'s harness has answered yet, judging the model', async () => {
    // Crew threads are never eager-spawned, so a codex one is cold, and unknown is not "takes none".
    caps.mockResolvedValue({ known: false, models_backend: 'codex', effort_supported: null, effort_levels: [] } as never)
    renderPane('member-helper', 'openai.gpt-6.1-sol')
    const chip = await waitFor(() => screen.getByTitle(/^Model: openai\.gpt-6\.1-sol(?: ·|$)/), HARNESS_CHAIN)

    await act(async () => { fireEvent.click(chip) })
    const menu = await waitFor(() => screen.getByRole('dialog', { name: 'Model list' }))

    expect(await within(menu).findByRole('slider', { name: 'Reasoning effort' }, HARNESS_CHAIN)).toBeInTheDocument()
  })
})
