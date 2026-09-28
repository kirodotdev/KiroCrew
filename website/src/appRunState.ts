/**
 * Per-app rail run state, derived from the app's OWN cron jobs.
 *
 * An installed app that wants its sidebar row to say "a job of mine is running
 * / that job failed" needs no new route and no new manifest field: it already
 * declares `contributes.crons[]`, the host stamps `created_by = "app:<name>"`
 * when it promotes those definitions (`apps/cron_sdk.py`), and the host owns
 * every fact this module reads. `GET /api/crons` carries the app attribution as
 * the derived `app` field, plus the same `is_running` / `last_status` /
 * `enabled` the Schedule page already renders.
 *
 * So no state is pushed and no state vocabulary is invented here. The four
 * states below are the ones `SchedulePage` already derives from those three
 * fields, and the precedence mirrors that page's own status cell so the rail and
 * the Schedule table can never disagree about one job.
 *
 * Why this is not the existing notification badge. That badge is a COUNT and it
 * means "something is waiting for you" -- a persistent item you should come and
 * read. A run state is transient and is not addressed to anyone: a spinner while
 * a job runs, an error dot until the next run clears it. Folding one into the
 * other would make a running job look like unread mail and would clear a real
 * waiting count when a job happened to succeed.
 *
 * Why an app cannot abuse it. Every input is host-written: an app supplies no
 * part of `created_by`, cannot write the in-flight marker (`cron_inflight.py`),
 * and cannot set `last_status` -- execution does. A row stuck on `error`
 * therefore means the app genuinely has a cron that keeps failing, which is true
 * information rather than a spoofed badge, and pausing that job on the Schedule
 * page clears the row (see `isHealthSignal`).
 */
import { toDate } from './i18n/format'
import type { CronJob } from './types'

/**
 * The run state of an app's scheduled work, as one value for its rail row.
 *
 * `idle` is represented by ABSENCE from the derived map rather than by a member
 * here: a caller merging maps must be able to tell "this app has nothing to
 * say" from "this app says it is idle", and only absence does that without
 * clearing a value another source set.
 */
export type AppRunState = 'running' | 'error' | 'success'

/**
 * Rank used to collapse several jobs of one app into a single state.
 *
 * Mirrors the order of `SchedulePage`'s status cell, which tests `is_running`
 * BEFORE it looks at `last_status`: a run in flight is what the user most wants
 * to see, and unlike an error it clears by itself. An error that is still true
 * reappears when the run finishes, so nothing is lost by ranking it second.
 */
const RANK: Record<AppRunState, number> = { running: 3, error: 2, success: 1 }

/**
 * How long after a run finishes its `success` state still shows, in ms.
 *
 * A success that never expired would put a permanent mark on every app with a
 * healthy schedule, which is noise rather than information -- and it is the
 * benign form of exactly the failure a rail indicator must not have: a state
 * nothing clears. An `error` needs no window because the next run replaces it,
 * and a `running` job clears when it finishes.
 *
 * The window exists so a SHORT run is visible at all. A job that finishes in two
 * seconds would otherwise flash a spinner between two refetches and leave
 * nothing behind, so a user who looked away could not tell it from a job that
 * never ran. Ninety seconds is above the dashboard's own refetch cadence (the
 * shared cron query is invalidated on every server `refresh` frame) by enough
 * that the state survives at least one refresh, and short enough to be gone by
 * the time the user next looks for a different reason.
 */
export const SUCCESS_WINDOW_MS = 90_000

/**
 * Whether *job* is a health signal at all.
 *
 * A job the user paused is deliberately excluded, and that is not a judgement
 * invented here: `cron.py`'s `unhealthy_jobs_from_disk` skips a user-paused
 * record with the reason written out -- "a job the user paused on purpose is not
 * a health signal, and a stale `last_status` from before they paused it is not
 * either". Honouring it here gives the user an opt-out that works with the app's
 * page closed, which is the whole regime this indicator covers.
 *
 * Only a USER pause is an opt-out. Execution also pauses a job itself after
 * enough consecutive failures (`auto_paused`, which sets `enabled=False` too),
 * and that job is not a quiet one to skip -- it is the worst failure the app
 * has. Reading `enabled` alone would drop exactly that job, so the wire carries
 * `user_paused` separately and this tests it instead. `unhealthy_jobs_from_disk`
 * draws the same line: it skips a user pause unconditionally and keeps an
 * auto-paused job in its own advisory bucket.
 *
 * A job that is running is kept even if it is disabled: `enabled` governs
 * whether it will be scheduled AGAIN, not whether the run happening now is
 * real, and hiding a run in progress because someone just paused the job would
 * make the indicator contradict the Schedule page.
 */
