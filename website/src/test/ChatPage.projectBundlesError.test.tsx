/**
 * A bundle-attached session whose Project list cannot be read must surface
 * that read failure through the page's pane-level ErrorNotice (hand-off on),
 * not hide it behind the chip's generic label (`errors-use-error-notice`).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
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
      { model_name: 'claude-sonnet-5', description: 'Claude Sonnet 5' },
    ]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({ model: 'claude-opus-5' }),
    agentResolvedModel: vi.fn().mockResolvedValue({ model: 'claude-opus-5' }),
    // The switch endpoints answer with the STORED value (deprecated ids are
    // remapped, project paths realpath-normalized server-side).
    chatSlotModel: vi.fn().mockResolvedValue({ ok: true, model: 'claude-sonnet-5' }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true, project: '/home/user/proj-x' }),
    // The agent switch also names the re-resolved workspace binding.
    chatSlotAgent: vi.fn().mockResolvedValue({ ok: true, agent: 'researcher', workspace: 'research-ws' }),
    recentProjects: vi.fn().mockResolvedValue({ dirs: ['/home/user/proj-x'] }),
    browseDirs: vi.fn().mockResolvedValue({ path: '/home/user', parent: '/home', dirs: [] }),
    projectGit: vi.fn().mockRejectedValue(new Error('not a repo')),
    projectBundles: vi.fn().mockResolvedValue({ projects: [] }),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'kirocrew' }, { name: 'researcher' }], defaultAgent: 'kirocrew' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
// No websocket: no `slots` frame can arrive, so the chips can only move if
// the switch callbacks write the store themselves — the property under test.
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
        status: null,
        slots: [{ key: 'slot-a', messages: 0, running: false, mode: '', agent: 'kirocrew', model: 'claude-opus-5', project: '/home/user/old-proj', project_id: 'project-payments', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
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

async function renderChat() {
  const store = makeStore()
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
  vi.clearAllMocks()
})

describe('ChatPage — Project list read failure on a bundle-attached session', { timeout: 15_000 }, () => {
  it('surfaces a failed Project list read through the pane-level ErrorNotice with the hand-off on', async () => {
    vi.mocked(api.projectBundles).mockRejectedValue(new Error('registry unreadable'))
    await renderChat()

    const notice = await waitFor(() => screen.getByTestId('project-bundles-error'))
    expect(notice.textContent).toContain('Projects could not be loaded')
    expect(notice.querySelector('button')).toBeTruthy() // the hand-off
  })

  it('renders no notice when the Project list reads fine', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({
      projects: [{ id: 'project-payments', name: 'Payments Platform', health: { status: 'healthy' } }],
    } as never)
    await renderChat()

    await waitFor(() => expect(vi.mocked(api.projectBundles)).toHaveBeenCalled())
    expect(screen.queryByTestId('project-bundles-error')).toBeNull()
  })
})
