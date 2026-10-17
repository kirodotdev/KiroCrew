/**
 * The task runner (/api/taskrunner): run status/start/cancel/pause/delete/
 * retry/rename, step edits, the hand-off to chat, input refinement,
 * plan/cancel/edit/execute, plan-from-chat, plan context and YAML export.
 */

import { toApiError } from '../apiError'
import type { ClientTransport } from './transport'

/**
 * A single task-runner plan step as sent to the server. Known fields are
 * typed; the payload is forwarded verbatim, so extra fields are permitted via
 * the index signature.
 */
export interface PlanStepInput {
  title?: string
  description?: string
  depends_on?: number[]
  requires_approval?: boolean
  [key: string]: unknown
}

export function createTaskRunnerEndpoints({ get, post, put, del, j, jfetch: fetch }: ClientTransport) {
  const runs = {
    // Task runner
    taskRunnerStatus: () => fetch('/api/taskrunner').then(j),
    startTaskRunner: (spec: string, agent?: string, workspaceDir?: string) => post('/api/taskrunner', { spec, agent: agent || '', workspace_dir: workspaceDir || '' }).then(j),
    cancelTaskRunner: (taskId?: string) => post('/api/taskrunner/cancel', taskId ? { task_id: taskId } : undefined).then(j),
    pauseTaskRun: (taskId: string) => post('/api/taskrunner/' + encodeURIComponent(taskId) + '/pause').then(j),
    deleteTaskRun: (taskId: string) => del('/api/taskrunner/' + encodeURIComponent(taskId)).then(j),
    retryTaskRun: (taskId: string, fromStep: number) => post('/api/taskrunner/' + encodeURIComponent(taskId) + '/retry', { from_step: fromStep }).then(j),
    renameTaskRun: (taskId: string, name: string) => fetch('/api/taskrunner/' + encodeURIComponent(taskId) + '/name', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }).then(j),
    updateTask: (taskId: string, index: number, updates: { title?: string; description?: string; depends_on?: number[]; requires_approval?: boolean; force_approval?: boolean }) => fetch('/api/taskrunner/' + encodeURIComponent(taskId) + '/tasks/' + index, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(updates) }).then(j),
    taskRunToChat: (taskId: string) => post('/api/taskrunner/' + encodeURIComponent(taskId) + '/to-chat').then(j),
  }

  const plans = {
    refineTaskInput: (input: string) => post('/api/taskrunner/refine', { input }).then(j),
    refineStatus: () => fetch('/api/taskrunner/refine').then(j),
    refineCancel: () => post('/api/taskrunner/refine/cancel').then(j),
    planTask: (input: string, source: string, spec?: string, agent?: string, workspaceDir?: string) =>
      post('/api/taskrunner/plan', { input, source, spec: spec || '', agent: agent || '', workspace_dir: workspaceDir || '' }).then(j),
    cancelPlan: () => post('/api/taskrunner/plan/cancel').then(j),
    updatePlan: (taskId: string, steps: PlanStepInput[]) =>
      put('/api/taskrunner/' + encodeURIComponent(taskId) + '/plan', { steps }).then(j),
    executePlan: (taskId: string, agent?: string, autoApprove?: boolean) =>
      post('/api/taskrunner/' + encodeURIComponent(taskId) + '/execute', { agent: agent || '', auto_approve: !!autoApprove }).then(j),
    planFromChat: (steps: PlanStepInput[], taskId?: string, originalInput?: string) =>
      post('/api/taskrunner/from-chat', { steps, task_id: taskId || '', original_input: originalInput || '' }).then(j),
    planContext: (taskId: string) =>
      fetch('/api/taskrunner/' + encodeURIComponent(taskId) + '/plan-context').then(j),
    /** Download the run's plan as a YAML workflow (re-importable via the "From YAML" tab).
     *  Fetches with the auth header, then triggers a browser download honoring the
     *  server's sanitized Content-Disposition filename. */
    exportPlanYaml: async (taskId: string) => {
      const r = await get('/api/taskrunner/' + encodeURIComponent(taskId) + '/plan.yaml')
      if (!r.ok) {
        throw await toApiError(r)
      }
      const blob = await r.blob()
      const cd = r.headers.get('Content-Disposition') || ''
      const m = /filename="?([^";]+)"?/.exec(cd)
      const filename = (m && m[1]) || `${taskId}.yaml`
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    },
  }

  return { runs, plans }
}
