import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, within, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import AgentSelector from '../components/AgentSelector'
import type { KiroCrewAgent } from '../components/AgentSelector'
import type { CronJob } from '../types'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn().mockResolvedValue({ models: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
  },
}))

/**
 * The job form's "Runs as" switch (#18772). A job either opens a new session on
 * a custom agent (a template) or is a crewmate's own schedule, so the form asks
 * which, and the picker under it lists only that kind.
 *
 * The roster is the folded execution catalog as `useAgents` hands it to the
 * Schedule page: one row per name, each tagged with its namespace.
 */
const catalog: KiroCrewAgent[] = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'built-in', source: 'kirocrew', selection_kind: 'member' },
  { name: 'radar', kiro_agent: 'kirocrew', workspace: 'radar', memory_store: 'radar', description: 'issue radar', source: 'kirocrew', selection_kind: 'member' },
  { name: 'scout', kiro_agent: 'kirocrew', workspace: 'scout', memory_store: 'scout', description: 'scouts issues', source: 'kirocrew', selection_kind: 'member' },
  { name: 'kiro-review', kiro_agent: 'kiro-review', workspace: '', memory_store: '', description: 'reviews PRs', source: 'aim', selection_kind: 'template' },
  { name: 'kiro-lite', kiro_agent: 'kiro-lite', workspace: '', memory_store: '', description: '', source: 'kirocrew', selection_kind: 'template' },
]

const job = (agent: string) =>
  ({ id: 'j1', name: 'review', message: 'review', schedule: '', enabled: true, cron_expr: '0 3 * * *', agent }) as CronJob

function optionNames() {
  fireEvent.click(screen.getByLabelText('Switch agent'))
  const names = within(screen.getByRole('listbox')).getAllByRole('option')
    .map(o => o.querySelector('.font-mono')?.textContent)
  fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' })
  return names
}

const segment = (name: string) => screen.getByRole('radio', { name })

beforeEach(() => {
  vi.mocked(api.createCron).mockReset()
})

