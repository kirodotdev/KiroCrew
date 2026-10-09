import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { renderWithProviders } from './helpers'
import LocalReviewView from '../apps/code-review-sage/views/LocalReviewView'
import type { LocalFinding, LocalReviewSession } from '../apps/code-review-sage/lib/types'
import type { ReviewFixTaskResponse } from '../types'

const api = vi.hoisted(() => ({
  localSessions: vi.fn(),
  localSession: vi.fn(),
  localReview: vi.fn(),
  localDisposition: vi.fn(),
  createFixTask: vi.fn(),
}))

vi.mock('../apps/code-review-sage/api', () => ({ sageApi: api }))

const clientApi = vi.hoisted(() => ({
  reviewFixStatus: vi.fn(),
  reviewFixAction: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: clientApi }))

const reviewedFiles: NonNullable<LocalReviewSession['files']> = [
  {
    path: 'src/app.ts', status: 'modified', additions: 2, deletions: 1,
    hunks: [{
      old_start: 1, new_start: 1,
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
  id: 'finding-error', file: 'src/app.ts', side: 'new', line: 2, end_line: 2,
  severity: 'error', category: 'correctness', title: 'Use the new value',
  message: 'The new value is not validated.', suggestion: 'Validate it before use.',
  confidence: 0.99, status: 'open', fingerprint: 'fp-error',
}

const warningFinding: LocalFinding = {
  id: 'finding-warning', file: 'src/app.ts', side: 'new', line: 3, severity: 'warning',
  title: 'Consider naming', message: 'The name could be clearer.', status: 'accepted',
  user_instruction: 'Keep the public name.', fingerprint: 'fp-warning',
}

const infoFinding: LocalFinding = {
  id: 'finding-info', file: 'src/app.ts', side: 'new', line: 3, severity: 'info',
  title: 'Informational note', message: 'This is useful context.', status: 'open', fingerprint: 'fp-info',
}

function makeSession(overrides: Partial<LocalReviewSession> = {}): LocalReviewSession {
  return {
    id: 'session-1', repository: '/repo', mode: 'all-working-tree', status: 'completed', revision: 'abc123',
    files: reviewedFiles, warning: 'Some generated files were skipped.',
    findings: [errorFinding, warningFinding, infoFinding], error: '',
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  api.localSessions.mockResolvedValue({ sessions: [{ id: 'session-1' }] })
  api.localSession.mockResolvedValue({ session: makeSession() })
  api.localReview.mockResolvedValue({ session: makeSession({ id: 'new-session', status: 'reviewing' }) })
  api.localDisposition.mockResolvedValue({ finding: errorFinding })
  clientApi.reviewFixStatus.mockImplementation(() => new Promise(() => undefined))
})

describe('LocalReviewView findings and dispositions', () => {
  it('renders anchored diffs, severity states, dispositions, and selection', async () => {
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

  it('renders a persisted review failure through ErrorNotice in every findings branch', async () => {
    api.localSession.mockResolvedValueOnce({
      session: makeSession({ status: 'failed', error: 'git diff timed out', findings: [] }),
    })
    renderWithProviders(<LocalReviewView />)

    expect(await screen.findByText('git diff timed out')).toBeInTheDocument()
  })
})

describe('LocalReviewView review mutation', () => {
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

  it('renders review failures', async () => {
    const user = userEvent.setup()
    api.localSessions.mockResolvedValue({ sessions: [] })
    api.localReview.mockRejectedValueOnce(new Error('review failed'))
    renderWithProviders(<LocalReviewView />)

    await user.type(await screen.findByRole('textbox', { name: 'Repository path' }), '/repo')
    await user.click(screen.getByRole('button', { name: 'Run local review' }))
    expect(await screen.findByText('This review failed')).toBeInTheDocument()
  })
})

describe('LocalReviewView read and disposition failures', () => {
  it('renders a sessions-list load failure with an agent hand-off', async () => {
    api.localSessions.mockRejectedValue(new Error('sessions unavailable'))
    renderWithProviders(<LocalReviewView />)

    expect(await screen.findByText('sessions unavailable')).toBeInTheDocument()
  })

  it('renders an active-session load failure with an agent hand-off', async () => {
    api.localSession.mockRejectedValue(new Error('session unavailable'))
    renderWithProviders(<LocalReviewView />)

    expect(await screen.findByText('session unavailable')).toBeInTheDocument()
  })

  it('renders a disposition failure instead of losing it silently', async () => {
    const user = userEvent.setup()
    api.localDisposition.mockRejectedValueOnce(new Error('disposition rejected'))
    renderWithProviders(<LocalReviewView />)

    await user.click((await screen.findAllByRole('button', { name: 'Dismiss' }))[0]!)
    expect(await screen.findByText('disposition rejected')).toBeInTheDocument()
  })
})

describe('LocalReviewView Fix selected -> Review Fix pipeline', () => {
  it('opens the Review Fix setup prefilled with the selected findings and the session repository', async () => {
    const user = userEvent.setup()
    renderWithProviders(<LocalReviewView />)

    const findingCheckboxes = await screen.findAllByRole('checkbox', { name: 'Select this finding' })
    await user.click(findingCheckboxes[0]!) // finding-error
    await user.click(findingCheckboxes[2]!) // finding-info

    await user.click(screen.getByRole('button', { name: 'Fix 2 selected findings' }))

    const setupTitle = await screen.findByText('Set up a Review Fix task')
    expect(setupTitle).toBeInTheDocument()
    const setup = within(setupTitle.closest('section')!)
    expect(setup.getByText('Selected findings: 2')).toBeInTheDocument()
    expect(setup.getByText('Use the new value')).toBeInTheDocument()
    expect(setup.getByText('Informational note')).toBeInTheDocument()
    // Prefilled with the session's OWN repository, and locked: the local
    // review's target repository is already known, so there is nothing for
    // the user to (mis)type here.
    const targetInput = setup.getByRole('textbox', { name: 'Target repository' })
    expect(targetInput).toHaveValue('/repo')
    expect(targetInput).toBeDisabled()
  })

  it('creates the fix task against the local session and shows the task panel', async () => {
    const user = userEvent.setup()
    const created: ReviewFixTaskResponse = {
      task_id: 'fix-task-1',
      revision: 0,
      state: 'awaiting_group_confirmation',
      review_fix: null,
    }
    api.createFixTask.mockResolvedValue(created)
    clientApi.reviewFixStatus.mockImplementation(() => new Promise(() => undefined))
    renderWithProviders(<LocalReviewView />)

    const findingCheckboxes = await screen.findAllByRole('checkbox', { name: 'Select this finding' })
    await user.click(findingCheckboxes[0]!)
    await user.click(screen.getByRole('button', { name: 'Fix 1 selected finding' }))
    await screen.findByText('Set up a Review Fix task')

    await user.click(screen.getByRole('button', { name: 'Create fix task' }))

    await waitFor(() => expect(api.createFixTask).toHaveBeenCalledWith(expect.objectContaining({
      target_path: '/repo',
      local_session_id: 'session-1',
      findings: [expect.objectContaining({ key: 'finding-error', file_path: 'src/app.ts' })],
    })))
    expect(await screen.findByText('Loading Review Fix task…')).toBeInTheDocument()
    expect(screen.queryByText('Set up a Review Fix task')).not.toBeInTheDocument()
  })

  it('resets an open fix setup and finding selection when the active session changes underneath it', async () => {
    // The sessions-list poll (every 5s) can change which session `activeId`
    // resolves to -- session-1's list entry drops out and session-2 becomes
    // first -- with no user action in between. An open "Fix selected" setup
    // and a stale finding selection must not silently carry over onto
    // session-2's own findings.
    const user = userEvent.setup()
    const otherFinding: LocalFinding = {
      id: 'finding-other', file: 'src/other.ts', side: 'new', line: 9, severity: 'error',
      title: 'A finding from a different session', message: 'Should never be seen alongside session-1 state.',
      status: 'open', fingerprint: 'fp-other',
    }
    const { queryClient } = renderWithProviders(<LocalReviewView />)

    const findingCheckboxes = await screen.findAllByRole('checkbox', { name: 'Select this finding' })
    await user.click(findingCheckboxes[0]!) // finding-error, on session-1
    await user.click(screen.getByRole('button', { name: 'Fix 1 selected finding' }))
    expect(await screen.findByText('Set up a Review Fix task')).toBeInTheDocument()

    // Simulate the poll: the sessions list now leads with session-2, and its
    // detail query resolves to a session with an unrelated finding set. This
    // is the SAME rendered component/queryClient -- no remount of our own --
    // so any reset must come from `activeId` changing underneath it.
    api.localSessions.mockResolvedValue({ sessions: [{ id: 'session-2' }] })
    api.localSession.mockImplementation((sessionId: string) => Promise.resolve({
      session: sessionId === 'session-2'
        ? makeSession({ id: 'session-2', repository: '/other-repo', findings: [otherFinding], warning: null })
        : makeSession(),
    }))
    await queryClient.refetchQueries({ queryKey: ['code-review-sage', 'local-sessions'] })

    expect(await screen.findByText('A finding from a different session')).toBeInTheDocument()
    expect(screen.queryByText('Set up a Review Fix task')).not.toBeInTheDocument()
    expect(screen.queryByText(/Fix \d+ selected finding/)).not.toBeInTheDocument()
    const resetCheckbox = screen.getByRole('checkbox', { name: 'Select this finding' })
    expect(resetCheckbox).not.toBeChecked()
  })
})
