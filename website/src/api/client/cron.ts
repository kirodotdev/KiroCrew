/**
 * Scheduled jobs: cron CRUD and batch delete, run/cancel/toggle/ack, the
 * hand-off to chat, vault secret grants, run history, run detail and scripts,
 * and cron folders.
 */

import type { CronJob } from '../../types'
import type { ClientTransport } from './transport'

export function createCronEndpoints({ post, put, del, j, sessionKeyHeader: _sk }: ClientTransport) {
  const jobs = {
    // Crons
    crons: (): Promise<{ jobs?: CronJob[] }> => fetch('/api/crons').then(j),
    createCron: (body: object) => post('/api/crons', body).then(j),
    deleteCron: (id: string) => del('/api/crons/' + id).then(j),
    batchDeleteCron: (ids: string[]) => del('/api/crons', { ids }).then(j),
    updateCron: (id: string, body: object) =>
      fetch('/api/crons/' + id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j),
    runCron: (id: string) => post('/api/crons/' + id + '/run').then(j),
    /** Grant/revoke vault secrets, or act on an agent-requested pending grant.
     * Body: {secret_env: {...}} grants (empty object revokes), or
     * {approve_pending: true, expected_secret_env, expected_ts} /
     * {deny_pending: true}. Approval restates the displayed request; the server
     * refuses with 409 stale_request if it was replaced. Operator-only server-side. */
    cronSecretsGrant: (id: string, body: { secret_env?: Record<string, string>; approve_pending?: boolean; deny_pending?: boolean; expected_secret_env?: Record<string, string> | null; expected_ts?: number; expected_source_sha256?: string }) =>
      put('/api/crons/' + id + '/secrets', body).then(j),
    cancelCron: (id: string) => post('/api/crons/' + id + '/cancel').then(j),
    cronToChat: (id: string) => post('/api/crons/' + id + '/to-chat').then(j),
    toggleCron: (id: string, enabled: boolean) => post('/api/crons/' + id + '/enable', { enabled }).then(j),
    cronHistory: (jobId: string, offset?: number, limit?: number) => {
      const p = new URLSearchParams()
      if (offset != null) p.set('offset', String(offset))
      if (limit != null) p.set('limit', String(limit))
      const qs = p.toString()
      return fetch('/api/crons/' + jobId + '/history' + (qs ? '?' + qs : ''), { headers: { ..._sk } }).then(j)
    },
    cronRunDetail: (jobId: string, runId: string) => fetch('/api/crons/' + jobId + '/history/' + encodeURIComponent(runId), { headers: { ..._sk } }).then(j),
    cronScript: (jobId: string) => fetch('/api/crons/' + jobId + '/script', { headers: { ..._sk } }).then(j),
  }

  const historyAndFolders = {
    ackCron: (id: string, summary: string, ts?: string) => post('/api/crons/' + id + '/ack', { summary, ts }).then(j),
    cronHistoryAll: (opts?: { offset?: number; limit?: number; jobId?: string }) => {
      const p = new URLSearchParams()
      if (opts?.offset != null) p.set('offset', String(opts.offset))
      if (opts?.limit != null) p.set('limit', String(opts.limit))
      if (opts?.jobId) p.set('job_id', opts.jobId)
      return fetch('/api/crons/history' + (p.toString() ? '?' + p : ''), { headers: { ..._sk } }).then(j)
    },

    // Cron Folders
    cronFolders: () => fetch('/api/cron-folders').then(j),
    createCronFolder: (name: string) => post('/api/cron-folders', { name }).then(j),
    updateCronFolder: (id: string, body: { name?: string }) =>
      fetch('/api/cron-folders/' + id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j),
    deleteCronFolder: (id: string) => del('/api/cron-folders/' + id).then(j),
  }

  return { jobs, historyAndFolders }
}
