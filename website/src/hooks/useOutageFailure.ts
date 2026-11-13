import { useEffect, useState, useSyncExternalStore } from 'react'

import { isGatewayConnected, subscribeGatewayConnected } from '../utils/errorReport'

/**
 * Whether a status read's CURRENT failure belongs to a dashboard outage: it
 * failed while the dashboard was disconnected from the gateway (a gateway
 * restart is one), or it is that same failure still cached after the
 * connection came back.
 *
 * The offline state already says the gateway cannot be reached, so a
 * per-feature "could not read the status" alert for the same outage is a
 * second, causeless error over nothing wrong (#32966). Once the connection is
 * back, `refetch` re-reads the outage's failure, so a reader that does not poll
 * is not left on it. A failure after the connection is back is a new one (React
 * Query stamps each failed fetch with a new `errorUpdatedAt`), so a read that
 * really fails is still reported.
 *
 * Reads the socket layer's context-free connection mirror, as `ErrorNotice`
 * does, so it works with or without the store's Provider.
 */
export function useOutageFailure(isError: boolean, errorUpdatedAt: number, refetch: () => unknown): boolean {
  const connected = useSyncExternalStore(subscribeGatewayConnected, isGatewayConnected)
  // The `errorUpdatedAt` of the last failure seen while disconnected.
  const [outageFailureAt, setOutageFailureAt] = useState<number | null>(null)
  const outage = isError && (!connected || outageFailureAt === errorUpdatedAt)
  useEffect(() => {
    if (!connected && isError) setOutageFailureAt(errorUpdatedAt)
  }, [connected, isError, errorUpdatedAt])
  // Back online with the outage's failure still cached: read it again. Runs
  // once per outage failure: the re-read either succeeds or fails with a new
  // `errorUpdatedAt`, and both end `outage`.
  const stale = connected && outage
  useEffect(() => {
    if (stale) refetch()
  }, [stale, errorUpdatedAt]) // eslint-disable-line react-hooks/exhaustive-deps -- `refetch` is a fresh closure each render
  return outage
}
