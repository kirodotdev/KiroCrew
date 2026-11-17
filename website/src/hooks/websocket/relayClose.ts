/** What a dashboard socket's close code says when the page came through the
 *  Kiro relay (a Kiro Mobile WebView or a desktop relay Crew window).
 *
 *  The codes are the relay's (protocol v1, `test/fixtures/relay_protocol_v1`):
 *  4401 when the token the socket opened with expires, 1012 for a drain or a
 *  move, 4404 when the Crew's host connection is gone, and the rest of the
 *  block 4400 to 4499 for the relay's own refusals. The relay
 *  rewrites a Crew close code in that block to 1011, and Crew's own gateway
 *  sends none of these on `/api/ws`, so a direct connection always reads as
 *  `offline`. */

/** `token_renewal` and `relay_drain` reconnect quietly: the app holding the
 *  token has already renewed it, so the next socket opens with the new one. */
export type SocketCloseKind = 'token_renewal' | 'relay_drain' | 'refused' | 'offline'

export const CLOSE_TOKEN_EXPIRED = 4401
export const CLOSE_RELAY_DRAIN = 1012
/** `crew_not_connected`: the relay has no host connection for this Crew (its
 *  gateway stopped or dropped). The relay accepts every later upgrade and
 *  closes it with this code until the host is back, so it is an outage that is
 *  still being retried, not a refusal of this viewer. */
export const CLOSE_CREW_NOT_CONNECTED = 4404
const RELAY_BLOCK_FIRST = 4400
const RELAY_BLOCK_LAST = 4499

/** The quiet reconnect's grace; defined beside the backoff so the shell's host
 *  cover waits the same time. */
export { QUIET_RECONNECT_GRACE_MS } from '../../utils/reconnectBackoff'

export function classifyClose(code: number | undefined): SocketCloseKind {
  if (code === CLOSE_TOKEN_EXPIRED) return 'token_renewal'
  if (code === CLOSE_RELAY_DRAIN) return 'relay_drain'
  if (code === CLOSE_CREW_NOT_CONNECTED) return 'offline'
  if (typeof code === 'number' && code >= RELAY_BLOCK_FIRST && code <= RELAY_BLOCK_LAST) return 'refused'
  return 'offline'
}

export function isQuietClose(kind: SocketCloseKind): boolean {
  return kind === 'token_renewal' || kind === 'relay_drain'
}
