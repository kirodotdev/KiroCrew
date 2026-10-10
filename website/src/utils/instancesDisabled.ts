import { parseErrorCode } from './errorReport'

/**
 * True only for the gateway's own `instances_disabled` 403 from
 * `/api/instances`: the one denial a UI treats as "feature off" and leaves
 * silent. That endpoint's other 403s (a non-owner caller, a Slack-origin
 * request) return false, so every caller (the top bar, the sidebar's
 * embedded-pane notice, the Instances and Remote crew settings panels, the
 * Deploy my crew registry read and the digit-chord shortcuts) shows them as
 * failures or keeps working, never as "feature off".
 *
 * Checked by shape (`status` + `body`) rather than `instanceof ApiError`, so it
 * reads the same field the client's own benign-denial parser keys on.
 */
export function isInstancesDisabledError(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false
  const { status, body } = error as { status?: unknown; body?: unknown }
  return status === 403 && typeof body === 'string' && parseErrorCode(body) === 'instances_disabled'
}
