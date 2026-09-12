import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'

import type { Run, RunReport } from '../apps/code-review-sage/lib/types'
import type { SageContextValue } from '../apps/code-review-sage/context'

/**
 * The detail pane's report AREA, which is a five-way switch (error, loading
 * skeleton, the report, "still running", "finished with nothing"), plus the
 * publish wiring it hands the report view. Getting the switch order wrong hides
 * a finished report behind a spinner; getting the publish wiring wrong posts the
 * WRONG comments, so both are pinned here.
 *
 * `ReportView` / `PostCommentsButton` are probes: they have their own suites,
 * and what this file is about is which callback RunDetail passes where.
 */
const sage: Record<string, unknown> = {}

vi.mock('../apps/code-review-sage/context', async importOriginal => {
  const actual = await importOriginal<typeof import('../apps/code-review-sage/context')>()
  return { ...actual, useSage: () => sage as unknown as SageContextValue }
})

vi.mock('../apps/code-review-sage/components/ReportView', () => ({
  default: ({ actions, isPosting, onPostFinding, onPostSelection, onArchive, archiveError }: {
    actions?: ReactNode
    isPosting?: (key: string) => boolean
    onPostFinding?: (changeId: string, key: string) => void
    onPostSelection?: (groups: { changeId: string; keys: string[] }[]) => Promise<void>
    onArchive?: () => void
    archiveError?: string | null
  }) => (
    <div data-testid="report">
      <span data-testid="posting-k1">{isPosting?.('zzz-key-1') ? 'zzz-yes' : 'zzz-no'}</span>
      <span data-testid="archive-error">{archiveError ?? 'zzz-none'}</span>
      <button onClick={() => onPostFinding?.('zzz-change-1', 'zzz-key-1')}>zzz-post-one</button>
      <button onClick={() => void onPostSelection?.([{ changeId: 'zzz-change-1', keys: ['zzz-key-1'] }])}>
        zzz-post-selection
      </button>
      <button onClick={() => onArchive?.()}>zzz-archive</button>
      {actions}
    </div>
  ),
}))

vi.mock('../apps/code-review-sage/components/PostCommentsButton', () => ({
  default: ({ onPost, posting, error }: {
    onPost: () => void
    posting: boolean
    error: string | null
  }) => (
    <div>
      <span data-testid="post-state">{posting ? 'zzz-busy' : 'zzz-idle'}</span>
      <span data-testid="post-error">{error ?? 'zzz-none'}</span>
      <button onClick={onPost}>zzz-post-all</button>
    </div>
  ),
}))

import RunDetail from '../apps/code-review-sage/components/RunDetail'

function makeRun(overrides: Partial<Run> = {}): Run {
  return {
    run_id: 'zzz-run-1',
    repo: 'zzzowner/zzzrepo',
    changes: ['https://github.com/zzzowner/zzzrepo/pull/7'],
    status: 'done',
    started_at: '2026-08-01T00:00:00Z',
    finished_at: '2026-08-01T00:05:00Z',
    ...overrides,
  }
}

function makeReport(overrides: Partial<RunReport> = {}): RunReport {
  return {
    run_id: 'zzz-run-1',
    status: 'done',
    ready: true,
    bands: { red: 0, yellow: 0, green: 1 },
    rows: [],
    generated_at: '2026-08-01T00:05:00Z',
    total: 1,
    report_slug: null,
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  Object.keys(sage).forEach(k => delete sage[k])
  Object.assign(sage, {
    pool: null,
    runs: [],
    cancelRun: vi.fn(),
    cancelling: false,
    report: null,
    reportLoading: false,
    reportError: null,
    archiveRun: vi.fn(),
    archiving: false,
    archiveError: null,
    postComments: vi.fn(),
    postCommentGroups: vi.fn(async () => {}),
    posting: false,
    postError: null,
    postingSelection: undefined,
    startReview: { mutate: vi.fn(), isPending: false, error: null, data: undefined },
  })
})

describe('RunDetail header', () => {
  it('names the review, its status, and when it started and finished', () => {
    render(<RunDetail run={makeRun()} />)
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('zzzowner/zzzrepo')
    expect(screen.getByText('Done')).toBeInTheDocument()
    expect(screen.getByText(/^started/)).toBeInTheDocument()
    expect(screen.getByText(/^finished/)).toBeInTheDocument()
  })

  it('adds a "+N more" tail when the run covers several pull requests', () => {
    render(<RunDetail run={makeRun({
      repo: undefined,
      changes: [
        'https://github.com/zzzowner/zzzrepo/pull/7',
        'https://github.com/zzzowner/zzzrepo/pull/8',
        'https://github.com/zzzowner/zzzrepo/pull/9',
      ],
    })} />)
    expect(screen.getByText(/\+2 more/)).toBeInTheDocument()
  })

  it('explains skipped in-flight pull requests when the backend refused duplicates', () => {
    render(<RunDetail run={makeRun({ skipped_inflight: 2 })} />)
    expect(screen.getByText(/2 pull requests were skipped/i)).toBeInTheDocument()
  })

  it('carries NO delete control — deleting is only offered on the run card', () => {
    render(<RunDetail run={makeRun()} />)
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument()
  })

  it('offers the retry from the failure notice, preserving the run model', async () => {
    const failedRun = makeRun({ status: 'error', error: 'zzz worker died', model: 'model-concrete' })
    render(<RunDetail run={failedRun} />)
    await userEvent.click(screen.getByRole('button', { name: /run it again/i }))
    expect((sage.startReview as { mutate: ReturnType<typeof vi.fn> }).mutate)
      .toHaveBeenCalledWith({ changes: failedRun.changes, model: 'model-concrete' })
  })
})

