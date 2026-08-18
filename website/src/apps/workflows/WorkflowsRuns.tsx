/**
 * WorkflowsRuns — the "Runs" view of the Workflows tab (M6.5).
 *
 * Lists all background workflow runs (newest-first), each clickable to load its
 * full event stream. The selected run is rendered with the same phase-tree +
 * budget helpers the live-run view uses (reused from WorkflowsPage), plus a
 * Cancel button for runs still in flight.
 *
 * Unlike the author/validate/run flow — which is proxied through the workflows
 * builtin-app backend at `/apps/workflows/api/*` — the run registry is served by
 * the gateway CORE API at `/api/workflows/*`. We poll the list every ~2s while
 * mounted (no WebSocket needed).
 *
 * Backend contract (frozen):
 *   GET  /api/workflows/runs            -> { runs: RunSummary[] }   (newest first)
 *   GET  /api/workflows/runs/{id}       -> RunDetail                (with events[])
 *   POST /api/workflows/runs/{id}/cancel -> { run_id, cancelled }
 */
import { useCallback, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Workflow as WorkflowIcon, CheckCircle2, XCircle, Loader2, Ban, ListTree } from 'lucide-react'
import { Badge } from '../../components/ui'
// Import shared view-model helpers from runModel directly (NOT from WorkflowsPage)
// so this module and WorkflowsPage do not form an import cycle.
import { latestBudget } from './runModel'
import WorkflowRunTree from './WorkflowRunTree'

import { i18nT } from '../../i18n/t'
// Gateway CORE API base — distinct from the builtin-app proxy used by the
// author/validate/run flow (`/apps/workflows/api/*`).
const CORE_API_BASE = '/api/workflows'

const POLL_MS = 2000

async function coreGet<T>(path: string): Promise<T> {
  const r = await fetch(`${CORE_API_BASE}${path}`, { credentials: 'same-origin' })
  if (!r.ok) throw new Error(`GET ${path} → ${r.status}`)
  return r.json() as Promise<T>
}

async function corePost<T>(path: string): Promise<T> {
  const r = await fetch(`${CORE_API_BASE}${path}`, {
    method: 'POST',
    credentials: 'same-origin',
  })
  if (!r.ok) throw new Error(`POST ${path} → ${r.status}`)
  return r.json() as Promise<T>
}

// ---------------------------------------------------------------------------
// Contract types (frozen — see module docstring)
// ---------------------------------------------------------------------------

export type RunStatus = 'running' | 'finished' | 'failed' | 'cancelled'

export interface RunSummary {
  run_id: string
  name: string
  status: RunStatus
  result: unknown
  error: string | null
  author: string | null
  session_key: string | null
  event_count: number
}

interface RunEvent {
  run_id: string
  seq: number
  ts: string
  type: string
  data: Record<string, any>
}

interface RunDetail {
  run_id: string
  name: string
  status: RunStatus
  result: unknown
  error: string | null
  event_count: number
  events: RunEvent[]
}

interface RunsListResponse {
  runs: RunSummary[]
}

interface CancelResponse {
  run_id: string
  cancelled: boolean
}

// ---------------------------------------------------------------------------
// Pure view-model helpers (unit-tested in WorkflowsRuns.test)
// ---------------------------------------------------------------------------

export interface StatusBadgeSpec {
  /** Badge variant accepted by the shared <Badge> component. */
  variant: 'ok' | 'err' | 'warn' | 'aim'
  label: string
  /** Whether this run is still in flight (and thus cancellable). */
  active: boolean
}

/**
 * Map a run status to its badge presentation. Unknown/missing statuses fall
 * back to a neutral "warn" badge so the list never renders a blank cell.
 */
export function statusBadge(status: string | null | undefined): StatusBadgeSpec {
  switch (status) {
    case 'running':
      return { variant: 'aim', label: 'Running', active: true }
    case 'finished':
      return { variant: 'ok', label: 'Finished', active: false }
    case 'failed':
      return { variant: 'err', label: 'Failed', active: false }
    case 'cancelled':
      return { variant: 'warn', label: 'Cancelled', active: false }
    default:
      return { variant: 'warn', label: status ? String(status) : i18nT('apps.workflows.workflowsRuns.unknown'), active: false }
  }
}

/** True when a run can be cancelled (i.e. it is still running). */
export function isCancellable(status: string | null | undefined): boolean {
  return statusBadge(status).active
}

