import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  GitBranch,
  Library,
  ListTree,
  Loader2,
  Play,
  Plus,
  Save,
  Trash2,
  Workflow,
} from 'lucide-react'

import {
  api,
  type WorkflowDefinition,
  type WorkflowDefinitionWrite,
  type WorkflowLineage,
} from '../../api/client'
import {
  Badge,
  Btn,
  Card,
  CardTitle,
  Input,
  SearchInput,
} from '../../components/ui'
import { isNotFoundError } from '../../api/apiError'
import { useConfirm } from '../../components/ConfirmDialog'
import ErrorDetails from '../../components/ErrorDetails'
import ErrorNotice from '../../components/ErrorNotice'
import { activeLocale, fmtDateTime } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { reportForError } from '../../utils/errorReport'
import SegmentedControl from '../../components/SegmentedControl'
import WorkflowSourceCode from '../../apps/workflows/WorkflowSourceCode'
import WorkflowsRuns from '../../apps/workflows/WorkflowsRuns'

interface EditorState {
  name: string
  description: string
  slug: string
  source: string
  format: 'python' | 'task-plan'
  derivedFrom: WorkflowLineage | null
}

type WorkflowDefinitionUpdate = Omit<
  WorkflowDefinitionWrite,
  'derived_from'
> & {
  expected_revision: number
  expected_id: string
}

type WorkflowManagementView = 'library' | 'runs'

const EMPTY_EDITOR: EditorState = {
  name: '',
  description: '',
  slug: '',
  source: '',
  format: 'python',
  derivedFrom: null,
}

function editorFromDefinition(definition: WorkflowDefinition): EditorState {
  return {
    name: definition.name,
    description: definition.description,
    slug: definition.slug,
    source: definition.source,
    format: definition.format ?? 'python',
    derivedFrom: definition.derived_from,
  }
}

/** `ref` when it names something a request can address, else null: never a
 *  blank string. */
function usableRef(ref: string | null | undefined): string | null {
  return ref && ref.trim() ? ref : null
}

/** `ref` when it names something a request can address and the read did not
 *  list it as shadowed, so it reaches this workflow; else null. */
function reachingRef(
  definition: WorkflowDefinition,
  ref: string | null | undefined,
): string | null {
  const usable = usableRef(ref)
  return usable && !definition.shadowed_refs?.includes(usable) ? usable : null
}

/** The ref `/workflow` reaches a saved workflow by: its slug, else its id, or
 *  null when neither reaches it. */
function commandRef(definition: WorkflowDefinition): string | null {
  return (
    reachingRef(definition, definition.slug) ??
    reachingRef(definition, definition.id)
  )
}

/** The ref the editor's requests address a saved workflow by: its id, which a
 *  rename does not move, else its slug, or null when neither reaches it. */
function actionRef(definition: WorkflowDefinition): string | null {
  return (
    reachingRef(definition, definition.id) ??
    reachingRef(definition, definition.slug)
  )
}

function editorsMatch(left: EditorState, right: EditorState): boolean {
  return (
    left.name === right.name &&
    left.description === right.description &&
    left.slug === right.slug &&
    left.source === right.source &&
    left.format === right.format &&
    left.derivedFrom?.workflow_id === right.derivedFrom?.workflow_id &&
    left.derivedFrom?.revision === right.derivedFrom?.revision
  )
}

function errorText(error: unknown): string {
  return error instanceof Error
    ? error.message
    : i18nT('pages.overview.workflowLibrary.request_failed')
}

