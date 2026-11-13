import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({
  workflowDefinitions: vi.fn(),
  authorWorkflow: vi.fn(),
  saveWorkflowDefinition: vi.fn(),
  updateWorkflowDefinition: vi.fn(),
  runWorkflowDefinition: vi.fn(),
  removeWorkflowDefinition: vi.fn(),
}))
// The server never lists a workflow it has removed: a removal it answers with
// ok tombstones the record, so every later list read leaves it out. The tab
// reads the library again after each removal, so the fake keeps that rule.
const removedOnServer = vi.hoisted(() => new Set<string>())
vi.mock('../api/client', () => ({
  api: {
    ...mockApi,
    workflowDefinitions: async (...args: unknown[]) => {
      const listed = (await mockApi.workflowDefinitions(...args)) as
        | { definitions?: Array<{ id: string }> }
        | undefined
      return {
        ...listed,
        definitions: (listed?.definitions ?? []).filter(
          (definition) => !removedOnServer.has(definition.id),
        ),
      }
    },
    removeWorkflowDefinition: async (...args: unknown[]) => {
      const result = (await mockApi.removeWorkflowDefinition(...args)) as
        | { ok?: boolean; id?: unknown }
        | undefined
      if (result?.ok && typeof result.id === 'string') {
        removedOnServer.add(result.id)
      }
      return result
    },
  },
}))
vi.mock('../apps/workflows/WorkflowSourceCode', () => ({
  default: ({
    source,
    sourceFormat,
    onChange,
    ariaLabel,
  }: {
    source: string
    sourceFormat?: 'python' | 'task-plan'
    onChange?: (value: string) => void
    ariaLabel: string
  }) => (
    <textarea
      aria-label={ariaLabel}
      data-language={sourceFormat === 'task-plan' ? 'yaml' : 'python'}
      data-line-numbers="true"
      value={source}
      onChange={(event) => onChange?.(event.target.value)}
    />
  ),
}))
vi.mock('../apps/workflows/WorkflowsRuns', () => ({
  default: () => <div>Unified workflow runs</div>,
}))

import WorkflowLibraryTab from '../pages/overview/WorkflowLibraryTab'
import { i18nT } from '../i18n/t'

const DEFINITION = {
  schema_version: 2,
  id: 'wfd_1',
  slug: 'debug-project',
  name: 'Debug Project',
  description: 'Investigate failures',
  created_at: '2026-08-24T00:00:00Z',
  updated_at: '2026-08-24T00:00:00Z',
  revision: 2,
  format: 'python' as const,
  source:
    'META = {"name": "debug-project"}\nasync def workflow(ctx):\n    return 1\n',
  content_hash: 'hash',
  derived_from: { workflow_id: 'wfd_parent', revision: 1 },
  revisions: [],
}

function renderTab() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <WorkflowLibraryTab />
      </QueryClientProvider>,
    ),
  }
}

beforeEach(() => {
  Object.values(mockApi).forEach((mock) => mock.mockReset())
  removedOnServer.clear()
  mockApi.workflowDefinitions.mockResolvedValue({ definitions: [DEFINITION] })
  mockApi.updateWorkflowDefinition.mockResolvedValue({
    ok: true,
    definition: { ...DEFINITION, revision: 3 },
  })
  mockApi.runWorkflowDefinition.mockResolvedValue({
    run_id: 'wf_1',
    revision: 2,
  })
})

/** The editor mounts in the commit after the row (auto-selection effect),
 *  so a test reaching for editor controls waits for it first. */
const editorReady = () => screen.findByLabelText(i18nT('pages.overview.workflowLibrary.source'))

