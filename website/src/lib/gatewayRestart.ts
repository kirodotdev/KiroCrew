/** How long a restart request counts as pending, and how long the update's
 *  restart latch (`hooks/websocket/bundleReload`) stays honored: a restart
 *  resolves in seconds, so one older than this died before exec. */
export const UPDATE_RESTART_LATCH_TTL_MS = 15 * 60 * 1000

/** When this tab last asked the gateway to restart (`api.restartGateway`, or
 *  an in-app update's `restarting` step), so the socket drop that follows reads
 *  as the restart the user asked for and not as a gateway that stopped. Cleared
 *  on the next connect, or when the request came back refused. */
let requestedAt: number | null = null

export function noteGatewayRestart(now: number = Date.now()): void {
  requestedAt = now
}

export function clearGatewayRestart(): void {
  requestedAt = null
}

/** How much longer a requested restart counts as pending, in ms (0 when none).
 *  shortcut: bounded by the update latch's TTL, so a restart that never comes
 *  back holds the host cover off that long; upgrade to a status probe if that
 *  wait shows up in practice. */
export function gatewayRestartPendingMs(now: number = Date.now()): number {
  if (requestedAt === null) return 0
  return Math.max(0, requestedAt + UPDATE_RESTART_LATCH_TTL_MS - now)
}
