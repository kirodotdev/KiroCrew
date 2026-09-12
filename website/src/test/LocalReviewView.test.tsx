import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { renderWithProviders } from './helpers'
import LocalReviewView from '../apps/code-review-sage/views/LocalReviewView'
import type { LocalFinding, LocalReviewSession } from '../apps/code-review-sage/lib/types'

const api = vi.hoisted(() => ({
  localSessions: vi.fn(),
  localSession: vi.fn(),
  localReview: vi.fn(),
  localDisposition: vi.fn(),
  localFix: vi.fn(),
}))

vi.mock('../apps/code-review-sage/api', () => ({ sageApi: api }))

const reviewedFiles: NonNullable<LocalReviewSession['files']> = [
  {
    path: 'src/app.ts',
    status: 'modified',
    additions: 2,
    deletions: 1,
    hunks: [{
      old_start: 1,
      new_start: 1,
      lines: [
        { kind: 'context', content: 'const before = true', old_line: 1, new_line: 1 },
        { kind: 'delete', content: 'const oldValue = 1', old_line: 2, new_line: null },
        { kind: 'add', content: 'const newValue = 2', old_line: null, new_line: 2 },
        { kind: 'add', content: 'const infoValue = 3', old_line: null, new_line: 3 },
      ],
    }],
  },
]

const errorFinding: LocalFinding = {
  id: 'finding-error',
  file: 'src/app.ts',
  side: 'new',
  line: 2,
  end_line: 2,
  severity: 'error',
  category: 'correctness',
  title: 'Use the new value',
  message: 'The new value is not validated.',
  suggestion: 'Validate it before use.',
  confidence: 0.99,
  status: 'open',
}

const warningFinding: LocalFinding = {
  id: 'finding-warning',
  file: 'src/app.ts',
  side: 'new',
  line: 3,
  severity: 'warning',
  title: 'Consider naming',
  message: 'The name could be clearer.',
  status: 'accepted',
  user_instruction: 'Keep the public name.',
}

const infoFinding: LocalFinding = {
  id: 'finding-info',
  file: 'src/app.ts',
  side: 'new',
  line: 3,
  severity: 'info',
  title: 'Informational note',
  message: 'This is useful context.',
  status: 'open',
}

function makeSession(overrides: Partial<LocalReviewSession> = {}): LocalReviewSession {
  return {
    id: 'session-1',
    repository: '/repo',
    mode: 'all-working-tree',
    status: 'completed',
    revision: 'abc123',
    files: reviewedFiles,
    warning: 'Some generated files were skipped.',
    findings: [errorFinding, warningFinding, infoFinding],
    error: '',
    fix_runs: [],
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  api.localSessions.mockResolvedValue({ sessions: [{ id: 'session-1' }] })
  api.localSession.mockResolvedValue({ session: makeSession() })
  api.localReview.mockResolvedValue({ session: makeSession({ id: 'new-session', status: 'reviewing' }) })
  api.localDisposition.mockResolvedValue({ finding: errorFinding })
  api.localFix.mockResolvedValue({ fix_id: 'fix-1', status: 'queued', finding_ids: [] })
})

