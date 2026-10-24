/**
 * The dynamic-workflow engine (/api/workflows): the run list, definition
 * list/save/update/remove/run, authoring from intent, and promoting a run to a
 * definition.
 */

import type { WorkflowRunSummary } from '../../types'
import type { ClientTransport } from './transport'

export interface WorkflowLineage {
  workflow_id: string
  revision: number
}

export interface WorkflowDefinitionRevision {
  revision: number
  source: string
  created_at: string
}

export interface WorkflowDefinition {
  schema_version: number
  id: string
  slug: string
  name: string
  description: string
  created_at: string
  updated_at: string
  revision: number
  format: 'python' | 'task-plan'
  source: string
  content_hash: string
  derived_from: WorkflowLineage | null
  revisions: WorkflowDefinitionRevision[]
  /** Read-only, set only when true: a removed workflow holds this one's slug,
   *  so `/workflow <slug>` answers not found and it answers only to its id. */
  slug_reserved?: boolean
  /** Read-only, set only when true: a removed workflow holds this one's id,
   *  as its own id (a restored copy, or one a failed removal left live) or as
   *  its slug, so its id answers not found. With `slug_reserved` too, no ref
   *  reaches it until its file is changed or deleted. */
  id_reserved?: boolean
}

export interface WorkflowDefinitionWrite {
  source: string
  format?: 'python' | 'task-plan'
  name?: string
  description?: string
  slug?: string
  derived_from?: WorkflowLineage | null
}

export function createWorkflowsEndpoints({ get, post, patch, del, j }: ClientTransport) {
  const engine = {
    /** Compact list of dynamic-workflow runs, newest first — the AUTHORITY for a
     *  run's status.
     *
     *  Live status reaches the chat only as one-shot `workflow_run_event` frames,
     *  so a client that was closed, asleep, or disconnected when a run ended holds
     *  a row that never leaves `running`. This is the read that corrects it (see
     *  `reconcileWorkflowRuns`). Rejects (503) when the workflows service is
     *  unavailable, which callers must treat as "no evidence" — never as "no runs".
     */
    workflowRuns: () =>
      get('/api/workflows/runs').then(j) as Promise<{ runs?: WorkflowRunSummary[] }>,
    workflowDefinitions: (search = '') =>
      get('/api/workflows/definitions' + (search ? `?q=${encodeURIComponent(search)}` : '')).then(j) as Promise<{ definitions: WorkflowDefinition[] }>,
    authorWorkflow: (intent: string) =>
      post('/api/workflows/author', { intent }).then(j) as Promise<{
        ok: boolean
        source: string
        meta?: { name?: string; description?: string }
        derived_from?: WorkflowLineage | null
        errors?: string[]
      }>,
    saveWorkflowDefinition: (body: WorkflowDefinitionWrite) =>
      post('/api/workflows/definitions', body).then(j) as Promise<{ ok: boolean; definition: WorkflowDefinition }>,
    promoteWorkflowRun: (
      runId: string,
      body: Omit<WorkflowDefinitionWrite, 'source' | 'derived_from'>,
    ) =>
      post(`/api/workflows/runs/${encodeURIComponent(runId)}/promote`, body).then(j) as Promise<{
        ok: boolean
        definition: WorkflowDefinition
      }>,
    updateWorkflowDefinition: (
      workflowRef: string,
      body: Omit<WorkflowDefinitionWrite, 'derived_from'> & {
        expected_revision: number
        /** The id the editor shows: a ref that now reaches another workflow answers 409. */
        expected_id?: string
      },
    ) => patch(`/api/workflows/definitions/${encodeURIComponent(workflowRef)}`, body).then(j) as Promise<{ ok: boolean; definition: WorkflowDefinition }>,
    /** Owner-only. Tombstones the definition: it leaves every list and run
     *  path, while its id still resolves as the parent of adapted workflows.
     *  With `expectedId`, a ref that now reaches another workflow answers 409
     *  and nothing is removed. */
    removeWorkflowDefinition: (workflowRef: string, expectedId?: string) =>
      del(
        `/api/workflows/definitions/${encodeURIComponent(workflowRef)}${
          expectedId ? `?expected_id=${encodeURIComponent(expectedId)}` : ''
        }`,
      ).then(j) as Promise<{
        ok: boolean
        id: string
        removed_at: string
      }>,
    runWorkflowDefinition: (workflowRef: string, input: string, args: Record<string, unknown> = {}) =>
      post(`/api/workflows/definitions/${encodeURIComponent(workflowRef)}/run`, { input, args }).then(j) as Promise<{ run_id: string; workflow_id: string; revision: number; slug: string }>,
  }

  return { engine }
}
