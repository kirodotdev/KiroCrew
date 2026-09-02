import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
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
 * A roster with more than one entry, and whose DEFAULT is not first — a
 * default-only regression and an accidental `[agents[0]]` truncation are
 * different bugs, and this shape fails on either.
 */
const roster: KiroCrewAgent[] = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'built-in', source: 'kirocrew' },
  { name: 'gpu-research', kiro_agent: 'gpu-research', workspace: 'research', memory_store: 'research', description: 'internal research', source: 'package' },
  { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-kb', description: 'paging', source: 'package' },
  { name: 'wiki', kiro_agent: 'gpu-wiki', workspace: 'wiki', memory_store: 'wiki', description: 'wiki edits', source: 'package' },
]

function messageJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'j1', name: 'nightly', message: 'do the thing', schedule: '', enabled: true,
    cron_expr: '0 3 * * *', ...overrides,
  } as CronJob
}

/** Open the selector and return the option names it offers. */
function openRosterOptions(): string[] {
  fireEvent.click(screen.getByLabelText('Switch agent'))
  const listbox = screen.getByRole('listbox')
  return within(listbox).getAllByRole('option').map(o => o.textContent || '')
}

describe('cron JobForm agent selector roster (#5990)', () => {
  it('offers the WHOLE roster in the side-panel (vertical) edit form, not just the default', () => {
    renderWithProviders(
      <JobForm job={messageJob()} agents={roster} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />,
    )
    const options = openRosterOptions()
    expect(options).toHaveLength(roster.length)
    for (const a of roster) {
      expect(options.some(text => text.includes(a.name))).toBe(true)
    }
  })

  it('offers the WHOLE roster in the inline (horizontal) create form', () => {
    renderWithProviders(
      <JobForm agents={roster} defaultAgent="kirocrew" onSaved={() => {}} layout="horizontal" />,
    )
    const options = openRosterOptions()
    expect(options).toHaveLength(roster.length)
    for (const a of roster) {
      expect(options.some(text => text.includes(a.name))).toBe(true)
    }
  })

  it('honours a stored NON-default agent and still offers the rest of the roster', () => {
    renderWithProviders(
      <JobForm job={messageJob({ agent: 'oncall' })} agents={roster} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />,
    )
    const trigger = screen.getByLabelText('Switch agent')
    // The job's stored agent survives into the form — a default-only regression
    // would show the default here even when the job names another agent.
    expect(trigger).toHaveTextContent('oncall')

    fireEvent.click(trigger)
    const listbox = screen.getByRole('listbox')
    // Exactly one "default" badge, and it marks the default rather than the
    // stored selection.
    expect(within(listbox).getAllByText('default')).toHaveLength(1)
    // Switching AWAY to a third agent is offered.
    expect(within(listbox).getByText('gpu-research')).toBeInTheDocument()
  })
})

/**
 * The state the report was actually describing: the roster fetch failed, so the
 * list is empty while the trigger still shows the default agent (SchedulePage
 * reads `defaultAgent` from a SEPARATE query that succeeded). The form used to
 * render that as the italic "No matches" — the same thing it says when you
 * filter for a name nobody has — so a failed load was indistinguishable from an
 * install with one agent, with no way to retry short of remounting the page.
 */
describe('cron JobForm agent selector roster failure (#5990)', () => {
  it('says the roster failed to load, rather than claiming there are no matches', () => {
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="kirocrew" rosterFailure={{ reloading: false, onReload: () => {} }} onSaved={() => {}} layout="vertical" />,
    )
    fireEvent.click(screen.getByLabelText('Switch agent'))

    expect(screen.getByText("Couldn't load the agent list.")).toBeInTheDocument()
    // The misleading empty-state must be gone, not merely accompanied.
    expect(screen.queryByText('No matches')).not.toBeInTheDocument()
  })

  it('offers a retry that re-runs the roster fetch', () => {
    const onReloadRoster = vi.fn()
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="kirocrew" rosterFailure={{ reloading: false, onReload: onReloadRoster }} onSaved={() => {}} layout="vertical" />,
    )
    fireEvent.click(screen.getByLabelText('Switch agent'))

    fireEvent.click(screen.getByText('Retry'))
    expect(onReloadRoster).toHaveBeenCalledTimes(1)
  })

  it('reports the failure on the inline create form too', () => {
    renderWithProviders(
      <JobForm agents={[]} defaultAgent="kirocrew" rosterFailure={{ reloading: false, onReload: () => {} }} onSaved={() => {}} layout="horizontal" />,
    )
    fireEvent.click(screen.getByLabelText('Switch agent'))

    expect(screen.getByText("Couldn't load the agent list.")).toBeInTheDocument()
  })

  it('keeps a roster it still holds usable when a later refresh failed', () => {
    renderWithProviders(
      <JobForm job={messageJob()} agents={roster} defaultAgent="kirocrew" rosterFailure={{ reloading: false, onReload: () => {} }} onSaved={() => {}} layout="vertical" />,
    )
    const options = openRosterOptions()

    // A stale error must not replace a list the user can act on.
    expect(options).toHaveLength(roster.length)
    expect(screen.queryByText("Couldn't load the agent list.")).not.toBeInTheDocument()
  })

  it('still says "No matches" when a filter — not a failure — empties the list', () => {
    renderWithProviders(
      <JobForm job={messageJob()} agents={roster} defaultAgent="kirocrew" onSaved={() => {}} layout="vertical" />,
    )
    fireEvent.click(screen.getByLabelText('Switch agent'))
    fireEvent.change(screen.getByLabelText('Filter agents'), { target: { value: 'nobody' } })

    expect(screen.getByText('No matches')).toBeInTheDocument()
    expect(screen.queryByText("Couldn't load the agent list.")).not.toBeInTheDocument()
  })

  it('shows the retry as in flight, so a retry that fails again is not silent', () => {
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="kirocrew" rosterFailure={{ reloading: true, onReload: () => {} }} onSaved={() => {}} layout="vertical" />,
    )
    fireEvent.click(screen.getByLabelText('Switch agent'))

    const btn = screen.getByRole('button', { name: 'Retrying…' })
    expect(btn).toBeDisabled()
    // A second press must not queue another fetch behind the one in flight.
    expect(screen.queryByText('Retry')).not.toBeInTheDocument()
  })

  it('withholds the filter box while the roster is unavailable', () => {
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="kirocrew" rosterFailure={{ reloading: false, onReload: () => {} }} onSaved={() => {}} layout="vertical" />,
    )
    fireEvent.click(screen.getByLabelText('Switch agent'))

    // Nothing to filter — the control is absent rather than merely inert.
    expect(screen.queryByLabelText('Filter agents')).not.toBeInTheDocument()
  })

  it('keeps the filter box when a roster is present despite a failed refresh', () => {
    renderWithProviders(
      <JobForm job={messageJob()} agents={roster} defaultAgent="kirocrew" rosterFailure={{ reloading: false, onReload: () => {} }} onSaved={() => {}} layout="vertical" />,
    )
    fireEvent.click(screen.getByLabelText('Switch agent'))

    expect(screen.getByLabelText('Filter agents')).toBeInTheDocument()
  })
})