function isHealthSignal(job: CronJob): boolean {
  if (job.is_running === true) return true
  if (job.user_paused === true) return false
  // An auto-paused job arrives as `enabled: false` with `user_paused: false`,
  // and must still count -- so a disabled job is only dropped when the wire
  // positively says the user did it. An older gateway that omits `user_paused`
  // falls back to `enabled`, the previous behaviour.
  if (job.user_paused === false) return true
  return job.enabled
}

/**
 * The state one job contributes, or null when it contributes nothing.
 *
 * A job that has never run carries no `last_status`, and "ready" is not a state
 * worth marking a rail icon with -- the icon's plain form already says that.
 *
 * `nowMs` is a parameter rather than a `Date.now()` call so the success window is
 * testable without a fake clock, and so every job in one pass is judged against
 * the same instant.
 */
function jobState(job: CronJob, nowMs: number): AppRunState | null {
  if (job.is_running === true) return 'running'
  if (job.last_status === 'error') return 'error'
  if (job.last_status !== 'ok') return null
  // `toDate` owns the seconds-vs-milliseconds question for the whole dashboard
  // (`i18n/format.ts` documents a value below SECONDS_CEILING as seconds), so
  // this does not restate the unit. A job reporting ok with no usable timestamp
  // cannot be placed in or out of the window, so it contributes nothing rather
  // than showing a mark that would never expire.
  const lastRun = toDate(job.last_run_ts)
  if (lastRun === null) return null
  const ageMs = nowMs - lastRun.getTime()
  // A negative age means the recorded finish is in the future -- clock skew
  // between the gateway host and the browser. Treating it as fresh is the safe
  // reading: it is a real recent run, and the window still expires it once the
  // browser clock passes it.
  if (ageMs > SUCCESS_WINDOW_MS) return null
  return 'success'
}

/**
 * Collapse the cron list into one run state per owning app.
 *
 * Apps with nothing to report are ABSENT from the result rather than present
 * with an idle value, for the reason `AppRunState` documents.
 */
export function appRunStates(
  jobs: readonly CronJob[],
  nowMs: number = Date.now(),
): Record<string, AppRunState> {
  // Prototype-less for the reason `appNotificationBadges` is: an app name is
  // attacker-chosen and the manifest reserves only a namespace list, so
  // `constructor`, `toString` and `__proto__` are all valid kebab-case names. On
  // a plain `{}`, reading `states['constructor']` for an app with no entry
  // returns the inherited `Object` function -- truthy, so a renderer's
  // `if (state)` guard would pass and then index `RANK` with a function.
  const states: Record<string, AppRunState> = Object.create(null)
  for (const job of jobs) {
    const app = job.app
    // Only an app-owned job. The host writes `app` only for one, so a
    // person-owned job is absent/null here and contributes nothing.
    if (!app) continue
    if (!isHealthSignal(job)) continue
    const next = jobState(job, nowMs)
    if (next === null) continue
    const current = states[app]
    if (current === undefined || RANK[next] > RANK[current]) states[app] = next
  }
  return states
}

/**
 * Milliseconds until the earliest showing `success` mark expires, or null.
 *
 * The derivation is a pure function of the job list, so a caller that recomputes
 * it only when that list changes leaves a `success` on screen past
 * `SUCCESS_WINDOW_MS` whenever no refresh happens to follow -- the window would
 * then be a claim the code does not keep. This returns the delay a caller can
 * arm a single timer on, so the mark clears on time without polling: a clock
 * ticking every second would re-render the rail continuously to show the same
 * thing in every second but one.
 *
 * Null when nothing is showing `success`, so a caller arms no timer at all in
 * the common case. Never negative: an already-expired run contributes no mark,
 * so there is nothing to wait for.
 *
 * Only a success the app actually SHOWS counts. An app whose winning state is
 * `error` or `running` may still own a recent successful job, and arming on that
 * one would fire a timer whose expiry changes no pixel -- the rank already hides
 * it. So the resolved states decide which jobs are worth waiting for, which is
 * what makes "no timer in the common case" true rather than nearly true.
 */
export function nextSuccessExpiryMs(
  jobs: readonly CronJob[],
  nowMs: number = Date.now(),
): number | null {
  const shown = appRunStates(jobs, nowMs)
  let soonest: number | null = null
  for (const job of jobs) {
    if (!job.app) continue
    if (shown[job.app] !== 'success') continue
    if (!isHealthSignal(job)) continue
    if (jobState(job, nowMs) !== 'success') continue
    const lastRun = toDate(job.last_run_ts)
    if (lastRun === null) continue
    // Clamped at 0 only as a lower bound. A future timestamp -- the clock skew
    // `jobState` admits as fresh -- correctly yields window + skew, because that
    // mark is due to clear 90s after the moment it claims to have run.
    const left = Math.max(0, SUCCESS_WINDOW_MS - (nowMs - lastRun.getTime()))
    if (soonest === null || left < soonest) soonest = left
  }
  return soonest
}
