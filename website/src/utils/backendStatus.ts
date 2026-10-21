/**
 * Banner copy for an app whose backend is repeatedly failing to restart.
 *
 * The gateway's health supervisor restarts an enabled app's backend after an
 * unexpected exit (see `_restart_exited_backend` in the backend). After a bounded
 * fast ramp it drops to a slow steady retry cadence and records that state on the
 * app's `backend_status.restart_exhausted`. There is no terminal give-up — retries
 * continue while the app is enabled — so this is "repeatedly failing to come back",
 * not "gave up".
 *
 * This module turns that status fact into the `{ title, message }` a backend-
 * unavailable `ErrorNotice` renders, so the banner names the condition and the
 * toggle-to-retry action instead of the generic "unavailable". It is pure: no
 * network, no React, no side effects — a formatter the callers can unit-test and
 * render.
 */
import { i18nT } from '../i18n/t'

/** The restart-exhausted block the gateway puts on `backend_status`. */
export interface RestartExhausted {
  /** The fast-attempt ceiling the ramp exhausted before dropping to the steady cadence. */
  attempts: number
}

/** The `backend_status` block the dashboard reads from `GET /api/apps`. */
export interface BackendStatus {
  running?: boolean
  port?: number
  healthy?: boolean
  pid?: number
  restart_exhausted?: RestartExhausted | null
}

export interface BackendUnavailableNotice {
  title: string
  message: string
}

/**
 * The restart-exhausted banner copy for `appLabel`, or `null` when the backend is
 * not in that state. `appLabel` is the app's display name (what the user sees),
 * not its slug. Returning `null` keeps the caller rendering its generic
 * backend-unavailable copy when nothing special is happening.
 */
export function restartExhaustedNotice(
  appLabel: string,
  status: BackendStatus | null | undefined,
): BackendUnavailableNotice | null {
  const exhausted = status?.restart_exhausted
  if (!exhausted) return null
  // `attempts` is the count the ramp exhausted; a non-finite or non-positive value
  // is a malformed payload, so fall back to 1 for a sensible plural rather than
  // rendering "failed 0 restarts".
  const count =
    Number.isFinite(exhausted.attempts) && exhausted.attempts > 0
      ? Math.floor(exhausted.attempts)
      : 1
  return {
    title: i18nT('apps.backend.restart_exhausted_title', { app: appLabel }),
    message: i18nT('apps.backend.restart_exhausted_message', { app: appLabel, count }),
  }
}
