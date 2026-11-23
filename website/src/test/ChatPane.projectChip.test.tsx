import { afterEach, describe, it, expect, vi } from 'vitest'
import type { ReactNode } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* A Split View pane's composer draws the chips the main chat's
 * composer draws for the same session: the project chip (the folder and the
 * picker it opens, switching THIS pane's session; no branch, which would put a
 * third button in the chip group) and, on a conductor mode chat or a crew
 * member, the conductor mode bolt in the agent spot. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const projectGit = vi.hoisted(() => vi.fn().mockResolvedValue({ path: '/work/demo', repo: true, branch: 'feat/pane-chips' }))
const chatSlotProject = vi.hoisted(() => vi.fn(async (_slot: string, path: string) => ({ project: `/real${path}` })))
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
    crewWorkers: vi.fn().mockResolvedValue({ root: 'root-1', workers: [] }),
    projectGit,
    projectAwsProfile: vi.fn().mockResolvedValue({ path: '/work/demo', profile: null, boundDir: null, profiles: [], status: null }),
    chatSlotProject,
  },
  SEARCH_MIN_CHARS: 2,
}))
// The picker's own listing is not under test: a stub shows that the chip opens
// it on this pane's folder and hands its pick to this pane's switch.
vi.mock('../components/ProjectPicker', () => ({
  default: ({ open, onSelect, startPath }: { open: boolean; onSelect: (path: string) => void; startPath?: string }) => (
    open ? <div data-testid="project-picker" data-start={startPath}><button onClick={() => onSelect('/work/other')}>pick other</button></div> : null
  ),
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
import { resetCrewModeCatalog, setCrewModeCatalog } from '../lib/crewMode'

const row = (key: string, extra: Record<string, unknown> = {}) => ({
  key, messages: 2, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...extra,
})

// The main chat holds another session, so a chip wired to the active slot
// instead of the pane's own would show and switch the wrong one.
function renderPane(paneKey: string, slots: ReturnType<typeof row>[]) {
  const base = chatReducer(undefined, { type: '@@init' })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      chat: { ...base, activeSlot: 'main', messages: [] } as RootState['chat'],
      dashboard: {
        status: null, connected: true, slots: [row('main', { title: 'Main', project: '/work/main' }), ...slots], slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}><QueryClientProvider client={qc}><ThemeProvider><MemoryRouter>
      <ChatPane slotKey={paneKey} />
    </MemoryRouter></ThemeProvider></QueryClientProvider></Provider>,
  )
  return store
}

describe('ChatPane composer chips in Split View', () => {
  afterEach(() => resetCrewModeCatalog())

  it("draws the project chip without the branch, and a pick there switches the pane's own session", async () => {
    const store = renderPane('p1', [row('p1', { title: 'Pane', project: '/work/demo' })])
    const chip = await screen.findByTestId('composer-project-chip')
    expect(chip).toHaveTextContent(/^demo$/)
    // The mock has a branch to give, so its absence is the pane never asking.
    expect(projectGit).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /Copy branch/ })).toBeNull()
    expect(chip.getAttribute('aria-label')).not.toMatch(/feat\/pane-chips/)
    expect(screen.queryByTestId('crew-mode-composer-chip')).toBeNull()
    fireEvent.click(chip)
    expect(screen.getByTestId('project-picker')).toHaveAttribute('data-start', '/work/demo')
    fireEvent.click(screen.getByRole('button', { name: 'pick other' }))
    await waitFor(() => expect(chatSlotProject).toHaveBeenCalledWith('p1', '/work/other'))
    await waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === 'p1')?.project).toBe('/real/work/other'))
    expect(screen.queryByTestId('project-picker')).toBeNull()
    expect(store.getState().dashboard.slots.find(s => s.key === 'main')?.project).toBe('/work/main')
  })

  it('draws the conductor mode bolt in the agent spot of a conductor mode chat', async () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])
    renderPane('c1', [row('c1', { title: 'Conductor', agent: 'kirocrew-conductor', agent_kind: 'template', crew_mode_root: true })])
    expect(await screen.findByTestId('crew-mode-composer-chip')).toHaveAttribute('aria-label', 'This chat runs in conductor mode')
  })

  it("names the role on a crew member's bolt", async () => {
    renderPane('w1', [row('root-1', { title: 'Sample goal', crew_mode_root: true }), row('w1', { title: 'Worker B', crew_root: 'root-1', created_by: 'root-1' })])
    expect(await screen.findByTestId('crew-mode-composer-chip')).toHaveAttribute('aria-label', 'Conductor mode · Worker')
  })
})
