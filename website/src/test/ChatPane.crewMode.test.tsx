import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import type { ReactNode } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* Edge review R3 G3 / Z1: a Split View pane follows the one Crew Mode approval
 * rule. A pane holding a WORKER draws no Allow / Trust / Reject band (the
 * decision is the conductor card's) and says where to decide it; a pane holding
 * the CONDUCTOR keeps its workers' approval cards. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const chatSlotAgent = vi.hoisted(() => vi.fn(async (_slot: string, agent: string) => ({ agent, agent_kind: 'template', workspace: 'default' })))
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
    chatSlotAgent,
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Kiro Crew', avatar: '' }) }))
// `choices`: the catalog rows, through the same conductor filter the real hook applies.
const agentsState = vi.hoisted(() => ({ defaultAgent: 'default', choices: undefined as Array<{ name: string; selection_kind: string }> | undefined }))
vi.mock('../hooks/useAgents', async () => {
  const { withoutConductors } = await import('../lib/crewMode')
  return { useAgents: () => ({ agents: [], defaultAgent: agentsState.defaultAgent, ...(agentsState.choices ? { choices: withoutConductors(agentsState.choices) } : {}) }) }
})
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { setCrewModeCatalog } from '../lib/crewMode'
import { turnOffCrewModeAsLts } from './crewModeOff'
import { __resetPanelTabs, openPanelView, panelStripOf } from '../hooks/usePanelTabs'

const permission = { role: 'permission', content: 'Shell', cls: '', meta: { approval_id: 'rid-1', tool_input: 'printf hi', request_id: 'rid-1' } }
const row = (key: string, extra: Record<string, unknown> = {}) => ({
  key, messages: 2, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...extra,
})

function renderPane(paneKey: string, slots: ReturnType<typeof row>[], messages: unknown[], slotLoading = false, chat: Partial<RootState['chat']> = {}) {
  const base = chatReducer(undefined, { type: '@@init' })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      chat: { ...base, activeSlot: paneKey, messages, slotLoading, ...chat } as RootState['chat'],
      dashboard: {
        status: null, connected: true, slots, slotsLoaded: true,
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
}

const pendingInfo = { pending_approval: true, pending_approval_info: { origin: 'native', request_id: 'rid-1', request_mid: 'm1', tool: 'Shell', tool_input: 'printf hi' } }

describe('ChatPane in Split View (Crew Mode)', () => {
  it('G3: a worker pane draws no approval band and says where to decide it', async () => {
    renderPane('w1', [row('root-1', { title: 'Sample goal', crew_mode_root: true }), row('w1', { title: 'Worker B', crew_root: 'root-1', created_by: 'root-1', ...pendingInfo })], [{ role: 'user', content: 'go', cls: '' }, permission])
    await waitFor(() => expect(screen.getByTestId('crew-worker-approval-waiting')).toHaveTextContent('Waiting for approval in Sample goal'))
    expect(screen.queryByText('Allow once')).toBeNull()
    expect(screen.queryByText('Trust all tools for this session')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Reject/ })).toBeNull()
  })

  it('G3: a plain chat pane with the same approval keeps its controls', async () => {
    renderPane('plain', [row('plain', { title: 'Plain', ...pendingInfo })], [{ role: 'user', content: 'go', cls: '' }, permission])
    expect(await screen.findByText('Allow once')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-worker-approval-waiting')).toBeNull()
  })

  it('G3: a worker pane under a closed conductor does not send', async () => {
    renderPane('w1', [row('w1', { title: 'Worker B', crew_root: 'root-1', created_by: 'root-1' })], [{ role: 'user', content: 'go', cls: '' }])
    expect(await screen.findByTestId('crew-worker-root-closed')).toHaveTextContent('Main chat is closed')
    const box = screen.getAllByRole('textbox')[0]
    expect(box).toHaveAttribute('aria-disabled', 'true')
  })

  it("Z1: a conductor pane keeps its workers' approval cards", async () => {
    renderPane('root-1', [
      row('root-1', { title: 'Sample goal', crew_mode_root: true }),
      row('w1', { title: 'Worker B', crew_root: 'root-1', created_by: 'root-1', ...pendingInfo }),
    ], [{ role: 'user', content: 'goal', cls: '' }])
    const cards = await screen.findByTestId('crew-approval-cards')
    expect(cards).toHaveTextContent('Worker B')
    expect(within(cards).getByRole('button', { name: /Approve/ })).toBeInTheDocument()
    expect(within(cards).getByRole('button', { name: /Reject/ })).toBeInTheDocument()
  })
})

describe('ChatPane in Split View with the conductor itself as the default agent', () => {
  beforeEach(() => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])
    agentsState.defaultAgent = 'kirocrew-conductor'
    agentsState.choices = [
      { name: 'kirocrew-conductor', selection_kind: 'template' },
      { name: 'other-template', selection_kind: 'template' },
      { name: 'helper-bot', selection_kind: 'member' },
    ]
  })
  afterEach(() => { setCrewModeCatalog(undefined, undefined); agentsState.defaultAgent = 'default'; agentsState.choices = undefined })
  const conductor = (extra: Record<string, unknown> = {}) => row('c1', { title: 'Conductor', agent: 'kirocrew-conductor', agent_kind: 'template', ...extra })
  const pickerRows = async () => {
    fireEvent.click(await screen.findByRole('button', { name: /^Agent: kirocrew-conductor/ }))
    const list = await screen.findByRole('listbox', { name: 'Agent list' })
    return within(list).getAllByRole('option').map(o => o.textContent ?? '')
  }

  it('makes a new root pane its agent label a picker of the other templates only', async () => {
    // A namespace-blind row: the pick still goes out as a template.
    agentsState.choices = agentsState.choices!.map(a => (a.name === 'other-template' ? { name: a.name } as typeof a : a))
    __resetPanelTabs(); chatSlotAgent.mockClear()
    openPanelView('c1', 'workers'); openPanelView('c1', 'command-center'); openPanelView('c1', 'changes')
    renderPane('c1', [conductor({ crew_mode_root: true, messages: 0 })], [])
    const options = await pickerRows()
    expect(options.some(t => t.includes('other-template'))).toBe(true)
    expect(options.some(t => t.includes('helper-bot') || t.includes('kirocrew-conductor'))).toBe(false)
    fireEvent.click(screen.getByRole('option', { name: /other-template/ }))
    await waitFor(() => expect(chatSlotAgent).toHaveBeenCalledWith('c1', 'other-template', 'template'))
    await waitFor(() => expect(panelStripOf('c1').ids).toEqual(['changes']))
  })

  it('offers no agent picker on a root pane once it has a message', async () => {
    // No root stamp, so the gateway lock's own clause cannot be what hides it.
    renderPane('c1', [conductor()], [{ role: 'user', content: 'go', cls: '' }])
    await waitFor(() => expect(screen.getAllByRole('textbox').length).toBeGreaterThan(0))
    expect(screen.queryByRole('button', { name: /^Agent: / })).toBeNull()
  })

  it('offers no agent picker on a root pane whose transcript is still loading', async () => {
    renderPane('c1', [conductor({ crew_mode_root: true, messages: 0 })], [], true)
    await waitFor(() => expect(screen.getAllByRole('textbox').length).toBeGreaterThan(0))
    expect(screen.queryByRole('button', { name: /^Agent: / })).toBeNull()
  })

  it('offers no agent picker on a new root pane while a turn runs', async () => {
    renderPane('c1', [conductor({ crew_mode_root: true, messages: 0, running: true })], [])
    await waitFor(() => expect(screen.getAllByRole('textbox').length).toBeGreaterThan(0))
    expect(screen.queryByRole('button', { name: /^Agent: / })).toBeNull()
  })

  it('offers no agent picker on a root pane whose window holds no user row but has older rows', async () => {
    // The gateway counts the older rows as a message and would refuse the pick.
    renderPane('c1', [conductor({ crew_mode_root: true })], [{ role: 'assistant', content: 'working', cls: '' }], false, { slotHasMore: true, slotCursorKey: 'c1' })
    await waitFor(() => expect(screen.getAllByRole('textbox').length).toBeGreaterThan(0))
    expect(screen.queryByRole('button', { name: /^Agent: / })).toBeNull()
  })
})

