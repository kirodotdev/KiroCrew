/**
 * Pure derivations behind the Browser settings panel.
 *
 * Kept out of the components so each fact the panel shows (which operation is
 * running, what each engine's row says, why a control is unavailable) is one
 * function of the gateway's answer plus this tab's own in-flight request, and so
 * each of those rules is testable without rendering.
 */
import type {
  BrowserEngine,
  BrowserEngineStatus,
  BrowserInstallData,
  BrowserInstallJob,
} from '../../api/client'

/** Engines the CLI can download. Mirrors `browser_cli.install.BROWSER_ENGINES`. */
export const BROWSER_ENGINES: readonly BrowserEngine[] = ['chromium', 'firefox', 'webkit']

/** This tab's own request, known only between the click and the POST's answer. */
export type PendingRequest =
  | { kind: 'cli_setup' }
  | { kind: 'engine_download'; engine: BrowserEngine }

/**
 * What is running on the gateway right now, and who says so.
 *
 * - `job`: the gateway named the operation. Authoritative in every tab.
 * - `pending`: this tab's click has not been answered yet. Only this tab can know
 *   the target, and only until the answer lands, which then supplies a `job`.
 * - `legacy`: an older gateway reports `installing` with no job detail, so the
 *   operation cannot be named at all.
 */
export type InstallActivity =
  | { source: 'job'; job: BrowserInstallJob }
  | { source: 'pending'; request: PendingRequest; startedAt: number }
  | { source: 'legacy' }

export function currentActivity(
  data: BrowserInstallData,
  pending: PendingRequest | null,
  pendingSince: number,
): InstallActivity | null {
  const job = data.install_job
  if (job && job.status === 'running') return { source: 'job', job }
  if (pending) return { source: 'pending', request: pending, startedAt: pendingSince }
  // `installing` with no running job is either an older gateway or a status that
  // disagrees with itself. Both keep the controls blocked: starting a second
  // install is the one outcome that is never right.
  if (data.installing) return { source: 'legacy' }
  return null
}

/** The most recent finished operation, retained by the gateway until replaced. */
export function finishedJob(data: BrowserInstallData): BrowserInstallJob | null {
  const job = data.install_job
  return job && job.status !== 'running' ? job : null
}

/**
 * One engine's detection result.
 *
 * `browser_status` wins when present because it is the only field that can say
 * `unknown`. The `browsers` booleans are the older fallback, and a gateway that
 * sends neither gets `unknown` rather than a confident "missing".
 */
export function engineStatus(data: BrowserInstallData, engine: BrowserEngine): BrowserEngineStatus {
  const detailed = data.browser_status?.[engine]
  if (detailed === 'downloaded' || detailed === 'missing' || detailed === 'unknown') return detailed
  const present = data.browsers?.[engine]
  if (present === true) return 'downloaded'
  if (present === false) return 'missing'
  return 'unknown'
}

/** The engine an activity is downloading, or `null` when it names none. */
export function activityEngine(activity: InstallActivity | null): BrowserEngine | null {
  if (!activity) return null
  if (activity.source === 'job') {
    return activity.job.kind === 'engine_download' ? activity.job.engine : null
  }
  if (activity.source === 'pending') {
    return activity.request.kind === 'engine_download' ? activity.request.engine : null
  }
  return null
}

/** True when the activity is CLI setup (named by the gateway or by this tab). */
export function activityIsCliSetup(activity: InstallActivity | null): boolean {
  if (!activity) return false
  if (activity.source === 'job') return activity.job.kind === 'cli_setup'
  if (activity.source === 'pending') return activity.request.kind === 'cli_setup'
  return false
}

export type EngineRowState = 'downloading' | 'downloaded' | 'missing' | 'retry' | 'unknown'

export function engineRowState(
  data: BrowserInstallData,
  engine: BrowserEngine,
  activity: InstallActivity | null,
): EngineRowState {
  if (activityEngine(activity) === engine) return 'downloading'
  const status = engineStatus(data, engine)
  if (status === 'downloaded') return 'downloaded'
  const last = finishedJob(data)
  const lastFailedHere =
    last !== null &&
    last.kind === 'engine_download' &&
    last.engine === engine &&
    (last.status === 'failed' || last.status === 'interrupted')
  if (lastFailedHere) return 'retry'
  return status
}

/**
 * Why install controls are unavailable, most fundamental first.
 *
 * A status the panel cannot read outranks everything: without it the panel does
 * not know whether something is running. A request whose answer was lost comes
 * next, because retrying it blind could start a duplicate download.
 */
export type InstallBlock =
  | { reason: 'status_unavailable' }
  | { reason: 'checking' }
  | { reason: 'engine_busy'; engine: BrowserEngine }
  | { reason: 'cli_busy' }
  | { reason: 'busy_generic' }

export function installBlock(
  activity: InstallActivity | null,
  statusUnavailable: boolean,
  outcomeUnknown: boolean,
): InstallBlock | null {
  if (statusUnavailable) return { reason: 'status_unavailable' }
  if (outcomeUnknown) return { reason: 'checking' }
  if (!activity) return null
  const engine = activityEngine(activity)
  if (engine) return { reason: 'engine_busy', engine }
  if (activityIsCliSetup(activity)) return { reason: 'cli_busy' }
  return { reason: 'busy_generic' }
}

/**
 * Seconds an operation has run, as of `now`.
 *
 * A running job's `elapsed_s` is the gateway's reading at response time, so it is
 * advanced by the time since that response arrived rather than recomputed from
 * `started_at` against this device's clock, which may disagree with the host's.
 */
export function elapsedSeconds(
  activity: InstallActivity | null,
  finished: BrowserInstallJob | null,
  receivedAt: number,
  now: number,
): number | null {
  if (activity?.source === 'job') {
    return Math.max(0, activity.job.elapsed_s + (now - receivedAt) / 1000)
  }
  if (activity?.source === 'pending') return Math.max(0, (now - activity.startedAt) / 1000)
  if (activity?.source === 'legacy') return null
  return finished ? Math.max(0, finished.elapsed_s) : null
}
