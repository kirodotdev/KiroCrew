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

// ── KAS-native workflows (/api/workflows/kas/*) ──
//
// When the configured agent backend is Kiro Agent (KAS), workflows run on KAS's
// own engine and these routes front it. Every one of them answers 409
// `engine_not_kas` on any other backend, so callers gate on `workflowEngine()`
// first. Shapes mirror `kas_workflows/wire.py` and `mirror.py`.

/** Which engine owns workflows on this gateway. */
export interface WorkflowEngineInfo {
  engine: 'kas' | 'crew'
  backend: string
  /** Whether the backend could serve KAS workflows at all (capability, independent of the opt-in). */
  kas_backend?: boolean
  /** `agent.kas_workflows`: the single opt-in into Kiro Agent's native workflows. */
  kas_workflows?: boolean
}

/** One plan node, as KAS describes it before a run (`NodeDescriptor.to_payload`). */
export interface KasNodeDescriptor {
  id: string
  type: string
  agent: string | null
  label: string | null
  maxIterations: number | null
  children: KasNodeDescriptor[]
}

/** A saved workflow definition KAS can launch. `source` is opaque: pass it back
 *  verbatim. It is null only for a non-built-in recipe KAS listed without one —
 *  kept so it can be named, but not launchable (check `launchable`). */
export interface KasRecipe {
  name: string
  source: string | null
  description: string | null
  builtIn: boolean
  readOnly: boolean
  tier: string | null
  inputs: { name: string; type: string }[]
  plan: KasNodeDescriptor[]
  stepCount: number
  loop: { nodeId: string; maxIterations: number } | null
  validationError: string | null
  /** Where the recipe lives: `bundled` (built-in / `bundled://`), `workspace`
   *  (its file is under the project), else `global`. */
  scope: 'workspace' | 'global' | 'bundled'
  /** The heading it lists under: bundled recipes render under `global`. */
  group: 'workspace' | 'global'
  /** False only for a non-built-in recipe KAS listed without a `source`. */
  launchable: boolean
}

/** One step of a recipe definition, for the detail view (`recipe_detail` `steps`). */
export interface KasRecipeStep {
  nodeId: string
  agent: string | null
  /** The step's prompt text, redacted and capped; null when the file omits it. */
  prompt: string | null
}

/** A recipe with its steps' prompt text (`GET …/recipes/detail`). `steps` is
 *  empty and `definitionAvailable` false for a bundled or YAML recipe, whose
 *  per-step text Crew does not read. */
export interface KasRecipeDetail {
  recipe: KasRecipe
  steps: KasRecipeStep[]
  definitionAvailable: boolean
}

/** One realised iteration of a `repeat` node: the loop body's nodes AS THAT
 *  iteration ran them (own status / sessionId / path). `iteration` is 0-based. */
export interface KasRunIteration {
  iteration: number
  path: string
  status: 'pending' | 'running' | 'paused' | 'completed' | 'failed' | 'aborted' | 'skipped' | string
  children: KasRunNode[]
}

