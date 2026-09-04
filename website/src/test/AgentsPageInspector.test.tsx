/**
 * Agent Templates inspector — roster, tabs, provenance, delete.
 *
 * The tab exposed one template's whole spec through a 420px-tall box: prompt,
 * tool chips, MCP chips and 96 denied-command patterns all scrolled inside it,
 * and delete went through a native `confirm()`. These tests pin the behaviour of
 * the replacement rather than its markup — every query is by accessible name or
 * an explicit test id so a restyle cannot turn the suite red.
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
  agentMetadataSave: vi.fn(),
  kirocrewAgents: vi.fn(),
  skills: vi.fn(),
  agentPatch: vi.fn(),
  agentDelete: vi.fn(),
  spawnClear: vi.fn(),
  spawnDelete: vi.fn(),
  setDefaultAgent: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../store', () => ({ useAppSelector: (fn: (s: unknown) => unknown) => fn({ dashboard: { status: { sessions: 0, subagents: 0 }, refreshTrigger: 0 } }) }))
vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    displayName: 'Kiro',
    capabilities: { agentTemplates: true },
    labels: { sessionProcess: 'ACP subprocess', configFile: 'agent.json' },
    fetchAvailableModels: () => Promise.resolve([{ name: 'claude-opus-4.8', description: '' }]),
    getContextWindow: () => 200_000,
  }),
}))

import AgentsPage from '../pages/AgentsPage'

const BUILTIN = {
  name: 'kirocrew',
  description: 'Full crew agent',
  source: 'kirocrew',
  model: 'claude-opus-4.8',
  skills: ['prepare-pr'],
  mcp_servers: ['kirocrew-core'],
  filename: 'kirocrew.json',
}
const USER_TEMPLATE = {
  name: 'reviewer',
  description: 'Reviews code',
  source: 'user',
  model: '',
  skills: [],
  mcp_servers: [],
  filename: 'reviewer.json',
}
const SCRATCH_TEMPLATE = {
  name: 'scratch',
  description: 'Nothing points at this one',
  source: 'user',
  model: '',
  skills: [],
  mcp_servers: [],
  filename: 'scratch.json',
}
const PACKAGE_TEMPLATE = {
  name: 'oncall-triage',
  description: 'Triages pages',
  source: 'package',
  model: 'claude-sonnet-4.6',
  skills: [],
  mcp_servers: [],
  package: 'oncall-radar',
  filename: 'local-oncall-radar.json',
}

const BUILTIN_DETAIL = {
  ...BUILTIN,
  prompt: 'file://~/.kiro/crew/prompts/kirocrew.md',
  tools: ['fs_read', 'fs_write'],
  allowedTools: ['fs_read'],
  mcpServers: { 'kirocrew-core': {} },
  toolsSettings: { execute_bash: { deniedCommands: ['rm -rf /', 'DROP TABLE'] } },
  skills: ['kiro-user/prepare-pr'],
  unmanaged_skills: [],
}

function renderPage(qc?: QueryClient) {
  const client = qc ?? new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AgentsPage embedded />
    </QueryClientProvider>,
  )
}

/** Select a template in the roster and wait for its detail to land.
 *  Queried inside the roster listbox: the selected template's name also appears
 *  in the detail header, so a bare text query is ambiguous by design. */
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
  mockApi.agentsInstalled.mockResolvedValue([BUILTIN, USER_TEMPLATE, PACKAGE_TEMPLATE, SCRATCH_TEMPLATE])
  mockApi.mcpProbeCache.mockResolvedValue([])
  mockApi.defaultAgent.mockResolvedValue({ default_agent: 'kirocrew' })
  mockApi.agentMetadata.mockResolvedValue({ content: '' })
  mockApi.skills.mockResolvedValue([])
  mockApi.agentDelete.mockResolvedValue({ ok: true })
  mockApi.kirocrewAgents.mockResolvedValue({
    agents: [
      { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: '', source: 'user' },
      { name: 'oncall', kiro_agent: 'kirocrew', workspace: 'oncall', memory_store: 'oncall', description: '', source: 'user' },
      { name: 'research', kiro_agent: 'reviewer', workspace: 'research', memory_store: 'research', description: '', source: 'user' },
    ],
    default_agent: 'default',
  })
  mockApi.agentDetail.mockImplementation((name: string) => {
    if (name === 'kirocrew') return Promise.resolve(BUILTIN_DETAIL)
    if (name === 'reviewer') return Promise.resolve({ ...USER_TEMPLATE, tools: ['fs_read'], unmanaged_skills: [] })
    if (name === 'scratch') return Promise.resolve({ ...SCRATCH_TEMPLATE, tools: ['fs_read'], unmanaged_skills: [] })
    return Promise.resolve({ ...PACKAGE_TEMPLATE, unmanaged_skills: [] })
  })
})

