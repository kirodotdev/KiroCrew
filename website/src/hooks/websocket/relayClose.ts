/** What a dashboard socket's close code says when the page came through the
 *  Kiro relay (a Kiro Mobile WebView or a desktop relay Crew window).
 *
 *  The codes are the relay's (protocol v1, `test/fixtures/relay_protocol_v1`):
 *  4401 when the token the socket opened with expires, 1012 for a drain or a
 *  move, and the block 4400 to 4499 for the relay's own refusals. The relay
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
export type SocketCloseKind = 'token_renewal' | 'relay_drain' | 'refused' | 'offline'

export const CLOSE_TOKEN_EXPIRED = 4401
export const CLOSE_RELAY_DRAIN = 1012
/** Normal closure the page did not ask for: a proxy ended a long-held socket. */
export const CLOSE_NORMAL = 1000
const RELAY_BLOCK_FIRST = 4400
const RELAY_BLOCK_LAST = 4499

/** How long a quiet reconnect may take to open before the dashboard says it is
 *  offline. The relay's own reconnect lands in about a second. */
export const QUIET_RECONNECT_GRACE_MS = 5000

export function classifyClose(code: number | undefined): SocketCloseKind {
  if (code === CLOSE_TOKEN_EXPIRED) return 'token_renewal'
  if (code === CLOSE_RELAY_DRAIN || code === CLOSE_NORMAL) return 'relay_drain'
  if (typeof code === 'number' && code >= RELAY_BLOCK_FIRST && code <= RELAY_BLOCK_LAST) return 'refused'
  return 'offline'
}

export function isQuietClose(kind: SocketCloseKind): boolean {
  return kind === 'token_renewal' || kind === 'relay_drain'
}