export interface RunRow {
  run_id: string
  /** Display name, falling back to the run id when no name is set. */
  name: string
  status: RunStatus
  badge: StatusBadgeSpec
  author: string | null
  /** Number of agent_started events seen for this run. */
  agentCount: number
  eventCount: number
  cancellable: boolean
}

/**
 * Reduce the raw run list into render-ready rows: resolve a display name,
 * derive the status badge, and count agents (best-effort from event_count when
 * a per-agent count is not separately provided by the contract).
 *
 * The contract gives us `event_count` but not a separate agent count, so we
 * surface `event_count` as the event tally and expose `agentCount` derived
 * from any `agent_count`/`agents` field the gateway may attach — defaulting to
 * 0 when absent. This keeps the pure helper resilient to the frozen shape.
 */
export function summarizeRuns(runs: RunSummary[] | null | undefined): RunRow[] {
  if (!runs) return []
  return runs.map(r => {
    const badge = statusBadge(r.status)
    // The frozen summary shape only guarantees event_count. Some gateways
    // additionally attach an agent tally; read it defensively without
    // assuming a shape the contract does not promise.
    const extra = r as RunSummary & { agent_count?: number; agents?: unknown[] }
    const agentCount =
      typeof extra.agent_count === 'number'
        ? extra.agent_count
        : Array.isArray(extra.agents)
          ? extra.agents.length
          : 0
    return {
      run_id: r.run_id,
      name: r.name && r.name.trim() ? r.name : r.run_id,
      status: r.status,
      badge,
      author: r.author ?? null,
      agentCount,
      eventCount: typeof r.event_count === 'number' ? r.event_count : 0,
      cancellable: badge.active,
    }
  })
}

/**
 * The contract says the list arrives newest-first. We keep that order but make
 * it explicit/idempotent: pin any still-running runs to the top (so live work
 * is always visible) while otherwise preserving the server's order. Pure and
 * non-mutating — returns a new array.
 */
