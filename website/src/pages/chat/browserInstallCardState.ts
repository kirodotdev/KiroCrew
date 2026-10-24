/**
 * What the in-chat browser-support card shows, as one pure function of the
 * gateway's install status plus this tab's own in-flight request.
 *
 * The gateway owns the install job, so a card in a second tab, a card under an
 * older reply and the Settings -> Browser panel all read the same operation.
 */
import type { BrowserInstallData } from '../../api/client'
import { currentActivity, finishedJob, type PendingRequest } from '../settings/browserInstallState'

export type BrowserInstallCardState =
  /** No status has loaded yet. */
  | { kind: 'checking' }
  /** The status read failed and nothing is cached. */
  | { kind: 'unavailable' }
  /** The CLI and its browser are present: browsing can go ahead. */
  | { kind: 'installed' }
  /** An install (or any operation holding the one install slot) is running.
   *  `stage` is the gateway's step name, when it reported one. */
  | { kind: 'running'; stage: string | null; elapsedS: number | null }
  /** The latest CLI setup ended without succeeding. */
  | { kind: 'failed'; detail: string | null }
  /** Nothing installed and nothing running: offer the install. A host without
   *  Node.js is still offered it: the installer resolves Node from more places
   *  than the status probe reads, and when it truly is missing the failure
   *  names the fix and the card links Settings -> Browser. */
  | { kind: 'needed' }

export function browserInstallCardState(
  data: BrowserInstallData | undefined,
  pending: PendingRequest | null,
  pendingSince: number,
): BrowserInstallCardState {
  if (!data) return pending ? { kind: 'running', stage: 'preparing', elapsedS: null } : { kind: 'checking' }
  const activity = currentActivity(data, pending, pendingSince)
  if (activity) {
    if (activity.source === 'job') {
      return { kind: 'running', stage: activity.job.stage, elapsedS: activity.job.elapsed_s }
    }
    return { kind: 'running', stage: activity.source === 'pending' ? 'preparing' : null, elapsedS: null }
  }
  // A failed CLI setup wins over filesystem detection because the CLI may be
  // present while required host libraries are still missing.
  const last = finishedJob(data)
  if (last && last.kind === 'cli_setup' && (last.status === 'failed' || last.status === 'interrupted')) {
    return { kind: 'failed', detail: last.error_detail }
  }
  // `browser_ok` false means the CLI is present but its browser build is not, so
  // the next browse would still fail: keep offering the install until both are.
  if (data.installed && data.browser_ok !== false) return { kind: 'installed' }
  return { kind: 'needed' }
}
