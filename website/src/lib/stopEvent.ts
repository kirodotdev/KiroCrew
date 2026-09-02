import type { ChatMessage } from '../types'

/**
 * True for the card the backend appends when a turn is stopped by the user.
 *
 * Two forms are load-bearing: the websocket path sets `kind` AND `meta.kind`,
 * while a transcript rehydrated from disk carries only the JSON-encoded `cls`
 * that `parse_cls_meta()` unpacks into `meta`. Lives in `lib/` rather than the
 * store so the protocol-layer scans can share the one definition instead of
 * re-checking half of it.
 */
export const isStopEvent = (m: ChatMessage): boolean =>
  m.kind === 'stop_event' || (m.meta as { kind?: string } | undefined)?.kind === 'stop_event'
