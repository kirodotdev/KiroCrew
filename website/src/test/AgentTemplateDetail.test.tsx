/**
 * The Agent Template PANEL in the crew editor.
 *
 * The template selector IS the panel's header bar, and the panel visibly
 * contains the definition it names. Blueprint (fork-on-first-edit) semantics
 * shape every state:
 *  - Shared/built-in/package template bound: the bar states source + reach and
 *    the helper line says an edit will branch a private copy; the first edit
 *    routes through a fork.
 *  - The crew's OWN copy bound: the bar shows the ORIGIN name (the copy's
 *    filename is bookkeeping), a Customized tag, a live change-count pill diffed
 *    against the origin, and two actions — Reset and Save as new template.
 *  - Switching away from a customized state, and Reset, both confirm first.
 *
 * Queried by visible text and accessible roles rather than markup, so a restyle
 * cannot turn these red.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({
  agentDetail: vi.fn(),
  agentsInstalled: vi.fn(),
  kirocrewAgents: vi.fn(),
  agentPatch: vi.fn(),
  agentFork: vi.fn(),
  agentPublish: vi.fn(),
  updateKirocrewAgent: vi.fn(),
  agentDelete: vi.fn(),
  skills: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import AgentTemplateDetail from '../components/crew/AgentTemplateDetail'
import { agentTemplatePaneEnabled } from '../hooks/useAgentTemplatePane'

const DENIED = Array.from({ length: 11 }, (_, i) => `denied-${i}`)
const FIELD_LABEL = 'Agent Template'

interface PaneProps {
  template?: string
  models?: string[]
  options?: string[]
  crew?: string
  onForked?: (t: string) => void
  onSelect?: (v: string) => void
  provenance?: Record<string, { source?: string; package?: string; kirocrew_owned?: boolean }>
}

/** Render the panel with the required props filled in; returns the mock
 *  callbacks so a test can assert against them. */
function renderPane(overrides: PaneProps = {}) {
  const onSelect = overrides.onSelect ?? vi.fn()
  const onForked = overrides.onForked ?? vi.fn()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AgentTemplateDetail
        template={overrides.template ?? 'atlas'}
        models={overrides.models ?? ['opus', 'sonnet']}
        options={overrides.options ?? ['atlas', 'reviewer']}
        crew={overrides.crew}
        onForked={onForked}
        onSelect={onSelect}
        provenance={overrides.provenance}
        fieldLabel={FIELD_LABEL}
      />
    </QueryClientProvider>,
  )
  return { onSelect, onForked }
}

/** An installed row describing the crew's OWN copy of `atlas`. */
const OWN_COPY_INSTALLED = [
  { name: 'atlas-crewA', source: 'builtin', filename: 'atlas-crewA.json', private_to: 'crewA', forked_from: 'atlas' },
]

/** Point `agentDetail` at a per-name lookup, so the copy and its origin can
 *  return different specs for the live diff. */
function detailsByName(map: Record<string, unknown>) {
  mockApi.agentDetail.mockImplementation((n: string) => Promise.resolve(map[n] ?? { name: n, skills: [] }))
}

/** Open the model dropdown and pick a value (Radix Select, driven by click). */
async function pickModel(name: string) {
  fireEvent.click(await screen.findByRole('combobox', { name: 'Model' }))
  fireEvent.click(await screen.findByRole('option', { name }))
}

