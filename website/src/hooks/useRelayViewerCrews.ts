/**
 * The desktop app's list of remote Crews reachable through the Kiro relay.
 *
 * Two gates, both needed: the desktop shell's `relayCrewAPI` preload bridge (a
 * plain browser tab and the PWA have none), and the gateway's crews route, which
 * answers 404 while `tunnel.relay_viewer_enabled` is off. Either one missing
 * hides the whole list, so the default dashboard is unchanged.
 */
import { useQuery } from '@tanstack/react-query'
import { api, ApiError, type RelayCrewEntry } from '../api/client'
import { parseErrorCode } from '../utils/errorReport'
import { i18nT } from '../i18n/t'

export type RelayCrewOpenResult = { ok: boolean; error?: string }
type RelayCrewBridge = { open: (endpointId: string) => Promise<RelayCrewOpenResult> }

/** Read lazily, not at module load, so a test can stub `window.relayCrewAPI` per case. */
function relayCrewBridge(): RelayCrewBridge | null {
  const b = (window as { relayCrewAPI?: Partial<RelayCrewBridge> }).relayCrewAPI
  return typeof b?.open === 'function' ? (b as RelayCrewBridge) : null
}

/** Hand ONLY the endpoint ID to the desktop shell. Main mints the access token
 *  and loads the Crew in its own window; nothing else crosses the bridge. */
export async function openRelayCrew(endpointId: string): Promise<RelayCrewOpenResult> {
  const bridge = relayCrewBridge()
  if (!bridge) return { ok: false, error: 'UNAVAILABLE' }
  try {
    return await bridge.open(endpointId)
  } catch {
    return { ok: false, error: 'UNAVAILABLE' }
  }
}

/** Plain text for a typed refusal code (the control plane client's codes, design
 *  section 5). Matched case-insensitively; every other code, the bridge's own
 *  INVALID_ENDPOINT and UNAVAILABLE included, gets the generic text. */
export function relayRefusalText(code: string | undefined, generic?: string): string {
  switch ((code || '').toUpperCase()) {
    case 'CREW_ACCESS_NOT_GRANTED': return i18nT('components.relayCrews.refusal_not_granted')
    case 'REMOTE_ACCESS_DISABLED': return i18nT('components.relayCrews.refusal_remote_access_off')
    case 'CREW_OFFLINE': return i18nT('components.relayCrews.refusal_offline')
    case 'THROTTLED': return i18nT('components.relayCrews.refusal_throttled')
    case 'SIGNED_OUT': return i18nT('components.relayCrews.refusal_signed_out')
    case 'NOT_SET_UP': return i18nT('components.relayCrews.refusal_not_configured')
    default: return generic ?? i18nT('components.relayCrews.refusal_generic')
  }
}

/** A late heartbeat still counts as online: the Crew is probably up, and greying it would mislead. */
const ONLINE_PRESENCE = new Set(['CONNECTED', 'HEARTBEAT_LATE'])

export function isRelayCrewOnline(entry: RelayCrewEntry): boolean {
  return ONLINE_PRESENCE.has(entry.presence.toUpperCase())
}

export const RELAY_VIEWER_CREWS_KEY = ['relay-viewer', 'crews'] as const

const isOffError = (err: unknown) => err instanceof ApiError && err.status === 404

export function useRelayViewerCrews(): {
  hidden: boolean
  crews: RelayCrewEntry[]
  refusal: string | null
} {
  const hasBridge = relayCrewBridge() !== null
  const q = useQuery({
    queryKey: RELAY_VIEWER_CREWS_KEY,
    queryFn: () => api.relayViewerCrews(),
    enabled: hasBridge,
    // A 404 is the feature-off answer, not a blip to retry.
    retry: false,
    // Presence changes on the host's side, so re-read; stop once the route says the feature is off.
    refetchInterval: query => (isOffError(query.state.error) ? false : 60_000),
  })
  const off = isOffError(q.error)
  if (!hasBridge || off || q.isPending) return { hidden: true, crews: [], refusal: null }
  if (q.error) {
    const code = q.error instanceof ApiError ? parseErrorCode(q.error.body) : undefined
    // A load failure must not read as an Open failure, so the list has its own fallback.
    return { hidden: false, crews: [], refusal: relayRefusalText(code, i18nT('components.relayCrews.refusal_list_generic')) }
  }
  return { hidden: false, crews: q.data?.crews ?? [], refusal: null }
}
