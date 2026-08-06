/**
 * The Agent Templates page's default-template control.
 *
 * This used to be a star pill on every row, which had to mean both "this is the
 * default" (state) and "make this the default" (control) with the same glyph,
 * and a StatCard above the list restating the answer. It is now one picker: the
 * assertions below are about that single place naming the current default and
 * writing the new one without a Save step.
 *
 * The label is asserted verbatim on purpose. This control writes
 * `config.agent.default_agent`, which is the fallback kiro-cli agent for CLI
 * chat, chat-channel threads and warm-pool sessions — NOT what a dashboard
 * session uses (that comes from its crew). A label reading "new sessions use"
 * would describe a different setting than the one being written.
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
  kirocrewAgents: vi.fn(),
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
  mockApi.kirocrewAgents.mockResolvedValue({ agents: [], default_agent: '' })
  mockApi.skills.mockResolvedValue([])
  mockApi.agentDetail.mockResolvedValue({ ...mkAgent('kirocrew'), unmanaged_skills: [] })
  mockApi.setDefaultAgent.mockResolvedValue({ ok: true, default_agent: '' })
})

describe('AgentsPage default template', () => {
  it('names the current default in one place', async () => {
    mockApi.defaultAgent.mockResolvedValue({ default_agent: 'fable' })
    renderPage()

    const picker = await screen.findByRole('combobox', { name: 'When nothing names a template, use' })
    expect(picker).toHaveTextContent('fable')
    // The state is no longer duplicated onto every row as a control.
    expect(screen.queryByText('Set as default')).not.toBeInTheDocument()
  })

  it('writes the pick immediately, with no Save step', async () => {
    mockApi.defaultAgent.mockResolvedValue({ default_agent: 'kirocrew' })
    renderPage()

    fireEvent.click(await screen.findByRole('combobox', { name: 'When nothing names a template, use' }))
    fireEvent.click(await screen.findByRole('option', { name: 'fable' }))

    await waitFor(() => expect(mockApi.setDefaultAgent).toHaveBeenCalledWith('fable'))
    expect(mockApi.agentPatch).not.toHaveBeenCalled()
  })

  it('can clear the default from the same picker', async () => {
    mockApi.defaultAgent.mockResolvedValue({ default_agent: 'kirocrew' })
    renderPage()

    fireEvent.click(await screen.findByRole('combobox', { name: 'When nothing names a template, use' }))
    fireEvent.click(await screen.findByRole('option', { name: 'No default template' }))

    await waitFor(() => expect(mockApi.setDefaultAgent).toHaveBeenCalledWith(''))
  })

  it('is absent when there are no templates to choose between', async () => {
    mockApi.agentsInstalled.mockResolvedValue([])
    mockApi.defaultAgent.mockResolvedValue({ default_agent: '' })
    renderPage()

    expect(await screen.findByText('No agent templates yet')).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'When nothing names a template, use' })).not.toBeInTheDocument()
  })
})
