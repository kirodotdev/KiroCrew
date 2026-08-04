import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the page imports ── */
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

const AGENT = {
  name: 'specialist',
  description: 'Reviews code',
  source: 'builtin',
  model: 'claude-opus-4.8',
  // The LIST row carries display NAMES, not catalog keys — feeding these to the
  // editor would offer chips that cannot be saved.
  skills: ['prepare-pr', 'rubber-duck'],
  mcp_servers: [],
  filename: 'specialist.json',
}

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
  mockApi.agentsInstalled.mockResolvedValue([AGENT])
  mockApi.mcpProbeCache.mockResolvedValue([])
  mockApi.defaultAgent.mockResolvedValue({ default_agent: '' })
  mockApi.skills.mockResolvedValue([{ key: 'babysit', name: 'babysit', description: '' }])
  mockApi.agentPatch.mockResolvedValue({ ok: true })
})

describe('AgentsPage skills section', () => {
  it('offers the editor when agent detail loaded', async () => {
    mockApi.agentDetail.mockResolvedValue({ ...AGENT, skills: ['kiro-user/prepare-pr'], unmanaged_skills: [] })
    renderPage()
    expect(await screen.findByRole('button', { name: /add skill/i })).toBeInTheDocument()
  })

  it('hides the editor — never an empty enabled one — when agent detail fails', async () => {
    // The real mapping is UNKNOWN here. An empty-but-enabled editor is
    // destructive: add/remove PATCH the complete desired key list and the
    // backend fully replaces the managed skill:// set, so a single "Add skill"
    // click over unknown state would delete every real mapping on disk.
    mockApi.agentDetail.mockRejectedValue(new Error('500 boom'))
    renderPage()

    // The fallback only triggers on an explicit selection whose detail fetch fails.
    fireEvent.click(await screen.findByText('specialist'))

    await waitFor(() => expect(screen.getByText(/Could not load this agent/i)).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /add skill/i })).not.toBeInTheDocument()
    expect(mockApi.agentPatch).not.toHaveBeenCalled()
  })
})