describe('WorkflowLibraryTab', () => {
  it('switches between the saved library and unified run history', async () => {
    renderTab()

    await screen.findByText('Debug Project')
    fireEvent.click(
      screen.getByRole('radio', { name: i18nT('pages.hooksPage.runs') }),
    )

    expect(screen.getByText('Unified workflow runs')).toBeInTheDocument()
    expect(screen.queryByText('Debug Project')).not.toBeInTheDocument()
  })

  it('shows saved workflows, slash invocation, and lineage', async () => {
    renderTab()

    expect(await screen.findByText('Debug Project')).toBeInTheDocument()
    await editorReady()
    expect(screen.getAllByText('/workflow debug-project')).toHaveLength(2)
    expect(screen.getByText(/wfd_parent/)).toBeInTheDocument()
    expect(
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.source')),
    ).toHaveAttribute('data-language', 'python')
    expect(
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.source')),
    ).toHaveAttribute('data-line-numbers', 'true')
  })

  it('marks the selected workflow in the list with aria-pressed', async () => {
    // A screen reader has to hear which saved workflow is open in the editor; the
    // accent background of the chosen row conveys that to a sighted user only.
    const OTHER = { ...DEFINITION, id: 'wfd_2', slug: 'other-flow', name: 'Other Flow' }
    mockApi.workflowDefinitions.mockResolvedValue({ definitions: [DEFINITION, OTHER] })
    renderTab()

    // The `/workflow <slug>` line is unique to the list row, so it scopes the
    // button even after the selected name is echoed in the editor header too.
    const firstRow = (await screen.findByText('/workflow debug-project')).closest('button')!
    const otherRow = screen.getByText('/workflow other-flow').closest('button')!
    // The tab auto-selects the top row, so exactly one reads pressed and the
    // other reads not-pressed -- the attribute tracks selection, not a constant.
    expect(firstRow.getAttribute('aria-pressed')).toBe('true')
    expect(otherRow.getAttribute('aria-pressed')).toBe('false')

    fireEvent.click(otherRow)
    await waitFor(() => expect(otherRow.getAttribute('aria-pressed')).toBe('true'))
    expect(firstRow.getAttribute('aria-pressed')).toBe('false')
  })

  it('runs a saved workflow by its id', async () => {
    renderTab()
    await screen.findByText('Debug Project')
    await editorReady()

    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.run'),
      }),
    )

    await waitFor(() =>
      expect(mockApi.runWorkflowDefinition).toHaveBeenCalledWith('wfd_1', ''),
    )
  })

  it('offers a workflow whose slug does not reach it under its id', async () => {
    mockApi.workflowDefinitions.mockResolvedValue({
      definitions: [{ ...DEFINITION, shadowed_refs: ['debug-project'] }],
    })
    renderTab()

    expect(await screen.findByText('Debug Project')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getAllByText('/workflow wfd_1')).toHaveLength(2),
    )
    expect(screen.queryByText('/workflow debug-project')).not.toBeInTheDocument()
  })

  it('lists a workflow whose slug is another workflow’s id under its own id, and runs it by that id', async () => {
    // A's id is B's slug, so /workflow shared-ref runs A: the read lists it
    // as shadowed on B, and nothing offers it for B.
    const A = {
      ...DEFINITION,
      id: 'shared-ref',
      slug: 'alpha',
      name: 'Alpha',
    }
    const B = {
      ...DEFINITION,
      id: 'wfd_other',
      slug: 'shared-ref',
      name: 'Other',
      shadowed_refs: ['shared-ref'],
    }
    mockApi.workflowDefinitions.mockResolvedValue({ definitions: [B, A] })
    renderTab()
    await screen.findByText('Other')

    await waitFor(() =>
      expect(screen.getAllByText('/workflow wfd_other')).toHaveLength(2),
    )
    expect(screen.getByText('/workflow alpha')).toBeInTheDocument()
    expect(screen.queryByText('/workflow shared-ref')).not.toBeInTheDocument()
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.run'),
      }),
    )
    await waitFor(() =>
      expect(mockApi.runWorkflowDefinition).toHaveBeenCalledWith('wfd_other', ''),
    )
  })

  it('sends every request by slug when its id does not reach it', async () => {
    mockApi.workflowDefinitions.mockResolvedValue({
      definitions: [{ ...DEFINITION, shadowed_refs: ['wfd_1'] }],
    })
    // The server presents a saved revision the way it presents a read.
    mockApi.updateWorkflowDefinition.mockResolvedValue({
      ok: true,
      definition: { ...DEFINITION, shadowed_refs: ['wfd_1'], revision: 3 },
    })
    mockApi.removeWorkflowDefinition.mockResolvedValue({
      ok: true,
      id: 'wfd_1',
      removed_at: '2026-10-24T00:00:00Z',
    })
    renderTab()
    await screen.findByText('Debug Project')
    await waitFor(() =>
      expect(screen.getAllByText('/workflow debug-project')).toHaveLength(2),
    )

    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.run'),
      }),
    )
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.save_revision'),
      }),
    )
    await waitFor(() =>
      expect(mockApi.runWorkflowDefinition).toHaveBeenCalledWith(
        'debug-project',
        '',
      ),
    )
    await waitFor(() =>
      expect(mockApi.updateWorkflowDefinition).toHaveBeenCalledWith(
        'debug-project',
        expect.objectContaining({ expected_revision: 2, expected_id: 'wfd_1' }),
      ),
    )
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.remove'),
      }),
    )
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(
      within(dialog).getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.remove'),
      }),
    )
    await waitFor(() =>
      expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledWith(
        'debug-project',
        'wfd_1',
      ),
    )
  })

  it('marks a workflow no ref reaches as having no slash command and offers no action on it', async () => {
    mockApi.workflowDefinitions.mockResolvedValue({
      definitions: [{ ...DEFINITION, shadowed_refs: ['debug-project', 'wfd_1'] }],
    })
    renderTab()
    await screen.findByText('Debug Project')

    await waitFor(() =>
      expect(
        screen.getAllByText(i18nT('pages.overview.workflowLibrary.no_command')),
      ).toHaveLength(2),
    )
    expect(
      screen.getByText(i18nT('pages.overview.workflowLibrary.no_command_note')),
    ).toBeInTheDocument()
    expect(screen.queryByText(/^\/workflow /)).not.toBeInTheDocument()
    for (const label of ['save_revision', 'run', 'remove'] as const) {
      expect(
        screen.getByRole('button', {
          name: i18nT(`pages.overview.workflowLibrary.${label}`),
        }),
      ).toBeDisabled()
    }
    // Nothing typed here could be saved, so the fields do not take edits.
    for (const label of ['name', 'slug', 'workflow_description'] as const) {
      expect(
        screen.getByLabelText(i18nT(`pages.overview.workflowLibrary.${label}`)),
      ).toHaveAttribute('readonly')
    }
  })

  it('draws every field of a workflow no ref reaches as read-only and names the file fields to change', async () => {
    mockApi.workflowDefinitions.mockResolvedValue({
      definitions: [{ ...DEFINITION, shadowed_refs: ['debug-project', 'wfd_1'] }],
    })
    renderTab()
    const source = await editorReady()
    await screen.findByText(i18nT('pages.overview.workflowLibrary.no_command_note'))

    const runInput = screen.getByLabelText(
      i18nT('pages.overview.workflowLibrary.run_input'),
    )
    expect(runInput).toHaveAttribute('readonly')
    for (const field of [
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.name')),
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.slug')),
      screen.getByLabelText(
        i18nT('pages.overview.workflowLibrary.workflow_description'),
      ),
      runInput,
    ]) {
      expect(field).toHaveAttribute('aria-readonly', 'true')
      expect(field).toHaveClass('opacity-60', 'cursor-not-allowed')
    }
    expect(source.parentElement).toHaveClass('opacity-60', 'cursor-not-allowed')
    expect(
      i18nT('pages.overview.workflowLibrary.no_command_note'),
    ).toContain('`id` and `slug`')
  })

  it('keeps the run input editable for a workflow a ref reaches and empty for one no ref reaches', async () => {
    mockApi.workflowDefinitions.mockResolvedValue({
      definitions: [
        DEFINITION,
        {
          ...DEFINITION,
          id: 'wfd_2',
          slug: 'triage-tickets',
          name: 'Triage Tickets',
          shadowed_refs: ['triage-tickets', 'wfd_2'],
        },
      ],
    })
    renderTab()
    await editorReady()
    const runInput = () =>
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.run_input'))
    expect(runInput()).not.toHaveAttribute('readonly')
    expect(runInput()).not.toHaveClass('opacity-60')
    fireEvent.change(runInput(), { target: { value: 'for debug-project' } })

    fireEvent.click(screen.getByRole('button', { name: /Triage Tickets/ }))
    await screen.findByText(i18nT('pages.overview.workflowLibrary.no_command_note'))
    expect(runInput()).toHaveAttribute('readonly')
    expect(runInput()).toHaveValue('')
  })

  it('finds a workflow whose slug does not reach it by the id its row shows', async () => {
    mockApi.workflowDefinitions.mockResolvedValue({
      definitions: [
        { ...DEFINITION, shadowed_refs: ['debug-project'] },
        { ...DEFINITION, id: 'wfd_2', slug: 'triage-tickets', name: 'Triage Tickets' },
      ],
    })
    renderTab()
    await screen.findAllByText('/workflow wfd_1')

    fireEvent.change(
      screen.getByPlaceholderText(i18nT('pages.overview.workflowLibrary.search')),
      { target: { value: 'wfd_1' } },
    )

    // The editor keeps showing the selected workflow, so the list itself is
    // what must still hold its row.
    expect(
      screen.queryByText(i18nT('pages.overview.workflowLibrary.empty')),
    ).not.toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getAllByText('/workflow wfd_1')).toHaveLength(2),
    )
    expect(screen.queryByText('Triage Tickets')).not.toBeInTheDocument()
  })

  it('shows saved TaskRunner plans as YAML workflows', async () => {
    mockApi.workflowDefinitions.mockResolvedValue({
      definitions: [
        {
          ...DEFINITION,
          format: 'task-plan',
          source: 'agents:\n  test:\n    prompt: run tests\n',
        },
      ],
    })
    renderTab()

    expect(
      await screen.findByLabelText(
        i18nT('pages.overview.workflowLibrary.source'),
      ),
    ).toHaveAttribute('data-language', 'yaml')
    expect(screen.getByText('task-plan')).toBeInTheDocument()
  })

  it('authors an unsaved draft and only promotes it after Save to library', async () => {
    mockApi.workflowDefinitions.mockResolvedValue({ definitions: [] })
    mockApi.authorWorkflow.mockResolvedValue({
      ok: true,
      source: DEFINITION.source,
      meta: { name: 'debug-project', description: 'Investigate failures' },
      derived_from: { workflow_id: 'wfd_parent', revision: 1 },
    })
    mockApi.saveWorkflowDefinition.mockResolvedValue({
      ok: true,
      definition: DEFINITION,
    })
    renderTab()

    fireEvent.click(
      await screen.findByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.new_workflow'),
      }),
    )
    fireEvent.change(
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.intent')),
      {
        target: { value: 'Debug the login flow' },
      },
    )
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.create_draft'),
      }),
    )

    await screen.findByRole('button', {
      name: i18nT('pages.overview.workflowLibrary.save_to_library'),
    })
    expect(mockApi.saveWorkflowDefinition).not.toHaveBeenCalled()
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.save_to_library'),
      }),
    )

    await waitFor(() =>
      expect(mockApi.saveWorkflowDefinition).toHaveBeenCalledWith(
        expect.objectContaining({
          source: DEFINITION.source,
          derived_from: { workflow_id: 'wfd_parent', revision: 1 },
        }),
      ),
    )
  })

  it('ignores an authoring response after selecting a saved workflow', async () => {
    let resolveAuthor!: (value: {
      ok: boolean
      source: string
      meta: { name: string; description: string }
      derived_from: null
    }) => void
    mockApi.authorWorkflow.mockReturnValue(
      new Promise((resolve) => {
        resolveAuthor = resolve
      }),
    )
    renderTab()

    await screen.findByText('Debug Project')
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.new_workflow'),
      }),
    )
    fireEvent.change(
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.intent')),
      {
        target: { value: 'Draft another workflow' },
      },
    )
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.create_draft'),
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: /Debug Project/ }))

    await act(async () => {
      resolveAuthor({
        ok: true,
        source: 'META = {"name": "late-draft"}\n',
        meta: { name: 'Late Draft', description: 'Arrived after navigation' },
        derived_from: null,
      })
    })

    expect(mockApi.authorWorkflow).toHaveBeenCalledTimes(1)
    expect(
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.source')),
    ).toHaveValue(DEFINITION.source)
  })

  it('updates with the selected definition revision as the precondition', async () => {
    renderTab()
    await screen.findByText('Debug Project')
    await editorReady()
    fireEvent.change(
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.source')),
      {
        target: { value: DEFINITION.source + '# changed' },
      },
    )
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.save_revision'),
      }),
    )

    await waitFor(() =>
      expect(mockApi.updateWorkflowDefinition).toHaveBeenCalledWith(
        'wfd_1',
        expect.objectContaining({
          expected_revision: 2,
          source: DEFINITION.source + '# changed',
        }),
      ),
    )
  })

  it('preserves edits made while a revision save is pending', async () => {
    let resolveUpdate!: (value: {
      ok: boolean
      definition: typeof DEFINITION
    }) => void
    mockApi.updateWorkflowDefinition.mockReturnValue(
      new Promise((resolve) => {
        resolveUpdate = resolve
      }),
    )
    renderTab()
    await screen.findByText('Debug Project')
    await editorReady()
    const source = screen.getByLabelText(
      i18nT('pages.overview.workflowLibrary.source'),
    )
    const submittedSource = DEFINITION.source + '# submitted change'
    const continuedSource = submittedSource + '\n# continued editing'
    fireEvent.change(source, { target: { value: submittedSource } })
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.save_revision'),
      }),
    )
    await waitFor(() =>
      expect(mockApi.updateWorkflowDefinition).toHaveBeenCalledTimes(1),
    )
    fireEvent.change(source, { target: { value: continuedSource } })

    await act(async () => {
      resolveUpdate({
        ok: true,
        definition: {
          ...DEFINITION,
          revision: 3,
          source: submittedSource,
        },
      })
    })

    expect(source).toHaveValue(continuedSource)
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.save_revision'),
      }),
    )
    await waitFor(() =>
      expect(mockApi.updateWorkflowDefinition).toHaveBeenLastCalledWith(
        'wfd_1',
        expect.objectContaining({
          expected_revision: 3,
          source: continuedSource,
        }),
      ),
    )
  })

  it('keeps the revision the draft was based on after a background refetch', async () => {
    const { client } = renderTab()
    await screen.findByText('Debug Project')
    await editorReady()
    fireEvent.change(
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.source')),
      {
        target: { value: DEFINITION.source + '# stale draft' },
      },
    )

    client.setQueryData(['workflow-definitions'], {
      definitions: [{ ...DEFINITION, revision: 3 }],
    })
    await screen.findByText(
      i18nT('pages.overview.workflowLibrary.revision', { revision: 3 }),
    )
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.save_revision'),
      }),
    )

    await waitFor(() =>
      expect(mockApi.updateWorkflowDefinition).toHaveBeenCalledWith(
        'wfd_1',
        expect.objectContaining({
          expected_revision: 2,
          source: DEFINITION.source + '# stale draft',
        }),
      ),
    )
  })

  it('refreshes after a conflict and requires reloading the editor before retry', async () => {
    const latest = {
      ...DEFINITION,
      revision: 3,
      source: DEFINITION.source + '# remote change',
    }
    mockApi.workflowDefinitions
      .mockResolvedValueOnce({ definitions: [DEFINITION] })
      .mockResolvedValue({ definitions: [latest] })
    mockApi.updateWorkflowDefinition.mockRejectedValue(
      Object.assign(new Error('revision conflict'), { status: 409 }),
    )
    renderTab()
    await screen.findByText('Debug Project')
    await editorReady()
    fireEvent.change(
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.source')),
      {
        target: { value: DEFINITION.source + '# local change' },
      },
    )

    const save = screen.getByRole('button', {
      name: i18nT('pages.overview.workflowLibrary.save_revision'),
    })
    fireEvent.click(save)

    await waitFor(() =>
      expect(mockApi.workflowDefinitions).toHaveBeenCalledTimes(2),
    )
    expect(save).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /Debug Project/ }))
    expect(save).not.toBeDisabled()
    expect(
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.source')),
    ).toHaveValue(latest.source)
  })

})


