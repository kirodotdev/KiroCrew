/**
 * "Share this computer" (issue 29949): the renderer's part of the native consent.
 *
 * The page names only which of this gateway's Remote Crews to share with, and
 * decides neither the folder nor the tools. It hands that crew's id to the
 * desktop shell's `connectedComputersAPI.share`. The shell's main process starts
 * this computer's desktop host on a pipe only it holds, reads the crew name and
 * tools from it, shows the native confirmation, the OS folder picker and one
 * yes/no per local tool, and only then has that host write the grant. The
 * gateway has no route that grants.
 *
 * A browser tab and the PWA have no bridge, so they cannot share at all (B13).
 */

interface ConnectedComputersBridge {
  share: (instanceId: string) => Promise<{ ok?: boolean; grant?: unknown; error?: string } | undefined>
}

function bridge(): ConnectedComputersBridge | undefined {
  const api = (window as { connectedComputersAPI?: Partial<ConnectedComputersBridge> }).connectedComputersAPI
  return typeof api?.share === 'function' ? (api as ConnectedComputersBridge) : undefined
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

/** The refusal a desktop shell or host error code stands for. */
export function shareRefusal(code: string | undefined): ShareRefusal {
  switch (code) {
    case 'cancelled': return 'cancelled'
    case 'unavailable': return 'needs_app'
    case 'not_local': return 'not_local'
    case 'connected_computers_disabled': return 'disabled'
    case 'protection_unavailable': return 'protection_unavailable'
    case 'protection_broken': return 'protection_broken'
    case 'turned_off': return 'turned_off'
    case 'enrollment_failed':
    case 'refused':
      return 'enrollment_failed'
    case 'unknown_instance': return 'unknown_instance'
    case 'no_consent': return 'expired'
    default: return 'failed'
  }
}

export type ShareOutcome = { ok: true } | { ok: false; refusal: ShareRefusal; code: string }

/** Share this computer with the Remote Crew `instanceId`. Resolves, never throws. */
export async function shareThisComputer(instanceId: string): Promise<ShareOutcome> {
  const api = bridge()
  if (!api) return { ok: false, refusal: 'needs_app', code: 'unavailable' }
  try {
    const result = await api.share(instanceId)
    if (result?.ok === true) return { ok: true }
    const code = result?.error || 'failed'
    return { ok: false, refusal: shareRefusal(code), code }
  } catch {
    // The shell refuses a window whose gateway is not this computer's.
    return { ok: false, refusal: 'not_local', code: 'bridge_refused' }
  }
}
