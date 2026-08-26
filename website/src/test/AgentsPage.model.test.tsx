/**
 * Agent Templates must survive a foreign-shaped `model` on any single agent.
 *
 * `~/.kiro/agents` is a SHARED directory: other tools (ACP adapters, IDE
 * plugins) write their own specs there, and not all of them spell `model` as a
 * plain string. An ACP-style `{ "id": "anthropic:claude-opus-4-8" }` rendered
 * as a JSX child throws React error #31 ("Objects are not valid as a React
 * child"), which put the WHOLE tab into the error boundary — every other
 * agent's row, the context panel and the subagents table included.
 *
 * The backend now coerces `model` on both `/api/agents/installed` and
 * `/api/agents/detail/{name}`, but `agentDetail` is otherwise a pass-through of
 * a user-editable spec, so the page keeps its own guard. These tests drive the
 * page with the raw bad shape to prove the guard, not the coercion.
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({
  spawnList: vi.fn(),
  sessionsContext: vi.fn(),
  sessionsUsage: vi.fn(),
  agentsInstalled: vi.fn(),
  mcpProbeCache: vi.fn(),
  defaultAgent: vi.fn(),
  agentDetail: vi.fn(),
  skills: vi.fn(),
  agentPatch: vi.fn(),
  spawnClear: vi.fn(),
  spawnDelete: vi.fn(),
  setDefaultAgent: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../store', () => ({ useAppSelector: (fn: (s: unknown) => unknown) => fn({ dashboard: { status: { sessions: 0, subagents: 0 }, refreshTrigger: 0 } }) }))
vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    capabilities: { agentTemplates: true },
    labels: { sessionProcess: 'ACP subprocess', configFile: 'agent.json' },
    fetchAvailableModels: () => Promise.resolve([{ name: 'claude-opus-4.8', description: '' }]),
    getContextWindow: () => 200_000,
  }),
}))

import AgentsPage from '../pages/AgentsPage'

/** The exact shape observed in the wild (`quickwork_acp_kiro.json`). */
const ACP_MODEL = { id: 'anthropic:claude-opus-4-8' }

const mkAgent = (name: string, model: unknown) => ({
  name,
  description: `${name} agent`,
  source: 'builtin',
  model,
  skills: [],
  mcp_servers: [],
  filename: `${name}.json`,
})

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AgentsPage embedded />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  Object.values(mockApi).forEach(fn => fn.mockReset())
  mockApi.spawnList.mockResolvedValue({ agents: [] })
  mockApi.sessionsContext.mockResolvedValue({ sessions: [] })
  mockApi.sessionsUsage.mockResolvedValue({ usage: null })
  mockApi.mcpProbeCache.mockResolvedValue([])
  mockApi.skills.mockResolvedValue([])
  mockApi.defaultAgent.mockResolvedValue({ default_agent: '' })
  mockApi.setDefaultAgent.mockResolvedValue({ ok: true, default_agent: '' })
})

describe('AgentsPage model rendering', () => {
  it('renders the list when one agent has an object model, keeping its siblings', async () => {
    mockApi.agentsInstalled.mockResolvedValue([
      mkAgent('kirocrew', 'claude-opus-4.8'),
      mkAgent('quickwork_acp_kiro', ACP_MODEL),
    ])
    mockApi.agentDetail.mockResolvedValue({ ...mkAgent('kirocrew', 'claude-opus-4.8'), unmanaged_skills: [] })

    renderPage()

    // The bad row is present and shows the "no pin" label rather than crashing.
    expect(await screen.findByText('quickwork_acp_kiro')).toBeInTheDocument()
    // The well-formed sibling is the real regression risk: pre-fix it vanished
    // with the rest of the tab, because one bad child unmounts the whole tree.
    // getAllByText, not getByText — the auto-opened detail panel repeats the
    // selected agent's name and model alongside its list row.
    expect(screen.getAllByText('kirocrew').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('claude-opus-4.8').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('auto').length).toBeGreaterThanOrEqual(1)
  })

  it('renders the detail model chip when the selected agent has an object model', async () => {
    mockApi.agentsInstalled.mockResolvedValue([mkAgent('quickwork_acp_kiro', ACP_MODEL)])
    // agentDetail passes the spec through, so the detail panel sees the raw shape.
    mockApi.agentDetail.mockResolvedValue({
      ...mkAgent('quickwork_acp_kiro', ACP_MODEL),
      skills: [],
      unmanaged_skills: [],
    })

    renderPage()

    fireEvent.click(await screen.findByText('quickwork_acp_kiro'))
    // Both the row label and the detail chip degrade to "auto".
    await waitFor(() => expect(screen.getAllByText('auto').length).toBeGreaterThanOrEqual(2))
  })

  it('shows auto for a null model instead of a blank', async () => {
    mockApi.agentsInstalled.mockResolvedValue([mkAgent('nullish', null)])
    mockApi.agentDetail.mockResolvedValue({ ...mkAgent('nullish', null), unmanaged_skills: [] })

    renderPage()

    expect(await screen.findByText('nullish')).toBeInTheDocument()
    expect(screen.getAllByText('auto').length).toBeGreaterThanOrEqual(1)
  })

  it('renders the detail panel when the selected agent has an object description', async () => {
    // `description` is rendered as a JSX child too, and an object is truthy — so
    // a bare `&&` guard let it through and crashed the tab the same way `model`
    // did. This is the class, not a second special case.
    const bad = { ...mkAgent('weird-desc', 'claude-opus-4.8'), description: { text: 'hi' } }
    mockApi.agentsInstalled.mockResolvedValue([bad])
    mockApi.agentDetail.mockResolvedValue({ ...bad, skills: [], unmanaged_skills: [] })

    renderPage()

    fireEvent.click(await screen.findByText('weird-desc'))
    // The row and the detail panel both survive; the unusable description is
    // simply not shown rather than taking the tree down.
    await waitFor(() => expect(screen.getAllByText('claude-opus-4.8').length).toBeGreaterThanOrEqual(1))
    expect(screen.queryByText('[object Object]')).not.toBeInTheDocument()
  })
})