describe('cron JobForm "Runs as" switch', () => {
  it('opens on Custom agent and lists only custom agents', () => {
    renderWithProviders(<JobForm agents={catalog} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />)
    expect(screen.getByRole('radiogroup', { name: 'Runs as' })).toBeInTheDocument()
    expect(segment('Custom agent')).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText('Runs in a chat of its own, with shared memory.')).toBeInTheDocument()
    expect(optionNames()).toEqual(['kiro-review', 'kiro-lite'])
  })

  it('Mate lists only mates, names the pick, and sends it as the agent', async () => {
    vi.mocked(api.createCron).mockResolvedValue({})
    const onSaved = vi.fn()
    renderWithProviders(<JobForm agents={catalog} defaultAgent="kirocrew" onSaved={onSaved} layout="vertical" />)
    fireEvent.click(segment('Mate'))
    expect(segment('Mate')).toHaveAttribute('aria-checked', 'true')
    // The stock `kirocrew` crew shares memory, so it never becomes a crewmate job.
    expect(optionNames()).toEqual(['radar', 'scout'])
    fireEvent.click(screen.getByLabelText('Switch agent'))
    fireEvent.click(within(screen.getByRole('listbox')).getByText('radar'))
    expect(screen.getByText(/Runs in radar's chat, with radar's own memory/)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'triage' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'triage issues' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.createCron).toHaveBeenCalledWith(expect.objectContaining({ agent: 'radar' }))
  })

  it('switching back to Custom agent returns to the default custom agent', async () => {
    vi.mocked(api.createCron).mockResolvedValue({})
    const onSaved = vi.fn()
    renderWithProviders(<JobForm agents={catalog} defaultAgent="kirocrew" onSaved={onSaved} layout="vertical" />)
    fireEvent.click(segment('Mate'))
    fireEvent.click(segment('Custom agent'))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'daily' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'run' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.createCron).toHaveBeenCalledWith(expect.objectContaining({ agent: '' }))
  })

  it('holds a template job on Custom agent when editing, and says why in text', () => {
    renderWithProviders(<JobForm job={job('kiro-review')} agents={catalog} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />)
    expect(segment('Custom agent')).toHaveAttribute('aria-checked', 'true')
    expect(segment('Mate')).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText("A job can't switch between custom agent and mate after it's created.")).toBeVisible()
    expect(optionNames()).toEqual(['kiro-review', 'kiro-lite'])
  })

  it('shows a mate job as its pinned mate, not the switch', async () => {
    // The create endpoint stores a crewmate pick as `member_id` and empties
    // `agent`, so the kind comes from `member_id`.
    const crewmateJob = { ...job(''), member_id: 'radar' } as CronJob
    renderWithProviders(<JobForm job={crewmateJob} agents={catalog} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />)
    expect(screen.queryByRole('radiogroup', { name: 'Runs as' })).toBeNull()
    expect(screen.queryByText(/Runs in a chat of its own/)).toBeNull()
    expect(screen.getByText('radar')).toBeInTheDocument()
    await waitFor(() => expect(api.models).toHaveBeenCalled())
  })

  it('clicking the option already chosen keeps the pick', async () => {
    vi.mocked(api.updateCron).mockResolvedValue({})
    const onSaved = vi.fn()
    renderWithProviders(<JobForm job={job('kiro-review')} agents={catalog} defaultAgent="kirocrew" onSaved={onSaved} layout="vertical" />)
    fireEvent.click(segment('Custom agent'))
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('kiro-review')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith('j1', expect.objectContaining({ agent: 'kiro-review' }))
  })

  it('clicking Mate again keeps the mate picked', () => {
    renderWithProviders(<JobForm agents={catalog} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />)
    fireEvent.click(segment('Mate'))
    fireEvent.click(screen.getByLabelText('Switch agent'))
    fireEvent.click(within(screen.getByRole('listbox')).getByText('scout'))
    fireEvent.click(segment('Mate'))
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('scout')
  })

  it('an older job naming a mate in agent, with no member_id, edits as Custom agent', async () => {
    // Such a job runs as a template, never in the crewmate's chat, so the
    // crewmate hint would be false for it; its stored agent is kept.
    renderWithProviders(<JobForm job={job('radar')} agents={catalog} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />)
    expect(segment('Custom agent')).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByText(/with radar's own memory/)).toBeNull()
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('radar')
    // Its own row stays in the list, so picking another agent can be undone.
    expect(optionNames()).toEqual(['kiro-review', 'kiro-lite', 'radar'])
    await waitFor(() => expect(api.models).toHaveBeenCalled())
  })

  it('greys out Mate when there are no mates', async () => {
    const templatesOnly = catalog.filter(a => a.selection_kind === 'template')
    renderWithProviders(<JobForm agents={templatesOnly} defaultAgent="" onSaved={() => {}} layout="vertical" />)
    expect(await screen.findByRole('radio', { name: 'Mate' })).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText('No mates yet. Add one on the Mates page.')).toBeVisible()
    // Barrier: let the form's mount-time reads settle inside the test.
    await waitFor(() => expect(api.models).toHaveBeenCalled())
  })

  it('a roster whose only mate is the shared-memory default crew greys out Mate', async () => {
    // `default` has no member_id, so the create endpoint would save it as a
    // custom-agent job; offering it under Crewmate would make the hint false.
    const defaultOnly = catalog.filter(a => a.name === 'kirocrew' || a.selection_kind === 'template')
    renderWithProviders(<JobForm agents={defaultOnly} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />)
    expect(await screen.findByRole('radio', { name: 'Mate' })).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText('No mates yet. Add one on the Mates page.')).toBeVisible()
    await waitFor(() => expect(api.models).toHaveBeenCalled())
  })

  it('a name-only roster has no switch and lists every row flat', () => {
    const nameOnly = catalog.map(({ selection_kind: _k, ...rest }) => rest)
    renderWithProviders(<JobForm agents={nameOnly} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />)
    expect(screen.queryByRole('radiogroup', { name: 'Runs as' })).toBeNull()
    fireEvent.click(screen.getByLabelText('Switch agent'))
    const listbox = screen.getByRole('listbox')
    expect(within(listbox).queryAllByRole('group')).toHaveLength(0)
    expect(within(listbox).getAllByRole('option')).toHaveLength(catalog.length)
  })

  it('a selector that did not opt in renders the same catalog flat', () => {
    renderWithProviders(
      <AgentSelector agents={catalog} defaultAgent="kirocrew" value="" onChange={() => {}} />,
    )
    fireEvent.click(screen.getByLabelText('Switch agent'))
    const listbox = screen.getByRole('listbox')
    expect(within(listbox).queryAllByRole('group')).toHaveLength(0)
    expect(within(listbox).getAllByRole('option')).toHaveLength(catalog.length)
  })
})
