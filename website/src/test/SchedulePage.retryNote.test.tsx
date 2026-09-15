import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

/**
 * a job that succeeded only after transient retries showed nothing
 * about it in the Last run column -- the attempt counter lived on a runtime
 * attribute the gateway callback cleared once the retry chain unwound, never
 * reaching the wire. `last_retry_count` closes that gap; this
 * pins the small note the Last run cell renders from it.
 */

// One fixed stamp so a test can make `last_retry_run_ts` match the run it reports
// or deliberately name an earlier one.
const TS = 1_770_000_000

const mkJob = (overrides: Partial<CronJob> = {}): CronJob => ({
  id: 'job-1',
  name: 'Nightly report',
  schedule: 'every 1d',
  message: 'send report',
  enabled: true,
  ...overrides,
} as CronJob)

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn(),
    cronFolders: vi.fn().mockResolvedValue([]),
    deleteCron: vi.fn(),
    batchDeleteCron: vi.fn(),
    createCron: vi.fn().mockResolvedValue({}),
    models: vi.fn().mockResolvedValue([]),
    updateCron: vi.fn().mockResolvedValue({}),
    toggleCron: vi.fn().mockResolvedValue({}),
    runCron: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    syncKirocrewAgents: vi.fn().mockResolvedValue({}),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
  },
}))

describe('SchedulePage Last run column — retry note', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows a "retried N times" note when the last run needed retries', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Flaky job', last_run_ts: TS, last_retry_count: 2, last_retry_run_ts: TS })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Flaky job')).toBeInTheDocument())

    expect(screen.getByText('Retried 2 times')).toBeInTheDocument()
  })

  it('renders nothing extra for a clean run (count 0)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Clean job', last_run_ts: TS, last_retry_count: 0, last_retry_run_ts: TS })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Clean job')).toBeInTheDocument())

    expect(screen.queryByText(/Retried/)).not.toBeInTheDocument()
  })

  it('renders nothing extra when the field is absent (older gateway)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Old gateway job', last_run_ts: Date.now() / 1000 })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Old gateway job')).toBeInTheDocument())

    expect(screen.queryByText(/Retried/)).not.toBeInTheDocument()
  })

  it('hides the note when the count belongs to an EARLIER run', async () => {
    // A cancelled run advances `last_run_ts` (the `every` scheduler needs it to)
    // without overwriting the count, so the pair disagrees. A number attached to
    // the wrong run is worse than no number.
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Cancelled job', last_run_ts: TS, last_retry_count: 3, last_retry_run_ts: TS - 3600 })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Cancelled job')).toBeInTheDocument())

    expect(screen.queryByText(/Retried/)).not.toBeInTheDocument()
  })

  it('uses the singular form for exactly one retry', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Once job', last_run_ts: TS, last_retry_count: 1, last_retry_run_ts: TS })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Once job')).toBeInTheDocument())

    expect(screen.getByText('Retried 1 time')).toBeInTheDocument()
  })
})
