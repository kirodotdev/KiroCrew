// The PR pane's run and its report must be the SAME run.
//
// `prRun` used to be a fresh search over `runs` for the newest review of the
// selected PR, while the report query is keyed by `selectedRunId`. Those two can
// disagree: a newer review of the same PR (another tab, or a re-review) appears at
// the head of `runs`, so `prRun` moved to it while the pane still rendered the
// older run's report. Every action hanging off `prRun` -- cancel, post -- then
// targeted a run whose findings were not the ones on screen.
//
// This drives the REAL provider and reads `prRun` out of `useSage()`, rather than
// re-implementing the derivation in the test body.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SageProvider, useSage } from '../apps/code-review-sage/context'
import { sageApi } from '../apps/code-review-sage/api'

vi.mock('../apps/code-review-sage/api', () => ({
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

const mockApi = sageApi as unknown as Record<string, ReturnType<typeof vi.fn>>

const OLDER = {
  run_id: 'run-old', repo: 'o/r', changes: ['https://github.com/o/r/pull/1'],
  change_ids: ['CR-1'], status: 'done',
  started_at: '2026-01-01T00:00:00Z', finished_at: '2026-01-01T00:05:00Z',
}
// Newest-first, so this is what the old `runs.find(...)` returned for CR-1.
const NEWER = { ...OLDER, run_id: 'run-new', started_at: '2026-01-02T00:00:00Z' }

const PR = { change_id: 'CR-1', url: 'https://github.com/o/r/pull/1', repo: 'o/r' }

/** Reads the derivation out of the real context and exposes the seams. */
function Probe() {
  const { prRun, selectedRunId, selectPr, selectRun } = useSage()
  return (
    <div>
      <div data-testid="pr-run">{prRun?.run_id ?? 'none'}</div>
      <div data-testid="selected">{selectedRunId ?? 'none'}</div>
      <button onClick={() => selectPr(PR as never)}>pick pr</button>
      <button onClick={() => selectRun('run-old')}>pick old run</button>
    </div>
  )
}

function mount() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SageProvider initialRunId={null}>
          <Probe />
        </SageProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('prRun and the displayed report stay on the same run', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.runs.mockResolvedValue({ runs: [NEWER, OLDER] })
    mockApi.runReport.mockResolvedValue({ ready: false })
    mockApi.settings.mockResolvedValue({ settings: {}, pool: null, reviewer: null })
    mockApi.pinnedRepos.mockResolvedValue({ repos: [] })
    mockApi.recentRepos.mockResolvedValue({ repos: [] })
    mockApi.repoPrs.mockResolvedValue({ repo: 'o/r', prs: [], count: 0 })
  })

  it('follows the selected run, not the newest review of the PR', async () => {
    mount()
    await waitFor(() => expect(mockApi.runs).toHaveBeenCalled())

    await userEvent.click(screen.getByText('pick pr'))
    // Then the user opens the OLDER review of that same PR from the list.
    await userEvent.click(screen.getByText('pick old run'))

    await waitFor(() =>
      expect(screen.getByTestId('selected').textContent).toBe('run-old'))
    // The old derivation returned run-new here (first match in a newest-first
    // list), so the pane's actions pointed at a run the report was not showing.
    expect(screen.getByTestId('pr-run').textContent).toBe('run-old')
  })

  it('is null for a multi-PR run, which has no single subject', async () => {
    // selectRun clears selectedPr for a run covering several PRs, so the pane
    // falls back to the run view. prRun must agree rather than picking one.
    mockApi.runs.mockResolvedValue({
      runs: [{
        ...OLDER, run_id: 'run-old',
        changes: ['https://github.com/o/r/pull/1', 'https://github.com/o/r/pull/2'],
        change_ids: ['CR-1', 'CR-2'],
      }],
    })
    mount()
    await waitFor(() => expect(mockApi.runs).toHaveBeenCalled())

    await userEvent.click(screen.getByText('pick old run'))

    await waitFor(() =>
      expect(screen.getByTestId('selected').textContent).toBe('run-old'))
    expect(screen.getByTestId('pr-run').textContent).toBe('none')
  })
})
