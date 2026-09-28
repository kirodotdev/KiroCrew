/**
 * The dynamic-workflow engine (/api/workflows): the run list, definition
 * list/save/update/run, authoring from intent, and promoting a run to a
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
}

export interface WorkflowDefinitionWrite {
  source: string
  format?: 'python' | 'task-plan'
  name?: string
  description?: string
  slug?: string
  derived_from?: WorkflowLineage | null
}

export function createWorkflowsEndpoints({ get, post, patch, j }: ClientTransport) {
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
      body: Omit<WorkflowDefinitionWrite, 'derived_from'> & { expected_revision: number },
    ) => patch(`/api/workflows/definitions/${encodeURIComponent(workflowRef)}`, body).then(j) as Promise<{ ok: boolean; definition: WorkflowDefinition }>,
    runWorkflowDefinition: (workflowRef: string, input: string, args: Record<string, unknown> = {}) =>
      post(`/api/workflows/definitions/${encodeURIComponent(workflowRef)}/run`, { input, args }).then(j) as Promise<{ run_id: string; workflow_id: string; revision: number; slug: string }>,
  }

  return { engine }
}