describe('RunDetail report area', () => {
  it('shows the report error instead of a skeleton or an empty state', () => {
    sage.reportError = new Error('zzz report unreadable')
    sage.reportLoading = true
    render(<RunDetail run={makeRun()} />)
    expect(screen.getByText('zzz report unreadable')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows the loading skeleton with an accessible status line', () => {
    sage.reportLoading = true
    render(<RunDetail run={makeRun()} />)
    expect(screen.getByRole('status')).toHaveTextContent(/Loading/i)
  })

  it('renders the report once it is ready', () => {
    sage.report = makeReport()
    render(<RunDetail run={makeRun()} />)
    expect(screen.getByTestId('report')).toBeInTheDocument()
  })

  it('promises the report later while the run is still going', () => {
    render(<RunDetail run={makeRun({ status: 'running', finished_at: undefined })} />)
    expect(screen.getByText(/Report appears here as soon as the review finishes/i)).toBeInTheDocument()
  })

  it('says a cancelled run stopped before anything finished', () => {
    render(<RunDetail run={makeRun({ status: 'cancelled' })} />)
    expect(screen.getByText(/produced no report/i)).toBeInTheDocument()
    expect(screen.getByText(/cancelled before any pull request finished/i)).toBeInTheDocument()
  })

  it('says a finished-but-empty run has nothing to report', () => {
    sage.report = makeReport({ ready: false })
    render(<RunDetail run={makeRun({ status: 'error' })} />)
    expect(screen.getByText(/nothing to report/i)).toBeInTheDocument()
    expect(screen.queryByText(/cancelled before/i)).not.toBeInTheDocument()
  })
})

describe('RunDetail publishing', () => {
  beforeEach(() => { sage.report = makeReport() })

  it('posts the whole review from the run-level action', async () => {
    render(<RunDetail run={makeRun()} />)
    await userEvent.click(screen.getByRole('button', { name: 'zzz-post-all' }))
    expect(sage.postComments).toHaveBeenCalledWith('zzz-run-1')
  })

  it('posts a single finding as its own one-key request', async () => {
    render(<RunDetail run={makeRun()} />)
    await userEvent.click(screen.getByRole('button', { name: 'zzz-post-one' }))
    expect(sage.postComments).toHaveBeenCalledWith('zzz-run-1', {
      changeId: 'zzz-change-1', keys: ['zzz-key-1'],
    })
  })

  it('sends a multi-change selection through the sequential group path', async () => {
    render(<RunDetail run={makeRun()} />)
    await userEvent.click(screen.getByRole('button', { name: 'zzz-post-selection' }))
    expect(sage.postCommentGroups).toHaveBeenCalledWith('zzz-run-1', [
      { changeId: 'zzz-change-1', keys: ['zzz-key-1'] },
    ])
  })

  it('archives from the report, surfacing an archive failure', async () => {
    sage.archiveError = new Error('zzz archive refused')
    render(<RunDetail run={makeRun()} />)
    await userEvent.click(screen.getByRole('button', { name: 'zzz-archive' }))
    expect(sage.archiveRun).toHaveBeenCalledWith('zzz-run-1')
    expect(screen.getByTestId('archive-error')).toHaveTextContent('zzz archive refused')
  })

  it('prefers the run-level post error over the mutation error', () => {
    sage.postError = new Error('zzz mutation error')
    render(<RunDetail run={makeRun({ post_error: 'zzz run error' })} />)
    expect(screen.getByTestId('post-error')).toHaveTextContent('zzz run error')
  })

  it('reads posting from the run flag too, not only the local mutation', () => {
    render(<RunDetail run={makeRun({ posting: true })} />)
    expect(screen.getByTestId('post-state')).toHaveTextContent('zzz-busy')
  })
})

describe('RunDetail per-comment posting attribution', () => {
  beforeEach(() => { sage.report = makeReport() })

  it('attributes a post observed only through the run flag to the WHOLE review', () => {
    // No local selection (another tab, or a reload mid-post): every card may say
    // it is going out, because there is nothing finer to attribute it to.
    sage.postingSelection = undefined
    render(<RunDetail run={makeRun({ posting: true })} />)
    expect(screen.getByTestId('posting-k1')).toHaveTextContent('zzz-yes')
  })

  it('says nothing is in flight when neither the run nor a selection is posting', () => {
    sage.postingSelection = undefined
    render(<RunDetail run={makeRun()} />)
    expect(screen.getByTestId('posting-k1')).toHaveTextContent('zzz-no')
  })

  it('treats a whole-review post (null selection) as covering every comment', () => {
    sage.postingSelection = null
    render(<RunDetail run={makeRun()} />)
    expect(screen.getByTestId('posting-k1')).toHaveTextContent('zzz-yes')
  })

  it('limits the in-flight marker to the keys actually being sent', () => {
    sage.postingSelection = { changeId: 'zzz-change-1', keys: ['zzz-key-1'] }
    const { unmount } = render(<RunDetail run={makeRun()} />)
    expect(screen.getByTestId('posting-k1')).toHaveTextContent('zzz-yes')
    unmount()

    sage.postingSelection = { changeId: 'zzz-change-1', keys: ['zzz-other-key'] }
    render(<RunDetail run={makeRun()} />)
    expect(screen.getByTestId('posting-k1')).toHaveTextContent('zzz-no')
  })
})
