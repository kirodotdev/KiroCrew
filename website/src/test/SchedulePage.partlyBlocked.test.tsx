import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import { isPartlyBlocked } from '../utils/cronPartlyBlocked'
import type { CronJob } from '../types'

/**
 * A cron run that did its work but had SOME tool calls refused by the security
 * gate keeps `last_status: "error"` on purpose (it lost work, so it is not
 * `ok`), and names the loss in `last_error` as "N of M tool call(s) blocked by
 * the security gate: ...". The Schedule page reads that reason and shows the
 * run as its own state, "Partial", rather than the "Error" a run that died
 * shows. The label is short so it fits the Status column's default width in
 * every locale; the hover title says what the state means and what was lost.
 */

const PARTIAL = '1 of 54 tool call(s) blocked by the security gate: Write /vault/daily.md (Blocked by security policy: write outside workspace)'
const TOTAL = 'all 2 tool call(s) blocked by the security gate: Write /etc/example.conf, Read /etc/example.key'
const HINT = 'The run finished, but the security gate refused some of its tool calls.'

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
    cronToChat: vi.fn().mockResolvedValue({}),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
  },
}))

/** The row that carries the job's name. */
const rowOf = (name: string) => screen.getByText(name).closest('tr') as HTMLElement

describe('isPartlyBlocked', () => {
  it('reads a partial gate refusal on an agent job', () => {
    expect(isPartlyBlocked({ last_status: 'error', last_error: PARTIAL })).toBe(true)
  })

  it('does not read a total refusal, another error, or a clean run', () => {
    expect(isPartlyBlocked({ last_status: 'error', last_error: TOTAL })).toBe(false)
    expect(isPartlyBlocked({ last_status: 'error', last_error: 'Traceback: boom' })).toBe(false)
    expect(isPartlyBlocked({ last_status: 'error', last_error: null })).toBe(false)
    expect(isPartlyBlocked({ last_status: 'ok', last_error: PARTIAL })).toBe(false)
  })

  it('matches only at the start of the reason', () => {
    expect(isPartlyBlocked({ last_status: 'error', last_error: `stderr: ${PARTIAL}` })).toBe(false)
  })

  it('never reads a script or command job, which no tool gate runs on', () => {
    expect(isPartlyBlocked({ last_status: 'error', last_error: PARTIAL, script: 'job.py' })).toBe(false)
    expect(isPartlyBlocked({ last_status: 'error', last_error: PARTIAL, command: 'echo hi' })).toBe(false)
  })
})

describe('SchedulePage Status column, partly blocked run', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows "Partial", not "Error", for a run that lost one call', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Vault digest', last_status: 'error', last_error: PARTIAL })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Vault digest')).toBeInTheDocument())

    const row = rowOf('Vault digest')
    const badge = within(row).getByText('Partial')
    // On hover the badge says what the state means, then what was lost, so the
    // reader need not open the job.
    expect(badge).toHaveAttribute('title', `${HINT}\n${PARTIAL}`)
    expect(within(row).queryByText('Error')).not.toBeInTheDocument()
  })

  it('keeps "Error" for a run whose every call was blocked, and for other failures', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [
        mkJob({ id: 'a', name: 'Blocked job', last_status: 'error', last_error: TOTAL }),
        mkJob({ id: 'b', name: 'Crashed job', last_status: 'error', last_error: 'Traceback: boom' }),
        mkJob({ id: 'c', name: 'Script job', last_status: 'error', last_error: PARTIAL, script: 'job.py' }),
      ],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Blocked job')).toBeInTheDocument())

    for (const name of ['Blocked job', 'Crashed job', 'Script job']) {
      const row = rowOf(name)
      expect(within(row).getByText('Error')).toBeInTheDocument()
      expect(within(row).queryByText('Partial')).not.toBeInTheDocument()
    }
  })

  it('leaves a clean run and a paused job as they were', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [
        mkJob({ id: 'a', name: 'Clean job', last_status: 'ok' }),
        mkJob({ id: 'b', name: 'Paused job', enabled: false, last_status: 'error', last_error: PARTIAL }),
      ],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Clean job')).toBeInTheDocument())

    expect(within(rowOf('Clean job')).getByText('OK')).toBeInTheDocument()
    expect(within(rowOf('Paused job')).getByText('Paused')).toBeInTheDocument()
    expect(within(rowOf('Paused job')).queryByText('Partial')).not.toBeInTheDocument()
  })
})
