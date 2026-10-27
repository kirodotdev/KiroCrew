import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ExecutionsView from './ExecutionsView'

const { cronHistoryAll } = vi.hoisted(() => ({ cronHistoryAll: vi.fn() }))

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn().mockResolvedValue({ jobs: [{ id: 'j1' }] }),
    cronHistoryAll,
    cronRunDetail: vi.fn(),
  },
}))

/** Renders one history row with `durationMs` and returns its Duration cell text. */
async function durationCell(durationMs: number): Promise<string> {
  cronHistoryAll.mockResolvedValue({
    runs: [
      {
        job_id: 'j1',
        job_name: 'Job One',
        run_id: `r-${durationMs}`,
        started_at: 1_700_000_000_000,
        duration_ms: durationMs,
        status: 'success',
        trigger: 'schedule',
      },
    ],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <ExecutionsView />
    </QueryClientProvider>,
  )
  await screen.findByText('Job One')
  // Columns: status, job name, started at, duration.
  return document.querySelectorAll('td')[3]?.textContent ?? ''
}

describe('ExecutionsView Duration column', () => {
  it('rolls a run that rounds up to 60.0s over to 1m 0s', async () => {
    expect(await durationCell(59_960)).toBe('1m 0s')
  })

  it('keeps tenths for a run that still rounds below a minute', async () => {
    expect(await durationCell(59_940)).toBe('59.9s')
  })

  it('keeps minutes and whole seconds past a minute', async () => {
    expect(await durationCell(125_400)).toBe('2m 5s')
  })
})
