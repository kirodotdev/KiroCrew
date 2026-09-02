import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

/**
 * The Schedule cell shows a COMPACT label derived from `cron_expr` and keeps the
 * verbose backend `schedule` in its tooltip. Three things are load-bearing:
 *
 * 1. The cell renders the compact label; the verbose prose reaches `title` only.
 * 2. A non-cron job (interval, one-shot) has no `cron_expr` and keeps its
 *    backend string, so those rows never render blank.
 * 3. The timezone keeps its own line — the compact label carries no zone
 *    abbreviation, so this is the only place the zone appears in the row.
 */

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
  },
}))

const VERBOSE = 'At 12:00 AM, on day 30 of the month, only in February'

describe('SchedulePage schedule cell', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the compact label and keeps the verbose form in the tooltip', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Feb job', schedule: VERBOSE, cron_expr: '0 0 30 2 *' })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Feb job')).toBeInTheDocument())

    const cell = screen.getByTitle(VERBOSE)
    expect(cell.tagName).toBe('TD')
    expect(cell.textContent).toContain('12:00 AM')
    expect(cell.textContent).toContain('Feb 30')
    // The qualifier the verbose form buries past the truncation point must NOT
    // be what the cell paints -- otherwise the tooltip is still the only copy.
    expect(cell.textContent).not.toContain('only in February')
  })

  it('keeps the backend string for a job with no cron expression', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Interval job', schedule: 'every 600s' })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Interval job')).toBeInTheDocument())

    // Never a blank Schedule column for an interval or one-shot job.
    expect(screen.getByTitle('every 600s').textContent).toContain('every 600s')
  })

  it('keeps the timezone on its own line beneath the label', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({
        name: 'Tz job',
        schedule: VERBOSE,
        cron_expr: '0 0 30 2 *',
        timezone: 'America/New_York',
      })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Tz job')).toBeInTheDocument())

    // Underscores are spaced for reading; the zone is the label's only home in
    // the row, since the compact form drops the abbreviation.
    const zone = screen.getByText('America/New York')
    expect(zone.className).toContain('block')
    expect(zone.closest('td')).toBe(screen.getByTitle(VERBOSE))
  })
})
