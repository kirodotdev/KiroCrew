import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

/**
 * A refused row action (#34557). The error used to render inside the sticky,
 * nowrap Actions cell, which sizes to its buttons, so "job is already running"
 * broke every few characters. It now gets its own full-width row under the job,
 * the same as a folder error. Mock conventions follow
 * `SchedulePage.rowRunBusy.test.tsx`.
 */

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
    runCron: vi.fn(),
    cancelCron: vi.fn(),
    cronToChat: vi.fn().mockResolvedValue({}),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
  },
}))

describe('SchedulePage row action error', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders a refused Run in its own full-width row, not in the Actions cell', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })
    vi.mocked(api).runCron.mockResolvedValue({ error: 'job is already running' } as never)

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())
    const jobRow = screen.getByText('Nightly report').closest('tr') as HTMLElement
    await userEvent.click(within(jobRow).getByRole('button', { name: 'Run' }))

    const notice = await screen.findByTestId('schedule-job-action-error')
    expect(notice).toHaveTextContent('job is already running')
    expect(jobRow).not.toContainElement(notice)
    const cell = notice.closest('td') as HTMLTableCellElement
    expect(cell).toHaveAttribute('colspan', '10')
    expect(cell.closest('tr')?.previousElementSibling).toBe(jobRow)
    // TableCell is nowrap; without this a long message runs past the table edge.
    expect(notice).toHaveClass('whitespace-normal')
  })
})
