import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'

import type {
  ReviewFixFindingSnapshot,
  Run,
  RunReport,
} from '../apps/code-review-sage/lib/types'

const sage: Record<string, unknown> = {}
const sourceState = vi.hoisted(() => ({
  data: undefined as unknown,
  isLoading: false,
  error: null as Error | null,
}))
const sageApi = vi.hoisted(() => ({
  fixTask: vi.fn(),
  reviewAgain: vi.fn(),
}))

type ReportMockProps = {
  actions?: ReactNode
  isPosting?: (key: string) => boolean
  onStartFix?: (findings: ReviewFixFindingSnapshot[]) => void
  onArchive?: () => void
  onPostFinding?: (changeId: string, key: string) => void
  onPostSelection?: (groups: { changeId: string; keys: string[] }[]) => void
}

vi.mock('../apps/code-review-sage/api', () => ({ sageApi }))

vi.mock('../apps/code-review-sage/context', () => ({
  useSage: () => sage,
}))

vi.mock('../apps/code-review-sage/components/PrSourcePanel', () => ({
  SourceError: ({ error }: { error: Error }) => <div role="alert">Source error: {error.message}</div>,
  usePrSource: () => sourceState,
}))

vi.mock('../apps/code-review-sage/components/ReviewModelPicker', () => ({
  default: ({ value, onChange, disabled }: {
    value: string
    onChange: (value: string) => void
    disabled?: boolean
  }) => (
    <input
      aria-label="Review model"
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.currentTarget.value)}
    />
  ),
}))

vi.mock('../apps/code-review-sage/components/ReviewChat', () => ({
  default: () => <div data-testid="review-chat">Review chat</div>,
}))

vi.mock('../apps/code-review-sage/components/DraftReviewActions', () => ({
  default: () => <div data-testid="draft-actions">Draft actions</div>,
}))

vi.mock('../apps/code-review-sage/components/EmptyState', () => ({
  default: ({ title, hint }: { title: string; hint: string }) => (
    <div data-testid="empty-state"><h2>{title}</h2><p>{hint}</p></div>
  ),
}))

vi.mock('../apps/code-review-sage/components/FailureNotice', () => ({
  default: ({ onRetry, retrying }: { onRetry?: () => void; retrying?: boolean }) => (
    <div data-testid="failure-notice">
      <span>Failure notice</span>
      {onRetry && <button type="button" onClick={onRetry} disabled={retrying}>Run it again</button>}
    </div>
  ),
}))

vi.mock('../apps/code-review-sage/components/RunProgress', () => ({
  default: ({ onCancel }: { onCancel: () => void }) => (
    <button type="button" onClick={onCancel}>Cancel review</button>
  ),
}))

vi.mock('../apps/code-review-sage/components/PostCommentsButton', () => ({
  default: ({ onPost }: { onPost: () => void }) => (
    <button type="button" onClick={onPost}>Post comments</button>
  ),
}))

vi.mock('../apps/code-review-sage/components/ReportView', () => ({
  default: ({
    actions, isPosting, onStartFix, onArchive, onPostFinding, onPostSelection,
  }: ReportMockProps) => (
    <div data-testid="report-view">
      <span data-testid="posting-state">{isPosting?.('finding-key') ? 'posting' : 'idle'}</span>
      {actions}
      {onStartFix && (
        <button
          type="button"
          onClick={() => onStartFix([{
            key: 'finding-key',
            title: 'Fix this finding',
            severity: 'red',
            body: 'Finding body',
            file_path: 'src/app.ts',
            line: 10,
            end_line: 10,
            fingerprint: 'finding-fingerprint',
            suggested_fix: 'Apply this fix',
          }])}
        >Start fix</button>
      )}
      {onArchive && <button type="button" onClick={onArchive}>Archive report</button>}
      {onPostFinding && <button type="button" onClick={() => onPostFinding('change-1', 'finding-key')}>Post one finding</button>}
      {onPostSelection && <button type="button" onClick={() => onPostSelection([{ changeId: 'change-1', keys: ['finding-key'] }])}>Post selected findings</button>}
    </div>
  ),
}))

