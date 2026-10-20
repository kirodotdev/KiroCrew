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
 * cookie — NOT the app-sdk data hooks (those need the SDK's scoped-API layer,
 * which a builtin page mounts for itself via `AppScopedApiProvider`; app identity
 * is published for every builtin page by BuiltinAppRoute).
 *
 * Backend contract: see kiro_crew/apps/builtins/workflows/server.py and the run
 * event schema in docs/system-specs/modules/workflows.md.
 */
import { useCallback, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Workflow as WorkflowIcon, Play, FileCode, ListTree } from 'lucide-react'
import { PageHeader } from '../../components/ui'
import SegmentedControl from '../../components/SegmentedControl'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { AppDisabledNotice, AppsButton } from '../../components/AppDisabledPage'
import { isAppNotEnabledError, toApiError, type ApiError } from '../../api/apiError'
import { retryPolicy } from '../../api/queryClient'
import { attachReport, parseErrorCode, recordError } from '../../utils/errorReport'
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
// /apps/workflows/api/* to the app's HTTP server). A refusal throws the shared
// ApiError, which keeps the gateway's body so the page can read its `code`.
const API_BASE = '/apps/workflows/api'

/** The ApiError for a failed call, with its report journaled and pinned to it as the dashboard
 *  client does (`api/client.ts`), so a notice that hands the failure to the agent carries the
 *  endpoint, status and code rather than only the sentence on screen. */
async function failure(r: Response, path: string): Promise<ApiError> {
  const error = await toApiError(r)
  return attachReport(error, recordError({
    source: 'api',
    message: error.message,
    status: error.status,
    code: parseErrorCode(error.body),
    endpoint: `${API_BASE}${path}`,
    detail: error.body,
  }))
}

async function apiGet<T>(path: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, { credentials: 'same-origin' })
  if (!r.ok) throw await failure(r, path)
  return r.json() as Promise<T>
}

async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw await failure(r, path)
  return r.json() as Promise<T>
}

interface RunResponse {
  ok: boolean
  result: unknown
  error: string | null
  error_code?: string | null
  events: WfEvent[]
}