export function orderRuns(rows: RunRow[]): RunRow[] {
  const running = rows.filter(r => r.cancellable)
  const rest = rows.filter(r => !r.cancellable)
  return [...running, ...rest]
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function WorkflowsRuns() {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const queryClient = useQueryClient()

  // Run list — react-query polling (dedup + caching + self-managed cleanup).
  const { data: runs = [], error: listErrorObj } = useQuery({
    queryKey: ['workflow-runs'],
    queryFn: () => coreGet<RunsListResponse>('/runs').then(r => r.runs ?? []),
    refetchInterval: POLL_MS,
  })
  const listError = listErrorObj instanceof Error ? listErrorObj.message : listErrorObj ? String(listErrorObj) : null

  // Selected-run detail — fetched + polled only while a run is selected.
  const { data: detail = null, error: detailErrorObj } = useQuery({
    queryKey: ['workflow-run-detail', selectedId],
    queryFn: () => coreGet<RunDetail>(`/runs/${encodeURIComponent(selectedId!)}`),
    enabled: !!selectedId,
    refetchInterval: POLL_MS,
  })

  // Cancel — mutation that invalidates both queries so the list + detail refresh.
  const cancelMutation = useMutation({
    mutationFn: (id: string) => corePost<CancelResponse>(`/runs/${encodeURIComponent(id)}/cancel`),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['workflow-runs'] })
      queryClient.invalidateQueries({ queryKey: ['workflow-run-detail'] })
    },
  })
  const cancelMutationError =
    cancelMutation.error instanceof Error ? cancelMutation.error.message : cancelMutation.error ? String(cancelMutation.error) : null
  const detailError =
    cancelMutationError ?? (detailErrorObj instanceof Error ? detailErrorObj.message : detailErrorObj ? String(detailErrorObj) : null)
  const cancellingId = cancelMutation.isPending ? (cancelMutation.variables ?? null) : null

  const select = useCallback((id: string) => setSelectedId(id), [])
  const cancelRun = useCallback((id: string) => cancelMutation.mutate(id), [cancelMutation])

  const rows = useMemo(() => orderRuns(summarizeRuns(runs)), [runs])

  // Phase-tree folding happens inside <WorkflowRunTree>; here we only keep the
  // budget badge for the detail header.
  const events = detail?.events ?? []
  const budget = useMemo(() => latestBudget(events), [events])

  return (
    <div className="px-4 md:px-6 pb-8 grid grid-cols-1 lg:grid-cols-2 gap-6">
      {/* ----- Runs list ----- */}
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-2 text-[13px] text-muted">
          <ListTree size={14} /> {i18nT('apps.workflows.workflowsRuns.background_runs')}
          <span className="ml-auto text-[11px] tabular-nums">{rows.length}</span>
        </div>

        {listError && (
          <div className="text-[12px] text-red-500 border border-red-500/30 rounded p-2">
            {i18nT('apps.workflows.workflowsRuns.could_not_load_runs')} {listError}
          </div>
        )}

        {rows.length === 0 && !listError && (
          <div className="text-[12px] text-muted border border-dashed border-border rounded p-4">
            {i18nT('apps.workflows.workflowsRuns.no_background_runs_yet_runs_started_in_the_backg')}
          </div>
        )}

        <ul className="flex flex-col gap-1.5">
          {rows.map(row => (
            <li key={row.run_id}>
              <div
                className={`flex items-center gap-2 px-3 py-2 rounded border text-[12px] cursor-pointer ${
                  row.run_id === selectedId
                    ? 'border-primary bg-card'
                    : 'border-border hover:bg-card'
                }`}
                onClick={() => select(row.run_id)}
                role="button"
                tabIndex={0}
                onKeyDown={e => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    select(row.run_id)
                  }
                }}
              >
                {row.badge.active ? (
                  <Loader2 size={14} className="text-accent animate-spin shrink-0" />
                ) : row.status === 'finished' ? (
                  <CheckCircle2 size={14} className="text-green-500 shrink-0" />
                ) : row.status === 'failed' ? (
                  <XCircle size={14} className="text-red-500 shrink-0" />
                ) : (
                  <Ban size={14} className="text-muted shrink-0" />
                )}
                <div className="flex flex-col min-w-0">
                  <span className="font-medium truncate">{row.name}</span>
                  <span className="font-mono text-[10px] text-muted truncate">
                    {row.run_id}
                    {row.author ? ` · ${row.author}` : ''}
                  </span>
                </div>
                <div className="ml-auto flex items-center gap-2 shrink-0">
                  <span className="text-[10px] text-muted tabular-nums">
                    {row.agentCount > 0 ? `${row.agentCount} agents · ` : ''}
                    {row.eventCount} {i18nT('apps.workflows.workflowsRuns.events')}
                  </span>
                  <Badge variant={row.badge.variant}>{row.badge.label}</Badge>
                  {row.cancellable && (
                    <button
                      onClick={e => {
                        e.stopPropagation()
                        cancelRun(row.run_id)
                      }}
                      disabled={cancellingId === row.run_id}
                      className="flex items-center gap-1 px-2 py-0.5 text-[11px] rounded border border-border disabled:opacity-50"
                      aria-label={`cancel ${row.name}`}
                    >
                      <Ban size={12} /> {cancellingId === row.run_id ? i18nT('apps.workflows.workflowsRuns.cancelling') : i18nT('apps.workflows.workflowsRuns.cancel')}
                    </button>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      </div>

      {/* ----- Selected run detail ----- */}
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-2 text-[13px] text-muted">
          <WorkflowIcon size={14} /> {detail ? detail.name || detail.run_id : i18nT('apps.workflows.workflowsRuns.run_detail')}
          {budget && (
            <span className="ml-auto text-[11px] tabular-nums">
              {i18nT('apps.workflows.workflowsRuns.budget')} {budget.spent}
              {budget.total != null ? ` / ${budget.total}` : ''}
            </span>
          )}
        </div>

        {detailError && (
          <div className="text-[12px] text-red-500 border border-red-500/30 rounded p-2">
            {detailError}
          </div>
        )}

        {!selectedId && !detailError && (
          <div className="text-[12px] text-muted border border-dashed border-border rounded p-4">
            {i18nT('apps.workflows.workflowsRuns.select_a_run_to_see_its_phases_agents_and_result')}
          </div>
        )}

        {/* Phase tree + narrator logs + result panel — the shared component, so
            the Author view, this Runs view, and the chat surfaces stay identical
            (and free of copy-paste duplication). */}
        {detail && (
          <WorkflowRunTree
            events={events}
            status={detail.status}
            result={detail.result}
            error={detail.error}
          />
        )}
      </div>
    </div>
  )
}