/** Open the header template dropdown and pick a value. */
async function pickTemplate(name: string) {
  fireEvent.click(await screen.findByRole('combobox', { name: FIELD_LABEL }))
  fireEvent.click(await screen.findByRole('option', { name }))
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.skills.mockResolvedValue([])
  mockApi.agentPatch.mockResolvedValue({})
  mockApi.agentFork.mockResolvedValue({ template: 'atlas-crewA' })
  mockApi.agentPublish.mockResolvedValue({ template: 'published-name' })
  mockApi.updateKirocrewAgent.mockResolvedValue({})
  mockApi.agentDelete.mockResolvedValue({})
  // A plain array, which is what the endpoint really answers — an object with an
  // `agents` key silently blanks provenance, so the fixture has to match.
  mockApi.agentsInstalled.mockResolvedValue([
    // 'builtin' is the endpoint's fallback for a plain `<name>.json`; on a
    // non-owned template it reads as 'custom' (relation to the shipped set).
    { name: 'atlas', source: 'builtin', filename: 'atlas.json', kirocrew_owned: false },
  ])
  mockApi.kirocrewAgents.mockResolvedValue({
    agents: [{ name: 'atlas', kiro_agent: 'atlas' }, { name: 'gpu-critic', kiro_agent: 'atlas' }],
  })
  mockApi.agentDetail.mockResolvedValue({
    name: 'atlas',
    model: 'opus',
    prompt: 'Plan before acting.',
    skills: ['grill'],
    tools: ['fs_read', 'fs_write'],
    allowedTools: ['fs_read'],
    mcpServers: { 'kirocrew-core': {} },
    toolsSettings: { execute_bash: { deniedCommands: DENIED } },
  })
})

describe('agent_template_pane flag', () => {
  it('is off unless the config says exactly true', () => {
    // A truthy-but-not-true value must not open a surface that relocates
    // shipped content — same contract as the connections_ui flag.
    expect(agentTemplatePaneEnabled(undefined)).toBe(false)
    expect(agentTemplatePaneEnabled({})).toBe(false)
    expect(agentTemplatePaneEnabled({ agent_template_pane: 'yes' })).toBe(false)
    expect(agentTemplatePaneEnabled({ agent_template_pane: true })).toBe(true)
  })
})

describe('the header bar is the template selector', () => {
  it('labels the selector "Template" and shows the bound template as its value', async () => {
    renderPane()
    expect(await screen.findByText('Template')).toBeInTheDocument()
    const selector = screen.getByRole('combobox', { name: FIELD_LABEL })
    expect(selector).toHaveTextContent('atlas')
  })

  it('renders the definition inside the panel: section totals a capped list cannot show', async () => {
    renderPane()
    // 11 denied commands, only 6 chips rendered: the title carries the total.
    expect(await screen.findByText(String(DENIED.length))).toBeInTheDocument()
    expect(screen.getByText('denied-0')).toBeInTheDocument()
    expect(screen.queryByText('denied-10')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText(/\+ 5 more/))
    expect(await screen.findByText('denied-10')).toBeInTheDocument()
  })

  it('keeps guardrails read-only, because the PATCH route cannot write them', async () => {
    renderPane()
    expect(await screen.findByText(/read-only here/i)).toBeInTheDocument()
  })

  it('says a file-backed prompt lives in a file instead of showing an empty box', async () => {
    mockApi.agentDetail.mockResolvedValue({
      name: 'atlas',
      prompt: 'file://~/.kiro/agents/prompts/atlas.md',
      skills: [],
    })
    renderPane()
    expect(await screen.findByText(/live in a file/i)).toBeInTheDocument()
    expect(screen.getByText('file://~/.kiro/agents/prompts/atlas.md')).toBeInTheDocument()
  })

  it('surfaces a load failure rather than rendering an empty definition', async () => {
    mockApi.agentDetail.mockRejectedValue(new Error('boom'))
    renderPane()
    await waitFor(() =>
      expect(screen.getByText(/could not load this template/i)).toBeInTheDocument(),
    )
  })
})

