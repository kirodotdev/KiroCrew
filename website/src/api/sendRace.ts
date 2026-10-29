/**
 * First submit wins, on the client side.
 *
 * The host counts accepted sends per session (its *send revision*). This tab
 * remembers the highest revision it has seen for each session, from the
 * `shared_crew.send_state` frame, the send-state read and a conflict answer, and
 * sends it as `base_revision`. When a collaborator's send was accepted after that
 * revision the host refuses this send with a `send_race` conflict naming them.
 * The draft then stays in the composer, and the sender decides: Send now (a new
 * submission at the current revision, which may queue) or leave it.
 *
 * Nothing here ever submits a draft by itself. A frame or a read only moves the
 * revision up, so a late read can never make this tab think it has seen less
 * than it has. A conflict answer is the host's current revision at the moment it
 * refused this tab, so it replaces what the tab knew, even when lower: when the
 * host process starts again it counts from zero, and a tab still holding a
 * higher revision would otherwise lose every send, Send now included.
 */
import { useSyncExternalStore } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { queryClient as appQueryClient } from './queryClient'
import type { AcceptedSend, SendRaceConflictBody, SendState } from './sharedCrew'

const revisions = new Map<string, number>()

export const sendStateQueryKey = (sessionKey: string) => ['shared-crew', 'send-state', sessionKey] as const

/** Record that this tab has seen *revision* for *sessionKey*. Only moves up. */
export function noteSendRevision(sessionKey: string | null | undefined, revision: unknown): void {
  if (!sessionKey || typeof revision !== 'number' || !Number.isInteger(revision) || revision < 0) return
  const prev = revisions.get(sessionKey)
  if (prev === undefined || revision > prev) revisions.set(sessionKey, revision)
}

/** The revision a send into *sessionKey* is based on, or `undefined` when this
 *  tab has not learned one (the host then does not compare the send). */
export function baseRevisionFor(sessionKey: string | null | undefined): number | undefined {
  return sessionKey ? revisions.get(sessionKey) : undefined
}

/** The client-chosen key that makes a retry of the same send answer once. */
export function sendIdempotencyKey(sendId: string): string {
  return 'send-' + sendId
}

export function isSendRaceConflict(body: unknown): body is SendRaceConflictBody {
  if (!body || typeof body !== 'object') return false
  const b = body as Partial<SendRaceConflictBody>
  return b.code === 'send_race' && typeof b.current_revision === 'number' && !!b.winner && typeof b.winner === 'object'
}

/** What the composer shows after losing a race in one session. */
export interface SendRaceNotice {
  winner: AcceptedSend
  currentRevision: number
}

const notices = new Map<string, SendRaceNotice>()
const listeners = new Set<() => void>()

function emit(): void {
  for (const l of listeners) l()
}

export function setSendRaceNotice(sessionKey: string, conflict: SendRaceConflictBody, client: QueryClient = appQueryClient): void {
  const current = conflict.current_revision
  if (Number.isInteger(current) && current >= 0) {
    const previous = revisions.get(sessionKey)
    const queryKey = sendStateQueryKey(sessionKey)
    const cached = client.getQueryData<SendState | null>(queryKey)
    revisions.set(sessionKey, current)
    if ((previous !== undefined && previous > current) || (cached && cached.revision > current)) {
      // A lower authoritative revision starts a fresh cache, including any read
      // in flight, so returning to the session cannot restore pre-restart state.
      void client.resetQueries({ queryKey, exact: true })
    }
  }
  notices.set(sessionKey, { winner: conflict.winner, currentRevision: conflict.current_revision })
  emit()
}

export function clearSendRaceNotice(sessionKey: string | null | undefined): void {
  if (!sessionKey || !notices.has(sessionKey)) return
  notices.delete(sessionKey)
  emit()
}

export function sendRaceNoticeFor(sessionKey: string | null | undefined): SendRaceNotice | null {
  return (sessionKey && notices.get(sessionKey)) || null
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** The race notice for *sessionKey*, re-rendering when it changes. */
export function useSendRaceNotice(sessionKey: string | null | undefined): SendRaceNotice | null {
  return useSyncExternalStore(subscribe, () => sendRaceNoticeFor(sessionKey), () => null)
}

/** Test seam: forget every revision and notice. */
export function resetSendRaceForTests(): void {
  revisions.clear()
  notices.clear()
  emit()
}
