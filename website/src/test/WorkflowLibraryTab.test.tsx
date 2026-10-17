import { Profiler } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({
  workflowDefinitions: vi.fn(),
  authorWorkflow: vi.fn(),
  saveWorkflowDefinition: vi.fn(),
  updateWorkflowDefinition: vi.fn(),
  runWorkflowDefinition: vi.fn(),
  removeWorkflowDefinition: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
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
import { ApiError } from '../api/apiError'
import { attachReport } from '../utils/errorReport'
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
    expect(screen.getAllByText('/workflow debug-project')).toHaveLength(2)
    expect(screen.getByText(/wfd_parent/)).toBeInTheDocument()
    expect(
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.source')),
    ).toHaveAttribute('data-language', 'python')
    expect(
      screen.getByLabelText(i18nT('pages.overview.workflowLibrary.source')),
    ).toHaveAttribute('data-line-numbers', 'true')
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

  describe('remove', () => {
    const OTHER = {
      ...DEFINITION,
      id: 'wfd_2',
      slug: 'triage-tickets',
      name: 'Triage Tickets',
      updated_at: '2026-08-23T00:00:00Z',
    }
    const removeLabel = () => i18nT('pages.overview.workflowLibrary.remove')
    // The editor mounts a commit after the list (the auto-select effect), so
    // a test that acts on the editor waits for the editor, not for a row.
    const editorShown = () =>
      screen.findByRole('button', { name: removeLabel() })

    it('asks first, naming the workflow and what keeps working', async () => {
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))

      const dialog = await screen.findByRole('dialog')
      expect(dialog).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.remove_confirm_title', {
          name: 'Debug Project',
        }),
      )
      const permanent = within(dialog).getByText(
        i18nT('pages.overview.workflowLibrary.remove_confirm_permanent'),
      )
      expect(permanent.tagName).toBe('STRONG')
      // The bullet leads with the outcome for this workflow and names the real
      // command, spelled out so a catalog edit cannot drift from it.
      expect(
        within(dialog).getAllByRole('listitem')[1].textContent,
      ).toBe(
        '/workflow debug-project stops working. A new workflow saved with this name gets debug-project-2.',
      )
      const effects = within(dialog)
        .getAllByRole('listitem')
        .map((item) => item.textContent)
      expect(effects).toEqual([
        i18nT('pages.overview.workflowLibrary.remove_confirm_listing'),
        i18nT('pages.overview.workflowLibrary.remove_confirm_name', {
          slug: 'debug-project',
        }),
        i18nT('pages.overview.workflowLibrary.remove_confirm_lineage'),
      ])
      fireEvent.click(
        screen.getByRole('button', {
          name: i18nT('components.confirmDialog.cancel'),
        }),
      )
      await waitFor(() =>
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
      )
      expect(mockApi.removeWorkflowDefinition).not.toHaveBeenCalled()
      expect(screen.getAllByText('/workflow debug-project')).toHaveLength(2)
    })

    it('drops the row only after the server confirms, then selects the next one', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      let settle: (value: unknown) => void = () => {}
      mockApi.removeWorkflowDefinition.mockReturnValue(
        new Promise((resolve) => {
          settle = resolve
        }),
      )
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      await waitFor(() =>
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledWith('wfd_1'),
      )
      expect(screen.getAllByText('/workflow debug-project')).toHaveLength(2)
      const pending = screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.removing'),
      })
      expect(pending).toBeDisabled()
      // The in-flight button is the only progress cue, so it must not take
      // the faded disabled look: it stays at full danger contrast and busy.
      expect(pending).toHaveAttribute('aria-busy', 'true')
      expect(pending.className).toContain('disabled:opacity-100')
      expect(pending.className).not.toContain('disabled:opacity-30')

      await act(async () => {
        settle({ ok: true, id: 'wfd_1', removed_at: '2026-10-11T00:00:00Z' })
      })

      await waitFor(() =>
        expect(
          screen.queryByText('/workflow debug-project'),
        ).not.toBeInTheDocument(),
      )
      expect(screen.getAllByText('/workflow triage-tickets')).toHaveLength(2)
    })

    it('says which workflow left once the removal succeeds', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      mockApi.removeWorkflowDefinition.mockResolvedValue({
        ok: true,
        id: 'wfd_1',
        removed_at: '2026-10-11T00:00:00Z',
      })
      renderTab()
      await editorShown()
      expect(screen.queryByRole('status')).not.toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      expect(await screen.findByRole('status')).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.removed', {
          name: 'Debug Project',
        }),
      )
      expect(screen.getAllByText('/workflow triage-tickets')).toHaveLength(2)

      fireEvent.click(screen.getAllByText('Triage Tickets')[0])
      await waitFor(() =>
        expect(screen.queryByRole('status')).not.toBeInTheDocument(),
      )
    })

    it('clears the removed note once the user edits the workflow now shown', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      mockApi.removeWorkflowDefinition.mockResolvedValue({
        ok: true,
        id: 'wfd_1',
        removed_at: '2026-10-11T00:00:00Z',
      })
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )
      await screen.findByRole('status')

      fireEvent.change(
        screen.getByLabelText(i18nT('pages.overview.workflowLibrary.name')),
        { target: { value: 'Triage Tickets v2' } },
      )

      await waitFor(() =>
        expect(screen.queryByRole('status')).not.toBeInTheDocument(),
      )
    })

    it('shows the removed note only together with the next workflow’s editor', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      let settle: (value: unknown) => void = () => {}
      mockApi.removeWorkflowDefinition.mockReturnValue(
        new Promise((resolve) => {
          settle = resolve
        }),
      )
      // Read the DOM at every React commit. A poll (findBy, waitFor) or a
      // MutationObserver sees only some of them, so a state that lasts one
      // commit would pass unseen and fail a later test under load.
      const runLabel = i18nT('pages.overview.workflowLibrary.run_input')
      let recording = false
      const states: string[] = []
      const onCommit = () => {
        if (!recording || !screen.queryByRole('status')) return
        const input = screen.queryByLabelText(runLabel) as HTMLInputElement | null
        const state = JSON.stringify({
          runInput: input?.value ?? null,
          triage: screen.queryAllByText('/workflow triage-tickets').length,
          debug: screen.queryAllByText('/workflow debug-project').length,
        })
        if (states[states.length - 1] !== state) states.push(state)
      }
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      })
      render(
        <QueryClientProvider client={client}>
          <Profiler id="workflow-library" onRender={onCommit}>
            <WorkflowLibraryTab />
          </Profiler>
        </QueryClientProvider>,
      )
      await editorShown()
      fireEvent.change(screen.getByLabelText(runLabel), {
        target: { value: 'for debug only' },
      })
      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )
      await waitFor(() =>
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledWith('wfd_1'),
      )

      recording = true
      await act(async () => {
        settle({ ok: true, id: 'wfd_1', removed_at: '2026-10-11T00:00:00Z' })
      })
      await screen.findByRole('status')
      recording = false

      expect(states).toEqual([
        JSON.stringify({ runInput: '', triage: 2, debug: 0 }),
      ])
    })

    it('carries none of the removed workflow’s run state onto the next one', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      mockApi.removeWorkflowDefinition.mockResolvedValue({
        ok: true,
        id: 'wfd_1',
        removed_at: '2026-10-11T00:00:00Z',
      })
      renderTab()
      await editorShown()
      const runInput = () =>
        screen.getByLabelText(
          i18nT('pages.overview.workflowLibrary.run_input'),
        ) as HTMLInputElement
      fireEvent.change(runInput(), { target: { value: 'for debug only' } })
      fireEvent.click(
        screen.getByRole('button', {
          name: i18nT('pages.overview.workflowLibrary.run'),
        }),
      )
      const started = i18nT('pages.overview.workflowLibrary.started', {
        runId: 'wf_1',
      })
      await screen.findByText(started)

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      await screen.findByRole('status')
      expect(screen.getAllByText('/workflow triage-tickets')).toHaveLength(2)
      expect(screen.queryByText(started)).not.toBeInTheDocument()
      expect(runInput().value).toBe('')
    })

    it('keeps a removal still in flight when the user picks another workflow', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      let fail: (error: unknown) => void = () => {}
      mockApi.removeWorkflowDefinition.mockReturnValue(
        new Promise((_resolve, reject) => {
          fail = reject
        }),
      )
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )
      await waitFor(() =>
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledWith('wfd_1'),
      )

      fireEvent.click(screen.getAllByText('Triage Tickets')[0])
      const nameField = screen.getByLabelText(
        i18nT('pages.overview.workflowLibrary.name'),
      ) as HTMLInputElement
      await waitFor(() => expect(nameField.value).toBe('Triage Tickets'))
      fireEvent.change(nameField, { target: { value: 'Triage v2' } })

      await act(async () => {
        fail(
          new ApiError(
            500,
            'could not remove workflow',
            JSON.stringify({ code: 'workflow_definition_write_failed' }),
          ),
        )
      })

      expect(await screen.findByRole('alert')).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.remove_failed', {
          name: 'Debug Project',
        }),
      )
      expect(nameField.value).toBe('Triage v2')
    })

    it('shows a removal failure that lands after the user starts a new workflow', async () => {
      let fail: (error: unknown) => void = () => {}
      mockApi.removeWorkflowDefinition.mockReturnValue(
        new Promise((_resolve, reject) => {
          fail = reject
        }),
      )
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )
      await waitFor(() =>
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledWith('wfd_1'),
      )
      fireEvent.click(
        screen.getByRole('button', {
          name: i18nT('pages.overview.workflowLibrary.new_workflow'),
        }),
      )
      await screen.findByText(
        i18nT('pages.overview.workflowLibrary.create_title'),
      )

      await act(async () => {
        fail(
          new ApiError(
            500,
            'could not remove workflow',
            JSON.stringify({ code: 'workflow_definition_write_failed' }),
          ),
        )
      })

      expect(await screen.findByRole('alert')).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.remove_failed', {
          name: 'Debug Project',
        }),
      )
      expect(
        screen.getByText(i18nT('pages.overview.workflowLibrary.create_title')),
      ).toBeInTheDocument()
    })

    it('leaves the workflow now shown alone when an earlier removal lands', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      let settle: (value: unknown) => void = () => {}
      mockApi.removeWorkflowDefinition.mockReturnValue(
        new Promise((resolve) => {
          settle = resolve
        }),
      )
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )
      await waitFor(() =>
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledWith('wfd_1'),
      )
      fireEvent.click(screen.getAllByText('Triage Tickets')[0])
      const nameField = screen.getByLabelText(
        i18nT('pages.overview.workflowLibrary.name'),
      ) as HTMLInputElement
      await waitFor(() => expect(nameField.value).toBe('Triage Tickets'))
      fireEvent.change(nameField, { target: { value: 'Triage v2' } })

      await act(async () => {
        settle({ ok: true, id: 'wfd_1', removed_at: '2026-10-11T00:00:00Z' })
      })

      await waitFor(() =>
        expect(
          screen.queryByText('/workflow debug-project'),
        ).not.toBeInTheDocument(),
      )
      expect(nameField.value).toBe('Triage v2')
    })

    it('keeps the row and announces the failure when removal fails', async () => {
      mockApi.removeWorkflowDefinition.mockRejectedValue(
        new ApiError(
          500,
          'could not remove workflow',
          JSON.stringify({
            error: 'could not remove workflow',
            code: 'workflow_definition_write_failed',
          }),
        ),
      )
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.remove_failed', {
          name: 'Debug Project',
        }),
      )
      // ErrorNotice's boxed banner, not a hand-written paragraph: the shared
      // surface is a DIV carrying the warning icon.
      expect(alert.tagName).toBe('DIV')
      expect(alert.querySelector('svg')).not.toBeNull()
      expect(alert).not.toHaveTextContent('could not remove workflow')
      expect(screen.getAllByText('/workflow debug-project')).toHaveLength(2)
    })

    it('shows the recorded status, code and path under the failure line', async () => {
      mockApi.removeWorkflowDefinition.mockRejectedValue(
        attachReport(
          new ApiError(
            500,
            'could not remove workflow',
            JSON.stringify({ code: 'workflow_definition_write_failed' }),
          ),
          {
            id: 'err-1',
            at: 0,
            source: 'api',
            message: 'could not remove workflow',
            status: 500,
            code: 'workflow_definition_write_failed',
            endpoint: '/api/workflows/definitions/wfd_1',
            route: '/',
          },
        ),
      )
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      const alert = await screen.findByRole('alert')
      const cause =
        '500 · workflow_definition_write_failed · /api/workflows/definitions/wfd_1'
      // The headline stays visible; the cause is behind a collapsed Details
      // disclosure that a keyboard and a screen reader can open.
      expect(alert).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.remove_failed', {
          name: 'Debug Project',
        }),
      )
      expect(alert).not.toHaveTextContent(cause)
      const details = within(alert).getByRole('button', {
        name: i18nT('pages.artifactDeployPage.details'),
      })
      expect(details).toHaveAttribute('aria-expanded', 'false')
      fireEvent.click(details)
      expect(details).toHaveAttribute('aria-expanded', 'true')
      expect(alert).toHaveTextContent(cause)
      // No hand-off: it leaves the page, and the editor may hold unsaved edits.
      // The only other control is the retry the failure line asks for.
      expect(within(alert).getAllByRole('button')).toEqual([
        within(alert).getByRole('button', {
          name: i18nT('pages.overview.workflowLibrary.remove_retry'),
        }),
        details,
      ])
    })

    it('retries the confirmed removal from the failure notice', async () => {
      mockApi.removeWorkflowDefinition
        .mockRejectedValueOnce(
          new ApiError(
            500,
            'could not remove workflow',
            JSON.stringify({ code: 'workflow_definition_write_failed' }),
          ),
        )
        .mockResolvedValueOnce({
          ok: true,
          id: 'wfd_1',
          removed_at: '2026-10-11T00:00:00Z',
        })
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )
      const alert = await screen.findByRole('alert')
      await waitFor(() =>
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
      )

      fireEvent.click(
        within(alert).getByRole('button', {
          name: i18nT('pages.overview.workflowLibrary.remove_retry'),
        }),
      )

      // The user already confirmed this removal, so the retry sends it
      // again without a second dialog.
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      await waitFor(() =>
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledTimes(2),
      )
      expect(mockApi.removeWorkflowDefinition).toHaveBeenLastCalledWith('wfd_1')
      await waitFor(() =>
        expect(
          screen.queryByText('/workflow debug-project'),
        ).not.toBeInTheDocument(),
      )
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(
        screen.getByText(
          i18nT('pages.overview.workflowLibrary.removed', {
            name: 'Debug Project',
          }),
        ),
      ).toBeInTheDocument()
    })

    it('keeps focus on the retry while it is pending and after it fails again', async () => {
      const failure = () =>
        new ApiError(
          500,
          'could not remove workflow',
          JSON.stringify({ code: 'workflow_definition_write_failed' }),
        )
      let rejectRetry: (error: unknown) => void = () => {}
      mockApi.removeWorkflowDefinition
        .mockRejectedValueOnce(failure())
        .mockImplementationOnce(
          () =>
            new Promise((_resolve, reject) => {
              rejectRetry = reject
            }),
        )
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )
      const alert = await screen.findByRole('alert')
      const retry = within(alert).getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.remove_retry'),
      })
      retry.focus()
      fireEvent.click(retry)
      // A second click in the same tick sends nothing more.
      fireEvent.click(retry)

      await waitFor(() =>
        expect(retry).toHaveAttribute('aria-busy', 'true'),
      )
      expect(retry).toBeInTheDocument()
      expect(retry).toHaveAttribute('aria-disabled', 'true')
      expect(retry).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.removing'),
      )
      expect(document.activeElement).toBe(retry)
      expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledTimes(2)

      rejectRetry(failure())
      await waitFor(() => expect(retry).not.toHaveAttribute('aria-busy'))
      expect(retry).toBeInTheDocument()
      expect(retry).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.remove_retry'),
      )
      expect(document.activeElement).toBe(retry)
      expect(screen.getByRole('alert')).toBeInTheDocument()
    })

    const writeFailure = () =>
      new ApiError(
        500,
        'could not remove workflow',
        JSON.stringify({ code: 'workflow_definition_write_failed' }),
      )

    // Confirms a removal through the editor and returns the failure notice.
    const confirmAndFail = async () => {
      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )
      const alert = await screen.findByRole('alert')
      await waitFor(() =>
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
      )
      return within(alert).getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.remove_retry'),
      })
    }

    it('moves focus to the next workflow’s Remove once a retry succeeds', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      let resolveRetry: (value: unknown) => void = () => {}
      mockApi.removeWorkflowDefinition
        .mockRejectedValueOnce(writeFailure())
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              resolveRetry = resolve
            }),
        )
      renderTab()
      await editorShown()

      const retry = await confirmAndFail()
      retry.focus()
      fireEvent.click(retry)
      await waitFor(() => expect(retry).toHaveAttribute('aria-busy', 'true'))

      // The retry is the one progress cue: the editor's Remove is only
      // disabled, with its own label and no spinner or busy state.
      const editorRemove = screen.getByRole('button', { name: removeLabel() })
      expect(editorRemove).toBeDisabled()
      expect(editorRemove).not.toHaveAttribute('aria-busy')
      expect(
        screen.getAllByText(i18nT('pages.overview.workflowLibrary.removing')),
      ).toHaveLength(1)

      await act(async () => {
        resolveRetry({
          ok: true,
          id: 'wfd_1',
          removed_at: '2026-10-11T00:00:00Z',
        })
      })

      await waitFor(() =>
        expect(screen.getAllByText('/workflow triage-tickets')).toHaveLength(2),
      )
      expect(retry).not.toBeInTheDocument()
      await waitFor(() =>
        expect(document.activeElement).toBe(
          screen.getByRole('button', { name: removeLabel() }),
        ),
      )
    })

    it('moves focus to the removed note when a retry empties the library', async () => {
      mockApi.removeWorkflowDefinition
        .mockRejectedValueOnce(writeFailure())
        .mockResolvedValueOnce({
          ok: true,
          id: 'wfd_1',
          removed_at: '2026-10-11T00:00:00Z',
        })
      renderTab()
      await editorShown()

      const retry = await confirmAndFail()
      retry.focus()
      fireEvent.click(retry)

      const note = await screen.findByText(
        i18nT('pages.overview.workflowLibrary.removed', {
          name: 'Debug Project',
        }),
      )
      await waitFor(() => expect(document.activeElement).toBe(note))
    })

    it('ignores Enter and Space on a retry that is still pending', async () => {
      const user = userEvent.setup()
      let rejectRetry: (error: unknown) => void = () => {}
      mockApi.removeWorkflowDefinition
        .mockRejectedValueOnce(writeFailure())
        .mockImplementationOnce(
          () =>
            new Promise((_resolve, reject) => {
              rejectRetry = reject
            }),
        )
      renderTab()
      await editorShown()

      const retry = await confirmAndFail()
      retry.focus()
      await user.keyboard('{Enter}')
      await waitFor(() => expect(retry).toHaveAttribute('aria-busy', 'true'))
      expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledTimes(2)

      await user.keyboard('{Enter}')
      await user.keyboard(' ')
      expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledTimes(2)
      expect(document.activeElement).toBe(retry)

      await act(async () => {
        rejectRetry(writeFailure())
      })
      await waitFor(() => expect(retry).not.toHaveAttribute('aria-busy'))
      expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledTimes(2)
    })

    it('shows no stale failure while a removal confirmed after a failed retry is pending', async () => {
      mockApi.removeWorkflowDefinition
        .mockRejectedValueOnce(writeFailure())
        .mockRejectedValueOnce(writeFailure())
        .mockImplementationOnce(() => new Promise(() => {}))
      renderTab()
      await editorShown()

      const retry = await confirmAndFail()
      fireEvent.click(retry)
      await waitFor(() =>
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledTimes(2),
      )
      await waitFor(() => expect(retry).not.toHaveAttribute('aria-busy'))
      expect(screen.getByRole('alert')).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )
      await waitFor(() =>
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledTimes(3),
      )

      // The failure the retry answered is settled, so the new removal
      // shows only its own progress on the editor's Remove.
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(
        screen.getByRole('button', {
          name: i18nT('pages.overview.workflowLibrary.removing'),
        }),
      ).toHaveAttribute('aria-busy', 'true')
    })

    it('puts Remove apart from Save, on the far side of the button row', async () => {
      renderTab()
      await editorShown()

      expect(screen.getByRole('button', { name: removeLabel() })).toHaveClass(
        'ml-auto',
      )
    })

    it('shows no cause line when nothing was recorded for the failure', async () => {
      mockApi.removeWorkflowDefinition.mockRejectedValue(
        new Error('socket closed'),
      )
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.remove_failed', {
          name: 'Debug Project',
        }),
      )
      expect(
        within(alert).queryByRole('button', {
          name: i18nT('pages.artifactDeployPage.details'),
        }),
      ).toBeNull()
    })

    it('treats a 404 as already removed and shows no error', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      mockApi.removeWorkflowDefinition.mockRejectedValue(
        Object.assign(new Error('no such saved workflow'), { status: 404 }),
      )
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      await waitFor(() =>
        expect(
          screen.queryByText('/workflow debug-project'),
        ).not.toBeInTheDocument(),
      )
      expect(screen.getAllByText('/workflow triage-tickets')).toHaveLength(2)
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.queryByText('no such saved workflow')).not.toBeInTheDocument()
    })
  })
})
