/**
 * useRunSnapshot — fetch the full run snapshot for a single workflow run id,
 * with optional polling while the run is still in flight. Shared by the chat
 * WorkflowProgressBar (expanded view) and the ActivityViewer Workflows tab.
 *
 * Backend: GET /api/workflows/runs/{run_id} returns the full snapshot
 * including `events[]` and the authored `source` (only on this endpoint).
 *
 * Built on `@tanstack/react-query` (project convention): request dedup, caching,
 * and a self-stopping `refetchInterval` that polls only while the run is still
 * `running` — no manual setInterval/useRef plumbing (which previously caused an
 * infinite refetch loop when the snapshot was in the effect dependency array).
 */
import { useQuery } from '@tanstack/react-query'
import type { WfEvent } from './runModel'

const CORE_API_BASE = '/api/workflows'

export interface RunSnapshot {
  run_id: string
  name?: string
  status: 'running' | 'finished' | 'failed' | 'cancelled' | string
  result?: unknown
  error?: string | null
  event_count?: number
  source?: string
  events: WfEvent[]
}

interface UseRunSnapshotOptions {
  /** Active fetch/poll only while this is true. */
  enabled: boolean
  /** Poll interval (ms) while the run is still running. Default 2000. */
  pollMs?: number
}

interface UseRunSnapshotResult {
  snapshot: RunSnapshot | null
  loading: boolean
  error: string | null
  refresh: () => void
}

export function useRunSnapshot(
  run_id: string | null,
  { enabled, pollMs = 2000 }: UseRunSnapshotOptions,
): UseRunSnapshotResult {
  const query = useQuery({
    queryKey: ['workflow-run-snapshot', run_id],
    queryFn: async () => {
      const r = await fetch(
        `${CORE_API_BASE}/runs/${encodeURIComponent(run_id!)}`,
        { credentials: 'same-origin' },
      )
      if (!r.ok) throw new Error(`GET /runs/${run_id} → ${r.status}`)
      return (await r.json()) as RunSnapshot
    },
    enabled: enabled && !!run_id,
    // Self-stopping poll: keep refetching only while the run is still running.
    refetchInterval: q => (q.state.data?.status === 'running' ? pollMs : false),
  })

  return {
    snapshot: query.data ?? null,
    loading: query.isLoading,
    error: query.error instanceof Error ? query.error.message : query.error ? String(query.error) : null,
    refresh: () => {
      query.refetch()
    },
  }
}
