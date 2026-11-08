import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, waitFor, within } from '@testing-library/react'
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
import { crewModePreviewOn } from './crewModePreview'
import { setCrewModeCatalog } from '../lib/crewMode'
import { PREVIEW_CREW_MODE } from '../utils/previewFlags'

const permission = { role: 'permission', content: 'Shell', cls: '', meta: { approval_id: 'rid-1', tool_input: 'printf hi', request_id: 'rid-1' } }
const row = (key: string, extra: Record<string, unknown> = {}) => ({
  key, messages: 2, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...extra,
})

function renderPane(paneKey: string, slots: ReturnType<typeof row>[], messages: unknown[]) {
  const base = chatReducer(undefined, { type: '@@init' })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      chat: { ...base, activeSlot: paneKey, messages } as RootState['chat'],
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

describe('ChatPane in Split View with the Crew Mode preview off', () => {
  // The gateway still runs Crew Mode; the device has the preview off.
  beforeEach(() => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])
    localStorage.removeItem(PREVIEW_CREW_MODE)
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

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
