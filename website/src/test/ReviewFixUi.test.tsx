import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { ComponentProps } from 'react'

import { renderWithProviders } from './helpers'
import FindingCard from '../apps/code-review-sage/components/FindingCard'
import ReviewFixSetup from '../apps/code-review-sage/components/ReviewFixSetup'
import ReviewFixTaskPanel from '../components/ReviewFixTaskPanel'
import type {
  ReviewFixFindingSnapshot, ReviewFixGit, ReviewFixGroup, ReviewFixMetadata, ReviewFixModel,
  ReviewFixState, ReviewFixTarget, ReviewFixTaskResponse, ReviewFixValidation,
} from '../types'

const setupMocks = vi.hoisted(() => {
  class MockSageApiError extends Error {
    code: string
    constructor(message: string, code: string) {
      super(message)
      this.code = code
    }
  }

  return { createFixTask: vi.fn(), MockSageApiError }
})

vi.mock('../apps/code-review-sage/api', () => ({
  sageApi: { createFixTask: setupMocks.createFixTask }, SageApiError: setupMocks.MockSageApiError,
}))

vi.mock('../apps/code-review-sage/components/ReviewModelPicker', () => ({
  default: ({ value, onChange, disabled }: { value: string; onChange: (value: string) => void; disabled?: boolean }) => (
    <input aria-label="Review model" value={value} disabled={disabled} onChange={event => onChange(event.currentTarget.value)} />
  ),
}))

vi.mock('../components/SimpleSelect', () => ({
  default: ({ options, optionLabels, value, onChange, disabled, 'aria-label': ariaLabel }: {
    options: string[]; optionLabels?: string[]; value: string; onChange: (value: string) => void
    disabled?: boolean; 'aria-label'?: string
  }) => (
    <select aria-label={ariaLabel} disabled={disabled} value={value} onChange={event => onChange(event.currentTarget.value)}>
      {options.map((option, index) => (
        <option key={option} value={option}>{optionLabels?.[index] ?? option}</option>
      ))}
    </select>
  ),
}))

const finding: ReviewFixFindingSnapshot = {
  key: 'change-1:finding:0', title: 'Fix target', severity: 'red', body: 'The target is inconsistent.',
  file_path: 'src/example.py', line: 12, end_line: 14,
  fingerprint: 'finding-fingerprint', suggested_fix: 'Align the target behavior.',
}

const target: ReviewFixTarget = {
  mode: 'current_branch', repo_root: '/repo', target_path: '/repo', target_ref: 'main', branch_name: 'main',
  head_sha: 'target-head-sha', dirty_fingerprint: 'target-fingerprint', tracked_paths: [], untracked_paths: [],
  upstream: 'origin/main', remote: 'origin',
}

const git: ReviewFixGit = {
  candidate_worktree_path: '/tmp/candidate', candidate_branch: 'review-fix/task-1', candidate_ref: 'candidate-head-sha',
  destination_worktree_path: '/repo', destination_branch: 'main', proposed_branch: 'review-fix/task-1', confirmed_branch: '',
  remote: 'origin', upstream: 'origin/main', push_preview: {}, push_result: {}, rereview_run_id: '',
}

const model: ReviewFixModel = {
  requested_model: 'auto', provider: 'acp', resolved_model_id: 'served-model',
  advertised_model_ids: ['served-model'], resolved_at: 1,
}

function validation(kind: string, groupRevision = 2): ReviewFixValidation {
  return {
    validation_id: `${kind}-validation`, group_id: 'group-1', group_revision: groupRevision, kind,
    command: kind === 'test' ? ['pytest', '-q'] : ['npm', 'run', 'build'],
    exit_code: 0, passed: true, started_at: 1, finished_at: 2, duration_secs: 1,
  }
}

function group(overrides: Partial<ReviewFixGroup> = {}): ReviewFixGroup {
  return {
    group_id: 'group-1', finding_keys: [finding.key], hard_edges: [], soft_edges: [], reasons: [],
    affected_files: ['src/example.py'], hard: true, state: 'ready_to_apply', revision: 2,
    candidate_patch_id: 'patch-1', candidate_base_sha: 'target-head-sha', candidate_head_sha: 'candidate-head-sha',
    patch_path: '/tmp/candidate.patch', diff_path: '/tmp/candidate.diff', validation_runs: [],
    apply_confirmed: false, applied_at: 0, commit_hash: '', commit_message: '',
    ...overrides,
  }
}

