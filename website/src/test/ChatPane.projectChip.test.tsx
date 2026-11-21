import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* A split-view pane's composer draws the same project chip as the main chat:
 * the folder name, the branch with its copy button, and the picker the chip
 * opens, whose pick switches THIS pane's slot. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../components/ProjectPicker', () => ({
  default: ({ open, onSelect, startPath }: { open: boolean; onSelect: (path: string) => void; startPath?: string }) => (open ? (
    <div data-testid="pane-project-picker" data-start={startPath}>
      <button type="button" onClick={() => onSelect('/other')}>pick other</button>
    </div>
  ) : null),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    projectGit: vi.fn().mockResolvedValue({ repo: true, branch: 'feat/pane-chip', detached: false }),
    chatSlotProject: vi.fn().mockResolvedValue({ project: '/other' }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Kiro Crew', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'
import { slotRowByKey } from '../store/slotIndex'

const SLOT = 'chat-1-project'

function renderPane() {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [{ key: SLOT, project: '/repo', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}><QueryClientProvider client={qc}><ThemeProvider><MemoryRouter>
      <ChatPane slotKey={SLOT} />
    </MemoryRouter></ThemeProvider></QueryClientProvider></Provider>,
  )
  return store
}

beforeEach(() => { localStorage.clear(); vi.mocked(api.chatSlotProject).mockClear() })

describe('ChatPane project chip', () => {
  it("shows the pane's folder and branch", async () => {
    renderPane()
    const chip = await screen.findByTestId('composer-project-chip')
    expect(chip).toHaveTextContent('repo')
    expect(await screen.findByText('feat/pane-chip')).toBeInTheDocument()
    expect(api.projectGit).toHaveBeenCalledWith('/repo')
  })

  it('names a failed branch read in the pane and keeps the folder chip', async () => {
    vi.mocked(api.projectGit).mockRejectedValueOnce(new Error('Unknown project directory'))
    renderPane()
    expect(await screen.findByTestId('chat-pane-project-git-error')).toHaveTextContent('Unknown project directory')
    expect(screen.getByTestId('composer-project-chip')).toHaveTextContent('repo')
    expect(screen.queryByText('feat/pane-chip')).toBeNull()
  })

  it("opens the picker and switches the pane's own slot", async () => {
    const store = renderPane()
    const chip = await screen.findByTestId('composer-project-chip')
    expect(screen.queryByTestId('pane-project-picker')).toBeNull()
    await act(async () => { fireEvent.click(chip) })
    const picker = await screen.findByTestId('pane-project-picker')
    expect(picker).toHaveAttribute('data-start', '/repo')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'pick other' })) })
    await waitFor(() => expect(api.chatSlotProject).toHaveBeenCalledWith(SLOT, '/other'))
    await waitFor(() => expect(slotRowByKey(store.getState().dashboard.slots, SLOT)?.project).toBe('/other'))
    expect(screen.queryByTestId('pane-project-picker')).toBeNull()
  })
})