describe('ChatPane in Split View in a build without conductor mode (LTS)', () => {
  // The gateway still runs conductor mode; the dashboard offers none.
  beforeEach(() => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])
    turnOffCrewModeAsLts()
  })
  afterEach(() => setCrewModeCatalog(undefined, undefined))

  it('offers no agent picker on a Crew Mode root pane that has a message, since the gateway keeps it on its conductor', async () => {
    renderPane('c1', [row('c1', { title: 'Conductor', agent: 'kirocrew-conductor', agent_kind: 'template', crew_mode_root: true })], [{ role: 'user', content: 'go', cls: '' }])
    await waitFor(() => expect(screen.getAllByRole('textbox').length).toBeGreaterThan(0))
    expect(screen.queryByRole('button', { name: /^Agent: / })).toBeNull()
  })

  it('keeps the agent picker on a conductor pane with a message that is no Crew Mode root (Incognito)', async () => {
    renderPane('c1', [row('c1', { title: 'Conductor', agent: 'kirocrew-conductor', agent_kind: 'template', memory_mode: 'incognito' })], [{ role: 'user', content: 'go', cls: '' }])
    expect(await screen.findByRole('button', { name: /^Agent: kirocrew-conductor/ })).toBeInTheDocument()
  })

  it('still offers the agent picker on a conductor pane with no message yet', async () => {
    renderPane('c1', [row('c1', { title: 'Conductor', agent: 'kirocrew-conductor', agent_kind: 'template' })], [])
    expect(await screen.findByRole('button', { name: /^Agent: kirocrew-conductor/ })).toBeInTheDocument()
  })
})