function response(
  state: ReviewFixState,
  groupOverrides: Partial<ReviewFixGroup> = {},
  modelOverrides: Partial<ReviewFixModel> = {},
): ReviewFixTaskResponse {
  const metadata: ReviewFixMetadata = {
    review_run_id: 'sage-run-1', pr_url: 'https://github.com/example/repo/pull/42', source_head_sha: 'source-head-sha',
    selected_finding_keys: [finding.key], finding_snapshots: [finding], state, revision: 7, target,
    model: { ...model, ...modelOverrides }, groups: [group(groupOverrides)], git, blocked_reason: '', attempts: {},
    logs: [], diff_paths: [], artifact_paths: [], audit_log: [], created_at: 1, updated_at: 2,
  }
  return { task_id: 'task-1', revision: 7, state, review_fix: metadata }
}

function renderSetup(overrides: Partial<ComponentProps<typeof ReviewFixSetup>> = {}) {
  return renderWithProviders(
    <ReviewFixSetup
      findings={[finding]}
      reviewRunId="sage-run-1"
      prUrl="https://github.com/example/repo/pull/42"
      sourceHeadSha="source-head-sha"
      model="served-model"
      onModelChange={vi.fn()}
      onCreated={vi.fn()}
      onClose={vi.fn()}
      {...overrides}
    />,
  )
}

function renderPanel(
  transport: ReturnType<typeof transportFor> | { status: ReturnType<typeof vi.fn>; action: ReturnType<typeof vi.fn> },
  props: Partial<ComponentProps<typeof ReviewFixTaskPanel>> = {},
) {
  return renderWithProviders(<ReviewFixTaskPanel taskId="task-1" transport={transport} {...props} />)
}

function transportFor(value: ReviewFixTaskResponse) {
  return { status: vi.fn().mockResolvedValue(value), action: vi.fn().mockResolvedValue(value) }
}

describe('Review Fix finding selection', () => {
  it('keeps post and fix selection independent', async () => {
    const onPostToggle = vi.fn()
    const onFixToggle = vi.fn()
    renderWithProviders(
      <FindingCard
        finding={{
          dimension: 'correctness', severity: 'red', headline: finding.title,
          observation: finding.body, file: finding.file_path, line: finding.line,
        }}
        selectable selected={false} onToggle={onPostToggle} label="src/example.py:12"
        fixSelectable fixSelected={false} onToggleFix={onFixToggle}
      />,
    )

    await userEvent.click(screen.getByRole('checkbox', { name: /to fix/i }))
    expect(onFixToggle).toHaveBeenCalledTimes(1)
    expect(onPostToggle).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('checkbox', { name: /to draft/i }))
    expect(onPostToggle).toHaveBeenCalledTimes(1)
  })

  it('does not render fix affordances when the row is ineligible', () => {
    renderWithProviders(
      <FindingCard
        finding={{ severity: 'yellow', headline: finding.title }}
        fixSelectable={false}
        onFix={vi.fn()}
      />,
    )

    expect(screen.queryByRole('checkbox', { name: /to fix/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /ask to fix/i })).not.toBeInTheDocument()
  })
})

