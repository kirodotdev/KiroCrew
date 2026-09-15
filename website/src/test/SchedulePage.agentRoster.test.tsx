/**
 * SchedulePage's own wiring of the roster failure (#5990).
 *
 * The sibling `JobForm.agentRoster` tests hand `rosterFailure` straight to the
 * form, so they pin the FORM's behaviour and say nothing about whether the page
 * ever builds that object. Mutation testing caught exactly that: replacing
 * SchedulePage's `rosterFailure = rosterError ? {...} : undefined` with a
 * constant `undefined` left every other test green while silently restoring the
 * original bug. This file closes that hole by driving the real page against a
 * failing `/api/agents`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

const job = {
  id: 'job-1', name: 'Nightly report', schedule: 'every 1d', message: 'send report', enabled: true,
} as CronJob

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn(),
    cronFolders: vi.fn().mockResolvedValue([]),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    models: vi.fn().mockResolvedValue([]),
    updateCron: vi.fn().mockResolvedValue({}),
    createCron: vi.fn().mockResolvedValue({}),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: 'kirocrew' }),
    syncKirocrewAgents: vi.fn().mockResolvedValue({}),
    kirocrewAgents: vi.fn(),
  },
}))

const ROSTER = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'built-in', source: 'kirocrew' },
  { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-kb', description: 'paging', source: 'package' },
]

/** Render the page, open the job's detail dialog, open its agent picker. */
async function openJobAgentPicker() {
  const { api } = await import('../api/client')
  vi.mocked(api).crons.mockResolvedValue({ jobs: [job] })
  renderWithProviders(<SchedulePage />)
  fireEvent.click(await screen.findByText('Nightly report', {}, { timeout: 5000 }))
  fireEvent.click(await screen.findByLabelText('Switch agent', {}, { timeout: 5000 }))
}

describe('SchedulePage roster failure wiring (#5990)', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([])
    vi.mocked(api).cronHistoryAll.mockResolvedValue({ runs: [] })
    vi.mocked(api).models.mockResolvedValue([])
    vi.mocked(api).defaultAgent.mockResolvedValue({ default_agent: 'kirocrew' })
    vi.mocked(api).syncKirocrewAgents.mockResolvedValue({})
    vi.mocked(api).kirocrewAgents.mockRejectedValue(new Error('gateway restarting'))
  })

  it('hands the picker a failure it can act on when /api/agents rejects', async () => {
    await openJobAgentPicker()

    await waitFor(() => {
      expect(screen.getByText("Couldn't load the agent list.")).toBeInTheDocument()
    })
    expect(screen.getByText('Retry')).toBeInTheDocument()
    // The trigger still reads the default agent, because that name comes from a
    // SEPARATE query that succeeded -- the exact shape of the report.
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('kirocrew')
    expect(screen.queryByText('No matches')).not.toBeInTheDocument()
  })

  it('recovers the roster through the page\u2019s own reload', async () => {
    const { api } = await import('../api/client')
    await openJobAgentPicker()
    await waitFor(() => expect(screen.getByText('Retry')).toBeInTheDocument())

    vi.mocked(api).kirocrewAgents.mockResolvedValue({ agents: ROSTER, default_agent: 'kirocrew' })
    fireEvent.click(screen.getByText('Retry'))

    await waitFor(() => expect(screen.getByText('oncall')).toBeInTheDocument())
    expect(screen.queryByText("Couldn't load the agent list.")).not.toBeInTheDocument()
  })

  it('recovers every roster consumer, not just this form', async () => {
    const { api } = await import('../api/client')
    const { store } = renderWithProviders(<SchedulePage />)
    vi.mocked(api).crons.mockResolvedValue({ jobs: [job] })
    fireEvent.click(await screen.findByText('Nightly report', {}, { timeout: 5000 }))
    fireEvent.click(await screen.findByLabelText('Switch agent', {}, { timeout: 5000 }))
    await waitFor(() => expect(screen.getByText('Retry')).toBeInTheDocument())

    const before = store.getState().dashboard.refreshTrigger
    vi.mocked(api).kirocrewAgents.mockResolvedValue({ agents: ROSTER, default_agent: 'kirocrew' })
    fireEvent.click(screen.getByText('Retry'))

    // `useAgents` state is per-instance and the app shell holds its own copy, so
    // a retry that refreshed only this page would leave the shell's roster (and
    // its agent-cycle shortcuts) on the empty one it failed with.
    await waitFor(() => expect(store.getState().dashboard.refreshTrigger).toBe(before + 1))
  })
})
