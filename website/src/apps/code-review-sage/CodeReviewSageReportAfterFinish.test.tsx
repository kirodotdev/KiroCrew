// A review that finishes while it is open shows its report.
//
// The report is polled only while the run list says the run is running, and the
// dashboard's client never marks a query stale on its own. The last poll can land
// before the report is written, so the run's status change is what fetches the
// finished report; without it the pane would keep the "not ready" answer.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SageProvider, useSage } from './context'
import { sageApi } from './api'

vi.mock('./api', () => ({
  sageApi: {
    runs: vi.fn(),
    runReport: vi.fn(),
    settings: vi.fn(),
    pinnedRepos: vi.fn(),
    recentRepos: vi.fn(),
    repoPrs: vi.fn(),
    myRepos: vi.fn(),
    learnings: vi.fn(),
  },
}))

// No interval poll fires during the test, so the status change is the only thing
// that can refetch the report.
vi.mock('./lib/layout', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./lib/layout')>()),
  LIVE_POLL_MS: 60 * 60_000,
}))

const mockApi = sageApi as unknown as Record<string, ReturnType<typeof vi.fn>>

const RUN = {
  run_id: 'run-1', repo: 'o/r', changes: ['https://github.com/o/r/pull/1'],
  change_ids: ['CR-1'], started_at: '2026-01-01T00:00:00Z',
}
const EMPTY = { red: 0, yellow: 0, green: 0 }

let finished = false
// What the pane would render on each provider render, in order.
let panes: string[] = []

function Probe() {
  const { report, reportLoading, activeRun } = useSage()
  panes.push(reportLoading ? 'skeleton' : report?.ready ? 'report' : 'no report')
  return (
    <div>
      <div data-testid="status">{activeRun?.status ?? 'none'}</div>
      <div data-testid="report-status">{report?.status ?? 'none'}</div>
      <div data-testid="ready">{String(Boolean(report?.ready))}</div>
    </div>
  )
}

function mount() {
  // staleTime as the dashboard's own client sets it.
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SageProvider initialRunId="run-1">
          <Probe />
        </SageProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
  return qc
}

const finish = (qc: QueryClient) => {
  finished = true
  return act(() => qc.refetchQueries({ queryKey: ['code-review-sage', 'runs'] }))
}

describe('a run that finishes while open', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    finished = false
    panes = []
    mockApi.runs.mockImplementation(async () => ({
      runs: [{ ...RUN, status: finished ? 'done' : 'running' }],
    }))
    mockApi.settings.mockResolvedValue({ settings: {}, pool: null, reviewer: null })
    mockApi.pinnedRepos.mockResolvedValue({ repos: [] })
    mockApi.recentRepos.mockResolvedValue({ repos: [] })
    mockApi.repoPrs.mockResolvedValue({ repo: 'o/r', prs: [], count: 0 })
  })

  it('fetches the report again once the run list shows it done', async () => {
    // The backend writes the report before it marks the run done, so both
    // answers flip together.
    mockApi.runReport.mockImplementation(async () => ({
      run_id: 'run-1', status: finished ? 'done' : 'running', ready: finished,
      bands: finished ? { red: 1, yellow: 0, green: 0 } : EMPTY,
      rows: [], generated_at: finished ? '2026-01-01T00:05:00Z' : '',
      total: finished ? 1 : 0, report_slug: null,
    }))
    const qc = mount()
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('running'))
    await waitFor(() => expect(screen.getByTestId('report-status').textContent).toBe('running'))
    expect(screen.getByTestId('ready').textContent).toBe('false')

    await finish(qc)

    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('done'))
    await waitFor(() => expect(screen.getByTestId('ready').textContent).toBe('true'))
  })

  it('keeps a report already on screen while the status change refetches it', async () => {
    // A poll that lands after the report is written but before the run is
    // marked done already answers ready.
    mockApi.runReport.mockImplementation(async () => ({
      run_id: 'run-1', status: finished ? 'done' : 'running', ready: true,
      bands: { red: 1, yellow: 0, green: 0 }, rows: [],
      generated_at: '2026-01-01T00:05:00Z', total: 1, report_slug: null,
    }))
    const qc = mount()
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('running'))
    await waitFor(() => expect(screen.getByTestId('report-status').textContent).toBe('running'))
    expect(screen.getByTestId('ready').textContent).toBe('true')

    await finish(qc)

    await waitFor(() => expect(screen.getByTestId('report-status').textContent).toBe('done'))
    const shown = panes.slice(panes.indexOf('report'))
    expect(shown.every((p) => p === 'report')).toBe(true)
  })
})
