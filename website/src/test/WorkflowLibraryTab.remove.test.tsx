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
import type { WorkflowDefinition } from '../api/client/workflows'
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

describe('WorkflowLibraryTab', () => {
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
      // command, spelled out so a catalog edit cannot drift from it. It names
      // no suffix: the next free number depends on the library, not on this
      // workflow's slug.
      expect(
        within(dialog).getAllByRole('listitem')[1].textContent,
      ).toBe(
        "/workflow debug-project stops working and stays reserved. A new workflow can't take that name and gets a numbered one instead.",
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

    it('names the command its row offers when the slug does not reach it', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [{ ...DEFINITION, shadowed_refs: ['debug-project'] }],
      })
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))

      const dialog = await screen.findByRole('dialog')
      expect(
        within(dialog).getAllByRole('listitem')[1].textContent,
      ).toBe(
        "/workflow wfd_1 stops working and stays reserved. A new workflow can't take that name and gets a numbered one instead.",
      )
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
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledWith('wfd_1', 'wfd_1'),
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
      // A removal can change other records' reserved refs, so the library is
      // read again after it: once at mount, once after the removal.
      await waitFor(() =>
        expect(mockApi.workflowDefinitions).toHaveBeenCalledTimes(2),
      )
      expect(screen.queryByText('/workflow debug-project')).not.toBeInTheDocument()
    })

    it('re-reads the library after a removal, so a survivor whose id the removed slug now holds goes by its own slug', async () => {
      // B was placed by hand: its id is A's slug, and its own slug is free.
      const B = {
        ...DEFINITION,
        id: 'debug-project',
        slug: 'hand-placed',
        name: 'Hand Placed',
        updated_at: '2026-08-23T00:00:00Z',
      }
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [{ ...DEFINITION, shadowed_refs: ['debug-project'] }, B],
      })
      mockApi.removeWorkflowDefinition.mockImplementation(async () => {
        // Removing A reserves its slug, which is B's id, so the server now
        // presents B with its id reserved.
        mockApi.workflowDefinitions.mockResolvedValue({
          definitions: [{ ...B, shadowed_refs: ['debug-project'] }],
        })
        return { ok: true, id: 'wfd_1', removed_at: '2026-10-24T00:00:00Z' }
      })
      const { client } = renderTab()
      await editorShown()
      expect(mockApi.workflowDefinitions).toHaveBeenCalledTimes(1)

      // No list read is in flight when the removal succeeds.
      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      await waitFor(() =>
        expect(
          client.getQueryData<{ definitions: WorkflowDefinition[] }>([
            'workflow-definitions',
          ])?.definitions,
        ).toEqual([{ ...B, shadowed_refs: ['debug-project'] }]),
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
          'hand-placed',
          '',
        ),
      )
      await waitFor(() =>
        expect(mockApi.updateWorkflowDefinition).toHaveBeenCalledWith(
          'hand-placed',
          expect.objectContaining({ expected_id: 'debug-project' }),
        ),
      )
    })

    it('re-reads the library after a removal fails, so a survivor whose id the removed slug now holds goes by its own slug', async () => {
      // A has a second live copy on disk, and B was placed by hand: its id is
      // A's slug, and its own slug is free.
      const B = {
        ...DEFINITION,
        id: 'debug-project',
        slug: 'hand-placed',
        name: 'Hand Placed',
        updated_at: '2026-08-23T00:00:00Z',
      }
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [{ ...DEFINITION, shadowed_refs: ['debug-project'] }, B],
      })
      mockApi.removeWorkflowDefinition.mockImplementation(async () => {
        // One copy of A is tombstoned before the write to the other fails, so
        // A's id and slug are reserved: A's live copy lists with no slash
        // command, and B with its id reserved.
        mockApi.workflowDefinitions.mockResolvedValue({
          definitions: [
            { ...DEFINITION, shadowed_refs: ['debug-project', 'wfd_1'] },
            { ...B, shadowed_refs: ['debug-project'] },
          ],
        })
        throw new ApiError(
          500,
          'could not remove workflow',
          JSON.stringify({ code: 'workflow_definition_write_failed' }),
        )
      })
      const { client } = renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      expect(await screen.findByRole('alert')).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.remove_failed', {
          name: 'Debug Project',
        }),
      )
      await waitFor(() =>
        expect(
          client.getQueryData<{ definitions: WorkflowDefinition[] }>([
            'workflow-definitions',
          ])?.definitions,
        ).toEqual([
          { ...DEFINITION, shadowed_refs: ['debug-project', 'wfd_1'] },
          { ...B, shadowed_refs: ['debug-project'] },
        ]),
      )
      fireEvent.click(screen.getAllByText('Hand Placed')[0])
      const nameField = screen.getByLabelText(
        i18nT('pages.overview.workflowLibrary.name'),
      ) as HTMLInputElement
      await waitFor(() => expect(nameField.value).toBe('Hand Placed'))
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
          'hand-placed',
          '',
        ),
      )
      await waitFor(() =>
        expect(mockApi.updateWorkflowDefinition).toHaveBeenCalledWith(
          'hand-placed',
          expect.objectContaining({ expected_id: 'debug-project' }),
        ),
      )
    })

    it('reads the library again after a second removal confirmed while the first one’s re-read is in flight', async () => {
      // B was placed by hand: its id is A's slug, and its own slug is free.
      const B = {
        ...DEFINITION,
        id: 'debug-project',
        slug: 'hand-placed',
        name: 'Hand Placed',
        updated_at: '2026-08-22T00:00:00Z',
      }
      let answerFirstReRead: (value: unknown) => void = () => {}
      mockApi.workflowDefinitions
        .mockResolvedValueOnce({
          definitions: [{ ...DEFINITION, shadowed_refs: ['debug-project'] }, OTHER, B],
        })
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              answerFirstReRead = resolve
            }),
        )
        .mockResolvedValue({ definitions: [{ ...B, shadowed_refs: ['debug-project'] }] })
      mockApi.removeWorkflowDefinition
        .mockResolvedValueOnce({
          ok: true,
          id: 'wfd_1',
          removed_at: '2026-10-24T00:00:00Z',
        })
        .mockResolvedValueOnce({
          ok: true,
          id: 'wfd_2',
          removed_at: '2026-10-24T00:01:00Z',
        })
      const { client } = renderTab()
      await editorShown()
      const nameField = () =>
        screen.getByLabelText(
          i18nT('pages.overview.workflowLibrary.name'),
        ) as HTMLInputElement

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      fireEvent.click(
        within(await screen.findByRole('dialog')).getByRole('button', {
          name: removeLabel(),
        }),
      )
      // A's removal reads the library again, and that read is still in
      // flight when the workflow now shown, Triage Tickets, is removed too.
      await waitFor(() =>
        expect(mockApi.workflowDefinitions).toHaveBeenCalledTimes(2),
      )
      await waitFor(() => expect(nameField().value).toBe('Triage Tickets'))
      await waitFor(() =>
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
      )
      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      fireEvent.click(
        within(await screen.findByRole('dialog')).getByRole('button', {
          name: removeLabel(),
        }),
      )

      const listed = () =>
        client.getQueryData<{ definitions: WorkflowDefinition[] }>([
          'workflow-definitions',
        ])?.definitions
      await waitFor(() => expect(listed()).toEqual([{ ...B, shadowed_refs: ['debug-project'] }]))
      // The first re-read answers last, from before either tombstone. The
      // second removal's read cancelled it, so it writes nothing.
      await act(async () => {
        answerFirstReRead({
          definitions: [{ ...DEFINITION, shadowed_refs: ['debug-project'] }, OTHER, B],
        })
      })
      expect(listed()).toEqual([{ ...B, shadowed_refs: ['debug-project'] }])
      expect(mockApi.workflowDefinitions).toHaveBeenCalledTimes(3)
      await waitFor(() => expect(nameField().value).toBe('Hand Placed'))
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
          'hand-placed',
          '',
        ),
      )
      await waitFor(() =>
        expect(mockApi.updateWorkflowDefinition).toHaveBeenCalledWith(
          'hand-placed',
          expect.objectContaining({ expected_id: 'debug-project' }),
        ),
      )
    })

    it('keeps a removed workflow out of a list read still in flight when it lands', async () => {
      const SAVED_MEANWHILE = {
        ...DEFINITION,
        id: 'wfd_3',
        slug: 'saved-meanwhile',
        name: 'Saved Meanwhile',
      }
      let answerEarlyRead: (value: unknown) => void = () => {}
      mockApi.workflowDefinitions
        .mockResolvedValueOnce({ definitions: [DEFINITION, OTHER] })
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              answerEarlyRead = resolve
            }),
        )
        .mockResolvedValue({ definitions: [OTHER, SAVED_MEANWHILE] })
      mockApi.removeWorkflowDefinition.mockResolvedValue({
        ok: true,
        id: 'wfd_1',
        removed_at: '2026-10-11T00:00:00Z',
      })
      const { client } = renderTab()
      await editorShown()

      // Something else (a draft saved meanwhile) has a list read in flight,
      // answered from before the removal, when the removal is confirmed.
      act(() => {
        void client.invalidateQueries({ queryKey: ['workflow-definitions'] })
      })
      await waitFor(() =>
        expect(mockApi.workflowDefinitions).toHaveBeenCalledTimes(2),
      )
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

      // The early read lands last and writes nothing: the removed workflow
      // stays out, and the library read again brings back what it carried.
      await act(async () => {
        answerEarlyRead({ definitions: [DEFINITION, OTHER, SAVED_MEANWHILE] })
      })
      expect(await screen.findByText('/workflow saved-meanwhile')).toBeInTheDocument()
      expect(screen.queryByText('/workflow debug-project')).not.toBeInTheDocument()
      expect(mockApi.workflowDefinitions).toHaveBeenCalledTimes(3)
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
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledWith('wfd_1', 'wfd_1'),
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
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledWith('wfd_1', 'wfd_1'),
      )

      fireEvent.click(screen.getAllByText('Triage Tickets')[0])
      const nameField = screen.getByLabelText(
        i18nT('pages.overview.workflowLibrary.name'),
      ) as HTMLInputElement
      await waitFor(() => expect(nameField.value).toBe('Triage Tickets'))
      // The removal in flight is Debug Project's, so Triage Tickets' Remove is
      // only disabled: no "Removing…" label, spinner or busy state on it. The
      // dialog's own confirm is also named Remove, so look once it has left.
      await waitFor(() =>
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
      )
      const triageRemove = screen.getByRole('button', { name: removeLabel() })
      expect(triageRemove).toBeDisabled()
      expect(triageRemove).not.toHaveAttribute('aria-busy')
      expect(
        screen.queryByText(i18nT('pages.overview.workflowLibrary.removing')),
      ).not.toBeInTheDocument()
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
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledWith('wfd_1', 'wfd_1'),
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
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledWith('wfd_1', 'wfd_1'),
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
      // Details is the notice's only control. The retry the failure line asks
      // for follows the notice, described by it.
      expect(within(alert).getAllByRole('button')).toEqual([details])
      const retry = screen.getByRole('button', {
        name: i18nT('pages.overview.workflowLibrary.remove_retry'),
      })
      expect(alert.contains(retry)).toBe(false)
      expect(alert.id).not.toBe('')
      expect(retry).toHaveAttribute('aria-describedby', alert.id)
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
      await screen.findByRole('alert')
      await waitFor(() =>
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
      )

      fireEvent.click(
        screen.getByRole('button', {
          name: i18nT('pages.overview.workflowLibrary.remove_retry'),
        }),
      )

      // The user already confirmed this removal, so the retry sends it
      // again without a second dialog.
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      await waitFor(() =>
        expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledTimes(2),
      )
      expect(mockApi.removeWorkflowDefinition).toHaveBeenLastCalledWith('wfd_1', 'wfd_1')
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
      await screen.findByRole('alert')
      const retry = screen.getByRole('button', {
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
      // The failure the retry answers is not the outcome while the retry runs,
      // so no failure line sits beside its progress cue.
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(retry).not.toHaveAttribute('aria-describedby')
      expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledTimes(2)

      rejectRetry(failure())
      await waitFor(() => expect(retry).not.toHaveAttribute('aria-busy'))
      expect(retry).toBeInTheDocument()
      expect(retry).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.remove_retry'),
      )
      expect(document.activeElement).toBe(retry)
      expect(screen.getByRole('alert')).toBeInTheDocument()
      expect(retry).toHaveAttribute('aria-describedby', screen.getByRole('alert').id)
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
      await screen.findByRole('alert')
      await waitFor(() =>
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
      )
      return screen.getByRole('button', {
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
      mockApi.removeWorkflowDefinition.mockImplementation(async () => {
        // Another tab removed it first: the library no longer lists it.
        mockApi.workflowDefinitions.mockResolvedValue({ definitions: [OTHER] })
        throw Object.assign(new Error('no such saved workflow'), { status: 404 })
      })
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

    it('keeps the row and shows the failure when a 404 leaves the record in the library', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      mockApi.removeWorkflowDefinition.mockRejectedValue(
        Object.assign(new Error('no such saved workflow'), { status: 404 }),
      )
      renderTab()
      await editorShown()
      const readsBefore = mockApi.workflowDefinitions.mock.calls.length

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      expect(await screen.findByRole('alert')).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.remove_failed', {
          name: 'Debug Project',
        }),
      )
      expect(mockApi.workflowDefinitions.mock.calls.length).toBeGreaterThan(
        readsBefore,
      )
      expect(screen.getAllByText('/workflow debug-project')).toHaveLength(2)
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
    })

    it('settles as removed when the response is lost after the removal committed', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      mockApi.removeWorkflowDefinition.mockImplementation(async () => {
        // The tombstone is written, then the response body read is cut.
        mockApi.workflowDefinitions.mockResolvedValue({ definitions: [OTHER] })
        throw new TypeError('network error')
      })
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
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it('keeps the row and shows the failure when a lost response leaves the record in the library', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      mockApi.removeWorkflowDefinition.mockRejectedValue(
        new TypeError('network error'),
      )
      renderTab()
      await editorShown()
      const readsBefore = mockApi.workflowDefinitions.mock.calls.length

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      expect(await screen.findByRole('alert')).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.remove_failed', {
          name: 'Debug Project',
        }),
      )
      expect(mockApi.workflowDefinitions.mock.calls.length).toBeGreaterThan(
        readsBefore,
      )
      expect(screen.getAllByText('/workflow debug-project')).toHaveLength(2)
    })

    it('retries by the ref the fresh read gives the workflow, not the one that 404ed', async () => {
      // A removed workflow holds this one's id, so Remove goes by its slug,
      // and another tab renames that slug before the removal arrives.
      const held = { ...DEFINITION, shadowed_refs: ['wfd_1'] }
      mockApi.workflowDefinitions.mockResolvedValue({ definitions: [held, OTHER] })
      mockApi.removeWorkflowDefinition
        .mockImplementationOnce(async () => {
          mockApi.workflowDefinitions.mockResolvedValue({
            definitions: [{ ...held, slug: 'debug-renamed' }, OTHER],
          })
          throw Object.assign(new Error('no such saved workflow'), { status: 404 })
        })
        .mockResolvedValueOnce({
          ok: true,
          id: 'wfd_1',
          removed_at: '2026-10-24T00:00:00Z',
        })
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )
      await screen.findByRole('alert')
      fireEvent.click(
        screen.getByRole('button', {
          name: i18nT('pages.overview.workflowLibrary.remove_retry'),
        }),
      )

      await waitFor(() =>
        expect(
          screen.queryByText('/workflow debug-renamed'),
        ).not.toBeInTheDocument(),
      )
      expect(
        mockApi.removeWorkflowDefinition.mock.calls.map(([ref]) => ref),
      ).toEqual(['debug-project', 'debug-renamed'])
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it('offers no "Try removing again" once the fresh read shows no ref reaches the workflow', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      mockApi.removeWorkflowDefinition.mockImplementation(async () => {
        // Another tab's removal of a copy sharing this one's id and slug
        // left this copy live, reached by no ref.
        mockApi.workflowDefinitions.mockResolvedValue({
          definitions: [
            { ...DEFINITION, shadowed_refs: ['debug-project', 'wfd_1'] },
            OTHER,
          ],
        })
        throw Object.assign(new Error('no such saved workflow'), { status: 404 })
      })
      renderTab()
      await editorShown()

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      await screen.findByRole('alert')
      await waitFor(() =>
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
      )
      await waitFor(() =>
        expect(
          screen.getAllByText(i18nT('pages.overview.workflowLibrary.no_command')),
        ).toHaveLength(2),
      )
      expect(
        screen.queryByRole('button', {
          name: i18nT('pages.overview.workflowLibrary.remove_retry'),
        }),
      ).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: removeLabel() })).toBeDisabled()
      expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledTimes(1)
    })

    it('reads the library afresh after a 404, not through a list read already in flight', async () => {
      const { client } = renderTab()
      await editorShown()
      mockApi.removeWorkflowDefinition.mockImplementation(async () => {
        // Another tab removed it. A list read this tab started before that
        // removal is still in flight when the DELETE answers 404.
        let release: () => void = () => {}
        mockApi.workflowDefinitions.mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              release = () => resolve({ definitions: [DEFINITION, OTHER] })
            }),
        )
        void client.refetchQueries({ queryKey: ['workflow-definitions'] })
        // Every read that starts from now on sees the server as it is.
        mockApi.workflowDefinitions.mockResolvedValue({ definitions: [OTHER] })
        setTimeout(() => release(), 20)
        throw Object.assign(new Error('no such saved workflow'), { status: 404 })
      })

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
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it('keeps the row and re-reads the library when Remove answers 409 because the ref moved', async () => {
      mockApi.workflowDefinitions.mockResolvedValue({
        definitions: [DEFINITION, OTHER],
      })
      mockApi.removeWorkflowDefinition.mockRejectedValue(
        Object.assign(
          new Error('that name now reaches a different saved workflow; reload the library'),
          { status: 409 },
        ),
      )
      renderTab()
      await editorShown()
      const readsBefore = mockApi.workflowDefinitions.mock.calls.length

      fireEvent.click(screen.getByRole('button', { name: removeLabel() }))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(
        within(dialog).getByRole('button', { name: removeLabel() }),
      )

      expect(await screen.findByRole('alert')).toHaveTextContent(
        i18nT('pages.overview.workflowLibrary.remove_failed', {
          name: 'Debug Project',
        }),
      )
      expect(mockApi.removeWorkflowDefinition).toHaveBeenCalledWith('wfd_1', 'wfd_1')
      expect(mockApi.workflowDefinitions.mock.calls.length).toBeGreaterThan(
        readsBefore,
      )
      expect(screen.getAllByText('/workflow debug-project')).toHaveLength(2)
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
    })
  })
})
