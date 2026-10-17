// Code Review Sage remembers where you were, and shows the last data first.
//
// Two behaviours, tested separately because they fail differently: restoring UI
// STATE (wrong screen after reload) and replaying query SNAPSHOTS (a skeleton on
// every reload). The snapshot half is stale-while-revalidate — the cached payload
// must render immediately AND a refetch must still go out.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import Workspace from '../apps/code-review-sage/Workspace'
import { SageProvider } from '../apps/code-review-sage/context'
import {
  UI_STATE_KEY, loadRecentRepos, loadUiState, readSnapshot, rememberRecentRepo,
  saveUiState, writeSnapshot,
} from '../apps/code-review-sage/lib/persist'
import { sageApi } from '../apps/code-review-sage/api'
import type { Run } from '../apps/code-review-sage/lib/types'

vi.mock('../apps/code-review-sage/api', () => ({
  sageApi: {
    runs: vi.fn(),
    runReport: vi.fn(),
    startReview: vi.fn(),
    startReviewLinks: vi.fn(),
    startRepoReview: vi.fn(),
    cancelRun: vi.fn(),
    deleteRun: vi.fn(),
    archiveRun: vi.fn(),
    postComments: vi.fn(),
    myRepos: vi.fn(),
    recentRepos: vi.fn(),
    pinnedRepos: vi.fn(),
    pinRepo: vi.fn(),
    pinRepoUrl: vi.fn(),
    unpinRepo: vi.fn(),
    repoPrs: vi.fn(),
    settings: vi.fn(),
    putSettings: vi.fn(),
    namespaces: vi.fn(),
    createNamespace: vi.fn(),
    deleteNamespace: vi.fn(),
    learnings: vi.fn(),
    pullRequestSource: vi.fn(),
  },
}))

const mockApi = sageApi as unknown as Record<string, ReturnType<typeof vi.fn>>

function run(over: Partial<Run> = {}): Run {
  return {
    run_id: 'run-aaa',
    repo: 'acme/widgets',
    changes: ['https://github.com/acme/widgets/pull/7'],
    change_ids: ['GH-acme-widgets-7'],
    status: 'done',
    started_at: new Date(Date.now() - 120_000).toISOString(),
    finished_at: new Date(Date.now() - 30_000).toISOString(),
    progress: { 'GH-acme-widgets-7': { phase: 'done' } },
    summary: { ok: true },
    ...over,
  }
}

function mount(initialRunId: string | null = null) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SageProvider initialRunId={initialRunId}>
          <Workspace />
        </SageProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('Sage persistence helpers', () => {
  beforeEach(() => localStorage.clear())

  it('round-trips UI state', () => {
    saveUiState({
      mainView: 'settings', listTab: 'reviews',
      activeRepo: { owner: 'acme', repo: 'widgets' },
      selectedRunId: 'run-aaa', selectedPr: null, detailTab: null,
    })
    expect(loadUiState().mainView).toBe('settings')
    expect(loadUiState().activeRepo).toEqual({ owner: 'acme', repo: 'widgets' })
  })

  it('discards a corrupt value instead of throwing', () => {
    localStorage.setItem(UI_STATE_KEY, '{not json')
    expect(loadUiState()).toEqual({})
  })

  it('discards state written by an older schema', () => {
    localStorage.setItem(UI_STATE_KEY, JSON.stringify({ v: 0, state: { mainView: 'settings' } }))
    expect(loadUiState()).toEqual({})
  })

  it('keeps the fetch timestamp with the snapshot', () => {
    writeSnapshot('runs', { runs: [] })
    const snap = readSnapshot<{ runs: unknown[] }>('runs')
    expect(snap?.data).toEqual({ runs: [] })
    // Without this, react-query would treat the replay as fresh and skip the
    // background refetch — the "revalidate" half of the behaviour.
    expect(typeof snap?.at).toBe('number')
  })

  it('ignores a snapshot older than a day', () => {
    writeSnapshot('runs', { runs: [] })
    const raw = JSON.parse(localStorage.getItem('kc:code-review-sage:cache:runs') as string)
    raw.at = Date.now() - 25 * 60 * 60 * 1000
    localStorage.setItem('kc:code-review-sage:cache:runs', JSON.stringify(raw))
    expect(readSnapshot('runs')).toBeUndefined()
  })

  it('skips a payload too large to store', () => {
    writeSnapshot('big', { blob: 'x'.repeat(300 * 1024) })
    expect(readSnapshot('big')).toBeUndefined()
  })
})

