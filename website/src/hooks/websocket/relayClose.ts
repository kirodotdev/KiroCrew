import type { OfflineCause } from '../../store/dashboardSlice'

/** What a dashboard socket's close code says when the page came through the
 *  Kiro relay (a Kiro Mobile WebView or a desktop relay Crew window).
 *
 *  The codes are the relay's (protocol v1, `test/fixtures/relay_protocol_v1`):
 *  4401 when the token the socket opened with expires, 1012 for a drain or a
 *  move, 4404 when the Crew's host connection is gone, 4410 when the Crew's
 *  owner turned Remote Control off, and the rest of the
 *  block 4400 to 4499 for the relay's own refusals. The relay
 *  rewrites a Crew close code in that block to 1011, and Crew's own gateway
 *  sends none of these on `/api/ws`, so a direct connection reads as
 *  `offline`, except for 1000.
 *
 *  1000 (normal closure) the page did not ask for is a drain too. The gateway
 *  never ends `/api/ws` with it, but a proxy in front of the dashboard does when
 *  it ends a socket it has held long enough and has no close code of its own
 *  to forward. The reconnect is quiet: if it does not open within the grace,
 *  the outage shows as for any other drain. */

/** `token_renewal` and `relay_drain` reconnect quietly: the app holding the
 *  token has already renewed it, so the next socket opens with the new one. */
export type SocketCloseKind = 'token_renewal' | 'relay_drain' | 'refused' | 'remote_off' | 'offline'

export const CLOSE_TOKEN_EXPIRED = 4401
export const CLOSE_RELAY_DRAIN = 1012
/** Normal closure the page did not ask for: a proxy ended a long-held socket. */
export const CLOSE_NORMAL = 1000
/** `crew_not_connected`: the relay has no host connection for this Crew (its
 *  gateway stopped or dropped). The relay accepts every later upgrade and
 *  closes it with this code until the host is back, so it is an outage that is
 *  still being retried, not a refusal of this viewer. */
export const CLOSE_CREW_NOT_CONNECTED = 4404
/** The Crew turned Remote Control off and released its lease; the relay closes
 *  every viewer with this code. Only the Crew's owner can turn it back on, on
 *  the Crew's own computer, so the viewer waits calmly rather than reading it
 *  as a refusal of this viewer. */
export const CLOSE_REMOTE_CONTROL_OFF = 4410
const RELAY_BLOCK_FIRST = 4400
const RELAY_BLOCK_LAST = 4499

/** The quiet reconnect's grace; defined beside the backoff so the shell's host
 *  cover waits the same time. */
export { QUIET_RECONNECT_GRACE_MS } from '../../utils/reconnectBackoff'

export function classifyClose(code: number | undefined): SocketCloseKind {
  if (code === CLOSE_TOKEN_EXPIRED) return 'token_renewal'
  if (code === CLOSE_RELAY_DRAIN || code === CLOSE_NORMAL) return 'relay_drain'
  if (code === CLOSE_CREW_NOT_CONNECTED) return 'offline'
  if (code === CLOSE_REMOTE_CONTROL_OFF) return 'remote_off'
  if (typeof code === 'number' && code >= RELAY_BLOCK_FIRST && code <= RELAY_BLOCK_LAST) return 'refused'
  return 'offline'
}

/** The offline cause a socket's close leaves on screen, given the one before it.
 *  A refusal and Remote Control off stay until a socket opens, so a later
 *  network drop keeps their wording. Only the relay's own `crew_not_connected`
 *  (4404) replaces a refusal: it says the host is gone, which a retry can fix.
 *  After Remote Control off the relay answers 4404 too, and that is still why. */
export function nextOfflineCause(
  previous: OfflineCause | undefined,
  kind: SocketCloseKind,
  code: number | undefined,
  tokenRenewalFailed: boolean,
): OfflineCause | undefined {
  if (tokenRenewalFailed) return 'session_ended'
  if (kind === 'refused' || kind === 'remote_off') return kind
  if (code === CLOSE_CREW_NOT_CONNECTED && previous === 'refused') return undefined
  return previous
}

export function isQuietClose(kind: SocketCloseKind): boolean {
  return kind === 'token_renewal' || kind === 'relay_drain'
}
