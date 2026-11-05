/**
 * The send revision a send is based on, read before the send when this tab has
 * not learned one yet.
 *
 * The host compares a send only when it carries `base_revision`, so a send
 * leaving before the session's send-state read lands would be accepted even
 * after a collaborator's send. A send therefore waits for that read, bounded:
 * when it fails, answers nothing, or outlasts the bound, the send goes without a
 * base, as a gateway without the route expects. The wait joins the read the
 * open session already started (the same query the collaboration strip runs),
 * so its answer is the state from when the session opened, not one taken after
 * the press. A late answer still records its revision.
 */
import type { QueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { queryClient as appQueryClient } from '../../api/queryClient'
import { baseRevisionFor, noteSendRevision } from '../../api/sendRace'
import type { SendState } from '../../api/sharedCrew'
import { latestSendState, sharedCrewKeys } from './sharedCrewSocket'

/** How long a send waits for the session's send-state read (ms). */
export const SEND_REVISION_SEED_WAIT_MS = 1500

async function readSendRevision(client: QueryClient, sessionKey: string): Promise<number | undefined> {
  try {
    // staleTime 0: an answer already cached is re-read, while a read in flight
    // is joined rather than repeated.
    const state = await client.fetchQuery({
      queryKey: sharedCrewKeys.sendState(sessionKey),
      queryFn: () => api.sharedCrewSendState(sessionKey),
      structuralSharing: latestSendState,
      staleTime: 0,
      retry: false,
    })
    if (!state) return undefined
    // A conflict can reset the cache after this read settles but before its
    // continuation runs. Only the current cache can seed a future press.
    noteSendRevision(sessionKey, client.getQueryData<SendState | null>(sharedCrewKeys.sendState(sessionKey))?.revision)
    return Number.isInteger(state.revision) && state.revision >= 0 ? state.revision : undefined
  } catch {
    // No revision learned: the send goes without a base. SessionCollabStrip
    // owns the shared send-state query's failure notice for this open session.
    return undefined
  }
}

/** Resolve once *sessionKey*'s send revision is known, or the read failed, or
 *  *waitMs* passed, with the revision that read answered (`undefined` when it
 *  was already known, failed, or ran out of time). Never rejects. */
export async function ensureSendRevision(
  sessionKey: string | null | undefined,
  { waitMs = SEND_REVISION_SEED_WAIT_MS, client = appQueryClient }: { waitMs?: number; client?: QueryClient } = {},
): Promise<number | undefined> {
  if (!sessionKey || baseRevisionFor(sessionKey) !== undefined) return undefined
  return boundedRead(sessionKey, waitMs, client)
}

async function boundedRead(sessionKey: string, waitMs: number, client: QueryClient): Promise<number | undefined> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const bound = new Promise<undefined>(resolve => { timer = setTimeout(() => resolve(undefined), waitMs) })
  try {
    return await Promise.race([readSendRevision(client, sessionKey), bound])
  } finally {
    clearTimeout(timer)
  }
}

/** The revision a send pressed now is based on: what this tab knew at the
 *  press, or, when it knew none, what the session's read answers (see
 *  `ensureSendRevision`). Read the press-time value synchronously at the press
 *  and hand it in, so a frame for a send this tab's person has not seen yet
 *  cannot raise the base while the send is on its way. */
export async function baseRevisionForPress(
  sessionKey: string | null | undefined,
  atPress: number | undefined,
  options?: { waitMs?: number; client?: QueryClient },
): Promise<number | undefined> {
  if (atPress !== undefined) return atPress
  if (!sessionKey) return undefined
  // The base is the read's own answer, never the shared store afterwards: a
  // frame for a send accepted after the press may already have raised that.
  return boundedRead(sessionKey, options?.waitMs ?? SEND_REVISION_SEED_WAIT_MS, options?.client ?? appQueryClient)
}

/** Test seam: forget every send-state read on the app's query client. */
export function resetSendRevisionSeedForTests(): void {
  appQueryClient.removeQueries({ queryKey: ['shared-crew', 'send-state'] })
}