describe('agent templates inspector — tabs', () => {
  it('gives the prompt and the guardrails a pane each instead of one scroll box', async () => {
    renderPage()
    await open('kirocrew')

    // Overview is what a newly-selected template opens on.
    expect(await screen.findByText('Where it comes from')).toBeInTheDocument()
    expect(screen.queryByText('rm -rf /')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /system prompt/i }))
    expect(await screen.findByText(/prompts\/kirocrew\.md/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /guardrails/i }))
    expect(await screen.findByText('rm -rf /')).toBeInTheDocument()
    expect(screen.getByText('DROP TABLE')).toBeInTheDocument()
    expect(screen.getByText(/read-only here/i)).toBeInTheDocument()
  })

  it('sends an At a glance count to the tab that owns the detail', async () => {
    renderPage()
    await open('kirocrew')

    fireEvent.click(await screen.findByRole('button', { name: /2 tools/i }))

    expect(await screen.findByText('fs_write')).toBeInTheDocument()
  })

  it('reopens on Overview when the selection moves, not on the previous tab', async () => {
    renderPage()
    await open('kirocrew')

    fireEvent.click(screen.getByRole('tab', { name: /guardrails/i }))
    expect(await screen.findByText('rm -rf /')).toBeInTheDocument()

    await open('reviewer')
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true'))
  })
})

describe('agent templates inspector — overview', () => {
  it('says which model the pin resolves to, in words', async () => {
    renderPage()
    await open('kirocrew')

    expect(await screen.findByText(/an agent set to inherit uses this model/i)).toBeInTheDocument()

    await open('reviewer')
    expect(await screen.findByText(/falls back to the global default/i)).toBeInTheDocument()
  })

  it('names the crews an edit would change', async () => {
    renderPage()
    await open('kirocrew')

    expect(await screen.findByText('Used By')).toBeInTheDocument()
    expect(screen.getByText('default')).toBeInTheDocument()
    expect(screen.getByText('oncall')).toBeInTheDocument()
    // Bound to `reviewer`, so it must not be listed under this template.
    expect(screen.queryByText('research')).not.toBeInTheDocument()
  })

  it('says so plainly when no crew is bound to the template', async () => {
    mockApi.kirocrewAgents.mockResolvedValue({ agents: [], default_agent: '' })
    renderPage()
    await open('kirocrew')

    expect(await screen.findByText('No agents use this template yet')).toBeInTheDocument()
  })
})

describe('agent templates inspector — roster filter', () => {
  it('narrows the roster and says when nothing matches', async () => {
    renderPage()
    const roster = await screen.findByRole('listbox', { name: 'Installed Agents' })
    await waitFor(() => expect(within(roster).getAllByRole('option')).toHaveLength(4))

    const filter = screen.getByRole('textbox', { name: 'Filter templates' })
    fireEvent.change(filter, { target: { value: 'oncall' } })

    await waitFor(() => expect(within(roster).getAllByRole('option')).toHaveLength(1))
    expect(within(roster).getByRole('option')).toHaveTextContent('oncall-triage')

    fireEvent.change(filter, { target: { value: 'zzz' } })
    expect(await screen.findByText('No templates match your filter')).toBeInTheDocument()
  })
})

describe('agent templates inspector — shared crews cache', () => {
  /* The Crews tab owns ['kirocrew-agents', <trigger>] and stores the whole
     response object. One QueryClient keeps ONE value per key, so if this page
     stored the unwrapped array instead, whichever tab mounted first would hand
     the other the wrong shape: `crews.filter` on an object throws during
     render here, and `agentsData?.agents` on an array silently empties the
     Crews roster. Both directions are asserted. */
  it('reads and leaves the cache in the shape the Crews tab stores', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['kirocrew-agents'], {
      agents: [{ name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: '', source: 'user' }],
      default_agent: 'default',
    })

    renderPage(qc)
    await open('kirocrew')

    expect(await screen.findByText('Used By')).toBeInTheDocument()
    expect(screen.getByText('default')).toBeInTheDocument()
    const cached = qc.getQueryData(['kirocrew-agents'])
    expect(Array.isArray(cached)).toBe(false)
    expect(cached).toMatchObject({ default_agent: 'default' })
  })
})