export default function WorkflowLibraryTab() {
  const queryClient = useQueryClient()
  const [view, setView] = useState<WorkflowManagementView>('library')
  const [filter, setFilter] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [intent, setIntent] = useState('')
  const [editor, setEditor] = useState<EditorState>(EMPTY_EDITOR)
  const [editorRevision, setEditorRevision] = useState<number | null>(null)
  const [revisionConflict, setRevisionConflict] = useState(false)
  const [runInput, setRunInput] = useState('')
  const [lastRunId, setLastRunId] = useState('')
  const editorGeneration = useRef(0)
  const { confirm, confirmDialog } = useConfirm()

  const { data, isLoading, error } = useQuery({
    queryKey: ['workflow-definitions'],
    queryFn: () => api.workflowDefinitions(),
  })
  const definitions = useMemo(() => data?.definitions ?? [], [data])
  const selected = useMemo(
    () =>
      definitions.find((definition) => definition.id === selectedId) ?? null,
    [definitions, selectedId],
  )
  // The ref the editor's requests use; null when no ref reaches the workflow.
  const selectedRef = selected ? actionRef(selected) : null
  const visible = useMemo(() => {
    const locale = activeLocale()
    const query = filter.trim().toLocaleLowerCase(locale)
    if (!query) return definitions
    return definitions.filter((definition) =>
      // The ref the row offers too: for a record whose slug does not reach
      // it that is its id, which the row shows and a reader may type.
      `${definition.name} ${definition.slug} ${definition.description} ${
        commandRef(definition) ?? ''
      }`
        .toLocaleLowerCase(locale)
        .includes(query),
    )
  }, [definitions, filter])

  useEffect(() => {
    if (creating || selectedId || definitions.length === 0) return
    editorGeneration.current += 1
    setSelectedId(definitions[0].id)
    setEditor(editorFromDefinition(definitions[0]))
    setEditorRevision(definitions[0].revision)
  }, [creating, definitions, selectedId])

  const authorDraft = useMutation({
    mutationFn: async ({
      requestedIntent,
    }: {
      requestedIntent: string
      generation: number
    }) => {
      const result = await api.authorWorkflow(requestedIntent)
      if (!result.ok) {
        throw new Error(i18nT('pages.overview.workflowLibrary.request_failed'))
      }
      return result
    },
    onSuccess: (result, variables) => {
      if (variables.generation !== editorGeneration.current) return
      setEditor({
        name: result.meta?.name ?? '',
        description: result.meta?.description ?? '',
        slug: result.meta?.name ?? '',
        source: result.source,
        format: 'python',
        derivedFrom: result.derived_from ?? null,
      })
    },
  })

  const saveDraft = useMutation({
    mutationFn: ({
      body,
    }: {
      body: WorkflowDefinitionWrite
      generation: number
      editorSnapshot: EditorState
    }) => api.saveWorkflowDefinition(body),
    onSuccess: async (result, variables) => {
      await queryClient.invalidateQueries({
        queryKey: ['workflow-definitions'],
      })
      if (variables.generation !== editorGeneration.current) return
      editorGeneration.current += 1
      setCreating(false)
      setSelectedId(result.definition.id)
      setEditor((current) =>
        editorsMatch(current, variables.editorSnapshot)
          ? editorFromDefinition(result.definition)
          : current,
      )
      setEditorRevision(result.definition.revision)
      setRevisionConflict(false)
    },
  })

  const saveRevision = useMutation({
    mutationFn: ({
      workflowRef,
      body,
    }: {
      workflowRef: string
      body: WorkflowDefinitionUpdate
      generation: number
      editorSnapshot: EditorState
    }) => api.updateWorkflowDefinition(workflowRef, body),
    onSuccess: async (result, variables) => {
      queryClient.setQueryData<{ definitions: WorkflowDefinition[] }>(
        ['workflow-definitions'],
        (current) => ({
          definitions: (current?.definitions ?? []).map((definition) =>
            definition.id === result.definition.id
              ? result.definition
              : definition,
          ),
        }),
      )
      if (variables.generation !== editorGeneration.current) return
      setEditor((current) =>
        editorsMatch(current, variables.editorSnapshot)
          ? editorFromDefinition(result.definition)
          : current,
      )
      setEditorRevision(result.definition.revision)
      setRevisionConflict(false)
    },
    onError: async (error, variables) => {
      if ((error as { status?: unknown })?.status !== 409) return
      await queryClient.invalidateQueries({
        queryKey: ['workflow-definitions'],
      })
      if (variables.generation === editorGeneration.current) {
        setRevisionConflict(true)
      }
    },
  })

  const runDefinition = useMutation({
    mutationFn: ({
      workflowRef,
      input,
    }: {
      workflowRef: string
      input: string
      generation: number
    }) => api.runWorkflowDefinition(workflowRef, input),
    onSuccess: (result, variables) => {
      if (variables.generation === editorGeneration.current) {
        setLastRunId(result.run_id)
      }
    },
  })

  const dropDefinition = (definitionId: string) => {
    const remaining = (
      queryClient.getQueryData<{ definitions: WorkflowDefinition[] }>([
        'workflow-definitions',
      ])?.definitions ?? []
    ).filter((definition) => definition.id !== definitionId)
    queryClient.setQueryData<{ definitions: WorkflowDefinition[] }>(
      ['workflow-definitions'],
      { definitions: remaining },
    )
    if (selectedId !== definitionId) return
    // Nothing the removed workflow left behind (a revision conflict, a save
    // or run outcome, its run input) may carry over onto the next one. The
    // next one is selected here, in the same update, the way the auto-select
    // effect would pick it: left to that effect, it lands a commit later and
    // the "removed" note shows over an empty editor in between.
    resetEditorOutcomes()
    setRunInput('')
    const next = remaining[0]
    setSelectedId(next?.id ?? null)
    if (!next) return
    setEditor(editorFromDefinition(next))
    setEditorRevision(next.revision)
  }

  const removeDefinition = useMutation({
    mutationFn: async ({
      definitionId,
      workflowRef,
    }: {
      definitionId: string
      workflowRef: string
      name: string
    }) => {
      try {
        await api.removeWorkflowDefinition(workflowRef, definitionId)
      } catch (error) {
        const status = (error as { status?: unknown })?.status
        if (!isNotFoundError(error) && status !== 409) throw error
        // A 404 can mean another tab already removed it, the outcome the user
        // asked for, or that the ref no longer reaches a record still on disk;
        // a 409 means the ref now reaches a different workflow, which the
        // server refused to remove. Only a fresh read of the library tells
        // them apart, so the removal settles as a success only when that read
        // no longer has the record. A list read already in flight may predate
        // the change, so it is cancelled rather than joined.
        await queryClient.cancelQueries({ queryKey: ['workflow-definitions'] })
        const fresh = await queryClient.fetchQuery({
          queryKey: ['workflow-definitions'],
          queryFn: () => api.workflowDefinitions(),
          staleTime: 0,
        })
        const stillThere = (fresh.definitions ?? []).some(
          (definition) => definition.id === definitionId,
        )
        if (stillThere || status === 409) throw error
      }
    },
    // The tab drops the row itself only once the server confirms the
    // removal, so a failed one leaves the row where the user saw it.
    onSuccess: (_result, variables) => {
      dropDefinition(variables.definitionId)
    },
    // Whatever the outcome, the library is read again once the removal
    // settles. A removal can change other records' reserved refs (a
    // hand-placed record whose id is the removed slug now answers only to its
    // own slug), and so can one that failed after tombstoning one of several
    // live copies. A list read still in flight may predate the tombstone and
    // would write the removed workflow back, so the new read cancels it
    // (TanStack's cancelRefetch) and brings back anything else that read
    // carried, such as a draft just saved.
    onSettled: () => {
      void queryClient.invalidateQueries({
        queryKey: ['workflow-definitions'],
      })
    },
  })

  // A retry re-sends a removal that failed. While it is in flight that
  // failure is not the current outcome, so the failure notice is hidden and
  // the retry, which keeps its place and its focus, carries the progress cue.
  // Only a settled failure retries, so a second click while the retry is
  // pending sends nothing.
  const [retryStarted, setRetryStarted] = useState(false)
  const retrying = retryStarted && removeDefinition.isPending
  // The failed request may have used a ref the library no longer answers to
  // (a partial removal reserved the id, or another tab renamed the slug), so
  // a retry goes by the ref the latest list read gives the workflow. When
  // that read lists it with no ref, or not at all, nothing can reach it and
  // the retry is not offered.
  const retryTarget = removeDefinition.variables
    ? definitions.find(
        (definition) =>
          definition.id === removeDefinition.variables?.definitionId,
      )
    : undefined
  const retryRef = retryTarget ? actionRef(retryTarget) : null
  const removeFailureId = useId()
  // The editor's Remove is the progress cue only for the workflow it shows:
  // a removal still in flight for another workflow leaves it plainly disabled.
  const removingHere =
    removeDefinition.isPending &&
    !retrying &&
    removeDefinition.variables?.definitionId === selected?.id
  // A successful retry unmounts the notice and the focused retry with it, so
  // focus moves to the editor's Remove for the workflow now shown, or to the
  // "removed" line when the library is now empty. It waits a frame for that
  // editor or line to mount.
  const removeButtonRef = useRef<HTMLButtonElement>(null)
  const removedNoteRef = useRef<HTMLParagraphElement>(null)
  const retryRemove = () => {
    const variables = removeDefinition.variables
    // No ref reaches it now: its row says so, and the retry is not offered.
    if (!removeDefinition.isError || !variables || retryRef === null) return
    setRetryStarted(true)
    removeDefinition.mutate(
      { ...variables, workflowRef: retryRef },
      {
        onSuccess: () =>
          requestAnimationFrame(() =>
            (removeButtonRef.current ?? removedNoteRef.current)?.focus(),
          ),
        onSettled: () => setRetryStarted(false),
      },
    )
  }

  const confirmRemove = async (definition: WorkflowDefinition) => {
    const workflowRef = actionRef(definition)
    // Remove is disabled when no ref reaches the workflow.
    if (workflowRef === null) return
    const confirmed = await confirm({
      title: i18nT('pages.overview.workflowLibrary.remove_confirm_title', {
        name: definition.name,
      }),
      body: (
        <>
          <strong className="font-semibold">
            {i18nT('pages.overview.workflowLibrary.remove_confirm_permanent')}
          </strong>
          {/* The dialog renders its body inside a paragraph, so the list is
              phrasing content: spans with list roles shown as list items. */}
          <span role="list" className="block mt-2 pl-5 list-disc space-y-1">
            <span role="listitem" className="list-item">
              {i18nT('pages.overview.workflowLibrary.remove_confirm_listing')}
            </span>
            <span role="listitem" className="list-item">
              {i18nT('pages.overview.workflowLibrary.remove_confirm_name', {
                slug: commandRef(definition) ?? '',
              })}
            </span>
            <span role="listitem" className="list-item">
              {i18nT('pages.overview.workflowLibrary.remove_confirm_lineage')}
            </span>
          </span>
        </>
      ),
      confirmLabel: i18nT('pages.overview.workflowLibrary.remove'),
    })
    if (!confirmed) return
    removeDefinition.mutate({
      definitionId: definition.id,
      workflowRef,
      name: definition.name,
    })
  }

  // Clears what the current editor's own actions produced. Every switch of
  // the editor to another workflow goes through here, so a late response
  // for the previous one is dropped by the generation bump.
  function resetEditorOutcomes() {
    editorGeneration.current += 1
    setRevisionConflict(false)
    authorDraft.reset()
    saveDraft.reset()
    saveRevision.reset()
    runDefinition.reset()
    setLastRunId('')
  }

  // The "removed" note describes the last removal only until the user acts
  // on the workflow now shown, so it never reads as being about that one.
  const clearRemovedNote = () => {
    if (removeDefinition.isSuccess) removeDefinition.reset()
  }

  // An in-flight removal is never reset. Resetting detaches its observer, so
  // its callbacks would keep the selection they closed over (and clear the
  // editor now shown) and its failure would show nowhere.
  const resetSettledRemoval = () => {
    if (!removeDefinition.isPending) removeDefinition.reset()
  }

  const choose = (definition: WorkflowDefinition) => {
    resetEditorOutcomes()
    resetSettledRemoval()
    setCreating(false)
    setSelectedId(definition.id)
    setEditor(editorFromDefinition(definition))
    setEditorRevision(definition.revision)
  }

  const beginCreate = () => {
    resetEditorOutcomes()
    resetSettledRemoval()
    setCreating(true)
    setSelectedId(null)
    setIntent('')
    setEditor(EMPTY_EDITOR)
    setEditorRevision(null)
  }

  const write = (
    key: keyof Omit<EditorState, 'derivedFrom' | 'format'>,
    value: string,
  ) => {
    clearRemovedNote()
    setEditor((current) => ({ ...current, [key]: value }))
  }

  // The notice carries no hand-off (it would leave this page with the editor's
  // unsaved edits), so the recorded cause sits behind a collapsed Details
  // disclosure instead: the status, the backend code and the failing path.
  const shownRemoveError = removeDefinition.isError
    ? removeDefinition.error
    : null
  const removeReport = shownRemoveError
    ? reportForError(shownRemoveError)
    : undefined
  const removeCause = removeReport
    ? [removeReport.status, removeReport.code, removeReport.endpoint]
        .filter((part) => part !== undefined && part !== '')
        .join(' · ')
    : ''

  const mutationError =
    authorDraft.error ||
    saveDraft.error ||
    saveRevision.error ||
    runDefinition.error

  return (
    <Card className="min-h-[520px]">
      <CardTitle className="flex-wrap">
        <Workflow className="lucide-inline" />
        {i18nT('apps.workflows.workflowsPage.workflows')}
        {view === 'library' ? (
          <span className="ml-auto">
            <Btn primary onClick={beginCreate}>
              <Plus className="lucide-inline" />{' '}
              {i18nT('pages.overview.workflowLibrary.new_workflow')}
            </Btn>
          </span>
        ) : null}
      </CardTitle>
      <div className="mb-4">
        <SegmentedControl<WorkflowManagementView>
          segments={[
            {
              key: 'library',
              label: i18nT('pages.overview.workflowLibrary.title'),
              icon: <Library size={14} />,
            },
            {
              key: 'runs',
              label: i18nT('pages.hooksPage.runs'),
              icon: <ListTree size={14} />,
            },
          ]}
          value={view}
          onChange={setView}
          layoutId="workflow-management-view"
          collapse={false}
        />
      </div>

      {view === 'runs' ? (
        <WorkflowsRuns embedded />
      ) : (
        <>
          <p className="text-[13px] text-muted mb-4">
            {i18nT('pages.overview.workflowLibrary.description')}
          </p>

          <div className="grid grid-cols-1 md:grid-cols-[260px_minmax(0,1fr)] gap-4">
        <aside className="min-w-0 border border-border rounded-lg p-2 bg-bg-elevated">
          <SearchInput
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder={i18nT('pages.overview.workflowLibrary.search')}
          />
          <div className="mt-2 max-h-[560px] overflow-y-auto [content-visibility:auto]">
            {isLoading ? (
              <p className="text-[12px] text-muted p-2">
                {i18nT('pages.overview.workflowLibrary.loading')}
              </p>
            ) : visible.length === 0 ? (
              <p className="text-[12px] text-muted p-2">
                {i18nT('pages.overview.workflowLibrary.empty')}
              </p>
            ) : (
              visible.map((definition) => (
                <Btn
                  key={definition.id}
                  onClick={() => choose(definition)}
                  className={`w-full mb-1 px-2.5 py-2 justify-start text-left block ${
                    selectedId === definition.id
                      ? 'bg-accent-subtle border-accent/40'
                      : ''
                  }`}
                >
                  <span className="block font-semibold truncate">
                    {definition.name}
                  </span>
                  {commandRef(definition) === null ? (
                    <span className="block text-[11px] text-warn-text truncate">
                      {i18nT('pages.overview.workflowLibrary.no_command')}
                    </span>
                  ) : (
                    <span className="block text-[11px] text-muted font-mono truncate">
                      /workflow {commandRef(definition)}
                    </span>
                  )}
                </Btn>
              ))
            )}
          </div>
        </aside>

        <section className="min-w-0">
          {/* A removal's outcome sits outside the create/edit branches, so it
              shows whichever the user moved to while the request was in
              flight, and a divider keeps it from reading as the status of the
              workflow below. No hand-off on the failure: the hand-off leaves
              this page, and the editor holds unsaved edits. */}
          {removeDefinition.isSuccess || shownRemoveError || retrying ? (
            <div className="mb-3 pb-3 border-b border-border">
              {removeDefinition.isSuccess ? (
                <p
                  ref={removedNoteRef}
                  role="status"
                  tabIndex={-1}
                  className="text-sm text-ok m-0 focus:outline-none"
                >
                  {i18nT('pages.overview.workflowLibrary.removed', {
                    name: removeDefinition.variables?.name ?? '',
                  })}
                </p>
              ) : null}
              <ErrorNotice
                id={removeFailureId}
                message={
                  shownRemoveError
                    ? i18nT('pages.overview.workflowLibrary.remove_failed', {
                        name: removeDefinition.variables?.name ?? '',
                      })
                    : null
                }
                report={removeReport}
                footer={<ErrorDetails details={removeCause} />}
              />
              {/* The retry re-sends the removal the user already confirmed,
                  so the editor and its unsaved edits stay where they are. It
                  sits after the failure notice, not inside it: while it runs
                  the notice is hidden and the retry keeps its place and its
                  focus. In flight it stays focusable (aria-disabled, not
                  disabled, which would blur it) and its label is the progress
                  cue. */}
              {(shownRemoveError || retrying) &&
              removeDefinition.variables &&
              (retrying || retryRef !== null) ? (
                <Btn
                  className={retrying ? 'mt-2 cursor-progress' : 'mt-2'}
                  onClick={retryRemove}
                  aria-describedby={shownRemoveError ? removeFailureId : undefined}
                  aria-disabled={retrying || undefined}
                  aria-busy={retrying || undefined}
                >
                  {retrying ? (
                    <Loader2
                      className="lucide-inline animate-spin"
                      aria-hidden="true"
                    />
                  ) : null}
                  {retrying ? ' ' : null}
                  {retrying
                    ? i18nT('pages.overview.workflowLibrary.removing')
                    : i18nT('pages.overview.workflowLibrary.remove_retry')}
                </Btn>
              ) : null}
            </div>
          ) : null}
          {creating ? (
            <div className="space-y-3">
              <h4 className="text-sm font-semibold text-text-strong">
                {i18nT('pages.overview.workflowLibrary.create_title')}
              </h4>
              <label
                htmlFor="workflow-intent"
                className="block text-[12px] text-muted"
              >
                <span className="block mb-1">
                  {i18nT('pages.overview.workflowLibrary.intent')}
                </span>
                <textarea
                  id="workflow-intent"
                  aria-label={i18nT('pages.overview.workflowLibrary.intent')}
                  value={intent}
                  onChange={(event) => setIntent(event.target.value)}
                  className="w-full min-h-24 bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-hidden focus-ring"
                />
              </label>
              <Btn
                primary
                onClick={() =>
                  authorDraft.mutate({
                    requestedIntent: intent.trim(),
                    generation: editorGeneration.current,
                  })
                }
                disabled={!intent.trim() || authorDraft.isPending}
              >
                {authorDraft.isPending ? (
                  <Loader2 className="lucide-inline animate-spin" />
                ) : (
                  <Workflow className="lucide-inline" />
                )}
                {i18nT('pages.overview.workflowLibrary.create_draft')}
              </Btn>
              {editor.source ? (
                <WorkflowEditor editor={editor} write={write} />
              ) : null}
              {editor.derivedFrom ? (
                <Lineage lineage={editor.derivedFrom} />
              ) : null}
              {editor.source ? (
                <Btn
                  primary
                  onClick={() =>
                    saveDraft.mutate({
                      body: {
                        source: editor.source,
                        format: editor.format,
                        name: editor.name,
                        description: editor.description,
                        slug: editor.slug,
                        derived_from: editor.derivedFrom,
                      },
                      generation: editorGeneration.current,
                      editorSnapshot: editor,
                    })
                  }
                  disabled={saveDraft.isPending}
                >
                  <Save className="lucide-inline" />{' '}
                  {i18nT('pages.overview.workflowLibrary.save_to_library')}
                </Btn>
              ) : null}
            </div>
          ) : selected ? (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <h4 className="text-base font-semibold text-text-strong">
                  {selected.name}
                </h4>
                <Badge variant="muted">
                  {i18nT('pages.overview.workflowLibrary.revision', {
                    revision: selected.revision,
                  })}
                </Badge>
                <Badge variant="aim">{selected.format ?? 'python'}</Badge>
                {selectedRef === null ? (
                  <Badge variant="warn">
                    {i18nT('pages.overview.workflowLibrary.no_command')}
                  </Badge>
                ) : (
                  <code className="text-[12px] text-accent">
                    /workflow {commandRef(selected)}
                  </code>
                )}
              </div>
              <p className="text-[11px] text-muted">
                {i18nT('pages.overview.workflowLibrary.updated', {
                  date: fmtDateTime(selected.updated_at),
                })}
              </p>
              {selectedRef === null ? (
                <p className="text-[12px] text-warn-text">
                  {i18nT('pages.overview.workflowLibrary.no_command_note')}
                </p>
              ) : null}
              {selected.derived_from ? (
                <Lineage lineage={selected.derived_from} />
              ) : null}
              <WorkflowEditor
                editor={editor}
                write={write}
                readOnly={selectedRef === null}
              />
              <div className="flex flex-wrap gap-2">
                <Btn
                  primary
                  onClick={() => {
                    if (selectedRef === null) return
                    clearRemovedNote()
                    saveRevision.mutate({
                      workflowRef: selectedRef,
                      body: {
                        source: editor.source,
                        name: editor.name,
                        description: editor.description,
                        slug: editor.slug,
                        expected_revision: editorRevision!,
                        // A slug the request goes by can be renamed away and
                        // taken: the server refuses a ref that now reaches
                        // another workflow (409) instead of writing into it.
                        expected_id: selected.id,
                      },
                      generation: editorGeneration.current,
                      editorSnapshot: editor,
                    })
                  }}
                  disabled={
                    selectedRef === null ||
                    editorRevision === null ||
                    revisionConflict ||
                    saveRevision.isPending
                  }
                >
                  <Save className="lucide-inline" />{' '}
                  {i18nT('pages.overview.workflowLibrary.save_revision')}
                </Btn>
                <Btn
                  ref={removeButtonRef}
                  danger
                  // While a removal of the workflow shown here is in flight
                  // this button is its only progress cue, so it keeps full
                  // danger contrast instead of the faded disabled look that
                  // reads as "nothing happened". A retry shows its progress on
                  // the retry, and a removal of another workflow is not this
                  // one's, so in both cases this one stays plain, only disabled.
                  className={
                    removingHere
                      ? 'ml-auto disabled:opacity-100 disabled:cursor-progress'
                      : 'ml-auto'
                  }
                  onClick={() => void confirmRemove(selected)}
                  disabled={removeDefinition.isPending || selectedRef === null}
                  aria-busy={removingHere || undefined}
                >
                  {removingHere ? (
                    <Loader2
                      className="lucide-inline animate-spin"
                      aria-hidden="true"
                    />
                  ) : (
                    <Trash2 className="lucide-inline" aria-hidden="true" />
                  )}{' '}
                  {removingHere
                    ? i18nT('pages.overview.workflowLibrary.removing')
                    : i18nT('pages.overview.workflowLibrary.remove')}
                </Btn>
              </div>
              <div className="border-t border-border pt-3">
                <label
                  htmlFor="workflow-run-input"
                  className="block text-[12px] text-muted mb-2"
                >
                  <span className="block mb-1">
                    {editor.format === 'task-plan'
                      ? i18nT('pages.chat.activityViewer.input')
                      : i18nT('pages.overview.workflowLibrary.run_input')}
                  </span>
                  <Input
                    id="workflow-run-input"
                    className="w-full"
                    value={runInput}
                    onChange={(event) => setRunInput(event.target.value)}
                  />
                </label>
                <Btn
                  onClick={() => {
                    if (selectedRef === null) return
                    clearRemovedNote()
                    runDefinition.mutate({
                      workflowRef: selectedRef,
                      input: runInput,
                      generation: editorGeneration.current,
                    })
                  }}
                  disabled={runDefinition.isPending || selectedRef === null}
                >
                  <Play className="lucide-inline" />{' '}
                  {i18nT('pages.overview.workflowLibrary.run')}
                </Btn>
                {lastRunId ? (
                  <span className="ml-2 text-[12px] text-ok">
                    {i18nT('pages.overview.workflowLibrary.started', {
                      runId: lastRunId,
                    })}
                  </span>
                ) : null}
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted">
              {i18nT('pages.overview.workflowLibrary.select_or_create')}
            </p>
          )}

          {error ? (
            <p className="text-sm text-danger mt-3">{errorText(error)}</p>
          ) : null}
          {mutationError ? (
            <p className="text-sm text-danger mt-3">
              {errorText(mutationError)}
            </p>
          ) : null}
        </section>
          </div>
        </>
      )}
      {confirmDialog}
    </Card>
  )
}