describe('ReviewFixSetup', () => {
  it('maps must-fix severities from both finding vocabularies to the shared label and color', async () => {
    // Report findings spell must-fix 'red'; local-review findings spell it
    // 'error'. Both must land on the danger badge with the "must-fix" word --
    // the regression this guards is 'error' silently painting yellow because
    // only 'red' was checked.
    renderSetup({
      findings: [
        { ...finding, key: 'f-red', severity: 'red' },
        { ...finding, key: 'f-error', severity: 'error' },
        { ...finding, key: 'f-yellow', severity: 'yellow' },
      ],
    })

    const mustFixBadges = screen.getAllByText('must-fix')
    expect(mustFixBadges).toHaveLength(2)
    expect(screen.getAllByText('should-fix')).toHaveLength(1)
    expect(screen.queryByText('red')).not.toBeInTheDocument()
    expect(screen.queryByText('error')).not.toBeInTheDocument()
  })

  it('sends the selected findings and target settings in the create payload', async () => {
    setupMocks.createFixTask.mockResolvedValue(response('awaiting_group_confirmation'))
    const onCreated = vi.fn()
    renderSetup({ onCreated })

    await userEvent.type(screen.getByRole('textbox', { name: /target repository/i }), '/repo')
    await userEvent.click(screen.getByRole('button', { name: /create fix task/i }))

    await waitFor(() => expect(setupMocks.createFixTask).toHaveBeenCalledTimes(1))
    expect(setupMocks.createFixTask).toHaveBeenCalledWith({
      target_path: '/repo', findings: [finding], review_run_id: 'sage-run-1',
      pr_url: 'https://github.com/example/repo/pull/42', source_head_sha: 'source-head-sha',
      target_mode: 'current_branch', model: 'served-model',
    })
    expect(onCreated).toHaveBeenCalledTimes(1)
    expect(onCreated.mock.calls[0]?.[0]).toEqual(response('awaiting_group_confirmation'))
  })

  it.each([
    ['target_required', 'Enter a target repository path.'],
    ['target_denied', 'This target path is not allowed.'],
    ['findings_required', 'No eligible findings are selected.'],
    ['blocked_model_resolution', 'Model resolution is blocked. Resolve it before continuing.'],
    ['unknown_code', 'Could not create the fix task.'],
  ])('maps %s creation errors to localized copy', async (code, expected) => {
    setupMocks.createFixTask.mockRejectedValue(
      code === 'unknown_code'
        ? new Error('backend failure')
        : new setupMocks.MockSageApiError('backend failure', code),
    )
    renderSetup()

    await userEvent.type(screen.getByRole('textbox', { name: /target repository/i }), '/repo')
    await userEvent.click(screen.getByRole('button', { name: /create fix task/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(expected)
    setupMocks.createFixTask.mockReset()
  })

  it('disables creation without a target or eligible findings and closes on request', async () => {
    const onClose = vi.fn()
    renderSetup({ findings: [], onClose })

    expect(screen.getByRole('button', { name: /create fix task/i })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: /close review fix setup/i }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('shows the pending state and forwards model changes while creating', async () => {
    let resolveCreate: (value: ReviewFixTaskResponse) => void = () => undefined
    setupMocks.createFixTask.mockReturnValue(new Promise<ReviewFixTaskResponse>((resolve) => {
      resolveCreate = resolve
    }))
    const onModelChange = vi.fn()
    const onCreated = vi.fn()
    renderSetup({ onModelChange, onCreated })

    await userEvent.clear(screen.getByRole('textbox', { name: 'Review model' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Review model' }), { target: { value: 'new-model' } })
    expect(onModelChange).toHaveBeenLastCalledWith('new-model')
    await userEvent.type(screen.getByRole('textbox', { name: /target repository/i }), '/repo')
    await userEvent.click(screen.getByRole('button', { name: /create fix task/i }))

    expect(await screen.findByRole('button', { name: /creating fix task/i })).toBeDisabled()
    expect(screen.getByRole('textbox', { name: /target repository/i })).toBeDisabled()
    expect(screen.getByRole('textbox', { name: 'Review model' })).toBeDisabled()

    const created = response('awaiting_group_confirmation')
    resolveCreate(created)
    await waitFor(() => expect(onCreated.mock.calls[0]?.[0]).toEqual(created))
  })
})

describe('ReviewFixTaskPanel', () => {
  it.each([
    { label: 'no passing validation', validationRuns: [], enabled: false },
    { label: 'the test command passes — the build command is optional', validationRuns: [validation('test')], enabled: true },
  ])('Apply reflects validation state: $label', async ({ validationRuns, enabled }) => {
    const transport = transportFor(response('ready_to_apply', { validation_runs: validationRuns }))
    renderPanel(transport)

    const applyButton = await screen.findByRole('button', { name: 'Apply' })
    const hint = 'Run validation with a passing test command to enable Apply.'
    if (enabled) {
      expect(applyButton).toBeEnabled()
      expect(screen.queryByText(hint)).not.toBeInTheDocument()
      // Build is optional: a test-only pass is a plain pass, not "build still needed".
      expect(screen.getByText('Validation: test validation passed')).toBeInTheDocument()
      expect(screen.queryByText(/build still needed/i)).not.toBeInTheDocument()
    } else {
      expect(applyButton).toBeDisabled()
      expect(screen.getByText(hint)).toBeInTheDocument()
    }
  })

  it('sends CAS-protected apply after test and build pass', async () => {
    const transport = transportFor(response('ready_to_apply', { validation_runs: [validation('test'), validation('build')] }))
    renderPanel(transport)

    await userEvent.click(await screen.findByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(transport.action).toHaveBeenCalledTimes(1))
    expect(transport.action).toHaveBeenCalledWith('task-1', expect.objectContaining({
      action: 'apply_group',
      expected_revision: 7,
      target_fingerprint: 'target-fingerprint',
      confirmed: true,
      confirmation_intent: 'apply_review_fix_group',
      group_id: 'group-1',
      expected_group_revision: 2,
    }))
  })

  it.each([
    {
      label: 'requires a test command before running validation, but not a build command',
      buildCommand: undefined,
      expected: { action: 'validate_group', test_command: ['pytest', '-q'], expected_revision: 7, expected_group_revision: 2 },
      assertNoBuildKey: true,
    },
    {
      label: 'sends build_command only when a build command is entered',
      buildCommand: 'npm run build',
      expected: { action: 'validate_group', test_command: ['pytest', '-q'], build_command: ['npm', 'run', 'build'] },
      assertNoBuildKey: false,
    },
  ])('$label', async ({ buildCommand, expected, assertNoBuildKey }) => {
    const transport = transportFor(response('awaiting_validation', { state: 'proposed' }))
    renderPanel(transport)

    const runValidation = await screen.findByRole('button', { name: 'Run validation' })
    expect(runValidation).toBeDisabled()
    await userEvent.type(screen.getByRole('textbox', { name: 'Test command' }), 'pytest -q')
    expect(runValidation).toBeEnabled()
    if (buildCommand) {
      await userEvent.type(screen.getByRole('textbox', { name: 'Build command' }), buildCommand)
    }

    await userEvent.click(runValidation)
    await waitFor(() => expect(transport.action).toHaveBeenCalledTimes(1))
    expect(transport.action).toHaveBeenCalledWith('task-1', expect.objectContaining(expected))
    if (assertNoBuildKey) {
      // No build_command key at all -- the backend distinguishes "no build
      // step" from "empty build step" by the key's absence.
      expect(transport.action.mock.calls[0][1]).not.toHaveProperty('build_command')
    }
  })

  it('hides Validate until the group has a captured candidate patch', async () => {
    const transport = transportFor(response('awaiting_validation', { state: 'proposed', candidate_patch_id: '' }))
    renderPanel(transport)

    await screen.findByText('group-1')
    expect(screen.queryByRole('button', { name: 'Run validation' })).not.toBeInTheDocument()
  })

  it.each([
    { label: 'proposed: execution unavailable until grouping is confirmed', state: 'proposed', confirmed: false },
    { label: 'confirmed: starts execution', state: 'confirmed', confirmed: true },
  ])('$label', async ({ state, confirmed }) => {
    const transport = transportFor(response('awaiting_group_confirmation', { state }))
    renderPanel(transport)

    if (confirmed) {
      const start = await screen.findByRole('button', { name: 'Start execution' })
      expect(screen.queryByRole('button', { name: 'Pause' })).not.toBeInTheDocument()

      await userEvent.click(start)
      await waitFor(() => expect(transport.action).toHaveBeenCalledTimes(1))
      expect(transport.action).toHaveBeenCalledWith('task-1', {
        action: 'resume', expected_revision: 7, target_fingerprint: 'target-fingerprint', confirmed: true,
      })
    } else {
      expect(await screen.findByRole('button', { name: 'Confirm grouping' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Start execution' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Pause' })).not.toBeInTheDocument()
    }
  })

  it.each(['awaiting_validation', 'ready_to_apply'] as const)('does not show Pause in %s', async (state) => {
    const transport = transportFor(response(state))
    renderPanel(transport)

    expect(await screen.findByRole('heading', { name: 'Review Fix task' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Pause' })).not.toBeInTheDocument()
  })

  it('offers model resolution when execution is blocked', async () => {
    const transport = transportFor(response('blocked_model_resolution', {}, { requested_model: 'auto', resolved_model_id: '' }))
    renderPanel(transport)

    const resolve = await screen.findByRole('button', { name: 'Resolve model' })
    await userEvent.click(resolve)
    await waitFor(() => expect(transport.action).toHaveBeenCalledTimes(1))
    expect(transport.action).toHaveBeenCalledWith('task-1', expect.objectContaining({
      action: 'resolve_model',
      model: 'auto',
      expected_revision: 7,
      target_fingerprint: 'target-fingerprint',
    }))
  })

  it('renders loading while the task status is pending', () => {
    const transport = {
      status: vi.fn(() => new Promise<ReviewFixTaskResponse>(() => undefined)),
      action: vi.fn().mockResolvedValue(response('draft')),
    }
    renderPanel(transport)

    expect(screen.getByText('Loading Review Fix task…')).toBeInTheDocument()
  })

  it('renders load failures and missing metadata as alerts', async () => {
    const failedTransport = transportFor(response('failed'))
    failedTransport.status.mockRejectedValueOnce(new Error('network down'))
    const failedView = renderPanel(failedTransport)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the Review Fix task.')
    failedView.unmount()

    const missing = { ...response('failed'), review_fix: null }
    const missingTransport = transportFor(missing)
    renderPanel(missingTransport, { taskId: 'task-2' })
    expect(await screen.findByRole('alert')).toHaveTextContent('Review Fix metadata is unavailable.')
  })

  it('shows blocked target details and opens the task runner', async () => {
    const task = response('blocked_dirty_overlap')
    task.review_fix!.target.tracked_paths = ['src/example.py', 'src/other.py']
    const transport = transportFor(task)
    const onOpenTaskRunner = vi.fn()
    renderPanel(transport, { onOpenTaskRunner })

    expect(await screen.findAllByText('Blocked: local changes overlap')).toHaveLength(2)
    expect(screen.getByText('src/example.py, src/other.py')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Open in Task Runner' }))
    expect(onOpenTaskRunner).toHaveBeenCalledWith('task-1')
  })

  it('offers grouping actions and locks hard-group assignment controls', async () => {
    const transport = transportFor(response('awaiting_group_confirmation', { state: 'proposed' }))
    renderPanel(transport)

    expect(await screen.findByRole('combobox', { name: 'Group for finding' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Save grouping' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm grouping' }))
    await waitFor(() => expect(transport.action).toHaveBeenCalledTimes(2))
    expect(transport.action.mock.calls.map(([id, input]) => [id, input.action])).toEqual([
      ['task-1', 'edit_soft_grouping'], ['task-1', 'confirm_grouping'],
    ])
  })

  it('renders failed validation runs and submits normalized commands', async () => {
    const failedRun = { ...validation('test'), passed: false, artifact_path: '/tmp/test.log' }
    const transport = transportFor(response('awaiting_validation', { state: 'validating', validation_runs: [failedRun] }))
    renderPanel(transport)

    expect(await screen.findByText('test validation failed')).toBeInTheDocument()
    expect(screen.getByText('/tmp/test.log')).toBeInTheDocument()
    await userEvent.type(screen.getByRole('textbox', { name: 'Test command' }), '  pytest   -q  ')
    await userEvent.type(screen.getByRole('textbox', { name: 'Build command' }), ' npm run build ')
    await userEvent.click(screen.getByRole('button', { name: 'Run validation' }))
    await waitFor(() => expect(transport.action).toHaveBeenCalledTimes(1))
    expect(transport.action).toHaveBeenCalledWith('task-1', expect.objectContaining({
      action: 'validate_group', test_command: ['pytest', '-q'], build_command: ['npm', 'run', 'build'],
    }))
  })

  it.each([
    ['stale_revision', 'This task changed elsewhere.'],
    ['revision_conflict', 'This task changed elsewhere.'],
    ['stale_group_revision', 'This task changed elsewhere.'],
    ['confirmation_required', 'Confirmation is required.'],
    ['unexpected', 'The action could not be completed.'],
  ])('maps task action error %s', async (code, expected) => {
    const transport = transportFor(response('running', { state: 'executing' }))
    transport.action.mockRejectedValueOnce({ code })
    renderPanel(transport)

    await userEvent.click(await screen.findByRole('button', { name: 'Pause' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(expected)
  })

  it('surfaces the backend message of a conflict it has no code mapped for', async () => {
    // A push whose preview went stale arrives as a bare 409: the code is inside
    // the ApiError body, so the message is what the user can act on.
    const transport = transportFor(response('awaiting_push', { state: 'committed' }))
    transport.action.mockRejectedValueOnce({ status: 409, message: 'push preview is stale' })
    renderPanel(transport)

    await userEvent.click(await screen.findByRole('button', { name: 'Push preview' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('push preview is stale')
  })

  it('hides Discard while execution is running', async () => {
    const transport = transportFor(response('running', { state: 'executing' }))
    renderPanel(transport)

    expect(await screen.findByRole('button', { name: 'Pause' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Discard candidate' })).not.toBeInTheDocument()
  })

  it.each([
    {
      label: 'discard',
      taskResponse: response('failed'),
      buttonName: 'Discard candidate',
      // The dialog restates the consequence before anything is sent.
      dialogText: "Any fixes already applied in this task's candidate that have not been pushed will be lost.",
      action: 'discard_candidate',
      confirmationIntent: 'discard_review_fix_candidate',
    },
    {
      label: 'push',
      taskResponse: response('awaiting_push', { state: 'committed' }),
      buttonName: 'Push',
      // Remote and branch are quoted separately, per the destructive-confirm pin.
      dialogText: 'Push to “origin”/“review-fix/task-1”?',
      action: 'push',
      confirmationIntent: 'push_review_fix_group',
    },
  ])('sends $label only after the confirmation dialog is accepted', async ({ taskResponse, buttonName, dialogText, action, confirmationIntent }) => {
    const transport = transportFor(taskResponse)
    renderPanel(transport)

    await userEvent.click(await screen.findByRole('button', { name: buttonName }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(dialogText)
    expect(transport.action).not.toHaveBeenCalled()

    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(transport.action).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: buttonName }))
    const reopened = await screen.findByRole('dialog')
    await userEvent.click(within(reopened).getByRole('button', { name: buttonName }))
    await waitFor(() => expect(transport.action).toHaveBeenCalledTimes(1))
    expect(transport.action).toHaveBeenCalledWith('task-1', expect.objectContaining({
      action, confirmation_intent: confirmationIntent, confirmed: true,
    }))
  })

  it('retries a blocked candidate', async () => {
    const transport = transportFor(response('blocked_dirty_overlap'))
    renderPanel(transport)

    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(transport.action).toHaveBeenCalledTimes(1))
    expect(transport.action.mock.calls.map(([, input]) => input.action)).toEqual(['retry'])
  })

  it('commits an applied group and pushes a committed group', async () => {
    const commitTransport = transportFor(response('awaiting_commit', { state: 'applied' }))
    const commitView = renderPanel(commitTransport)
    await userEvent.type(await screen.findByRole('textbox', { name: 'Commit message' }), 'Apply fix')
    // Refresh diff is gone once the group is applied: the backend only
    // accepts capture_group_patch during the validation phases, so the old
    // refresh-then-commit sequence here pinned an action the backend refuses.
    // The row holds the message input's Commit button alone.
    expect(screen.queryByRole('button', { name: 'More actions' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Commit' }))
    await waitFor(() => expect(commitTransport.action).toHaveBeenCalledTimes(1))
    expect(commitTransport.action.mock.calls.map(([, input]) => input.action)).toEqual(['commit_group'])
    commitView.unmount()

    const pushTransport = transportFor(response('awaiting_push', { state: 'committed' }))
    renderPanel(pushTransport, { taskId: 'task-2' })
    await userEvent.click(await screen.findByRole('button', { name: 'Push preview' }))
    await userEvent.click(screen.getByRole('button', { name: 'Push' }))
    // Push is behind the confirmation dialog; only confirming sends it.
    const pushDialog = await screen.findByRole('dialog')
    await userEvent.click(within(pushDialog).getByRole('button', { name: 'Push' }))
    await waitFor(() => expect(pushTransport.action).toHaveBeenCalledTimes(2))
    expect(pushTransport.action.mock.calls.map(([, input]) => input.action)).toEqual(['push_preview', 'push'])
  })

  it.each([
    {
      label: 'is committed (not yet awaiting_push), keeping refresh in overflow',
      // Task committed (not yet awaiting_push), group committed: the preview must
      // still be offered — the old panel showed NO actions in this window, so a
      // user who had committed every group could not request the push preview
      // that unlocks the push. Push itself stays push-phase-only (the backend
      // refuses it before an approved preview exists). Refresh is gone: the
      // group is committed and the backend no longer accepts a re-capture.
      taskResponse: response('committed', { state: 'committed' }),
      showPush: false,
      absentButton: 'More actions',
    },
    {
      label: 'is awaiting_push, exactly preview and push with refresh in overflow',
      taskResponse: response('awaiting_push', { state: 'committed' }),
      showPush: true,
      // max-two-buttons-per-row: the third action (refresh) is a menu item.
      absentButton: 'Refresh diff',
    },
  ])('shows preview while the task %s', async ({ taskResponse, showPush, absentButton }) => {
    const transport = transportFor(taskResponse)
    renderPanel(transport)

    expect(await screen.findByRole('button', { name: 'Push preview' })).toBeInTheDocument()
    if (showPush) {
      expect(screen.getByRole('button', { name: 'Push' })).toBeInTheDocument()
    } else {
      expect(screen.queryByRole('button', { name: 'Push' })).not.toBeInTheDocument()
    }
    expect(screen.queryByRole('button', { name: absentButton })).not.toBeInTheDocument()
    // Before push_preview has been requested, the preview card itself is omitted.
    expect(screen.queryByText(/^To\s/)).not.toBeInTheDocument()
  })

  it('renders the push preview: commits, files, remote/branch, and a diverged warning', async () => {
    const task = response('awaiting_push', { state: 'committed' })
    task.review_fix!.git = {
      ...task.review_fix!.git,
      push_preview: {
        remote: 'origin', branch: 'review-fix/task-1', upstream: '', preview_base: 'refs/remotes/origin/HEAD',
        commits: ['abc1234 Fix the target', 'def5678 Tidy up'], files: ['src/example.py', 'src/other.py'], diverged: true,
      },
    }
    const transport = transportFor(task)
    renderPanel(transport)

    expect(await screen.findByText('To “origin”/“review-fix/task-1”')).toBeInTheDocument()
    expect(screen.getByText('abc1234')).toBeInTheDocument()
    expect(screen.getByText('Fix the target')).toBeInTheDocument()
    expect(screen.getByText('def5678')).toBeInTheDocument()
    expect(screen.getByText('src/example.py, src/other.py')).toBeInTheDocument()
    expect(screen.getByText(/diverged/i)).toBeInTheDocument()

    // The dialog where Push is approved must show the same preview, not just
    // the card on the page: the user approves exactly what they are shown.
    await userEvent.click(screen.getByRole('button', { name: 'Push' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Fix the target')).toBeInTheDocument()
    expect(within(dialog).getByText('src/example.py, src/other.py')).toBeInTheDocument()
    expect(within(dialog).getByText(/diverged/i)).toBeInTheDocument()
  })

  it('labels the target and candidate mono lines', async () => {
    const transport = transportFor(response('ready_to_apply'))
    renderPanel(transport)

    await screen.findByText('Current branch')
    expect(screen.getAllByText('Path:')).toHaveLength(2)
    expect(screen.getAllByText('Branch:')).toHaveLength(2)
    expect(screen.getAllByText('Commit:')).toHaveLength(2)
  })

  it('hides the push affordances until a group is committed', async () => {
    const transport = transportFor(response('awaiting_push', { state: 'applied' }))
    renderPanel(transport)

    // Await the loaded task (the panel renders an empty shell until status
    // resolves) before asserting absence — findByRole would WAIT for the
    // element and make a not-in-document assertion fail.
    await screen.findByText('group-1')
    expect(screen.queryByRole('button', { name: 'Push preview' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Push' })).not.toBeInTheDocument()
  })

  it.each([
    {
      label: 'in awaiting_validation without a diff_path',
      // Regression: the overflow menu (the only entry point to Refresh diff)
      // used to be gated on hasDiff — but capture_group_patch never sets
      // diff_path/patch_path (only apply does), so before Apply hasDiff is
      // always false and the menu never rendered. Gate on the group lifecycle
      // instead: a pre-Apply group without a diff still exposes re-capture.
      taskResponse: response('awaiting_validation', {
        state: 'ready_to_apply', diff_path: '', patch_path: '',
        validation_runs: [validation('test'), validation('build')],
      }),
    },
    {
      label: 'for a ready_to_apply group with a diff_path',
      // The default fixture group IS applied-eligible (ready_to_apply, diff
      // present): refresh must stay available there too — a candidate worktree
      // that moved after validation needs a re-capture before Apply.
      taskResponse: response('ready_to_apply', { validation_runs: [validation('test'), validation('build')] }),
    },
  ])('keeps re-capture reachable $label', async ({ taskResponse }) => {
    const transport = transportFor(taskResponse)
    renderPanel(transport)

    await screen.findByText('group-1')
    const overflow = screen.getByRole('button', { name: 'More actions' })
    fireEvent.keyDown(overflow, { key: 'Enter' })
    expect(await screen.findByRole('menuitem', { name: /Refresh diff/i })).toBeInTheDocument()
  })

  it('hides re-capture during awaiting_push', async () => {
    // Push phase fills the row (preview + push, max-two-buttons rule) and the
    // committed diff is final, so the menu drops the refresh item entirely.
    const transport = transportFor(response('awaiting_push', { state: 'committed' }))
    renderPanel(transport)

    await screen.findByText('group-1')
    expect(screen.queryByRole('button', { name: 'More actions' })).not.toBeInTheDocument()
  })

  it('resumes a paused task and reviews a pushed task again', async () => {
    const pausedTransport = transportFor(response('paused'))
    renderPanel(pausedTransport)
    await userEvent.click(await screen.findByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(pausedTransport.action).toHaveBeenCalledWith('task-1', expect.objectContaining({ action: 'resume' })))

    let resolveReview: () => void = () => undefined
    const onReviewAgain = vi.fn(() => new Promise<void>((resolve) => { resolveReview = resolve }))
    const pushedTransport = transportFor(response('pushed', { state: 'committed' }))
    renderPanel(pushedTransport, { taskId: 'task-2', onReviewAgain })
    const reviewAgain = await screen.findByRole('button', { name: 'Review again' })
    await userEvent.click(reviewAgain)
    expect(reviewAgain).toBeDisabled()
    expect(onReviewAgain).toHaveBeenCalledTimes(1)
    resolveReview()
    await waitFor(() => expect(reviewAgain).toBeEnabled())
  })

  it('keeps a rejected review-again in view instead of losing it silently', async () => {
    const onReviewAgain = vi.fn().mockRejectedValue(new Error('review already running'))
    const pushedTransport = transportFor(response('pushed', { state: 'committed' }))
    renderPanel(pushedTransport, { taskId: 'task-2', onReviewAgain })
    const reviewAgain = await screen.findByRole('button', { name: 'Review again' })
    await userEvent.click(reviewAgain)

    expect(await screen.findByRole('alert')).toHaveTextContent('The action could not be completed.')
    await waitFor(() => expect(reviewAgain).toBeEnabled())
  })

  it('uses target fallbacks, collapses groups, and omits discard when done', async () => {
    const task = response('done', { state: 'committed' })
    task.review_fix!.target = { ...task.review_fix!.target, branch_name: '', target_ref: 'target-ref', head_sha: '' }
    task.review_fix!.git = { ...task.review_fix!.git, candidate_worktree_path: '', candidate_branch: '', candidate_ref: '' }
    const transport = transportFor(task)
    renderPanel(transport)

    expect(await screen.findByText('Current branch')).toBeInTheDocument()
    expect(screen.getByText('target-ref')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Discard candidate' })).not.toBeInTheDocument()
    const groupButton = screen.getByRole('button', { name: /group-1/ })
    expect(groupButton).toHaveAttribute('aria-expanded', 'true')
    await userEvent.click(groupButton)
    expect(groupButton).toHaveAttribute('aria-expanded', 'false')
  })
})
