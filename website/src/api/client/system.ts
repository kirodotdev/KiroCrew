/**
 * Gateway health and housekeeping: the status, tunnel and host-system
 * probes, the /api/system/session-storage cleanup, restore, empty, inventory
 * and trash surface, session health with the durable task queue's capacity
 * view, the log level, and the problem-report diagnostics bundle.
 */

import type { SessionStorageReport, SessionStorageCleanup, SessionStorageEmptyJob, SessionInventoryList, SessionInventoryDetail, SessionTrashResult } from '../../types'
import type { TasksSummary, TasksListResponse, TaskDetailResponse } from '../tasks'
import type { ClientTransport } from './transport'

/** Tunnel status surfaced by GET /api/tunnel/status (backend TunnelManager).
 *  Enables mobile dashboard access via a remote tunnel. */
export interface TunnelStatus {
  state: 'disabled' | 'starting' | 'connected' | 'reconnecting' | 'error' | 'stopped'
  url: string
  error: string
  uptime: number
  reconnect_attempt: number
  /**
   * Why a `disabled` tunnel is off. `boot_flag` means the gateway was started
   * with `--no-tunnel` and will never publish, whatever `tunnel.enabled` says in
   * its config (a Dev Fleet pod always boots that way). Empty for every other
   * state and for an ordinary unconfigured tunnel. Optional because a gateway
   * older than this field does not send it.
   */
  reason?: string
}

export function createSystemEndpoints({ get, post, j }: ClientTransport) {
  const statusAndStorage = {
    status: () => fetch('/api/status').then(j),
    tunnelStatus: () => fetch('/api/tunnel/status').then(j) as Promise<TunnelStatus>,
    system: () => fetch('/api/system').then(j),
    sessionStorage: () => get('/api/system/session-storage').then(j) as Promise<SessionStorageReport>,
    sessionStorageCleanup: (olderThanDays: number, dryRun = false) =>
      post('/api/system/session-storage/cleanup', { older_than_days: olderThanDays, dry_run: dryRun })
        .then(j) as Promise<SessionStorageCleanup>,
    sessionStorageRestore: (batchId: string, uids?: string[]) =>
      post('/api/system/session-storage/restore', uids ? { batch_id: batchId, uids } : { batch_id: batchId })
        .then(j) as Promise<{ restored: number }>,
    /** Starts an empty and returns the job; the delete outlives this request. */
    sessionStorageEmpty: (batchIds: string[]) =>
      post('/api/system/session-storage/empty', { batch_ids: batchIds }).then(j) as Promise<SessionStorageEmptyJob>,
    /** The running or last-finished empty. Cheap — no store is walked, so it polls. */
    sessionStorageEmptyStatus: () =>
      get('/api/system/session-storage/empty').then(j) as Promise<{ job: SessionStorageEmptyJob | null }>,
    /** Session inventory — the flat list contract (§1). */
    sessionInventory: () =>
      get('/api/system/session-storage/sessions').then(j) as Promise<SessionInventoryList>,
    /** Session detail — lazy per-row fetch (§2). */
    sessionInventoryDetail: (uid: string) =>
      get(`/api/system/session-storage/sessions/${encodeURIComponent(uid)}`).then(j) as Promise<SessionInventoryDetail>,
    /** Move explicit selection to trash (§3). */
    sessionInventoryTrash: (uids: string[]) =>
      post('/api/system/session-storage/trash', { uids }).then(j) as Promise<SessionTrashResult>,
  }

  const taskQueue = {
    sessionsHealth: () => fetch('/api/sessions/health').then(j),
    // Durable task queue + capacity view (System > Services "Tasks & capacity").
    tasksSummary: () => fetch('/api/tasks/summary').then(j) as Promise<TasksSummary>,
    tasksList: (params: { state?: string; lane?: string; limit?: number } = {}) => {
      const q = new URLSearchParams()
      if (params.state) q.set('state', params.state)
      if (params.lane) q.set('lane', params.lane)
      if (params.limit != null) q.set('limit', String(params.limit))
      const qs = q.toString()
      return fetch(`/api/tasks${qs ? `?${qs}` : ''}`).then(j) as Promise<TasksListResponse>
    },
    taskDetail: (id: string) => fetch(`/api/tasks/${encodeURIComponent(id)}`).then(j) as Promise<TaskDetailResponse>,
    taskCancel: (id: string) => post(`/api/tasks/${encodeURIComponent(id)}/cancel`).then(j) as Promise<{ ok: boolean; cancelled: boolean; code?: string }>,
  }

  const logs = {
    // Logs
    logLevel: () => fetch('/api/logs/level').then(j),
    setLogLevel: (level: string) => post('/api/logs/level', { level }).then(j),
  }

  const diagnostics = {
    collectDiagnostics: (body: { note: string; include_logs: boolean }) =>
      post('/api/diagnostics/collect', body).then(j) as Promise<{
        zip_path: string
        filename: string
        included: string[]
        skipped: string[]
        redaction_summary: Record<string, number>
        total_redactions: number
        github_issue_url: string
        download_url: string
      }>,
  }

  return { statusAndStorage, taskQueue, logs, diagnostics }
}