describe('LocalReviewView findings and fix flow', () => {
  it('renders anchored diffs, severity states, dispositions, selection, and fix guidance', async () => {
    const user = userEvent.setup()
    renderWithProviders(<LocalReviewView />)

    expect(await screen.findByRole('heading', { name: 'Local' })).toBeInTheDocument()
    expect(await screen.findByText('Some generated files were skipped.')).toBeInTheDocument()
    expect(screen.getByText('src/app.ts')).toBeInTheDocument()
    expect(screen.getByText('const oldValue = 1')).toBeInTheDocument()
    expect(screen.getByText('const newValue = 2')).toBeInTheDocument()
    expect(screen.getAllByText('Use the new value')).toHaveLength(2)
    expect(screen.getByText('Validate it before use.')).toBeInTheDocument()
    expect(screen.getByText('informational')).toBeInTheDocument()
    expect(screen.queryByText('Selected findings: 0')).not.toBeInTheDocument()

    const findingInputs = screen.getAllByRole('textbox', { name: 'Guidance for the fix agent (optional)' })
    await user.type(findingInputs[0]!, 'Please preserve the API')
    await user.click(screen.getAllByRole('button', { name: 'Dismiss' })[0]!)
    await waitFor(() => expect(api.localDisposition).toHaveBeenCalledWith(
      'session-1', 'finding-error', 'dismissed', 'Please preserve the API',
    ))

    const refreshedInputs = screen.getAllByRole('textbox', { name: 'Guidance for the fix agent (optional)' })
    await user.type(refreshedInputs[1]!, 'Accept this context')
    await user.click(screen.getAllByRole('button', { name: 'Accept' })[1]!)
    await waitFor(() => expect(api.localDisposition).toHaveBeenCalledWith(
      'session-1', 'finding-info', 'accepted', 'Accept this context',
    ))

    const findingCheckboxes = screen.getAllByRole('checkbox', { name: 'Select this finding' })
    await user.click(findingCheckboxes[0]!)
    await user.click(findingCheckboxes[0]!)
    await user.click(findingCheckboxes[0]!)
    await user.click(findingCheckboxes[2]!)
    const instruction = screen.getAllByRole('textbox', { name: 'Guidance for the fix agent (optional)' }).at(-1)!
    await user.type(instruction, 'Fix both findings together')
    await user.click(screen.getByRole('button', { name: 'Fix selected' }))
    await waitFor(() => expect(api.localFix).toHaveBeenCalledWith(
      'session-1', ['finding-error', 'finding-info'], 'Fix both findings together',
    ))
  })

  it('renders the reviewing status without findings', async () => {
    api.localSession.mockResolvedValueOnce({ session: makeSession({ status: 'reviewing' }) })
    renderWithProviders(<LocalReviewView />)

    expect(await screen.findAllByText('Reviewing local changes…')).toHaveLength(2)
    expect(screen.queryByText('No actionable findings')).not.toBeInTheDocument()
  })

  it('renders the completed no-findings state and failed status label', async () => {
    api.localSession.mockResolvedValueOnce({ session: makeSession({ findings: [], warning: null }) })
    renderWithProviders(<LocalReviewView />)

    expect(await screen.findByText('No actionable findings')).toBeInTheDocument()

    const failedView = renderWithProviders(<LocalReviewView />)
    api.localSession.mockResolvedValueOnce({ session: makeSession({ status: 'failed', findings: [], warning: null }) })
    await failedView.queryClient.refetchQueries({ queryKey: ['code-review-sage', 'local-session', 'session-1'] })
    expect(await screen.findByText('Failed')).toBeInTheDocument()
  })
})