/** One node of a run's step tree, with its live state (`mirror._node_payload`). */
export interface KasRunNode {
  id: string
  /** The node's root-relative runtime path, e.g. `loop/iter-1/code`. Unique per
   *  instance, so a loop body's step has a distinct path in each iteration. */
  path: string
  type: string
  agent: string | null
  label: string
  maxIterations: number | null
  status: 'pending' | 'running' | 'paused' | 'completed' | 'failed' | 'aborted' | 'skipped' | string
  sessionId: string | null
  iteration: number | null
  reason: string | null
  /** Step nodes: the model / effort KAS resolved for the step, when known. */
  model?: string | null
  effort?: string | null
  children: KasRunNode[]
  /** `repeat` nodes only: one entry per iteration KAS has reached. `children`
   *  stays the definition (latest-instance state); when this is non-empty the
   *  surface renders these instead. */
  iterations?: KasRunIteration[]
  /** An included workflow, expanded in place: the group of steps it brought in.
   *  `recipe` is the included workflow's name, `path` its file when known. */
  include?: { recipe: string; path?: string | null } | null
  /** `repeat` nodes: iterations run so far against the budget. */
  loop?: { done: number; max: number } | null
  /** Which per-step controls KAS allows on this node right now. */
  actions?: KasNodeAction[]
  // ── Payload fields from mirror.py, all optional so an older gateway omits them. ──
  /** KAS is waiting out a transient-error backoff on this step
   *  (`node_paused kind:'retry-wait'`). The step is STILL `running`; its only
   *  action is `cancel`. `reason` is KAS's English backoff text. Gap 1. */
  retryWait?: { reason: string; since: string } | null
  /** ISO-8601 UTC, per instance inside loops. `endedAt` is null while
   *  running/paused. Duration = `endedAt - startedAt`. Gap 23. */
  startedAt?: string | null
  endedAt?: string | null
  /** The last `watch_poll` result on a `watch` node; null on other types and
   *  before the first poll. Gap 23. */
  watchOutcome?: { outcome: 'idle' | 'new-activity' | 'terminal-state'; at: string | null } | null
  /** This instance has an open wait in `attention`: a tool/plan approval, or a
   *  question/reply. Drives "Needs approval" / "Waiting for you". Gap 23. */
  waitingFor?: 'approval' | 'input' | null
  /** Top-level not-yet-started node the queued plan (`queuedSteps`) will replace
   *  ("Will be replaced"). False below the top level and when nothing is queued. Gap 11. */
  replacementPending?: boolean
}

/** Per-node controls the backend derives from KAS's own rules. */
export type KasNodeAction = 'grant' | 'complete' | 'fail' | 'cancel' | 'reply'

/** Run-level controls the backend derives from KAS's own rules. */
export type KasRunCapability = 'pause' | 'resume' | 'cancel' | 'delete' | 'retry' | 'restart'

/** Why a run is parked, when it is. */
export type KasPauseReason =
  | 'loop_exhausted'
  | 'awaiting_input'
  | 'plan_approval'
  | 'transient_error'
  | 'retries_exhausted'
  | 'user'
  | 'unknown'

export interface KasRunPause {
  reason: KasPauseReason | string
  nodeId?: string | null
  path?: string | null
  message?: string | null
  iterations?: { done: number; max: number } | null
}

/** One question a step asked the person through its question tool. */
export interface KasQuestionItem {
  id: string
  prompt: string
  options?: { label: string; description?: string | null }[]
  multiSelect?: boolean
  freeText?: boolean
}

/** A step's open question-tool ask (`/state` `questions`). */
export interface KasQuestion {
  key: string
  workflowId: string
  nodeId: string
  path?: string | null
  slot?: string | null
  title?: string | null
  questions: KasQuestionItem[]
}

/** An answer per question id: one option label / free text, or several labels. */
export type KasQuestionAnswers = Record<string, string | string[]>

export type KasRunStatus = 'running' | 'paused' | 'completed' | 'failed' | 'aborted'
export type KasRunDisplay = 'running' | 'pausing' | 'paused' | 'completed' | 'failed' | 'stopped'

/** The controls a run offers, decided server-side (`wire.run_controls`). */
export interface KasRunControls {
  display: KasRunDisplay
  primary: 'open' | 'pause' | 'resume' | null
  canStop: boolean
  canRetry: boolean
  canDelete: boolean
  needsAttention: boolean
  /** Why it needs you: a step tool/plan approval, a step question or reply
   *  wait, or a park nobody asked for. `null` iff `needsAttention` is false.
   *  Optional for an older gateway. */
  attentionKind?: 'approval' | 'input' | 'parked' | null
}

