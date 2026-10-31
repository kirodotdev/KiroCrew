/**
 * React Query reads for Shared Crew. Each except the mode answers `null` when
 * the gateway does not serve the route, so a consumer renders "not available"
 * rather than an error. Live updates arrive as socket frames (./sharedCrewSocket) into these
 * same cache entries.
 */
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { api } from '../../api/client'
import { noteSendRevision } from '../../api/sendRace'
import type { CreatorFilter, SendState } from '../../api/sharedCrew'
import { ALL_UNAVAILABLE } from '../../api/sharedCrewUi'
import { latestSendState, sharedCrewKeys } from './sharedCrewSocket'

/** The Crew's capabilities; all unavailable until the gateway says otherwise. */
export function useSharedCrewCapabilities({ fetchOnMount = true }: { fetchOnMount?: boolean } = {}) {
  const query = useQuery({
    queryKey: sharedCrewKeys.capabilities,
    queryFn: () => api.sharedCrewCapabilities(),
    staleTime: 60_000,
    retry: false,
    refetchOnMount: fetchOnMount,
    retryOnMount: fetchOnMount,
  })
  return { ...query, capabilities: query.data ?? ALL_UNAVAILABLE }
}

/** Who this tab is, as the server derived it; `null` when it reports no one. */
export function useSharedCrewViewer(enabled = true) {
  return useQuery({ queryKey: sharedCrewKeys.viewer, queryFn: () => api.sharedCrewViewer(), enabled, staleTime: 60_000, retry: false })
}

/** This Crew's People/setup mode. A missing or failing route is a query error,
 *  which every consumer renders as an error, never as a personal Crew. */
export function useSharedCrewMode(enabled = true, { fetchOnMount = true }: { fetchOnMount?: boolean } = {}) {
  return useQuery({
    queryKey: sharedCrewKeys.mode,
    queryFn: () => api.sharedCrewMode(),
    enabled,
    staleTime: 60_000,
    retry: false,
    refetchOnMount: fetchOnMount,
    retryOnMount: fetchOnMount,
  })
}

export function useSharedCrewPeople(enabled = true, { fetchOnMount = true }: { fetchOnMount?: boolean } = {}) {
  return useQuery({ queryKey: sharedCrewKeys.people, queryFn: () => api.sharedCrewPeople(), enabled, retry: false, refetchOnMount: fetchOnMount, retryOnMount: fetchOnMount })
}

export function useSharedCrewCapacity(enabled = true) {
  return useQuery({ queryKey: sharedCrewKeys.capacity, queryFn: () => api.sharedCrewCapacity(), enabled, retry: false })
}

export function useSessionWork(sessionKey: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: sharedCrewKeys.work(sessionKey ?? ''),
    queryFn: () => api.sharedCrewWork(sessionKey ?? ''),
    enabled: enabled && !!sessionKey,
    retry: false,
  })
}

export function useSessionPresence(sessionKey: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: sharedCrewKeys.presence(sessionKey ?? ''),
    queryFn: () => api.sharedCrewPresence(sessionKey ?? ''),
    enabled: enabled && !!sessionKey,
    retry: false,
  })
}

export function useSessionDecisions(sessionKey: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: sharedCrewKeys.decisions(sessionKey ?? ''),
    queryFn: () => api.sharedCrewDecisions(sessionKey ?? ''),
    enabled: enabled && !!sessionKey,
    retry: false,
  })
}

/** The session's send revision, read on open and after every reconnect (the
 *  socket's open invalidates `shared-crew`), then kept current by frames. The
 *  revision it reads is what this tab's next send is based on. */
export function useSessionSendState(sessionKey: string | null | undefined, enabled = true) {
  const client = useQueryClient()
  const query = useQuery({
    queryKey: sharedCrewKeys.sendState(sessionKey ?? ''),
    queryFn: () => api.sharedCrewSendState(sessionKey ?? ''),
    structuralSharing: latestSendState,
    enabled: enabled && !!sessionKey,
    retry: false,
  })
  useEffect(() => {
    if (sessionKey) noteSendRevision(sessionKey, client.getQueryData<SendState | null>(sharedCrewKeys.sendState(sessionKey))?.revision)
  }, [client, sessionKey, query.data])
  return query
}

/** Who holds the session's composer typing lease, read on open and after every
 *  reconnect, then kept current by `shared_crew.typing_lease` frames. */
export function useSessionTypingLease(sessionKey: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: sharedCrewKeys.typingLease(sessionKey ?? ''),
    queryFn: () => api.sharedCrewTypingLease(sessionKey ?? ''),
    enabled: enabled && !!sessionKey,
    retry: false,
  })
}

/** One server-filtered page per cursor; the caller holds the cursor list. */
export function useSharedCrewSessionsPage(creator: CreatorFilter, q: string, cursor: string, enabled = true) {
  return useQuery({
    queryKey: [...sharedCrewKeys.sessions(creator, q), cursor],
    queryFn: () => api.sharedCrewSessions({ creator, q: q || undefined, cursor: cursor || undefined, limit: 50 }),
    staleTime: 0,
    enabled,
    retry: false,
    placeholderData: prev => prev,
  })
}