describe('LocalReviewView review and fix mutations', () => {
  it('guards an empty repository, shows reviewing while starting, and accepts the result', async () => {
    const user = userEvent.setup()
    let resolveReview: (value: { session: LocalReviewSession }) => void = () => undefined
    api.localReview.mockReturnValue(new Promise<{ session: LocalReviewSession }>((resolve) => {
      resolveReview = resolve
    }))
    api.localSessions.mockResolvedValue({ sessions: [] })
    renderWithProviders(<LocalReviewView />)

    const start = await screen.findByRole('button', { name: 'Run local review' })
    fireEvent.submit(start.closest('form')!)
    expect(api.localReview).not.toHaveBeenCalled()

    await user.type(screen.getByRole('textbox', { name: 'Repository path' }), '/repo')
    await user.click(start)
    expect(await screen.findByRole('button', { name: 'Reviewing local changes…' })).toBeDisabled()
    resolveReview({ session: makeSession({ id: 'new-session', status: 'reviewing' }) })
    await waitFor(() => expect(api.localReview).toHaveBeenCalledWith('/repo', 'all-working-tree', undefined))
  })

  it('renders review failures and fix pending/error states', async () => {
    const user = userEvent.setup()
    api.localSessions.mockResolvedValue({ sessions: [] })
    api.localReview.mockRejectedValueOnce(new Error('review failed'))
    renderWithProviders(<LocalReviewView />)

    await user.type(await screen.findByRole('textbox', { name: 'Repository path' }), '/repo')
    await user.click(screen.getByRole('button', { name: 'Run local review' }))
    expect(await screen.findByText('This review failed')).toBeInTheDocument()
  })

  it('keeps the fix button pending and shows a localized fix failure', async () => {
    const user = userEvent.setup()
    let resolveFix: (value: { fix_id: string; status: string; finding_ids: string[] }) => void = () => undefined
    api.localFix.mockReturnValue(new Promise((resolve) => { resolveFix = resolve }))
    renderWithProviders(<LocalReviewView />)

    const checkbox = (await screen.findAllByRole('checkbox', { name: 'Select this finding' }))[0]!
    await user.click(checkbox)
    await user.click(screen.getByRole('button', { name: 'Fix selected' }))
    expect(screen.getByRole('button', { name: 'Fix selected' })).toBeDisabled()
    resolveFix({ fix_id: 'fix-2', status: 'queued', finding_ids: ['finding-error'] })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Fix selected' })).not.toBeInTheDocument())

    api.localFix.mockRejectedValueOnce(new Error('fix failed'))
    await user.click((await screen.findAllByRole('checkbox', { name: 'Select this finding' }))[0]!)
    await user.click(screen.getByRole('button', { name: 'Fix selected' }))
    expect(await screen.findByText('This review failed')).toBeInTheDocument()
  })
})

describe('LocalReviewView fix runs', () => {
  it('renders the last fix run status and its changed files', async () => {
    api.localSession.mockResolvedValue({
      session: makeSession({
        fix_runs: [
          { id: 'fix-1', status: 'completed', changed_files: [' M src/app.ts', 'A src/new.ts'] },
          { id: 'fix-2', status: 'running' },
        ],
      }),
    })
    renderWithProviders(<LocalReviewView />)

    // The LAST run is the one on screen: still running, so no file list yet.
    expect(await screen.findByText('Fix run')).toBeInTheDocument()
    expect(screen.getByText('Running')).toBeInTheDocument()
    expect(screen.queryByText('Changed files')).not.toBeInTheDocument()
  })

  it('keeps a failed fix run visible with its error instead of dropping it', async () => {
    // The run finished (session status is settled), so nothing re-renders after
    // the failure — if the panel only rendered while the session moved, the
    // error would never appear at all.
    api.localSession.mockResolvedValue({
      session: makeSession({
        status: 'completed',
        fix_runs: [{ id: 'fix-1', status: 'failed', error: 'the fix agent timed out' }],
      }),
    })
    renderWithProviders(<LocalReviewView />)

    expect(await screen.findByText('Fix run')).toBeInTheDocument()
    expect(await screen.findByText('Failed')).toBeInTheDocument()
    expect(screen.getByText('the fix agent timed out')).toBeInTheDocument()
    // No changed files to show when the run failed.
    expect(screen.queryByText('Changed files')).not.toBeInTheDocument()
  })

  it('lists the files a completed fix run changed', async () => {
    api.localSession.mockResolvedValue({
      session: makeSession({
        status: 'completed',
        fix_runs: [{ id: 'fix-1', status: 'completed', changed_files: [' M src/app.ts'] }],
      }),
    })
    renderWithProviders(<LocalReviewView />)

    expect(await screen.findByText('Changed files')).toBeInTheDocument()
    // The porcelain line is listed verbatim inside the run's <code>.
    expect(screen.getByText((_, element) =>
      element?.tagName === 'CODE' && element.textContent === ' M src/app.ts',
    )).toBeInTheDocument()
    expect(screen.queryByText('the fix agent timed out')).not.toBeInTheDocument()
  })
})