describe('WorkflowLibraryTab list-load failure', () => {
  it('announces the error through role=alert and offers a retry that refetches', async () => {
    mockApi.workflowDefinitions
      .mockRejectedValueOnce(new Error('workflow service is unreachable'))
      .mockResolvedValueOnce({ definitions: [DEFINITION] })

    renderTab()

    // The failure renders through ErrorNotice (role="alert"), not a bare
    // text-danger paragraph, so assistive tech announces it.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/workflow service is unreachable|request failed/i)

    expect(mockApi.workflowDefinitions).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: /retry/i }))
    await waitFor(() => expect(mockApi.workflowDefinitions).toHaveBeenCalledTimes(2))

    // After the successful refetch the list renders and the alert clears.
    // Assert the list ROW specifically (a button): once a definition loads the
    // detail pane auto-selects it and renders its name again in a heading, so a
    // bare findByText('Debug Project') now matches two nodes.
    await screen.findByRole('button', { name: /Debug Project/ })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps the pressed Retry mounted and focused while the retry reads', async () => {
    // Retry clears `error` while it reads, which used to unmount the button
    // the user had just pressed and drop their focus to the page (#32164).
    let settle: (value: { definitions: never[] }) => void = () => {}
    mockApi.workflowDefinitions
      .mockRejectedValueOnce(new Error('workflow service is unreachable'))
      .mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
    renderTab()

    const retry = await screen.findByRole('button', { name: /retry/i })
    retry.focus()
    fireEvent.click(retry)
    await waitFor(() => expect(mockApi.workflowDefinitions).toHaveBeenCalledTimes(2))

    expect(retry).toBeInTheDocument()
    expect(retry).toHaveFocus()
    expect(retry).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(retry)
    expect(mockApi.workflowDefinitions).toHaveBeenCalledTimes(2)

    await act(async () => { settle({ definitions: [] }) })
    await waitFor(() => expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument())
  })

  it('offers the agent hand-off on the error when nothing is unsaved', async () => {
    mockApi.workflowDefinitions.mockReset()
    mockApi.workflowDefinitions.mockRejectedValue(new Error('workflow service is unreachable'))

    renderTab()

    await screen.findByRole('alert')
    // No draft in flight, so following the hand-off loses nothing: it is offered.
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('withholds the hand-off while a new draft is being authored', async () => {
    // First load succeeds so the user can start a draft; the next list read
    // (a refetch) fails while that unsaved draft is in the editor.
    mockApi.workflowDefinitions.mockReset()
    mockApi.workflowDefinitions
      .mockResolvedValueOnce({ definitions: [DEFINITION] })
      .mockRejectedValue(new Error('workflow service is unreachable'))

    const { client } = renderTab()
    await screen.findByRole('button', { name: /Debug Project/ })

    // Author a new draft (sets `creating`), then force a list refetch failure.
    fireEvent.click(screen.getByRole('button', { name: /new workflow/i }))
    await act(async () => {
      await client.refetchQueries({ queryKey: ['workflow-definitions'] })
    })

    const alert = await screen.findByRole('alert')
    expect(alert).toBeInTheDocument()
    // Following "Ask the agent" would unmount the tab and discard the draft, so
    // the hand-off is withheld while the draft is unsaved.
    expect(screen.queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
  })

  it('withholds the hand-off while an unsent run input is typed', async () => {
    // A clean selected definition (editor not dirty) but a typed-but-unsent run
    // payload: a background refetch failure must not offer a hand-off that would
    // discard it.
    mockApi.workflowDefinitions.mockReset()
    mockApi.workflowDefinitions
      .mockResolvedValueOnce({ definitions: [DEFINITION] })
      .mockRejectedValue(new Error('workflow service is unreachable'))

    const { client } = renderTab()
    await screen.findByRole('button', { name: /Debug Project/ })
    await editorReady()

    const runInput = document.getElementById('workflow-run-input') as HTMLInputElement
    fireEvent.change(runInput, { target: { value: 'unsent payload' } })
    await act(async () => {
      await client.refetchQueries({ queryKey: ['workflow-definitions'] })
    })

    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
  })
})
