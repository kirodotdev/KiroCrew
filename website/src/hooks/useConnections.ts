import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ChannelConnection, ConnectionsPayload } from '../types'

/**
 * Per-connection enrolment + ceiling state, shared by every surface that shows
 * it (the session header chip, the sidebar glyph, Settings).
 *
 * Polled rather than fetched once: enrolment lives in the trust roster and the
 * ceiling in a hot-reloading profile, so both can change under an open tab. The
 * interval matches the sibling `governance-channels` query so a tightened policy
 * and a revoked enrolment surface on the same beat.
 */
export function useConnections() {
  return useQuery({
    queryKey: ['connections'],
    queryFn: () => api.getConnections(),
    staleTime: 30_000,
    refetchInterval: 30_000,
    retry: false,
  })
}

/** The connection a channel-originated session belongs to, or `undefined`.
 *
 * Matches on the TRANSPORT because a session key's surface segment carries the
 * transport, and today every transport has exactly one connection. A transport
 * that grows a second one also teaches the key format its connection name, and
 * this is the single place that has to learn to read it.
 */
export function connectionForTransport(
  payload: ConnectionsPayload | undefined,
  transport: string,
): ChannelConnection | undefined {
  // A payload with the feature switched off carries roster contents that describe
  // a gate which is not running, so no surface may read them. Returning undefined
  // here is what makes every consumer render nothing from ONE check instead of
  // four that can drift.
  if (!payload || payload.enabled === false || !transport) return undefined
  return payload.connections.find(c => c.transport === transport)
}

/** Whether this connection's reach is narrowed by governance.
 *
 * `permitted === null` is deliberately NOT restricted: that is a transient
 * evaluation failure, and rendering it as a restriction would tell the operator
 * an admin denied something when nobody did.
 */
export function isRestricted(connection: ChannelConnection | undefined): boolean {
  if (!connection) return false
  return !connection.enrolled || connection.permitted === false || connection.senders_pinned
}