function WorkflowEditor({
  editor,
  write,
  readOnly = false,
}: {
  editor: EditorState
  write: (
    key: keyof Omit<EditorState, 'derivedFrom' | 'format'>,
    value: string,
  ) => void
  /** For a workflow no ref reaches: nothing typed here could be saved. */
  readOnly?: boolean
}) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label htmlFor="workflow-name" className="text-[12px] text-muted">
          <span className="block mb-1">
            {i18nT('pages.overview.workflowLibrary.name')}
          </span>
          <Input
            id="workflow-name"
            className="w-full"
            value={editor.name}
            readOnly={readOnly}
            onChange={(event) => write('name', event.target.value)}
          />
        </label>
        <label htmlFor="workflow-slug" className="text-[12px] text-muted">
          <span className="block mb-1">
            {i18nT('pages.overview.workflowLibrary.slug')}
          </span>
          <Input
            id="workflow-slug"
            className="w-full"
            value={editor.slug}
            readOnly={readOnly}
            onChange={(event) => write('slug', event.target.value)}
          />
        </label>
      </div>
      <label
        htmlFor="workflow-description"
        className="block text-[12px] text-muted"
      >
        <span className="block mb-1">
          {i18nT('pages.overview.workflowLibrary.workflow_description')}
        </span>
        <Input
          id="workflow-description"
          className="w-full"
          value={editor.description}
          readOnly={readOnly}
          onChange={(event) => write('description', event.target.value)}
        />
      </label>
      <div className="block text-[12px] text-muted">
        <span className="block mb-1">
          {i18nT('pages.overview.workflowLibrary.source')}
        </span>
        <WorkflowSourceCode
          source={editor.source}
          sourceFormat={editor.format}
          onChange={readOnly ? undefined : (value) => write('source', value)}
          ariaLabel={i18nT('pages.overview.workflowLibrary.source')}
        />
      </div>
    </div>
  )
}

function Lineage({ lineage }: { lineage: WorkflowLineage }) {
  return (
    <div className="flex items-center gap-2 text-[12px] text-muted bg-accent-subtle rounded-md px-3 py-2">
      <GitBranch className="lucide-inline" />
      {i18nT('pages.overview.workflowLibrary.adapted_from')}
      <code>
        {lineage.workflow_id}@{lineage.revision}
      </code>
    </div>
  )
}