describe('agent templates inspector — stale detail responses', () => {
  it('ignores a detail response that lands after the selection moved', async () => {
    let landReviewer = () => {}
    mockApi.agentDetail.mockImplementation((name: string) => {
      if (name === 'reviewer') {
        return new Promise(resolve => {
          landReviewer = () => resolve({ ...USER_TEMPLATE, tools: ['fs_read'], unmanaged_skills: [] })
        })
      }
      if (name === 'kirocrew') return Promise.resolve(BUILTIN_DETAIL)
      return Promise.resolve({ ...PACKAGE_TEMPLATE, unmanaged_skills: [] })
    })

    renderPage()
    await open('kirocrew')

    // reviewer's fetch hangs; the user gives up and picks the package template.
    const roster = await screen.findByRole('listbox', { name: 'Installed Agents' })
    const rows = within(roster).getAllByRole('option')
    fireEvent.click(rows.find(r => r.textContent?.startsWith('reviewer'))!)
    fireEvent.click(rows.find(r => r.textContent?.startsWith('oncall-triage'))!)
    await waitFor(() => expect(screen.getAllByText('oncall-radar').length).toBeGreaterThan(0))

    landReviewer()

    // The pane still belongs to the last CLICK, not the last RESPONSE — and the
    // template the stale response named offers no delete, so a retargeted
    // confirm cannot appear either.
    await waitFor(() => expect(screen.getAllByText('oncall-radar').length).toBeGreaterThan(0))
    expect(screen.queryByTestId('delete-template')).not.toBeInTheDocument()
    expect(screen.queryByTestId('confirm-delete-template')).not.toBeInTheDocument()
  })

  it('disarms a confirmed delete synchronously when another template is picked', async () => {
    renderPage()
    await open('scratch')

    fireEvent.click(await screen.findByTestId('delete-template'))
    expect(screen.getByTestId('confirm-delete-template')).toBeInTheDocument()

    // Not awaited: the disarm must happen on the click, not after the next
    // template's detail resolves and the passive effect runs.
    const roster = screen.getByRole('listbox', { name: 'Installed Agents' })
    const row = within(roster).getAllByRole('option').find(r => r.textContent?.startsWith('oncall-triage'))!
    fireEvent.click(row)
    expect(screen.queryByTestId('confirm-delete-template')).not.toBeInTheDocument()
    expect(mockApi.agentDelete).not.toHaveBeenCalled()
  })
})