describe('Sage restores its last state', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.runs.mockResolvedValue({ runs: [run()] })
    mockApi.pinnedRepos.mockResolvedValue({ repos: [{ owner: 'acme', repo: 'widgets' }] })
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  it('comes back to the repo and tab it was left on', async () => {
    saveUiState({
      mainView: 'reviews', listTab: 'reviews',
      activeRepo: { owner: 'acme', repo: 'widgets' },
      selectedRunId: null, selectedPr: null, detailTab: null,
    })
    mount()
    // The repo is active (its PR fetch fired) and the Reviews tab is in front.
    await waitFor(() => expect(mockApi.repoPrs).toHaveBeenCalled())
    const tab = await screen.findByRole('tab', { name: /Reviews/ })
    expect(tab.getAttribute('aria-selected')).toBe('true')
  })

  it('drops a restored repo that is no longer pinned', async () => {
    saveUiState({
      mainView: 'reviews', listTab: 'pulls',
      activeRepo: { owner: 'gone', repo: 'away' },
      selectedRunId: null, selectedPr: null, detailTab: null,
    })
    mount()
    // Otherwise the column stays stuck on a repo the rail no longer lists. The
    // persisted value going null is the precise signal it was dropped.
    await waitFor(() => expect(loadUiState().activeRepo).toBeNull())
  })

  it('drops a restored review that has since been evicted', async () => {
    saveUiState({
      mainView: 'reviews', listTab: 'reviews', activeRepo: null,
      selectedRunId: 'run-long-gone', selectedPr: null, detailTab: null,
    })
    mount()
    expect(await screen.findByText(/Select a review to see its progress/)).toBeTruthy()
  })

  it('persists a repo pick for the next visit', async () => {
    mount()
    await screen.findByRole('complementary')
    await userEvent.click(await screen.findByRole('button', {
      name: /Pick a repository|^Repository:/,
    }))
    await userEvent.click(await screen.findByRole('menuitem', { name: /acme\/\s*widgets/ }))
    await waitFor(() => expect(loadUiState().activeRepo)
      .toEqual({ owner: 'acme', repo: 'widgets' }))
  })
})

describe('Sage serves the last payload while it refreshes', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.pinnedRepos.mockResolvedValue({ repos: [] })
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  it('shows cached reviews before the request resolves', async () => {
    writeSnapshot('runs', { runs: [run()] })
    // A request that never settles: anything rendered came from the snapshot.
    mockApi.runs.mockReturnValue(new Promise(() => {}))
    mount()
    await userEvent.click(await screen.findByRole('tab', { name: /Reviews/ }))
    const rail = await screen.findByRole('complementary')
    expect(await within(rail).findByRole('button', { name: /Review of acme\/widgets/ }))
      .toBeTruthy()
  })

  it('still refetches, so the cached list is replaced by fresh data', async () => {
    writeSnapshot('runs', { runs: [run()] })
    mockApi.runs.mockResolvedValue({ runs: [run({
      run_id: 'run-bbb',
      repo: 'other/thing',
      changes: ['https://github.com/other/thing/pull/3'],
      change_ids: ['GH-other-thing-3'],
    })] })
    mount()
    await userEvent.click(await screen.findByRole('tab', { name: /Reviews/ }))
    const rail = await screen.findByRole('complementary')
    // The stale entry is shown first, then revalidation swaps it out.
    expect(await within(rail).findByRole('button', { name: /Review of other\/thing/ }))
      .toBeTruthy()
    expect(mockApi.runs).toHaveBeenCalled()
  })

  it('revalidates a cached PR list even though it is cached for a minute', async () => {
    // The PR query has staleTime 60s, so the snapshot's ORIGINAL timestamp is
    // what makes it count as stale on arrival. Replaying it without that
    // timestamp would look freshly fetched and suppress the refetch for a
    // minute — you would be reading a list that never revalidated.
    saveUiState({
      mainView: 'reviews', listTab: 'pulls',
      activeRepo: { owner: 'acme', repo: 'widgets' },
      selectedRunId: null, selectedPr: null, detailTab: null,
    })
    mockApi.pinnedRepos.mockResolvedValue({ repos: [{ owner: 'acme', repo: 'widgets' }] })
    mockApi.runs.mockResolvedValue({ runs: [] })
    writeSnapshot('prs:acme/widgets', {
      repo: 'acme/widgets',
      count: 1,
      prs: [{
        url: 'https://github.com/acme/widgets/pull/7', number: 7,
        title: 'Cached pull request', head_sha: 'abc1234', author: 'ann',
        updated_at: new Date().toISOString(), draft: false,
        change_id: 'GH-acme-widgets-7', reviewed: false, reviewed_stale: false,
      }],
    })
    // Aged past staleTime: a snapshot written a moment ago is legitimately
    // fresh, and NOT refetching it is correct. The interesting case is the one a
    // real reload hits — a payload from earlier.
    const cacheKey = 'kc:code-review-sage:cache:prs:acme/widgets'
    const stored = JSON.parse(localStorage.getItem(cacheKey) as string)
    stored.at = Date.now() - 5 * 60_000
    localStorage.setItem(cacheKey, JSON.stringify(stored))
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', count: 0, prs: [] })
    mount()
    expect(await screen.findByText('Cached pull request')).toBeTruthy()
    await waitFor(() => expect(mockApi.repoPrs).toHaveBeenCalled())
  })

  it('writes a snapshot from the response for the next load', async () => {
    mockApi.runs.mockResolvedValue({ runs: [run()] })
    mount()
    await waitFor(() => expect(readSnapshot<{ runs: Run[] }>('runs')?.data.runs?.[0].run_id)
      .toBe('run-aaa'))
  })
})