interface ValidateResponse {
  ok: boolean
  errors: string[]
  /** The script's own `META` dict, echoed back unparsed. Arbitrary author-supplied
   *  JSON, so it stays opaque rather than claiming a shape nothing checks. */
  meta: Record<string, unknown> | null
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

/** The example-scripts query: read as the page opens, and asked again by `reaskHeldRefusal`. */
const EXAMPLES_KEY = ['workflow-examples']

/** Stable stand-in for "no run yet". A fresh `[]` per render would give every
 *  hook keyed on the event list a new identity on every render. */
const NO_EVENTS: WfEvent[] = []

export default function WorkflowsPage() {
  const [view, setView] = useState<WorkflowsView>('author')
  const [source, setSource] = useState<string>(STARTER)
  // Set when the author first focuses the editor: from then on the editor is where a script is
  // being started, so a refusal no longer replaces it.
  const [engaged, setEngaged] = useState(false)

  // Shipped example workflows for the author to start from (cached, deduped). The disabled-app
  // refusal is not retried: it is the app's state rather than a blip, and a retry only keeps the
  // editor on screen for the retry delay before the page gives way.
  const { data: examples = [], error: examplesError } = useQuery({
    queryKey: EXAMPLES_KEY,
    queryFn: () => apiGet<ExampleScript[]>('/examples'),
    retry: (count, error) => !isAppNotEnabledError(error) && retryPolicy(count, error),
  })

  // A Validate or Run the backend answered proves the app is on again, so an example refusal
  // still held from before is stale: ask for the examples again, which also takes its notice
  // down. Once per answer, and read from the cache when the answer lands rather than from the
  // render that started the call.
  const queryClient = useQueryClient()
  const reaskHeldRefusal = () => {
    if (isAppNotEnabledError(queryClient.getQueryState(EXAMPLES_KEY)?.error)) {
      void queryClient.invalidateQueries({ queryKey: EXAMPLES_KEY })
    }
  }

  // Validate + run are imperative actions → useMutation (loading/error/data
  // managed by react-query; no manual running/error/run state).
  const validateMutation = useMutation({
    mutationFn: (src: string) => apiPost<ValidateResponse>('/validate', { source: src }),
    onSuccess: () => {
      // An answered Validate takes down a Run refusal held from before too, which only Run's
      // next click would otherwise clear. Not in `reaskHeldRefusal`, which Run's own onSuccess
      // shares: that one is built from a render that still holds the refusal this Run replaces,
      // and resetting there would drop the run that just answered.
      if (isAppNotEnabledError(runMutation.error)) runMutation.reset()
      reaskHeldRefusal()
    },
  })
  const runMutation = useMutation({
    mutationFn: (src: string) =>
      apiPost<RunResponse>('/run', { source: src, run_id: `wf_ui_${Date.now()}` }),
    onSuccess: reaskHeldRefusal,
  })

  const validation = validateMutation.data ?? null
  const run = runMutation.data ?? null
  // Cover BOTH phases of doRun (validate → run): the Run button awaits
  // validateMutation before runMutation starts, so gating only on
  // runMutation.isPending leaves a window where a fast double-click fires two
  // concurrent validate+run sequences (duplicate runs). Include the pending
  // validation so the button is disabled for the whole gesture.
  const running = runMutation.isPending || validateMutation.isPending
  const errText = (e: unknown) => (e ? (e instanceof Error ? e.message : String(e)) : null)
  const error = errText(runMutation.error)
  // The validator's TRANSPORT failure (non-2xx, network), as opposed to its
  // verdict: `validation.ok === false` is a successful request that said no.
  const validateError = errText(validateMutation.error)

  // mutateAsync rejects on a transport failure; the rejection is already held
  // in `validateMutation.error` for the notice, so the callers swallow it here
  // instead of surfacing an unhandled promise.
  const validate = useCallback(
    () => validateMutation.mutateAsync(source).catch(() => null),
    [validateMutation, source],
  )

  const doRun = useCallback(async () => {
    const v = await validateMutation.mutateAsync(source).catch(() => null)
    if (!v || !v.ok) return // E1: invalid script (or an unreachable validator) blocks the run
    runMutation.reset()
    runMutation.mutate(source)
  }, [validateMutation, runMutation, source])

  // The run view is driven by the events the synchronous /run returned.
  // Phase-tree folding happens inside <WorkflowRunTree>; here we only need the
  // budget badge for the header.
  const events = run?.events ?? NO_EVENTS
  const budget = useMemo(() => latestBudget(events), [events])

  // The gateway refuses every call to this app's backend while the app is off, and any of the
  // three can be the first to meet that refusal. The example fetch is asked as the page opens:
  // until the author has focused the editor or changed the starter script nothing can be lost,
  // so the Author view gives way to the disabled-app notice. Only that view does. The page keeps
  // its header and its Author/Runs switch, because the Runs view reads the gateway's run registry
  // (`/api/workflows/*`) rather than the app's proxied API and works with the app off, and it
  // keeps the fields of its Save form in its own state, so a refusal that lands while it is open
  // must not unmount it. Validate and Run are answered over a script the author may have written,
  // and it lives only in `source`, while the notice's one button unmounts this page. So their
  // refusal is a notice beside the editor, which stays, and so is an example refusal that lands
  // once the author is in the editor.
  const examplesOff = isAppNotEnabledError(examplesError)
  const validateOff = isAppNotEnabledError(validateMutation.error)
  const runOff = isAppNotEnabledError(runMutation.error)
  const authorOff = examplesOff && !engaged && source === STARTER
  // No hand-off: the workflow `source` in the editor is unsaved local state.
  const appOffNotice = (
    <ErrorNotice
      title={i18nT('components.appHost.this_app_is_disabled')}
      message={i18nT('components.appHost.enable_this_app_from_apps_to_use_it')}
      footer={<AppsButton />}
    />
  )

  return (
    <div>
      <PageHeader title={i18nT('apps.workflows.workflowsPage.workflows')} subtitle={i18nT('apps.workflows.workflowsPage.author_run_and_watch_dynamic_workflows')} />
      <div className="px-4 md:px-6 pb-4">
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

      {view === 'author' && authorOff && <AppDisabledNotice refusal={examplesError} />}

      {view === 'author' && !authorOff && (
      <div className="px-4 md:px-6 pb-8 grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* ----- Author ----- */}
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2 text-[13px] text-muted">
            <FileCode size={14} /> {i18nT('apps.workflows.workflowsPage.workflow_script_python')}
          </div>
          <textarea
            value={source}
            onChange={e => setSource(e.target.value)}
            onFocus={() => setEngaged(true)}
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
          {/* A failed example fetch that is not the disabled-app refusal: without this the
              "Load example…" control is simply absent. Titled for what failed, since the
              editor beside it still works. No hand-off: the workflow `source` in the editor
              is unsaved local state. */}
          <ErrorNotice
            title={i18nT('apps.workflows.workflowsPage.couldn_t_load_the_example_workflows')}
            message={examplesOff ? null : errText(examplesError)}
            messagePlacement="below"
          />
          {/* The validator's rejection, one line per problem; below it, the
              validator being unreachable at all, or the app being switched off.
              No hand-off: the workflow `source` in the editor is unsaved local state. */}
          <ErrorNotice
            title={i18nT('apps.workflows.workflowsPage.invalid_fix_before_running')}
            message={validation && !validation.ok ? validation.errors.join('\n') : null}
            messageClassName="font-mono whitespace-pre-line"
          />
          {(validateOff || examplesOff) && appOffNotice}
          <ErrorNotice
            title={i18nT('apps.workflows.workflowsPage.couldn_t_validate_the_script')}
            message={validateOff ? null : validateError}
          />
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

          {/* No hand-off: the workflow `source` in the editor is unsaved local state. */}
          {runOff && appOffNotice}
          <ErrorNotice
            title={i18nT('apps.workflows.workflowsPage.couldn_t_start_the_run')}
            message={runOff ? null : error}
          />

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
              errorCode={run?.error_code}
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
