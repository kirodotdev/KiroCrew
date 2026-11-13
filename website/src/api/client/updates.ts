/**
 * Gateway updates and restart: the update check, changelog and releases,
 * apply/auto/channel, restart without update, the in-app update arm with its
 * status and dismissal, cancel and simulate.
 */

import type { UpdateCheckResult } from '../../types'
import { askAppConfirm } from '../../lib/appConfirm'
import { i18nT } from '../../i18n/t'
import { asWorkInFlight, WorkConfirmDeclined, workInFlightNotice, type WorkInFlight } from '../../utils/workInFlight'
import type { ClientTransport } from './transport'

export function createUpdatesEndpoints({ post, del, j, jfetch: fetch }: ClientTransport) {
  /**
   * The one confirmation for a restart that would cut running work. Every
   * caller of `restartGateway` gets it from here: the gateway answers with what
   * is running, this shows it, and `force: true` goes out only after the person
   * says yes. A "no" throws `WorkConfirmDeclined`, which callers treat as a quiet
   * cancel, not an error.
   */
  const confirmRestartUnderWork = async (work: WorkInFlight): Promise<void> => {
    const ok = await askAppConfirm({
      title: i18nT('components.updateFoundModal.work_running_title'),
      body: workInFlightNotice(work),
      confirmLabel: i18nT('components.updateFoundModal.restart_anyway'),
      layer: 'top',
    })
    if (!ok) throw new WorkConfirmDeclined(i18nT('components.updateFoundModal.restart_cancelled'))
  }

  /**
   * Restart the gateway without updating. Asked with `force: false` first: while
   * work is running the gateway answers 409 `work_in_flight` and restarts nothing.
   * The connection drops as the process image is replaced, so callers must treat
   * a network failure after a 200 as the expected path rather than an error.
   */
  const restartGateway = async () => {
    const first = await post('/api/restart', { force: false })
    if (first.status === 409) {
      const work = asWorkInFlight(await first.clone().json().catch(() => null))
      if (work) {
        await confirmRestartUnderWork(work)
        return post('/api/restart', { force: true }).then(j)
      }
    }
    return j(first)
  }

  const lifecycle = {
    // Update
    checkUpdate: () => fetch('/api/update/check').then(j) as Promise<UpdateCheckResult>,
    changelog: () => fetch('/api/changelog').then(j),
    releases: () => fetch('/api/releases').then(j),
    /**
     * Update when I restart: ARMS the update. Nothing installs and nothing restarts
     * now, whatever is running; the gateway applies it at its next start, before it
     * serves anything. Answers `202 {armed: true, version}`.
     */
    applyUpdate: () => post('/api/update', { when: 'next-start' }).then(j) as Promise<{ armed?: boolean; version?: string }>,
    /** Take the armed update back: the next start applies nothing. */
    disarmUpdate: () => post('/api/update', { when: 'cancel' }).then(j) as Promise<{ armed?: boolean }>,
    setAutoUpdate: (enabled: boolean) => post('/api/update/auto', { enabled }).then(j),
    /**
     * Whether config.local.json pins `auto_update`, and whose entry it is (`overlay_source`
     * `user` or `policy`, with the policy's `{label, detail}`), read without running a check.
     */
    autoUpdatePin: () => fetch('/api/update/auto').then(j) as Promise<{ overlay_override?: unknown; overlay_source?: unknown; overlay_policy?: unknown }>,
    /**
     * Move this install onto another release channel. Changes which feed the next
     * check compares against; it never installs anything, so the response is the
     * re-run check against the NEW channel.
     */
    setUpdateChannel: (channel: string) => post('/api/update/channel', { channel }).then(j) as Promise<UpdateCheckResult>,
    restartGateway,
    // In-app wheel update step-up: arming records the request and returns the
    // host command to run; the approval nonce never reaches this client.
    //
    // The gateway RE-CHECKS the feed before arming, so the armed `version` can
    // outrank the one the panel was offering (that verdict is refreshed only
    // every 12h). `version_display` is the armed version folded for display, and
    // is what the armed copy must name — the raw one keeps a promoted build's rc
    // stamp.
    // `offered`: the version the clicked button named. The gateway reads it only
    // to tell an upgrade click from a channel-move click; it arms its own fresh
    // verdict either way.
    armUpdate: (offered?: string) => post('/api/update/arm', offered ? { offered } : undefined).then(j) as Promise<{ ok?: boolean; armed?: boolean; request_id?: string; version?: string; version_display?: string; expires_in?: number; approve_command?: string; error?: string; code?: string }>,
    armStatus: () => fetch('/api/update/arm').then(j) as Promise<{ armed: boolean; managed_by?: string; request_id?: string; version?: string; requested_by?: string; armed_at?: number; expires_in?: number; approve_command?: string }>,
    // Decline a packaged-app update request. Removes a nudge, grants nothing.
    dismissUpdateArm: (requestId: string) => del(`/api/update/arm?request_id=${encodeURIComponent(requestId)}`).then(j) as Promise<{ ok?: boolean; armed?: boolean; dismissed?: boolean }>,
    cancelUpdate: () => post('/api/update/cancel').then(j),
    simulateUpdate: (opts?: { delay?: number; fail_at?: string }) => post('/api/update/simulate', opts || {}).then(j),
  }

  return { lifecycle }
}
