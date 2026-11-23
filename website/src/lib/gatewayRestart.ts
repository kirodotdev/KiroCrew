/** How long a restart request counts as pending, and how long the update's
 *  restart latch (`hooks/websocket/bundleReload`) stays honored: a restart
 *  resolves in seconds, so one older than this died before exec. */
export const UPDATE_RESTART_LATCH_TTL_MS = 15 * 60 * 1000

/** When this tab last asked the gateway to restart (`api.restartGateway`, or
 *  an in-app update's `restarting` step), so the socket drop that follows reads
 *  as the restart the user asked for and not as a gateway that stopped. Cleared
 *  on the next connect, or when the request came back refused. */
let requestedAt: number | null = null
/** Whether that request waits through the restart in this tab, so its
 *  `restarting` step arms no reload latch. */
let keepsTab = false

export function noteGatewayRestart(now: number = Date.now(), { keepTab = false }: { keepTab?: boolean } = {}): void {
  requestedAt = now
  keepsTab = keepTab
}

export function clearGatewayRestart(): void {
  requestedAt = null
  keepsTab = false
}

/** True once when the pending request asked to keep this tab loaded. */
export function takeRestartKeepsTab(): boolean {
  const kept = keepsTab && gatewayRestartPendingMs() > 0
  keepsTab = false
  return kept
}

/** How much longer a requested restart counts as pending, in ms (0 when none).
 *  shortcut: bounded by the update latch's TTL, so a restart that never comes
 *  back holds the host cover off that long; upgrade to a status probe if that
 *  wait shows up in practice. */
export function gatewayRestartPendingMs(now: number = Date.now()): number {
  if (requestedAt === null) return 0
  return Math.max(0, requestedAt + UPDATE_RESTART_LATCH_TTL_MS - now)
}
