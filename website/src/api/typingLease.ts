/**
 * The composer typing lease, on the client side.
 *
 * The host holds one lease per shared session: while one collaborator types,
 * everyone else's composer is read-only, and the host refuses their send with
 * `409 composer_locked`. The lease state arrives as `shared_crew.typing_lease`
 * frames and the `/api/shared-crew/typing-lease` read (./sharedCrew). A refused
 * send carries the lease too, and is recorded here to trigger a fresh read.
 * Response arrival time does not order server state. The refused draft is
 * never touched.
 */
import { useSyncExternalStore } from 'react'
import type { ComposerLockedBody, PublicPrincipal, TypingLease } from './sharedCrew'

export function isComposerLocked(body: unknown): body is ComposerLockedBody {
  if (!body || typeof body !== 'object') return false
  const b = body as Partial<ComposerLockedBody>
  return b.code === 'composer_locked' && !!b.holder && typeof b.holder === 'object' && !!b.lease && typeof b.lease === 'object'
}

/** Whether *lease* locks a tab whose verified principal is *viewer*. Fails open:
 *  with no known viewer the tab is not locked, and the host still refuses. */
export function leaseLocks(lease: TypingLease | null | undefined, viewer: PublicPrincipal | null | undefined): boolean {
  if (!lease?.holder || !viewer?.id) return false
  const ids = lease.holder_ids?.length ? lease.holder_ids : [lease.holder.id]
  return !ids.includes(viewer.id)
}

interface Refused {
  lease: TypingLease
  at: number
}

const refused = new Map<string, Refused>()
const listeners = new Set<() => void>()

function emit(): void {
  for (const l of listeners) l()
}

/** Record the lease a `composer_locked` refusal carried. */
export function noteComposerLocked(body: ComposerLockedBody): void {
  if (!body.session_key) return
  refused.set(body.session_key, { lease: body.lease, at: Date.now() })
  emit()
}

export function refusedLeaseFor(sessionKey: string | null | undefined): Refused | null {
  return (sessionKey && refused.get(sessionKey)) || null
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** The lease the last refusal in *sessionKey* reported, re-rendering on change. */
export function useRefusedLease(sessionKey: string | null | undefined): Refused | null {
  return useSyncExternalStore(subscribe, () => refusedLeaseFor(sessionKey), () => null)
}

/** Test seam. */
export function resetTypingLeaseForTests(): void {
  refused.clear()
  emit()
}
