import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import AgentSelector from '../components/AgentSelector'
import type { KiroCrewAgent } from '../components/AgentSelector'
import type { CronJob } from '../types'

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn().mockResolvedValue({ models: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
  },
}))

/**
 * The folded execution catalog as `useAgents` hands it to the schedule form:
 * one row per name, each tagged with the namespace it came from. The template
 * `kiro-review` is bound to no crew, so it is reachable ONLY as a template.
 */
const catalog: KiroCrewAgent[] = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'built-in', source: 'kirocrew', selection_kind: 'member' },
  { name: 'radar', kiro_agent: 'kirocrew', workspace: 'radar', memory_store: 'radar', description: 'issue radar', source: 'kirocrew', selection_kind: 'member' },
  { name: 'kiro-review', kiro_agent: 'kiro-review', workspace: '', memory_store: '', description: 'reviews PRs', source: 'aim', selection_kind: 'template' },
  { name: 'kiro-lite', kiro_agent: 'kiro-lite', workspace: '', memory_store: '', description: '', source: 'kirocrew', selection_kind: 'template' },
]

function openList() {
  fireEvent.click(screen.getByLabelText('Switch agent'))
  return screen.getByRole('listbox')
}

describe('cron JobForm agent picker — crews and templates in two groups', () => {
  it('lists crewmates and agent templates as two labelled groups, every row still an option', () => {
    renderWithProviders(
      <JobForm agents={catalog} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />,
    )
    const listbox = openList()
    const crews = within(listbox).getByRole('group', { name: 'Crewmates' })
    const templates = within(listbox).getByRole('group', { name: 'Agent templates' })
    expect(within(crews).getAllByRole('option').map(o => o.textContent)).toEqual([
      expect.stringContaining('kirocrew'),
      expect.stringContaining('radar'),
    ])
    expect(within(templates).getAllByRole('option').map(o => o.textContent)).toEqual([
      expect.stringContaining('kiro-review'),
      expect.stringContaining('kiro-lite'),
    ])
    // The whole catalog is offered — grouping reorders, it never drops.
    expect(within(listbox).getAllByRole('option')).toHaveLength(catalog.length)
    // Both headers are drawn, since the roster holds both kinds.
    const headers = within(listbox).getAllByTestId('panel-section-header')
    expect(headers.map(h => h.textContent)).toEqual([
      expect.stringContaining('Crewmates'),
      expect.stringContaining('Agent templates'),
    ])
    // The template group says what a template pick means for the job.
    expect(within(templates).getByText(/default crewmate's workspace and memory/)).toBeInTheDocument()
  })

  it('picking a template stores its bare name, exactly as picking a crew does', () => {
    renderWithProviders(
      <JobForm agents={catalog} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />,
    )
    const listbox = openList()
    fireEvent.click(within(listbox).getByText('kiro-review'))
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('kiro-review')
  })

  it('a stored template name lights the template row and the crew keeps its default badge', () => {
    renderWithProviders(
      <JobForm
        job={{ id: 'j1', name: 'review', message: 'review', schedule: '', enabled: true, cron_expr: '0 3 * * *', agent: 'kiro-review' } as CronJob}
        agents={catalog}
        defaultAgent="kirocrew"
        onSaved={() => {}}
        layout="vertical"
      />,
    )
    const listbox = openList()
    const [selected] = within(listbox).getAllByRole('option', { selected: true })
    expect(selected).toHaveTextContent('kiro-review')
    // Exactly one default badge, on the crew that holds the default.
    expect(within(listbox).getAllByText('default')).toHaveLength(1)
  })

  it('a default that is only reachable as a template carries the default badge', () => {
    // The roster is folded one row per name, so the badge follows the name
    // wherever that one row landed — a template-only default is still the
    // default, and the picker says so.
    renderWithProviders(
      <JobForm agents={catalog} defaultAgent="kiro-review" onSaved={() => {}} layout="vertical" />,
    )
    const listbox = openList()
    const templates = within(listbox).getByRole('group', { name: 'Agent templates' })
    const opt = within(templates).getAllByRole('option').find(o => o.querySelector('.font-mono')?.textContent === 'kiro-review')!
    expect(opt).toBeDefined()
    expect(within(opt).getByText('default')).toBeInTheDocument()
    expect(within(listbox).getAllByText('default')).toHaveLength(1)
  })

  it('filtering to one kind keeps that group header rather than flattening the list', () => {
    renderWithProviders(
      <JobForm agents={catalog} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />,
    )
    const listbox = openList()
    fireEvent.change(screen.getByLabelText('Filter agents'), { target: { value: 'kiro-' } })
    expect(within(listbox).queryByRole('group', { name: 'Crewmates' })).toBeNull()
    const templates = within(listbox).getByRole('group', { name: 'Agent templates' })
    expect(within(templates).getAllByRole('option')).toHaveLength(2)
    expect(within(listbox).getAllByTestId('panel-section-header')).toHaveLength(1)
  })

  it('a roster of one kind draws no group chrome, and a name-only roster stays flat', () => {
    const membersOnly = catalog.filter(a => a.selection_kind === 'member')
    const { unmount } = renderWithProviders(
      <JobForm agents={membersOnly} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />,
    )
    let listbox = openList()
    // Still grouped for AT (one labelled group), but no header names a
    // distinction the list does not draw.
    expect(within(listbox).getByRole('group', { name: 'Crewmates' })).toBeInTheDocument()
    expect(within(listbox).queryAllByTestId('panel-section-header')).toHaveLength(0)
    unmount()

    const nameOnly = catalog.map(({ selection_kind: _k, ...rest }) => rest)
    renderWithProviders(
      <JobForm agents={nameOnly} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />,
    )
    listbox = openList()
    expect(within(listbox).queryAllByRole('group')).toHaveLength(0)
    expect(within(listbox).getAllByRole('option')).toHaveLength(catalog.length)
  })

  it('a selector that did not opt in renders the same catalog flat', () => {
    renderWithProviders(
      <AgentSelector agents={catalog} defaultAgent="kirocrew" value="" onChange={() => {}} />,
    )
    const listbox = openList()
    expect(within(listbox).queryAllByRole('group')).toHaveLength(0)
    expect(within(listbox).getAllByRole('option')).toHaveLength(catalog.length)
  })
})