vi.mock('../apps/code-review-sage/components/ReviewFixSetup', () => ({
  default: ({
    onCreated, onClose,
  }: {
    onCreated: (response: { task_id: string }) => void
    onClose: () => void
  }) => (
    <div data-testid="fix-setup">
      <button type="button" onClick={() => onCreated({ task_id: 'task-1' })}>Create mocked fix task</button>
      <button type="button" onClick={onClose}>Close mocked fix setup</button>
    </div>
  ),
}))

vi.mock('../components/ReviewFixTaskPanel', () => ({
  default: ({ onReviewAgain }: { onReviewAgain?: () => Promise<void> | void }) => (
    <div data-testid="fix-task-panel">
      {onReviewAgain && <button type="button" onClick={() => void onReviewAgain()}>Task review again</button>}
    </div>
  ),
}))

vi.mock('../components/PullRequestPanel', () => ({
  default: () => <div data-testid="pull-request-panel">Pull request source panel</div>,
}))

import PrReviewDetail from '../apps/code-review-sage/components/PrReviewDetail'

function makeRun(overrides: Partial<Run> = {}): Run {
  return {
    run_id: 'run-1',
    changes: ['https://github.com/acme/widgets/pull/7'],
    status: 'error',
    started_at: '2026-08-01T00:00:00Z',
    error: 'zzz worker died',
    model: 'model-concrete',
    ...overrides,
  }
}

const pr = {
  url: 'https://github.com/acme/widgets/pull/7',
  number: 7,
  change_id: 'GH-acme-widgets-7',
  title: 'Tighten the cookie jar',
}

function makeReport(overrides: Partial<RunReport> = {}): RunReport {
  return {
    run_id: 'run-1',
    status: 'done',
    ready: true,
    bands: { red: 1, yellow: 0, green: 0 },
    rows: [{
      change_id: pr.change_id,
      url: pr.url,
      title: 'Fix this finding',
      platform: 'github',
      band: 'red',
      why: 'blast=small + 1x red',
      score: 1,
      design_risk: 'low',
      blast: 'small',
      red: 1,
      yellow: 0,
      deep_reviewed: true,
      gate_verdict: 'review',
      findings: [{
        severity: 'red',
        headline: 'Fix this finding',
        observation: 'The value is not validated.',
        consequence: 'Invalid input can pass through.',
        suggestion: 'Validate the value.',
        file: 'src/app.ts',
        line: 10,
        end_line: 10,
        fingerprint: 'finding-fingerprint',
      }],
    }],
    generated_at: '2026-08-01T00:00:00Z',
    total: 1,
    report_slug: null,
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  sourceState.data = undefined
  sourceState.isLoading = false
  sourceState.error = null
  sageApi.fixTask.mockResolvedValue({
    revision: 9,
    review_fix: { target: { dirty_fingerprint: 'dirty-fingerprint' } },
  })
  sageApi.reviewAgain.mockResolvedValue({})
  Object.keys(sage).forEach(key => delete sage[key])
  Object.assign(sage, {
    prRun: makeRun(),
    report: null,
    reportLoading: false,
    reportError: null,
    startReview: { mutate: vi.fn(), isPending: false, error: null },
    cancelRun: vi.fn(),
    cancelling: false,
    pool: null,
    archiveRun: vi.fn(),
    archiving: false,
    archiveError: null,
    runs: [],
    postComments: vi.fn(),
    postCommentGroups: vi.fn(async () => {}),
    posting: false,
    postError: null,
    postingSelection: undefined,
    reviewModel: 'auto',
    setReviewModel: vi.fn(),
  })
})

