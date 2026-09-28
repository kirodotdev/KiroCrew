import type { ChatMessage } from '../types'

/**
 * True for the `error` row the backend appends when it has ALREADY queued the
 * recovery that re-runs the turn.
 *
 * Role alone cannot answer this: a terminal failure and an auto-retry notice both
 * arrive as role `error` with `cls="msg msg-err"`, so a scan keying on the role
 * treats a pending retry as a finished failure and re-offers a choice that is
 * already re-running. Both carriers are load-bearing for the same reason as
 * `isStopEvent` -- the live broadcast ships `kind`, a rebuilt transcript `meta.kind`.
 */
export const isRetryNotice = (m: ChatMessage): boolean =>
  m.kind === 'transient_retry' || (m.meta as { kind?: string } | undefined)?.kind === 'transient_retry'
