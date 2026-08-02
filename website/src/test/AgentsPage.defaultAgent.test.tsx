/**
 * The Agent Templates page's set-default control.
 *
 * The assertions below are about the LABEL as much as the wiring: they fail if
 * the button loses its text, keeping the control recognisable as a control
 * rather than a bare glyph.
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
  agentMetadata: vi.fn(),
  skills: vi.fn(),
  agentPatch: vi.fn(),
  spawnClear: vi.fn(),
  spawnDelete: vi.fn(),
  setDefaultAgent: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../store', () => ({ useAppSelector: (fn: (s: unknown) => unknown) => fn({ dashboard: { status: { sessions: 1, subagents: 0 }, refreshTrigger: 0 } }) }))
vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    capabilities: { agentTemplates: true },
    labels: { sessionProcess: 'ACP subprocess', configFile: 'agent.json' },
    fetchAvailableModels: () => Promise.resolve([{ name: 'claude-opus-4.8', description: '' }]),
  }),
}))

import AgentsPage from '../pages/AgentsPage'

const mkAgent = (name: string) => ({
  name,
  description: `${name} agent`,
  source: 'builtin',
  model: 'claude-opus-4.8',
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
  mockApi.agentsInstalled.mockResolvedValue([mkAgent('kirocrew'), mkAgent('fable')])
  mockApi.mcpProbeCache.mockResolvedValue([])
  mockApi.agentMetadata.mockResolvedValue({ content: '' })
  mockApi.skills.mockResolvedValue([])
  mockApi.agentDetail.mockResolvedValue({ ...mkAgent('kirocrew'), unmanaged_skills: [] })
  mockApi.setDefaultAgent.mockResolvedValue({ ok: true, default_agent: '' })
})

describe('AgentsPage default agent', () => {
  it('names the action in words on every non-default row', async () => {
    mockApi.defaultAgent.mockResolvedValue({ default_agent: '' })
    renderPage()
    const buttons = await screen.findAllByText('Set as default')
    expect(buttons).toHaveLength(2)
  })

  it('writes the default when the labelled control is clicked', async () => {
    mockApi.defaultAgent.mockResolvedValue({ default_agent: '' })
    renderPage()
    const buttons = await screen.findAllByText('Set as default')
    fireEvent.click(buttons[0])
    await waitFor(() => expect(mockApi.setDefaultAgent).toHaveBeenCalledWith('kirocrew'))
  })

  it('reads Default on the row that holds it, and clears it when clicked again', async () => {
    mockApi.defaultAgent.mockResolvedValue({ default_agent: 'kirocrew' })
    renderPage()
    // Exactly one row is the default, so the label appears once in the list.
    const pill = await screen.findByText('Default')
    fireEvent.click(pill)
    // Empty string is how the endpoint expresses "no default agent".
    await waitFor(() => expect(mockApi.setDefaultAgent).toHaveBeenCalledWith(''))
  })

  it('surfaces the current default in the summary cards, naming the scope', async () => {
    mockApi.defaultAgent.mockResolvedValue({ default_agent: 'kirocrew' })
    renderPage()
    expect(await screen.findByText('Default for new sessions')).toBeInTheDocument()
  })

  it('shows an em dash rather than a blank card when no default is set', async () => {
    mockApi.defaultAgent.mockResolvedValue({ default_agent: '' })
    renderPage()
    expect(await screen.findByText('Default for new sessions')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})
