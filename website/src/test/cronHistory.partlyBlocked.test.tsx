import { describe, it, expect, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ExecutionsView from '../components/ExecutionsView'
import LogEntry from '../components/LogEntry'

// #24616: history rows read a partly blocked run's `error`, not its summary,
// which for a run that wrote a result is that result, refusal banner first.

const PARTIAL = '1 of 54 tool call(s) blocked by the security gate: Write /vault/daily.md (Blocked by security policy: write outside workspace)'
const TOTAL = 'all 2 tool call(s) blocked by the security gate: Write /etc/example.conf, Read /etc/example.key'
const HINT = 'The run finished, but the security gate refused some of its tool calls.'

const { crons, cronHistoryAll } = vi.hoisted(() => ({ crons: vi.fn(), cronHistoryAll: vi.fn() }))
vi.mock('../api/client', () => ({ api: { crons, cronHistoryAll, cronRunDetail: vi.fn() } }))

const run = (job_id: string, job_name: string, error: string, summary = error) => ({
  job_id, job_name, run_id: `r-${job_id}`, started_at: 1_790_000_000, duration_ms: 1200,
  status: 'failure' as const, trigger: 'scheduled' as const, summary, error,
})

describe('cron history views, partly blocked run', () => {
  it('Executions tab shows "Partial" for a run that lost some calls, "Error" for a total block', async () => {
    crons.mockResolvedValue({ jobs: [{ id: 'a', name: 'Vault digest' }, { id: 'b', name: 'Blocked job' }] })
    cronHistoryAll.mockResolvedValue({
      runs: [
        run('a', 'Vault digest', PARTIAL, `⛔ ${PARTIAL} — that work did not happen.\n\nDigest written.`),
        run('b', 'Blocked job', TOTAL),
      ],
    })

    renderWithProviders(<ExecutionsView />)
    const rowOf = async (name: string) => (await screen.findByRole('button', { name })).closest('tr') as HTMLElement

    expect(within(await rowOf('Vault digest')).getByText('Partial')).toHaveAttribute('title', `${HINT}\n${PARTIAL}`)
    const blocked = await rowOf('Blocked job')
    expect(within(blocked).getByText('Error')).toBeInTheDocument()
    expect(within(blocked).queryByText('Partial')).not.toBeInTheDocument()
  })

  it('Logs tab shows "Partial" for the same run', () => {
    renderWithProviders(<LogEntry entry={run('a', 'Vault digest', PARTIAL)} jobId="a" />)
    expect(screen.getByText('Partial')).toHaveAttribute('title', `${HINT}\n${PARTIAL}`)
    expect(screen.queryByText('Error')).not.toBeInTheDocument()
  })
})
