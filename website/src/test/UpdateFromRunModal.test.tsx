import { beforeEach, describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { ApiError } from '../api/apiError'

const mocks = vi.hoisted(() => ({
  workflowDefinitions: vi.fn(),
  previewWorkflowRunUpdate: vi.fn(),
  updateWorkflowRunDefinition: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mocks }))
// SimpleSelect is Radix-backed off touch; a native stand-in keeps the test on
// this dialog's wiring (options in, value out).
vi.mock('../components/SimpleSelect', () => ({
  default: ({
    options,
    value,
    onChange,
    'aria-label': ariaLabel,
  }: {
    options: string[]
    value: string
    onChange: (v: string) => void
    'aria-label'?: string
  }) => (
    <select aria-label={ariaLabel} value={value} onChange={e => onChange(e.target.value)}>
      <option value="" aria-label="empty" />
      {options.map(o => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  ),
}))
import { renderWithProviders } from './helpers'
import UpdateFromRunModal from '../components/workflows/UpdateFromRunModal'

const RUN_ID = 'wf_000042'

function preview(revision: number, overrides: Record<string, unknown> = {}) {
  return {
    target: { id: 'wfd_hunt', slug: 'bug-hunt', name: 'Bug Hunt', revision, format: 'python' },
    diff: '⋯ lines 1-2 ⋯\n-    return 1\n+    return 2',
    unchanged: false,
    format_mismatch: false,
    run_source_sha256: 'abc123',
    ...overrides,
  }
}

/** Apply also waits on the definitions list, so the preview alone does not make it clickable. */
async function clickApplyWhenEnabled() {
  await waitFor(() => expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
}

beforeEach(() => {
  mocks.workflowDefinitions.mockReset()
  mocks.previewWorkflowRunUpdate.mockReset()
  mocks.updateWorkflowRunDefinition.mockReset()
  mocks.workflowDefinitions.mockResolvedValue({
    definitions: [
      { id: 'wfd_hunt', slug: 'bug-hunt', name: 'Bug Hunt' },
      { id: 'wfd_other', slug: 'other', name: 'Other' },
    ],
  })
})

describe('UpdateFromRunModal', () => {
  it('previews the suggested workflow and appends the revision it showed', async () => {
    mocks.previewWorkflowRunUpdate.mockResolvedValue(preview(3))
    const definition = { id: 'wfd_hunt', slug: 'bug-hunt', revision: 4 }
    mocks.updateWorkflowRunDefinition.mockResolvedValue({ ok: true, definition })
    const onUpdated = vi.fn()
    renderWithProviders(<UpdateFromRunModal runId={RUN_ID} onClose={vi.fn()} onUpdated={onUpdated} />)

    expect(await screen.findByTestId('workflow-update-revision')).toHaveTextContent('Revision 3 → 4')
    expect(mocks.previewWorkflowRunUpdate).toHaveBeenCalledWith(RUN_ID, '')
    expect(screen.getAllByTestId('change-card-diff-line').map(l => l.dataset.kind)).toEqual(['remove', 'add'])
    await waitFor(() => expect(screen.getByLabelText('Saved workflow')).toHaveValue('bug-hunt'))

    await clickApplyWhenEnabled()
    await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(definition))
    expect(mocks.updateWorkflowRunDefinition).toHaveBeenCalledWith(RUN_ID, {
      workflow: 'wfd_hunt',
      expected_revision: 3,
      expected_source_sha256: 'abc123',
      expected_id: 'wfd_hunt',
    })
  })

  it('reloads the diff after a stale revision and applies against the new one', async () => {
    mocks.previewWorkflowRunUpdate.mockResolvedValueOnce(preview(3)).mockResolvedValue(preview(4))
    mocks.updateWorkflowRunDefinition
      .mockRejectedValueOnce(
        new ApiError(409, 'newer revision', JSON.stringify({ code: 'workflow_definition_conflict' })),
      )
      .mockResolvedValue({ ok: true, definition: { slug: 'bug-hunt', revision: 5 } })
    const onUpdated = vi.fn()
    renderWithProviders(<UpdateFromRunModal runId={RUN_ID} onClose={vi.fn()} onUpdated={onUpdated} />)

    await screen.findByText('Revision 3 → 4')
    await clickApplyWhenEnabled()
    expect(await screen.findByTestId('workflow-update-stale')).toHaveTextContent('Review the new diff')
    expect(await screen.findByText('Revision 4 → 5')).toBeInTheDocument()
    expect(onUpdated).not.toHaveBeenCalled()
    expect(screen.queryByTestId('workflow-update-error')).not.toBeInTheDocument()

    await clickApplyWhenEnabled()
    await waitFor(() => expect(onUpdated).toHaveBeenCalled())
    expect(mocks.updateWorkflowRunDefinition).toHaveBeenLastCalledWith(RUN_ID, {
      workflow: 'wfd_hunt',
      expected_revision: 4,
      expected_source_sha256: 'abc123',
      expected_id: 'wfd_hunt',
    })
  })

  it('keeps Apply disabled when the reload after a conflict fails', async () => {
    mocks.previewWorkflowRunUpdate
      .mockResolvedValueOnce(preview(3))
      .mockRejectedValue(new ApiError(500, 'could not read saved workflow'))
    mocks.updateWorkflowRunDefinition.mockRejectedValue(
      new ApiError(409, 'newer revision', JSON.stringify({ code: 'workflow_definition_conflict' })),
    )
    renderWithProviders(<UpdateFromRunModal runId={RUN_ID} onClose={vi.fn()} onUpdated={vi.fn()} />)

    await screen.findByText('Revision 3 → 4')
    await clickApplyWhenEnabled()
    expect(await screen.findByTestId('workflow-update-error')).toHaveTextContent('could not read saved workflow')
    expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled()
    expect(mocks.updateWorkflowRunDefinition).toHaveBeenCalledTimes(1)
  })

  it('names a workflow whose id is shadowed by its slug, and shows the server reason', async () => {
    mocks.workflowDefinitions.mockResolvedValue({
      definitions: [{ id: 'wfd_hunt', slug: 'bug-hunt', name: 'Bug Hunt', shadowed_refs: ['wfd_hunt'] }],
    })
    mocks.previewWorkflowRunUpdate.mockResolvedValue(preview(3))
    mocks.updateWorkflowRunDefinition.mockRejectedValue(new ApiError(400, 'workflow source is invalid'))
    renderWithProviders(<UpdateFromRunModal runId={RUN_ID} onClose={vi.fn()} onUpdated={vi.fn()} />)

    await screen.findByText('Revision 3 → 4')
    await screen.findByRole('option', { name: 'bug-hunt' })
    await clickApplyWhenEnabled()
    const notice = await screen.findByTestId('workflow-update-error')
    expect(notice).toHaveTextContent('workflow source is invalid')
    // The dialog holds no draft, so the error can be handed to the agent.
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    expect(mocks.updateWorkflowRunDefinition).toHaveBeenCalledWith(
      RUN_ID,
      expect.objectContaining({ workflow: 'bug-hunt', expected_id: 'wfd_hunt' }),
    )
  })

  it('offers a workflow whose slug is shadowed by its id, and names the previewed target', async () => {
    mocks.workflowDefinitions.mockResolvedValue({
      definitions: [
        { id: 'wfd_hunt', slug: 'bug-hunt', name: 'Bug Hunt' },
        { id: 'wfd_debug', slug: 'debug-project', name: 'Debug', shadowed_refs: ['debug-project'] },
        { id: 'gone', slug: 'gone', name: 'Gone', shadowed_refs: ['gone'] },
      ],
    })
    mocks.previewWorkflowRunUpdate.mockResolvedValue(preview(3))
    renderWithProviders(<UpdateFromRunModal runId={RUN_ID} onClose={vi.fn()} onUpdated={vi.fn()} />)

    expect(await screen.findByTestId('workflow-update-target')).toHaveTextContent('Bug Hunt')
    await screen.findByRole('option', { name: 'wfd_debug' })
    expect(screen.queryByRole('option', { name: 'debug-project' })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'gone' })).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Saved workflow'), { target: { value: 'wfd_debug' } })
    await waitFor(() => expect(mocks.previewWorkflowRunUpdate).toHaveBeenLastCalledWith(RUN_ID, 'wfd_debug'))
  })

  it('cannot be dismissed while Apply is pending', async () => {
    mocks.previewWorkflowRunUpdate.mockResolvedValue(preview(3))
    let rejectUpdate: (err: unknown) => void = () => {}
    mocks.updateWorkflowRunDefinition.mockReturnValue(
      new Promise((_resolve, reject) => {
        rejectUpdate = reject
      }),
    )
    const onClose = vi.fn()
    renderWithProviders(<UpdateFromRunModal runId={RUN_ID} onClose={onClose} onUpdated={vi.fn()} />)

    await screen.findByText('Revision 3 → 4')
    await clickApplyWhenEnabled()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled())
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).not.toHaveBeenCalled()
    rejectUpdate(new ApiError(500, 'could not write workflow'))
    expect(await screen.findByTestId('workflow-update-error')).toHaveTextContent('could not write workflow')
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
  })

  it('holds Apply until the definitions list says whether the id is shadowed', async () => {
    let resolveDefinitions: (value: unknown) => void = () => {}
    mocks.workflowDefinitions.mockReturnValue(
      new Promise(resolve => {
        resolveDefinitions = resolve
      }),
    )
    mocks.previewWorkflowRunUpdate.mockResolvedValue(preview(3))
    mocks.updateWorkflowRunDefinition.mockResolvedValue({ ok: true, definition: { slug: 'bug-hunt', revision: 4 } })
    renderWithProviders(<UpdateFromRunModal runId={RUN_ID} onClose={vi.fn()} onUpdated={vi.fn()} />)

    await screen.findByText('Revision 3 → 4')
    expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled()
    resolveDefinitions({
      definitions: [{ id: 'wfd_hunt', slug: 'bug-hunt', name: 'Bug Hunt', shadowed_refs: ['wfd_hunt'] }],
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() =>
      expect(mocks.updateWorkflowRunDefinition).toHaveBeenCalledWith(RUN_ID, expect.objectContaining({ workflow: 'bug-hunt' })),
    )
  })

  it('keeps Apply disabled when the definitions list fails to load', async () => {
    mocks.workflowDefinitions.mockRejectedValue(new ApiError(500, 'could not list workflows'))
    mocks.previewWorkflowRunUpdate.mockResolvedValue(preview(3))
    renderWithProviders(<UpdateFromRunModal runId={RUN_ID} onClose={vi.fn()} onUpdated={vi.fn()} />)

    expect(await screen.findByTestId('workflow-update-error')).toHaveTextContent('could not list workflows')
    await screen.findByText('Revision 3 → 4')
    expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled()
  })

  it('clears a conflict when the owner picks another workflow', async () => {
    mocks.previewWorkflowRunUpdate.mockResolvedValue(preview(3))
    mocks.updateWorkflowRunDefinition.mockRejectedValue(
      new ApiError(409, 'newer revision', JSON.stringify({ code: 'workflow_definition_conflict' })),
    )
    renderWithProviders(<UpdateFromRunModal runId={RUN_ID} onClose={vi.fn()} onUpdated={vi.fn()} />)

    await screen.findByText('Revision 3 → 4')
    await screen.findByRole('option', { name: 'other' })
    await clickApplyWhenEnabled()
    await screen.findByTestId('workflow-update-stale')
    fireEvent.change(screen.getByLabelText('Saved workflow'), { target: { value: 'other' } })
    await waitFor(() => expect(screen.queryByTestId('workflow-update-stale')).not.toBeInTheDocument())
    expect(screen.queryByTestId('workflow-update-error')).not.toBeInTheDocument()
  })

  it('previews the workflow the owner picks and refuses an unchanged source', async () => {
    mocks.previewWorkflowRunUpdate.mockImplementation(async (_run: string, ref: string) =>
      ref === 'other'
        ? preview(1, {
            target: { id: 'wfd_other', slug: 'other', name: 'Other', revision: 1, format: 'python' },
            diff: '',
            unchanged: true,
          })
        : preview(3),
    )
    renderWithProviders(<UpdateFromRunModal runId={RUN_ID} onClose={vi.fn()} onUpdated={vi.fn()} />)

    await screen.findByText('Revision 3 → 4')
    await screen.findByRole('option', { name: 'other' })
    fireEvent.change(screen.getByLabelText('Saved workflow'), { target: { value: 'other' } })
    expect(await screen.findByText("This run's workflow is already the saved workflow's current revision.")).toBeInTheDocument()
    expect(mocks.previewWorkflowRunUpdate).toHaveBeenLastCalledWith(RUN_ID, 'other')
    expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled()
  })
})