export interface KasRunRow {
  workflowId: string
  name: string | null
  workflowName: string | null
  status: KasRunStatus
  parentSessionId: string | null
  createdAt: string | null
  startedAt: string | null
  endedAt: string | null
  updatedAt: string | null
  pausePending: boolean
  pauseReason: string | null
  completedSteps: number | null
  totalSteps: number | null
  activeSteps: { nodeId: string; label: string; sessionId: string | null; path?: string }[]
  attention: { kind: string; nodeId: string; sessionId: string | null; nodePath: string[]; since: string | null; toolCallId: string | null }[]
  blocking: { nodeId: string; label: string; kind: string; sessionId: string | null; toolCallId: string | null } | null
  controls: KasRunControls
  nodes: KasRunNode[]
  queuedSteps: KasNodeDescriptor[]
  revision: number
  /** The dashboard chat this run reports into, when the router knows it. */
  slotKey?: string | null
  /** A live Kiro Agent process drives the run. False after a KAS restart until reconnected. */
  driven?: boolean
  /** Why the run is parked, structured; null when it is not. */
  pause?: KasRunPause | null
  /** Which run controls KAS allows now (`retry`, `restart`, …). */
  actions?: KasRunCapability[]
  /** Kiro Agent's OWN step counts (G1), computed by KAS at send time over step
   *  and watch nodes. Absent/null for an older bundled kiro-cli, a finished run,
   *  or before any event arrived — then fall back to `completedSteps` /
   *  `totalSteps`. When `stepsTotal` is non-null the four below describe ONE
   *  moment. A terminal run clears all four to null. */
  stepsCompleted?: number | null
  stepsTotal?: number | null
  /** The step/watch node the run is on (running, else paused, else failed, else
   *  pending); null when no node qualifies. A loop body's bare node id — look it
   *  up in `nodes` recursively, not by path. */
  currentNodeId?: string | null
  /** True while a watch runs and no step runs or waits; `currentNodeId` is then
   *  that watch. Never null when `stepsTotal` is known. */
  waitingOnWatch?: boolean | null
  // ── Optional for an older gateway. ──
  /** The step tree is loaded (`nodes` came from a plan). False means "not loaded
   *  yet", so an empty `activeSteps` says nothing. Gap 23. */
  stepsKnown?: boolean
  /** How many queued plan replacements KAS applied to this run (0 = original). Gap 11. */
  planRevision?: number
  /** The latest resolved plan change. `reason` is KAS's text for `rejected`
   *  (redacted). `at` is ISO-8601 UTC. Gap 11. */
  lastPlanUpdate?: { outcome: 'applied' | 'rejected' | 'dropped'; reason: string | null; at: string } | null
}

export interface KasApprovalOption {
  optionId: string
  kind: 'allow_once' | 'allow_always' | 'reject_once' | 'reject_always'
  name: string
}

/** One step permission request waiting for a person. `plan` is KAS's
 *  `WorkflowPlanApproval` (`kind`, `steps`, …) on a plan approval. */
export interface KasApproval {
  key: string
  workflowId: string
  nodeId: string
  title: string
  command: string | null
  options: KasApprovalOption[]
  /** KAS's plan payload; on a plan approval it may carry `text` and the
   *  structured plan as `nodes` (run-node shaped), drawn as a step tree. */
  plan: (Record<string, unknown> & { text?: string; nodes?: unknown }) | null
  /** This approval is KAS's workflow-plan handoff, never auto-approved (not even
   *  in Trust). The card shows `planText` and titles itself as a plan. */
  isPlan: boolean
  /** The plan as KAS's plain text (one line per node, two-space indent per
   *  level; redacted, capped). Null unless `isPlan`. Shown verbatim — not parsed. */
  planText: string | null
  createdAt: number
}

export interface KasRunsSnapshot {
  runs: KasRunRow[]
  approvals: KasApproval[]
  /** Open question-tool asks from steps (`/state` only). */
  questions?: KasQuestion[]
  revision: number
  /** The recipe list's own poll key (`/state` only): bumped on every KAS
   *  `recipes_changed` push, so a client re-reads recipes when the set changes. */
  recipesRevision?: number
}

/** One entry of a step session's transcript, oldest first. */
export type KasStepEntry =
  | { kind: 'user'; id: string; ts: string | null; text: string }
  | { kind: 'assistant'; id: string; ts: string | null; text: string; reasoning: boolean }
  | {
      kind: 'tool'
      id: string
      ts: string | null
      toolCallId: string
      name: string
      title: string
      toolKind: string | null
      status: string
      input: string | null
      output: string | null
    }

