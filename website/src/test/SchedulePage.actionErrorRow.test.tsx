import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

/**
 * A row action's error (#34557) renders on its own full-width row under the job,
 * not inside the pinned Actions cell, where it was squeezed to the width of the
 * controls and broke every few characters.
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
    runCron: vi.fn().mockResolvedValue({ error: 'job is already running' }),
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

  it('renders on its own full-width row, not in the Actions cell', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    const jobRow = screen.getByText('Nightly report').closest('tr')
    const runButton = Array.from(jobRow?.querySelectorAll('button') ?? []).find(b => b.textContent === 'Run')
    expect(runButton).toBeTruthy()
    fireEvent.click(runButton!)

    const notice = await screen.findByTestId('schedule-job-action-error')
    expect(notice).toHaveTextContent('job is already running')
    const cell = notice.closest('td')
    // The pinned Actions cell is one column; the error row spans the table.
    expect(cell?.className ?? '').not.toContain('sticky')
    expect(Number(cell?.getAttribute('colspan'))).toBeGreaterThan(1)
    expect(notice.closest('tr')).not.toBe(jobRow)
    // Pinned to the visible left edge, so a sideways-scrolled table still shows it.
    const pin = screen.getByTestId('schedule-job-action-error-pin')
    expect(pin.className).toContain('sticky')
    // TableCell is whitespace-nowrap; a long error must still wrap inside the pin,
    // at the width of the table's scroller (a container), not of the window.
    expect(pin.className).toContain('whitespace-normal')
    expect(pin.className).toContain('max-w-[calc(100cqw-2rem)]')
    expect(pin.closest('.\\@container')).toBeTruthy()
  })
})
