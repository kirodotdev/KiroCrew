import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import JobLogsView from '../components/JobLogsView'
import { runWaitingUntil } from '../utils/cronRunWait'
import { fmtTime } from '../i18n/format'
import type { CronJob } from '../types'

/**
 * A recurring job that is not on a strict schedule starts a random time after
 * its schedule. For that wait the gateway already holds the run, so the job
 * arrives with `is_running: true`, and nothing has run. The gateway also sends
 * `waiting_until`, and the Schedule page shows the job as waiting, with the
 * time it starts, instead of as running. Cancel stays, since the held run can
 * still be cancelled.
 */

const STARTS_AT = 1_770_001_500
const WAITING = `Waiting, starts by ${fmtTime(STARTS_AT)}`
const HINT = 'This run has not started yet. A repeating job starts at a random time after its scheduled time, so that jobs do not all start at once.'

const mkJob = (overrides: Partial<CronJob> = {}): CronJob => ({
  id: 'job-1',
  name: 'Nightly report',
  schedule: 'every 1d',
  message: 'send report',
  enabled: true,
  last_run_ts: 1_770_000_000,
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
    cancelCron: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    cronHistory: vi.fn().mockResolvedValue({ runs: [], total: 0 }),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
  },
}))

/** The row that carries the job's name. */
const rowOf = (name: string) => screen.getByText(name).closest('tr') as HTMLElement

describe('runWaitingUntil', () => {
  it('reads the start time of a held run that has not started', () => {
    expect(runWaitingUntil({ is_running: true, waiting_until: STARTS_AT })).toBe(STARTS_AT)
  })

  it('reads nothing for a run that is executing, an idle job, or an older gateway', () => {
    expect(runWaitingUntil({ is_running: true, waiting_until: null })).toBeNull()
    expect(runWaitingUntil({ is_running: true })).toBeNull()
    expect(runWaitingUntil({ is_running: false, waiting_until: STARTS_AT })).toBeNull()
    expect(runWaitingUntil({ waiting_until: STARTS_AT })).toBeNull()
  })
})

describe('SchedulePage Status column, a run waiting to start', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows "Waiting, starts by <time>", not "Running", and keeps Cancel', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Daily digest', is_running: true, running_since: 1_770_000_000, waiting_until: STARTS_AT })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Daily digest')).toBeInTheDocument())

    const row = rowOf('Daily digest')
    expect(within(row).getByText(WAITING)).toHaveAttribute('title', HINT)
    expect(within(row).queryByText('Running')).not.toBeInTheDocument()
    const cancel = within(row).getByRole('button', { name: 'Cancel' })
    expect(cancel.closest('[title]')).toHaveAttribute('title', 'Cancel this run before it starts')
  })

  it('keeps "Running" for a run that is executing', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Daily digest', is_running: true, running_since: 1_770_000_000, waiting_until: null })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Daily digest')).toBeInTheDocument())

    const row = rowOf('Daily digest')
    expect(within(row).getByText('Running')).toBeInTheDocument()
    expect(within(row).queryByTestId('cron-waiting-badge')).not.toBeInTheDocument()
    const cancel = within(row).getByRole('button', { name: 'Cancel' })
    expect(cancel.closest('[title]')).toHaveAttribute('title', 'Cancel running execution')
  })
})

describe('JobLogsView banner, a run waiting to start', () => {
  beforeEach(() => vi.clearAllMocks())

  it('says the run is waiting and when it starts, with Cancel and no elapsed time', async () => {
    renderWithProviders(
      <JobLogsView jobId="job-1" isRunning runningSince={1_770_000_000} waitingUntil={STARTS_AT} onCancel={() => {}} />,
    )

    expect(await screen.findByText(WAITING)).toBeInTheDocument()
    expect(screen.queryByText('Currently running…')).not.toBeInTheDocument()
    expect(screen.queryByText(/s elapsed/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveAttribute('title', 'Cancel this run before it starts')
  })

  it('keeps the running banner for a run that is executing', async () => {
    renderWithProviders(
      <JobLogsView jobId="job-1" isRunning runningSince={1_770_000_000} waitingUntil={null} onCancel={() => {}} />,
    )

    expect(await screen.findByText('Currently running…')).toBeInTheDocument()
    expect(screen.queryByTestId('job-logs-waiting')).not.toBeInTheDocument()
  })
})