describe('most recently picked repos', () => {
  beforeEach(() => localStorage.clear())

  it('remembers a pick', () => {
    rememberRecentRepo({ owner: 'acme', repo: 'widgets' })
    expect(loadRecentRepos()).toEqual([{ owner: 'acme', repo: 'widgets' }])
  })

  it('moves a repicked repo back to the front', () => {
    rememberRecentRepo({ owner: 'a', repo: 'one' })
    rememberRecentRepo({ owner: 'b', repo: 'two' })
    rememberRecentRepo({ owner: 'a', repo: 'one' })
    expect(loadRecentRepos()).toEqual([
      { owner: 'a', repo: 'one' }, { owner: 'b', repo: 'two' },
    ])
  })

  it('keeps the list short', () => {
    for (const n of [1, 2, 3, 4, 5, 6, 7]) {
      rememberRecentRepo({ owner: 'o', repo: `r${n}` })
    }
    // A long "recent" section would just be a second copy of the full list.
    expect(loadRecentRepos()).toHaveLength(5)
    expect(loadRecentRepos()[0]).toEqual({ owner: 'o', repo: 'r7' })
  })

  it('survives a corrupt value', () => {
    localStorage.setItem('kc:code-review-sage:recent-repos', 'not json')
    expect(loadRecentRepos()).toEqual([])
  })
})

describe('the report and PR source are served stale-while-revalidate', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.pinnedRepos.mockResolvedValue({ repos: [] })
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  it('paints a cached report before the request resolves', async () => {
    writeSnapshot('report:run-aaa', {
      run_id: 'run-aaa', status: 'done', ready: true,
      bands: { red: 1, yellow: 0, green: 0 }, generated_at: '', total: 1,
      report_slug: null,
      rows: [{
        change_id: 'GH-acme-widgets-7',
        url: 'https://github.com/acme/widgets/pull/7',
        title: 'Cached finding row', band: 'red', why: 'w', score: 10,
        design_risk: 'high', blast: 'LARGE', red: 1, yellow: 0,
        deep_reviewed: true, gate_verdict: 'CONCERNS', findings: [],
      }],
    })
    mockApi.runs.mockResolvedValue({ runs: [run()] })
    // A report request that never settles: anything shown came from the snapshot.
    mockApi.runReport.mockReturnValue(new Promise(() => {}))
    mount('run-aaa')
    expect(await screen.findByText(/Cached finding row/)).toBeTruthy()
  })

  it('writes a report snapshot only once it is ready', async () => {
    mockApi.runs.mockResolvedValue({ runs: [run()] })
    mockApi.runReport.mockResolvedValue({
      run_id: 'run-aaa', status: 'running', ready: false, bands: null,
      generated_at: '', total: 0, report_slug: null, rows: [],
    })
    mount('run-aaa')
    await waitFor(() => expect(mockApi.runReport).toHaveBeenCalled())
    // A not-ready report describes a moment in a run's life; replaying it later
    // would misreport a finished review as still working.
    expect(readSnapshot('report:run-aaa')).toBeUndefined()
  })
})
