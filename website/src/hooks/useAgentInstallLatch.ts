import { useEffect } from 'react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { updateInfoQuery } from '../api/updateInfoQuery'
import { useInstallDispatch } from './useInstallDispatch'
import type { UpdateState } from './useUpdateSubscription'

/**
 * An Install click on an agent's update request whose download is still
 * running: the request to retire, and whether that download was seen to start.
 * `null` when nothing is armed.
 *
 * Held in the query cache and finished by {@link useAgentInstallLatch}, mounted
 * once in shell/appShell.tsx, because the panel that sets it mounts only on the
 * About tab: leaving the tab mid-download must not drop the install the user
 * approved, nor leave the request armed on the gateway.
 */
type AgentInstallLatch = { requestId: string | undefined; sawDownload: boolean }

/** The ONE definition of the latch's cache entry; readers pass it to useQuery. */
export const agentInstallLatchQuery = {
  queryKey: ['agent-install-latch'] as const,
  queryFn: (): AgentInstallLatch | null => null,
  enabled: false,
  staleTime: Infinity,
}

/**
 * The install the latch dispatched, for the About panel to show as its own: in
 * flight, and refused.
 */
export const agentInstallDispatchQuery = {
  queryKey: ['agent-install-dispatch'] as const,
  queryFn: (): { dispatched: boolean; failed: boolean } | null => null,
  enabled: false,
  staleTime: Infinity,
  // Never collected: the shell can write a refusal while About is closed.
  gcTime: Infinity,
}

export function armAgentInstall(qc: QueryClient, requestId: string | undefined) {
  qc.setQueryData<AgentInstallLatch | null>(agentInstallLatchQuery.queryKey, { requestId, sawDownload: false })
}

export function releaseAgentInstall(qc: QueryClient) {
  qc.setQueryData<AgentInstallLatch | null>(agentInstallLatchQuery.queryKey, null)
}

/**
 * Finishes an armed click wherever the user is: installs the download it
 * started once it lands, or releases the click when that download fails or is
 * dropped. A click left armed keeps both request actions disabled and installs
 * whatever a later download brings, with nobody clicking.
 */
export function useAgentInstallLatch() {
  const queryClient = useQueryClient()
  const { data: updateState } = useQuery<UpdateState | null>({
    queryKey: ['update-state'],
    queryFn: () => null,
    enabled: false,
    staleTime: Infinity,
  })
  const { data: latch } = useQuery(agentInstallLatchQuery)
  const { installMutation, dispatched: installDispatched, dispatchInstall } = useInstallDispatch(updateState)
  const installFailed = installMutation.isError
  useEffect(() => {
    queryClient.setQueryData(agentInstallDispatchQuery.queryKey, { dispatched: installDispatched, failed: installFailed })
  }, [installDispatched, installFailed, queryClient])
  // Query-cache listeners run for every updater payload, before React Query
  // batches observer renders, so the latch read here keeps the event order for
  // one armed click even when `downloading` and its cancellation share a render.
  //
  // Read in that order, a payload also says when a click's request ended
  // before any download started. For a click made with nothing found, the
  // main process checks first: `not-available` and a failed check end the
  // request there, and so does `found` on the feed lane with auto-download
  // off, where it waits for a Download click. The managed lane follows its
  // `found` with `downloaded` at once, and with auto-download on the download
  // starts; the feed lane says which on the `found` itself, and a shell too old
  // to say leaves the cached preference to answer. The updater's own
  // download-phase failure ends a click whenever it is pushed. The rendered
  // state cannot say any of this: a click is also made FROM each of them, a
  // failed download's Retry included.
  useEffect(() => queryClient.getQueryCache().subscribe(event => {
    if (event.type !== 'updated'
      || event.query.queryKey.length !== 1
      || event.query.queryKey[0] !== 'update-state') return
    const pushed = event.query.state.data as UpdateState | null | undefined
    const armed = queryClient.getQueryData<AgentInstallLatch | null>(agentInstallLatchQuery.queryKey)
    if (!armed) return
    if (pushed?.state === 'downloading') {
      if (!armed.sawDownload) markDownloadSeen(queryClient, armed)
      return
    }
    const info = queryClient.getQueryData<UpdateInfo | null>(updateInfoQuery.queryKey)
    const ended = (pushed?.state === 'error' && pushed.phase === 'download') || (armed.sawDownload
      ? pushed?.state === 'not-available' || pushed?.state === 'checking'
      : pushed?.state === 'not-available'
        || (pushed?.state === 'error' && pushed.phase === 'check')
        || (pushed?.state === 'found' && (pushed.autoDownload ?? info?.autoDownload) === false && !info?.updateCommand))
    if (ended) releaseAgentInstall(queryClient)
  }), [queryClient])
  // A click made while a download is already running sees no fresh push of
  // `downloading` before the state it is in.
  useEffect(() => {
    if (latch && !latch.sawDownload && updateState?.state === 'downloading') markDownloadSeen(queryClient, latch)
  }, [latch, updateState?.state, queryClient])
  const landed = !!latch && updateState?.state === 'downloaded'
  useEffect(() => {
    // The download the request card started has landed: retire the request,
    // best-effort, as AboutPanel's retireAgentRequest does, and install. One
    // shot, so a later, unrelated download does not ride this click.
    if (!landed || installDispatched) return
    const requestId = latch?.requestId
    releaseAgentInstall(queryClient)
    if (requestId) void api.dismissUpdateArm(requestId).catch(() => {})
    dispatchInstall()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [landed, installDispatched])
  // A download the main process DROPS after it started (it re-checks the feed
  // first, and a retraction or a channel switch ends it without an error)
  // releases the click. Only AFTER the click's `downloading`: both states are
  // also ones a click is made FROM, and from there the main process discovers
  // first and then downloads.
  const ended = !!latch && latch.sawDownload
    && (updateState?.state === 'not-available' || updateState?.state === 'checking')
  useEffect(() => {
    if (ended) releaseAgentInstall(queryClient)
  }, [ended, queryClient])
}

function markDownloadSeen(qc: QueryClient, latch: AgentInstallLatch) {
  qc.setQueryData<AgentInstallLatch | null>(agentInstallLatchQuery.queryKey, { ...latch, sawDownload: true })
}
