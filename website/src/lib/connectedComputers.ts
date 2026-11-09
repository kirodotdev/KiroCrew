/**
 * "Share this computer" (issue 29949): the renderer half of the native consent.
 *
 * Two steps, and the page decides neither the folder nor the tools:
 *   1. `api.connectedComputersLocalConsent(instanceId)` asks THIS computer's
 *      gateway for a one-time consent id (owner, direct local request, feature
 *      on). Nothing is granted.
 *   2. The id goes to the desktop shell's `connectedComputersAPI.confirm`. Its
 *      main process reads the crew name and tools from the gateway, shows the
 *      native confirmation, the OS folder picker and one yes/no per local tool,
 *      and only then redeems the id.
 *
 * A browser tab and the PWA have no bridge, so they cannot share at all (B13).
 */

import { ApiError, apiErrorCode } from '../api/apiError'

interface ConnectedComputersBridge {
  confirm: (consent: string) => Promise<{ ok?: boolean; grant?: unknown; error?: string } | undefined>
}

function bridge(): ConnectedComputersBridge | undefined {
  const api = (window as { connectedComputersAPI?: Partial<ConnectedComputersBridge> }).connectedComputersAPI
  return typeof api?.confirm === 'function' ? (api as ConnectedComputersBridge) : undefined
}

/** True only in the desktop shell. Read lazily so a test can stub the bridge per case. */
export function canShareThisComputer(): boolean {
  return bridge() !== undefined
}

/** Why a share did not happen, one state per sentence the user can act on. */
export type ShareRefusal =
  | 'cancelled'
  | 'needs_app'
  | 'not_local'
  | 'disabled'
  | 'protection_unavailable'
  | 'protection_broken'
  | 'turned_off'
  | 'enrollment_failed'
  | 'unknown_instance'
  | 'expired'
  | 'failed'

/** The refusal a gateway or shell error code stands for. */
export function shareRefusal(code: string | undefined): ShareRefusal {
  switch (code) {
    case 'cancelled': return 'cancelled'
    case 'unavailable': return 'needs_app'
    case 'not_local':
    case 'no_local_gateway':
    case 'internal_secret_required':
      return 'not_local'
    case 'connected_computers_disabled': return 'disabled'
    case 'protection_unavailable': return 'protection_unavailable'
    case 'protection_broken': return 'protection_broken'
    case 'turned_off': return 'turned_off'
    case 'enrollment_failed':
    case 'refused':
      return 'enrollment_failed'
    case 'unknown_instance': return 'unknown_instance'
    case 'no_consent':
    case 'invalid_consent':
      return 'expired'
    default: return 'failed'
  }
}

export type ShareOutcome = { ok: true } | { ok: false; refusal: ShareRefusal; code: string }

/**
 * Share this computer with one Remote Crew. Resolves, never throws, except that
 * an auth failure at step 1 rethrows its `ApiError` so the page shows it as
 * one (a sign-in problem is not a refusal of this feature).
 *
 * `mintConsent` is step 1 (injected so the caller keeps its React Query mutation).
 */
export async function shareThisComputer(
  mintConsent: () => Promise<{ consent?: unknown }>,
): Promise<ShareOutcome> {
  const api = bridge()
  if (!api) return { ok: false, refusal: 'needs_app', code: 'unavailable' }
  let consent: unknown
  try {
    consent = (await mintConsent()).consent
  } catch (e) {
    const code = e instanceof ApiError ? apiErrorCode(e) : ''
    const refusal = shareRefusal(code)
    if (refusal === 'failed') throw e
    return { ok: false, refusal, code }
  }
  if (typeof consent !== 'string' || !consent) return { ok: false, refusal: 'failed', code: 'no_consent' }
  try {
    const result = await api.confirm(consent)
    if (result?.ok === true) return { ok: true }
    const code = result?.error || 'failed'
    return { ok: false, refusal: shareRefusal(code), code }
  } catch {
    // The shell refuses a window whose gateway is not this computer's.
    return { ok: false, refusal: 'not_local', code: 'bridge_refused' }
  }
}
