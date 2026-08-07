/**
 * WorkflowsPage — the dashboard "Workflows" tab (M3, gates E1–E4).
 *
 * Author a dynamic-workflow Python script, validate it, run it, and see the
 * result: a phase tree with per-agent status, narrator log lines, a budget
 * gauge, and the final result. Talks to the workflows builtin-app backend over
 * its proxied API (`/apps/workflows/api/*`). The synchronous `/run` returns the
 * complete run event stream, which drives the run view.
 *
 * This is a BUILTIN dashboard page (rendered by BuiltinAppRoute inside the main
 * React tree), so it uses same-origin `fetch` with the dashboard's session
 * cookie — NOT the app-sdk hooks (those require <AppApiProvider>, which only
 * wraps standalone/installed apps via AppHost).
 *
 * Backend contract: see kiro_crew/apps/builtins/workflows/server.py and the run
 * event schema in docs/system-specs/modules/workflows.md.
 */
import { useCallback, useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Workflow as WorkflowIcon, Play, FileCode, ListTree } from 'lucide-react'
import { PageHeader } from '../../components/ui'
import SegmentedControl from '../../components/SegmentedControl'
import SimpleSelect from '../../components/SimpleSelect'
import WorkflowsRuns from './WorkflowsRuns'
import WorkflowRunTree from './WorkflowRunTree'
import { groupByPhase, latestBudget, type WfEvent, type AgentRow, type PhaseGroup } from './runModel'

import { i18nT } from '../../i18n/t'
// Re-export the pure event-stream helpers and types from the shared runModel
// so the existing test imports (`from '../apps/workflows/WorkflowsPage'`) keep
// working without touching test code.
export { groupByPhase, latestBudget }
export type { WfEvent, AgentRow, PhaseGroup }

// Proxied base for the workflows builtin-app backend (KiroCrew reverse-proxies
// /apps/workflows/api/* to the app's HTTP server).
const API_BASE = '/apps/workflows/api'

async function apiGet<T>(path: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, { credentials: 'same-origin' })
  if (!r.ok) throw new Error(`GET ${path} → ${r.status}`)
  return r.json() as Promise<T>
}

async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`POST ${path} → ${r.status}`)
  return r.json() as Promise<T>
}

interface RunResponse {
  ok: boolean
  result: unknown
  error: string | null
  events: WfEvent[]
}

interface ValidateResponse {
  ok: boolean
  errors: string[]
  meta: Record<string, any> | null
}

interface ExampleScript {
  name: string
  description: string
  source: string
}

const STARTER = `META = {"name": "hello", "description": "a tiny workflow"}

async def workflow(ctx):
    ctx.phase("Work")
    ctx.log("starting")
    out = await ctx.agent("say hello")
    return {"said": out}
`

type WorkflowsView = 'author' | 'runs'