/** A workflow step's own chat session (`GET …/steps/{nodeId}/transcript`).
 *  404 `step_unknown` means the step has no session yet. */
export interface KasStepTranscript {
  workflowId: string
  nodeId: string
  sessionId: string
  title: string | null
  agent: string | null
  model: string | null
  /** 'running' | 'idle' | … */
  status: string
  credits: number | null
  startedAt: string | null
  updatedAt: string | null
  /** At most 400; `truncated` when older ones were dropped. */
  entries: KasStepEntry[]
  truncated: boolean
}

export type KasRunAction = 'pause' | 'resume' | 'cancel' | 'delete'

export interface KasLaunchBody {
  slot: string
  source: string
  inputs?: Record<string, string>
  runLabel?: string
  name?: string
  /** Model id for the run's steps; absent = each step's own. */
  model?: string
  /** Reasoning effort for the run's steps; absent = each step's own. */
  effort?: string
}

/** The effort levels KAS accepts, lowest first. */
export const KAS_EFFORT_LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'] as const

export function createWorkflowsEndpoints({ get, post, patch, del, j, jAccepted }: ClientTransport) {
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
    /** Which engine owns workflows here: `kas` when the backend is Kiro Agent. */
    workflowEngine: () =>
      get('/api/workflows/engine').then(j) as Promise<WorkflowEngineInfo>,
    /** The recipes KAS can launch in project `cwd` (an absolute directory). */
    kasRecipes: (cwd: string) =>
      get('/api/workflows/kas/recipes?cwd=' + encodeURIComponent(cwd)).then(j) as Promise<{ recipes: KasRecipe[] }>,
    /** One recipe with its steps' prompt text and agent (`cwd` scopes the lookup;
     *  `source` must be one KAS currently lists). A bundled/YAML recipe answers
     *  `definitionAvailable: false` with no steps. */
    kasRecipeDetail: (cwd: string, source: string) =>
      get(
        '/api/workflows/kas/recipes/detail?cwd=' +
          encodeURIComponent(cwd) +
          '&source=' +
          encodeURIComponent(source),
      ).then(j) as Promise<KasRecipeDetail>,
    /** Create a `<name>.workflow.json` scaffold under the project (`workspace`)
     *  or the user's home (`global`). 409 `recipe_exists` if one already exists. */
    kasCreateRecipe: (cwd: string, name: string, scope: 'workspace' | 'global') =>
      post('/api/workflows/kas/recipes', { cwd, name, scope }).then(j) as Promise<{ name: string; source: string }>,
    /** Delete a recipe's file. `source` must be a listed, non-read-only recipe
     *  for `cwd`; built-in recipes cannot be deleted. */
    kasDeleteRecipe: (cwd: string, source: string) =>
      del(
        '/api/workflows/kas/recipes?cwd=' +
          encodeURIComponent(cwd) +
          '&source=' +
          encodeURIComponent(source),
      ).then(j) as Promise<{ deleted: boolean }>,
    /** Every run in project `cwd`, read from disk. Also folds them into `kasState`. */
    kasRuns: (cwd: string) =>
      get('/api/workflows/kas/runs?cwd=' + encodeURIComponent(cwd)).then(j) as Promise<KasRunsSnapshot>,
    /** Cheap poll: the runs and approvals the router already holds, plus a revision. */
    kasState: () =>
      get('/api/workflows/kas/state').then(j) as Promise<KasRunsSnapshot>,
    kasRun: (workflowId: string) =>
      get('/api/workflows/kas/runs/' + encodeURIComponent(workflowId)).then(j) as Promise<KasRunRow & { approvals: KasApproval[] }>,
    /** One step's own chat session. 404 `step_unknown` until the step has
     *  started. `path` (the node's runtime path) picks one iteration of a loop's
     *  step; omit it for a step that runs once. */
    kasStepTranscript: (workflowId: string, nodeId: string, path?: string | null) =>
      get(
        '/api/workflows/kas/runs/' +
          encodeURIComponent(workflowId) +
          '/steps/' +
          encodeURIComponent(nodeId) +
          '/transcript' +
          (path ? '?path=' + encodeURIComponent(path) : ''),
      ).then(j) as Promise<KasStepTranscript>,
    /** Launch a run in an existing chat whose project is set. 202: the chat's turn starts it. */
    kasLaunchRun: (body: KasLaunchBody) =>
      post('/api/workflows/kas/runs', body).then(jAccepted) as Promise<{ slot: string; queued: boolean }>,
    kasRunControl: (workflowId: string, action: KasRunAction, reason?: string) =>
      post(
        '/api/workflows/kas/runs/' + encodeURIComponent(workflowId) + '/' + action,
        reason ? { reason } : {},
      ).then(j) as Promise<{ workflowId: string; row: KasRunRow | null }>,
    /** Re-attach a chat's unfinished runs after a Kiro Agent restart. */
    kasReconnectSlot: (slot: string) =>
      post('/api/workflows/kas/slots/' + encodeURIComponent(slot) + '/reconnect', {}).then(jAccepted) as Promise<unknown>,
    /** One chat's runs, read back (read-only) from the run ledger after a gateway restart. */
    kasChatRuns: (slot: string) =>
      get('/api/workflows/kas/slots/' + encodeURIComponent(slot) + '/runs').then(j) as Promise<KasRunsSnapshot>,
    kasAnswerApproval: (key: string, optionId: string) =>
      post('/api/workflows/kas/approvals/' + encodeURIComponent(key), { optionId }).then(j) as Promise<{ answered: boolean }>,
    /** Send the person's reply to a step parked for their next message. `path`
     *  names the loop iteration when the step is inside one. 409
     *  `kas_workflow_error` when the step is not waiting; 404 `step_unknown`. */
    kasReplyToStep: (workflowId: string, nodeId: string, text: string, path?: string | null) =>
      post(
        '/api/workflows/kas/runs/' +
          encodeURIComponent(workflowId) +
          '/steps/' +
          encodeURIComponent(nodeId) +
          '/reply',
        path ? { text, path } : { text },
      ).then(j) as Promise<{ sent: boolean }>,
    /** Retry every failed step, or one (`nodeId`, plus `path` inside a loop or include). */
    kasRetryRun: (workflowId: string, target?: { nodeId: string; path?: string | null }) =>
      post(
        '/api/workflows/kas/runs/' + encodeURIComponent(workflowId) + '/retry',
        target ? { nodeId: target.nodeId, ...(target.path ? { path: target.path } : {}) } : {},
      ).then(j) as Promise<unknown>,
    /** Run a finished run again from scratch. */
    kasRestartRun: (workflowId: string) =>
      post('/api/workflows/kas/runs/' + encodeURIComponent(workflowId) + '/restart', {}).then(j) as Promise<unknown>,
    /** Per-step writes: grant an exhausted loop more passes, mark a paused step
     *  done/failed (or end a loop and continue), stop one running step. */
    kasStepAction: (
      workflowId: string,
      nodeId: string,
      verb: 'grant' | 'complete' | 'cancel',
      body: { path?: string | null; iterations?: number; status?: 'completed' | 'failed'; reason?: string },
    ) => {
      const { path, ...rest } = body
      return post(
        '/api/workflows/kas/runs/' + encodeURIComponent(workflowId) + '/steps/' + encodeURIComponent(nodeId) + '/' + verb,
        path ? { ...rest, path } : rest,
      ).then(j) as Promise<unknown>
    },
    /** Answer (or dismiss) a step's question-tool ask. */
    kasAnswerQuestion: (key: string, body: { answers: KasQuestionAnswers } | { dismiss: true }) =>
      post('/api/workflows/kas/questions/' + encodeURIComponent(key), body).then(j) as Promise<unknown>,
  }

  return { engine }
}
