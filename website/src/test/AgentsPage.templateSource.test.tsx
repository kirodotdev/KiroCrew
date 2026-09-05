/**
 * Agent Templates page — where a template came from, in words.
 *
 * The page printed the `source` field itself, so every language showed the
 * internal identifiers `kirocrew`, `package` and `builtin`. Two surfaces say it:
 * a badge beside the template name and the Source row under "Where it comes
 * from"; both must resolve through the shared label helper so they cannot drift
 * apart. The fixtures below use the values discovery actually assigns.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
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
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../store', () => ({ useAppSelector: (fn: (s: unknown) => unknown) => fn({ dashboard: { status: { sessions: 0, subagents: 0 }, refreshTrigger: 0 } }) }))
vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    displayName: 'Kiro',
    capabilities: { agentTemplates: true },
    labels: { sessionProcess: 'ACP subprocess', configFile: 'agent.json' },
    fetchAvailableModels: () => Promise.resolve([]),
    getContextWindow: () => 200_000,
  }),
}))

import AgentsPage from '../pages/AgentsPage'

/** `kirocrew.json` is one of the constant filenames Kiro Crew maintains itself. */
const OWNED = {
  name: 'kirocrew', description: 'Full crew agent', source: 'kirocrew', model: '',
  skills: [], mcp_servers: [], filename: 'kirocrew.json', kirocrew_owned: true,
}
/** A package install: `{package}-{name}.json`. */
const FROM_PACKAGE = {
  name: 'oncall-triage', description: 'Triages pages', source: 'package', model: '',
  skills: [], mcp_servers: [], package: 'oncall-radar',
  filename: 'local-oncall-radar-oncall-triage.json', kirocrew_owned: false,
}
/**
 * Discovery labels every plain `{name}.json` `builtin` — the fallback for a file
 * it cannot attribute, which is what a hand-written spec looks like. So this is
 * the case whose rendered word must NOT be "Built-in".
 */
const PLAIN_FILE = {
  name: 'reviewer', description: 'Reviews code', source: 'builtin', model: '',
  skills: [], mcp_servers: [], filename: 'reviewer.json', kirocrew_owned: false,
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AgentsPage embedded />
    </QueryClientProvider>,
  )
}

async function open(name: string) {
  const roster = await screen.findByRole('listbox', { name: 'Installed Agents' })
  const rows = await within(roster).findAllByRole('option')
  const row = rows.find(r => r.textContent?.startsWith(name))
  if (!row) throw new Error(`no roster row for ${name}`)
  fireEvent.click(row)
  await waitFor(() => expect(screen.getByRole('tablist')).toBeInTheDocument())
}

beforeEach(() => {
  Object.values(mockApi).forEach(fn => fn.mockReset())
  mockApi.spawnList.mockResolvedValue({ agents: [] })
  mockApi.sessionsContext.mockResolvedValue({ sessions: [] })
  mockApi.sessionsUsage.mockResolvedValue({ usage: null })
  mockApi.agentsInstalled.mockResolvedValue([OWNED, FROM_PACKAGE, PLAIN_FILE])
  mockApi.mcpProbeCache.mockResolvedValue([])
  mockApi.defaultAgent.mockResolvedValue({ default_agent: 'kirocrew' })
  mockApi.agentMetadata.mockResolvedValue({ content: '' })
  mockApi.skills.mockResolvedValue([])
  mockApi.kirocrewAgents.mockResolvedValue({ agents: [], default_agent: '' })
  mockApi.agentDetail.mockImplementation((name: string) => {
    const row = [OWNED, FROM_PACKAGE, PLAIN_FILE].find(a => a.name === name) ?? PLAIN_FILE
    return Promise.resolve({ ...row, tools: [], unmanaged_skills: [] })
  })
})

describe('agent templates page — source is shown as a word, not a field value', () => {
  it('calls a template Kiro Crew maintains itself Built-in, in both places', async () => {
    renderPage()
    await open('kirocrew')

    expect(await screen.findByText('Where it comes from')).toBeInTheDocument()
    // The header badge and the Source row: two independent render paths.
    expect(screen.getAllByText('Built-in')).toHaveLength(2)
  })

  it('names the actual package on the badge, keeping the category word in the detail row', async () => {
    renderPage()
    await open('oncall-triage')

    expect(await screen.findByText('Where it comes from')).toBeInTheDocument()
    // The badge and the dedicated Package row both carry the package name.
    expect(screen.getAllByText('oncall-radar').length).toBeGreaterThanOrEqual(2)
    // The Source detail row keeps the category word, so it does not repeat the
    // Package row right below it.
    expect(screen.getAllByText('Package').length).toBeGreaterThanOrEqual(1)
    // The raw lowercase field value never renders.
    expect(screen.queryByText('package')).toBeNull()
  })

  it('calls an unattributable plain spec Custom, never Built-in', async () => {
    renderPage()
    await open('reviewer')

    expect(await screen.findByText('Where it comes from')).toBeInTheDocument()
    expect(screen.getAllByText('Custom')).toHaveLength(2)
    // `builtin` is discovery's fallback, so claiming it ships with the product
    // would be a lie about a file the user may well have written.
    expect(screen.queryByText('Built-in')).toBeNull()
    expect(screen.queryByText('builtin')).toBeNull()
  })
})
