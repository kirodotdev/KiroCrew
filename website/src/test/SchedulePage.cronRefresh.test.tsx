import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { act } from 'react'
import { createTestStore, renderWithProviders } from './helpers'
import { triggerCronRefresh, triggerRefresh } from '../store/dashboardSlice'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

// The Schedule page re-reads its job list when the cron caches go stale, not on
// every refresh frame: a turn ending on any session pushes one, and each used
// to re-read the whole list.

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn(),
    cronFolders: vi.fn().mockResolvedValue([]),
    models: vi.fn().mockResolvedValue([]),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: 'kirocrew', default_template: 'kirocrew' }),
    members: vi.fn().mockResolvedValue({ members: [] }),
  },
}))

const JOB = { id: 'job-1', name: 'Nightly report', schedule: 'every 1d', message: 'send report', enabled: true } as CronJob

describe('Schedule page reload', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('does not re-read the job list on a refresh that leaves the cron caches alone', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [JOB] })
    const store = createTestStore()
    renderWithProviders(<SchedulePage />, { store })
    await screen.findByText('Nightly report')
    await waitFor(() => expect(api.crons).toHaveBeenCalled())
    const reads = vi.mocked(api).crons.mock.calls.length

    // `act` flushes the effects, and `load()` calls `api.crons()` before its
    // first await, so a reload this refresh started has already been counted.
    act(() => { store.dispatch(triggerRefresh()) })
    expect(vi.mocked(api).crons.mock.calls.length).toBe(reads)
    // The same path does count a cron refresh, so the check above could see one.
    act(() => { store.dispatch(triggerCronRefresh()) })
    expect(vi.mocked(api).crons.mock.calls.length).toBe(reads + 1)
  })

  it('re-reads the job list when the cron caches go stale', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [JOB] })
    const store = createTestStore()
    renderWithProviders(<SchedulePage />, { store })
    await screen.findByText('Nightly report')

    vi.mocked(api).crons.mockResolvedValue({ jobs: [{ ...JOB, name: 'Renamed report' }] })
    act(() => { store.dispatch(triggerCronRefresh()) })
    expect(await screen.findByText('Renamed report')).toBeInTheDocument()
  })
})
