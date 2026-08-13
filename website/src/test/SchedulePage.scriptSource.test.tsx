import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

// The script-source panel in the job detail dialog: present (collapsed) for a
// job that carries a `script`, absent for message/command jobs, and lazy — the
// source endpoint is only hit on first expand.

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
    cronScript: vi.fn(),
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

describe('SchedulePage script source panel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders collapsed for a script job and fetches the source on expand', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ script: '/home/user/.kiro/crew/crons/monitor.py:run' })],
    })
    vi.mocked(api).cronScript.mockResolvedValue({
      source: 'def run(ctx):\n    ctx.notify("hello")\n',
      file: 'monitor.py',
      function: 'run',
      truncated: false,
    })

    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByText('Nightly report'))

    // Panel toggle is present but the source has NOT been fetched yet (lazy).
    const toggle = await screen.findByText('Script source')
    expect(api.cronScript).not.toHaveBeenCalled()

    fireEvent.click(toggle)
    await waitFor(() => expect(api.cronScript).toHaveBeenCalledWith('job-1'))
    await screen.findByText(/ctx\.notify\("hello"\)/)
    // Download action is exposed on the expanded source view.
    expect(screen.getByLabelText('Download script')).toBeTruthy()
  })

  it('does not render the panel for a job without a script', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })

    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByText('Nightly report'))

    // Detail dialog is open (its Pause action is visible) but no script panel.
    await screen.findByText('Pause')
    expect(screen.queryByText('Script source')).toBeNull()
    expect(api.cronScript).not.toHaveBeenCalled()
  })

  it('shows the truncation notice when the backend flags a capped read', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ script: '/home/user/.kiro/crew/crons/big.py:run' })],
    })
    vi.mocked(api).cronScript.mockResolvedValue({
      source: '# capped\n',
      file: 'big.py',
      function: 'run',
      truncated: true,
    })

    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByText('Nightly report'))
    fireEvent.click(await screen.findByText('Script source'))

    await screen.findByText(/Truncated/)
  })
})