describe('PrReviewDetail retry model preservation', () => {
  it('uses the saved model for both failure-notice and header retries', async () => {
    const user = userEvent.setup()
    render(<PrReviewDetail pr={pr} />)

    await user.click(screen.getByRole('button', { name: /run it again/i }))
    expect((sage.startReview as { mutate: ReturnType<typeof vi.fn> }).mutate)
      .toHaveBeenCalledWith({ changes: [pr.url], model: 'model-concrete' })

    const mutate = (sage.startReview as { mutate: ReturnType<typeof vi.fn> }).mutate
    mutate.mockClear()
    await user.click(screen.getByRole('button', { name: /retry review/i }))
    expect(mutate).toHaveBeenCalledWith({ changes: [pr.url], model: 'model-concrete' })
  })
})

describe('PrReviewDetail report states and actions', () => {
  it('renders an unreviewed PR and starts its first review', async () => {
    const user = userEvent.setup()
    sage.prRun = null
    const mutate = vi.fn()
    sage.startReview = { mutate, isPending: false, error: null }
    render(<PrReviewDetail pr={pr} />)

    expect(screen.getByTestId('empty-state')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /^Review$/ }))
    expect(mutate).toHaveBeenCalledWith([pr.url])
  })

  it('renders loading, report errors, running progress, and empty completed reports', () => {
    sage.reportLoading = true
    const loadingView = render(<PrReviewDetail pr={pr} />)
    expect(screen.getByRole('status')).toBeInTheDocument()
    loadingView.unmount()

    sage.reportLoading = false
    sage.reportError = new Error('report unavailable')
    const errorView = render(<PrReviewDetail pr={pr} />)
    expect(screen.getByText('report unavailable')).toBeInTheDocument()
    errorView.unmount()

    sage.reportError = null
    sage.prRun = makeRun({ status: 'running' })
    sage.runs = [sage.prRun]
    const runningView = render(<PrReviewDetail pr={pr} />)
    expect(screen.getByRole('button', { name: 'Cancel review' })).toBeInTheDocument()
    expect(screen.getByText(/findings appear here as soon as/i)).toBeInTheDocument()
    runningView.unmount()

    sage.prRun = makeRun({ status: 'done' })
    sage.report = makeReport({ rows: [{ ...makeReport().rows[0]!, url: 'https://github.com/acme/widgets/pull/99' }], bands: { red: 0, yellow: 0, green: 0 }, total: 0 })
    const emptyView = render(<PrReviewDetail pr={pr} />)
    expect(screen.getByTestId('empty-state')).toBeInTheDocument()
    emptyView.unmount()
  })

  it('renders report actions, Review Fix setup/task, and re-review callbacks', async () => {
    const user = userEvent.setup()
    sage.report = makeReport()
    sage.runs = [sage.prRun]
    render(<PrReviewDetail pr={pr} />)

    expect(screen.getByTestId('report-view')).toBeInTheDocument()
    expect(screen.getByTestId('posting-state')).toHaveTextContent('idle')
    await user.click(screen.getByRole('button', { name: 'Post comments' }))
    await user.click(screen.getByRole('button', { name: 'Post one finding' }))
    await user.click(screen.getByRole('button', { name: 'Post selected findings' }))
    await user.click(screen.getByRole('button', { name: 'Archive report' }))
    expect(sage.postComments).toHaveBeenCalledWith('run-1', { changeId: pr.change_id })
    expect(sage.postComments).toHaveBeenCalledWith('run-1', { changeId: 'change-1', keys: ['finding-key'] })
    expect(sage.postCommentGroups).toHaveBeenCalledWith('run-1', [{ changeId: 'change-1', keys: ['finding-key'] }])
    expect(sage.archiveRun).toHaveBeenCalledWith('run-1')

    await user.click(screen.getByRole('button', { name: 'Start fix' }))
    expect(screen.getByTestId('fix-setup')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Create mocked fix task' }))
    expect(screen.getByTestId('fix-task-panel')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Task review again' }))
    await waitFor(() => expect(sageApi.fixTask).toHaveBeenCalledWith('task-1'))
    expect(sageApi.reviewAgain).toHaveBeenCalledWith('task-1', {
      expected_revision: 9,
      target_fingerprint: 'dirty-fingerprint',
    })

    fireEvent.change(screen.getByRole('textbox', { name: 'Review model' }), {
      target: { value: 'model-new' },
    })
    expect(sage.setReviewModel).toHaveBeenCalledWith('model-new')
    expect(screen.getByTestId('review-chat')).toBeInTheDocument()
    expect(screen.getByTestId('draft-actions')).toBeInTheDocument()
  })

  it('renders cancelled and per-change failure explanations', () => {
    sage.report = { ...makeReport(), ready: false }
    sage.prRun = makeRun({ status: 'cancelled' })
    const cancelledView = render(<PrReviewDetail pr={pr} />)
    expect(screen.getByText(/cancelled before this one finished/i)).toBeInTheDocument()
    cancelledView.unmount()

    sage.prRun = makeRun({
      progress: { [pr.change_id]: { phase: 'failed', error: 'provider failed' } },
    })
    const failedView = render(<PrReviewDetail pr={pr} />)
    expect(screen.getByText(/see the reason above/i)).toBeInTheDocument()
    failedView.unmount()
  })
})

