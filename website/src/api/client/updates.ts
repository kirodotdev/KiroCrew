/**
 * Gateway updates and restart: the update check, changelog and releases,
 * apply/auto/channel, restart without update, the in-app update arm with its
 * status and dismissal, cancel and simulate.
 */

import type { UpdateCheckResult } from '../../types'
import type { ClientTransport } from './transport'

export function createUpdatesEndpoints({ post, del, j, jfetch: fetch }: ClientTransport) {
  const lifecycle = {
    // Update
    checkUpdate: () => fetch('/api/update/check').then(j) as Promise<UpdateCheckResult>,
    changelog: () => fetch('/api/changelog').then(j),
    releases: () => fetch('/api/releases').then(j),
    applyUpdate: () => post('/api/update').then(j),
    setAutoUpdate: (enabled: boolean) => post('/api/update/auto', { enabled }).then(j),
    /**
     * Move this install onto another release channel. Changes which feed the next
     * check compares against; it never installs anything, so the response is the
     * re-run check against the NEW channel.
     */
    setUpdateChannel: (channel: string) => post('/api/update/channel', { channel }).then(j) as Promise<UpdateCheckResult>,
    /**
     * Restart the gateway without updating. The connection drops as the process
     * image is replaced, so callers must treat a network failure after a 200 as
     * the expected path rather than an error.
     */
    restartGateway: () => post('/api/restart').then(j),
    // In-app wheel update step-up: arming records the request and returns the
    // host command to run; the approval nonce never reaches this client.
    armUpdate: () => post('/api/update/arm').then(j) as Promise<{ ok?: boolean; armed?: boolean; request_id?: string; version?: string; expires_in?: number; approve_command?: string; error?: string; code?: string }>,
    armStatus: () => fetch('/api/update/arm').then(j) as Promise<{ armed: boolean; managed_by?: string; request_id?: string; version?: string; requested_by?: string; armed_at?: number; expires_in?: number; approve_command?: string }>,
    // Decline a packaged-app update request. Removes a nudge, grants nothing.
    dismissUpdateArm: (requestId: string) => del(`/api/update/arm?request_id=${encodeURIComponent(requestId)}`).then(j) as Promise<{ ok?: boolean; armed?: boolean; dismissed?: boolean }>,
    cancelUpdate: () => post('/api/update/cancel').then(j),
    simulateUpdate: (opts?: { delay?: number; fail_at?: string }) => post('/api/update/simulate', opts || {}).then(j),
  }

  return { lifecycle }
}
