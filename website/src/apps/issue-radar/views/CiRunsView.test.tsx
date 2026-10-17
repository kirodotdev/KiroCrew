// The CI runs dashboard: one block per connected GitHub repo, read on demand.
//
// Pins the three render states the tab can be in (runs, empty, failed) per repo,
// that a failed repo shows its own error row without hiding the others, and that
// opening the tab is the only thing that reads: the query does not refetch on
// window focus.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { CiRunsResponse, CiRun } from '../api'

const ciRuns = vi.fn<() => Promise<CiRunsResponse>>()

vi.mock('../api', () => ({
  issueRadarApi: { ciRuns: () => ciRuns() },
}))

import CiRunsView, { runState } from './CiRunsView'

function run(over: Partial<CiRun>): CiRun {
  return {
    id: 1,
    name: 'CI',
    status: 'completed',
    conclusion: 'success',
    url: 'https://github.com/acme/alpha/actions/runs/1',
    branch: 'feature',
    pull_requests: [12],
    created_at: '2026-01-01T00:00:00Z',
    started_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:02:00Z',
    duration_sec: 120,
    ...over,
  }
}

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <CiRunsView />
    </QueryClientProvider>,
  )
}

describe('CiRunsView', () => {
  beforeEach(() => {
    ciRuns.mockReset()
  })

  it('lists running and failed runs per repo, with an error row for a repo that failed', async () => {
    ciRuns.mockResolvedValue({
      truncated: false,
      repos: [
        {
          owner: 'acme', repo: 'alpha', provider: 'github', host: 'github.com', alerts: true, error: null,
          runs: [
            run({ id: 1, name: 'Lint', status: 'in_progress', conclusion: null, duration_sec: null }),
            run({ id: 2, name: 'Tests', conclusion: 'failure' }),
          ],
        },
        {
          owner: 'acme', repo: 'down', provider: 'github', host: 'github.com', alerts: false,
          error: 'provider_error', runs: [],
        },
      ],
    })
    renderView()
    const alpha = await screen.findByRole('region', { name: 'acme/alpha' })
    expect(alpha.textContent).toContain('Running')
    expect(alpha.textContent).toContain('Failed')
    expect(alpha.textContent).toContain('Failure alerts on')
    expect(screen.getByRole('link', { name: 'Tests' }).getAttribute('href')).toBe(
      'https://github.com/acme/alpha/actions/runs/1',
    )
    const down = screen.getByRole('region', { name: 'acme/down' })
    expect(down.textContent).toContain('Couldn’t read this repository’s runs')
    expect(down.textContent).not.toContain('Failure alerts on')
    expect(within(down).getByTestId('ci-runs-repo-error')).toBeTruthy()
    // A long repo name truncates instead of pushing the alerts marker off a narrow pane.
    expect(within(alpha).getByRole('heading', { name: 'acme/alpha' }).className).toContain('truncate')
  })

  it('shows the empty states', async () => {
    ciRuns.mockResolvedValue({
      truncated: false,
      repos: [
        { owner: 'acme', repo: 'quiet', provider: 'github', host: 'github.com', alerts: false, error: null, runs: [] },
      ],
    })
    renderView()
    expect(await screen.findByText('No workflow runs yet.')).toBeTruthy()
  })

  it('says when no GitHub repo is connected', async () => {
    ciRuns.mockResolvedValue({ truncated: false, repos: [] })
    renderView()
    expect(await screen.findByText('No GitHub repository is connected.')).toBeTruthy()
  })

  it('shows a load error and reads again only on Refresh', async () => {
    ciRuns.mockRejectedValueOnce(new Error('boom'))
    renderView()
    expect(await screen.findByTestId('ci-runs-error')).toBeTruthy()
    expect(ciRuns).toHaveBeenCalledTimes(1)
    window.dispatchEvent(new Event('focus'))
    expect(ciRuns).toHaveBeenCalledTimes(1)
    ciRuns.mockResolvedValue({ truncated: true, repos: [] })
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    await waitFor(() => expect(ciRuns).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('Not every connected GitHub repository is listed here.')).toBeTruthy()
  })

  it('maps every run state to a label', () => {
    expect(runState({ status: 'queued', conclusion: null }).label).toBe('Queued')
    expect(runState({ status: 'completed', conclusion: 'success' }).label).toBe('Passed')
    expect(runState({ status: 'completed', conclusion: 'timed_out' }).label).toBe('Timed out')
    expect(runState({ status: 'completed', conclusion: 'cancelled' }).label).toBe('Cancelled')
    expect(runState({ status: 'completed', conclusion: 'skipped' }).label).toBe('Finished')
  })
})
