import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

/**
 * UX Review: the Last Error panel was gated on `job.script`, so a MESSAGE job's
 * `last_error` rendered only as the status cell's `title` attribute -- a hover
 * tooltip, which is no signal at all in the desktop app.
 *
 * That field is exactly where a project-bound job's skip reason lands (an agent
 * that no longer resolves, a bound directory that vanished, a project file
 * shadowing a Crew Member), and those skips deliberately spend no auto-pause
 * strike, so nothing else escalates -- the text IS the whole signal. These pin
 * that it is rendered, for a message job as well as a script one.
 */

const mkJob = (overrides: Partial<CronJob> = {}): CronJob =>
  ({
    id: 'job-1',
    name: 'Nightly report',
    schedule: 'every 1d',
    message: 'send report',
    enabled: true,
    ...overrides,
  }) as CronJob

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
    // `useAgents` calls `agentCatalog`, and the job form's folder picker calls
    // `chatFolders`. Neither is this file's subject, but both run when the page
    // renders -- an unstubbed `agentCatalog` throws "not a function" inside a
    // passive effect, which fails every test in the file before its own
    // assertion runs.
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    chatFolders: vi.fn().mockResolvedValue([]),
    syncKirocrewAgents: vi.fn().mockResolvedValue({}),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
  },
}))

const SKIP_REASON =
  "This job is bound to a Crew Member, and its project directory defines an " +
  "agent with the same name ('default'). The run was skipped instead of letting " +
  "that agent run with the member's own memory. Rename the project's agent or " +
  "unbind the member."

describe('SchedulePage — Last Error visibility', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it("renders a message job's skip reason as visible text, not only a tooltip", async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ last_status: 'error', last_error: SKIP_REASON })],
    })

    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByText('Nightly report'))

    await waitFor(() =>
      expect(screen.getByTestId('schedule-job-last-error')).toBeInTheDocument(),
    )
    // The remedy sentence itself must be on screen -- a panel that renders but
    // truncates the reason away would leave the operator no better off.
    expect(screen.getByTestId('schedule-job-last-error').textContent).toContain(
      'Rename the project',
    )
  })

  it('still renders a script job\'s captured output with log styling', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [
        mkJob({
          script: '~/.kiro/crew/crons/x.py:run',
          last_status: 'error',
          last_error: 'Traceback\n  line 2\nValueError',
        }),
      ],
    })

    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByText('Nightly report'))

    const panel = await screen.findByTestId('schedule-job-last-error')
    expect(panel.className).toContain('font-mono')
  })

  it('renders no Last Error panel when the job has no error', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob({ last_status: 'ok' })] })

    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByText('Nightly report'))

    // The drawer's own Last run row proves it opened, without matching the job
    // name (which the drawer renders a second time).
    await waitFor(() => expect(screen.getAllByText('Nightly report').length).toBeGreaterThan(1))
    expect(screen.queryByTestId('schedule-job-last-error')).toBeNull()
  })
})