export default function WorkflowsPage() {
  const [view, setView] = useState<WorkflowsView>('author')
  const [source, setSource] = useState<string>(STARTER)

  // Shipped example workflows for the author to start from (cached, deduped).
  const { data: examples = [] } = useQuery({
    queryKey: ['workflow-examples'],
    queryFn: () => apiGet<ExampleScript[]>('/examples'),
  })

  // Validate + run are imperative actions → useMutation (loading/error/data
  // managed by react-query; no manual running/error/run state).
  const validateMutation = useMutation({
    mutationFn: (src: string) => apiPost<ValidateResponse>('/validate', { source: src }),
  })
  const runMutation = useMutation({
    mutationFn: (src: string) =>
      apiPost<RunResponse>('/run', { source: src, run_id: `wf_ui_${Date.now()}` }),
  })

  const validation = validateMutation.data ?? null
  const run = runMutation.data ?? null
  // Cover BOTH phases of doRun (validate → run): the Run button awaits
  // validateMutation before runMutation starts, so gating only on
  // runMutation.isPending leaves a window where a fast double-click fires two
  // concurrent validate+run sequences (duplicate runs). Include the pending
  // validation so the button is disabled for the whole gesture.
  const running = runMutation.isPending || validateMutation.isPending
  const error = runMutation.error
    ? runMutation.error instanceof Error
      ? runMutation.error.message
      : String(runMutation.error)
    : null

  const validate = useCallback(() => validateMutation.mutateAsync(source), [validateMutation, source])

  const doRun = useCallback(async () => {
    const v = await validateMutation.mutateAsync(source)
    if (!v.ok) return // E1: invalid script blocks the run
    runMutation.reset()
    runMutation.mutate(source)
  }, [validateMutation, runMutation, source])

  // The run view is driven by the events the synchronous /run returned.
  // Phase-tree folding happens inside <WorkflowRunTree>; here we only need the
  // budget badge for the header.
  const events = run?.events ?? []
  const budget = useMemo(() => latestBudget(events), [events])

  return (
    <div>
      <PageHeader title={i18nT('apps.workflows.workflowsPage.workflows')} subtitle={i18nT('apps.workflows.workflowsPage.author_run_and_watch_dynamic_workflows')} />
      <div className="px-6 pb-4">
        <SegmentedControl<WorkflowsView>
          segments={[
            { key: 'author', label: 'Author', icon: <FileCode size={14} /> },
            { key: 'runs', label: 'Runs', icon: <ListTree size={14} /> },
          ]}
          value={view}
          onChange={setView}
          layoutId="workflows-view"
        />
      </div>

      {view === 'runs' && <WorkflowsRuns />}

      {view === 'author' && (
      <div className="px-6 pb-8 grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* ----- Author ----- */}
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2 text-[13px] text-muted">
            <FileCode size={14} /> {i18nT('apps.workflows.workflowsPage.workflow_script_python')}
          </div>
          <textarea
            value={source}
            onChange={e => setSource(e.target.value)}
            spellCheck={false}
            className="font-mono text-[12px] leading-relaxed h-72 p-3 rounded border border-border bg-card resize-y"
            aria-label={i18nT('apps.workflows.workflowsPage.workflow_source')}
          />
          <div className="flex items-center gap-2">
            <button
              onClick={doRun}
              disabled={running}
              className="flex items-center gap-1.5 px-3 py-1.5 text-[13px] font-medium rounded bg-accent text-accent-fg disabled:opacity-50"
            >
              <Play size={14} /> {running ? i18nT('apps.workflows.workflowsPage.running') : i18nT('apps.workflows.workflowsPage.run')}
            </button>
            <button
              onClick={validate}
              className="px-3 py-1.5 text-[13px] rounded border border-border"
            >
              {i18nT('apps.workflows.workflowsPage.validate')}
            </button>
            {examples.length > 0 && (
              /* A one-shot ACTION list, not a persisted value. Nothing on this
                 page records "which example is loaded" — picking one copies its
                 source into the editor, which the operator then edits freely, so
                 a trigger showing the last pick would be stale the moment it was
                 set. Holding `value` at '' keeps the trigger on its command label
                 and, because the controlled value never becomes the picked item,
                 lets the SAME example be re-loaded after a bad edit — the old
                 native select (like any value-bound select) refused to re-fire
                 for the option already selected. */
              <SimpleSelect
                options={examples.map(ex => ex.name)}
                value=""
                onChange={name => {
                  const ex = examples.find(x => x.name === name)
                  if (ex) setSource(ex.source)
                }}
                triggerFallback={i18nT('apps.workflows.workflowsPage.load_example')}
                aria-label={i18nT('apps.workflows.workflowsPage.load_example')}
                style={{ marginLeft: 'auto' }}
              />
            )}
          </div>
          {validation && !validation.ok && (
            <div className="text-[12px] text-red-500 border border-red-500/30 rounded p-2">
              <div className="font-medium mb-1">{i18nT('apps.workflows.workflowsPage.invalid_fix_before_running')}</div>
              <ul className="list-disc pl-4">
                {validation.errors.map((e, i) => <li key={i}>{e}</li>)}
              </ul>
            </div>
          )}
        </div>

        {/* ----- Live run view ----- */}
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2 text-[13px] text-muted">
            <WorkflowIcon size={14} /> {i18nT('apps.workflows.workflowsPage.run')}
            {budget && (
              <span className="ml-auto text-[11px] tabular-nums">
                {i18nT('apps.workflows.workflowsPage.budget')} {budget.spent}{budget.total != null ? ` / ${budget.total}` : ''}
              </span>
            )}
          </div>

          {error && (
            <div className="text-[12px] text-red-500 border border-red-500/30 rounded p-2">
              {i18nT('apps.workflows.workflowsPage.request_failed')} {error}
            </div>
          )}

          {events.length === 0 && !error && (
            <div className="text-[12px] text-muted border border-dashed border-border rounded p-4">
              {i18nT('apps.workflows.workflowsPage.run_a_workflow_to_see_its_phases_agents_and_resu')}
            </div>
          )}

          {/* Phase tree + narrator logs + result panel — shared with the chat
              surfaces and the Runs view (single source of truth, no duplication). */}
          {events.length > 0 && (
            <WorkflowRunTree
              events={events}
              status={run ? (run.ok ? 'finished' : 'failed') : 'running'}
              result={run?.result}
              error={run?.error}
            />
          )}
        </div>
      </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Event-stream → view-model helpers live in ./runModel; re-exported above so
// existing imports (and the WorkflowsPage.test unit tests) keep working.
// ---------------------------------------------------------------------------