describe('PrReviewDetail source and header states', () => {
  it('renders source loading, errors, valid source data, and invalid source URLs', async () => {
    const user = userEvent.setup()
    sourceState.isLoading = true
    const loadingView = render(<PrReviewDetail pr={pr} />)
    await user.click(screen.getByRole('tab', { name: /pull request/i }))
    expect(screen.getByText(/loading pull-request details/i)).toBeInTheDocument()
    loadingView.unmount()

    sourceState.isLoading = false
    sourceState.error = new Error('source unavailable')
    const errorView = render(<PrReviewDetail pr={pr} />)
    await user.click(screen.getByRole('tab', { name: /pull request/i }))
    expect(screen.getByRole('alert')).toHaveTextContent('source unavailable')
    errorView.unmount()

    sourceState.error = null
    sourceState.data = {
      title: 'Provider title',
      author: 'alice',
      updatedAt: '2026-08-01T00:00:00Z',
      headSha: 'abcdef1234567890',
      draft: true,
      checks: [{ bucket: 'failed' }],
    }
    const sourceView = render(<PrReviewDetail pr={{ ...pr, title: '' }} />)
    expect(screen.getByRole('heading', { name: 'Provider title' })).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /pull request/i }))
    expect(screen.getByTestId('pull-request-panel')).toBeInTheDocument()
    sourceView.unmount()

    sourceState.data = { title: 'Provider title' }
    const badView = render(<PrReviewDetail pr={{ ...pr, url: 'javascript:bad' }} />)
    await user.click(screen.getByRole('tab', { name: /pull request/i }))
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /#7/i })).toHaveAttribute('href', '#')
    badView.unmount()

    sourceState.data = undefined
    const absentView = render(<PrReviewDetail pr={pr} />)
    await user.click(screen.getByRole('tab', { name: /pull request/i }))
    expect(screen.queryByTestId('pull-request-panel')).not.toBeInTheDocument()
    absentView.unmount()
  })

  it('shows start-review error, busy state, running state, and reviewed labels', () => {
    sage.startReview = { mutate: vi.fn(), isPending: false, error: new Error('cannot start') }
    const errorView = render(<PrReviewDetail pr={pr} />)
    expect(screen.getByText('cannot start')).toBeInTheDocument()
    errorView.unmount()

    sage.startReview = { mutate: vi.fn(), isPending: true, error: null }
    sage.prRun = null
    const busyView = render(<PrReviewDetail pr={pr} />)
    expect(screen.getByRole('button', { name: 'Review' })).toBeDisabled()
    busyView.unmount()

    sage.startReview = { mutate: vi.fn(), isPending: false, error: null }
    sage.prRun = makeRun({ status: 'running' })
    const runningView = render(<PrReviewDetail pr={pr} />)
    expect(screen.getByRole('button', { name: 'Reviewing…' })).toBeDisabled()
    runningView.unmount()

    sage.prRun = null
    const reviewedView = render(<PrReviewDetail pr={{ ...pr, reviewed: true }} />)
    expect(screen.getByRole('button', { name: 'Review again' })).toBeInTheDocument()
    reviewedView.unmount()
  })
})