describe('agent templates inspector — delete', () => {
  it('parks focus on Cancel, never on the destructive confirm', async () => {
    renderPage()
    await open('scratch')

    fireEvent.click(await screen.findByTestId('delete-template'))

    /* Enter fires a native button's click on keydown AND auto-repeats, so focus
       on "Yes, delete it" would let one held Enter arm and delete in a single
       gesture — the two-step would exist in markup only. */
    expect(screen.getByTestId('cancel-delete-template')).toHaveFocus()
    expect(screen.getByTestId('confirm-delete-template')).not.toHaveFocus()
  })

  it('asks twice before removing a config file', async () => {
    renderPage()
    await open('scratch')

    fireEvent.click(await screen.findByTestId('delete-template'))
    // Arming is not deleting.
    expect(mockApi.agentDelete).not.toHaveBeenCalled()
    expect(screen.getByText(/Delete the template scratch\?/)).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('cancel-delete-template'))
    expect(mockApi.agentDelete).not.toHaveBeenCalled()

    fireEvent.click(await screen.findByTestId('delete-template'))
    fireEvent.click(screen.getByTestId('confirm-delete-template'))
    await waitFor(() => expect(mockApi.agentDelete).toHaveBeenCalledWith('scratch'))
  })

  it('disarms a half-confirmed delete when the selection moves', async () => {
    renderPage()
    await open('scratch')

    fireEvent.click(await screen.findByTestId('delete-template'))
    expect(screen.getByTestId('confirm-delete-template')).toBeInTheDocument()

    await open('oncall-triage')
    await waitFor(() => expect(screen.queryByTestId('confirm-delete-template')).not.toBeInTheDocument())
  })

  it('offers no delete for a built-in or a package template', async () => {
    renderPage()
    await open('kirocrew')
    expect(screen.queryByTestId('delete-template')).not.toBeInTheDocument()

    await open('oncall-triage')
    await waitFor(() => expect(screen.getAllByText('oncall-radar').length).toBeGreaterThan(0))
    expect(screen.queryByTestId('delete-template')).not.toBeInTheDocument()
  })

  /* Deleting a template something still points at leaves a dangling reference:
     a crew bound to it boots from nothing, and the fallback name is handed to
     kiro-cli as an agent id. Both guards say what to do first, because a button
     that silently disappears teaches nothing. */
  it('refuses to delete a template a crew is still bound to, and says which', async () => {
    mockApi.kirocrewAgents.mockResolvedValue({
      agents: [{ name: 'research', kiro_agent: 'reviewer', workspace: 'r', memory_store: 'r', description: '', source: 'user' }],
      default_agent: 'default',
    })
    renderPage()
    await open('reviewer')

    expect(await screen.findByText(/research .*repoint them on the Agents tab/i)).toBeInTheDocument()
    expect(screen.queryByTestId('delete-template')).not.toBeInTheDocument()
  })

  it('refuses to delete the fallback template', async () => {
    mockApi.defaultAgent.mockResolvedValue({ default_agent: 'scratch' })
    mockApi.kirocrewAgents.mockResolvedValue({ agents: [], default_agent: '' })
    renderPage()
    await open('scratch')

    expect(await screen.findByText(/fallback template cannot be deleted/i)).toBeInTheDocument()
    expect(screen.queryByTestId('delete-template')).not.toBeInTheDocument()
  })

  it('withholds delete until the crew roster has actually loaded', async () => {
    // An unresolved query makes `usedBy` empty, which must not read as
    // "nothing uses it" — that is exactly how a bound template gets deleted.
    let landCrews: (v: unknown) => void = () => {}
    mockApi.kirocrewAgents.mockImplementation(() => new Promise(resolve => { landCrews = resolve }))
    renderPage()
    await open('reviewer')

    expect(screen.queryByTestId('delete-template')).not.toBeInTheDocument()

    landCrews({ agents: [], default_agent: '' })
    expect(await screen.findByTestId('delete-template')).toBeInTheDocument()
  })

  it('withholds delete until the fallback setting has actually loaded', async () => {
    // Same hole on the other query: an unresolved default reads as '' , which
    // would never equal the selected name, so the fallback template would be
    // offered for deletion on first paint.
    let landDefault: (v: unknown) => void = () => {}
    mockApi.defaultAgent.mockImplementation(() => new Promise(resolve => { landDefault = resolve }))
    renderPage()
    await open('scratch')

    expect(screen.queryByTestId('delete-template')).not.toBeInTheDocument()

    landDefault({ default_agent: 'scratch' })
    expect(await screen.findByText(/fallback template cannot be deleted/i)).toBeInTheDocument()
    expect(screen.queryByTestId('delete-template')).not.toBeInTheDocument()
  })

  it('surfaces a server refusal and re-syncs instead of failing silently', async () => {
    // The handler checks references under the config lock, so it can refuse a
    // delete this page's cached snapshot thought was fine. That refusal has to
    // be visible, and the stale snapshot has to be re-read.
    // api/client raises ApiError, whose `message` is already the unwrapped
    // server text — a plain Error models exactly what the page reads.
    mockApi.agentDelete.mockRejectedValue(
      new Error("Cannot delete 'scratch': still used by researcher."),
    )
    renderPage()
    await open('scratch')

    fireEvent.click(await screen.findByTestId('delete-template'))
    fireEvent.click(screen.getByTestId('confirm-delete-template'))

    expect(await screen.findByText(/still used by researcher/)).toBeInTheDocument()
    // Re-synced, so the next render derives blockedBy from fresh data.
    await waitFor(() => expect(mockApi.kirocrewAgents.mock.calls.length).toBeGreaterThan(1))
    // And the armed confirm is dropped — no second identical attempt on one click.
    expect(screen.queryByTestId('confirm-delete-template')).not.toBeInTheDocument()
  })

  it('withholds delete when a reference REFETCH fails, leaving stale data behind', async () => {
    // The sharp case: a failed refetch keeps the last good data, so the mutation
    // settles and `crewsData` stays defined — an undefined-check alone would
    // keep comparing against a roster the server may no longer agree with.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['kirocrew-agents'], { agents: [], default_agent: '' })

    renderPage(qc)
    await open('scratch')
    expect(await screen.findByTestId('delete-template')).toBeInTheDocument()

    mockApi.kirocrewAgents.mockRejectedValue(new Error('network'))
    await qc.refetchQueries({ queryKey: ['kirocrew-agents'] })

    await waitFor(() => expect(screen.queryByTestId('delete-template')).not.toBeInTheDocument())
  })

  it('withholds delete while a fallback change is still in flight, and disarms', async () => {
    // The PUT can land before the refetch, so `defaultAgent` still names the
    // OLD template while config already names this one.
    let landWrite: (v: unknown) => void = () => {}
    mockApi.setDefaultAgent.mockImplementation(() => new Promise(resolve => { landWrite = resolve }))
    renderPage()
    await open('scratch')

    fireEvent.click(await screen.findByTestId('delete-template'))
    expect(screen.getByTestId('confirm-delete-template')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('combobox', { name: 'When nothing names a template, use' }))
    fireEvent.click(await screen.findByRole('option', { name: 'scratch' }))

    // Armed state is dropped at the source, and delete stays withheld until the
    // write settles and the refetched setting can be compared.
    expect(screen.queryByTestId('confirm-delete-template')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('delete-template')).not.toBeInTheDocument())
    expect(mockApi.agentDelete).not.toHaveBeenCalled()

    landWrite({ ok: true, default_agent: 'scratch' })
  })
})