describe('shared (non-copy) template bound', () => {
  it('states source, blast radius and the fork hint — and offers no copy-only controls', async () => {
    renderPane({ crew: 'crewA' })
    // The source badge names where it came from; 'custom' is the fallback the
    // filename-guessed provenance reports for a plain builtin spec.
    expect(await screen.findByText('Custom')).toBeInTheDocument()
    // Two crews point at it, so the reach is stated rather than implied.
    expect(screen.getByText(/used by 2 agents/i)).toBeInTheDocument()
    // The helper line warns that the first edit branches a private copy.
    expect(screen.getByText(/its own copy of the template/i)).toBeInTheDocument()

    // None of the own-copy affordances exist on a shared template.
    expect(screen.queryByText('Customized')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /\d+ changes?/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reset my changes' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Save as new template/ })).not.toBeInTheDocument()
  })

  it('reads a template Kiro Crew owns as built-in, not as the reader\'s own', async () => {
    // Provenance is guessed from the filename, so every helper spec Kiro Crew
    // installs reports source 'builtin' — the same value a hand-written one
    // gets. `kirocrew_owned` is the tie-breaker that must read "Built-in".
    mockApi.agentsInstalled.mockResolvedValue([
      { name: 'kirocrew-heartbeat', source: 'builtin', filename: 'kirocrew-heartbeat.json', kirocrew_owned: true },
    ])
    mockApi.agentDetail.mockResolvedValue({ name: 'kirocrew-heartbeat', skills: [] })
    renderPane({ template: 'kirocrew-heartbeat', options: ['kirocrew-heartbeat'] })
    expect(await screen.findByText('Built-in')).toBeInTheDocument()
    expect(screen.queryByText('Customized')).not.toBeInTheDocument()
  })

  it('names the actual package as the source', async () => {
    mockApi.agentsInstalled.mockResolvedValue([
      { name: 'papyrus-writer', source: 'package', package: 'papyrus', filename: 'papyrus-writer.json' },
    ])
    mockApi.agentDetail.mockResolvedValue({ name: 'papyrus-writer', skills: [] })
    renderPane({ template: 'papyrus-writer', options: ['papyrus-writer'] })
    expect(await screen.findByText('papyrus')).toBeInTheDocument()
  })
})

describe('the crew\'s own copy bound', () => {
  it('shows the ORIGIN name in the header, not the copy\'s bookkeeping filename', async () => {
    mockApi.agentsInstalled.mockResolvedValue(OWN_COPY_INSTALLED)
    detailsByName({
      'atlas-crewA': { name: 'atlas-crewA', model: 'opus', skills: [] },
      atlas: { name: 'atlas', model: 'opus', skills: [] },
    })
    renderPane({ template: 'atlas-crewA', crew: 'crewA' })

    // The Customized tag appears only once the installed query has resolved the
    // own-copy state — anchor on it so the header value read below is settled.
    expect(await screen.findByText('Customized')).toBeInTheDocument()
    const selector = screen.getByRole('combobox', { name: FIELD_LABEL })
    expect(selector).toHaveTextContent('atlas')
    expect(selector).not.toHaveTextContent('atlas-crewA')
    // The own copy is nobody else's, so the shared-template lines are suppressed.
    expect(screen.queryByText(/used by 2 agents/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/its own copy of the template/i)).not.toBeInTheDocument()
    expect(screen.getByText(/Based on the atlas template/i)).toBeInTheDocument()
  })

  it('counts the changed sections by diffing the copy against its origin', async () => {
    mockApi.agentsInstalled.mockResolvedValue(OWN_COPY_INSTALLED)
    // Differ in EXACTLY two sections: model and prompt. Everything else equal.
    detailsByName({
      'atlas-crewA': { name: 'atlas-crewA', model: 'sonnet', prompt: 'New prompt', skills: ['grill'] },
      atlas: { name: 'atlas', model: 'opus', prompt: 'Old prompt', skills: ['grill'] },
    })
    renderPane({ template: 'atlas-crewA', crew: 'crewA' })

    expect(await screen.findByRole('button', { name: /2 changes/ })).toBeInTheDocument()
  })

  it('opens a popover listing the changed field labels with their "was:" values', async () => {
    mockApi.agentsInstalled.mockResolvedValue(OWN_COPY_INSTALLED)
    detailsByName({
      'atlas-crewA': { name: 'atlas-crewA', model: 'sonnet', prompt: 'New prompt', skills: ['grill'] },
      atlas: { name: 'atlas', model: 'opus', prompt: 'Old prompt', skills: ['grill'] },
    })
    renderPane({ template: 'atlas-crewA', crew: 'crewA' })

    fireEvent.click(await screen.findByRole('button', { name: /2 changes/ }))

    // Each changed row pairs the field label with its prior value.
    const wasOpus = await screen.findByText('was: opus')
    const modelRow = wasOpus.closest('div') as HTMLElement
    expect(within(modelRow).getByText('Model')).toBeInTheDocument()

    const wasPrompt = screen.getByText('was: Old prompt')
    const promptRow = wasPrompt.closest('div') as HTMLElement
    expect(within(promptRow).getByText('System Prompt')).toBeInTheDocument()
  })

  it('offers no Reset when the copy matches its origin (nothing to reset)', async () => {
    mockApi.agentsInstalled.mockResolvedValue(OWN_COPY_INSTALLED)
    // Identical specs -> zero changes -> no change pill, so no popover and no Reset.
    const same = { name: 'x', model: 'opus', prompt: 'same', skills: ['grill'] }
    detailsByName({ 'atlas-crewA': same, atlas: same })
    renderPane({ template: 'atlas-crewA', crew: 'crewA' })

    await screen.findByRole('button', { name: 'Save as new template…' })
    expect(screen.queryByRole('button', { name: /\d+ changes?/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reset my changes' })).not.toBeInTheDocument()
  })

  it('Reset confirms, then rebinds to the origin and removes the copy', async () => {
    mockApi.agentsInstalled.mockResolvedValue(OWN_COPY_INSTALLED)
    detailsByName({
      'atlas-crewA': { name: 'atlas-crewA', model: 'sonnet', skills: ['grill'] },
      atlas: { name: 'atlas', model: 'opus', skills: ['grill'] },
    })
    const { onSelect } = renderPane({ template: 'atlas-crewA', crew: 'crewA' })

    // Reset lives in the changes popover; the pill only renders once the live
    // diff against the origin has resolved, so finding it doubles as the wait.
    fireEvent.click(await screen.findByRole('button', { name: /1 change/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Reset my changes' }))
    // A confirm stands between the click and the destructive rebind.
    fireEvent.click(await screen.findByRole('button', { name: 'Discard my changes' }))

    await waitFor(() =>
      expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('crewA', { kiro_agent: 'atlas' }),
    )
    await waitFor(() => expect(mockApi.agentDelete).toHaveBeenCalledWith('atlas-crewA'))
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith('atlas'))
  })
})

describe('Save as new template', () => {
  beforeEach(() => {
    mockApi.agentsInstalled.mockResolvedValue(OWN_COPY_INSTALLED)
    detailsByName({
      'atlas-crewA': { name: 'atlas-crewA', model: 'sonnet', skills: ['grill'] },
      atlas: { name: 'atlas', model: 'opus', skills: ['grill'] },
    })
  })

  it('rejects an invalid name with the rule text and never calls the API', async () => {
    renderPane({ template: 'atlas-crewA', crew: 'crewA' })
    fireEvent.click(await screen.findByRole('button', { name: /Save as new template/ }))

    const input = await screen.findByRole('textbox')
    fireEvent.change(input, { target: { value: 'bad name!' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save template' }))

    expect(await screen.findByText(/letters, digits, dots, dashes or underscores/i)).toBeInTheDocument()
    expect(mockApi.agentPublish).not.toHaveBeenCalled()
  })

  it('publishes a valid name, then rebinds the crew to the new template', async () => {
    mockApi.agentPublish.mockResolvedValue({ template: 'my-template' })
    const { onSelect } = renderPane({ template: 'atlas-crewA', crew: 'crewA' })
    fireEvent.click(await screen.findByRole('button', { name: /Save as new template/ }))

    fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'my-template' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save template' }))

    await waitFor(() =>
      expect(mockApi.agentPublish).toHaveBeenCalledWith('atlas-crewA', 'crewA', 'my-template'),
    )
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith('my-template'))
  })
})

describe('switching the bound template from the header', () => {
  it('selects directly, with no confirm, when the current template is shared', async () => {
    const { onSelect } = renderPane({ crew: 'crewA' })
    await screen.findByRole('combobox', { name: FIELD_LABEL })
    await pickTemplate('reviewer')

    await waitFor(() => expect(onSelect).toHaveBeenCalledWith('reviewer'))
    expect(screen.queryByRole('button', { name: 'Discard changes and switch' })).not.toBeInTheDocument()
  })

  it('confirms first when switching away from a customized copy, and only then selects', async () => {
    mockApi.agentsInstalled.mockResolvedValue(OWN_COPY_INSTALLED)
    detailsByName({
      'atlas-crewA': { name: 'atlas-crewA', model: 'sonnet', skills: ['grill'] },
      atlas: { name: 'atlas', model: 'opus', skills: ['grill'] },
    })
    const { onSelect } = renderPane({ template: 'atlas-crewA', crew: 'crewA' })
    await screen.findByText('Customized')
    await pickTemplate('reviewer')

    // The confirm gates the switch: nothing selected until it is accepted.
    expect(onSelect).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes and switch' }))
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith('reviewer'))
  })
})

describe('blueprint semantics — fork on first edit', () => {
  it('forks first, then patches the NEW copy and rebinds the parent, when editing a shared template', async () => {
    mockApi.agentDetail.mockResolvedValue({ name: 'atlas', model: 'opus', skills: [] })
    mockApi.agentFork.mockResolvedValue({ template: 'atlas-crewA' })
    const { onForked } = renderPane({ crew: 'crewA' })

    await screen.findByRole('combobox', { name: 'Model' })
    await pickModel('sonnet')

    await waitFor(() => expect(mockApi.agentFork).toHaveBeenCalledWith('atlas', 'crewA'))
    // The edit lands on the copy the fork returned, never on the shared template.
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledWith('atlas-crewA', { model: 'sonnet' }))
    expect(onForked).toHaveBeenCalledWith('atlas-crewA')
    // Fork must precede the patch — patching the shared name first would mutate
    // the original before branching.
    expect(mockApi.agentFork.mock.invocationCallOrder[0])
      .toBeLessThan(mockApi.agentPatch.mock.invocationCallOrder[0])
    expect(mockApi.agentPatch).not.toHaveBeenCalledWith('atlas', { model: 'sonnet' })
  })

  it('patches the own copy directly, never forking again', async () => {
    mockApi.agentsInstalled.mockResolvedValue(OWN_COPY_INSTALLED)
    detailsByName({
      'atlas-crewA': { name: 'atlas-crewA', model: 'opus', skills: [] },
      atlas: { name: 'atlas', model: 'opus', skills: [] },
    })
    const { onForked } = renderPane({ template: 'atlas-crewA', crew: 'crewA' })

    await screen.findByRole('combobox', { name: 'Model' })
    await pickModel('sonnet')

    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledWith('atlas-crewA', { model: 'sonnet' }))
    expect(mockApi.agentFork).not.toHaveBeenCalled()
    expect(onForked).not.toHaveBeenCalled()
  })

  it('leaves legacy no-crew editing unchanged: patches the template with no fork', async () => {
    mockApi.agentDetail.mockResolvedValue({ name: 'atlas', model: 'opus', skills: [] })
    // No crew prop — the Agent Templates tab's direct-edit behavior.
    renderPane()

    await screen.findByRole('combobox', { name: 'Model' })
    await pickModel('sonnet')

    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledWith('atlas', { model: 'sonnet' }))
    expect(mockApi.agentFork).not.toHaveBeenCalled()
  })
})
