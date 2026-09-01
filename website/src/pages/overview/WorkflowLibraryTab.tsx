import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  GitBranch,
  Library,
  ListTree,
  Loader2,
  Play,
  Plus,
  Save,
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
import { activeLocale, fmtDateTime } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
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
  const visible = useMemo(() => {
    const locale = activeLocale()
    const query = filter.trim().toLocaleLowerCase(locale)
    if (!query) return definitions
    return definitions.filter((definition) =>
      `${definition.name} ${definition.slug} ${definition.description}`
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
      definitionId,
      body,
    }: {
      definitionId: string
      body: WorkflowDefinitionUpdate
      generation: number
      editorSnapshot: EditorState
    }) => api.updateWorkflowDefinition(definitionId, body),
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
      slug,
      input,
    }: {
      slug: string
      input: string
      generation: number
    }) => api.runWorkflowDefinition(slug, input),
    onSuccess: (result, variables) => {
      if (variables.generation === editorGeneration.current) {
        setLastRunId(result.run_id)
      }
    },
  })

  const choose = (definition: WorkflowDefinition) => {
    editorGeneration.current += 1
    setCreating(false)
    setSelectedId(definition.id)
    setEditor(editorFromDefinition(definition))
    setEditorRevision(definition.revision)
    setRevisionConflict(false)
    authorDraft.reset()
    saveDraft.reset()
    saveRevision.reset()
    runDefinition.reset()
    setLastRunId('')
  }

  const beginCreate = () => {
    editorGeneration.current += 1
    setCreating(true)
    setSelectedId(null)
    setIntent('')
    setEditor(EMPTY_EDITOR)
    setEditorRevision(null)
    setRevisionConflict(false)
    authorDraft.reset()
    saveDraft.reset()
    saveRevision.reset()
    runDefinition.reset()
    setLastRunId('')
  }

  const write = (
    key: keyof Omit<EditorState, 'derivedFrom' | 'format'>,
    value: string,
  ) =>
    setEditor((current) => ({ ...current, [key]: value }))

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
                  <span className="block text-[11px] text-muted font-mono truncate">
                    /workflow {definition.slug}
                  </span>
                </Btn>
              ))
            )}
          </div>
        </aside>

        <section className="min-w-0">
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
                  className="w-full min-h-24 bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none focus-ring"
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
                <code className="text-[12px] text-accent">
                  /workflow {selected.slug}
                </code>
              </div>
              <p className="text-[11px] text-muted">
                {i18nT('pages.overview.workflowLibrary.updated', {
                  date: fmtDateTime(selected.updated_at),
                })}
              </p>
              {selected.derived_from ? (
                <Lineage lineage={selected.derived_from} />
              ) : null}
              <WorkflowEditor editor={editor} write={write} />
              <div className="flex flex-wrap gap-2">
                <Btn
                  primary
                  onClick={() =>
                    saveRevision.mutate({
                      definitionId: selected.id,
                      body: {
                        source: editor.source,
                        name: editor.name,
                        description: editor.description,
                        slug: editor.slug,
                        expected_revision: editorRevision!,
                      },
                      generation: editorGeneration.current,
                      editorSnapshot: editor,
                    })
                  }
                  disabled={
                    editorRevision === null ||
                    revisionConflict ||
                    saveRevision.isPending
                  }
                >
                  <Save className="lucide-inline" />{' '}
                  {i18nT('pages.overview.workflowLibrary.save_revision')}
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
                  onClick={() =>
                    runDefinition.mutate({
                      slug: selected.slug,
                      input: runInput,
                      generation: editorGeneration.current,
                    })
                  }
                  disabled={runDefinition.isPending}
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
    </Card>
  )
}

function WorkflowEditor({
  editor,
  write,
}: {
  editor: EditorState
  write: (
    key: keyof Omit<EditorState, 'derivedFrom' | 'format'>,
    value: string,
  ) => void
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
          onChange={(value) => write('source', value)}
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
